"""构造约束本地 Coding Agent 行为的系统提示词。

任务流位置：CLI 在创建 Agent Loop 前调用本模块，把解析后的 workspace 边界
写入系统消息；该消息随后作为每次模型对话的首条上下文。
"""

from __future__ import annotations

from pathlib import Path


def build_system_prompt(
    workspace: Path,
    *,
    skill_catalog: str | None = None,
    active_skills: str | None = None,
    memory_context: str | None = None,
) -> str:
    """生成包含 workspace、Todo、Skills、Memory 和委派要求的提示词。"""

    prompt = (
        "You are CodeForge, a local coding agent. "
        f"Your workspace is {workspace.resolve()}. "
        "Use the provided tools to inspect, edit, and test code. "
        "For multi-step work, maintain a concise checklist with todo_write and "
        "keep at most one item in_progress. "
        "Workspace skills are reusable instructions: inspect the catalog and "
        "call read_skill before following a relevant skill that is not already "
        "active. "
        "Use memory_search before relying on prior project decisions, and only "
        "use memory_write for verified reusable facts without credentials. "
        "Use delegate_readonly for bounded codebase investigations that need an "
        "isolated context; it cannot modify the workspace. "
        "Long-running commands may use background_start, followed by status "
        "and output checks before claiming completion. "
        "Never assume a tool succeeded: read its result and recover from errors. "
        "Keep all file operations inside the workspace."
    )
    if skill_catalog:
        prompt += f"\n\nAvailable workspace skills:\n{skill_catalog}"
    if active_skills:
        prompt += f"\n\nPreloaded skill instructions:\n{active_skills}"
    if memory_context:
        prompt += f"\n\nRelevant verified workspace memories:\n{memory_context}"
    return prompt
