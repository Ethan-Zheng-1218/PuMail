@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   PuMail Pack
echo ============================================
echo.
call electron\build.bat
