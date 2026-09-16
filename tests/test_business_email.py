"""Offline-проверки read-only подключения и анализа деловой почты."""

from datetime import date, datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock

from shared.agent_runtime.business import BusinessAgent, BusinessStore
from shared.agent_runtime.business_email import (
    BusinessEmail,
    BusinessEmailService,
    EmailConfig,
    IMAPEmailSource,
    OpenAIEmailAnalyzer,
    OpenAIEmailReplyDrafter,
    SMTPEmailSender,
)
from shared.agent_runtime.model_provider import ModelResponseError


TODAY = date(2026, 9, 15)


class BusinessEmailTests(unittest.TestCase):
    def test_config_is_optional_strict_and_hides_password(self):
        self.assertIsNone(EmailConfig.from_env({}))
        config = EmailConfig.from_env({
            "BUSINESS_EMAIL_ENABLED": "true",
            "BUSINESS_EMAIL_IMAP_HOST": "imap.example.com",
            "BUSINESS_EMAIL_USERNAME": "owner@example.com",
            "BUSINESS_EMAIL_APP_PASSWORD": "private-app-password",
        })
        self.assertEqual((config.host, config.port, config.max_messages),
                         ("imap.example.com", 993, 30))
        self.assertNotIn("private-app-password", repr(config))
        with self.assertRaises(ValueError):
            EmailConfig.from_env({"BUSINESS_EMAIL_ENABLED": "true",
                                  "BUSINESS_EMAIL_IMAP_HOST": "https://bad.example"})

    def test_imap_uses_readonly_peek_and_extracts_text_without_attachment(self):
        raw = (
            b"From: Client <client@example.com>\r\n"
            b"Subject: New order\r\nDate: Tue, 15 Sep 2026 10:00:00 +0300\r\n"
            b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n"
            b"--x\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Please send an invoice.\r\n--x\r\n"
            b"Content-Type: text/plain\r\nContent-Disposition: attachment; filename=secret.txt\r\n\r\n"
            b"ATTACHMENT SECRET\r\n--x--\r\n"
        )
        client = Mock()
        client.login.return_value = ("OK", [b"logged"])
        client.select.return_value = ("OK", [b"1"])
        client.search.return_value = ("OK", [b"7"])
        client.fetch.return_value = ("OK", [(b"7 BODY[]", raw)])
        factory = Mock(return_value=client)
        config = EmailConfig("imap.example.com", 993, "owner", "app-password")
        source = IMAPEmailSource(config, client_factory=factory, today=lambda: TODAY)

        messages = source.fetch_recent(7)

        factory.assert_called_once_with("imap.example.com", 993, timeout=30)
        client.select.assert_called_once_with("INBOX", readonly=True)
        client.search.assert_called_once_with(None, "SINCE", "09-Sep-2026")
        client.fetch.assert_called_once_with(b"7", "(BODY.PEEK[])")
        client.logout.assert_called_once()
        self.assertEqual(messages[0].subject, "New order")
        self.assertIn("send an invoice", messages[0].snippet)
        self.assertNotIn("ATTACHMENT SECRET", messages[0].snippet)

    def test_analyzer_forces_strict_structure_and_service_renders_sections(self):
        data = {
            "summary": "Есть новый запрос клиента.",
            "urgent_actions": ["Ответить по теме New order сегодня"],
            "sales_leads": ["Client — запрос стоимости"],
            "payments_and_invoices": ["Подготовить счёт Client"],
            "risks": [],
        }
        sdk = Mock()
        sdk.responses.create.return_value = SimpleNamespace(
            status="completed", output=[SimpleNamespace(
                type="function_call", name="analyze_business_email",
                arguments=json.dumps(data, ensure_ascii=False),
            )],
        )
        analyzer = OpenAIEmailAnalyzer(sdk, reasoning_effort="low")
        source = Mock()
        source.fetch_recent.return_value = [BusinessEmail(
            "7", "Client", "New order", "2026-09-15T10:00+03:00", "Нужен счёт"
        )]
        report = BusinessEmailService(source, analyzer, today=lambda: TODAY).report(7)
        self.assertIn("Срочно:\n- Ответить", report)
        self.assertIn("Лиды:", report)
        request = sdk.responses.create.call_args.kwargs
        self.assertTrue(request["tools"][0]["strict"])
        self.assertFalse(request["parallel_tool_calls"])
        self.assertEqual(request["reasoning"], {"effort": "low"})
        self.assertNotIn("app-password", request["input"])

    def test_bad_model_output_and_agent_commands_are_safe(self):
        sdk = Mock()
        sdk.responses.create.return_value = SimpleNamespace(
            status="completed", output=[SimpleNamespace(
                type="function_call", name="analyze_business_email",
                arguments='{"summary":"x","summary":"y"}',
            )],
        )
        with self.assertRaises(ModelResponseError):
            OpenAIEmailAnalyzer(sdk).analyze([
                BusinessEmail("1", "a", "b", "c", "d")
            ], today=TODAY)

        base = Mock(has_context=False)
        base.memory_command.return_value = None
        extractor = Mock()
        with self.subTest("disabled"):
            agent = BusinessAgent(base, Mock(spec=BusinessStore), extractor)
            self.assertIn("не подключена", agent.reply("/email-report"))
            extractor.extract.assert_not_called()

        service = Mock()
        service.report.return_value = "готовая сводка"
        agent = BusinessAgent(base, Mock(spec=BusinessStore), extractor,
                              email_service=service)
        self.assertEqual(agent.reply("/email-report 14"), "готовая сводка")
        service.report.assert_called_once_with(14)
        self.assertIn("от 1 до 30", agent.reply("/email-report 31"))

    def test_reply_draft_is_bound_to_real_message_and_requires_one_decision(self):
        source = Mock()
        source.fetch_recent.return_value = [BusinessEmail(
            "7", "Client <client@example.com>", "New order",
            "2026-09-15T10:00+03:00", "Нужна стоимость",
            "<message-7@example.com>", "client@example.com",
        )]
        drafter = Mock()
        drafter.draft.return_value = (
            "7", "Здравствуйте! Уточните, пожалуйста, объём работ.", "Новый запрос клиента"
        )
        sender = Mock()
        service = BusinessEmailService(
            source, Mock(), reply_drafter=drafter, sender=sender,
            today=lambda: TODAY,
            clock=lambda: datetime(2026, 9, 15, 12, tzinfo=timezone.utc),
            token_factory=lambda: "safe-token-123456",
        )
        suggestion = service.suggest_reply(7)
        self.assertIn("Кому: client@example.com", suggestion)
        self.assertIn("Отправить этот текст?", suggestion)
        self.assertEqual(service.pending_reply.token, "safe-token-123456")

        self.assertIn("отменена", service.decide_reply("safe-token-123456", False))
        sender.send.assert_not_called()
        self.assertIn("недоступен", service.decide_reply("safe-token-123456", True))

        service.suggest_reply(7)
        self.assertIn("отправлено", service.decide_reply("safe-token-123456", True))
        sender.send.assert_called_once()
        sent = sender.send.call_args.args[0]
        self.assertEqual((sent.recipient, sent.in_reply_to),
                         ("client@example.com", "<message-7@example.com>"))
        self.assertIn("недоступен", service.decide_reply("safe-token-123456", True))
        sender.send.assert_called_once()

    def test_reply_drafter_rejects_unknown_uid_and_smtp_uses_reply_headers(self):
        sdk = Mock()
        sdk.responses.create.return_value = SimpleNamespace(
            status="completed", output=[SimpleNamespace(
                type="function_call", name="draft_business_email_reply",
                arguments=json.dumps({
                    "action": "draft", "selected_uid": "999",
                    "body": "Ответ", "rationale": "Причина",
                }, ensure_ascii=False),
            )],
        )
        with self.assertRaises(ModelResponseError):
            OpenAIEmailReplyDrafter(sdk).draft([
                BusinessEmail("7", "Client", "Order", "date", "text")
            ], today=TODAY)

        smtp = MagicMock()
        connection = smtp.return_value.__enter__.return_value
        config = EmailConfig(
            "imap.example.com", 993, "owner@example.com", "app-password",
            smtp_host="smtp.example.com",
        )
        from shared.agent_runtime.business_email import PendingEmailReply
        reply = PendingEmailReply(
            "safe-token-123456", "client@example.com", "Re: Order", "Ответ", "Причина",
            "<original@example.com>", datetime(2026, 9, 15, 13, tzinfo=timezone.utc),
        )
        SMTPEmailSender(config, client_factory=smtp).send(reply)
        smtp.assert_called_once_with("smtp.example.com", 465, timeout=30)
        connection.login.assert_called_once_with("owner@example.com", "app-password")
        message = connection.send_message.call_args.args[0]
        self.assertEqual(message["To"], "client@example.com")
        self.assertEqual(message["In-Reply-To"], "<original@example.com>")


if __name__ == "__main__":
    unittest.main()
