#!/usr/bin/env python3
"""machine_profile.py — паспорт машины: чем компьютер пользователя отличается.

Зачем
-----
Приложение обязано работать «у всех и без администратора» (ОШИБКИ.md № 2).
Когда у одного пользователя всё хорошо, а у другого — нет, сравнивать нужно не
код, а МАШИНУ. Паспорт отвечает на вопросы, которые иначе выясняют перепиской
по кругу: какая Windows и сборка, сколько мониторов и какие у них масштабы,
это RDP или обычный вход, тёмная ли тема, хватает ли экрана, есть ли права
администратора, какая версия Chrome и где лежит профиль.

Инварианты (проверяются тестами `tests/test_machine_profile.py`)
--------------------------------------------------------------
* **никогда не бросает**: любой источник может отсутствовать — в паспорте
  останется `None`, но сбор продолжится;
* **никаких секретов**: пути обрезаются до `%USERPROFILE%` / `%APPDATA%` /
  `%TEMP%`, из окружения берётся БЕЛЫЙ список ключей, содержимое профилей и
  пользовательские файлы не читаются;
* **только чтение**: ни одного изменения в системе; права администратора не
  запрашиваются (проверка — чтением, без UAC);
* **только стандартная библиотека**: собирается и на Linux (в тестах), и на
  Windows у пользователя.

Как читается
------------
`profile()` → словарь (схема ниже), `risks()` → список «что в этой машине
рискованно и что с этим делать», `format_report()` → текстовый блок для
`UNDERSTANDING.md` и разбора. Коды рисков машиночитаемы и совпадают с
подсказками в `tools/analyze_browser_logs.py`.
"""
from __future__ import annotations

import ctypes
import os
import platform
import sys
from pathlib import Path

#: Версия схемы паспорта. Меняется, когда меняется состав полей: разбор обязан
#: понимать старые логи, поэтому схема указывается в самом паспорте.
SCHEMA = 1

WIN = sys.platform.startswith("win")

#: Переменные окружения, которые РАЗРЕШЕНО писать в паспорт: только те, что
#: влияют на отрисовку и запуск браузера. Белый список, а не чёрный: в
#: окружении бывают токены и пароли, их в пересылаемом логе быть не должно.
ENV_WHITELIST = (
    "LANG", "LC_ALL", "LC_CTYPE", "LANGUAGE",
    "PYTHONUTF8", "PYTHONIOENCODING", "PYTHONHASHSEED",
    "QT_SCALE_FACTOR", "QT_SCREEN_SCALE_FACTORS", "QT_AUTO_SCREEN_SCALE_FACTOR",
    "QT_ENABLE_HIGHDPI_SCALING", "QT_SCALE_FACTOR_ROUNDING_POLICY",
    "QT_QPA_PLATFORM", "QT_OPENGL", "QTWEBENGINE_CHROMIUM_FLAGS",
    "LEGALYZE_*",  # префиксные ключи приложения
)

