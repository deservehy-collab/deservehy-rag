# ch08 即插即用工具系统设计规格

日期：2026-10-30
状态：定稿（远程授权：用户不在场审批，方案由智能体按推荐拍定，全部决策见 §18）
范围：工具层从「写死的五个内置工具」升级为「注册中心＋校验＋权限＋执行引擎＋审计＋MCP 接入＋建工单确认流」

## 1. 目标

1. 工具不再写死：内置工具与 MCP 工具在**同一份注册表**里长得一模一样（名字／用途描述／JSON Schema 参数定义三样齐全），新增工具只登记、不改核心代码。
2. 每次调用先过**统一校验**，不合法不执行，把中文错误说明当成工具结果回灌模型，让它去追问用户或重组织调用。
3. **权限只认我们这侧的规则**：工具分只读／写两类，写操作由执行引擎把门，本系统唯一写工具 `create_ticket` 必须等前端确认回来才执行。
4. 超时、重试、错误分诊、结果格式化收敛到**一处执行引擎**，内置与 MCP 走同一条路。
5. 每次调用（含被拦被拒）**落 `tool_audit_logs` 一条**，审计写失败不许反过来拦住工具执行。
6. 自建两个业务 MCP Server（物流／售后）跑在独立进程，客服系统作 Client 接入；物流查询由 MCP 接管，内置 `query_logistics` 下线。
7. 建工单走**确认流**：Agent 追问补齐必填 → `LangGraph interrupt` 推预览卡片 → 点「确认提交」落库并在回复里带工单号，点「取消」不执行并按「权限拒绝」落审计。

## 2. 范围

### 2.1 本章做

- `app/tools/` 包：注册中心、JSON Schema 校验、权限判定、执行引擎、结果格式化、审计写入、MCP 接入。
- `tool_audit_logs` 表（ORM ＋ `scripts/init_sql/006_tools.sql`）。
- 两个 MCP Server 进程（Streamable HTTP）与 `MultiServerMCPClient` 接入。
- 内置 `query_logistics` 下线，改由物流 MCP Server 提供同名工具。
- `create_ticket` 的 interrupt 确认流：图节点、`confirm` SSE 帧、`POST /api/chat/resume`、前端预览卡片。
- 演示脚本 `scripts/demo_tools_mcp.py`、追问行为标注集 `evals/ticket_confirm_cases.jsonl` 与评估脚本。

### 2.2 本章不做

- Skill 机制（生态概念，不落代码）。
- 仓储等更多外部系统：两个业务 MCP 足够。
- 工具级限流、熔断、成本账单、工具市场／热部署框架。
- MCP resources／prompts／sampling：只用 tools。
- 内置工具改造成进程外；内置仍在客服进程内执行。

## 3. 现状与差距（逐条有证据）

| 现状 | 证据 | 差距 |
|---|---|---|
| 五个内置工具用 `@tool(args_schema=Pydantic)` 声明，参数定义只在 pydantic 里 | `app/tools.py:232`、`:82-112` | 注册中心要的是 JSON Schema，需统一成 `dict` 口径 |
| `ToolExecutor` 已有 pydantic 预校验、逐工具超时、重试、错误分诊、`ensure_ascii=False` | `app/tools.py:139-202` | 校验写死 `tool.args_schema.model_validate(args)`：MCP 工具的 `args_schema` 是 **JSON Schema dict**（`langchain_mcp_adapters/tools.py:531` 直接塞 `tool.inputSchema`），这一行对 MCP 工具会 `AttributeError` |
| 重试白名单叫 `RESTRICTED_TOOLS`，实为「只读工具可重试」 | `app/tools.py:19`、`:153-154` | 名字反义、按工具名硬编码集合，MCP 工具进不来 |
| `ActionGuard` 是输出流文本过滤器（剥 `[[actions:...]]`），不是权限闸门 | `app/agent/guards.py:77-130` | 用户需求的「写操作把门」**目前不存在**，全新实现 |
| 全仓无 `interrupt(`、无 `Command(resume=...)`；第 6 章订单选择器是 `State.pending` ＋前端把点选当新一句话重发 | `app/workflow/policy_nodes.py:66-68`、`app/workflow/coref.py:28-37`、`app/web/chat.js:460-463` | 本章按定死选型改用 LangGraph `interrupt`，这条链是新写的，不复用点选续跑 |
| 内置 mock 是硬编码字典，ch02 规格明确否决随机数 | `app/tools.py:21-71`、`docs/superpowers/specs/2026-10-28-ch02-function-calling-design.md:124` | 与本章「随机生成 mock」冲突，处理方式见 §18-C1 |
| 无审计表；`tickets` 由 HTTP 接口或模型直接写库，无调用留痕 | `app/storage.py:194-212` | 新建 `tool_audit_logs` |
| 工单号在 `messages` 里只有 assistant／tool 两类行，SSE 帧 8 个（`meta/token/status/citations/actions/orders/done/error`） | `app/main.py:184-192`、`README.md:295-302` | 新增第 9 帧 `confirm` |

