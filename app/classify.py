# -*- coding: utf-8 -*-
"""Правила разбора входящих. Работают офлайн, без LLM; каждое решение объяснимо."""
from __future__ import annotations

import re

from .models import Action, Classification, Email, Event, Person, Risk, Source, Task
from .profile import Profile

HR_WORDS = ("отпуск", "кандидат", "найм", "нанять", "оффер", "увольнен", "кадров", "кадры", "резюме")
MONEY_WORDS = ("скидк", "перераспределен", "бюджет q", "закупк", "согласование бюджета", "бюджеты")
CLIENT_OBLIGATION_WORDS = ("сроки запуска", "срок запуска", "подтвердите", "гарант", "обязательств")
LEADERSHIP_ROLE = "руководитель руководителя"
NEWSLETTER_ROLE = "рассылка"
INVITE_ROLE = "приглашение"


def _stems(text: str) -> set[str]:
    return {word[:5] for word in re.findall(r"[a-zа-яё0-9-]+", text.lower()) if len(word) > 3}


def _calendar_match(email: Email, events: list[Event]) -> Event | None:
    words = _stems(email.text)
    best: Event | None = None
    best_score = 0
    for event in events:
        if event.start.date() != email.received.date():
            continue
        score = len(words & _stems(event.summary))
        if score > best_score:
            best, best_score = event, score
    return best


def _find_person(org: list[Person], *needles: str) -> Person | None:
    for person in org:
        haystack = f"{person.name} {person.position}".lower()
        if all(needle.lower() in haystack for needle in needles):
            return person
    return None


def _related_task(tasks: list[Task], words: tuple[str, ...]) -> Task | None:
    for task in tasks:
        low = task.task.lower()
        if any(word in low for word in words):
            return task
    return None


def _source_person(person: Person | None) -> list[Source]:
    if not person:
        return []
    return [
        Source(
            kind="org",
            title=f"{person.name} — {person.position}",
            quote=f"роль для руководителя: {person.role}",
            ref=f"org_structure.csv#{person.id}",
        )
    ]


def _source_profile_rule(profile: Profile, risk: Risk | None) -> list[Source]:
    if risk is None:
        return []
    rule = profile.rule_for(risk)
    return [rule.source] if rule else []


def _source_auto_rule(profile: Profile, needle: str) -> list[Source]:
    for bullet in profile.auto_rules:
        if needle in bullet.lower():
            return [
                Source(
                    kind="profile",
                    title="Можно без подтверждения",
                    quote=bullet,
                    ref=f"{profile.source_path.name}#auto",
                )
            ]
    return []


def _topic(text: str, role: str) -> str:
    low = text.lower()
    if role == NEWSLETTER_ROLE:
        return "рассылка"
    if role == INVITE_ROLE:
        return "приглашение"
    if any(w in low for w in HR_WORDS):
        return "кадры"
    if any(w in low for w in MONEY_WORDS):
        return "деньги и бюджеты"
    if "жалоб" in low:
        return "жалоба клиента"
    if any(w in low for w in CLIENT_OBLIGATION_WORDS):
        return "обязательства клиентам"
    if "тезис" in low or "совет директоров" in low or role == LEADERSHIP_ROLE:
        return "руководство"
    if "кп" in low or "коммерческ" in low:
        return "продажи"
    if "доступ" in low:
        return "доступы"
    if any(w in low for w in ("прогноз", "дашборд", "статус", "отчёт", "отчет")):
        return "отчёты и статусы"
    if "перенос" in low or "участие" in low:
        return "календарь"
    return "прочее"


PROTECTED_TOPIC_WORDS = (
    "стратег", "1:1", "один на один", "совет директор", "бюджет",
    "клиент", "партнёр", "партнер", "кандидат", "отпуск",
)


def _calendar_conflict(email: Email, events: list[Event]) -> Event | None:
    """Двигаем автоматически только операционные встречи.

    Если письмо само про защищённую тему (стратегия, клиент, бюджет, 1:1),
    перенос затрагивает защищённый блок и уходит на решение руководителя.
    """
    low = email.text
    if not any(word in low for word in PROTECTED_TOPIC_WORDS):
        return None
    day = email.received.date()
    protected = {"руководство", "клиент", "партнёр", "деньги", "один_на_один"}
    for event in events:
        if event.start.date() != day:
            continue
        if event.category in protected and event.hours >= 1:
            return event
    return None


