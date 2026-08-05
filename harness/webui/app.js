"use strict";

const $ = (selector) => document.querySelector(selector);
const elements = {
  form: $("#taskForm"), prompt: $("#prompt"), run: $("#runButton"),
  cancel: $("#cancelButton"), copy: $("#copyButton"), formError: $("#formError"),
  connectionDot: $("#connectionDot"), connectionText: $("#connectionText"),
  status: $("#statusPill"), workspace: $("#workspace"), stepTokens: $("#stepTokens"),
  stepTokenDetail: $("#stepTokenDetail"), totalTokens: $("#totalTokens"),
  totalTokenDetail: $("#totalTokenDetail"), steps: $("#steps"),
  compactions: $("#compactions"), toolCalls: $("#toolCalls"),
  toolFailures: $("#toolFailures"), todoCount: $("#todoCount"),
  todoList: $("#todoList"), currentOperation: $("#currentOperation"),
  activityPulse: $("#activityPulse"), stepProgress: $("#stepProgress"),
  answer: $("#answer"), error: $("#error"), session: $("#sessionId"),
  timeline: $("#timeline"),
};

const statusLabels = {
  idle: "空闲", starting: "正在启动", running: "执行中", cancelling: "正在停止",
  cancelled: "已停止", completed: "已完成", failed: "失败",
};
let currentState = null;
const formatNumber = (value) => new Intl.NumberFormat("zh-CN").format(Number(value || 0));
const isRunning = (status) => ["starting", "running", "cancelling"].includes(status);

function render(state) {
  currentState = state;
  const tokens = state.tokens || {};
  const tools = state.tools || {};
  const status = state.status || "idle";
  elements.status.textContent = statusLabels[status] || status;
  elements.status.className = `status-pill ${status}`;
  elements.workspace.textContent = state.workspace || "";
  elements.workspace.title = state.workspace || "";
  elements.session.textContent = state.session_id || "暂无会话";
  elements.session.title = state.session_id || "";
  elements.stepTokens.textContent = formatNumber(tokens.step_total);
  elements.stepTokenDetail.textContent = `输入 ${formatNumber(tokens.step_prompt)} · 输出 ${formatNumber(tokens.step_completion)}`;
  elements.totalTokens.textContent = formatNumber(tokens.total);
  elements.totalTokenDetail.textContent = `输入 ${formatNumber(tokens.prompt)} · 输出 ${formatNumber(tokens.completion)}`;
  elements.steps.textContent = `${state.steps || 0} / ${state.max_steps || 0}`;
  elements.compactions.textContent = `上下文压缩 ${state.compactions || 0} 次`;
  elements.toolCalls.textContent = formatNumber(tools.calls);
  elements.toolFailures.textContent = `失败 ${formatNumber(tools.failures)} 次`;
  elements.stepProgress.style.width = `${Math.min(100, ((state.steps || 0) / Math.max(1, state.max_steps || 1)) * 100)}%`;
  elements.run.disabled = isRunning(status);
  elements.cancel.disabled = !isRunning(status) || status === "cancelling";
  elements.prompt.disabled = isRunning(status);
  renderTodos(state.todos || []);
  renderOperation(state.current_operation, status);
  renderAnswer(state.answer || "", state.error || "");
  renderTimeline(state.operations || []);
}

function renderTodos(todos) {
  elements.todoCount.textContent = String(todos.length);
  elements.todoList.replaceChildren();
  if (!todos.length) {
    elements.todoList.className = "todo-list empty-state";
    elements.todoList.textContent = "Agent 制定计划后会显示在这里。";
    return;
  }
  elements.todoList.className = "todo-list";
  todos.forEach((todo) => {
    const item = document.createElement("div");
    const status = todo.status || "pending";
    item.className = `todo-item ${status}`;
    const check = document.createElement("span");
    check.className = "todo-check";
    check.textContent = status === "completed" ? "✓" : "";
    const content = document.createElement("span");
    content.className = "todo-content";
    content.textContent = todo.content || todo.title || todo.id || "未命名任务";
    const label = document.createElement("span");
    label.className = "todo-status";
    label.textContent = status.replace("_", " ");
    item.append(check, content, label);
    elements.todoList.append(item);
  });
}

