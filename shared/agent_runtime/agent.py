"""Прозрачный цикл: запрос модели → разрешённые инструменты → ответ."""

from .model_provider import ModelProvider, OpenAIProvider
from .tool_bridge import CalculatorDispatcher
from .memory import MemoryStore
from .tools import ToolRouter, default_tools


MAX_TOOL_CALLS = 3
EMPTY_ANSWER = "Модель не вернула текстовый ответ. Попробуйте другое сообщение."
LIMIT_ANSWER = "Не удалось завершить запрос за допустимое число шагов. Уточните запрос."


class FirstAgent:
    def __init__(
        self,
        provider: ModelProvider | None = None,
        dispatcher: CalculatorDispatcher | None = None,
        memory: MemoryStore | None = None,
        *,
        allow_memory_write: bool = True,
        allow_public_network: bool = True,
        mcp_tools=None,
    ) -> None:
        self.provider = provider or OpenAIProvider(memory_enabled=memory is not None)
        self.dispatcher = dispatcher or CalculatorDispatcher()
        local_tools = default_tools(self.dispatcher, memory, allow_memory_write=allow_memory_write,
                                    allow_public_network=allow_public_network)
        self.tools = ToolRouter(local_tools, mcp_tools) if mcp_tools is not None else local_tools
        if isinstance(self.provider, OpenAIProvider):
            self.provider.tool_schemas = self.tools.schemas()
            self.provider.memory_enabled = memory is not None and allow_memory_write
        self.previous_response_id: str | None = None
        self.memory = memory

    @property
    def has_context(self) -> bool:
        if self.memory is not None:
            return bool(self.memory.history())
        return self.previous_response_id is not None

    def reset(self) -> None:
        """Начать новый диалог; данные у провайдера не удаляются."""
        if self.memory is not None:
            self.memory.reset()
        self.previous_response_id = None

    def memory_command(self, message: str) -> str | None:
        parts = message.strip().split(maxsplit=1)
        command = parts[0].split("@", 1)[0].lower() if parts else ""
        argument = parts[1] if len(parts) > 1 else ""
        if command not in ("/remember", "/memory", "/forget"):
            return None
        if self.memory is None:
            return "Постоянная память не подключена."
        if command == "/memory":
            facts = self.memory.facts()
            return "\n".join(f"{key} = {value}" for key, value in facts.items()) or "Нет сохранённых фактов."
        if command == "/forget":
            removed = self.memory.forget(argument)
            if removed:
                self.previous_response_id = None
            return "Факт удалён, локальный диалог сброшен." if removed else "Факт не найден. Используйте /forget название."
        name, separator, value = argument.partition("=")
        if not separator:
            return "Используйте /remember название = значение."
        try:
            self.memory.remember(name, value)
        except ValueError as error:
            return str(error)
        return "Факт сохранён."

    def reply(self, message: str) -> str:
        command_answer = self.memory_command(message)
        if command_answer is not None:
            return command_answer
        input_data: str | list[dict] = message
        previous_id = self.previous_response_id
        if self.memory is not None:
            input_data = self.memory.context() + [{"role": "user", "content": message}]
            previous_id = None
        calls_used = 0

        while True:
            response = self.provider.generate(input_data, previous_id)
            if not response.tool_calls:
                if not response.text:
                    return EMPTY_ANSWER
                # Контекст обновляем только после готового ответа пользователю.
                if self.memory is not None:
                    self.memory.save_turn(message, response.text)
                self.previous_response_id = response.response_id
                return response.text

            if calls_used + len(response.tool_calls) > MAX_TOOL_CALLS:
                return LIMIT_ANSWER

            outputs = []
            for call in response.tool_calls:
                outputs.append({
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": self.tools.run(call, message),
                })
            calls_used += len(response.tool_calls)
            input_data = outputs
            previous_id = response.response_id
