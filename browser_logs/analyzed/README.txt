Здесь появляются разборы присланных сессий: на каждую — своя папка с
ANALYSIS.md (человеческий отчёт) и analysis.json (машинный).

Делается командой из TestLegalUniversal_v19:
    python3 tools/analyze_browser_logs.py --list
    python3 tools/analyze_browser_logs.py

Содержимое этой папки в Git не попадает: разборы могут содержать пути и имена
файлов с вашей машины. В репозитории остаётся только этот файл-указатель
и образец разбора в ../example/.