function renderOperation(operation, status) {
  const running = operation && operation.status === "running";
  elements.activityPulse.classList.toggle("hidden", !running);
  elements.currentOperation.replaceChildren();
  const icon = document.createElement("span");
  icon.className = "operation-icon";
  icon.textContent = running ? "RUN" : status === "completed" ? "OK" : "···";
  const content = document.createElement("div");
  const title = document.createElement("strong");
  const detail = document.createElement("p");
  if (running) {
    title.textContent = operation.tool;
    detail.textContent = "工具正在工作，完成后会进入操作时间线。";
  } else if (status === "running") {
    title.textContent = "模型正在思考";
    detail.textContent = "等待下一次模型响应或工具调用。";
  } else if (status === "completed") {
    title.textContent = "任务已完成";
    detail.textContent = "最终回答和全部操作已经记录。";
  } else if (status === "failed") {
    title.textContent = "任务执行失败";
    detail.textContent = "请查看回答区域中的错误信息。";
  } else {
    title.textContent = "等待任务";
    detail.textContent = "工具开始执行时会实时更新。";
  }
  content.append(title, detail);
  elements.currentOperation.append(icon, content);
}

function renderAnswer(answer, error) {
  elements.answer.textContent = answer || "任务完成后，最终回答会显示在这里。";
  elements.answer.classList.toggle("empty-state", !answer);
  elements.copy.disabled = !answer;
  elements.error.textContent = error;
  elements.error.classList.toggle("hidden", !error);
}

function renderTimeline(operations) {
  elements.timeline.replaceChildren();
  if (!operations.length) {
    elements.timeline.className = "timeline empty-state";
    elements.timeline.textContent = "文件、Shell、测试等操作会按完成时间排列。";
    return;
  }
  elements.timeline.className = "timeline";
  [...operations].reverse().slice(0, 30).forEach((operation) => {
    const item = document.createElement("div");
    item.className = `timeline-item ${operation.status || ""}`;
    const dot = document.createElement("span");
    dot.className = "timeline-dot";
    const main = document.createElement("div");
    main.className = "timeline-main";
    const tool = document.createElement("strong");
    tool.textContent = operation.tool || "unknown";
    const summary = document.createElement("p");
    summary.textContent = operation.summary || operation.error_type || "已完成";
    summary.title = summary.textContent;
    main.append(tool, summary);
    const time = document.createElement("span");
    time.className = "timeline-time";
    const date = new Date(operation.timestamp);
    time.textContent = Number.isNaN(date.getTime()) ? `${operation.duration_ms || 0}ms` : date.toLocaleTimeString("zh-CN", { hour12: false });
    item.append(dot, main, time);
    elements.timeline.append(item);
  });
}

async function fetchState() {
  const response = await fetch("/api/state", { cache: "no-store" });
  if (!response.ok) throw new Error("无法读取运行状态");
  render(await response.json());
}

async function submitTask(event) {
  event.preventDefault();
  elements.formError.textContent = "";
  const prompt = elements.prompt.value.trim();
  if (!prompt) return;
  try {
    const response = await fetch("/api/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "任务启动失败");
    await fetchState();
  } catch (error) {
    elements.formError.textContent = error.message;
  }
}

async function cancelTask() {
  elements.formError.textContent = "";
  try {
    const response = await fetch("/api/cancel", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "停止任务失败");
  } catch (error) {
    elements.formError.textContent = error.message;
  }
}

function connectEvents() {
  const stream = new EventSource("/api/events");
  const names = [
    "snapshot", "run.started", "run.cancelling", "run.finished", "state.updated",
    "model.started", "model.completed", "tool.started", "tool.completed",
    "tool.finished", "todos.updated", "context.compacted",
  ];
  names.forEach((name) => stream.addEventListener(name, async (event) => {
    elements.connectionDot.classList.add("connected");
    elements.connectionText.textContent = "实时连接";
    const payload = JSON.parse(event.data);
    if (["snapshot", "run.started", "run.cancelling", "run.finished"].includes(name)) render(payload);
    else if (payload.state) render(payload.state);
    else await fetchState();
  }));
  stream.onopen = () => {
    elements.connectionDot.classList.add("connected");
    elements.connectionText.textContent = "实时连接";
  };
  stream.onerror = () => {
    elements.connectionDot.classList.remove("connected");
    elements.connectionText.textContent = "正在重连";
  };
}

elements.form.addEventListener("submit", submitTask);
elements.cancel.addEventListener("click", cancelTask);
elements.copy.addEventListener("click", async () => {
  if (currentState?.answer) await navigator.clipboard.writeText(currentState.answer);
});
elements.prompt.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && event.ctrlKey) elements.form.requestSubmit();
});
fetchState().catch((error) => { elements.formError.textContent = error.message; });
connectEvents();
