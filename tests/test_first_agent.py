"""Offline checks for the agent loop, provider mapping and terminal interface."""

import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from openai import OpenAIError


SOURCE = Path(__file__).resolve().parents[1] / "03_first_agent"
sys.path.insert(0, str(SOURCE))
from agent import EMPTY_ANSWER, LIMIT_ANSWER, FirstAgent
from model_provider import ModelReply, ModelResponseError, OpenAIProvider, ToolCall
from tool_bridge import CalculatorDispatcher
import main as cli


def tool_reply(response_id="r1", call_id="c1", name="calculate_balance", arguments=None):
    return ModelReply(
        response_id, "", (
            ToolCall(call_id, name, arguments or '{"revenue":"120000","expenses":"90000"}'),
        )
    )


class FakeProvider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def generate(self, input_data, previous_response_id):
        self.requests.append((input_data, previous_response_id))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


class AgentTests(unittest.TestCase):
    def test_plain_text_and_next_turn_keep_context(self):
        provider = FakeProvider([ModelReply("r1", "Привет"), ModelReply("r2", "Снова привет")])
        agent = FirstAgent(provider)
        self.assertEqual(agent.reply("Первый"), "Привет")
        self.assertEqual(agent.reply("Второй"), "Снова привет")
        self.assertEqual(provider.requests, [("Первый", None), ("Второй", "r1")])
        self.assertEqual(agent.previous_response_id, "r2")

    def test_model_calls_existing_calculator_then_answers(self):
        provider = FakeProvider([tool_reply(), ModelReply("r2", "Разница: 30000.00")])
        agent = FirstAgent(provider)
        self.assertEqual(agent.reply("Посчитай выручку и расходы"), "Разница: 30000.00")
        self.assertEqual(provider.requests[0], ("Посчитай выручку и расходы", None))
        outputs, previous_id = provider.requests[1]
        self.assertEqual(previous_id, "r1")
        self.assertEqual(outputs[0]["type"], "function_call_output")
        self.assertEqual(outputs[0]["call_id"], "c1")
        self.assertEqual(json.loads(outputs[0]["output"])["value"], "30000.00")
        self.assertEqual(agent.previous_response_id, "r2")

    def test_tool_context_is_used_on_next_user_turn(self):
        provider = FakeProvider([
            tool_reply(), ModelReply("r2", "Разница 30000"),
            ModelReply("r3", "В прошлом расчёте 30000"),
        ])
        agent = FirstAgent(provider)
        agent.reply("Посчитай")
        agent.reply("Повтори результат")
        self.assertEqual(provider.requests[2], ("Повтори результат", "r2"))

    def test_invalid_arguments_are_reported_without_execution(self):
        for arguments in ('{"revenue":"1"}', '{"revenue":"1","expenses":"0","extra":"x"}',
                          '{"revenue":1,"expenses":"0"}', '{"revenue":"-1","expenses":"0"}',
                          "{broken", "x" * 513, "\ud800"):
            with self.subTest(arguments=arguments[:40]):
                provider = FakeProvider([
                    tool_reply(arguments=arguments), ModelReply("r2", "Уточните суммы."),
                ])
                agent = FirstAgent(provider)
                self.assertEqual(agent.reply("Посчитай"), "Уточните суммы.")
                output = json.loads(provider.requests[1][0][0]["output"])
                self.assertFalse(output["ok"])
                self.assertIsNone(output["value"])

    def test_unknown_tool_does_not_run(self):
        allowed = Mock()
        allowed.name = "calculate_balance"
        provider = FakeProvider([
            tool_reply(name="delete_file"), ModelReply("r2", "Недоступно."),
        ])
        agent = FirstAgent(provider, CalculatorDispatcher(allowed))
        self.assertEqual(agent.reply("Удалить файл"), "Недоступно.")
        allowed.run.assert_not_called()
        self.assertFalse(json.loads(provider.requests[1][0][0]["output"])["ok"])

    def test_tool_loop_has_finite_budget_and_keeps_previous_context(self):
        provider = FakeProvider([
            tool_reply("r1", "c1"), tool_reply("r2", "c2"),
            tool_reply("r3", "c3"), tool_reply("r4", "c4"),
        ])
        agent = FirstAgent(provider)
        agent.previous_response_id = "last-good"
        self.assertEqual(agent.reply("Ещё"), LIMIT_ANSWER)
        self.assertEqual(agent.previous_response_id, "last-good")
        self.assertEqual(len(provider.requests), 4)

    def test_empty_response_and_api_error_preserve_context(self):
        agent = FirstAgent(FakeProvider([ModelReply("empty", "")]))
        agent.previous_response_id = "last-good"
        self.assertEqual(agent.reply("Первый"), EMPTY_ANSWER)
        self.assertEqual(agent.previous_response_id, "last-good")
        agent.provider = FakeProvider([OpenAIError("simulated failure")])
        with self.assertRaises(OpenAIError):
            agent.reply("Второй")
        self.assertEqual(agent.previous_response_id, "last-good")

    def test_incomplete_model_response_is_reported_safely(self):
        sdk = Mock()
        sdk.responses.create.return_value = SimpleNamespace(status="incomplete")
        with self.assertRaises(ModelResponseError):
            OpenAIProvider(client=sdk).generate("Вопрос", None)
    def test_multiple_calls_in_one_response_are_returned_together(self):
        first = ToolCall("c1", "calculate_balance", '{"revenue":"2","expenses":"1"}')
        second = ToolCall("c2", "calculate_balance", '{"revenue":"3","expenses":"1"}')
        provider = FakeProvider([ModelReply("r1", "", (first, second)), ModelReply("r2", "Готово")])
        self.assertEqual(FirstAgent(provider).reply("Два расчёта"), "Готово")
        outputs = provider.requests[1][0]
        self.assertEqual([x["call_id"] for x in outputs], ["c1", "c2"])
        self.assertEqual([json.loads(x["output"])["value"] for x in outputs], ["1.00", "2.00"])

    def test_openai_adapter_maps_function_call_and_strict_schema(self):
        sdk = Mock()
        sdk.responses.create.return_value = SimpleNamespace(
            id="r1", output_text="", status="completed",
            output=[SimpleNamespace(type="function_call", call_id="c1",
                                    name="calculate_balance", arguments='{"revenue":"2","expenses":"1"}')],
        )
        provider = OpenAIProvider(client=sdk)
        response = provider.generate("Посчитай", None)
        self.assertEqual(response.tool_calls[0].call_id, "c1")
        request = sdk.responses.create.call_args.kwargs
        self.assertEqual(request["tools"][0]["name"], "calculate_balance")
        self.assertTrue(request["tools"][0]["strict"])
        self.assertFalse(request["tools"][0]["parameters"]["additionalProperties"])
        self.assertFalse(request["parallel_tool_calls"])
        provider.generate([{"type": "function_call_output", "call_id": "c1", "output": "{}"}], "r1")
        self.assertEqual(sdk.responses.create.call_args.kwargs["previous_response_id"], "r1")
        self.assertIn("instructions", sdk.responses.create.call_args.kwargs)

    def test_reasoning_override_preserves_tools_and_safe_timing(self):
        sdk = Mock()
        sdk.responses.create.return_value = SimpleNamespace(
            id="r1", output_text="ok", status="completed", output=[])
        provider = OpenAIProvider(client=sdk, reasoning_effort="low")
        with self.assertLogs("agent_runtime", level="INFO") as logs:
            provider.generate("private-message", "r0")
        request = sdk.responses.create.call_args.kwargs
        self.assertEqual(request["reasoning"], {"effort": "low"})
        self.assertEqual(request["previous_response_id"], "r0")
        self.assertEqual(request["tools"][0]["name"], "calculate_balance")
        self.assertIn("duration_s=", str(logs.output))
        self.assertNotIn("private-message", str(logs.output))
        OpenAIProvider(client=sdk).generate("hello", None)
        self.assertNotIn("reasoning", sdk.responses.create.call_args.kwargs)

    def test_concise_responses_apply_only_when_enabled(self):
        sdk = Mock()
        sdk.responses.create.return_value = SimpleNamespace(
            id="r1", output_text="ok", status="completed", output=[])
        OpenAIProvider(client=sdk, concise_responses=True).generate("Вопрос", None)
        self.assertIn("В Telegram обычно отвечай кратко", sdk.responses.create.call_args.kwargs["instructions"])
        self.assertIn("дай полный ответ", sdk.responses.create.call_args.kwargs["instructions"])
        OpenAIProvider(client=sdk).generate("Вопрос", None)
        self.assertNotIn("В Telegram", sdk.responses.create.call_args.kwargs["instructions"])


