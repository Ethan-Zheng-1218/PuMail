@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."

echo [1/3] Preparing installer artwork...
python scripts\make-installer-assets.py
if errorlevel 1 goto failed

echo [2/3] Packaging Python backend...
python -m PyInstaller packaging\PuMail.spec --noconfirm
if errorlevel 1 goto failed

echo [3/3] Building Windows installer...
pushd electron
call npm ci
if errorlevel 1 (
    popd
    goto failed
)
call npm run pack
set "buildResult=%errorlevel%"
popd
if not "%buildResult%"=="0" goto failed

echo Build complete: PuMail-build\PuMail-Setup.exe
exit /b 0

:failed
echo Build failed. See the error above.
exit /b 1
