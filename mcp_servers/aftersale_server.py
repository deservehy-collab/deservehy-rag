"""售后查询 MCP Server（独立进程，8932）。ch08 需求 6：售后查询走外部工具。"""
import asyncio
import os

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from mcp_servers.mockgen import refund_progress_payload, seeded, warranty_payload

FAIL_TIMES = int(os.getenv("AFTERSALE_FAIL_TIMES", "0"))
SLOW_SECONDS = float(os.getenv("AFTERSALE_SLOW_SECONDS", "0"))
EXTRA_TOOLS = [name.strip() for name in os.getenv("AFTERSALE_EXTRA_TOOLS", "").split(",") if name.strip()]

mcp = FastMCP("aftersale", host=os.getenv("MCP_HOST", "127.0.0.1"),
              port=int(os.getenv("AFTERSALE_PORT", "8932")), streamable_http_path="/mcp",
              stateless_http=True, json_response=True)

_failures = 0


async def _lag_and_fail() -> None:
    global _failures
    if SLOW_SECONDS > 0:
        await asyncio.sleep(SLOW_SECONDS)
    if _failures < FAIL_TIMES:
        _failures += 1
        raise RuntimeError(f"售后系统忙（演示夹具，第 {_failures} 次失败）")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def query_warranty(order_id: str) -> dict:
    """按订单号查询是否在保，返回状态与剩余保修天数。"""
    await _lag_and_fail()
    return {"ok": True, "data": warranty_payload(order_id)}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def query_refund_progress(order_id: str) -> dict:
    """按订单号查询退款处理进度。"""
    await _lag_and_fail()
    return {"ok": True, "data": refund_progress_payload(order_id)}


if "query_coupon_balance" in EXTRA_TOOLS:
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def query_coupon_balance(user_id: str) -> dict:
        """查询用户优惠券余额（验收③演示用：只重启本 Server 就能被客服系统发现）。"""
        rnd = seeded("query_coupon_balance", user_id)
        return {"ok": True, "data": {"user_id": user_id, "count": rnd.randint(0, 5),
                                     "amount": rnd.choice([0, 5, 10, 20])}}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
