@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ============================================================================
rem  Legalyze v20 — РЕКОМЕНДУЕМАЯ сборка: standalone (папка), БЕЗ прав админа.
rem  Скорость старта ~0.5-1 с (нет распаковки в %%TEMP%%), стабильность выше,
rem  антивирусы реагируют реже. Раздавать нужно ВСЮ папку build\main.dist.
rem  Полное объяснение каждого ключа — BUILD_NUITKA.md.
rem ============================================================================

set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=%~dp0.venv\Scripts\python.exe"

"%PYTHON%" -c "import sys, struct; assert sys.platform=='win32' and struct.calcsize('P')==8, 'Собирайте на 64-битной Windows 64-битным Python'"
if errorlevel 1 exit /b 1

"%PYTHON%" -m nuitka --version >nul 2>&1
if errorlevel 1 (
    echo Nuitka не установлена. Выполните: .venv\Scripts\python -m pip install -r requirements-build.txt
    exit /b 1
)

"%PYTHON%" -m nuitka ^
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
if errorlevel 1 (
    echo.
    echo СБОРКА НЕ УДАЛАСЬ. Частые причины — BUILD_NUITKA.md § 6:
    echo   * нет Visual Studio Build Tools;  * сборка не из .venv;
    echo   * падение на шаге LTO — уберите ключ --lto=yes и повторите.
    exit /b 1
)

if not exist "build\main.dist\Legalyze.exe" (
    echo Ожидался build\main.dist\Legalyze.exe — его нет. Смотрите вывод Nuitka выше.
    exit /b 1
)

echo.
echo ГОТОВО: %~dp0build\main.dist\Legalyze.exe
echo Раздавайте ВСЮ папку build\main.dist (или её zip), а не только exe.
echo По желанию: положите рядом browser\chrome.exe (tools\fetch_chrome.bat).
echo После запуска проверьте в новой сессии %%APPDATA%%\Legalyze\logs: machine.fingerprint и selfcheck.*
exit /b 0
