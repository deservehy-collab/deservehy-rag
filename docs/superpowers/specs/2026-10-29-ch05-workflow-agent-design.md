# ch05 Workflow 编排 + 主 Agent 设计

> 状态：待评审
> 关联章节：ch02（工具链）、ch03/ch04（检索与重排）
> 目标读者：实现者、评审者

## 1. 目标

把现有「一次工具轮次 + 第二遍收敛」的对话服务升级为确定性 Workflow：图骨架负责分流与兜底，主 Agent 只作为骨架里的一个核心节点负责需要推理的回合。

验收行为：

1. 政策类问题一定先走强制检索节点，日志可见 `node=retrieve`。
2. 「订单 1001 的物流到哪了」由 Agent 自己决定调用哪些工具。
3. 「我要投诉」出现两个互相独立的前端按钮：转人工、建工单；转人工是前端模拟，建工单落 `tickets` 表；不点按钮继续聊天不受影响。
4. 纯闲聊走固定话术，不产生任何模型调用。
5. 需要先查订单再查物流的问题，Agent 至少两次工具往返。

## 2. 架构总览

```
START
  └─ coref（指代消解，透传：question → previous_question）
      └─ intent（确定性快速通道 或 LLM JSON 分类）
          └─ route（硬编码 7 意图 → 4 出口）
              ├─ knowledge（商品咨询 / 退款退货）
              │    └─ retrieve（强制检索，emit citations/status）
              │        └─ gate（low_confidence?）
              │            ├─ 低置信 → refuse（固定话术 + 入池）
              │            └─ 通过   → agent
              ├─ agent（物流 / 订单 / 售后，直接带工具）
              ├─ complaint（固定安抚话术 + 两个操作）
              └─ chitchat（固定话术，无模型调用）
                  └─ log（logger.info 结构化轨迹 + 落库 + emit done）
```

四个出口：`knowledge`、`agent`、`complaint`、`chitchat`。`refuse` 是 `knowledge` 的内部分支，不算第五个出口。

## 3. State 与检查点

### 3.1 State 字段

```python
class WorkflowState(TypedDict, total=False):
    conversation_id: int
    question: str
    previous_question: str | None
    messages: list[BaseMessage]          # 本轮工作副本，每轮覆盖
    intent: str                          # 七类之一
    route: str                           # knowledge/agent/complaint/chitchat
    retrieval: dict                      # {low_confidence, reason, canonical_query, strategy}
    evidence: list[dict]                 # 已编号的知识条目
    citations: list[dict]                # 前端来源抽屉数据
    reply: str
    actions: list[str]                   # human / ticket
    finish_reason: str | None
    steps: int                           # 本回合模型调用次数
    tool_calls: int                      # 本回合工具调用总次数
    tokens: int                          # 估算 token
    stopped_by: str | None               # max_steps / token_budget
    offer: dict | None                   # {turn, actions, ticket: {description,type}|None, ticket_no: str|None}
    turn_count: Annotated[int, operator.add]
    last_intent: str
    last_question: str
```

- 只有 `turn_count` 用 `operator.add` 累加，其余字段每轮覆盖。
- `messages` 是「本轮模型工作副本」：由 `prepare_chat()` 生成（system + 前端 history + 当前问题），不是服务端权威历史。权威历史仍由 MySQL `messages` 表和前端 history 负责，避免与既有协议重复。
- 跨轮真正有价值的状态：`turn_count`、`last_intent`、`last_question`、`offer`、`ticket_no`。验收时可用 `graph.get_state()` 打印。

### 3.2 检查点

