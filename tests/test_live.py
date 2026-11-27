# -*- coding: utf-8 -*-
"""Живые каналы без сети: фейковые IMAP, SMTP и Telegram Bot API."""
from __future__ import annotations

import json
from email.message import EmailMessage

import pytest

from app.journal import Journal
from app.live import (
    ChannelError,
    Channels,
    approve_draft,
    channels_status,
    telegram_listen,
)

PASSWORD = "mail-secret"
TOKEN = "123456:telegram-secret"


def raw_mail(subject: str, body: str, sender: str = "client@example.com", name: str = "Клиент") -> bytes:
    msg = EmailMessage()
    msg["From"] = f"{name} <{sender}>"
    msg["To"] = "leader@example.com"
    msg["Subject"] = subject
    msg["Date"] = "Mon, 07 Dec 2026 09:00:00 +0300"
    msg.set_content(body)
    return msg.as_bytes()


class FakeIMAP:
    def __init__(self, host: str, port: int, messages: list[bytes]):
        self.host, self.port = host, port
        self.messages = messages

    def login(self, user: str, password: str) -> None:
        if password != PASSWORD:
            raise RuntimeError("bad password")

    def select(self, mailbox: str, readonly: bool = False):
        return "OK", [b"1"]

    def uid(self, command: str, *args):
        if command == "search":
            return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.messages)))]
        if command == "fetch":
            uid = int(args[0])
            return "OK", [(b"1 (BODY[] {0})", self.messages[uid - 1])]
        raise AssertionError(command)

    def logout(self) -> None:
        pass


class FakeSMTP:
    sent: list[EmailMessage] = []

    def __init__(self, host: str, port: int):
        self.host, self.port = host, port

    def login(self, user: str, password: str) -> None:
        if password != PASSWORD:
            raise RuntimeError("bad password")

    def send_message(self, msg: EmailMessage) -> None:
        FakeSMTP.sent.append(msg)

    def quit(self) -> None:
        pass


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeTelegram:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []

    def __call__(self, request, timeout: int = 10):
        method = request.full_url.rsplit("/", 1)[-1]
        payload = json.loads(request.data.decode("utf-8"))
        self.calls.append((method, payload))
        if method == "getMe":
            return FakeResponse({"ok": True, "result": {"username": "asya_bot"}})
        if method == "getUpdates":
            return FakeResponse({"ok": True, "result": self.updates})
        return FakeResponse({"ok": True, "result": {"message_id": 1}})

    def sent_texts(self) -> list[str]:
        return [payload.get("text", "") for method, payload in self.calls if method == "sendMessage"]


def make_channels(settings, messages: list[bytes], telegram: bool = True) -> tuple[Channels, FakeTelegram]:
    settings.env.update(
        {
            "MAIL_A_IMAP_HOST": "imap.example.com",
            "MAIL_A_IMAP_PORT": "993",
            "MAIL_A_SMTP_HOST": "smtp.example.com",
            "MAIL_A_SMTP_PORT": "465",
            "MAIL_A_USER": "leader@example.com",
            "MAIL_A_PASSWORD": PASSWORD,
            "MAIL_A_FROM_NAME": "Руководитель",
            "TELEGRAM_BOT_TOKEN": TOKEN if telegram else "",
            "TELEGRAM_CHAT_ID": "42",
            "TELEGRAM_ALLOWED_CHATS": "42",
        }
    )
    fake_tg = FakeTelegram()
    ch = Channels(
        settings,
        imap_factory=lambda host, port: FakeIMAP(host, port, messages),
        smtp_factory=FakeSMTP,
        urlopen=fake_tg,
    )
    return ch, fake_tg


def live_run(settings, ch: Channels, messages: list[bytes]):
    from app.live import run_live

    journal = Journal(settings.db_path)
    try:
        result = run_live(settings, journal, channels=ch)
        drafts = journal.drafts()
    finally:
        journal.close()
    return result, drafts


def test_fetch_parses_mail_and_skips_seen(settings) -> None:
    messages = [raw_mail("Первый", "текст"), raw_mail("Второй", "ещё текст")]
    ch, _ = make_channels(settings, messages)
    emails, uids = ch.fetch("a", limit=10, seen={"1"})
    assert [email.subject for email in emails] == ["Второй"]
    assert uids == ["2"]
    assert emails[0].from_email == "client@example.com"
    assert emails[0].id == "live-a-2"


