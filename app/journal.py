# -*- coding: utf-8 -*-
"""Журнал: SQLite для разборов, черновиков, правок и подтверждений."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  profile_id TEXT,
  dataset TEXT,
  metrics_json TEXT
);
CREATE TABLE IF NOT EXISTS classifications (
  run_id INTEGER NOT NULL,
  email_id TEXT NOT NULL,
  action TEXT NOT NULL,
  topic TEXT,
  confidence REAL,
  payload_json TEXT,
  PRIMARY KEY (run_id, email_id)
);
CREATE TABLE IF NOT EXISTS drafts (
  run_id INTEGER NOT NULL,
  draft_id TEXT NOT NULL,
  email_id TEXT NOT NULL,
  kind TEXT,
  topic TEXT,
  recipient TEXT,
  to_email TEXT,
  subject TEXT,
  body TEXT,
  sources_json TEXT,
  status TEXT DEFAULT 'waiting_approval',
  edited INTEGER DEFAULT 0,
  sent INTEGER DEFAULT 0,
  created_at TEXT,
  PRIMARY KEY (run_id, draft_id)
);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER,
  draft_id TEXT NOT NULL,
  decision TEXT NOT NULL,
  comment TEXT,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER,
  draft_id TEXT NOT NULL,
  topic TEXT,
  before_body TEXT,
  after_body TEXT,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS protocols (
  run_id INTEGER NOT NULL,
  protocol_id TEXT NOT NULL,
  payload_json TEXT,
  PRIMARY KEY (run_id, protocol_id)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT
);
CREATE TABLE IF NOT EXISTS protocol_tasks (
  run_id INTEGER NOT NULL,
  protocol_id TEXT NOT NULL,
  task_index INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'waiting',
  at TEXT,
  PRIMARY KEY (run_id, protocol_id, task_index)
);
CREATE TABLE IF NOT EXISTS protocol_task_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  protocol_id TEXT NOT NULL,
  task_index INTEGER NOT NULL,
  before_text TEXT,
  after_text TEXT,
  before_owner TEXT,
  after_owner TEXT,
  before_deadline TEXT,
  after_deadline TEXT,
  reason TEXT,
  at TEXT NOT NULL,
  synced_at TEXT,
  sync_note TEXT,
  sync_channel TEXT
);
CREATE TABLE IF NOT EXISTS protocol_steps (
  run_id INTEGER NOT NULL,
  protocol_id TEXT NOT NULL,
  task_index INTEGER NOT NULL,
  step_index INTEGER NOT NULL,
  title TEXT NOT NULL,
  owner TEXT,
  role TEXT,
  deadline TEXT,
  depends_on TEXT,
  parallel INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'waiting',
  note TEXT,
  at TEXT,
  updated_at TEXT,
  PRIMARY KEY (run_id, protocol_id, task_index, step_index)
);
CREATE TABLE IF NOT EXISTS seen_messages (
  channel TEXT NOT NULL,
  uid TEXT NOT NULL,
  at TEXT NOT NULL,
  PRIMARY KEY (channel, uid)
);
CREATE TABLE IF NOT EXISTS meeting_minutes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  seconds INTEGER NOT NULL DEFAULT 0,
  profile TEXT,
  role TEXT,
  employee TEXT,
  employee_position TEXT,
  week TEXT,
  subtitle TEXT,
  channel TEXT,
  items_json TEXT
);
CREATE TABLE IF NOT EXISTS meeting_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  minute_id INTEGER NOT NULL,
  item_index INTEGER NOT NULL DEFAULT 0,
  title TEXT NOT NULL,
  owner TEXT,
  due TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL
);
"""

TASK_STATUSES = ("waiting", "approved", "in_progress", "done", "open")
STEP_STATUSES = ("waiting", "in_progress", "done")
MEETING_TASK_STATUSES = ("open", "done")


