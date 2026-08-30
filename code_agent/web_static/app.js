"use strict";

const state = {
  socket: null,
  sessionId: null,
  running: false,
  eventCount: 0,
  tools: new Map(),
  approvals: new Map(),
  assistantStream: null,
};

const elements = {
  activity: document.querySelector("#activity"),
  welcome: document.querySelector("#welcome"),
  composer: document.querySelector("#composer"),
  taskInput: document.querySelector("#taskInput"),
  sendButton: document.querySelector("#sendButton"),
  stopButton: document.querySelector("#stopButton"),
  runState: document.querySelector("#runState"),
  workspacePath: document.querySelector("#workspacePath"),
  modelName: document.querySelector("#modelName"),
  sandboxName: document.querySelector("#sandboxName"),
  sessionTitle: document.querySelector("#sessionTitle"),
  branchName: document.querySelector("#branchName"),
  sessionList: document.querySelector("#sessionList"),
  eventCount: document.querySelector("#eventCount"),
  connectionState: document.querySelector("#connectionState"),
  connectionDot: document.querySelector("#connectionDot"),
  contextPercent: document.querySelector("#contextPercent"),
  contextUsed: document.querySelector("#contextUsed"),
  contextBar: document.querySelector("#contextBar"),
  contextLayers: document.querySelector("#contextLayers"),
  compactCurrent: document.querySelector("#compactCurrent"),
  compactFill: document.querySelector("#compactFill"),
  modifiedCount: document.querySelector("#modifiedCount"),
  verifiedCount: document.querySelector("#verifiedCount"),
  errorCount: document.querySelector("#errorCount"),
  pendingApproval: document.querySelector("#pendingApproval"),
  gitStatus: document.querySelector("#gitStatus"),
  gitDetails: document.querySelector("#gitDetails"),
  recallInput: document.querySelector("#recallInput"),
  recallDialog: document.querySelector("#recallDialog"),
  recallContent: document.querySelector("#recallContent"),
  sessionsRail: document.querySelector("#sessionsRail"),
  inspector: document.querySelector("#inspector"),
};

const toolIcons = {
  search_text: "search",
  read_file: "file",
  replace_in_file: "pencil",
  write_file: "filePlus",
  run_command: "terminal",
  list_files: "list",
  fetch_url: "arrow",
  git_status: "git",
  git_diff: "diff",
  git_log: "history",
  git_commit: "commit",
  git_push: "upload",
};

const iconPaths = {
  search: ["M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14", "M16.2 16.2 21 21"],
  file: ["M6 2h8l4 4v16H6z", "M14 2v6h6"],
  filePlus: ["M6 2h8l4 4v16H6z", "M14 2v6h6", "M9 14h6", "M12 11v6"],
  pencil: ["M4 20l4.2-1 11-11-3.2-3.2-11 11z", "M14.5 6.3l3.2 3.2"],
  terminal: ["M5 7l5 5-5 5", "M12 17h7"],
  list: ["M8 6h11", "M8 12h11", "M8 18h11", "M4 6h.01", "M4 12h.01", "M4 18h.01"],
  arrow: ["M7 17 17 7", "M9 7h8v8"],
  git: ["M6 3v14", "M6 11c0-3 3-5 7-5h3", "M6 21a2 2 0 1 0 0-4 2 2 0 0 0 0 4", "M18 8a2 2 0 1 0 0-4 2 2 0 0 0 0 4"],
  diff: ["M7 4v16", "M4 7l3-3 3 3", "M17 20V4", "M14 17l3 3 3-3"],
  history: ["M4 5v5h5", "M5.2 10a8 8 0 1 0 2-5", "M12 7v5l3 2"],
  commit: ["M3 12h6", "M15 12h6", "M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8"],
  upload: ["M12 17V4", "M7 9l5-5 5 5", "M5 20h14"],
  chevron: ["M7 9l5 5 5-5"],
  flag: ["M5 21V4", "M5 5h11l-2 4 2 4H5"],
  user: ["M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8", "M4.5 21a7.5 7.5 0 0 1 15 0"],
};

function createSvgIcon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  for (const data of iconPaths[name] || ["M5 12h14"]) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", data);
    svg.append(path);
  }
  return svg;
}

function websocketUrl() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const params = new URLSearchParams(location.search);
  const sessionId = params.get("session");
  const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
  return `${protocol}//${location.host}/ws${query}`;
}

