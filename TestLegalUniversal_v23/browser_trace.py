"""browser_trace.py — приборная панель браузера: полный и доказуемый сбор поведения.

Зачем этот модуль
-----------------
Приложение должно вести себя у всех пользователей одинаково («предсказуемо,
как в Docker»). Про «одинаково» нельзя спорить словами: нужны измерения.
Модуль отвечает на три вопроса, которые до сих пор оставались без ответа:

1. **Встало ли окно браузера туда, куда его просили?**
   Целевой прямоугольник считается из клиентской области родителя (плейсхолдера)
   и `inset` — «левый/верхний/правый/нижний» в ФИЗИЧЕСКИХ пикселях. Фактический
   берётся у Windows (`GetWindowRect`/`GetClientRect`), а расхождение
   раскладывается на причины с числами (масштаб монитора, оторванный родитель,
   отказ `SetWindowPos` с кодом ошибки, асинхронная задержка Chromium и т.д.).
2. **Почему объекты «съехали и встали на место через 2–3 секунды»?**
   Вокруг каждого возмущения (хоткей «Окно», сворот/разворот, смена DPI) идёт
   высокочастотная временная шкала: позиция окна, клиентский размер, `innerWidth`,
   `devicePixelRatio`, положение поля ввода, скролл. По ней видно не «мнение», а
   точные миллисекунды: когда началось, сколько длилось, чем закончилось.
3. **Файлы действительно экспортировались?**
   Для каждого вложения пишется всё: файл на диске (размер, время, SHA-256),
   результат впрыска в страницу, что реально оказалось в `input[type=file]`,
   сколько чипов прикреплённых файлов в чате и с какими именами, куда встал
   вердикт — «прикреплено», «вложено, но безымянно», «не прикрепилось»,
   «на этой поверхности вложения нет вообще».

Куда пишет
----------
В папку сессии диагностики (её создаёт `diagnostics.py`, обычно
`%APPDATA%\\Legalyze\\logs\\<сессия>`):

    browser-trace.jsonl            все события, JSON-строки
    browser-trace.txt              то же человеческим текстом
    browser-window-timeline.jsonl  временная шкала окна (только изменения)
    browser-window-timeline.txt
    page-outline-<причина>.json    структура страницы: теги/классы/геометрия
    page-code-<причина>.html       «код страницы» без текста и секретов
    page-meta-<причина>.json       скрипты/фреймы/ресурсы, без содержимого
    browser-trace-summary.json     машинный итог сессии
    UNDERSTANDING.md               человеческий итог: что делал браузер и почему

Правила
-------
* Модуль НИКОГДА не ломает приложение: любая ошибка сбора превращается в запись
  в логе, а не в исключение наружу. Импортируется и на Linux (для тестов):
  Win32-вызовы идут только через ctypes и только на Windows.
* Приватность: в лог не попадают текст пользователя, содержимое промтов и PDF,
  токены, пароли, HWID, cookie. Имена файлов, которые создаёт сама программа
  (`<промт>.pdf`, `Шаблон.txt`), пишутся: без них экспорт недоказуем.
* Сбор асинхронный: пишущий поток отдельный, поток GUI никогда не ждёт диск.
* Сбор ограничен по объёму (кольцо + лимиты на файл), чтобы лог не разрастался
  до гигабайтов на долгой сессии.
"""
from __future__ import annotations

import atexit
import ctypes
import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

TRACE_VERSION = "browser-trace-1"

# ------------------------------------------------------------------ limits --
MAX_FIELD = 300            # символов в строковом поле
MAX_QUEUE = 40000          # записей в очереди (защита от переполнения)
MAX_TRACE_BYTES = 16 << 20  # один файл журнала не больше 16 МБ
MAX_TIMELINE_SAMPLES = 4000  # строк в одной временной шкале
PAGE_CODE_MAX_BYTES = 1 << 20  # 1 МБ на «код страницы»
PAGE_CODE_MAX_NODES = 4000
OUTLINE_MAX_NODES = 600

#: Имя папки внутри сессии диагностики (двойная страховка, если LOG_DIR ещё нет).
FALLBACK_DIR = "browser-trace"

EVENT_FILE = "browser-trace.jsonl"
HUMAN_FILE = "browser-trace.txt"
TIMELINE_FILE = "browser-window-timeline.jsonl"
TIMELINE_HUMAN = "browser-window-timeline.txt"
SUMMARY_FILE = "browser-trace-summary.json"
UNDERSTANDING_FILE = "UNDERSTANDING.md"

#: Причины расхождения «просили — получили». Тексты — для человека, код — для
#: анализатора: он ищет эти коды в логе и по ним даёт вердикт.
REASONS = {
    "dead_window": "Окна больше нет (IsWindow=0): процесс браузера умер или HWND устарел.",
    "not_child": "Окно НЕ дочернее у плейсхолдера (GetParent != parent): его кто-то оторвал.",
    "hidden": "Окно скрыто (WS_VISIBLE снят) или свёрнуто: картинки не будет, даже если позиция верна.",
    "detached_style": "У окна снят стиль WS_CHILD — оно живёт отдельным окном.",
    "off_target": "Позиция/размер окна не совпали с целью (см. dx/dy/dw/dh).",
    "dpi_scale": "Расхождение кратно масштабу монитора: координаты заданы в DIP, а окно — в физических px.",
    "parent_moved": "Родитель переехал после расчёта: координаты цели были посчитаны для старой позиции плейсхолдера.",
    "parent_clipped": "Цель выходит за клиентскую область родителя: окно физически не может занять эту позицию.",
    "size_clamped": "Ширина/высота окна не совпали: Chromium или Windows держит минимальный размер.",
    "call_failed": "Последний вызов SetWindowPos вернул 0 — смотрите код ошибки в win32.error.",
    "async_pending": "Вызов отправлен, но окно ещё не переехало (асинхронная очередь Chromium/Windows).",
    "not_requested": "Позицию никто не задавал: вызовы sync ещё не выполнялись.",
    "page_lost": "CDP-соединение со страницей потеряно: метрики страницы недоступны.",
    "ok": "Совпало с целью.",
}

WIN32_ERRORS = {
    0: "нет ошибки",
    5: "ERROR_ACCESS_DENIED (окно другого процесса/очереди ввода)",
    6: "ERROR_INVALID_HANDLE",
    87: "ERROR_INVALID_PARAMETER (неверный прямоугольник)",
    1400: "ERROR_INVALID_WINDOW_HANDLE (HWND устарел или окно уничтожено)",
    1401: "ERROR_INVALID_MENU_HANDLE",
    1402: "ERROR_INVALID_CURSOR_HANDLE",
    1403: "ERROR_INVALID_ACCEL_HANDLE",
    1404: "ERROR_INVALID_HOOK_HANDLE",
    1405: "ERROR_INVALID_DWP_HANDLE",
    1406: "ERROR_INVALID_DWP_HANDLE",
    1407: "ERROR_INVALID_DWP_HANDLE",
    1408: "ERROR_INVALID_WINDOW_HANDLE",
    1409: "ERROR_INVALID_WINDOW_HANDLE",
    1410: "ERROR_CLASS_ALREADY_EXISTS",
    1411: "ERROR_CLASS_DOES_NOT_EXIST",
    1804: "ERROR_INVALID_STARTING_CODESEG",
    0x80070005: "E_ACCESSDENIED (COM)",
}

# ------------------------------------------------------------------- state --
_lock = threading.RLock()
_enabled = True
_dir = None
_queue = None            # type: ignore[var-annotated]
_timeline_queue = None   # type: ignore[var-annotated]
_writer = None
_started = False
_finished = False
_failures = 0
_watchers = set()
_marks = []
_last_win_call = {}
#: Последний аудит экспорта: (подпись, время) — чтобы не писать одно и то же.
_last_audit = {}
_win_calls = 0
_session = {"installed_at": None, "dir": None, "os": sys.platform}
_counters = {}
#: Последний паспорт машины (машина может смениться — например, RDP или
#: подключение второго монитора, поэтому хранится последний, а не первый).
_machine = None
#: Самопроверки: имя → последний вердикт. В сводку идёт последний по времени.
_selfchecks = {}


def install(log_dir=None, enabled=None):
    """Включить сбор. Вызывать ПОСЛЕ `diagnostics.setup()`.

    `log_dir` — папка сессии; по умолчанию берётся из `diagnostics.LOG_DIR`,
    затем `%APPDATA%/Legalyze/logs/browser-trace`. Если ничего не подошло —
    сбор тихо выключается (приложение из-за логов падать не должно).
    """
    global _enabled, _dir, _queue, _timeline_queue, _writer, _started, _finished
    with _lock:
        if _started:
            return True
        if enabled is False:
            _enabled = False
            return False
        target = _resolve_dir(log_dir)
        if target is None:
            _enabled = False
            return False
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            _enabled = False
            return False
        _dir = target
        # Новая сессия — новые счётчики: итог не должен смешивать запуски
        # (важно и для тестов, и для повторного старта внутри одного процесса).
        _counters.clear()
        globals()["_win_calls"] = 0
        _marks.clear()
        _queue = queue.Queue(maxsize=MAX_QUEUE)
        _timeline_queue = queue.Queue(maxsize=MAX_QUEUE)
        _writer = _Writer(target, _queue, _timeline_queue)
        _writer.start()
        _started = True
        _finished = False
        _session["installed_at"] = _now()
        _session["dir"] = str(target)
        atexit.register(finish)
        mark("trace.installed", dir=str(target), version=TRACE_VERSION,
             python=sys.version.split()[0], platform=sys.platform)
        return True


