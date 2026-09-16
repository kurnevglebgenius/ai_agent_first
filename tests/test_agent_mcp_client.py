"""Агент использует постоянное MCP-подключение и прежнюю границу разрешений."""

import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.agent_runtime.agent import FirstAgent
from shared.agent_runtime.mcp_client import MCPConfig, MCPToolClient
from shared.agent_runtime.model_provider import ModelReply, ToolCall


class AgentMcpTests(unittest.TestCase):
    def test_settings_choose_only_known_read_tools(self):
        with patch.dict(os.environ, {"MCP_ENABLED": "true", "MCP_ALLOW_PUBLIC_NETWORK": "false",
                                     "MCP_TOOL_NAMES": "calculate_arithmetic,days_between"}, clear=True):
            config = MCPConfig.from_env()
            self.assertTrue(config.enabled)
            self.assertEqual(config.tool_names, ("calculate_arithmetic", "days_between"))
        with patch.dict(os.environ, {"MCP_ENABLED": "yes"}, clear=True):
            with self.assertRaises(ValueError):
                MCPConfig.from_env()
        with patch.dict(os.environ, {"MCP_ENABLED": "true", "MCP_TOOL_NAMES": "remember_fact"},
                        clear=True):
            with self.assertRaises(ValueError):
                MCPConfig.from_env()

    def test_real_client_reuses_connection_for_multiple_calls(self):
        config = MCPConfig(True, False, ("calculate_arithmetic", "days_between"))
        with MCPToolClient(config) as remote:
            self.assertEqual({schema["name"] for schema in remote.schemas()},
                             {"calculate_arithmetic", "days_between"})
            call = ToolCall("test", "calculate_arithmetic",
                            '{"operation":"add","left":"0.1","right":"0.2"}')
            self.assertEqual(json.loads(remote.run(call))["value"], "0.3")
            self.assertEqual(json.loads(remote.run(call))["value"], "0.3")
        self.assertFalse(remote._thread.is_alive())

    def test_agent_routes_selected_tool_over_mcp_without_duplicate_schema(self):
        config = MCPConfig(True, False, ("calculate_arithmetic",))
        with MCPToolClient(config) as remote:
            provider = Mock()
            provider.generate.side_effect = [
                ModelReply("r1", "", (ToolCall("c1", "calculate_arithmetic",
                                              '{"operation":"add","left":"2","right":"3"}'),)),
                ModelReply("r2", "Ответ: 5"),
            ]
            agent = FirstAgent(provider=provider, mcp_tools=remote, allow_public_network=False)
            agent.tools.local.run = Mock(side_effect=AssertionError("Локальный вызов запрещён"))
            names = [schema["name"] for schema in agent.tools.schemas()]
            self.assertEqual(len(names), len(set(names)))
            self.assertEqual(agent.reply("Сколько будет 2+3?"), "Ответ: 5")
            output = provider.generate.call_args.args[0][0]
            self.assertEqual(json.loads(output["output"])["value"], "5")
            agent.tools.local.run.assert_not_called()

    def test_bad_arguments_do_not_reach_mcp_server(self):
        config = MCPConfig(True, False, ("calculate_arithmetic",))
        with MCPToolClient(config) as remote:
            remote._request = Mock(side_effect=AssertionError("Вызова MCP не должно быть"))
            for raw in ('{"operation":"add","left":"1","left":"2","right":"3"}',
                        '{"operation":"add","left":1,"right":"3"}',
                        '{"operation":"add","left":"1"}'):
                with self.subTest(raw=raw):
                    result = json.loads(remote.run(ToolCall("bad", "calculate_arithmetic", raw)))
                    self.assertFalse(result["ok"])
            remote._request.assert_not_called()
