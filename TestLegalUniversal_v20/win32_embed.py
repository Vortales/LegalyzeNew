"""Embedding of an out-of-process Chromium/Chrome window into the Qt GUI.

Why this module exists
----------------------
The page's own microphone is a *browser* feature (Web Speech API and/or
getUserMedia streamed to Google). QtWebEngine cannot provide it: its
`SpeechRecognition` crashes the renderer (no Google API key in the build) and
`getUserMedia` is unavailable (QTBUG-55108). Windows System.Speech — our v1..v7
bridge — is not what the user asked for and is not the page's own voice input.

So the browser must be a real, branded Chromium running as its own process.
`release/` did exactly that and the microphone worked; what it did *not* get
right is stability. Two of the three reported failures are windowing bugs that
this module fixes:

* **"wide window with a single ИИ label"** — the child window was positioned
  with `GetClientRect` (PHYSICAL pixels) but Chromium is per-monitor DPI aware
  (PMv2) and reads window sizes as DIPs. On any display scaled to 125%/150%
  the browser therefore believed it was ~1.5x wider than it really was, laid
  the page out in the wide desktop variant and showed the centred wordmark
  instead of the chat. Sizing alone cannot fix this at every DPI, so the
  layout is *pinned* over CDP (`Emulation.setDeviceMetricsOverride`) and the
  window is only responsible for pixels.
* **"chrome starts, hangs, does nothing"** — window discovery raced window
  creation, silently took the first `Chrome_WidgetWin_*` it saw (helper
  surfaces included) and, when `SetParent` failed, rolled back to a 900x700
  window at (80,80) — i.e. a detached browser the user could not use.

Everything here is *checked*: every Win32 call reports its result to the
diagnostics log, `SetParent` is verified with `GetParent`, and any failure is
rolled back so the user is never left with an invisible or detached browser.
"""
import ctypes
import time
from ctypes import wintypes

import diagnostics as diag

# ----------------------------------------------------------------- constants
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_POPUP = 0x80000000
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_DLGFRAME = 0x00400000
WS_BORDER = 0x00800000
WS_CLIPCHILDREN = 0x02000000
WS_CLIPSIBLINGS = 0x04000000
WS_EX_DLGMODALFRAME = 0x00000001
WS_EX_WINDOWEDGE = 0x00000100
WS_EX_CLIENTEDGE = 0x00000200
WS_EX_STATICEDGE = 0x00020000
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
HWND_TOP = 0
SW_HIDE = 0
SW_SHOWNA = 8
WM_CLOSE = 0x0010
SW_SHOW = 5
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_FRAMECHANGED = 0x0020
SWP_ASYNCWINDOWPOS = 0x4000
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002

# Гарантии уничтожения дерева процессов (v15): terminate() снимает только
# родителя, рендереры и утилиты выживают и копятся между запусками.
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

BROWSER_EXE_NAMES = ("chrome.exe", "msedge.exe", "chromium.exe",
                     "brave.exe", "browser.exe", "vivaldi.exe", "opera.exe")

# ITaskbarList: одного стиля мало — Chrome сам возвращает кнопку в панель
# задач через ITaskbarList::AddTab, поэтому стиль и DeleteTab нужны вместе.
CLSID_TASKBAR_LIST = "{56FDF344-FD6D-11D0-958A-006097C9A090}"
IID_ITASKBAR_LIST = "{56FDF342-FD6D-11D0-958A-006097C9A090}"
CLSCTX_ALL = 0x0017
_COM_HR_INIT = 3
_COM_ADD_TAB = 4
_COM_DELETE_TAB = 5
_COM_RELEASE = 2

BROWSER_WINDOW_CLASS = "Chrome_WidgetWin_1"
try:  # WINFUNCTYPE exists only on Windows; the module must still import
      # elsewhere so the test-suite can check its logic.
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
except Exception:  # pragma: no cover - Linux/macOS
    WNDENUMPROC = None


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _set_last_error(value=0):
    """ctypes.set_last_error/get_last_error exist only on Windows."""
    setter = getattr(ctypes, "set_last_error", None)
    if setter is not None:
        setter(value)


def _get_last_error():
    getter = getattr(ctypes, "get_last_error", None)
    return getter() if getter is not None else 0


def _win_error(code, message):
    factory = getattr(ctypes, "WinError", None)
    if factory is not None:
        return factory(code, message)
    return OSError(code, message)


def _user32():
    try:
        return ctypes.WinDLL("user32", use_last_error=True)
    except Exception:
        diag.exception("win32_embed.user32")
        return None