def _resolve_dir(log_dir):
    if log_dir:
        return Path(log_dir)
    try:
        import diagnostics as diag
        if getattr(diag, "LOG_DIR", None):
            return Path(diag.LOG_DIR)
    except Exception:
        pass
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Legalyze" / "logs" / FALLBACK_DIR
    try:
        return Path.home() / ".legalyze" / "logs" / FALLBACK_DIR
    except Exception:
        return None


def enabled():
    return bool(_enabled)


def directory():
    return _dir


def _now():
    return datetime.now(timezone.utc).isoformat()


def _short(value):
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = str(value)
    return text if len(text) <= MAX_FIELD else text[:MAX_FIELD] + "…"


def _clean(value):
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in list(value.items())[:64]}
    if isinstance(value, (list, tuple, set)):
        return [_clean(v) for v in list(value)[:64]]
    if isinstance(value, bytes):
        return "<bytes:%d>" % len(value)
    return str(value)[:MAX_FIELD]


def _put(record, timeline=False):
    """Положить запись в очередь. Никогда не бросает."""
    global _failures
    if not _enabled or not _started:
        return
    target = _timeline_queue if timeline else _queue
    if target is None:
        return
    try:
        target.put_nowait(record)
    except queue.Full:
        with _lock:
            _failures += 1
    except Exception:
        with _lock:
            _failures += 1


def _record(kind, /, **fields):
    """Собрать запись журнала. Никогда не бросает — даже при конфликте имён.

    Служебные ключи (`time`, `t`, `pid`, `thread`, `kind`) заняты самой
    записью. Раньше совпавшее с ними имя поля роняло сбор целиком:
    `_record() got multiple values for keyword argument 'kind'` — именно это
    случилось на живой сессии, когда вложение передало `extra={"kind": "pdf"}`.
    Теперь `kind` — ТОЛЬКО позиционный параметр (слэш в сигнатуре), а
    совпавшее с занятым именем поле сохраняется как `arg_<ключ>`: данные не
    теряются, и запись остаётся читаемой.
    """
    payload = {"time": _now(), "t": round(time.monotonic(), 4), "pid": os.getpid(),
               "thread": threading.current_thread().name, "kind": str(kind)}
    for key, value in fields.items():
        name = str(key)
        if name in payload:
            name = "arg_" + name
        payload[name] = _clean(value)
    return payload


def _bump(name, delta=1):
    with _lock:
        _counters[name] = _counters.get(name, 0) + delta


def mark(name, **fields):
    """Событие «что делает приложение» (хоткей, шторка, sync, экспорт).

    Дублируется в основной диагностический журнал (`diagnostics.event`) как
    `bt.<имя>` — чтобы все события сессии были видны и в `diagnostic.jsonl`.
    """
    if not _enabled:
        return
    record = _record("mark." + str(name), **fields)
    _emit_mark(record)
    try:
        import diagnostics as diag
        diag.event("bt." + str(name), **{k: _short(v) for k, v in fields.items()})
    except Exception:
        pass


def _emit_mark(record):
    _put(record)


def state(key, value):
    """Именованное состояние: попадает в итог сессии."""
    _bump("state.set")
    _put(_record("state", key=str(key), value=_clean(value)))


def env_fingerprint(extra=None, reason="start", screens=None, facts=None):
    """Паспорт машины в журнал: `machine.fingerprint` (+ запоминается для сводки).

    Зачем отдельно от `env.contract`. Контракт (`native_browser.env_contract`)
    описывает ЗАПУСК браузера: путь, ключи, зум, профиль. Паспорт описывает
    МАШИНУ: Windows и сборку, мониторы и их масштабы, сессию (RDP/консоль),
    права, тему, экран целиком. Именно паспорт объясняет дефекты «у другого
    пользователя, но не у меня» (ОШИБКИ.md № 2), потому что сравнивать две
    машины можно только по одинаково собранным числам.

    Никогда не бросает: если паспорт собрать не удалось, в журнале останется
    строка с `error`, а приложение продолжит работать.

    `facts` — готовый паспорт. Нужен тестам и примеру сессии: на стенде нет
    Windows, а проверять разбор на пустом паспорте бессмысленно.
    """
    global _machine
    if not _enabled:
        return None
    try:
        if facts is None:
            import machine_profile as mp
            facts = mp.profile(extra=extra, screens=screens)
    except Exception as exc:
        _put(_record("machine.fingerprint", reason=str(reason), error=str(exc)[:160]))
        return None
    _machine = facts
    record = _record("machine.fingerprint", reason=str(reason))
    record.update(facts)
    _put(record)
    # В человеческий файл — коротко: главное, что видно глазами.
    try:
        risks = mp.risk_codes(facts)
        _put(_record("machine.summary",
                     windows=((facts.get("windows") or {}).get("version")),
                     build=((facts.get("windows") or {}).get("build")),
                     screens=len(facts.get("screens") or []),
                     scale=facts.get("scale"),
                     session=((facts.get("session") or {}).get("kind")),
                     admin=((facts.get("session") or {}).get("admin")),
                     theme=facts.get("theme"), risks=risks))
    except Exception:
        pass
    return facts


def machine():
    """Последний собранный паспорт машины (или None, если не собирался)."""
    return _machine


def selfcheck(name, ok, detail="", advice=""):
    """Строка самопроверки: приборная панель САМА выносит вердикт по подсистеме.

    Идея ТЗ: разбирать чужой лог глазами не должен никто. Поэтому приложение
    на каждой ключевой вехе пишет вердикт «ок / не ок» — и в итоговой сводке
    видно состояние проверок, не читая журнал построчно:

    * `engine` — каким движком показывается страница (native/QtWebEngine);
    * `window_position` — встало ли окно в плейсхолдер (по замерам, не «должно»);
    * `layout` — совпадает ли раскладка страницы с целью (форма окна, dsf=1.0);
    * `export` — прикрепились ли файлы (по чипам и по факту на диске);
    * `hotkeys` — зарегистрированы ли клавиши (без прав администратора);
    * `mic` — доступен ли ввод голосом штатным путём страницы;
    * `admin_free` — работает ли приложение без прав администратора.

    Возвращает вердикт; никогда не бросает.
    """
    ok = bool(ok)
    if not _enabled:
        return ok
    global _selfchecks
    try:
        record = _record("selfcheck." + str(name), ok=ok,
                         detail=_short(detail), advice=_short(advice))
        _put(record)
        with _lock:
            _selfchecks[str(name)] = {"ok": ok, "detail": str(detail or ""),
                                      "advice": str(advice or ""), "time": _now()}
            _counters["selfcheck.%s.%s" % (name, "ok" if ok else "fail")] = \
                _counters.get("selfcheck.%s.%s" % (name, "ok" if ok else "fail"), 0) + 1
    except Exception:
        try:
            import diagnostics as diag
            diag.exception("btrace.selfcheck")
        except Exception:
            pass
    return ok


def selfchecks():
    """Все вердикты самопроверки: имя → {ok, detail, advice, time}."""
    with _lock:
        return {k: dict(v) for k, v in _selfchecks.items()}


def _read_selfchecks(directory):
    """Самопроверки из файла: нужны, если вердикты писались до перезапуска."""
    out = {}
    for row in _read_jsonl(Path(directory) / EVENT_FILE):
        kind = str(row.get("kind") or "")
        if not kind.startswith("selfcheck."):
            continue
        name = kind.split(".", 1)[1]
        out[name] = {"ok": bool(row.get("ok")), "detail": str(row.get("detail") or ""),
                     "advice": str(row.get("advice") or ""), "time": row.get("time")}
    return out


def _drain_wait(timeout=2.0):
    """Дождаться, пока писатель разгребёт очереди (нужно для итогов сессии)."""
    deadline = time.monotonic() + max(0.05, float(timeout))
    while time.monotonic() < deadline:
        try:
            empty = (_queue is None or _queue.empty()) and \
                    (_timeline_queue is None or _timeline_queue.empty())
        except Exception:
            return
        if empty:
            return
        time.sleep(0.05)


