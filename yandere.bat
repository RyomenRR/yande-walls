@echo off
REM Windows batch wrapper for yandere-wallpaper
REM Usage: yandere.bat [options]

setlocal enabledelayedexpansion

REM Get script directory
set "SCRIPT_DIR=%~dp0"

REM Load configuration if it exists
if exist "%SCRIPT_DIR%configuration.conf" (
    for /f "tokens=*" %%A in ('type "%SCRIPT_DIR%configuration.conf" ^| findstr /r "^[A-Z_]*="') do (
        set "%%A"
    )
)

REM Run Python script
python "%SCRIPT_DIR%yande.py" %*

endlocal
