"""Сохранение одной операции в JSON и чтение при следующем запуске."""

import argparse
import json
from pathlib import Path

from business_operation import calculate_balance, parse_amount, read_amount


DEFAULT_FILE = Path(__file__).resolve().parent / "data" / "operation.json"


def validate_operation(data: object) -> dict[str, str]:
    """Принимать только две денежные суммы в виде строк."""
    if not isinstance(data, dict) or set(data) != {"revenue", "expenses"}:
        raise ValueError("Ожидается объект с полями revenue и expenses.")
    result = {}
    for name in ("revenue", "expenses"):
        if not isinstance(data[name], str):
            raise ValueError(f"Поле {name} должно быть строкой с суммой.")
        result[name] = format(parse_amount(data[name]), ".2f")
    return result


def save_operation(path: Path, data: dict[str, str]) -> None:
    """Создать новый файл; существующие данные не перезаписывать."""
    operation = validate_operation(data)
    content = json.dumps(operation, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Режим x откажет, если файл уже существует.
    with path.open("x", encoding="utf-8") as file:
        file.write(content)


def load_operation(path: Path) -> dict[str, str]:
    """Прочитать и проверить файл, не изменяя его."""
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    return validate_operation(data)


def show_operation(operation: dict[str, str]) -> None:
    revenue = parse_amount(operation["revenue"])
    expenses = parse_amount(operation["expenses"])
    print(f"Выручка: {revenue:.2f}")
    print(f"Расходы: {expenses:.2f}")
    print(f"Разница: {calculate_balance(revenue, expenses):.2f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("save", "read"), help="сохранить или прочитать")
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE, help="путь к JSON-файлу")
    args = parser.parse_args()
    try:
        if args.command == "save":
            if args.file.exists():
                raise FileExistsError(args.file)
            print("Введите суммы за один период в одной валюте.")
            revenue = read_amount("Выручка: ")
            expenses = read_amount("Расходы: ")
            save_operation(args.file, {
                "revenue": format(revenue, ".2f"),
                "expenses": format(expenses, ".2f"),
            })
            print(f"Сохранено: {args.file}")
        operation = load_operation(args.file)
        show_operation(operation)
        return 0
    except FileExistsError:
        print("Файл уже существует. Используйте read или другой путь через --file.")
    except FileNotFoundError:
        print("Файл не найден. Сначала сохраните операцию командой save.")
    except (ValueError, UnicodeError) as error:
        print(f"Некорректные данные: {error}")
    except OSError:
        print("Не удалось прочитать или записать файл. Проверьте путь и права доступа.")
    except (EOFError, KeyboardInterrupt):
        print("\nВвод отменён.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
