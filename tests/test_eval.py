"""验证全部评测案例加载、临时 Git Workspace、确定性断言和指标报告。

任务流位置：这些测试不访问真实 Provider；它们保证 ``python -m eval.benchmark``
在 --live 前能可靠校验 10 个能力案例和 15 个日常案例，并保存诊断信息。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from eval.assertions import AssertionContext, evaluate_assertions
from eval.benchmark import (
    DEFAULT_CASES_DIR,
    load_case_results,
    load_cases,
    merge_case_results,
    prepare_workspace,
)
from eval.metrics import build_summary, render_markdown_report
from eval.models import (
    AssertionOutcome,
    CaseResult,
    EvalAssertion,
)
from harness.agent.state import AgentState


def test_all_natural_language_cases_validate() -> None:
    """验证 25 个案例唯一有序，且日常 Prompt 不点名内部工具。"""

    cases = load_cases(DEFAULT_CASES_DIR)

    assert [case.id for case in cases] == [
        *[f"D{number:02d}" for number in range(1, 16)],
        *[f"E{number:02d}" for number in range(1, 11)],
    ]
    assert all(case.turns for case in cases)
    assert all(
        len(turn.prompt.strip()) >= 20
        for case in cases
        for turn in case.turns
    )
    internal_tool_names = {
        "subagent_spawn",
        "delegate_readonly",
        "todo_write",
        "memory_write",
        "read_skill",
        "background_start",
        "mcp_workspace_project_stats",
    }
    daily_prompts = [
        turn.prompt
        for case in cases
        if case.suite == "daily"
        for turn in case.turns
    ]
    assert len([case for case in cases if case.suite == "daily"]) == 15
    assert all(
        tool not in prompt
        for prompt in daily_prompts
        for tool in internal_tool_names
    )


def test_prepare_workspace_creates_clean_git_baseline(
    tmp_path: Path,
) -> None:
    """验证案例 Fixture 被安全写入并形成干净的初始 Git Commit。"""

    if shutil.which("git") is None:
        pytest.skip("git executable is required")
    case = next(
        case for case in load_cases(DEFAULT_CASES_DIR) if case.id == "E01"
    )

    workspace = prepare_workspace(case, tmp_path)

    assert (workspace / "src" / "config.py").is_file()
    assert (workspace / ".git").is_dir()
    assert shutil.which("git") is not None


def test_deterministic_assertions_use_files_audit_and_todos(
    tmp_path: Path,
) -> None:
    """验证断言无需额外 LLM 即可同时检查文件、审计和 Todo。"""

    (tmp_path / "result.txt").write_text("Aurora-17\n", encoding="utf-8")
    state = AgentState(
        todos=[{"id": "done", "content": "finish", "status": "completed"}]
    )
    assertions = [
        EvalAssertion(
            kind="file_contains",
            path="result.txt",
            text="Aurora-17",
        ),
        EvalAssertion(kind="audit_tool_used", tool="write_file"),
        EvalAssertion(kind="todo_all_completed"),
    ]
    context = AssertionContext(
        workspace=tmp_path,
        stdout="done",
        stderr="",
        states=[state],
        audit_records=[
            {
                "tool_name": "write_file",
                "result": {"success": True},
                "permission": {"granted": True},
            }
        ],
    )

    outcomes = evaluate_assertions(assertions, context)

    assert all(outcome.passed for outcome in outcomes)


def test_metrics_report_success_safety_and_efficiency() -> None:
    """验证汇总指标和 Markdown 报告包含 P0 展示所需的核心数据。"""

    results = [
        CaseResult(
            case_id="E01",
            name="pass",
            category="coding",
            passed=True,
            exit_codes=[0],
            duration_seconds=2.0,
            steps=2,
            tool_calls=3,
            tool_failures=0,
            assertions=[
                AssertionOutcome(
                    kind="tests_pass",
                    passed=True,
                    detail="ok",
                )
            ],
            artifact_dir="artifacts/E01",
            tags=["recovery"],
        ),
        CaseResult(
            case_id="E02",
            name="fail",
            category="coding",
            passed=False,
            exit_codes=[1],
            duration_seconds=4.0,
            steps=4,
            tool_calls=5,
            tool_failures=1,
            assertions=[
                AssertionOutcome(
                    kind="outside_sentinel_unchanged",
                    passed=False,
                    detail="changed",
                    safety=True,
                )
            ],
            artifact_dir="artifacts/E02",
        ),
    ]

    summary = build_summary(results)
    report = render_markdown_report(
        results,
        summary,
        model="demo-model",
        git_commit="abc123",
    )

    assert summary["task_success_rate"] == 0.5
    assert summary["safety_violations"] == 1
    assert summary["median_tool_calls"] == 4.0
    assert 0 <= summary["comprehensive_score"] <= 100
    assert set(summary["score_components"]) == {
        "task_completion",
        "engineering_quality",
        "safety",
        "tool_reliability",
        "capability_selection",
    }
    assert "demo-model" in report
    assert "Comprehensive score" in report
    assert "E02 / outside_sentinel_unchanged" in report


def test_incremental_results_replace_same_case_id(tmp_path: Path) -> None:
    """验证增量复测只覆盖同名旧案例，并保留其他已有结果。"""

    old_one = CaseResult(
        case_id="D01",
        name="old one",
        category="daily",
        passed=False,
        exit_codes=[1],
        duration_seconds=1,
        artifact_dir="old/D01",
    )
    old_two = CaseResult(
        case_id="D02",
        name="old two",
        category="daily",
        passed=True,
        exit_codes=[0],
        duration_seconds=1,
        artifact_dir="old/D02",
    )
    new_one = old_one.model_copy(
        update={"passed": True, "exit_codes": [0], "artifact_dir": "new/D01"}
    )
    path = tmp_path / "cases.jsonl"
    path.write_text(
        old_one.model_dump_json() + "\n" + old_two.model_dump_json() + "\n",
        encoding="utf-8",
    )

    merged = merge_case_results(load_case_results(path), [new_one])

    assert [result.case_id for result in merged] == ["D01", "D02"]
    assert merged[0].passed
    assert merged[0].artifact_dir == "new/D01"
    assert merged[1].artifact_dir == "old/D02"
