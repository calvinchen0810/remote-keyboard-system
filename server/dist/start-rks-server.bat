@echo off
setlocal
cd /d "%~dp0"

if not exist "rks-server.exe" (
  echo [ERROR] rks-server.exe not found in %~dp0
  pause
  exit /b 1
)

if "%HOST%"=="" set "HOST=0.0.0.0"
if "%PORT%"=="" set "PORT=8000"
if "%AUTO_CONNECT_SERIAL%"=="" set "AUTO_CONNECT_SERIAL=1"
if "%CAMERA_INDEX%"=="" set "CAMERA_INDEX=0"
if "%JPEG_QUALITY%"=="" set "JPEG_QUALITY=75"

echo ======================================
echo RKS Server EXE Startup
echo HOST=%HOST%
echo PORT=%PORT%
echo AUTO_CONNECT_SERIAL=%AUTO_CONNECT_SERIAL%
echo CAMERA_INDEX=%CAMERA_INDEX%
echo JPEG_QUALITY=%JPEG_QUALITY%
echo ======================================
echo.

"%~dp0rks-server.exe"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo [INFO] rks-server exited with code %EXIT_CODE%
pause
exit /b %EXIT_CODE%
