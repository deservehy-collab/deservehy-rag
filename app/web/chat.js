"use strict";

const form = document.querySelector("#chat-form");
const input = document.querySelector("#message-input");
const sendButton = document.querySelector("#send-button");
const stopButton = document.querySelector("#stop-button");
const newChatButton = document.querySelector("#new-chat");
const messages = document.querySelector("#messages");
const welcome = document.querySelector("#welcome");
const scrollArea = document.querySelector("#conversation-scroll");
const jumpButton = document.querySelector("#jump-bottom");
const headerStatus = document.querySelector("#header-status");
const announcer = document.querySelector("#announcer");
const citationLayer = document.querySelector("#citation-layer");
const citationPanel = citationLayer.querySelector(".citation-panel");
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const CLIENT_KEY = "deservehy-client-id";
const FEEDBACK_PREFIX = "deservehy-feedback:";
let history = [];
let conversationId = null;
let cachedClientId = null;
let activeTurn = null;
let followBottom = true;

function clientId() {
  if (cachedClientId) return cachedClientId;
  try {
    const stored = localStorage.getItem(CLIENT_KEY);
    if (stored && /^[A-Za-z0-9_-]{8,64}$/.test(stored)) {
      cachedClientId = stored;
      return cachedClientId;
    }
    const generated = (crypto.randomUUID?.() || `${Date.now()}${Math.random().toString(36).slice(2)}`)
      .replace(/[^A-Za-z0-9_-]/g, "")
      .slice(0, 64);
    localStorage.setItem(CLIENT_KEY, generated);
    cachedClientId = generated;
  } catch {
    cachedClientId = "anonymous" + Date.now().toString(36);
  }
  return cachedClientId;
}

function svgIcon(href) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", href);
  svg.append(use);
  return svg;
}

function scrollToLatest(force = false) {
  if (followBottom || force) scrollArea.scrollTop = scrollArea.scrollHeight;
}

function updateInput() {
  input.style.height = "auto";
  input.style.height = Math.min(148, Math.max(51, input.scrollHeight)) + "px";
  sendButton.disabled = Boolean(activeTurn) || !input.value.trim();
}

function setBusy(busy) {
  sendButton.hidden = busy;
  stopButton.hidden = !busy;
  newChatButton.disabled = busy;
  headerStatus.textContent = busy ? "正在为你整理回复…" : "关于购物，尽管问我";
  updateInput();
}

function messageElement(role, text = "", displayName = "") {
  const article = document.createElement("article");
  article.className = "message message-" + role;
  const avatar = document.createElement("div");
  avatar.className = "message-avatar";
  avatar.setAttribute("aria-hidden", "true");
  if (role === "assistant") {
    avatar.append(svgIcon("#icon-chat"));
  } else {
    avatar.textContent = "我";
  }
  const stack = document.createElement("div");
  stack.className = "message-stack";
  const meta = document.createElement("div");
  meta.className = "message-meta";
  const name = document.createElement("span");
  name.textContent = displayName || (role === "assistant" ? "小邻客服" : "你");
  const now = new Date();
  const time = document.createElement("time");
  time.dateTime = now.toISOString();
  time.textContent = now.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  meta.append(name, time);
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  const content = document.createElement("span");
  content.className = "message-text";
  const textNode = document.createTextNode(text);
  content.append(textNode);
  bubble.append(content);
  stack.append(meta, bubble);
  article.append(avatar, stack);
  messages.append(article);
  return { article, stack, bubble, content, textNode, tools: new Map() };
}

function createToolBadge(view) {
  const badge = document.createElement("div");
  badge.className = "tool-badge is-running";
  badge.setAttribute("aria-hidden", "true");
  const glyph = document.createElement("span");
  glyph.className = "tool-glyph";
  const label = document.createElement("span");
  label.className = "tool-label";
  badge.append(glyph, label);
  view.stack.insertBefore(badge, view.bubble);
  scrollToLatest();
  return badge;
}

