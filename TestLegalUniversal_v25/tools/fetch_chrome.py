"""Download a pinned Chrome for Testing build into `<app>/browser/`.

Why this exists
---------------
Every user gets the SAME engine, which removes most of the "works on my
machine" class of failures: one Chromium version, one rendering path, one
speech stack. The application prefers this portable build over any installed
browser (`native_browser.discover_browsers`), and falls back to the installed
Google Chrome / Edge automatically.

Chrome for Testing is Google's official, branded Chrome build published for
automation — it is a real Chrome (not a stripped Chromium), which is exactly
what the page's own microphone needs.

Usage
-----
    py tools/fetch_chrome.py                # last known good stable, win64
    py tools/fetch_chrome.py --version 140.0.7339.207
    py tools/fetch_chrome.py --url https://.../chrome-win64.zip
    py tools/fetch_chrome.py --dest C:\\Legalyze\\browser

Stdlib only (urllib + zipfile): no extra dependency is added to the app.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

INDEX = ("https://googlechromelabs.github.io/chrome-for-testing/"
         "last-known-good-versions-with-downloads.json")


def default_dest() -> Path:
    here = Path(__file__).resolve().parent
    return here.parent / "browser"


class FetchError(RuntimeError):
    """Сеть/индекс недоступны: печатаем понятное сообщение, а не traceback."""


def fetch_json(url: str, timeout: float = 30.0) -> dict:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as response:
            return json.load(response)
    except Exception as exc:
        raise FetchError("Не удалось получить список версий (%s).\n"
                         "Проверьте интернет/прокси/антивирус или укажите прямую "
                         "ссылку: --url https://.../chrome-win64.zip" % exc) from exc


def pick_url(index: dict, channel: str = "Stable", platform: str = "win64") -> tuple[str, str]:
    channels = index.get("channels") or {}
    entry = channels.get(channel) or {}
    version = entry.get("version") or ""
    for item in (entry.get("downloads") or {}).get("chrome") or []:
        if item.get("platform") == platform:
            return version, item["url"]
    raise SystemExit("No %s chrome build for %s in the index" % (channel, platform))


def download(url: str, target: Path, report=print) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    report("Downloading %s" % url)
    try:
        with opener.open(url, timeout=120) as response, open(target, "wb") as handle:
            shutil.copyfileobj(response, handle, length=1024 * 256)
    except Exception as exc:
        raise FetchError("Загрузка прервана (%s).\nПопробуйте ещё раз или "
                         "скачайте архив вручную и укажите --url." % exc) from exc
    size = target.stat().st_size
    if size < 5 * 1024 * 1024:
        raise FetchError("Архив подозрительно маленький (%.1f MB) — похоже, это "
                         "страница ошибки, а не Chrome." % (size / 1048576.0))
    report("Saved %s (%.1f MB)" % (target, size / 1048576.0))


def extract(archive: Path, dest: Path, report=print) -> Path:
    with zipfile.ZipFile(archive) as zf:
        members = [n for n in zf.namelist() if n.endswith("chrome.exe")]
        if not members:
            raise SystemExit("chrome.exe not found inside the archive")
        # Keep the archive's own folder layout (chrome-win64/...).
        root = members[0].split("/")[0] if "/" in members[0] else ""
        report("Unpacking into %s" % dest)
        zf.extractall(dest)
    if root:
        inner = dest / root
        if inner.is_dir():
            # Flatten: the app looks for <dest>/chrome.exe
            for child in inner.iterdir():
                target = dest / child.name
                if target.exists():
                    if target.is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        target.unlink(missing_ok=True)
                shutil.move(str(child), str(dest))
            shutil.rmtree(inner, ignore_errors=True)
    exe = dest / "chrome.exe"
    if not exe.is_file():
        raise SystemExit("Unpacked, but %s is missing" % exe)
    return exe


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="exact Chrome for Testing version")
    parser.add_argument("--channel", default="Stable",
                        choices=["Stable", "Beta", "Dev", "Canary"])
    parser.add_argument("--url", help="direct chrome-win64.zip URL (skips the index)")
    parser.add_argument("--dest", help="destination folder (default: <app>/browser)")
    parser.add_argument("--platform", default="win64")
    parser.add_argument("--dry-run", action="store_true",
                        help="только показать, что будет скачано (без загрузки)")
    args = parser.parse_args(argv)

    dest = Path(args.dest) if args.dest else default_dest()
    url = args.url
    version = args.version or ""
    if not url:
        index = fetch_json(INDEX)
        if args.version:
            wanted = args.version
            url = None
            for entry in (index.get("versions") or []):
                if entry.get("version") == wanted:
                    for item in (entry.get("downloads") or {}).get("chrome") or []:
                        if item.get("platform") == args.platform:
                            url = item["url"]
                            break
                    break
            if not url:
                raise SystemExit("Version %s not found for %s" % (wanted, args.platform))
        else:
            version, url = pick_url(index, args.channel, args.platform)

    print("Chrome for Testing %s -> %s" % (version or "(pinned url)", dest))
    if args.dry_run:
        print("URL: %s" % url)
        print("Проверка без загрузки завершена.")
        return 0
    tmp = Path(tempfile.mkdtemp(prefix="legalyze-chrome-"))
    archive = tmp / "chrome.zip"
    try:
        download(url, archive)
        exe = extract(archive, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    size = exe.stat().st_size / 1048576.0
    print("OK: %s (%.1f MB)" % (exe, size))
    print("The application will pick it up automatically on the next start.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FetchError as error:
        print("ОШИБКА: %s" % error, file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("Отменено.", file=sys.stderr)
        sys.exit(130)
