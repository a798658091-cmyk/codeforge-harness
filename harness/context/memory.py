"""实现 workspace 隔离的 SQLite 长期记忆与模型工具。

任务流位置：CLI 在启动 Agent Loop 前按当前用户提示召回相关记忆并注入系统
上下文；模型也可通过 memory_write/search/delete 管理跨会话项目知识。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from pydantic import Field

from harness.safety.audit import AuditLogger
from harness.tools.base import BaseTool, ToolArguments, ToolContext, ToolError


class SensitiveMemoryError(ValueError):
    """记忆内容疑似包含凭据或密钥时抛出。"""


@dataclass(frozen=True)
class MemoryRecord:
    """表示一条可检索、可更新的 workspace 长期记忆。"""

    id: str
    key: str
    content: str
    tags: tuple[str, ...]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ExplicitMemoryIntent:
    """表示从用户原话中识别出的明确长期记忆请求。"""

    key: str
    content: str
    tags: tuple[str, ...] = ("explicit", "user-requested")


class MemoryStore:
    """使用 SQLite 保存按稳定 key 去重的项目级长期记忆。"""

    def __init__(self, path: str | Path) -> None:
        """绑定数据库路径并初始化线程锁，数据库在首次访问时创建。"""

        self.path = Path(path).expanduser().resolve()
        self._lock = threading.RLock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        """创建带行对象和超时配置的 SQLite 连接。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """确保事务提交或回滚，并在使用后关闭连接。"""

        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _ensure_initialized(self) -> None:
        """以线程安全、幂等方式创建记忆表和更新时间索引。"""

        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            with self._connection() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS memories (
                        id TEXT PRIMARY KEY,
                        key TEXT NOT NULL UNIQUE,
                        content TEXT NOT NULL,
                        tags_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_memories_updated
                        ON memories(updated_at DESC);
                    """
                )
            self._initialized = True

    def write(
        self,
        key: str,
        content: str,
        *,
        tags: list[str] | tuple[str, ...] = (),
    ) -> MemoryRecord:
        """创建或按 key 更新记忆，并拒绝疑似敏感凭据。"""

        normalized_key = key.strip()
        normalized_content = content.strip()
        normalized_tags = tuple(
            dict.fromkeys(tag.strip() for tag in tags if tag.strip())
        )
        if not normalized_key:
            raise ValueError("memory key cannot be empty")
        if not normalized_content:
            raise ValueError("memory content cannot be empty")
        if len(normalized_key) > 120:
            raise ValueError("memory key is too long")
        if len(normalized_content) > 10000:
            raise ValueError("memory content is too long")
        if len(normalized_tags) > 20 or any(
            len(tag) > 80 for tag in normalized_tags
        ):
            raise ValueError("memory tags exceed limits")
        _reject_sensitive_memory(
            "\n".join([normalized_key, normalized_content, *normalized_tags])
        )

        self._ensure_initialized()
        now = _utc_now()
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT id, created_at FROM memories WHERE key = ?",
                (normalized_key,),
            ).fetchone()
            identifier = existing["id"] if existing else uuid.uuid4().hex
            created_at = existing["created_at"] if existing else now
            connection.execute(
                """
                INSERT INTO memories(
                    id, key, content, tags_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    content = excluded.content,
                    tags_json = excluded.tags_json,
                    updated_at = excluded.updated_at
                """,
                (
                    identifier,
                    normalized_key,
                    normalized_content,
                    json.dumps(normalized_tags, ensure_ascii=False),
                    created_at,
                    now,
                ),
            )
        return MemoryRecord(
            id=identifier,
            key=normalized_key,
            content=normalized_content,
            tags=normalized_tags,
            created_at=created_at,
            updated_at=now,
        )

    def search(self, query: str, *, limit: int = 5) -> list[MemoryRecord]:
        """使用轻量关键词评分检索最相关且较新的记忆。"""

        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("memory search query cannot be empty")
        if not 1 <= limit <= 50:
            raise ValueError("memory search limit must be between 1 and 50")
        self._ensure_initialized()
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT id, key, content, tags_json, created_at, updated_at "
                "FROM memories ORDER BY updated_at DESC LIMIT 1000"
            ).fetchall()

        tokens = _search_tokens(normalized_query)
        scored: list[tuple[int, int, MemoryRecord]] = []
        for recency, row in enumerate(rows):
            record = _memory_from_row(row)
            haystack = " ".join(
                [record.key, record.content, *record.tags]
            ).casefold()
            exact_bonus = 8 if normalized_query.casefold() in haystack else 0
            token_score = sum(
                3 if token in record.key.casefold() else 1
                for token in tokens
                if token in haystack
            )
            score = exact_bonus + token_score
            if score > 0:
                scored.append((score, -recency, record))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in scored[:limit]]

    def list_recent(self, *, limit: int = 20) -> list[MemoryRecord]:
        """按更新时间返回最近记忆，供空检索和调试使用。"""

        if not 1 <= limit <= 100:
            raise ValueError("memory list limit must be between 1 and 100")
        self._ensure_initialized()
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT id, key, content, tags_json, created_at, updated_at "
                "FROM memories ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_memory_from_row(row) for row in rows]

    def delete(self, key: str) -> bool:
        """按稳定 key 删除一条记忆，并返回是否确实存在。"""

        normalized_key = key.strip()
        if not normalized_key:
            raise ValueError("memory key cannot be empty")
        self._ensure_initialized()
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM memories WHERE key = ?",
                (normalized_key,),
            )
        return cursor.rowcount > 0

    def render_relevant(
        self,
        query: str,
        *,
        limit: int = 5,
        max_characters: int = 4000,
    ) -> str:
        """把相关记忆渲染为有长度上限的系统提示词片段。"""

        records = self.search(query, limit=limit)
        lines = [
            f"- [{record.key}] {record.content}"
            for record in records
        ]
        rendered = "\n".join(lines)
        return rendered[:max_characters]


class MemoryWriteArguments(ToolArguments):
    """定义写入或更新长期记忆所需参数。"""

    key: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=10000)
    tags: list[str] = Field(default_factory=list, max_length=20)


class MemorySearchArguments(ToolArguments):
    """定义关键词检索和最大返回数量。"""

    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=5, ge=1, le=50)


class MemoryDeleteArguments(ToolArguments):
    """定义删除长期记忆所需的稳定 key。"""

    key: str = Field(min_length=1, max_length=120)


class MemoryWriteTool(BaseTool):
    """让主 Agent 保存可跨会话复用的项目事实或偏好。"""

    name: ClassVar[str] = "memory_write"
    description: ClassVar[str] = (
        "Create or update one workspace memory. Never store credentials, "
        "secrets, transient logs, or unverified guesses."
    )
    arguments_model: ClassVar[type[ToolArguments]] = MemoryWriteArguments

    def __init__(self, store: MemoryStore | None) -> None:
        """绑定可选的 SQLite MemoryStore。"""

        self.store = store

    def execute(
        self,
        arguments: ToolArguments,
        context: ToolContext,
    ) -> str:
        """校验依赖和参数后写入记忆，并返回稳定标识。"""

        if not isinstance(arguments, MemoryWriteArguments):
            raise TypeError("memory_write received unexpected arguments")
        store = _require_store(self.store)
        record = store.write(
            arguments.key,
            arguments.content,
            tags=arguments.tags,
        )
        return json.dumps(_memory_to_dict(record), ensure_ascii=False, indent=2)


class MemorySearchTool(BaseTool):
    """让 Agent 搜索当前 workspace 的长期记忆。"""

    name: ClassVar[str] = "memory_search"
    description: ClassVar[str] = (
        "Search verified long-term memories for the current workspace."
    )
    arguments_model: ClassVar[type[ToolArguments]] = MemorySearchArguments

    def __init__(self, store: MemoryStore | None) -> None:
        """绑定可选的 SQLite MemoryStore。"""

        self.store = store

    def execute(
        self,
        arguments: ToolArguments,
        context: ToolContext,
    ) -> str:
        """执行关键词检索并返回 JSON 数组。"""

        if not isinstance(arguments, MemorySearchArguments):
            raise TypeError("memory_search received unexpected arguments")
        records = _require_store(self.store).search(
            arguments.query,
            limit=arguments.limit,
        )
        return json.dumps(
            [_memory_to_dict(record) for record in records],
            ensure_ascii=False,
            indent=2,
        )


class MemoryDeleteTool(BaseTool):
    """让主 Agent 删除过期或错误的长期记忆。"""

    name: ClassVar[str] = "memory_delete"
    description: ClassVar[str] = (
        "Delete one outdated or incorrect workspace memory by exact key."
    )
    arguments_model: ClassVar[type[ToolArguments]] = MemoryDeleteArguments

    def __init__(self, store: MemoryStore | None) -> None:
        """绑定可选的 SQLite MemoryStore。"""

        self.store = store

    def execute(
        self,
        arguments: ToolArguments,
        context: ToolContext,
    ) -> str:
        """删除指定 key，并报告是否找到记录。"""

        if not isinstance(arguments, MemoryDeleteArguments):
            raise TypeError("memory_delete received unexpected arguments")
        deleted = _require_store(self.store).delete(arguments.key)
        if not deleted:
            raise ToolError(f"memory not found: {arguments.key}")
        return f"deleted memory: {arguments.key}"


def _require_store(store: MemoryStore | None) -> MemoryStore:
    """取得已配置的 MemoryStore，禁用时返回工具错误。"""

    if store is None:
        raise ToolError("memory is disabled")
    return store


def _reject_sensitive_memory(value: str) -> None:
    """使用审计模块的凭据模式拒绝持久化敏感文本。"""

    if AuditLogger._redact_string(value) != value:
        raise SensitiveMemoryError(
            "memory appears to contain a credential or secret"
        )


def extract_explicit_memory_intents(prompt: str) -> list[ExplicitMemoryIntent]:
    """提取用户明确要求“记住”的内容，不推断普通陈述或隐含偏好。

    该函数只识别带有明确动作词的中文和英文表达，例如“请记住……”或
    “remember that ...”。实际持久化仍由 CLI 通过 Tool Registry 完成，因此
    权限、Hooks、敏感信息过滤和审计边界不会被绕过。
    """

    if not prompt.strip():
        return []
    patterns = (
        re.compile(
            r"(?<!不要)(?<!不用)(?<!无需)"
            r"(?:请你?记住|(?:^|[\n。！？])\s*记住)"
            r"\s*[:：]?\s*(?P<content>[^。！？\n]{2,1000})",
            re.MULTILINE,
        ),
        re.compile(
            r"(?<!do not )(?<!don't )\b(?:please\s+)?remember"
            r"(?:\s+that)?\s*[:：]?\s*"
            r"(?P<content>[^.!?\n]{2,1000})",
            re.IGNORECASE,
        ),
    )
    intents_by_key: dict[str, ExplicitMemoryIntent] = {}
    for pattern in patterns:
        for match in pattern.finditer(prompt):
            content = match.group("content").strip(" \t,，;；:：")
            if not content:
                continue
            key = _derive_explicit_memory_key(content)
            intents_by_key[key] = ExplicitMemoryIntent(
                key=key,
                content=content,
            )
    return list(intents_by_key.values())


def _derive_explicit_memory_key(content: str) -> str:
    """从记忆主题生成稳定 key，使同类偏好再次声明时能够更新原记录。"""

    folded = content.casefold()
    if "沟通偏好" in content or (
        "偏好" in content and any(word in content for word in ("中文", "回答", "说明"))
    ):
        return "项目沟通偏好"
    if "测试命令" in content or "test command" in folded:
        return "项目测试命令"
    if "代码风格" in content or "coding style" in folded:
        return "项目代码风格"

    topic = re.split(r"[:：]", content, maxsplit=1)[0].strip()
    topic = re.sub(r"^(?:我的|本项目的?|这个项目的?|that\s+)", "", topic)
    if 2 <= len(topic) <= 40:
        return topic[:120]
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    return f"explicit-memory-{digest}"


def _search_tokens(query: str) -> set[str]:
    """提取英文词和中文双字片段，支持简单跨语言检索。"""

    tokens = {
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9_.-]{2,}", query)
    }
    for sequence in re.findall(r"[\u3400-\u9fff]+", query):
        if len(sequence) == 1:
            tokens.add(sequence)
        else:
            tokens.update(
                sequence[index : index + 2]
                for index in range(len(sequence) - 1)
            )
    return tokens or {query.casefold()}


def _memory_from_row(row: sqlite3.Row) -> MemoryRecord:
    """把 SQLite 行转换为不可变 MemoryRecord。"""

    return MemoryRecord(
        id=row["id"],
        key=row["key"],
        content=row["content"],
        tags=tuple(json.loads(row["tags_json"])),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _memory_to_dict(record: MemoryRecord) -> dict[str, object]:
    """把记忆模型转换为适合工具返回的 JSON 字典。"""

    return {
        "id": record.id,
        "key": record.key,
        "content": record.content,
        "tags": list(record.tags),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _utc_now() -> str:
    """生成可按字符串排序的 UTC 时间。"""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


__all__ = [
    "ExplicitMemoryIntent",
    "MemoryDeleteTool",
    "MemoryRecord",
    "MemorySearchTool",
    "MemoryStore",
    "MemoryWriteTool",
    "SensitiveMemoryError",
    "extract_explicit_memory_intents",
]
