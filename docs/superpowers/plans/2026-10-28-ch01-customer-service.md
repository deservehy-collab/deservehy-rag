# ch01 电商智能客服 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付本地可运行、可 curl 验收的流式多轮客服与售后结构化提取 API。

**Architecture:** 无状态 FastAPI 接收当前问题和完整历史，经 PromptTemplate 与预算裁剪交给 ChatOpenAI。只使用 OpenAI Chat Completions 直连和 json_mode，不使用工具或 Agent。代码由主执行者编写，独立审查者评审计划及最终关键路径。

**Tech Stack:** Python 3.12、FastAPI 0.162.0、LangChain 1.4.7、langchain-openai 1.7.3、Pydantic v2、pydantic-settings、HTTPX、Uvicorn、pytest。

**Spec:** docs/superpowers/specs/2026-10-28-ch01-customer-service-design.md

## Global Constraints

- 指定技术栈不变；发现真正矛盾或不可实现的要求才询问用户，不为普通细节和阶段交接重复暂停。
- Context7 MCP 已验证可经 HTTPS JSON-RPC 直接调用；每个具体库/API 在使用前查询，安装后核对实际版本与签名。
- 代码 TDD；纯 Prompt/数据用标注样例评估，不把关键词存在性测试当成模型行为验收。
- 每个任务完成、计划评审通过、code review 与 finish 立即追加 dev-notes/ch01.md 四项记录。
- 输入上限 12000 字符；history 最多 100 条，只允许完整 user/assistant 轮次。
- 默认上下文 8192、输出 1024、安全余量 512；估算器为 UTF-8 字节长度加消息协议开销，不声称精确计费 token。
- 必需配置只在 .env/环境变量：OPENAI_BASE_URL、OPENAI_MODEL、OPENAI_API_KEY。真实密钥不出现在文档、测试输出或版本控制中。
- SSE 是真实上游 delta 的实时转发；错误流不能再发送 done。提取失败返回 502/504，不伪造 JSON、不自动换协议。
- 仅 localhost 本地运行，不部署、不改 App 设置、不创建 git 提交；当前目录无 Git 仓库，不为 worktree 初始化无关仓库。
- 模型真实验收缺凭据时如实保留未完成状态，不以模拟上游通过代替真实模型。

## 文件与职责

- `requirements.txt`：直接依赖；`requirements.lock.txt`：通过安装验证的完整精确版本。
- `pyproject.toml`：pytest 路径配置；`.gitignore`：排除 .env、.venv、缓存。
- `.env.example` / `.env`：无密钥模板与本地待填配置；不覆盖用户已有 .env。
- `app/config.py`：Settings.from_env()、预算和必需配置校验。
- `app/schemas.py`：HistoryMessage、ChatRequest、ExtractRequest、AfterSales。
- `app/context.py`：estimate_tokens()、build_messages()、ContextBudgetError。
- `app/prompts.py`：客服与提取 PromptTemplate；生成 system messages。
- `app/model.py`：build_model() 唯一 OpenAI 协议模型工厂。
- `app/service.py`：CustomerService，流式与 structured output；不做 HTTP 路由。
- `app/main.py`：create_app()、lifespan、API 路由、错误映射和 SSE 编码。
- `tests/test_core.py`：配置、请求、预算行为。
- `tests/test_api.py`：真实 ChatOpenAI + HTTPX 模拟传输协议测试。
- `tests/test_streaming.py`：真实本地 TCP 服务的首块、断开、异常验证。
- `scripts/demo.py`：连接实际应用的两轮对话及提取演示，输出实时事件。
- `scripts/evaluate.py`：真实上游标注集评估与非零失败退出。
- `evals/after_sales.json`、`evals/chat.json`：合成的标注样例，不含真实用户数据。
- `examples/chat.json`、`examples/extract.json`：UTF-8 curl 请求文件，避开 Windows 中文命令行编码问题。
- `start.bat`、`test.bat`：可双击的本地运行/测试入口。

## Task 1：配置、输入模型和上下文预算

**Interfaces**