#: Что считается проблемным само по себе (и почему) — правила `risks()`.
#: Код → (уровень, текст, что делать). Уровни: `warn` — вероятная причина
#: дефекта, `info` — важный факт для сравнения машин.
RISK_RULES = {
    "dpi_125": ("warn", "масштаб экрана 125 %",
                "окно и inset считаются в DIP: проверить `_monitor_scale()` и "
                "`BROWSER_DX/DY`, смотреть причины `dpi_scale` в шкалах"),
    "dpi_150": ("warn", "масштаб экрана 150 %",
                "как при 125 %: пересчёт DIP↔px обязателен, иначе окно "
                "браузера уезжает на десятки пикселей"),
    "dpi_other": ("warn", "нестандартный масштаб экрана",
                  "пересчёт DIP↔px проверять по замеру, а не по предположению"),
    "multi_monitor": ("warn", "несколько мониторов",
                      "окно может уехать на другой экран или получить другой "
                      "масштаб: смотреть `parent_moved` и смену `screen`"),
    "mixed_scale": ("warn", "у мониторов РАЗНЫЙ масштаб",
                    "при переносе окна меняется DPI: ждать `dpi_scale` и "
                    "`browser.layout_watch`"),
    "rdp_session": ("warn", "сеанс удалённого рабочего стола (RDP)",
                    "в RDP нет GPU-ускорения, DPI может меняться на ходу: "
                    "смотреть `browser.retry` и `layout_restore`"),
    "small_screen": ("warn", "маленький экран",
                     "целевое окно 453×735 плюс панели может не помещаться: "
                     "проверить `parent_clipped` и `size_clamped`"),
    "dark_theme": ("warn", "в Windows включена тёмная тема",
                   "страница может прийти тёмной: смотреть `page.probe.dark` "
                   "и `page.theme_probe`"),
    "old_windows": ("warn", "старая сборка Windows",
                    "проверять наличие `GetDpiForMonitor`, `libEGL` и "
                    "WebView2-зависимостей по `browser.discovered`"),
    "unsupported_windows": ("warn", "Windows старше 10",
                            "нативный Chrome может не запускаться: смотреть "
                            "`browser.no_candidate` и режим QtWebEngine"),
    "no_gpu_hint": ("info", "признаков GPU-ускорения нет",
                    "проверить `browser.try ... gpu` и `webengine.compat_installed`"),
    "running_as_admin": ("info", "приложение запущено с правами администратора",
                         "это НЕ требование: приложение обязано работать без "
                         "прав (проверить на обычном пользователе)"),
    "screens_unknown": ("info", "геометрию мониторов прочитать не удалось",
                        "прислать `qt.screen` из `diagnostic.jsonl` и "
                        "разрешение вручную"),
    "autohide_taskbar": ("info", "панель задач скрыта автоматически",
                         "влияет на рабочую область и на `SetParent`: "
                         "сверить `work` и `parent_clipped`"),
}

#: Минимальный экран, на котором целевое окно 453×735 помещается с панелями.
MIN_SCREEN = (1280, 800)

#: Сборки Windows, начиная с которых проверены DPI-API (10 2004 = 19041).
MIN_BUILD = 19041


# --------------------------------------------------------------------------
# Санитизация
# --------------------------------------------------------------------------
def redact(value):
    """Путь → безопасный для пересылки: домашние каталоги → переменные.

    В логе не должно быть имени пользователя: `C:\\Users\\Мария\\AppData\\...`
    превращается в `%APPDATA%\\...`. Содержимое файлов не читается вовсе.
    """
    text = str(value or "")
    if not text:
        return text
    pairs = []
    for key in ("APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "USERPROFILE", "HOMEPATH"):
        base = os.environ.get(key)
        if base:
            pairs.append((str(base), "%%%s%%" % key))
    try:
        pairs.append((str(Path.home()), "%USERPROFILE%"))
    except Exception:
        pass
    # Длинные префиксы первыми: иначе %APPDATA% съест %USERPROFILE%.
    for prefix, marker in sorted(pairs, key=lambda kv: -len(kv[0])):
        if prefix and text.lower().startswith(prefix.lower()):
            return marker + text[len(prefix):]
    return text


def env_info(environ=None):
    """Окружение по БЕЛОМУ списку (секреты в паспорт не попадают)."""
    source = dict(os.environ if environ is None else environ)
    out = {}
    for key, value in source.items():
        name = str(key)
        upper = name.upper()
        if name in ENV_WHITELIST:
            out[name] = str(value)[:120]
            continue
        # Ключи приложения — тоже, но НИКОГДА не похожие на секреты: в этом
        # окружении живёт токен доступа, и он не должен уехать в пересылаемый лог.
        if upper.startswith("LEGALYZE_") and not any(
                word in upper for word in ("TOKEN", "SECRET", "PASS", "KEY", "AUTH")):
            out[name] = str(value)[:120]
    return out


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------
def _registry(path, name):
    """Значение из реестра Windows. Ничего не пишет, при любой ошибке — None."""
    if not WIN:
        return None
    try:
        import winreg  # noqa: PLC0415 - только на Windows
        root, _, rest = path.partition("\\")
        roots = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}
        with winreg.OpenKey(roots.get(root, winreg.HKEY_LOCAL_MACHINE), rest) as key:
            value = winreg.QueryValueEx(key, name)[0]
        return value
    except Exception:
        return None


