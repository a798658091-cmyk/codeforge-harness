from pathlib import Path

from harness.tools import build_default_registry


def test_day1_hard_deny_blocks_destructive_shell_command(
    workspace: Path,
) -> None:
    registry = build_default_registry(workspace)

    result = registry.dispatch(
        "shell",
        {"command": "git reset --hard HEAD"},
    )

    assert result.success is False
    assert "hard deny" in result.content


# Full allow/ask/deny policy tests are implemented in the Day 2 milestone.
