@echo off
cd /d "%~dp0"

REM Check for updates from GitHub if running in a Git clone
where git >nul 2>nul
if %errorlevel% equ 0 (
    if exist ".git" (
        echo [Rorishper] Checking for updates from GitHub...
        git fetch --quiet origin main >nul 2>nul
        git pull --quiet --ff-only origin main >nul 2>nul
    )
)

if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 (
        echo Could not create the Python environment. Make sure Python 3 is installed.
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
    echo Could not update pip in the app environment.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Dependency installation failed. Check your internet connection and try again.
    pause
    exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" app.py
