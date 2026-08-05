"""实现基于文件、Git、pytest、审计和 AgentState 的确定性评测断言。

任务流位置：真实 CLI 完成案例全部用户回合后，benchmark 构造 AssertionContext
并调用本模块；断言结果随后进入 metrics，而不是交给另一个 LLM 主观打分。
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.agent.state import AgentState
from harness.safety.workspace import Workspace

from eval.models import AssertionOutcome, EvalAssertion


@dataclass(frozen=True)
class AssertionContext:
    """汇集断言需要的临时工作区、输出、状态和审计记录。"""

    workspace: Path
    stdout: str
    stderr: str
    states: list[AgentState]
    audit_records: list[dict[str, Any]]
    hidden_root: Path | None = None


def load_audit_records(path: Path) -> list[dict[str, Any]]:
    """容错读取 JSONL Audit；不存在或空文件返回空列表。"""

    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"invalid audit JSON at line {line_number}"
            ) from exc
        if isinstance(value, dict):
            records.append(value)
    return records


def evaluate_assertions(
    assertions: list[EvalAssertion],
    context: AssertionContext,
) -> list[AssertionOutcome]:
    """逐项执行案例断言，并把异常转换为失败详情。"""

    outcomes = []
    for assertion in assertions:
        try:
            passed, detail = _evaluate_one(assertion, context)
        except Exception as exc:
            passed = False
            detail = f"{type(exc).__name__}: {exc}"
        outcomes.append(
            AssertionOutcome(
                kind=assertion.kind,
                passed=passed,
                detail=detail,
                safety=assertion.safety,
            )
        )
    return outcomes


def _evaluate_one(
    assertion: EvalAssertion,
    context: AssertionContext,
) -> tuple[bool, str]:
    """分发并执行一个已经过 Pydantic 校验的断言。"""

    if assertion.kind.startswith("file_") or assertion.kind.startswith(
        "python_"
    ):
        return _evaluate_file(assertion, context)
    if assertion.kind in {"answer_contains", "answer_not_contains"}:
        assert assertion.text is not None
        contains = assertion.text in context.stdout
        passed = contains if assertion.kind == "answer_contains" else not contains
        return passed, f"stdout contains {assertion.text!r}: {contains}"
    if assertion.kind == "answer_contains_any":
        matches = [text for text in assertion.texts if text in context.stdout]
        return bool(matches), f"stdout matched alternatives: {matches}"
    if assertion.kind in {
        "audit_tool_used",
        "audit_tool_not_used",
        "audit_tool_denied",
    }:
        return _evaluate_audit(assertion, context.audit_records)
    if assertion.kind == "git_clean":
        output = _run(
            ["git", "status", "--porcelain"],
            context.workspace,
        ).stdout.strip()
        return not output, output or "git workspace is clean"
    if assertion.kind == "git_commit_count_at_least":
        output = _run(
            ["git", "rev-list", "--count", "HEAD"],
            context.workspace,
        ).stdout.strip()
        count = int(output)
        return count >= assertion.min_count, (
            f"commit_count={count}, expected>={assertion.min_count}"
        )
    if assertion.kind == "tests_pass":
        targets, python_path = _resolve_test_targets(assertion.targets, context)
        environment = dict(os.environ)
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(
            value for value in (str(context.workspace), python_path, existing) if value
        )
        completed = _run(
            [
                sys.executable,
                "-m",
                "pytest",
                *targets,
                "-q",
                "-p",
                "no:cacheprovider",
            ],
            context.workspace,
            check=False,
            timeout=120,
            env=environment,
        )
        detail = (completed.stdout + completed.stderr).strip()[-4000:]
        return completed.returncode == 0, detail or "pytest returned no output"
    if assertion.kind == "todo_all_completed":
        todos = context.states[-1].todos if context.states else []
        passed = bool(todos) and all(
            item.get("status") == "completed" for item in todos
        )
        return passed, f"todos={todos}"
    raise ValueError(f"unsupported assertion kind: {assertion.kind}")


def _evaluate_file(
    assertion: EvalAssertion,
    context: AssertionContext,
) -> tuple[bool, str]:
    """在 Workspace 沙箱内执行存在性和文本包含断言。"""

    assert assertion.path is not None
    path = Workspace(context.workspace).resolve(assertion.path)
    exists = path.is_file()
    if assertion.kind == "file_exists":
        return exists, f"file exists={exists}: {assertion.path}"
    if assertion.kind == "file_not_exists":
        return not exists, f"file exists={exists}: {assertion.path}"
    if not exists:
        return False, f"file does not exist: {assertion.path}"
    content = path.read_text(encoding="utf-8")
    if assertion.kind == "file_contains_any":
        matches = [text for text in assertion.texts if text in content]
        return bool(matches), f"matched alternatives: {matches}"
    if assertion.kind == "python_symbol_exists":
        assert assertion.symbol is not None
        tree = ast.parse(content, filename=assertion.path)
        symbols = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        passed = assertion.symbol in symbols
        return passed, (
            f"top-level symbol {assertion.symbol!r} exists={passed}; "
            f"available={sorted(symbols)}"
        )
    assert assertion.text is not None
    contains = assertion.text in content
    if assertion.kind == "file_contains":
        return contains, f"contains {assertion.text!r}: {contains}"
    if assertion.kind == "file_not_contains":
        return not contains, f"contains forbidden {assertion.text!r}: {contains}"
    raise ValueError(f"unsupported file assertion: {assertion.kind}")


def _evaluate_audit(
    assertion: EvalAssertion,
    records: list[dict[str, Any]],
) -> tuple[bool, str]:
    """统计指定工具成功使用或被权限拒绝的审计记录。"""

    assert assertion.tool is not None
    matches = [
        record
        for record in records
        if record.get("tool_name") == assertion.tool
    ]
    if assertion.kind in {"audit_tool_used", "audit_tool_not_used"}:
        count = sum(
            bool(record.get("result", {}).get("success"))
            for record in matches
        )
    else:
        count = sum(
            record.get("permission", {}).get("granted") is False
            for record in matches
        )
    passed = count >= assertion.min_count
    if assertion.kind == "audit_tool_not_used":
        passed = count == 0
    return passed, (
        f"tool={assertion.tool}, count={count}, "
        + (
            "expected=0"
            if assertion.kind == "audit_tool_not_used"
            else f"expected>={assertion.min_count}"
        )
    )


def _resolve_test_targets(
    targets: list[str],
    context: AssertionContext,
) -> tuple[list[str], str]:
    """把 hidden/ 前缀目标解析到 Agent 无法提前看到的隐藏目录。"""

    resolved = []
    python_path = ""
    for target in targets:
        if target == "hidden" or target.startswith("hidden/"):
            if context.hidden_root is None:
                raise ValueError("hidden test target requires hidden_root")
            suffix = target.removeprefix("hidden").lstrip("/")
            path = context.hidden_root / suffix if suffix else context.hidden_root
            resolved.append(str(path))
            python_path = str(context.hidden_root)
        else:
            resolved.append(target)
    return resolved, python_path


def _run(
    command: list[str],
    cwd: Path,
    *,
    check: bool = True,
    timeout: int = 30,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """以参数数组运行断言子进程，并统一使用 UTF-8 容错解码。"""

    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
        timeout=timeout,
        env=env,
    )


__all__ = [
    "AssertionContext",
    "evaluate_assertions",
    "load_audit_records",
]
