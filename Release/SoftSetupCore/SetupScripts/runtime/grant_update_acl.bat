@echo off
setlocal
set "DIR=%~1"
if "%DIR%"=="" exit /b 1
if not exist "%DIR%" exit /b 1
icacls "%DIR%" /grant *S-1-5-32-545:(OI)(CI)M /T /C /Q
icacls "%DIR%" /grant *S-1-5-11:(OI)(CI)M /T /C /Q
exit /b 0