class _Writer(threading.Thread):
    """Пишет две очереди: события и временную шкалу. Диск — не поток GUI."""

    def __init__(self, directory, events, timeline):
        super().__init__(name="BrowserTraceWriter", daemon=True)
        self.directory = Path(directory)
        self._events = events
        self._timeline = timeline
        self._stopped = threading.Event()
        self._open()

    def _open(self):
        self.event = open(self.directory / EVENT_FILE, "a", encoding="utf-8")
        self.human = open(self.directory / HUMAN_FILE, "a", encoding="utf-8")
        self.tl = open(self.directory / TIMELINE_FILE, "a", encoding="utf-8")
        self.tl_human = open(self.directory / TIMELINE_HUMAN, "a", encoding="utf-8")

    def _rotate(self):
        try:
            if os.path.getsize(self.directory / EVENT_FILE) <= MAX_TRACE_BYTES:
                return
        except OSError:
            return
        try:
            for handle in (self.event, self.human, self.tl, self.tl_human):
                handle.close()
        except Exception:
            pass
        for name in (EVENT_FILE, HUMAN_FILE, TIMELINE_FILE, TIMELINE_HUMAN):
            path = self.directory / name
            try:
                if path.exists():
                    path.replace(self.directory / (name + ".1"))
            except OSError:
                pass
        self._open()

    @staticmethod
    def _human(record, skip=("time", "t", "pid", "thread", "kind")):
        parts = []
        for key, value in record.items():
            if key in skip:
                continue
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False,
                                                                  default=str)
            if len(text) > 110:
                text = text[:110] + "…"
            parts.append("%s=%s" % (key, text))
        stamp = str(record.get("time", ""))[11:23]
        return "%s %s | %s" % (stamp, record.get("kind", "?"), " ".join(parts))

    def run(self):
        while not self._stopped.is_set():
            progressed = False
            try:
                record = self._events.get(timeout=0.5)
            except queue.Empty:
                record = None
            if record is not None:
                progressed = True
                try:
                    self.event.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                    self.human.write(self._human(record) + "\n")
                except Exception:
                    pass
            progressed_tl = False
            while True:
                try:
                    item = self._timeline.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    continue
                progressed_tl = True
                try:
                    self.tl.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
                    self.tl_human.write(self._human(item) + "\n")
                except Exception:
                    break
            if progressed or progressed_tl:
                try:
                    self.event.flush()
                    self.tl.flush()
                except Exception:
                    pass
                self._rotate()
        self.flush_and_close()

    def flush_and_close(self):
        """Сбросить и закрыть файлы — и из потока, и из `finish()`.

        Без этого последние строки могли остаться в буфере и пропасть при
        выходе процесса: «человеческий» файл оказывался пустым, хотя
        JSON-журнал уже был записан (проверено на примере сессии).
        """
        for handle in (self.event, self.human, self.tl, self.tl_human):
            try:
                handle.flush()
                handle.close()
            except Exception:
                pass

    def stop(self):
        self._stopped.set()


# ============================================================================
# 1. ОКНО: измерение, целевой прямоугольник, объяснение расхождения
# ============================================================================
_user32 = None
_user32_ready = False


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _win_dll():
    """user32 с прототипами. На не-Windows вернёт None."""
    global _user32, _user32_ready
    if sys.platform != "win32":
        return None
    if _user32 is not None:
        return _user32
    if _user32_ready:
        return None
    _user32_ready = True
    try:
        u = ctypes.WinDLL("user32", use_last_error=True)
        from ctypes import wintypes
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
        u.GetWindowRect.restype = wintypes.BOOL
        u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
        u.GetClientRect.restype = wintypes.BOOL
        u.GetParent.argtypes = [wintypes.HWND]
        u.GetParent.restype = wintypes.HWND
        u.IsWindow.argtypes = [wintypes.HWND]
        u.IsWindow.restype = wintypes.BOOL
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsWindowVisible.restype = wintypes.BOOL
        u.IsIconic.argtypes = [wintypes.HWND]
        u.IsIconic.restype = wintypes.BOOL
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.restype = ctypes.c_int
        u.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(_POINT)]
        u.ClientToScreen.restype = wintypes.BOOL
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(ctypes.c_ulong)]
        u.GetWindowThreadProcessId.restype = ctypes.c_ulong
        u.MapWindowPoints.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.POINTER(_POINT),
                                      ctypes.c_uint]
        u.MapWindowPoints.restype = ctypes.c_int
        try:
            u.GetDpiForWindow.argtypes = [wintypes.HWND]
            u.GetDpiForWindow.restype = ctypes.c_uint
        except Exception:
            pass
        try:
            u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        except Exception:
            pass
        _user32 = u
    except Exception:
        _user32 = None
    return _user32


GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_POPUP = 0x80000000


def _class_name(u, hwnd):
    buf = ctypes.create_unicode_buffer(256)
    try:
        if u.GetClassNameW(hwnd, buf, 255):
            return buf.value
    except Exception:
        pass
    return ""


def window_probe(hwnd, parent=0, inset=(0, 0, 0, 0)):
    """Снимок окна: где просили, где оказалось, почему разошлось.

    Возвращает словарь; на не-Windows — {'supported': False}. Все числа —
    физические пиксели (как их видит Windows).
    """
    result = {"supported": False, "hwnd": int(hwnd or 0), "parent": int(parent or 0),
              "verdict": "unsupported", "reasons": []}
    u = _win_dll()
    if u is None or not hwnd:
        return result
    result["supported"] = True
    try:
        if not u.IsWindow(hwnd):
            result.update({"verdict": "dead_window", "reasons": ["dead_window"]})
            return result
        parent_actual = int(u.GetParent(hwnd) or 0)
        rect = _RECT()
        client = _RECT()
        ok_rect = bool(u.GetWindowRect(hwnd, ctypes.byref(rect)))
        ok_client = bool(u.GetClientRect(hwnd, ctypes.byref(client)))
        style = 0
        exstyle = 0
        try:
            style = int(u.GetWindowLongPtrW(hwnd, GWL_STYLE))
            exstyle = int(u.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))
        except Exception:
            pass
        dpi = 0
        try:
            dpi = int(u.GetDpiForWindow(hwnd))
        except Exception:
            dpi = 0
        tid = ctypes.c_ulong(0)
        pid = 0
        try:
            tid = u.GetWindowThreadProcessId(hwnd, ctypes.byref(ctypes.c_ulong(0)))
        except Exception:
            tid = 0
        try:
            pt = _POINT()
            pid_c = ctypes.c_ulong(0)
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_c))
            pid = int(pid_c.value)
        except Exception:
            pid = 0
        result.update({
            "class": _class_name(u, hwnd),
            "rect": [rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top],
            "client": [client.right - client.left, client.bottom - client.top],
            "parent_actual": parent_actual,
            "visible": bool(u.IsWindowVisible(hwnd)),
            "minimized": bool(u.IsIconic(hwnd)),
            "child_style": bool(style & WS_CHILD),
            "popup_style": bool(style & WS_POPUP),
            "visible_style": bool(style & WS_VISIBLE),
            "dpi": dpi,
            "scale": round(dpi / 96.0, 4) if dpi else None,
            "thread": int(tid or 0),
            "pid": pid,
            "rect_ok": ok_rect,
            "client_ok": ok_client,
            # Поля `error` здесь СОЗНАТЕЛЬНО нет: это был «последний код
            # ошибки» от совсем другой операции, и в логе он выглядел как
            # ошибка замера, которой не было (см. ОШИБКИ.md № 7).
        })
        target = target_rect(u, parent, inset) if parent else None
        result["target"] = target
        if target:
            dx = int(rect.left - target[0])
            dy = int(rect.top - target[1])
            dw = int((rect.right - rect.left) - target[2])
            dh = int((rect.bottom - rect.top) - target[3])
            result.update({"dx": dx, "dy": dy, "dw": dw, "dh": dh})
            reasons = []
            if parent_actual != int(parent):
                reasons.append("not_child")
            if not result["visible"]:
                reasons.append("hidden")
            if not result["child_style"]:
                reasons.append("detached_style")
            if abs(dx) <= 1 and abs(dy) <= 1 and abs(dw) <= 1 and abs(dh) <= 1:
                verdict = "on_target"
            else:
                verdict = "off_target"
                if abs(dx - round(dx * (result["scale"] or 1.0))) <= 1 and \
                        (result["scale"] or 1.0) != 1.0:
                    reasons.append("dpi_scale")
                elif dw or dh:
                    reasons.append("size_clamped")
                reasons.append("off_target")
            result["verdict"] = verdict
            result["reasons"] = reasons
            result["explain"] = explain_reasons(reasons, result)
    except Exception as exc:  # pragma: no cover - Windows only
        result["probe_error"] = str(exc)[:200]
    return result


def target_rect(u, parent, inset=(0, 0, 0, 0)):
    """Целевой прямоугольник окна браузера В ЭКРАННЫХ координатах.

    Считается ровно так же, как `win32_embed.box_for`: от клиентской области
    родителя и inset (физические px). Если родителя нет — None.
    """
    if u is None or not parent:
        return None
    try:
        if not u.IsWindow(parent):
            return None
        client = _RECT()
        if not u.GetClientRect(parent, ctypes.byref(client)):
            return None
        origin = _POINT(0, 0)
        if not u.ClientToScreen(parent, ctypes.byref(origin)):
            return None
        width = client.right - client.left
        height = client.bottom - client.top
        left, top, right, bottom = (int(v or 0) for v in (tuple(inset) + (0, 0, 0, 0))[:4])
        box_w = width - left - right
        box_h = height - top - bottom
        if box_w <= 0 or box_h <= 0:
            return None
        return (int(origin.x) + left, int(origin.y) + top, int(box_w), int(box_h))
    except Exception:
        return None