- `Settings.from_env() -> Settings` 从绝对项目路径 .env 读取；`settings.input_budget: int` 计算预算。
- `HistoryMessage(role: Literal['user','assistant'], content: str)`。
- `ChatRequest(message: str, history: list[HistoryMessage] = [])`。
- `ExtractRequest(description: str)`。
- `AfterSales(order_id: str | None, request_type: Literal['refund','return_refund','exchange','repair','logistics','other','unknown'], expected_solution: str | None)`；字段全部必需、禁止额外字段。
- `estimate_tokens(messages: list[BaseMessage]) -> int`。
- `build_messages(system: str, request: ChatRequest, budget: int) -> list[BaseMessage]`；超出最小输入预算抛 `ContextBudgetError`。

- [x] 创建本地 .venv，安装直接依赖，记录精确版本；不改全局环境。
- [x] 先写 tests/test_core.py，覆盖空输入、角色、完整轮次、中文/多字节、边界和当前问题不可裁掉：

```python
def test_trims_whole_oldest_turn():
    request = ChatRequest(message="now", history=[
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "answer"},
    ])
    minimum = [SystemMessage(content="rules"), HumanMessage(content="now")]
    result = build_messages("rules", request, estimate_tokens(minimum))
    assert result == minimum


def test_current_question_cannot_be_removed():
    with pytest.raises(ContextBudgetError):
        build_messages("rules", ChatRequest(message="now"), 1)
```

- [x] 运行 `.venv/Scripts/python -m pytest tests/test_core.py -q`，记录模块/实现尚不存在的红灯。
- [x] 实现 config.py、schemas.py、context.py；采用以下整轮裁剪核心：

```python
while estimate_tokens(messages) > budget and len(messages) > 2:
    del messages[1:3]
if estimate_tokens(messages) > budget:
    raise ContextBudgetError("当前问题超过上下文预算，请缩短后重试。")
```

- [x] 重跑核心测试，验证 .env 可从其他工作目录加载、SecretStr 不泄漏、非法预算被拒绝，追加任务完成日志。

## Task 2：真实流式接口和结构化提取

**Interfaces**

- `chat_system(store_name: str) -> str`、`extraction_system() -> str` 来自受控 PromptTemplate。
- `build_model(settings: Settings, *, async_client: httpx.AsyncClient | None = None) -> ChatOpenAI`。
- `CustomerService(settings: Settings, model: ChatOpenAI)`。
- `CustomerService.prepare_chat(request: ChatRequest) -> list[BaseMessage]`。
- `CustomerService.stream_chat(messages: list[BaseMessage]) -> AsyncIterator[tuple[str, dict]]`。
- `CustomerService.extract(request: ExtractRequest) -> AfterSales`。
- `create_app(settings: Settings | None = None, model: ChatOpenAI | None = None) -> FastAPI`；默认在 lifespan 读取配置，测试可注入真实 ChatOpenAI 的 HTTPX mock transport。

- [x] 写 HTTPX MockTransport 测试，让模型仍经过真实 LangChain/OpenAI 序列化；断言以下线协议而不是只测一个假模型：

```python
assert request.url.path == "/v1/chat/completions"
body = json.loads(request.content)
assert "tools" not in body and "tool_choice" not in body
assert body["model"] == "test-model"
assert body["max_completion_tokens"] == 1024
assert body.get("response_format") == {"type": "json_object"}
```

聊天路径另断言 `stream is True`、无 response_format，历史内容及顺序准确，两个文本块得到两个 token 事件。

- [x] 运行 `.venv/Scripts/python -m pytest tests/test_api.py -q`，先看到缺少接口/服务的红灯。
- [x] 实现 PromptTemplate、工厂、service、main；模型关键配置与结构化选择：

```python
model = ChatOpenAI(
    model=settings.openai_model,
    base_url=str(settings.openai_base_url),
    api_key=settings.openai_api_key,
    max_tokens=settings.max_output_tokens,
    timeout=settings.request_timeout_seconds,
    max_retries=0,
    use_responses_api=False,
    stream_usage=False,
    temperature=None,
    http_async_client=async_client,
)
structured = model.with_structured_output(
    AfterSales, method="json_mode", include_raw=True
)
```

在调用提取前将 extraction_system 与用户描述作为完整消息参与预算；Pydantic Schema 是 system 内容的一部分，计入预算。

