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

rem Standalone distribution (debug/deployment variant): keep ALL files in build\main.dist, not only the EXE.
rem No administrator manifest, no sandbox changes, no runtime algorithm changes.
"%PYTHON%" -m nuitka ^
    --mode=standalone ^
    --msvc=latest ^
    --enable-plugin=pyqt6 ^
    --windows-console-mode=disable ^
    --windows-icon-from-ico=icon.ico ^
    --product-name=Legalyze ^
    --file-description="Legalyze desktop client" ^
    --file-version=1.0.5.0 ^
    --product-version=1.0.5.0 ^
    --include-data-files=data/DejaVuSans.ttf=data/DejaVuSans.ttf ^
    --include-data-files=data/FONT-LICENSE.txt=data/FONT-LICENSE.txt ^
    --output-dir=build ^
    --output-filename=Legalyze.exe ^
    --report=build/compilation-report.xml ^
    main.py
if errorlevel 1 exit /b 1
if not exist "build\main.dist\Legalyze.exe" (
    echo Expected output build\main.dist\Legalyze.exe was not found. Check the Nuitka output.
    exit /b 1
)

echo Output: %~dp0build\main.dist\Legalyze.exe
echo Ship ALL files including QtWebEngineProcess, Qt resources, locales and DLLs.
echo This variant does NOT require an external chromium folder.
exit /b 0
