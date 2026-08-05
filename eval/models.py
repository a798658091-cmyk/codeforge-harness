"""定义 CodeForge 评测案例、断言结果和单次运行结果模型。

任务流位置：benchmark 读取 ``eval/cases/*.json`` 后先经本模块校验，再准备临时
Git Workspace、运行真实 CLI、执行确定性断言并交给 metrics 汇总。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


CASE_ID_PATTERN = re.compile(r"^[ED][0-9]{2}$")


class EvalModel(BaseModel):
    """禁止评测配置出现未声明字段，避免拼写错误被静默忽略。"""

    model_config = ConfigDict(extra="forbid")


class FixtureFile(EvalModel):
    """描述评测临时 Workspace 中需要预置的 UTF-8 文件。"""

    path: str = Field(min_length=1, max_length=500)
    content: str = Field(max_length=200_000)


class EvalTurn(EvalModel):
    """描述一轮真实用户输入以及它是否恢复上一会话。"""

    prompt: str = Field(min_length=1, max_length=20_000)
    resume_previous: bool = False


AssertionKind = Literal[
    "answer_contains",
    "answer_contains_any",
    "answer_not_contains",
    "audit_tool_denied",
    "audit_tool_not_used",
    "audit_tool_used",
    "file_contains",
    "file_contains_any",
    "file_exists",
    "file_not_contains",
    "file_not_exists",
    "git_clean",
    "git_commit_count_at_least",
    "python_symbol_exists",
    "tests_pass",
    "todo_all_completed",
]


class CapabilityPolicy(EvalModel):
    """描述自然任务中建议、谨慎和禁止使用的高层能力。"""

    preferred: list[str] = Field(default_factory=list, max_length=20)
    discouraged: list[str] = Field(default_factory=list, max_length=20)
    forbidden: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_unique_groups(self) -> "CapabilityPolicy":
        """拒绝同一个能力同时出现在互斥策略组中。"""

        groups = [set(self.preferred), set(self.discouraged), set(self.forbidden)]
        if groups[0] & groups[1] or groups[0] & groups[2] or groups[1] & groups[2]:
            raise ValueError("capability policy groups must not overlap")
        return self


class QualityPolicy(EvalModel):
    """定义适用于当前案例的代码质量、范围和测试要求。"""

    enabled: bool = True
    min_score: float = Field(default=70, ge=0, le=100)
    allowed_paths: list[str] = Field(default_factory=lambda: ["**"], max_length=50)
    forbidden_paths: list[str] = Field(
        default_factory=lambda: [".git/**", ".codeforge/**", ".worktrees/**"],
        max_length=50,
    )
    max_changed_files: int = Field(default=12, ge=0, le=500)
    max_changed_lines: int = Field(default=800, ge=0, le=100_000)
    max_cyclomatic_complexity: int = Field(default=12, ge=1, le=100)
    max_function_lines: int = Field(default=80, ge=5, le=2000)
    require_tests_changed: bool = False
    require_python_syntax: bool = True


class EvalAssertion(EvalModel):
    """定义一个无需 LLM Judge 即可重复验证的评测断言。"""

    kind: AssertionKind
    path: str | None = Field(default=None, max_length=500)
    text: str | None = Field(default=None, max_length=20_000)
    texts: list[str] = Field(default_factory=list, max_length=30)
    symbol: str | None = Field(default=None, max_length=200)
    tool: str | None = Field(default=None, max_length=120)
    targets: list[str] = Field(default_factory=list, max_length=20)
    min_count: int = Field(default=1, ge=1, le=1000)
    safety: bool = False

    @model_validator(mode="after")
    def validate_required_fields(self) -> "EvalAssertion":
        """根据断言类型检查 path、text、tool 或 targets 等必需字段。"""

        path_kinds = {
            "file_contains",
            "file_contains_any",
            "file_exists",
            "file_not_contains",
            "file_not_exists",
            "python_symbol_exists",
        }
        text_kinds = {
            "answer_contains",
            "answer_not_contains",
            "file_contains",
            "file_not_contains",
        }
        tool_kinds = {
            "audit_tool_denied",
            "audit_tool_not_used",
            "audit_tool_used",
        }
        if self.kind in path_kinds and not self.path:
            raise ValueError(f"{self.kind} requires path")
        if self.kind in text_kinds and self.text is None:
            raise ValueError(f"{self.kind} requires text")
        if self.kind in {"answer_contains_any", "file_contains_any"}:
            if not self.texts or any(not text for text in self.texts):
                raise ValueError(f"{self.kind} requires non-empty texts")
        if self.kind == "python_symbol_exists" and not self.symbol:
            raise ValueError("python_symbol_exists requires symbol")
        if self.kind in tool_kinds and not self.tool:
            raise ValueError(f"{self.kind} requires tool")
        if self.kind == "tests_pass" and not self.targets:
            raise ValueError("tests_pass requires targets")
        return self


class EvalCase(EvalModel):
    """描述一个可独立复制、运行和自动判定的自然语言任务。"""

    id: str
    name: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1000)
    suite: Literal["capability", "daily", "adversarial"] = "capability"
    files: list[FixtureFile] = Field(default_factory=list, max_length=100)
    hidden_files: list[FixtureFile] = Field(default_factory=list, max_length=100)
    turns: list[EvalTurn] = Field(min_length=1, max_length=5)
    assertions: list[EvalAssertion] = Field(min_length=1, max_length=50)
    permissions: list[str] = Field(default_factory=list, max_length=30)
    mcp_config: str | None = Field(default=None, max_length=500)
    capability_policy: CapabilityPolicy = Field(default_factory=CapabilityPolicy)
    quality: QualityPolicy = Field(default_factory=QualityPolicy)
    max_steps: int = Field(default=24, ge=1, le=60)
    timeout_seconds: int = Field(default=240, ge=10, le=1800)
    tags: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def validate_case(self) -> "EvalCase":
        """检查案例 ID、文件路径唯一性和首轮恢复配置。"""

        if not CASE_ID_PATTERN.fullmatch(self.id):
            raise ValueError("case id must use E01..E99 or D01..D99 format")
        if self.id.startswith("D") and self.suite != "daily":
            raise ValueError("D-prefixed cases must use suite=daily")
        paths = [file.path for file in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("fixture file paths must be unique")
        hidden_paths = [file.path for file in self.hidden_files]
        if len(hidden_paths) != len(set(hidden_paths)):
            raise ValueError("hidden fixture paths must be unique")
        if self.turns[0].resume_previous:
            raise ValueError("the first turn cannot resume a previous session")
        if self.suite == "daily":
            brittle = [
                assertion.text
                for assertion in self.assertions
                if assertion.kind == "file_contains"
                and assertion.text is not None
                and assertion.text.lstrip().startswith(("def ", "class "))
            ]
            if brittle:
                raise ValueError(
                    "daily cases must use python_symbol_exists instead of "
                    "lexical def/class assertions"
                )
        return self


class QualityDimension(EvalModel):
    """记录代码质量某一维度的得分和解释。"""

    name: str
    score: float = Field(ge=0)
    max_score: float = Field(gt=0)
    detail: str


class QualityResult(EvalModel):
    """记录确定性代码质量、范围、安全和能力选择评估结果。"""

    score: float = Field(ge=0, le=100)
    hard_gate_passed: bool = True
    changed_files: list[str] = Field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    issues: list[str] = Field(default_factory=list)
    safety_violations: list[str] = Field(default_factory=list)
    dimensions: list[QualityDimension] = Field(default_factory=list)
    capability_score: float = Field(default=100, ge=0, le=100)
    used_capabilities: list[str] = Field(default_factory=list)
    capability_notes: list[str] = Field(default_factory=list)


class AssertionOutcome(EvalModel):
    """记录一项确定性断言的通过状态和诊断详情。"""

    kind: str
    passed: bool
    detail: str
    safety: bool = False


class CaseResult(EvalModel):
    """记录一个评测案例的最终指标、断言和可复现元数据。"""

    case_id: str
    name: str
    category: str
    passed: bool
    exit_codes: list[int]
    duration_seconds: float
    steps: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    compactions: int = 0
    assertions: list[AssertionOutcome] = Field(default_factory=list)
    quality: QualityResult | None = None
    artifact_dir: str
    error: str | None = None
    tags: list[str] = Field(default_factory=list)


__all__ = [
    "AssertionOutcome",
    "CaseResult",
    "CapabilityPolicy",
    "EvalAssertion",
    "EvalCase",
    "EvalTurn",
    "FixtureFile",
    "QualityDimension",
    "QualityPolicy",
    "QualityResult",
]
