# ch10 多标签主题分类器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用飞轮低置信问题与 LLM 合成语料，全参微调 RoBERTa-wwm-ext 多标签主题分类器，经旁路推理服务批量归类并落 `topic_classifications`，在 `/admin/topics` 展示 17 类主题分布。

**Architecture:** 数据构建（清洗 → 生成/预标/抽审 → 去重 → 分层切分 → 只增强训练集）与训练/评测离线完成；推理服务是独立 FastAPI 进程（默认 CPU），主应用只通过 HTTP 调它，后台定时器按待归类数量触发批量归类；聊天主链路永不调用分类器。

**Tech Stack:** Python 3.12、torch 2.11.0+cu128、transformers 5.21.0、scikit-learn 1.9.1、FastAPI、SQLAlchemy 2.1.6、APScheduler、MySQL 8/SQLite。

**Spec:** `docs/superpowers/specs/2026-11-02-ch10-topic-classifier-design.md`

## Global Constraints

- 17 类固定顺序：退换货、物流、尺码、发票、质量问题、运费、优惠活动、价保、支付、订单修改、库存补货、商品信息、保修维修、账号、会员积分、评价、其他。
- 多标签判定只按字面诉求；`买大了想退` → `["退换货", "尺码"]`；全部低于阈值回落 `["其他"]`。
- 合成语料 1200 条：其中 300 条多标签来自固定 10 个组合各 30 条；17 类每类的**标签支持数下限**为 16×70 + 其他 80（配额）。300 条多标签贡献 600 个标签位后，先补每类配额缺口（合计 600 条），再按类目轮转补齐 300 条单标签到 1200 条总量。配额是下限，不是精确总数——若坚持「标签位总和=1200」则总条数只能是 900（本计划已裁定以 1200 条总量为准）。
- 分层切分 80/10/10，seed=42；只对合成语料分层，真实问题去重后追加训练集；增强只作用于训练集。
- 模型固定 `hfl/chinese-roberta-wwm-ext`，**全参微调**；不做 LoRA/QLoRA、不微调 embedding、不微调意图识别。
- 推理服务默认 CPU、端口 8933；ONNX 不在本章范围。
- 聊天主链路（`app/workflow`、`app/agent`、`app/service`）不得 import `app.topics.model` 或 `app.topics.service`。
- 表结构以用户 DDL 为准：`topic_classifications(question_id UNIQUE, labels JSON, classified_at)`，FK 指向 `low_confidence_questions.id`。
- 真实 DB 文本、密钥、`.env`、日志、模型权重不入库；`data/topics/real_*.csv/jsonl` 必须 git-ignored。
- 每完成一个阶段（计划评审通过、每个任务完成、code review 结论、finish）向 `dev-notes/ch10.md` 追加四样：用户关键原话 / 关键产出 / 用户拒绝或纠偏 / 翻车与返工；不许收尾一次性补记。
- 具体库/框架用法先查 Context7；本会话无 Context7 时读已安装包源码，并在 `dev-notes/ch10.md` 写明「读了哪个文件、哪一行」。
- 测试统一跑：`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q`。
- 不为演示好看改判据；训练/数据任务用标注样例或评估集验证替代 TDD；前端 Vibe Coding，不套 TDD/code review。
- 不改全局 git config；推送 GitHub 前先告诉用户开代理；最终一次提交标题：`第十次提交（ch10）：多标签主题分类器与主题分布`。

---

## File Structure

| 文件 | 责任 |
|---|---|
| `app/topics/__init__.py` | 空包标记；**不** import torch/transformers/FastAPI |
| `app/topics/taxonomy.py` | 17 类标签、边界说明、近邻规则、排序与校验；全仓唯一事实源 |
| `app/topics/clean.py` | 脱敏与文本归一（NFKC、零宽、空白、标点、PII） |
| `app/topics/prompts.py` | 生成、独立预标、改写、错别字清洗四组 Prompt |
| `app/topics/dataset.py` | `Sample`、JSONL 读写、去重、迭代分层切分、增强、抽审合并、组装 |
| `app/topics/model.py` | 加载微调模型与阈值、`apply_thresholds`、批量预测 |
| `app/topics/service.py` | 旁路 FastAPI 推理服务（`/health`、`/classify`） |
| `app/topics/worker.py` | 待归类查询、`TopicClient`、批量归类、upsert `topic_classifications` |
| `app/topics/scheduler.py` | 进程内定时触发 `run_batch`（APScheduler） |
| `app/topics/stats.py` | 主题分布统计（供后台接口） |
| `app/topics/evaluation.py` | 逐类指标、2×2 混淆、17×17 共现、Markdown/HTML 渲染 |
| `scripts/build_topic_dataset.py` | 生成/预标/改写/清洗/抽审/切分 CLI，落盘所有语料与统计报告 |
| `scripts/train_topic_classifier.py` | 手写全参微调循环、早停、阈值调优、产物与训练报告 |
| `scripts/evaluate_topic_classifier.py` | 测试集评测、真实抽检、多标签用例、判错导出 |
| `scripts/classify_topics.py` | 手动批量归类（与调度共用 `run_batch`） |
| `scripts/demo_topics.py` | 三条验收一次跑完 |
| `scripts/migrate_ch10.py` | 幂等补建 `topic_classifications` |
| `scripts/init_sql/008_topics.sql` | 用户给定 DDL 原样 |
| `app/web/admin-topics.html` / `.js` | 主题分布页（Vibe Coding） |
| `app/web/admin.html` / `admin.css` | 加导航链接与图表样式 |
| `start-topic.bat` | 双击启动推理服务（ASCII + CRLF） |
| `.gitignore` | Task 8 起忽略 `data/topics/real_*`，防止 DB 原文入库 |
| `tests/test_ch10_*.py` | 各单元契约测试 |
| `data/topics/README.md` | 语料字段与重建命令 |
| `evals/topics/multilabel_cases.jsonl` | 验收③固定用例 |

**依赖方向**：`dataset/prompts/clean → taxonomy`；`model → taxonomy`；`service → model`；`worker/stats → taxonomy + models`；`scheduler → worker`；`main.py → worker/scheduler/stats`。`worker`、`stats`、`scheduler` 不得 import torch。

## 已核实的库事实（写代码前不要再猜）

| 事实 | 证据 |
|---|---|
| `BertForSequenceClassification`、`get_linear_schedule_with_warmup` 在 transformers 5.21 可导入 | 本机 `python -c` 探针 2026-11-02 |
| `hfl/chinese-roberta-wwm-ext` 在 `https://hf-mirror.com` 返回 200，含 `config.json`/`pytorch_model.bin`/`vocab.txt`/`tokenizer.json` | `curl` API 探针 |
| torch 2.11.0+cu128，RTX 4050 Laptop 6GB，cc 8.9，支持 bf16 | `torch.cuda.get_device_properties` |
| 未安装 `accelerate`/`datasets`/`onnx`/`onnxruntime`/`matplotlib` | `importlib` 探针 |
| `IdType = mysql.BIGINT(unsigned=True).with_variant(Integer, "sqlite")` | `app/models.py:18` |
| `JSON` 列在 MySQL/SQLite 均可直接用；`TABLE_OPTIONS` 统一 InnoDB/utf8mb4 | `app/models.py:4-22` |
| `EvalScheduler` 用 `AsyncIOScheduler` + `asyncio.to_thread`，`shutdown` 捕获 `SchedulerNotRunningError` | `app/knowledge/scheduler.py:45-91` |
| SQLite 测试夹具 `sqlite_factory()` 建全表并 seed FAQ | `tests/support.py:116-126` |
| `_settings()` 默认关闭后台任务，新增键要同步加到默认值 | `tests/support.py:80-105` |
| 真实池 17 行 / 13 个不同问法，样例覆盖约 10 类 | 本机 MySQL 只读查询 |

---

## Task 1: 配置、依赖与 17 类术语表

**Files:**
- Create: `app/topics/__init__.py`
- Create: `app/topics/taxonomy.py`
- Modify: `app/config.py`（在 `flywheel_dedupe_candidates` 之后加 ch10 字段）
- Modify: `tests/support.py`（`_settings` 默认值加 `topic_classify_enabled=False`）
- Modify: `requirements.txt`（显式 pin transformers）
- Modify: `.env.example`（加 ch10 七个键）
- Test: `tests/test_ch10_taxonomy.py`
- Test: `tests/test_ch10_config.py`

**Interfaces:**
- Produces: `LABELS: tuple[str, ...]`（17 个，顺序固定）、`DESCRIPTIONS: dict[str, str]`、`BOUNDARIES: tuple[str, ...]`、`OTHER: str`、`order_labels(labels) -> list[str]`、`validate_labels(labels) -> list[str]`。
- Produces: `Settings.topic_classify_enabled`、`topic_service_url`、`topic_classify_interval_minutes`、`topic_classify_min_pending`、`topic_classify_batch`、`topic_model_dir`、`topic_device`、`topic_max_len`。

- [ ] **Step 1: Context7 检查（pydantic-settings/transformers 用法）**

运行：

```bash
# 本会话没有 Context7 工具时，记录到 dev-notes 后改读已装包源码
python -c "import mcp" 2>/dev/null || true
```

若 `mcp_list keyword=context7` 为空，在 `dev-notes/ch10.md` 记：「Context7 不可用；pydantic-settings 按 `app/config.py` 现有写法，transformers 版本事实见计划表」。**不要凭记忆加参数。**

- [ ] **Step 2: 写失败的术语表测试**

创建 `tests/test_ch10_taxonomy.py`：

```python
import pytest

from app.topics.taxonomy import BOUNDARIES, DESCRIPTIONS, LABELS, OTHER, order_labels, validate_labels


def test_labels_are_17_unique_in_fixed_order():
    assert len(LABELS) == 17 == len(set(LABELS))
    assert LABELS[:3] == ("退换货", "物流", "尺码")
    assert LABELS[-1] == OTHER == "其他"
    assert set(DESCRIPTIONS) == set(LABELS)
    assert all(DESCRIPTIONS[label].strip() for label in LABELS)


def test_boundaries_cover_user_rules_verbatim():
    text = "".join(BOUNDARIES)
    for phrase in ("修归保修维修", "退归退换货", "运费管钱", "物流管货",
                   "价保是补差价", "优惠活动是券和满减"):
        assert phrase in text


def test_order_labels_dedupes_and_follows_taxonomy_order():
    assert order_labels(["尺码", "退换货", "尺码"]) == ["退换货", "尺码"]
    assert order_labels([]) == []


def test_validate_labels_rejects_empty_and_unknown():
    with pytest.raises(ValueError):
        validate_labels([])
    with pytest.raises(ValueError, match="未知主题标签"):
        validate_labels(["退款"])
    assert validate_labels(["评价", "退换货"]) == ["退换货", "评价"]


def test_multilabel_policy_example_is_expressible():
    assert order_labels(["退换货", "尺码"]) == ["退换货", "尺码"]
```

运行：

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_ch10_taxonomy.py
```

预期：`ModuleNotFoundError: No module named 'app.topics'`。

- [ ] **Step 3: 写失败的配置测试**

创建 `tests/test_ch10_config.py`：

```python
import pytest
from pydantic import ValidationError

from app.config import Settings


def make(**overrides):
    base = {"openai_base_url": "http://upstream.invalid/v1", "openai_model": "m",
            "openai_api_key": "k"}
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_topic_defaults():
    settings = make()
    assert settings.topic_classify_enabled is False
    assert settings.topic_service_url == "http://127.0.0.1:8933"
    assert settings.topic_classify_interval_minutes == 10
    assert settings.topic_classify_min_pending == 20
    assert settings.topic_classify_batch == 50
    assert settings.topic_model_dir == ".cache/models/topic-classifier"
    assert settings.topic_device == "cpu"
    assert settings.topic_max_len == 128


def test_topic_bounds_reject_bad_values():
    for field, value in (("topic_classify_interval_minutes", 0),
                         ("topic_classify_min_pending", 0),
                         ("topic_classify_batch", 0),
                         ("topic_classify_batch", 201),
                         ("topic_max_len", 16),
                         ("topic_max_len", 257),
                         ("topic_device", "mps")):
        with pytest.raises(ValidationError):
            make(**{field: value})
```

运行：两项都失败（`AttributeError`/`ValidationError` 未按预期）。

- [ ] **Step 4: 实现 `app/topics/__init__.py` 与 `taxonomy.py`**

`app/topics/__init__.py` 只留一行 docstring：

```python
"""客服系统主题分类：术语表、数据构建、训练、推理与后台统计。"""
```

`app/topics/taxonomy.py`：

```python
from collections.abc import Iterable

LABELS: tuple[str, ...] = (
    "退换货", "物流", "尺码", "发票", "质量问题", "运费", "优惠活动", "价保", "支付",
    "订单修改", "库存补货", "商品信息", "保修维修", "账号", "会员积分", "评价", "其他",
)

DESCRIPTIONS: dict[str, str] = {
    "退换货": "退货、换货、退款的条件与流程；「退」归这里。",
    "物流": "包裹运输、配送进度、签收异常；管货。",
    "尺码": "尺码表、偏大偏小、按身高体重选码、买大买小。",
    "发票": "开票、发票类型、抬头、电子发票。",
    "质量问题": "到货破损、瑕疵、变质、功能故障等商品本身的问题。",
    "运费": "运费金额、包邮门槛、运费由谁承担；管钱。",
    "优惠活动": "优惠券、满减、折扣、活动规则。",
    "价保": "下单后降价、补差价。",
    "支付": "付款方式、支付失败、扣款异常。",
    "订单修改": "未发货前改地址/改商品/取消/合并订单。",
    "库存补货": "缺货、何时补货、到货提醒。",
    "商品信息": "参数、材质、颜色、保质期、适用人群等属性。",
    "保修维修": "保修期、维修流程、换配件；「修」归这里。",
    "账号": "登录、注册、密码、账号安全。",
    "会员积分": "积分获取与使用、会员等级、权益。",
    "评价": "评价入口、修改/删除评价、差评处理。",
    "其他": "无具体诉求、闲聊、纯订单号/单号、无法归入以上。",
}

BOUNDARIES: tuple[str, ...] = (
    "修归保修维修、退归退换货；一句话同时提到就都打。",
    "运费管钱、物流管货：「运费怎么算」→ 运费；「几天能到」→ 物流。",
    "价保是补差价、优惠活动是券和满减：「刚买就降价」→ 价保；「有什么满减」→ 优惠活动。",
    "尺码 vs 商品信息：选码/大小 → 尺码；颜色/材质/参数 → 商品信息。",
    "库存补货 vs 商品信息：有没有货/何时到 → 库存补货；商品属性 → 商品信息。",
    "订单修改 vs 退换货：发货前改信息 → 订单修改；退货退款 → 退换货。",
    "发票 vs 支付：开票/抬头 → 发票；付款失败/扣款 → 支付。",
    "质量问题 vs 保修维修：到货即坏/破损/变质 → 质量问题；用了一段时间坏了要修 → 保修维修；字面同时有「坏」和「修」可双标。",
)

OTHER = "其他"
_INDEX = {label: index for index, label in enumerate(LABELS)}


def order_labels(labels: Iterable[str]) -> list[str]:
    unique = set(labels)
    unknown = unique - _INDEX.keys()
    if unknown:
        raise ValueError(f"未知主题标签：{'、'.join(sorted(unknown))}")
    return [label for label in LABELS if label in unique]


def validate_labels(labels: Iterable[str]) -> list[str]:
    ordered = order_labels(labels)
    if not ordered:
        raise ValueError("主题标签不能为空")
    return ordered
```

- [ ] **Step 5: 加配置字段、测试默认值、依赖与环境示例**

在 `app/config.py` 的 `flywheel_dedupe_candidates` 行后插入：

```python
    # ch10 主题分类：后台批量归类默认关（与定时评估一致），演示时显式打开。
    # 用 AliasChoices 而不是 alias：测试用字段名构造 Settings，pydantic v2 的 alias 会忽略字段名。
    topic_classify_enabled: bool = Field(
        default=False, validation_alias=AliasChoices("TOPIC_CLASSIFY_ENABLED", "topic_classify_enabled"))
    topic_service_url: str = Field(
        default="http://127.0.0.1:8933", min_length=1, max_length=255,
        validation_alias=AliasChoices("TOPIC_SERVICE_URL", "topic_service_url"))
    topic_classify_interval_minutes: int = Field(
        default=10, ge=1, le=1440,
        validation_alias=AliasChoices("TOPIC_CLASSIFY_INTERVAL_MINUTES", "topic_classify_interval_minutes"))
    topic_classify_min_pending: int = Field(
        default=20, ge=1,
        validation_alias=AliasChoices("TOPIC_CLASSIFY_MIN_PENDING", "topic_classify_min_pending"))
    topic_classify_batch: int = Field(
        default=50, ge=1, le=200,
        validation_alias=AliasChoices("TOPIC_CLASSIFY_BATCH", "topic_classify_batch"))
    topic_model_dir: str = Field(
        default=".cache/models/topic-classifier", min_length=1, max_length=255,
        validation_alias=AliasChoices("TOPIC_MODEL_DIR", "topic_model_dir"))
    topic_device: Literal["cpu", "cuda"] = Field(
        default="cpu", validation_alias=AliasChoices("TOPIC_DEVICE", "topic_device"))
    topic_max_len: int = Field(
        default=128, ge=32, le=256,
        validation_alias=AliasChoices("TOPIC_MAX_LEN", "topic_max_len"))
```

`tests/support.py` 的 `_settings` defaults 追加：

```python
        # ch10 主题分类默认关：未启动推理服务时别让调度器在测试里打日志。
        "topic_classify_enabled": False,
```

`requirements.txt` 在 `sentence-transformers>=5,<7` 后加：

```text
# ch10 主题分类训练/推理直接依赖，锁文件里已是 5.21.0；不要升大版本
transformers==5.21.0
```

`.env.example` 末尾追加：

```ini
# ch10 主题分类：后台批量归类（默认关，演示时打开）
TOPIC_CLASSIFY_ENABLED=false
TOPIC_SERVICE_URL=http://127.0.0.1:8933
TOPIC_CLASSIFY_INTERVAL_MINUTES=10
TOPIC_CLASSIFY_MIN_PENDING=20
TOPIC_CLASSIFY_BATCH=50

# 推理服务自身
TOPIC_MODEL_DIR=.cache/models/topic-classifier
TOPIC_DEVICE=cpu
TOPIC_MAX_LEN=128
```

- [ ] **Step 6: 跑测试确认全绿**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_ch10_taxonomy.py tests/test_ch10_config.py tests/test_core.py
```

预期：全部 passed。

- [ ] **Step 7: 追加 dev-notes 并提交**

在 `dev-notes/ch10.md` 追加「阶段 3 · T1 配置与术语表」四样留痕，然后：

```bash
git add app/topics/__init__.py app/topics/taxonomy.py app/config.py tests/support.py \
  requirements.txt .env.example tests/test_ch10_taxonomy.py tests/test_ch10_config.py \
  dev-notes/ch10.md
git commit -m "feat(ch10): 17 类术语表、配置键与依赖"
```

---

## Task 2: 数据模型、DDL 与幂等迁移

**Files:**
- Modify: `app/models.py`（文件末尾加 `TopicClassification`）
- Create: `scripts/init_sql/008_topics.sql`
- Create: `scripts/migrate_ch10.py`
- Test: `tests/test_ch10_schema.py`

