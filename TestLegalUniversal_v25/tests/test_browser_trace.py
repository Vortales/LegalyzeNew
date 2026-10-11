"""Тесты приборной панели браузера: сбор, шкалы, экспорт и разбор логов.

Смысл этих тестов — гарантировать, что сбор НЕ ломает приложение и что по
записанному логу действительно можно ответить на вопросы:

* встало ли окно на указанную позицию и почему нет (`window_probe`, шкалы);
* экспортировались ли файлы (`audit_export` и его вердикты);
* читается ли всё это анализатором (`tools/analyze_browser_logs.py`).
"""
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import browser_trace as btrace  # noqa: E402


class FakeValue:
    """Ответ CDP на один вызов: `{"value": ...}`."""

    def __init__(self, value):
        self.value = value

    def get(self, key, default=None):
        return self.value if key == "value" else default


class FakePage:
    """Страница, отвечающая на конкретные скрипты прибора.

    Строки скриптов различаются устойчивыми признаками, поэтому фейк не
    зависит от порядка вызовов: так тест не разваливается от правки текста
    запросов в `browser_trace`.
    """

    def __init__(self, chips=0, chip_names=None, file_inputs=1, inputs_with_files=0,
                 inner=(658, 1091), dpr=1.0):
        self.chips = chips
        self.chip_names = chip_names or []
        self.file_inputs = file_inputs
        self.inputs_with_files = inputs_with_files
        self.inner = inner
        self.dpr = dpr
        self.calls = []

    def eval(self, expr, timeout=None, **kwargs):  # noqa: A003 - как в CDP
        self.calls.append(str(expr)[:60])
        text = str(expr)
        if "visualViewport" in text:
            return FakeValue({"iw": self.inner[0], "ih": self.inner[1], "dpr": self.dpr,
                              "sx": 0, "sy": 0, "vw": self.inner[0], "vh": self.inner[1],
                              "vs": 1.0, "ox": 0, "oy": 0, "cw": 640, "ch": 40,
                              "cx": 10, "cy": 900, "ready": "complete", "active": "textarea"})
        if "chip_names" in text:
            return FakeValue({"file_inputs": self.file_inputs, "inputs_with_files": self.inputs_with_files,
                              "picked": [], "chips": self.chips, "chip_names": self.chip_names,
                              "progress": 0, "alerts": [], "ready": "complete",
                              "focused": True, "active": "textarea"})
        if "outline" in text or "getBoundingClientRect" in text and "composer" in text:
            return FakeValue({"outline": [], "composer": {"tag": "textarea", "box": [1, 2, 3, 4]},
                              "file_inputs": self.file_inputs, "chips": self.chips,
                              "nodes": 0, "has_file_input": True})
        if "scripts_total" in text:
            return FakeValue({"host": "gemini.google.com", "path": "/app", "nodes": 10,
                              "scripts_total": 3, "frames_total": 0, "csp": 0})
        if "document.documentElement" in text and "KEEP_SRC" in text:
            return FakeValue({"html": "<html><body><textarea/></body></html>", "nodes": 3,
                              "truncated": False})
        return FakeValue({})

    def send(self, *args, **kwargs):  # noqa: D401 - не используется прибором
        return {}


class TraceSessionMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="btrace-")
        self.dir = Path(self._tmp.name)
        btrace.finish()
        self.assertTrue(btrace.install(log_dir=self.dir))

    def tearDown(self):
        btrace.finish()
        self._tmp.cleanup()

    def rows(self, name):
        return btrace._read_jsonl(self.dir / name)