def configure(user32):
    """Declare argtypes/restype: on 64-bit Windows the default int conversions
    truncate HWND/HANDLE and every call silently fails (release-era bug)."""
    if not user32:
        return
    for name, args, result in (
        ("GetParent", [wintypes.HWND], wintypes.HWND),
        ("IsWindow", [wintypes.HWND], wintypes.BOOL),
        ("IsWindowVisible", [wintypes.HWND], wintypes.BOOL),
        ("SetParent", [wintypes.HWND, wintypes.HWND], wintypes.HWND),
        ("ShowWindow", [wintypes.HWND, ctypes.c_int], wintypes.BOOL),
        ("GetClientRect", [wintypes.HWND, ctypes.POINTER(RECT)], wintypes.BOOL),
        ("GetWindowRect", [wintypes.HWND, ctypes.POINTER(RECT)], wintypes.BOOL),
        ("SetWindowPos", [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL),
        ("MoveWindow", [wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                        ctypes.c_int, wintypes.BOOL], wintypes.BOOL),
        ("EnumWindows", [WNDENUMPROC, wintypes.LPARAM], wintypes.BOOL),
        ("GetClassNameW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        ("GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)],
         wintypes.DWORD),
        ("SetFocus", [wintypes.HWND], wintypes.HWND),
        ("GetFocus", [], wintypes.HWND),
        ("GetForegroundWindow", [], wintypes.HWND),
        ("IsChild", [wintypes.HWND, wintypes.HWND], wintypes.BOOL),
        ("PostMessageW", [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                          wintypes.LPARAM], wintypes.BOOL),
        ("GetAncestor", [wintypes.HWND, wintypes.UINT], wintypes.HWND),
    ):
        try:
            fn = getattr(user32, name)
            fn.argtypes, fn.restype = args, result
        except Exception:
            pass
    for name, args, result in (
        ("SetWindowLongPtrW", [wintypes.HWND, ctypes.c_int, ctypes.c_void_p], ctypes.c_void_p),
        ("GetWindowLongPtrW", [wintypes.HWND, ctypes.c_int], ctypes.c_void_p),
        ("GetDpiForWindow", [wintypes.HWND], wintypes.UINT),
    ):
        try:
            fn = getattr(user32, name)
            fn.argtypes, fn.restype = args, result
        except Exception:
            pass


def _trace_win(name, started, ok, hwnd=0, parent=0, inset=(0, 0, 0, 0), error=None,
               extra=None):
    """Записать вызов Win32 в приборную панель браузера (browser_trace).

    Замер фактического положения делает сам модуль `browser_trace`: по логу
    видно не «вызов отправлен», а «окно оказалось вот здесь». Любая ошибка
    сбора не имеет права ломать работу — поэтому всё завёрнуто в try/except.
    """
    try:
        import browser_trace as _bt
    except Exception:
        return
    try:
        _bt.record_win_call(name, ok, hwnd=hwnd, parent=parent, inset=inset,
                            error=error, seconds=time.monotonic() - started,
                            extra=extra)
    except Exception:
        pass


def get_window_long(user32, hwnd, index):
    getter = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
    _set_last_error(0)
    value = getter(hwnd, index)
    return int(value) & 0xFFFFFFFFFFFFFFFF if value else 0


def set_window_long(user32, hwnd, index, value):
    setter = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
    _set_last_error(0)
    previous = setter(hwnd, index, ctypes.c_void_p(value & 0xFFFFFFFFFFFFFFFF))
    error = _get_last_error()
    diag.event("win32.style", hwnd=int(hwnd), index=index,
               previous=int(previous) & 0xFFFFFFFFFFFFFFFF if previous else 0,
               new=value, error=error)
    if not previous and error:
        raise _win_error(error, 'win32 style change failed')
    return int(previous) & 0xFFFFFFFFFFFFFFFF if previous else 0


def snapshot(user32, hwnd):
    rect = RECT()
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    info = {"hwnd": int(hwnd), "pid": pid.value,
            "visible": bool(user32.IsWindowVisible(hwnd)),
            "rect": [rect.left, rect.top, rect.right, rect.bottom],
            "style": get_window_long(user32, hwnd, GWL_STYLE),
            "exstyle": get_window_long(user32, hwnd, GWL_EXSTYLE),
            "parent": int(user32.GetParent(hwnd) or 0)}
    try:
        info["dpi"] = int(user32.GetDpiForWindow(hwnd))
    except Exception:
        pass
    return info