**Interfaces:**
- Produces: `TopicClassification`（`id`、`question_id`、`labels`、`classified_at`；`uk_question_id`；`fk_topic_question`）。
- Produces: `scripts.migrate_ch10.migrate(engine) -> list[str]`，幂等；首次返回 `["表 topic_classifications"]`，第二次返回 `[]`。

- [ ] **Step 1: 写失败的表结构测试**

创建 `tests/test_ch10_schema.py`：

```python
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Base, LowConfidenceQuestion, TopicClassification

SQL_FILE = Path(__file__).resolve().parents[1] / "scripts/init_sql/008_topics.sql"


def _engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'topic-schema.db'}")


def _seed_pool(session, text="运费怎么算"):
    row = LowConfidenceQuestion(raw_question=text, source="retrieval_low_conf")
    session.add(row)
    session.flush()
    return row.id


def test_table_is_created_in_sqlite_and_columns_match(tmp_path):
    engine = _engine(tmp_path)
    Base.metadata.create_all(engine)
    assert "topic_classifications" in inspect(engine).get_table_names()
    columns = {c["name"]: c for c in inspect(engine).get_columns("topic_classifications")}
    assert set(columns) == {"id", "question_id", "labels", "classified_at"}
    assert columns["question_id"]["nullable"] is False
    assert columns["labels"]["nullable"] is False


def test_unique_question_id_and_foreign_key(tmp_path):
    engine = _engine(tmp_path)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        question_id = _seed_pool(session)
        session.add(TopicClassification(question_id=question_id, labels=["运费"]))
        session.commit()
        session.add(TopicClassification(question_id=question_id, labels=["物流"]))
        with pytest.raises(IntegrityError):
            session.commit()


def test_mysql_ddl_matches_user_ddl():
    from sqlalchemy.dialects import mysql
    from sqlalchemy.schema import CreateTable

    ddl = str(CreateTable(TopicClassification.__table__).compile(dialect=mysql.dialect()))
    assert "BIGINT UNSIGNED NOT NULL AUTO_INCREMENT" in ddl
    assert "labels JSON NOT NULL" in ddl
    assert "classified_at DATETIME NOT NULL" in ddl
    assert "CONSTRAINT uk_question_id UNIQUE (question_id)" in ddl
    assert "CONSTRAINT fk_topic_question FOREIGN KEY(question_id) " \
           "REFERENCES low_confidence_questions (id)" in ddl
    assert "CHARSET=utf8mb4" in ddl


def test_sql_file_contains_user_ddl_lines():
    sql = SQL_FILE.read_text(encoding="utf-8")
    assert "CREATE TABLE topic_classifications" in sql
    assert "UNIQUE KEY uk_question_id (question_id)" in sql
    assert "CONSTRAINT fk_topic_question FOREIGN KEY (question_id)" in sql
    assert "REFERENCES low_confidence_questions (id)" in sql
    assert "SET NAMES utf8mb4;" in sql
    assert "COMMENT '主题分类结果'" in sql


def test_migrate_is_idempotent(tmp_path):
    from scripts.migrate_ch10 import migrate

    engine = _engine(tmp_path)
    assert migrate(engine) == ["表 topic_classifications"]
    assert migrate(engine) == []
```

运行：`ModuleNotFoundError: app.models.TopicClassification`。

- [ ] **Step 2: 在 `app/models.py` 末尾加 ORM**

```python
class TopicClassification(Base):
    """ch10 主题归类结果：一条低置信问题只保留一行，重跑按 question_id 更新。"""

    __tablename__ = "topic_classifications"
    __table_args__ = (
        UniqueConstraint("question_id", name="uk_question_id"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    question_id: Mapped[int] = mapped_column(
        IdType,
        ForeignKey("low_confidence_questions.id", name="fk_topic_question"),
        nullable=False,
    )
    labels: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    classified_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
```

- [ ] **Step 3: 写 `scripts/init_sql/008_topics.sql`（用户 DDL 原样）**

```sql
-- ch10 · 多标签主题分类 · 建表 DDL
-- Docker 首次初始化会自动执行；已有库跑 python scripts/migrate_ch10.py（幂等）。
SET NAMES utf8mb4;

CREATE TABLE topic_classifications (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  question_id   BIGINT UNSIGNED NOT NULL                COMMENT '归类的问题,指向 low_confidence_questions.id',
  labels        JSON            NOT NULL                COMMENT '多标签,17 类权威类目里命中的若干个',
  classified_at DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '归类时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_question_id (question_id),
  CONSTRAINT fk_topic_question FOREIGN KEY (question_id) REFERENCES low_confidence_questions (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='主题分类结果';
```

- [ ] **Step 4: 写 `scripts/migrate_ch10.py`**

```python
"""ch10 升级已有库：只补 topic_classifications，重复执行安全。

跑法：
    python scripts/migrate_ch10.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import inspect  # noqa: E402

from app.config import Settings  # noqa: E402
from app.database import build_database_url, build_engine  # noqa: E402
from app.models import Base  # noqa: E402

NEW_TABLES = ("topic_classifications",)


def migrate(engine) -> list[str]:
    done: list[str] = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in NEW_TABLES:
                continue
            missing = not inspect(conn).has_table(table.name)
            table.create(conn, checkfirst=True)
            if missing:
                done.append(f"表 {table.name}")
    return done


def main() -> int:
    settings = Settings.from_env()
    engine = build_engine(build_database_url(settings))
    try:
        done = migrate(engine)
    finally:
        engine.dispose()
    print("ch10 迁移完成：" + ("；".join(done) if done else "结构已是最新，无需变更"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: 跑测试与迁移脚本**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_ch10_schema.py tests/test_ch09_schema.py
python -m scripts.migrate_ch10
```

预期：测试全绿；迁移输出「ch10 迁移完成：表 topic_classifications」。

- [ ] **Step 6: 追加 dev-notes 并提交**

```bash
git add app/models.py scripts/init_sql/008_topics.sql scripts/migrate_ch10.py \
  tests/test_ch10_schema.py dev-notes/ch10.md
git commit -m "feat(ch10): topic_classifications 表与幂等迁移"
```

---

## Task 3: 清洗与脱敏

**Files:**
- Create: `app/topics/clean.py`
- Test: `tests/test_ch10_clean.py`

**Interfaces:**
- Produces: `mask_pii(text: str) -> str`、`clean_text(text: str) -> str`。顺序固定：先 `mask_pii` 再 `clean_text`。

- [ ] **Step 1: 写失败测试**

```python
from app.topics.clean import clean_text, mask_pii


def test_masks_phone_id_card_and_order_numbers():
    assert mask_pii("电话 13812345678") == "电话 [手机号]"
    assert mask_pii("身份证 110101199001011234") == "身份证 [证件号]"
    assert mask_pii("订单 1001 什么时候发") == "订单[单号] 什么时候发"
    assert mask_pii("运单号：SF1234567890") == "运单号:[单号]"
    assert mask_pii("订单号 998877") == "订单号 [单号]"
    assert mask_pii("编号 12345678") == "编号 [单号]"
    # 全角数字会先被 NFKC 折成明文，必须在 mask_pii 里就折；CJK 贴着数字也不能漏
    assert mask_pii("电话１３８１２３４５６７８") == "电话[手机号]"
    assert mask_pii("银行卡号是6222021234567890") == "银行卡号是[单号]"
    assert mask_pii("电话1381234567890") == "电话[单号]"
    # 全角连字符/下划线经 NFKC 折成 ASCII，不能因此屏蔽后面的数字
    assert mask_pii("卡号－6222021234567890") == "卡号-[单号]"
    assert mask_pii("卡号_6222021234567890") == "卡号_[单号]"


def test_keeps_short_numbers_that_are_not_ids():
    assert mask_pii("满 99 包邮吗") == "满 99 包邮吗"


def test_normalizes_fullwidth_and_whitespace():
    assert clean_text("  ＡＢＣ　１２３  ") == "ABC 123"
    assert clean_text("你好\u200b世界\n\n\t怎么 退货") == "你好世界 怎么 退货"
    assert clean_text('“退换货？”') == "退换货?"


def test_is_idempotent():
    once = clean_text(mask_pii("  订单 1001， 电话13812345678！！  "))
    twice = clean_text(mask_pii(once))
    assert once == twice == "订单[单号], 电话[手机号]!!"


def test_remasking_already_masked_text_is_fixed_point():
    text = "订单号 [单号] 电话[手机号] 证件[证件号]"
    assert mask_pii(text) == text


def test_empty_input():
    assert clean_text(None) == ""
    assert clean_text("   ") == ""
```

运行：`ModuleNotFoundError: app.topics.clean`。

- [ ] **Step 2: 实现 `app/topics/clean.py`**

```python
import re
import unicodedata

_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_ORDER = re.compile(
    r"(快递单号|运单号|订单号|订单|(?<!订)单号)(?!号)(\s*[:：]?\s*)([A-Za-z0-9-]{4,})")
_BARE_NUMBER = re.compile(r"(?<![A-Za-z0-9])\d{6,}(?![A-Za-z0-9])")
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2028-\u202f\u2060\ufeff]")
_PUNCT = str.maketrans({"！": "!", "？": "?", "，": ",", "。": ".", "：": ":", "；": ";",
                       "（": "(", "）": ")"})


def _mask_order(match: re.Match) -> str:
    prefix, separator = match.group(1), match.group(2)
    # 「订单 1001」贴在一起写；「订单号 998877」「运单号：SF...」保留原有分隔。
    return f"{prefix}{'' if prefix == '订单' else separator}[单号]"


def mask_pii(text: str) -> str:
    # 先折全角：否则「１３８…」绕开正则，后一步 clean_text 再折成明文手机号。
    text = unicodedata.normalize("NFKC", text)
    text = _ID_CARD.sub("[证件号]", text)
    text = _PHONE.sub("[手机号]", text)
    text = _ORDER.sub(_mask_order, text)
    return _BARE_NUMBER.sub("[单号]", text)


def clean_text(text: str | None) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = _ZERO_WIDTH.sub("", text)
    text = "".join(ch if unicodedata.category(ch)[0] != "C" else " " for ch in text)
    text = text.translate(_PUNCT)
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" \"'“”‘’")
```

- [ ] **Step 3: 跑测试并修正边界**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_ch10_clean.py
```

若「订单 1001」被 `_BARE_NUMBER` 先命中，检查顺序：`_ORDER` 必须在 `_BARE_NUMBER` 之前；若标点未转换，检查 `NFKC` 与 `_PUNCT` 的先后。

- [ ] **Step 4: 追加 dev-notes 并提交**

```bash
git add app/topics/clean.py tests/test_ch10_clean.py dev-notes/ch10.md
git commit -m "feat(ch10): 文本清洗与脱敏"
```

---

## Task 4: 语料读写、去重与迭代分层切分

**Files:**
- Create: `app/topics/dataset.py`
- Create: `tests/test_ch10_dataset.py`

**Interfaces:**
- Produces: `Sample(id: str, text: str, labels: list[str], origin: str, target_labels: list[str], prelabels: list[str], seed_id: str = "")`（dataclass，`frozen=True`；`seed_id` 只给改写样本）
- Produces: `make_sample(*, id, text, labels, origin="synthetic", target_labels=None, prelabels=None, seed_id="") -> Sample`
- Produces: `read_jsonl(path) -> list[Sample]`、`write_jsonl(path, samples) -> None`
- Produces: `dedupe(samples, threshold=0.9) -> tuple[list[Sample], int]`
- Produces: `iterative_split(samples, ratios=(0.8, 0.1, 0.1), seed=42) -> dict[str, list[Sample]]`，键固定 `"train"/"val"/"test"`
- Consumes: `app.topics.taxonomy.validate_labels/order_labels/LABELS`

- [ ] **Step 1: 写失败的去重与切分测试**

创建 `tests/test_ch10_dataset.py`：

```python
import pytest

from app.topics.dataset import (
    Sample, dedupe, iterative_split, make_sample, read_jsonl, write_jsonl,
)


def sample(i, labels, text=None):
    return make_sample(id=f"s{i:03d}", text=text or f"问题{i}", labels=labels)


def test_make_sample_orders_labels_and_rejects_empty_text():
    item = make_sample(id="s1", text="  买大了想退  ", labels=["尺码", "退换货", "尺码"])
    assert item.text == "买大了想退"
    assert item.labels == ["退换货", "尺码"]
    with pytest.raises(ValueError):
        make_sample(id="s2", text="   ", labels=["物流"])
    with pytest.raises(ValueError, match="未知主题标签"):
        make_sample(id="s3", text="退款", labels=["退款"])


def test_jsonl_roundtrip(tmp_path):
    path = tmp_path / "x.jsonl"
    items = [sample(1, ["物流"]), make_sample(id="s2", text="发票怎么开", labels=["发票"],
                                              prelabels=["发票"])]
    write_jsonl(path, items)
    assert read_jsonl(path) == items


def test_dedupe_exact_and_near_duplicate():
    # 字符 3-gram：B 只比 A 多一个尾字，Jaccard = 10/11 > 0.9
    a = "一二三四五六七八九十一二"
    b = a + "三"
    kept, dropped = dedupe([sample(1, ["物流"], "快递到哪了"),
                            sample(2, ["物流"], " 快递到哪了 "),
                            sample(3, ["物流"], a),
                            sample(4, ["物流"], b)])
    assert [item.id for item in kept] == ["s001", "s003"]
    assert dropped == 2


def test_dedupe_keeps_first_occurrence_and_order():
    kept, dropped = dedupe([sample(3, ["物流"], "同一句"), sample(1, ["物流"], "同一句")])
    assert [item.id for item in kept] == ["s003"] and dropped == 1


def _corpus():
    labels = ["退换货", "物流", "尺码", "发票", "运费"]
    items = []
    for i in range(100):
        first = labels[i % len(labels)]
        pair = [first] if i % 4 else [first, labels[(i + 1) % len(labels)]]
        items.append(sample(i, pair, f"第{i}条不同的问题描述"))
    return items


def test_split_is_disjoint_cover_and_reproducible():
    items = _corpus()
    first = iterative_split(items, seed=42)
    second = iterative_split(items, seed=42)
    groups = [first[name] for name in ("train", "val", "test")]
    ids = [[item.id for item in group] for group in groups]
    assert sum(len(group) for group in groups) == len(items)
    assert len(set().union(*[set(group) for group in ids])) == len(items)
    assert ids == [[item.id for item in second[name]] for name in ("train", "val", "test")]
    assert abs(len(first["val"]) / len(items) - 0.1) <= 0.02
    assert abs(len(first["test"]) / len(items) - 0.1) <= 0.02


def test_split_covers_frequent_label_in_val_and_test():
    items = _corpus()
    splits = iterative_split(items, seed=42)
    for name in ("val", "test"):
        present = {label for item in splits[name] for label in item.labels}
        assert {"退换货", "物流", "尺码", "发票", "运费"} <= present


def test_split_is_balanced_per_label():
    splits = iterative_split(_corpus(), seed=42)
    for label in ("退换货", "物流", "尺码", "发票", "运费"):
        count = sum(1 for item in splits["val"] if label in item.labels)
        assert 1 <= count <= 4
```

运行：

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_ch10_dataset.py
```

预期：`ModuleNotFoundError: No module named 'app.topics.dataset'`。

- [ ] **Step 2: 实现 `dataset.py` 的读写、去重与切分**

```python
"""语料读写、去重与迭代分层切分。"""

from __future__ import annotations

import json
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from app.topics.taxonomy import LABELS, order_labels, validate_labels

SPLITS = ("train", "val", "test")


@dataclass(frozen=True)
class Sample:
    id: str
    text: str
    labels: list[str]
    origin: str = "synthetic"
    target_labels: list[str] = field(default_factory=list)
    prelabels: list[str] = field(default_factory=list)
    seed_id: str = ""

    def to_dict(self) -> dict:
        payload = {
            "id": self.id, "text": self.text, "labels": list(self.labels),
            "origin": self.origin, "target_labels": list(self.target_labels),
            "prelabels": list(self.prelabels),
        }
        if self.seed_id:
            payload["seed_id"] = self.seed_id
        return payload


def make_sample(*, id: str, text: str, labels: Sequence[str],
                origin: str = "synthetic", target_labels: Sequence[str] | None = None,
                prelabels: Sequence[str] | None = None, seed_id: str = "") -> Sample:
    cleaned = " ".join(str(text).split())
    if not cleaned:
        raise ValueError("样本文本为空")
    return Sample(
        id=id,
        text=cleaned,
        labels=validate_labels(labels),
        origin=origin,
        target_labels=validate_labels(target_labels if target_labels else labels),
        prelabels=order_labels([label for label in (prelabels or []) if label in LABELS]),
        seed_id=seed_id,
    )


def read_jsonl(path: Path | str) -> list[Sample]:
    samples = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            samples.append(make_sample(**json.loads(line)))
    return samples


def write_jsonl(path: Path | str, samples: Sequence[Sample]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(json.dumps(item.to_dict(), ensure_ascii=False) + "\n" for item in samples),
        encoding="utf-8",
    )


def normalize_for_dedupe(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.casefold())


def _trigrams(text: str) -> set[str]:
    normalized = normalize_for_dedupe(text)
    if len(normalized) < 3:
        return {normalized} if normalized else set()
    return {normalized[i:i + 3] for i in range(len(normalized) - 2)}


def jaccard(a: str, b: str) -> float:
    left, right = _trigrams(a), _trigrams(b)
    if not left and not right:
        return 1.0
    return len(left & right) / len(left | right)


def dedupe(samples: Sequence[Sample], threshold: float = 0.9) -> tuple[list[Sample], int]:
    """精确去重后做字符 3-gram 近似去重，保留先出现者。"""
    kept: list[Sample] = []
    seen: set[str] = set()
    dropped = 0
    for item in samples:
        normalized = normalize_for_dedupe(item.text)
        if not normalized or normalized in seen:
            dropped += 1
            continue
        if any(jaccard(item.text, other.text) >= threshold for other in kept):
            dropped += 1
            continue
        seen.add(normalized)
        kept.append(item)
    return kept, dropped


def iterative_split(samples: Sequence[Sample], ratios: tuple[float, float, float] = (0.8, 0.1, 0.1),
                    seed: int = 42) -> dict[str, list[Sample]]:
    """按标签迭代分配：稀有标签先分，每次放进该类最缺且尚有余量的分片。"""
    rng = random.Random(seed)
    shuffled = list(samples)
    rng.shuffle(shuffled)
    total = len(shuffled)
    caps = {name: int(total * ratio) for name, ratio in zip(SPLITS, ratios)}
    caps["train"] += total - sum(caps.values())
    buckets: dict[str, list[Sample]] = {name: [] for name in SPLITS}
    assigned: set[str] = set()

    support = Counter(label for item in shuffled for label in item.labels)
    for label in sorted(support, key=lambda name: (support[name], LABELS.index(name))):
        members = [item for item in shuffled if label in item.labels]
        for item in members:
            if item.id in assigned:
                continue
            candidates = [name for name in SPLITS if len(buckets[name]) < caps[name]]
            if not candidates:
                break
            target = {name: support[label] * ratio for name, ratio in zip(SPLITS, ratios)}
            current = {name: sum(1 for other in buckets[name] if label in other.labels)
                       for name in SPLITS}
            # 余量相同按 train→val→test 顺序取；最后用 rng 打破平局，保证同 seed 可复现
            best = max(candidates, key=lambda name: (target[name] - current[name],
                                                     -SPLITS.index(name), rng.random()))
            buckets[best].append(item)
            assigned.add(item.id)

    for item in shuffled:
        if item.id in assigned:
            continue
        candidates = [name for name in SPLITS if len(buckets[name]) < caps[name]]
        best = max(candidates, key=lambda name: (caps[name] - len(buckets[name]), -SPLITS.index(name)))
        buckets[best].append(item)
        assigned.add(item.id)

    # 硬保证：样本数 ≥10 的标签在 val/test 至少出现一次；从 train 挪一条过去。
    for label, count in support.items():
        if count < 10:
            continue
        for name in ("val", "test"):
            if any(label in item.labels for item in buckets[name]):
                continue
            donor = next((item for item in buckets["train"] if label in item.labels), None)
            if donor is not None:
                buckets["train"].remove(donor)
                buckets[name].append(donor)
    return buckets
```

