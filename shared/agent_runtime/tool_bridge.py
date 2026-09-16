"""Единственный разрешённый инструмент текущего агента: локальный калькулятор."""

from dataclasses import asdict
import json
from pathlib import Path
import sys

from .model_provider import ToolCall


# Учебный модуль запускается отдельно; добавляем его каталог без зависимости от cwd.
_EXAMPLES = str(Path(__file__).resolve().parents[2] / "01_python_basics")
if _EXAMPLES not in sys.path:
    sys.path.insert(0, _EXAMPLES)
from calculator_tool import CalculatorTool  # noqa: E402


MAX_ARGUMENT_BYTES = 512


class CalculatorDispatcher:
    """Проверка разрешения и аргументов до вызова работающего калькулятора."""

    def __init__(self, tool: CalculatorTool | None = None) -> None:
        self.tool = tool or CalculatorTool()

    def run(self, call: ToolCall) -> str:
        # Чистый расчёт относится к READ: он не меняет файлы и не отправляет сообщения.
        if call.name != self.tool.name:
            return json.dumps({"ok": False, "value": None, "error": "Инструмент не разрешён."})
        try:
            size = len(call.arguments.encode("utf-8")) if isinstance(call.arguments, str) else None
        except UnicodeError:
            size = None
        if size is None or size > MAX_ARGUMENT_BYTES:
            return json.dumps({"ok": False, "value": None, "error": "Аргументы слишком велики или неверны."})
        try:
            args = json.loads(call.arguments)
        except ValueError:
            args = None
        if not isinstance(args, dict) or set(args) != {"revenue", "expenses"}:
            return json.dumps({"ok": False, "value": None, "error": "Нужны только revenue и expenses."})
        result = self.tool.run(args["revenue"], args["expenses"])
        return json.dumps(asdict(result), ensure_ascii=False)
