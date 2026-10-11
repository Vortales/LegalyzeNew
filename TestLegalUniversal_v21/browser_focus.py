"""Native keyboard-focus handoff for the embedded Chromium child.

The browser is another process with its own input queue: a click inside it does
not automatically move keyboard focus across processes. This module only hands
focus to windows that belong to our own child, and only while our window is
foreground — no hooks, no key capture, no synthetic typing.

No keyboard hook, text capture, or synthetic key forwarding. Inspect only mouse
button state/window handles; join input queues only for the duration of SetFocus.
"""
import ctypes
from ctypes import wintypes
import diagnostics as diag


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('flags', wintypes.DWORD),
                ('hwndActive', wintypes.HWND), ('hwndFocus', wintypes.HWND),
                ('hwndCapture', wintypes.HWND), ('hwndMenuOwner', wintypes.HWND),
                ('hwndMoveSize', wintypes.HWND), ('hwndCaret', wintypes.HWND),
                ('rcCaret', wintypes.RECT)]


def configure(u, k):
    if not u:
        return
    for name, args, result in (
        ('GetForegroundWindow', [], wintypes.HWND),
        ('GetFocus', [], wintypes.HWND),
        ('SetFocus', [wintypes.HWND], wintypes.HWND),
        ('GetCursorPos', [ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
        ('WindowFromPoint', [wintypes.POINT], wintypes.HWND),
        ('IsChild', [wintypes.HWND, wintypes.HWND], wintypes.BOOL),
        ('GetAsyncKeyState', [ctypes.c_int], ctypes.c_short),
        ('AttachThreadInput', [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL], wintypes.BOOL),
        ('GetGUIThreadInfo', [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)], wintypes.BOOL),
    ):
        fn = getattr(u, name)
        fn.argtypes, fn.restype = args, result
    k.GetCurrentThreadId.argtypes, k.GetCurrentThreadId.restype = [], wintypes.DWORD


def belongs(u, root, hwnd):
    return bool(root and hwnd and (int(root) == int(hwnd) or u.IsChild(root, hwnd)))


def thread_focus(u, tid):
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(info)
    if not u.GetGUIThreadInfo(tid, ctypes.byref(info)):
        return {'error': ctypes.get_last_error()}
    return {'active': int(info.hwndActive or 0), 'focus': int(info.hwndFocus or 0),
            'caret': int(info.hwndCaret or 0)}


def handoff(u, k, host, browser, target):
    """Focus only our own child, only while our window/browser is foreground."""
    foreground = u.GetForegroundWindow()
    if not (belongs(u, host, foreground) or belongs(u, browser, foreground)):
        diag.event("focus.skipped", reason="foreground_not_owned")
        return False
    if not u.IsWindow(browser) or not belongs(u, browser, target):
        return False
    if belongs(u, browser, thread_focus(u, 0).get('focus', 0)):
        diag.event('focus.already_owned', browser=int(browser))
        return True
    current_tid = k.GetCurrentThreadId()
    target_tid = u.GetWindowThreadProcessId(target, None)
    if not target_tid:
        return False
    before = thread_focus(u, target_tid)
    attached = False
    try:
        if target_tid != current_tid:
            ctypes.set_last_error(0)
            attached = bool(u.AttachThreadInput(current_tid, target_tid, True))
            if not attached:
                diag.event('focus.attach_failed', error=ctypes.get_last_error(), target_tid=target_tid)
                return False
        ctypes.set_last_error(0)
        previous = u.SetFocus(target)
        error = ctypes.get_last_error()
        after = thread_focus(u, target_tid)
        ok = belongs(u, browser, after.get('focus', 0))
        diag.event('focus.handoff', host=int(host), browser=int(browser), target=int(target),
                   target_tid=target_tid, previous=int(previous or 0), error=error,
                   before=before, after=after, success=ok)
        return ok
    finally:
        if attached:
            ctypes.set_last_error(0)
            ok = bool(u.AttachThreadInput(current_tid, target_tid, False))
            diag.event('focus.detach', success=ok, error=ctypes.get_last_error(),
                       after=thread_focus(u, target_tid))


class BrowserFocusBridge:
    def __init__(self, u, k):
        self.u, self.k = u, k
        self._left_down = False

    def poll(self, host, browser, enabled):
        """Called by a GUI timer, never steals focus when clicking Qt controls/dialogs."""
        if not self.u:
            return
        down = bool(self.u.GetAsyncKeyState(0x01) & 0x8000)  # mouse button only
        pressed = down and not self._left_down
        self._left_down = down
        if not (pressed and enabled and browser):
            return
        point = wintypes.POINT()
        if not self.u.GetCursorPos(ctypes.byref(point)):
            return
        target = self.u.WindowFromPoint(point)
        if belongs(self.u, browser, target):
            handoff(self.u, self.k, host, browser, target)
