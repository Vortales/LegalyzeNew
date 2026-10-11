@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=%~dp0.venv\Scripts\python.exe"

"%PYTHON%" -c "import sys, struct; assert sys.platform == 'win32' and struct.calcsize('P') == 8, 'Build on 64-bit Windows with 64-bit Python'"
if errorlevel 1 exit /b 1
"%PYTHON%" -m nuitka --version
if errorlevel 1 (
    echo Install requirements-build.txt first. See README.md.
    exit /b 1
)

rem ONEFILE release build. Syntax is the confirmed reference command from
rem release_reference\BUILD_COMMAND.txt, plus the font license and file version
rem metadata. No administrator manifest, no sandbox changes, no logic changes.
"%PYTHON%" -m nuitka ^
    --onefile ^
    --windows-console-mode=disable ^
    --enable-plugin=pyqt6 ^
    --include-package=requests ^
    --include-package=websocket ^
    --include-package=cryptography ^
    --include-package=fpdf ^
    --include-data-file=data/DejaVuSans.ttf=data/DejaVuSans.ttf ^
    --include-data-file=data/FONT-LICENSE.txt=data/FONT-LICENSE.txt ^
    --windows-icon-from-ico=icon.ico ^
    --assume-yes-for-downloads ^
    --product-name=Legalyze ^
    --file-description="Legalyze desktop client" ^
    --file-version=1.0.5.0 ^
    --product-version=1.0.5.0 ^
    --output-dir=build ^
    --output-filename=Legalyze.exe ^
    main.py
if errorlevel 1 exit /b 1
if not exist "build\Legalyze.exe" (
    echo Expected output build\Legalyze.exe was not found. Check the Nuitka output.
    exit /b 1
)

echo Output: %~dp0build\Legalyze.exe
echo ONEFILE: a single self-contained EXE. No python/chromium folder needed.
echo After-build steps are in README.md, section "Сборка EXE".
exit /b 0
