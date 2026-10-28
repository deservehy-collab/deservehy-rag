@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 exit /b 1
if exist ".venv\Scripts\python.exe" goto check_dependencies
py -3.12 -m venv .venv
if errorlevel 1 goto failed

:check_dependencies
".venv\Scripts\python.exe" -c "import fastapi, langchain_openai, pydantic_settings, uvicorn" >nul 2>&1
if not errorlevel 1 goto run
".venv\Scripts\python.exe" -m pip install -r requirements.lock.txt
if errorlevel 1 goto failed

:run
if not exist ".env" copy /y ".env.example" ".env" >nul
echo Configure OPENAI_BASE_URL, OPENAI_MODEL and OPENAI_API_KEY in .env before use.
echo Local server: http://127.0.0.1:8000
echo Press Ctrl+C to stop.
".venv\Scripts\python.exe" -X utf8 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
set "RESULT=%ERRORLEVEL%"
goto finish

:failed
set "RESULT=1"
echo Setup failed. Install Python 3.12, then run this file again.

:finish
popd
pause
exit /b %RESULT%
