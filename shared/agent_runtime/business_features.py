"""Категории, бюджеты, шаблоны, CSV и зашифрованные сессионные backup."""

from __future__ import annotations

import csv
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

from .business import (
    BUSINESS_FIELDS,
    ID_PATTERN,
    BusinessDraft,
    BusinessStore,
    BusinessValidationError,
    normalize_amount,
    normalize_currency,
    _utc_timestamp,
    validate_complete,
)


DEFAULT_ALIASES = {
    "таргет": "реклама",
    "таргетинг": "реклама",
    "instagram ads": "реклама",
    "google ads": "реклама",
    "реклама в instagram": "реклама",
    "офис": "аренда",
    "аренда помещения": "аренда",
    "продажа сайта": "разработка",
    "разработка сайта": "разработка",
    "лендинг": "разработка",
}
SCHEDULES = frozenset({"daily", "weekly", "monthly", "yearly"})
BACKUP_PATTERN = re.compile(r"[0-9a-f]{12}-\d{8}T\d{6}Z-[0-9a-f]{8}\.bak\Z")
TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")


@dataclass(frozen=True)
class BudgetStatus:
    category: str
    currency: str
    month: str
    limit: str
    used: str
    remaining: str


@dataclass(frozen=True)
class OperationTemplate:
    name: str
    schedule: str
    kind: str
    amount: str
    currency: str
    category: str | None
    counterparty: str | None
    note: str | None
    project: str | None
    payment_status: str


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _month(value: str | None, today: date) -> str:
    raw = (value or "month").strip().casefold()
    if raw in ("month", "месяц", "текущий"):
        return f"{today.year:04d}-{today.month:02d}"
    if not re.fullmatch(r"\d{4}-\d{2}", raw):
        raise BusinessValidationError("Месяц должен быть YYYY-MM или month.")
    year, month = map(int, raw.split("-"))
    if not 1 <= month <= 12:
        raise BusinessValidationError("Некорректный месяц бюджета.")
    return f"{year:04d}-{month:02d}"


def _short_text(value: str, label: str, maximum: int = 80) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise BusinessValidationError(f"{label}: нужен непустой текст до {maximum} символов.")
    normalized = value.strip().casefold()
    if "\x00" in normalized:
        raise BusinessValidationError(f"Некорректное поле «{label}».")
    return normalized


