"""Запустить API-пример целиком: сервис, клиент и проверку результата."""

import os
import secrets

from api_client import ApiClient, ApiError
from local_service import running_service


def main() -> int:
    # При отсутствии настройки создаём временный токен, не записывая его на диск.
    token = os.getenv("API_LAB_TOKEN") or secrets.token_urlsafe(24)
    try:
        with running_service(token) as url:
            client = ApiClient(url, token)
            initial = client.request("GET", "/tasks")
            print(f"GET /tasks: записей {len(initial.data['tasks'])}")

            created = client.request("POST", "/tasks", {"title": "Проверить отчёт салона"})
            print(f"POST /tasks: HTTP {created.status}, создана задача {created.data['id']}")
            saved = client.request("GET", "/tasks")
            if created.data not in saved.data["tasks"]:
                raise ApiError("Проверка записи не прошла.")
            print(f"GET /tasks: запись подтверждена — {created.data['title']}")

            for path in ("/demo/rate-limit", "/demo/unavailable"):
                response = client.request("GET", path)
                print(f"GET {path}: HTTP {response.status}, попыток {response.attempts}")

            try:
                ApiClient(url, token + "-wrong").request("GET", "/tasks")
            except ApiError as error:
                if error.status != 401:
                    raise
                print(f"Неверный токен: HTTP 401, попыток {error.attempts}")

            try:
                ApiClient(url, token, timeout=0.03).request("GET", "/demo/slow")
            except ApiError as error:
                if error.status is not None or error.attempts != 3:
                    raise
                print("Медленный ответ: таймаут, остановка после 3 попыток.")
            else:
                raise ApiError("Ожидаемый таймаут не произошёл.")
        print("Пример завершён. Локальный сервис остановлен; учебные записи удалены из памяти.")
        return 0
    except (ApiError, ValueError, OSError) as error:
        message = str(error) if isinstance(error, (ApiError, ValueError)) else "Ошибка локального сервиса."
        print(f"Ошибка: {message}")
        return 1
    except KeyboardInterrupt:
        print("\nПример остановлен.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
