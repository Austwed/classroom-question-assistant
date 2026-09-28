@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    call setup.cmd || exit /b 1
)
".venv\Scripts\python.exe" "src\app.py"
if errorlevel 1 pause
