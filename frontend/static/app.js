// ── Elements ─────────────────────────────────────────────────────────────────
const chat = document.getElementById("chat");
const empty = document.getElementById("empty");
const form = document.getElementById("form");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");
const resetBtn = document.getElementById("reset");
const tabs = document.getElementById("tabs");
const publisherSelect = document.getElementById("publisher");
const complexitySelect = document.getElementById("complexity");
const useCacheToggle = document.getElementById("use-cache");
const debugToggle = document.getElementById("use-debug");
const tokenStreamToggle = document.getElementById("use-token-stream");
const streamToggle = document.getElementById("use-stream");
const streamToggleWrap = document.getElementById("stream-toggle-wrap");
const resolvedModel = document.getElementById("resolved-model");
const llmKeyInput = document.getElementById("llm-key");
const legend = document.getElementById("legend");
const sidebar = document.getElementById("sidebar");
const sessionChip = document.getElementById("session-chip");
const sessionList = document.getElementById("session-list");
const newChatBtn = document.getElementById("new-chat");
const statLatency = document.getElementById("stat-latency");
const statTokens = document.getElementById("stat-tokens");
const statSession = document.getElementById("stat-session");
const statTurns = document.getElementById("stat-turns");

let view = "direct";
let inFlight = false;
const VIEWS = {
  direct: { history: [], sessionTokens: 0, turns: 0, lastLatency: null, lastTokens: null },
  agent: { sessionId: null, sessionTokens: 0, turns: 0, lastLatency: null, lastTokens: null },
};

let modelMatrix = {};
let streamingAvailable = false;
let streamingPublishers = [];
const PUBLISHER_LABEL = { gemini: "Google", anthropic: "Anthropic" };
const COMPLEXITY_LABEL = { low: "Low (fast)", high: "High (capable)" };

// ── Config / legend ──────────────────────────────────────────────────────────
function populate(select, values, defaultValue, labelMap) {
  select.innerHTML = "";
  (values || []).forEach((v) => {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = labelMap?.[v] ?? v;
    if (v === defaultValue) opt.selected = true;
    select.appendChild(opt);
  });
}

function updateResolvedModel() {
  const key = `${publisherSelect.value}:${complexitySelect.value}`;
  resolvedModel.textContent = modelMatrix[key] ? `→ ${modelMatrix[key]}` : "";
}

// Show the Stream toggle only when the BFF has a streaming proxy configured AND
// the selected publisher supports streaming (Gemini for now).
function updateStreamToggleVisibility() {
  const ok = streamingAvailable && streamingPublishers.includes(publisherSelect.value);
  streamToggleWrap.hidden = !ok;
}

function streamingActive() {
  return streamingAvailable && streamToggle.checked && streamingPublishers.includes(publisherSelect.value);
}

function renderLegend(subAgents) {
  legend.innerHTML = "";
  (subAgents || []).forEach((a) => {
    const chip = document.createElement("span");
    chip.className = "legend-chip";
    chip.textContent = a.label;
    legend.appendChild(chip);
  });
}

async function loadConfig() {
  try {
    const cfg = await (await fetch("/api/config")).json();
    modelMatrix = cfg.model_matrix || {};
    populate(publisherSelect, cfg.publishers, cfg.default_publisher, PUBLISHER_LABEL);
    populate(complexitySelect, cfg.complexities, cfg.default_complexity, COMPLEXITY_LABEL);
    updateResolvedModel();
    renderLegend(cfg.sub_agents);
    streamingAvailable = !!cfg.streaming;
    streamingPublishers = cfg.streaming_publishers || [];
    updateStreamToggleVisibility();
  } catch (err) {
    console.warn("Failed to load /api/config:", err);
  }
}

// ── Rendering primitives ─────────────────────────────────────────────────────
function refreshEmptyState() {
  const has = chat.querySelector(`.turn[data-view="${view}"]`);
  empty.style.display = has ? "none" : "flex";
  empty.textContent =
    view === "agent"
      ? "Chat with the supervisor — it routes to specialists as needed."
      : "Compare models directly through the unified Apigee endpoint.";
}

function appendTurn(role, text, className = "") {
  const wrap = document.createElement("div");
  wrap.className = `turn ${role}`;
  wrap.dataset.view = view;
  const msg = document.createElement("div");
  msg.className = `msg ${className}`.trim();
  msg.textContent = text;
  wrap.appendChild(msg);
  chat.appendChild(wrap);
  chat.scrollTop = chat.scrollHeight;
  refreshEmptyState();
  return { wrap, msg };
}

function makeTyping() {
  const s = document.createElement("span");
  s.className = "typing";
  s.innerHTML = "<i></i><i></i><i></i>";
  return s;
}

function appendPendingModel() {
  const wrap = document.createElement("div");
  wrap.className = "turn model";
  wrap.dataset.view = view;
  const msg = document.createElement("div");
  msg.className = "msg pending";
  msg.appendChild(makeTyping());
  wrap.appendChild(msg);
  chat.appendChild(wrap);
  chat.scrollTop = chat.scrollHeight;
  refreshEmptyState();
  return { wrap, msg };
}

function renderMarkdown(target, text) {
  if (window.marked && window.DOMPurify) {
    target.innerHTML = window.DOMPurify.sanitize(window.marked.parse(text, { gfm: true, breaks: true }));
    // open links in a new tab so navigation doesn't blow away the chat
    target.querySelectorAll("a[href]").forEach((a) => {
      a.target = "_blank";
      a.rel = "noopener noreferrer";
    });
  } else {
    target.textContent = text;
  }
}

function formatLatency(ms) {
  if (ms == null) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${ms}ms`;
}

function formatTokens(usage) {
  if (!usage || usage.total == null) return "—";
  const p = usage.prompt ?? "?";
  const o = usage.output ?? "?";
  return (
    `<span class="tok-in" title="Input / prompt tokens">↓ ${p}</span>` +
    `<span class="tok-out" title="Output / response tokens">↑ ${o}</span>`
  );
}

async function copyToClipboard(btn, text) {
  try {
    await navigator.clipboard.writeText(text);
    const prev = btn.textContent;
    btn.textContent = "Copied";
    btn.classList.add("done");
    setTimeout(() => {
      btn.textContent = prev;
      btn.classList.remove("done");
    }, 1200);
  } catch {
    /* clipboard blocked (insecure context) — ignore */
  }
}

function renderMeta(wrap, { model, publisher, complexity, latency_ms, usage, copyText }) {
  const meta = document.createElement("div");
  meta.className = "meta";
  const pills = [`<span class="pill">model <strong>${model ?? "supervisor"}</strong></span>`];
  if (publisher && complexity) pills.push(`<span class="pill">route <strong>${publisher} · ${complexity}</strong></span>`);
  if (latency_ms != null) pills.push(`<span class="pill">latency <strong>${formatLatency(latency_ms)}</strong></span>`);
  if (usage && usage.total != null) pills.push(`<span class="pill tokens-pill">${formatTokens(usage)}</span>`);
  meta.innerHTML = pills.join("");
  if (copyText) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copy-btn";
    btn.textContent = "Copy";
    btn.title = "Copy response";
    btn.addEventListener("click", () => copyToClipboard(btn, copyText));
    meta.appendChild(btn);
  }
  wrap.appendChild(meta);
}

// Smooth typewriter: decouples bursty network deltas from rendering. Incoming
// text is queued and a requestAnimationFrame loop reveals it with ease-out
// catch-up, so even a whole sentence arriving at once types out fluidly.
// `render(el, text)` paints the revealed substring — pass renderMarkdown for
// LIVE formatting. Painting is throttled (~16/s) so the per-frame markdown
// re-parse stays cheap and flicker-free; finish() does one clean final render.
function makeTypewriter(msgEl, render) {
  render = render || ((el, t) => { el.textContent = t; });
  let target = "";
  let shown = 0;
  let raf = null;
  let finishing = false;
  let renderFinal = null;
  let resolveDone = null;
  let lastPaint = 0;
  const THROTTLE_MS = 60; // cap live re-renders at ~16/s

  const paint = (ts) => {
    raf = null;
    if (shown < target.length) {
      const remaining = target.length - shown;
      shown = Math.min(target.length, shown + Math.max(1, Math.round(remaining / 7)));
      const now = ts || performance.now();
      if (now - lastPaint >= THROTTLE_MS) {
        render(msgEl, target.slice(0, shown));
        chat.scrollTop = chat.scrollHeight;
        lastPaint = now;
      }
      schedule();
    } else if (finishing) {
      if (renderFinal) renderFinal();
      if (resolveDone) resolveDone();
    }
  };
  const schedule = () => {
    if (raf === null) raf = requestAnimationFrame(paint);
  };

  return {
    push(text) {
      if (!text) return;
      target += text;
      schedule();
    },
    // Resolves once the queue has fully drained and renderFinalFn has run.
    finish(renderFinalFn) {
      finishing = true;
      renderFinal = renderFinalFn;
      return new Promise((res) => {
        resolveDone = res;
        schedule();
      });
    },
  };
}

// Debug: collapsible panel of the raw per-event data for a streamed message.
function attachEvents(wrap, events) {
  if (!events || !events.length) return;
  const details = document.createElement("details");
  details.className = "events";
  const summary = document.createElement("summary");
  summary.textContent = `raw events (${events.length})`;
  details.appendChild(summary);
  const pre = document.createElement("pre");
  pre.className = "events-body";
  pre.textContent = events
    .map((e, i) => `# ${i}\n${JSON.stringify(e, null, 2)}`)
    .join("\n\n");
  details.appendChild(pre);
  wrap.appendChild(details);
}

