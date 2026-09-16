import json
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.agent_runtime.tools import default_tools
from shared.agent_runtime.tool_bridge import CalculatorDispatcher
from shared.agent_runtime.model_provider import ToolCall


class DataTests(unittest.TestCase):
    def run_tool(self, name, **args):
        registry = default_tools(CalculatorDispatcher(), None, allow_public_network=False)
        return json.loads(registry.run(ToolCall("c1", name, json.dumps(args))))

    def test_unit_conversions(self):
        for value, source, target, expected in (("1", "mi", "m", "1609.344"),
                ("1", "lb", "kg", "0.45359237"), ("2", "h", "min", "120"),
                ("32", "F", "C", "0"), ("0", "C", "K", "273.15"), ("100", "C", "F", "212")):
            result = self.run_tool("convert_units", value=value, from_unit=source, to_unit=target)
            self.assertTrue(result["ok"])
            self.assertEqual(Decimal(result["value"]["value"]), Decimal(expected))

    def test_invalid_units_and_absolute_zero(self):
        for source, target, value in (("m", "kg", "1"), ("bad", "m", "1"), ("K", "C", "-1"), ("m", "km", "NaN")):
            self.assertFalse(self.run_tool("convert_units", value=value, from_unit=source, to_unit=target)["ok"])

    def test_statistics(self):
        result = self.run_tool("summarize_numbers", numbers="0.1, 0.2, 0.3, 0.4")
        self.assertEqual(result["value"], {"count": 4, "sum": "1.0", "mean": "0.25", "median": "0.25", "min": "0.1", "max": "0.4"})
        self.assertEqual(self.run_tool("summarize_numbers", numbers="-5")["value"]["median"], "-5")

    def test_bad_and_large_lists(self):
        for numbers in ("", "1,,2", "NaN", ",".join(["1"] * 101)):
            self.assertFalse(self.run_tool("summarize_numbers", numbers=numbers)["ok"])


if __name__ == "__main__":
    unittest.main()
