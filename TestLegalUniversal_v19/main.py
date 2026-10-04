"""Legalyze Win10/Win11 diagnostic client.
See README.md for isolation, known causes, logging and Windows validation steps.
The production client directory is deliberately untouched.
"""
import diagnostics as diag
diag.setup()
from diagnostics import stage
from qt_browser import BrowserPane, DEBUG_PORT, READY_SCRIPT
import storage_paths
# v20: приборная панель браузера. Импорт — сразу после `diagnostics.setup()`:
# ниже, рядом с `diag.install_http()`, модуль уже включается в работу. Собирает
# то, чего не было в логе: «куда просили поставить окно, где оно оказалось и
# почему разошлось», хронологию «съехало — вернулось» в миллисекундах и
# доказательства экспорта файлов.
import browser_trace as btrace

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import ctypes
import threading
import hashlib
import math
import base64
import requests
from ctypes import wintypes
from pathlib import Path

from websocket import create_connection
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QCheckBox, QDialog, QComboBox,
    QFormLayout, QMessageBox, QInputDialog, QGraphicsDropShadowEffect,
    QSizePolicy, QProgressDialog,
)
from PyQt6.QtCore import (QTimer, QThread, pyqtSignal, Qt, QPoint, QRectF, QLockFile, pyqtSlot,
                          QObject, QEvent)
from PyQt6.QtGui import (
    QFont, QPainter, QColor, QPen, QBrush, QPainterPath, QKeySequence,
)

from config import (
    load_config, save_config, get_template_filled,
    SERVER_URL, CURRENT_VERSION, APP_DIR, DATA_DIR, PROMPTS_DIR,
    TEMPLATE_FILE, normalize_template,
    ensure_template_file, write_template_file, save_template,
)
from crypto_utils import decrypt_data_auto
from hwid_gen import generate_hwid
from updater import check_for_update, restart_app

from secure_store import (
    SecureWorkspace, PromptCacheManifest,
    encrypt_secret, decrypt_secret,
    encrypt_blob, decrypt_blob,
    purge_plaintext_artifacts, shred_file,
    sha256_bytes, template_hash,
)

# FPDF нужен только как legacy-фолбэк для старых текстовых .enc (ТЗ 1.3).
try:
    from fpdf import FPDF
except Exception:
    diag.exception("main.py:65")
    FPDF = None


diag.install_http()
# Сбор поведения браузера: та же папка сессии, что у диагностики. Отказ сбора
# (нет прав на запись) не мешает приложению работать — он просто выключится.
BROWSER_TRACE_ON = bool(btrace.install())

URL = "https://google.com/ai"
PROFILE = APP_DIR / "qtwebengine-profile"
# Профиль НАСТОЯЩЕГО браузера: отдельный и постоянный (согласие/вход — один раз).
BROWSER_PROFILE = APP_DIR / "browser-profile"






X, Y, W, H = 1426, 200, 453, 735

# ============================================================================
# НАСТРОЙКИ РАСПОЛОЖЕНИЯ ОБЪЕКТОВ — правьте числа ТОЛЬКО здесь и перезапускайте
# ============================================================================
# Значения НЕ угаданы: сдвиг окна подобран на Windows в измерительной сборке
# pre14 (режим администратора). Остальное — те же ползунки, но числами.

# --- окно браузера внутри окна программы, DIP (сдвиг и изменение размера) ---
BROWSER_DX = -10     # сдвиг окна браузера по X: -10 = влево, +10 = вправо
BROWSER_DY = -24     # сдвиг окна браузера по Y: -20 = вверх, +20 = вниз
BROWSER_DW = 0       # изменить ширину окна браузера (0 = не менять)
BROWSER_DH = 0       # изменить высоту окна браузера (0 = не менять)

# --- раскладка страницы ---
PAGE_ZOOM = 0.667    # нативный зум страницы (0.667 = 67 %)
WIDTH_DELTA = 0      # поправка ширины раскладки, CSS px
HEIGHT_DELTA = 0     # поправка высоты раскладки, CSS px

# --- сдвиг содержимого страницы, CSS px (0 = страницу не трогаем вообще) ---
PAGE_OFFSET_X = 0    # сдвинуть всю страницу вправо/влево
PAGE_OFFSET_Y = 0    # сдвинуть всю страницу вверх/вниз
PAD_BOTTOM = 0       # добавить отступ снизу страницы
SCROLL_X = 0         # прокрутка сразу после загрузки
SCROLL_Y = 0

# --- сдвиг ТОЛЬКО поля ввода, CSS px ---
INPUT_DX = 0
INPUT_DY = 0
INPUT_SELECTOR = ('textarea, div[contenteditable="true"], rich-textarea, '
                  '[role="textbox"], input[type="text"]')

# --- речевой ввод и внешний вид страницы (v18) ---
SPEECH_LANG = "ru-RU"     # язык распознавания речи. Приложение русское,
                          # поэтому русский имеет ПРИОРИТЕТ над языком Windows:
                          # у пользователя из США распознавание всё равно будет
                          # по-русски. Изменить можно и в config.json
                          # (ключ speech_lang).
ACCEPT_LANGUAGES = ""     # Accept-Language профиля. Пусто = "<язык>,<язык>,en".
FORCE_LIGHT_THEME = True  # светлая тема страницы. На Windows с тёмной темой
                          # Chrome иначе рисует чат тёмным (у разных
                          # пользователей получалось по-разному).
AUTO_INSTALL_BROWSER = True  # если настоящего Chrome нет — предложить скачать
                          # официальный Chrome for Testing (это и есть
                          # «установка речевого ввода»: в этом браузере
                          # работает ШТАТНЫЙ микрофон страницы, и никакие
                          # речевые пакеты Windows не нужны).
REVIVE_AFTER_SHOW = True  # «оживлять» браузер после сворачивания/разворачивания
                          # окна: иначе вместо страницы остаётся чёрный фон.
REVIVE_STEPS = (0, 150, 350, 600, 900, 1300, 1800, 2400)  # мс: Chromium
                          # применяет перенос окна и перерисовку не мгновенно.
                          # Последний шаг — жёсткий предел: GUI прогрузки
                          # снимается в любом случае.
REVIVE_SETTLE_N = 3       # сколько ОДИНАКОВЫХ измерений подряд считать, что
                          # браузер устоялся (innerWidth, dpr, размер окна)
REVIVE_MIN_MS = 1600      # раньше этого срока GUI прогрузки НЕ снимаем даже
                          # при совпавших измерениях: возмущение у Chromium
                          # начинается позже первых кадров (по приёмке — около
                          # двух секунд), поэтому «три одинаковых кадра на
                          # 300-й миллисекунде» ещё ни о чём не говорят.
                          # 1600 мс (v19): 1200 оказалось мало на машинах,
                          # где объекты встают на место дольше.
REVIVE_REVEAL_GATE_MS = 700  # v19: перед тем как снять GUI прогрузки, ждём
                          # `readyState == "complete"` — но не дольше этого
                          # времени, чтобы индикатор не залип навсегда.
REVIVE_FINAL_MS = 180     # пауза после финальной правки: кадр должен лечь
REVIVE_TITLE = "Подготовка страницы"      # тот же индикатор, что на старте
REVIVE_MESSAGE = "Пожалуйста, подождите…"

# --- определение и исправление поверхности страницы (v19) ---
SURFACE_READY_TIMEOUT = 90    # сколько максимум ждать настоящей готовности чата
SURFACE_STABLE_ROUNDS = 2     # сколько ОДИНАКОВЫХ замеров подряд считать
                              # страницу устоявшейся (готовность = «complete»
                              # плюс поле ввода плюс совпавший замер)
SURFACE_POLL_MS = 500         # период опроса состояния страницы
THEME_MAX_REPAIRS = 4         # сколько раз подряд приводить тему к светлой,
                              # если страница всё равно тёмная
UPLOAD_BLOCK_THRESHOLD = 6    # столько пустых попыток прикрепить файлы подряд
                              # достаточно, чтобы считать: на ЭТОЙ странице
                              # вложение не работает
UPLOAD_BLOCK_LOG_EVERY = 15.0  # и не чаще раза в столько секунд писать в лог
AUTO_CORRECT_SURFACE = True   # если вложение не работает — открыть рабочую
                              # страницу самим (причина «Error - 1»: сервис
                              # отдал НЕ ту поверхность, на которой есть чат)
SURFACE_FALLBACK_URL = "https://gemini.google.com/app"  # чат, под который
                              # написана автоматизация (поле ввода, «плюс»,
                              # прикреплённые файлы)

# --- поведение лишних окон браузера ---
CLOSE_STRAY_WINDOWS = True    # после скрытия закрывать лишнее окно приложения
TASKBAR_DELETE_TAB = True     # доп. снимать кнопку через ITaskbarList.
                              # Если COM хоть раз упадёт, он отключится сам,
                              # а стиль WS_EX_TOOLWINDOW продолжит работать.
                              # Поставьте False, чтобы не звать COM вообще.
# =========================== конец блока настроек ============================

# Журнал pid своего браузера: по нему добиваемся то, что осталось от прошлого
# запуска (иначе его окно висит в панели задач и мешает синглтону профиля).
BROWSER_PIDS_FILE = APP_DIR / "browser-pids.json"

PAGE_TUNE_JS = """
(function(){
  try {
    var D = document, id = 'legalyze-tune-css';
    var s = D.getElementById(id);
    var offX = %d, offY = %d, padB = %d, inX = %d, inY = %d;
    var sel = %s;
    if (!offX && !offY && !padB && !inX && !inY) {
      if (s && s.parentNode) { s.parentNode.removeChild(s); }
      return {ok: true, removed: true};
    }
    if (!s) {
      s = D.createElement('style');
      s.id = id;
      (D.head || D.documentElement).appendChild(s);
    }
    var css = '';
    if (offX || offY) {
      css += 'html, body { transform: translate(' + offX + 'px, ' + offY + 'px); }';
    }
    if (padB) {
      css += 'html, body { padding-bottom: ' + padB + 'px !important; }';
    }
    if (inX || inY) {
      css += sel + ' { transform: translate(' + inX + 'px, ' + inY + 'px) !important; }';
    }
    s.textContent = css;
    var out = {ok: true, len: css.length};
    if (%d || %d) {
      window.scrollTo(%d, %d);
      out.scrolled = true;
    }
    return out;
  } catch (e) {
    return {ok: false, error: String(e)};
  }
})()
"""


def page_tune_js(with_scroll=True):
    """Сдвиги страницы из блока настроек: один <style> + прокрутка.

    При нулевых значениях стиль СНИМАЕТСЯ — страница остаётся ровно такой,
    как в проверенной на Windows v13. `with_scroll=False` — для повторного
    применения (не мешаем пользователю прокручивать самому).
    """
    sx = int(SCROLL_X) if with_scroll else 0
    sy = int(SCROLL_Y) if with_scroll else 0
    return PAGE_TUNE_JS % (int(PAGE_OFFSET_X), int(PAGE_OFFSET_Y), int(PAD_BOTTOM),
                           int(INPUT_DX), int(INPUT_DY),
                           json.dumps(str(INPUT_SELECTOR)),
                           sx, sy, sx, sy)


def page_tuning_needed():
    """Нужно ли вообще трогать страницу (все сдвиги нулевые -> нет)."""
    return bool(PAGE_OFFSET_X or PAGE_OFFSET_Y or PAD_BOTTOM
                or INPUT_DX or INPUT_DY or SCROLL_X or SCROLL_Y)


def zoom_settings_needed():
    """Отличаются ли настройки раскладки от проверенных значений v13."""
    return bool(WIDTH_DELTA or HEIGHT_DELTA
                or abs(float(PAGE_ZOOM) - (2.0 / 3.0)) > 1e-6)

GWL_STYLE = -16
GWL_EXSTYLE = -20

WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_CLIPCHILDREN = 0x02000000
WS_CLIPSIBLINGS = 0x04000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_DLGFRAME = 0x00400000
WS_BORDER = 0x00800000

WS_EX_DLGMODALFRAME = 0x00000001
WS_EX_WINDOWEDGE = 0x00000100
WS_EX_CLIENTEDGE = 0x00000200
WS_EX_STATICEDGE = 0x00020000
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000

SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080
SWP_NOACTIVATE = 0x0010
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SW_SHOW = 5

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
HWND_TOP = 0
HWND_TOPMOST = -1
WM_HOTKEY = 0x0312

user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if sys.platform == "win32" else None

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM) if sys.platform == "win32" else None


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


if user32:
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL

    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int

    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long

    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    user32.SetWindowLongW.restype = ctypes.c_long

    if hasattr(user32, "GetWindowLongPtrW"):
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_void_p

        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p

    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL


    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL

    user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
    user32.RegisterHotKey.restype = wintypes.BOOL

    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UnregisterHotKey.restype = wintypes.BOOL

    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetClientRect.restype = wintypes.BOOL

    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL

    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL

    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int

if kernel32:
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL

    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL




def get_window_long(hwnd, index):
    if not user32:
        return 0
    try:
        if hasattr(user32, "GetWindowLongPtrW"):
            return int(user32.GetWindowLongPtrW(hwnd, index) or 0) & 0xFFFFFFFF
        return int(user32.GetWindowLongW(hwnd, index) or 0) & 0xFFFFFFFF
    except Exception:
        diag.exception("main.py:294")
        return 0


def set_window_long(hwnd, index, value):
    if not user32:
        return 0
    try:
        value = int(value) & 0xFFFFFFFF
        if hasattr(user32, "SetWindowLongPtrW"):
            return user32.SetWindowLongPtrW(hwnd, index, ctypes.c_void_p(value))
        return user32.SetWindowLongW(hwnd, index, value)
    except Exception:
        diag.exception("main.py:307")
        return 0


