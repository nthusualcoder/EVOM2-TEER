@echo off
rem Build EVOM2_TEER.exe (PyInstaller options live in release.py)
cd /d "%~dp0"
set PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe
if not exist "%PY%" set PY=python
"%PY%" release.py build
if errorlevel 1 (
  echo.
  echo *** BUILD FAILED - see messages above ***
  pause
  exit /b 1
)
echo.
echo DONE
pause