function delegationChip(d) {
  const chip = document.createElement("span");
  chip.className = "delegation";
  chip.innerHTML = `→ Transferring to sub-agent: <strong>${d.label}</strong>`;
  return chip;
}

function renderDelegations(wrap, delegations) {
  if (!delegations || !delegations.length) return;
  const box = document.createElement("div");
  box.className = "delegations";
  delegations.forEach((d) => box.appendChild(delegationChip(d)));
  wrap.insertBefore(box, wrap.firstChild);
}

// Collapsible "thinking" panel for a model turn. Idempotent: creates the
// <details> once (placed just above the answer `beforeEl`) and returns its body
// so callers can stream text into it (live) or set it once (history replay).
function ensureThinkingPanel(wrap, beforeEl) {
  let details = wrap.querySelector(":scope > details.thinking");
  if (!details) {
    details = document.createElement("details");
    details.className = "thinking";
    const summary = document.createElement("summary");
    summary.textContent = "Thinking";
    details.appendChild(summary);
    const body = document.createElement("div");
    body.className = "thinking-body";
    details.appendChild(body);
    wrap.insertBefore(details, beforeEl || null);
  }
  return details.querySelector(".thinking-body");
}

// ── Stats ────────────────────────────────────────────────────────────────────
function applyStats(state) {
  statLatency.textContent = formatLatency(state.lastLatency);
  statTokens.innerHTML = state.lastTokens ? formatTokens(state.lastTokens) : "—";
  statSession.textContent = state.sessionTokens.toLocaleString();
  statTurns.textContent = state.turns;
}

function recordStats(state, { latency_ms, usage }) {
  state.lastLatency = latency_ms;
  state.lastTokens = usage || null;
  if (usage?.total) state.sessionTokens += usage.total;
  state.turns += 1;
  applyStats(state);
}

// ── View switching ───────────────────────────────────────────────────────────
function switchView(next) {
  if (next === view || inFlight) return;
  view = next;
  document.body.className = `view-${view}`;
  tabs.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  if (view === "admin") {
    loadAdmin();
    return; // no chat stats/empty-state/input in the admin view
  }
  if (view === "trace") {
    loadTraces();
    return; // trace explorer has its own layout
  }
  if (view === "sessions") {
    loadSessionTree();
    return; // navigator tree has its own layout
  }
  applyStats(VIEWS[view]);
  refreshEmptyState();
  if (view === "agent") loadSessions();
  input.focus();
}

tabs.addEventListener("click", (e) => {
  const btn = e.target.closest(".tab");
  if (btn) switchView(btn.dataset.view);
});

publisherSelect.addEventListener("change", () => {
  updateResolvedModel();
  updateStreamToggleVisibility();
});
complexitySelect.addEventListener("change", updateResolvedModel);

// ── Direct view ──────────────────────────────────────────────────────────────
async function sendDirect(message) {
  const state = VIEWS.direct;
  state.history.push({ role: "user", text: message });
  appendTurn("user", message);
  const { wrap, msg } = appendPendingModel();
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        history: state.history,
        publisher: publisherSelect.value,
        complexity: complexitySelect.value,
        use_cache: useCacheToggle.checked,
        api_key: llmKeyInput.value.trim(),
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      wrap.remove();
      appendTurn("model", `Error: ${data.error || res.status}${data.detail ? `\n\n${data.detail}` : ""}`, "error");
      state.history.pop();
      return;
    }
    msg.classList.remove("pending");
    msg.innerHTML = "";
    if (data.text) {
      msg.classList.add("markdown");
      renderMarkdown(msg, data.text);
    } else {
      msg.textContent = "(empty response)";
    }
    renderMeta(wrap, { ...data, copyText: data.text });
    recordStats(state, data);
    state.history.push({ role: "model", text: data.text || "" });
  } catch (err) {
    wrap.remove();
    appendTurn("model", `Network error: ${err.message}`, "error");
    state.history.pop();
  }
}

// Streamed Direct turn: tokens stream in from the separate streaming proxy.
async function sendDirectStream(message) {
  const state = VIEWS.direct;
  state.history.push({ role: "user", text: message });
  appendTurn("user", message);
  const { wrap, msg } = appendPendingModel();

  let answer = "";
  let answering = false;
  let donePayload = null;
  let errMsg = null;
  let tw = null;
  const rawEvents = [];

  const beginAnswer = () => {
    if (!answering) {
      answering = true;
      msg.classList.remove("pending");
      msg.classList.add("markdown", "streaming");
      msg.innerHTML = "";
      tw = makeTypewriter(msg, renderMarkdown); // live formatting
    }
  };
  const onEvent = (evt) => {
    const d = evt.data || {};
    switch (evt.type) {
      case "text":
        beginAnswer();
        answer += d.text || "";
        tw.push(d.text || "");
        break;
      case "raw":
        rawEvents.push(d);
        break;
      case "done":
        donePayload = d;
        break;
      case "error":
        errMsg = d.message || "stream error";
        break;
    }
  };

  try {
    const res = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        history: state.history,
        publisher: publisherSelect.value,
        complexity: complexitySelect.value,
        use_cache: useCacheToggle.checked,
        debug: debugToggle.checked,
        api_key: llmKeyInput.value.trim(),
      }),
    });
    if (!res.ok || !res.body) {
      const d = await res.json().catch(() => ({}));
      throw new Error(d.error || `HTTP ${res.status}`);
    }
    await consumeSSE(res, onEvent);

    if (errMsg && !answering) {
      msg.classList.remove("pending", "markdown");
      msg.classList.add("error");
      msg.textContent = `Error: ${errMsg}`;
      state.history.pop();
      return;
    }
    if (!answering) {
      msg.classList.remove("pending");
      msg.textContent = errMsg ? `Error: ${errMsg}` : "(empty response)";
    }
    if (answering && tw) {
      await tw.finish(() => {
        msg.classList.remove("streaming");
        renderMarkdown(msg, answer); // clean final pass over the full text
      });
    }
    const meta = donePayload || {};
    renderMeta(wrap, {
      model: meta.model,
      publisher: meta.publisher,
      complexity: meta.complexity,
      latency_ms: meta.latency_ms,
      usage: meta.usage,
      copyText: answer,
    });
    recordStats(state, { latency_ms: meta.latency_ms, usage: meta.usage });
    attachEvents(wrap, rawEvents);
    state.history.push({ role: "model", text: answer });
  } catch (err) {
    msg.classList.remove("pending", "markdown");
    msg.classList.add("error");
    msg.textContent = `Network error: ${err.message}`;
    state.history.pop();
  }
}

