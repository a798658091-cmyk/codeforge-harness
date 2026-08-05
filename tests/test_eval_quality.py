"""验证隐藏测试、Git 差异、静态分析和代码质量百分制评分。

任务流位置：真实 Provider 完成临时工作区任务后，benchmark 会调用这些被测函数；
本文件只构造本地 Git 仓库，不调用 LLM、LangSmith 或 Langfuse。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from eval.assertions import AssertionContext, evaluate_assertions
from eval.benchmark import (
    DEFAULT_CASES_DIR,
    _evaluation_environment,
    load_cases,
    prepare_workspace,
)
from eval.models import AssertionOutcome, EvalAssertion, EvalCase
from eval.quality import collect_git_changes, evaluate_quality


def test_hidden_tests_run_outside_agent_workspace(tmp_path: Path) -> None:
    """验证 hidden/ 测试位于独立目录但可以导入工作区代码。"""

    workspace = tmp_path / "workspace"
    hidden = tmp_path / "hidden"
    workspace.mkdir()
    hidden.mkdir()
    (workspace / "answer.py").write_text("VALUE = 42\n", encoding="utf-8")
    (hidden / "test_answer.py").write_text(
        "from answer import VALUE\n\ndef test_value():\n    assert VALUE == 42\n",
        encoding="utf-8",
    )

    outcomes = evaluate_assertions(
        [EvalAssertion(kind="tests_pass", targets=["hidden/test_answer.py"])],
        AssertionContext(
            workspace=workspace,
            stdout="",
            stderr="",
            states=[],
            audit_records=[],
            hidden_root=hidden,
        ),
    )

    assert outcomes[0].passed, outcomes[0].detail


def test_quality_scores_good_change_and_detects_forbidden_path(
    tmp_path: Path,
) -> None:
    """验证正常小改动获高分，而禁止路径会触发安全硬门。"""

    if shutil.which("git") is None:
        pytest.skip("git executable is required")
    case = next(
        case for case in load_cases(DEFAULT_CASES_DIR) if case.id == "D02"
    )
    workspace = prepare_workspace(case, tmp_path)
    baseline = _git(workspace, "rev-parse", "HEAD")
    (workspace / "src" / "config.py").write_text(
        "def parse_timeout(value, default=30):\n"
        "    \"\"\"Parse an optional timeout.\"\"\"\n"
        "    if value is None or not str(value).strip():\n"
        "        return default\n"
        "    return int(value)\n",
        encoding="utf-8",
    )
    outcomes = [
        AssertionOutcome(kind="tests_pass", passed=True, detail="ok"),
        AssertionOutcome(kind="file_not_contains", passed=True, detail="ok"),
        AssertionOutcome(
            kind="outside_sentinel_unchanged",
            passed=True,
            detail="ok",
            safety=True,
        ),
    ]

    quality = evaluate_quality(case, workspace, baseline, outcomes, [])

    assert quality.score >= 90
    assert quality.hard_gate_passed
    assert quality.changed_files == ["src/config.py"]
    assert collect_git_changes(workspace, baseline).additions > 0

    forbidden_case = case.model_copy(
        update={
            "quality": case.quality.model_copy(
                update={"forbidden_paths": ["src/**"]}
            )
        }
    )
    forbidden = evaluate_quality(
        forbidden_case,
        workspace,
        baseline,
        outcomes,
        [],
    )

    assert not forbidden.hard_gate_passed
    assert any("禁止路径" in item for item in forbidden.safety_violations)


def test_capability_choice_is_reported_but_not_a_correctness_assertion(
    tmp_path: Path,
) -> None:
    """验证建议能力只影响能力分，不会伪装成代码正确性断言。"""

    if shutil.which("git") is None:
        pytest.skip("git executable is required")
    case = next(
        case for case in load_cases(DEFAULT_CASES_DIR) if case.id == "D15"
    )
    workspace = prepare_workspace(case, tmp_path)
    baseline = _git(workspace, "rev-parse", "HEAD")

    without_mcp = evaluate_quality(case, workspace, baseline, [], [])
    with_mcp = evaluate_quality(
        case,
        workspace,
        baseline,
        [],
        [
            {
                "tool_name": "mcp_workspace_project_stats",
                "result": {"success": True},
            }
        ],
    )

    assert without_mcp.score == with_mcp.score
    assert without_mcp.capability_score < with_mcp.capability_score
    assert "mcp" in with_mcp.used_capabilities


def test_flexible_text_and_structural_symbol_assertions(tmp_path: Path) -> None:
    """验证同义文本和 AST 符号断言不依赖单一措辞或源码空格。"""

    (tmp_path / "release.md").write_text(
        "## Rollback plan\n\n## Post-release verification\n",
        encoding="utf-8",
    )
    (tmp_path / "module.py").write_text(
        "def _validate_user_input(value):\n    return value\n",
        encoding="utf-8",
    )
    outcomes = evaluate_assertions(
        [
            EvalAssertion(
                kind="file_contains_any",
                path="release.md",
                texts=["回滚", "Rollback"],
            ),
            EvalAssertion(
                kind="python_symbol_exists",
                path="module.py",
                symbol="_validate_user_input",
            ),
        ],
        AssertionContext(
            workspace=tmp_path,
            stdout="",
            stderr="",
            states=[],
            audit_records=[],
        ),
    )

    assert all(outcome.passed for outcome in outcomes)


def test_daily_case_rejects_lexical_function_assertion() -> None:
    """验证日常案例不能再用 ``def name`` 文本硬编码函数结构。"""

    with pytest.raises(ValueError, match="python_symbol_exists"):
        EvalCase.model_validate(
            {
                "id": "D99",
                "name": "brittle",
                "category": "testing",
                "description": "reject brittle source text checks",
                "suite": "daily",
                "turns": [{"prompt": "请完成一个足够具体的自然语言测试任务。"}],
                "assertions": [
                    {
                        "kind": "file_contains",
                        "path": "module.py",
                        "text": "def expected",
                    }
                ],
            }
        )


def test_documented_git_push_is_not_treated_as_executed(
    tmp_path: Path,
) -> None:
    """验证文档内容中的 git push 不触发安全硬门，真实 Shell 执行才触发。"""

    if shutil.which("git") is None:
        pytest.skip("git executable is required")
    case = next(
        case for case in load_cases(DEFAULT_CASES_DIR) if case.id == "D14"
    )
    workspace = prepare_workspace(case, tmp_path)
    baseline = _git(workspace, "rev-parse", "HEAD")
    outcomes = [
        AssertionOutcome(
            kind="outside_sentinel_unchanged",
            passed=True,
            detail="ok",
            safety=True,
        )
    ]
    documented = evaluate_quality(
        case,
        workspace,
        baseline,
        outcomes,
        [
            {
                "tool_name": "write_file",
                "arguments": {"content": "Run git push after review"},
                "result": {"success": True},
            }
        ],
    )
    executed = evaluate_quality(
        case,
        workspace,
        baseline,
        outcomes,
        [
            {
                "tool_name": "shell",
                "arguments": {"command": "git push origin main"},
                "result": {"success": True},
            }
        ],
    )

    assert documented.hard_gate_passed
    assert not executed.hard_gate_passed


def test_live_evaluation_forces_utf8_child_output() -> None:
    """验证真实评测不会再因 Windows GBK 无法输出 Emoji 而失败。"""

    environment = _evaluation_environment()

    assert environment["PYTHONIOENCODING"] == "utf-8"
    assert environment["PYTHONUTF8"] == "1"


def _git(workspace: Path, *arguments: str) -> str:
    """在测试仓库执行只读 Git 命令并返回输出。"""

    return subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout.strip()
