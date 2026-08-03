"""验证 workspace Skills 的发现、显式选择和只读工具访问。

任务流位置：模拟 CLI 启动时扫描 SKILL.md，并验证模型可以先看目录、再按名称
读取完整指令；所有路径均位于 pytest 临时 workspace。
"""

from pathlib import Path

from harness.context.skills import SkillRegistry
from harness.safety.permissions import PermissionPolicy
from harness.tools import build_default_registry


def _create_skill(workspace: Path, name: str = "review") -> Path:
    """在临时工作区创建带 frontmatter 的示例技能。"""

    skill_dir = workspace / ".codeforge" / "skills" / name
    skill_dir.mkdir(parents=True)
    path = skill_dir / "SKILL.md"
    path.write_text(
        "---\n"
        f"name: {name}\n"
        "description: Review changes safely\n"
        "---\n"
        "# Review\n\nRead tests before suggesting changes.\n",
        encoding="utf-8",
    )
    return path


def test_skill_registry_discovers_metadata_and_mentions(
    workspace: Path,
) -> None:
    """验证技能元数据、目录和 $name 显式引用。"""

    path = _create_skill(workspace)
    registry = SkillRegistry.discover(workspace)

    skill = registry.get("review")
    assert skill.path == path.resolve()
    assert skill.description == "Review changes safely"
    assert "review" in registry.catalog()
    assert registry.explicit_mentions("请使用 $review 然后再次 $review") == [
        "review"
    ]
    assert "Read tests" in registry.render_selected(["review"])


def test_skill_tools_list_and_read_discovered_skill(workspace: Path) -> None:
    """验证 list_skills 和 read_skill 通过 Tool Registry 工作。"""

    _create_skill(workspace)
    skills = SkillRegistry.discover(workspace)
    registry = build_default_registry(
        workspace,
        skill_registry=skills,
        permission_policy=PermissionPolicy(default="allow"),
    )

    listed = registry.dispatch("list_skills", {})
    read = registry.dispatch("read_skill", {"name": "review"})
    missing = registry.dispatch("read_skill", {"name": "missing"})

    assert listed.success is True
    assert "Review changes safely" in listed.content
    assert read.success is True
    assert "Read tests" in read.content
    assert missing.success is False


def test_workspace_skill_directory_takes_precedence(workspace: Path) -> None:
    """验证 .codeforge/skills 可覆盖同名项目技能。"""

    project_dir = workspace / "skills" / "review"
    project_dir.mkdir(parents=True)
    (project_dir / "SKILL.md").write_text(
        "---\nname: review\ndescription: project copy\n---\nproject",
        encoding="utf-8",
    )
    _create_skill(workspace)

    registry = SkillRegistry.discover(workspace)

    assert registry.get("review").description == "Review changes safely"