class CliTests(unittest.TestCase):
    def setUp(self):
        for target in ("load_dotenv", "FirstAgent", "MemoryStore", "BusinessAgent",
                       "BusinessStore", "OpenAIBusinessExtractor"):
            mocked = patch.object(cli, target)
            setattr(self, target, mocked.start())
            self.addCleanup(mocked.stop)
        environment = patch.dict(os.environ, {"OPENAI_API_KEY": "test-placeholder"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        output = patch("sys.stdout", new_callable=io.StringIO)
        self.output = output.start()
        self.addCleanup(output.stop)

    def test_missing_key_does_not_create_client(self):
        os.environ.pop("OPENAI_API_KEY")
        cli.main()
        self.FirstAgent.assert_not_called()
        self.assertIn("OPENAI_API_KEY", self.output.getvalue())

    def test_empty_input_and_exit_do_not_call_model(self):
        with patch("builtins.input", side_effect=["  ", " EXIT "]):
            cli.main()
        self.BusinessAgent.return_value.reply.assert_not_called()

    def test_api_failure_allows_next_message(self):
        self.BusinessAgent.return_value.reply.side_effect = [
            OpenAIError("simulated failure"), "recovered"
        ]
        with patch("builtins.input", side_effect=["first", "second", "exit"]):
            cli.main()
        self.assertEqual(self.BusinessAgent.return_value.reply.call_count, 2)
        self.assertIn("recovered", self.output.getvalue())

    def test_incomplete_model_response_allows_next_message(self):
        self.BusinessAgent.return_value.reply.side_effect = [
            ModelResponseError("not complete"), "recovered"
        ]
        with patch("builtins.input", side_effect=["first", "second", "exit"]):
            cli.main()
        self.assertIn("recovered", self.output.getvalue())
        self.assertIn("Модель не завершила ответ", self.output.getvalue())
    def test_eof_and_interrupt_exit_cleanly(self):
        for exception in (EOFError, KeyboardInterrupt):
            with self.subTest(exception=exception):
                with patch("builtins.input", side_effect=exception):
                    cli.main()
        self.BusinessAgent.return_value.reply.assert_not_called()

    def test_cli_connects_and_closes_enabled_mcp_client(self):
        os.environ["MCP_ENABLED"] = "true"
        with patch.object(cli, "MCPToolClient") as mcp_client:
            with patch("builtins.input", return_value="exit"):
                cli.main()
        self.assertIs(self.FirstAgent.call_args.kwargs["mcp_tools"], mcp_client.return_value)
        mcp_client.return_value.close.assert_called_once()

    def test_public_tools_stay_disabled_when_mcp_is_off(self):
        os.environ["MCP_ENABLED"] = "false"
        os.environ["MCP_ALLOW_PUBLIC_NETWORK"] = "false"
        with patch("builtins.input", return_value="exit"):
            cli.main()
        self.assertIsNone(self.FirstAgent.call_args.kwargs["mcp_tools"])
        self.assertFalse(self.FirstAgent.call_args.kwargs["allow_public_network"])

    def test_cli_wraps_first_agent_with_session_scoped_business_agent(self):
        with patch("builtins.input", return_value="exit"):
            cli.main()
        self.assertIs(self.BusinessAgent.call_args.args[0], self.FirstAgent.return_value)
        self.assertEqual(self.BusinessStore.call_args.args[1], "cli:local")
        self.assertIs(self.BusinessAgent.call_args.args[1], self.BusinessStore.return_value)


if __name__ == "__main__":
    unittest.main()
