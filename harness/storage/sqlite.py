"""使用 SQLite 持久化 Agent 会话和不可变检查点。

任务流位置：CLI 为每次新任务创建会话，Agent Loop 在用户消息、模型回合、工具
结果与压缩后回调本模块保存状态；--resume 从最新检查点恢复后继续同一会话。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from harness.agent.state import AgentState
from harness.storage.models import SessionCheckpoint, SessionInfo


class SessionNotFoundError(LookupError):
    """请求恢复不存在或尚无检查点的会话时抛出。"""


class SQLiteSessionStore:
    """提供线程安全的 SQLite 会话创建、检查点保存和恢复接口。"""

    def __init__(self, path: str | Path) -> None:
        """创建数据库父目录并初始化表结构。"""

        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        """创建启用外键和超时设置的短生命周期连接。"""

        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """提交或回滚事务，并确保短生命周期连接被关闭。"""

        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        """以幂等方式创建会话和检查点表及查询索引。"""

        with self._lock, self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    workspace TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, sequence),
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_checkpoints_session_sequence
                    ON checkpoints(session_id, sequence DESC);
                CREATE INDEX IF NOT EXISTS idx_sessions_workspace_updated
                    ON sessions(workspace, updated_at DESC);
                """
            )

    def create_session(
        self,
        workspace: str | Path,
        *,
        session_id: str | None = None,
    ) -> SessionInfo:
        """为规范化工作区创建一个新会话并返回其元数据。"""

        identifier = session_id or uuid.uuid4().hex
        if not identifier.strip():
            raise ValueError("session_id cannot be empty")
        now = _utc_now()
        resolved_workspace = str(Path(workspace).expanduser().resolve())
        try:
            with self._lock, self._connection() as connection:
                connection.execute(
                    "INSERT INTO sessions(id, workspace, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (identifier, resolved_workspace, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"session already exists: {identifier}") from exc
        return SessionInfo(identifier, resolved_workspace, now, now)

    def get_session(self, session_id: str) -> SessionInfo:
        """按 ID 查询会话，不存在时抛出明确异常。"""

        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT id, workspace, created_at, updated_at "
                "FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise SessionNotFoundError(f"session not found: {session_id}")
        return _session_from_row(row)

    def latest_session(self, workspace: str | Path) -> SessionInfo:
        """返回指定工作区最近更新的会话。"""

        resolved_workspace = str(Path(workspace).expanduser().resolve())
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT id, workspace, created_at, updated_at FROM sessions "
                "WHERE workspace = ? ORDER BY updated_at DESC LIMIT 1",
                (resolved_workspace,),
            ).fetchone()
        if row is None:
            raise SessionNotFoundError(
                f"no session found for workspace: {resolved_workspace}"
            )
        return _session_from_row(row)

    def list_sessions(
        self,
        workspace: str | Path | None = None,
        *,
        limit: int = 20,
    ) -> list[SessionInfo]:
        """按最近更新时间列出会话，可选择只查看一个工作区。"""

        if limit < 1:
            raise ValueError("limit must be at least 1")
        query = (
            "SELECT id, workspace, created_at, updated_at FROM sessions"
        )
        parameters: tuple[object, ...]
        if workspace is None:
            parameters = (limit,)
            query += " ORDER BY updated_at DESC LIMIT ?"
        else:
            resolved_workspace = str(Path(workspace).expanduser().resolve())
            parameters = (resolved_workspace, limit)
            query += " WHERE workspace = ? ORDER BY updated_at DESC LIMIT ?"
        with self._lock, self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_session_from_row(row) for row in rows]

    def save_checkpoint(
        self,
        session_id: str,
        state: AgentState,
        *,
        reason: str,
    ) -> SessionCheckpoint:
        """在一个事务中追加完整状态快照并更新会话时间。"""

        now = _utc_now()
        payload = json.dumps(
            state.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock, self._connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if exists is None:
                raise SessionNotFoundError(
                    f"session not found: {session_id}"
                )
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence "
                "FROM checkpoints WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            sequence = int(row["next_sequence"])
            connection.execute(
                "INSERT INTO checkpoints"
                "(session_id, sequence, reason, state_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, sequence, reason, payload, now),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
        return SessionCheckpoint(
            session_id=session_id,
            sequence=sequence,
            reason=reason,
            state=AgentState.from_dict(state.to_dict()),
            created_at=now,
        )

    def load_latest_checkpoint(
        self,
        session_id: str,
    ) -> SessionCheckpoint:
        """读取会话序号最大的状态快照。"""

        self.get_session(session_id)
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT session_id, sequence, reason, state_json, created_at "
                "FROM checkpoints WHERE session_id = ? "
                "ORDER BY sequence DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if row is None:
            raise SessionNotFoundError(
                f"session has no checkpoints: {session_id}"
            )
        return SessionCheckpoint(
            session_id=row["session_id"],
            sequence=int(row["sequence"]),
            reason=row["reason"],
            state=AgentState.from_dict(json.loads(row["state_json"])),
            created_at=row["created_at"],
        )


def _session_from_row(row: sqlite3.Row) -> SessionInfo:
    """把 SQLite 行转换为稳定的 SessionInfo。"""

    return SessionInfo(
        id=row["id"],
        workspace=row["workspace"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _utc_now() -> str:
    """生成可按字符串排序的 UTC ISO 时间。"""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
