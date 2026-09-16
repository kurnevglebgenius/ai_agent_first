"""Проверки доступа, реального runtime и транспорта без внешних запросов."""

import io
import importlib.util
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "04_telegram_agent"))
sys.path.insert(0, str(ROOT))
from config import Config
from command_catalog import MENU_COMMANDS, START_TEXT, render_help, telegram_menu_commands
from telegram_adapter import HELP, TelegramAdapter
from telegram_api import TelegramAPI, TelegramError, connect_ipv4
import socket
from shared.agent_runtime.agent import FirstAgent
from shared.agent_runtime.model_provider import ModelReply, ToolCall
from shared.agent_runtime.mcp_client import MCPConfig


TELEGRAM_MAIN_FILE = ROOT / "04_telegram_agent" / "main.py"
telegram_main_spec = importlib.util.spec_from_file_location("lab_telegram_main", TELEGRAM_MAIN_FILE)
telegram_main = importlib.util.module_from_spec(telegram_main_spec)
telegram_main_spec.loader.exec_module(telegram_main)


def update(text="hello", user=123, chat_type="private", update_id=1):
    message = {"from": {"id": user}, "chat": {"id": user, "type": chat_type}}
    if text is not None:
        message["text"] = text
    else:
        message["photo"] = [{}]
        message["caption"] = "Do not treat captions as text"
    return {"update_id": update_id, "message": message}