def explain_reasons(reasons, data=None):
    """Человеческие объяснения кодов причин, с числами из замера."""
    data = data or {}
    lines = []
    for code in reasons:
        text = REASONS.get(code, "")
        if code == "dpi_scale" and data.get("scale"):
            text += " Масштаб окна: %.2f (DPI %s)." % (float(data["scale"]), data.get("dpi"))
        if code == "off_target":
            text += " dx=%s dy=%s dw=%s dh=%s (px)." % (data.get("dx"), data.get("dy"),
                                                        data.get("dw"), data.get("dh"))
        if code == "not_child":
            text += " parent_actual=%s, parent_ожидался=%s." % (data.get("parent_actual"),
                                                                data.get("parent"))
        if code == "call_failed":
            error = int(data.get("last_error") or 0)
            text += " GetLastError=%s (%s)." % (error, WIN32_ERRORS.get(error, "см. winerror"))
        if code == "async_pending":
            text += " С момента вызова прошло %s мс." % data.get("since_ms")
        if text:
            lines.append(text)
    return lines


def record_win_call(name, ok, hwnd=0, parent=0, inset=(0, 0, 0, 0), error=None,
                    seconds=None, extra=None):
    """Зарегистрировать вызов Win32, меняющий окно браузера.

    Вызывается из `win32_embed` ПОСЛЕ каждого вызова. Здесь же измеряется
    фактическое положение — поэтому по логу видно, применился ли вызов.
    """
    global _win_calls, _last_win_call
    # Пока сбор не включён — не трогаем ни Windows, ни файлы: вызов обязан быть
    # полностью бесплатным и невидимым для приложения.
    if not _enabled or not _started:
        return None
    probe = window_probe(hwnd, parent, inset) if hwnd else {"supported": False}
    with _lock:
        _win_calls += 1
        call_index = _win_calls
        _last_win_call = {"name": name, "ok": bool(ok), "ms": seconds, "index": call_index,
                          "error": error, "time": _now()}
    for code in (probe.get("reasons") or []):
        _bump("reason.%s" % code)
    if error:
        _bump("reason.call_failed")
    # Состояние окна пишем только при расхождении или неудаче: лог не должен
    # пухнуть от одинаковых «всё хорошо».
    interesting = (not ok) or probe.get("verdict") in ("off_target", "dead_window") \
        or bool(extra)
    if interesting:
        # `extra` может содержать ключ с тем же именем, что и замер (например,
        # visible): словарь собирается заранее, иначе будет TypeError —
        # проверено на живом примере (v20).
        payload = {"verdict": probe.get("verdict"), "reasons": probe.get("reasons"),
                   "rect": probe.get("rect"), "target": probe.get("target"),
                   "dx": probe.get("dx"), "dy": probe.get("dy"), "dw": probe.get("dw"),
                   "dh": probe.get("dh"), "client": probe.get("client"),
                   "visible": probe.get("visible"), "minimized": probe.get("minimized"),
                   "child": probe.get("child_style"), "scale": probe.get("scale"),
                   "parent_actual": probe.get("parent_actual"),
                   "explain": probe.get("explain")}
        payload.update(extra or {})
        _put(_record("win32.call", name=str(name), ok=bool(ok), error=error,
                     ms=round(seconds, 3) if isinstance(seconds, (int, float)) else seconds,
                     call_index=call_index, **payload))
        _bump("win32.call.interesting")
    else:
        _put(_record("win32.call.ok", name=str(name), ok=True, error=error,
                     call_index=call_index, verdict=probe.get("verdict", "ok"),
                     ms=round(seconds, 3) if isinstance(seconds, (int, float)) else seconds))
    _bump("win32.call")
    return probe


def last_win_call():
    with _lock:
        return dict(_last_win_call)


# ============================================================================
# 2. ВРЕМЕННАЯ ШКАЛА: что происходит с окном и страницей после возмущения
# ============================================================================
JS_SIGNATURE = r"""(() => {
  try {
    const vv = window.visualViewport;
    const el = document.querySelector('textarea, div[contenteditable="true"], [role="textbox"]');
    let box = null;
    if (el && el.getBoundingClientRect) {
      const r = el.getBoundingClientRect();
      box = [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)];
    }
    return {
      iw: window.innerWidth | 0, ih: window.innerHeight | 0,
      dpr: Math.round((window.devicePixelRatio || 0) * 1000) / 1000,
      sx: Math.round(window.scrollX || 0), sy: Math.round(window.scrollY || 0),
      vw: vv ? Math.round(vv.width) : 0, vh: vv ? Math.round(vv.height) : 0,
      vs: vv ? Math.round((vv.scale || 1) * 1000) / 1000 : 0,
      ox: vv ? Math.round(vv.offsetLeft) : 0, oy: vv ? Math.round(vv.offsetTop) : 0,
      cw: box ? box[2] : 0, ch: box ? box[3] : 0,
      cx: box ? box[0] : 0, cy: box ? box[1] : 0,
      ready: String(document.readyState || ''),
      active: (document.activeElement && document.activeElement.tagName || '').toLowerCase()
    };
  } catch (e) { return {error: String(e).slice(0, 80)}; }
})()"""


def page_signature(page, timeout=0.6):
    """Метрики страницы одним коротким вызовом. None — страницы нет."""
    if page is None:
        return None
    try:
        value = page.eval(JS_SIGNATURE, timeout=timeout).get("value")
        return value if isinstance(value, dict) else None
    except Exception:
        return None


