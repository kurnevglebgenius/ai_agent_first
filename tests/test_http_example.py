"""Offline HTTP validation, failures and concurrent execution."""

import asyncio
import importlib.util
import io
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError


SOURCE = Path(__file__).resolve().parents[1] / "01_python_basics" / "http_example.py"
spec = importlib.util.spec_from_file_location("http_example", SOURCE)
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)

TODO = {"id": 1, "title": "Учебная задача", "completed": False}


class HttpExampleTests(unittest.TestCase):
    def test_get_parses_json_and_sets_timeout(self):
        with patch.object(example, "urlopen", return_value=io.BytesIO(json.dumps(TODO).encode())) as call:
            self.assertEqual(example.fetch_todo("https://example.test/todos/1"), TODO)
        request = call.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(call.call_args.kwargs["timeout"], 10)

    def test_http_network_and_timeout_errors(self):
        for failure, message in [
            (HTTPError("https://example.test", 429, "rate limit", {}, None), "HTTP 429"),
            (HTTPError("https://example.test", 404, "missing", {}, None), "HTTP 404"),
            (URLError("offline"), "Сеть"),
            (TimeoutError(), "таймаут"),
        ]:
            with self.subTest(message=message):
                with patch.object(example, "urlopen", side_effect=failure):
                    with self.assertRaisesRegex(example.FetchError, message):
                        example.fetch_todo("https://example.test/todos/1")

    def test_invalid_and_oversized_responses(self):
        for body in [
            b"not json", b"\xff", b"[]" , b"{}",
            json.dumps({**TODO, "id": True}).encode(),
            json.dumps({**TODO, "completed": "false"}).encode(),
            json.dumps({**TODO, "title": 12}).encode(),
            b"x" * (example.MAX_RESPONSE_BYTES + 1),
        ]:
            with self.subTest(body=body[:60]):
                with patch.object(example, "urlopen", return_value=io.BytesIO(body)):
                    with self.assertRaises(example.FetchError):
                        example.fetch_todo("https://example.test/todos/1")

    def test_invalid_url_is_rejected_before_network(self):
        for url in ["file:///tmp/data", "", "https://", "https://user:secret@example.test",
                    "https://example.test?key=value", "https://example.test/#fragment",
                    "https://example.test:bad"]:
            with self.subTest(url=url):
                with patch.object(example, "urlopen") as call:
                    with self.assertRaises(ValueError):
                        example.fetch_todo(url)
                    call.assert_not_called()

    def test_pair_runs_in_parallel_worker_threads(self):
        barrier = threading.Barrier(2, timeout=5)
        main_thread = threading.get_ident()

        def fake_fetch(url):
            self.assertNotEqual(threading.get_ident(), main_thread)
            barrier.wait()
            return {**TODO, "id": int(url.rsplit("/", 1)[1])}

        with patch.object(example, "fetch_todo", side_effect=fake_fetch):
            results = asyncio.run(example.fetch_pair("https://example.test"))
        self.assertEqual([result["id"] for result in results], [1, 2])

    def test_one_failure_does_not_hide_other_result(self):
        def fake_fetch(url):
            if url.endswith("/1"):
                raise example.FetchError("HTTP 503")
            return {**TODO, "id": 2}

        with patch.dict(example.os.environ, {"HTTP_EXAMPLE_BASE_URL": "https://example.test"}):
            with patch.object(example, "fetch_todo", side_effect=fake_fetch):
                with patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(asyncio.run(example.main()), 1)
        self.assertIn("HTTP 503", output.getvalue())
        self.assertIn("Задача 2", output.getvalue())

    def test_environment_overrides_default_url(self):
        with patch.dict(example.os.environ, {"HTTP_EXAMPLE_BASE_URL": "https://example.test/"}):
            with patch.object(example, "fetch_todo", return_value=TODO) as call:
                with patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(asyncio.run(example.main()), 0)
        self.assertEqual(
            {item.args[0] for item in call.call_args_list},
            {"https://example.test/todos/1", "https://example.test/todos/2"},
        )

    def test_bad_configuration_does_not_start_requests(self):
        with patch.dict(example.os.environ, {"HTTP_EXAMPLE_BASE_URL": ""}):
            with patch.object(example, "fetch_todo") as call:
                with patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(asyncio.run(example.main()), 1)
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
