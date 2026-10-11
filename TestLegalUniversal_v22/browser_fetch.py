"""browser_fetch.py — скачивание закреплённого Chrome for Testing (v18).

Зачем
-----
ШТАТНЫЙ микрофон страницы (Web Speech API) живёт только в НАСТОЯЩЕМ
браузере с речевым сервисом Google. Если у пользователя не оказалось ни
Chrome, ни закреплённой сборки, приложение уходит во встроенный QtWebEngine,
а там запись голоса идёт через Windows System.Speech — и пользователь видит
«В Windows не установлен распознаватель речи» (на Windows 11 речевой пакет
не установлен по умолчанию).

Этот модуль устраняет причину, а не следствие: он скачивает официальный
**Chrome for Testing** — настоящий брендированный Chrome, который Google
публикует для автоматизации. Тогда:

* микрофон — штатный, самой страницы; никаких речевых пакетов Windows не
  нужно вообще;
* движок одинаковый у всех пользователей (одна версия, один путь рендера);
* речевой ввод —云端 Google, язык задаётся явно (по умолчанию ru-RU).

Почему не `tools/fetch_chrome.py`
--------------------------------
Тот скрипт остаётся ручным инструментом и в `--onefile`-сборку не попадает
(в exe включается только шрифт). Этот модуль импортируется `main.py`,
поэтому работает и из исходников, и из собранного exe.

Только стандартная библиотека: `urllib` + `zipfile`. Никаких новых
зависимостей.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

INDEX = ("https://googlechromelabs.github.io/chrome-for-testing/"
         "last-known-good-versions-with-downloads.json")

CHUNK = 1 << 18  # 256 КБ


class FetchError(RuntimeError):
    """Сеть/индекс недоступны: понятное сообщение вместо traceback."""


def fetch_json(url: str, timeout: float = 30.0) -> dict:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as response:
            return json.load(response)
    except Exception as exc:
        raise FetchError(
            "Не удалось получить список версий (%s).\n"
            "Проверьте интернет, прокси и антивирус." % exc) from exc


def pick_url(index: dict, channel: str = "Stable",
             platform: str = "win64"):
    """(version, url) последней Known-Good сборки канала."""
    entry = (index.get("channels") or {}).get(channel) or {}
    version = entry.get("version") or ""
    for item in (entry.get("downloads") or {}).get("chrome") or []:
        if item.get("platform") == platform:
            return version, item["url"]
    raise FetchError("В индексе нет сборки %s для %s" % (channel, platform))


def default_dest(app_dir=None) -> Path:
    """`<папка приложения>/browser` — именно там его ищет discover_browsers."""
    if app_dir:
        return Path(app_dir) / "browser"
    return Path(__file__).resolve().parent / "browser"


def download(url: str, target: Path, report=None, cancel=None) -> Path:
    """Скачать `url` в `target`. `report(доля, текст)`, `cancel()` — отмена."""
    report = report or (lambda *_a: None)
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "Legalyze/1.0.8"})
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        with open(target, "wb") as out:
            while True:
                if cancel is not None and cancel():
                    raise FetchError("Скачивание отменено")
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total:
                    report(min(0.98, done / float(total)),
                           "Скачано %.0f из %.0f МБ" % (done / 1048576.0,
                                                        total / 1048576.0))
                else:
                    report(0.5, "Скачано %.0f МБ" % (done / 1048576.0))
    return target


def extract(archive: Path, dest: Path, report=None) -> Path:
    """Распаковать архив так, чтобы `<dest>/chrome.exe` существовал."""
    report = report or (lambda *_a: None)
    dest.mkdir(parents=True, exist_ok=True)
    report(0.98, "Распаковка…")
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(dest)
    # Архив содержит папку chrome-win64 — раскладываем содержимое в `dest`.
    for child in list(dest.iterdir()):
        if child.is_dir() and (child / "chrome.exe").is_file():
            for inner in list(child.iterdir()):
                target = dest / inner.name
                if target.exists():
                    continue
                shutil.move(str(inner), str(target))
            try:
                child.rmdir()
            except Exception:
                pass
    exe = dest / "chrome.exe"
    if not exe.is_file():
        raise FetchError("Распаковано, но %s не найден" % exe)
    return exe


def install(dest, report=None, cancel=None, url=None, version=None,
            channel="Stable", platform="win64") -> Path:
    """Скачать и распаковать Chrome for Testing. Возвращает путь к chrome.exe."""
    report = report or (lambda *_a: None)
    dest = Path(dest)
    # Уже стоит — не качаем 170 МБ второй раз.
    if (dest / "chrome.exe").is_file():
        report(1.0, "Уже установлен: %s" % (dest / "chrome.exe"))
        return dest / "chrome.exe"
    if not url:
        report(0.02, "Получение списка версий…")
        index = fetch_json(INDEX)
        if version:
            url = None
            for entry in (index.get("versions") or []):
                if entry.get("version") == version:
                    for item in (entry.get("downloads") or {}).get("chrome") or []:
                        if item.get("platform") == platform:
                            url = item["url"]
                            break
                    break
            if not url:
                raise FetchError("Версия %s не найдена" % version)
        else:
            version, url = pick_url(index, channel, platform)
    report(0.05, "Chrome for Testing %s" % (version or ""))
    tmp = Path(tempfile.mkdtemp(prefix="legalyze-chrome-"))
    archive = tmp / "chrome.zip"
    try:
        download(url, archive, report=report, cancel=cancel)
        if cancel is not None and cancel():
            raise FetchError("Скачивание отменено")
        exe = extract(archive, dest, report=report)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    report(1.0, "Готово: %s" % exe)
    return exe


def already_installed(dest) -> bool:
    exe = Path(dest) / "chrome.exe"
    return exe.is_file()
