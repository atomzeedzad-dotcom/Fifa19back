@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 "%~dp0Fifaback19Launcher.py"
) else (
  python "%~dp0Fifaback19Launcher.py"
)
if errorlevel 1 (
  echo Requires Python 3.10 or newer with tkinter. See README.md.
  pause
)
