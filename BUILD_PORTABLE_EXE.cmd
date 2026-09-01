@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "LOG=%~dp0BUILD_DIAGNOSTIC.txt"
>"%LOG%" echo Sector Radar portable app-folder build diagnostic
>>"%LOG%" echo Started: %date% %time%
>>"%LOG%" echo Script: %~f0
>>"%LOG%" echo Folder: %~dp0

echo Sector Radar portable app-folder build is running.
echo This may take several minutes.
echo Diagnostic file: BUILD_DIAGNOSTIC.txt
echo.

call :MAIN >>"%LOG%" 2>&1
set "RC=%errorlevel%"

echo.
if "%RC%"=="0" goto SUCCESS

echo Portable app-folder build failed. Exit code: %RC%
echo The diagnostic file has been created next to this launcher:
echo %LOG%
echo.
type "%LOG%"
echo.
pause
exit /b %RC%

:SUCCESS
echo Portable app-folder build completed successfully.
echo Output: dist\SectorRadar_v4.4.0-alpha.41\
echo Keep the EXE and its _internal folder together.
echo Full build log: %LOG%
pause
exit /b 0

:MAIN
echo Command processor: %ComSpec%
echo PATH: %PATH%

cd /d "%~dp0"
if errorlevel 1 (
  echo ERROR: Cannot enter the application folder.
  exit /b 90
)

if not exist "sector_radar.pyw" (
  echo ERROR: sector_radar.pyw is missing.
  exit /b 91
)

if not exist "sector_radar.manifest" (
  echo ERROR: sector_radar.manifest is missing.
  exit /b 98
)

call :FIND_PYTHON
if defined PYTHON_CMD goto PYTHON_FOUND

echo ERROR: Python was not found in PATH.
echo Install Python 3.11 or newer and enable Add Python to PATH.
exit /b 92

:PYTHON_FOUND
echo Python command: "%PYTHON_CMD%" %PYTHON_PREFIX%
"%PYTHON_CMD%" %PYTHON_PREFIX% -c "import sys; print('Python:', sys.version); print('Executable:', sys.executable)"
if errorlevel 1 exit /b 93

echo Step 1 of 4: Running application self-test...
"%PYTHON_CMD%" %PYTHON_PREFIX% sector_radar.pyw --self-test
if errorlevel 1 exit /b 94

echo Step 2 of 4: Installing or updating PyInstaller...
"%PYTHON_CMD%" %PYTHON_PREFIX% -m pip install --upgrade pyinstaller
if errorlevel 1 exit /b 95

echo Step 3 of 4: Building the portable app folder...
"%PYTHON_CMD%" %PYTHON_PREFIX% -m PyInstaller --noconfirm --clean --windowed --onedir --contents-directory "_internal" --name "SectorRadar_v4.4.0-alpha.41" --manifest "sector_radar.manifest" sector_radar.pyw
if errorlevel 1 exit /b 96

echo Step 4 of 4: Verifying the app folder...
if not exist "dist\SectorRadar_v4.4.0-alpha.41\SectorRadar_v4.4.0-alpha.41.exe" (
  echo ERROR: PyInstaller returned success but the EXE is missing.
  exit /b 97
)
if not exist "dist\SectorRadar_v4.4.0-alpha.41\_internal" (
  echo ERROR: PyInstaller returned success but the _internal runtime is missing.
  exit /b 99
)

echo BUILD_OK: dist\SectorRadar_v4.4.0-alpha.41\
exit /b 0

:FIND_PYTHON
for %%C in (py.exe py.cmd python.exe python3.exe) do (
  for /f "delims=" %%P in ('where %%C 2^>nul') do (
    "%%P" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
    if not errorlevel 1 (
      set "PYTHON_CMD=%%P"
      set "PYTHON_PREFIX="
      exit /b 0
    )
  )
)
exit /b 1