class BasicCollectionTests(TraceSessionMixin):
    def test_mark_and_state_land_in_the_journal(self):
        btrace.mark("hotkey.pressed", id=1, visible=True)
        btrace.state("browser.mode", "external_chrome")
        btrace.finish()
        kinds = {row.get("kind") for row in self.rows("browser-trace.jsonl")}
        self.assertIn("mark.hotkey.pressed", kinds)
        self.assertIn("mark.trace.installed", kinds)
        summary = json.loads((self.dir / "browser-trace-summary.json").read_text())
        self.assertEqual(summary["counters"].get("state.set"), 1)

    def test_win_call_reports_off_target_with_reason(self):
        # Прямо подменяем замер: окно «не встало» — на 24 px выше цели.
        fake = {"supported": True, "hwnd": 1, "parent": 2, "verdict": "off_target",
                "reasons": ["off_target", "dpi_scale"], "rect": [0, 0, 439, 728],
                "target": [0, 24, 439, 728], "dx": 0, "dy": -24, "dw": 0, "dh": 0,
                "client": [439, 728], "scale": 1.25, "dpi": 120,
                "explain": ["Позиция/размер окна не совпали с целью"]}
        with patch.object(btrace, "window_probe", return_value=fake):
            probe = btrace.record_win_call("sync_now", True, hwnd=1, parent=2,
                                           inset=(-10, -24, 10, 24))
        self.assertEqual(probe["verdict"], "off_target")
        btrace.finish()
        rows = [r for r in self.rows("browser-trace.jsonl") if r.get("kind") == "win32.call"]
        self.assertTrue(rows, "вызов с расхождением обязан попасть в журнал")
        self.assertEqual(rows[-1]["dy"], -24)
        self.assertIn("dpi_scale", rows[-1]["reasons"])
        summary = json.loads((self.dir / "browser-trace-summary.json").read_text())
        self.assertTrue(summary["problems"], "расхождение должно попасть в «проблемы»")


class TimelineTests(TraceSessionMixin):
    def test_timeline_records_deviation_and_settle(self):
        # Скрипт замера: сначала окно «съехало», потом встало — ровно то, что
        # видит пользователь после хоткея «Окно».
        sequence = [
            {"verdict": "off_target", "reasons": ["async_pending"], "rect": [0, 0, 439, 728],
             "target": [0, 24, 439, 728], "dx": 0, "dy": -24, "dw": 0, "dh": 0,
             "client": [439, 728], "visible": True, "scale": 1.0},
        ] * 3 + [
            {"verdict": "on_target", "reasons": ["ok"], "rect": [0, 24, 439, 728],
             "target": [0, 24, 439, 728], "dx": 0, "dy": 0, "dw": 0, "dh": 0,
             "client": [439, 728], "visible": True, "scale": 1.0},
        ] * 60
        index = {"i": 0}

        def scripted(hwnd, parent=0, inset=(0, 0, 0, 0)):
            item = dict(sequence[min(index["i"], len(sequence) - 1)])
            index["i"] += 1
            item.update({"supported": True, "hwnd": hwnd, "parent": parent})
            return item

        page = FakePage()
        with patch.object(btrace, "window_probe", side_effect=scripted):
            watcher = btrace.watch("show", 1234, 5678, (0, 0, 0, 0),
                                   page_provider=lambda: page, duration=3.0,
                                   fast_ms=10, slow_ms=20, fast_until=0.5,
                                   stable_needed=2, min_ms=0)
            self.assertIsNotNone(watcher)
            watcher.join(5.0)
        btrace.finish()
        rows = self.rows("browser-window-timeline.jsonl")
        verdicts = [r.get("verdict") for r in rows]
        self.assertIn("off_target", verdicts)
        self.assertIn("on_target", verdicts)
        summary = json.loads((self.dir / "browser-trace-summary.json").read_text())
        watch = (summary.get("watchers") or {}).get("show") or {}
        self.assertGreaterEqual(int(watch.get("deviations") or 0), 1)
        self.assertIsNotNone(watch.get("final_verdict"))
        # Итог для человека тоже сформирован.
        understanding = (self.dir / "UNDERSTANDING.md").read_text(encoding="utf-8")
        self.assertIn("Позиция окна", understanding)


