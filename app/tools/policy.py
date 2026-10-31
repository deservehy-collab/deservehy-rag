from dataclasses import dataclass

from app.tools.registry import ToolSpec


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    needs_confirm: bool = False
    reason: str = ""


def write_allowlist(settings) -> set[str]:
    return {item.strip() for item in (settings.tool_write_allowlist or "").split(",") if item.strip()}


def decide(spec: ToolSpec, *, confirmed: bool, settings) -> PermissionDecision:
    """顺序即优先级；判定只看本地配置，MCP Server 自己的用途声明一律不参与（规格 §8）。

    reason 会被引擎原样回灌给模型，所以写成完整、可行动的句子，不是内部术语。
    """
    if spec.permission == "read":
        return PermissionDecision(True)
    allowlist = write_allowlist(settings)
    if spec.name not in allowlist:
        if spec.source == "mcp":
            return PermissionDecision(False, reason="外部写操作未列入本地写白名单，系统不会执行；"
                                                    "请不要重复调用，可改用只读工具或向用户说明。")
        return PermissionDecision(False, reason="这个写操作不在系统允许范围内，无法执行。")
    if confirmed:
        return PermissionDecision(True)
    return PermissionDecision(False, needs_confirm=True, reason="写操作需要用户在页面确认后才能执行。")
