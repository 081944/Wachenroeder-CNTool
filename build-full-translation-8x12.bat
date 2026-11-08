@echo off
setlocal

cd /d "%~dp0"

where pwsh >nul 2>nul
if errorlevel 1 (
    echo PowerShell 7 was not found. Please install PowerShell 7.
    if not defined WACHEN_BUILD_NO_PAUSE pause
    exit /b 1
)

echo Building the full Chinese translation with the 8x12 font...
echo.
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\build-full-translation.ps1" -FontMode 8x12 %*
set "exitCode=%ERRORLEVEL%"

echo.
if not "%exitCode%"=="0" (
    echo Build failed with exit code %exitCode%.
) else (
    echo Build completed successfully. See the output path printed above.
)
if not defined WACHEN_BUILD_NO_PAUSE pause
exit /b %exitCode%