def windows_info():
    """Версия, сборка и редакция Windows (реестр + `platform`, только чтение)."""
    out = {"release": None, "version": None, "build": None, "edition": None,
           "display_version": None, "ubr": None}
    if not WIN:
        # На Linux паспорт Windows не выдумывает: пусто — значит «не Windows».
        return out
    try:
        out["release"] = platform.release()
        out["version"] = platform.version()
    except Exception:
        pass
    try:
        parts = str(out.get("version") or "").split(".")
        if len(parts) >= 3:
            out["build"] = int(parts[2])
    except (TypeError, ValueError):
        pass
    try:
        if WIN:
            version = sys.getwindowsversion()
            out["build"] = int(getattr(version, "build", None) or out["build"] or 0) or out["build"]
            out["service_pack"] = str(getattr(version, "service_pack", "") or "")
    except Exception:
        pass
    current = "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion"
    out["edition"] = _registry(current, "ProductName")
    out["display_version"] = _registry(current, "DisplayVersion")
    out["ubr"] = _registry(current, "UBR")
    return out


def theme_info():
    """Тёмная или светлая тема приложений (реестр; None — не прочитать)."""
    value = _registry("HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Themes"
                      "\\Personalize", "AppsUseLightTheme")
    if value is None:
        return None
    try:
        return "light" if int(value) else "dark"
    except (TypeError, ValueError):
        return None


def session_info():
    """Тип сеанса и права: RDP, администратор, DPI-осведомлённость процесса."""
    out = {"kind": None, "admin": None, "dpi_awareness": None}
    if WIN:
        try:
            out["kind"] = "rdp" if ctypes.windll.user32.GetSystemMetrics(0x1000) else "console"
        except Exception:
            out["kind"] = None
        try:
            out["admin"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            out["admin"] = None
        try:
            # 2 = PROCESS_PER_MONITOR_DPI_AWARE, 1 = system aware, 0 = unaware.
            out["dpi_awareness"] = int(ctypes.windll.shcore.GetProcessDpiAwareness(None))
        except Exception:
            try:
                context = ctypes.windll.user32.GetThreadDpiAwarenessContext()
                out["dpi_awareness"] = int(ctypes.windll.user32.GetAwarenessFromDpiAwarenessContext(context))
            except Exception:
                out["dpi_awareness"] = None
    return out


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT), ("rcWork", _RECT),
                ("dwFlags", ctypes.c_ulong), ("szDevice", ctypes.c_wchar * 32)]


def _rect_pair(rect):
    return [int(rect.left), int(rect.top),
            int(rect.right - rect.left), int(rect.bottom - rect.top)]


def _win_screens():
    """Мониторы через Win32: прямоугольники, рабочая область, DPI каждого."""
    screens = []
    if not WIN:
        return screens
    user32 = ctypes.windll.user32
    collected = []

    def callback(handle, _dc, _rect, _data):
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            return 1
        dpi = None
        try:
            value = ctypes.c_uint()
            if ctypes.windll.shcore.GetDpiForMonitor(handle, 0, ctypes.byref(value),
                                                     ctypes.byref(ctypes.c_uint())) == 0:
                dpi = int(value.value)
        except Exception:
            dpi = None
        collected.append({
            "name": str(info.szDevice or ""),
            "primary": bool(info.dwFlags & 1),
            "rect": _rect_pair(info.rcMonitor),
            "work": _rect_pair(info.rcWork),
            "dpi": dpi,
        })
        return 1

    try:
        proc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.POINTER(_RECT), ctypes.c_double)(callback)
        user32.EnumDisplayMonitors(None, None, proc, 0)
    except Exception:
        return screens
    for index, item in enumerate(collected):
        screens.append(_finish_screen(item, index))
    return screens


