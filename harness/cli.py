from __future__ import annotations

import argparse
import sys
from pathlib import Path

from harness.agent.loop import AgentLoop, AgentLoopLimitError
from harness.agent.prompt import build_system_prompt
from harness.config import ConfigurationError, Settings
from harness.providers.openai_compatible import OpenAICompatibleProvider
from harness.tools import build_default_registry


def build_parser() -> argparse.ArgumentParser:
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
        "--list-tools",
        action="store_true",
        help="show registered tools and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = Path(args.workspace).expanduser().resolve()
    registry = build_default_registry(workspace)
    if args.list_tools:
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
