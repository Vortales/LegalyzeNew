"""Полная диагностика v21: каждый объект, каждый вызов, каждый отказ.

Принципы (из отчёта «Error - 1»):

* лог пишется **асинхронно** — поток GUI никогда не ждёт диск, иначе
  «максимальное логирование» само стало бы причиной тормозов и миганий;
* в лог НЕ попадают пароли, токены, HWID, содержимое PDF/промтов и тела
  ответов — только типы, размеры, коды и перечисления;
* каждая запись — строка JSON (`diagnostic.jsonl`) + человеческая строка
  (`events.log`), чтобы логи можно было и читать глазами, и разбирать скриптом;
* в конце сессии пишется `summary.json`: вердикт по странице, режим браузера,
  все исключения счётчиками, все предупреждения, итог экспорта.
  Именно он и есть «обратная связь», которую можно прислать разработчику.

Ничего из этого не показывается пользователю: нет ни окон, ни консолей,
ни MessageBox (кроме аварийного хука, как и раньше).
"""
import atexit
import ctypes
from datetime import datetime, timezone
import faulthandler
import functools
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import platform
import queue
import re
import sys
import threading
import time
import traceback
import uuid
from storage_paths import app_dir

LOG_DIR = None
FAULT_FILE = None
_started = False
_exception_counts = {}
_last_beat = time.monotonic()
_states = {}
_warnings = []
_event_counts = {}
_dropped = 0
_queue = None
_writer = None
_lock = threading.RLock()
_sanitizer = re.compile(r'(?i)(token|password|secret|api[_-]?key|authorization|hwid|cookie|bearer)')

#: Максимум символов в строковом поле: логи должны читаться, а не тонуть в тексте.
MAX_FIELD = 400
MAX_QUEUE = 20000
MAX_BYTES = 12 * 1024 * 1024
ROTATIONS = 3
MAX_SESSIONS = 12
#: Через сколько секунд молчания главного потока печатать стек всех потоков.
HEARTBEAT_WARN = 15
FAULT_TIMEOUT = 300

EVENT_FILE = 'diagnostic.jsonl'
HUMAN_FILE = 'events.log'
SUMMARY_FILE = 'summary.json'


# ------------------------------------------------------------------ helpers --
def _short(value):
    """Обрезать строку и выкинуть из неё всё, что похоже на секрет."""
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = str(value)
    if len(text) > MAX_FIELD:
        text = text[:MAX_FIELD] + '…'
    return text


def _clean(key, value):
    """Поля с секретами не пишутся целиком — только факт их наличия."""
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, (list, tuple, set)):
        return [_clean(key, item) for item in list(value)[:64]]
    if isinstance(value, dict):
        return {str(k): _clean(str(k), v) for k, v in list(value.items())[:64]}
    if _sanitizer.search(str(key)):
        return '<redacted:%d>' % len(str(value))
    return _short(value)


def _fields(data):
    return {str(k): _clean(k, v) for k, v in data.items()}


# -------------------------------------------------------------------- writer --
class _Writer(threading.Thread):
    """Пишет очередь на диск: JSON-строки + человеческое зеркало."""

    def __init__(self, directory):
        super().__init__(name='DiagnosticWriter', daemon=True)
        self.directory = Path(directory)
        self.jsonl = None
        self.human = None
        self._open()
        self._stopping = False

    def _open(self):
        self.jsonl = open(self.directory / EVENT_FILE, 'a', encoding='utf-8')
        self.human = open(self.directory / HUMAN_FILE, 'a', encoding='utf-8')

    def _rotate_if_needed(self):
        try:
            if self.jsonl.tell() <= MAX_BYTES:
                return
        except Exception:
            return
        try:
            self.jsonl.close()
            self.human.close()
        except Exception:
            pass
        for name in (EVENT_FILE, HUMAN_FILE):
            for index in range(ROTATIONS, 0, -1):
                old = self.directory / ('%s.%d' % (name, index))
                if index == ROTATIONS and old.exists():
                    try:
                        old.unlink()
                    except OSError:
                        pass
                    continue
                if old.exists():
                    try:
                        old.replace(self.directory / ('%s.%d' % (name, index + 1)))
                    except OSError:
                        pass
            first = self.directory / name
            if first.exists():
                try:
                    first.replace(self.directory / ('%s.1' % name))
                except OSError:
                    pass
        self._open()

    def _human_line(self, record):
        parts = []
        for key, value in record.items():
            if key in ('time', 'pid'):
                continue
            text = json.dumps(value, ensure_ascii=False, default=str)
            if len(text) > 120:
                text = text[:120] + '…'
            parts.append('%s=%s' % (key, text))
        return '%s [%s] %s' % (record.get('time', ''), record.get('thread', '?'),
                               ' '.join(parts))

    def run(self):
        while not self._stopping:
            record = _queue.get()
            if record is None:
                break
            try:
                line = json.dumps(record, ensure_ascii=False, default=str)
                self.jsonl.write(line + '\n')
                self.human.write(self._human_line(record) + '\n')
                self._rotate_if_needed()
                if _queue.qsize() < 4:
                    self.jsonl.flush()
                    self.human.flush()
            except Exception:
                pass
        try:
            self.jsonl.flush()
            self.human.flush()
            self.jsonl.close()
            self.human.close()
        except Exception:
            pass

    def stop(self):
        self._stopping = True


