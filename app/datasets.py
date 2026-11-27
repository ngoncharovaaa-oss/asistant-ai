# -*- coding: utf-8 -*-
"""Загрузчики синтетического датасета ИИкузницы."""
from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path

from .models import Email, Event, Person, Task


def _row_text(row: dict[str, str], key: str) -> str:
    return (row.get(key) or "").strip()


def load_inbox(path: Path) -> list[Email]:
    emails: list[Email] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            emails.append(
                Email(
                    id=_row_text(row, "id"),
                    received=datetime.strptime(_row_text(row, "received"), "%Y-%m-%d %H:%M"),
                    from_name=_row_text(row, "from_name"),
                    from_email=_row_text(row, "from_email"),
                    sender_role=_row_text(row, "sender_role"),
                    to=_row_text(row, "to"),
                    subject=_row_text(row, "subject"),
                    body=_row_text(row, "body"),
                    category=_row_text(row, "category"),
                    expected_action=_row_text(row, "expected_action"),
                    reason=_row_text(row, "reason"),
                )
            )
    return emails


def load_tasks(path: Path) -> list[Task]:
    tasks: list[Task] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            due = _row_text(row, "due")
            tasks.append(
                Task(
                    id=_row_text(row, "id"),
                    task=_row_text(row, "task"),
                    owner=_row_text(row, "owner"),
                    due=datetime.strptime(due, "%Y-%m-%d") if due else None,
                    status=_row_text(row, "status"),
                    source=_row_text(row, "source"),
                )
            )
    return tasks


def load_org(path: Path) -> list[Person]:
    people: list[Person] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            people.append(
                Person(
                    id=_row_text(row, "id"),
                    name=_row_text(row, "name"),
                    position=_row_text(row, "position"),
                    role=_row_text(row, "role_for_leader"),
                    reports_to=_row_text(row, "reports_to"),
                    email=_row_text(row, "email"),
                )
            )
    return people


def load_contacts(path: Path) -> dict[str, str]:
    """Необязательный справочник «имя → почта» для делегирования в живом режиме."""
    if not path.exists():
        return {}
    contacts: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            name = _row_text(row, "name")
            email = _row_text(row, "email")
            if name and email:
                contacts[name.lower()] = email
    return contacts


def _unfold(raw: str) -> list[str]:
    lines: list[str] = []
    for line in raw.splitlines():
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _parse_dt(value: str) -> datetime | None:
    value = value.split(":")[-1].strip()
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def load_calendar(path: Path) -> list[Event]:
    events: list[Event] = []
    current: dict[str, str] | None = None
    attendees: list[str] = []
    for line in _unfold(path.read_text(encoding="utf-8")):
        if line.startswith("BEGIN:VEVENT"):
            current, attendees = {}, []
            continue
        if line.startswith("END:VEVENT") and current is not None:
            start = _parse_dt(current.get("DTSTART", ""))
            end = _parse_dt(current.get("DTEND", ""))
            if start and end:
                events.append(
                    Event(
                        uid=current.get("UID", ""),
                        start=start,
                        end=end,
                        summary=current.get("SUMMARY", ""),
                        category=current.get("CATEGORIES", ""),
                        attendees=attendees,
                    )
                )
            current = None
            continue
        if current is None:
            continue
        if line.startswith("ATTENDEE"):
            match = re.search(r"CN=([^:;]+)", line)
            if match:
                attendees.append(match.group(1).strip())
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.split(";")[0]
            if key in {"UID", "DTSTART", "DTEND", "SUMMARY", "CATEGORIES"}:
                current[key] = value
    return events


def load_meeting_summaries(meetings_dir: Path) -> list[tuple[Path, str]]:
    if not meetings_dir.exists():
        return []
    return [
        (path, path.read_text(encoding="utf-8"))
        for path in sorted(meetings_dir.glob("*Summary*.txt"))
    ]
