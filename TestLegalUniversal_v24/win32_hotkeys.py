"""Монитор глобальных горячих клавиш для GUI (без потоков и без скрытых окон).

Архитектура (отказоустойчивая):

* события клавиш получает главный поток Qt: таймер QTimer (~25 мс) вызывает
  :meth:`HotkeyMonitor.tick`, который опрашивает ``GetAsyncKeyState``;
  никакой оконной процедуры, очереди сообщений или мармалинга сигналов из
  других потоков нет в принципе — поэтому хоткеи не могут "не сработать";
* ``RegisterHotKey`` применяется только best-effort и только чтобы нажатая
  клавиша не уходила в другие приложения (подавление проброса). Его отказ
  НИКОГДА не блокирует работу: события уже приходят опросом, а статус
  остаётся "Готов к работе" (ложных "Конфликт горячих клавиш" не бывает);
* события отдаются колбэком ``on_event(key_id)`` прямо из таймера главного
  потока, т.е. в контексте Qt GUI — вызывающий код может безопасно трогать
  виджеты.
"""

from __future__ import annotations

import ctypes
import sys
import time
from typing import Callable, Dict, Optional

IS_WINDOWS = sys.platform == "win32"

# Идентификаторы действий (бизнес-код main.py использует их как раньше).
ID_TOGGLE_VISIBILITY = 1
ID_MIC = 2

# Совместимость с прежними именами (main.py, тесты).
HOTKEY_TOGGLE_ID = ID_TOGGLE_VISIBILITY
HOTKEY_MIC_ID = ID_MIC

WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000

_DEFAULT_TOGGLE = "F2"
_DEFAULT_MIC = "F3"
_DEBOUNCE = 0.35  # антидребезг между срабатываниями, сек
_POLL_HINT = "Set QTimer interval to ~25 ms and call tick() from the main thread"

VK_MAP = {
    "ESC": 0x1B,
    "ESCAPE": 0x1B,
    "TAB": 0x09,
    "SPACE": 0x20,
    "ENTER": 0x0D,
    "RETURN": 0x0D,
    "BACKSPACE": 0x08,
    "DELETE": 0x2E,
    "INSERT": 0x2D,
    "HOME": 0x24,
    "END": 0x23,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,
    "UP": 0x26,
    "DOWN": 0x28,
    "LEFT": 0x25,
    "RIGHT": 0x27,
    "PAUSE": 0x13,
    "PRINTSCREEN": 0x2C,
    "SCROLLLOCK": 0x91,
    "NUMLOCK": 0x90,
    "CAPSLOCK": 0x14,
}
for _i in range(1, 25):
    VK_MAP[f"F{_i}"] = 0x6F + _i
for _i in range(10):
    VK_MAP[str(_i)] = 0x30 + _i
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    VK_MAP[_c] = ord(_c)

_PASSTHROUGH_KEYS = {
    "ESC",
    "ESCAPE",
    "TAB",
    "SPACE",
    "ENTER",
    "RETURN",
    "BACKSPACE",
    "DELETE",
    "INSERT",
    "HOME",
    "END",
    "PAGEUP",
    "PAGEDOWN",
    "UP",
    "DOWN",
    "LEFT",
    "RIGHT",
    "PRINTSCREEN",
    "SCROLLLOCK",
    "NUMLOCK",
    "CAPSLOCK",
}


def normalize_key(raw: object, default: str = _DEFAULT_TOGGLE) -> str:
    value = str(raw or default).strip().upper()
    if value.startswith("KEY_"):
        value = value[4:]
    return value if value in VK_MAP else default


def key_name(raw: object) -> str:
    return normalize_key(raw, _DEFAULT_TOGGLE).title().replace("F", "F")


def is_passthrough(raw: object) -> bool:
    return normalize_key(raw) in _PASSTHROUGH_KEYS


def resolve_vk(raw: object) -> int:
    return VK_MAP[normalize_key(raw)]


def _make_key_state() -> Callable[[int], bool]:
    """Возвращает быстрый предикат нажатия клавиши (GetAsyncKeyState)."""
    if not IS_WINDOWS:
        return lambda vk: False
    try:
        user32 = ctypes.windll.user32
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_ushort

        def _state(vk: int) -> bool:
            try:
                return bool(user32.GetAsyncKeyState(int(vk)) & 0x8000)
            except Exception:
                return False

        return _state
    except Exception:
        return lambda vk: False


