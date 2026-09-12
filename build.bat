@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem 控制端跑在 Win2008 / Win7，必须用 Python 3.8 打包
py -3.8-64 -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] py -3.8-64 not found. Install 64-bit Python 3.8.
  goto END
)
py -3.8-64 -c "import sys,struct; print('[INFO] Python', sys.version.split()[0], struct.calcsize('P')*8, 'bit')"

echo [1/2] pip install (Python 3.8)...
set HTTP_PROXY=
set HTTPS_PROXY=
set http_proxy=
set https_proxy=
py -3.8-64 -m pip install -q "flask<3.1" waitress pystray "pillow<11" "pyinstaller>=5.13,<6.12"
if errorlevel 1 (
  echo [WARN] online pip failed, try local _win7_wheels
  py -3.8-64 -m pip install -q --no-index --find-links "_win7_wheels" "flask<3.1" waitress pystray "pillow<11" "pyinstaller>=5.13,<6.12"
)
if errorlevel 1 (
  echo [ERROR] pip install failed
  goto END
)

echo [2/2] PyInstaller Win7/2008...
py -3.8-64 -m PyInstaller --noconfirm --clean "工厂同步控制台.spec"
if errorlevel 1 (
  echo [ERROR] PyInstaller failed
  goto END
)

echo.
echo OK dist\工厂同步控制台\工厂同步控制台.exe
echo Use this folder on Win2008 / Win7 (Python 3.8 runtime).

:END
echo.
pause
exit /b 0
