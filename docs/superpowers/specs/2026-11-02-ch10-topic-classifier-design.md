# ch10 设计规格：飞轮低置信问题的多标签主题分类器

- 日期：2026-11-02
- 父提交：`f149688`（第九次提交 ch09）
- 本章分支：`ch10-topic-classifier`
- 过程留痕：`dev-notes/ch10.md`

## 1. 用户原话与目标

> 「我要用 Superpowers 模式给客服系统训练一个自己的模型：微调一个多标签主题分类器，把飞轮攒下的低置信度问题按主题批量归类，好决定先补哪块知识。」

六项功能要求：

1. 语料从飞轮 `low_confidence_questions` 池取真实问题；池量不足时由 LLM 按术语表生成模拟问题补足。数据处理四步：清洗（脱敏、错别字、格式）→ 归并到 17 类术语表 → 80/10/10 分层抽样 → 仅训练集做增强（同义词替换、句式微调）。
2. 多标签标注：一句话字面提到几个诉求就打几个标签，一个不多一个不少；走「LLM 按术语表预标 + 人工抽审」。
3. 训练：RoBERTa-wwm-ext **全参微调**（不用 LoRA），验证集盯分，正则化 + 早停。
4. 评测：留出测试集逐类 P/R/F1、混淆矩阵；自动指标之外人工抽判错样本复核。
5. 部署：导出推理服务，旁路批量归类——低置信问题攒够一批归一次，写 `topic_classifications`；**实时对话主链路不调用**。
6. 前端：飞轮后台加主题分布页，展示各类目问题量。

三条验收：

1. 测试集各类目 F1 与混淆矩阵报告能跑出来。
2. 拿一批真实问题跑归类，后台主题分布页能看到各类目统计。
3. 「买大了想退」这类多诉求句子能同时命中多个类目。

**技术栈定死**：哈工大讯飞 `hfl/chinese-roberta-wwm-ext` 全参微调；推理服务化（ONNX 或 transformers 服务，本章选 transformers）。

**本章不做**：LoRA/QLoRA、embedding 微调、意图识别微调。

**过程约束**：全程 Superpowers；不可单测的纯 Prompt/数据/训练任务用标注样例或评估集验证替代 TDD；前端 Vibe Coding，不套 brainstorm/TDD/review；新库/框架用法先查 Context7，本会话没有 Context7 时读已安装包源码并在 `dev-notes/ch10.md` 留痕。

## 2. 已确认的环境事实（设计的前置约束）

| 事实 | 实测来源 | 对设计的影响 |
|---|---|---|
| GPU：NVIDIA RTX 4050 Laptop，**6.0GB 显存**，compute capability 8.9 | `torch.cuda.get_device_properties(0)` | 全参微调必须小批量 + 梯度累积 + bf16 + `max_len=128`；先 smoke 再全量 |
| torch 2.11.0+cu128、transformers 5.21.0、scikit-learn 1.9.1、numpy 2.5.4、pandas 3.1.0 | `importlib.metadata.version` | 训练无需新增核心依赖 |
| `BertForSequenceClassification`、`get_linear_schedule_with_warmup` 在 transformers 5.21 可导入 | 本机导入探针 | 手写训练循环可行；不装 `accelerate`/`datasets` |
| 未安装 `accelerate`、`datasets`、`onnx`、`onnxruntime`、`matplotlib` | 本机导入探针 | 不引入 HF Trainer、训练集加载器、ONNX、matplotlib |
| 模型镜像：`https://hf-mirror.com` 可访问，`hfl/chinese-roberta-wwm-ext` 返回 200，含 `config.json`/`pytorch_model.bin`/`vocab.txt`/`tokenizer.json`；HF 官方站在本机当前不可达 | `curl` 探针（2026-11-02） | 下载走 `HF_ENDPOINT=https://hf-mirror.com`；仍失败则停下来请用户开代理 |
| 真实池：`low_confidence_questions` **17 行 / 13 个不同问法**；`review_queue` 13 条；样例覆盖运费、发票、活动、积分、物流、商品信息等约 10 类，含「订单 1001」噪声 | 直连本机 MySQL 只读查询 | 合成语料是主力（约 1200 条）；真实行去重后 13 条人工标好只进训练集，17 行原始归类结果另做真实抽检表 |
| 术语表：17 类由用户给定，近邻规则由用户给定 | 用户原话 | 落成唯一事实源，全链路只读这一份 |
| 新表 DDL 由用户给定：`UNIQUE KEY uk_question_id` + 指向 `low_confidence_questions.id` 的外键 | 用户消息 | 一条池行只有一行归类；重跑默认跳过，`--force` 更新 |
| 迁移惯例：`scripts/init_sql/00N_*.sql` + 幂等 `scripts/migrate_chNN.py`；`init_db` 的 `create_all` 不会给旧表加列 | `scripts/migrate_ch09.py`、`scripts/init_db.py` | ch10 新增 `008_topics.sql` + `migrate_ch10.py`；新表用 `checkfirst=True` 幂等创建 |
| 后台任务惯例：`app/knowledge/scheduler.py` 的 `EvalScheduler`/`MiningScheduler`，lifespan 启动、finally 关闭；测试默认关闭后台任务 | `app/main.py`、`tests/support.py` | 复用同一形状加 `TopicScheduler`；`tests/support.py` 默认 `topic_classify_enabled=False` |
| 审核页骨架：`app/web/admin.html` + `admin.js` + `admin.css`，接口前缀 `/api/admin` | 读源码 | 新页 `/admin/topics` 沿用视觉与错误码风格 |
| requirements 已有 `sentence-transformers>=5,<7`，锁文件里 `transformers==5.21.0` | `requirements.txt:14`、`requirements.lock.txt:113` | requirements.txt 显式加 `transformers==5.21.0`，与锁文件一致 |

