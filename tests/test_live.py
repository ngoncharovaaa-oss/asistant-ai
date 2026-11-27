# -*- coding: utf-8 -*-
"""Живые каналы без сети: фейковые IMAP, SMTP и Telegram Bot API."""
from __future__ import annotations

import email as mailparse
import json
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, parsedate_to_datetime

import pytest
from fastapi import HTTPException

import app.api as api
from app.journal import Journal
from app.live import (
    ChannelError,
    Channels,
    approve_draft,
    channels_status,
    mailboxes_overview,
    run_live,
    telegram_listen,
)
from app.mailboxes import MailboxError, MailboxStore

PASSWORD = "mail-secret"
TOKEN = "123456:telegram-secret"


def raw_mail(
    subject: str,
    body: str,
    sender: str = "client@example.com",
    name: str = "Клиент",
    date: str = "Mon, 07 Dec 2026 09:00:00 +0300",
) -> bytes:
    msg = EmailMessage()
    msg["From"] = f"{name} <{sender}>"
    msg["To"] = "leader@example.com"
    msg["Subject"] = subject
    msg["Date"] = date
    msg.set_content(body)
    return msg.as_bytes()


def _imap_day(value: str) -> datetime:
    return datetime.strptime(value, "%d-%b-%Y")


def _mail_day(raw: bytes) -> datetime:
    parsed = parsedate_to_datetime(mailparse.message_from_bytes(raw).get("Date", ""))
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _today_stamp() -> str:
    return format_datetime(datetime.now().astimezone())


class FakeIMAP:
    fetch_specs: list[str] = []

    def __init__(self, host: str, port: int, messages: list[bytes], unseen: set[str] | None = None):
        self.host, self.port = host, port
        self.messages = messages
        self.unseen = unseen

    def login(self, user: str, password: str) -> None:
        if password != PASSWORD:
            raise RuntimeError("bad password")

    def select(self, mailbox: str, readonly: bool = False):
        return "OK", [b"1"]

    def uid(self, command: str, *args):
        if command == "search":
            criteria = [str(arg).upper() for arg in args if isinstance(arg, str)]
            uids = [str(i + 1) for i in range(len(self.messages))]
            if "UNSEEN" in criteria and self.unseen is not None:
                uids = [uid for uid in uids if uid in self.unseen]
            if "SINCE" in criteria:
                since = None
                for index, arg in enumerate(args):
                    if isinstance(arg, str) and arg.upper() == "SINCE" and index + 1 < len(args):
                        since = _imap_day(args[index + 1])
                if since:
                    uids = [uid for uid in uids if _mail_day(self.messages[int(uid) - 1]) >= since]
            return "OK", [b" ".join(uid.encode() for uid in uids)]
        if command == "fetch":
            FakeIMAP.fetch_specs.append(str(args[1]) if len(args) > 1 else "")
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


def registry_mailbox(settings, address: str = "team@example.org"):
    store = MailboxStore(settings.db_path.parent / "mailboxes.json")
    return store.add(address, PASSWORD, provider="mailru", from_name="Руководитель")


def registry_channels(settings, messages: list[bytes], unseen: set[str] | None = None):
    return Channels(
        settings,
        imap_factory=lambda host, port: FakeIMAP(host, port, messages, unseen=unseen),
        smtp_factory=FakeSMTP,
        urlopen=FakeTelegram(),
    )


def test_mailbox_store_masks_password_and_dedupes(settings) -> None:
    store = MailboxStore(settings.db_path.parent / "mailboxes.json")
    mb = store.add("Team@Example.org", PASSWORD, provider="mailru")
    assert mb.address == "team@example.org"
    assert mb.imap_host == "imap.mail.ru"
    assert mb.smtp_host == "smtp.mail.ru"
    assert mb.smtp_port == 465
    public = json.dumps(mb.public(), ensure_ascii=False)
    assert PASSWORD not in public
    assert mb.public()["password_set"] is True
    with pytest.raises(MailboxError):
        store.add("team@example.org", PASSWORD, provider="mailru")
    with pytest.raises(MailboxError):
        store.add("не-почта", PASSWORD)
    with pytest.raises(MailboxError):
        store.add("other@example.org", "", provider="mailru")
    assert store.remove(mb.id) is True
    assert store.list() == []


