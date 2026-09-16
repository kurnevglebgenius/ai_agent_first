"""Вызвать калькулятор как инструмент и показать структурированный результат."""

import argparse
from dataclasses import asdict
import json

from calculator_tool import CalculatorTool


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revenue", help="выручка, например 120000 или 1250,50")
    parser.add_argument("expenses", help="расходы за тот же период и в той же валюте")
    args = parser.parse_args()

    tool = CalculatorTool()
    result = tool.run(args.revenue, args.expenses)
    print(json.dumps({"tool": tool.name, **asdict(result)}, ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