- 生产：`AsyncSqliteSaver`（`langgraph-checkpoint-sqlite==3.1.2`），路径 `settings.workflow_checkpoint_path`，默认 `.cache/langgraph/ch05.sqlite`（`.cache/` 已被 `.gitignore` 覆盖）。
- 测试：路径 `":memory:"` 时用 `InMemorySaver`，不落文件。
- `thread_id = str(conversation_id)`；`durability="sync"`，保证操作按钮依赖的 `offer` 在回复结束前已经落盘。
- 每轮输入只带 `conversation_id / question / messages / actions=[]`；若上一轮异常中断留下 pending task，LangGraph 在新输入下会从 START 重新执行（本地已用探针验证）。
- 异步图统一用 `await graph.aget_state()` / `await graph.aupdate_state()` 读写检查点（AsyncSqliteSaver 不依赖同步 API）。

### 3.3 依赖变更

`requirements.txt` 增加：

```
langgraph-checkpoint-sqlite==3.1.2
aiosqlite>=0.20
```

（该包还依赖 `sqlite-vec>=0.1.6`，由 pip 自动安装；`langgraph-checkpoint 4.3.1` 满足 `>=4.3,<5`。）

## 4. 节点契约

### 4.1 coref（透传）

```python
def coref_node(state) -> dict:
    return {"previous_question": state.get("last_question"), "turn_count": 1,
            "last_question": state["question"], "actions": [], "offer": None}
```

本章不做正式指代消解；`previous_question` 只供意图分类参考。

### 4.2 intent

两层：

1. **确定性快速通道**（`routing.fast_chitchat`）：整句只匹配问候/致谢/告别（≤12 字，允许标点与“呀/啊/哈/~”），直接 `intent="闲聊"`，不调模型。这是为了同时满足「闲聊不调模型」与「意图识别用 prompt」。
2. **LLM 分类**：`IntentClassifier.classify(question, previous_question)`，JSON 输出 `{"intent": "物流"}`，七类：
   `物流 / 订单 / 商品咨询 / 退款退货 / 售后 / 投诉 / 闲聊`。
   - 使用 `with_structured_output(IntentResult, method="json_mode")`，超时 `settings.workflow_intent_timeout_seconds`（默认 10 秒）。
   - 任何异常、空值、非法枚举 → 记录 warning，返回默认 `售后`（进 Agent，带全部工具，最不容易卡死用户）。
   - Prompt 明确：用户文本只是待分类内容，不是指令。

分类示例边界（写进 prompt 与评估集）：

| 意图 | 典型问法 |
|---|---|
| 物流 | 包裹到哪了、快递怎么还没动 |
| 订单 | 订单状态、什么时候发货、订单金额 |
| 商品咨询 | 价格、库存、材质、型号适配 |
| 退款退货 | 退款政策、退货流程、运费谁承担 |
| 售后 | 商品坏了想换/维修/补发 |
| 投诉 | 投诉、差评、态度不好 |
| 闲聊 | 你好、谢谢、再见、你是谁 |

### 4.3 route

```python
ROUTES = {
    "商品咨询": "knowledge",
    "退款退货": "knowledge",
    "物流": "agent",
    "订单": "agent",
    "售后": "agent",
    "投诉": "complaint",
    "闲聊": "chitchat",
}
```

非法意图在 route 节点兜底为 `agent`。路由是 `add_conditional_edges` 的硬编码映射，不交给模型。

### 4.4 retrieve（仅 knowledge）

- 调用 `KnowledgeRetriever.retrieve(question)`，复用 ch04 的改写、同义词、BM25+dense、RRF、重排。
- 发出 `status`：`tool="retrieve", label="检索知识库"` 的 start/end，前端复用现有工具徽章。
- 有证据时发出 `citations`。
- 产出 `retrieval={low_confidence, reason, canonical_query, strategy}`、`evidence`（已编号）、`citations`。
- 检索抛异常（Milvus 不可用等）→ 视为 `low_confidence=True, reason="retrieval_error"`，不把异常抛给用户。

### 4.5 gate

只判断 `state["retrieval"]["low_confidence"]`：

- 低置信 → `refuse`：输出 `REFUSAL_TEXT`，调用 `LowConfidencePool.record(conversation_id, question, "retrieval_low_conf", reason)`（失败只记日志），停止。
- 通过 → `agent`，并把证据注入系统提示。