class TimelineCounterTests(TraceSessionMixin):
    """Правила, без которых шкала и её итоги читаются неверно (см. ОШИБКИ.md № 7)."""

    def _run_off_target_watch(self, seconds=2.2):
        fake = {"supported": True, "verdict": "off_target", "reasons": ["off_target"],
                "rect": [0, 0, 439, 728], "target": [0, 24, 439, 728],
                "dx": 0, "dy": -24, "dw": 0, "dh": 0, "client": [439, 728],
                "visible": True, "scale": 1.0}
        with patch.object(btrace, "window_probe", return_value=dict(fake)):
            watcher = btrace.watch("show", 1234, 5678, (0, 0, 0, 0),
                                   page_provider=lambda: FakePage(), duration=seconds,
                                   fast_ms=20, slow_ms=50, fast_until=seconds,
                                   stable_needed=2, min_ms=0)
            self.assertIsNotNone(watcher)
            watcher.join(seconds + 5.0)

    def test_pulse_is_rare_and_deviations_count_rows_not_samples(self):
        self._run_off_target_watch()
        btrace.finish()
        rows = self.rows("browser-window-timeline.jsonl")
        pulses = [r for r in rows if r.get("kind") == "watch.pulse"]
        off_rows = [r for r in rows if r.get("verdict") == "off_target"]
        # За 2.2 с пульсов должно быть 1–2, а не ~100 (иначе шкалу не читать).
        self.assertLessEqual(len(pulses), 3)
        summary = json.loads((self.dir / "browser-trace-summary.json").read_text())
        watch = (summary.get("watchers") or {}).get("show") or {}
        self.assertEqual(int(watch.get("deviations") or 0), len(off_rows))
        self.assertEqual(int(watch.get("off_episodes") or 0), 1)

    def test_watch_end_closes_the_timeline_and_carries_settle(self):
        sequence = [{"verdict": "off_target", "reasons": ["async_pending"],
                     "rect": [0, 0, 439, 728], "target": [0, 24, 439, 728],
                     "dx": 0, "dy": -24, "dw": 0, "dh": 0, "client": [439, 728],
                     "visible": True, "scale": 1.0}] * 2 + [
                    {"verdict": "on_target", "reasons": [], "rect": [0, 24, 439, 728],
                     "target": [0, 24, 439, 728], "dx": 0, "dy": 0, "dw": 0, "dh": 0,
                     "client": [439, 728], "visible": True, "scale": 1.0}] * 60
        index = {"i": 0}

        def scripted(hwnd, parent=0, inset=(0, 0, 0, 0)):
            item = dict(sequence[min(index["i"], len(sequence) - 1)])
            index["i"] += 1
            item.update({"supported": True, "hwnd": hwnd, "parent": parent})
            return item

        with patch.object(btrace, "window_probe", side_effect=scripted):
            watcher = btrace.watch("show", 1234, 5678, (0, 0, 0, 0),
                                   page_provider=lambda: FakePage(), duration=3.0,
                                   fast_ms=20, slow_ms=50, fast_until=0.4,
                                   stable_needed=2, min_ms=0)
            watcher.join(6.0)
        btrace.finish()
        rows = self.rows("browser-window-timeline.jsonl")
        ends = [r for r in rows if r.get("kind") == "watch.end"]
        self.assertTrue(ends, "итог шкалы обязан попасть в саму шкалу")
        self.assertEqual(ends[-1].get("tag"), "show")
        self.assertIsNotNone(ends[-1].get("settle_ms"))


