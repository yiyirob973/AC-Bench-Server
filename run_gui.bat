@echo off
setlocal
cd /d "%~dp0"

rem Prefer the Windows "py" launcher (what double-clicking a .py file uses), then python on PATH.
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 ac_bench_gui.py
    goto done
)
where python >nul 2>nul
if %errorlevel%==0 (
    python ac_bench_gui.py
    goto done
)
echo Python was not found.
echo Install Python 3 from python.org and tick "Add python.exe to PATH" in the installer.
pause
exit /b 1

:done
if errorlevel 1 (
    echo.
    echo The program stopped with an error. The message above says why.
    echo If it says Python was not found and mentions the Microsoft Store, install Python from python.org
    echo or turn off the "python.exe" App execution alias in Windows Settings.
    pause
)