def _emit(record):
    """Отправить запись в очередь. Потеря записи — сама событие, а не падение."""
    global _dropped
    if _queue is None:
        return
    try:
        with _lock:
            name = record.get('event', '?')
            _event_counts[name] = _event_counts.get(name, 0) + 1
        _queue.put_nowait(record)
    except queue.Full:
        with _lock:
            _dropped += 1


def event(event_name, **fields):
    _emit({'time': datetime.now(timezone.utc).isoformat(),
           'pid': os.getpid(), 'thread': threading.current_thread().name,
           'event': str(event_name), **_fields(fields)})


def warn(key, **fields):
    """Предупреждение: попадает и в поток событий, и в `summary.json`."""
    with _lock:
        if len(_warnings) < 500:
            _warnings.append({'time': datetime.now(timezone.utc).isoformat(),
                              'key': str(key),
                              'fields': _fields(fields)})
    event('warn.' + str(key), **fields)


def state(key, value):
    """Именованное состояние сессии: попадает в `summary.json`."""
    with _lock:
        previous = _states.get(key)
        _states[str(key)] = _clean(key, value)
    if previous != _states.get(key):
        event('state.set', key=str(key), value=_states.get(key))
    return _states.get(key)


def exception(where, info=None):
    typ, value, tb = info or sys.exc_info()
    name = getattr(typ, '__name__', str(typ))
    key = (str(where), name)
    with _lock:
        count = _exception_counts.get(key, 0) + 1
        _exception_counts[key] = count
    # В поток событий — только первые и дальше по степеням двойки: одно и то же
    # исключение в цикле не должно топить лог. В сводку — всегда со счётчиком.
    if count > 3 and count & (count - 1):
        return
    frames = [{'file': Path(f.filename).name, 'line': f.lineno, 'function': f.name}
              for f in traceback.extract_tb(tb)] if tb else []
    event('exception', where=str(where), occurrence=count, type=name,
          errno=getattr(value, 'errno', None), winerror=getattr(value, 'winerror', None),
          frames=frames)


def trace(fn):
    """Трассировка одной функции: вход, выход, длительность, отказ.

    Аргументы НЕ печатаются (в них токены, пути файлов пользователя и пароли) —
    только число позиционных и именованных.
    """
    if inspect.iscoroutinefunction(fn):
        return fn

    @functools.wraps(fn)
    def traced(*args, **kwargs):
        name = fn.__qualname__
        started = time.monotonic()
        event('trace.begin', name=name, args=len(args), kwargs=len(kwargs))
        try:
            result = fn(*args, **kwargs)
        except BaseException:
            event('trace.fail', name=name, type=sys.exc_info()[0].__name__,
                  seconds=round(time.monotonic() - started, 3))
            raise
        event('trace.end', name=name, seconds=round(time.monotonic() - started, 3))
        return result
    return traced


def stage(fn):
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        start = time.monotonic()
        event('stage.begin', name=fn.__qualname__)
        try:
            result = fn(*args, **kwargs)
        except SystemExit as exc:
            event('stage.exit', name=fn.__qualname__,
                  code=exc.code if isinstance(exc.code, int) else None)
            raise
        except TypeError:
            # Diagnose argument binding without exposing argument values or exception text.
            try:
                signature.bind(*args, **kwargs)
            except TypeError:
                event('call.signature_mismatch', name=fn.__qualname__,
                      positional_count=len(args), keyword_count=len(kwargs),
                      parameter_count=len(signature.parameters))
            exception(fn.__qualname__)
            raise
        except BaseException:
            exception(fn.__qualname__)
            raise
        event('stage.end', name=fn.__qualname__, seconds=round(time.monotonic()-start, 3))
        return result
    return wrapped


