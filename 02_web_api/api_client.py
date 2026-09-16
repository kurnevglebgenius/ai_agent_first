"""Небольшой JSON-клиент: авторизация, timeout и ограниченные повторы GET."""

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import json
import math
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


MAX_BODY = 65536


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None, attempts: int = 0):
        super().__init__(message)
        self.status = status
        self.attempts = attempts


@dataclass(frozen=True)
class ApiResponse:
    status: int
    data: dict
    attempts: int


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Не пересылаем Authorization на адрес из ответа сервера.
        return None


def retry_delay(header: str | None, attempt: int) -> float:
    """Retry-After: секунды или HTTP-date; иначе небольшая возрастающая пауза."""
    fallback = 0.1 * (2 ** (attempt - 1))
    if header is None:
        return fallback
    try:
        if header.strip().isdigit():
            delay = float(header)
        else:
            date = parsedate_to_datetime(header)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return fallback
    if not math.isfinite(delay) or delay > 5:
        # Не игнорируем указание сервера, повторяя раньше разрешённого времени.
        raise ApiError("Сервис просит ждать больше 5 секунд. Повторите позднее.")
    return delay


class ApiClient:
    def __init__(self, base_url: str, token: str, timeout: float = 2.0):
        try:
            parts = urlsplit(base_url)
            valid = (
                parts.hostname and not parts.username and not parts.password
                and not parts.query and not parts.fragment and parts.path in ("", "/")
                and (parts.scheme == "https" or
                     (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1")))
            )
            parts.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Нужен HTTPS-адрес сервиса или HTTP на loopback без пути и учётных данных.")
        if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise ValueError("Токен должен быть непустым ASCII-текстом без пробелов.")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Таймаут должен быть положительным конечным числом.")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        handlers = [NoRedirects()]
        if parts.hostname in ("127.0.0.1", "localhost", "::1"):
            handlers.append(ProxyHandler({}))
        self.opener = build_opener(*handlers)

    def request(self, method: str, path: str, data: dict | None = None) -> ApiResponse:
        if method not in ("GET", "POST"):
            raise ValueError("В примере поддерживаются только GET и POST.")
        if not path.startswith("/") or path.startswith("//") or any(c in path for c in "\r\n"):
            raise ValueError("Укажите относительный путь вида /tasks.")
        if method == "GET" and data is not None:
            raise ValueError("GET в этом примере не отправляет тело.")
        body = None if data is None else json.dumps(data, allow_nan=False).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.base_url + path, data=body, headers=headers, method=method)
        # POST не повторяем: сервер мог создать запись до обрыва соединения.
        limit = 3 if method == "GET" else 1
        for attempt in range(1, limit + 1):
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    status = response.status
                    raw = response.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raise ApiError("Ответ превышает 64 КиБ.", status, attempt)
                try:
                    result = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise ApiError("Ответ не является корректным JSON.", status, attempt) from None
                if not isinstance(result, dict):
                    raise ApiError("Ожидается JSON-объект.", status, attempt)
                return ApiResponse(status, result, attempt)
            except HTTPError as error:
                status = error.code
                retry_after = error.headers.get("Retry-After")
                error.close()
                if status not in (429, 502, 503, 504) or attempt == limit:
                    raise ApiError(f"Запрос завершился HTTP {status}.", status, attempt) from None
                try:
                    delay = retry_delay(retry_after, attempt)
                except ApiError:
                    raise ApiError("Сервис просит длительную паузу. Повторите позднее.", status, attempt) from None
            except (URLError, OSError, HTTPException):
                if attempt == limit:
                    message = "Сеть недоступна или истёк таймаут."
                    if method == "POST":
                        message += " Результат записи неизвестен; автоматически POST не повторялся."
                    raise ApiError(message, attempts=attempt) from None
                delay = retry_delay(None, attempt)
            sleep(delay)
        raise AssertionError("Недостижимый код")