def force_topmost(hwnd):
    """Жёстко поднимает окно в слой TOPMOST (режим «картинка-в-картинке»)."""
    if not user32 or not hwnd:
        return
    try:
        user32.SetWindowPos(
            wintypes.HWND(int(hwnd)), wintypes.HWND(HWND_TOPMOST),
            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
    except Exception:
        diag.exception("main.py:321")
        pass


# Полный список поддерживаемых клавиш (F1..F24, буквы, цифры, numpad, спецклавиши)
VK_MAP = {
    # Функциональные клавиши (F1 - F24)
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74,
    "F6": 0x75, "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79,
    "F11": 0x7A, "F12": 0x7B, "F13": 0x7C, "F14": 0x7D, "F15": 0x7E,
    "F16": 0x7F, "F17": 0x80, "F18": 0x81, "F19": 0x82, "F20": 0x83,
    "F21": 0x84, "F22": 0x85, "F23": 0x86, "F24": 0x87,

    # Буквы (A - Z)
    "A": 0x41, "B": 0x42, "C": 0x43, "D": 0x44, "E": 0x45, "F": 0x46,
    "G": 0x47, "H": 0x48, "I": 0x49, "J": 0x4A, "K": 0x4B, "L": 0x4C,
    "M": 0x4D, "N": 0x4E, "O": 0x4F, "P": 0x50, "Q": 0x51, "R": 0x52,
    "S": 0x53, "T": 0x54, "U": 0x55, "V": 0x56, "W": 0x57, "X": 0x58,
    "Y": 0x59, "Z": 0x5A,

    # Цифры основного блока
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,

    # Цифровая клавиатура (Numpad)
    "Num0": 0x60, "Num1": 0x61, "Num2": 0x62, "Num3": 0x63,
    "Num4": 0x64, "Num5": 0x65, "Num6": 0x66, "Num7": 0x67,
    "Num8": 0x68, "Num9": 0x69,
    "Num*": 0x6A, "Num+": 0x6B, "Num-": 0x6D, "Num.": 0x6E, "Num/": 0x6F,

    # Управление и навигация
    "Space": 0x20, "Enter": 0x0D, "Tab": 0x09, "Esc": 0x1B,
    "Backspace": 0x08, "CapsLock": 0x14, "ScrollLock": 0x91,
    "Pause": 0x13, "PrintScreen": 0x2C,
    "Insert": 0x2D, "Delete": 0x2E, "Home": 0x24, "End": 0x23,
    "PageUp": 0x21, "PageDown": 0x22,
    "Up": 0x26, "Down": 0x28, "Left": 0x25, "Right": 0x27,

    # Символьные и пунктуационные клавиши
    "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
    "`": 0xC0,
}

QT_KEY_TO_NAME = {}
for _i in range(1, 25):
    if hasattr(Qt.Key, f"Key_F{_i}"):
        QT_KEY_TO_NAME[getattr(Qt.Key, f"Key_F{_i}")] = f"F{_i}"
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    if hasattr(Qt.Key, f"Key_{_c}"):
        QT_KEY_TO_NAME[getattr(Qt.Key, f"Key_{_c}")] = _c
for _d in "0123456789":
    if hasattr(Qt.Key, f"Key_{_d}"):
        QT_KEY_TO_NAME[getattr(Qt.Key, f"Key_{_d}")] = _d

QT_KEY_TO_NAME.update({
    Qt.Key.Key_Space: "Space", Qt.Key.Key_Return: "Enter", Qt.Key.Key_Enter: "Enter",
    Qt.Key.Key_Tab: "Tab", Qt.Key.Key_Escape: "Esc", Qt.Key.Key_Backspace: "Backspace",
    Qt.Key.Key_CapsLock: "CapsLock", Qt.Key.Key_ScrollLock: "ScrollLock",
    Qt.Key.Key_Pause: "Pause", Qt.Key.Key_Print: "PrintScreen",
    Qt.Key.Key_Insert: "Insert", Qt.Key.Key_Delete: "Delete",
    Qt.Key.Key_Home: "Home", Qt.Key.Key_End: "End",
    Qt.Key.Key_PageUp: "PageUp", Qt.Key.Key_PageDown: "PageDown",
    Qt.Key.Key_Up: "Up", Qt.Key.Key_Down: "Down",
    Qt.Key.Key_Left: "Left", Qt.Key.Key_Right: "Right",
    Qt.Key.Key_Minus: "-", Qt.Key.Key_Equal: "=",
    Qt.Key.Key_BracketLeft: "[", Qt.Key.Key_BracketRight: "]",
    Qt.Key.Key_Backslash: "\\", Qt.Key.Key_Semicolon: ";",
    Qt.Key.Key_Apostrophe: "'", Qt.Key.Key_Comma: ",",
    Qt.Key.Key_Period: ".", Qt.Key.Key_Slash: "/",
    Qt.Key.Key_QuoteLeft: "`",
})

RU_TO_EN_KEY = {
    'Й': 'Q', 'Ц': 'W', 'У': 'E', 'К': 'R', 'Е': 'T', 'Н': 'Y', 'Г': 'U', 'Ш': 'I', 'Щ': 'O', 'З': 'P', 'Х': '[', 'Ъ': ']',
    'Ф': 'A', 'Ы': 'S', 'В': 'D', 'А': 'F', 'П': 'G', 'Р': 'H', 'О': 'J', 'Л': 'K', 'Д': 'L', 'Ж': ';', 'Э': "'",
    'Я': 'Z', 'Ч': 'X', 'С': 'C', 'М': 'V', 'И': 'B', 'Т': 'N', 'Ь': 'M', 'Б': ',', 'Ю': '.', 'Ё': '`',
}

MODIFIER_MAP = {
    "Ctrl": 0x0002,
    "Alt": 0x0001,
    "Shift": 0x0004,
    "Win": 0x0008,
}

from win32_hotkeys import HotkeyMonitor, HOTKEY_TOGGLE_ID, HOTKEY_MIC_ID
from web_compat import (JS_PURGE, JS_MIC_TOGGLE, JS_MIC_STOP, JS_MIC_START,
                        CONSENT_CLICK_JS, JS_LIGHT_THEME, speech_lang_js)
from native_browser import (NativeBrowser, discover_browsers, choose_target,
                            apply_zoom, apply_viewport, zoom_ok, grant_microphone,
                            speech_surface)
import native_browser
import win32_embed
import browser_focus
import browser_fetch


# v16: события, которыми Qt/Windows сообщают о смене DPI или монитора под
# окном. `DevicePixelRatioChange` есть не во всех сборках Qt6 — берём те,
# что есть.
_METRIC_CHANGE_EVENTS = tuple(
    t for t in (
        getattr(QEvent.Type, 'DevicePixelRatioChange', None),
        getattr(QEvent.Type, 'ScreenChangeInternal', None),
    ) if t is not None)


THEME = {
    "bg": "#0e1020",
    "bg2": "#151830",
    "panel": "rgba(14, 16, 32, 0.96)",
    "line": "#262a4a",
    "text": "#e6e8f5",
    "muted": "#8f96bf",
    "accent": "#7c83ff",
    "accent2": "#5b61e8",
    "danger": "#ef4444",
    "ok": "#34d399",
}

BASE_QSS = f"""
QDialog, QWidget#Card {{
    background: {THEME['bg']};
    border: 1px solid {THEME['line']};
    border-radius: 14px;
}}
QLabel {{ color: {THEME['text']}; font-size: 13px; }}
QLabel#Caption {{ color: {THEME['muted']}; font-size: 11px; }}
QLineEdit {{
    background: {THEME['bg2']};
    border: 1px solid {THEME['line']};
    border-radius: 8px;
    padding: 10px 12px;
    color: {THEME['text']};
    font-size: 14px;
    selection-background-color: {THEME['accent2']};
}}
QLineEdit:focus {{ border: 1px solid {THEME['accent']}; background: #171b38; }}
QComboBox {{
    background: {THEME['bg2']};
    border: 1px solid {THEME['line']};
    border-radius: 8px;
    padding: 8px 10px;
    color: {THEME['text']};
    font-size: 13px;
}}
QComboBox:hover {{ border: 1px solid {THEME['accent']}; }}
QComboBox QAbstractItemView {{
    background: {THEME['bg2']};
    color: {THEME['text']};
    selection-background-color: {THEME['accent2']};
    border: 1px solid {THEME['line']};
    outline: none;
}}
QCheckBox {{ color: {THEME['muted']}; font-size: 12px; }}
QPushButton {{
    background: {THEME['bg2']};
    color: {THEME['text']};
    border: 1px solid {THEME['line']};
    border-radius: 9px;
    padding: 9px 16px;
    font-size: 13px;
    font-weight: 600;
}}
QPushButton:hover {{ border-color: {THEME['accent']}; background: #1b1f40; }}
QPushButton:pressed {{ background: #222750; }}
QPushButton#Primary {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {THEME['accent']}, stop:1 {THEME['accent2']});
    border: none;
    color: #ffffff;
}}
QPushButton#Primary:hover {{ background: {THEME['accent2']}; }}
QPushButton#Ghost {{ background: transparent; color: {THEME['muted']}; }}
QPushButton#Ghost:hover {{ color: {THEME['text']}; border-color: {THEME['accent']}; }}
QPushButton#Danger {{
    background: rgba(239, 68, 68, 0.14);
    color: #fca5a5;
    border: 1px solid rgba(239, 68, 68, 0.55);
}}
QPushButton#Danger:hover {{ background: rgba(239, 68, 68, 0.32); color: #fff; }}
"""


class CDP:
    @stage
    def __init__(self, ws_url):
        self.ws = create_connection(ws_url, suppress_origin=True, max_size=None, timeout=5, http_no_proxy=["127.0.0.1", "localhost"])
        self._id = 0
        self._lock = threading.Lock()

    def send(self, method, params=None, timeout=60):
        started = time.monotonic()
        deadline = started + timeout
        if not self._lock.acquire(timeout=timeout):
            diag.event("cdp.lock_timeout", method=method, timeout=timeout)
            raise TimeoutError("CDP lock acquisition")
        try:
            # v19: в логе видны вызываемый метод и КЛЮЧИ параметров (не сами
            # значения — в них файлы, токены и текст промтов).
            diag.event("cdp.send", method=method, timeout=timeout,
                       params=sorted(str(k) for k in (params or {}).keys())[:16])
            self._id += 1
            mid = self._id
            payload = json.dumps({"id": mid, "method": method, "params": params or {}})
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(method)
            self.ws.settimeout(left)
            self.ws.send(payload)
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(method)
                self.ws.settimeout(left)
                raw = self.ws.recv()
                if not raw:
                    raise ConnectionError("CDP socket closed")
                msg = json.loads(raw)
                if msg.get("id") == mid:
                    if "error" in msg:
                        error = msg.get("error") or {}
                        diag.event("cdp.protocol_error", method=method,
                                   code=error.get('code'),
                                   text=str(error.get('message') or '')[:200],
                                   params=sorted(str(k) for k in (params or {}).keys())[:16])
                        raise RuntimeError("CDP protocol error: " + method)
                    result = msg.get("result", {}) or {}
                    diag.event("cdp.result", method=method,
                               seconds=round(time.monotonic()-started, 3),
                               keys=sorted(str(k) for k in result.keys())[:12]
                               if isinstance(result, dict) else [])
                    return result
        except Exception:
            diag.exception("CDP.send:" + method)
            raise
        finally:
            self._lock.release()

    def eval(self, expr, await_promise=False, by_value=True, timeout=60):
        r = self.send("Runtime.evaluate", {
            "expression": expr,
            "awaitPromise": await_promise,
            "returnByValue": by_value,
            "userGesture": True,
            "generatePreview": False,
        }, timeout=timeout)

        if isinstance(r, dict) and "exceptionDetails" in r:
            # v19: текст исключения страницы — главный источник «почему скрипт
            # не сработал». Раньше он терялся (в логе было только RuntimeError).
            details = r.get("exceptionDetails") or {}
            diag.event("cdp.js_exception", method="Runtime.evaluate",
                       text=str(details.get("text") or "")[:200],
                       line=int((details.get("exception") or {}).get("lineNumber", 0)
                                if isinstance(details.get("exception"), dict) else 0))
            raise RuntimeError(str(details.get("text", "JS exception")))

        if isinstance(r, dict):
            return r.get("result", {})
        return {}

    def close(self):
        try:
            self.ws.close()
        except Exception:
            diag.exception("main.py:553")
            pass








def _http_json(port, path, timeout=1.0):
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return json.load(r)


@stage
def attach_chrome(port, timeout=30):
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            version = _http_json(port, "/json/version")
            browser_ws = version.get("webSocketDebuggerUrl")

            targets = _http_json(port, "/json/list")
            pages = [
                t for t in targets
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
            ]

            # NEVER a consent/account wall: `consent.google.com` used to match
            # "any google page", automation attached to the wall and waited for
            # a chat that never arrived (the "chrome starts and hangs" report).
            chosen = choose_target(targets)

            if browser_ws and chosen:
                diag.event("cdp.discovered", browser=version.get("Browser"), protocol=version.get("Protocol-Version"), pages=len(pages))
                browser = CDP(browser_ws)
                try:
                    return browser, CDP(chosen["webSocketDebuggerUrl"])
                except Exception:
                    diag.exception("main.py:686")
                    browser.close()
                    raise
        except Exception:
            diag.exception("main.py:690")
            pass

        time.sleep(0.1)

    raise TimeoutError("chrome/devtools not ready")


def grant_mic_permission(browser: CDP, page: CDP):
    origins = set()

    try:
        origin = page.eval("location.origin", timeout=5).get("value")
        if origin and origin != "null":
            origins.add(origin)
    except Exception:
        diag.exception("main.py:706")
        pass

    origins.update([
        "https://google.com",
        "https://www.google.com",
        "https://gemini.google.com",
        "https://accounts.google.com",
    ])

    browser_context_id = None
    try:
        info = browser.send("Target.getBrowserContexts", timeout=5)
        ids = info.get("browserContextIds") or []
        if ids:
            browser_context_id = ids[0]
    except Exception:
        diag.exception("main.py:723")
        pass

    for origin in origins:
        params = {"origin": origin, "permissions": ["audioCapture", "videoCapture"]}
        if browser_context_id:
            params["browserContextId"] = browser_context_id

        try:
            browser.send("Browser.grantPermissions", params, timeout=5)
        except Exception:
            diag.exception("main.py:734")
            pass



# Детект файлов в чате Google AI
JS_CHECK_FILES = r"""
(() => {
const chips = Array.from(document.querySelectorAll('div.ArblTe'));
let done = 0;
let uploading = false;
const names = [];
for (const chip of chips) {
    let fname = '';
    try {
        const host = chip.closest('div[role="button"]');
        if (host) {
            fname = (host.getAttribute('title') || host.getAttribute('aria-label') || '').trim();
            if (!fname) {
                const label = host.querySelector('div.zrI2ad');
                const txt = (label ? label.innerText : host.innerText) || '';
                fname = txt.replace(/\s+/g, ' ').trim();
            }
        }
        if (!fname) fname = ((chip.innerText || '').replace(/\s+/g, ' ').trim());
        fname = fname.slice(0, 160);
    } catch (e) { fname = ''; }
    if (chip.querySelector('div.zXgSbc')) { done += 1; names.push(fname); continue; }
    const bar = chip.querySelector('div[data-progressvalue]');
    if (bar) {
        const val = parseFloat(bar.getAttribute('data-progressvalue') || '0');
        if (val < 0.99) { uploading = true; continue; }
    }
    done += 1; names.push(fname);
}
return { present: done, uploading: uploading, names: names };
})()
"""

JS_FIND_INPUT = r"""
(() => {
    const ins = Array.from(document.querySelectorAll('input[type=file]'));
    if (!ins.length) return null;

    const suitable = ins.find((input) => {
        if (!input.accept) return true;
        const accept = String(input.accept).toLowerCase();
        return accept.includes('pdf') || accept.includes('application') || accept.includes('*');
    });

    return suitable || ins[0];
})()
"""

JS_DISABLE_CONTEXT_MENU = "window.addEventListener('contextmenu', (e) => { e.preventDefault(); }, true);"

JS_DISABLE_DRAG = r"""
(() => {
    const s = document.createElement('style');
    s.innerHTML = 'html, body, *:not(input):not(textarea):not([contenteditable="true"]) { -webkit-app-region: no-drag !important; user-select: none !important; }';
    document.head.appendChild(s);
})();
"""

# Микрофон: СТАРТ записи речи (ТЗ п.1)
JS_START_RECORDING = r"""
(() => {
    const findMic = () => {
        // 1. Поиск по специфичным атрибутам и классам кнопки микрофона
        const selectors = [
            'button[data-xid="input-plate-voice-button"]',
            'button[data-xid="h4gG8"]',
            'button.uMMzHc.vpw7Fc',
            'button.vpw7Fc',
            'button[aria-label="Микрофон"]',
            'button[aria-label="Использовать микрофон"]',
            'button[aria-label*="икрофон" i]',
            'button[aria-label*="дикт" i]',
            'button[aria-label*="Microphone" i]',
            'button[aria-label*="voice" i]:not([aria-label*="send" i]):not([aria-label*="тправ" i])',
            'div[role="button"][aria-label*="икрофон" i]'
        ];
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (el && el.offsetParent !== null) return el;
        }

        // 2. Поиск внутри контейнера ввода (input plate / footer / bottom form)
        const inputArea = document.querySelector('.esoFne, .Txyg0d, footer, [role="region"], form') || document.body;
        const buttons = Array.from(inputArea.querySelectorAll('button, div[role="button"]'));
        for (const btn of buttons) {
            const label = (btn.getAttribute('aria-label') || '').toLowerCase();
            const xid = (btn.getAttribute('data-xid') || '').toLowerCase();
            if ((label.includes('микрофон') || label.includes('mic') || xid.includes('voice')) && !xid.includes('send')) {
                return btn;
            }
        }

        // 3. Поиск по SVG path иконки микрофона
        const paths = Array.from(inputArea.querySelectorAll('svg path'));
        for (const p of paths) {
            const d = p.getAttribute('d') || '';
            if (d.includes('M480-400') || d.includes('M12 14c1.66') || d.includes('M12 2a3 3 0')) {
                const btn = p.closest('button, div[role="button"]');
                if (btn && !(btn.getAttribute('data-xid') || '').includes('send')) return btn;
            }
        }

        return null;
    };

    const el = findMic();
    if (!el) return { ok: false, reason: 'not_found' };

    try { el.focus(); } catch(e) {}
    const rect = el.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;

    const evOpts = {
        bubbles: true,
        cancelable: true,
        view: window,
        clientX: x,
        clientY: y,
        button: 0,
        buttons: 1
    };

    try { el.dispatchEvent(new PointerEvent('pointerdown', { ...evOpts, pointerId: 1, pointerType: 'mouse', isPrimary: true, pressure: 0.5 })); } catch(e) {}
    try { el.dispatchEvent(new MouseEvent('mousedown', evOpts)); } catch(e) {}
    try { el.dispatchEvent(new PointerEvent('pointerup', { ...evOpts, pointerId: 1, pointerType: 'mouse', isPrimary: true, buttons: 0, pressure: 0 })); } catch(e) {}
    try { el.dispatchEvent(new MouseEvent('mouseup', { ...evOpts, buttons: 0 })); } catch(e) {}
    try { el.dispatchEvent(new MouseEvent('click', { ...evOpts, buttons: 0 })); } catch(e) {}
    try { el.click(); } catch(e) {}

    return { ok: true, x: x, y: y };
})()
"""

# Микрофон: ПРОВЕРКА активного процесса записи (ТЗ п.2)
JS_CHECK_RECORDING = r"""
(() => {
    const textEls = document.querySelectorAll('.pRjbAe, [aria-live="polite"], div');
    for (const el of textEls) {
        if (el.innerText && el.innerText.includes('Преобразование речи')) return { recording: true };
    }
    if (document.querySelector('.tFTltc, .S7I6ve, .S34Aff, .r5nqdd')) return { recording: true };
    if (document.querySelector('button[data-xid="input-plate-voice-send-button"]')) return { recording: true };
    const ta = document.querySelector('textarea.ITIRGe, textarea[aria-label="Задайте вопрос"]');
    if (ta && (ta.hidden || ta.style.display === 'none')) return { recording: true };

    return { recording: false };
})()
"""

# Микрофон: ОТПРАВКА голосовой записи в ИИ чат (ТЗ п.3)
JS_STOP_AND_SEND = r"""
(() => {
    const findSend = () => {
        // 1. Поиск точной кнопки отправки голосового ввода
        const selectors = [
            'button[data-xid="input-plate-voice-send-button"]',
            'button.wdK4Nc',
            'button[aria-label="Отправить"]',
            'button.uMMzHc.wdK4Nc',
            'button[data-xid*="voice-send"]',
            'button[data-xid*="send"]',
            'button[aria-label*="тправить" i]',
            'button[aria-label*="end" i]',
            'button[aria-label*="ubmit" i]',
            'div[role="button"][aria-label*="тправить" i]'
        ];
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (el) return el;
        }

        // 2. Поиск по иконке отправки в области ввода
        const inputArea = document.querySelector('.esoFne, .Txyg0d, footer, form') || document.body;
        const buttons = Array.from(inputArea.querySelectorAll('button, div[role="button"]'));
        for (const btn of buttons) {
            const label = (btn.getAttribute('aria-label') || '').toLowerCase();
            const xid = (btn.getAttribute('data-xid') || '').toLowerCase();
            if (label.includes('отправ') || label.includes('send') || xid.includes('send')) {
                return btn;
            }
        }
        return null;
    };

    const triggerClick = (el) => {
        if (!el) return null;
        try { el.disabled = false; } catch(e) {}
        try { el.removeAttribute('disabled'); } catch(e) {}
        try { el.setAttribute('aria-disabled', 'false'); } catch(e) {}
        try { el.focus(); } catch(e) {}

        const rect = el.getBoundingClientRect();
        const x = rect.left + rect.width / 2;
        const y = rect.top + rect.height / 2;

        const evOpts = {
            bubbles: true,
            cancelable: true,
            view: window,
            clientX: x,
            clientY: y,
            button: 0,
            buttons: 1
        };

        try { el.dispatchEvent(new PointerEvent('pointerdown', { ...evOpts, pointerId: 1, pointerType: 'mouse', isPrimary: true, pressure: 0.5 })); } catch(e) {}
        try { el.dispatchEvent(new MouseEvent('mousedown', evOpts)); } catch(e) {}
        try { el.dispatchEvent(new PointerEvent('pointerup', { ...evOpts, pointerId: 1, pointerType: 'mouse', isPrimary: true, buttons: 0, pressure: 0 })); } catch(e) {}
        try { el.dispatchEvent(new MouseEvent('mouseup', { ...evOpts, buttons: 0 })); } catch(e) {}
        try { el.dispatchEvent(new MouseEvent('click', { ...evOpts, buttons: 0 })); } catch(e) {}
        try { el.click(); } catch(e) {}

        return { ok: true, x: x, y: y };
    };

    const el = findSend();
    if (el) {
        return triggerClick(el);
    }

    // Fallback: клик по контейнеру остановки или Enter в текстовом поле
    const waveEl = document.querySelector('.tFTltc, .pRjbAe, .S34Aff');
    if (waveEl) {
        triggerClick(waveEl);
    }

    const inputEl = document.querySelector('textarea, div[contenteditable="true"], div[role="textbox"]');
    if (inputEl) {
        try { inputEl.hidden = false; } catch(e) {}
        try { inputEl.focus(); } catch(e) {}
        const opts = { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true };
        inputEl.dispatchEvent(new KeyboardEvent('keydown', opts));
        inputEl.dispatchEvent(new KeyboardEvent('keypress', opts));
        inputEl.dispatchEvent(new KeyboardEvent('keyup', opts));
        return { ok: true, fallback: 'enter' };
    }

    return { ok: false, reason: 'not_found' };
})()
"""

# Удаление файлов из чата
JS_REMOVE_ALL_FILES = r"""
(() => {
    let clicked = 0;
    const fire = (el) => {
        try {
            const r = el.getBoundingClientRect();
            const x = r.left + r.width / 2, y = r.top + r.height / 2;
            for (const t of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                el.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, clientX: x, clientY: y}));
            }
            clicked++;
        } catch (e) {}
    };
    const chips = document.querySelectorAll('div.ArblTe');
    for (const chip of chips) {
        const x = chip.querySelector('div.zXgSbc');
        if (x) fire(x);
    }
    if (!clicked) {
        const old = document.querySelectorAll(
            'button[aria-label*="Delete"], button[aria-label*="Remove"], button[aria-label*="Удалить"]');
        for (const el of old) { try { el.click(); clicked++; } catch (e) {} }
    }
    return clicked;
})()
"""

JS_CHECK_END = r"""
(() => {
    const bodyText = (document.body ? document.body.innerText : '') || '';
    const cleanText = bodyText.toUpperCase().replace(/[\s=*/\-_]+/g, '');
    const matches = cleanText.match(/КОНЕЦОТВЕТА/g);
    return matches ? matches.length : 0;
})()
"""


def install_compat(browser: CDP, page: CDP, native: bool = False):
    """Согласуем User-Agent и client hints с обычным Chrome для feature-gating сайта.

    `native=True` — настоящий Chrome: UA и client hints честные, поэтому
    подменять их не нужно, а шим речи **вреден** (он подменяет рабочий
    `SpeechRecognition`, которым сайт и записывает голос). Здесь
    устанавливается только кликер согласия: в ЕС (Испания) свежий профиль
    сначала показывается страница «Aceptar todo», и пока она не принята,
    чата не будет.
    """
    if native:
        try:
            page.send("Page.enable", timeout=5)
        except Exception:
            diag.exception("main.py:compat_native_enable")
        try:
            page.send("Page.addScriptToEvaluateOnNewDocument",
                      {"source": CONSENT_CLICK_JS}, timeout=5)
            page.eval(CONSENT_CLICK_JS, timeout=5)
            diag.event("browser.consent_installed")
        except Exception:
            diag.exception("main.py:compat_native_consent")
        return

    try:
        ua = str(page.eval("navigator.userAgent", timeout=5).get("value") or "")
        full = ""
        m = re.search(r"Chrome/([\d.]+)", ua)
        if m:
            full = m.group(1)
        major = full.split(".")[0] if full else "120"
        page.send("Network.enable", timeout=5)
        params = {
            "acceptLanguage": "ru-RU,ru;q=0.9,en;q=0.8",
            "platform": "Windows",
            "userAgentMetadata": {
                "brands": [
                    {"brand": "Chromium", "version": major},
                    {"brand": "Google Chrome", "version": major},
                    {"brand": "Not;A=Brand", "version": "24"},
                ],
                "fullVersion": full or (major + ".0.0.0"),
                "platform": "Windows",
                "platformVersion": "15.0.0",
                "architecture": "x86",
                "model": "",
                "mobile": False,
                "bitness": "64",
                "wow64": False,
            },
        }
        if ua:
            params["userAgent"] = ua
        page.send("Network.setUserAgentOverride", params, timeout=5)
        diag.event("webengine.compat_installed", chrome=full or "?")
    except Exception:
        diag.exception("webengine.compat")


# v18: скрипты, которые внедряются при создании документа и работают всегда.
PAGE_SCRIPTS = (JS_DISABLE_CONTEXT_MENU, JS_DISABLE_DRAG, JS_PURGE)


def page_scripts(lang="ru-RU", light=True):
    """Полный набор скриптов страницы с подставленным языком (v18)."""
    scripts = [speech_lang_js(lang)]
    if light:
        scripts.append(JS_LIGHT_THEME)
    scripts.extend(PAGE_SCRIPTS)
    return tuple(scripts)


# v18: вернуть страницу к жизни после разворачивания окна. Chromium
# приостанавливает отрисовку, пока окно скрыто, — одного показа мало.
JS_REVIVE = r"""
(() => {
    try { window.dispatchEvent(new Event('resize')); } catch (e) {}
    try { document.documentElement && document.documentElement.style &&
          document.documentElement.style.setProperty('color-scheme', 'light', 'important'); } catch (e) {}
    try { const b = document.body; if (b) { b.offsetHeight; } } catch (e) {}
    return true;
})();
"""


# v19: отпечаток страницы. Раньше готовность определялась одним вопросом
# «есть ли текстовое поле» — он отвечал «да» уже на `readyState == "interactive"`,
# когда страница ещё грузится. Из-за этого GUI прогрузки снимали слишком рано и
# пользователь видел недогруженную страницу (жалоба «Error - 1»). Здесь одним
# вызовом снимается ВСЁ, по чему видно, что именно открыто:
#   * `verdict` — какая это поверхность (чат / режим ИИ в поиске / вход / согласие);
#   * `ready` — настоящая готовность документа;
#   * `composer`, `files`, `chips` — есть ли во что писать и куда прикреплять файл;
#   * `dark`, `bg`, `scheme` — тёмная страница или нет (жалоба «нестандартно
#     тёмного цвета»): замеряется, а не предполагается;
#   * `account` — вошёл ли пользователь в аккаунт Google;
#   * `pending` — сколько ресурсов уже запрошено (по нему видно залипание).
JS_SURFACE = r"""
(() => {
    const qsa = (s) => { try { return document.querySelectorAll(s).length } catch (e) { return 0 } };
    const host = String(location.hostname || '');
    const path = String(location.pathname || '');
    const query = String(location.search || '');
    const protocol = String(location.protocol || '');
    let verdict = 'other';
    if (protocol === 'chrome-error:') verdict = 'browser_error';
    else if (host === 'accounts.google.com') verdict = 'login';
    else if (host === 'consent.google.com') verdict = 'consent';
    else if (host === 'gemini.google.com' || host === 'aistudio.google.com') verdict = 'gemini';
    else if (host === 'about' || location.href === 'about:blank') verdict = 'blank';
    else if ((host === 'www.google.com' || host === 'google.com') &&
             (query.indexOf('udm=50') >= 0 || path === '/ai')) verdict = 'ai_mode';
    else if (host === 'www.google.com' || host === 'google.com') verdict = 'google_other';
    let bg = '';
    let scheme = '';
    try { bg = String(getComputedStyle(document.body).backgroundColor || ''); } catch (e) {}
    try { scheme = String((getComputedStyle(document.documentElement).colorScheme) || ''); } catch (e) {}
    let dark = false;
    try { dark = !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches); } catch (e) {}
    let account = false;
    try {
        account = !!(document.querySelector('a[href*="accounts.google.com/ServiceLogin"]') === null &&
            (document.querySelector('img[src*="googleusercontent.com"]') ||
             document.querySelector('div[aria-label*="Google Account"]') ||
             document.querySelector('a[href*="accounts.google.com/SignOut"]')));
    } catch (e) {}
    return {
        verdict: verdict, host: host, path: path, ready: document.readyState,
        composer: qsa('div.esoFne') + qsa('rich-textarea') + qsa('textarea') +
            qsa('div[contenteditable="true"]') + qsa('[role="textbox"]'),
        files: qsa('input[type="file"]'), chips: qsa('div.ArblTe'),
        buttons: qsa('button'), frames: qsa('iframe'),
        bodyChildren: document.body ? document.body.children.length : 0,
        dark: dark, bg: bg, scheme: scheme, account: account,
        pending: (performance.getEntriesByType('resource') || []).length,
        lang: String(navigator.language || '')
    };
})()
"""

# Разрешённые значения: данные страницы — всегда недоверенный ввод.
_SURFACE_VERDICTS = ("gemini", "ai_mode", "google_other", "login", "consent",
                     "browser_error", "blank", "other")
_COLOR_RE = re.compile(r"^rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*(,\s*[\d.]+\s*)?\)$")


def probe_page_surface(page, timeout=3):
    """Снять отпечаток страницы: только числа, перечисления и цвет (v19)."""
    empty = {"verdict": "unknown", "ready": "unknown", "composer": 0, "files": 0,
             "chips": 0, "bodyChildren": 0, "dark": False, "bg": "", "scheme": "",
             "account": False, "pending": 0, "lang": "", "host": "", "buttons": 0,
             "frames": 0}
    if page is None:
        return empty
    try:
        raw = page.eval(JS_SURFACE, timeout=timeout).get("value")
    except Exception:
        diag.exception("main.surface_probe")
        return empty
    if not isinstance(raw, dict):
        return empty
    result = dict(empty)
    verdict = raw.get("verdict")
    result["verdict"] = verdict if verdict in _SURFACE_VERDICTS else "other"
    ready = raw.get("ready")
    result["ready"] = ready if ready in ("loading", "interactive", "complete") else "unknown"
    for key in ("composer", "files", "chips", "bodyChildren", "pending", "buttons",
                "frames"):
        value = raw.get(key)
        result[key] = int(value) if isinstance(value, int) and 0 <= value <= 1000000 else 0
    for key in ("dark", "account"):
        result[key] = raw.get(key) is True
    bg = str(raw.get("bg") or "")
    result["bg"] = bg[:32] if _COLOR_RE.match(bg.strip()) else ""
    result["scheme"] = str(raw.get("scheme") or "")[:16]
    result["lang"] = str(raw.get("lang") or "")[:16]
    host = str(raw.get("host") or "")
    result["host"] = host[:64]
    path = str(raw.get("path") or "")
    result["path"] = path[:64] if path in ("/", "/ai", "/search", "/app") else "<other>"
    return result


def _surface_signature(surface):
    return (surface.get("verdict"), surface.get("ready"), surface.get("composer"),
            surface.get("files"), surface.get("chips"), surface.get("dark"),
            surface.get("bg"), surface.get("account"))


def measure_theme(page, surface=None):
    """Замер темы страницы: тёмная или нет — по факту, а не по настройке (v19).

    Жалоба «Error - 1»: страница «нестандартно тёмного цвета», хотя приложение
    применило светлую тему. Значит применение надо ПРОВЕРЯТЬ. Тёмный фон здесь
    определяется по цвету `body` (яркость), а не по `prefers-color-scheme`:
    сервис может красить страницу своей разметкой, а системное свойство при
    этом оставаться светлым.
    """
    data = surface if isinstance(surface, dict) and surface.get("bg") \
        else probe_page_surface(page)
    bg = str(data.get("bg") or "")
    numbers = [int(n) for n in re.findall(r"\d{1,3}", bg)[:3]]
    dark = False
    if len(numbers) == 3:
        # Яркость по восприятию: тёмный фон — это серый/чёрный, а не цветной.
        luminance = 0.299 * numbers[0] + 0.587 * numbers[1] + 0.114 * numbers[2]
        dark = luminance < 110
    dark = bool(dark or data.get("dark"))
    diag.event("page.theme_probe", bg=bg, scheme=str(data.get("scheme") or ""),
               dark=dark, verdict=data.get("verdict"))
    return dark


def repair_light_theme(page, lang="ru-RU"):
    """Привести страницу к светлой теме и проверить, что получилось (v19).

    Почему это не «ещё один вызов»: в v18 светлая тема применялась ОДИН раз, на
    ещё не загруженной странице, и всё. Здесь применяется и ПРОВЕРЯЕТСЯ: если
    замер всё ещё тёмный — применяется снова (не больше `THEME_MAX_REPAIRS`),
    а в логе остаётся и цвет, и verdict.
    """
    applied = apply_language_and_theme(page, lang=lang, light=True)
    try:
        page.send("Emulation.setAutoDarkModeOverride", {"enabled": False}, timeout=4)
    except Exception:
        diag.exception("main.repair_auto_dark")
    try:
        page.eval(JS_LIGHT_THEME, timeout=5)
    except Exception:
        diag.exception("main.repair_light_js")
    return applied


def wait_for_chat_surface(page, timeout=SURFACE_READY_TIMEOUT, poll=SURFACE_POLL_MS,
                          lang="ru-RU", light=True, should_stop=None,
                          on_round=None):
    """Ждать НАСТОЯЩЕЙ готовности чата, а не первого текстового поля (v19).

    Раньше ворота (`READY_SCRIPT`) отвечали «готово» уже на
    `readyState == "interactive"`: страница в этот момент ещё грузится, поэтому
    GUI прогрузки снимали слишком рано. Теперь готовность — это:
      * `readyState == "complete"` (догрузилось, а не «почти»);
      * есть поле ввода (во что писать запрос);
      * **два одинаковых замера подряд** (страница устоялась);
      * тема приведена к светлой, если замер показал тёмную.
    Возвращает словарь: `ready`, `verdict`, `surface`, `rounds`,
    `blocked` (причина, по которой чата здесь нет и не будет).
    """
    deadline = time.monotonic() + max(1.0, float(timeout))
    result = {"ready": False, "verdict": "unknown", "surface": {}, "rounds": 0,
              "blocked": "", "stable": 0, "theme_repairs": 0}
    if page is None:
        result["blocked"] = "no_page"
        return result
    last_signature = None
    last_log = ""
    stable = 0
    while time.monotonic() < deadline:
        if callable(should_stop) and should_stop():
            result["blocked"] = "stopped"
            return result
        result["rounds"] += 1
        surface = probe_page_surface(page)
        result["surface"] = surface
        result["verdict"] = surface.get("verdict") or "unknown"
        if callable(on_round):
            try:
                on_round(result["rounds"], surface)
            except Exception:
                diag.exception("main.surface_round_hook")
        signature = _surface_signature(surface)
        previous_signature = last_signature
        # Одинаковые замеры подряд пишем один раз: лог не должен зарастать
        # сотнями одинаковых строк, а «залипла» страница или нет — видно.
        if signature != last_signature:
            diag.event("page.surface", **{k: surface[k] for k in
                       ("verdict", "ready", "composer", "files", "chips", "dark",
                        "bg", "scheme", "account", "pending", "bodyChildren",
                        "lang", "host", "path") if k in surface})
            last_signature = signature
        verdict = surface.get("verdict")
        if verdict in ("login", "consent", "browser_error"):
            # Чата здесь не будет никогда: дальше ждать бессмысленно.
            result["blocked"] = verdict
            diag.warn("surface_blocked", reason=verdict)
            return result
        if light and surface.get("ready") == "complete" \
                and result["theme_repairs"] < THEME_MAX_REPAIRS \
                and measure_theme(page, surface):
            result["theme_repairs"] += 1
            diag.warn("dark_theme_measured", repair=result["theme_repairs"],
                      bg=str(surface.get("bg") or ""))
            repair_light_theme(page, lang=lang)
            stable = 0
            last_signature = None
            continue
        ready = (surface.get("ready") == "complete" and int(surface.get("composer") or 0) > 0)
        if not ready:
            stable = 0
        elif signature == previous_signature:
            stable += 1
        else:
            stable = 1
        result["stable"] = stable
        if ready and stable >= int(SURFACE_STABLE_ROUNDS):
            result["ready"] = True
            diag.event("page.ready", rounds=result["rounds"], verdict=verdict,
                       theme_repairs=result["theme_repairs"])
            return result
        now = time.strftime("%H:%M:%S")
        if now != last_log:
            last_log = now
            diag.event("page.wait", round=result["rounds"],
                       left=round(deadline - time.monotonic(), 1),
                       verdict=verdict, ready=surface.get("ready"),
                       composer=surface.get("composer"),
                       stable=stable, pending=surface.get("pending"))
        time.sleep(max(0.05, float(poll) / 1000.0))
    result["blocked"] = result["blocked"] or "timeout"
    diag.warn("surface_timeout", **{k: result["surface"].get(k)
                                    for k in ("verdict", "ready", "composer",
                                              "files", "pending", "host")})
    return result


def apply_language_and_theme(page, lang="ru-RU", light=True):
    """Язык интерфейса и светлая тема страницы через CDP (v18).

    * `Emulation.setLocaleOverride` — язык страницы (navigator.language):
      именно по нему сервис ИИ выбирает язык ответов и распознавания;
    * `Emulation.setAutoDarkModeOverride` — выключает авто-тёмную тему Chrome;
    * `Emulation.setEmulatedMedia` — фиксирует `prefers-color-scheme: light`.
    Идемпотентно: любая ошибка только пишется в лог, страница не ломается.
    """
    applied = {}
    if page is None:
        return applied
    try:
        page.send("Emulation.setLocaleOverride", {"locale": str(lang or "ru-RU")},
                  timeout=4)
        applied["locale"] = str(lang or "ru-RU")
    except Exception:
        diag.exception("main.locale_override")
    if not light:
        return applied
    try:
        page.send("Emulation.setAutoDarkModeOverride", {"enabled": False}, timeout=4)
        applied["auto_dark"] = False
    except Exception:
        diag.exception("main.auto_dark")
    try:
        page.send("Emulation.setEmulatedMedia", {
            "features": [{"name": "prefers-color-scheme", "value": "light"}],
        }, timeout=4)
        applied["color_scheme"] = "light"
    except Exception:
        diag.exception("main.emulated_media")
    return applied


def install_purge(page: CDP, lang="ru-RU", light=True):
    for src in page_scripts(lang, light):
        try:
            page.send("Page.addScriptToEvaluateOnNewDocument", {"source": src}, timeout=5)
        except Exception:
            diag.exception("main.py:1111")
            pass

    try:
        page.send("Page.enable", timeout=5)
    except Exception:
        diag.exception("main.py:1117")
        pass

    # v18: язык и тема — сразу и по CDP (часть свойств живёт только там).
    state = apply_language_and_theme(page, lang, light)
    if state:
        diag.event("page.language_theme", **state)

    for src in page_scripts(lang, light):
        try:
            page.eval(src, timeout=5)
        except Exception:
            diag.exception("main.py:1124")
            pass


def log_page_probe(page):
    # Deliberately no URL, query, title, text, filenames or HTML in this result.
    script = """(() => {
        const h = location.hostname;
        const category = h === 'accounts.google.com' ? 'google_login' :
            h === 'consent.google.com' ? 'google_consent' :
            ['google.com', 'www.google.com', 'gemini.google.com'].includes(h) ? 'google' :
            location.protocol === 'chrome-error:' ? 'browser_error' :
            location.href === 'about:blank' ? 'blank' : 'other';
        return {category, ready: document.readyState,
            bodyChildren: document.body ? document.body.children.length : 0,
            fileInputs: document.querySelectorAll('input[type="file"]').length,
            editors: document.querySelectorAll('textarea,[contenteditable="true"],[role="textbox"]').length,
            frames: document.querySelectorAll('iframe').length,
            documentFocused: document.hasFocus(),
            activeEditable: !!(document.activeElement &&
                (document.activeElement.isContentEditable || ['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName))),
            activeReadOnly: !!(document.activeElement && document.activeElement.readOnly),
            activeDisabled: !!(document.activeElement && document.activeElement.disabled)};
    })()"""
    try:
        raw = page.eval(script, timeout=2).get("value")
        if isinstance(raw, dict):
            # Treat page-provided data as untrusted: permit only fixed enums and counts.
            category = raw.get("category")
            ready = raw.get("ready")
            counts = {key: raw[key] for key in ("bodyChildren", "fileInputs", "editors", "frames")
                      if type(raw.get(key)) is int and 0 <= raw[key] <= 1000000}
            flags = {key: raw[key] for key in ("documentFocused", "activeEditable", "activeReadOnly", "activeDisabled")
                     if type(raw.get(key)) is bool}
            diag.event("page.probe", category=category if category in
                       ("google_login", "google_consent", "google", "browser_error", "blank", "other") else "unknown",
                       ready=ready if ready in ("loading", "interactive", "complete") else "unknown", **counts, **flags)
    except Exception:
        diag.exception("page.probe")


def check_state(page: CDP):
    """Состояние чата: present — число готовых файлов, names — их имена."""
    default = {"present": 0, "uploading": False, "names": [], "probe_ok": False}

    try:
        r = page.eval(JS_CHECK_FILES, timeout=2)
        value = r.get("value")
        if isinstance(value, dict):
            try:
                present = int(value.get("present", 0) or 0)
            except Exception:
                diag.exception("main.py:1139")
                present = 1 if value.get("present") else 0
            names = value.get("names", []) or []
            return {
                "probe_ok": True,
                "present": present,
                "uploading": bool(value.get("uploading", False)),
                "names": [str(n) for n in names] if isinstance(names, list) else [],
            }
    except Exception:
        diag.exception("main.py:1148")
        pass

    return default


def _chat_has_file(names, wanted: str) -> bool:
    """Есть ли файл wanted среди прикреплённых (поиск по подстроке имени)."""
    want = (wanted or "").strip().lower()
    if not want:
        return False
    for n in names or []:
        text = str(n or "").lower()
        if want in text or text in want:
            return True
    return False


def _mime_for_path(path) -> str:
    """MIME-тип по расширению: PDF и TXT грузятся каждый со своим типом."""
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        return "application/pdf"
    if ext in (".txt", ".json", ".md", ".csv"):
        return "text/plain"
    return "application/octet-stream"


def upload_file_advanced(page: CDP, path: Path, mime: str | None = None) -> bool:
    """
    Надежный метод загрузки файла через эмуляцию Drag & Drop с DataTransfer API и File Input.
    Работает с любыми современными SPA, так как полностью имитирует действие
    пользователя (перетаскивание файла), обходя ограничения скрытых input'ов.
    Одинаково используется и для PDF промта, и для Шаблон.txt.
    """
    try:
        if not path or not Path(path).exists():
            return False

        if page.eval(READY_SCRIPT, timeout=2).get("value") is not True:
            return False
        file_path = Path(path)
        file_name = file_path.name.replace("\\", "").replace('"', "").replace("\n", "")
        mime = mime or _mime_for_path(file_path)
        file_data = file_path.read_bytes()
        b64_data = base64.b64encode(file_data).decode('ascii')

        js_inject = f"""
        (() => {{
            try {{
                const b64 = "{b64_data}";
                const fileName = "{file_name}";

                const binary = atob(b64);
                const bytes = new Uint8Array(binary.length);
                for (let i = 0; i < binary.length; i++) {{
                    bytes[i] = binary.charCodeAt(i);
                }}

                const blob = new Blob([bytes], {{ type: '{mime}' }});
                const file = new File([blob], fileName, {{ type: '{mime}' }});
                const dt = new DataTransfer();
                dt.items.add(file);

                let matched = false;

                // 1. Устанавливаем в скрытый input
                const inputs = Array.from(document.querySelectorAll('input[type="file"]'));
                for (const input of inputs) {{
                    try {{
                        input.files = dt.files;
                        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        matched = true;
                    }} catch (e) {{}}
                }}

                // 2. Эмулируем Drag & Drop на текстовое поле (основной триггер для Google AI)
                const dropZone = document.querySelector('.esoFne') 
                              || document.querySelector('textarea') 
                              || document.querySelector('[role="textbox"]')
;

                if (dropZone) {{
                    dropZone.dispatchEvent(new DragEvent('dragenter', {{ bubbles: true, cancelable: true, dataTransfer: dt }}));
                    dropZone.dispatchEvent(new DragEvent('dragover', {{ bubbles: true, cancelable: true, dataTransfer: dt }}));
                    dropZone.dispatchEvent(new DragEvent('drop', {{
                        bubbles: true,
                        cancelable: true,
                        dataTransfer: dt
                    }}));
                    matched = true;
                }}

                return matched;
            }} catch (err) {{
                return false;
            }}
        }})()
        """

        res = page.eval(js_inject, timeout=10)
        return bool(res.get("value"))

    except Exception:
        diag.exception("main.py:1252")
        return False




_enum_callbacks = []




def text_to_pdf_bytes(text: bytes, title: str = "") -> bytes:
    if FPDF is None:
        raise RuntimeError("fpdf is not installed")

    pdf = FPDF()
    try:
        if title:
            pdf.set_title(title)
    except Exception:
        diag.exception("main.py:1318")
        pass

    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    try:
        font_path = DATA_DIR / "DejaVuSans.ttf"
        if font_path.exists():
            pdf.add_font("DejaVu", "", str(font_path))
            pdf.set_font("DejaVu", size=10)
        else:
            pdf.set_font("Helvetica", size=10)
    except Exception:
        diag.exception("main.py:1332")
        try:
            pdf.set_font("Helvetica", size=10)
        except Exception:
            diag.exception("main.py:1336")
            pass

    decoded = text.decode("utf-8", errors="replace")

    for line in decoded.splitlines():
        line = line.strip()

        if not line:
            try:
                pdf.ln(6)
            except Exception:
                diag.exception("main.py:1348")
                pass
            continue

        try:
            pdf.multi_cell(0, 6, line)
        except Exception:
            diag.exception("main.py:1355")
            try:
                safe_line = line.encode("latin-1", errors="replace").decode("latin-1")
                pdf.multi_cell(0, 6, safe_line)
            except Exception:
                diag.exception("main.py:1360")
                pass

    return bytes(pdf.output())


_ILLEGAL_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def prompt_display_name(filename: str) -> str:
    """Имя так, как оно отображается в выпадающем списке (без служебных расширений)."""
    name = (filename or "").strip()
    name = name.replace("\\", "/").split("/")[-1]
    for ext in (".enc", ".bin", ".dat", ".txt", ".json", ".prompt", ".pdf"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
    return name or "prompt"


def pdf_file_name(filename: str) -> str:
    """Итоговое имя PDF, которое увидит ИИ-чат."""
    safe = _ILLEGAL_FS.sub("_", prompt_display_name(filename)).strip(" .")
    return f"{safe or 'prompt'}.pdf"


def cache_key(filename: str) -> str:
    return _ILLEGAL_FS.sub("_", (filename or "").strip()) or "prompt"


TEMPLATE_EXPORT_NAME = "Шаблон.txt"
SHABLON_MANIFEST_KEY = "__shablon_txt__"
REQUIRED_CHAT_FILES = 2

WORKSPACE = SecureWorkspace(prefix="legalyze_")
MANIFEST = PromptCacheManifest(DATA_DIR / "prompt_cache.json")


class AuthWorker(QThread):
    done = pyqtSignal(bool, str, dict)

    @stage
    def __init__(self, login, password, remember):
        super().__init__()
        self.login = login
        self.password = password
        self.remember = remember

    def run(self):
        try:
            res = requests.post(
                f"{SERVER_URL}/api/client/auth",
                json={"login": self.login, "password": self.password},
                timeout=10,
            )
            data = res.json()

            if not data.get("success"):
                self.done.emit(False, data.get("error", "Ошибка"), {})
                return

            self.done.emit(True, "", data.get("data", {}))
        except Exception as e:
            diag.exception("main.py:1422")
            self.done.emit(False, f"Ошибка подключения: {e}", {})


class HWIDWorker(QThread):
    done = pyqtSignal(bool, str)

    @stage
    def __init__(self, token, hwid):
        super().__init__()
        self.token = token
        self.hwid = hwid

    def run(self):
        try:
            res = requests.post(
                f"{SERVER_URL}/api/client/hwid",
                json={"hwid": self.hwid},
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=10,
            )
            data = res.json()

            if data.get("success"):
                self.done.emit(True, "")
            else:
                self.done.emit(False, data.get("error", "Ошибка HWID"))
        except Exception as e:
            diag.exception("main.py:1450")
            self.done.emit(False, str(e))


class BalanceWorker(QThread):
    done = pyqtSignal(bool, int, bool, str)
    ENDPOINTS = ("/api/client/queries", "/api/client/me", "/api/client/profile")

    @stage
    def __init__(self, token):
        super().__init__()
        self.token = token

    def run(self):
        headers = {"Authorization": f"Bearer {self.token}"}
        for ep in self.ENDPOINTS:
            try:
                res = requests.get(f"{SERVER_URL}{ep}", headers=headers, timeout=8)
                if res.status_code != 200:
                    continue
                data = res.json()
                if not isinstance(data, dict) or not data.get("success", True):
                    continue
                payload = data.get("data", data)
                if isinstance(payload, dict) and isinstance(payload.get("user"), dict):
                    payload = payload["user"]
                if not isinstance(payload, dict) or "queriesRemaining" not in payload:
                    continue
                remaining = int(payload.get("queriesRemaining") or 0)
                unlimited = bool(payload.get("hasUnlimited", False))
                unlimited_until = str(payload.get("unlimitedUntil") or "")
                self.done.emit(True, remaining, unlimited, unlimited_until)
                return
            except Exception:
                diag.exception("main.py:1484")
                continue
        self.done.emit(False, 0, False, "")


class DecrementWorker(QThread):
    done = pyqtSignal(bool, int)

    @stage
    def __init__(self, token):
        super().__init__()
        self.token = token

    def run(self):
        try:
            res = requests.post(
                f"{SERVER_URL}/api/client/queries",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=10,
            )
            data = res.json()

            if data.get("success"):
                remaining = int(data.get("data", {}).get("queriesRemaining", 0) or 0)
                self.done.emit(True, remaining)
            else:
                self.done.emit(False, 0)
        except Exception:
            diag.exception("main.py:1512")
            self.done.emit(False, 0)


class PromptLoaderWorker(QThread):
    progress = pyqtSignal(str)
    loaded = pyqtSignal(bool, object)

    @stage
    def __init__(self, cfg, token, force=False):
        super().__init__()
        self.cfg = cfg
        self.token = token
        self.force = force

    def _server_checksum(self, filename: str) -> str:
        try:
            res = requests.head(
                f"{SERVER_URL}/api/client/prompts",
                params={"filename": filename},
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=4,
            )
            return res.headers.get("X-Checksum", "") or res.headers.get("ETag", "").strip('"')
        except Exception:
            diag.exception("main.py:1537")
            return ""

    def _download(self, filename: str, dst: Path) -> bool:
        res = requests.get(
            f"{SERVER_URL}/api/client/prompts",
            params={"filename": filename},
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=20,
        )
        if res.status_code == 200 and res.content:
            dst.write_bytes(res.content)
            return True
        return False

    def _prepare_template_file(self) -> str:
        tpl = normalize_template(self.cfg.get("template", {}) or {})
        tpl_hash = template_hash(tpl)
        entry = MANIFEST.get(SHABLON_MANIFEST_KEY)

        need_write = (
            self.force
            or not TEMPLATE_FILE.exists()
            or entry.get("template_hash") != tpl_hash
        )
        if need_write:
            self.progress.emit("Обновление Шаблон.txt…")
            write_template_file(tpl)
            try:
                file_sha = sha256_bytes(TEMPLATE_FILE.read_bytes())
            except Exception:
                diag.exception("main.py:1568")
                file_sha = ""
            MANIFEST.put(
                SHABLON_MANIFEST_KEY,
                template_hash=tpl_hash,
                file_sha256=file_sha,
                updated_at=int(time.time()),
            )
        else:
            self.progress.emit("Шаблон не изменился — используем готовый Шаблон.txt…")
        return str(TEMPLATE_FILE)

    def run(self):
        try:
            sel = self.cfg.get("selected_prompt", "")
            if not sel:
                self.loaded.emit(False, "Промт не выбран — откройте «Промт» и выберите файл")
                return

            PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
            key = cache_key(sel)
            enc_source = PROMPTS_DIR / key
            enc_pdf = PROMPTS_DIR / f"{key}.pdfenc"
            out_name = pdf_file_name(sel)

            self.progress.emit("Проверка версии промта на сервере…")
            remote_sum = self._server_checksum(sel)
            entry = MANIFEST.get(key)

            local_sum = ""
            if enc_source.exists():
                try:
                    local_sum = sha256_bytes(enc_source.read_bytes())
                except Exception:
                    diag.exception("main.py:1602")
                    local_sum = ""

            unchanged = (
                not self.force
                and enc_pdf.exists()
                and entry.get("pdf_name") == out_name
                and (
                    (remote_sum and entry.get("server_checksum") == remote_sum)
                    or (not remote_sum and entry.get("source_sha256") == local_sum and local_sum)
                )
            )

            pdf_bytes = None

            if unchanged:
                try:
                    self.progress.emit("Промт не изменился — мгновенный экспорт из кэша…")
                    cached = decrypt_blob(enc_pdf.read_bytes())
                    if cached[:4] == b"%PDF":
                        pdf_bytes = cached
                except Exception:
                    diag.exception("main.py:1624")
                    pdf_bytes = None

            if pdf_bytes is None:
                need_download = self.force or not enc_source.exists() or (
                    remote_sum and remote_sum != entry.get("server_checksum", "")
                )

                if need_download:
                    self.progress.emit("Скачивание зашифрованного промта…")
                    try:
                        if not self._download(sel, enc_source) and not enc_source.exists():
                            self.loaded.emit(False, "Сервер не отдал файл промта")
                            return
                    except Exception as e:
                        diag.exception("main.py:1639")
                        if not enc_source.exists():
                            self.loaded.emit(False, f"Нет связи с сервером: {e}")
                            return

                if not enc_source.exists():
                    self.loaded.emit(False, "Файл промта не найден локально")
                    return

                raw = enc_source.read_bytes()
                local_sum = sha256_bytes(raw)

                self.progress.emit("Расшифровка PDF промта…")
                decrypted = decrypt_data_auto(raw)
                if isinstance(decrypted, str):
                    decrypted = decrypted.encode("utf-8")

                if decrypted[:4] == b"%PDF":
                    pdf_bytes = bytes(decrypted)
                else:
                    if FPDF is None:
                        self.loaded.emit(False, "Устаревший текстовый промт на сервере, обновите его до PDF")
                        return
                    self.progress.emit("Старый формат промта — формирование PDF…")
                    pdf_bytes = text_to_pdf_bytes(decrypted, title=prompt_display_name(sel))

                decrypted = b""

                try:
                    enc_pdf.write_bytes(encrypt_blob(pdf_bytes))
                except Exception:
                    diag.exception("main.py:1670")
                    pass

                MANIFEST.put(
                    key,
                    server_checksum=remote_sum or entry.get("server_checksum", ""),
                    source_sha256=local_sum,
                    pdf_name=out_name,
                    pdf_sha256=sha256_bytes(pdf_bytes),
                    updated_at=int(time.time()),
                )

            pdf_path = WORKSPACE.write(out_name, pdf_bytes)
            template_path = self._prepare_template_file()

            self.progress.emit("Экспорт в чат…")
            self.loaded.emit(True, {"pdf": str(pdf_path), "template": template_path})

        except Exception as e:
            diag.exception("main.py:1689")
            self.loaded.emit(False, f"Ошибка подготовки промта: {e}")


class LoginWindow(QDialog):
    login_success = pyqtSignal(dict, str, dict)

    @stage
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg

        self.setWindowTitle("Legalyze — Авторизация")
        self.setFixedSize(380, 320)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Dialog
        )
        self.setStyleSheet(BASE_QSS)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        title = QLabel("Legalyze")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {THEME['accent']};")
        layout.addWidget(title)

        subtitle = QLabel("Вход в аккаунт")
        subtitle.setObjectName("Caption")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)
        layout.addSpacing(6)

        self.login_input = QLineEdit()
        self.login_input.setPlaceholderText("Логин")
        self.login_input.setText(cfg.get("login", ""))
        layout.addWidget(self.login_input)

        self.pass_input = QLineEdit()
        self.pass_input.setPlaceholderText("Пароль")
        self.pass_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass_input.setText(decrypt_secret(cfg.get("password", "")))
        layout.addWidget(self.pass_input)

        self.remember_cb = QCheckBox("Запомнить меня")
        self.remember_cb.setChecked(cfg.get("remember", False))
        layout.addWidget(self.remember_cb)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #f87171; font-size: 11px;")
        self.error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        self.cancel_btn = QPushButton("Отмена")
        self.cancel_btn.setObjectName("Ghost")
        self.cancel_btn.setDefault(False)
        self.cancel_btn.setAutoDefault(False)
        self.cancel_btn.clicked.connect(lambda _checked=False: self.reject())
        btn_layout.addWidget(self.cancel_btn)

        self.login_btn = QPushButton("Войти")
        self.login_btn.setObjectName("Primary")
        self.login_btn.setDefault(False)
        self.login_btn.setAutoDefault(False)
        # Explicit bool discard: compiled Nuitka methods may bypass PyQt slot arity trimming.
        self.login_btn.clicked.connect(lambda _checked=False: self._do_login())
        btn_layout.addWidget(self.login_btn)

        layout.addLayout(btn_layout)

        self._is_logging_in = False
        self._worker = None
        self._hwid_worker = None
        self._auth_data = {}

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._do_login()
            event.accept()
            return
        elif event.key() == Qt.Key.Key_Escape:
            self.reject()
            event.accept()
            return
        super().keyPressEvent(event)

    @pyqtSlot()  # Explicit zero-argument Qt slot: clicked(bool) must not reach @stage.
    @stage
    def _do_login(self):
        if self._is_logging_in:
            return
        if self._worker and self._worker.isRunning():
            return

        login = self.login_input.text().strip()
        password = self.pass_input.text().strip()

        if not login or not password:
            self.error_label.setText("Введите логин и пароль")
            return

        self._is_logging_in = True
        self.login_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.error_label.setText("Подключение…")

        self._worker = AuthWorker(login, password, self.remember_cb.isChecked())
        self._worker.done.connect(self._on_auth)
        self._worker.start()

    @stage
    def _on_auth(self, ok, err, data):
        diag.event("auth.result", success=bool(ok))
        if not ok:
            self._is_logging_in = False
            self.login_btn.setEnabled(True)
            self.cancel_btn.setEnabled(True)
            self.error_label.setText(err)
            return

        self._auth_data = data
        token = data.get("token", "")
        user = data.get("user", {})

        if not token:
            self._is_logging_in = False
            self.login_btn.setEnabled(True)
            self.cancel_btn.setEnabled(True)
            self.error_label.setText("Ошибка: сервер не вернул токен")
            return

        cfg = self.cfg.copy()
        cfg["login"] = self.login_input.text().strip()
        cfg["token"] = token

        if self.remember_cb.isChecked():
            cfg["password"] = encrypt_secret(self.pass_input.text().strip())
            cfg["remember"] = True
        else:
            cfg["password"] = ""
            cfg["remember"] = False

        game_fields = {
            "Name": user.get("gameName", ""),
            "ID": user.get("gameId", ""),
            "Fraction": user.get("gameFraction", ""),
            "Rang": user.get("gameRang", ""),
            "Department": user.get("gameDepartment", ""),
            "JobTitle": user.get("gameJobTitle", ""),
        }
        local_template = cfg.get("template", {}) or {}
        for key, val in game_fields.items():
            if val and str(val).strip():
                local_template[key] = str(val).strip()
        cfg["template"] = normalize_template(local_template)

        save_config(cfg)
        try:
            write_template_file(local_template)
        except Exception:
            diag.exception("main.py:1857")
            pass

        if not user.get("hwidBound"):
            hwid = generate_hwid()
            self.error_label.setText("Регистрация устройства…")
            self._hwid_worker = HWIDWorker(token, hwid)
            self._hwid_worker.done.connect(lambda ok2, err2: self._on_hwid(ok2, err2, cfg, user))
            self._hwid_worker.start()
        else:
            self._is_logging_in = False
            self.login_success.emit(cfg, token, user)
            self.accept()

    @stage
    def _on_hwid(self, ok, err, cfg, user):
        diag.event("hwid.binding_result", success=bool(ok))
        if not ok:
            self._is_logging_in = False
            self.login_btn.setEnabled(True)
            self.cancel_btn.setEnabled(True)
            self.error_label.setText(f"HWID: {err}")
            return

        self._is_logging_in = False
        self.login_success.emit(cfg, cfg.get("token", ""), user)
        self.accept()