- [x] 流式 async for 转发 delta，捕获 finish_reason，finally 关闭生成器；无文本、缺完成标记或异常输出 error，不输出成功 done；stop/length 正常终止均输出一次 done。
- [x] 提取用 raw 的 finish_reason 拒绝截断、拒绝 parsing_error；验证后的 AfterSales 才能成为成功 HTTP 响应。
- [x] 映射超时 504、上游/解析失败 502、输入/预算 422；流启动后的异常使用 SSE error；不回传异常 repr、上游地址或密钥。
- [x] lifespan 关闭自建模型的同步和异步客户端；注入测试模型由测试拥有；无 .env 时给出不含值的配置缺失说明。
- [x] 测试上游 401/429、超时、首块前/中途异常、JSON 缺字段/错类型/额外字段/length、非法输入和历史裁剪；全绿后立即记录任务完成。

## Task 3：真实 TCP 验证、演示和 Prompt 标注评估

**Interfaces**

- `scripts.demo` 默认访问 `http://127.0.0.1:8000`，用上一轮实际收到的回答生成第二轮历史，再提取售后 JSON；异常非零退出。
- `scripts.evaluate` 从项目 .env 加载当前模型，用同一 service 执行 evals；记录每字段和全字段准确率。客服约束启发式检查不冒充完整语义评审；输出正文供人工核对，测试样例全为合成数据。

- [x] 先写 test_streaming.py，用本地 Uvicorn + 可控 MockTransport 的 AsyncByteStream：上游发第一块后等待 threading.Event，测试端收到第一块后才允许发送余下部分，从而排除整段缓冲。

```python
with httpx.Client(timeout=5) as client:
    with client.stream("POST", base_url + "/api/chat", json={"message": "hello"}) as response:
        lines = response.iter_lines()
        assert next(lines) == "event: token"
        assert json.loads(next(lines)[6:])["content"] == "first"
        release_second_chunk.set()
```

- [x] 对客户端关闭连接断言上游流 finally 被触发；对上游中途失败断言已收到首块、随后 error、没有 done。
- [x] 运行补充 TCP 集成测试；三项在现有实现上直接通过，无需修改正确的取消逻辑，不伪造红灯。使用本机临时端口，不停止用户已有服务。
- [x] 编写合成售后标注集：退款、退货退款、换货、维修、物流、缺订单、缺方案、否定诉求、前导零、注入、冲突。期望字段值完整列出；同义表达采用可接受值集合，不因合理同义词误判。
- [x] 编写客服标注集：多轮事实、无工具能力边界、无店铺政策时不承诺、隐私输入、指令注入；评估器标出每条原始回答供核查。
- [x] 实现 demo/evaluate，测试失败退出语义、第二轮历史来源与字段评分函数；纯标注数据检查形状不是行为评估通过。
- [x] 创建 UTF-8 请求样例和 start.bat/test.bat；.env 不存在时只创建空配置，不替用户选择付费上游或下载本地模型。
- [x] 运行全测试、pip check、compileall；启动服务器，在真实 TCP 链路运行 curl -N。用 mock 上游时输出明确标注模拟；实际凭据就绪才做真实模型验收。
**已完成（真实 DeepSeek 上游）：** 用户提供本项目密钥后，实测上游在售模型为 deepseek-flash / deepseek-v4-pro（旧 deepseek-chat 已退役但仍作非思考别名）；选用 deepseek-flash 并以 OPENAI_EXTRA_BODY 关闭思考模式。真实流式首块 0.93 秒、87 个增量；scripts.demo 两轮与提取通过；scripts.evaluate 12 条提取三字段准确率各 1.0、全字段 1.0，4 条客服场景启发式筛查全过，仍需按 review_criteria 人工核对语义。
- [x] 追加本地任务产物与测试证据，然后交独立 code review；修复所有必须修项后复跑测试，调用 verification-before-completion 与 finishing-a-development-branch。无 Git 时只交付工作目录，不制造提交或 PR。

## 计划自审与验收映射

- spec §1–4、§8 → Task 1 的配置、输入与预算，加 Task 2 的工厂。
- spec §5–7 → Task 2 的两个接口、模板和异常语义。
- spec §9 → Task 1/2 红绿测试、Task 3 实际 TCP 与真实模型/Prompt 评估。
- spec §10–11 → 每任务日志、独立计划/代码评审、Context7 查询及最终证据。
- 采用 inline execution，避免每个小任务新建实现代理；保留独立计划评审和代码终审，不再让用户选一次执行模式。
