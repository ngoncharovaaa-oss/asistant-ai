# -*- coding: utf-8 -*-
"""HTTP-слой без внешнего клиента: вызываем обработчики напрямую."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.api as api
from app.config import Settings


@pytest.fixture
def client(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(api, "settings", settings)
    api.run_endpoint(api.RunRequest())


def test_run_and_summary(client) -> None:
    data = api.summary()
    assert data["empty"] is False
    assert data["metrics"]["emails"] == 80
    assert len(data["inbox"]) == 80
    assert len(data["drafts"]) == 54
    assert len(data["protocols"]) == 5
    assert data["economics"]["saving"] == 69_221
    assert data["morning"]["date"] == "2026-12-07"
    assert any(event["kind"] == "run" for event in data["events"])


def test_edit_approve_reject_flow(client) -> None:
    drafts = api.summary()["drafts"]
    first, second = drafts[0]["id"], drafts[1]["id"]
    assert api.edit(first, api.EditRequest(body="Правка ассистента: срок пятница."))["edited"] is True
    assert "Правка" in api.draft(first)["body"]
    assert api.approve(first)["sent"] is True
    assert api.reject(second)["ok"] is True
    with pytest.raises(HTTPException):
        api.approve(second)


def test_unknown_draft_returns_404(client) -> None:
    with pytest.raises(HTTPException):
        api.draft("d-нет-такого")
    with pytest.raises(HTTPException):
        api.approve("d-нет-такого")


def test_protocol_task_approval(client) -> None:
    protocol = api.summary()["protocols"][0]
    pid = protocol["id"]
    assert protocol["approved_tasks"] == 0
    assert protocol["tasks"][0]["status"] == "waiting"
    assert api.approve_protocol_task(pid, 0)["status"] == "approved"
    current = next(p for p in api.summary()["protocols"] if p["id"] == pid)
    assert current["approved_tasks"] == 1
    assert current["tasks"][0]["status"] == "approved"
    result = api.approve_protocol(pid)
    assert result["approved"] == len(current["tasks"]) - 1
    assert all(task["status"] == "approved" for task in api.protocols()["protocols"][0]["tasks"])
    assert any(event["kind"] == "task" for event in api.summary()["events"])
    with pytest.raises(HTTPException):
        api.approve_protocol_task("нет-такого", 0)
    with pytest.raises(HTTPException):
        api.approve_protocol_task(pid, 999)


def test_calendar_and_journal_csv(client) -> None:
    data = api.summary()
    assert data["calendar"] == []
    protocol = data["protocols"][0]
    api.approve_protocol(protocol["id"])
    calendar = api.calendar()
    assert calendar["approved"] == len(protocol["tasks"])
    assert calendar["total"] == 37
    assert calendar["tasks"][0]["protocol"] == protocol["title"]
    csv_body = api.journal_csv().body.decode("utf-8")
    assert "at,kind,details" in csv_body
    assert "tasks_bulk" in csv_body


def test_dashboard_contains_queue_and_palette() -> None:
    page = api.dashboard()
    assert "AsiStantAI" in page
    assert 'id="drafts"' in page
    assert "#B4552D" in page
    assert "0 автоотправок" in page
    assert 'id="channels"' in page
    assert "Живой прогон" in page
    assert 'id="zoomBtn"' in page


def test_channels_and_live_run_requires_config(client) -> None:
    status = api.channels()
    assert status["mail_configured"] is False
    assert status["live_ready"] is False
    assert status["zoom"]["configured"] is False
    checked = api.channels_check()
    assert checked["telegram"]["configured"] is False
    with pytest.raises(HTTPException) as error:
        api.zoom_meeting(api.ZoomRequest(topic="Планёрка"))
    assert error.value.status_code == 409
    with pytest.raises(HTTPException):
        api.run_endpoint(api.RunRequest(source="live"))


def test_zoom_meeting_link_mode(client, monkeypatch) -> None:
    monkeypatch.setitem(api.settings.env, "ZOOM_PERSONAL_LINK", "https://zoom.us/j/42")
    result = api.zoom_meeting(api.ZoomRequest(topic="Планёрка", start="2026-11-28 10:00"))
    assert result["mode"] == "link"
    assert result["join_url"] == "https://zoom.us/j/42"
    assert any(event["kind"] == "zoom_link" for event in api.summary()["events"])


def test_health_and_profiles(client) -> None:
    health = api.health()
    assert health["ok"] is True
    assert health["has_run"] is True
    profiles = api.profiles()["profiles"]
    assert any(item["name"] == "leader2.md" for item in profiles)


def test_run_with_second_profile(client) -> None:
    result = api.run_endpoint(api.RunRequest(profile="leader2.md"))
    assert result["profile"]["name"] == "Мария Орлова"
    assert result["metrics"]["auto_sends"] == 0
    summary = api.summary()
    assert summary["metrics"]["profile_name"] == "Мария Орлова"


def _first_protocol(client) -> dict:
    return api.summary()["protocols"][0]


def test_protocol_task_state_resolved_and_unresolved(client) -> None:
    protocol = _first_protocol(client)
    pid = protocol["id"]
    with pytest.raises(HTTPException) as error:
        api.protocol_task_state(pid, 0, api.TaskStateRequest(status="in_progress"))
    assert error.value.status_code == 409

    api.approve_protocol_task(pid, 0)
    result = api.protocol_task_state(pid, 0, api.TaskStateRequest(status="in_progress"))
    assert result["status"] == "in_progress"
    current = next(item for item in api.summary()["protocols"] if item["id"] == pid)
    assert current["tasks"][0]["status"] == "in_progress"
    assert current["in_progress_tasks"] == 1

    api.protocol_task_state(pid, 0, api.TaskStateRequest(status="done", note="Согласовано с руководителем"))
    current = next(item for item in api.summary()["protocols"] if item["id"] == pid)
    assert current["tasks"][0]["status"] == "done"
    assert current["tasks"][0]["note"] == "Согласовано с руководителем"
    assert current["resolved_tasks"] == 1
    assert api.calendar()["tasks"][0]["status"] == "done"

    api.protocol_task_state(pid, 0, api.TaskStateRequest(status="open", note="Нужно решение по бюджету"))
    current = next(item for item in api.summary()["protocols"] if item["id"] == pid)
    assert current["tasks"][0]["status"] == "open"
    assert current["open_tasks"] == 1
    assert current["tasks"][0]["note"] == "Нужно решение по бюджету"
    assert api.summary()["protocol_progress"]["unresolved"] == 1

    with pytest.raises(HTTPException) as error:
        api.protocol_task_state(pid, 0, api.TaskStateRequest(status="unknown"))
    assert error.value.status_code == 400


def test_protocol_task_revision_and_leader_sync(client) -> None:
    protocol = _first_protocol(client)
    pid = protocol["id"]
    api.approve_protocol_task(pid, 0)
    result = api.protocol_task_revise(
        pid,
        0,
        api.TaskReviseRequest(
            text="Выделить вендоров к продвижению в 3 квартале",
            owner="BDM",
            deadline="до 15.12.2026",
            reason="Задача уточнена после согласования",
        ),
    )
    assert result["revised"] is True
    current = next(item for item in api.summary()["protocols"] if item["id"] == pid)
    task = current["tasks"][0]
    assert task["text"] == "Выделить вендоров к продвижению в 3 квартале"
    assert task["owner"] == "BDM"
    assert task["revised"] is True
    assert task["synced_at"] == ""
    assert current["unsynced_tasks"] == 1

    synced = api.protocol_task_sync(pid, 0, api.TaskSyncRequest(note="Согласовано в Telegram"))
    assert synced["ok"] is True
    assert synced["channel"] == "mock"
    task = next(item for item in api.summary()["protocols"] if item["id"] == pid)["tasks"][0]
    assert task["synced_at"]
    assert task["sync_channel"] == "mock"
    assert next(item for item in api.summary()["protocols"] if item["id"] == pid)["unsynced_tasks"] == 0

    again = api.protocol_task_sync(pid, 0, api.TaskSyncRequest())
    assert again["already"] is True
    kinds = {event["kind"] for event in api.summary()["events"]}
    assert {"task_revise", "task_sync"} <= kinds

    with pytest.raises(HTTPException) as error:
        api.protocol_task_sync(pid, 1, api.TaskSyncRequest())
    assert error.value.status_code == 409


def test_protocol_task_decomposition_parallel_and_sequential(client) -> None:
    protocol = _first_protocol(client)
    pid = protocol["id"]
    api.approve_protocol_task(pid, 0)
    steps = [
        api.StepInput(title="Зафиксировать метрики по вендорам", role="Аналитик", deadline="до 15.12.2026"),
        api.StepInput(title="Проанализировать витрину конкурентов", role="Команда CJM", deadline="до 10.12.2026"),
        api.StepInput(
            title="Провести переговоры с вендорами",
            role="BDM",
            deadline="до 20.12.2026",
            depends_on=[0, 1],
        ),
        api.StepInput(
            title="Выделить спец-ассортимент",
            role="Команда CJM",
            deadline="до 25.12.2026",
            depends_on=[2],
        ),
    ]
    result = api.protocol_task_steps(pid, 0, api.StepsRequest(steps=steps))
    assert result["count"] == 4
    plan = result["steps"]
    assert plan[0]["parallel"] is True and plan[1]["parallel"] is True
    assert plan[2]["depends_on"] == [0, 1]
    assert plan[3]["depends_on"] == [2]

    with pytest.raises(HTTPException) as error:
        api.protocol_step_state(pid, 0, 2, api.StepStateRequest(status="in_progress"))
    assert error.value.status_code == 409
    assert "blocked:0,1" in str(error.value.detail)

    api.protocol_step_state(pid, 0, 0, api.StepStateRequest(status="done"))
    api.protocol_step_state(pid, 0, 1, api.StepStateRequest(status="done"))
    api.protocol_step_state(pid, 0, 2, api.StepStateRequest(status="in_progress"))
    api.protocol_step_state(pid, 0, 2, api.StepStateRequest(status="done"))
    api.protocol_step_state(pid, 0, 3, api.StepStateRequest(status="done"))
    task = next(item for item in api.summary()["protocols"] if item["id"] == pid)["tasks"][0]
    assert task["steps_done"] == 4
    assert task["steps_total"] == 4
    assert task["ready_to_close"] is True

    with pytest.raises(HTTPException) as error:
        api.protocol_step_state(pid, 0, 0, api.StepStateRequest(status="in_progress"))
    assert error.value.status_code == 409
    assert "dependent_started:2" in str(error.value.detail)

    appended = api.protocol_task_steps(
        pid,
        0,
        api.StepsRequest(
            steps=[api.StepInput(title="Согласовать запуск акции", role="Руководитель", depends_on=[0])],
            mode="append",
        ),
    )
    assert appended["count"] == 5
    assert appended["steps"][-1]["index"] == 4

    with pytest.raises(HTTPException):
        api.protocol_task_steps(
            pid,
            0,
            api.StepsRequest(steps=[api.StepInput(title="Поздний шаг", depends_on=[3])]),
        )


def test_protocol_task_errors(client) -> None:
    protocol = _first_protocol(client)
    pid = protocol["id"]
    with pytest.raises(HTTPException) as error:
        api.protocol_task_revise(pid, 999, api.TaskReviseRequest(text="Нет такой задачи"))
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        api.protocol_task_steps("нет-такого", 0, api.StepsRequest(steps=[api.StepInput(title="Шаг")]))
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        api.protocol_task_steps(
            pid, 0, api.StepsRequest(steps=[api.StepInput(title="Шаг", depends_on=[4])])
        )
    assert error.value.status_code == 400
    with pytest.raises(HTTPException) as error:
        api.protocol_step_state(pid, 0, 0, api.StepStateRequest(status="done"))
    assert error.value.status_code == 404
