Это ПРИМЕР. Он сгенерирован искусственно (не с чьей-либо машины) — только
чтобы показать, что именно появляется в папке сессии и как выглядит
автоматический разбор.

Пересоздать пример в любой момент:
    cd TestLegalUniversal_v19
    python3 tools/example_session.py --tag example-session
    python3 tools/analyze_browser_logs.py

Что смотреть в примере:
  browser-window-timeline.txt  — построчная хронология в миллисекундах:
                                 «съехало (dy=-24) → -48 → -24 → встало на цель»
  browser-trace.txt            — вызовы Win32 и вердикты экспорта
  UNDERSTANDING.md             — готовый человеческий вывод по сессии
  ANALYSIS.md / analysis.json  — то, что читаю я при разборе присланной сессии
  page-code-*.html             — «код страницы» без текста и секретов
  diagnostic.jsonl             — журнал приложения (как в настоящей сессии)
