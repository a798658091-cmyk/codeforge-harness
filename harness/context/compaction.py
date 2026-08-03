"""实现无需额外模型调用的确定性对话上下文压缩。

任务流位置：Agent Loop 每次请求 Provider 前调用本模块；超过消息数或字符预算时，
旧消息被整理成摘要，系统消息和近期工具调用链保留，并随后写入 SQLite 检查点。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SUMMARY_PREFIX = "[CodeForge earlier context summary]"


@dataclass(frozen=True)
class CompactionResult:
    """描述压缩后的消息、是否发生变化及移除数量。"""

    messages: list[dict[str, Any]]
    compacted: bool
    removed_messages: int = 0


class ContextCompactor:
    """按消息数和估算字符预算压缩较旧的对话历史。"""

    def __init__(
        self,
        *,
        max_messages: int = 40,
        keep_recent: int = 12,
        max_characters: int = 40000,
        max_summary_characters: int = 6000,
    ) -> None:
        """配置触发阈值、近期消息保留量和摘要长度。"""

        if max_messages < 4:
            raise ValueError("max_messages must be at least 4")
        if keep_recent < 2 or keep_recent >= max_messages:
            raise ValueError(
                "keep_recent must be at least 2 and less than max_messages"
            )
        if max_characters < 1000:
            raise ValueError("max_characters must be at least 1000")
        if max_summary_characters < 500:
            raise ValueError("max_summary_characters must be at least 500")
        self.max_messages = max_messages
        self.keep_recent = keep_recent
        self.max_characters = max_characters
        self.max_summary_characters = max_summary_characters

    def compact(
        self,
        messages: list[dict[str, Any]],
    ) -> CompactionResult:
        """在超过预算时用本地摘要替换旧消息并保留合法工具消息边界。"""

        copied = [dict(message) for message in messages]
        if len(copied) <= 1 or not self._over_budget(copied):
            return CompactionResult(copied, compacted=False)

        system = (
            copied[0]
            if copied[0].get("role") == "system"
            else None
        )
        body = copied[1:] if system is not None else copied
        if len(body) <= self.keep_recent:
            return CompactionResult(copied, compacted=False)

        cut = len(body) - self.keep_recent
        # 不让近期上下文从孤立的 tool 消息开始；向前保留其 assistant 调用。
        while cut > 0 and body[cut].get("role") == "tool":
            cut -= 1
        if cut <= 0:
            return CompactionResult(copied, compacted=False)

        old_messages = body[:cut]
        recent_messages = body[cut:]
        summary = self._summarize(old_messages)
        summary_message = {
            "role": "system",
            "content": f"{SUMMARY_PREFIX}\n{summary}",
        }
        compacted_messages = []
        if system is not None:
            compacted_messages.append(system)
        compacted_messages.extend([summary_message, *recent_messages])
        return CompactionResult(
            compacted_messages,
            compacted=True,
            removed_messages=len(old_messages),
        )

    def estimate_characters(self, messages: list[dict[str, Any]]) -> int:
        """估算消息内容和工具调用参数占用的上下文字符数。"""

        return sum(len(str(message)) for message in messages)

    def _over_budget(self, messages: list[dict[str, Any]]) -> bool:
        """判断消息数量或字符数是否越过任一配置阈值。"""

        return (
            len(messages) > self.max_messages
            or self.estimate_characters(messages) > self.max_characters
        )

    def _summarize(self, messages: list[dict[str, Any]]) -> str:
        """提取角色、文本与工具名称，生成有界且可重复的摘要。"""

        lines = [self._summarize_message(message) for message in messages]
        text = "\n".join(line for line in lines if line)
        if len(text) > self.max_summary_characters:
            omitted = len(text) - self.max_summary_characters
            text = (
                text[: self.max_summary_characters]
                + f"\n...[truncated {omitted} summary characters]"
            )
        return text or "No textual content was retained from earlier messages."

    @staticmethod
    def _summarize_message(message: dict[str, Any]) -> str:
        """把单条 OpenAI 风格消息转换为一行可读摘要。"""

        role = str(message.get("role", "unknown")).upper()
        content = str(message.get("content") or "").replace("\n", " ").strip()
        if len(content) > 500:
            content = f"{content[:500]}..."
        tool_calls = message.get("tool_calls") or []
        names = [
            str(call.get("function", {}).get("name", "unknown"))
            for call in tool_calls
            if isinstance(call, dict)
        ]
        details = content
        if names:
            suffix = f"requested tools: {', '.join(names)}"
            details = f"{details}; {suffix}" if details else suffix
        if role == "TOOL":
            tool_name = message.get("name", "unknown")
            details = f"{tool_name}: {details}"
        return f"- {role}: {details}" if details else f"- {role}: (empty)"
