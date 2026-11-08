@echo off
setlocal

cd /d "%~dp0"

where pwsh >nul 2>nul
if errorlevel 1 (
    echo PowerShell 7 was not found. The command is pwsh.
    echo Please install PowerShell 7 and run this script again.
    pause
    exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found.
    echo Please install Python 3 and run this script again.
    pause
    exit /b 1
)

echo Validating edited images...
echo.

python "%~dp0tools\validate-image-edits.py" %*
set "exitCode=%ERRORLEVEL%"

echo.
if not "%exitCode%"=="0" (
    echo Image validation found invalid files. Exit code: %exitCode%.
    pause
    exit /b %exitCode%
)

echo All edited images passed validation.
pause
exit /b 0
