"""Учебный HTTP-сервис, доступный только на этом компьютере."""

from contextlib import contextmanager
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time


@contextmanager
def running_service(token: str):
    tasks = []
    counts = {}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(2)

        def log_message(self, format, *args):
            # Не выводим заголовки, токены или содержимое запросов.
            pass

        def respond(self, status, data, retry_after=None):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                if retry_after is not None:
                    self.send_header("Retry-After", str(retry_after))
                self.end_headers()
                self.wfile.write(body)
            except (ConnectionError, TimeoutError):
                pass  # Клиент мог уже завершить запрос по таймауту.

        def authorized(self):
            actual = self.headers.get("Authorization", "").encode("utf-8")
            expected = f"Bearer {token}".encode("utf-8")
            if not hmac.compare_digest(actual, expected):
                self.respond(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            if self.path == "/tasks":
                with lock:
                    snapshot = list(tasks)
                self.respond(200, {"tasks": snapshot})
                return
            if self.path in ("/demo/rate-limit", "/demo/unavailable"):
                with lock:
                    counts[self.path] = counts.get(self.path, 0) + 1
                    count = counts[self.path]
                if count == 1:
                    status = 429 if self.path.endswith("rate-limit") else 503
                    self.respond(status, {"error": "try later"}, retry_after=1)
                else:
                    self.respond(200, {"recovered": True})
                return
            if self.path == "/demo/slow":
                time.sleep(0.2)
                self.respond(200, {"ready": True})
                return
            self.respond(404, {"error": "not found"})

        def do_POST(self):
            if not self.authorized():
                return
            if self.path != "/tasks":
                self.respond(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    self.respond(413, {"error": "invalid body size"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self.respond(415, {"error": "expected JSON"})
                    return
                data = json.loads(self.rfile.read(length))
                if (
                    not isinstance(data, dict) or set(data) != {"title"}
                    or not isinstance(data["title"], str)
                    or not 1 <= len(data["title"].strip()) <= 200
                ):
                    raise ValueError
            except (ValueError, UnicodeError, OSError):
                self.respond(400, {"error": "invalid task"})
                return
            with lock:
                task = {"id": len(tasks) + 1, "title": data["title"].strip()}
                tasks.append(task)
            self.respond(201, task)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