## 4. 组件与边界

```
用户消息 → /api/chat → Workflow → agent_node
                                      │  ①现取工具集：builtin_specs() + mcp.discover_specs()
                                      │  ②ToolRegistry 合并（重名即拒）
                                      │  ③model.bind_tools(registry.langchain_tools())
                                      ▼
                              ToolEngine.run(name, args, confirmed)
                                 │ validate_arguments ─失败→ 校验拦下（回灌中文说明）
                                 │ policy.decide ─CONFIRM_REQUIRED→ State.pending_write + emit(confirm) → confirm_ticket
                                 │                 └─REJECT→ 权限拒绝（落审计）
                                 │ 执行（超时／重试矩阵）→ format_result
                                 └ audit.record(ToolOutcome)   ← 每条调用必落，写失败只告警
confirm_ticket(interrupt) ─confirm→ execute_write → END
                    └────cancel──→ write_cancelled → END
```

| 模块 | 一句话职责 | 依赖 |
|---|---|---|
| `app/tools/registry.py` | `ToolSpec` 三样＋`ToolRegistry` 合并与查重 | 无业务依赖 |
| `app/tools/builtin.py` | 内置工具定义、参数模型、固定 mock；`builtin_specs(settings)` | `registry`、`storage`、`knowledge` |
| `app/tools/validate.py` | 按 JSON Schema 校验参数，产出中文错误说明 | `registry.ToolSpec` |
| `app/tools/policy.py` | 只读／写分类与三态判定，只认本地配置 | `registry.ToolSpec`、`settings` |
| `app/tools/engine.py` | 唯一调用出口：校验→权限→超时重试→分诊→格式化→审计 | 上面四个＋`audit` |
| `app/tools/formatting.py` | 结果挑字段、枚举码翻人话、中文不转义、摘要模板 | `registry.ToolSpec` |
| `app/tools/audit.py` | 写 `tool_audit_logs`，绝不抛 | `models.ToolAuditLog` |
| `app/tools/mcp.py` | MCP 连接配置、`MultiServerMCPClient`、把 MCP 工具转成 `ToolSpec` | `registry`、`settings` |

公开接口（后续任务按此签名对接）：

```python
# registry.py
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    json_schema: dict
    source: str                    # "builtin" | "mcp"
    mcp_server: str | None         # 内置为 None
    permission: str                # "read" | "write"
    label: str                     # 前端徽章中文名
    tool: BaseTool

class ToolRegistry:
    def __init__(self, specs: Sequence[ToolSpec]) -> None: ...   # 重名抛 DuplicateToolError
    def get(self, name: str) -> ToolSpec | None: ...
    def specs(self) -> list[ToolSpec]: ...
    def langchain_tools(self) -> list[BaseTool]: ...

# engine.py
@dataclass(frozen=True)
class ToolOutcome:
    tool: str; ok: bool; code: str; content: str; summary: str; duration_ms: int
    source: str; mcp_server: str | None; retry_count: int; audit_status: str; arguments: dict

class ToolEngine:
    async def run(self, *, name: str, args: dict, tool_call_id: str | None,
                  conversation_id: int | None, confirmed: bool = False) -> ToolOutcome: ...
```

## 5. 数据模型：`tool_audit_logs`

用户提供的 DDL 原样落 `scripts/init_sql/006_tools.sql`（含 `SET NAMES utf8mb4;` 头一行，与 001/003/004/005 一致），ORM 在 `app/models.py` 同构：

- `id` / `conversation_id`（NULL 允许，无会话上下文的调用也要能记）/ `tool_call_id` / `tool_name` / `tool_source` ENUM(`builtin`,`mcp`) / `mcp_server` / `arguments` JSON / `result_summary` TEXT / `status` ENUM(`成功`,`失败`,`超时`,`校验拦下`,`权限拒绝`) / `error_message` / `retry_count` / `duration_ms` / `created_at`。
- **不挂外键**：审计写入不能被引用约束拦住；`conversation_id` 只建普通索引。
- 索引名沿用用户给的 `idx_conversation_id` / `idx_tool_name` / `idx_status`（已逐名比对 `app/models.py` 现有 13 个 `Index(...)`，三者均无冲突；SQLite 索引名全库唯一，见 `app/models.py:100` 注释）。
- ORM 用 `IdType`／`TABLE_OPTIONS`／`Enum(...)` 与既有表一致（`app/models.py:8-10`、`:91-93`），`arguments` 用已导入的 `JSON` 类型，SQLite 与 MySQL 双向可建。
- 新库由 `scripts/init_db.py` 的 `Base.metadata.create_all` 直接建表；已有库执行 006 一次。两套并存规则不变（`scripts/init_sql/005_context.sql:2-3`）。

