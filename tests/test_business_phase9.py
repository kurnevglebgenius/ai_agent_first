"""Offline-проверки аналитики, исправлений, бюджетов и защищённых backup."""

from datetime import date, datetime, timezone
import csv
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from shared.agent_runtime.business import BusinessAgent, BusinessStore
from shared.agent_runtime.business_features import BusinessFeatures
from shared.agent_runtime.business_reporting import (
    BusinessQuery,
    OpenAIBusinessQueryExtractor,
    parse_period,
    render_comparison,
    render_report,
    render_top_expenses,
)
from shared.agent_runtime.model_provider import ModelResponseError


TODAY = date(2026, 9, 15)
NOW = datetime(2026, 9, 15, 12, 34, 56, tzinfo=timezone.utc)


class Base:
    has_context = False

    def memory_command(self, _message):
        return None

    def reply(self, _message):
        return "обычный ответ"

    def reset(self):
        pass


class Extractor:
    def __init__(self, *items):
        self.items = iter(items)

    def extract(self, _message, *, today, draft):
        return next(self.items)


class QueryExtractor:
    def __init__(self, query):
        self.query = query

    def extract_query(self, _message, *, today):
        return self.query


class Phase9Tests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "business.sqlite3"
        self.counter = 0

    def store(self, session="alice"):
        def identifier():
            self.counter += 1
            return f"{self.counter:032x}"
        return BusinessStore(self.path, session, id_factory=identifier, clock=lambda: NOW)

    @staticmethod
    def save(store, **changes):
        fields = {
            "kind": "expense", "amount": "20", "currency": "RUB",
            "occurred_on": "2026-09-15", "category": "реклама",
        }
        fields.update(changes)
        store.update_draft(fields)
        return store.commit_draft()

    def test_old_phase8_database_is_migrated_without_losing_data(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute("""CREATE TABLE business_operations (
                id TEXT PRIMARY KEY, session TEXT NOT NULL, kind TEXT NOT NULL,
                amount TEXT NOT NULL, currency TEXT NOT NULL, occurred_on TEXT NOT NULL,
                category TEXT, counterparty TEXT, note TEXT, created_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE business_drafts (
                session TEXT PRIMARY KEY, id TEXT NOT NULL UNIQUE, kind TEXT, amount TEXT,
                currency TEXT, occurred_on TEXT, category TEXT, counterparty TEXT, note TEXT,
                updated_at TEXT NOT NULL)""")
            db.execute("INSERT INTO business_operations VALUES (?,?,?,?,?,?,?,?,?,?)", (
                "a" * 32, "alice", "expense", "10", "RUB", "2026-09-01",
                "реклама", None, None, "2026-09-01T00:00:00Z",
            ))
            db.commit()
        finally:
            db.close()
        store = self.store()
        item = store.list_operations()[0]
        self.assertEqual((item.amount, item.payment_status, item.project, item.deleted_at),
                         ("10", "paid", None, None))

    def test_alias_report_and_currency_safe_top(self):
        store = self.store()
        features = BusinessFeatures(store)
        self.assertEqual(features.resolve_category("таргет"), "реклама")
        features.set_alias("промо", "маркетинг")
        self.save(store, category=features.resolve_category("промо"), amount="35")
        self.save(store, category="аренда", amount="5", currency="USD")
        report = render_report(store, parse_period("2026-09", TODAY))
        self.assertIn("Расходы: 35 RUB, 5 USD", report)
        top = render_top_expenses(store, parse_period("month", TODAY))
        self.assertIn("RUB:\n- маркетинг: 35 RUB", top)
        self.assertIn("USD:\n- аренда: 5 USD", top)

    def test_edit_soft_delete_restore_and_audit(self):
        store = self.store()
        item = self.save(store)
        agent = BusinessAgent(Base(), store, Extractor(), today=lambda: TODAY,
                              features=BusinessFeatures(store))
        self.assertIn("Черновик изменения", agent.reply(f"/edit {item.id} amount=25"))
        self.assertIn("обновлена", agent.reply("/save"))
        self.assertEqual(store.get_operation(item.id).amount, "25")
        self.assertIn("удалена", agent.reply(f"/delete {item.id}"))
        self.assertIsNone(store.get_operation(item.id))
        self.assertIn(item.id, agent.reply("/deleted"))
        self.assertIn("восстановлена", agent.reply(f"/restore {item.id}"))
        self.assertEqual([event.action for event in reversed(store.list_audit())][:4],
                         ["created", "updated", "deleted", "restored"])

    def test_budget_alert_and_unpaid_exclusion(self):
        store = self.store()
        features = BusinessFeatures(store)
        features.set_budget("реклама", "100", "RUB", None, TODAY)
        agent = BusinessAgent(
            Base(), store,
            Extractor({"kind": "expense", "amount": "85", "currency": "RUB",
                       "occurred_on": "сегодня", "category": "таргет"}),
            today=lambda: TODAY, features=features,
        )
        agent.reply("Потратили 85 рублей на таргет")
        self.assertIn("осталось 15 RUB", agent.reply("/save"))
        self.save(store, amount="50", payment_status="unpaid")
        status = features.list_budgets(None, TODAY)[0]
        self.assertEqual((status.used, status.remaining), ("85", "15"))

    def test_template_creates_confirmable_draft_not_operation(self):
        store = self.store()
        features = BusinessFeatures(store)
        store.update_draft({"kind": "expense", "amount": "50", "currency": "BYN",
                            "occurred_on": "2026-09-01", "category": "аренда"})
        agent = BusinessAgent(Base(), store, Extractor(), today=lambda: TODAY, features=features)
        self.assertIn("Шаблон", agent.reply("/template save аренда monthly"))
        agent.reply("/cancel")
        self.assertIn("Черновик готов", agent.reply("/template use аренда"))
        self.assertEqual(store.list_operations(), [])
        self.assertEqual(store.get_draft().occurred_on, TODAY.isoformat())

    def test_natural_query_is_read_only_and_uses_project_filter(self):
        store = self.store()
        self.save(store, kind="income", amount="200", project="Сайт Альфа")
        agent = BusinessAgent(
            Base(), store, Extractor(None), today=lambda: TODAY,
            query_extractor=QueryExtractor(BusinessQuery(
                "report", "month", project="Альфа"
            )),
        )
        answer = agent.reply("Сколько заработали на проекте Альфа?")
        self.assertIn("Доходы: 200 RUB", answer)
        self.assertEqual(len(store.list_operations()), 1)

    def test_comparison_and_unpaid_command(self):
        store = self.store()
        self.save(store, amount="10", occurred_on="2026-08-15")
        self.save(store, amount="15", occurred_on="2026-09-15", payment_status="unpaid",
                  project="Лендинг")
        self.save(store, amount="20", occurred_on="2026-09-15")
        comparison = render_comparison(
            store, parse_period("2026-08", TODAY), parse_period("2026-09", TODAY)
        )
        self.assertIn("10 → 20 (+10)", comparison)
        agent = BusinessAgent(Base(), store, Extractor(), today=lambda: TODAY)
        self.assertIn("Лендинг", agent.reply("/unpaid"))

    def test_csv_and_encrypted_backup_round_trip(self):
        store = self.store()
        item = self.save(store, project="Сайт")
        features = BusinessFeatures(store, passphrase="correct horse battery")
        csv_path = features.export_csv(store.query_operations())
        with csv_path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual((rows[0]["id"], rows[0]["project"]), (item.id, "Сайт"))

        backup = features.create_backup()
        self.assertNotIn(b"\xd1\x80\xd0\xb5\xd0\xba\xd0\xbb\xd0\xb0\xd0\xbc\xd0\xb0", backup.read_bytes())
        store.delete_operation(item.id)
        features.restore_backup(backup.name)
        self.assertIsNotNone(store.get_operation(item.id))
        wrong = BusinessFeatures(store, passphrase="another wrong phrase")
        with self.assertRaisesRegex(ValueError, "расшифровать"):
            wrong.restore_backup(backup.name)
        with self.assertRaisesRegex(ValueError, "имя backup"):
            features.restore_backup("../secret.bak")
        bob = BusinessFeatures(self.store("bob"), passphrase="correct horse battery")
        with self.assertRaisesRegex(ValueError, "другой сессии"):
            bob.restore_backup(backup.name)

    def test_backup_works_with_generated_local_key(self):
        store = self.store()
        self.save(store)
        first = BusinessFeatures(store)
        backup = first.create_backup()
        self.assertTrue(first.local_key_path.is_file())
        operation_id = store.list_operations()[0].id
        store.delete_operation(operation_id)
        # Новый объект читает тот же локальный ключ и может проверить копию.
        BusinessFeatures(store).restore_backup(backup.name)
        self.assertEqual(len(store.list_operations()), 1)

    def test_query_extractor_rejects_duplicate_json_keys(self):
        sdk = Mock()
        data = {name: None for name in (
            "period", "second_period", "category", "counterparty", "project", "currency"
        )}
        tail = json.dumps(data, ensure_ascii=False)[1:]
        sdk.responses.create.return_value = SimpleNamespace(
            status="completed", output=[SimpleNamespace(
                type="function_call", name="extract_business_query",
                arguments='{"action":"report","action":"other",' + tail,
            )],
        )
        with self.assertRaises(ModelResponseError):
            OpenAIBusinessQueryExtractor(sdk).extract_query("x", today=TODAY)


if __name__ == "__main__":
    unittest.main()
