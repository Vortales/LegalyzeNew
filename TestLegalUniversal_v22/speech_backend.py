"""Offline dictation for the in-page speech shim via Windows System.Speech.

The embedded QtWebEngine has no Web Speech API backend (recognition in real
Chrome is a Google service tied to Chrome's proprietary keys). The site hides
its microphone button when `SpeechRecognition` is absent. `web_compat.py`
installs a faithful SpeechRecognition shim; this module backs it with real
speech-to-text on every Windows 10/11 machine: a hidden PowerShell host runs
the inbox .NET `System.Speech` dictation engine and streams JSON events.

No third-party Python packages and no cloud calls: PowerShell 5.1 and
System.Speech ship with Windows. Audio stays on the machine. Recognition
quality/availability depends on the installed Windows speech language
(Settings > Time & language > Speech).

v7 — why the host used to die 0.3 s after start (real log, build 19045,
`universal-embedded-v6`, pid 17540):

    speech.started   lang=ru-RU
    speech.host_exit code=2 stderr=""      <-- 310 ms later, no events

Exit code 2 is emitted by the script itself:
``New-Object System.Speech.Recognition.SpeechRecognitionEngine($cult)``
threw, i.e. **no recognizer is registered for that culture on that machine**
("No recognizer of the required ID found"), or `System.Speech` never entered
the load context so the type simply does not exist. The old script then exited
*before* streaming a single event, and Python's read loop saw EOF and
reported `no-speech` — which the GUI printed as "Речь не распознана" while
`mic_active` stayed True, so the next mic press ran the "stop and send"
branch and submitted an empty prompt. That is exactly the report:
"нажимаю на микрофон и всё, сразу отправка без записи".

v7 fixes, in order of importance:

1. **Recogniser discovery instead of blind construction.** The host asks
   `InstalledRecognizers()` what this Windows actually has, streams the list
   (`recognizers` event -> `speech.recognizers` in the log) and picks
   (a) the exact culture, (b) any recogniser for the same language,
   (c) any installed recogniser at all (`speech.lang_fallback`). A machine
   with only en-US installed therefore keeps recording instead of dying.
2. **Assembly loading with a fallback chain** (`Add-Type`,
   `LoadWithPartialName`, `LoadFrom` next to the CLR) and an explicit
   `service-not-allowed` error when System.Speech really is unavailable.
3. **Synchronous `Recognize()` loop.** The old code relied on .NET event
   handlers (`add_SpeechRecognized`) plus a `Start-Sleep` pump; PowerShell
   only runs those actions when the runspace pumps the event queue, which a
   `-NonInteractive` host never does reliably. `Recognize()` is a blocking
   .NET call: no pump, no `DoEvents`, no Windows.Forms, works in any host.
4. **Every failure now carries the real .NET message** (`msg` field ->
   `speech.error` in the log) instead of a silent exit code.
5. Python never downgrades an explicit host error to `no-speech`, keeps the
   last error for the GUI, and `main.py` resets the mic state on failure so a
   mic press can never send an empty prompt (see `mic.cancelled_no_text`).
"""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import diagnostics as diag

# SpeechRecognition DOMException-compatible error names.
ERROR_NAMES = ("aborted", "audio-capture", "network", "no-speech",
               "not-allowed", "service-not-allowed", "language-not-supported")

# Neutral/short language tags -> specific cultures System.Speech can load.
LANG_ALIASES = {
    "ru": "ru-RU", "en": "en-US", "de": "de-DE", "fr": "fr-FR",
    "es": "es-ES", "uk": "uk-UA", "it": "it-IT", "pt": "pt-PT",
    "tr": "tr-TR", "pl": "pl-PL", "kk": "kk-KZ", "be": "be-BY",
}


def norm_lang(lang):
    """Return a SPECIFIC culture tag (ru-RU): System.Speech rejects "ru"."""
    raw = str(lang or "").strip().replace("_", "-")[:16]
    if not raw:
        return "ru-RU"
    low = raw.lower()
    if low in LANG_ALIASES:
        return LANG_ALIASES[low]
    parts = raw.split("-")
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0].lower() + "-" + parts[1].upper()
    return raw


