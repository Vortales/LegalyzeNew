@echo off
rem Pinned Chrome for Testing -> <app>\browser\chrome.exe
rem Gives every user the SAME engine (deterministic layout + native microphone).
setlocal
pushd "%~dp0.."
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" tools\fetch_chrome.py %*
) else (
  py -3 tools\fetch_chrome.py %* || python tools\fetch_chrome.py %*
)
popd
endlocal
