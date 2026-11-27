# -*- coding: utf-8 -*-
"""Сборка решений и черновиков. Каждый факт — со ссылкой на источник."""
from __future__ import annotations

from datetime import datetime, timedelta

from .knowledge import KnowledgeBase
from .models import Action, Classification, Draft, Email, Event, Person, Source, Task
from .profile import Profile

OPTIONS = {
    "деньги и бюджеты": "согласовать сумму / запросить обоснование / отказать",
    "кадры": "согласовать / уточнить у HR-партнёра / отложить",
    "обязательства клиентам": "подтвердить срок / предложить новый / запросить детали",
    "руководство": "подготовить ответ / запросить вводные / делегировать подготовку",
    "приглашение": "принять / отказаться / предложить замену",
    "календарь": "перенести / сохранить слот / предложить альтернативу",
}

ASSIGN_ACTIONS = {
    "продажи": "подготовить КП и вернуться со сроком",
    "доступы": "оформить доступ после решения по кадрам",
    "жалоба клиента": "разобрать обращение и вернуть клиенту со сроком",
}


def next_business_days(start: datetime, days: int) -> datetime:
    current = start
    added = 0
    while added < days:
        current += timedelta(days=1)
        if current.weekday() < 5:
            added += 1
    return current


def _unique_sources(items: list[Source], limit: int = 4) -> list[Source]:
    seen: set[str] = set()
    out: list[Source] = []
    for source in items:
        if source.ref in seen:
            continue
        seen.add(source.ref)
        out.append(source)
        if len(out) >= limit:
            break
    return out


def _cite(numbers: list[int]) -> str:
    return " ".join(f"[{n}]" for n in numbers)


def _sources_with_context(
    classification: Classification,
    email: Email,
    kb: KnowledgeBase,
) -> list[Source]:
    quote = email.body.strip().replace("\n", " ")
    if len(quote) > 160:
        quote = quote[:157] + "…"
    items = [
        Source(kind="email", title=email.subject, quote=quote, ref=f"inbox.csv#{email.id}")
    ]
    items += classification.sources
    if classification.action == Action.ASK:
        # к решению добавляем только материалы с двумя общими словами — иначе шум
        items += kb.search(
            f"{email.subject} {email.body}",
            limit=1,
            kinds={"task", "org", "meeting", "profile"},
            min_overlap=2,
            min_score=1.0,
        )
    return _unique_sources(items)


def build_draft(
    classification: Classification,
    email: Email,
    profile: Profile,
    kb: KnowledgeBase,
    hints: dict[str, dict] | None = None,
    now: datetime | None = None,
) -> Draft | None:
    if classification.action in {Action.DIGEST, Action.IGNORE}:
        return None

    now = now or datetime.now()
    deadline = next_business_days(email.received, 2)
    sources = _sources_with_context(classification, email, kb)
    hint = (hints or {}).get(classification.topic)
    if hint:
        return Draft(
            id=f"d-{email.id}",
            email_id=email.id,
            kind=hint.get("kind", "learned"),
            topic=classification.topic,
            recipient=hint.get("recipient", ""),
            subject=hint["subject"],
            body=hint["body"],
            sources=sources,
            created_at=now,
        )

    greeting = profile.greeting_name()
    signature = profile.signature

    if classification.action == Action.ASK:
        options = OPTIONS.get(classification.topic, "решить / запросить вводные / делегировать")
        rule = classification.reasons[0] if classification.reasons else "правило профиля"
        refs = _cite(list(range(1, min(len(sources), 3) + 1)))
        body = (
            f"Нужно ваше решение: «{email.subject}».\n"
            f"{email.from_name}: {email.body}\n"
            f"Почему у вас: {rule}. {refs}\n"
            f"Варианты: {options}.\n"
            f"Срок решения: до {deadline:%d.%m.%Y}.\n"
        )
        return Draft(
            id=f"d-{email.id}",
            email_id=email.id,
            kind="decision",
            topic=classification.topic,
            recipient=profile.name,
            subject=f"Решение: {email.subject}",
            body=body,
            sources=sources,
            created_at=now,
        )

    if classification.action == Action.DELEGATE:
        action = ASSIGN_ACTIONS.get(classification.topic, "взять в работу и вернуться со сроком")
        refs = _cite(list(range(1, min(len(sources), 3) + 1)))
        assignee = classification.assignee or "ответственному"
        first = classification.assignee.split()[0] if classification.assignee else "коллеги"
        body = (
            f"{first}, добрый день.\n\n"
            f"Передаю запрос: «{email.subject}» — {email.from_name}.\n"
            f"Прошу {action} до {deadline:%d.%m.%Y}. {refs}\n"
            f"Итог включу в пятничную сводку.\n\n"
            f"{signature}\n"
        )
        return Draft(
            id=f"d-{email.id}",
            email_id=email.id,
            kind="assignment",
            topic=classification.topic,
            recipient=assignee,
            to_email=classification.assignee_email,
            subject=f"Передаю: {email.subject}",
            body=body,
            sources=sources,
            created_at=now,
        )

    # AUTO: только внутренний календарь, без внешних обязательств
    refs = _cite([1] if sources else [])
    if "перенос" in email.text:
        body = (
            f"Перенос согласован: «{email.subject}».\n"
            f"Проверил календарь: защищённые блоки не затронуты. {refs}\n\n"
            f"{signature}\n"
        )
    else:
        event = next((s for s in sources if s.kind == "calendar"), None)
        when = f" {event.quote}" if event else ""
        body = (
            f"Подтверждаю участие{when}. {refs}\n\n"
            f"{signature}\n"
        )
    return Draft(
        id=f"d-{email.id}",
        email_id=email.id,
        kind="calendar",
        topic=classification.topic,
        recipient=email.from_name or email.from_email,
        to_email=email.from_email,
        subject=f"Re: {email.subject}",
        body=body,
        sources=sources,
        created_at=now,
    )
