/* 研栖 ResearchNest 前端：无构建工具，原生 JS + fetch。 */

const state = {
  route: "overview",
  health: null,
  papers: [],
  memories: [],
  answer: null,
  weekly: null,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

/* ------------------------------------------------------------------ 基础 */
async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await response.text();
  let payload = {};
  try {
    payload = text ? JSON.parse(text) : {};
  } catch {
    payload = { raw: text };
  }
  if (!response.ok) {
    const error = payload.error || {};
    throw new Error(error.message || `HTTP ${response.status}`);
  }
  return payload;
}

function toast(message, type = "ok") {
  const node = document.createElement("div");
  node.className = `toast ${type === "error" ? "error" : ""}`;
  node.textContent = message;
  $("#toast").appendChild(node);
  setTimeout(() => node.remove(), 4200);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function inline(text) {
  return text
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[(\d+)\]/g, '<span class="cite">[$1]</span>');
}

function renderMarkdown(source) {
  const lines = escapeHtml(source || "").split("\n");
  let html = "";
  let para = [];
  let list = null;
  const flushPara = () => {
    if (para.length) {
      html += `<p>${inline(para.join(" "))}</p>`;
      para = [];
    }
  };
  const flushList = () => {
    if (list) {
      html += `<${list.tag}>${list.items.map((item) => `<li>${inline(item)}</li>`).join("")}</${list.tag}>`;
      list = null;
    }
  };
  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) {
      flushPara();
      flushList();
      continue;
    }
    let match;
    if ((match = line.match(/^(#{1,4})\s+(.*)$/))) {
      flushPara();
      flushList();
      const level = Math.min(match[1].length + 1, 4);
      html += `<h${level}>${inline(match[2])}</h${level}>`;
      continue;
    }
    if ((match = line.match(/^&gt;\s?(.*)$/))) {
      flushPara();
      flushList();
      html += `<blockquote>${inline(match[1])}</blockquote>`;
      continue;
    }
    if ((match = line.match(/^[-*]\s+(.*)$/))) {
      flushPara();
      if (!list || list.tag !== "ul") {
        flushList();
        list = { tag: "ul", items: [] };
      }
      list.items.push(match[1]);
      continue;
    }
    if ((match = line.match(/^\d+\.\s+(.*)$/))) {
      flushPara();
      if (!list || list.tag !== "ol") {
        flushList();
        list = { tag: "ol", items: [] };
      }
      list.items.push(match[1]);
      continue;
    }
    para.push(line.trim());
  }
  flushPara();
  flushList();
  return html;
}

function countLabel(value, unit) {
  return `${Number(value || 0)}${unit}`;
}

function kindLabel(kind) {
  const map = {
    profile: "画像",
    preference: "偏好",
    constraint: "约束",
    decision: "决策",
    status: "进展",
    fact: "事实",
    note: "笔记",
  };
  return map[kind] || kind || "事实";
}

/* ------------------------------------------------------------------ 布局 */
function setTitle(title, subtitle) {
  $("#page-title").innerHTML = `${escapeHtml(title)}<small>${escapeHtml(subtitle)}</small>`;
}

function renderStatus() {
  const health = state.health;
  if (!health) return;
  const pills = [];
  pills.push(
    `<span class="pill ${health.seekdb?.ok ? "ok" : "bad"}"><span class="dot"></span>seekdb ${health.seekdb?.ok ? "已连接" : "未连接"}</span>`
  );
  pills.push(
    `<span class="pill ${health.powercontext?.ok ? "ok" : "bad"}"><span class="dot"></span>PowerContext ${health.powercontext?.ok ? "已连接" : "未连接"}</span>`
  );
  if (health.demo) pills.push(`<span class="pill demo"><span class="dot"></span>演示模式</span>`);
  if (health.llm?.enabled) pills.push(`<span class="pill"><span class="dot"></span>LLM ${health.llm.model}</span>`);
  $("#status-pills").innerHTML = pills.join("");
  $("#scope-mini").textContent = health.scope_id ? `scope: ${health.scope_id}` : "";

  const banner = $("#demo-banner");
  if (health.demo) {
    banner.innerHTML =
      '<div class="notice warn"><b>演示模式</b>：当前使用内存数据，便于先体验界面；真实检索与记忆请按 README 启动 seekdb 与 PowerContext 后以 live 模式运行。</div>';
  } else if (!health.seekdb?.ok) {
    banner.innerHTML = `<div class="notice error"><b>seekdb 未连接</b>：${escapeHtml(
      state.health.warnings?.slice(-1)[0] || "请检查 .env 与数据库状态。"
    )}</div>`;
  } else {
    banner.innerHTML = "";
  }
}

async function refreshHealth() {
  try {
    state.health = await api("/api/health");
    renderStatus();
  } catch (error) {
    toast(`健康检查失败：${error.message}`, "error");
  }
}

/* ------------------------------------------------------------------ 导航 */
function bindNav() {
  $$("#nav button").forEach((button) => {
    button.addEventListener("click", () => {
      location.hash = `#/${button.dataset.route}`;
    });
  });
}

function currentRoute() {
  const raw = location.hash.replace(/^#\/?/, "").trim();
  const name = raw.split("?")[0];
  return ["overview", "library", "ask", "memory", "weekly", "verify"].includes(name)
    ? name
    : "overview";
}

function queryParams() {
  const raw = location.hash.replace(/^#\/?/, "");
  const index = raw.indexOf("?");
  return new URLSearchParams(index >= 0 ? raw.slice(index + 1) : "");
}

function markNav(route) {
  $$("#nav button").forEach((button) => {
    button.classList.toggle("active", button.dataset.route === route);
  });
}

async function route() {
  const routeName = currentRoute();
  state.route = routeName;
  markNav(routeName);
  if (!state.health) await refreshHealth();
  $("#view").innerHTML = '<div class="skeleton">正在加载…</div>';
  try {
    if (routeName === "overview") await viewOverview();
    if (routeName === "library") await viewLibrary();
    if (routeName === "ask") await viewAsk();
    if (routeName === "memory") await viewMemory();
    if (routeName === "weekly") await viewWeekly();
    if (routeName === "verify") await viewVerify();
  } catch (error) {
    $("#view").innerHTML = `<div class="notice error">加载失败：${escapeHtml(error.message)}</div>`;
  }
}

/* ------------------------------------------------------------------ 概览 */
async function viewOverview() {
  setTitle("概览", "文献找得到，上下文丢不了");
  const [papers, stats, logs] = await Promise.all([
    api("/api/papers").catch(() => ({ papers: [] })),
    api("/api/stats").catch(() => ({})),
    api("/api/logs?limit=8").catch(() => ({ retrieval: [], powercontext: [] })),
  ]);
  state.papers = papers.papers || [];
  const memoryCount = state.health?.powercontext?.ok
    ? (state.memories.length || (await api("/api/memories").then((r) => r.memories.length).catch(() => 0)))
    : 0;

  const recent = (logs.retrieval || []).slice(0, 4);
  const recentHtml = recent.length
    ? recent
        .map(
          (log) => `<div class="list-item"><div class="main">
            <div class="title">${escapeHtml(log.query)}</div>
            <div class="meta">${escapeHtml(log.created_at)} · ${escapeHtml(log.mode)} ·
              候选 ${log.vector_ids.length + log.fts_ids.length} · ${log.latency_ms}ms</div>
          </div><span class="tag teal">${escapeHtml(String(log.fused_ids.length))} 条融合</span></div>`
        )
        .join("")
    : '<div class="empty"><b>还没有检索记录</b>导入示例文献后，去「检索问答」问一个问题。</div>';

  $("#view").innerHTML = `
    <section class="hero">
      <h1>把读过的论文，变成随时能调用的上下文</h1>
      <p>研栖面向研究生与科研工作者的真实一周：文献越读越散、组会周报总要重拼、换个会话就要重新解释课题。
      它用 <b>seekdb</b> 存文献并做「向量 + 全文 + 结构化过滤」混合检索，用 <b>PowerContext</b> 记住你的方向、偏好、约束与进展；
      每次回答和周报都带得出出处，也带得上你的上下文。</p>
      <div class="hero-actions">
        <button class="btn" data-action="import-samples">一键导入示例文献</button>
        <button class="btn ghost" data-action="go-ask">去检索问答</button>
        <button class="btn ghost" data-action="go-weekly">生成组会周报</button>
      </div>
    </section>

    <div class="grid stats" style="margin-top:16px">
      <div class="card stat"><div class="value">${countLabel(stats.papers ?? state.papers.length, "")}</div><div class="label">文献 / 笔记</div></div>
      <div class="card stat"><div class="value">${countLabel(stats.chunks, "")}</div><div class="label">可检索切片</div></div>
      <div class="card stat"><div class="value">${countLabel(stats.searches, "")}</div><div class="label">混合检索次数</div></div>
      <div class="card stat"><div class="value">${countLabel(memoryCount, "")}</div><div class="label">PowerContext 记忆</div></div>
    </div>

    <div class="grid two" style="margin-top:16px">
      <div class="card">
        <div class="card-head"><div><div class="card-title">最近检索</div><div class="card-sub">每次检索都会写入 seekdb 的 retrieval_logs，可在「技术核验」核对</div></div>
          <button class="btn small" data-action="go-verify">查看日志</button></div>
        <div class="list">${recentHtml}</div>
      </div>
      <div class="card">
        <div class="card-head"><div class="card-title">工作流</div></div>
        <div class="flow">
          <div class="step"><div class="num">01</div><b>导入文献</b><span>Markdown / 文本切片后写入 seekdb，同时建立向量与全文索引。</span></div>
          <div class="step"><div class="num">02</div><b>混合检索</b><span>两路召回 + RRF 融合 + 年份/会议/标签过滤，回答带出处。</span></div>
          <div class="step"><div class="num">03</div><b>记忆复用</b><span>方向、偏好、约束、进展存入 PowerContext，下次自动带上。</span></div>
        </div>
        <div class="notice info" style="margin-top:12px">
          技术分工：<b>seekdb</b> 负责文献、切片、检索日志与行为数据；<b>PowerContext</b> 负责跨会话的研究记忆与有界上下文。
        </div>
      </div>
    </div>`;

  bindOverview();
}

function bindOverview() {
  $$('[data-action="import-samples"]').forEach((button) =>
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const result = await api("/api/samples/import", { method: "POST", body: "{}" });
        toast(`已导入 ${result.count} 篇示例材料`);
        await route();
      } catch (error) {
        toast(error.message, "error");
        button.disabled = false;
      }
    })
  );
  $$('[data-action="go-ask"]').forEach((button) =>
    button.addEventListener("click", () => (location.hash = "#/ask"))
  );
  $$('[data-action="go-weekly"]').forEach((button) =>
    button.addEventListener("click", () => (location.hash = "#/weekly"))
  );
  $$('[data-action="go-verify"]').forEach((button) =>
    button.addEventListener("click", () => (location.hash = "#/verify"))
  );
}

