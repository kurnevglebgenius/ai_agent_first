"""Локальный MCP-адаптер к проверенному реестру инструментов проекта."""

import argparse
import asyncio
from collections import deque
import json
from pathlib import Path
import sys
from time import monotonic

# При запуске файла из произвольной рабочей папки находим общий runtime проекта.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import MCPError
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    INVALID_PARAMS,
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

from shared.agent_runtime.model_provider import ToolCall
from shared.agent_runtime.tool_bridge import CalculatorDispatcher
from shared.agent_runtime.tools import default_tools


MAX_CALLS_PER_MINUTE = 60
MAX_CONCURRENT_CALLS = 4
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "value": {},
        "error": {"type": ["string", "null"]},
    },
    "required": ["ok", "value", "error"],
    "additionalProperties": False,
}


def build_server(*, allow_public_network: bool = False) -> Server:
    """Публикуем только права, заранее выданные локальному процессу."""
    registry = default_tools(
        CalculatorDispatcher(), None, allow_public_network=allow_public_network
    )
    schemas = registry.schemas()
    advertised = {
        schema["name"]: Tool(
            name=schema["name"],
            description=schema["description"],
            input_schema=schema["parameters"],
            output_schema=OUTPUT_SCHEMA,
        )
        for schema in schemas
    }
    recent_calls = deque()
    rate_lock = asyncio.Lock()
    slots = asyncio.Semaphore(MAX_CONCURRENT_CALLS)

    def mcp_result(data: dict) -> CallToolResult:
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
            structured_content=data,
            is_error=not data["ok"],
        )

    async def list_tools(
        ctx: ServerRequestContext, params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        if params is not None and params.cursor:
            raise MCPError(INVALID_PARAMS, "Pagination cursor is not supported")
        return ListToolsResult(tools=list(advertised.values()))

    async def call_tool(
        ctx: ServerRequestContext, params: CallToolRequestParams
    ) -> CallToolResult:
        if params.name not in advertised:
            raise MCPError(INVALID_PARAMS, "Unknown or unavailable tool")
        async with rate_lock:
            now = monotonic()
            while recent_calls and now - recent_calls[0] >= 60:
                recent_calls.popleft()
            if len(recent_calls) >= MAX_CALLS_PER_MINUTE:
                return mcp_result({"ok": False, "value": None,
                                   "error": "Слишком много вызовов; повторите позже."})
            recent_calls.append(now)
        try:
            arguments = json.dumps(params.arguments or {}, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, OverflowError):
            arguments = "null"  # Реестр вернёт обычную ошибку аргументов.
        async with slots:
            raw = await asyncio.to_thread(registry.run, ToolCall("mcp", params.name, arguments))
        result = json.loads(raw)
        return mcp_result(result)

    return Server("ai-agents-lab", version="0.1.0", on_list_tools=list_tools,
                  on_call_tool=call_tool)


async def serve(*, allow_public_network: bool = False) -> None:
    server = build_server(allow_public_network=allow_public_network)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-public-network", action="store_true",
                        help="Разрешить четыре справочных GET-инструмента")
    args = parser.parse_args()
    asyncio.run(serve(allow_public_network=args.allow_public_network))


if __name__ == "__main__":
    main()
