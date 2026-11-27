# -*- coding: utf-8 -*-
"""Постоянные комнаты Zoom/Телемост: хранение, валидация и API."""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

import app.api as api
from app.links import LinkError, load, set_link
from app.live import Channels


def test_save_and_remove_links(settings) -> None:
    assert set_link(settings, "telemost", "telemost.yandex.ru/j/1234") == "https://telemost.yandex.ru/j/1234"
    assert set_link(settings, "zoom", "https://zoom.us/j/555?pwd=x") == "https://zoom.us/j/555?pwd=x"
    data = load(settings)
    assert data["telemost"].startswith("https://telemost.yandex.ru/j/")
    ch = Channels(settings)
    assert ch.telemost.config.configured is True
    assert ch.zoom.config.mode == "link"

    set_link(settings, "telemost", "")
    assert "telemost" not in load(settings)
    assert Channels(settings).telemost.config.configured is False


def test_rejects_foreign_hosts(settings) -> None:
    with pytest.raises(LinkError):
        set_link(settings, "telemost", "https://zoom.us/j/1")
    with pytest.raises(LinkError):
        set_link(settings, "zoom", "https://example.com/j/1")
    with pytest.raises(LinkError):
        set_link(settings, "zoom", "https://zoom.us/profile")
    with pytest.raises(LinkError):
        set_link(settings, "teams", "https://zoom.us/j/1")


def test_env_link_wins_over_saved(settings) -> None:
    set_link(settings, "telemost", "https://telemost.yandex.ru/j/111")
    set_link(settings, "zoom", "https://zoom.us/j/111")
    settings.env["TELEMOST_LINK"] = "https://telemost.yandex.ru/j/222"
    ch = Channels(settings)
    assert ch.telemost.create_meeting("Тема")["join_url"] == "https://telemost.yandex.ru/j/222"
    assert ch.zoom.create_meeting("Тема")["join_url"] == "https://zoom.us/j/111"


def test_api_saves_link(settings, monkeypatch) -> None:
    monkeypatch.setattr(api, "settings", settings)
    with pytest.raises(HTTPException) as excinfo:
        api.channel_link(api.ChannelLinkRequest(provider="zoom", link="https://example.com/j/1"))
    assert excinfo.value.status_code == 400

    status = api.channel_link(api.ChannelLinkRequest(provider="zoom", link="https://zoom.us/j/777"))
    assert status["zoom"]["mode"] == "link"
    assert status["zoom"]["link_set"] is True
    assert status["zoom"]["configured"] is True
    assert "https://zoom.us/j/777" not in json.dumps(status)

    removed = api.channel_link(api.ChannelLinkRequest(provider="zoom", link=""))
    assert removed["zoom"]["configured"] is False


def test_zoom_login_endpoint_requires_app(settings, monkeypatch) -> None:
    monkeypatch.setattr(api, "settings", settings)
    with pytest.raises(HTTPException) as excinfo:
        api.zoom_login()
    assert excinfo.value.status_code == 409

    settings.env.update(
        ZOOM_CLIENT_ID="cid",
        ZOOM_CLIENT_SECRET="secret",
        ZOOM_REDIRECT_URI="http://127.0.0.1:8000/api/zoom/callback",
    )
    data = api.zoom_login()
    assert data["url"].startswith("https://zoom.us/oauth/authorize?")


def test_zoom_callback_rejects_bad_state(settings, monkeypatch) -> None:
    monkeypatch.setattr(api, "settings", settings)
    response = api.zoom_callback(code="code", state="nope")
    assert response.status_code == 307
    assert response.headers["location"] == "/?zoom=error"