function connect() {
  const socket = new WebSocket(websocketUrl());
  state.socket = socket;
  setConnection("连接中", false);

  socket.addEventListener("open", () => setConnection("已连接", true));
  socket.addEventListener("close", () => {
    setConnection("已断开", false);
    setRunning(false, "连接已断开", "error");
  });
  socket.addEventListener("error", () => showNotice("WebSocket 连接失败", "error"));
  socket.addEventListener("message", (message) => {
    let data;
    try {
      data = JSON.parse(message.data);
    } catch (_error) {
      showNotice("收到无法解析的服务端消息", "error");
      return;
    }
    handleMessage(data);
  });
}

function handleMessage(message) {
  if (message.type === "ready") {
    state.sessionId = message.session_id;
    elements.workspacePath.textContent = message.workspace;
    elements.workspacePath.title = message.workspace;
    elements.modelName.textContent = message.model;
    elements.sandboxName.textContent = message.sandbox;
    elements.branchName.textContent = message.branch || "—";
    for (const event of message.history || []) renderEvent(event, true);
    updateContext(message.context || {});
    loadSessions();
    return;
  }
  if (message.type === "event") {
    renderEvent(message.event, false);
    return;
  }
  if (message.type === "snapshot") {
    updateContext(message.context || {});
    return;
  }
  if (message.type === "recalled") {
    showRecalledEvent(message);
    return;
  }
  if (message.type === "protocol_error") {
    showNotice(message.message || "请求被拒绝", "error");
  }
}

function renderEvent(event, replay) {
  state.eventCount += 1;
  elements.eventCount.textContent = String(state.eventCount);
  const type = event.event_type;
  const payload = event.payload || {};

  if (type === "user.message") {
    hideWelcome();
    appendConversation("用户", payload.text || "", event.timestamp, false);
    if (!replay && payload.text) {
      elements.sessionTitle.textContent = payload.text.slice(0, 42);
    }
  } else if (type === "assistant.delta") {
    if (!replay) appendAssistantDelta(payload.text || "", event.timestamp);
  } else if (type === "assistant.message") {
    finalizeAssistant(payload.text || "", event.timestamp, replay);
  } else if (type === "tool.requested") {
    hideWelcome();
    createToolStep(payload, event.timestamp);
  } else if (type === "tool.started") {
    updateToolStep(payload.call_id, "running", payload);
  } else if (type === "tool.completed") {
    updateToolStep(payload.call_id, "completed", payload, event.event_id);
  } else if (type === "tool.failed") {
    updateToolStep(payload.call_id, "failed", payload, event.event_id);
  } else if (type === "tool.approval_required") {
    renderApproval(payload);
  } else if (type === "tool.approval_decided") {
    resolveApproval(payload);
  } else if (type === "context.usage") {
    updateUsage(payload.used_tokens || 0, payload.limit_tokens || 0);
  } else if (type === "context.compaction_started") {
    showNotice("正在整理上下文…", "info");
  } else if (type === "context.compaction_completed") {
    const before = payload.before_tokens || 0;
    const after = payload.after_tokens || 0;
    showNotice(`上下文已整理：${before.toLocaleString()} → ${after.toLocaleString()}`, "success");
  } else if (type === "context.cleared") {
    showNotice("模型上下文已清除，审计日志仍然保留", "success");
  } else if (type === "error") {
    appendError(payload.message || "未知错误", event.timestamp);
    setRunning(false, "发生错误", "error");
  } else if (type === "turn.completed") {
    const reason = payload.reason || "stop";
    const label = reason === "user_cancelled" ? "已停止" : reason === "model_error" || reason === "web_error" ? "执行失败" : "任务完成";
    setRunning(false, label, reason.includes("error") ? "error" : "idle");
    loadSessions();
  }
  scrollActivity();
}

function hideWelcome() {
  if (elements.welcome) elements.welcome.hidden = true;
}

function appendConversation(actor, text, timestamp, robot) {
  const block = document.createElement("article");
  block.className = "event-block";
  const head = document.createElement("div");
  head.className = "event-head";
  const mark = document.createElement("span");
  mark.className = `actor-mark${robot ? " robot" : ""}`;
  if (robot) {
    const image = document.createElement("img");
    image.src = "/static/mascot.png";
    image.alt = "";
    mark.append(image);
  } else {
    mark.append(createSvgIcon("user"));
  }
  const name = document.createElement("strong");
  name.className = "actor-name";
  name.textContent = actor;
  const time = document.createElement("time");
  time.className = "event-time";
  time.textContent = formatTime(timestamp);
  head.append(mark, name, time);
  const copy = document.createElement("p");
  copy.className = "event-copy";
  copy.textContent = text;
  block.append(head, copy);
  elements.activity.append(block);
  return { block, copy };
}

