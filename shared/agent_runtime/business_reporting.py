"""Детерминированные периоды, отчёты и read-only вопросы к бизнес-данным."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
import json
import logging
import re
from time import perf_counter
from typing import Protocol

from .business import (
    BusinessOperation,
    BusinessStore,
    BusinessValidationError,
    normalize_currency,
)
from .model_provider import ModelResponseError


logger = logging.getLogger("agent_runtime")


MONTHS = {
    "январь": 1, "января": 1, "февраль": 2, "февраля": 2,
    "март": 3, "марта": 3, "апрель": 4, "апреля": 4,
    "май": 5, "мая": 5, "июнь": 6, "июня": 6,
    "июль": 7, "июля": 7, "август": 8, "августа": 8,
    "сентябрь": 9, "сентября": 9, "октябрь": 10, "октября": 10,
    "ноябрь": 11, "ноября": 11, "декабрь": 12, "декабря": 12,
}


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date
    label: str


@dataclass(frozen=True)
class BusinessQuery:
    action: str
    period: str | None = None
    second_period: str | None = None
    category: str | None = None
    counterparty: str | None = None
    project: str | None = None
    currency: str | None = None


class BusinessQueryExtractor(Protocol):
    def extract_query(self, message: str, *, today: date) -> BusinessQuery | None:
        """Вернуть безопасное read-only намерение либо None для обычного вопроса."""


def parse_period(value: str, today: date) -> DateRange:
    raw = value.strip().casefold()
    if raw in ("today", "сегодня"):
        return DateRange(today, today, today.isoformat())
    if raw in ("yesterday", "вчера"):
        day = today - timedelta(days=1)
        return DateRange(day, day, day.isoformat())
    if raw in ("week", "неделя", "эта неделя", "текущая неделя"):
        start = today - timedelta(days=today.weekday())
        return DateRange(start, start + timedelta(days=6), f"{start.isoformat()}…{(start + timedelta(days=6)).isoformat()}")
    if raw in ("month", "месяц", "этот месяц", "текущий месяц"):
        raw = f"{today.year:04d}-{today.month:02d}"
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        year, month = map(int, raw.split("-"))
        if not 1 <= month <= 12:
            raise BusinessValidationError("Некорректный месяц отчёта.")
        start = date(year, month, 1)
        return DateRange(start, date(year, month, monthrange(year, month)[1]), raw)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            day = date.fromisoformat(raw)
        except ValueError as error:
            raise BusinessValidationError("Некорректная дата отчёта.") from error
        return DateRange(day, day, raw)
    pair = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s+(\d{4}-\d{2}-\d{2})", raw)
    if pair:
        try:
            start, end = (date.fromisoformat(item) for item in pair.groups())
        except ValueError as error:
            raise BusinessValidationError("Некорректный диапазон отчёта.") from error
        if start > end:
            raise BusinessValidationError("Начало периода не может быть позже конца.")
        return DateRange(start, end, f"{start.isoformat()}…{end.isoformat()}")
    month_name = re.fullmatch(r"([а-яё]+)(?:\s+(\d{4}))?", raw)
    if month_name and month_name.group(1) in MONTHS:
        month = MONTHS[month_name.group(1)]
        year = int(month_name.group(2) or today.year)
        start = date(year, month, 1)
        return DateRange(start, date(year, month, monthrange(year, month)[1]),
                         f"{year:04d}-{month:02d}")
    raise BusinessValidationError(
        "Период: today, week, month, YYYY-MM, YYYY-MM-DD или две ISO-даты."
    )


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _totals(operations: list[BusinessOperation], kind: str | None = None) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for item in operations:
        if kind is None or item.kind == kind:
            result[item.currency] = result.get(item.currency, Decimal("0")) + Decimal(item.amount)
    return result


def _money_lines(values: dict[str, Decimal]) -> str:
    if not values:
        return "—"
    return ", ".join(f"{_decimal_text(values[currency])} {currency}"
                     for currency in sorted(values))


def render_report(
    store: BusinessStore,
    period: DateRange,
    *,
    category: str | None = None,
    counterparty: str | None = None,
    project: str | None = None,
    currency: str | None = None,
) -> str:
    operations = store.query_operations(
        date_from=period.start.isoformat(), date_to=period.end.isoformat(),
        category=category.casefold() if category else None,
        counterparty=counterparty, project=project,
        currency=normalize_currency(currency) if currency else None,
    )
    paid = [item for item in operations if item.payment_status == "paid"]
    unpaid = [item for item in operations if item.payment_status == "unpaid"]
    income, expense = _totals(paid, "income"), _totals(paid, "expense")
    currencies = sorted(set(income) | set(expense))
    profit = {currency: income.get(currency, Decimal("0")) - expense.get(currency, Decimal("0"))
              for currency in currencies}
    filters = [value for value in (
        f"категория={category.casefold()}" if category else None,
        f"контрагент={counterparty}" if counterparty else None,
        f"проект={project}" if project else None,
        f"валюта={currency.upper()}" if currency else None,
    ) if value]
    suffix = f" ({'; '.join(filters)})" if filters else ""
    lines = [
        f"Отчёт за {period.label}{suffix}:",
        f"Доходы: {_money_lines(income)}",
        f"Расходы: {_money_lines(expense)}",
        f"Результат: {_money_lines(profit)}",
    ]
    if unpaid:
        lines.append(f"Ожидают оплаты: {_money_lines(_totals(unpaid))}")
    return "\n".join(lines)


def render_top_expenses(store: BusinessStore, period: DateRange) -> str:
    operations = store.query_operations(
        date_from=period.start.isoformat(), date_to=period.end.isoformat(),
        kind="expense", payment_status="paid",
    )
    totals: dict[tuple[str, str], Decimal] = {}
    for item in operations:
        key = (item.category or "без категории", item.currency)
        totals[key] = totals.get(key, Decimal("0")) + Decimal(item.amount)
    if not totals:
        return f"За {period.label} расходов нет."
    lines = [f"Расходы по категориям за {period.label}:"]
    # Категории ранжируются только внутри валюты: RUB и USD нельзя сравнивать без курса.
    for currency in sorted({key[1] for key in totals}):
        lines.append(f"{currency}:")
        ordered = sorted(
            ((category, amount) for (category, code), amount in totals.items()
             if code == currency),
            key=lambda item: (-item[1], item[0]),
        )
        lines.extend(f"- {category}: {_decimal_text(amount)} {currency}"
                     for category, amount in ordered)
    return "\n".join(lines)


def render_comparison(store: BusinessStore, first: DateRange, second: DateRange) -> str:
    def expenses(period):
        return _totals(store.query_operations(
            date_from=period.start.isoformat(), date_to=period.end.isoformat(),
            kind="expense", payment_status="paid",
        ))
    left, right = expenses(first), expenses(second)
    currencies = sorted(set(left) | set(right))
    if not currencies:
        return "В сравниваемых периодах расходов нет."
    lines = [f"Сравнение расходов: {first.label} → {second.label}"]
    for currency in currencies:
        before, after = left.get(currency, Decimal("0")), right.get(currency, Decimal("0"))
        change = after - before
        sign = "+" if change > 0 else ""
        lines.append(
            f"- {currency}: {_decimal_text(before)} → {_decimal_text(after)} "
            f"({sign}{_decimal_text(change)})"
        )
    return "\n".join(lines)


def render_unpaid(store: BusinessStore, limit: int = 20) -> str:
    items = store.query_operations(payment_status="unpaid", limit=limit)
    if not items:
        return "Неоплаченных операций нет."
    lines = ["Ожидают оплаты:"]
    for item in items:
        direction = "получить" if item.kind == "income" else "оплатить"
        lines.append(
            f"- {item.id} | {item.occurred_on} | {direction} {item.amount} {item.currency} | "
            f"{item.project or item.counterparty or item.category or 'без уточнения'}"
        )
    return "\n".join(lines)


QUERY_FIELDS = ("action", "period", "second_period", "category",
                "counterparty", "project", "currency")
QUERY_SCHEMA = {
    "type": "function",
    "name": "extract_business_query",
    "description": "Извлечь безопасный read-only вопрос к локальным бизнес-операциям.",
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": [
                "report", "top_expenses", "compare_expenses", "unpaid", "other"
            ]},
            **{name: {"anyOf": [{"type": "string"}, {"type": "null"}]}
               for name in QUERY_FIELDS if name != "action"},
        },
        "required": list(QUERY_FIELDS),
        "additionalProperties": False,
    },
}
QUERY_INSTRUCTIONS = (
    "Классифицируй только read-only вопрос к уже сохранённым доходам и расходам. "
    "report подходит для сумм дохода, расхода, результата, клиента или проекта; "
    "top_expenses — для вопроса, на что ушло больше всего; compare_expenses — для двух периодов; "
    "unpaid — для неоплаченных работ или счетов. Для обычного вопроса верни other. "
    "Период верни как today, week, month, YYYY-MM, YYYY-MM-DD либо 'дата дата'. "
    "Для сравнения второй период положи в second_period. Не придумывай фильтры и валюту. "
    "Это только чтение: никогда не предлагай запись, изменение или удаление."
)


class OpenAIBusinessQueryExtractor:
    def __init__(self, client, model: str = "gpt-5-mini", reasoning_effort: str | None = None):
        self.client = client
        self.model = model
        self.reasoning_effort = reasoning_effort

    def extract_query(self, message: str, *, today: date) -> BusinessQuery | None:
        request = {
            "model": self.model,
            "input": json.dumps({"application_date": today.isoformat(),
                                 "last_user_message": message}, ensure_ascii=False),
            "instructions": QUERY_INSTRUCTIONS,
            "tools": [QUERY_SCHEMA],
            "tool_choice": {"type": "function", "name": "extract_business_query"},
            "parallel_tool_calls": False,
        }
        if self.reasoning_effort is not None:
            request["reasoning"] = {"effort": self.reasoning_effort}
        started = perf_counter()
        try:
            response = self.client.responses.create(**request)
        finally:
            logger.info("business_query_finished duration_s=%.2f", perf_counter() - started)
        if getattr(response, "status", "completed") != "completed":
            raise ModelResponseError("Модель не завершила разбор бизнес-вопроса.")
        calls = [item for item in getattr(response, "output", ())
                 if getattr(item, "type", None) == "function_call"]
        if len(calls) != 1 or calls[0].name != "extract_business_query":
            raise ModelResponseError("Модель не вернула структуру бизнес-вопроса.")
        try:
            raw = calls[0].arguments
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > 8000:
                raise ValueError()

            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result

            data = json.loads(raw, object_pairs_hook=unique_object)
        except (TypeError, ValueError, UnicodeError, RecursionError) as error:
            raise ModelResponseError("Модель вернула некорректный бизнес-вопрос.") from error
        if (not isinstance(data, dict) or set(data) != set(QUERY_FIELDS)
                or data.get("action") not in QUERY_SCHEMA["parameters"]["properties"]["action"]["enum"]
                or any(value is not None and not isinstance(value, str)
                       for name, value in data.items() if name != "action")):
            raise ModelResponseError("Модель вернула некорректный бизнес-вопрос.")
        if data["action"] == "other":
            return None
        for name in QUERY_FIELDS[1:]:
            if data[name] is not None and (not data[name].strip() or len(data[name]) > 120):
                raise ModelResponseError("Модель вернула некорректный фильтр бизнес-вопроса.")
        return BusinessQuery(*(data[name].strip() if isinstance(data[name], str) else None
                               for name in QUERY_FIELDS))


def answer_query(store: BusinessStore, query: BusinessQuery, today: date) -> str:
    if query.action == "unpaid":
        return render_unpaid(store)
    period = parse_period(query.period or "month", today)
    if query.action == "top_expenses":
        return render_top_expenses(store, period)
    if query.action == "compare_expenses":
        if not query.second_period:
            raise BusinessValidationError("Для сравнения нужны два периода.")
        return render_comparison(store, period, parse_period(query.second_period, today))
    return render_report(
        store, period, category=query.category, counterparty=query.counterparty,
        project=query.project, currency=query.currency,
    )
