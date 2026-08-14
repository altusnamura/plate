@echo off
REM Double-click this to start PLATE on Windows.
REM
REM Creates a private Python environment on first run (takes a minute or two),
REM reuses it after that, then starts the app and opens it in your browser.
REM Serves to the local network as well, so your phone can reach it.

setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo First run: setting up. This takes a minute.
    echo.
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo.
        echo Could not create the Python environment.
        echo Install Python 3.11 or newer from https://python.org and try again.
        echo Tick "Add python.exe to PATH" in the installer.
        echo.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
    ".venv\Scripts\python.exe" -m pip install --quiet -r plate\requirements.txt
    if errorlevel 1 (
        echo.
        echo Could not install dependencies. Check your internet connection.
        echo.
        pause
        exit /b 1
    )
    echo Setup complete.
    echo.
)

REM Give the server a moment to bind before the browser asks for the page.
start "" /b cmd /c "timeout /t 3 /nobreak >nul & start http://localhost:8099"

".venv\Scripts\python.exe" run.py --lan

echo.
echo PLATE has stopped.
pause
