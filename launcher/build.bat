@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set GCC=
if exist "C:\Tools\llvm-mingw-20241119-ucrt-x86_64\bin\gcc.exe" set "GCC=C:\Tools\llvm-mingw-20241119-ucrt-x86_64\bin\gcc.exe"
if exist "C:\Tools\llvm-mingw-20241119-ucrt-x86_64\bin\windres.exe" set "WINDRES=C:\Tools\llvm-mingw-20241119-ucrt-x86_64\bin\windres.exe"
if "%GCC%"=="" (
  where gcc >nul 2>&1
  if not errorlevel 1 set GCC=gcc
)
if "%WINDRES%"=="" (
  where windres >nul 2>&1
  if not errorlevel 1 set WINDRES=windres
)
if "%GCC%"=="" (
  echo [ERROR] gcc not found. Need llvm-mingw or MinGW-w64.
  exit /b 1
)

if not exist "..\dist" mkdir "..\dist"
if exist launcher.res del /f /q launcher.res >nul 2>&1

if not "%WINDRES%"=="" (
  "%WINDRES%" -I. launcher.rc -O coff -o launcher.res
  if errorlevel 1 (
    echo [ERROR] windres failed
    exit /b 1
  )
)

set RES=
if exist launcher.res set RES=launcher.res

"%GCC%" -mwindows -O2 -s -nostdlib -fno-stack-protector -fno-ident -finput-charset=UTF-8 -Wl,-e,WinMainCRTStartup -o "..\dist\agent_launcher.exe" launcher.c %RES% -lkernel32 -luser32
if errorlevel 1 (
  echo [ERROR] gcc launcher failed
  exit /b 1
)
echo OK dist\agent_launcher.exe
exit /b 0
