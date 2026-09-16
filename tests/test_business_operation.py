"""Проверки точных сумм и ввода первого практического примера."""

from decimal import Decimal
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "01_python_basics" / "business_operation.py"
spec = importlib.util.spec_from_file_location("business_operation", SOURCE)
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)


class BusinessOperationTests(unittest.TestCase):
    def test_supported_amounts(self):
        for text, expected in [
            ("120000", "120000"), (" 1250,50 ", "1250.50"),
            ("0.10", "0.10"), ("0", "0"), ("12.5", "12.5"),
            ("999999999999.99", "999999999999.99"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(example.parse_amount(text), Decimal(expected))

    def test_invalid_amounts_are_rejected_without_rounding(self):
        for text in [
            "", " ", "-1", "+1", "1.001", "NaN", "Infinity", "1e3",
            "1 000", "1_000", "1,2.3", "рубли", "1000000000000", ".50", "1.",
        ]:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    example.parse_amount(text)

    def test_exact_decimal_subtraction(self):
        self.assertEqual(
            example.calculate_balance(example.parse_amount("0.30"), example.parse_amount("0.20")),
            Decimal("0.10"),
        )
        self.assertEqual(
            example.calculate_balance(
                example.parse_amount("999999999999.99"), example.parse_amount("0.01")
            ),
            Decimal("999999999999.98"),
        )

    def test_positive_negative_and_zero_results(self):
        for revenue, expenses, expected in [
            ("120000", "90000", "30000"), ("50", "80", "-30"), ("10", "10", "0"),
        ]:
            with self.subTest(expected=expected):
                self.assertEqual(
                    example.calculate_balance(Decimal(revenue), Decimal(expenses)), Decimal(expected)
                )

    def test_cli_retries_bad_input_and_formats_result(self):
        with patch("builtins.input", side_effect=["ошибка", "120000", "90000"]):
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                example.main()
        self.assertIn("Ошибка:", output.getvalue())
        self.assertIn("Разница: 30000.00", output.getvalue())

    def test_cli_explains_negative_and_zero_balance(self):
        for amounts, message in [
            (["50", "80"], "Расходы превышают выручку."),
            (["10", "10"], "Выручка равна расходам."),
        ]:
            with self.subTest(amounts=amounts):
                with patch("builtins.input", side_effect=amounts):
                    with patch("sys.stdout", new_callable=io.StringIO) as output:
                        example.main()
                self.assertIn(message, output.getvalue())

    def test_cancel_does_not_print_partial_calculation(self):
        for inputs in ([EOFError], ["100", KeyboardInterrupt]):
            with self.subTest(inputs=inputs):
                with patch("builtins.input", side_effect=inputs):
                    with patch("sys.stdout", new_callable=io.StringIO) as output:
                        example.main()
                self.assertIn("Расчёт не выполнен.", output.getvalue())
                self.assertNotIn("Разница:", output.getvalue())


if __name__ == "__main__":
    unittest.main()