`audit_status` 与 `ok`／`code` 的映射（唯一口径）：

| 触发 | `code` | `audit_status` | `ok` |
|---|---|---|---|
| 正常返回 | `ok` | `成功` | True |
| 工具返回 `{"ok": false}`（查不到） | `not_found` 等原码 | `失败` | False |
| 抛异常（非超时） | `tool_error` | `失败` | False |
| 超时（重试耗尽） | `tool_timeout` | `超时` | False |
| Schema 校验不过 | `invalid_arguments` | `校验拦下` | False |
| 权限判定拒绝／用户点取消 | `permission_denied` | `权限拒绝` | False |
| 需要确认（尚未拒绝） | `confirm_required` | 不落审计 | False |

`confirm_required` 不落审计：它既不是成功也不是拒绝，落了他会污染统计；用户随后点取消才落「权限拒绝」（验收⑤口径）。

`retry_count` 口径固定为**实际发生的重试次数 = 尝试次数 − 1**：一次成功的调用记 0，`TOOL_MAX_ATTEMPTS=2` 且两次都超时记 1，写操作恒为 0。验收⑥就按这个数读。

## 6. 注册中心

1. **统一三样**：内置工具由 pydantic 参数模型 `model_json_schema()` 转 JSON Schema；MCP 工具直接用 `StructuredTool.args_schema`（已是 Server 给的 `inputSchema`）。
2. **内置登记时机**：内置工具的实现闭包要绑 `conversation_id`（建工单、查订单都得知道是哪次会话），所以 `ToolSpec` 列表**每轮现拼**（`build_specs(store, conversation_id, knowledge)`），不能在启动时缓存一份；能在启动时缓存的只有与请求无关的静态元数据（名字、描述、JSON Schema、权限、中文名）。`query_logistics` 从这里下线（§11）。
3. **MCP 现问现拿**：每次构造上下文前 `await client.get_tools()` 重新拉取。adapters 0.3.2 的语义正合适——`get_tools()` 每次调用新建 session、无常驻连接（`langchain_mcp_adapters/client.py:166-176`），所以 Server 侧新加工具后只重启 Server 就可见（验收③）。
4. **重名即拒**：`ToolRegistry.__init__` 遇到重名抛 `DuplicateToolError`，由调用方记一条 `logger.error` 并回退到「丢掉后到的那个、保住先到的内置」，绝不让两个同名工具进 `bind_tools`（OpenAI 会直接 400）。
5. **不改核心代码的新工具**：`TOOL_EXTRA_MODULES`（逗号分隔的点分模块路径）里的模块暴露 `EXTRA_TOOLS: list[BaseTool]`，注册中心按配置追加加载。演示与验收①用这条，而不是改 `agent_node.py`。
   - 适用边界要说清：这个配置在**启动时**读取（`Settings` 只在 `create_app` 里装一次，模块本身也被 `sys.modules` 缓存），改它需要重启客服服务。验收①的原话只承诺「只做注册动作、不动核心代码」，没承诺免重启；承诺免重启的是第 3 点的 MCP 现取。两条别混。
6. **工具表不可变**：`ToolRegistry` 一旦构造完成只读；每轮请求基于「本轮现拼的内置 ＋ 本轮 `get_tools()` 结果」新建一个实例，不改全局状态，避免并发请求互相污染。

## 7. 参数校验

1. `jsonschema.Draft202012Validator(spec.json_schema)`。内置工具的 schema 由 pydantic 生成，本就带 `extra="forbid"` 语义；MCP 工具的 `inputSchema` **原样使用，不额外注入 `additionalProperties: false`**——只有第 10 节里我们自己写、能在测试里穷举入参的那两个 Server 才按最严格口径校验，将来接入别的第三方 Server 时，注入这条会误杀对方本来合法接受的字段。越权风险由 §8 权限把门兜，不靠收紧 schema。
2. 拦下三类问题：类型不对、必填缺失、取值越界（含 pydantic 转成 JSON Schema 的 `min_length`／`max_length`／`enum`／`ge`／`le`）。
3. **不抛异常**：错误说明包成一条工具结果回灌模型，正文中文、带字段名与期望，例如：
   `参数校验未通过：ticket_type 缺失（期望：工单类型，只能是：售后、投诉、咨询）。请向用户确认后重新调用。`
