@echo off
REM ── build-exes.bat ──────────────────────────────────────────────────────
REM  Builds standalone single-file Windows .exes for both backend services.
REM
REM  Outputs (in app\backend\dist\):
REM    watchdog-backend.exe   - FastAPI server
REM    watchdog-cloud.exe     - cloud relay connector
REM
REM  Run from this directory:
REM    cd C:\WATCH-DOG\app\backend
REM    build-exes.bat
REM
REM  First run takes ~5 min (PyInstaller analyses every dependency).
REM  Subsequent runs are faster because PyInstaller caches imports.
REM ────────────────────────────────────────────────────────────────────────

setlocal
cd /d "%~dp0"

echo.
echo ========================================
echo   WATCH-DOG backend - PyInstaller build
echo ========================================
echo.

REM ── Step 1: ensure PyInstaller is installed ─────────────────────────────
where pyinstaller >nul 2>&1
if errorlevel 1 (
    echo [setup] PyInstaller not found - installing...
    pip install pyinstaller
    if errorlevel 1 (
        echo [ERROR] pip install pyinstaller failed.
        pause
        exit /b 1
    )
)

REM ── Step 2: clean old builds so stale modules don't sneak in ────────────
if exist build      rmdir /s /q build
if exist dist       rmdir /s /q dist
if exist __pycache__ rmdir /s /q __pycache__

REM ── Step 3: build the backend exe ───────────────────────────────────────
echo.
echo [1/2] Building watchdog-backend.exe ...
echo       (this takes a few minutes the first time)
echo.
pyinstaller backend.spec --clean --noconfirm
if errorlevel 1 (
    echo.
    echo [ERROR] backend build failed - check the messages above.
    pause
    exit /b 1
)

REM ── Step 4: build the cloud connector exe ───────────────────────────────
echo.
echo [2/2] Building watchdog-cloud.exe ...
echo.
pyinstaller cloud.spec --clean --noconfirm
if errorlevel 1 (
    echo.
    echo [ERROR] cloud build failed - check the messages above.
    pause
    exit /b 1
)

REM ── Done ────────────────────────────────────────────────────────────────
echo.
echo ========================================
echo   Build complete
echo ========================================
echo.
echo Output:
dir /b dist\*.exe 2>nul
echo.
echo Test the backend exe before bundling:
echo   dist\watchdog-backend.exe
echo.
echo Then move to the frontend and run:
echo   cd ..\frontend
echo   npm run dist:win
echo.

pause
endlocal