class TemplateWindow(QDialog):
    """Вкладка «Шаблон» — выровненная форма (ТЗ п.3)."""

    FIELDS = [
        ("Name", "Имя и фамилия", "Mike Macmillan"),
        ("ID", "ID", "85028"),
        ("Fraction", "Фракция", "SANG"),
        ("Rang", "Ранг", "11"),
        ("Department", "Отдел", "MP"),
        ("JobTitle", "Должность", "Заместитель командующего MP, сенатор NG"),
    ]

    @stage
    def __init__(self, cfg, token="", parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.token = token
        self.setWindowTitle("Шаблон данных")
        self.setFixedSize(560, 480)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Dialog
        )
        self.setStyleSheet(BASE_QSS)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(30, 26, 30, 24)
        root.setSpacing(6)

        title = QLabel("Шаблон данных")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(f"font-size: 19px; font-weight: 700; color: {THEME['accent']};")
        root.addWidget(title)

        hint = QLabel("Поля подставляются в промт перед экспортом в чат")
        hint.setObjectName("Caption")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(hint)

        root.addSpacing(14)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)

        self.fields = {}
        template = cfg.get("template", {}) or {}

        for key, caption, placeholder in self.FIELDS:
            lbl = QLabel(f"{caption}")
            lbl.setMinimumWidth(130)
            lbl.setStyleSheet(f"color: {THEME['muted']}; font-size: 13px;")
            inp = QLineEdit()
            inp.setPlaceholderText(placeholder)
            val = template.get(key, "")
            inp.setText(val if val is not None else "")
            inp.setMinimumHeight(40)
            inp.setMinimumWidth(300)
            self.fields[key] = inp
            form.addRow(lbl, inp)

        root.addLayout(form)
        root.addStretch()

        self.sync_label = QLabel("")
        self.sync_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sync_label.setStyleSheet("color: #f59e0b; font-size: 11px;")
        root.addWidget(self.sync_label)

        btn = QPushButton("Сохранить")
        btn.setObjectName("Primary")
        btn.setMinimumHeight(46)
        btn.setMinimumWidth(200)
        btn.clicked.connect(lambda _checked=False: self._save())

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("Отмена")
        cancel_btn.setObjectName("Ghost")
        cancel_btn.setMinimumHeight(46)
        cancel_btn.setMinimumWidth(200)
        cancel_btn.clicked.connect(lambda _checked=False: self.reject())
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(btn)
        btn_layout.addStretch()
        root.addLayout(btn_layout)

    def _save(self):
        save_template(self.cfg, self.get_template())
        self._sync_to_server()
        self.accept()

    def _sync_to_server(self):
        if not self.token:
            return
        try:
            template = self.get_template()
            requests.post(
                f"{SERVER_URL}/api/client/template",
                json={"template": template},
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=8,
            )
        except Exception:
            diag.exception("main.py:1996")
            pass

    def get_template(self):
        return {k: v.text().strip() for k, v in self.fields.items()}


class HotkeyDialog(QDialog):
    @stage
    def __init__(self, current_key, title="Горячая клавиша", taken=None, parent=None):
        super().__init__(parent)

        self.selected = current_key
        self.taken = set(taken or [])

        self.setWindowTitle(title)
        self.setFixedSize(360, 250)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Dialog
        )
        self.setStyleSheet(BASE_QSS)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)

        head = QLabel(title)
        head.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {THEME['accent']};")
        layout.addWidget(head)

        self.capture_label = QLabel("Нажмите любую клавишу…")
        self.capture_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.capture_label.setMinimumHeight(54)
        self.capture_label.setStyleSheet(
            f"background: {THEME['bg2']}; border: 1px dashed {THEME['accent']};"
            f"border-radius: 10px; font-size: 20px; font-weight: 700; color: {THEME['text']};"
        )
        layout.addWidget(self.capture_label)

        cap = QLabel("или выберите из списка")
        cap.setObjectName("Caption")
        cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(cap)

        self.combo = QComboBox()
        self.combo.addItems(list(VK_MAP.keys()))
        if current_key in VK_MAP:
            self.combo.setCurrentText(current_key)
            self.capture_label.setText(current_key)
        self.combo.currentTextChanged.connect(self._on_combo)
        layout.addWidget(self.combo)

        self.error = QLabel("")
        self.error.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error.setStyleSheet("color: #f87171; font-size: 11px;")
        layout.addWidget(self.error)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        cancel_btn = QPushButton("Отмена")
        cancel_btn.setObjectName("Ghost")
        cancel_btn.clicked.connect(lambda _checked=False: self.reject())
        btn_layout.addWidget(cancel_btn)

        self.ok_btn = QPushButton("Применить")
        self.ok_btn.setObjectName("Primary")
        self.ok_btn.clicked.connect(lambda _checked=False: self._accept_if_valid())
        btn_layout.addWidget(self.ok_btn)

        layout.addLayout(btn_layout)
        self.setFocus()

    def _on_combo(self, text):
        self.selected = text
        self.capture_label.setText(text)
        self._validate()

    def keyPressEvent(self, event):
        if event.isAutoRepeat():
            return

        key = Qt.Key(event.key())
        if key in (Qt.Key.Key_Escape,):
            self.reject()
            return

        name = None
        # 1. Попытка определить по аппаратному коду клавиши Windows (Win32 Virtual Key)
        # Это работает одинаково надежно на любой раскладке клавиатуры
        try:
            vk = event.nativeVirtualKey()
            if vk:
                for k, code in VK_MAP.items():
                    if code == vk:
                        name = k
                        break
        except Exception:
            diag.exception("main.py:2098")
            pass

        # 2. Попытка определить через Qt Key enum
        if not name:
            name = QT_KEY_TO_NAME.get(key)

        # 3. Попытка сопоставить через русско-английскую раскладку
        if not name:
            ru_char = event.text().upper()
            mapped = RU_TO_EN_KEY.get(ru_char)
            if mapped and mapped in VK_MAP:
                name = mapped
            elif ru_char in VK_MAP:
                name = ru_char

        if not name:
            self.error.setText("Эта клавиша не поддерживается")
            return

        self.selected = name
        self.capture_label.setText(name)
        self.combo.blockSignals(True)
        self.combo.setCurrentText(name)
        self.combo.blockSignals(False)
        self._validate()

    def _validate(self) -> bool:
        if self.selected in self.taken:
            self.error.setText("Эта клавиша уже занята другой функцией")
            self.ok_btn.setEnabled(False)
            return False
        self.error.setText("")
        self.ok_btn.setEnabled(True)
        return True

    def _accept_if_valid(self):
        if self.selected and self._validate():
            self.accept()

    def get_key(self):
        return self.selected


class HotkeyChip(QWidget):
    clicked = pyqtSignal()

    @stage
    def __init__(self, caption: str, key: str, parent=None):
        super().__init__(parent)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)

        self.caption = QLabel(caption)
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.caption.setStyleSheet(
            f"color: {THEME['muted']}; font-size: 9px; letter-spacing: 0.4px; border: none;"
        )
        lay.addWidget(self.caption)

        self.button = QPushButton(key)
        self.button.setFixedHeight(22)
        self.button.setMinimumWidth(58)
        self.button.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._apply_style(False)
        self.button.clicked.connect(lambda _checked=False: self.clicked.emit())
        lay.addWidget(self.button)

    def _apply_style(self, active: bool):
        if not self.button.isEnabled():
            self.button.setStyleSheet("""
                QPushButton {
                    background: rgba(255, 255, 255, 0.05);
                    color: #555875;
                    border: 1px solid #262a4a;
                    border-radius: 6px;
                    font-size: 11px;
                    font-weight: 700;
                    padding: 1px 6px;
                }
            """)
            return

        if active:
            self.button.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(52, 211, 153, 0.28);
                    color: #a7f3d0;
                    border: 1px solid {THEME['ok']};
                    border-radius: 6px;
                    font-size: 11px;
                    font-weight: 700;
                    padding: 1px 6px;
                }}
            """)
        else:
            self.button.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(124, 131, 255, 0.16);
                    color: #cdd2ff;
                    border: 1px solid rgba(124, 131, 255, 0.55);
                    border-radius: 6px;
                    font-size: 11px;
                    font-weight: 700;
                    padding: 1px 6px;
                }}
                QPushButton:hover {{
                    background: rgba(124, 131, 255, 0.32);
                    border-color: rgba(124, 131, 255, 0.85);
                    color: #ffffff;
                }}
            """)

    def set_key(self, key: str):
        self.button.setText(key)

    def set_active(self, active: bool):
        self._apply_style(active)


