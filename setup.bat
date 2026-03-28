@echo off
REM ===== Initial Setup Script =====
echo ========================================
echo   Grade Result Automation - Setup
echo ========================================
echo.

cd /d "%~dp0"

REM 1. Create Python virtual environment
echo [1/4] Creating Python virtual environment...
python -m venv venv

REM 2. Install Python packages (use venv pip directly)
echo [2/4] Installing Python packages...
venv\Scripts\pip.exe install -r requirements.txt

REM 3. Install Playwright browser
echo [3/4] Installing Playwright Chromium browser...
venv\Scripts\playwright.exe install chromium

REM 4. Create .env file
echo [4/4] Checking .env file...
if not exist ".env" (
    copy .env.example .env
    echo .env file created. Please edit passwords manually.
) else (
    echo .env file already exists.
)

REM 5. Install Node.js upload API packages
echo.
echo [Extra] Installing Node.js upload API packages...
cd upload-api
call npm install
cd ..

echo.
echo ========================================
echo   Setup Complete!
echo ========================================
echo.
echo Next steps:
echo   1. Open .env file and check/edit passwords.
echo   2. Run run.bat to test.
echo   3. Run setup_scheduler.bat as Admin to enable daily auto-run.
echo.
pause