def callback_update(data, user=123):
    return {"update_id": 2, "callback_query": {
        "id": "callback-1", "from": {"id": user}, "data": data,
        "message": {"message_id": 55, "chat": {"id": user, "type": "private"}},
    }}


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.provider = Mock()
        self.agent = FirstAgent(self.provider)
        self.adapter = TelegramAdapter(self.api, self.agent, 123)

    def test_text_uses_existing_runtime_and_real_tool(self):
        self.provider.generate.side_effect = [
            ModelReply("r1", "", (ToolCall("c1", "calculate_balance",
                        '{"revenue":"120000","expenses":"90000"}'),)),
            ModelReply("r2", "30000.00"),
        ]
        self.adapter.handle_update(update("Посчитай"))
        self.api.send_text.assert_called_once_with(123, "30000.00")
        self.api.send_chat_action.assert_called_once_with(123)
        outputs, previous_id = self.provider.generate.call_args.args
        self.assertEqual(json.loads(outputs[0]["output"])["value"], "30000.00")
        self.assertEqual(previous_id, "r1")

    def test_denied_users_and_groups_cannot_run_any_commands(self):
        self.agent.previous_response_id = "keep"
        for text in ("hello", "/start", "/help", "/status", "/reset", None):
            for item in (update(text, user=999), update(text, chat_type="group")):
                self.adapter.handle_update(item)
        self.provider.generate.assert_not_called()
        self.api.send_text.assert_not_called()
        self.assertEqual(self.agent.previous_response_id, "keep")

    def test_commands_unsupported_and_empty_do_not_call_llm(self):
        for text in ("/start", "/help", "/status", "/unknown", "  ", None):
            self.adapter.handle_update(update(text))
        self.assertEqual(self.api.send_text.call_count, 6)
        self.provider.generate.assert_not_called()

    def test_business_commands_are_routed_to_agent_and_listed_in_help(self):
        business_agent = Mock()
        business_agent.reply.side_effect = lambda text: f"handled:{text}"
        adapter = TelegramAdapter(self.api, business_agent, 123)
        commands = ("/draft", "/save", "/cancel", "/list 5", "/money",
                    "/delete " + "a" * 32, "/mail", "/mail_report 7",
                    "/mail_reply 7", "/records 5", "/email-status")
        for command in commands:
            adapter.handle_update(update(command))
        self.assertEqual([call.args[0] for call in business_agent.reply.call_args_list],
                         list(commands))
        self.assertEqual(self.api.send_text.call_count, len(commands))
        for command in ("/draft", "/save", "/cancel", "/list", "/money",
                        "/mail", "/mail_report", "/mail_reply"):
            self.assertIn(command, HELP)
        self.assertIn("/delete", render_help("/help учет"))

    def test_start_and_help_topics_are_short_and_human_readable(self):
        self.adapter.handle_update(update("/start"))
        self.api.send_text.assert_called_with(123, START_TEXT)
        self.api.reset_mock()
        self.adapter.handle_update(update("/help почта"))
        answer = self.api.send_text.call_args.args[1]
        self.assertIn("Деловая почта", answer)
        self.assertIn("/mail_reply", answer)
        self.assertNotIn("/email-reply", answer)

    def test_email_reply_buttons_confirm_once_and_reject_foreign_callback(self):
        business_agent = Mock()
        business_agent.reply.return_value = "Предлагаемый ответ"
        business_agent.email_service.pending_reply.token = "safe-token-123456"
        business_agent.email_decision.return_value = "Письмо отправлено."
        adapter = TelegramAdapter(self.api, business_agent, 123)
        adapter.handle_update(update("/mail_reply 7"))
        markup = self.api.send_text.call_args.kwargs["reply_markup"]
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"],
                         "email:send:safe-token-123456")

        self.api.reset_mock()
        adapter.handle_update(callback_update("email:send:safe-token-123456"))
        business_agent.email_decision.assert_called_once_with("safe-token-123456", True)
        self.api.answer_callback.assert_called_once_with("callback-1")
        self.api.clear_reply_buttons.assert_called_once_with(123, 55)
        self.api.send_text.assert_called_once_with(123, "Письмо отправлено.")

        self.api.reset_mock()
        business_agent.email_decision.reset_mock()
        adapter.handle_update(callback_update("email:send:safe-token-123456", user=999))
        business_agent.email_decision.assert_not_called()
        self.api.send_text.assert_not_called()

    def test_reset_starts_new_context(self):
        self.provider.generate.side_effect = [ModelReply("r1", "one"), ModelReply("r2", "two")]
        self.adapter.handle_update(update("first"))
        self.adapter.handle_update(update("/reset"))
        self.adapter.handle_update(update("second"))
        self.assertEqual(self.provider.generate.call_args.args, ("second", None))

    def test_failure_is_safe_and_next_message_works(self):
        self.agent.previous_response_id = "keep"
        self.provider.generate.side_effect = [RuntimeError("secret-token"), ModelReply("r2", "ok")]
        with self.assertLogs("telegram_agent") as logs:
            self.adapter.handle_update(update("private message"))
        self.assertEqual(self.agent.previous_response_id, "keep")
        self.assertNotIn("secret-token", str(logs.output) + str(self.api.send_text.call_args))
        self.assertNotIn("private message", str(logs.output))
        self.adapter.handle_update(update("next"))
        self.api.send_text.assert_called_with(123, "ok")

    def test_poll_retry_offsets_and_no_regeneration_after_delivery_error(self):
        self.api.get_updates.side_effect = [TelegramError(), [update()], [update()], KeyboardInterrupt()]
        self.api.send_text.side_effect = TelegramError(403)
        self.provider.generate.return_value = ModelReply("r1", "ok")
        with patch("telegram_adapter.time.sleep") as sleep:
            with self.assertRaises(KeyboardInterrupt):
                self.adapter.run()
        sleep.assert_called_once_with(5)
        self.api.set_my_commands.assert_called_once_with(telegram_menu_commands())
        self.assertEqual(self.adapter.offset, 2)
        self.provider.generate.assert_called_once()
        self.assertEqual([c.args[0] for c in self.api.get_updates.call_args_list], [0, 0, 2, 2])

    def test_fatal_poll_errors_stop(self):
        for code in (401, 404, 409):
            self.api.get_updates.side_effect = TelegramError(code)
            with self.assertRaises(TelegramError):
                self.adapter.run()


