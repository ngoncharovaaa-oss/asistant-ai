# -*- coding: utf-8 -*-
"""Режим «Ася ведёт встречу»: сценарий по данным прогона, озвучка и передача в Zoom."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app.api as api
import app.voice as voice
from app.config import Settings


@pytest.fixture
def client(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(api, "settings", settings)
    api.run_endpoint(api.RunRequest())


def _request(host: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/zoom/open",
            "headers": [(b"host", host.encode("ascii"))],
        }
    )


def test_meeting_uses_run_data(client) -> None:
    data = api.meeting_view()
    assert [item["id"] for item in data["items"]] == [
        "week",
        "protocols",
        "decisions",
        "calendar",
        "money",
        "control",
    ]
    assert data["profile"] == "Иван Петров"
    assert data["intro"].startswith("Добрый день, Иван.")
    assert "80 писем" in data["items"][0]["fact"]
    assert "54 черновика" in data["items"][0]["fact"]
    assert "40 решений" in data["items"][2]["fact"]
    assert "311 661 ₽" in data["items"][4]["fact"]
    assert data["items"][5]["fact"] == "80/80 · 0 автоотправок"
    assert data["zoom"] == {"configured": False, "link_set": False, "mode": ""}
    assert data["estimated_seconds"] > 60


def test_avatar_say_rejects_line_outside_script(client) -> None:
    with pytest.raises(HTTPException) as excinfo:
        api.avatar_say(api.SayRequest(text="Свободная реплика вне сценария"))
    assert excinfo.value.status_code == 403


def test_avatar_say_returns_wav(client, tmp_path, monkeypatch) -> None:
    wav = tmp_path / "line.wav"
    wav.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")
    seen: list[str] = []

    def fake_synthesize(settings: Settings, text: str):
        seen.append(text)
        return wav

    monkeypatch.setattr(api.voice, "synthesize", fake_synthesize)
    intro = api.meeting_view()["intro"]
    response = api.avatar_say(api.SayRequest(text=intro))
    assert seen == [intro]
    assert response.media_type == "audio/wav"
    assert str(response.path) == str(wav)


def test_avatar_say_reports_voice_failure(client, monkeypatch) -> None:
    def broken(settings: Settings, text: str):
        raise voice.VoiceError("sapi down")

    monkeypatch.setattr(api.voice, "synthesize", broken)
    intro = api.meeting_view()["intro"]
    with pytest.raises(HTTPException) as excinfo:
        api.avatar_say(api.SayRequest(text=intro))
    assert excinfo.value.status_code == 503


def test_meeting_done_logs_event(client) -> None:
    data = api.meeting_done(api.MeetingDoneRequest(items=6, seconds=188))
    assert data["ok"] is True
    event = data["events"][0]
    assert event["kind"] == "meeting"
    assert event["payload"]["items"] == 6
    assert event["payload"]["seconds"] == 188


def test_meeting_done_saves_protocol(client) -> None:
    data = api.meeting_done(
        api.MeetingDoneRequest(items=2, seconds=125, covered=[0, 2], channel="zoom")
    )
    minute = api.minute_view(data["id"])
    assert minute["channel"] == "zoom"
    assert minute["seconds"] == 125
    assert minute["profile"] == "Иван Петров"
    assert [item["covered"] for item in minute["items"]] == [
        True,
        False,
        True,
        False,
        False,
        False,
    ]
    listed = api.minutes_view(limit=5)["minutes"]
    assert listed[0]["id"] == data["id"]
    markdown = api.minute_markdown(data["id"]).body.decode("utf-8")
    assert "# Протокол планёрки" in markdown
    assert "освещено" in markdown
    assert "не рассмотрено" in markdown
    assert "Освещено пунктов: 2 из 6" in markdown


def test_meeting_join_saves_room_locally(client, settings) -> None:
    from app import links

    payload = api.MeetingJoinRequest(provider="zoom", link="https://zoom.us/j/123456")
    data = api.meeting_join(payload, _request("127.0.0.1:8000"))
    assert data["ok"] is True
    assert data["saved"] is True
    assert data["url"] == "https://zoom.us/j/123456"
    assert links.saved(settings, "zoom") == "https://zoom.us/j/123456"
    assert data["channels"]["zoom"]["link_set"] is True


def test_meeting_join_on_stand_does_not_save(client, settings) -> None:
    from app import links

    payload = api.MeetingJoinRequest(provider="telemost", link="https://telemost.yandex.ru/j/42")
    data = api.meeting_join(payload, _request("stand.example.com"))
    assert data["saved"] is False
    assert data["url"] == "https://telemost.yandex.ru/j/42"
    assert links.saved(settings, "telemost") == ""


def test_meeting_join_rejects_foreign_host(client) -> None:
    payload = api.MeetingJoinRequest(provider="zoom", link="https://evil.example.com/j/1")
    with pytest.raises(HTTPException) as excinfo:
        api.meeting_join(payload, _request("127.0.0.1:8000"))
    assert excinfo.value.status_code == 400


def test_join_room_is_local_only(client) -> None:
    with pytest.raises(HTTPException) as excinfo:
        api.join_room("zoom", _request("stand.example.com"))
    assert excinfo.value.status_code == 404


def test_join_room_redirects_to_saved_room(client, settings) -> None:
    from app import links

    links.set_link(settings, "telemost", "https://telemost.yandex.ru/j/777")
    response = api.join_room("telemost", _request("localhost:8000"))
    assert response.status_code == 307
    assert response.headers["location"] == "https://telemost.yandex.ru/j/777"
    with pytest.raises(HTTPException) as excinfo:
        api.join_room("zoom", _request("localhost:8000"))
    assert excinfo.value.status_code == 404


def test_meeting_task_lifecycle(client) -> None:
    done = api.meeting_done(api.MeetingDoneRequest(items=2, seconds=60, covered=[0, 1]))
    minute_id = done["id"]
    added = api.minute_task_add(
        minute_id,
        api.MeetingTaskRequest(
            item_index=1,
            title="Свести итоги недели",
            owner="Анна",
            due="2026-12-01",
        ),
    )
    task = added["task"]
    assert task["status"] == "open"
    assert task["owner"] == "Анна"
    assert task["item_index"] == 1
    minute = api.minute_view(minute_id)
    assert [item["id"] for item in minute["tasks"]] == [task["id"]]
    listed = api.minutes_view(limit=5)["minutes"][0]
    assert (listed["tasks_total"], listed["tasks_done"]) == (1, 0)
    markdown = api.minute_markdown(minute_id).body.decode("utf-8")
    assert "## Задачи по встрече" in markdown
    assert "Свести итоги недели" in markdown
    assert "01.12.2026" in markdown
    toggled = api.minute_task_state(
        minute_id, task["id"], api.MeetingTaskStateRequest(status="done")
    )
    assert toggled["task"]["status"] == "done"
    assert api.minutes_view(limit=5)["minutes"][0]["tasks_done"] == 1
    back = api.minute_task_state(
        minute_id, task["id"], api.MeetingTaskStateRequest(status="open")
    )
    assert back["task"]["status"] == "open"


def test_meeting_task_unknown_minute(client) -> None:
    with pytest.raises(HTTPException) as excinfo:
        api.minute_task_add(999, api.MeetingTaskRequest(title="Задача"))
    assert excinfo.value.status_code == 404


def test_meeting_task_bad_status(client) -> None:
    minute_id = api.meeting_done(api.MeetingDoneRequest(seconds=10))["id"]
    task = api.minute_task_add(minute_id, api.MeetingTaskRequest(title="Проверить письма"))["task"]
    with pytest.raises(HTTPException) as excinfo:
        api.minute_task_state(
            minute_id, task["id"], api.MeetingTaskStateRequest(status="waiting")
        )
    assert excinfo.value.status_code == 400
    with pytest.raises(HTTPException) as excinfo:
        api.minute_task_state(999, task["id"], api.MeetingTaskStateRequest(status="done"))
    assert excinfo.value.status_code == 404


def test_zoom_open_is_local_only(client) -> None:
    with pytest.raises(HTTPException) as excinfo:
        api.zoom_open(_request("stand.example.com"))
    assert excinfo.value.status_code == 404


def test_zoom_open_without_room(client) -> None:
    with pytest.raises(HTTPException) as excinfo:
        api.zoom_open(_request("127.0.0.1:8000"))
    assert excinfo.value.status_code == 404


def test_zoom_open_redirects_to_saved_room(client, monkeypatch) -> None:
    link = "https://zoom.us/j/111?pwd=secret"
    monkeypatch.setattr(api, "load_zoom", lambda settings: SimpleNamespace(personal_link=link))
    response = api.zoom_open(_request("localhost:8000"))
    assert response.status_code == 307
    assert response.headers["location"] == link