/* ------------------------------------------------------------------ 文献库 */
async function viewLibrary() {
  setTitle("文献库", "论文、课程笔记、组会记录统一进 seekdb");
  const { papers } = await api("/api/papers");
  state.papers = papers || [];
  const cards = state.papers.length
    ? state.papers
        .map(
          (paper) => `
        <div class="card paper-card">
          <h3>${escapeHtml(paper.title)}</h3>
          <div class="paper-meta">
            <span>${paper.year ? paper.year + " 年" : "年份未知"}</span>
            <span>·</span><span>${escapeHtml(paper.venue || "未填写来源")}</span>
            ${paper.authors ? `<span>·</span><span>${escapeHtml(paper.authors)}</span>` : ""}
          </div>
          <div class="tags">
            ${(paper.tags || "")
              .split(",")
              .filter(Boolean)
              .map((tag) => `<span class="tag teal">${escapeHtml(tag)}</span>`)
              .join("")}
          </div>
          <div class="foot">
            <span class="tag mono">${paper.chunk_count} chunks</span>
            <span>
              <button class="btn small" data-action="view-paper" data-id="${paper.paper_id}">查看切片</button>
              <button class="btn small danger" data-action="delete-paper" data-id="${paper.paper_id}">删除</button>
            </span>
          </div>
        </div>`
        )
        .join("")
    : '<div class="empty" style="grid-column:1/-1"><b>文献库还是空的</b>点击右上角「导入示例文献」快速体验，或粘贴自己的笔记。</div>';

  $("#view").innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <div class="card-head">
        <div><div class="card-title">文献与笔记</div><div class="card-sub">切片后写入 seekdb；每条切片都带年份、来源与标签，可做结构化过滤</div></div>
        <div>
          <button class="btn" data-action="open-import">粘贴导入</button>
          <button class="btn primary" data-action="import-samples">导入示例文献</button>
        </div>
      </div>
      <div class="notice info">
        导入的 Markdown 支持文件头 <code>title / authors / year / venue / tags</code>；没有也可以，系统会从一级标题推断。
      </div>
    </div>
    <div class="grid cards">${cards}</div>`;

  bindLibrary();
}

function bindLibrary() {
  $$('[data-action="import-samples"]').forEach((button) =>
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const result = await api("/api/samples/import", { method: "POST", body: "{}" });
        toast(`已导入 ${result.count} 篇示例材料`);
        await route();
      } catch (error) {
        toast(error.message, "error");
        button.disabled = false;
      }
    })
  );
  $$('[data-action="open-import"]').forEach((button) =>
    button.addEventListener("click", () => openImportModal())
  );
  $$('[data-action="delete-paper"]').forEach((button) =>
    button.addEventListener("click", async () => {
      if (!confirm("确认删除这篇文献及其切片？")) return;
      try {
        await api(`/api/papers/${button.dataset.id}`, { method: "DELETE" });
        toast("已删除");
        await route();
      } catch (error) {
        toast(error.message, "error");
      }
    })
  );
  $$('[data-action="view-paper"]').forEach((button) =>
    button.addEventListener("click", () => showPaper(button.dataset.id))
  );
}

async function showPaper(paperId) {
  try {
    const paper = await api(`/api/papers/${paperId}`);
    const chunks = (paper.chunks || [])
      .map(
        (chunk) => `<div class="evidence-item">
          <div class="head"><span class="source">${escapeHtml(chunk.section || "正文")} · 片段 ${chunk.chunk_index + 1}</span>
          <span class="tag mono">${escapeHtml(chunk.chunk_id)}</span></div>
          <div class="body">${escapeHtml(chunk.content)}</div>
        </div>`
      )
      .join("");
    openModal(`
      <h2>${escapeHtml(paper.title)}</h2>
      <div class="paper-meta" style="margin-bottom:12px">
        <span>${paper.year || "年份未知"}</span><span>·</span><span>${escapeHtml(paper.venue || "未填写来源")}</span>
        <span>·</span><span>${escapeHtml(paper.source_name || "")}</span>
      </div>
      <div class="evidence">${chunks}</div>
      <div class="modal-actions"><button class="btn" data-action="close-modal">关闭</button></div>`);
  } catch (error) {
    toast(error.message, "error");
  }
}

function openImportModal() {
  openModal(`
    <h2>导入文献 / 笔记</h2>
    <p class="card-sub" style="margin-bottom:12px">支持 .md / .txt；文件头可以写 title、authors、year、venue、tags。</p>
    <div class="field">
      <label>选择文件</label>
      <input type="file" id="import-file" accept=".md,.txt,.markdown" />
    </div>
    <div class="field">
      <label>文件名</label>
      <input type="text" id="import-name" placeholder="例如：2026-组会记录.md" />
    </div>
    <div class="field">
      <label>内容</label>
      <textarea id="import-content" rows="12" placeholder="也可以直接粘贴 Markdown 或纯文本…"></textarea>
    </div>
    <div class="modal-actions">
      <button class="btn" data-action="close-modal">取消</button>
      <button class="btn primary" id="import-submit">导入</button>
    </div>`);
  $("#import-file").addEventListener("change", (event) => {
    const file = event.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      $("#import-content").value = reader.result;
      if (!$("#import-name").value) $("#import-name").value = file.name;
    };
    reader.readAsText(file, "utf-8");
  });
  $("#import-submit").addEventListener("click", async () => {
    const name = $("#import-name").value.trim() || "未命名.md";
    const content = $("#import-content").value;
    if (!content.trim()) {
      toast("内容不能为空", "error");
      return;
    }
    try {
      const result = await api("/api/papers", {
        method: "POST",
        body: JSON.stringify({ name, content }),
      });
      closeModal();
      toast(`已导入《${result.title}》，${result.chunks} 个切片`);
      await route();
    } catch (error) {
      toast(error.message, "error");
    }
  });
}

/* ------------------------------------------------------------------ 问答 */
async function viewAsk() {
  setTitle("检索问答", "向量 + 全文 + 结构化过滤，回答带出处");
  const previous = state.answer;
  $("#view").innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <div class="searchbar">
        <input type="text" id="question" placeholder="例如：混合检索里 RRF 的 k 值怎么选？有实验数据吗？" />
        <button class="btn primary" id="ask-button">检索并回答</button>
      </div>
      <div class="filters" style="margin-top:12px">
        <div class="field"><label>起始年份</label><input type="number" id="year-from" placeholder="2020" /></div>
        <div class="field"><label>结束年份</label><input type="number" id="year-to" placeholder="2026" /></div>
        <div class="field"><label>会议 / 来源</label><input type="text" id="venue" placeholder="示例会议" /></div>
        <div class="field"><label>标签</label><input type="text" id="tag" placeholder="混合检索" /></div>
      </div>
      <div class="card-sub" style="margin-top:10px">检索会同时走 seekdb 的向量近邻与全文匹配，再用 RRF 融合；过滤条件下推到 SQL。</div>
    </div>
    <div id="ask-result">${previous ? answerHtml(previous) : emptyAnswerHtml()}</div>`;

  $("#ask-button").addEventListener("click", runAsk);
  $("#question").addEventListener("keydown", (event) => {
    if (event.key === "Enter") runAsk();
  });
  if (previous) bindAnswer();

  const params = queryParams();
  const preset = params.get("q");
  if (preset) {
    $("#question").value = preset;
    runAsk();
  }
}

