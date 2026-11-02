"use strict";

const queue = document.querySelector("#queue");
const emptyHint = document.querySelector("#empty");
const statusLine = document.querySelector("#status-line");
const totalEl = document.querySelector("#queue-total");
const scopeEl = document.querySelector("#queue-scope");
const refreshButton = document.querySelector("#refresh");
const tabs = Array.from(document.querySelectorAll(".tab"));

const STATUS_CLASS = { "待审": "is-pending", "通过": "is-approved", "驳回": "is-rejected" };
const SOURCE_LABELS = {
  retrieval_low_conf: "检索没答上",
  self_check: "模型自评知识不够",
  user_feedback: "用户点了没用",
};
const GUIDANCE = "召回片段为空 = 知识库确实没这块；有片段但分数都很低 = 有相关内容但没检到，先别急着加新条目。";
const PAGE_LIMIT = 100;

const state = { status: "待审", total: 0 };
let listRevision = 0;

function setStatus(text) {
  statusLine.textContent = text;
}

function updateCount() {
  totalEl.textContent = String(state.total);
  scopeEl.textContent = state.status || "全部";
}

function readableError(payload) {
  const messages = {
    database_unavailable: "后台存储暂时不可用，请稍后刷新。",
    review_not_found: "这条记录不存在。",
    review_conflict: "这条记录不存在，或已经通过入库，不能再驳回。",
    already_approved: "这条已经通过过了。",
    empty_answer: "请先填写终答。",
    invalid_request: "填写的内容不符合要求，请检查后重试。",
  };
  return messages[payload?.code] || "操作没成功，请稍后刷新重试。";
}

async function load() {
  const revision = ++listRevision;
  setStatus("正在加载…");
  refreshButton.disabled = true;
  const params = new URLSearchParams({ limit: String(PAGE_LIMIT) });
  if (state.status) params.set("status", state.status);
  try {
    const response = await fetch("/api/admin/review-queue?" + params.toString());
    const payload = await response.json().catch(() => null);
    if (revision !== listRevision) return;
    if (!response.ok) {
      setStatus(readableError(payload));
      return;
    }
    render(payload.items || [], payload.total || 0);
    setStatus("");
  } catch {
    if (revision === listRevision) setStatus("连不上后台服务，请确认服务已启动。");
  } finally {
    if (revision === listRevision) refreshButton.disabled = false;
  }
}

function render(items, total) {
  queue.replaceChildren();
  for (const item of items) queue.append(buildRow(item));
  state.total = total;
  updateCount();
  emptyHint.hidden = items.length > 0;
}

function smallMeta(text) {
  const span = document.createElement("span");
  span.className = "row-meta";
  span.textContent = text;
  return span;
}

function buildRow(item) {
  const row = document.createElement("article");
  row.className = "row";

  const head = document.createElement("button");
  head.type = "button";
  head.className = "row-head";
  head.setAttribute("aria-expanded", "false");

  const titleLine = document.createElement("span");
  titleLine.className = "row-titleline";
  const title = document.createElement("span");
  title.className = "row-title";
  title.textContent = item.normalized_question || "（无标准问）";
  const badge = document.createElement("span");
  badge.className = "badge " + (STATUS_CLASS[item.review_status] || "");
  badge.textContent = item.review_status || "";
  titleLine.append(title, badge);

  const meta = document.createElement("span");
  meta.className = "row-meta-line";
  meta.append(smallMeta("出现 " + (item.occurrence_count ?? 0) + " 次"),
              smallMeta("更新于 " + (item.updated_at || "—")));

  const preview = document.createElement("span");
  preview.className = "row-preview";
  preview.textContent = item.ai_suggested_answer || "（AI 没给示例答案，需要人工填写）";

  const chevron = document.createElement("span");
  chevron.className = "row-chevron";
  chevron.setAttribute("aria-hidden", "true");
  chevron.textContent = "⌄";

  const main = document.createElement("span");
  main.className = "row-main";
  main.append(titleLine, meta, preview);
  head.append(main, chevron);

  const actions = document.createElement("span");
  actions.className = "row-actions";
  const approveButton = document.createElement("button");
  approveButton.type = "button";
  approveButton.className = "action is-primary";
  approveButton.textContent = "通过";
  const rejectButton = document.createElement("button");
  rejectButton.type = "button";
  rejectButton.className = "action";
  rejectButton.textContent = "驳回";
  const note = document.createElement("span");
  note.className = "row-note";
  note.setAttribute("role", "status");
  actions.append(approveButton, rejectButton, note);

  const detail = document.createElement("div");
  detail.className = "row-detail";
  detail.hidden = true;

  let expanded = false;
  let detailBody = null;
  let detailPromise = null;

  function ensureDetail() {
    if (expanded && (detailBody || detailPromise)) return detailPromise || Promise.resolve(detailBody);
    expanded = true;
    row.classList.add("is-open");
    head.setAttribute("aria-expanded", "true");
    detail.hidden = false;
    if (!detailPromise) {
      detail.replaceChildren(detailMessage("正在加载详情…"));
      detailPromise = fetchDetail(item.id).then(loaded => {
        if (!loaded) {
          detailPromise = null;
          detail.replaceChildren(detailMessage("详情没加载出来，收起后再试一次。"));
          return null;
        }
        detailBody = buildDetail(loaded, item);
        detail.replaceChildren(detailBody);
        return detailBody;
      });
    }
    return detailPromise;
  }

  function collapse() {
    expanded = false;
    row.classList.remove("is-open");
    head.setAttribute("aria-expanded", "false");
    detail.hidden = true;
  }

  head.addEventListener("click", () => {
    if (expanded) collapse();
    else void ensureDetail();
  });

  approveButton.addEventListener("click", () => {
    void approveRow(item, row, approveButton, rejectButton, note, ensureDetail);
  });
  rejectButton.addEventListener("click", () => {
    void rejectRow(item, row, approveButton, rejectButton, note);
  });

  if (item.review_status !== "待审") {
    approveButton.disabled = true;
    rejectButton.disabled = true;
  }

  row.append(head, actions, detail);
  return row;
}

