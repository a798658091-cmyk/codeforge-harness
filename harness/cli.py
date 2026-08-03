"""提供 CodeForge 命令行、安全、会话、Memory、委派和后台任务装配。

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
from harness.context.memory import MemoryStore
from harness.context.skills import SkillRegistry
from harness.delegation.subagent import ReadonlySubagentRunner
from harness.providers.openai_compatible import OpenAICompatibleProvider
from harness.safety.audit import AuditLogger
from harness.safety.permissions import PermissionPolicy, PermissionRequest
from harness.storage.sqlite import SQLiteSessionStore, SessionNotFoundError
from harness.tasks.background import BackgroundJobManager
from harness.tasks.notifications import NotificationCenter
from harness.tasks.todo import TodoList
from harness.tools import build_default_registry


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
        "--audit-log",
        help="workspace-relative JSONL audit path",
    )
    parser.add_argument(
        "--no-audit",
        action="store_true",
        help="disable JSONL tool audit logging",
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
        registry = build_default_registry(workspace)
        for name in registry.names():
            print(name)
        return 0

    prompt = " ".join(args.prompt).strip()
    if not prompt:
        prompt = input("CodeForge task> ").strip()
    if not prompt:
        print("error: prompt cannot be empty", file=sys.stderr)
        return 2

    background_manager: BackgroundJobManager | None = None
    notification_center: NotificationCenter | None = None
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
        memory_context = (
            memory_store.render_relevant(prompt)
            if memory_store is not None
            else ""
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
        registry = build_default_registry(
            workspace,
            permission_policy=permission_policy,
            approval_handler=_approve_tool_call,
            audit_logger=audit_logger,
            todo_list=todo_list,
            skill_registry=skill_registry,
            memory_store=memory_store,
            subagent_runner=subagent_runner,
            background_manager=background_manager,
            notification_center=notification_center,
        )
        loop = AgentLoop(
            provider=provider,
            registry=registry,
            system_prompt=build_system_prompt(
                settings.workspace,
                skill_catalog=skill_registry.catalog(),
                active_skills=active_skills,
                memory_context=memory_context,
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
        )
        try:
            result = loop.run(prompt, initial_state=initial_state)
        finally:
            # Provider 或模型循环出现未预料异常时也不能遗留后台进程。
            _shutdown_background(background_manager)
    except (
        ConfigurationError,
        KeyError,
        SessionNotFoundError,
        ValueError,
        FileNotFoundError,
    ) as exc:
        _shutdown_background(background_manager)
        _print_notifications(notification_center)
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except AgentLoopLimitError as exc:
        _shutdown_background(background_manager)
        _print_notifications(notification_center)
        print(f"agent stopped: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        _shutdown_background(background_manager)
        _print_notifications(notification_center)
        print("\ninterrupted", file=sys.stderr)
        return 130

    _shutdown_background(background_manager)
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
    _print_notifications(notification_center)
    return 0


def _shutdown_background(
    manager: BackgroundJobManager | None,
) -> None:
    """在所有 CLI 退出路径终止仍在运行的后台作业。"""

    if manager is not None:
        manager.shutdown(cancel_running=True)


def _print_notifications(center: NotificationCenter | None) -> None:
    """把退出时仍未读取的运行时通知输出到终端。"""

    if center is None:
        return
    for notification in center.list(unread_only=True):
        print(
            f"[notification:{notification.level}] "
            f"{notification.title}: {notification.message}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