def classify(
    email: Email,
    profile: Profile,
    org: list[Person],
    tasks: list[Task] | None = None,
    events: list[Event] | None = None,
) -> Classification:
    tasks = tasks or []
    events = events or []
    low = email.text
    role = email.sender_role
    risks: list[Risk] = []
    reasons: list[str] = []
    sources: list[Source] = []

    def result(action: Action, confidence: float, topic: str | None = None, assignee: str = "") -> Classification:
        return Classification(
            email_id=email.id,
            action=action,
            topic=topic or _topic(low, role),
            confidence=confidence,
            reasons=list(reasons),
            risks=list(risks),
            assignee=assignee,
            sources=list(sources),
        )

    # 1. Рассылки — не отвечаем и не тащим в сводку
    if role == NEWSLETTER_ROLE or "новости продуктов" in low or "рассылк" in low:
        reasons.append("рассылка: профиль разрешает отказ без подтверждения")
        sources += _source_auto_rule(profile, "рассыл")
        return result(Action.IGNORE, 0.99)

    # 2. Чувствительные темы: только на решение руководителя
    if role == LEADERSHIP_ROLE:
        risks.append(Risk.LEADERSHIP)
        reasons.append("письмо от вышестоящего руководителя: отвечает только руководитель")
        sources += _source_profile_rule(profile, Risk.LEADERSHIP)
        return result(Action.ASK, 0.99)

    if any(word in low for word in HR_WORDS):
        risks.append(Risk.HR)
        reasons.append("кадровый вопрос: требует подтверждения руководителя")
        sources += _source_profile_rule(profile, Risk.HR)
        return result(Action.ASK, 0.98)

    if any(word in low for word in MONEY_WORDS) or "бюджет" in email.subject.lower():
        risks.append(Risk.MONEY)
        reasons.append("деньги и бюджеты: только с подтверждением руководителя")
        sources += _source_profile_rule(profile, Risk.MONEY)
        return result(Action.ASK, 0.98)

    if any(word in low for word in CLIENT_OBLIGATION_WORDS) and (
        role == "клиент" or "клиент" in low or "обязательств" in low
    ):
        risks.append(Risk.CLIENT)
        reasons.append("внешнее обязательство клиенту: сроки и условия решает руководитель")
        sources += _source_profile_rule(profile, Risk.CLIENT)
        return result(Action.ASK, 0.97)

    if role == INVITE_ROLE and any(word in low for word in ("спикер", "конференц", "выступлен")):
        risks.append(Risk.EXTERNAL)
        reasons.append("внешнее выступление: решение за руководителем")
        return result(Action.ASK, 0.96)

    # 3. Делегирование по оргструктуре
    if any(word in low for word in ("кп", "коммерческое предложение", "коммерческ", "лиценз")):
        person = _find_person(org, "директор по продажам")
        assignee = person.name if person else ""
        reasons.append("запрос КП: передаётся директору по продажам, итог — в сводку")
        sources += _source_person(person)
        task = _related_task(tasks, ("кп", "коммерческ"))
        if task:
            sources.append(
                Source(
                    kind="task",
                    title=f"Задача {task.id}: {task.task}",
                    quote=f"срок {task.due:%d.%m.%Y}, статус «{task.status}»" if task.due else task.status,
                    ref=f"tasks.csv#{task.id}",
                )
            )
        return result(Action.DELEGATE, 0.95, "продажи", assignee)

    if any(word in low for word in ("доступ", "crm")):
        person = _find_person(org, "ассистент")
        assignee = person.name if person else ""
        reasons.append("доступ к CRM: оформляет ассистент после решения по кадрам")
        sources += _source_person(person)
        return result(Action.DELEGATE, 0.93, "доступы", assignee)

    if "жалоб" in low:
        person = _find_person(org, "клиентского сервиса")
        assignee = person.name if person else ""
        reasons.append("жалоба клиента: в клиентский сервис, пункт в планёрку")
        sources += _source_person(person)
        task = _related_task(tasks, ("жалоб",))
        if task:
            sources.append(
                Source(
                    kind="task",
                    title=f"Задача {task.id}: {task.task}",
                    quote=f"владелец {task.owner}, статус «{task.status}»",
                    ref=f"tasks.csv#{task.id}",
                )
            )
        return result(Action.DELEGATE, 0.94, "жалоба клиента", assignee)

    # 4. Календарь: регулярные внутренние встречи
    if "перенос" in low or "подтвердите участие" in low or "подтверждение участия" in low:
        conflict = _calendar_conflict(email, events)
        if conflict:
            reasons.append(
                f"перенос задевает защищённый блок «{conflict.summary}» — нужно решение руководителя"
            )
            sources.append(
                Source(
                    kind="calendar",
                    title=conflict.summary,
                    quote=f"{conflict.start:%d.%m %H:%M}–{conflict.end:%H:%M}, {conflict.category}",
                    ref=f"calendar.ics#{conflict.uid}",
                )
            )
            return result(Action.ASK, 0.9, "календарь")
        reasons.append("операционная внутренняя встреча: перенос по правилам календаря, без внешних обязательств")
        sources += _source_auto_rule(profile, "перенос")
        event = _calendar_match(email, events)
        if event:
            sources.append(
                Source(
                    kind="calendar",
                    title=event.summary,
                    quote=f"{event.start:%d.%m %H:%M}–{event.end:%H:%M}, {event.category}",
                    ref=f"calendar.ics#{event.uid}",
                )
            )
        sources += _source_person(next((p for p in org if p.name == email.from_name), None))
        return result(Action.AUTO, 0.92, "календарь")

    # 5. Отчёты и статусы — в сводку, не отвечаем
    if any(word in low for word in ("прогноз", "дашборд", "статус", "отчёт", "отчет")):
        reasons.append("информационное: в утреннюю или недельную сводку")
        return result(Action.DIGEST, 0.9)

    # 6. Безопасное правило по умолчанию
    reasons.append("нет правила для темы: безопасно передать руководителю")
    return result(Action.ASK, 0.55)
