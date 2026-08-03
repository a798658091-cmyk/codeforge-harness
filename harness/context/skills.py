"""发现、校验并按需加载 workspace 内的 SKILL.md 指令包。

任务流位置：CLI 启动时扫描技能目录，把目录清单加入系统提示词；模型可调用
list_skills/read_skill 渐进读取内容，用户也可用 --skill 或 $技能名预加载。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pydantic import Field

from harness.safety.workspace import Workspace, WorkspaceViolation
from harness.tools.base import BaseTool, ToolArguments, ToolContext, ToolError


SKILL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
SKILL_MENTION_PATTERN = re.compile(r"(?<!\w)\$([A-Za-z0-9][A-Za-z0-9_-]{0,79})")


@dataclass(frozen=True)
class Skill:
    """保存一个技能的名称、简介、受沙箱约束的来源和完整说明。"""

    name: str
    description: str
    path: Path
    content: str


class SkillRegistry:
    """管理已发现技能，并提供目录、选择和提示词渲染能力。"""

    def __init__(self, skills: list[Skill] | None = None) -> None:
        """按名称建立技能索引并拒绝重复项。"""

        self._skills: dict[str, Skill] = {}
        for skill in skills or []:
            if skill.name in self._skills:
                raise ValueError(f"duplicate skill name: {skill.name}")
            self._skills[skill.name] = skill

    @classmethod
    def discover(cls, workspace: str | Path) -> "SkillRegistry":
        """扫描 .codeforge/skills 和 skills 下的一级 SKILL.md。"""

        boundary = Workspace(Path(workspace))
        discovered: dict[str, Skill] = {}
        roots = [
            boundary.root / ".codeforge" / "skills",
            boundary.root / "skills",
        ]
        for root in roots:
            if not root.exists():
                continue
            for candidate in sorted(root.glob("*/SKILL.md")):
                try:
                    safe_path = boundary.resolve(candidate, must_exist=True)
                except (WorkspaceViolation, FileNotFoundError):
                    continue
                skill = _read_skill(safe_path)
                # 更靠近用户的 .codeforge/skills 具有更高优先级。
                discovered.setdefault(skill.name, skill)
        return cls(list(discovered.values()))

    def names(self) -> list[str]:
        """按名称返回所有可用技能。"""

        return sorted(self._skills)

    def get(self, name: str) -> Skill:
        """按名称取得技能，不存在时给出包含可选项的错误。"""

        skill = self._skills.get(name)
        if skill is None:
            available = ", ".join(self.names()) or "none"
            raise KeyError(f"unknown skill {name!r}; available: {available}")
        return skill

    def catalog(self) -> str:
        """生成适合系统提示词或工具输出的精简技能目录。"""

        if not self._skills:
            return "No workspace skills are available."
        return "\n".join(
            f"- {skill.name}: {skill.description}"
            for skill in (self._skills[name] for name in self.names())
        )

    def explicit_mentions(self, prompt: str) -> list[str]:
        """提取用户提示词中的 $skill-name 显式引用。"""

        return list(dict.fromkeys(SKILL_MENTION_PATTERN.findall(prompt)))

    def render_selected(self, names: list[str]) -> str:
        """把显式选择的技能完整内容拼接为系统提示词片段。"""

        sections = []
        for name in dict.fromkeys(names):
            skill = self.get(name)
            sections.append(
                f"## Active skill: {skill.name}\n{skill.content.strip()}"
            )
        return "\n\n".join(sections)


class ListSkillsArguments(ToolArguments):
    """定义 list_skills 的空参数对象。"""

    pass


class ReadSkillArguments(ToolArguments):
    """定义 read_skill 所需的技能名称。"""

    name: str = Field(min_length=1, max_length=80)


class ListSkillsTool(BaseTool):
    """让模型查看当前工作区可用的技能目录。"""

    name: ClassVar[str] = "list_skills"
    description: ClassVar[str] = (
        "List workspace skills and their short descriptions."
    )
    arguments_model: ClassVar[type[ToolArguments]] = ListSkillsArguments

    def __init__(self, registry: SkillRegistry) -> None:
        """绑定只读技能注册表。"""

        self.registry = registry

    def execute(
        self,
        arguments: ToolArguments,
        context: ToolContext,
    ) -> str:
        """返回精简技能目录。"""

        return self.registry.catalog()


class ReadSkillTool(BaseTool):
    """让模型按名称读取一个已发现技能的完整指令。"""

    name: ClassVar[str] = "read_skill"
    description: ClassVar[str] = (
        "Read the complete SKILL.md instructions for one workspace skill."
    )
    arguments_model: ClassVar[type[ToolArguments]] = ReadSkillArguments

    def __init__(self, registry: SkillRegistry) -> None:
        """绑定只读技能注册表。"""

        self.registry = registry

    def execute(
        self,
        arguments: ToolArguments,
        context: ToolContext,
    ) -> str:
        """返回指定技能内容，并把未知名称转换为工具错误。"""

        if not isinstance(arguments, ReadSkillArguments):
            raise TypeError("read_skill received unexpected arguments")
        try:
            return self.registry.get(arguments.name).content
        except KeyError as exc:
            raise ToolError(str(exc)) from exc


def _read_skill(path: Path) -> Skill:
    """读取单个 SKILL.md，并解析可选的简单 YAML 风格头部。"""

    content = path.read_text(encoding="utf-8")
    if len(content) > 100_000:
        raise ValueError(f"skill file is too large: {path}")
    metadata, body = _split_frontmatter(content)
    name = metadata.get("name", path.parent.name).strip()
    if not SKILL_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"invalid skill name {name!r} in {path}")
    description = metadata.get("description", "").strip()
    if not description:
        description = _first_description_line(body) or f"Instructions for {name}"
    description = description[:500]
    return Skill(name=name, description=description, path=path, content=body)


def _split_frontmatter(content: str) -> tuple[dict[str, str], str]:
    """解析仅包含 name/description 单行键值的可选 frontmatter。"""

    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, content
    try:
        end = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration:
        return {}, content
    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip() in {"name", "description"}:
            metadata[key.strip()] = value.strip().strip("\"'")
    return metadata, "\n".join(lines[end + 1 :]).strip()


def _first_description_line(body: str) -> str:
    """从正文提取第一个非标题、非空行作为缺省简介。"""

    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:300]
    return ""


__all__ = [
    "ListSkillsTool",
    "ReadSkillTool",
    "Skill",
    "SkillRegistry",
]