class WindowWatch(threading.Thread):
    """Временная шкала вокруг возмущения: «съехало — вернулось» в миллисекундах.

    Пишет ТОЛЬКО изменения (плюс редкий пульс), поэтому шкала читается глазами:
    каждая строка — новое состояние окна/страницы с отметкой времени от начала.
    """

    def __init__(self, tag, hwnd, parent, inset, page_provider=None, extra_provider=None,
                 duration=8.0, fast_ms=20, slow_ms=250, fast_until=3.0,
                 stable_needed=3, min_ms=1200):
        super().__init__(name="BrowserWatch:%s" % tag, daemon=True)
        self.tag = str(tag)
        self.hwnd = int(hwnd or 0)
        self.parent = int(parent or 0)
        self.inset = tuple(int(v) for v in (tuple(inset) + (0, 0, 0, 0))[:4])
        self.page_provider = page_provider
        self.extra_provider = extra_provider
        self.duration = float(duration)
        self.fast_ms = max(10, int(fast_ms))
        self.slow_ms = max(50, int(slow_ms))
        self.fast_until = float(fast_until)
        self.stable_needed = max(2, int(stable_needed))
        self.min_ms = max(0, int(min_ms))
        self._stopped = threading.Event()
        self.index = 0
        self.started_at = 0.0
        self.samples = 0
        self.changes = 0
        self.errors = 0
        self.settle_ms = None
        self.stable_since_ms = None
        self.last_pulse_ms = 0.0
        self.off_episodes = 0
        self._was_off = False
        self.deviations = 0
        self.max_offset = 0
        self.final_verdict = None

    # ---------------------------------------------------------------- public
    def stop(self):
        self._stopped.set()

    def run(self):  # pragma: no cover - время/потоки
        self.started_at = time.monotonic()
        with _lock:
            _watchers.add(self)
        try:
            self._loop()
        except Exception as exc:
            _bump("watch.error")
            _put(_record("watch.error", tag=self.tag, error=str(exc)[:200]))
        finally:
            with _lock:
                _watchers.discard(self)
            self._finalize()

    # ---------------------------------------------------------------- intern
    def _elapsed_ms(self):
        return int((time.monotonic() - self.started_at) * 1000)

    def _extra(self):
        if not self.extra_provider:
            return {}
        try:
            data = self.extra_provider()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _loop(self):
        _put(_record("watch.begin", tag=self.tag, hwnd=self.hwnd, parent=self.parent,
                     inset=list(self.inset), duration_s=self.duration,
                     fast_ms=self.fast_ms, slow_ms=self.slow_ms,
                     page_provider=bool(self.page_provider), **self._extra()))
        last = None
        last_change_ms = 0
        stable = 0
        page_failures = 0
        page_backoff_ms = 0
        page_next_ms = 0
        while not self._stopped.is_set():
            elapsed = self._elapsed_ms()
            if elapsed > self.duration * 1000:
                break
            probe = window_probe(self.hwnd, self.parent, self.inset) if self.hwnd else {}
            page = None
            # Метрики страницы идут через общее CDP-соединение, поэтому берём
            # их отдельным, более редким потоком: окно — на каждом замере,
            # страница — не чаще, чем позволяет бюджет и предыдущий ответ.
            want_page = (self.page_provider is not None and page_failures < 5
                         and elapsed >= page_next_ms)
            if want_page:
                page_started = time.monotonic()
                try:
                    page = page_signature(self.page_provider())
                except Exception:
                    page = None
                spent_ms = int((time.monotonic() - page_started) * 1000)
                page_next_ms = elapsed + max(150, spent_ms * 3)
                if spent_ms > 350:
                    # CDP занят приложением — не мешаем ему: реже спрашиваем.
                    page_backoff_ms = min(2000, max(page_backoff_ms, spent_ms * 4))
                    page_next_ms = elapsed + page_backoff_ms
                    _put(_record("watch.page_backoff", tag=self.tag, ms=elapsed,
                                 spent_ms=spent_ms, next_ms=page_next_ms))
                elif page_backoff_ms:
                    page_backoff_ms = max(0, page_backoff_ms // 2)
                    page_next_ms = elapsed + page_backoff_ms
                if page is None:
                    page_failures += 1
                else:
                    page_failures = 0
            record = {
                "time": _now(), "pid": os.getpid(), "thread": self.name,
                "tag": self.tag, "ms": elapsed, "idx": self.index,
                "rect": probe.get("rect"), "target": probe.get("target"),
                "dx": probe.get("dx"), "dy": probe.get("dy"),
                "dw": probe.get("dw"), "dh": probe.get("dh"),
                "client": probe.get("client"), "vis": probe.get("visible"),
                "min": probe.get("minimized"), "child": probe.get("child_style"),
                "scale": probe.get("scale"), "verdict": probe.get("verdict"),
                "reasons": probe.get("reasons"), "page": page,
                "wc": _win_calls_snapshot(),
                "extra": self._extra(),
            }
            self.index += 1
            self.samples += 1
            # Страница опрашивается реже окна: «страница не спрошена» — это НЕ
            # изменение состояния. Иначе каждое второе измерение выглядело бы
            # как перемена, и шкала превращалась бы в шум.
            if page is not None:
                self._last_page = page
            page_view = page if page is not None else getattr(self, "_last_page", None)
            record["page"] = page_view
            record["page_fresh"] = page is not None
            signature = json.dumps({k: record.get(k) for k in
                                    ("rect", "target", "client", "vis", "min", "child",
                                     "scale", "extra")} |
                                   {"page": page_view}, sort_keys=True, default=str)
            changed = signature != last
            if changed:
                self.changes += 1
                last = signature
                last_change_ms = elapsed
                stable = 0
                record["kind"] = "watch.change"
                _put(record, timeline=True)
            else:
                stable += 1
            # Устой: позиция совпала и держится `stable_needed` замеров подряд.
            if probe.get("verdict") == "on_target" and changed is False:
                if stable >= self.stable_needed and elapsed >= self.min_ms \
                        and self.stable_since_ms is None:
                    self.stable_since_ms = elapsed
                    self.settle_ms = elapsed
                    _put(_record("watch.settled", tag=self.tag, ms=elapsed,
                                 samples=self.samples, changes=self.changes,
                                 page=page, extra=record["extra"]), timeline=False)
            elif probe.get("verdict") != "on_target":
                # Снова ушло с цели — «устой» отменяется: съезжание продолжается.
                self.stable_since_ms = None
                self.settle_ms = None
            off_target = probe.get("verdict") not in (None, "on_target")
            if off_target:
                for code in (probe.get("reasons") or []):
                    _bump("reason.%s" % code)
                self.max_offset = max(self.max_offset,
                                      abs(int(probe.get("dx") or 0)) + abs(int(probe.get("dy") or 0)))
                if self._was_off is False:
                    # Уход считается эпизодом: «отклонилось — вернулось» = 1.
                    self.off_episodes += 1
            self._was_off = off_target
            if changed and off_target:
                # Счётчик отклонений = число ЗАПИСАННЫХ строк «не на цели»
                # (изменения и пульсы). Раньше он считал замеры (20 мс × 2 с =
                # ~100) и в сводке выглядел как «94 отклонения» — проверено.
                self.deviations += 1
            # Пульс — редкая отметка «ничего не меняется»: не чаще раза в
            # секунду от последнего ПУЛЬСА (не от изменения). Иначе, пока окно
            # стоит не на цели, шкала превращается в поток копий — проверено.
            if not changed and elapsed - max(last_change_ms, self.last_pulse_ms) > 1000:
                self.last_pulse_ms = elapsed
                record["kind"] = "watch.pulse"
                _put(record, timeline=True)
                if off_target:
                    self.deviations += 1
            self.final_verdict = probe.get("verdict")
            interval = self.fast_ms if elapsed < self.fast_until * 1000 else self.slow_ms
            if self._stopped.wait(interval / 1000.0):
                break
        end = _record("watch.end", tag=self.tag, ms=self._elapsed_ms(), samples=self.samples,
                      changes=self.changes, settle_ms=self.settle_ms,
                      deviations=self.deviations, off_episodes=self.off_episodes,
                      max_offset_px=self.max_offset,
                      final_verdict=self.final_verdict, errors=self.errors)
        # Итог шкалы пишется И в шкалу, и в журнал: в шкале он закрывает
        # хронологию («встало на цель через N мс»), в журнале — попадает в
        # машинную сводку и в разбор присланной сессии.
        _put(end, timeline=True)
        _put(dict(end))

    def _finalize(self):
        summary = {
            "tag": self.tag, "duration_ms": self._elapsed_ms(), "samples": self.samples,
            "changes": self.changes, "settle_ms": self.settle_ms,
            "deviations": self.deviations, "off_episodes": self.off_episodes,
            "max_offset_px": self.max_offset,
            "final_verdict": self.final_verdict,
        }
        state("watch.%s" % self.tag, summary)
        if self.settle_ms is not None and self.settle_ms > 1000:
            _put(_record("watch.slow_settle", tag=self.tag,
                         settle_ms=self.settle_ms,
                         hint="Пользователь видит это как «съехало и вернулось через N секунд»."))


def _win_calls_snapshot():
    with _lock:
        return _win_calls


def watch(tag, hwnd, parent, inset, page_provider=None, extra_provider=None, **kwargs):
    """Запустить временную шкалу. Возвращает объект (или None, если выключено)."""
    if not _enabled:
        return None
    if not hwnd:
        mark("watch.skipped", tag=str(tag), reason="no_hwnd")
        return None
    watcher = WindowWatch(str(tag), hwnd, parent, inset,
                          page_provider=page_provider, extra_provider=extra_provider,
                          **kwargs)
    watcher.start()
    return watcher


def stop_watchers():
    with _lock:
        watchers = list(_watchers)
    for watcher in watchers:
        try:
            watcher.stop()
        except Exception:
            pass


def active_watchers():
    """Сколько шкал сейчас пишется (нужно GUI-таймеру снимков)."""
    with _lock:
        return len(_watchers)


# ============================================================================
# 3. СТРАНИЦА: структура («код»), метрики, окружение
# ============================================================================
JS_META = r"""(() => {
  try {
    const scripts = Array.from(document.scripts || []);
    const clean = (u) => {
      try { const x = new URL(u, location.href); return x.host + x.pathname; }
      catch (e) { return ''; }
    };
    const frames = Array.from(document.querySelectorAll('iframe')).map((f) => {
      try { const x = new URL(f.src || '', location.href); return x.host; } catch (e) { return ''; }
    });
    const csp = document.querySelector('meta[http-equiv="Content-Security-Policy"]');
    const metas = Array.from(document.querySelectorAll('meta[name]')).map(m => m.name).slice(0, 40);
    const langs = Array.from(document.querySelectorAll('html')).map(h => h.lang || '');
    const links = Array.from(document.styleSheets || []).length;
    return {
      host: location.hostname, path: (location.pathname || '').slice(0, 80),
      protocol: location.protocol, titleLen: (document.title || '').length,
      ready: document.readyState,
      nodes: document.getElementsByTagName('*').length,
      scripts_total: scripts.length,
      scripts_inline: scripts.filter(s => !s.src).length,
      scripts: scripts.slice(0, 120).map(s => ({src: s.src ? clean(s.src) : '',
        len: (s.textContent || '').length, type: (s.type || '').slice(0, 40),
        async: !!s.async, defer: !!s.defer})),
      frames: frames.slice(0, 20), frames_total: frames.length,
      stylesheets: links, csp: csp ? 1 : 0, metas: metas,
      lang: langs[0] || '', dir: document.documentElement.dir || '',
      color_scheme: (getComputedStyle(document.documentElement).colorScheme || '').slice(0, 24),
      body_bg: (getComputedStyle(document.body || document.documentElement).backgroundColor || '').slice(0, 32),
      viewport: window.innerWidth + 'x' + window.innerHeight,
      dpr: Math.round((window.devicePixelRatio || 0) * 1000) / 1000,
      resources: (performance.getEntriesByType('resource') || []).length,
      has_focus: document.hasFocus(),
      active: (document.activeElement && (document.activeElement.tagName || '')) + ''
    };
  } catch (e) { return {error: String(e).slice(0, 120)}; }
})()"""

JS_OUTLINE = r"""(() => {
  try {
    const out = [];
    const MAX = %d;
    const safe = (el) => {
      const r = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
      let cs = {};
      try { cs = getComputedStyle(el); } catch (e) {}
      return {
        d: 0, t: (el.tagName || '').toLowerCase(),
        id: (el.id || '').slice(0, 48),
        c: Array.from(el.classList || []).slice(0, 5).map(c => String(c).slice(0, 40)),
        r: el.getAttribute ? (el.getAttribute('role') || '').slice(0, 32) : '',
        box: r ? [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)] : null,
        disp: cs.display || '', pos: cs.position || '', z: cs.zIndex || '',
        of: cs.overflow || '', vis: cs.visibility || '', op: cs.opacity || ''
      };
    };
    const walk = (el, depth) => {
      if (!el || out.length >= MAX || depth > 24) return;
      const item = safe(el);
      item.d = depth;
      out.push(item);
      const kids = el.children || [];
      for (let i = 0; i < kids.length && out.length < MAX; i++) walk(kids[i], depth + 1);
    };
    walk(document.documentElement, 0);
    const composer = document.querySelector('textarea, div[contenteditable="true"], [role="textbox"]');
    const input = document.querySelector('input[type="file"]');
    const chips = document.querySelectorAll('div.ArblTe').length;
    let anchor = null;
    if (composer) {
      const r = composer.getBoundingClientRect();
      anchor = {tag: (composer.tagName || '').toLowerCase(),
                box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
                cls: Array.from(composer.classList || []).slice(0, 5),
                id: (composer.id || '').slice(0, 48),
                parent: (composer.parentElement && composer.parentElement.className || '').toString().slice(0, 80)};
    }
    return {outline: out, composer: anchor, file_inputs: document.querySelectorAll('input[type="file"]').length,
            chips: chips, nodes: out.length, has_file_input: !!input};
  } catch (e) { return {error: String(e).slice(0, 120)}; }
})()"""

JS_SANITIZED_DOM = r"""(() => {
  try {
    const MAX = %d;
    const ALLOWED = ['class', 'id', 'role', 'type', 'name', 'aria-live', 'aria-hidden',
                     'aria-expanded', 'aria-modal', 'placeholder', 'lang', 'dir', 'for',
                     'contenteditable', 'disabled', 'readonly', 'checked', 'src', 'href'];
    const KEEP_SRC = ['class', 'id', 'role', 'type', 'name', 'aria-live', 'aria-hidden',
                      'aria-expanded', 'aria-modal', 'placeholder', 'lang', 'dir', 'for',
                      'contenteditable', 'disabled', 'readonly', 'checked'];
    const out = [];
    let count = 0;
    const walk = (el, depth) => {
      if (!el || count >= MAX) return;
      count++;
      if (el.nodeType === 3) { out.push(''); return; }   // текст вырезаем
      if (el.nodeType === 8) { out.push(''); return; }   // комментарии не нужны
      if (el.nodeType !== 1) return;
      const tag = (el.tagName || '').toLowerCase();
      const tagOk = ['script', 'style', 'noscript', 'template', 'svg', 'link', 'meta'].indexOf(tag) < 0;
      const ind = '  '.repeat(Math.min(depth, 30));
      if (!tagOk) {
        out.push(ind + '<' + tag + ' data-len="' +
          ((el.textContent || '').length) + '" data-src="' +
          ((el.src || '') ? 'yes' : 'no') + '>');
        return;
      }
      let attrs = '';
      for (const a of Array.from(el.attributes || [])) {
        const n = (a.name || '').toLowerCase();
        let v = a.value == null ? '' : String(a.value);
        if (n === 'src' || n === 'href') {
          try { const x = new URL(v, location.href); if (n === 'src') v = x.host + x.pathname;
                else v = (v.startsWith('http') ? x.origin + x.pathname : v.slice(0, 60)); }
          catch (e) { v = v.slice(0, 60); }
        } else if (KEEP_SRC.indexOf(n) < 0) { continue; }
        attrs += ' ' + n + '="' + v.replace(/["<>]/g, '').slice(0, 120) + '"';
      }
      const childCount = (el.children || []).length;
      out.push(ind + '<' + tag + attrs + (childCount ? '>' : '/>'));
      for (const ch of Array.from(el.childNodes || [])) walk(ch, depth + 1);
      if (childCount) out.push(ind + '</' + tag + '>');
    };
    walk(document.documentElement, 0);
    return {html: out.join('\n'), nodes: count, truncated: count >= MAX};
  } catch (e) { return {error: String(e).slice(0, 120)}; }
})()"""

JS_EXPORT_AUDIT = r"""(() => {
  try {
    const files = Array.from(document.querySelectorAll('input[type="file"]'));
    const picked = [];
    for (const f of files.slice(0, 8)) {
      const list = Array.from(f.files || []).slice(0, 8);
      picked.push({count: list.length, accept: (f.accept || '').slice(0, 60),
                   names: list.map(x => String(x.name || '').slice(0, 80)),
                   sizes: list.map(x => x.size | 0)});
    }
    const chips = Array.from(document.querySelectorAll('div.ArblTe')).slice(0, 8);
    const chipNames = chips.map(c => (c.textContent || '').trim().slice(0, 80));
    const progress = document.querySelectorAll(
      '[role="progressbar"], mat-progress-bar, .progress, [aria-busy="true"]').length;
    const alerts = [];
    for (const a of Array.from(document.querySelectorAll('[role="alert"], .error, [aria-live="assertive"]')).slice(0, 5)) {
      const t = (a.textContent || '').trim();
      alerts.push(t.length ? t.slice(0, 60).replace(/\s+/g, ' ') : '');
    }
    return {
      file_inputs: files.length,
      inputs_with_files: picked.filter(p => p.count > 0).length,
      picked: picked,
      chips: chips.length, chip_names: chipNames,
      progress: progress, alerts: alerts.filter(Boolean),
      ready: document.readyState,
      focused: document.hasFocus(),
      active: (document.activeElement && document.activeElement.tagName || '').toLowerCase()
    };
  } catch (e) { return {error: String(e).slice(0, 120)}; }
})()"""


def _stamp(reason):
    return "%s-%s" % (re.sub(r"[^0-9a-zA-Z_-]+", "_", str(reason))[:32],
                      time.strftime("%H%M%S"))


def _write_artifact(prefix, reason, extension, text):
    """Сохранить артефакт страницы. Больше лимита — не сохраняем (см. шапку)."""
    if not _enabled or _dir is None:
        return None
    payload = text.encode("utf-8", errors="replace")
    if len(payload) > PAGE_CODE_MAX_BYTES:
        payload = payload[:PAGE_CODE_MAX_BYTES] + b"\n<!-- truncated -->"
        _bump("artifact.truncated")
    path = Path(_dir) / ("%s-%s.%s" % (prefix, _stamp(reason), extension))
    try:
        path.write_bytes(payload)
        _put(_record("artifact.written", file=path.name, bytes=len(payload),
                     reason=str(reason)))
        return path
    except OSError:
        _bump("artifact.failed")
        return None


def page_meta(page, reason="probe", timeout=3):
    """Окружение страницы: скрипты, фреймы, ресурсы — без текста и содержимого."""
    if page is None:
        return None
    try:
        value = page.eval(JS_META, timeout=timeout).get("value")
    except Exception:
        _bump("page.meta.failed")
        return None
    if not isinstance(value, dict):
        return None
    _put(_record("page.meta", reason=str(reason), **value))
    try:
        path = Path(_dir) / ("page-meta-%s.json" % _stamp(reason))
        path.write_text(json.dumps(value, ensure_ascii=False, indent=1, default=str),
                        encoding="utf-8")
    except Exception:
        pass
    return value


def page_outline(page, reason="probe", timeout=4, limit=OUTLINE_MAX_NODES):
    """Структура DOM: теги/классы/роли/геометрия. Текст не собирается вообще."""
    if page is None:
        return None
    script = JS_OUTLINE % int(limit)
    try:
        value = page.eval(script, timeout=timeout).get("value")
    except Exception:
        _bump("page.outline.failed")
        return None
    if not isinstance(value, dict) or value.get("error"):
        _put(_record("page.outline.error", reason=str(reason),
                     error=str((value or {}).get("error"))[:120]))
        return None
    _put(_record("page.outline", reason=str(reason), nodes=value.get("nodes"),
                 file_inputs=value.get("file_inputs"), chips=value.get("chips"),
                 composer=value.get("composer")))
    try:
        path = Path(_dir) / ("page-outline-%s.json" % _stamp(reason))
        path.write_text(json.dumps(value, ensure_ascii=False, indent=1, default=str),
                        encoding="utf-8")
    except Exception:
        pass
    return value


def page_code(page, reason="probe", timeout=5, node_limit=PAGE_CODE_MAX_NODES):
    """«Код страницы»: HTML-структура без текста, без значений и без скриптов.

    Это именно то, что нужно, чтобы понять, ПОЧЕМУ поле ввода стоит не там:
    видно цепочку элементов, классы, атрибуты и заголовок разметки. Текст
    пользователя, содержимое промтов, токены и значения полей вырезаны.
    """
    if page is None:
        return None
    script = JS_SANITIZED_DOM % int(node_limit)
    try:
        value = page.eval(script, timeout=timeout).get("value")
    except Exception:
        _bump("page.code.failed")
        return None
    if not isinstance(value, dict) or not value.get("html"):
        _put(_record("page.code.empty", reason=str(reason),
                     error=str((value or {}).get("error"))[:120]))
        return None
    html = value.get("html") or ""
    header = ("<!-- Legalyze: санитизированный снимок структуры страницы.\n"
              "     Текст узлов, содержимое скриптов, значения полей и токены вырезаны.\n"
              "     Причина: %s -->\n" % str(reason))
    path = _write_artifact("page-code", reason, "html", header + html)
    _put(_record("page.code", reason=str(reason), nodes=value.get("nodes"),
                 truncated=value.get("truncated"), file=path.name if path else ""))
    return value


def capture_page(page, reason="probe", full=False):
    """Полный снимок страницы: метрики + структура (+ «код», если full)."""
    result = {"reason": str(reason)}
    result["meta"] = page_meta(page, reason)
    result["outline"] = page_outline(page, reason)
    result["signature"] = page_signature(page)
    if full:
        result["code"] = page_code(page, reason)
    return result


def audit_export(page, stage, paths=None, extra=None):
    """Доказательство экспорта: что на диске и что реально в странице.

    `stage` — «before» / «after_inject» / «final». Возвращает словарь с
    вердиктом, который попадает и в журнал, и в итог сессии.
    """
    record = {"stage": str(stage)}
    files = []
    for path in (paths or []):
        try:
            p = Path(path)
            if not p.exists():
                files.append({"name": p.name, "exists": False})
                continue
            info = p.stat()
            digest = hashlib.sha256()
            with p.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(chunk)
            files.append({"name": p.name, "exists": True, "bytes": int(info.st_size),
                          "mtime": round(info.st_mtime, 1),
                          "sha256_12": digest.hexdigest()[:12]})
        except Exception as exc:
            files.append({"name": getattr(path, "name", str(path)), "error": str(exc)[:120]})
    record["files"] = files
    page_data = None
    if page is not None:
        try:
            page_data = page.eval(JS_EXPORT_AUDIT, timeout=3).get("value")
        except Exception:
            page_data = None
    if isinstance(page_data, dict):
        record["page"] = page_data
        inputs = int(page_data.get("file_inputs") or 0)
        with_files = int(page_data.get("inputs_with_files") or 0)
        chips = int(page_data.get("chips") or 0)
        names = []
        for chip in (page_data.get("chip_names") or []):
            if isinstance(chip, str) and chip.strip():
                names.append(chip.strip()[:80])
        record["chip_names"] = names
        disk_ok = bool(files) and all(f.get("exists") for f in files)
        # Порядок проверок — от ДОКАЗАННОГО к предполагаемому:
        # * чипы с именами в чате доказывают вложение даже тогда, когда поля
        #   `input[type=file]` на странице уже нет (Gemini убирает его после
        #   загрузки). Раньше проверка `inputs == 0` шла первой и вердикт
        #   объявлял «на этой поверхности вложение невозможно» 171 раз подряд
        #   при реально прикреплённых файлах — проверено логом
        #   20261004-162559-12408-c5e41c (chips=4, names заполнены);
        # * «файла нет на диске» — только если в чате ничего не прикрепилось.
        if chips > 0 and names:
            verdict = "confirmed_attached"
        elif chips > 0:
            verdict = "attached_unnamed"
        elif not disk_ok and files:
            verdict = "file_missing_on_disk"
        elif with_files > 0:
            verdict = "injected_not_attached"
        elif inputs == 0:
            verdict = "surface_without_file_input"
        else:
            verdict = "not_attached"
        record["verdict"] = verdict
        _bump("export.verdict.%s" % verdict)
    else:
        record["verdict"] = "page_unavailable"
    if extra:
        record.update(extra)
    # Одинаковые аудиты подряд (поток выгрузки проверяет состояние несколько
    # раз в секунду) не пишем в журнал каждый раз: от 171 одинаковых строк
    # «доказательство» превращалось в шум, а разбор показывал 171 «ошибку».
    # Первая строка вердикта, смена вердикта и редкий (раз в 5 с) повтор —
    # попадают всегда, остальные считаются счётчиком export.audit_repeat.
    global _last_audit
    signature = (str(stage), record.get("verdict"), tuple(names),
                 int(inputs), int(with_files))
    now = time.monotonic()
    repeated = (_last_audit.get("signature") == signature
                and (now - float(_last_audit.get("at") or 0.0)) < 5.0)
    if repeated:
        _bump("export.audit_repeat")
        record["repeated"] = True
        return record
    _last_audit = {"signature": signature, "at": now, "verdict": record.get("verdict")}
    _put(_record("export.audit", **record))
    if record.get("verdict") not in (None, "confirmed_attached"):
        state("export.%s" % stage, record.get("verdict"))
    return record


# ============================================================================
# 4. ИТОГИ: машинная сводка и человеческое «UNDERSTANDING.md»
# ============================================================================
#: Вердикты экспорта — для человеческого итога.
EXPORT_VERDICTS = {
    "confirmed_attached": "Файлы действительно прикрепились к чату (имена видны).",
    "attached_unnamed": "Чипы файлов есть, но имена не прочитались: смотреть chip_names.",
    "injected_not_attached": "Файл впрыснут в input, но в чате не появился: страница не приняла.",
    "not_attached": "Файл не прикрепился: чипов нет, в input файлов нет.",
    "surface_without_file_input": "На этой поверхности нет input[type=file] — вложение невозможно.",
    "file_missing_on_disk": "Файла нет на диске: экспортировать было нечего.",
    "page_unavailable": "Страница недоступна (CDP): экспорт не проверить.",
}


def finish():
    """Закрыть сбор: дописать итог, сводку и остановить шкалы."""
    global _finished, _started, _enabled
    if not _started or _finished:
        return
    _finished = True
    try:
        stop_watchers()
        time.sleep(0.05)
        mark("trace.finish", events=sum(_counters.values()), unknown=dict(_counters))
    except Exception:
        pass
    summary = build_summary()
    try:
        if _dir is not None:
            (_dir / SUMMARY_FILE).write_text(
                json.dumps(summary, ensure_ascii=False, indent=1, default=str),
                encoding="utf-8")
            (_dir / UNDERSTANDING_FILE).write_text(render_understanding(summary),
                                                   encoding="utf-8")
    except Exception:
        pass
    try:
        if _queue is not None:
            _queue.put(None)
        if _timeline_queue is not None:
            _timeline_queue.put(None)
        # Сначала даём писателю разгрести очереди, затем останавливаем поток и
        # СИНХРОННО закрываем файлы: иначе последние строки остаются в буфере
        # и теряются при выходе процесса.
        _drain_wait(2.0)
        if _writer is not None:
            _writer.stop()
            _writer.join(2.0)
            _writer.flush_and_close()
    except Exception:
        pass
    _started = False


def build_summary():
    """Итог сессии: счётчики, вердикты, ключевые метрики (машинно-читаемо)."""
    with _lock:
        counters = dict(_counters)
    summary = {
        "trace_version": TRACE_VERSION,
        "session_dir": str(_dir) if _dir else None,
        "installed_at": _session.get("installed_at"),
        "finished_at": _now(),
        "pid": os.getpid(),
        "platform": sys.platform,
        "counters": counters,
        "win_calls_total": _win_calls,
        "watchers": {},
        "machine": _machine,
        "selfcheck": selfchecks(),
        "problems": [],
        "hints": [],
    }
    # Шкалы: читаем записанные файлы (писатель дописывает асинхронно, поэтому
    # сначала ждём, чтобы он разгрёб очереди, и только потом читаем).
    _drain_wait(2.0)
    try:
        if _writer is not None:
            for handle in (getattr(_writer, "event", None), getattr(_writer, "tl", None)):
                try:
                    handle.flush()
                except Exception:
                    pass
    except Exception:
        pass
    if _dir is not None:
        summary["watchers"] = _summarize_timeline(Path(_dir) / TIMELINE_FILE)
        try:
            merged = _read_selfchecks(_dir)
            merged.update(summary.get("selfcheck") or {})
            summary["selfcheck"] = merged
        except Exception:
            pass
        summary["problems"], summary["hints"] = _diagnose(summary, Path(_dir))
    return summary


def _read_jsonl(path, limit=200000):
    rows = []
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as stream:
            for index, line in enumerate(stream):
                if index >= limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except OSError:
        pass
    return rows


def _summarize_timeline(path):
    """Итоги по каждой шкале: когда устоялось, сколько было отклонений."""
    result = {}
    for row in _read_jsonl(path):
        tag = str(row.get("tag") or "")
        kind = row.get("kind")
        if not tag or kind not in ("watch.change", "watch.deviation", "watch.pulse", "watch.end"):
            continue
        item = result.setdefault(tag, {"samples": 0, "changes": 0, "deviations": 0,
                                       "first_ms": None, "last_ms": 0,
                                       "max_offset_px": 0, "off_target_until_ms": None,
                                       "settle_ms": None, "final_verdict": None,
                                       "ended": False})
        item["samples"] += 1
        ms = int(row.get("ms") or 0)
        item["last_ms"] = max(item["last_ms"], ms)
        if item["first_ms"] is None:
            item["first_ms"] = ms
        if kind == "watch.end":
            # Итог шкалы: он точнее, чем «первый кадр на цели» (окно может
            # мигнуть на цели и уехать снова).
            item["ended"] = True
            item["settle_ms"] = row.get("settle_ms")
            item["off_episodes"] = row.get("off_episodes")
            item["changes"] = max(int(item["changes"]), int(row.get("changes") or 0))
            item["deviations"] = max(int(item["deviations"]), int(row.get("deviations") or 0))
            item["max_offset_px"] = max(int(item["max_offset_px"]),
                                        int(row.get("max_offset_px") or 0))
            if row.get("final_verdict"):
                item["final_verdict"] = row.get("final_verdict")
            continue
        if kind == "watch.change":
            item["changes"] += 1
        verdict = row.get("verdict")
        if verdict:
            item["final_verdict"] = verdict
        if verdict and verdict != "on_target":
            item["deviations"] += 1
            item["off_target_until_ms"] = ms
            offset = abs(int(row.get("dx") or 0)) + abs(int(row.get("dy") or 0))
            item["max_offset_px"] = max(item["max_offset_px"], offset)
    return result


def _diagnose(summary, directory):
    """Автоматический разбор: что в этой сессии было не так и что смотреть."""
    problems, hints = [], []
    watchers = summary.get("watchers") or {}
    slow = [(tag, data) for tag, data in watchers.items()
            if int(data.get("deviations") or 0) > 0]
    for tag, data in sorted(slow, key=lambda kv: -int(kv[1].get("deviations") or 0))[:6]:
        problems.append("watch:%s" % tag)
        settle = data.get("settle_ms")
        tail = ("Встало на цель через %s мс после возмущения." % settle
                if settle is not None else "Устойчивого положения так и не дождались.")
        episodes = data.get("off_episodes")
        hints.append("Шкала «%s»: уходов %s, строк «не на цели» %d, максимальный уход %d px, "
                     "последний кадр не на цели на %d мс. %s"
                     % (tag, episodes if episodes is not None else "?",
                        data.get("deviations", 0), data.get("max_offset_px", 0),
                        data.get("off_target_until_ms") or 0, tail))
    counters = summary.get("counters") or {}
    for code in ("dpi_scale", "not_child", "call_failed", "size_clamped", "parent_moved",
                 "async_pending", "hidden"):
        key = "reason.%s" % code
        if counters.get(key):
            problems.append(key)
    for verdict, description in (
            ("confirmed_attached", "Файлы действительно прикрепились к чату."),
            ("attached_unnamed", "Чипы файлов есть, но имена не прочитались: смотреть chip_names."),
            ("injected_not_attached", "Файл впрыснут в input, но в чате не появился: страница не приняла."),
            ("not_attached", "Файл не прикрепился: в чате нет чипов и в input нет файлов."),
            ("surface_without_file_input", "На этой поверхности нет input[type=file] — вложение невозможно."),
            ("file_missing_on_disk", "Файла нет на диске: экспортировать было нечего."),
            ("page_unavailable", "Страница недоступна (CDP): экспорт не проверить.")):
        count = counters.get("export.verdict.%s" % verdict)
        if count:
            hints.append("Экспорт «%s» ×%d — %s" % (verdict, count, description))
    for name, data in sorted((summary.get("selfcheck") or {}).items()):
        if data.get("ok"):
            continue
        problems.append("selfcheck:%s" % name)
        detail = (": %s" % data.get("detail")) if data.get("detail") else ""
        advice = data.get("advice") or ""
        hints.append("Проверка «%s» НЕ пройдена%s%s"
                     % (name, detail, (". Что делать: %s" % advice) if advice else ""))
    machine = summary.get("machine") or {}
    if machine:
        try:
            import machine_profile as mp
            for line in mp.format_risks(machine, limit=5):
                hints.append("Машина%s" % line[1:])
        except Exception:
            pass
    if not problems:
        hints.append("Отклонений окна в шкалах не зафиксировано, "
                     "критических вердиктов экспорта нет.")
    return problems, hints


def render_understanding(summary):
    """UNDERSTANDING.md — то, что читает человек первым."""
    lines = []
    lines.append("# Что делал браузер: разбор сессии")
    lines.append("")
    lines.append("Сформировано автоматически модулем `browser_trace.py` (%s)."
                 % TRACE_VERSION)
    lines.append("")
    lines.append("- Сессия: `%s`" % (summary.get("session_dir") or "?"))
    lines.append("- Начало: %s" % (summary.get("installed_at") or "?"))
    lines.append("- Конец: %s" % (summary.get("finished_at") or "?"))
    lines.append("- Всего вызовов Win32 к окну браузера: %s"
                 % summary.get("win_calls_total", 0))
    lines.append("")
    lines.append("## 0. Машина и проверки")
    lines.append("")
    machine = summary.get("machine") or {}
    if machine:
        try:
            import machine_profile as mp
            lines.append(mp.format_report(machine))
            risk_lines = mp.format_risks(machine)
            if risk_lines:
                lines.append("")
                lines.append("Риски этой машины (и что с ними делать):")
                lines.extend(risk_lines)
        except Exception:
            lines.append("- Паспорт машины есть в `machine.fingerprint`, "
                         "но отчёт по нему собрать не удалось.")
    else:
        lines.append("Паспорт машины в этой сессии не собирался "
                     "(старая сборка либо сбор отключён).")
    checks = summary.get("selfcheck") or {}
    lines.append("")
    if checks:
        lines.append("| Проверка | Итог | Подробности |")
        lines.append("|---|---|---|")
        for name, data in sorted(checks.items()):
            lines.append("| %s | %s | %s |" % (name, "ок" if data.get("ok") else "**НЕ ОК**",
                                               data.get("detail") or ""))
    else:
        lines.append("Самопроверок в этой сессии нет (старая сборка).")
    lines.append("")
    lines.append("## 1. Позиция окна: когда съезжало и когда встало")
    lines.append("")
    watchers = summary.get("watchers") or {}
    if not watchers:
        lines.append("Временных шкал не зафиксировано (возмущений не было).")
    else:
        lines.append("| Шкала | Замеров | Изменений | Уходов | Строк «не на цели» | "
                     "Макс. уход | Не на цели до | Встало на цель через |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for tag, data in sorted(watchers.items()):
            settle = data.get("settle_ms")
            lines.append("| %s | %s | %s | %s | %s | %s px | %s мс | %s |"
                         % (tag, data.get("samples"), data.get("changes"),
                            data.get("off_episodes"), data.get("deviations"),
                            data.get("max_offset_px"), data.get("off_target_until_ms"),
                            ("%s мс" % settle) if settle is not None else "не дождались"))
    lines.append("")
    lines.append("## 2. Экспорт файлов: что доказано")
    lines.append("")
    lines.append("| Вердикт | Сколько раз | Что значит |")
    lines.append("|---|---|---|")
    counters = summary.get("counters") or {}
    for verdict in ("confirmed_attached", "attached_unnamed", "injected_not_attached",
                    "not_attached", "surface_without_file_input", "file_missing_on_disk",
                    "page_unavailable"):
        count = counters.get("export.verdict.%s" % verdict)
        if count:
            lines.append("| %s | %s | %s |" % (verdict, count,
                                               EXPORT_VERDICTS.get(verdict, "")))
    if not any(counters.get("export.verdict.%s" % v) for v in
               ("confirmed_attached", "attached_unnamed", "injected_not_attached",
                "not_attached", "surface_without_file_input", "file_missing_on_disk",
                "page_unavailable")):
        lines.append("| — | — | проверок экспорта не было |")
    lines.append("")
    lines.append("## 3. Что это значит")
    lines.append("")
    for hint in (summary.get("hints") or []):
        lines.append("- %s" % hint)
    lines.append("")
    lines.append("## 4. Куда смотреть дальше")
    lines.append("")
    lines.append("- `browser-window-timeline.txt` — построчная хронология окна.")
    lines.append("- `browser-trace.txt` — все вызовы Win32 и вердикты экспорта.")
    lines.append("- `page-outline-*.json`, `page-code-*.html` — структура страницы.")
    lines.append("- `diagnostic.jsonl` — полный журнал приложения (события, CDP, ошибки).")
    lines.append("")
    return "\n".join(lines)