## 3. 架构：六个边界清楚的单元

```
┌─ 1. app/topics/taxonomy.py   17 类唯一事实源（标签、边界、排序、校验）
├─ 2. app/topics/{clean,prompts,dataset}.py
│      清洗/脱敏、生成与预标 Prompt、去重/分层切分/增强/抽审合并
├─ 3. app/topics/model.py      加载权重、阈值、回落「其他」、批量预测
├─ 4. app/topics/service.py    旁路 FastAPI 推理服务（:8933，默认 CPU）
├─ 5. app/topics/{worker,scheduler,stats}.py
│      待归类查询、写表/更新、后台定时器、分布统计
└─ 6. scripts/                 建数据集、训练、评测、手动归类、演示、迁移 + 008 SQL
      前端 app/web/admin-topics.*（Vibe Coding，不套流程）
```

依赖方向单向：`2/3/5 → 1`；`4 → 3`；`5 → 4`（仅通过 HTTP）；主应用只依赖 `5` 与 `1`，绝不 import `3`。这样聊天进程不会加载分类模型，推理服务挂了也不影响对话。

## 4. 17 类权威术语表（`app/topics/taxonomy.py`）

顺序即后台展示顺序、标签数组排序、模型输出下标：

| 序号 | 标签 | 边界说明 |
|---|---|---|
| 0 | 退换货 | 退货、换货、退款的条件与流程；「退」归这里 |
| 1 | 物流 | 包裹运输、配送进度、签收异常；管货 |
| 2 | 尺码 | 尺码表、偏大偏小、按身高体重选码、买大买小 |
| 3 | 发票 | 开票、发票类型、抬头、电子发票 |
| 4 | 质量问题 | 到货破损、瑕疵、变质、功能故障等商品本身的问题 |
| 5 | 运费 | 运费金额、包邮门槛、运费由谁承担；管钱 |
| 6 | 优惠活动 | 优惠券、满减、折扣、活动规则 |
| 7 | 价保 | 下单后降价、补差价 |
| 8 | 支付 | 付款方式、支付失败、扣款异常 |
| 9 | 订单修改 | 未发货前改地址/改商品/取消/合并订单 |
| 10 | 库存补货 | 缺货、何时补货、到货提醒 |
| 11 | 商品信息 | 参数、材质、颜色、保质期、适用人群等属性 |
| 12 | 保修维修 | 保修期、维修流程、换配件；「修」归这里 |
| 13 | 账号 | 登录、注册、密码、账号安全 |
| 14 | 会员积分 | 积分获取与使用、会员等级、权益 |
| 15 | 评价 | 评价入口、修改/删除评价、差评处理 |
| 16 | 其他 | 无具体诉求、闲聊、纯订单号/单号、无法归入以上 |

**近邻规则（原样固化，生成与预标 Prompt 都带）**

- 修归保修维修、退归退换货；一句话同时提到就都打。
- 运费管钱、物流管货：「运费怎么算」→ 运费；「几天能到」→ 物流。
- 价保是补差价、优惠活动是券和满减：「刚买就降价」→ 价保；「有什么满减」→ 优惠活动。
- 尺码 vs 商品信息：选码/大小 → 尺码；颜色/材质/参数 → 商品信息。
- 库存补货 vs 商品信息：有没有货/何时到 → 库存补货；商品属性 → 商品信息。
- 订单修改 vs 退换货：发货前改信息 → 订单修改；退货退款 → 退换货。
- 发票 vs 支付：开票/抬头 → 发票；付款失败/扣款 → 支付。
- 质量问题 vs 保修维修：到货即坏/破损/变质 → 质量问题；用了一段时间坏了要修 → 保修维修；字面同时有「坏」和「修」可双标。

**判定政策**

- 只按字面诉求打标，不做意图推断；不设标签数量上限，实际以 1~3 个为主。
- `买大了想退` → `["退换货", "尺码"]`。
- 模型 17 个 sigmoid 输出；每类一个验证集选出的阈值；全部不过阈值时回落 `["其他"]`，保证每条归类至少一个标签。
- 标签去重后按上表顺序存 JSON 数组；未知标签直接报错，不静默丢弃。

公开接口：

```python
LABELS: tuple[str, ...]          # 长度 17，顺序同上
DESCRIPTIONS: dict[str, str]      # 每类一句边界说明
BOUNDARIES: tuple[str, ...]       # 近邻规则原文
OTHER: str = "其他"

def order_labels(labels: Iterable[str]) -> list[str]: ...
def validate_labels(labels: Iterable[str]) -> list[str]: ...
```