class LoadingOverlay(QWidget):
    @stage
    def __init__(self, parent_window):
        super().__init__(None)
        self._owner = parent_window
        self._angle = 0.0
        self._pulse = 0.0
        self._title = "Подготовка промта"
        self._message = "Пожалуйста, подождите…"
        self._error = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setCursor(Qt.CursorShape.BusyCursor)

        self._anim = QTimer(self)
        self._anim.timeout.connect(self._tick)
        self._anim.setInterval(16)

    def show_state(self, title: str, message: str, error: bool = False):
        if (
            self.isVisible()
            and self._title == title
            and self._message == message
            and self._error == error
        ):
            if not self._anim.isActive():
                self._anim.start()
            return
        self._title = title
        self._message = message
        self._error = error
        self.sync_geometry()
        if not self.isVisible():
            self.show()
        if not self._anim.isActive():
            self._anim.start()
        force_topmost(int(self.winId()))
        self.update()

    def set_message(self, message: str):
        self._message = message
        self.update()

    def finish(self):
        self._anim.stop()
        self.hide()

    def sync_geometry(self):
        try:
            owner = self._owner
            if not owner:
                return
            g = owner.geometry()
            top = owner.top_inset
            bottom = owner.bottom_inset
            self.setGeometry(g.x(), g.y() + top, g.width(), g.height() - top - bottom)
        except Exception:
            diag.exception("main.py:2285")
            pass

    def _tick(self):
        self._angle = (self._angle + 4.2) % 360.0
        self._pulse = (self._pulse + 0.035) % (2 * math.pi)
        self.update()

    def mousePressEvent(self, event):
        event.accept()

    def mouseDoubleClickEvent(self, event):
        event.accept()

    def wheelEvent(self, event):
        event.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = self.rect()
        p.fillRect(rect, QColor(9, 11, 24, 238))

        cx = rect.width() / 2
        cy = rect.height() / 2

        glow = QColor(124, 131, 255, 26)
        radius = 90 + 12 * math.sin(self._pulse)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(glow))
        p.drawEllipse(QRectF(cx - radius, cy - radius - 40, radius * 2, radius * 2))

        card_w = min(rect.width() - 40, 340)
        card_h = 210
        card = QRectF(cx - card_w / 2, cy - card_h / 2, card_w, card_h)
        path = QPainterPath()
        path.addRoundedRect(card, 16, 16)
        p.setBrush(QBrush(QColor(21, 24, 48, 245)))
        p.setPen(QPen(QColor(38, 42, 74), 1))
        p.drawPath(path)

        spin_r = 26.0
        spin_rect = QRectF(cx - spin_r, card.top() + 34, spin_r * 2, spin_r * 2)

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(42, 47, 84), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(spin_rect, 0, 360 * 16)

        accent = QColor(239, 68, 68) if self._error else QColor(124, 131, 255)
        p.setPen(QPen(accent, 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        span = int((110 + 40 * math.sin(self._pulse)) * 16)
        p.drawArc(spin_rect, int(-self._angle * 16), span)

        p.setPen(QColor(230, 232, 245))
        f = QFont("Segoe UI", 12, QFont.Weight.Bold)
        p.setFont(f)
        title_rect = QRectF(card.left() + 16, card.top() + 100, card.width() - 32, 26)
        p.drawText(title_rect, int(Qt.AlignmentFlag.AlignCenter), self._title)

        p.setPen(QColor(143, 150, 191))
        p.setFont(QFont("Segoe UI", 9))
        msg_rect = QRectF(card.left() + 18, card.top() + 128, card.width() - 36, 52)
        p.drawText(
            msg_rect,
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap),
            self._message,
        )

        bar_w = card.width() - 44
        bar = QRectF(card.left() + 22, card.bottom() - 26, bar_w, 4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(38, 42, 74))
        p.drawRoundedRect(bar, 2, 2)

        seg_w = bar_w * 0.32
        pos = (self._angle / 360.0) * (bar_w + seg_w) - seg_w
        seg = QRectF(bar.left() + max(0.0, pos), bar.top(),
                     min(seg_w, bar_w - max(0.0, pos)), bar.height())
        p.setBrush(accent)
        p.drawRoundedRect(seg, 2, 2)

        p.end()


class TemplateSyncWorker(QThread):
    done = pyqtSignal(dict)

    @stage
    def __init__(self, token, parent=None):
        super().__init__(parent)
        self.token = token

    def run(self):
        try:
            response = requests.get(f"{SERVER_URL}/api/client/template",
                                    headers={"Authorization": f"Bearer {self.token}"}, timeout=8)
            response.raise_for_status()
            data = response.json()
            template = data.get("data", {}).get("template") if data.get("success") else None
            if isinstance(template, dict):
                self.done.emit(template)
        except Exception:
            diag.exception("template.sync")


class NativeHost(QObject):
    """Owns the real browser process and its embedded window.

    The page's own microphone lives in the browser, not in Qt: Web Speech API
    and getUserMedia+upload are wired to the vendor's cloud service and only
    exist in branded builds. So the browser runs out of process and its window
    is parented into the Qt placeholder.

    Every step is supervised, which is what the old `release/` build lacked:
    * DevTools never answers   -> retry with a fresh profile (a stale singleton
      owning the profile makes a new Chromium exit immediately),
    * still nothing            -> retry with `--disable-gpu` (Parsec/hybrid GPU
      stalls were one of the reported hangs),
    * still nothing            -> next installed browser.
    Any embedding failure is rolled back, so the user never keeps an invisible
    or detached browser window.
    """

    POLL_MS = 250
    DEVTOOLS_TIMEOUT = 25.0
    WINDOW_TIMEOUT = 30.0
    MAX_ATTEMPTS = 6

    ready = pyqtSignal(int)     # DevTools port
    failed = pyqtSignal(str)    # human readable reason

    def __init__(self, parent=None, url=URL, profile=None, app_dir=None,
                 width=W, height=H, configured=None, lang="ru-RU",
                 light_theme=True, accept_languages=None):
        super().__init__(parent)
        # v18: язык речевого ввода и светлая тема страницы.
        self.lang = str(lang or "ru-RU")
        self.light_theme = bool(light_theme)
        self.accept_languages = accept_languages
        self.url = url
        self.width = int(width)
        self.height = int(height)
        self.configured = configured
        self.placeholder = None
        self.candidates = []
        self.index = 0
        self.attempt = 0          # 0 = normal, 1 = fresh profile, 2 = GPU off
        self.attempts = 0
        self.gpu_off = os.environ.get("LEGALYZE_DISABLE_GPU") == "1"
        self.native = None
        self.hwnd = None
        self.port = None
        self.stage = "idle"
        self.stage_deadline = 0.0
        self.pids = set()
        # v15: сдвиг/размер окна браузера внутри плейсхолдера, физические px.
        self.inset = (0, 0, 0, 0)
        self.user32 = None
        self.kernel32 = None
        self.focus_bridge = None
        # v17: виджет-шторка приложения. Её hwnd передаётся в `embed`, чтобы
        # поднять шторку ДО показа окна браузера.
        self.cover_widget = None
        self.app_dir = Path(app_dir) if app_dir else Path(sys.argv[0]).resolve().parent
        self.profile = Path(profile) if profile else BROWSER_PROFILE
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self._init_win32()

    def _init_win32(self):
        if sys.platform != "win32":
            return
        try:
            self.user32 = ctypes.WinDLL("user32", use_last_error=True)
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            win32_embed.configure(self.user32)
            browser_focus.configure(self.user32, self.kernel32)
            self.focus_bridge = browser_focus.BrowserFocusBridge(self.user32, self.kernel32)
        except Exception:
            diag.exception("main.native_win32")
            self.user32 = None

    # ------------------------------------------------------------- lifecycle
    def start(self, placeholder):
        self.placeholder = placeholder
        self.candidates = discover_browsers(self.app_dir, self.configured)
        if not self.candidates:
            diag.event("browser.no_candidate", app_dir=str(self.app_dir))
            self.failed.emit("no-browser")
            return False
        self._spawn()
        return True

    def _spawn(self):
        if self.attempts >= self.MAX_ATTEMPTS:
            self.failed.emit("attempts-exhausted")
            return
        if self.index >= len(self.candidates):
            self.failed.emit("no-browser")
            return
        self.attempts += 1
        candidate = self.candidates[self.index]
        self.native = NativeBrowser(url=self.url, profile_dir=self.profile,
                                    app_dir=self.app_dir, gpu=not self.gpu_off,
                                    attempt=self.attempt,
                                    lang=self.lang,
                                    light_theme=self.light_theme,
                                    accept_languages=self.accept_languages)
        diag.event("browser.try", name=candidate.name, kind=candidate.kind,
                   attempt=self.attempts, gpu=not self.gpu_off)
        try:
            # Off-screen staging: the window is created (so it can be found)
            # but is never visible before the Qt cover is on top of it.
            self.native.launch(candidate, self.width, self.height, staging=(-8192, -8192))
        except Exception:
            diag.exception("main.browser_spawn")
            self._retry("spawn")
            return
        self.pids = {int(self.native.proc.pid)}
        _pid_journal_write(self.pids)
        self.stage = "devtools"
        self.stage_deadline = time.monotonic() + self.DEVTOOLS_TIMEOUT
        self.timer.start(self.POLL_MS)

    def _tick(self):
        if self.native is None:
            self.timer.stop()
            return
        if self.stage == "devtools":
            state, version, targets = self.native.probe()
            if state == "exited":
                self._retry("browser-exited")
                return
            if state == "ready":
                page = choose_target(targets or [])
                if page is None:
                    if time.monotonic() > self.stage_deadline:
                        self._retry("no-page")
                    return
                self.port = self.native.port
                diag.event("browser.devtools", browser=(version or {}).get("Browser"),
                           url=str(page.get("url"))[:120])
                self.stage = "window"
                self.stage_deadline = time.monotonic() + self.WINDOW_TIMEOUT
                return
            if time.monotonic() > self.stage_deadline:
                self._retry("devtools-timeout")
            return
        if self.stage == "window":
            self.refresh_pids()
            _pid_journal_write(self.pids)
            hwnd = win32_embed.find_browser_window(self.user32, self.pids, timeout=0.5)
            if hwnd:
                self._embed(hwnd)
                return
            if time.monotonic() > self.stage_deadline:
                self._retry("window-timeout")

    @diag.trace
    def _embed(self, hwnd):
        parent = int(self.placeholder.winId()) if self.placeholder else 0
        if not parent:
            self._retry("no-placeholder")
            return
        # Скрыть ДО встраивания: без этого окно на мгновение появляется
        # настоящим окном (со всеми элементами браузера) и попадает в Alt+Tab.
        win32_embed.hide_window(self.user32, hwnd)
        # v17: hwnd шторки — `embed` поднимет её до показа окна браузера.
        cover = 0
        if self.user32 is not None and self.cover_widget is not None:
            try:
                cover = int(self.cover_widget.winId())
            except Exception:
                diag.exception("main.cover_hwnd")
                cover = 0
        if not win32_embed.embed(self.user32, hwnd, parent, self.inset, cover=cover):
            self._retry("embed-failed")
            return
        self.hwnd = hwnd
        self.stage = "ready"
        self.timer.stop()
        win32_embed.sync(self.user32, hwnd, parent, self.inset)
        # v15: панель задач. Chrome сам возвращает свою кнопку через
        # ITaskbarList::AddTab и не сразу, поэтому скрываем сразу и повторяем.
        self.hide_taskbar()
        # Chrome поднимает окна и возвращает кнопку не сразу: первые секунды
        # проверяем часто, потом достаточно одного таймера раз в 5 с.
        for delay in (300, 800, 1500, 2500, 4000):
            QTimer.singleShot(delay, self.hide_taskbar)
        QTimer.singleShot(1200, self.hide_taskbar)
        QTimer.singleShot(4000, self.hide_taskbar)
        diag.event("browser.embedded", hwnd=int(hwnd), parent=parent, port=self.port,
                   exe=self.native.exe, kind=self.native.kind, inset=list(self.inset))
        self.ready.emit(int(self.port))

    @diag.trace
    def hide_taskbar(self):
        """Убрать из панели задач и встроенное окно, и все лишние окна браузера.

        Безопасна при повторном вызове: стиль меняется только если он уже не
        тот, а DeleteTab идемпотентен. Дерево pid пересчитывается каждый раз,
        иначе всплывшие позже окна остаются висеть.
        """
        if not self.hwnd or not self.user32:
            return
        self.refresh_pids()
        try:
            win32_embed.hide_from_taskbar(self.user32, self.hwnd,
                                          delete_tab=bool(TASKBAR_DELETE_TAB))
            self._hide_strays(keep=self.hwnd)
        except Exception:
            diag.exception("main.hide_taskbar")

    def refresh_pids(self):
        """Пересчитать дерево pid.

        После `ready` таймер останавливается, и `pids` застывает: процессы,
        поднятые позже (рендереры, утилиты), в него не попадают, и их окна
        остаются висеть в Alt+Tab.
        """
        proc = getattr(self.native, "proc", None)
        if proc is None:
            return
        try:
            root = int(proc.pid)
            self.pids |= {root} | win32_embed.descendant_pids(root)
        except Exception:
            diag.exception("main.refresh_pids")

    def _hide_strays(self, keep=0):
        """Скрыть (и закрыть) все окна браузера, кроме встроенного."""
        if not self.user32:
            return
        try:
            win32_embed.hide_stray_windows(self.user32, self.pids, keep=keep,
                                           close_after_hide=bool(CLOSE_STRAY_WINDOWS),
                                           delete_tab=bool(TASKBAR_DELETE_TAB))
        except Exception:
            diag.exception("main.hide_strays")

    def _kill_all(self):
        """Снять ВСЁ дерево браузера.

        `proc.terminate()` убивает только родителя: рендереры, GPU и утилиты
        выживают, их окна остаются — из-за этого «плодятся браузеры».
        Чужие процессы не трогаем: только pid своего дерева.
        """
        self.refresh_pids()
        pids = set(int(p) for p in (self.pids or ()))
        proc = getattr(self.native, "proc", None)
        if proc is not None:
            try:
                pids.add(int(proc.pid))
            except Exception:
                pass
        if not pids:
            return
        try:
            win32_embed.hide_stray_windows(self.user32, pids)
        except Exception:
            diag.exception("main.hide_stray_before_kill")
        try:
            win32_embed.kill_tree(pids)
        except Exception:
            diag.exception("main.kill_tree")

    def _retry(self, reason):
        self.timer.stop()
        diag.event("browser.retry", reason=reason, index=self.index,
                   attempt=self.attempts, gpu=not self.gpu_off)
        self._kill_all()
        if self.native is not None:
            self.native.close()
            self.native = None
        self.hwnd = None
        self.port = None
        # 1st: same browser, fresh profile. 2nd: GPU off. 3rd: next browser.
        if self.attempt == 0:
            self.attempt = 1
        elif self.attempt == 1:
            self.attempt = 2
            self.gpu_off = True
        else:
            self.index += 1
            self.attempt = 0
            self.gpu_off = os.environ.get("LEGALYZE_DISABLE_GPU") == "1"
            if self.index >= len(self.candidates):
                self.failed.emit(reason)
                return
        self._spawn()

    def restart_next(self):
        """Switch to the next browser (used when this build cannot do the
        page's voice input)."""
        self.timer.stop()
        self._kill_all()
        if self.native is not None:
            self.native.close()
            self.native = None
        self.hwnd = None
        self.port = None
        self.index += 1
        self.attempt = 0
        self.attempts = 0
        if self.index >= len(self.candidates):
            return False
        self._spawn()
        return True

    # --------------------------------------------------------------- helpers
    def sync(self):
        if not (self.hwnd and self.placeholder):
            return
        try:
            win32_embed.sync(self.user32, self.hwnd, int(self.placeholder.winId()),
                             self.inset)
        except Exception:
            diag.exception("main.sync_geometry")

    def poll_focus(self, host_hwnd, enabled):
        if self.focus_bridge is None or not self.hwnd:
            return
        try:
            self.focus_bridge.poll(host_hwnd, self.hwnd, enabled)
        except Exception:
            diag.exception("main.focus_poll")

    def alive(self):
        return self.native is not None and self.native.alive()

    def stop(self):
        self.timer.stop()
        self._kill_all()
        if self.native is not None:
            self.native.close()
            self.native = None
        self.hwnd = None
        self.port = None
        self.stage = "stopped"


class BrowserFetchWorker(QThread):
    """Скачивает официальный Chrome for Testing (v18).

    Зачем: ШТАТНЫЙ микрофон страницы работает только в настоящем Chrome.
    Если его нет, приложение уходит во встроенный движок, а там запись идёт
    через Windows System.Speech — и пользователь видит «В Windows не
    установлен распознаватель речи». Скачивание устраняет ПРИЧИНУ.
    """

    progress = pyqtSignal(float, str)
    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, dest, parent=None):
        super().__init__(parent)
        self.dest = Path(dest)
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            exe = browser_fetch.install(
                self.dest,
                report=lambda ratio, text: self.progress.emit(float(ratio), str(text)),
                cancel=lambda: self._cancel)
            self.succeeded.emit(str(exe))
        except Exception as exc:
            self.failed.emit(str(exc))


def app_dir() -> Path:
    r"""Папка приложения: для exe — рядом с ним, для исходников — рядом с main.py.

    Именно здесь `discover_browsers` ищет закреплённую сборку `browser\chrome.exe`.
    """
    try:
        return Path(sys.argv[0]).resolve().parent
    except Exception:
        diag.exception("main.app_dir")
        return Path.cwd()


class ChromeWorker(QThread):
    """CDP automation only: Qt owns the embedded view and browser processes."""
    hwnd_ready = pyqtSignal(object)
    failed = pyqtSignal()

    def __init__(self, parent=None, port=None, native=False, size=None,
                 lang="ru-RU", light=True):
        super().__init__(parent)
        self.proc = None
        self.browser = None
        self.page = None
        self.failure = {}
        self.port = port
        self.native = native
        # (physical width, height) of the placeholder: the zoom is computed from
        # it, so the layout is identical at 100 %, 125 % and 150 %.
        self.size = size
        # v18: язык речевого ввода и тема страницы (приходят из MainWindow).
        self.lang = lang
        self.light = light
        # v19: отпечаток поверхности (заполняется воротами готовности).
        self.surface = {}

    @diag.trace
    def run(self):
        try:
            self.browser, self.page = attach_chrome(self.port or DEBUG_PORT, timeout=30)
            try:
                install_compat(self.browser, self.page, native=self.native)
                grant_mic_permission(self.browser, self.page)
            except Exception:
                diag.exception("webengine.compat_stage")
            # v19: ворота готовности. Раньше «чатом» считалось любое текстовое
            # поле на странице с `readyState == "interactive"` — GUI прогрузки
            # снимали на недогруженной странице (именно это и описано в
            # «Error - 1»: «браузер как будто не прогрузился»).
            self.surface = wait_for_chat_surface(
                self.page, timeout=SURFACE_READY_TIMEOUT, lang=self.lang,
                light=self.light, should_stop=self.isInterruptionRequested)
            diag.state("page.verdict", self.surface.get("verdict"))
            diag.state("page.ready", bool(self.surface.get("ready")))
            diag.state("page.blocked", self.surface.get("blocked") or "")
            try:
                install_purge(self.page, lang=self.lang, light=self.light)
                if self.native:
                    self._apply_zoom()
                    grant_microphone(self.browser, self.page)
            except Exception:
                diag.exception("webengine.purge_stage")
            diag.event("webengine.chat_ready",
                       ready=bool(self.surface.get("ready")),
                       verdict=self.surface.get("verdict"),
                       blocked=self.surface.get("blocked") or "",
                       rounds=self.surface.get("rounds"),
                       theme_repairs=self.surface.get("theme_repairs"))
            # Чатом это не оказалось (вход / согласие / залипание) — приложение
            # НЕ падает: страница показывается, а причина уходит в лог и в
            # строку состояния, чтобы пользователь мог войти в аккаунт.
            self.hwnd_ready.emit(True)
            return
        except Exception as exc:
            self.failure = {"phase": "web_page", "type": type(exc).__name__, "exit_code": None}
            diag.exception("webengine.worker")
            if not self.isInterruptionRequested():
                self.failed.emit()

    def _apply_zoom(self):
        """Native 67 % zoom, measured — never assumed (see native_browser.apply_zoom)."""
        logical_w, logical_h, physical_w, physical_h = self.size or (W, H, W, H)
        if not logical_w or not logical_h:
            return
        try:
            result = apply_zoom(self.page, int(logical_w), int(logical_h),
                                int(physical_w or logical_w), int(physical_h or logical_h))
        except Exception:
            diag.exception("webengine.zoom")
            return
        diag.event("webengine.zoom_applied", source=(result or {}).get("source"),
                   logical=(int(logical_w), int(logical_h)),
                   physical=(int(physical_w or logical_w), int(physical_h or logical_h)))

    def close_connections(self):
        for connection in (self.page, self.browser):
            if connection:
                try:
                    connection.close()
                except Exception:
                    diag.exception("webengine.close_connection")



class UploadThread(QThread):
    """Держит в чате ОБА файла: PDF промт и Шаблон.txt (ТЗ 1.3)."""

    decrement_signal = pyqtSignal()
    attached = pyqtSignal(bool)
    failed = pyqtSignal(str)
    #: v19: «на этой странице вложение не работает» — приходит с вердиктом.
    blocked = pyqtSignal(str)

    @stage
    def __init__(self, page, pdf_path, template_path, token, page_provider=None):
        super().__init__()
        self.page = page
        # CDP-соединение умирает при навигации/перезагрузке страницы: поток
        # берёт актуальный page у воркера, иначе он вечно долбится в мёртвый
        # websocket и файлы не появляются до ручного Ctrl+R (v6, лог 20260930).
        self.page_provider = page_provider
        self.pdf_path = pdf_path
        self.template_path = template_path
        self.token = token

        self._stop = False
        self.last_upload = 0
        self.files_loaded = False
        self._dedup_done = False
        self.wait_started = time.monotonic()
        self._next_diagnostic = 0.0
        self.fails = 0
        self.end_check_counter = 0
        # v19: сколько проверок подряд в чате НИ ОДНОГО файла. Если порог
        # превышен — это не «медленно грузится», а «на этой поверхности вложение
        # не работает» (жалоба «Error - 1»: .pdf и .txt не экспортировались).
        self.empty_rounds = 0
        self._last_block_log = 0.0

    def _report_blocked(self, state):
        """Один раз на `UPLOAD_BLOCK_LOG_EVERY` секунд: почему файлов нет."""
        now = time.monotonic()
        if now - self._last_block_log < float(UPLOAD_BLOCK_LOG_EVERY):
            return
        self._last_block_log = now
        surface = probe_page_surface(self.page)
        verdict = str(surface.get("verdict") or "unknown")
        diag.warn("upload_blocked", verdict=verdict, empty_rounds=self.empty_rounds,
                  files=surface.get("files"), chips=surface.get("chips"),
                  composer=surface.get("composer"), ready=surface.get("ready"),
                  account=surface.get("account"), pending=surface.get("pending"),
                  probe_ok=bool(state.get("probe_ok")),
                  uploading=bool(state.get("uploading")), fails=self.fails)
        # v20: один раз на поток — снимок «кода» страницы, на которой вложение
        # не работает. Именно этот файл показывает, ЧТО за поверхность отдал
        # сервис (например, режим ИИ в поиске без input[type=file]).
        if not getattr(self, "_trace_code_done", False):
            self._trace_code_done = True
            try:
                btrace.capture_page(self.page, reason="upload_blocked-%s" % verdict,
                                    full=True)
            except Exception:
                diag.exception("main.upload_blocked_capture")
        try:
            self.blocked.emit(verdict)
        except Exception:
            diag.exception("main.upload_blocked_signal")
        # Страницу без толку долбить не надо: вложений там нет.
        time.sleep(1.5)

    def _missing_files(self, names):
        missing = []
        try:
            pdf_name = Path(self.pdf_path).name if self.pdf_path else ""
        except Exception:
            diag.exception("main.py:2470")
            pdf_name = ""
        if pdf_name and self.pdf_path and not _chat_has_file(names, pdf_name):
            missing.append(self.pdf_path)
        if self.template_path and not _chat_has_file(names, TEMPLATE_EXPORT_NAME):
            missing.append(self.template_path)
        return missing

    def _has_both_files(self, names):
        try:
            pdf_name = Path(self.pdf_path).name if self.pdf_path else ""
        except Exception:
            diag.exception("main.py:2482")
            pdf_name = ""
        return bool(
            pdf_name
            and _chat_has_file(names, pdf_name)
            and _chat_has_file(names, TEMPLATE_EXPORT_NAME)
        )

    def _refresh_page(self):
        if self.page_provider is None:
            return
        try:
            fresh = self.page_provider()
        except Exception:
            diag.exception("main.upload_page_provider")
            return
        if fresh is not None and fresh is not self.page:
            self.page = fresh
            diag.event("upload.page_refreshed")

    def run(self):
        while not self._stop:
            try:
                self._refresh_page()
                if self.wait_started is not None and time.monotonic() - self.wait_started >= 90:
                    self.failed.emit("attachment_deadline")
                    return
                pdf_ok = bool(self.pdf_path) and Path(self.pdf_path).exists()
                tpl_ok = bool(self.template_path) and Path(self.template_path).exists()
                if not self.page or not pdf_ok or not tpl_ok:
                    self.attached.emit(False)
                    time.sleep(0.2)
                    continue

                state = check_state(self.page)
                # v19: считаем проверки, на которых в чате нет ни одного файла.
                # Раньше поток молча повторял впрыск десятки раз подряд (в логе
                # «Error - 1» — 92 попытки и ни одной ошибки), и по логу было
                # невозможно понять, что вложение вообще не работает.
                if int(state.get("present", 0) or 0) == 0:
                    self.empty_rounds += 1
                else:
                    self.empty_rounds = 0
                if (self.empty_rounds >= int(UPLOAD_BLOCK_THRESHOLD)
                        and self.wait_started is not None
                        and time.monotonic() - self.wait_started > 12):
                    self._report_blocked(state)
                if time.monotonic() >= self._next_diagnostic:
                    self._next_diagnostic = time.monotonic() + 5
                    diag.event("upload.state", present=state.get("present"), uploading=state.get("uploading"),
                               probe_ok=state.get("probe_ok", False), pdf_exists=pdf_ok, template_exists=tpl_ok,
                               attempts_failed=self.fails)
                    log_page_probe(self.page)

                if state["uploading"]:
                    time.sleep(0.05)
                    continue

                present = int(state.get("present", 0) or 0)
                names = state.get("names", []) or []
                names_ok = any(str(n or "").strip() for n in names)

                if names_ok:
                    missing = self._missing_files(names)
                    complete = present >= REQUIRED_CHAT_FILES and not missing
                else:
                    missing = []
                    complete = present >= REQUIRED_CHAT_FILES

                if complete:
                    self.wait_started = None
                    if (
                        names_ok
                        and not self._dedup_done
                        and present > REQUIRED_CHAT_FILES + 2
                        and self._has_both_files(names)
                    ):
                        self._dedup_done = True
                        self.files_loaded = False
                        self.attached.emit(False)
                        try:
                            self.page.eval(JS_REMOVE_ALL_FILES, timeout=3)
                        except Exception:
                            diag.exception("main.py:2530")
                            pass
                        self.last_upload = 0
                        time.sleep(0.8)
                        continue
                    if not self.files_loaded:
                        self.attached.emit(True)
                    self.files_loaded = True
                    self.fails = 0
                    # v20: «оба файла на месте» подтверждается замером ещё раз —
                    # теперь уже по финальному состоянию чата (имена, чипы),
                    # а не по счётчику.
                    try:
                        btrace.audit_export(
                            self.page, "complete",
                            [self.pdf_path, self.template_path],
                            extra={"present": int(present),
                                   "names": [str(n)[:80] for n in (names or [])][:8]})
                    except Exception:
                        diag.exception("main.audit_complete")
                    time.sleep(0.15)
                    continue

                if self.wait_started is None:
                    self.wait_started = time.monotonic()
                if self.files_loaded:
                    self.files_loaded = False
                    self.last_upload = 0
                    self.end_check_counter = 0
                    self.attached.emit(False)

                if (time.time() - self.last_upload) > 0.8:
                    time.sleep(random.uniform(0.05, 0.2))

                    if names_ok:
                        to_upload = list(missing) or (
                            [self.pdf_path, self.template_path]
                            if present < REQUIRED_CHAT_FILES else []
                        )
                    elif present == 0:
                        to_upload = [self.pdf_path, self.template_path]
                    else:
                        try:
                            self.page.eval(JS_REMOVE_ALL_FILES, timeout=3)
                        except Exception:
                            diag.exception("main.py:2562")
                            pass
                        self.last_upload = time.time()
                        time.sleep(0.6)
                        continue

                    if not to_upload:
                        time.sleep(0.15)
                        continue

                    ok = True
                    for path in to_upload:
                        if self._stop:
                            return
                        started_attempt = time.monotonic()
                        kind = "pdf" if path == self.pdf_path else "template"
                        try:
                            size = Path(path).stat().st_size if path else 0
                        except Exception:
                            size = 0
                        injected = upload_file_advanced(self.page, path)
                        # v19: попытка видна целиком — чем впрыскивали, сколько
                        # весило, сколько заняло и во что (сколько в странице
                        # полей для файла). Без этого «файлы не экспортируются»
                        # остаётся непроверяемым утверждением.
                        diag.event("upload.attempt", kind=kind, injected=bool(injected),
                                   bytes=int(size),
                                   ms=round((time.monotonic() - started_attempt) * 1000),
                                   empty_rounds=int(self.empty_rounds),
                                   fails=int(self.fails),
                                   name=Path(path).name if path else "")
                        # v20: доказательство экспорта по каждому вложению —
                        # файл на диске (размер/sha), результат впрыска, что
                        # оказалось в input[type=file] и в чипах чата.
                        try:
                            btrace.audit_export(
                                self.page, "after_inject" if injected else "inject_failed",
                                [path], extra={"kind": kind, "injected": bool(injected),
                                               "bytes": int(size),
                                               "ms": round((time.monotonic() - started_attempt) * 1000)})
                        except Exception:
                            diag.exception("main.audit_inject")
                        if not injected:
                            ok = False
                            break
                        time.sleep(0.3)

                    if ok:
                        self.last_upload = time.time()
                        self.fails = 0
                        time.sleep(0.3)
                        continue

                    self.fails += 1
                    # Безопасный повтор без аварийного выхода из цикла
                    backoff = min(3.0, 0.5 + 0.3 * min(self.fails, 8))
                    time.sleep(backoff)
                    continue

                time.sleep(0.05)

            except Exception:
                diag.exception("main.py:2594")
                time.sleep(0.2)

    def stop(self):
        self._stop = True


