"""Интеграция MCP-клиента с разрешениями и реальным stdio-процессом."""

import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from mcp import Client, MCPError, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import INVALID_PARAMS


ROOT = Path(__file__).resolve().parents[1]
SERVER_FILE = ROOT / "07_mcp" / "server.py"
spec = importlib.util.spec_from_file_location("lab_mcp_server", SERVER_FILE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class McpServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_list_and_schema_come_from_registry(self):
        async with Client(module.build_server(), raise_exceptions=True) as client:
            tools = (await client.list_tools()).tools
            names = {tool.name for tool in tools}
            self.assertEqual(names, {
                "calculate_balance", "calculate_arithmetic", "calculate_percentage",
                "days_between", "convert_units", "summarize_numbers",
            })
            registry_schemas = module.default_tools(module.CalculatorDispatcher(), None,
                                                    allow_public_network=False).schemas()
            days_schema = next(schema for schema in registry_schemas
                               if schema["name"] == "days_between")
            self.assertEqual(next(tool for tool in tools if tool.name == "days_between").input_schema,
                             days_schema["parameters"])

    async def test_opt_in_only_adds_public_read_tools(self):
        async with Client(module.build_server(allow_public_network=True),
                          raise_exceptions=True) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            self.assertEqual(names - {
                "calculate_balance", "calculate_arithmetic", "calculate_percentage",
                "days_between", "convert_units", "summarize_numbers",
            }, {"find_places", "get_weather", "convert_currency", "search_wikipedia"})
            self.assertNotIn("remember_fact", names)

    async def test_results_and_errors_use_mcp_contract(self):
        async with Client(module.build_server(), raise_exceptions=True) as client:
            success = await client.call_tool("calculate_arithmetic", {
                "operation": "add", "left": "0.1", "right": "0.2"})
            self.assertFalse(success.is_error)
            self.assertEqual(success.structured_content,
                             {"ok": True, "value": "0.3", "error": None})
            self.assertEqual(json.loads(success.content[0].text), success.structured_content)

            invalid = await client.call_tool("calculate_arithmetic", {
                "operation": "divide", "left": "1", "right": "0"})
            self.assertTrue(invalid.is_error)
            self.assertFalse(invalid.structured_content["ok"])
            self.assertEqual(json.loads(invalid.content[0].text), invalid.structured_content)

            missing = await client.call_tool("calculate_arithmetic", {"operation": "add"})
            self.assertTrue(missing.is_error)
            self.assertEqual(missing.structured_content["error"], "Неверные аргументы инструмента.")

            with self.assertRaises(MCPError) as error:
                await client.call_tool("remember_fact", {"name": "x"})
            self.assertEqual(error.exception.error.code, INVALID_PARAMS)

    async def test_real_stdio_exchange(self):
        parameters = StdioServerParameters(command=sys.executable, args=[str(SERVER_FILE)])
        async with Client(stdio_client(parameters)) as client:
            self.assertEqual(client.protocol_version, "2026-07-28")
            self.assertIn("convert_units", {tool.name for tool in (await client.list_tools()).tools})
            result = await client.call_tool("days_between", {
                "start_date": "2024-02-28", "end_date": "2024-03-01"})
            self.assertEqual(result.structured_content["value"], {"days": 2})

    async def test_process_rate_limit(self):
        with patch.object(module, "MAX_CALLS_PER_MINUTE", 1):
            async with Client(module.build_server(), raise_exceptions=True) as client:
                arguments = {"start_date": "2024-02-28", "end_date": "2024-03-01"}
                self.assertFalse((await client.call_tool("days_between", arguments)).is_error)
                limited = await client.call_tool("days_between", arguments)
                self.assertTrue(limited.is_error)
                self.assertIn("Слишком много", limited.structured_content["error"])