## 5. 数据构建

### 5.1 真实池取数与清洗

- 从 `low_confidence_questions` 按 `id` 升序取全部行；`raw_question` 去空白后为空则丢弃。
- `app/topics/clean.py`：
  - `mask_pii(text)`：手机号 `1[3-9]\d{9}` → `[手机号]`；18 位身份证 → `[证件号]`；「订单/单号/运单号 + 数字」→ 前缀保留 + `[单号]`；裸 6 位以上数字 → `[单号]`。
  - `clean_text(text)`：NFKC 全半角归一、去零宽字符与控制字符、连续空白折叠为单空格、中英文标点统一、首尾去引号与空白。
  - 顺序固定：先 `mask_pii` 再 `clean_text`。
- 真实问题再走一遍 LLM 修错别字（批量 20 条/次，只改错别字与明显病句，不改变诉求）；解析失败保留清洗后的原文。
- 修完错别字后走独立预标（同 5.3 的 Prompt），导出 `data/topics/real_audit.csv`（列 `id,text,prelabels,correct_labels,note`，**git-ignored**）；人工在 CSV 上逐条确认并就地填写 `correct_labels`（留空表示同意预标），整张表直接作为 `--apply-real-audit` 的输入，不再二次转存。真实样本少，要求**全部人工确认**，不只抽样。
- 最终写 `data/topics/real_train.jsonl`，**git-ignored**（DB 内容不入库）；去重按 5.4，重复问法只保留 id 最小的一条，因此 17 行最终约 13 条进入训练集；原始 17 行的归类结果仍用于验收②的真实抽检表。

### 5.2 合成语料

- 总数 **1200** 条；**300** 条多标签（25%），从固定 10 个组合各取 30 条：退换货+尺码、运费+物流、价保+优惠活动、发票+订单修改、会员积分+运费、库存补货+保修维修、质量问题+退换货、支付+订单修改、商品信息+库存补货、账号+会员积分。
- 配额是**每类标签支持数下限**，不是精确条数：16 类各 **70** 个标签位、「其他」**80** 个标签位（16×70+80=1200 个标签位）。300 条多标签贡献 600 个标签位，因此单标签补足量与总量这样算：
  - 组合先排；每类「配额 − 该类的组合出现数」即该类的单标签缺口。
  - 缺口之和约 600 条单标签（16 类缺口加总；组合每类出现次数固定，可直接算出）。
  - 余下约 300 条单标签按 17 类轮转补齐，使总量恰好 1200 条；不要求各标签位总和等于 1200（那会与 300 条多标签矛盾）。
- 常量与校验写在 `scripts/build_topic_dataset.py`：总量必须 1200、多标签必须 300、每类标签支持数必须 ≥ 配额；任何一项不满足直接退出非零。
- 生成 Prompt 带：17 类术语表、近邻规则、目标诉求组合（仅给诉求，不给标签名之外的提示）、口语化要求、长度 8~40 字、禁止出现真实姓名/电话/地址/订单号；要求输出 JSON 数组 `{"items":[{"question": "..."}]}`，每批 10 条。
- 解析失败重试 1 次；仍失败则记录缺口并让构建脚本退出非零，**不允许静默少生成**。
- 生成结果写 `data/topics/synthetic_v1.jsonl`（**提交入库**，纯合成、无隐私），字段：

```json
{"id":"syn-0001","text":"...","labels":["退换货","尺码"],"origin":"synthetic",
 "target_labels":["退换货","尺码"],"prelabels":["退换货","尺码"]}
```

### 5.3 独立预标与人工抽审

- 预标 Prompt 只给术语表与近邻规则，**不给** `target_labels`；每批 20 条，输出 `{"items":[{"id":..., "labels":[...]}]}`。
- 预标与 `target_labels` 不一致的样本标为 `disagree`；预标解析失败保留空数组并计入审计。
- 审计样本：每类随机 2 条（固定 seed）+ 最多 20 条 `disagree`，去重后约 50 条；导出 `evals/topics/audit_sample.csv`，列：

```text
id,text,prelabels,correct_labels,note
```

- 人工只填 `correct_labels`（逗号分隔）与 `note`；修正结果保存为 `evals/topics/audit_corrections.jsonl`（`{"id","labels","note"}`）。
- 构建脚本用修正标签覆盖语料标签，并输出 `审计条数 / 修正条数 / 预标一致率 / 修正后一致率` 到数据集统计报告。
- 审计样本只从合成语料抽取，避免把 DB 原文写进仓库。

### 5.4 去重

- 归一化文本做精确去重；再算字符 3-gram Jaccard，阈值 `0.9`，高于阈值视为重复，保留先出现者。
- 真实与合成合并前做一次去重；增强产物再做一次。
- `dedupe(samples, threshold=0.9) -> (kept, dropped_count)`，顺序稳定。

### 5.5 分层切分

- 只对合成语料做 80/10/10；真实样本人为追加到训练集，**不参与分层比例**，避免十几条真实样本撬动切分。
- `iterative_split(samples, ratios=(0.8, 0.1, 0.1), seed=42)`：
  1. 固定 seed 打乱；
  2. 标签按样本数从少到多处理，每个标签把尚未分配的样本依次放进「当前最缺该类」且仍有容量的分片；
  3. 多标签样本只分配一次；
  4. 剩余样本按打乱顺序补满目标容量。