# -------------------------------------------------------------------- summary --
def summary_payload():
    with _lock:
        return {
            'session': dict(getattr(sys, '_legalyze_session', {}) or {}),
            'log_dir': str(LOG_DIR) if LOG_DIR else None,
            'generated': datetime.now(timezone.utc).isoformat(),
            'alive_seconds': round(time.monotonic() - _started, 1) if _started else None,
            'states': dict(_states),
            'exceptions': [{'where': k[0], 'type': k[1], 'count': v}
                           for k, v in sorted(_exception_counts.items(),
                                              key=lambda kv: -kv[1])],
            'warnings': list(_warnings[-50:]),
            'events': dict(sorted(_event_counts.items(), key=lambda kv: -kv[1])),
            'dropped_records': _dropped,
        }


def write_summary(reason='exit'):
    if not LOG_DIR:
        return
    payload = summary_payload()
    payload['end_reason'] = str(reason)
    try:
        (Path(LOG_DIR) / SUMMARY_FILE).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, default=str),
            encoding='utf-8')
    except Exception:
        pass


def flush(timeout=2.0):
    """Дописать всё, что осталось в очереди (вызывается при выходе)."""
    if _queue is None or _writer is None:
        return
    _queue.put(None)
    _writer.join(timeout)


# ---------------------------------------------------------------------- setup --
def _rotate_sessions(base):
    """Оставить только последние `MAX_SESSIONS` папок: лог не должен расти вечно."""
    try:
        sessions = sorted((p for p in base.iterdir() if p.is_dir()),
                          key=lambda p: p.name, reverse=True)
    except OSError:
        return
    for old in sessions[MAX_SESSIONS:]:
        for child in sorted(old.iterdir()):
            try:
                child.unlink()
            except OSError:
                pass
        try:
            old.rmdir()
        except OSError:
            pass


def setup():
    global LOG_DIR, FAULT_FILE, _started, _queue, _writer
    if _started:
        return
    session = time.strftime('%Y%m%d-%H%M%S') + '-%d-%s' % (os.getpid(), uuid.uuid4().hex[:6])
    base = app_dir() / 'logs'
    for base in (app_dir() / 'logs',):
        try:
            directory = base / session
            directory.mkdir(parents=True, exist_ok=False)
            LOG_DIR = directory
            break
        except OSError:
            continue
    else:
        raise RuntimeError('Cannot create diagnostic log directory')
    _started = time.monotonic()
    _queue = queue.Queue(maxsize=MAX_QUEUE)
    _writer = _Writer(LOG_DIR)
    _writer.start()
    FAULT_FILE = open(LOG_DIR / 'stacks.log', 'a', encoding='utf-8')
    faulthandler.enable(FAULT_FILE, all_threads=True)
    # Разовая страховка: если процесс залип намертво, через FAULT_TIMEOUT в
    # stacks.log лягут стеки всех потоков. Раньше было 60 с — из-за этого в
    # лог попадал ложный «Timeout», который принимали за настоящий краш.
    faulthandler.dump_traceback_later(FAULT_TIMEOUT, repeat=False, file=FAULT_FILE)
    sys._legalyze_session = {'id': session, 'pid': os.getpid(),
                             'started': datetime.now(timezone.utc).isoformat()}
    atexit.register(_shutdown)

    def uncaught(typ, value, tb):
        exception('sys.excepthook', (typ, value, tb))
        write_summary('uncaught:%s' % getattr(typ, '__name__', '?'))
        flush()
        if sys.platform == 'win32':
            ctypes.windll.user32.MessageBoxW(None,
                f'Ошибка {typ.__name__}. Диагностика:\n{LOG_DIR}', 'Legalyze — диагностика', 0x10)
    sys.excepthook = uncaught
    threading.excepthook = lambda args: exception('thread.excepthook',
                                                 (args.exc_type, args.exc_value,
                                                  args.exc_traceback))
    versions = {}
    for package in ('PyQt6', 'PyQt6-Qt6', 'requests', 'websocket-client',
                    'cryptography', 'fpdf2'):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = 'not in metadata'
    event('session.start', platform=platform.platform(), python=sys.version,
          bitness=ctypes.sizeof(ctypes.c_void_p)*8, executable=sys.executable,
          cwd=str(Path.cwd()), frozen=bool(getattr(sys, 'frozen', False)),
          nuitka_compiled=('__compiled__' in globals()),
          release_revision='universal-embedded-v21', packages=versions,
          windows_build=str(sys.getwindowsversion()) if sys.platform == 'win32' else None,
          modes={k: os.environ.get(k) for k in ('LEGALYZE_DISABLE_GPU',
                                                'LEGALYZE_EXTERNAL_BROWSER',
                                                'LEGALYZE_FRESH_PROFILE',
                                                'LEGALYZE_LOG_OFF')})
    threading.Thread(target=collect_system, name='SystemInventory', daemon=True).start()
    try:
        _rotate_sessions(base)
    except Exception:
        exception('diagnostics.rotate')


