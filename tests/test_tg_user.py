# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api as api
import app.tg_user as tg_user
from app.journal import Journal
from app.tg_user import (
    TelegramUser,
    TelegramUserConfig,
    TelegramUserError,
    load_telegram_user,
    login,
    reply_draft,
)


class FakeClient:
    def __init__(self, dialogs, authorized=True):
        self.dialogs = dialogs
        self.authorized = authorized
        self.calls: list[str] = []
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_me(self):
        self.calls.append("get_me")
        return SimpleNamespace(username="demo_leader", first_name="Иван", last_name="Волков")

    async def get_dialogs(self, limit: int = 200):
        self.calls.append(f"get_dialogs:{limit}")
        return self.dialogs

    async def get_messages(self, entity, limit: int = 20):
        self.calls.append(f"get_messages:{entity.name}:{limit}")
        return entity.messages[:limit]


def _msg(text, out=False, media=False, action=False, when=None):
    return SimpleNamespace(
        message=text,
        out=out,
        media=SimpleNamespace(id=1) if media else None,
        action=SimpleNamespace(id=1) if action else None,
        date=when or datetime(2026, 11, 27, 9, 30),
    )


def _dialog(name, unread, messages, group=False, channel=False, username="", mentions=0):
    entity = SimpleNamespace(name=name, username=username, messages=messages)
    return SimpleNamespace(
        id=f"id-{name}",
        name=name,
        entity=entity,
        unread_count=unread,
        unread_mentions_count=mentions,
        is_group=group,
        is_channel=channel,
    )


def _dialogs():
    return [
        _dialog(
            "Анна Петрова",
            3,
            [_msg("Можно созвон завтра в 11?"), _msg("Отправила файл", out=True)],
            username="anna",
        ),
        _dialog("Команда", 5, [_msg("ок, принято", out=True), _msg("Спасибо!")], group=True),
        _dialog("Канал новостей", 0, [_msg("дайджест")], channel=True),
    ]


def _configured(settings, tmp_path: Path) -> TelegramUserConfig:
    session = tmp_path / "telegram_user.session"
    session.write_bytes(b"session")
    settings.env["TELEGRAM_API_ID"] = "12345"
    settings.env["TELEGRAM_API_HASH"] = "hash-should-not-leak"
    settings.env["TELEGRAM_SESSION"] = str(session)
    return load_telegram_user(settings)


def test_public_config_hides_api_hash(settings, tmp_path: Path) -> None:
    config = _configured(settings, tmp_path)
    assert config.configured is True
    assert config.authorized is True
    public = config.public
    assert public["mode"] == "user"
    assert public["session_set"] is True
    assert "hash-should-not-leak" not in str(public)
    assert "hash-should-not-leak" not in repr(config)


def test_unconfigured_and_unauthorized_are_reported(settings, tmp_path: Path) -> None:
    with pytest.raises(TelegramUserError) as excinfo:
        TelegramUser(load_telegram_user(settings)).unread()
    assert "not configured" in str(excinfo.value)

    settings.env["TELEGRAM_API_ID"] = "12345"
    settings.env["TELEGRAM_API_HASH"] = "hash"
    settings.env["TELEGRAM_SESSION"] = str(tmp_path / "missing.session")
    user = TelegramUser(load_telegram_user(settings))
    assert user.check()["ok"] is False
    with pytest.raises(TelegramUserError) as excinfo:
        user.unread()
    assert "not authorized" in str(excinfo.value)


def test_login_requires_keys(settings) -> None:
    with pytest.raises(TelegramUserError) as excinfo:
        login(settings)
    assert "TELEGRAM_API_ID" in str(excinfo.value)


def test_unread_totals_previews_and_no_read_marking(settings, tmp_path: Path) -> None:
    config = _configured(settings, tmp_path)
    client = FakeClient(_dialogs())
    user = TelegramUser(config, client_factory=lambda _cfg: client)
    data = user.unread(limit=20)

    assert data["total"] == 8
    assert data["dialogs"] == 2
    assert [item["name"] for item in data["items"]] == ["Анна Петрова", "Команда"]
    anna = data["items"][0]
    assert anna["kind"] == "user" and anna["username"] == "anna"
    assert anna["last_text"] == "Можно созвон завтра в 11?"
    assert anna["outgoing"] is False
    group = data["items"][1]
    assert group["kind"] == "group"
    assert group["last_text"] == "Спасибо!"
    assert group["last_at"] == "2026-11-27T09:30:00"

    assert all("mark" not in call and "read" not in call for call in client.calls)
    assert "get_messages:Анна Петрова:3" in client.calls
    assert client.connected is False


