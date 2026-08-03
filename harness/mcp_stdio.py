"""实现真实 stdio MCP 生命周期、JSON-RPC 通信与远端工具适配。

任务流位置：CLI 显式加载 MCP 配置后，本模块启动 Server 子进程，依次执行
initialize、notifications/initialized 和 tools/list；模型调用动态工具时，调用会
先经过主 Tool Registry，再由 MCPRemoteTool 转发为 tools/call。
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from harness.safety.workspace import Workspace
from harness.tools.base import (
    BaseTool,
    ToolArguments,
    ToolContext,
    ToolError,
    ToolOutput,
)

MCP_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = frozenset(
    {"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}
)
_SAFE_ENVIRONMENT_NAMES = frozenset(
    {
        "ALLUSERSPROFILE",
        "APPDATA",
        "COMSPEC",
        "HOMEDRIVE",
        "HOMEPATH",
        "HOME",
        "LANG",
        "LOCALAPPDATA",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERDOMAIN",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
    }
)


class MCPError(RuntimeError):
    """表示 MCP 配置、协议、进程或远端调用失败。"""


class MCPConfigurationError(MCPError):
    """表示 stdio MCP 配置文件无效或越过 workspace。"""


class MCPProtocolError(MCPError):
    """表示 Server 返回非法 JSON-RPC 或不兼容协议。"""


class MCPTimeoutError(MCPError):
    """表示 MCP 请求超过配置的最长等待时间。"""


class StdioMCPServerConfig(BaseModel):
    """描述一个需要由 Harness 启动的 stdio MCP Server。"""

    model_config = ConfigDict(extra="forbid")

    command: list[str] = Field(min_length=1)
    cwd: str = "."
    timeout_seconds: float = Field(default=15.0, gt=0, le=300)
    env: list[str] = Field(default_factory=list)
    enabled: bool = True


class _MCPConfigFile(BaseModel):
    """校验 MCP JSON 配置文件的顶层 servers 映射。"""

    model_config = ConfigDict(extra="forbid")

    servers: dict[str, StdioMCPServerConfig]


@dataclass(frozen=True)
class MCPToolDefinition:
    """保存 Server 返回且已完成基础校验的工具定义。"""

    name: str
    description: str
    input_schema: dict[str, Any]


def load_mcp_config(
    path: str | Path,
    workspace: str | Path,
) -> dict[str, StdioMCPServerConfig]:
    """从 workspace 内读取并校验显式指定的 MCP JSON 配置。"""

    boundary = Workspace(Path(workspace))
    try:
        config_path = boundary.resolve(path, must_exist=True)
    except (OSError, ValueError) as exc:
        raise MCPConfigurationError(f"invalid MCP config path: {exc}") from exc
    if not config_path.is_file():
        raise MCPConfigurationError(f"MCP config is not a file: {path}")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        parsed = _MCPConfigFile.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise MCPConfigurationError(f"invalid MCP config: {exc}") from exc
    for name in parsed.servers:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name):
            raise MCPConfigurationError(
                f"invalid MCP server name {name!r}; use letters, digits, _ or -"
            )
    return parsed.servers


class StdioMCPClient:
    """管理一个 stdio MCP Server 的进程、请求路由和协议生命周期。"""

    def __init__(
        self,
        name: str,
        config: StdioMCPServerConfig,
        workspace: str | Path,
    ) -> None:
        """绑定 Server 名称、启动配置和工作区边界。"""

        self.name = name
        self.config = config
        self.workspace = Workspace(Path(workspace))
        self.process: subprocess.Popen[str] | None = None
        self.protocol_version: str | None = None
        self.server_info: dict[str, Any] = {}
        self.server_capabilities: dict[str, Any] = {}
        self.notifications: list[dict[str, Any]] = []
        self._next_id = 1
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._stderr_stream: Any | None = None
        self._reader_error: str | None = None
        self._closing = False

    def start(self) -> None:
        """启动子进程并完成 MCP 初始化与能力协商。"""

        if self.process is not None:
            return
        self._reader_error = None
        self._closing = False
        command = [self._expand_token(item) for item in self.config.command]
        cwd = self.workspace.resolve(self.config.cwd, must_exist=True)
        if not cwd.is_dir():
            raise MCPConfigurationError(
                f"MCP server cwd is not a directory: {self.config.cwd}"
            )
        log_path = self.workspace.resolve(
            f".codeforge/mcp/{self.name}.stderr.log"
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._stderr_stream = log_path.open("a", encoding="utf-8")
        try:
            self.process = subprocess.Popen(
                command,
                cwd=cwd,
                env=self._build_environment(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr_stream,
                text=True,
                encoding="utf-8",
                errors="strict",
                bufsize=1,
                shell=False,
            )
        except OSError as exc:
            self._close_stderr()
            raise MCPConfigurationError(
                f"failed to start MCP server {self.name}: {exc}"
            ) from exc
        self._reader = threading.Thread(
            target=self._read_stdout,
            name=f"codeforge-mcp-{self.name}",
            daemon=True,
        )
        self._reader.start()
        try:
            result = self.request(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "codeforge-harness",
                        "version": "0.1.0",
                    },
                },
            )
            negotiated = str(result.get("protocolVersion", ""))
            if negotiated not in SUPPORTED_PROTOCOL_VERSIONS:
                raise MCPProtocolError(
                    f"unsupported MCP protocol version: {negotiated or 'missing'}"
                )
            capabilities = result.get("capabilities")
            if not isinstance(capabilities, dict):
                raise MCPProtocolError("MCP initialize result lacks capabilities")
            self.protocol_version = negotiated
            self.server_capabilities = dict(capabilities)
            server_info = result.get("serverInfo", {})
            self.server_info = (
                dict(server_info) if isinstance(server_info, dict) else {}
            )
            self.notify("notifications/initialized")
        except Exception:
            self.close()
            raise

    def list_tools(self) -> list[MCPToolDefinition]:
        """分页读取 Server 暴露的全部工具定义。"""

        if "tools" not in self.server_capabilities:
            raise MCPProtocolError(
                f"MCP server {self.name} did not negotiate tools capability"
            )
        tools: list[MCPToolDefinition] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(100):
            params = {"cursor": cursor} if cursor else {}
            result = self.request("tools/list", params)
            raw_tools = result.get("tools")
            if not isinstance(raw_tools, list):
                raise MCPProtocolError("tools/list result must contain a tools list")
            for raw_tool in raw_tools:
                tools.append(_parse_tool_definition(raw_tool))
            next_cursor = result.get("nextCursor")
            if not next_cursor:
                return tools
            if not isinstance(next_cursor, str) or next_cursor in seen_cursors:
                raise MCPProtocolError("tools/list returned an invalid cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise MCPProtocolError("tools/list exceeded 100 pages")

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """调用一个远端 MCP 工具并返回原始 CallToolResult。"""

        return self.request(
            "tools/call",
            {"name": name, "arguments": arguments},
        )

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """发送带 ID 的 JSON-RPC 请求并在超时内等待对应响应。"""

        process = self._require_process()
        if process.poll() is not None:
            raise MCPProtocolError(
                f"MCP server {self.name} exited with code {process.returncode}"
            )
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        )
        try:
            response = response_queue.get(timeout=self.config.timeout_seconds)
        except queue.Empty as exc:
            try:
                self.notify(
                    "notifications/cancelled",
                    {"requestId": request_id, "reason": "client timeout"},
                )
            except MCPError:
                pass
            raise MCPTimeoutError(
                f"MCP request timed out: server={self.name}, method={method}"
            ) from exc
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
        if "error" in response:
            error = response["error"]
            if isinstance(error, dict):
                code = error.get("code", "unknown")
                message = error.get("message", "MCP request failed")
                raise MCPProtocolError(f"MCP error {code}: {message}")
            raise MCPProtocolError(f"invalid MCP error response: {error!r}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise MCPProtocolError("MCP response result must be an object")
        return result

    def notify(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """发送不含 ID 且无需响应的 JSON-RPC 通知。"""

        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def close(self) -> None:
        """按 stdio 生命周期关闭 stdin，并在需要时终止 Server。"""

        self._closing = True
        process = self.process
        if process is None:
            self._close_stderr()
            return
        if process.stdin is not None and not process.stdin.closed:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
        if self._reader is not None:
            self._reader.join(timeout=1.0)
        self.process = None
        self._close_stderr()

    def _read_stdout(self) -> None:
        """持续解析 stdout 单行 JSON-RPC，并按响应 ID 路由到等待方。"""

        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for raw_line in process.stdout:
                line = raw_line.rstrip("\r\n")
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    self._reader_error = f"invalid JSON on MCP stdout: {exc}"
                    self._fail_pending(self._reader_error)
                    return
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    self._reader_error = "invalid JSON-RPC message on MCP stdout"
                    self._fail_pending(self._reader_error)
                    return
                message_id = message.get("id")
                if message_id is not None and (
                    "result" in message or "error" in message
                ):
                    with self._lock:
                        target = self._pending.get(message_id)
                    if target is not None:
                        target.put(message)
                    continue
                if message_id is not None and "method" in message:
                    self._send(
                        {
                            "jsonrpc": "2.0",
                            "id": message_id,
                            "error": {
                                "code": -32601,
                                "message": "Client method not supported",
                            },
                        }
                    )
                    continue
                self.notifications.append(message)
            if (
                not self._closing
                and self.process is not None
                and self.process.poll() is not None
            ):
                self._reader_error = (
                    f"MCP server {self.name} closed stdout with code "
                    f"{self.process.returncode}"
                )
                self._fail_pending(self._reader_error)
        except (OSError, UnicodeError) as exc:
            self._reader_error = f"failed reading MCP stdout: {exc}"
            self._fail_pending(self._reader_error)

    def _send(self, message: dict[str, Any]) -> None:
        """把一个无内嵌换行的 UTF-8 JSON-RPC 消息写入 Server stdin。"""

        process = self._require_process()
        if process.stdin is None or process.stdin.closed:
            raise MCPProtocolError(f"MCP server stdin is closed: {self.name}")
        serialized = json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            with self._write_lock:
                process.stdin.write(serialized + "\n")
                process.stdin.flush()
        except (OSError, UnicodeError) as exc:
            raise MCPProtocolError(
                f"failed writing to MCP server {self.name}: {exc}"
            ) from exc

    def _fail_pending(self, reason: str) -> None:
        """在 reader 失败时唤醒全部请求并返回统一协议错误。"""

        response = {
            "jsonrpc": "2.0",
            "error": {"code": -32000, "message": reason},
        }
        with self._lock:
            targets = list(self._pending.values())
        for target in targets:
            try:
                target.put_nowait(response)
            except queue.Full:
                continue

    def _require_process(self) -> subprocess.Popen[str]:
        """取得已经启动的 Server 进程，缺失时返回协议错误。"""

        if self.process is None:
            raise MCPProtocolError(f"MCP server is not running: {self.name}")
        if self._reader_error:
            raise MCPProtocolError(self._reader_error)
        return self.process

    def _expand_token(self, value: str) -> str:
        """展开配置中受支持的 Python 和 workspace 占位符。"""

        return value.replace("{python}", sys.executable).replace(
            "{workspace}", str(self.workspace.root)
        )

    def _build_environment(self) -> dict[str, str]:
        """仅继承运行必需变量和配置显式允许传给 Server 的变量。"""

        environment = {
            name: value
            for name, value in os.environ.items()
            if name.upper() in _SAFE_ENVIRONMENT_NAMES
        }
        for name in self.config.env:
            if name in os.environ:
                environment[name] = os.environ[name]
        return environment

    def _close_stderr(self) -> None:
        """关闭 MCP stderr 日志文件句柄。"""

        if self._stderr_stream is not None:
            self._stderr_stream.close()
            self._stderr_stream = None


class StdioMCPManager:
    """统一启动多个 stdio Server、导出工具并在 CLI 退出时清理。"""

    def __init__(
        self,
        configs: dict[str, StdioMCPServerConfig],
        workspace: str | Path,
    ) -> None:
        """为启用的配置创建尚未启动的客户端。"""

        self.clients = {
            name: StdioMCPClient(name, config, workspace)
            for name, config in configs.items()
            if config.enabled
        }

    def start_and_discover(self) -> list["MCPRemoteTool"]:
        """依次启动 Server 并把发现的工具包装为 Registry 工具。"""

        remote_tools: list[MCPRemoteTool] = []
        names: set[str] = set()
        try:
            for server_name, client in self.clients.items():
                client.start()
                for definition in client.list_tools():
                    exposed_name = _exposed_tool_name(
                        server_name,
                        definition.name,
                    )
                    if exposed_name in names:
                        raise MCPConfigurationError(
                            f"duplicate exposed MCP tool: {exposed_name}"
                        )
                    names.add(exposed_name)
                    remote_tools.append(
                        MCPRemoteTool(
                            exposed_name=exposed_name,
                            server_name=server_name,
                            definition=definition,
                            client=client,
                        )
                    )
        except Exception:
            self.close()
            raise
        return remote_tools

    def close(self) -> None:
        """逆序关闭全部已经启动的 MCP Server。"""

        for client in reversed(list(self.clients.values())):
            client.close()


class MCPRemoteTool(BaseTool):
    """把一个远端 MCP Tool 适配成 CodeForge 的本地 BaseTool。"""

    def __init__(
        self,
        *,
        exposed_name: str,
        server_name: str,
        definition: MCPToolDefinition,
        client: StdioMCPClient,
    ) -> None:
        """保留原始 schema，并创建常用 JSON Schema 对应的 Pydantic 模型。"""

        self.name = exposed_name
        self.description = (
            f"[MCP server: {server_name}] {definition.description}"
        )
        self.arguments_model = _arguments_model_from_schema(
            exposed_name,
            definition.input_schema,
        )
        self.server_name = server_name
        self.remote_name = definition.name
        self.input_schema = definition.input_schema
        self.client = client

    def openai_schema(self) -> dict[str, Any]:
        """向模型原样暴露 Server 提供的 inputSchema。"""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    def execute(
        self,
        arguments: ToolArguments,
        context: ToolContext,
    ) -> str | ToolOutput:
        """把已校验参数转发到 tools/call，并统一渲染 MCP 内容块。"""

        try:
            result = self.client.call_tool(
                self.remote_name,
                arguments.model_dump(mode="json", exclude_unset=True),
            )
            rendered = _render_call_tool_result(result)
        except MCPError as exc:
            raise ToolError(str(exc)) from exc
        return ToolOutput(
            content=rendered,
            success=not bool(result.get("isError", False)),
        )


def _parse_tool_definition(raw: Any) -> MCPToolDefinition:
    """校验 tools/list 中单个工具的必需字段。"""

    if not isinstance(raw, dict):
        raise MCPProtocolError("MCP tool definition must be an object")
    name = raw.get("name")
    schema = raw.get("inputSchema")
    if not isinstance(name, str) or not name:
        raise MCPProtocolError("MCP tool name must be a non-empty string")
    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
        raise MCPProtocolError("MCP tool inputSchema must be an object schema")
    description = raw.get("description")
    return MCPToolDefinition(
        name=name,
        description=(
            description if isinstance(description, str) else f"MCP tool {name}"
        ),
        input_schema=dict(schema),
    )


def _exposed_tool_name(server_name: str, remote_name: str) -> str:
    """生成满足常见模型 function name 限制的稳定命名空间名称。"""

    normalized_server = re.sub(r"[^A-Za-z0-9_-]", "_", server_name)
    normalized_tool = re.sub(r"[^A-Za-z0-9_-]", "_", remote_name)
    name = f"mcp_{normalized_server}_{normalized_tool}"
    if len(name) <= 64:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{name[:55]}_{digest}"


def _arguments_model_from_schema(
    tool_name: str,
    schema: dict[str, Any],
) -> type[ToolArguments]:
    """把常见对象 JSON Schema 转成严格 Pydantic 参数模型。"""

    properties = schema.get("properties", {})
    required_items = schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required_items, list):
        raise MCPProtocolError(f"invalid inputSchema for MCP tool {tool_name}")
    required = set(required_items)
    fields: dict[str, tuple[Any, Any]] = {}
    for field_name, raw_field in properties.items():
        if not isinstance(field_name, str) or not isinstance(raw_field, dict):
            raise MCPProtocolError(f"invalid property in MCP tool {tool_name}")
        annotation = _python_type_from_schema(
            raw_field,
            f"{tool_name}_{field_name}",
        )
        default = ... if field_name in required else raw_field.get("default", None)
        field_options: dict[str, Any] = {
            "default": default,
            "description": raw_field.get("description"),
        }
        if raw_field.get("type") == "string":
            field_options.update(
                {
                    "min_length": raw_field.get("minLength"),
                    "max_length": raw_field.get("maxLength"),
                    "pattern": raw_field.get("pattern"),
                }
            )
        elif raw_field.get("type") in {"integer", "number"}:
            field_options.update(
                {
                    "ge": raw_field.get("minimum"),
                    "le": raw_field.get("maximum"),
                }
            )
        elif raw_field.get("type") == "array":
            field_options.update(
                {
                    "min_length": raw_field.get("minItems"),
                    "max_length": raw_field.get("maxItems"),
                }
            )
        field_options = {
            key: value for key, value in field_options.items() if value is not None
        }
        fields[field_name] = (
            annotation,
            Field(**field_options),
        )
    model_name = "MCP_" + re.sub(r"[^A-Za-z0-9_]", "_", tool_name)
    return create_model(model_name, __base__=ToolArguments, **fields)


def _python_type_from_schema(schema: dict[str, Any], name: str) -> Any:
    """递归转换 MCP 参数常见的 primitive、array、object 和 enum 类型。"""

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and enum_values:
        return Literal.__getitem__(tuple(enum_values))
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        non_null = [item for item in schema_type if item != "null"]
        if len(non_null) == 1 and len(non_null) != len(schema_type):
            nested = dict(schema)
            nested["type"] = non_null[0]
            return _python_type_from_schema(nested, name) | None
    if schema_type == "string":
        return str
    if schema_type == "integer":
        return int
    if schema_type == "number":
        return float
    if schema_type == "boolean":
        return bool
    if schema_type == "array":
        items = schema.get("items", {})
        item_type = (
            _python_type_from_schema(items, f"{name}_item")
            if isinstance(items, dict)
            else Any
        )
        return list[item_type]
    if schema_type == "object":
        nested_properties = schema.get("properties")
        if isinstance(nested_properties, dict):
            return _arguments_model_from_schema(name, schema)
        return dict[str, Any]
    return Any


def _render_call_tool_result(result: dict[str, Any]) -> str:
    """把文本、结构化内容和其他 MCP 内容块转换成模型可读字符串。"""

    rendered: list[str] = []
    content = result.get("content", [])
    if not isinstance(content, list):
        raise MCPProtocolError("tools/call content must be a list")
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                rendered.append(text)
                continue
        rendered.append(json.dumps(block, ensure_ascii=False, default=str))
    structured = result.get("structuredContent")
    if structured is not None:
        rendered.append(
            "Structured content:\n"
            + json.dumps(structured, ensure_ascii=False, indent=2, default=str)
        )
    return "\n".join(rendered) if rendered else "(MCP tool returned no content)"


__all__ = [
    "MCPConfigurationError",
    "MCPError",
    "MCPProtocolError",
    "MCPRemoteTool",
    "MCPTimeoutError",
    "StdioMCPClient",
    "StdioMCPManager",
    "StdioMCPServerConfig",
    "load_mcp_config",
]
