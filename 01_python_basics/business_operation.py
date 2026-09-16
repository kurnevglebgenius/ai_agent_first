"""Первый пример: выручка минус расходы, без API и сохранения файлов."""

from decimal import Decimal
import re


def parse_amount(text: str) -> Decimal:
    """Проверить сумму и преобразовать текст в точное десятичное число."""
    normalized = text.strip().replace(",", ".")
    # До 12 цифр целой части и до двух знаков после точки. Без округления.
    if not re.fullmatch(r"[0-9]{1,12}(\.[0-9]{1,2})?", normalized):
        raise ValueError(
            "Введите неотрицательную сумму: например, 120000 или 1250,50. "
            "Не более 12 цифр до запятой и двух после, без пробелов внутри."
        )
    return Decimal(normalized)


def calculate_balance(revenue: Decimal, expenses: Decimal) -> Decimal:
    """Вычесть расходы из выручки. Обе суммы — за один период и в одной валюте."""
    return revenue - expenses


def read_amount(prompt: str) -> Decimal:
    """Повторить ввод, если пользователь ошибся в сумме."""
    while True:
        try:
            return parse_amount(input(prompt))
        except ValueError as error:
            print(f"Ошибка: {error}")


def main() -> None:
    print("Расчёт разницы между выручкой и расходами.")
    print("Введите суммы за один период в одной валюте.")
    try:
        revenue = read_amount("Выручка: ")
        expenses = read_amount("Расходы: ")
    except (EOFError, KeyboardInterrupt):
        print("\nВвод отменён. Расчёт не выполнен.")
        return

    balance = calculate_balance(revenue, expenses)
    print(f"Выручка: {revenue:.2f}")
    print(f"Расходы: {expenses:.2f}")
    print(f"Разница: {balance:.2f}")
    if balance < 0:
        print("Расходы превышают выручку.")
    elif balance == 0:
        print("Выручка равна расходам.")
    else:
        print("Выручка превышает расходы.")


if __name__ == "__main__":
    main()