def _system_dpi():
    """DPI системы, если по мониторам прочитать не удалось."""
    if not WIN:
        return None
    try:
        return int(ctypes.windll.user32.GetDpiForSystem())
    except Exception:
        pass
    try:
        return int(ctypes.windll.gdi32.GetDeviceCaps(ctypes.windll.user32.GetDC(0), 88))
    except Exception:
        return None


def _finish_screen(item, index):
    """Дополнить монитор масштабом и индексом: `scale = dpi / 96`."""
    screen = dict(item)
    screen["index"] = int(index)
    dpi = screen.get("dpi") or _system_dpi() or 96
    screen["dpi"] = int(dpi)
    screen["scale"] = round(float(dpi) / 96.0, 4)
    return screen


def qt_screens(app=None):
    """Мониторы глазами Qt (то же, что видит само приложение). Нет Qt — [].

    Qt — единственный источник, который знает масштаб РОВНО так, как его
    использует приложение (у Win32 и Qt могут расходиться округления), поэтому
    паспорт берёт Qt, если приложение уже запущено, и Win32 — если нет.
    """
    screens = []
    try:
        from PyQt6.QtGui import QGuiApplication  # noqa: PLC0415
    except Exception:
        return screens
    try:
        application = app or QGuiApplication.instance()
        if application is None:
            return screens
        primary = application.primaryScreen()
        for index, screen in enumerate(application.screens()):
            geometry = screen.geometry()
            available = screen.availableGeometry()
            dpi = None
            for attr in ("logicalDotsPerInch", "physicalDotsPerInch"):
                try:
                    dpi = float(getattr(screen, attr)())
                    break
                except Exception:
                    continue
            dpr = 1.0
            try:
                dpr = round(float(screen.devicePixelRatio()), 4)
            except Exception:
                pass
            width = _finish_screen({
                "name": str(screen.name()),
                "primary": screen is primary,
                "rect": [int(geometry.x()), int(geometry.y()),
                         int(geometry.width()), int(geometry.height())],
                "work": [int(available.x()), int(available.y()),
                         int(available.width()), int(available.height())],
                "dpi": dpi,
            }, index)
            width["dpr"] = dpr
            screens.append(width)
    except Exception:
        return []
    return screens


def taskbar_info(screens=None):
    """Прокси «панель задач скрыта»: сравнение экрана и рабочей области."""
    screen_list = screens or []
    hints = []
    for screen in screen_list:
        try:
            rect = screen.get("rect") or []
            work = screen.get("work") or []
            if len(rect) == 4 and len(work) == 4:
                hints.append(int(rect[2]) - int(work[2]) <= 1 and
                             int(rect[3]) - int(work[3]) <= 1)
        except Exception:
            continue
    if not hints:
        return {"autohide_proxy": None, "screens_without_reserved_area": 0}
    return {"autohide_proxy": all(hints),
            "screens_without_reserved_area": sum(1 for item in hints if item)}


# --------------------------------------------------------------------------
# Паспорт
# --------------------------------------------------------------------------
def app_info(extra=None):
    """Сведения о приложении: версия, сборка exe, режим, целевое окно."""
    info = {
        "python": platform.python_version(),
        "python_bits": 64 if sys.maxsize > 2 ** 32 else 32,
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": redact(sys.executable),
        "pid": os.getpid(),
    }
    if extra:
        info.update({str(k): v for k, v in extra.items()})
    return info


def profile(extra=None, screens=None, environ=None):
    """Собрать паспорт машины. Никогда не бросает: что не прочиталось — `None`."""
    facts = {"schema": SCHEMA, "collected_at": None}
    try:
        from datetime import datetime, timezone
        facts["collected_at"] = datetime.now(timezone.utc).isoformat()
    except Exception:
        pass
    for key, getter in (("windows", windows_info), ("session", session_info),
                        ("theme", theme_info)):
        try:
            facts[key] = getter()
        except Exception:
            facts[key] = None
    try:
        screen_list = screens if screens else (_win_screens() if WIN else [])
        facts["screens"] = screen_list
        facts["virtual"] = _virtual_rect(screen_list)
        facts["taskbar"] = taskbar_info(screen_list)
    except Exception:
        facts["screens"] = []
        facts["virtual"] = None
        facts["taskbar"] = None
    try:
        facts["app"] = app_info(extra)
    except Exception:
        facts["app"] = {}
    try:
        facts["env"] = env_info(environ)
    except Exception:
        facts["env"] = {}
    try:
        facts["platform"] = {"system": platform.system(), "release": platform.release(),
                             "machine": platform.machine(), "locale": _locale()}
    except Exception:
        facts["platform"] = {}
    # Масштаб главного (или первого) экрана — самое важное число паспорта.
    facts["scale"] = _primary_scale(facts.get("screens") or [])
    return facts


