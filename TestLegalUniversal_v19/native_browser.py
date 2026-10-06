"""Managed out-of-process Chromium/Chrome host with the page's own microphone.

Why a real browser and not QtWebEngine
--------------------------------------
The chat page records voice with the browser's own speech pipeline (Web Speech
API and/or a `getUserMedia` stream sent to Google). That pipeline is not part
of open-source Chromium: it is wired to the browser vendor's cloud service and
is only present in *branded* builds.

* QtWebEngine exposes `SpeechRecognition`, but `start()` kills the renderer
  (no key) — proven by the v4 logs;
* vanilla/ungoogled Chromium and Electron expose the object and never return a
  result ("Build a desktop app on Electron, and the API may be present and
  simply never return a result" — AssemblyAI);
* Microsoft Edge exposes `webkitSpeechRecognition` but MDN issue #22126
  documents it returning nothing at all.

Therefore **Google Chrome (branded) is the reference target**; everything else
is a fallback and is verified at runtime (`browser.speech_surface`).

What made the old `release/` build unstable, and what is fixed here
---------------------------------------------------------------
1. **Consent interstitials (Spain: "Aceptar todo").** A fresh profile in the EU
   gets a consent page before the chat. Worse, the old target picker matched
   *any* `google.com` page, so automation attached to `consent.google.com` and
   waited for a chat that was never there — "chrome starts and hangs".
   Fixed: consent hosts are excluded from target selection, a multi-language
   consent clicker runs on every document, and the profile is persistent so
   consent is accepted exactly once per machine.
2. **"Wide window with a single ИИ label".** `GetClientRect` returns PHYSICAL
   pixels while a PMv2-aware Chromium reads window sizes as DIPs, so on a
   125%/150% display the browser thought it was ~1.5x wider and rendered the
   desktop layout (centred wordmark) instead of the chat. Fixed by pinning the
   layout over CDP (`Emulation.setDeviceMetricsOverride`) — the CSS viewport is
   identical on every machine, only the pixels follow the display.
3. **Hangs.** Caused by profile-singleton hand-off (a stale Chromium owning the
   profile makes the new process exit immediately), by GPU/driver stalls
   (Parsec/virtual adapters, hybrid Intel+NVIDIA) and by embedding a helper
   window. Fixed with a dedicated profile + automatic fresh-profile retry, a
   `--disable-gpu` retry, checked window discovery and full rollback on any
   embedding failure.

Microphone permission is granted up front WITHOUT the flag Chrome complains
about: the content settings of the profile itself
(`profile.content_settings.exceptions.media_stream_mic`, `setting: 1` — exactly
what Chrome writes after the user presses "Allow") plus CDP
`Browser.grantPermissions` / `Browser.setPermission`. `--use-fake-ui-for-media-
stream` is not used any more: it is in Chrome's list of unsupported switches
and produces the yellow "unsupported command-line flag" bar.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

import diagnostics as diag

DEFAULT_URL = "https://google.com/ai"
ZOOM = 2.0 / 3.0            # native 67 % window zoom, as in the working builds

# Chrome stores the page zoom as a "zoom level": factor = 1.2 ** level.
# log(2/3) / log(1.2) = -2.223901614059533 — the very value the working
# `release/` build wrote into the profile preferences.
ZOOM_LEVEL = math.log(ZOOM) / math.log(1.2)
ZOOM_HOSTS = ("google.com", "www.google.com", "gemini.google.com",
              "aistudio.google.com", "accounts.google.com")

# Hosts that are NOT the chat: consent/account walls. Attaching to one of these
# is what made the old build "hang" (Spain: EU consent interstitial).
NON_APP_HOSTS = (
    "consent.google.com",
    "accounts.google.com",
    "policies.google.com",
    "support.google.com",
    "myaccount.google.com",
    "gds.google.com",
    "ogs.google.com",
    "chrome-error",
    "devtools",
    "about:blank",
    "chrome://",
)
APP_HOSTS = (
    "gemini.google.com",
    "aistudio.google.com",
    "google.com/ai",
    "bard.google.com",
    "www.google.com",
)

BROWSER_CLASSES = ("Chrome_WidgetWin_1", "Chrome_WidgetWin_0")

# Отключаемые возможности Chrome (ОДНА строка `--disable-features`).
DISABLED_FEATURES = ("SystemTitlebar", "CaptionButtons", "Translate",
                     "OptimizationHints", "MediaRouter", "GlobalMediaControls",
                     "DialMediaRouteProvider", "InProductHelp", "MenuCommands",
                     "AcceptCHFrame")
# v18: авто-тёмная тема содержимого (chrome://flags#enable-force-dark) — она и
# делала страницу тёмной на Windows с тёмной темой. Сам `prefers-color-scheme`
# фиксируется по CDP (`Emulation.setEmulatedMedia`) и в CSS (`JS_LIGHT_THEME`).
FORCE_LIGHT_FEATURES = ("WebContentsForceDark", "DarkMode")


# --------------------------------------------------------------- discovery --
class Candidate:
    __slots__ = ("name", "kind", "path", "rank")

    def __init__(self, name, kind, path, rank):
        self.name = name
        self.kind = kind
        self.path = str(path)
        self.rank = rank

    def as_dict(self):
        return {"name": self.name, "kind": self.kind, "path": self.path, "rank": self.rank}

    def __repr__(self):  # pragma: no cover - diagnostics only
        return "Candidate(%s, %s, %s)" % (self.name, self.kind, self.path)


def _exists(path):
    try:
        return bool(path) and Path(path).is_file()
    except Exception:
        return False


def _registry_app_path(exe_name):
    """`App Paths\\<exe>` is the documented, localisation-independent lookup."""
    try:
        import winreg
    except Exception:
        return None
    for hive, root in ((getattr(__import__("winreg"), "HKEY_CURRENT_USER"), "HKCU"),
                       (getattr(__import__("winreg"), "HKEY_LOCAL_MACHINE"), "HKLM")):
        try:
            key = winreg.OpenKey(
                hive, "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + exe_name)
            value, _ = winreg.QueryValueEx(key, "")
            winreg.CloseKey(key)
            if value:
                return str(value).strip('"')
        except Exception:
            continue
    return None


def _env(name):
    return os.environ.get(name) or ""


def discover_browsers(app_dir=None, configured=None):
    """Ordered candidates: pinned portable build -> Chrome -> Edge -> others.

    A pinned portable build (see `tools/fetch_chrome.py`) is the most
    deterministic option: every user gets the same engine. Installed Chrome is
    the best microphone host. Edge/Chromium are last-resort fallbacks.
    """
    app_dir = Path(app_dir) if app_dir else Path.cwd()
    local = Path(_env("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    program_files = Path(_env("ProgramFiles") or r"C:\Program Files")
    program_files_x86 = Path(_env("ProgramFiles(x86)") or r"C:\Program Files (x86)")

    out = []

    def add(name, kind, path, rank):
        if _exists(path):
            out.append(Candidate(name, kind, path, rank))

    # 0. explicit override from config.json (support/debugging)
    add("configured", "configured", configured, 0)

    # 1. pinned portable build shipped/downloaded next to the application
    for sub in ("browser", "chromium"):
        for name in ("chrome.exe", "chromium.exe", "msedge.exe"):
            add("portable", "portable", app_dir / sub / name, 1)
    add("portable", "portable", app_dir / "chrome.exe", 1)

    # 2. installed Google Chrome (the reference Web Speech implementation)
    add("chrome", "chrome", _registry_app_path("chrome.exe"), 2)
    add("chrome", "chrome", program_files / "Google" / "Chrome" / "Application" / "chrome.exe", 2)
    add("chrome", "chrome", program_files_x86 / "Google" / "Chrome" / "Application" / "chrome.exe", 2)
    add("chrome", "chrome", local / "Google" / "Chrome" / "Application" / "chrome.exe", 2)
    # v18: каналы Chrome тоже настоящие и брендированные — речевой сервис в них есть.
    for sub_dir, label in (("Chrome Beta", "chrome-beta"),
                           ("Chrome Dev", "chrome-dev"),
                           ("Chrome SxS", "chrome-canary")):
        add(label, "chrome", program_files / "Google" / sub_dir / "Application" / "chrome.exe", 2)
        add(label, "chrome", local / "Google" / sub_dir / "Application" / "chrome.exe", 2)

    # 3. Edge (present on every Windows 10/11, weaker speech support)
    add("edge", "edge", _registry_app_path("msedge.exe"), 3)
    add("edge", "edge", program_files_x86 / "Microsoft" / "Edge" / "Application" / "msedge.exe", 3)
    add("edge", "edge", program_files / "Microsoft" / "Edge" / "Application" / "msedge.exe", 3)

    # 4. other Chromium-family browsers
    # v18: Яндекс.Браузер и Opera — брендированные сборки на Chromium, их тоже
    # пробуем (у Яндекса собственный речевой сервис, но Web Speech в нём есть).
    add("yandex", "chromium", local / "Yandex" / "YandexBrowser" / "Application" / "browser.exe", 4)
    add("yandex", "chromium", program_files / "Yandex" / "YandexBrowser" / "Application" / "browser.exe", 4)
    add("opera", "chromium", local / "Programs" / "Opera" / "launcher.exe", 4)
    add("opera", "chromium", program_files / "Opera" / "launcher.exe", 4)
    add("brave", "chromium", program_files / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe", 4)
    add("brave", "chromium", local / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe", 4)
    add("vivaldi", "chromium", local / "Vivaldi" / "Application" / "vivaldi.exe", 4)
    add("chromium", "chromium", local / "Chromium" / "Application" / "chrome.exe", 5)

    # de-duplicate by resolved path, keep the best rank
    best = {}
    for cand in out:
        try:
            key = str(Path(cand.path).resolve()).lower()
        except Exception:
            key = cand.path.lower()
        if key not in best or cand.rank < best[key].rank:
            best[key] = cand
    ordered = sorted(best.values(), key=lambda c: (c.rank, c.name))
    diag.event("browser.discovered", count=len(ordered),
               candidates=[c.as_dict() for c in ordered][:8])
    return ordered


# ------------------------------------------------------------------ launch --
def build_args(exe, port, profile, url, width=453, height=735, gpu=True,
               staging=(0, 0), extra=(), lang="ru-RU", light_theme=True):
    """Flags proven in `release/` plus the stability additions of this build.

    v18:
    * `lang` — язык интерфейса браузера. Он же определяет язык ШТАТНОГО
      распознавания речи страницы: без него у пользователя из США Gemini
      распознаёт английский, хотя приложение русское.
    * `light_theme` — `--force-light-mode`: иначе на Windows с тёмной темой
      Chrome рисует страницу тёмной (у одного пользователя светлая, у другого
      тёмная — ровно из-за системной темы).
    """
    args = [
        exe,
        "--remote-debugging-port=%d" % port,
        "--remote-debugging-address=127.0.0.1",
        "--user-data-dir=%s" % profile,
        "--app=%s" % url,
        # Eдинственное изменение флагов против v9: `--use-fake-ui-for-media-stream`
        # — из-за него Chrome показывал жёлтую полосу «вы используете
        # неподдерживаемый флаг командной строки». Права микрофона вместо него
        # пишутся в профиль (write_permissions) и выдаются по CDP.
        # `--use-fake-device-for-media-stream` не нужен тем более: он подменяет
        # микрофон фейковым сигналом.
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-crash-restore-window",
        "--disable-session-crashed-bubble",
        "--disable-infobars",
        "--noerrdialogs",
        "--disable-component-update",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        # ОДИН `--disable-features`: повторный ключ перезаписал бы список.
        "--disable-features=" + ",".join(
            tuple(DISABLED_FEATURES) + (tuple(FORCE_LIGHT_FEATURES) if light_theme else ())),
        "--window-position=%d,%d" % (int(staging[0]), int(staging[1])),
        "--window-size=%d,%d" % (int(width), int(height)),
        "--log-file=%s" % (Path(profile).parent / "chromium.log"),
        "--enable-logging",
    ]
    # v18: язык браузера и его речевого сервиса (приоритетно русский).
    if lang:
        args.append("--lang=%s" % str(lang))
    if not gpu:
        args.append("--disable-gpu")
        args.append("--disable-gpu-compositing")
        args.append("--disable-software-rasterizer")
    args.extend(extra)
    return args


class NativeBrowser:
    """Owns the browser process, its DevTools port and (via win32_embed) its
    window. Knows nothing about Qt."""

    def __init__(self, url=DEFAULT_URL, profile_dir=None, app_dir=None,
                 report=None, gpu=None, attempt=0, lang="ru-RU",
                 light_theme=True, accept_languages=None):
        self.url = url
        # v18: язык речевого ввода и светлая тема страницы.
        self.lang = str(lang or "ru-RU")
        self.light_theme = bool(light_theme)
        self.accept_languages = accept_languages
        self.app_dir = Path(app_dir) if app_dir else Path.cwd()
        base = Path(profile_dir) if profile_dir else self.app_dir / "browser-profile"
        self.profile_base = Path(base)
        self.report = report or diag.event
        self.port = None
        self.proc = None
        self.exe = None
        self.kind = "unknown"
        # `attempt` is the retry index owned by the caller (NativeHost). A retry
        # MUST get a different profile directory: a Chromium profile is locked by
        # a SingletonLock, and when a stale process still owns it a new launch
        # hands the URL off to that dead process and exits immediately - which is
        # exactly the "chrome hangs / never opens" report.
        self.attempt = max(0, int(attempt or 0))
        self.profile_dir = self.profile_for(self.attempt)
        self.gpu = (os.environ.get("LEGALYZE_DISABLE_GPU") != "1") if gpu is None else gpu

    def profile_for(self, attempt):
        """Profile directory used by retry `attempt` (0 = the main profile)."""
        attempt = max(0, int(attempt or 0))
        if attempt == 0:
            return self.profile_base
        return Path("%s-retry%d" % (self.profile_base, attempt))

    # -------------------------------------------------------------- lifecycle
    def _event(self, name, **data):
        try:
            self.report(name, **data)
        except Exception:
            diag.exception("native_browser.report")

    def launch(self, candidate, width=453, height=735, staging=(0, 0)):
        """Spawn the browser for `candidate`. Each retry gets a fresh profile so
        a stale singleton can never hand the launch off to a dead process."""
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()

        self.exe = candidate.path
        self.kind = candidate.kind
        self.profile_dir = self.profile_for(self.attempt)
        try:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            diag.exception("native_browser.profile_mkdir")
        # NATIVE 67 % zoom, written BEFORE the launch: Chrome starts the page at
        # `physical / 0.667` CSS px and scales it down, exactly like Ctrl+'-'.
        # Without it the chat is laid out wider than the window and its text is
        # pushed outside the right edge (the v8 report).
        try:
            write_zoom(self.profile_dir)
        except Exception:
            diag.exception("native_browser.zoom_prefs")
        # Микрофон: права в самом профиле вместо удалённого флага.
        try:
            write_permissions(self.profile_dir)
        except Exception:
            diag.exception("native_browser.permissions")
        # v18: язык профиля — иначе речевой ввод идёт на языке системы.
        try:
            write_locale(self.profile_dir, self.lang, self.accept_languages)
        except Exception:
            diag.exception("native_browser.locale")

        args = build_args(self.exe, self.port, self.profile_dir, self.url,
                          width, height, self.gpu, staging,
                          lang=self.lang, light_theme=self.light_theme)
        # v19: контракт окружения и проверка профиля ПЕРЕД запуском — иначе
        # «у одного пользователя тёмная страница, у другого нет» остаётся
        # загадкой: состояния профиля и ключей в логе просто не было.
        try:
            env_contract(self.exe, self.kind, self.port, self.profile_dir, args,
                         width=width, height=height, lang=self.lang,
                         light=self.light_theme, url=self.url,
                         attempt=self.attempt, gpu=self.gpu)
        except Exception:
            diag.exception("native_browser.env_contract")
        try:
            diag.browser_binary(self.exe)
        except Exception:
            diag.exception("native_browser.binary_stat")
        try:
            verify_prefs(self.profile_dir, lang=self.lang)
        except Exception:
            diag.exception("native_browser.verify_prefs")
        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 4  # SW_SHOWNOACTIVATE: findable, not stealing focus
        self._event("browser.launch", exe=self.exe, kind=self.kind, port=self.port,
                    profile=str(self.profile_dir), gpu=self.gpu,
                    width=width, height=height, attempt=self.attempt)
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     creationflags=creationflags,
                                     startupinfo=startupinfo)
        self._event("browser.spawned", pid=self.proc.pid, port=self.port)
        return self.proc

    def http_json(self, path, timeout=1.0):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as response:
            return json.load(response)

    def wait_devtools(self, timeout=30.0):
        """Poll /json/version and /json/list. Returns (version, targets)."""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                self._event("browser.exited_early", code=self.proc.returncode)
                return None, None
            try:
                version = self.http_json("/json/version", timeout=1.0)
                targets = self.http_json("/json/list", timeout=1.0)
                self._event("browser.devtools_ready", browser=version.get("Browser"),
                            protocol=version.get("Protocol-Version"),
                            targets=len(targets or []))
                return version, targets
            except Exception as exc:
                last = str(exc)
                time.sleep(0.2)
        self._event("browser.devtools_timeout", last=str(last)[:200], timeout=timeout)
        return None, None

    def probe(self):
        """Single non-blocking DevTools poll: ('waiting'|'ready'|'exited', version, targets)."""
        if self.proc is not None and self.proc.poll() is not None:
            return "exited", None, None
        try:
            version = self.http_json("/json/version", timeout=0.8)
            targets = self.http_json("/json/list", timeout=0.8)
            return "ready", version, targets
        except Exception:
            return "waiting", None, None

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def close(self):
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            diag.exception("native_browser.terminate")
        deadline = time.time() + 5
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.1)
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                diag.exception("native_browser.kill")
        self._event("browser.closed", port=self.port)


# ------------------------------------------------------------ target choice --
def _host_of(url):
    try:
        return (urllib.parse.urlsplit(url or "").hostname or "").lower()
    except Exception:
        return ""


def is_blocked_target(url):
    """Consent / sign-in / error surfaces: never automate these."""
    raw = (url or "").lower()
    host = _host_of(url)
    return any(token in raw or token == host for token in NON_APP_HOSTS)


def is_app_target(url):
    host = _host_of(url)
    raw = (url or "").lower()
    return (any(token in host for token in ("gemini.google.com", "aistudio.google.com",
                                            "bard.google.com"))
            or "/ai" in raw or "google.com/ai" in raw)


def choose_target(targets):
    """Pick the chat page, never a consent/account wall.

    The old picker took the first `google.com` page — `consent.google.com`
    matched, so automation attached to the wall and waited for a chat that was
    never there ("chrome starts, hangs, does nothing").

    Order: the chat page -> any ordinary page -> a consent/account wall (last
    resort, so the clicker can dismiss it and the redirect can happen) -> a
    blank/error page is never chosen (keeps the caller polling).
    """
    pages = [t for t in (targets or [])
             if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    real = [p for p in pages if (p.get("url") or "").lower().startswith(("http://", "https://"))]
    blocked = [p for p in real if is_blocked_target(p["url"])]
    neutral = [p for p in real if not is_blocked_target(p["url"])]
    app = [p for p in neutral if is_app_target(p["url"])]
    if app:
        chosen = app[0]
    elif neutral:
        chosen = neutral[0]
    elif blocked:
        chosen = blocked[0]
    elif real:
        chosen = real[0]
    else:
        chosen = None
    diag.event("browser.target_chosen",
               pages=[(p.get("url") or "")[:120] for p in pages][:8],
               chosen=(chosen.get("url") if chosen else "")[:120],
               wall_only=bool(blocked and not neutral))
    return chosen


def on_consent_page(url):
    host = _host_of(url)
    return host.endswith("consent.google.com") or "consent.google" in (url or "")


# --------------------------------------------------------------------- zoom --
def zoom_level(factor=ZOOM):
    """Chrome page-zoom level for `factor` (factor = 1.2 ** level)."""
    return math.log(factor) / math.log(1.2)


def _chrome_timestamp():
    """`last_modified` in Chrome's own time base (microseconds since 1601)."""
    import datetime
    epoch = datetime.datetime(1601, 1, 1)
    delta = datetime.datetime.utcnow() - epoch
    return str(int(delta.total_seconds() * 1000000))