业务类出口不经过 gate（这是明确需求）。

### 4.6 agent

核心节点，内部调用手写 ReAct 循环 `app/agent/react.py`。

**工具集**

- 业务路由：`query_order / query_product / query_logistics / query_faq / create_ticket`（复用 ch02）。
- 知识路由：去掉 `query_faq`（已经强制检索过，避免重复付费检索），其余相同。

**系统提示**

- 基础：ch04 `chat_system`（含引用规则、负面知识、防注入）。
- 追加：
  - 信息足够就回答，不要空转；一次最多一个工具；不要重复相同调用。
  - 知识路由注入证据块（`【知识库证据】[n] 小节/问题/原文 …【证据结束】`），政策事实只能依据证据，并标 `[n]`；同时明确覆盖基础提示里的「先用 query_faq」——知识库已经检索完毕，本轮没有也不应再调该工具。
  - 若建议用户选择后续操作，在回答最后单独一行输出 `[[actions:human]]`、`[[actions:ticket]]` 或 `[[actions:human,ticket]]`。

**业务路由里的 query_faq（保留 ch04 语义）**

- `query_faq` 成功时：立刻发 `citations`，把编号集合并入 `CitationGuard`；若启用 `EvidenceSelfCheck`，按 ch04 做一次自评。
  - 自评不通过 → 记 `self_check` 入池，直接发 `REFUSAL_TEXT` 并结束本轮，不再发起下一次生成。
- `query_faq` 返回 `low_confidence` 时：记 `retrieval_low_conf` 入池，直接发 `REFUSAL_TEXT` 并结束本轮。
- 其它工具失败（未找到、超时、数据库不可用）：结果照常回灌，让 Agent 如实说明。
- 这里说的是显式知识查询的失败语义；业务路由仍然**不经过预检索闸门**（§4.5）。

**ReAct 循环接口**

```python
@dataclass
class ReActResult:
    reply: str
    finish_reason: str | None
    steps: int
    tool_calls: int
    tokens: int
    stopped_by: str | None

async def run_react(*, model, tools, executor, messages, on_token, on_status,
                    max_steps: int, token_budget: int) -> ReActResult
```

- 每步：`model.bind_tools(tools).astream(messages)`；正文经 `ReplyGuard`（以及知识路由的 `CitationGuard`）后交给 `on_token`。
- 有 tool_calls：
  - 单次响应 >5 个工具 → `ValueError`（沿用 ch02 的 `invalid_model_output`）。
  - 落库 assistant(tool_calls) 与每条 tool 结果；`on_status` 发 start/end。
  - 回到循环。
- 无 tool_calls：校验 `finish_reason ∈ {stop, length}`，结束；正文为空（且没有被 ReplyGuard 拦掉有效内容）→ 用 `FALLBACK_PLAIN` 作为回复，不发空消息。
- **停止条件**：
  - 正常收敛（无工具调用）。
  - `max_steps`（默认 4）次工具轮次后，再发一次**不绑定工具**的收敛调用，用已有工具结果作答。
  - 估算 token 超过 `token_budget`（默认 6000）→ 立即停止，输出 `BUDGET_TEXT`。
  - 模型/网络异常向上抛，由 service 统一映射为 `error`。
- token 估算复用 `app.context.estimate_tokens` + 已生成正文长度，明确标注为估算值。
- 每次模型调用套 `asyncio.timeout(settings.request_timeout_seconds)`。

**操作标记**

`ActionGuard` 负责：

- 流式拦截 `[[actions:...]]`，不允许泄漏到正文；
- 可能被拆成 `[[a`、`[[act` 的尾巴先扣住，flush 时再定夺；
- 解析出 `human / ticket`，去重；
- 标记必须从正文和落库文本中移除。

`[[` 之外的方括号（引用角标 `[1]`）不受影响。