def test_unread_counts_today_and_peek_preview(settings) -> None:
    FakeIMAP.fetch_specs = []
    registry_mailbox(settings)
    messages = [
        raw_mail("Старый запрос", "Письмо без ответа.", date="Mon, 07 Dec 2020 09:00:00 +0300"),
        raw_mail("Срочно: согласование", "Нужен ответ сегодня.", date=_today_stamp()),
    ]
    ch = registry_channels(settings, messages, unseen={"1", "2"})
    label = next(iter(ch.accounts))
    data = ch.unread(label, limit=5)
    assert data["total"] == 2
    assert data["today"] == 1
    assert data["messages"][0]["subject"] == "Срочно: согласование"
    assert data["messages"][0]["today"] is True
    assert data["messages"][1]["today"] is False
    assert FakeIMAP.fetch_specs and all("BODY.PEEK[HEADER.FIELDS" in spec for spec in FakeIMAP.fetch_specs)


def test_unread_period_filters_and_overview(settings) -> None:
    registry_mailbox(settings)
    now = datetime.now()

    def when(days_ago: int) -> str:
        return (now - timedelta(days=days_ago)).strftime("%a, %d %b %Y %H:%M:%S +0300")

    messages = [
        raw_mail("Старый запрос", "Письмо без ответа.", date=when(40)),
        raw_mail("Вчерашнее", "Ответьте, пожалуйста.", date=when(1)),
        raw_mail("Срочно: согласование", "Нужен ответ сегодня.", date=when(0)),
    ]
    ch = registry_channels(settings, messages, unseen={"1", "2", "3"})
    label = next(iter(ch.accounts))

    today = ch.unread(label, limit=5, period="today")
    assert today["total"] == 1 and today["today"] == 1
    assert today["period"] == "today" and today["period_title"] == "за сегодня"

    week = ch.unread(label, limit=5, period="7d")
    assert week["total"] == 2
    assert [m["subject"] for m in week["messages"]] == ["Срочно: согласование", "Вчерашнее"]

    everything = ch.unread(label, limit=5, period="all")
    assert everything["total"] == 3 and everything["today"] == 1
    assert everything["period"] == "all"

    data = mailboxes_overview(settings, with_unread=True, channels=ch, period="3d")
    assert data["period"] == "3d"
    assert data["unread"] == 2
    assert data["unread_today"] == 1
    assert [item["id"] for item in data["periods"]] == ["today", "3d", "7d", "30d", "all"]


def test_overview_shows_unread_without_secrets(settings) -> None:
    mb = registry_mailbox(settings)
    messages = [
        raw_mail("Старый запрос", "Письмо без ответа.", date="Mon, 07 Dec 2020 09:00:00 +0300"),
        raw_mail("Срочно: согласование", "Нужен ответ сегодня.", date=_today_stamp()),
    ]
    ch = registry_channels(settings, messages, unseen={"1", "2"})
    data = mailboxes_overview(settings, with_unread=True, channels=ch)
    payload = json.dumps(data, ensure_ascii=False)
    assert PASSWORD not in payload
    assert PASSWORD not in repr(ch.accounts)
    assert data["unread"] == 2
    assert data["unread_today"] == 1
    item = next(entry for entry in data["mailboxes"] if entry["id"] == mb.id)
    assert item["source"] == "registry" and item["removable"] is True
    assert item["provider"] == "mailru"
    assert "gmail" in data["providers"] and data["providers"]["gmail"]["smtp_port"] == 465


def test_live_drafts_from_unread_only(settings) -> None:
    mb = registry_mailbox(settings)
    messages = [
        raw_mail("Подтвердите участие во встрече", "Подтвердите участие, пожалуйста."),
        raw_mail("Счёт на оплату", "Пришлите счёт за ноябрь."),
    ]
    ch = registry_channels(settings, messages, unseen={"2"})
    journal = Journal(settings.db_path)
    try:
        result = run_live(settings, journal, channels=ch, labels=[mb.id], unseen_only=True)
        assert result is not None
        assert result.metrics["emails"] == 1
        assert journal.run_dataset() == f"imap:{mb.id}"
        drafts = journal.drafts()
        assert drafts and all(item["email_id"] == f"live-{mb.id}-2" for item in drafts)
        assert journal.seen_uids(mb.id) == {"2"}
    finally:
        journal.close()


