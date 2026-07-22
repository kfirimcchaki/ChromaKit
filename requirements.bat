@echo off
setlocal
cd /d "%~dp0"

python -m pip install --upgrade pip
if errorlevel 1 exit /b %errorlevel%

rem Installs ChromaKit audio DSP dependencies, including Pedalboard.
python -m pip install -r requirements.txt
if errorlevel 1 exit /b %errorlevel%

echo.
echo Requirements installed.

