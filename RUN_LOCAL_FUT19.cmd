@echo off
setlocal
cd /d "%~dp0"
title FIFA19 Local Server - Backend Preview
where py >nul 2>nul
if not errorlevel 1 (
  py -3 "%~dp0server\localfut19.py"
) else (
  python "%~dp0server\localfut19.py"
)
if errorlevel 1 pause