def test_live_run_then_smtp_approve(settings) -> None:
    FakeSMTP.sent = []
    messages = [
        raw_mail("Согласуйте бюджет", "Прошу согласовать бюджет на квартал."),
        raw_mail("Подтвердите участие во встрече", "Подтвердите, пожалуйста, участие во встрече."),
    ]
    ch, fake_tg = make_channels(settings, messages)
    result, drafts = live_run(settings, ch, messages)
    assert result is not None
    assert result.metrics["mode"] == "live"
    assert result.metrics["emails"] == 2
    assert result.metrics["auto_sends"] == 0
    assert len(drafts) == 2

    journal = Journal(settings.db_path)
    try:
        assert journal.run_dataset().startswith("imap:")
        decision = next(item for item in drafts if item["kind"] == "decision")
        reply = next(item for item in drafts if item["kind"] == "calendar")
        assert reply["to_email"] == "client@example.com"

        decision_result = approve_draft(settings, journal, decision["id"], channels=ch)
        assert decision_result["sent"] is False
        assert decision_result["note"]
        assert FakeSMTP.sent == []

        result_send = approve_draft(settings, journal, reply["id"], channels=ch)
        assert result_send["sent"] is True
        assert result_send["delivery"]["channel"] == "smtp"
        assert len(FakeSMTP.sent) == 1
        assert FakeSMTP.sent[0]["To"] == "client@example.com"

        events = journal.events(limit=20)
        approve_event = next(event for event in events if event["kind"] == "approve")
        assert approve_event["payload"]["channel"] == "smtp"
        assert "mock_send" not in approve_event["payload"]
        assert PASSWORD not in json.dumps(events, ensure_ascii=False)
        assert TOKEN not in json.dumps(events, ensure_ascii=False)
    finally:
        journal.close()

    assert any("Отправлено" in text for text in fake_tg.sent_texts())

    second, _ = live_run(settings, ch, messages)
    assert second is None


def test_status_check_hides_secrets(settings) -> None:
    ch, _ = make_channels(settings, [raw_mail("Тема", "Текст")])
    status = channels_status(settings, check=True, channels=ch)
    payload = json.dumps(status, ensure_ascii=False)
    assert PASSWORD not in payload
    assert TOKEN not in payload
    assert status["accounts"]["a"]["configured"] is True
    assert status["accounts"]["a"]["ok"] is True
    assert status["accounts"]["a"]["smtp_ok"] is True
    assert status["telegram"]["ok"] is True
    assert status["telegram"]["username"] == "asya_bot"
    assert status["live_ready"] is True


def test_telegram_commands_and_approve_button(settings) -> None:
    FakeSMTP.sent = []
    messages = [raw_mail("Подтвердите участие во встрече", "Подтвердите участие, пожалуйста.")]
    ch, fake_tg = make_channels(settings, messages)
    result, drafts = live_run(settings, ch, messages)
    assert result is not None
    reply = next(item for item in drafts if item["kind"] == "calendar")

    fake_tg.updates = [
        {
            "update_id": 10,
            "message": {"text": "/drafts", "chat": {"id": 42}},
        }
    ]
    journal = Journal(settings.db_path)
    try:
        handled = telegram_listen(settings, journal, channels=ch, once=True)
        assert handled == 1
        send_calls = [payload for method, payload in fake_tg.calls if method == "sendMessage"]
        assert any("inline_keyboard" in payload.get("reply_markup", {}) for payload in send_calls)

        fake_tg.updates = [
            {
                "update_id": 11,
                "callback_query": {
                    "id": "cb1",
                    "data": f"approve:{reply['id']}",
                    "message": {"chat": {"id": 42}},
                },
            }
        ]
        telegram_listen(settings, journal, channels=ch, once=True)
        assert len(FakeSMTP.sent) == 1
        assert any(text == "Отправлено" for text in fake_tg.sent_texts() + [
            payload.get("text", "") for method, payload in fake_tg.calls if method == "answerCallbackQuery"
        ])
    finally:
        journal.close()


def test_telegram_denies_foreign_chat(settings) -> None:
    ch, fake_tg = make_channels(settings, [])
    fake_tg.updates = [
        {
            "update_id": 20,
            "message": {"text": "/status", "chat": {"id": 777}},
        }
    ]
    journal = Journal(settings.db_path)
    try:
        telegram_listen(settings, journal, channels=ch, once=True)
    finally:
        journal.close()
    assert any("Нет доступа" in text for text in fake_tg.sent_texts())


def test_live_requires_mail_config(settings) -> None:
    channel = Channels(settings)
    with pytest.raises(ChannelError):
        channel.fetch("a")
