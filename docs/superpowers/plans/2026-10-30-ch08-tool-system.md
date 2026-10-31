# ch08 即插即用工具系统 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把写死的五个内置工具升级成「注册中心＋JSON Schema 校验＋权限把门＋统一执行引擎＋审计落库＋两个自建 MCP Server＋建工单前端确认流」，新工具只登记即可被主力 Agent 用上。

**Architecture:** 新增 `app/tools/` 包，八个模块各一职责；每轮请求把「内置 specs ＋ MCP 现取 specs」合成一个只读 `ToolRegistry`，模型只看到这张表；所有调用（含 `/api/tickets`、`/api/refunds`、图内 policy 节点）统一走 `ToolEngine.run()`，引擎按「查表→校验→权限→执行(超时/重试)→格式化→审计」固定顺序走。建工单用 LangGraph `interrupt` 把预览推到前端，确认回来才真执行。

**Tech Stack:** FastAPI 0.162.0 / SQLAlchemy 2.1.6 / langgraph 1.2.18 / langchain-core 1.7.0 / mcp 1.30.0（官方 Python SDK，FastMCP + Streamable HTTP）/ langchain-mcp-adapters 0.3.2（`MultiServerMCPClient`）/ jsonschema 4.26.0（Draft 2020-12）/ pytest 9.2.0。

**Spec:** `docs/superpowers/specs/2026-10-30-ch08-tool-system-design.md`（本计划实现它，逐节引用 §n）

## Global Constraints

- 所有 Python 命令一律用项目虚拟环境并带 `-X utf8`：`.venv/Scripts/python.exe -X utf8 ...`；全量回归 `LANGSMITH_TRACING=false .venv/Scripts/python.exe -X utf8 -m pytest -q`（ch07 基线 313 passed，只许变多不许变红）。
- 依赖只做两件事：`requirements.txt` 加 `mcp==1.30.0`、`langchain-mcp-adapters==0.3.2`；`requirements.lock.txt` 按 `pip freeze` 补上实际传递依赖（PyJWT、attrs、httpx-sse、jsonschema、jsonschema-specifications、python-multipart、referencing、rpds-py、sse-starlette、uvicorn 已在册则不重复）。**不引入任何其他新依赖。**
- 技术选型定死，不许自行替换：Server 侧官方 SDK `FastMCP` + Streamable HTTP，Client 侧 `MultiServerMCPClient`。走不通就停下来报告，不换方案（用户原话）。
- 三处已实测的 API 事实，写代码时照抄不凭记忆：Server 侧 `mcp.run(transport="streamable-http")` 是**连字符**；Client 侧连接配置 `"transport": "streamable_http"` 是**下划线**；MCP 工具的 `args_schema` 是 **JSON Schema dict**（`.venv/Lib/site-packages/langchain_mcp_adapters/tools.py:531`），且这些工具**只有 coroutine 没有 func**（`:532`），所以引擎统一用 `await tool.ainvoke(args)`（同步工具也支持：实测会在 executor 线程里跑）。
- 隐私：`.env`、`log/`、`.cache` 不入库、不回显任何 key；`tool_audit_logs` 含用户原话，README 要与 `messages` 同级说明。
- 提交：用户规则「每章一次提交」优先于技能的频繁提交。**每个任务结束只做一次本地提交，绝不推送**；交付任务用 `git reset --soft bddfeba` 压成一条「第八次提交（ch08）：…」后再推。ch07 的教训：文档与评估数字没定稿就推主提交，结果被迫 squash＋force-with-lease。
- 推送命令：`git -c http.proxy= -c https.proxy= push origin main`；若报 `Recv failure: Connection was reset`，改用单次注入 `git -c http.proxy=http://127.0.0.1:7890 -c https.proxy=http://127.0.0.1:7890 push origin main`。不改全局 git config。
- 面向用户的文案一律中文；工具结果序列化 `ensure_ascii=False`；错误按「参数不合法／查询落空／真故障」三类如实分诊，不粉饰（§9）。
- 留痕：每完成一个阶段就向 `dev-notes/ch08.md` 追加一段（用户原话／关键产出／我拒绝或纠偏了什么／翻车与返工），不许收尾一次性补记。

## 文件地图

| 文件 | 动作 | 责任 |
|---|---|---|
| `app/tools/__init__.py` | 新建 | 对外再导出，保证 `from app.tools import X` 不断 |
| `app/tools/registry.py` | 新建 | `ToolSpec`／`ToolRegistry`／`DuplicateToolError`／`merge_specs` |
| `app/tools/builtin.py` | 新建（从 `app/tools.py` 搬） | 参数模型、固定 mock、五个内置工具、`build_specs()` |
| `app/tools/legacy.py` | 新建（临时） | 旧 `ToolExecutor`/`summarize` 原样搬家，T10 删除 |
| `app/tools/validate.py` | 新建 | JSON Schema 校验＋中文说明 |
| `app/tools/policy.py` | 新建 | 只读／写三态判定，只认本地配置 |
| `app/tools/formatting.py` | 新建 | 字段投影、枚举翻人话、摘要 |
| `app/tools/audit.py` | 新建 | 写 `tool_audit_logs`，绝不抛 |
| `app/tools/engine.py` | 新建 | 唯一调用出口 `ToolEngine.run()` |
| `app/tools/mcp.py` | 新建 | 连接配置、`MultiServerMCPClient`、发现与缓存、`ToolSpec` 转换 |
| `app/models.py` `scripts/init_sql/006_tools.sql` | 改／新建 | `ToolAuditLog` |
| `mcp_servers/{__init__,mockgen,logistics_server,aftersale_server}.py`、`start-mcp.bat` | 新建 | 两个独立进程的 MCP Server |
| `app/workflow/{state,confirm_nodes,graph,agent_node}.py` | 改／新建 | `pending_write`、interrupt 节点与两条分支 |
| `app/{schemas,service,main}.py` | 改 | `ResumeRequest`、`stream_mode=["custom","updates"]`、`POST /api/chat/resume` |
| `app/agent/react.py`、`app/workflow/policy_nodes.py` | 改 | 改走 `ToolEngine`，状态帧带 `source` |
| `app/config.py`、`app/context.py`、`.env.example` | 改 | 新配置键、工具 schema 预算口径 |
| `app/web/chat.js`、`app/web/chat.css` | 改 | 工单预览卡片（Vibe Coding） |
| `examples/tools/demo_coupon.py` | 新建 | 验收①「只登记不改核心码」的示例工具 |
| `examples/tools/demo_slow_write.py` | 新建 | 验收⑥「写操作超时不自动重试」的演示工具 |
| `scripts/demo_tools_mcp.py`、`scripts/evaluate_ticket_confirm.py`、`evals/ticket_confirm_cases.jsonl` | 新建 | 验收①–⑥、追问行为评估 |
| `tests/*`、`README.md`、`dev-notes/ch08.md` | 新建／改 | 见 §16 |

---

### Task 1: 把 interrupt／stream_mode 语义固化成回归测试

LangGraph 的三处行为决定了确认流怎么写（发出帧的位置、节点重跑、重复 resume）。spike 已实测，本任务把结论变成会红的测试，防止升级库后静默变卦。规格 §18-C6。

**Files:**
- Create: `tests/test_interrupt_streaming.py`

**Interfaces:**
- Consumes: 无
- Produces: 四条被 T13/T14 依赖的事实断言；`CUSTOM_UPDATES = ["custom", "updates"]`

- [ ] **Step 1: 写测试**

```python
import asyncio
import operator
from typing import Annotated, TypedDict

from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

CUSTOM_UPDATES = ["custom", "updates"]


class SpikeState(TypedDict, total=False):
    log: Annotated[list, operator.add]
    pending: dict | None
    decision: str
    reruns: int


def _build():
    """agent 发一帧后挂起；confirm 节点会重跑，所以副作用必须留在 agent 里。"""

    def agent(state):
        get_stream_writer()(("token", {"content": "agent"}))
        return {"pending": {"tool": "create_ticket"}, "log": ["agent"]}

    def confirm_ticket(state):
        # 这帧是「含 interrupt 的节点 resume 时从头重跑」的唯一探针：真重跑 ⇒ resume 轮会再看见一次
        # entered；若哪天 langgraph 改成原地恢复，resume 轮就只剩 status 一帧，本测试立刻变红。
        # （没有这一行，「重跑」与「原地恢复」两种实现的输出完全相同，断言锁不住任何事实。）
        get_stream_writer()(("entered", {"tool": state["pending"]["tool"]}))
        decision = interrupt(state["pending"])
        get_stream_writer()(("status", {"phase": "resumed", "decision": decision}))
        return {"decision": decision, "log": [f"decision:{decision}"], "reruns": 1}

    builder = StateGraph(SpikeState)
    builder.add_node("agent", agent)
    builder.add_node("confirm_ticket", confirm_ticket)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", "confirm_ticket")
    builder.add_edge("confirm_ticket", END)
    return builder.compile(checkpointer=InMemorySaver())


async def _drain(graph, payload, config):
    return [item async for item in graph.astream(payload, config, stream_mode=CUSTOM_UPDATES,
                                                 durability="sync")]


def test_interrupt_surfaces_as_updates_chunk():
    async def run():
        config = {"configurable": {"thread_id": "t-one"}}
        chunks = await _drain(_build(), {"log": [], "pending": None}, config)
        # 本机 langgraph 1.2.18 实测首轮四帧：agent 的 custom(token) → agent 的 updates
        # → confirm 的 custom(entered) → 带 __interrupt__ 的 updates。Task 1 首轮执行时 confirm
        # 还没有 entered 那帧、只有三帧，评审后裁决加回探针——它同时让「节点重跑」变成可断言的事实。
        assert [c[0] for c in chunks] == ["custom", "updates", "custom", "updates"]
        assert chunks[0][1] == ("token", {"content": "agent"})
        assert chunks[1][1] == {"agent": {"pending": {"tool": "create_ticket"}, "log": ["agent"]}}
        assert chunks[2][1] == ("entered", {"tool": "create_ticket"})
        assert "__interrupt__" in chunks[3][1]
        interrupted = chunks[-1][1]["__interrupt__"]
        assert interrupted[0].value == {"tool": "create_ticket"}
        # 纯 custom 模式看不见中断信号——这就是服务层必须用双模式的原因。
        # 注意：stream_mode 传单个字符串时 langgraph 吐的是裸 payload（不是 (mode, payload)），
        # 所以这里直接比整帧列表，别写成 i[0]。
        single = [i async for i in _build().astream({"log": [], "pending": None},
                                                    {"configurable": {"thread_id": "t-two"}},
                                                    stream_mode="custom", durability="sync")]
        assert single == [("token", {"content": "agent"}), ("entered", {"tool": "create_ticket"})]
    asyncio.run(run())


def test_resumed_node_reruns_from_the_top_and_custom_writer_still_works():
    async def run():
        graph = _build()
        config = {"configurable": {"thread_id": "t-three"}}
        await _drain(graph, {"log": [], "pending": None}, config)
        resumed = await _drain(graph, Command(resume="confirm"), config)
        # entered 再次出现＝节点真的从头重跑过（T13 的副作用必须留在 agent，理由就在这行）。
        assert [c[1] for c in resumed if c[0] == "custom"] == [
            ("entered", {"tool": "create_ticket"}),
            ("status", {"phase": "resumed", "decision": "confirm"}),
        ]
        values = (await graph.aget_state(config)).values
        assert values["log"] == ["agent", "decision:confirm"]
    asyncio.run(run())


def test_double_resume_is_a_silent_no_op_and_pending_looks_like_a_new_run():
    async def run():
        graph = _build()
        config = {"configurable": {"thread_id": "t-four"}}
        await _drain(graph, {"log": [], "pending": None}, config)
        await _drain(graph, Command(resume="confirm"), config)
        assert await _drain(graph, Command(resume="confirm"), config) == []
        # 用户无视卡片直接问下一句：不报错，但会再挂一个新 interrupt，旧卡片必须作废。
        again = await _drain(graph, {"log": [], "pending": None}, config)
        assert again[-1][1]["__interrupt__"][0].value == {"tool": "create_ticket"}
    asyncio.run(run())


def test_mcp_style_tool_only_supports_ainvoke():
    """引擎只用一条 ainvoke 路径：同步工具照样能 ainvoke（落 executor 线程），
    而 adapters 造的 MCP 工具没有 func，同步 invoke 会炸。"""
    @tool("sync_probe")
    def sync_probe(order_id: str) -> dict:
        """测试用。"""
        return {"ok": True, "order_id": order_id}

    async def run():
        assert (await sync_probe.ainvoke({"order_id": "1001"}))["ok"] is True
    asyncio.run(run())
```

- [ ] **Step 2: 跑测试确认全绿**

Run: `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_interrupt_streaming.py -q`
Expected: PASS（4 passed）。这些是 spike 结论的镜像；若任何一条红，说明本机 langgraph 行为与规格 §18-C6 的假设不符，**先停下来报告，不要改确认流设计**。

- [ ] **Step 3: 本地提交**

```bash
git add tests/test_interrupt_streaming.py dev-notes/ch08.md
git commit -m "test(ch08/T1): 固化 interrupt 与 stream_mode 语义"
```
`dev-notes/ch08.md` 追加「阶段 3：T1 interrupt 语义回归测试」一段（含本任务 4 条测试的实测输出摘录）。规格、计划与「阶段 1/2」留痕已在 `docs(ch08)` 提交里落库，本任务不再重复追加阶段 2。

---

### Task 2: 依赖、配置键与 `tool_audit_logs` 表

审计表和配置先行，后面每个模块都能读到确定的键；SQL 与 ORM 双向可建（新库走 `init_db.py`，老库执行 006）。规格 §5、§14。

**Files:**
- Modify: `requirements.txt`、`requirements.lock.txt`、`app/config.py`、`app/models.py`、`.env.example`
- Create: `scripts/init_sql/006_tools.sql`
- Test: `tests/test_ch08_schema.py`

**Interfaces:**
- Consumes: 无
- Produces: `models.ToolAuditLog`（字段名见 Step 4）；`Settings.mcp_servers/mcp_logistics_url/mcp_logistics_permission/mcp_aftersale_url/mcp_aftersale_permission/tool_write_allowlist/tool_extra_modules/tool_audit_enabled/tool_discovery_cache_seconds/tool_schema_reserve_tokens`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_ch08_schema.py
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from app.config import Settings
from app.models import Base, ToolAuditLog


def test_settings_expose_ch08_keys_with_defaults():
    settings = Settings(_env_file=None, openai_base_url="http://upstream.invalid/v1",
                        openai_model="m", openai_api_key="k")
    assert settings.mcp_servers == "logistics,aftersale"
    assert settings.mcp_logistics_url == "http://127.0.0.1:8931/mcp"
    assert settings.mcp_aftersale_url == "http://127.0.0.1:8932/mcp"
    assert settings.mcp_logistics_permission == "read"
    assert settings.mcp_aftersale_permission == "read"
    assert settings.tool_write_allowlist == "create_ticket"
    assert settings.tool_extra_modules == ""
    assert settings.tool_audit_enabled is True
    assert settings.tool_discovery_cache_seconds == 60
    assert settings.tool_schema_reserve_tokens == 400


def test_tool_audit_log_columns_and_no_foreign_key():
    table = ToolAuditLog.__table__
    columns = {item.name: str(item.type) for item in table.columns}
    assert set(columns) == {"id", "conversation_id", "tool_call_id", "tool_name", "tool_source",
                            "mcp_server", "arguments", "result_summary", "status",
                            "error_message", "retry_count", "duration_ms", "created_at"}
    assert columns["tool_name"] == "VARCHAR(128)"
    assert columns["arguments"] == "JSON"
    assert columns["retry_count"] == "INTEGER"
    assert columns["created_at"] == "DATETIME"
    assert table.columns["conversation_id"].nullable
    assert not table.columns["tool_name"].nullable
    assert table.foreign_keys == set()
    # SQLAlchemy 2.1.6 的 Enum.type.enums 是 list[str]（不是 pydantic 那种 .status 对象）
    assert set(ToolAuditLog.__table__.columns["status"].type.enums) == \
        {"成功", "失败", "超时", "校验拦下", "权限拒绝"}


def test_sqlite_can_create_and_roundtrip(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    Base.metadata.create_all(engine)
    conn = engine.raw_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO tool_audit_logs (conversation_id, tool_call_id, tool_name, tool_source,"
            " mcp_server, arguments, result_summary, status, error_message, retry_count, duration_ms)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (7, "call_1", "query_logistics", "mcp", "logistics", '{"order_id":"1001"}',
             "2 条物流记录", "成功", None, 1, 42),
        )
        conn.commit()
        row = cursor.execute("SELECT status, retry_count, duration_ms, tool_source FROM tool_audit_logs").fetchone()
        assert row == ("成功", 1, 42, "mcp")
    finally:
        conn.close()


def test_006_sql_declares_the_same_table():
    text = Path("scripts/init_sql/006_tools.sql").read_text(encoding="utf-8")
    assert text.startswith("SET NAMES utf8mb4;")
    for name in ("tool_audit_logs", "idx_conversation_id", "idx_tool_name", "idx_status"):
        assert name in text
    assert "FOREIGN KEY" not in text.upper()


@pytest.mark.parametrize("bad", ["审计", "未知"])
def test_status_enum_rejects_values_outside_the_five_documented(bad):
    allowed = set(Base.metadata.tables["tool_audit_logs"].columns["status"].type.enums)
    assert bad not in allowed
```

- [ ] **Step 2: 跑到红**

Run: `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_ch08_schema.py -q`
Expected: FAIL — `ModuleNotFoundError`/`AttributeError: 'Settings' object has no attribute 'mcp_servers'`，`ImportError: cannot import name 'ToolAuditLog'`。

- [ ] **Step 3: 加配置键（`app/config.py`，接在 `tool_max_attempts` 之后）**

```python
    mcp_servers: str = Field(default="logistics,aftersale", min_length=0, max_length=255)
    mcp_logistics_url: str = Field(default="http://127.0.0.1:8931/mcp", min_length=1, max_length=255)
    mcp_logistics_permission: Literal["read", "write"] = "read"
    mcp_aftersale_url: str = Field(default="http://127.0.0.1:8932/mcp", min_length=1, max_length=255)
    mcp_aftersale_permission: Literal["read", "write"] = "read"
    tool_write_allowlist: str = Field(default="create_ticket", min_length=0, max_length=255)
    tool_extra_modules: str = Field(default="", min_length=0, max_length=512)
    tool_audit_enabled: bool = True
    tool_discovery_cache_seconds: int = Field(default=60, ge=0, le=3600)
    tool_schema_reserve_tokens: int = Field(default=400, ge=0, le=8000)
