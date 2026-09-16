"""Проверки границы разрешений, побочных эффектов и контракта tools."""

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.agent_runtime.agent import FirstAgent
from shared.agent_runtime.memory import MemoryStore
from shared.agent_runtime.model_provider import CALCULATOR_SCHEMA, ModelReply, OpenAIProvider, ToolCall
from shared.agent_runtime.tools import Permission, ToolDefinition, ToolRegistry, ToolRouter


class ToolTests(unittest.TestCase):
    def registry(self, permission=Permission.READ, grants=()):
        self.handler = Mock(return_value={"ok": True, "value": "1.00"})
        return ToolRegistry([ToolDefinition(CALCULATOR_SCHEMA, permission, 512, self.handler)], grants)

    def call(self, arguments='{"revenue":"2","expenses":"1"}', name="calculate_balance"):
        return ToolCall("c1", name, arguments)

    def test_each_permission_requires_explicit_matching_grant(self):
        for permission in (Permission.READ, Permission.WRITE):
            registry = self.registry(permission)
            self.assertEqual(registry.schemas(), [])
            self.assertFalse(json.loads(registry.run(self.call()))["ok"])
            self.handler.assert_not_called()
            registry = self.registry(permission, {("calculate_balance", permission)})
            self.assertTrue(json.loads(registry.run(self.call()))["ok"])
            self.handler.assert_called_once()
        registry = self.registry(Permission.WRITE, {("calculate_balance", Permission.READ)})
        self.assertFalse(json.loads(registry.run(self.call()))["ok"])
        self.handler.assert_not_called()

    def test_unknown_and_invalid_calls_never_reach_handler(self):
        registry = self.registry(grants={("calculate_balance", Permission.READ)})
        invalid = ['{broken', '[]', '{}', 'null', '\ud800', 'x' * 513,
                   '{"revenue":2,"expenses":"1"}',
                   '{"revenue":"2","expenses":"1","extra":"yes"}',
                   '{"revenue":"2","revenue":"3","expenses":"1"}',
                   '[' * 200 + '0' + ']' * 200]
        for arguments in invalid:
            with self.subTest(arguments=repr(arguments[:40])):
                self.assertFalse(json.loads(registry.run(self.call(arguments)))["ok"])
        self.assertFalse(json.loads(registry.run(self.call(name="send_message")))["ok"])
        self.handler.assert_not_called()

    def test_handler_error_is_safe_and_not_retried(self):
        registry = self.registry(grants={("calculate_balance", Permission.READ)})
        self.handler.side_effect = RuntimeError("private-content")
        output = registry.run(self.call())
        self.assertNotIn("private-content", output)
        self.assertEqual(set(json.loads(output)), {"ok", "value", "error"})
        self.assertFalse(json.loads(output)["ok"])
        self.handler.assert_called_once()

    def test_returned_schema_cannot_change_validation(self):
        registry = self.registry(grants={("calculate_balance", Permission.READ)})
        registry.schemas()[0]["parameters"]["properties"]["extra"] = {"type": "string"}
        self.assertTrue(json.loads(registry.run(self.call()))["ok"])

    def test_duplicate_registration_fails(self):
        definition = ToolDefinition(CALCULATOR_SCHEMA, Permission.READ, 512, Mock())
        with self.assertRaises(ValueError):
            ToolRegistry([definition, definition], ())

    def test_mcp_cannot_replace_write_tool(self):
        registry = self.registry(Permission.WRITE, {("calculate_balance", Permission.WRITE)})
        remote = Mock()
        remote.schemas.return_value = [CALCULATOR_SCHEMA]
        with self.assertRaisesRegex(ValueError, "READ"):
            ToolRouter(registry, remote)
        remote.run.assert_not_called()

    def test_disabled_memory_write_cannot_be_requested_by_model(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = MemoryStore(Path(directory) / "memory.sqlite3", "test")
            provider = Mock()
            provider.generate.side_effect = [ModelReply("r1", "", (
                ToolCall("c1", "remember_fact", json.dumps({
                    "name": "имя", "value": "Глеб", "evidence": "Меня зовут Глеб"})),)),
                ModelReply("r2", "Запись отключена")]
            agent = FirstAgent(provider, memory=memory, allow_memory_write=False)
            agent.reply("Меня зовут Глеб")
            self.assertEqual(memory.facts(), {})
            output = provider.generate.call_args.args[0][0]
            self.assertEqual(output["call_id"], "c1")
            self.assertFalse(json.loads(output["output"])["ok"])
            # Явная команда владельца сохраняет прежнее поведение.
            agent.reply("/remember имя = Глеб")
            self.assertEqual(memory.facts(), {"имя": "Глеб"})

    def test_agent_advertises_only_its_available_tools(self):
        sdk = Mock()
        sdk.responses.create.return_value = SimpleNamespace(
            id="r1", status="completed", output=[], output_text="ok")
        provider = OpenAIProvider(client=sdk, memory_enabled=True)
        FirstAgent(provider).reply("Привет")
        self.assertEqual([schema["name"] for schema in sdk.responses.create.call_args.kwargs["tools"]],
                         ["calculate_balance", "convert_units", "summarize_numbers", "find_places",
                          "get_weather", "convert_currency", "search_wikipedia",
                          "calculate_arithmetic", "calculate_percentage", "days_between"])

    def test_dangerous_tools_are_blocked_even_with_grants(self):
        for permission, confirmation in ((Permission.EXTERNAL, False), (Permission.WRITE, True),
                                         (Permission.READ, True)):
            handler = Mock()
            registry = ToolRegistry([ToolDefinition(
                CALCULATOR_SCHEMA, permission, 512, handler, requires_confirmation=confirmation)],
                {("calculate_balance", permission)})
            self.assertEqual(registry.schemas(), [])
            self.assertFalse(json.loads(registry.run(self.call()))["ok"])
            handler.assert_not_called()

    def test_tool_requires_description_and_valid_limits(self):
        from copy import deepcopy
        for description in ("", None, 1):
            schema = deepcopy(CALCULATOR_SCHEMA)
            schema["description"] = description
            with self.assertRaises(ValueError):
                ToolRegistry([ToolDefinition(schema, Permission.READ, 512, Mock())], ())
        for limit in (0, -1, True):
            with self.assertRaises(ValueError):
                ToolRegistry([ToolDefinition(CALCULATOR_SCHEMA, Permission.READ, limit, Mock())], ())


if __name__ == "__main__":
    unittest.main()
