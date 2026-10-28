import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from app.config import Settings
from app.context import ContextBudgetError
from app.model import build_model
from app.schemas import AfterSales, ChatRequest, ExtractRequest
from app.service import CustomerService, ModelFailure


def create_app(settings: Settings | None = None, model: ChatOpenAI | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        try:
            resolved_settings = settings or Settings.from_env()
        except ValidationError:
            raise RuntimeError("模型配置无效，请填写项目 .env 中的地址、模型名、密钥，并检查 token 预算。") from None
        resolved_model = model or build_model(resolved_settings)
        try:
            application.state.service = CustomerService(resolved_settings, resolved_model)
            yield
        finally:
            if model is None:
                await resolved_model.root_async_client.close()
                resolved_model.root_client.close()

    application = FastAPI(title="电商智能客服 · ch01", lifespan=lifespan)
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
            stream = service.stream_chat(messages)
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
