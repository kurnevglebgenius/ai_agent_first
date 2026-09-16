"""Конвертация единиц и статистика ограниченных наборов чисел."""

from decimal import Context, Decimal, localcontext
from statistics import mean, median
from .utility_tools import number, schema, success, validated


# Коэффициенты к метру, килограмму и секунде. Температура обрабатывается отдельно.
UNITS = {
    "m": ("length", "1"), "km": ("length", "1000"), "cm": ("length", "0.01"),
    "mm": ("length", "0.001"), "in": ("length", "0.0254"), "ft": ("length", "0.3048"),
    "mi": ("length", "1609.344"), "kg": ("mass", "1"), "g": ("mass", "0.001"),
    "lb": ("mass", "0.45359237"), "s": ("time", "1"), "min": ("time", "60"), "h": ("time", "3600"),
}


def convert_units(args, message):
    value = number(args["value"])
    source, target = args["from_unit"], args["to_unit"]
    with localcontext(Context(prec=40)):
        if source in ("C", "F", "K") and target in ("C", "F", "K"):
            celsius = (value - 32) * 5 / 9 if source == "F" else value - Decimal("273.15") if source == "K" else value
            if celsius < Decimal("-273.15"):
                raise ValueError("Температура ниже абсолютного нуля.")
            result = celsius * 9 / 5 + 32 if target == "F" else celsius + Decimal("273.15") if target == "K" else celsius
        else:
            if source not in UNITS or target not in UNITS or UNITS[source][0] != UNITS[target][0]:
                raise ValueError("Нужны поддерживаемые единицы одной размерности.")
            result = value * Decimal(UNITS[source][1]) / Decimal(UNITS[target][1])
    return success({"value": format(result, "f"), "unit": target})


def summarize_numbers(args, message):
    parts = args["numbers"].split(",")
    if not 1 <= len(parts) <= 100:
        raise ValueError("Нужно от 1 до 100 чисел через запятую.")
    values = [number(part.strip()) for part in parts]
    with localcontext(Context(prec=40)):
        result = {"sum": sum(values), "mean": mean(values), "median": median(values),
                  "min": min(values), "max": max(values)}
    return success({"count": len(values), **{key: format(value, "f") for key, value in result.items()}})


def definitions():
    return [
        (schema("convert_units", "Перевод единиц одной размерности: длина, масса, время, температура. Точность до 40 значащих цифр.",
                {"value": "Десятичное число", "from_unit": "m, km, cm, mm, in, ft, mi; kg, g, lb; s, min, h; C, F, K",
                 "to_unit": "Целевая единица из той же группы"}), validated(convert_units)),
        (schema("summarize_numbers", "Количество, сумма, среднее, медиана, минимум и максимум до 100 чисел.",
                {"numbers": "Числа через запятую, десятичный разделитель точка, например 1.5, 2, 3"}), validated(summarize_numbers)),
    ]