def _shutdown():
    try:
        event('session.atexit')
        write_summary('exit')
    except Exception:
        pass
    flush()


def install_qt(app):
    from PyQt6.QtCore import QTimer, qInstallMessageHandler, QT_VERSION_STR, PYQT_VERSION_STR
    qInstallMessageHandler(lambda kind, ctx, message: event('qt.message', kind=str(kind),
                                                            message=_short(message)))
    event('qt.start', qt=QT_VERSION_STR, pyqt=PYQT_VERSION_STR, platform=app.platformName(),
          style=app.style().objectName() if app.style() else None)
    screens = app.screens()
    event('qt.screen_count', screens=len(screens),
          primary=app.primaryScreen().name() if app.primaryScreen() else None)
    for screen in screens:
        event('qt.screen', geometry=screen.geometry().getRect(),
              available=screen.availableGeometry().getRect(),
              dpi=screen.logicalDotsPerInch(), dpr=screen.devicePixelRatio())

    def beat():
        global _last_beat
        _last_beat = time.monotonic()
    timer = QTimer(app)
    timer.timeout.connect(beat)
    timer.start(1000)
    app._diagnostic_timer = timer

    def watch():
        dumps = 0
        while True:
            time.sleep(10)
            age = round(time.monotonic() - _last_beat, 1)
            event('gui.heartbeat', age_seconds=age)
            if age > HEARTBEAT_WARN and dumps < 20:
                warn('gui_frozen', age_seconds=age)
                faulthandler.dump_traceback(file=FAULT_FILE, all_threads=True)
                dumps += 1
    threading.Thread(target=watch, name='DiagnosticWatchdog', daemon=True).start()
    app.aboutToQuit.connect(lambda: event('qt.aboutToQuit'))
    app.lastWindowClosed.connect(lambda: event('qt.lastWindowClosed'))


def install_http():
    import requests
    from urllib.parse import urlsplit
    original = requests.sessions.Session.request

    @functools.wraps(original)
    def request(self, method, url, *args, **kwargs):
        parsed = urlsplit(url)
        # Never record query, headers, request/response body, HWID or authorization.
        fields = dict(method=method, host=parsed.hostname,
                      path=parsed.path if parsed.path.startswith('/api/client/') else '<omitted>')
        started = time.monotonic()
        event('http.begin', **fields)
        try:
            response = original(self, method, url, *args, **kwargs)
        except Exception:
            exception('http.request')
            raise
        event('http.end', **fields, status=response.status_code,
              seconds=round(time.monotonic()-started, 3))
        return response
    requests.sessions.Session.request = request