```

- [ ] **Step 4: 加 ORM 模型（`app/models.py`，接在 `Ticket` 之后；`Index`、`JSON`、`Enum`、`IdType`、`TABLE_OPTIONS` 都已在册）**

```python
class ToolAuditLog(Base):
    __tablename__ = "tool_audit_logs"
    # 审计表不挂外键：conversation_id 只是线索，约束不能反过来拦住留痕（规格 §5）。
    __table_args__ = (
        Index("idx_conversation_id", "conversation_id"),
        Index("idx_tool_name", "tool_name"),
        Index("idx_status", "status"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int | None] = mapped_column(IdType)
    tool_call_id: Mapped[str | None] = mapped_column(String(64))
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_source: Mapped[str] = mapped_column(Enum("builtin", "mcp", name="tool_source"), nullable=False)
    mcp_server: Mapped[str | None] = mapped_column(String(64))
    arguments: Mapped[dict | None] = mapped_column(JSON)
    result_summary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Enum("成功", "失败", "超时", "校验拦下", "权限拒绝", name="tool_audit_status"), nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(String(512))
    retry_count: Mapped[int] = mapped_column(default=0, server_default=text("0"), nullable=False)
    duration_ms: Mapped[int] = mapped_column(default=0, server_default=text("0"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
```

- [ ] **Step 5: 写 `scripts/init_sql/006_tools.sql`（用户给的 DDL 原样，只把 MySQL 的 `TINYINT UNSIGNED` 与 `ENUM` 保持原样）**

```sql
SET NAMES utf8mb4;
-- ch08 工具层：调用审计表。已有库执行一次；新库由 `python scripts/init_db.py` 用 ORM 直接建表。
-- 不挂外键：写审计失败不许反过来拦住工具执行（规格 §5）。

CREATE TABLE tool_audit_logs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  conversation_id BIGINT UNSIGNED NULL COMMENT '所属会话；无会话上下文时为 NULL',
  tool_call_id VARCHAR(64) NULL COMMENT '模型给出的 tool_call_id',
  tool_name VARCHAR(128) NOT NULL COMMENT '工具名',
  tool_source ENUM('builtin','mcp') NOT NULL COMMENT '来源',
  mcp_server VARCHAR(64) NULL COMMENT 'MCP Server 名；内置为 NULL',
  arguments JSON NULL COMMENT '调用参数（含用户原话，与 messages 同级隐私）',
  result_summary TEXT NULL COMMENT '结果摘要，最长 60 字',
  status ENUM('成功','失败','超时','校验拦下','权限拒绝') NOT NULL COMMENT '状态',
  error_message VARCHAR(512) NULL COMMENT '错误说明',
  retry_count TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '实际重试次数 = 尝试次数 - 1',
  duration_ms INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '耗时（毫秒）',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_conversation_id (conversation_id),
  KEY idx_tool_name (tool_name),
  KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

- [ ] **Step 6: 装依赖并固化 lock**

```bash
.venv/Scripts/python.exe -m pip install "mcp==1.30.0" "langchain-mcp-adapters==0.3.2" -i https://mirrors.aliyun.com/pypi/simple/ || \
.venv/Scripts/python.exe -m pip install "mcp==1.30.0" "langchain-mcp-adapters==0.3.2"
```
然后 `requirements.txt` 追加两行 pin，并用 `.venv/Scripts/python.exe -m pip freeze` 找出新增传递依赖追加进 `requirements.lock.txt`（照 T2 Step 1 列的名单核对）。两包本机已存在时 pip 会直接 `Requirement already satisfied`——仍要写进 requirements，否则换机装不上。

- [ ] **Step 7: 跑到绿**

Run: `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_ch08_schema.py tests/test_db.py tests/test_ch04_schema.py -q`
Expected: PASS。`.env.example` 补一组注释过的键（只放占位与本地地址，不放任何 key）。

> **T2 完成后的评审加强（dev-notes 阶段 5）**：本步给出的测试是下限。落库版本额外做了四件事——`006_tools.sql` 与 ORM 的列/索引**全等**比对（正则抽 DDL）、`arguments` JSON 中文原样回读、裸 SQL 省略 `retry_count`/`duration_ms` 断言 `(0, 0)`、MySQL 方言渲染出原生 `ENUM('成功','失败','超时','校验拦下','权限拒绝')`。原因：实测 SQLite **不把 Enum 渲染成 CHECK**（插第六种状态能成功），所以「五态」只能靠生产 DDL＋执行引擎自己拦，不能靠开发库。后续任务照本计划抄代码时，以 `tests/test_ch08_schema.py` 的现状为准。

- [ ] **Step 8: 本地提交** `git commit -am "feat(ch08/T2): 审计表、配置键与 MCP 依赖"`

---

### Task 3: `app/tools/` 包与注册中心（含内置搬家）

把 285 行的 `app/tools.py` 拆成包，先只加注册中心与搬家，**行为零改动**（`ToolExecutor` 原样活在 `legacy.py`，T10 才删）。规格 §4、§6。

**Files:**
- Create: `app/tools/__init__.py`、`app/tools/registry.py`、`app/tools/builtin.py`、`app/tools/legacy.py`、`tests/test_tool_registry.py`
- Delete: `app/tools.py`（内容搬空后删除）
- Modify: `app/agent/react.py:15`、`app/agent/react.py:113`、`app/workflow/agent_node.py:10`、`app/workflow/policy_nodes.py:7`、`app/main.py:38`、`app/context.py:60-62` 的 import 路径

**Interfaces:**
- Consumes: `app.tools.builtin` 的 `make_tools`、`TOOL_LABELS`、`list_orders`、四个 Args 模型
- Produces:
  ```python
  # app/tools/registry.py
  @dataclass(frozen=True)
  class ToolSpec:
      name: str; description: str; json_schema: dict; source: str; mcp_server: str | None
      permission: str; label: str; tool: BaseTool

  class DuplicateToolError(ValueError): ...
  class ToolRegistry:
      def __init__(self, specs: Sequence[ToolSpec]) -> None            # 重名抛 DuplicateToolError
      def get(self, name: str) -> ToolSpec | None
      def names(self) -> list[str]
      def specs(self) -> list[ToolSpec]
      def langchain_tools(self) -> list[BaseTool]
  def merge_specs(groups: Sequence[Sequence[ToolSpec]]) -> tuple[list[ToolSpec], list[str]]  # (保留的, 被丢的名字)
  # app/tools/builtin.py
  def build_specs(store, conversation_id: int, knowledge=None, *,
                  settings=None, include_logistics: bool = True) -> list[ToolSpec]
  def builtin_json_schemas() -> list[dict]      # 给 context.tool_schema_tokens() 用
  ```

- [ ] **Step 1: 写失败测试**

```python
# tests/test_tool_registry.py
import pytest
from sqlalchemy import create_engine

from app.database import build_session_factory
from app.models import Base
from app.seed import seed_faq
from app.storage import ConversationStore
from app.tools.builtin import builtin_json_schemas, build_specs
from app.tools.registry import DuplicateToolError, ToolRegistry, ToolSpec, merge_specs
from tests.test_tools import FakeKnowledge  # noqa: F401  (夹具复用，见 Step 2 说明)


@pytest.fixture()
def specs(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'reg.db'}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    seed_faq(factory)
    store = ConversationStore(factory)
    conversation_id = store.start_turn(user_id="anon-1", message="帮我看看")
    result = build_specs(store, conversation_id, FakeKnowledge())
    engine.dispose()
    return result


def test_every_spec_carries_the_three_required_pieces(specs):
    assert {item.name for item in specs} == {"query_order", "query_product", "query_logistics",
                                             "query_faq", "create_ticket"}
    for item in specs:
        assert item.description.strip(), item.name
        assert item.json_schema["type"] == "object"
        assert item.json_schema["properties"], item.name
        assert item.source == "builtin" and item.mcp_server is None
        assert item.permission == ("write" if item.name == "create_ticket" else "read")
        assert item.label, item.name


def test_builtin_schema_estimate_covers_every_local_tool():
    schemas = builtin_json_schemas()
    assert len(schemas) == 4          # 四个参数模型；create_ticket 与 query_logistics 各复用其一
    assert all(item["type"] == "object" for item in schemas)


def test_each_tool_carries_its_own_parameter_schema(specs):
    """三样里最容易被糊弄的一行：每个工具的 schema 必须各归各的，全用同一个模型也照样能过上一条测试。"""
    by_name = {item.name: item for item in specs}
    expected = {
        "query_order": {"order_id"},
        "query_product": {"product_id", "keyword"},
        "query_logistics": {"order_id"},
        "query_faq": {"keyword", "category"},
        "create_ticket": {"description", "ticket_type"},
    }
    for name, fields in expected.items():
        assert set(by_name[name].json_schema["properties"]) == fields, name
    assert by_name["create_ticket"].description != by_name["query_order"].description


def test_registry_rejects_duplicate_names(specs):
    with pytest.raises(DuplicateToolError):
        ToolRegistry([*specs, specs[0]])


def test_missing_required_field_is_refused_at_construction(specs):
    base = dict(specs[0].__dict__)
    for field, empty in (("description", ""), ("json_schema", {})):
        broken = ToolSpec(**{**base, "name": f"坏工具-{field}", field: empty})
        with pytest.raises(ValueError, match=field):
            ToolRegistry([broken])


def test_registry_hands_back_the_same_table_it_was_given(specs):
    registry = ToolRegistry(specs)
    assert registry.names() == [item.name for item in specs]
    assert [item.name for item in registry.specs()] == registry.names()
    assert [item.name for item in registry.langchain_tools()] == registry.names()
    assert all(hasattr(item, "ainvoke") for item in registry.langchain_tools())
    assert registry.get("query_faq").label == "查询常见问题"
    assert registry.get("没有这个工具") is None


def test_merge_keeps_the_first_and_reports_what_it_dropped(specs):
    winner = specs[0]
    # clone 只改 name 的话它和 winner 完全等价，「保留最后一个但保持顺序」的实现照样能绿——
    # 所以必须在看得见的位置（label）上不同，并断言留下来的是 winner 这个对象本身。
    clone = ToolSpec(**{**winner.__dict__, "label": "冒牌同名工具"})
    kept, dropped = merge_specs([specs, [clone]])
    assert kept[0] is winner
    assert kept == list(specs)
    assert dropped == [winner.name]
```

- [ ] **Step 2: 跑到红**

Run: `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_tool_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.tools.builtin'; 'app.tools' is not a package`（实测：测试文件第 8 行先 import `builtin`，比 brief 原写的 `app.tools.registry` 更早炸；同一类「包还不存在」的红。评审已核：这条红反而更硬，因为它证明拆包前一个模块都没建。）。
（`FakeKnowledge` 定义在 `tests/test_tools.py`，本任务先直接从那里 import；T10 把工具相关夹具集中到 `tests/support.py` 时一并搬走。）

- [ ] **Step 3: 写 `app/tools/registry.py`**

```python
from dataclasses import dataclass
from typing import Sequence

from langchain_core.tools import BaseTool


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


def _check(spec: ToolSpec) -> None:
    missing = [key for key, value in (("description", spec.description),
                                      ("json_schema", spec.json_schema)) if not value]
    if missing:
        raise ValueError(f"工具 {spec.name} 缺少 {('、'.join(missing))}；宁可起不来，也不让模型见到空定义的工具。")


class ToolRegistry:
    def __init__(self, specs: Sequence[ToolSpec]) -> None:
        by_name: dict[str, ToolSpec] = {}
        for spec in specs:
            _check(spec)
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
```

- [ ] **Step 4: 搬家**

`app/tools/builtin.py` = 原 `app/tools.py` 的 `TOOL_LABELS`、`ORDERS/PRODUCTS/LOGISTICS`、`MISSING_TICKET_TYPE`、`list_orders`、四个 Args 模型、`make_tools` 原样搬入（逐字，不改一个字符），再加：

```python
def builtin_json_schemas() -> list[dict]:
    return [model.model_json_schema() for model in (OrderArgs, ProductArgs, FaqArgs, TicketArgs)]


def build_specs(store, conversation_id: int, knowledge=None, *, settings=None,
                 include_logistics: bool = True) -> list[ToolSpec]:
    """内置工具在每次构造上下文时登记进注册表：三样齐全，权限按本地规则给。

    `settings`（T5 的权限白名单）与 `include_logistics`（T9 内置物流工具下线）是留给后续任务的
    落点，本轮只走默认值。
    """
    tools = make_tools(store, conversation_id, knowledge)
    specs = []
    for name, item in tools.items():
        if name == "query_logistics" and not include_logistics:
            continue
        specs.append(ToolSpec(
            name=name,
            description=item.description or "",
            json_schema=item.args_schema.model_json_schema(),
            source="builtin",
            mcp_server=None,
            permission="write" if name in WRITE_TOOLS else "read",
            label=TOOL_LABELS.get(name, name),
            tool=item,
        ))
    return specs
```
`WRITE_TOOLS = {"create_ticket"}` 也放进 `builtin.py`（T5 的 `policy.py` 用 settings 里的白名单，这里是 spec 的缺省分类）。

`app/tools/legacy.py` = 原 `app/tools.py` 的 `ToolOutcome`、`ToolExecutor`、`_elapsed`、`summarize`、`MAX_SUMMARY`、`RESTRICTED_TOOLS` 逐字搬入，顶部注明 `"""旧执行器：T10 用 app.tools.engine 取代后删除。"""`。

`app/tools/__init__.py`：

```python
from app.tools.builtin import (
    ORDERS, PRODUCTS, LOGISTICS, TOOL_LABELS, FaqArgs, OrderArgs, ProductArgs, TicketArgs,
    builtin_json_schemas, build_specs, list_orders, make_tools,
)
from app.tools.legacy import MAX_SUMMARY, RESTRICTED_TOOLS, ToolOutcome, ToolExecutor, summarize
from app.tools.registry import DuplicateToolError, ToolRegistry, ToolSpec, merge_specs

__all__ = [name for name in dir() if not name.startswith("_")]
```

改 import：`react.py:15`、`agent_node.py:10`、`policy_nodes.py:7`、`main.py:38` 的 `from app.tools import ...` 保持字面不变（靠 `__init__.py` 兜住）；`react.py` 里 `ToolOutcome/ToolExecutor` 改从 `app.tools.legacy` 取，`context.py:60-62` 的四个模型改从 `app.tools.builtin` 取（`context` 依赖注册表会绕成环，必须直接指到 builtin）。最后 `git rm app/tools.py`。

- [ ] **Step 5: 跑到绿（含全量回归）**

Run: `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_tool_registry.py tests/test_tools.py tests/test_react.py tests/test_workflow_agent.py -q` → PASS；再全量 `LANGSMITH_TRACING=false .venv/Scripts/python.exe -X utf8 -m pytest -q` → 基线 323 ＋ 本任务 7 条 ＝ 330 全绿。零行为改动是本任务的验收线。
「零行为改动」的可核对口径：比对 `git show HEAD:app/tools.py` 的 245 条非空行与新包四模块，只允许 `from pydantic import ...` 一行按模块职责拆成两行，其余必须逐字出现。
**评审加强（dev-notes 阶段 7）**：brief 原写的 5 条里有 4 处断言拦不住实现错误——「所有 schema 用同一个模型」「`_check` 只查 description」「merge 保留最后一个但保序」「`langchain_tools()` 忘了拆 spec」四种改法当时都能绿。上表的测试块已按变异验证改写为 7 条：四条变异逐一实测变红（M1 `test_merge_…`、M2 `test_missing_…`、M3 `test_each_tool_…`、M4 `test_registry_hands_back_…`），还原后全绿。

- [ ] **Step 6: 本地提交** `git commit -am "refactor(ch08/T3): 工具层拆包并加注册中心（行为不变）"`

---

### Task 4: JSON Schema 参数校验

执行前统一校验，拦下不抛异常，把中文说明回灌模型。规格 §7。内置工具的跨字段约束（`ProductArgs.require_one`）JSON Schema 表达不了，留给 pydantic 在 T8 的分诊里兜。

**Files:**
- Create: `app/tools/validate.py`、`tests/test_tool_validate.py`
- Modify: `app/tools/builtin.py`（`TicketArgs.ticket_type` 用 `json_schema_extra` 把合法取值名单挂进 schema —— 下文「评审加强」段说明为什么这一步躲不掉）、`tests/test_context.py`（ch07 演示预算数字随 schema 变重算）

**Interfaces:**
- Consumes: `ToolSpec.json_schema`
- Produces:
  ```python
  @dataclass(frozen=True)
  class ValidationReport:
      ok: bool
      message: str          # 中文；ok=True 时为空串
  def validate_arguments(schema: dict, args: dict) -> ValidationReport
  ```

- [ ] **Step 1: 写失败测试**

```python
# tests/test_tool_validate.py
from app.tools.builtin import OrderArgs, ProductArgs, TicketArgs
from app.tools.validate import validate_arguments

ORDER = OrderArgs.model_json_schema()
TICKET = TicketArgs.model_json_schema()
PRODUCT = ProductArgs.model_json_schema()


def test_valid_arguments_pass_silently():
    assert validate_arguments(ORDER, {"order_id": "1001"}).ok is True
    assert validate_arguments(ORDER, {"order_id": "1001"}).message == ""


def test_missing_required_field_names_the_field_and_expectation():
    report = validate_arguments(ORDER, {})
    assert report.ok is False
    assert "order_id" in report.message
    assert "缺失" in report.message
    assert "订单号" in report.message            # 取自 schema 里的 description
    assert report.message.endswith("请向用户确认后重新调用。")


def test_wrong_type_is_reported_in_chinese():
    report = validate_arguments(ORDER, {"order_id": 1001})
    assert report.ok is False
    assert "order_id" in report.message and "string" in report.message and "int" in report.message


def test_every_missing_required_field_is_named_once():
    """两个字段同时缺失时不许把同一个字段报两遍——模型照着问就会问错话。"""
    report = validate_arguments(TICKET, {})
    assert "description 缺失" in report.message and "ticket_type 缺失" in report.message
    assert report.message.count("缺失") == 2
    assert "required property" not in report.message      # 英文原文不许漏进给模型的中文说明


def test_out_of_range_and_enum_violations_are_caught():
    too_short = validate_arguments(TICKET, {"description": "碎", "ticket_type": "售后"})
    assert too_short.ok is False and "description" in too_short.message
    assert "长度不在范围内" in too_short.message and "5–500" in too_short.message
    bad_enum = validate_arguments(TICKET, {"description": "杯子碎了想换货", "ticket_type": "退货"})
    assert bad_enum.ok is False and "售后" in bad_enum.message and "投诉" in bad_enum.message
    too_long = validate_arguments(TICKET, {"description": "长" * 600, "ticket_type": "售后"})
    assert too_long.ok is False and "500" in too_long.message


def test_unexpected_extra_field_is_rejected_for_builtin_schemas():
    report = validate_arguments(ORDER, {"order_id": "1001", "extra": 1})
    assert report.ok is False and "多余" in report.message
    assert "extra" in report.message and "Additional properties" not in report.message


def test_mcp_schema_is_used_verbatim_without_tightening_extra_properties():
    """规格 §7.1：只按 Server 给的 schema 校验，不注入 additionalProperties:false。"""
    schema = {"type": "object", "properties": {"order_id": {"type": "string", "description": "订单号"}},
              "required": ["order_id"]}
    assert validate_arguments(schema, {"order_id": "1001", "carrier": "SF"}).ok is True


def test_non_dict_input_is_refused():
    assert validate_arguments(ORDER, "1001").ok is False       # type: ignore[arg-type]


def test_product_needs_at_least_one_of_two_optionals_via_pydantic_not_schema():
    # JSON Schema 表达不了跨字段约束：这里必须放行，由引擎把 pydantic 错误归到参数类。
    assert validate_arguments(PRODUCT, {}).ok is True
```

- [ ] **Step 2: 跑到红**

Run: `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_tool_validate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.tools.validate'`。

- [ ] **Step 3: 写实现**

```python
# app/tools/validate.py
import re
from dataclasses import dataclass

from jsonschema import Draft202012Validator

_SUFFIX = "请向用户确认后重新调用。"
_REQUIRED_PATTERN = re.compile(r"'(?P<name>[^']+)' is a required property")
_NUMERIC_WORDS = {
    "minimum": "不小于",
    "exclusiveMinimum": "大于",
    "maximum": "不大于",
    "exclusiveMaximum": "小于",
}


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    message: str


def _spec_at(schema: dict, path: list) -> dict:
    node = schema
    for step in path:
        node = (node.get("properties") or {}).get(step) or {} if isinstance(node, dict) else {}
    return node


def _span(spec: dict) -> str:
    low, high = spec.get("minLength"), spec.get("maxLength")
    if low is not None and high is not None:
        return f"{low}–{high}"
    if low is not None:
        return f"至少 {low}"
    if high is not None:
        return f"最多 {high}"
    return "限定"


def _describe(error, schema: dict) -> str:
    path = list(error.absolute_path)
    field = ".".join(str(item) for item in path) or "（根）"
    spec = _spec_at(schema, path)
    hint = spec.get("description") or ""
    tail = f"，{hint}" if hint else ""
    kind = error.validator
    if kind == "required":
        # jsonschema 不给「缺的是哪一个」留结构位：schema_path 恒为 ['required']，validator_value 是整份
        # 清单，属性名只出现在 message 开头那个引号里（4.26.0 实测）。取不到就退回清单里第一个还没出现的键，
        # 绝不把英文原文塞进给模型的中文说明。
        matched = _REQUIRED_PATTERN.match(error.message)
        if matched:
            missing = matched.group("name")
        else:
            present = error.instance if isinstance(error.instance, dict) else {}
            names = error.validator_value if isinstance(error.validator_value, list) else []
            missing = next((name for name in names if name not in present), field)
        wanted = ((schema.get("properties") or {}).get(missing) or {}).get("description") or ""
        return f"{missing} 缺失（期望：{wanted or '必填项'}）"
    if kind == "type":
        expected = error.validator_value
        expected = "、".join(expected) if isinstance(expected, list) else str(expected)
        return f"{field} 类型不对（期望 {expected}，实际 {type(error.instance).__name__}）"
    if kind == "enum":
        return f"{field} 取值越界，只能是：{'、'.join(str(item) for item in error.validator_value)}"
    if kind == "additionalProperties":
        allowed = set(spec.get("properties") or {})
        offending = sorted(str(key) for key in (error.instance or {}) if key not in allowed)
        return f"{field} 带了多余的参数：{'、'.join(offending) or '未命名参数'}"
    if kind in {"minLength", "maxLength"}:
        return f"{field} 长度不在范围内（{_span(spec)} 字{tail}）"
    if kind in _NUMERIC_WORDS:
        return f"{field} 数值越界（应{_NUMERIC_WORDS[kind]} {error.validator_value}{tail}）"
    if kind == "pattern":
        return f"{field} 格式不符合要求（应匹配 {error.validator_value}）"
    return f"{field} 不符合 {kind} 校验规则"


def validate_arguments(schema: dict, args: dict) -> ValidationReport:
    if not isinstance(args, dict):
        return ValidationReport(False, "参数校验未通过：调用参数必须是对象（键值对）。" + _SUFFIX)
    errors = sorted(Draft202012Validator(schema).iter_errors(args),
                    key=lambda item: list(item.absolute_path))
    if not errors:
        return ValidationReport(True, "")
    lines = [_describe(error, schema) for error in errors]
    return ValidationReport(False, "参数校验未通过：" + "；".join(lines) + "。" + _SUFFIX)
```

- [ ] **Step 4: 跑到绿**

Run: `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_tool_validate.py -q` → PASS（9 passed：brief 8 条 ＋ 新增「两个必填同时缺失各报一次」）。首轮实测红了两处，都是上面代码块的缺陷先于测试暴露：(1) 原 `str(error.message).strip("'").split("'")[1]` 会把首引号去掉，输出 `" is a required property 缺失…"` —— jsonschema 的 required 错误只有 message 里带属性名，改用 `_REQUIRED_PATTERN` 取名；(2) 长度关键字是 `minLength`/`maxLength`（camelCase），写成 `min_length` 会让「500 字上限」那条永远匹配不上。另外 `TicketArgs.ticket_type` 的 pydantic schema 里没有 enum，见下文「评审加强」段。若长度或 required 断言红，先打印 `errors[0].message`/`error.validator` 核对 jsonschema 的措辞与关键字名，不要改测试期望迁就实现。

- [ ] **Step 5: 本地提交** `git commit -am "feat(ch08/T4): JSON Schema 校验与中文错误说明"`

**评审加强（dev-notes 阶段 8）**：三处计划缺陷在首轮 RED 里暴露，均已回改到上面的代码块——required 分支不能用 `strip("'")`（会切掉首引号），长度关键字是 camelCase 的 `minLength`/`maxLength`。第三处更关键：`TicketArgs.ticket_type` 的 pydantic JSON Schema 里**根本没有 enum**，三个合法取值只存在于 description 文本，`test_out_of_range_and_enum_violations_are_caught` 的 `bad_enum.ok` 因此为 True。修法是 `TICKET_TYPES` 常量 ＋ `Field(json_schema_extra={"enum": ...})`：只改 JSON Schema 的呈现，pydantic 运行时一字未动（`ticket_type=""` 仍走 `MISSING_TICKET_TYPE` 兜底，已实测通过）。代价是工具 schema 的固定预留变大，ch07 演示预算从 `(5650, 3954, 1695)` 变成 `(5638, 3946, 1691)`，只改 `tests/test_context.py` 的期望并注明来源；README 与 ch07 文档里的历史数字不动，交 T18 统一对账。另新增 `test_every_missing_required_field_is_named_once` 锁住「两个必填同时缺失各报一次」——原实现（对每个 required 错误都报第一个缺失项）会把同一字段报两遍。

独立评审后又修了两处 Important：(1) `additionalProperties` 与兜底分支原样回显 jsonschema 的英文 `error.message`，而规格 §7 要求回灌中文（新增断言 `"Additional properties" not in message`，并把兜底改成只报关键字名）；(2) `validate_arguments` 的 `args` 漏了 brief 要求的 `dict` 注解，已补。同时顺手修了长度/数值/嵌套路径的文案：`_spec_at` 按路径取字段 schema，长度只报实际存在的上下界，数值带上真实限值。变异验证：把 `additionalProperties` 改回回显英文 → 对应测试变红；把 `minLength`/`maxLength` 改回 snake_case → `too_short` 断言变红；还原即绿。

---

### Task 5: 权限判定

只读／写两分类，写操作三态放行／待确认／拒绝；外部 MCP 的自述一律不算数。规格 §8。

**Files:**
- Create: `app/tools/policy.py`、`tests/test_tool_policy.py`

**Interfaces:**
- Consumes: `ToolSpec`、`Settings.tool_write_allowlist`、`Settings.mcp_*_permission`
- Produces:
  ```python
  @dataclass(frozen=True)
  class PermissionDecision:
      allowed: bool
      needs_confirm: bool = False
      reason: str = ""
  def write_allowlist(settings) -> set[str]
  def decide(spec: ToolSpec, *, confirmed: bool, settings) -> PermissionDecision
  ```

- [ ] **Step 1: 写失败测试**

```python
# tests/test_tool_policy.py
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
```

- [ ] **Step 2: 跑到红** — Run: `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_tool_policy.py -q`；Expected: `ModuleNotFoundError: No module named 'app.tools.policy'`。

- [ ] **Step 3: 写实现**

```python
# app/tools/policy.py
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
```

- [ ] **Step 4: 跑到绿** — 同 Step 2 命令，Expected: 8 passed。
- [ ] **Step 5: 本地提交** `git commit -am "feat(ch08/T5): 工具权限三态判定"`

**评审加强（dev-notes 阶段 10–11）**：brief 原文 6 条测试与 Step 4 的「7 passed」对不上（计划缺陷）。补 `test_mcp_write_tool_listed_in_allowlist_still_needs_confirmation`：白名单里的 MCP 写工具不是默许，仍要用户确认——这条把「MCP 一律拒绝」的错误实现钉死（变异实测只它变红）。独立评审后又补 `test_unknown_permission_fails_closed_as_write`（未知 `permission` 走写路径而不是被当只读放行，变异实测变红）、断言 `needs_confirm` 理由包含「确认」、`settings()` 显式把 `tool_write_allowlist` 置空以隔离环境变量。`import pytest` 原文未使用，已删。

---

### Task 6: 结果格式化

挑字段、枚举翻人话、中文不转义、把 `ToolMessage`（内置的 JSON content、MCP 的 `structured_content` artifact）归一成 dict。规格 §9 格式化五条。

**Files:**
- Create: `app/tools/formatting.py`、`tests/test_tool_formatting.py`

**Interfaces:**
- Consumes: `ToolSpec`
- Produces:
  ```python
  MAX_SUMMARY = 60
  ENUM_LABELS: dict[str, dict[str, str]]
  PROJECTIONS: dict[str, dict[str, list[str]]]     # 工具名 -> {"data": [...], "items": [...]}
  def normalize_result(raw: object) -> object      # ToolMessage / JSON 文本 / 原样 dict
  def format_content(result: object, spec: ToolSpec) -> str   # ensure_ascii=False 的 JSON 字符串
  def summarize(spec: ToolSpec, result: object) -> str
  ```

- [ ] **Step 1: 写失败测试**

```python
# tests/test_tool_formatting.py
import json

from langchain_core.messages import ToolMessage

from app.tools.builtin import TOOL_LABELS  # build_specs 只在需要真 spec 的用例里用
from app.tools.formatting import ENUM_LABELS, format_content, normalize_result, summarize
from app.tools.registry import ToolSpec
from tests.test_tool_policy import make_tool


def spec(name="query_logistics", source="builtin", server=None, permission="read"):
    return ToolSpec(name=name, description="d", json_schema={"type": "object", "properties": {}},
                    source=source, mcp_server=server, permission=permission,
                    label=TOOL_LABELS.get(name, name), tool=make_tool(name))


def test_projection_keeps_only_useful_fields_and_human_labels():
    payload = {"ok": True, "data": {"order_id": "1001", "carrier": "SF", "status": "IN_TRANSIT",
                                    "internal_code": 99,
                                    "nodes": [{"time": "a", "text": "A"}, {"time": "b", "text": "B"}]}}
    projected = json.loads(format_content(payload, spec()))
    assert projected["data"]["status"] == "运输中"
    assert "internal_code" not in projected["data"]
    assert projected["data"]["latest_node"] == "B"
    assert projected["data"].pop("nodes") == [{"time": "b", "text": "B"}]


def test_chinese_is_not_escaped():
    assert "\\u" not in format_content({"ok": True, "data": {"items": [{"name": "保温杯"}]}},
                                       spec("query_product"))


def test_missing_projection_passes_the_payload_through_untouched():
    payload = {"ok": True, "data": {"whatever": [1, 2, 3]}}
    assert json.loads(format_content(payload, spec("brand_new_tool"))) == payload


def test_order_status_enum_is_translated():
    out = json.loads(format_content({"ok": True, "data": {"order_id": "1001", "status": "SHIPPED",
                                                          "amount": "199.00", "created_at": "x",
                                                          "items": [{"product_id": "P1001",
                                                                     "name": "保温杯", "quantity": 1,
                                                                     "note": "内部"}]}},
                                     spec("query_order")))
    assert out["data"]["status"] == ENUM_LABELS["order_status"]["SHIPPED"]
    assert "note" not in out["data"]["items"][0]


def test_tool_message_prefers_the_structured_artifact():
    """实测：adapters 的 artifact 是 {"structured_content": {...}} 包装，不是裸 payload。"""
    msg = ToolMessage(content='{"ok": true, "data": {"n": 1}}', tool_call_id="c1", name="m",
                      artifact={"structured_content": {"ok": True, "data": {"n": 2}}})
    assert normalize_result(msg) == {"ok": True, "data": {"n": 2}}


def test_artifact_is_enveloped_and_never_overrides_an_error():
    bare = ToolMessage(content='{"ok": true, "data": {"n": 1}}', tool_call_id="c1", name="m",
                      artifact={"structured_content": {"n": 2}})
    assert normalize_result(bare) == {"ok": True, "data": {"n": 2}}
    errored = ToolMessage(content="连接被拒绝", tool_call_id="c1", name="m", status="error",
                          artifact={"structured_content": {"ok": True, "data": {"n": 3}}})
    assert normalize_result(errored) == {"ok": False, "error": "tool_error", "message": "连接被拒绝"}


def test_tool_message_without_artifact_falls_back_to_the_content():
    assert normalize_result(ToolMessage(content='{"ok": true, "data": {"n": 1}}',
                                        tool_call_id="c1", name="m")) == {"ok": True, "data": {"n": 1}}
    assert normalize_result(ToolMessage(content="null", tool_call_id="c1", name="m")) == {"ok": True, "data": None}


def test_plain_text_content_survives_as_a_message():
    assert normalize_result(ToolMessage(content="查不到这单", tool_call_id="c1", name="m")) == {
        "ok": False, "error": "tool_error", "message": "查不到这单"}
    assert normalize_result(ToolMessage(content="连接被拒绝", tool_call_id="c1", name="m",
                                        status="error"))["message"] == "连接被拒绝"


def test_mcp_content_blocks_are_read_as_text():
    """真进程实测：FastMCP 的 dict 返回值到客户端是内容块列表，不是字符串。"""
    msg = ToolMessage(content=[{"type": "text", "text": '{\n  "ok": true,\n  "data": {"n": 4}\n}',
                                "id": "lc_1"}], tool_call_id="c1", name="query_logistics")
    assert normalize_result(msg) == {"ok": True, "data": {"n": 4}}


def test_builtin_dict_and_bare_json_string_both_normalize():
    assert normalize_result({"ok": True, "data": {"n": 1}}) == {"ok": True, "data": {"n": 1}}
    assert normalize_result('{"ok": true, "data": {"n": 3}}') == {"ok": True, "data": {"n": 3}}


def test_summaries_use_builtin_templates_and_generic_fallback():
    assert "2 条物流记录" in summarize(spec(), {"ok": True, "data": {"nodes": [{"text": "A"}, {"text": "上海"}]}})
    assert summarize(spec("query_faq"), {"ok": True, "data": {"items": [1, 2]}}) == "命中 2 条店铺政策"
    assert summarize(spec("create_ticket"), {"ok": True, "data": {"ticket_no": "T1"}}) == "工单 T1 已创建"
    generic = summarize(spec("query_station_info", source="mcp", server="logistics"),
                        {"ok": True, "data": {"items": [1, 2, 3]}})
    assert generic.startswith("查询物流：") and "3" in generic
    assert summarize(spec(), {"ok": False, "message": "没有找到订单 9999 的物流记录。"}) == "没有找到订单 9999 的物流记录。"
    assert len(summarize(spec(), {"ok": True, "data": {"nodes": [{"text": "长" * 200}]}})) <= 60


def test_every_summary_path_clips_to_60_and_list_data_does_not_crash():
    long_order = {"ok": True, "data": {"order_id": "9" * 64, "status": "SHIPPED"}}
    assert len(summarize(spec("query_order"), long_order)) <= 60
    generic = spec("query_station_info", source="mcp", server="logistics")
    assert summarize(generic, {"ok": True, "data": [1, 2, 3]}).startswith("查询物流：3")
    write = spec("create_refund", source="mcp", server="aftersale", permission="write")
    assert summarize(write, {"ok": True, "data": {"items": [1]}}).startswith("create_refund：")
    assert summarize(spec("brand_new_tool"), {"ok": True, "data": {}}) == "brand_new_tool：已执行"
```

- [ ] **Step 2: 跑到红** — Run: `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_tool_formatting.py -q`；Expected: `ModuleNotFoundError`。

- [ ] **Step 3: 写实现**

```python
# app/tools/formatting.py
import json

from langchain_core.messages import ToolMessage

MAX_SUMMARY = 60

# 内部枚举码 → 人话（现状散在 app/tools.py:12-18 与 app/workflow/actions.py:12-16，集中到这一处）
ENUM_LABELS = {
    "order_status": {"SHIPPED": "已发货", "PENDING": "待发货", "PAID": "已付款",
                     "CANCELLED": "已取消", "SIGNED": "已签收"},
    "logistics_status": {"IN_TRANSIT": "运输中", "PICKED_UP": "已揽收", "DELIVERED": "已签收",
                         "ABNORMAL": "异常"},
    "warranty_status": {"IN_WARRANTY": "在保", "OUT_OF_WARRANTY": "已过保"},
    "refund_status": {"PROCESSING": "处理中", "APPROVED": "已通过", "REJECTED": "已驳回",
                      "REFUNDED": "已退款"},
}

# 每个工具留哪些字段：data 顶层 + items 元素；不在册的工具原样透传，绝不因格式化丢关键字段。
PROJECTIONS = {
    "query_order": {"data": ["order_id", "status", "amount", "created_at", "items"],
                    "items": ["product_id", "name", "quantity"]},
    "query_logistics": {"data": ["order_id", "carrier", "tracking_no", "status"], "nodes": 1},
    "query_product": {"data": ["items"], "items": ["product_id", "name", "price", "stock", "highlights"]},
    "query_faq": {"data": ["items", "canonical_query", "low_confidence"],
                  "items": ["n", "id", "question", "answer", "category", "section_path", "content_type"]},
    "create_ticket": {"data": ["ticket_no", "status"]},
    "query_warranty": {"data": ["order_id", "status", "days_left", "expires_on"]},
    "query_refund_progress": {"data": ["order_id", "status", "updated_at", "remark"]},
}

_STATUS_ENUM_KEY = {"query_order": "order_status", "query_logistics": "logistics_status",
                    "query_warranty": "warranty_status", "query_refund_progress": "refund_status"}

_GENERIC_COUNT_KEYS = ("items", "nodes", "list", "records")

# MCP 未登记中文名的工具用 Server 级中文名兜底，避免摘要里出现英文工具名。
_SERVER_LABELS = {"logistics": "查询物流", "aftersale": "查询售后"}


def _translate(tool: str, value):
    key = _STATUS_ENUM_KEY.get(tool)
    if not key or not isinstance(value, str):
        return value
    return ENUM_LABELS[key].get(value.upper(), value)


def _summary_label(spec) -> str:
    if spec.label and spec.label != spec.name:
        return spec.label
    if spec.permission == "read":
        return _SERVER_LABELS.get(spec.mcp_server or "", spec.label or spec.name)
    return spec.label or spec.name


def _text_of(content) -> str:
    """ToolMessage.content 实测有三种：str（内置）、内容块 list（MCP 文本块）、其他。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return str(content)


def _from_text(text: str, *, errored: bool) -> object:
    if errored:
        return {"ok": False, "error": "tool_error", "message": text[:500] or "工具执行失败。"}
    try:
        parsed = json.loads(text)
    except ValueError:
        # 非 JSON 的裸文本按坏消息如实回灌，模型能直接引用给用户
        return {"ok": False, "error": "tool_error", "message": text[:500] or "工具没有返回内容。"}
    return parsed if isinstance(parsed, dict) else {"ok": True, "data": parsed}


def normalize_result(raw):
    """引擎统一用 tool_call 形状调用，回来的是 ToolMessage。

    实测（.venv，langchain-core 1.7.0 + adapters 0.3.2 + mcp 1.30.0 真进程）：
      · `ainvoke(args_dict)` 对内置返回 dict、对 MCP 只返回 content 字符串，artifact 被丢掉；
      · `ainvoke(tool_call)` 两种来源都返回 ToolMessage；
      · 内置的 content 是紧凑 JSON 字符串，MCP 的 content 是**内容块列表**
        `[{"type": "text", "text": "{...}"}]`（FastMCP 用 indent=2 序列化，中文不转义）；
      · `artifact` 只有 Server 给出 `structuredContent` 时才有，且形如
        `{"structured_content": {...}}`（TypedDict 包装）——我们自己的 Server 返回 `dict`
        实测拿不到，所以主路是解析 content，artifact 只当外部 Server 愿意给时的优先项。
    """
    if isinstance(raw, ToolMessage):
        if raw.status == "error":
            return _from_text(_text_of(raw.content), errored=True)
        artifact = raw.artifact if isinstance(raw.artifact, dict) else None
        structured = (artifact or {}).get("structured_content")
        if isinstance(structured, dict):
            return structured if "ok" in structured else {"ok": True, "data": structured}
        if isinstance(structured, list):
            return {"ok": True, "data": structured}
        return _from_text(_text_of(raw.content), errored=False)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, list)):
        return _from_text(_text_of(raw), errored=False)
    return {"ok": True, "data": raw}


def _project(tool: str, result: dict) -> dict:
    rule = PROJECTIONS.get(tool)
    if not rule or not isinstance(result.get("data"), dict):
        return result
    data = result["data"]
    keep = {key: data[key] for key in rule.get("data", []) if key in data}
    if "status" in keep:
        keep["status"] = _translate(tool, keep["status"])
    if "items" in keep and isinstance(keep["items"], list):
        fields = rule.get("items", [])
        keep["items"] = [{key: item[key] for key in fields if key in item} for item in keep["items"]
                         if isinstance(item, dict)]
    if "nodes" in data and isinstance(data["nodes"], list) and data["nodes"]:
        rule_nodes = rule.get("nodes")
        keep["nodes"] = data["nodes"][-rule_nodes:] if rule_nodes else data["nodes"]
        latest = data["nodes"][-1]
        if isinstance(latest, dict) and "text" in latest:
            keep["latest_node"] = latest["text"]
    return {**result, "data": keep}


def format_content(result, spec) -> str:
    payload = _project(spec.name, result) if isinstance(result, dict) else result
    return json.dumps(payload, ensure_ascii=False, default=str)

def _count(data: dict) -> int | None:
    for key in _GENERIC_COUNT_KEYS:
        if isinstance(data.get(key), list):
            return len(data[key])
    return None


def _clip(text: str) -> str:
    return str(text)[:MAX_SUMMARY]


def summarize(spec, result) -> str:
    if not isinstance(result, dict) or not result.get("ok"):
        message = (result or {}).get("message") if isinstance(result, dict) else None
        return _clip(message or "工具没有返回结果。")
    data = result.get("data")
    if isinstance(data, list):
        data = {"items": data}
    elif not isinstance(data, dict):
        data = {}
    name = spec.name
    if name == "query_order":
        return _clip(f"订单 {data.get('order_id')} {data.get('status')}")
    if name == "query_logistics":
        nodes = data.get("nodes") or []
        latest = nodes[-1].get("text") if nodes and isinstance(nodes[-1], dict) else "暂无节点"
        return _clip(f"{len(nodes)} 条物流记录，最新：{latest}")
    if name == "query_product":
        items = data.get("items") or []
        first = items[0].get("name") if items and isinstance(items[0], dict) else None
        return f"{len(items)} 个商品匹配：{first}" if first else "未找到商品"
    if name == "query_faq":
        return _clip(f"命中 {len(data.get('items') or [])} 条店铺政策")
    if name == "create_ticket":
        return _clip(f"工单 {data.get('ticket_no')} 已创建")
    count = _count(data)
    if count is not None:
        return _clip(f"{_summary_label(spec)}：{count} 条结果")
    key = next((f"{k}={v}" for k, v in data.items() if isinstance(v, (str, int))), "")
    return _clip(f"{_summary_label(spec)}：{key}") if key else _clip(f"{_summary_label(spec)}：已执行")
```

- [ ] **Step 4: 跑到绿** — 同 Step 2 命令，Expected: 12 passed。
- [ ] **Step 5: 本地提交** `git commit -am "feat(ch08/T6): 数据驱动的结果格式化"`

**实现偏差（dev-notes 阶段 12）**：计划代码块漏了 `from langchain_core.messages import ToolMessage`（`normalize_result` 必用），且 `PROJECTIONS["query_order"]` 的 `data` 没列 `items`，`_project` 会先把 `items` 整个丢掉，`test_order_status_enum_is_translated` 的「`items[0]` 不含 `note`」必炸。两处已回改。另：`test_summaries_…` 要求未登记中文名的 MCP 工具摘要以「查询物流：」开头，而 `ToolSpec.label` 此时是英文工具名，故新增 `_SERVER_LABELS`（logistics→查询物流、aftersale→查询售后）在 `label` 未中文化时兜底；变异「删掉 Server 兜底」实测让该用例变红。

**T6 评审修复（dev-notes 阶段 13）**：独立评审提了 3 条 Important 并全部落实——① `summarize` 遇 `data` 为 list 直接崩，改为先 `isinstance(data, list)` 视作 `{"items": data}`、非 dict 置空；② 4 条模板返回路径（query_order/query_product/query_faq/create_ticket）没截 60 字，统一走 `_clip()`；③ artifact 分支绕过 envelope 且无视 `status == "error"`，改为先判 error 再包 `{"ok": True, "data": ...}`。Minor 也一并修：nodes/items 用 `.get` 防护、`_SERVER_LABELS` 兜底只对 `permission == "read"`（写工具保留真名以免把写操作说成「查询」）、`query_order` 投影补回 `product_id`。计划代码块已同步为最终实现。变异 M4–M7 逐一实测变红、还原即绿。

---

### Task 7: 审计写入

每次调用一条；被拒被拦也落；写失败绝不反过来拦住工具执行。规格 §5、§15。

**Files:**
- Create: `app/tools/audit.py`、`tests/test_tool_audit.py`

**Interfaces:**
- Consumes: `models.ToolAuditLog`、`sessionmaker`
- Produces:
  ```python
  @dataclass
  class AuditRecord:
      tool: str; source: str; mcp_server: str | None; arguments: dict
      status: str; summary: str; error: str | None; retry_count: int; duration_ms: int
      tool_call_id: str | None; conversation_id: int | None
  class ToolAuditWriter:
      def __init__(self, session_factory, *, enabled: bool = True) -> None
      def record(self, item: AuditRecord) -> None      # 同步、绝不抛
  ```

- [ ] **Step 1: 写失败测试**

```python
# tests/test_tool_audit.py
import pytest
from sqlalchemy import create_engine, select

from app.database import build_session_factory
from app.models import Base, ToolAuditLog
from app.tools.audit import AuditRecord, ToolAuditWriter


@pytest.fixture()
def writer(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    yield ToolAuditWriter(factory), factory
    engine.dispose()


def record(**overrides):
    payload = {"tool": "query_order", "source": "builtin", "mcp_server": None,
               "arguments": {"order_id": "1001"}, "status": "成功", "summary": "订单 1001 已发货",
               "error": None, "retry_count": 0, "duration_ms": 12,
               "tool_call_id": "call_1", "conversation_id": 7}
    payload.update(overrides)
    return AuditRecord(**payload)


def test_every_call_lands_exactly_one_row(writer):
    audit, factory = writer
    for status in ("成功", "失败", "超时", "校验拦下", "权限拒绝"):
        audit.record(record(status=status))
    with factory() as session:
        rows = list(session.scalars(select(ToolAuditLog).order_by(ToolAuditLog.id)))
    assert [row.status for row in rows] == ["成功", "失败", "超时", "校验拦下", "权限拒绝"]
    assert rows[0].tool_source == "builtin" and rows[0].arguments == {"order_id": "1001"}
    assert rows[0].created_at is not None


def test_mcp_rows_carry_the_server_name(writer):
    audit, factory = writer
    audit.record(record(source="mcp", mcp_server="logistics", tool="query_logistics"))
    with factory() as session:
        row = session.scalars(select(ToolAuditLog)).one()
    assert (row.tool_source, row.mcp_server) == ("mcp", "logistics")


def test_null_conversation_is_allowed(writer):
    audit, factory = writer
    audit.record(record(conversation_id=None, tool_call_id=None))
    with factory() as session:
        assert session.scalars(select(ToolAuditLog)).one().conversation_id is None


def test_disabled_writer_writes_nothing(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'off.db'}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    ToolAuditWriter(factory, enabled=False).record(record())
    with factory() as session:
        assert list(session.scalars(select(ToolAuditLog))) == []
    engine.dispose()


def test_failure_to_write_never_raises(tmp_path, caplog):
    engine = create_engine(f"sqlite:///{tmp_path / 'bad.db'}")
    Base.metadata.create_all(engine)

    class Broken:
        def __call__(self):
            raise RuntimeError("库挂了")

    with caplog.at_level("WARNING"):
        ToolAuditWriter(Broken()).record(record())        # 不许抛
        ToolAuditWriter(None).record(record())            # 没 factory 也不许抛
    assert "审计写入失败" in caplog.text
    engine.dispose()


def test_database_error_is_swallowed_and_retried_once(tmp_path, caplog):
    engine = create_engine(f"sqlite:///{tmp_path / 'missing.db'}")   # 故意不建表
    factory = build_session_factory(engine)
    calls = []

    def counting_factory():
        calls.append(1)
        return factory()

    with caplog.at_level("WARNING"):
        ToolAuditWriter(counting_factory).record(record())
    assert len(calls) == 2
    assert "attempt=2" in caplog.text
    engine.dispose()


def test_long_summary_and_error_are_truncated(writer):
    audit, factory = writer
    audit.record(record(summary="长" * 5000, error="错" * 5000))
    with factory() as session:
        row = session.scalars(select(ToolAuditLog)).one()
    assert len(row.result_summary) <= 2000
    assert len(row.error_message) <= 512


def test_full_row_round_trips_every_operational_field(writer):
    """T17 要按这些列核对验收，默认值能落库不代表字段真写进去了。"""
    audit, factory = writer
    audit.record(record(source="mcp", mcp_server="logistics", tool="query_logistics",
                        conversation_id=42, tool_call_id="call_42", retry_count=2,
                        duration_ms=321, summary="2 条物流记录，最新：已揽收"))
    with factory() as session:
        row = session.scalars(select(ToolAuditLog)).one()
    assert (row.tool_name, row.tool_source, row.mcp_server) == ("query_logistics", "mcp", "logistics")
    assert (row.conversation_id, row.tool_call_id) == (42, "call_42")
    assert (row.retry_count, row.duration_ms) == (2, 321)
    assert row.result_summary == "2 条物流记录，最新：已揽收"


def test_source_is_inferred_from_server_when_the_source_string_is_unknown(writer):
    audit, factory = writer
    audit.record(record(source="外部", mcp_server="aftersale"))
    with factory() as session:
        row = session.scalars(select(ToolAuditLog)).one()
    assert row.tool_source == "mcp"


def test_unserialisable_arguments_are_dropped_on_the_retry_so_the_status_row_survives(writer):
    audit, factory = writer
    audit.record(record(arguments={"x": object()}, summary="长" * 5000))
    with factory() as session:
        row = session.scalars(select(ToolAuditLog)).one()
    assert row.arguments is None and row.status == "成功"
    assert len(row.result_summary) == 2000          # 降级只丢载荷，不丢摘要


def test_string_columns_are_clamped_to_their_declared_widths(writer):
    audit, factory = writer
    audit.record(record(tool="t" * 200, tool_call_id="c" * 100, mcp_server="m" * 100,
                        source="mcp", error="e" * 1000, summary="短"))
    with factory() as session:
        row = session.scalars(select(ToolAuditLog)).one()
    assert (len(row.tool_name), len(row.tool_call_id), len(row.mcp_server)) == (128, 64, 64)
    assert len(row.error_message) == 512


def test_unknown_status_is_dropped_with_a_warning(writer, caplog):
    """MySQL 的 status 是 ENUM：写进去只会整行失败，重试也救不回，不如在本地就拦住。"""
    audit, factory = writer
    with caplog.at_level("WARNING"):
        audit.record(record(status="乱写"))
    assert rows_of(factory) == []
    assert "未知状态" in caplog.text


def rows_of(factory):
    with factory() as session:
        return list(session.scalars(select(ToolAuditLog)))


def test_commit_phase_failure_rebuilds_the_row_and_lands_once(tmp_path):
    """commit 阶段失败过的实例带着 identity 变 detached，复用它会变成 UPDATE 并抛 StaleDataError（评审实测）。"""
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    engine = create_engine(f"sqlite:///{tmp_path / 'commit.db'}")
    Base.metadata.create_all(engine)
    state = {"first": True}

    class FlakySession(Session):
        def commit(self):
            if state["first"]:
                state["first"] = False
                super().flush()                      # 行已 INSERT、主键已回填，随后 commit 失败
                raise OperationalError("commit", {}, Exception("连接断了"))
            return super().commit()

    class FlakyFactory:
        def __call__(self):
            return FlakySession(bind=engine)

    ToolAuditWriter(FlakyFactory()).record(record())
    with engine.connect() as connection:
        count = connection.execute(select(ToolAuditLog)).all()
    assert len(count) == 1
    engine.dispose()


def test_none_counters_fall_back_to_zero(writer):
    audit, factory = writer
    audit.record(record(retry_count=None, duration_ms=None))
    with factory() as session:
        row = session.scalars(select(ToolAuditLog)).one()
    assert (row.retry_count, row.duration_ms) == (0, 0)
```

- [ ] **Step 2: 跑到红** — Run: `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_tool_audit.py -q`；Expected: `ModuleNotFoundError: No module named 'app.tools.audit'`。

- [ ] **Step 3: 写实现**

```python
# app/tools/audit.py
import logging
from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError

from app.models import ToolAuditLog

logger = logging.getLogger(__name__)

MAX_SUMMARY_COLUMN = 2000
MAX_ERROR_COLUMN = 512
STATUSES = frozenset({"成功", "失败", "超时", "校验拦下", "权限拒绝"})


@dataclass
class AuditRecord:
    tool: str
    source: str
    mcp_server: str | None
    arguments: dict
    status: str
    summary: str
    error: str | None
    retry_count: int
    duration_ms: int
    tool_call_id: str | None
    conversation_id: int | None


class ToolAuditWriter:
    """审计是留痕不是闸门：任何失败只记日志，工具调用结果照常返回（规格 §15）。"""

    def __init__(self, session_factory, *, enabled: bool = True) -> None:
        self.session_factory = session_factory
        self.enabled = enabled

    def record(self, item: AuditRecord) -> None:
        if not self.enabled or self.session_factory is None:
            return
        if item.status not in STATUSES:
            logger.warning("工具审计写入失败（未知状态）：tool=%s status=%r", item.tool, item.status)
            return
        for attempt in (1, 2):
            try:
                # 每次尝试都重建行对象：commit 阶段失败过的实例会带着 identity 变 detached，
                # 再 add 会被当成 UPDATE，第二次必抛 StaleDataError（评审实测）。
                row = self._row(item, drop_arguments=attempt == 2)
            except Exception:
                logger.warning("工具审计写入失败（字段无法落库）：tool=%s status=%s",
                               item.tool, item.status, exc_info=True)
                return
            try:
                with self.session_factory() as session:
                    session.add(row)
                    session.commit()
                return
            except SQLAlchemyError:
                logger.warning("工具审计写入失败（数据库）：tool=%s status=%s attempt=%s",
                               item.tool, item.status, attempt, exc_info=True)
            except Exception:
                logger.warning("工具审计写入失败：tool=%s status=%s", item.tool, item.status, exc_info=True)
                return

    @staticmethod
    def _row(item: AuditRecord, *, drop_arguments: bool = False) -> ToolAuditLog:
        if item.source in ("builtin", "mcp"):
            source = item.source
        else:
            source = "mcp" if item.mcp_server else "builtin"
        return ToolAuditLog(
            conversation_id=item.conversation_id,
            tool_call_id=(item.tool_call_id or None) and str(item.tool_call_id)[:64],
            tool_name=item.tool[:128],
            tool_source=source,
            mcp_server=(item.mcp_server or None) and str(item.mcp_server)[:64],
            arguments=None if drop_arguments else (item.arguments if isinstance(item.arguments, dict) else None),
            result_summary=(item.summary or "")[:MAX_SUMMARY_COLUMN] or None,
            status=item.status,
            error_message=(item.error or None) and str(item.error)[:MAX_ERROR_COLUMN],
            retry_count=max(0, int(item.retry_count or 0)),
            duration_ms=max(0, int(item.duration_ms or 0)),
        )
```

- [ ] **Step 4: 跑到绿** — 同 Step 2 命令，Expected: 14 passed。
- [ ] **Step 5: 本地提交** `git commit -am "feat(ch08/T7): 工具审计写入器"`

**T7 评审修复（dev-notes 阶段 15）**：独立评审 3 Important 全部落实——① 跨 session 复用同一个 ORM 行在 commit 阶段失败后会变 detached＋带 identity，第二次 `add` 变成 UPDATE 抛 `StaleDataError`，改为每次尝试 `_row()` 重建（新增 commit 阶段失败用例，用假 Session 在 flush 后抛错复现）；② 降级路径原来只测「调两次」，补 `arguments={"x": object()}` 用例，断言落 1 行且 `arguments is None`；③ 关键列（tool_name/tool_call_id/conversation_id/retry_count/duration_ms/summary）与 128/64/512 截断全无断言，补齐并逐项变异。Minor：去掉重试时把 summary 砍到 500 的无谓损失（规格是 2000）；`status` 不在五态内直接丢弃并记 WARNING（MySQL ENUM 写不进去，重试救不回）；`source` 非法但带 `mcp_server` 时归 `mcp`。

---

### Task 8: 统一执行引擎

校验→权限→执行（超时/重试矩阵）→分诊→格式化→审计，一处走完。规格 §8、§9。`confirm_required` 不落审计（§5 映射表），其余四类必落。

**Files:**
- Create: `app/tools/engine.py`、`tests/test_tool_engine.py`

**Interfaces:**
- Consumes: `ToolRegistry`、`validate_arguments`、`decide`、`formatting`、`ToolAuditWriter`、`Settings`
- Produces:
  ```python
  @dataclass(frozen=True)
  class ToolOutcome:
      tool: str; ok: bool; code: str; content: str; summary: str; duration_ms: int
      source: str = "builtin"; mcp_server: str | None = None; retry_count: int = 0
      audit_status: str = "成功"; arguments: dict = field(default_factory=dict)
      tool_call_id: str | None = None
  class ToolEngine:
      def __init__(self, registry, *, settings, audit=None) -> None
      async def run(self, *, name: str, args: dict, tool_call_id: str | None = None,
                    conversation_id: int | None = None, confirmed: bool = False) -> ToolOutcome
      def record_denial(self, *, name: str, args: dict, reason: str,
                        tool_call_id: str | None = None, conversation_id: int | None = None) -> None
  ```
  执行一律走 `spec.tool.ainvoke({"type": "tool_call", "name", "args", "id"})`：实测只有这个形状能让
  MCP 工具的 `artifact` 回来（`ainvoke(args_dict)` 只给 content 字符串，内置工具反而给 dict），
  两种来源统一成 `ToolMessage` 一条路。`tool_call_id` 缺省时引擎自己造一个，保证审计能对齐；
  `ToolOutcome.tool_call_id` 把实际用的那个 id 回给调用方，T13 的 pending 卡片靠它对账。

  评审后补的三条口径（都写进测试）：
  - **写操作失败一律报「结果未知」**：同步工具被 LangChain 丢进 `run_in_executor`，`asyncio.wait_for`
    超时只取消 `await`，线程照跑，库里可能已经改了。写工具的 `tool_timeout`／`CancelledError`／
    普通异常统一用 `WRITE_UNKNOWN_RESULT`（「可能已经执行，请勿重复提交，稍后人工核对」）。
  - **`ok=False` 必须带得走原因**：业务失败（`{"ok": false}`）也要把 message 写进审计 `error_message`。
  - **校验自身会炸**：外部 MCP Server 送来的 schema 可能是非法 JSON Schema（`{"type": "nonsense"}`
    会抛 `jsonschema.exceptions.UnknownType`），`validate_arguments` 必须包 try，炸了按
    `invalid_arguments` 拒绝，不拖垮整轮。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_tool_engine.py
import asyncio
import json
import time

import pytest
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.database import build_session_factory
from app.models import Base, ToolAuditLog
from app.tools.audit import ToolAuditWriter
from app.tools.engine import ToolEngine
from app.tools.registry import ToolSpec


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(min_length=1, max_length=64, description="订单号")


def spec_of(handle, *, name, permission="read", source="builtin", server=None, label="测试"):
    return ToolSpec(name=name, description=handle.description,
                    json_schema=handle.args_schema.model_json_schema() if isinstance(
                        handle.args_schema, type) else handle.args_schema,
                    source=source, mcp_server=server, permission=permission, label=label, tool=handle)


def settings(**overrides):
    base = {"openai_base_url": "http://upstream.invalid/v1", "openai_model": "m", "openai_api_key": "k",
            "tool_timeout_seconds": 0.05, "tool_max_attempts": 2}
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture()
def audit(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'engine.db'}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    yield ToolAuditWriter(factory), factory
    engine.dispose()


def rows(factory):
    with factory() as session:
        return list(session.scalars(select(ToolAuditLog).order_by(ToolAuditLog.id)))


def ok_tool(name="query_order"):
    @tool(name, args_schema=Args, description="按订单号查询。")
    def handle(order_id: str) -> dict:
        return {"ok": True, "data": {"order_id": order_id, "status": "SHIPPED"}}
    return handle


def run(engine, **kwargs):
    return asyncio.run(engine.run(**kwargs))


def test_schema_only_violation_is_caught_before_pydantic(tmp_path):
    """JSON Schema 比 pydantic 更严时必须先拦在引擎这层，而不是靠 LangChain 的参数校验兜。"""
    engine_db = create_engine(f"sqlite:///{tmp_path / 'schema.db'}")
    Base.metadata.create_all(engine_db)
    factory = build_session_factory(engine_db)
    calls = []

    @tool("query_order", args_schema=Args, description="按订单号查询。")
    def spy(order_id: str) -> dict:
        calls.append(order_id)
        return {"ok": True}

    strict = Args.model_json_schema()
    strict["properties"]["order_id"]["maxLength"] = 4          # 只写进 JSON Schema，pydantic 仍允许 64
    spec = ToolSpec(name="query_order", description="按订单号查询。", json_schema=strict,
                    source="builtin", mcp_server=None, permission="read", label="查询订单", tool=spy)
    engine = ToolEngine(_registry([spec]), settings=settings(), audit=ToolAuditWriter(factory))
    outcome = run(engine, name="query_order", args={"order_id": "12345"})
    assert outcome.code == "invalid_arguments" and calls == []
    assert "长度不在范围内" in outcome.content
    engine_db.dispose()


def test_unknown_tool_is_reported_and_not_audited_as_failure_of_a_real_tool(audit):
    writer, factory = audit
    engine = ToolEngine(_registry([]), settings=settings(), audit=writer)
    outcome = run(engine, name="query_weather", args={})
    assert outcome.ok is False and outcome.code == "unknown_tool"
    assert rows(factory) == []           # 表里没这个工具，落审计只会脏数据（决定见 §18-C3 的口径）


def _registry(specs):
    from app.tools.registry import ToolRegistry
    return ToolRegistry(specs)


def test_validation_blocks_before_execution_and_lands_as_checked(audit):
    writer, factory = audit
    calls = []

    @tool("query_order", args_schema=Args, description="按订单号查询。")
    def spy(order_id: str) -> dict:
        calls.append(order_id)
        return {"ok": True}

    engine = ToolEngine(_registry([spec_of(spy, name="query_order")]), settings=settings(), audit=writer)
    outcome = run(engine, name="query_order", args={"order_id": ""}, conversation_id=3)
    assert outcome.ok is False and outcome.code == "invalid_arguments"
    assert "order_id" in outcome.content and calls == []
    assert rows(factory)[0].status == "校验拦下"


def test_write_without_confirmation_returns_confirm_required_and_writes_nothing(audit):
    writer, factory = audit

    @tool("create_ticket", args_schema=Args, description="建工单。")
    def handle(order_id: str) -> dict:
        raise AssertionError("未确认不许执行")

    engine = ToolEngine(_registry([spec_of(handle, name="create_ticket", permission="write")]),
                        settings=settings(), audit=writer)
    outcome = run(engine, name="create_ticket", args={"order_id": "1001"})
    assert outcome.ok is False and outcome.code == "confirm_required"
    assert outcome.audit_status == "confirm_required"
    assert rows(factory) == []


def test_write_outside_allowlist_is_denied_and_audited(audit):
    writer, factory = audit

    @tool("delete_order", args_schema=Args, description="删单。")
    def handle(order_id: str) -> dict:
        raise AssertionError("白名单外不许执行")

    engine = ToolEngine(_registry([spec_of(handle, name="delete_order", permission="write")]),
                        settings=settings(), audit=writer)
    outcome = run(engine, name="delete_order", args={"order_id": "1001"}, confirmed=True)
    assert outcome.ok is False and outcome.code == "permission_denied"
    assert rows(factory)[0].status == "权限拒绝"


def test_record_denial_leaves_a_permission_row_without_executing(audit):
    """用户在卡片上点「取消」：调用根本没发生，但第 5 条要求这次也留痕（T13 的取消分支靠它）。"""
    writer, factory = audit

    @tool("create_ticket", args_schema=Args, description="建工单。")
    def never(order_id: str) -> dict:
        raise AssertionError("取消不该执行工具")

    engine = ToolEngine(_registry([spec_of(never, name="create_ticket", permission="write")]),
                        settings=settings(), audit=writer)
    engine.record_denial(name="create_ticket", args={"order_id": "1001"},
                         reason="用户在页面取消了这次操作。", tool_call_id="call_9", conversation_id=3)
    row = rows(factory)[0]
    assert (row.status, row.tool_call_id, row.retry_count) == ("权限拒绝", "call_9", 0)
    assert row.arguments == {"order_id": "1001"}


def test_record_denial_of_an_unregistered_tool_writes_nothing(audit):
    writer, factory = audit
    engine = ToolEngine(_registry([]), settings=settings(), audit=writer)
    engine.record_denial(name="ghost_tool", args={}, reason="取消")
    assert rows(factory) == []


def test_read_tool_retries_transient_failure_then_succeeds(audit):
    writer, factory = audit
    seen = []

    @tool("query_order", args_schema=Args, description="查询。")
    def flaky(order_id: str) -> dict:
        seen.append(order_id)
        if len(seen) == 1:
            raise OperationalError("select 1", {}, Exception("忙"))
        return {"ok": True, "data": {"order_id": order_id}}

    engine = ToolEngine(_registry([spec_of(flaky, name="query_order")]), settings=settings(), audit=writer)
    outcome = run(engine, name="query_order", args={"order_id": "1001"})
    assert outcome.ok is True and seen == ["1001", "1001"] and outcome.retry_count == 1
    assert rows(factory)[0].retry_count == 1 and rows(factory)[0].status == "成功"


def test_business_empty_result_is_not_retried(audit):
    writer, factory = audit
    seen = []

    @tool("query_order", args_schema=Args, description="查询。")
    def empty(order_id: str) -> dict:
        seen.append(order_id)
        return {"ok": False, "error": "not_found", "message": f"没有找到订单 {order_id}。"}

    engine = ToolEngine(_registry([spec_of(empty, name="query_order")]), settings=settings(), audit=writer)
    outcome = run(engine, name="query_order", args={"order_id": "9999"})
    assert outcome.ok is False and outcome.code == "not_found" and seen == ["9999"]
    row = rows(factory)[0]
    assert row.status == "失败"
    assert row.error_message == "没有找到订单 9999。"        # 业务失败也要留下原因，否则审计看不出为什么失败


def test_write_timeout_is_not_retried_and_retry_count_is_zero(audit):
    """验收⑥后半段：写操作超时绝不自动重试，重复执行比失败更糟。"""
    writer, factory = audit
    seen = []

    @tool("create_ticket", args_schema=Args, description="建工单。")
    def slow(order_id: str) -> dict:
        seen.append(order_id)
        time.sleep(0.2)
        return {"ok": True}

    engine = ToolEngine(_registry([spec_of(slow, name="create_ticket", permission="write")]),
                        settings=settings(), audit=writer)
    outcome = run(engine, name="create_ticket", args={"order_id": "1001"}, confirmed=True)
    assert outcome.ok is False and outcome.code == "tool_timeout" and len(seen) == 1
    # 同步工具跑在 executor 线程里，超时只是不再等：任务可能已经落库，必须如实报「结果未知」。
    assert "可能已经执行" in outcome.content and "重复提交" in outcome.content
    assert "可能已经执行" in outcome.summary
    row = rows(factory)[0]
    assert row.status == "超时" and row.retry_count == 0


def test_read_timeout_retries_and_reports_attempts(audit):
    writer, factory = audit
    seen = []

    @tool("query_order", args_schema=Args, description="查询。")
    def slow(order_id: str) -> dict:
        seen.append(order_id)
        time.sleep(0.2)
        return {"ok": True}

    engine = ToolEngine(_registry([spec_of(slow, name="query_order")]), settings=settings(), audit=writer)
    outcome = run(engine, name="query_order", args={"order_id": "1001"})
    assert outcome.code == "tool_timeout" and len(seen) == 2
    row = rows(factory)[0]
    assert row.status == "超时" and row.retry_count == 1 and row.duration_ms > 0


def test_pydantic_cross_field_rules_are_triaged_as_invalid_arguments(audit):
    writer, factory = audit

    class ProductArgs(BaseModel):
        model_config = ConfigDict(extra="forbid")
        product_id: str | None = Field(default=None, description="商品 ID")
        keyword: str | None = Field(default=None, description="关键词")

        from pydantic import model_validator

        @model_validator(mode="after")
        def require_one(self):
            if not (self.product_id or self.keyword):
                raise ValueError("product_id 和 keyword 至少填写一个。")
            return self

    @tool("query_product", args_schema=ProductArgs, description="查商品。")
    def handle(product_id=None, keyword=None) -> dict:
        raise AssertionError("JSON Schema 放行、pydantic 拦住，不该执行到这里")

    spec = ToolSpec(name="query_product", description="查商品。",
                    json_schema=ProductArgs.model_json_schema(), source="builtin", mcp_server=None,
                    permission="read", label="查询商品", tool=handle)
    engine = ToolEngine(_registry([spec]), settings=settings(), audit=writer)
    outcome = run(engine, name="query_product", args={})
    assert outcome.ok is False and outcome.code == "invalid_arguments"
    assert "至少填写一个" in outcome.content
    assert "Value error" not in outcome.content and "。。" not in outcome.content
    assert rows(factory)[0].status == "校验拦下"


def test_mcp_structured_result_is_normalised_and_audited_with_the_server_name(audit):
    writer, factory = audit
    from langchain_core.tools import StructuredTool

    async def call(order_id: str):
        return ('{"ok": true, "data": {"carrier": "\\u987a\\u4e30"}}',
                {"structured_content": {"ok": True, "data": {"carrier": "顺丰速运", "status": "IN_TRANSIT",
                                                             "nodes": [{"time": "t", "text": "已揽收"}]}}})

    mcp_tool = StructuredTool(name="query_logistics", description="查物流。",
                              coroutine=call, args_schema={"type": "object",
                                                            "properties": {"order_id": {"type": "string"}},
                                                            "required": ["order_id"]},
                              response_format="content_and_artifact")
    spec = ToolSpec(name="query_logistics", description="查物流。", json_schema=mcp_tool.args_schema,
                    source="mcp", mcp_server="logistics", permission="read", label="查询物流", tool=mcp_tool)
    engine = ToolEngine(_registry([spec]), settings=settings(), audit=writer)
    outcome = run(engine, name="query_logistics", args={"order_id": "1001"}, conversation_id=9)
    assert outcome.ok is True and outcome.source == "mcp" and outcome.mcp_server == "logistics"
    assert "顺丰" in outcome.content and "\\u" not in outcome.content          # 中文不转义
    assert outcome.summary.startswith("1 条物流记录")
    row = rows(factory)[0]
    assert (row.tool_source, row.mcp_server, row.status) == ("mcp", "logistics", "成功")
    assert row.arguments == {"order_id": "1001"} and row.conversation_id == 9


def test_engine_invokes_with_the_tool_call_shape(audit):
    """实测：`ainvoke(args_dict)` 会把 MCP 的 artifact 丢掉，只有 tool_call 形状才回 ToolMessage。
    引擎对内置和 MCP 统一走这一条路，用一个只记录入参的假工具把形状钉住。"""
    from langchain_core.messages import ToolMessage

    writer, factory = audit
    received = []

    class Stub:
        name = "query_order"
        description = "查询。"
        args_schema = {"type": "object", "properties": {"order_id": {"type": "string"}}}

        async def ainvoke(self, payload):
            received.append(payload)
            return ToolMessage(content='{"ok": true, "data": {"order_id": "1001"}}',
                               tool_call_id=payload["id"], name=payload["name"])

    engine = ToolEngine(_registry([ToolSpec(name="query_order", description="查询。",
                                            json_schema=Stub.args_schema, source="builtin", mcp_server=None,
                                            permission="read", label="查询订单", tool=Stub())]),
                        settings=settings(), audit=writer)
    outcome = run(engine, name="query_order", args={"order_id": "1001"}, tool_call_id="call-42",
                  conversation_id=5)
    assert received == [{"type": "tool_call", "name": "query_order", "args": {"order_id": "1001"},
                         "id": "call-42"}]
    assert outcome.ok is True and json.loads(outcome.content)["data"]["order_id"] == "1001"
    assert outcome.tool_call_id == "call-42"
    assert rows(factory)[0].tool_call_id == "call-42"        # 审计里的 id 与模型给出的对齐


def test_engine_mints_a_call_id_when_the_caller_has_none(audit):
    writer, factory = audit
    engine = ToolEngine(_registry([spec_of(ok_tool(), name="query_order")]), settings=settings(), audit=writer)
    outcome = run(engine, name="query_order", args={"order_id": "1001"}, conversation_id=5)
    assert outcome.ok is True
    assert rows(factory)[0].tool_call_id


def test_audit_failure_does_not_change_the_outcome(audit):
    """§5：审计写失败必须被引擎咽下去，工具结果照常返回（第 5 条：审计不许反噬执行）。"""
    class ExplodingAudit:
        calls = 0

        def record(self, item):
            ExplodingAudit.calls += 1
            raise RuntimeError("审计库挂了")

    engine = ToolEngine(_registry([spec_of(ok_tool(), name="query_order")]), settings=settings(),
                        audit=ExplodingAudit())
    outcome = run(engine, name="query_order", args={"order_id": "1001"})
    assert outcome.ok is True and ExplodingAudit.calls == 1


def test_malformed_schema_is_reported_without_crashing(audit):
    """外部 MCP Server 可能送来非法 schema：不能让它把整个调用炸掉，也不能当它校验通过。"""
    writer, factory = audit
    calls = []

    class Stub:
        name = "query_warranty"
        description = "查在保。"
        args_schema = {"type": "nonsense", "properties": {"order_id": {"type": "string"}}}

        async def ainvoke(self, payload):
            calls.append(payload)
            return {"ok": True}

    spec = ToolSpec(name="query_warranty", description="查在保。", json_schema=Stub.args_schema,
                    source="mcp", mcp_server="aftersale", permission="read", label="查在保", tool=Stub())
    outcome = run(ToolEngine(_registry([spec]), settings=settings(), audit=writer),
                  name="query_warranty", args={"order_id": "1001"})
    assert outcome.ok is False and outcome.code == "invalid_arguments" and calls == []
    assert "参数定义有问题" in outcome.content
    assert rows(factory)[0].status == "校验拦下"


def test_cancelled_call_is_audited_as_failure_then_reraised(audit):
    """前端断开或图被中断时，CancelledError 也要留一条「失败」，然后原样抛给上层。"""
    writer, factory = audit

    class Stub:
        name = "query_order"
        description = "查询。"
        args_schema = {"type": "object", "properties": {"order_id": {"type": "string"}},
                       "required": ["order_id"]}

        async def ainvoke(self, payload):
            raise asyncio.CancelledError()

    engine = ToolEngine(_registry([ToolSpec(name="query_order", description="查询。",
                                            json_schema=Stub.args_schema, source="builtin", mcp_server=None,
                                            permission="read", label="查询订单", tool=Stub())]),
                        settings=settings(), audit=writer)

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await engine.run(name="query_order", args={"order_id": "1001"}, conversation_id=2)

    asyncio.run(scenario())
    row = rows(factory)[0]
    assert row.status == "失败" and row.tool_call_id.startswith("engine-")
    assert "取消" in row.error_message


def test_pydantic_error_on_the_second_attempt_carries_the_attempt_count(audit):
    writer, factory = audit
    seen = []

    class Stub:
        name = "query_order"
        description = "查询。"
        args_schema = {"type": "object", "properties": {"order_id": {"type": "string"}},
                       "required": ["order_id"]}

        async def ainvoke(self, payload):
            seen.append(payload)
            if len(seen) == 1:
                raise OperationalError("select 1", {}, Exception("忙"))
            Args.model_validate({})          # 第二次尝试才爆 pydantic：重试过几次要如实记

    spec = ToolSpec(name="query_order", description="查询。", json_schema=Stub.args_schema,
                    source="builtin", mcp_server=None, permission="read", label="查询订单", tool=Stub())
    outcome = run(ToolEngine(_registry([spec]), settings=settings(), audit=writer),
                  name="query_order", args={"order_id": "1001"})
    assert outcome.code == "invalid_arguments" and len(seen) == 2
    row = rows(factory)[0]
    assert (row.status, row.retry_count) == ("校验拦下", 1)


def test_record_denial_of_a_read_tool_writes_nothing(audit):
    """只有写操作才会弹卡片；给只读工具补「取消」记录没有意义（也不该有这种调用）。"""
    writer, factory = audit
    engine = ToolEngine(_registry([spec_of(ok_tool(), name="query_order")]),
                        settings=settings(), audit=writer)
    engine.record_denial(name="query_order", args={"order_id": "1001"}, reason="取消")
    assert rows(factory) == []
```

- [ ] **Step 2: 跑到红** — Run: `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_tool_engine.py -q`；Expected: `ModuleNotFoundError: No module named 'app.tools.engine'`。

- [ ] **Step 3: 写实现**

```python
# app/tools/engine.py
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.tools.audit import AuditRecord
from app.tools.formatting import MAX_SUMMARY, format_content, normalize_result, summarize
from app.tools.policy import decide
from app.tools.registry import ToolRegistry
from app.tools.validate import validate_arguments

logger = logging.getLogger(__name__)

WRITE_UNKNOWN_RESULT = "写操作结果未知：可能已经执行，请勿重复提交，稍后人工核对。"


@dataclass(frozen=True)
class ToolOutcome:
    """`ok=False` 有两种：业务空结果（code 是业务码，如 not_found）与执行失败
    （code 是 tool_error/tool_timeout）。调用方判「这轮工具是否失败」看 code，不要只看 ok。"""

    tool: str
    ok: bool
    code: str
    content: str
    summary: str
    duration_ms: int
    source: str = "builtin"
    mcp_server: str | None = None
    retry_count: int = 0
    audit_status: str = "成功"
    arguments: dict = field(default_factory=dict)
    tool_call_id: str | None = None


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


class ToolEngine:
    """所有工具调用的唯一出口：查表 → 校验 → 权限 → 执行(超时/重试) → 格式化 → 审计。"""

    def __init__(self, registry: ToolRegistry, *, settings, audit=None) -> None:
        self.registry = registry
        self.settings = settings
        self.audit = audit
        self.timeouts = {"query_faq": settings.knowledge_tool_timeout_seconds}

    def _timeout_for(self, spec) -> float:
        return self.timeouts.get(spec.name, self.settings.tool_timeout_seconds)

    async def _call(self, spec, args: dict, call_id: str, limit: float):
        payload = {"type": "tool_call", "name": spec.name, "args": dict(args), "id": call_id}
        return await asyncio.wait_for(spec.tool.ainvoke(payload), limit)

    async def run(self, *, name: str, args: dict, tool_call_id: str | None = None,
                  conversation_id: int | None = None, confirmed: bool = False) -> ToolOutcome:
        started = time.perf_counter()
        args = dict(args or {})
        call_id = tool_call_id or f"engine-{uuid4().hex[:12]}"
        spec = self.registry.get(name)
        if spec is None:
            logger.warning("工具未登记：%s", name)
            return self._skip(name, "unknown_tool", f"未知工具 {name}", "失败", started, args)

        try:
            report = validate_arguments(spec.json_schema, args)
        except Exception:
            logger.exception("工具参数定义非法，无法校验：%s", name)
            return self._reject(spec, "invalid_arguments", "这个工具的参数定义有问题，暂时无法调用。",
                                "校验拦下", started, args, call_id, conversation_id)
        if not report.ok:
            return self._reject(spec, "invalid_arguments", report.message, "校验拦下", started, args,
                                call_id, conversation_id)

        decision = decide(spec, confirmed=confirmed, settings=self.settings)
        if not decision.allowed:
            if decision.needs_confirm:
                return self._skip(spec.name, "confirm_required",
                                  "该操作需要用户在页面上确认后才能执行。", "confirm_required",
                                  started, args, spec=spec, call_id=call_id)
            return self._reject(spec, "permission_denied", decision.reason, "权限拒绝", started, args,
                                call_id, conversation_id)

        write = spec.permission == "write"
        attempts = 1 if write else self.settings.tool_max_attempts
        limit = self._timeout_for(spec)
        code, message, result = "tool_error", "工具执行失败，请稍后重试。", None
        tries = 0
        for tries in range(1, attempts + 1):
            try:
                result = normalize_result(await self._call(spec, args, call_id, limit))
            except TimeoutError:
                # 写工具跑在 executor 线程里，取消 await 并不等于没执行，只能如实报「结果未知」。
                code = "tool_timeout"
                message = WRITE_UNKNOWN_RESULT if write else "工具响应超时，请稍后重试。"
            except asyncio.CancelledError:
                self._failure(spec, args, "tool_error",
                              WRITE_UNKNOWN_RESULT if write else "调用被取消，请稍后重试。",
                              started, tries - 1, call_id, conversation_id)
                raise
            except ValidationError as exc:          # JSON Schema 表达不了的跨字段规则落这里
                return self._reject(spec, "invalid_arguments",
                                    "参数校验未通过：" + _pydantic_text(exc) + "。请向用户确认后重新调用。",
                                    "校验拦下", started, args, call_id, conversation_id,
                                    retry_count=tries - 1)
            except SQLAlchemyError:
                code = "tool_error"
                message = WRITE_UNKNOWN_RESULT if write else "数据库暂时不可用，请稍后重试。"
            except Exception:
                logger.exception("工具执行异常：%s", spec.name)
                code = "tool_error"
                message = WRITE_UNKNOWN_RESULT if write else "工具执行失败，请稍后重试。"
            else:
                outcome = self._success(spec, args, result, started, tries - 1, call_id)
                self._audit(outcome, call_id, conversation_id,
                            error=None if outcome.ok else outcome.summary)
                return outcome
        return self._failure(spec, args, code, message, started, tries - 1, call_id, conversation_id)

    def _success(self, spec, args, result, started, retry_count, tool_call_id) -> ToolOutcome:
        payload = format_content(result, spec)
        if isinstance(result, dict) and result.get("ok") is False:
            message = str(result.get("message") or "工具没有返回结果。")
            return ToolOutcome(tool=spec.name, ok=False, code=str(result.get("error") or "tool_error"),
                               content=payload, summary=message[:MAX_SUMMARY],
                               duration_ms=_elapsed(started), source=spec.source,
                               mcp_server=spec.mcp_server, retry_count=retry_count,
                               audit_status="失败", arguments=dict(args),
                               tool_call_id=tool_call_id)
        return ToolOutcome(tool=spec.name, ok=True, code="ok", content=payload,
                           summary=summarize(spec, result), duration_ms=_elapsed(started),
                           source=spec.source, mcp_server=spec.mcp_server,
                           retry_count=retry_count, audit_status="成功", arguments=dict(args),
                           tool_call_id=tool_call_id)

    def _failure(self, spec, args, code, message, started, retry_count, tool_call_id, conversation_id):
        outcome = ToolOutcome(tool=spec.name, ok=False, code=code, content=_json_error(code, message),
                              summary=message[:MAX_SUMMARY], duration_ms=_elapsed(started),
                              source=spec.source, mcp_server=spec.mcp_server, retry_count=retry_count,
                              audit_status="超时" if code == "tool_timeout" else "失败",
                              arguments=dict(args), tool_call_id=tool_call_id)
        self._audit(outcome, tool_call_id, conversation_id, error=message)
        return outcome

    def _reject(self, spec, code, message, status, started, args, call_id, conversation_id, *,
                retry_count=0):
        outcome = ToolOutcome(tool=spec.name, ok=False, code=code, content=_json_error(code, message),
                              summary=message[:MAX_SUMMARY], duration_ms=_elapsed(started),
                              source=spec.source, mcp_server=spec.mcp_server, retry_count=retry_count,
                              audit_status=status, arguments=dict(args), tool_call_id=call_id)
        self._audit(outcome, call_id, conversation_id, error=message)
        return outcome

    def record_denial(self, *, name: str, args: dict, reason: str,
                      tool_call_id: str | None = None, conversation_id: int | None = None) -> None:
        """用户在卡片上点了「取消」：调用没发生，但第 5 条要求留痕——按「权限拒绝」落一行。"""
        spec = self.registry.get(name)
        if spec is None or spec.permission != "write":
            return
        call_id = tool_call_id or f"denied-{uuid4().hex[:12]}"
        outcome = self._skip(name, "permission_denied", reason, "权限拒绝",
                             time.perf_counter(), args, spec=spec, call_id=call_id)
        self._audit(outcome, call_id, conversation_id, error=reason)

    def _skip(self, key, code, message, status, started, args, *, spec=None, call_id=None):
        return ToolOutcome(tool=key, ok=False, code=code, content=_json_error(code, message),
                           summary=message[:MAX_SUMMARY], duration_ms=_elapsed(started),
                           source=(spec.source if spec else "unknown"),
                           mcp_server=(spec.mcp_server if spec else None), retry_count=0,
                           audit_status=status, arguments=dict(args or {}),
                           tool_call_id=call_id)

    def _audit(self, outcome, call_id, conversation_id, *, error) -> None:
        # 只有真正放行过的调用会走到这里：未知工具与 confirm_required 在 run/record_denial 里早退，
        # 不再落库（§5 映射表：那次调用根本没执行）。
        if self.audit is None:
            return
        try:
            self.audit.record(AuditRecord(
                tool=outcome.tool, source=outcome.source, mcp_server=outcome.mcp_server,
                arguments=outcome.arguments, status=outcome.audit_status, summary=outcome.summary,
                error=error, retry_count=outcome.retry_count, duration_ms=outcome.duration_ms,
                tool_call_id=call_id, conversation_id=conversation_id,
            ))
        except Exception:
            logger.warning("审计写入失败，工具结果照常返回：tool=%s", outcome.tool, exc_info=True)


def _json_error(code: str, message: str) -> str:
    return json.dumps({"ok": False, "error": code, "message": message}, ensure_ascii=False)


_TYPE_HINTS = {
    "missing": "缺少必填项",
    "string_too_short": "长度太短",
    "string_too_long": "长度太长",
    "string_type": "应为字符串",
    "int_type": "应为整数",
    "int_parsing": "无法解析为整数",
    "float_parsing": "无法解析为数字",
    "bool_type": "应为布尔值",
    "extra_forbidden": "带了多余的参数",
}


def _pydantic_text(exc: ValidationError) -> str:
    parts = []
    for item in exc.errors():
        loc = "、".join(str(x) for x in item.get("loc", ())) or "参数"
        hint = _TYPE_HINTS.get(str(item.get("type") or ""))
        msg = str(item.get("msg") or "").removeprefix("Value error, ").strip().rstrip("。")
        detail = f"{hint}：{msg}" if hint and msg and msg != hint else (msg or hint or "不符合校验规则")
        parts.append(f"{loc}：{detail}")
    return "；".join(parts)[:400]
```

- [ ] **Step 4: 跑到绿** — 同 Step 2 命令，Expected: 20 passed。`test_write_without_confirmation_returns_confirm_required_and_writes_nothing` 必须保持「不落审计」——这是 §5 映射表里唯一不写的状态。
- [ ] **Step 5: 评审修复与变异验证（已做，留档）**
  - 评审结论：Spec ❌｜Critical 0｜Important 5｜Minor 4。I-1 写超时文案、I-2 业务失败 error_message、I-3 pydantic 中文说明、I-4 校验自身异常＋`CancelledError`＋`_audit` 兜底、I-5 审计桩空转；Minor 里 `tool_call_id` 回传、`record_denial` 只写工具两条一并落进实现。
  - 补了 4 条测试（畸形 schema／`CancelledError`／第 2 次尝试的 `retry_count`／只读取消不落行），16 → 20 条。
  - 变异验证 9 条全部变红：写超时文案退回通用、业务失败不记原因、pydantic 原文直出、校验不包 try、
    `CancelledError` 不留痕、审计异常不吞、pydantic 分支丢重试次数、只读工具也记取消、成功结果不带 call id。
  - 转 T12 处理（记入 ledger Deferred）：MCP `ToolMessage(status="error")` 被 `normalize_result` 当业务失败，
    只读工具遇到 MCP 传输错误不会重试——接 Client 时按「传输层错误可重试、业务错误不重试」再分一次。
- [ ] **Step 6: 本地提交** `git commit -am "feat(ch08/T8): 统一执行引擎"`

---

### Task 9: 组装层 `wiring.py`、动态挂载样例、schema 预算

把「注册中心 → 引擎」的构造收在一处，并证明验收①：新工具只做一个注册动作就能被主力 Agent 用上。规格 §6.5、§13。

**Files:**
- Create: `app/tools/wiring.py`、`examples/tools/demo_coupon.py`、`tests/test_tool_wiring.py`
- Modify: `app/tools/registry.py`（加 `spec_from_tool`）、`app/context.py:60-62`（`tool_schema_tokens` 加 reserve 参数、改用 `builtin_json_schemas()`）、`app/context.py:81-114`（`ContextBudget.from_settings` 计入 reserve）、`app/service.py:81-87`（`preflight` 同一口径）
- Test: `tests/test_tool_wiring.py`（预算断言也在里面，不再单开文件）
- 注：`.env.example` 的 `TOOL_EXTRA_MODULES`／`TOOL_SCHEMA_RESERVE_TOKENS` 在 T2（`cfcd77e`）已登记，本任务无需再动。

**Interfaces:**
- Consumes: `build_specs`、`ToolRegistry`、`merge_specs`、`ToolEngine`、`ToolAuditWriter`、`Settings`
- Produces:
  ```python
  # app/tools/registry.py
  def spec_from_tool(tool: BaseTool, *, source: str, permission: str = "read",
                     label: str | None = None, mcp_server: str | None = None) -> ToolSpec
  # app/tools/wiring.py
  def extra_specs(settings) -> list[ToolSpec]
  def build_registry(store, conversation_id: int | None, knowledge, *, settings,
                     mcp_specs: Sequence[ToolSpec] = (), exclude: Iterable[str] = ()) -> ToolRegistry
  def build_engine(registry, *, settings, session_factory) -> ToolEngine
  ```
  动态模块契约（写进 `demo_coupon.py` 的模块 docstring，README 也抄一份）：模块暴露 `EXTRA_TOOLS: list[BaseTool]`，可选 `EXTRA_LABELS`／`EXTRA_PERMISSION`（都是 `dict[工具名, 值]`）。`TOOL_EXTRA_MODULES` 是逗号分隔的模块 dotted-name，**启动时读取**，改了要重启客服服务——这条边界在规格 §6.5 已写清，免重启承诺只适用于 MCP（T12）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_tool_wiring.py
import asyncio
import logging
import sys
import types

import pytest
from langchain_core.tools import StructuredTool
from sqlalchemy import create_engine, select

from app.config import Settings
from app.context import ContextBudget, tool_schema_tokens
from app.database import build_session_factory
from app.models import Base, ToolAuditLog
from app.seed import seed_faq
from app.storage import ConversationStore
from app.tools.registry import spec_from_tool
from app.tools.wiring import build_engine, build_registry, extra_specs
from tests.test_tool_policy import make_tool


def settings(**overrides):
    base = {"openai_base_url": "http://upstream.invalid/v1", "openai_model": "m", "openai_api_key": "k"}
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture()
def store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'wiring.db'}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    seed_faq(factory)
    yield ConversationStore(factory)
    engine.dispose()


def rows(store):
    with store.session_factory() as session:
        return list(session.scalars(select(ToolAuditLog).order_by(ToolAuditLog.id)))


def test_demo_module_is_collected_by_name_only():
    specs = extra_specs(settings(tool_extra_modules="examples.tools.demo_coupon"))
    assert {item.name for item in specs} == {"query_coupon", "issue_coupon"}
    for item in specs:
        assert item.source == "builtin" and item.description and item.json_schema["properties"]
    assert {item.name: item.permission for item in specs} == {"query_coupon": "read",
                                                              "issue_coupon": "write"}
    assert {item.name: item.label for item in specs} == {"query_coupon": "查询优惠券",
                                                          "issue_coupon": "补发优惠券"}


def test_registry_merges_builtin_and_dynamic_without_touching_core_code(store):
    registry = build_registry(store, 1, None, settings=settings(
        tool_extra_modules="examples.tools.demo_coupon"))
    assert set(registry.names()) >= {"query_order", "query_product", "query_logistics",
                                     "query_faq", "create_ticket", "query_coupon", "issue_coupon"}
    assert registry.get("query_coupon").label == "查询优惠券"


def test_same_name_keeps_the_first_registered_and_warns(store, caplog):
    """合并顺序是契约的一部分：内置先到先得，MCP 同名工具被丢且留下 WARNING。"""
    duplicate = spec_from_tool(make_tool("query_order"), source="mcp", mcp_server="logistics",
                               label="MCP 查询订单")
    with caplog.at_level(logging.WARNING, logger="app.tools.wiring"):
        registry = build_registry(store, 1, None, settings=settings(), mcp_specs=[duplicate])
    kept = registry.get("query_order")
    assert (kept.source, kept.mcp_server) == ("builtin", None)
    assert "query_order" in caplog.text


def test_dynamic_read_tool_runs_through_the_engine_and_lands_audit(store):
    registry = build_registry(store, 1, None, settings=settings(
        tool_extra_modules="examples.tools.demo_coupon"))
    engine = build_engine(registry, settings=settings(), session_factory=store.session_factory)
    outcome = asyncio.run(engine.run(name="query_coupon", args={"order_id": "1001"}, conversation_id=1))
    assert outcome.ok is True and "满 100 减 10" in outcome.content
    assert "优惠券" in outcome.summary
    assert rows(store)[0].tool_name == "query_coupon" and rows(store)[0].status == "成功"


def test_dynamic_write_tool_is_denied_until_the_local_allowlist_lists_it(store):
    registry = build_registry(store, 1, None, settings=settings(
        tool_extra_modules="examples.tools.demo_coupon"))
    denied = asyncio.run(build_engine(registry, settings=settings(),
                                      session_factory=store.session_factory).run(
        name="issue_coupon", args={"order_id": "1001"}, confirmed=True, conversation_id=1))
    assert denied.ok is False and denied.code == "permission_denied"
    assert rows(store)[0].status == "权限拒绝"

    allowed_settings = settings(tool_extra_modules="examples.tools.demo_coupon",
                                tool_write_allowlist="create_ticket,issue_coupon")
    allowed = asyncio.run(build_engine(build_registry(store, 1, None, settings=allowed_settings),
                                       settings=allowed_settings,
                                       session_factory=store.session_factory).run(
        name="issue_coupon", args={"order_id": "1001"}, confirmed=True, conversation_id=1))
    assert allowed.ok is True


def test_unreadable_module_is_skipped_not_fatal(store):
    registry = build_registry(store, 1, None, settings=settings(tool_extra_modules="no.such.module"))
    assert "query_order" in registry.names()


def test_malformed_extra_module_is_skipped_not_fatal(monkeypatch):
    """模块能被 import 但结构不对（EXTRA_TOOLS 不是列表、元素不是工具）也不能拖垮整轮。"""
    monkeypatch.setitem(sys.modules, "broken_extra_shapes",
                        types.SimpleNamespace(EXTRA_TOOLS={"not": "a list"}))
    assert extra_specs(settings(tool_extra_modules="broken_extra_shapes")) == []

    monkeypatch.setitem(sys.modules, "broken_extra_tools",
                        types.SimpleNamespace(EXTRA_TOOLS=[object()], EXTRA_PERMISSION=["nope"]))
    assert extra_specs(settings(tool_extra_modules="broken_extra_tools")) == []

    monkeypatch.setitem(sys.modules, "broken_extra_scalar", types.SimpleNamespace(EXTRA_TOOLS=5))
    assert extra_specs(settings(tool_extra_modules="broken_extra_scalar")) == []


def test_exclude_drops_a_tool_for_a_route(store):
    registry = build_registry(store, 1, None, settings=settings(), exclude={"query_faq"})
    assert "query_faq" not in registry.names() and "query_order" in registry.names()


def test_spec_from_tool_accepts_pydantic_and_dict_schemas():
    builtin = spec_from_tool(make_tool("query_order"), source="builtin", label="查询订单")
    assert builtin.permission == "read" and builtin.json_schema["properties"]["order_id"]

    external = StructuredTool(name="query_warranty", description="查在保。",
                              coroutine=lambda **kwargs: None,
                              args_schema={"type": "object", "properties": {"order_id": {"type": "string"}}})
    converted = spec_from_tool(external, source="mcp", mcp_server="aftersale")
    assert converted.json_schema == external.args_schema and converted.label == "query_warranty"
    assert (converted.source, converted.mcp_server, converted.permission) == ("mcp", "aftersale", "read")


def test_spec_from_tool_gives_a_tool_without_arguments_an_empty_object_schema():
    class Ping:
        name = "ping"
        description = "心跳。"
        args_schema = None

    converted = spec_from_tool(Ping(), source="builtin")
    assert converted.json_schema == {"type": "object", "properties": {}}
    assert converted.label == "ping"


def test_schema_reserve_widens_the_system_cost_and_shrinks_history():
    assert tool_schema_tokens(400) - tool_schema_tokens() == 400
    assert tool_schema_tokens(-100) == tool_schema_tokens()     # 负数按 0 算，不能把成本估少

    tight = ContextBudget.from_settings(settings(context_window_tokens=20000,
                                                  tool_schema_reserve_tokens=0))
    wide = ContextBudget.from_settings(settings(context_window_tokens=20000,
                                                 tool_schema_reserve_tokens=1200))
    assert wide.system - tight.system == 1200
    assert tight.available - wide.available == 1200
    assert tight.history - wide.history == 1200                 # 窗口够小，reserve 真的挤掉历史
```

- [ ] **Step 2: 跑到红** — Run: `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_tool_wiring.py -q`；Expected: `ModuleNotFoundError: No module named 'app.tools.wiring'`。

- [ ] **Step 3: 加 `spec_from_tool`（`app/tools/registry.py` 末尾）**

```python
def spec_from_tool(tool, *, source, permission="read", label=None, mcp_server=None):
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
```

`app/tools/registry.py` 顶部同步补 `from pydantic import BaseModel`。

- [ ] **Step 4: 写 `app/tools/wiring.py`**

```python
import importlib
import logging

from app.tools.audit import ToolAuditWriter
from app.tools.builtin import build_specs
from app.tools.engine import ToolEngine
from app.tools.registry import ToolRegistry, merge_specs, spec_from_tool

logger = logging.getLogger(__name__)


def _names(raw: str) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


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
```

- [ ] **Step 5: 写 `examples/tools/demo_coupon.py`**

```python
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
```

- [ ] **Step 6: 预算侧改口径**

`app/context.py`：

```python
def tool_schema_tokens(reserve: int = 0) -> int:
    schemas = builtin_json_schemas()
    return max(0, reserve) + estimate_text_tokens(json.dumps(schemas, ensure_ascii=False))
```
`from_settings` 里 `system = estimate_text_tokens(chat_system(settings.store_name)) + tool_schema_tokens(settings.tool_schema_reserve_tokens)`；`app/service.py` 的 `preflight` 用同一个 `tool_schema_tokens(self.settings.tool_schema_reserve_tokens)`。两处口径必须一致，否则预算自检和实际拼装互相打脸。

> 搬家时顺手修掉一个既有 bug：旧 `tool_schema_tokens()` 的模型清单是 `(OrderArgs, ProductArgs, FaqArgs, TicketArgs, OrderArgs)`，`OrderArgs` 被数了两遍，白占预算。`builtin_json_schemas()` 只列四个真实模型。

`tests/test_context.py` 的演示预算期望随之改变，实测为 **`(5308, 3715, 1592)`**（原 `(5638, 3946, 1691)`）：
`+400` 的 reserve 与「修掉 OrderArgs 重复计数」的约 `−70` 叠加，净差 330。断言旁写清这两项来源，避免下次对不上时又去猜。
同时在用例入参里显式写 `"tool_schema_reserve_tokens": 400`：Settings 仍会读进程环境变量，不 pin 住的话换台机器口径就变了。

- [ ] **Step 7: 跑到绿（含回归）** — Run: `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_tool_wiring.py tests/test_context.py tests/test_ch07_budget.py -q` → PASS（文件名以 `--collect-only` 实际为准，找不到就跑全量）。Expected 全绿，其中 `tests/test_tool_wiring.py` 新增 **11 条**。
- [ ] **Step 8: 独立评审与修复（已做，留档）** — 评审：Spec ✅、Critical 0、Important 2、Minor 6。
  - I1（本轮修）：`extra_specs` 原来只把 `import` 包了 try；`EXTRA_TOOLS` 不是列表／元素不是工具／`EXTRA_LABELS` 不是 dict 时会抛
    `AttributeError`，而 T10 起每请求都会调 `build_registry` ⇒ 每轮都挂。改为「结构校验 + 逐工具隔离」，并补 `EXTRA_TOOLS` 为 dict／含 `object()`／为标量三种畸形用例。
  - I2（本轮修）：合并顺序「内置 → 动态 → MCP、先到先得」没有用例锁。补 `test_same_name_keeps_the_first_registered_and_warns`（MCP 同名 `query_order` 被丢、保留内置、WARNING 落日志）。变异把顺序反转即红。
  - Minor 修了 3 条：`spec_from_tool` 用 `issubclass(BaseModel)` 判断并把「认不出的 schema」兜成空 object（删掉冗余的 `None` 分支，由兜底统一处理）；`tool_schema_tokens` 对负数 reserve 取 `max(0, ...)`；reserve 用例改为「system／available／history 三处各差 1200」，原 `wide.history <= tight.history` 在默认窗口下两边都是 12000、属空转。
  - 转下游：README 抄动态模块契约（T18）；`EXTRA_PERMISSION` 缺省 `read` 的漏标风险（写工具不声明就当只读，会在 T12 的 MCP 权限评审里一并复核，动态模块作者契约维持「写必须显式声明」）；按路由排除 MCP 工具（T12 再扩 `exclude`）。
  - 变异 8 条全部变红：非列表不拦、坏工具不隔离、合并顺序反转、重名不告警、`mcp_server` 不回填、无参兜底坏掉、负数 reserve、`from_settings` 不计 reserve。
- [ ] **Step 9: 本地提交** `git commit -am "feat(ch08/T9): 组装层、动态工具模块与 schema 预算"`

---

### Task 10: 调用点全量改走引擎，删掉旧执行器

需求第 4 条：所有工具调用走同一处。`react.py`、`policy_nodes.py`、`main.py`、`scripts/demo_agent_loop.py` 四处调用点全部换成 `build_registry`／`build_engine`，`legacy.py` 退役。规格 §8、§12。

**Files:**
- Modify: `app/agent/react.py:15,32-50,103-133`、`app/workflow/agent_node.py:10,105-162`、`app/workflow/policy_nodes.py:7,41-47`、`app/main.py:38,228-236,287-294`、`app/workflow/state.py`（加 `pending_write`）、`app/tools/__init__.py`（去掉 legacy 再导出）、`scripts/demo_agent_loop.py:16,28-31`
- Delete: `app/tools/legacy.py`
- Test: `tests/test_tool_callsites.py`（新增）＋适配 `tests/test_tools.py`、`tests/test_react.py`、`tests/test_workflow_agent.py`、`tests/test_chat_tools.py`

**Interfaces:**
- Consumes: `build_registry`、`build_engine`、`ToolEngine.run`、`ToolOutcome`
- Produces:
  ```python
  # app/agent/react.py —— 签名换代，其余循环逻辑不动
  async def run_react(*, model, registry: ToolRegistry, engine: ToolEngine, messages: list,
                      on_token, on_status, max_steps=4, token_budget=6000, timeout=60.0,
                      guard=None, on_tool_outcome=None, store=None, conversation_id=None,
                      tool_result_max_tokens=None) -> ReActResult
  @dataclass
  class ReActResult:
      reply: str; finish_reason: str | None; steps: int; tool_calls: int; tokens: int
      stopped_by: str | None
      pending_write: dict | None = None    # {"tool","args","tool_call_id","pending_id"}
  ```
  `status` 帧多两个字段：`source`、`mcp_server`（内置为 `"builtin"`／`None`）；前端在 T15 之前不认识它们也会照旧忽略。

- [ ] **Step 1: 先把 ReAct 的模型替身挪成公共夹具**

`tests/test_react.py:14-60` 的 `chunk`、`call`、`BoundModel`、`ScriptedModel` 整段移进 `tests/support.py`（`FakeExecutor` 先留在原文件），`tests/test_react.py` 改成 `from tests.support import ScriptedModel, call, msg_chunk`。
**注意重名**：`tests/support.py:24` 已经有一个 `chunk(content=None, finish_reason=None)`，那是拼 httpx SSE 字节用的，签名完全不同。搬过来的 ReAct 版必须改名 `msg_chunk`，两者都要用 `test_react.py` 里 `chunk(` 的 22 处调用（定义行除外）一并跟着改；搬完先跑 `pytest tests/test_react.py tests/test_api.py -q` 确认全绿——这一步不改行为。

- [ ] **Step 2: 写失败测试**

```python
# tests/test_tool_callsites.py
import asyncio
import json

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import create_engine, select

from app.agent.react import run_react
from app.config import Settings
from app.database import build_session_factory
from app.models import Base, Message, ToolAuditLog
from app.prompts import chat_system
from app.seed import seed_faq
from app.storage import ConversationStore
from app.tools.wiring import build_engine, build_registry
from app.workflow.policy_nodes import make_order_node
from tests.support import ScriptedModel, call, msg_chunk


def settings(**overrides):
    base = {"openai_base_url": "http://upstream.invalid/v1", "openai_model": "m", "openai_api_key": "k",
            "tool_timeout_seconds": 2, "tool_max_attempts": 2}
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture()
def ctx(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'callsites.db'}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    seed_faq(factory)
    store = ConversationStore(factory)
    conversation_id = store.start_turn(user_id="anon-1", message="帮我建个工单")
    yield store, conversation_id, factory
    engine.dispose()


def audit_rows(store):
    with store.session_factory() as session:
        return list(session.scalars(select(ToolAuditLog).order_by(ToolAuditLog.id)))


def run_round(ctx, rounds, *, question="订单 1001 到哪了"):
    store, conversation_id, factory = ctx
    cfg = settings()
    registry = build_registry(store, conversation_id, None, settings=cfg, exclude={"query_faq"})
    model = ScriptedModel(rounds)
    statuses, tokens = [], []
    result = asyncio.run(run_react(
        model=model, registry=registry,
        engine=build_engine(registry, settings=cfg, session_factory=factory),
        messages=[SystemMessage(content=chat_system("测试店")), HumanMessage(content=question)],
        on_token=tokens.append, on_status=statuses.append,
        store=store, conversation_id=conversation_id, timeout=5))
    return result, statuses, tokens, store


def test_write_call_stops_the_loop_and_hands_back_pending_write(ctx):
    args = {"description": "杯子碎了，想申请售后", "ticket_type": "售后"}
    result, statuses, _, store = run_round(
        ctx, [[msg_chunk("", "tool_calls", [call("create_ticket", args, "call_1")])],
              [msg_chunk("好的")]])
    assert result.pending_write["tool"] == "create_ticket"
    assert result.pending_write["args"] == args
    assert result.pending_write["tool_call_id"] == "call_1"
    assert len(result.pending_write["pending_id"]) >= 8
    assert result.stopped_by == "await_confirm" and result.tool_calls == 1

    last = statuses[-1]
    assert (last["phase"], last["tool"], last["ok"]) == ("end", "create_ticket", False)
    assert last["source"] == "builtin" and last["mcp_server"] is None and "确认" in last["summary"]

    with store.session_factory() as session:
        rows = list(session.scalars(select(Message).where(Message.role == "tool")))
    assert json.loads(rows[-1].content)["error"] == "confirm_required"
    assert rows[-1].tool_call_id == "call_1"
    assert audit_rows(store) == []                    # 待确认不落审计（规格 §5）


def test_read_call_goes_through_the_engine_and_writes_audit(ctx):
    result, statuses, _, store = run_round(
        ctx, [[msg_chunk("", "tool_calls", [call("query_order", {"order_id": "1001"}, "call_1")])],
              [msg_chunk("已发货")]])
    assert result.pending_write is None and result.tool_calls == 1 and result.reply == "已发货"
    assert statuses[0] == {"phase": "start", "tool": "query_order", "label": "查询订单",
                           "source": "builtin", "mcp_server": None}
    row = audit_rows(store)[0]
    assert (row.tool_name, row.status, row.tool_source) == ("query_order", "成功", "builtin")
    assert row.retry_count == 0


def test_unknown_tool_from_the_model_is_reported_not_raised(ctx):
    result, _, _, store = run_round(
        ctx, [[msg_chunk("", "tool_calls", [call("query_weather", {"city": "上海"}, "call_1")])],
              [msg_chunk("我查不到天气")]])
    assert result.reply == "我查不到天气" and result.tool_calls == 1
    assert audit_rows(store) == []                    # 未登记的工具写不出有意义的审计
    with store.session_factory() as session:
        row = list(session.scalars(select(Message).where(Message.role == "tool")))[-1]
    assert json.loads(row.content)["error"] == "unknown_tool"


def test_policy_order_node_uses_the_engine(ctx):
    store, conversation_id, factory = ctx

    class Deps:
        pass

    deps = Deps()
    deps.store, deps.settings, deps.knowledge = store, settings(), None
    state = asyncio.run(make_order_node(deps)({"conversation_id": conversation_id,
                                               "question": "订单 1001 到哪了", "intent": "物流"}))
    assert state["order"]["order_id"] == "1001"
    row = audit_rows(store)[0]
    assert (row.tool_name, row.status) == ("query_order", "成功")
```

`/api/tickets` 与 `/api/refunds` 不新增用例：`tests/test_api.py` 里第 5 章那两条按钮建单用例**期望值一个字不改**，它们全绿就是「那条路保持不动」的证据（改的只是把执行交给引擎并带 `confirmed=True`）。
- [ ] **Step 3: 跑到红** — Run: `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_tool_callsites.py -q`；Expected: `TypeError: run_react() got an unexpected keyword argument 'registry'`。

- [ ] **Step 4: 改 `react.py`**

三处改动，循环骨架不动：

```python
# import 行：删掉 app.tools 的 TOOL_LABELS/ToolExecutor/ToolOutcome，改成
from app.tools.engine import ToolEngine, ToolOutcome
from app.tools.registry import ToolRegistry

# 签名：tools/executor → registry/engine；ReActResult 加 pending_write=None

        bound = model.bind_tools(registry.langchain_tools())
```
执行段（原 `:112-133`）替换为：

```python
        for item in stored:
            spec = registry.get(item["name"])
            label = spec.label if spec else item["name"]
            source = spec.source if spec else "unknown"
            server = spec.mcp_server if spec else None
            on_status({"phase": "start", "tool": item["name"], "label": label,
                       "source": source, "mcp_server": server})
            outcome = await engine.run(name=item["name"], args=item["args"], tool_call_id=item["id"],
                                       conversation_id=conversation_id)
            if outcome.code == "confirm_required":
                # 引擎没有执行这次调用；把「等用户确认」如实写成工具结果，剩下的调用原样不动。
                await _store_tool_row(store, conversation_id, item["id"], outcome.content)
                on_status({"phase": "end", "tool": item["name"], "label": label, "ok": False,
                           "summary": outcome.summary, "duration_ms": outcome.duration_ms,
                           "source": source, "mcp_server": server})
                tail = guard.finish()          # 与其他出口一致：扣住的尾巴先发出去
                if tail:
                    on_token(tail)
                return ReActResult(guard.text, "stop", steps, total + 1, estimate_tokens(work),
                                   "await_confirm",
                                   pending_write={"tool": item["name"], "args": item["args"],
                                                  "tool_call_id": item["id"],
                                                  "pending_id": uuid4().hex})
            await _store_tool_row(store, conversation_id, item["id"], outcome.content)
            on_status({"phase": "end", "tool": item["name"], "label": label, "ok": outcome.ok,
                       "summary": outcome.summary, "duration_ms": outcome.duration_ms,
                       "source": source, "mcp_server": server})
            total += 1
            ...（on_tool_outcome／forced／ToolMessage 三段逻辑逐字照旧）
```
`_store_tool_row` 就是 `asyncio.to_thread(store.add_message, ...)` 的两行小工具；`pending_write` 里的 `pending_id` 由这里生成（nonce 的来源，T14 的 409 比对靠它）。

> 注意：原代码把 `total += 1` 放在执行之后，`confirm_required` 分支也必须 `total + 1`，否则日志里「这轮调了几次工具」与实际不符。

- [ ] **Step 5: 改其余三处调用点**

`app/workflow/agent_node.py:109-148`（引擎要的 session 工厂直接取 `deps.store.session_factory`，`ConversationStore` 早就暴露了这个属性，不再往 `WorkflowDeps` 加第二个来源）：

```python
        registry = build_registry(deps.store, conversation_id, deps.knowledge,
                                  settings=deps.settings, mcp_specs=mcp_specs,
                                  exclude={"query_faq"} if route in ("knowledge", "policy") else ())
        engine = build_engine(registry, settings=deps.settings, session_factory=deps.store.session_factory)
        result = await run_react(model=deps.model, registry=registry, engine=engine, messages=messages, ...)
        ...
        return {"reply": result.reply, ..., "pending_write": result.pending_write}
```
`mcp_specs` 从 `state` 之外的地方来？不要塞进 State（检查点里存工具对象没意义）：`agent_node` 从 `deps.mcp` 现取——`deps.mcp` 是 T12 才有的 `McpDiscovery`，本任务先让 `agent_node` 读 `getattr(deps, "mcp", None)`，`None` 时 `mcp_specs=()`，非 None 时取 `mcp.specs()`（T12 必须提供这个方法）。这样 T10 与 T12 各自可测。

`app/workflow/policy_nodes.py:41-47` 与 `app/main.py:228-236`／`:287-294`：同样的两行替换，`main.py` 的两处调用改成 `engine.run(name="create_ticket", args=..., confirmed=True, conversation_id=body.conversation_id)`；`scripts/demo_agent_loop.py:28-31` 同样换成 `build_registry`／`build_engine`。

- [ ] **Step 6: State 与 deps**

`app/workflow/state.py` 加 `pending_write: dict | None`（`total=False` 的 TypedDict，加字段不影响既有节点）。`WorkflowDeps` 本任务不加字段：引擎要的 session 工厂走 `deps.store.session_factory`，`agent_node` 与 `policy_nodes` 都从这里取，避免同一进程出现两个来源。`app/workflow/policy_nodes.py:41-47` 的构造同样只用 `deps.store`／`deps.settings`／`deps.knowledge`。

- [ ] **Step 7: 退役 `legacy.py`**

`git rm app/tools/legacy.py`，`app/tools/__init__.py` 删掉那行 `from app.tools.legacy import ...`，并把 `__all__ = [name for name in dir() ...]` 换成显式名单（`dir()` 会把子模块名也塞进去，T3 评审已实测），`ToolEngine`/`ToolOutcome` 改从 `app.tools.engine` 再导出；`react.py` 里对 `app.tools.legacy` 的 import 已在上一步换成 `app.tools.engine`。`RESTRICTED_TOOLS` 这个集合就此消失——重试口径改由 `spec.permission` 决定（读：`tool_max_attempts`；写：1 次）。

- [ ] **Step 8: 适配既有用例**

- `tests/test_tools.py`：`test_registry_exposes_the_five_business_tools` 保留（`make_tools` 仍在，五个内置工具不变）；五条 `test_executor_*`／`test_faq_uses_its_own_timeout` 直接删掉——T8 已覆盖同样语义，**不留双份**。把 `FakeKnowledge` 挪进 `tests/support.py`，`tests/test_tool_registry.py` 的 import 跟着改。
- `tests/test_react.py`：`TOOLS = {name: tool(name) ...}` 改成 `ToolRegistry([...specs])` ＋ `build_engine`（fake 工具全部标 `read`，避免触发确认流），断言 `executor.calls` 改成那批 fake 工具的调用记录（`StructuredTool` ＋ `extra="allow"` 的参数模型，JSON Schema 校验放行任意参数）。
- `tests/test_workflow_agent.py:102`：`model.tool_names` 仍应是五个内置工具名（`deps.mcp` 为 None 时）；本任务不改期望值。
- `tests/test_chat_tools.py:26`：status 帧多了 `source`/`mcp_server`，把整帧相等改成 `payload["tool"] == ... and payload["label"] == "查询物流"` 再单独断言 `payload["source"] == "builtin"`。
- `tests/test_api.py:27`：同上，工具名单不改，只改逐帧比对方式。

- [ ] **Step 9: 跑到绿（全量）** — Run: `LANGSMITH_TRACING=false .venv/Scripts/python.exe -X utf8 -m pytest -q`；Expected: **400 passed, 3 skipped**（T9 结束 401，＋`tests/test_tool_callsites.py` 4 条，－`tests/test_tools.py` 删掉的 5 条执行器用例）。
- [ ] **Step 10: 本地提交** `git commit -am "refactor(ch08/T10): 所有调用点统一走执行引擎，旧执行器退役"`

**实现期修正（T10 完成后回填，2026-10-31）**
- Step 2 计划代码块两处必炸：`chat_system` 在 `app.prompts`（不在 `app.context`）；`{"description": "杯子碎了"}` 只有 4 字，会先被 `TicketArgs.description` 的 `min_length=5` 拦成 `invalid_arguments`，永远走不到 `confirm_required` —— 改成「杯子碎了，想申请售后」。
- Step 4 的确认出口补了 `guard.finish()`＋`on_token(tail)`：与其他三个出口保持同一不变量「`reply` 等于已流给客户端的全部文本」，否则被守卫扣住的尾巴会静默丢掉。
- Step 4 的 `_store_tool_row` 同时承担普通工具结果的落库（原 `if store is not None ...` 两行），确认分支与普通分支共用。
- Step 5 的 MCP 契约落为 `await mcp.specs()`：T12 的 `McpDiscovery.specs(*, force=False)` 是 async（本计划 T12 段已定），`agent_node` 用 `_mcp_specs()` 包了 `try/except` 并降级空表；失败只记 WARNING，本轮只剩内置工具。
- Step 7 的 `__all__` 换成显式 18 项名单（`dir()` 会把 `builtin`/`engine`/`registry` 子模块名混进去，T3 评审已实测），`ToolEngine`/`ToolOutcome` 从 `app.tools.engine` 再导出。
- Step 8 实际删掉的是 5 条执行器用例（`test_executor_rejects_unknown_tool_and_bad_arguments`、`test_executor_summarizes_success_for_the_badge`、`test_executor_retries_transient_failures`、`test_executor_times_out_and_retries_reads_only`、`test_faq_uses_its_own_timeout`），计划原文「六条」是数错；`FakeKnowledge` 只有 `test_tool_registry.py` 引用，`test_tool_formatting.py` 不涉及。
- 全量：**400 passed, 3 skipped**。

**评审修复（T10 完成后回填，2026-10-31）**
- I1 同步 `mcp.specs()` 与 T12 的 async 定义冲突：`agent_node` 改 `await _mcp_specs(mcp)`，`try/except` 降级 `()` 并记 WARNING（规格 §11.4：Server 挂了不能让整轮对话断掉）。补 `test_mcp_specs_awaits_and_falls_back_to_empty`，变异「去掉 await」即红。
- I2 确认暂停会静默丢掉同轮后续 `tool_calls`（assistant 行已存全部，后续没有对应 tool 行，下一轮历史不成对）：确认分支把 `stored[position+1:]` 各写一条 `{"ok":false,"error":"not_executed"}` 的 tool 行并发 WARNING，不入引擎、不发 status、不落审计。补多调用用例（读→写→读，断言三行 tool 消息、审计只有那条读）。
- I3 确认出口的不变量没有测试锁住（删 `guard.finish()` 仍全绿）：补带前言的确认用例，断言 `"".join(tokens) == reply == "好的，我帮你建工单D"`（尾字 D 会被 `ReplyGuard` 扣住，必须 `finish()` 才交出来）、tool 行恰好 1 条、审计 0 条；变异「删确认出口的 finish」即红。
- Minor ④：`policy_nodes.order_node` 的 start 帧改从 `registry.get("query_order")` 的 spec 取 `label/source/mcp_server`（不再硬编码）；补 start/end 帧断言与「查不到订单 → end.ok=False」用例，变异「end 帧恒 True」即红。
- Minor ①：`app/main.py` 两处 `confirmed=True` 各加一行注释说明「按钮即用户确认，白名单仍是硬闸门」；该事实已由 `tests/test_tool_engine.py::test_write_outside_allowlist_is_denied_and_audited`（`confirmed=True` 仍拒）锁定，不另写同义用例。
- Minor ③：`pending_write` 维持四键（`tool/args/tool_call_id/pending_id`），**不在 T10 塞 `preview`**。规格 §12.2 的预览语义由 T13 的 `write_preview(state)` 从 `args` 现拼承载（计划 T13 段），避免两处真相；T18 文档按此口径写。
- Minor ②：`tests/test_react.py` 的 `FakeArgs(extra="allow")` 保留——夹具要放行任意参数，`ToolSpec` 的 schema 校验在 T8 已有独立用例，不需要在这里重复拦。
- 全量：**404 passed, 3 skipped**（400 ＋ 4 新）。

---

### Task 11: 两个自建 MCP Server（物流／售后）

独立进程、Streamable HTTP、Server 内按入参种子生成 mock，不接真实系统也不建表。规格 §10、§18-C1。

**实测已钉死的四条 API 事实（照抄，别再凭记忆改）**
- Server 侧：`FastMCP(name, host=, port=, streamable_http_path=, stateless_http=, json_response=)`，起跑用 `mcp.run(transport="streamable-http")`——**连字符**（`mcp/server/fastmcp/server.py:297`）。
- Client 侧连接配置：`{"transport": "streamable_http"}`——**下划线**（`langchain_mcp_adapters/sessions.py:167`）。
- `@mcp.tool()` 装饰 async 函数即可（`func_metadata` 认 async）；工具的 `inputSchema` 由类型注解生成，客户端拿到的 `args_schema` 就是这个 JSON Schema dict（含 `title`，**不含** `additionalProperties`）。
- 真进程实测：返回 `dict` 时客户端 `ToolMessage.content` 是**内容块列表** `[{"type":"text","text":"{...}"}]`，`artifact` 为 `None`（FastMCP 没给 `structuredContent`）；Server 侧参数校验失败在 `handle_tool_errors=False` 下抛 `_MCPToolExecutionError`。归一逻辑已在 T6 覆盖。

**Files:**
- Create: `mcp_servers/__init__.py`（空文件，为 `-m mcp_servers.xxx` 启动）、`mcp_servers/mockgen.py`、`mcp_servers/logistics_server.py`、`mcp_servers/aftersale_server.py`、`start-mcp.bat`、`tests/test_mcp_servers.py`
- Modify: `.gitignore`（不用改）、`requirements.txt`（无新增）

**Interfaces:**
- Consumes: 官方 `mcp` SDK（已装 1.30.0）
- Produces:
  - HTTP 端点 `http://127.0.0.1:8931/mcp`（工具 `query_logistics`）、`http://127.0.0.1:8932/mcp`（工具 `query_warranty`、`query_refund_progress`）
  - 载荷统一 `{"ok": true, "data": {...}}`，状态字段给**英文枚举码**（`IN_TRANSIT`／`IN_WARRANTY`／`PROCESSING`…），翻人话归 T6 的 `ENUM_LABELS`——正好证明外部数据也走我们这套格式化
  - `mockgen.logistics_payload(order_id)`、`mockgen.warranty_payload(order_id)`、`mockgen.refund_progress_payload(order_id)` → dict（同一入参永远同一结果）
  - 演示夹具：`LOGISTICS_SLOW_SECONDS`／`AFTERSALE_SLOW_SECONDS`（首字延迟）、`LOGISTICS_FAIL_TIMES`（前 N 次抛错）、`LOGISTICS_EXTRA_TOOLS`／`AFTERSALE_EXTRA_TOOLS`（逗号分隔的额外工具名，验收③用）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_mcp_servers.py
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from langchain_mcp_adapters.client import MultiServerMCPClient

REPO = Path(__file__).resolve().parents[1]
LOGISTICS = {"url": "http://127.0.0.1:8931/mcp", "transport": "streamable_http"}
AFTERSALE = {"url": "http://127.0.0.1:8932/mcp", "transport": "streamable_http"}

pytestmark = pytest.mark.skipif(not os.environ.get("RUN_MCP_INTEGRATION", "1") == "1",
                                reason="需要真进程")


def wait_until_open(port: int, proc: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Server 进程提前退出，返回码 {proc.returncode}")
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"端口 {port} 在 {timeout}s 内没起来")


class Server:
    def __init__(self, module: str, port: int, env: dict | None = None):
        self.port = port
        self.proc = subprocess.Popen([sys.executable, "-m", module],
                                     cwd=REPO, env={**os.environ, **(env or {})})
        wait_until_open(self.port, self.proc)

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


async def call(url: str, name: str, args: dict):
    client = MultiServerMCPClient({"probe": {"url": url, "transport": "streamable_http"}},
                                  handle_tool_errors=False)
    tools = await client.get_tools(server_name="probe")
    tool = next((item for item in tools if item.name == name), None)
    assert tool is not None, [item.name for item in tools]
    message = await tool.ainvoke({"type": "tool_call", "name": name, "args": args, "id": "call_1"})
    blocks = message.content if isinstance(message.content, list) else [{"type": "text", "text": message.content}]
    text = "".join(item["text"] for item in blocks if item.get("type") == "text")
    return tools, json.loads(text)


@pytest.fixture()
def logistics():
    server = Server("mcp_servers.logistics_server", 8931)
    yield server
    server.stop()


@pytest.fixture()
def aftersale():
    server = Server("mcp_servers.aftersale_server", 8932)
    yield server
    server.stop()


def test_logistics_server_exposes_query_logistics_with_our_contract(logistics):
    tools, payload = asyncio.run(call(LOGISTICS["url"], "query_logistics", {"order_id": "1001"}))
    assert {item.name for item in tools} == {"query_logistics"}
    assert payload["ok"] is True
    assert set(payload["data"]) >= {"order_id", "carrier", "tracking_no", "status", "nodes"}
    assert payload["data"]["status"] == "IN_TRANSIT" or payload["data"]["status"].isupper()


def test_same_order_id_always_gives_the_same_track(logistics):
    first = asyncio.run(call(LOGISTICS["url"], "query_logistics", {"order_id": "1001"}))[1]
    second = asyncio.run(call(LOGISTICS["url"], "query_logistics", {"order_id": "1001"}))[1]
    other = asyncio.run(call(LOGISTICS["url"], "query_logistics", {"order_id": "2002"}))[1]
    assert first == second                                   # 确定性：验收与测试能对答案
    assert first["data"]["nodes"] != other["data"]["nodes"]   # 随机性：不同订单给不同轨迹


def test_aftersale_server_lists_two_warranty_tools(aftersale):
    tools, payload = asyncio.run(call(AFTERSALE["url"], "query_warranty", {"order_id": "1001"}))
    assert {item.name for item in tools} == {"query_warranty", "query_refund_progress"}
    assert payload["data"]["status"] in {"IN_WARRANTY", "OUT_OF_WARRANTY"}
    progress = asyncio.run(call(AFTERSALE["url"], "query_refund_progress", {"order_id": "1001"}))[1]
    assert progress["data"]["status"] in {"PROCESSING", "APPROVED", "REJECTED", "REFUNDED"}


def test_fail_times_knob_raises_on_the_first_calls(tmp_path):
    server = Server("mcp_servers.logistics_server", 8931, {"LOGISTICS_FAIL_TIMES": "1"})
    try:
        with pytest.raises(Exception):
            asyncio.run(call(LOGISTICS["url"], "query_logistics", {"order_id": "1001"}))
        ok = asyncio.run(call(LOGISTICS["url"], "query_logistics", {"order_id": "1001"}))[1]
        assert ok["ok"] is True
    finally:
        server.stop()


def test_extra_tools_knob_adds_a_tool_without_touching_client_code():
    before = Server("mcp_servers.logistics_server", 8931)
    try:
        names = {item.name for item in asyncio.run(
            MultiServerMCPClient({"probe": LOGISTICS}).get_tools(server_name="probe"))}
        assert "query_station_info" not in names
    finally:
        before.stop()
    after = Server("mcp_servers.logistics_server", 8931, {"LOGISTICS_EXTRA_TOOLS": "query_station_info"})
    try:
        tools, payload = asyncio.run(call(LOGISTICS["url"], "query_station_info", {"station": "上海"}))
        assert "query_station_info" in {item.name for item in tools}
        assert payload["ok"] is True
    finally:
        after.stop()
```

- [ ] **Step 2: 跑到红** — Run: `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_mcp_servers.py -q`；Expected: `ModuleNotFoundError: No module named 'mcp_servers'`。

- [ ] **Step 3: 写 `mcp_servers/mockgen.py`**

```python
"""Server 内的 mock 数据：按入参做**确定性**伪随机。

需求要「随机生成 mock」，纯随机会让验收②和集成测试无法对答案，
所以种子取工具名＋入参（规格 §18-C1）。不接真实系统，也不建任何表。
"""
import hashlib
import random

CARRIERS = ["顺丰速运", "中通快递", "圆通速递", "京东物流", "邮政EMS"]
STATIONS = ["上海转运中心", "杭州转运中心", "北京顺义分拨", "广州佛山分拨", "武汉鄂州分拨"]
STEPS = ["包裹已揽收，快递员{courier}", "干线运输中，下一站{station}", "已到达{station}，正在分拣",
         "派送中，预计今日送达", "已签收，感谢使用"]
LOGISTICS_STATUS = ["PICKED_UP", "IN_TRANSIT", "IN_TRANSIT", "DELIVERED"]


def seeded(*parts) -> random.Random:
    key = "|".join(str(part) for part in parts)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def _fill(text: str, rnd: random.Random) -> str:
    return text.format(courier=rnd.choice(["张伟", "李静", "王强"]), station=rnd.choice(STATIONS))


def logistics_payload(order_id: str) -> dict:
    rnd = seeded("query_logistics", order_id)
    steps = rnd.randint(2, 4)
    nodes = [{"time": f"2026-10-{20 + index:02d} {9 + index * 3:02d}:{index * 7 % 60:02d}",
              "text": _fill(STEPS[min(index, len(STEPS) - 1)], rnd)} for index in range(steps)]
    return {"order_id": order_id, "carrier": rnd.choice(CARRIERS),
            "tracking_no": f"SF{rnd.randint(10**11, 10**12 - 1)}",
            "status": LOGISTICS_STATUS[min(steps - 1, len(LOGISTICS_STATUS) - 1)],
            "nodes": nodes}


def warranty_payload(order_id: str) -> dict:
    rnd = seeded("query_warranty", order_id)
    days_left = rnd.randint(-30, 330)
    return {"order_id": order_id,
            "status": "IN_WARRANTY" if days_left >= 0 else "OUT_OF_WARRANTY",
            "days_left": days_left, "expires_on": f"2027-{rnd.randint(1, 12):02d}-{rnd.randint(1, 28):02d}"}


def refund_progress_payload(order_id: str) -> dict:
    rnd = seeded("query_refund_progress", order_id)
    status = rnd.choice(["PROCESSING", "APPROVED", "REJECTED", "REFUNDED"])
    remark = {"PROCESSING": "退款审核中，1-3 个工作日内到账", "APPROVED": "审核通过，等待打款",
              "REJECTED": "审核未通过：商品已签收超过 7 天", "REFUNDED": "款项已原路退回"}[status]
    return {"order_id": order_id, "status": status, "remark": remark,
            "updated_at": f"2026-10-{rnd.randint(20, 29):02d} {rnd.randint(8, 20):02d}:00"}
```

- [ ] **Step 4: 写 `mcp_servers/logistics_server.py`**

```python
"""物流查询 MCP Server（独立进程，8931）。ch08 需求 6：物流查询由 MCP 接管。"""
import asyncio
import os

from mcp.server.fastmcp import FastMCP

from mcp_servers.mockgen import STATIONS, logistics_payload, seeded

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


@mcp.tool()
async def query_logistics(order_id: str) -> dict:
    """按订单号查询物流轨迹，返回承运商、运单号、状态与节点列表。"""
    await _lag_and_fail()
    return {"ok": True, "data": logistics_payload(order_id)}


if "query_station_info" in EXTRA_TOOLS:
    @mcp.tool()
    async def query_station_info(station: str) -> dict:
        """查询转运站点的作业情况（验收③演示用：只重启本 Server 就能被客服系统发现）。"""
        rnd = seeded("query_station_info", station)
        return {"ok": True, "data": {"station": station,
                                     "queue": rnd.randint(0, 40),
                                     "backlog": rnd.choice(["正常", "积压"])}}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
```
`aftersale_server.py` 同构：端口 8932、`AFTERSALE_*` 三个同名前缀夹具、两个工具 `query_warranty`／`query_refund_progress`，`EXTRA_TOOLS` 里给一个 `query_coupon_balance`（读）。

- [ ] **Step 5: 写 `start-mcp.bat`（双击起两个进程）**

```bat
@echo off
cd /d "%~dp0"
set PY=.venv\Scripts\python.exe
start "mcp-logistics" "%PY%" -m mcp_servers.logistics_server
start "mcp-aftersale" "%PY%" -m mcp_servers.aftersale_server
echo MCP servers started on 8931 and 8932. Close these windows to stop.
timeout /t 3 >nul
```
> 行内不写中文注释：cmd 对长中文行的处理会截断（本机已踩过）。中文说明放 README。

- [ ] **Step 6: 跑到绿** — 同 Step 2 命令；Expected: 5 passed（两条起进程的约 20s）。顺手手测：`start-mcp.bat` 双击后两个窗口都停在 `Uvicorn running on http://127.0.0.1:893x`。
- [ ] **Step 7: 本地提交** `git commit -am "feat(ch08/T11): 物流与售后两个 MCP Server"`

---

### Task 12: Client 接入、注册表合并、内置 `query_logistics` 下线

需求 1＋需求 6 的后半段：MCP 连上就现取工具、与内置合成一份表；物流查询改由 MCP 提供，内置那个删掉，不留重名。规格 §6、§10。

**Files:**
- Create: `app/tools/mcp.py`、`tests/test_mcp_client.py`
- Modify: `app/tools/builtin.py`（删 `LOGISTICS` 字典与 `query_logistics` 工具，`TOOL_LABELS` 保留中文名给徽章）、`app/tools/wiring.py`（无改，`mcp_specs` 已在 T9 留好口子）、`app/workflow/agent_node.py`（`deps.mcp` 现取）、`app/service.py`（构造 `McpDiscovery` 并塞进 deps）、`app/workflow/graph.py`（`WorkflowDeps` 加 `mcp`）、`scripts/demo_tools.py`（前置条件提示）
- Test（适配）: `tests/test_tools.py:53-55,75-83`、`tests/test_chat_tools.py`、`tests/test_workflow_agent.py:102`、`tests/test_api.py:27`、`tests/support.py:70`、`tests/test_tool_wiring.py:66`

**Interfaces:**
- Consumes: `MultiServerMCPClient`、`spec_from_tool`、`build_registry(mcp_specs=...)`
- Produces:
  ```python
  # app/tools/mcp.py
  CLIENT_TRANSPORT = "streamable_http"
  def server_endpoints(settings) -> dict[str, tuple[str, str]]   # {name: (url, permission)}
  class McpDiscovery:
      def __init__(self, settings) -> None
      async def specs(self, *, force: bool = False) -> list[ToolSpec]
      def forget(self, name: str | None = None) -> None          # 连不上时清缓存，下次现取
  ```
  `deps.mcp` 为 `None`（未配置 MCP 或服务层没注入）时，一切照 T10 的纯内置跑——单测不依赖真进程。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_mcp_client.py
import asyncio
import json

import pytest

from app.config import Settings
from app.tools.mcp import McpDiscovery, server_endpoints


class FakeAdaptersClient:
    """只替身 adapters 的 get_tools，验我们这侧的合并与权限口径。"""

    def __init__(self, connections, **kwargs):
        self.connections = connections
        self.handle_tool_errors = kwargs.get("handle_tool_errors")
        self.server_name = None

    async def get_tools(self, *, server_name=None):
        self.server_name = server_name
        from tests.test_mcp_client import fake_tool

        if server_name == "broken":
            raise OSError("连不上")
        if server_name == "logistics":
            return [fake_tool("query_logistics", "按订单号查询物流轨迹。")]
        return [fake_tool("query_warranty", "查询是否在保。"),
                fake_tool("create_shipment", "发货（外部写操作）")]


def fake_tool(name, description):
    from langchain_core.tools import StructuredTool

    return StructuredTool(name=name, description=description, coroutine=lambda **kwargs: None,
                          args_schema={"type": "object", "properties": {"order_id": {"type": "string"}}})


def settings(**overrides):
    base = {"openai_base_url": "http://upstream.invalid/v1", "openai_model": "m", "openai_api_key": "k"}
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_endpoints_come_from_settings_and_keep_local_permission(monkeypatch):
    table = server_endpoints(settings(mcp_servers="logistics, aftersale"))
    assert set(table) == {"logistics", "aftersale"}
    assert table["logistics"][0].endswith("/mcp")
    assert table["aftersale"][1] == "read"
    assert server_endpoints(settings(mcp_servers="")) == {}


def test_discovery_marks_source_server_and_permission(monkeypatch):
    monkeypatch.setattr("app.tools.mcp.MultiServerMCPClient", FakeAdaptersClient)
    specs = asyncio.run(McpDiscovery(settings(mcp_servers="logistics,aftersale",
                                              mcp_aftersale_permission="write")).specs())
    by_name = {item.name: item for item in specs}
    assert by_name["query_logistics"].source == "mcp"
    assert by_name["query_logistics"].mcp_server == "logistics"
    assert by_name["query_logistics"].label == "查询物流"          # TOOL_LABELS 里留着中文名
    assert by_name["query_warranty"].label == "query_warranty"    # 没登记的用工具名
    assert by_name["query_warranty"].permission == "write"         # 我们这侧标的，不看 Server 声明
    assert by_name["create_shipment"].permission == "write"


def test_handle_tool_errors_is_forced_off(monkeypatch):
    captured = {}
    monkeypatch.setattr("app.tools.mcp.MultiServerMCPClient",
                        lambda connections, **kwargs: captured.setdefault("kwargs", kwargs) or FakeAdaptersClient(connections, **kwargs))
    asyncio.run(McpDiscovery(settings(mcp_servers="logistics")).specs())
    assert captured["kwargs"]["handle_tool_errors"] is False


def test_dead_server_does_not_break_the_others_and_is_refetched_next_time(monkeypatch):
    monkeypatch.setattr("app.tools.mcp.MultiServerMCPClient", FakeAdaptersClient)
    discovery = McpDiscovery(settings(mcp_servers="logistics,broken", tool_discovery_cache_seconds=0))
    names = [item.name for item in asyncio.run(discovery.specs())]
    assert names == ["query_logistics"]
    assert asyncio.run(discovery.specs())                          # 不抛，下一轮再试


def test_cache_honours_ttl_and_force_skips_it(monkeypatch):
    calls = []

    class Counting(FakeAdaptersClient):
        async def get_tools(self, *, server_name=None):
            calls.append(server_name)
            return await super().get_tools(server_name=server_name)

    monkeypatch.setattr("app.tools.mcp.MultiServerMCPClient", Counting)
    discovery = McpDiscovery(settings(mcp_servers="logistics", tool_discovery_cache_seconds=60))
    asyncio.run(discovery.specs())
    asyncio.run(discovery.specs())
    assert calls == ["logistics"]                                  # 第二次命中缓存
    asyncio.run(discovery.specs(force=True))
    assert calls == ["logistics", "logistics"]


def test_registry_merges_mcp_tools_and_builtin_no_longer_clashes(store_factory):
    """验收②的形状：物流只剩一个工具，来源是 MCP；内置的四个还在。"""
    from app.tools.builtin import build_specs
    from app.tools.registry import ToolRegistry
    from app.tools.wiring import build_registry

    store = ConversationStore(store_factory)
    conversation_id = store.start_turn(user_id="anon-1", message="物流到哪了")
    mcp_spec = spec_from_tool(fake_tool("query_logistics", "按订单号查询物流轨迹。"),
                              source="mcp", mcp_server="logistics", label="查询物流")
    registry = build_registry(store, conversation_id, None, settings=settings(), mcp_specs=[mcp_spec])
    assert registry.names().count("query_logistics") == 1
    assert registry.get("query_logistics").source == "mcp"
    assert {"query_order", "query_product", "query_faq", "create_ticket"} <= set(registry.names())
    assert "query_logistics" not in {item.name for item in build_specs(store, conversation_id, None)}
```

- [ ] **Step 2: 跑到红** — Run: `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_mcp_client.py -q`；Expected: `ModuleNotFoundError: No module named 'app.tools.mcp'`。

- [ ] **Step 3: 写 `app/tools/mcp.py`**

```python
import logging
import time

from langchain_mcp_adapters.client import MultiServerMCPClient

from app.tools.builtin import TOOL_LABELS
from app.tools.registry import spec_from_tool

logger = logging.getLogger(__name__)

# Client 侧字面量是下划线（sessions.py:167）；Server 侧 run(transport="streamable-http") 是连字符。
CLIENT_TRANSPORT = "streamable_http"
DEFAULT_PERMISSION = {"logistics": "read", "aftersale": "read"}


def server_endpoints(settings) -> dict[str, tuple[str, str]]:
    configured = [name.strip() for name in (settings.mcp_servers or "").split(",") if name.strip()]
    table = {"logistics": (settings.mcp_logistics_url, settings.mcp_logistics_permission),
             "aftersale": (settings.mcp_aftersale_url, settings.mcp_aftersale_permission)}
    return {name: table[name] for name in configured if name in table}


class McpDiscovery:
    """现问现拿：每次 get_tools() 都新建 session（adapters 语义），所以 Server 侧新加工具
    只重启该 Server 就可见。TTL（缺省 60 秒，见 T2）只是省掉同一进程内的重复握手：
    新工具最多 60 秒后出现在表里，客服系统代码不动、不重启；演示要立刻看到就 `specs(force=True)`
    或把 `TOOL_DISCOVERY_CACHE_SECONDS` 设成 0。"""

    def __init__(self, settings) -> None:
        self.settings = settings
        self._cache: dict[str, tuple[float, list]] = {}

    async def specs(self, *, force: bool = False) -> list:
        results = []
        for name, (url, permission) in server_endpoints(self.settings).items():
            cached = self._cache.get(name)
            ttl = self.settings.tool_discovery_cache_seconds
            if not force and cached and time.monotonic() - cached[0] < ttl:
                results.extend(cached[1])
                continue
            try:
                client = MultiServerMCPClient({name: {"url": url, "transport": CLIENT_TRANSPORT}},
                                              handle_tool_errors=False)
                tools = await client.get_tools(server_name=name)
            except Exception:
                logger.warning("MCP Server %s（%s）发现失败，本轮跳过它的工具。", name, url)
                self.forget(name)
                if cached:
                    results.extend(cached[1])
                continue
            specs = [spec_from_tool(item, source="mcp", mcp_server=name, permission=permission,
                                    label=TOOL_LABELS.get(item.name)) for item in tools]
            self._cache[name] = (time.monotonic(), specs)
            logger.info("MCP 发现：%s ← %s（%s）", name, "、".join(item.name for item in specs), permission)
            results.extend(specs)
        return results

    def forget(self, name: str | None = None) -> None:
        self._cache.pop(name, None) if name else self._cache.clear()
```
`spec_from_tool(..., label=None)` 会退回工具名（T9 已实现），所以这里传 `TOOL_LABELS.get(...)` 的 `None` 也安全。

- [ ] **Step 4: 内置 `query_logistics` 下线**

`app/tools/builtin.py`：删掉 `LOGISTICS` 常量与 `make_tools` 里的 `query_logistics` 分支，`TOOL_LABELS` 的 `"query_logistics": "查询物流"` **保留**（MCP 同名工具徽章照用）；`build_specs` 去掉 `include_logistics` 形参（T3 留的口子就此关闭）。`app/tools/__init__.py` 的再导出删掉 `LOGISTICS`。

配套用例改动（逐个点名，别漏）：
- `tests/test_tools.py:53`：`assert list(tools) == ["query_order", "query_product", "query_faq", "create_ticket"]`；`:54` 的 `TOOL_LABELS["query_logistics"] == "查询物流"` 保留，但 `:55` 改成 `assert set(tools) <= set(TOOL_LABELS)`；`:75-83 test_query_logistics_returns_nodes` **删除**（语义已搬到 `tests/test_mcp_servers.py::test_logistics_server_exposes_query_logistics_with_our_contract`）。
- `tests/support.py:70`：`streamed_tool_call(name="query_order", ...)`（默认替身不再假定内置物流）。
- `tests/test_chat_tools.py`：夹具里给 fake model 发的仍是 `query_logistics` 调用，改为通过 `build_registry(..., mcp_specs=[spec_from_tool(fake_logistics_tool, source="mcp", mcp_server="logistics", label="查询物流")])` 注入一个假 MCP 工具——顺带把徽章的 `source`/`mcp_server` 一起验了。
- `tests/test_workflow_agent.py:102`：`model.tool_names` 期望改成四个内置工具名；再加一条「`deps.mcp` 给出 specs 时五个名字都在」。
- `tests/test_api.py:27`：同上口径。
- `scripts/demo_tools.py:8`：`订单物流` 那条 case 顶部打印提示「需要先 `start-mcp.bat`」，脚本检测到注册表里没有 `query_logistics` 时明确打印这句人话，不静默失败。

- [ ] **Step 5: 接进服务层**

`app/workflow/graph.py` 的 `WorkflowDeps` 加 `mcp: object = None`；`app/service.py:62-75` 构造 `McpDiscovery(settings)` 传入。`app/workflow/agent_node.py`：

```python
        mcp_specs = await deps.mcp.specs() if deps.mcp is not None else ()
        registry = build_registry(deps.store, conversation_id, deps.knowledge, settings=deps.settings,
                                  mcp_specs=mcp_specs, exclude=exclude)
```
`/api/tickets`、`/api/refunds` 两条按钮路径**不**接 MCP：它们只调内置 `create_ticket`，省一次握手，也少一处外部故障面（写进 README）。

- [ ] **Step 6: 跑到绿（含真进程）**

Run: `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_mcp_client.py tests/test_mcp_servers.py -q` → PASS。
再跑一次端到端手测：起两个 Server，用 `ToolEngine` 直接 `run(name="query_logistics", args={"order_id": "1001"}, conversation_id=…)`，确认 `audit` 行里 `tool_source='mcp'`、`mcp_server='logistics'`，且 `content` 里 `status` 已经是「运输中」这类人话（证明外部数据也过了 T6 的格式化）。
- [ ] **Step 7: 全量回归＋本地提交** `LANGSMITH_TRACING=false .venv/Scripts/python.exe -X utf8 -m pytest -q` → 全绿；`git commit -am "feat(ch08/T12): MCP 工具接入注册表，物流查询移交外部 Server"`

**实现期修正（T12 完成后回填，2026-10-31）**
- Step 3 的 `specs()` 原文顺序握手、无超时：真机两个 Server 都停着时各等约 2.3 秒、合计 4.7 秒，把 `tests/test_streaming.py` 三条 5 秒预算的用例拖成 ReadTimeout。实现改为 `asyncio.gather` 并发 + 每台 `asyncio.timeout(settings.tool_timeout_seconds)`；失败仍 `forget(name)`，本轮的旧清单照旧顶用。
- Step 1 从 6 条增到 7 条：补 `test_a_stalled_server_is_bounded_by_the_tool_timeout`（`tool_timeout_seconds=0.2` 下停摆 Server 必须 2 秒内放弃）。
- 计划文件清单漏改两处：`tests/test_tool_registry.py`（两处断言写死五工具，改成四项、删 `query_logistics` 那行）、`tests/test_streaming.py`（`running_server` 的 Settings 补 `mcp_servers=""`，单测不连真进程）。
- Step 1 的 `tests/support.py` 默认值补 `mcp_servers=""`，并加 `FakeMcp`／`mcp_logistics_spec()` 给 `test_chat_tools.py`、`test_workflow_agent.py` 复用；`test_workflow_agent.py` 新增「`deps.mcp` 给出清单时五个名字都在」。
- Step 5 末尾「按钮路径不接 MCP 写进 README」转 T18（ch08 README 章节整体在 T18 写，避免现在留孤立说明）。
- 全量：**417 passed, 3 skipped**（410 － 内置物流用例 1 ＋ T12 新增 8）。

**评审修复（T12 完成后回填，2026-10-31）**
- I1 第三方工具可能缺 `description`／`schema`：原实现只把握手包在 try 里，spec 构造与校验在外，坏工具照进缓存，`ToolRegistry` 每轮整表拒收 → 服务层回落 `invalid_model_output`，整轮只剩 `meta`＋`error`。`_fetch` 改为逐件 `spec_from_tool`＋`check_spec`（`registry._check` 顺势公开成 `check_spec`），坏的跳过并 WARNING；新增 `test_incomplete_external_tool_is_skipped_not_fatal`。
- I2 `gather` 并发无测试：改回顺序 await 原来仍全绿。新增 `test_two_stalled_servers_wait_concurrently`（两台各 `tool_timeout_seconds=0.5`，总耗时 < 0.8s）；变异实测变红。
- I3 「失败回退旧清单」无测试：删掉 `return list(cached[1]) if cached else []` 仍全绿。新增 `test_a_failed_refetch_keeps_the_old_list_for_this_turn_only`（成功缓存 → 断电 `force` 仍返回旧清单 → 下一轮重新握手且为空）；变异实测变红。
- Minor ①：`server_endpoints` 对未登记名字发 WARNING；②：超时日志带异常类型名（`TimeoutError` 的 str 是空串）；③：「最坏等待＝一次超时」改为「近似一次超时（含取消清理）」；④：README 的「查物流（内置 mock 数据）」改成 MCP 提供；⑤：dev-notes 阶段 24 里「失败不清空已缓存」的说法与代码对齐。
- 未采纳：评审建议再加一条 API 级「MCP 卡住仍按时首包」用例——`McpDiscovery` 的超时用例已直接卡住握手，`test_streaming.py` 另测首包时序；再加一条会是同一断言的第二次拷贝。
- 全量：**420 passed, 3 skipped**（417 ＋ 3 条新用例）。

---

### Task 13: 确认流——interrupt 节点、两条分支与双模式拆包

需求 7 的后端半段：模型发起写操作 → 引擎不执行 → 图在 `confirm_ticket` 挂起 → 预览推给前端 → 确认才执行、取消按「权限拒绝」落审计。规格 §7、§11。

三条被 T1 钉住的性质决定了写法：① 纯 `custom` 流看不见中断，服务层必须 `["custom","updates"]` 双模式；② **含 `interrupt()` 的节点 resume 时从头重跑**，所以 `emit`／落库这些副作用一律放上游 `agent_node`，`confirm_ticket` 节点里只留一句 `interrupt()`；③ 重复 resume 是空操作，但挂起期间灌新输入会再挂一个新的 interrupt ⇒ 确认帧必须带 nonce（`pending_write["pending_id"]`，T10 生成）。

**Files:**
- Create: `app/workflow/confirm_nodes.py`、`tests/test_workflow_confirm.py`
- Modify: `app/workflow/events.py`（`CUSTOM_UPDATES`＋`unwrap`）、`app/workflow/graph.py:47-77`（三节点两条条件边）、`:130-141`（`respond` 走 `_drive`，新增 `resume`）、`app/workflow/agent_node.py`（暂停轮落库＋早退）、`app/workflow/state.py`（`write_decision`）、`app/prompts.py`（四段文案＋`AGENT_ADDENDUM` 第 4 条）

**Interfaces:**
- Consumes: `ReActResult.pending_write`（T10）、`ToolEngine.run(confirmed=True)`／`record_denial`（T8）、`build_registry`／`build_engine`（T9）、`TOOL_LABELS`
- Produces:
  ```python
  # app/workflow/events.py
  CUSTOM_UPDATES = ["custom", "updates"]
  def unwrap(mode: str, data: object) -> list[tuple[str, dict]]
  # app/workflow/confirm_nodes.py
  def write_preview(state) -> dict
  def agent_selector(state) -> str            # "confirm" | "log"
  def decision_selector(state) -> str         # "execute" | "cancel"
  def make_confirm_ticket_node()
  def make_execute_write_node(deps)
  def make_write_cancelled_node(deps)
  # app/workflow/graph.py
  async def Workflow.resume(self, conversation_id: int, decision: str)   # 同样 yield (event, payload)
  # app/prompts.py
  TICKET_CONFIRM_TEXT / TICKET_CREATED_TEXT / TICKET_CANCELLED_TEXT / TICKET_FAILED_TEXT
  ```
  `confirm` 帧载荷＝`write_preview(state)`，形状（T14/T15 都靠它）：
  `{"pending_id","tool","tool_call_id","label","turn","question","fields":[{"label","value"}],"note"}`。
  **我替你定的**：暂停轮不再发 `actions` 帧，卡片就是这一步唯一的按钮（否则「建工单」按钮和确认卡片同时在场，用户会点两遍）；`agent` 之后的边从 `add_edge("agent","log")` 换成条件边，其余节点不动。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_workflow_confirm.py
import asyncio

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select

from app.models import Message, Ticket, ToolAuditLog
from app.prompts import (AGENT_ADDENDUM, TICKET_CANCELLED_TEXT, TICKET_CONFIRM_TEXT, TICKET_CREATED_TEXT)
from app.storage import ConversationStore
from app.workflow.events import unwrap
from app.workflow.graph import Workflow, WorkflowDeps
from tests.support import (FixedClassifier, ScriptedModel, _settings, call, msg_chunk, sqlite_factory)

TICKET_ARGS = {"description": "保温杯收到就是漏的，想退货", "ticket_type": "售后"}
TICKET_ROUND = [msg_chunk("", "tool_calls", [call("create_ticket", TICKET_ARGS, "call_1")])]


def build(factory, model):
    """route=agent 的直达路径：分类器固定成「其他」，绕开检索与闸门。"""
    deps = WorkflowDeps(model=model, settings=_settings({}), store=ConversationStore(factory),
                        classifier=FixedClassifier("其他"))
    return Workflow(deps=deps, checkpointer=InMemorySaver())


def drive(workflow, conversation_id, question="帮我建个工单，杯子是漏的"):
    messages = [SystemMessage(content="base system"), HumanMessage(content=question)]

    async def run():
        return [item async for item in workflow.respond(conversation_id, question, messages)]
    return asyncio.run(run())


def resume(workflow, conversation_id, decision):
    async def run():
        return [item async for item in workflow.resume(conversation_id, decision)]
    return asyncio.run(run())


def paused(factory, model, user_id="confirm-1"):
    """跑一轮到挂起，返回 (workflow, conversation_id, 卡片载荷)。"""
    store = ConversationStore(factory)
    conversation_id = store.start_turn(user_id=user_id, message="帮我建个工单，杯子是漏的")
    workflow = build(factory, model)
    frames = drive(workflow, conversation_id)
    return workflow, conversation_id, dict(frames[-2][1])


def count(factory, model_class):
    with factory() as session:
        return session.scalar(select(func.count()).select_from(model_class)) or 0


def assistant_rows(factory, conversation_id):
    with factory() as session:
        rows = session.scalars(select(Message).where(Message.conversation_id == conversation_id,
                                                     Message.role == "assistant").order_by(Message.id))
        return [row.content for row in rows]


def test_paused_turn_emits_confirm_then_done_and_creates_nothing():
    with sqlite_factory() as factory:
        workflow, conversation_id, card = paused(factory, ScriptedModel([TICKET_ROUND]))
        assert card["tool"] == "create_ticket" and card["label"] == "建工单"
        assert card["tool_call_id"] == "call_1" and card["turn"] == 1
        assert len(card["pending_id"]) >= 8
        assert {"工单类型", "问题描述", "所属会话"} <= {item["label"] for item in card["fields"]}
        assert "确认后" in card["note"]
        assert count(factory, Ticket) == 0 and count(factory, ToolAuditLog) == 0
        # log 节点本轮跑不到，暂停轮的正文由 agent 节点落库，且只落这一条
        assert assistant_rows(factory, conversation_id) == [TICKET_CONFIRM_TEXT]


def test_graph_is_parked_on_the_confirm_node():
    with sqlite_factory() as factory:
        workflow, conversation_id, card = paused(factory, ScriptedModel([TICKET_ROUND]))

        async def read():
            return await workflow.get_state(conversation_id)
        snapshot = asyncio.run(read())
        assert snapshot.next == ("confirm_ticket",)
        assert snapshot.values["pending_write"]["pending_id"] == card["pending_id"]
        assert snapshot.values["stopped_by"] == "await_confirm"


def test_accept_executes_once_writes_the_ticket_and_logs_the_reply():
    with sqlite_factory() as factory:
        workflow, conversation_id, card = paused(factory, ScriptedModel([TICKET_ROUND]))
        frames = resume(workflow, conversation_id, "accept")
        assert [name for name, _ in frames] == ["status", "status", "token", "done"]
        assert (frames[0][1]["phase"], frames[0][1]["tool"]) == ("start", "create_ticket")
        assert frames[1][1]["ok"] is True and "工单" in frames[1][1]["summary"]
        text = frames[2][1]["content"]
        with factory() as session:
            ticket = session.scalars(select(Ticket)).one()
            audit = session.scalars(select(ToolAuditLog)).one()
        assert text == TICKET_CREATED_TEXT.format(ticket_no=ticket.ticket_no)
        assert (audit.status, audit.tool_name, audit.tool_call_id) == ("成功", "create_ticket", "call_1")
        assert count(factory, Ticket) == 1
        assert assistant_rows(factory, conversation_id) == [TICKET_CONFIRM_TEXT, text]

        async def read():
            return await workflow.get_state(conversation_id)
        values = asyncio.run(read()).values
        assert values["pending_write"] is None            # 卡片就此作废，重复 resume 打不到第二次


def test_cancel_creates_no_ticket_but_audits_a_denial():
    """验收⑤：点取消 → 工单没建、审计状态「权限拒绝」。"""
    with sqlite_factory() as factory:
        workflow, conversation_id, _ = paused(factory, ScriptedModel([TICKET_ROUND]))
        frames = resume(workflow, conversation_id, "cancel")
        assert [name for name, _ in frames] == ["token", "done"]
        assert frames[0][1]["content"] == TICKET_CANCELLED_TEXT
        assert count(factory, Ticket) == 0
        with factory() as session:
            audit = session.scalars(select(ToolAuditLog)).one()
        assert (audit.status, audit.retry_count, audit.tool_call_id) == ("权限拒绝", 0, "call_1")
        assert audit.arguments == TICKET_ARGS


def test_a_second_resume_is_a_silent_no_op():
    with sqlite_factory() as factory:
        workflow, conversation_id, _ = paused(factory, ScriptedModel([TICKET_ROUND]))
        assert count(factory, Ticket) == 0
        resume(workflow, conversation_id, "accept")
        assert resume(workflow, conversation_id, "accept") == []
        assert count(factory, Ticket) == 1


def test_anything_but_accept_takes_the_cancel_branch():
    """选择器不许因为意外的 resume 值抛 KeyError：写操作宁可不做。"""
    with sqlite_factory() as factory:
        workflow, conversation_id, _ = paused(factory, ScriptedModel([TICKET_ROUND]))
        resume(workflow, conversation_id, "whatever")
        assert count(factory, Ticket) == 0
        assert count(factory, ToolAuditLog) == 1


def test_unwrap_translates_custom_updates_and_interrupts():
    from types import SimpleNamespace

    assert unwrap("custom", ("token", {"content": "你好"})) == [("token", {"content": "你好"})]
    assert unwrap("updates", {"agent": {"reply": "你好"}}) == []
    assert unwrap("updates", None) == []
    frames = unwrap("updates", {"__interrupt__": [SimpleNamespace(value={"tool": "create_ticket"})]})
    assert [name for name, _ in frames] == ["confirm", "done"]
    assert frames[0][1] == {"tool": "create_ticket"}
    assert frames[1][1] == {"finish_reason": "stop"}


def test_agent_prompt_tells_the_model_to_ask_before_calling_the_write_tool():
    """需求 7 前半段「缺必填就主动追问、不许瞎编」只能靠提示词约束，用文本契约钉住。"""
    for phrase in ("create_ticket", "明确要求", "追问", "不许编造", "售后、投诉、咨询"):
        assert phrase in AGENT_ADDENDUM
```

- [ ] **Step 2: 跑到红** — Run: `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_workflow_confirm.py -q`；Expected: `ModuleNotFoundError: No module named 'app.workflow.confirm_nodes'`（或 `ImportError: cannot import name 'unwrap'`）。

- [ ] **Step 3: `app/workflow/events.py` 加拆包**

```python
CUSTOM_UPDATES = ["custom", "updates"]


def unwrap(mode: str, data: object) -> list[tuple[str, dict]]:
    """把双模式流的一帧翻译成对外事件。

    custom：节点自己发的 (event, payload)，原样透出。
    updates：只关心 interrupt——图挂起时外部拿不到任何 custom 帧，
    这里翻成 confirm＋done，前端才知道要出卡片、并且可以收尾这条流。
    """
    if mode == "custom":
        return [data]
    payload = data if isinstance(data, dict) else {}
    interrupted = payload.get("__interrupt__")
    if not interrupted:
        return []
    value = interrupted[0].value if isinstance(interrupted, (list, tuple)) and interrupted else None
    return [("confirm", value), ("done", {"finish_reason": "stop"})]
```

- [ ] **Step 4: `app/workflow/confirm_nodes.py`**

```python
import json
import logging

from langgraph.types import interrupt

from app.prompts import TICKET_CANCELLED_TEXT, TICKET_CREATED_TEXT, TICKET_FAILED_TEXT
from app.tools.builtin import TOOL_LABELS
from app.tools.wiring import build_engine, build_registry
from app.workflow.events import emit

logger = logging.getLogger(__name__)

CANCELLATION_REASON = "用户在页面取消了这次操作。"
FIELD_LABELS = {"ticket_type": "工单类型", "description": "问题描述"}


def write_preview(state) -> dict:
    """卡片只展示已经确定的东西：参数原样列出来，外加这一轮的会话与订单。"""
    pending = state.get("pending_write") or {}
    args = pending.get("args") or {}
    fields = [{"label": FIELD_LABELS.get(key, key), "value": str(value)} for key, value in args.items()]
    order = state.get("order") or {}
    if order.get("order_id"):
        fields.append({"label": "关联订单", "value": str(order["order_id"])})
    fields.append({"label": "所属会话", "value": f"#{state.get('conversation_id')}"})
    return {"pending_id": pending.get("pending_id"), "tool": pending.get("tool"),
            "tool_call_id": pending.get("tool_call_id"),
            "label": TOOL_LABELS.get(pending.get("tool") or "", pending.get("tool") or "写操作"),
            "turn": state.get("turn_count") or 1, "question": state.get("question") or "",
            "fields": fields, "note": "确认后才会真正执行，取消则什么都不会发生。"}


def make_confirm_ticket_node():
    def confirm_ticket(state) -> dict:
        # resume 时这个节点从头重跑：除了这一句 interrupt，这里什么都不许做。
        decision = interrupt(write_preview(state))
        return {"write_decision": str(decision or "cancel")}

    return confirm_ticket


def agent_selector(state) -> str:
    return "confirm" if state.get("pending_write") else "log"


def decision_selector(state) -> str:
    return "execute" if state.get("write_decision") == "accept" else "cancel"


async def _engine(deps, conversation_id):
    """写操作执行时要的注册表／引擎只能现搭：内置工具是按会话闭包生成的（规格 §6.2）。"""
    registry = build_registry(deps.store, conversation_id, deps.knowledge, settings=deps.settings,
                              mcp_specs=await _mcp_specs(deps))
    return registry, build_engine(registry, settings=deps.settings, session_factory=deps.store.session_factory)


async def _mcp_specs(deps):
    return list(await deps.mcp.specs()) if getattr(deps, "mcp", None) is not None else []


def make_execute_write_node(deps):
    async def execute_write(state) -> dict:
        pending = state.get("pending_write") or {}
        conversation_id = state["conversation_id"]
        name = pending.get("tool") or ""
        registry, engine = await _engine(deps, conversation_id)
        spec = registry.get(name)
        label = spec.label if spec else name
        source = spec.source if spec else "unknown"
        server = spec.mcp_server if spec else None
        emit("status", {"phase": "start", "tool": name, "label": label,
                        "source": source, "mcp_server": server})
        outcome = await engine.run(name=name, args=pending.get("args") or {},
                                   tool_call_id=pending.get("tool_call_id"),
                                   conversation_id=conversation_id, confirmed=True)
        emit("status", {"phase": "end", "tool": name, "label": label, "ok": outcome.ok,
                        "summary": outcome.summary, "duration_ms": outcome.duration_ms,
                        "source": source, "mcp_server": server})
        ticket_no = _ticket_no(outcome.content) if outcome.ok else None
        reply = TICKET_CREATED_TEXT.format(ticket_no=ticket_no) if ticket_no else TICKET_FAILED_TEXT
        logger.info("workflow node=execute_write conv=%s tool=%s ok=%s ticket=%s",
                    conversation_id, name, outcome.ok, ticket_no)
        emit("token", {"content": reply})
        return {"pending_write": None, "reply": reply, "finish_reason": "stop",
                "stopped_by": None if ticket_no else "write_failed"}

    return execute_write


def make_write_cancelled_node(deps):
    async def write_cancelled(state) -> dict:
        pending = state.get("pending_write") or {}
        conversation_id = state["conversation_id"]
        _, engine = await _engine(deps, conversation_id)
        engine.record_denial(name=pending.get("tool") or "", args=pending.get("args") or {},
                             reason=CANCELLATION_REASON, tool_call_id=pending.get("tool_call_id"),
                             conversation_id=conversation_id)
        logger.info("workflow node=write_cancelled conv=%s tool=%s", conversation_id, pending.get("tool"))
        emit("token", {"content": TICKET_CANCELLED_TEXT})
        return {"pending_write": None, "reply": TICKET_CANCELLED_TEXT, "finish_reason": "stop",
                "stopped_by": "cancelled_by_user"}

    return write_cancelled


def _ticket_no(content) -> str | None:
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return None
    return (payload.get("data") or {}).get("ticket_no")
```
`_mcp_specs` 在 T13 阶段 `deps.mcp` 还是 `None`（T12 才注入），所以这段现在只会返回 `[]`——留着是为了 T12 接上后 MCP 写工具也能走同一条确认路径，不必再改这两个节点。

- [ ] **Step 5: 接线 `graph.py`**

```python
from langgraph.types import Command                       # 顶部 import
from app.workflow.confirm_nodes import (agent_selector, decision_selector, make_confirm_ticket_node,
                                        make_execute_write_node, make_write_cancelled_node)
from app.workflow.events import CUSTOM_UPDATES            # 顶部 import（_traced 之前）
```
`build_graph` 里加三个节点，并把 `for name in ("refuse", "agent", "complaint", "chitchat")` 这条循环里的 `"agent"` 摘掉：
```python
    builder.add_conditional_edges("agent", agent_selector, {"log": "log", "confirm": "confirm_ticket"})
    builder.add_conditional_edges("confirm_ticket", decision_selector,
                                  {"execute": "execute_write", "cancel": "write_cancelled"})
    builder.add_edge("execute_write", "log")
    builder.add_edge("write_cancelled", "log")
```
`Workflow` 的运行器换成一个入口两处复用：
```python
    async def _drive(self, graph_input, config):
        stream = self.graph.astream(graph_input, config, stream_mode=CUSTOM_UPDATES, durability="sync")
        try:
            async for mode, data in stream:
                for event, payload in unwrap(mode, data):
                    yield event, payload
        finally:
            await stream.aclose()

    async def respond(self, conversation_id, question, messages, *, context_plan=None, current_message_id=None):
        payload = {...原样...}
        async for item in self._drive(payload, self._config(conversation_id)):
            yield item

    async def resume(self, conversation_id: int, decision: str):
        async for item in self._drive(Command(resume=decision), self._config(conversation_id)):
            yield item
```

- [ ] **Step 6: `agent_node` 暂停分支＋文案＋State**

`app/workflow/agent_node.py` 在 `result = await run_react(...)` 之后、`actions = [...]` 之前插入（`TICKET_CONFIRM_TEXT` 从 `app.prompts` 导入）：
```python
        if result.pending_write:
            # 图马上在 confirm_ticket 挂起，log 节点本轮跑不到：这一轮的正文由这里落库。
            text = result.reply or TICKET_CONFIRM_TEXT
            await asyncio.to_thread(deps.store.add_message, conversation_id, "assistant", content=text)
            if not result.reply:
                emit("token", {"content": text})
            logger.info("workflow node=agent conv=%s paused=confirm_write tool=%s",
                        conversation_id, result.pending_write["tool"])
            return {"reply": text, "actions": [], "offer": None,
                    "finish_reason": result.finish_reason, "steps": result.steps,
                    "tool_calls": result.tool_calls, "tokens": result.tokens,
                    "stopped_by": result.stopped_by, "pending_write": result.pending_write}
```
`app/prompts.py` 追加（`AGENT_ADDENDUM` 补第 4 条，逐字照抄）：
```python
TICKET_CONFIRM_TEXT = "我把工单信息整理好了，请在下面的卡片里确认：点「确认提交」才会真正创建，点「取消」就不动了。"
TICKET_CREATED_TEXT = "工单 {ticket_no} 已提交，当前状态：待处理，我们会尽快跟进。"
TICKET_CANCELLED_TEXT = "好，这张工单我没有提交，什么都没改。你需要时随时再让我建。"
TICKET_FAILED_TEXT = "工单没能提交成功，你可以再确认一次，或者直接转人工客服。"
```
```
4. 只有用户明确要求建工单时才调用 create_ticket；description 必须由用户已经说过的内容组成，
   信息不够就先追问「具体遇到什么问题、想怎么处理」，不许编造；ticket_type 只能填 售后、投诉、咨询。
   调用后系统会先把工单预览给用户确认，你不用重复罗列工单内容。
```
`app/workflow/state.py` 加 `write_decision: str`（`pending_write` 已由 T10 加好）。

- [ ] **Step 7: 跑到绿** — 同 Step 2 命令；Expected: 8 passed。

- [ ] **Step 8: 全量回归** — `LANGSMITH_TRACING=false .venv/Scripts/python.exe -X utf8 -m pytest -q` → 全绿。既有用例不许靠改期望值蒙过去：`respond` 对外仍然只吐 `(event, payload)`，帧序不变。

- [ ] **Step 9: 本地提交** `git commit -am "feat(ch08/T13): 写操作确认流的图与节点（interrupt 挂起／两分支）"`

**实现期修正（T13 完成后回填，2026-10-31）**
- Step 1 的 `assistant_rows` 增加 `if row.content is not None`：工具轮那条 assistant 行只带 `tool_calls`、正文是 NULL，不过滤会永远数出两条。
- Step 1 的卡片 label 断言改为「创建工单」（与 Step 4 的 `TOOL_LABELS` 对齐，同一个工具只保留一个中文名）。
- Step 1 新增两条用例（共 10 条）：`test_a_new_question_after_the_card_does_not_resurrect_it`、`test_a_turn_that_never_reaches_the_agent_also_clears_the_card`；后者钉住 coref 复位（投诉／闲聊不经过 agent，只靠 agent 回写会漏）。
- Step 7 期望数 8 → 10；全量 **430 passed, 3 skipped**。
- decision 值以本节为准取 `"accept"`／`"cancel"`（规格 §12.7 的 `"confirm"` 不再使用，T14 照此写）。

**独立评审后回改（2026-10-31，报告 `.superpowers/sdd/2026-10-30-ch08-tool-system/review-T13-report.md`）**
- I-1：`_ticket_no` 加形状守卫（非 dict / `data` 非 dict 一律 `None`），配一条形状矩阵单测——MCP 写工具的返回形状不由我们定。
- I-2：`Workflow.resume` 先读 checkpoint，没有挂起的 interrupt 就空操作。原报告 §「重复 resume 返回空序列」只对完成态 thread 成立，空 thread 会被 LangGraph 当新输入从 `START` 起图、`coref` 直接 `KeyError: 'question'`。
- I-3：`TICKET_FAILED_TEXT` 的「再确认一次」改成「再让我建一次」（失败时卡片已作废，第二次 resume 实测是空操作）；补 2 条失败分支用例，变异 M10（`stopped_by` 恒 `None`）从全绿变 1 红。
- I-4：**不补第二条 tool 行，改的是规格**。`storage._history_filter` 只回放 user/assistant 正文，模型从来读不到 tool 行；工单号已经由 assistant 正文进历史。补一条同 `tool_call_id` 的行反而把 `assistant(tool_calls)→tool` 的 1:1 变成 1:2。规格 §12.5 已按此改写，用例 `test_the_executed_result_does_not_add_a_second_tool_row` 钉住。
- Mi-1／Mi-2／Mi-3／Mi-5 已修（卡片 label 单源／`record_denial` 进 `to_thread`／认不出挂起形状不发空白卡片／coref 同时复位 `write_decision`）；Mi-4 测试改成按帧名取卡片；Mi-6（两处 `_mcp_specs`）留到 T18。
- 规格 §12.2／§12.3／§12.4／§12.7／§12.8 的文本与实现相反或过时，本次一并回改（报告建议本章收尾做，提前到本轮）。
- 用例 10 → 16。

---

### Task 14: `POST /api/chat/resume`——确认接口与会话锁复用

需求 7 的接口半段：把卡片上的「确认提交／取消」变成一次带 nonce 的 resume，并且与 `/api/chat` 共用同一把会话锁。规格 §11。

**我替你定的**：nonce 不符**不发 409**，而是在锁内发一帧 `error`（`code="no_pending_confirm"`）。理由：查 `pending_write` 必须和 `Command(resume=...)` 在同一把锁里，否则「检查通过 → 别人插一轮 → resume 打到新卡片」的窗口关不掉；而前端 `readStream` 对 `error` 帧和 409 JSON 的处理路径完全一样（都走 `readableError`），用户看到的话术不变。上一轮设计笔记里写的 409 就此作废，`/api/tickets` 的 409 保持原样（那条路不涉锁内状态）。

**Files:**
- Modify: `app/schemas.py`（`ResumeRequest`）、`app/service.py:89-176`（抽出 `_stream`，新增 `respond_resume`）、`app/main.py`（`/api/chat/resume`＋`encode_events` 复用）
- Test: `tests/test_chat_resume.py`

**Interfaces:**
- Consumes: `Workflow.resume`／`Workflow.lock`／`Workflow.get_state`（T13）、`store.owner`
- Produces:
  ```python
  # app/schemas.py
  class ResumeRequest(BaseModel):   # conversation_id / client_id / decision / pending_id
  # app/service.py
  async def CustomerService._stream(self, conversation_id: int, stream) -> AsyncIterator[tuple[str, dict]]
  async def CustomerService.respond_resume(self, request: ResumeRequest) -> AsyncIterator[tuple[str, dict]]
  ```

- [ ] **Step 1: 写失败测试**

```python
# tests/test_chat_resume.py
from contextlib import contextmanager

from sqlalchemy import func, select

from app.models import Ticket, ToolAuditLog
from app.prompts import TICKET_CANCELLED_TEXT, TICKET_CREATED_TEXT
from tests.support import api_with_db, events, streamed_tool_call

TICKET_CALL = {"description": "保温杯收到就是漏的，想退货", "ticket_type": "售后"}
CLIENT = "anon-resume-1"


@contextmanager
def confirm_card(handler, client_id=CLIENT):
    """跑一轮到挂起，交出 (client, factory, 会话号, 卡片)。会话号取 meta 帧，不给 confirm 加字段。"""
    with api_with_db(handler) as (client, seen, factory):
        frames = events(client.post("/api/chat", json={"message": "帮我建个工单，杯子是漏的",
                                                       "client_id": client_id}))
        assert [name for name, _ in frames][-2:] == ["confirm", "done"]
        assert len(seen) == 1                                  # 暂停轮没有第二次模型调用
        card = next(payload for name, payload in frames if name == "confirm")
        yield client, factory, frames[0][1]["conversation_id"], card


def test_accept_creates_exactly_one_ticket_and_streams_the_reply():
    def handler(request):
        return streamed_tool_call("create_ticket", TICKET_CALL)

    with confirm_card(handler) as (client, factory, conversation_id, card):
        frames = events(client.post("/api/chat/resume", json={
            "conversation_id": conversation_id, "client_id": CLIENT,
            "decision": "accept", "pending_id": card["pending_id"]}))
        assert [name for name, _ in frames] == ["status", "status", "token", "done"]
        with factory() as session:
            tickets = list(session.scalars(select(Ticket)))
            audit = list(session.scalars(select(ToolAuditLog)))
        assert len(tickets) == 1
        assert frames[2][1]["content"] == TICKET_CREATED_TEXT.format(ticket_no=tickets[0].ticket_no)
        assert (audit[0].status, audit[0].tool_name) == ("成功", "create_ticket")


def test_cancel_leaves_no_ticket_and_audits_a_denial():
    """验收⑤的接口形状：点取消 → 工单没建、审计状态「权限拒绝」。"""
    def handler(request):
        return streamed_tool_call("create_ticket", TICKET_CALL)

    with confirm_card(handler) as (client, factory, conversation_id, card):
        frames = events(client.post("/api/chat/resume", json={
            "conversation_id": conversation_id, "client_id": CLIENT,
            "decision": "cancel", "pending_id": card["pending_id"]}))
        assert [name for name, _ in frames] == ["token", "done"]
        assert frames[0][1]["content"] == TICKET_CANCELLED_TEXT
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(Ticket)) == 0
            audit = list(session.scalars(select(ToolAuditLog)))
        assert (audit[0].status, audit[0].retry_count) == ("权限拒绝", 0)


def test_a_stale_pending_id_is_refused_without_touching_the_graph():
    def handler(request):
        return streamed_tool_call("create_ticket", TICKET_CALL)

    with confirm_card(handler) as (client, factory, conversation_id, _):
        frames = events(client.post("/api/chat/resume", json={
            "conversation_id": conversation_id, "client_id": CLIENT,
            "decision": "accept", "pending_id": "deadbeefdeadbeef"}))
        assert [name for name, _ in frames] == ["error"]
        assert frames[0][1]["code"] == "no_pending_confirm"
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(Ticket)) == 0
            assert list(session.scalars(select(ToolAuditLog))) == []


def test_resuming_the_same_card_twice_creates_one_ticket():
    def handler(request):
        return streamed_tool_call("create_ticket", TICKET_CALL)

    with confirm_card(handler) as (client, factory, conversation_id, card):
        payload = {"conversation_id": conversation_id, "client_id": CLIENT,
                   "decision": "accept", "pending_id": card["pending_id"]}
        events(client.post("/api/chat/resume", json=payload))
        second = events(client.post("/api/chat/resume", json=payload))
        assert [name for name, _ in second] == ["error"]
        assert second[0][1]["code"] == "no_pending_confirm"
        with factory() as session:
            assert len(list(session.scalars(select(Ticket)))) == 1


def test_another_user_cannot_resume_my_card():
    def handler(request):
        return streamed_tool_call("create_ticket", TICKET_CALL)

    with confirm_card(handler, "anon-resume-5") as (client, _, conversation_id, card):
        response = client.post("/api/chat/resume", json={
            "conversation_id": conversation_id, "client_id": "someone-else-1",
            "decision": "accept", "pending_id": card["pending_id"]})
        assert response.status_code == 403
        assert response.json()["code"] == "forbidden"


def test_decision_and_pending_id_are_validated():
    with api_with_db(lambda request: streamed_tool_call("create_ticket", TICKET_CALL)) as (client, seen, factory):
        bad = client.post("/api/chat/resume", json={"conversation_id": 1, "client_id": "anon-resume-6",
                                                    "decision": "maybe", "pending_id": "aaaaaaaa"})
        short = client.post("/api/chat/resume", json={"conversation_id": 1, "client_id": "anon-resume-6",
                                                      "decision": "accept", "pending_id": "abc"})
        assert bad.status_code == short.status_code == 422
        assert bad.json()["code"] == "invalid_request"
        assert seen == []
```
`_stream` 的 `except` 元组里没有 `ModelFailure`，所以 `respond_resume` 用 `yield "error"` 而不是抛异常（详见 Step 4 的注）。这六条用例全部走真实 SSE 帧序列，不 mock 图。

- [ ] **Step 2: 跑到红** — Run: `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_chat_resume.py -q`；Expected: 404（路由不存在）或 `AttributeError: 'CustomerService' object has no attribute 'respond_resume'`。

- [ ] **Step 3: `ResumeRequest`**

```python
class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    conversation_id: Annotated[int, Field(ge=1)]
    client_id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"[A-Za-z0-9_-]+")]
    decision: Literal["accept", "cancel"]
    pending_id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"[a-f0-9]+")]
```
`app/main.py:35` 的 import 加上 `ResumeRequest`。

- [ ] **Step 4: `service.py` 抽出 `_stream`**

把 `respond()` 里 `lock = self.workflow.lock(...)` 到 `finally` 那一整段（含 feeder／queue／except／finally）原样搬进新方法，只改两点：入参变成 `(conversation_id, stream)`，摘要调度**留**在 `respond` 里：
```python
    async def _stream(self, conversation_id: int, stream) -> AsyncIterator[tuple[str, dict]]:
        """同一会话串行；节点跑在 LangGraph 自己的任务上，所以消费放进独立任务，
        断连时只对它做一次干净 Task.cancel()（ch07 的教训，注释随代码一起搬）。"""
        ... 原样：lock／pump／feeder／while True／except (...)／finally ...
```
`respond()` 尾段改成：
```python
        stream = self.workflow.respond(conversation_id, request.message, messages,
                                       context_plan=plan.as_dict() if plan else None,
                                       current_message_id=current_id)
        failed = False
        async for event, payload in self._stream(conversation_id, stream):
            if event == "error":
                failed = True
            yield event, payload
        if not failed and plan is not None and plan.layer2_over and self.summary is not None:
            logger.info("summary trigger conv=%s layer2_raw_tokens=%s layer2_budget=%s from=%s upto=%s",
                        conversation_id, plan.layer2_raw_tokens, plan.layer2_budget,
                        plan.summary_upto, plan.layer1_from)
            self.summary.schedule(conversation_id, from_msg_id=plan.summary_upto,
                                  upto_msg_id=plan.layer1_from)
```
新增 `respond_resume`——查卡片与 resume 必须在同一把锁里（见本任务开头的决定）：
```python
    async def respond_resume(self, request) -> AsyncIterator[tuple[str, dict]]:
        if self.workflow is None:
            yield "error", ModelFailure("database_unavailable", DB_FAILURE_MESSAGE, 503).payload()
            return

        async def drive():
            try:
                snapshot = await self.workflow.get_state(request.conversation_id)
            except (SQLAlchemyError, sqlite3.Error):
                yield "error", ModelFailure("database_unavailable", DB_FAILURE_MESSAGE, 503).payload()
                return
            pending = (snapshot.values or {}).get("pending_write") or {}
            if pending.get("pending_id") != request.pending_id:
                yield "error", ModelFailure("no_pending_confirm", "这张确认卡片已经失效了，请重新发起。",
                                            409).payload()
                return
            async for item in self.workflow.resume(request.conversation_id, request.decision):
                yield item

        async for item in self._stream(request.conversation_id, drive()):
            yield item
```
`drive()` 自己 `yield "error"` 而不是抛 `ModelFailure`：`_stream` 的 `except` 元组里没有 `ModelFailure`，抛出去会变成 500；错误帧才是这条流的用户语言。

- [ ] **Step 5: 接口**

`app/main.py` 在 `/api/chat` 之后加（`encode_events` 提出来当模块级小函数复用，或直接重复三行——按现有风格重复即可，别为两处调用做抽象）：
```python
    @application.post("/api/chat/resume")
    async def resume_chat(body: ResumeRequest, request: Request):
        service = request.app.state.service
        try:
            owner = await asyncio.to_thread(service.store.owner, body.conversation_id)
        except (SQLAlchemyError, AttributeError):
            raise ModelFailure("database_unavailable", DB_FAILURE_MESSAGE, 503) from None
        if owner != body.client_id:
            raise ModelFailure("forbidden", "无权操作该会话。", 403)

        async def encode_events():
            stream = service.respond_resume(body)
            try:
                async for event, payload in stream:
                    yield f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            finally:
                await stream.aclose()

        return StreamingResponse(encode_events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
```

- [ ] **Step 6: 跑到绿** — 同 Step 2 命令；Expected: 6 passed。
- [ ] **Step 7: 全量回归＋本地提交** `LANGSMITH_TRACING=false .venv/Scripts/python.exe -X utf8 -m pytest -q` → 全绿（`tests/test_api.py` 第 5 章按钮那两条必须还是绿的）；`git commit -am "feat(ch08/T14): /api/chat/resume 确认接口与会话锁复用"`

**实现期修正（T14 完成后回填，2026-10-31）**
- Step 1 的 `confirm_card` 夹具多交出一个 `seen`（上游调用记录），否则「resume 不再叫模型」这句断言没有着力点；用例从 6 条增到 **7 条**：新增 `test_resume_is_serialised_behind_the_conversation_lock`，用 `client.portal.call`（`tests/test_refunds.py` 已有先例）在应用循环里先持有会话锁，再起 resume 任务，等 50ms 断言它没跑完——「resume 绕过 `_stream` 直接跑 `drive()`」实测变红。
- Step 4 落地时把注释拆两处：断连／独立任务那三行说明跟着 `_stream` 走，摘要触发条件留在 `respond`（`_stream` 也被 resume 复用，搬进去会让「确认提交」这轮也去压摘要）。
- Step 6 期望数 6 → 7；全量 **437 passed, 3 skipped**。
- 已知既有测试缺口（不是本次引入）：「异常轮不压摘要」的反面条件从 ch07 起就没有测试盯（把 `if not failed` 去掉全绿），转 T18 决定是否补。

---

### Task 15: 前端工单预览卡片（Vibe Coding，不套 TDD 与评审）

需求 7 的前端半段：卡片带「确认提交／取消」，点下去打 `/api/chat/resume`，回复接着流进新气泡。按工作要求第 1 条，这一节走 Vibe Coding：**不写失败测试、不派评审子代理**，验收＝`node --check` 过语法＋真浏览器把四个场景点一遍。规格 §12。

**Files:**
- Modify: `app/web/chat.js`（`readableError:197-208`、`readStream:559-622`、`sendQuestion:644-739`、新增 `addConfirmCard`／`resumeConfirm`）
- Modify: `app/web/chat.css`（新增 `.confirm-*` 一组；移动端那条 `max-width: none` 带上 `.confirm-card`）

**Interfaces:**
- Consumes: `confirm` 帧 `{pending_id,tool,tool_call_id,label,turn,question,fields:[{label,value}],note}`（T13 定义、T14 测试钉住）、`POST /api/chat/resume`（T14）
- Produces: 无（前端是这条链路的终点，后端不读它的任何返回）

- [ ] **Step 1: `readStream` 认下 `confirm` 帧**

放在 `orders` 分支之后、`token` 分支之前（`chat.js:604-606`）。`pending_id` 是 resume 的必要参数，缺了就不认这张卡：

```js
        if (event === "confirm") {
          if (typeof payload?.pending_id === "string" && Array.isArray(payload?.fields)) handlers.onConfirm(payload);
        }
```

- [ ] **Step 2: 卡片渲染**

紧跟 `addRefundForm`（`chat.js:470-520`）之后新增。字段值全部来自模型给的参数，属于外部输入——**只走 `textContent`，一次 `innerHTML` 都不许出现**：

```js
function addConfirmCard(view, card) {
  const box = document.createElement("div");
  box.className = "confirm-card";
  box.setAttribute("role", "group");
  box.setAttribute("aria-label", "写操作确认");
  const title = document.createElement("p");
  title.className = "confirm-title";
  title.textContent = "确认" + (card.label || "这次操作") + "？";
  const note = document.createElement("p");
  note.className = "confirm-note";
  note.textContent = card.note || "点「确认提交」才会真正执行。";
  const list = document.createElement("dl");
  list.className = "confirm-fields";
  for (const field of card.fields) {
    const dt = document.createElement("dt");
    dt.textContent = field.label;
    const dd = document.createElement("dd");
    dd.textContent = field.value;
    list.append(dt, dd);
  }
  const row = document.createElement("div");
  row.className = "confirm-actions";
  const submit = document.createElement("button");
  submit.type = "button";
  submit.className = "confirm-submit";
  submit.textContent = "确认提交";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "confirm-cancel";
  cancel.textContent = "取消";
  const status = document.createElement("p");
  status.className = "confirm-status";
  row.append(submit, cancel);
  box.append(title, note, list, row, status);
  const click = decision => {
    if (activeTurn) return;
    submit.disabled = cancel.disabled = true;
    status.textContent = decision === "accept" ? "正在提交……" : "正在取消……";
    resumeConfirm(card, decision, status);
  };
  submit.addEventListener("click", () => click("accept"));
  cancel.addEventListener("click", () => click("cancel"));
  view.stack.append(box);
  scrollToLatest();
}
```

- [ ] **Step 3: 恢复流**

紧跟 `addConfirmCard` 之后新增。**不复用 `sendQuestion`**：那个函数绑死了「用户刚说了一句话」的语义（回显气泡、`welcome`、90 s 超时里重填原话），而这一轮没有新的用户输入；帧解析仍然复用 `readStream`，所以协议只有一处实现。`showFeedback` 的「重新提问」按钮在这里正好回填卡片带来的原话（`card.question`，T13 就把这句放进了预览）。

```js
async function resumeConfirm(card, decision, status) {
  const turn = { controller: new AbortController(), stopped: false, timedOut: false,
    turnId: conversationId + "-resume-" + Date.now(), answer: "" };
  activeTurn = turn;
  setBusy(true);
  const view = messageElement("assistant");
  view.article.classList.add("is-writing");
  const writer = typewriter(view.textNode);
  turn.writer = writer;                     // 「停止」按钮会摸 activeTurn.writer，不给就 TypeError
  const timeout = window.setTimeout(() => { turn.timedOut = true; turn.controller.abort(); }, 90000);
  announcer.textContent = decision === "accept" ? "正在提交工单。" : "正在取消这次操作。";
  try {
    const response = await fetch("/api/chat/resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conversation_id: conversationId, client_id: clientId(),
                             decision, pending_id: card.pending_id }),
      signal: turn.controller.signal,
    });
    const finishReason = await readStream(response, {
      onStatus: payload => (payload.phase === "start" ? beginTool(view, payload) : endTool(view, payload)),
      onText: text => { turn.answer += text; writer.push(text); },
    });
    window.clearTimeout(timeout);
    await writer.drain();
    if (!turn.answer.trim()) throw new Error("这次没有收到正文，请重新发起。");
    if (finishReason === "length") throw new Error("回复因长度限制而中断，请缩短问题后重新提问。");
    renderAnswer(view, turn.answer, []);
    addAnswerFeedback(view, turn.turnId);
    status.textContent = decision === "accept" ? "已提交。" : "已取消，什么都没动。";
    announcer.textContent = decision === "accept" ? "工单已提交。" : "这次操作已取消。";
    void loadConversations();
  } catch (error) {
    writer.flush();
    interruptTools(view);
    if (!turn.answer) view.textNode.data = turn.stopped ? "已停止。" : "这一步暂时没走完。";
    status.textContent = turn.timedOut ? "等候回应超时，可以重新点一次。" : "这一步没走完，可以重新发起或直接说需求。";
    showFeedback(view, turn.timedOut ? "等候回复超时了，请重新发起。" : error.message, card.question || "", turn.stopped);
  } finally {
    window.clearTimeout(timeout);
    view.article.classList.remove("is-writing");
    activeTurn = null;
    setBusy(false);
    scrollToLatest();
  }
}
```

`readStream` 的 `error` 帧会 `throw`，所以 nonce 失效（T14 的 `no_pending_confirm`）在这里落到 `catch`：卡片上显示那句「这一步没走完」，气泡里显示 `readableError` 的话术（Step 4 补映射）。

- [ ] **Step 4: 接进 `sendQuestion`、失效选择器、错误文案**

四处小改，逐条对照：

```js
// (1) turn 初始化加一个字段（chat.js:647-649 那个对象字面量里）
    refund: null, orderItems: [], confirm: null };

// (2) readStream 的 handlers 里，onOrders 之后（chat.js:702-705）
      onConfirm: payload => { waiting.remove(); turn.confirm = payload; },

// (3) 成功路径按「正文 → 订单卡 → 按钮 → 确认卡 → 反馈」的顺序挂（chat.js:720-722）
    if (turn.actionItems.length) addActionButtons(view, turn.actionItems, turn.actionTurn, turn.refund);
    if (turn.confirm) addConfirmCard(view, turn.confirm);
    addAnswerFeedback(view, turn.turnId);

// (4) 下一轮开始时把旧卡片一起锁掉（chat.js:656 的选择器加一段）
  for (const stale of messages.querySelectorAll(
      ".action-button:not(:disabled), .order-card:not(:disabled), .refund-form :is(select, input, button), .confirm-card button")) {
    stale.disabled = true;
  }
```

`readableError` 的表里加一行（`chat.js:198-206`），否则 `no_pending_confirm` 会掉进最后一句通用话术，用户不知道发生了什么：

```js
    no_pending_confirm: "这张确认卡片已经失效了，请重新说一次你的需求。",
```

- [ ] **Step 5: 样式**

`chat.css` 追加，全部用现有 token，不引新色（`.refund-form` 是同款容器，尺寸直接对齐它）：

```css
.confirm-card { display: flex; flex-direction: column; gap: 8px; width: 100%; max-width: 420px; margin-top: 10px; padding: 13px 14px; border: 1px solid #c3d5c6; border-left: 3px solid var(--green); border-radius: 11px; background: var(--surface); }
.confirm-title { margin: 0; color: var(--green); font-size: 13px; font-weight: 600; }
.confirm-note { margin: 0; color: var(--muted); font-size: 12px; line-height: 1.6; }
.confirm-fields { display: grid; grid-template-columns: 72px minmax(0, 1fr); gap: 4px 12px; margin: 2px 0; }
.confirm-fields dt { color: var(--muted); font-size: 12px; }
.confirm-fields dd { margin: 0; color: var(--ink); font-size: 12px; overflow-wrap: anywhere; }
.confirm-actions { display: flex; gap: 8px; }
.confirm-submit { min-height: 36px; padding: 7px 16px; border: 1px solid var(--green); border-radius: 9px; background: var(--green); color: #fff; font-size: 13px; transition: background .18s, opacity .18s; }
.confirm-submit:hover:not(:disabled) { background: #1f5540; }
.confirm-cancel { min-height: 36px; padding: 7px 16px; border: 1px solid var(--line); border-radius: 9px; background: var(--surface); color: var(--muted); font-size: 13px; transition: background .18s, color .18s; }
.confirm-cancel:hover:not(:disabled) { background: #f2f7f2; color: var(--ink); }
.confirm-submit:disabled, .confirm-cancel:disabled { opacity: .5; }
.confirm-status { margin: 0; color: var(--green); font-size: 12px; min-height: 18px; }
```

移动端那条 `.order-card, .refund-form { max-width: none; }`（`chat.css` 媒体查询内）改成 `.order-card, .refund-form, .confirm-card { max-width: none; }`。

- [ ] **Step 6: 语法检查**

Run: `node --check app/web/chat.js`
Expected: 无输出，退出码 0。（`chat.css` 没语法检查工具，靠下一步肉眼与渲染确认。）

- [ ] **Step 7: 起服务，浏览器实测五个场景**

先按 README 起客服服务与两个 MCP Server（`start-mcp.bat`），打开聊天页。**实测用 headless Chrome＋CDP，不用 `mcp__browser-use__*`**：browser-use 的页签在后台会被 Chrome 节流定时器，打字机每 22ms 吐一批字符、正文收敛极慢，卡片永远等不到（同一份脚本在 headless 下实测 `visibilityState=visible`、120ms 定时器误差 4ms）。脚本 `.superpowers/sdd/…/t15-browser.mjs`，DOM 快照打屏＋截图存 `shots/`。**五个场景都要留证据**，结论写进 `dev-notes/ch08.md` 阶段 28：

1. **确认路径**：输入「订单 1002 的快递从 21 号起就没更新，帮我建个工单催一下派送」→ 期望气泡里出现预览卡片，字段列出「问题描述／工单类型／所属会话」→ 点「确认提交」→ 新气泡出现「工单 T2026… 已提交」（工单号前缀是 `T`，不是 TK），卡片右下变成「已提交。」，两个按钮变灰。
   ⚠ 需求原文的「杯子收到就是漏的，帮我建工单要退货」**测不到这条链路**：退货／退款被 ch06 路由判给 `policy`（强制检索＋固定话术），根本不进 agent 节点，也就永远不会出卡片。确认流只在 agent 路由（物流／订单／其他）可达——这是产品事实，T16 评估集与 T17 演示的文案都要按此写。
2. **取消路径**：换个说法再要一次工单（同一会话里措辞要自包含，「还是那笔 1002」在新会话里没有先行词）→ 点「取消」→ 新气泡是「好，这张工单我没有提交，什么都没改…」，卡片右下「已取消，什么都没动。」，页面上不出现新工单号（第 5 章投诉按钮建的单不受影响）。
3. **旧卡片失效**：挂起时另发一句**不需要写操作**的话（例如「帮我看看订单 1001 的物流到哪了」）。让旧卡片作废的不是"挂出新卡片"，而是 `coref` 每轮复位 `pending_write` ⇒ 服务端 nonce 已空。新轮次一结束，页面上所有卡片的按钮都会被置灰（这就是失效的自然表现，用户点不动）；要验 `no_pending_confirm` 那条话术，得**用脚本把灰掉的按钮解锁再点**，模拟"另一个标签页里留着的老卡片"→ 卡片显示「这一步没走完…」，气泡显示「这张确认卡片已经失效了，请重新说一次你的需求。」，不产生工单。
4. **键盘可达与窄屏**：Tab 能从「确认提交」走到「取消」且有 `:focus-visible` 描边（复用全局那条 3px `#79a78c` / offset 4px）；把视口设成 380 px 宽，卡片不溢出、字段值长时换行（`overflow-wrap: anywhere` 生效）、两个按钮仍 ≥36 px 高。
5. **徽章三态**（计划原文漏了，实测才发现）：确认暂停时那一格的徽章**不能**显示「创建工单失败」——用户会以为出错。要求 `end` 状态帧带 `code`，前端按 `ok`／`code=="confirm_required"`／其余三分：「已创建工单」／「创建工单待确认」／「创建工单失败」。

- [ ] **Step 7b: 顺带修掉实测暴露的后端缺陷**

真机启动（`uvicorn app.main:app`，不带注入的 `session_factory`）时 `app/main.py` 直接崩：`build_engine() missing 2 required keyword-only arguments`。根因是 `:22` 从 `app.database` 导入的 `build_engine` 被 `:38` 从 `app.tools.wiring` 导入的同名函数盖掉，而 T9 之后 wiring 版多了两个必填关键字参数。这条分支**所有测试都覆盖不到**（夹具一律注入 `session_factory`），所以 400 多条全绿也照样起不来。修法：wiring 版在 `main.py` 里改名 `build_tool_engine`，并补一条钉住两个名字互不覆盖的测试。

- [ ] **Step 8: 提交**

```bash
git add app/web/chat.js app/web/chat.css app/main.py app/agent/react.py \
        tests/test_tool_wiring.py tests/test_workflow_confirm.py
git commit -m "feat(ch08/T15): 工单预览卡片与 resume 前端接续（Vibe Coding）"
```

---

### Task 16: 「该追问就追问、说清了才出卡片」评估集

需求 7 前半段（缺必填先追问、不许瞎编）只能靠提示词约束，按工作要求第 1 条用**标注集＋真实模型**替代单测。两个指标照验收④的口径：`ask_rate` 门槛 **1.000**（该追问的一条都不许越过卡片线），`invoke_rate` 门槛 **≥0.90**（说清了的例子里真推出卡片的比例）。规格 §13。

**Files:**
- Create: `evals/ticket_confirm_cases.jsonl`、`scripts/evaluate_ticket_confirm.py`

**Interfaces:**
- Consumes: `Workflow`／`WorkflowDeps`（T13 后的形状）、`Settings.from_env()`、`build_model`、`ConversationStore.start_turn`、`confirm` 帧的 `fields`（T13）
- Produces: `evals/reports/ch08-ticket-confirm-<时间戳>.md`；未达标以 `SystemExit(1)` 退出

**我替你定的**：意图分类用 `FixedClassifier("其他")` 钉死（route=agent），这一集**只评 Agent 的发起／追问行为**，不掺路由的抖动——路由本身已有 `tests/test_intent.py` 和第 6 章的意图评估兜着。脚本里自带 8 行的桩分类器，不 import `tests/`：评估脚本是交付物，不该依赖测试包。

- [ ] **Step 1: 写标注集**

`evals/ticket_confirm_cases.jsonl`，12 行（6 条 `ask` ＋ 6 条 `invoke`），逐字照抄：

```
{"id":"ask-01","kind":"ask","message":"帮我建个工单","must_any":["什么","哪方面","具体","描述","情况","怎么"]}
{"id":"ask-02","kind":"ask","message":"我要投诉，帮我建工单","must_any":["什么","哪方面","具体","描述","情况","怎么"]}
{"id":"ask-03","kind":"ask","message":"建工单","must_any":["什么","哪方面","具体","描述","情况","怎么"]}
{"id":"ask-04","kind":"ask","message":"帮我建工单，你随便写点什么都行","must_any":["什么","哪方面","具体","描述","情况","怎么"]}
{"id":"ask-05","kind":"ask","message":"给我建个工单吧","must_any":["什么","哪方面","具体","描述","情况","怎么"]}
{"id":"ask-06","kind":"ask","message":"我要建工单，类型和内容你看着填","must_any":["什么","哪方面","具体","描述","情况","怎么"]}
{"id":"invoke-01","kind":"invoke","message":"杯子收到就是漏的，帮我建工单要退货","expect_any":["漏","退货"]}
{"id":"invoke-02","kind":"invoke","message":"订单 1001 的包裹三天没动了，帮我建工单催一下物流","expect_any":["物流","1001","三天","没动"]}
{"id":"invoke-03","kind":"invoke","message":"我上周买的保温杯盖子裂了，帮我建工单，想换一个新的","expect_any":["盖子","裂","换"]}
{"id":"invoke-04","kind":"invoke","message":"订单 1002 显示签收但我没收到货，帮我建工单投诉这件事","expect_any":["1002","签收","没收到"]}
{"id":"invoke-05","kind":"invoke","message":"帮我建工单问一下这个保温杯能不能进洗碗机，说明书里没写","expect_any":["洗碗机"]}
{"id":"invoke-06","kind":"invoke","message":"买的养生壶第二次通电就跳闸，帮我建工单，要求退款","expect_any":["跳闸","退款","养生壶"]}
```

- [ ] **Step 2: 写评估脚本**

`scripts/evaluate_ticket_confirm.py`，报告结构、门槛不达标就 `SystemExit(1)`、收尾关上游连接三条都照 `scripts/evaluate_summary.py` 的约定：

```python
"""真模型跑「说清了才发起写操作、信息不足必须先追问」标注集，防止卡片拿到编出来的参数。"""

import asyncio
import json
import tempfile
from datetime import datetime
from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, func, select

from app.config import Settings
from app.database import build_session_factory
from app.model import build_model
from app.models import Base, Ticket
from app.schemas import IntentResult
from app.storage import ConversationStore
from app.workflow.graph import Workflow, WorkflowDeps

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals/ticket_confirm_cases.jsonl"
REPORT_DIR = ROOT / "evals/reports"
GATE_ASK = 1.0
GATE_INVOKE = 0.90
TYPES = {"售后", "投诉", "咨询"}


class FixedClassifier:
    """路由不是本集的评估对象：钉成「其他」，直连主 Agent。"""

    async def classify(self, *args, **kwargs):
        return IntentResult(intent="其他", confidence=1.0)


def card_fields(card):
    return {item["label"]: item["value"] for item in card.get("fields", [])}


def judge(case, text, card):
    if case["kind"] == "ask":
        if card is not None:
            return False, f"信息不足却推了卡片，描述＝{card_fields(card).get('问题描述', '')!r}"
        if not any(mark in text for mark in ("？", "?")):
            return False, f"没有发问，正文＝{text[:60]!r}"
        if not any(word in text for word in case["must_any"]):
            return False, f"问句没问到点上，缺追问词 {case['must_any']}：{text[:60]!r}"
        return True, "先追问，没编参数"
    if card is None:
        return False, f"信息齐全却没出卡片，正文＝{text[:60]!r}"
    fields = card_fields(card)
    if fields.get("工单类型") not in TYPES:
        return False, f"工单类型不合法：{fields.get('工单类型')!r}"
    description = fields.get("问题描述", "")
    if not any(word in description for word in case["expect_any"]):
        return False, f"问题描述没带出关键事实 {case['expect_any']}：{description!r}"
    return True, f"卡片已出（{fields['工单类型']}｜{description[:28]}）"


async def run_case(settings, model, store, case, index):
    conversation_id = store.start_turn(user_id=f"eval-ticket-{index}", message=case["message"])
    workflow = Workflow(deps=WorkflowDeps(model=model, settings=settings, store=store,
                                          classifier=FixedClassifier()),
                        checkpointer=InMemorySaver())
    frames = []
    async for item in workflow.respond(conversation_id, case["message"],
                                       [HumanMessage(content=case["message"])]):
        frames.append(item)
    text = "".join(payload["content"] for event, payload in frames if event == "token")
    card = next((payload for event, payload in frames if event == "confirm"), None)
    return text, card


async def main() -> None:
    settings = Settings.from_env()
    model = build_model(settings)
    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"模型：{settings.openai_model}；样本：{len(cases)}")

    with tempfile.TemporaryDirectory() as workdir:
        engine = create_engine(f"sqlite:///{Path(workdir) / 'eval.db'}")
        Base.metadata.create_all(engine)
        store = ConversationStore(build_session_factory(engine))
        rows = []
        for index, case in enumerate(cases, start=1):
            text, card = await run_case(settings, model, store, case, index)
            ok, reason = judge(case, text, card)
            rows.append((case, ok, reason))
            print(f"- {'OK ' if ok else 'ERR'} `{case['id']}` {case['message']}｜{reason}")
        with store.session_factory() as session:
            tickets = session.scalar(select(func.count()).select_from(Ticket))
        engine.dispose()

    ask = [row for row in rows if row[0]["kind"] == "ask"]
    invoke = [row for row in rows if row[0]["kind"] == "invoke"]
    ask_rate = sum(1 for row in ask if row[1]) / len(ask) if ask else 0.0
    invoke_rate = sum(1 for row in invoke if row[1]) / len(invoke) if invoke else 0.0
    passed = ask_rate >= GATE_ASK and invoke_rate >= GATE_INVOKE and tickets == 0

    lines = [
        "# ch08 建工单确认流评估",
        "",
        f"- 时间：{datetime.now():%Y-%m-%d %H:%M}",
        f"- 模型：{settings.openai_model}",
        f"- 样本：{len(rows)}（ask {len(ask)}／invoke {len(invoke)}）",
        f"- 该追问追问率：{ask_rate:.3f}（门槛 {GATE_ASK:.3f}）",
        f"- 说清了直接出卡片率：{invoke_rate:.3f}（门槛 {GATE_INVOKE:.2f}）",
        f"- 评估过程中误落的工单数：{tickets}（必须为 0：卡片只预览，不提交）",
        f"- 结论：{'通过' if passed else '未达标，改 AGENT_ADDENDUM 的追问要求与示例，不放宽判据'}",
        "",
        "## 逐例结果",
        "",
        *(f"- {'OK ' if ok else 'ERR'} `{case['id']}`（{case['kind']}）{case['message']}｜{reason}"
          for case, ok, reason in rows),
        "",
    ]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = REPORT_DIR / f"ch08-ticket-confirm-{datetime.now():%Y%m%d-%H%M%S}.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"追问率 {ask_rate:.3f}／发起率 {invoke_rate:.3f}；报告：{report}")

    await model.root_async_client.close()
    model.root_client.close()
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 3: 跑一遍（真实模型，12 例约 3–6 分钟）**

Run: `LANGSMITH_TRACING=false .venv/Scripts/python.exe -X utf8 -m scripts.evaluate_ticket_confirm`
Expected: 退出码 0，`evals/reports/ch08-ticket-confirm-*.md` 生成，`该追问追问率：1.000`、`说清了直接出卡片率：≥0.90`、工单数 0。不达标的处理顺序写死在报告结论里：**改 `AGENT_ADDENDUM` 的追问要求与示例句子，不放宽判据**（这条与第 7 章摘要评估同一口径）。

- [ ] **Step 4: 留痕并提交**

`dev-notes/ch08.md` 阶段 30 记：12 例、两个指标实测值、报告文件名、失败例子的原文（如果有）。

```bash
git add evals/ticket_confirm_cases.jsonl scripts/evaluate_ticket_confirm.py evals/reports dev-notes/ch08.md
git commit -m "test(ch08): 建工单确认流的追问评估集与脚本（T16）"
```

---

### Task 17: 演示脚本 `scripts/demo_tools_mcp.py`——六条验收逐条给证据

验收①–⑥ 各一个 `--case`，`--case all` 连着跑。约定照抄 `scripts/demo_tools.py`：`httpx.Client(base_url=…)`、`DemoFailure`、失败打印「验收未通过：…」并返回 1。规格 §15。

| 验收 | `--case` | 怎么证明 |
|---|---|---|
| ① 只做注册即可用、不动核心代码 | `register` | 进程内：源码里搜不到 `coupon`；`build_registry` 收到新工具且三样齐全；引擎真跑一次并落审计 |
| ② 两个 Server 独立起进程，问物流走 MCP | `logistics` | 真起 Server 子进程 → `McpDiscovery` 里 `query_logistics.source=="mcp"`；HTTP 问物流，`status` 帧带 `source=mcp`／`mcp_server=logistics` |
| ③ Server 侧新加工具只重启该 Server | `hot-discover` | 同一 demo 进程内：8941 上 Server A（无新工具）→ 发现表没有 `query_station_info`；换成带 `LOGISTICS_EXTRA_TOOLS` 的 Server B → **同一个 `McpDiscovery` 实例** `specs(force=True)` 就看见了 |
| ④ 没说清先追问 → 卡片 → 确认 → 落库带工单号 | `confirm` | HTTP 两步：「帮我建个工单」不许出卡片；补全描述后出卡片，`resume decision=accept` 的正文含 `TK`，`tickets` 表多一行 |
| ⑤ 点取消 → 不建单、审计「权限拒绝」 | `cancel` | HTTP：`resume decision=cancel` 的正文是取消话术；`tickets` 行数不变；审计最后一行 `status=权限拒绝`、`retry_count=0` |
| ⑥ 超时看得见重试与兜底；写操作不自动重试 | `timeout` | 进程内两次：读工具超时 → 审计 `retry_count=1`；写工具超时 → 模块自己数出来的调用次数 **1**、审计 `retry_count=0` |

**Files:**
- Create: `scripts/demo_tools_mcp.py`、`examples/tools/demo_slow_write.py`

**Interfaces:**
- Consumes: `build_registry`／`build_engine`（T9）、`ToolRegistry.get`、`McpDiscovery`（T12）、`mcp_servers.logistics_server`（T11，端口 env `LOGISTICS_PORT`）、`Settings.from_env`、`build_database_url`、`/api/chat`、`/api/chat/resume`
- Produces: 终端打印；退出码 0／1

- [ ] **Step 1: 写 `examples/tools/demo_slow_write.py`（验收⑥写路径的活样本）**

```python
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
```

- [ ] **Step 2: 写 `scripts/demo_tools_mcp.py`**

```python
"""第 8 章六条验收的演示：每条打印它证明了什么，任一硬条件不满足以非零码退出。

前置条件：
  1) 客服服务已起：.venv\\Scripts\\python.exe -X utf8 -m uvicorn app.main:app --port 8000
  2) 两个 MCP Server 已起：双击 start-mcp.bat（只有 --case logistics 需要；其余自己起子进程）
  3) .env 里的模型与数据库可用（服务与脚本读同一个库，才看得到同一条审计）
"""
import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from sqlalchemy import create_engine, func, select

from app.config import Settings
from app.database import build_database_url, build_engine as build_sql_engine, build_session_factory
from app.models import Base, Ticket, ToolAuditLog
from app.storage import ConversationStore
from app.tools.mcp import McpDiscovery
from app.tools.wiring import build_engine, build_registry

REPO = Path(__file__).resolve().parents[1]
CLIENT_ID = "demo-mcp-tools"
CASES = ("register", "logistics", "hot-discover", "confirm", "cancel", "timeout")


class DemoFailure(Exception):
    pass


def show(title, lines) -> None:
    print(f"\n=== {title} ===")
    for line in lines:
        print("  " + line)


def wait_port(port: int, proc: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise DemoFailure(f"MCP Server 提前退出，返回码 {proc.returncode}")
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise DemoFailure(f"MCP Server 端口 {port} 在 {timeout}s 内没起来")


class McpProcess:
    """自己起、自己关：验收③要的就是「只动 Server 这一侧」。"""

    def __init__(self, port: int, env: dict):
        self.port = port
        self.proc = subprocess.Popen([sys.executable, "-m", "mcp_servers.logistics_server"],
                                     cwd=REPO, env={**os.environ, "LOGISTICS_PORT": str(port), **env})
        wait_port(port, self.proc)
        self.url = f"http://127.0.0.1:{port}/mcp"

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def temp_store():
    workdir = tempfile.TemporaryDirectory()
    engine = create_engine(f"sqlite:///{Path(workdir.name) / 'demo.db'}")
    Base.metadata.create_all(engine)
    return ConversationStore(build_session_factory(engine)), Path(workdir.name), workdir


def stream(client: httpx.Client, path: str, body: dict) -> dict:
    frames, answer, conversation_id = [], "", None
    event, data = "", []
    with client.stream("POST", path, json=body) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line and data:
                payload = json.loads("\n".join(data))
                frames.append((event, payload))
                if event == "meta":
                    conversation_id = payload["conversation_id"]
                elif event == "token":
                    answer += payload["content"]
                elif event == "error":
                    raise DemoFailure(payload["message"])
                event, data = "", []
    return {"frames": frames, "answer": answer, "conversation_id": conversation_id}


def card_of(result: dict):
    return next((payload for event, payload in result["frames"] if event == "confirm"), None)


def case_register(settings, args):
    """验收①：新工具只做注册动作就能被用上，核心代码一行没改。"""
    for relative in ("app/workflow/agent_node.py", "app/agent/react.py", "app/tools/wiring.py"):
        if "coupon" in (REPO / relative).read_text(encoding="utf-8"):
            raise DemoFailure(f"{relative} 里出现了 coupon——注册不该改核心代码。")
    store, _path, workdir = temp_store()
    with workdir:
        # 演示不依赖 .env：模块名直接注入 settings，等价于在 .env 里写 TOOL_EXTRA_MODULES
        with_coupon = settings.model_copy(update={"tool_extra_modules": "examples.tools.demo_coupon"})
        conversation_id = store.start_turn(user_id=CLIENT_ID, message="这单有什么券能用？")
        table = build_registry(store, conversation_id, None, settings=with_coupon,
                               mcp_specs=())
        spec = table.get("query_coupon")
        if spec is None:
            raise DemoFailure("注册中心没收到 query_coupon。")
        if not (spec.source == "builtin" and spec.permission == "read" and spec.label and spec.json_schema):
            raise DemoFailure(f"三样不齐：source={spec.source} permission={spec.permission} label={spec.label}")
        engine = build_engine(table, settings=with_coupon, session_factory=store.session_factory)
        outcome = asyncio.run(engine.run(name="query_coupon", args={"order_id": "1001"},
                                         tool_call_id="demo-register", conversation_id=conversation_id))
        if not outcome.ok:
            raise DemoFailure(f"新工具跑不通：{outcome.code}｜{outcome.summary}")
        denied = asyncio.run(engine.run(name="issue_coupon", args={"order_id": "1001"},
                                        tool_call_id="demo-register-write",
                                        conversation_id=conversation_id, confirmed=True))
        if denied.ok or denied.code != "permission_denied":
            raise DemoFailure(f"白名单外的写操作竟然执行了：{denied.code}")
        with store.session_factory() as session:
            rows = session.scalars(select(ToolAuditLog).order_by(ToolAuditLog.id)).all()
            statuses = [row.status for row in rows]
        if "成功" not in statuses or "权限拒绝" not in statuses:
            raise DemoFailure(f"审计没落全：{statuses}")
        show("验收①：注册即可用，不动核心代码", [
            f"query_coupon 已在表里：{spec.label}｜{spec.permission}｜schema {len(spec.json_schema.get('properties', {}))} 个参数",
            f"真跑一次：{outcome.summary}（{outcome.duration_ms}ms，source={outcome.source}）",
            f"注册≠授权：issue_coupon → {denied.code}，审计状态序列 {statuses}",
        ])


def case_logistics(settings, args):
    """验收②：两个 Server 独立进程；问物流这一次走的是 MCP，不是内置。"""
    discovery = McpDiscovery(settings)
    specs = asyncio.run(discovery.specs(force=True))
    by_name = {item.name: item for item in specs}
    if "query_logistics" not in by_name:
        raise DemoFailure("MCP 里没拿到 query_logistics——先双击 start-mcp.bat 起两个 Server。")
    if by_name["query_logistics"].source != "mcp" or by_name["query_logistics"].mcp_server != "logistics":
        raise DemoFailure("query_logistics 的来源不是 logistics MCP Server。")
    if "query_warranty" not in by_name:
        raise DemoFailure("售后 Server 没连上（缺 query_warranty）。")
    with httpx.Client(base_url=args.base_url, timeout=120) as client:
        result = stream(client, "/api/chat",
                        {"message": "订单 1001 的物流到哪了？", "client_id": CLIENT_ID})
    badges = [payload for event, payload in result["frames"] if event == "status"]
    over_mcp = [item for item in badges if item.get("tool") == "query_logistics" and item.get("phase") == "end"]
    if not over_mcp:
        raise DemoFailure(f"这一轮没有物流状态帧。事件：{[e for e, _ in result['frames']]}")
    if over_mcp[-1].get("source") != "mcp":
        raise DemoFailure(f"物流没走 MCP：{over_mcp[-1]}")
    if "1001" not in result["answer"]:
        raise DemoFailure(f"回答没体现订单号。回答：{result['answer']}")
    show("验收②：问物流走 MCP", [
        f"MCP 发现表：{sorted(by_name)}",
        f"status 帧：tool={over_mcp[-1]['tool']} source={over_mcp[-1].get('source')} "
        f"mcp_server={over_mcp[-1].get('mcp_server')} ok={over_mcp[-1]['ok']}",
        f"答：{result['answer'][:120]}",
    ])


def case_hot_discover(settings, args):
    """验收③：Server 侧新加工具只重启 Server——客服这侧进程、代码、缓存配置都不动。"""
    first = McpProcess(8941, {})
    try:
        discovery = McpDiscovery(settings.model_copy(update={"mcp_logistics_url": first.url}))
        before = {item.name for item in asyncio.run(discovery.specs(force=True))}
        if "query_station_info" in before:
            raise DemoFailure("没开额外工具的 Server 就带了 query_station_info，夹具不成立。")
    finally:
        first.stop()
    second = McpProcess(8941, {"LOGISTICS_EXTRA_TOOLS": "query_station_info"})
    try:
        after = {item.name: item for item in asyncio.run(discovery.specs(force=True))}
        if "query_station_info" not in after:
            raise DemoFailure(f"换了 Server 进程后仍没发现新工具：{sorted(after)}")
        if after["query_station_info"].source != "mcp" or after["query_station_info"].permission != "read":
            raise DemoFailure("新工具来源／权限不对：权限只认我们这侧的规则。")
        show("验收③：热发现（同一个进程、同一个 McpDiscovery 实例）", [
            f"重启前：{sorted(before)}",
            f"重启后：{sorted(after)}（新增 query_station_info｜source={after['query_station_info'].source}｜"
            f"permission={after['query_station_info'].permission}）",
            "客服系统代码没改、进程没重启；force=True 只是跳过 60s 缓存，"
            "不设它则新工具最多 60 秒后出现在表里（TOOL_DISCOVERY_CACHE_SECONDS 可调 0 立刻生效）。",
        ])
    finally:
        second.stop()


def case_confirm(settings, args):
    """验收④：没说清→先追问；说清了→卡片→确认→落库，回复带工单号。"""
    with httpx.Client(base_url=args.base_url, timeout=120) as client:
        vague = stream(client, "/api/chat", {"message": "帮我建个工单", "client_id": CLIENT_ID})
        if card_of(vague) is not None:
            raise DemoFailure("信息不足却直接推了卡片——追问约束没生效（硬指标见 T16 评估集）。")
        if not any(mark in vague["answer"] for mark in ("？", "?")):
            raise DemoFailure(f"没有追问，直接答了：{vague['answer'][:80]}")

        full = stream(client, "/api/chat",
                      {"message": "杯子收到就是漏的，帮我建工单要退货", "client_id": CLIENT_ID})
        card = card_of(full)
        if card is None:
            raise DemoFailure(f"说清了却没出卡片：{[e for e, _ in full['frames']]}｜{full['answer'][:80]}")
        resumed = stream(client, "/api/chat/resume",
                         {"conversation_id": full["conversation_id"], "client_id": CLIENT_ID,
                          "decision": "accept", "pending_id": card["pending_id"]})
    if "TK" not in resumed["answer"]:
        raise DemoFailure(f"确认后回复里没有工单号：{resumed['answer']}")
    factory = build_session_factory(build_sql_engine(build_database_url(settings)))
    with factory() as session:
        ticket = session.scalars(select(Ticket).order_by(Ticket.id.desc())).first()
    if ticket is None or ticket.ticket_no not in resumed["answer"]:
        raise DemoFailure("回复里的工单号在 tickets 表里找不到。")
    show("验收④：追问 → 卡片 → 确认 → 落库", [
        f"「帮我建个工单」→ {vague['answer'][:80]}（无卡片、无写操作）",
        f"卡片字段：{[(item['label'], item['value']) for item in card['fields']]}",
        f"确认后：{resumed['answer'][:80]}",
        f"tickets 表最新一行：{ticket.ticket_no}｜{ticket.status}｜{ticket.ticket_type}",
    ])


def case_cancel(settings, args):
    """验收⑤：点取消 → 工单没建，审计按「权限拒绝」留痕。"""
    factory = build_session_factory(build_sql_engine(build_database_url(settings)))
    with factory() as session:
        before = session.scalar(select(func.count()).select_from(Ticket)) or 0
    with httpx.Client(base_url=args.base_url, timeout=120) as client:
        full = stream(client, "/api/chat",
                      {"message": "养生壶第二次通电就跳闸，帮我建工单要求退款", "client_id": CLIENT_ID})
        card = card_of(full)
        if card is None:
            raise DemoFailure(f"没出卡片，取消流程无从谈起：{full['answer'][:80]}")
        resumed = stream(client, "/api/chat/resume",
                         {"conversation_id": full["conversation_id"], "client_id": CLIENT_ID,
                          "decision": "cancel", "pending_id": card["pending_id"]})
    with factory() as session:
        after = session.scalar(select(func.count()).select_from(Ticket)) or 0
        row = session.scalars(select(ToolAuditLog).where(ToolAuditLog.conversation_id == full["conversation_id"])
                              .order_by(ToolAuditLog.id.desc())).first()
    if after != before:
        raise DemoFailure(f"取消了却多出一张工单：{before} → {after}")
    if row is None or row.status != "权限拒绝":
        raise DemoFailure(f"审计没按「权限拒绝」落：{row.status if row else None}")
    if row.retry_count != 0 or row.tool_name != "create_ticket":
        raise DemoFailure(f"取消的审计字段不对：{row.tool_name}｜retry={row.retry_count}")
    show("验收⑤：取消不建单、审计留痕", [
        f"回复：{resumed['answer'][:80]}",
        f"tickets 行数：{before} → {after}（没动）",
        f"审计：{row.tool_name}｜状态={row.status}｜retry_count={row.retry_count}｜"
        f"arguments={row.arguments}｜error={row.error_message}",
    ])


def case_timeout(settings, args):
    """验收⑥：读操作超时能看到重试与兜底；写操作超时必须一次都不重试。"""
    slow = settings.model_copy(update={"tool_timeout_seconds": 0.4, "tool_max_attempts": 2})
    server = McpProcess(8942, {"LOGISTICS_SLOW_SECONDS": "3"})
    # 这个 Server 必须活到读工具真的被调用之后：发现只拿名字和 schema，执行还要再连一次，
    # 提前关掉只会得到「连不上」，测不到超时与重试。写工具是内置模块，不依赖它。
    try:
        discovery = McpDiscovery(slow.model_copy(update={"mcp_logistics_url": server.url}))
        mcp_specs = asyncio.run(discovery.specs(force=True))
        store, _path, workdir = temp_store()
        with workdir:
            conversation_id = store.start_turn(user_id=CLIENT_ID, message="超时演示")
            # 同一个 settings 同时喂注册与引擎：写白名单与短超时都要生效，否则这条用例测不到「不重试」
            spec_settings = slow.model_copy(update={"tool_extra_modules": "examples.tools.demo_slow_write",
                                                    "tool_write_allowlist": "create_ticket,demo_release_stock"})
            table = build_registry(store, conversation_id, None, settings=spec_settings, mcp_specs=mcp_specs)
            engine = build_engine(table, settings=spec_settings, session_factory=store.session_factory)

            read_outcome = asyncio.run(engine.run(name="query_logistics", args={"order_id": "1001"},
                                                  tool_call_id="demo-timeout-read",
                                                  conversation_id=conversation_id))
            if read_outcome.ok or read_outcome.audit_status != "超时":
                raise DemoFailure(f"读工具超时没按「超时」分诊：{read_outcome.code}｜{read_outcome.audit_status}")
            if read_outcome.retry_count != 1:
                raise DemoFailure(f"读操作该重试 1 次（尝试 2 − 1），实际 {read_outcome.retry_count}")

            import examples.tools.demo_slow_write as slow_write

            write_outcome = asyncio.run(engine.run(name="demo_release_stock", args={"note": "超时看次数"},
                                                   tool_call_id="demo-timeout-write",
                                                   conversation_id=conversation_id, confirmed=True))
            if write_outcome.audit_status != "超时":
                raise DemoFailure(f"写工具没按超时分诊：{write_outcome.code}")
            if slow_write.ATTEMPTS != 1 or write_outcome.retry_count != 0:
                raise DemoFailure(f"写操作被自动重试了：调用 {slow_write.ATTEMPTS} 次，retry_count="
                                  f"{write_outcome.retry_count}")

            with store.session_factory() as session:
                rows = session.scalars(select(ToolAuditLog).order_by(ToolAuditLog.id)).all()
            if len(rows) < 2:
                raise DemoFailure(f"审计没落够两行：{len(rows)}")
            last = rows[-1]
            show("验收⑥：超时、重试与兜底", [
                f"读（MCP query_logistics）：状态={read_outcome.audit_status}｜retry_count={read_outcome.retry_count}"
                f"｜耗时={read_outcome.duration_ms}ms｜兜底给模型的话术={read_outcome.content[:60]}",
                f"写（demo_release_stock）：模块内 ATTEMPTS={slow_write.ATTEMPTS}（应为 1）｜"
                f"retry_count={write_outcome.retry_count}（应为 0）",
                "审计字段：tool_name/source/mcp_server/arguments/result_summary/status/error_message/"
                "retry_count/duration_ms/created_at",
                f"最后一行：{last.tool_name}｜{last.tool_source}｜{last.mcp_server}｜{last.status}｜"
                f"{last.retry_count}｜{last.duration_ms}ms｜{last.result_summary}",
            ])
    finally:
        server.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="第 8 章工具系统六条验收演示。")
    parser.add_argument("--case", default="all", choices=("all", *CASES))
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
    except Exception as exc:
        print(f"验收未通过：读不到模型配置——{exc}")
        return 1
    runners = {"register": case_register, "logistics": case_logistics, "hot-discover": case_hot_discover,
               "confirm": case_confirm, "cancel": case_cancel, "timeout": case_timeout}
    selected = CASES if args.case == "all" else (args.case,)
    try:
        for name in selected:
            runners[name](settings, args)
    except (DemoFailure, httpx.HTTPError, ValueError, OSError) as exc:
        message = str(exc) if isinstance(exc, DemoFailure) else "服务或数据库不可用，请检查服务、.env 与上游配置。"
        print("\n验收未通过：" + message)
        return 1
    print(f"\n通过：{('、'.join(selected))}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: 冒烟（不起服务也能跑的两条）**

Run: `.venv/Scripts/python.exe -X utf8 -m scripts.demo_tools_mcp --case register` 与 `--case timeout`
Expected: 都打印「通过」。`register` 靠 `TOOL_EXTRA_MODULES` 里的 `examples.tools.demo_coupon`（**注意：脚本用的是 `settings.model_copy` 传模块名，不依赖 .env**）；`timeout` 会自己起 8942 上的 Server，3 秒 sleep 配 0.4 秒超时。

- [ ] **Step 4: 全量（起服务＋两个 Server）**

先 `start-mcp.bat`、再起 uvicorn，然后
Run: `.venv/Scripts/python.exe -X utf8 -m scripts.demo_tools_mcp --case all`
Expected: 六段全过、退出码 0。`confirm`／`cancel` 依赖真实模型行为——任一条不达标就先看 T16 的报告（同一判据），**不要为了演示好看去改判据**。

- [ ] **Step 5: 留痕并提交**

`dev-notes/ch08.md` 阶段 31 贴六段实际输出（工单号可保留，是演示数据；审计里 `arguments` 只含演示话术）。

```bash
git add scripts/demo_tools_mcp.py examples/tools/demo_slow_write.py dev-notes/ch08.md
git commit -m "feat(ch08): 六条验收的 MCP 演示脚本与超时写样本（T17）"
```

**实现期修正（T17 完成后回填，2026-10-31）**
- `Ticket` 没有 `id`，主键是 `ticket_no`（`app/models.py:98`）；`"TK" in answer` 的号型也不对（实际 `T{YYYYMMDD}{seq:03d}`）。Step 2 的 `case_confirm` 改成 `TICKET_NO = re.compile(r"T\d{11}")` 取号 + `session.get(Ticket, 号码)` 按主键回查——比「查最新一行」强，共享库里最新未必是这轮的。
- `temp_store()` 原文 `with TemporaryDirectory()` 在 Windows 上必 `PermissionError`（SQLite 文件还被连接占着就删目录，`register`／`timeout` 两条当场就红）。改成 `@contextmanager`：先 `engine.dispose()` 再 `cleanup()`。
- `tool_timeout_seconds` 一个键同时管 MCP 握手（`app/tools/mcp.py:60`）与工具调用。原文的 0.4s 实测低于握手耗时（本地 loopback 0.35～0.46s），`query_logistics` 进不了表、用例只能报 `unknown_tool`。改成超时 2s ＋ `LOGISTICS_SLOW_SECONDS=9`。
- `case_hot_discover`／`case_timeout` 额外把 `mcp_servers` 收成 `"logistics"`：这两条只验物流那台，列上售后会白等一次 8932 超时并在终端刷一条误导性的「发现失败」。`case_logistics` 不动——两台独立进程正是它的判据。
- 验收④「有没有追问」的判据从「句尾问号」改成 `ASK_MARKS`（问号／请告诉我／请补充／请说明／麻烦提供／还需要）：真模型回的是「请告诉我：1.…；2.…」这种清单句式。**硬判据不变**（信息不足不许出卡片），T16 评估集门槛一条没动。
- **Step 2 的两句 `case_confirm`／`case_cancel` 话术按原文跑不出卡片**：「…帮我建工单要退货」「…要求退款」会被 ch06 分类成 `退款退货`／`售后`→`policy`（`app/workflow/routing.py:5-14`），这条路不经过 agent 节点，确认流根本不存在。改成落 `物流`→agent 的说法（快递停五天／显示签收但没收到），并在脚本里写明这句约束。
- 六条全过（退出码 0）的实际输出、以及演示顺手抓到的「开发库缺 `tool_audit_logs` 表」一并记在 `dev-notes/ch08.md` 阶段 31；后者给 T18 的 README 加了一条「升级后必须重跑 `start.bat`／`scripts.init_db`」。

---

### Task 18: 文档、代码评审、全量回归与「第八次提交（ch08）」交付

需求文档同步、独立评审、一次性交付。规格 §16、§17。

**Files:**
- Modify: `README.md`、`dev-notes/ch08.md`、评审修复涉及的文件（以实际为准）

**Interfaces:**
- Consumes: T1–T17 的全部产出
- Produces: 远端 `main` 上一条「第八次提交（ch08）」

- [ ] **Step 1: README 逐处改（左边的原句一定位到，改成右边的文本）**

1. 简介段（`README.md:3`）末尾的「以及三层上下文管理（…）。」之后追加一句：

   > 以及即插即用的工具系统（注册中心＋JSON Schema 校验＋权限把门＋统一执行引擎＋审计留痕，物流与售后各一个自建 MCP Server，建工单要页面确认）。

2. `- 五个业务工具：查订单、查商品、查物流（内置 mock 数据）、查知识库、建工单（写库）。` 整条替换：

   `- 七个工具登记在同一张表里：内置查订单、查商品、查知识库、建工单（写库，必须先页面确认）；MCP 查物流轨迹、查是否在保、查退款进度（两个独立进程的 Server，内部按入参确定性生成 mock）。内置 query_logistics 已下线，物流统一走 MCP，不留重名。`

3. `- 九张表：`… 整条替换（表名数与列表都要改）：

   `- 十张表：`conversations`、`messages`、`conversation_summaries`、`faq`、`tickets`、`knowledge_chunks`、`qa_extraction_staging`、`low_confidence_questions`、`faith_cases`、`tool_audit_logs`。`

4. 在「投诉回合不进 Agent…」那条之后新增两条：

   ```
   - 工具系统：新工具只做注册动作就能被主 Agent 用上（`TOOL_EXTRA_MODULES` 动态模块，或 MCP 连上现问现拿）；执行前统一按 JSON Schema 校验，拦下的错误包成一条工具结果回灌模型，不抛异常了事；只读／写两分类由我们这侧的规则定，外部 Server 的用途声明不算数；所有调用走同一个引擎，统一超时／重试／错误分诊／结果格式化（读最多 `TOOL_MAX_ATTEMPTS` 次，写恒为 1 次）；每次调用（含被拒、被拦）都落一行 `tool_audit_logs`。
   - 建工单确认流：只有客户明确要求才发起，信息不足先追问、不许拿编的参数提交；LangGraph `interrupt` 把预览推到页面卡片，点「确认提交」才真执行并回工单号，点「取消」一张单都不建、按「权限拒绝」留痕。第 5 章投诉回合那两个前端按钮那条路保持原样，不接这条流。
   ```

5. 「配置」小节末尾（`TOOL_RESULT_MAX_TOKENS` 等既有键之后）补 ch08 的键，一行一个，值照 `.env.example`：

   ```
   | `MCP_SERVERS` | 要接的 MCP Server 名单，逗号分隔，默认 `logistics,aftersale` |
   | `MCP_LOGISTICS_URL` | 物流 Server 的 Streamable HTTP 地址，默认 `http://127.0.0.1:8931/mcp` |
   | `MCP_LOGISTICS_PERMISSION` | 物流工具的权限（我们这侧说了算），默认 `read` |
   | `MCP_AFTERSALE_URL` | 售后 Server 地址，默认 `http://127.0.0.1:8932/mcp` |
   | `MCP_AFTERSALE_PERMISSION` | 售后工具权限，默认 `read` |
   | `TOOL_WRITE_ALLOWLIST` | 允许执行的写操作名单，默认 `create_ticket`；名单外的写操作直接「权限拒绝」 |
   | `TOOL_EXTRA_MODULES` | 动态工具模块（逗号分隔点分路径），空＝不加载；启动时读取，改了要重启客服服务 |
   | `TOOL_AUDIT_ENABLED` | 是否落审计，默认 `true`；设 `0` 完全停掉留痕 |
   | `TOOL_DISCOVERY_CACHE_SECONDS` | MCP 工具表缓存秒数，默认 `60`；设 `0` 每轮现取 |
   | `TOOL_SCHEMA_RESERVE_TOKENS` | 工具 JSON Schema 在预算里的预留，默认 `400` |
   ```

   再补一句既有键的语义变化：`TOOL_TIMEOUT_SECONDS`（默认 5）与 `TOOL_MAX_ATTEMPTS`（默认 2）从 ch08 起由统一引擎执行，且**只对只读工具重试**，写操作恒为一次。

6. 「快速开始」里 `start.bat` 那条之后加一行（Windows 双击）：

   ```bat
   start-mcp.bat        :: 双击起物流(8931)与售后(8932)两个 MCP Server；要停就关掉那两个窗口
   ```

7. 「演示与验收」小节在 `demo_tools` 那段之后新增：

   ```bat
   .venv\Scripts\python.exe -X utf8 -m scripts.demo_tools_mcp --case all      :: 六条验收逐条给证据
   .venv\Scripts\python.exe -X utf8 -m scripts.evaluate_ticket_confirm        :: 12 例追问／发起标注集，报告写 evals/reports/ch08-ticket-confirm-<时间戳>.md
   ```

   并写清前置条件：先起 uvicorn、`start-mcp.bat` 起两个 Server、`.env` 的模型与数据库可用（`--case register`／`timeout` 不依赖服务，`hot-discover` 自己起 8941）。`scripts/demo_tools.py` 的第一句「订单 1001 的物流到哪了？」现在由 MCP 提供，其余四条不变。

8. 「SSE 协议」的事件顺序里，`status` 那条补上新字段、`orders` 之后插入 `confirm`：

   ```
   - `status`：工具开始/结束（`phase`、`tool`、`label`，结束帧带 `ok`、`summary`、`duration_ms`；ch08 起还带 `source`（`builtin`／`mcp`）与 `mcp_server`）；
   - `confirm`：写操作预览卡片 `{"pending_id","tool","tool_call_id","label","turn","question","fields":[{"label","value"}],"note"}`。出现这一帧时本轮**没有**执行任何东西，`pending_id` 是这张卡片的 nonce，点卡片时原样带回；
   ```

   在该列表之后补一段帧序说明：

   > 暂停一轮的帧序是 `meta → status → status → [token] → confirm → done`；`done` 由服务层从 LangGraph 的 `__interrupt__` 翻出来，好让前端把这条流收干净。恢复一轮（`/api/chat/resume`）没有 `meta`，确认是 `status → status → token → done`，取消是 `token → done`。

9. `POST /api/refunds` 那段之后新增：

   > `POST /api/chat/resume`，请求体 `{"conversation_id": 12, "client_id": "web-xxxxxxxx", "decision": "accept", "pending_id": "3f9c3f9c3f9c"}`：`decision` 只能是 `accept`／`cancel`，`pending_id` 必须是这一轮 `confirm` 帧里给的那个。卡片已失效（会话没挂在确认点上、或已被别的卡片顶掉）回一帧 `error`（`code="no_pending_confirm"`）；他人会话 403，参数不合法 422。`accept` 才执行 `create_ticket` 并写 `tickets` 表，`cancel` 不执行任何东西、只按「权限拒绝」落一行审计。查 `pending_id` 与执行 resume 在同一把会话锁里，所以不存在「检查通过后被别人插一轮」的窗口。

   并在 `/api/tickets` 与 `/api/refunds` 两段末尾各补一句：`这条按钮路径不接 MCP、不走确认卡片，ch05/ch06 的校验与幂等口径原样保留。`

10. 「审计与隐私」新小节（放在 SSE 协议之后、目录之前）：

    > `tool_audit_logs` 每次工具调用落一行（被拒、被拦同样落）：`conversation_id`、`tool_call_id`、`tool_name`、`tool_source`、`mcp_server`、`arguments`、`result_summary`、`status`（成功／失败／超时／校验拦下／权限拒绝）、`error_message`、`retry_count`、`duration_ms`、`created_at`。这张表**不挂外键**——审计是流水账，会话删了记录要留着；写审计失败也只打日志，绝不反过来拦工具执行。`arguments` 存的是模型给出的参数原文，可能含用户描述过的问题细节；本项目所有演示数据都是 mock，不含真实姓名、电话、地址，`.env` 与数据库口令不入库。要完全停掉留痕把 `TOOL_AUDIT_ENABLED=0`。

11. 「目录」代码块里 `app/` 那行补「工具注册中心／校验／权限／格式化／审计／引擎／MCP 发现」，并新增两行：

    ```
    mcp_servers/  两个自建 MCP Server（物流、售后）与确定性 mock
    examples/     知识库示例文档；tools/ 下是动态注册与超时演示用的示例工具
    ```

- [ ] **Step 2: 独立代码评审**

用 `superpowers:requesting-code-review`（本机即 `Agent` ＋ `general-purpose`）派一个评审子代理，**不要带本会话历史**，只给它：改动范围 `bddfeba..HEAD`、规格 `docs/superpowers/specs/2026-10-30-ch08-tool-system-design.md`、计划 `docs/superpowers/plans/2026-10-30-ch08-tool-system.md`、七条需求与六条验收原文。

```bash
git rev-parse HEAD          # 记为 HEAD_SHA，BASE_SHA = bddfeba
```

重点让它盯四处（都是本章最容易出事的地方）：`ToolEngine` 的重试与超时边界（写路径必须一次）、审计失败不许冒泡、`interrupt` 节点重跑的副作用位置、`/api/chat/resume` 的锁与 nonce 校验。Critical 立刻修、Important 修完再往下走、Minor 记进 `dev-notes/ch08.md`。修完重跑 Step 3。

- [ ] **Step 3: 全量回归**

Run: `LANGSMITH_TRACING=false .venv/Scripts/python.exe -X utf8 -m pytest -q`
Expected: 全绿。基线是第 7 章的 313 passed，ch08 净增的用例数（T1/T2/T3–T14 各任务的 Step 计数相加）记进 dev-notes；任何既有用例被改动作废都要单独说明理由（内置 `query_logistics` 下线属合法作废）。

- [ ] **Step 4: 密钥与隐私扫描（只列文件名，绝不回显命中内容）**

```bash
git grep -lI -E "sk-[A-Za-z0-9]{8,}|password=|api_key\s*=\s*[\"'][^\"']{6,}" -- . ':!.env.example' || echo CLEAN
git check-ignore -v .env
git status --porcelain | cut -c4- | grep -E '^(\.env|log/|\.cache/)' || echo "无隐私文件待提交"
```

Expected: 第一条输出 `CLEAN`（或只有本就该存在的示例占位），第二条确认 `.env` 被忽略，第三条输出「无隐私文件待提交」。若命中真密钥：先从文件里删掉，再重新扫描；已进过历史就要按用户流程处理，不许自行 force push 改历史。

- [ ] **Step 5: 压成一条提交**

本章按「每章一次提交」交付，本地分任务提交先攒着，这里压平：

```bash
git status              # 先看清有没有未跟踪的隐私文件或不该带的产物
git reset --soft bddfeba
git add -A
git status              # 复核暂存清单：不得出现 .env / log/ / .cache/
git commit -m "$(cat <<'EOF'
第八次提交（ch08）：即插即用工具系统、两个自建 MCP Server 与建工单确认流

实现了什么：
- 工具注册中心（内置＋动态模块＋MCP 现取合成一张表，重名保留先登记的）、JSON Schema 校验
  （拦下不抛异常，错误包成一条工具结果回灌模型）、只读／写权限把门（只认本地规则，外部声明不算数）、
  统一执行引擎（超时／重试／错误分诊／结果格式化，读最多两次、写恒一次）、
  tool_audit_logs 审计（被拒被拦同样落，无外键，写失败不拦执行）。
- 物流与售后两个 MCP Server（官方 Python SDK，Streamable HTTP 独立进程，确定性 mock）；
  客服系统经 MultiServerMCPClient 接入，内置 query_logistics 下线。
- create_ticket 确认流：LangGraph interrupt 推预览卡片，POST /api/chat/resume 带 nonce，
  确认才执行并回工单号，取消按「权限拒绝」留痕；第 5 章按钮那条路未改动。
- 六条验收的演示脚本 scripts/demo_tools_mcp.py 与追问评估集 scripts/evaluate_ticket_confirm.py。

隐私：.env、log/ 与数据库口令不入库；演示数据全为 mock，不含真实姓名、电话、地址。
EOF
)"
git log --oneline -1
```

- [ ] **Step 6: 推送**

父提交就是远端 `main` 的当前头（`bddfeba`），所以是快进推送，**不需要 force**：

```bash
git -c http.proxy= -c https.proxy= push origin main
```

直推被重置时，单次注入代理（不改全局 git config）：

```bash
git -c http.proxy=http://127.0.0.1:7890 -c https.proxy=http://127.0.0.1:7890 push origin main
```

端口先用 `netstat` 现查，别照旧记忆填。推送后 `git log --oneline -3` 复核远端头是这一条。

- [ ] **Step 7: dev-notes 收尾与交付报告**

`dev-notes/ch08.md` 追加最后一段：评审结论与修了哪几条（Critical/Important/Minor 分列）、全量 pytest 的实际数字、六条验收各自贴的证据（命令＋关键输出行）、密钥扫描结果、提交 sha 与推送结果、以及本计划里那几处「我替你定了」的最终落地情况（暂停轮不发 `actions`、nonce 失效走 `error` 帧而非 409、`session_factory` 只留一个来源、TTL 缺省 60 秒）。

最后给用户的交付说明就三样：**功能演示命令**（`start-mcp.bat` ＋ uvicorn ＋ `demo_tools_mcp --case all` ＋ 页面点一遍卡片）、**测试结果**（全量 pytest 数字＋两个评估集指标）、**dev-notes 路径** `dev-notes/ch08.md`。



