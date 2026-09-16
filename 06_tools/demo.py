"""Проверка шести новых tools без LLM. --live включает четыре публичных GET."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.agent_runtime.tools import default_tools
from shared.agent_runtime.tool_bridge import CalculatorDispatcher
from shared.agent_runtime.model_provider import ToolCall


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    tools = default_tools(CalculatorDispatcher(), None, allow_public_network=args.live)
    cases = [("convert_units", {"value": "1", "from_unit": "mi", "to_unit": "km"}),
             ("summarize_numbers", {"numbers": "10,20,30"})]
    if args.live:
        cases += [("find_places", {"query": "Minsk"}),
                  ("get_weather", {"latitude": "53.9", "longitude": "27.5667"}),
                  ("convert_currency", {"amount": "100", "from_currency": "EUR", "to_currency": "USD"}),
                  ("search_wikipedia", {"query": "Python programming", "language": "en"})]
    failed = False
    for name, inputs in cases:
        output = json.loads(tools.run(ToolCall("demo", name, json.dumps(inputs))))
        print(name, json.dumps(output, ensure_ascii=True))
        failed |= not output["ok"]
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
