@echo off
rem ============================================================
rem  Wireless ADB Tool launcher (pure ASCII on purpose:
rem  Chinese text in a .bat breaks under mixed 936/65001 consoles.
rem  All Chinese UI is printed by the Python program itself.)
rem ============================================================
title ADB Wireless Debug Tool
setlocal
cd /d "%~dp0"

rem --- locate the main python script (the only non-underscore .py) ---
set "PYFILE="
for %%f in (*.py) do (
    echo %%f | findstr /b "_" >nul || set "PYFILE=%%f"
)
if "%PYFILE%"=="" set "PYFILE=*.py"
if "%PYFILE%"=="*.py" (
    echo [ERROR] main python script not found next to this bat.
    pause
    exit /b 1
)

rem --- auto-install missing deps on first run ---
python -c "import qrcode, PIL" 2>nul
if errorlevel 1 (
    echo Installing dependencies: qrcode / Pillow ...
    python -m pip install qrcode pillow --disable-pip-version-check --quiet
)

rem --- run ---
python "%PYFILE%"
if errorlevel 1 pause
endlocal
