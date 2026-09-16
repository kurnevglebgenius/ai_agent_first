"""Real local HTTP tests and deterministic client retry/error checks."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.client import IncompleteRead
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError


SOURCE = Path(__file__).resolve().parents[1] / "02_web_api"


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, SOURCE / filename)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: module}):
        spec.loader.exec_module(module)
    return module


api = load_module("lab_api_client", "api_client.py")
service = load_module("lab_local_service", "local_service.py")


class Response(io.BytesIO):
    status = 200


def http_error(status, headers=None):
    return HTTPError("https://example.test/tasks", status, "test", headers or {}, io.BytesIO(b"private"))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = api.ApiClient("https://example.test", "test-placeholder")

    def test_get_auth_json_and_timeout(self):
        with patch.object(self.client.opener, "open", return_value=Response(b'{"tasks": []}')) as call:
            response = self.client.request("GET", "/tasks")
        self.assertEqual((response.status, response.data, response.attempts), (200, {"tasks": []}, 1))
        request = call.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-placeholder")
        self.assertEqual(call.call_args.kwargs["timeout"], 2.0)

    def test_transient_get_failure_retries_with_backoff(self):
        with patch.object(self.client.opener, "open", side_effect=[
            URLError("private details"), http_error(503), Response(b'{"ok": true}')
        ]) as call:
            with patch.object(api, "sleep") as sleep:
                response = self.client.request("GET", "/tasks")
        self.assertEqual(response.attempts, 3)
        self.assertEqual(call.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.1, 0.2])

    def test_retry_limit_stops_after_three_attempts(self):
        with patch.object(self.client.opener, "open", side_effect=TimeoutError) as call:
            with patch.object(api, "sleep") as sleep:
                with self.assertRaises(api.ApiError) as caught:
                    self.client.request("GET", "/tasks")
        self.assertEqual(caught.exception.attempts, 3)
        self.assertEqual(call.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_post_is_not_retried_on_uncertain_or_http_failure(self):
        for failure in [TimeoutError(), IncompleteRead(b"partial"), http_error(503), http_error(429)]:
            with self.subTest(failure=type(failure).__name__):
                with patch.object(self.client.opener, "open", side_effect=failure) as call:
                    with patch.object(api, "sleep") as sleep:
                        with self.assertRaises(api.ApiError) as caught:
                            self.client.request("POST", "/tasks", {"title": "demo"})
                self.assertEqual(call.call_count, 1)
                self.assertEqual(caught.exception.attempts, 1)
                sleep.assert_not_called()

    def test_permanent_errors_and_redirects_are_not_retried(self):
        for status in (301, 302, 307, 400, 401, 403, 404):
            with self.subTest(status=status):
                with patch.object(self.client.opener, "open", side_effect=http_error(status)) as call:
                    with self.assertRaises(api.ApiError) as caught:
                        self.client.request("GET", "/tasks")
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(call.call_count, 1)
                self.assertNotIn("private", str(caught.exception))

    def test_retry_after_seconds_are_respected(self):
        with patch.object(self.client.opener, "open", side_effect=[
            http_error(429, {"Retry-After": "2"}), Response(b"{}")
        ]):
            with patch.object(api, "sleep") as sleep:
                self.assertEqual(self.client.request("GET", "/tasks").attempts, 2)
        sleep.assert_called_once_with(2.0)

    def test_retry_after_date_invalid_and_excessive_wait(self):
        past = format_datetime(datetime.now(timezone.utc) - timedelta(seconds=5), usegmt=True)
        self.assertEqual(api.retry_delay(past, 1), 0)
        self.assertEqual(api.retry_delay("nonsense", 2), 0.2)
        with patch.object(self.client.opener, "open", side_effect=http_error(429, {"Retry-After": "60"})) as call:
            with patch.object(api, "sleep") as sleep:
                with self.assertRaises(api.ApiError):
                    self.client.request("GET", "/tasks")
        self.assertEqual(call.call_count, 1)
        sleep.assert_not_called()

    def test_bad_or_large_json_is_not_retried(self):
        for raw in (b"<html>", b"[]", b"\xff", b"x" * (api.MAX_BODY + 1)):
            with self.subTest(raw=raw[:20]):
                with patch.object(self.client.opener, "open", return_value=Response(raw)) as call:
                    with self.assertRaises(api.ApiError):
                        self.client.request("GET", "/tasks")
                self.assertEqual(call.call_count, 1)

    def test_invalid_settings_and_paths(self):
        for url in ("http://example.test", "https://user:pass@example.test", "file:///tmp",
                    "https://example.test/path", "https://example.test?key=secret",
                    "https://example.test:bad"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    api.ApiClient(url, "test-placeholder")
        for token in ("", "line\nbreak", "has space", "токен"):
            with self.assertRaises(ValueError):
                api.ApiClient("https://example.test", token)
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                api.ApiClient("https://example.test", "test-placeholder", timeout=timeout)
        for path in ("https://other.test", "//other.test", "/tasks\n"):
            with self.assertRaises(ValueError):
                self.client.request("GET", path)


class LocalServiceTests(unittest.TestCase):
    def setUp(self):
        context = service.running_service("local-test-placeholder")
        self.url = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.client = api.ApiClient(self.url, "local-test-placeholder")

    def test_post_is_visible_in_following_get(self):
        self.assertEqual(self.client.request("GET", "/tasks").data, {"tasks": []})
        created = self.client.request("POST", "/tasks", {"title": " Проверить отчёт "})
        self.assertEqual(created.status, 201)
        self.assertEqual(created.data, {"id": 1, "title": "Проверить отчёт"})
        self.assertEqual(self.client.request("GET", "/tasks").data["tasks"], [created.data])

    def test_auth_and_validation_do_not_create_records(self):
        wrong = api.ApiClient(self.url, "wrong-placeholder")
        with self.assertRaises(api.ApiError) as caught:
            wrong.request("POST", "/tasks", {"title": "not allowed"})
        self.assertEqual(caught.exception.status, 401)
        for data in ({"title": ""}, {"title": 42}, {"title": "x" * 201}, {"unknown": "value"}):
            with self.assertRaises(api.ApiError) as caught:
                self.client.request("POST", "/tasks", data)
            self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.client.request("GET", "/tasks").data["tasks"], [])

    def test_real_rate_limit_and_unavailable_recover(self):
        for path in ("/demo/rate-limit", "/demo/unavailable"):
            with patch.object(api, "sleep") as sleep:
                response = self.client.request("GET", path)
            self.assertEqual(response.attempts, 2)
            self.assertTrue(response.data["recovered"])
            sleep.assert_called_once_with(1.0)

    def test_unknown_route_is_404(self):
        with self.assertRaises(api.ApiError) as caught:
            self.client.request("GET", "/missing")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.attempts, 1)

    def test_real_timeout_is_bounded_by_retry_count(self):
        client = api.ApiClient(self.url, "local-test-placeholder", timeout=0.02)
        with patch.object(api, "sleep"):
            with self.assertRaises(api.ApiError) as caught:
                client.request("GET", "/demo/slow")
        self.assertEqual(caught.exception.attempts, 3)


if __name__ == "__main__":
    unittest.main()