**offer**

- `actions` 非空时写 `offer={turn: turn_count, actions, ticket: {description, type} | None, ticket_no: None}`。
- `ticket` 动作存在时：知识/业务路由的 `type` 按意图取 `售后` 或 `咨询`，描述为 `f"用户反馈：{question}"`；投诉路由固定 `投诉`，描述 `f"用户投诉：{question}"`。
- 无 actions 时 `offer=None`（新一轮开始时 coref 也会清空）。

### 4.7 complaint

- 不调模型、不检索、不进 Agent。
- `COMPLAINT_TEXT = "真的抱歉让你有这样的体验，你的感受我记下了。你可以选择转人工客服继续沟通，也可以先建一张工单，我们会尽快跟进处理。"`
- `actions = ["human", "ticket"]`，写入 offer（turn、描述、类型=投诉）。
- 两个按钮只是「建议」，后端不会自动建单或转接。

### 4.8 chitchat

- `CHITCHAT_TEXT = "你好呀，我是小邻客服。订单、物流、退换货、店铺政策都可以问我。"`
- 不调模型、不检索、不产生 offer。

### 4.9 refuse

- 输出 `REFUSAL_TEXT`，落低置信池，`finish_reason="stop"`，无 actions。

### 4.10 log

- `logger.info("workflow node=log conv=%s intent=%s route=%s low_conf=%s steps=%s tools=%s tokens=%s stopped=%s", …)`。
- 不记录用户原话，避免隐私写入日志。
- 写 assistant 最终回复到 `messages` 表（固定话术也写）。
- 通过 `get_stream_writer()` 发出 `done`（携带 `finish_reason`）。
- 因此 service 不再自己补 `done`；图跑完即协议完整。

## 5. SSE 协议

保持既有事件：`meta / status / citations / token / done / error`，新增：

```
event: actions
data: {"items":[{"id":"human","label":"转人工"},{"id":"ticket","label":"建工单"}],"turn":3}
```

- `meta` 仍由 service 在进入图之前发。
- 顺序：`meta → (status)* → citations? → token* → actions? → done`。
- `error` 仍由 service 捕获图执行异常后发，且不再发 `done`。

## 6. 前端

原生 HTML/CSS/JS，只加操作区，不改现有视觉语言。

- `actions` 事件到达后，在回答气泡下方渲染一行按钮，每个 id 一个按钮，互不影响。
- 转人工（纯前端）：
  - 点击后仅禁用「转人工」；
  - 追加一条系统提示「已转接人工客服」；
  - 追加一条名为「客服小猫」的助手消息：「您好，我是客服小猫，请问有什么可以帮您的？」；
  - 不调用后端、不写库。
- 建工单：
  - 点击后仅禁用「建工单」；
  - `POST /api/tickets`，body `{conversation_id, client_id, turn}`；
  - 成功显示「工单 T… 已创建，客服会尽快跟进。」；失败提示并恢复按钮；
  - 不触发转人工。
- 两个都不点、继续输入：正常进入下一轮，新一轮 offer 覆盖旧 offer。
- 按钮出现在满意度反馈之前；反馈仍保持在最下方。

## 7. 接口

### POST /api/tickets

请求：

```json
{"conversation_id": 12, "client_id": "abc12345", "turn": 3}
```

校验顺序：

1. Pydantic：`conversation_id >= 1`，`client_id` 8–64 位 `[A-Za-z0-9_-]`，`turn >= 1`。
2. `ConversationStore.owner(conversation_id)` 必须等于 `client_id`，否则 403 `forbidden`。
3. `(await workflow.get_state(thread_id)).values["offer"]` 必须存在、`offer["turn"] == turn`、`"ticket" in offer["actions"]`，否则 409 `no_pending_offer`。
4. `offer["ticket_no"]` 已存在 → 直接返回它（幂等）；否则在按会话的进程内锁里，通过 `make_tools(store, conversation_id, knowledge)` 取 ch02 的 `create_ticket` 工具，用 `ToolExecutor.execute("create_ticket", {...})` 建单（写工具不重试），成功后用 `await workflow.update_state(...)` 回写 `ticket_no`。
   - 描述 = `offer["ticket"]["description"]`，超过 500 字截断；不足 5 字时补足前缀；类型取 `offer["ticket"]["type"]`。
   - 工具返回失败 → 502/503，按 `outcome.code` 映射，不透出内部细节。

