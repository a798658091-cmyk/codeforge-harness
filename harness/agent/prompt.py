"""构造约束本地 Coding Agent 行为的系统提示词。

任务流位置：CLI 在创建 Agent Loop 前调用本模块，把解析后的 workspace 边界
写入系统消息；该消息随后作为每次模型对话的首条上下文。
"""

from __future__ import annotations

from pathlib import Path


def build_system_prompt(workspace: Path) -> str:
    """生成包含 workspace 边界和工具使用要求的系统提示词。"""

    return (
        "You are CodeForge, a local coding agent. "
        f"Your workspace is {workspace.resolve()}. "
        "Use the provided tools to inspect, edit, and test code. "
        "Never assume a tool succeeded: read its result and recover from errors. "
        "Keep all file operations inside the workspace."
    )