class TransportTests(unittest.TestCase):
    def test_payload_plain_text_and_long_emoji_response(self):
        api = TelegramAPI("123:test-placeholder")
        api.call = Mock()
        text = "😀" * 5000 + "<hello>"
        api.send_text(123, text)
        chunks = [c.kwargs["text"] for c in api.call.call_args_list]
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(c.encode("utf-16-le")) // 2 <= 4096 for c in chunks))
        self.assertTrue(all("parse_mode" not in c.kwargs for c in api.call.call_args_list))

    def test_send_429_retries_but_network_failure_does_not(self):
        api = TelegramAPI("123:test-placeholder")
        api.call = Mock(side_effect=[TelegramError(429, 2), True])
        with patch("telegram_api.time.sleep") as sleep:
            api.send_text(123, "hello")
        sleep.assert_called_once_with(2)
        api.call = Mock(side_effect=TelegramError())
        with self.assertRaises(TelegramError):
            api.send_text(123, "hello")
        api.call.assert_called_once()

    def test_http_payload_and_response(self):
        api = TelegramAPI("123:test-placeholder")
        with patch.object(api._opener, "open") as opening:
            opening.return_value.__enter__.return_value = io.BytesIO(b'{"ok":true,"result":[]}')
            self.assertEqual(api.get_updates(9), [])
        request = opening.call_args.args[0]
        self.assertEqual(json.loads(request.data), {
            "offset": 9, "timeout": 25,
            "allowed_updates": ["message", "callback_query"],
        })
        self.assertEqual(request.method, "POST")

    def test_typing_action_uses_telegram_method(self):
        api = TelegramAPI("123:test-placeholder")
        api.call = Mock(return_value=True)
        api.send_chat_action(123)
        api.call.assert_called_once_with("sendChatAction", chat_id=123, action="typing")

    def test_command_menu_payload_uses_valid_telegram_names(self):
        payload = telegram_menu_commands()
        self.assertEqual(len(payload), len(MENU_COMMANDS))
        self.assertEqual(len({item["command"] for item in payload}), len(payload))
        for item in payload:
            self.assertRegex(item["command"], r"^[a-z0-9_]{1,32}$")
            self.assertTrue(1 <= len(item["description"]) <= 256)
        api = TelegramAPI("123:test-placeholder")
        api.call = Mock(return_value=True)
        api.set_my_commands(payload)
        api.call.assert_called_once_with("setMyCommands", commands=payload)

    def test_http_errors_hide_secrets_and_parse_retry(self):
        api = TelegramAPI("123:test-placeholder")
        error = HTTPError("https://secret-token", 429, "secret-token", {},
                          io.BytesIO(b'{"parameters":{"retry_after":3}}'))
        with patch.object(api._opener, "open", side_effect=error):
            with self.assertRaises(TelegramError) as caught:
                api.get_updates(0)
        self.assertEqual(caught.exception.retry_after, 3)
        self.assertNotIn("secret-token", str(caught.exception))
        with patch.object(api._opener, "open", side_effect=URLError("secret-token")):
            with self.assertRaises(TelegramError):
                api.get_updates(0)

    def test_ipv4_transport_tries_next_address_without_global_dns_changes(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
                     for ip in ("192.0.2.1", "192.0.2.2")]
        connection = Mock()
        with patch("telegram_api.socket.getaddrinfo", return_value=addresses) as resolve:
            with patch("telegram_api.socket.create_connection",
                       side_effect=[OSError(), connection]) as connect:
                self.assertIs(connect_ipv4(("api.telegram.org", 443), 4), connection)
        resolve.assert_called_once_with("api.telegram.org", 443, socket.AF_INET, socket.SOCK_STREAM)
        self.assertEqual(connect.call_count, 2)


