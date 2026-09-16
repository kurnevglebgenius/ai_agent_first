"""JSON round-trip, invalid files and protection against overwriting."""

import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "01_python_basics"


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, SOURCE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


basics = load_module("json_example_basics", "business_operation.py")
with patch.dict(sys.modules, {"business_operation": basics}):
    example = load_module("json_example", "business_json.py")


class BusinessJsonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "data" / "operation.json"
        self.operation = {"revenue": "0.30", "expenses": "0.20"}

    def test_round_trip_preserves_exact_money_as_strings(self):
        example.save_operation(self.path, self.operation)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), self.operation)
        loaded = example.load_operation(self.path)
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            example.show_operation(loaded)
        self.assertIn("Разница: 0.10", output.getvalue())

    def test_existing_file_is_never_overwritten(self):
        example.save_operation(self.path, self.operation)
        original = self.path.read_bytes()
        with self.assertRaises(FileExistsError):
            example.save_operation(self.path, {"revenue": "100", "expenses": "0"})
        self.assertEqual(self.path.read_bytes(), original)

    def test_missing_file_is_not_created_by_read(self):
        with self.assertRaises(FileNotFoundError):
            example.load_operation(self.path)
        self.assertFalse(self.path.exists())

    def test_broken_json_is_not_modified(self):
        self.path.parent.mkdir()
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            example.load_operation(self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{broken")

    def test_invalid_schema_and_amounts_rejected(self):
        for data in [
            [], None, {}, {"revenue": "1"},
            {"revenue": 0.3, "expenses": "0"},
            {"revenue": "NaN", "expenses": "0"},
            {"revenue": "1.001", "expenses": "0"},
            {"revenue": "-1", "expenses": "0"},
            {"revenue": "1", "expenses": "0", "extra": "ignored?"},
        ]:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    example.save_operation(self.path, data)
                self.assertFalse(self.path.exists())
                self.path.parent.mkdir(exist_ok=True)
                self.path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(ValueError):
                    example.load_operation(self.path)
                self.path.unlink()

    def test_cli_read_reports_missing_file(self):
        with patch.object(sys, "argv", ["example", "read", "--file", str(self.path)]):
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(example.main(), 1)
        self.assertIn("Файл не найден", output.getvalue())

    def test_cli_save_and_read(self):
        for command in ("save", "read"):
            with patch.object(sys, "argv", ["example", command, "--file", str(self.path)]):
                with patch("builtins.input", side_effect=["120000", "90000"]) as user_input:
                    with patch("sys.stdout", new_callable=io.StringIO) as output:
                        self.assertEqual(example.main(), 0)
                if command == "read":
                    user_input.assert_not_called()
                self.assertIn("Разница: 30000.00", output.getvalue())

    def test_cancel_does_not_create_file(self):
        with patch.object(sys, "argv", ["example", "save", "--file", str(self.path)]):
            with patch("builtins.input", side_effect=["100", EOFError]):
                with patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(example.main(), 1)
        self.assertFalse(self.path.exists())

    def test_cli_handles_file_access_error(self):
        with patch.object(sys, "argv", ["example", "read"]):
            with patch.object(example, "load_operation", side_effect=PermissionError):
                with patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(example.main(), 1)
        self.assertIn("права доступа", output.getvalue())


if __name__ == "__main__":
    unittest.main()