class Journal:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(drafts)")}
        if "topic" not in columns:
            self.conn.execute("ALTER TABLE drafts ADD COLUMN topic TEXT")
        if "to_email" not in columns:
            self.conn.execute("ALTER TABLE drafts ADD COLUMN to_email TEXT")
        task_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(protocol_tasks)")}
        for name, ddl in (
            ("note", "TEXT DEFAULT ''"),
            ("text_override", "TEXT"),
            ("owner_override", "TEXT"),
            ("deadline_override", "TEXT"),
            ("revised", "INTEGER NOT NULL DEFAULT 0"),
            ("revision_reason", "TEXT"),
            ("revised_at", "TEXT"),
            ("synced_at", "TEXT"),
            ("sync_note", "TEXT"),
            ("sync_channel", "TEXT"),
            ("updated_at", "TEXT"),
        ):
            if name not in task_columns:
                self.conn.execute(f"ALTER TABLE protocol_tasks ADD COLUMN {name} {ddl}")
        minute_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(meeting_minutes)")}
        for name in ("employee", "employee_position"):
            if name not in minute_columns:
                self.conn.execute(f"ALTER TABLE meeting_minutes ADD COLUMN {name} TEXT")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def log(self, kind: str, payload: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO events(at, kind, payload_json) VALUES(?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), kind, json.dumps(payload, ensure_ascii=False)),
        )
        self.conn.commit()

    def start_run(self, profile_id: str, dataset: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(started_at, profile_id, dataset) VALUES(?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), profile_id, dataset),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, metrics: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, metrics_json=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), json.dumps(metrics, ensure_ascii=False), run_id),
        )
        self.conn.commit()

    def save_classification(self, run_id: int, payload: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO classifications(run_id, email_id, action, topic, confidence, payload_json) "
            "VALUES(?,?,?,?,?,?)",
            (
                run_id,
                payload["email_id"],
                payload["action"],
                payload.get("topic"),
                payload.get("confidence"),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def save_draft(self, run_id: int, draft: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO drafts(run_id, draft_id, email_id, kind, topic, recipient, to_email, subject, body, "
            "sources_json, status, edited, sent, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                draft["id"],
                draft["email_id"],
                draft.get("kind"),
                draft.get("topic"),
                draft.get("recipient"),
                draft.get("to_email"),
                draft.get("subject"),
                draft.get("body"),
                json.dumps(draft.get("sources", []), ensure_ascii=False),
                draft.get("status", "waiting_approval"),
                int(bool(draft.get("edited"))),
                int(bool(draft.get("sent"))),
                draft.get("created_at"),
            ),
        )
        self.conn.commit()

    def save_protocol(self, run_id: int, payload: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO protocols(run_id, protocol_id, payload_json) VALUES(?,?,?)",
            (run_id, payload["id"], json.dumps(payload, ensure_ascii=False)),
        )
        self.conn.commit()

    def last_run(self) -> int | None:
        row = self.conn.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return int(row["id"]) if row else None

    def run_metrics(self, run_id: int | None = None) -> dict[str, Any] | None:
        run_id = run_id or self.last_run()
        if not run_id:
            return None
        row = self.conn.execute("SELECT metrics_json FROM runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row["metrics_json"]) if row and row["metrics_json"] else None

    def classifications(self, run_id: int | None = None) -> list[dict[str, Any]]:
        run_id = run_id or self.last_run()
        if not run_id:
            return []
        rows = self.conn.execute(
            "SELECT payload_json FROM classifications WHERE run_id=? ORDER BY email_id", (run_id,)
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def protocols(self, run_id: int | None = None) -> list[dict[str, Any]]:
        run_id = run_id or self.last_run()
        if not run_id:
            return []
        rows = self.conn.execute(
            "SELECT payload_json FROM protocols WHERE run_id=? ORDER BY protocol_id", (run_id,)
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, started_at, finished_at, profile_id, dataset FROM runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT at, kind, payload_json FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            {"at": row["at"], "kind": row["kind"], "payload": json.loads(row["payload_json"] or "{}")}
            for row in rows
        ]

    def save_minutes(self, payload: dict[str, Any]) -> int:
        """Протокол встречи, которую Ася провела голосом."""
        cur = self.conn.execute(
            "INSERT INTO meeting_minutes(started_at, finished_at, seconds, profile, role, employee, employee_position, week, subtitle, channel, items_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                payload["started_at"],
                payload.get("finished_at", ""),
                int(payload.get("seconds", 0)),
                payload.get("profile", ""),
                payload.get("role", ""),
                payload.get("employee", ""),
                payload.get("employee_position", ""),
                payload.get("week", ""),
                payload.get("subtitle", ""),
                payload.get("channel", ""),
                json.dumps(payload.get("items", []), ensure_ascii=False),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    @staticmethod
    def _minute_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"] or "",
            "seconds": int(row["seconds"] or 0),
            "profile": row["profile"] or "",
            "role": row["role"] or "",
            "employee": row["employee"] or "",
            "employee_position": row["employee_position"] or "",
            "week": row["week"] or "",
            "subtitle": row["subtitle"] or "",
            "channel": row["channel"] or "",
            "items": json.loads(row["items_json"] or "[]"),
        }

    def minutes(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM meeting_minutes ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
        counts = {
            int(row["minute_id"]): (int(row["total"]), int(row["done"]))
            for row in self.conn.execute(
                "SELECT minute_id, COUNT(*) AS total, SUM(status='done') AS done "
                "FROM meeting_tasks GROUP BY minute_id"
            )
        }
        out = []
        for row in rows:
            item = self._minute_view(row)
            total, done = counts.get(item["id"], (0, 0))
            item["tasks_total"] = total
            item["tasks_done"] = done
            out.append(item)
        return out

    def minute(self, minute_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM meeting_minutes WHERE id=?", (int(minute_id),)).fetchone()
        if not row:
            return None
        item = self._minute_view(row)
        item["tasks"] = self.meeting_tasks(item["id"])
        return item

    @staticmethod
    def _meeting_task_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "minute_id": int(row["minute_id"]),
            "item_index": int(row["item_index"] or 0),
            "title": row["title"],
            "owner": row["owner"] or "",
            "due": row["due"] or "",
            "status": row["status"] or "open",
            "created_at": row["created_at"],
        }

    def meeting_tasks(self, minute_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM meeting_tasks WHERE minute_id=? ORDER BY id", (int(minute_id),)
        ).fetchall()
        return [self._meeting_task_view(row) for row in rows]

    def add_meeting_task(
        self, minute_id: int, item_index: int, title: str, owner: str = "", due: str = ""
    ) -> dict[str, Any] | None:
        if self.minute(minute_id) is None:
            return None
        cur = self.conn.execute(
            "INSERT INTO meeting_tasks(minute_id, item_index, title, owner, due, status, created_at) "
            "VALUES(?,?,?,?,?,'open',?)",
            (int(minute_id), int(item_index), title, owner, due, self._now()),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM meeting_tasks WHERE id=?", (cur.lastrowid,)).fetchone()
        return self._meeting_task_view(row)

    def set_meeting_task_state(
        self, minute_id: int, task_id: int, status: str
    ) -> dict[str, Any] | None:
        if status not in MEETING_TASK_STATUSES:
            return None
        row = self.conn.execute(
            "SELECT * FROM meeting_tasks WHERE id=? AND minute_id=?", (int(task_id), int(minute_id))
        ).fetchone()
        if not row:
            return None
        self.conn.execute(
            "UPDATE meeting_tasks SET status=? WHERE id=?", (status, int(task_id))
        )
        self.conn.commit()
        fresh = self.conn.execute("SELECT * FROM meeting_tasks WHERE id=?", (int(task_id),)).fetchone()
        return self._meeting_task_view(fresh)

    def drafts(self, run_id: int | None = None) -> list[dict[str, Any]]:
        run_id = run_id or self.last_run()
        if not run_id:
            return []
        rows = self.conn.execute("SELECT * FROM drafts WHERE run_id=? ORDER BY draft_id", (run_id,)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["id"] = item["draft_id"]
            item["sources"] = json.loads(item.pop("sources_json") or "[]")
            item["edited"] = bool(item["edited"])
            item["sent"] = bool(item["sent"])
            out.append(item)
        return out

    def draft(self, draft_id: str, run_id: int | None = None) -> dict[str, Any] | None:
        run_id = run_id or self.last_run()
        row = self.conn.execute(
            "SELECT * FROM drafts WHERE run_id=? AND draft_id=?", (run_id, draft_id)
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["id"] = item["draft_id"]
        item["sources"] = json.loads(item.pop("sources_json") or "[]")
        item["edited"] = bool(item["edited"])
        item["sent"] = bool(item["sent"])
        return item

    def approve(self, draft_id: str, comment: str = "", delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        """Единственная точка «отправки».

        Без delivery — демо-мок: сеть не вызывается. С delivery — вызывающий код уже
        отправил письмо (SMTP) или уведомление (Telegram) и передаёт факт доставки.
        """
        run_id = self.last_run()
        draft = self.draft(draft_id, run_id)
        if not draft:
            return {"ok": False, "error": "draft_not_found"}
        if draft["status"] == "rejected":
            return {"ok": False, "error": "draft_rejected"}
        if draft["status"] == "approved":
            return {
                "ok": True,
                "status": "approved",
                "sent": bool(draft.get("sent")),
                "already": True,
                "mock": delivery is None,
            }
        sent = bool(delivery.get("sent")) if delivery is not None else True
        self.conn.execute(
            "UPDATE drafts SET status='approved', sent=? WHERE run_id=? AND draft_id=?",
            (int(sent), run_id, draft_id),
        )
        self.conn.execute(
            "INSERT INTO approvals(run_id, draft_id, decision, comment, at) VALUES(?,?,?,?,?)",
            (run_id, draft_id, "approve", comment, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()
        payload: dict[str, Any] = {"draft_id": draft_id}
        if delivery is None:
            payload["mock_send"] = True
        else:
            payload.update(
                {
                    "channel": delivery.get("channel", ""),
                    "sent": sent,
                    "to": delivery.get("to", ""),
                }
            )
            if delivery.get("message_id"):
                payload["message_id"] = delivery["message_id"]
            if delivery.get("reason"):
                payload["reason"] = delivery["reason"]
        self.log("approve", payload)
        result: dict[str, Any] = {"ok": True, "status": "approved", "sent": sent}
        if delivery is None:
            result["mock"] = True
        else:
            result["delivery"] = delivery
        return result

    def run_dataset(self, run_id: int | None = None) -> str:
        run_id = run_id or self.last_run()
        if not run_id:
            return ""
        row = self.conn.execute("SELECT dataset FROM runs WHERE id=?", (run_id,)).fetchone()
        return row["dataset"] if row and row["dataset"] else ""

    def seen_uids(self, channel: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT uid FROM seen_messages WHERE channel=?", (channel,)
        ).fetchall()
        return {row["uid"] for row in rows}

    def mark_seen(self, channel: str, uids: list[str]) -> None:
        if not uids:
            return
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen_messages(channel, uid, at) VALUES(?,?,?)",
            [(channel, uid, now) for uid in uids],
        )
        self.conn.commit()

    def reject(self, draft_id: str, comment: str = "") -> dict[str, Any]:
        run_id = self.last_run()
        self.conn.execute(
            "UPDATE drafts SET status='rejected' WHERE run_id=? AND draft_id=?", (run_id, draft_id)
        )
        self.conn.execute(
            "INSERT INTO approvals(run_id, draft_id, decision, comment, at) VALUES(?,?,?,?,?)",
            (run_id, draft_id, "reject", comment, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()
        self.log("reject", {"draft_id": draft_id})
        return {"ok": True, "status": "rejected"}

    def save_edit(self, draft_id: str, new_body: str, comment: str = "") -> dict[str, Any]:
        run_id = self.last_run()
        draft = self.draft(draft_id, run_id)
        if not draft:
            return {"ok": False, "error": "draft_not_found"}
        self.conn.execute(
            "UPDATE drafts SET body=?, edited=1 WHERE run_id=? AND draft_id=?",
            (new_body, run_id, draft_id),
        )
        self.conn.execute(
            "INSERT INTO edits(run_id, draft_id, topic, before_body, after_body, at) VALUES(?,?,?,?,?,?)",
            (
                run_id,
                draft_id,
                draft["topic"],
                draft["body"],
                new_body,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()
        self.log("edit", {"draft_id": draft_id, "comment": comment})
        return {"ok": True, "edited": True}

    def protocol_task_states(self, run_id: int | None = None) -> dict[str, dict[int, dict[str, Any]]]:
        """Статус, правка и синхронизация по каждой задаче протокола."""
        run_id = run_id or self.last_run()
        if not run_id:
            return {}
        rows = self.conn.execute(
            "SELECT * FROM protocol_tasks WHERE run_id=?", (run_id,)
        ).fetchall()
        out: dict[str, dict[int, dict[str, Any]]] = {}
        for row in rows:
            item: dict[str, Any] = {
                "status": row["status"] or "waiting",
                "note": row["note"] or "",
                "updated_at": row["updated_at"] or row["at"] or "",
                "revised": bool(row["revised"]),
                "revision_reason": row["revision_reason"] or "",
                "revised_at": row["revised_at"] or "",
                "synced_at": row["synced_at"] or "",
                "sync_note": row["sync_note"] or "",
                "sync_channel": row["sync_channel"] or "",
            }
            for key, column in (
                ("text", "text_override"),
                ("owner", "owner_override"),
                ("deadline", "deadline_override"),
            ):
                if row[column] is not None:
                    item[key] = row[column]
            out.setdefault(row["protocol_id"], {})[int(row["task_index"])] = item
        return out

    def protocol_task_statuses(self, run_id: int | None = None) -> dict[str, dict[int, str]]:
        states = self.protocol_task_states(run_id)
        return {
            protocol_id: {index: item["status"] for index, item in marks.items()}
            for protocol_id, marks in states.items()
        }

    def _protocol(self, protocol_id: str, run_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT payload_json FROM protocols WHERE run_id=? AND protocol_id=?",
            (run_id, protocol_id),
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _task_state_row(self, run_id: int, protocol_id: str, task_index: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM protocol_tasks WHERE run_id=? AND protocol_id=? AND task_index=?",
            (run_id, protocol_id, task_index),
        ).fetchone()
        return dict(row) if row else None

    def _task_ref(self, protocol_id: str, task_index: int) -> tuple[int | None, list[dict[str, Any]] | None, dict[str, Any] | None]:
        run_id = self.last_run()
        if not run_id:
            return None, None, {"ok": False, "error": "no_run"}
        protocol = self._protocol(protocol_id, run_id)
        if protocol is None:
            return None, None, {"ok": False, "error": "protocol_not_found"}
        tasks = protocol.get("tasks", [])
        if task_index < 0 or task_index >= len(tasks):
            return None, None, {"ok": False, "error": "task_not_found"}
        return run_id, tasks, None

    @staticmethod
    def _task_view(task: dict[str, Any], row: dict[str, Any] | None) -> dict[str, Any]:
        out = dict(task)
        if row:
            for key, column in (
                ("text", "text_override"),
                ("owner", "owner_override"),
                ("deadline", "deadline_override"),
            ):
                if row.get(column) is not None:
                    out[key] = row[column]
        return out

    def protocol_task(self, protocol_id: str, task_index: int) -> dict[str, Any] | None:
        run_id, tasks, error = self._task_ref(protocol_id, task_index)
        if error or tasks is None or run_id is None:
            return None
        row = self._task_state_row(run_id, protocol_id, task_index)
        task = self._task_view(tasks[task_index], row)
        task["status"] = (row or {}).get("status") or "waiting"
        task["revised"] = bool((row or {}).get("revised"))
        task["synced_at"] = (row or {}).get("synced_at") or ""
        task["revision_reason"] = (row or {}).get("revision_reason") or ""
        return task

    def _ensure_task_row(self, run_id: int, protocol_id: str, task_index: int) -> None:
        now = self._now()
        self.conn.execute(
            "INSERT OR IGNORE INTO protocol_tasks(run_id, protocol_id, task_index, status, note, revised, at, updated_at) "
            "VALUES(?,?,?,'waiting','',0,?,?)",
            (run_id, protocol_id, task_index, now, now),
        )

    def _mark_task(
        self, run_id: int, protocol_id: str, index: int, status: str, note: str | None = None
    ) -> None:
        now = self._now()
        self._ensure_task_row(run_id, protocol_id, index)
        self.conn.execute(
            "UPDATE protocol_tasks SET status=?, "
            "note=CASE WHEN ? IS NULL THEN note ELSE ? END, updated_at=? "
            "WHERE run_id=? AND protocol_id=? AND task_index=?",
            (status, note, note, now, run_id, protocol_id, index),
        )

    def approve_protocol_task(self, protocol_id: str, task_index: int) -> dict[str, Any]:
        """Поручение попадает в календарь только после действия человека."""
        run_id, tasks, error = self._task_ref(protocol_id, task_index)
        if error:
            return error
        row = self._task_state_row(run_id, protocol_id, task_index)
        current = (row or {}).get("status") or "waiting"
        if current != "waiting":
            return {"ok": True, "status": current, "already": True}
        self._mark_task(run_id, protocol_id, task_index, "approved")
        self.conn.commit()
        task = self._task_view(tasks[task_index], self._task_state_row(run_id, protocol_id, task_index))
        self.log(
            "task",
            {
                "protocol_id": protocol_id,
                "task_index": task_index,
                "owner": task.get("owner", ""),
                "text": task.get("text", ""),
                "mock_calendar": True,
            },
        )
        return {"ok": True, "status": "approved", "mock": True}

    def approve_protocol(self, protocol_id: str) -> dict[str, Any]:
        run_id = self.last_run()
        if not run_id:
            return {"ok": False, "error": "no_run"}
        protocol = self._protocol(protocol_id, run_id)
        if protocol is None:
            return {"ok": False, "error": "protocol_not_found"}
        statuses = self.protocol_task_statuses(run_id).get(protocol_id, {})
        approved = 0
        for index in range(len(protocol.get("tasks", []))):
            if statuses.get(index, "waiting") == "waiting":
                self._mark_task(run_id, protocol_id, index, "approved")
                approved += 1
        self.conn.commit()
        if approved:
            self.log("tasks_bulk", {"protocol_id": protocol_id, "count": approved, "mock_calendar": True})
        return {"ok": True, "approved": approved, "mock": True}

    def set_task_status(
        self, protocol_id: str, task_index: int, status: str, note: str = ""
    ) -> dict[str, Any]:
        """Решён / не решён / в работе: статус исполнения, а не факт постановки."""
        if status not in TASK_STATUSES:
            return {"ok": False, "error": "bad_status"}
        run_id, tasks, error = self._task_ref(protocol_id, task_index)
        if error:
            return error
        row = self._task_state_row(run_id, protocol_id, task_index)
        current = (row or {}).get("status") or "waiting"
        if status in {"in_progress", "done", "open"} and current == "waiting":
            return {"ok": False, "error": "task_not_approved"}
        note = (note or "").strip()
        if status == "open" and not note:
            note = "вопрос не решён"
        if status == "done" and not note:
            note = "решён"
        self._mark_task(run_id, protocol_id, task_index, status, note)
        self.conn.commit()
        task = self._task_view(tasks[task_index], self._task_state_row(run_id, protocol_id, task_index))
        self.log(
            "task_state",
            {
                "protocol_id": protocol_id,
                "task_index": task_index,
                "status": status,
                "note": note,
                "owner": task.get("owner", ""),
                "text": task.get("text", "")[:300],
            },
        )
        return {"ok": True, "status": status, "note": note, "updated_at": self._now(), "task": task}

    def revise_protocol_task(
        self,
        protocol_id: str,
        task_index: int,
        text: str,
        owner: str = "",
        deadline: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        """Задача изменилась: сохраняем ревизию, показываем её руководителю."""
        run_id, tasks, error = self._task_ref(protocol_id, task_index)
        if error:
            return error
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "empty_text"}
        row = self._task_state_row(run_id, protocol_id, task_index)
        before = self._task_view(tasks[task_index], row)
        now = self._now()
        self._ensure_task_row(run_id, protocol_id, task_index)
        cur = self.conn.execute(
            "INSERT INTO protocol_task_revisions(run_id, protocol_id, task_index, before_text, after_text, "
            "before_owner, after_owner, before_deadline, after_deadline, reason, at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                protocol_id,
                task_index,
                before.get("text", ""),
                text,
                before.get("owner", ""),
                owner,
                before.get("deadline", ""),
                deadline,
                reason,
                now,
            ),
        )
        revision_id = int(cur.lastrowid)
        self.conn.execute(
            "UPDATE protocol_tasks SET text_override=?, owner_override=?, deadline_override=?, "
            "revised=1, revision_reason=?, revised_at=?, synced_at=NULL, sync_note=NULL, "
            "sync_channel=NULL, updated_at=? WHERE run_id=? AND protocol_id=? AND task_index=?",
            (text, owner, deadline, reason, now, now, run_id, protocol_id, task_index),
        )
        self.conn.commit()
        updated = self._task_view(tasks[task_index], self._task_state_row(run_id, protocol_id, task_index))
        self.log(
            "task_revise",
            {
                "protocol_id": protocol_id,
                "task_index": task_index,
                "revision_id": revision_id,
                "owner": owner,
                "reason": reason,
            },
        )
        return {
            "ok": True,
            "revised": True,
            "revision_id": revision_id,
            "revised_at": now,
            "synced_at": "",
            "task": updated,
        }

    def sync_protocol_task(
        self, protocol_id: str, task_index: int, note: str = "", channel: str = "mock"
    ) -> dict[str, Any]:
        """Синхронизация правки с руководителем — отдельное действие человека."""
        run_id, tasks, error = self._task_ref(protocol_id, task_index)
        if error:
            return error
        row = self.conn.execute(
            "SELECT id, synced_at FROM protocol_task_revisions "
            "WHERE run_id=? AND protocol_id=? AND task_index=? ORDER BY id DESC LIMIT 1",
            (run_id, protocol_id, task_index),
        ).fetchone()
        if not row:
            return {"ok": False, "error": "nothing_to_sync"}
        if row["synced_at"]:
            task = self._task_view(tasks[task_index], self._task_state_row(run_id, protocol_id, task_index))
            return {"ok": True, "already": True, "synced_at": row["synced_at"], "channel": channel, "task": task}
        now = self._now()
        self.conn.execute(
            "UPDATE protocol_task_revisions SET synced_at=?, sync_note=?, sync_channel=? WHERE id=?",
            (now, note, channel, row["id"]),
        )
        self.conn.execute(
            "UPDATE protocol_tasks SET synced_at=?, sync_note=?, sync_channel=?, updated_at=? "
            "WHERE run_id=? AND protocol_id=? AND task_index=?",
            (now, note, channel, now, run_id, protocol_id, task_index),
        )
        self.conn.commit()
        task = self._task_view(tasks[task_index], self._task_state_row(run_id, protocol_id, task_index))
        self.log(
            "task_sync",
            {
                "protocol_id": protocol_id,
                "task_index": task_index,
                "channel": channel,
                "note": note,
                "owner": task.get("owner", ""),
                "text": task.get("text", "")[:300],
            },
        )
        return {
            "ok": True,
            "already": False,
            "synced_at": now,
            "channel": channel,
            "note": note,
            "task": task,
        }

    @staticmethod
    def _step_view(row: sqlite3.Row) -> dict[str, Any]:
        try:
            depends = [int(value) for value in json.loads(row["depends_on"] or "[]")]
        except (TypeError, ValueError, json.JSONDecodeError):
            depends = []
        return {
            "index": int(row["step_index"]),
            "title": row["title"],
            "owner": row["owner"] or "",
            "role": row["role"] or "",
            "deadline": row["deadline"] or "",
            "depends_on": depends,
            "parallel": bool(row["parallel"]),
            "status": row["status"] or "waiting",
            "note": row["note"] or "",
            "updated_at": row["updated_at"] or row["at"] or "",
        }

    def _steps_for(self, run_id: int, protocol_id: str, task_index: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM protocol_steps WHERE run_id=? AND protocol_id=? AND task_index=? "
            "ORDER BY step_index",
            (run_id, protocol_id, task_index),
        ).fetchall()
        return [self._step_view(row) for row in rows]

    def protocol_steps(self, run_id: int | None = None) -> dict[str, dict[int, list[dict[str, Any]]]]:
        run_id = run_id or self.last_run()
        if not run_id:
            return {}
        rows = self.conn.execute(
            "SELECT * FROM protocol_steps WHERE run_id=? ORDER BY protocol_id, task_index, step_index",
            (run_id,),
        ).fetchall()
        out: dict[str, dict[int, list[dict[str, Any]]]] = {}
        for row in rows:
            steps = out.setdefault(row["protocol_id"], {}).setdefault(int(row["task_index"]), [])
            steps.append(self._step_view(row))
        for by_task in out.values():
            for steps in by_task.values():
                done = {step["index"] for step in steps if step["status"] == "done"}
                for step in steps:
                    step["blocked_by"] = [dep for dep in step["depends_on"] if dep not in done]
        return out

    def save_protocol_steps(
        self,
        protocol_id: str,
        task_index: int,
        steps: list[dict[str, Any]],
        mode: str = "replace",
    ) -> dict[str, Any]:
        """Раскладываем составное поручение на шаги с последовательностью и параллелью."""
        if mode not in {"replace", "append"}:
            return {"ok": False, "error": "bad_mode"}
        if len(steps) > 20:
            return {"ok": False, "error": "too_many_steps"}
        run_id, tasks, error = self._task_ref(protocol_id, task_index)
        if error:
            return error
        existing = self._steps_for(run_id, protocol_id, task_index) if mode == "append" else []
        offset = len(existing)
        total = offset + len(steps)
        normalized: list[dict[str, Any]] = []
        for position, item in enumerate(steps):
            title = str(item.get("title", "")).strip()
            if not title:
                return {"ok": False, "error": "empty_step"}
            deps_raw = item.get("depends_on") or []
            deps: list[int] = []
            for value in deps_raw:
                try:
                    dep = int(value)
                except (TypeError, ValueError):
                    return {"ok": False, "error": "bad_dependency"}
                if dep < 0 or dep >= total:
                    return {"ok": False, "error": "bad_dependency"}
                if dep >= offset + position:
                    return {"ok": False, "error": "dependency_forward"}
                if dep not in deps:
                    deps.append(dep)
            deps.sort()
            normalized.append(
                {
                    "title": title,
                    "owner": str(item.get("owner", "")).strip(),
                    "role": str(item.get("role", "")).strip(),
                    "deadline": str(item.get("deadline", "")).strip(),
                    "depends_on": deps,
                    "parallel": not deps,
                }
            )
        now = self._now()
        if mode == "replace":
            self.conn.execute(
                "DELETE FROM protocol_steps WHERE run_id=? AND protocol_id=? AND task_index=?",
                (run_id, protocol_id, task_index),
            )
        for position, item in enumerate(normalized):
            self.conn.execute(
                "INSERT INTO protocol_steps(run_id, protocol_id, task_index, step_index, title, owner, role, "
                "deadline, depends_on, parallel, status, note, at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'waiting','',?,?)",
                (
                    run_id,
                    protocol_id,
                    task_index,
                    offset + position,
                    item["title"],
                    item["owner"],
                    item["role"],
                    item["deadline"],
                    json.dumps(item["depends_on"]),
                    int(item["parallel"]),
                    now,
                    now,
                ),
            )
        self.conn.commit()
        all_steps = self.protocol_steps(run_id).get(protocol_id, {}).get(task_index, [])
        self.log(
            "task_steps",
            {
                "protocol_id": protocol_id,
                "task_index": task_index,
                "mode": mode,
                "count": len(all_steps),
                "roles": sorted({step["role"] for step in all_steps if step["role"]}),
            },
        )
        return {"ok": True, "mode": mode, "count": len(all_steps), "steps": all_steps}

    def set_step_status(
        self,
        protocol_id: str,
        task_index: int,
        step_index: int,
        status: str,
        note: str = "",
    ) -> dict[str, Any]:
        if status not in STEP_STATUSES:
            return {"ok": False, "error": "bad_status"}
        run_id, tasks, error = self._task_ref(protocol_id, task_index)
        if error:
            return error
        steps = self._steps_for(run_id, protocol_id, task_index)
        current = next((step for step in steps if step["index"] == step_index), None)
        if current is None:
            return {"ok": False, "error": "step_not_found"}
        if status in {"in_progress", "done"}:
            done = {step["index"] for step in steps if step["status"] == "done"}
            blocked = [dep for dep in current["depends_on"] if dep not in done]
            if blocked:
                return {"ok": False, "error": "blocked", "blocked_by": blocked}
        if current["status"] == "done" and status != "done":
            dependents = [
                step["index"]
                for step in steps
                if step_index in step["depends_on"] and step["status"] in {"in_progress", "done"}
            ]
            if dependents:
                return {"ok": False, "error": "dependent_started", "dependent": dependents}
        now = self._now()
        self.conn.execute(
            "UPDATE protocol_steps SET status=?, note=?, updated_at=? "
            "WHERE run_id=? AND protocol_id=? AND task_index=? AND step_index=?",
            (status, note, now, run_id, protocol_id, task_index, step_index),
        )
        self.conn.commit()
        updated = self.protocol_steps(run_id).get(protocol_id, {}).get(task_index, [])
        step = next((item for item in updated if item["index"] == step_index), current)
        self.log(
            "step_state",
            {
                "protocol_id": protocol_id,
                "task_index": task_index,
                "step_index": step_index,
                "status": status,
                "role": step.get("role", ""),
                "title": step.get("title", "")[:300],
            },
        )
        return {"ok": True, "step": step, "steps": updated}

    def protocol_progress(self, run_id: int | None = None) -> dict[str, Any]:
        run_id = run_id or self.last_run()
        if not run_id:
            return {"overall": {}, "protocols": {}}
        protocols = self.protocols(run_id)
        states = self.protocol_task_states(run_id)
        steps = self.protocol_steps(run_id)
        overall = {
            "tasks": 0,
            "waiting": 0,
            "approved": 0,
            "in_progress": 0,
            "done": 0,
            "open": 0,
            "resolved": 0,
            "unresolved": 0,
            "revised": 0,
            "unsynced": 0,
            "steps": 0,
            "steps_done": 0,
            "ready_to_close": 0,
        }
        per: dict[str, dict[str, Any]] = {}
        for protocol in protocols:
            protocol_id = protocol.get("id", "")
            marks = states.get(protocol_id, {})
            item = {key: 0 for key in overall}
            for index, task in enumerate(protocol.get("tasks", [])):
                state = marks.get(index, {})
                status = state.get("status", "waiting")
                item["tasks"] += 1
                if status in item:
                    item[status] += 1
                if status == "done":
                    item["resolved"] += 1
                if status == "open":
                    item["unresolved"] += 1
                if state.get("revised"):
                    item["revised"] += 1
                    if not state.get("synced_at"):
                        item["unsynced"] += 1
                step_list = steps.get(protocol_id, {}).get(index, [])
                if step_list:
                    item["steps"] += len(step_list)
                    done = sum(1 for step in step_list if step["status"] == "done")
                    item["steps_done"] += done
                    if done == len(step_list) and status != "done":
                        item["ready_to_close"] += 1
            per[protocol_id] = item
            for key in overall:
                overall[key] += item[key]
        return {"overall": overall, "protocols": per}

    def learning_hints(self) -> dict[str, dict[str, Any]]:
        """Последняя правка по каждой теме становится шаблоном для будущих черновиков."""
        rows = self.conn.execute(
            "SELECT topic, after_body, draft_id FROM edits ORDER BY id DESC"
        ).fetchall()
        hints: dict[str, dict[str, Any]] = {}
        for row in rows:
            topic = row["topic"]
            if topic in hints:
                continue
            draft = self.conn.execute(
                "SELECT subject, kind, recipient FROM drafts WHERE draft_id=? ORDER BY run_id DESC LIMIT 1",
                (row["draft_id"],),
            ).fetchone()
            hints[topic] = {
                "body": row["after_body"],
                "subject": draft["subject"] if draft else "",
                "kind": draft["kind"] if draft else "learned",
                "recipient": draft["recipient"] if draft else "",
            }
        return hints