class ExportAuditTests(TraceSessionMixin):
    def test_export_is_proved_with_names_and_disk_facts(self):
        payload = self.dir / "Тест.pdf"
        payload.write_bytes(b"%PDF-1.4 test payload")
        page = FakePage(chips=2, chip_names=["Тест.pdf", "Шаблон.txt"], file_inputs=1,
                        inputs_with_files=0)
        # Файл прикреплён (чипы есть) — вердикт положительный, даже если одного
        # из файлов нет на диске: доказательство вложения важнее.
        record = btrace.audit_export(page, "complete", [payload, self.dir / "missing.txt"])
        self.assertEqual(record["verdict"], "confirmed_attached")
        # А вот когда в чате НИЧЕГО не прикрепилось и файла нет на диске —
        # это отдельный вердикт «экспортировать было нечего».
        record = btrace.audit_export(
            FakePage(chips=0, chip_names=[], file_inputs=1), "inject_failed",
            [self.dir / "missing.txt"])
        self.assertEqual(record["verdict"], "file_missing_on_disk")
        record = btrace.audit_export(page, "final", [payload])
        self.assertEqual(record["verdict"], "confirmed_attached")
        self.assertEqual(record["chip_names"][0], "Тест.pdf")
        self.assertTrue(record["files"][0]["sha256_12"])
        # Поверхность без поля вложения — отдельный вердикт, а не «не сработало».
        bare = btrace.audit_export(FakePage(file_inputs=0, chips=0), "blocked", [payload])
        self.assertEqual(bare["verdict"], "surface_without_file_input")
        btrace.finish()
        summary = json.loads((self.dir / "browser-trace-summary.json").read_text())
        counters = summary["counters"]
        self.assertEqual(counters.get("export.verdict.confirmed_attached"), 2)
        self.assertEqual(counters.get("export.verdict.file_missing_on_disk"), 1)


class PageCaptureTests(TraceSessionMixin):
    def test_page_code_is_saved_without_text(self):
        page = FakePage(chips=1, chip_names=["Шаблон.txt"])
        result = btrace.capture_page(page, "chat-ready", full=True)
        self.assertIsNotNone(result.get("code"))
        files = list(self.dir.glob("page-code-*.html"))
        self.assertTrue(files, "«код страницы» обязан сохраняться")
        text = files[0].read_text(encoding="utf-8")
        self.assertIn("<textarea", text)
        self.assertIn("Санитизированный", text.replace("санитизированный", "Санитизированный"))
        self.assertTrue(list(self.dir.glob("page-outline-*.json")))


class FieldCollisionTests(TraceSessionMixin):
    def test_extra_cannot_break_win_call_recording(self):
        """`extra` с именем замеряемого поля раньше ронял запись (TypeError)."""
        fake = {"supported": True, "verdict": "on_target", "reasons": [], "rect": [0, 0, 1, 1],
                "target": [0, 0, 1, 1], "dx": 0, "dy": 0, "dw": 0, "dh": 0,
                "client": [1, 1], "visible": True, "scale": 1.0}
        with patch.object(btrace, "window_probe", return_value=dict(fake)):
            btrace.record_win_call("show_window", True, hwnd=1, parent=2,
                                   extra={"visible": False, "stage": "finalize"})
        btrace.finish()
        rows = [r for r in self.rows("browser-trace.jsonl") if r.get("kind") == "win32.call"]
        self.assertTrue(rows)
        self.assertEqual(rows[-1].get("stage"), "finalize")