def write_zoom(profile_dir, level=None, hosts=ZOOM_HOSTS):
    """Write the NATIVE 67 % page zoom into the profile before the launch.

    This is the same mechanism as pressing Ctrl+'-' down to 67 %: Chrome lays
    the page out at `physical / 0.667` CSS pixels and then scales the whole
    rendering down to fit the window. Nothing is clipped and nothing shifts to
    the right — the page is simply smaller, exactly as in the working builds.

    Written into `<profile>/Default/Preferences` (and `<profile>/Preferences`
    for older layouts), merged with whatever Chrome left there, atomically.
    """
    level = ZOOM_LEVEL if level is None else float(level)
    base = Path(profile_dir)
    written = []
    stamp = _chrome_timestamp()
    for sub in ("Default", ""):
        target_dir = base / sub if sub else base
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            prefs_file = target_dir / "Preferences"
            prefs = {}
            if prefs_file.is_file():
                try:
                    prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
                except Exception:
                    diag.exception("native_browser.prefs_read")
                    prefs = {}
            if not isinstance(prefs, dict):
                prefs = {}
            partition = prefs.setdefault("partition", {})
            if not isinstance(partition, dict):
                partition = {}
                prefs["partition"] = partition
            partition["default_zoom_level"] = {"x": level}
            per_host = partition.setdefault("per_host_zoom_levels", {})
            if not isinstance(per_host, dict):
                per_host = {}
                partition["per_host_zoom_levels"] = per_host
            bucket = per_host.setdefault("x", {})
            if not isinstance(bucket, dict):
                bucket = {}
                per_host["x"] = bucket
            for host in hosts:
                bucket[host] = {"zoom_level": level, "last_modified": stamp}
            tmp = prefs_file.with_name(prefs_file.name + ".tmp")
            tmp.write_text(json.dumps(prefs, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(prefs_file)
            written.append(str(prefs_file))
        except Exception:
            diag.exception("native_browser.write_zoom")
    diag.event("browser.zoom_prefs", level=round(level, 6), files=written)
    return bool(written)


def write_locale(profile_dir, lang="ru-RU", accept=None):
    """Записать язык профиля: Accept-Language и выбранный UI-язык (v18).

    Мало поставить `--lang`: Chrome берёт список языков из профиля, и именно
    он уходит в заголовке Accept-Language. Сервис ИИ по нему выбирает язык
    интерфейса и — что важнее — язык ШТАТНОГО распознавания речи.
    """
    accepted = accept or "%s,%s;q=0.9,en-US;q=0.8,en;q=0.7" % (lang, lang.split("-")[0])
    base = Path(profile_dir)
    written = []
    for sub in ("Default", ""):
        target_dir = base / sub if sub else base
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            prefs_file = target_dir / "Preferences"
            prefs = {}
            if prefs_file.is_file():
                try:
                    prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
                except Exception:
                    diag.exception("native_browser.prefs_read_locale")
                    prefs = {}
            if not isinstance(prefs, dict):
                prefs = {}
            intl = prefs.setdefault("intl", {})
            if not isinstance(intl, dict):
                intl = {}
                prefs["intl"] = intl
            intl["accept_languages"] = accepted
            intl["selected_languages"] = accepted
            # Язык интерфейса браузера: без него на англоязычной Windows
            # Chrome остаётся английским даже с --lang=ru-RU.
            try:
                settings = prefs.setdefault("settings", {})
                if isinstance(settings, dict):
                    settings.setdefault("languages", {})
            except Exception:
                diag.exception("native_browser.prefs_languages")
            tmp = prefs_file.with_name(prefs_file.name + ".tmp")
            tmp.write_text(json.dumps(prefs, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(prefs_file)
            written.append(str(prefs_file))
        except Exception:
            diag.exception("native_browser.write_locale")
    diag.event("browser.locale_prefs", lang=str(lang), accept=accepted,
               files=written)
    return bool(written)


def write_permissions(profile_dir, hosts=ZOOM_HOSTS, level=1):
    """Grant the microphone in the profile itself — no flag, no bubble.

    Chrome shows "You are using an unsupported command-line flag" for
    `--use-fake-ui-for-media-stream`, so the permission is written the way the
    browser writes it itself after the user presses "Allow":
    `profile.content_settings.exceptions.media_stream_mic` with `setting: 1`
    (ALLOW). The file is merged with whatever Chrome left there and replaced
    atomically; it is called on every launch.
    """
    base = Path(profile_dir)
    stamp = _chrome_timestamp()
    written = []
    for sub_dir in ("Default", ""):
        target_dir = base / sub_dir if sub_dir else base
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            prefs_file = target_dir / "Preferences"
            prefs = {}
            if prefs_file.is_file():
                try:
                    prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
                except Exception:
                    diag.exception("native_browser.prefs_read")
                    prefs = {}
            if not isinstance(prefs, dict):
                prefs = {}
            profile = prefs.setdefault("profile", {})
            if not isinstance(profile, dict):
                profile = {}
                prefs["profile"] = profile
            settings = profile.setdefault("content_settings", {})
            if not isinstance(settings, dict):
                settings = {}
                profile["content_settings"] = settings
            exceptions = settings.setdefault("exceptions", {})
            if not isinstance(exceptions, dict):
                exceptions = {}
                settings["exceptions"] = exceptions
            for kind in ("media_stream_mic", "media_stream_camera"):
                bucket = exceptions.setdefault(kind, {})
                if not isinstance(bucket, dict):
                    bucket = {}
                    exceptions[kind] = bucket
                for host in hosts:
                    for pattern in ("https://%s:443,*" % host,
                                    "https://[*.]%s:443,*" % host,
                                    "https://%s,*" % host):
                        bucket[pattern] = {"last_modified": stamp,
                                           "setting": int(level),
                                           "secondary_pattern": "*"}
            tmp = prefs_file.with_name(prefs_file.name + ".tmp")
            tmp.write_text(json.dumps(prefs, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(prefs_file)
            written.append(str(prefs_file))
        except Exception:
            diag.exception("native_browser.write_permissions")
    diag.event("browser.permissions", files=written)
    return bool(written)


def css_size_for(logical_w, logical_h, zoom=ZOOM):
    """CSS size of a page zoomed to `zoom` inside a window of `logical` DIPs.

    `logical` is DPI-independent (Qt works in DIP), so the layout is the SAME
    at 100 %, 125 % and 150 %: 453 DIP / 0.667 = 680 CSS px everywhere.
    """
    return (max(320, int(round(logical_w / zoom))),
            max(320, int(round(logical_h / zoom))))


def refresh_emulation(page, logical_w, logical_h, physical_w=None, physical_h=None,
                      zoom=ZOOM, tries=3, settle=0.1):
    """Объявить переопределение в «форме окна» (логические px, dsf=1.0) и ИЗМЕРИТЬ.

    ФОРМА ВЫВЕДЕНА ИЗ ЖИВОГО ЛОГА (сессия 20261004-162559-12408-c5e41c,
    ключевые точки в `diagnostic.jsonl`):

    * `13:26:13.389 browser.zoom source=native` — ПЕРЕОПРЕДЕЛЕНИЯ НЕТ, раскладка
      верная: `innerWidth=658 dpr=0.6667` (работает один зум профиля 66.7 %);
    * `13:26:21.812` и `21.882` — «толчок» (`apply_viewport_raw`) объявил
      `658+1` и `658` CSS px при `deviceScaleFactor=0.6672`, то есть ВКЛЮЧИЛ
      переопределение с масштабом — и множители СЛОЖИЛИСЬ с зумом профиля:
      `658/0.667 = 987`, `0.6672 x 0.667 = 0.445`. Именно это и видел
      пользователь: `13:26:22.03 browser.viewport ... dsf=0.6672`;
    * `13:26:22.284` и `22.536` — переборы той же формы (`fit=true`, `dsf=1.0`)
      раскладку не вернули;
    * `13:26:23.303 browser.zoom source=emulation-adaptive ok=true` — победила
      форма **439 CSS px при dsf=1.0**, то есть «эмулируемое устройство = само
      окно»: `439/0.667 = 658`, `1.0 x 0.667 = 0.667` — ровно верная раскладка.

    Отсюда правило: пока зум профиля в силе, переопределение объявляется в
    ЛОГИЧЕСКИХ пикселях окна с `dsf=1.0` — тогда складывать нечего. Зум
    профиля при этом работает сам, а CDP служит только для «толчка»/ремонта.

    Возвращает `{ok, source, innerWidth, dpr}`; никогда не бросает.
    """
    result = {"ok": False, "source": "window-dsf1", "innerWidth": 0, "dpr": None}
    width = max(320, int(round(float(logical_w or 0) or 320)))
    height = max(320, int(round(float(logical_h or 0) or 320)))
    for attempt in range(max(1, int(tries))):
        try:
            if not apply_viewport_raw(page, width, height, 1.0):
                return result
        except Exception:
            diag.exception("native_browser.refresh_emulation")
            return result
        try:
            ok, metrics = zoom_ok(page, logical_w, logical_h, physical_w,
                                  physical_h, zoom)
        except Exception:
            diag.exception("native_browser.refresh_measure")
            ok, metrics = False, {}
        metrics = metrics or {}
        result["innerWidth"] = int(metrics.get("innerWidth") or 0)
        try:
            result["dpr"] = round(float(metrics.get("dpr") or 0.0), 4)
        except (TypeError, ValueError):
            result["dpr"] = None
        result["ok"] = bool(ok)
        if ok or attempt + 1 >= max(1, int(tries)):
            break
        if settle:
            time.sleep(max(0.0, float(settle)))
    return result


def page_metrics(page):
    """/innerWidth, innerHeight, devicePixelRatio, scrollWidth/ of the page."""
    probe = ("JSON.stringify({w: innerWidth, h: innerHeight, dpr: devicePixelRatio,"
             " sw: document.documentElement ? document.documentElement.scrollWidth : 0})")
    try:
        value = page.eval(probe, timeout=3).get("value")
        data = json.loads(value or "{}")
    except Exception:
        diag.exception("native_browser.metrics")
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "innerWidth": int(data.get("w") or 0),
        "innerHeight": int(data.get("h") or 0),
        "dpr": float(data.get("dpr") or 0.0),
        "scrollWidth": int(data.get("sw") or 0),
    }


def zoom_ok(page, logical_w, logical_h, physical_w=None, physical_h=None,
            zoom=ZOOM, tol_w=28, tol_h=48):
    """True when the page is REALLY zoomed to `zoom` and really fits.

    v8 forced the CSS viewport to 680 px but never scaled the rendering down:
    the chat was laid out wider than the window, so its text ended up outside
    the right edge ("весь текст съехал вправо"). Two things are checked here:
    * the layout is `logical / zoom` CSS px — not wider, not narrower;
    * `innerWidth * dpr` equals the window's PHYSICAL width, i.e. the layout is
      mapped onto the window instead of overflowing it.
    """
    css_w, css_h, _dsf = target_css(logical_w, logical_h, physical_w, physical_h,
                                    zoom)
    metrics = page_metrics(page)
    if not metrics or metrics["innerWidth"] <= 0:
        return False, metrics
    if abs(metrics["innerWidth"] - css_w) > tol_w:
        return False, metrics
    if abs(metrics["innerHeight"] - css_h) > tol_h:
        return False, metrics
    if physical_w:
        rendered = metrics["innerWidth"] * metrics["dpr"]
        if abs(rendered - physical_w) > max(6.0, 0.03 * physical_w):
            return False, metrics
    return True, metrics


def settle_layout(page, logical_w, logical_h, physical_w=None, physical_h=None,
                  zoom=ZOOM, budget_ms=1200, step_ms=100, need=2, repair=None,
                  on_event=None):
    """Держать раскладку в цели ЗАМЕРОМ, пока возмущение не кончилось (v20).

    Зачем цикл, а не одна проверка. Механизм поломки доказан по записям сессии
    20261004-162559-12408-c5e41c и оказался НЕ «Chrome передумал», а арифметикой
    самого приложения: пока переопределения не было вовсе (`browser.zoom
    source=native`, 13:26:13.389) раскладку держал один зум профиля (66.7 %) —
    658 x 0.6667. Первое же объявление CDP с `deviceScaleFactor=0.6672`
    («толчок», 13:26:21.885) СЛОЖИЛОСЬ с ним: 658/0.667 = 987 и
    0.6672 x 0.667 = 0.445 — замер шкалы в 21.900 уже показывает `dpr=0.445`,
    хотя за 0.65 с до этого было 0.667. Возврат занимал секунды, потому что
    единственная проверка стояла до снятия шторки, а следующий тик сторожа —
    через 5 с (`browser.zoom_restored reason=timer`, 23.303).

    Поэтому цикл: как только замер разошёлся, вызывается `repair()`, и всё это
    происходит, пока шторка ещё стоит. Лечение — не «ждать таймера», а объявить
    переопределение в ЛОГИЧЕСКИХ пикселях окна при `dsf=1.0` (форма, которой
    закончилось живое лечение: `source=emulation-adaptive`).

    Возвращается словарь `{ok, fixed, good, ms}`; исключения наружу не выходят
    никогда.

    «Чинить» — только по ЗАМЕРУ: сама функция ничего не предполагает о том,
    какой из двух механизмов зума активен (зум профиля или эмуляция CDP):
    измеряет и передаёт решение `repair()`.
    """
    started = time.monotonic()
    deadline = started + max(0.0, float(budget_ms) / 1000.0)
    step = max(0.02, float(step_ms) / 1000.0)
    good = 0
    fixed = 0
    ok = False
    metrics = {}
    try:
        while True:
            try:
                ok, metrics = zoom_ok(page, logical_w, logical_h, physical_w,
                                      physical_h, zoom)
            except Exception:
                diag.exception("native_browser.settle_measure")
                ok, metrics = False, {}
            if ok:
                good += 1
                if good >= max(1, int(need)):
                    break
            else:
                good = 0
                if repair is not None:
                    fixed += 1
                    try:
                        repair()
                    except Exception:
                        diag.exception("native_browser.settle_repair")
            if time.monotonic() >= deadline:
                break
            time.sleep(step)
    except Exception:
        diag.exception("native_browser.settle_layout")
    result = {"ok": bool(ok and good >= max(1, int(need))), "fixed": int(fixed),
              "good": int(good), "ms": round((time.monotonic() - started) * 1000)}
    if metrics:
        result["innerWidth"] = int(metrics.get("innerWidth") or 0)
        result["dpr"] = round(float(metrics.get("dpr") or 0.0), 3)
    if on_event is not None:
        try:
            on_event(result)
        except Exception:
            diag.exception("native_browser.settle_event")
    return result


def target_css(logical_w, logical_h, physical_w=None, physical_h=None,
               zoom=ZOOM):
    """(css_w, css_h, dsf) — раскладка, которая ТОЧНО заполняет окно браузера.

    Отсюда берутся и ширина, и высота раскладки, поэтому поле ввода и поле
    ответа стоят на своих местах:

    * ширина = `логический размер (DIP) / zoom` — 453 / 0.667 = **680 CSS px**
      одинаково при 100 %, 125 % и 150 %;
    * `dsf = физическая_ширина / css_ширина` — поверхность ровно такой же
      ширины, как окно: справа ничего не обрезано и не уехало;
    * высота = `физическая_высота / dsf` — поверхность ровно такой же высоты,
      как клиентская область окна (у окна приложения может быть рамка), поэтому
      нижняя часть страницы — поле ввода — не срезана.
    """
    css_w = max(320, int(round(logical_w / zoom)))
    physical_w = physical_w or logical_w
    physical_h = physical_h or logical_h
    dsf = (float(physical_w) / css_w) if css_w else 1.0
    css_h = max(320, int(round(float(physical_h) / dsf)))
    return css_w, css_h, dsf


def zoom_factor(metrics, logical_w):
    """Эффективный зум: логический размер (DIP) / innerWidth.

    1.0 — зума нет (именно так выглядит «объекты съехали вправо»),
    0.667 — всё верно.
    """
    if not metrics or not metrics.get("innerWidth") or not logical_w:
        return None
    return float(logical_w) / float(metrics["innerWidth"])


def apply_viewport_raw(page, width, height, dsf, fit_window=False):
    """One `Emulation.setDeviceMetricsOverride` with explicit numbers."""
    params = {
        "width": int(width),
        "height": int(height),
        "deviceScaleFactor": float(dsf),
        "mobile": False,
        "fitWindow": bool(fit_window),
    }
    try:
        page.send("Emulation.setDeviceMetricsOverride", params, timeout=5)
    except Exception:
        diag.exception("native_browser.viewport")
        return False
    return True


def apply_viewport(page, logical_w, logical_h, physical_w=None, physical_h=None,
                   zoom=ZOOM, dsf=None, fit_window=False):
    """One `Emulation.setDeviceMetricsOverride` (low level).

    `deviceScaleFactor = physical / css` makes the emulated device exactly as
    many device pixels as the window has, so the page is laid out at
    `logical / zoom` CSS px AND scaled to fit — no clipping, no right shift.
    """
    css_w, css_h, base_dsf = target_css(logical_w, logical_h, physical_w,
                                        physical_h, zoom)
    physical_w = physical_w or logical_w
    physical_h = physical_h or logical_h
    used_dsf = base_dsf if dsf is None else float(dsf)
    if not apply_viewport_raw(page, css_w, css_h, used_dsf, fit_window):
        return False
    diag.event("browser.viewport", css=(css_w, css_h),
               logical=(logical_w, logical_h), physical=(physical_w, physical_h),
               dsf=round(used_dsf, 4), fit=bool(fit_window))
    return True


def apply_zoom(page, logical_w, logical_h, physical_w=None, physical_h=None,
               zoom=ZOOM, settle=0.25):
    """Guarantee the native 67 % zoom, whatever the browser did with the prefs.

    1. The profile is launched with the zoom written into Preferences, so
       normally nothing else is needed — measured first, nothing overridden.
    2. If the measurement shows a different layout (prefs ignored, another
       Chrome, a fresh profile), the zoom is forced over CDP and re-measured:
       first `deviceScaleFactor = physical / css` (self-consistent: the
       emulated device IS the window), then the same with `fitWindow`, then
       plain `fitWindow`. Whatever really works wins: the result is MEASURED,
       never assumed.
    """
    physical_w = physical_w or logical_w
    physical_h = physical_h or logical_h
    css_w, css_h, _dsf = target_css(logical_w, logical_h, physical_w, physical_h,
                                    zoom)

    def done(ok, source, metrics):
        diag.event("browser.zoom", source=source, ok=bool(ok), css=(css_w, css_h),
                   logical=(logical_w, logical_h),
                   physical=(int(physical_w), int(physical_h)),
                   zoom=round(zoom_factor(metrics, logical_w) or 0.0, 4),
                   overflow=max(0, int((metrics or {}).get("scrollWidth") or 0)
                                - int((metrics or {}).get("innerWidth") or 0)),
                   **(metrics or {}))
        return {"ok": bool(ok), "source": source, "metrics": metrics}

    ok, metrics = zoom_ok(page, logical_w, logical_h, physical_w, physical_h, zoom)
    if ok:
        return done(True, "native", metrics)

    attempts = (
        ("emulation-dsf", {}),
        ("emulation-dsf-fit", {"fit_window": True}),
        ("emulation-fit", {"dsf": 1.0, "fit_window": True}),
    )
    for name, kwargs in attempts:
        if not apply_viewport(page, logical_w, logical_h, physical_w, physical_h,
                              zoom, **kwargs):
            continue
        time.sleep(settle)
        ok, metrics = zoom_ok(page, logical_w, logical_h, physical_w, physical_h, zoom)
        if ok:
            return done(True, name, metrics)

    # Измерение вместо предположений: если зум профиля и эмуляция наложились,
    # видимая ширина отличается ровно во столько же раз, во сколько показало
    # измерение, — переопределение уточняется за 1-2 шага. Только CDP.
    ok, metrics = adapt_viewport(page, logical_w, logical_h, physical_w,
                                 physical_h, zoom, settle)
    return done(ok, "emulation-adaptive" if ok else "emulation-failed", metrics)


def adapt_viewport(page, logical_w, logical_h, physical_w, physical_h,
                   zoom=ZOOM, settle=0.25, steps=3):
    """Уточнить переопределение по ИЗМЕРЕНИЮ (только CDP).

    Никаких нажатий клавиш и перезапусков браузера: эта функция принципиально
    не может сломать GUI, в отличие от перезапуска ради `--force-device-scale-
    factor` (именно он ломал сборки, где кнопки переставали нажиматься).
    """
    css_w, css_h, _dsf = target_css(logical_w, logical_h, physical_w, physical_h,
                                    zoom)
    metrics = {}
    width, height = css_w, css_h
    for _step in range(max(1, int(steps))):
        dsf = (float(physical_w) / width) if width else 1.0
        if not apply_viewport_raw(page, width, height, dsf):
            return False, metrics
        time.sleep(settle)
        ok, metrics = zoom_ok(page, logical_w, logical_h, physical_w, physical_h,
                              zoom)
        if ok:
            return True, metrics
        measured = int((metrics or {}).get("innerWidth") or 0)
        if not measured or not css_w:
            break
        scale = float(css_w) / float(measured)
        if abs(scale - 1.0) < 0.02:
            # Ширина верная, но условие не выполнилось — крутить бессмысленно.
            break
        scale = max(0.5, min(2.0, scale))
        width = max(320, int(round(width * scale)))
        height = max(320, int(round(height * scale)))
    return False, metrics


def verify_viewport(page, logical_w, logical_h, zoom=ZOOM):
    """Только ширина раскладки (БЕЗ проверки вписывания — см. zoom_ok).

    Оставлена как раз потому, что одна эта проверка и пропустила дефект v8:
    при CSS 680 px и dpr 1 она отвечает «ок», хотя страница шире окна и её
    текст уехал за правую границу. Настоящая проверка — `zoom_ok`.
    """
    css_w, css_h = css_size_for(logical_w, logical_h, zoom)
    metrics = page_metrics(page)
    if not metrics:
        return False
    return abs(metrics["innerWidth"] - css_w) <= 28


#: `Browser.setPermission` отвечает -32602 («неверные параметры») на части
#: сборок Chrome — в логе «Error - 1» это две ошибки подряд. Повторять такой
#: вызов бессмысленно: после первой же неудачи метод больше не трогаем, а
#: право остаётся выданным через `Browser.grantPermissions` и профиль.
_SET_PERMISSION_UNSUPPORTED = False


def env_contract(exe, kind, port, profile, args, width=0, height=0, lang="ru-RU",
                 light=True, url="", attempt=0, gpu=True):
    """Контракт окружения: чем ЭТА машина отличается от любой другой (v19).

    «Один и тот же exe ведёт себя по-разному на двух Windows 10» — ровно тот
    случай, когда нужно сравнить не код, а окружение. В лог пишется ВСЁ, что
    влияет на отрисовку и на страницу: путь и размер браузера, список ключей
    запуска, зум, язык, тема, профиль, размер окна. Две строки `env.contract`
    от разных пользователей сравниваются построчно: разошлось — значит причина
    в окружении, а не в коде.
    """
    try:
        stat = Path(exe).stat()
        exe_bytes, exe_mtime = int(stat.st_size), round(float(stat.st_mtime), 1)
    except Exception:
        exe_bytes, exe_mtime = None, None
    switches = [str(a) for a in (args or ()) if str(a).startswith("-")]
    diag.event("env.contract",
               release="universal-embedded-v19",
               exe=str(exe), exe_bytes=exe_bytes, exe_mtime=exe_mtime,
               kind=str(kind), port=int(port or 0), profile=str(profile),
               url=str(url), attempt=int(attempt), gpu=bool(gpu),
               width=int(width), height=int(height), lang=str(lang),
               light_theme=bool(light), zoom=round(float(ZOOM), 6),
               zoom_level=round(float(ZOOM_LEVEL), 9),
               switches=switches, switches_count=len(switches),
               disabled_features=list(DISABLED_FEATURES),
               force_light_features=list(FORCE_LIGHT_FEATURES) if light else [])
    return switches


def verify_prefs(profile_dir, lang="ru-RU"):
    """Прочитать профиль обратно: записанное — не значит применённое (v19).

    Chrome перезаписывает `Preferences` при выходе, поэтому перед запуском
    проверяем, что зум, язык и право микрофона в файле ЕСТЬ и ровно такие,
    какие нужны. Расхождение пишется в лог как `browser.prefs_drift` — это и
    есть «предсказуемость по-докеровски»: состояние приводится к эталону, а не
    предполагается.
    """
    base = Path(profile_dir)
    report = {}
    for sub_dir in ("Default", ""):
        target = (base / sub_dir if sub_dir else base) / "Preferences"
        if not target.is_file():
            continue
        try:
            prefs = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            diag.exception("native_browser.prefs_verify_read")
            continue
        if not isinstance(prefs, dict):
            continue
        partition = prefs.get("partition") or {}
        zoom = (partition.get("default_zoom_level") or {}).get("x") \
            if isinstance(partition, dict) else None
        intl = prefs.get("intl") or {}
        accept = intl.get("accept_languages") if isinstance(intl, dict) else None
        settings = ((prefs.get("profile") or {}).get("content_settings") or {})
        exceptions = settings.get("exceptions") or {} if isinstance(settings, dict) else {}
        mic = (exceptions.get("media_stream_mic") or {}) if isinstance(exceptions, dict) else {}
        report[str(target)] = {
            "zoom_level": round(float(zoom), 6) if isinstance(zoom, (int, float)) else None,
            "accept_languages": str(accept) if accept else None,
            "mic_hosts": len(mic) if isinstance(mic, dict) else 0,
        }
    if not report:
        diag.event("browser.prefs_missing", profile=str(base))
        return report
    for target, values in report.items():
        drift = []
        if values["zoom_level"] is None \
                or abs(float(values["zoom_level"]) - ZOOM_LEVEL) > 1e-6:
            drift.append("zoom")
        if not values["accept_languages"] or not str(lang).split("-")[0] in \
                str(values["accept_languages"]):
            drift.append("locale")
        if not values["mic_hosts"]:
            drift.append("mic")
        event = "browser.prefs_drift" if drift else "browser.prefs_verified"
        diag.event(event, file=target, drift=drift, **values)
    return report


def grant_microphone(browser, page):
    """Pre-grant the microphone so the page's own recorder never hits a prompt.

    `Browser.grantPermissions` is a real Chrome feature (it failed with -32000
    in QtWebEngine, which is one more reason the embedded engine cannot do the
    page's native voice input).
    """
    origins = set()
    try:
        origin = page.eval("location.origin", timeout=5).get("value")
        if origin and origin != "null":
            origins.add(origin)
    except Exception:
        diag.exception("native_browser.origin")
    origins.update(["https://google.com", "https://www.google.com",
                    "https://gemini.google.com", "https://aistudio.google.com"])
    context_id = None
    try:
        info = browser.send("Target.getBrowserContexts", timeout=5)
        ids = info.get("browserContextIds") or []
        if ids:
            context_id = ids[0]
    except Exception:
        diag.exception("native_browser.contexts")
    granted = []
    for origin in origins:
        params = {"origin": origin, "permissions": ["audioCapture", "videoCapture"]}
        if context_id:
            params["browserContextId"] = context_id
        try:
            browser.send("Browser.grantPermissions", params, timeout=5)
            granted.append(origin)
        except Exception:
            diag.exception("native_browser.grant")
    # Belt and braces: the modern, origin-scoped method. On some builds it
    # answers -32602 (invalid params) — then it is simply not available here.
    global _SET_PERMISSION_UNSUPPORTED
    if not _SET_PERMISSION_UNSUPPORTED:
        try:
            browser.send("Browser.setPermission", {
                "origin": (sorted(origins)[0] if origins else "https://google.com"),
                "permission": {"name": "audioCapture"},
                "setting": "granted",
            }, timeout=5)
        except Exception:
            _SET_PERMISSION_UNSUPPORTED = True
            diag.event("browser.set_permission_unsupported")
            diag.exception("native_browser.set_permission")
    # Проверка, а не предположение: состояние права видно в логе. Если оно не
    # `granted`, микрофон страницы не запустится — причина будет видна сразу.
    # v19: `awaitPromise` — раньше запрос возвращал сам Promise, в логе было
    # `state: {}` и право ВСЕГДА казалось невыданным (лог «Error - 1»).
    state = None
    try:
        state = page.eval(
            "navigator.permissions.query({name:'microphone'})"
            ".then(function(r){return r.state}).catch(function(){return 'unknown'})",
            await_promise=True, timeout=5).get("value")
    except Exception:
        diag.exception("native_browser.permission_state")
    if state not in ("granted", '"granted"') and not _SET_PERMISSION_UNSUPPORTED:
        try:
            real_origin = page.eval("location.origin", timeout=5).get("value")
            if real_origin and real_origin != "null":
                browser.send("Browser.setPermission", {
                    "origin": real_origin,
                    "permission": {"name": "audioCapture"},
                    "setting": "granted",
                }, timeout=5)
        except Exception:
            _SET_PERMISSION_UNSUPPORTED = True
            diag.event("browser.set_permission_unsupported")
            diag.exception("native_browser.set_permission_retry")
    diag.event("browser.mic_granted", origins=granted, context=bool(context_id),
               state=state, set_permission_used=not _SET_PERMISSION_UNSUPPORTED)
    return granted


def speech_surface(page):
    """Report whether this build can actually do the page's voice input."""
    probe = ("JSON.stringify({sr: !!(window.SpeechRecognition||window.webkitSpeechRecognition),"
             " gum: !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia),"
             " perm: !!(navigator.permissions && navigator.permissions.query),"
             " lang: navigator.language, langs: (navigator.languages||[]).join(',')})")
    try:
        value = page.eval(probe, timeout=5).get("value")
        data = json.loads(value or "{}")
    except Exception:
        diag.exception("native_browser.speech_surface")
        data = {}
    diag.event("browser.speech_surface", **data)
    # Язык страницы: по нему сервис выбирает язык распознавания. Если он не
    # русский — в логе это видно сразу, а не по жалобе «диктует по-английски».
    lang = str(data.get("lang") or "")
    if lang and not lang.lower().startswith("ru"):
        diag.warn("speech_language_not_russian", lang=lang,
                  langs=str(data.get("langs") or "")[:80])
    return data


def copy_profile_defaults(profile_dir):
    """Nothing to seed today; keeps the profile path explicit for the caller."""
    return Path(profile_dir)
