"""实现支持 Todo、压缩和检查点恢复的原生 Agent Loop。

任务流位置：CLI 完成组件装配后进入这里；本模块反复调用 Provider 获取模型
回合，把工具调用交给 Tool Registry，将结果写回 reducer，并在关键阶段压缩或
持久化状态后发送给下一轮模型。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from harness.agent.state import AgentState, reduce_state
from harness.context.compaction import ContextCompactor
from harness.providers.base import AssistantTurn, ModelProvider
from harness.tools.registry import ToolRegistry

CheckpointCallback = Callable[[AgentState, str], None]
CancelCheck = Callable[[], bool]
RuntimeEventCallback = Callable[[str, dict[str, Any]], None]


class AgentLoopLimitError(RuntimeError):
    """Agent 执行轮数超过配置上限时抛出的异常。"""

    pass


class AgentLoopCancelledError(RuntimeError):
    """外部协作任务请求取消时，用于安全结束当前 Agent Loop。"""

    pass


@dataclass(frozen=True)
class AgentRunResult:
    """封装 Agent 的最终回答及完整运行状态。"""

    answer: str
    state: AgentState


class AgentLoop:
    """不依赖图框架的“模型 → 工具 → 结果”原生执行循环。"""

    def __init__(
        self,
        *,
        provider: ModelProvider,
        registry: ToolRegistry,
        system_prompt: str,
        max_steps: int = 20,
        compactor: ContextCompactor | None = None,
        checkpoint_callback: CheckpointCallback | None = None,
        cancel_check: CancelCheck | None = None,
        event_callback: RuntimeEventCallback | None = None,
    ) -> None:
        """注入 Provider、工具、安全上下文、压缩器和检查点回调。"""

        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self.provider = provider
        self.registry = registry
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.compactor = compactor
        self.checkpoint_callback = checkpoint_callback
        self.cancel_check = cancel_check
        self.event_callback = event_callback

    def run(
        self,
        prompt: str,
        *,
        history: list[dict[str, Any]] | None = None,
        initial_state: AgentState | None = None,
    ) -> AgentRunResult:
        """从新状态或恢复状态执行闭环，直到得到最终文本或达到上限。"""

        if history is not None and initial_state is not None:
            raise ValueError("history and initial_state cannot be used together")
        state = (
            AgentState.from_dict(initial_state.to_dict())
            if initial_state is not None
            else AgentState(messages=list(history or []))
        )
        self._restore_or_initialize_todos(state, resumed=initial_state is not None)
        if not state.messages or state.messages[0].get("role") != "system":
            state.messages.insert(
                0, {"role": "system", "content": self.system_prompt}
            )
        else:
            # 恢复时刷新系统提示词，使新的 Skills 配置能够在本轮生效。
            state.messages[0] = {
                "role": "system",
                "content": self.system_prompt,
            }
        if self._close_interrupted_tool_calls(state):
            self._checkpoint(state, "resume_recovery")
        reduce_state(
            state,
            {"type": "message", "message": {"role": "user", "content": prompt}},
        )
        self._checkpoint(state, "user_message")
        self._emit("run.started", {"steps": state.steps})

        for _ in range(self.max_steps):
            self._raise_if_cancelled()
            self._compact_if_needed(state)
            reduce_state(state, {"type": "step"})
            self._emit("model.started", {"step": state.steps})
            turn = self.provider.complete(
                state.messages,
                self.registry.schemas(),
            )
            self._raise_if_cancelled()
            reduce_state(state, {"type": "usage", "usage": turn.usage})
            self._emit(
                "model.completed",
                {
                    "step": state.steps,
                    "usage": turn.usage,
                    "prompt_tokens": state.prompt_tokens,
                    "completion_tokens": state.completion_tokens,
                },
            )
            self._append_assistant_turn(state, turn)
            self._checkpoint(state, "assistant_turn")

            if not turn.tool_calls:
                self._checkpoint(state, "completed")
                self._emit("run.completed", {"steps": state.steps})
                return AgentRunResult(answer=turn.content, state=state)

            for call in turn.tool_calls:
                self._raise_if_cancelled()
                self._emit(
                    "tool.started",
                    {"step": state.steps, "tool": call.name, "call_id": call.id},
                )
                result = self.registry.dispatch(call.name, call.arguments)
                reduce_state(
                    state,
                    {
                        "type": "tool_result",
                        "success": result.success,
                    },
                )
                content = result.content
                if not result.success:
                    content = f"[tool_error:{result.error_type}] {content}"
                reduce_state(
                    state,
                    {
                        "type": "message",
                        "message": {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "name": call.name,
                            "content": content,
                        },
                    },
                )
                self._sync_todos_to_state(state)
                self._emit(
                    "tool.completed",
                    {
                        "step": state.steps,
                        "tool": call.name,
                        "call_id": call.id,
                        "success": result.success,
                        "duration_ms": result.duration_ms,
                        "error_type": result.error_type,
                    },
                )
                self._emit("todos.updated", {"todos": state.todos})
                self._checkpoint(state, "tool_result")

        self._checkpoint(state, "step_limit")
        self._emit("run.failed", {"reason": "step_limit"})
        raise AgentLoopLimitError(
            f"agent exceeded max_steps={self.max_steps}"
        )

    def _raise_if_cancelled(self) -> None:
        """在模型回合和工具调用边界响应外部的协作取消信号。"""

        if self.cancel_check is not None and self.cancel_check():
            raise AgentLoopCancelledError("agent run was cancelled")

    @staticmethod
    def _append_assistant_turn(
        state: AgentState,
        turn: AssistantTurn,
    ) -> None:
        """把标准化模型回合转换为 assistant 消息并写入状态。"""

        message: dict[str, Any] = {
            "role": "assistant",
            "content": turn.content,
        }
        if turn.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                        ),
                    },
                }
                for call in turn.tool_calls
            ]
        reduce_state(
            state,
            {"type": "message", "message": message},
        )

    def _compact_if_needed(self, state: AgentState) -> None:
        """压缩超出预算的上下文，并为压缩结果创建检查点。"""

        if self.compactor is None:
            return
        result = self.compactor.compact(state.messages)
        if not result.compacted:
            return
        reduce_state(
            state,
            {"type": "compaction", "messages": result.messages},
        )
        self._checkpoint(state, "context_compacted")
        self._emit(
            "context.compacted",
            {"compactions": state.compactions},
        )

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """向可选观测回调发送事件，并隔离前端日志异常。"""

        if self.event_callback is None:
            return
        try:
            self.event_callback(event_type, payload)
        except Exception:
            return

    def _restore_or_initialize_todos(
        self,
        state: AgentState,
        *,
        resumed: bool,
    ) -> None:
        """让 ToolContext 的 TodoList 与新建或恢复的 AgentState 对齐。"""

        todo_list = self.registry.context.todo_list
        if todo_list is None:
            return
        if resumed:
            todo_list.replace(state.todos)
        else:
            reduce_state(
                state,
                {"type": "todos", "todos": todo_list.snapshot()},
            )

    def _sync_todos_to_state(self, state: AgentState) -> None:
        """在工具执行后把共享 Todo 清单复制回 AgentState。"""

        todo_list = self.registry.context.todo_list
        if todo_list is not None:
            reduce_state(
                state,
                {"type": "todos", "todos": todo_list.snapshot()},
            )

    def _checkpoint(self, state: AgentState, reason: str) -> None:
        """在配置持久化回调时保存当前状态和阶段原因。"""

        if self.checkpoint_callback is not None:
            self.checkpoint_callback(state, reason)

    @staticmethod
    def _close_interrupted_tool_calls(state: AgentState) -> bool:
        """为恢复时尚无结果的工具调用补充中断错误，避免非法消息序列。"""

        pending: list[dict[str, Any]] = []
        completed_ids: set[str] = set()
        for message in reversed(state.messages):
            if message.get("role") == "assistant" and message.get("tool_calls"):
                pending = list(message["tool_calls"])
                break
            if message.get("role") == "tool" and message.get("tool_call_id"):
                completed_ids.add(str(message["tool_call_id"]))
            elif message.get("role") in {"user", "assistant"}:
                return False
        changed = False
        for call in pending:
            call_id = str(call.get("id", ""))
            if not call_id or call_id in completed_ids:
                continue
            function = call.get("function") or {}
            reduce_state(
                state,
                {
                    "type": "message",
                    "message": {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": str(function.get("name", "unknown")),
                        "content": (
                            "[tool_error:interrupted] Tool call did not "
                            "finish before session resume; reassess before "
                            "retrying."
                        ),
                    },
                },
            )
            changed = True
        return changed
