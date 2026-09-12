@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set VER=2.0.7
if not "%~1"=="" set VER=%~1

python -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python and check PATH.
  goto END
)

python -c "import struct,sys; print('[INFO] Win10 Python', sys.version.split()[0], struct.calcsize('P')*8, 'bit')"

echo [1/5] pip install Win10 (mftparser)...
python -m pip install -q "requests" "urllib3" pystray pillow pyinstaller mftparser
if errorlevel 1 (
  echo [ERROR] pip install failed
  echo [ERROR] If mftparser missing: use the same 32/64-bit Python as workshop Agent
  goto END
)

echo [2/5] PyInstaller Win10 agent.exe + _internal ...
python -m PyInstaller --noconfirm --clean "agent.spec"
if errorlevel 1 (
  echo [ERROR] PyInstaller Win10 failed
  goto END
)

echo [3/5] pip + PyInstaller Win7 (Python 3.8-64)...
py -3.8-64 -c "import sys,struct; print('[INFO] Win7 Python', sys.version.split()[0], struct.calcsize('P')*8, 'bit')"
if errorlevel 1 (
  echo [ERROR] py -3.8-64 not found. Install 64-bit Python 3.8 to build Win7 runtime.
  goto END
)
set HTTP_PROXY=
set HTTPS_PROXY=
set http_proxy=
set https_proxy=
py -3.8-64 -m pip install -q "requests" "urllib3" pystray "pillow<11" "pyinstaller>=5.13,<6.12"
if errorlevel 1 (
  echo [WARN] online pip failed, try local _win7_wheels
  py -3.8-64 -m pip install -q --no-index --find-links "_win7_wheels" "requests" "urllib3" pystray "pillow<11" "pyinstaller>=5.13,<6.12"
)
if errorlevel 1 (
  echo [ERROR] Win7 pip install failed
  goto END
)
py -3.8-64 -m PyInstaller --noconfirm --clean "agent_win7.spec"
if errorlevel 1 (
  echo [ERROR] PyInstaller Win7 failed
  goto END
)

echo [4/5] C launcher + assemble dist\agent_setup ...
call "%~dp0launcher\build.bat"
if errorlevel 1 (
  echo [ERROR] C launcher build failed
  goto END
)
python assemble_agent_setup.py
if errorlevel 1 (
  echo [ERROR] assemble agent_setup failed
  goto END
)

echo [5/5] zip packages\agent_%VER%.zip ...
python make_agent_package.py %VER%
if errorlevel 1 (
  echo [ERROR] zip failed
  goto END
)

echo.
echo OK
echo   dist\agent_setup\        手工安装与网页升级同一份（启动器+win10+win7）
echo   packages\agent_%VER%.zip 上传到控制台即可，机台按系统自适应
echo SoftwarePack 请选 dist\agent_setup

:END
echo.
pause
exit /b 0