- 硬保证（测试断言）：三份无交集、并集等于输入；每个样本数 ≥10 的类在 val 与 test 至少各 1 条；各类占比与 10%/10% 偏差 ≤2 个百分点；同 seed 两次结果完全一致。
- `--split` 组装时：合成训练集 + 可选的 `data/topics/real_train.jsonl`（追加、不参与比例）→ 按 5.6 做增强 → 落盘。切分产物写 `.cache/topics/splits/{train,val,test}.jsonl`（git-ignored，可由脚本重建）。

### 5.6 增强（只扩训练集）

- **同义词替换**：固定词表，词级替换，每句最多 2 处，命中已存在词不重复替换；示例：快递↔物流、包邮↔免运费、退换↔退换货、开票↔开发票、优惠券↔券、价保↔保价、补货↔到货、积分↔会员分。
- **句式微调**：构建阶段先为每条合成样本预生成 1 条改写，存 `data/topics/paraphrase_v1.jsonl`（提交入库）：

```json
{"id":"par-0001","seed_id":"syn-0001","text":"...","labels":["退换货","尺码"],"origin":"paraphrase"}
```

- 改写生成后做一次独立预标校验：预标与 `seed` 标签不一致的改写直接丢弃，不入 `paraphrase_v1.jsonl`。
- 训练时只对训练集样本按 seed 抽 30% 挂上其改写；若改写对应的 `seed_id` 不在训练集，则丢弃——**变体与原样本同组，杜绝验证/测试泄漏**。
- 同义词替换在 `--split` 组装训练集时确定性执行（固定 seed、每句最多 2 处），只作用于训练集。
- 增强后再次去重；增强样本不改变标签。

### 5.7 产物与可复现性

| 文件 | 是否入库 | 说明 |
|---|---|---|
| `data/topics/synthetic_v1.jsonl` | 是 | 1200 条合成语料，固定内容 |
| `data/topics/paraphrase_v1.jsonl` | 是 | 句式改写池，只挂训练集 |
| `data/topics/real_train.jsonl` | 否 | DB 原文清洗后 + 人工标签，git-ignored |
| `data/topics/real_audit.csv` | 否 | 真实问题逐条人工确认表，含 DB 原文，人工就地编辑；apply 直接读它，git-ignored |
| `.cache/topics/splits/*.jsonl` | 否 | 固定 seed 重建 |
| `.cache/models/chinese-roberta-wwm-ext/` | 否 | 预训练权重 |
| `.cache/models/topic-classifier/` | 否 | 微调后权重、tokenizer、labels、thresholds、config、metrics |
| `evals/topics/audit_sample.csv` | 是 | 仅合成样本 |
| `evals/topics/audit_corrections.jsonl` | 是 | 人工修正，仅合成样本 |
| `evals/topics/multilabel_cases.jsonl` | 是 | 验收③固定用例 |
| `evals/topics/error_review.csv` | 是 | 判错样本复核表 |
| `evals/reports/ch10-*.{json,md,html}` | 是 | 数据集、训练、评测报告 |

- 数据集统计报告 `evals/reports/ch10-dataset-<ts>.md`：各类计数、多标签比例、去重数、切分比例、预标一致率、审计修正数、增强条数。
- 所有 LLM 产出落盘后再进入训练；重训不重新调 LLM。

### 5.8 数据构建命令

```bash
python -m scripts.build_topic_dataset --generate --count 1200 --out data/topics/synthetic_v1.jsonl
python -m scripts.build_topic_dataset --paraphrase --in data/topics/synthetic_v1.jsonl --out data/topics/paraphrase_v1.jsonl
python -m scripts.build_topic_dataset --prelabel --in data/topics/synthetic_v1.jsonl
python -m scripts.build_topic_dataset --audit-sample --out evals/topics/audit_sample.csv
python -m scripts.build_topic_dataset --apply-audit --audit evals/topics/audit_corrections.jsonl --in data/topics/synthetic_v1.jsonl
python -m scripts.build_topic_dataset --pull-real --out data/topics/real_audit.csv
python -m scripts.build_topic_dataset --apply-real-audit --audit data/topics/real_audit.csv --out data/topics/real_train.jsonl
python -m scripts.build_topic_dataset --split --seed 42 --out .cache/topics/splits
```

`--pull-real` 需要连数据库并调用 LLM 清洗与预标，产出带空 `correct_labels` 的人工确认表；`--apply-real-audit` 只读本地文件，不连库。其余模式只读已落盘文件或写新文件。每个模式结束打印一行中文摘要。

## 6. 数据模型与迁移

### 6.1 ORM（`app/models.py`）