// ── Agent view ───────────────────────────────────────────────────────────────
function updateSessionChip(id) {
  sessionChip.textContent = id ? `session ${id.slice(0, 8)}…` : "no session";
}

function parseSSE(frame) {
  let type = "message";
  const data = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) type = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
  }
  if (!data.length) return null;
  try {
    return { type, data: JSON.parse(data.join("\n")) };
  } catch {
    return null;
  }
}

// Read an SSE response body, dispatching each parsed frame to onEvent.
async function consumeSSE(res, onEvent) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, i);
      buf = buf.slice(i + 2);
      const evt = parseSSE(frame);
      if (evt) onEvent(evt);
    }
  }
  if (buf.trim()) {
    const evt = parseSSE(buf);
    if (evt) onEvent(evt);
  }
}

// Streamed turn: delegations render the instant the supervisor emits them
// (before the sub-agent answers), then the answer text streams in.
async function sendAgent(message) {
  const state = VIEWS.agent;
  appendTurn("user", message);
  const { wrap, msg } = appendPendingModel();

  let box = null;
  let answer = "";
  let thoughts = "";
  let answering = false;
  let donePayload = null;
  let errMsg = null;
  let tw = null;
  const rawEvents = [];

  const ensureBox = () => {
    if (!box) {
      box = document.createElement("div");
      box.className = "delegations";
      wrap.insertBefore(box, wrap.firstChild);
    }
    return box;
  };
  const beginAnswer = () => {
    if (!answering) {
      answering = true;
      msg.classList.remove("pending");
      msg.classList.add("markdown", "streaming");
      msg.innerHTML = "";
      tw = makeTypewriter(msg, renderMarkdown); // live formatting
    }
  };
  const onEvent = (evt) => {
    const d = evt.data || {};
    switch (evt.type) {
      case "session":
        if (d.session_id) {
          state.sessionId = d.session_id;
          updateSessionChip(d.session_id);
        }
        break;
      case "delegation":
        ensureBox().appendChild(delegationChip(d));
        chat.scrollTop = chat.scrollHeight;
        break;
      case "thought":
        thoughts += d.text || "";
        ensureThinkingPanel(wrap, msg).textContent = thoughts;
        chat.scrollTop = chat.scrollHeight;
        break;
      case "text":
        beginAnswer();
        answer += d.text || "";
        tw.push(d.text || "");
        break;
      case "raw":
        rawEvents.push(d);
        break;
      case "done":
        donePayload = d;
        break;
      case "error":
        errMsg = d.message || "agent stream error";
        break;
    }
  };

  try {
    const res = await fetch("/api/agent/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        session_id: state.sessionId,
        debug: debugToggle.checked,
        stream_tokens: tokenStreamToggle.checked,
      }),
    });
    if (!res.ok || !res.body) {
      const d = await res.json().catch(() => ({}));
      throw new Error(d.error || `HTTP ${res.status}`);
    }
    await consumeSSE(res, onEvent);

    if (errMsg && !answering) {
      msg.classList.remove("pending", "markdown");
      msg.classList.add("error");
      msg.textContent = `Error: ${errMsg}`;
      return;
    }
    if (!answering) {
      msg.classList.remove("pending");
      msg.textContent = errMsg ? `Error: ${errMsg}` : "(no text returned)";
    }
    if (answering && tw) {
      await tw.finish(() => {
        msg.classList.remove("streaming");
        renderMarkdown(msg, answer); // clean final pass over the full text
      });
    }
    const meta = donePayload || {};
    renderMeta(wrap, {
      model: "supervisor",
      latency_ms: meta.latency_ms,
      usage: meta.usage,
      copyText: answer,
    });
    recordStats(state, { latency_ms: meta.latency_ms, usage: meta.usage });
    attachEvents(wrap, rawEvents);
    loadSessions(); // surface the new/updated session in the sidebar
  } catch (err) {
    msg.classList.remove("pending", "markdown");
    msg.classList.add("error");
    msg.textContent = `Network error: ${err.message}`;
  }
}

