"""Синхронный адаптер агента к локальному MCP-серверу через постоянный stdio-клиент."""

import asyncio
from concurrent.futures import Future, TimeoutError as FutureTimeout
from contextlib import AsyncExitStack
from dataclasses import dataclass
import json
import os
from pathlib import Path
from queue import Queue
import sys
from threading import Thread

from mcp import Client, MCPError, StdioServerParameters
from mcp.client.stdio import stdio_client


LOCAL_TOOLS = frozenset({
    "calculate_balance", "calculate_arithmetic", "calculate_percentage",
    "days_between", "convert_units", "summarize_numbers",
})
PUBLIC_TOOLS = frozenset({
    "find_places", "get_weather", "convert_currency", "search_wikipedia",
})
SERVER_FILE = Path(__file__).resolve().parents[2] / "07_mcp" / "server.py"
CONNECT_TIMEOUT = 15
CALL_TIMEOUT = 20
REQUEST_TIMEOUT = 30
MAX_RESULT_BYTES = 65536


class MCPConnectionError(RuntimeError):
    """Подключение или схема сервера не подходят агенту."""


def _bool_setting(name: str, default: str = "false") -> bool:
    value = os.getenv(name, default).strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name}: используйте true или false.")
    return value == "true"


@dataclass(frozen=True)
class MCPConfig:
    enabled: bool = False
    allow_public_network: bool = False
    tool_names: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "MCPConfig":
        enabled = _bool_setting("MCP_ENABLED")
        public = _bool_setting("MCP_ALLOW_PUBLIC_NETWORK")
        allowed = LOCAL_TOOLS | (PUBLIC_TOOLS if public else frozenset())
        raw_names = os.getenv("MCP_TOOL_NAMES", "").strip()
        names = tuple(part.strip() for part in raw_names.split(",")) if raw_names else tuple(sorted(allowed))
        if not names or len(names) != len(set(names)) or not set(names) <= allowed:
            raise ValueError("MCP_TOOL_NAMES: укажите уникальные имена доступных READ-инструментов.")
        return cls(enabled, public, names)