- [ ] **Step 3: 跑测试**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_ch10_dataset.py
```

预期：全部通过。若 `test_split_is_balanced_per_label` 的计数上限变了，先打印三份的标签分布再调断言，不要放宽到「只要 ≥1」。

- [ ] **Step 4: 追加 dev-notes 并提交**

在 `dev-notes/ch10.md` 记：切分算法最终采用「稀有标签优先 + 缺口最大优先 + 覆盖率修复」，并粘一条实际 `sizes={train,val,test}` 输出。

```bash
git add app/topics/dataset.py tests/test_ch10_dataset.py dev-notes/ch10.md
git commit -m "feat(ch10): 语料去重与迭代分层切分"
```

---

## Task 5: 训练集增强、句式改写挂载与抽审修正

**Files:**
- Modify: `app/topics/dataset.py`（追加增强与组装函数）
- Modify: `tests/test_ch10_dataset.py`（追加测试）
- Create: `data/topics/README.md`

**Interfaces:**
- Produces: `SYNONYMS: tuple[tuple[str, str], ...]`（至少 8 组，含快递↔物流、包邮↔免运费、开票↔开发票、价保↔保价、补货↔到货、积分↔会员分）
- Produces: `apply_synonyms(text: str, rng: random.Random, max_replacements: int = 2) -> str`
- Produces: `attach_paraphrases(train, paraphrases, *, ratio=0.3, seed=42) -> list[Sample]`（按改写样本自带的 `seed_id` 分组）
- Produces: `apply_corrections(samples, corrections: dict[str, list[str]]) -> list[Sample]`
- Produces: `assemble_splits(synthetic, *, real=(), paraphrases=(), seed=42) -> dict[str, list[Sample]]`
- Consumes: Task 4 的 `Sample/dedupe/iterative_split`

- [ ] **Step 1: 追加失败测试**

在 `tests/test_ch10_dataset.py` 末尾追加：

```python
from app.topics.dataset import (
    SYNONYMS, apply_corrections, apply_synonyms, assemble_splits, attach_paraphrases,
)


def test_synonyms_have_required_pairs_and_no_duplicate_source():
    pairs = dict(SYNONYMS)
    assert pairs["快递"] == "物流"
    assert pairs["包邮"] == "免运费"
    assert pairs["开票"] == "开发票"
    assert pairs["价保"] == "保价"
    assert pairs["补货"] == "到货"
    assert pairs["积分"] == "会员分"
    sources = [source for source, _ in SYNONYMS]
    assert len(sources) == len(set(sources))


def test_apply_synonyms_at_most_two_and_skips_existing_target():
    import random

    text = "快递包邮吗，开票怎么弄"
    replaced = apply_synonyms(text, random.Random(1), max_replacements=2)
    assert sum(word in replaced for word in ("物流", "免运费", "开发票")) == 2
    # 原文已有「物流」时不再把「快递」替换成第二个「物流」
    assert apply_synonyms("物流快递", random.Random(1)) == "物流快递"


def test_attach_paraphrases_only_when_seed_in_train():
    train = [sample(1, ["退换货"]), sample(2, ["物流"]), sample(3, ["发票"])]
    paras = [
        make_sample(id="p1", text="退换货改写", labels=["退换货"], origin="paraphrase",
                    seed_id="s001"),
        make_sample(id="p2", text="物流改写", labels=["物流"], origin="paraphrase",
                    seed_id="s999"),
    ]
    merged = attach_paraphrases(train, paras, ratio=1.0, seed=42)
    ids = [item.id for item in merged]
    assert "p1" in ids and "p2" not in ids


def test_apply_corrections_overrides_labels_and_rejects_unknown():
    items = [sample(1, ["物流"]), sample(2, ["发票"])]
    fixed = apply_corrections(items, {"s001": ["运费", "物流"]})
    assert fixed[0].labels == ["物流", "运费"]
    assert fixed[1].labels == ["发票"]
    with pytest.raises(ValueError, match="未知主题标签"):
        apply_corrections(items, {"s001": ["退款"]})


def test_assemble_splits_appends_real_only_to_train_and_augments_train_only():
    synthetic = _corpus()
    real = [make_sample(id="real-1", text="真实问题", labels=["物流"], origin="real")]
    # 每个合成样本都配一条改写；切分后 train 里至少有一个 seed 能挂上改写
    paras = [make_sample(id=f"p{i:03d}", text=f"第{i}条不同的问题描述改写",
                         labels=synthetic[i].labels, origin="paraphrase",
                         seed_id=f"s{i:03d}") for i in range(100)]
    splits = assemble_splits(synthetic, real=real, paraphrases=paras, seed=42)
    train_ids = {item.id for item in splits["train"]}
    assert "real-1" in train_ids
    assert all(item.id != "real-1" for name in ("val", "test") for item in splits[name])
    assert any(item.origin == "paraphrase" for item in splits["train"])
    # 验证/测试集绝不出现改写样本
    assert all(item.origin != "paraphrase" for name in ("val", "test") for item in splits[name])
```

- [ ] **Step 2: 在 `dataset.py` 追加实现**

```python
SYNONYMS: tuple[tuple[str, str], ...] = (
    ("快递", "物流"),
    ("包邮", "免运费"),
    ("退换", "退换货"),
    ("开票", "开发票"),
    ("优惠券", "券"),
    ("价保", "保价"),
    ("补货", "到货"),
    ("积分", "会员分"),
)


def apply_synonyms(text: str, rng: random.Random, max_replacements: int = 2) -> str:
    result = text
    changed = 0
    for source, target in rng.sample(list(SYNONYMS), len(SYNONYMS)):
        if changed >= max_replacements:
            break
        if source in result and target not in result:
            result = result.replace(source, target, 1)
            changed += 1
    return result


def attach_paraphrases(train: Sequence[Sample], paraphrases: Sequence[Sample], *,
                       ratio: float = 0.3, seed: int = 42) -> list[Sample]:
    """变体与 seed 同组：seed 不在训练集就丢弃，避免验证/测试泄漏。"""
    rng = random.Random(seed)
    train_ids = {item.id for item in train}
    grouped: dict[str, list[Sample]] = {}
    for para in paraphrases:
        if para.seed_id in train_ids:
            grouped.setdefault(para.seed_id, []).append(para)
    chosen = sorted(grouped)
    sample_size = min(len(chosen), max(0, round(len(train) * ratio)))
    picked = set(rng.sample(chosen, sample_size)) if sample_size else set()
    merged = list(train)
    for seed_id in sorted(picked):
        merged.extend(grouped[seed_id])
    return merged


def apply_corrections(samples: Sequence[Sample], corrections: dict[str, list[str]]) -> list[Sample]:
    fixed = []
    for item in samples:
        labels = corrections.get(item.id, item.labels)
        fixed.append(make_sample(id=item.id, text=item.text, labels=labels,
                                 origin=item.origin, target_labels=item.target_labels,
                                 prelabels=item.prelabels, seed_id=item.seed_id))
    return fixed


def assemble_splits(synthetic: Sequence[Sample], *, real: Sequence[Sample] = (),
                    paraphrases: Sequence[Sample] = (), seed: int = 42) -> dict[str, list[Sample]]:
    splits = iterative_split(synthetic, seed=seed)
    train = list(splits["train"])
    if real:
        train.extend(real)
    rng = random.Random(seed)
    augmented = [make_sample(id=f"{item.id}-syn", text=apply_synonyms(item.text, rng),
                             labels=item.labels, origin="synonym",
                             target_labels=item.target_labels)
                 for item in train]
    originals = {item.id: item.text for item in train}
    train.extend(item for item in augmented
                 if item.text != originals[item.id.removesuffix("-syn")])
    if paraphrases:
        train = attach_paraphrases(train, paraphrases, seed=seed)
    kept, _ = dedupe(train)
    return {"train": kept, "val": list(splits["val"]), "test": list(splits["test"])}
```

- [ ] **Step 3: 跑新测试**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_ch10_dataset.py
```

预期：全部通过。若 `apply_synonyms("物流快递")` 的断言失败，检查「目标词已在原文则跳过」的判断是否在替换之前。

- [ ] **Step 4: 写 `data/topics/README.md`**

```markdown
# ch10 主题分类语料

| 文件 | 入库 | 说明 |
|---|---|---|
| `synthetic_v1.jsonl` | 是 | 1200 条 LLM 合成问题，17 类配额固定，仅合成无隐私 |
| `paraphrase_v1.jsonl` | 是 | 句式改写池，只挂训练集，`seed_id` 指回合成样本 |
| `real_train.jsonl` | 否 | 飞轮真实问题清洗后 + 人工标签，git-ignored |
| `real_audit.csv` | 否 | 真实问题人工确认表，含 DB 原文，人工就地编辑；`--apply-real-audit` 直接读它，git-ignored |

字段：`id,text,labels,origin,target_labels,prelabels`。标签只允许 `app/topics/taxonomy.py`
的 17 类，写入前按术语表顺序排序。

重建命令见 `docs/superpowers/plans/2026-11-02-ch10-topic-classifier.md` 的
「数据构建与生成」任务；真实池原文不提交，仓库里只保留合成语料。
```

- [ ] **Step 5: 追加 dev-notes 并提交**

```bash
git add app/topics/dataset.py tests/test_ch10_dataset.py data/topics/README.md dev-notes/ch10.md
git commit -m "feat(ch10): 训练集同义词与改写增强"
```

---

## Task 6: Prompt 与数据构建 CLI

**Files:**
- Create: `app/topics/prompts.py`
- Create: `scripts/build_topic_dataset.py`
- Create: `tests/test_ch10_build.py`

**Interfaces:**
- Produces: `generate_prompt(labels, count)`、`prelabel_prompt(items)`、`paraphrase_prompt(items)`、`typo_prompt(items)`、`parse_payload(content) -> dict`
- Produces: `QUOTAS`、`COMBOS`、`TOTAL=1200`、`plan_generation() -> list[dict]`
- Produces: `generate_group(llm, labels, count, batch=10, retries=1) -> list[str]`
- Produces: `export_audit(samples, out, per_class=2, max_disagree=20, seed=42) -> int`
- Produces: `run_generate / run_prelabel / run_paraphrase / run_apply_audit / run_pull_real / run_apply_real_audit / run_split`
- Consumes: Task 3 的 `clean_text/mask_pii`、Task 4/5 的 `Sample/dedupe/assemble_splits`、`app.model.build_model`

- [ ] **Step 1: 写失败的构建测试**

创建 `tests/test_ch10_build.py`：

```python
import csv
import json
from collections import Counter
from types import SimpleNamespace

import pytest

from app.topics.dataset import make_sample, read_jsonl
from app.topics.prompts import parse_payload
from app.topics.taxonomy import LABELS
from scripts.build_topic_dataset import (
    COMBOS, QUOTAS, TOTAL, export_audit, generate_group, plan_generation, run_paraphrase, run_split,
)


class FakeLLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(content=self.outputs.pop(0))


def test_plan_generation_hits_total_and_quotas():
    plan = plan_generation()
    assert sum(item["count"] for item in plan) == TOTAL == 1200
    assert len(COMBOS) == 10
    assert sum(item["count"] for item in plan if len(item["labels"]) == 2) == 300
    support = Counter()
    for item in plan:
        for label in item["labels"]:
            support[label] += item["count"]
    assert set(support) == set(LABELS)
    assert all(support[label] >= QUOTAS[label] for label in LABELS)


def test_parse_payload_accepts_fenced_json_and_rejects_garbage():
    assert parse_payload('```json\n{"items":[{"question":"在吗"}]}\n```')["items"][0]["question"] == "在吗"
    with pytest.raises(ValueError):
        parse_payload("没有 JSON")
    with pytest.raises(ValueError):
        parse_payload("[1, 2]")


def test_generate_group_retries_once_then_succeeds():
    llm = FakeLLM(["不是 JSON", json.dumps({"items": [{"question": "快递到哪了"},
                                                       {"question": "什么时候发货"}]},
                                          ensure_ascii=False)])
    assert generate_group(llm, ["物流"], 2) == ["快递到哪了", "什么时候发货"]
    assert len(llm.prompts) == 2


def test_generate_group_raises_instead_of_silently_short():
    llm = FakeLLM(["坏输出", "还是坏输出"])
    with pytest.raises(RuntimeError, match="生成失败"):
        generate_group(llm, ["物流"], 2)


def test_export_audit_is_deterministic_and_per_class(tmp_path):
    samples = [make_sample(id=f"s{i:03d}", text=f"第{i}条问题", labels=[LABELS[i % 17]],
                           prelabels=[LABELS[i % 17]]) for i in range(17 * 6)]
    left, right = tmp_path / "a.csv", tmp_path / "b.csv"
    assert export_audit(samples, left, per_class=2) == 34
    export_audit(samples, right, per_class=2)
    assert left.read_text(encoding="utf-8-sig") == right.read_text(encoding="utf-8-sig")
    rows = list(csv.DictReader(left.open(encoding="utf-8-sig")))
    assert len(rows) == 34
    assert all(row["correct_labels"] == "" and row["note"] == "" for row in rows)


def test_run_paraphrase_keeps_only_label_preserving_rewrites(tmp_path):
    seeds = tmp_path / "seeds.jsonl"
    from app.topics.dataset import write_jsonl

    write_jsonl(seeds, [make_sample(id="s1", text="快递到哪了", labels=["物流"]),
                        make_sample(id="s2", text="发票怎么开", labels=["发票"])])
    llm = FakeLLM([
        json.dumps({"items": [{"id": "s1", "text": "我的包裹现在到哪了"},
                              {"id": "s2", "text": "怎么开发票"}]}, ensure_ascii=False),
        json.dumps({"items": [{"id": "par-0001", "labels": ["物流"]},
                              {"id": "par-0002", "labels": ["支付"]}]}, ensure_ascii=False),
    ])
    out = tmp_path / "par.jsonl"
    kept, dropped = run_paraphrase(llm, seeds, out)
    assert (kept, dropped) == (1, 1)
    items = read_jsonl(out)
    assert items[0].seed_id == "s1" and items[0].labels == ["物流"]


def test_run_split_appends_real_and_keeps_paraphrase_out_of_val_test(tmp_path):
    from app.topics.dataset import write_jsonl

    labels = ["退换货", "物流", "尺码", "发票", "运费"]
    synthetic = [make_sample(id=f"s{i:03d}", text=f"第{i}条不同问法", labels=[labels[i % 5]])
                 for i in range(100)]
    synthetic_path = tmp_path / "syn.jsonl"
    write_jsonl(synthetic_path, synthetic)
    real_path = tmp_path / "real.jsonl"
    write_jsonl(real_path, [make_sample(id="real-1", text="真实问题", labels=["物流"], origin="real")])
    para_path = tmp_path / "par.jsonl"
    write_jsonl(para_path, [make_sample(id=f"p{i:03d}", text=f"改写第{i}条不同问法",
                                        labels=[labels[i % 5]], origin="paraphrase",
                                        seed_id=f"s{i:03d}") for i in range(100)])

    splits = run_split(synthetic_path, tmp_path / "splits", real_path=real_path,
                       paraphrase_path=para_path, seed=42, report_dir=tmp_path)
    train_ids = {item.id for item in splits["train"]}
    assert "real-1" in train_ids
    assert any(item.origin == "paraphrase" for item in splits["train"])
    for name in ("val", "test"):
        assert all(item.origin != "paraphrase" for item in splits[name])
    assert (tmp_path / "splits" / "train.jsonl").exists()
```

- [ ] **Step 2: 实现 `prompts.py`**

```python
"""生成、独立预标、改写、错别字清洗四组 Prompt。"""

from __future__ import annotations

import json
import re

from app.topics.taxonomy import BOUNDARIES, DESCRIPTIONS, LABELS


def taxonomy_block() -> str:
    return "\n".join(f"- {label}：{DESCRIPTIONS[label]}" for label in LABELS)


def boundary_block() -> str:
    return "\n".join(f"- {rule}" for rule in BOUNDARIES)


GENERATE_SYSTEM = (
    "你是电商客服语料工程师。只输出一个 JSON 对象，不要解释，不要 Markdown 代码块。"
    "问题必须是顾客真实会说出口的中文口语，8~40 字，不得包含真实姓名、电话、地址、订单号。"
)


def generate_prompt(labels: list[str], count: int) -> str:
    return (
        f"{GENERATE_SYSTEM}\n\n主题术语表：\n{taxonomy_block()}\n\n"
        f"近邻规则：\n{boundary_block()}\n\n"
        f"本批只写同时表达这些诉求的问题：{'、'.join(labels)}。\n"
        f"共 {count} 条，每条只表达以上诉求，不要额外添加其他主题，条与条之间不得重复。\n"
        "输出格式：{\"items\":[{\"question\":\"...\"}]}"
    )


def prelabel_prompt(items: list[tuple[str, str]]) -> str:
    payload = json.dumps([{"id": item_id, "text": text} for item_id, text in items],
                         ensure_ascii=False)
    return (
        "你是电商客服主题标注员。按术语表给每条问题打多标签：字面提到几个诉求就打几个，"
        "一个不多一个不少；只按字面，不做意图推断。没有具体诉求、纯单号或闲聊打「其他」。\n\n"
        f"术语表：\n{taxonomy_block()}\n\n近邻规则：\n{boundary_block()}\n\n"
        f"待标注：{payload}\n\n"
        "输出格式：{\"items\":[{\"id\":\"...\",\"labels\":[\"...\"]}]}；labels 只能取术语表里的名字。"
    )


def paraphrase_prompt(items: list[tuple[str, str]]) -> str:
    payload = json.dumps([{"id": item_id, "text": text} for item_id, text in items],
                         ensure_ascii=False)
    return (
        "把下面的客服问题换一种口语说法，诉求完全不变，不改标签含义，8~40 字，每条只输出一个改写。\n"
        f"原句：{payload}\n"
        "输出格式：{\"items\":[{\"id\":\"...\",\"text\":\"...\"}]}"
    )


def typo_prompt(items: list[tuple[str, str]]) -> str:
    payload = json.dumps([{"id": item_id, "text": text} for item_id, text in items],
                         ensure_ascii=False)
    return (
        "只改下面的错别字和明显病句，不改变诉求，不扩写；没有错就原样返回。\n"
        f"原句：{payload}\n"
        "输出格式：{\"items\":[{\"id\":\"...\",\"text\":\"...\"}]}"
    )


def parse_payload(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型输出里没有 JSON 对象")
    payload = json.loads(text[start:end + 1])
    if not isinstance(payload, dict):
        raise ValueError("JSON 顶层必须是对象")
    return payload
```

- [ ] **Step 3: 实现 `scripts/build_topic_dataset.py`**

