from pathlib import Path

import pytest

from harness.safety.workspace import Workspace, WorkspaceViolation


def test_workspace_accepts_relative_paths(workspace: Path) -> None:
    boundary = Workspace(workspace)

    resolved = boundary.resolve("src/sample.py", must_exist=True)

    assert resolved == workspace / "src" / "sample.py"


def test_workspace_rejects_parent_traversal(workspace: Path) -> None:
    boundary = Workspace(workspace)

    with pytest.raises(WorkspaceViolation):
        boundary.resolve("../outside.txt")


def test_workspace_rejects_absolute_external_path(
    workspace: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    boundary = Workspace(workspace)
    external = tmp_path_factory.mktemp("external") / "secret.txt"
    external.write_text("secret", encoding="utf-8")

    with pytest.raises(WorkspaceViolation):
        boundary.resolve(external, must_exist=True)
