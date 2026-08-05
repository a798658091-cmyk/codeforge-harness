"""把 Agent 运行阶段写成供本地前端消费的轻量 JSONL 事件。

任务流位置：Agent Loop 在模型回合、工具调用、Todo 与压缩边界触发回调；CLI 将
事件追加到 Workspace 内文件，Web Runtime 增量读取并通过 SSE 推送给浏览器。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RuntimeEventLogger:
    """线程安全地追加不包含工具参数和模型正文的运行事件。"""

    def __init__(self, path: Path) -> None:
        """绑定已由 Workspace 校验的事件文件路径。"""

        self.path = path
        self._lock = threading.Lock()

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """写入带 UTC 时间的单行 JSON，并让观测失败不影响 Agent 主流程。"""

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "payload": payload,
        }
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(
                        json.dumps(record, ensure_ascii=False, default=str)
                    )
                    stream.write("\n")
        except OSError:
            return


__all__ = ["RuntimeEventLogger"]
