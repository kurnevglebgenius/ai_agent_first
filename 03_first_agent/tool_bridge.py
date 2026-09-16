"""Совместимый импорт: реализация находится в shared/agent_runtime."""

from pathlib import Path
import sys

_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shared.agent_runtime.tool_bridge import (  # noqa: E402, F401
    MAX_ARGUMENT_BYTES, CalculatorDispatcher
)
