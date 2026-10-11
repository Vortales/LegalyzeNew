# Сборка EXE: оптимальный вариант (Nuitka, standalone + доп. файлы)

Документ отвечает на задачу: **собрать самый быстрый, корректный и стабильный
`Legalyze.exe`** — не обязательно одним файлом. Здесь две команды:

1. **Рекомендуемая — `--mode=standalone`** (§ 2): exe + папка файлов рядом.
   Быстрый запуск (~0.5–1 с вместо 2–5 с), меньше ложных срабатываний
   антивирусов, отдельно обновляемый браузер, видимые ошибки при сбое.
2. **Альтернативная — `--onefile`** (§ 4): один самодостаточный exe.
   Оставлена для случаев, когда требование «ровно один файл» важнее скорости.

Всё проверено по `python -m nuitka --help` версии **Nuitka 4.2.2** (актуальная на
дату правки): имена ключей в документе совпадают с реальными. Устаревшие ключи
из старых инструкций (`--windows-disable-console`, `--include-qt-plugins`) в
Nuitka 4.x **не существуют** — их использование даёт ошибку разбора аргументов.

---

## 1. Подготовка (один раз, Windows 10/11 x64)

```bat
cd /d C:\Projects\Legalyze\TestLegalUniversal_v23
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements-build.txt
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -c "import PyQt6, requests, websocket, cryptography, fpdf; print('ok')"
.venv\Scripts\python -m nuitka --version
```

Компилятор: **MSVC** (Visual Studio Build Tools, компонент «Разработка
классических приложений на C++»). Без MSVC Nuitka скачает MinGW64
(`--mingw64`) — работать будет, но MSVC даёт меньший exe и меньше проблем с
QtWebEngine.

Пакеты сборки (`requirements-build.txt`): `Nuitka>=2.8`, `ordered-set`,
`zstandard`.

---

## 2. Рекомендуемая сборка — standalone (быстро и стабильно)

Одна строка, ничего не сокращать:

```bat
.venv\Scripts\python -m nuitka ^
    --mode=standalone ^
    --msvc=latest ^
    --lto=yes ^
    --enable-plugin=pyqt6 ^
    --windows-console-mode=disable ^
    --windows-icon-from-ico=icon.ico ^
    --include-package=requests ^
    --include-package=websocket ^
    --include-package=cryptography ^
    --include-package=fpdf ^
    --include-module=machine_profile ^
    --include-module=browser_trace ^
    --include-module=web_compat ^
    --include-module=diagnostics ^
    --include-module=storage_paths ^
    --include-data-files=data/DejaVuSans.ttf=data/DejaVuSans.ttf ^
    --include-data-files=data/FONT-LICENSE.txt=data/FONT-LICENSE.txt ^
    --include-distribution-metadata=PyQt6 ^
    --include-distribution-metadata=PyQt6-Qt6 ^
    --include-distribution-metadata=PyQt6-WebEngine ^
    --include-distribution-metadata=requests ^
    --include-distribution-metadata=urllib3 ^
    --include-distribution-metadata=websocket-client ^
    --include-distribution-metadata=cryptography ^
    --include-distribution-metadata=fpdf2 ^
    --assume-yes-for-downloads ^
    --product-name=Legalyze ^
    --file-description="Legalyze desktop client" ^
    --company-name=Legalyze ^
    --file-version=1.0.5.0 ^
    --product-version=1.0.5.0 ^
    --copyright="Legalyze" ^
    --report=build/nuitka-report.xml ^
    --output-dir=build ^
    --output-filename=Legalyze.exe ^
    main.py
```

То же самое делает **`build_standalone.bat`** (проверяет 64-битную Windows,
наличие Nuitka и появление результата). Результат — папка:

```
build\main.dist\
    Legalyze.exe                ← запускать этот файл
    python3*.dll, Qt6*.dll      ← рядом (обязательно)
    qtwebengine_resources.pak
    PyQt6\Qt6\plugins\...
    QtWebEngineProcess.exe
    data\DejaVuSans.ttf
    ...                        ← ещё ~50–120 файлов
```

**Раздавать нужно ВСЮ папку `main.dist`** (или её zip), не только exe.

### 2.1. Почему именно так (каждый ключ — зачем)

