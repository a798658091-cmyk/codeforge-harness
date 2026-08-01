# CodeForge Harness

一个轻量、可运行、可测试的本地 Coding Agent Harness。项目面向 Agent
工程实习展示，重点不是封装现成工作流框架，而是从零实现模型、工具与环境之间
的原生执行闭环。

> 设计参考
> [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)，
> 并进行了模块化重构和独立实现。本项目不复制其 `s20` 教学型单文件结构。

合同合规审查 Agent 是另一个独立项目；CodeForge 只负责通用 Coding Agent
Harness，不包含法律 RAG 或合同风险判断逻辑。

## 当前完成度

Day 1 已完成：

- 原生 Agent Loop：模型决定是否调用工具，Harness 负责执行和回灌结果。
- Provider 抽象：OpenAI-compatible 实现兼容 OpenAI SDK 与 DeepSeek API。
- Mock Provider：无需真实 API 即可测试完整循环。
- 统一工具注册、Pydantic 参数校验与错误隔离。
- `read_file`、`write_file`、`edit_file`、`search`、`apply_patch`、
  `shell`、`run_tests`。
- 工作区路径约束和基础危险命令 hard-deny。
- reducer、工具与 Mock LLM 集成测试。

Day 2 已完成：

- workspace 路径沙箱覆盖父目录、绝对路径、符号链接和 Shell cwd 逃逸。
- 支持精确工具名和 Glob 规则的 `allow`、`ask`、`deny` 权限决策。
- `ask` 通过 CLI 展示工具参数并等待人工确认；无审批函数时 fail-closed。
- PreToolUse Hook 可阻止执行，PostToolUse Hook 可观察结果且错误相互隔离。
- 工具成功、参数错误、权限拒绝和 Hook 结果统一写入脱敏 JSONL 审计日志。
- 安全策略、Hooks、审计、Agent 错误回灌和路径逃逸测试。

SQLite 恢复、上下文压缩、Subagent、后台任务、Worktree 和真实 MCP 属于可选的
后续里程碑，不会提前放置没有调用路径的实现。

## 核心闭环

```text
User prompt
    |
    v
ModelProvider.complete(messages, tool schemas)
    |
    +-- final text --------------------------> return
    |
    +-- tool calls
            |
            v
      ToolRegistry.dispatch
            |
      Pydantic validation
            |
      allow / ask / deny
            |
      PreToolUse Hooks
            |
      Workspace-bound tool
            |
      PostToolUse Hooks + JSONL audit
            |
            v
      tool result -> messages -> next model turn
```

Agent Loop 只依赖两个接口：

1. `ModelProvider`：把不同模型 API 规范化为 `AssistantTurn`。
2. `ToolRegistry`：把工具 schema、参数校验、执行和错误结果统一起来。

因此测试时可以用 Mock Provider 替换真实模型，而不改变 Agent Loop。

## 快速开始

要求 Python 3.10+。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

在 `.env` 中配置模型，不要提交真实 Key：

```dotenv
CODEFORGE_MODEL=deepseek-v4-pro
CODEFORGE_API_KEY=your-api-key
CODEFORGE_BASE_URL=https://api.deepseek.com

CODEFORGE_PERMISSION_DEFAULT=ask
CODEFORGE_PERMISSION_RULES=read_file=allow,search=allow
CODEFORGE_AUDIT_LOG=.codeforge/audit.jsonl
```

查看已注册工具：

```powershell
.\.venv\Scripts\python -m harness --list-tools
```

执行任务：

```powershell
.\.venv\Scripts\python -m harness `
  --workspace D:\path\to\your-project `
  "阅读代码并修复失败的测试"
```

临时覆盖权限规则或关闭审计：

```powershell
.\.venv\Scripts\python -m harness `
  --permission shell=deny `
  --permission run_tests=allow `
  --no-audit `
  "阅读代码并运行测试"
```

CLI 默认 `permission-default=ask`，同时允许 `read_file` 和 `search`。未命中只读
规则的写入、Shell 和测试工具会在执行前询问。审计默认写入 workspace 内的
`.codeforge/audit.jsonl`，该目录已被 `.gitignore` 忽略。

## 测试

运行 Day 1 + Day 2 全部离线测试：

```powershell
.\.venv\Scripts\python -m pytest -q -p no:cacheprovider
```

只运行核心 Agent Loop 集成测试：

