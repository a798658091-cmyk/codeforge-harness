"""提供测试公共 fixture，以及可选的逐阶段终端报告能力。

任务流位置：位于 pytest 收集与各测试用例之间，为工具和 Agent Loop 测试创建
彼此隔离的临时 workspace、默认 Tool Registry，并展示测试执行阶段。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.tools import build_default_registry
from harness.tools.registry import ToolRegistry
from harness.safety.permissions import PermissionPolicy


def pytest_addoption(parser: pytest.Parser) -> None:
    """注册用于显示逐项测试过程的 CodeForge pytest 选项。"""

    group = parser.getgroup("codeforge")
    group.addoption(
        "--show-test-process",
        action="store_true",
        help=(
            "show each test's start, temporary workspace, call result, "
            "and teardown result"
        ),
    )


def _process_reporter(item: pytest.Item):
    """在启用详细过程时返回 pytest 终端报告器。"""

    if not item.config.getoption("--show-test-process"):
        return None
    return item.config.pluginmanager.get_plugin("terminalreporter")


def pytest_runtest_setup(item: pytest.Item) -> None:
    """在每项测试 setup 前输出醒目的开始标记。"""

    reporter = _process_reporter(item)
    if reporter is not None:
        reporter.write_sep("-", f"START {item.nodeid}")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """输出测试主体及 teardown 的结果、状态和耗时。"""

    outcome = yield
    report = outcome.get_result()
    reporter = _process_reporter(item)
    if reporter is None:
        return

    if report.when == "call":
        reporter.write_line(
            f"[RESULT] {report.outcome.upper()} "
            f"({report.duration:.3f}s)"
        )
    elif report.when == "setup" and report.failed:
        reporter.write_line("[SETUP] FAILED")
    elif report.when == "teardown":
        reporter.write_line(
            f"[TEARDOWN] {report.outcome.upper()} "
            f"({report.duration:.3f}s)"
        )
        reporter.write_sep("-", f"END {item.nodeid}")


@pytest.fixture
def workspace(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    """创建包含示例源码的独立临时 workspace。"""

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "sample.py").write_text(
        "def greet(name):\n"
        "    return f'hello {name}'\n",
        encoding="utf-8",
    )
    if request.config.getoption("--show-test-process"):
        reporter = request.config.pluginmanager.get_plugin(
            "terminalreporter"
        )
        if reporter is not None:
            reporter.write_line(f"[WORKSPACE] {tmp_path}")
    return tmp_path


@pytest.fixture
def registry(workspace: Path) -> ToolRegistry:
    """为当前临时 workspace 构造默认 Tool Registry。"""

    return build_default_registry(
        workspace,
        permission_policy=PermissionPolicy(default="allow"),
    )
