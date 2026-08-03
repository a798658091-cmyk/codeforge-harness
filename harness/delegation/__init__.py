"""导出 CodeForge 的受限只读 Subagent 能力。

任务流位置：CLI 创建真实 Provider 后装配 ReadonlySubagentRunner，再由默认工具
注册表暴露 delegate_readonly；完整并行团队与可写委派暂不在此版本实现。
"""

from harness.delegation.protocols import SubagentResult
from harness.delegation.subagent import (
    DelegateReadonlyTool,
    ReadonlySubagentRunner,
)

__all__ = [
    "DelegateReadonlyTool",
    "ReadonlySubagentRunner",
    "SubagentResult",
]
