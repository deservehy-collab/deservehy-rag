@echo off
setlocal
cd /d "%~dp0deploy\langfuse"
docker compose down
if errorlevel 1 (
  echo [ch09] docker compose down FAILED. Check: docker compose -f deploy\langfuse\docker-compose.yml ps
  pause
  exit /b 1
)
echo [ch09] Langfuse stopped. Data volumes are kept.
