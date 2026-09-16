"""Локальные инструменты без побочных действий и внешних зависимостей."""

from datetime import date
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
import re


def schema(name, description, fields):
    return {
        "type": "function", "name": name, "description": description, "strict": True,
        "parameters": {
            "type": "object",
            "properties": {key: {"type": "string", "description": text} for key, text in fields.items()},
            "required": list(fields), "additionalProperties": False,
        },
    }


ARITHMETIC_SCHEMA = schema(
    "calculate_arithmetic", "Выполнить одно арифметическое действие над двумя числами без исполнения кода.",
    {"operation": "add, subtract, multiply или divide", "left": "Первое число", "right": "Второе число"})
PERCENT_SCHEMA = schema(
    "calculate_percentage", "Вычислить указанный процент от числа (base × percent / 100), не изменение цены.",
    {"base": "Исходное число", "percent": "Процент, например 20 или 12.5"})
DATES_SCHEMA = schema(
    "days_between", "Разница end_date минус start_date в календарных днях; один день даёт 0.",
    {"start_date": "Начальная дата YYYY-MM-DD", "end_date": "Конечная дата YYYY-MM-DD"})
FACTS_SCHEMA = schema(
    "search_facts", "Найти сохранённые факты текущего пользователя по подстроке названия или значения.",
    {"query": "Непустая строка поиска до 100 символов; поиск без учёта регистра"})


def number(text):
    # Ограничения до Decimal исключают NaN, Infinity, экспоненты и огромные значения.
    if not isinstance(text, str) or not re.fullmatch(r"-?[0-9]{1,12}(?:\.[0-9]{1,6})?", text):
        raise ValueError("Нужно число: до 12 цифр целой части и 6 после точки, без пробелов и экспоненты.")
    return Decimal(text)


def success(value):
    return {"ok": True, "value": value, "error": None}


def calculate_arithmetic(args, message):
    left, right = number(args["left"]), number(args["right"])
    operation = args["operation"]
    with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
        if operation == "add":
            result = left + right
        elif operation == "subtract":
            result = left - right
        elif operation == "multiply":
            result = left * right
        elif operation == "divide":
            if right == 0:
                raise ValueError("Делить на ноль нельзя.")
            result = left / right
        else:
            raise ValueError("Допустимые операции: add, subtract, multiply, divide.")
    return success(format(result, "f"))


def calculate_percentage(args, message):
    base, percent = number(args["base"]), number(args["percent"])
    with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
        result = base * percent / 100
    return success(format(result, "f"))


def days_between(args, message):
    values = []
    for key in ("start_date", "end_date"):
        text = args[key]
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
            raise ValueError("Нужна существующая дата в формате YYYY-MM-DD.")
        try:
            values.append(date.fromisoformat(text))
        except ValueError:
            raise ValueError("Нужна существующая дата в формате YYYY-MM-DD.") from None
    return success({"days": (values[1] - values[0]).days})


def search_facts(memory, args, message):
    query = args["query"].strip()
    if not query or len(query) > 100:
        raise ValueError("Нужна непустая строка поиска до 100 символов.")
    query = query.casefold()
    return success({name: value for name, value in memory.facts().items()
                    if query in name.casefold() or query in value.casefold()})


def validated(handler):
    """Показываем только контролируемые ошибки проверки этих локальных функций."""
    def execute(args, message):
        try:
            return handler(args, message)
        except ValueError as error:
            return {"ok": False, "value": None, "error": str(error)}
    return execute