def _locale():
    """Язык системы (для сверки с `lang=ru-RU`, который ставит приложение)."""
    try:
        import locale
        lang = locale.getlocale()[0]
        if lang:
            return str(lang)
    except Exception:
        pass
    return str(os.environ.get("LANG") or os.environ.get("LC_ALL") or "")


def _primary_scale(screens):
    for screen in screens:
        if screen.get("primary"):
            return screen.get("scale")
    if screens:
        return screens[0].get("scale")
    return None


def _virtual_rect(screens):
    """Общая область всех мониторов: [x, y, ширина, высота]."""
    rects = [s.get("rect") for s in (screens or []) if len(s.get("rect") or []) == 4]
    if not rects:
        return None
    left = min(r[0] for r in rects)
    top = min(r[1] for r in rects)
    right = max(r[0] + r[2] for r in rects)
    bottom = max(r[1] + r[3] for r in rects)
    return [int(left), int(top), int(right - left), int(bottom - top)]


# --------------------------------------------------------------------------
# Риски
# --------------------------------------------------------------------------
def _screen_issue(screen):
    """Один экран меньше минимума — это риск для окна 453×735."""
    try:
        width, height = int((screen.get("work") or screen.get("rect"))[2]), \
            int((screen.get("work") or screen.get("rect"))[3])
    except (TypeError, ValueError, IndexError):
        return False
    return width < MIN_SCREEN[0] or height < MIN_SCREEN[1]


def risks(facts):
    """Что в этой машине рискованно и что с этим делать. Список словарей.

    Порядок — от вероятных причин дефектов к справочным фактам, чтобы первая
    строка отчёта была самой полезной. Правила — в `RISK_RULES`.
    """
    facts = facts or {}
    found = []

    def add(code, detail=""):
        rule = RISK_RULES.get(code)
        if not rule:
            return
        level, text, advice = rule
        found.append({"code": code, "level": level, "text": text,
                      "detail": str(detail or ""), "advice": advice})

    screens = facts.get("screens") or []
    scales = sorted({float(s.get("scale") or 1.0) for s in screens})
    scale = facts.get("scale")
    try:
        scale = float(scale) if scale is not None else None
    except (TypeError, ValueError):
        scale = None
    if scale is not None:
        if abs(scale - 1.5) < 0.01:
            add("dpi_150", "%s → %s" % (scale, _scale_px(scale)))
        elif abs(scale - 1.25) < 0.01:
            add("dpi_125", "%s → %s" % (scale, _scale_px(scale)))
        elif abs(scale - 1.0) > 0.01:
            add("dpi_other", "%s → %s" % (scale, _scale_px(scale)))
    if len(screens) > 1:
        add("multi_monitor", "%d экрана(ов): %s" % (
            len(screens), ", ".join("%s×%s@%s" % (s.get("rect", [0, 0, 0, 0])[2],
                                                  s.get("rect", [0, 0, 0, 0])[3],
                                                  s.get("scale")) for s in screens[:4])))
        if len(scales) > 1:
            add("mixed_scale", "масштабы: %s" % ", ".join(str(s) for s in scales))
    if not screens:
        add("screens_unknown")
    if any(_screen_issue(s) for s in screens):
        add("small_screen", "; ".join(
            "%s×%s" % ((s.get("work") or s.get("rect") or [0, 0, 0, 0])[2],
                       (s.get("work") or s.get("rect") or [0, 0, 0, 0])[3])
            for s in screens[:3]))
    session = facts.get("session") or {}
    if session.get("kind") == "rdp":
        add("rdp_session")
    if session.get("admin"):
        add("running_as_admin")
    windows = facts.get("windows") or {}
    build = windows.get("build")
    release = str(windows.get("release") or "")
    if release and release not in ("10", "11"):
        add("unsupported_windows", "release=%s (%s)" % (release, windows.get("version")))
    elif isinstance(build, int) and build and build < MIN_BUILD:
        add("old_windows", "build=%s (эталон %s)" % (build, MIN_BUILD))
    if (facts.get("theme") or "") == "dark":
        add("dark_theme")
    taskbar = facts.get("taskbar") or {}
    if taskbar.get("autohide_proxy"):
        add("autohide_taskbar")
    return found


