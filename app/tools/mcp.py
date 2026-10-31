import asyncio
import logging
import time

from langchain_mcp_adapters.client import MultiServerMCPClient

from app.tools.builtin import TOOL_LABELS
from app.tools.registry import ToolSpec, check_spec, spec_from_tool

logger = logging.getLogger(__name__)

# Client 侧字面量是下划线（sessions.py:167）；Server 侧 run(transport="streamable-http") 是连字符。
CLIENT_TRANSPORT = "streamable_http"


def server_endpoints(settings) -> dict[str, tuple[str, str]]:
    configured = [name.strip() for name in (settings.mcp_servers or "").split(",") if name.strip()]
    table = {"logistics": (settings.mcp_logistics_url, settings.mcp_logistics_permission),
             "aftersale": (settings.mcp_aftersale_url, settings.mcp_aftersale_permission)}
    unknown = [name for name in configured if name not in table]
    if unknown:
        logger.warning("MCP_SERVERS 里有未登记的名字，已忽略：%s", "、".join(unknown))
    return {name: table[name] for name in configured if name in table}


class McpDiscovery:
    """现问现拿：每次 get_tools() 都新建 session（adapters 语义），所以 Server 侧新加工具
    只重启该 Server 就可见。TTL（缺省 60 秒，见 T2）只是省掉同一进程内的重复握手：
    新工具最多 60 秒后出现在表里，客服系统代码不动、不重启；演示要立刻看到就 `specs(force=True)`
    或把 `TOOL_DISCOVERY_CACHE_SECONDS` 设成 0。"""

    def __init__(self, settings) -> None:
        self.settings = settings
        self._cache: dict[str, tuple[float, list[ToolSpec]]] = {}

    async def specs(self, *, force: bool = False) -> list[ToolSpec]:
        results: list[ToolSpec] = []
        pending: list[tuple[str, str, str]] = []
        for name, (url, permission) in server_endpoints(self.settings).items():
            cached = self._cache.get(name)
            ttl = self.settings.tool_discovery_cache_seconds
            if not force and cached and time.monotonic() - cached[0] < ttl:
                results.extend(cached[1])
                continue
            pending.append((name, url, permission))
        if pending:
            # 并发握手：一台 Server 挂了不该把另一台的等待时间翻倍。最坏等待近似一次超时
            # （取消后的会话清理还会再占一点时间）。
            for specs in await asyncio.gather(*(self._fetch(*item) for item in pending)):
                results.extend(specs)
        return results

    async def _fetch(self, name: str, url: str, permission: str) -> list[ToolSpec]:
        cached = self._cache.get(name)
        try:
            client = MultiServerMCPClient(
                {name: {"url": url, "transport": CLIENT_TRANSPORT}},
                tool_name_prefix=False, handle_tool_errors=False)
            # 握手也受本地超时约束：Server 没起或卡住时，一轮对话不能被它拖过请求预算。
            async with asyncio.timeout(self.settings.tool_timeout_seconds):
                tools = await client.get_tools(server_name=name)
        except Exception as exc:
            logger.warning("MCP Server %s（%s）发现失败，本轮跳过它的工具：%s",
                           name, url, str(exc) or type(exc).__name__)
            self.forget(name)
            return list(cached[1]) if cached else []
        specs: list[ToolSpec] = []
        for item in tools:
            # 第三方工具可能缺描述或 schema；这种不进表，否则 ToolRegistry 每轮都会拒收整张表。
            try:
                spec = spec_from_tool(item, source="mcp", mcp_server=name, permission=permission,
                                      label=TOOL_LABELS.get(item.name))
                check_spec(spec)
            except Exception as exc:
                logger.warning("MCP 工具 %s/%s 定义不完整，已跳过：%s", name,
                               getattr(item, "name", "?"), exc)
                continue
            specs.append(spec)
        self._cache[name] = (time.monotonic(), specs)
        logger.info("MCP 发现：%s ← %s", name, "、".join(item.name for item in specs))
        return specs

    def forget(self, name: str | None = None) -> None:
        if name is None:
            self._cache.clear()
        else:
            self._cache.pop(name, None)
