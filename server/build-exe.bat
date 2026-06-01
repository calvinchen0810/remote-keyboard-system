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

echo [STEP] Building onefile exe...
"%PYTHON_EXE%" -m PyInstaller --noconfirm --clean --onefile --name rks-server --add-data "static;static" main.py
if errorlevel 1 (
  echo [ERROR] Build failed
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