响应：`{"ticket_no": "T20261030001", "status": "待处理"}`。

数据库异常 → 503 `database_unavailable`。重复点击、旧轮次按钮、别人的会话都不会造出第二张工单。

> 已知限制：幂等锁是单进程锁；多 worker 部署时只能依赖前端禁用 + 轮次校验，仍可能有极小概率重复建单。本章不引入新的幂等表。

## 8. 配置

```python
workflow_checkpoint_path: str = ".cache/langgraph/ch05.sqlite"
workflow_agent_max_steps: int = Field(default=4, ge=1, le=8)
workflow_agent_token_budget: int = Field(default=6000, ge=1000, le=60000)
workflow_intent_timeout_seconds: float = Field(default=10, gt=0, allow_inf_nan=False)
```

`.env.example` 同步；`.env` 不动。

## 9. 日志与可观测性

- 每个节点入口：`logger.info("workflow node=%s conv=%s", name, conversation_id)`。
- log 节点汇总：意图、路由、置信结果、步数、工具数、估算 token、停止原因、耗时。
- 验收 1 直接检查 uvicorn 输出里的 `node=retrieve`。
- 不打印用户原话、不打印密钥、不打印上游请求体。

## 10. 测试与评估

### 10.1 单测（pytest）

- `run_react`：单轮收敛、两轮工具、超过 max_steps 后强制收敛、token 预算停止、工具超时仍继续、非法 finish_reason。
- `ActionGuard`：完整标记剥离、拆分标记扣留、引用角标不误伤、重复动作去重。
- `routing`：七意图映射、未知意图兜底、快速闲聊通道（含“你好，我要退货”不能误判）。
- 图：知识路由低置信不进 Agent、业务路由不进 retrieve、投诉两个动作、闲聊（快速通道）上游调用为 0、其余闲聊仅一次意图调用且零生成调用、turn_count 跨轮累加、offer 每轮覆盖、业务路由 `query_faq` 的 citations 仍会出现、自评拒绝时不再发起下一次生成。
- API：四类路由的 SSE 事件序列；client 断开时上游关闭；错误映射不回退成成功。
- 工单接口：归属 403、无 offer 409、旧轮次 409、幂等、写表成功。

测试注入：

- `create_app(..., classifier=None)`；测试传 `FixedClassifier("售后")` 让既有用例继续走 Agent。
- 默认 `workflow_checkpoint_path=":memory:"`，避免测试落盘污染。

### 10.2 意图评估（非单测，标注集）

- `evals/intent_cases.jsonl`：每类至少 5 条，另加 5 条快速通道与 3 条歧义句，共 ≥38 条，字段 `{"text","intent","note"}`。
- `scripts/evaluate_intent.py` 用真实模型逐条分类，输出总准确率、每类准确率、错例；写 `evals/reports/ch05-intent-<时间戳>.md`。
- 门槛：总体 ≥ 0.90；低于门槛先改 prompt/示例，不改路由。

### 10.3 真实验收（demo）

- `python -m scripts.demo_agent_loop`：裸 ReAct 两步工具（祛魅热身）。
- `python -m scripts.demo_workflow`：跑 5 条验收场景，打印 `node=` 轨迹与回复摘要。
- 浏览器：投诉两个按钮、转人工文案、建工单落库、互不影响。

## 11. 错误处理与安全

