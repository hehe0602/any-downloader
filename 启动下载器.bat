@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

REM ==== Pick a Python interpreter: py -3.13 -> py -3.12 -> py -> python ====
set "PYCMD="
py -3.13 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3.13"
if not defined PYCMD (
    py -3.12 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PYCMD=py -3.12"
)
if not defined PYCMD (
    py -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PYCMD=py"
)
if not defined PYCMD (
    python -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PYCMD=python"
)

if not defined PYCMD (
    echo [ERROR] Python 3.10+ was not found. Please install Python first.
    pause
    exit /b 1
)

REM ==== Check dependencies; install from Tsinghua mirror on first run ====
%PYCMD% -c "import httpx, webview, curl_cffi" >nul 2>nul
if errorlevel 1 (
    echo First run: installing dependencies, please wait...
    %PYCMD% -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed. Check network and retry.
        pause
        exit /b 1
    )
)

REM ==== Launch GUI ====
%PYCMD% main.py
set "EXITCODE=%errorlevel%"
if not "%EXITCODE%"=="0" (
    echo.
    echo [ERROR] Program exited with code %EXITCODE%
    pause
)
endlocal

