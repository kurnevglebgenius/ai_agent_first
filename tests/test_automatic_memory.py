"""Automatic memory tool validation and agent integration."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.agent_runtime.agent import FirstAgent
from shared.agent_runtime.memory import MemoryStore
from shared.agent_runtime.memory_tool import remember_fact
from shared.agent_runtime.model_provider import ModelReply, ToolCall, OpenAIProvider


class AutomaticMemoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.memory = MemoryStore(Path(temp.name) / 'memory.sqlite3', 'test')

    def arguments(self, **overrides):
        return json.dumps(dict(name='имя', value='Глеб', evidence='Меня зовут Глеб', **overrides))

    def test_model_tool_saves_and_updates_fact(self):
        provider = Mock()
        agent = FirstAgent(provider, memory=self.memory)
        for name in ('Глеб', 'Иван'):
            message = f'Меня зовут {name}'
            arguments = json.dumps({'name': 'имя', 'value': name, 'evidence': message})
            provider.generate.side_effect = [ModelReply('r1', '', (ToolCall('c1', 'remember_fact', arguments),)), ModelReply('r2', 'Запомнил')]
            agent.reply(message)
            self.assertEqual(self.memory.facts(), {'имя': name})
            output = provider.generate.call_args.args[0][0]
            self.assertEqual(output['call_id'], 'c1')
            self.assertTrue(json.loads(output['output'])['ok'])

    def test_untrusted_arguments_do_not_write(self):
        for args in ('{broken', '[]', '{}', '"string"', '\ud800', 'x' * 16001,
                     json.dumps({'name': 1, 'value': 'Глеб', 'evidence': 'Глеб'}),
                     json.dumps({'name': 'имя', 'value': 'Иван', 'evidence': 'Меня зовут Глеб'}),
                     self.arguments()):
            with self.subTest(args=repr(args[:50])):
                self.assertFalse(json.loads(remember_fact(self.memory, args, 'Текущий вопрос'))['ok'])
        self.assertEqual(self.memory.facts(), {})

    def test_missing_store_and_full_store_report_failure(self):
        args = self.arguments()
        self.assertFalse(json.loads(remember_fact(None, args, 'Меня зовут Глеб'))['ok'])
        for i in range(30):
            self.memory.remember(str(i), 'value')
        self.assertFalse(json.loads(remember_fact(self.memory, args, 'Меня зовут Глеб'))['ok'])
        self.assertEqual(len(self.memory.facts()), 30)

    def test_provider_only_advertises_memory_when_enabled(self):
        sdk = Mock()
        sdk.responses.create.return_value = SimpleNamespace(id='r1', status='completed', output=[], output_text='ok')
        for enabled in (False, True):
            OpenAIProvider(client=sdk, memory_enabled=enabled).generate('hello', None)
            names = [tool['name'] for tool in sdk.responses.create.call_args.kwargs['tools']]
            self.assertEqual('remember_fact' in names, enabled)

    def test_saved_fact_survives_failure_of_final_model_response(self):
        provider = Mock()
        provider.generate.side_effect = [ModelReply('r1', '', (ToolCall('c1', 'remember_fact', self.arguments()),)), RuntimeError('network')]
        with self.assertRaises(RuntimeError):
            FirstAgent(provider, memory=self.memory).reply('Меня зовут Глеб')
        self.assertEqual(self.memory.facts(), {'имя': 'Глеб'})
        self.assertEqual(self.memory.history(), [])


if __name__ == '__main__':
    unittest.main()
