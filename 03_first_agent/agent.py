"""Совместимый импорт: реализация находится в shared/agent_runtime."""

from pathlib import Path
import sys

_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shared.agent_runtime.agent import (  # noqa: E402, F401
    EMPTY_ANSWER, LIMIT_ANSWER, MAX_TOOL_CALLS, FirstAgent
)
