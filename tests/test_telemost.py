# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.api as api
from app.journal import Journal
from app.live import Channels, channels_status
from app.telemost import TelemostError

ROOM = "https://telemost.yandex.ru/j/12345678901234"


def test_link_meeting_returns_room_and_invite(settings) -> None:
    settings.env["TELEMOST_LINK"] = ROOM
    channels = Channels(settings)
    result = channels.telemost.create_meeting("Планёрка", "2026-11-28 10:00")
    assert result["mode"] == "link"
    assert result["platform"] == "telemost"
    assert result["join_url"] == ROOM
    assert result["start"] == "2026-11-28T10:00:00"
    assert result["invite"].splitlines() == ["Планёрка", "2026-11-28 10:00", ROOM]


def test_link_without_scheme_is_normalized(settings) -> None:
    settings.env["TELEMOST_LINK"] = "telemost.yandex.ru/j/12345678901234"
    status = channels_status(settings, check=True)
    assert status["telemost"]["configured"] is True
    assert status["telemost"]["ok"] is True
    assert status["telemost_ready"] is True


def test_foreign_link_is_invalid_and_unconfigured(settings) -> None:
    settings.env["TELEMOST_LINK"] = "https://zoom.us/j/123"
    channel = Channels(settings).telemost
    assert channel.config.configured is False
    assert channel.config.public["invalid"] is True
    assert channel.check()["ok"] is False
    with pytest.raises(TelemostError):
        channel.create_meeting("Тема")


def test_bad_start_time_is_rejected(settings) -> None:
    settings.env["TELEMOST_LINK"] = ROOM
    with pytest.raises(TelemostError) as excinfo:
        Channels(settings).telemost.create_meeting("Тема", "завтра утром")
    assert "start time" in str(excinfo.value)


def test_api_telemost_meeting_logs_and_maps_errors(settings, monkeypatch) -> None:
    monkeypatch.setattr(api, "settings", settings)
    with pytest.raises(HTTPException) as excinfo:
        api.telemost_meeting(api.TelemostRequest(topic="Планёрка"))
    assert excinfo.value.status_code == 409

    settings.env["TELEMOST_LINK"] = ROOM
    result = api.telemost_meeting(api.TelemostRequest(topic="Планёрка", start="2026-11-28 10:00"))
    assert result["join_url"] == ROOM
    journal = Journal(settings.db_path)
    try:
        events = journal.events(limit=10)
    finally:
        journal.close()
    event = next(item for item in events if item["kind"] == "telemost_link")
    assert event["payload"]["topic"] == "Планёрка"
    assert event["payload"]["mode"] == "link"

    with pytest.raises(HTTPException) as excinfo:
        api.telemost_meeting(api.TelemostRequest(topic="Планёрка", start="после обеда"))
    assert excinfo.value.status_code == 400