def class_name(user32, hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def client_size(user32, hwnd):
    rect = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return (0, 0)
    return (rect.right - rect.left, rect.bottom - rect.top)


def eligible(info, name):
    """Only the real app window: never a hidden helper/startup surface."""
    left, top, right, bottom = info["rect"]
    return (name == BROWSER_WINDOW_CLASS and info["visible"] and not info["parent"]
            and right - left >= 120 and bottom - top >= 120)


def _windows_of(user32, pids):
    found = []

    def callback(hwnd, _lparam):
        try:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in pids:
                return True
            name = class_name(user32, hwnd)
            # Шире, чем одна константа: Chrome поднимает Chrome_WidgetWin_0/1/2,
            # и именно «двойка» оставалась висеть в Alt+Tab.
            if not (name == BROWSER_WINDOW_CLASS or name.startswith("Chrome_WidgetWin")):
                return True
            info = snapshot(user32, hwnd)
            info["class"] = name
            found.append(info)
        except Exception:
            diag.exception("win32_embed.enum_callback")
        return True

    _set_last_error(0)
    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


def child_pids(pid):
    """Direct children via Toolhelp32: no wmic, no subprocess, no locale."""
    kernel32 = _kernel32()
    if kernel32 is None:      # не Windows: просто нет детей
        return set()
    try:
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.restype = wintypes.BOOL
    except Exception:
        return set()
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snapshot:
        return set()
    found = set()
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                if int(entry.th32ParentProcessID) == int(pid):
                    found.add(int(entry.th32ProcessID))
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    except Exception:
        diag.exception("win32_embed.child_pids")
    finally:
        try:
            kernel32.CloseHandle(snapshot)
        except Exception:
            pass
    return found


def process_tree(pids):
    return set(int(p) for p in pids or ())


def find_browser_window(user32, pids, timeout=25.0, poll=0.2, extra_pids=None):
    """Wait for the app window instead of racing it.

    Chromium creates its window asynchronously and opens helper surfaces first.
    The release build sampled once and happily embedded a helper, or failed and
    left a detached 900x700 browser behind.
    """
    if not user32:
        return None
    wanted = process_tree(pids)
    deadline = time.monotonic() + max(0.5, timeout)
    best = None
    seen = []
    while time.monotonic() < deadline:
        windows = _windows_of(user32, wanted)
        if windows and not seen:
            seen = windows
        candidates = [w for w in windows if eligible(w, w["class"])]
        if candidates:
            # Largest eligible surface: the app window, not a transient popup.
            best = max(candidates, key=lambda w: (w["rect"][2] - w["rect"][0]) *
                       (w["rect"][3] - w["rect"][1]))["hwnd"]
            break
        if extra_pids:
            wanted |= process_tree(extra_pids())
        time.sleep(poll)
    diag.event("win32.find_window", wanted=sorted(wanted), found=int(best or 0),
               windows=seen[:12])
    return best


def embed(user32, hwnd, parent, inset=(0, 0, 0, 0), cover=0):
    """Attach the browser as a child of the Qt placeholder. Rolls back on any
    failure so the user never ends up with an invisible or floating browser.

    `inset` сразу ставит окно со сдвигом v15: иначе оно на мгновение
    оказывается в (0, 0), а потом переезжает — это видно как рывок.

    `cover` — hwnd шторки приложения (v17). Окно сначала ставится на место
    скрытым, затем шторка поднимается наверх, и только потом окно браузера
    показывается — то есть оно НИКОГДА не оказывается поверх шторки.
    """
    if not user32 or not hwnd or not parent:
        return False
    started = time.monotonic()
    diag.event("embed.before", child=snapshot(user32, hwnd),
               host=snapshot(user32, parent))
    style = get_window_long(user32, hwnd, GWL_STYLE)
    exstyle = get_window_long(user32, hwnd, GWL_EXSTYLE)
    try:
        set_window_long(user32, hwnd, GWL_STYLE,
                        (style & ~(WS_POPUP | WS_CAPTION | WS_SYSMENU | WS_THICKFRAME |
                                   WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_DLGFRAME | WS_BORDER))
                        | WS_CHILD | WS_VISIBLE | WS_CLIPCHILDREN | WS_CLIPSIBLINGS)
        _set_last_error(0)
        user32.SetParent(hwnd, parent)
        error = _get_last_error()
        actual = int(user32.GetParent(hwnd) or 0)
        diag.event("embed.SetParent", error=error, actual=actual, expected=int(parent))
        _trace_win("embed.SetParent", started, actual == int(parent), hwnd=int(hwnd),
                   parent=int(parent), error=error,
                   extra={"parent_actual": actual, "expected": int(parent)})
        if actual != int(parent):
            raise OSError(error, "SetParent did not attach the browser")
        set_window_long(user32, hwnd, GWL_EXSTYLE,
                        (exstyle & ~(WS_EX_DLGMODALFRAME | WS_EX_WINDOWEDGE | WS_EX_CLIENTEDGE |
                                     WS_EX_STATICEDGE | WS_EX_APPWINDOW | WS_EX_NOACTIVATE))
                        | WS_EX_TOOLWINDOW)
        # v17: порядок принципиален. Раньше первым шёл ShowWindow, и окно
        # браузера вставало поверх шторки — на этапе «Ожидание страницы»
        # промелькивали неудалённые элементы. Теперь: ставим на место (sync
        # сам показывает окно, поэтому шторку поднимаем и после него),
        # поднимаем шторку, показываем окно, поднимаем шторку снова.
        sync(user32, hwnd, parent, inset)
        if cover:
            raise_window(user32, cover)
        user32.ShowWindow(hwnd, SW_SHOW)
        if cover:
            raise_window(user32, cover)
        diag.event("embed.after", child=snapshot(user32, hwnd),
                   cover=int(cover or 0))
        _trace_win("embed.done", started, True, hwnd=int(hwnd), parent=int(parent),
                   inset=tuple(inset or ()), error=_get_last_error(),
                   extra={"cover": int(cover or 0),
                          "hosting": "child-of-placeholder"})
        return True
    except Exception:
        diag.exception("win32_embed.embed")
        _trace_win("embed.failed", started, False, hwnd=int(hwnd), parent=int(parent),
                   inset=tuple(inset or ()), error=_get_last_error())
        try:
            # Detach first: a half-attached child is worse than a normal window.
            user32.SetParent(hwnd, None)
            set_window_long(user32, hwnd, GWL_STYLE, style)
            set_window_long(user32, hwnd, GWL_EXSTYLE, exstyle)
            user32.ShowWindow(hwnd, SW_SHOW)
        except Exception:
            diag.exception("win32_embed.rollback")
        return False


def box_for(user32, parent, inset=(0, 0, 0, 0)):
    """(x, y, w, h) окна браузера по размерам плейсхолдера и `inset`.

    `inset` = (left, top, right, bottom) в ФИЗИЧЕСКИХ пикселях плейсхолдера:
    v15 ставит окно браузера со сдвигом, подобранным на Windows
    (BROWSER_DX / BROWSER_DY). Пустой inset — прежнее поведение v13.
    """
    if not user32 or not parent:
        return None
    width, height = client_size(user32, parent)
    if width <= 0 or height <= 0:
        return None
    left, top, right, bottom = (int(v or 0) for v in (tuple(inset) + (0, 0, 0, 0))[:4])
    box_w = width - left - right
    box_h = height - top - bottom
    if box_w <= 0 or box_h <= 0:
        return None
    return (left, top, box_w, box_h)


def sync(user32, hwnd, parent, inset=(0, 0, 0, 0)):
    """Keep the child exactly over the placeholder (honouring `inset`), in the
    placeholder's own client coordinates (child windows are never DPI-virtualised)."""
    if not user32 or not hwnd or not parent:
        return False
    box = box_for(user32, parent, inset)
    if box is None:
        return False
    x, y, width, height = box
    started = time.monotonic()
    _set_last_error(0)
    ok = user32.SetWindowPos(hwnd, None, x, y, width, height,
                             SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW |
                             SWP_FRAMECHANGED | SWP_ASYNCWINDOWPOS)
    error = _get_last_error()
    if not ok:
        diag.event("win32.SetWindowPos.failed", error=error)
    diag.event("win32.sync", hwnd=int(hwnd), wanted=[int(x), int(y), int(width), int(height)],
               ok=bool(ok), error=error, actual=snapshot(user32, hwnd))
    _trace_win("sync", started, bool(ok), hwnd=int(hwnd), parent=int(parent),
               inset=tuple(inset or ()), error=error,
               extra={"wanted": [int(x), int(y), int(width), int(height)]})
    return bool(ok)


def is_window(user32, hwnd):
    try:
        return bool(hwnd and user32.IsWindow(hwnd))
    except Exception:
        return False


def windows_of(user32, pids):
    """Все окна дерева браузера (вспомогательные поверхности тоже)."""
    if not user32:
        return []
    return _windows_of(user32, set(int(p) for p in pids or ()))


# --------------------------------------------------------------- processes --
_kernel32_dll = None


def _kernel32():
    """kernel32 с объявленными прототипами (ReadProcessMemory в том числе)."""
    global _kernel32_dll
    if _kernel32_dll is not None:
        return _kernel32_dll
    try:
        dll = ctypes.WinDLL("kernel32", use_last_error=True)
        dll.GetCurrentProcess.restype = wintypes.HANDLE
        dll.GetCurrentProcess.argtypes = []
        dll.ReadProcessMemory.restype = wintypes.BOOL
        dll.ReadProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPCVOID,
                                          wintypes.LPVOID, ctypes.c_size_t,
                                          ctypes.POINTER(ctypes.c_size_t)]
        _kernel32_dll = dll
    except Exception:
        diag.exception("win32_embed.kernel32")
        _kernel32_dll = False
    return _kernel32_dll or None