function emptyAnswerHtml() {
  return `<div class="card"><div class="empty"><b>问一个你真正关心的问题</b>
    比如「RRF 的 k 值怎么选」「Agent 记忆为什么要分三层」「本周实验的延迟数据」。
    回答里的 [1] [2] 对应下方证据，点开源文可以核对。</div></div>`;
}

async function runAsk() {
  const question = $("#question").value.trim();
  if (!question) {
    toast("先输入一个问题", "error");
    return;
  }
  const years = (value) => (value ? Number(value) : null);
  const filters = {
    year_from: years($("#year-from").value),
    year_to: years($("#year-to").value),
    venue: $("#venue").value.trim(),
    tag: $("#tag").value.trim(),
  };
  const button = $("#ask-button");
  button.disabled = true;
  button.textContent = "检索中…";
  try {
    const result = await api("/api/ask", {
      method: "POST",
      body: JSON.stringify({ question, filters, top_k: 6 }),
    });
    state.answer = result;
    $("#ask-result").innerHTML = answerHtml(result);
    bindAnswer();
  } catch (error) {
    $("#ask-result").innerHTML = `<div class="notice error">${escapeHtml(error.message)}</div>`;
  } finally {
    button.disabled = false;
    button.textContent = "检索并回答";
  }
}

function answerHtml(result) {
  const citations = (result.citations || [])
    .map(
      (citation) =>
        `<span class="tag mono" title="${escapeHtml(citation.chunk_id)}">[${citation.index}] ${escapeHtml(
          citation.title
        )}${citation.section ? " · " + escapeHtml(citation.section) : ""} · 片段 ${citation.chunk_index + 1}</span>`
    )
    .join("");

  const evidence = (result.search?.hits || [])
    .map(
      (hit, index) => `<div class="evidence-item">
        <div class="head">
          <span class="source">[${index + 1}] ${escapeHtml(hit.title)}${
        hit.section ? " · " + escapeHtml(hit.section) : ""
      } · 片段 ${hit.chunk_index + 1}</span>
          <span class="tag mono">${escapeHtml(hit.chunk_id)}</span>
        </div>
        <div class="body">${escapeHtml(hit.content)}</div>
        <div class="chips">
          ${(hit.matched_by || [])
            .map((channel) => `<span class="tag ${channel === "vector" ? "teal" : "amber"}">${channel}</span>`)
            .join("")}
          <span class="tag mono">RRF ${hit.rrf_score}</span>
          ${hit.distance != null ? `<span class="tag mono">L2 ${hit.distance}</span>` : ""}
        </div>
      </div>`
    )
    .join("");

  const memories = (result.memories || [])
    .map(
      (memory) => `<div class="memory-chip"><span class="kind">记忆</span><span>${escapeHtml(memory.text)}</span></div>`
    )
    .join("");

  const search = result.search || {};
  const notices = [];
  if (result.memory_warning) notices.push(`<div class="notice warn">${escapeHtml(result.memory_warning)}</div>`);
  if (result.warning) notices.push(`<div class="notice warn">${escapeHtml(result.warning)}</div>`);
  if (search.warning) notices.push(`<div class="notice warn">${escapeHtml(search.warning)}</div>`);

  return `<div class="grid two">
    <div class="card">
      <div class="card-head">
        <div><div class="card-title">回答</div>
          <div class="card-sub">${escapeHtml(result.engine)} · ${result.elapsed_ms}ms ·
          向量候选 ${search.vector_candidates ?? 0} · 全文候选 ${search.fts_candidates ?? 0}</div></div>
        <span class="tag teal">seekdb 检索</span>
      </div>
      ${notices.join("")}
      <div class="answer">${renderMarkdown(result.answer || "")}</div>
      <div class="tags" style="margin-top:12px">${citations}</div>
      ${evidence ? `<div class="card-head" style="margin-top:18px"><div class="card-title">证据原文</div></div><div class="evidence">${evidence}</div>` : ""}
    </div>
    <div>
      <div class="card">
        <div class="card-head"><div><div class="card-title">本轮用到的记忆</div>
          <div class="card-sub">来自 PowerContext，跨会话保存${
            result.memory_source === "standing" ? "（本次提问未命中，已按常驻主题召回）" : ""
          }</div></div></div>
        <div class="list">${memories || '<div class="empty">没有命中的记忆。去「研究记忆」写入方向、偏好或进展。</div>'}</div>
      </div>
      <div class="card" style="margin-top:16px">
        <div class="card-head"><div class="card-title">检索通道</div></div>
        <dl class="kv">
          <dt>查询</dt><dd>${escapeHtml(search.query || "")}</dd>
          <dt>向量候选</dt><dd>${search.vector_candidates ?? 0}</dd>
          <dt>全文候选</dt><dd>${search.fts_candidates ?? 0}</dd>
          <dt>融合耗时</dt><dd>${search.latency_ms ?? 0} ms</dd>
          <dt>过滤条件</dt><dd>${escapeHtml(JSON.stringify(result.filters || {}))}</dd>
        </dl>
      </div>
    </div>
  </div>`;
}

