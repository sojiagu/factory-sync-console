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

echo [1/3] pip install...
python -m pip install -q "requests" "urllib3" pystray pillow pyinstaller
if errorlevel 1 (
  echo [ERROR] pip install failed
  goto END
)

echo [2/3] PyInstaller agent.exe + _internal ...
python -m PyInstaller --noconfirm --clean "agent.spec"
if errorlevel 1 (
  echo [ERROR] PyInstaller failed
  goto END
)

echo [3/3] zip packages\agent_%VER%.zip ...
python make_agent_package.py %VER%
if errorlevel 1 (
  echo [ERROR] zip failed
  goto END
)

echo.
echo OK
echo   dist\agent\agent.exe
echo   packages\agent_%VER%.zip
echo Copy dist\agent\ to %%LOCALAPPDATA%%\agent\
echo Then use web page to push upgrade.

:END
echo.
pause
exit /b 0
