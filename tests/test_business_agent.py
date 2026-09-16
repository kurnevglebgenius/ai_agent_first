"""Offline-проверки Phase 8: домен, диалог, SQLite и extractor."""

from datetime import date, datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from shared.agent_runtime.business import (
    BUSINESS_FIELDS,
    BUSINESS_COMMANDS,
    BusinessAgent,
    BusinessStore,
    BusinessValidationError,
    OpenAIBusinessExtractor,
    SAFE_STORAGE_ERROR,
    canonical_business_command,
    normalize_patch,
    route_business_message,
)
from shared.agent_runtime.model_provider import ModelResponseError


TODAY = date(2026, 9, 15)
NOW = datetime(2026, 9, 15, 12, 34, 56, tzinfo=timezone.utc)
FULL_INCOME = {
    "kind": "income",
    "amount": "00120000.50",
    "currency": "byn",
    "occurred_on": "сегодня",
    "category": "дизайн",
    "counterparty": "Альфа",
    "note": "лендинг",
}
FULL_EXPENSE = {
    "kind": "expense",
    "amount": "450",
    "currency": "BYN",
    "occurred_on": "вчера",
    "category": "реклама",
}


class FakeBaseAgent:
    def __init__(self):
        self.has_context = False
        self.replies = []
        self.reset_calls = 0

    def reply(self, message):
        self.replies.append(message)
        return "обычный ответ"

    def reset(self):
        self.reset_calls += 1

    def memory_command(self, message):
        return f"memory:{message}" if message.strip().lower() == "/memory" else None


class FakeExtractor:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def extract(self, message, *, today, draft):
        self.calls.append((message, today, draft))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


class BusinessAgentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "business.sqlite3"
        self.counter = 0

    def make_store(self, session="alice"):
        def next_id():
            self.counter += 1
            return f"{self.counter:032x}"
        return BusinessStore(self.path, session, id_factory=next_id, clock=lambda: NOW)

    def make_agent(self, *responses, session="alice"):
        base = FakeBaseAgent()
        extractor = FakeExtractor(*responses)
        store = self.make_store(session)
        agent = BusinessAgent(base, store, extractor, today=lambda: TODAY)
        return agent, store, extractor, base

    def test_complete_income_is_normalized_and_saved_only_after_confirmation(self):
        agent, store, extractor, _ = self.make_agent(FULL_INCOME)
        answer = agent.reply("Запиши доход 120000.50 BYN сегодня")
        self.assertIn("Черновик готов", answer)
        self.assertIn("Сумма: 120000.5", answer)
        self.assertIn("Валюта: BYN", answer)
        self.assertIn("Дата: 2026-09-15", answer)
        self.assertEqual(store.list_operations(), [])

        saved = agent.reply("/save")
        self.assertIn("Операция сохранена", saved)
        self.assertNotIn("None", saved)
        records = store.list_operations()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].amount, "120000.5")
        self.assertEqual(records[0].created_at, "2026-09-15T12:34:56Z")
        self.assertEqual(len(extractor.calls), 1)

    def test_complete_expense_resolves_yesterday_from_injected_date(self):
        agent, store, _, _ = self.make_agent(FULL_EXPENSE)
        self.assertIn("2026-09-14", agent.reply("Вчера потратил 450 BYN на рекламу"))
        agent.reply("/save")
        item = store.list_operations()[0]
        self.assertEqual((item.kind, item.occurred_on), ("expense", "2026-09-14"))

    def test_each_required_field_is_requested_when_missing(self):
        replacements = {
            "kind": "income",
            "amount": "10.00",
            "currency": "usd",
            "occurred_on": "2026-09-01",
        }
        labels = {"kind": "тип", "amount": "сумма", "currency": "валюта", "occurred_on": "дата"}
        complete = {"kind": "expense", "amount": "3", "currency": "EUR",
                    "occurred_on": "2026-08-31"}
        for missing, replacement in replacements.items():
            with self.subTest(missing=missing):
                separate = Path(self.path.parent) / f"{missing}.sqlite3"
                store = BusinessStore(separate, "alice")
                extractor = FakeExtractor(
                    {name: value for name, value in complete.items() if name != missing},
                    {missing: replacement},
                )
                agent = BusinessAgent(FakeBaseAgent(), store, extractor, today=lambda: TODAY)
                self.assertIn(f"Не хватает: {labels[missing]}", agent.reply("начало"))
                self.assertIn("Черновик готов", agent.reply("дополнение"))

    def test_sequential_answers_extend_same_persistent_draft(self):
        agent, store, extractor, _ = self.make_agent(
            {"kind": "expense"}, {"amount": "25.00"}, {"currency": "eur"},
            {"occurred_on": "вчера"},
        )
        answers = [agent.reply(text) for text in ("расход", "25", "EUR", "вчера")]
        self.assertIn("сумма, валюта, дата", answers[0])
        self.assertIn("валюта, дата", answers[1])
        self.assertIn("Не хватает: дата", answers[2])
        self.assertIn("Черновик готов", answers[3])
        self.assertEqual(extractor.calls[1][2], {"kind": "expense"})
        self.assertEqual(store.get_draft().id, "00000000000000000000000000000001")

    def test_invalid_amounts_never_create_a_draft(self):
        invalid = ("NaN", "Infinity", "1e3", "0", "-1", "1.234", "1234567890123", "1,20", "")
        for amount in invalid:
            with self.subTest(amount=amount):
                agent, store, _, _ = self.make_agent({"kind": "income", "amount": amount})
                self.assertIn("Не удалось обновить черновик", agent.reply("операция"))
                self.assertIsNone(store.get_draft())

    def test_invalid_date_currency_text_and_extra_fields_are_rejected(self):
        invalid = (
            {"currency": "РУБ"}, {"currency": "US"}, {"occurred_on": "2026-02-30"},
            {"occurred_on": "15.09.2026"}, {"category": "x" * 81},
            {"counterparty": "x" * 121}, {"note": "x" * 501}, {"note": "   "},
            {"session": "mallory"}, {"id": "chosen-by-model"},
        )
        for values in invalid:
            with self.subTest(values=list(values)):
                with self.assertRaises(BusinessValidationError):
                    normalize_patch(values, TODAY)

    def test_cancel_and_repeated_save_do_not_create_operations(self):
        agent, store, _, _ = self.make_agent(FULL_INCOME)
        agent.reply("доход")
        self.assertIn("отменён", agent.reply("/cancel"))
        self.assertEqual(store.list_operations(), [])

        second = FakeExtractor(FULL_EXPENSE)
        agent.extractor = second
        agent.reply("расход")
        first_save = agent.reply("/save")
        second_save = agent.reply("/save")
        self.assertIn("сохранена", first_save)
        self.assertIn("нет", second_save)
        self.assertEqual(len(store.list_operations()), 1)

    def test_incomplete_save_is_refused(self):
        agent, store, _, _ = self.make_agent({"amount": "10"})
        agent.reply("10")
        self.assertIn("Сохранение невозможно", agent.reply("/save"))
        self.assertEqual(store.list_operations(), [])
        self.assertIsNotNone(store.get_draft())

    def test_restart_quotes_and_sql_like_text_are_stored_exactly(self):
        payload = "Клиент O'Brien'; DROP TABLE business_operations;--"
        values = dict(FULL_INCOME, counterparty=payload, note='Счёт "A-1"')
        agent, store, _, _ = self.make_agent(values)
        agent.reply("операция с кавычками")
        agent.reply("/save")

        reopened = BusinessStore(self.path, "alice")
        item = reopened.list_operations()[0]
        self.assertEqual(item.counterparty, payload)
        self.assertEqual(item.note, 'Счёт "A-1"')
        self.assertEqual(len(store.list_operations()), 1)

    def test_sessions_cannot_list_or_delete_each_others_records_or_drafts(self):
        alice, alice_store, _, _ = self.make_agent(FULL_INCOME, session="alice")
        alice.reply("доход")
        alice.reply("/save")
        operation_id = alice_store.list_operations()[0].id

        bob_store = self.make_store("bob")
        bob = BusinessAgent(FakeBaseAgent(), bob_store, FakeExtractor({"amount": "5"}),
                            today=lambda: TODAY)
        self.assertEqual(bob.reply("/records"), "Сохранённых операций нет.")
        self.assertIn("не найдена", bob.reply(f"/delete {operation_id}"))
        bob.reply("5")
        self.assertIsNotNone(bob_store.get_draft())
        self.assertIsNone(alice_store.get_draft())
        self.assertEqual(len(alice_store.list_operations()), 1)

    def test_records_and_delete_are_deterministic_without_extractor_or_llm(self):
        agent, store, extractor, base = self.make_agent(FULL_INCOME)
        agent.reply("доход")
        agent.reply("/save")
        operation_id = store.list_operations()[0].id
        self.assertIn(operation_id, agent.reply("/records 1"))
        self.assertIn("удалена", agent.reply(f"/delete {operation_id}"))
        self.assertEqual(agent.reply("/records"), "Сохранённых операций нет.")
        self.assertEqual(len(extractor.calls), 1)
        self.assertEqual(base.replies, [])

    def test_human_command_aliases_keep_legacy_commands_compatible(self):
        expected = {
            "/list": "/records",
            "/money": "/categories",
            "/summary": "/report",
            "/trash": "/deleted",
            "/restore_record": "/restore",
            "/backup_list": "/backups",
            "/backup_restore": "/restore-backup",
            "/mail": "/email-status",
            "/mail_report": "/email-report",
            "/mail_reply": "/email-reply",
        }
        for alias, canonical in expected.items():
            with self.subTest(alias=alias):
                self.assertIn(alias, BUSINESS_COMMANDS)
                self.assertEqual(canonical_business_command(alias), canonical)
                self.assertEqual(canonical_business_command(canonical), canonical)

        agent, _, _, _ = self.make_agent()
        self.assertEqual(agent.reply("/list"), "Сохранённых операций нет.")
        self.assertEqual(agent.reply("/money"),
                         "Сохранённых операций для разбивки по категориям нет.")

    def test_categories_separate_income_and_expense_with_exact_currency_totals(self):
        operations = (
            dict(FULL_EXPENSE, amount="20", currency="RUB", category="Реклама"),
            dict(FULL_EXPENSE, amount="5.50", currency="RUB", category="реклама"),
            dict(FULL_EXPENSE, amount="100", currency="BYN", category="аренда"),
            dict(FULL_INCOME, amount="200", currency="RUB", category="Дизайн"),
        )
        agent, store, extractor, base = self.make_agent(*operations)
        for index in range(len(operations)):
            agent.reply(f"операция {index}")
            agent.reply("/save")
        answer = agent.reply("/categories")
        self.assertIn("Расходы:\n", answer)
        self.assertIn("- реклама: 25.5 RUB", answer)
        self.assertIn("- аренда: 100 BYN", answer)
        self.assertIn("Доходы:\n- дизайн: 200 RUB", answer)
        self.assertEqual(len(extractor.calls), 4)
        self.assertEqual(base.replies, [])
        self.assertEqual(store.list_operations()[0].category, "дизайн")

    def test_plain_question_delegates_and_does_not_change_business_state(self):
        agent, store, extractor, base = self.make_agent(None)
        self.assertEqual(agent.reply("Как посчитать маржу?"), "обычный ответ")
        self.assertEqual(base.replies, ["Как посчитать маржу?"])
        self.assertIsNone(store.get_draft())
        self.assertEqual(store.list_operations(), [])
        self.assertEqual(len(extractor.calls), 1)

    def test_fast_router_skips_business_extractors_for_ordinary_chat(self):
        agent, store, extractor, base = self.make_agent()
        agent.message_router = route_business_message
        self.assertEqual(agent.reply("Привет! Как твои дела?"), "обычный ответ")
        self.assertEqual(base.replies, ["Привет! Как твои дела?"])
        self.assertEqual(extractor.calls, [])
        self.assertIsNone(store.get_draft())

    def test_fast_router_keeps_operations_queries_and_draft_followups(self):
        cases = {
            "Мы потратили 20 рублей на рекламу": "operation",
            "Запиши доход 5000 BYN от клиента Альфа": "operation",
            "Сколько мы потратили за месяц?": "query",
            "Покажи прибыль проекта Альфа": "query",
            "Объясни, как посчитать маржу": "chat",
            "Какая сегодня погода?": "chat",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(route_business_message(message), expected)
        self.assertEqual(route_business_message("20", has_draft=True), "operation")

    def test_memory_command_bypasses_extractor_even_with_active_draft(self):
        agent, _, extractor, _ = self.make_agent({"amount": "10"})
        agent.reply("Запиши 10")
        self.assertEqual(agent.reply("/memory"), "memory:/memory")
        self.assertEqual(len(extractor.calls), 1)

    def test_reset_removes_draft_but_preserves_saved_operations(self):
        agent, store, _, base = self.make_agent(FULL_INCOME)
        agent.reply("доход")
        agent.reply("/save")
        agent.extractor = FakeExtractor({"amount": "2"})
        agent.reply("ещё")
        agent.reset()
        self.assertEqual(base.reset_calls, 1)
        self.assertIsNone(store.get_draft())
        self.assertEqual(len(store.list_operations()), 1)

    def test_sqlite_error_is_safe_and_does_not_reach_extractor_or_base_agent(self):
        agent, store, extractor, base = self.make_agent(FULL_INCOME)
        secret = "C:\\private\\business.sqlite3 SELECT * secret"
        with patch.object(store, "get_draft", side_effect=sqlite3.OperationalError(secret)):
            answer = agent.reply("частные данные")
        self.assertEqual(answer, SAFE_STORAGE_ERROR)
        self.assertNotIn("private", answer)
        self.assertNotIn("SELECT", answer)
        self.assertEqual(extractor.calls, [])
        self.assertEqual(base.replies, [])


class OpenAIExtractorTests(unittest.TestCase):
    @staticmethod
    def arguments(**changes):
        data = {"intent": "record_operation", **{name: None for name in BUSINESS_FIELDS}}
        data.update(changes)
        return json.dumps(data, ensure_ascii=False)

    def test_adapter_forces_strict_single_extraction_and_passes_explicit_date(self):
        sdk = Mock()
        sdk.responses.create.return_value = SimpleNamespace(
            status="completed",
            output=[SimpleNamespace(
                type="function_call", name="extract_business_operation",
                arguments=self.arguments(kind="expense", amount="10", occurred_on="вчера"),
            )],
        )
        extractor = OpenAIBusinessExtractor(sdk, reasoning_effort="low")
        result = extractor.extract("Вчера потратил 10", today=TODAY, draft={"currency": "BYN"})
        self.assertEqual(result, {"kind": "expense", "amount": "10", "occurred_on": "вчера"})
        request = sdk.responses.create.call_args.kwargs
        self.assertFalse(request["parallel_tool_calls"])
        self.assertEqual(request["tool_choice"]["name"], "extract_business_operation")
        self.assertTrue(request["tools"][0]["strict"])
        self.assertFalse(request["tools"][0]["parameters"]["additionalProperties"])
        self.assertEqual(request["reasoning"], {"effort": "low"})
        self.assertIn("потратили на рекламу", request["instructions"])
        sent = json.loads(request["input"])
        self.assertEqual(sent["application_date"], "2026-09-15")
        self.assertEqual(sent["active_draft"], {"currency": "BYN"})

    def test_other_intent_returns_none(self):
        sdk = Mock()
        sdk.responses.create.return_value = SimpleNamespace(
            status="completed",
            output=[SimpleNamespace(type="function_call", name="extract_business_operation",
                                    arguments=self.arguments(intent="other"))],
        )
        self.assertIsNone(OpenAIBusinessExtractor(sdk).extract(
            "Привет", today=TODAY, draft=None
        ))

    def test_malformed_model_proposals_are_rejected_safely(self):
        bad_arguments = (
            "{broken",
            '{"intent":"other","intent":"record_operation"}',
            json.dumps({"intent": "record_operation", "kind": 1}),
            "x" * 16_001,
        )
        for arguments in bad_arguments:
            with self.subTest(size=len(arguments)):
                sdk = Mock()
                sdk.responses.create.return_value = SimpleNamespace(
                    status="completed",
                    output=[SimpleNamespace(type="function_call",
                                            name="extract_business_operation",
                                            arguments=arguments)],
                )
                with self.assertRaises(ModelResponseError):
                    OpenAIBusinessExtractor(sdk).extract("x", today=TODAY, draft=None)


if __name__ == "__main__":
    unittest.main()