4. 校验发生在权限判定**之前**：不给非法参数任何执行或确认的机会。
5. 内置工具不再走 `args_schema.model_validate`（`app/tools.py:145` 那条对 MCP 会炸的路径整体退役）。

## 8. 权限控制

判定函数 `policy.decide(spec, confirmed, settings)` → `PermissionDecision(allowed, needs_confirm, reason)`，规则按顺序命中：

| 条件 | 结果 |
|---|---|
| `spec.permission == "read"` | 放行 |
| `spec.name` 在 `TOOL_WRITE_ALLOWLIST` 且 `confirmed=True` | 放行 |
| `spec.name` 在 `TOOL_WRITE_ALLOWLIST` 且 `confirmed=False` | `needs_confirm=True`（引擎不执行，转确认流） |
| `spec.source == "mcp"` 且其 Server 在本地配置里声明为 `permission=write`，但工具不在写白名单 | 拒绝，`reason=外部写操作未列入本地写白名单` |
| 其余（`permission == "write"` 且不在白名单） | 拒绝，`reason=写操作白名单外` |

1. **只认我们这侧**：分类来源是本地配置（Server 的 `permission` 标记＋工具名白名单），完全不看 MCP Server 自己写的 `ToolAnnotations`，也不让模型临场判断。adapters 也不把 annotations 透传成 `BaseTool` 字段，这条路天然封死。
2. 本章 `TOOL_WRITE_ALLOWLIST` 只有 `create_ticket`；两个 MCP Server 都标 `permission=read`。
3. 被拒的调用：引擎产出 `code=permission_denied` 的 `ToolOutcome`（模型看到「这个操作需要用户在页面确认后才能执行」这类人话），同时落一条「权限拒绝」审计。
4. **没有绕过口**：`react.py` 不再直接碰 `BaseTool.invoke`／`store.create_ticket`，写库只存在于 `execute_write` 节点里的这一次 `engine.run(confirmed=True)`。

## 9. 执行引擎

顺序固定：`查注册表 → 校验 → 权限 → 执行(超时/重试) → 格式化 → 审计`。

**重试矩阵**

| 情形 | 重试 | 说明 |
|---|---|---|
| `permission == "read"` | 最多 `TOOL_MAX_ATTEMPTS`（默认 2）次 | 只对暂时性故障重试：超时、数据库暂时不可用、MCP 传输层异常 |
| `permission == "write"` | 1 次，`retry_count` 恒为 0 | 超时未必没执行，重复执行比失败更糟（沿用 `app/tools.py:153-154` 的既有判断，改成权限驱动而非工具名硬编码） |
| 工具返回 `{"ok": false}` 业务空结果 | 不重试 | 空结果不是故障（现行为保持） |
| 参数校验／权限拦下 | 不重试、不执行 | — |

**错误三分诊**（如实回给模型，不粉饰）

| 类 | `code` | 给模型的 content |
|---|---|---|
| 参数不合法 | `invalid_arguments` | 中文校验说明（§7） |
| 查询落空 | 工具原码（`not_found` 等） | 工具原样 JSON，`ok=false` |
| 真故障 | `tool_error` / `tool_timeout` | 人话原因＋是否可稍后重试 |

**结果格式化**（`formatting.py`，数据驱动，内置与 MCP 同一套）

1. 字段挑选：按注册表里的投影规则只留回答用得上的字段（例：物流留 `carrier/tracking_no/status/nodes[-1]`；订单留 `order_id/status/amount/items[].name`），投影表缺失时原样透传，绝不因格式化丢关键字段。
2. 枚举码翻人话：`formatting.ENUM_LABELS` 集中维护（现状散在 `app/tools.py:12-18` 的 `TOOL_LABELS`、`app/workflow/actions.py:12-16`）。
3. 序列化 `json.dumps(..., ensure_ascii=False, default=str)`（沿用现口径）。
4. `summary` 给前端徽章：MCP 工具没有内置模板时走通用模板「{label}：{结果条数或关键字段}」，截断 `MAX_SUMMARY=60`。
5. MCP 返回体：adapters 用 `response_format="content_and_artifact"`（`langchain_mcp_adapters/tools.py:533`），`invoke()` 返回 `(content, artifact)`，引擎统一归一：优先取 `artifact` 的结构体，无则把 `content` 里的文本块拼成字符串再尝试 `json.loads`。