```powershell
.\.venv\Scripts\python -m pytest tests\test_agent_loop.py -q `
  -p no:cacheprovider
```

查看全部测试的开始、临时 workspace、执行结果和 teardown：

```powershell
.\scripts\test_all_verbose.ps1
```

保留的核心详细入口（不包含独立的 Hooks/Audit 测试文件）：

```powershell
.\scripts\test_day1_verbose.ps1
```

只运行 Day 2 安全测试：

```powershell
.\.venv\Scripts\python -m pytest `
  tests\test_permissions.py `
  tests\test_hooks.py `
  tests\test_audit.py `
  tests\test_workspace.py `
  tests\test_agent_loop.py `
  -vv -s --show-test-process -p no:cacheprovider
```

使用当前进程或系统中已经设置的环境变量，额外执行一次真实 Provider 请求：

```powershell
.\scripts\test_day1_verbose.ps1 -LiveProvider
```

真实测试读取 `CODEFORGE_API_KEY` / `DEEPSEEK_API_KEY`，模型和地址默认采用
`deepseek-v4-pro` 与 `https://api.deepseek.com`；已有的 `CODEFORGE_MODEL` 和
`CODEFORGE_BASE_URL` 可以覆盖默认值。测试不会创建配置文件，也不会打印 API
Key。该模式会访问外部 API 并产生少量模型费用；不带 `-LiveProvider` 时仍然只
运行离线测试。

也可以直接使用 pytest 的详细模式：

```powershell
.\.venv\Scripts\python -m pytest -vv -ra --tb=short `
  --show-test-process -p no:cacheprovider
```

测试不需要 API Key。Mock Provider 会先返回工具调用，再根据工具结果返回最终
答案，用来验证真实的“模型—工具—结果—模型”闭环。Provider 单元测试注入的
也是本地假客户端，不会发出真实网络请求或消耗模型额度。

pytest 进程从项目根目录启动；文件、Shell、Patch 和 `run_tests` 等工具测试使用
pytest 为各测试创建的独立临时 workspace。详细模式会打印每个临时目录的绝对
路径，测试结束后 pytest 负责清理这些目录。

## 安全说明

- 文件路径在解析符号链接后仍必须位于 workspace 内。
- `write_file` 只创建新文件，不直接覆盖已有文件。
- `edit_file` 使用精确文本匹配，默认拒绝多处歧义替换。
- `apply_patch` 先校验全部变更，再写入；写入异常时回滚已修改文件。
- Shell 子进程不会继承名称含 Key、Token、Secret、Password 的环境变量。
- CLI 默认允许读取和搜索，其余工具进入 `ask` 人工审批。
- `deny` 和未获批准的 `ask` 在 PreToolUse 与工具执行前停止调用。
- PreToolUse 异常采用 fail-closed；PostToolUse 异常不伪造已发生的工具结果。
- JSONL 审计记录权限、参数、结果、耗时和 Hook 错误，并脱敏常见凭据。
- 审计路径自身也必须位于 workspace 内。
- Shell 的 cwd 受路径沙箱约束，但 Day 2 尚不是操作系统级进程沙箱；命令文本
  内主动引用的外部绝对路径仍需通过权限审批和 hard-deny 控制。

## 面试讲解要点

可以把本项目概括为：

> 我没有使用 LangChain 或 LangGraph 编排，而是实现了一个原生 Agent Loop。
> 模型输出 Provider-neutral 的工具调用，注册表用 Pydantic 校验参数，再把调用
> 分发给受 Workspace 边界约束的工具。工具结果作为 `tool` 消息回灌模型。
> 集成测试使用 Mock LLM 完整模拟两轮对话，因此不依赖外部 API，能够稳定验证
> Harness 的核心控制流。

几个关键设计取舍：

- Provider 与循环解耦，模型供应商变化不会影响工具系统。
- 工具失败被转换为模型可见的结构化结果，不会直接击穿循环。
- 安全边界位于工具执行入口，而不是只依赖系统提示词。
- `apply_patch` 采用结构化变更而不是让模型自由拼接 Shell 命令。
- 状态通过 reducer 更新，为后续 SQLite checkpoint 和 `--resume` 留出稳定入口。

## 许可证

MIT。参考项目同样使用 MIT 许可证；本项目保留明确设计参考说明，并采用独立的
模块化实现。