PS_SCRIPT = r"""
param([string]$Lang = 'ru-RU', [int]$ParentPid = 0)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

function Emit([hashtable]$o) {
  try {
    [Console]::Out.WriteLine((ConvertTo-Json -InputObject $o -Compress -Depth 5))
    [Console]::Out.Flush()
  } catch { }
}

function EmitError([string]$code, [string]$msg) {
  Emit @{ type = 'error'; code = $code; msg = $msg }
}

function Reason([object]$err) {
  $m = ''
  try { $m = [string]$err.Exception.Message } catch { $m = '' }
  if (-not $m) { try { $m = [string]$err } catch { $m = '' } }
  if (-not $m) { $m = 'unknown' }
  return (($m -replace '\s+', ' ').Trim())
}

# ---------------------------------------------------------------- assembly
# System.Speech is NOT auto-loaded in -NonInteractive hosts. Try every
# documented way to bring it into the load context; if none works, say so
# with a real message instead of dying with an empty stderr (v6: code 2).
$engineType = $null
try { $engineType = [System.Speech.Recognition.SpeechRecognitionEngine] } catch { $engineType = $null }
if ($null -eq $engineType) {
  try { Add-Type -AssemblyName System.Speech -ErrorAction Stop } catch { }
  try { $engineType = [System.Speech.Recognition.SpeechRecognitionEngine] } catch { $engineType = $null }
}
if ($null -eq $engineType) {
  try { [void][System.Reflection.Assembly]::LoadWithPartialName('System.Speech') } catch { }
  try { $engineType = [System.Speech.Recognition.SpeechRecognitionEngine] } catch { $engineType = $null }
}
if ($null -eq $engineType) {
  try {
    $rt = [System.Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory()
    [void][System.Reflection.Assembly]::LoadFrom((Join-Path $rt 'System.Speech.dll'))
  } catch { }
  try { $engineType = [System.Speech.Recognition.SpeechRecognitionEngine] } catch { $engineType = $null }
}
if ($null -eq $engineType) {
  EmitError 'service-not-allowed' 'System.Speech is not available in this .NET runtime'
  exit 6
}

# ----------------------------------------------------------------- culture
# The recognizer requires a SPECIFIC culture: a neutral culture (ru, en)
# throws "Culture name ... is not supported".
$want = $Lang
if ((-not $want) -or (-not $want.Trim())) { $want = 'ru-RU' }
$cult = $null
try { $cult = [System.Globalization.CultureInfo]::GetCultureInfo($want) } catch { $cult = $null }
if ($null -eq $cult -or $cult.IsNeutralCulture) {
  try {
    $two = $want.Substring(0, 2).ToLowerInvariant()
    foreach ($c in [System.Globalization.CultureInfo]::GetCultures(
        [System.Globalization.CultureTypes]::SpecificCultures)) {
      if ((-not $c.IsNeutralCulture) -and ($c.TwoLetterISOLanguageName.ToLowerInvariant() -eq $two)) {
        $cult = $c; break
      }
    }
  } catch { }
}
if ($null -eq $cult) { try { $cult = [System.Globalization.CultureInfo]::GetCultureInfo('ru-RU') } catch { } }
if ($null -eq $cult) { try { $cult = [System.Globalization.CultureInfo]::CurrentCulture } catch { } }

# ------------------------------------------------------------- recognizers
# Never construct the engine blindly: "No recognizer of the required ID
# found" is the #1 silent killer on Windows 10 (the desktop recognizer only
# exists for languages whose speech feature is installed).
$all = @()
try { $all = @([System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers()) } catch { $all = @() }
$names = @()
foreach ($r in $all) { try { $names += [string]$r.Culture.Name } catch { } }
Emit @{ type = 'recognizers'; count = $names.Count; langs = $names; want = [string]$want }

if ($names.Count -eq 0) {
  EmitError 'service-not-allowed' 'No Windows speech recognizer installed (Settings > Time & language > Speech)'
  exit 7
}

$ri = $null
foreach ($r in $all) {
  if ([string]$r.Culture.Name -ieq [string]$cult.Name) { $ri = $r; break }
}
if ($null -eq $ri) {
  foreach ($r in $all) {
    if ([string]$r.Culture.TwoLetterISOLanguageName -ieq [string]$cult.TwoLetterISOLanguageName) { $ri = $r; break }
  }
}
$fallback = $false
if ($null -eq $ri) { $ri = $all[0]; $fallback = $true }

# ------------------------------------------------------------------ engine
$rc = $null
try {
  $rc = New-Object System.Speech.Recognition.SpeechRecognitionEngine($ri)
} catch {
  $reason = Reason $_
  try { $rc = New-Object System.Speech.Recognition.SpeechRecognitionEngine } catch { $rc = $null }
  if ($null -eq $rc) {
    EmitError 'language-not-supported' ("SpeechRecognitionEngine cannot be created: " + $reason)
    exit 2
  }
}

try { $rc.SetInputToDefaultAudioDevice() } catch {
  EmitError 'audio-capture' ("No default audio input device: " + (Reason $_)); exit 3
}

try {
  $rc.InitialSilenceTimeout = [TimeSpan]::FromSeconds(15)
  $rc.BabbleTimeout = [TimeSpan]::FromSeconds(20)
  $rc.EndSilenceTimeout = [TimeSpan]::FromMilliseconds(800)
  $rc.EndSilenceTimeoutAmbiguous = [TimeSpan]::FromMilliseconds(1500)
} catch { }

try {
  $grammar = New-Object System.Speech.Recognition.DictationGrammar
  $grammar.Name = 'dictation'
  $rc.LoadGrammar($grammar)
} catch {
  EmitError 'language-not-supported' ("DictationGrammar failed: " + (Reason $_)); exit 2
}

$hello = @{ type = 'started'; lang = [string]$ri.Culture.Name; requested = [string]$want }
if ($fallback) { $hello['fallback'] = $true }
Emit $hello

# -------------------------------------------------------------- recognition
# SYNCHRONOUS loop. .NET event subscriptions only fire when PowerShell
# pumps its event queue, which a -NonInteractive host does not do
# reliably -- that is why v5/v6 streamed zero events even while the host was
# alive. Recognize() is a blocking .NET call: no pump, no WinForms message
# loop, nothing that can be missing from the load context. One call is one
# utterance; the loop makes it continuous. Stopping = killing this process.
$errors = 0
while ($true) {
  # Родитель умер (аварийный выход приложения) -> не держим микрофон.
  if ($ParentPid -gt 0) {
    $alive = $null
    try { $alive = Get-Process -Id $ParentPid -ErrorAction SilentlyContinue } catch { $alive = $null }
    if ($null -eq $alive) { exit 0 }
  }
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  $res = $null
  try {
    $res = $rc.Recognize()
  } catch {
    $errors = $errors + 1
    $reason = Reason $_
    if ($errors -ge 4) {
      EmitError 'audio-capture' ("Recognize() failed: " + $reason)
      exit 8
    }
    try { $rc.SetInputToDefaultAudioDevice() } catch { }
    Start-Sleep -Milliseconds 400
    continue
  }
  $sw.Stop()
  if ($null -eq $res) {
    # Таймаут тишины. Если движок вернулся мгновенно (нет грамматики/
    # неверное состояние) — не крутим процессор в пустую.
    if ($sw.Elapsed.TotalMilliseconds -lt 250) { Start-Sleep -Milliseconds 300 }
    continue
  }
  $errors = 0
  $text = ''
  try { $text = [string]$res.Text } catch { $text = '' }
  $conf = 0.0
  try { $conf = [double]$res.Confidence } catch { $conf = 0.0 }
  if ($text.Trim().Length -gt 0) {
    $payload = @{ type = 'result'; text = $text; confidence = $conf; final = $true;
                  lang = [string]$ri.Culture.Name }
    if ($fallback) { $payload['fallback'] = $true }
    Emit $payload
  } else {
    Emit @{ type = 'rejected'; text = '' }
  }
}
"""

