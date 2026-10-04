@echo off
chcp 65001 >nul
title WeChat Auto-Reply Bot
setlocal

set "SCRIPT=%~dp0wx_bot.py"
set PYTHONIOENCODING=utf-8

echo ==================================================
echo   WeChat Auto-Reply Bot
echo   - reply to private messages
echo   - reply when @mentioned in groups
echo   Stop: press Ctrl+C, or just close this window
echo ==================================================
echo.

tasklist /FI "IMAGENAME eq Weixin.exe" 2>nul | find /I "Weixin.exe" >nul
if not errorlevel 1 goto wechat_ok
tasklist /FI "IMAGENAME eq WeChat.exe" 2>nul | find /I "WeChat.exe" >nul
if not errorlevel 1 goto wechat_ok
echo [!] WeChat is not running.
echo     Open WeChat, log in, then run this again.
echo.
pause
exit /b 1

:wechat_ok
where py >nul 2>nul
if not errorlevel 1 goto use_py
where python >nul 2>nul
if not errorlevel 1 goto use_python
echo [!] Python not found in PATH.
echo     Install Python 3.9+ from https://www.python.org/downloads/
echo     (remember to tick "Add python.exe to PATH")
echo.
pause
exit /b 1

:use_py
set "PYCMD=py -3"
goto check_deps

:use_python
set "PYCMD=python"
goto check_deps

:check_deps
%PYCMD% -c "import wechatauto" >nul 2>nul
if not errorlevel 1 goto run
echo [!] Dependency missing. Run this once first:
echo         %PYCMD% -m pip install -r "%~dp0requirements.txt"
echo.
pause
exit /b 1

:run
echo Starting ... keep WeChat logged in.
echo.
%PYCMD% -u "%SCRIPT%"
set RC=%ERRORLEVEL%

echo.
if "%RC%"=="1" goto not_started
echo [Bot stopped]
goto done

:not_started
echo [!] Bot did not start.
echo     Most likely another instance is already running - close that window first.

:done
echo.
pause
exit /b 0
