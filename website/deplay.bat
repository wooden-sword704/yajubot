@echo off
echo ========================================
echo   onaaki one-click firebase Deploy
echo ========================================
echo.

rem  Execute the deployment [deploy.ps1]
powershell -ExecutionPolicy Bypass -File deploy.ps1
if errorlevel 1 (
    echo.
    echo [ERROR] Deploy failed.
    pause
    exit /b 1
)

echo.
echo ========================================
echo   Deploy completed successfully!
echo ========================================
pause