function beginTool(view, payload) {
  const name = typeof payload.tool === "string" ? payload.tool : "";
  const label = typeof payload.label === "string" && payload.label ? payload.label : "查询";
  const badge = createToolBadge(view);
  badge.querySelector(".tool-label").textContent = "正在" + label;
  view.tools.set(name || label, { badge, label });
}

function endTool(view, payload) {
  const name = typeof payload.tool === "string" ? payload.tool : "";
  const entry = view.tools.get(name) || Array.from(view.tools.values()).at(-1);
  if (!entry) return;
  view.tools.delete(name);
  const ok = payload.ok === true;
  const summary = typeof payload.summary === "string" ? payload.summary : "";
  entry.badge.classList.remove("is-running");
  entry.badge.classList.add(ok ? "is-ok" : "is-failed");
  entry.badge.querySelector(".tool-glyph").replaceChildren(svgIcon(ok ? "#icon-check" : "#icon-alert"));
  entry.badge.querySelector(".tool-label").textContent = ok ? "已" + entry.label : entry.label + "失败";
  entry.badge.title = summary;
  announcer.textContent = ok ? "已" + entry.label : entry.label + "失败，可以稍后重试。";
}

function interruptTools(view) {
  for (const { badge, label } of view.tools.values()) {
    badge.classList.remove("is-running");
    badge.classList.add("is-failed");
    badge.querySelector(".tool-glyph").replaceChildren(svgIcon("#icon-alert"));
    badge.querySelector(".tool-label").textContent = label + "未完成";
    badge.title = "回复中断，可以重新提问";
  }
  view.tools.clear();
}

function typewriter(node) {
  let pending = "";
  let timer = null;
  let resolveDrain = null;
  function settle() {
    if (resolveDrain) resolveDrain();
    resolveDrain = null;
  }
  function tick() {
    const characters = Array.from(pending);
    const count = reducedMotion.matches ? characters.length : Math.max(1, Math.ceil(characters.length / 90));
    node.appendData(characters.slice(0, count).join(""));
    pending = characters.slice(count).join("");
    scrollToLatest();
    if (pending) timer = window.setTimeout(tick, 22);
    else { timer = null; settle(); }
  }
  return {
    push(text) {
      pending += text;
      if (timer === null) tick();
    },
    drain() {
      return pending ? new Promise(resolve => { resolveDrain = resolve; }) : Promise.resolve();
    },
    flush() {
      window.clearTimeout(timer);
      timer = null;
      node.appendData(pending);
      pending = "";
      settle();
      scrollToLatest();
    },
  };
}

function readableError(payload) {
  const messagesByCode = {
    context_budget_exceeded: "这次的问题或对话太长了，请缩短问题，或开启新对话。",
    invalid_request: "问题太长或内容不完整，请修改后再发送。",
    upstream_timeout: "等候回复超时了，请稍后重新提问。",
    upstream_rate_limited: "客服暂时繁忙，请稍后重新提问。",
    invalid_model_output: "这次没有收到完整、有效的回复，请重新提问。",
    upstream_error: "客服暂时无法回复，请稍后重试；持续出现时请联系服务维护者。",
    database_unavailable: "会话存储暂时不可用，请稍后重新提问。",
  };
  return messagesByCode[payload?.code] || "暂时无法完成回复，请稍后重新提问。";
}

function renderAnswer(view, text, citations) {
  const sources = new Map((citations || []).map(item => [item.n, item]));
  view.article.classList.remove("is-writing");
  view.content.replaceChildren();
  const pattern = /\[(\d{1,2})\]/g;
  let lastIndex = 0;
  let match;
  while ((match = pattern.exec(text))) {
    if (match.index > lastIndex) view.content.append(document.createTextNode(text.slice(lastIndex, match.index)));
    const source = sources.get(Number(match[1]));
    if (source) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "citation";
      button.textContent = match[0];
      button.title = source.section_path || source.question || "查看来源";
      button.setAttribute("aria-label", "查看来源 " + match[1] + "：" + (source.section_path || source.question || "知识库条目"));
      button.addEventListener("click", () => openCitation(source, button));
      view.content.append(button);
    } else {
      view.content.append(document.createTextNode(match[0]));
    }
    lastIndex = pattern.lastIndex;
  }
  view.content.append(document.createTextNode(text.slice(lastIndex)));
}

