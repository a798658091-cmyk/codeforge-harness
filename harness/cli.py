"""提供 CodeForge CLI、安全、会话、Memory、MCP、委派和后台任务装配。

任务流位置：承接 ``python -m harness`` 入口，读取配置并创建 Provider、默认
Tool Registry、安全策略、SQLite 检查点、上下文管理和 Agent Loop，最后启动
或恢复任务并输出结果。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harness.agent.loop import AgentLoop, AgentLoopLimitError
from harness.agent.prompt import build_system_prompt
from harness.config import ConfigurationError, Settings
from harness.context.compaction import ContextCompactor
from harness.context.memory import (
    MemoryStore,
    extract_explicit_memory_intents,
)
from harness.context.skills import SkillRegistry
from harness.delegation.message_bus import MessageBus
from harness.delegation.subagent import ReadonlySubagentRunner
from harness.delegation.team import WritableSubagentManager
from harness.mcp_stdio import (
    MCPError,
    StdioMCPManager,
    load_mcp_config,
)
from harness.providers.openai_compatible import OpenAICompatibleProvider
from harness.runtime_events import RuntimeEventLogger
from harness.safety.audit import AuditLogger
from harness.safety.permissions import PermissionPolicy, PermissionRequest
from harness.safety.workspace import Workspace
from harness.storage.sqlite import SQLiteSessionStore, SessionNotFoundError
from harness.tasks.background import BackgroundJobManager
from harness.tasks.notifications import NotificationCenter
from harness.tasks.todo import TodoList
from harness.tools import build_default_registry
from harness.tools.registry import ToolRegistry
from harness.worktrees import WorktreeError, WorktreeManager


def _approve_tool_call(request: PermissionRequest) -> bool:
    """在终端展示 ask 类型工具调用并读取用户确认。"""

    arguments = json.dumps(
        request.arguments,
        ensure_ascii=False,
        default=str,
    )
    if len(arguments) > 2000:
        arguments = f"{arguments[:2000]}..."
    print(
        f"\n[permission:ask] tool={request.tool_name}\n"
        f"arguments={arguments}",
        file=sys.stderr,
    )
    answer = input("Allow this tool call? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def _auto_approve_tool_call(request: PermissionRequest) -> bool:
    """在 --yes 模式下自动同意 ask 请求，并保留可见的审批提示。"""

    print(
        f"[permission:auto-approved] tool={request.tool_name}",
        file=sys.stderr,
    )
    return True


def build_parser() -> argparse.ArgumentParser:
    """创建并返回 CodeForge 命令行参数解析器。"""

    parser = argparse.ArgumentParser(
        prog="codeforge",
        description="A lightweight local coding-agent harness.",
    )
    parser.add_argument("prompt", nargs="*", help="coding task")
    parser.add_argument(
        "--workspace",
        default=".",
        help="workspace boundary (default: current directory)",
    )
    parser.add_argument("--model", help="override CODEFORGE_MODEL")
    parser.add_argument("--base-url", help="OpenAI-compatible API base URL")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument(
        "--permission-default",
        choices=("allow", "ask", "deny"),
        help="default tool permission decision",
    )
    parser.add_argument(
        "--permission",
        action="append",
        default=[],
        metavar="TOOL=DECISION",
        help="override one tool or glob rule; may be repeated",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help=(
            "automatically approve ask decisions; deny rules, hooks, "
            "hard-deny checks, and workspace sandboxing still apply"
        ),
    )
    parser.add_argument(
        "--audit-log",
        help="workspace-relative JSONL audit path",
    )
    parser.add_argument(
        "--no-audit",
        action="store_true",
        help="disable JSONL tool audit logging",
    )
    parser.add_argument(
        "--runtime-event-log",
        help="workspace-relative JSONL event stream for a local dashboard",
    )
    session_group = parser.add_mutually_exclusive_group()
    session_group.add_argument(
        "--resume",
        metavar="SESSION_ID",
        help="resume a session ID, or use 'latest' for this workspace",
    )
    session_group.add_argument(
        "--session-id",
        help="use a chosen ID for a new session",
    )
    parser.add_argument(
        "--session-db",
        help="workspace-relative SQLite session database path",
    )
    parser.add_argument(
        "--no-session",
        action="store_true",
        help="disable SQLite checkpoints for this run",
    )
    parser.add_argument(
        "--no-compaction",
        action="store_true",
        help="disable deterministic context compaction",
    )
    parser.add_argument(
        "--context-max-messages",
        type=int,
        help="compact before a provider call above this message count",
    )
    parser.add_argument(
        "--context-keep-recent",
        type=int,
        help="number of recent messages retained after compaction",
    )
    parser.add_argument(
        "--skill",
        action="append",
        default=[],
        metavar="NAME",
        help="preload a workspace skill; may be repeated",
    )
    parser.add_argument(
        "--memory-db",
        help="workspace-relative SQLite long-term memory path",
    )
    parser.add_argument(
        "--no-memory",
        action="store_true",
        help="disable long-term memory recall and tools",
    )
    parser.add_argument(
        "--subagent-max-steps",
        type=int,
        help="default maximum steps for the read-only subagent",
    )
    parser.add_argument(
        "--max-subagents",
        type=int,
        default=2,
        choices=range(1, 5),
        metavar="1..4",
        help="maximum concurrent writable subagents (default: 2)",
    )
    parser.add_argument(
        "--mcp-config",
        help=(
            "workspace-relative JSON config for explicitly trusted stdio "
            "MCP servers"
        ),
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="show registered tools and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """装配运行组件、执行用户任务并返回进程退出码。"""

    args = build_parser().parse_args(argv)
    workspace = Path(args.workspace).expanduser().resolve()
    if args.list_tools:
        return _list_registered_tools(workspace, args.mcp_config)

    prompt = " ".join(args.prompt).strip()
    if not prompt:
        prompt = input("CodeForge task> ").strip()
    if not prompt:
        print("error: prompt cannot be empty", file=sys.stderr)
        return 2

    background_manager: BackgroundJobManager | None = None
    notification_center: NotificationCenter | None = None
    mcp_manager: StdioMCPManager | None = None
    writable_subagents: WritableSubagentManager | None = None
    message_bus: MessageBus | None = None
    captured_memory_keys: list[str] = []
    try:
        settings = Settings.from_env(
            workspace=workspace,
            model=args.model,
            base_url=args.base_url,
            permission_default=args.permission_default,
            permission_rules=",".join(args.permission),
            audit_log=args.audit_log,
            disable_audit=args.no_audit,
            session_db=args.session_db,
            disable_sessions=args.no_session,
            memory_db=args.memory_db,
            disable_memory=args.no_memory,
            context_max_messages=args.context_max_messages,
            context_keep_recent=args.context_keep_recent,
            subagent_max_steps=args.subagent_max_steps,
        )
        if args.resume and settings.session_db is None:
            raise ConfigurationError("--resume requires session storage")

        store = (
            SQLiteSessionStore(settings.session_db)
            if settings.session_db is not None
            else None
        )
        initial_state = None
        session_id = None
        if args.resume:
            if store is None:  # 上面的配置检查保证仅用于类型收窄
                raise ConfigurationError("session storage is disabled")
            session = (
                store.latest_session(settings.workspace)
                if args.resume == "latest"
                else store.get_session(args.resume)
            )
            if Path(session.workspace).resolve() != settings.workspace:
                raise ConfigurationError(
                    "resumed session belongs to a different workspace"
                )
            session_id = session.id
            initial_state = store.load_latest_checkpoint(session_id).state
        elif store is not None:
            session = store.create_session(
                settings.workspace,
                session_id=args.session_id,
            )
            session_id = session.id

        todo_list = TodoList(initial_state.todos if initial_state else [])
        skill_registry = SkillRegistry.discover(settings.workspace)
        memory_store = (
            MemoryStore(settings.memory_db)
            if settings.memory_db is not None
            else None
        )
        selected_skills = [
            *args.skill,
            *skill_registry.explicit_mentions(prompt),
        ]
        active_skills = skill_registry.render_selected(selected_skills)
        permission_policy = PermissionPolicy(
            settings.permission_rules,
            default=settings.permission_default,
        )
        audit_logger = (
            AuditLogger(settings.audit_log)
            if settings.audit_log is not None
            else None
        )
        runtime_event_logger = (
            RuntimeEventLogger(
                Workspace(settings.workspace).resolve(args.runtime_event_log)
            )
            if args.runtime_event_log
            else None
        )
        notification_center = NotificationCenter()
        background_manager = BackgroundJobManager(
            settings.workspace,
            notifications=notification_center,
        )
        provider = OpenAICompatibleProvider(
            model=settings.model,
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.request_timeout,
            max_tokens=settings.max_tokens,
        )
        subagent_runner = ReadonlySubagentRunner(
            provider=provider,
            workspace=settings.workspace,
            skill_registry=skill_registry,
            memory_store=memory_store,
            audit_logger=audit_logger,
            default_max_steps=settings.subagent_max_steps,
        )
        message_bus = MessageBus()
        try:
            worktree_manager = WorktreeManager(settings.workspace)
        except WorktreeError as exc:
            # 非 Git 目录仍可使用全部原有能力，只禁用依赖分支隔离的可写 Worker。
            worktree_manager = None
            print(
                f"[subagent:writable-disabled] {exc}",
                file=sys.stderr,
            )
        if worktree_manager is not None:
            writable_subagents = WritableSubagentManager(
                provider=provider,
                workspace=settings.workspace,
                worktrees=worktree_manager,
                message_bus=message_bus,
                audit_logger=audit_logger,
                max_workers=args.max_subagents,
            )
        mcp_tools = []
        if args.mcp_config:
            mcp_manager = StdioMCPManager(
                load_mcp_config(args.mcp_config, settings.workspace),
                settings.workspace,
            )
            mcp_tools = mcp_manager.start_and_discover()
        registry = build_default_registry(
            workspace,
            permission_policy=permission_policy,
            approval_handler=(
                _auto_approve_tool_call if args.yes else _approve_tool_call
            ),
            audit_logger=audit_logger,
            todo_list=todo_list,
            skill_registry=skill_registry,
            memory_store=memory_store,
            subagent_runner=subagent_runner,
            writable_subagents=writable_subagents,
            message_bus=message_bus,
            background_manager=background_manager,
            notification_center=notification_center,
        )
        for mcp_tool in mcp_tools:
            registry.register(mcp_tool)
        memory_capture_status, captured_memory_keys = (
            _capture_explicit_memories(
                prompt,
                registry,
                enabled=memory_store is not None,
            )
        )
        memory_context = (
            memory_store.render_relevant(prompt)
            if memory_store is not None
            else ""
        )
        loop = AgentLoop(
            provider=provider,
            registry=registry,
            system_prompt=build_system_prompt(
                settings.workspace,
                skill_catalog=skill_registry.catalog(),
                active_skills=active_skills,
                memory_context=memory_context,
                memory_capture_status=memory_capture_status,
            ),
            max_steps=args.max_steps,
            compactor=(
                None
                if args.no_compaction
                else ContextCompactor(
                    max_messages=settings.context_max_messages,
                    keep_recent=settings.context_keep_recent,
                    max_characters=settings.context_max_characters,
                )
            ),
            checkpoint_callback=(
                (
                    lambda state, reason: store.save_checkpoint(
                        session_id,
                        state,
                        reason=reason,
                    )
                )
                if store is not None and session_id is not None
                else None
            ),
            event_callback=(
                runtime_event_logger.emit
                if runtime_event_logger is not None
                else None
            ),
        )
        try:
            result = loop.run(prompt, initial_state=initial_state)
        finally:
            # Provider 或模型循环出现未预料异常时也不能遗留后台进程。
            _shutdown_subagents(writable_subagents)
            _shutdown_background(background_manager)
            _shutdown_mcp(mcp_manager)
    except (
        ConfigurationError,
        KeyError,
        SessionNotFoundError,
        ValueError,
        FileNotFoundError,
        MCPError,
    ) as exc:
        _shutdown_subagents(writable_subagents)
        _shutdown_background(background_manager)
        _shutdown_mcp(mcp_manager)
        _print_notifications(notification_center)
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except AgentLoopLimitError as exc:
        _shutdown_subagents(writable_subagents)
        _shutdown_background(background_manager)
        _shutdown_mcp(mcp_manager)
        _print_notifications(notification_center)
        print(f"agent stopped: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        _shutdown_subagents(writable_subagents)
        _shutdown_background(background_manager)
        _shutdown_mcp(mcp_manager)
        _print_notifications(notification_center)
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception:
        # 未知缺陷仍向上暴露，但必须先清理当前进程拥有的所有子进程。
        _shutdown_subagents(writable_subagents)
        _shutdown_background(background_manager)
        _shutdown_mcp(mcp_manager)
        raise

    _shutdown_subagents(writable_subagents)
    _shutdown_background(background_manager)
    _shutdown_mcp(mcp_manager)
    print(result.answer)
    print(
        f"\n[steps={result.state.steps}, "
        f"tools={result.state.tool_calls}, "
        f"failures={result.state.tool_failures}]"
    )
    if settings.audit_log is not None:
        print(f"[audit={settings.audit_log}]")
    if session_id is not None:
        print(f"[session={session_id}]")
    if result.state.todos:
        print(f"[todos={len(result.state.todos)}]")
    if result.state.compactions:
        print(f"[compactions={result.state.compactions}]")
    if captured_memory_keys:
        print(f"[memory-saved={', '.join(captured_memory_keys)}]")
    _print_subagents(writable_subagents)
    _print_background_jobs(background_manager)
    _print_notifications(notification_center)
    return 0


def _shutdown_background(
    manager: BackgroundJobManager | None,
) -> None:
    """在所有 CLI 退出路径终止仍在运行的后台作业。"""

    if manager is not None:
        manager.shutdown(cancel_running=True)


def _shutdown_subagents(
    manager: WritableSubagentManager | None,
) -> None:
    """在 CLI 退出路径协作取消仍在运行的 Worker 并回收线程。"""

    if manager is not None:
        manager.shutdown(cancel_running=True)


def _shutdown_mcp(manager: StdioMCPManager | None) -> None:
    """在所有 CLI 退出路径关闭已启动的 stdio MCP Server。"""

    if manager is not None:
        manager.close()


def _list_registered_tools(workspace: Path, mcp_config: str | None) -> int:
    """列出本地和可选 MCP 工具，并确保临时 Server 正常关闭。"""

    manager: StdioMCPManager | None = None
    try:
        registry = build_default_registry(workspace)
        if mcp_config:
            manager = StdioMCPManager(
                load_mcp_config(mcp_config, workspace),
                workspace,
            )
            for tool in manager.start_and_discover():
                registry.register(tool)
        for name in registry.names():
            print(name)
        return 0
    except MCPError as exc:
        print(f"MCP configuration error: {exc}", file=sys.stderr)
        return 2
    finally:
        _shutdown_mcp(manager)


def _print_notifications(center: NotificationCenter | None) -> None:
    """把退出时仍未读取的运行时通知输出到终端。"""

    if center is None:
        return
    for notification in center.list(unread_only=True):
        print(
            f"[notification:{notification.level}] "
            f"{notification.title}: {notification.message}"
        )


def _capture_explicit_memories(
    prompt: str,
    registry: ToolRegistry,
    *,
    enabled: bool,
) -> tuple[str, list[str]]:
    """通过完整工具安全链持久化用户明确要求记住的内容。"""

    intents = extract_explicit_memory_intents(prompt)
    if not intents:
        return "", []
    if not enabled:
        return "- Memory is disabled; explicit requests were not saved.", []

    status_lines: list[str] = []
    saved_keys: list[str] = []
    for intent in intents:
        result = registry.dispatch(
            "memory_write",
            {
                "key": intent.key,
                "content": intent.content,
                "tags": list(intent.tags),
            },
        )
        if result.success:
            saved_keys.append(intent.key)
            status_lines.append(f'- Saved memory "{intent.key}".')
        else:
            status_lines.append(
                f'- Failed to save memory "{intent.key}": '
                f"{result.error_type or 'unknown_error'}."
            )
    return "\n".join(status_lines), saved_keys


def _print_background_jobs(manager: BackgroundJobManager | None) -> None:
    """在任务结束时直接展示后台作业终态，避免用户手工翻找日志。"""

    if manager is None:
        return
    jobs = manager.list()
    if not jobs:
        return
    print("[background-jobs]")
    for job in jobs:
        relative_log = manager.workspace.relative(job.log_path)
        print(
            f"- {job.id}: {job.status.value}, "
            f"return_code={job.return_code}, log={relative_log}"
        )


def _print_subagents(manager: WritableSubagentManager | None) -> None:
    """在最终结果后展示本轮可写 Worker 的状态、分支和提交。"""

    if manager is None:
        return
    tasks = manager.list()
    if not tasks:
        return
    print("[subagents]")
    for task in tasks:
        print(
            f"- {task.id}: {task.status.value}, branch={task.branch}, "
            f"commit={task.commit or '-'}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
