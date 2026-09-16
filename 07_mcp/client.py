"""Локальный MCP-клиент: список инструментов, ручной вызов и smoke-check."""

import argparse
import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path
import sys

from mcp import Client, MCPError, StdioServerParameters
from mcp.client.stdio import stdio_client


SERVER_FILE = Path(__file__).resolve().with_name("server.py")
CONNECT_TIMEOUT = 15
CALL_TIMEOUT = 20


def parse_arguments(items: list[str]) -> dict[str, str]:
    """Все текущие схемы реестра принимают строки; повторные ключи запрещены."""
    result = {}
    for item in items:
        name, separator, value = item.partition("=")
        if not separator or not name or name in result:
            raise ValueError("Аргументы задаются как --arg имя=значение без повторов.")
        result[name] = value
    return result


async def list_all_tools(client: Client):
    tools = []
    cursor = None
    while True:
        page = await client.list_tools(cursor=cursor)
        tools.extend(page.tools)
        if page.next_cursor is None:
            return tools
        if page.next_cursor == cursor:
            raise ValueError("Сервер вернул повторный курсор списка инструментов.")
        cursor = page.next_cursor


async def timed_list_tools(client: Client):
    async with asyncio.timeout(CALL_TIMEOUT):
        return await list_all_tools(client)


async def timed_call_tool(client: Client, name: str, arguments: dict):
    async with asyncio.timeout(CALL_TIMEOUT):
        return await client.call_tool(name, arguments)


async def check_server(client: Client) -> None:
    """Проверяем именно протокол и несколько разных локальных инструментов."""
    names = {tool.name for tool in await timed_list_tools(client)}
    required = {"calculate_arithmetic", "days_between", "convert_units"}
    if not required <= names or "remember_fact" in names:
        raise ValueError("Неверный набор доступных MCP-инструментов.")
    cases = (
        ("calculate_arithmetic", {"operation": "add", "left": "0.1", "right": "0.2"}, "0.3"),
        ("days_between", {"start_date": "2024-02-28", "end_date": "2024-03-01"}, {"days": 2}),
        ("convert_units", {"value": "1", "from_unit": "km", "to_unit": "m"},
         {"value": "1000", "unit": "m"}),
    )
    for name, arguments, expected in cases:
        response = await timed_call_tool(client, name, arguments)
        data = response.structured_content
        if response.is_error or not isinstance(data, dict) or not data.get("ok"):
            raise ValueError(f"Проверка {name} не прошла: {data!r}")
        if data.get("value") != expected:
            raise ValueError(f"Проверка {name} вернула неожиданный результат.")
    print(f"MCP подключён; версия протокола {client.protocol_version}; "
          f"инструментов {len(names)}; три вызова прошли.")


async def run_command(args: argparse.Namespace) -> int:
    arguments = parse_arguments(args.arg) if args.command == "call" else None
    server_args = [str(SERVER_FILE)]
    if args.allow_public_network:
        server_args.append("--allow-public-network")
    parameters = StdioServerParameters(command=sys.executable, args=server_args)
    async with AsyncExitStack() as stack:
        async with asyncio.timeout(CONNECT_TIMEOUT):
            client = await stack.enter_async_context(Client(stdio_client(parameters)))
        if args.command == "check":
            await check_server(client)
            return 0
        if args.command == "list":
            tools = await timed_list_tools(client)
            print(f"MCP {client.protocol_version}; доступно инструментов: {len(tools)}")
            for tool in tools:
                print(f"- {tool.name}: {tool.description}")
                if args.details:
                    properties = tool.input_schema.get("properties", {})
                    for name, schema in properties.items():
                        required = "обязательно" if name in tool.input_schema.get("required", []) else "необязательно"
                        print(f"    {name} ({required}): {schema.get('description', 'строка')}")
            return 0
        response = await timed_call_tool(client, args.name, arguments)
        if response.structured_content is not None:
            print(json.dumps(response.structured_content, ensure_ascii=False, indent=2))
        else:
            for block in response.content:
                if block.type == "text":
                    print(block.text)
        return 1 if response.is_error else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-public-network", action="store_true",
                        help="Запустить сервер с четырьмя публичными GET-инструментами")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="Показать инструменты MCP-сервера")
    listing.add_argument("--details", action="store_true", help="Показать входные поля")
    call = commands.add_parser("call", help="Вызвать инструмент с именованными строковыми аргументами")
    call.add_argument("name", help="Имя инструмента")
    call.add_argument("--arg", action="append", default=[], metavar="ИМЯ=ЗНАЧЕНИЕ")
    commands.add_parser("check", help="Проверить протокол и три локальных вызова")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run_command(args))
    except (MCPError, OSError, ValueError, TimeoutError) as error:
        print(f"Ошибка MCP-клиента: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
