"""Настройки читаются только из окружения; .env загружает точка входа."""

from dataclasses import dataclass, field
import os
import re


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    allowed_user_id: int
    reasoning_effort: str = "low"

    @classmethod
    def from_env(cls):
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise ValueError("Задайте TELEGRAM_BOT_TOKEN в окружении или 04_telegram_agent/.env.")
        raw_id = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()
        if not raw_id.isascii() or not raw_id.isdecimal() or int(raw_id) <= 0:
            raise ValueError("TELEGRAM_ALLOWED_USER_ID должен быть положительным числовым user ID.")
        if not os.getenv("OPENAI_API_KEY", "").strip():
            raise ValueError("Задайте OPENAI_API_KEY в окружении или 04_telegram_agent/.env.")
        effort = os.getenv("OPENAI_REASONING_EFFORT", "low").strip()
        if effort not in {"minimal", "low", "medium", "high"}:
            raise ValueError("OPENAI_REASONING_EFFORT: допустимы minimal, low, medium, high.")
        return cls(token, int(raw_id), effort)
