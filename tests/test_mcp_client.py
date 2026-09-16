"""Проверка команд локального MCP-клиента и безопасного разбора аргументов."""

import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLIENT_FILE = ROOT / "07_mcp" / "client.py"
spec = importlib.util.spec_from_file_location("lab_mcp_client", CLIENT_FILE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class McpClientTests(unittest.TestCase):
    def test_diagnostic_client_times_out_unresponsive_server(self):
        class SlowClient:
            async def list_tools(self, cursor=None):
                await asyncio.sleep(1)

        with patch.object(module, "CALL_TIMEOUT", 0.01):
            with self.assertRaises(TimeoutError):
                asyncio.run(module.timed_list_tools(SlowClient()))

    def test_named_arguments_reject_bad_or_duplicate_keys(self):
        self.assertEqual(module.parse_arguments(["left=0.1", "right=0.2"]),
                         {"left": "0.1", "right": "0.2"})
        for items in (["left"], ["=1"], ["left=1", "left=2"]):
            with self.subTest(items=items), self.assertRaises(ValueError):
                module.parse_arguments(items)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(CLIENT_FILE), *args],
                              cwd=ROOT, capture_output=True, text=True, timeout=30)

    def test_check_uses_real_subprocess_connection(self):
        result = self.run_cli("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2026-07-28", result.stdout)
        self.assertIn("инструментов 6", result.stdout)

    def test_call_and_error_exit_codes(self):
        success = self.run_cli("call", "calculate_arithmetic", "--arg", "operation=add",
                               "--arg", "left=0.1", "--arg", "right=0.2")
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(json.loads(success.stdout)["value"], "0.3")
        failure = self.run_cli("call", "calculate_arithmetic", "--arg", "operation=divide",
                               "--arg", "left=1", "--arg", "right=0")
        self.assertEqual(failure.returncode, 1)
        self.assertFalse(json.loads(failure.stdout)["ok"])
