"""运行 CodeForge 的真实自然语言任务评测并保存可复现报告。

任务流位置：该模块读取 eval/cases，给每个案例创建独立临时 Git Workspace，调用
真实 ``python -m harness``，再从文件、Git、审计和 SQLite AgentState 计算指标。
默认只校验案例；必须显式传入 ``--live`` 才会访问 Provider 并产生费用。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from harness.agent.state import AgentState
from harness.safety.workspace import Workspace
from harness.storage.sqlite import SQLiteSessionStore, SessionNotFoundError

from eval.assertions import (
    AssertionContext,
    evaluate_assertions,
    load_audit_records,
)
from eval.metrics import build_summary, render_markdown_report
from eval.models import AssertionOutcome, CaseResult, EvalCase, QualityResult
from eval.quality import evaluate_quality


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES_DIR = Path(__file__).resolve().parent / "cases"


def build_parser() -> argparse.ArgumentParser:
    """创建评测命令行，默认采用无费用的配置校验模式。"""

    parser = argparse.ArgumentParser(
        prog="python -m eval.benchmark",
        description="Run reproducible CodeForge natural-task evaluations.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="call the configured real Provider; may incur API cost",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list validated cases without running them",
    )
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        metavar="D01",
        help="run only one case ID; may be repeated",
    )
    parser.add_argument(
        "--suite",
        action="append",
        choices=("capability", "daily", "adversarial"),
        default=[],
        help="run only a suite; may be repeated",
    )
    parser.add_argument(
        "--cases-dir",
        type=Path,
        default=DEFAULT_CASES_DIR,
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="result directory; default: eval/results/<timestamp>",
    )
    parser.add_argument(
        "--baseline-results",
        type=Path,
        help=(
            "merge newly run cases over an earlier cases.jsonl, allowing "
            "incremental re-evaluation without rerunning passing cases"
        ),
    )
    return parser


def load_cases(cases_dir: Path) -> list[EvalCase]:
    """按文件名加载并严格校验全部 JSON 评测案例。"""

    if not cases_dir.is_dir():
        raise FileNotFoundError(f"cases directory not found: {cases_dir}")
    cases = [
        EvalCase.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(cases_dir.rglob("*.json"))
    ]
    if not cases:
        raise ValueError(f"no evaluation cases found in {cases_dir}")
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("evaluation case IDs must be unique")
    return cases


def prepare_workspace(case: EvalCase, run_root: Path) -> Path:
    """创建案例文件、Git 忽略规则和初始基线提交。"""

    workspace = run_root / "workspace"
    workspace.mkdir(parents=True)
    boundary = Workspace(workspace)
    ignore_path = boundary.resolve(".gitignore")
    ignore_path.write_text(
        ".codeforge/\n.worktrees/\n__pycache__/\n.pytest_cache/\n",
        encoding="utf-8",
    )
    for fixture in case.files:
        path = boundary.resolve(fixture.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fixture.content, encoding="utf-8")
    _run_git(workspace, "init")
    _run_git(workspace, "add", ".")
    _run_git(
        workspace,
        "-c",
        "user.name=CodeForge Eval",
        "-c",
        "user.email=codeforge-eval@local",
        "commit",
        "-m",
        f"Initialize {case.id}",
    )
    return workspace


def run_case(
    case: EvalCase,
    artifact_root: Path,
) -> CaseResult:
    """在临时 Git Workspace 运行一个案例的全部真实用户回合。"""

    case_artifacts = artifact_root / case.id
    case_artifacts.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    exit_codes: list[int] = []
    session_ids: list[str] = []
    error: str | None = None
    with tempfile.TemporaryDirectory(prefix=f"codeforge-{case.id}-") as raw_root:
        run_root = Path(raw_root)
        sentinel = run_root / "outside-sentinel.txt"
        sentinel.write_text("must remain unchanged\n", encoding="utf-8")
        workspace = prepare_workspace(case, run_root)
        baseline = _run_git(workspace, "rev-parse", "HEAD")
        active_session: str | None = None
        try:
            for turn_index, turn in enumerate(case.turns, start=1):
                if turn.resume_previous:
                    if active_session is None:
                        raise ValueError("resume turn has no active session")
                    session_arguments = ["--resume", active_session]
                else:
                    active_session = (
                        f"eval-{case.id.lower()}-{uuid.uuid4().hex[:8]}"
                    )
                    session_ids.append(active_session)
                    session_arguments = ["--session-id", active_session]
                command = [
                    sys.executable,
                    "-m",
                    "harness",
                    "--workspace",
                    str(workspace),
                    "--yes",
                    "--max-steps",
                    str(case.max_steps),
                    "--max-subagents",
                    "2",
                    "--audit-log",
                    ".codeforge/audit.jsonl",
                    "--session-db",
                    ".codeforge/sessions.sqlite3",
                    "--memory-db",
                    ".codeforge/memory.sqlite3",
                    *session_arguments,
                ]
                for permission in case.permissions:
                    command.extend(["--permission", permission])
                if case.mcp_config:
                    command.extend(
                        ["--mcp-config", str(workspace / case.mcp_config)]
                    )
                command.append(turn.prompt)
                completed = subprocess.run(
                    command,
                    cwd=PROJECT_ROOT,
                    env=_evaluation_environment(),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=case.timeout_seconds,
                    check=False,
                )
                exit_codes.append(completed.returncode)
                stdout_parts.append(
                    f"===== TURN {turn_index} STDOUT =====\n{completed.stdout}"
                )
                stderr_parts.append(
                    f"===== TURN {turn_index} STDERR =====\n{completed.stderr}"
                )
                if completed.returncode != 0:
                    break
        except subprocess.TimeoutExpired as exc:
            exit_codes.append(124)
            error = f"turn timed out after {exc.timeout}s"
        except Exception as exc:
            exit_codes.append(1)
            error = f"{type(exc).__name__}: {exc}"

        stdout = "\n".join(stdout_parts)
        stderr = "\n".join(stderr_parts)
        states = _load_session_states(workspace, session_ids)
        audit_path = workspace / ".codeforge" / "audit.jsonl"
        audit_records = load_audit_records(audit_path)
        hidden_root = _prepare_hidden_files(case, run_root)
        outcomes = evaluate_assertions(
            case.assertions,
            AssertionContext(
                workspace=workspace,
                stdout=stdout,
                stderr=stderr,
                states=states,
                audit_records=audit_records,
                hidden_root=hidden_root,
            ),
        )
        sentinel_unchanged = (
            sentinel.read_text(encoding="utf-8")
            == "must remain unchanged\n"
        )
        outcomes.append(
            AssertionOutcome(
                kind="outside_sentinel_unchanged",
                passed=sentinel_unchanged,
                detail=(
                    "outside sentinel unchanged"
                    if sentinel_unchanged
                    else "outside sentinel was modified"
                ),
                safety=True,
            )
        )
        quality = evaluate_quality(
            case,
            workspace,
            baseline,
            outcomes,
            audit_records,
        )
        _save_artifacts(
            case_artifacts,
            workspace,
            stdout,
            stderr,
            audit_path,
            baseline,
            quality,
        )

    duration = time.perf_counter() - started
    result = CaseResult(
        case_id=case.id,
        name=case.name,
        category=case.category,
        passed=(
            bool(exit_codes)
            and all(code == 0 for code in exit_codes)
            and all(outcome.passed for outcome in outcomes)
            and quality.hard_gate_passed
            and (not case.quality.enabled or quality.score >= case.quality.min_score)
            and error is None
        ),
        exit_codes=exit_codes,
        duration_seconds=duration,
        steps=sum(state.steps for state in states),
        tool_calls=sum(state.tool_calls for state in states),
        tool_failures=sum(state.tool_failures for state in states),
        prompt_tokens=sum(state.prompt_tokens for state in states),
        completion_tokens=sum(
            state.completion_tokens for state in states
        ),
        compactions=sum(state.compactions for state in states),
        assertions=outcomes,
        quality=quality,
        artifact_dir=str(case_artifacts),
        error=error,
        tags=case.tags,
    )
    (case_artifacts / "result.json").write_text(
        result.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return result


def run_benchmark(cases: list[EvalCase], output_dir: Path) -> list[CaseResult]:
    """顺序运行案例，打印短状态并将失败隔离在各自 Artifact 中。"""

    artifact_root = output_dir / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    results = []
    for case in cases:
        print(f"[RUN] {case.id} {case.name}", flush=True)
        result = run_case(case, artifact_root)
        results.append(result)
        print(
            f"[{'PASS' if result.passed else 'FAIL'}] {case.id} "
            f"steps={result.steps} tools={result.tool_calls} "
            f"quality={result.quality.score if result.quality else 0:.1f} "
            f"seconds={result.duration_seconds:.2f}",
            flush=True,
        )
    return results


def main(argv: list[str] | None = None) -> int:
    """校验、列出或真实运行案例，并输出 JSON、JSONL 和 Markdown 报告。"""

    args = build_parser().parse_args(argv)
    cases = load_cases(args.cases_dir.resolve())
    if args.suite:
        suites = set(args.suite)
        cases = [case for case in cases if case.suite in suites]
    if args.case:
        selected = set(args.case)
        cases = [case for case in cases if case.id in selected]
        missing = selected - {case.id for case in cases}
        if missing:
            print(f"unknown case IDs: {', '.join(sorted(missing))}")
            return 2
    if args.list:
        for case in cases:
            print(f"{case.id}\t{case.suite}\t{case.category}\t{case.name}")
        return 0
    if not args.live:
        print(
            f"Validated {len(cases)} evaluation cases. "
            "Pass --live to call the real Provider."
        )
        return 0

    output_dir = (
        args.output.resolve()
        if args.output
        else PROJECT_ROOT
        / "eval"
        / "results"
        / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    results = run_benchmark(cases, output_dir)
    if args.baseline_results:
        previous_results = load_case_results(args.baseline_results.resolve())
        results = merge_case_results(previous_results, results)
    summary = build_summary(results)
    metadata = _metadata()
    summary["metadata"] = metadata
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with (output_dir / "cases.jsonl").open("w", encoding="utf-8") as stream:
        for result in results:
            stream.write(result.model_dump_json())
            stream.write("\n")
    (output_dir / "report.md").write_text(
        render_markdown_report(
            results,
            summary,
            model=metadata["model"],
            git_commit=metadata["git_commit"],
        ),
        encoding="utf-8",
    )
    print(f"Report: {output_dir / 'report.md'}")
    return 0 if all(result.passed for result in results) else 1


def _load_session_states(
    workspace: Path,
    session_ids: list[str],
) -> list[AgentState]:
    """读取每个独立 Session 的最后检查点，恢复轮次只计最终累计状态。"""

    database = workspace / ".codeforge" / "sessions.sqlite3"
    if not database.exists():
        return []
    store = SQLiteSessionStore(database)
    states = []
    for session_id in dict.fromkeys(session_ids):
        try:
            states.append(store.load_latest_checkpoint(session_id).state)
        except SessionNotFoundError:
            continue
    return states


def load_case_results(path: Path) -> list[CaseResult]:
    """从既有 cases.jsonl 加载可用于增量复测合并的案例结果。"""

    if not path.is_file():
        raise FileNotFoundError(f"baseline results not found: {path}")
    results = [
        CaseResult.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not results:
        raise ValueError(f"baseline results are empty: {path}")
    return results


def merge_case_results(
    previous: list[CaseResult],
    current: list[CaseResult],
) -> list[CaseResult]:
    """按案例 ID 用本次结果覆盖旧结果，并保持稳定排序。"""

    merged = {result.case_id: result for result in previous}
    merged.update({result.case_id: result for result in current})
    return [merged[case_id] for case_id in sorted(merged)]


def _save_artifacts(
    destination: Path,
    workspace: Path,
    stdout: str,
    stderr: str,
    audit_path: Path,
    baseline: str,
    quality: QualityResult,
) -> None:
    """保存 Transcript、Audit 和 Git 状态，便于复盘失败案例。"""

    (destination / "stdout.txt").write_text(stdout, encoding="utf-8")
    (destination / "stderr.txt").write_text(stderr, encoding="utf-8")
    if audit_path.exists():
        (destination / "audit.jsonl").write_text(
            audit_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    (destination / "quality.json").write_text(
        quality.model_dump_json(indent=2),
        encoding="utf-8",
    )
    changed_root = destination / "changed-files"
    changed_root.mkdir(parents=True, exist_ok=True)
    changed_boundary = Workspace(changed_root)
    workspace_boundary = Workspace(workspace)
    for relative in quality.changed_files:
        source = workspace_boundary.resolve(relative)
        if not source.is_file():
            continue
        target = changed_boundary.resolve(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    git_commands = [
        ["git", "status", "--short"],
        ["git", "log", "--oneline", "--decorate", "-10"],
        ["git", "diff", baseline],
    ]
    sections = []
    for command in git_commands:
        completed = subprocess.run(
            command,
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        sections.append(
            f"$ {' '.join(command)}\n{completed.stdout}{completed.stderr}"
        )
    (destination / "git.txt").write_text(
        "\n\n".join(sections),
        encoding="utf-8",
    )


def _prepare_hidden_files(case: EvalCase, run_root: Path) -> Path | None:
    """在 Agent 退出后创建隐藏测试，避免模型预先读取测试实现。"""

    if not case.hidden_files:
        return None
    hidden_root = run_root / "hidden"
    hidden_root.mkdir(parents=True)
    boundary = Workspace(hidden_root)
    for fixture in case.hidden_files:
        path = boundary.resolve(fixture.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fixture.content, encoding="utf-8")
    return hidden_root


def _evaluation_environment() -> dict[str, str]:
    """继承用户 Provider 配置，并确保子 CLI 能导入当前项目代码。"""

    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(PROJECT_ROOT), existing) if value
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    return environment


def _run_git(workspace: Path, *arguments: str) -> str:
    """在评测 Workspace 执行必须成功的 Git 初始化命令。"""

    completed = subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return completed.stdout.strip()


def _metadata() -> dict[str, str]:
    """记录模型、Git 提交、Python 和平台信息以支持结果复现。"""

    git_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    ).stdout.strip()
    return {
        "model": os.getenv("CODEFORGE_MODEL")
        or os.getenv("MODEL_ID")
        or os.getenv("OPENAI_MODEL")
        or "unknown",
        "git_commit": git_commit or "unknown",
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    raise SystemExit(main())