function appendAssistantDelta(text, timestamp) {
  if (!state.assistantStream) {
    state.assistantStream = appendConversation("LcTiCodeAgent", "", timestamp, true);
  }
  state.assistantStream.copy.textContent += text;
}

function finalizeAssistant(text, timestamp, replay) {
  if (state.assistantStream && !replay) {
    if (!state.assistantStream.copy.textContent && text) {
      state.assistantStream.copy.textContent = text;
    }
    state.assistantStream = null;
    return;
  }
  if (text) appendConversation("LcTiCodeAgent", text, timestamp, true);
}

function createToolStep(payload, timestamp) {
  const callId = String(payload.call_id || crypto.randomUUID());
  const name = String(payload.name || "tool");
  const step = document.createElement("article");
  step.className = "tool-step";
  step.dataset.status = "requested";
  step.dataset.callId = callId;
  const summary = document.createElement("div");
  summary.className = "tool-summary";
  summary.tabIndex = 0;
  summary.setAttribute("role", "button");
  summary.setAttribute("aria-expanded", "false");

  const icon = document.createElement("span");
  icon.className = "tool-icon";
  icon.append(createSvgIcon(toolIcons[name] || "list"));
  const toolName = document.createElement("strong");
  toolName.className = "tool-name";
  toolName.textContent = name;
  const description = document.createElement("span");
  description.className = "tool-description";
  description.textContent = describeTool(name, payload.arguments || {});
  const time = document.createElement("time");
  time.className = "event-time";
  time.textContent = formatTime(timestamp);
  const duration = document.createElement("span");
  duration.className = "tool-duration";
  duration.textContent = "等待";
  const toolState = document.createElement("span");
  toolState.className = "tool-state";
  toolState.textContent = "·";
  const disclosure = document.createElement("span");
  disclosure.className = "tool-disclosure";
  disclosure.append(createSvgIcon("chevron"));
  const detail = document.createElement("pre");
  detail.className = "tool-detail";
  detail.textContent = JSON.stringify(payload.arguments || {}, null, 2);

  summary.append(icon, toolName, description, time, duration, toolState, disclosure);
  summary.addEventListener("click", () => toggleTool(step, summary));
  summary.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      toggleTool(step, summary);
    }
  });
  step.append(summary, detail);
  elements.activity.append(step);
  state.tools.set(callId, { step, duration, toolState, detail, description });
}

function updateToolStep(callId, status, payload, eventId) {
  const tool = state.tools.get(String(callId));
  if (!tool) return;
  tool.step.dataset.status = status;
  tool.toolState.textContent = status === "completed" ? "✓" : status === "failed" ? "!" : "…";
  const duration = payload.duration_ms;
  tool.duration.textContent = Number.isFinite(duration) ? `${duration}ms` : status === "running" ? "运行中" : "完成";
  const detailText = payload.content || payload.error || payload.summary;
  if (detailText) tool.detail.textContent = String(detailText);
  if (payload.summary) tool.description.textContent = String(payload.summary).slice(0, 180);
  if (eventId) {
    tool.detail.dataset.eventId = eventId;
    tool.detail.title = `event_id: ${eventId}`;
  }
}

function toggleTool(step, summary) {
  const expanded = step.classList.toggle("expanded");
  summary.setAttribute("aria-expanded", String(expanded));
}

function describeTool(name, args) {
  if (name === "read_file") return `读取 ${args.path || "文件"}`;
  if (name === "search_text") return `搜索 “${args.query || ""}”`;
  if (name === "replace_in_file") return `修改 ${args.path || "文件"}`;
  if (name === "write_file") return `创建 ${args.path || "文件"}`;
  if (name === "run_command") return Array.isArray(args.argv) ? args.argv.join(" ") : "运行验证命令";
  if (name.startsWith("git_")) return "检查或更新 Git 状态";
  if (name === "fetch_url") return `访问 ${args.url || "HTTPS 地址"}`;
  return JSON.stringify(args).slice(0, 120);
}

