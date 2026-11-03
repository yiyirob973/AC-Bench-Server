@echo off
setlocal
cd /d "%~dp0"
rem Builds dist\AC-Bench.exe (single portable exe). Needs Python 3 with tkinter: pip install pyinstaller
if not exist template_race.ini (
    echo template_race.ini is missing from this folder: it is embedded in the exe.
    pause
    exit /b 1
)
py -3 -m pip install --upgrade pyinstaller
py -3 -m PyInstaller --noconfirm AC-Bench.spec
if errorlevel 1 (
    echo Build failed.
    pause
    exit /b 1
)
echo.
echo Done: dist\AC-Bench.exe