def place_window(window, app):
    """Use Qt logical coordinates, not physical pixels from a different monitor."""
    screen = app.primaryScreen()
    g = screen.availableGeometry()
    # Keep existing fixed-size layout; top-align if the screen is shorter than it.
    window.move(g.x() + max(0, g.width()-window.width()-16),
                g.y() + max(0, (g.height()-window.height())//2))
    # v16: окно обязано целиком оказаться внутри рабочей области. Если монитор
    # сменили или отключили (проектор, второй экран) и окно уехало ВНЕ всех
    # экранов — возвращаем его на основной. Размер окна не трогаем: он
    # фиксированный и выверен, поэтому раскладка остаётся прежней.
    frame = window.frameGeometry()
    if (not frame.isEmpty()
            and not any(frame.intersects(s.availableGeometry()) for s in app.screens())):
        window.move(g.x() + max(0, (g.width()-window.width())//2),
                    g.y() + max(0, (g.height()-window.height())//2))
        event('main.geometry_rescued', geometry=window.geometry().getRect(),
              available=g.getRect())
    # Если экран физически ниже окна — честно пишем это в лог: браузер при
    # этом НЕ съезжает, просто нижняя полоса окна не помещается.
    if window.height() > g.height() or window.width() > g.width():
        event('main.geometry_clipped', window=[window.width(), window.height()],
              available=g.getRect())
    event('main.geometry', geometry=window.geometry().getRect(), available=g.getRect(),
          dpr=window.devicePixelRatioF(), visible=window.isVisible(),
          hwnd=int(window.winId()), screens=len(app.screens()))


def collect_system():
    """Bounded, background-only inventory. No serials, user list, IPs or environment dump."""
    if sys.platform != 'win32':
        return
    import subprocess
    command = (
        '$ErrorActionPreference="Stop"; '
        '[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); '
        '@{GPU=@(Get-CimInstance Win32_VideoController | '
        'Select-Object Name,DriverVersion,Status,AdapterRAM); '
        'OS=(Get-CimInstance Win32_OperatingSystem | '
        'Select-Object Caption,Version,BuildNumber,OSArchitecture); '
        'DPI=(Get-CimInstance Win32_DesktopMonitor | Select-Object ScreenWidth,ScreenHeight); '
        'Theme=(Get-ItemProperty -Path '
        '"HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize" '
        '-Name AppsUseLightTheme -ErrorAction SilentlyContinue).AppsUseLightTheme; '
        'Locale=(Get-Culture).Name} | ConvertTo-Json -Depth 4'
    )
    try:
        event('windows.privilege', elevated=bool(ctypes.windll.shell32.IsUserAnAdmin()),
              remote_session=bool(ctypes.windll.user32.GetSystemMetrics(0x1000)),
              proxy_variables_present=[k for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                                                   'NO_PROXY') if os.environ.get(k)])
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, timeout=20, creationflags=0x08000000)
        if result.returncode == 0:
            event('windows.inventory', data=_short(result.stdout.decode('utf-8', errors='replace')))
        else:
            event('windows.inventory.failed', returncode=result.returncode)
    except Exception:
        exception('windows.inventory')


def monitor_window(window):
    from PyQt6.QtCore import QTimer

    def sample():
        worker = window.worker
        try:
            extra = {
                'page_verdict': getattr(window, '_surface_verdict', None),
                'native_mode': bool(getattr(window, 'native_mode', False)),
                'chat_ready': bool(getattr(window, '_chat_ready', False)),
                'cover_visible': bool(getattr(window, 'browser_cover', None)
                                      and window.browser_cover.isVisible()),
                'settling': bool(getattr(window, '_settling', False)),
            }
        except Exception:
            exception('diagnostics.snapshot_extra')
            extra = {}
        event('main.snapshot', visible=window.isVisible(), minimized=window.isMinimized(),
              geometry=window.geometry().getRect(), dpr=window.devicePixelRatioF(),
              chrome_hwnd=window.chrome_hwnd,
              worker_running=bool(worker and worker.isRunning()),
              chromium_exit=worker.proc.poll() if worker and worker.proc else None,
              overlay_visible=window.overlay.isVisible(), **extra)
    timer = QTimer(window)
    timer.timeout.connect(sample)
    timer.start(5000)
    window._diagnostic_snapshot_timer = timer
    sample()


def inspect_browser(path):
    import hashlib
    try:
        target = Path(path)
        digest = hashlib.sha256()
        with target.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b''):
                digest.update(chunk)
        event('chromium.binary', path=str(target), bytes=target.stat().st_size,
              sha256=digest.hexdigest())
    except Exception:
        exception('chromium.binary')


def browser_binary(path):
    """Размер и время изменения: сравнить две сборки, не читая 300 МБ."""
    try:
        info = Path(path).stat()
        event('browser.binary', path=str(path), bytes=info.st_size,
              mtime=round(info.st_mtime, 1))
    except Exception:
        exception('browser.binary')
