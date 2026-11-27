# -*- coding: utf-8 -*-
"""Модели данных агента."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Action(str, Enum):
    ASK = "спросить_руководителя"
    DELEGATE = "делегировать"
    AUTO = "авто"
    DIGEST = "в_сводку"
    IGNORE = "игнорировать"


class Risk(str, Enum):
    MONEY = "деньги"
    HR = "кадры"
    CLIENT = "обязательства_клиентам"
    LEADERSHIP = "руководство"
    EXTERNAL = "внешнее_выступление"


SENSITIVE_RISKS = {Risk.MONEY, Risk.HR, Risk.CLIENT, Risk.LEADERSHIP}


@dataclass
class Email:
    id: str
    received: datetime
    from_name: str
    from_email: str
    sender_role: str
    to: str
    subject: str
    body: str
    category: str = ""
    expected_action: str = ""
    reason: str = ""

    @property
    def text(self) -> str:
        return f"{self.subject}\n{self.body}".lower()


@dataclass
class Person:
    id: str
    name: str
    position: str
    role: str
    reports_to: str
    email: str = ""


@dataclass
class Task:
    id: str
    task: str
    owner: str
    due: datetime | None
    status: str
    source: str


@dataclass
class Event:
    uid: str
    start: datetime
    end: datetime
    summary: str
    category: str
    attendees: list[str] = field(default_factory=list)

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


@dataclass
class Source:
    kind: str
    title: str
    quote: str
    ref: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "title": self.title, "quote": self.quote, "ref": self.ref}


@dataclass
class Classification:
    email_id: str
    action: Action
    topic: str
    confidence: float
    reasons: list[str]
    risks: list[Risk]
    assignee: str = ""
    assignee_email: str = ""
    sources: list[Source] = field(default_factory=list)

    @property
    def requires_approval(self) -> bool:
        return True  # инвариант: любое действие ждёт подтверждения

    def as_dict(self) -> dict[str, Any]:
        return {
            "email_id": self.email_id,
            "action": self.action.value,
            "topic": self.topic,
            "confidence": round(self.confidence, 2),
            "reasons": self.reasons,
            "risks": [r.value for r in self.risks],
            "assignee": self.assignee,
            "assignee_email": self.assignee_email,
            "sources": [s.as_dict() for s in self.sources],
        }


@dataclass
class Draft:
    id: str
    email_id: str
    kind: str  # reply | assignment | decision | calendar
    topic: str
    recipient: str
    subject: str
    body: str
    sources: list[Source]
    to_email: str = ""
    status: str = "waiting_approval"
    created_at: datetime | None = None
    edited: bool = False
    sent: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email_id": self.email_id,
            "kind": self.kind,
            "topic": self.topic,
            "recipient": self.recipient,
            "to_email": self.to_email,
            "subject": self.subject,
            "body": self.body,
            "sources": [s.as_dict() for s in self.sources],
            "status": self.status,
            "created_at": self.created_at.isoformat(timespec="seconds") if self.created_at else None,
            "edited": self.edited,
            "sent": self.sent,
        }


@dataclass
class ProtocolTask:
    text: str
    owner: str
    deadline: str = ""
    done: bool = False
    status_source: str = ""


@dataclass
class Protocol:
    id: str
    title: str
    decisions: list[str]
    tasks: list[ProtocolTask]
    open_questions: list[str]
    sources: list[Source]
    path: str = ""
    when: str = ""
    host: str = ""
    participants: str = ""
    agenda: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    doc_path: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "decisions": self.decisions,
            "tasks": [
                {
                    "text": t.text,
                    "owner": t.owner,
                    "deadline": t.deadline,
                    "status_source": t.status_source,
                }
                for t in self.tasks
            ],
            "open_questions": self.open_questions,
            "sources": [s.as_dict() for s in self.sources],
            "path": self.path,
            "when": self.when,
            "host": self.host,
            "participants": self.participants,
            "agenda": self.agenda,
            "risks": self.risks,
            "next_steps": self.next_steps,
            "doc_path": self.doc_path,
        }