**超时**：`TOOL_TIMEOUT_SECONDS`（默认 5）为全局，`query_faq` 继续吃 `KNOWLEDGE_TOOL_TIMEOUT_SECONDS`（默认 20）；MCP 工具另受 adapters 的 `timeout`／`sse_read_timeout` 约束，两者取本地超时更小的那个生效。

## 10. MCP Server 侧

目录 `mcp_servers/`（不叫 `mcp/`，避免与官方包 `mcp` 顶名）。

| Server | 端口 | 工具 | 说明 |
|---|---|---|---|
| `logistics` | `127.0.0.1:8931`，路径 `/mcp` | `query_logistics(order_id)` | 接管原内置物流查询 |
| `aftersale` | `127.0.0.1:8932`，路径 `/mcp` | `query_warranty(order_id)`、`query_refund_progress(order_id)` | 查在保／查退货进度 |

- 写法：`FastMCP(name, host="127.0.0.1", port=..., streamable_http_path="/mcp")` ＋ `@mcp.tool(description=..., annotations=ToolAnnotations(readOnlyHint=True))` ＋ `mcp.run(transport="streamable-http")`。字面量以已装 `mcp==1.30.0` 源码为准：`FastMCP.run` 收 `"streamable-http"`（连字符，`mcp/server/fastmcp/server.py:297`），Client 侧 adapters 收 `"streamable_http"`（下划线，`langchain_mcp_adapters/sessions.py:167`）——两处不一样，写死在配置注释里防手滑。
- mock 数据：**按入参种子的确定性生成**（`random.Random(f"{tool}:{order_id}")` 派生承运商／节点时间／在保剩余天数），每次同参数结果一致，不同参数看起来像随机数据。理由与冲突记录见 §18-C1。
- 不接真实系统、不建表、不落库；Server 进程自己只读环境变量。
- 演示夹具（不属于产品行为，代码注释与 README 都要标）：`LOGISTICS_SLOW_SECONDS`（工具内 sleep，造超时）、`LOGISTICS_FAIL_TIMES`（前 N 次抛错，造重试）、`LOGISTICS_EXTRA_TOOLS=query_station_info`（逗号分隔的额外工具名，造验收③的「Server 侧新加工具」）、`TICKET_SLOW_SECONDS`（内置 `create_ticket` 内 sleep，造写操作超时）。

## 11. MCP Client 接入与内置物流下线

1. `mcp.py` 按 `MCP_SERVERS=logistics,aftersale` 逐个构造连接配置；`MultiServerMCPClient(connections, tool_name_prefix=False, handle_tool_errors=False)`。
   - `handle_tool_errors=False` 是刻意的：默认 `True` 会把 MCP 执行错误转成 `ToolMessage(status="error")`（`langchain_mcp_adapters/tools.py:527`），错误就不抛了，我们的分诊与重试会瞎掉。关掉后异常照常上抛，由引擎统一分诊。
2. 每次请求现取工具 → 转 `ToolSpec(source="mcp", permission=<本地配置>)` → 与内置合并进同一个 `ToolRegistry`。
3. 内置 `query_logistics` 从 `builtin_specs()` 摘掉，`TOOL_LABELS` 里保留「查询物流」这个中文徽章名（label 挂在 MCP 的 spec 上），README 的工具清单同步改。**不留重名**：物流 MCP 工具就叫 `query_logistics`。
4. Server 挂了怎么办：`get_tools()` 抛异常时记 `logger.warning`、本轮以「只剩内置＋上次缓存」继续跑（缓存 TTL 60s），对话不因外部系统不可用而整条断掉；缓存只兜「列表」，调用仍然现打，避免拿旧列表打到已消失的工具。

## 12. 建工单确认流

