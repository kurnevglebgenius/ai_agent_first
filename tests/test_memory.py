"""Проверки памяти без сети и ключей, только во временной базе."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.agent_runtime.memory import MemoryStore
from shared.agent_runtime.agent import FirstAgent, EMPTY_ANSWER
from shared.agent_runtime.model_provider import ModelReply, ToolCall


class MemoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'memory.sqlite3'
        self.memory = MemoryStore(self.path, 'alice')
        self.provider = Mock()
        self.agent = FirstAgent(self.provider, memory=self.memory)

    def test_restart_restores_history_and_facts_without_response_id(self):
        self.agent.reply('/remember валюта = BYN')
        self.provider.generate.return_value = ModelReply('r1', 'Первый ответ')
        self.agent.reply('Первый вопрос')
        restarted = FirstAgent(self.provider, memory=MemoryStore(self.path, 'alice'))
        restarted.reply('Продолжим')
        messages, previous = self.provider.generate.call_args.args
        self.assertIsNone(previous)
        self.assertIn('BYN', messages[0]['content'])
        self.assertEqual(messages[1]['content'], 'Первый вопрос')
        self.assertEqual(messages[2]['content'], 'Первый ответ')

    def test_user_isolation_and_parameterized_values(self):
        self.memory.remember("x'; DROP TABLE facts;--", 'данные')
        self.memory.save_turn('private', 'answer')
        other = MemoryStore(self.path, 'bob')
        self.assertEqual(other.context(), [])
        other.reset()
        self.assertEqual(len(self.memory.facts()), 1)
        self.assertEqual(len(self.memory.history()), 2)

    def test_reset_preserves_facts_forget_clears_stale_dialogue(self):
        self.agent.reply('/remember имя = Глеб')
        self.memory.save_turn('имя?', 'Глеб')
        self.agent.reset()
        self.assertEqual(self.memory.history(), [])
        self.assertEqual(self.memory.facts(), {'имя': 'Глеб'})
        self.memory.save_turn('имя?', 'Глеб')
        self.agent.reply('/forget имя')
        self.assertEqual(self.memory.context(), [])
        self.provider.generate.assert_not_called()

    def test_history_is_bounded_and_ordered(self):
        for i in range(15):
            self.memory.save_turn(str(i), 'x' * 13000)
        history = self.memory.history()
        self.assertEqual(len(history), 20)
        self.assertEqual(history[0]['content'], '5')
        self.assertEqual(history[-2]['content'], '14')
        self.assertEqual(len(history[-1]['content']), 12000)

    def test_commands_validate_update_and_limit_facts(self):
        self.assertIn('Используйте', self.agent.reply('/remember bad'))
        self.agent.reply('/remember x = old')
        self.agent.reply('/remember x = new')
        self.assertEqual(self.memory.facts(), {'x': 'new'})
        for i in range(29):
            self.memory.remember(str(i), 'value')
        with self.assertRaises(ValueError):
            self.memory.remember('overflow', 'value')
        self.memory.remember('x', 'updated')
        for name, value in [('', 'x'), ('x', ''), ('x', 'a' * 1001)]:
            with self.assertRaises(ValueError):
                self.memory.remember(name, value)
        self.provider.generate.assert_not_called()

    def test_failed_or_empty_response_does_not_save_partial_turn(self):
        self.provider.generate.return_value = ModelReply('r1', '')
        self.assertEqual(self.agent.reply('question'), EMPTY_ANSWER)
        self.provider.generate.side_effect = RuntimeError('failure')
        with self.assertRaises(RuntimeError):
            self.agent.reply('question')
        self.assertEqual(self.memory.history(), [])

    def test_tool_loop_keeps_call_id_and_saves_only_final_answer(self):
        self.provider.generate.side_effect = [
            ModelReply('r1', '', (ToolCall('c1', 'calculate_balance', '{"revenue":"10","expenses":"3"}'),)),
            ModelReply('r2', '7.00')]
        self.assertEqual(self.agent.reply('Посчитай'), '7.00')
        data, previous = self.provider.generate.call_args.args
        self.assertEqual(previous, 'r1')
        self.assertEqual(data[0]['call_id'], 'c1')
        self.assertEqual(self.memory.history()[-1]['content'], '7.00')

    def test_telegram_access_and_memory_commands(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '04_telegram_agent'))
        from telegram_adapter import TelegramAdapter
        api = Mock()
        adapter = TelegramAdapter(api, self.agent, 123)
        def update(user, text):
            return {'message': {'from': {'id': user}, 'chat': {'id': user, 'type': 'private'}, 'text': text}}
        adapter.handle_update(update(999, '/remember name = intruder'))
        self.assertEqual(self.memory.facts(), {})
        api.send_text.assert_not_called()
        adapter.handle_update(update(123, '/remember name = owner'))
        adapter.handle_update(update(123, '/memory'))
        api.send_text.assert_called_with(123, 'name = owner')
        self.provider.generate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