class PromptSelectionWindow(QDialog):
    @stage
    def __init__(self, items, display_names, current_index, parent=None):
        super().__init__(parent)
        self.items = items
        self.choice = None

        self.setWindowTitle("Выбор промта")
        self.setFixedSize(280, 160)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Dialog
        )
        self.setStyleSheet(BASE_QSS)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        title = QLabel("Выберите рабочий промт")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(f"font-size: 14px; font-weight: 700; color: {THEME['accent']};")
        layout.addWidget(title)

        self.combo = QComboBox()
        self.combo.addItems(display_names)
        self.combo.setCurrentIndex(current_index)
        self.combo.setMinimumHeight(38)
        layout.addWidget(self.combo)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)

        cancel_btn = QPushButton("Отмена")
        cancel_btn.setObjectName("Ghost")
        cancel_btn.clicked.connect(lambda _checked=False: self.reject())
        btn_layout.addWidget(cancel_btn)

        ok_btn = QPushButton("Выбрать")
        ok_btn.setObjectName("Primary")
        ok_btn.clicked.connect(lambda _checked=False: self._on_accept())
        btn_layout.addWidget(ok_btn)

        layout.addLayout(btn_layout)
        self.sync_position()

    def sync_position(self):
        parent = self.parentWidget()
        if parent:
            pg = parent.geometry()
            self.move(pg.x() - self.width() + 2, pg.y())

    def _on_accept(self):
        idx = self.combo.currentIndex()
        if 0 <= idx < len(self.items):
            self.choice = self.items[idx]
        self.accept()