1. **触发**：模型申请 `create_ticket`。必填缺失时引擎按 §7 回灌中文说明，模型自己追问用户（不许编）——这是验收④前半段。
2. **agent_node**：`engine.run(confirmed=False)` 返回 `code=confirm_required` → 把 `State.pending_write = {"tool": "create_ticket", "args": {...}, "tool_call_id": ..., "label": ..., "pending_id": <nonce>}` 交回图后 return。**`confirm` 帧不在这里发**：由 `events.unwrap` 从 `__interrupt__` 翻译（见第 4 条）。
3. **图**：`agent → (条件边) confirm_ticket → (条件边) {execute_write | write_cancelled} → log → END`。必须经过 `log`：正文落库与 `done` 帧都在那儿，直连 END 前端就收不到收尾。
4. **confirm_ticket 节点体内只有 `interrupt(write_preview(state))` 这一句**：LangGraph 恢复时**从节点开头重跑**（`langgraph/types.py:916-918`），所以这一句之外不许有任何副作用（emit／落库都不行）。卡片载荷由 `write_preview` 现算，8 个字段（`pending_id／tool／tool_call_id／label／turn／question／fields／note`）；`unwrap` 把挂起帧翻成 `confirm`＋`done`，一次挂起只出一张卡片。
5. **execute_write**（`decision == "accept"`）：`engine.run(..., confirmed=True)` 真执行 → 落 `tickets` → `log` 节点写一条 assistant 正文（带工单号，工单号取 `storage.py:214-218` 的 `T{YYYYMMDD}{seq:03d}`）。**执行结果不再补第二条 tool 行**：历史只回放 user/assistant 正文（`storage._history_filter`），工单号靠正文进模型上下文；暂停轮那条 `confirm_required` 的 tool 行已经与 `assistant(tool_calls)` 配成 1:1，补第二条会把它变成 1:2。徽章与正文共用一个判据——拿不到工单号就按失败上报。
6. **write_cancelled**（其他任何 resume 值）：不调用工具、不落 `tickets`，由 `audit` 直接写一条 `status=权限拒绝`、`error_message=用户在预览卡片取消` 的记录，回复固定人话「已取消，没有提交工单」。
7. **resume 接口**：`POST /api/chat/resume`，体 `{conversation_id, client_id, decision: "accept"|"cancel"}`；owner 校验沿用 `main.py:200-204`（不是本人 403），会话锁沿用 `Workflow.lock`（`graph.py:97-100`），流式返回与 `/api/chat` **同一套帧**，前端把 token 续到同一条消息上。`Workflow.resume` 自己先查 checkpoint：没有挂起的 interrupt 就空操作（否则 `Command(resume=...)` 会被当成新输入从 `START` 起图）。
8. **幂等**：LangGraph 的 interrupt 只在挂起态可被 resume 一次，卡片作废后再点拿到 `no_pending_confirm`（`service.py:159-164` 在锁内发一帧 `error`，不是 HTTP 409——那条流已经是 SSE 了；`no_pending_offer` 是第 5 章动作按钮那条路的码，别混）；第 5 章投诉回合「前端按钮直接建工单」那条路**完全不动**（`chat.js:419` → `POST /api/tickets`），点按钮本身就是用户确认。

## 13. 前端（Vibe Coding，按用户要求不套流程）

聊天流里一张工单预览卡片：标题「确认提交工单？」、两行只读信息（工单类型、问题描述）、「确认提交」「取消」两个按钮；点完按钮立刻禁用并回显状态，旧卡片在新轮次统一失效（沿用 `chat.js:656-658`）。最贴近的现成模式是 `addRefundForm`（`chat.js:470-520`：建 form、`preventDefault`、成功后禁用控件）。

## 14. 配置（`.env` 新增，全部有默认值，缺配置不影响启动）

```
MCP_SERVERS=logistics,aftersale
MCP_LOGISTICS_URL=http://127.0.0.1:8931/mcp
MCP_LOGISTICS_PERMISSION=read
MCP_AFTERSALE_URL=http://127.0.0.1:8932/mcp
MCP_AFTERSALE_PERMISSION=read
TOOL_WRITE_ALLOWLIST=create_ticket
TOOL_EXTRA_MODULES=
TOOL_AUDIT_ENABLED=true
TOOL_DISCOVERY_CACHE_SECONDS=60
```

`TOOL_TIMEOUT_SECONDS`／`TOOL_MAX_ATTEMPTS`／`KNOWLEDGE_TOOL_TIMEOUT_SECONDS` 沿用现有键（`app/config.py:51-52`、`:73`）。

## 15. 错误与边界

- 注册中心缺三样之一 → 启动即 `ValueError`（宁可起不来，也不让模型见到描述为空／schema 为 `{}` 的工具）。
- MCP Server 未起 → 本轮只有内置工具，`logger.warning`，对话照常；物流问题会走「查不到」而非崩。
- 审计写入失败（库挂／字段超长）→ 截断 `result_summary` 到 2000 字后重试一次，仍失败只 `logger.warning`，**工具调用结果照常返回**。
- `arguments` 里可能带用户原话：审计表照记（这是审计的目的），README 明确提示该表含个人信息、与 `messages` 同级隐私，不入库不进 git。
- `ToolRegistry` 拒重名后仍想让外部工具生效 → 用 `tool_name_prefix=True` 或改 Server 侧工具名，本章不做自动改名。
- resume 时 checkpoint 里没有挂起的 interrupt（卡片已过期、被别的卡片顶掉、进程重启）→ 在会话锁内回一帧 SSE `error`（`code="no_pending_confirm"`，前端话术「这张确认卡片已经失效了，请重新说一次你的需求」），**不发 HTTP 409**：查 `pending_write` 与 `Command(resume=...)` 必须在同一把锁里，否则「检查通过 → 别人插一轮 → resume 打到新卡片」的窗口关不掉；`/api/tickets` 那条按钮路径的 409 保持原样。

