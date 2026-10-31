from pydantic import BaseModel

from app.config import Settings
from app.tools.policy import decide, write_allowlist
from app.tools.registry import ToolSpec


class DummyArgs(BaseModel):
    order_id: str


def make_tool(name="query_order"):
    from langchain_core.tools import tool

    @tool(name, args_schema=DummyArgs, description="测试用工具。")
    def handle(order_id: str) -> dict:
        return {"ok": True}
    return handle


def spec(name="query_order", *, source="builtin", server=None, permission="read"):
    return ToolSpec(name=name, description="测试用工具。",
                    json_schema=DummyArgs.model_json_schema(), source=source, mcp_server=server,
                    permission=permission, label="测试", tool=make_tool(name))


def settings(**overrides):
    base = {"openai_base_url": "http://upstream.invalid/v1", "openai_model": "m", "openai_api_key": "k",
            "tool_write_allowlist": ""}
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_read_tools_are_allowed_without_confirmation():
    decision = decide(spec(), confirmed=False, settings=settings())
    assert decision.allowed is True and decision.needs_confirm is False


def test_allowlisted_write_needs_confirmation_first_then_passes():
    tool = spec("create_ticket", permission="write")
    allow = settings(tool_write_allowlist="create_ticket")
    pending = decide(tool, confirmed=False, settings=allow)
    assert pending.allowed is False and pending.needs_confirm is True
    assert "确认" in pending.reason
    passed = decide(tool, confirmed=True, settings=allow)
    assert passed.allowed is True and passed.needs_confirm is False


def test_unknown_permission_fails_closed_as_write():
    """permission 只该是 read/write；写错的值不当只读放行，按写路径要求白名单。"""
    decision = decide(spec("odd_tool", permission="admin"), confirmed=True, settings=settings())
    assert decision.allowed is False and decision.needs_confirm is False


def test_write_tool_outside_allowlist_is_rejected():
    tool = spec("delete_order", permission="write")
    decision = decide(tool, confirmed=True, settings=settings())
    assert decision.allowed is False and decision.needs_confirm is False
    assert "不在系统允许范围内" in decision.reason


def test_mcp_server_declared_write_locally_rejects_tools_outside_allowlist():
    """Server 自称只读、或本地把它标成 write，都按我们这侧的规则走。"""
    tool = spec("create_shipment", source="mcp", server="logistics", permission="write")
    decision = decide(tool, confirmed=True, settings=settings())
    assert decision.allowed is False
    assert "外部写操作未列入本地写白名单" in decision.reason


def test_mcp_write_tool_listed_in_allowlist_still_needs_confirmation():
    """白名单里的外部写工具不是默许：同样要先经用户确认，拒绝理由也只因白名单。"""
    tool = spec("sync_inventory", source="mcp", server="aftersale", permission="write")
    allow = settings(tool_write_allowlist="sync_inventory")
    pending = decide(tool, confirmed=False, settings=allow)
    assert pending.allowed is False and pending.needs_confirm is True
    assert decide(tool, confirmed=True, settings=allow).allowed is True


def test_mcp_read_tool_is_allowed():
    tool = spec("query_logistics", source="mcp", server="logistics")
    assert decide(tool, confirmed=False, settings=settings()).allowed is True


def test_allowlist_comes_from_settings_and_splits_on_commas():
    assert write_allowlist(settings(tool_write_allowlist="create_ticket, foo ")) == {"create_ticket", "foo"}
    assert write_allowlist(settings(tool_write_allowlist="")) == set()
