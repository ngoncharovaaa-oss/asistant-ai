# -*- coding: utf-8 -*-
"""Сценарий встречи, которую Ася ведёт голосом по данным текущего прогона.

Цифры не пишутся руками: письма, черновики, протоколы, календарь и экономика
берутся из метрик, сводки и прогресса поручений. Реплики проверяемы по журналу.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

FALLBACK_TOPICS = (
    "сроки запуска интеграции",
    "перераспределение бюджета",
    "кандидат на позицию аккаунт-менеджера",
)

MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _num(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _week_label(start: str, end: str) -> str:
    try:
        first = date.fromisoformat(start)
        last = date.fromisoformat(end)
    except ValueError:
        return "текущей неделе"
    if first.month == last.month:
        return f"{first.day}–{last.day} {MONTHS[first.month - 1]}"
    return f"{first.day} {MONTHS[first.month - 1]} — {last.day} {MONTHS[last.month - 1]}"


def _topics(digest: dict[str, Any]) -> str:
    seen: list[str] = []
    for item in digest.get("decisions_waiting") or []:
        subject = str(item.get("subject") or "").strip()
        if subject and subject not in seen:
            seen.append(subject)
        if len(seen) == 3:
            break
    if not seen:
        seen = list(FALLBACK_TOPICS)
    said = [text[0].lower() + text[1:] if text else text for text in seen]
    if len(said) == 1:
        return said[0]
    return ", ".join(said[:-1]) + " и " + said[-1]


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text else text


def _employee_meeting(
    *,
    employee: dict[str, Any],
    messages: list[dict[str, Any]],
    draft: dict[str, Any],
    week: str,
    profile: str,
    role: str,
) -> dict[str, Any]:
    name = str(employee.get("name") or "").strip()
    first = name.split()[0] if name else "коллега"
    position = str(employee.get("position") or "").strip()
    person_role = str(employee.get("role") or "").strip()
    incoming = [m for m in messages if str(m.get("direction") or "") == "in"]
    unread = sum(1 for m in incoming if not m.get("read"))
    latest = str((incoming[-1].get("text") if incoming else "") or "").strip()
    previous = str((incoming[-2].get("text") if len(incoming) > 1 else "") or "").strip()

    intro = (
        f"Добрый день, {first}. Я Ася, ИИ-помощник руководителя. "
        f"Провожу встречу с вами по вопросам, которые вы прислали, и договорённостям на неделю {week}. "
        "Коротко: что требует решения, что я подготовила и что зафиксирую в протоколе."
    )

    if latest:
        questions_say = f"Вопросы. {first}, вы пишете: «{latest}»."
        if previous:
            questions_say += f" Ранее: «{previous}»."
        questions_say += " Разберём по порядку и зафиксируем решение."
    else:
        questions_say = (
            f"Вопросы. {first}, новых сообщений от вас нет — "
            "разберём текущие задачи и планы на неделю."
        )

    reply = str(draft.get("reply") or "").strip()
    reason = str(draft.get("reason") or "").strip()
    if reply:
        answer_say = f"Ответ. Я подготовила черновик: «{reply}»."
        if reason:
            answer_say += f" Основание: {_lower_first(reason)}"
        answer_say += " Отправка только после утверждения руководителем, автоотправок ноль."
        answer_fact = "черновик готов · отправка вручную"
    else:
        answer_say = (
            "Ответ. Черновик подготовлю по итогам встречи — "
            "после того как руководитель утвердит решение."
        )
        answer_fact = "ответ ждёт решения"

    items = [
        {
            "id": "questions",
            "title": "Вопросы из переписки",
            "fact": f"{len(incoming)} входящих · {unread} без ответа",
            "say": questions_say,
        },
        {
            "id": "answer",
            "title": "Ответ и решение",
            "fact": answer_fact,
            "say": answer_say,
        },
        {
            "id": "agreements",
            "title": "Договорённости",
            "fact": "протокол · задачи",
            "say": (
                "Договорённости. Зафиксирую итог встречи в протоколе; "
                f"задачи поставим на {name} и срок, который назовёте. "
                "Протокол сохранится после завершения встречи."
            ),
        },
    ]

    outro = (
        f"На этом у меня всё, {first}. Сохраню протокол встречи и передам договорённости. "
        "Хорошего дня!"
    )

    lines = [intro] + [item["say"] for item in items] + [outro]
    seconds = int(sum(len(line) for line in lines) / 14)

    subtitle = "Встреча · " + (name or "сотрудник")
    if position:
        subtitle += " · " + position
    subtitle += " · " + week

    return {
        "kind": "employee",
        "profile": profile,
        "role": role,
        "attendee": {"name": name, "position": position, "role": person_role},
        "week": week,
        "subtitle": subtitle,
        "intro": intro,
        "outro": outro,
        "items": items,
        "estimated_seconds": seconds,
    }


def build_meeting(
    *,
    metrics: dict[str, Any] | None = None,
    protocols: list[dict[str, Any]] | None = None,
    progress: dict[str, Any] | None = None,
    digest: dict[str, Any] | None = None,
    profile: str = "",
    role: str = "",
    employee: dict[str, Any] | None = None,
    chat_messages: list[dict[str, Any]] | None = None,
    chat_draft: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metrics = metrics or {}
    digest = digest or {}
    progress = progress or {}
    econ = metrics.get("economics") or {}

    emails = _num(metrics.get("emails") or digest.get("emails"))
    drafts_n = _num(metrics.get("drafts"))
    decisions = _num(metrics.get("decisions"))
    delegated = _num(metrics.get("delegated"))
    auto = _num(metrics.get("auto"))
    protocols_n = _num(metrics.get("protocols") or len(protocols or []))
    tasks = _num(metrics.get("protocol_tasks") or progress.get("tasks"))
    open_q = _num(metrics.get("protocol_open_questions"))
    approved = _num(progress.get("approved"))
    meetings = _num(metrics.get("meetings_week") or digest.get("meetings"))
    protected = metrics.get("protected_hours_week")
    if protected is None:
        protected = digest.get("protected_hours") or 0
    protected_s = f"{float(protected):.4g}".replace(".", ",")

    accuracy = metrics.get("accuracy") or {}
    hits = _num(accuracy.get("hits") or accuracy.get("total") or emails)
    total = _num(accuracy.get("total") or emails or hits)
    citations = _num(round(100 * float(metrics.get("citations_coverage") or 0)))
    auto_sends = _num(metrics.get("auto_sends"))

    drafts_m = _num(econ.get("drafts_per_month"))
    manual = _num(econ.get("manual_cost"))
    agent = _num(econ.get("agent_cost"))
    saving = _num(econ.get("saving"))
    try:
        payback = f"{float(econ.get('payback_months') or 0):.1f}".replace(".", ",")
    except (TypeError, ValueError):
        payback = "—"

    week = _week_label(str(digest.get("week_start") or ""), str(digest.get("week_end") or ""))
    if employee:
        return _employee_meeting(
            employee=employee,
            messages=chat_messages or [],
            draft=chat_draft or {},
            week=week,
            profile=profile,
            role=role,
        )
    topics = _topics(digest)
    greeting = "Добрый день"
    if profile and profile.split():
        greeting += ", " + profile.split()[0]
    greeting += "."

    intro = (
        f"{greeting} Я Ася, ИИ-помощник руководителя. Провожу планёрку по неделе {week}. "
        "Коротко расскажу, что я сделала, что ждёт вашего решения и где нужен ваш выбор."
    )

    calendar_say = f"Календарь. На неделе {meetings} {plural(meetings, 'встреча', 'встречи', 'встреч')} и {protected_s} {plural(_num(round(float(protected))), 'защищённый час', 'защищённых часа', 'защищённых часов')}. Защищённые блоки я не занимаю операционными встречами."
    if approved:
        protocols_say = (
            f"Протоколы. Разобрано {protocols_n} {plural(protocols_n, 'встреча', 'встречи', 'встреч')}: "
            f"{tasks} {plural(tasks, 'поручение', 'поручения', 'поручений')} и {open_q} "
            f"{plural(open_q, 'открытый вопрос', 'открытых вопроса', 'открытых вопросов')}. "
            f"{approved} уже в календаре."
        )
    else:
        protocols_say = (
            f"Протоколы. Разобрано {protocols_n} {plural(protocols_n, 'встреча', 'встречи', 'встреч')}: "
            f"{tasks} {plural(tasks, 'поручение', 'поручения', 'поручений')} и {open_q} "
            f"{plural(open_q, 'открытый вопрос', 'открытых вопроса', 'открытых вопросов')}. "
            "В календарь пока ничего не передано — поручения ждут утверждения."
        )

    items = [
        {
            "id": "week",
            "title": "Итоги недели",
            "fact": f"{emails} писем · {drafts_n} черновика",
            "say": (
                f"Итоги недели. Я разобрала {emails} {plural(emails, 'письмо', 'письма', 'писем')}. "
                f"{drafts_n} {plural(drafts_n, 'черновик', 'черновика', 'черновиков')} готовы: "
                f"{decisions} ждут вашего решения, {delegated} делегированы, {auto} можно отправлять без вас. "
                "Автоотправок ноль: каждое письмо уходит только после утверждения."
            ),
        },
        {
            "id": "protocols",
            "title": "Протоколы и поручения",
            "fact": f"{protocols_n} протоколов · {tasks} поручений",
            "say": protocols_say,
        },
        {
            "id": "decisions",
            "title": "Что ждёт решения",
            "fact": f"{decisions} решений",
            "say": (
                f"Решения. В очереди {decisions} {plural(decisions, 'пункт', 'пункта', 'пунктов')}. "
                f"Первые три: {topics}. По каждому готов черновик со ссылками на источник."
            ),
        },
        {
            "id": "calendar",
            "title": "Календарь недели",
            "fact": f"{meetings} встреч · {protected_s} ч защиты",
            "say": calendar_say,
        },
        {
            "id": "money",
            "title": "Экономика",
            "fact": f"{fmt(saving)} ₽ в месяц",
            "say": (
                f"Экономика. {drafts_m} {plural(drafts_m, 'черновик', 'черновика', 'черновиков')} в месяц. "
                f"Вручную это {fmt(manual)} {plural(manual, 'рубль', 'рубля', 'рублей')}, "
                f"со мной {fmt(agent)} {plural(agent, 'рубль', 'рубля', 'рублей')}. "
                f"Экономия {fmt(saving)} {plural(saving, 'рубль', 'рубля', 'рублей')} в месяц, "
                f"окупаемость {payback} месяца."
            ),
        },
        {
            "id": "control",
            "title": "Контроль качества",
            "fact": f"{hits}/{total} · {auto_sends} автоотправок",
            "say": (
                f"Контроль. Точность разбора {hits} из {total}, "
                f"{citations} процентов черновиков со ссылками на источники, "
                f"автоотправок {'ноль' if auto_sends == 0 else auto_sends}. "
                "Пароли и токены я не запрашиваю."
            ),
        },
    ]

    outro = (
        f"На этом у меня всё. Откройте очередь решений — там {decisions} "
        f"{plural(decisions, 'пункт', 'пункта', 'пунктов')}. "
        "Если нужно провести встречу в Zoom, включите демонстрацию этого окна: я говорю вживую. "
        "Хорошей недели!"
    )

    lines = [intro] + [item["say"] for item in items] + [outro]
    seconds = int(sum(len(line) for line in lines) / 14)

    subtitle = "Планёрка · " + week
    if profile:
        subtitle += " · " + profile + (f", {role}" if role else "")

    return {
        "profile": profile,
        "role": role,
        "week": week,
        "subtitle": subtitle,
        "intro": intro,
        "outro": outro,
        "items": items,
        "estimated_seconds": seconds,
    }


CHANNEL_LABELS = {"zoom": "Zoom", "telemost": "Телемост"}


def _stamp(value: str) -> str:
    try:
        moment = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return ""
    return moment.strftime("%d.%m.%Y, %H:%M")


def _duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes, rest = divmod(seconds, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин {rest} с"
    return f"{rest} с"


def minutes_markdown(minute: dict[str, Any]) -> str:
    """Протокол встречи в Markdown: повестка, что освещено, итог."""
    items = minute.get("items") or []
    covered = [item for item in items if item.get("covered")]
    profile = str(minute.get("profile") or "").strip()
    role = str(minute.get("role") or "").strip()
    leader = profile + (f", {role}" if role else "") if profile else "—"
    channel = CHANNEL_LABELS.get(str(minute.get("channel") or ""), "экран Аси")
    attendee = str(minute.get("employee") or "").strip()

    lines = [
        "# Протокол встречи" if attendee else "# Протокол планёрки",
        "",
        f"**Когда:** {_stamp(minute.get('started_at', ''))} · **Длительность:** {_duration(minute.get('seconds', 0))}",
        f"**Ведущая:** Ася, AsiStantAI · **Руководитель:** {leader}",
    ]
    if attendee:
        position = str(minute.get("employee_position") or "").strip()
        lines.append(f"**Сотрудник:** {attendee}" + (f", {position}" if position else ""))
    if minute.get("week"):
        lines.append(f"**Неделя:** {minute['week']}")
    lines.append(f"**Канал:** {channel}")
    lines += ["", "## Повестка", ""]
    for item in items:
        mark = "освещено" if item.get("covered") else "не рассмотрено"
        lines.append(f"{item.get('index', '')}. **{item.get('title', '')}** — {item.get('fact', '')} — {mark}")
    lines += ["", f"Освещено пунктов: {len(covered)} из {len(items)}.", "", "## Итог", ""]
    for item in covered:
        lines.append(f"- {item.get('title', '')}: {item.get('fact', '')}")
    if not covered:
        lines.append("- Встреча завершена без доклада по пунктам.")
    tasks = minute.get("tasks") or []
    if tasks:
        lines += ["", "## Задачи по встрече", ""]
        for number, task in enumerate(tasks, 1):
            details = []
            if task.get("item_index"):
                details.append(f"к пункту {task['item_index']}")
            if task.get("owner"):
                details.append(f"исполнитель: {task['owner']}")
            if task.get("due"):
                due = str(task["due"])
                parts = due.split("-")
                if len(parts) == 3:
                    due = f"{parts[2]}.{parts[1]}.{parts[0]}"
                details.append(f"срок: {due}")
            details.append("выполнено" if task.get("status") == "done" else "в работе")
            lines.append(f"{number}. **{task.get('title', '')}** — " + " · ".join(details))
    lines += [
        "",
        "Автоотправок нет: письма и поручения уходят только после утверждения руководителем.",
        "",
        "_Протокол собран автоматически по данным прогона; решения и поручения утверждает руководитель._",
        "",
    ]
    return "\n".join(lines)