# One dictation session per process. stop()/abort() terminate the host; the
# results already streamed to the page are kept by the JS shim.


class DictationEngine:
    """Spawns the PowerShell host and translates its events for the bridge.

    Each start() opens a new "generation"; events from older hosts are dropped
    so a fast stop/start can never interleave two sessions. A start() while a
    host for the SAME language is already running is a no-op that only
    re-announces "started" (the page freely restarts its recognizers; the
    engine must keep the one live host).
    """

    def __init__(self, on_event):
        self.on_event = on_event  # callable(type: str, data: dict)
        self._proc = None
        self._gen = 0
        self._got_result = False
        self._got_error = False
        self._ended = True
        self._lock = threading.Lock()
        self._lang = None
        self._sid = ""
        self._speech_seen = False
        self.last_error = None   # {"code": str, "msg": str} for the GUI
        self.last_notice = None  # ("fallback", lang) when the host had to
                                 # use a non-requested recognizer

    # ----------------------------------------------------------------- state

    @property
    def active(self):
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    @property
    def speech_seen(self):
        """True once the current host produced any speech activity."""
        with self._lock:
            return self._speech_seen

    @property
    def lang(self):
        with self._lock:
            return self._lang

    # --------------------------------------------------------------- control

    def start(self, lang="ru-RU", sid=""):
        norm = norm_lang(lang)
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            same = bool(running and self._lang == norm)
            if same:
                self._sid = sid or self._sid
                gen = self._gen
        if same:
            # Идемпотентный старт: повторный start (гонки страницы, эхо
            # хоткея, ретраи сайта) не убивает живый хост распознавания.
            diag.event("speech.start_ignored", lang=norm)
            self._event(gen, "started", {})
            return
        self.stop()
        if sys.platform != "win32":
            self._event(self._gen, "error", {"code": "service-not-allowed",
                                             "msg": "Windows only"})
            self._event(self._gen, "end", {})
            return
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._got_result = False
            self._got_error = False
            self._ended = False
            self._lang = norm
            self._sid = str(sid or "")
            self._speech_seen = False
            self.last_error = None
            self.last_notice = None
        script = self._write_script()
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA",
                 "-ExecutionPolicy", "Bypass", "-File", script,
                 "-ParentPid", str(os.getpid()), "-Lang", norm],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, creationflags=creationflags,
            )
        except Exception:
            diag.exception("speech.spawn")
            with self._lock:
                self._ended = True
            self._event(gen, "error", {"code": "service-not-allowed",
                                       "msg": "powershell.exe not found"})
            self._event(gen, "end", {})
            return
        with self._lock:
            self._proc = proc
        threading.Thread(target=self._read_loop, args=(gen, proc),
                         name="SpeechPS%d" % gen, daemon=True).start()
        threading.Thread(target=self._watch_exit, args=(gen, proc),
                         name="SpeechPSX%d" % gen, daemon=True).start()
        diag.event("speech.started", lang=norm, session=str(sid)[:16])

    def stop(self, sid=None):
        """Stop the host. `sid` from a stale page session is ignored, so a
        superseded recognizer can never kill the live one. No sid (or an empty
        one) forces the stop."""
        with self._lock:
            current = self._sid
        if sid and current and str(sid) != current:
            diag.event("speech.stop_ignored", sid=str(sid)[:24], current=str(current)[:24])
            return
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                diag.exception("speech.terminate")
        self._finish(self._gen)

    def shutdown(self):
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                diag.exception("speech.terminate")
        with self._lock:
            self._ended = True

    # ---------------------------------------------------------------- intake

    def _read_loop(self, gen, proc):
        got = False
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    diag.event("speech.bad_line")
                    continue
                if self._handle(gen, msg) == "result":
                    got = True
        except Exception:
            diag.exception("speech.read")
        finally:
            with self._lock:
                if gen == self._gen:
                    self._got_result = self._got_result or got
            # A host that already reported its own failure must not be
            # downgraded to "no-speech": the GUI needs the real reason.
            with self._lock:
                explicit = self._got_error
            self._finish(gen, force_error=not got and not explicit)

    def _watch_exit(self, gen, proc):
        """Report host deaths that used to vanish silently (stderr=DEVNULL)."""
        try:
            err = b""
            try:
                if proc.stderr is not None:
                    err = proc.stderr.read()[-800:]
            except Exception:
                pass
            code = proc.wait()
            tail = err.decode("utf-8", "replace").strip().replace("\r", " ")
            tail = " ".join(tail.split())[:200]
            with self._lock:
                stale = gen != self._gen
            if not stale:
                diag.event("speech.host_exit", code=code, stderr=tail)
        except Exception:
            diag.exception("speech.host_watch")

    def _handle(self, gen, msg) -> str:
        kind = str(msg.get("type") or "")
        if kind in ("speechstart", "hypothesis", "result", "rejected"):
            with self._lock:
                self._speech_seen = True
        if kind == "started":
            data = {}
            if msg.get("lang"):
                data["lang"] = str(msg.get("lang"))
            if msg.get("fallback"):
                diag.event("speech.lang_fallback", lang=str(msg.get("lang")),
                           requested=str(msg.get("requested")))
                data["fallback"] = True
                with self._lock:
                    self.last_notice = ("fallback", str(msg.get("lang")))
            self._event(gen, "started", data)
        elif kind == "recognizers":
            langs = msg.get("langs") or []
            if not isinstance(langs, (list, tuple)):
                langs = [langs]
            diag.event("speech.recognizers", count=int(msg.get("count") or len(langs)),
                       langs=",".join(str(x) for x in langs)[:200],
                       want=str(msg.get("want") or "")[:16])
        elif kind == "hypothesis":
            self._event(gen, "hypothesis", {"text": str(msg.get("text") or "")})
        elif kind == "result":
            self._event(gen, "result", {"text": str(msg.get("text") or ""),
                                        "confidence": float(msg.get("confidence") or 0.0)})
        elif kind == "rejected":
            self._event(gen, "rejected", {"text": str(msg.get("text") or "")})
        elif kind in ("speechstart", "speechend"):
            self._event(gen, kind, {})
        elif kind == "error":
            code = str(msg.get("code") or "network")
            if code not in ERROR_NAMES:
                code = "network"
            info = {"code": code, "msg": str(msg.get("msg") or "")[:200]}
            with self._lock:
                self._got_error = True
                self.last_error = info
            diag.event("speech.error", code=code, msg=info["msg"])
            self._event(gen, "error", info)
        elif kind == "idle":
            pass  # silence timeout inside the host: not an event for the page
        else:
            diag.event("speech.unknown_event", kind=kind[:32])
        return kind

    def _finish(self, gen, force_error=False):
        with self._lock:
            if gen != self._gen or self._ended:
                return
            self._ended = True
            got = self._got_result
        if force_error and not got:
            self._event(gen, "error", {"code": "no-speech",
                                       "msg": "host exited without a result"})
        self._event(gen, "end", {})

    def _event(self, gen, kind, data):
        with self._lock:
            stale = gen != self._gen
        if stale:
            return
        try:
            self.on_event(kind, data)
        except Exception:
            diag.exception("speech.event")

    # ---------------------------------------------------------------- script

    def _write_script(self) -> str:
        base = Path(os.environ.get("APPDATA") or Path.home()) / "Legalyze" / "runtime"
        payload = b"\xef\xbb\xbf" + PS_SCRIPT.encode("utf-8")
        try:
            base.mkdir(parents=True, exist_ok=True)
            path = base / "dictation.ps1"
            path.write_bytes(payload)
            return str(path)
        except Exception:
            diag.exception("speech.script_write")
            import tempfile
            path = Path(tempfile.gettempdir()) / "legalyze-dictation.ps1"
            path.write_bytes(payload)
            return str(path)
