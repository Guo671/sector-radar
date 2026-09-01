@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "LOG=%~dp0STARTUP_DIAGNOSTIC.txt"
>"%LOG%" echo Sector Radar startup diagnostic
>>"%LOG%" echo Started: %date% %time%
>>"%LOG%" echo Script: %~f0
>>"%LOG%" echo Folder: %~dp0

echo Sector Radar launcher is running.
echo This window will stay open while the app is running.
echo Diagnostic file: STARTUP_DIAGNOSTIC.txt
echo.

call :MAIN >>"%LOG%" 2>&1
set "RC=%errorlevel%"

if "%RC%"=="0" exit /b 0

echo.
echo Sector Radar did not start. Exit code: %RC%
echo The diagnostic file has been created next to this launcher:
echo %LOG%
echo.
type "%LOG%"
echo.
pause
exit /b %RC%

:MAIN
echo Command processor: %ComSpec%
echo PATH: %PATH%

cd /d "%~dp0"
if errorlevel 1 (
  echo ERROR: Cannot enter the application folder.
  exit /b 90
)

if not exist "sector_radar.pyw" (
  echo ERROR: sector_radar.pyw is missing. Extract the complete ZIP first.
  exit /b 91
)

where py.exe >nul 2>&1
if not errorlevel 1 goto RUN_WITH_PY

where python.exe >nul 2>&1
if not errorlevel 1 goto RUN_WITH_PYTHON

echo ERROR: Python was not found in PATH.
echo Install Python 3.11 or newer and enable Add Python to PATH.
exit /b 92

:RUN_WITH_PY
echo Python command: py.exe -3
py.exe -3 -c "import sys; print('Python:', sys.version); print('Executable:', sys.executable)"
if errorlevel 1 exit /b 93
py.exe -3 sector_radar.pyw --self-test
if errorlevel 1 exit /b 94
echo Self-test passed. Starting the main window...
py.exe -3 sector_radar.pyw
exit /b %errorlevel%

:RUN_WITH_PYTHON
echo Python command: python.exe
python.exe -c "import sys; print('Python:', sys.version); print('Executable:', sys.executable)"
if errorlevel 1 exit /b 95
python.exe sector_radar.pyw --self-test
if errorlevel 1 exit /b 96
echo Self-test passed. Starting the main window...
python.exe sector_radar.pyw
exit /b %errorlevel%