```python
class TopicClassification(Base):
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

与用户 DDL 逐项对齐：`labels JSON NOT NULL`、`classified_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP`、`UNIQUE KEY uk_question_id`、`CONSTRAINT fk_topic_question`。SQLite 测试库由 `Base.metadata.create_all` 建表。

### 6.2 建表与迁移

- `scripts/init_sql/008_topics.sql`：用户 DDL 原样（含 `SET NAMES utf8mb4;`）。
- `scripts/migrate_ch10.py`：照 `migrate_ch09.py` 的结构，`NEW_TABLES = ("topic_classifications",)`，对 MySQL/SQLite 都用 `table.create(conn, checkfirst=True)`；本表无新增列/索引，函数只返回「表 topic_classifications」或「结构已是最新」。
- `scripts/init_db.py` 无需改：`create_all` 会带上新表；已有库跑 `python scripts/migrate_ch10.py`。

### 6.3 写入语义

- 一条池行只有一行归类：`UNIQUE(question_id)` 是最终防线。
- 默认只处理未归类行；`--force` 时对已有行更新 `labels` 与 `classified_at`。
- 先用 `SELECT ... WHERE question_id IN (...)` 查已有行，再逐条 update/insert，最后统一 commit；并发撞唯一键时捕获 `IntegrityError`，回滚该批并记 `failed`，不整批丢结果。
- 标签写库前统一 `validate_labels()`；空标签回落 `["其他"]`。

## 7. 训练

### 7.1 预训练权重

```bash
HF_ENDPOINT=https://hf-mirror.com .venv/Scripts/python.exe -c \
  "from huggingface_hub import snapshot_download; snapshot_download('hfl/chinese-roberta-wwm-ext', local_dir='.cache/models/chinese-roberta-wwm-ext')"
```

`start-topic.bat` 与 README 给同一条命令；下载后校验 `config.json`、`pytorch_model.bin`、`vocab.txt` 存在。下载失败不在代码里偷偷换模型，停下来找用户开代理。

### 7.2 超参与正则化（`scripts/train_topic_classifier.py`）

| 项 | 值 | 理由 |
|---|---|---|
| `max_len` | 128 | 问题句 8~40 字，128 足够且省显存 |
| `batch_size` | 8 | 6GB 起始值；OOM 降到 4 |
| `grad_accum` | 2 | 等效 batch 16 |
| `epochs` | 8 | 配合早停，通常 3~5 轮停 |
| `lr` | 2e-5 | BERT 全参微调常用值 |
| `weight_decay` | 0.01 | 正则化 |
| `warmup_ratio` | 0.1 | 稳定前期 |
| `dropout` | 0.1 | classifier dropout |
| `patience` | 2 | 验证集 macro-F1 连续两轮不升即停 |
| `seed` | 42 | 与数据切分一致 |
| AMP | CUDA bf16 | 4050 支持；CPU 用 fp32 |
| 梯度裁剪 | 1.0 | 防炸 |

### 7.3 训练循环

- 手写 PyTorch 循环，不装 `accelerate`、不用 `Trainer`、不装 `datasets`。
- 数据集：读 `train.jsonl`/`val.jsonl`，tokenizer 批量编码，标签转 17 维 float 向量，`BCEWithLogitsLoss`。
- 优化器 AdamW；scheduler 线性衰减；每轮结束在验证集算 macro-F1（阈值固定 0.5，仅用于选模型）。
- 保存验证集 macro-F1 最高的 state dict；早停触发或轮次用尽后恢复最佳权重。
- 训练结束在**最佳模型**上逐类调阈值：0.20~0.80 步长 0.05，最大化该类 F1；该类验证集无正例时阈值取 0.5。
- 产物写 `.cache/models/topic-classifier/`：`model.safetensors`（`save_pretrained` 默认）、tokenizer 文件、`labels.json`、`thresholds.json`、`train_meta.json`（超参、耗时、最佳轮次、显存峰值）。
- 训练报告 `evals/reports/ch10-train-<ts>.md`：每轮 train loss / val macro-F1、早停轮次、阈值表、显存峰值。
- `--smoke`：取 40 条训练、10 条验证，1 轮、`max_len=64`、batch 4，只验证显存、保存与加载；不产出正式指标。
- OOM 处理：捕获 `torch.cuda.OutOfMemoryError`，打印「把 `--batch-size` 降到 4、`--max-len` 降到 96 后重跑」，退出非零，不静默降级。

### 7.4 训练命令

```bash
python -m scripts.train_topic_classifier --smoke
python -m scripts.train_topic_classifier \
  --train .cache/topics/splits/train.jsonl \
  --val .cache/topics/splits/val.jsonl \
  --out .cache/models/topic-classifier
