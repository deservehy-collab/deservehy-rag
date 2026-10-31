from dataclasses import dataclass
from typing import Sequence

from langchain_core.tools import BaseTool
from pydantic import BaseModel


@dataclass(frozen=True)
class ToolSpec:
    """注册表里的一行：内置与 MCP 工具在这三样上必须长得一样。"""

    name: str
    description: str
    json_schema: dict
    source: str                      # "builtin" | "mcp"
    mcp_server: str | None
    permission: str                  # "read" | "write"
    label: str                       # 前端徽章中文名
    tool: BaseTool


class DuplicateToolError(ValueError):
    pass


def check_spec(spec: ToolSpec) -> None:
    """登记前的最后一道闸：三样缺一不可。MCP 拿到的不完整工具也用这个判，判不过就跳过。"""
    missing = [key for key, value in (("description", spec.description),
                                      ("json_schema", spec.json_schema)) if not value]
    if missing:
        raise ValueError(f"工具 {spec.name} 缺少 {('、'.join(missing))}；宁可起不来，也不让模型见到空定义的工具。")


class ToolRegistry:
    def __init__(self, specs: Sequence[ToolSpec]) -> None:
        by_name: dict[str, ToolSpec] = {}
        for spec in specs:
            check_spec(spec)
            if spec.name in by_name:
                raise DuplicateToolError(f"工具重名：{spec.name}")
            by_name[spec.name] = spec
        self._by_name = by_name

    def get(self, name: str) -> ToolSpec | None:
        return self._by_name.get(name)

    def names(self) -> list[str]:
        return list(self._by_name)

    def specs(self) -> list[ToolSpec]:
        return list(self._by_name.values())

    def langchain_tools(self) -> list[BaseTool]:
        return [item.tool for item in self._by_name.values()]


def merge_specs(groups: Sequence[Sequence[ToolSpec]]) -> tuple[list[ToolSpec], list[str]]:
    """先到的赢（内置在前），被丢的名字回给调用方记日志——绝不让两个同名工具进 bind_tools。"""
    kept: list[ToolSpec] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for spec in group:
            if spec.name in seen:
                dropped.append(spec.name)
                continue
            seen.add(spec.name)
            kept.append(spec)
    return kept, dropped


def spec_from_tool(tool: BaseTool, *, source: str, permission: str = "read",
                   label: str | None = None, mcp_server: str | None = None) -> ToolSpec:
    """把任意 BaseTool 折算成 ToolSpec：内置动态模块和 MCP 工具都走这一条路。"""
    schema = tool.args_schema
    if isinstance(schema, dict):
        json_schema = schema                       # adapters 造出来的 MCP 工具就是 JSON Schema dict
    elif isinstance(schema, type) and issubclass(schema, BaseModel):
        json_schema = schema.model_json_schema()   # pydantic 模型类（内置 @tool）
    elif hasattr(schema, "model_json_schema"):
        json_schema = schema.model_json_schema()   # pydantic 模型实例
    else:
        json_schema = {"type": "object", "properties": {}}   # 无参工具（None）与认不出的 schema
    return ToolSpec(name=tool.name, description=tool.description or "", json_schema=json_schema,
                    source=source, mcp_server=mcp_server, permission=permission,
                    label=label or tool.name, tool=tool)
