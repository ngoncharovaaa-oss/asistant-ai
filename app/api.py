# -*- coding: utf-8 -*-
"""HTTP-слой AsiStantAI: запуск, очередь решений, черновики, протоколы, сводки."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from . import __version__, voice
from .bitrix24 import BitrixError, load_bitrix24
from .calendar_links import protocol_id_for, topic_for
from .chat import ChatError, ChatStore
from .config import Settings
from .datasets import load_calendar
from .digest import PROTECTED_CATEGORIES
from .journal import Journal
from .links import LinkError, normalize, set_link
from .meeting import build_meeting, minutes_markdown
from .live import (
    ChannelError,
    Channels,
    approve_draft,
    channels_status,
    mailbox_store,
    mailboxes_overview,
    run_live,
)
from .mailboxes import MailboxError
from .periods import PeriodError, require_period
from .pipeline import run as run_pipeline
from .telemost import TelemostError, load_telemost
from .tg_user import TelegramUserError
from .zoom import ZoomError, authorize_url, check_state, load_zoom

settings = Settings.load()
app = FastAPI(title="AsiStantAI", version=__version__)


class RunRequest(BaseModel):
    profile: str | None = None
    source: str | None = None  # dataset | live


class EditRequest(BaseModel):
    body: str = Field(min_length=1, max_length=6000)
    comment: str = Field(default="", max_length=500)


class MailboxRequest(BaseModel):
    address: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=400)
    provider: str = Field(default="custom", max_length=20)
    label: str = Field(default="", max_length=100)
    from_name: str = Field(default="", max_length=100)
    imap_host: str = Field(default="", max_length=200)
    imap_port: int = Field(default=0, ge=0, le=65535)
    smtp_host: str = Field(default="", max_length=200)
    smtp_port: int = Field(default=0, ge=0, le=65535)


class MailboxPasswordRequest(BaseModel):
    password: str = Field(min_length=1, max_length=400)


class MailboxDraftsRequest(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=20)


class ChatMessageRequest(BaseModel):
    employee: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=2000)
    role: str = Field(default="", max_length=40)
    source: str = Field(default="dashboard", max_length=40)


class ChatReplyRequest(BaseModel):
    employee: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=2000)


class ZoomRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=200)
    start: str = Field(default="", max_length=32)
    minutes: int = Field(default=45, ge=15, le=240)
    agenda: str = Field(default="", max_length=500)


class TelemostRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=200)
    start: str = Field(default="", max_length=32)


class ChannelLinkRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=20)
    link: str = Field(default="", max_length=500)


class BitrixContactRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    name: str = Field(default="", max_length=200)
    role: str = Field(default="", max_length=100)
    note: str = Field(default="", max_length=2000)


class TaskStateRequest(BaseModel):
    status: str = Field(min_length=1, max_length=20)
    note: str = Field(default="", max_length=500)


class TaskReviseRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    owner: str = Field(default="", max_length=200)
    deadline: str = Field(default="", max_length=200)
    reason: str = Field(default="", max_length=500)


class TaskSyncRequest(BaseModel):
    note: str = Field(default="", max_length=500)


class StepInput(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    owner: str = Field(default="", max_length=200)
    role: str = Field(default="", max_length=200)
    deadline: str = Field(default="", max_length=200)
    depends_on: list[int] = Field(default_factory=list, max_length=20)


class StepsRequest(BaseModel):
    steps: list[StepInput] = Field(default_factory=list, max_length=20)
    mode: str = Field(default="replace", max_length=10)


class StepStateRequest(BaseModel):
    status: str = Field(min_length=1, max_length=20)
    note: str = Field(default="", max_length=500)


class SayRequest(BaseModel):
    text: str = Field(min_length=1, max_length=voice.MAX_CHARS)
    employee: str = Field(default="", max_length=120)


class MeetingDoneRequest(BaseModel):
    items: int = Field(default=0, ge=0, le=20)
    seconds: int = Field(default=0, ge=0, le=4 * 3600)
    covered: list[int] = Field(default_factory=list, max_length=20)
    channel: str = Field(default="", max_length=20)
    employee: str = Field(default="", max_length=120)


class MeetingJoinRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=20)
    link: str = Field(min_length=1, max_length=500)


class MeetingTaskRequest(BaseModel):
    item_index: int = Field(default=0, ge=0, le=20)
    title: str = Field(min_length=2, max_length=200)
    owner: str = Field(default="", max_length=100)
    due: str = Field(default="", max_length=40)


class MeetingTaskStateRequest(BaseModel):
    status: str = Field(min_length=1, max_length=10)


def _journal() -> Journal:
    return Journal(settings.db_path)


def _period(value: str | None) -> str:
    try:
        return require_period(value)
    except PeriodError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _chat_store() -> ChatStore:
    return ChatStore(settings.db_path)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _resolve_profile(name: str | None) -> Path | None:
    if not name:
        return None
    candidate = (settings.profiles_dir / name).resolve()
    root = settings.profiles_dir.resolve()
    if root not in candidate.parents:
        raise HTTPException(status_code=400, detail="profile_outside_profiles_dir")
    if not candidate.exists():
        raise HTTPException(status_code=404, detail="profile_not_found")
    return candidate


def _profiles() -> list[dict]:
    items = []
    root = settings.profiles_dir
    if root.exists():
        for path in sorted(root.glob("*.md")):
            if path.name.lower().startswith("readme"):
                continue
            items.append({"name": path.name, "default": False})
    default = settings.data_dir / "leader_profile.md"
    if default.exists():
        items.insert(0, {"name": "leader_profile.md", "default": True})
    return items


IN_CALENDAR = {"approved", "in_progress", "done", "open"}


def _apply_task_progress(
    protocols: list[dict],
    states: dict[str, dict[int, dict]],
    steps: dict[str, dict[int, list[dict]]],
) -> None:
    """Накладываем на протокол статусы, правки и шаги из журнала."""
    for protocol in protocols:
        protocol_id = protocol.get("id", "")
        marks = states.get(protocol_id, {})
        plans = steps.get(protocol_id, {})
        tasks = protocol.get("tasks", [])
        for index, task in enumerate(tasks):
            state = marks.get(index, {})
            for key in ("text", "owner", "deadline"):
                if key in state:
                    task[key] = state[key]
            task["status"] = state.get("status", "waiting")
            task["note"] = state.get("note", "")
            task["updated_at"] = state.get("updated_at", "")
            task["revised"] = bool(state.get("revised"))
            task["revision_reason"] = state.get("revision_reason", "")
            task["revised_at"] = state.get("revised_at", "")
            task["synced_at"] = state.get("synced_at", "")
            task["sync_note"] = state.get("sync_note", "")
            task["sync_channel"] = state.get("sync_channel", "")
            task_steps = plans.get(index, [])
            task["steps"] = task_steps
            task["steps_total"] = len(task_steps)
            task["steps_done"] = sum(1 for step in task_steps if step["status"] == "done")
            task["ready_to_close"] = bool(task_steps) and task["steps_done"] == len(task_steps) and task["status"] != "done"
        protocol["approved_tasks"] = sum(1 for task in tasks if task["status"] in IN_CALENDAR)
        protocol["calendar_tasks"] = protocol["approved_tasks"]
        protocol["resolved_tasks"] = sum(1 for task in tasks if task["status"] == "done")
        protocol["open_tasks"] = sum(1 for task in tasks if task["status"] == "open")
        protocol["in_progress_tasks"] = sum(1 for task in tasks if task["status"] == "in_progress")
        protocol["revised_tasks"] = sum(1 for task in tasks if task["revised"])
        protocol["unsynced_tasks"] = sum(1 for task in tasks if task["revised"] and not task["synced_at"])
        protocol["steps_total"] = sum(task["steps_total"] for task in tasks)
        protocol["steps_done"] = sum(task["steps_done"] for task in tasks)


def _approved_tasks(protocols: list[dict]) -> list[dict]:
    items = []
    for protocol in protocols:
        for index, task in enumerate(protocol.get("tasks", [])):
            if task.get("status") in IN_CALENDAR:
                items.append(
                    {
                        "protocol": protocol.get("title", ""),
                        "protocol_id": protocol.get("id", ""),
                        "index": index,
                        "owner": task.get("owner", ""),
                        "text": task.get("text", ""),
                        "deadline": task.get("deadline", ""),
                        "status": task.get("status", "approved"),
                        "note": task.get("note", ""),
                        "steps_done": task.get("steps_done", 0),
                        "steps_total": task.get("steps_total", 0),
                    }
                )
    return items


PROTOCOL_ERROR_CODES = {
    "no_run": 409,
    "protocol_not_found": 404,
    "task_not_found": 404,
    "step_not_found": 404,
    "bad_status": 400,
    "task_not_approved": 409,
    "empty_text": 400,
    "empty_step": 400,
    "bad_dependency": 400,
    "dependency_forward": 409,
    "blocked": 409,
    "dependent_started": 409,
    "nothing_to_sync": 409,
    "bad_mode": 400,
    "too_many_steps": 400,
}


def _protocol_error(result: dict) -> None:
    error = str(result.get("error") or "protocol_action_failed")
    code = PROTOCOL_ERROR_CODES.get(error, 409)
    if result.get("blocked_by"):
        error = "blocked:" + ",".join(str(value) for value in result["blocked_by"])
    if result.get("dependent"):
        error = "dependent_started:" + ",".join(str(value) for value in result["dependent"])
    raise HTTPException(status_code=code, detail=error)


@app.get("/api/health")
def health() -> dict:
    journal = _journal()
    try:
        runs = journal.runs(limit=1)
    finally:
        journal.close()
    return {
        "ok": True,
        "version": __version__,
        "profile": settings.data_dir.name,
        "demo_mode": settings.demo_mode,
        "llm": settings.llm_model,
        "has_run": bool(runs),
        "last_run": runs[0] if runs else None,
        "channels": channels_status(settings),
    }


@app.get("/api/profiles")
def profiles() -> dict:
    return {"profiles": _profiles()}


@app.post("/api/run")
def run_endpoint(payload: RunRequest | None = None) -> dict:
    profile_path = _resolve_profile(payload.profile if payload else None)
    if payload and payload.source == "live":
        journal = _journal()
        try:
            result = run_live(settings, journal, profile_path=profile_path)
        except ChannelError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        finally:
            journal.close()
        if result is None:
            return {"run_id": None, "new_emails": 0, "skipped": True, "reason": "no_new_mail"}
        return {
            "run_id": result.run_id,
            "source": "live",
            "new_emails": result.metrics["emails"],
            "profile": {"name": result.profile.name, "role": result.profile.role},
            "metrics": result.metrics,
            "files": result.files,
        }
    result = run_pipeline(settings, profile_path=profile_path)
    return {
        "run_id": result.run_id,
        "profile": {"name": result.profile.name, "role": result.profile.role},
        "metrics": result.metrics,
        "files": result.files,
    }


@app.get("/api/summary")
def summary() -> dict:
    journal = _journal()
    try:
        run_id = journal.last_run()
        if not run_id:
            return {"empty": True, "profiles": _profiles(), "version": __version__}
        metrics = journal.run_metrics(run_id) or {}
        drafts = journal.drafts(run_id)
        inbox = journal.classifications(run_id)
        protocols = journal.protocols(run_id)
        task_states = journal.protocol_task_states(run_id)
        task_steps = journal.protocol_steps(run_id)
        progress = journal.protocol_progress(run_id)
        events = journal.events(limit=12)
    finally:
        journal.close()
    _apply_task_progress(protocols, task_states, task_steps)
    digest = _read_json(settings.out_dir / "weekly_digest.json")
    morning = _read_json(settings.out_dir / "morning_plan.json")
    return {
        "empty": False,
        "version": __version__,
        "run_id": run_id,
        "profiles": _profiles(),
        "metrics": metrics,
        "drafts": drafts,
        "inbox": inbox,
        "protocols": protocols,
        "calendar": _approved_tasks(protocols),
        "protocol_progress": progress.get("overall", {}),
        "protocol_progress_detail": progress.get("protocols", {}),
        "digest": digest,
        "morning": morning,
        "events": events,
        "economics": metrics.get("economics", {}),
        "channels": channels_status(settings),
    }


INTERNAL_ROLES = {"подчинённый", "коллега", "руководитель руководителя"}


@app.get("/api/crm")
def crm() -> dict:
    """CRM из входящих: контакт, сколько писем, последняя тема и действие Аси."""
    journal = _journal()
    try:
        run_id = journal.last_run()
        classified = journal.classifications(run_id) if run_id else []
    finally:
        journal.close()
    by_id = {item.get("email_id"): item for item in classified}
    path = settings.data_dir / "inbox.csv"
    contacts: dict[str, dict] = {}
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                email = (row.get("from_email") or "").strip()
                if not email:
                    continue
                role = (row.get("sender_role") or "").strip()
                item = contacts.setdefault(
                    email,
                    {
                        "email": email,
                        "name": (row.get("from_name") or "").strip() or email,
                        "role": role,
                        "external": role not in INTERNAL_ROLES,
                        "letters": 0,
                        "topics": [],
                        "last_received": "",
                        "last_subject": "",
                        "last_action": "",
                    },
                )
                item["letters"] += 1
                received = (row.get("received") or "").strip()
                if received >= item["last_received"]:
                    item["last_received"] = received
                    item["last_subject"] = (row.get("subject") or "").strip()
                    action = (by_id.get((row.get("id") or "").strip()) or {}).get("action")
                    item["last_action"] = action or (row.get("expected_action") or "").strip()
                topic = (row.get("category") or "").strip()
                if topic and topic not in item["topics"]:
                    item["topics"].append(topic)
    items = sorted(contacts.values(), key=lambda c: (not c["external"], c["name"].lower()))
    return {
        "contacts": items,
        "total": len(items),
        "external": sum(1 for c in items if c["external"]),
        "internal": sum(1 for c in items if not c["external"]),
        "letters": sum(c["letters"] for c in items),
        "bitrix": load_bitrix24(settings).public,
    }


@app.post("/api/crm/bitrix/check")
def crm_bitrix_check() -> dict:
    channel = Channels(settings).bitrix
    result = dict(channel.config.public)
    if channel.config.configured:
        result.update(channel.check())
    else:
        result.update({"ok": False, "detail": "не настроен"})
    return result


@app.post("/api/crm/bitrix/contact")
def crm_bitrix_contact(payload: BitrixContactRequest) -> dict:
    email = payload.email.strip()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="bitrix24: contact needs an email")
    channel = Channels(settings).bitrix
    try:
        result = channel.sync_contact(
            email, payload.name.strip(), payload.role.strip(), payload.note.strip()
        )
    except BitrixError as exc:
        code = 409 if str(exc) == "bitrix24: not configured" else 502
        raise HTTPException(status_code=code, detail=str(exc)) from None
    journal = _journal()
    try:
        journal.log(
            "bitrix_contact",
            {"email": email, "id": result.get("id", ""), "created": result.get("created", False)},
        )
    finally:
        journal.close()
    return result


@app.post("/api/crm/bitrix/import")
def crm_bitrix_import() -> dict:
    channel = Channels(settings).bitrix
    try:
        items = channel.contacts()
    except BitrixError as exc:
        code = 409 if str(exc) == "bitrix24: not configured" else 502
        raise HTTPException(status_code=code, detail=str(exc)) from None
    journal = _journal()
    try:
        journal.log("bitrix_import", {"count": len(items)})
    finally:
        journal.close()
    return {"contacts": items, "count": len(items), "portal": channel.config.portal}


@app.get("/api/drafts")
def drafts(action: str | None = None, status: str | None = None) -> dict:
    journal = _journal()
    try:
        items = journal.drafts()
    finally:
        journal.close()
    if action:
        items = [item for item in items if item["kind"] == action or item["topic"] == action]
    if status:
        items = [item for item in items if item["status"] == status]
    return {"drafts": items, "count": len(items)}


@app.get("/api/drafts/{draft_id}")
def draft(draft_id: str) -> dict:
    journal = _journal()
    try:
        item = journal.draft(draft_id)
    finally:
        journal.close()
    if not item:
        raise HTTPException(status_code=404, detail="draft_not_found")
    return item


@app.post("/api/drafts/{draft_id}/edit")
def edit(draft_id: str, payload: EditRequest) -> dict:
    journal = _journal()
    try:
        result = journal.save_edit(draft_id, payload.body, payload.comment)
    finally:
        journal.close()
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "edit_failed"))
    return result


@app.post("/api/drafts/{draft_id}/approve")
def approve(draft_id: str) -> dict:
    journal = _journal()
    try:
        result = approve_draft(settings, journal, draft_id)
    except ChannelError:
        raise HTTPException(status_code=502, detail="send_failed") from None
    finally:
        journal.close()
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "approve_failed"))
    return result


@app.post("/api/drafts/{draft_id}/reject")
def reject(draft_id: str) -> dict:
    journal = _journal()
    try:
        result = journal.reject(draft_id)
    finally:
        journal.close()
    return result


@app.get("/api/protocols")
def protocols() -> dict:
    journal = _journal()
    try:
        items = journal.protocols()
        states = journal.protocol_task_states()
        steps = journal.protocol_steps()
        progress = journal.protocol_progress()
    finally:
        journal.close()
    _apply_task_progress(items, states, steps)
    return {
        "protocols": items,
        "count": len(items),
        "progress": progress.get("overall", {}),
        "detail": progress.get("protocols", {}),
    }


@app.post("/api/protocols/{protocol_id}/tasks/{task_index}/approve")
def approve_protocol_task(protocol_id: str, task_index: int) -> dict:
    journal = _journal()
    try:
        result = journal.approve_protocol_task(protocol_id, task_index)
    finally:
        journal.close()
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "task_approve_failed"))
    return result


@app.post("/api/protocols/{protocol_id}/approve")
def approve_protocol(protocol_id: str) -> dict:
    journal = _journal()
    try:
        result = journal.approve_protocol(protocol_id)
    finally:
        journal.close()
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "protocol_approve_failed"))
    return result


@app.post("/api/protocols/{protocol_id}/tasks/{task_index}/state")
def protocol_task_state(protocol_id: str, task_index: int, payload: TaskStateRequest) -> dict:
    journal = _journal()
    try:
        result = journal.set_task_status(protocol_id, task_index, payload.status, payload.note)
    finally:
        journal.close()
    if not result.get("ok"):
        _protocol_error(result)
    return result


@app.post("/api/protocols/{protocol_id}/tasks/{task_index}/revise")
def protocol_task_revise(protocol_id: str, task_index: int, payload: TaskReviseRequest) -> dict:
    journal = _journal()
    try:
        result = journal.revise_protocol_task(
            protocol_id, task_index, payload.text, payload.owner, payload.deadline, payload.reason
        )
    finally:
        journal.close()
    if not result.get("ok"):
        _protocol_error(result)
    return result


@app.post("/api/protocols/{protocol_id}/tasks/{task_index}/sync")
def protocol_task_sync(protocol_id: str, task_index: int, payload: TaskSyncRequest) -> dict:
    journal = _journal()
    try:
        task = journal.protocol_task(protocol_id, task_index)
        if task is None:
            raise HTTPException(status_code=404, detail="task_not_found")
        if not task.get("revised"):
            raise HTTPException(status_code=409, detail="nothing_to_sync")
        if task.get("synced_at"):
            return journal.sync_protocol_task(protocol_id, task_index, payload.note, channel="mock")
        channel = "mock"
        if journal.run_dataset().startswith("imap:"):
            channels = Channels(settings)
            if channels.telegram.can_notify:
                text = (
                    f"Ася: задача скорректирована\n{task.get('text', '')}\n"
                    f"Владелец: {task.get('owner') or 'не назначен'}"
                )
                try:
                    channels.tg_send(text)
                except ChannelError:
                    raise HTTPException(status_code=502, detail="sync_notify_failed") from None
                channel = "telegram"
        result = journal.sync_protocol_task(protocol_id, task_index, payload.note, channel=channel)
    finally:
        journal.close()
    if not result.get("ok"):
        _protocol_error(result)
    return result


@app.post("/api/protocols/{protocol_id}/tasks/{task_index}/steps")
def protocol_task_steps(protocol_id: str, task_index: int, payload: StepsRequest) -> dict:
    journal = _journal()
    try:
        result = journal.save_protocol_steps(
            protocol_id,
            task_index,
            [step.model_dump() for step in payload.steps],
            mode=payload.mode,
        )
    finally:
        journal.close()
    if not result.get("ok"):
        _protocol_error(result)
    return result


@app.post("/api/protocols/{protocol_id}/tasks/{task_index}/steps/{step_index}/state")
def protocol_step_state(
    protocol_id: str, task_index: int, step_index: int, payload: StepStateRequest
) -> dict:
    journal = _journal()
    try:
        result = journal.set_step_status(
            protocol_id, task_index, step_index, payload.status, payload.note
        )
    finally:
        journal.close()
    if not result.get("ok"):
        _protocol_error(result)
    return result


def _calendar_events() -> list[dict]:
    path = settings.data_dir / "calendar.ics"
    if not path.exists():
        return []
    items = []
    for event in sorted(load_calendar(path), key=lambda item: item.start):
        protocol_id = protocol_id_for(event.summary, event.category)
        items.append(
            {
                "date": event.start.strftime("%Y-%m-%d"),
                "start": event.start.strftime("%H:%M"),
                "end": event.end.strftime("%H:%M"),
                "summary": event.summary,
                "category": event.category,
                "protected": event.category in PROTECTED_CATEGORIES,
                "protocol_id": protocol_id,
                "protocol_topic": topic_for(protocol_id),
            }
        )
    return items


@app.get("/api/calendar")
def calendar() -> dict:
    journal = _journal()
    try:
        items = journal.protocols()
        states = journal.protocol_task_states()
        steps = journal.protocol_steps()
        total = sum(len(protocol.get("tasks", [])) for protocol in items)
    finally:
        journal.close()
    _apply_task_progress(items, states, steps)
    tasks = _approved_tasks(items)
    events = _calendar_events()
    days = sorted({event["date"] for event in events})
    return {
        "tasks": tasks,
        "approved": len(tasks),
        "total": total,
        "events": events,
        "days": days,
        "protocols": {protocol["id"]: protocol for protocol in items},
    }


@app.get("/api/channels")
def channels() -> dict:
    return channels_status(settings)


@app.post("/api/channels/check")
def channels_check() -> dict:
    return channels_status(settings, check=True)


@app.get("/api/mailboxes")
def mailboxes() -> dict:
    return mailboxes_overview(settings)


@app.get("/api/mailboxes/unread")
def mailboxes_unread(period: str = "all") -> dict:
    return mailboxes_overview(settings, with_unread=True, period=_period(period))


@app.post("/api/mailboxes/check")
def mailboxes_check(period: str = "all") -> dict:
    data = mailboxes_overview(settings, with_unread=True, period=_period(period))
    data["channels"] = channels_status(settings, check=True)
    return data


def _mailbox_login_check(mailbox_id: str) -> dict:
    try:
        stats = Channels(settings).unread(mailbox_id, limit=0)
    except ChannelError as exc:
        text = str(exc)
        return {"ok": False, "detail": text.split(": ", 1)[-1] if text.startswith("mail_") else text}
    return {"ok": True, "unread": stats["total"], "today": stats["today"]}


@app.post("/api/mailboxes")
def mailbox_add(payload: MailboxRequest) -> dict:
    try:
        mb = mailbox_store(settings).add(
            payload.address,
            payload.password,
            provider=payload.provider,
            label=payload.label,
            from_name=payload.from_name,
            imap_host=payload.imap_host,
            imap_port=payload.imap_port or None,
            smtp_host=payload.smtp_host,
            smtp_port=payload.smtp_port or None,
        )
    except MailboxError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    journal = _journal()
    try:
        journal.log("mailbox_add", {"id": mb.id, "provider": mb.provider})
    finally:
        journal.close()
    return {"ok": True, "mailbox": mb.public(), "check": _mailbox_login_check(mb.id), **mailboxes_overview(settings)}


@app.post("/api/mailboxes/{mailbox_id}/password")
def mailbox_password(mailbox_id: str, payload: MailboxPasswordRequest) -> dict:
    try:
        mb = mailbox_store(settings).update_password(mailbox_id, payload.password)
    except MailboxError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if mb is None:
        raise HTTPException(status_code=404, detail="Ящик не найден")
    journal = _journal()
    try:
        journal.log("mailbox_password", {"id": mb.id})
    finally:
        journal.close()
    return {"ok": True, "mailbox": mb.public(), "check": _mailbox_login_check(mb.id), **mailboxes_overview(settings)}


@app.delete("/api/mailboxes/{mailbox_id}")
def mailbox_delete(mailbox_id: str) -> dict:
    if mailbox_id in {"a", "b"}:
        raise HTTPException(status_code=400, detail="Этот ящик задан в .env — уберите его там")
    if not mailbox_store(settings).remove(mailbox_id):
        raise HTTPException(status_code=404, detail="Ящик не найден")
    journal = _journal()
    try:
        journal.log("mailbox_remove", {"id": mailbox_id})
    finally:
        journal.close()
    return {"ok": True, **mailboxes_overview(settings)}


@app.get("/api/mailboxes/{mailbox_id}/messages")
def mailbox_messages(mailbox_id: str, limit: int = 20, period: str = "all") -> dict:
    ch = Channels(settings)
    if mailbox_id not in ch.accounts:
        raise HTTPException(status_code=404, detail="Ящик не найден")
    try:
        data = ch.unread(mailbox_id, limit=max(1, min(limit, 50)), period=_period(period))
    except ChannelError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"id": mailbox_id, **data}


@app.post("/api/mailboxes/drafts")
def mailbox_drafts(payload: MailboxDraftsRequest) -> dict:
    journal = _journal()
    try:
        result = run_live(settings, journal, labels=payload.ids or None, unseen_only=True)
    except ChannelError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    finally:
        journal.close()
    if result is None:
        return {"run_id": None, "new_emails": 0, "skipped": True, "reason": "no_unread_mail"}
    return {
        "run_id": result.run_id,
        "source": "live",
        "new_emails": result.metrics["emails"],
        "profile": {"name": result.profile.name, "role": result.profile.role},
        "metrics": result.metrics,
        "files": result.files,
    }


@app.post("/api/zoom/meeting")
def zoom_meeting(payload: ZoomRequest) -> dict:
    channels = Channels(settings)
    try:
        result = channels.zoom.create_meeting(payload.topic, payload.start, payload.minutes, payload.agenda)
    except ZoomError as exc:
        code = 409 if str(exc) == "zoom: not configured" else 502
        raise HTTPException(status_code=code, detail=str(exc)) from None
    journal = _journal()
    try:
        journal.log("zoom_link", {"topic": result.get("topic", ""), "mode": result.get("mode", "")})
    finally:
        journal.close()
    return result


@app.post("/api/telemost/meeting")
def telemost_meeting(payload: TelemostRequest) -> dict:
    channels = Channels(settings)
    try:
        result = channels.telemost.create_meeting(payload.topic, payload.start)
    except TelemostError as exc:
        text = str(exc)
        if text == "telemost: not configured":
            code = 409
        elif "start time" in text:
            code = 400
        else:
            code = 502
        raise HTTPException(status_code=code, detail=text) from None
    journal = _journal()
    try:
        journal.log("telemost_link", {"topic": result.get("topic", ""), "mode": result.get("mode", "")})
    finally:
        journal.close()
    return result


@app.get("/api/zoom/login")
def zoom_login() -> dict:
    channels = Channels(settings)
    try:
        url = authorize_url(channels.zoom.config)
    except ZoomError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"url": url}


@app.get("/api/zoom/callback")
def zoom_callback(code: str = "", state: str = "", error: str = "") -> Response:
    if error or not code or not check_state(state):
        return RedirectResponse("/?zoom=error")
    channels = Channels(settings)
    try:
        result = channels.zoom.login(code)
    except ZoomError:
        return RedirectResponse("/?zoom=error")
    journal = _journal()
    try:
        journal.log("zoom_login", {"name": result.get("name", "")})
    finally:
        journal.close()
    return RedirectResponse("/?zoom=ok")


@app.post("/api/zoom/logout")
def zoom_logout() -> dict:
    channels = Channels(settings)
    channels.zoom.logout()
    journal = _journal()
    try:
        journal.log("zoom_logout", {})
    finally:
        journal.close()
    return channels_status(settings)


@app.post("/api/channels/links")
def channel_link(payload: ChannelLinkRequest) -> dict:
    try:
        value = set_link(settings, payload.provider, payload.link)
    except LinkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    journal = _journal()
    try:
        journal.log(
            "channel_link",
            {"provider": payload.provider, "action": "saved" if value else "removed"},
        )
    finally:
        journal.close()
    return channels_status(settings)


def _tg_user_code(exc: TelegramUserError) -> int:
    text = str(exc)
    return 409 if ("not configured" in text or "not authorized" in text) else 502


@app.get("/api/telegram/user")
def telegram_user_status() -> dict:
    return dict(Channels(settings).tg_user.config.public)


@app.get("/api/telegram/user/unread")
def telegram_user_unread(limit: int = 20, period: str = "all") -> dict:
    try:
        return Channels(settings).tg_user.unread(limit, _period(period))
    except TelegramUserError as exc:
        raise HTTPException(status_code=_tg_user_code(exc), detail=str(exc)) from None


@app.post("/api/telegram/user/drafts")
def telegram_user_drafts(limit: int = 5, period: str = "all") -> dict:
    try:
        result = Channels(settings).tg_user.drafts(limit, _period(period))
    except TelegramUserError as exc:
        raise HTTPException(status_code=_tg_user_code(exc), detail=str(exc)) from None
    journal = _journal()
    try:
        journal.log("tg_drafts", {"dialogs": result["dialogs"], "drafts": len(result["drafts"])})
    finally:
        journal.close()
    return result


@app.get("/api/chat")
def chat_threads(period: str = "today") -> dict:
    store = _chat_store()
    try:
        return store.threads(_period(period))
    finally:
        store.close()


@app.get("/api/chat/messages")
def chat_messages(employee: str, period: str = "today", limit: int = 100) -> dict:
    store = _chat_store()
    try:
        return store.thread(employee, _period(period), limit=max(1, min(limit, 200)))
    except ChatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    finally:
        store.close()


@app.post("/api/chat/messages")
def chat_message_add(payload: ChatMessageRequest) -> dict:
    store = _chat_store()
    try:
        row = store.add(payload.employee, payload.text, role=payload.role, source=payload.source)
    except ChatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    finally:
        store.close()
    journal = _journal()
    try:
        journal.log("chat_message", {"employee": row["employee"], "source": row["source"]})
    finally:
        journal.close()
    return {"ok": True, "message": row}


@app.post("/api/chat/reply")
def chat_reply(payload: ChatReplyRequest) -> dict:
    store = _chat_store()
    try:
        row = store.reply(payload.employee, payload.text)
    except ChatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    finally:
        store.close()
    journal = _journal()
    try:
        journal.log("chat_reply", {"employee": row["employee"]})
    finally:
        journal.close()
    return {"ok": True, "message": row}


@app.post("/api/chat/read")
def chat_read(employee: str = "") -> dict:
    store = _chat_store()
    try:
        marked = store.mark_read(employee.strip())
    finally:
        store.close()
    journal = _journal()
    try:
        journal.log("chat_read", {"employee": employee.strip(), "marked": marked})
    finally:
        journal.close()
    return {"ok": True, "marked": marked}


@app.post("/api/chat/drafts")
def chat_drafts(period: str = "today", employee: str = "") -> dict:
    store = _chat_store()
    try:
        data = store.drafts(_period(period), employee.strip())
    finally:
        store.close()
    journal = _journal()
    try:
        journal.log("chat_drafts", {"drafts": len(data["drafts"]), "unread": data["total"]})
    finally:
        journal.close()
    return data


@app.get("/api/journal.csv")
def journal_csv() -> Response:
    journal = _journal()
    try:
        events = journal.events(limit=500)
    finally:
        journal.close()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["at", "kind", "details"])
    for event in reversed(events):
        payload = event.get("payload") or {}
        detail = ", ".join(
            f"{key}={value}" for key, value in payload.items() if not isinstance(value, (list, dict))
        )
        writer.writerow([event["at"], event["kind"], detail])
    return Response(
        "\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="asistant-journal.csv"'},
    )


@app.get("/api/metrics")
def metrics() -> dict:
    journal = _journal()
    try:
        data = journal.run_metrics() or {}
    finally:
        journal.close()
    return data


@app.get("/api/economics")
def economics() -> dict:
    journal = _journal()
    try:
        data = (journal.run_metrics() or {}).get("economics", {})
    finally:
        journal.close()
    return data


@app.get("/api/journal")
def journal_view(limit: int = 50) -> dict:
    journal = _journal()
    try:
        return {"runs": journal.runs(), "events": journal.events(limit)}
    finally:
        journal.close()


def _meeting_payload(employee: str = "") -> dict:
    journal = _journal()
    try:
        run_id = journal.last_run()
        if not run_id:
            raise HTTPException(status_code=409, detail="meeting: run the week first")
        metrics = journal.run_metrics(run_id) or {}
        protocols = journal.protocols(run_id)
        progress = journal.protocol_progress(run_id)
    finally:
        journal.close()
    attendee = None
    messages: list[dict] = []
    draft: dict = {}
    who = employee.strip()
    if who:
        store = _chat_store()
        try:
            threads = store.threads("all").get("threads") or []
            person = next((item for item in threads if str(item.get("employee") or "") == who), None)
            if person is None:
                raise HTTPException(status_code=400, detail="meeting: employee not found")
            thread = store.thread(who, "all", limit=200)
        except ChatError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        finally:
            store.close()
        attendee = {
            "name": who,
            "position": str(person.get("position") or ""),
            "role": str(person.get("role") or ""),
        }
        messages = thread.get("messages") or []
        draft = thread.get("draft") or {}
    digest = _read_json(settings.out_dir / "weekly_digest.json")
    payload = build_meeting(
        metrics=metrics,
        protocols=protocols,
        progress=progress.get("overall", {}),
        digest=digest,
        profile=str(metrics.get("profile_name") or ""),
        role=str(metrics.get("profile_role") or ""),
        employee=attendee,
        chat_messages=messages,
        chat_draft=draft,
    )
    channels = channels_status(settings)
    zoom = channels.get("zoom", {})
    payload["zoom"] = {
        "configured": bool(zoom.get("configured")),
        "link_set": bool(zoom.get("link_set")),
        "mode": str(zoom.get("mode") or ""),
    }
    telemost = channels.get("telemost", {})
    payload["telemost"] = {
        "configured": bool(telemost.get("configured")),
        "link_set": bool(telemost.get("link_set")),
        "mode": str(telemost.get("mode") or ""),
    }
    return payload


@app.get("/api/meeting")
def meeting_view(employee: str = "") -> dict:
    return _meeting_payload(employee)


@app.post("/api/avatar/say")
def avatar_say(payload: SayRequest) -> Response:
    meeting = _meeting_payload(payload.employee)
    allowed = {meeting["intro"], meeting["outro"]} | {item["say"] for item in meeting["items"]}
    text = voice.clean(payload.text)
    if text not in allowed:
        raise HTTPException(status_code=403, detail="meeting: line outside the script")
    try:
        path = voice.synthesize(settings, text)
    except voice.VoiceError:
        raise HTTPException(status_code=503, detail="voice unavailable") from None
    return FileResponse(
        path,
        media_type="audio/wav",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.post("/api/meeting/done")
def meeting_done(payload: MeetingDoneRequest) -> dict:
    meeting = _meeting_payload(payload.employee)
    agenda = meeting.get("items") or []
    covered = {index for index in payload.covered if 0 <= index < len(agenda)}
    if not covered and payload.items:
        covered = set(range(min(payload.items, len(agenda))))
    channel = payload.channel if payload.channel in {"zoom", "telemost"} else ""
    attendee = meeting.get("attendee") or {}
    finished = datetime.now()
    started = finished - timedelta(seconds=payload.seconds)
    minute = {
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": finished.isoformat(timespec="seconds"),
        "seconds": payload.seconds,
        "profile": meeting.get("profile", ""),
        "role": meeting.get("role", ""),
        "employee": str(attendee.get("name") or ""),
        "employee_position": str(attendee.get("position") or ""),
        "week": meeting.get("week", ""),
        "subtitle": meeting.get("subtitle", ""),
        "channel": channel,
        "items": [
            {
                "index": index + 1,
                "id": item["id"],
                "title": item["title"],
                "fact": item["fact"],
                "covered": index in covered,
            }
            for index, item in enumerate(agenda)
        ],
    }
    journal = _journal()
    try:
        minute_id = journal.save_minutes(minute)
        journal.log(
            "meeting",
            {"items": len(covered), "seconds": payload.seconds, "channel": channel, "minute_id": minute_id},
        )
        events = journal.events(limit=8)
    finally:
        journal.close()
    return {
        "ok": True,
        "id": minute_id,
        "covered": len(covered),
        "seconds": payload.seconds,
        "channel": channel,
        "events": events,
    }


@app.get("/api/minutes")
def minutes_view(limit: int = 20) -> dict:
    journal = _journal()
    try:
        return {"minutes": journal.minutes(limit)}
    finally:
        journal.close()


@app.get("/api/minutes/{minute_id}.md")
def minute_markdown(minute_id: int) -> Response:
    journal = _journal()
    try:
        minute = journal.minute(minute_id)
    finally:
        journal.close()
    if not minute:
        raise HTTPException(status_code=404, detail="minutes not found")
    return Response(
        content=minutes_markdown(minute),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="protocol-{minute_id}.md"'},
    )


@app.get("/api/minutes/{minute_id}")
def minute_view(minute_id: int) -> dict:
    journal = _journal()
    try:
        minute = journal.minute(minute_id)
    finally:
        journal.close()
    if not minute:
        raise HTTPException(status_code=404, detail="minutes not found")
    return minute


@app.post("/api/minutes/{minute_id}/tasks")
def minute_task_add(minute_id: int, payload: MeetingTaskRequest) -> dict:
    journal = _journal()
    try:
        task = journal.add_meeting_task(
            minute_id,
            payload.item_index,
            payload.title.strip(),
            payload.owner.strip(),
            payload.due.strip(),
        )
        if task is None:
            raise HTTPException(status_code=404, detail="minutes not found")
        journal.log(
            "meeting_task",
            {
                "minute_id": minute_id,
                "task_id": task["id"],
                "item_index": task["item_index"],
                "owner_set": bool(task["owner"]),
                "due_set": bool(task["due"]),
            },
        )
    finally:
        journal.close()
    return {"ok": True, "task": task}


@app.post("/api/minutes/{minute_id}/tasks/{task_id}/state")
def minute_task_state(minute_id: int, task_id: int, payload: MeetingTaskStateRequest) -> dict:
    if payload.status not in {"open", "done"}:
        raise HTTPException(status_code=400, detail="task: status must be open or done")
    journal = _journal()
    try:
        task = journal.set_meeting_task_state(minute_id, task_id, payload.status)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        journal.log(
            "meeting_task_state",
            {"minute_id": minute_id, "task_id": task_id, "status": payload.status},
        )
    finally:
        journal.close()
    return {"ok": True, "task": task}


_LOCAL_HOSTS = {"127.0.0.1", "localhost"}
_JOIN_PROVIDERS = {"zoom", "telemost"}


def _local_request(request: Request) -> bool:
    host = (request.headers.get("host") or "").split(":")[0].lower()
    return host in _LOCAL_HOSTS


@app.post("/api/meeting/join")
def meeting_join(payload: MeetingJoinRequest, request: Request) -> dict:
    local = _local_request(request)
    try:
        url = set_link(settings, payload.provider, payload.link) if local else normalize(payload.provider, payload.link)
    except LinkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    journal = _journal()
    try:
        journal.log("meeting_join", {"provider": payload.provider, "saved": local})
    finally:
        journal.close()
    return {
        "ok": True,
        "provider": payload.provider,
        "url": url,
        "saved": local,
        "channels": channels_status(settings),
    }


@app.get("/api/join/{provider}")
def join_room(provider: str, request: Request) -> Response:
    if provider not in _JOIN_PROVIDERS or not _local_request(request):
        raise HTTPException(status_code=404, detail="not found")
    link = load_zoom(settings).personal_link if provider == "zoom" else load_telemost(settings).link
    try:
        url = normalize(provider, link)
    except LinkError:
        url = ""
    if not url:
        raise HTTPException(status_code=404, detail=f"{provider} room not configured")
    journal = _journal()
    try:
        journal.log("meeting_join", {"provider": provider, "saved": True})
    finally:
        journal.close()
    return RedirectResponse(url, status_code=307)


@app.get("/api/zoom/open")
def zoom_open(request: Request) -> Response:
    if not _local_request(request):
        raise HTTPException(status_code=404, detail="not found")
    link = load_zoom(settings).personal_link or ""
    if not link:
        raise HTTPException(status_code=404, detail="zoom room not configured")
    return RedirectResponse(link, status_code=307)


def _zoom_app_link(url: str) -> str:
    parts = urlsplit(str(url or "").strip())
    if not parts.scheme.startswith("http") or "zoom" not in parts.netloc.lower():
        return ""
    segments = [segment for segment in parts.path.split("/") if segment]
    confno = ""
    if "j" in segments:
        index = segments.index("j")
        if index + 1 < len(segments):
            confno = segments[index + 1]
    elif "wc" in segments:
        rest = [segment for segment in segments[segments.index("wc") + 1:] if segment != "join"]
        if rest:
            confno = rest[-1]
    if not confno.isdigit():
        return ""
    query = {"action": "join", "confno": confno}
    pwd = (parse_qs(parts.query).get("pwd") or [""])[0]
    if pwd:
        query["pwd"] = pwd
    return "zoommtg://zoom.us/join?" + urlencode(query)


@app.get("/api/zoom/app")
def zoom_app(request: Request) -> Response:
    if not _local_request(request):
        raise HTTPException(status_code=404, detail="not found")
    link = load_zoom(settings).personal_link or ""
    deep = _zoom_app_link(link)
    if not deep:
        raise HTTPException(status_code=404, detail="zoom app link unavailable")
    journal = _journal()
    try:
        journal.log("meeting_join", {"provider": "zoom", "saved": True, "app": True})
    finally:
        journal.close()
    return RedirectResponse(deep, status_code=307)


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return HTML_PAGE


ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_ASSET_NAMES = {"asya.jpg": "image/jpeg", "ivan-avatar.png": "image/png"}


@app.get("/assets/{name}")
def asset(name: str) -> Response:
    media_type = _ASSET_NAMES.get(name)
    path = ASSETS_DIR / name
    if not media_type or not path.is_file():
        raise HTTPException(status_code=404, detail="asset not found")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-cache"})


def _icon_response(name: str, media_type: str) -> Response:
    path = ASSETS_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="icon not found")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/favicon.svg")
def favicon_svg() -> Response:
    return _icon_response("favicon.svg", "image/svg+xml")


@app.get("/favicon.ico")
def favicon_ico() -> Response:
    return _icon_response("favicon.ico", "image/x-icon")


@app.get("/apple-touch-icon.png")
def apple_touch_icon() -> Response:
    return _icon_response("apple-touch-icon.png", "image/png")


HTML_PAGE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AsiStantAI · Ася</title>
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="theme-color" content="#B4552D">
<style>
  :root{
    --cream:#F8F1E7; --cream2:#F1E5D4; --paper:#FFFCF6; --line:#E3D2BC;
    --terra:#B4552D; --terra-dark:#8E3F20; --chest:#3E2A20; --gold:#C79A43;
    --sage:#66795C; --muted:#8A7461;
  }
  *{box-sizing:border-box}
  body{
    margin:0; background:
      radial-gradient(1100px 500px at 12% -10%, #FBF4E8 0%, transparent 60%),
      radial-gradient(900px 420px at 95% 0%, #F5E7D2 0%, transparent 55%),
      var(--cream);
    color:var(--chest); font:15px/1.55 -apple-system,"Segoe UI",Roboto,Arial,sans-serif;
  }
  header{
    display:flex; align-items:center; justify-content:space-between; gap:16px;
    padding:20px 32px; border-bottom:1px solid var(--line);
    background:linear-gradient(180deg,rgba(255,252,246,.95),rgba(255,252,246,.75));
    backdrop-filter:blur(6px); position:sticky; top:0; z-index:10;
  }
  .brand{display:flex; align-items:center; gap:14px}
  .face{
    width:46px;height:46px;border-radius:50%;object-fit:cover;object-position:center 22%;
    border:2px solid var(--paper);box-shadow:0 8px 18px rgba(142,63,32,.22);background:var(--cream2);
  }
  .leader{
    display:flex;align-items:center;gap:9px;padding:4px 13px 4px 4px;
    border:1px solid var(--line);border-radius:999px;background:var(--paper);
  }
  .leader .face{width:34px;height:34px;border:0;box-shadow:none;object-position:center 20%}
  .leader-init{
    width:34px;height:34px;border-radius:50%;display:grid;place-items:center;
    background:var(--cream2);color:var(--terra-dark);font:600 12px/1 Georgia,serif;
  }
  .leader-init[hidden]{display:none}
  .leader-text b{display:block;font:600 13px/1.15 Georgia,serif;color:var(--chest)}
  .leader-text span{display:block;font-size:10.5px;color:var(--muted);letter-spacing:.05em;text-transform:uppercase}
  .brand h1{font:600 21px/1.1 Georgia,serif;margin:0;letter-spacing:.01em}
  .brand p{margin:2px 0 0;color:var(--muted);font-size:12.5px;letter-spacing:.04em;text-transform:uppercase}
  .controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  select,button{font:inherit;color:inherit}
  select{
    background:var(--paper);border:1px solid var(--line);border-radius:999px;
    padding:9px 14px;min-width:180px;
  }
  .btn{
    border:1px solid var(--terra);background:transparent;color:var(--terra-dark);
    border-radius:999px;padding:9px 16px;cursor:pointer;transition:.18s;
  }
  .btn:hover{background:rgba(180,85,45,.08)}
  .btn.primary{background:var(--terra);color:#FFF8EF;box-shadow:0 6px 14px rgba(180,85,45,.28)}
  .btn.primary:hover{background:var(--terra-dark)}
  .btn.ghost{border-color:var(--line);color:var(--muted)}
  .btn:disabled{opacity:.45;cursor:default}
  .shell{
    display:grid;grid-template-columns:208px minmax(0,1fr);gap:24px;
    max-width:1500px;margin:0 auto;padding:22px 28px 48px;align-items:start;
  }
  main{min-width:0}
  #secMail,#secMessages,#secMeetings,#secTasks,#secCrm{scroll-margin-top:96px}
  .rail{
    position:sticky;top:96px;display:flex;flex-direction:column;gap:3px;
    background:linear-gradient(180deg,var(--paper),#FFF9EF);
    border:1px solid var(--line);border-radius:18px;padding:12px;
    box-shadow:0 10px 24px rgba(62,42,32,.05);
  }
  .rail-title{margin:2px 8px 8px;font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
  .rail-item{
    display:flex;align-items:center;gap:10px;width:100%;
    border:0;background:transparent;border-radius:12px;padding:9px 10px;
    cursor:pointer;color:var(--chest);font:14px inherit;text-align:left;transition:.15s;
  }
  .rail-item svg{width:18px;height:18px;flex:none;color:var(--terra)}
  .rail-item span{flex:1}
  .rail-badge{
    min-width:20px;padding:1px 7px;border-radius:999px;background:var(--cream2);
    color:var(--terra-dark);font:600 11px/1.6 inherit;text-align:center;
  }
  .rail-badge[hidden]{display:none}
  .rail-item:hover{background:rgba(180,85,45,.07)}
  .rail-item.on{background:var(--chest);color:#FBF3E6}
  .rail-item.on svg{color:#F0C98A}
  .rail-item.on .rail-badge{background:rgba(255,255,255,.16);color:#FBF3E6}
  .rail-foot{margin-top:10px;padding:10px 8px 2px;border-top:1px solid var(--line);font-size:11px;color:var(--muted);line-height:1.5}
  .eyebrow{color:var(--gold);font-size:12px;letter-spacing:.14em;text-transform:uppercase;margin:0 0 6px}
  .hero h2{font:600 30px/1.2 Georgia,serif;margin:0 0 2px}
  .hero .sub{color:var(--muted);margin:0 0 22px}
  .kpis{display:grid;grid-template-columns:repeat(7,1fr);gap:10px;margin-bottom:26px}
  .kpi{
    background:var(--paper);border:1px solid var(--line);border-radius:16px;padding:14px 16px;
    box-shadow:0 1px 0 #fff inset,0 8px 18px rgba(62,42,32,.04);
  }
  .kpi b{display:block;font:600 26px/1.15 Georgia,serif;color:var(--terra-dark)}
  .kpi span{color:var(--muted);font-size:12px;letter-spacing:.02em}
  .kpi.gold b{color:var(--gold)}
  .kpi.sage b{color:var(--sage)}
  .grid{display:grid;grid-template-columns:1.55fr .95fr;gap:20px;align-items:start}
  .panel{
    background:var(--paper);border:1px solid var(--line);border-radius:18px;padding:18px 20px;
    box-shadow:0 10px 24px rgba(62,42,32,.05);margin-bottom:20px;
  }
  .panel h3{font:600 18px/1.2 Georgia,serif;margin:0 0 12px}
  aside .panel{padding:16px 18px}
  aside .panel h3{font-size:16px;margin-bottom:10px}
  .panel-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;margin-bottom:14px}
  .panel-head h3{margin:0}
  .tabs{display:flex;gap:6px;flex-wrap:wrap}
  .tab{
    border:1px solid var(--line);background:transparent;border-radius:999px;
    padding:5px 11px;font-size:12.5px;color:var(--muted);cursor:pointer;
  }
  .tab.on{background:var(--chest);border-color:var(--chest);color:#FBF3E6}
  .search{
    width:100%;margin:0 0 12px;border:1px solid var(--line);border-radius:10px;
    padding:7px 11px;font:13px inherit;background:#FFFDF8;color:var(--chest);
  }
  .search:focus{outline:2px solid #EBC7AF;outline-offset:1px}
  .panel h3 a{font:500 12px Georgia,serif;color:var(--terra-dark);text-decoration:none;margin-left:8px}
  .panel h3 a:hover{text-decoration:underline}
  .cards{display:flex;flex-direction:column;gap:12px;max-height:1180px;overflow:auto;padding-right:4px}
  .card{border:1px solid var(--line);border-radius:14px;padding:14px 16px;background:#FFFDF9}
  .card.approved{border-color:#BFCDB4;background:#FBFFF7}
  .card.rejected{opacity:.6}
  .card .row1{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
  .pill{font-size:11.5px;letter-spacing:.03em;border-radius:999px;padding:3px 9px;border:1px solid var(--line);color:var(--muted)}
  .pill.ask{background:#FBEDE4;border-color:#EBC7AF;color:var(--terra-dark)}
  .pill.delegate{background:#EFF3E9;border-color:#CBD8BF;color:#4F6246}
  .pill.auto{background:#F7F0DC;border-color:#E6D3A5;color:#8A6A22}
  .pill.digest{background:#F2EEE8;color:var(--muted)}
  .card h4{font:600 16.5px/1.35 Georgia,serif;margin:0 0 4px}
  .meta{color:var(--muted);font-size:12.5px}
  .body{
    white-space:pre-wrap;font-size:13.5px;line-height:1.55;margin:10px 0 8px;
    background:#FFF8EE;border-left:3px solid var(--gold);border-radius:0 8px 8px 0;padding:9px 12px;
  }
  .sources{margin:8px 0 10px;padding:0;list-style:none;font-size:12.5px;color:var(--muted)}
  .sources li{padding:3px 0 3px 16px;position:relative}
  .sources li:before{content:"§";position:absolute;left:0;color:var(--gold)}
  .actions{display:flex;gap:8px;flex-wrap:wrap}
  .actions .btn{padding:6px 12px;font-size:13px}
  textarea{
    width:100%;min-height:150px;border:1px solid var(--line);border-radius:10px;padding:10px;
    font:13.5px/1.5 inherit;background:#FFFDF8;color:var(--chest);resize:vertical;margin:8px 0;
  }
  .list{list-style:none;margin:0;padding:0}
  .list li{padding:7px 0;border-bottom:1px dashed var(--line);font-size:13.5px}
  .list li:last-child{border-bottom:0}
  .list .when{color:var(--terra-dark);font-variant-numeric:tabular-nums;margin-right:8px}
  .list .protected{color:var(--terra);font-weight:600}
  .mini{color:var(--muted);font-size:12px}
  .econ-line{display:flex;justify-content:space-between;gap:10px;padding:5px 0;font-size:13.5px}
  .econ-line b{font-variant-numeric:tabular-nums}
  .econ-total{border-top:1px solid var(--line);margin-top:6px;padding-top:8px;font-size:15px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
  th{color:var(--muted);font-weight:500;font-size:12px;letter-spacing:.04em;text-transform:uppercase}
  tr.miss td{background:#FDF0E8}
  .ok{color:var(--sage);font-weight:600}
  .bad{color:var(--terra);font-weight:600}
  .proto{border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:12px;background:#FFFDF9}
  .proto h4{font:600 15.5px/1.3 Georgia,serif;margin:0 0 6px}
  .proto ul{margin:6px 0 0;padding-left:0;list-style:none;font-size:13px}
  .proto li{display:flex;gap:10px;align-items:flex-start;justify-content:space-between;padding:5px 0;border-bottom:1px dashed var(--line)}
  .proto li:last-child{border-bottom:0}
  .proto li.task{display:block;padding:9px 0}
  .proto details{margin:6px 0 0;font-size:12.5px;color:var(--chest)}
  .proto details summary{cursor:pointer;color:var(--muted);font-weight:600;padding:3px 0}
  .proto details ol,.proto details ul{margin:4px 0 4px 18px;padding:0;list-style:disc;font-size:12.5px}
  .proto details li{display:list-item;border:0;padding:2px 0;justify-content:initial}
  .proto details p{margin:4px 0;line-height:1.5}
  .proto .txt{flex:1}
  .proto .btn{padding:3px 9px;font-size:11.5px;white-space:nowrap}
  .proto .done{color:var(--sage);font-size:11.5px;font-weight:600;white-space:nowrap}
  .task-top{display:flex;gap:10px;align-items:flex-start;justify-content:space-between}
  .task-badges{display:flex;gap:5px;flex-wrap:wrap;margin-top:5px}
  .task-badges .pill{white-space:normal}
  .task-actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:7px}
  .task-actions .btn{padding:3px 9px;font-size:11.5px}
  .status-waiting{background:#F2EEE8;color:var(--muted)}
  .status-approved{background:#F7F0DC;border-color:#E6D3A5;color:#8A6A22}
  .status-in_progress{background:#EFF3E9;border-color:#CBD8BF;color:#4F6246}
  .status-done{background:#E7F0E2;border-color:#BFCDB4;color:#47623C}
  .status-open{background:#FBEDE4;border-color:#EBC7AF;color:var(--terra-dark)}
  .task-note{width:100%;border:1px solid var(--line);border-radius:8px;padding:5px 9px;font:12.5px inherit;background:#FFFDF8;color:var(--chest);margin-top:6px}
  .task-form{display:grid;grid-template-columns:1.4fr 1fr .9fr .9fr;gap:6px;margin-top:8px}
  .task-form input,.task-form textarea,.task-form select{
    border:1px solid var(--line);border-radius:8px;padding:5px 9px;font:12.5px inherit;
    background:#FFFDF8;color:var(--chest);width:100%;
  }
  .task-form textarea{grid-column:1/-1;min-height:52px;resize:vertical}
  .task-form .wide{grid-column:1/-1}
  .task-form .btn{justify-self:start}
  .board-stats{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px}
  .board-stats .pill{background:var(--cream2);border-color:var(--line)}
  .board{display:flex;flex-direction:column;gap:8px}
  .board-row{
    border:1px solid var(--line);border-radius:12px;padding:10px 12px;background:#FFFDF9;
    display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px 12px;align-items:start;
  }
  .board-row .txt{font-size:13.5px;line-height:1.45}
  .board-row .mini{font-size:11.5px}
  .board-row .task-actions{margin-top:0;justify-content:flex-end}
  .board-row.done{opacity:.62}
  .crm-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
  .crm-card{border:1px solid var(--line);border-radius:14px;padding:12px 14px;background:#FFFDF9}
  .crm-head{display:flex;align-items:flex-start;gap:10px}
  .crm-ava{
    width:36px;height:36px;border-radius:50%;flex:none;display:grid;place-items:center;
    background:var(--cream2);color:var(--terra-dark);font:600 14px/1 Georgia,serif;
  }
  .crm-head b{display:block;font:600 14px/1.3 Georgia,serif}
  .crm-head .mini{margin-top:1px;word-break:break-all}
  .crm-card .crm-sub{margin-top:8px;font-size:12.5px;font-style:italic;color:var(--chest)}
  .crm-card .crm-meta{margin-top:6px;font-size:11.5px;color:var(--muted)}
  .crm-card .task-actions{margin-top:9px}
  .crm-card .task-actions .btn{padding:3px 9px;font-size:11.5px}
  .crm-internal{margin-top:14px}
  .crm-internal summary{cursor:pointer;color:var(--muted);font-size:12.5px;font-weight:600}
  .crm-internal .crm-grid{margin-top:10px}
  .steps{margin:9px 0 0;padding:0 0 0 10px;border-left:2px solid var(--line);font-size:12.5px}
  .step{padding:5px 0;border-bottom:1px dashed var(--line)}
  .step:last-child{border-bottom:0}
  .step-head{display:flex;gap:8px;align-items:flex-start;justify-content:space-between}
  .step .role{color:var(--terra-dark);font-weight:600}
  .step .step-actions{display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end}
  .step .step-actions .btn{padding:2px 7px;font-size:11px}
  .steps-meta{color:var(--muted);font-size:11.5px;margin-top:2px}
  .preset-row{display:flex;gap:6px;flex-wrap:wrap;margin-top:7px}
  .preset-row .btn{padding:4px 9px;font-size:11.5px}
  .revised{color:var(--gold);font-weight:600}
  .synced{color:var(--sage);font-weight:600}
  .proto-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}
  .proto-head .btn{padding:4px 10px;font-size:12px;white-space:nowrap}
  .channels{display:flex;flex-direction:column;gap:7px}
  .ch{display:flex;justify-content:space-between;gap:8px;align-items:center;font-size:13px}
  .badge{font-size:11px;border-radius:999px;padding:2px 8px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
  .badge.ok{background:#EFF3E9;border-color:#CBD8BF;color:#4F6246}
  .badge.warn{background:#FBEDE4;border-color:#EBC7AF;color:var(--terra-dark)}
  .mail-item{padding:6px 0;border-bottom:1px solid var(--line);font-size:12.5px}
  .mail-item .mini{font-size:11px}
  details summary{cursor:pointer;color:var(--muted);font-size:13px}
  footer{color:var(--muted);font-size:12.5px;text-align:center;padding:0 32px 40px}
  .empty{padding:28px;text-align:center;color:var(--muted)}
  .toast{
    position:fixed;right:24px;bottom:24px;background:var(--chest);color:#FBF3E6;
    padding:11px 16px;border-radius:12px;font-size:13.5px;box-shadow:0 12px 26px rgba(62,42,32,.28);
    opacity:0;transform:translateY(8px);transition:.25s;pointer-events:none;max-width:340px
  }
  .toast.on{opacity:1;transform:none}
  .cal-gate{
    position:fixed;inset:0;background:rgba(62,42,32,.38);backdrop-filter:blur(4px);
    display:flex;align-items:center;justify-content:center;padding:24px;z-index:40;
  }
  .cal-gate[hidden]{display:none}
  .cal-sheet{
    background:var(--paper);border:1px solid var(--line);border-radius:20px;
    box-shadow:0 24px 60px rgba(62,42,32,.28);width:min(1100px,100%);
    max-height:88vh;overflow:auto;padding:22px 24px;
  }
  .cal-head{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;margin-bottom:14px}
  .cal-head h3{font:600 22px/1.2 Georgia,serif;margin:0}
  .cal-week{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}
  .cal-day{background:#FFFDF9;border:1px solid var(--line);border-radius:12px;padding:10px;min-height:170px}
  .cal-day h4{font:600 13px/1.2 Georgia,serif;margin:0 0 8px;color:var(--terra-dark)}
  .cal-ev{display:block;width:100%;text-align:left;font:inherit;color:inherit;cursor:pointer;
    border:0;border-left:3px solid var(--gold);background:#FFF8EE;border-radius:0 7px 7px 0;
    padding:5px 8px;margin-bottom:6px;font-size:12px;line-height:1.35}
  .cal-ev:hover,.cal-ev:focus-visible{background:#FBEFD9;border-left-color:var(--terra);outline:none}
  .cal-ev.protected{border-left-color:var(--terra);background:#FBEDE4}
  .cal-ev.protected:hover,.cal-ev.protected:focus-visible{background:#F7E2D6}
  .cal-ev time{display:block;color:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
  .cal-ev b{font-weight:600;font-size:12px}
  .cal-ev span{display:block;color:var(--muted);font-size:10.5px;margin-top:1px}
  .cal-tasks{margin-top:16px;border-top:1px solid var(--line);padding-top:12px}
  .cal-tasks h4{font:600 15px Georgia,serif;margin:0 0 6px}
  .meet-top{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:0 0 8px}
  .meet-block{border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:0 0 12px;background:#FFFDF9}
  .meet-block h4{font:600 15.5px/1.3 Georgia,serif;margin:0 0 4px}
  .meet-open{margin-top:6px;background:none;border:0;padding:0;font:600 12.5px/1.3 inherit;
    color:var(--terra-dark);border-bottom:1px dashed currentColor;cursor:pointer}
  .meet-q{margin:6px 0 0;padding-left:18px;font-size:13px;line-height:1.5}
  .meet-q li{margin:4px 0}
  .meet-body .proto details{margin-top:10px}
  .proto.flash{box-shadow:0 0 0 3px rgba(180,85,45,.32);transition:box-shadow .25s}
  @media (max-width:860px){.cal-week{grid-template-columns:1fr 1fr}}
  @media (max-width:560px){.cal-week{grid-template-columns:1fr}}
  .btn.meet{border-color:var(--gold);color:var(--chest);background:rgba(199,154,67,.14);display:inline-flex;align-items:center;gap:8px}
  .btn.meet:hover{background:rgba(199,154,67,.24)}
  .btn.meet::before{content:'';width:7px;height:7px;border-radius:50%;background:var(--terra);animation:mtgPulse 1.9s infinite}
  @keyframes mtgPulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.45;transform:scale(.72)}}
  .mtg{
    position:fixed;inset:0;z-index:60;color:#F7EBDD;display:flex;flex-direction:column;
    padding:18px 22px 20px;overflow:auto;
    background:
      radial-gradient(1200px 620px at 18% -12%, #4A3322 0%, transparent 55%),
      radial-gradient(900px 520px at 104% 104%, #3A2318 0%, transparent 52%),
      #20130D;
  }
  .mtg[hidden]{display:none}
  .mtg-bar{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:14px}
  .mtg-brand{display:flex;align-items:center;gap:12px}
  .mtg-dot{width:10px;height:10px;border-radius:50%;background:#D06A3C;box-shadow:0 0 0 4px rgba(208,106,60,.18);animation:mtgPulse 2s infinite}
  .mtg-brand b{display:block;font:600 17px/1.2 Georgia,serif;letter-spacing:.01em}
  .mtg-brand span{display:block;color:#C9AF96;font-size:12px;margin-top:2px}
  .mtg-bar-right{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .mtg-timer{font:600 15px/1 Consolas,monospace;color:#E9D3BA;background:rgba(255,248,239,.08);border:1px solid rgba(233,211,186,.22);padding:8px 12px;border-radius:999px}
  .mtg-who{max-width:240px;background:rgba(255,248,239,.08);border:1px solid rgba(233,211,186,.22);color:#E9D3BA;border-radius:999px;padding:8px 10px;font:500 12.5px/1.1 -apple-system,"Segoe UI",sans-serif}
  .mtg-who option{color:#3E2A20}
  .mtg-bar .btn,.mtg-controls .btn{border-color:rgba(233,211,186,.34);color:#F0DEC9}
  .mtg-bar .btn:hover,.mtg-controls .btn:hover{background:rgba(240,222,201,.1)}
  .mtg-bar .btn.primary,.mtg-controls .btn.primary{border-color:transparent;color:#FFF8EF}
  .mtg-body{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(300px,.85fr);gap:16px;flex:1;min-height:0}
  .mtg-stage{display:flex;flex-direction:column;gap:12px;min-height:0}
  .mtg-tile{position:relative;flex:1;min-height:320px;border-radius:22px;overflow:hidden;border:1px solid rgba(233,211,186,.18);
    background:linear-gradient(180deg,#FFF7EB,#F3E2CC);box-shadow:0 18px 50px rgba(0,0,0,.35)}
  .mtg-photo{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;object-position:center 20%;
    transition:transform .16s ease-out;will-change:transform}
  .mtg-rim{position:absolute;inset:0;pointer-events:none;border-radius:inherit;opacity:.25;
    box-shadow:inset 0 0 0 3px rgba(199,154,67,.5), inset 0 -150px 130px -90px rgba(62,42,32,.55);
    transition:opacity .15s linear}
  .mtg-pip{position:absolute;top:14px;right:14px;width:118px;height:158px;border-radius:14px;overflow:hidden;
    border:1px solid rgba(233,211,186,.5);box-shadow:0 12px 30px rgba(0,0,0,.35);background:#2A1810}
  .mtg-pip img{width:100%;height:100%;object-fit:cover;object-position:center 18%;display:block}
  .mtg-pip-ini{position:absolute;inset:0;display:grid;place-items:center;background:linear-gradient(160deg,#B4552D,#7C3A1E);color:#FFF8EF;font:600 34px/1 Georgia,serif;letter-spacing:.03em}
  .mtg-pip span{position:absolute;left:0;right:0;bottom:0;padding:4px 8px;background:rgba(32,19,13,.74);
    color:#F7EBDD;font:600 10.5px/1.3 -apple-system,"Segoe UI",sans-serif;letter-spacing:.02em}
  .mtg-badge{position:absolute;left:16px;bottom:14px;display:flex;align-items:center;gap:9px;
    background:rgba(32,19,13,.78);color:#F7EBDD;border:1px solid rgba(247,235,221,.16);
    padding:6px 12px 6px 7px;border-radius:999px;backdrop-filter:blur(4px)}
  .mtg-badge i{width:26px;height:26px;border-radius:50%;background:radial-gradient(circle at 35% 30%,#E8C09E,#B4552D);
    box-shadow:inset 0 0 0 1px rgba(0,0,0,.15)}
  .mtg-badge b{font:600 13px/1 Georgia,serif}
  .mtg-badge span{color:#CDB49B;font-size:11px}
  .mtg-wave{position:absolute;right:16px;bottom:16px;display:flex;align-items:flex-end;gap:3px;height:28px}
  .mtg-wave i{width:4px;height:5px;border-radius:2px;background:#8E3F20;transition:height .08s linear}
  .mtg-caption{background:rgba(255,251,244,.96);color:#3E2A20;border-radius:16px;padding:13px 16px;min-height:68px;
    font:15.5px/1.5 Georgia,serif;box-shadow:0 10px 26px rgba(0,0,0,.25)}
  .mtg-caption::before{content:'Ася';display:inline-block;font:600 10.5px/1 -apple-system,"Segoe UI",sans-serif;
    letter-spacing:.12em;color:#B4552D;text-transform:uppercase;margin-right:9px;transform:translateY(-1px)}
  .mtg-controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .mtg-controls .mini{color:#B79D84;font-size:11.5px;margin-left:auto}
  .mtg-agenda{background:rgba(255,248,239,.06);border:1px solid rgba(233,211,186,.18);border-radius:22px;
    padding:16px 16px 12px;display:flex;flex-direction:column;min-height:0}
  .mtg-agenda h4{font:600 16px/1.2 Georgia,serif;margin:0 0 10px;color:#F3E1CB}
  .mtg-agenda ol{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px;overflow:auto;flex:1}
  .mtg-item{display:flex;gap:10px;align-items:flex-start;padding:10px 11px;border-radius:13px;
    border:1px solid rgba(233,211,186,.14);cursor:pointer;transition:.18s;background:rgba(255,248,239,.03)}
  .mtg-item:hover{background:rgba(255,248,239,.09)}
  .mtg-item .n{flex:0 0 24px;height:24px;border-radius:50%;display:grid;place-items:center;
    font:600 11.5px/1 Georgia,serif;color:#EAD6BF;background:rgba(255,248,239,.12)}
  .mtg-item b{display:block;font:600 13.5px/1.3 -apple-system,"Segoe UI",sans-serif;color:#F5E8D7}
  .mtg-item small{display:block;color:#B79D84;font-size:11.5px;margin-top:2px}
  .mtg-item em{margin-left:auto;font-style:normal;font-size:11px;color:#B79D84;white-space:nowrap;padding-top:3px}
  .mtg-item.now{border-color:#C4703F;background:rgba(180,85,45,.30);box-shadow:0 0 0 1px rgba(196,112,63,.42)}
  .mtg-item.now .n{background:#B4552D;color:#FFF6EC}
  .mtg-item.now em{color:#F2C9A9}
  .mtg-item.done em{color:#A9BF8F}
  .mtg-agenda p{color:#B79D84;font-size:11.5px;margin:10px 0 0}
  .mtg-join{margin-top:12px;border-top:1px solid rgba(233,211,186,.16);padding-top:12px;display:flex;flex-direction:column;gap:6px}
  .mtg-join h4{font:600 14px/1.2 Georgia,serif;margin:0 0 2px;color:#F3E1CB}
  .mtg-join label{font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;color:#B79D84;margin-top:2px}
  .mtg-join-row{display:flex;gap:6px}
  .mtg-join-row input{flex:1;min-width:0;background:rgba(255,248,239,.07);border:1px solid rgba(233,211,186,.22);
    border-radius:9px;color:#F5E8D7;padding:7px 10px;font-size:12px;font-family:inherit}
  .mtg-join-row input::placeholder{color:#8F7661}
  .mtg-join .mini{color:#B79D84;font-size:11px;margin:0}
  .mtg-join .mtg-join-row .btn{padding:6px 12px;font-size:12px;flex:0 0 auto}
  .min-meta{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12.5px;color:var(--muted);margin-bottom:12px}
  .min-agenda{list-style:none;margin:0 0 12px;padding:0;display:flex;flex-direction:column;gap:7px}
  .min-item{border:1px solid var(--line);border-left:3px solid #D8C3A5;border-radius:0 10px 10px 0;padding:8px 11px;background:#FFFDF9}
  .min-item.done{border-left-color:#A9BF8F;background:#FBFAF3}
  .min-item b{display:block;font:600 13px/1.35 Georgia,serif;color:var(--chest)}
  .min-item small{display:block;color:var(--muted);font-size:11.5px;margin-top:2px}
  .min-history{display:flex;flex-wrap:wrap;gap:7px;margin-top:8px}
  .min-history .btn{padding:5px 9px;font-size:11.5px}
  .min-list{display:flex;flex-direction:column;gap:7px}
  .min-row{display:flex;align-items:center;gap:12px;border:1px solid var(--line);border-radius:12px;padding:10px 13px;
    background:#FFFDF9;cursor:pointer;transition:.16s;text-align:left;font:inherit;color:inherit}
  .min-row:hover{background:#FFF8EE;border-color:var(--gold)}
  .min-row b{font:600 13.5px/1.3 Georgia,serif;color:var(--chest);white-space:nowrap}
  .min-row span{color:var(--muted);font-size:12px;min-width:0}
  .min-row em{margin-left:auto;font-style:normal;color:var(--terra-dark);font-size:11.5px;white-space:nowrap}
  .min-tasks{margin-top:16px;border-top:1px solid var(--line);padding-top:12px}
  .min-tasks h4{font:600 15px/1.2 Georgia,serif;margin:0 0 8px;color:var(--chest)}
  .min-tasks-list{list-style:none;margin:0 0 10px;padding:0;display:flex;flex-direction:column;gap:6px}
  .min-task{display:flex;align-items:center;gap:10px;justify-content:space-between;border:1px solid var(--line);
    border-radius:10px;padding:8px 11px;background:#FFFDF9}
  .min-task.done{background:#FBFAF3;border-color:#DDE5D3}
  .min-task b{display:block;font:600 13px/1.35 Georgia,serif;color:var(--chest)}
  .min-task small{display:block;color:var(--muted);font-size:11.5px;margin-top:2px}
  .min-task.done b{text-decoration:line-through;color:var(--muted)}
  .min-task .btn{flex:0 0 auto;padding:5px 9px;font-size:11.5px}
  .min-form{display:grid;grid-template-columns:minmax(150px,.9fr) minmax(180px,1.5fr) minmax(110px,.7fr) 145px auto;
    gap:8px;align-items:center}
  .min-form select,.min-form input{border:1px solid var(--line);background:#FFFDF9;border-radius:9px;
    padding:7px 10px;font:12.5px/1.4 inherit;color:var(--chest);min-width:0;width:100%}
  .min-form .btn{padding:7px 14px;font-size:12.5px;white-space:nowrap}
  @media (max-width:820px){.min-form{grid-template-columns:1fr 1fr}}
  @media (max-width:520px){.min-form{grid-template-columns:1fr}}
  @media (max-width:980px){
    .mtg{padding:12px}
    .mtg-body{grid-template-columns:1fr}
    .mtg-agenda{order:-1;max-height:34vh}
    .mtg-tile{min-height:250px}
    .mtg-controls .mini{margin-left:0;width:100%}
  }
  @media (max-width:1080px){
    .kpis{grid-template-columns:repeat(3,1fr)}
    .grid{grid-template-columns:1fr}
    .cards{max-height:none}
    .shell{grid-template-columns:1fr;gap:14px;padding:18px 20px 40px}
    .rail{position:static;flex-direction:row;flex-wrap:wrap;align-items:center;padding:8px;border-radius:14px}
    .rail-title,.rail-foot{display:none}
    .rail-item{width:auto;flex:0 0 auto;padding:8px 12px;font-size:12.5px}
  }
  @media (max-width:640px){
    header{padding:14px 18px}
    .shell{padding:20px 18px 40px}
    .kpis{grid-template-columns:repeat(2,1fr)}
    .brand p{display:none}
    .leader{padding:4px}
    .leader-text{display:none}
  }

  /* ---------- вход: образ Аси или своё фото ---------- */
  body.gate-open{overflow:hidden}
  .gate{
    position:fixed;inset:0;z-index:300;display:none;overflow:auto;padding:20px;
    background:radial-gradient(1100px 620px at 16% 6%,#FDF6E9 0,#F7EDDD 54%,#EFE0CA 100%);
  }
  .gate.on{display:flex}
  .gate-sheet{
    width:min(940px,100%);margin:auto;background:var(--paper);
    border:1px solid var(--line);border-radius:20px;overflow:hidden;box-shadow:0 28px 64px rgba(46,27,20,.17);
  }
  .gate-head{display:flex;align-items:center;gap:12px;padding:16px 24px;border-bottom:1px solid var(--line);background:#FFF8EE}
  .gate-head .brand{min-width:0}
  .gate-head .pill{margin-left:auto}
  .gate-body{display:grid;grid-template-columns:.9fr 1.1fr;gap:28px;padding:26px 24px 24px}
  .gate-intro .eyebrow{margin-bottom:8px}
  .gate-intro h2{font:600 33px/1.12 Georgia,serif;letter-spacing:-.02em;margin:0}
  .gate-intro > p{margin:10px 0 0;font-size:12.5px;line-height:1.6;color:var(--muted);max-width:340px}
  .gate-points{list-style:none;margin:16px 0 0;padding:0;display:grid;gap:9px}
  .gate-points li{position:relative;padding-left:16px;font-size:11.5px;line-height:1.5;color:var(--chest)}
  .gate-points li:before{content:"";position:absolute;left:0;top:6px;width:6px;height:6px;border-radius:50%;background:var(--terra)}
  .gate-points b{font-weight:600}
  .gate-priv{margin-top:18px;padding:10px 12px;border:1px dashed #DCC3A4;border-radius:10px;background:#FFF8EE;font-size:10px;line-height:1.5;color:var(--muted)}
  .gate-priv b{color:var(--chest)}
  .gate-pick{display:grid;grid-template-columns:1fr 1fr;gap:11px}
  .gate-opt{
    position:relative;display:flex;flex-direction:column;padding:0;overflow:hidden;text-align:left;
    border:1px solid var(--line);border-radius:14px;background:#FFF8EE;cursor:pointer;
    transition:border-color .18s,box-shadow .18s,transform .18s;
  }
  .gate-opt:hover{border-color:#D8B9A5;transform:translateY(-1px)}
  .gate-opt.on{border-color:var(--terra);box-shadow:0 0 0 1px var(--terra) inset}
  .gate-art{position:relative;display:block;height:118px;overflow:hidden;background:#F1DFC9}
  .gate-art img{width:100%;height:100%;object-fit:cover;object-position:50% 20%}
  .gate-art:after{content:"";position:absolute;inset:auto 0 0 0;height:44px;background:linear-gradient(to top,rgba(46,27,20,.2),rgba(46,27,20,0))}
  .gate-art.own{display:grid;place-items:center;background:repeating-linear-gradient(135deg,#F7EDDD 0 9px,#F2E4D0 9px 18px)}
  .gate-art.own:after{display:none}
  .gate-plus{width:42px;height:42px;border-radius:50%;border:1.5px dashed #C2954A;display:grid;place-items:center;font-size:23px;line-height:1;color:var(--terra);background:rgba(255,251,243,.9)}
  .gate-opt-body{padding:9px 11px 11px}
  .gate-opt-body b{display:block;font-size:12.5px}
  .gate-opt-body small{display:block;margin-top:2px;font-size:10px;line-height:1.4;color:var(--muted)}
  .gate-mark{position:absolute;top:8px;right:8px;width:18px;height:18px;border-radius:50%;border:1.5px solid #C9AE8F;background:rgba(255,251,243,.92);display:grid;place-items:center;z-index:2}
  .gate-opt.on .gate-mark{border-color:var(--terra);background:var(--terra)}
  .gate-opt.on .gate-mark:after{content:"";width:7px;height:7px;border-radius:50%;background:#FFF6E9}
  .own-panel{margin-top:11px;padding:11px;border:1px solid var(--line);border-radius:14px;background:#FFF8EE;display:grid;gap:9px}
  .own-panel[hidden]{display:none}
  .own-drop[hidden]{display:none}
  .own-drop{min-height:92px;padding:9px;border:1.5px dashed #D9BFA4;border-radius:11px;background:var(--paper);display:grid;place-items:center;cursor:pointer;transition:border-color .16s,background .16s;outline:none}
  .own-drop:hover,.own-drop:focus-visible,.own-drop.over{border-color:var(--terra);background:#FFF6EA}
  .own-empty{text-align:center}
  .own-plus{width:32px;height:32px;margin:0 auto 5px;border-radius:50%;background:rgba(180,85,45,.12);color:var(--terra);display:grid;place-items:center;font-size:18px;font-weight:600;line-height:1}
  .own-empty b{display:block;font-size:11.5px}
  .own-empty small{display:block;margin-top:2px;font-size:9.5px;color:var(--muted)}
  .own-preview{display:flex;align-items:center;gap:11px;width:100%}
  .own-preview[hidden]{display:none}
  .own-preview img{width:58px;height:58px;border-radius:10px;object-fit:cover;border:1px solid var(--line);flex:0 0 58px}
  .own-meta{flex:1;min-width:0}
  .own-meta b{display:block;font-size:11.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .own-meta small{display:block;margin-top:2px;font-size:9.5px;color:var(--muted)}
  .own-again{font-size:10px;color:var(--terra);text-decoration:underline;white-space:nowrap}
  .own-steps{display:grid;grid-template-columns:1fr 1fr;gap:6px 10px}
  .own-steps[hidden]{display:none}
  .own-step{display:flex;align-items:center;gap:6px;font-size:10px;color:var(--muted);transition:color .16s}
  .own-step i{width:13px;height:13px;border-radius:50%;border:1.5px solid #D9BFA4;flex:0 0 13px;display:grid;place-items:center;transition:background .16s,border-color .16s}
  .own-step.on{color:var(--chest)}
  .own-step.on i{border-color:var(--terra);background:rgba(180,85,45,.16)}
  .own-step.on i:after{content:"";width:5px;height:5px;border-radius:50%;background:var(--terra);animation:gatePulse .8s infinite}
  .own-step.done{color:var(--chest)}
  .own-step.done i{border-color:var(--sage);background:var(--sage)}
  .own-step.done i:after{content:"\2713";font-size:8px;line-height:1;color:#FFF6E9}
  @keyframes gatePulse{0%,100%{opacity:.35}50%{opacity:1}}
  .own-result{display:flex;align-items:center;gap:11px;padding-top:2px}
  .own-result[hidden]{display:none}
  .gate-face{width:70px;height:70px;border-radius:50%;overflow:hidden;flex:0 0 70px;background:var(--paper);border:2px solid var(--terra);box-shadow:0 7px 16px rgba(46,27,20,.18)}
  .gate-face img{width:100%;height:100%;object-fit:cover;object-position:50% 24%}
  .own-result b{display:block;font-size:11.5px}
  .own-change{margin-top:6px;padding:0;border:0;background:none;color:var(--terra);font:inherit;font-size:10.5px;text-decoration:underline;cursor:pointer}
  .own-result small{display:block;margin-top:3px;font-size:10px;line-height:1.45;color:var(--muted)}
  .gate-foot{display:flex;align-items:center;gap:12px;margin-top:13px}
  .gate-foot .btn{flex:1}
  .gate-foot .btn.primary{padding:11px 16px}
  .gate-note{max-width:196px;font-size:9.5px;line-height:1.45;color:var(--muted)}
  .gate-body[hidden]{display:none}
  .gate-ch-list{display:grid;gap:10px;align-content:start}
  .gate-ch-card{display:grid;gap:7px;padding:11px 12px;border:1px solid var(--line);border-radius:14px;background:#FFF8EE}
  .gate-ch-head{display:flex;align-items:baseline;justify-content:space-between;gap:10px}
  .gate-ch-head b{font-size:12.5px}
  .gate-ch-head span{font-size:10px;color:var(--muted);text-align:right}
  .gate-ch-card .search{padding:7px 9px;font-size:11.5px}
  .gate-ch-card .btn{padding:7px 10px;font-size:11.5px}
  .gate-ch-note{font-size:10px;line-height:1.5;color:var(--muted)}
  @media (max-width:860px){
    .gate-body{grid-template-columns:1fr;gap:18px}
    .gate-intro>p{max-width:none}
    .gate-points{grid-template-columns:1fr 1fr}
    .gate-priv{margin-top:12px}
  }
</style>
</head>
<body>
<div class="gate" id="entryGate" role="dialog" aria-modal="true" aria-labelledby="gateTitle">
  <div class="gate-sheet">
    <div class="gate-head">
      <div class="brand">
        <img class="face" id="gateBrandFace" src="/assets/asya.jpg?v=2" alt="Ася">
        <div>
          <h1>AsiStantAI</h1>
          <p>Ася · цифровой аватар</p>
        </div>
      </div>
      <span class="pill">ИИкузница 2026 · трек «Цифровой аватар»</span>
    </div>
    <div class="gate-body" id="gateFaceBody">
      <div class="gate-intro">
        <p class="eyebrow">Вход в систему · шаг 1 из 2</p>
        <h2 id="gateTitle">Кто поведёт ваш день?</h2>
        <p>Возьмите готовый образ Аси или загрузите своё фото — система соберёт аватар в том же стиле и будет говорить от вашего лица.</p>
        <ul class="gate-points">
          <li><b>Ася</b> — готовый аватар, работает сразу</li>
          <li><b>Своё фото</b> — аватар в стиле Аси за пару секунд</li>
          <li><b>Без автоотправок</b> — каждое действие подтверждаете вы</li>
        </ul>
        <div class="gate-priv"><b>Фото остаётся на устройстве.</b> Стилизация идёт в браузере: без загрузки на сервер и без передачи третьим лицам.</div>
      </div>
      <div class="gate-right">
        <div class="gate-pick" role="radiogroup" aria-label="Выбор образа">
          <button class="gate-opt on" type="button" data-face="asya" role="radio" aria-checked="true">
            <span class="gate-art"><img src="/assets/asya.jpg?v=2" alt="Готовый портрет Аси"></span>
            <span class="gate-mark"></span>
            <span class="gate-opt-body"><b>Ася</b><small>Готовый цифровой аватар в фирменном стиле</small></span>
          </button>
          <button class="gate-opt" type="button" data-face="own" role="radio" aria-checked="false">
            <span class="gate-art own"><span class="gate-plus">+</span></span>
            <span class="gate-mark"></span>
            <span class="gate-opt-body"><b>Ваше фото</b><small>Соберём аватар по фото — в стиле Аси</small></span>
          </button>
        </div>
        <div class="own-panel" id="ownPanel" hidden>
          <div class="own-drop" id="ownDrop" tabindex="0" role="button" aria-label="Загрузить своё фото">
            <input type="file" id="photoInput" accept="image/*" hidden>
            <div class="own-empty" id="ownEmpty">
              <span class="own-plus">+</span>
              <b>Перетащите фото или нажмите</b>
              <small>JPG или PNG · лицо в кадре · до 8 МБ</small>
            </div>
            <div class="own-preview" id="ownPreview" hidden>
              <img id="ownSrc" alt="Ваше фото">
              <div class="own-meta"><b id="ownName">Ваше фото</b><small>нажмите, чтобы выбрать другое</small></div>
              <span class="own-again">Другое</span>
            </div>
          </div>
          <div class="own-steps" id="ownSteps" hidden>
            <div class="own-step" data-gstep="0"><i></i><span>Кадрируем по лицу</span></div>
            <div class="own-step" data-gstep="1"><i></i><span>Смягчаем фон</span></div>
            <div class="own-step" data-gstep="2"><i></i><span>Переводим в стиль Аси</span></div>
            <div class="own-step" data-gstep="3"><i></i><span>Рисуем тон и контур</span></div>
          </div>
          <div class="own-result" id="ownResult" hidden>
            <div class="gate-face"><img id="gateFace" alt="Аватар в стиле Аси"></div>
            <div><b>Аватар готов</b><small>Тёплый тон, контур и мягкий фон — как у Аси. Дальше система говорит от вашего лица.</small><button class="own-change" id="ownAgain" type="button">Другое фото</button></div>
          </div>
        </div>
        <div class="gate-foot">
          <button class="btn primary" id="gateEnter" type="button">Войти с Асей</button>
          <span class="gate-note">Образ можно сменить в любой момент — кнопкой «Образ» в шапке.</span>
        </div>
      </div>
    </div>
    <div class="gate-body" id="gateChannels" hidden>
      <div class="gate-intro">
        <p class="eyebrow">Вход в систему · шаг 2 из 2</p>
        <h2>Куда Асе смотреть?</h2>
        <p>Подключите почты, Telegram и CRM — Ася начнёт с живых непрочитанных, сообщений и контактов. Любой шаг можно пропустить: всё это остаётся в панелях справа.</p>
        <ul class="gate-points">
          <li><b>Почты</b> — адрес и пароль приложения, вход проверяется сразу</li>
          <li><b>Telegram</b> — бот для уведомлений и ваш аккаунт для непрочитанных</li>
          <li><b>CRM · Битрикс24</b> — входящий вебхук для контактов</li>
        </ul>
        <div class="gate-priv"><b>Секреты остаются локально.</b> Пароль ящика пишется в data/mailboxes.json (0600), вебхук Битрикс24 читается только из .env. В журнал, интерфейс и архив они не попадают.</div>
      </div>
      <div class="gate-ch-list">
        <div class="gate-ch-card">
          <div class="gate-ch-head"><b>Почты</b><span id="onbMailState">…</span></div>
          <div style="display:flex;gap:6px">
            <select class="search" id="onbProvider" style="max-width:132px"></select>
            <input class="search" id="onbAddress" placeholder="адрес@почта" autocomplete="username">
          </div>
          <input class="search" id="onbPassword" type="password" placeholder="пароль приложения" autocomplete="off">
          <button class="btn primary" id="onbMailAdd" type="button">Добавить ящик</button>
          <div class="gate-ch-note" id="onbMailNote"></div>
        </div>
        <div class="gate-ch-card">
          <div class="gate-ch-head"><b>Telegram</b><span id="onbTgState">…</span></div>
          <div class="gate-ch-note" id="onbTgText">…</div>
          <button class="btn ghost" id="onbTgCheck" type="button">Проверить Telegram</button>
        </div>
        <div class="gate-ch-card">
          <div class="gate-ch-head"><b>CRM · Битрикс24</b><span id="onbCrmState">…</span></div>
          <div class="gate-ch-note" id="onbCrmText">…</div>
          <button class="btn ghost" id="onbCrmCheck" type="button">Проверить Битрикс24</button>
        </div>
        <div class="gate-foot">
          <button class="btn primary" id="onbDone" type="button">Продолжить в систему</button>
          <button class="btn ghost" id="onbSkip" type="button" style="flex:0 0 auto">Пропустить</button>
        </div>
      </div>
    </div>
  </div>
</div>
<header>
  <div class="brand">
    <img class="face" id="asyaFace" src="/assets/asya.jpg?v=2" alt="Ася">
    <div>
      <h1>AsiStantAI</h1>
      <p>Ася · ИИ-помощник руководителя</p>
    </div>
  </div>
  <div class="controls">
    <div class="leader" id="leaderChip">
      <img class="face" id="leaderFace" src="/assets/ivan-avatar.png?v=2" alt="Иван Петров — аватар">
      <span class="leader-init" id="leaderInitials" hidden>ИП</span>
      <div class="leader-text"><b id="leaderName">Иван Петров</b><span>руководитель</span></div>
    </div>
    <select id="profile" title="Профиль руководителя"></select>
    <button class="btn ghost" id="changeFace" style="padding:9px 12px" title="Выбрать образ Аси или собрать аватар по своему фото">Образ</button>
    <button class="btn meet" id="meetBtn" title="Живой аватар Аси проводит планёрку по текущим данным">Ася ведёт встречу</button>
    <button class="btn ghost" id="liveBtn" title="Снять новые письма из IMAP">Живой прогон</button>
    <button class="btn primary" id="runBtn">Пересобрать неделю</button>
  </div>
</header>
<div class="shell">
<nav class="rail" aria-label="Разделы дашборда">
  <p class="rail-title">Разделы</p>
  <button class="rail-item on" type="button" data-sec="secMeetings">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M8 3v4M16 3v4M3 10h18"/><path d="M8 14h2M8 17h2M13 14h3"/></svg>
    <span>Встречи</span><b class="rail-badge" id="railMeetings" hidden>0</b>
  </button>
  <button class="rail-item" type="button" data-sec="secTasks">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6h11M9 12h11M9 18h11"/><path d="M4 5.5l1.4 1.4L7.5 4.8M4 11.5l1.4 1.4 2.1-2.1M4 17.5l1.4 1.4 2.1-2.1"/></svg>
    <span>Задачи</span><b class="rail-badge" id="railTasks" hidden>0</b>
  </button>
  <button class="rail-item" type="button" data-sec="secMail">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3.5 6.5L12 13l8.5-6.5"/></svg>
    <span>Письма</span><b class="rail-badge" id="railMail" hidden>0</b>
  </button>
  <button class="rail-item" type="button" data-sec="secMessages">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a8 8 0 0 1-8 8H4l2.2-3.1A8 8 0 1 1 21 12z"/><path d="M8.5 11.5h7M8.5 15h4.5"/></svg>
    <span>Сообщения</span><b class="rail-badge" id="railMessages" hidden>0</b>
  </button>
  <button class="rail-item" type="button" data-sec="secCrm">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3.2"/><path d="M3.5 19.5c.6-3.2 2.8-5 5.5-5s4.9 1.8 5.5 5"/><path d="M16 5.4a3.2 3.2 0 0 1 0 5.2M17.2 14.7c1.8.5 3 2.1 3.4 4.3"/></svg>
    <span>CRM</span><b class="rail-badge" id="railCrm" hidden>0</b>
  </button>
  <div class="rail-foot">0 автоотправок<br>каждое действие — после вашего подтверждения</div>
</nav>
<main>
  <section class="hero">
    <p class="eyebrow">Демонстрация на данных ИИкузницы · 26.11.2026</p>
    <h2 id="profileLine">Руководитель</h2>
    <p class="sub" id="runLine">Готовлю разбор почты, календаря и протоколов.</p>
    <div class="kpis" id="kpis"></div>
  </section>

  <section class="grid" id="secMail">
    <div class="panel">
      <div class="panel-head">
        <h3>Очередь решений</h3>
        <div class="tabs" id="tabs"></div>
      </div>
      <input class="search" id="q" placeholder="Поиск по теме, адресату, тексту">
      <div class="cards" id="drafts"><div class="empty">Загружаю…</div></div>
    </div>
    <aside>
      <div class="panel">
        <h3>План дня</h3>
        <div id="morning" class="mini">…</div>
      </div>
      <div class="panel">
        <h3>Неделя</h3>
        <div id="digest" class="mini">…</div>
      </div>
      <div class="panel">
        <div class="panel-head" style="margin-bottom:8px">
          <h3>Календарь</h3>
          <button class="btn ghost" id="calendarLogin" style="padding:4px 10px;font-size:12px">Войти в календарь</button>
        </div>
        <div id="calendar" class="mini">…</div>
      </div>
      <div class="panel">
        <div class="panel-head" style="margin-bottom:8px">
          <h3>Каналы</h3>
          <button class="btn ghost" id="checkChannels" style="padding:4px 10px;font-size:12px">Проверить</button>
        </div>
        <div id="channels" class="channels">…</div>
      </div>
      <div class="panel">
        <div class="panel-head" style="margin-bottom:8px">
          <h3>Почты</h3>
          <button class="btn ghost" id="mailUnreadBtn" style="padding:4px 10px;font-size:12px">Непрочитанные</button>
        </div>
        <select class="search" id="mbPeriod" style="margin-bottom:8px">
          <option value="today">Период: сегодня</option>
          <option value="3d">Период: 3 дня</option>
          <option value="7d">Период: 7 дней</option>
          <option value="30d">Период: 30 дней</option>
          <option value="all">Период: всё время</option>
        </select>
        <div id="mailboxes" class="channels">…</div>
        <div id="mailUnreadLine" class="mini" style="margin-top:8px"></div>
        <div style="display:flex;gap:6px;margin-top:8px">
          <button class="btn ghost" id="openUnreadBtn" style="flex:1;padding:5px 8px;font-size:12px">Открыть непрочитанные</button>
          <button class="btn primary" id="makeDraftsBtn" style="flex:1;padding:5px 8px;font-size:12px">Черновики</button>
        </div>
        <div id="unreadList" class="mini" style="margin-top:8px"></div>
        <details style="margin-top:10px">
          <summary style="cursor:pointer;font-size:12px;color:var(--muted)">Добавить ящик</summary>
          <div style="display:flex;flex-direction:column;gap:6px;margin-top:8px">
            <select class="search" id="mbProvider"></select>
            <input class="search" id="mbAddress" placeholder="Адрес: name@example.com" autocomplete="off">
            <input class="search" id="mbPassword" type="password" placeholder="Пароль приложения" autocomplete="new-password">
            <input class="search" id="mbFromName" placeholder="Подпись отправителя (необязательно)">
            <div id="mbCustom" style="display:none;flex-direction:column;gap:6px">
              <input class="search" id="mbImapHost" placeholder="IMAP-сервер">
              <input class="search" id="mbSmtpHost" placeholder="SMTP-сервер">
            </div>
            <div class="mini" id="mbNote" style="font-size:11px"></div>
            <button class="btn primary" id="mbAddBtn" style="padding:6px 10px;font-size:12px">Добавить ящик</button>
          </div>
        </details>
      </div>
      <div class="panel">
        <h3>Zoom</h3>
        <div id="zoomStatus" class="mini">…</div>
        <div style="display:flex;gap:6px;margin-top:8px">
          <button class="btn ghost" id="zoomLoginBtn" style="flex:1;padding:5px 8px;font-size:12px">Войти в Zoom</button>
          <button class="btn ghost" id="zoomLogoutBtn" style="flex:1;padding:5px 8px;font-size:12px">Выйти</button>
        </div>
        <details style="margin-top:10px">
          <summary style="cursor:pointer;font-size:12px;color:var(--muted)">Постоянная ссылка на комнату</summary>
          <input class="search" id="zoomLink" placeholder="https://zoom.us/j/…" autocomplete="off" style="margin-top:8px">
          <div style="display:flex;gap:6px;margin-top:6px">
            <button class="btn ghost" id="zoomLinkBtn" style="flex:1;padding:6px 10px;font-size:12px">Сохранить</button>
            <button class="btn" id="zoomJoinBtn" style="flex:1;padding:6px 10px;font-size:12px">Подключиться</button>
          </div>
          <div class="mini" style="margin-top:6px;font-size:11px">Вход не обязателен: со ссылкой на комнату встречи выдаются без API.</div>
        </details>
        <input class="search" id="zoomTopic" placeholder="Тема встречи" value="Планёрка с Асей" style="margin-top:10px">
        <input class="search" id="zoomStart" placeholder="Когда: 2026-11-28 10:00 (необязательно)" style="margin-top:7px">
        <button class="btn" id="zoomBtn" style="width:100%;margin-top:8px">Создать ссылку</button>
        <div id="zoomResult" class="mini" style="margin-top:8px"></div>
        <button class="btn meet" id="zoomMeetBtn" style="width:100%;margin-top:10px;justify-content:center">Ася ведёт встречу</button>
        <div class="mini" style="margin-top:6px;font-size:11px">Ася говорит вживую у вас на экране. Для Zoom включите демонстрацию этого окна.</div>
      </div>
      <div class="panel">
        <h3>Телемост</h3>
        <input class="search" id="tmTopic" placeholder="Тема встречи" value="Планёрка с Асей">
        <input class="search" id="tmStart" placeholder="Когда: 2026-11-28 10:00 (необязательно)" style="margin-top:7px">
        <button class="btn" id="tmBtn" style="width:100%;margin-top:8px">Ссылка Телемоста</button>
        <div id="tmResult" class="mini" style="margin-top:8px"></div>
        <details style="margin-top:10px">
          <summary style="cursor:pointer;font-size:12px;color:var(--muted)">Комната Телемоста</summary>
          <input class="search" id="tmLink" placeholder="https://telemost.yandex.ru/j/…" autocomplete="off" style="margin-top:8px">
          <div style="display:flex;gap:6px;margin-top:6px">
            <button class="btn ghost" id="tmLinkBtn" style="flex:1;padding:6px 10px;font-size:12px">Сохранить</button>
            <button class="btn" id="tmJoinBtn" style="flex:1;padding:6px 10px;font-size:12px">Подключиться</button>
          </div>
          <div class="mini" style="margin-top:6px;font-size:11px">Откройте Телемост, создайте встречу и вставьте ссылку — тема и время добавятся к ней автоматически.</div>
        </details>
        <div class="mini" style="margin-top:6px;font-size:11px">
          <a href="https://telemost.yandex.ru/" target="_blank" rel="noopener">Открыть Телемост</a>
        </div>
      </div>
      <div class="panel">
        <h3>Мой Telegram</h3>
        <div id="tgUserStatus" class="mini">…</div>
        <select class="search" id="tgPeriod" style="margin-top:8px">
          <option value="today">Период: сегодня</option>
          <option value="3d">Период: 3 дня</option>
          <option value="7d">Период: 7 дней</option>
          <option value="30d">Период: 30 дней</option>
          <option value="all">Период: всё время</option>
        </select>
        <div style="display:flex;gap:6px;margin-top:8px">
          <button class="btn ghost" id="tgUnreadBtn" style="flex:1;padding:5px 8px;font-size:12px">Непрочитанные</button>
          <button class="btn primary" id="tgDraftsBtn" style="flex:1;padding:5px 8px;font-size:12px">Черновики</button>
        </div>
        <div id="tgUserList" class="mini" style="margin-top:8px"></div>
      </div>
      <div class="panel" id="secMessages">
        <div class="panel-head" style="margin-bottom:8px">
          <h3>Личный чат</h3>
          <select class="search" id="chatPeriod" style="max-width:120px">
            <option value="today">сегодня</option>
            <option value="3d">3 дня</option>
            <option value="7d">7 дней</option>
            <option value="30d">30 дней</option>
            <option value="all">всё время</option>
          </select>
        </div>
        <div id="chatStatus" class="mini">…</div>
        <div id="chatThreads" class="mini" style="margin-top:6px"></div>
        <div style="display:flex;gap:6px;margin-top:8px">
          <button class="btn ghost" id="chatDraftsBtn" style="flex:1;padding:5px 8px;font-size:12px">Черновики Аси</button>
          <button class="btn ghost" id="chatReadBtn" style="flex:1;padding:5px 8px;font-size:12px">Прочитать всё</button>
        </div>
        <details style="margin-top:10px">
          <summary style="cursor:pointer;font-size:12px;color:var(--muted)">Написать от сотрудника</summary>
          <div style="display:flex;flex-direction:column;gap:6px;margin-top:8px">
            <select class="search" id="chatFrom"></select>
            <textarea class="search" id="chatText" rows="3" placeholder="Сообщение руководителю" style="resize:vertical"></textarea>
            <button class="btn primary" id="chatSendBtn" style="padding:6px 10px;font-size:12px">Отправить в чат</button>
            <div class="mini" style="font-size:11px">Сотрудник пишет напрямую; в рабочем контуре сюда же приходят сообщения из Telegram-бота. Ответы — вручную.</div>
          </div>
        </details>
      </div>
      <div class="panel">
        <h3>Журнал <a href="/api/journal.csv" download>CSV</a></h3>
        <ul class="list" id="journal"></ul>
      </div>
    </aside>
  </section>

  <section class="panel" id="secMeetings">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
      <h3 style="margin:0">Протоколы встреч</h3>
      <button class="btn ghost" id="minutesBtn" type="button" style="padding:6px 12px;font-size:12px">Все протоколы Аси</button>
    </div>
    <p class="mini" style="margin:10px 0 14px">Решения, поручения с владельцами и открытые вопросы. Статус «решён / не решён», правка задачи с синхронизацией руководителю и разложение на шаги для аналитика, BDM и CJM — последовательно или параллельно.</p>
    <div id="protocols"></div>
  </section>

  <section class="panel" id="secTasks">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
      <h3 style="margin:0">Задачи и поручения</h3>
      <span class="mini">Все поручения из протоколов в одном списке — статус меняется здесь и в протоколе</span>
    </div>
    <div class="board-stats" id="taskStats"></div>
    <div class="board" id="tasks"><div class="empty">Загружаю…</div></div>
  </section>

  <section class="panel" id="secCrm">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
      <h3 style="margin:0">CRM · контакты</h3>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn ghost" id="bitrixCheck" type="button" style="padding:6px 12px;font-size:12px">Проверить Битрикс24</button>
        <button class="btn ghost" id="bitrixImport" type="button" style="padding:6px 12px;font-size:12px">Импорт из Битрикс24</button>
        <button class="btn ghost" id="crmRefresh" type="button" style="padding:6px 12px;font-size:12px">Обновить</button>
      </div>
    </div>
    <p class="mini" style="margin:10px 0 8px">Контакты собраны из входящих писем прогона: кто писал, сколько писем, последняя тема и что с этим сделала Ася. Внешние — клиенты, вендоры и приглашения; внутренние — команда.</p>
    <div id="bitrixStatus" class="mini" style="margin:0 0 14px"></div>
    <div id="crm"><div class="empty">Загружаю…</div></div>
  </section>

  <section class="panel">
    <details>
      <summary>Вся почта и сверка с разметкой организаторов</summary>
      <div style="margin-top:12px;max-height:520px;overflow:auto">
        <table id="inboxTable"></table>
      </div>
    </details>
  </section>

  <section class="panel" id="secEcon">
    <h3 style="margin:0 0 10px">Экономика</h3>
    <div id="econ">…</div>
  </section>
</main>
</div>
<footer>0 автоотправок · отправка только после утверждения · в демо-режиме письма не уходят</footer>
<div class="toast" id="toast"></div>
<div class="cal-gate" id="calGate" hidden>
  <div class="cal-sheet" role="dialog" aria-modal="true" aria-labelledby="calTitle">
    <div class="cal-head">
      <div>
        <p class="eyebrow" style="margin-bottom:4px">Календарь руководителя · вход без пароля</p>
        <h3 id="calTitle">Неделя</h3>
        <p class="mini" id="calSub">…</p>
      </div>
      <button class="btn ghost" id="calClose" type="button" style="padding:6px 13px;font-size:12px">Закрыть</button>
    </div>
    <div class="cal-week" id="calWeek"></div>
    <div class="cal-tasks">
      <h4>Поручения после утверждения</h4>
      <ul class="list" id="calTasks"></ul>
    </div>
    <p class="mini" style="margin:12px 0 0">Пароли и токены не запрашиваются: календарь собран из локального calendar.ics демо-данных и утверждённых протоколов. Защищённые блоки Ася не занимает операционными встречами. Нажмите на встречу — откроется прошлый протокол по теме, вопросы Аси и задачи.</p>
  </div>
</div>
<div class="cal-gate" id="calMeet" hidden>
  <div class="cal-sheet" role="dialog" aria-modal="true" aria-labelledby="meetTitle">
    <div class="cal-head">
      <div>
        <p class="eyebrow" style="margin-bottom:4px">Встреча в календаре · прошлый протокол по теме</p>
        <h3 id="meetTitle">Встреча</h3>
        <p class="mini" id="meetSub">…</p>
      </div>
      <button class="btn ghost" id="meetClose" type="button" style="padding:6px 13px;font-size:12px">К календарю</button>
    </div>
    <div class="meet-body" id="meetBody"></div>
    <p class="mini" style="margin:12px 0 0">Протокол встречи показан из архива прошедших встреч по теме. Новые поручения после этой встречи Ася заведёт в тот же протокол — с владельцами и сроками.</p>
  </div>
</div>
<div class="cal-gate" id="minGate" hidden>
  <div class="cal-sheet" role="dialog" aria-modal="true" aria-labelledby="minTitle">
    <div class="cal-head">
      <div>
        <p class="eyebrow" style="margin-bottom:4px">Ася ведёт встречу · протокол</p>
        <h3 id="minTitle">Протокол планёрки</h3>
        <p class="mini" id="minSub">…</p>
      </div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <button class="btn ghost" id="minAll" type="button" hidden style="padding:6px 13px;font-size:12px">Все протоколы</button>
        <a class="btn ghost" id="minDownload" hidden style="padding:6px 13px;font-size:12px;text-decoration:none">Скачать .md</a>
        <button class="btn ghost" id="minClose" type="button" style="padding:6px 13px;font-size:12px">Закрыть</button>
      </div>
    </div>
    <div id="minBody"></div>
  </div>
</div>
<div class="mtg" id="meeting" hidden>
  <div class="mtg-bar">
    <div class="mtg-brand">
      <span class="mtg-dot"></span>
      <div><b>Ася ведёт встречу</b><span id="mtgSub">Планёрка</span></div>
    </div>
    <div class="mtg-bar-right">
      <select class="mtg-who" id="mtgWho" title="С кем провести встречу"><option value="">Руководитель</option></select>
      <span class="mtg-timer" id="mtgTimer">00:00</span>
      <button class="btn ghost" id="mtgMinutesBtn" type="button">Протокол</button>
      <button class="btn ghost" id="mtgZoom" type="button">Подключиться</button>
      <button class="btn ghost" id="mtgClose" type="button">Завершить</button>
    </div>
  </div>
  <div class="mtg-body">
    <div class="mtg-stage">
      <div class="mtg-tile">
        <img class="mtg-photo" id="mtgPhoto" src="/assets/asya.jpg?v=2" alt="Ася — живой аватар" draggable="false">
        <div class="mtg-rim" id="mtgRim" aria-hidden="true"></div>
        <div class="mtg-pip">
          <img id="mtgPipImg" src="/assets/ivan-avatar.png?v=2" alt="Иван Петров">
          <div class="mtg-pip-ini" id="mtgPipIni" hidden></div>
          <span id="mtgPipName">Иван · руководитель</span>
        </div>
        <div class="mtg-badge"><i></i><b>Ася</b><span>AsiStantAI · ведущая</span></div>
        <div class="mtg-wave" id="mtgWave" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>
      </div>
      <div class="mtg-caption" id="mtgCaption">Нажмите «Начать встречу» — Ася поприветствует вас голосом и проведёт по повестке.</div>
      <div class="mtg-controls">
        <button class="btn primary" id="mtgPlay" type="button">Начать встречу</button>
        <button class="btn ghost" id="mtgNext" type="button">Следующий пункт</button>
        <span class="mini" id="mtgHint">Голос: системный русский синтез на этом ПК · без клонирования</span>
      </div>
    </div>
    <aside class="mtg-agenda">
      <h4>Повестка</h4>
      <ol id="mtgList"></ol>
      <p>Собрано из текущего прогона: письма, протоколы, календарь и экономика. Цифры совпадают с журналом.</p>
      <div class="mtg-join">
        <h4>Подключиться к встрече</h4>
        <label for="mtgZoomLink">Zoom</label>
        <div class="mtg-join-row">
          <input id="mtgZoomLink" placeholder="https://zoom.us/j/…" autocomplete="off">
          <button class="btn ghost" id="mtgZoomJoin" type="button">Войти</button>
        </div>
        <p class="mini" id="mtgZoomNote">Ссылка не сохранена: вставьте и нажмите «Войти».</p>
        <a class="mini" id="mtgZoomWeb" href="/api/join/zoom" target="_blank" rel="noopener" hidden>Zoom не открылся — войти в браузере</a>
        <label for="mtgTmLink">Телемост</label>
        <div class="mtg-join-row">
          <input id="mtgTmLink" placeholder="https://telemost.yandex.ru/j/…" autocomplete="off">
          <button class="btn ghost" id="mtgTmJoin" type="button">Войти</button>
        </div>
        <p class="mini" id="mtgTmNote">Ссылка не сохранена: вставьте и нажмите «Войти».</p>
        <p class="mini">Ссылка сохранится локально и встреча откроется сразу. Чтобы участники видели Асю, включите демонстрацию этого окна.</p>
      </div>
    </aside>
  </div>
</div>

<script>
const ACT = {
  'спросить_руководителя': 'Решение',
  'делегировать': 'Делегировано',
  'авто': 'Авто',
  'в_сводку': 'В сводку',
  'игнорировать': 'Пропущено'
};
const CLS = {
  'спросить_руководителя': 'ask',
  'делегировать': 'delegate',
  'авто': 'auto',
  'в_сводку': 'digest',
  'игнорировать': 'digest'
};
const state = { data: null, tab: 'all', editing: null, q: '', revising: null, stepPanel: new Set(), allProtocols: new Set(), mailboxes: null, tgUser: null, calendarWeek: null, calEvent: null, crm: null, bitrix: null };
const $ = (id) => document.getElementById(id);
const num = (n) => new Intl.NumberFormat('ru-RU').format(n);
const TASK_STATUS = {
  waiting: 'ждёт постановки',
  approved: 'в календаре',
  in_progress: 'в работе',
  done: 'решён',
  open: 'не решён'
};
const STEP_STATUS = { waiting: 'ждёт', in_progress: 'в работе', done: 'готово' };
const STEP_PRESETS = {
  'Вендоры к продвижению': [
    {title: 'Зафиксировать метрики по вендорам и ассортименту', role: 'Аналитик', owner: '', deadline: 'до 15.12.2026', depends_on: []},
    {title: 'Проанализировать витрину конкурентов и спец-ассортимент', role: 'Команда CJM', owner: '', deadline: 'до 10.12.2026', depends_on: []},
    {title: 'Провести переговоры с вендорами об акции', role: 'BDM', owner: '', deadline: 'до 20.12.2026', depends_on: [0, 1]},
    {title: 'Выделить спец-ассортимент и обновить витрину', role: 'Команда CJM', owner: '', deadline: 'до 25.12.2026', depends_on: [2]}
  ],
  'Витрина конкурентов': [
    {title: 'Собрать витрину конкурентов по категориям', role: 'Команда CJM', owner: '', deadline: '', depends_on: []},
    {title: 'Сверить цены и промо-механики', role: 'Аналитик', owner: '', deadline: '', depends_on: [0]},
    {title: 'Подготовить предложения по ассортименту', role: 'BDM', owner: '', deadline: '', depends_on: [0]},
    {title: 'Утвердить изменения витрины', role: 'Руководитель', owner: '', deadline: '', depends_on: [1, 2]}
  ],
  'Метрики к сроку': [
    {title: 'Согласовать перечень метрик', role: 'Аналитик', owner: '', deadline: 'до 01.12.2026', depends_on: []},
    {title: 'Собрать данные по продажам и воронке', role: 'Аналитик', owner: '', deadline: 'до 10.12.2026', depends_on: [0]},
    {title: 'Проверить метрики с BDM', role: 'BDM', owner: '', deadline: 'до 12.12.2026', depends_on: [1]},
    {title: 'Зафиксировать метрики до 15 декабря', role: 'Аналитик', owner: '', deadline: 'до 15.12.2026', depends_on: [2]}
  ]
};
function taskKey(protocol, index) { return protocol.id + ':' + index; }

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}
function toast(text) {
  const t = $('toast');
  t.textContent = text;
  t.classList.add('on');
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove('on'), 2600);
}
async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = res.status;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.json();
}
async function runWeek() {
  const profile = $('profile').value;
  $('runBtn').disabled = true;
  $('runLine').textContent = 'Разбираю 80 писем, 50 событий и 5 протоколов…';
  try {
    await api('/api/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({profile: profile || null})
    });
    await load();
    toast('Неделя пересобрана: черновики ждут подтверждения');
  } catch (err) {
    toast('Ошибка запуска: ' + err.message);
  } finally {
    $('runBtn').disabled = false;
  }
}
async function runLive() {
  $('liveBtn').disabled = true;
  toast('Снимаю новые письма из IMAP…');
  try {
    const result = await api('/api/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({source: 'live'})
    });
    if (result.skipped) {
      toast('Новых писем нет — очередь не менялась');
      return;
    }
    await load();
    toast('Живой прогон: писем ' + result.new_emails + ', черновиков ' + result.metrics.drafts);
  } catch (err) {
    const detail = String(err.message || '');
    toast(detail.indexOf('not configured') >= 0 ? 'Почта не настроена: заполните .env (MAIL_A_*)' : 'Живой прогон: ' + detail);
  } finally {
    $('liveBtn').disabled = false;
  }
}
async function checkChannels() {
  $('checkChannels').disabled = true;
  try {
    const data = await api('/api/channels/check', {method: 'POST'});
    state.data.channels = data;
    renderChannels();
    const accounts = Object.values(data.accounts || {});
    const bad = accounts.filter((a) => a.configured && a.ok === false).length +
      ((data.telegram && data.telegram.configured && data.telegram.ok === false) ? 1 : 0) +
      ((data.telegram_user && data.telegram_user.authorized && data.telegram_user.ok === false) ? 1 : 0) +
      ((data.zoom && data.zoom.configured && data.zoom.ok === false) ? 1 : 0) +
      ((data.telemost && data.telemost.configured && data.telemost.ok === false) ? 1 : 0);
    toast(bad ? 'Есть ошибки каналов — смотрите подписи' : 'Каналы проверены');
  } catch (err) {
    toast('Проверка не удалась: ' + err.message);
  } finally {
    $('checkChannels').disabled = false;
  }
}
async function loadMailboxes() {
  try {
    state.mailboxes = await api('/api/mailboxes');
  } catch (err) {
    state.mailboxes = { mailboxes: [], providers: {} };
  }
  renderProviders();
  renderMailboxes();
  renderUnreadLine();
}
function renderProviders() {
  const select = $('mbProvider');
  if (select.options.length) return;
  const providers = (state.mailboxes && state.mailboxes.providers) || {};
  for (const [key, p] of Object.entries(providers)) {
    const option = el('option', null, p.title || key);
    option.value = key;
    select.appendChild(option);
  }
  select.value = providers.gmail ? 'gmail' : select.value;
  select.onchange = updateProviderNote;
  updateProviderNote();
}
function updateProviderNote() {
  const select = $('mbProvider');
  const providers = (state.mailboxes && state.mailboxes.providers) || {};
  const p = providers[select.value] || {};
  $('mbNote').textContent = p.note || '';
  $('mbCustom').style.display = select.value === 'custom' ? 'flex' : 'none';
}
function renderMailboxes() {
  const box = $('mailboxes');
  box.innerHTML = '';
  const items = (state.mailboxes && state.mailboxes.mailboxes) || [];
  if (!items.length) {
    box.appendChild(el('div', null, 'Ящики не добавлены.'));
    return;
  }
  for (const mb of items) {
    const row = el('div', 'ch');
    const title = el('span');
    title.appendChild(el('b', null, mb.address || mb.id));
    const sub = (mb.provider_title || '') + (mb.configured ? '' : ' · нет пароля');
    title.appendChild(el('div', 'mini', sub));
    if (mb.unread_error) {
      const why = el('div', 'mini', mb.unread_error.replace(/^mail_[^:]+: /, ''));
      why.style.color = 'var(--terra-dark)';
      title.appendChild(why);
    }
    row.appendChild(title);
    if (typeof mb.unread === 'number') {
      row.appendChild(el('span', 'badge ' + (mb.unread > 0 ? 'warn' : 'ok'), mb.unread + ' непрочит.'));
    } else if (mb.unread_error) {
      const badge = el('span', 'badge warn', 'ошибка');
      badge.title = mb.unread_error;
      row.appendChild(badge);
    } else {
      row.appendChild(el('span', 'badge ' + (mb.configured ? 'ok' : 'warn'), mb.configured ? 'готов' : 'не настроен'));
    }
    if (mb.removable) {
      const pw = el('button', 'btn ghost', 'пароль');
      pw.title = 'Обновить пароль приложения и сразу проверить вход';
      pw.style.cssText = 'padding:1px 7px;font-size:12px';
      pw.onclick = async () => {
        const value = prompt('Новый пароль приложения для ' + (mb.address || mb.id));
        if (!value) return;
        pw.disabled = true;
        try {
          const res = await api('/api/mailboxes/' + encodeURIComponent(mb.id) + '/password', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({password: value})
          });
          state.mailboxes = res;
          renderMailboxes();
          if (res.check && res.check.ok) {
            toast('Вход выполнен · непрочитанных: ' + res.check.unread + ' · за сегодня: ' + res.check.today);
          } else {
            toast('Пароль сохранён, но вход не удался: ' + ((res.check && res.check.detail) || 'ошибка'));
          }
          await refreshUnread(true);
        } catch (err) {
          toast('Не удалось обновить пароль: ' + err.message);
        } finally {
          pw.disabled = false;
        }
      };
      row.appendChild(pw);
      const del = el('button', 'btn ghost', '×');
      del.title = 'Удалить ящик';
      del.style.cssText = 'padding:1px 7px;font-size:12px';
      del.onclick = async () => {
        if (!confirm('Удалить ящик ' + (mb.address || mb.id) + '?')) return;
        try {
          state.mailboxes = await api('/api/mailboxes/' + encodeURIComponent(mb.id), {method: 'DELETE'});
          renderMailboxes();
          renderUnreadLine();
          toast('Ящик удалён');
        } catch (err) {
          toast('Не удалось удалить: ' + err.message);
        }
      };
      row.appendChild(del);
    }
    box.appendChild(row);
  }
}
function mailPeriod() {
  const select = $('mbPeriod');
  return (select && select.value) || 'today';
}
function renderUnreadLine() {
  const data = state.mailboxes || {};
  const line = $('mailUnreadLine');
  if (typeof data.unread !== 'number') { line.textContent = ''; return; }
  const items = data.mailboxes || [];
  const configured = items.filter((mb) => mb.configured).length;
  const reachable = items.filter((mb) => typeof mb.unread === 'number').length;
  if (configured && !reachable) {
    line.textContent = 'Не удалось войти в ящики: ' + configured + ' — причина в строке ящика.';
    return;
  }
  const errors = items.filter((mb) => mb.unread_error).length;
  const title = data.period_title ? ' ' + data.period_title : '';
  line.textContent = 'Непрочитанных' + title + ': ' + data.unread +
    ((data.period && data.period !== 'today' && data.unread_today) ? ' · сегодня: ' + data.unread_today : '') +
    (errors ? ' · ошибок входа: ' + errors : '');
}
async function refreshUnread(silent) {
  try {
    state.mailboxes = await api('/api/mailboxes/unread?period=' + encodeURIComponent(mailPeriod()));
    renderMailboxes();
    renderUnreadLine();
    return state.mailboxes;
  } catch (err) {
    if (!silent) toast('Не удалось проверить: ' + err.message);
    return null;
  }
}
async function openUnread() {
  $('openUnreadBtn').disabled = true;
  const box = $('unreadList');
  box.innerHTML = '';
  box.appendChild(el('div', 'mini', 'Собираю непрочитанные…'));
  try {
    const data = await refreshUnread(true);
    const items = ((data && data.mailboxes) || []).filter((mb) => mb.configured);
    const period = (data && data.period) || mailPeriod();
    box.innerHTML = '';
    if (!items.length) { box.textContent = 'Нет настроенных ящиков — добавьте пароль приложения.'; return; }
    let shown = 0;
    for (const mb of items) {
      let res;
      try {
        res = await api('/api/mailboxes/' + encodeURIComponent(mb.id) + '/messages?period=' + encodeURIComponent(period));
      } catch (err) {
        box.appendChild(el('div', 'mini', (mb.address || mb.id) + ': ' + err.message));
        continue;
      }
      box.appendChild(el('div', 'mini', (mb.address || mb.id) + ' · непрочитанных ' + (res.period_title || '') + ': ' + res.total));
      for (const msg of res.messages) {
        const item = el('div', 'mail-item');
        item.appendChild(el('div', null, msg.subject));
        item.appendChild(el('div', 'mini', (msg.from_name || msg.from_email) + ' · ' + msg.received.replace('T', ' ')));
        box.appendChild(item);
        shown += 1;
      }
    }
    if (!shown) box.appendChild(el('div', 'mini', 'Непрочитанных писем нет.'));
  } finally {
    $('openUnreadBtn').disabled = false;
  }
}
async function makeDrafts() {
  const data = state.mailboxes || {};
  const ids = (data.mailboxes || [])
    .filter((mb) => mb.configured && (typeof mb.unread !== 'number' || mb.unread > 0))
    .map((mb) => mb.id);
  if (!ids.length) { toast('Сначала добавьте ящик и пароль приложения'); return; }
  $('makeDraftsBtn').disabled = true;
  try {
    const result = await api('/api/mailboxes/drafts', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids})
    });
    if (result.skipped) { toast('Непрочитанных писем нет — черновики не нужны'); return; }
    await load();
    await refreshUnread(true);
    toast('Черновики по непрочитанным: писем ' + result.new_emails + ', черновиков ' + result.metrics.drafts);
  } catch (err) {
    toast('Не удалось: ' + err.message);
  } finally {
    $('makeDraftsBtn').disabled = false;
  }
}
async function addMailbox() {
  const address = ($('mbAddress').value || '').trim();
  const password = $('mbPassword').value;
  if (!address) { toast('Укажите адрес почты'); return; }
  if (!password) { toast('Укажите пароль приложения'); return; }
  $('mbAddBtn').disabled = true;
  try {
    const payload = {
      address,
      password,
      provider: $('mbProvider').value || 'custom',
      from_name: ($('mbFromName').value || '').trim(),
      imap_host: ($('mbImapHost').value || '').trim(),
      smtp_host: ($('mbSmtpHost').value || '').trim()
    };
    state.mailboxes = await api('/api/mailboxes', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    renderMailboxes();
    $('mbPassword').value = '';
    $('mbAddress').value = '';
    const check = (state.mailboxes && state.mailboxes.check) || null;
    toast(check && check.ok
      ? 'Ящик добавлен · непрочитанных: ' + check.unread + ' · за сегодня: ' + check.today
      : 'Ящик добавлен, но вход не удался: ' + ((check && check.detail) || 'проверьте пароль'));
    await refreshUnread(true);
  } catch (err) {
    toast('Не удалось добавить: ' + err.message);
  } finally {
    $('mbAddBtn').disabled = false;
  }
}
async function createZoom() {
  const topic = ($('zoomTopic').value || '').trim();
  const start = ($('zoomStart').value || '').trim();
  if (!topic) { toast('Укажите тему встречи'); return; }
  $('zoomBtn').disabled = true;
  try {
    const result = await api('/api/zoom/meeting', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({topic, start, minutes: 45})
    });
    showZoom(result);
    toast(result.mode === 'login' ? 'Встреча Zoom создана: вход выполнен'
      : result.mode === 'api' ? 'Встреча Zoom создана' : 'Постоянная ссылка Zoom');
  } catch (err) {
    const detail = String(err.message || '');
    toast(detail.indexOf('not configured') >= 0 ? 'Zoom не настроен: войдите в Zoom или сохраните постоянную ссылку' : 'Zoom: ' + detail);
  } finally {
    $('zoomBtn').disabled = false;
  }
}
function showZoom(result) {
  const box = $('zoomResult');
  box.innerHTML = '';
  box.appendChild(el('div', null, result.topic + (result.start ? ' · ' + result.start.replace('T', ' ') : '')));
  const link = el('a', null, result.join_url);
  link.href = result.join_url;
  link.target = '_blank';
  link.rel = 'noopener';
  box.appendChild(link);
  const copy = el('button', 'btn ghost', 'Скопировать');
  copy.style.cssText = 'padding:3px 9px;font-size:11.5px;margin-left:8px';
  copy.onclick = async () => {
    try { await navigator.clipboard.writeText(result.join_url); toast('Ссылка скопирована'); }
    catch (e) { toast('Не удалось скопировать — выделите ссылку'); }
  };
  box.appendChild(copy);
}
async function createTelemost() {
  const topic = ($('tmTopic').value || '').trim();
  const start = ($('tmStart').value || '').trim();
  if (!topic) { toast('Укажите тему встречи'); return; }
  $('tmBtn').disabled = true;
  try {
    const result = await api('/api/telemost/meeting', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({topic, start})
    });
    showTelemost(result);
    toast('Ссылка Телемоста готова');
  } catch (err) {
    const detail = String(err.message || '');
    toast(detail.indexOf('not configured') >= 0 ? 'Телемост не настроен: сохраните ссылку на комнату' : 'Телемост: ' + detail);
  } finally {
    $('tmBtn').disabled = false;
  }
}
function showTelemost(result) {
  const box = $('tmResult');
  box.innerHTML = '';
  box.appendChild(el('div', null, result.topic + (result.start ? ' · ' + result.start.replace('T', ' ') : '')));
  const link = el('a', null, result.join_url);
  link.href = result.join_url;
  link.target = '_blank';
  link.rel = 'noopener';
  box.appendChild(link);
  const copy = el('button', 'btn ghost', 'Скопировать');
  copy.style.cssText = 'padding:3px 9px;font-size:11.5px;margin-left:8px';
  copy.onclick = async () => {
    try { await navigator.clipboard.writeText(result.invite || result.join_url); toast('Приглашение скопировано'); }
    catch (e) { toast('Не удалось скопировать — выделите ссылку'); }
  };
  box.appendChild(copy);
  if (result.invite) {
    const invite = el('div', 'mini', result.invite.replace(/\n/g, ' · '));
    invite.style.cssText = 'margin-top:6px;font-size:11px';
    box.appendChild(invite);
  }
}
function renderZoomPanel() {
  const zoom = ((state.data.channels || {}).zoom) || {};
  const box = $('zoomStatus');
  if (zoom.logged_in) {
    box.textContent = 'Вход выполнен' + (zoom.name ? ': ' + zoom.name : '') + '. Встречи создаются через API Zoom.';
  } else if (zoom.mode === 'api') {
    box.textContent = 'Server-to-Server API готов: встречи создаются приложением Zoom.';
  } else if (zoom.mode === 'link') {
    box.textContent = 'Постоянная ссылка сохранена: встречи идут в вашу комнату без API.';
  } else {
    box.textContent = 'Вход не выполнен. Войдите в Zoom или сохраните постоянную ссылку на комнату.';
  }
  $('zoomLogoutBtn').disabled = !zoom.logged_in;
}
async function refreshChannels() {
  try {
    state.data.channels = await api('/api/channels');
    renderChannels();
  } catch (err) {}
}
function zoomLogin() {
  const win = window.open('', '_blank');
  api('/api/zoom/login').then((data) => {
    if (win) win.location = data.url;
    else toast('Разрешите всплывающие окна и повторите вход');
  }).catch((err) => {
    if (win) win.close();
    const detail = String(err.message || '');
    toast(detail.indexOf('oauth app') >= 0
      ? 'Вход в Zoom: добавьте ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET и ZOOM_REDIRECT_URI приложения Zoom'
      : 'Вход в Zoom: ' + detail);
  });
}
async function zoomLogout() {
  try {
    state.data.channels = await api('/api/zoom/logout', {method: 'POST'});
    renderChannels();
    toast('Вход в Zoom завершён');
  } catch (err) {
    toast('Zoom: ' + err.message);
  }
}
async function saveChannelLink(provider, inputId) {
  const link = ($(inputId).value || '').trim();
  try {
    state.data.channels = await api('/api/channels/links', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({provider, link})
    });
    $(inputId).value = '';
    renderChannels();
    toast(link ? 'Ссылка сохранена' : 'Ссылка убрана');
  } catch (err) {
    const detail = String(err.message || '')
      .replace(/^zoom:\s*/, '')
      .replace(/^telemost:\s*/, '');
    toast((provider === 'zoom' ? 'Zoom' : 'Телемост') + ': ' + detail);
  }
}
function zoomCallbackNote() {
  const params = new URLSearchParams(location.search);
  const zoom = params.get('zoom');
  if (!zoom) return;
  history.replaceState(null, '', location.pathname);
  toast(zoom === 'ok' ? 'Zoom: вход выполнен' : 'Zoom: вход не завершён — попробуйте снова');
}
async function loadTgUser() {
  const box = $('tgUserStatus');
  try {
    const status = await api('/api/telegram/user');
    state.tgUser = status;
    if (status.authorized) {
      box.textContent = 'Вход выполнен' + (status.username ? ': @' + status.username : '') + '. Чтение не помечает сообщения прочитанными.';
    } else if (status.configured) {
      box.textContent = 'Сессия не авторизована: выполните python -m app.cli tg-login.';
    } else {
      box.textContent = 'Не подключён: TELEGRAM_API_ID и TELEGRAM_API_HASH в .env, затем python -m app.cli tg-login.';
    }
  } catch (err) {
    box.textContent = 'Telegram: ' + err.message;
  }
}
function tgPeriod() {
  const select = $('tgPeriod');
  return (select && select.value) || 'today';
}
async function tgUnread() {
  const box = $('tgUserList');
  box.textContent = 'Считаю…';
  try {
    const data = await api('/api/telegram/user/unread?period=' + encodeURIComponent(tgPeriod()));
    box.innerHTML = '';
    box.appendChild(el('div', null, 'Непрочитанных ' + (data.period_title || '') + ': ' + data.total + ' · чатов: ' + data.dialogs));
    if (!data.items.length) box.appendChild(el('div', 'mini', 'Нет непрочитанных.'));
    for (const item of data.items) {
      const row = el('div', 'mail-item');
      const head = el('div');
      head.appendChild(el('b', null, item.name || (item.username ? '@' + item.username : 'Чат')));
      const badge = el('span', 'badge warn', String(item.unread));
      badge.style.marginLeft = '6px';
      head.appendChild(badge);
      row.appendChild(head);
      if (item.last_text) row.appendChild(el('div', 'mini', item.last_text));
      box.appendChild(row);
    }
  } catch (err) {
    box.textContent = '';
    toast('Telegram: ' + err.message);
  }
}
async function tgDrafts() {
  const box = $('tgUserList');
  box.textContent = 'Готовлю черновики…';
  try {
    const data = await api('/api/telegram/user/drafts?period=' + encodeURIComponent(tgPeriod()), {method: 'POST'});
    box.innerHTML = '';
    box.appendChild(el('div', null, 'Черновиков: ' + data.drafts.length + ' · непрочитанных ' + (data.period_title || '') + ': ' + data.total));
    if (!data.drafts.length) box.appendChild(el('div', 'mini', 'Нет непрочитанных — черновики не нужны.'));
    for (const draft of data.drafts) {
      const card = el('div', 'mail-item');
      const head = el('div');
      head.appendChild(el('b', null, draft.name || 'Чат'));
      head.appendChild(el('span', 'badge warn', String(draft.unread)));
      card.appendChild(head);
      if (draft.basis) card.appendChild(el('div', 'mini', 'Сообщение: ' + draft.basis));
      const reply = el('div', null, draft.reply);
      reply.style.cssText = 'margin-top:4px;white-space:pre-wrap';
      card.appendChild(reply);
      const copy = el('button', 'btn ghost', 'Скопировать');
      copy.style.cssText = 'padding:3px 9px;font-size:11.5px;margin-top:4px';
      copy.onclick = async () => {
        try { await navigator.clipboard.writeText(draft.reply); toast('Черновик скопирован'); }
        catch (e) { toast('Не удалось скопировать — выделите текст'); }
      };
      card.appendChild(copy);
      box.appendChild(card);
    }
    toast('Черновики готовы: ' + data.drafts.length);
  } catch (err) {
    box.textContent = '';
    toast('Telegram: ' + err.message);
  }
}
function chatPeriod() {
  const select = $('chatPeriod');
  return (select && select.value) || 'today';
}
function chatWhen(value) {
  return (value || '').replace('T', ' ').slice(0, 16);
}
let chatEmployeesLoaded = false;
function fillChatEmployees(employees) {
  const select = $('chatFrom');
  if (!select || chatEmployeesLoaded || !employees || !employees.length) return;
  for (const person of employees) {
    const option = el('option', null, person.name);
    option.value = person.name;
    select.appendChild(option);
  }
  chatEmployeesLoaded = true;
}
async function loadChat() {
  const status = $('chatStatus');
  status.textContent = 'Считаю…';
  try {
    const data = await api('/api/chat?period=' + encodeURIComponent(chatPeriod()));
    state.chat = data;
    fillChatEmployees(data.employees);
    renderChat();
    renderRail();
  } catch (err) {
    status.textContent = 'Чат: ' + err.message;
  }
}
function renderChat() {
  const data = state.chat || {};
  const status = $('chatStatus');
  const box = $('chatThreads');
  if (typeof data.unread !== 'number') { status.textContent = ''; return; }
  status.textContent = 'Непрочитанных ' + (data.period_title || '') + ': ' + data.unread +
    ' · сотрудников: ' + (data.threads || []).length;
  box.innerHTML = '';
  if (!data.threads || !data.threads.length) {
    box.appendChild(el('div', 'mini', 'Сообщений за период нет.'));
    return;
  }
  for (const th of data.threads) {
    const row = el('div', 'mail-item');
    const head = el('div');
    head.appendChild(el('b', null, th.employee));
    if (th.unread) {
      const badge = el('span', 'badge warn', String(th.unread));
      badge.style.marginLeft = '6px';
      head.appendChild(badge);
    }
    row.appendChild(head);
    row.appendChild(el('div', 'mini', (th.position || th.role || '') + ' · ' + chatWhen(th.last_at)));
    row.appendChild(el('div', 'mini', (th.last_direction === 'out' ? 'Вы: ' : '') + th.last_text));
    const pane = el('div');
    pane.style.display = 'none';
    row.appendChild(pane);
    let opened = false;
    let loaded = false;
    row.onclick = async () => {
      if (!loaded) {
        pane.textContent = '…';
        try {
          const thread = await api('/api/chat/messages?period=' + encodeURIComponent(chatPeriod()) +
            '&employee=' + encodeURIComponent(th.employee));
          renderChatThread(pane, thread, th.employee);
          loaded = true;
        } catch (err) {
          pane.textContent = 'Чат: ' + err.message;
          return;
        }
      }
      opened = !opened;
      pane.style.display = opened ? 'block' : 'none';
    };
    box.appendChild(row);
  }
}
function renderChatThread(pane, thread, employee) {
  pane.innerHTML = '';
  for (const m of thread.messages) {
    const line = el('div');
    line.style.cssText = 'padding:4px 0;border-bottom:1px dashed rgba(0,0,0,.06);white-space:pre-wrap';
    line.textContent = (m.direction === 'out' ? 'Вы: ' : '') + m.text;
    line.appendChild(el('div', 'mini', chatWhen(m.at) + (m.direction === 'in' ? (m.read ? ' · прочитано' : ' · новое') : '')));
    pane.appendChild(line);
  }
  const input = el('textarea', 'search');
  input.rows = 2;
  input.placeholder = 'Ответ вручную';
  input.style.cssText = 'width:100%;margin-top:8px;resize:vertical';
  if (thread.draft) {
    const draft = el('div');
    draft.style.cssText = 'margin-top:6px;padding:8px;border-radius:8px;background:rgba(0,0,0,.03)';
    draft.appendChild(el('div', 'mini', 'Черновик Аси · ' + (thread.draft.reason || '')));
    const text = el('div', null, thread.draft.reply);
    text.style.cssText = 'margin-top:4px;white-space:pre-wrap';
    draft.appendChild(text);
    const take = el('button', 'btn ghost', 'Взять черновик');
    take.style.cssText = 'padding:3px 9px;font-size:11.5px;margin-top:4px';
    take.onclick = (ev) => {
      ev.stopPropagation();
      input.value = thread.draft.reply;
      input.focus();
    };
    draft.appendChild(take);
    pane.appendChild(draft);
  }
  pane.appendChild(input);
  const actions = el('div');
  actions.style.cssText = 'display:flex;gap:6px;margin-top:6px';
  const send = el('button', 'btn primary', 'Записать ответ');
  send.style.cssText = 'padding:4px 9px;font-size:11.5px';
  send.onclick = async (ev) => {
    ev.stopPropagation();
    const value = (input.value || '').trim();
    if (!value) { toast('Ответ пустой'); return; }
    try {
      await api('/api/chat/reply', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({employee, text: value})
      });
      toast('Ответ записан · отправьте его вручную');
      await loadChat();
    } catch (err) {
      toast('Не удалось: ' + err.message);
    }
  };
  actions.appendChild(send);
  if (thread.unread) {
    const read = el('button', 'btn ghost', 'Прочитано');
    read.style.cssText = 'padding:4px 9px;font-size:11.5px';
    read.onclick = async (ev) => {
      ev.stopPropagation();
      try {
        await api('/api/chat/read?employee=' + encodeURIComponent(employee), {method: 'POST'});
        await loadChat();
      } catch (err) {
        toast('Не удалось: ' + err.message);
      }
    };
    actions.appendChild(read);
  }
  pane.appendChild(actions);
}
async function chatSend() {
  const employee = $('chatFrom').value;
  const text = ($('chatText').value || '').trim();
  if (!employee) { toast('Выберите сотрудника'); return; }
  if (!text) { toast('Введите сообщение'); return; }
  $('chatSendBtn').disabled = true;
  try {
    await api('/api/chat/messages', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({employee, text})
    });
    $('chatText').value = '';
    await loadChat();
    toast('Сообщение в чате · от ' + employee);
  } catch (err) {
    toast('Не удалось: ' + err.message);
  } finally {
    $('chatSendBtn').disabled = false;
  }
}
async function chatDrafts() {
  const box = $('chatThreads');
  box.textContent = 'Готовлю черновики…';
  try {
    const data = await api('/api/chat/drafts?period=' + encodeURIComponent(chatPeriod()), {method: 'POST'});
    box.innerHTML = '';
    box.appendChild(el('div', null, 'Черновиков: ' + data.drafts.length + ' · непрочитанных ' + (data.period_title || '') + ': ' + data.total));
    if (!data.drafts.length) box.appendChild(el('div', 'mini', 'Нет непрочитанных — черновики не нужны.'));
    for (const draft of data.drafts) {
      const card = el('div', 'mail-item');
      const head = el('div');
      head.appendChild(el('b', null, draft.employee));
      head.appendChild(el('span', 'badge warn', String(draft.unread)));
      card.appendChild(head);
      if (draft.basis) card.appendChild(el('div', 'mini', 'Сообщение: ' + draft.basis));
      const reply = el('div', null, draft.reply);
      reply.style.cssText = 'margin-top:4px;white-space:pre-wrap';
      card.appendChild(reply);
      const copy = el('button', 'btn ghost', 'Скопировать');
      copy.style.cssText = 'padding:3px 9px;font-size:11.5px;margin-top:4px';
      copy.onclick = async () => {
        try { await navigator.clipboard.writeText(draft.reply); toast('Черновик скопирован'); }
        catch (e) { toast('Не удалось скопировать — выделите текст'); }
      };
      card.appendChild(copy);
      box.appendChild(card);
    }
    toast('Черновики готовы: ' + data.drafts.length);
  } catch (err) {
    box.textContent = '';
    toast('Чат: ' + err.message);
  }
}
async function chatReadAll() {
  try {
    const res = await api('/api/chat/read', {method: 'POST'});
    await loadChat();
    toast('Отмечено прочитанным: ' + res.marked);
  } catch (err) {
    toast('Не удалось: ' + err.message);
  }
}
async function load() {
  zoomCallbackNote();
  let data = await api('/api/summary');
  if (data.empty) {
    await api('/api/run', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    data = await api('/api/summary');
  }
  state.data = data;
  render();
  await loadMailboxes();
  await loadTgUser();
  await loadChat();
  await loadCrm();
}
function renderProfiles() {
  const select = $('profile');
  const current = select.value;
  select.innerHTML = '';
  for (const p of state.data.profiles || []) {
    const option = el('option', null, p.default ? p.name + ' · по умолчанию' : p.name);
    option.value = p.default ? '' : p.name;
    select.appendChild(option);
  }
  select.value = current;
}
function renderKpis() {
  const m = state.data.metrics || {};
  const e = state.data.economics || {};
  const box = $('kpis');
  box.innerHTML = '';
  const dayMin = e.emails_per_day_min || 50;
  const dayMax = e.emails_per_day_max || 100;
  const weekMin = dayMin * 5;
  const weekMax = dayMax * 5;
  const perWeek = (e.meetings && e.meetings.per_week) || {};
  const meetMin = perWeek.min || 25;
  const meetMax = perWeek.max || 40;
  const items = [
    [weekMin + '–' + weekMax, 'писем в неделю · ' + dayMin + '–' + dayMax + ' в день', ''],
    [meetMin + '–' + meetMax, 'встреч в неделю', 'gold'],
    [m.drafts || 0, 'черновика ждут', ''],
    [m.decisions || 0, 'на решение', 'gold'],
    [m.auto_sends || 0, 'автоотправок', 'sage'],
    [(m.accuracy && m.accuracy.total) ? Math.round((m.accuracy.accuracy || 0) * 100) : '—', 'точность разбора, %', 'sage'],
    [(m.citations_coverage ? Math.round(m.citations_coverage * 100) : 0), 'черновиков со ссылками, %', 'gold']
  ];
  for (const [value, label, cls] of items) {
    const card = el('div', 'kpi ' + cls);
    card.appendChild(el('b', null, value));
    card.appendChild(el('span', null, label));
    box.appendChild(card);
  }
}
function renderHeader() {
  const m = state.data.metrics || {};
  const name = m.profile_name || 'Руководитель';
  $('profileLine').textContent = name + (m.profile_role ? ' · ' + m.profile_role : '');
  $('leaderName').textContent = name;
  $('leaderChip').title = name + (m.profile_role ? ' · ' + m.profile_role : '');
  const isIvan = /^Иван/i.test(name);
  $('leaderFace').hidden = !isIvan;
  $('leaderInitials').hidden = isIvan;
  if (!isIvan) {
    const parts = name.trim().split(/\s+/);
    $('leaderInitials').textContent = ((parts[0] || 'Р')[0] || 'Р') + ((parts[1] || '')[0] || '');
  }
  const actions = m.actions || {};
  const parts = Object.entries(actions).map(([k, v]) => (ACT[k] || k).toLowerCase() + ' ' + v);
  const mode = m.mode === 'live' ? 'живой прогон · ' + (m.email_source || 'imap') + ' · ' : '';
  $('runLine').textContent = 'Запуск #' + state.data.run_id + ' · ' + mode + (parts.join(' · ') || 'данных нет');
}
function renderTabs() {
  const m = state.data.metrics || {};
  const actions = m.actions || {};
  const tabs = $('tabs');
  tabs.innerHTML = '';
  const all = el('button', 'tab' + (state.tab === 'all' ? ' on' : ''), 'Все · ' + (m.drafts || 0));
  all.onclick = () => { state.tab = 'all'; renderDrafts(); renderTabs(); };
  tabs.appendChild(all);
  for (const key of ['спросить_руководителя', 'делегировать', 'авто']) {
    const count = actions[key] || 0;
    const tab = el('button', 'tab' + (state.tab === key ? ' on' : ''), (ACT[key] || key) + ' · ' + count);
    tab.onclick = () => { state.tab = key; renderDrafts(); renderTabs(); };
    tabs.appendChild(tab);
  }
}
function actionOf(draft) {
  if (draft.kind === 'decision') return 'спросить_руководителя';
  if (draft.kind === 'assignment') return 'делегировать';
  if (draft.kind === 'calendar') return 'авто';
  return 'спросить_руководителя';
}
function renderDrafts() {
  const box = $('drafts');
  box.innerHTML = '';
  let items = state.data.drafts || [];
  if (state.tab !== 'all') items = items.filter((d) => actionOf(d) === state.tab);
  const q = (state.q || '').trim().toLowerCase();
  if (q) {
    items = items.filter((d) => [d.topic, d.subject, d.recipient, d.body].join(' ').toLowerCase().includes(q));
  }
  if (!items.length) {
    const empty = el('div', 'empty');
    if (state.tab === 'в_сводку' || state.tab === 'игнорировать') {
      empty.textContent = 'По этим действиям черновиков нет — письма идут в сводку или пропускаются.';
    } else if (q) {
      empty.textContent = 'Ничего не найдено — очистите поиск.';
    } else {
      empty.textContent = 'Очередь пуста.';
    }
    box.appendChild(empty);
    return;
  }
  for (const draft of items) box.appendChild(cardFor(draft));
}
function cardFor(draft) {
  const action = actionOf(draft);
  const card = el('article', 'card ' + draft.status);
  const row1 = el('div', 'row1');
  row1.appendChild(el('span', 'pill ' + CLS[action], ACT[action]));
  row1.appendChild(el('span', 'pill', draft.topic || 'без темы'));
  const liveMode = (state.data.metrics && state.data.metrics.mode) === 'live';
  const statusText = draft.status === 'approved'
    ? (draft.sent ? (liveMode ? 'отправлено' : 'отправлено (демо)') : 'утверждено')
    : draft.status === 'rejected' ? 'отклонено' : 'ждёт подтверждения';
  row1.appendChild(el('span', 'pill', statusText));
  if (draft.edited) row1.appendChild(el('span', 'pill', 'правка учтена'));
  card.appendChild(row1);
  card.appendChild(el('h4', null, draft.subject || '(без темы)'));
  const address = draft.to_email ? ' · ' + draft.to_email : '';
  card.appendChild(el('div', 'meta', 'Кому: ' + (draft.recipient || '—') + address + ' · письмо ' + draft.email_id));
  card.appendChild(el('div', 'body', draft.body || ''));

  if (draft.sources && draft.sources.length) {
    const list = el('ul', 'sources');
    for (const source of draft.sources.slice(0, 4)) {
      list.appendChild(el('li', null, source.title + ' — ' + source.quote));
    }
    card.appendChild(list);
  }

  const actions = el('div', 'actions');
  if (draft.status === 'waiting_approval') {
    const ok = el('button', 'btn primary', 'Утвердить');
    ok.onclick = async () => {
      try {
        const result = await api('/api/drafts/' + encodeURIComponent(draft.id) + '/approve', {method: 'POST'});
        const delivery = result.delivery || {};
        if (delivery.channel === 'smtp') toast('Отправлено через SMTP: ' + (delivery.to || ''));
        else if (result.mock) toast('Утверждено. Демо-отправка зафиксирована в журнале.');
        else toast(result.note || 'Утверждено.');
        await load();
      } catch (err) { toast('Не удалось утвердить: ' + err.message); }
    };
    actions.appendChild(ok);
    const edit = el('button', 'btn', 'Поправить');
    edit.onclick = () => {
      state.editing = state.editing === draft.id ? null : draft.id;
      renderDrafts();
    };
    actions.appendChild(edit);
    const no = el('button', 'btn ghost', 'Отклонить');
    no.onclick = async () => {
      if (!confirm('Отклонить черновик? Письмо не уйдёт.')) return;
      try {
        await api('/api/drafts/' + encodeURIComponent(draft.id) + '/reject', {method: 'POST'});
        toast('Черновик отклонён.');
        await load();
      } catch (err) { toast('Ошибка: ' + err.message); }
    };
    actions.appendChild(no);
  }
  card.appendChild(actions);

  if (state.editing === draft.id) {
    const area = el('textarea');
    area.value = draft.body || '';
    card.appendChild(area);
    const save = el('button', 'btn primary', 'Сохранить правку');
    save.onclick = async () => {
      try {
        await api('/api/drafts/' + encodeURIComponent(draft.id) + '/edit', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({body: area.value})
        });
        state.editing = null;
        toast('Правка сохранена — тема запомнит стиль.');
        await load();
      } catch (err) { toast('Не удалось сохранить: ' + err.message); }
    };
    const wrap = el('div', 'actions');
    wrap.appendChild(save);
    card.appendChild(wrap);
  }
  return card;
}
function renderMorning() {
  const box = $('morning');
  const plan = state.data.morning || {};
  box.innerHTML = '';
  if (!plan.events) { box.textContent = 'Нет данных.'; return; }
  const head = el('div', 'mini', plan.date + ' · встреч ' + plan.events.length + ' · защищённых ' + plan.protected_hours + ' ч');
  box.appendChild(head);
  const list = el('ul', 'list');
  for (const event of plan.events.slice(0, 7)) {
    const item = el('li');
    item.appendChild(el('span', 'when', event.start + '–' + event.end));
    item.appendChild(el('span', event.protected ? 'protected' : '', event.summary));
    list.appendChild(item);
  }
  box.appendChild(list);
  if (plan.focus && plan.focus.length) {
    box.appendChild(el('div', 'mini', 'Фокус: ' + plan.focus.join(' · ')));
  }
}
function renderDigest() {
  const box = $('digest');
  const d = state.data.digest || {};
  box.innerHTML = '';
  if (!d.emails) { box.textContent = 'Нет данных.'; return; }
  box.appendChild(el('div', null, 'Писем ' + d.emails + ' · без руководителя ' + d.without_leader + ' (' + Math.round((d.without_leader_share || 0) * 100) + '%)'));
  box.appendChild(el('div', null, 'Встреч ' + d.meetings + ' · защищённых ' + d.protected_hours + ' ч'));
  if (d.tasks_overdue && d.tasks_overdue.length) {
    box.appendChild(el('div', 'bad', 'Просрочено задач: ' + d.tasks_overdue.length));
  } else {
    box.appendChild(el('div', 'ok', 'Просроченных задач нет'));
  }
  const protocols = d.protocols || [];
  if (protocols.length) {
    box.appendChild(el('div', 'mini', 'Протоколов: ' + protocols.length + ' · поручений ' + protocols.reduce((s, p) => s + p.tasks, 0)));
  }
}
function renderCalendar() {
  const box = $('calendar');
  box.innerHTML = '';
  const tasks = state.data.calendar || [];
  if (!tasks.length) {
    box.textContent = 'Поручений пока нет — утвердите их в протоколах.';
    return;
  }
  box.appendChild(el('div', null, 'Задач в календаре: ' + tasks.length));
  const list = el('ul', 'list');
  for (const task of tasks.slice(0, 6)) {
    const owner = task.owner ? task.owner + ' — ' : '';
    const deadline = task.deadline ? ' · ' + task.deadline : '';
    const status = task.status && task.status !== 'approved' ? ' · ' + (TASK_STATUS[task.status] || task.status) : '';
    const steps = task.steps_total ? ' · шаги ' + task.steps_done + '/' + task.steps_total : '';
    list.appendChild(el('li', null, owner + task.text + deadline + status + steps));
  }
  box.appendChild(list);
  if (tasks.length > 6) box.appendChild(el('div', 'mini', '… ещё ' + (tasks.length - 6)));
}
function calDayLabel(iso, long) {
  const d = new Date(iso + 'T00:00:00');
  return d.toLocaleDateString('ru-RU', long
    ? {day: 'numeric', month: 'long'}
    : {weekday: 'short', day: 'numeric', month: 'short'});
}
const CAL_CATEGORY = {
  'операционная': 'операционная', 'руководство': 'руководство', 'клиент': 'клиент',
  'партнёр': 'партнёр', 'деньги': 'деньги', 'один_на_один': 'один на один'
};
function calCategory(value) { return CAL_CATEGORY[value] || value; }
function renderCalendarModal() {
  const cal = state.calendarWeek || {events: [], days: [], tasks: []};
  const days = cal.days || [];
  const events = cal.events || [];
  const weekEvents = events.filter((event) => days.includes(event.date));
  $('calTitle').textContent = days.length
    ? 'Две недели ' + calDayLabel(days[0], true) + ' — ' + calDayLabel(days[days.length - 1], true)
    : 'Календарь';
  $('calSub').textContent = 'Встреч: ' + weekEvents.length
    + ' · у каждой — прошлый протокол и задачи · поручений в календаре: ' + (cal.tasks || []).length;
  const week = $('calWeek');
  week.innerHTML = '';
  for (const day of days) {
    const box = el('div', 'cal-day');
    box.appendChild(el('h4', null, calDayLabel(day, false)));
    const items = events.filter((event) => event.date === day);
    if (!items.length) box.appendChild(el('div', 'mini', 'Свободно'));
    for (const item of items) {
      const node = el('button', 'cal-ev' + (item.protected ? ' protected' : ''));
      node.type = 'button';
      node.appendChild(el('time', null, item.start + '–' + item.end));
      node.appendChild(el('b', null, item.summary));
      node.appendChild(el('span', null, calCategory(item.category) + (item.protocol_topic ? ' · ' + item.protocol_topic : '')));
      node.title = 'Открыть прошлый протокол по теме и задачи';
      node.onclick = () => openCalMeeting(item);
      box.appendChild(node);
    }
    week.appendChild(box);
  }
  const list = $('calTasks');
  list.innerHTML = '';
  const tasks = cal.tasks || [];
  if (!tasks.length) {
    list.appendChild(el('li', 'mini', 'Поручений пока нет — утвердите их в протоколах.'));
  }
  for (const task of tasks) {
    const owner = task.owner ? task.owner + ' — ' : '';
    const deadline = task.deadline ? ' · ' + task.deadline : ' · без срока';
    list.appendChild(el('li', null, owner + task.text + deadline));
  }
}
function calProtocolFor(id) {
  const cal = state.calendarWeek || {};
  return (cal.protocols && cal.protocols[id]) || null;
}
function openCalMeeting(item) {
  state.calEvent = item;
  renderCalMeeting();
  $('calMeet').hidden = false;
  $('calMeet').querySelector('.cal-sheet').scrollTop = 0;
}
function closeCalMeeting() {
  $('calMeet').hidden = true;
  state.calEvent = null;
}
function renderCalMeeting() {
  const item = state.calEvent;
  if (!item) return;
  const protocol = calProtocolFor(item.protocol_id);
  $('meetTitle').textContent = item.summary;
  $('meetSub').textContent = calDayLabel(item.date, true) + ' · ' + item.start + '–' + item.end
    + ' · ' + calCategory(item.category) + (item.protected ? ' · защищённая встреча' : '');
  const body = $('meetBody');
  body.innerHTML = '';
  const top = el('div', 'meet-top');
  top.appendChild(el('span', 'pill', calCategory(item.category)));
  if (item.protected) top.appendChild(el('span', 'pill', 'Ася не занимает'));
  if (protocol) top.appendChild(el('span', 'pill', 'прошлый протокол: ' + protocol.when));
  body.appendChild(top);
  if (!protocol) {
    body.appendChild(el('p', 'mini', 'Прошлого протокола по этой теме пока нет.'));
    return;
  }
  const tasks = protocol.tasks || [];
  const block = el('div', 'meet-block');
  block.appendChild(el('h4', null, 'Прошлый протокол: ' + protocol.title));
  const bits = [];
  if (protocol.when) bits.push(protocol.when);
  if (protocol.host && !/^не назван/i.test(protocol.host)) bits.push('ведущий: ' + protocol.host);
  if (item.protocol_topic) bits.push('тема: ' + item.protocol_topic);
  bits.push('поручений: ' + tasks.length);
  bits.push('открытых вопросов: ' + (protocol.open_questions || []).length);
  block.appendChild(el('div', 'mini', bits.join(' · ')));
  const open = el('button', 'meet-open', 'Открыть карточку протокола в системе →');
  open.type = 'button';
  open.onclick = () => openProtocolFromMeeting(protocol.id);
  block.appendChild(open);
  body.appendChild(block);
  if ((protocol.open_questions || []).length) {
    const q = el('div', 'meet-block');
    q.appendChild(el('h4', null, 'Вопросы Аси на встрече по прошлому протоколу'));
    const ul = el('ul', 'meet-q');
    for (const text of protocol.open_questions.slice(0, 3)) ul.appendChild(el('li', null, text));
    q.appendChild(ul);
    q.appendChild(el('div', 'mini', 'Остальные ' + Math.max(0, protocol.open_questions.length - 3) + ' — в карточке протокола.'));
    body.appendChild(q);
  }
  const details = el('div', 'proto meet-body');
  for (const node of [
    protocolDetails('Решения прошлого протокола', protocol.decisions, true),
    protocolDetails('Риски и открытые вопросы', protocol.open_questions, true),
    protocolDetails('Дальнейшие шаги', protocol.next_steps, false),
  ]) {
    if (node) details.appendChild(node);
  }
  body.appendChild(details);
  const tblock = el('div', 'meet-block');
  tblock.appendChild(el('h4', null, 'Задачи из протокола (' + tasks.length + ')'));
  const ul = el('ul');
  const shown = state.allProtocols.has(protocol.id) ? tasks.length : Math.min(tasks.length, 5);
  for (let i = 0; i < shown; i++) ul.appendChild(calTaskRow(tasks[i]));
  tblock.appendChild(ul);
  if (tasks.length > shown) {
    const more = actionButton('Показать все ' + tasks.length, 'btn ghost', () => {
      state.allProtocols.add(protocol.id);
      renderCalMeeting();
    });
    more.style.cssText = 'padding:4px 10px;font-size:12px;margin-top:6px';
    tblock.appendChild(more);
  }
  body.appendChild(tblock);
}
function calTaskRow(task) {
  const li = el('li', 'task');
  const top = el('div', 'task-top');
  const txt = el('div', 'txt');
  txt.appendChild(el('div', null, task.text));
  const badges = el('div', 'task-badges');
  if (task.owner) badges.appendChild(el('span', 'pill', task.owner));
  if (task.deadline) badges.appendChild(el('span', 'pill', task.deadline));
  badges.appendChild(statusPill(task.status));
  txt.appendChild(badges);
  top.appendChild(txt);
  li.appendChild(top);
  return li;
}
function openProtocolFromMeeting(id) {
  state.allProtocols.add(id);
  closeCalMeeting();
  closeCalendar();
  renderProtocols();
  const card = document.getElementById('proto-' + id);
  if (!card) return;
  card.scrollIntoView({behavior: 'smooth', block: 'start'});
  card.classList.add('flash');
  setTimeout(() => card.classList.remove('flash'), 1800);
}
async function openCalendar() {
  $('calMeet').hidden = true;
  state.calEvent = null;
  $('calGate').hidden = false;
  try {
    const cal = await api('/api/calendar');
    state.data.calendar = cal.tasks;
    state.calendarWeek = cal;
  } catch (err) {
    toast('Календарь: ' + err.message);
  }
  renderCalendarModal();
}
function closeCalendar() {
  $('calGate').hidden = true;
}
const MTG = {
  data: null, items: [], step: -2, token: 0, mode: 'idle', ctx: null,
  source: null, buffer: null, offset: 0, startedAtCtx: 0, onDone: null, safety: 0,
  analyser: null, raf: 0, timer: 0, startedAt: 0, fake: false,
  covered: new Set(), channel: '', employee: '', people: null
};
function mtgCaption(text) {
  $('mtgCaption').textContent = text;
}
function renderMtgAgenda() {
  const list = $('mtgList');
  list.innerHTML = '';
  MTG.items.forEach((item, index) => {
    const row = el('li', 'mtg-item');
    row.appendChild(el('span', 'n', String(index + 1)));
    const box = el('div');
    box.appendChild(el('b', null, item.title));
    box.appendChild(el('small', null, item.fact));
    row.appendChild(box);
    row.appendChild(el('em', null, ''));
    row.onclick = () => {
      if (MTG.mode === 'idle') startMtgTimer();
      speakStep(index);
    };
    list.appendChild(row);
  });
  paintMtgAgenda();
}
function paintMtgAgenda() {
  const rows = $('mtgList').children;
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];
    const covered = i !== MTG.step && (MTG.covered.has(i) || MTG.step > MTG.items.length);
    row.classList.toggle('now', i === MTG.step);
    row.classList.toggle('done', covered);
    const mark = row.lastChild;
    mark.textContent = i === MTG.step ? 'говорит' : (covered ? 'освещено' : '');
  }
}
function mtgTick() {
  const seconds = Math.floor((Date.now() - MTG.startedAt) / 1000);
  const mm = String(Math.floor(seconds / 60)).padStart(2, '0');
  const ss = String(seconds % 60).padStart(2, '0');
  $('mtgTimer').textContent = mm + ':' + ss;
}
function startMtgTimer() {
  if (MTG.timer) return;
  MTG.startedAt = Date.now();
  MTG.timer = setInterval(mtgTick, 1000);
  mtgTick();
}
function resetMtgTimer() {
  clearInterval(MTG.timer);
  MTG.timer = 0;
  MTG.startedAt = 0;
}
function mtgAudioCtx() {
  if (!MTG.ctx) {
    const Ctor = window.AudioContext || window.webkitAudioContext;
    MTG.ctx = Ctor ? new Ctor() : null;
  }
  return MTG.ctx;
}
function stopMtgAudio() {
  if (MTG.safety) {
    clearTimeout(MTG.safety);
    MTG.safety = 0;
  }
  if (MTG.source) {
    const source = MTG.source;
    MTG.source = null;
    try { source.onended = null; source.stop(); } catch (err) {}
  }
  if (window.speechSynthesis) speechSynthesis.cancel();
  MTG.analyser = null;
  MTG.buffer = null;
  MTG.onDone = null;
  MTG.offset = 0;
  MTG.fake = false;
}
function startMtgBuffer(buffer, offset, token, done) {
  const ctx = mtgAudioCtx();
  const source = ctx.createBufferSource();
  source.buffer = buffer;
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 1024;
  analyser.smoothingTimeConstant = 0.55;
  source.connect(analyser);
  analyser.connect(ctx.destination);
  MTG.source = source;
  MTG.analyser = analyser;
  MTG.buffer = buffer;
  MTG.onDone = done;
  MTG.offset = Math.max(0, Math.min(offset || 0, buffer.duration - 0.05));
  MTG.startedAtCtx = ctx.currentTime - MTG.offset;
  source.onended = () => {
    if (MTG.source !== source || MTG.token !== token) return;
    stopMtgAudio();
    done();
  };
  if (MTG.safety) clearTimeout(MTG.safety);
  MTG.safety = setTimeout(() => {
    if (MTG.token !== token || MTG.source !== source) return;
    stopMtgAudio();
    done();
  }, (buffer.duration - MTG.offset + 2) * 1000);
  ctx.resume().catch(() => {}).then(() => {
    if (MTG.token !== token || MTG.source !== source) return;
    try { source.start(0, MTG.offset); } catch (err) {}
  });
}
async function playMtgBlob(blob, token, done, text) {
  const ctx = mtgAudioCtx();
  try {
    if (!ctx) throw new Error('no audio context');
    const data = await blob.arrayBuffer();
    if (MTG.token !== token) return;
    const buffer = await ctx.decodeAudioData(data);
    if (MTG.token !== token) return;
    startMtgBuffer(buffer, 0, token, done);
  } catch (err) {
    if (MTG.token !== token) return;
    stopMtgAudio();
    playMtgBrowserVoice(text, token, done);
  }
}
function playMtgBrowserVoice(text, token, done) {
  if (!('speechSynthesis' in window)) {
    toast('Озвучка недоступна: нет системного синтеза');
    done();
    return;
  }
  const utter = new SpeechSynthesisUtterance(text);
  utter.lang = 'ru-RU';
  utter.rate = 1.02;
  const voices = speechSynthesis.getVoices().filter((voice) => /^ru/i.test(voice.lang));
  if (voices.length) utter.voice = voices[0];
  MTG.fake = true;
  utter.onend = () => {
    if (MTG.token !== token) return;
    MTG.fake = false;
    done();
  };
  utter.onerror = () => {
    if (MTG.token !== token) return;
    MTG.fake = false;
    done();
  };
  speechSynthesis.speak(utter);
}
async function speakStep(step) {
  const total = MTG.items.length;
  if (step > total || !MTG.data) return finishSpeech();
  const token = ++MTG.token;
  stopMtgAudio();
  MTG.step = step;
  if (step >= 0 && step < total) MTG.covered.add(step);
  MTG.mode = 'speaking';
  $('mtgPlay').textContent = 'Пауза';
  $('mtgNext').disabled = false;
  startMtgTimer();
  paintMtgAgenda();
  const line = step < 0 ? MTG.data.intro : (step >= total ? MTG.data.outro : MTG.items[step].say);
  mtgCaption(line);
  const next = () => {
    if (MTG.token !== token || MTG.mode === 'paused') return;
    if (step >= total) return finishSpeech();
    speakStep(step + 1);
  };
  try {
    const res = await fetch('/api/avatar/say', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text: line, employee: MTG.employee || ''})
    });
    if (!res.ok) throw new Error('tts');
    const blob = await res.blob();
    if (MTG.token !== token) return;
    playMtgBlob(blob, token, next, line);
  } catch (err) {
    if (MTG.token !== token) return;
    playMtgBrowserVoice(line, token, next);
  }
}
function finishSpeech() {
  MTG.mode = 'done';
  MTG.token++;
  stopMtgAudio();
  $('mtgPlay').textContent = 'Повторить';
  $('mtgNext').disabled = true;
  paintMtgAgenda();
  if (MTG.data) mtgCaption(MTG.data.outro);
  toast('Встреча проведена — решения за вами');
}
function mtgToggle() {
  if (MTG.mode === 'speaking') {
    const ctx = mtgAudioCtx();
    if (MTG.source && ctx) {
      MTG.offset = Math.max(0, ctx.currentTime - MTG.startedAtCtx);
      if (MTG.safety) {
        clearTimeout(MTG.safety);
        MTG.safety = 0;
      }
      const source = MTG.source;
      MTG.source = null;
      try { source.onended = null; source.stop(); } catch (err) {}
      MTG.analyser = null;
    } else if (window.speechSynthesis) {
      speechSynthesis.pause();
    }
    MTG.fake = false;
    MTG.mode = 'paused';
    $('mtgPlay').textContent = 'Продолжить';
    return;
  }
  if (MTG.mode === 'paused') {
    if (MTG.buffer && MTG.onDone) {
      startMtgBuffer(MTG.buffer, MTG.offset, MTG.token, MTG.onDone);
    } else if (window.speechSynthesis) {
      speechSynthesis.resume();
      MTG.fake = true;
    }
    MTG.mode = 'speaking';
    $('mtgPlay').textContent = 'Пауза';
    return;
  }
  speakStep(-1);
}
function mtgNextStep() {
  if (!MTG.data) return;
  if (MTG.mode === 'idle') return speakStep(-1);
  speakStep(Math.min(MTG.step + 1, MTG.items.length));
}
function mtgLoop() {
  const data = new Uint8Array(1024);
  let smooth = 0;
  const photo = $('mtgPhoto');
  const rim = $('mtgRim');
  const bars = $('mtgWave').children;
  const tick = () => {
    MTG.raf = requestAnimationFrame(tick);
    const now = performance.now() / 1000;
    let level = 0;
    if (MTG.analyser) {
      MTG.analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i++) {
        const value = (data[i] - 128) / 128;
        sum += value * value;
      }
      const rms = Math.sqrt(sum / data.length);
      level = Math.max(0, Math.min(1, (rms - 0.012) * 16));
    } else if (MTG.fake) {
      level = 0.22 + 0.42 * Math.abs(Math.sin(now * 9.3)) * (0.6 + 0.4 * Math.sin(now * 23));
    }
    smooth += (level - smooth) * 0.35;
    rim.style.opacity = (0.25 + smooth * 0.55).toFixed(2);
    const breathe = Math.sin(now * 1.4) * 1.6;
    const sway = Math.sin(now * 0.62) * 1.2;
    const nod = smooth * Math.sin(now * 5.1) * 1.6;
    const zoom = 1 + smooth * 0.022;
    photo.style.transform = 'scale(' + zoom.toFixed(3) + ') translate('
      + sway.toFixed(2) + 'px,' + (breathe + nod).toFixed(2) + 'px)';
    for (let i = 0; i < bars.length; i++) {
      const height = 5 + smooth * 22 * (0.35 + 0.65 * Math.abs(Math.sin(now * (7 + i * 1.7) + i)));
      bars[i].style.height = height.toFixed(1) + 'px';
    }
  };
  tick();
}
function stopMtgLoop() {
  cancelAnimationFrame(MTG.raf);
  MTG.raf = 0;
  stopMtgAudio();
  MTG.fake = false;
  $('mtgRim').style.opacity = '0.25';
  $('mtgPhoto').style.transform = 'none';
  for (const bar of $('mtgWave').children) bar.style.height = '5px';
}
async function openMeeting(employee) {
  MTG.employee = typeof employee === 'string' ? employee : '';
  const query = MTG.employee ? '?employee=' + encodeURIComponent(MTG.employee) : '';
  try {
    MTG.data = await api('/api/meeting' + query);
  } catch (err) {
    toast('Встреча: ' + err.message);
    return;
  }
  MTG.items = MTG.data.items || [];
  MTG.step = -2;
  MTG.mode = 'idle';
  MTG.token++;
  MTG.covered = new Set();
  MTG.channel = '';
  stopMtgLoop();
  resetMtgTimer();
  $('meeting').hidden = false;
  document.body.style.overflow = 'hidden';
  $('mtgSub').textContent = MTG.data.subtitle || 'Планёрка';
  $('mtgTimer').textContent = '00:00';
  $('mtgPlay').textContent = 'Начать встречу';
  $('mtgNext').disabled = true;
  $('mtgZoomLink').value = '';
  $('mtgTmLink').value = '';
  updateMtgJoinNotes();
  updateMtgPip();
  fillMtgWho();
  mtgCaption(MTG.employee
    ? 'Нажмите «Начать встречу» — Ася поздоровается с сотрудником голосом и проведёт встречу по вопросам из переписки.'
    : 'Нажмите «Начать встречу» — Ася поприветствует вас голосом и проведёт по повестке.');
  renderMtgAgenda();
  mtgLoop();
}
function mtgIsLocal() {
  return location.hostname === '127.0.0.1' || location.hostname === 'localhost';
}
function updateMtgJoinNotes() {
  const data = MTG.data || {};
  const note = (provider, nodeId) => {
    const info = data[provider] || {};
    const name = provider === 'zoom' ? 'Zoom' : 'Телемост';
    let text;
    if (info.link_set && mtgIsLocal()) {
      text = provider === 'zoom'
        ? 'Комната Zoom сохранена: «Подключиться» откроет приложение Zoom на этом ПК.'
        : 'Комната Телемоста сохранена: «Войти» откроет её без ввода.';
    } else if (info.link_set) text = 'Комната ' + name + ' сохранена. На стенде адрес не раскрывается — вставьте ссылку, чтобы открыть встречу здесь.';
    else text = 'Ссылка ' + name + ' не сохранена: вставьте и нажмите «Войти».';
    if (MTG.channel === provider) text += ' Встреча идёт здесь.';
    $(nodeId).textContent = text;
  };
  note('zoom', 'mtgZoomNote');
  note('telemost', 'mtgTmNote');
  const web = $('mtgZoomWeb');
  if (web) web.hidden = !((data.zoom || {}).link_set && mtgIsLocal());
}
function updateMtgPip() {
  const attendee = MTG.data && MTG.data.attendee;
  const img = $('mtgPipImg');
  const initials = $('mtgPipIni');
  const name = $('mtgPipName');
  if (!img || !initials || !name) return;
  if (attendee && attendee.name) {
    img.hidden = true;
    initials.hidden = false;
    initials.textContent = attendee.name.split(/\s+/).map((word) => word[0] || '').join('').slice(0, 2).toUpperCase();
    const position = attendee.position || attendee.role || 'сотрудник';
    name.textContent = attendee.name.split(/\s+/)[0] + ' · ' + position.charAt(0).toLowerCase() + position.slice(1);
  } else {
    img.hidden = false;
    initials.hidden = true;
    const leader = (MTG.data && MTG.data.profile ? MTG.data.profile.split(/\s+/)[0] : 'Иван');
    name.textContent = leader + ' · руководитель';
  }
}
async function fillMtgWho() {
  const select = $('mtgWho');
  if (!select) return;
  const leader = (MTG.data && MTG.data.profile) || 'Руководитель';
  const current = MTG.employee || '';
  select.innerHTML = '';
  const own = document.createElement('option');
  own.value = '';
  own.textContent = leader + ' · руководитель';
  select.appendChild(own);
  if (!MTG.people) {
    try {
      const data = await api('/api/chat?period=today');
      MTG.people = data.employees || [];
    } catch (err) {
      MTG.people = [];
    }
  }
  for (const person of MTG.people) {
    if (!person || person.name === leader) continue;
    const option = document.createElement('option');
    option.value = person.name;
    option.textContent = person.name + (person.position ? ' · ' + person.position : '');
    select.appendChild(option);
  }
  select.value = current;
}
function joinChannel(provider, inputId) {
  const input = inputId ? $(inputId) : null;
  const link = input ? (input.value || '').trim() : '';
  const name = provider === 'zoom' ? 'Zoom' : 'Телемост';
  const info = (MTG.data && MTG.data[provider]) || {};
  if (!link) {
    if (info.link_set && mtgIsLocal()) {
      window.open(provider === 'zoom' ? '/api/zoom/app' : '/api/join/' + provider, '_blank', 'noopener');
      MTG.channel = provider;
      updateMtgJoinNotes();
      toast(provider === 'zoom' ? 'Открываю Zoom на этом компьютере' : 'Открываю сохранённую комнату ' + name);
      return;
    }
    toast(info.link_set
      ? name + ': комната сохранена, но на стенде адрес не раскрывается — вставьте ссылку'
      : name + ': вставьте ссылку на комнату');
    if (input) input.focus();
    return;
  }
  const tab = window.open('', '_blank');
  fetch('/api/meeting/join', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({provider, link})
  }).then(async (res) => {
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || 'ссылка не подошла');
    if (tab) tab.location = data.url;
    else toast('Разрешите всплывающие окна и нажмите «Войти» ещё раз');
    if (input) input.value = '';
    if (data.channels) {
      state.data.channels = data.channels;
      renderChannels();
    }
    if (MTG.data) {
      MTG.channel = provider;
      MTG.data[provider] = {configured: true, link_set: true, mode: 'link'};
      updateMtgJoinNotes();
    }
    toast(name + (data.saved ? ': комната сохранена, открываю встречу' : ': открываю встречу'));
  }).catch((err) => {
    if (tab) tab.close();
    const detail = String(err.message || '').replace(/^(zoom|telemost):\s*/, '');
    toast(name + ': ' + detail);
  });
}
function closeMeeting() {
  const started = MTG.startedAt > 0;
  const seconds = started ? Math.round((Date.now() - MTG.startedAt) / 1000) : 0;
  const covered = Array.from(MTG.covered).sort((a, b) => a - b);
  const channel = MTG.channel;
  MTG.token++;
  MTG.mode = 'idle';
  stopMtgLoop();
  resetMtgTimer();
  $('meeting').hidden = true;
  document.body.style.overflow = '';
  if (!started) return;
  api('/api/meeting/done', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({items: covered.length, seconds: seconds, covered: covered, channel: channel, employee: MTG.employee || ''})
  }).then((data) => {
    toast('Встреча завершена: ' + minuteDuration(seconds) + ' · освещено ' + covered.length + ' из ' + MTG.items.length);
    openMinutes(data.id);
    load().catch(() => {});
  }).catch(() => {
    toast('Встреча завершена: ' + minuteDuration(seconds));
  });
}
function openMeetingZoom() {
  const data = MTG.data || {};
  const saved = ['zoom', 'telemost'].filter((provider) => data[provider] && data[provider].link_set);
  if (saved.length && mtgIsLocal()) {
    const provider = saved[0];
    window.open(provider === 'zoom' ? '/api/zoom/app' : '/api/join/' + provider, '_blank', 'noopener');
    MTG.channel = provider;
    updateMtgJoinNotes();
    toast(provider === 'zoom' ? 'Открываю Zoom на этом компьютере — комната сохранена' : 'Открываю комнату Телемоста');
    return;
  }
  $('mtgZoomLink').focus();
  toast(saved.length
    ? 'Комната сохранена, но на стенде адрес не раскрывается — вставьте ссылку в поле Zoom'
    : 'Вставьте ссылку на комнату Zoom или Телемоста и нажмите «Войти»');
}
function minuteDuration(seconds) {
  const total = Math.max(0, Math.round(seconds || 0));
  if (total >= 3600) return Math.floor(total / 3600) + ' ч ' + Math.round((total % 3600) / 60) + ' мин';
  if (total >= 60) {
    const rest = total % 60;
    return Math.floor(total / 60) + ' мин' + (rest ? ' ' + rest + ' с' : '');
  }
  return total + ' с';
}
function minuteStamp(value) {
  if (!value) return '—';
  const stamp = new Date(value);
  if (isNaN(stamp.getTime())) return String(value);
  const pad = (n) => String(n).padStart(2, '0');
  return pad(stamp.getDate()) + '.' + pad(stamp.getMonth() + 1) + '.' + stamp.getFullYear() +
    ', ' + pad(stamp.getHours()) + ':' + pad(stamp.getMinutes());
}
function closeMinutes() {
  $('minGate').hidden = true;
}
function openMinutes(id) {
  $('minGate').hidden = false;
  $('minSub').textContent = 'Загружаю…';
  $('minDownload').hidden = true;
  $('minAll').hidden = !id;
  $('minBody').innerHTML = '';
  if (!id) {
    $('minTitle').textContent = 'Все протоколы встреч';
    api('/api/minutes?limit=100').then((data) => renderMinuteHistory(data.minutes || [])).catch((err) => {
      $('minSub').textContent = 'Протоколы недоступны: ' + err.message;
    });
    return;
  }
  $('minTitle').textContent = 'Протокол планёрки';
  api('/api/minutes/' + id).then(renderMinute).catch((err) => {
    $('minSub').textContent = 'Протокол недоступен: ' + err.message;
  });
}
function renderMinuteHistory(minutes) {
  $('minSub').textContent = minutes.length
    ? 'Сохранено: ' + minutes.length + ' · откройте любой и поставьте задачи'
    : 'Протоколов пока нет';
  const body = $('minBody');
  body.innerHTML = '';
  if (!minutes.length) {
    body.appendChild(el('p', 'mini', 'Проведите встречу и завершите её — Ася запишет протокол с повесткой и освещёнными пунктами.'));
    return;
  }
  const list = el('div', 'min-list');
  for (const minute of minutes) {
    const row = el('button', 'min-row');
    row.type = 'button';
    row.appendChild(el('b', null, minuteStamp(minute.finished_at)));
    const items = minute.items || [];
    const covered = items.filter((item) => item.covered).length;
    row.appendChild(el('span', null, minuteDuration(minute.seconds) + ' · ' +
      (minute.channel === 'zoom' ? 'Zoom' : minute.channel === 'telemost' ? 'Телемост' : 'без канала')));
    row.appendChild(el('em', null, covered + ' из ' + items.length + ' · задач ' +
      (minute.tasks_total ? minute.tasks_done + '/' + minute.tasks_total : 'нет')));
    row.onclick = () => openMinutes(minute.id);
    list.appendChild(row);
  }
  body.appendChild(list);
  body.appendChild(el('p', 'mini', 'Протоколы хранятся в журнале AsiStantAI и выгружаются в Markdown. Автоотправок нет.'));
}
function dueLabel(value) {
  const parts = String(value || '').split('-');
  return parts.length === 3 ? parts[2] + '.' + parts[1] + '.' + parts[0] : (value || '');
}
function renderMinuteTasks(minute, items, body) {
  const wrap = el('div', 'min-tasks');
  wrap.appendChild(el('h4', null, 'Задачи по встрече'));
  const tasks = minute.tasks || [];
  if (tasks.length) {
    const list = el('ul', 'min-tasks-list');
    for (const task of tasks) {
      const row = el('li', 'min-task' + (task.status === 'done' ? ' done' : ''));
      const box = el('div');
      box.appendChild(el('b', null, task.title));
      const meta = [];
      if (task.item_index > 0) meta.push('к пункту ' + task.item_index);
      if (task.owner) meta.push('исполнитель: ' + task.owner);
      if (task.due) meta.push('срок: ' + dueLabel(task.due));
      box.appendChild(el('small', null, meta.join(' · ') || 'исполнитель и срок не заданы'));
      row.appendChild(box);
      row.appendChild(actionButton(task.status === 'done' ? 'Вернуть' : 'Выполнено', 'btn ghost',
        () => setMinuteTask(minute.id, task.id, task.status === 'done' ? 'open' : 'done')));
      list.appendChild(row);
    }
    wrap.appendChild(list);
  } else {
    wrap.appendChild(el('p', 'mini', 'Задач пока нет: выберите пункт повестки, впишите поручение и нажмите «Поставить».'));
  }
  const form = el('div', 'min-form');
  const select = el('select');
  select.id = 'minTaskItem';
  select.appendChild(new Option('Вся встреча', '0'));
  for (const item of items) select.appendChild(new Option(item.index + '. ' + item.title, String(item.index)));
  const title = el('input');
  title.id = 'minTaskTitle';
  title.placeholder = 'Поручение';
  title.maxLength = 200;
  const owner = el('input');
  owner.id = 'minTaskOwner';
  owner.placeholder = 'Исполнитель';
  owner.maxLength = 100;
  const due = el('input');
  due.id = 'minTaskDue';
  due.type = 'date';
  select.onchange = () => {
    const value = Number(select.value || 0);
    title.value = value > 0 && items[value - 1] ? items[value - 1].title : '';
  };
  form.appendChild(select);
  form.appendChild(title);
  form.appendChild(owner);
  form.appendChild(due);
  form.appendChild(actionButton('Поставить', 'btn primary', () => addMinuteTask(minute.id)));
  wrap.appendChild(form);
  wrap.appendChild(el('p', 'mini', 'Задача остаётся в журнале и отмечается выполненной в один клик. Автоотправок исполнителю нет.'));
  body.appendChild(wrap);
}
async function addMinuteTask(minuteId) {
  const title = ($('minTaskTitle').value || '').trim();
  if (title.length < 2) {
    toast('Задача: впишите текст поручения');
    $('minTaskTitle').focus();
    return;
  }
  try {
    await api('/api/minutes/' + minuteId + '/tasks', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        item_index: Number($('minTaskItem').value || 0),
        title: title,
        owner: ($('minTaskOwner').value || '').trim(),
        due: ($('minTaskDue').value || '').trim()
      })
    });
    toast('Задача поставлена по протоколу');
    openMinutes(minuteId);
    load().catch(() => {});
  } catch (err) {
    toast('Задача: ' + err.message);
  }
}
function setMinuteTask(minuteId, taskId, status) {
  api('/api/minutes/' + minuteId + '/tasks/' + taskId + '/state', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({status: status})
  }).then(() => {
    toast(status === 'done' ? 'Задача отмечена выполненной' : 'Задача возвращена в работу');
    openMinutes(minuteId);
    load().catch(() => {});
  }).catch((err) => toast('Задача: ' + err.message));
}
function renderMinute(minute) {
  const items = minute.items || [];
  const covered = items.filter((item) => item.covered).length;
  $('minSub').textContent = minuteStamp(minute.finished_at) + ' · ' + minuteDuration(minute.seconds) +
    (minute.channel ? ' · ' + (minute.channel === 'zoom' ? 'Zoom' : 'Телемост') : '');
  const download = $('minDownload');
  download.href = '/api/minutes/' + minute.id + '.md';
  download.hidden = false;
  const body = $('minBody');
  body.innerHTML = '';
  const meta = el('div', 'min-meta');
  meta.appendChild(el('span', null, 'Ведущая: Ася · AsiStantAI'));
  if (minute.profile) meta.appendChild(el('span', null, 'Руководитель: ' + minute.profile));
  if (minute.employee) meta.appendChild(el('span', null, 'Сотрудник: ' + minute.employee +
    (minute.employee_position ? ', ' + minute.employee_position : '')));
  if (minute.week) meta.appendChild(el('span', null, minute.week));
  meta.appendChild(el('span', null, 'Освещено: ' + covered + ' из ' + items.length));
  body.appendChild(meta);
  if (items.length) {
    const list = el('ol', 'min-agenda');
    for (const item of items) {
      const row = el('li', 'min-item' + (item.covered ? ' done' : ''));
      row.appendChild(el('b', null, item.index + '. ' + item.title));
      row.appendChild(el('small', null, (item.covered ? 'освещено · ' : 'не рассмотрено · ') + item.fact));
      list.appendChild(row);
    }
    body.appendChild(list);
  }
  renderMinuteTasks(minute, items, body);
  body.appendChild(el('p', 'mini', 'Решения и поручения утверждает руководитель; автоотправок нет.'));
}
function channelRow(name, value, data) {
  const row = el('div', 'ch');
  row.appendChild(el('span', null, name + (value ? ' · ' + value : '')));
  let label = 'не настроен';
  let cls = 'warn';
  if (data && data.configured) { label = 'настроен'; cls = 'ok'; }
  if (data && data.ok === false) { label = 'ошибка'; cls = 'warn'; }
  if (data && data.ok === true) { label = 'готов'; cls = 'ok'; }
  const badge = el('span', 'badge ' + cls, label);
  if (data && data.detail) badge.title = data.detail;
  row.appendChild(badge);
  return row;
}
function renderChannels() {
  const box = $('channels');
  box.innerHTML = '';
  const data = state.data.channels || {};
  const accounts = data.accounts || {};
  const labels = Object.keys(accounts);
  if (!labels.length) box.appendChild(el('div', null, 'Ящики не заданы.'));
  for (const label of labels) {
    const acc = accounts[label] || {};
    const name = label === 'a' ? 'Ящик А' : label === 'b' ? 'Ящик Б' : (acc.address || 'Ящик ' + label);
    box.appendChild(channelRow(name, acc.address || '', acc));
  }
  box.appendChild(channelRow('Telegram', (data.telegram && data.telegram.configured) ? 'бот' : '', data.telegram || {}));
  const zoom = data.zoom || {};
  const zoomMode = zoom.mode === 'login' ? 'вход выполнен'
    : zoom.mode === 'api' ? 'API'
    : (zoom.mode === 'link' ? 'постоянная ссылка' : '');
  box.appendChild(channelRow('Zoom', zoomMode, zoom));
  const telemost = data.telemost || {};
  box.appendChild(channelRow('Телемост', telemost.configured ? 'постоянная комната' : '', telemost));
  const tgUser = data.telegram_user || {};
  box.appendChild(channelRow('Telegram (мой)', tgUser.authorized ? 'аккаунт' : '', Object.assign({}, tgUser, {configured: !!tgUser.authorized})));
  const bitrix = data.bitrix24 || {};
  box.appendChild(channelRow('Битрикс24', bitrix.portal || (bitrix.configured ? 'вебхук' : ''), bitrix));
  box.appendChild(el('div', 'mini', data.live_ready
    ? 'Живой режим готов: письма из IMAP, отправка после утверждения.'
    : 'Демо на датасете. Для живого режима заполните .env: MAIL_A_*, MAIL_B_*, TELEGRAM_*.'));
  if (!zoom.configured) {
    box.appendChild(el('div', 'mini', 'Zoom: войдите в Zoom или сохраните постоянную ссылку на комнату.'));
  }
  if (!telemost.configured) {
    box.appendChild(el('div', 'mini', telemost.invalid
      ? 'Телемост: сохранённая ссылка должна быть на telemost.yandex.ru.'
      : 'Телемост: сохраните ссылку на комнату telemost.yandex.ru/j/…'));
  }
  if (!tgUser.authorized) {
    box.appendChild(el('div', 'mini', tgUser.configured
      ? 'Мой Telegram: сессия не авторизована — python -m app.cli tg-login.'
      : 'Мой Telegram: TELEGRAM_API_ID и TELEGRAM_API_HASH в .env, затем tg-login.'));
  }
  if (!bitrix.configured) {
    box.appendChild(el('div', 'mini', bitrix.invalid
      ? 'Битрикс24: адрес вебхука должен быть https://<портал>/rest/<id>/<код>/.'
      : 'Битрикс24: входящий вебхук портала — BITRIX24_WEBHOOK_URL в .env.'));
  }
  renderZoomPanel();
}
function renderEcon() {
  const box = $('econ');
  const e = state.data.economics || {};
  box.innerHTML = '';
  if (!e.drafts_per_month) { box.textContent = 'Нет данных.'; return; }
  const m = e.meetings || {};
  const week = m.per_week || {};
  const month = m.per_month || {};
  const hours = m.hours_per_month || {};
  const review = m.review_cost || {};
  const span = (low, high, suffix) => num(low) + '–' + num(high) + (suffix || '');
  const rows = [
    ['Почта в день', span(e.emails_per_day_min, e.emails_per_day_max)],
    ['Обращений в месяц', span(e.requests_per_month_min, e.requests_per_month_max)],
    ['Черновиков в месяц', span(e.drafts_per_month_min, e.drafts_per_month_max)],
    ['Стоимость черновика', num(e.cost_per_draft) + ' ₽'],
    ['Встреч в неделю', span(week.min, week.max) + ' · ' + span(hours.min, hours.max) + ' ч'],
    ['Протоколов в месяц', span(month.min, month.max)],
    ['Ручная обработка почты', span(e.manual_cost_min, e.manual_cost_max, ' ₽')],
    ['Агент: почта', span(e.agent_cost_min, e.agent_cost_max, ' ₽')],
    ['Агент: встречи', span(review.min, review.max, ' ₽')]
  ];
  for (const [label, value] of rows) {
    const line = el('div', 'econ-line');
    line.appendChild(el('span', null, label));
    line.appendChild(el('b', null, value));
    box.appendChild(line);
  }
  const total = el('div', 'econ-line econ-total');
  total.appendChild(el('span', null, 'Экономия в месяц'));
  total.appendChild(el('b', null, span(e.total_saving_min, e.total_saving_max, ' ₽')));
  box.appendChild(total);
  box.appendChild(el('div', 'mini', 'Почта и встречи. Окупаемость внедрения: '
    + span(e.total_payback_months_min, e.total_payback_months_max) + ' мес · 0 автоотправок'));
  box.appendChild(el('div', 'mini', 'Встречи: ' + m.minutes_per_meeting + ' мин, протокол '
    + m.review_minutes + ' мин проверки вместо ' + m.manual_minutes + ' мин вручную'));
}
function statusPill(status) {
  const known = ['waiting', 'approved', 'in_progress', 'done', 'open'];
  const cls = known.indexOf(status) >= 0 ? status : 'waiting';
  return el('span', 'pill status-' + cls, TASK_STATUS[status] || status);
}
function taskLabel(task) {
  const owner = task.owner ? task.owner + ' — ' : '';
  const deadline = task.deadline ? ' · ' + task.deadline : '';
  return owner + task.text + deadline;
}
function actionButton(label, cls, handler) {
  const button = el('button', cls, label);
  button.onclick = handler;
  return button;
}
function noteFor(protocol, index) {
  const node = $('note-' + taskKey(protocol, index));
  return node ? node.value.trim() : '';
}
async function approveTask(protocol, index) {
  try {
    await api('/api/protocols/' + encodeURIComponent(protocol.id) + '/tasks/' + index + '/approve', {method: 'POST'});
    await load();
    toast('Поручение поставлено (демо)');
  } catch (err) { toast('Ошибка: ' + err.message); }
}
async function setTaskStatus(protocol, index, status, note) {
  try {
    await api('/api/protocols/' + encodeURIComponent(protocol.id) + '/tasks/' + index + '/state', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status: status, note: note !== undefined ? note : noteFor(protocol, index)})
    });
    await load();
    toast(status === 'done' ? 'Задача отмечена решённой' : status === 'open' ? 'Отмечено: вопрос не решён' : 'Задача в работе');
  } catch (err) { toast('Статус не изменился: ' + err.message); }
}
async function syncTask(protocol, index) {
  try {
    const result = await api('/api/protocols/' + encodeURIComponent(protocol.id) + '/tasks/' + index + '/sync', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({note: noteFor(protocol, index)})
    });
    await load();
    toast(result.channel === 'telegram' ? 'Руководитель уведомлён в Telegram' : 'Синхронизация с руководителем зафиксирована (демо)');
  } catch (err) { toast('Не удалось синхронизировать: ' + err.message); }
}
function reviseForm(protocol, index, task) {
  const form = el('div', 'task-form');
  const text = el('textarea');
  text.value = task.text || '';
  text.placeholder = 'Новая формулировка задачи';
  const owner = el('input');
  owner.value = task.owner || '';
  owner.placeholder = 'Владелец';
  const deadline = el('input');
  deadline.value = task.deadline || '';
  deadline.placeholder = 'Срок';
  const reason = el('input');
  reason.placeholder = 'Почему задача изменилась (увидит руководитель)';
  const buttons = el('div', 'wide actions');
  const save = actionButton('Сохранить правку', 'btn primary', async () => {
    if (!text.value.trim()) { toast('Формулировка не может быть пустой'); return; }
    try {
      await api('/api/protocols/' + encodeURIComponent(protocol.id) + '/tasks/' + index + '/revise', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: text.value.trim(), owner: owner.value, deadline: deadline.value, reason: reason.value})
      });
      state.revising = null;
      await load();
      toast('Задача скорректирована — синхронизируйте с руководителем');
    } catch (err) { toast('Не удалось сохранить: ' + err.message); }
  });
  const cancel = actionButton('Отмена', 'btn ghost', () => { state.revising = null; renderProtocols(); });
  buttons.appendChild(save);
  buttons.appendChild(cancel);
  form.appendChild(text);
  form.appendChild(owner);
  form.appendChild(deadline);
  form.appendChild(reason);
  form.appendChild(buttons);
  return form;
}
async function setStepStatus(protocol, index, step, status) {
  try {
    await api('/api/protocols/' + encodeURIComponent(protocol.id) + '/tasks/' + index + '/steps/' + step.index + '/state', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status: status})
    });
    await load();
    toast(status === 'done' ? 'Шаг закрыт' : 'Шаг в работе');
  } catch (err) { toast('Шаг не изменился: ' + err.message); }
}
function stepRow(protocol, index, task, step) {
  const row = el('div', 'step');
  const head = el('div', 'step-head');
  const left = el('div');
  const title = el('div');
  title.appendChild(el('b', null, (step.index + 1) + '. '));
  title.appendChild(el('span', 'role', step.role || 'исполнитель'));
  title.appendChild(el('span', null, ' · ' + step.title));
  left.appendChild(title);
  const meta = [];
  if (step.owner) meta.push('ответственный: ' + step.owner);
  if (step.deadline) meta.push('срок: ' + step.deadline);
  if (step.parallel) meta.push('параллельно');
  else if (step.depends_on.length) meta.push('после шагов: ' + step.depends_on.map((d) => d + 1).join(', '));
  if (step.blocked_by && step.blocked_by.length) meta.push('ждёт шаги: ' + step.blocked_by.map((d) => d + 1).join(', '));
  left.appendChild(el('div', 'steps-meta', meta.join(' · ') || 'без зависимостей'));
  head.appendChild(left);
  const actions = el('div', 'step-actions');
  actions.appendChild(el('span', 'pill status-' + step.status, STEP_STATUS[step.status] || step.status));
  const blocked = (step.blocked_by || []).length > 0;
  if (step.status !== 'done') {
    const start = actionButton('В работу', 'btn', () => setStepStatus(protocol, index, step, 'in_progress'));
    start.disabled = blocked || step.status === 'in_progress';
    if (blocked) start.title = 'Сначала закройте шаги: ' + step.blocked_by.map((d) => d + 1).join(', ');
    actions.appendChild(start);
    const done = actionButton('Готово', 'btn primary', () => setStepStatus(protocol, index, step, 'done'));
    done.disabled = blocked;
    if (blocked) done.title = 'Сначала закройте шаги: ' + step.blocked_by.map((d) => d + 1).join(', ');
    actions.appendChild(done);
  } else {
    actions.appendChild(actionButton('Вернуть', 'btn ghost', () => setStepStatus(protocol, index, step, 'in_progress')));
  }
  head.appendChild(actions);
  row.appendChild(head);
  return row;
}
function stepsBlock(protocol, index, task) {
  const wrap = el('div', 'steps');
  for (const step of task.steps) wrap.appendChild(stepRow(protocol, index, task, step));
  return wrap;
}
async function applyPreset(protocol, index, task, name) {
  if (task.steps_total && !confirm('Заменить текущие шаги сценарием «' + name + '»?')) return;
  try {
    await api('/api/protocols/' + encodeURIComponent(protocol.id) + '/tasks/' + index + '/steps', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({steps: STEP_PRESETS[name], mode: 'replace'})
    });
    state.stepPanel.add(taskKey(protocol, index));
    await load();
    toast('Сценарий разложен на шаги');
  } catch (err) { toast('Не удалось разложить: ' + err.message); }
}
function stepPanel(protocol, index, task) {
  const wrap = el('div');
  if (!task.steps_total) {
    wrap.appendChild(el('div', 'mini', 'Типовой сценарий для смежных служб или ручной шаг. Последовательные шаги ждут закрытия предыдущих, параллельные идут одновременно.'));
  }
  const presets = el('div', 'preset-row');
  for (const name of Object.keys(STEP_PRESETS)) {
    presets.appendChild(actionButton(name, 'btn', () => applyPreset(protocol, index, task, name)));
  }
  wrap.appendChild(presets);
  const form = el('div', 'task-form');
  const title = el('input');
  title.placeholder = 'Шаг: что сделать';
  const role = el('input');
  role.placeholder = 'Роль / служба';
  const owner = el('input');
  owner.placeholder = 'Ответственный';
  const deadline = el('input');
  deadline.placeholder = 'Срок';
  const depends = el('select');
  const parallel = el('option', null, 'Параллельно');
  parallel.value = '';
  depends.appendChild(parallel);
  for (const step of task.steps || []) {
    const option = el('option', null, 'После шага ' + (step.index + 1));
    option.value = String(step.index);
    depends.appendChild(option);
  }
  const add = actionButton('Добавить шаг', 'btn primary', async () => {
    if (!title.value.trim()) { toast('Опишите шаг'); return; }
    const dep = depends.value === '' ? [] : [parseInt(depends.value, 10)];
    try {
      await api('/api/protocols/' + encodeURIComponent(protocol.id) + '/tasks/' + index + '/steps', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({steps: [{
          title: title.value.trim(),
          role: role.value,
          owner: owner.value,
          deadline: deadline.value,
          depends_on: dep
        }], mode: 'append'})
      });
      await load();
      toast('Шаг добавлен');
    } catch (err) { toast('Не удалось добавить шаг: ' + err.message); }
  });
  form.appendChild(title);
  form.appendChild(role);
  form.appendChild(owner);
  form.appendChild(deadline);
  form.appendChild(depends);
  form.appendChild(add);
  wrap.appendChild(form);
  return wrap;
}
function taskRow(protocol, index, task) {
  const key = taskKey(protocol, index);
  const li = el('li', 'task');
  const top = el('div', 'task-top');
  const txt = el('div', 'txt');
  txt.appendChild(el('div', null, taskLabel(task)));
  const badges = el('div', 'task-badges');
  badges.appendChild(statusPill(task.status));
  if (task.status_source) badges.appendChild(el('span', 'pill', 'по протоколу: ' + task.status_source));
  if (task.revised) {
    badges.appendChild(el('span', 'pill ' + (task.synced_at ? 'synced' : 'revised'), task.synced_at ? 'синхронизировано' : 'изменено · ждёт синхронизации'));
  }
  if (task.steps_total) badges.appendChild(el('span', 'pill', 'шаги ' + task.steps_done + '/' + task.steps_total));
  if (task.ready_to_close) badges.appendChild(el('span', 'pill', 'все шаги готовы — можно закрыть'));
  if (task.status !== 'waiting' && task.note) badges.appendChild(el('span', 'pill', task.note));
  txt.appendChild(badges);
  top.appendChild(txt);
  li.appendChild(top);

  const actions = el('div', 'task-actions');
  if (task.status === 'waiting') {
    actions.appendChild(actionButton('В задачи', 'btn primary', () => approveTask(protocol, index)));
  } else {
    if (task.status !== 'in_progress' && task.status !== 'done') {
      actions.appendChild(actionButton('В работу', 'btn', () => setTaskStatus(protocol, index, 'in_progress')));
    }
    if (task.status !== 'done') {
      actions.appendChild(actionButton('Решён', 'btn', () => setTaskStatus(protocol, index, 'done')));
    }
    if (task.status !== 'open') {
      actions.appendChild(actionButton('Не решён', 'btn ghost', () => setTaskStatus(protocol, index, 'open')));
    }
    if (task.status === 'done') {
      actions.appendChild(actionButton('Вернуть в работу', 'btn ghost', () => setTaskStatus(protocol, index, 'in_progress')));
    }
    if (task.revised && !task.synced_at) {
      actions.appendChild(actionButton('Синхронизировать', 'btn primary', () => syncTask(protocol, index)));
    }
  }
  actions.appendChild(actionButton('Изменить', 'btn ghost', () => {
    state.revising = state.revising === key ? null : key;
    renderProtocols();
  }));
  actions.appendChild(actionButton(state.stepPanel.has(key) ? 'Свернуть шаги' : 'Шаги', 'btn ghost', () => {
    if (state.stepPanel.has(key)) state.stepPanel.delete(key);
    else state.stepPanel.add(key);
    renderProtocols();
  }));
  li.appendChild(actions);

  if (task.status !== 'waiting') {
    const note = el('input', 'task-note');
    note.id = 'note-' + key;
    const current = task.note ? ' (сейчас: ' + task.note.slice(0, 80) + ')' : '';
    note.placeholder = 'Комментарий к статусу' + current;
    li.appendChild(note);
  }
  if (state.revising === key) li.appendChild(reviseForm(protocol, index, task));
  if (task.steps_total) li.appendChild(stepsBlock(protocol, index, task));
  if (state.stepPanel.has(key)) li.appendChild(stepPanel(protocol, index, task));
  return li;
}
function protocolDetails(title, items, ordered) {
  if (!items || !items.length) return null;
  const box = el('details');
  box.appendChild(el('summary', null, title + ' (' + items.length + ')'));
  const list = el(ordered ? 'ol' : 'ul');
  for (const item of items) list.appendChild(el('li', null, item));
  box.appendChild(list);
  return box;
}
function renderProtocols() {
  const box = $('protocols');
  box.innerHTML = '';
  const items = state.data.protocols || [];
  if (!items.length) { box.appendChild(el('div', 'empty', 'Протоколов нет.')); return; }
  const progress = state.data.protocol_progress || {};
  if (progress.tasks) {
    box.appendChild(el('p', 'mini', 'Задачи: ' + progress.tasks + ' · в работе ' + progress.in_progress +
      ' · решено ' + progress.resolved + ' · не решено ' + progress.unresolved +
      ' · правки к синхронизации ' + progress.unsynced + ' · шаги ' + progress.steps_done + '/' + progress.steps));
  }
  for (const protocol of items) {
    const card = el('div', 'proto');
    card.id = 'proto-' + protocol.id;
    const head = el('div', 'proto-head');
    const left = el('div');
    left.appendChild(el('h4', null, protocol.title));
    const total = protocol.tasks.length;
    const approved = protocol.approved_tasks || 0;
    let meta = 'Решений: ' + protocol.decisions.length + ' · поручений: ' + total + ' · открытых вопросов: ' + protocol.open_questions.length;
    if (total) meta += ' · в календаре: ' + approved;
    if (protocol.resolved_tasks) meta += ' · решено: ' + protocol.resolved_tasks;
    if (protocol.open_tasks) meta += ' · не решено: ' + protocol.open_tasks;
    if (protocol.unsynced_tasks) meta += ' · правки к синхронизации: ' + protocol.unsynced_tasks;
    if (protocol.steps_total) meta += ' · шаги: ' + protocol.steps_done + '/' + protocol.steps_total;
    if (protocol.next_steps && protocol.next_steps.length) meta += ' · шагов: ' + protocol.next_steps.length;
    left.appendChild(el('div', 'mini', meta));
    if (protocol.when || protocol.host) {
      const bits = [];
      if (protocol.when) bits.push(protocol.when);
      if (protocol.host) bits.push('ведущий: ' + protocol.host);
      left.appendChild(el('div', 'mini', bits.join(' · ')));
    }
    head.appendChild(left);
    if (approved < total) {
      const all = actionButton('Утвердить все', 'btn', async () => {
        all.disabled = true;
        try {
          await api('/api/protocols/' + encodeURIComponent(protocol.id) + '/approve', {method: 'POST'});
          await load();
          toast('Поручения переданы в календарь (демо)');
        } catch (err) { toast('Ошибка: ' + err.message); all.disabled = false; }
      });
      head.appendChild(all);
    }
    card.appendChild(head);
    for (const node of [
      protocolDetails('Повестка', protocol.agenda, true),
      protocolDetails('Решения', protocol.decisions, true),
      protocolDetails('Открытые вопросы и риски', protocol.open_questions, true),
      protocolDetails('Дальнейшие шаги', protocol.next_steps, false),
    ]) {
      if (node) card.appendChild(node);
    }
    if (protocol.participants) {
      const people = el('details');
      people.appendChild(el('summary', null, 'Участники'));
      people.appendChild(el('p', null, protocol.participants));
      card.appendChild(people);
    }
    if (total) {
      const ul = el('ul');
      const expanded = state.allProtocols.has(protocol.id);
      const shown = expanded ? total : Math.min(total, 5);
      for (let i = 0; i < shown; i++) ul.appendChild(taskRow(protocol, i, protocol.tasks[i]));
      card.appendChild(ul);
      if (total > 5) {
        const more = actionButton(expanded ? 'Свернуть' : 'Показать все ' + total, 'btn ghost', () => {
          if (expanded) state.allProtocols.delete(protocol.id);
          else state.allProtocols.add(protocol.id);
          renderProtocols();
        });
        more.style.cssText = 'padding:4px 10px;font-size:12px;margin-top:6px';
        card.appendChild(more);
      }
    }
    box.appendChild(card);
  }
}
const RAIL_SECTIONS = ['secMail', 'secMessages', 'secMeetings', 'secTasks', 'secCrm'];
function setRailBadge(id, count) {
  const node = $(id);
  if (!node) return;
  node.textContent = count;
  node.hidden = !count;
}
function renderRail() {
  const m = state.data && state.data.metrics ? state.data.metrics : {};
  const open = collectTasks().filter((row) => row.task.status !== 'done').length;
  setRailBadge('railMail', m.drafts || 0);
  setRailBadge('railTasks', open);
  setRailBadge('railMeetings', ((state.data && state.data.protocols) || []).length);
  setRailBadge('railMessages', (state.chat && state.chat.unread) || 0);
  setRailBadge('railCrm', (state.crm && state.crm.external) || 0);
}
function setRailActive(id) {
  for (const button of document.querySelectorAll('.rail-item')) {
    button.classList.toggle('on', button.dataset.sec === id);
  }
}
function railGo(id) {
  const target = $(id);
  if (!target) return;
  const smooth = !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  target.scrollIntoView({behavior: smooth ? 'smooth' : 'auto', block: 'start'});
  setRailActive(id);
  if (!smooth) return;
  window.setTimeout(() => {
    const top = target.getBoundingClientRect().top;
    if (top > 180 || top < 48) target.scrollIntoView({block: 'start'});
  }, 800);
}
function trackRail() {
  let current = RAIL_SECTIONS[0];
  for (const id of RAIL_SECTIONS) {
    const node = $(id);
    if (node && node.getBoundingClientRect().top <= 151) current = id;
  }
  setRailActive(current);
}
function collectTasks() {
  const rows = [];
  for (const protocol of (state.data && state.data.protocols) || []) {
    (protocol.tasks || []).forEach((task, index) => rows.push({protocol: protocol, index: index, task: task}));
  }
  return rows;
}
function deadlineKey(value) {
  const match = /(\d{2})\.(\d{2})\.(\d{4})/.exec(value || '');
  return match ? match[3] + match[2] + match[1] : '99999999';
}
function renderTasks() {
  const box = $('tasks');
  const stats = $('taskStats');
  if (!box || !stats) return;
  box.innerHTML = '';
  stats.innerHTML = '';
  const rows = collectTasks();
  if (!rows.length) { box.appendChild(el('div', 'empty', 'Поручений нет.')); return; }
  const counts = {waiting: 0, approved: 0, in_progress: 0, done: 0, open: 0};
  for (const row of rows) counts[row.task.status] = (counts[row.task.status] || 0) + 1;
  for (const text of [
    'Всего ' + rows.length,
    'ждут постановки ' + counts.waiting,
    'в календаре ' + counts.approved,
    'в работе ' + counts.in_progress,
    'решено ' + counts.done,
    'не решено ' + counts.open
  ]) stats.appendChild(el('span', 'pill', text));
  const order = {open: 0, in_progress: 1, approved: 2, waiting: 3, done: 4};
  rows.sort((a, b) => (order[a.task.status] ?? 9) - (order[b.task.status] ?? 9) ||
    deadlineKey(a.task.deadline).localeCompare(deadlineKey(b.task.deadline)));
  for (const row of rows) {
    const protocol = row.protocol;
    const index = row.index;
    const task = row.task;
    const node = el('div', 'board-row' + (task.status === 'done' ? ' done' : ''));
    const txt = el('div', 'txt');
    txt.appendChild(el('div', null, task.text || '—'));
    const meta = [];
    if (task.owner) meta.push('ответственный: ' + task.owner);
    if (task.deadline) meta.push('срок: ' + task.deadline);
    meta.push(protocol.title);
    txt.appendChild(el('div', 'mini', meta.join(' · ')));
    const badges = el('div', 'task-badges');
    badges.appendChild(statusPill(task.status));
    if (task.steps_total) badges.appendChild(el('span', 'pill', 'шаги ' + task.steps_done + '/' + task.steps_total));
    txt.appendChild(badges);
    node.appendChild(txt);
    const actions = el('div', 'task-actions');
    const keepNote = task.note || '';
    if (task.status === 'waiting') {
      actions.appendChild(actionButton('В задачи', 'btn primary', () => approveTask(protocol, index)));
    } else {
      if (task.status !== 'in_progress' && task.status !== 'done') {
        actions.appendChild(actionButton('В работу', 'btn', () => setTaskStatus(protocol, index, 'in_progress', keepNote)));
      }
      if (task.status !== 'done') {
        actions.appendChild(actionButton('Решён', 'btn', () => setTaskStatus(protocol, index, 'done', keepNote)));
      }
      if (task.status !== 'open') {
        actions.appendChild(actionButton('Не решён', 'btn ghost', () => setTaskStatus(protocol, index, 'open', keepNote)));
      }
      if (task.status === 'done') {
        actions.appendChild(actionButton('Вернуть в работу', 'btn ghost', () => setTaskStatus(protocol, index, 'in_progress', keepNote)));
      }
    }
    node.appendChild(actions);
    box.appendChild(node);
  }
}
function bitrixInfo() {
  const channels = (state.data && state.data.channels) || {};
  return channels.bitrix24 || (state.crm && state.crm.bitrix) || {};
}
function renderBitrixStatus() {
  const box = $('bitrixStatus');
  if (!box) return;
  const info = bitrixInfo();
  const imported = state.bitrix ? ' · в панели ' + state.bitrix.count + ' из Битрикс24' : '';
  box.textContent = info.invalid
    ? 'Битрикс24: адрес вебхука должен быть https://<портал>/rest/<id>/<код>/ — проверьте BITRIX24_WEBHOOK_URL в .env.'
    : info.configured
      ? 'Битрикс24: ' + (info.portal || 'портал') + ' · входящий вебхук настроен' + imported
      : 'Битрикс24 не настроен. Входящий вебхук: CRM → Настройки → Разработчикам → Другое → Входящий вебхук (права crm); адрес — в BITRIX24_WEBHOOK_URL (.env).';
}
function crmCard(contact) {
  const card = el('article', 'crm-card');
  const head = el('div', 'crm-head');
  head.appendChild(el('span', 'crm-ava', (contact.name || contact.email || '?').trim().charAt(0).toUpperCase()));
  const info = el('div');
  info.style.minWidth = '0';
  info.style.flex = '1';
  info.appendChild(el('b', null, contact.name || contact.email));
  info.appendChild(el('div', 'mini', (contact.role || 'контакт') + ' · ' + contact.email));
  if (contact.company || contact.phone) {
    info.appendChild(el('div', 'mini', [contact.company, contact.phone].filter(Boolean).join(' · ')));
  }
  head.appendChild(info);
  card.appendChild(head);
  if (contact.last_subject) card.appendChild(el('div', 'crm-sub', '«' + contact.last_subject + '»'));
  const meta = [];
  if (contact.source === 'bitrix24') meta.push('из Битрикс24');
  else meta.push('писем: ' + contact.letters);
  if (contact.in_bitrix && contact.source !== 'bitrix24') meta.push('в Битрикс24');
  if (contact.last_received) meta.push('последнее: ' + contact.last_received.slice(0, 10));
  if (contact.source !== 'bitrix24') meta.push('Ася: ' + (ACT[contact.last_action] || contact.last_action || '—'));
  card.appendChild(el('div', 'crm-meta', meta.join(' · ')));
  const actions = el('div', 'task-actions');
  actions.appendChild(actionButton('Скопировать адрес', 'btn ghost', async () => {
    try { await navigator.clipboard.writeText(contact.email); toast('Адрес скопирован'); }
    catch (err) { toast('Не удалось скопировать'); }
  }));
  if (!contact.in_bitrix && contact.source !== 'bitrix24' && contact.external) {
    actions.appendChild(actionButton('В Битрикс24', 'btn', () => exportContact(contact)));
  }
  card.appendChild(actions);
  return card;
}
function renderCrm() {
  const box = $('crm');
  if (!box) return;
  renderBitrixStatus();
  box.innerHTML = '';
  const data = state.crm || {};
  if (data.error) { box.appendChild(el('div', 'empty', 'CRM: ' + data.error)); return; }
  if (!data.contacts) { box.appendChild(el('div', 'empty', 'Загружаю…')); return; }
  const imported = (state.bitrix && state.bitrix.contacts) || [];
  const byEmail = {};
  for (const item of imported) {
    if (item.email) byEmail[item.email.toLowerCase()] = item;
  }
  const local = data.contacts.map((contact) => {
    const hit = byEmail[(contact.email || '').toLowerCase()];
    return hit ? Object.assign({}, contact, {in_bitrix: true, bitrix_id: hit.id}) : contact;
  });
  const localEmails = new Set(local.map((contact) => (contact.email || '').toLowerCase()));
  const extra = imported.filter((contact) => contact.email && !localEmails.has(contact.email.toLowerCase()));
  if (!local.length && !extra.length) { box.appendChild(el('div', 'empty', 'Контактов пока нет — нужен прогон по входящим.')); return; }
  box.appendChild(el('p', 'mini', 'Контактов: ' + local.length + ' · внешних ' + data.external +
    ' · внутренних ' + data.internal + ' · писем ' + data.letters +
    (imported.length ? ' · из Битрикс24 ' + imported.length : '')));
  const external = local.filter((c) => c.external);
  const grid = el('div', 'crm-grid');
  for (const contact of external) grid.appendChild(crmCard(contact));
  box.appendChild(grid);
  const internal = local.filter((c) => !c.external);
  if (internal.length) {
    const details = el('details', 'crm-internal');
    details.appendChild(el('summary', null, 'Внутренние контакты (' + internal.length + ')'));
    const inner = el('div', 'crm-grid');
    for (const contact of internal) inner.appendChild(crmCard(contact));
    details.appendChild(inner);
    box.appendChild(details);
  }
  if (extra.length) {
    const details = el('details', 'crm-internal');
    details.appendChild(el('summary', null, 'Из Битрикс24 без писем в прогоне (' + extra.length + ')'));
    const inner = el('div', 'crm-grid');
    for (const contact of extra) inner.appendChild(crmCard(contact));
    details.appendChild(inner);
    box.appendChild(details);
  }
}
function bitrixToastError(err) {
  const text = String((err && err.message) || err || '');
  if (text === 'bitrix24: not configured') { toast('Битрикс24 не настроен: нужен BITRIX24_WEBHOOK_URL в .env.'); return; }
  toast('Битрикс24: ' + text.replace(/^bitrix24:\s*/, ''));
}
async function exportContact(contact) {
  if (!bitrixInfo().configured) { toast('Битрикс24 не настроен: нужен BITRIX24_WEBHOOK_URL в .env.'); return; }
  const note = contact.last_subject
    ? 'AsiStantAI: последнее обращение «' + contact.last_subject + '»' +
      (contact.last_received ? ' · ' + String(contact.last_received).slice(0, 10) : '') +
      (contact.last_action ? ' · ' + (ACT[contact.last_action] || contact.last_action) : '')
    : '';
  try {
    const data = await api('/api/crm/bitrix/contact', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email: contact.email, name: contact.name || '', role: contact.role || '', note: note})
    });
    contact.in_bitrix = true;
    contact.bitrix_id = data.id;
    renderCrm();
    toast((data.created ? 'Контакт создан в Битрикс24' : 'Контакт уже был — добавлен комментарий') +
      (data.portal ? ' · ' + data.portal : ''));
  } catch (err) { bitrixToastError(err); }
}
async function importBitrix() {
  if (!bitrixInfo().configured) { toast('Битрикс24 не настроен: нужен BITRIX24_WEBHOOK_URL в .env.'); return; }
  try {
    toast('Запрашиваю контакты из Битрикс24…');
    const data = await api('/api/crm/bitrix/import', {method: 'POST'});
    state.bitrix = data;
    renderCrm();
    toast('Битрикс24: ' + data.count + ' контактов' + (data.portal ? ' · ' + data.portal : ''));
  } catch (err) { bitrixToastError(err); }
}
async function checkBitrix() {
  try {
    const data = await api('/api/crm/bitrix/check', {method: 'POST'});
    if (state.data && state.data.channels) state.data.channels.bitrix24 = data;
    renderCrm();
    renderChannels();
    toast(data.ok ? 'Битрикс24: связь есть' : 'Битрикс24: ' + (data.detail || 'нет связи'));
  } catch (err) { bitrixToastError(err); }
}
async function loadCrm() {
  try {
    state.crm = await api('/api/crm');
  } catch (err) {
    state.crm = {error: err.message};
  }
  renderCrm();
  renderRail();
}
function renderInbox() {
  const table = $('inboxTable');
  table.innerHTML = '';
  const head = el('tr');
  for (const title of ['Письмо', 'От', 'Тема', 'Ася', 'Разметка', '']) head.appendChild(el('th', null, title));
  table.appendChild(head);
  for (const item of state.data.inbox || []) {
    const tr = el('tr', item.expected && item.action !== item.expected ? 'miss' : '');
    tr.appendChild(el('td', null, item.email_id));
    tr.appendChild(el('td', null, item.from_name));
    tr.appendChild(el('td', null, item.subject));
    tr.appendChild(el('td', null, ACT[item.action] || item.action));
    tr.appendChild(el('td', null, ACT[item.expected] || item.expected || '—'));
    const ok = item.expected ? item.action === item.expected : null;
    tr.appendChild(el('td', ok === null ? '' : (ok ? 'ok' : 'bad'), ok === null ? '—' : (ok ? '✓' : '✗')));
    table.appendChild(tr);
  }
}
const JOURNAL = {
  approve: 'черновик утверждён',
  edit: 'правка учтена',
  reject: 'черновик отклонён',
  run: 'неделя пересобрана',
  task: 'поручение в календаре',
  tasks_bulk: 'поручения в календаре',
  task_state: 'статус задачи изменён',
  task_revise: 'задача скорректирована',
  task_sync: 'правка синхронизирована',
  task_steps: 'шаги поручения',
  step_state: 'статус шага изменён',
  zoom_link: 'ссылка Zoom создана',
  telemost_link: 'ссылка Телемост создана',
  tg_drafts: 'черновики Telegram готовы',
  meeting: 'встреча с Асей проведена',
  meeting_join: 'подключение к встрече',
  meeting_task: 'задача по протоколу',
  meeting_task_state: 'статус задачи по протоколу'
};
function renderJournal() {
  const box = $('journal');
  box.innerHTML = '';
  const events = state.data.events || [];
  if (!events.length) { box.appendChild(el('li', 'mini', 'Пока пусто.')); return; }
  for (const event of events.slice(0, 8)) {
    const item = el('li');
    item.appendChild(el('span', 'when', event.at.slice(11, 16)));
    item.appendChild(el('span', null, JOURNAL[event.kind] || event.kind));
    box.appendChild(item);
  }
}
function render() {
  renderProfiles();
  renderHeader();
  renderKpis();
  renderTabs();
  renderDrafts();
  renderMorning();
  renderDigest();
  renderCalendar();
  renderChannels();
  renderEcon();
  renderProtocols();
  renderTasks();
  renderCrm();
  renderInbox();
  renderJournal();
  renderRail();
}
$('crmRefresh').onclick = loadCrm;
$('bitrixCheck').onclick = checkBitrix;
$('bitrixImport').onclick = importBitrix;
for (const button of document.querySelectorAll('.rail-item')) {
  button.onclick = () => railGo(button.dataset.sec);
}
let railTick = false;
window.addEventListener('scroll', () => {
  if (railTick) return;
  railTick = true;
  requestAnimationFrame(() => { railTick = false; trackRail(); });
}, {passive: true});
$('runBtn').onclick = runWeek;
$('liveBtn').onclick = runLive;
$('checkChannels').onclick = checkChannels;
$('mailUnreadBtn').onclick = () => refreshUnread(false).then((data) => {
  if (data) toast('Непрочитанных ' + (data.period_title || '') + ': ' + data.unread);
});
$('mbPeriod').onchange = () => refreshUnread(false);
$('openUnreadBtn').onclick = openUnread;
$('makeDraftsBtn').onclick = makeDrafts;
$('mbAddBtn').onclick = addMailbox;
$('zoomBtn').onclick = createZoom;
$('zoomLoginBtn').onclick = zoomLogin;
$('zoomLogoutBtn').onclick = zoomLogout;
$('zoomLinkBtn').onclick = () => saveChannelLink('zoom', 'zoomLink');
$('tmLinkBtn').onclick = () => saveChannelLink('telemost', 'tmLink');
$('tmBtn').onclick = createTelemost;
$('tgUnreadBtn').onclick = tgUnread;
$('tgDraftsBtn').onclick = tgDrafts;
$('tgPeriod').onchange = tgUnread;
$('chatPeriod').onchange = loadChat;
$('chatDraftsBtn').onclick = chatDrafts;
$('chatReadBtn').onclick = chatReadAll;
$('chatSendBtn').onclick = chatSend;
$('calendarLogin').onclick = openCalendar;
$('calClose').onclick = closeCalendar;
$('meetClose').onclick = closeCalMeeting;
$('meetBtn').onclick = () => openMeeting($('mtgWho').value || '');
$('zoomMeetBtn').onclick = () => openMeeting($('mtgWho').value || '');
$('mtgWho').onchange = () => {
  const who = $('mtgWho').value;
  openMeeting(who);
  toast(who ? 'Встреча перестроена: ' + who : 'Встреча с руководителем');
};
$('mtgPlay').onclick = mtgToggle;
$('mtgNext').onclick = mtgNextStep;
$('mtgZoom').onclick = openMeetingZoom;
$('mtgClose').onclick = closeMeeting;
$('mtgMinutesBtn').onclick = () => openMinutes(0);
$('mtgZoomJoin').onclick = () => joinChannel('zoom', 'mtgZoomLink');
$('mtgTmJoin').onclick = () => joinChannel('telemost', 'mtgTmLink');
$('zoomJoinBtn').onclick = () => joinChannel('zoom', 'zoomLink');
$('tmJoinBtn').onclick = () => joinChannel('telemost', 'tmLink');
$('minutesBtn').onclick = () => openMinutes(0);
$('minAll').onclick = () => openMinutes(0);
$('minClose').onclick = closeMinutes;
for (const pair of [['mtgZoomLink', 'mtgZoomJoin'], ['mtgTmLink', 'mtgTmJoin'], ['zoomLink', 'zoomJoinBtn'], ['tmLink', 'tmJoinBtn']]) {
  $(pair[0]).addEventListener('keydown', (event) => {
    if (event.key === 'Enter') $(pair[1]).click();
  });
}
$('calGate').addEventListener('click', (event) => {
  if (event.target === $('calGate')) closeCalendar();
});
$('calMeet').addEventListener('click', (event) => {
  if (event.target === $('calMeet')) closeCalMeeting();
});
$('minGate').addEventListener('click', (event) => {
  if (event.target === $('minGate')) closeMinutes();
});
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  if (!$('minGate').hidden) closeMinutes();
  else if (!$('meeting').hidden) closeMeeting();
  else if (!$('calMeet').hidden) closeCalMeeting();
  else if (!$('calGate').hidden) closeCalendar();
});

/* ---------- вход: образ Аси или своё фото ---------- */
const ASYA_FACE = '/assets/asya.jpg?v=2';
const FACE_KEY = 'asistFace:v1';
const FACE_W = 512;
const FACE_H = 640;
const FACE_RAMP = [
  [0.00, 42, 26, 19], [0.16, 82, 50, 36], [0.34, 132, 84, 60],
  [0.50, 180, 120, 90], [0.64, 214, 158, 124], [0.76, 236, 194, 164],
  [0.86, 246, 220, 196], [0.94, 251, 238, 219], [1.00, 255, 249, 240]
];
let faceMode = 'asya';
let faceData = '';
let gateBusy = false;
const entryGate = $('entryGate');
const gateEnter = $('gateEnter');
const ownPanel = $('ownPanel');
const ownDrop = $('ownDrop');
const photoInput = $('photoInput');
const ownSrc = $('ownSrc');
const ownEmpty = $('ownEmpty');
const ownPreview = $('ownPreview');
const ownSteps = $('ownSteps');
const ownResult = $('ownResult');
const gateFace = $('gateFace');
const gateFaceBody = $('gateFaceBody');
const gateChannels = $('gateChannels');
const ONBOARD_KEY = 'asistant.onboard.v1';
const sleep = (ms) => new Promise((res) => setTimeout(res, ms));

function setFaceMode(mode) {
  if (gateBusy) return;
  faceMode = mode;
  document.querySelectorAll('.gate-opt').forEach((b) => {
    const on = b.dataset.face === mode;
    b.classList.toggle('on', on);
    b.setAttribute('aria-checked', on ? 'true' : 'false');
  });
  ownPanel.hidden = mode !== 'own';
  updateGateEnter();
}

function updateGateEnter() {
  gateEnter.disabled = gateBusy || (faceMode === 'own' && !faceData);
  gateEnter.textContent = gateBusy
    ? 'Собираю аватар…'
    : faceMode === 'own'
      ? (faceData ? 'Войти с моим аватаром' : 'Сначала добавьте фото')
      : 'Войти с Асей';
}

function applyFace() {
  const own = faceMode === 'own' && !!faceData;
  const src = own ? faceData : ASYA_FACE;
  ['asyaFace', 'gateBrandFace'].forEach((id) => {
    const node = $(id);
    if (node) node.src = src;
  });
  // ведущая планёрки всегда Ася: свой образ её не заменяет
  const host = $('mtgPhoto');
  if (host) host.src = ASYA_FACE;
}

function rememberFace() {
  try {
    localStorage.setItem(FACE_KEY, JSON.stringify(faceMode === 'own' ? { mode: 'own', data: faceData } : { mode: 'asya' }));
  } catch (e) {}
}

function savedFace() {
  try {
    return JSON.parse(localStorage.getItem(FACE_KEY) || 'null');
  } catch (e) {
    return null;
  }
}

function openGate() {
  entryGate.classList.add('on');
  document.body.classList.add('gate-open');
  gateChannels.hidden = true;
  gateFaceBody.hidden = false;
  if (faceData) {
    gateFace.src = faceData;
    ownResult.hidden = false;
    ownDrop.hidden = true;
    ownSteps.hidden = true;
  }
  setFaceMode(faceMode);
  setTimeout(() => {
    const opt = document.querySelector('.gate-opt.on');
    if (opt) opt.focus();
  }, 80);
}

function closeGate() {
  entryGate.classList.remove('on');
  document.body.classList.remove('gate-open');
}

function readImage(src) {
  return new Promise((res, rej) => {
    const img = new Image();
    img.onload = () => res(img);
    img.onerror = rej;
    img.src = src;
  });
}

function readFileData(file) {
  return new Promise((res, rej) => {
    const fr = new FileReader();
    fr.onload = () => res(fr.result);
    fr.onerror = rej;
    fr.readAsDataURL(file);
  });
}

function boxBlur(src, w, h, r) {
  const tmp = new Float32Array(w * h);
  const out = new Float32Array(w * h);
  for (let y = 0; y < h; y++) {
    let sum = 0;
    for (let x = -r; x <= r; x++) sum += src[y * w + Math.min(w - 1, Math.max(0, x))];
    const cnt = r * 2 + 1;
    for (let x = 0; x < w; x++) {
      tmp[y * w + x] = sum / cnt;
      sum += src[y * w + Math.min(w - 1, x + r + 1)] - src[y * w + Math.max(0, x - r)];
    }
  }
  for (let x = 0; x < w; x++) {
    let sum = 0;
    for (let y = -r; y <= r; y++) sum += tmp[Math.min(h - 1, Math.max(0, y)) * w + x];
    const cnt = r * 2 + 1;
    for (let y = 0; y < h; y++) {
      out[y * w + x] = sum / cnt;
      sum += tmp[Math.min(h - 1, y + r + 1) * w + x] - tmp[Math.max(0, y - r) * w + x];
    }
  }
  return out;
}

function toneAt(v) {
  for (let i = 1; i < FACE_RAMP.length; i++) {
    if (v <= FACE_RAMP[i][0]) {
      const a = FACE_RAMP[i - 1];
      const b = FACE_RAMP[i];
      const t = (v - a[0]) / (b[0] - a[0]);
      return [a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t, a[3] + (b[3] - a[3]) * t];
    }
  }
  return FACE_RAMP[FACE_RAMP.length - 1].slice(1);
}

function stylizeFace(img) {
  const c = document.createElement('canvas');
  c.width = FACE_W;
  c.height = FACE_H;
  const ctx = c.getContext('2d');
  ctx.fillStyle = '#FBF4E7';
  ctx.fillRect(0, 0, FACE_W, FACE_H);
  const s = Math.max(FACE_W / img.width, FACE_H / img.height);
  const dw = img.width * s;
  const dh = img.height * s;
  ctx.drawImage(img, (FACE_W - dw) / 2, FACE_H * 0.5 - dh * 0.44, dw, dh);

  const px = ctx.getImageData(0, 0, FACE_W, FACE_H);
  const d = px.data;
  const n = FACE_W * FACE_H;
  const lum = new Float32Array(n);
  for (let i = 0, p = 0; p < n; i += 4, p++) {
    lum[p] = (d[i] * 0.299 + d[i + 1] * 0.587 + d[i + 2] * 0.114) / 255;
  }
  const soft = boxBlur(lum, FACE_W, FACE_H, 3);
  const edge = new Float32Array(n);
  for (let y = 0; y < FACE_H; y++) {
    for (let x = 0; x < FACE_W; x++) {
      const p = y * FACE_W + x;
      const gx = soft[y * FACE_W + Math.min(FACE_W - 1, x + 1)] - soft[y * FACE_W + Math.max(0, x - 1)];
      const gy = soft[Math.min(FACE_H - 1, y + 1) * FACE_W + x] - soft[Math.max(0, y - 1) * FACE_W + x];
      edge[p] = Math.min(1, Math.sqrt(gx * gx + gy * gy) * 2.4);
    }
  }
  for (let y = 0, p = 0; y < FACE_H; y++) {
    for (let x = 0; x < FACE_W; x++, p++) {
      const v = Math.min(1, Math.max(0, (soft[p] - 0.5) * 1.1 + 0.56));
      const tone = toneAt(v);
      const e = edge[p] * 0.42;
      const dx = (x / FACE_W - 0.5) * 1.18;
      const dy = (y / FACE_H - 0.46) * 1.05;
      const vig = Math.min(1, Math.max(0.58, 1 - (dx * dx + dy * dy) * 0.34));
      const grain = (Math.random() - 0.5) * 7;
      const i = p * 4;
      d[i] = Math.max(0, Math.min(255, (tone[0] * (1 - e) + 46 * e) * vig + grain));
      d[i + 1] = Math.max(0, Math.min(255, (tone[1] * (1 - e) + 27 * e) * vig + grain));
      d[i + 2] = Math.max(0, Math.min(255, (tone[2] * (1 - e) + 20 * e) * vig + grain));
    }
  }
  ctx.putImageData(px, 0, 0);
  return c.toDataURL('image/jpeg', 0.92);
}

async function takePhoto(file) {
  if (!file || gateBusy) return;
  if (!/^image\//.test(file.type)) {
    toast('Нужен файл изображения: JPG или PNG с лицом в кадре');
    return;
  }
  if (file.size > 8 * 1024 * 1024) {
    toast('Файл слишком большой: до 8 МБ');
    return;
  }
  let img;
  let src;
  try {
    src = await readFileData(file);
    img = await readImage(src);
  } catch (err) {
    toast('Не удалось прочитать фото: попробуйте JPG или PNG до 8 МБ');
    return;
  }
  ownSrc.src = src;
  $('ownName').textContent = file.name || 'Ваше фото';
  ownDrop.hidden = false;
  ownEmpty.hidden = true;
  ownPreview.hidden = false;
  ownSteps.hidden = false;
  ownResult.hidden = true;
  faceData = '';
  gateBusy = true;
  updateGateEnter();
  const steps = Array.from(ownSteps.querySelectorAll('.own-step'));
  steps.forEach((step) => { step.className = 'own-step'; });
  for (const step of steps) {
    step.classList.add('on');
    await sleep(420);
    step.classList.remove('on');
    step.classList.add('done');
  }
  faceData = stylizeFace(img);
  gateFace.src = faceData;
  ownResult.hidden = false;
  gateBusy = false;
  updateGateEnter();
  toast('Аватар готов: фото обработано на устройстве в стиле Аси');
}

document.querySelectorAll('.gate-opt').forEach((b) => b.addEventListener('click', () => setFaceMode(b.dataset.face)));
ownDrop.addEventListener('click', () => photoInput.click());
ownDrop.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    photoInput.click();
  }
});
photoInput.addEventListener('change', () => {
  const file = photoInput.files && photoInput.files[0];
  if (file) takePhoto(file);
  photoInput.value = '';
});
['dragenter', 'dragover'].forEach((ev) => ownDrop.addEventListener(ev, (event) => {
  event.preventDefault();
  ownDrop.classList.add('over');
}));
['dragleave', 'drop'].forEach((ev) => ownDrop.addEventListener(ev, (event) => {
  event.preventDefault();
  ownDrop.classList.remove('over');
}));
ownDrop.addEventListener('drop', (event) => {
  const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
  if (file) takePhoto(file);
});
$('ownAgain').onclick = () => photoInput.click();
function onboardSeen() {
  try { return localStorage.getItem(ONBOARD_KEY) === '1'; } catch (e) { return false; }
}
function finishOnboard() {
  rememberFace();
  applyFace();
  try { localStorage.setItem(ONBOARD_KEY, '1'); } catch (e) {}
  closeGate();
}
function fillOnboardProviders(providers) {
  const select = $('onbProvider');
  if (select.options.length) return;
  const list = Object.entries(providers || {});
  const pairs = list.length ? list : [['gmail', 'Gmail'], ['mailru', 'Mail.ru'], ['yandex', 'Яндекс'], ['outlook', 'Outlook'], ['custom', 'Другой']];
  for (const [key, p] of pairs) {
    const option = el('option', null, (p && p.title) || p);
    option.value = key;
    select.appendChild(option);
  }
  select.value = providers && providers.gmail ? 'gmail' : (select.options[0] ? select.options[0].value : 'custom');
}
async function refreshOnboard() {
  try {
    const mb = await api('/api/mailboxes');
    fillOnboardProviders(mb.providers);
    const items = mb.mailboxes || [];
    const on = items.filter((m) => m.configured).length;
    $('onbMailState').textContent = on ? 'подключено: ' + on : 'не подключено';
  } catch (err) {
    $('onbMailState').textContent = 'нет связи';
  }
  try {
    const ch = await api('/api/channels');
    const bot = ch.telegram || {};
    const mine = ch.telegram_user || {};
    $('onbTgState').textContent = bot.configured && mine.authorized ? 'готово' : 'не подключено';
    $('onbTgText').textContent =
      'Бот: ' + (bot.configured ? 'настроен' : 'нет') + ' · мой Telegram: ' + (mine.authorized ? 'вход выполнен' : 'входа нет') +
      '. Бот — TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env. Мой аккаунт — один раз python -m app.cli tg-login в терминале: телефон и код вводятся там.';
  } catch (err) {
    $('onbTgState').textContent = 'нет связи';
  }
  try {
    const crm = await api('/api/crm');
    const bx = (crm && crm.bitrix) || {};
    if (bx.invalid) {
      $('onbCrmState').textContent = 'ошибка адреса';
      $('onbCrmText').textContent = 'BITRIX24_WEBHOOK_URL должен иметь вид https://<портал>/rest/<id>/<код>/. Вебхук в интерфейсе не показывается.';
    } else if (bx.configured) {
      $('onbCrmState').textContent = bx.portal || 'вебхук';
      $('onbCrmText').textContent = 'Портал ' + (bx.portal || '') + ': контакты ищутся и создаются через входящий вебхук. Проверка — кнопкой ниже.';
    } else {
      $('onbCrmState').textContent = 'не подключено';
      $('onbCrmText').textContent = 'Впишите BITRIX24_WEBHOOK_URL в .env (образец — .env.example) и перезапустите сервер. В интерфейсе и журнале вебхук не сохраняется.';
    }
  } catch (err) {
    $('onbCrmState').textContent = 'нет связи';
  }
}
async function showChannelsStep() {
  gateFaceBody.hidden = true;
  gateChannels.hidden = false;
  $('onbMailNote').textContent = '';
  $('onbMailState').textContent = '…';
  $('onbTgState').textContent = '…';
  $('onbCrmState').textContent = '…';
  await refreshOnboard();
}
$('onbDone').onclick = finishOnboard;
$('onbSkip').onclick = finishOnboard;
$('onbMailAdd').onclick = async () => {
  const address = ($('onbAddress').value || '').trim();
  const password = $('onbPassword').value;
  const provider = $('onbProvider').value || 'custom';
  if (address.indexOf('@') < 1) { toast('Укажите адрес почты'); return; }
  if (!password) { toast('Нужен пароль приложения почты'); return; }
  const btn = $('onbMailAdd');
  btn.disabled = true;
  btn.textContent = 'Проверяю вход…';
  try {
    const res = await api('/api/mailboxes', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({address, password, provider, label: 'Мой ящик'})
    });
    const check = (res && res.check) || {};
    $('onbPassword').value = '';
    $('onbAddress').value = '';
    $('onbMailNote').textContent = check.ok
      ? 'Вход выполнен · непрочитанных: ' + check.unread + ' · за сегодня: ' + check.today
      : 'Ящик добавлен, но вход не удался: ' + (check.detail || 'проверьте пароль приложения');
    const items = (res && res.mailboxes) || [];
    const on = items.filter((m) => m.configured).length;
    $('onbMailState').textContent = on ? 'подключено: ' + on : 'не подключено';
    await loadMailboxes();
    await refreshUnread(true);
  } catch (err) {
    $('onbMailNote').textContent = 'Не удалось добавить: ' + (err.message || 'проверьте данные');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Добавить ящик';
  }
};
$('onbTgCheck').onclick = async () => {
  const btn = $('onbTgCheck');
  btn.disabled = true;
  btn.textContent = 'Проверяю…';
  try {
    const data = await api('/api/channels/check', {method: 'POST'});
    if (state.data) state.data.channels = data;
    await refreshOnboard();
  } catch (err) {
    $('onbTgState').textContent = 'нет связи';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Проверить Telegram';
  }
};
$('onbCrmCheck').onclick = async () => {
  const btn = $('onbCrmCheck');
  btn.disabled = true;
  btn.textContent = 'Проверяю…';
  try {
    const data = await api('/api/crm/bitrix/check', {method: 'POST'});
    if (state.data && state.data.channels) state.data.channels.bitrix24 = data;
    $('onbCrmState').textContent = data.ok ? (data.portal || 'связь есть') : 'не подключено';
    $('onbCrmText').textContent = data.ok
      ? (data.detail || 'Связь с Битрикс24 есть')
      : 'Проверка: ' + (data.detail || 'не настроено') + '. Вебхук — в BITRIX24_WEBHOOK_URL (.env).';
  } catch (err) {
    $('onbCrmText').textContent = 'Проверка не удалась: ' + (err.message || '');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Проверить Битрикс24';
  }
};
gateEnter.onclick = () => {
  if (gateBusy) return;
  if (faceMode === 'own' && !faceData) {
    toast('Сначала добавьте фото');
    return;
  }
  if (!onboardSeen()) {
    showChannelsStep();
    return;
  }
  rememberFace();
  applyFace();
  closeGate();
};
$('changeFace').onclick = openGate;
(function initFace() {
  const saved = savedFace();
  if (saved && saved.mode === 'own' && saved.data) {
    faceMode = 'own';
    faceData = saved.data;
    applyFace();
  } else if (saved && saved.mode === 'asya') {
    applyFace();
  } else {
    setFaceMode('asya');
    openGate();
  }
})();

$('q').oninput = (event) => {
  state.q = event.target.value;
  renderDrafts();
};
load().catch((err) => {
  $('drafts').innerHTML = '';
  $('drafts').appendChild(el('div', 'empty', 'Не удалось загрузить: ' + err.message));
});
</script>
</body>
</html>
"""
