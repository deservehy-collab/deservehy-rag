"""Langfuse 观测（旁路）。失败一律降级为「没有观测」，不许影响回复。

`langfuse` 的 import 全在函数内：没装 langfuse 的环境（只跑单测）也要能启动应用。
intent 落每轮根 observation，不落 trace 级（探针实测，见 dev-notes 阶段 4）。
"""

import logging
import os
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

INTENT_TAG_PREFIX = "intent:"

# 本轮「根 observation」的 span。探针实验 1：图内 ambient span 是 ASGI 的 POST /api/chat，
# 事件管线只认 scope.name=langfuse-sdk 的 span ⇒ 写在 ambient 上等于写进空气。
# 所以 turn_trace 把自己建的根 span 放进这个 contextvar，图内节点**只**用它：ambient 兜底
# 已在评审修复轮 1 删掉（拿不到根 span 就返回 False，不写 ambient）。
_turn_root: ContextVar[Any] = ContextVar("ch09_turn_root_span", default=None)


def build_client(settings: Any) -> Any | None:
    """构造 Langfuse 客户端。返回 `None` = 观测关闭；三层降级，任何情况下都不抛。

    名字从 `build_handler` 改过来的：handler 必须在每轮 trace_id 定了之后才建得出来
    （`CallbackHandler(trace_context={"trace_id": …})`），这里只能给客户端。
    """
    if not settings.langfuse_enabled:
        return None
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        logger.warning("LANGFUSE_ENABLED=true 但 public/secret key 不全，观测关闭。")
        return None
    try:
        os.environ["LANGFUSE_HOST"] = settings.langfuse_host
        os.environ["LANGFUSE_PUBLIC_KEY"] = settings.langfuse_public_key
        os.environ["LANGFUSE_SECRET_KEY"] = settings.langfuse_secret_key
        from langfuse import Langfuse

        return Langfuse(host=settings.langfuse_host, public_key=settings.langfuse_public_key,
                        secret_key=settings.langfuse_secret_key)
    except Exception:
        logger.exception("Langfuse 初始化失败，观测关闭，业务照常运行。")
        return None


@dataclass(frozen=True)
class TurnTrace:
    trace_id: str
    handler: Any | None
    config: dict          # 要并进 LangGraph config：{"callbacks": [...], "metadata": {...}}


def _public_key_of(client: Any) -> Any | None:
    """CallbackHandler 的 public_key：先问客户端对象，再退回 `build_client` 写过的环境变量。

    langfuse 5.1.0 的 `Langfuse` 实例不暴露 public_key 属性（只有 `_base_url`/`_project_id`
    这些），所以退回是常态而不是兜底；都为空时传 None，让 SDK 走默认客户端配置。
    """
    for attribute in ("_public_key", "public_key", "_project_public_key"):
        value = getattr(client, attribute, None)
        if isinstance(value, str) and value:
            return value
    return os.environ.get("LANGFUSE_PUBLIC_KEY") or None


def _unwrap_span(candidate: Any) -> Any:
    """langfuse 的 observation 包装对象把 OTEL span 藏在 `_otel_span` 上（`_client/span.py:316`，
    包装类自己没有 `set_attribute`/`is_recording`）。裸 OTEL span 和测试里的假 span 原样返回。"""
    if candidate is None:
        return None
    return getattr(candidate, "_otel_span", candidate)


def _is_recording(span: Any) -> bool:
    try:
        return span is not None and bool(span.is_recording())
    except Exception:
        return False


