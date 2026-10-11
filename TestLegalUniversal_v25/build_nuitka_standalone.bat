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

echo Starting Standalone Nuitka build for Legalyze (v25)...

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
    --file-description="Legalyze" ^
    --company-name=Legalyze ^
    --file-version=1.0.5.0 ^
    --product-version=1.0.5.0 ^
    --copyright="Legalyze" ^
    --output-dir=build ^
    --output-filename=Legalyze.exe ^
    --report=build/nuitka-report.xml ^
    main.py
if errorlevel 1 exit /b 1

if not exist "build\main.dist\Legalyze.exe" if not exist "build\Legalyze.dist\Legalyze.exe" (
    echo Expected output Legalyze.exe was not found. Check the Nuitka output.
    exit /b 1
)

echo Build complete. Output folder is in build directory.
exit /b 0
