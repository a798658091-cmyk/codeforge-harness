"""提供上下文压缩、workspace Skills 和 SQLite 长期记忆能力。

任务流位置：CLI 用 Skills 与 Memory 构造系统上下文，Agent Loop 使用压缩器控制
发送给 Provider 的历史规模；具体类型从子模块导入以避免工具层循环依赖。
"""