def process_names():
    """{pid: 'chrome.exe'} по Toolhelp32: без wmic, без subprocess, без локали."""
    kernel32 = _kernel32()
    if kernel32 is None:
        return {}
    try:
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.restype = wintypes.BOOL
    except Exception:
        return {}
    handle = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not handle:
        return {}
    names = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if kernel32.Process32FirstW(handle, ctypes.byref(entry)):
            while True:
                names[int(entry.th32ProcessID)] = str(entry.szExeFile or "")
                if not kernel32.Process32NextW(handle, ctypes.byref(entry)):
                    break
    except Exception:
        diag.exception("win32_embed.process_names")
    finally:
        try:
            kernel32.CloseHandle(handle)
        except Exception:
            pass
    return names


def descendant_pids(pid):
    """Всё дерево потомков, а не только прямые дети: рендереры, GPU, утилиты."""
    found = set()
    queue = [int(pid)]
    while queue:
        current = queue.pop()
        for child in child_pids(current):
            if child in found or int(child) == int(current):
                continue
            found.add(int(child))
            queue.append(int(child))
    return found


def kill_tree(pids, grace=2.0):
    """Снять ВСЁ дерево процессов браузера, а не только стартовый процесс.

    `proc.terminate()` убивает только родителя: рендереры, GPU и утилиты
    выживают, их окна остаются висеть — из-за этого «плодятся браузеры».
    Ничего чужого не трогаем: только переданные pid.
    """
    kernel32 = _kernel32()
    if kernel32 is None:
        return []
    try:
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
    except Exception:
        diag.exception("win32_embed.kill_tree_decls")
        return []

    def terminate(pid):
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
        if not handle or int(handle) == INVALID_HANDLE_VALUE:
            return False
        try:
            return bool(kernel32.TerminateProcess(handle, 1))
        finally:
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass

    killed = []
    for pid in sorted({int(p) for p in pids or ()}):
        if int(pid) <= 4:
            continue
        try:
            if terminate(pid):
                killed.append(int(pid))
        except Exception:
            diag.exception("win32_embed.terminate")
    if killed:
        diag.event("win32.tree_killed", pids=killed)
    return killed