function renderApproval(payload) {
  hideWelcome();
  const requestId = String(payload.request_id || "");
  const card = document.createElement("article");
  card.className = "approval-card";
  card.dataset.requestId = requestId;
  const heading = document.createElement("div");
  heading.className = "approval-heading";
  const flag = document.createElement("span");
  flag.className = "approval-flag";
  flag.append(createSvgIcon("flag"));
  const title = document.createElement("h2");
  title.textContent = "需要一次性批准";
  const chip = document.createElement("span");
  chip.className = "tool-chip";
  chip.textContent = payload.name || "tool";
  heading.append(flag, title, chip);
  const explanation = document.createElement("p");
  explanation.textContent = approvalExplanation(payload.name, payload.reason);
  const ledger = buildApprovalLedger(payload);
  const actions = document.createElement("div");
  actions.className = "approval-actions";
  const note = document.createElement("span");
  note.className = "approval-note";
  note.textContent = "审批期间状态变化将使本次请求失效";
  const deny = document.createElement("button");
  deny.className = "deny-button";
  deny.type = "button";
  deny.textContent = "拒绝";
  const approve = document.createElement("button");
  approve.className = "approve-button";
  approve.type = "button";
  approve.textContent = "允许本次";
  deny.addEventListener("click", () => submitApproval(requestId, false));
  approve.addEventListener("click", () => submitApproval(requestId, true));
  actions.append(note, deny, approve);
  card.append(heading, explanation, ledger, actions);
  elements.activity.append(card);
  state.approvals.set(requestId, { card, deny, approve, actions });
  elements.pendingApproval.textContent = String(state.approvals.size);
  setRunning(true, "等待一次性批准", "approval");
  selectTab(payload.name && payload.name.startsWith("git_") ? "git" : "security");
  if (payload.name && payload.name.startsWith("git_")) renderGitContext(payload.context || {});
  approve.focus();
}

function approvalExplanation(name, reason) {
  if (name === "git_push") return "此操作会将本地提交发送到公开远端。批准仅对本次请求有效。";
  if (name === "git_commit") return "此操作会改变本地仓库历史。批准仅对本次请求有效。";
  if (name === "fetch_url") return "此操作会向工作区外发送 HTTPS 请求。批准仅对本次请求有效。";
  return reason || "此操作需要你的单次批准。";
}

function buildApprovalLedger(payload) {
  const ledger = document.createElement("dl");
  ledger.className = "approval-ledger";
  const context = payload.context || {};
  const rows = payload.name === "git_push"
    ? [
        ["Remote", context.remote],
        ["Destination", remoteDestination(context.remote_url)],
        ["Branch", context.branch ? `${context.branch} → ${context.branch}` : null],
        ["Commits", context.commit_count],
        ["HEAD", shortHash(context.head)],
        ["Changed files", Array.isArray(context.changed_files) ? context.changed_files.length : null],
        ["Secret scan", context.secret_scan],
        ["Force", context.force === false ? "Disabled" : context.force],
      ]
    : Object.entries(context).slice(0, 8);
  for (const [label, value] of rows) {
    if (value === undefined || value === null || value === "") continue;
    const row = document.createElement("div");
    const term = document.createElement("dt");
    term.textContent = String(label);
    const description = document.createElement("dd");
    description.textContent = String(value);
    row.append(term, description);
    ledger.append(row);
  }
  return ledger;
}

function submitApproval(requestId, approved) {
  send({ type: "approval", request_id: requestId, approved });
  const approval = state.approvals.get(requestId);
  if (!approval) return;
  approval.deny.disabled = true;
  approval.approve.disabled = true;
  approval.approve.textContent = approved ? "已允许" : "已拒绝";
}

function resolveApproval(payload) {
  const requestId = String(payload.request_id || "");
  const approval = state.approvals.get(requestId);
  if (!approval) return;
  const result = document.createElement("strong");
  result.textContent = payload.approved ? "已允许本次操作" : "已拒绝本次操作";
  result.style.color = payload.approved ? "#238346" : "#a93e35";
  approval.actions.replaceChildren(result);
  state.approvals.delete(requestId);
  elements.pendingApproval.textContent = String(state.approvals.size);
  setRunning(true, "继续执行", "running");
}