class MCPToolClient:
    """Один процесс сервера и один клиент; при ошибке следующий запрос переподключается.

    Синхронный FirstAgent вызывает методы из своего потока. SDK-клиент живёт в
    отдельном потоке и обрабатывает очередь; async-контекст открывается и
    закрывается в одной задаче, как требует транспорт SDK.
    """

    def __init__(self, config: MCPConfig, *, server_file: Path = SERVER_FILE):
        if not config.enabled:
            raise ValueError("MCP-клиент создаётся только при MCP_ENABLED=true.")
        permitted = LOCAL_TOOLS | (PUBLIC_TOOLS if config.allow_public_network else frozenset())
        if not config.tool_names or not set(config.tool_names) <= permitted:
            raise ValueError("Нужны разрешённые имена MCP-инструментов.")
        self._allowed = frozenset(config.tool_names)
        self._server_file = Path(server_file).resolve()
        self._public = config.allow_public_network
        self._queue: Queue = Queue()
        self._closed = False
        self._thread = Thread(target=self._thread_main, name="mcp-client", daemon=True)
        self._thread.start()
        try:
            self._schemas = self._load_schemas()
        except Exception:
            self.close()
            raise MCPConnectionError("Не удалось подключить MCP-сервер или проверить его инструменты.") from None

    def _thread_main(self) -> None:
        asyncio.run(self._worker())

    async def _worker(self) -> None:
        stack = AsyncExitStack()
        client = None
        try:
            while True:
                operation, payload, future = await asyncio.to_thread(self._queue.get)
                if operation == "stop":
                    future.set_result(None)
                    break
                try:
                    if client is None:
                        args = [str(self._server_file)]
                        if self._public:
                            args.append("--allow-public-network")
                        params = StdioServerParameters(command=sys.executable, args=args)
                        async with asyncio.timeout(CONNECT_TIMEOUT):
                            client = await stack.enter_async_context(Client(stdio_client(params)))
                    if operation == "list":
                        async with asyncio.timeout(CALL_TIMEOUT):
                            value = await client.list_tools()
                    else:
                        name, arguments = payload
                        async with asyncio.timeout(CALL_TIMEOUT):
                            value = await client.call_tool(name, arguments)
                    if not future.done():
                        future.set_result(value)
                except Exception as error:
                    if not future.done():
                        future.set_exception(error)
                    if isinstance(error, MCPError):
                        # Ошибка конкретного запроса не разрывает исправное соединение.
                        continue
                    try:
                        await stack.aclose()
                    except Exception:
                        pass
                    stack = AsyncExitStack()
                    client = None
        finally:
            try:
                await stack.aclose()
            except Exception:
                pass

    def _request(self, operation: str, payload=None):
        if self._closed or not self._thread.is_alive():
            raise MCPConnectionError("MCP-клиент остановлен.")
        future: Future = Future()
        self._queue.put((operation, payload, future))
        try:
            return future.result(timeout=REQUEST_TIMEOUT)
        except FutureTimeout:
            raise MCPConnectionError("MCP-сервер не ответил за отведённое время.") from None

    def _load_schemas(self) -> list[dict]:
        listed = self._request("list").tools
        selected = {}
        for tool in listed:
            if tool.name not in self._allowed:
                continue
            parameters = tool.input_schema
            properties = parameters.get("properties") if isinstance(parameters, dict) else None
            if (tool.name in selected or not isinstance(tool.description, str)
                    or not tool.description.strip() or not isinstance(parameters, dict)
                    or parameters.get("type") != "object"
                    or parameters.get("additionalProperties") is not False
                    or not isinstance(properties, dict)
                    or set(parameters.get("required", [])) != set(properties)
                    or any(not isinstance(prop, dict) or prop.get("type") != "string"
                           for prop in properties.values())):
                raise MCPConnectionError("MCP-сервер вернул неподдерживаемую схему.")
            selected[tool.name] = {
                "type": "function", "name": tool.name, "description": tool.description,
                "strict": True, "parameters": parameters,
            }
        if set(selected) != self._allowed:
            raise MCPConnectionError("MCP-сервер не опубликовал выбранные инструменты.")
        return [selected[name] for name in sorted(selected)]

    def schemas(self) -> list[dict]:
        return json.loads(json.dumps(self._schemas, ensure_ascii=False))

    def run(self, call, message: str = "") -> str:
        def failure(error: str):
            return json.dumps({"ok": False, "value": None,
                               "error": error},
                              ensure_ascii=False)

        if call.name not in self._allowed:
            return failure("MCP-инструмент не разрешён.")
        try:
            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result

            if (not isinstance(call.arguments, str)
                    or len(call.arguments.encode("utf-8")) > 16000):
                return failure("Неверные аргументы MCP-инструмента.")
            arguments = json.loads(call.arguments, object_pairs_hook=unique_object)
            fields = next(schema["parameters"]["properties"] for schema in self._schemas
                          if schema["name"] == call.name)
            if (not isinstance(arguments, dict) or set(arguments) != set(fields)
                    or not all(isinstance(value, str) for value in arguments.values())):
                return failure("Неверные аргументы MCP-инструмента.")
        except (ValueError, UnicodeError, RecursionError):
            return failure("Неверные аргументы MCP-инструмента.")
        try:
            response = self._request("call", (call.name, arguments))
            data = response.structured_content
            if (not isinstance(data, dict) or set(data) != {"ok", "value", "error"}
                    or type(data["ok"]) is not bool
                    or response.is_error != (not data["ok"])):
                return failure("MCP-сервер вернул неверный результат.")
            raw = json.dumps(data, ensure_ascii=False, allow_nan=False)
            return raw if len(raw.encode("utf-8")) <= MAX_RESULT_BYTES else failure(
                "MCP-сервер вернул слишком большой результат.")
        except Exception:
            # Не повторяем вызов: после сетевой ошибки результат мог быть неизвестен.
            return failure("MCP-инструмент недоступен; повторите запрос позже.")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        future: Future = Future()
        self._queue.put(("stop", None, future))
        try:
            future.result(timeout=5)
        except FutureTimeout:
            pass
        self._thread.join(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