```

真实问题去重后的 13 条与增强样本在构建阶段已并入 `train.jsonl`；训练脚本只认文件，不直接连库、不调 LLM。

## 8. 评测

`scripts/evaluate_topic_classifier.py`：

- 输入测试集、模型目录、可选真实样本文件；输出 `evals/reports/ch10-topic-<ts>.json`、`.md`、`.html`。
- 指标：
  - 逐类 precision / recall / F1 / support（sklearn，`zero_division=0`）；
  - macro-F1、micro-F1、subset accuracy（整条完全命中）、Hamming loss；
  - 每类 2×2 混淆矩阵（TP/FP/FN/TN），JSON 全量 + Markdown 表；
  - 17×17「真标签 × 预测标签」共现矩阵：每条样本的每个真标签与每个预测标签计数；Markdown 只列非对角 Top 15 混淆对，完整矩阵进 JSON；
  - HTML 用自写 CSS grid 热力图，不依赖 matplotlib。
- 多标签验收：读 `evals/topics/multilabel_cases.jsonl`，逐条输出「文本 / 期望标签 / 预测标签 / 是否全部命中」；`买大了想退` 必须同时含 `退换货` 与 `尺码`，否则脚本退出非零。
- 真实抽检：若 `data/topics/real_train.jsonl` 存在，输出去重后 13 条真实问题的「标签 vs 预测」表（原始 17 行的完整归类结果由验收②的批量脚本展示），并明确标注「已进训练集，仅观察真实分布」；文件不存在则打印提示并跳过，不算失败。
- 判错样本导出：按 FN/FP 数量和类别覆盖选约 25 条，写 `evals/topics/error_review.csv`，列 `id,text,true_labels,pred_labels,error_type,note`；`error_type` 由人工填（术语边界/标注错/数据不足/模型容量），复核结论进 Markdown 报告。
- 退出码：模型缺失、测试集为空、多标签用例失败 → 非零；指标本身差不判失败，如实报告。

## 9. 推理服务

### 9.1 模型封装（`app/topics/model.py`）

```python
@dataclass(frozen=True)
class TopicPrediction:
    labels: list[str]
    scores: dict[str, float]      # 17 类概率，按 taxonomy 顺序

class ModelNotReady(RuntimeError): ...

class TopicModel:
    def __init__(self, model_dir: Path, *, device: str = "cpu", max_len: int = 128): ...
    def predict(self, texts: list[str]) -> list[TopicPrediction]: ...
    @staticmethod
    def is_ready(model_dir: Path) -> bool: ...

def apply_thresholds(probs: Sequence[float], thresholds: Sequence[float]) -> list[str]: ...
```

- 加载 `labels.json`、`thresholds.json`；缺任一文件抛 `ModelNotReady`。
- `torch.no_grad()` + `model.eval()`；批量最多 64 条；返回概率保留 4 位小数。
- 阈值判定与「其他」回落抽成纯函数 `apply_thresholds(probs, thresholds) -> list[str]`，单测覆盖：过阈值多标签、全不过回落、边界等于阈值算命中。

### 9.2 服务（`app/topics/service.py`）

- FastAPI；`GET /health` → `{"status":"ok"|"not_ready","model_dir":...,"labels":17,"device":"cpu"}`；模型缺失也允许启动，`/health` 报 `not_ready`。
- `POST /classify`：

```json
{"texts":["买大了想退","运费怎么算"]}
→ {"items":[{"labels":["退换货","尺码"],"scores":{"退换货":0.98,"尺码":0.91,...}}, ...]}
```

- 校验：`texts` 1~64 条、每条去空白后非空、总字符 ≤ 20000；违反返回 422。模型未就绪返回 503 `{"code":"model_not_ready","message":"分类模型还没训练好，请先运行 scripts/train_topic_classifier.py。"}`。
- `create_app(model_dir=None, predictor=None)`：测试注入假 predictor，不加载真模型。
- 启动：`python -m app.topics.service --host 127.0.0.1 --port 8933`；`start-topic.bat` 双击启动并提示地址。
- **默认 CPU**：避免与 bge-m3/reranker 抢 6GB 显存；`TOPIC_DEVICE=cuda` 可切换。批量 50 条短文本 CPU 推理在秒级，够用。

## 10. 批量归类与后台调度

### 10.1 worker（`app/topics/worker.py`）

```python
@dataclass(frozen=True)
class ClassifyReport:
    pending: int = 0
    classified: int = 0
    failed: int = 0

def pending_count(session_factory) -> int: ...
def pending_questions(session_factory, limit: int) -> list[tuple[int, str]]: ...
def store_classifications(session_factory, results: dict[int, list[str]],
                          *, force: bool = False) -> int: ...
def run_batch(session_factory, classify: Callable[[list[str]], list[list[str]]],
              *, min_pending: int = 20, limit: int = 50,
              force: bool = False) -> ClassifyReport: ...
