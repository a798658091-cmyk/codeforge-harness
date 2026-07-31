from __future__ import annotations

from pathlib import Path

import pytest

from harness.tools import build_default_registry
from harness.tools.registry import ToolRegistry


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "sample.py").write_text(
        "def greet(name):\n"
        "    return f'hello {name}'\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def registry(workspace: Path) -> ToolRegistry:
    return build_default_registry(workspace)