```python
"""构建 ch10 主题分类语料：生成、预标、改写、抽审、真实池、切分。"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from app.config import Settings
from app.database import build_database_url, build_engine, build_session_factory
from app.models import LowConfidenceQuestion
from app.topics.clean import clean_text, mask_pii
from app.topics.dataset import (
    Sample, apply_corrections, assemble_splits, dedupe, make_sample, read_jsonl, write_jsonl,
)
from app.topics.prompts import (
    generate_prompt, paraphrase_prompt, parse_payload, prelabel_prompt, typo_prompt,
)
from app.topics.taxonomy import LABELS, OTHER, order_labels

QUOTAS = {label: (80 if label == OTHER else 70) for label in LABELS}
COMBO_EACH = 30
TOTAL = 1200
COMBOS = (
    ("退换货", "尺码"), ("运费", "物流"), ("价保", "优惠活动"), ("发票", "订单修改"),
    ("会员积分", "运费"), ("库存补货", "保修维修"), ("质量问题", "退换货"),
    ("支付", "订单修改"), ("商品信息", "库存补货"), ("账号", "会员积分"),
)
PII = re.compile(r"1[3-9]\d{9}|\d{17}[\dXx]|\d{6,}")


def plan_generation() -> list[dict]:
    """300 条多标签 + 配额缺口单标签 + 轮转补齐到 1200 条。"""
    support: Counter[str] = Counter()
    for combo in COMBOS:
        for label in combo:
            support[label] += COMBO_EACH
    plan = [{"labels": list(combo), "count": COMBO_EACH} for combo in COMBOS]
    used = COMBO_EACH * len(COMBOS)
    for label in LABELS:
        deficit = max(0, QUOTAS[label] - support[label])
        if deficit:
            plan.append({"labels": [label], "count": deficit})
            used += deficit
    extra = TOTAL - used
    if extra < 0:
        raise ValueError(f"配额总和 {used} 超过 {TOTAL}，请检查 COMBOS 与 QUOTAS")
    base, remainder = divmod(extra, len(LABELS))
    for index, label in enumerate(LABELS):
        count = base + (1 if index < remainder else 0)
        if count:
            plan.append({"labels": [label], "count": count})
    return plan


def _ask(llm, prompt: str) -> str:
    result = llm.invoke(prompt)
    return str(getattr(result, "content", result))


def valid_question(text: str) -> bool:
    return 4 <= len(text) <= 60 and not PII.search(text)


def generate_group(llm, labels: list[str], count: int, batch: int = 10,
                   retries: int = 1) -> list[str]:
    questions: list[str] = []
    while len(questions) < count:
        need = min(batch, count - len(questions))
        accepted = None
        for _ in range(retries + 1):
            try:
                payload = parse_payload(_ask(llm, generate_prompt(labels, need)))
                items = [clean_text(str(row.get("question", ""))) for row in payload.get("items", [])]
                items = [mask_pii(item) for item in items if valid_question(item)]
                if items:
                    accepted = items[:need]
                    break
            except Exception:
                continue
        if not accepted:
            raise RuntimeError(f"生成失败：{labels} 还差 {count - len(questions)} 条")
        questions.extend(accepted)
    return questions


def run_generate(llm, out: Path, report_dir: Path, *, seed: int = 42) -> list[Sample]:
    samples = []
    counter = 1
    for group in plan_generation():
        for text in generate_group(llm, group["labels"], group["count"]):
            samples.append(make_sample(id=f"syn-{counter:04d}", text=text,
                                       labels=group["labels"], target_labels=group["labels"]))
            counter += 1
    kept, dropped = dedupe(samples)
    if dropped:
        raise RuntimeError(f"生成后去重掉 {dropped} 条，不足 {TOTAL}；请重跑 --generate")
    if len(kept) != TOTAL:
        raise RuntimeError(f"生成条数 {len(kept)} != {TOTAL}")
    write_jsonl(out, kept)
    write_report(report_dir, "dataset", _dataset_stats(kept, {}, {}))
    print(f"生成完成：{len(kept)} 条 → {out}")
    return kept


def _dataset_stats(samples: list[Sample], splits: dict, extra: dict) -> dict:
    counts = Counter(label for item in samples for label in item.labels)
    multi = sum(1 for item in samples if len(item.labels) > 1)
    agree = sum(1 for item in samples if item.prelabels and set(item.prelabels) == set(item.labels))
    compared = sum(1 for item in samples if item.prelabels)
    stats = {
        "total": len(samples),
        "per_label": {label: counts[label] for label in LABELS},
        "multi_label": multi,
        "prelabel_agree": f"{agree}/{compared}" if compared else "未预标",
        "splits": {name: len(items) for name, items in splits.items()},
        **extra,
    }
    return stats


def write_report(report_dir: Path, kind: str, stats: dict) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = report_dir / f"ch10-{kind}-{stamp}.md"
    lines = [f"# ch10 {kind} 报告", "", f"- 生成时间：{stamp}", ""]
    for key, value in stats.items():
        lines.append(f"- {key}：{value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_prelabel(llm, path: Path, batch: int = 20) -> list[Sample]:
    samples = read_jsonl(path)
    updated = []
    for start in range(0, len(samples), batch):
        chunk = samples[start:start + batch]
        try:
            payload = parse_payload(_ask(llm, prelabel_prompt([(s.id, s.text) for s in chunk])))
            got = {str(row.get("id")): row.get("labels", []) for row in payload.get("items", [])}
        except Exception:
            got = {}
        for item in chunk:
            labels = order_labels([label for label in got.get(item.id, []) if label in LABELS])
            updated.append(replace(item, prelabels=labels))
    write_jsonl(path, updated)
    agree = sum(1 for item in updated if set(item.prelabels) == set(item.labels))
    print(f"预标完成：{len(updated)} 条，与目标标签一致 {agree} 条（{agree / len(updated):.1%}）")
    return updated


def run_paraphrase(llm, in_path: Path, out_path: Path, batch: int = 10) -> tuple[int, int]:
    seeds = read_jsonl(in_path)
    rewrites: dict[str, str] = {}
    for start in range(0, len(seeds), batch):
        chunk = seeds[start:start + batch]
        try:
            payload = parse_payload(_ask(llm, paraphrase_prompt([(s.id, s.text) for s in chunk])))
        except Exception:
            continue
        known = {s.id: s for s in chunk}
        for row in payload.get("items", []):
            seed_id = str(row.get("id", ""))
            text = clean_text(str(row.get("text", "")))
            if seed_id in known and text and text != known[seed_id].text:
                rewrites[seed_id] = text
    candidates = [(f"par-{index:04d}", seed_id, rewrites[seed_id])
                  for index, seed_id in enumerate([s.id for s in seeds if s.id in rewrites], 1)]
    labels_by_id: dict[str, list[str]] = {}
    for start in range(0, len(candidates), 20):
        chunk = [(item_id, text) for item_id, _, text in candidates[start:start + 20]]
        try:
            payload = parse_payload(_ask(llm, prelabel_prompt(chunk)))
        except Exception:
            continue
        for row in payload.get("items", []):
            labels_by_id[str(row.get("id"))] = order_labels(
                [label for label in row.get("labels", []) if label in LABELS])
    seed_by_id = {s.id: s for s in seeds}
    kept, dropped = [], 0
    for item_id, seed_id, text in candidates:
        labels = labels_by_id.get(item_id, [])
        seed = seed_by_id[seed_id]
        if not labels or set(labels) != set(seed.labels):
            dropped += 1
            continue
        kept.append(make_sample(id=item_id, text=text, labels=labels, origin="paraphrase",
                                target_labels=seed.labels, prelabels=labels, seed_id=seed_id))
    write_jsonl(out_path, kept)
    print(f"改写完成：保留 {len(kept)} 条，丢弃 {dropped} 条（预标不一致）")
    return len(kept), dropped


def export_audit(samples: list[Sample], out: Path, per_class: int = 2,
                 max_disagree: int = 20, seed: int = 42) -> int:
    rng = random.Random(seed)
    picked: dict[str, Sample] = {}
    for label in LABELS:
        pool = [item for item in samples if label in item.labels]
        for item in rng.sample(pool, min(per_class, len(pool))):
            picked[item.id] = item
    disagree = [item for item in samples if set(item.prelabels) != set(item.labels)]
    for item in rng.sample(disagree, min(max_disagree, len(disagree))):
        picked.setdefault(item.id, item)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "text", "prelabels", "correct_labels", "note"])
        for item in sorted(picked.values(), key=lambda row: row.id):
            writer.writerow([item.id, item.text, ",".join(item.prelabels), "", ""])
    print(f"审计样本已导出：{len(picked)} 条 → {out}（请人工填 correct_labels 与 note）")
    return len(picked)


def _read_corrections(path: Path) -> dict[str, list[str]]:
    corrections = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            corrections[str(row["id"])] = row["labels"]
    return corrections


def run_apply_audit(path: Path, audit: Path) -> tuple[int, int]:
    samples = read_jsonl(path)
    corrections = _read_corrections(audit)
    fixed = apply_corrections(samples, corrections)
    changed = sum(1 for before, after in zip(samples, fixed) if before.labels != after.labels)
    write_jsonl(path, fixed)
    print(f"审计修正已合并：{len(corrections)} 条修正，实际改动 {changed} 条")
    return len(corrections), changed


def run_pull_real(settings: Settings, llm, out_csv: Path, batch: int = 20) -> int:
    engine = build_engine(build_database_url(settings))
    try:
        with build_session_factory(engine)() as session:
            rows = session.execute(
                select(LowConfidenceQuestion.id, LowConfidenceQuestion.raw_question)
                .order_by(LowConfidenceQuestion.id)
            ).all()
    finally:
        engine.dispose()
    seen, unique = set(), []
    for question_id, raw in rows:
        text = clean_text(mask_pii(raw or ""))
        if text and text not in seen:
            seen.add(text)
            unique.append((f"real-{question_id}", text))
    fixed: dict[str, str] = {}
    for start in range(0, len(unique), batch):
        chunk = unique[start:start + batch]
        try:
            payload = parse_payload(_ask(llm, typo_prompt(chunk)))
        except Exception:
            continue
        for row in payload.get("items", []):
            text = clean_text(str(row.get("text", "")))
            if text:
                fixed[str(row.get("id"))] = text
    pairs = [(item_id, fixed.get(item_id, text)) for item_id, text in unique]
    labels: dict[str, list[str]] = {}
    for start in range(0, len(pairs), batch):
        chunk = pairs[start:start + batch]
        try:
            payload = parse_payload(_ask(llm, prelabel_prompt(chunk)))
        except Exception:
            continue
        for row in payload.get("items", []):
            labels[str(row.get("id"))] = order_labels(
                [label for label in row.get("labels", []) if label in LABELS])
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "text", "prelabels", "correct_labels", "note"])
        for item_id, text in pairs:
            writer.writerow([item_id, text, ",".join(labels.get(item_id, [])), "", ""])
    print(f"真实池已导出：{len(pairs)} 条不同问法 → {out_csv}（全部需人工确认）")
    return len(pairs)


def run_apply_real_audit(audit: Path, out: Path) -> int:
    samples = []
    with audit.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            # 真实行没有“目标标签”，人工留空视为同意预标；预标也空才跳过
            raw = (row.get("correct_labels") or row.get("prelabels") or "")
            raw = raw.replace("，", ",").replace("、", ",")
            labels = [part.strip() for part in raw.split(",") if part.strip()]
            if not labels:
                continue
            samples.append(make_sample(id=row["id"], text=row["text"], labels=labels,
                                       origin="real", target_labels=labels))
    kept, dropped = dedupe(samples)
    write_jsonl(out, kept)
    print(f"真实训练集已写入：{len(kept)} 条（去重 {dropped} 条）→ {out}")
    return len(kept)


def run_split(synthetic_path: Path, out_dir: Path, *, real_path: Path | None = None,
              paraphrase_path: Path | None = None, seed: int = 42,
              report_dir: Path | None = None) -> dict[str, list[Sample]]:
    synthetic, synthetic_dropped = dedupe(read_jsonl(synthetic_path))
    real = read_jsonl(real_path) if real_path and real_path.exists() else []
    real, real_dropped = dedupe(real)
    paraphrases = read_jsonl(paraphrase_path) if paraphrase_path and paraphrase_path.exists() else []
    splits = assemble_splits(synthetic, real=real, paraphrases=paraphrases, seed=seed)
    for name, items in splits.items():
        write_jsonl(out_dir / f"{name}.jsonl", items)
    if report_dir is not None:
        write_report(report_dir, "dataset", _dataset_stats(
            synthetic, splits, {"real_train": len(real), "paraphrase_pool": len(paraphrases),
                                "dedupe_synthetic": synthetic_dropped,
                                "dedupe_real": real_dropped}))
    print("切分完成：" + "，".join(f"{name}={len(items)}" for name, items in splits.items()))
    return splits


def build_llm():
    from app.model import build_model

    return build_model(Settings.from_env())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ch10 主题分类语料构建")
    group = parser.add_mutually_exclusive_group(required=True)
    for flag in ("generate", "prelabel", "paraphrase", "audit-sample", "apply-audit",
                 "pull-real", "apply-real-audit", "split"):
        group.add_argument(f"--{flag}", action="store_true")
    parser.add_argument("--in", dest="in_path", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--real", type=Path, default=Path("data/topics/real_train.jsonl"))
    parser.add_argument("--paraphrase", type=Path, default=Path("data/topics/paraphrase_v1.jsonl"))
    parser.add_argument("--report-dir", type=Path, default=Path("evals/reports"))
    parser.add_argument("--count", type=int, default=TOTAL)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    if args.generate:
        if args.count != TOTAL:
            parser.error(f"--count 固定为 {TOTAL}")
        run_generate(build_llm(), args.out or Path("data/topics/synthetic_v1.jsonl"),
                     args.report_dir, seed=args.seed)
    elif args.prelabel:
        run_prelabel(build_llm(), args.in_path or Path("data/topics/synthetic_v1.jsonl"))
    elif args.paraphrase:
        run_paraphrase(build_llm(), args.in_path or Path("data/topics/synthetic_v1.jsonl"),
                       args.out or Path("data/topics/paraphrase_v1.jsonl"))
    elif args.audit_sample:
        export_audit(read_jsonl(args.in_path or Path("data/topics/synthetic_v1.jsonl")),
                     args.out or Path("evals/topics/audit_sample.csv"), seed=args.seed)
    elif args.apply_audit:
        run_apply_audit(args.in_path or Path("data/topics/synthetic_v1.jsonl"),
                        args.audit or Path("evals/topics/audit_corrections.jsonl"))
    elif args.pull_real:
        run_pull_real(Settings.from_env(), build_llm(),
                      args.out or Path("data/topics/real_audit.csv"))
    elif args.apply_real_audit:
        run_apply_real_audit(args.audit or Path("data/topics/real_audit.csv"),
                             args.out or args.real)
    else:
        run_split(args.in_path or Path("data/topics/synthetic_v1.jsonl"),
                  args.out or Path(".cache/topics/splits"), real_path=args.real,
                  paraphrase_path=args.paraphrase, seed=args.seed, report_dir=args.report_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 跑测试**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_ch10_build.py tests/test_ch10_dataset.py
```

预期：全部通过。若 `run_paraphrase` 的 `(kept, dropped)` 不是 `(1, 1)`，先打印两批 fake 输出对应的 prompt，检查 `par-0001/par-0002` 的 id 是否按过滤后的 seed 顺序编号。

- [ ] **Step 5: 追加 dev-notes 并提交**

在 dev-notes 记「配额算法裁定」：总条数 1200 优先，配额是每类支持数下限；300 条多标签贡献 600 个标签位，先补 600 条单标签配额缺口，再按类目轮转补 300 条到总量 1200。

```bash
git add app/topics/prompts.py scripts/build_topic_dataset.py tests/test_ch10_build.py dev-notes/ch10.md
git commit -m "feat(ch10): 语料生成与抽审 CLI"
```

---

## Task 7: 生成 1200 条合成语料、预标与改写池

> 数据任务，不写 pytest；用条数、配额、隐私正则与人工抽样验收。**LLM 失败时不许手写或复制填充凑数，停下来报告失败批次。**

**Files:**
- Create: `data/topics/synthetic_v1.jsonl`（提交）
- Create: `data/topics/paraphrase_v1.jsonl`（提交）
- Create: `evals/reports/ch10-dataset-<ts>.md`
- Modify: `dev-notes/ch10.md`

- [ ] **Step 1: 确认 LLM 配置可用**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -c "from app.config import Settings; s=Settings.from_env(); print(s.openai_base_url, s.openai_model); print('key_len', len(s.openai_api_key.get_secret_value()))"
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -c "from app.config import Settings; from app.model import build_model; m=build_model(Settings.from_env()); print(m.invoke('只回复 OK').content)"
```

第二条必须打印 `OK`。失败先查 `.env` 与网络；不要换模型、不要编语料。

- [ ] **Step 2: 生成**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m scripts.build_topic_dataset \
  --generate --count 1200 --out data/topics/synthetic_v1.jsonl
```

预期最后一行：`生成完成：1200 条 → data/topics/synthetic_v1.jsonl`。中途抛 `生成失败：['某类'] 还差 N 条` 或「去重掉 N 条」时：重跑同一命令；连续两次仍失败就把失败批次贴给用户，不继续。

- [ ] **Step 3: 校验条数、配额与隐私**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 - <<'PY'
import json, re
from collections import Counter
from pathlib import Path

