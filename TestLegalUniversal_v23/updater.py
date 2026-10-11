"""updater.py — ПРИМИТИВНАЯ проверка версии (ТЗ 2, 2.1).

Старая логика автообновления (скачивание zip, замена exe через скрипт) УДАЛЕНА:
она себя не оправдала. Теперь клиент только СРАВНИВАЕТ свою версию
с версией на сервере. Решение о выходе принимает main.py:
версия ниже серверной -> предупреждение + требование скачать новую
версию с сайта вручную и заменить файлы -> полный выход.

Оставлен только ручной перезапуск restart_app() (кнопка «⟳» в клиенте).
"""
import diagnostics as diag
from diagnostics import stage

import os
import subprocess
import sys

import requests

from config import SERVER_URL, CURRENT_VERSION


def _parse_version(v: str) -> list:
    """'1.10.0' -> [1, 10, 0] (для корректного сравнения версий)."""
    parts = []
    for chunk in str(v or "").strip().split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return parts or [0]


def _compare_versions(a: str, b: str) -> int:
    """Сравнение версий: 1 — a новее b, 0 — равны, -1 — a старее b."""
    pa, pb = _parse_version(a), _parse_version(b)
    for va, vb in zip(pa + [0] * 3, pb + [0] * 3):
        if va > vb:
            return 1
        if va < vb:
            return -1
    return 0


@stage
def check_for_update(token: str) -> dict | None:
    """Проверка версии на сервере. Возвращает info, если клиент УСТАРЕЛ."""
    try:
        res = requests.get(
            f"{SERVER_URL}/api/client/update",
            params={"version": CURRENT_VERSION},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        data = res.json()
        if data.get("success") and data.get("data", {}).get("hasUpdate"):
            info = data["data"]
            # Двойная страховка на клиенте: серверная версия не новее — ок.
            if _compare_versions(str(info.get("version", "")), CURRENT_VERSION) <= 0:
                return None
            return info
    except Exception:
        diag.exception("updater.py:61")
        pass
    return None


@stage
def restart_app():
    """Ручной перезапуск приложения (кнопка «⟳»). К обновлениям не относится.

    Консоль НЕ открывается ни в одном режиме: exe запускается напрямую,
    скрипт — через pythonw / CREATE_NO_WINDOW, фоллбэк — замена образа
    текущего процесса (os.execl), а не запуск голого интерпретатора.
    """
    # 1. Собранный exe (Nuitka / PyInstaller, 1 файл): запускаем его же.
    try:
        exe = ""
        if getattr(sys, "frozen", False):
            exe = sys.executable or ""
        else:
            argv0 = sys.argv[0] if sys.argv else ""
            if argv0 and argv0.lower().endswith(".exe"):
                exe = os.path.abspath(argv0)
        if exe and exe.lower().endswith(".exe") and os.path.isfile(exe):
            os.startfile(exe)  # type: ignore[attr-defined]
            return
    except Exception:
        diag.exception("updater.py:87")
        pass
    # 2. Запуск из исходников: тот же скрипт с теми же аргументами,
    # интерпретатор — оконный (без консоли).
    try:
        script = os.path.abspath(sys.argv[0]) if sys.argv else ""
        if script and os.path.isfile(script) and not script.lower().endswith(".exe"):
            interp = sys.executable or ""
            candidates = []
            if interp.lower().endswith("python.exe"):
                # pythonw.exe — оконный интерпретатор, консоли не создаёт.
                candidates.append(interp[: -len("python.exe")] + "pythonw.exe")
            candidates.append(interp)
            no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0  # SW_HIDE
            for py in candidates:
                if py and os.path.isfile(py):
                    subprocess.Popen(
                        [py, script, *sys.argv[1:]],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=no_window,
                        startupinfo=startupinfo,
                        close_fds=True,
                    )
                    return
    except Exception:
        diag.exception("updater.py:119")
        pass
    # 3. Фоллбэк: замена образа процесса (консоли не открывает).
    try:
        os.execl(sys.executable, sys.executable, *sys.argv)
    except Exception:
        diag.exception("updater.py:125")
        pass