function detailMessage(text) {
  const p = document.createElement("p");
  p.className = "detail-message";
  p.textContent = text;
  return p;
}

async function fetchDetail(id) {
  try {
    const response = await fetch("/api/admin/review-queue/" + id);
    const payload = await response.json().catch(() => null);
    if (!response.ok || !payload) {
      if (payload?.code) setStatus(readableError(payload));
      return null;
    }
    return payload;
  } catch {
    setStatus("连不上后台服务，请确认服务已启动。");
    return null;
  }
}

function buildDetail(detail, item) {
  const body = document.createElement("div");
  body.className = "detail";

  const answerBlock = document.createElement("section");
  answerBlock.className = "detail-block";
  const answerTitle = document.createElement("h2");
  answerTitle.textContent = "AI 示例答案";
  const suggested = document.createElement("p");
  suggested.className = "suggested";
  suggested.textContent = detail.ai_suggested_answer || "（AI 没给示例答案）";
  const label = document.createElement("label");
  label.className = "final-label";
  label.htmlFor = "final-answer-" + detail.id;
  label.textContent = "终答（通过时写入知识库）";
  const textarea = document.createElement("textarea");
  textarea.className = "final-answer";
  textarea.id = "final-answer-" + detail.id;
  textarea.rows = 4;
  textarea.maxLength = 2000;
  textarea.value = detail.approved_answer || detail.ai_suggested_answer || "";
  textarea.placeholder = "写成知识库能直接复用的答案，审核通过后会入库并向量化。";
  if (item.review_status !== "待审") {
    textarea.readOnly = true;
    textarea.classList.add("is-readonly");
  }
  const finalNote = document.createElement("p");
  finalNote.className = "final-note";
  finalNote.textContent = item.review_status === "待审"
    ? "通过前可以直接改这句；改完再点右侧「通过」。"
    : "这条已经处理过，终答只读。";
  answerBlock.append(answerTitle, suggested, label, textarea, finalNote);

  const sources = Array.isArray(detail.sources) ? detail.sources : [];
  const sourceBlock = document.createElement("section");
  sourceBlock.className = "detail-block";
  const sourceTitle = document.createElement("h2");
  sourceTitle.textContent = "归并进来的用户原话（" + sources.length + " 条）";
  sourceBlock.append(sourceTitle);
  if (!sources.length) {
    const none = document.createElement("p");
    none.className = "sources-empty";
    none.textContent = "还没有归并原话。";
    sourceBlock.append(none);
  } else {
    const list = document.createElement("div");
    list.className = "sources";
    for (const source of sources) list.append(buildSource(source));
    sourceBlock.append(list);
  }

  const guidance = document.createElement("p");
  guidance.className = "guidance";
  guidance.textContent = GUIDANCE;

  body.append(answerBlock, sourceBlock, guidance);
  return body;
}

function buildSource(source) {
  const card = document.createElement("div");
  card.className = "source";

  const top = document.createElement("div");
  top.className = "source-top";
  const raw = document.createElement("p");
  raw.className = "source-raw";
  raw.textContent = source.raw_question || "（无原话）";
  const tag = document.createElement("span");
  tag.className = "source-tag";
  tag.textContent = SOURCE_LABELS[source.source] || source.source || "未知来源";
  top.append(raw, tag);

  const meta = document.createElement("p");
  meta.className = "source-meta";
  const parts = [];
  if (source.conversation_id) parts.push("会话 #" + source.conversation_id);
  if (source.created_at) parts.push(source.created_at);
  if (source.reason) parts.push(source.reason);
  meta.textContent = parts.join(" · ");

  const chunks = Array.isArray(source.retrieved_chunks) ? source.retrieved_chunks : [];
  const details = document.createElement("details");
  details.className = "chunks";
  const summary = document.createElement("summary");
  summary.textContent = chunks.length
    ? "当时的召回片段（" + chunks.length + " 条）"
    : "当时的召回片段（0 条，知识库确实没有这块）";
  details.append(summary);
  if (chunks.length) {
    const list = document.createElement("div");
    list.className = "chunk-list";
    for (const chunk of chunks) list.append(buildChunk(chunk));
    details.append(list);
  }

  card.append(top, meta, details);
  return card;
}