function renderGitContext(context) {
  elements.gitStatus.textContent = "Preflight ready";
  elements.gitDetails.replaceChildren();
  const remote = gitSection("远端", context.remote_url || context.remote || "—");
  const branch = gitSection("当前分支", context.branch || "—");
  const commits = gitSection("即将推送的提交", Array.isArray(context.commits) ? context.commits.join("\n") : shortHash(context.head));
  elements.gitDetails.append(remote, branch, commits);
  if (Array.isArray(context.changed_files)) {
    const section = document.createElement("section");
    section.className = "git-section";
    const title = document.createElement("h3");
    title.textContent = `变更文件 (${context.changed_files.length})`;
    const list = document.createElement("ul");
    list.className = "git-file-list";
    for (const file of context.changed_files) {
      const item = document.createElement("li");
      item.textContent = file;
      list.append(item);
    }
    section.append(title, list);
    elements.gitDetails.append(section);
  }
  const ledger = document.createElement("dl");
  ledger.className = "git-ledger git-section";
  for (const [label, value] of [
    ["Working tree", context.working_tree_dirty ? "Dirty" : "Clean"],
    ["Secret scan", context.secret_scan || "—"],
    ["Force", context.force === false ? "Disabled" : context.force],
  ]) {
    const row = document.createElement("div");
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    description.textContent = String(value);
    row.append(term, description);
    ledger.append(row);
  }
  elements.gitDetails.append(ledger);
}

function gitSection(titleText, value) {
  const section = document.createElement("section");
  section.className = "git-section";
  const title = document.createElement("h3");
  title.textContent = titleText;
  const code = document.createElement("code");
  code.textContent = value || "—";
  section.append(title, code);
  return section;
}

function updateContext(context) {
  const limit = Number(context.limit_tokens || 32000);
  const used = Number(context.used_tokens || context.estimated_tokens || 0);
  updateUsage(used, limit);
  const layers = context.layers || {};
  const memory = context.working_memory || {};
  const layerData = [
    ["Pinned", "pinned", Number(layers.pinned?.estimated_tokens || 0), `${layers.pinned?.items || 0} items`],
    ["Working memory", "working", 0, `${(memory.modified_files || 0) + (memory.verified_commands || 0) + (memory.open_errors || 0)} facts`],
    ["Recent raw", "recent", Number(layers.recent?.estimated_tokens || 0), `${layers.recent?.items || 0} items`],
    ["Recoverable evidence", "evidence", Number(layers.evidence?.estimated_tokens || 0), `${layers.evidence?.items || 0} items`],
  ];
  elements.contextLayers.replaceChildren();
  elements.contextBar.replaceChildren();
  for (const [label, key, tokens, meta] of layerData) {
    const row = document.createElement("div");
    row.className = "layer-row";
    const tab = document.createElement("span");
    tab.className = `layer-tab ${key}`;
    const name = document.createElement("span");
    name.className = "layer-label";
    name.textContent = label;
    const details = document.createElement("span");
    details.className = "layer-meta";
    details.textContent = tokens ? `${tokens.toLocaleString()} · ${meta}` : meta;
    row.append(tab, name, details);
    elements.contextLayers.append(row);
    if (tokens > 0) {
      const segment = document.createElement("span");
      segment.className = `context-segment ${key}`;
      segment.style.width = `${Math.max(3, (tokens / limit) * 100)}%`;
      segment.title = `${label}: ${tokens.toLocaleString()}`;
      elements.contextBar.append(segment);
    }
  }
  const free = document.createElement("span");
  free.className = "context-segment free";
  elements.contextBar.append(free);
  elements.modifiedCount.textContent = String(memory.modified_files || 0);
  elements.verifiedCount.textContent = String(memory.verified_commands || 0);
  elements.errorCount.textContent = String(memory.open_errors || 0);
}

function updateUsage(used, limit) {
  const ratio = limit ? Math.min(1, used / limit) : 0;
  const percent = Math.round(ratio * 100);
  elements.contextPercent.textContent = `${percent}%`;
  elements.contextUsed.textContent = `${Number(used).toLocaleString()} / ${Number(limit).toLocaleString()}`;
  elements.compactCurrent.textContent = `当前 ${percent}%`;
  elements.compactFill.style.width = `${percent}%`;
  const filled = Math.ceil(ratio * 6);
  document.querySelectorAll(".mini-segments i").forEach((segment, index) => {
    segment.classList.toggle("filled", index < filled);
  });
}

function appendError(message, timestamp) {
  const block = appendConversation("错误", message, timestamp, false).block;
  block.style.color = "#a93e35";
}

function showNotice(message, kind) {
  const notice = document.createElement("div");
  notice.className = "tool-step expanded";
  notice.dataset.status = kind === "error" ? "failed" : "completed";
  const detail = document.createElement("pre");
  detail.className = "tool-detail";
  detail.style.display = "block";
  detail.style.margin = "8px";
  detail.textContent = message;
  notice.append(detail);
  elements.activity.append(notice);
  scrollActivity();
}

