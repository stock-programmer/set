@echo off
setlocal
if not exist "%~dp0tools\start_web.ps1" (
  echo Please extract the entire ZIP first, then open this launcher from the extracted folder.
  pause
  exit /b 1
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\start_web.ps1" -Interactive %*
if errorlevel 1 (
  pause
  exit /b 1
)