def test_unread_period_filters_by_message_date(settings, tmp_path: Path) -> None:
    config = _configured(settings, tmp_path)
    now = datetime.now()
    client = FakeClient(
        [
            _dialog(
                "Анна Петрова",
                2,
                [
                    _msg("Вчерашний вопрос", when=now - timedelta(days=1)),
                    _msg("Давнее письмо", when=now - timedelta(days=12)),
                ],
            ),
            _dialog(
                "Команда",
                1,
                [_msg("Сегодня в работе", when=now.replace(hour=12, minute=0, second=0, microsecond=0))],
                group=True,
            ),
        ]
    )
    user = TelegramUser(config, client_factory=lambda _cfg: client)

    today = user.unread(limit=20, period="today")
    assert today["total"] == 1 and today["dialogs"] == 1
    assert today["items"][0]["name"] == "Команда"
    assert today["period"] == "today" and today["period_title"] == "за сегодня"

    week = user.unread(limit=20, period="7d")
    assert week["total"] == 2 and week["dialogs"] == 2
    anna = next(item for item in week["items"] if item["name"] == "Анна Петрова")
    assert anna["unread"] == 1 and anna["last_text"] == "Вчерашний вопрос"

    everything = user.unread(limit=20, period="all")
    assert everything["total"] == 3
    anna_all = next(item for item in everything["items"] if item["name"] == "Анна Петрова")
    assert anna_all["unread"] == 2

    drafts = user.drafts(limit=5, period="7d")
    assert drafts["total"] == 2
    assert len(drafts["drafts"]) == 2
    assert drafts["period_title"] == "за 7 дней"


def test_drafts_have_replies_based_on_last_incoming(settings, tmp_path: Path) -> None:
    config = _configured(settings, tmp_path)
    client = FakeClient(_dialogs())
    user = TelegramUser(config, client_factory=lambda _cfg: client)
    data = user.drafts(limit=5)

    assert data["total"] == 8
    assert len(data["drafts"]) == 2
    anna = data["drafts"][0]
    assert anna["basis"] == "Можно созвон завтра в 11?"
    assert anna["reply"].startswith("Добрый день, Анна!")
    assert "созвон" in anna["reply"].lower()
    assert anna["note"] == "Отправка — вручную из Telegram."


def test_reply_draft_templates() -> None:
    assert "Спасибо большое" in reply_draft("Мария", "user", "Поздравляю с праздником!")
    assert "Вижу вопрос" in reply_draft("Олег", "user", "Когда отчёт?")
    assert "Пожалуйста" in reply_draft("Олег", "user", "Спасибо за помощь")
    assert reply_draft("", "group", "привет").startswith("Добрый день!")


def test_unauthorized_session_fails_on_call(settings, tmp_path: Path) -> None:
    config = _configured(settings, tmp_path)
    client = FakeClient(_dialogs(), authorized=False)
    user = TelegramUser(config, client_factory=lambda _cfg: client)
    with pytest.raises(TelegramUserError) as excinfo:
        user.unread()
    assert "not authorized" in str(excinfo.value)
    assert client.connected is False


def test_api_status_unread_and_drafts(settings, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(api, "settings", settings)
    monkeypatch.setattr(tg_user, "_make_client", lambda _cfg: FakeClient(_dialogs()))

    status = api.telegram_user_status()
    assert status == {
        "configured": False,
        "authorized": False,
        "session_set": False,
        "username": "",
        "mode": "",
    }
    with pytest.raises(HTTPException) as excinfo:
        api.telegram_user_unread()
    assert excinfo.value.status_code == 409

    _configured(settings, tmp_path)
    assert api.telegram_user_status()["authorized"] is True
    unread = api.telegram_user_unread(limit=20)
    assert unread["total"] == 8
    drafts = api.telegram_user_drafts(limit=5)
    assert len(drafts["drafts"]) == 2

    journal = Journal(settings.db_path)
    try:
        events = journal.events(limit=10)
    finally:
        journal.close()
    event = next(item for item in events if item["kind"] == "tg_drafts")
    assert event["payload"] == {"dialogs": 2, "drafts": 2}
    assert "Спасибо" not in str(event)
