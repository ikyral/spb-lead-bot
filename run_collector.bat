@echo off
rem Runs the collector with the Python from .venv and appends its output to logs\collector.log.
rem For Windows Task Scheduler: the exit code is passed on (0 done, 1 error, 2 a source refused).
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1

if not exist ".venv\Scripts\python.exe" (
    echo Not found: .venv\Scripts\python.exe. See "Installing the collector" in README.
    exit /b 1
)
if not exist logs mkdir logs

".venv\Scripts\python.exe" collect_cli.py %* >> "logs\collector.log" 2>&1
set EXIT_CODE=%ERRORLEVEL%
endlocal & exit /b %EXIT_CODE%
