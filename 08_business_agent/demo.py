"""Детерминированная демонстрация Business Agent без LLM и внешней сети."""

from datetime import date, datetime, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.agent_runtime.business import BusinessAgent, BusinessStore  # noqa: E402


class OfflineExtractor:
    """Фиксированные предложения полей нужны только для воспроизводимого demo."""

    def __init__(self):
        self.responses = iter((
            {"kind": "expense", "amount": "450.00", "occurred_on": "вчера",
             "category": "реклама"},
            {"currency": "byn"},
        ))

    def extract(self, message, *, today, draft):
        return next(self.responses)


class NoModelAgent:
    has_context = False

    def reset(self):
        return None

    def memory_command(self, message):
        return None

    def reply(self, message):
        raise AssertionError("В offline-demo обычный LLM-ответ не требуется.")


def main() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "business.sqlite3"
        store = BusinessStore(
            path,
            "demo",
            clock=lambda: datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc),
        )
        agent = BusinessAgent(
            NoModelAgent(), store, OfflineExtractor(), today=lambda: date(2026, 9, 15)
        )
        for message in (
            "Вчера потратил 450 на рекламу", "BYN", "/save", "/list", "/money"
        ):
            print(f"Вы: {message}")
            print(f"Агент: {agent.reply(message)}\n")

        reopened = BusinessStore(path, "demo")
        assert len(reopened.list_operations()) == 1
        print("Проверка перезапуска: сохранена ровно одна операция.")


if __name__ == "__main__":
    main()
