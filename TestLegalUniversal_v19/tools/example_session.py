#!/usr/bin/env python3
"""example_session.py — искусственная сессия для проверки разбора логов.

Зачем
-----
Разбор (`tools/analyze_browser_logs.py`) должен уметь читать сессию, которой
ещё нет: пока пользователь не прислал свой `%APPDATA%\\Legalyze\\logs\\<сессия>`.
Этот скрипт создаёт ТАКУЮ ЖЕ папку, но с выдуманными событиями, и позволяет
проверить, что сформированный отчёт читается и отвечает на два вопроса:
«почему окно не на позиции» и «экспортировались ли файлы».

Важно: данные выдуманные. Они годятся только для проверки разбора и как
образец формата — не как «пример с чьей-то машины».

Запуск
------
    python3 tools/example_session.py                  # в browser_logs/inbox/…
    python3 tools/example_session.py --out /tmp/demo  # в конкретную папку
    python3 tools/example_session.py --tag 2026-10-04-пример

После этого разбор делается как обычно:
    python3 tools/analyze_browser_logs.py
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # TestLegalUniversal_v19
REPO = ROOT.parent                                  # корень репозитория
sys.path.insert(0, str(ROOT))

import browser_trace as btrace  # noqa: E402  (путь добавлен выше)

#: Целевой прямоугольник «как просило приложение» и уходы от него по времени.
TARGET = [1400, 200, 439, 728]


class ScriptedProbe:
    """Выдаёт заранее расписанную хронологию: съехало → вернулось (v20)."""

    def __init__(self):
        # (мс от начала шкалы, dy, причины, масштаб DPI)
        self.script = [
            (0, -24, ["async_pending"], 1.0),
            (60, -48, ["dpi_scale", "off_target"], 1.5),
            (300, -24, ["off_target"], 1.5),
            (1900, 0, [], 1.5),
        ]
        self.calls = 0
        self.clock = 0.0
        self.clock_at = None

    def __call__(self, hwnd, parent=0, inset=(0, 0, 0, 0), **kwargs):
        if self.clock_at is None:
            self.clock_at = time.monotonic()
        self.clock = (time.monotonic() - self.clock_at) * 1000.0
        step = self.script[-1]
        for item in self.script:
            if self.clock >= item[0]:
                step = item
        self.calls += 1
        _, dy, reasons, scale = step
        rect = [TARGET[0], TARGET[1] + dy, TARGET[2], TARGET[3]]
        return {
            "supported": True, "hwnd": int(hwnd), "parent": int(parent),
            "class": "Chrome_WidgetWin_1", "verdict": "off_target" if reasons else "on_target",
            "reasons": list(reasons), "rect": rect, "target": list(TARGET),
            "dx": 0, "dy": dy, "dw": 0, "dh": 0,
            "client": [439, 728], "parent_actual": int(parent),
            "visible": True, "minimized": False, "child_style": True,
            "popup_style": False, "visible_style": True,
            "dpi": int(96 * scale), "scale": scale, "pid": 4242, "thread": 1111,
        }

    def tick(self, ms=20.0):
        self.clock += ms
        return self.clock


class FakePage:
    """Страница, у которой есть только то, что спрашивает `browser_trace`."""

    #: Метрики раскладки можно менять по ходу сценария: так пример показывает
    #: «двойной зум» (658×0.667 → 987×0.445) — сложение объявления CDP с
    #: `deviceScaleFactor` и зума профиля, то есть ровно то, что видно
    #: пользователю как «объекты съехали».
    layout = {"iw": 658, "ih": 1091, "dpr": 0.667}

    class _Value:
        def __init__(self, value):
            self._value = value

        def get(self, key, default=None):
            return self._value if key == "value" else default

    def eval(self, expression, timeout=None, **kwargs):  # noqa: A003
        text = str(expression)
        if "visualViewport" in text:
            value = {"iw": self.layout["iw"], "ih": self.layout["ih"],
                     "dpr": self.layout["dpr"], "sx": 0, "sy": 0,
                     "vw": self.layout["iw"], "vh": self.layout["ih"], "vs": 1.0,
                     "ox": 0, "oy": 0, "cw": 612, "ch": 44, "cx": 23, "cy": 980,
                     "ready": "complete", "active": "textarea"}
        elif "chip_names" in text:
            value = {"file_inputs": 1, "inputs_with_files": 1,
                     "picked": [{"count": 1, "accept": "",
                                 "names": ["Промт.pdf"], "sizes": [18008]}],
                     "chips": 2, "chip_names": ["Промт.pdf", "Шаблон.txt"],
                     "progress": 0, "alerts": [], "ready": "complete",
                     "focused": True, "active": "textarea"}
        elif "outline" in text:
            value = {"outline": [], "composer": {"tag": "textarea", "box": [23, 980, 612, 44]},
                     "file_inputs": 1, "chips": 2, "nodes": 0, "has_file_input": True}
        elif "scripts_total" in text:
            value = {"host": "gemini.google.com", "path": "/app", "nodes": 12,
                     "scripts_total": 4, "frames_total": 0, "csp": 1}
        elif "KEEP_SRC" in text:
            value = {"html": '<html><body><rich-textarea class="input">'
                             '<textarea class="ql-editor"></textarea></rich-textarea>'
                             '</body></html>',
                     "nodes": 4, "truncated": False}
        else:
            value = {}
        return self._Value(value)

    def send(self, *args, **kwargs):
        return {}


def diag_rows(session_dir: Path):
    """Маленький `diagnostic.jsonl` — ровно те события, что читает разбор."""
    now = datetime.now(timezone.utc)

    def row(offset_ms, event, **fields):
        return {"time": (now + timedelta(milliseconds=offset_ms)).isoformat(),
                "pid": 4242, "thread": "MainThread", "event": event, **fields}

    rows = [
        row(0, "session.start", platform="Windows-10-10.0.26100-SP0",
            python="3.11.9 (tags/v3.11.9)", bitness=64, executable="Legalyze.exe",
            cwd="C:\\Legalyze", frozen=True, nuitka_compiled=True,
            release_revision="universal-embedded-v19",
            packages={"PyQt6": "6.7.1", "PyQt6-Qt6": "6.7.2"},
            windows_build="10.0.26100", modes={"LEGALYZE_DISABLE_GPU": None}),
        row(120, "qt.start", qt="6.7.2", pyqt="6.7.1", platform="windows", style="windowsvista"),
        row(140, "qt.screen_count", screens=1, primary="\\\\.\\DISPLAY1"),
        row(150, "qt.screen", geometry=[0, 0, 2560, 1440],
            available=[0, 0, 2560, 1392], dpi=96, dpr=1.5),
        row(400, "browser.discovered", name="Chrome", kind="chrome",
            rank=0, path="C:\\Legalyze\\browser\\chrome.exe"),
        row(420, "browser.try", name="Legalyze Chromium", kind="chrome",
            attempt=1, gpu=False),
        row(900, "browser.devtools", browser="Chrome/126.0.6478.127", port=9222),
        row(1500, "browser.embedded", hwnd=853124, parent=723908, port=9222,
            kind="chrome", inset=[-10, -24, 10, 24]),
        row(1600, "browser.mode", engine="external_chrome", exe="chrome.exe",
            port=9222, inset=[-10, -24, 10, 24], dx=-10, dy=-24, zoom=0.667,
            light_theme=True, lang="ru-RU"),
        row(2600, "page.surface", verdict="gemini", ready="complete", composer=1,
            files=1, chips=0, host="gemini.google.com", path="/app"),
        row(3000, "page.surface_after_show", verdict="gemini", ready="complete",
            composer=1, files=1, chips=0),
        row(3400, "warn.browser", reason="upload blocked on this surface"),
        row(3500, "exception", where="main.upload", type="RuntimeError",
            message="surface without file input"),
        row(9000, "page.surface_changed", verdict="gemini", previous="ai_mode",
            when="after_show"),
        row(9800, "browser.reveal_gate", ready=True),
        row(10000, "browser.revealed", stable=True, ms=840),
        row(10400, "session.end", reason="user"),
    ]
    with (session_dir / "diagnostic.jsonl").open("w", encoding="utf-8") as stream:
        for item in rows:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")


def build_session(session_dir: Path) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    diag_rows(session_dir)

    btrace.finish()
    btrace.install(log_dir=session_dir)
    page = FakePage()
    probe = ScriptedProbe()
    real_probe = btrace.window_probe
    btrace.window_probe = probe                      # noqa: SLF001 — так и задумано
    try:
        btrace.mark("trace.installed", dir=str(session_dir), version=btrace.TRACE_VERSION)
        btrace.mark("hotkey.pressed", id=1, visible=True)
        btrace.mark("window.hide")

        # 1. Скрытие: всё уже стоит на цели.
        def hide_probe(hwnd, parent=0, inset=(0, 0, 0, 0), **kwargs):
            data = real_probe(hwnd, parent, inset)
            data.update({"verdict": "on_target", "dx": 0, "dy": 0, "dw": 0, "dh": 0,
                         "reasons": [], "rect": list(TARGET), "target": list(TARGET)})
            return data

        btrace.window_probe = hide_probe             # noqa: SLF001
        hide_watch = btrace.watch("hide", 853124, 723908, (-10, -24, 10, 24),
                                  page_provider=lambda: page, duration=0.4,
                                  fast_ms=20, slow_ms=40, fast_until=0.2,
                                  stable_needed=2, min_ms=0)
        hide_watch.join(5)

        # 2. Показ: хоткей «Окно» — окно уезжает и возвращается.
        btrace.window_probe = probe                  # noqa: SLF001
        btrace.mark("window.show", was_hidden=True)
        btrace.mark("revive.schedule", step_ms=[0, 150, 350, 600, 900, 1300, 1800, 2400],
                    min_ms=1600, settle_n=3)
        btrace.record_win_call("sync_now", True, hwnd=853124, parent=723908,
                               inset=(-10, -24, 10, 24), seconds=0.004,
                               extra={"stage": "finalize", "sync": True})
        btrace.record_win_call("show_window", True, hwnd=853124, seconds=0.001,
                               extra={"was_visible": False, "visible": True})
        btrace.record_win_call("invalidate", True, hwnd=853124, seconds=0.001)
        for step, delay in ((0, 0), (1, 150), (2, 350), (3, 600), (4, 900), (5, 1300),
                            (6, 1800), (7, 2400)):
            probe.clock = float(delay)
            probe.clock_at = None                     # замер «на этом шаге»
            measure = btrace.window_probe(853124, 723908, (-10, -24, 10, 24))
            btrace.mark("revive.step", step=step, ms=delay,
                        verdict=measure.get("verdict"), dy=measure.get("dy"))
        probe.clock = 0.0
        def break_layout(after_ms):
            """Вернуть раскладку в «двойной зум» после N мс шкалы (как Chrome)."""
            deadline = time.monotonic() + after_ms / 1000.0
            while time.monotonic() < deadline:
                time.sleep(0.02)
            page.layout = {"iw": 987, "ih": 1636, "dpr": 0.445}
            time.sleep(1.0)
            page.layout = {"iw": 658, "ih": 1091, "dpr": 0.667}
            btrace.mark("layout.restored", reason="zoom-guard")

        threading.Thread(target=break_layout, args=(1900,), daemon=True).start()
        show_watch = btrace.watch("show", 853124, 723908, (-10, -24, 10, 24),
                                  page_provider=lambda: page, duration=3.2,
                                  fast_ms=20, slow_ms=100, fast_until=2.4,
                                  stable_needed=2, min_ms=0)
        show_watch.join(10)
        btrace.mark("revive.reveal", verdict="on_target", dy=0)

        # 3. Экспорт: сначала поверхность без input[type=file], потом удача.
        tmp = Path(tempfile.mkdtemp(prefix="legalyze-example-"))
        pdf = tmp / "Промт.pdf"
        txt = tmp / "Шаблон.txt"
        pdf.write_bytes(b"%PDF-1.4" + b"x" * 18000)
        txt.write_text("Шаблон промта", encoding="utf-8")

        class NoInputPage(FakePage):
            def eval(self, expression, timeout=None, **kwargs):  # noqa: A003
                if "chip_names" in str(expression):
                    return self._Value({"file_inputs": 0, "inputs_with_files": 0,
                                        "picked": [], "chips": 0, "chip_names": [],
                                        "progress": 0, "alerts": [], "ready": "complete"})
                return super().eval(expression, timeout=timeout, **kwargs)

        btrace.audit_export(NoInputPage(), "upload_blocked-gemini", [pdf, txt],
                            extra={"surface": "ai_mode"})
        btrace.audit_export(page, "complete", [pdf, txt],
                            extra={"present": 2, "names": ["Промт.pdf", "Шаблон.txt"]})
        btrace.capture_page(page, "chat-ready", full=True)
        btrace.mark("chat.ready", verdict="gemini", blocked=False)
        shutil.rmtree(tmp, ignore_errors=True)
    finally:
        btrace.window_probe = real_probe             # noqa: SLF001
        btrace.finish()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Искусственная сессия для разбора логов")
    parser.add_argument("--tag", default=None, help="имя папки сессии")
    parser.add_argument("--out", default=None, help="куда положить сессию")
    args = parser.parse_args(argv)
    tag = args.tag or ("example-session-" + datetime.now().strftime("%Y-%m-%d"))
    out = Path(args.out) if args.out else REPO / "browser_logs" / "inbox" / tag
    shutil.rmtree(out, ignore_errors=True)
    build_session(out)
    # Имена артефактов «кода страницы» содержат время — для примера (он лежит
    # в репозитории) время убирается, иначе каждый пересбор менял бы имена.
    if out.name.startswith("example-session"):
        for path in sorted(out.glob("page-*-chat-ready-*.*")):
            stem, suffix = path.name.split("-chat-ready-", 1)
            fixed = out / ("%s-chat-ready.%s" % (stem, suffix.rsplit(".", 1)[-1]))
            if fixed.exists():
                fixed.unlink()
            path.rename(fixed)
    print("сессия: %s" % out)
    print("разбор : cd %s && python3 tools/analyze_browser_logs.py" % ROOT.name)
    for name in sorted(p.name for p in out.iterdir()):
        print("   ", name, (out / name).stat().st_size, "Б")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
