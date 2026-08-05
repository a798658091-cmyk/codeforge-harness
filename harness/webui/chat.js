"use strict";

const $ = (selector) => document.querySelector(selector);
const ui = {
  form: $("#taskForm"), prompt: $("#prompt"), run: $("#runButton"), cancel: $("#cancelButton"),
  error: $("#formError"), newTask: $("#newTaskButton"), currentTask: $("#currentTaskButton"),
  taskTitle: $("#currentTaskTitle"), taskMeta: $("#currentTaskMeta"), sessionTitle: $("#sessionTitle"),
  workspace: $("#workspace"), sessionId: $("#sessionId"), status: $("#statusPill"),
  stepTokens: $("#stepTokens"), totalTokens: $("#totalTokens"), toolCalls: $("#toolCalls"),
  connectionDot: $("#connectionDot"), connectionText: $("#connectionText"),
  transcript: $("#transcript"), conversation: $("#conversation"),
};
const statusLabels = {
  idle: "空闲", starting: "正在启动", running: "执行中", cancelling: "正在停止",
  cancelled: "已停止", completed: "已完成", failed: "失败",
};
const todoLabels = { pending: "待处理", in_progress: "进行中", completed: "完成" };
let state = null;
let answers = [];
const number = (value) => new Intl.NumberFormat("zh-CN").format(Number(value || 0));
const running = (status) => ["starting", "running", "cancelling"].includes(status);

function shortTitle(prompt) {
  const line = String(prompt || "").trim().split(/\r?\n/, 1)[0];
  return line ? (line.length > 32 ? `${line.slice(0, 32)}…` : line) : "新对话";
}

function currentTurn(value) {
  if (!value.prompt) return null;
  const keys = ["turn_id", "prompt", "answer", "error", "status", "started_at", "finished_at", "steps", "max_steps", "current_operation", "todos", "tokens", "tools", "compactions", "operations"];
  return Object.fromEntries(keys.map((key) => [key, value[key]]));
}

