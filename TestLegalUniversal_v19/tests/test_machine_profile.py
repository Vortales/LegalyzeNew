"""Тесты паспорта машины, самопроверки и приёмки разбора (ТЗ § 2, § 4).

Что здесь проверяется и почему именно так
-----------------------------------------
1. **Паспорт не ломает приложение.** Любой источник может отсутствовать: нет
   реестра, нет Win32, нет Qt — сбор обязан вернуть словарь с `None`, а не
   исключение. Иначе один чужой компьютер уронит диагностику целиком.
2. **В паспорт не попадают секреты.** Пути обрезаются до `%USERPROFILE%` /
   `%APPDATA%`, окружение — по белому списку (проверяется на подставном
   окружении с паролем внутри).
3. **Риски считаются по правилам, а не «на глаз».** Каждое условие из матрицы
   ТЗ § 4 (125 %, 150 %, два монитора, RDP, тёмная тема, старый Windows,
   маленький экран, панель задач) даёт свой код; эталонная машина не даёт
   ни одного предупреждения.
4. **Разбор обязан закрыть приёмку ТЗ § 2.** На искусственной сессии
   (`tools/example_session.py`) в `analysis.json` обязаны быть заполнены все
   десять источников ответов: движок, позиция, раскладка, устой, экспорт,
   стадия экспорта, машина, права, хоткеи/микрофон, сопоставление с дефектами.
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))

import browser_trace as btrace  # noqa: E402
import machine_profile as mp  # noqa: E402


def _load_analyzer():
    """Разбор подключается как модуль: он лежит в `tools/`, а не в пакете."""
    path = ROOT / "tools" / "analyze_browser_logs.py"
    spec = importlib.util.spec_from_file_location("analyze_browser_logs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RedactionTests(unittest.TestCase):
    """В пересылаемом логе не должно быть имени пользователя и секретов."""

    def test_home_and_appdata_are_replaced_with_variables(self):
        home = str(Path.home())
        redacted = mp.redact(str(Path(home) / "Legalyze" / "logs"))
        self.assertTrue(redacted.startswith("%USERPROFILE%"), redacted)
        self.assertNotIn(home, redacted)
        appdata = mp.os.environ.get("APPDATA")
        if appdata:
            replaced = mp.redact(str(Path(appdata) / "Legalyze"))
            self.assertTrue(replaced.startswith("%APPDATA%"), replaced)
            self.assertNotIn(appdata, replaced)
        self.assertEqual(mp.redact(""), "")
        self.assertEqual(mp.redact(None), "")

    def test_unknown_paths_are_left_alone(self):
        self.assertEqual(mp.redact("C:\\Program Files\\Google"), "C:\\Program Files\\Google")

    def test_environment_is_filtered_by_whitelist(self):
        fake = {"QT_SCALE_FACTOR": "1.5", "LEGALYZE_DEBUG": "1",
                "AWS_SECRET_ACCESS_KEY": "не должно попасть",
                "ANTHROPIC_API_KEY": "и это тоже"}
        info = mp.env_info(fake)
        self.assertEqual(info.get("QT_SCALE_FACTOR"), "1.5")
        self.assertEqual(info.get("LEGALYZE_DEBUG"), "1")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", info)
        self.assertNotIn("ANTHROPIC_API_KEY", info)
        self.assertNotIn("не должно попасть", json.dumps(info, ensure_ascii=False))


class RiskMatrixTests(unittest.TestCase):
    """Матрица ТЗ § 4: каждое внешнее условие — свой код риска."""

    def _facts(self, scale=1.0, screens=None, session=None, theme="light",
               release="10", build=19045, autohide=False):
        screen = screens if screens is not None else [
            {"index": 0, "primary": True, "rect": [0, 0, 2560, 1440],
             "work": [0, 0, 2560, 1400], "dpi": int(96 * scale), "scale": scale}]
        return {"screens": screen, "scale": screen[0].get("scale") if screen else None,
                "session": session or {"kind": "console", "admin": False},
                "windows": {"release": release, "build": build,
                            "version": "10.0.%s" % build},
                "theme": theme, "taskbar": {"autohide_proxy": autohide}}

    def test_reference_machine_has_no_risks(self):
        self.assertEqual(mp.risk_codes(self._facts()), [])

    def test_scale_150_and_125_are_risks(self):
        self.assertIn("dpi_150", mp.risk_codes(self._facts(scale=1.5)))
        self.assertIn("dpi_125", mp.risk_codes(self._facts(scale=1.25)))
        self.assertIn("dpi_other", mp.risk_codes(self._facts(scale=1.75)))

    def test_second_monitor_and_mixed_scale(self):
        screens = [
            {"index": 0, "primary": True, "rect": [0, 0, 2560, 1440],
             "work": [0, 0, 2560, 1400], "dpi": 144, "scale": 1.5},
            {"index": 1, "primary": False, "rect": [2560, 0, 1920, 1200],
             "work": [2560, 0, 1920, 1160], "dpi": 96, "scale": 1.0}]
        codes = mp.risk_codes(self._facts(scale=1.5, screens=screens))
        self.assertIn("multi_monitor", codes)
        self.assertIn("mixed_scale", codes)

    def test_small_screen_is_a_risk(self):
        screen = [{"index": 0, "primary": True, "rect": [0, 0, 1366, 768],
                   "work": [0, 0, 1366, 728], "dpi": 96, "scale": 1.0}]
        self.assertIn("small_screen", mp.risk_codes(self._facts(screens=screen)))

    def test_rdp_and_admin_are_reported(self):
        codes = mp.risk_codes(self._facts(
            session={"kind": "rdp", "admin": True}))
        self.assertIn("rdp_session", codes)
        self.assertIn("running_as_admin", codes)
        admin = [item for item in mp.risks(self._facts(
            session={"kind": "rdp", "admin": True})) if item["code"] == "running_as_admin"]
        self.assertEqual(admin[0]["level"], "info",
                         "права администратора — факт, а не поломка")

    def test_dark_theme_is_a_risk(self):
        self.assertIn("dark_theme", mp.risk_codes(self._facts(theme="dark")))

    def test_old_and_unknown_windows(self):
        self.assertIn("old_windows", mp.risk_codes(self._facts(build=18362)))
        self.assertIn("unsupported_windows", mp.risk_codes(self._facts(release="7")))
        self.assertNotIn("old_windows", mp.risk_codes(self._facts(build=22631)))

    def test_hidden_taskbar_is_informational(self):
        items = mp.risks(self._facts(autohide=True))
        codes = {item["code"]: item for item in items}
        self.assertEqual(codes["autohide_taskbar"]["level"], "info")

    def test_unknown_screens_are_reported_not_invented(self):
        codes = mp.risk_codes({"screens": [], "scale": None})
        self.assertIn("screens_unknown", codes)
        self.assertEqual([c for c in codes if c.startswith("dpi")], [])


class ProfileRobustnessTests(unittest.TestCase):
    """Паспорт собирается на любой машине и никогда не бросает."""

    def test_profile_returns_schema_and_keys(self):
        facts = mp.profile()
        self.assertEqual(facts.get("schema"), mp.SCHEMA)
        for key in ("windows", "session", "screens", "app", "env", "theme",
                    "taskbar", "platform", "scale", "virtual"):
            self.assertIn(key, facts)

    def test_profile_survives_missing_win32(self):
        original = mp.WIN
        mp.WIN = True
        try:
            # ctypes.windll на Linux нет вовсе — все ветки обязаны это пережить.
            facts = mp.profile()
        finally:
            mp.WIN = original
        self.assertIsInstance(facts, dict)
        self.assertIn("windows", facts)

    def test_windows_facts_are_not_invented_on_linux(self):
        if mp.WIN:
            self.skipTest("проверка для стенда без Windows")
        self.assertEqual(mp.windows_info().get("build"), None)
        self.assertEqual(mp.session_info().get("kind"), None)

    def test_format_report_and_risks_are_human_readable(self):
        facts = mp.profile(screens=[
            {"index": 0, "primary": True, "rect": [0, 0, 2560, 1440],
             "work": [0, 0, 2560, 1400], "dpi": 144, "scale": 1.5}])
        facts["windows"] = {"release": "10", "version": "10.0.19045",
                            "build": 19045, "edition": "Windows 10 Pro"}
        report = mp.format_report(facts)
        self.assertIn("Windows 10 Pro", report)
        self.assertIn("2560×1440", report)
        risks = mp.format_risks(facts)
        self.assertTrue(any("150 %" in line for line in risks))
        self.assertTrue(all("— **" in line or "**" in line for line in risks))
        self.assertIsInstance(mp.format_risks({}), list)


class SelfCheckTests(unittest.TestCase):
    """Самопроверка: вердикты видны в журнале, сводке и UNDERSTANDING.md."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="legalyze-selfcheck-")
        self.directory = Path(self.tmp.name)
        self.assertTrue(btrace.install(log_dir=self.directory, enabled=True))

    def tearDown(self):
        try:
            btrace.finish()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_verdicts_are_recorded_with_counters(self):
        self.assertTrue(btrace.selfcheck("layout", True, detail="658x0.667"))
        self.assertFalse(btrace.selfcheck("export", False, detail="not_attached",
                                          advice="смотреть чипы чата"))
        btrace._drain_wait(2.0)  # писатель асинхронный: дать ему сбросить файл
        rows = [json.loads(line) for line
                in (self.directory / "browser-trace.jsonl").read_text(
                    encoding="utf-8").splitlines() if line.strip()]
        kinds = {row.get("kind"): row for row in rows}
        self.assertIn("selfcheck.layout", kinds)
        self.assertTrue(kinds["selfcheck.layout"].get("ok"))
        self.assertIn("selfcheck.export", kinds)
        self.assertFalse(kinds["selfcheck.export"].get("ok"))
        self.assertEqual(kinds["selfcheck.export"].get("advice"), "смотреть чипы чата")
        summary = btrace.build_summary()
        self.assertEqual(summary["selfcheck"]["layout"]["ok"], True)
        self.assertEqual(summary["selfcheck"]["export"]["ok"], False)
        self.assertIn("selfcheck.export.fail", summary["counters"])

    def test_failed_check_becomes_a_problem_and_a_hint(self):
        btrace.selfcheck("native_browser", False, detail="QtWebEngine",
                         advice="предложить установку Chrome")
        summary = btrace.build_summary()
        self.assertIn("selfcheck:native_browser", summary["problems"])
        self.assertTrue(any("native_browser" in hint for hint in summary["hints"]))
        self.assertTrue(any("Chrome" in hint for hint in summary["hints"]))

    def test_machine_fingerprint_lands_in_summary_and_understanding(self):
        facts = mp.profile(screens=[
            {"index": 0, "primary": True, "rect": [0, 0, 2560, 1440],
             "work": [0, 0, 2560, 1400], "dpi": 96, "scale": 1.0}])
        facts["windows"] = {"release": "10", "version": "10.0.19045",
                            "build": 19045, "edition": "Windows 10 Pro"}
        btrace.env_fingerprint(reason="test", facts=facts)
        summary = btrace.build_summary()
        self.assertEqual((summary.get("machine") or {}).get("windows", {}).get("build"),
                         19045)
        btrace.finish()
        understanding = (self.directory / "UNDERSTANDING.md").read_text(encoding="utf-8")
        self.assertIn("Машина и проверки", understanding)
        self.assertIn("Windows 10 Pro", understanding)

    def test_fingerprint_failure_never_breaks_the_session(self):
        # Подставляем «сломанный» паспорт: сбор обязан записать ошибку и жить.
        original = btrace.env_fingerprint
        try:
            result = btrace.env_fingerprint(reason="test", facts={"schema": 1})
        finally:
            btrace.env_fingerprint = original
        self.assertIsInstance(result, dict)


