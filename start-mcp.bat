@echo off
setlocal
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo .venv not found. Create it first: python -m venv .venv
  pause
  exit /b 1
)
start "mcp-logistics" "%PY%" -m mcp_servers.logistics_server
start "mcp-aftersale" "%PY%" -m mcp_servers.aftersale_server
echo MCP servers started on 8931 and 8932. Close these windows to stop.
"%SystemRoot%\System32\ping.exe" -n 4 127.0.0.1 >nul
endlocal