# ------------------------------------------------------------- taskbar/COM --
class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text):
    """'{{xxxxxxxx-xxxx-...}}' -> GUID (без comtypes и без pywin32)."""
    raw = str(text).strip("{}").split("-")
    value = GUID()
    value.Data1 = int(raw[0], 16)
    value.Data2 = int(raw[1], 16)
    value.Data3 = int(raw[2], 16)
    value.Data4 = (ctypes.c_ubyte * 8)(*bytes.fromhex(raw[3] + raw[4]))
    return value


def _ole32():
    try:
        dll = ctypes.WinDLL("ole32", use_last_error=True)
        dll.CoInitialize.argtypes = [wintypes.LPVOID]
        dll.CoInitialize.restype = ctypes.c_long
        dll.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), wintypes.LPVOID,
                                         wintypes.DWORD, ctypes.POINTER(GUID),
                                         ctypes.POINTER(ctypes.c_void_p)]
        dll.CoCreateInstance.restype = ctypes.c_long
        return dll
    except Exception:
        diag.exception("win32_embed.ole32")
        return None


#: True после первой же ошибки обращения к памяти: COM в этом процессе
#: больше не трогаем, чтобы чужая vtable не уронила всё приложение.
_COM_DISABLED = False

#: ITaskbarList: IUnknown(3) + HrInit, AddTab, DeleteTab, ActivateTab,
#: SetActiveAlt. Читаем и проверяем ВСЕ слоты сразу: в логе «Error - 1»
#: слот 3 (HrInit) оказался пустым — то есть указатель был не на тот объект.
_COM_SLOTS = 8
_ptr_size = ctypes.sizeof(ctypes.c_void_p)


