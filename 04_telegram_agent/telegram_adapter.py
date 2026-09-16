"""Telegram-события → команды интерфейса или существующий Agent Runtime."""

import logging
import re
import time

from command_catalog import START_TEXT, render_help, telegram_menu_commands
from telegram_api import TelegramError
from shared.agent_runtime.business import BUSINESS_COMMANDS, canonical_business_command


logger = logging.getLogger("telegram_agent")
HELP = render_help()


class TelegramAdapter:
    def __init__(self, api, agent, allowed_user_id):
        self.api = api
        self.agent = agent
        self.allowed_user_id = allowed_user_id
        self.offset = 0

    def handle_update(self, update):
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            self._handle_email_callback(callback)
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        sender = message.get("from", {})
        chat = message.get("chat", {})
        # Проверяем ДО любой команды, ответа, контекста или вызова LLM.
        if (sender.get("id") != self.allowed_user_id or sender.get("is_bot")
                or chat.get("type") != "private" or chat.get("id") != self.allowed_user_id):
            logger.warning("access_denied")
            return
        text = message.get("text")
        command = ""
        if not isinstance(text, str):
            # Будущая точка расширения для voice/photo/document.
            logger.info("unsupported_message")
            answer = "Пока поддерживается только текст. Отправьте текстовое сообщение."
        else:
            text = text.strip()
            command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
            if command == "/start":
                answer = START_TEXT
            elif command == "/help":
                answer = render_help(text)
            elif command == "/status":
                try:
                    context = "есть" if self.agent.has_context else "пуст"
                    answer = f"Бот работает. Runtime: FirstAgent. Контекст: {context}. Доступность LLM не проверялась."
                except Exception:
                    logger.error("memory_error")
                    answer = "Не удалось прочитать локальную память."
            elif command == "/reset":
                try:
                    self.agent.reset()
                    answer = (
                        "Контекст и бизнес-черновик сброшены. Сохранённые факты и операции остаются. "
                        "Данные у провайдера не удаляются."
                    )
                except Exception:
                    logger.error("memory_error")
                    answer = "Не удалось сбросить локальный диалог. Попробуйте ещё раз."
            elif command in ("/remember", "/memory", "/forget"):
                try:
                    answer = self.agent.memory_command(text)
                except Exception:
                    logger.error("memory_error")
                    answer = "Не удалось обратиться к локальной памяти. Попробуйте ещё раз."
            elif command in BUSINESS_COMMANDS:
                try:
                    answer = self.agent.reply(text)
                except Exception:
                    logger.error("business_error")
                    answer = "Не удалось обратиться к локальным бизнес-данным. Попробуйте ещё раз."
            elif command.startswith("/"):
                answer = "Неизвестная команда. Используйте /help."
            elif not text:
                answer = "Отправьте непустой текст."
            else:
                logger.info("agent_request")
                try:
                    self.api.send_chat_action(chat["id"])
                except TelegramError:
                    # Индикатор — только улучшение интерфейса, его сбой не отменяет ответ.
                    logger.warning("typing_indicator_failed")
                started = time.perf_counter()
                try:
                    answer = self.agent.reply(text)
                    logger.info("agent_response duration_s=%.2f", time.perf_counter() - started)
                except Exception:
                    # Не выводим str(exc), traceback, запросы или ответы модели.
                    logger.error("agent_error duration_s=%.2f", time.perf_counter() - started)
                    answer = "Не удалось получить ответ агента. Попробуйте ещё раз или используйте /reset."
            if command.startswith("/"):
                logger.info("command_handled")
        started = time.perf_counter()
        try:
            reply_markup = self._reply_markup(command)
            if reply_markup is None:
                self.api.send_text(chat["id"], answer)
            else:
                self.api.send_text(chat["id"], answer, reply_markup=reply_markup)
        finally:
            logger.info("delivery_finished duration_s=%.2f", time.perf_counter() - started)
        logger.info("response_sent")

    def _reply_markup(self, command):
        if canonical_business_command(command) != "/email-reply":
            return None
        service = getattr(self.agent, "email_service", None)
        pending = getattr(service, "pending_reply", None)
        token = getattr(pending, "token", None)
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{12,40}", token):
            return None
        return {"inline_keyboard": [[
            {"text": "✅ Отправить", "callback_data": f"email:send:{token}"},
            {"text": "❌ Не отправлять", "callback_data": f"email:cancel:{token}"},
        ]]}

    def _handle_email_callback(self, callback):
        sender = callback.get("from", {})
        message = callback.get("message", {})
        chat = message.get("chat", {}) if isinstance(message, dict) else {}
        data = callback.get("data")
        callback_id = callback.get("id")
        if (sender.get("id") != self.allowed_user_id or sender.get("is_bot")
                or chat.get("type") != "private" or chat.get("id") != self.allowed_user_id
                or not isinstance(callback_id, str)):
            logger.warning("access_denied")
            return
        match = re.fullmatch(r"email:(send|cancel):([A-Za-z0-9_-]{12,40})", data or "")
        if match is None:
            self.api.answer_callback(callback_id)
            return
        self.api.answer_callback(callback_id)
        try:
            answer = self.agent.email_decision(match.group(2), match.group(1) == "send")
        except Exception:
            logger.error("email_decision_error")
            answer = "Не удалось обработать решение. Письмо не было повторно отправлено."
        message_id = message.get("message_id")
        if isinstance(message_id, int):
            try:
                self.api.clear_reply_buttons(chat["id"], message_id)
            except TelegramError:
                logger.warning("email_buttons_clear_failed")
        self.api.send_text(chat["id"], answer)
        logger.info("email_decision_handled")

    def run(self):
        try:
            self.api.set_my_commands(telegram_menu_commands())
            logger.info("command_menu_ready")
        except TelegramError:
            # Меню — удобство интерфейса; бот продолжает работать и без него.
            logger.warning("command_menu_failed")
        logger.info("polling_started")
        while True:
            try:
                updates = self.api.get_updates(self.offset)
            except TelegramError as error:
                if error.code in (401, 404, 409):
                    raise
                logger.warning("polling_retry")
                time.sleep(max(5, error.retry_after))
                continue
            for update in updates:
                update_id = update["update_id"]
                if update_id < self.offset:
                    continue
                try:
                    self.handle_update(update)
                except TelegramError as error:
                    if error.code in (401, 404, 409):
                        raise
                    logger.error("delivery_failed")
                except Exception:
                    logger.error("update_failed")
                # Не повторяем LLM/tools при неудаче отправки ответа.
                self.offset = update_id + 1
