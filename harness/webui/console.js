"use strict";

const $ = (selector) => document.querySelector(selector);
const elements = {
  form: $("#taskForm"), prompt: $("#prompt"), run: $("#runButton"),
  cancel: $("#cancelButton"), copy: $("#copyButton"), formError: $("#formError"),
  newTask: $("#newTaskButton"), currentTask: $("#currentTaskButton"),
  currentTaskTitle: $("#currentTaskTitle"), currentTaskMeta: $("#currentTaskMeta"),
  sessionTitle: $("#sessionTitle"), connectionDot: $("#connectionDot"),
  connectionText: $("#connectionText"), status: $("#statusPill"),
  workspace: $("#workspace"), session: $("#sessionId"), transcript: $("#transcript"),
  welcome: $("#welcomeBlock"), userMessage: $("#userMessage"), userPrompt: $("#userPrompt"),
  stepTokens: $("#stepTokens"), totalTokens: $("#totalTokens"), toolCalls: $("#toolCalls"),
  stepTokenDetail: $("#stepTokenDetail"), totalTokenDetail: $("#totalTokenDetail"),
  steps: $("#steps"), compactions: $("#compactions"), toolFailures: $("#toolFailures"),
  todoCount: $("#todoCount"), todoList: $("#todoList"),
  currentOperation: $("#currentOperation"), stepProgress: $("#stepProgress"),
  timeline: $("#timeline"), answerBlock: $("#answerBlock"),
  answer: $("#answer"), error: $("#error"),
};

const statusLabels = {
  idle: "空闲", starting: "正在启动", running: "执行中", cancelling: "正在停止",
  cancelled: "已停止", completed: "已完成", failed: "失败",
};
const todoLabels = { pending: "待处理", in_progress: "进行中", completed: "完成" };
let currentState = null;
const formatNumber = (value) => new Intl.NumberFormat("zh-CN").format(Number(value || 0));
const isRunning = (status) => ["starting", "running", "cancelling"].includes(status);

function taskTitle(prompt) {
  const text = String(prompt || "").trim().split(/\r?\n/, 1)[0];
  return text ? (text.length > 34 ? `${text.slice(0, 34)}…` : text) : "新任务";
}

function render(state) {
  const bottomDistance = elements.transcript.scrollHeight - elements.transcript.scrollTop - elements.transcript.clientHeight;
  currentState = state;
  const tokens = state.tokens || {};
  const tools = state.tools || {};
  const status = state.status || "idle";
  const title = taskTitle(state.prompt);
  elements.status.textContent = statusLabels[status] || status;
  elements.status.className = `status-pill ${status}`;
  elements.currentTaskTitle.textContent = title;
  elements.sessionTitle.textContent = title;
  elements.currentTaskMeta.textContent = state.started_at ? `${statusLabels[status] || status} · ${state.steps || 0} 步` : "等待输入";
  elements.workspace.textContent = state.workspace || "";
  elements.workspace.title = state.workspace || "";
  elements.session.textContent = state.session_id || "暂无会话";
  elements.session.title = state.session_id || "";
  elements.stepTokens.textContent = formatNumber(tokens.step_total);
  elements.totalTokens.textContent = formatNumber(tokens.total);
  elements.toolCalls.textContent = formatNumber(tools.calls);
  elements.stepTokenDetail.textContent = `本步：输入 ${formatNumber(tokens.step_prompt)} · 输出 ${formatNumber(tokens.step_completion)}`;
  elements.totalTokenDetail.textContent = `累计：输入 ${formatNumber(tokens.prompt)} · 输出 ${formatNumber(tokens.completion)}`;
  elements.steps.textContent = `步骤 ${state.steps || 0} / ${state.max_steps || 0}`;
  elements.compactions.textContent = `压缩 ${state.compactions || 0} 次`;
  elements.toolFailures.textContent = `失败 ${formatNumber(tools.failures)} 次`;
  elements.stepProgress.style.width = `${Math.min(100, ((state.steps || 0) / Math.max(1, state.max_steps || 1)) * 100)}%`;
  elements.run.disabled = isRunning(status);
  elements.cancel.disabled = !isRunning(status) || status === "cancelling";
  elements.prompt.disabled = isRunning(status);
  elements.welcome.classList.toggle("hidden", Boolean(state.prompt));
  elements.userMessage.classList.toggle("hidden", !state.prompt);
  elements.userPrompt.textContent = state.prompt || "";
  renderTodos(state.todos || []);
  renderOperation(state.current_operation, status);
  renderTimeline(state.operations || []);
  renderAnswer(state.answer || "", state.error || "", status);
  if (bottomDistance < 120) {
    requestAnimationFrame(() => { elements.transcript.scrollTop = elements.transcript.scrollHeight; });
  }
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
    check.textContent = status === "completed" ? "✓" : status === "in_progress" ? "·" : "";
    const content = document.createElement("span");
    content.className = "todo-content";
    content.textContent = todo.content || todo.title || todo.id || "未命名任务";
    const label = document.createElement("span");
    label.className = "todo-status";
    label.textContent = todoLabels[status] || status;
    item.append(check, content, label);
    elements.todoList.append(item);
  });
}

