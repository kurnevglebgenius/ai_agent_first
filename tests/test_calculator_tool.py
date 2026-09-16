"""Проверки контракта инструмента и его терминального примера."""

import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "01_python_basics"


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, SOURCE / filename)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: module}):
        spec.loader.exec_module(module)
    return module


basics = load_module("tool_basics", "business_operation.py")
with patch.dict(sys.modules, {"business_operation": basics}):
    calculator = load_module("calculator_tool", "calculator_tool.py")
with patch.dict(sys.modules, {"calculator_tool": calculator}):
    demo = load_module("tool_demo", "tool_example.py")


class CalculatorToolTests(unittest.TestCase):
    def test_exact_success_result(self):
        result = calculator.CalculatorTool().run("0,30", "0.20")
        self.assertIsInstance(result, calculator.ToolResult)
        self.assertTrue(result.ok)
        self.assertEqual(result.value, "0.10")
        self.assertIsNone(result.error)

    def test_negative_zero_and_maximum_amount(self):
        tool = calculator.CalculatorTool()
        for revenue, expenses, expected in [
            ("10", "30", "-20.00"), ("10", "10", "0.00"),
            ("999999999999.99", "0.01", "999999999999.98"),
        ]:
            with self.subTest(expected=expected):
                self.assertEqual(tool.run(revenue, expenses).value, expected)

    def test_invalid_inputs_return_error_without_value(self):
        for revenue, expenses in [
            ("oops", "0"), ("1", "NaN"), ("1.001", "0"),
            ("-1", "0"), (1.5, "0"), ("1", None), (True, "0"),
        ]:
            with self.subTest(revenue=revenue, expenses=expenses):
                result = calculator.CalculatorTool().run(revenue, expenses)
                self.assertFalse(result.ok)
                self.assertIsNone(result.value)
                self.assertTrue(result.error)

    def test_tool_has_no_terminal_interaction_or_stale_state(self):
        tool = calculator.CalculatorTool()
        with patch("builtins.input", side_effect=AssertionError("Unexpected input")):
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                failed = tool.run("bad", "0")
                succeeded = tool.run("120000", "90000")
        self.assertFalse(failed.ok)
        self.assertEqual(succeeded.value, "30000.00")
        self.assertIsNone(succeeded.error)
        self.assertEqual(output.getvalue(), "")

    def test_cli_outputs_json_and_exit_code(self):
        for revenue, expected_ok, exit_code in [("120000", True, 0), ("bad", False, 1)]:
            with self.subTest(revenue=revenue):
                with patch.object(sys, "argv", ["tool_example", revenue, "90000"]):
                    with patch("sys.stdout", new_callable=io.StringIO) as output:
                        self.assertEqual(demo.main(), exit_code)
                result = json.loads(output.getvalue())
                self.assertEqual(result["tool"], "calculate_balance")
                self.assertEqual(result["ok"], expected_ok)
                if expected_ok:
                    self.assertEqual(result["value"], "30000.00")
                else:
                    self.assertIsNone(result["value"])
                    self.assertTrue(result["error"])


if __name__ == "__main__":
    unittest.main()
