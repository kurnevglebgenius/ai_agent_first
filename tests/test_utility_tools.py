"""Предметные проверки новых инструментов через реальный реестр."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.agent_runtime.agent import FirstAgent
from shared.agent_runtime.memory import MemoryStore
from shared.agent_runtime.model_provider import ModelReply, ToolCall
from shared.agent_runtime.tool_bridge import CalculatorDispatcher
from shared.agent_runtime.tools import default_tools


class UtilityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "memory.sqlite3"
        self.memory = MemoryStore(self.path, "owner")
        self.tools = default_tools(CalculatorDispatcher(), self.memory)

    def run_tool(self, name, **args):
        result = json.loads(self.tools.run(ToolCall("c1", name, json.dumps(args))))
        self.assertEqual(set(result), {"ok", "value", "error"})
        return result

    def test_arithmetic_decimal_precision_and_all_operations(self):
        for operation, left, right, expected in (
            ("add", "0.1", "0.2", "0.3"), ("subtract", "1", "3", "-2"),
            ("multiply", "-1.25", "4", "-5.00"), ("divide", "1", "8", "0.125"),
            ("divide", "1", "3", "0." + "3" * 40),
        ):
            with self.subTest(operation=operation, right=right):
                result = self.run_tool("calculate_arithmetic", operation=operation, left=left, right=right)
                self.assertTrue(result["ok"])
                self.assertEqual(result["value"], expected)

    def test_invalid_numbers_and_division_by_zero(self):
        for value in ("NaN", "Infinity", "1e5", "1,5", " 1", "1.1234567", "9" * 13, "__import__('os')", 1):
            with self.subTest(value=value):
                self.assertFalse(self.run_tool("calculate_arithmetic", operation="add", left=value, right="1")["ok"])
        result = self.run_tool("calculate_arithmetic", operation="divide", left="1", right="-0")
        self.assertFalse(result["ok"])
        self.assertIn("ноль", result["error"])
        self.assertFalse(self.run_tool("calculate_arithmetic", operation="eval", left="1", right="1")["ok"])

    def test_percentage_exact_and_no_implicit_rounding_to_money(self):
        for base, percent, expected in (("250", "12.5", "31.25"), ("0.01", "1", "0.0001"),
                                        ("100", "150", "150"), ("100", "-10", "-10")):
            self.assertEqual(self.run_tool("calculate_percentage", base=base, percent=percent)["value"], expected)
        self.assertFalse(self.run_tool("calculate_percentage", base="1", percent="NaN")["ok"])

    def test_dates_leap_year_same_date_and_reverse(self):
        for start, end, days in (("2024-02-28", "2024-03-01", 2),
                                  ("2025-02-28", "2025-03-01", 1),
                                  ("2024-03-01", "2024-02-28", -2),
                                  ("2024-01-01", "2024-01-01", 0)):
            self.assertEqual(self.run_tool("days_between", start_date=start, end_date=end)["value"], {"days": days})
        for value in ("2025-02-29", "20240101", "2024-1-1", "0000-01-01", "завтра"):
            self.assertFalse(self.run_tool("days_between", start_date=value, end_date="2024-01-01")["ok"])

    def test_fact_search_is_case_insensitive_and_session_scoped(self):
        self.memory.remember("Город", "Минск")
        self.memory.remember("проект", "Агент")
        MemoryStore(self.path, "other").remember("город", "Москва")
        self.assertEqual(self.run_tool("search_facts", query=" ГОРОД ")["value"], {"Город": "Минск"})
        self.assertEqual(self.run_tool("search_facts", query="мин")["value"], {"Город": "Минск"})
        for query in ("Москва", "' OR 1=1 --"):
            self.assertEqual(self.run_tool("search_facts", query=query)["value"], {})
        for query in (" ", "a" * 101):
            self.assertFalse(self.run_tool("search_facts", query=query)["ok"])
        self.assertEqual(len(self.memory.facts()), 2)

    def test_fact_search_unavailable_without_memory_and_available_without_write(self):
        tools = default_tools(CalculatorDispatcher(), None)
        self.assertNotIn("search_facts", [s["name"] for s in tools.schemas()])
        self.assertFalse(json.loads(tools.run(ToolCall("c1", "search_facts", '{"query":"имя"}')))["ok"])
        tools = default_tools(CalculatorDispatcher(), self.memory, allow_memory_write=False)
        self.assertIn("search_facts", [s["name"] for s in tools.schemas()])
        self.assertNotIn("remember_fact", [s["name"] for s in tools.schemas()])

    def test_each_tool_rejects_extra_fields_and_missing_arguments(self):
        for name in ("calculate_arithmetic", "calculate_percentage", "days_between", "search_facts"):
            self.assertFalse(self.run_tool(name)["ok"])
            self.assertFalse(self.run_tool(name, confirmed="true", unknown="yes")["ok"])

    def test_agent_routes_new_tool_result_and_call_id(self):
        provider = Mock()
        provider.generate.side_effect = [ModelReply("r1", "", (
            ToolCall("percent-1", "calculate_percentage", '{"base":"250","percent":"12.5"}'),)),
            ModelReply("r2", "31.25")]
        self.assertEqual(FirstAgent(provider).reply("12.5% от 250"), "31.25")
        output = provider.generate.call_args.args[0][0]
        self.assertEqual(output["call_id"], "percent-1")
        self.assertEqual(json.loads(output["output"])["value"], "31.25")


if __name__ == "__main__":
    unittest.main()