function renderOperation(operation, status) {
  const running = operation && operation.status === "running";
  elements.currentOperation.replaceChildren();
  elements.currentOperation.className = `current-operation${running ? " running" : ""}`;
  const glyph = document.createElement("span");
  glyph.className = "activity-glyph";
  glyph.textContent = running ? "●" : status === "completed" ? "✓" : status === "failed" ? "!" : "···";
  const content = document.createElement("div");
  const title = document.createElement("strong");
  const detail = document.createElement("p");
  if (running && operation.tool === "model") {
    title.textContent = "模型正在思考";
    detail.textContent = "正在决定下一步操作。";
  } else if (running) {
    title.textContent = operation.tool || "unknown";
    detail.textContent = "工具正在执行，结果随后进入下方时间线。";
  } else if (status === "running") {
    title.textContent = "等待下一步";
    detail.textContent = "模型响应和工具状态会在这里切换。";
  } else if (status === "completed") {
    title.textContent = "任务已完成";
    detail.textContent = "最终回答和执行过程已经保留。";
  } else if (status === "failed") {
    title.textContent = "任务执行失败";
    detail.textContent = "请向下查看终止原因和最后完成的操作。";
  } else if (status === "cancelled") {
    title.textContent = "任务已停止";
    detail.textContent = "可以在输入框中提交一个新任务。";
  } else {
    title.textContent = "等待任务";
    detail.textContent = "模型或工具开始工作时会实时更新。";
  }
  content.append(title, detail);
  elements.currentOperation.append(glyph, content);
}

function renderTimeline(operations) {
  elements.timeline.replaceChildren();
  if (!operations.length) {
    elements.timeline.className = "timeline empty-state";
    elements.timeline.textContent = "执行文件、Shell 或测试工具后会显示在这里。";
    return;
  }
  elements.timeline.className = "timeline";
  operations.forEach((operation) => {
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

function renderAnswer(answer, error, status) {
  const visible = Boolean(answer || error || ["completed", "failed", "cancelled"].includes(status));
  elements.answerBlock.classList.toggle("hidden", !visible);
  elements.answer.textContent = answer || (status === "completed" ? "任务已完成。" : "");
  elements.copy.disabled = !answer;
  elements.error.textContent = error;
  elements.error.classList.toggle("hidden", !error);
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

function prepareNewTask() {
  if (isRunning(currentState?.status)) return;
  elements.prompt.value = "";
  elements.prompt.focus();
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
elements.newTask.addEventListener("click", prepareNewTask);
elements.currentTask.addEventListener("click", () => { elements.transcript.scrollTop = 0; });
elements.copy.addEventListener("click", async () => {
  if (currentState?.answer) await navigator.clipboard.writeText(currentState.answer);
});
elements.prompt.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && event.ctrlKey) elements.form.requestSubmit();
});
document.addEventListener("keydown", (event) => {
  if (event.key.toLowerCase() === "k" && event.ctrlKey) {
    event.preventDefault();
    prepareNewTask();
  }
});
fetchState().catch((error) => { elements.formError.textContent = error.message; });
connectEvents();
