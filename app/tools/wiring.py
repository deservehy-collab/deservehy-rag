import importlib
import logging

from app.tools.audit import ToolAuditWriter
from app.tools.builtin import build_specs
from app.tools.engine import ToolEngine
from app.tools.registry import ToolRegistry, merge_specs, spec_from_tool

logger = logging.getLogger(__name__)


def _names(raw: str) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


async def fetch_mcp_specs(mcp) -> tuple:
    """MCP 清单取不到就当没有：外部 Server 故障不能让整轮对话断掉（规格 §11.4）。"""
    if mcp is None:
        return ()
    try:
        return tuple(await mcp.specs())
    except Exception:
        logger.warning("MCP 工具清单获取失败，本轮只用内置工具。", exc_info=True)
        return ()


def extra_specs(settings) -> list:
    """`TOOL_EXTRA_MODULES` 里每个模块的 EXTRA_TOOLS 折算成 ToolSpec；坏模块跳过不拖垮启动。"""
    specs = []
    for module_name in _names(settings.tool_extra_modules):
        try:
            module = importlib.import_module(module_name)
        except Exception:
            logger.exception("动态工具模块导入失败，已跳过：%s", module_name)
            continue
        tools = getattr(module, "EXTRA_TOOLS", None) or []
        labels = getattr(module, "EXTRA_LABELS", None) or {}
        permissions = getattr(module, "EXTRA_PERMISSION", None) or {}
        if not isinstance(tools, (list, tuple)):
            logger.warning("动态工具模块的 EXTRA_TOOLS 不是列表，已跳过：%s", module_name)
            continue
        if not isinstance(labels, dict):
            labels = {}
        if not isinstance(permissions, dict):
            permissions = {}
        for tool in tools:
            try:
                specs.append(spec_from_tool(tool, source="builtin",
                                            permission=permissions.get(tool.name, "read"),
                                            label=labels.get(tool.name)))
            except Exception:
                logger.exception("动态工具无法登记，已跳过：%s", module_name)
    return specs


def build_registry(store, conversation_id, knowledge, *, settings, mcp_specs=(), exclude=()):
    """一请求一表：内置闭包要绑 conversation_id，所以每轮现拼（规格 §6.6）。"""
    skip = set(exclude)
    builtin = [item for item in build_specs(store, conversation_id, knowledge) if item.name not in skip]
    kept, dropped = merge_specs([builtin, extra_specs(settings), list(mcp_specs)])
    if dropped:
        logger.warning("工具重名，保留先登记的，丢弃：%s", "、".join(sorted(set(dropped))))
    return ToolRegistry(kept)


def build_engine(registry, *, settings, session_factory):
    return ToolEngine(registry, settings=settings,
                      audit=ToolAuditWriter(session_factory, enabled=settings.tool_audit_enabled))
