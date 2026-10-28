@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 exit /b 1
if exist ".venv\Scripts\python.exe" goto run
echo Run start.bat once to prepare the local Python environment.
set "RESULT=1"
goto finish

:run
set "LANGSMITH_TRACING=false"
".venv\Scripts\python.exe" -X utf8 -m pytest %*
set "RESULT=%ERRORLEVEL%"

:finish
popd
pause
exit /b %RESULT%
