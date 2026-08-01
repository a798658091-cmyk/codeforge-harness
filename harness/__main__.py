"""支持通过 ``python -m harness`` 启动 CodeForge 命令行程序。

任务流位置：用户启动任务时最先进入的 Python 模块，随后把控制权交给
``harness.cli.main`` 完成参数解析、组件装配和 Agent Loop 执行。
"""

from harness.cli import main

raise SystemExit(main())
