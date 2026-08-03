"""定义 SQLite 会话和检查点的只读传输模型。

任务流位置：CLI 查询或恢复会话时，SQLiteSessionStore 用这些模型返回元数据和
AgentState，避免把数据库行对象直接泄漏到 Agent Loop。
"""

from __future__ import annotations

from dataclasses import dataclass

from harness.agent.state import AgentState


@dataclass(frozen=True)
class SessionInfo:
    """描述一个可恢复会话的标识、工作区和更新时间。"""

    id: str
    workspace: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SessionCheckpoint:
    """封装会话中一个带序号和原因的完整 Agent 状态快照。"""

    session_id: str
    sequence: int
    reason: str
    state: AgentState
    created_at: str
