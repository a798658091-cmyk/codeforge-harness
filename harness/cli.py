"""提供 CodeForge 命令行参数、安全配置、依赖装配和退出码处理。

任务流位置：承接 ``python -m harness`` 入口，读取配置并创建 Provider、默认
Tool Registry、安全策略、审计和 Agent Loop，最后启动任务并输出结果。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harness.agent.loop import AgentLoop, AgentLoopLimitError
from harness.agent.prompt import build_system_prompt
from harness.config import ConfigurationError, Settings
from harness.providers.openai_compatible import OpenAICompatibleProvider
from harness.safety.audit import AuditLogger
from harness.safety.permissions import PermissionPolicy, PermissionRequest
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

    try:
        settings = Settings.from_env(
            workspace=workspace,
            model=args.model,
            base_url=args.base_url,
            permission_default=args.permission_default,
            permission_rules=",".join(args.permission),
            audit_log=args.audit_log,
            disable_audit=args.no_audit,
        )
        permission_policy = PermissionPolicy(
            settings.permission_rules,
            default=settings.permission_default,
        )
        audit_logger = (
            AuditLogger(settings.audit_log)
            if settings.audit_log is not None
            else None
        )
        registry = build_default_registry(
            workspace,
            permission_policy=permission_policy,
            approval_handler=_approve_tool_call,
            audit_logger=audit_logger,
        )
        provider = OpenAICompatibleProvider(
            model=settings.model,
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.request_timeout,
            max_tokens=settings.max_tokens,
        )
        loop = AgentLoop(
            provider=provider,
            registry=registry,
            system_prompt=build_system_prompt(settings.workspace),
            max_steps=args.max_steps,
        )
        result = loop.run(prompt)
    except (ConfigurationError, ValueError, FileNotFoundError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except AgentLoopLimitError as exc:
        print(f"agent stopped: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130

    print(result.answer)
    print(
        f"\n[steps={result.state.steps}, "
        f"tools={result.state.tool_calls}, "
        f"failures={result.state.tool_failures}]"
    )
    if settings.audit_log is not None:
        print(f"[audit={settings.audit_log}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
