"""保存 CodeForge 用于真实 stdio MCP 演示的本地 Server。

任务流位置：这些 Server 作为独立子进程由 Harness MCP 客户端启动，不会被主
Python 进程直接导入；其 stdout 只承载 JSON-RPC，普通日志写入 stderr。
"""
