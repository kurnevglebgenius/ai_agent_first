"""Единый вход для инструментов модели: разрешение, проверка, исполнение."""

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
import json
from typing import Callable

from .memory_tool import remember_fact
from .model_provider import CALCULATOR_SCHEMA, MEMORY_SCHEMA, ToolCall
from . import utility_tools as utility
from . import data_tools
from .public_tools import PublicTools


class Permission(str, Enum):
    READ = "read"
    WRITE = "write"
    EXTERNAL = "external"


@dataclass(frozen=True)
class ToolDefinition:
    schema: dict
    permission: Permission
    max_argument_bytes: int
    execute: Callable[[dict, str], dict]
    requires_confirmation: bool = False


class ToolRegistry:
    """Разрешения выдаёт приложение по имени и классу, никогда не модель.

    Текущий контракт аргументов: плоский объект обязательных строковых полей.
    Для иных типов сначала следует расширить валидатор и его тесты.
    """

    def __init__(self, definitions, grants):
        self._tools = {}
        self._grants = frozenset(grants)
        for definition in definitions:
            schema = deepcopy(definition.schema)
            name = schema["name"]
            parameters = schema["parameters"]
            if (not isinstance(name, str) or not name.strip()
                    or not isinstance(schema.get("description"), str) or not schema["description"].strip()
                    or schema.get("type") != "function" or schema.get("strict") is not True
                    or not isinstance(definition.permission, Permission)
                    or type(definition.requires_confirmation) is not bool
                    or type(definition.max_argument_bytes) is not int or definition.max_argument_bytes <= 0
                    or not callable(definition.execute)
                    or name in self._tools or parameters.get("type") != "object"
                    or parameters.get("additionalProperties") is not False
                    or set(parameters["required"]) != set(parameters["properties"])
                    or any(p != {"type": "string"} for p in (
                        {k: v for k, v in prop.items() if k != "description"}
                        for prop in parameters["properties"].values()))):
                raise ValueError("Повторное имя или неподдерживаемая схема инструмента.")
            self._tools[name] = ToolDefinition(
                schema, definition.permission, definition.max_argument_bytes, definition.execute,
                definition.requires_confirmation)

    @staticmethod
    def _blocked(tool):
        # До реализации подтверждения конкретных аргументов чувствительные действия закрыты.
        return tool.requires_confirmation or tool.permission == Permission.EXTERNAL

    def schemas(self):
        return [deepcopy(tool.schema) for name, tool in self._tools.items()
                if (name, tool.permission) in self._grants and not self._blocked(tool)]

    def remote_read_names(self):
        return {name for name, tool in self._tools.items()
                if tool.permission == Permission.READ
                and (name, tool.permission) in self._grants and not self._blocked(tool)}

    def run(self, call: ToolCall, message: str = "") -> str:
        def failure(error):
            return json.dumps({"ok": False, "value": None, "error": error}, ensure_ascii=False)

        tool = self._tools.get(call.name)
        if tool is None or (call.name, tool.permission) not in self._grants:
            return failure("Инструмент не разрешён.")
        if self._blocked(tool):
            return failure("Нужно подтверждение конкретного действия; механизм подтверждения пока не подключён.")
        try:
            if (not isinstance(call.arguments, str)
                    or len(call.arguments.encode("utf-8")) > tool.max_argument_bytes):
                raise ValueError()

            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result

            args = json.loads(call.arguments, object_pairs_hook=unique_object)
            fields = tool.schema["parameters"]["properties"]
            if (not isinstance(args, dict) or set(args) != set(fields)
                    or not all(isinstance(value, str) for value in args.values())):
                raise ValueError()
        except (ValueError, UnicodeError, RecursionError):
            return failure("Неверные аргументы инструмента.")
        try:
            result = tool.execute(args, message)
            if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                raise ValueError()
            return json.dumps({"ok": result["ok"], "value": result.get("value"),
                               "error": result.get("error")}, ensure_ascii=False, allow_nan=False)
        except Exception:
            # Не отдаём модели пути, секреты или сырые ошибки обработчика. Не повторяем запись.
            return failure("Не удалось выполнить инструмент.")


class ToolRouter:
    """Выбранные MCP-инструменты заменяют одноимённые локальные схемы и вызовы."""

    def __init__(self, local: ToolRegistry, remote):
        self.local = local
        self.remote = remote
        remote_schemas = remote.schemas()
        self._remote_names = {schema["name"] for schema in remote_schemas}
        if len(self._remote_names) != len(remote_schemas):
            raise ValueError("Повторное имя MCP-инструмента.")
        if not self._remote_names <= local.remote_read_names():
            raise ValueError("MCP может заменить только разрешённые READ-инструменты.")
        self._schemas = [schema for schema in local.schemas()
                         if schema["name"] not in self._remote_names] + remote_schemas

    def schemas(self):
        return deepcopy(self._schemas)

    def run(self, call: ToolCall, message: str = "") -> str:
        if call.name in self._remote_names:
            return self.remote.run(call, message)
        return self.local.run(call, message)


def default_tools(dispatcher, memory, *, allow_memory_write=True, allow_public_network=True):
    definitions = [ToolDefinition(
        CALCULATOR_SCHEMA, Permission.READ, 512,
        lambda args, message: json.loads(dispatcher.run(ToolCall(
            "", "calculate_balance", json.dumps(args, ensure_ascii=False)))))]
    grants = {("calculate_balance", Permission.READ)}
    extra = data_tools.definitions()
    if allow_public_network:
        extra += PublicTools().definitions()
    for schema, handler in extra:
        definitions.append(ToolDefinition(schema, Permission.READ, 4096, handler))
        grants.add((schema["name"], Permission.READ))
    for schema, handler in (
        (utility.ARITHMETIC_SCHEMA, utility.calculate_arithmetic),
        (utility.PERCENT_SCHEMA, utility.calculate_percentage),
        (utility.DATES_SCHEMA, utility.days_between),
    ):
        definitions.append(ToolDefinition(schema, Permission.READ, 512, utility.validated(handler)))
        grants.add((schema["name"], Permission.READ))
    if memory is not None:
        definitions.append(ToolDefinition(
            utility.FACTS_SCHEMA, Permission.READ, 1024,
            utility.validated(lambda args, message: utility.search_facts(memory, args, message))))
        grants.add(("search_facts", Permission.READ))
        definitions.append(ToolDefinition(
            MEMORY_SCHEMA, Permission.WRITE, 16000,
            lambda args, message: json.loads(remember_fact(
                memory, json.dumps(args, ensure_ascii=False), message))))
        if allow_memory_write:
            grants.add(("remember_fact", Permission.WRITE))
    return ToolRegistry(definitions, grants)
