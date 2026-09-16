"""Получить две учебные задачи по HTTP, не блокируя цикл asyncio."""

import asyncio
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://jsonplaceholder.typicode.com"
TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 65536


class FetchError(Exception):
    """Понятная ошибка внешнего сервиса."""


def validate_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme in ("http", "https") and parts.hostname
            and parts.username is None and parts.password is None
            and not parts.query and not parts.fragment
        )
        parts.port  # Проверить, что порт корректен, если он задан.
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Нужен адрес HTTP/HTTPS без логина, пароля, query и fragment.")
    return url


def fetch_todo(url: str) -> dict:
    """Обычный блокирующий HTTP GET с таймаутом и проверкой ответа."""
    validate_url(url)
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raise FetchError(f"Сервис вернул HTTP {error.code}.") from None
    except (URLError, OSError):
        raise FetchError("Сеть недоступна или истёк таймаут запроса.") from None

    if len(body) > MAX_RESPONSE_BYTES:
        raise FetchError("Ответ превышает допустимые 64 КиБ.")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        raise FetchError("Сервис вернул некорректный JSON.") from None
    if (
        not isinstance(data, dict)
        or type(data.get("id")) is not int
        or not isinstance(data.get("title"), str)
        or type(data.get("completed")) is not bool
    ):
        raise FetchError("В ответе нужны id (целое), title (строка), completed (bool).")
    return {"id": data["id"], "title": data["title"], "completed": data["completed"]}


async def fetch_pair(base_url: str) -> list:
    # urllib блокирует поток. to_thread переносит ожидание сети в рабочие потоки.
    return await asyncio.gather(
        asyncio.to_thread(fetch_todo, f"{base_url}/todos/1"),
        asyncio.to_thread(fetch_todo, f"{base_url}/todos/2"),
        return_exceptions=True,
    )


async def main() -> int:
    base_url = os.getenv("HTTP_EXAMPLE_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
    try:
        validate_url(base_url)
    except ValueError as error:
        print(f"Ошибка настройки HTTP_EXAMPLE_BASE_URL: {error}")
        return 1

    print("Запрашиваем две учебные задачи одновременно...")
    results = await fetch_pair(base_url)
    failed = False
    for number, result in enumerate(results, start=1):
        if isinstance(result, Exception):
            failed = True
            # Не печатаем сырой ответ сервера или внутренние исключения.
            message = str(result) if isinstance(result, FetchError) else "Не удалось получить данные."
            print(f"Запрос {number}: {message}")
        else:
            status = "выполнена" if result["completed"] else "не выполнена"
            print(f"Задача {result['id']}: {result['title']} — {status}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nОстановка запрошена. Уже начатые сетевые операции могут завершаться до таймаута.")
        raise SystemExit(1)
