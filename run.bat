@echo off
REM ===== Run Grade Automation =====
cd /d "%~dp0"

echo [%date% %time%] Starting grade automation...

REM Use venv python directly if exists, otherwise system python
if exist "venv\Scripts\python.exe" (
    echo Using venv python...
    venv\Scripts\python.exe main.py
) else (
    echo Venv not found. Run setup.bat first, or installing to system python...
    pip install -r requirements.txt
    python main.py
)

echo [%date% %time%] Done!
pause
