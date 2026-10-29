import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_openai import ChatOpenAI
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.context import ContextBudgetError
from app.database import build_database_url, build_engine, build_session_factory
from app.knowledge.embedding import BgeM3Embedder
from app.knowledge.ingest import KnowledgeIngestor
from app.knowledge.mining import ConversationMiner
from app.knowledge.retrieval import KnowledgeRetriever
from app.knowledge.scheduler import MiningScheduler
from app.knowledge.vector_store import MilvusVectorStore
from app.model import build_model
from app.schemas import AfterSales, ChatRequest, ExtractRequest
from app.service import CustomerService, ModelFailure
from app.storage import ConversationStore

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    model: ChatOpenAI | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        try:
            resolved_settings = settings or Settings.from_env()
        except ValidationError:
            raise RuntimeError("模型配置无效，请填写项目 .env 中的地址、模型名、密钥，并检查 token 预算。") from None
        resolved_model = model or build_model(resolved_settings)
        engine = None
        factory = session_factory
        if factory is None:
            engine = build_engine(build_database_url(resolved_settings))
            factory = build_session_factory(engine)
        embedder = BgeM3Embedder(
            resolved_settings.embedding_model,
            device=resolved_settings.embedding_device,
            batch_size=resolved_settings.embedding_batch_size,
        )
        vector_store = MilvusVectorStore(resolved_settings.milvus_uri, resolved_settings.milvus_collection)
        try:
            if embedder.local_model_path() is not None:
                await asyncio.to_thread(embedder.warmup)
        except Exception:
            logger.exception("嵌入模型预热失败，首次检索可能较慢或返回 knowledge_unavailable。")
        knowledge = KnowledgeRetriever(
            factory,
            embedder,
            vector_store,
            candidates=resolved_settings.knowledge_candidates,
            top_k=resolved_settings.knowledge_top_k,
            min_score=resolved_settings.knowledge_min_score,
        )
        scheduler = None
        try:
            application.state.service = CustomerService(
                resolved_settings, resolved_model, ConversationStore(factory), knowledge
            )
            if (
                resolved_settings.knowledge_mining_enabled
                and resolved_settings.knowledge_mining_interval_minutes > 0
            ):
                try:
                    ingestor = await asyncio.to_thread(
                        KnowledgeIngestor,
                        factory,
                        embedder,
                        vector_store,
                        batch_size=resolved_settings.embedding_batch_size,
                    )
                    miner = ConversationMiner(
                        factory,
                        resolved_model,
                        ingestor,
                        embedder,
                        lookback_days=resolved_settings.knowledge_mining_lookback_days,
                        dedupe_threshold=resolved_settings.knowledge_dedupe_threshold,
                    )
                    scheduler = MiningScheduler(
                        miner, resolved_settings.knowledge_mining_interval_minutes
                    )
                    await scheduler.start()
                except Exception:
                    logger.exception("知识挖掘定时任务启动失败，服务继续运行。")
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown()
            vector_store.close()
            if model is None:
                await resolved_model.root_async_client.close()
                resolved_model.root_client.close()
            if engine is not None:
                engine.dispose()

    application = FastAPI(title="电商智能客服 · ch03", lifespan=lifespan)
    web_directory = Path(__file__).resolve().parent / "web"
    application.mount("/assets", StaticFiles(directory=web_directory), name="assets")

    @application.get("/", response_class=FileResponse, include_in_schema=False)
    async def chat_page():
        return FileResponse(web_directory / "index.html")

    @application.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"code": "invalid_request", "message": "请求格式不正确，请检查必填内容、历史角色和字段长度。"})

    @application.exception_handler(ContextBudgetError)
    async def over_budget(request: Request, exc: ContextBudgetError):
        return JSONResponse(status_code=422, content={"code": "context_budget_exceeded", "message": str(exc)})

    @application.exception_handler(ModelFailure)
    async def model_error(request: Request, exc: ModelFailure):
        return JSONResponse(status_code=exc.status_code, content=exc.payload())

    @application.post("/api/chat")
    async def chat(body: ChatRequest, request: Request):
        service = request.app.state.service
        messages = service.prepare_chat(body)

        async def encode_events():
            stream = service.respond(body, messages)
            try:
                async for event, payload in stream:
                    yield f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            finally:
                await stream.aclose()

        return StreamingResponse(encode_events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @application.post("/api/after-sales/extract", response_model=AfterSales)
    async def extract(body: ExtractRequest, request: Request):
        return await request.app.state.service.extract(body)

    return application


app = create_app()