rows = [json.loads(line) for line in Path("data/topics/synthetic_v1.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
assert len(rows) == 1200, len(rows)
assert sum(1 for row in rows if len(row["labels"]) > 1) == 300, "多标签必须是 300 条"
support = Counter(label for row in rows for label in row["labels"])
assert support["其他"] >= 80, support
assert all(support[label] >= 70 for label in support if label != "其他"), support
assert not [row for row in rows if re.search(r"1[3-9]\d{9}|\d{6,}", row["text"])]
assert all(4 <= len(row["text"]) <= 60 for row in rows)
print("每类支持数：", dict(support))
print("多标签：", sum(1 for row in rows if len(row["labels"]) > 1))
for label in ("退换货", "物流", "运费", "其他"):
    print(f"\n[{label}]")
    for row in [row for row in rows if label in row["labels"]][:5]:
        print(" ", row["text"], row["labels"])
PY
```

人工看四类各 5 条：必须像顾客口语、只含目标诉求、没有姓名/电话/地址/订单号。不像就回到 Prompt 调整后重生成，**不许直接开训**。

- [ ] **Step 4: 预标与改写池**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m scripts.build_topic_dataset \
  --prelabel --in data/topics/synthetic_v1.jsonl
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m scripts.build_topic_dataset \
  --paraphrase --in data/topics/synthetic_v1.jsonl --out data/topics/paraphrase_v1.jsonl
```

预标一致率 <80%：先改 `prelabel_prompt()`（补近邻例子）并重跑 `--prelabel`，不要把 `target_labels` 塞进预标 Prompt。改写池校验：

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 - <<'PY'
import json
from pathlib import Path

rows = [json.loads(line) for line in Path("data/topics/paraphrase_v1.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
assert rows and all(row.get("seed_id") for row in rows)
assert len({row["seed_id"] for row in rows}) == len(rows), "一个 seed 只能留一条改写"
assert all(row["text"] for row in rows)
print(f"改写池 {len(rows)} 条，覆盖 {len(rows) / 1200:.1%} 的合成样本")
PY
```

- [ ] **Step 5: 追加 dev-notes 并提交**

记：生成耗时、预标一致率、改写保留/丢弃数、人工看样本的结论。

```bash
git add data/topics/synthetic_v1.jsonl data/topics/paraphrase_v1.jsonl evals/reports/ch10-dataset-*.md dev-notes/ch10.md
git commit -m "data(ch10): 1200 条合成语料与改写池"
```

---

## Task 8: 人工抽审（人工检查点，必须停下等用户）

> 这是本章唯一的人工数据检查点。脚本只导出待填表，**不代替用户填 `correct_labels`**。

**Files:**
- Create: `evals/topics/audit_sample.csv`（提交）
- Create: `evals/topics/audit_corrections.jsonl`（提交）
- Create: `data/topics/real_audit.csv`（git-ignored；人工就地编辑，整表作为 apply 输入）
- Create: `data/topics/real_train.jsonl`（git-ignored）
- Modify: `data/topics/synthetic_v1.jsonl`（应用修正）
- Modify: `.gitignore`（加 `data/topics/real_*`，必须在拉真实数据前生效）
- Modify: `dev-notes/ch10.md`

- [ ] **Step 1: 导出合成抽审表**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m scripts.build_topic_dataset \
  --audit-sample --in data/topics/synthetic_v1.jsonl --out evals/topics/audit_sample.csv
```

- [ ] **Step 2: 导出真实池确认表**

先确认 `.gitignore` 含 `data/topics/real_*`，没有就加上（否则下一步会把 DB 原文带进仓库）。

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m scripts.build_topic_dataset \
  --pull-real --out data/topics/real_audit.csv
```

预期 13 行左右（17 行原始数据按清洗后文本去重）。如果打印 17 行，说明清洗后仍无重复，按实际行数继续。`data/topics/real_audit.csv` 含 DB 原文，确认 `.gitignore` 已忽略 `data/topics/real_*` 后再进行下一步。

- [ ] **Step 3: 停下来，请用户填表**

向用户发一条消息，原文包含：

> 合成语料抽审表在 `evals/topics/audit_sample.csv`（约 50 条），真实问题确认表在 `data/topics/real_audit.csv`（约 13 条）。请只填 `correct_labels`（逗号分隔，必须来自 17 类）和 `note`（为什么改）；同意预标就留空 `correct_labels`。填完告诉我，我再合并。

**停在这里，不执行 Step 4**，直到用户明确说填完。等待期间可以继续做 Task 9 的代码准备，但不能把未审数据当最终数据训练。

- [ ] **Step 4: 把 CSV 转成修正 JSONL 并校验标签**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 - <<'PY'
import csv, json
from pathlib import Path

from app.topics.taxonomy import LABELS


def convert(csv_path, jsonl_path):
    written = 0
    with Path(csv_path).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    with Path(jsonl_path).open("w", encoding="utf-8") as out:
        for row in rows:
            raw = (row.get("correct_labels") or "").replace("，", ",").replace("、", ",")
            labels = [part.strip() for part in raw.split(",") if part.strip()]
            if not labels:
                continue
            unknown = [label for label in labels if label not in LABELS]
            assert not unknown, f"{row['id']} 含未知标签 {unknown}"
            out.write(json.dumps({"id": row["id"], "labels": labels,
                                  "note": row.get("note", "")}, ensure_ascii=False) + "\n")
            written += 1
    print(f"{csv_path} → {jsonl_path}：{written} 条修正，{len(rows) - written} 条同意预标")


convert("evals/topics/audit_sample.csv", "evals/topics/audit_corrections.jsonl")
# 真实表不转 JSONL：--apply-real-audit 要读取整张 CSV（含 text 与 prelabels），
# 人工留空 correct_labels 表示同意预标；只改行值、不删行。
with Path("data/topics/real_audit.csv").open(encoding="utf-8-sig", newline="") as handle:
    for row in csv.DictReader(handle):
        raw = (row.get("correct_labels") or "").replace("，", ",").replace("、", ",")
        labels = [part.strip() for part in raw.split(",") if part.strip()]
        unknown = [label for label in labels if label not in LABELS]
        assert not unknown, f"{row['id']} 含未知标签 {unknown}"
print("真实确认表校验通过，保留原 CSV 供 --apply-real-audit 读取。")
PY
```

- [ ] **Step 5: 应用修正并生成真实训练集**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m scripts.build_topic_dataset \
  --apply-audit --in data/topics/synthetic_v1.jsonl --audit evals/topics/audit_corrections.jsonl
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m scripts.build_topic_dataset \
  --apply-real-audit --audit data/topics/real_audit.csv --out data/topics/real_train.jsonl
```

真实表整行 `correct_labels` 留空视为同意预标（`--apply-real-audit` 回落到 `prelabels`）；只有 `prelabels` 也空才跳过。跳过数记进 dev-notes。

- [ ] **Step 6: 追加 dev-notes 并提交（真实文件不入库）**

```bash
git add data/topics/synthetic_v1.jsonl evals/topics/audit_sample.csv evals/topics/audit_corrections.jsonl .gitignore dev-notes/ch10.md
git status --short data/topics/
git commit -m "data(ch10): 合成语料人工抽审结果"
```

`git status --short data/topics/` 里不得出现 `real_audit.csv`、`real_train.jsonl`；出现就先补 `.gitignore` 再提交。

---

## Task 9: 切分与增强组装

**Files:**
- Create: `.cache/topics/splits/{train,val,test}.jsonl`（git-ignored）
- Create: `evals/reports/ch10-dataset-<ts>.md`
- Modify: `dev-notes/ch10.md`

- [ ] **Step 1: 跑切分**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m scripts.build_topic_dataset \
  --split --in data/topics/synthetic_v1.jsonl --seed 42 --out .cache/topics/splits \
  --real data/topics/real_train.jsonl --paraphrase data/topics/paraphrase_v1.jsonl
```

预期：`train≈960+真实 13+增强`、`val≈120`、`test≈120`；训练集因同义词替换与 30% 改写会变多，val/test 只来自合成语料。

- [ ] **Step 2: 校验切分无泄漏**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 - <<'PY'
import json
from pathlib import Path

def load(name):
    path = Path(".cache/topics/splits") / f"{name}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

parts = {name: load(name) for name in ("train", "val", "test")}
for name in ("val", "test"):
    assert all(row["origin"] != "paraphrase" for row in parts[name]), name
train_seed_ids = {row.get("seed_id") for row in parts["train"] if row["origin"] == "paraphrase"}
val_ids = {row["id"] for row in parts["val"]}
test_ids = {row["id"] for row in parts["test"]}
assert not (train_seed_ids & (val_ids | test_ids)), "改写挂在验证/测试样本上了"
assert all(row["labels"] for row in parts["train"] + parts["val"] + parts["test"])
print({name: len(rows) for name, rows in parts.items()})
print("val 类目覆盖：", sorted({label for row in parts["val"] for label in row["labels"]}))
print("test 类目覆盖：", sorted({label for row in parts["test"] for label in row["labels"]}))
PY
```

类目覆盖必须为 17；不足说明切分或语料有问题，先查再继续。

- [ ] **Step 3: 追加 dev-notes**

记：三份条数、真实 13 条是否全进 train、改写挂载条数、同义词增强条数、17 类覆盖。

- [ ] **Step 4: 提交报告（切分产物不入库）**

```bash
git add evals/reports/ch10-dataset-*.md dev-notes/ch10.md
git commit -m "data(ch10): 分层切分与训练集增强"
```

---

## Task 10: 模型封装与阈值回落

**Files:**
- Create: `app/topics/model.py`
- Create: `tests/test_ch10_model.py`

**Interfaces:**
- Produces: `ModelNotReady(RuntimeError)`、`TopicPrediction(labels: list[str], scores: dict[str, float])`
- Produces: `apply_thresholds(probs: Sequence[float], thresholds: Sequence[float]) -> list[str]`
- Produces: `TopicModel(model_dir, *, device="cpu", max_len=128)`，方法 `predict(texts) -> list[TopicPrediction]`、静态 `is_ready(model_dir) -> bool`
- Consumes: `app.topics.taxonomy.LABELS/OTHER/order_labels`

- [ ] **Step 1: 写失败的纯函数测试**

创建 `tests/test_ch10_model.py`：

```python
import pytest

from app.topics.model import ModelNotReady, TopicModel, apply_thresholds
from app.topics.taxonomy import LABELS, OTHER


def probs(**hits):
    return [hits.get(label, 0.1) for label in LABELS]


def test_apply_thresholds_multi_fallback_and_boundary():
    assert apply_thresholds(probs(退换货=0.9, 尺码=0.8), [0.5] * 17) == ["退换货", "尺码"]
    assert apply_thresholds(probs(), [0.5] * 17) == [OTHER]
    # 等于阈值算命中
    assert apply_thresholds(probs(物流=0.5), [0.5] * 17) == ["物流"]
    assert apply_thresholds(probs(物流=0.4999), [0.5] * 17) == [OTHER]


def test_apply_thresholds_rejects_wrong_length():
    with pytest.raises(ValueError):
        apply_thresholds([0.1], [0.5])
    with pytest.raises(ValueError):
        apply_thresholds([0.1] * 17, [0.5])


def test_is_ready_requires_labels_thresholds_and_weights(tmp_path):
    assert TopicModel.is_ready(tmp_path) is False
    (tmp_path / "labels.json").write_text("[]", encoding="utf-8")
    (tmp_path / "thresholds.json").write_text("{}", encoding="utf-8")
    assert TopicModel.is_ready(tmp_path) is False
    (tmp_path / "model.safetensors").write_text("", encoding="utf-8")
    assert TopicModel.is_ready(tmp_path) is True
    (tmp_path / "model.safetensors").unlink()
    (tmp_path / "pytorch_model.bin").write_text("", encoding="utf-8")
    assert TopicModel.is_ready(tmp_path) is True


def test_missing_artifacts_raise_model_not_ready(tmp_path):
    with pytest.raises(ModelNotReady):
        TopicModel(tmp_path)
```

运行：

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_ch10_model.py
```

预期：`ModuleNotFoundError: No module named 'app.topics.model'`。

- [ ] **Step 2: 实现 `model.py`**

```python
"""加载微调权重、阈值判定与批量预测；torch/transformers 只在构造时导入。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from app.topics.taxonomy import LABELS, OTHER, order_labels


class ModelNotReady(RuntimeError):
    pass


@dataclass(frozen=True)
class TopicPrediction:
    labels: list[str]
    scores: dict[str, float]


def apply_thresholds(probs: Sequence[float], thresholds: Sequence[float]) -> list[str]:
    if len(probs) != len(LABELS) or len(thresholds) != len(LABELS):
        raise ValueError("概率与阈值长度必须等于 17")
    hits = [LABELS[index] for index, (prob, threshold) in enumerate(zip(probs, thresholds))
            if prob >= threshold]
    return hits or [OTHER]


class TopicModel:
    def __init__(self, model_dir: Path | str, *, device: str = "cpu", max_len: int = 128):
        self.model_dir = Path(model_dir)
        self.max_len = max_len
        if not self.is_ready(self.model_dir):
            raise ModelNotReady(f"模型文件不完整：{self.model_dir}")
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        labels = json.loads((self.model_dir / "labels.json").read_text(encoding="utf-8"))
        if labels != list(LABELS):
            raise ModelNotReady("labels.json 与 17 类术语表不一致")
        raw = json.loads((self.model_dir / "thresholds.json").read_text(encoding="utf-8"))
        self.thresholds = [float(raw[label]) for label in LABELS]
        self._torch = torch
        self.device = device
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_dir, local_files_only=True)
        self.model = AutoModelForSequenceClassification.from_pretrained(
            self.model_dir, local_files_only=True).to(device)
        self.model.eval()

    @staticmethod
    def is_ready(model_dir: Path | str) -> bool:
        root = Path(model_dir)
        has_meta = all((root / name).is_file() for name in ("labels.json", "thresholds.json"))
        has_weights = any((root / name).is_file()
                          for name in ("model.safetensors", "pytorch_model.bin"))
        return has_meta and has_weights

    def predict(self, texts: list[str]) -> list[TopicPrediction]:
        if not 1 <= len(texts) <= 64:
            raise ValueError("一次预测 1~64 条")
        predictions = []
        with self._torch.no_grad():
            for start in range(0, len(texts), 16):
                batch = texts[start:start + 16]
                encoded = self.tokenizer(batch, padding=True, truncation=True,
                                         max_length=self.max_len, return_tensors="pt")
                encoded = {key: value.to(self.device) for key, value in encoded.items()}
                probs = self._torch.sigmoid(self.model(**encoded).logits).cpu().tolist()
                for row in probs:
                    predictions.append(TopicPrediction(
                        labels=order_labels(apply_thresholds(row, self.thresholds)),
                        scores={label: round(float(score), 4)
                                for label, score in zip(LABELS, row)},
                    ))
        return predictions
```

- [ ] **Step 3: 跑测试并提交**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_ch10_model.py
git add app/topics/model.py tests/test_ch10_model.py dev-notes/ch10.md
git commit -m "feat(ch10): 分类模型封装与阈值回落"
```

---

## Task 11: 旁路推理服务与启动脚本

**Files:**
- Create: `app/topics/service.py`
- Create: `start-topic.bat`（ASCII + CRLF）
- Create: `tests/test_ch10_service.py`

**Interfaces:**
- Produces: `create_app(model_dir=None, predictor=None, *, settings=None) -> FastAPI`
- HTTP: `GET /health`、`POST /classify`
- Consumes: Task 10 的 `TopicModel/ModelNotReady/TopicPrediction`

- [ ] **Step 1: 写失败的服务测试**

创建 `tests/test_ch10_service.py`：

```python
from fastapi.testclient import TestClient

from app.topics.model import TopicPrediction
from app.topics.service import create_app
from app.topics.taxonomy import LABELS
from tests.support import _settings


class FakePredictor:
    def __init__(self):
        self.calls = []

    def predict(self, texts):
        self.calls.append(texts)
        return [TopicPrediction(labels=["尺码", "退换货"],
                                scores={label: 0.5 for label in LABELS}) for _ in texts]


def test_health_and_classify_order_labels():
    fake = FakePredictor()
    client = TestClient(create_app(predictor=fake))
    assert client.get("/health").json()["status"] == "ok"
    body = client.post("/classify", json={"texts": ["买大了想退", "运费怎么算"]}).json()
    assert body["items"][0]["labels"] == ["退换货", "尺码"]
    assert list(body["items"][0]["scores"]) == list(LABELS)
    assert fake.calls == [["买大了想退", "运费怎么算"]]


def test_validation_returns_422():
    client = TestClient(create_app(predictor=FakePredictor()))
    for texts in ([], ["   "], ["x"] * 65, ["长" * 20001]):
        assert client.post("/classify", json={"texts": texts}).status_code == 422


def test_not_ready_model_returns_503(tmp_path):
    settings = _settings({"topic_model_dir": str(tmp_path)})
    client = TestClient(create_app(model_dir=tmp_path, settings=settings))
    assert client.get("/health").json()["status"] == "not_ready"
    response = client.post("/classify", json={"texts": ["在吗"]})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "model_not_ready"
```

- [ ] **Step 2: 实现 `service.py`**

```python
"""旁路主题分类推理服务；默认 CPU，端口 8933。"""

from __future__ import annotations

import argparse
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.topics.model import ModelNotReady, TopicModel
from app.topics.taxonomy import LABELS, order_labels


class ClassifyRequest(BaseModel):
    texts: list[str]


def create_app(model_dir=None, predictor=None, *, settings=None) -> FastAPI:
    resolved_dir = Path(model_dir) if model_dir else None
    device, max_len = "cpu", 128
    state = {"predictor": predictor, "error": None}
    if predictor is None:
        from app.config import Settings

        settings = settings or Settings.from_env()
        resolved_dir = resolved_dir or Path(settings.topic_model_dir)
        device, max_len = settings.topic_device, settings.topic_max_len
        try:
            state["predictor"] = TopicModel(resolved_dir, device=device, max_len=max_len)
        except ModelNotReady as exc:
            state["error"] = str(exc)
    resolved_dir = resolved_dir or Path(".cache/models/topic-classifier")

    app = FastAPI(title="ch10 主题分类推理服务")

    @app.get("/health")
    def health():
        return {
            "status": "ok" if state["predictor"] is not None else "not_ready",
            "model_dir": str(resolved_dir),
            "labels": len(LABELS),
            "device": device,
        }

    @app.post("/classify")
    def classify(body: ClassifyRequest):
        texts = [text.strip() for text in body.texts]
        if (not 1 <= len(texts) <= 64 or any(not text for text in texts)
                or sum(len(text) for text in texts) > 20000):
            raise HTTPException(status_code=422,
                                detail="texts 需要 1~64 条非空文本，总长度不超过 20000 字符")
        if state["predictor"] is None:
            raise HTTPException(status_code=503, detail={
                "code": "model_not_ready",
                "message": "分类模型还没训练好，请先运行 scripts/train_topic_classifier.py。",
            })
        try:
            predictions = state["predictor"].predict(texts)
        except Exception:
            raise HTTPException(status_code=503, detail={
                "code": "predict_failed",
                "message": "分类推理失败，请查看推理服务日志。",
            }) from None
        return {"items": [
            {"labels": order_labels(prediction.labels),
             "scores": {label: prediction.scores[label] for label in LABELS}}
            for prediction in predictions
        ]}

    return app


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ch10 主题分类推理服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8933)
    parser.add_argument("--model-dir", type=Path)
    args = parser.parse_args(argv)
    import uvicorn

    uvicorn.run(create_app(args.model_dir), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: 写 `start-topic.bat`（必须保存为 CRLF）**

```bat
@echo off
cd /d "%~dp0"
set HF_ENDPOINT=https://hf-mirror.com
echo 启动主题分类推理服务：http://127.0.0.1:8933
".venv\Scripts\python.exe" -X utf8 -m app.topics.service --host 127.0.0.1 --port 8933
pause
```

保存后确认换行：

```bash
python -c "data=open('start-topic.bat','rb').read(); print(b'\\r\\n' in data, data.count(b'\\n'))"
```

- [ ] **Step 4: 跑测试并提交**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_ch10_service.py tests/test_ch10_model.py
git add app/topics/service.py tests/test_ch10_service.py start-topic.bat dev-notes/ch10.md
git commit -m "feat(ch10): 旁路推理服务"
```

---

## Task 12: 批量归类、写表与分布统计

**Files:**
- Create: `app/topics/worker.py`
- Create: `app/topics/stats.py`
- Create: `scripts/classify_topics.py`
- Create: `tests/test_ch10_worker.py`

**Interfaces:**
- Produces: `ClassifyReport(pending, classified, failed)`
- Produces: `pending_count(session_factory) -> int`、`pending_questions(session_factory, limit, *, force=False) -> list[tuple[int, str]]`
- Produces: `store_classifications(session_factory, results: dict[int, list[str]], *, force=False) -> int`
- Produces: `run_batch(session_factory, classify, *, min_pending=20, limit=50, force=False) -> ClassifyReport`
- Produces: `TopicClient(base_url, timeout=30.0)`，可调用 `client(texts) -> list[list[str]]`
- Produces: `distribution(session_factory) -> dict`（17 类固定顺序）

- [ ] **Step 1: 写失败的 worker/stats 测试**

创建 `tests/test_ch10_worker.py`：

```python
import pytest

from app.models import LowConfidenceQuestion, TopicClassification
from app.topics.stats import distribution
from app.topics.taxonomy import LABELS
from app.topics.worker import (
    ClassifyReport, pending_count, pending_questions, run_batch, store_classifications,
)
from tests.support import sqlite_factory


def seed(factory, text="怎么查物流"):
    with factory() as session:
        row = LowConfidenceQuestion(raw_question=text, source="self_check")
        session.add(row)
        session.commit()
        return row.id


def test_pending_only_unclassified():
    with sqlite_factory() as factory:
        first = seed(factory, "怎么查物流")
        second = seed(factory, "运费怎么算")
        seed(factory, "   ")  # 空白文本无法归类，不能占 pending 名额
        assert pending_count(factory) == 2
        assert [qid for qid, _ in pending_questions(factory, 10)] == [first, second]
        store_classifications(factory, {first: ["物流"]})
        assert pending_count(factory) == 1
        assert [qid for qid, _ in pending_questions(factory, 10)] == [second]
        assert {qid for qid, _ in pending_questions(factory, 10, force=True)} == {first, second}


def test_store_is_idempotent_and_force_updates():
    with sqlite_factory() as factory:
        qid = seed(factory)
        assert store_classifications(factory, {qid: ["物流"]}) == 1
        assert store_classifications(factory, {qid: ["运费"]}) == 0
        with factory() as session:
            assert session.query(TopicClassification).one().labels == ["物流"]
        assert store_classifications(factory, {qid: ["运费"]}, force=True) == 1
        with factory() as session:
            assert session.query(TopicClassification).one().labels == ["运费"]


def test_store_empty_labels_falls_back_to_other_and_rejects_unknown():
    with sqlite_factory() as factory:
        qid = seed(factory)
        store_classifications(factory, {qid: []})
        with factory() as session:
            assert session.query(TopicClassification).one().labels == ["其他"]
        with pytest.raises(ValueError, match="未知主题标签"):
            store_classifications(factory, {seed(factory, "第二问"): ["退款"]})


def test_run_batch_skips_below_threshold_and_handles_failure():
    with sqlite_factory() as factory:
        seed(factory)
        calls = []
        report = run_batch(factory, lambda texts: calls.append(texts) or [["物流"]], min_pending=2)
        assert report == ClassifyReport(pending=1) and calls == []

        def boom(texts):
            raise RuntimeError("service down")

        report = run_batch(factory, boom, min_pending=1)
        assert report.failed == 1 and report.classified == 0


def test_run_batch_counts_unknown_labels_as_failed():
    with sqlite_factory() as factory:
        first, second = seed(factory, "第一问"), seed(factory, "第二问")
        report = run_batch(factory, lambda texts: [["物流"], ["退款"]], min_pending=1, limit=2)
        assert report.classified == 1 and report.failed == 1
        with factory() as session:
            assert session.query(TopicClassification).one().question_id == first


def test_distribution_fixed_order_and_counts():
    with sqlite_factory() as factory:
        first, second, third = seed(factory, "第一问"), seed(factory, "第二问"), seed(factory, "第三问")
        store_classifications(factory, {first: ["物流", "运费"], second: ["其他"]})
        data = distribution(factory)
        assert data["total_classified"] == 2
        assert data["pending"] == 1
        assert data["multi_label"] == 1
        assert [row["label"] for row in data["classes"]] == list(LABELS)
        by_label = {row["label"]: row for row in data["classes"]}
        assert by_label["物流"]["count"] == 1 and by_label["物流"]["pct"] == 0.5
        assert by_label["其他"]["count"] == 1


def test_distribution_empty_database():
    with sqlite_factory() as factory:
        data = distribution(factory)
        assert data["total_classified"] == 0 and data["pending"] == 0
        assert data["multi_label"] == 0 and data["last_classified_at"] is None
        assert len(data["classes"]) == 17
        assert all(row["count"] == 0 and row["pct"] == 0.0 for row in data["classes"])
```

- [ ] **Step 2: 实现 `worker.py`**

```python
"""待归类查询、推理服务客户端、批量写表。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models import LowConfidenceQuestion, TopicClassification
from app.topics.taxonomy import OTHER, validate_labels

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClassifyReport:
    pending: int = 0
    classified: int = 0
    failed: int = 0


def _unclassified():
    # 空白文本永远无法归类，留在池里会让 pending_count 永远降不下去。
    return (select(LowConfidenceQuestion.id)
            .outerjoin(TopicClassification,
                       TopicClassification.question_id == LowConfidenceQuestion.id)
            .where(TopicClassification.id.is_(None))
            .where(func.trim(LowConfidenceQuestion.raw_question) != ""))


def pending_count(session_factory) -> int:
    with session_factory() as session:
        return session.scalar(select(func.count()).select_from(_unclassified().subquery())) or 0


def pending_questions(session_factory, limit: int, *, force: bool = False) -> list[tuple[int, str]]:
    statement = (select(LowConfidenceQuestion.id, LowConfidenceQuestion.raw_question)
                 .where(func.trim(LowConfidenceQuestion.raw_question) != ""))
    if not force:
        statement = statement.where(LowConfidenceQuestion.id.in_(_unclassified()))
    with session_factory() as session:
        result = session.execute(
            statement.order_by(LowConfidenceQuestion.id).limit(limit)).all()
    return [(int(qid), (raw or "").strip()) for qid, raw in result if (raw or "").strip()]


def store_classifications(session_factory, results: dict[int, list[str]], *,
                          force: bool = False) -> int:
    if not results:
        return 0
    normalized = {qid: validate_labels(labels or [OTHER]) for qid, labels in results.items()}
    try:
        with session_factory() as session:
            existing = {row.question_id: row for row in session.scalars(
                select(TopicClassification)
                .where(TopicClassification.question_id.in_(list(normalized))))}
            written = 0
            for question_id, labels in normalized.items():
                row = existing.get(question_id)
                if row is None:
                    session.add(TopicClassification(question_id=question_id, labels=labels))
                    written += 1
                elif force:
                    row.labels = labels
                    row.classified_at = func.now()
                    written += 1
            session.commit()
            return written
    except IntegrityError:
        logger.warning("主题归类撞唯一键，本批回滚。", exc_info=True)
        return 0


class TopicClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def __call__(self, texts: list[str]) -> list[list[str]]:
        response = self._client.post("/classify", json={"texts": texts})
        response.raise_for_status()
        items = response.json().get("items")
        if not isinstance(items, list) or len(items) != len(texts):
            raise ValueError("推理服务返回条数不匹配")
        return [list(item.get("labels") or []) for item in items]

    def close(self) -> None:
        self._client.close()


def run_batch(session_factory, classify: Callable[[list[str]], list[list[str]]], *,
              min_pending: int = 20, limit: int = 50,
              force: bool = False) -> ClassifyReport:
    pending = pending_count(session_factory)
    if not force and pending < min_pending:
        return ClassifyReport(pending=pending)
    rows = pending_questions(session_factory, limit, force=force)
    if not rows:
        return ClassifyReport(pending=pending)
    try:
        labels = classify([text for _, text in rows])
    except Exception:
        logger.warning("主题归类服务调用失败，本批跳过。", exc_info=True)
        return ClassifyReport(pending=pending, failed=len(rows))
    if len(labels) != len(rows):
        logger.warning("主题归类返回 %s 条，期望 %s 条。", len(labels), len(rows))
        return ClassifyReport(pending=pending, failed=len(rows))
    results, failed = {}, 0
    for (question_id, _), item in zip(rows, labels):
        try:
            results[question_id] = validate_labels(item or [OTHER])
        except ValueError:
            failed += 1
    written = store_classifications(session_factory, results, force=force)
    return ClassifyReport(pending=pending, classified=written,
                          failed=failed + (len(results) - written))
```

- [ ] **Step 3: 实现 `stats.py`**

```python
"""主题分布统计；JSON 在 Python 侧解析，MySQL/SQLite 同路。"""

from __future__ import annotations

from sqlalchemy import func

from app.models import LowConfidenceQuestion, TopicClassification
from app.topics.taxonomy import LABELS, OTHER, order_labels


def distribution(session_factory) -> dict:
    with session_factory() as session:
        rows = session.query(TopicClassification).all()
        # 与 worker.pending_count 口径一致：空白问题不参与待归类统计。
        pool_total = (session.query(LowConfidenceQuestion)
                      .filter(func.trim(LowConfidenceQuestion.raw_question) != "").count())
    counts = {label: 0 for label in LABELS}
    multi_label = 0
    last = None
    for row in rows:
        labels = order_labels([label for label in (row.labels or []) if label in counts]) or [OTHER]
        multi_label += 1 if len(labels) > 1 else 0
        for label in labels:
            counts[label] += 1
        if row.classified_at is not None and (last is None or row.classified_at > last):
            last = row.classified_at
    total = len(rows)
    return {
        "ok": True,
        "total_classified": total,
        "pending": max(pool_total - total, 0),
        "multi_label": multi_label,
        "last_classified_at": last.isoformat() if last else None,
        "classes": [
            {"label": label, "count": counts[label],
             "pct": round(counts[label] / total, 4) if total else 0.0}
            for label in LABELS
        ],
    }
```

- [ ] **Step 4: 实现 `scripts/classify_topics.py`**

```python
"""手动批量归类：与后台调度共用 run_batch。"""

from __future__ import annotations

import argparse

from app.config import Settings
from app.database import build_database_url, build_engine, build_session_factory
from app.topics.worker import TopicClient, run_batch


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ch10 手动批量主题归类")
    parser.add_argument("--min-pending", type=int, default=1)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    engine = build_engine(build_database_url(settings))
    client = TopicClient(settings.topic_service_url)
    try:
        report = run_batch(build_session_factory(engine), client,
                           min_pending=args.min_pending, limit=args.limit, force=args.force)
    finally:
        client.close()
        engine.dispose()
    print(f"待归类 {report.pending}，已归类 {report.classified}，失败 {report.failed}")
    if report.failed and not report.classified:
        print("推理服务不可达：请先双击 start-topic.bat。")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: 跑测试并提交**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_ch10_worker.py
git add app/topics/worker.py app/topics/stats.py scripts/classify_topics.py tests/test_ch10_worker.py dev-notes/ch10.md
git commit -m "feat(ch10): 批量归类写表与主题分布统计"
```

---

## Task 13: 后台调度、主应用接线与分布接口

**Files:**
- Create: `app/topics/scheduler.py`
- Modify: `app/main.py`（lifespan 构造/关闭调度器；新增分布接口）
- Create: `tests/test_ch10_admin_api.py`

**Interfaces:**
- Produces: `TopicScheduler(job, interval_minutes)`，`start()/shutdown()`
- Produces: `GET /api/admin/topics/distribution`（响应结构见规格 §11.1）
- Consumes: Task 12 的 `TopicClient/run_batch/distribution`

- [ ] **Step 1: 写失败的管理接口测试**

创建 `tests/test_ch10_admin_api.py`：

```python
import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.models import LowConfidenceQuestion
from app.topics.taxonomy import LABELS
from app.topics.worker import store_classifications
from tests.support import api_with_db, streamed


def noop(request):
    return streamed()


def seed(factory, text):
    with factory() as session:
        row = LowConfidenceQuestion(raw_question=text, source="self_check")
        session.add(row)
        session.commit()
        return row.id


def test_distribution_counts_percentages_and_order():
    with api_with_db(noop) as (client, _, factory):
        first, second, third = seed(factory, "怎么查物流"), seed(factory, "运费怎么算"), seed(factory, "在吗")
        store_classifications(factory, {first: ["物流", "运费"], second: ["其他"]})
        body = client.get("/api/admin/topics/distribution").json()
        assert body["total_classified"] == 2 and body["pending"] == 1
        assert body["multi_label"] == 1
        assert [row["label"] for row in body["classes"]] == list(LABELS)
        by_label = {row["label"]: row for row in body["classes"]}
        assert by_label["物流"]["count"] == 1 and by_label["物流"]["pct"] == 0.5
        assert by_label["其他"]["count"] == 1
        assert body["last_classified_at"] is not None


def test_distribution_empty_database_is_all_zero():
    with api_with_db(noop) as (client, _, _factory):
        body = client.get("/api/admin/topics/distribution").json()
        assert body["total_classified"] == 0 and body["pending"] == 0
        assert body["multi_label"] == 0 and body["last_classified_at"] is None
        assert len(body["classes"]) == 17
        assert all(row["count"] == 0 and row["pct"] == 0.0 for row in body["classes"])


def test_distribution_db_failure_is_503(monkeypatch):
    import app.topics.stats as topic_stats

    def boom(factory):
        raise SQLAlchemyError("db down")

    monkeypatch.setattr(topic_stats, "distribution", boom)
    with api_with_db(noop) as (client, _, _factory):
        response = client.get("/api/admin/topics/distribution")
        assert response.status_code == 503
        assert response.json()["code"] == "database_unavailable"
```

运行：

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_ch10_admin_api.py
```

预期：404（接口还没加）。

- [ ] **Step 2: 实现 `scheduler.py`**

```python
"""进程内定时主题归类；照 EvalScheduler 的形状。"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class TopicScheduler:
    def __init__(self, job, interval_minutes: int):
        self.job = job
        self.interval_minutes = interval_minutes
        self._scheduler = None

    async def start(self) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        try:
            scheduler.add_job(self._run, "interval", minutes=self.interval_minutes,
                              id="topic-classify", max_instances=1, coalesce=True)
            scheduler.start()
        except Exception:
            if scheduler.running:
                scheduler.shutdown(wait=False)
            raise
        self._scheduler = scheduler
        logger.info("主题归类定时任务已启动，间隔 %s 分钟。", self.interval_minutes)

    async def _run(self) -> None:
        try:
            await asyncio.to_thread(self.job)
        except Exception:
            logger.exception("主题归类失败，等待下一轮。")

    def shutdown(self) -> None:
        from apscheduler.schedulers.base import SchedulerNotRunningError

        scheduler, self._scheduler = self._scheduler, None
        if scheduler is None:
            return
        try:
            scheduler.shutdown(wait=False)
        except SchedulerNotRunningError:
            logger.debug("主题归类调度器未运行，跳过 shutdown。")
```

- [ ] **Step 3: 接进 `app/main.py`**

顶部 import 区加：

```python
from app.topics import stats as topic_stats
```

在 `scheduler = None` / `eval_scheduler = None` 旁加：

```python
        topic_scheduler = None
        topic_client = None
```

在 `if resolved_settings.eval_schedule_cron:` 整块之后、`yield` 之前加：

```python
                if resolved_settings.topic_classify_enabled:
                    try:
                        from app.topics.scheduler import TopicScheduler
                        from app.topics.worker import TopicClient, run_batch

                        topic_client = TopicClient(resolved_settings.topic_service_url)

                        def run_topic_batch():
                            report = run_batch(
                                factory, topic_client,
                                min_pending=resolved_settings.topic_classify_min_pending,
                                limit=resolved_settings.topic_classify_batch,
                            )
                            logger.info("主题归类：待归类 %s，已归类 %s，失败 %s。",
                                        report.pending, report.classified, report.failed)

                        topic_scheduler = TopicScheduler(
                            run_topic_batch, resolved_settings.topic_classify_interval_minutes)
                        await topic_scheduler.start()
                    except Exception:
                        topic_scheduler = None
                        logger.exception("主题归类定时任务启动失败，服务继续运行。")
```

`finally` 里、`eval_scheduler.shutdown()` 之后加：

```python
                if topic_scheduler is not None:
                    topic_scheduler.shutdown()
                if topic_client is not None:
                    topic_client.close()
```

在 `/api/admin/review-queue` 路由之后加：

```python
    @application.get("/api/admin/topics/distribution")
    async def admin_topic_distribution(request: Request):
        factory = request.app.state.service.store.session_factory
        try:
            data = await asyncio.to_thread(topic_stats.distribution, factory)
        except (SQLAlchemyError, sqlite3.Error):
            raise ModelFailure("database_unavailable", DB_FAILURE_MESSAGE, 503) from None
        return data
```

注意：必须调用 `topic_stats.distribution`（模块属性），不要 `from app.topics.stats import distribution`，否则测试的 monkeypatch 不生效。

- [ ] **Step 4: 跑测试与全量回归**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_ch10_admin_api.py tests/test_ch10_worker.py tests/test_ch10_model.py tests/test_ch10_service.py
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q
```

预期：新增用例全过，全量无回归（默认 `topic_classify_enabled=False`，旧用例不会连推理服务）。

- [ ] **Step 5: 追加 dev-notes 并提交**

```bash
git add app/topics/scheduler.py app/main.py tests/test_ch10_admin_api.py dev-notes/ch10.md
git commit -m "feat(ch10): 后台归类调度与主题分布接口"
```

---

## Task 14: 主题分布页（Vibe Coding，不套 TDD/review）

**Files:**
- Create: `app/web/admin-topics.html`
- Create: `app/web/admin-topics.js`
- Modify: `app/web/admin.html`（顶部加「主题分布」链接）
- Modify: `app/web/admin.css`（条形图与摘要卡样式）
- Modify: `app/main.py`（`/admin/topics` 返回页面）

- [ ] **Step 1: 加页面路由**

在 `/admin` 路由后加：

```python
    @application.get("/admin/topics", response_class=FileResponse, include_in_schema=False)
    def topics_page():
        return FileResponse(web_directory / "admin-topics.html")
```

- [ ] **Step 2: 写页面骨架**

`app/web/admin-topics.html`：

```html
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>主题分布 · deservehy客服</title>
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='10' fill='%23164d46'/%3E%3Cpath d='M8 9h16v12H13l-5 4V9z' fill='none' stroke='%23d6efda' stroke-width='2'/%3E%3C/svg%3E">
  <link rel="stylesheet" href="/assets/admin.css">
  <script src="/assets/admin-topics.js" defer></script>
</head>
<body>
  <header class="topbar">
    <div class="topbar-title">
      <a class="back" href="/admin">← 回到审核</a>
      <h1>主题分布</h1>
      <p>低置信问题按 17 类主题归类后，先补量大的那几类。</p>
    </div>
    <a class="refresh" href="/">回到聊天</a>
  </header>

  <main class="workspace">
    <section class="summary" aria-label="归类概况">
      <div class="summary-card"><span id="total-classified">—</span><small>已归类</small></div>
      <div class="summary-card"><span id="pending">—</span><small>待归类</small></div>
      <div class="summary-card"><span id="multi-label">—</span><small>多标签</small></div>
      <div class="summary-card"><span id="last-classified">—</span><small>最近归类</small></div>
    </section>

    <div class="toolbar">
      <p class="status-line" id="status-line" role="status" aria-live="polite"></p>
      <button type="button" class="refresh" id="refresh">刷新</button>
    </div>

    <section class="topic-bars" id="bars" aria-label="17 类主题分布"></section>
    <p class="empty" id="empty" hidden>还没有归类结果。启动推理服务后运行
      <code>python -m scripts.classify_topics</code>，或打开 <code>TOPIC_CLASSIFY_ENABLED</code>
      等后台自动归类。</p>
  </main>
</body>
</html>
```

- [ ] **Step 3: 写 `admin-topics.js`**

```javascript
const $ = (selector) => document.querySelector(selector);
let loading = false;

function render(data) {
  $("#total-classified").textContent = data.total_classified;
  $("#pending").textContent = data.pending;
  $("#multi-label").textContent = data.multi_label;
  $("#last-classified").textContent = data.last_classified_at
    ? data.last_classified_at.slice(0, 16).replace("T", " ")
    : "—";
  const max = Math.max(1, ...data.classes.map((row) => row.count));
  const bars = $("#bars");
  bars.replaceChildren(...data.classes.map((row) => {
    const item = document.createElement("div");
    item.className = "topic-bar-row";
    const label = document.createElement("span");
    label.className = "topic-bar-label";
    label.textContent = row.label;
    const track = document.createElement("span");
    track.className = "topic-bar-track";
    const fill = document.createElement("span");
    fill.className = "topic-bar-fill";
    fill.style.width = `${Math.round((row.count / max) * 100)}%`;
    track.append(fill);
    const value = document.createElement("span");
    value.className = "topic-bar-value";
    value.textContent = `${row.count} · ${(row.pct * 100).toFixed(1)}%`;
    item.append(label, track, value);
    return item;
  }));
  bars.hidden = data.total_classified === 0;
  $("#empty").hidden = data.total_classified > 0;
}

async function load() {
  if (loading) return;
  loading = true;
  const status = $("#status-line");
  status.textContent = "正在读取主题分布…";
  try {
    const response = await fetch("/api/admin/topics/distribution");
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(body.message || (response.status === 503
        ? "数据库暂时不可用" : "加载失败"));
    }
    render(body);
    status.textContent = "";
  } catch (error) {
    status.textContent = `暂时读不到主题分布：${error.message}。请确认后台和数据库已启动。`;
  } finally {
    loading = false;
  }
}

$("#refresh").addEventListener("click", load);
load();
```

- [ ] **Step 4: 加样式与审核页导航**

`admin.css` 末尾追加（可按现有变量调整颜色，保持同一套视觉）：

```css
.summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.summary-card { background: #fff; border: 1px solid #dce5e2; border-radius: 14px; padding: 16px; }
.summary-card span { display: block; font-size: 28px; font-weight: 700; color: #164d46; }
.summary-card small { color: #5f6f6a; }
.topic-bars { display: grid; gap: 10px; margin-top: 18px; }
.topic-bar-row { display: grid; grid-template-columns: 88px 1fr 96px; align-items: center; gap: 12px; }
.topic-bar-track { background: #e8efec; border-radius: 999px; height: 18px; overflow: hidden; }
.topic-bar-fill { display: block; height: 100%; background: linear-gradient(90deg, #2e7d6e, #6fbf9d); border-radius: 999px; }
.topic-bar-value { text-align: right; color: #40524c; font-variant-numeric: tabular-nums; }
@media (max-width: 720px) {
  .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .topic-bar-row { grid-template-columns: 64px 1fr 84px; }
}
```

`admin.html` 的 `topbar-title` 里、`<p>` 之后加：

```html
      <a class="back" href="/admin/topics">主题分布 →</a>
```

- [ ] **Step 5: 浏览器实测（Vibe Coding 验收）**

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

用浏览器打开 `http://127.0.0.1:8000/admin/topics`：

1. 空库显示空状态文案，17 行条形图隐藏；
2. 顶部四个数字都有值（已归类 0、待归类 0、多标签 0、最近归类 —）；
3. 从 `/admin` 能点到「主题分布」，从 `/admin/topics` 能点回审核；
4. 断网/停后台时错误文案是中文，不出现 traceback。

有真实归类数据后的条形图验证放到 Task 18（模型训练完成后）。

- [ ] **Step 6: 追加 dev-notes 并提交**

```bash
git add app/web/admin-topics.html app/web/admin-topics.js app/web/admin.html app/web/admin.css app/main.py dev-notes/ch10.md
git commit -m "feat(ch10): 主题分布后台页"
```

---

## Task 15: 下载预训练权重与训练脚本（smoke 验收）

> 训练脚本不可单测；用 `--smoke` 验证显存、保存与加载。下载失败**不许换模型**，停下来请用户开代理。

**Files:**
- Create: `scripts/train_topic_classifier.py`
- Create: `.cache/models/chinese-roberta-wwm-ext/`（git-ignored）
- Modify: `dev-notes/ch10.md`

- [ ] **Step 1: 下载并校验权重**

```bash
HF_ENDPOINT=https://hf-mirror.com .venv/Scripts/python.exe -c "from huggingface_hub import snapshot_download; snapshot_download('hfl/chinese-roberta-wwm-ext', local_dir='.cache/models/chinese-roberta-wwm-ext')"
.venv/Scripts/python.exe -c "from pathlib import Path; d=Path('.cache/models/chinese-roberta-wwm-ext'); print({name: (d/name).is_file() for name in ('config.json','pytorch_model.bin','vocab.txt','tokenizer.json')})"
```

四个值必须全 `True`。失败：先在 dev-notes 记错误，再请用户开代理重试；不下载别的模型。

- [ ] **Step 2: 写训练脚本骨架**

创建 `scripts/train_topic_classifier.py`：

```python
"""ch10 全参微调 RoBERTa-wwm-ext 多标签主题分类器（手写 PyTorch 循环）。"""

from __future__ import annotations

import argparse
import copy
import json
import random
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import torch
from sklearn.metrics import f1_score
from torch.utils.data import DataLoader, Dataset
from transformers import (
    AutoTokenizer, BertForSequenceClassification, get_linear_schedule_with_warmup,
)

from app.topics.dataset import read_jsonl
from app.topics.taxonomy import LABELS


class TopicDataset(Dataset):
    def __init__(self, samples, tokenizer, max_len: int):
        encoded = tokenizer([item.text for item in samples], padding="max_length",
                            truncation=True, max_length=max_len, return_tensors="pt")
        self.input_ids = encoded["input_ids"]
        self.attention_mask = encoded["attention_mask"]
        self.labels = torch.tensor([
            [1.0 if label in item.labels else 0.0 for label in LABELS] for item in samples
        ])

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, index):
        return {"input_ids": self.input_ids[index],
                "attention_mask": self.attention_mask[index],
                "labels": self.labels[index]}


def evaluate(model, loader, device):
    model.eval()
    truths, probs = [], []
    with torch.no_grad():
        for batch in loader:
            batch = {key: value.to(device) for key, value in batch.items()}
            logits = model(input_ids=batch["input_ids"],
                           attention_mask=batch["attention_mask"]).logits
            probs.append(torch.sigmoid(logits).cpu().numpy())
            truths.append(batch["labels"].cpu().numpy())
    return np.concatenate(truths), np.concatenate(probs)


def macro_f1(truths, probs, threshold: float = 0.5) -> float:
    return float(f1_score(truths, (probs >= threshold).astype(int),
                          average="macro", zero_division=0))


def tune_thresholds(truths, probs) -> list[float]:
    thresholds = []
    for index in range(len(LABELS)):
        if truths[:, index].sum() == 0:
            thresholds.append(0.5)
            continue
        best_threshold, best_score = 0.5, -1.0
        for step in range(4, 17):  # 0.20 ~ 0.80
            threshold = round(step * 0.05, 2)
            score = f1_score(truths[:, index],
                             (probs[:, index] >= threshold).astype(int), zero_division=0)
            if score > best_score:
                best_threshold, best_score = threshold, score
        thresholds.append(best_threshold)
    return thresholds


def write_report(report_dir: Path, history: list[dict], best_epoch: int,
                 thresholds: list[float], meta: dict) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = report_dir / f"ch10-train-{stamp}.md"
    lines = ["# ch10 训练报告", "", f"- 最佳轮次：{best_epoch}",
             f"- 最佳验证 macro-F1（阈值 0.5）：{max(row['val_macro_f1'] for row in history):.4f}",
             f"- 设备：{meta['device']}，显存峰值：{meta['peak_gb']:.2f} GB",
             f"- 耗时：{meta['seconds']:.1f}s", "",
             "| 轮次 | train loss | val macro-F1 |", "|---|---|---|"]
    lines += [f"| {row['epoch']} | {row['train_loss']:.4f} | {row['val_macro_f1']:.4f} |"
              for row in history]
    lines += ["", "## 每类阈值（验证集调优）", "", "| 类目 | 阈值 |", "|---|---|"]
    lines += [f"| {label} | {threshold} |" for label, threshold in zip(LABELS, thresholds)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def train(args) -> int:
    torch.manual_seed(args.seed)
    random.seed(args.seed)
    np.random.seed(args.seed)
    device = "cpu" if args.device == "cpu" or not torch.cuda.is_available() else "cuda"
    model_dir = Path(args.model_dir)
    tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True)
    train_samples = read_jsonl(args.train)
    val_samples = read_jsonl(args.val)
    if args.smoke:
        train_samples, val_samples = train_samples[:40], val_samples[:10]
        args.epochs, args.max_len, args.batch_size = 1, 64, 4
    train_set = TopicDataset(train_samples, tokenizer, args.max_len)
    val_set = TopicDataset(val_samples, tokenizer, args.max_len)
    train_loader = DataLoader(train_set, batch_size=args.batch_size, shuffle=True)
    val_loader = DataLoader(val_set, batch_size=args.batch_size)
    model = BertForSequenceClassification.from_pretrained(
        model_dir, num_labels=len(LABELS), problem_type="multi_label_classification",
        local_files_only=True).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr,
                                  weight_decay=args.weight_decay)
    total_steps = max(1, len(train_loader) * args.epochs // args.grad_accum)
    scheduler = get_linear_schedule_with_warmup(
        optimizer, int(total_steps * args.warmup_ratio), total_steps)
    best_state, best_f1, best_epoch, stale = None, -1.0, 0, 0
    history = []
    peak_gb = 0.0
    if device == "cuda":
        torch.cuda.reset_peak_memory_stats()
    for epoch in range(1, args.epochs + 1):
        model.train()
        total_loss = 0.0
        optimizer.zero_grad()
        for step, batch in enumerate(train_loader, 1):
            batch = {key: value.to(device) for key, value in batch.items()}
            try:
                with torch.autocast(device_type="cuda", dtype=torch.bfloat16,
                                    enabled=device == "cuda"):
                    loss = model(**batch).loss
                (loss / args.grad_accum).backward()
            except torch.cuda.OutOfMemoryError:
                print("显存不足：把 --batch-size 降到 4、--max-len 降到 96 后重跑。",
                      file=sys.stderr)
                return 2
            total_loss += loss.item()
            if step % args.grad_accum == 0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
                scheduler.step()
                optimizer.zero_grad()
        truths, probs = evaluate(model, val_loader, device)
        score = macro_f1(truths, probs)
        history.append({"epoch": epoch, "train_loss": total_loss / len(train_loader),
                        "val_macro_f1": score})
        print(f"epoch {epoch}: train_loss={total_loss / len(train_loader):.4f} "
              f"val_macro_f1={score:.4f}")
        if score > best_f1:
            best_f1, best_epoch, stale = score, epoch, 0
            best_state = copy.deepcopy(model.state_dict())
        else:
            stale += 1
            if stale >= args.patience:
                print(f"验证集 {args.patience} 轮未提升，早停。")
                break
    model.load_state_dict(best_state)
    truths, probs = evaluate(model, val_loader, device)
    thresholds = tune_thresholds(truths, probs)
    if device == "cuda":
        peak_gb = torch.cuda.max_memory_allocated() / 1024 ** 3
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(out)
    tokenizer.save_pretrained(out)
    (out / "labels.json").write_text(json.dumps(list(LABELS), ensure_ascii=False),
                                     encoding="utf-8")
    (out / "thresholds.json").write_text(
        json.dumps(dict(zip(LABELS, thresholds)), ensure_ascii=False), encoding="utf-8")
    meta = {"device": device, "peak_gb": peak_gb, "best_epoch": best_epoch,
            "history": history, "thresholds": thresholds,
            "params": vars(args), "smoke": args.smoke}
    (out / "train_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2,
                                                    default=str), encoding="utf-8")
    if not args.smoke:
        path = write_report(Path(args.report_dir), history, best_epoch, thresholds, meta)
        print(f"训练报告：{path}")
    print(f"模型已保存：{out}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ch10 全参微调主题分类器")
    parser.add_argument("--train", type=Path, default=Path(".cache/topics/splits/train.jsonl"))
    parser.add_argument("--val", type=Path, default=Path(".cache/topics/splits/val.jsonl"))
    parser.add_argument("--model-dir", type=Path,
                        default=Path(".cache/models/chinese-roberta-wwm-ext"))
    parser.add_argument("--out", type=Path, default=Path(".cache/models/topic-classifier"))
    parser.add_argument("--report-dir", type=Path, default=Path("evals/reports"))
    parser.add_argument("--epochs", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--grad-accum", type=int, default=2)
    parser.add_argument("--max-len", type=int, default=128)
    parser.add_argument("--lr", type=float, default=2e-5)
    parser.add_argument("--weight-decay", type=float, default=0.01)
    parser.add_argument("--warmup-ratio", type=float, default=0.1)
    parser.add_argument("--patience", type=int, default=2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args(argv)
    if args.smoke and args.out == Path(".cache/models/topic-classifier"):
        args.out = Path(".cache/models/topic-classifier-smoke")
    return train(args)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: 跑 smoke**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m scripts.train_topic_classifier --smoke
.venv/Scripts/python.exe -c "from pathlib import Path; from app.topics.model import TopicModel; d=Path('.cache/models/topic-classifier-smoke'); print(sorted(p.name for p in d.iterdir())); print(TopicModel(d).predict(['买大了想退'])[0].labels)"
```

预期：训练 1 轮不 OOM；目录含 `model.safetensors`、`config.json`、`labels.json`、`thresholds.json`；加载后能输出标签。OOM 时先按提示降 batch/max_len 重跑；仍失败就停下来问用户。

- [ ] **Step 4: 追加 dev-notes 并提交脚本**

记：smoke 的显存峰值、耗时、保存文件清单、加载预测结果。

```bash
git add scripts/train_topic_classifier.py dev-notes/ch10.md
git commit -m "feat(ch10): 全参微调训练脚本"
```

---

## Task 16: 全量训练与训练报告

**Files:**
- Create: `.cache/models/topic-classifier/`（git-ignored）
- Create: `evals/reports/ch10-train-<ts>.md`（提交）
- Modify: `dev-notes/ch10.md`

- [ ] **Step 1: 跑全量训练**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m scripts.train_topic_classifier \
  --train .cache/topics/splits/train.jsonl \
  --val .cache/topics/splits/val.jsonl \
  --out .cache/models/topic-classifier
```

- [ ] **Step 2: 判读训练结果**

检查：

1. 每轮 `val_macro_f1` 是否总体上升；
2. 是否在 8 轮内早停；
3. `evals/reports/ch10-train-*.md` 的阈值表是否落在 0.20~0.80；
4. `train_meta.json` 的 `peak_gb` 是否 < 6。

指标不达预期（例如 macro-F1 低于 0.6）不直接改阈值凑分：先看验证集逐类混错，再决定补数据或调 Prompt；仍然不行就如实写进 dev-notes，向用户报告。

- [ ] **Step 3: OOM 兜底**

按顺序只改这两个参数重跑，不换模型：

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m scripts.train_topic_classifier \
  --batch-size 4 --grad-accum 4 --max-len 96
```

仍 OOM：停止，向用户报告显存事实与已试参数，等用户决定；不许换小模型或 LoRA。

- [ ] **Step 4: 追加 dev-notes 并提交报告**

```bash
git add evals/reports/ch10-train-*.md dev-notes/ch10.md
git commit -m "train(ch10): 全量微调与训练报告"
```

---

## Task 17: 多标签评测、混淆矩阵与判错导出

**Files:**
- Create: `app/topics/evaluation.py`
- Create: `tests/test_ch10_evaluation.py`
- Create: `scripts/evaluate_topic_classifier.py`
- Create: `evals/topics/multilabel_cases.jsonl`
- Create: `evals/topics/error_review.csv`
- Create: `evals/reports/ch10-topic-<ts>.{json,md,html}`

**Interfaces:**
- Produces: `compute_metrics(truths, preds) -> dict`（`per_label/macro_f1/micro_f1/subset_accuracy/hamming_loss/confusion/cooccurrence`）
- Produces: `top_confusions(cooccurrence, top=15) -> list[dict]`
- Produces: `render_markdown(metrics, extra_lines=()) -> str`、`render_html(metrics) -> str`
- Consumes: Task 10 的 `TopicModel`

- [ ] **Step 1: 写失败的指标测试**

创建 `tests/test_ch10_evaluation.py`：

```python
from app.topics.evaluation import compute_metrics, render_html, render_markdown, top_confusions
from app.topics.taxonomy import LABELS


def vec(*indexes):
    row = [0] * len(LABELS)
    for index in indexes:
        row[index] = 1
    return row


def test_perfect_predictions():
    truths = [vec(0, 2), vec(1), vec(16)]
    metrics = compute_metrics(truths, truths)
    assert metrics["macro_f1"] == 1.0 and metrics["micro_f1"] == 1.0
    assert metrics["subset_accuracy"] == 1.0 and metrics["hamming_loss"] == 0.0
    assert metrics["confusion"]["退换货"] == {"tp": 1, "fp": 0, "fn": 0, "tn": 2}


def test_false_negative_and_false_positive_land_in_confusion():
    metrics = compute_metrics([vec(1)], [vec(5)])
    assert metrics["confusion"]["物流"] == {"tp": 0, "fp": 0, "fn": 1, "tn": 0}
    assert metrics["confusion"]["运费"] == {"tp": 0, "fp": 1, "fn": 0, "tn": 0}
    assert metrics["per_label"]["物流"]["recall"] == 0.0
    assert metrics["per_label"]["运费"]["precision"] == 0.0


def test_cooccurrence_counts_truth_by_pred_and_top_confusions_skip_diagonal():
    metrics = compute_metrics([vec(0), vec(4)], [vec(2), vec(4)])
    co = metrics["cooccurrence"]
    assert co[LABELS.index("退换货")][LABELS.index("尺码")] == 1
    assert co[LABELS.index("质量问题")][LABELS.index("质量问题")] == 1
    pairs = top_confusions(co)
    assert pairs[0] == {"true": "退换货", "pred": "尺码", "count": 1}
    assert all(pair["true"] != pair["pred"] for pair in pairs)


def test_renderers_contain_labels_and_no_matplotlib():
    metrics = compute_metrics([vec(0)], [vec(0)])
    markdown = render_markdown(metrics, ["多标签验收：通过"])
    assert "| 类目 | P | R | F1 | support |" in markdown
    assert "多标签验收：通过" in markdown
    html = render_html(metrics)
    assert "退换货" in html and "真:退换货" in html
    assert "matplotlib" not in html
```

运行：

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_ch10_evaluation.py
```

预期：`ModuleNotFoundError: No module named 'app.topics.evaluation'`。

- [ ] **Step 2: 实现 `evaluation.py`**

```python
"""多标签评测指标与 Markdown/HTML 报告；HTML 热力图不依赖 matplotlib。"""

from __future__ import annotations

import html as html_lib

import numpy as np
from sklearn.metrics import f1_score, hamming_loss, precision_recall_fscore_support

from app.topics.taxonomy import LABELS


def compute_metrics(truths, preds) -> dict:
    true = np.asarray(truths, dtype=int)
    pred = np.asarray(preds, dtype=int)
    precision, recall, f1, support = precision_recall_fscore_support(
        true, pred, labels=list(range(len(LABELS))), average=None, zero_division=0)
    per_label, confusion = {}, {}
    for index, label in enumerate(LABELS):
        tp = int(((true[:, index] == 1) & (pred[:, index] == 1)).sum())
        fp = int(((true[:, index] == 0) & (pred[:, index] == 1)).sum())
        fn = int(((true[:, index] == 1) & (pred[:, index] == 0)).sum())
        tn = int(((true[:, index] == 0) & (pred[:, index] == 0)).sum())
        per_label[label] = {"precision": round(float(precision[index]), 4),
                            "recall": round(float(recall[index]), 4),
                            "f1": round(float(f1[index]), 4), "support": int(support[index])}
        confusion[label] = {"tp": tp, "fp": fp, "fn": fn, "tn": tn}
    cooccurrence = np.zeros((len(LABELS), len(LABELS)), dtype=int)
    for truth_row, pred_row in zip(true, pred):
        for true_index in np.where(truth_row == 1)[0]:
            for pred_index in np.where(pred_row == 1)[0]:
                cooccurrence[true_index, pred_index] += 1
    return {
        "per_label": per_label,
        "macro_f1": round(float(f1_score(true, pred, average="macro", zero_division=0)), 4),
        "micro_f1": round(float(f1_score(true, pred, average="micro", zero_division=0)), 4),
        "subset_accuracy": round(float((true == pred).all(axis=1).mean()), 4),
        "hamming_loss": round(float(hamming_loss(true, pred)), 4),
        "confusion": confusion,
        "cooccurrence": cooccurrence.tolist(),
    }


def top_confusions(cooccurrence, top: int = 15) -> list[dict]:
    pairs = []
    for true_index, true_label in enumerate(LABELS):
        for pred_index, pred_label in enumerate(LABELS):
            count = int(cooccurrence[true_index][pred_index])
            if true_index != pred_index and count:
                pairs.append({"true": true_label, "pred": pred_label, "count": count})
    return sorted(pairs, key=lambda row: (-row["count"], row["true"], row["pred"]))[:top]


def render_markdown(metrics: dict, extra_lines=()) -> str:
    lines = ["# ch10 主题分类评测报告", "",
             f"- macro-F1：{metrics['macro_f1']}", f"- micro-F1：{metrics['micro_f1']}",
             f"- 整条完全命中：{metrics['subset_accuracy']}",
             f"- Hamming loss：{metrics['hamming_loss']}", "",
             "| 类目 | P | R | F1 | support | TP | FP | FN | TN |",
             "|---|---|---|---|---|---|---|---|---|"]
    for label in LABELS:
        row, matrix = metrics["per_label"][label], metrics["confusion"][label]
        lines.append(f"| {label} | {row['precision']} | {row['recall']} | {row['f1']} | "
                     f"{row['support']} | {matrix['tp']} | {matrix['fp']} | "
                     f"{matrix['fn']} | {matrix['tn']} |")
    lines += ["", "## Top 混淆对", "", "| 真标签 | 预测标签 | 次数 |", "|---|---|---|"]
    for pair in top_confusions(metrics["cooccurrence"]):
        lines.append(f"| {pair['true']} | {pair['pred']} | {pair['count']} |")
    lines += ["", *extra_lines]
    return "\n".join(lines) + "\n"


def render_html(metrics: dict) -> str:
    cooccurrence = metrics["cooccurrence"]
    peak = max((max(row) for row in cooccurrence), default=0) or 1
    header = "".join(f"<th>{html_lib.escape(label)}</th>" for label in LABELS)
    rows = []
    for true_index, true_label in enumerate(LABELS):
        cells = [f"<th>{html_lib.escape(true_label)}</th>"]
        for pred_index, pred_label in enumerate(LABELS):
            value = cooccurrence[true_index][pred_index]
            alpha = value / peak
            cells.append(
                f"<td title='真:{html_lib.escape(true_label)} 预测:"
                f"{html_lib.escape(pred_label)} 次数:{value}' "
                f"style='background:rgba(46,125,110,{alpha:.3f})'>{value or ''}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>ch10 混淆矩阵</title><style>"
        "body{font-family:system-ui,sans-serif;margin:24px;color:#243831}"
        "table{border-collapse:collapse;font-size:12px}"
        "th,td{border:1px solid #dce5e2;padding:4px 6px;text-align:center}"
        "th{background:#f2f7f5}td{min-width:28px}"
        "</style></head><body><h1>17×17 真标签 × 预测标签</h1>"
        f"<table><thead><tr><th>真 \\ 预测</th>{header}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></body></html>"
    )
```

- [ ] **Step 3: 写固定多标签用例**

`evals/topics/multilabel_cases.jsonl`：

```jsonl
{"text":"买大了想退","expected":["退换货","尺码"]}
{"text":"运费怎么算，几天能到","expected":["运费","物流"]}
{"text":"刚买就降价了能补差价吗，顺便问下有什么满减","expected":["价保","优惠活动"]}
{"text":"发票抬头写错了，收货地址也想改","expected":["发票","订单修改"]}
{"text":"这个用了两个月就坏了，还能修吗","expected":["质量问题","保修维修"]}
{"text":"会员积分怎么用，能抵运费吗","expected":["会员积分","运费"]}
```

- [ ] **Step 4: 写评测脚本**

创建 `scripts/evaluate_topic_classifier.py`：

```python
"""测试集评测、多标签验收、真实抽检与判错导出。"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path

from app.topics.dataset import read_jsonl
from app.topics.evaluation import compute_metrics, render_html, render_markdown
from app.topics.model import ModelNotReady, TopicModel
from app.topics.taxonomy import LABELS


def matrix(samples) -> list[list[int]]:
    return [[1 if label in item.labels else 0 for label in LABELS] for item in samples]


def label_matrix(labels_list) -> list[list[int]]:
    return [[1 if label in labels else 0 for label in LABELS] for labels in labels_list]


def predict(model, texts, batch=64):
    result = []
    for start in range(0, len(texts), batch):
        result.extend(model.predict(texts[start:start + batch]))
    return result


def pick_errors(samples, predictions, limit=25):
    errors = []
    for item, prediction in zip(samples, predictions):
        false_negative = [label for label in item.labels if label not in prediction.labels]
        false_positive = [label for label in prediction.labels if label not in item.labels]
        if false_negative or false_positive:
            errors.append((item, prediction, false_negative, false_positive))
    errors.sort(key=lambda row: (-(len(row[2]) + len(row[3])), row[0].id))
    chosen, covered = [], set()
    for error in errors:
        classes = set(error[0].labels) | set(error[1].labels)
        if classes - covered:
            chosen.append(error)
            covered |= classes
            if len(chosen) >= limit:
                return chosen
    for error in errors:
        if len(chosen) >= limit:
            break
        if error not in chosen:
            chosen.append(error)
    return chosen


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ch10 评测")
    parser.add_argument("--test", type=Path, default=Path(".cache/topics/splits/test.jsonl"))
    parser.add_argument("--model-dir", type=Path, default=Path(".cache/models/topic-classifier"))
    parser.add_argument("--cases", type=Path, default=Path("evals/topics/multilabel_cases.jsonl"))
    parser.add_argument("--real", type=Path, default=Path("data/topics/real_train.jsonl"))
    parser.add_argument("--out-dir", type=Path, default=Path("evals/reports"))
    parser.add_argument("--errors", type=Path, default=Path("evals/topics/error_review.csv"))
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args(argv)
    if not args.test.exists():
        print(f"测试集不存在：{args.test}，先跑 --split。")
        return 2
    try:
        model = TopicModel(args.model_dir, device=args.device)
    except ModelNotReady as exc:
        print(f"模型未就绪：{exc}，先跑 scripts/train_topic_classifier.py。")
        return 2
    test = read_jsonl(args.test)
    if not test:
        print("测试集为空。")
        return 2
    predictions = predict(model, [item.text for item in test])
    metrics = compute_metrics(matrix(test), label_matrix([p.labels for p in predictions]))

    extra = []
    failed_cases = []
    if args.cases.exists():
        cases = [json.loads(line) for line in args.cases.read_text(encoding="utf-8").splitlines()
                 if line.strip()]
        case_predictions = predict(model, [case["text"] for case in cases])
        extra += ["## 多标签验收", "", "| 文本 | 期望 | 预测 | 全部命中 |", "|---|---|---|---|"]
        for case, prediction in zip(cases, case_predictions):
            hit = set(case["expected"]) <= set(prediction.labels)
            extra.append(f"| {case['text']} | {'、'.join(case['expected'])} | "
                         f"{'、'.join(prediction.labels)} | {'是' if hit else '否'} |")
            if not hit:
                failed_cases.append(case["text"])
        if "买大了想退" in failed_cases:
            extra.append("\n> 验收③失败：`买大了想退` 没有同时命中退换货与尺码。")
    else:
        extra.append("> 未找到多标签用例文件，跳过验收③。")

    if args.real.exists():
        real = read_jsonl(args.real)
        real_predictions = predict(model, [item.text for item in real])
        extra += ["", "## 真实问题抽检（已进训练集，仅观察真实分布）", "",
                  "| 文本 | 人工标签 | 预测标签 |", "|---|---|---|"]
        extra += [f"| {item.text} | {'、'.join(item.labels)} | {'、'.join(prediction.labels)} |"
                  for item, prediction in zip(real, real_predictions)]
    else:
        extra.append("> 未找到真实训练集，跳过真实抽检。")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / f"ch10-topic-{stamp}.json").write_text(
        json.dumps({"metrics": metrics, "failed_cases": failed_cases},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out_dir / f"ch10-topic-{stamp}.md").write_text(
        render_markdown(metrics, extra), encoding="utf-8")
    (args.out_dir / f"ch10-topic-{stamp}.html").write_text(
        render_html(metrics), encoding="utf-8")

    errors = pick_errors(test, predictions)
    args.errors.parent.mkdir(parents=True, exist_ok=True)
    with args.errors.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "text", "true_labels", "pred_labels", "error_type", "note"])
        for item, prediction, _, _ in errors:
            writer.writerow([item.id, item.text, ",".join(item.labels),
                             ",".join(prediction.labels), "", ""])
    print(f"macro-F1={metrics['macro_f1']} micro-F1={metrics['micro_f1']} "
          f"subset={metrics['subset_accuracy']}")
    print(f"报告：{args.out_dir}/ch10-topic-{stamp}.{{json,md,html}}；判错样本：{args.errors}")
    others = [text for text in failed_cases if text != "买大了想退"]
    if others:
        print("以下多标签用例未全部命中（仅提示，不判失败）：" + "、".join(others))
    if "买大了想退" in failed_cases:
        # 验收③只把这一条定为硬失败；其余用例只做观察，避免演示判据被扩大。
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: 跑单测与评测**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_ch10_evaluation.py
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m scripts.evaluate_topic_classifier
```

预期：单测全过；评测打印 macro-F1 与报告路径；`买大了想退` 同时命中 `退换货` 与 `尺码`，否则退出码 3，**不许改用例通过**。指标差就如实报告，先看混淆对与 25 条判错样本。

- [ ] **Step 6: 人工复核判错样本（人工检查点）**

把 `evals/topics/error_review.csv` 的 `error_type` 与 `note` 交给用户填（术语边界/标注错/数据不足/模型容量）。用户填完后再把结论写进评测报告；未填前报告注明「判错复核待人工」。

**停在这里，不执行 Step 7**，直到用户明确说填完。等待期间可以继续 Task 18 的演示脚本代码准备，但不得把未复核的判错结论写成已复核。

- [ ] **Step 7: 追加 dev-notes 并提交**

```bash
git add app/topics/evaluation.py tests/test_ch10_evaluation.py scripts/evaluate_topic_classifier.py evals/topics/multilabel_cases.jsonl evals/topics/error_review.csv evals/reports/ch10-topic-* dev-notes/ch10.md
git commit -m "feat(ch10): 多标签评测与混淆矩阵报告"
```

---

## Task 18: 演示脚本、文档、回归与交付

**Files:**
- Create: `scripts/demo_topics.py`
- Modify: `README.md`
- Verify: `.gitignore` 已含 `data/topics/real_*`（Task 8 已加）
- Modify: `dev-notes/ch10.md`

- [ ] **Step 1: 写演示脚本**

`scripts/demo_topics.py`：按规格 §14 的顺序执行——检查模型与测试集 → 评测摘要与 Top 混淆对 → 调服务归类真实池 → 6 条多标签用例 → 分布摘要。核心代码：

```python
"""ch10 三条验收一次跑完。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.config import Settings
from app.database import build_database_url, build_engine, build_session_factory
from app.topics.dataset import read_jsonl
from app.topics.evaluation import compute_metrics, top_confusions
from app.topics.model import ModelNotReady, TopicModel
from app.topics.stats import distribution
from app.topics.taxonomy import LABELS
from app.topics.worker import TopicClient, run_batch

MODEL = Path(".cache/models/topic-classifier")
TEST = Path(".cache/topics/splits/test.jsonl")


def main() -> int:
    if not (MODEL / "labels.json").exists() or not TEST.exists():
        print("缺少模型或测试集：先跑 --split 与 scripts/train_topic_classifier.py。")
        return 2
    model = TopicModel(MODEL)
    test = read_jsonl(TEST)
    # TopicModel.predict 单次上限 64 条，测试集约 120 条，必须自己切块。
    predictions = []
    for start in range(0, len(test), 64):
        predictions.extend(model.predict([item.text for item in test[start:start + 64]]))
    metrics = compute_metrics(
        [[1 if label in item.labels else 0 for label in LABELS] for item in test],
        [[1 if label in prediction.labels else 0 for label in LABELS] for prediction in predictions],
    )
    print(f"① 测试集 macro-F1={metrics['macro_f1']} micro-F1={metrics['micro_f1']}")
    for label in LABELS:
        row = metrics["per_label"][label]
        print(f"   {label}: P={row['precision']} R={row['recall']} F1={row['f1']} "
              f"support={row['support']}")
    print("   Top 混淆对：", top_confusions(metrics["cooccurrence"], 5))

    settings = Settings.from_env()
    engine = build_engine(build_database_url(settings))
    factory = build_session_factory(engine)
    client = TopicClient(settings.topic_service_url)
    try:
        report = run_batch(factory, client, min_pending=1, limit=50, force=True)
        if report.failed and not report.classified:
            print("② 推理服务不可达：先双击 start-topic.bat。")
            return 2
        print(f"② 批量归类：待归类 {report.pending}，已归类 {report.classified}，失败 {report.failed}")
        data = distribution(factory)
        print(f"   已归类 {data['total_classified']}，多标签 {data['multi_label']}，"
              f"待归类 {data['pending']}")
    finally:
        client.close()
        engine.dispose()

    cases = [json.loads(line) for line in Path("evals/topics/multilabel_cases.jsonl")
             .read_text(encoding="utf-8").splitlines() if line.strip()]
    failures = []
    print("③ 多标签用例：")
    for case in cases:
        prediction = model.predict([case["text"]])[0]
        hit = set(case["expected"]) <= set(prediction.labels)
        print(f"   {'✓' if hit else '✗'} {case['text']} → {prediction.labels}")
        if not hit:
            failures.append(case["text"])
    others = [text for text in failures if text != "买大了想退"]
    if others:
        print("其他用例未全部命中（仅提示）：" + "、".join(others))
    if "买大了想退" in failures:
        # 验收③只把「买大了想退」定为硬失败，其余用例只观察，避免演示判据被扩大。
        print("多标签验收失败：买大了想退 没有同时命中退换货与尺码。")
        return 3
    print("后台主题分布页：http://127.0.0.1:8000/admin/topics")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: 更新 `.gitignore` 与 README**

`README.md` 增加「ch10 主题分类」一节，至少写到：

```markdown
## ch10 多标签主题分类器

### 准备
1. 下载预训练权重：
   `HF_ENDPOINT=https://hf-mirror.com .venv/Scripts/python.exe -c "from huggingface_hub import snapshot_download; snapshot_download('hfl/chinese-roberta-wwm-ext', local_dir='.cache/models/chinese-roberta-wwm-ext')"`
2. 复制 `.env.example` 里的 `TOPIC_*` 配置到 `.env`；默认 `TOPIC_CLASSIFY_ENABLED=false`。
3. 已有数据库执行 `python scripts/migrate_ch10.py`（新库由 `docker compose` 自动跑 `008_topics.sql`）。

### 构建、训练、评测
```bash
python -m scripts.build_topic_dataset --split --seed 42 --out .cache/topics/splits
python -m scripts.train_topic_classifier
python -m scripts.evaluate_topic_classifier
```

### 演示
```bash
start-topic.bat
python -m scripts.classify_topics --force --limit 17
python -m scripts.demo_topics
```

浏览器打开 `http://127.0.0.1:8000/admin/topics` 看主题分布。实时聊天主链路不调用分类器；
推理服务默认 CPU，避免和聊天模型抢 6GB 显存。
```

- [ ] **Step 3: 全量回归**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest -q
```

必须全绿；记录总数与 ch10 新增用例数到 dev-notes。**不要为了让用例通过去改判据。**

- [ ] **Step 4: 请求代码评审（后端/数据脚本）**

用 `superpowers:requesting-code-review` 对本章分支做一次全量评审；前端页面（Task 14）按约定不送 review。重点看：

1. 聊天主链路是否真的没有 import `app.topics.model/service`；
2. `run_batch` 的失败/唯一键路径是否会把整批结果吞掉；
3. 真实 DB 文本是否可能被提交（`git status` + `.gitignore`）；
4. 阈值回落、空标签、未知标签三条边界；
5. 评测脚本是否会在多标签失败时仍返回 0。

把每条 finding 的修法与验证写进 dev-notes。

- [ ] **Step 5: 最终验收**

```bash
python -m scripts.demo_topics
```

三条验收逐条核对：

1. 报告里有逐类 P/R/F1 与 2×2、17×17 混淆矩阵；
2. `topic_classifications` 有真实行，`/admin/topics` 显示非零条形图；
3. `买大了想退` 同时命中 `退换货` 与 `尺码`。

- [ ] **Step 6: 收尾提交**

先扫密钥与 DB 原文：

```bash
git status --short
git grep -n -E "sk-[A-Za-z0-9]{10,}|1[3-9][0-9]{9}" -- . ':!tests' ':!evals/topics/audit_sample.csv' || true
git check-ignore -v data/topics/real_train.jsonl data/topics/real_audit.csv
```

`git check-ignore` 必须命中 `data/topics/real_*`。然后按用户「每章一次提交」的习惯，把本章提交压成一个，标题固定：

```bash
git log --oneline f149688..HEAD
git reset --soft f149688
git commit -m "$(cat <<'EOF'
第十次提交（ch10）：多标签主题分类器与主题分布

- 1200 条合成语料 + 人工抽审 + 80/10/10 分层切分 + 仅训练集增强
- RoBERTa-wwm-ext 全参微调，验证集早停与逐类阈值
- 测试集逐类 P/R/F1、2×2 与 17×17 混淆矩阵、判错样本导出
- 旁路推理服务与后台批量归类，写 topic_classifications
- /admin/topics 主题分布页；实时聊天主链路不调用分类器
- 真实 DB 原文、密钥、模型权重不入库
EOF
)"
git status --short
```

`git reset --soft` 会保留工作区，提交前先确认 `git log` 列出的都是本章提交（`f149688..HEAD`），不要把 ch09 之前的提交带进来。推送前告诉用户开代理，用户确认后再 `git push origin ch10-topic-classifier`（或按本章合并惯例处理）。

- [ ] **Step 7: 最终 dev-notes**

追加：交付命令、测试总数、三条验收结果、代码评审结论、遗留风险（如单类 support 小、真实池只有 13 条）。

---