## 16. 测试与评估

TDD（先红后绿，`pytest` 全绿是硬门槛）：

| 用例组 | 关键断言 |
|---|---|
| `tests/test_tool_registry.py` | 内置 pydantic → JSON Schema 三样齐全；重名抛 `DuplicateToolError`；`TOOL_EXTRA_MODULES` 免改核心码即可见 |
| `tests/test_tool_validate.py` | 类型错／必填缺／越界／多余字段各拦下；中文说明含字段名与期望；拦下不抛异常 |
| `tests/test_tool_policy.py` | §8 五行决策表逐行走查；MCP Server 标 write 且不在白名单 → 拒绝；Server 声明只读但白名单外写工具仍拒绝 |
| `tests/test_tool_engine.py` | 读工具超时重试到 `TOOL_MAX_ATTEMPTS`、写工具 `retry_count==0`；业务空结果不重试；MCP 工具走通（`args_schema` 为 dict 不再 `AttributeError`）；`(content, artifact)` 归一；`ensure_ascii=False` |
| `tests/test_tool_audit.py` | 六类状态各落一条；被拒与被校验拦下也落；审计失败不阻断执行（`session_factory` 抛异常 → outcome 仍 ok） |
| `tests/test_tool_mcp_client.py` | 连接配置字面量是 `streamable_http`；`handle_tool_errors=False`；Server 不可用降级为内置＋缓存 |
| `tests/test_workflow_confirm.py`＋`tests/test_chat_resume.py` | interrupt 挂起 → `confirm` 帧 → resume `accept` 落 `tickets` 且回复含工单号；resume `cancel` 不落库且审计「权限拒绝」；重复 resume 是空操作（返回空帧）；nonce 不符回一帧 `error`；暂停轮的正文与卡片只发一次 |
| `tests/test_tools.py`（改） | 原物流断言迁到 MCP 侧；内置清单里 `query_logistics` 消失 |
| `tests/test_mcp_servers.py` | 起真 Server 进程（`streamable_http`）打 `/mcp` 列工具＋调用工具；确定性 mock（同参数两次结果字节一致）；三个夹具开关（慢／前 N 次失败／额外工具） |
| `tests/test_mcp_client.py` | 连接配置字面量是 `streamable_http`；`handle_tool_errors=False`；Server 不可用降级为内置＋上次缓存 |

Prompt／数据类任务按用户要求把 TDD 换成标注样例或评估集：

- `evals/ticket_confirm_cases.jsonl`：12 条「要建工单但缺必填／信息齐全／类型含糊」样例（6 条 `ask`＋6 条 `invoke`），`scripts/evaluate_ticket_confirm.py` 打真模型跑，门槛＝该追问的追问率 1.000、不该追问的直接发起率 ≥0.90，报告落 `evals/reports/ch08-ticket-confirm-<时间戳>.md`。评估把意图分类钉成「其他」直连主 Agent，只评发起／追问行为，不掺路由抖动。
- 内置工具下线：不算 prompt，但同样跑一次演示脚本对照。

前端卡片＝Vibe Coding 例外：不套 brainstorm／TDD／code review，用浏览器实点验。

## 17. 验收（逐条对上用户原话）