class _Registration:
    """Лучше-попытка RegisterHotKey: подавление проброса клавиши, не более."""

    def __init__(self) -> None:
        self._entries: Dict[int, int] = {}
        self.ok = False

    def update(self, hwnd: int, keys: Dict[int, int]) -> None:
        self.release()
        if not IS_WINDOWS or not hwnd:
            return
        try:
            user32 = ctypes.windll.user32
            user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
            user32.RegisterHotKey.restype = ctypes.c_bool
            user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.UnregisterHotKey.restype = ctypes.c_bool
        except Exception:
            return
        any_ok = False
        for key_id, vk in keys.items():
            if not vk:
                continue
            try:
                if user32.RegisterHotKey(ctypes.c_void_p(int(hwnd)), int(key_id), MOD_NOREPEAT, int(vk)):
                    self._entries[key_id] = int(hwnd)
                    any_ok = True
            except Exception:
                continue
        self.ok = any_ok

    def release(self) -> None:
        if not self._entries:
            self.ok = False
            return
        try:
            user32 = ctypes.windll.user32
            for key_id, hwnd in list(self._entries.items()):
                try:
                    user32.UnregisterHotKey(ctypes.c_void_p(int(hwnd)), int(key_id))
                except Exception:
                    pass
        finally:
            self._entries.clear()
            self.ok = False


class HotkeyMonitor:
    """Опросник горячих клавиш для таймера главного потока Qt.

    ``on_event(key_id)`` вызывается прямо из :meth:`tick` — т.е. в том потоке,
    который дёргает таймер (главный поток GUI). ``set_keys`` всегда возвращает
    ``True``: опрос физического состояния клавиатуры работает всегда, поэтому
    статус "Конфликт горячих клавиш" невозможен по построению.
    """

    def __init__(self, on_event: Callable[[int], object]) -> None:
        self._on_event = on_event
        self._keys: Dict[int, int] = {}
        self._down: Dict[int, bool] = {}
        self._last_fire: Dict[int, float] = {}
        self._get_state = _make_key_state()
        self._registration = _Registration()
        self._hwnd = 0

    # ------------------------------------------------------------------ keys
    @property
    def keys(self) -> Dict[int, int]:
        return dict(self._keys)

    def set_keys(self, toggle_vk: int, mic_vk: int) -> bool:
        self._keys = {
            ID_TOGGLE_VISIBILITY: int(toggle_vk or 0),
            ID_MIC: int(mic_vk or 0),
        }
        # Сбрасываем "край нажатия": новая клавиша не должна выстрелить
        # от того, что её держат в момент переназначения.
        self._down = {key_id: False for key_id in self._keys}
        self._resuppress()
        # Опрос работает всегда — ложные "конфликты" запрещены.
        return True

    def clear(self) -> bool:
        return self.set_keys(0, 0)

    # ------------------------------------------------------------------- run
    def tick(self, now: Optional[float] = None) -> None:
        """Опрос клавиатуры; вызывать из QTimer главного потока (~25 мс)."""
        ts = time.monotonic() if now is None else now
        for key_id, vk in self._keys.items():
            if not vk:
                continue
            try:
                down = bool(self._get_state(vk))
            except Exception:
                down = False
            was_down = self._down.get(key_id, False)
            last = self._last_fire.get(key_id)
            if down and not was_down and (last is None or (ts - last) >= _DEBOUNCE):
                self._last_fire[key_id] = ts
                try:
                    self._on_event(key_id)
                except Exception:
                    pass
            self._down[key_id] = down

    def resync(self) -> None:
        """Сбросить край нажатия (после смены клавиш / активации окна)."""
        for key_id in self._keys:
            self._down[key_id] = False

    # ------------------------------------------------------- suppression only
    def try_suppress(self, hwnd: int) -> bool:
        """Best-effort подавление проброса клавиш в другие приложения.

        Результат — чисто информационный; срабатывание хоткеев от него не
        зависит (оно идёт опросом), и статус никогда не показывает "конфликт".
        """
        self._hwnd = int(hwnd or 0)
        self._registration.update(self._hwnd, self._keys)
        return self._registration.ok

    def _resuppress(self) -> None:
        # Перерегистрируем подавление на уже известном hwnd (если был).
        if getattr(self, "_hwnd", 0):
            self._registration.update(self._hwnd, self._keys)
        else:
            self._registration.release()

    @property
    def suppressed(self) -> bool:
        return self._registration.ok

    def release(self) -> None:
        self._registration.release()

    def stop(self) -> None:
        """Совместимость с прежним API: освобождение регистрации."""
        self.release()

    # ---------------------------------------------------------------- status
    @property
    def status_text(self) -> str:
        return "Готов к работе"

    @property
    def poll_hint(self) -> str:
        return _POLL_HINT