let citationTrigger = null;
let citationHideTimer = null;
function openCitation(source, trigger) {
  citationTrigger = trigger || null;
  citationLayer.querySelector(".citation-path").textContent = source.section_path || "店铺知识库";
  citationLayer.querySelector(".citation-category").textContent = source.category ? "分类：" + source.category : "";
  citationLayer.querySelector(".citation-question").textContent = source.question || "（无来源问题）";
  citationLayer.querySelector(".citation-answer").textContent = source.answer || "（无原文）";
  window.clearTimeout(citationHideTimer);
  citationHideTimer = null;
  citationLayer.hidden = false;
  void citationLayer.offsetWidth;
  citationLayer.classList.add("is-open");
  citationPanel.focus();
  announcer.textContent = "已打开来源详情，按 Esc 关闭。";
}

function closeCitation() {
  if (citationLayer.hidden) return;
  citationLayer.classList.remove("is-open");
  const delay = reducedMotion.matches ? 0 : 220;
  citationHideTimer = window.setTimeout(() => { citationLayer.hidden = true; citationHideTimer = null; }, delay);
  if (citationTrigger) { citationTrigger.focus(); citationTrigger = null; }
}

function readFeedback(key) {
  try {
    const value = localStorage.getItem(key);
    return value === "up" || value === "down" ? value : null;
  } catch {
    return null;
  }
}