def _scale_px(scale):
    """Во сколько «физических» пикселей превращается 1 DIP при таком масштабе."""
    try:
        return "%d%%" % round(float(scale) * 100)
    except (TypeError, ValueError):
        return "?"


def risk_codes(facts):
    return [item["code"] for item in risks(facts)]


# --------------------------------------------------------------------------
# Текст
# --------------------------------------------------------------------------
def format_report(facts):
    """Человеческий блок «Машина» для UNDERSTANDING.md и ANALYSIS.md."""
    facts = facts or {}
    lines = []
    windows = facts.get("windows") or {}
    session = facts.get("session") or {}
    app = facts.get("app") or {}
    platform_info = facts.get("platform") or {}
    title = " ".join(str(x) for x in (windows.get("edition"),
                                      windows.get("display_version"),
                                      windows.get("version")) if x)
    lines.append("- Windows: %s (сборка %s%s)"
                 % (title or "не прочитано", windows.get("build"),
                    ", UBR %s" % windows.get("ubr") if windows.get("ubr") else ""))
    lines.append("- Сеанс: %s%s, права администратора: %s, DPI-осведомлённость: %s"
                 % (session.get("kind") or "?",
                    " (удалённый рабочий стол)" if session.get("kind") == "rdp" else "",
                    session.get("admin"), session.get("dpi_awareness")))
    lines.append("- Тема приложений: %s, язык системы: %s"
                 % (facts.get("theme") or "?", platform_info.get("locale") or "?"))
    for screen in facts.get("screens") or []:
        rect = screen.get("rect") or [0, 0, 0, 0]
        work = screen.get("work") or [0, 0, 0, 0]
        lines.append("- Экран %s%s: %s×%s в (%s,%s), рабочая %s×%s, DPI %s, масштаб %s%s"
                     % (screen.get("index"), " (основной)" if screen.get("primary") else "",
                        rect[2], rect[3], rect[0], rect[1], work[2], work[3],
                        screen.get("dpi"), screen.get("scale"),
                        ", Qt dpr %s" % screen.get("dpr") if screen.get("dpr") else ""))
    if facts.get("virtual"):
        lines.append("- Общая область мониторов: %s" % (facts["virtual"],))
    lines.append("- Приложение: Python %s (%s бит)%s, exe: %s"
                 % (app.get("python"), app.get("python_bits"),
                    ", собрано в exe" if app.get("frozen") else " (из исходников)",
                    app.get("executable")))
    if facts.get("env"):
        lines.append("- Переменные режимов: %s"
                     % ", ".join("%s=%s" % kv for kv in sorted(facts["env"].items())))
    return "\n".join(lines)


def format_risks(facts, limit=8):
    """Строки «риск (уровень) — что делать»: сначала вероятные причины."""
    levels = {"warn": 0, "info": 1}
    items = sorted(risks(facts), key=lambda item: levels.get(item["level"], 2))
    lines = []
    for item in items[:limit]:
        lines.append("- **%s** (%s)%s: %s"
                     % (item["text"], item["level"],
                        " — %s" % item["detail"] if item["detail"] else "",
                        item["advice"]))
    return lines


if __name__ == "__main__":  # ручная проверка на живой машине
    import json
    data = profile()
    print(json.dumps(data, ensure_ascii=False, indent=1, default=str))
    print()
    print(format_report(data))
    print()
    print("\n".join(format_risks(data)) or "рисков не найдено")