@contextmanager
def turn_trace(client: Any | None, conversation_id: int, *, name: str = "chat-turn",
               tags: list[str] | None = None):
    """一轮对话一个根 span + 一个绑到该 trace 的 CallbackHandler。

    client 为 None（观测关闭）时直通 yield None，零副作用。
    根 span 必须是 langfuse-sdk 自己建的：探针证明只有它的属性会被事件管线认。
    """
    if client is None:
        yield None
        return

    stack = ExitStack()
    token = None
    turn = None
    try:
        trace_id = client.create_trace_id(seed=f"{name}-{conversation_id}-{uuid4().hex}")
        root = stack.enter_context(client.start_as_current_observation(
            name=name, as_type="span", trace_context={"trace_id": trace_id}))
        # 先把根 span 注册进 contextvar，再让图跑起来：图内节点即使被换了 ambient context，
        # 也还能顺着 contextvar 找到这一轮的根 span。
        token = _turn_root.set(root)
        from langfuse.langchain import CallbackHandler

        handler = CallbackHandler(public_key=_public_key_of(client),
                                  trace_context={"trace_id": trace_id})
        turn = TurnTrace(trace_id, handler,
                         trace_config(conversation_id, handler, name=name, tags=tags))
    except Exception:
        # 观测建不起来最多是「这一轮没观测」：logger.exception 留痕，回复照旧。
        logger.exception("Langfuse 本轮追踪初始化失败，这一轮没有观测，业务照常运行。")
        turn = None
    try:
        yield turn
    finally:
        if token is not None:
            try:
                _turn_root.reset(token)
            except Exception:
                logger.debug("Langfuse 根 span contextvar 复位失败，忽略。", exc_info=True)
        try:
            stack.close()
        except Exception:
            logger.debug("Langfuse 根 span 关闭失败，忽略。", exc_info=True)


def trace_config(conversation_id: int, handler: Any | None, *, name: str = "chat-turn",
                 tags: list[str] | None = None) -> dict:
    """要并进 LangGraph config 的片段。session/user 都用会话 id：本项目没有账号体系，
    编造用户 id 是假数据（README 里也要写明这点）。

    `name` 是 trace 名，由调用方（正常情况下就是 `turn_trace`）传进来，跟根 span 同名：
    这里写死 chat-turn 而 `turn_trace(name=…)` 收参数的话，trace_name 会跟实际根 observation
    对不上（评审修复轮 1 第 6 条裁定）。默认值不变 ⇒ 现有调用与断言不用改。
    """
    metadata: dict[str, Any] = {
        "langfuse_session_id": f"conv-{conversation_id}",
        "langfuse_user_id": f"conv-{conversation_id}",
        "langfuse_trace_name": name,
    }
    if tags:
        metadata["langfuse_tags"] = list(tags)
    return {"callbacks": [handler] if handler is not None else [], "metadata": metadata}


def set_intent_on_trace(intent: str, confidence: float) -> bool:
    """把意图写到**本轮根 observation** 的 trace 级属性上。返回 False 表示没落地（本轮没有根 span）。

    只认 `turn_trace` 用 contextvar 注册的本轮根 span，**不退回 ambient span**：ambient 恰恰是
    那个不会被事件管线导出的 ASGI span，写上去照样返回 True，就是上一轮「探针假阳性」的原样复刻
    （评审修复轮 1 第 4 条裁定；实测证据见 dev-notes 阶段 4）。

    根 span 这一行的 `langfuse.trace.tags` 由 intent **独占**（这里直接赋值，不合并）：要加别的
    tag 请走 `trace_config` 的 `langfuse_tags`（run metadata 那条路由 SDK 自己做去重合并）。
    探针实测：tag 只落根 observation 自己那一行，**不摊给同 trace 的兄弟行**，
    所以按 intent 汇总必须走 Observations v2 + Metrics v2 按 traceId 本地 join（T16）。
    返回值只供探针与日志，不参与任何业务判据。
    """
    try:
        from langfuse import LangfuseOtelSpanAttributes as A

        span = _unwrap_span(_turn_root.get())
        if not _is_recording(span):
            logger.debug("本轮没有根 span（turn_trace 没建出来或已收尾），intent 不写。")
            return False
        span.set_attribute(A.TRACE_TAGS, [INTENT_TAG_PREFIX + str(intent)])
        span.set_attribute(f"{A.TRACE_METADATA}.intent", str(intent))
        span.set_attribute(f"{A.TRACE_METADATA}.intent_confidence", float(confidence))
        return True
    except Exception:
        logger.debug("写入 intent trace 属性失败，忽略。", exc_info=True)
        return False


def flush_tracing() -> None:
    try:
        from langfuse import get_client

        get_client().flush()
    except Exception:
        logger.debug("Langfuse flush 失败，忽略。", exc_info=True)
