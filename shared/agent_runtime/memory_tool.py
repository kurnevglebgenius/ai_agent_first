"""Проверка аргументов записи и привязки факта к текущему сообщению."""

import json
import sqlite3


def remember_fact(memory, arguments, message):
    try:
        if not isinstance(arguments, str) or len(arguments.encode("utf-8")) > 16000:
            raise ValueError("Слишком большие аргументы.")
        args = json.loads(arguments)
        if not isinstance(args, dict) or set(args) != {"name", "value", "evidence"}:
            raise ValueError("Нужны name, value и evidence.")
        if not all(isinstance(value, str) and value.strip() for value in args.values()):
            raise ValueError("Аргументы должны быть непустыми строками.")
        if args["evidence"] not in message or args["value"] not in args["evidence"]:
            raise ValueError("Факт должен подтверждаться текущим сообщением пользователя.")
        if memory is None:
            raise ValueError("Постоянная память не подключена.")
        memory.remember(args["name"], args["value"])
        return json.dumps({"ok": True, "name": args["name"].strip(), "value": args["value"].strip()}, ensure_ascii=False)
    except (ValueError, UnicodeError, RecursionError):
        return json.dumps({"ok": False, "error": "Факт не сохранён: проверьте аргументы, источник и лимиты памяти."}, ensure_ascii=False)
    except sqlite3.Error:
        return json.dumps({"ok": False, "error": "Факт не сохранён: локальная база недоступна."}, ensure_ascii=False)
