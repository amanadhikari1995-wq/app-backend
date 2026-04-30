@echo off
cd /d "%~dp0"
title WATCHDOG Backend

echo  Installing / checking Python packages...
echo  ^(First run installs ccxt which is large - please wait^)
echo.
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo  [ERROR] pip install failed.
    echo  Try running:  pip install -r requirements.txt
    pause & exit /b 1
)
echo.
echo  [OK] Packages ready. Starting backend on http://localhost:8000 ...
echo.

:: Start the cloud connector in a separate window
start "WATCHDOG Cloud Connector" cmd /k "cd /d "%~dp0" && echo. && echo  ========================================== && echo   WATCH-DOG Cloud Connector && echo   Connecting to watchdogbot.cloud ... && echo  ========================================== && echo. && python sdk/wd_cloud.py"

:: Start the backend (this window)
python -m uvicorn app.main:app --reload --reload-dir app --host 0.0.0.0 --port 8000
pause