function setRunning(running, label, mode = "running") {
  state.running = running;
  elements.stopButton.disabled = !running;
  elements.sendButton.disabled = running;
  elements.taskInput.disabled = running;
  elements.runState.dataset.state = mode;
  elements.runState.lastElementChild.textContent = label;
}

function setConnection(label, connected) {
  elements.connectionState.textContent = label;
  elements.connectionDot.style.background = connected ? "var(--mint)" : "var(--coral)";
}

function selectTab(name) {
  document.querySelectorAll(".inspector-tabs [role=tab]").forEach((tab) => {
    tab.setAttribute("aria-selected", String(tab.dataset.tab === name));
  });
  document.querySelectorAll(".inspector-pane").forEach((pane) => {
    pane.classList.toggle("active", pane.dataset.pane === name);
  });
}

async function loadSessions() {
  try {
    const response = await fetch("/api/sessions", { cache: "no-store" });
    const data = await response.json();
    renderSessions(data.sessions || []);
  } catch (_error) {
    elements.sessionList.replaceChildren();
  }
}

function renderSessions(sessions) {
  elements.sessionList.replaceChildren();
  for (const session of sessions) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `session-item${session.session_id === state.sessionId ? " active" : ""}`;
    const title = document.createElement("span");
    title.className = "session-title";
    title.textContent = session.title;
    const time = document.createElement("time");
    time.className = "session-time";
    time.textContent = relativeTime(session.updated);
    const meta = document.createElement("span");
    meta.className = "session-meta";
    meta.textContent = `${session.events} 个事件`;
    button.append(title, time, meta);
    button.addEventListener("click", () => {
      if (session.session_id === state.sessionId) return;
      location.href = `/?session=${encodeURIComponent(session.session_id)}`;
    });
    elements.sessionList.append(button);
  }
}

function showRecalledEvent(message) {
  elements.recallContent.textContent = message.event
    ? JSON.stringify(message.event, null, 2)
    : `没有找到事件 ${message.event_id}`;
  elements.recallDialog.showModal();
}

function remoteDestination(url) {
  if (!url) return "—";
  return String(url).replace(/^https?:\/\/github\.com\//, "").replace(/\.git$/, "");
}

function shortHash(hash) {
  return hash ? String(hash).slice(0, 7) : "—";
}

function formatTime(timestamp) {
  if (!timestamp) return "";
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString("zh-CN", { hour12: false });
}

function relativeTime(seconds) {
  const elapsed = Math.max(0, Date.now() / 1000 - Number(seconds || 0));
  if (elapsed < 60) return "刚刚";
  if (elapsed < 3600) return `${Math.floor(elapsed / 60)} 分钟前`;
  if (elapsed < 86400) return `${Math.floor(elapsed / 3600)} 小时前`;
  return `${Math.floor(elapsed / 86400)} 天前`;
}

function scrollActivity() {
  requestAnimationFrame(() => {
    elements.activity.scrollTop = elements.activity.scrollHeight;
  });
}

function send(message) {
  if (!state.socket || state.socket.readyState !== WebSocket.OPEN) {
    showNotice("尚未连接到本地 Agent", "error");
    return false;
  }
  state.socket.send(JSON.stringify(message));
  return true;
}

elements.composer.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = elements.taskInput.value.trim();
  if (!text || state.running) return;
  if (send({ type: "run", text })) {
    elements.taskInput.value = "";
    setRunning(true, "正在工作", "running");
  }
});

elements.taskInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    elements.composer.requestSubmit();
  }
});

elements.stopButton.addEventListener("click", () => {
  if (send({ type: "stop" })) setRunning(true, "正在停止", "running");
});
document.querySelector("#compactButton").addEventListener("click", () => send({ type: "compact" }));
document.querySelector("#clearButton").addEventListener("click", () => {
  if (confirm("清除模型上下文？追加式审计日志不会删除。")) send({ type: "clear" });
});
document.querySelector("#recallButton").addEventListener("click", () => {
  const eventId = elements.recallInput.value.trim();
  if (eventId) send({ type: "recall", event_id: eventId });
});
document.querySelector("#newSession").addEventListener("click", () => { location.href = "/"; });
document.querySelector("#sessionsToggle").addEventListener("click", () => elements.sessionsRail.classList.toggle("open"));
document.querySelector("#inspectorToggle").addEventListener("click", () => elements.inspector.classList.toggle("open"));
document.querySelectorAll(".inspector-tabs [role=tab]").forEach((tab) => {
  tab.addEventListener("click", () => selectTab(tab.dataset.tab));
});

connect();
