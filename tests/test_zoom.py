# -*- coding: utf-8 -*-
"""Zoom без сети: постоянная ссылка и API поверх фейкового HTTP."""
from __future__ import annotations

import json

import pytest

from app.live import Channels, channels_status
from app.zoom import ZoomError

SECRET = "zoom-client-secret"
TOKEN = "zoom-access-token"


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeZoom:
    def __init__(self):
        self.requests = []
        self.fail = False

    def __call__(self, request, timeout: int = 10):
        self.requests.append(request)
        if self.fail:
            raise OSError("network down")
        url = request.full_url
        if url.startswith("https://zoom.us/oauth/token"):
            return FakeResponse(
                {"access_token": TOKEN, "refresh_token": "refresh-token", "expires_in": 3600}
            )
        if request.method == "GET" and "/users/" in url:
            return FakeResponse({"display_name": "Ася"})
        if request.method == "POST" and url.endswith("/meetings"):
            return FakeResponse(
                {
                    "id": 98765432101,
                    "join_url": "https://zoom.us/j/98765432101?pwd=abc",
                    "start_url": "https://zoom.us/s/host-secret",
                }
            )
        raise AssertionError(url)


def api_settings(settings, **env):
    settings.env.update(env)
    return settings


def test_link_mode_without_network(settings) -> None:
    api_settings(settings, ZOOM_PERSONAL_LINK="https://zoom.us/j/111")
    calls: list = []

    def forbidden(request, timeout: int = 10):
        calls.append(request)
        raise AssertionError("network must not be used")

    ch = Channels(settings, urlopen=forbidden)
    status = channels_status(settings, check=True, channels=ch)
    assert status["zoom"]["configured"] is True
    assert status["zoom"]["mode"] == "link"
    assert status["zoom"]["ok"] is True
    assert status["zoom_ready"] is True
    result = ch.zoom.create_meeting("Планёрка", "2026-11-28 10:00")
    assert result["mode"] == "link"
    assert result["join_url"] == "https://zoom.us/j/111"
    assert result["start"] == "2026-11-28T10:00:00"
    assert calls == []


def test_api_mode_creates_meeting_and_hides_secrets(settings) -> None:
    api_settings(
        settings,
        ZOOM_ACCOUNT_ID="acc",
        ZOOM_CLIENT_ID="cid",
        ZOOM_CLIENT_SECRET=SECRET,
    )
    fake = FakeZoom()
    ch = Channels(settings, urlopen=fake)
    status = channels_status(settings, check=True, channels=ch)
    payload = json.dumps(status, ensure_ascii=False)
    assert SECRET not in payload
    assert TOKEN not in payload
    assert status["zoom"]["mode"] == "api"
    assert status["zoom"]["ok"] is True
    assert status["zoom_ready"] is True

    result = ch.zoom.create_meeting("Бюджет", "2026-11-28 10:00", minutes=60)
    assert result["join_url"].startswith("https://zoom.us/j/")
    assert result["id"] == "98765432101"
    assert "start_url" not in result
    assert "host-secret" not in json.dumps(result, ensure_ascii=False)

    token_request = fake.requests[0]
    assert token_request.full_url.startswith("https://zoom.us/oauth/token?")
    assert token_request.get_header("Authorization").startswith("Basic ")
    meeting_request = fake.requests[-1]
    body = json.loads(meeting_request.data.decode("utf-8"))
    assert body["topic"] == "Бюджет"
    assert body["duration"] == 60
    assert body["start_time"] == "2026-11-28T10:00:00"
    assert body["timezone"] == "Europe/Moscow"
    assert body["settings"]["waiting_room"] is True


def test_api_errors_are_generic(settings) -> None:
    api_settings(
        settings,
        ZOOM_ACCOUNT_ID="acc",
        ZOOM_CLIENT_ID="cid",
        ZOOM_CLIENT_SECRET=SECRET,
    )
    fake = FakeZoom()
    fake.fail = True
    ch = Channels(settings, urlopen=fake)
    status = channels_status(settings, check=True, channels=ch)
    assert status["zoom"]["ok"] is False
    assert SECRET not in status["zoom"]["detail"]
    with pytest.raises(ZoomError) as error:
        ch.zoom.create_meeting("Тема")
    assert SECRET not in str(error.value)
    assert TOKEN not in str(error.value)


