@echo off
REM Switch Node — start the hub and this machine's node together.
REM Double-click this file. Two windows open; close them to stop.

setlocal
cd /d "%~dp0"

REM A system-wide proxy (common on Windows with a VPN client) would otherwise
REM capture the hub's own localhost calls and every request fails. Node agents
REM and the hub talk over 127.0.0.1 / the LAN, never through a proxy.
set NO_PROXY=127.0.0.1,localhost,192.168.0.0/16,10.0.0.0/8
set no_proxy=%NO_PROXY%
set PYTHONUNBUFFERED=1

if not exist "config.json" (
  echo config.json is missing. Copying config.example.json ...
  copy /y config.example.json config.json >nul
  echo.
  echo Put your BotFather token in config.json, then run this again.
  pause
  exit /b 1
)

echo Starting hub ...
start "switch-node hub" cmd /k python hub.py

timeout /t 4 /nobreak >nul

echo Starting node for this machine ...
start "switch-node node" cmd /k python node_agent.py --name %COMPUTERNAME% --hub http://127.0.0.1:8787

echo.
echo Both windows are up.
echo   Hub  : http://127.0.0.1:8787/health
echo   Node : %COMPUTERNAME%
echo.
echo Message your bot on Telegram and send /start.
pause
