"""Небольшой инструмент: проверенные суммы на входе, объект результата на выходе."""

from dataclasses import dataclass

from business_operation import calculate_balance, parse_amount


@dataclass(frozen=True)
class ToolResult:
    """Одинаковые поля для успешного результата и ошибки."""

    ok: bool
    value: str | None = None
    error: str | None = None


class CalculatorTool:
    name = "calculate_balance"
    description = "Вычислить разницу выручки и расходов за один период в одной валюте."

    def run(self, revenue: str, expenses: str) -> ToolResult:
        # Инструмент не читает терминал и не печатает: его вызывает другая программа.
        if not isinstance(revenue, str) or not isinstance(expenses, str):
            return ToolResult(ok=False, error="Передайте обе суммы строками, например '1250.50'.")
        try:
            balance = calculate_balance(parse_amount(revenue), parse_amount(expenses))
        except ValueError as error:
            return ToolResult(ok=False, error=str(error))
        return ToolResult(ok=True, value=format(balance, ".2f"))
