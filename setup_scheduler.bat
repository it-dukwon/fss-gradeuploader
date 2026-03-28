@echo off
echo ========================================
echo   Grade Uploader - Scheduler Setup
echo ========================================
echo.

set "SCRIPT_DIR=%~dp0"

REM Read SCHEDULE_TIME from .env file
set "RUN_TIME=08:00"
for /f "usebackq tokens=1,* delims==" %%A in ("%SCRIPT_DIR%.env") do (
    if "%%A"=="SCHEDULE_TIME" set "RUN_TIME=%%B"
)
echo Schedule time: %RUN_TIME%
echo.

set "TASK_NAME=FSS_GradeUploader"
set "RUN_BAT=%SCRIPT_DIR%run.bat"

REM Delete existing task (ignore error if not exists)
schtasks /delete /tn "%TASK_NAME%" /f >/dev/null 2>&1

REM Create daily scheduled task
schtasks /create /tn "%TASK_NAME%" /tr "\"%RUN_BAT%\"" /sc daily /st %RUN_TIME% /f

if %errorlevel% equ 0 (
    echo.
    echo ========================================
    echo   Scheduler registered successfully!
    echo   Task: %TASK_NAME%
    echo   Time: %RUN_TIME% daily
    echo ========================================
    echo.
    echo To change time: edit SCHEDULE_TIME in .env
    echo Then run this script again.
) else (
    echo.
    echo [ERROR] Failed to register scheduler.
    echo Try running as Administrator.
)
echo.
pause
