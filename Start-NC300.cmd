@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    python -m venv .venv
    if errorlevel 1 goto :failed
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 goto :failed
)
start "" ".venv\Scripts\pythonw.exe" -m nc300
exit /b 0
:failed
echo NC300 Control could not start. Python 3.11 or newer and the Bluetooth dependency are required.
pause
exit /b 1