| Ключ | Что делает | Почему это важно здесь |
|---|---|---|
| `--mode=standalone` | exe + файлы рядом, без распаковки в `%TEMP%` при каждом запуске | **скорость**: старт ~0.5–1 с вместо 2–5 с; **стабильность**: нет `%TEMP%\ONEFILE_*`, который чистит Windows и помечает антивирус |
| `--msvc=latest` | компилятор MSVC | меньше exe, штатная работа QtWebEngine, нет «скачанных» тулчейнов |
| `--lto=yes` | link-time optimization | быстрее и компактнее код. Если сборка падает именно на этом шаге — уберите ключ (см. § 6) |
| `--enable-plugin=pyqt6` | Qt6: плагины платформ, ресурсы, переводы | без него exe стартует и падает. Плагин сам подтягивает `QtWebEngineProcess.exe`, `qtwebengine_resources.pak`, `locales` |
| `--windows-console-mode=disable` | GUI-приложение без чёрного окна | штатный вид; ошибки всё равно видны в логе сессии |
| `--include-package=requests/websocket/cryptography/fpdf` | пакеты с подгрузкой в рантайме | без них exe падает при обращении к серверу и при экспорте PDF |
| `--include-module=machine_profile/browser_trace/web_compat/diagnostics/storage_paths` | наши модули | часть импортируется ВНУТРИ функций (например, `import machine_profile` в `env_fingerprint`); явное указание снимает вопрос «включил ли Nuitka этот модуль» |
| `--include-data-files=...=...` | шрифт и лицензия | PDF-экспорт и юридическая чистота; формат `<откуда>=<куда в сборке>` |
| `--include-distribution-metadata=<пакет>` | `*.dist-info` в сборку | `diagnostics.py` пишет в лог ВЕРСИИ пакетов через `importlib.metadata.version()`. Без этого в exe-логе было бы `not in metadata` — и чужая сессия теряла бы версии. Теперь версии есть и в exe |
| `--assume-yes-for-downloads` | без вопросов при докачке зависимостей | чтобы сборка не «зависала» на запросе |
| метаданные (`--product-name`, `--file-version`, …) | свойства exe | видно версию в свойствах файла; полезно при разборе чужих логов |
| `--report=build/nuitka-report.xml` | отчёт сборки | при «в exe нет модуля X» видно, что Nuitka реально включил |
| `--output-dir=build`, `--output-filename=Legalyze.exe` | куда и как называть | предсказуемый путь |

### 2.2. Чего в команде НЕТ — и почему

| Ключ | Почему нельзя |
|---|---|
| `--windows-uac-admin` | Приложение обязано работать **без прав администратора** (ОШИБКИ.md № 4). Манифест админа ломает запуск у обычного пользователя и провоцирует UAC |
| `--windows-uac-uiaccess` | То же: не требует админа, но помечает приложение «только для UIAccess» — лишний риск |
| `--windows-disable-console` | Ключ УСТАРЕЛ: в Nuitka 4.x его нет, используйте `--windows-console-mode=disable` |
| `--include-qt-plugins=...` | Ключа в Nuitka 4.x нет; плагин `pyqt6` включает нужные Qt-плагины сам |
| `--onefile` (в этой команде) | Распаковка в `%TEMP%` при каждом старте: медленный запуск и эвристика антивирусов. Нужен ровно один файл — см. § 4 |
| `--upx` / плагин `upx` | UPX-сжатие ломает подпись и усиливает ложные срабатывания антивирусов; выигрыш в размере не стоит потерь |
| `--nofollow-import-to=tests,tools` | Экономия минимальна (эти модули и так не импортируются приложением), а риск случайно отрезать нужное — реальный. Не добавляем |

---

## 3. Что делать после компиляции

1. **Проверить на сборочной машине:** `build\main.dist\Legalyze.exe` запускается
   без установленного Python, окно поднимается, страница ИИ грузится.
2. **Проверить, что сработала самопроверка** — в свежей сессии
   `%APPDATA%\Legalyze\logs\<сессия>`:
   * `machine.fingerprint` — паспорт машины есть;
   * `selfcheck.engine`, `selfcheck.native_browser`, `selfcheck.admin_free` — «ок»;
   * `session.start.release_revision = universal-embedded-v23` и
     `packages` — версии пакетов, а не `not in metadata`.