def read_pointer(address):
    """Прочитать один указатель БЕЗ разыменования средствами Python.

    Причина появления этой функции — настоящий краш из «Error - 1»:
    `table[slot]` — это разыменование чужой памяти из Python. Такой доступ НЕ
    защищён ни SEH, ни ctypes: если адрес плохой, процесс падает с «Windows
    fatal exception: access violation» и перехватить это уже нельзя (потому в
    одном логе исключение OSError, а в другом — смерть процесса).

    `ReadProcessMemory` на собственный процесс возвращает FALSE вместо краша:
    плохой указатель превращается в обычный «прочитать не удалось».
    """
    kernel = _kernel32()
    if kernel is None or not address:
        return None
    buffer = ctypes.c_void_p()
    read = ctypes.c_size_t(0)
    try:
        ok = kernel.ReadProcessMemory(kernel.GetCurrentProcess(),
                                      ctypes.c_void_p(int(address)),
                                      ctypes.byref(buffer), _ptr_size,
                                      ctypes.byref(read))
    except Exception:
        diag.exception("win32_embed.read_pointer")
        return None
    if not ok or int(read.value or 0) != _ptr_size:
        return None
    return int(buffer.value or 0)


def com_vtable(interface, slots=_COM_SLOTS):
    """Прочитать `slots` первых указателей vtable. None — читать нельзя/нечего."""
    if not interface:
        return None
    table = read_pointer(interface)
    if not table:
        diag.event("win32.com_abort", reason="vtable_pointer_unreadable")
        return None
    result = []
    for index in range(int(slots)):
        address = read_pointer(table + index * _ptr_size)
        if not address:
            diag.event("win32.com_abort", reason="empty_slot", slot=int(index))
            return None
        result.append(address)
    # У живого COM-объекта слоты разные: совпадение означает мусор.
    if len(set(result)) != len(result):
        diag.event("win32.com_abort", reason="duplicate_slots")
        return None
    return result


def _com_call(interface, slot, *args):
    """Вызов слота vtable COM-интерфейса. Вне Windows всегда None.

    Адрес ОБЯЗАН быть целым: `proto(c_void_p)` — это TypeError, а попытка
    вызвать «функцию» по мусорному адресу — access violation (именно это и
    произошло в первой v15). v19: сначала БЕЗОПАСНОЕ чтение vtable
    (`com_vtable`), и только потом вызов — тогда даже «кривой» указатель не
    убивает процесс, а просто отключает COM (см. `read_pointer`).
    """
    global _COM_DISABLED
    if _COM_DISABLED or not interface:
        return None
    slot = int(slot)
    slots = com_vtable(interface)
    if not slots or slot >= len(slots):
        diag.event("win32.com_abort", reason="slot_out_of_range", slot=slot)
        return None
    address = int(slots[slot] or 0)
    if not address:
        diag.event("win32.com_empty_slot", slot=slot)
        return None
    try:
        proto = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p,
                                   *([wintypes.HWND] * len(args)))
        fn = proto(address)
        hr = int(fn(ctypes.c_void_p(int(interface)), *args))
        diag.event("win32.com_call", slot=slot, hr=hr)
        return hr
    except OSError:
        # Доступ к чужой памяти: один раз достаточно, дальше без COM.
        _COM_DISABLED = True
        diag.exception("win32_embed.com_call_av")
        return None
    except Exception:
        diag.exception("win32_embed.com_call")
        return None


def taskbar_delete_tab(hwnd):
    """Убрать кнопку окна из панели задач: ITaskbarList::DeleteTab.

    Именно это и нужно в v15: стиля `WS_EX_TOOLWINDOW` недостаточно, потому
    что Chrome сам возвращает свою кнопку через `ITaskbarList::AddTab`.

    v19: успех — ЭТО ФАКТ, а не надежда. Раньше `win32.taskbar_deleted`
    писался и тогда, когда вызов упал с access violation (лог «Error - 1»:
    `com_empty_slot 3` → `exception OSError` → `taskbar_deleted` = успех).
    Теперь пишется либо `taskbar_deleted` с настоящим HRESULT, либо
    `taskbar_failed`, а вызывающий получает честный False.
    """
    global _COM_DISABLED
    if not hwnd or _COM_DISABLED:
        return False
    ole32 = _ole32()
    if ole32 is None:
        return False
    pointer = ctypes.c_void_p()
    try:
        try:
            hr_ole = int(ole32.CoInitialize(None))
            if hr_ole < 0:
                diag.event("win32.com_coinit", hr=hr_ole)
        except Exception:
            diag.exception("win32_embed.coinitialize")
        hr = int(ole32.CoCreateInstance(ctypes.byref(_guid(CLSID_TASKBAR_LIST)), None,
                                        CLSCTX_ALL,
                                        ctypes.byref(_guid(IID_ITASKBAR_LIST)),
                                        ctypes.byref(pointer)))
        if hr < 0 or not pointer.value:
            diag.event("win32.taskbar.create_failed", hr=hr)
            return False
        # Проверяем объект ДО любого вызова: в логе «Error - 1» слот 3 был
        # пустым, а вызов по соседнему слоту уронил процесс.
        if com_vtable(pointer.value) is None:
            _COM_DISABLED = True
            diag.event("win32.taskbar_abort", hwnd=int(hwnd),
                       reason="vtable_unreadable")
            return False
        hr_init = _com_call(pointer.value, _COM_HR_INIT)
        if hr_init is not None and hr_init < 0:
            diag.event("win32.taskbar_hrinit_failed", hr=int(hr_init))
            return False
        hr_delete = _com_call(pointer.value, _COM_DELETE_TAB, wintypes.HWND(int(hwnd)))
        if hr_delete is None or int(hr_delete) < 0:
            diag.event("win32.taskbar_failed", hwnd=int(hwnd), hr=hr_delete)
            return False
        diag.event("win32.taskbar_deleted", hwnd=int(hwnd), hr=int(hr_delete))
        return True
    except Exception:
        diag.exception("win32_embed.taskbar")
        return False
    finally:
        if pointer.value:
            _com_call(pointer.value, _COM_RELEASE)