- 意图分类失败 → 默认「售后」进 Agent，不阻断。
- 检索失败 → 拒答 + 入池，不 500。
- Agent 上游失败 → 既有 `model_failure` 映射，SSE `error`，不发 `done`。
- 检查点写失败 → 向上抛，service 返回 `database_unavailable`（会话状态存储不可用，不静默成功）。
- 工具只读重试、写工具不重试的规则沿用 ch02。
- 前端不能直接传工单描述/类型，描述由服务端根据当轮问题生成。
- 不新增业务工具、不新增数据表。

## 12. 不做

- 正式指代消解（本章透传）。
- 上下文管理策略升级、MCP 接入、飞轮入库。
- 真正的转人工系统、客服排队、坐席分配。
- 多 worker 级别的工单幂等表。
- 观测平台（Langfuse 等）。

## 13. 文件结构

```
app/agent/react.py                 手写 ReAct 循环（可独立测试与 demo）
app/workflow/state.py              WorkflowState
app/workflow/routing.py            七意图、四出口、快速闲聊通道
app/workflow/classifier.py         IntentClassifier
app/workflow/actions.py            ActionGuard、offer 构造
app/workflow/input_nodes.py        coref / intent / route
app/workflow/knowledge_node.py     retrieve / gate / refuse
app/workflow/agent_node.py         Agent 节点（包装 run_react）
app/workflow/reply_nodes.py        complaint / chitchat / log
app/workflow/graph.py              build_graph + Workflow 运行器
app/prompts.py                     意图 prompt、Agent 追加提示、固定话术
app/config.py, app/schemas.py, app/storage.py, app/service.py, app/main.py
app/web/index.html, app/web/chat.js, app/web/chat.css
scripts/demo_agent_loop.py, scripts/demo_workflow.py, scripts/evaluate_intent.py
evals/intent_cases.jsonl
tests/test_react.py, tests/test_workflow.py, tests/test_workflow_api.py, tests/test_tickets.py
```

## 14. 验收映射

| 验收 | 实现点 | 证据 |
|---|---|---|
| 政策问题强制检索 | knowledge 路由 → retrieve 节点 | 日志 `node=retrieve`、`citations` 事件 |
| 订单 1001 物流由 Agent 调工具 | 意图=物流 → agent | `status` 工具徽章 |
| 投诉两个独立按钮 | complaint → actions | `actions` 事件 + 两按钮 |
| 转人工文案 | 前端模拟 | 「已转接人工客服」+「客服小猫」问候 |
| 建工单写表 | POST /api/tickets | `tickets` 新增行 |
| 闲聊固定话术、零模型 | 快速通道 + chitchat 节点 | 「你好」上游调用计数为 0 |
| 多步 ReAct | run_react 多轮 | demo 打印 2 次工具往返 |

## 15. 参考（Context7 核对记录）

本机未注册 Context7 MCP，直接请求 `https://mcp.context7.com/mcp`（与 ch04 相同方式）：

- `resolve-library-id(libraryName="langgraph", query=...)` → `/websites/langchain_oss_python_langgraph`。
- `query-docs` 关键结论：
  - `StateGraph(State).add_node/add_conditional_edges/compile(checkpointer=)`；`graph.get_state(config)`、`graph.update_state(config, values)`，`thread_id` 必填。
  - `astream(..., stream_mode="custom", version="v2")` + `get_stream_writer()` 可在节点内发自定义事件；`stream_mode="messages"` 返回 `(chunk, metadata)`，可按 `langgraph_node` 过滤。
  - `durability` 取 `exit / async / sync`。
  - `langgraph-checkpoint-sqlite` 需单独安装，提供 `SqliteSaver / AsyncSqliteSaver`；异步图用 `AsyncSqliteSaver`。
- 本地实测：`langgraph 1.2.18`、`langgraph-checkpoint 4.3.1`；自定义事件与跨轮状态累加均通过；节点抛错后同 thread 新输入可从 START 重跑。