class ExportVerdictTests(TraceSessionMixin):
    """Вердикт обязан говорить правду и когда файл прикреплён, и когда нет."""

    def test_chips_prove_attachment_even_without_file_input(self):
        # Gemini убирает input[type=file] после загрузки: чипы — единственный
        # признак, и он главнее. До правки вердикт был «вложение невозможно»
        # (171 раз подряд при реально прикреплённых файлах).
        payload = self.dir / "Memphis.pdf"
        payload.write_bytes(b"%PDF-1.4 payload")
        page = FakePage(chips=2, chip_names=["Memphis.pdf", "Шаблон.txt"],
                        file_inputs=0, inputs_with_files=0)
        record = btrace.audit_export(page, "complete", [payload])
        self.assertEqual(record["verdict"], "confirmed_attached")

    def test_extra_field_named_kind_cannot_break_the_record(self):
        # На живой сессии `extra={"kind": "pdf"}` роняла запись целиком:
        # TypeError в _record(). Теперь ключ сохраняется как arg_kind.
        page = FakePage(chips=0, chip_names=[], file_inputs=1)
        btrace.audit_export(page, "after_inject", None,
                            extra={"attach": "pdf", "kind": "pdf", "bytes": 10})
        btrace.finish()
        rows = [r for r in self.rows("browser-trace.jsonl") if r.get("kind") == "export.audit"]
        self.assertTrue(rows, "запись обязана состояться даже при конфликте имён")
        self.assertEqual(rows[-1].get("attach"), "pdf")
        self.assertEqual(rows[-1].get("arg_kind"), "pdf")

    def test_identical_audits_are_collapsed(self):
        page = FakePage(chips=0, chip_names=[], file_inputs=0)
        for _ in range(6):
            btrace.audit_export(page, "complete", None)
        btrace.finish()
        rows = [r for r in self.rows("browser-trace.jsonl") if r.get("kind") == "export.audit"]
        self.assertEqual(len(rows), 1, "одинаковые проверки не должны забивать журнал")
        summary = json.loads((self.dir / "browser-trace-summary.json").read_text())
        self.assertGreaterEqual(summary["counters"].get("export.audit_repeat", 0), 5)


class SafetyTests(unittest.TestCase):
    def test_probe_without_windows_never_raises(self):
        probe = btrace.window_probe(0, 0, (0, 0, 0, 0))
        self.assertIn("verdict", probe)
        probe = btrace.window_probe(123456, 654321, (-10, -24, 10, 24))
        self.assertIn("verdict", probe)

    def test_record_win_call_without_install_is_a_noop(self):
        btrace.finish()
        self.assertIsNone(btrace.record_win_call("sync", True, hwnd=1, parent=2))
        self.assertEqual(btrace.active_watchers(), 0)


class AnalyzerTests(TraceSessionMixin):
    def _module(self):
        spec = importlib.util.spec_from_file_location(
            "analyze_browser_logs", ROOT / "tools" / "analyze_browser_logs.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_analyzer_turns_a_session_into_a_readable_report(self):
        page = FakePage(chips=1, chip_names=["Промт.pdf"])
        btrace.mark("hotkey.pressed", id=1)
        payload = self.dir / "Промт.pdf"
        payload.write_bytes(b"%PDF-1.4 data")
        btrace.audit_export(page, "final", [payload])
        sequence = [
            {"verdict": "off_target", "reasons": ["off_target"], "dx": 0, "dy": -24,
             "rect": [0, 0, 439, 728], "target": [0, 24, 439, 728], "dw": 0, "dh": 0,
             "client": [439, 728], "visible": True, "scale": 1.0},
        ] * 2 + [{"verdict": "on_target", "reasons": ["ok"], "dx": 0, "dy": 0, "dw": 0,
                  "dh": 0, "rect": [0, 24, 439, 728], "target": [0, 24, 439, 728],
                  "client": [439, 728], "visible": True, "scale": 1.0}] * 30
        counter = {"i": 0}

        def scripted(hwnd, parent=0, inset=(0, 0, 0, 0)):
            item = dict(sequence[min(counter["i"], len(sequence) - 1)])
            counter["i"] += 1
            item.update({"supported": True, "hwnd": hwnd, "parent": parent})
            return item

        with patch.object(btrace, "window_probe", side_effect=scripted):
            watcher = btrace.watch("show", 1, 2, (0, 0, 0, 0), page_provider=lambda: page,
                                   duration=2.0, fast_ms=10, slow_ms=20, fast_until=0.3,
                                   stable_needed=2, min_ms=0)
            watcher.join(5.0)
        btrace.finish()
        module = self._module()
        out = self.dir / "out"
        target, report = module.analyze_one(self.dir, out)
        text = (target / "ANALYSIS.md").read_text(encoding="utf-8")
        self.assertIn("Позиция окна", text)
        self.assertIn("confirmed_attached", text)
        self.assertIn("Экспорт файлов", text)
        self.assertTrue(report["watches"])


if __name__ == "__main__":
    unittest.main()
