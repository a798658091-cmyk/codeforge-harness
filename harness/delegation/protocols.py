"""定义只读 Subagent 对外返回的稳定协议模型。

任务流位置：ReadonlySubagentRunner 完成独立 Agent Loop 后生成该结果，委派工具
再把它序列化回主 Agent，避免直接暴露子循环内部状态对象。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SubagentResult:
    """描述只读子任务的答案、运行指标和唯一标识。"""

    subagent_id: str
    answer: str
    steps: int
    tool_calls: int
    tool_failures: int