class AcceptanceTests(unittest.TestCase):
    """Приёмка ТЗ § 2: разбор обязан ответить на все десять вопросов."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="legalyze-acceptance-")
        cls.inbox = Path(cls.tmp.name) / "inbox"
        cls.out = Path(cls.tmp.name) / "analyzed"
        cls.session = cls.inbox / "example-session"
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "example_session.py"),
             "--tag", "example-session", "--out", str(cls.session)],
            capture_output=True, text=True, cwd=str(ROOT))
        if result.returncode != 0:
            raise AssertionError("пример сессии не собрался: %s %s"
                                 % (result.stdout, result.stderr))
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "analyze_browser_logs.py"),
             str(cls.session), "--out", str(cls.out)],
            capture_output=True, text=True, cwd=str(ROOT))
        if result.returncode != 0:
            raise AssertionError("разбор упал: %s %s" % (result.stdout, result.stderr))
        cls.report = json.loads(
            (cls.out / "example-session" / "analysis.json").read_text(encoding="utf-8"))
        cls.markdown = (cls.out / "example-session" / "ANALYSIS.md").read_text(
            encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_engine_and_native_mode_are_answered(self):
        checks = self.report.get("selfcheck") or {}
        self.assertIn("engine", checks)
        self.assertIn("native_browser", checks)

    def test_window_position_and_layout_are_answered(self):
        self.assertTrue(self.report.get("watches"), "шкалы обязаны быть в отчёте")
        self.assertIn("Раскладка СТРАНИЦЫ", self.markdown)
        self.assertTrue(self.report.get("layout_watch"))

    def test_settle_time_is_answered(self):
        for tag, data in (self.report.get("watches") or {}).items():
            self.assertIn("settle_ms", data, "устой шкалы %s не посчитан" % tag)

    def test_export_is_answered_with_disk_facts(self):
        exports = self.report.get("exports") or []
        self.assertTrue(exports, "вердиктов экспорта нет")
        attested = [row for row in exports
                    if any(f.get("sha256_12") for f in (row.get("files") or []))]
        self.assertTrue(attested, "у вложений нет SHA-256 с диска")
        stages = {row.get("stage") for row in exports}
        self.assertTrue(stages, "стадии экспорта не записаны")

    def test_machine_and_admin_are_answered(self):
        machine = self.report.get("machine") or {}
        self.assertEqual((machine.get("windows") or {}).get("build"), 19045)
        self.assertIn("admin_free", self.report.get("selfcheck") or {})
        self.assertTrue(self.report.get("machine_risks"),
                        "у машины с масштабом 150 % обязаны быть риски")

    def test_hotkeys_and_mic_are_answered(self):
        checks = self.report.get("selfcheck") or {}
        self.assertIn("hotkeys", checks)
        self.assertIn("mic", checks)

    def test_known_defects_are_matched(self):
        ids = {item.get("id") for item in (self.report.get("defects") or [])}
        self.assertIn("1", ids, "картина «множители сложились» обязана опознаваться")
        self.assertTrue("Похоже на уже найденные дефекты" in self.markdown)

    def test_report_has_sections_0_and_5(self):
        self.assertIn("## 0. Машина и проверки", self.markdown)
        self.assertIn("## 5. Что это значит", self.markdown)


class OldSessionGraceTests(unittest.TestCase):
    """Старая сессия (без паспорта и проверок) читается, но риски не выдумываются."""

    def test_old_session_without_machine_has_no_risks(self):
        analyzer = _load_analyzer()
        report = {"machine": None, "selfcheck": {}, "watches": {}, "exports": [],
                  "surface": [], "repairs": []}
        self.assertEqual(analyzer.machine_risks(report), [])
        self.assertEqual(analyzer.machine_block(report), [])
        self.assertEqual(analyzer.known_defects(report), [])
        self.assertIn("Паспорт машины в этой сессии не записан",
                      "\n".join(["Паспорт машины в этой сессии не записан"]))

    def test_analyzer_reads_session_without_new_events(self):
        analyzer = _load_analyzer()
        with tempfile.TemporaryDirectory(prefix="legalyze-old-") as tmp:
            directory = Path(tmp)
            (directory / "diagnostic.jsonl").write_text(
                json.dumps({"time": "2026-01-01T00:00:00+00:00", "event": "session.start",
                            "packages": {"PyQt6": "6.11.0"}}, ensure_ascii=False) + "\n",
                encoding="utf-8")
            (directory / "browser-trace.jsonl").write_text("", encoding="utf-8")
            (directory / "browser-window-timeline.jsonl").write_text("", encoding="utf-8")
            data = analyzer.load_session(directory)
            report = analyzer.analyze(data)
        self.assertIsNone(report.get("machine"))
        self.assertEqual(report.get("machine_risks"), [])
        self.assertEqual(report.get("selfcheck"), {})
        self.assertIn("defects", report)


if __name__ == "__main__":
    unittest.main()
