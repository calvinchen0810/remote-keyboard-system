@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=%~dp0env\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
  echo [ERROR] Python venv not found: %PYTHON_EXE%
  echo Please create venv first under server\env
  pause
  exit /b 1
)

echo [STEP] Checking PyInstaller...
"%PYTHON_EXE%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
  echo [STEP] Installing PyInstaller...
  "%PYTHON_EXE%" -m pip install pyinstaller
  if errorlevel 1 (
    echo [ERROR] Failed to install PyInstaller
    pause
    exit /b 1
  )
)

if not exist "%~dp0rks-server.spec" (
  echo [ERROR] Build spec not found: %~dp0rks-server.spec
  pause
  exit /b 1
)

:choose_work_dir
set "WORK_DIR=%TEMP%\rks-pyinstaller-%RANDOM%-%RANDOM%"
if exist "%WORK_DIR%" goto choose_work_dir
mkdir "%WORK_DIR%"
if errorlevel 1 (
  echo [ERROR] Cannot create build work directory: %WORK_DIR%
  pause
  exit /b 1
)

echo [STEP] Building onefile exe...
echo Work directory: %WORK_DIR%
"%PYTHON_EXE%" -m PyInstaller --noconfirm --workpath "%WORK_DIR%" --distpath "%~dp0dist" "%~dp0rks-server.spec"
if errorlevel 1 (
  echo [ERROR] Build failed
  echo Build diagnostics retained in: %WORK_DIR%
  pause
  exit /b 1
)

echo.
echo [OK] Build complete
echo Output: %~dp0dist\rks-server.exe
echo Starter: %~dp0dist\start-rks-server.bat
echo.
pause
exit /b 0