def hide_from_taskbar(user32, hwnd, delete_tab=True):
    """Окно не должно появляться в панели задач: стиль + DeleteTab.

    `delete_tab=False` — только стиль (если COM в этом процессе отключён).
    """
    if not user32 or not hwnd:
        return False
    started = time.monotonic()
    styled = False
    try:
        style = get_window_long(user32, hwnd, GWL_EXSTYLE)
        wanted = (style & ~(WS_EX_APPWINDOW | WS_EX_NOACTIVATE)) | WS_EX_TOOLWINDOW
        if wanted != style:
            set_window_long(user32, hwnd, GWL_EXSTYLE, wanted)
        user32.SetWindowPos(wintypes.HWND(int(hwnd)), None, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER |
                            SWP_NOACTIVATE | SWP_FRAMECHANGED)
        styled = True
    except Exception:
        diag.exception("win32_embed.taskbar_style")
    deleted = taskbar_delete_tab(hwnd) if delete_tab else False
    diag.event("win32.taskbar_hidden", hwnd=int(hwnd), style=styled,
               delete_tab=bool(deleted))
    _trace_win("hide_from_taskbar", started, bool(styled or deleted), hwnd=int(hwnd),
               extra={"style": styled, "delete_tab": bool(deleted)})
    return bool(styled or deleted)