def test_api_mailbox_add_list_delete(settings, monkeypatch) -> None:
    monkeypatch.setattr(api, "settings", settings)
    monkeypatch.setattr(api, "_mailbox_login_check", lambda mb_id: {"ok": False, "detail": "нет сети в тесте"})
    added = api.mailbox_add(
        api.MailboxRequest(
            address="Lead@Example.com",
            password="app-password",
            provider="gmail",
            from_name="Руководитель",
        )
    )
    assert added["ok"] is True
    assert added["mailbox"]["address"] == "lead@example.com"
    assert added["mailbox"]["password_set"] is True
    listed = api.mailboxes()
    payload = json.dumps(listed, ensure_ascii=False)
    assert "app-password" not in payload
    assert listed["mailboxes"][0]["provider_title"] == "Gmail"
    mb_id = listed["mailboxes"][0]["id"]
    assert api.mailbox_delete(mb_id)["ok"] is True
    with pytest.raises(HTTPException):
        api.mailbox_delete(mb_id)
    with pytest.raises(HTTPException):
        api.mailbox_add(api.MailboxRequest(address="bad", password="x", provider="gmail"))


def test_login_hint_explains_app_password() -> None:
    from app.live import MailAccount, _imap_login_hint

    gmail = MailAccount(
        label="g", address="a@gmail.com", imap_host="imap.gmail.com", imap_port=993,
        smtp_host="smtp.gmail.com", smtp_port=465, user="a@gmail.com",
    )
    hint = _imap_login_hint(gmail, RuntimeError("b'[AUTHENTICATIONFAILED] Invalid credentials (Failure)'"))
    assert "Gmail" in hint and "пароль приложения" in hint
    mail = MailAccount(
        label="m", address="a@example.org", imap_host="imap.mail.ru", imap_port=993,
        smtp_host="smtp.mail.ru", smtp_port=465, user="a@example.org",
    )
    hint = _imap_login_hint(mail, RuntimeError("NEOBHODIM parol prilozheniya"))
    assert "Mail.ru" in hint and "внешних приложений" in hint


def test_mailbox_password_can_be_replaced(settings) -> None:
    store = MailboxStore(settings.db_path.parent / "mailboxes.json")
    mb = store.add("lead@example.com", "old-secret", provider="gmail")
    with pytest.raises(MailboxError):
        store.update_password(mb.id, "   ")
    updated = store.update_password(mb.id, "new-app-secret")
    assert updated is not None and updated.id == mb.id
    kept = store.get(mb.id)
    assert kept is not None and kept.password == "new-app-secret"
    assert "new-app-secret" not in json.dumps(kept.public(), ensure_ascii=False)
    assert store.update_password("mb-missing", "x") is None


def test_api_mailbox_password_update(settings, monkeypatch) -> None:
    monkeypatch.setattr(api, "settings", settings)
    monkeypatch.setattr(api, "_mailbox_login_check", lambda mb_id: {"ok": False, "detail": "Gmail не принял пароль"})
    added = api.mailbox_add(
        api.MailboxRequest(address="lead@example.com", password="old", provider="gmail")
    )
    assert added["check"]["ok"] is False
    mb_id = added["mailbox"]["id"]
    result = api.mailbox_password(mb_id, api.MailboxPasswordRequest(password="new-app-secret"))
    assert result["ok"] is True
    assert result["check"]["ok"] is False
    assert "new-app-secret" not in json.dumps(result, ensure_ascii=False)
    store = MailboxStore(settings.db_path.parent / "mailboxes.json")
    stored = store.get(mb_id)
    assert stored is not None and stored.password == "new-app-secret"
    with pytest.raises(HTTPException):
        api.mailbox_password("mb-missing", api.MailboxPasswordRequest(password="x"))
