# -*- coding: utf-8 -*-
"""Утренний план и недельная сводка: только важное, без пересылки всех писем."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from .models import Action, Classification, Email, Event, Protocol, Task

PROTECTED_CATEGORIES = {"один_на_один", "руководство", "клиент", "партнёр"}
WEEKDAYS = {
    0: "понедельник", 1: "вторник", 2: "среда", 3: "четверг",
    4: "пятница", 5: "суббота", 6: "воскресенье",
}


def _day_events(events: list[Event], day) -> list[Event]:
    return sorted((event for event in events if event.start.date() == day), key=lambda event: event.start)


def morning_plan(
    day: datetime,
    emails: list[Email],
    classifications: list[Classification],
    events: list[Event],
    tasks: list[Task],
    protocols: list[Protocol] | None = None,
    limit: int = 4,
) -> dict:
    by_id = {item.email_id: item for item in classifications}
    todays = [email for email in emails if email.received.date() == day.date()]
    decisions = [
        {
            "email_id": email.id,
            "subject": email.subject,
            "topic": by_id[email.id].topic if email.id in by_id else "",
            "risk": [risk.value for risk in by_id[email.id].risks] if email.id in by_id else [],
        }
        for email in todays
        if email.id in by_id and by_id[email.id].action == Action.ASK
    ]
    digest_items = [
        {"email_id": email.id, "subject": email.subject, "from": email.from_name}
        for email in todays
        if email.id in by_id and by_id[email.id].action == Action.DIGEST
    ]
    delegated = [
        {
            "email_id": email.id,
            "subject": email.subject,
            "assignee": by_id[email.id].assignee,
        }
        for email in todays
        if email.id in by_id and by_id[email.id].action == Action.DELEGATE
    ]
    due_soon = [
        task
        for task in tasks
        if task.due and task.status != "готово" and day.date() <= task.due.date() <= (day + timedelta(days=2)).date()
    ]
    overdue = [
        task
        for task in tasks
        if task.due and task.status != "готово" and task.due.date() < day.date()
    ]
    day_events = _day_events(events, day.date())
    protected = [event for event in day_events if event.category in PROTECTED_CATEGORIES]
    focus: list[str] = []
    seen: set[str] = set()
    candidates = [event.summary for event in protected] + [item["subject"] for item in decisions]
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        focus.append(candidate)
        if len(focus) >= limit:
            break

    return {
        "date": day.strftime("%Y-%m-%d"),
        "weekday": WEEKDAYS[day.weekday()],
        "events": [
            {
                "summary": event.summary,
                "start": event.start.strftime("%H:%M"),
                "end": event.end.strftime("%H:%M"),
                "category": event.category,
                "protected": event.category in PROTECTED_CATEGORIES,
            }
            for event in day_events
        ],
        "meetings_hours": round(sum(event.hours for event in day_events), 1),
        "protected_hours": round(sum(event.hours for event in protected), 1),
        "decisions": decisions,
        "digest": digest_items,
        "delegated": delegated,
        "due_soon": [
            {"id": task.id, "task": task.task, "owner": task.owner, "due": task.due.strftime("%d.%m") if task.due else ""}
            for task in due_soon
        ],
        "overdue": [
            {"id": task.id, "task": task.task, "owner": task.owner, "due": task.due.strftime("%d.%m") if task.due else ""}
            for task in overdue
        ],
        "focus": focus[:limit],
        "protocols": [protocol.title for protocol in (protocols or [])][:3],
    }


def weekly_digest(
    week_start: datetime,
    emails: list[Email],
    classifications: list[Classification],
    events: list[Event],
    tasks: list[Task],
    protocols: list[Protocol] | None = None,
    metrics: dict | None = None,
) -> dict:
    week_end = week_start + timedelta(days=7)
    week_emails = [email for email in emails if week_start.date() <= email.received.date() < week_end.date()]
    ids = {email.id for email in week_emails}
    week_classes = [item for item in classifications if item.email_id in ids]
    counts = Counter(item.action.value for item in week_classes)
    week_events = [event for event in events if week_start.date() <= event.start.date() < week_end.date()]
    protected = [event for event in week_events if event.category in PROTECTED_CATEGORIES]
    overdue = [task for task in tasks if task.due and task.status != "готово" and task.due < week_end]
    protocols = protocols or []
    email_by_id = {email.id: email for email in week_emails}
    decisions = sorted(
        (item for item in week_classes if item.action == Action.ASK),
        key=lambda item: email_by_id[item.email_id].received if item.email_id in email_by_id else week_end,
    )
    delegated = [item for item in week_classes if item.action == Action.DELEGATE]
    decisions_by_topic = Counter(item.topic for item in decisions)
    no_leader = counts.get(Action.AUTO.value, 0) + counts.get(Action.DIGEST.value, 0)
    no_leader += counts.get(Action.IGNORE.value, 0) + counts.get(Action.DELEGATE.value, 0)

    return {
        "week_start": week_start.strftime("%Y-%m-%d"),
        "week_end": (week_end - timedelta(days=1)).strftime("%Y-%m-%d"),
        "emails": len(week_emails),
        "actions": dict(sorted(counts.items(), key=lambda pair: -pair[1])),
        "without_leader": no_leader,
        "without_leader_share": round(no_leader / len(week_emails), 3) if week_emails else 0.0,
        "decisions_total": len(decisions),
        "decisions_by_topic": dict(decisions_by_topic.most_common()),
        "decisions_waiting": [
            {
                "email_id": item.email_id,
                "subject": email_by_id[item.email_id].subject if item.email_id in email_by_id else "",
                "topic": item.topic,
                "confidence": round(item.confidence, 2),
            }
            for item in decisions
        ],
        "delegated": [
            {"email_id": item.email_id, "topic": item.topic, "assignee": item.assignee} for item in delegated
        ],
        "meetings": len(week_events),
        "meeting_hours": round(sum(event.hours for event in week_events), 1),
        "protected_hours": round(sum(event.hours for event in protected), 1),
        "tasks_overdue": [
            {"id": task.id, "task": task.task, "owner": task.owner, "due": task.due.strftime("%d.%m") if task.due else ""}
            for task in overdue
        ],
        "protocols": [
            {"id": protocol.id, "title": protocol.title, "tasks": len(protocol.tasks), "open": len(protocol.open_questions)}
            for protocol in protocols
        ],
        "metrics": metrics or {},
    }


def _hours(value: float) -> str:
    return f"{value:.1f}".replace(".", ",")


def digest_markdown(digest: dict) -> str:
    lines = [f"# Неделя {digest['week_start']} — {digest['week_end']}", ""]
    lines.append(f"Писем: {digest['emails']}. Без участия руководителя: {digest['without_leader']} "
                 f"({digest['without_leader_share']:.0%}).")
    lines.append("")
    lines.append("## Что сделано по правилам")
    for action, count in digest["actions"].items():
        lines.append(f"- {action}: {count}")
    lines.append("")
    lines.append(f"Встреч: {digest['meetings']} ({_hours(digest['meeting_hours'])} ч), "
                 f"из них защищённых: {_hours(digest['protected_hours'])} ч.")
    lines.append("")
    lines.append(f"## Ждут решения: {digest.get('decisions_total', len(digest['decisions_waiting']))}")
    if digest["decisions_waiting"]:
        for topic, count in (digest.get("decisions_by_topic") or {}).items():
            lines.append(f"- {topic}: {count}")
        lines.append("")
        lines.append("Первые по сроку:")
        for item in digest["decisions_waiting"][:5]:
            subject = f" · {item['subject']}" if item.get("subject") else ""
            lines.append(f"- {item['email_id']} · {item['topic']}{subject}")
    else:
        lines.append("- Нет.")
    lines.append("")
    lines.append("## Передано команде")
    if digest["delegated"]:
        for item in digest["delegated"]:
            lines.append(f"- {item['email_id']} → {item['assignee'] or 'ответственный'} · {item['topic']}")
    else:
        lines.append("- Нет.")
    lines.append("")
    if digest["tasks_overdue"]:
        lines.append("## Риски по срокам")
        for task in digest["tasks_overdue"]:
            lines.append(f"- {task['id']} {task['task']} — {task['owner']}, срок {task['due']}")
        lines.append("")
    if digest["protocols"]:
        lines.append("## Протоколы")
        for protocol in digest["protocols"]:
            lines.append(f"- {protocol['title']}: задач {protocol['tasks']}, открытых вопросов {protocol['open']}")
        lines.append("")
    return "\n".join(lines)