function allTurns(value) {
  const turns = [...(value.turns || [])];
  const current = currentTurn(value);
  if (current) turns.push(current);
  return turns;
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function section(label, className = "") {
  const wrapper = node("section", `turn-section ${className}`.trim());
  const rail = node("div", "turn-label", label);
  const content = node("div", "turn-content");
  wrapper.append(rail, content);
  return { wrapper, content };
}

function render(value) {
  const bottomDistance = ui.transcript.scrollHeight - ui.transcript.scrollTop - ui.transcript.clientHeight;
  state = value;
  const turns = allTurns(value);
  const current = turns.at(-1);
  const title = shortTitle(turns[0]?.prompt);
  const totals = value.totals || {};
  const step = current?.tokens || {};
  const status = value.status || "idle";
  ui.taskTitle.textContent = title;
  ui.sessionTitle.textContent = title;
  ui.taskMeta.textContent = turns.length ? `${turns.length} 轮 · ${statusLabels[status] || status}` : "等待输入";
  ui.workspace.textContent = value.workspace || "";
  ui.workspace.title = value.workspace || "";
  ui.sessionId.textContent = value.session_id || "暂无会话";
  ui.status.textContent = statusLabels[status] || status;
  ui.status.className = status;
  ui.stepTokens.textContent = number(step.step_total);
  ui.totalTokens.textContent = number((totals.prompt_tokens || 0) + (totals.completion_tokens || 0));
  ui.toolCalls.textContent = number(totals.tool_calls);
  ui.run.disabled = running(status);
  ui.cancel.disabled = !running(status) || status === "cancelling";
  ui.prompt.disabled = running(status);
  renderConversation(turns);
  if (bottomDistance < 100) requestAnimationFrame(() => { ui.transcript.scrollTop = ui.transcript.scrollHeight; });
}

function renderConversation(turns) {
  ui.conversation.replaceChildren();
  answers = [];
  if (!turns.length) {
    const welcome = node("section", "welcome");
    welcome.append(
      node("small", "", "CODEFORGE LOCAL AGENT"),
      node("h2", "", "开始一个编码任务"),
      node("p", "", "发送第一条要求；任务完成后可在底部继续追问，新的输入和回答会依次追加到同一会话。"),
    );
    ui.conversation.append(welcome);
    return;
  }
  turns.forEach((turn, index) => ui.conversation.append(renderTurn(turn, index)));
}

function renderTurn(turn, index) {
  const article = node("article", "turn");
  const user = section("YOU", "user-section");
  user.content.append(node("pre", "user-text", turn.prompt || ""));
  article.append(user.wrapper);
  if ((turn.todos || []).length) article.append(renderTodos(turn.todos));
  if (turn.current_operation) article.append(renderOperation(turn.current_operation));
  if ((turn.operations || []).length) article.append(renderTools(turn.operations));
  if (turn.answer || turn.error) article.append(renderAnswer(turn, index));
  if ((turn.tokens?.total || 0) || (turn.steps || 0) || (turn.tools?.calls || 0)) {
    article.append(renderMetrics(turn));
  }
  return article;
}

function renderTodos(todos) {
  const plan = section("PLAN");
  const heading = node("div", "section-title");
  heading.append(node("strong", "", "Todo"), node("span", "", String(todos.length)));
  const list = node("div", "todo-list");
  todos.forEach((todo) => {
    const status = todo.status || "pending";
    const item = node("div", `todo-item ${status}`);
    const check = node("span", "todo-check", status === "completed" ? "✓" : status === "in_progress" ? "·" : "");
    item.append(check, node("span", "todo-copy", todo.content || todo.title || todo.id || "未命名任务"), node("span", "todo-state", todoLabels[status] || status));
    list.append(item);
  });
  plan.content.append(heading, list);
  return plan.wrapper;
}

function renderOperation(operation) {
  const active = section("NOW");
  const content = node("div", "active-operation");
  const detail = node("div");
  const isModel = operation.tool === "model";
  detail.append(node("strong", "", isModel ? "模型正在思考" : operation.tool || "unknown"), node("p", "", isModel ? "正在决定下一步操作。" : "工具正在执行，完成后会显示结果。"));
  content.append(node("span", "operation-pulse", "●"), detail);
  active.content.append(content);
  return active.wrapper;
}

function renderTools(operations) {
  const trace = section("TRACE");
  const heading = node("div", "section-title");
  heading.append(node("strong", "", "执行过程"), node("span", "", `${operations.length} 次工具调用`));
  const list = node("div", "tool-list");
  operations.forEach((operation) => {
    const row = node("div", `tool-row ${operation.status || ""}`);
    const main = node("div", "tool-main");
    main.append(node("strong", "", operation.tool || "unknown"), node("p", "", operation.summary || operation.error_type || "已完成"));
    const date = new Date(operation.timestamp);
    const time = Number.isNaN(date.getTime()) ? `${operation.duration_ms || 0}ms` : date.toLocaleTimeString("zh-CN", { hour12: false });
    row.append(node("span", "tool-dot"), main, node("span", "tool-time", time));
    list.append(row);
  });
  trace.content.append(heading, list);
  return trace.wrapper;
}

function renderAnswer(turn, index) {
  const done = section(turn.error && !turn.answer ? "ERROR" : "ASSISTANT");
  const card = node("div", "answer-card");
  const heading = node("div", "section-title");
  const copy = node("button", "copy-answer", "复制");
  copy.type = "button";
  copy.dataset.answer = String(index);
  answers[index] = turn.answer || "";
  if (!turn.answer) copy.disabled = true;
  heading.append(node("strong", "", turn.error && !turn.answer ? "任务未完成" : "CodeForge"), copy);
  card.append(heading);
  if (turn.answer) card.append(node("pre", "answer-text", turn.answer));
  if (turn.error) card.append(node("pre", "error-text", turn.error));
  done.content.append(card);
  return done.wrapper;
}

function renderMetrics(turn) {
  const tokens = turn.tokens || {};
  const tools = turn.tools || {};
  const metrics = node("div", "turn-metrics");
  metrics.append(
    node("span", "", `本轮 Token：输入 ${number(tokens.prompt)} · 输出 ${number(tokens.completion)}`),
    node("span", "", `步骤 ${number(turn.steps)} / 上限 ${number(turn.max_steps)}`),
    node("span", "", `工具 ${number(tools.calls)} · 失败 ${number(tools.failures)}`),
  );
  if (turn.compactions) metrics.append(node("span", "", `上下文压缩 ${number(turn.compactions)} 次`));
  return metrics;
}

async function fetchState() {
  const response = await fetch("/api/state", { cache: "no-store" });
  if (!response.ok) throw new Error("无法读取运行状态");
  render(await response.json());
}

async function submit(event) {
  event.preventDefault();
  ui.error.textContent = "";
  const prompt = ui.prompt.value.trim();
  if (!prompt) return;
  try {
    const response = await fetch("/api/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt }) });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "任务启动失败");
    ui.prompt.value = "";
    await fetchState();
  } catch (error) {
    ui.error.textContent = error.message;
  }
}

async function cancel() {
  ui.error.textContent = "";
  try {
    const response = await fetch("/api/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "停止任务失败");
  } catch (error) {
    ui.error.textContent = error.message;
  }
}

async function newConversation() {
  if (running(state?.status)) return;
  ui.error.textContent = "";
  try {
    const response = await fetch("/api/new", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "无法新建对话");
    ui.prompt.value = "";
    await fetchState();
    ui.prompt.focus();
  } catch (error) {
    ui.error.textContent = error.message;
  }
}

function connectEvents() {
  const stream = new EventSource("/api/events");
  const events = ["snapshot", "conversation.started", "run.started", "run.cancelling", "run.finished", "state.updated", "model.started", "model.completed", "tool.started", "tool.completed", "tool.finished", "todos.updated", "context.compacted"];
  events.forEach((name) => stream.addEventListener(name, async (event) => {
    ui.connectionDot.classList.add("connected");
    ui.connectionText.textContent = "实时连接";
    const payload = JSON.parse(event.data);
    if (payload.workspace && payload.turns) render(payload);
    else if (payload.state) render(payload.state);
    else await fetchState();
  }));
  stream.onopen = () => { ui.connectionDot.classList.add("connected"); ui.connectionText.textContent = "实时连接"; };
  stream.onerror = () => { ui.connectionDot.classList.remove("connected"); ui.connectionText.textContent = "正在重连"; };
}

ui.form.addEventListener("submit", submit);
ui.cancel.addEventListener("click", cancel);
ui.newTask.addEventListener("click", newConversation);
ui.currentTask.addEventListener("click", () => { ui.transcript.scrollTop = 0; });
ui.conversation.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-answer]");
  if (button && answers[Number(button.dataset.answer)]) await navigator.clipboard.writeText(answers[Number(button.dataset.answer)]);
});
ui.prompt.addEventListener("keydown", (event) => { if (event.key === "Enter" && event.ctrlKey) ui.form.requestSubmit(); });
document.addEventListener("keydown", (event) => { if (event.key.toLowerCase() === "k" && event.ctrlKey) { event.preventDefault(); newConversation(); } });
fetchState().catch((error) => { ui.error.textContent = error.message; });
connectEvents();