function saveFeedback(key, value) {
  try {
    localStorage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

function systemNote(text) {
  const note = document.createElement("p");
  note.className = "system-note";
  note.textContent = text;
  const gap = document.createElement("div");
  gap.className = "message-gap";
  messages.append(note, gap);
  scrollToLatest();
}

async function createTicket(turn, button, note) {
  button.disabled = true;
  try {
    const response = await fetch("/api/tickets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conversation_id: conversationId, client_id: clientId(), turn }),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok || !payload?.ticket_no) throw new Error(payload?.code || "ticket_failed");
    note.textContent = "工单 " + payload.ticket_no + " 已创建，客服会尽快跟进。";
    announcer.textContent = "工单已创建。";
  } catch {
    button.disabled = false;
    note.textContent = "工单暂时没建成，请稍后再试。";
  }
}

function addActionButtons(view, items, turn) {
  if (!items.length) return;
  const row = document.createElement("div");
  row.className = "action-row";
  const note = document.createElement("p");
  note.className = "action-note";
  for (const item of items) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "action-button";
    button.textContent = item.label;
    if (item.id === "human") {
      button.addEventListener("click", () => {
        button.disabled = true;
        systemNote("已转接人工客服");
        messageElement("assistant", "您好，我是客服小猫，请问有什么可以帮您的？", "客服小猫");
        announcer.textContent = "已转接人工客服，客服小猫已接入。";
      });
    } else if (item.id === "ticket") {
      button.addEventListener("click", () => createTicket(turn, button, note));
    } else {
      button.hidden = true;
    }
    row.append(button);
  }
  row.append(note);
  view.stack.append(row);
  scrollToLatest();
}

function addAnswerFeedback(view, turnId) {
  const row = document.createElement("div");
  row.className = "answer-feedback";
  const note = document.createElement("span");
  note.className = "feedback-note";
  const buttons = {};
  const key = FEEDBACK_PREFIX + turnId;
  const lock = value => {
    for (const [name, button] of Object.entries(buttons)) {
      button.classList.toggle("is-selected", name === value);
      button.setAttribute("aria-pressed", name === value ? "true" : "false");
      button.disabled = true;
    }
    note.textContent = "已反馈";
  };
  for (const [value, label, icon] of [["up", "有帮助", "#icon-thumb-up"], ["down", "没帮助", "#icon-thumb-down"]]) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "feedback-button";
    button.title = label;
    button.setAttribute("aria-label", label);
    button.setAttribute("aria-pressed", "false");
    button.append(svgIcon(icon));
    button.addEventListener("click", () => {
      if (!saveFeedback(key, value)) return;
      lock(value);
      announcer.textContent = "已记录你的反馈，谢谢。";
    });
    buttons[value] = button;
    row.append(button);
  }
  row.append(note);
  const saved = readFeedback(key);
  if (saved) lock(saved);
  view.stack.append(row);
}

async function readStream(response, handlers) {
  if (!response.ok) {
    let payload;
    try { payload = await response.json(); } catch { payload = null; }
    throw new Error(readableError(payload));
  }
  if (!response.body || !response.headers.get("content-type")?.includes("text/event-stream")) {
    throw new Error("没有收到有效回复，请重新提问。");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let separator;
      while ((separator = /\r\n\r\n|\n\n|\r\r/.exec(buffer))) {
        const frame = buffer.slice(0, separator.index);
        buffer = buffer.slice(separator.index + separator[0].length);
        let event = "message";
        const data = [];
        for (const line of frame.split(/\r\n|\r|\n/)) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
        }
        if (!data.length) continue;
        let payload;
        try { payload = JSON.parse(data.join("\n")); }
        catch { throw new Error("收到的回复不完整，请重新提问。"); }
        if (event === "error") throw new Error(readableError(payload));
        if (event === "meta") {
          if (Number.isInteger(payload?.conversation_id) && payload.conversation_id > 0) handlers.onMeta(payload.conversation_id);
        }
        if (event === "status") {
          if (payload && (payload.phase === "start" || payload.phase === "end")) handlers.onStatus(payload);
        }
        if (event === "citations") {
          if (Array.isArray(payload?.items)) handlers.onCitations(payload.items);
        }
        if (event === "actions") {
          if (Array.isArray(payload?.items) && Number.isInteger(payload?.turn)) {
            handlers.onActions(payload.items, payload.turn);
          }
        }
        if (event === "token") {
          if (typeof payload?.content !== "string") throw new Error("收到的回复格式不正确，请重新提问。");
          handlers.onText(payload.content);
        }
        if (event === "done") {
          if (!["stop", "length"].includes(payload?.finish_reason)) throw new Error("回复没有完整结束，请重新提问。");
          return payload.finish_reason;
        }
      }
      if (done) throw new Error("连接中断，回复没有完整结束。请重新提问。");
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

function showFeedback(view, message, question, stopped = false) {
  view.article.classList.toggle("message-stopped", stopped);
  const feedback = document.createElement("div");
  feedback.className = "message-feedback";
  const description = document.createElement("p");
  description.textContent = message;
  const retry = document.createElement("button");
  retry.type = "button";
  retry.textContent = "重新提问";
  retry.addEventListener("click", () => {
    if (activeTurn) return;
    input.value = question;
    updateInput();
    input.focus();
  });
  feedback.append(description, retry);
  view.stack.append(feedback);
  announcer.textContent = message;
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  const question = input.value.trim();
  if (!question || activeTurn) return;
  const suffix = Date.now() + "-" + Math.random().toString(36).slice(2, 8);
  const turn = { controller: new AbortController(), stopped: false, timedOut: false,
    turnId: "local-" + suffix, citations: [], actionItems: [], actionTurn: 0 };
  activeTurn = turn;
  input.value = "";
  welcome.hidden = true;
  document.querySelector("#conversation-date").hidden = false;
  followBottom = true;
  messageElement("user", question);
  for (const stale of messages.querySelectorAll(".action-button:not(:disabled)")) stale.disabled = true;
  const view = messageElement("assistant");
  view.article.classList.add("is-writing");
  const waiting = document.createElement("span");
  waiting.className = "waiting";
  const waitingText = document.createElement("span");
  waitingText.textContent = "正在整理回复";
  waiting.append(waitingText, ...Array.from({ length: 3 }, () => document.createElement("i")));
  view.bubble.append(waiting);
  const writer = typewriter(view.textNode);
  turn.writer = writer;
  let answer = "";
  const timeout = window.setTimeout(() => {
    turn.timedOut = true;
    turn.controller.abort();
  }, 90000);
  setBusy(true);
  scrollToLatest(true);
  announcer.textContent = "问题已发送，正在等待回复。";
  try {
    const body = { message: question, history: history.slice(-100), client_id: clientId() };
    if (conversationId) body.conversation_id = conversationId;
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: turn.controller.signal,
    });
    const finishReason = await readStream(response, {
      onMeta: id => { conversationId = id; turn.turnId = id + "-" + suffix; },
      onStatus: payload => {
        waiting.remove();
        if (payload.phase === "start") beginTool(view, payload);
        else endTool(view, payload);
      },
      onCitations: items => {
        waiting.remove();
        turn.citations = items;
      },
      onActions: (items, actionTurn) => { turn.actionItems = items; turn.actionTurn = actionTurn; },
      onText: text => {
        if (!text) return;
        waiting.remove();
        answer += text;
        writer.push(text);
      },
    });
    window.clearTimeout(timeout);
    await writer.drain();
    if (turn.stopped) throw new Error("stopped");
    if (!answer.trim()) throw new Error("这次没有收到正文，请重新提问。");
    if (finishReason === "length") throw new Error("回复因长度限制而中断，请缩短问题后重新提问。");
    if (Array.from(answer.trim()).length > 12000) throw new Error("回复过长，无法继续接续这一轮。请开启新对话。");
    renderAnswer(view, answer, turn.citations);
    if (turn.actionItems.length) addActionButtons(view, turn.actionItems, turn.actionTurn);
    addAnswerFeedback(view, turn.turnId);
    history.push({ role: "user", content: question }, { role: "assistant", content: answer.trim() });
    history = history.slice(-100);
    announcer.textContent = "小邻已回复，你可以继续追问。";
  } catch (error) {
    writer.flush();
    interruptTools(view);
    if (!answer) view.textNode.data = turn.stopped ? "已停止回复。" : "这次暂时没能回复你。";
    const message = turn.stopped ? "这条回复未完成，可以重新提问。" : turn.timedOut ? "等候回复超时了，请重新提问。" : error instanceof TypeError ? "连接失败，请检查网络或确认客服服务已启动。" : error.message;
    showFeedback(view, message, question, turn.stopped);
  } finally {
    window.clearTimeout(timeout);
    waiting.remove();
    view.article.classList.remove("is-writing");
    activeTurn = null;
    setBusy(false);
    scrollToLatest();
  }
});

