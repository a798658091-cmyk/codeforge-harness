# CodeForge Harness — Web 实时控制台

## 项目简介

CodeForge Harness 是一个轻量、可运行、可测试的本地 Coding Agent Harness，
面向 Agent 工程实习展示。项目不依赖 LangChain 或 LangGraph 等现成工作流
框架，而是从零实现了模型、工具与环境之间的**原生执行闭环**。

核心架构只依赖两个接口：

- **ModelProvider** — 把不同模型 API（OpenAI / DeepSeek 等）规范化为统一的
  `AssistantTurn`，使循环与具体供应商解耦。
- **ToolRegistry** — 统一工具注册、Pydantic 参数校验、权限决策（allow / ask /
  deny）、Hooks 和 JSONL 审计，所有工具调用走同一条安全管道。

在此基础上，项目逐步实现了上下文压缩与 SQLite 会话恢复、长期 Memory、
Workspace Skills 渐进加载、同步只读 Subagent、基于 Git Worktree 的异步可写
Subagent、后台 Shell 与通知、stdio MCP 客户端，以及进程内 MessageBus
生命周期事件。集成测试使用 Mock Provider 完整模拟多轮工具对话，无需外部
API 即可稳定验证控制流。

## Web Demo 概述

项目内建了一个**不依赖 Node、LangSmith 或外部服务的简易本地控制台**。
Web Runtime 启动真实的 `python -m harness` CLI 子进程，并通过 Agent 原生
JSONL 事件、SQLite 检查点和审计日志，把以下状态以 **SSE（Server-Sent Events）**
实时推送到浏览器：

- 当前 Todo 清单（来自 `todo_read` / `todo_write`）
- 模型/工具执行阶段（思考 / 工具调用 / 结果回灌）
- 单步与累计 Token 消耗
- 已完成步骤数、工具失败数
- 最终回答内容
- 审计时间线（权限决策、工具参数与结果、耗时）

页面由 `harness/webui/` 下的纯静态文件（`index.html`、`app.js`、
`styles.css`）构成，浏览器端使用原生 `EventSource` 消费 SSE 事件流，无需
任何前端构建步骤。

## 快速启动

在项目虚拟环境中执行：

```powershell
Set-Location "D:\c++项目\codeforge-harness"
.\.venv\Scripts\python -m harness.web `
  --workspace "D:\c++项目\codeforge-harness"
```

然后访问：

```text
http://127.0.0.1:8765
```

服务启动后，页面直接继承当前终端中已配置的 `CODEFORGE_API_KEY`、
`CODEFORGE_BASE_URL` 和 `CODEFORGE_MODEL` 等 Provider 环境变量，不需要
在浏览器中重新输入凭据。

## 安全与权限

为方便本地演示，Web 任务默认使用 `--yes` 自动批准 `ask` 级别的权限请求。
以下安全机制仍然完整生效：

- 显式 `deny` 规则不会被覆盖
- Shell hard-deny 危险命令列表继续拦截
- Workspace 路径沙箱限制文件访问范围
- PreToolUse / PostToolUse Hooks 正常执行
- 服务固定绑定传入的 Workspace，浏览器**不能切换**到其他目录

## 架构要点

```text
浏览器 (SSE EventSource)
    |
    +--> GET /api/events?since=0        SSE 事件流（Todo、阶段、Token、审计）
    +--> POST /api/task { "prompt": … } 提交新任务
    |
    v
Python HTTP Server (ThreadingHTTPServer, 端口 8765)
    |
    +--> EventStream (有界队列 + Condition 条件变量)
    |        |
    |        +--> publish(event_type, payload) → notify_all()
    |        +--> wait_after(sequence, timeout) → SSE 阻塞等待
    |
    +--> 后台监控线程
    |        |
    |        +--> 读取 SQLite 检查点（会话消息、Todo、指标）
    |        +--> 扫描权限审批输出（tool=xxx approved/denied）
    |        +--> 解析 JSONL 审计日志（工具调用、结果、耗时）
    |        +--> 汇总后通过 EventStream 推送
    |
    +--> CLI 子进程
             |
             +--> python -m harness --yes --workspace <固定路径>
             +--> stdout / stderr / JSONL / SQLite → 监控线程消费
```

关键设计取舍：

- **零外部依赖**：不需要 Node.js、npm、Docker 或云服务；只需要 Python 标准库
  `http.server` + `threading` + `subprocess`。
- **单向数据流**：浏览器只能提交任务 prompt 和订阅 SSE；所有状态由服务端
  监控线程主动推送，前端不轮询。
- **Token 计数准确**：每轮 Provider 返回 `usage` 后更新累计值；当前版本
  不是逐 Token 流式生成（即非 stream 模式）。
- **单 Workspace 绑定**：服务启动时固定 Workspace 路径，运行期间不可更改，
  避免浏览器端任意切换目录的安全风险。

## 与 CLI 的关系

`harness.web` 不是独立的执行引擎。它内部启动了一个真实的 `python -m harness`
子进程，该子进程走完整的 Agent Loop、权限、Hooks、审计和 SQLite 持久化路径。
Web 层只是为同一套执行路径增加了一个**实时可视化前端**，所有行为与 CLI 完全
一致，不会出现"Web 上能做但 CLI 不能做"或相反的情况。

## 故障排查

- **端口占用**：默认端口 8765，可通过 `--port` 参数修改。
- **Provider 未配置**：Web 服务不会回退到 Mock Provider；如 `.env` 未设置
  API Key，任务提交后将收到 Provider 初始化错误事件。
- **浏览器无更新**：SSE 连接默认 15 秒超时发送心跳；确认防火墙或代理没有
  缓冲 `text/event-stream` 类型的响应。
- **任务超时**：Web 任务与 CLI 使用相同的超时机制；超时后子进程被终止，
  事件流会推送 `run_error` 类型事件。
