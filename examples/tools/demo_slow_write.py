"""验收⑥的活样本：一个必定超时的写操作，用来看「写操作不自动重试」。

ATTEMPTS 记录它被真正调了几次——引擎在写路径上只要重试一次，这个数字就会 >1。
把它写进 TOOL_EXTRA_MODULES 和 TOOL_WRITE_ALLOWLIST 才可能被执行（注册不等于授权）。
"""
import asyncio

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

ATTEMPTS = 0


class SlowWriteArgs(BaseModel):
    note: str = Field(min_length=1, max_length=40, description="备注，随便给一句话")


async def _release_stock(note: str) -> dict:
    global ATTEMPTS
    ATTEMPTS += 1
    await asyncio.sleep(30)
    return {"ok": True, "data": {"note": note, "released": True}}


release_stock = StructuredTool(
    name="demo_release_stock", description="演示用写操作：锁库存，响应极慢，用来观察超时行为。",
    coroutine=_release_stock, args_schema=SlowWriteArgs)

EXTRA_TOOLS = [release_stock]
EXTRA_LABELS = {"demo_release_stock": "演示锁库存"}
EXTRA_PERMISSION = {"demo_release_stock": "write"}