input.addEventListener("input", updateInput);
input.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    if (!sendButton.disabled) form.requestSubmit();
  }
});
stopButton.addEventListener("click", () => {
  if (!activeTurn) return;
  activeTurn.stopped = true;
  activeTurn.controller.abort();
  activeTurn.writer.flush();
});
newChatButton.addEventListener("click", () => {
  if (activeTurn) return;
  history = [];
  conversationId = null;
  messages.replaceChildren();
  welcome.hidden = false;
  document.querySelector("#conversation-date").hidden = true;
  jumpButton.hidden = true;
  followBottom = true;
  scrollArea.scrollTop = 0;
  announcer.textContent = "新对话已开始。";
  input.focus();
});
document.querySelectorAll("[data-prompt]").forEach(button => {
  button.addEventListener("click", () => {
    input.value = button.dataset.prompt;
    updateInput();
    input.focus();
  });
});
scrollArea.addEventListener("scroll", () => {
  followBottom = scrollArea.scrollHeight - scrollArea.clientHeight - scrollArea.scrollTop < 80;
  jumpButton.hidden = followBottom || !messages.children.length;
}, { passive: true });
window.addEventListener("resize", () => scrollToLatest());
jumpButton.addEventListener("click", () => { followBottom = true; scrollToLatest(true); });
citationLayer.querySelector(".citation-close").addEventListener("click", closeCitation);
citationLayer.querySelector(".citation-scrim").addEventListener("click", closeCitation);
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && !citationLayer.hidden) closeCitation();
});
updateInput();
