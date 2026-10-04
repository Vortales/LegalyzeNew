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
    return data


def first_of(rows, event, key="event"):
    for row in rows:
        if row.get(key) == event:
            return row
    return None


def counter_of(rows, key="event"):
    return Counter(str(row.get(key)) for row in rows if row.get(key))


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
        "surface": [],
        "errors": Counter(),
        "warnings": Counter(),
        "marks": Counter(),
        "artifacts": data["artifacts"],
        "advice": [],
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

    for row in trace:
        kind = str(row.get("kind") or "")
        if kind == "win32.call":
            report["win_calls"]["total"] += 1
            if not row.get("ok"):
                report["win_calls"]["failed"] += 1
            if row.get("verdict") not in (None, "on_target", "unsupported"):
                report["win_calls"]["off_target"] += 1
                if len(report["win_calls"]["examples"]) < 12:
                    report["win_calls"]["examples"].append(row)
            for code in (row.get("reasons") or []):
                report["reasons"][str(code)] += 1
        elif kind == "win32.call.ok":
            report["win_calls"]["total"] += 1
        elif kind == "export.audit":
            report["exports"].append(row)
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
    return report


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
            add("| %s | %s | **%s** | %s | %s | inputs=%s, с файлами=%s |"
                % (str(item.get("time"))[11:23], item.get("stage"), item.get("verdict"),
                   files_text, "; ".join(str(c) for c in chips[:4]),
                   page.get("file_inputs"), page.get("inputs_with_files")))
    add("")
    add("## 4. Поверхность страницы и ошибки")
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
