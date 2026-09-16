"""Сетевые границы и справочные инструменты без реальных запросов."""

import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.agent_runtime.public_tools import PublicClient, PublicApiError, PublicTools, NoRedirects, MAX_RESPONSE_BYTES
from shared.agent_runtime.tools import default_tools
from shared.agent_runtime.tool_bridge import CalculatorDispatcher
from shared.agent_runtime.model_provider import ToolCall


class PublicTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.handlers = {schema["name"]: handler for schema, handler in PublicTools(self.client).definitions()}

    def run_tool(self, name, **args):
        output = self.handlers[name](args, "private user message")
        self.assertEqual(set(output), {"ok", "value", "error"})
        return output

    def test_places_return_choices_and_no_results(self):
        self.client.get.return_value = {"results": [{"name": "Минск", "country": "Беларусь", "latitude": 53.9, "longitude": 27.56}]}
        result = self.run_tool("find_places", query="Минск")
        self.assertEqual(result["value"]["places"][0]["latitude"], 53.9)
        self.assertNotIn("private", str(self.client.get.call_args))
        self.client.get.return_value = {}
        self.assertEqual(self.run_tool("find_places", query="unknown")["value"]["places"], [])

    def test_weather_preserves_units_and_time(self):
        self.client.get.return_value = {"current": {"time": "2026-09-14T12:00", "temperature_2m": 18,
            "relative_humidity_2m": 70, "wind_speed_10m": 10}, "current_units": {
            "temperature_2m": "°C", "relative_humidity_2m": "%", "wind_speed_10m": "km/h"}}
        result = self.run_tool("get_weather", latitude="53.9", longitude="27.56")
        self.assertTrue(result["ok"])
        self.assertEqual(result["value"]["units"]["temperature_2m"], "°C")
        self.assertEqual(self.client.get.call_args.args[1]["timezone"], "UTC")

    def test_currency_preserves_date_and_decimal_result(self):
        self.client.get.return_value = {"base": "EUR", "date": "2026-09-11", "rates": {"USD": 1.15}}
        result = self.run_tool("convert_currency", amount="100", from_currency="eur", to_currency="usd")
        self.assertEqual(result["value"]["converted"], "115.00")
        self.assertEqual(result["value"]["date"], "2026-09-11")
        self.client.get.return_value["base"] = "GBP"
        self.assertFalse(self.run_tool("convert_currency", amount="100", from_currency="EUR", to_currency="USD")["ok"])

    def test_wikipedia_urls_cannot_change_host(self):
        self.client.get.return_value = {"pages": [{"title": "Article", "key": "//evil.example/a?b", "description": None}]}
        result = self.run_tool("search_wikipedia", query="Python", language="en")
        self.assertEqual(result["value"]["articles"][0]["url"], "https://en.wikipedia.org/wiki/%2F%2Fevil.example%2Fa%3Fb")

    def test_invalid_input_never_calls_network(self):
        cases = [("find_places", {"query": " "}), ("find_places", {"query": "a" * 101}),
            ("get_weather", {"latitude": "91", "longitude": "0"}),
            ("get_weather", {"latitude": "NaN", "longitude": "0"}),
            ("convert_currency", {"amount": "-1", "from_currency": "EUR", "to_currency": "USD"}),
            ("convert_currency", {"amount": "1", "from_currency": "../", "to_currency": "USD"}),
            ("search_wikipedia", {"query": "Python", "language": "localhost"})]
        for name, args in cases:
            self.assertFalse(self.run_tool(name, **args)["ok"])
        self.client.get.assert_not_called()

    def test_bad_response_is_reported_without_raw_body(self):
        self.client.get.return_value = {"private-secret": "internal"}
        result = self.run_tool("get_weather", latitude="0", longitude="0")
        self.assertFalse(result["ok"])
        self.assertNotIn("private-secret", result["error"])
        self.client.get.return_value = {"results": [{"name": "City", "latitude": float("nan"), "longitude": 1}]}
        self.assertFalse(self.run_tool("find_places", query="City")["ok"])

    def test_network_can_be_disabled_in_registry(self):
        tools = default_tools(CalculatorDispatcher(), None, allow_public_network=False)
        for name in self.handlers:
            self.assertNotIn(name, [schema["name"] for schema in tools.schemas()])
            self.assertFalse(json.loads(tools.run(ToolCall("c1", name, "{}")))["ok"])


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = PublicClient()
        self.client.opener = Mock()

    def response(self, body):
        response = Mock()
        response.read.return_value = body
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        self.client.opener.open.return_value = context
        return response

    def test_fixed_https_get_timeout_and_encoding(self):
        self.response(b'{"pages": []}')
        self.client.get("wiki_en", {"q": "a&url=http://localhost", "limit": 5})
        request = self.client.opener.open.call_args.args[0]
        self.assertTrue(request.full_url.startswith("https://en.wikipedia.org/w/rest.php/v1/search/page?q=a%26url%3D"))
        self.assertEqual(request.get_method(), "GET")
        self.assertFalse(request.has_header("Authorization"))
        self.assertEqual(self.client.opener.open.call_args.kwargs["timeout"], 8)
        with self.assertRaises(PublicApiError):
            self.client.get("http://localhost", {})

    def test_invalid_and_oversize_json(self):
        for body in (b"<html>private</html>", b"[]", b'{"x":NaN}', b"x" * (MAX_RESPONSE_BYTES + 1)):
            response = self.response(body)
            with self.assertRaises(PublicApiError):
                self.client.get("rates", {})
            response.read.assert_called_once_with(MAX_RESPONSE_BYTES + 1)

    def test_http_and_network_errors_are_safe_and_not_retried(self):
        for error in (HTTPError("private-url", 429, "private", {}, io.BytesIO()),
                      HTTPError("private-url", 302, "private", {}, io.BytesIO()), URLError("private")):
            self.client.opener.open.reset_mock()
            self.client.opener.open.side_effect = error
            with self.assertRaises(PublicApiError) as caught:
                self.client.get("rates", {})
            self.assertNotIn("private", str(caught.exception))
            self.client.opener.open.assert_called_once()
        self.assertIsNone(NoRedirects().redirect_request(None, None, 302, "", {}, "http://localhost"))


if __name__ == "__main__":
    unittest.main()
