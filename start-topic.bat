@echo off
setlocal
cd /d "%~dp0"
set HF_ENDPOINT=https://hf-mirror.com
echo Starting topic classifier service: http://127.0.0.1:8933
".venv\Scripts\python.exe" -X utf8 -m app.topics.service --host 127.0.0.1 --port 8933
pause