// ── Sessions sidebar ─────────────────────────────────────────────────────────
function relativeTime(value) {
  if (!value) return "";
  let t = Date.parse(value);
  if (Number.isNaN(t)) {
    const n = Number(value);
    if (!Number.isNaN(n)) t = n < 1e12 ? n * 1000 : n;
  }
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function markActiveSession(id) {
  sessionList.querySelectorAll(".session-item").forEach((li) => {
    li.classList.toggle("active", li.dataset.id === id);
  });
}

function renderSessionList(sessions) {
  sessionList.innerHTML = "";
  if (!sessions.length) {
    const li = document.createElement("li");
    li.className = "session-empty";
    li.textContent = "No sessions yet";
    sessionList.appendChild(li);
    return;
  }
  sessions.forEach((s) => {
    const li = document.createElement("li");
    li.className = "session-item" + (s.session_id === VIEWS.agent.sessionId ? " active" : "");
    li.dataset.id = s.session_id;
    const title = document.createElement("div");
    title.className = "session-item-title";
    title.textContent = s.title || "Untitled session";
    const time = document.createElement("div");
    time.className = "session-item-time";
    time.textContent = relativeTime(s.last_update_time);
    const del = document.createElement("button");
    del.type = "button";
    del.className = "session-del";
    del.title = "Delete session";
    del.textContent = "✕";
    del.addEventListener("click", (e) => {
      e.stopPropagation();
      deleteSession(s.session_id);
    });
    li.appendChild(title);
    li.appendChild(time);
    li.appendChild(del);
    li.addEventListener("click", () => selectSession(s.session_id));
    sessionList.appendChild(li);
  });
}

async function loadSessions() {
  try {
    const data = await (await fetch("/api/sessions")).json();
    if (data.sessions) renderSessionList(data.sessions);
  } catch (err) {
    console.warn("Failed to load sessions:", err);
  }
}

function clearAgentTurns() {
  chat.querySelectorAll('.turn[data-view="agent"]').forEach((el) => el.remove());
}

function resetAgentStats() {
  const state = VIEWS.agent;
  state.sessionTokens = 0;
  state.turns = 0;
  state.lastLatency = null;
  state.lastTokens = null;
  applyStats(state);
}

async function selectSession(id) {
  if (inFlight || id === VIEWS.agent.sessionId) return;
  clearAgentTurns();
  resetAgentStats();
  const state = VIEWS.agent;
  state.sessionId = id;
  updateSessionChip(id);
  markActiveSession(id);
  refreshEmptyState();
  try {
    const data = await (await fetch(`/api/sessions/${encodeURIComponent(id)}`)).json();
    const turns = data.turns || [];
    turns.forEach((t) => {
      if (t.role === "user") {
        appendTurn("user", t.text);
      } else {
        const { wrap, msg } = appendTurn("model", "");
        renderDelegations(wrap, t.delegations);
        if (t.thoughts) ensureThinkingPanel(wrap, msg).textContent = t.thoughts;
        if (t.text) {
          msg.classList.add("markdown");
          renderMarkdown(msg, t.text);
        } else {
          msg.textContent = "(no text)";
        }
        renderMeta(wrap, { model: "supervisor", copyText: t.text });
      }
    });
    state.turns = turns.filter((t) => t.role === "model").length;
    applyStats(state);
  } catch (err) {
    appendTurn("model", `Failed to load session: ${err.message}`, "error");
  }
}

async function deleteSession(id) {
  if (inFlight) return;
  if (!window.confirm("Delete this session? This can't be undone.")) return;
  try {
    const res = await fetch(`/api/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      console.warn("Delete failed:", d.error || res.status);
      return;
    }
    if (id === VIEWS.agent.sessionId) newChat();
    loadSessions();
  } catch (err) {
    console.warn("Delete failed:", err.message);
  }
}

function newChat() {
  if (inFlight) return;
  clearAgentTurns();
  resetAgentStats();
  VIEWS.agent.sessionId = null;
  updateSessionChip(null);
  markActiveSession(null);
  refreshEmptyState();
  input.focus();
}

newChatBtn.addEventListener("click", newChat);

// Direct view: clear the comparison transcript + its stats.
resetBtn.addEventListener("click", () => {
  if (inFlight) return;
  chat.querySelectorAll('.turn[data-view="direct"]').forEach((el) => el.remove());
  const state = VIEWS.direct;
  state.history.length = 0;
  state.sessionTokens = 0;
  state.turns = 0;
  state.lastLatency = null;
  state.lastTokens = null;
  applyStats(state);
  refreshEmptyState();
  input.focus();
});

// ── Composer ─────────────────────────────────────────────────────────────────
function autoGrow() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
}

async function send(message) {
  if (inFlight) return;
  inFlight = true;
  sendBtn.disabled = true;
  input.disabled = true;
  try {
    if (view === "agent") await sendAgent(message);
    else if (streamingActive()) await sendDirectStream(message);
    else await sendDirect(message);
  } finally {
    inFlight = false;
    sendBtn.disabled = false;
    input.disabled = false;
    input.focus();
    autoGrow();
  }
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || inFlight) return;
  input.value = "";
  autoGrow();
  send(text);
});

input.addEventListener("input", autoGrow);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});

// ── Admin view (ACL management — /api/admin/*, server-side enforced) ─────────
const adminTab = document.getElementById("admin-tab");
const adminAlert = document.getElementById("admin-alert");
const adminRoles = document.getElementById("admin-roles");
const adminUsers = document.getElementById("admin-users");
const roleAddForm = document.getElementById("role-add");
const userAddForm = document.getElementById("user-add");

const ADMIN = { roles: {}, users: {}, agents: [] };

function adminNote(text, isError = false) {
  adminAlert.hidden = !text;
  adminAlert.textContent = text || "";
  adminAlert.classList.toggle("error", isError);
  if (text && !isError) setTimeout(() => { adminAlert.hidden = true; }, 2500);
}

async function adminApi(url, opts = {}) {
  const res = await fetch(url, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) {
    let detail = `${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* keep status */ }
    throw new Error(detail);
  }
  return res.json();
}

async function loadAdmin() {
  adminRoles.textContent = "loading…";
  adminUsers.textContent = "";
  try {
    const [acl, agents] = await Promise.all([
      adminApi("/api/admin/acl"),
      adminApi("/api/admin/agents"),
    ]);
    ADMIN.roles = acl.roles || {};
    ADMIN.users = acl.users || {};
    ADMIN.agents = (agents.agents || []).map((a) => a.name);
    renderAdmin();
  } catch (err) {
    adminRoles.textContent = "";
    adminNote(`Failed to load ACL: ${err.message}`, true);
  }
}

function checkbox(labelText, checked, extraClass = "") {
  const label = document.createElement("label");
  label.className = `admin-check ${extraClass}`.trim();
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = checked;
  const span = document.createElement("span");
  span.textContent = labelText;
  label.append(box, span);
  return { label, box };
}

function actionBtn(text, cls, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = cls;
  b.textContent = text;
  b.addEventListener("click", onClick);
  return b;
}

async function mutate(promise, okMsg) {
  try {
    await promise;
    adminNote(okMsg);
    await loadAdmin();
  } catch (err) {
    adminNote(err.message, true);
  }
}

function renderAdmin() {
  // Roles: '*' first, then every registry agent + any extra names a role holds.
  const knownAgents = [...new Set([
    ...ADMIN.agents,
    ...Object.values(ADMIN.roles).flatMap((r) => (r.agents || []).filter((a) => a !== "*")),
  ])].sort();

  adminRoles.innerHTML = "";
  Object.entries(ADMIN.roles).sort().forEach(([name, role]) => {
    const card = document.createElement("div");
    card.className = "admin-item";
    const head = document.createElement("div");
    head.className = "admin-item-head";
    const title = document.createElement("strong");
    title.textContent = name;
    head.appendChild(title);

    const { label: adminLabel, box: adminBox } = checkbox("admin (can manage this view)",
      !!role.is_admin, "admin-flag");
    const agentBoxes = [];
    const agentsWrap = document.createElement("div");
    agentsWrap.className = "admin-agents";
    const star = checkbox("* all agents", (role.agents || []).includes("*"));
    agentBoxes.push(["*", star.box]);
    agentsWrap.appendChild(star.label);
    knownAgents.forEach((a) => {
      const c = checkbox(a, (role.agents || []).includes(a));
      agentBoxes.push([a, c.box]);
      agentsWrap.appendChild(c.label);
    });

    const actions = document.createElement("div");
    actions.className = "admin-actions";
    actions.append(
      actionBtn("Save", "admin-save", () => {
        const agents = agentBoxes.filter(([, b]) => b.checked).map(([a]) => a);
        mutate(adminApi(`/api/admin/roles/${encodeURIComponent(name)}`, {
          method: "PUT", body: JSON.stringify({ agents, is_admin: adminBox.checked }),
        }), `role '${name}' saved`);
      }),
      actionBtn("Delete", "admin-delete", () => {
        if (!confirm(`Delete role '${name}'? Users holding it lose its agents.`)) return;
        mutate(adminApi(`/api/admin/roles/${encodeURIComponent(name)}`, { method: "DELETE" }),
          `role '${name}' deleted`);
      }),
    );
    head.appendChild(actions);
    card.append(head, adminLabel, agentsWrap);
    adminRoles.appendChild(card);
  });

  adminUsers.innerHTML = "";
  Object.entries(ADMIN.users).sort().forEach(([email, roles]) => {
    const card = document.createElement("div");
    card.className = "admin-item";
    const head = document.createElement("div");
    head.className = "admin-item-head";
    const title = document.createElement("strong");
    title.textContent = email;
    head.appendChild(title);

    const roleBoxes = [];
    const rolesWrap = document.createElement("div");
    rolesWrap.className = "admin-agents";
    Object.keys(ADMIN.roles).sort().forEach((r) => {
      const c = checkbox(r, (roles || []).includes(r));
      roleBoxes.push([r, c.box]);
      rolesWrap.appendChild(c.label);
    });

    const actions = document.createElement("div");
    actions.className = "admin-actions";
    actions.append(
      actionBtn("Save", "admin-save", () => {
        const chosen = roleBoxes.filter(([, b]) => b.checked).map(([r]) => r);
        mutate(adminApi(`/api/admin/users/${encodeURIComponent(email)}`, {
          method: "PUT", body: JSON.stringify({ roles: chosen }),
        }), `user '${email}' saved`);
      }),
      actionBtn("Delete", "admin-delete", () => {
        if (!confirm(`Remove '${email}' from the ACL? They lose all agents (fail-closed).`)) return;
        mutate(adminApi(`/api/admin/users/${encodeURIComponent(email)}`, { method: "DELETE" }),
          `user '${email}' removed`);
      }),
    );
    head.appendChild(actions);
    card.append(head, rolesWrap);
    adminUsers.appendChild(card);
  });
}

roleAddForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const name = document.getElementById("role-name").value.trim().toLowerCase();
  if (!name) return;
  document.getElementById("role-name").value = "";
  mutate(adminApi(`/api/admin/roles/${encodeURIComponent(name)}`, {
    method: "PUT", body: JSON.stringify({ agents: [], is_admin: false }),
  }), `role '${name}' created — now check its agents and Save`);
});

userAddForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const email = document.getElementById("user-email").value.trim().toLowerCase();
  if (!email) return;
  document.getElementById("user-email").value = "";
  mutate(adminApi(`/api/admin/users/${encodeURIComponent(email)}`, {
    method: "PUT", body: JSON.stringify({ roles: [] }),
  }), `user '${email}' added — now check their roles and Save`);
});

// ── Trace Explorer (admin view — /api/admin/traces*, server-side enforced) ───
// Reconstructs one end-to-end transaction from the four ai-logs: the full
// reference topology with the touched components lit, an animated packet whose
// dwell on each hop is proportional to that hop's measured latency, a latency
// waterfall, and the raw log records.
const SVGNS = "http://www.w3.org/2000/svg";
const traceListEl = document.getElementById("trace-list");
const traceCountEl = document.getElementById("trace-count");
const traceUserSel = document.getElementById("trace-user");
const traceViewSel = document.getElementById("trace-view");
const traceEmptyEl = document.getElementById("trace-empty");
const tracePane = document.getElementById("trace-viewpane");
const traceSummaryEl = document.getElementById("trace-summary");
const traceGraph = document.getElementById("trace-graph");
const traceWaterfall = document.getElementById("trace-waterfall");
const traceRecords = document.getElementById("trace-records");
const tracePlay = document.getElementById("trace-play");
const traceReplay = document.getElementById("trace-replay");
const traceScrub = document.getElementById("trace-scrub");
const traceClock = document.getElementById("trace-clock");

// SVG layout (fixed reference topology — coords in viewBox space).
const T_COLGAP = 152, T_ROWGAP = 86, T_MX = 82, T_MY = 46, T_NW = 120, T_NH = 50;
const anim = { raf: 0, playing: false, speed: 1, wall: 0, total: 1, steps: [], flow: null };