def test_bad_start_time(settings) -> None:
    api_settings(settings, ZOOM_PERSONAL_LINK="https://zoom.us/j/1")
    ch = Channels(settings)
    with pytest.raises(ZoomError):
        ch.zoom.create_meeting("Тема", "завтра")


def test_unconfigured_zoom(settings) -> None:
    ch = Channels(settings)
    assert channels_status(settings, channels=ch)["zoom"]["configured"] is False
    with pytest.raises(ZoomError):
        ch.zoom.create_meeting("Тема")


def test_saved_link_from_dashboard(settings) -> None:
    from app.links import set_link

    set_link(settings, "zoom", "zoom.us/j/222")
    ch = Channels(settings)
    assert ch.zoom.config.mode == "link"
    assert ch.zoom.create_meeting("Тема")["join_url"] == "https://zoom.us/j/222"


def test_oauth_login_flow(settings) -> None:
    from urllib.parse import parse_qs, urlparse

    from app.zoom import authorize_url, check_state

    api_settings(
        settings,
        ZOOM_CLIENT_ID="cid",
        ZOOM_CLIENT_SECRET=SECRET,
        ZOOM_REDIRECT_URI="http://127.0.0.1:8000/api/zoom/callback",
    )
    fake = FakeZoom()
    ch = Channels(settings, urlopen=fake)
    url = authorize_url(ch.zoom.config)
    assert url.startswith("https://zoom.us/oauth/authorize?")
    query = parse_qs(urlparse(url).query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["cid"]
    assert query["redirect_uri"] == ["http://127.0.0.1:8000/api/zoom/callback"]
    state = query["state"][0]
    assert check_state(state) is True
    assert check_state(state) is False

    result = ch.zoom.login("test-code")
    assert result == {"name": "Ася", "mode": "login"}
    assert ch.zoom.config.logged_in is True
    tokens_file = settings.db_path.parent / "zoom_oauth.json"
    tokens = json.loads(tokens_file.read_text(encoding="utf-8"))
    assert tokens["refresh_token"] == "refresh-token"

    status = channels_status(settings, channels=ch)
    assert status["zoom"]["mode"] == "login"
    assert status["zoom"]["logged_in"] is True
    assert status["zoom"]["name"] == "Ася"
    payload = json.dumps(status, ensure_ascii=False)
    assert TOKEN not in payload
    assert "refresh-token" not in payload

    meeting = ch.zoom.create_meeting("Бюджет", "2026-11-28 10:00")
    assert meeting["mode"] == "login"
    assert meeting["join_url"].startswith("https://zoom.us/j/")
    assert "/users/me/meetings" in fake.requests[-1].full_url

    ch.zoom.logout()
    assert not tokens_file.exists()
    assert channels_status(settings, channels=Channels(settings))["zoom"]["configured"] is False


def test_expired_login_token_is_refreshed(settings) -> None:
    api_settings(
        settings,
        ZOOM_CLIENT_ID="cid",
        ZOOM_CLIENT_SECRET=SECRET,
        ZOOM_REDIRECT_URI="http://127.0.0.1:8000/api/zoom/callback",
    )
    (settings.db_path.parent / "zoom_oauth.json").write_text(
        json.dumps({"access_token": "old", "refresh_token": "r1", "expires_at": 1, "name": "Ася"}),
        encoding="utf-8",
    )
    fake = FakeZoom()
    ch = Channels(settings, urlopen=fake)
    assert ch.zoom.config.mode == "login"
    assert ch.zoom.check()["ok"] is True
    assert "grant_type=refresh_token" in fake.requests[0].full_url
    saved = json.loads((settings.db_path.parent / "zoom_oauth.json").read_text(encoding="utf-8"))
    assert saved["access_token"] == TOKEN