class MainWindow(QMainWindow):
    overlay_state = pyqtSignal(str, str, bool)
    # v18.2: браузер устоялся — можно снимать шторку (сигнал нужен, потому что
    # измерение идёт в фоновом потоке, а шторку снимает только главный).
    revive_settled = pyqtSignal(bool)
    response_poll_finished = pyqtSignal(object)
    # Горячие клавиши опрашиваются таймером главного потока (win32_hotkeys),
    # никаких потоков/скрытых окон: колбэк приходит сразу в контекст Qt GUI.

    @stage
    def __init__(self, cfg, token, user_data):
        super().__init__()
        self.cfg = cfg
        self.token = token
        self.user_data = user_data or {}
        self.queries_remaining = int(self.user_data.get("queriesRemaining", 0) or 0)
        self.has_unlimited = bool(self.user_data.get("hasUnlimited", False))
        self.unlimited_until = str(self.user_data.get("unlimitedUntil") or "")

        self.mic_active = False
        self._mic_got_text = False
        # Режим браузера: native = настоящий Chrome/Chromium (штатный микрофон
        # страницы), False = встроенный QtWebEngine (резерв: диктовка Windows).
        self.native_mode = False
        self.cdp_port = DEBUG_PORT
        self.browser_host = None
        self.browser_physical = None
        self.browser_logical = None
        self._browser_demoted = False
        self._mic_retried = False
        self._mic_last_ts = 0.0
        self._mic_busy = False
        # v18: установка браузера и «оживление» после сворачивания.
        self._browser_install_offered = False
        self._fetch_worker = None
        self._fetch_dialog = None
        self._was_hidden = False
        self._was_minimized = False
        self._revive_settled = 0
        self._revive_last = None
        self._settling = False
        self._zoom_lock = threading.Lock()
        self.revive_settled.connect(self._on_revive_settled)
        # v18: язык речевого ввода и тема страницы (правятся в config.json).
        self.speech_lang = str(self.cfg.get("speech_lang") or SPEECH_LANG or "ru-RU")
        self.light_theme = bool(self.cfg.get("light_theme", FORCE_LIGHT_THEME))
        self.accept_languages = (str(self.cfg.get("accept_languages")
                                     or ACCEPT_LANGUAGES or "") or None)

        self.worker = None
        self.chrome_hwnd = None
        self.upload_thread = None
        self.pdf_path = None
        self.template_path = None
        self.prompt_loader = None
        self.pdf_attached = False
        self.pdf_was_loaded_once = False
        self._last_attached_state = None
        self._closing = False
        self._cleaned = False
        self._decrement_worker = None
        self._balance_worker = None
        self._hotkeys_registered = False
        self._chat_ready = False
        self._crash_recoveries = 0
        self.hotkeys = HotkeyMonitor(on_event=self._on_hotkey)
        # Опрос GetAsyncKeyState из главного потока Qt (~25 мс): работает с
        # любым фокусом/раскладкой и не может "не сработать".
        self._hotkey_timer = QTimer(self)
        self._hotkey_timer.setInterval(25)
        self._hotkey_timer.timeout.connect(self._hotkey_tick)
        self._hotkey_timer.start()
        self._response_poll_busy = False
        self.response_poll_finished.connect(self._apply_response_poll)
        self._upload_failed = False
        self.upload_deadline_timer = QTimer(self)
        self.upload_deadline_timer.setSingleShot(True)
        self.upload_deadline_timer.timeout.connect(self._upload_wait_expired)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Window
        )
        self.setWindowTitle("Legalyze AI — диагностика Win10/Win11")
        self.drag_position = QPoint()

        self.setFixedSize(W, H)
        self.setGeometry(X, Y, W, H)

        self.top_inset = 54
        self.bottom_inset = 30
        self.side_inset = 20

        self.central_widget = QWidget()
        self.central_widget.setStyleSheet(f"background: {THEME['bg']};")
        self.setCentralWidget(self.central_widget)

        self.browser_placeholder = QWidget(self.central_widget)
        self.browser_placeholder.setGeometry(0, 0, W, H)
        self.browser_placeholder.setStyleSheet(f"background: {THEME['bg']};")
        self.browser_placeholder.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.browser_cover = QWidget(self.browser_placeholder)
        self.browser_cover.setGeometry(0, 0, W, H)
        self.browser_cover.setStyleSheet(f"background: {THEME['bg']};")
        # Окно браузера — настоящее HWND-окно внутри плейсхолдера, а обычный
        # виджет-потомок Qt всегда рисуется ПОД ним. Только нативное окно
        # поднимается выше: без этого «сырой» браузер виден до загрузки.
        self.browser_cover.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.browser_cover.hide()

        # v15: Chrome сам возвращает кнопку в панель задач — следим periodically.
        self.taskbar_timer = QTimer(self)
        self.taskbar_timer.timeout.connect(self._hide_browser_taskbar)

        self._build_chrome_ui()

        self.overlay = LoadingOverlay(self)
        self.overlay_state.connect(self._apply_overlay_state)
        self.overlay_state.emit("Инициализация", "Запуск защищённого браузера…", False)

        self._register_hotkeys()
        self._refresh_balance()
        QTimer.singleShot(0, self._sync_template_from_server)

        self.init_timer = QTimer(self)
        self.init_timer.timeout.connect(self._start_chrome)
        self.init_timer.setSingleShot(True)

        self._last_decremented_response_state = 0

        self.response_end_timer = QTimer(self)
        self.response_end_timer.timeout.connect(self._check_response_end_from_ui)
        self.response_end_timer.start(1000)

        self.topmost_timer = QTimer(self)
        self.topmost_timer.timeout.connect(self._enforce_topmost)
        self.topmost_timer.start(1200)

    def _build_chrome_ui(self):
        panel = f"background-color: {THEME['panel']};"
        side_height = H - self.top_inset - self.bottom_inset

        self.overlay_top = QWidget(self.central_widget)
        self.overlay_top.setGeometry(0, 0, W, self.top_inset)
        self.overlay_top.setStyleSheet(
            panel + f"border-bottom: 1px solid {THEME['line']};"
        )

        shadow = QGraphicsDropShadowEffect(self.overlay_top)
        shadow.setBlurRadius(18)
        shadow.setOffset(0, 3)
        shadow.setColor(QColor(0, 0, 0, 170))
        self.overlay_top.setGraphicsEffect(shadow)

        icon_btn = f"""
            QPushButton {{
                background: rgba(255, 255, 255, 0.04);
                color: {THEME['text']};
                border: 1px solid {THEME['line']};
                border-radius: 7px;
                font-size: 11px;
                font-weight: 600;
                padding: 5px 9px;
            }}
            QPushButton:hover {{
                background: rgba(124, 131, 255, 0.24);
                border-color: {THEME['accent']};
                color: #ffffff;
            }}
            QPushButton:pressed {{ background: rgba(124, 131, 255, 0.4); }}
        """

        top_layout = QHBoxLayout(self.overlay_top)
        top_layout.setContentsMargins(12, 6, 10, 6)
        top_layout.setSpacing(6)

        brand = QVBoxLayout()
        brand.setSpacing(0)

        title_label = QLabel("Legalyze")
        title_label.setStyleSheet(
            f"color: {THEME['accent']}; font-weight: 800; font-size: 15px;"
            "letter-spacing: 0.3px; border: none;"
        )
        brand.addWidget(title_label)

        self.status_label = QLabel("Подключение…")
        self.status_label.setStyleSheet(
            f"color: {THEME['muted']}; font-size: 9px; border: none;"
        )
        brand.addWidget(self.status_label)
        top_layout.addLayout(brand)

        top_layout.addStretch()

        # ── чипы горячих клавиш с подписями ──
        self.chip_mic = HotkeyChip("Микрофон", self.cfg.get("hotkey_mic", "F3"))
        self.chip_mic.button.setEnabled(True)
        self.chip_mic.clicked.connect(lambda _checked=False: self._choose_mic_hotkey())
        top_layout.addWidget(self.chip_mic)

        self.chip_toggle = HotkeyChip("Окно", self.cfg.get("hotkey_toggle", "F2"))
        self.chip_toggle.clicked.connect(lambda _checked=False: self._choose_hotkey())
        top_layout.addWidget(self.chip_toggle)

        sep = QLabel("")
        sep.setFixedWidth(1)
        sep.setStyleSheet(f"background: {THEME['line']}; border: none;")
        top_layout.addWidget(sep)

        self.btn_template = QPushButton("Шаблон")
        self.btn_template.setToolTip("Данные шаблона")
        self.btn_template.setStyleSheet(icon_btn)
        self.btn_template.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_template.clicked.connect(lambda _checked=False: self._open_template())
        top_layout.addWidget(self.btn_template)

        self.btn_prompt = QPushButton("Промт")
        self.btn_prompt.setToolTip("Выбор промта")
        self.btn_prompt.setStyleSheet(icon_btn)
        self.btn_prompt.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_prompt.clicked.connect(lambda _checked=False: self._choose_prompt())
        top_layout.addWidget(self.btn_prompt)

        btn_restart = QPushButton("⟳")
        btn_restart.setToolTip("Перезапуск")
        btn_restart.setStyleSheet(icon_btn)
        btn_restart.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_restart.clicked.connect(lambda _checked=False: self._restart())
        top_layout.addWidget(btn_restart)

        btn_close = QPushButton("✕")
        btn_close.setToolTip("Закрыть")
        btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_close.setStyleSheet(f"""
            QPushButton {{
                background: rgba(239, 68, 68, 0.16);
                color: #fca5a5;
                border: 1px solid rgba(239, 68, 68, 0.5);
                border-radius: 7px;
                font-size: 12px;
                font-weight: 700;
                padding: 5px 10px;
            }}
            QPushButton:hover {{ background: rgba(239, 68, 68, 0.55); color: #fff; }}
        """)
        btn_close.clicked.connect(lambda _checked=False: self._close_app())
        top_layout.addWidget(btn_close)

        self.overlay_left = QWidget(self.central_widget)
        self.overlay_left.setGeometry(0, self.top_inset, self.side_inset, side_height)
        self.overlay_left.setStyleSheet(panel + f"border-right: 1px solid {THEME['line']};")

        self.overlay_right = QWidget(self.central_widget)
        self.overlay_right.setGeometry(W - self.side_inset, self.top_inset, self.side_inset, side_height)
        self.overlay_right.setStyleSheet(panel + f"border-left: 1px solid {THEME['line']};")

        self.overlay_bottom = QWidget(self.central_widget)
        self.overlay_bottom.setGeometry(0, H - self.bottom_inset, W, self.bottom_inset)
        self.overlay_bottom.setStyleSheet(panel + f"border-top: 1px solid {THEME['line']};")

        bottom_layout = QHBoxLayout(self.overlay_bottom)
        bottom_layout.setContentsMargins(12, 4, 12, 4)
        bottom_layout.setSpacing(8)

        self.query_label = QLabel()
        self.query_label.setStyleSheet(
            f"color: {THEME['text']}; font-size: 11px; font-weight: 600; border: none;"
        )
        bottom_layout.addWidget(self.query_label)

        bottom_layout.addStretch()

        self.prompt_label = QLabel("промт: —")
        self.prompt_label.setStyleSheet(f"color: {THEME['muted']}; font-size: 10px; border: none;")
        bottom_layout.addWidget(self.prompt_label)

        self.dot_label = QLabel("●")
        self.dot_label.setStyleSheet("color: #f59e0b; font-size: 11px; border: none;")
        bottom_layout.addWidget(self.dot_label)

        self._update_query_label()
        self._update_prompt_label()

        for w in (self.overlay_top, self.overlay_left, self.overlay_right, self.overlay_bottom):
            w.raise_()

    def _update_query_label(self):
        if self.has_unlimited:
            date_str = ""
            if self.unlimited_until:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(
                        self.unlimited_until.replace("Z", "+00:00")
                    )
                    date_str = dt.strftime("%d.%m.%Y")
                except Exception:
                    diag.exception("main.py:2907")
                    date_str = ""
            if date_str:
                self.query_label.setText(f"Безлимит до {date_str}")
            else:
                self.query_label.setText("Безлимит")
        else:
            self.query_label.setText(f"Запросы:  {self.queries_remaining}")

    def _update_prompt_label(self):
        sel = self.cfg.get("selected_prompt", "")
        self.prompt_label.setText(f"промт: {prompt_display_name(sel) if sel else '—'}")

    def _set_status(self, text: str, color: str = None):
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            f"color: {color or THEME['muted']}; font-size: 9px; border: none;"
        )

    def _set_dot(self, color: str):
        self.dot_label.setStyleSheet(f"color: {color}; font-size: 11px; border: none;")

    def _apply_overlay_state(self, title, message, error):
        if not title:
            self.overlay.finish()
            return
        self.overlay.show_state(title, message, error)

    def _show_overlay(self, title, message, error=False):
        self.browser_cover.show()
        # Шторка должна быть ПОВЕРХ встроенного браузера (web-виджет создаётся
        # позже и иначе перекрывает её в z-порядке Qt).
        self.browser_cover.raise_()
        self.overlay_state.emit(title, message, error)

    def _prompt_selected(self) -> bool:
        """Выбран ли промт (v17).

        Пока промт не выбран, браузер не показывается: окно ожидания стоит
        ровно до момента выбора. Если конфиг по какой-то причине недоступен —
        интерфейс не блокируем (возвращаем True).
        """
        try:
            return bool(str(self.cfg.get("selected_prompt", "") or "").strip())
        except Exception:
            diag.exception("main.prompt_selected")
            return True

    # ------------------------------------------------------- v20: приборная панель
    def _trace_qt_probe(self):
        """Состояние Qt читается ТОЛЬКО в потоке GUI (v20).

        Виджеты Qt нельзя опрашивать из чужого потока — это как раз тот случай,
        когда «сбор логов» сам стал бы причиной нестабильности. Поэтому GUI
        раз в 100 мс обновляет простой словарь, а фоновой шкале достаётся уже
        готовый снимок (обычные числа и флаги, без обращений к Qt).
        """
        data = {}
        try:
            data["win_visible"] = bool(self.isVisible())
            data["win_min"] = bool(self.isMinimized())
            data["win_geo"] = list(self.geometry().getRect())
            data["dpr"] = round(float(self.devicePixelRatioF()), 3)
            data["cover"] = bool(self.browser_cover.isVisible())
            data["settling"] = bool(getattr(self, "_settling", False))
            data["overlay"] = bool(self.overlay.isVisible())
            data["chat_ready"] = bool(getattr(self, "_chat_ready", False))
            data["native"] = bool(getattr(self, "native_mode", False))
        except Exception:
            diag.exception("main.trace_qt_probe")
        return data

    def _trace_qt_tick(self):
        """Тик GUI-таймера: обновить снимок состояния для временной шкалы."""
        try:
            self._trace_qt_cache = self._trace_qt_probe()
            self._trace_qt_cache_at = time.monotonic()
            # Шкал больше нет — таймер не нужен: сбор не должен работать вхолостую.
            if btrace.active_watchers() == 0 and getattr(self, "_trace_qt_timer", None):
                self._trace_qt_timer.stop()
        except Exception:
            diag.exception("main.trace_qt_tick")

    def _trace_extra(self):
        """Снимок состояния Qt для записи (никогда не трогает виджеты извне)."""
        cache = getattr(self, "_trace_qt_cache", None)
        stamp = float(getattr(self, "_trace_qt_cache_at", 0.0) or 0.0)
        if cache and (time.monotonic() - stamp) < 2.0:
            return dict(cache)
        # Кэша ещё нет (первый вызов) — читаем сами. Сюда попадаем только из
        # потока GUI: таймер запускается ДО первой шкалы.
        return self._trace_qt_probe()

    def _trace_qt_keepalive(self):
        """Держать GUI-снимок свежим, пока есть хоть одна шкала (v20)."""
        if not getattr(self, "_trace_qt_timer", None):
            self._trace_qt_timer = QTimer(self)
            self._trace_qt_timer.setInterval(100)
            self._trace_qt_timer.timeout.connect(self._trace_qt_tick)
        if not self._trace_qt_timer.isActive():
            self._trace_qt_tick()
            self._trace_qt_timer.start()

    def _trace_args(self):
        """(user32, hwnd, parent, inset) для приборной панели. None — нет окна."""
        host = getattr(self, "browser_host", None)
        if host is None or getattr(host, "user32", None) is None or not getattr(host, "hwnd", 0):
            return None
        try:
            inset = tuple(self._browser_inset())
        except Exception:
            diag.exception("main.trace_inset")
            inset = tuple(getattr(host, "inset", (0, 0, 0, 0)) or (0, 0, 0, 0))
        try:
            parent = int(self.browser_placeholder.winId())
        except Exception:
            diag.exception("main.trace_parent")
            parent = 0
        return int(host.hwnd), parent, inset

    def _trace_watch(self, tag, duration=8.0, fast_until=3.0, min_ms=1200,
                     fast_ms=20, slow_ms=250, stable_needed=3):
        """Запустить временную шкалу окна вокруг возмущения (v20).

        Ничего в приложении не меняет: только измеряет. Если окна ещё нет
        (резервный движок, страница не поднялась) — просто ничего не делает.
        """
        if not BROWSER_TRACE_ON:
            return None
        args = self._trace_args()
        if args is None:
            btrace.mark("watch.skip", tag=str(tag), reason="no-native-window")
            return None
        hwnd, parent, inset = args
        # Снимок Qt обязана обновлять GUI-сторона — фоновая шкала только читает.
        self._trace_qt_keepalive()
        try:
            return btrace.watch(tag, hwnd, parent, inset,
                                page_provider=self._current_page,
                                extra_provider=self._trace_extra,
                                duration=duration, fast_until=fast_until,
                                min_ms=min_ms, fast_ms=fast_ms, slow_ms=slow_ms,
                                stable_needed=stable_needed)
        except Exception:
            diag.exception("main.trace_watch")
            return None

    def _trace_page(self, reason, full=False):
        """Снимок страницы в фоне: структура, метрики, при full — «код» (v20)."""
        if not BROWSER_TRACE_ON:
            return

        def task():
            page = self._current_page()
            if page is None:
                btrace.mark("page.snapshot_skipped", reason=str(reason))
                return
            try:
                btrace.capture_page(page, reason=reason, full=bool(full))
            except Exception:
                diag.exception("main.trace_page")

        threading.Thread(target=task, name="PageSnapshot", daemon=True).start()

    def _cover_to_top(self):
        """Шторка поверх окна браузера: и по z-порядку Qt, и по Win32 (v17).

        Окно браузера — настоящее HWND-окно внутри плейсхолдера. После
        `SetParent` оно встаёт поверх всех соседей, поэтому одного `raise_()`
        мало: поднимаем шторку принудительно через `SetWindowPos(HWND_TOP)`.
        """
        try:
            if not self.browser_cover.isVisible():
                self.browser_cover.show()
            self.browser_cover.raise_()
        except Exception:
            diag.exception("main.cover_raise")
        host = getattr(self, "browser_host", None)
        user32 = getattr(host, "user32", None) if host is not None else None
        if user32 is None:
            return
        try:
            win32_embed.raise_window(user32, int(self.browser_cover.winId()))
        except Exception:
            diag.exception("main.cover_top_win32")

    def _hold_cover_if_needed(self):
        """Держать шторку, пока браузер не должен быть виден (v17).

        Условие ровно одно: чат подтвердил готовность **И** промт выбран.
        Пока второго нет (чистый первый запуск), браузер остаётся закрытым —
        окно ожидания стоит до момента выбора промта, а не на секунду.
        """
        if self._closing:
            return
        # v18.3: пока браузер «устаивается» после показа окна, GUI прогрузки
        # стоит независимо ни от чего — иначе съехавшая раскладка видна.
        if getattr(self, "_settling", False):
            self._cover_to_top()
            try:
                if self.overlay.isVisible():
                    self.overlay.sync_geometry()
                    force_topmost(int(self.overlay.winId()))
            except Exception:
                diag.exception("main.settle_overlay_top")
            return
        if self._chat_ready and self._prompt_selected():
            return
        self._cover_to_top()

    @pyqtSlot(bool)
    def _on_revive_settled(self, stable):
        """Снять шторку: браузер встал на место (v18.2)."""
        self._settling = False
        if self._closing:
            return
        self._hide_overlay()
        diag.event("browser.revealed", stable=bool(stable),
                   settling=bool(getattr(self, "_settling", False)))

    def _hide_overlay(self):
        # v18.2: во время стабилизации шторку не снимает никто — только
        # `_on_revive_settled`, когда измерение подтвердило, что всё на месте.
        if getattr(self, "_settling", False):
            self._cover_to_top()
            return
        self.overlay_state.emit("", "", False)
        # v17: пока промт не выбран, браузер остаётся закрытым шторкой —
        # окно ожидания стоит ровно до момента выбора промта.
        if not self._prompt_selected():
            self._cover_to_top()
            return
        self.browser_cover.hide()


    def _enforce_topmost(self):
        if self._closing or not self.isVisible():
            return

        hwnd = int(self.winId())
        force_topmost(hwnd)

        if self.overlay.isVisible():
            self.overlay.sync_geometry()
            force_topmost(int(self.overlay.winId()))

    def moveEvent(self, event):
        super().moveEvent(event)
        try:
            if self.overlay.isVisible():
                self.overlay.sync_geometry()
        except Exception:
            diag.exception("main.py:2960")
            pass
        # v16: окно перетащили на монитор с другим масштабом — пересчитываем
        # сдвиг и зум СРАЗУ, а не по 5-секундному таймеру. Иначе браузер
        # уезжает в угол или в сторону.
        self._check_dpi_drift()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_chrome_geometry()

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_chrome_geometry()
        was_hidden = bool(getattr(self, "_was_hidden", False))
        # v18: окно вернулось после скрытия (F2) — браузер нужно перерисовать.
        if was_hidden:
            self._was_hidden = False
            self._schedule_revive()
        # v20: приборная панель. Показ окна — главное возмущение из всех: именно
        # после него пользователь видит «съехало и встало на место». Шкала
        # измеряет и окно, и страницу, и состояние Qt — по миллисекундам.
        btrace.mark("window.show", was_hidden=was_hidden, args=self._trace_extra())
        self._trace_watch("show", duration=10.0, fast_until=4.0, min_ms=1200)

    def hideEvent(self, event):
        super().hideEvent(event)
        self._was_hidden = True
        # v20: скрытие — начало возмущения; фиксируем позицию на момент ухода.
        btrace.mark("window.hide", args=self._trace_extra())
        self._trace_watch("hide", duration=4.0, fast_until=1.5, min_ms=300)
        if not REVIVE_AFTER_SHOW:
            return
        # v18.2: шторка встаёт СЕЙЧАС, пока окно не видно. Когда окно
        # покажут, пользователь увидит панель приложения, а не съезжающую
        # страницу: шторка снимется только по сигналу «устоялось».
        self._cover_to_top()
        # v18.1: окно всё равно не видно — ставим браузер точно на место,
        # чтобы к следующему показу он уже стоял там, где нужно.
        self._sync_browser_now()

    def changeEvent(self, event):
        super().changeEvent(event)
        self._sync_chrome_geometry()
        # v16: сам Windows/Qt сообщает о смене DPI или монитора — та же реакция.
        try:
            if event.type() in _METRIC_CHANGE_EVENTS:
                self._on_screen_metrics_changed("change_event")
        except Exception:
            diag.exception("main.change_metrics")
        # v18: вернулись из «свёрнутого» состояния — браузер перерисовываем.
        try:
            if event.type() == QEvent.Type.WindowStateChange:
                if self.isMinimized():
                    self._was_minimized = True
                    # v20: сворот — тоже возмущение: окно теряет кадр, а при
                    # развороте Chromium пересобирает раскладку.
                    btrace.mark("window.minimize", args=self._trace_extra())
                    self._trace_watch("minimize", duration=6.0, fast_until=2.0,
                                      min_ms=300)
                    # v18.2: свёрнутое окно всё равно не видно — поднимаем
                    # шторку заранее, чтобы при развороте не мелькнула
                    # съехавшая страница.
                    if REVIVE_AFTER_SHOW:
                        self._cover_to_top()
                elif self._was_minimized:
                    self._was_minimized = False
                    btrace.mark("window.restore", args=self._trace_extra())
                    self._schedule_revive()
        except Exception:
            diag.exception("main.revive_state")

    def _check_dpi_drift(self):
        """Среагировать на смену масштаба монитора под окном (v16).

        `devicePixelRatioF()` меняется, когда окно переезжает на монитор с
        другим масштабом. Пересчитываем сдвиг окна браузера, цель зума и
        убираем кнопку из панели задач — ровно то, что делает 5-секундный
        таймер, но немедленно. Пока монитор один и тот же — вызов пустой.
        """
        if self._closing or not self.native_mode:
            return
        try:
            dpr = float(self.devicePixelRatioF())
        except Exception:
            return
        if dpr <= 0:
            return
        previous = float(getattr(self, "_last_dpr", 0.0) or 0.0)
        if abs(dpr - previous) < 0.001:
            return
        self._last_dpr = dpr
        self._on_screen_metrics_changed("dpi_drift")

    def _on_screen_metrics_changed(self, source):
        """Пересчитать геометрию браузера под текущий монитор и масштаб (v16).

        Ничего не «поправляет» вручную: сдвиг считается из BROWSER_DX/DY в
        физические пиксели ТЕКУЩЕГО монитора, а зум — от РЕАЛЬНОГО размера
        окна браузера. Поэтому при 100 %, 125 % и 150 % раскладка одна и та
        же, окно не уезжает ни в угол, ни в сторону, и поле ввода остаётся на
        месте. Браузер при этом НЕ перезапускается.
        """
        if self._closing or not self.native_mode:
            return
        try:
            diag.event("main.screen_metrics_changed", source=str(source),
                       dpr=round(float(self.devicePixelRatioF()), 4),
                       scale=round(float(self._monitor_scale()), 4),
                       inset=list(self._browser_inset()))
        except Exception:
            diag.exception("main.metrics_log")
        # Реальный размер окна браузера — от него считается нативный зум 67 %.
        try:
            self.browser_physical = self._placeholder_physical_size()
            self.browser_logical = self._placeholder_logical_size()
        except Exception:
            diag.exception("main.metrics_size")
        self._sync_chrome_geometry()
        try:
            if self.worker is not None:
                self.worker.size = self._zoom_size()
        except Exception:
            diag.exception("main.metrics_worker_size")
        # Зум проверяется и, если нужно, возвращается в фоновом потоке:
        # GUI не блокируется, браузер не перезапускается.
        self._ensure_zoom()
        self._hide_browser_taskbar()

    def _sync_chrome_geometry(self):
        host = getattr(self, "browser_host", None)
        if self.native_mode and host is not None:
            # v16: сдвиг пересчитывается на КАЖДОЙ синхронизации. При смене
            # монитора или масштаба физический размер плейсхолдера меняется, и
            # сдвиг в пикселях обязан меняться вместе с ним: в DIP он как был
            # -10/-40, так и остаётся, а в пикселях при 150 % это -15/-60.
            # Без этого окно браузера уезжало бы в сторону на чужом масштабе.
            # Пока монитор не менялся — значение то же самое, до пикселя.
            try:
                host.inset = self._browser_inset()
            except Exception:
                diag.exception("main.reinset_sync")
            host.sync()
            return
        if getattr(self, "web_browser", None) is not None:
            try:
                self.web_browser.setGeometry(self.side_inset, self.top_inset,
                                             W - 2 * self.side_inset,
                                             H - self.top_inset - self.bottom_inset)
            except Exception:
                diag.exception("main.sync_embedded")

    def _browser_inset(self):
        """(left, top, right, bottom) окна браузера в ФИЗИЧЕСКИХ px плейсхолдера.

        Сдвиг из pre14, переведённый в физические пиксели текущего монитора:
        при 125 % и 150 % сдвиг в DIP тот же самый, а в пикселях — во столько
        же раз больше. Размер окна меняется только если заданы BROWSER_DW/DH.
        """
        try:
            dx = int(self.cfg.get("browser_dx", BROWSER_DX))
            dy = int(self.cfg.get("browser_dy", BROWSER_DY))
            dw = int(self.cfg.get("browser_dw", BROWSER_DW))
            dh = int(self.cfg.get("browser_dh", BROWSER_DH))
        except (TypeError, ValueError):
            dx, dy, dw, dh = BROWSER_DX, BROWSER_DY, BROWSER_DW, BROWSER_DH
        logical_w, logical_h = self._placeholder_logical_size()
        physical_w, physical_h = self._placeholder_physical_size()
        scale = (float(physical_w) / float(logical_w)) if logical_w else 1.0
        left = int(round(dx * scale))
        top = int(round(dy * scale))
        right = -int(round((dx + dw) * scale))
        bottom = -int(round((dy + dh) * scale))
        return (left, top, right, bottom)

    def _hide_browser_taskbar(self):
        """Периодически убираем кнопку браузера из панели задач (v15)."""
        host = getattr(self, "browser_host", None)
        if host is None or self._closing or not self.native_mode:
            return
        try:
            host.hide_taskbar()
        except Exception:
            diag.exception("main.taskbar_timer")

    def _hide_strays_once(self):
        """Убрать лишние окна браузера, пересчитав дерево pid (v16).

        Без `refresh_pids()` дерево застывает на моменте `ready`, и окна,
        поднятые позже, остаются висеть в Alt+Tab.
        """
        host = getattr(self, "browser_host", None)
        if host is None or self._closing or not self.native_mode:
            return
        try:
            host.refresh_pids()
            host._hide_strays(keep=int(getattr(host, "hwnd", 0) or 0))
        except Exception:
            diag.exception("main.hide_strays_once")

    def _placeholder_logical_size(self):
        """РЕАЛЬНЫЙ размер плейсхолдера в DIP (Qt работает в DIP).

        Размер берётся у самого виджета, а не у воображаемого «слота» между
        рамками: плейсхолдер занимает всё окно (0, 0, W, H) = 453x735 DIP, и
        окно браузера растягивается именно на него. Прежняя формула давала
        413x651 DIP, то есть ширину раскладки 413 / 0.667 = 620 CSS px вместо
        нужных 453 / 0.667 = 680 CSS px — поэтому поле ввода и поле ответа ИИ
        стояли не на своих местах и съезжали вправо.
        """
        placeholder = getattr(self, "browser_placeholder", None)
        if placeholder is not None:
            try:
                width, height = int(placeholder.width()), int(placeholder.height())
                if width > 0 and height > 0:
                    return width, height
            except Exception:
                diag.exception("main.placeholder_logical")
        return int(W), int(H)

    def _browser_physical_size(self):
        """Физический размер клиентской области САМОГО окна браузера.

        Именно эта область видна пользователю, поэтому раскладка считается от
        неё: у окна приложения может быть рамка/заголовок, и тогда поверхность
        по высоте плейсхолдера срезала бы нижнюю часть страницы — поле ввода.
        Резерв — клиентская область плейсхолдера (пока окно ещё не встроено).
        """
        host = getattr(self, "browser_host", None)
        user32 = getattr(host, "user32", None) if host is not None else None
        hwnd = (getattr(host, "hwnd", 0) if host is not None else 0) \
            or getattr(self, "chrome_hwnd", 0)
        placeholder_size = self._placeholder_physical_size()
        if user32 and hwnd:
            try:
                width, height = win32_embed.client_size(user32, int(hwnd))
                width, height = int(width), int(height)
                if width > 0 and height > 0:
                    # Окно ещё не растянуто под плейсхолдер — не верим ему.
                    if abs(width - placeholder_size[0]) <= 0.4 * placeholder_size[0]:
                        return width, height
            except Exception:
                diag.exception("main.browser_physical")
        return placeholder_size

    def _monitor_scale(self):
        """Масштаб монитора: физический пиксель / DIP.

        1.0 при 100 %, 1.25 при 125 %, 1.5 при 150 %. Без этого метода перевод
        физического размера окна в DIP невозможен.
        """
        logical_w, logical_h = self._placeholder_logical_size()
        physical_w, physical_h = self._placeholder_physical_size()
        scale = (float(physical_w) / float(logical_w)) if logical_w else 0.0
        if scale <= 0:
            # Резерв: масштаб самого окна Qt. При 100 % оба пути дают 1.0,
            # поэтому на отлаженной машине ровным счётом ничего не меняется.
            try:
                scale = float(self.devicePixelRatioF())
            except Exception:
                diag.exception("main.monitor_scale_dpr")
                scale = 0.0
        return scale if scale > 0 else 1.0

    def _browser_logical_size(self):
        """Логический (DIP) размер САМОГО окна браузера.

        Резерв — плейсхолдер, пока окно ещё не встроено.
        """
        physical_w, physical_h = self._browser_physical_size()
        scale = self._monitor_scale()
        if scale <= 0:
            scale = 1.0
        width = int(round(physical_w / scale))
        height = int(round(physical_h / scale))
        if width > 0 and height > 0:
            return width, height
        return self._placeholder_logical_size()

    def _zoom_size(self):
        """(логические DIP, физические пиксели) — для расчёта зума 67 %.

        Логический размер берётся у САМОГО окна браузера, а НЕ у плейсхолдера:
        на Windows клиентская область окна браузера на 14x7 px меньше
        плейсхолдера (453x735 против 439x728). Цель, посчитанная от
        плейсхолдера, даёт dsf 439/679 = 0.6465 вместо 439/658 = 0.667 —
        страница получается на 3 % мельче, и именно поэтому отображение
        объектов в v15 отличалось от замеров pre14. От своего окна цель
        совпадает с нативным рендером ровно, и ничего переопределять не нужно.
        """
        logical_w, logical_h = self._browser_logical_size()
        physical_w, physical_h = self._browser_physical_size()
        return logical_w, logical_h, physical_w, physical_h

    def _zoom_args(self):
        """(logical_w, logical_h, physical_w, physical_h) для native_browser.

        Поправки блока настроек подаются так, что `target_css` вернёт ровно
        нужные css и dsf: `target_css` считает `css = logical / zoom` и
        `css_h = physical_h / dsf`.
        """
        logical_w, logical_h, physical_w, physical_h = self._zoom_size()
        css_w = max(320, int(round(logical_w / float(PAGE_ZOOM))) + int(WIDTH_DELTA))
        dsf = (float(physical_w) / css_w) if css_w else 1.0
        css_h = max(320, int(round(physical_h / dsf)) + int(HEIGHT_DELTA))
        return (css_w * float(PAGE_ZOOM), logical_h, physical_w, css_h * dsf)

    def _placeholder_physical_size(self):
        """Физический размер плейсхолдера (GetClientRect даёт физические пиксели).

        Зум считается от него: страница верстается в `physical / 0.667` CSS px и
        масштабируется в окно. Поэтому раскладка одинаковая при 100 %, 125 % и
        150 % — и текст никогда не уезжает за правую границу.
        """
        user32 = None
        host = getattr(self, "browser_host", None)
        if host is not None:
            user32 = getattr(host, "user32", None)
        placeholder = getattr(self, "browser_placeholder", None)
        if user32 is not None and placeholder is not None:
            try:
                width, height = win32_embed.client_size(user32, int(placeholder.winId()))
                if width > 0 and height > 0:
                    return int(width), int(height)
            except Exception:
                diag.exception("main.placeholder_size")
        # Резерв: логический размер плейсхолдера (до появления окна браузера).
        return (int(W - 2 * self.side_inset),
                int(H - self.top_inset - self.bottom_inset))

    def _ensure_zoom(self):
        """Держать нативный зум 67 % (защита от «широкого окна» и сдвига вправо).

        Метрики сбрасываются при смене таргета/перезагрузке, после чего чат
        снова рисует desktop-раскладку: широкую, со словом «ИИ» по центру и
        текстом, уезжающим за правую границу окна. Проверяем в фоновом потоке —
        GUI не блокируется.
        """
        if self._closing or not self.native_mode:
            return
        page = getattr(getattr(self, "worker", None), "page", None)
        if page is None:
            return
        threading.Thread(target=self._zoom_guard_once, args=(page, "timer"),
                         daemon=True).start()

    def _zoom_guard_once(self, page, reason="guard", pin_first=False):
        """ОДИН проход сторожа зума: измерить и, если надо, вернуть. True — всё верно.

        Выделено из `_ensure_zoom` в v18.1: после показа окна раскладку надо
        проверять и чинить **сразу**, а не ждать следующего тика таймера
        (5 с — отсюда и «съехало и встало на место только через пару секунд»).

        Одновременные проходы исключены (`_zoom_lock`): два потока не должны
        наперебой слать `Emulation.setDeviceMetricsOverride`.
        """
        if page is None or self._closing:
            return False
        if not self._zoom_lock.acquire(blocking=False):
            # Уже проверяется в соседнем потоке. Раньше здесь возвращалось
            # True, и пропущенный проход засчитывался как «всё верно».
            return None
        try:
            logical_w, logical_h, physical_w, physical_h = self._zoom_args()
            # v18: язык и тема держатся намертво — после перезагрузки
            # страницы Chrome возвращает системные значения.
            try:
                apply_language_and_theme(page, self.speech_lang, self.light_theme)
            except Exception:
                diag.exception("main.zoom_guard_theme")
            # Сдвиги страницы внедряются и держатся (после перезагрузки
            # стиль пропадает, поэтому он проверяется каждый проход).
            if page_tuning_needed():
                try:
                    page.eval(page_tune_js(with_scroll=False), timeout=4)
                except Exception:
                    diag.exception("main.page_tune")
            ok, metrics = zoom_ok(page, logical_w, logical_h, physical_w,
                                  physical_h, zoom=PAGE_ZOOM)
            if ok:
                return True
            if pin_first:
                # v18.2: сначала прибиваем раскладку ОДНИМ вызовом — так
                # ширина страницы перестаёт зависеть от размера окна, и
                # «качка» прекращается. Лестница переборов ниже даёт несколько
                # видимых перевёрсток подряд — она только на крайний случай.
                try:
                    apply_viewport(page, logical_w, logical_h, physical_w,
                                   physical_h, PAGE_ZOOM)
                    time.sleep(0.12)
                except Exception:
                    diag.exception("main.zoom_pin")
                ok, metrics = zoom_ok(page, logical_w, logical_h, physical_w,
                                      physical_h, zoom=PAGE_ZOOM)
                if ok:
                    diag.event("browser.zoom_pinned", reason=str(reason))
                    return True
            result = apply_zoom(page, logical_w, logical_h, physical_w,
                                physical_h, zoom=PAGE_ZOOM)
            diag.event("browser.zoom_restored",
                       source=(result or {}).get("source"), reason=str(reason),
                       inner=(metrics or {}).get("innerWidth"),
                       dpr=round(float((metrics or {}).get("dpr") or 0.0), 3))
            return bool((result or {}).get("ok"))
        except Exception:
            diag.exception("main.zoom_guard")
            return False
        finally:
            self._zoom_lock.release()

    def _apply_page_tuning(self):
        """Однократно применить сдвиги страницы вместе с прокруткой."""
        if self._closing or not self.native_mode or not page_tuning_needed():
            return
        page = getattr(getattr(self, "worker", None), "page", None)
        if page is None:
            return
        js = page_tune_js(with_scroll=True)

        def task():
            try:
                page.eval(js, timeout=4)
            except Exception:
                diag.exception("main.page_tune_once")

        threading.Thread(target=task, daemon=True).start()

    @stage
    def _register_hotkeys(self):
        try:
            toggle_key = self.cfg.get("hotkey_toggle", "F2")
            mic_key = self.cfg.get("hotkey_mic", "F3")

            vk_toggle = VK_MAP.get(toggle_key, 0x71)
            vk_mic = VK_MAP.get(mic_key, 0x72)

            # Опрос работает всегда: ложный статус конфликта невозможен.
            self._hotkeys_registered = bool(self.hotkeys.set_keys(vk_toggle, vk_mic))
            try:
                suppressed = self.hotkeys.try_suppress(int(self.winId() or 0))
            except Exception:
                suppressed = False
            diag.event(
                "hotkeys.configured",
                toggle=str(toggle_key),
                mic=str(mic_key),
                suppressed=bool(suppressed),
            )
            return self._hotkeys_registered
        except Exception:
            diag.exception("main.register_hotkeys")
            return False

    def _hotkey_tick(self):
        """Тик опроса клавиатуры (главный поток Qt)."""
        try:
            self.hotkeys.tick()
        except Exception:
            diag.exception("main.hotkey_tick")

    def _unregister_hotkeys(self):
        try:
            self.hotkeys.clear()
        except Exception:
            diag.exception("main.unregister_hotkeys")
        self._hotkeys_registered = False

    @diag.trace
    def _on_hotkey(self, key_id):
        if self._closing:
            return
        # v20: в лог попадает и сам факт нажатия, и его время: без этого
        # невозможно отличить «хоткей не сработал» от «сработал поздно».
        btrace.mark("hotkey.pressed", id=int(key_id),
                    visible=bool(self.isVisible()),
                    args=self._trace_extra())
        if key_id == HOTKEY_TOGGLE_ID:
            self._toggle_visibility()
        elif key_id == HOTKEY_MIC_ID:
            self._toggle_mic()

    def _change_hotkey(self, cfg_key: str, default: str, caption: str, chip: HotkeyChip, other_cfg_key: str):
        current = self.cfg.get(cfg_key, default)
        taken = {self.cfg.get(other_cfg_key, "")} - {current}

        self._unregister_hotkeys()
        try:
            dlg = HotkeyDialog(current, title=caption, taken=taken, parent=self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                key = dlg.get_key()
                if key and key in VK_MAP:
                    self.cfg[cfg_key] = key
                    save_config(self.cfg)
                    chip.set_key(key)
        finally:
            if self._register_hotkeys():
                self._set_status("Готов к работе", THEME["ok"])

    def _choose_hotkey(self):
        self._change_hotkey(
            "hotkey_toggle", "F2", "Клавиша: скрыть/показать окно",
            self.chip_toggle, "hotkey_mic",
        )

    def _choose_mic_hotkey(self):
        self._change_hotkey(
            "hotkey_mic", "F3", "Клавиша: микрофон",
            self.chip_mic, "hotkey_toggle",
        )

    @pyqtSlot()  # Explicit zero-argument Qt slot: clicked(bool) must not reach @stage.
    @stage
    def _toggle_visibility(self):
        # v20: хоткей «Окно» — точная точка отсчёта для временной шкалы. По ней
        # видно, через сколько миллисекунд после показа браузер встал на место
        # (жалоба «объекты съезжают и возвращаются через 2–3 секунды»).
        action = "hide" if self.isVisible() else "show"
        btrace.mark("hotkey.toggle.begin", action=action, args=self._trace_extra())
        if action == "hide":
            self.hide()
            if self.overlay.isVisible():
                self.overlay.hide()
        else:
            self.show()
            self.activateWindow()
            force_topmost(int(self.winId()))
            if self.overlay._anim.isActive():
                self.overlay.show()
                self.overlay.sync_geometry()
                force_topmost(int(self.overlay.winId()))
        btrace.mark("hotkey.toggle.end", action=action, args=self._trace_extra())

    def _page_eval_async(self, expression, timeout=3, callback=None):
        page = getattr(self.worker, "page", None) if self.worker else None
        if not page:
            return

        def task():
            value = None
            try:
                value = page.eval(expression, timeout=timeout).get("value")
            except Exception:
                diag.exception("main.py:3145")
                value = None
            if callback:
                try:
                    callback(value)
                except Exception:
                    diag.exception("main.py:3151")
                    pass

        threading.Thread(target=task, daemon=True).start()

    def _cdp_click_coords(self, x, y):
        page = getattr(self.worker, "page", None) if self.worker else None
        if not page or x is None or y is None:
            return
        try:
            page.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": float(x), "y": float(y)}, timeout=2)
            page.send("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "x": float(x), "y": float(y), "clickCount": 1}, timeout=2)
            page.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "x": float(x), "y": float(y), "clickCount": 1}, timeout=2)
        except Exception:
            diag.exception("main.py:3165")
            pass

    def _toggle_mic(self):
        if not self.worker or not getattr(self.worker, "page", None):
            return
        if not self._chat_ready:
            self._set_status("Страница ещё загружается — подождите…", THEME["muted"])
            return

        now = time.monotonic()
        # Защита от дребезга и удержания клавиши (одиночное нажатие)
        if self._mic_busy or (now - self._mic_last_ts) < 0.35:
            return

        self._mic_last_ts = now
        self._mic_busy = True

        # Снимаем блокировку СТРОГО в главном потоке Qt
        QTimer.singleShot(350, lambda: setattr(self, "_mic_busy", False))

        if not self.mic_active:
            # ─────────────────────────────────────────────────────────────
            # 1-е нажатие: старт записи речи (п.1 ТЗ)
            # ─────────────────────────────────────────────────────────────
            self.mic_active = True
            self._mic_got_text = False
            self.chip_mic.set_active(True)
            self._set_status("Запись голоса… (повтор: отправить)", THEME["ok"])

            if self.native_mode:
                # ШТАТНАЯ запись голоса самим браузером: клик по кнопке
                # микрофона страницы. Ни моста, ни диктовки Windows.
                self._mic_retried = False
                self._native_mic_start()
                return

            def on_shim_eval(ok):
                if ok:
                    # НАСТОЯЩАЯ диктовка (Windows System.Speech) запущена:
                    # текст идёт в поле ввода. Кнопка сайта/реплика лишь
                    # показывает состояние — веб-конвейер сайта не вызывается.
                    diag.event("mic.dictation_started")
                    return
                diag.event("mic.dictation_unavailable")

                # Запасной путь: штатный сценарий клика по кнопке микрофона.
                def on_mic_eval(val):
                    if isinstance(val, dict) and "x" in val and "y" in val:
                        self._cdp_click_coords(val["x"], val["y"])
                    else:
                        reason = val.get("reason") if isinstance(val, dict) else "no_result"
                        diag.event("mic.button_missing", reason=str(reason))
                        self.mic_active = False
                        self.chip_mic.set_active(False)
                        self._set_status(
                            "Микрофон недоступен — проверьте разрешения Windows",
                            THEME["danger"],
                        )

                self._page_eval_async(JS_START_RECORDING, timeout=3, callback=on_mic_eval)

            # Явный старт (не toggle): если состояние страницы рассинхронизировалось,
            # первое нажатие всё равно должно НАЧАТЬ запись, а не отправить.
            self._page_eval_async(JS_MIC_START, timeout=3, callback=on_shim_eval)
        else:
            # ─────────────────────────────────────────────────────────────
            # 2-е нажатие: отправка запроса в чат ИИ (п.3 ТЗ)
            # ─────────────────────────────────────────────────────────────
            # Отправляем ТОЛЬКО когда диктовка дала текст. Если хост
            # умер/язык не поддерживается, второе нажатие обязано
            # СНОВА начать запись, а не отправить пустой запрос
            # (реальный лог v6: "нажимаю микрофон — сразу отправка").
            if not self._mic_got_text:
                self._mic_cancel("Запись не началась — нажмите «Микрофон» ещё раз")
                return

            self.mic_active = False
            self._mic_got_text = False
            self.chip_mic.set_active(False)
            self._set_status("Отправка запроса…", THEME["accent"])

            def on_send_eval(val):
                if isinstance(val, dict) and "x" in val and "y" in val:
                    self._cdp_click_coords(val["x"], val["y"])
                elif not (isinstance(val, dict) and val.get("ok") is True):
                    diag.event("mic.send_missing")
                    self._set_status("Кнопка отправки не найдена — проверьте страницу", THEME["danger"])

            def on_stop_eval(_val):
                # Сначала останавливаем собственную диктовку (если она шла),
                # затем штатный сценарий: клик по кнопке отправки или Enter.
                self._page_eval_async(JS_STOP_AND_SEND, timeout=3, callback=on_send_eval)

            self._page_eval_async(JS_MIC_STOP, timeout=3, callback=on_stop_eval)
            QTimer.singleShot(2500, lambda: self._set_status("Готов к работе", THEME["ok"]))

    def _native_mic_start(self):
        """Первое нажатие: запустить ШТАТНУЮ запись голоса страницы."""

        def on_click(val):
            if not (isinstance(val, dict) and val.get("ok")):
                diag.event("mic.native_button_missing",
                           reason=str((val or {}).get("reason")))
                self._mic_reset_state()
                self._set_status("Кнопка микрофона не найдена — обновите страницу (Ctrl+R)",
                                 THEME["danger"])
                return
            diag.event("mic.native_started")
            QTimer.singleShot(700, self._native_mic_verify)

        self._page_eval_async(JS_START_RECORDING, timeout=3, callback=on_click)

    def _native_mic_verify(self):
        """Убеждаемся, что страница ДЕЙСТВИТЕЛЬНО записывает.

        Прежняя сборка просто отправляла запрос, не проверяя этого, — отсюда
        «нажимаю на микрофон и всё, сразу отправка без записи».
        """
        def on_check(val):
            recording = bool(isinstance(val, dict) and val.get("recording"))
            diag.event("mic.native_verify", recording=recording)
            if recording:
                # Сайт записывает сам: повторное нажатие = отправка.
                self._mic_got_text = True
                self._set_status("Идёт запись… (повтор: отправить)", THEME["ok"])
                return
            if not self._mic_retried:
                # Клик мог не дойти во время перерисовки — один повтор.
                self._mic_retried = True
                self._page_eval_async(JS_START_RECORDING, timeout=3)
                QTimer.singleShot(900, self._native_mic_verify)
                return
            self._mic_retried = False
            self._mic_reset_state()
            self._set_status(
                "Запись не началась — разрешите микрофон в Windows и повторите (F3)",
                THEME["danger"])

        self._page_eval_async(JS_CHECK_RECORDING, timeout=3, callback=on_check)

    def _open_template(self):
        overlay_was_visible = self.overlay.isVisible()
        if overlay_was_visible:
            self.overlay.hide()

        dlg = TemplateWindow(self.cfg, token=self.token, parent=self)
        dialog_result = dlg.exec()

        if overlay_was_visible:
            self.overlay.show()
            self.overlay.sync_geometry()
            force_topmost(int(self.overlay.winId()))

        if dialog_result == QDialog.DialogCode.Accepted:
            save_template(self.cfg, dlg.get_template())
            self._reload_prompt()

    @pyqtSlot()  # Explicit zero-argument Qt slot: clicked(bool) must not reach @stage.
    @stage
    def _sync_template_from_server(self):
        if not self.token:
            return
        self._template_worker = TemplateSyncWorker(self.token, self)
        self._template_worker.done.connect(self._apply_server_template)
        self._template_worker.start()

    def _apply_server_template(self, server_template):
        local_template = self.cfg.get("template", {}) or {}
        changed = False
        for key in ("Name", "ID", "Fraction", "Rang", "Department", "JobTitle"):
            value = str(server_template.get(key, "") or "").strip()
            if value and value != str(local_template.get(key, "") or "").strip():
                local_template[key] = value
                changed = True
        if changed:
            save_template(self.cfg, local_template)
            if self.worker and self.worker.page:
                self._reload_prompt()

    def _choose_prompt(self):
        try:
            res = requests.get(
                f"{SERVER_URL}/api/client/prompts",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=10,
            )
            data = res.json()

            if not data.get("success") or not data.get("data"):
                QMessageBox.information(self, "Промты", "Нет доступных промтов на сервере")
                return

            files = data.get("data", [])
            items = [f.get("filename", "") for f in files if f.get("filename")]

            if not items:
                QMessageBox.information(self, "Промты", "Нет доступных промтов на сервере")
                return

            display = [prompt_display_name(i) for i in items]
            current = self.cfg.get("selected_prompt", "")
            idx = items.index(current) if current in items else 0

            overlay_was_visible = self.overlay.isVisible()
            if overlay_was_visible:
                self.overlay.hide()

            dlg = PromptSelectionWindow(items, display, idx, self)
            dialog_result = dlg.exec()

            if dialog_result != QDialog.DialogCode.Accepted or not dlg.choice:
                if overlay_was_visible:
                    self.overlay.show()
                    self.overlay.sync_geometry()
                    force_topmost(int(self.overlay.winId()))
                return

            item = dlg.choice

            res2 = requests.post(
                f"{SERVER_URL}/api/client/prompts",
                json={"filename": item},
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=10,
            )

            if res2.json().get("success"):
                self.cfg["selected_prompt"] = item
                save_config(self.cfg)
                self._update_prompt_label()
                self._reload_prompt()
            else:
                QMessageBox.warning(self, "Ошибка", "Сервер не подтвердил выбор промта")
                if overlay_was_visible:
                    self.overlay.show()

        except Exception as e:
            diag.exception("main.py:3307")
            QMessageBox.warning(self, "Ошибка", str(e))
            if 'overlay_was_visible' in locals() and overlay_was_visible:
                self.overlay.show()

    def _reload_prompt(self):
        if not self.worker or not getattr(self.worker, "page", None):
            return

        self.pdf_attached = False
        self.pdf_was_loaded_once = False
        self._last_attached_state = None
        self._set_dot("#f59e0b")
        self._show_overlay("Синхронизация", "Обновление служебных файлов…")
        self._page_eval_async(JS_REMOVE_ALL_FILES, 3)
        QTimer.singleShot(650, lambda: self._load_prompt(force=True))

    @pyqtSlot()  # Explicit zero-argument Qt slot: clicked(bool) must not reach @stage.
    @stage
    def _start_chrome(self):
        # Окно браузера — другой процесс со своей очередью ввода: отдаём ему
        # фокус по клику мыши внутри него (без хуков и перехвата клавиш).
        if getattr(self, "focus_timer", None) is None:
            self.focus_timer = QTimer(self)
            self.focus_timer.timeout.connect(self._poll_browser_focus)
            self.focus_timer.start(120)
        if self.worker is not None or self._closing:
            return
        # v18: без настоящего Chrome микрофон не работает штатно — предлагаем
        # поставить его ДО запуска. Если Chrome уже есть, вызов пустой.
        if self._offer_browser_install(self._browser_candidates()):
            return
        # Никаких миганий: до готовности страницы видна только шторка.
        self.browser_cover.show()
        self.browser_cover.raise_()
        self.overlay_state.emit("", "", False)
        self._set_status("Ожидание страницы ИИ…", THEME["muted"])

        # Штатный микрофон страницы живёт только в настоящем браузере.
        host = NativeHost(self, url=URL, profile=BROWSER_PROFILE,
                          width=W, height=H,
                          configured=(self.cfg.get("browser_path") or None),
                          lang=self.speech_lang,
                          light_theme=self.light_theme,
                          accept_languages=self.accept_languages)
        # ВАЖНО: сначала `browser_host`, потом расчёт сдвига. `_browser_inset`
        # берёт ФИЗИЧЕСКИЙ размер плейсхолдера через `browser_host.user32`;
        # без этого он уходил в резерв (413x651 вместо 453x735) и сдвиг
        # получался на 9 % меньше нужного (-9/-36 вместо -10/-40).
        self.browser_host = host
        # v17: хост поднимает шторку до показа окна браузера (п.1).
        host.cover_widget = self.browser_cover
        host.inset = self._browser_inset()
        host.ready.connect(self._on_browser_ready)
        host.failed.connect(self._on_browser_failed)
        if not host.start(self.browser_placeholder):
            self._start_embedded_fallback("no-browser")

    # ---------------------------------------------------------- v18: браузер
    def _browser_candidates(self):
        try:
            return native_browser.discover_browsers(app_dir(),
                                                    self.cfg.get("browser_path") or None)
        except Exception:
            diag.exception("main.discover")
            return []

    @staticmethod
    def _has_branded_browser(candidates) -> bool:
        """Настоящий Chrome (установленный или закреплённый) — есть?"""
        for cand in candidates or ():
            if str(getattr(cand, "kind", "") or "") in ("chrome", "portable"):
                return True
        return False

    def _offer_browser_install(self, candidates) -> bool:
        """Предложить поставить Chrome for Testing. True — запуск отложен (v18).

        Ничего не делает, если настоящий Chrome уже найден: на отлаженной
        машине поведение не меняется вообще.
        """
        if not AUTO_INSTALL_BROWSER or self._closing:
            return False
        if self._browser_install_offered or self._fetch_worker is not None:
            return False
        if self.cfg.get("browser_install_declined"):
            return False
        if self._has_branded_browser(candidates):
            return False
        self._browser_install_offered = True
        diag.event("browser.offer", candidates=[getattr(c, "kind", "?")
                                                for c in (candidates or ())][:6])
        answer = QMessageBox.question(
            self, "Нужен браузер для голосового ввода",
            "Голосовой ввод работает штатным микрофоном страницы, а он есть "
            "только в настоящем Google Chrome.\n\n"
            "Скачать официальный Chrome for Testing (около 170 МБ) прямо сейчас? "
            "Это и есть установка речевого ввода: никаких речевых пакетов "
            "Windows afterwards не потребуется.\n\n"
            "Без него запись голоса пойдёт через распознавание Windows — а на "
            "Windows 11 оно обычно не установлено.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if answer != QMessageBox.StandardButton.Yes:
            try:
                self.cfg["browser_install_declined"] = True
                save_config(self.cfg)
            except Exception:
                diag.exception("main.decline_browser")
            return False
        self._start_browser_install()
        return True

    def _start_browser_install(self):
        dest = app_dir() / "browser"
        dialog = QProgressDialog("Подготовка…", "Отмена", 0, 100, self)
        dialog.setWindowTitle("Установка браузера")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setMinimumDuration(0)
        dialog.setValue(0)
        self._fetch_dialog = dialog
        worker = BrowserFetchWorker(dest, self)
        self._fetch_worker = worker
        worker.progress.connect(self._on_fetch_progress)
        worker.succeeded.connect(self._on_fetch_done)
        worker.failed.connect(self._on_fetch_failed)
        dialog.canceled.connect(worker.cancel)
        dialog.show()
        worker.start()
        self._set_status("Установка браузера…", THEME["muted"])

    def _on_fetch_progress(self, ratio, text):
        try:
            if self._fetch_dialog is not None:
                self._fetch_dialog.setValue(int(max(0.0, min(1.0, float(ratio))) * 100))
                self._fetch_dialog.setLabelText(str(text))
        except Exception:
            diag.exception("main.fetch_progress")

    def _close_fetch_dialog(self):
        try:
            if self._fetch_dialog is not None:
                self._fetch_dialog.close()
        except Exception:
            diag.exception("main.fetch_close")
        self._fetch_dialog = None
        self._fetch_worker = None

    @pyqtSlot(str)
    @stage
    def _on_fetch_done(self, path):
        diag.event("browser.installed", path=str(path))
        self._close_fetch_dialog()
        self._set_status("Браузер установлен — запуск…", THEME["ok"])
        if getattr(self, "web_browser", None) is not None:
            # Мы уже во встроенном движке: честнее перезапуститься целиком.
            restart_app()
            return
        QTimer.singleShot(300, self._start_chrome)

    @pyqtSlot(str)
    @stage
    def _on_fetch_failed(self, reason):
        text = str(reason)
        cancelled = "отмен" in text.lower()
        diag.event("browser.install_failed", reason=text[:200], cancelled=cancelled)
        self._close_fetch_dialog()
        if cancelled:
            self._set_status("Установка браузера отменена", THEME["muted"])
        else:
            self._set_status("Браузер не установлен — голос вводится диктовкой Windows",
                             THEME["danger"])
            QMessageBox.warning(
                self, "Браузер не установлен",
                "Не удалось скачать Chrome for Testing:\n%s\n\n"
                "Проверьте подключение к интернету, установите Google Chrome "
                "вручную или запустите tools\\fetch_chrome.bat, затем "
                "перезапустите программу." % text[:300])
        # Продолжаем тем, что есть: настоящего Chrome нет — пойдёт резерв.
        if getattr(self, "web_browser", None) is None and not self._closing:
            QTimer.singleShot(300, self._start_chrome)

    # -------------------------------------------------------- v18: «оживление»
    @diag.trace
    def _schedule_revive(self):
        if not REVIVE_AFTER_SHOW or self._closing:
            return
        # «Оживлять» нечего (резервный движок, страница ещё не готова) —
        # шторку тогда не поднимаем вообще, иначе она осталась бы навсегда.
        if not self.native_mode:
            return
        if getattr(getattr(self, "worker", None), "page", None) is None:
            return
        self._revive_settled = 0
        self._revive_last = None
        self._revive_step = 0
        self._settling = True
        # v20: до первого шага «оживления» фиксируем исходное состояние — иначе
        # в логе не будет видно, ОТКУДА браузер возвращался на место.
        btrace.mark("revive.schedule", step_ms=list(REVIVE_STEPS),
                    min_ms=int(REVIVE_MIN_MS), settle_n=int(REVIVE_SETTLE_N),
                    args=self._trace_extra())
        self._trace_landed_ms = None      # «когда встало на цель» — заново
        # v18.3: ровно тот же GUI прогрузки, что на этапе «Ожидание страницы»,
        # и тем же наивысшим приоритетом: индикатор — отдельное окно поверх
        # всех (WindowStaysOnTop + force_topmost), под ним — шторка.
        self._cover_to_top()
        self._show_overlay(REVIVE_TITLE, REVIVE_MESSAGE)
        for delay in REVIVE_STEPS:
            QTimer.singleShot(delay, self._revive_browser)
        # Страховка: GUI прогрузки не остаётся навсегда ни при каких условиях.
        QTimer.singleShot(int(REVIVE_STEPS[-1]) + 2500, self._settle_watchdog)

    def _settle_watchdog(self):
        """Снять шторку принудительно, если стабилизация не завершилась."""
        if not getattr(self, "_settling", False):
            return
        diag.event("browser.settle_watchdog")
        self._finish_settle(False)

    def _browser_sync_args(self):
        """(user32, hwnd, parent, inset) для СИНХРОННОГО переноса окна браузера.

        Всё, что зависит от Qt, берётся здесь, в ГЛАВНОМ потоке; в фоновый
        уходят только HWND и числа. None — переносить нечего.
        """
        if self._closing or not self.native_mode:
            return None
        host = getattr(self, "browser_host", None)
        if host is None or host.user32 is None or not host.hwnd:
            return None
        try:
            host.inset = self._browser_inset()
        except Exception:
            diag.exception("main.revive_inset")
        try:
            parent = int(self.browser_placeholder.winId())
        except Exception:
            diag.exception("main.revive_parent")
            parent = 0
        return host.user32, int(host.hwnd), parent, tuple(host.inset)

    def _sync_browser_now(self):
        """Поставить окно браузера на место СИНХРОННО (в фоновом потоке)."""
        args = self._browser_sync_args()
        if args is None:
            return
        user32, hwnd, parent, inset = args

        def task():
            try:
                win32_embed.sync_now(user32, hwnd, parent, inset)
            except Exception:
                diag.exception("main.sync_now")

        threading.Thread(target=task, daemon=True).start()


    def _revive_browser(self):
        """Держать GUI прогрузки, пока браузер не встанет на место (v18.3).

        Пока GUI прогрузки стоит, **окно браузера не трогаем вообще** — только
        измеряем. Это главное отличие от v18.2: каждый `SetWindowPos` /
        `ShowWindow` / `InvalidateRect` заставляет Chromium пересобрать кадр,
        а Chrome при этом поднимает своё окно — и пользователь видит мигание
        поверх шторки. Поэтому вся работа с окном делается **один раз**, в
        `_finalize_and_reveal()`, когда измерения показали, что всё устоялось.

        Снимаем GUI прогрузки только когда:
        * прошло не меньше `REVIVE_MIN_MS` (возмущение у Chromium начинается
          позже первых кадров);
        * **И** последние `REVIVE_SETTLE_N` измерения подряд одинаковы
          (`innerWidth`, `dpr`, размер окна, сдвиг);
        * либо наступил последний шаг `REVIVE_STEPS` (жёсткий предел).
        """
        if not getattr(self, "_settling", False):
            return          # уже устоялось — больше не дёргаем
        if self._closing or not self.native_mode:
            # Режим сменился или приложение закрывается — GUI снимаем, иначе
            # он остался бы навсегда.
            self._finish_settle(False)
            return
        args = self._browser_sync_args()
        if args is None:
            self._finish_settle(False)
            return
        user32, hwnd, parent, inset = args
        page = getattr(getattr(self, "worker", None), "page", None)
        step = int(getattr(self, "_revive_step", 0))
        self._revive_step = step + 1
        delay = int(REVIVE_STEPS[step]) if step < len(REVIVE_STEPS) else int(REVIVE_STEPS[-1])
        last_step = step >= len(REVIVE_STEPS) - 1
        # Шторка и индикатор — поверх (это главный поток Qt).
        self._cover_to_top()
        if page is None:
            if last_step:
                self._finish_settle(False)
            return

        def task():
            settled = 0
            try:
                # ТОЛЬКО измерение: окно не двигаем, не перерисовываем,
                # раскладку не трогаем — иначе Chrome лезет поверх шторки.
                signature = self._layout_signature(page, user32, hwnd, inset)
                if signature == self._revive_last:
                    settled = int(getattr(self, "_revive_settled", 0)) + 1
                else:
                    settled = 1
                self._revive_settled = settled
                self._revive_last = signature
                # v20: вместе с подписью — измерение ОКНА: где оно на самом
                # деле относительно цели и почему. Без этого «не встал на
                # позицию» остаётся словами.
                probe = btrace.window_probe(hwnd, parent, inset)
                diag.event("browser.revive", step=int(step), ms=int(delay),
                           settled=int(settled), inset=list(inset),
                           signature=list(signature or ()),
                           verdict=probe.get("verdict"), dx=probe.get("dx"),
                           dy=probe.get("dy"), dw=probe.get("dw"), dh=probe.get("dh"),
                           client=probe.get("client"), visible=probe.get("visible"),
                           explain=probe.get("explain"))
                if probe.get("verdict") not in (None, "on_target"):
                    btrace.mark("revive.off_target", step=int(step), ms=int(delay),
                                dx=probe.get("dx"), dy=probe.get("dy"),
                                reasons=probe.get("reasons"), client=probe.get("client"),
                                target=probe.get("target"), rect=probe.get("rect"))
                elif getattr(self, "_trace_landed_ms", None) is None:
                    # Первый замер, когда окно УЖЕ на цели: это и есть ответ на
                    # жалобу «съехало и вернулось через 2–3 секунды».
                    self._trace_landed_ms = int(delay)
                    btrace.mark("revive.on_target", step=int(step), ms=int(delay),
                                rect=probe.get("rect"), target=probe.get("target"),
                                client=probe.get("client"), scale=probe.get("scale"))
            except Exception:
                diag.exception("main.revive_measure")
            enough = (delay >= int(REVIVE_MIN_MS)
                      and settled >= int(REVIVE_SETTLE_N))
            if last_step or enough:
                self._finalize_and_reveal()

        threading.Thread(target=task, daemon=True).start()

    @diag.trace
    def _finalize_and_reveal(self):
        """ОДИН раз поставить окно и раскладку, пока GUI прогрузки ещё стоит.

        Вызывается, когда измерения показали, что браузер устоялся (или на
        жёстком пределе). Здесь и только здесь трогаем окно браузера, чтобы не
        провоцировать Chromium на перерисовку на глазах у пользователя.
        """
        args = self._browser_sync_args()
        if args is None:
            self._finish_settle(False)
            return
        user32, hwnd, parent, inset = args
        page = getattr(getattr(self, "worker", None), "page", None)

        def task():
            try:
                try:
                    win32_embed.sync_now(user32, hwnd, parent, inset)
                    win32_embed.show_window(user32, hwnd)
                    win32_embed.invalidate(user32, hwnd)
                    # v20: сразу после переноса — измерение «встало ли на цель».
                    # Именно этот замер и отвечает на вопрос пользователя.
                    probe = btrace.record_win_call("revive.reveal", True, hwnd=hwnd,
                                                   parent=parent, inset=inset,
                                                   extra={"stage": "finalize"})
                    btrace.mark("revive.reveal", verdict=(probe or {}).get("verdict"),
                                dx=(probe or {}).get("dx"), dy=(probe or {}).get("dy"),
                                dw=(probe or {}).get("dw"), dh=(probe or {}).get("dh"),
                                client=(probe or {}).get("client"),
                                reasons=(probe or {}).get("reasons"))
                except Exception:
                    diag.exception("main.revive_win32")
                if page is not None:
                    try:
                        page.send("Page.bringToFront", {}, timeout=3)
                    except Exception:
                        pass
                    try:
                        page.eval(JS_REVIVE, timeout=3)
                    except Exception:
                        pass
                    # Раскладка — ИЗМЕРЕНИЕМ и, если нужно, одним вызовом CDP.
                    ok = self._zoom_guard_once(page, reason="revive-final",
                                               pin_first=True)
                    if ok:
                        self._nudge_metrics(page)
                # Даём кадру лечь: Chromium перерисовывает не мгновенно.
                time.sleep(max(0.05, float(REVIVE_FINAL_MS) / 1000.0))
                # v19: снимаем GUI прогрузки по ЗАМЕРУ страницы, а не только
                # по таймеру: «объекты внутри браузера ещё не на своих местах»
                # — это ровно тот случай, когда страница не дорисована.
                ready = self._wait_page_complete(page, REVIVE_REVEAL_GATE_MS)
                diag.event("browser.reveal_gate", ready=ready)
            except Exception:
                diag.exception("main.revive_final")
            self._finish_settle(True)
            # v19: после разворота страница проверяется, а не предполагается.
            self._probe_surface("restore")

        threading.Thread(target=task, daemon=True).start()

    def _probe_surface(self, when="timer"):
        """Проверить, что открыто в браузере, и записать это в лог (v19).

        Отдельным потоком: CDP-запрос не должен задерживать поток GUI ни при
        развороте окна, ни при показе страницы.
        """

        def task():
            try:
                page = self._current_page()
                if page is None:
                    return
                surface = probe_page_surface(page, timeout=3)
                verdict = str(surface.get("verdict") or "unknown")
                diag.state("page.verdict", verdict)
                if verdict != getattr(self, "_surface_verdict", None):
                    previous = str(getattr(self, "_surface_verdict", "") or "")
                    self._surface_verdict = verdict
                    diag.event("page.surface_changed", verdict=verdict,
                               previous=previous, when=str(when))
                    # v20: поверхность сменилась — снимаем её структуру и «код».
                    # Это ровно тот случай, когда страница отдала НЕ тот чат, и
                    # по снимку видно, чем она на самом деле оказалась.
                    shots = int(getattr(self, "_trace_surface_shots", 0) or 0)
                    if shots < 4:
                        self._trace_surface_shots = shots + 1
                        btrace.capture_page(page, reason="surface-%s" % verdict, full=True)
                        btrace.mark("page.surface_captured", verdict=verdict,
                                    previous=previous, shot=shots + 1)
                diag.event("page.surface_after_%s" % re.sub(r"\W+", "", str(when)),
                           **{k: surface.get(k) for k in
                              ("verdict", "ready", "composer", "files", "chips",
                               "dark", "bg", "account", "pending", "host")})
            except Exception:
                diag.exception("main.probe_surface")

        threading.Thread(target=task, name="SurfaceProbe",
                         daemon=True).start()

    def _wait_page_complete(self, page, limit_ms):
        """Дождаться `readyState == "complete"`, но не дольше `limit_ms` (v19).

        Возвращает True (дождались), False (предел) или None (страницы/соединения
        нет). GUI прогрузки снимается в любом случае: залипший индикатор хуже,
        чем недодержанный.
        """
        if page is None:
            return None
        deadline = time.monotonic() + max(0.0, float(limit_ms) / 1000.0)
        while True:
            try:
                if probe_page_surface(page, timeout=2).get("ready") == "complete":
                    return True
            except Exception:
                diag.exception("main.reveal_gate")
                return None
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def _layout_signature(self, page, user32, hwnd, inset):
        """Подпись текущего состояния: (innerWidth, dpr, размер окна, сдвиг).

        Пока Chromium «устаивается», подпись меняется — по ней и определяется
        момент, когда браузер действительно встал на место.
        """
        try:
            metrics = native_browser.page_metrics(page)
            inner = int((metrics or {}).get("innerWidth") or 0)
            dpr = round(float((metrics or {}).get("dpr") or 0.0), 3)
        except Exception:
            diag.exception("main.revive_metrics")
            inner, dpr = 0, 0.0
        try:
            client = tuple(int(v) for v in win32_embed.client_size(user32, hwnd))
        except Exception:
            diag.exception("main.revive_client")
            client = (0, 0)
        return (inner, dpr, client[0], client[1], tuple(int(v) for v in inset))

    def _finish_settle(self, stable):
        """Снять шторку (сигнал уходит в главный поток Qt)."""
        if not getattr(self, "_settling", False):
            return
        try:
            self.revive_settled.emit(bool(stable))
        except Exception:
            diag.exception("main.revive_signal")
            self._settling = False
            try:
                self._hide_overlay()
            except Exception:
                diag.exception("main.revive_reveal_fallback")

    def _nudge_metrics(self, page):
        """На 1 px изменить ширину эмуляции и вернуть: Chromium рисует новый кадр.

        Делается ТОЛЬКО если метрики и так переопределены ровно в цель зума,
        — иначе раскладка была бы изменена. Проверка по измерению, не по догадке.
        """
        try:
            metrics = native_browser.page_metrics(page)
        except Exception:
            diag.exception("main.nudge_metrics")
            return
        inner = int((metrics or {}).get("innerWidth") or 0)
        logical_w, logical_h, physical_w, physical_h = self._zoom_args()
        css_w, css_h, dsf = native_browser.target_css(
            logical_w, logical_h, physical_w, physical_h, PAGE_ZOOM)
        if not inner or abs(inner - css_w) > 2:
            return
        try:
            native_browser.apply_viewport_raw(page, css_w + 1, css_h, dsf)
            time.sleep(0.06)
            native_browser.apply_viewport_raw(page, css_w, css_h, dsf)
            diag.event("browser.metrics_nudged", css=(int(css_w), int(css_h)))
        except Exception:
            diag.exception("main.metrics_nudge")

    @pyqtSlot(int)
    @stage
    @diag.trace
    def _on_browser_ready(self, port):
        if self._closing:
            return
        self.native_mode = True
        self.cdp_port = int(port)
        self.chrome_hwnd = int(self.browser_host.hwnd or 0)
        diag.event("browser.ready", port=self.cdp_port, hwnd=self.chrome_hwnd)
        # v19: чем именно показывается страница. Это первое, что нужно знать по
        # логу: настоящий Chrome (штатный микрофон страницы) или встроенный
        # движок. Раньше в логе этого не было вообще.
        try:
            exe = getattr(getattr(self.browser_host, "native", None), "exe", None)
            kind = getattr(getattr(self.browser_host, "native", None), "kind", None)
        except Exception:
            exe, kind = None, None
        diag.event("browser.mode", engine="external_chrome" if self.native_mode
                   else "qtwebengine", exe=str(exe or ""), kind=str(kind or ""),
                   port=self.cdp_port, hwnd=self.chrome_hwnd,
                   zoom=float(PAGE_ZOOM), inset=list(self._browser_inset()),
                   dx=int(BROWSER_DX), dy=int(BROWSER_DY),
                   light_theme=bool(self.light_theme), lang=str(self.speech_lang),
                   placeholder=[self.browser_placeholder.width(),
                                self.browser_placeholder.height()],
                   dpr=float(self.devicePixelRatioF()),
                   screens=len(QApplication.screens()), url=str(URL))
        diag.state("browser.mode", "external_chrome" if self.native_mode
                   else "qtwebengine")
        self._set_status("Подключение к странице ИИ…", THEME["muted"])
        # Окно браузера только что стало дочерним: шторка снова сверху, поэтому
        # сырой браузер не виден ни на одной машине.
        self._cover_to_top()
        # v17: Chrome поднимает окна и меняет z-порядок не сразу — первые
        # секунды повторяем подъём шторки с той же частотой, что и скрытие
        # кнопки в панели задач.
        for delay in (300, 800, 1500, 2500, 4000):
            QTimer.singleShot(delay, self._hold_cover_if_needed)
        self._start_web_automation()
        # Окно дочернего процесса подстраивается под плейсхолдер не сразу.
        QTimer.singleShot(500, self._sync_chrome_geometry)
        QTimer.singleShot(1500, self._sync_chrome_geometry)
        # Реальный физический размер окна браузера — от него считается зум.
        self.browser_physical = self._placeholder_physical_size()
        self.browser_logical = self._placeholder_logical_size()
        # Точка отсчёта для отслеживания переезда на другой масштаб (v16).
        try:
            self._last_dpr = float(self.devicePixelRatioF())
        except Exception:
            self._last_dpr = 0.0
        # v16: поздние окна браузера (рендерер, утилиты) поднимаются уже ПОСЛЕ
        # `ready` — убираем их с запасом, чтобы ни на экране, ни в Alt+Tab не
        # появился «сырой» браузер с неудалёнными элементами.
        QTimer.singleShot(0, self._hide_strays_once)
        QTimer.singleShot(1500, self._hide_strays_once)
        QTimer.singleShot(3000, self._hide_strays_once)
        # Масштаб держится намертво: если страница перезагрузилась или сменила
        # таргет, метрики возвращаются к «широкой» раскладке — следим.
        if getattr(self, "zoom_timer", None) is None:
            self.zoom_timer = QTimer(self)
            self.zoom_timer.timeout.connect(self._ensure_zoom)
        self.zoom_timer.start(5000)
        # Сдвиг пересчитывается по настоящему плейсхолдеру: при `_start_chrome`
        # окно Qt ещё могло быть без корректного HWND.
        try:
            host = getattr(self, "browser_host", None)
            if host is not None:
                host.inset = self._browser_inset()
                host.sync()
        except Exception:
            diag.exception("main.reinset")
        # v15: Chrome сам возвращает кнопку в панель задач — проверяем раз в 5 с.
        self.taskbar_timer.start(5000)
        # Сдвиги страницы из блока настроек — один раз, когда страница уже есть.
        if page_tuning_needed():
            QTimer.singleShot(1500, self._apply_page_tuning)

    @pyqtSlot(str)
    @stage
    def _on_browser_failed(self, reason):
        if self._closing:
            return
        diag.event("browser.host_failed", reason=str(reason))
        self._start_embedded_fallback(str(reason))

    @stage
    def _start_embedded_fallback(self, reason):
        """Резерв: встроенный QtWebEngine.

        Используется только если настоящий браузер не найден или не запустился.
        Штатного микрофона страницы здесь нет (движок без ключа Google), поэтому
        запись голоса идёт через Windows-диктовку — пользователь видит об этом
        честный статус.
        """
        if self._closing or getattr(self, "web_browser", None) is not None:
            return
        diag.event("browser.fallback_embedded", reason=str(reason))
        # v18: встроенный движок — значит запись пойдёт диктовкой Windows,
        # которой у большинства пользователей Win11 просто нет. Предлагаем
        # настоящий Chrome: это и есть лечение, а не косметика.
        if self._offer_browser_install(self._browser_candidates()):
            return
        self.native_mode = False
        self.cdp_port = DEBUG_PORT
        self.web_browser = BrowserPane(PROFILE, self.browser_placeholder)
        self.web_browser.setGeometry(self.side_inset, self.top_inset,
            W - 2 * self.side_inset, H - self.top_inset - self.bottom_inset)
        self.web_browser.show()
        self.web_browser.view.loadFinished.connect(self._web_load_finished)
        self._hook_speech_feedback()
        self.browser_cover.show()
        self.browser_cover.raise_()
        if str(reason) == "no-browser":
            self._set_status("Браузер со штатным микрофоном не найден — "
                             "диктовка Windows (на Win11 её обычно нет)",
                             THEME["danger"])
        else:
            self._set_status("Браузер не запустился — диктовка Windows "
                             "(на Win11 её обычно нет)", THEME["danger"])
        self._start_web_automation()
        self.web_browser.open_home()

    def _poll_browser_focus(self):
        """Внепроцессное окно имеет свою очередь ввода: отдаём фокус по клику."""
        host = getattr(self, "browser_host", None)
        if host is None:
            return
        # v17: сторож работает с САМОГО СТАРТА браузера, а не только после
        # `ready` (раньше он требовал `native_mode`, тот включался уже в
        # `_on_browser_ready` — то есть весь этап «Ожидание страницы» шторку
        # никто не держал, и неудалённые элементы промелькивали). Теперь
        # шторка держится, пока чат не готов ИЛИ пока не выбран промт.
        self._hold_cover_if_needed()
        if not self.native_mode:
            return
        try:
            host.poll_focus(int(self.winId()), self.isActiveWindow())
        except Exception:
            diag.exception("main.focus_timer")

    # Диагностика диктовки: понятные русские тексты вместо кодов DOMException.
    # v18: у каждой проблемы с распознаванием Windows есть ОДНА настоящая
    # причина — нет браузера со штатным микрофоном. Поэтому каждая подсказка
    # говорит не только про Windows, но и про то, что лечит причину.
    MIC_ERROR_MESSAGES = {
        "not-allowed": "Нет доступа к микрофону — разрешите его в Параметрах Windows",
        "audio-capture": "Микрофон не найден — проверьте устройство записи",
        "language-not-supported": "Нет русского распознавателя речи — установите речевой пакет Windows",
        "no-speech": "Речь не распознана — повторите (F3)",
        "service-not-allowed": "Диктовка Windows недоступна — установите Google Chrome со штатным микрофоном",
        "network": "Сбой диктовки — повторите (F3)",
        "aborted": "Запись прервана",
    }

    # Куда ведёт «вылечить причину»: штатный микрофон страницы.
    BROWSER_HINT = (" Голосовой ввода штатно работает в Chrome: перезапустите "
                    "программу и согласитесь на установку браузера.")

    def _mic_reset_state(self):
        """Снять «активную запись»: следующее нажатие снова НАЧИНАЕТ запись."""
        self.mic_active = False
        self._mic_got_text = False
        try:
            self.chip_mic.set_active(False)
        except Exception:
            pass

    def _mic_cancel(self, reason):
        """Повторное нажатие без распознанного текста: НИКОГДА не отправляем
        пустой запрос в чат ИИ (реальный дефект v6)."""
        self._mic_reset_state()
        self._page_eval_async(JS_MIC_STOP, timeout=3)
        self._set_status(reason, THEME["muted"])
        diag.event("mic.cancelled_no_text")

    def _on_speech_error(self, code):
        detail = ""
        try:
            engine = self.web_browser.speech_bridge.engine
            detail = str((getattr(engine, "last_error", None) or {}).get("msg") or "")[:200]
        except Exception:
            detail = ""
        text = self.MIC_ERROR_MESSAGES.get(code) or ("Ошибка диктовки: %s" % code)
        low = detail.lower()
        if code == "service-not-allowed" and ("recognizer" in low or "speech" in low):
            # v18: самое частое сообщение у пользователей Windows 11. Его
            # причина — не «сломанная Windows», а отсутствие браузера со
            # штатным микрофоном страницы. Говорим об этом прямо.
            text = ("В Windows нет распознавателя речи — на Windows 11 оно не "
                    "ставится по умолчанию." + self.BROWSER_HINT)
        elif code == "language-not-supported" and "recognizer" in low:
            text = ("Нет распознавателя для этого языка — установите русский "
                    "речевой пакет (Параметры → Время и язык → Речь)."
                    + self.BROWSER_HINT)
        # Любая «мёртвая» диктовка снимает активную запись: иначе следующее
        # нажатие ушло бы в ветку «стоп + отправка» и отправило пустой запрос.
        if code in ("service-not-allowed", "language-not-supported",
                    "audio-capture", "not-allowed"):
            self._mic_reset_state()
        elif self.mic_active and not self._mic_got_text:
            self._mic_reset_state()
        self._set_status(text, THEME["danger"])
        diag.event("mic.speech_error", code=code, detail=detail[:200])
        # v18: лечим ПРИЧИНУ в момент отказа, а не только при запуске. Если
        # настоящего Chrome нет — сразу предлагаем его поставить (после
        # установки приложение перезапустится и микрофон станет штатным).
        if code in ("service-not-allowed", "language-not-supported"):
            try:
                if self._offer_browser_install(self._browser_candidates()):
                    text = ("Ставлю браузер со штатным микрофоном — после "
                            "установки программа перезапустится")
                    self._set_status(text, THEME["muted"])
            except Exception:
                diag.exception("main.speech_error_offer")

    def _hook_speech_feedback(self):
        """Видимая обратная связь диктовки: слушаю / распознано / ошибка.

        Сигналы приходят из потока чтения PowerShell-хоста; Qt доставляет их в
        главный поток автоматически (queued connection). v7: любое завершение
        хоста без текста снимает «активную запись», чтобы повторное нажатие
        микрофона снова начало запись, а не отправило пустой запрос.
        """
        if getattr(self, "_speech_hooked", False):
            return
        bridge = getattr(self.web_browser, "speech_bridge", None)
        if bridge is None:
            return
        self._speech_hooked = True

        def on_started():
            notice = None
            try:
                notice = self.web_browser.speech_bridge.engine.last_notice
            except Exception:
                notice = None
            if notice and notice[0] == "fallback":
                # Распознаватель запрошенного языка не установлен — хост
                # взял тот, что есть. Честно говорим об этом пользователю
                # (v18: и сразу объясняем, как получить русский).
                self._set_status(
                    "Слушаю… (распознаватель %s, а не %s — установите русский "
                    "речевой пакет или браузер со штатным микрофоном)"
                    % (str(notice[1]), self.speech_lang), THEME["muted"])
                diag.event("mic.lang_fallback_shown", lang=str(notice[1]),
                           wanted=str(self.speech_lang))
            else:
                self._set_status("Слушаю… говорите", THEME["ok"])

        def on_hypothesis(text):
            if str(text or "").strip():
                self._mic_got_text = True

        def on_result(text, _confidence):
            if str(text or "").strip():
                self._mic_got_text = True
            self._set_status("Распознано: %s" % (str(text or "")[:48] or "—"), THEME["ok"])

        def on_error(code):
            self._on_speech_error(str(code))

        def on_ended():
            if self.mic_active and not self._mic_got_text:
                self._mic_reset_state()
                self._set_status("Речь не распознана — нажмите «Микрофон» ещё раз",
                                 THEME["muted"])
                diag.event("mic.no_text_on_end")

        try:
            bridge.started.connect(on_started)
            bridge.hypothesis.connect(on_hypothesis)
            bridge.result.connect(on_result)
            bridge.error.connect(on_error)
            bridge.ended.connect(on_ended)
        except Exception:
            diag.exception("main.speech_feedback")

    def _start_web_automation(self):
        previous = self.worker
        if previous and previous.isRunning():
            return
        if previous:
            previous.close_connections()
        self._chat_ready = False
        self.worker = ChromeWorker(self, port=getattr(self, "cdp_port", DEBUG_PORT),
                                  native=self.native_mode, size=self._zoom_size(),
                                  lang=self.speech_lang, light=self.light_theme)
        self.worker.hwnd_ready.connect(self._on_hwnd)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def _web_load_finished(self, ok):
        if self._closing:
            return
        if not ok:
            # Падение рендер-процесса (например, из-за запроса микрофона на
            # движке без WebRTC) не оставляет пользователя с пустой страницей:
            # шторка + статус + автоматическая перезагрузка (до 3 раз).
            crashed = bool(getattr(self.web_browser, "crashed", False))
            if crashed and self._crash_recoveries < 3:
                self._crash_recoveries += 1
                try:
                    self.web_browser.crashed = False
                except Exception:
                    diag.exception("main.crash_flag")
                self._chat_ready = False
                self.mic_active = False
                self.chip_mic.set_active(False)
                self._set_status("Восстановление страницы…", THEME["muted"])
                self._show_overlay("Восстановление", "Страница ИИ перезагружается…")
                QTimer.singleShot(700, self._recover_web)
                diag.event("webengine.crash_recovery", attempt=self._crash_recoveries)
                return
            self._set_status("Страница не загружена — Ctrl+R", THEME["danger"])
            self._hide_overlay()
        else:
            # Страница загружена: зачистка уже внедрена при создании документа,
            # даём ей гарантированный проход и лишь затем открываем шторку.
            QTimer.singleShot(250, self._reveal_page)
            if self.worker and not self.worker.isRunning():
                self._set_status("Проверка готовности чата…", THEME["muted"])
                self._start_web_automation()

    def _reveal_page(self):
        if self._closing:
            return
        self._hide_overlay()

    def _recover_web(self):
        if self._closing:
            return
        # Зависшие потоки работают с мёртвым CDP-соединением — останавливаем,
        # чтобы не спамить ошибками; после готовности промт загрузится заново.
        try:
            if self.upload_thread and self.upload_thread.isRunning():
                self.upload_thread.stop()
                self.upload_thread.wait(500)
        except Exception:
            diag.exception("main.recover_upload_stop")
        if self.native_mode and getattr(self, "browser_host", None) is not None:
            # Настоящий браузер: перезапускаем хост целиком (свежий профиль /
            # без GPU / следующий браузер — по счётчику попыток).
            self.browser_host.stop()
            self.worker = None
            self._start_chrome()
            return
        self._start_web_automation()
        self.web_browser.open_home()

    @stage
    @diag.trace
    def _on_hwnd(self, hwnd):
        try:
            if not hwnd:
                self._on_failed()
                return

            self.mic_active = False
            self.chip_mic.set_active(False)
            self._chat_ready = True
            # v19: что именно открылось — чат, режим ИИ в поиске или вход.
            surface = getattr(self.worker, "surface", None) or {}
            self._surface_verdict = str(surface.get("verdict") or "unknown")
            self._surface_blocked = str(surface.get("blocked") or "")
            if self._surface_blocked in ("login", "consent"):
                self._set_status("Войдите в аккаунт Google в окне чата",
                                 THEME["danger"])
            elif self._surface_blocked:
                self._set_status("Страница не распознана — обновите её (Ctrl+R)",
                                 THEME["danger"])

            self.browser_placeholder.update()

            if self.native_mode:
                # Окончательная подгонка окна браузера под плейсхолдер и
                # раскрытие страницы (loadFinished здесь не приходит — браузер
                # внешний, поэтому шторку снимаем сами).
                QTimer.singleShot(200, self._sync_chrome_geometry)
                if not self._check_speech_surface():
                    return
                QTimer.singleShot(250, self._reveal_page)
                QTimer.singleShot(600, self._sync_chrome_geometry)

            force_topmost(int(self.winId()))
            self._set_status("Готов к работе", THEME["ok"])

            # v20: чат готов — фиксируем это событие и снимаем структуру страницы
            # («код» один раз). По снимку видно, та ли поверхность открылась и
            # где именно стоит поле ввода.
            btrace.mark("chat.ready", verdict=self._surface_verdict,
                        blocked=self._surface_blocked, args=self._trace_extra())
            if not getattr(self, "_trace_chat_captured", False):
                self._trace_chat_captured = True
                self._trace_page("chat-ready", full=True)

            self._load_prompt()

        except Exception:
            diag.exception("main.py:3369")
            self._on_failed()

    def _check_speech_surface(self):
        """Проверяем, умеет ли этот браузер штатный голосовой ввод страницы.

        QtWebEngine/vanilla Chromium/Edge отдают объект, который либо падает,
        либо молчит. Если ни Web Speech, ни getUserMedia недоступны — пробуем
        следующий браузер из списка (у пользователя может быть и Chrome, и Edge).
        """
        if not self.native_mode or self.worker is None or self.worker.page is None:
            return True
        try:
            surface = speech_surface(self.worker.page)
        except Exception:
            diag.exception("main.speech_surface")
            return True
        if surface.get("sr") or surface.get("gum"):
            return True
        if self._browser_demoted or self.browser_host is None:
            self._set_status(
                "В этом браузере нет голосового ввода — установите Google Chrome",
                THEME["danger"])
            return False
        self._browser_demoted = True
        diag.event("browser.demote", reason="no-speech-surface")
        self._set_status("Голосовой ввод недоступен — пробуем другой браузер…",
                         THEME["muted"])
        QTimer.singleShot(400, self._restart_browser_next)
        return False

    def _restart_browser_next(self):
        if self._closing or self.browser_host is None:
            return
        host = self.browser_host
        try:
            if self.worker is not None:
                self.worker.close_connections()
        except Exception:
            diag.exception("main.demote_close")
        self.worker = None
        self._chat_ready = False
        self.browser_cover.show()
        self.browser_cover.raise_()
        if not host.restart_next():
            self._set_status("Голосовой ввод недоступен — установите Google Chrome",
                             THEME["danger"])
            self._hide_overlay()

    def _load_prompt(self, force=False) -> bool:
        sel = self.cfg.get("selected_prompt", "")
        if not sel:
            # v16: первый запуск на чистой машине (нет %APPDATA%\Legalyze).
            # Раньше здесь просто вставала красная шторка: браузер открывался,
            # GUI был ею закрыт, экспорт .pdf/.txt НЕ начинался — пока
            # пользователь сам не догадается нажать «Промт». Теперь открывается
            # тот же самый диалог выбора, что и по кнопке. Рабочий сценарий
            # (промт уже выбран) не меняется вообще.
            if not getattr(self, "_prompt_picker_shown", False):
                self._prompt_picker_shown = True
                diag.event("prompt.picker_auto", reason="no-selection")
                QTimer.singleShot(0, self._choose_prompt)
            self._show_overlay(
                "Выберите промт",
                "Сейчас откроется список промтов — выберите файл. Без него "
                "отправка запросов заблокирована.",
            )
            return False

        if self.prompt_loader and self.prompt_loader.isRunning():
            return True

        self._last_attached_state = None
        self._show_overlay("Подготовка промта", "Проверка версии на сервере…")
        self._set_dot("#f59e0b")

        self.prompt_loader = PromptLoaderWorker(self.cfg, self.token, force=force)
        self.prompt_loader.progress.connect(lambda m: self.overlay.set_message(m))
        self.prompt_loader.loaded.connect(self._on_prompt_loaded)
        self.prompt_loader.start()
        return True

    def _on_prompt_loaded(self, success, payload):
        if self._closing:
            return
        diag.event("prompt.prepared", success=bool(success))
        if success and isinstance(payload, dict):
            self.pdf_path = Path(payload.get("pdf", ""))
            self.template_path = Path(payload.get("template", "") or str(TEMPLATE_FILE))
            self.overlay.set_message(
                f"Экспорт «{self.pdf_path.name}» + «{TEMPLATE_EXPORT_NAME}» в чат…"
            )
            self._start_upload_thread()
        else:
            self.pdf_attached = False
            self._set_dot(THEME["danger"])
            self._show_overlay("Промт недоступен", str(payload), error=True)
            QTimer.singleShot(8000, lambda: self._load_prompt(force=True))

    def _start_upload_thread(self):
        if not self.worker or not getattr(self.worker, "page", None):
            return

        if not self.pdf_path or not Path(self.pdf_path).exists():
            self._load_prompt(force=True)
            return
        if not self.template_path or not Path(self.template_path).exists():
            self._load_prompt(force=True)
            return

        if self.upload_thread:
            # Актуальное CDP-соединение (после перезагрузки страницы старое
            # мертво) — иначе поток остаётся с мёртвым page навсегда.
            self.upload_thread.page = self.worker.page
            self.upload_thread.page_provider = self._current_page
        if self.upload_thread and self.upload_thread.isRunning() and self.upload_thread._stop:
            self._show_overlay("Предыдущая загрузка останавливается", "Перезапустите клиент перед повторной загрузкой.", error=True)
            return
        self._upload_failed = False
        self.upload_deadline_timer.start(120000)
        if self.upload_thread and self.upload_thread.isRunning():
            self.upload_thread.wait_started = time.monotonic()
            self.upload_thread.pdf_path = self.pdf_path
            self.upload_thread.template_path = self.template_path
            self.upload_thread.fails = 0
            self.upload_thread.last_upload = 0
            self.upload_thread.end_check_counter = 0
            self.upload_thread.files_loaded = False
            self.upload_thread._dedup_done = False
            return

        if self.upload_thread:
            try:
                self.upload_thread.wait(100)
            except Exception:
                diag.exception("main.py:3435")
                pass

        self.upload_thread = UploadThread(
            self.worker.page, self.pdf_path, self.template_path, self.token,
            page_provider=self._current_page,
        )
        self.upload_thread.decrement_signal.connect(self._on_decrement)
        self.upload_thread.attached.connect(self._on_files_attached)
        self.upload_thread.failed.connect(self._on_upload_failed)
        self.upload_thread.blocked.connect(self._on_upload_blocked)
        self.upload_thread.start()

    @pyqtSlot(str)
    def _on_upload_blocked(self, verdict):
        """Вложение не работает: исправляем поверхность, а не молчим (v19).

        Причина «Error - 1»: сервис отдал НЕ ту страницу (режим ИИ в поиске
        вместо чата) — на ней есть поле ввода и микрофон, но нет прикрепления
        файлов, поэтому .pdf и .txt не экспортировались, а в логе это выглядело
        как 92 успешные попытки. Теперь вердикт известен, и один раз открывается
        рабочая страница чата: предсказуемо на любой машине.
        """
        verdict = str(verdict or "unknown")
        diag.state("upload.blocked_verdict", verdict)
        if not AUTO_CORRECT_SURFACE or getattr(self, "_surface_corrected", False):
            return
        if verdict not in ("ai_mode", "google_other", "other", "blank"):
            return
        page = self._current_page()
        if page is None or self._closing:
            return
        self._surface_corrected = True
        diag.event("page.surface_corrected", verdict=verdict,
                   url=SURFACE_FALLBACK_URL,
                   was_host=str(getattr(self, "_surface_verdict", "")))
        try:
            page.send("Page.navigate", {"url": SURFACE_FALLBACK_URL}, timeout=10)
        except Exception:
            diag.exception("main.surface_correct_navigate")
            return
        self._show_overlay("Подготовка страницы",
                           "Открываем рабочую страницу чата…")
        self._restart_automation(reason="surface_corrected")

    def _restart_automation(self, reason=""):
        """Перезапустить автоматизацию страницы (v19): ворота пройдут заново."""
        diag.event("automation.restart", reason=str(reason))
        try:
            if self.worker is not None:
                self.worker.requestInterruption()
                self.worker.close_connections()
        except Exception:
            diag.exception("main.restart_automation_close")
        self.worker = None
        self._chat_ready = False
        try:
            if self.upload_thread and self.upload_thread.isRunning():
                self.upload_thread.stop()
        except Exception:
            diag.exception("main.restart_automation_upload")
        QTimer.singleShot(700, self._start_web_automation)

    def _current_page(self):
        """Актуальный CDP page воркера (None, если воркера/соединения нет)."""
        try:
            if self.worker and not self._closing:
                return self.worker.page
        except Exception:
            diag.exception("main.current_page")
        return None

    @pyqtSlot()
    def _upload_wait_expired(self):
        self._on_upload_failed("attachment_deadline")

    @pyqtSlot(str)
    def _on_upload_failed(self, reason):
        if self._closing or self._upload_failed:
            return
        self._upload_failed = True
        self.upload_deadline_timer.stop()
        if self.upload_thread:
            self.upload_thread.stop()
        self.pdf_attached = False
        diag.event("upload.failed", reason=reason)
        self._hide_overlay()
        QMessageBox.warning(self, "Файлы не прикреплены", "Ожидание остановлено. Проверьте вход в Google и доступность чата. "
                           "Закройте сообщение и проверьте страницу (Ctrl+R — обновить). Логи: " + str(diag.LOG_DIR))

    def _on_files_attached(self, attached: bool):
        if self._closing or self._upload_failed:
            return
        attached = bool(attached)
        if attached:
            self.upload_deadline_timer.stop()
        elif not self.upload_deadline_timer.isActive():
            self.upload_deadline_timer.start(120000)
        diag.event("upload.attachments_confirmed", complete=attached)
        if attached == self._last_attached_state:
            return
        self._last_attached_state = attached
        self.pdf_attached = attached

        if attached:
            self.pdf_was_loaded_once = True
            self._set_dot(THEME["ok"])
            self._set_status("Готов к работе", THEME["ok"])
            self._hide_overlay()
        else:
            self._set_dot("#f59e0b")
            if not self.pdf_was_loaded_once:
                if self.pdf_path and Path(self.pdf_path).exists():
                    self._show_overlay(
                        "Синхронизация данных",
                        f"Подключение конфигурации и шаблона «{TEMPLATE_EXPORT_NAME}»…",
                    )
            else:
                self._set_status("Синхронизация…", "#f59e0b")

    def _refresh_balance(self):
        self._balance_worker = BalanceWorker(self.token)
        self._balance_worker.done.connect(self._on_balance)
        self._balance_worker.start()

    @stage
    def _on_balance(self, ok, remaining, unlimited, unlimited_until):
        diag.event("balance.result", success=bool(ok), allowed=bool(unlimited or (remaining or 0) > 0))
        if ok:
            self.queries_remaining = int(remaining or 0)
            self.has_unlimited = bool(unlimited) or self.has_unlimited
            if unlimited_until:
                self.unlimited_until = unlimited_until
            self._update_query_label()

        if not self.has_unlimited and self.queries_remaining <= 0:
            self._set_dot(THEME["danger"])
            self._show_overlay(
                "Запросы исчерпаны",
                "На вашем аккаунте 0 доступных запросов. Доступ заблокирован.",
                error=True
            )
            return

        if hasattr(self, 'init_timer'):
            self.init_timer.start(50)

    def _on_decrement(self):
        self._decrement_worker = DecrementWorker(self.token)
        self._decrement_worker.done.connect(self._on_decrement_finished)
        self._decrement_worker.start()

    def _check_response_end_from_ui(self):
        if self._closing or self._response_poll_busy or not self.pdf_attached:
            return
        page = getattr(self.worker, "page", None) if self.worker else None
        if not page:
            return
        self._response_poll_busy = True
        def task():
            value = None
            try:
                value = int(page.eval(JS_CHECK_END, timeout=1).get("value") or 0)
            except Exception:
                diag.exception("response.poll")
            finally:
                self.response_poll_finished.emit(value)
        threading.Thread(target=task, name="ResponsePoll", daemon=True).start()

    @pyqtSlot(object)
    def _apply_response_poll(self, current_markers_count):
        self._response_poll_busy = False
        if self._closing or not self.pdf_attached or current_markers_count is None:
            return
        if not hasattr(self, "_first_check_done"):
            self._first_check_done = True
            self._last_decremented_response_state = current_markers_count
            return
        if current_markers_count > self._last_decremented_response_state:
            self._last_decremented_response_state = current_markers_count
            self._on_decrement()
        elif current_markers_count < self._last_decremented_response_state:
            self._last_decremented_response_state = current_markers_count

    def _on_decrement_finished(self, ok, remaining):
        if ok:
            self.queries_remaining = int(remaining or 0)
            self._update_query_label()

            if not self.has_unlimited and self.queries_remaining <= 0:
                QMessageBox.critical(
                    self,
                    "Запросы исчерпаны",
                    "Доступ к ИИ приостановлен. Пополните баланс для продолжения работы."
                )
                self._close_app()

    @pyqtSlot()  # Explicit zero-argument Qt slot: clicked(bool) must not reach @stage.
    @stage
    def _on_failed(self):
        if self._closing:
            return
        diag.event("webengine.not_ready", **getattr(self.worker, "failure", {}))
        self._hide_overlay()
        if self.native_mode and self._crash_recoveries < 2:
            # Страница не стала чатом (согласие/вход/сеть): перезапускаем
            # браузер целиком — это то, чего не хватало прежней сборке.
            self._crash_recoveries += 1
            self._set_status("Перезапуск браузера…", THEME["muted"])
            self.browser_cover.show()
            self.browser_cover.raise_()
            try:
                if self.browser_host is not None:
                    self.browser_host.stop()
            except Exception:
                diag.exception("main.host_stop")
            self.worker = None
            diag.event("browser.restart", attempt=self._crash_recoveries)
            QTimer.singleShot(800, self._start_chrome)
            return
        self._set_status("Войдите в Google; Ctrl+R — повтор", THEME["danger"])

    @stage
    def _cleanup(self):
        if self._cleaned:
            return

        self._cleaned = True
        self.upload_deadline_timer.stop()
        self.response_end_timer.stop()
        self.topmost_timer.stop()
        if getattr(self, "zoom_timer", None) is not None:
            self.zoom_timer.stop()
        if getattr(self, "taskbar_timer", None) is not None:
            self.taskbar_timer.stop()
        self.chrome_hwnd = None

        try:
            self.overlay.finish()
            self.overlay.deleteLater()
        except Exception:
            diag.exception("main.py:3560")
            pass

        try:
            if getattr(self, "browser_host", None) is not None:
                self.browser_host.stop()
        except Exception:
            diag.exception("main.py:host_cleanup")
            pass
        # Браузер снят вместе со всем деревом — журнал можно очистить.
        try:
            _pid_journal_write([])
        except Exception:
            diag.exception("main.pid_journal_clear")

        try:
            if self.upload_thread:
                self.upload_thread.stop()
                self.upload_thread.wait(1500)
        except Exception:
            diag.exception("main.py:3568")
            pass

        try:
            if self.prompt_loader and self.prompt_loader.isRunning():
                self.prompt_loader.wait(1000)
        except Exception:
            diag.exception("main.py:3575")
            pass

        worker = self.worker
        if worker:
            worker.requestInterruption()
            worker.close_connections()
        # Let bounded network tasks finish before QObject/profile destruction.
        for task in (worker, self.upload_thread, self.prompt_loader,
                     getattr(self, "_balance_worker", None), getattr(self, "_template_worker", None),
                     getattr(self, "_decrement_worker", None)):
            if task and task.isRunning():
                task.wait()
        if worker:
            worker.close_connections()
        if hasattr(self, "web_browser"):
            self.web_browser.shutdown()
        self._unregister_hotkeys()
        try:
            self.hotkeys.stop()
        except Exception:
            diag.exception("main.hotkeys_stop")

        try:
            if self.pdf_path and WORKSPACE.path in Path(self.pdf_path).parents:
                shred_file(self.pdf_path)
        except Exception:
            diag.exception("main.py:3623")
            pass

        try:
            WORKSPACE.dispose()
            purge_plaintext_artifacts(DATA_DIR)
        except Exception:
            diag.exception("main.py:3630")
            pass

        # v20: закрыть приборную панель браузера ПОСЛЕ остановки браузера и
        # рабочих потоков: здесь дописываются итог (`UNDERSTANDING.md`),
        # машинная сводка и сбрасываются на диск все файлы сбора. Без этого
        # последние строки оставались в буфере и пользователь копировал
        # неполную сессию (проверено: `browser-trace.txt` выходил 0 байт).
        try:
            btrace.stop_watchers()
            btrace.finish()
        except Exception:
            diag.exception("main.trace_finish")

    @pyqtSlot()  # Explicit zero-argument Qt slot: clicked(bool) must not reach @stage.
    @stage
    def _close_app(self):
        if self._closing:
            return

        self._closing = True
        self._cleanup()

        try:
            QApplication.instance().quit()
        except Exception:
            diag.exception("main.py:3644")
            pass

        diag.event("session.qt_exit", reason="close or restart")
        QApplication.instance().quit()

    @pyqtSlot()  # Explicit zero-argument Qt slot: clicked(bool) must not reach @stage.
    @stage
    def _restart(self):
        if self._closing:
            return

        self._closing = True
        self._cleanup()
        restart_app()
        diag.event("session.qt_exit", reason="close or restart")
        QApplication.instance().quit()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if event.position().y() <= self.top_inset:
                self.drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and not self.drag_position.isNull():
            self.move(event.globalPosition().toPoint() - self.drag_position)
            if self.overlay.isVisible():
                self.overlay.sync_geometry()
            event.accept()

    def mouseReleaseEvent(self, event):
        self.drag_position = QPoint()
        event.accept()

    def closeEvent(self, event):
        self._close_app()
        event.accept()


def _pid_journal_read() -> list:
    """pid браузера, который запускал ПРОШЛЫЙ экземпляр приложения."""
    try:
        data = json.loads(BROWSER_PIDS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [int(p) for p in data if str(p).lstrip("-").isdigit()]
    except Exception:
        pass
    return []


def _pid_journal_write(pids) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        BROWSER_PIDS_FILE.write_text(
            json.dumps(sorted({int(p) for p in pids})[:200]), encoding="utf-8")
    except Exception:
        diag.exception("main.pid_journal_write")


def _kill_leftover_browsers() -> None:
    """Добить браузер, оставшийся от прошлого запуска (только СВОЙ).

    Прошлый запуск мог упасть или быть убит: тогда его Chrome остаётся висеть
    в панели задач и держит профиль (синглтон), из-за чего новый процесс
    молча выходит. Имя процесса проверяется, чтобы никогда не снять чужой.
    """
    pids = _pid_journal_read()
    _pid_journal_write([])
    if not pids:
        return
    names = win32_embed.process_names()
    stale = [p for p in pids
             if str(names.get(p, "")).lower() in win32_embed.BROWSER_EXE_NAMES]
    if not stale:
        return
    user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None
    if user32 is not None:
        win32_embed.configure(user32)
    try:
        win32_embed.hide_stray_windows(user32, stale)
        win32_embed.kill_tree(stale)
    except Exception:
        diag.exception("main.leftover_kill")
    diag.event("browser.leftover_killed", pids=stale, known=len(pids))


def _migrate_config_secrets(cfg: dict) -> dict:
    try:
        pwd = cfg.get("password", "")
        if pwd and not pwd.startswith(("lgz1:", "lgz2:")):
            cfg["password"] = encrypt_secret(pwd)
            save_config(cfg)
    except Exception:
        diag.exception("main.py:3690")
        pass
    return cfg


def purge_stray_sources(app_dir, data_dir) -> int:
    r"""Удалить .py/.pyc, если они всё-таки оказались в хранилище (v16).

    Клиент НИКОГДА не записывает исходники в %APPDATA%\Legalyze. Проверено по
    всему коду: единственные записи в эту папку — config.json, Шаблон.txt,
    data/DejaVuSans.ttf, data/prompt_cache.json, data/prompts/*,
    browser-pids.json, логи и профиль браузера. Значит .py могут попасть туда
    только извне: из старой папки при миграции, от ручного запуска из исходников
    или если пользователь сам скопировал. Удаляем их и пишем в лог, чтобы в
    папке оставалось только то, что создаёт сама программа.
    """
    removed = 0
    for root in (app_dir, data_dir):
        try:
            d = Path(root)
            if not d.exists() or not d.is_dir():
                continue
            for pattern in ("*.py", "*.pyc", "*.pyo", "*.pyw", "*.spec"):
                for f in d.glob(pattern):
                    try:
                        if f.is_file():
                            f.unlink()
                            removed += 1
                    except Exception:
                        diag.exception("main.purge_stray_sources")
            cache = d / "__pycache__"
            if cache.is_dir():
                shutil.rmtree(cache, ignore_errors=True)
                removed += 1
        except Exception:
            diag.exception("main.purge_stray_sources_root")
    if removed:
        diag.event("storage.stray_py", removed=int(removed))
    return removed


@stage
def main():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    instance_lock = QLockFile(str(APP_DIR / "diagnostic-client.lock"))
    instance_lock.setStaleLockTime(0)
    if not instance_lock.tryLock(100):
        diag.event("instance.already_running")
        if user32:
            user32.MessageBoxW(None, "Диагностический клиент уже запущен.", "Legalyze", 0x30)
        return
    legacy_locks = []
    try:
        locked_roots = set()
        for label, old_root in storage_paths.legacy_roots():
            if old_root.exists() and old_root not in locked_roots:
                locked_roots.add(old_root)
                old_lock = QLockFile(str(old_root / "diagnostic-client.lock"))
                old_lock.setStaleLockTime(0)
                if not old_lock.tryLock(100):
                    raise RuntimeError("Old diagnostic client is still running")
                legacy_locks.append(old_lock)
        # Release lock file handles before directory rename (mandatory on Windows).
        # The old clients must be closed; browser-process preflight runs in migrator.
        for old_lock in legacy_locks:
            old_lock.unlock()
        for migration in storage_paths.migrate_legacy(APP_DIR, storage_paths.legacy_roots()):
            diag.event("storage.migrated", **migration)
    except Exception:
        diag.exception("storage.migration")
        if user32:
            user32.MessageBoxW(None,
                "Не удалось безопасно перенести старые данные. Закройте старые версии Legalyze и их Chromium, затем повторите запуск. "
                "Данные не удалялись принудительно. Логи: " + str(diag.LOG_DIR), "Legalyze — перенос данных", 0x30)
        return
    finally:
        for old_lock in legacy_locks:
            old_lock.unlock()
    diag.event("storage.paths", app_dir=str(APP_DIR), data_dir=str(DATA_DIR), profile=str(PROFILE), logs=str(diag.LOG_DIR))
    cfg = load_config()
    cfg = _migrate_config_secrets(cfg)
    try:
        ensure_template_file(cfg.get("template", {}))
    except Exception:
        diag.exception("main.py:3711")
        pass
    purge_plaintext_artifacts(DATA_DIR)
    # v16: в хранилище данных лежат только данные — исходники там не место.
    purge_stray_sources(APP_DIR, DATA_DIR)
    # v15: перед запуском своего браузера убираем окна прошлого запуска.
    _kill_leftover_browsers()

    app = QApplication(sys.argv)
    diag.install_qt(app)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(BASE_QSS)

    login_win = LoginWindow(cfg)
    result = {"cfg": cfg, "token": cfg.get("token", ""), "user": {}}

    def on_success(c, t, u):
        result["cfg"] = c
        result["token"] = t
        result["user"] = u

    login_win.login_success.connect(on_success)
    if login_win.exec() != QDialog.DialogCode.Accepted:
        sys.exit(0)

    cfg = result["cfg"]
    token = result["token"]
    user_data = result["user"]

    diag.event("auth.accepted")
    _enforce_latest_version(token)

    if not get_template_filled(cfg):
        tpl_win = TemplateWindow(cfg, token=token)
        if tpl_win.exec() == QDialog.DialogCode.Accepted:
            save_template(cfg, tpl_win.get_template())

    window = MainWindow(cfg, token, user_data)
    diag.place_window(window, app)
    window.show()
    window.raise_()
    window.activateWindow()
    diag.monitor_window(window)
    diag.event("main.shown", visible=window.isVisible(), geometry=window.geometry().getRect())
    force_topmost(int(window.winId()))

    sys.exit(app.exec())


@stage
def _enforce_latest_version(token: str):
    update_info = None
    try:
        update_info = check_for_update(token)
    except Exception:
        diag.exception("main.py:3779")
        pass
    if not update_info:
        return
    diag.event("update.required_exit")
    server_version = str(update_info.get("version", "") or "")
    QMessageBox.warning(
        None,
        "Доступна новая версия",
        f"Ваша версия: {CURRENT_VERSION}\n"
        f"Новая версия: {server_version}\n\n"
        "Скачайте новую версию с сайта и вручную замените файлы.\n"
        "Программа будет закрыта.",
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