function svgEl(tag, attrs = {}) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function fmtMs(v) {
  if (v == null) return "—";
  return v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`;
}

async function loadTraces() {
  traceListEl.innerHTML = SPINNER("loading traces…");
  const params = new URLSearchParams();
  if (traceUserSel.value) params.set("user", traceUserSel.value);
  if (traceViewSel.value) params.set("view", traceViewSel.value);
  try {
    const data = await adminApi(`/api/admin/traces?${params}`);
    renderTraceList(data.traces || []);
  } catch (err) {
    traceListEl.textContent = "";
    traceCountEl.textContent = `failed: ${err.message}`;
  }
}

function renderTraceList(traces) {
  // keep the user filter populated with everyone seen (union, preserve pick)
  const seen = new Set([...traceUserSel.options].map((o) => o.value));
  for (const t of traces) {
    if (t.user && !seen.has(t.user)) {
      seen.add(t.user);
      traceUserSel.append(new Option(t.user, t.user));
    }
  }
  traceCountEl.textContent = `${traces.length} trace${traces.length === 1 ? "" : "s"}`;
  traceListEl.replaceChildren();
  if (!traces.length) {
    traceListEl.innerHTML = `<div class="trace-list-empty">No traces yet — run a turn in the Direct or Agent view.</div>`;
    return;
  }
  // Group by session — all the turns of one conversation together (agent turns
  // carry the Agent Engine session id; Direct turns have none → their own group).
  const groups = new Map();
  for (const t of traces) {
    const key = t.session_id || "";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(t);
  }
  for (const [sid, items] of groups) {
    const group = document.createElement("div");
    group.className = "trace-group";
    const head = document.createElement("div");
    head.className = "trace-group-head";
    const totalMs = items.reduce((a, t) => a + (t.total_ms || 0), 0);
    const n = `${items.length} turn${items.length === 1 ? "" : "s"}`;
    head.innerHTML = sid
      ? `<span class="tg-label">session</span><span class="tg-id" title="${escapeHtml(sid)}">${escapeHtml(sid)}</span><span class="tg-meta">${n} · ${fmtMs(totalMs)}</span>`
      : `<span class="tg-label">no session</span><span class="tg-meta">${n} · direct</span>`;
    group.append(head);
    for (const t of items) group.append(traceRow(t));
    traceListEl.append(group);
  }
}

function traceRow(t) {
  const row = document.createElement("button");
  row.type = "button";
  row.className = "trace-row";
  row.dataset.id = t.trace_id;
  const when = t.started_at ? new Date(t.started_at).toLocaleTimeString() : "";
  const detail = t.view === "agent"
    ? `${(t.delegations || []).length} agent${(t.delegations || []).length === 1 ? "" : "s"}`
    : (t.model || "direct");
  row.innerHTML = `
    <span class="trace-row-top">
      <span class="trace-row-lat">${fmtMs(t.total_ms)}</span>
      <span class="trace-row-view trace-view-${t.view || "direct"}">${t.view || "?"}</span>
    </span>
    <span class="trace-row-user">${escapeHtml(t.user || "unknown")}</span>
    <span class="trace-row-meta">${escapeHtml(detail)} · ${when}</span>`;
  row.addEventListener("click", () => {
    traceListEl.querySelectorAll(".trace-row").forEach((r) => r.classList.remove("active"));
    row.classList.add("active");
    openTrace(t.trace_id);
  });
  return row;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function openTrace(id) {
  stopAnim();
  traceEmptyEl.hidden = true;
  tracePane.hidden = false;
  traceSummaryEl.innerHTML = SPINNER("loading trace…");
  try {
    const flow = await adminApi(`/api/admin/traces/${id}`);
    anim.flow = flow;
    renderSummary(flow);
    renderTopology(flow);
    renderPie(flow);
    renderWaterfall(flow);
    renderRecords(flow);
    buildAnimation(flow);
  } catch (err) {
    traceSummaryEl.textContent = `Failed to load trace: ${err.message}`;
  }
}

function renderSummary(flow) {
  const s = flow.summary;
  const chips = [
    ["total", fmtMs(s.total_ms)],
    ["hops", s.hops],
    ["view", s.view || "?"],
    ["user", s.user || "unknown"],
  ];
  if (s.model) chips.push(["model", s.model]);
  if (s.session_id) chips.push(["session", s.session_id]);
  if (s.delegations?.length) chips.push(["delegated", s.delegations.join(", ")]);
  traceSummaryEl.replaceChildren();
  for (const [k, v] of chips) {
    const c = document.createElement("span");
    c.className = "trace-chip";
    c.innerHTML = `<span class="trace-chip-k">${k}</span><span class="trace-chip-v">${escapeHtml(String(v))}</span>`;
    traceSummaryEl.append(c);
  }
  const tid = document.createElement("span");
  tid.className = "trace-chip trace-chip-id";
  tid.textContent = s.trace_id;
  traceSummaryEl.append(tid);
}

function layoutNodes(nodes) {
  const pos = {};
  for (const n of nodes) {
    pos[n.id] = { x: T_MX + n.col * T_COLGAP, y: T_MY + n.row * T_ROWGAP, node: n };
  }
  return pos;
}

function renderTopology(flow) {
  const nodes = flow.nodes;
  const pos = layoutNodes(nodes);
  const maxCol = Math.max(...nodes.map((n) => n.col));
  const maxRow = Math.max(...nodes.map((n) => n.row));
  const W = T_MX * 2 + maxCol * T_COLGAP;
  const H = T_MY * 2 + maxRow * T_ROWGAP;
  traceGraph.setAttribute("viewBox", `0 0 ${W} ${H}`);
  traceGraph.replaceChildren();

  // reference edges (greyed); brighten those between two lit nodes
  const edgeLayer = svgEl("g", { class: "trace-edges" });
  for (const e of flow.edges) {
    const a = pos[e.from], b = pos[e.to];
    if (!a || !b) continue;
    const lit = a.node.hit && b.node.hit;
    edgeLayer.append(svgEl("line", {
      x1: a.x, y1: a.y, x2: b.x, y2: b.y,
      class: `trace-edge${lit ? " lit" : ""}`,
    }));
  }
  traceGraph.append(edgeLayer);

  // nodes
  const nodeLayer = svgEl("g", { class: "trace-nodes" });
  for (const n of nodes) {
    const p = pos[n.id];
    const g = svgEl("g", {
      class: `trace-node${n.hit ? " hit" : " dark"}`,
      "data-node": n.id,
      transform: `translate(${p.x},${p.y})`,
      style: n.hit ? `color:${n.color}` : "",
    });
    const rect = svgEl("rect", {
      x: -T_NW / 2, y: -T_NH / 2, width: T_NW, height: T_NH, rx: 11,
      fill: n.hit ? n.color : "var(--panel-2)",
      stroke: n.hit ? n.color : "var(--border)",
    });
    g.append(rect);
    const label = svgEl("text", { class: "trace-node-label", y: n.sub ? -3 : 4 });
    label.textContent = n.label;
    g.append(label);
    if (n.sub) {
      const sub = svgEl("text", { class: "trace-node-sub", y: 11 });
      sub.textContent = n.sub;
      g.append(sub);
    }
    if (n.hit && n.latency_ms != null) {
      const lat = svgEl("text", { class: "trace-node-lat", y: T_NH / 2 + 14 });
      lat.textContent = fmtMs(n.latency_ms);
      g.append(lat);
      // when a hop's SELF time is much less than its cumulative, show it — this
      // is the "the gateway only added Xms; the backend is the rest" insight
      if (n.self_ms != null && n.self_ms < n.latency_ms * 0.8) {
        const self = svgEl("text", { class: "trace-node-self", y: T_NH / 2 + 25 });
        self.textContent = `self ${fmtMs(n.self_ms)}`;
        g.append(self);
      }
    }
    nodeLayer.append(g);
  }
  traceGraph.append(nodeLayer);

  // the moving packet (hidden until play)
  const packet = svgEl("circle", { class: "trace-packet", r: 8, cx: -99, cy: -99 });
  traceGraph.append(packet);
  anim.pos = pos;
  anim.packet = packet;
}

function renderWaterfall(flow) {
  const spans = flow.spans.filter((s) => s.start != null);
  traceWaterfall.replaceChildren();
  if (!spans.length) {
    traceWaterfall.innerHTML = `<div class="trace-list-empty">No timed spans on this trace.</div>`;
    return;
  }
  const t0 = Math.min(...spans.map((s) => s.start));
  const t1 = Math.max(...spans.map((s) => (s.end != null ? s.end : s.start)));
  const span = Math.max(t1 - t0, 1);
  const byId = Object.fromEntries(flow.nodes.map((n) => [n.id, n]));
  for (const s of spans) {
    const n = byId[s.node] || {};
    const left = Math.min(99, ((s.start - t0) / span) * 100);
    const width = Math.max(Math.min(100 - left, (((s.end ?? s.start) - s.start) / span) * 100), 1.5);
    const row = document.createElement("div");
    row.className = "wf-row";
    row.dataset.node = s.node;
    // bars use margin-left + width (block flow) — robust vs. absolute collapse
    const label = document.createElement("span");
    label.className = "wf-label";
    label.textContent = n.label || s.node;
    const track = document.createElement("span");
    track.className = "wf-track";
    const bar = document.createElement("span");
    bar.className = "wf-bar";
    bar.style.marginLeft = `${left}%`;
    bar.style.width = `${width}%`;
    bar.style.background = n.color || "#888";
    track.append(bar);
    const lat = document.createElement("span");
    lat.className = "wf-lat";
    lat.textContent = fmtMs(s.latency_ms);
    row.append(label, track, lat);
    traceWaterfall.append(row);
  }
}

// ── Latency-by-component donut (interactive) ─────────────────────────────────
function renderPie(flow) {
  const svg = document.getElementById("trace-pie-svg");
  const legend = document.getElementById("trace-pie-legend");
  svg.replaceChildren();
  legend.replaceChildren();
  // Exclusive per-LAYER attribution (model / backend / gateway / agent / A2A
  // transport / startup / streaming) — every ms assigned to the innermost thing
  // actually happening, so it sums to the turn with no residual guesswork.
  const parts = (flow.summary.attribution || [])
    .map((a) => ({ id: `attr:${a.label}`, label: a.label, color: a.color, ms: a.ms }));
  const total = parts.reduce((a, p) => a + p.ms, 0);
  if (!total) {
    legend.innerHTML = `<div class="trace-list-empty">No per-component latencies.</div>`;
    return;
  }
  const cx = 60, cy = 60, r = 46, rin = 27;
  let a0 = -Math.PI / 2;
  for (const p of parts) {
    const frac = p.ms / total;
    const a1 = a0 + frac * Math.PI * 2;
    const big = frac > 0.5 ? 1 : 0;
    const x0 = cx + r * Math.cos(a0), y0 = cy + r * Math.sin(a0);
    const x1 = cx + r * Math.cos(a1), y1 = cy + r * Math.sin(a1);
    const xi1 = cx + rin * Math.cos(a1), yi1 = cy + rin * Math.sin(a1);
    const xi0 = cx + rin * Math.cos(a0), yi0 = cy + rin * Math.sin(a0);
    const path = document.createElementNS(SVGNS, "path");
    path.setAttribute("d",
      `M${x0} ${y0} A${r} ${r} 0 ${big} 1 ${x1} ${y1} L${xi1} ${yi1} A${rin} ${rin} 0 ${big} 0 ${xi0} ${yi0} Z`);
    path.setAttribute("fill", p.color);
    path.setAttribute("class", "pie-slice");
    path.dataset.node = p.id;
    const pct = Math.round(frac * 100);
    const title = document.createElementNS(SVGNS, "title");
    title.textContent = `${p.label} — ${fmtMs(p.ms)} (${pct}%)`;
    path.append(title);
    // hover cross-highlights the waterfall + pie legend
    path.addEventListener("mouseenter", () => highlightNode(p.id, true));
    path.addEventListener("mouseleave", () => highlightNode(p.id, false));
    svg.append(path);
    a0 = a1;

    const row = document.createElement("div");
    row.className = "pie-leg-row";
    row.dataset.node = p.id;
    row.innerHTML = `<span class="pie-dot" style="background:${p.color}"></span>
      <span class="pie-leg-label">${escapeHtml(p.label)}</span>
      <span class="pie-leg-val">${fmtMs(p.ms)} · ${pct}%</span>`;
    row.addEventListener("mouseenter", () => highlightNode(p.id, true));
    row.addEventListener("mouseleave", () => highlightNode(p.id, false));
    legend.append(row);
  }
  const center = document.createElementNS(SVGNS, "text");
  center.setAttribute("x", cx); center.setAttribute("y", cy + 3);
  center.setAttribute("class", "pie-center");
  center.textContent = parts.length;
  svg.append(center);
}

function highlightNode(id, on) {
  document.querySelectorAll(`.pie-slice[data-node="${CSS.escape(id)}"]`).forEach((el) => el.classList.toggle("hot", on));
  document.querySelectorAll(`.pie-leg-row[data-node="${CSS.escape(id)}"]`).forEach((el) => el.classList.toggle("hot", on));
  document.querySelectorAll(`.wf-row[data-node="${CSS.escape(id)}"]`).forEach((el) => el.classList.toggle("hot", on));
  traceGraph.querySelectorAll(`.trace-node[data-node="${CSS.escape(id)}"]`).forEach((el) => el.classList.toggle("hot", on));
}

const traceNative = document.getElementById("trace-native");

function renderRecords(flow) {
  traceRecords.replaceChildren();
  const byId = Object.fromEntries(flow.nodes.map((n) => [n.id, n]));
  const showNative = traceNative.checked;
  let lastNarr = null;   // for collapsing repeated native log lines
  for (const r of flow.records) {
    const n = r.node ? byId[r.node] : null;
    // Native line = the engine's framework/SDK log text (no structured payload
    // we emit). Hidden unless the toggle is on; when shown it's click-to-expand.
    if (!n) {
      if (!showNative) continue;
      const msg = r.message;
      if (!msg) continue;
      if (lastNarr && lastNarr.msg === msg) {
        lastNarr.count += 1;
        lastNarr.badge.textContent = `×${lastNarr.count}`;
        lastNarr.badge.hidden = false;
        continue;
      }
      const det = document.createElement("details");
      det.className = "trace-rec rec-native";
      const sum = document.createElement("summary");
      const badge = document.createElement("span");
      badge.className = "rec-narr-count";
      badge.hidden = true;
      sum.innerHTML = `<span class="rec-dot dark"></span><span class="rec-log">${escapeHtml(r.log || "")}</span>
        <span class="rec-native-msg">${escapeHtml(msg)}</span>`;
      sum.append(badge);
      det.append(sum);
      const pre = document.createElement("pre");
      pre.className = "rec-json";
      pre.textContent = msg;
      det.append(pre);
      traceRecords.append(det);
      lastNarr = { msg, count: 1, badge };
      continue;
    }
    lastNarr = null;
    // our structured record
    const det = document.createElement("details");
    det.className = "trace-rec";
    const sum = document.createElement("summary");
    const dot = `<span class="rec-dot" style="background:${n.color}"></span>`;
    const lat = r.latency_ms != null ? `<span class="rec-lat">${fmtMs(r.latency_ms)}</span>` : "";
    sum.innerHTML = `${dot}<span class="rec-name">${escapeHtml(n.label)}</span>
      <span class="rec-log">${escapeHtml(r.log || "")}</span>${lat}`;
    det.append(sum);
    const pre = document.createElement("pre");
    pre.className = "rec-json";
    pre.textContent = JSON.stringify(r.payload, null, 2);
    det.append(pre);
    traceRecords.append(det);
  }
}

// toggling native logs just re-renders the records from the loaded trace
traceNative.addEventListener("change", () => { if (anim.flow) renderRecords(anim.flow); });

// ── Animation: proportional replay with transport controls ───────────────────
function buildAnimation(flow) {
  // A full ROUND TRIP: descend through the hops in request order (browser down
  // to the deepest call), then ASCEND back up and land on the browser — because
  // responses return inside-out and the BFF completes last. Per-segment weight
  // ∝ the arriving hop's latency, so it dwells on slow hops both ways.
  const pos = anim.pos;
  const descent = [];
  if (pos.browser) descent.push({ x: pos.browser.x, y: pos.browser.y, node: "browser", lat: 0 });
  for (const s of flow.spans) {
    const p = pos[s.node];
    if (p) descent.push({ x: p.x, y: p.y, node: s.node, lat: s.latency_ms });
  }
  // ascent retraces back up to the browser (excludes the deepest node — we're
  // already there — and the browser is re-added as the final landing)
  const ascent = descent.slice(1, -1).reverse();
  const pts = descent.concat(ascent, descent.length ? [descent[0]] : []);
  const UNIT = 120; // ms of virtual weight for a latency-less hop
  const steps = [];
  let cum = 0;
  for (let i = 1; i < pts.length; i++) {
    const w = Math.max(pts[i].lat || 0, UNIT);
    steps.push({ from: pts[i - 1], to: pts[i], w, cumStart: cum });
    cum += w;
  }
  // trajectory line = the descent path (the ascent retraces the same line)
  traceGraph.querySelectorAll(".trace-trajectory").forEach((el) => el.remove());
  if (descent.length > 1) {
    const poly = svgEl("polyline", {
      class: "trace-trajectory",
      points: descent.map((p) => `${p.x},${p.y}`).join(" "),
    });
    traceGraph.insertBefore(poly, anim.packet);
  }
  anim.steps = steps;
  anim.total = cum || 1;
  anim.wall = 0;
  anim.flow = flow;
  updateScrub(0);
  paintProgress(0);
  setPlay(false);
  // auto-play once a trace opens (presenter can pause/scrub)
  if (steps.length) startAnim();
}

// TARGET wall-clock (ms) for a full 1× replay (the whole round trip) — slow
// hops still take proportionally longer.
const T_TARGET = 6000;

function startAnim() {
  if (!anim.steps.length) return;
  setPlay(true);
  let last = performance.now();
  const tick = (now) => {
    if (!anim.playing) return;
    const dt = now - last; last = now;
    anim.wall += (dt / T_TARGET) * anim.speed; // wall is normalized 0..1
    if (anim.wall >= 1) { anim.wall = 1; paintProgress(1); updateScrub(1); setPlay(false); return; }
    paintProgress(anim.wall);
    updateScrub(anim.wall);
    anim.raf = requestAnimationFrame(tick);
  };
  anim.raf = requestAnimationFrame(tick);
}

function stopAnim() {
  anim.playing = false;
  if (anim.raf) cancelAnimationFrame(anim.raf);
  anim.raf = 0;
}

function setPlay(on) {
  anim.playing = on;
  tracePlay.textContent = on ? "❚❚ Pause" : "▶ Play";
  if (!on && anim.raf) { cancelAnimationFrame(anim.raf); anim.raf = 0; }
}

function updateScrub(p) { traceScrub.value = String(Math.round(p * 1000)); }

// paint the packet + node lighting + clock for normalized progress p (0..1),
// where p maps linearly onto the VIRTUAL (latency-weighted) timeline.
function paintProgress(p) {
  const steps = anim.steps;
  if (!steps.length || !anim.packet) return;
  const target = p * anim.total; // virtual position
  let seg = steps[steps.length - 1], segP = 1;
  for (const st of steps) {
    if (target <= st.cumStart + st.w) {
      seg = st; segP = st.w ? (target - st.cumStart) / st.w : 1; break;
    }
  }
  segP = Math.max(0, Math.min(1, segP));
  anim.packet.setAttribute("cx", seg.from.x + (seg.to.x - seg.from.x) * segP);
  anim.packet.setAttribute("cy", seg.from.y + (seg.to.y - seg.from.y) * segP);
  // light every node the packet has reached (arrived = segP past its start)
  const reached = new Set(["browser"]);
  for (const st of steps) {
    if (target >= st.cumStart + st.w * 0.5) reached.add(st.to.node);
  }
  traceGraph.querySelectorAll(".trace-node.hit").forEach((g) => {
    g.classList.toggle("active", reached.has(g.dataset.node));
  });
  traceWaterfall.querySelectorAll(".wf-row").forEach((r) => {
    r.classList.toggle("active", reached.has(r.dataset.node));
  });
  const totalMs = anim.flow?.summary?.total_ms;
  traceClock.textContent = totalMs != null ? fmtMs(p * totalMs) : `${Math.round(target)} u`;
}

tracePlay.addEventListener("click", () => {
  if (anim.playing) setPlay(false);
  else { if (anim.wall >= 1) anim.wall = 0; startAnim(); }
});
traceReplay.addEventListener("click", () => { stopAnim(); anim.wall = 0; startAnim(); });
traceScrub.addEventListener("input", () => {
  setPlay(false);
  anim.wall = Number(traceScrub.value) / 1000;
  paintProgress(anim.wall);
});
document.querySelectorAll(".trace-speed").forEach((b) => {
  b.addEventListener("click", () => {
    anim.speed = Number(b.dataset.speed);
    document.querySelectorAll(".trace-speed").forEach((x) => x.classList.toggle("active", x === b));
  });
});
document.getElementById("trace-refresh").addEventListener("click", loadTraces);
traceUserSel.addEventListener("change", loadTraces);
traceViewSel.addEventListener("change", loadTraces);

// ── Sessions navigator (admin view — collapsible dashboard) ──────────────────
// A tree user → session → trace → specialist sub-session, enriched with EAGER
// counts/metrics at every level (interactions, sub-sessions, tokens, time) plus
// a top summary strip. Read-only on /api/admin/sessions. Clicking a trace jumps
// to the Trace view.
const sessTreeEl = document.getElementById("sess-tree");
const sessUserSel = document.getElementById("sess-user");
const sessCountEl = document.getElementById("sess-count");
const sessSummaryEl = document.getElementById("sess-summary");
const AGENT_PURPLE = "#7c3aed";

function goToTrace(traceId) {
  switchView("trace");
  openTrace(traceId);
}

function fmtTok(n) {
  if (n == null) return "—";
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

function metaBadge(k, v) {
  return `<span class="mb"><span class="mb-k">${k}</span><span class="mb-v">${escapeHtml(String(v))}</span></span>`;
}

function agentChips(agents) {
  return (agents || []).map((a) => `<span class="agent-chip">${escapeHtml(a)}</span>`).join("");
}

function modelChips(models) {
  return (models || []).map((m) => `<span class="model-chip">${escapeHtml(m)}</span>`).join("");
}

const SPINNER = (label) => `<div class="loading-row"><span class="spinner"></span>${escapeHtml(label)}</div>`;

async function loadSessionTree() {
  sessTreeEl.innerHTML = SPINNER("loading sessions…");
  sessSummaryEl.replaceChildren();
  const params = new URLSearchParams();
  if (sessUserSel.value) params.set("user", sessUserSel.value);
  try {
    const data = await adminApi(`/api/admin/sessions?${params}`);
    renderSessionTree(data.users || [], data.summary || {});
  } catch (err) {
    sessTreeEl.textContent = "";
    sessCountEl.textContent = `failed: ${err.message}`;
  }
}

function renderSessionTree(users, summary) {
  const seen = new Set([...sessUserSel.options].map((o) => o.value));
  for (const u of users) {
    if (u.user && u.user !== "unknown" && !seen.has(u.user)) {
      seen.add(u.user);
      sessUserSel.append(new Option(u.user, u.user));
    }
  }
  sessCountEl.textContent = `${users.length} user${users.length === 1 ? "" : "s"}`;
  sessSummaryEl.innerHTML = users.length
    ? metaBadge("users", summary.users) + metaBadge("sessions", summary.sessions)
      + metaBadge("interactions", summary.interactions) + metaBadge("sub-sessions", summary.sub_sessions)
      + metaBadge("total time", fmtMs(summary.total_ms))
    : "";
  sessTreeEl.replaceChildren();
  if (!users.length) {
    sessTreeEl.innerHTML = `<div class="trace-list-empty">No sessions yet — run a turn in the Direct or Agent view.</div>`;
    return;
  }
  for (const u of users) sessTreeEl.append(userNode(u));
}

// Generic expandable node: a header row (caret + label) toggling a lazily-built
// child container. buildChildren may be async (for the per-trace sub fetch).
function treeNode(cls, labelHtml, buildChildren, { labelClick } = {}) {
  const wrap = document.createElement("div");
  wrap.className = `tree-node ${cls}`;
  const head = document.createElement("div");
  head.className = "tree-row";
  const caret = document.createElement("span");
  caret.className = "tree-caret";
  caret.textContent = buildChildren ? "▸" : "";
  const label = document.createElement("div");
  label.className = "tree-label";
  label.innerHTML = labelHtml;
  head.append(caret, label);
  const kids = document.createElement("div");
  kids.className = "tree-kids";
  kids.hidden = true;
  let built = false;
  const toggle = async () => {
    if (!buildChildren) return;
    if (kids.hidden && !built) {
      built = true;
      caret.textContent = "…";
      try {
        for (const c of await buildChildren()) kids.append(c);
      } catch (err) {
        const d = document.createElement("div");
        d.className = "tree-row tree-empty";
        d.textContent = `failed: ${err.message}`;
        kids.append(d);
      }
    }
    kids.hidden = !kids.hidden;
    caret.textContent = kids.hidden ? "▸" : "▾";
  };
  caret.addEventListener("click", (e) => { e.stopPropagation(); toggle(); });
  label.addEventListener("click", labelClick || toggle);
  wrap.append(head, kids);
  return wrap;
}

function userNode(u) {
  const label = `<span class="tree-icon">👤</span><span class="tree-name">${escapeHtml(u.user)}</span>
    <span class="tree-badges">${metaBadge("sessions", u.session_count)}${metaBadge("interactions", u.trace_count)}${metaBadge("sub-sessions", u.sub_session_count)}${metaBadge("tokens", fmtTok(u.tokens))}${metaBadge("time", fmtMs(u.total_ms))}</span>`;
  return treeNode("tree-user", label, () => u.sessions.map(sessionNode));
}

function sessionNode(s) {
  const id = s.session_id || "(no session — direct)";
  const chips = agentChips(s.agents) + modelChips(s.models);
  const label = `<span class="tree-icon">🗂</span><span class="tree-sid" title="${escapeHtml(id)}">${escapeHtml(id)}</span>
    <span class="chips-wrap">${chips}</span>
    <span class="tree-badges">${metaBadge("interactions", s.count)}${metaBadge("sub-sessions", s.sub_session_count)}${metaBadge("tokens", fmtTok(s.tokens))}${metaBadge("time", fmtMs(s.total_ms))}</span>`;
  const maxMs = Math.max(1, ...s.traces.map((t) => t.total_ms || 0));
  return treeNode("tree-session", label, () => s.traces.map((t) => traceNode(t, maxMs)));
}

function traceNode(t, maxMs) {
  const pct = Math.max(6, Math.round(((t.total_ms || 0) / maxMs) * 100));
  const preview = t.preview || `${t.trace_id.slice(0, 12)}…`;
  const label = `
    <span class="tl-bar"><span class="tl-fill" style="width:${pct}%"></span></span>
    <span class="tree-lat">${fmtMs(t.total_ms)}</span>
    <span class="trace-row-view trace-view-${t.view || "direct"}">${t.view || "?"}</span>
    <span class="tl-preview">${escapeHtml(preview)}</span>
    <span class="model-chips">${modelChips(t.models)}</span>
    <span class="tree-badges">${metaBadge("subs", t.sub_session_count)}${metaBadge("tok", fmtTok(t.tokens))}</span>
    <span class="tree-open">open ↗</span>`;
  // sub-sessions are already in the payload; the caret expands them, the label
  // navigates to the Trace view. No caret when the turn delegated to nobody.
  const hasSubs = (t.sub_sessions || []).length > 0;
  return treeNode("tree-trace", label,
    hasSubs ? () => t.sub_sessions.map(subRow) : null,
    { labelClick: () => goToTrace(t.trace_id) });
}

function subRow(s) {
  const d = document.createElement("div");
  d.className = "tree-row tree-sub";
  d.innerHTML = `<span class="tree-caret"></span><span class="tree-label">
    <span class="rec-dot" style="background:${AGENT_PURPLE}"></span>
    <span class="tree-sub-agent">${escapeHtml(s.agent)}_agent</span>
    <span class="tree-tid" title="${escapeHtml(s.session)}">${escapeHtml(s.session)}</span>
    <span class="tree-badges">${metaBadge("calls", s.model_calls)}${metaBadge("tok", fmtTok(s.tokens))}</span></span>`;
  return d;
}

document.getElementById("sess-refresh").addEventListener("click", loadSessionTree);
sessUserSel.addEventListener("change", loadSessionTree);

async function initMe() {
  try {
    const me = await (await fetch("/api/me")).json();
    if (me.is_admin) {
      adminTab.hidden = false;
      document.getElementById("trace-tab").hidden = false;
      document.getElementById("sessions-tab").hidden = false;
    }
  } catch (err) {
    console.warn("Failed to load /api/me:", err);
  }
}

// Personal LLM API key: persisted locally, sent per request. The gateway
// binds it to the IAP email (403 on mismatch) and meters per-key llmQuota.
llmKeyInput.value = localStorage.getItem("llm_api_key") || "";
llmKeyInput.addEventListener("change", () => {
  localStorage.setItem("llm_api_key", llmKeyInput.value.trim());
});

// ── Init ─────────────────────────────────────────────────────────────────────
loadConfig();
initMe();
applyStats(VIEWS.direct);
refreshEmptyState();
autoGrow();
