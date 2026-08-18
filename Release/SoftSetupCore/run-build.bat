@rem pack
cd /d %~dp0
@call makeskinzip.bat runtime

cd /d %~dp0
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0force_user_level.ps1"
if errorlevel 1 (
  echo [ERROR] failed to set RequestExecutionLevel user
  pause
  exit /b 1
)
".\NSIS\makensis.exe" ".\SetupScripts\runtime\setup.nsi"

@pause
