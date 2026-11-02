@echo off
setlocal
cd /d "%~dp0deploy\langfuse"
if not exist .env (
  echo [ch09] Missing deploy\langfuse\.env: copy .env.example, fill in every credential, then run this file again.
  pause
  exit /b 1
)
docker compose up -d
if errorlevel 1 (
  echo [ch09] docker compose up -d FAILED: the containers did not start, so waiting for health would be pointless.
  echo        See what is wrong with: docker compose -f deploy\langfuse\docker-compose.yml ps
  echo        Then read the logs:     docker compose -f deploy\langfuse\docker-compose.yml logs langfuse-web clickhouse redis
  pause
  exit /b 1
)
echo [ch09] Waiting for Langfuse web to become ready, up to 180 seconds...
set /a tries=0
:wait
curl -s -m 5 http://127.0.0.1:3000/api/public/health | findstr OK >nul
if %errorlevel%==0 goto ready
set /a tries+=1
if %tries% geq 60 (
  echo [ch09] Not ready within 180 seconds. Check: docker compose -f deploy\langfuse\docker-compose.yml ps
  echo        Then: logs langfuse-web / logs clickhouse.
  echo        If ClickHouse was OOM-killed, lower its mem_limit to 1024m once; if it still fails, stop and ask the user.
  echo        Do NOT switch to Langfuse Cloud and do NOT drop ClickHouse.
  pause
  exit /b 1
)
rem ping is used as the delay: timeout returns instantly when stdin is not a console.
ping -n 4 127.0.0.1 >nul
goto wait
:ready
echo [ch09] Ready. UI http://127.0.0.1:3000 - account is LANGFUSE_INIT_USER_EMAIL in deploy\langfuse\.env
set "PRESET_KEY_OK=0"
findstr /b /c:"LANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-lf-" ".env" >nul
if not errorlevel 1 set "PRESET_KEY_OK=1"
findstr /b /c:"LANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-" ".env" >nul
if errorlevel 1 set "PRESET_KEY_OK=0"
if "%PRESET_KEY_OK%"=="1" echo [ch09] Preset project keys verified: pk-lf-/sk-lf- are present in deploy\langfuse\.env.
if "%PRESET_KEY_OK%"=="0" echo [ch09] WARNING: deploy\langfuse\.env has no pk-lf-/sk-lf- preset key, so no project credential exists.
if "%PRESET_KEY_OK%"=="0" echo        Compose only warns on an undefined ${VAR} and silently substitutes an empty string,
if "%PRESET_KEY_OK%"=="0" echo        which is why this stack can look healthy while the app side has nothing to use.
set "APP_KEY_OK=0"
findstr /r /c:"^LANGFUSE_PUBLIC_KEY=." "%~dp0.env" >nul 2>&1
if not errorlevel 1 set "APP_KEY_OK=1"
if "%APP_KEY_OK%"=="1" echo [ch09] Root .env already has LANGFUSE_PUBLIC_KEY set.
if "%APP_KEY_OK%"=="0" echo [ch09] WARNING: no pk-lf- in the root .env, the application gets no credential and ch09 T2 will fail with HTTP 401.
if "%APP_KEY_OK%"=="0" echo [ch09] Copy LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY from deploy\langfuse\.env into the root .env, set LANGFUSE_ENABLED=true.
pause