class BusinessFeatures:
    def __init__(self, store: BusinessStore, *, passphrase: str | None = None):
        self.store = store
        self.passphrase = passphrase.strip() if isinstance(passphrase, str) else ""
        self.exports_dir = store.path.parent / "exports"
        self.backups_dir = store.path.parent / "backups"
        self.local_key_path = store.path.parent / "business-backup.key"
        with store._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS business_category_aliases (
                    session TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    category TEXT NOT NULL,
                    PRIMARY KEY(session, alias)
                )
            """)

            db.execute("""
                CREATE TABLE IF NOT EXISTS business_budgets (
                    session TEXT NOT NULL,
                    category TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    month TEXT NOT NULL,
                    limit_amount TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(session, category, currency, month)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS business_templates (
                    session TEXT NOT NULL,
                    name TEXT NOT NULL,
                    schedule TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    category TEXT,
                    counterparty TEXT,
                    note TEXT,
                    project TEXT,
                    payment_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(session, name)
                )
            """)

    @staticmethod
    def default_aliases() -> dict[str, str]:
        return dict(DEFAULT_ALIASES)

    def _timestamp(self) -> str:
        return _utc_timestamp(self.store._clock)

    def resolve_category(self, category: str) -> str:
        alias = _short_text(category, "Категория")
        with self.store._connect() as db:
            row = db.execute("""
                SELECT category FROM business_category_aliases
                WHERE session=? AND alias=?
            """, (self.store.session, alias)).fetchone()
        return row["category"] if row else DEFAULT_ALIASES.get(alias, alias)

    def set_alias(self, alias: str, category: str) -> tuple[str, str]:
        alias, category = (_short_text(alias, "Псевдоним"),
                           _short_text(category, "Категория"))
        with self.store._connect() as db:
            db.execute("""
                INSERT INTO business_category_aliases(session, alias, category)
                VALUES (?, ?, ?) ON CONFLICT(session, alias)
                DO UPDATE SET category=excluded.category
            """, (self.store.session, alias, category))
            self.store._audit(db, "alias_saved", "category_alias", alias)
        return alias, category

    def delete_alias(self, alias: str) -> bool:
        alias = _short_text(alias, "Псевдоним")
        with self.store._connect() as db:
            removed = db.execute("""
                DELETE FROM business_category_aliases WHERE session=? AND alias=?
            """, (self.store.session, alias)).rowcount
            if removed:
                self.store._audit(db, "alias_deleted", "category_alias", alias)
            return bool(removed)

    def list_aliases(self) -> list[tuple[str, str]]:
        with self.store._connect() as db:
            return [tuple(row) for row in db.execute("""
                SELECT alias, category FROM business_category_aliases
                WHERE session=? ORDER BY alias
            """, (self.store.session,)).fetchall()]

    def set_budget(self, category: str, amount: str, currency: str,
                   month: str | None, today: date) -> BudgetStatus:
        category = self.resolve_category(category)
        amount, currency, month = normalize_amount(amount), normalize_currency(currency), _month(month, today)
        with self.store._connect() as db:
            db.execute("""
                INSERT INTO business_budgets(
                    session, category, currency, month, limit_amount, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session, category, currency, month)
                DO UPDATE SET limit_amount=excluded.limit_amount, updated_at=excluded.updated_at
            """, (self.store.session, category, currency, month, amount,
                  self._timestamp()))
            self.store._audit(db, "budget_saved", "budget", f"{month}:{currency}:{category}")
        return self.budget_status(category, currency, month, today)

    def budget_status(self, category: str, currency: str, month: str | None,
                      today: date) -> BudgetStatus:
        category, currency, month = (self.resolve_category(category),
                                     normalize_currency(currency), _month(month, today))
        with self.store._connect() as db:
            row = db.execute("""
                SELECT limit_amount FROM business_budgets
                WHERE session=? AND category=? AND currency=? AND month=?
            """, (self.store.session, category, currency, month)).fetchone()
        if row is None:
            raise BusinessValidationError("Бюджет для этой категории, валюты и месяца не найден.")
        operations = self.store.query_operations(
            date_from=f"{month}-01",
            date_to=f"{month}-{monthrange(int(month[:4]), int(month[5:]))[1]:02d}",
            kind="expense",
            category=category, currency=currency, payment_status="paid",
        )
        used = sum((Decimal(item.amount) for item in operations), Decimal("0"))
        limit = Decimal(row["limit_amount"])
        return BudgetStatus(category, currency, month, _decimal_text(limit),
                            _decimal_text(used), _decimal_text(limit - used))

    def list_budgets(self, month: str | None, today: date) -> list[BudgetStatus]:
        month = _month(month, today)
        with self.store._connect() as db:
            rows = db.execute("""
                SELECT category, currency FROM business_budgets
                WHERE session=? AND month=? ORDER BY category, currency
            """, (self.store.session, month)).fetchall()
        return [self.budget_status(row["category"], row["currency"], month, today)
                for row in rows]

    def delete_budget(self, category: str, currency: str, month: str | None,
                      today: date) -> bool:
        category, currency, month = (self.resolve_category(category),
                                     normalize_currency(currency), _month(month, today))
        with self.store._connect() as db:
            removed = db.execute("""
                DELETE FROM business_budgets
                WHERE session=? AND category=? AND currency=? AND month=?
            """, (self.store.session, category, currency, month)).rowcount
            if removed:
                self.store._audit(db, "budget_deleted", "budget", f"{month}:{currency}:{category}")
            return bool(removed)

    def budget_after_operation(self, operation, today: date) -> BudgetStatus | None:
        if operation.kind != "expense" or operation.payment_status != "paid" or not operation.category:
            return None
        month = operation.occurred_on[:7]
        try:
            return self.budget_status(operation.category, operation.currency, month, today)
        except BusinessValidationError:
            return None

    def save_template(self, name: str, schedule: str, draft: BusinessDraft) -> OperationTemplate:
        name = _short_text(name, "Название шаблона")
        schedule = schedule.strip().casefold()
        if schedule not in SCHEDULES:
            raise BusinessValidationError("Период шаблона: daily, weekly, monthly или yearly.")
        fields = draft.fields()
        fields.setdefault("payment_status", "paid")
        validate_complete(fields, date(2000, 1, 1))
        with self.store._connect() as db:
            db.execute("""
                INSERT INTO business_templates(
                    session, name, schedule, kind, amount, currency, category,
                    counterparty, note, project, payment_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session, name) DO UPDATE SET
                    schedule=excluded.schedule, kind=excluded.kind, amount=excluded.amount,
                    currency=excluded.currency, category=excluded.category,
                    counterparty=excluded.counterparty, note=excluded.note,
                    project=excluded.project, payment_status=excluded.payment_status
            """, (self.store.session, name, schedule,
                  *(fields.get(key) for key in (
                      "kind", "amount", "currency", "category", "counterparty",
                      "note", "project", "payment_status"
                  )), self._timestamp()))
            self.store._audit(db, "template_saved", "template", name)
        return self.get_template(name)

    @staticmethod
    def _template_from_row(row) -> OperationTemplate:
        return OperationTemplate(*(row[name] for name in (
            "name", "schedule", "kind", "amount", "currency", "category",
            "counterparty", "note", "project", "payment_status"
        )))

    def get_template(self, name: str) -> OperationTemplate | None:
        name = _short_text(name, "Название шаблона")
        with self.store._connect() as db:
            row = db.execute("""
                SELECT * FROM business_templates WHERE session=? AND name=?
            """, (self.store.session, name)).fetchone()
        return self._template_from_row(row) if row else None

    def list_templates(self) -> list[OperationTemplate]:
        with self.store._connect() as db:
            rows = db.execute("""
                SELECT * FROM business_templates WHERE session=? ORDER BY name
            """, (self.store.session,)).fetchall()
        return [self._template_from_row(row) for row in rows]

    def delete_template(self, name: str) -> bool:
        name = _short_text(name, "Название шаблона")
        with self.store._connect() as db:
            removed = db.execute("""
                DELETE FROM business_templates WHERE session=? AND name=?
            """, (self.store.session, name)).rowcount
            if removed:
                self.store._audit(db, "template_deleted", "template", name)
            return bool(removed)

    def template_fields(self, name: str, today: date) -> dict[str, str] | None:
        item = self.get_template(name)
        if item is None:
            return None
        return {key: value for key, value in {
            "kind": item.kind, "amount": item.amount, "currency": item.currency,
            "occurred_on": today.isoformat(), "category": item.category,
            "counterparty": item.counterparty, "note": item.note, "project": item.project,
            "payment_status": item.payment_status,
        }.items() if value is not None}

    def export_csv(self, operations) -> Path:
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        stamp = self._timestamp().replace("-", "").replace(":", "")
        prefix = hashlib.sha256(self.store.session.encode()).hexdigest()[:12]
        path = self.exports_dir / f"{prefix}-{stamp}-{uuid4().hex[:8]}.csv"
        with path.open("x", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=[
                "id", "kind", "amount", "currency", "occurred_on", "category",
                "counterparty", "project", "payment_status", "note", "created_at",
            ])
            writer.writeheader()
            for item in operations:
                writer.writerow({name: getattr(item, name) for name in writer.fieldnames})
        return path

    def _require_passphrase(self) -> str:
        if self.passphrase and len(self.passphrase) < 12:
            raise BusinessValidationError(
                "Для backup задайте BUSINESS_BACKUP_PASSPHRASE длиной не менее 12 символов в .env."
            )
        if self.passphrase:
            return self.passphrase
        # Без настройки создаём локальный ключ с exclusive-create. Он лежит рядом с
        # игнорируемой SQLite и делает backup работоспособным, но не переносимым сам по себе.
        import os
        import secrets
        try:
            descriptor = os.open(
                self.local_key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError:
            descriptor = None
        if descriptor is not None:
            try:
                os.write(descriptor, secrets.token_urlsafe(32).encode("ascii"))
            finally:
                os.close(descriptor)
        try:
            key = self.local_key_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as error:
            raise BusinessValidationError("Не удалось открыть локальный ключ backup.") from error
        if len(key) < 32 or len(key) > 128 or not key.isascii() or any(ch.isspace() for ch in key):
            raise BusinessValidationError("Локальный ключ backup повреждён.")
        return key

    def _snapshot(self) -> dict:
        with self.store._connect() as db:
            operations = [dict(row) for row in db.execute("""
                SELECT id, kind, amount, currency, occurred_on, category, counterparty,
                       note, project, payment_status, created_at, deleted_at
                FROM business_operations WHERE session=? ORDER BY rowid
            """, (self.store.session,)).fetchall()]
            budgets = [dict(row) for row in db.execute("""
                SELECT category, currency, month, limit_amount
                FROM business_budgets WHERE session=? ORDER BY category, currency, month
            """, (self.store.session,)).fetchall()]
            templates = [dict(row) for row in db.execute("""
                SELECT name, schedule, kind, amount, currency, category, counterparty,
                       note, project, payment_status, created_at
                FROM business_templates WHERE session=? ORDER BY name
            """, (self.store.session,)).fetchall()]
            aliases = [dict(row) for row in db.execute("""
                SELECT alias, category FROM business_category_aliases
                WHERE session=? ORDER BY alias
            """, (self.store.session,)).fetchall()]
        return {"version": 1, "operations": operations, "budgets": budgets,
                "templates": templates, "aliases": aliases}

    @staticmethod
    def _key(passphrase: str, salt: bytes) -> bytes:
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        return Scrypt(salt=salt, length=32, n=2 ** 14, r=8, p=1).derive(passphrase.encode("utf-8"))

    def create_backup(self) -> Path:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        import os
        passphrase = self._require_passphrase()
        raw = json.dumps(self._snapshot(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        salt, nonce = os.urandom(16), os.urandom(12)
        encrypted = AESGCM(self._key(passphrase, salt)).encrypt(
            nonce, raw, b"business-agent-backup-v1"
        )
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        prefix = hashlib.sha256(self.store.session.encode()).hexdigest()[:12]
        stamp = self._timestamp().replace("-", "").replace(":", "")
        path = self.backups_dir / f"{prefix}-{stamp}-{uuid4().hex[:8]}.bak"
        with path.open("xb") as stream:
            stream.write(b"BAB1" + salt + nonce + encrypted)
        with self.store._connect() as db:
            self.store._audit(db, "backup_created", "backup", path.name)
        return path

    def list_backups(self) -> list[str]:
        if not self.backups_dir.exists():
            return []
        prefix = hashlib.sha256(self.store.session.encode()).hexdigest()[:12] + "-"
        return sorted((path.name for path in self.backups_dir.glob(f"{prefix}*.bak")
                       if BACKUP_PATTERN.fullmatch(path.name)), reverse=True)[:20]

    def _read_backup(self, filename: str) -> dict:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        passphrase = self._require_passphrase()
        if not BACKUP_PATTERN.fullmatch(filename):
            raise BusinessValidationError("Некорректное имя backup-файла.")
        prefix = hashlib.sha256(self.store.session.encode()).hexdigest()[:12] + "-"
        if not filename.startswith(prefix):
            raise BusinessValidationError("Backup принадлежит другой сессии.")
        path = (self.backups_dir / filename).resolve()
        if path.parent != self.backups_dir.resolve() or not path.is_file():
            raise BusinessValidationError("Backup-файл не найден.")
        raw = path.read_bytes()
        if len(raw) > 10_000_000 or len(raw) < 49 or raw[:4] != b"BAB1":
            raise BusinessValidationError("Некорректный backup-файл.")
        salt, nonce, encrypted = raw[4:20], raw[20:32], raw[32:]
        try:
            clear = AESGCM(self._key(passphrase, salt)).decrypt(
                nonce, encrypted, b"business-agent-backup-v1"
            )
            data = json.loads(clear)
        except (InvalidTag, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise BusinessValidationError("Не удалось расшифровать или проверить backup.") from error
        self._validate_snapshot(data)
        return data

    @staticmethod
    def _validate_snapshot(data: object) -> None:
        if (not isinstance(data, dict)
                or set(data) != {"version", "operations", "budgets", "templates", "aliases"}
                or data["version"] != 1
                or any(not isinstance(data[name], list)
                       for name in ("operations", "budgets", "templates", "aliases"))):
            raise BusinessValidationError("Неподдерживаемый формат backup.")
        for item in data["operations"]:
            expected = {"id", *BUSINESS_FIELDS, "created_at", "deleted_at"}
            if (not isinstance(item, dict) or set(item) != expected
                    or not isinstance(item["id"], str)
                    or not ID_PATTERN.fullmatch(item["id"])):
                raise BusinessValidationError("Некорректная операция в backup.")
            fields = {name: item[name] for name in BUSINESS_FIELDS if item[name] is not None}
            validate_complete(fields, date(2000, 1, 1))
            if (not isinstance(item["created_at"], str)
                    or not TIMESTAMP_PATTERN.fullmatch(item["created_at"])
                    or item["deleted_at"] is not None
                    and (not isinstance(item["deleted_at"], str)
                         or not TIMESTAMP_PATTERN.fullmatch(item["deleted_at"]))):
                raise BusinessValidationError("Некорректное время операции в backup.")
        if len(data["operations"]) > 5000:
            raise BusinessValidationError("Backup содержит слишком много операций.")
        for item in data["budgets"]:
            if not isinstance(item, dict) or set(item) != {
                "category", "currency", "month", "limit_amount"
            }:
                raise BusinessValidationError("Некорректный бюджет в backup.")
            _short_text(item["category"], "Категория")
            normalize_currency(item["currency"])
            _month(item["month"], date(2000, 1, 1))
            normalize_amount(item["limit_amount"])
        for item in data["templates"]:
            expected = {"name", "schedule", "created_at", *(
                "kind", "amount", "currency", "category", "counterparty",
                "note", "project", "payment_status"
            )}
            if not isinstance(item, dict) or set(item) != expected:
                raise BusinessValidationError("Некорректный шаблон в backup.")
            _short_text(item["name"], "Название шаблона")
            if item["schedule"] not in SCHEDULES:
                raise BusinessValidationError("Некорректный период шаблона в backup.")
            fields = {name: item[name] for name in BUSINESS_FIELDS
                      if name != "occurred_on" and item.get(name) is not None}
            fields["occurred_on"] = "2000-01-01"
            validate_complete(fields, date(2000, 1, 1))
            if not isinstance(item["created_at"], str) or not TIMESTAMP_PATTERN.fullmatch(item["created_at"]):
                raise BusinessValidationError("Некорректное время шаблона в backup.")
        for item in data["aliases"]:
            if not isinstance(item, dict) or set(item) != {"alias", "category"}:
                raise BusinessValidationError("Некорректный псевдоним в backup.")
            _short_text(item["alias"], "Псевдоним")
            _short_text(item["category"], "Категория")
        if any(len(data[name]) > 5000 for name in ("budgets", "templates", "aliases")):
            raise BusinessValidationError("Backup содержит слишком много данных.")

    def restore_backup(self, filename: str) -> None:
        data = self._read_backup(filename)
        with self.store._connect() as db:
            for table in ("business_drafts", "business_operations", "business_budgets",
                          "business_templates", "business_category_aliases"):
                db.execute(f"DELETE FROM {table} WHERE session=?", (self.store.session,))
            for item in data["operations"]:
                db.execute("""
                    INSERT INTO business_operations(
                        id, session, kind, amount, currency, occurred_on, category,
                        counterparty, note, project, payment_status, created_at, deleted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (item["id"], self.store.session,
                      *(item[name] for name in BUSINESS_FIELDS), item["created_at"], item["deleted_at"]))
            for item in data["budgets"]:
                db.execute("""
                    INSERT INTO business_budgets(
                        session, category, currency, month, limit_amount, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (self.store.session, item["category"], item["currency"], item["month"],
                      item["limit_amount"], self._timestamp()))
            for item in data["templates"]:
                db.execute("""
                    INSERT INTO business_templates(
                        session, name, schedule, kind, amount, currency, category,
                        counterparty, note, project, payment_status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (self.store.session, *(item[name] for name in (
                    "name", "schedule", "kind", "amount", "currency", "category",
                    "counterparty", "note", "project", "payment_status", "created_at"
                ))))
            for item in data["aliases"]:
                db.execute("""
                    INSERT INTO business_category_aliases(session, alias, category)
                    VALUES (?, ?, ?)
                """, (self.store.session, item["alias"], item["category"]))
            self.store._audit(db, "backup_restored", "backup", filename)
