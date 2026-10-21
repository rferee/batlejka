@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto install

echo Creating virtual environment in .venv ...
where py >nul 2>nul
if errorlevel 1 (
    python -m venv .venv
) else (
    py -3 -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
    echo Could not create .venv - is Python 3.10+ installed?
    pause
    exit /b 1
)

:install
echo Checking Python packages ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
    echo pip install failed.
    pause
    exit /b 1
)

rem Extra arguments are passed through, e.g.  run.bat --port 9000 --no-browser
echo Starting Batlejka ...
".venv\Scripts\python.exe" -m mocap_preprocess %*
if errorlevel 1 pause
