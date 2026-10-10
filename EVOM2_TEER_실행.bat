@echo off
rem Run EVOM2_TEER.py with this PC Python (code lives in "EVOM2 장비 코드")
cd /d "%~dp0EVOM2 장비 코드"
set PYW=%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe
if not exist "%PYW%" set PYW=pythonw
start "" "%PYW%" "EVOM2_TEER.py"