function bindAnswer() {
  $$('[data-action="close-modal"]').forEach((button) =>
    button.addEventListener("click", closeModal)
  );
}

/* ------------------------------------------------------------------ 记忆 */
async function viewMemory() {
  setTitle("研究记忆", "保存在 PowerContext，下次打开仍然有效");
  const { memories } = await api("/api/memories");
  state.memories = memories || [];
  const items = state.memories.length
    ? state.memories
        .map(
          (memory) => `<div class="list-item">
            <div class="main">
              <div class="title">${escapeHtml(memory.text)}</div>
              <div class="meta">${kindLabel(memory.kind)} · v${memory.version} ·
                ${escapeHtml(memory.entry_id)} · ${memory.state === "active" ? "生效中" : "已停用"}</div>
            </div>
            <button class="btn small danger" data-action="retire-memory" data-id="${escapeHtml(
              memory.entry_id
            )}">停用</button>
          </div>`
        )
        .join("")
    : '<div class="empty"><b>还没有记忆</b>先写入一条研究方向或表达偏好。</div>';

  $("#view").innerHTML = `
    <div class="grid two">
      <div class="card">
        <div class="card-head"><div><div class="card-title">记忆列表</div>
          <div class="card-sub">${state.memories.length} 条生效中；停用不会删除历史版本</div></div>
          <button class="btn small" data-action="refresh-memory">刷新</button></div>
        <div class="list">${items}</div>
      </div>
      <div>
        <div class="card">
          <div class="card-head"><div><div class="card-title">写入一条记忆</div>
            <div class="card-sub">越具体，召回越准</div></div></div>
          <div class="field"><label>类型</label>
            <select id="memory-kind">
              <option value="profile">画像 / 研究方向</option>
              <option value="preference">偏好</option>
              <option value="constraint">约束</option>
              <option value="decision">决策</option>
              <option value="status">进展 / 计划</option>
              <option value="fact">事实</option>
            </select>
          </div>
          <div class="field"><label>内容</label>
            <textarea id="memory-text" rows="4" placeholder="例如：实验只能使用单卡 24GB，向量索引优先 HNSW。"></textarea>
          </div>
          <div class="field"><label>原因（可选）</label>
            <input type="text" id="memory-reason" placeholder="为什么值得记住" />
          </div>
          <button class="btn primary" id="memory-save">保存到 PowerContext</button>
        </div>
        <div class="card" style="margin-top:16px">
          <div class="card-head"><div class="card-title">记忆如何被复用</div></div>
          <div class="card-sub" style="line-height:1.9">
            每次「检索问答」与「组会周报」都会先检索记忆并准备有界上下文，再把命中的记忆叠加到回答里。
            页面「技术核验」保留了每次 remember / search / prepare 的调用记录，可核对记忆确实被读写。
          </div>
        </div>
      </div>
    </div>`;

  $("#memory-save").addEventListener("click", async () => {
    const text = $("#memory-text").value.trim();
    if (!text) {
      toast("记忆内容不能为空", "error");
      return;
    }
    try {
      await api("/api/memories", {
        method: "POST",
        body: JSON.stringify({
          text,
          kind: $("#memory-kind").value,
          reason: $("#memory-reason").value.trim(),
        }),
      });
      toast("已写入研究记忆");
      await route();
    } catch (error) {
      toast(error.message, "error");
    }
  });
  $$('[data-action="refresh-memory"]').forEach((button) =>
    button.addEventListener("click", () => route())
  );
  $$('[data-action="retire-memory"]').forEach((button) =>
    button.addEventListener("click", async () => {
      const memory = state.memories.find((item) => item.entry_id === button.dataset.id);
      if (!memory) return;
      if (!confirm("停用后不再参与召回，历史版本仍保留。继续？")) return;
      try {
        await api("/api/memories/retire", {
          method: "POST",
          body: JSON.stringify({ citation: memory.citation, reason: "用户在页面手动停用" }),
        });
        toast("已停用");
        await route();
      } catch (error) {
        toast(error.message, "error");
      }
    })
  );
}