class ConfigTests(unittest.TestCase):
    def test_fail_closed(self):
        for values in ({}, {"TELEGRAM_BOT_TOKEN": "123:placeholder"},
                       {"TELEGRAM_BOT_TOKEN": "123:placeholder", "TELEGRAM_ALLOWED_USER_ID": "-1"}):
            with patch.dict(os.environ, values, clear=True):
                with self.assertRaises(ValueError):
                    Config.from_env()

    def test_valid_config_hides_token(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:placeholder",
                                   "TELEGRAM_ALLOWED_USER_ID": "123", "OPENAI_API_KEY": "placeholder"}, clear=True):
            config = Config.from_env()
        self.assertEqual(config.allowed_user_id, 123)
        self.assertEqual(config.reasoning_effort, "low")
        self.assertNotIn("placeholder", repr(config))

    def test_reasoning_setting_is_validated(self):
        values = {"TELEGRAM_BOT_TOKEN": "123:placeholder", "TELEGRAM_ALLOWED_USER_ID": "123",
                  "OPENAI_API_KEY": "placeholder", "OPENAI_REASONING_EFFORT": "high"}
        with patch.dict(os.environ, values, clear=True):
            self.assertEqual(Config.from_env().reasoning_effort, "high")
            os.environ["OPENAI_REASONING_EFFORT"] = "invalid"
            with self.assertRaises(ValueError):
                Config.from_env()


class McpStartupTests(unittest.TestCase):
    def test_telegram_passes_mcp_client_to_agent_and_closes_it(self):
        with ExitStack() as stack:
            mocks = {name: stack.enter_context(patch.object(telegram_main, name))
                     for name in ("load_dotenv", "OpenAI", "DefaultHttpxClient", "HTTPTransport",
                                  "MemoryStore", "MCPToolClient", "FirstAgent", "TelegramAdapter",
                                  "TelegramAPI", "BusinessAgent", "BusinessStore",
                                  "OpenAIBusinessExtractor")}
            stack.enter_context(patch.object(telegram_main.Config, "from_env",
                                             return_value=Config("123:placeholder", 123, "low")))
            stack.enter_context(patch.object(telegram_main.MCPConfig, "from_env",
                                             return_value=MCPConfig(True, False,
                                                                    ("calculate_arithmetic",))))
            self.assertIsNone(telegram_main.main())
            remote = mocks["MCPToolClient"].return_value.__enter__.return_value
            kwargs = mocks["FirstAgent"].call_args.kwargs
            self.assertIs(kwargs["mcp_tools"], remote)
            self.assertFalse(kwargs["allow_public_network"])
            self.assertTrue(kwargs["provider"].concise_responses)
            self.assertEqual(mocks["BusinessStore"].call_args.args[1], "telegram:123")
            self.assertIs(mocks["TelegramAdapter"].call_args.args[1],
                          mocks["BusinessAgent"].return_value)
            mocks["TelegramAdapter"].return_value.run.assert_called_once()
            mocks["MCPToolClient"].return_value.__exit__.assert_called_once()

    def test_telegram_keeps_public_tools_disabled_without_mcp(self):
        with ExitStack() as stack:
            mocks = {name: stack.enter_context(patch.object(telegram_main, name))
                     for name in ("load_dotenv", "OpenAI", "DefaultHttpxClient", "HTTPTransport",
                                  "MemoryStore", "MCPToolClient", "FirstAgent", "TelegramAdapter",
                                  "TelegramAPI", "BusinessAgent", "BusinessStore",
                                  "OpenAIBusinessExtractor")}
            stack.enter_context(patch.object(telegram_main.Config, "from_env",
                                             return_value=Config("123:placeholder", 123, "low")))
            stack.enter_context(patch.object(telegram_main.MCPConfig, "from_env",
                                             return_value=MCPConfig(False, False,
                                                                    ("calculate_arithmetic",))))
            self.assertIsNone(telegram_main.main())
            kwargs = mocks["FirstAgent"].call_args.kwargs
            self.assertIsNone(kwargs["mcp_tools"])
            self.assertFalse(kwargs["allow_public_network"])
            mocks["MCPToolClient"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
