@echo off
setlocal EnableExtensions
cd /d "%~dp0"

python -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python and check PATH.
  goto END
)

echo [1/2] pip install...
python -m pip install -q flask waitress pystray pillow pyinstaller
if errorlevel 1 (
  echo [ERROR] pip install failed
  goto END
)

echo [2/2] PyInstaller...
python -m PyInstaller --noconfirm --clean "工厂同步控制台.spec"
if errorlevel 1 (
  echo [ERROR] PyInstaller failed
  goto END
)

echo.
echo OK dist\工厂同步控制台\工厂同步控制台.exe

:END
echo.
pause
exit /b 0