def raise_window(user32, hwnd):
    """Поднять окно наверх z-порядка внутри родителя (v17).

    Шторка приложения и окно браузера — оба настоящие HWND-окна внутри
    плейсхолдера. После `SetParent` новое дочернее окно встаёт ПОВЕРХ всех
    соседей, поэтому одного `QWidget.raise_()` мало: пока шторку не поднять
    через `SetWindowPos(HWND_TOP)`, виден «сырой» браузер с неудалёнными
    элементами. Именно это и промелькнуло на этапе «Ожидание страницы».
    """
    if not user32 or not hwnd:
        return False
    try:
        started = time.monotonic()
        _set_last_error(0)
        ok = user32.SetWindowPos(wintypes.HWND(int(hwnd)),
                                 wintypes.HWND(HWND_TOP),
                                 0, 0, 0, 0,
                                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        error = _get_last_error()
        if not ok:
            diag.event("win32.SetWindowPos.failed", error=error)
        _trace_win("raise_window", started, bool(ok), hwnd=int(hwnd), error=error)
        return bool(ok)
    except Exception:
        diag.exception("win32_embed.raise_window")
        return False


def show_window(user32, hwnd):
    """Показать окно без активации (v18), с проверкой результата (v19).

    После скрытия родительского окна дочернее окно браузера скрывается вместе
    с ним; один `SetParent`-синхронизации недостаточно — Chromium успевает
    приостановить отрисовку, и на экране остаётся чёрный прямоугольник.

    v19: результат проверяется по `IsWindowVisible`, а не по возврату
    `ShowWindow` — она отвечает «окно было видно раньше», а не «видно сейчас».
    """
    if not user32 or not hwnd:
        return False
    try:
        started = time.monotonic()
        _set_last_error(0)
        was = bool(user32.ShowWindow(wintypes.HWND(int(hwnd)), SW_SHOWNA))
        visible = bool(user32.IsWindowVisible(wintypes.HWND(int(hwnd))))
        error = _get_last_error()
        diag.event("win32.show_window", hwnd=int(hwnd), was_visible=was,
                   visible=visible, error=error)
        _trace_win("show_window", started, visible, hwnd=int(hwnd), error=error,
                   extra={"was_visible": was, "visible": visible})
        return visible
    except Exception:
        diag.exception("win32_embed.show_window")
        return False


def invalidate(user32, hwnd):
    """Перерисовать окно: InvalidateRect + UpdateWindow (v18)."""
    if not user32 or not hwnd:
        return False
    try:
        started = time.monotonic()
        handle = wintypes.HWND(int(hwnd))
        user32.InvalidateRect(handle, None, True)
        user32.UpdateWindow(handle)
        _trace_win("invalidate", started, True, hwnd=int(hwnd))
        return True
    except Exception:
        diag.exception("win32_embed.invalidate")
        return False


def sync_now(user32, hwnd, parent, inset=(0, 0, 0, 0)):
    """То же, что `sync`, но СИНХРОННО — без `SWP_ASYNCWINDOWPOS` (v18.1).

    С асинхронным флагом система лишь ОТПРАВЛЯЕТ запрос в поток Chrome и не
    ждёт его: после показа скрытого окна браузер ещё секунду-две рисуется на
    старом месте, и содержимое выглядит съехавшим. Здесь вызов возвращается
    только когда перемещение применено, поэтому съехавший кадр не успевает
    попасть на экран. Используется редко (только при показе окна), поэтому
    ожидание потока Chrome здесь безопаснее, чем в общем `sync`.
    """
    if not user32 or not hwnd or not parent:
        return False
    box = box_for(user32, parent, inset)
    if box is None:
        return False
    x, y, width, height = box
    started = time.monotonic()
    _set_last_error(0)
    ok = user32.SetWindowPos(hwnd, None, x, y, width, height,
                             SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW |
                             SWP_FRAMECHANGED)
    error = _get_last_error()
    if not ok:
        diag.event("win32.SetWindowPosSync.failed", error=error)
    diag.event("win32.sync_now", hwnd=int(hwnd),
               wanted=[int(x), int(y), int(width), int(height)], ok=bool(ok),
               error=error, actual=snapshot(user32, hwnd))
    _trace_win("sync_now", started, bool(ok), hwnd=int(hwnd), parent=int(parent),
               inset=tuple(inset or ()), error=error,
               extra={"wanted": [int(x), int(y), int(width), int(height)],
                      "sync": True})
    return bool(ok)


def hide_window(user32, hwnd):
    """Скрыть окно целиком: не видно ни на экране, ни в Alt+Tab."""
    if not user32 or not hwnd:
        return False
    try:
        started = time.monotonic()
        _set_last_error(0)
        was = bool(user32.ShowWindow(wintypes.HWND(int(hwnd)), SW_HIDE))
        visible = bool(user32.IsWindowVisible(wintypes.HWND(int(hwnd))))
        error = _get_last_error()
        diag.event("win32.hide_window", hwnd=int(hwnd), was_visible=was,
                   visible=visible, error=error)
        _trace_win("hide_window", started, not visible, hwnd=int(hwnd), error=error,
                   extra={"visible": visible})
        return not visible
    except Exception:
        diag.exception("win32_embed.hide_window")
        return False


def hide_stray_windows(user32, pids, keep=0, close_after_hide=False,
                       delete_tab=True):
    """Скрыть ВСЕ окна дерева браузера, кроме встроенного (`keep`).

    Chrome поднимает вспомогательные поверхности, и именно они копятся в панели
    задач и в Alt+Tab после повторов запуска. Встроенное окно не трогаем.

    Сначала окно скрывается (пользователь не видит его нигде и сразу), и
    только если это настоящее лишнее окно приложения — ему посылается
    WM_CLOSE. Служебные окна Chrome (`Chrome_WidgetWin_0`) закрывать нельзя:
    это опорное окно браузера, закрытие убивает весь процесс.
    """
    if not user32:
        return []
    hidden = []
    closed = []
    for info in windows_of(user32, pids):
        hwnd = int(info.get("hwnd") or 0)
        if not hwnd or hwnd == int(keep or 0) or not info.get("visible"):
            continue
        try:
            user32.ShowWindow(wintypes.HWND(hwnd), SW_HIDE)
        except Exception:
            diag.exception("win32_embed.hide_stray")
            continue
        hide_from_taskbar(user32, hwnd, delete_tab=delete_tab)
        hidden.append(hwnd)
        if close_after_hide and info.get("class") == BROWSER_WINDOW_CLASS:
            try:
                if user32.PostMessageW(wintypes.HWND(hwnd), WM_CLOSE, 0, 0):
                    closed.append(hwnd)
            except Exception:
                diag.exception("win32_embed.close_stray")
    if hidden:
        diag.event("win32.stray_hidden", hidden=hidden, closed=closed,
                   keep=int(keep or 0))
    return hidden
