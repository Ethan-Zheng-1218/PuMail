@echo off
chcp 65001 >nul
echo ============================================
echo   PuMail Build Script
echo ============================================
echo.

:: 1. Prepare icons
echo [Step 1/4] Preparing icons...
copy /Y cat.ico electron\build\icon.ico >nul
python -c "from PIL import Image; img=Image.open('cat.ico'); img=img.resize((512,512) if img.size!=(512,512) else img.size, Image.LANCZOS); img.save('electron/build/icon.png','PNG')"
echo   icon.ico + icon.png ready
echo.

:: 2. Generate installer bitmaps
echo [Step 2/4] Generating installer images...
python electron\build\_make_bitmaps.py
echo.

:: 3. PyInstaller backend
echo [Step 3/4] PyInstaller packaging...
cd /d "%~dp0.."
python -m PyInstaller PuMail.spec --noconfirm
if errorlevel 1 (
    echo ERROR: PyInstaller failed
    pause
    exit /b 1
)
echo.

:: 4. electron-builder installer
echo [Step 4/4] electron-builder packaging...
set PATH=C:\Program Files\nodejs;%PATH%
cd electron
call npx electron-builder --win nsis
if errorlevel 1 (
    echo ERROR: electron-builder failed
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Build complete!
echo   Output: ..\PuMail-build\PuMail-Setup.exe
echo ============================================
pause
