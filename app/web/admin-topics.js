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
