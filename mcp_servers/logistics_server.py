"""物流查询 MCP Server（独立进程，8931）。ch08 需求 6：物流查询由 MCP 接管。"""
import asyncio
import os

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from mcp_servers.mockgen import logistics_payload, seeded

FAIL_TIMES = int(os.getenv("LOGISTICS_FAIL_TIMES", "0"))
SLOW_SECONDS = float(os.getenv("LOGISTICS_SLOW_SECONDS", "0"))
EXTRA_TOOLS = [name.strip() for name in os.getenv("LOGISTICS_EXTRA_TOOLS", "").split(",") if name.strip()]

mcp = FastMCP("logistics", host=os.getenv("MCP_HOST", "127.0.0.1"),
              port=int(os.getenv("LOGISTICS_PORT", "8931")), streamable_http_path="/mcp",
              stateless_http=True, json_response=True)

_failures = 0


async def _lag_and_fail() -> None:
    global _failures
    if SLOW_SECONDS > 0:
        await asyncio.sleep(SLOW_SECONDS)
    if _failures < FAIL_TIMES:
        _failures += 1
        raise RuntimeError(f"物流系统忙（演示夹具，第 {_failures} 次失败）")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def query_logistics(order_id: str) -> dict:
    """按订单号查询物流轨迹，返回承运商、运单号、状态与节点列表。"""
    await _lag_and_fail()
    return {"ok": True, "data": logistics_payload(order_id)}


if "query_station_info" in EXTRA_TOOLS:
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def query_station_info(station: str) -> dict:
        """查询转运站点的作业情况（验收③演示用：只重启本 Server 就能被客服系统发现）。"""
        rnd = seeded("query_station_info", station)
        return {"ok": True, "data": {"station": station,
                                     "queue": rnd.randint(0, 40),
                                     "backlog": rnd.choice(["正常", "积压"])}}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
