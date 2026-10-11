@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo No venv found. Run: python -m venv .venv
  exit /b 1
)
".venv\Scripts\python" -m nuitka --onefile --windows-console-mode=disable --enable-plugin=pyqt6 --include-package=requests --include-package=websocket --include-package=cryptography --include-package=fpdf --include-data-file=data/DejaVuSans.ttf=data/DejaVuSans.ttf --windows-icon-from-ico=icon.ico --assume-yes-for-downloads --output-dir=build --output-filename=Legalyze.exe main.py
if errorlevel 1 (
  echo.
  echo BUILD FAILED
  exit /b 1
)
echo.
echo OK: %~dp0build\Legalyze.exe
