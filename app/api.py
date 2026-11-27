# -*- coding: utf-8 -*-
"""HTTP-слой AsiStantAI: запуск, очередь решений, черновики, протоколы, сводки."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from . import __version__
from .config import Settings
from .journal import Journal
from .live import ChannelError, Channels, approve_draft, channels_status, run_live
from .pipeline import run as run_pipeline
from .zoom import ZoomError

settings = Settings.load()
app = FastAPI(title="AsiStantAI", version=__version__)


class RunRequest(BaseModel):
    profile: str | None = None
    source: str | None = None  # dataset | live


class EditRequest(BaseModel):
    body: str = Field(min_length=1, max_length=6000)
    comment: str = Field(default="", max_length=500)


class ZoomRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=200)
    start: str = Field(default="", max_length=32)
    minutes: int = Field(default=45, ge=15, le=240)
    agenda: str = Field(default="", max_length=500)


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


def _journal() -> Journal:
    return Journal(settings.db_path)


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
    return {"tasks": tasks, "approved": len(tasks), "total": total}


@app.get("/api/channels")
def channels() -> dict:
    return channels_status(settings)


@app.post("/api/channels/check")
def channels_check() -> dict:
    return channels_status(settings, check=True)


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


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return HTML_PAGE


HTML_PAGE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AsiStantAI · Ася</title>
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
  .mark{
    width:46px;height:46px;border-radius:50%;display:grid;place-items:center;
    background:radial-gradient(circle at 30% 25%, #D9764B, var(--terra) 60%, var(--terra-dark));
    color:#FFF6EA;font:600 22px/1 Georgia,serif;box-shadow:0 8px 18px rgba(142,63,32,.28);
  }
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
  main{max-width:1280px;margin:0 auto;padding:28px 32px 56px}
  .eyebrow{color:var(--gold);font-size:12px;letter-spacing:.14em;text-transform:uppercase;margin:0 0 6px}
  .hero h2{font:600 30px/1.2 Georgia,serif;margin:0 0 2px}
  .hero .sub{color:var(--muted);margin:0 0 22px}
  .kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:26px}
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
  details summary{cursor:pointer;color:var(--muted);font-size:13px}
  footer{color:var(--muted);font-size:12.5px;text-align:center;padding:0 32px 40px}
  .empty{padding:28px;text-align:center;color:var(--muted)}
  .toast{
    position:fixed;right:24px;bottom:24px;background:var(--chest);color:#FBF3E6;
    padding:11px 16px;border-radius:12px;font-size:13.5px;box-shadow:0 12px 26px rgba(62,42,32,.28);
    opacity:0;transform:translateY(8px);transition:.25s;pointer-events:none;max-width:340px
  }
  .toast.on{opacity:1;transform:none}
  @media (max-width:1080px){
    .kpis{grid-template-columns:repeat(3,1fr)}
    .grid{grid-template-columns:1fr}
    .cards{max-height:none}
  }
  @media (max-width:640px){
    header{padding:14px 18px}
    main{padding:20px 18px 40px}
    .kpis{grid-template-columns:repeat(2,1fr)}
    .brand p{display:none}
  }
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="mark">А</div>
    <div>
      <h1>AsiStantAI</h1>
      <p>Ася · ИИ-помощник руководителя</p>
    </div>
  </div>
  <div class="controls">
    <select id="profile" title="Профиль руководителя"></select>
    <button class="btn ghost" id="liveBtn" title="Снять новые письма из IMAP">Живой прогон</button>
    <button class="btn primary" id="runBtn">Пересобрать неделю</button>
  </div>
</header>
<main>
  <section class="hero">
    <p class="eyebrow">Демонстрация на данных ИИкузницы · 26.11.2026</p>
    <h2 id="profileLine">Руководитель</h2>
    <p class="sub" id="runLine">Готовлю разбор почты, календаря и протоколов.</p>
    <div class="kpis" id="kpis"></div>
  </section>

  <section class="grid">
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
        <h3>Календарь</h3>
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
        <h3>Zoom</h3>
        <input class="search" id="zoomTopic" placeholder="Тема встречи" value="Планёрка с Асей">
        <input class="search" id="zoomStart" placeholder="Когда: 2026-11-28 10:00 (необязательно)" style="margin-top:7px">
        <button class="btn" id="zoomBtn" style="width:100%;margin-top:8px">Создать ссылку</button>
        <div id="zoomResult" class="mini" style="margin-top:8px"></div>
      </div>
      <div class="panel">
        <h3>Экономика</h3>
        <div id="econ">…</div>
      </div>
      <div class="panel">
        <h3>Журнал <a href="/api/journal.csv" download>CSV</a></h3>
        <ul class="list" id="journal"></ul>
      </div>
    </aside>
  </section>

  <section class="panel">
    <h3>Протоколы встреч</h3>
    <p class="mini" style="margin:-6px 0 14px">Решения, поручения с владельцами и открытые вопросы. Статус «решён / не решён», правка задачи с синхронизацией руководителю и разложение на шаги для аналитика, BDM и CJM — последовательно или параллельно.</p>
    <div id="protocols"></div>
  </section>

  <section class="panel">
    <details>
      <summary>Вся почта и сверка с разметкой организаторов</summary>
      <div style="margin-top:12px;max-height:520px;overflow:auto">
        <table id="inboxTable"></table>
      </div>
    </details>
  </section>
</main>
<footer>0 автоотправок · отправка только после утверждения · в демо-режиме письма не уходят</footer>
<div class="toast" id="toast"></div>

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
const state = { data: null, tab: 'all', editing: null, q: '', revising: null, stepPanel: new Set(), allProtocols: new Set() };
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
      ((data.zoom && data.zoom.configured && data.zoom.ok === false) ? 1 : 0);
    toast(bad ? 'Есть ошибки каналов — смотрите подписи' : 'Каналы проверены');
  } catch (err) {
    toast('Проверка не удалась: ' + err.message);
  } finally {
    $('checkChannels').disabled = false;
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
    toast(result.mode === 'api' ? 'Встреча Zoom создана' : 'Постоянная ссылка Zoom');
  } catch (err) {
    const detail = String(err.message || '');
    toast(detail.indexOf('not configured') >= 0 ? 'Zoom не настроен: ZOOM_* или ZOOM_PERSONAL_LINK в .env' : 'Zoom: ' + detail);
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
async function load() {
  let data = await api('/api/summary');
  if (data.empty) {
    await api('/api/run', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    data = await api('/api/summary');
  }
  state.data = data;
  render();
}
function renderProfiles() {
  const select = $('profile');
  const current = select.value;
  select.innerHTML = '';
  for (const p of state.data.profiles || []) {
    const option = el('option', null, p.default ? 'Профиль: ' + p.name + ' (по умолчанию)' : 'Профиль: ' + p.name);
    option.value = p.default ? '' : p.name;
    select.appendChild(option);
  }
  select.value = current;
}
function renderKpis() {
  const m = state.data.metrics || {};
  const box = $('kpis');
  box.innerHTML = '';
  const items = [
    [m.emails || 0, 'писем за неделю', ''],
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
  $('profileLine').textContent = (m.profile_name || 'Руководитель') + (m.profile_role ? ' · ' + m.profile_role : '');
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
    box.appendChild(channelRow('Ящик ' + (label === 'a' ? 'А' : 'Б'), acc.address || '', acc));
  }
  box.appendChild(channelRow('Telegram', (data.telegram && data.telegram.configured) ? 'бот' : '', data.telegram || {}));
  const zoom = data.zoom || {};
  const zoomMode = zoom.mode === 'api' ? 'API' : (zoom.mode === 'link' ? 'постоянная ссылка' : '');
  box.appendChild(channelRow('Zoom', zoomMode, zoom));
  box.appendChild(el('div', 'mini', data.live_ready
    ? 'Живой режим готов: письма из IMAP, отправка после утверждения.'
    : 'Демо на датасете. Для живого режима заполните .env: MAIL_A_*, MAIL_B_*, TELEGRAM_*.'));
  if (!zoom.configured) {
    box.appendChild(el('div', 'mini', 'Zoom: ZOOM_PERSONAL_LINK или ZOOM_ACCOUNT_ID + ZOOM_CLIENT_ID + ZOOM_CLIENT_SECRET.'));
  }
}
function renderEcon() {
  const box = $('econ');
  const e = state.data.economics || {};
  box.innerHTML = '';
  if (!e.drafts_per_month) { box.textContent = 'Нет данных.'; return; }
  const rows = [
    ['Черновиков в месяц', e.drafts_per_month],
    ['Стоимость черновика', e.cost_per_draft + ' ₽'],
    ['Стоимость месяца', num(e.agent_cost) + ' ₽'],
    ['Ручная обработка', num(e.manual_cost) + ' ₽']
  ];
  for (const [label, value] of rows) {
    const line = el('div', 'econ-line');
    line.appendChild(el('span', null, label));
    line.appendChild(el('b', null, value));
    box.appendChild(line);
  }
  const total = el('div', 'econ-line econ-total');
  total.appendChild(el('span', null, 'Экономия в месяц'));
  total.appendChild(el('b', null, num(e.saving) + ' ₽'));
  box.appendChild(total);
  box.appendChild(el('div', 'mini', 'Окупаемость внедрения: ' + e.payback_months + ' мес · 0 автоотправок'));
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
async function setTaskStatus(protocol, index, status) {
  try {
    await api('/api/protocols/' + encodeURIComponent(protocol.id) + '/tasks/' + index + '/state', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status: status, note: noteFor(protocol, index)})
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
    left.appendChild(el('div', 'mini', meta));
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
  zoom_link: 'ссылка Zoom создана'
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
  renderInbox();
  renderJournal();
}
$('runBtn').onclick = runWeek;
$('liveBtn').onclick = runLive;
$('checkChannels').onclick = checkChannels;
$('zoomBtn').onclick = createZoom;
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
