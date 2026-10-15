@echo off
REM Dev launcher (no build needed): runs from .venv, opens the UI in the browser.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Missing .venv. Setup:
  echo   python -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -e .
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" launcher.py
