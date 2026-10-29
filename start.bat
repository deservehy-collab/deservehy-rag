@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 exit /b 1
if exist ".venv\Scripts\python.exe" goto check_dependencies
py -3.12 -m venv .venv
if errorlevel 1 goto failed

:check_dependencies
".venv\Scripts\python.exe" -c "import fastapi, langchain_openai, pydantic_settings, uvicorn, sqlalchemy, pymysql, sentence_transformers, pymilvus, apscheduler, torch" >nul 2>&1
if not errorlevel 1 goto run
".venv\Scripts\python.exe" -m pip install -i https://mirrors.aliyun.com/pypi/simple/ -f https://mirrors.aliyun.com/pytorch-wheels/cu128/ -r requirements.lock.txt
if errorlevel 1 goto failed

:run
if not exist ".env" copy /y ".env.example" ".env" >nul
echo Configure OPENAI_BASE_URL, OPENAI_MODEL, OPENAI_API_KEY and DB_PASSWORD in .env before use.
where docker >nul 2>&1
if errorlevel 1 goto no_docker
docker compose up -d
if errorlevel 1 echo MySQL/Milvus did not start. Chat will report database_unavailable until they are ready.
".venv\Scripts\python.exe" -X utf8 -m scripts.init_db
if errorlevel 1 echo Database init failed. Check Docker and DB_PASSWORD.
if not exist ".cache\models\bge-m3\pytorch_model.bin" goto no_model
".venv\Scripts\python.exe" -X utf8 -m scripts.ingest_knowledge
if errorlevel 1 echo Knowledge ingest failed. If the Milvus schema changed, run: scripts.ingest_knowledge --rebuild --yes
if not exist ".cache\models\bge-reranker-v2-m3\model.safetensors" goto no_reranker
goto run_server

:no_reranker
echo Reranker model missing. Run: .venv\Scripts\modelscope download --model BAAI/bge-reranker-v2-m3 --local_dir .cache\models\bge-reranker-v2-m3
echo Knowledge questions will report knowledge_unavailable until the reranker is ready.
goto run_server

:no_model
echo Embedding model missing. Run: .venv\Scripts\python.exe -m pip install modelscope
echo Then: .venv\Scripts\modelscope download --model BAAI/bge-m3 --local_dir .cache\models\bge-m3
echo Then: .venv\Scripts\python.exe -X utf8 -m scripts.ingest_knowledge
goto run_server

:no_docker
echo Docker not found. Start MySQL on 127.0.0.1:3307 and Milvus on 127.0.0.1:19530, then run scripts.init_db and scripts.ingest_knowledge manually.

:run_server
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