```

- `pending_questions`：`low_confidence_questions LEFT JOIN topic_classifications`，只取 `topic_classifications.id IS NULL`，按池行 id 升序，`limit` 条；`force=True` 时不限未归类。
- `run_batch`：未归类数低于 `min_pending` 直接返回（`classified=0`，不调服务）；否则取 `limit` 条，调用 `classify(texts)`；服务异常记 warning，返回 `failed=len(rows)`，不抛给调用方。
- `TopicClient`：同步 `httpx.Client`，30 秒超时，POST `{base_url}/classify`；非 200、JSON 结构不对、返回条数不匹配都抛异常。
- 服务返回空标签时写 `["其他"]`；未知标签直接记该条 failed，不写脏数据。

### 10.2 调度（`app/topics/scheduler.py`）

- 照 `EvalScheduler`：`APScheduler` 的 `BackgroundScheduler`，`interval` 分钟、`max_instances=1`、`coalesce=True`，`start()`/`shutdown()`。
- 主应用 lifespan：`topic_classify_enabled=True` 时构造 `TopicClient` 与 `TopicScheduler`，任务体调用 `run_batch(...)` 并打一行中文日志；`finally` 里 `shutdown()`（在 engine dispose 之前）。
- 默认关闭（`TOPIC_CLASSIFY_ENABLED=false`），与定时评估一致，避免测试与未起服务时刷日志；README 与演示脚本写明打开方式。
- 服务没起：任务体捕获异常只记一次 warning，聊天不受影响；不重试堆积。

### 10.3 手动脚本

`scripts/classify_topics.py`：

```bash
python -m scripts.classify_topics --min-pending 1 --limit 100
python -m scripts.classify_topics --force --limit 17
```

- 与调度共用 `run_batch`；`--force` 重算已归类行；结束打印 `待归类/已归类/失败`。
- 服务不可达时退出码 2，并提示先运行 `start-topic.bat`。

## 11. 后台主题分布页（Vibe Coding）

### 11.1 接口

`GET /api/admin/topics/distribution`：

```json
{
  "ok": true,
  "total_classified": 17,
  "pending": 0,
  "multi_label": 5,
  "last_classified_at": "2026-11-02T18:00:00",
  "classes": [
    {"label": "退换货", "count": 3, "pct": 0.1765},
    ...  // 17 类固定顺序，含 0 计数
  ]
}
```

- 计数在 Python 侧解析 JSON 完成，不依赖 MySQL JSON 函数，SQLite 测试同路。
- `pct = count / total_classified`，无归类时全 0；保留 4 位小数。
- DB 不可用沿用现有 `ModelFailure("database_unavailable", ..., 503)`。

### 11.2 页面

- `/admin/topics` → `app/web/admin-topics.html` + `admin-topics.js`；`admin.html` 顶部加「主题分布」链接。
- 内容：四个摘要数字（已归类、待归类、多标签、最近归类时间）+ 17 类横向条形图（纯 CSS 宽度，按最大值归一）+ 百分比；顶部有「刷新」。
- 空状态：没有归类数据时显示「还没有归类结果。启动推理服务后运行 `python -m scripts.classify_topics`，或打开 `TOPIC_CLASSIFY_ENABLED` 等后台自动归类。」
- 错误文案沿用审核页：连不上后台/数据库不可用/服务未就绪，用人话，不显示 traceback。
- 浏览器实测：启动应用与推理服务，跑一批归类，打开 `/admin/topics` 核对数字与条形图；不套 TDD/review。

## 12. 配置项汇总（`.env.example` 增补）

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

`Settings` 字段与校验：

| 字段 | 默认 | 约束 |
|---|---|---|
| `topic_classify_enabled` | `False` | bool |
| `topic_service_url` | `http://127.0.0.1:8933` | 非空、长度 ≤255 |
| `topic_classify_interval_minutes` | 10 | 1~1440 |
| `topic_classify_min_pending` | 20 | ≥1 |
| `topic_classify_batch` | 50 | 1~200 |
| `topic_model_dir` | `.cache/models/topic-classifier` | 非空 |
| `topic_device` | `cpu` | `Literal["cpu","cuda"]` |
| `topic_max_len` | 128 | 32~256 |

`tests/support.py` 的默认配置加 `"topic_classify_enabled": False`。

## 13. 测试策略

### 13.1 TDD 覆盖（可单测的代码）

| 测试文件 | 断言要点 |
|---|---|
| `tests/test_ch10_taxonomy.py` | 17 类唯一、顺序固定、每类边界非空、4 条近邻规则原文存在；`order_labels` 去重排序；未知标签抛 `ValueError` |
| `tests/test_ch10_clean.py` | 手机号/身份证/订单号脱敏；NFKC 全角转半角；零宽字符与连续空白清理；空串与纯空白；幂等（清洗两次结果一致） |
| `tests/test_ch10_dataset.py` | 精确/近似去重；切分无交集且并集完整；每类占比偏差 ≤2 个百分点；同 seed 可复现；每个 ≥10 样本的类在 val/test 至少 1 条；增强只挂训练样本且变体随 seed 同组；审计修正覆盖标签 |
| `tests/test_ch10_model.py` | `apply_thresholds`：多标签、全不过回落「其他」、等于阈值命中、未知标签不输出 |
| `tests/test_ch10_service.py` | 假 predictor：`/health`、`/classify` 正常；空/超 64 条/超长 422；模型缺失 503；返回 labels 与 taxonomy 顺序一致 |
| `tests/test_ch10_worker.py` | SQLite：pending 只取未归类；写库幂等（重复跑不新增）；`force` 更新 labels 与时间；空标签回落「其他」；未知标签计 failed；服务异常返回 failed 不抛 |
| `tests/test_ch10_admin_api.py` | 分布接口 17 类固定顺序、计数/百分比/多标签/待归类正确；空库全 0；DB 不可用 503 |
| `tests/test_ch10_schema.py` | ORM 与 `008_topics.sql` 的列名、类型、`uk_question_id`、`fk_topic_question` 一致；重复 `question_id` 插入报 `IntegrityError` |
| `tests/test_ch10_migrate.py` | SQLite 上 `migrate` 幂等；重复执行第二次返回空列表 |

### 13.2 标注集/评估集验证（替代 TDD 的部分）

