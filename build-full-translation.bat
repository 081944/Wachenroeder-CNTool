@echo off
setlocal

cd /d "%~dp0"

where pwsh >nul 2>nul
if errorlevel 1 (
    echo PowerShell 7 was not found. The command is pwsh.
    echo Please install PowerShell 7 and run this script again.
    if not defined WACHEN_BUILD_NO_PAUSE pause
    exit /b 1
)

echo Building the full Chinese translation package...
echo.

pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\build-full-translation.ps1" %*
set "exitCode=%ERRORLEVEL%"

echo.
if not "%exitCode%"=="0" (
    echo Build failed with exit code %exitCode%.
    if not defined WACHEN_BUILD_NO_PAUSE pause
    exit /b %exitCode%
)

echo Build completed successfully.
echo See the output path printed above.
if not defined WACHEN_BUILD_NO_PAUSE pause
exit /b 0
