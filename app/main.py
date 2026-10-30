import asyncio
import json
import logging
import sqlite3
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Query, Request
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_openai import ChatOpenAI
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.agent.guards import DB_FAILURE_MESSAGE
from app.config import Settings
from app.context import ContextBudget, ContextBudgetError
from app.database import build_database_url, build_engine, build_session_factory
from app.knowledge.embedding import BgeM3Embedder
from app.knowledge.ingest import KnowledgeIngestor
from app.knowledge.mining import ConversationMiner
from app.knowledge.pool import LowConfidencePool
from app.knowledge.rerank import BgeReranker
from app.knowledge.retrieval import KnowledgeRetriever
from app.knowledge.rewrite import QueryRewriter
from app.knowledge.scheduler import MiningScheduler
from app.knowledge.selfcheck import EvidenceSelfCheck
from app.knowledge.vector_store import MilvusVectorStore
from app.logging_config import configure_file_logging
from app.model import build_model
from app.schemas import AfterSales, ChatRequest, ExtractRequest, RefundRequest, TicketRequest
from app.service import CustomerService, ModelFailure
from app.storage import ConversationStore
from app.tools import ToolExecutor, make_tools
from app.workflow.graph import open_checkpointer

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    model: ChatOpenAI | None = None,
    session_factory: sessionmaker[Session] | None = None,
    classifier=None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        try:
            resolved_settings = settings or Settings.from_env()
        except ValidationError:
            raise RuntimeError("模型配置无效，请填写项目 .env 中的地址、模型名、密钥，并检查 token 预算。") from None
        resolved_model = model or build_model(resolved_settings)
        configure_file_logging(resolved_settings.log_file)
        problem = ContextBudget.from_settings(resolved_settings).startup_problem()
        if problem:
            logger.error("启动自检：%s", problem)
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
        reranker = BgeReranker(
            resolved_settings.knowledge_rerank_model,
            device=resolved_settings.knowledge_rerank_device,
            batch_size=resolved_settings.knowledge_rerank_batch_size,
        )
        # 重排模型约 2.3GB，和嵌入模型同时常驻会挤压 6GB 显存；改为首次 FAQ 检索时按需加载。
        knowledge = KnowledgeRetriever(
            factory,
            embedder,
            vector_store,
            candidates=resolved_settings.knowledge_candidates,
            top_k=resolved_settings.knowledge_top_k,
            rrf_k=resolved_settings.knowledge_rrf_k,
            rewrite=QueryRewriter(resolved_model, enabled=resolved_settings.knowledge_query_rewrite),
            reranker=reranker,
            strategy=resolved_settings.knowledge_strategy,
            rerank_min_score=resolved_settings.knowledge_rerank_min_score,
        )
        scheduler = None
        async with AsyncExitStack() as stack:
            checkpointer = await stack.enter_async_context(
                open_checkpointer(resolved_settings.workflow_checkpoint_path)
            )
            try:
                application.state.service = CustomerService(
                    resolved_settings,
                    resolved_model,
                    ConversationStore(factory),
                    knowledge,
                    pool=LowConfidencePool(factory),
                    selfcheck=EvidenceSelfCheck(
                        resolved_model, enabled=resolved_settings.knowledge_self_check
                    ),
                    classifier=classifier,
                    checkpointer=checkpointer,
                )
                application.state.workflow = application.state.service.workflow
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
                service = getattr(application.state, "service", None)
                if service is not None and service.summary is not None:
                    # 后台摘要可能还在写库，先排空再关连接池，免得任务落在已 dispose 的 engine 上。
                    await service.summary.wait()
                vector_store.close()
                if model is None:
                    await resolved_model.root_async_client.close()
                    resolved_model.root_client.close()
                if engine is not None:
                    engine.dispose()

    application = FastAPI(title="电商智能客服 · ch07", lifespan=lifespan)
    # 检查点回写失败时记住 (会话, 轮次) → 工单号，同一进程内重试不会再建第二张；
    # 多进程部署需要数据库级幂等键，教学项目按单进程。
    application.state.ticket_ids: dict[tuple[int, int], str] = {}
    application.state.refund_ids: dict[tuple[int, int], str] = {}
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
        service.preflight(body)

        async def encode_events():
            stream = service.respond(body)
            try:
                async for event, payload in stream:
                    yield f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            finally:
                await stream.aclose()

        return StreamingResponse(encode_events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @application.post("/api/tickets")
    async def create_ticket(body: TicketRequest, request: Request):
        service = request.app.state.service
        workflow = request.app.state.workflow
        store = service.store
        try:
            owner = await asyncio.to_thread(store.owner, body.conversation_id)
        except (SQLAlchemyError, AttributeError):
            raise ModelFailure("database_unavailable", DB_FAILURE_MESSAGE, 503) from None
        if owner != body.client_id:
            raise ModelFailure("forbidden", "无权操作该会话。", 403)

        cache_key = (body.conversation_id, body.turn)

        # 与 /api/chat 共用同一把会话锁：工单读到 offer 后，新一轮 coref 不能插进来把它清掉，
        # 回写也不会复活旧 offer。
        async with workflow.lock(body.conversation_id):
            try:
                snapshot = await workflow.get_state(body.conversation_id)
            except (SQLAlchemyError, sqlite3.Error):
                raise ModelFailure("database_unavailable", DB_FAILURE_MESSAGE, 503) from None
            offer = (snapshot.values or {}).get("offer")
            if not offer or offer.get("turn") != body.turn or "ticket" not in (offer.get("actions") or []):
                raise ModelFailure("no_pending_offer", "这个操作已经失效，请重新提问。", 409)
            if offer.get("ticket_no"):
                return {"ticket_no": offer["ticket_no"], "status": "待处理"}
            cached = request.app.state.ticket_ids.get(cache_key)
            if cached:
                return {"ticket_no": cached, "status": "待处理"}

            ticket = offer.get("ticket") or {}
            description = (ticket.get("description") or f"会话 {body.conversation_id} 用户反馈")[:500]
            if len(description) < 5:
                description = (description + "（用户提交工单）")[:500]
            tools = make_tools(store, body.conversation_id, workflow.knowledge)
            executor = ToolExecutor(
                tools, timeout=service.settings.tool_timeout_seconds,
                attempts=service.settings.tool_max_attempts,
                timeouts={"query_faq": service.settings.knowledge_tool_timeout_seconds},
            )
            outcome = await executor.execute(
                "create_ticket", {"description": description,
                                  "ticket_type": ticket.get("type") or "售后"})
            if not outcome.ok:
                logger.warning("工单创建失败：%s", outcome.code)
                raise ModelFailure("ticket_failed", "工单暂时创建失败，请稍后重试。", 502)
            try:
                ticket_no = json.loads(outcome.content)["data"]["ticket_no"]
            except (TypeError, ValueError, KeyError):
                logger.warning("工单工具返回了无法解析的结果。")
                raise ModelFailure("ticket_failed", "工单暂时创建失败，请稍后重试。", 502) from None
            request.app.state.ticket_ids[cache_key] = ticket_no
            try:
                # 工单已经落库，回写失败不能让用户以为没建成；只影响下次点击时走缓存还是查检查点。
                await workflow.update_state(body.conversation_id,
                                            {"offer": {**offer, "ticket_no": ticket_no}})
            except (SQLAlchemyError, sqlite3.Error):
                logger.warning("工单 %s 已创建，但写回检查点失败。", ticket_no, exc_info=True)
            return {"ticket_no": ticket_no, "status": "待处理"}

    @application.post("/api/refunds")
    async def create_refund(body: RefundRequest, request: Request):
        service = request.app.state.service
        workflow = request.app.state.workflow
        store = service.store
        try:
            owner = await asyncio.to_thread(store.owner, body.conversation_id)
        except (SQLAlchemyError, AttributeError):
            raise ModelFailure("database_unavailable", DB_FAILURE_MESSAGE, 503) from None
        if owner != body.client_id:
            raise ModelFailure("forbidden", "无权操作该会话。", 403)

        cache_key = (body.conversation_id, body.turn)
        # 和 /api/tickets 同一条路径：锁住会话，避免新聊天清掉 offer 时退款单还按旧 offer 建。
        async with workflow.lock(body.conversation_id):
            try:
                snapshot = await workflow.get_state(body.conversation_id)
            except (SQLAlchemyError, sqlite3.Error):
                raise ModelFailure("database_unavailable", DB_FAILURE_MESSAGE, 503) from None
            offer = (snapshot.values or {}).get("offer") or {}
            refund = offer.get("refund") or {}
            if (offer.get("turn") != body.turn or not refund
                    or "refund" not in (offer.get("actions") or [])):
                raise ModelFailure("no_pending_offer", "这个操作已经失效，请重新提问。", 409)
            if offer.get("refund_no"):
                return {"refund_no": offer["refund_no"], "status": "待处理"}
            cached = request.app.state.refund_ids.get(cache_key)
            if cached:
                return {"refund_no": cached, "status": "待处理"}

            description = f"退款申请：订单 {refund.get('order_id')}｜原因：{body.reason}"
            if body.note:
                description += f"｜补充：{body.note}"
            tools = make_tools(store, body.conversation_id, workflow.knowledge)
            executor = ToolExecutor(
                tools, timeout=service.settings.tool_timeout_seconds,
                attempts=service.settings.tool_max_attempts,
                timeouts={"query_faq": service.settings.knowledge_tool_timeout_seconds},
            )
            outcome = await executor.execute(
                "create_ticket", {"description": description[:500], "ticket_type": "售后"})
            if not outcome.ok:
                logger.warning("退款申请创建失败：%s", outcome.code)
                raise ModelFailure("ticket_failed", "退款申请暂时提交失败，请稍后重试。", 502)
            try:
                refund_no = json.loads(outcome.content)["data"]["ticket_no"]
            except (TypeError, ValueError, KeyError):
                logger.warning("工单工具返回了无法解析的结果。")
                raise ModelFailure("ticket_failed", "退款申请暂时提交失败，请稍后重试。", 502) from None
            request.app.state.refund_ids[cache_key] = refund_no
            try:
                await workflow.update_state(body.conversation_id,
                                            {"offer": {**offer, "refund_no": refund_no}})
            except (SQLAlchemyError, sqlite3.Error):
                logger.warning("退款申请 %s 已创建，但写回检查点失败。", refund_no, exc_info=True)
            return {"refund_no": refund_no, "status": "待处理"}

    @application.get("/api/conversations")
    async def list_conversations(
        request: Request,
        client_id: Annotated[str, Query(min_length=8, max_length=64, pattern=r"[A-Za-z0-9_-]+")],
    ):
        store = request.app.state.service.store
        try:
            items = await asyncio.to_thread(store.list_conversations, client_id)
        except (SQLAlchemyError, sqlite3.Error):
            raise ModelFailure("database_unavailable", DB_FAILURE_MESSAGE, 503) from None
        return {"items": items}

    @application.get("/api/conversations/{conversation_id}/messages")
    async def conversation_messages(
        request: Request,
        conversation_id: Annotated[int, PathParam(ge=1, le=2 ** 63 - 1)],
        client_id: Annotated[str, Query(min_length=8, max_length=64, pattern=r"[A-Za-z0-9_-]+")],
    ):
        store = request.app.state.service.store
        try:
            rows = await asyncio.to_thread(store.conversation_messages, conversation_id, client_id)
        except (SQLAlchemyError, sqlite3.Error):
            raise ModelFailure("database_unavailable", DB_FAILURE_MESSAGE, 503) from None
        # 会话不存在也回 403，避免把别人的会话 id 探测出来。
        if rows is None:
            raise ModelFailure("forbidden", "无权访问该会话。", 403)
        return {"conversation_id": conversation_id, "items": rows}

    @application.post("/api/after-sales/extract", response_model=AfterSales)
    async def extract(body: ExtractRequest, request: Request):
        return await request.app.state.service.extract(body)

    return application


app = create_app()