3. **Раздавать:** папку `build\main.dist` целиком (zip). Если используется
   закреплённый браузер — рядом с `Legalyze.exe` положить папку `browser\`
   (`tools\fetch_chrome.bat`), либо дать приложению скачать Chrome for Testing
   при первом запуске (оно умеет это само, без прав администратора).
4. **Подпись кода** (если есть сертификат) — единственный честный способ убрать
   предупреждения антивирусов и SmartScreen:

   ```bat
   signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /a build\main.dist\Legalyze.exe
   ```

   Подписывать нужно **после** сборки; повторная сборка подпись не сохраняет.

---

## 4. Альтернатива — один файл (`--onefile`)

Когда требование «ровно один exe» важнее скорости:

```bat
.venv\Scripts\python -m nuitka ^
    --onefile ^
    --msvc=latest ^
    --enable-plugin=pyqt6 ^
    --windows-console-mode=disable ^
    --include-package=requests ^
    --include-package=websocket ^
    --include-package=cryptography ^
    --include-package=fpdf ^
    --include-module=machine_profile ^
    --include-module=browser_trace ^
    --include-data-file=data/DejaVuSans.ttf=data/DejaVuSans.ttf ^
    --include-data-file=data/FONT-LICENSE.txt=data/FONT-LICENSE.txt ^
    --include-distribution-metadata=PyQt6 ^
    --include-distribution-metadata=PyQt6-Qt6 ^
    --include-distribution-metadata=requests ^
    --include-distribution-metadata=websocket-client ^
    --include-distribution-metadata=cryptography ^
    --include-distribution-metadata=fpdf2 ^
    --windows-icon-from-ico=icon.ico ^
    --assume-yes-for-downloads ^
    --product-name=Legalyze ^
    --file-description="Legalyze desktop client" ^
    --file-version=1.0.5.0 ^
    --product-version=1.0.5.0 ^
    --output-dir=build ^
    --output-filename=Legalyze.exe ^
    main.py
```

Это выполняет `build_onefile.bat`. Цена: первый запуск 2–5 с (распаковка в
`%TEMP%\ONEFILE_*`), из-за чего антивирусы чаще ругаются эвристикой.

Исторический файл `BUILD_ONEFILE.txt` оставлен как есть (в нём подробный разбор
именно onefile-варианта); он относится к v19-синтаксису и не является
рекомендуемым способом сборки v20.

---

## 5. Отладочная сборка (консоль включена)

Когда exe «молча закрывается» — собрать с консолью и увидеть текст ошибки:

```bat
.venv\Scripts\python -m nuitka ^
    --mode=standalone ^
    --msvc=latest ^
    --enable-plugin=pyqt6 ^
    --windows-console-mode=force ^
    --include-package=requests --include-package=websocket --include-package=cryptography --include-package=fpdf ^
    --include-module=machine_profile --include-module=browser_trace ^
    --include-data-files=data/DejaVuSans.ttf=data/DejaVuSans.ttf ^
    --assume-yes-for-downloads ^
    --output-dir=build-debug ^
    --output-filename=Legalyze-debug.exe ^
    main.py
```

---

## 6. Частые ошибки и что делать

| Симптом | Причина и лечение |
|---|---|
| `The C compiler 'cl.exe' ... not found` | Нет Build Tools (§ 1) или сборка идёт не из x64-окружения. Запускать из «x64 Native Tools Command Prompt for VS» |
| `ModuleNotFoundError: PyQt6` при сборке | Команда запущена системным python. Всегда `.venv\Scripts\python -m nuitka ...` |
| Сборка падает на LTO | Убрать `--lto=yes` (шаг оптимизации зависит от версии MSVC). На корректность не влияет |
| exe стартует и сразу закрывается | Собрать отладочный вариант (§ 5) — увидеть текст; затем смотреть `%APPDATA%\Legalyze\logs\<сессия>\diagnostic.jsonl` |
| «Нет модуля websocket / cryptography» | Пропущен `--include-package=...` — взять команду из § 2 целиком |
| В логе `packages: not in metadata` | Пропущены `--include-distribution-metadata=...` (§ 2). Версии пакетов в чужой сессии не восстановить |
| Большой размер папки | Это QtWebEngine (~200–400 МБ с ресурсами). Уменьшать за счёт выкидывания Qt-модулей нельзя: страница ИИ использует `QtWebEngineWidgets`. Компромисс — не включать `PyQt6-Qt6` целиком, но это ломает плагины |
| Антивирус ругается на папку | Эвристика на неподписанный exe. Лечится подписью кода (§ 3.4); UPX не использовать |

---

## 7. Проверка «сборка = источники»

Собранная v20 обязана уметь то же, что исходники. Минимальный чек-лист после
сборки (по логу сессии):

```
machine.fingerprint          — паспорт машины есть, масштаб и мониторы видны
selfcheck.engine             — внешний Chrome или резервный движок (ясно какой)
selfcheck.native_browser     — ок/не ок
selfcheck.admin_free         — права: нет
selfcheck.layout             — раскладка сходится (v20: форма окна, dsf=1.0)
browser.layout_watch         — удержание раскладки записано (если было возмущение)
selfcheck.window_position    — окно стояло на цели (по шкалам)
selfcheck.export             — файлы прикрепились (или точный вердикт)
selfcheck.hotkeys, selfcheck.mic — хоткеи и микрофон
```

Если хотя бы одна строка отсутствует — сборка не та (проверьте
`--include-module=machine_profile --include-module=browser_trace`).
