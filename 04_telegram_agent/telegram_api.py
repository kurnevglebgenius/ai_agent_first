"""Минимальный Telegram Bot API transport на стандартной библиотеке."""

import json
from http.client import HTTPSConnection
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, Request, build_opener


def connect_ipv4(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    """Только транспорт Telegram; глобальный DNS и соединения LLM не меняем."""
    host, port = address
    addresses = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    last_error = None
    for _, _, _, _, target in addresses:
        try:
            return socket.create_connection(target, timeout, source_address)
        except OSError as error:
            last_error = error
    if last_error is not None:
        raise last_error
    raise OSError("No IPv4 address available")


class IPv4HTTPSConnection(HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = connect_ipv4


class IPv4HTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        # Исходный hostname остаётся в HTTPSConnection: SNI и проверка TLS сохранены.
        return self.do_open(IPv4HTTPSConnection, request, context=self._context)


class TelegramError(Exception):
    def __init__(self, code=0, retry_after=0):
        # Не сохраняем URL, response body или исходное исключение с токеном.
        super().__init__("Telegram request failed")
        self.code = code
        self.retry_after = retry_after


class TelegramAPI:
    def __init__(self, token):
        self._base_url = f"https://api.telegram.org/bot{token}/"
        self._opener = build_opener(IPv4HTTPSHandler())

    def call(self, method, **payload):
        request = Request(
            self._base_url + method,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=40) as response:
                result = json.load(response)
        except HTTPError as error:
            code = error.code
            try:
                result = json.load(error)
                delay = result.get("parameters", {}).get("retry_after", 0)
            except (ValueError, OSError, AttributeError):
                delay = 0
            finally:
                error.close()
            raise TelegramError(code, delay) from None
        except (URLError, OSError, ValueError):
            raise TelegramError() from None
        if not isinstance(result, dict):
            raise TelegramError()
        if not result.get("ok"):
            raise TelegramError(result.get("error_code", 0),
                                result.get("parameters", {}).get("retry_after", 0))
        return result["result"]

    def get_updates(self, offset):
        return self.call(
            "getUpdates", offset=offset, timeout=25,
            allowed_updates=["message", "callback_query"],
        )

    def send_text(self, chat_id, text, *, reply_markup=None):
        # 2000 Unicode codepoints укладываются также в 4096 UTF-16 units.
        # Plain text: ответ модели не интерпретируется как HTML/Markdown.
        for start in range(0, len(text), 2000):
            chunk = text[start:start + 2000]
            for attempt in range(3):
                try:
                    payload = {"chat_id": chat_id, "text": chunk}
                    if reply_markup is not None and start + 2000 >= len(text):
                        payload["reply_markup"] = reply_markup
                    self.call("sendMessage", **payload)
                    break
                except TelegramError as error:
                    # Только явный отказ 429 можно повторить без риска дубля.
                    if error.code != 429 or attempt == 2:
                        raise
                    time.sleep(max(1, error.retry_after))

    def send_chat_action(self, chat_id, action="typing"):
        return self.call("sendChatAction", chat_id=chat_id, action=action)

    def set_my_commands(self, commands):
        return self.call("setMyCommands", commands=commands)

    def answer_callback(self, callback_query_id):
        self.call("answerCallbackQuery", callback_query_id=callback_query_id)

    def clear_reply_buttons(self, chat_id, message_id):
        self.call(
            "editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
            reply_markup={"inline_keyboard": []},
        )