/* ------------------------------------------------------------------ 周报 */
async function viewWeekly() {
  setTitle("组会周报", "从本周检索与记忆自动生成初稿");
  const existing = state.weekly;
  $("#view").innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <div class="card-head">
        <div><div class="card-title">生成周报</div>
          <div class="card-sub">汇总本周问答/阅读行为、seekdb 检索证据与 PowerContext 记忆</div></div>
        <div style="display:flex;gap:8px;align-items:end">
          <div class="field" style="margin:0"><label>天数</label>
            <select id="weekly-days"><option>7</option><option>14</option><option>30</option></select></div>
          <button class="btn primary" id="weekly-button">生成</button>
        </div>
      </div>
      <div class="field" style="margin:0"><label>聚焦主题（可选）</label>
        <input type="text" id="weekly-focus" placeholder="例如：RRF 参数消融" /></div>
    </div>
    <div id="weekly-result">${existing ? weeklyHtml(existing) : '<div class="card"><div class="empty"><b>还没有生成周报</b>选择时间范围后点击「生成」。</div></div>'}</div>`;

  $("#weekly-button").addEventListener("click", async () => {
    const days = Number($("#weekly-days").value || 7);
    const focus = $("#weekly-focus").value.trim();
    const button = $("#weekly-button");
    button.disabled = true;
    button.textContent = "生成中…";
    try {
      const result = await api(`/api/weekly?days=${days}&focus=${encodeURIComponent(focus)}`);
      state.weekly = result;
      $("#weekly-result").innerHTML = weeklyHtml(result);
      bindWeekly();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      button.disabled = false;
      button.textContent = "生成";
    }
  });
  if (existing) bindWeekly();
  const params = queryParams();
  if (params.get("auto")) {
    $("#weekly-focus").value = params.get("focus") || "";
    $("#weekly-button").click();
  }
}

function weeklyHtml(result) {
  const citationTags = (result.citations || [])
    .map(
      (citation) =>
        `<span class="tag mono">[${citation.index}] ${escapeHtml(citation.title)} · 片段 ${
          citation.chunk_index + 1
        }</span>`
    )
    .join("");
  return `<div class="grid two">
    <div class="card">
      <div class="card-head"><div><div class="card-title">周报预览</div>
        <div class="card-sub">${escapeHtml(result.engine)} · 近 ${result.days} 天 · 可复制或下载 Markdown</div></div>
        <div>
          <button class="btn small" data-action="copy-weekly">复制</button>
          <button class="btn small" data-action="download-weekly">下载</button>
          <button class="btn small primary" data-action="archive-weekly">归档到 PowerContext</button>
        </div>
      </div>
      ${result.warning ? `<div class="notice warn">${escapeHtml(result.warning)}</div>` : ""}
      <div class="answer" id="weekly-markdown">${renderMarkdown(result.markdown || "")}</div>
      <div class="tags" style="margin-top:12px">${citationTags}</div>
    </div>
    <div>
      <div class="card">
        <div class="card-head"><div class="card-title">本周数据</div></div>
        <dl class="kv">
          <dt>文献</dt><dd>${result.stats?.papers ?? 0} 篇</dd>
          <dt>切片</dt><dd>${result.stats?.chunks ?? 0} 个</dd>
          <dt>检索</dt><dd>${result.stats?.searches ?? 0} 次</dd>
          <dt>行为</dt><dd>${result.stats?.activities ?? 0} 条</dd>
        </dl>
      </div>
      <div class="card" style="margin-top:16px">
        <div class="card-head"><div class="card-title">本周行为</div></div>
        <div class="list">${
          (result.activity || [])
            .slice(0, 8)
            .map(
              (item) => `<div class="list-item"><div class="main">
                <div class="title">${escapeHtml(item.title || item.detail || item.kind)}</div>
                <div class="meta">${escapeHtml(item.kind)} · ${escapeHtml(item.created_at)}</div>
              </div></div>`
            )
            .join("") || '<div class="empty">本周还没有行为记录。</div>'
        }</div>
      </div>
    </div>
  </div>`;
}

function bindWeekly() {
  $$('[data-action="copy-weekly"]').forEach((button) =>
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(state.weekly?.markdown || "");
        toast("已复制 Markdown");
      } catch {
        toast("浏览器拒绝了剪贴板访问", "error");
      }
    })
  );
  $$('[data-action="download-weekly"]').forEach((button) =>
    button.addEventListener("click", () => {
      const blob = new Blob([state.weekly?.markdown || ""], { type: "text/markdown;charset=utf-8" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = "科研周报.md";
      link.click();
      URL.revokeObjectURL(link.href);
    })
  );
  $$('[data-action="archive-weekly"]').forEach((button) =>
    button.addEventListener("click", async () => {
      if (!state.weekly) return;
      button.disabled = true;
      try {
        await api("/api/weekly/archive", {
          method: "POST",
          body: JSON.stringify({ markdown: state.weekly.markdown, summary: "" }),
        });
        toast("已保存为 Source 并写入一条进展记忆");
      } catch (error) {
        toast(error.message, "error");
      } finally {
        button.disabled = false;
      }
    })
  );
}

/* ------------------------------------------------------------------ 核验 */
async function viewVerify() {
  setTitle("技术核验", "看得到每一次检索与记忆读写");
  const logs = await api("/api/logs?limit=20");
  await refreshHealth();
  const health = state.health || {};
  const seekdb = health.seekdb || {};
  const pc = health.powercontext || {};

  const retrievalRows = (logs.retrieval || [])
    .map((log) => {
      const fused = (log.fused_ids || [])
        .slice(0, 3)
        .map((item) => `<span class="tag mono">${escapeHtml(item.id)}</span>`)
        .join(" ");
      return `<tr>
        <td class="mono">${escapeHtml(log.created_at)}</td>
        <td>${escapeHtml(log.query)}</td>
        <td class="mono">${escapeHtml(log.mode)}</td>
        <td class="mono">V${log.vector_ids.length} / F${log.fts_ids.length}</td>
        <td>${fused || "—"}</td>
        <td class="mono">${log.latency_ms}ms</td>
      </tr>`;
    })
    .join("");

  const callRows = (logs.powercontext || [])
    .map(
      (call) => `<tr>
        <td class="mono">${escapeHtml(call.time || "")}</td>
        <td>${escapeHtml(call.op || "")}</td>
        <td class="mono">${escapeHtml(JSON.stringify(call.request || {}).slice(0, 90))}</td>
        <td class="mono">${call.status || ""}</td>
        <td class="mono">${call.duration_ms || 0}ms</td>
      </tr>`
    )
    .join("");

  $("#view").innerHTML = `
    <div class="grid stats" style="margin-bottom:16px">
      <div class="card stat"><div class="value" style="font-size:20px">${seekdb.ok ? "OK" : "未连接"}</div>
        <div class="label">seekdb ${escapeHtml(seekdb.version || "")}</div></div>
      <div class="card stat"><div class="value" style="font-size:20px">${seekdb.fts_available ? "ik" : "回退"}</div>
        <div class="label">全文索引解析器</div></div>
      <div class="card stat"><div class="value" style="font-size:20px">${seekdb.vector_index ? "HNSW" : "精确扫描"}</div>
        <div class="label">向量索引</div></div>
      <div class="card stat"><div class="value" style="font-size:20px">${pc.ok ? "OK" : "未连接"}</div>
        <div class="label">PowerContext scope</div></div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="card-head"><div><div class="card-title">本次运行配置</div>
        <div class="card-sub">scope 与地址来自 .env；不展示任何密钥</div></div>
        <button class="btn small" data-action="refresh-verify">刷新</button></div>
      <dl class="kv">
        <dt>seekdb</dt><dd>${escapeHtml(health.seekdb?.database || "-")} @ ${escapeHtml(
    (health.seekdb && health.seekdb.host) || "127.0.0.1"
  )}</dd>
        <dt>PowerContext</dt><dd>${escapeHtml(pc.url || "-")} · ${escapeHtml(pc.scope_id || "-")}</dd>
        <dt>Embedding</dt><dd>${escapeHtml(health.embedding?.provider || "-")} · ${health.embedding?.dim || "-"} 维</dd>
        <dt>生成模型</dt><dd>${health.llm?.enabled ? escapeHtml(health.llm.model) : "抽取式模板（未配置 LLM Key）"}</dd>
        <dt>检索策略</dt><dd>向量 Top-K + 全文 Top-K → RRF(k=60) → 结构化过滤</dd>
      </dl>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="card-head"><div><div class="card-title">seekdb 检索日志</div>
        <div class="card-sub">表 retrieval_logs：两路候选、融合结果与耗时</div></div></div>
      <div class="table-wrap">
        <table><thead><tr><th>时间</th><th>查询</th><th>通道</th><th>候选</th><th>融合 Top3</th><th>耗时</th></tr></thead>
        <tbody>${retrievalRows || '<tr><td colspan="6">暂无记录</td></tr>'}</tbody></table>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><div><div class="card-title">PowerContext 调用记录</div>
        <div class="card-sub">记忆写入、检索与上下文准备的本地审计日志</div></div></div>
      <div class="table-wrap">
        <table><thead><tr><th>时间</th><th>操作</th><th>参数摘要</th><th>状态</th><th>耗时</th></tr></thead>
        <tbody>${callRows || '<tr><td colspan="5">暂无记录</td></tr>'}</tbody></table>
      </div>
    </div>`;

  $$('[data-action="refresh-verify"]').forEach((button) =>
    button.addEventListener("click", () => route())
  );
}

/* ------------------------------------------------------------------ 弹窗 */
function openModal(html) {
  $("#modal-box").innerHTML = html;
  $("#modal").classList.remove("hidden");
  $$('[data-action="close-modal"]', $("#modal-box")).forEach((button) =>
    button.addEventListener("click", closeModal)
  );
}

function closeModal() {
  $("#modal").classList.add("hidden");
  $("#modal-box").innerHTML = "";
}

/* ------------------------------------------------------------------ 启动 */
function boot() {
  bindNav();
  window.addEventListener("hashchange", route);
  $("#modal").addEventListener("click", (event) => {
    if (event.target.id === "modal") closeModal();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeModal();
  });
  route();
}

boot();