- **生成 Prompt**：抽 30 条生成结果人工看是否口语化、是否只含目标诉求、是否有隐私串；统计解析失败率。
- **预标 Prompt**：在 50 条审计样本上算与目标标签一致率；一致率 <80% 时先改 Prompt 再重建语料，不直接开训。
- **训练**：`--smoke` 证明显存与保存/加载；全量训练看每轮 val macro-F1 是否上升、早停是否在 8 轮内触发；训练报告写入留痕。
- **评测**：测试集逐类指标 + 25 条判错人工归因；若某类 F1 明显低，先看混淆对与样本，不手调阈值凑分。
- **前端**：浏览器实测分布页与审核页导航，检查空状态与错误状态。

### 13.3 回归

- 新增配置默认关闭，不改变现有 630 个用例行为。
- 全量 `pytest` 必须通过；新增测试单独可见。
- README、`.env.example`、数据库迁移说明同步更新。

## 14. 交付物与验收命令

**代码**

- 新增 `app/topics/{taxonomy,clean,prompts,dataset,model,service,worker,scheduler,stats}.py`
- 新增 `scripts/{build_topic_dataset,train_topic_classifier,evaluate_topic_classifier,classify_topics,demo_topics,migrate_ch10}.py`
- 新增 `scripts/init_sql/008_topics.sql`
- 修改 `app/models.py`、`app/main.py`、`app/config.py`、`tests/support.py`、`requirements.txt`、`.env.example`、`.gitignore`、`README.md`
- 前端新增 `app/web/admin-topics.html`、`admin-topics.js`，修改 `admin.html`、`admin.css`
- 新增 `start-topic.bat`（ASCII + CRLF，双击可跑）
- 数据与评估集：`data/topics/synthetic_v1.jsonl`、`paraphrase_v1.jsonl`、`README.md`；`evals/topics/{audit_sample.csv,audit_corrections.jsonl,multilabel_cases.jsonl,error_review.csv}`

**逐条验收**

① 测试集指标与混淆矩阵：

```bash
python -m scripts.evaluate_topic_classifier \
  --test .cache/topics/splits/test.jsonl \
  --model-dir .cache/models/topic-classifier
```

打开 `evals/reports/ch10-topic-*.md` 看逐类 P/R/F1、2×2 矩阵、Top 混淆对与 HTML 热力图。

② 真实问题批量归类 + 后台分布：

```bash
start-topic.bat                        # 另开窗口
python -m scripts.classify_topics --force --limit 17
# 浏览器打开 http://127.0.0.1:8000/admin/topics
```

③ 多标签：

```bash
python -m scripts.demo_topics
```

脚本必须证明 `买大了想退` 同时命中 `退换货` 与 `尺码`，并覆盖其余 5 条固定用例。

**演示脚本** `scripts/demo_topics.py`：

1. 检查模型与测试集存在，否则打印缺失命令并退出 2；
2. 跑评测并打印 macro-F1、逐类 F1 表、Top 混淆对；
3. 调推理服务归类真实池（服务不可达则提示先启动，退出 2）；
4. 打印 6 条多标签用例结果，断言验收③；
5. 打印分布接口的摘要数字与 `/admin/topics` 地址。

**留痕与提交**

- 每个阶段追加 `dev-notes/ch10.md`（用户原话 / 关键产出 / 拒绝或纠偏 / 翻车与返工），不攒到最后。
- 每章一次提交，标题：`第十次提交（ch10）：多标签主题分类器与主题分布`；提交前扫密钥与 DB 原文，`data/topics/real_train.jsonl` 必须被忽略。
- 推送 GitHub 前先告诉用户开代理。

## 15. 风险与未决

| 风险 | 影响 | 兜底 |
|---|---|---|
| 6GB 显存全参微调 OOM | 训练中断 | 先 smoke；batch 8→4、`max_len` 128→96、梯度累积补等效批量；仍失败停下来问用户，不自行换模型 |
| 模型下载失败 | 无法开训 | 已确认 hf-mirror 可访问；失败时请用户开代理，不换模型 |
| transformers 5.x API 变更 | 训练脚本报错 | 只用已导入验证的 `BertForSequenceClassification`、tokenizer、scheduler；编码前再跑一次探针 |
| 生成语料标签噪声 | 指标不可信 | 独立预标 + 50 条人工抽审；预标一致率 <80% 先改 Prompt |
| 每类测试样本仅 6~8 条 | 单类 F1 波动大 | 报告标 support；主看 macro-F1 与混淆对；不吹单类分数 |
| 真实池噪声（「订单 1001」） | 真实抽检表难看 | 保留为 `其他` 训练样本；报告中如实说明 |
| 推理服务与聊天抢显存 | 聊天变慢或 OOM | 服务默认 CPU；需要 CUDA 时由用户显式打开 |
| 后台定时器与手动脚本并发 | 重复写库 | 唯一键兜底 + `IntegrityError` 回滚该批；调度器 `max_instances=1` |
| 人工抽审拖延 | 训练无法定稿 | 先把审计表与命令交付；未审前用预标标签跑通链路，最终报告注明审计状态 |

**未决事项**：无。三项关键选择已由用户确认：约 1200 条语料、真实 17 行去重后全部进训练集并单列真实抽检、主应用定时任务调推理服务；推理默认 CPU、不采用 ONNX 也已在设计确认。
