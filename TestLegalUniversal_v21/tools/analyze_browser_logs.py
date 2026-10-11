#!/usr/bin/env python3
"""analyze_browser_logs.py — разбор логов браузера, присланных пользователем.

Зачем
-----
Пользователь присылает папку `%APPDATA%\\Legalyze\\logs\\<сессия>` (или её
архив). Читать её глазами нельзя: там десятки тысяч строк. Этот скрипт
превращает папку в один человеческий отчёт `ANALYSIS.md`, в котором есть:

* что за сессия (версия, Windows, масштаб, какой браузер найден и запущен);
* **позиция окна**: где окно «съезжало», на сколько пикселей, сколько
  миллисекунд держалось расхождение и чем оно объясняется (шкалы `watch.*`);
* **экспорт файлов**: по каждому вложению — файл на диске (размер, SHA-256),
  результат впрыска, что оказалось в `input[type=file]` и в чипах чата,
  итоговый вердикт «прикрепилось / не прикрепилось / вложений тут нет»;
* что ещё происходило: обнаружение браузера, поверхность страницы, шторка,
  ошибки и предупреждения счётчиками;
* список вероятных причин и что смотреть дальше.

Запуск
------
    python3 tools/analyze_browser_logs.py                 # весь inbox
    python3 tools/analyze_browser_logs.py <папка_или_zip>  # одна сессия
    python3 tools/analyze_browser_logs.py --list           # что лежит в inbox

Результат: `browser_logs/analyzed/<имя>/ANALYSIS.md` и `analysis.json`.
Только стандартная библиотека — запускается и на Linux, и на Windows.
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
DEFAULT_INBOX = REPO / "browser_logs" / "inbox"
DEFAULT_OUT = REPO / "browser_logs" / "analyzed"

#: Коды причин из `browser_trace.REASONS` — здесь свои подсказки: что делать.
ADVICE = {
    "dpi_scale": "Масштаб монитора: сдвиг задан в DIP, а окно — в физических px. "
                 "Проверить `_browser_inset()` и `_monitor_scale()`; лечится пересчётом inset.",
    "parent_moved": "Родитель переехал после расчёта цели: нужен синхронный перенос "
                    "(`sync_now`) сразу после перемещения окна приложения.",
    "parent_clipped": "Цель выходит за клиентскую область плейсхолдера: уменьшить окно "
                      "браузера или снять сдвиг (BROWSER_DX/DY, BROWSER_DW/DH).",
    "size_clamped": "Windows/Chromium держит минимальный размер окна: сверить "
                    "BROWSER_DW/BROWSER_DH и запрошенный прямоугольник.",
    "call_failed": "`SetWindowPos` вернул 0 — смотреть код GetLastError в `win32.call`.",
    "async_pending": "Вызов отправлен, окно ещё не переехало: перейти на `sync_now`.",
    "not_child": "Окно оторвано от плейсхолдера: смотреть `embed.SetParent` и `embed.failed`.",
    "detached_style": "С окна снят WS_CHILD: повторно выполнить `embed`.",
    "hidden": "Окно скрыто: смотреть `win32.show_window` и шторку.",
    "dead_window": "Окно браузера уничтожено (процесс умер или перезапустился).",
    "page_lost": "CDP-соединение потеряно: страница перезагружалась или упала.",
}

#: Подсказки по рискам МАШИНЫ (коды из `machine_profile.RISK_RULES`) — на случай,
#: когда модуль паспорта недоступен и риски приходят текстом.
MACHINE_ADVICE = {
    "dpi_125": "Масштаб 125 %: пересчитать DIP↔px (`_monitor_scale`, BROWSER_DX/DY).",
    "dpi_150": "Масштаб 150 %: то же, но расхождение вдвое заметнее.",
    "dpi_other": "Нестандартный масштаб: проверять пересчёт по замеру.",
    "multi_monitor": "Несколько мониторов: следить за `parent_moved` и сменой экрана.",
    "mixed_scale": "Разный масштаб мониторов: при переносе окна ждать `dpi_scale`.",
    "rdp_session": "RDP: нет GPU-ускорения, DPI может меняться на ходу.",
    "small_screen": "Маленький экран: целевое окно 453×735 может не помещаться.",
    "dark_theme": "Тёмная тема Windows: проверить `page.probe.dark`/`page.theme_probe`.",
    "old_windows": "Старая сборка Windows: проверить DPI-API и зависимости Chrome.",
    "unsupported_windows": "Windows старше 10: нативный Chrome может не запускаться.",
    "screens_unknown": "Геометрию мониторов прочитать не удалось.",
    "running_as_admin": "Запуск с правами: путь обычного пользователя не проверен.",
}

#: Какая проверка (`selfcheck.<имя>`) на какой дефект из `ОШИБКИ.md` указывает.
SELFCHECK_TO_DEFECT = {
    "window_position": "1",
    "layout": "1",
    "export": "3",
    "mic": "4",
    "native_browser": "2",
    "hotkeys": "7",
}


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Разбор логов браузера Legalyze")
    parser.add_argument("session", nargs="?", help="папка сессии или zip-архив")
    parser.add_argument("--inbox", default=str(DEFAULT_INBOX))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--list", action="store_true", help="показать, что в inbox")
    return parser.parse_args(argv)


def looks_like_session(path: Path) -> bool:
    if not path.is_dir():
        return False
    names = {p.name for p in path.iterdir() if p.is_file()}
    return bool(names & {"diagnostic.jsonl", "browser-trace.jsonl",
                         "browser-trace-summary.json", "events.log"})


def discover(inbox: Path):
    """Найти сессии в inbox: папки и zip-архивы.

    Архивы распаковываются рядом (в `_unpacked/<имя>`), исходный zip не трогаем.
    """
    found = []
    if not inbox.exists():
        return found
    for entry in sorted(inbox.iterdir()):
        if entry.is_dir() and entry.name.startswith("_"):
            continue
        if looks_like_session(entry):
            found.append(entry)
            continue
        if entry.suffix.lower() == ".zip":
            target = inbox / "_unpacked" / entry.stem
            if not target.exists():
                try:
                    target.mkdir(parents=True, exist_ok=True)
                    with zipfile.ZipFile(entry) as archive:
                        archive.extractall(target)
                except Exception as exc:  # noqa: BLE001
                    print("не удалось распаковать %s: %s" % (entry.name, exc))
                    continue
            nested = [p for p in nested_sessions(target)]
            if nested:
                found.extend(nested)
            elif looks_like_session(target):
                found.append(target)
    return found


def nested_sessions(root: Path):
    """Сессии могут лежать на уровень глубже (logs/<сессия>)."""
    for child in sorted(root.iterdir()):
        if child.is_dir() and looks_like_session(child):
            yield child


def read_jsonl(path: Path, limit=400000):
    rows = []
    if not path.exists():
        return rows
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for index, line in enumerate(stream):
                if index >= limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except OSError:
        pass
    return rows


def load_session(directory: Path):
    data = {
        "dir": str(directory),
        "name": directory.name,
        "diagnostic": read_jsonl(directory / "diagnostic.jsonl"),
        "trace": read_jsonl(directory / "browser-trace.jsonl"),
        "timeline": read_jsonl(directory / "browser-window-timeline.jsonl"),
        "summary": None,
        "understanding": "",
        "artifacts": sorted(p.name for p in directory.iterdir()
                            if p.is_file() and p.suffix in (".html", ".json")),
    }
    summary_path = directory / "browser-trace-summary.json"
    if summary_path.exists():
        try:
            data["summary"] = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            data["summary"] = None
    understanding = directory / "UNDERSTANDING.md"
    if understanding.exists():
        try:
            data["understanding"] = understanding.read_text(encoding="utf-8")
        except OSError:
            data["understanding"] = ""
    stacks_path = directory / "stacks.log"
    if stacks_path.exists():
        try:
            data["stacks"] = stacks_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            data["stacks"] = ""
    else:
        data["stacks"] = ""
    return data


def first_of(rows, event, key="event"):
    for row in rows:
        if row.get(key) == event:
            return row
    return None


def counter_of(rows, key="event"):
    return Counter(str(row.get(key)) for row in rows if row.get(key))


def _rederive_verdict(row):
    """Вердикт по фактам из записи — та же логика, что в `browser_trace`.

    Нужно, чтобы СТАРЫЕ сессии (до правки) читались правильно: в ранних сборках
    проверка `input[type=file]` шла раньше чипов, и уже прикреплённый файл
    объявлялся как «на этой поверхности вложение невозможно» (подтверждено
    логом 20261004-162559: chips=4, имена файлов заполнены).
    """
    page = row.get("page") or {}
    files = row.get("files") or []
    if not isinstance(page, dict) or not page:
        return row.get("verdict") or "page_unavailable"
    inputs = int(page.get("file_inputs") or 0)
    with_files = int(page.get("inputs_with_files") or 0)
    chips = int(page.get("chips") or 0)
    names = [str(n).strip() for n in (page.get("chip_names") or []) if str(n).strip()]
    if not names:
        names = [str(n).strip() for n in (row.get("names") or []) if str(n).strip()]
    disk_ok = bool(files) and all(f.get("exists") for f in files)
    if chips > 0 and names:
        return "confirmed_attached"
    if chips > 0:
        return "attached_unnamed"
    if not disk_ok and files:
        return "file_missing_on_disk"
    if with_files > 0:
        return "injected_not_attached"
    if inputs == 0:
        return "surface_without_file_input"
    return "not_attached"


def _same_export(left, right):
    """Одинаковые ли проверки — чтобы свёрнуть повторы в одну строку отчёта."""
    def sign(row):
        page = row.get("page") or {}
        return (row.get("stage"), row.get("verdict"),
                tuple((f.get("name"), f.get("sha256_12")) for f in (row.get("files") or [])),
                int(page.get("chips") or 0), int(page.get("file_inputs") or 0),
                tuple(str(n) for n in (page.get("chip_names") or [])))
    return sign(left) == sign(right)


def analyze(data):
    diag = data["diagnostic"]
    trace = data["trace"]
    timeline = data["timeline"]
    report = {
        "session": data["name"],
        "dir": data["dir"],
        "started": None,
        "versions": {},
        "browser": {},
        "screens": [],
        "watches": {},
        "slow_settles": {},
        "win_calls": {"total": 0, "failed": 0, "off_target": 0, "examples": []},
        "reasons": Counter(),
        "exports": [],
        "exports_repeated": 0,
        "surface": [],
        "errors": Counter(),
        "warnings": Counter(),
        "marks": Counter(),
        "artifacts": data["artifacts"],
        "advice": [],
        "machine": None,
        "machine_risks": [],
        "selfcheck": {},
        "repairs": [],
        "defects": [],
        "stacks": data.get("stacks") or "",
    }
    start = first_of(diag, "session.start")
    if start:
        report["started"] = start.get("time")
        report["versions"] = start.get("packages") or {}
        report["env"] = {k: start.get(k) for k in
                         ("platform", "python", "windows_build", "frozen",
                          "nuitka_compiled", "modes")}
    for row in diag:
        event = str(row.get("event") or "")
        if event == "qt.screen":
            report["screens"].append(row)
        elif event == "browser.discovered":
            report["browser"].setdefault("found", []).append(
                {k: row.get(k) for k in ("name", "kind", "rank", "path")})
        elif event == "browser.try":
            report["browser"].setdefault("tried", []).append(
                {k: row.get(k) for k in ("name", "kind", "attempt", "gpu")})
        elif event == "browser.mode":
            report["browser"]["mode"] = row
        elif event == "browser.embedded":
            report["browser"]["embedded"] = row
        elif event.startswith("browser.retry"):
            report["browser"].setdefault("retries", []).append(row)
        elif event == "page.surface" or event.startswith("page.surface_after"):
            report["surface"].append({k: row.get(k) for k in
                                      ("time", "verdict", "ready", "composer", "files",
                                       "chips", "dark", "bg", "host", "path", "event")})
        elif event.startswith("exception"):
            report["errors"]["%s:%s" % (row.get("where"), row.get("type"))] += 1
        elif event.startswith("warn."):
            report["warnings"][str(row.get("event"))] += 1
        elif event == "machine.fingerprint" and not report["machine"]:
            report["machine"] = _strip_record(row)
        elif event.startswith("selfcheck."):
            report["selfcheck"].setdefault(
                event.split(".", 1)[1],
                {"ok": bool(row.get("ok")), "detail": row.get("detail") or "",
                 "advice": row.get("advice") or "", "time": row.get("time")})
        elif event == "browser.layout_repair":
            report["repairs"].append({k: row.get(k) for k in
                                      ("time", "reason", "innerWidth", "dpr",
                                       "restored")})
        elif event == "browser.layout_watch":
            report.setdefault("layout_watch", []).append(
                {k: row.get(k) for k in ("time", "reason", "ok", "fixed", "good",
                                         "ms", "innerWidth", "dpr")})

    for row in trace:
        kind = str(row.get("kind") or "")
        if kind == "machine.fingerprint":
            report["machine"] = _strip_record(row)
        elif kind.startswith("selfcheck."):
            report["selfcheck"][kind.split(".", 1)[1]] = {
                "ok": bool(row.get("ok")), "detail": row.get("detail") or "",
                "advice": row.get("advice") or "", "time": row.get("time")}
        if kind == "win32.call":
            report["win_calls"]["total"] += 1
            if not row.get("ok"):
                report["win_calls"]["failed"] += 1
            if row.get("verdict") not in (None, "on_target", "unsupported"):
                report["win_calls"]["off_target"] += 1
                if len(report["win_calls"]["examples"]) < 12:
                    report["win_calls"]["examples"].append(row)
                # Причины считаем только у вызовов, которые НЕ попали в цель:
                # «hidden» при штатном скрытии окна — не поломка, а факт, и в
                # списке «причин расхождений» он выглядел как проблема.
                for code in (row.get("reasons") or []):
                    report["reasons"][str(code)] += 1
        elif kind == "win32.call.ok":
            report["win_calls"]["total"] += 1
        elif kind == "export.audit":
            if row.get("repeated"):
                report["exports_repeated"] = int(report.get("exports_repeated") or 0) + 1
                continue
            row = dict(row)
            row["verdict_saved"] = row.get("verdict")
            row["verdict"] = _rederive_verdict(row)
            if report["exports"] and _same_export(report["exports"][-1], row):
                report["exports"][-1]["count"] = int(report["exports"][-1].get("count") or 1) + 1
                report["exports"][-1]["last_time"] = row.get("time")
                report["exports_repeated"] = int(report.get("exports_repeated") or 0) + 1
            else:
                row["count"] = 1
                row["last_time"] = row.get("time")
                report["exports"].append(row)
        elif kind == "mark.browser.layout_repair":
            report["repairs"].append({k: row.get(k) for k in
                                      ("time", "reason", "innerWidth", "dpr",
                                       "restored")})
        elif kind.startswith("mark.browser.layout_watch"):
            report.setdefault("layout_watch", []).append(
                {k: row.get(k) for k in ("time", "reason", "ok", "fixed", "good",
                                         "ms", "innerWidth", "dpr")})
        elif kind.startswith("mark."):
            report["marks"][kind] += 1
        elif kind == "watch.slow_settle":
            # Отдельный словарь: если писать в report["watches"][tag], ключ
            # создаётся раньше полного набора полей и разбор падает на
            # KeyError 'samples' (проверено на примере сессии).
            report["slow_settles"][str(row.get("tag"))] = row.get("settle_ms")

    # Временная шкала: по каждой шкале — таймлайн изменений и вывод об устое.
    for row in timeline:
        tag = str(row.get("tag") or "")
        if not tag:
            continue
        item = report["watches"].setdefault(tag, {"samples": 0, "changes": 0,
                                                  "deviations": 0, "first_ms": None,
                                                  "last_ms": 0, "max_offset_px": 0,
                                                  "off_target_until_ms": None,
                                                  "events": [], "final_verdict": None,
                                                  "started_at": None, "settled_at": None,
                                                  "settle_ms": None, "ended": False,
                                                  "off_episodes": None})
        ms = int(row.get("ms") or 0)
        item["samples"] += 1
        item["last_ms"] = max(item["last_ms"], ms)
        if item["started_at"] is None:
            item["started_at"] = row.get("time")
        # Раскладка СТРАНИЦЫ — отдельно от окна: в живой сессии окно стояло
        # идеально (dx=dy=0), а «съезжали» объекты внутри, потому что Chrome
        # переприменял зум профиля поверх эмуляции. Собираем состояния на
        # каждой строке, а не только на «отклонениях».
        item.setdefault("layout_states", [])
        page_row = row.get("page") or {}
        if page_row.get("iw"):
            state_key = (int(page_row.get("iw") or 0),
                         round(float(page_row.get("dpr") or 0.0), 3))
            if not item["layout_states"] or item["layout_states"][-1]["state"] != state_key:
                if item["layout_states"]:
                    item["layout_states"][-1]["until_ms"] = ms
                item["layout_states"].append({"ms": ms, "state": state_key,
                                              "until_ms": None})
        kind = row.get("kind")
        if kind == "watch.change":
            item["changes"] += 1
        if kind == "watch.end":
            # Итог шкалы: точнее, чем «первый кадр на цели» — окно может
            # мигнуть на цели и уехать снова (см. ОШИБКИ.md №1).
            item["ended"] = True
            item["settle_ms"] = row.get("settle_ms")
            if row.get("off_episodes") is not None:
                item["off_episodes"] = row.get("off_episodes")
            item["deviations"] = max(int(item.get("deviations") or 0),
                                     int(row.get("deviations") or 0))
            item["max_offset_px"] = max(int(item.get("max_offset_px") or 0),
                                        int(row.get("max_offset_px") or 0))
            if row.get("final_verdict"):
                item["final_verdict"] = row.get("final_verdict")
            continue
        verdict = row.get("verdict")
        if verdict:
            item["final_verdict"] = verdict
        # Причины считаем только по изменениям: в «пульсе» повторяется та же
        # картина, и счётчик раздувался бы в разы (проверено на примере).
        if row.get("kind") == "watch.change":
            for code in (row.get("reasons") or []):
                report["reasons"][str(code)] += 1
        if verdict and verdict != "on_target":
            item["deviations"] += 1
            item["off_target_until_ms"] = ms
            offset = abs(int(row.get("dx") or 0)) + abs(int(row.get("dy") or 0))
            item["max_offset_px"] = max(item["max_offset_px"], offset)
            item["events"].append({"ms": ms, "time": row.get("time"),
                                   "dx": row.get("dx"), "dy": row.get("dy"),
                                   "dw": row.get("dw"), "dh": row.get("dh"),
                                   "client": row.get("client"),
                                   "rect": row.get("rect"), "target": row.get("target"),
                                   "reasons": row.get("reasons"),
                                   "page": row.get("page"), "extra": row.get("extra")})
        elif verdict == "on_target" and item.get("settled_at") is None and ms > 0:
            item["settled_at"] = row.get("time")
            item["settle_ms"] = ms
        if item["first_ms"] is None:
            item["first_ms"] = ms
    report["machine_risks"] = machine_risks(report)
    report["defects"] = known_defects(report)
    return report


#: Служебные поля записи журнала — в паспорт машины они не относятся.
RECORD_KEYS = ("kind", "pid", "thread", "t", "time", "level")


def _strip_record(row):
    """Запись журнала → чистый словарь данных (без служебных полей записи)."""
    return {k: v for k, v in dict(row).items() if k not in RECORD_KEYS}


def machine_risks(report):
    """Риски МАШИНЫ по её паспорту (`machine.fingerprint`).

    Правила живут в `machine_profile.RISK_RULES` — одна точка правды и для
    приложения, и для разбора. Если модуля рядом нет, риски не выдумываются:
    лучше пустой список, чем подсказка «на глаз».
    """
    facts = report.get("machine") or {}
    if not facts:
        return []
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import machine_profile as mp
        risks = mp.risks(facts)
    except Exception:
        return []
    for item in risks:
        item.setdefault("advice", MACHINE_ADVICE.get(item.get("code"), ""))
    return risks


def machine_block(report):
    """Строки «Машина»: паспорт + риски. Пусто, если паспорта нет."""
    facts = report.get("machine") or {}
    if not facts:
        return []
    lines = []
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import machine_profile as mp
        lines.extend(mp.format_report(facts).splitlines())
    except Exception:
        lines.append("- Паспорт машины прочитан, но отчёт собрать не удалось.")
    risks = report.get("machine_risks") or []
    if risks:
        lines.append("")
        lines.append("Риски машины (по паспорту, не по догадке):")
        for item in sorted(risks, key=lambda x: 0 if x.get("level") == "warn" else 1):
            detail = (" — %s" % item["detail"]) if item.get("detail") else ""
            advice = (" **Что делать:** %s" % item["advice"]) if item.get("advice") else ""
            lines.append("- **%s** (%s)%s.%s" % (item.get("text"), item.get("level"),
                                                 detail, advice))
    return lines


def selfcheck_block(report):
    """Таблица самопроверок: что приложение проверило и с каким итогом."""
    checks = report.get("selfcheck") or {}
    if not checks:
        return []
    lines = ["| Проверка | Итог | Подробности |", "|---|---|---|"]
    for name, data in sorted(checks.items()):
        detail = str(data.get("detail") or "").replace("|", "\\|")
        advice = str(data.get("advice") or "").replace("|", "\\|")
        if not data.get("ok") and advice:
            detail = ("%s. Что делать: %s" % (detail, advice)).strip(". ")
        lines.append("| %s | %s | %s |" % (name, "ок" if data.get("ok") else "**НЕ ОК**",
                                           detail))
    return lines


def known_defects(report):
    """Сопоставить факты сессии с уже найденными дефектами (`ОШИБКИ.md`).

    Смысл: разбор не должен каждый раз заново «догадываться». Если картина
    совпала с известным дефектом — это пишется прямо, вместе с номером записи,
    и человек сразу видит «это уже было, лечение такое-то».
    """
    found = []

    def add(number, title, evidence):
        if not any(item["id"] == number for item in found):
            found.append({"id": number, "title": title, "evidence": evidence})

    if _multiply_note(report):
        add("1", "раскладка «съехала»: множители зума сложились (0.667×0.667)",
            "таблица раскладки по замерам")
    for tag, item in (report.get("watches") or {}).items():
        if int(item.get("deviations") or 0) > 0:
            add("1", "окно/раскладка уходили от цели",
                "шкала «%s»: %s строк «не на цели»" % (tag, item.get("deviations")))
            break
    repairs = report.get("repairs") or []
    if repairs:
        add("1", "исправление v20 сработало: раскладка возвращена ремонтом",
            "browser.layout_repair ×%d (fixed=%s)" % (len(repairs), repairs[0].get("fixed")))
    stacks = report.get("stacks") or ""
    if "0xc000001d" in stacks or "_win_screens" in stacks:
        add("9", "фатальный сбой процесса 0xc000001d (Illegal instruction) при снятии паспорта",
            "stacks.log: machine_profile._win_screens (Win32 коллбэк EnumDisplayMonitors в v20; устранено в v21)")
    elif stacks.strip():
        first_line = stacks.strip().splitlines()[0]
        add("9", "фатальный сбой процесса (%s)" % first_line, "stacks.log")
    bad_exports = [row for row in (report.get("exports") or [])
                   if row.get("verdict") not in ("confirmed_attached", "attached_unnamed")]
    if bad_exports:
        add("3", "файлы могли не прикрепиться",
            "вердикты: %s" % ", ".join(sorted({str(r.get("verdict")) for r in bad_exports})))
    if any(row.get("dark") for row in (report.get("surface") or [])):
        add("5", "страница пришла тёмной", "page.surface: dark=true")
    checks = report.get("selfcheck") or {}
    for name, data in checks.items():
        if data.get("ok"):
            continue
        number = SELFCHECK_TO_DEFECT.get(name) or "2"
        add(number, "не пройдена проверка «%s»" % name,
            str(data.get("detail") or "")[:160])
    risks = report.get("machine_risks") or []
    if risks:
        add("2", "машина отличается от эталонной (класс «не у меня») ",
            "риски: %s" % ", ".join(item.get("code") for item in risks[:5]))
    return found


def _multiply_note(report):
    """«Множители сложились»: dpr уехал в 0.445 при цели 0.667 — это 0.667².

    Так выглядит САМЫЙ частый вид поломки: объявление CDP с
    `deviceScaleFactor` складывается с зумом профиля. Найдено по сессии
    20261004-162559-12408-c5e41c, где в одну точку времени попали
    `browser.metrics_nudged` (толчок с dsf) и `browser.viewport ... dsf=0.6672`,
    а замер показал `innerWidth=987 dpr=0.445` вместо `658 x 0.667`.
    """
    notes = []
    zoom = None
    for key, value in (report.get("versions") or {}).items():
        if "zoom" in str(key).lower():
            try:
                zoom = float(value)
            except (TypeError, ValueError):
                zoom = None
    for tag, item in (report.get("watches") or {}).items():
        states = item.get("layout_states") or []
        for entry in states:
            try:
                width, dpr = entry["state"][0], float(entry["state"][1])
            except (TypeError, ValueError, IndexError):
                continue
            if not width or not dpr:
                continue
            target = None
            for other in states:
                try:
                    if abs(float(other["state"][1]) * float(other["state"][1]) - dpr) < 0.01:
                        target = float(other["state"][1])
                        break
                except (TypeError, ValueError, IndexError):
                    continue
            if target is None and zoom and abs(dpr - zoom * zoom) < 0.01:
                target = zoom
            if target and target > dpr + 0.05:
                notes.append(
                    "Шкала «%s»: на %s мс `dpr=%.3f` — это %.3f×%.3f, то есть "
                    "**множители зума СЛОЖИЛИСЬ** (переопределение CDP с "
                    "`deviceScaleFactor` поверх зума профиля); ширина раскладки "
                    "при этом %d css px вместо %d. Лечение — объявлять "
                    "переопределение в логических пикселях окна при `dsf=1.0` "
                    "(`browser.layout_repair`), а не ждать таймера."
                    % (tag, entry.get("ms"), dpr, target, target, width,
                       int(round(width * dpr / target))))
                return notes
    return notes


def _layout_note(report):
    """Одно предложение о главном: окно стоит, а раскладка уезжала."""
    notes = []
    for tag, item in (report.get("watches") or {}).items():
        states = item.get("layout_states") or []
        if len(states) > 1:
            first = states[0]["state"]
            others = [e for e in states if e["state"] != first]
            if others:
                wrong = others[0]
                width = max(1, int(item.get("last_ms") or 1))
                percents = round(100.0 * float(wrong["state"][0]) / float(first[0] or 1))
                notes.append(
                    "Шкала «%s»: раскладка страницы уходила с %d×%s на %d×%s "
                    "(≈%d %% от исходной ширины) и возвращалась — это и есть "
                    "видимое «объекты съехали»."
                    % (tag, first[0], first[1], wrong["state"][0], wrong["state"][1], percents))
    return notes


def render(report):
    lines = []
    add = lines.append
    add("# Разбор логов браузера: %s" % report["session"])
    add("")
    add("- Папка: `%s`" % report["dir"])
    if report.get("started"):
        add("- Начало сессии: %s" % report["started"])
    if report.get("env"):
        env = report["env"]
        add("- Платформа: %s, Python %s%s" % (env.get("platform"), env.get("python"),
                                              ", exe" if env.get("frozen") else " (из исходников)"))
        if env.get("windows_build"):
            add("- Windows: %s" % env["windows_build"])
        modes = {k: v for k, v in (env.get("modes") or {}).items() if v}
        if modes:
            add("- Переменные окружения режимов: %s" % json.dumps(modes, ensure_ascii=False))
    if report.get("versions"):
        add("- Версии: %s" % ", ".join("%s=%s" % (k, v) for k, v in report["versions"].items()))
    add("")
    add("## 0. Машина и проверки")
    add("")
    block = machine_block(report)
    if block:
        lines.extend(block)
    else:
        add("Паспорт машины в этой сессии не записан (старая сборка либо сбор "
            "отключён). Риски машины в таком случае НЕ вычисляются: догадки "
            "вместо чисел в отчёт не попадают.")
    checks = selfcheck_block(report)
    add("")
    if checks:
        add("Самопроверка приложения (вердикты ставит само приложение):")
        add("")
        lines.extend(checks)
    else:
        add("Самопроверок в этой сессии нет (старая сборка).")
    watch = report.get("layout_watch") or []
    if watch:
        add("")
        add("Удержание раскладки (v20): записей `browser.layout_watch` — %d, "
            "ремонтов `browser.layout_repair` — %d."
            % (len(watch), len(report.get("repairs") or [])))
    add("")
    add("## 1. Окружение и браузер")
    add("")
    screens = report.get("screens") or []
    if screens:
        add("| Экран | Геометрия | Рабочая область | DPI | Масштаб |")
        add("|---|---|---|---|---|")
        for screen in screens:
            add("| — | %s | %s | %s | %s |" % (screen.get("geometry"),
                                               screen.get("available"),
                                               screen.get("dpi"), screen.get("dpr")))
    browser = report.get("browser") or {}
    found = browser.get("found") or []
    if found:
        add("")
        add("Найдены браузеры (в порядке предпочтения):")
        for item in found[:8]:
            add("- `%s` (%s, ранг %s)" % (item.get("name"), item.get("kind"), item.get("rank")))
    mode = browser.get("mode")
    if mode:
        add("")
        add("Итоговый режим: **%s** (`%s`), порт %s, шторка/вставка: `%s`"
            % (mode.get("engine"), mode.get("exe") or mode.get("kind"),
               mode.get("port"), (browser.get("embedded") or {}).get("kind")))
        add("")
        add("Параметры вставки: inset=%s, dx=%s, dy=%s, zoom=%s, тема светлая=%s, язык=%s"
            % (mode.get("inset"), mode.get("dx"), mode.get("dy"), mode.get("zoom"),
               mode.get("light_theme"), mode.get("lang")))
    retries = browser.get("retries") or []
    if retries:
        add("")
        add("Перезапуски/повторы браузера: %s" % ", ".join(
            "%s (%s)" % (r.get("reason"), r.get("attempt")) for r in retries[:10]))
    add("")
    add("## 2. Позиция окна: «съезжало и возвращалось»")
    add("")
    watches = report.get("watches") or {}
    if not watches:
        add("Временных шкал в логе нет: приложение не показывало/не скрывало окно "
            "либо сбор шкал не попал в эту сессию.")
    for tag, item in sorted(watches.items()):
        add("### Шкала «%s»" % tag)
        add("")
        add("- Замеров: %s, изменений состояния: %s, строк «не на цели»: %s, уходов: %s"
            % (item.get("samples"), item.get("changes"), item.get("deviations"),
               item.get("off_episodes") if item.get("off_episodes") is not None else "?"))
        if item.get("started_at"):
            add("- Начало: %s" % item["started_at"])
        if item.get("settle_ms") is not None:
            add("- **Встало на цель через ≈%s мс** (по замерам шкалы)" % item["settle_ms"])
        states = item.get("layout_states") or []
        if len(states) > 1:
            add("")
            add("Раскладка СТРАНИЦЫ по замерам (innerWidth×dpr — это и видит "
                "пользователь как «съехали объекты»):")
            add("")
            add("| с, мс | по, мс | innerWidth | dpr |")
            add("|---|---|---|---|")
            for entry in states[:8]:
                add("| %s | %s | %s | %s |" % (entry["ms"], entry["until_ms"] or "—",
                                               entry["state"][0], entry["state"][1]))
        slow_ms = (report.get("slow_settles") or {}).get(tag)
        if slow_ms is not None:
            add("- **Медленный устой** (это и видит пользователь как «съезжало "
                "2–3 секунды»): %s мс" % slow_ms)
        if item.get("max_offset_px"):
            add("- Максимальный уход: %s px (последний кадр не на цели на %s мс)"
                % (item["max_offset_px"], item.get("off_target_until_ms")))
        events = item.get("events") or []
        if events:
            add("")
            add("| мс | время | dx | dy | окно (rect) | цель | причины | страница |")
            add("|---|---|---|---|---|---|---|---|")
            for event in events[:40]:
                page = event.get("page") or {}
                add("| %s | %s | %s | %s | %s | %s | %s | %s |"
                    % (event.get("ms"), str(event.get("time"))[11:23], event.get("dx"),
                       event.get("dy"), event.get("rect"), event.get("target"),
                       event.get("reasons"),
                       "iw=%s dpr=%s" % (page.get("iw"), page.get("dpr"))))
            if len(events) > 40:
                add("")
                add("… ещё %d строк — в `browser-window-timeline.txt`." % (len(events) - 40))
        add("")
    wins = report.get("win_calls") or {}
    add("### Win32-вызовы к окну")
    add("")
    add("- Всего: %s, с отказом: %s, с уходом от цели: %s"
        % (wins.get("total"), wins.get("failed"), wins.get("off_target")))
    reasons = report.get("reasons") or {}
    if reasons:
        add("- Причины расхождений: %s"
            % ", ".join("%s×%s" % (k, v) for k, v in reasons.most_common(10)))
    examples = wins.get("examples") or []
    if examples:
        add("")
        add("| время | вызов | ok | dx | dy | dw | dh | client | ошибка | пояснение |")
        add("|---|---|---|---|---|---|---|---|---|---|")
        for item in examples:
            explain = item.get("explain") or []
            if isinstance(explain, list):
                explain = "; ".join(str(x) for x in explain[:2])
            add("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
                % (str(item.get("time"))[11:23], item.get("name"), item.get("ok"),
                   item.get("dx"), item.get("dy"), item.get("dw"), item.get("dh"),
                   item.get("client"), item.get("error") or item.get("winerror"),
                   str(explain)[:160]))
    add("")
    add("## 3. Экспорт файлов: доказательства")
    if report.get("exports_repeated"):
        add("")
        add("Повторных (одинаковых) проверок того же состояния: **%s** — не "
            "ошибки, а подтверждение, что вложение не менялось."
            % report["exports_repeated"])
    add("")
    exports = report.get("exports") or []
    if not exports:
        add("Проверок экспорта в логе нет: до вложения файлов дело не дошло "
            "(шторка, ожидание промта или страница не готова).")
    else:
        add("| время | этап | вердикт | файлы | чипы чата | поле ввода |")
        add("|---|---|---|---|---|---|")
        for item in exports[-40:]:
            files = item.get("files") or []
            files_text = "; ".join(
                "%s%s" % (f.get("name") or "?",
                          (" (%s Б, sha %s)" % (f.get("bytes"), f.get("sha256_12")))
                          if f.get("exists") else " (НЕТ НА ДИСКЕ)")
                for f in files[:4])
            page = item.get("page") or {}
            chips = item.get("chip_names") or page.get("chip_names") or []
            chips_text = ("%s" % int(page.get("chips") or 0)) if int(page.get("chips") or 0) else (
                "; ".join(str(c) for c in chips[:4]) if chips else "—")
            count = int(item.get("count") or 1)
            add("| %s | %s | **%s**%s | %s | %s | inputs=%s, с файлами=%s |"
                % (str(item.get("time"))[11:23], item.get("stage"), item.get("verdict"),
                   (" ×%d" % count) if count > 1 else "",
                   files_text, chips_text,
                   page.get("file_inputs"), page.get("inputs_with_files")))
            if item.get("verdict_saved") and item["verdict_saved"] != item.get("verdict"):
                add("| | | *(в записи было `%s` — пересчитано по фактам)* | | | |"
                    % item["verdict_saved"])
    add("")
    add("## 4. Поверхность страницы и ошибки")
    add("")
    if report.get("stacks"):
        stack_lines = [l for l in report["stacks"].strip().splitlines() if l.strip()]
        header = stack_lines[0] if stack_lines else "Аварийное завершение процесса"
        add("### ⚠️ Фатальный сбой процесса (`stacks.log`)")
        add("- **Сбой:** `%s`" % header)
        add("- **Диагноз:** процесс аварийно завершён операционной системой (код 3 / 0xc000001d).")
        add("```")
        for line in stack_lines[:25]:
            add(line)
        if len(stack_lines) > 25:
            add("... [ещё %d строк в stacks.log]" % (len(stack_lines) - 25))
        add("```")
        add("")
    surface = report.get("surface") or []
    if surface:
        add("| время | событие | вердикт | ready | composer | files | chips | host |")
        add("|---|---|---|---|---|---|---|---|")
        for item in surface[:30]:
            add("| %s | %s | %s | %s | %s | %s | %s | %s |"
                % (str(item.get("time"))[11:23], item.get("event"), item.get("verdict"),
                   item.get("ready"), item.get("composer"), item.get("files"),
                   item.get("chips"), item.get("host")))
    errors = report.get("errors") or {}
    if errors:
        add("")
        add("Ошибки (топ): %s" % ", ".join("%s×%s" % (k, v) for k, v in errors.most_common(8)))
    warnings = report.get("warnings") or {}
    if warnings:
        add("")
        add("Предупреждения (топ): %s" % ", ".join("%s×%s" % (k, v)
                                                   for k, v in warnings.most_common(8)))
    marks = report.get("marks") or {}
    if marks:
        add("")
        add("Ключевые отметки приложения: %s" % ", ".join("%s×%s" % (k, v)
                                                          for k, v in marks.most_common(12)))
    add("")
    add("## 5. Что это значит")
    defects = report.get("defects") or []
    if defects:
        add("")
        add("Похоже на уже найденные дефекты (`ОШИБКИ.md`):")
        for item in defects:
            add("- **№ %s** — %s (%s)" % (item.get("id"), item.get("title"),
                                          item.get("evidence")))
    for note in _multiply_note(report) + _layout_note(report):
        add("")
        add("- %s" % note)
    add("")
    if reasons:
        for code, count in (report["reasons"]).most_common(6):
            advice = ADVICE.get(code)
            if advice:
                add("- **%s** (×%s): %s" % (code, count, advice))
    if not reasons and any(item.get("deviations") for item in watches.values()):
        add("- Отклонения есть, но коды причин не записались: смотреть "
            "`browser-window-timeline.txt` (возможно, окно двигает сам Chromium).")
    if not reasons and not any(item.get("deviations") for item in watches.values()):
        add("- По замерам окно стояло на цели; если пользователь видит «съезжание», "
            "смотреть `page.*` в шкале — это верстка страницы, а не позиция окна.")
    add("")
    add("## 6. Файлы сессии")
    add("")
    for name in report.get("artifacts") or []:
        add("- `%s`" % name)
    add("")
    return "\n".join(lines)


def analyze_one(directory: Path, out_root: Path):
    data = load_session(directory)
    report = analyze(data)
    target = out_root / directory.name
    target.mkdir(parents=True, exist_ok=True)
    (target / "ANALYSIS.md").write_text(render(report), encoding="utf-8")
    serializable = dict(report)
    serializable["reasons"] = dict(report["reasons"])
    serializable["errors"] = dict(report["errors"])
    serializable["warnings"] = dict(report["warnings"])
    serializable["marks"] = dict(report["marks"])
    (target / "analysis.json").write_text(
        json.dumps(serializable, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    return target, report


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    inbox = Path(args.inbox)
    out_root = Path(args.out)
    if args.list:
        sessions = discover(inbox)
        if not sessions:
            print("В %s нет сессий." % inbox)
        for session in sessions:
            print(session)
        return 0
    if args.session:
        target = Path(args.session)
        if target.suffix.lower() == ".zip":
            print("Распакуйте архив в %s и повторите." % inbox)
            return 2
        sessions = [target] if looks_like_session(target) else list(nested_sessions(target))
    else:
        sessions = discover(inbox)
    if not sessions:
        print("Сессий не найдено. Положите папку логов в %s" % inbox)
        return 1
    for session in sessions:
        try:
            target, report = analyze_one(session, out_root)
        except Exception as exc:  # noqa: BLE001
            print("Ошибка разбора %s: %s" % (session, exc))
            continue
        watches = report.get("watches") or {}
        slow = {k: v.get("settle_ms") for k, v in watches.items()
                if v.get("settle_ms")}
        print("Разобрано: %s" % session)
        print("  отчёт: %s" % (target / "ANALYSIS.md"))
        print("  шкалы: %s" % (", ".join(sorted(watches)) or "нет"))
        print("  устой (мс): %s" % (slow or "нет данных"))
        print("  экспорт: %s" % ", ".join(
            "%s×%s" % (v, c) for v, c in
            Counter(str(e.get("verdict")) for e in report.get("exports") or []).items())
            or "нет проверок")
        print("  причины отклонений: %s" % (dict(report.get("reasons")) or "нет"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