function chunkScore(chunk) {
  const value = chunk?.scores?.rerank;
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function buildChunk(chunk) {
  const box = document.createElement("div");
  box.className = "chunk";

  const head = document.createElement("div");
  head.className = "chunk-head";
  const question = document.createElement("p");
  question.className = "chunk-q";
  question.textContent = (chunk.n ? chunk.n + ". " : "") + (chunk.question || "（无问题）");
  const score = chunkScore(chunk);
  const scoreTag = document.createElement("span");
  scoreTag.className = "chunk-score " + (score === null ? "is-unknown"
    : score >= 0.6 ? "is-high" : score >= 0.3 ? "is-mid" : "is-low");
  scoreTag.textContent = score === null ? "无精排分" : "精排分 " + score.toFixed(2);
  head.append(question, scoreTag);

  box.append(head);
  if (chunk.section_path) {
    const path = document.createElement("p");
    path.className = "chunk-path";
    path.textContent = chunk.section_path;
    box.append(path);
  }
  const answer = document.createElement("p");
  answer.className = "chunk-a";
  answer.textContent = chunk.answer || "（无原文）";
  box.append(answer);
  return box;
}

function setActionsEnabled(approveButton, rejectButton, enabled) {
  approveButton.disabled = !enabled;
  rejectButton.disabled = !enabled;
}

function markStatus(item, row, statusText, approveButton, rejectButton) {
  item.review_status = statusText;
  const badge = row.querySelector(".badge");
  if (badge) {
    badge.className = "badge " + (STATUS_CLASS[statusText] || "");
    badge.textContent = statusText;
  }
  approveButton.disabled = true;
  rejectButton.disabled = true;
  const textarea = row.querySelector(".final-answer");
  if (textarea) {
    textarea.readOnly = true;
    textarea.classList.add("is-readonly");
  }
  if (state.status === "待审") {
    row.remove();
    state.total = Math.max(0, state.total - 1);
    emptyHint.hidden = queue.children.length > 0;
    updateCount();
  } else if (state.status && state.status !== statusText) {
    void load();
  }
}

async function approveRow(item, row, approveButton, rejectButton, note, ensureDetail) {
  if (approveButton.disabled) return;
  note.textContent = "";
  const detailBody = await ensureDetail();
  if (!detailBody) {
    note.textContent = "详情还没加载出来，稍后再试。";
    return;
  }
  const textarea = detailBody.querySelector(".final-answer");
  const answer = textarea ? textarea.value.trim() : "";
  if (!answer) {
    note.textContent = "请先填写终答。";
    textarea?.focus();
    return;
  }
  setActionsEnabled(approveButton, rejectButton, false);
  note.textContent = "正在入库…";
  try {
    const response = await fetch("/api/admin/review-queue/" + item.id + "/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answer }),
    });
    const payload = await response.json().catch(() => null);
    if (response.ok && payload?.ok) {
      note.textContent = "";
      markStatus(item, row, "通过", approveButton, rejectButton);
      return;
    }
    note.textContent = readableError(payload);
    if (payload?.code === "already_approved") {
      void load();
      return;
    }
    setActionsEnabled(approveButton, rejectButton, true);
    if (payload?.code === "empty_answer") textarea?.focus();
  } catch {
    note.textContent = "请求没送出去，请确认服务在运行。";
    setActionsEnabled(approveButton, rejectButton, true);
  }
}

async function rejectRow(item, row, approveButton, rejectButton, note) {
  if (rejectButton.disabled) return;
  const reason = window.prompt("驳回理由（只进日志）");
  if (reason === null) return;
  setActionsEnabled(approveButton, rejectButton, false);
  note.textContent = "正在驳回…";
  try {
    const body = reason.trim() ? { reason: reason.trim() } : {};
    const response = await fetch("/api/admin/review-queue/" + item.id + "/reject", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => null);
    if (response.ok && payload?.ok) {
      note.textContent = "";
      markStatus(item, row, "驳回", approveButton, rejectButton);
      return;
    }
    note.textContent = readableError(payload);
    setActionsEnabled(approveButton, rejectButton, true);
    if (payload?.code === "review_conflict") void load();
  } catch {
    note.textContent = "请求没送出去，请确认服务在运行。";
    setActionsEnabled(approveButton, rejectButton, true);
  }
}

for (const tab of tabs) {
  tab.addEventListener("click", () => {
    const next = tab.dataset.status;
    if (next === state.status) return;
    state.status = next;
    for (const other of tabs) {
      const active = other === tab;
      other.classList.toggle("is-active", active);
      other.setAttribute("aria-selected", active ? "true" : "false");
    }
    void load();
  });
}

refreshButton.addEventListener("click", () => { void load(); });
updateCount();
void load();
