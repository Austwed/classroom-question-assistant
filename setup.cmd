@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE="
    if not defined PYTHON_EXE if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" set "PYTHON_EXE=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    if not defined PYTHON_EXE where python >nul 2>nul && set "PYTHON_EXE=python"
    if defined PYTHON_EXE (
        "!PYTHON_EXE!" -m venv ".venv" || exit /b 1
    ) else (
        where py >nul 2>nul || (
            echo Python 3.12 was not found. Install Python 3.12, then run setup.cmd again.
            exit /b 1
        )
        py -3 -m venv ".venv" || exit /b 1
    )
)

".venv\Scripts\python.exe" -m pip install --no-cache-dir -r requirements.txt || exit /b 1
".venv\Scripts\python.exe" -c "import sys,numpy as np; sys.path.insert(0,'src'); from engine import LocalASR; from settings import AppParameters; LocalASR().transcribe(np.zeros(16000,dtype=np.float32),AppParameters())" || exit /b 1
".venv\Scripts\python.exe" -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8', download_root='models')" || exit /b 1
echo Setup complete.