| # | 用户原话 | 怎么验 |
|---|---|---|
| 1 | 新写一个简单工具，只做注册动作，Agent 就能用上 | `scripts/demo_tools_mcp.py --case register`：把 `examples.tools.demo_coupon` 注进 `TOOL_EXTRA_MODULES`，注册中心当场给出三样齐全的 `ToolSpec`，统一引擎真跑一次并落审计；同时断言 `agent_node.py`／`react.py`／`wiring.py` 源码里搜不到 `coupon`——核心代码 0 改动 |
| 2 | 两个 Server 单独起进程，问物流轨迹走 MCP | `--case logistics`：断言 SSE `status` 帧里 `source=mcp`、`mcp_server=logistics`，`tool_audit_logs` 落一条 builtin 以外的成功记录 |
| 3 | Server 侧新加工具，只重启 Server，客服系统代码不动、不重启 | `--case hot-discover`：同一个演示进程内、同一个 `McpDiscovery` 实例，先连不带额外工具的 Server（表里没有 `query_station_info`），关掉进程再在同一个端口起带 `LOGISTICS_EXTRA_TOOLS=query_station_info` 的 Server，`specs(force=True)` 就看见新工具（不设 `force` 则最多等 `TOOL_DISCOVERY_CACHE_SECONDS=60` 秒）；客服系统代码一行没改 |
| 4 | 「帮我建个工单」但没说清 → 先追问；出现预览卡片；点确认 → 落库＋回复带工单号 | `--case confirm`：脚本先问「帮我建个工单」，断言没有 `confirm` 帧且正文是问句；再补全描述得 `confirm` 帧，`POST /api/chat/resume {decision:"accept"}`，查 `tickets` 新增行与回复里的 `T…` 工单号；前端浏览器实点一次 |
| 5 | 同一张预览点取消 → 工单没建，审计里 create_ticket 状态是「权限拒绝」 | `--case cancel`：`tickets` 行数不变，`tool_audit_logs` 末条 `status='权限拒绝'`、`tool_name='create_ticket'`、`retry_count=0` |
| 6 | 人为造超时：看得到重试与兜底，审计里重试次数／超时状态／耗时齐全；写操作超时不被自动重试（`retry_count` 为 0） | `--case timeout`：读侧 `LOGISTICS_SLOW_SECONDS=9` 配 `TOOL_TIMEOUT_SECONDS=2`、`TOOL_MAX_ATTEMPTS=2` → 审计 `status='超时'`、`retry_count=1`、`duration_ms` 有值；写侧换成 `examples/tools/demo_slow_write.py`（模块内 `ATTEMPTS` 自己数被调几次）→ `ATTEMPTS==1`、`retry_count=0`。（超时阈值原定 0.4 秒，实测连 MCP 握手都不止那么多，会被「发现失败」抢先，见 dev-notes 阶段 31 的实现期修正。） |

## 18. 决策留痕（远程授权下我替你定的）

**C1 冲突：ch02 说「mock 用固定字典、不用随机数以保证可复现」，本章你写「随机生成 mock 数据」。**
处理：取**按入参种子的确定性生成**——数据由 `random.Random(f"{tool}:{key}")` 现场生成（满足「随机生成」、Server 内不建表不写死表），同参数结果恒定（满足可复现与测试断言）。不采纳纯随机，因为验收②③与全部集成测试都要能对答案字节断言。

**C2 冲突：第 6 章订单选择器并没有用 `interrupt`（是 `State.pending`＋前端把点选当新问题重发），而你要求「照第 6 章的做法用 LangGraph interrupt」。**
处理：按定死的技术栈走真 `interrupt`＋`Command(resume=...)`，交互形态沿用订单卡片的观感（聊天流里一张卡片、点按钮续跑）。旧点选链一行不改，两条路并存。

**C3 「登记进同一份工具表」我理解为进程内注册中心，不是 MySQL 表。**
理由：验收③要求新工具不重启客服服务就可用，工具定义若落 DB 就得分层刷新与失效，复杂度白花。DB 侧只落「调用审计」（§5）。若你其实想要一张 `tools` 定义表，说一声，加一张只读物化表即可，注册中心接口不变。

**C4 模块划分：新建 `app/tools/` 包，把现在的单文件 `app/tools.py` 拆开。**
理由：一个文件同时装 mock 数据／参数模型／执行引擎／注册，本章要加五件事进去，必然膨胀到 600＋ 行。拆完 `builtin.py` 只装工具定义与 mock，其余各一职责（§4）。代价是所有 `from app.tools import ...` 的引用点与测试要改，`pytest` 全绿兜底。

**C5 MCP 工具的权限默认按只读放行。**
理由：未知即拒会让验收③失败（新工具免重启可用的前提是能被放行）。安全边界靠「写白名单＋Server 本地 `permission` 标记」守住：任何被本地标记为 write 的 Server，其工具不在白名单一律拒绝。

**C6 风险（实现时先做 spike 验证，不通就回来告诉你）：** `stream_mode="custom"` 下 LangGraph 的 interrupt 信息如何被 `astream` 吐出来，我还没在已装 1.2.18 上验过。已定兜底方案（不改选型）：`stream_mode=["custom", "updates"]`，从 `updates` 里取 `__interrupt__`，仍由 service 层翻成 `confirm` 帧。落进 §16 的第一条集成用例先探这个。
