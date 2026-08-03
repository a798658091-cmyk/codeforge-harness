# CodeForge Harness 架构

CodeForge 使用原生循环连接模型、工具和本地工作区。Day 3 增加会话与上下文管理，
Day 4 精简版增加长期 Memory、同步只读 Subagent、后台 Shell 和运行时通知。

```mermaid
flowchart TB
    USER["用户任务"] --> CLI["CLI / Settings"]
    CLI --> SESSION["创建会话或 --resume 恢复"]
    SESSION --> LOOP["Agent Loop + AgentState reducer"]
    LOOP --> COMPACT["上下文预算检查 / Skills / Memory 召回"]
    COMPACT --> PROVIDER["真实或 Mock ModelProvider"]
    PROVIDER --> DECISION{"文本回答还是工具调用？"}
    DECISION -- "文本" --> CHECKPOINT["SQLite 检查点"]
    CHECKPOINT --> ANSWER["最终答案 + session ID"]
    DECISION -- "工具" --> REGISTRY["Tool Registry"]
    REGISTRY --> SAFETY["参数校验 → 权限 → Pre Hook → 沙箱"]
    SAFETY --> TOOLS["代码 / Todo / Skills / Memory / 后台 Shell"]
    TOOLS --> SUBAGENT["只读 Subagent 独立循环"]
    SUBAGENT --> READONLY["read / search / Skills / memory_search"]
    TOOLS --> NOTICE["后台终态通知"]
    TOOLS --> AFTER["Post Hook → 审计 → reducer"]
    AFTER --> CHECKPOINT
    CHECKPOINT --> LOOP
```

## Day 3～Day 4 数据位置

- 短期运行状态：`AgentState`，包含消息、指标、Todo 和压缩次数。
- 会话与检查点：workspace 内的 `.codeforge/sessions.sqlite3`。
- 审计日志：workspace 内的 `.codeforge/audit.jsonl`。
- 用户级 workspace Skills：`.codeforge/skills/<name>/SKILL.md`。
- 项目共享 Skills：`skills/<name>/SKILL.md`。
- 长期项目记忆：`.codeforge/memory.sqlite3`。
- 后台命令日志：`.codeforge/background/<job_id>.log`。

SQLite 检查点保存的是完整可恢复状态；上下文压缩改变后续发送给模型的消息，压缩
结果也会立即创建检查点。审计日志负责回答“工具做过什么”，会话数据库负责回答
“Agent 对话和计划进行到哪里”，Memory 负责“哪些已确认知识值得跨会话复用”。

通知当前只在一个 CLI 进程内保存，退出前统一展示；后台进程也只属于当前 CLI，
退出时会被清理。只读 Subagent 同步执行且没有写工具，完整并行委派留待 Worktree
隔离和 MessageBus 完成后再实现。
