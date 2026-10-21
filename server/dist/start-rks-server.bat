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
if "%CAMERA_WIDTH%"=="" set "CAMERA_WIDTH=1280"
if "%CAMERA_HEIGHT%"=="" set "CAMERA_HEIGHT=720"
if "%JPEG_QUALITY%"=="" set "JPEG_QUALITY=75"

set "TESSERACT_CMD=%~dp0Tesseract-OCR\tesseract.exe"
if not exist "%TESSERACT_CMD%" (
  echo [WARN] Tesseract executable not found: "%TESSERACT_CMD%"
  echo [WARN] OCR will be unavailable. Other RKS functions can still be used.
)
if not exist "%~dp0Tesseract-OCR\tessdata\eng.traineddata" (
  echo [WARN] English OCR data not found: "%~dp0Tesseract-OCR\tessdata\eng.traineddata"
  echo [WARN] OCR requires eng.traineddata. Continuing RKS startup.
)

echo ======================================
echo RKS Server EXE Startup
echo HOST=%HOST%
echo PORT=%PORT%
echo AUTO_CONNECT_SERIAL=%AUTO_CONNECT_SERIAL%
echo CAMERA_INDEX=%CAMERA_INDEX%
echo CAMERA_WIDTH=%CAMERA_WIDTH%  CAMERA_HEIGHT=%CAMERA_HEIGHT%
echo JPEG_QUALITY=%JPEG_QUALITY%
echo TESSERACT_CMD="%TESSERACT_CMD%"
echo ======================================
echo.

"%~dp0rks-server.exe"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo [INFO] rks-server exited with code %EXIT_CODE%
pause
exit /b %EXIT_CODE%
