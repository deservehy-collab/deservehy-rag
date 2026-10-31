"""验收①的活样本：把它写进 `TOOL_EXTRA_MODULES` 就能被注册中心收进，核心代码一行不改。

动态模块契约：
  EXTRA_TOOLS       list[BaseTool]，必填
  EXTRA_LABELS      dict[工具名, 中文名]，徽章用；缺省用工具名
  EXTRA_PERMISSION  dict[工具名, "read"|"write"]，缺省 read
写操作还要进 `TOOL_WRITE_ALLOWLIST` 才可能被执行，且必须等前端确认——注册不等于授权。
`TOOL_EXTRA_MODULES` 在启动时读取，改完要重启客服服务；免重启承诺只适用于 MCP（规格 §6.5）。
"""
from langchain_core.tools import tool
from pydantic import BaseModel, Field


class CouponArgs(BaseModel):
    order_id: str = Field(min_length=1, max_length=64, description="订单号")


@tool("query_coupon", args_schema=CouponArgs)
def query_coupon(order_id: str) -> dict:
    """查询这张订单可用的优惠券（演示用动态只读工具）。"""
    return {"ok": True, "data": {"order_id": order_id, "coupons": [{"name": "满 100 减 10", "type": "店铺券"}]}}


@tool("issue_coupon", args_schema=CouponArgs)
def issue_coupon(order_id: str) -> dict:
    """给用户补发一张优惠券（演示用写操作，默认不在写白名单里）。"""
    return {"ok": True, "data": {"order_id": order_id, "issued": True, "name": "满 100 减 10"}}


EXTRA_TOOLS = [query_coupon, issue_coupon]
EXTRA_LABELS = {"query_coupon": "查询优惠券", "issue_coupon": "补发优惠券"}
EXTRA_PERMISSION = {"issue_coupon": "write"}
