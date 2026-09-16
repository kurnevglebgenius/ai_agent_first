"""Публичные справочные API. Только фиксированные HTTPS endpoints и GET."""

from datetime import date
from decimal import Context, Decimal, localcontext
from http.client import HTTPException
import json
import math
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .utility_tools import number, schema, success


ENDPOINTS = {
    "places": "https://geocoding-api.open-meteo.com/v1/search",
    "weather": "https://api.open-meteo.com/v1/forecast",
    "rates": "https://api.frankfurter.dev/v1/latest",
    **{f"wiki_{lang}": f"https://{lang}.wikipedia.org/w/rest.php/v1/search/page"
       for lang in ("ru", "en", "de", "es", "fr")},
}
MAX_RESPONSE_BYTES = 131072


class PublicApiError(Exception):
    """Контролируемое сообщение без адресов запросов и тела внешней ошибки."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PublicClient:
    def __init__(self):
        self.opener = build_opener(NoRedirects())

    def get(self, endpoint, params):
        if endpoint not in ENDPOINTS:
            raise PublicApiError("Сервис не разрешён.")
        request = Request(ENDPOINTS[endpoint] + "?" + urlencode(params), headers={
            "Accept": "application/json", "User-Agent": "AIAgentsLab/1.0 (https://github.com/kurnevglebgenius/ai_agent_first)"}, method="GET")
        try:
            with self.opener.open(request, timeout=8) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise PublicApiError("Ответ сервиса слишком большой.")
            def invalid_constant(value):
                raise ValueError()
            data = json.loads(raw, parse_constant=invalid_constant)
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except HTTPError as error:
            status = error.code
            error.close()
            if status == 429:
                raise PublicApiError("Лимит сервиса исчерпан. Повторите позже.") from None
            raise PublicApiError(f"Справочный сервис вернул HTTP {status}.") from None
        except (URLError, OSError, HTTPException):
            raise PublicApiError("Сервис недоступен или истёк таймаут.") from None
        except (ValueError, UnicodeError, RecursionError):
            raise PublicApiError("Сервис вернул некорректный JSON.") from None


def text_query(value, limit=100):
    value = value.strip()
    if not 2 <= len(value) <= limit or any(ord(char) < 32 for char in value):
        raise ValueError(f"Нужен запрос от 2 до {limit} символов без управляющих символов.")
    return value


def api_number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise PublicApiError("В ответе сервиса отсутствуют корректные числовые данные.")
    return value


def api_text(value, limit=500):
    if not isinstance(value, str) or not value:
        raise PublicApiError("В ответе сервиса отсутствуют текстовые данные.")
    return value[:limit]


def safe(handler):
    def execute(args, message):
        try:
            return handler(args, message)
        except (ValueError, PublicApiError) as error:
            return {"ok": False, "value": None, "error": str(error)}
        except (KeyError, TypeError, IndexError):
            return {"ok": False, "value": None, "error": "Неожиданная структура ответа справочного сервиса."}
    return execute


class PublicTools:
    def __init__(self, client=None):
        self.client = client or PublicClient()

    def find_places(self, args, message):
        query = text_query(args["query"])
        data = self.client.get("places", {"name": query, "count": 5, "language": "ru", "format": "json"})
        rows = data.get("results", [])
        if not isinstance(rows, list):
            raise PublicApiError("Некорректный список мест.")
        places = [{"name": api_text(row["name"]), "country": row.get("country"),
                   "region": row.get("admin1"), "latitude": api_number(row["latitude"]),
                   "longitude": api_number(row["longitude"])} for row in rows[:5]]
        return success({"places": places, "source": "https://open-meteo.com/", "attribution": "Open-Meteo / GeoNames"})

    def get_weather(self, args, message):
        lat, lon = number(args["latitude"]), number(args["longitude"])
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError("Широта должна быть от -90 до 90, долгота от -180 до 180.")
        data = self.client.get("weather", {"latitude": str(lat), "longitude": str(lon),
            "current": "temperature_2m,relative_humidity_2m,wind_speed_10m", "timezone": "UTC"})
        current = data["current"]
        values = {key: api_number(current[key]) for key in (
            "temperature_2m", "relative_humidity_2m", "wind_speed_10m")}
        return success({"time_utc": api_text(current["time"]), "current": values,
                        "units": {key: api_text(data["current_units"][key]) for key in values},
                        "source": "https://open-meteo.com/", "kind": "model_current_conditions"})

    def convert_currency(self, args, message):
        amount = number(args["amount"])
        base, target = args["from_currency"].upper(), args["to_currency"].upper()
        if amount < 0 or not all(re.fullmatch(r"[A-Z]{3}", code) for code in (base, target)):
            raise ValueError("Нужна неотрицательная сумма и трёхбуквенные коды валют, например EUR и USD.")
        if base == target:
            raise ValueError("Выберите разные валюты.")
        data = self.client.get("rates", {"base": base, "symbols": target})
        if data["base"] != base:
            raise PublicApiError("Сервис вернул другую базовую валюту.")
        rate = Decimal(str(api_number(data["rates"][target])))
        if rate <= 0:
            raise PublicApiError("Сервис вернул неверный курс.")
        rate_date = api_text(data["date"])
        try:
            date.fromisoformat(rate_date)
        except ValueError:
            raise PublicApiError("Сервис вернул неверную дату курса.") from None
        with localcontext(Context(prec=40)):
            converted = amount * rate
        return success({"amount": str(amount), "from_currency": base, "to_currency": target,
                        "converted": format(converted, "f"), "rate": str(rate), "date": rate_date,
                        "source": "https://frankfurter.dev/", "kind": "reference_rate_not_bank_quote"})

    def search_wikipedia(self, args, message):
        query = text_query(args["query"], 200)
        language = args["language"]
        if language not in ("ru", "en", "de", "es", "fr"):
            raise ValueError("Поддерживаются языки ru, en, de, es, fr.")
        data = self.client.get(f"wiki_{language}", {"q": query, "limit": 5})
        rows = data["pages"]
        if not isinstance(rows, list):
            raise PublicApiError("Некорректный список статей.")
        return success({"articles": [{"title": api_text(row["title"]),
            "description": api_text(row["description"]) if row.get("description") else None,
            "url": f"https://{language}.wikipedia.org/wiki/" + quote(api_text(row["key"]), safe="")}
            for row in rows[:5]], "source": f"https://{language}.wikipedia.org/"})

    def definitions(self):
        return [
            (schema("find_places", "Найти до 5 городов и координаты через Open-Meteo. При неоднозначности уточни выбор.",
                    {"query": "Город и при необходимости страна, 2–100 символов"}), safe(self.find_places)),
            (schema("get_weather", "Текущая модельная погода Open-Meteo по известным координатам; не прогноз по дням.",
                    {"latitude": "Широта от -90 до 90", "longitude": "Долгота от -180 до 180"}), safe(self.get_weather)),
            (schema("convert_currency", "Пересчитать сумму по последнему справочному курсу Frankfurter с датой. Не банковская котировка; не все валюты поддерживаются.",
                    {"amount": "Неотрицательное десятичное число", "from_currency": "Исходная валюта, например EUR",
                     "to_currency": "Целевая валюта, например USD"}), safe(self.convert_currency)),
            (schema("search_wikipedia", "Найти до 5 статей Википедии с описаниями и ссылками. Это поиск статей, не чтение полного текста.",
                    {"query": "Поисковый запрос, 2–200 символов", "language": "ru, en, de, es или fr"}), safe(self.search_wikipedia)),
        ]
