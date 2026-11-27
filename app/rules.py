# -*- coding: utf-8 -*-
"""Правила агента, выведенные из файлов, а не зашитые списком слов.

Профиль руководителя даёт запретные темы, разрешённые автодействия и ритм дня;
оргструктура — кому делегировать; календарь — что защищать; tasks.csv — где
сроки уже нарушены или статус расходится с датой. Модуль ничего не отправляет:
он объясняет решение и готовит план, отправка остаётся за человеком.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .datasets import load_calendar, load_inbox, load_org, load_tasks
from .models import Action, Email, Event, Person, Risk, Source, Task
from .profile import Profile, load_profile

INTERNAL_ROLES = frozenset({"подчинённый", "коллега", "руководитель руководителя"})
NEWSLETTER = "рассылка"
INVITE = "приглашение"
BOSS = "руководитель руководителя"
SENSITIVE_CATEGORIES = frozenset({"деньги", "кадры", "обязательства_клиентам", "руководство"})
PROTECTED_EVENT_CATEGORIES = frozenset({"клиент", "партнёр", "деньги", "руководство", "один_на_один"})

WORK_START = time(9, 0)
WORK_END = time(18, 0)
BLOCK = timedelta(minutes=30)
WEEKDAY_WORDS = (
    ("понед", 0), ("вторн", 1), ("сред", 2), ("четвер", 3),
    ("пятниц", 4), ("суббот", 5), ("воскрес", 6),
)
WEEKDAY_NAMES = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")

_TOKENS = re.compile(r"[a-zа-яё0-9-]+")
_DATES = re.compile(r"\b(\d{1,2})\.(\d{1,2})\b")
_STOP = {"и", "в", "на", "с", "по", "для", "или", "а", "но", "не", "как", "что", "это"}
# Служебные глаголы, которые есть почти в каждой задаче и не связывают её с письмом.
_TASK_STOP_STEMS = {"подго", "разоб", "обнов", "запус", "подпи", "онбор", "согла"}


def _tokens(text: str) -> list[str]:
    return [token for token in _TOKENS.findall(text.lower()) if token not in _STOP]


def _stem(token: str) -> str:
    return token[:5]


def _stems(text: str) -> set[str]:
    return {_stem(token) for token in _tokens(text) if len(token) >= 4}


def _clean(text: str) -> str:
    return text.strip().rstrip(".;:").strip()


def _split_example(bullet: str) -> tuple[str, str]:
    match = re.match(r"^(.*?)\s*\((.*)\)\s*[;.]?$", bullet)
    return (_clean(match.group(1)), match.group(2)) if match else (_clean(bullet), "")


@dataclass(frozen=True)
class ForbiddenTopic:
    risk: Risk
    name: str
    keywords: tuple[str, ...]
    scope: str
    source: Source


@dataclass(frozen=True)
class AutoRule:
    kind: str
    text: str
    source: Source


@dataclass
class RuleSet:
    profile: Profile
    goals: list[str]
    focus_min: float
    focus_max: float
    focus_areas: list[str]
    forbidden: list[ForbiddenTopic]
    auto: list[AutoRule]
    plan_time: time
    review_weekday: int
    review_time: time
    channels: list[str]
    warnings: list[str] = field(default_factory=list)

    def auto_source(self, kind: str) -> Source | None:
        for rule in self.auto:
            if rule.kind == kind:
                return rule.source
        return None

    def forbidden_for(self, risk: Risk) -> ForbiddenTopic | None:
        return next((topic for topic in self.forbidden if topic.risk == risk), None)


def _scope(risk: Risk) -> str:
    if risk in (Risk.MONEY, Risk.HR):
        return "internal"
    if risk is Risk.CLIENT:
        return "client"
    return "leadership"


def _keywords(risk: Risk, head: str, examples: str) -> set[str]:
    stems = {_stem(word) for word in _tokens(head) + _tokens(examples) if len(word) >= 4}
    if risk is Risk.CLIENT:
        # «клиент» сам по себе — ещё не обязательство: жалобы и статусы идут в сервис.
        stems.discard("клиен")
    return stems


def _rhythm(text: str, warnings: list[str]) -> tuple[time, int, time]:
    line = next((raw for raw in text.splitlines() if "ритм" in raw.lower()), "")
    if not line:
        warnings.append("в профиле нет строки «Ритм» — взяты 08:30 и пятница 17:00")
        return time(8, 30), 4, time(17, 0)
    times = re.findall(r"(\d{1,2}):(\d{2})", line)
    if len(times) < 2:
        warnings.append("в ритме меньше двух времён — взяты 08:30 и 17:00")
        times = times + [("8", "30"), ("17", "0")][: 2 - len(times)]
    plan = time(int(times[0][0]), int(times[0][1]))
    review = time(int(times[1][0]), int(times[1][1]))
    weekday = 4 if "пятниц" in line.lower() else 0
    return plan, weekday, review


def load_rules(profile: Profile) -> RuleSet:
    text = profile.source_path.read_text(encoding="utf-8")
    warnings: list[str] = []

    forbidden: list[ForbiddenTopic] = []
    for rule in profile.sensitive_rules:
        if rule.risk is None or rule.risk is Risk.EXTERNAL:
            continue
        head, examples = _split_example(rule.text)
        forbidden.append(
            ForbiddenTopic(
                risk=rule.risk,
                name=head,
                keywords=tuple(sorted(_keywords(rule.risk, head, examples))),
                scope=_scope(rule.risk),
                source=rule.source,
            )
        )
    if len(forbidden) < 4:
        warnings.append(f"распознано запретных тем: {len(forbidden)} из 4")

    auto: list[AutoRule] = []
    seen_kinds: set[str] = set()
    patterns = (
        ("calendar", ("перенос", "встреч")),
        ("news", ("рассыл",)),
        ("invites", ("приглашен",)),
        ("delegate", ("передач",)),
        ("digest", ("отчёт", "сводк")),
        ("digest", ("отчет", "сводк")),
    )
    for bullet in profile.auto_rules:
        low = bullet.lower()
        for kind, needles in patterns:
            if kind in seen_kinds or not all(needle in low for needle in needles):
                continue
            seen_kinds.add(kind)
            auto.append(
                AutoRule(
                    kind=kind,
                    text=_clean(bullet),
                    source=Source(
                        kind="profile",
                        title="Можно без подтверждения",
                        quote=bullet,
                        ref=f"{profile.source_path.name}#auto",
                    ),
                )
            )
    for kind in ("calendar", "news", "delegate", "digest", "invites"):
        if kind not in seen_kinds:
            warnings.append(f"не найдено разрешение на действие «{kind}»")

    plan_time, review_weekday, review_time = _rhythm(text, warnings)
    hours = re.search(r"(\d+)\s*[–—−-]\s*(\d+)\s*час", text)
    focus_min, focus_max = (float(hours.group(1)), float(hours.group(2))) if hours else (8.0, 12.0)
    if not hours:
        warnings.append("не найдена цель по часам — взято 8–12 ч")

    focus_areas = [_clean(item) for item in profile.protected if _clean(item)]
    channels = [_clean(item) for item in profile.channels if _clean(item)]

    return RuleSet(
        profile=profile,
        goals=list(profile.goals),
        focus_min=focus_min,
        focus_max=focus_max,
        focus_areas=focus_areas,
        forbidden=forbidden,
        auto=auto,
        plan_time=plan_time,
        review_weekday=review_weekday,
        review_time=review_time,
        channels=channels,
        warnings=warnings,
    )


@dataclass
class Thread:
    key: tuple[str, str]
    emails: list[Email] = field(default_factory=list)

    @property
    def first(self) -> Email:
        return self.emails[0]

    @property
    def count(self) -> int:
        return len(self.emails)

    @property
    def subject(self) -> str:
        return self.first.subject

    @property
    def from_name(self) -> str:
        return self.first.from_name


def build_threads(emails: list[Email]) -> list[Thread]:
    groups: dict[tuple[str, str], Thread] = {}
    for email in sorted(emails, key=lambda item: (item.received, item.id)):
        subject = re.sub(r"\s+", " ", email.subject.strip().lower())
        key = (email.from_email.lower(), subject)
        groups.setdefault(key, Thread(key=key)).emails.append(email)
    return list(groups.values())


@dataclass
class Decision:
    thread: Thread
    action: Action
    topic: str
    reason: str
    target: Person | None = None
    citations: list[Source] = field(default_factory=list)
    gates: list[str] = field(default_factory=list)
    side: list[str] = field(default_factory=list)
    risk: Risk | None = None
    move: dict | None = None


def _find_person(org: list[Person], *needles: str) -> Person | None:
    for person in org:
        haystack = f"{person.position} {person.name}".lower()
        if all(needle in haystack for needle in needles):
            return person
    return None


def _person_source(person: Person | None) -> list[Source]:
    if person is None:
        return []
    return [
        Source(
            kind="org",
            title=f"{person.name} — {person.position}",
            quote=f"роль для руководителя: {person.role}",
            ref=f"org_structure.csv#{person.id}",
        )
    ]


def _rule_source(ruleset: RuleSet, risk: Risk) -> list[Source]:
    rule = ruleset.profile.rule_for(risk)
    return [rule.source] if rule else []


def _auto_source(ruleset: RuleSet, kind: str) -> list[Source]:
    source = ruleset.auto_source(kind)
    return [source] if source else []


def _forbidden_match(ruleset: RuleSet, email: Email) -> ForbiddenTopic | None:
    low = email.text
    for topic in ruleset.forbidden:
        if topic.scope == "leadership":
            if email.sender_role == BOSS:
                return topic
            if email.sender_role in INTERNAL_ROLES and "письм" in low and "руковод" in low:
                return topic
            continue
        if topic.scope == "client":
            if email.sender_role == "клиент" and any(word in low for word in topic.keywords):
                return topic
            if "обязательств" in low:
                return topic
            continue
        if email.sender_role not in INTERNAL_ROLES:
            continue
        if any(word in low for word in topic.keywords):
            return topic
    return None


def _is_newsletter(email: Email) -> bool:
    return email.sender_role == NEWSLETTER or "рассылк" in email.text


def _is_calendar_request(email: Email, ruleset: RuleSet) -> bool:
    low = email.text
    if email.sender_role not in INTERNAL_ROLES:
        return False
    if "перенос" in low:
        return True
    return "подтверд" in low and "планёрк" in low


def _is_informational(email: Email) -> bool:
    return email.sender_role == "подчинённый" and any(
        word in email.text for word in ("прогноз", "дашборд", "статус", "отчёт", "отчет")
    )


def _event_copy(event: Event) -> Event:
    return Event(
        uid=event.uid,
        start=event.start,
        end=event.end,
        summary=event.summary,
        category=event.category,
        attendees=list(event.attendees),
    )


def _rhythm_busy(ruleset: RuleSet, day: date) -> list[tuple[datetime, datetime, str]]:
    blocks: list[tuple[datetime, datetime, str]] = []
    if day.weekday() < 5:
        start = datetime.combine(day, ruleset.plan_time)
        blocks.append((start, start + BLOCK, "План дня"))
    if day.weekday() == ruleset.review_weekday:
        start = datetime.combine(day, ruleset.review_time)
        blocks.append((start, start + BLOCK, "Итог недели"))
    return blocks


def _overlaps(start: datetime, end: datetime, other_start: datetime, other_end: datetime) -> bool:
    return start < other_end and other_start < end


def _find_event(events: list[Event], email: Email) -> Event | None:
    words = _stems(email.text)
    best: Event | None = None
    best_score = 0
    for event in events:
        if abs((event.start.date() - email.received.date()).days) > 7:
            continue
        score = len(words & _stems(event.summary))
        if score > best_score:
            best, best_score = event, score
    return best


def _parse_move(email: Email) -> tuple[int | None, time | None]:
    low = email.text
    match = re.search(r"на\s+(понед\w*|вторн\w*|сред\w*|четвер\w*|пятниц\w*|суббот\w*|воскрес\w*)", low)
    weekday = None
    if match:
        word = match.group(1)
        weekday = next(index for prefix, index in WEEKDAY_WORDS if word.startswith(prefix))
    at = re.search(r"(\d{1,2}):(\d{2})", low)
    slot = time(int(at.group(1)), int(at.group(2))) if at else None
    if slot and (slot.hour > 23 or slot.minute > 59):
        slot = None
    return weekday, slot


def _try_move(
    email: Email, events: list[Event], ruleset: RuleSet
) -> tuple[dict, str | None]:
    """Возвращает изменение календаря и причину отказа (если перенос невозможен)."""
    weekday, slot = _parse_move(email)
    event = _find_event(events, email)
    if event is None:
        return {}, "встреча не найдена в календаре"
    if weekday is None or slot is None:
        return {}, "в письме не указан новый день и время"
    if event.category in PROTECTED_EVENT_CATEGORIES:
        return {}, f"встреча «{event.summary}» защищённой категории «{event.category}»"
    days = (weekday - email.received.weekday()) % 7 or 7
    target = email.received.date() + timedelta(days=days)
    new_start = datetime.combine(target, slot)
    new_end = new_start + (event.end - event.start)
    for start, end, title in _rhythm_busy(ruleset, target):
        if _overlaps(new_start, new_end, start, end):
            return {}, f"слот пересекает защищённый блок «{title}»"
    busy = [
        other.summary
        for other in events
        if other is not event and _overlaps(new_start, new_end, other.start, other.end)
    ]
    if busy:
        return {}, f"слот занят: {busy[0]}"
    change = {
        "event": event.summary,
        "event_uid": event.uid,
        "from": f"{event.start:%d.%m %H:%M}–{event.end:%H:%M}",
        "to": f"{new_start:%d.%m %H:%M}–{new_end:%H:%M}",
        "status": "applied",
        "email_id": email.id,
    }
    event.start, event.end = new_start, new_end
    return change, None


def _next_planerka(events: list[Event], after: datetime) -> Event | None:
    candidates = [
        event for event in events
        if "планёрк" in event.summary.lower() and event.start > after
    ]
    return min(candidates, key=lambda event: event.start) if candidates else None


def _access_gate(thread: Thread, threads: list[Thread]) -> tuple[list[str], list[Source]]:
    own = _stems(thread.first.text)
    for other in threads:
        if other is thread:
            continue
        category = other.first.category
        if category not in ("кадры", "руководство") and "оффер" not in other.first.text:
            continue
        shared = (own & _stems(other.first.text)) - {"руков", "подтв", "досту", "нужен", "нужно"}
        if shared:
            order = other.first.id
            return (
                [f"доступ выдаётся после решения по кадрам (письмо {order}: {other.subject})"],
                [
                    Source(
                        kind="email",
                        title=other.subject,
                        quote=other.first.body,
                        ref=f"inbox.csv#{order}",
                    )
                ],
            )
    return [], []


def decide_thread(thread: Thread, ruleset: RuleSet, org: list[Person], threads: list[Thread],
                  events: list[Event]) -> Decision:
    email = thread.first
    low = email.text
    topic_default = "прочее"

    if _is_newsletter(email):
        return Decision(
            thread, Action.IGNORE, "рассылка",
            "рассылка: профиль разрешает отказ без подтверждения",
            citations=_auto_source(ruleset, "news"),
        )

    forbidden = _forbidden_match(ruleset, email)
    if forbidden is not None:
        reason = f"профиль: «{forbidden.name}» — только с подтверждением руководителя"
        if forbidden.scope == "internal":
            reason = f"профиль: «{forbidden.name}» для внутренних писем — только с подтверждением"
        return Decision(
            thread, Action.ASK, forbidden.name, reason,
            risk=forbidden.risk, citations=[forbidden.source],
        )

    if email.sender_role == INVITE:
        if any(word in low for word in ("спикер", "выступ", "конференц")):
            return Decision(
                thread, Action.ASK, "внешнее выступление",
                "внешнее выступление — не «неприоритетное приглашение», решает руководитель",
                citations=_auto_source(ruleset, "invites"),
            )
        return Decision(
            thread, Action.AUTO, "приглашение",
            "неприоритетное приглашение: отказ без подтверждения",
            citations=_auto_source(ruleset, "invites"),
        )

    if email.sender_role in INTERNAL_ROLES and "доступ" in low and ("crm" in low or "систем" in low):
        person = _find_person(org, "ассистент")
        gates, gate_sources = _access_gate(thread, threads)
        return Decision(
            thread, Action.DELEGATE, "доступы",
            "запрос доступа: оформляет ассистент, без отдельного решения руководителя",
            target=person, citations=_person_source(person) + gate_sources,
            gates=gates,
        )

    if email.sender_role == "клиент" and ("коммерческ" in low or re.search(r"\bкп\b", low)):
        person = _find_person(org, "продаж")
        return Decision(
            thread, Action.DELEGATE, "продажи",
            "маршрут профиля: КП → директор по продажам, итог — в сводку",
            target=person, citations=_person_source(person) + _auto_source(ruleset, "delegate"),
        )

    if "жалоб" in low:
        person = _find_person(org, "клиентского сервиса")
        side = ["пункт в повестку ближайшей планёрки"]
        planerka = _next_planerka(events, email.received)
        if planerka is not None:
            side = [f"пункт в повестку планёрки {planerka.start:%d.%m %H:%M}"]
        return Decision(
            thread, Action.DELEGATE, "жалоба клиента",
            "маршрут профиля: жалоба → клиентский сервис, разбор — на планёрке",
            target=person, citations=_person_source(person) + _auto_source(ruleset, "delegate"),
            side=side,
        )

    if _is_calendar_request(email, ruleset):
        change, refusal = _try_move(email, events, ruleset)
        if refusal and "не указан новый" not in refusal:
            return Decision(
                thread, Action.ASK, "календарь",
                f"перенос не выполнен автоматически: {refusal}",
                side=[refusal],
            )
        if change:
            side = [f"{change['event']}: {change['from']} → {change['to']}"]
            if "подтверд" in low:
                side = ["подтверждение участия отправлено (внутренняя встреча)"]
            return Decision(
                thread, Action.AUTO, "календарь",
                "внутренняя регулярная встреча: перенос по правилам календаря",
                citations=_auto_source(ruleset, "calendar"), side=side, move=change,
            )
        planerka = _next_planerka(events, email.received)
        side = [f"подтверждена планёрка {planerka.start:%d.%m %H:%M}"] if planerka else []
        return Decision(
            thread, Action.AUTO, "календарь",
            "внутренняя регулярная встреча: подтверждение по правилам календаря",
            citations=_auto_source(ruleset, "calendar"), side=side,
        )

    if _is_informational(email):
        return Decision(
            thread, Action.DIGEST, "отчёты и статусы",
            "отчёт или статус: не отвечаем, включаем в утреннюю или недельную сводку",
            citations=_auto_source(ruleset, "digest"),
        )

    return Decision(
        thread, Action.ASK, topic_default,
        "нет правила для темы — безопасно передать руководителю",
    )


def _merge(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _busy(ruleset: RuleSet, events: list[Event], day: date) -> list[tuple[datetime, datetime]]:
    intervals = [(event.start, event.end) for event in events if event.start.date() == day]
    intervals += [(start, end) for start, end, _ in _rhythm_busy(ruleset, day)]
    return _merge(intervals)


def _free_windows(ruleset: RuleSet, events: list[Event], day: date) -> list[tuple[datetime, datetime]]:
    start = datetime.combine(day, WORK_START)
    end = datetime.combine(day, WORK_END)
    windows: list[tuple[datetime, datetime]] = []
    cursor = start
    for busy_start, busy_end in _busy(ruleset, events, day):
        if busy_start > cursor:
            windows.append((cursor, min(busy_start, end)))
        cursor = max(cursor, busy_end)
    if cursor < end:
        windows.append((cursor, end))
    return [(left, right) for left, right in windows if right > left]


def _focus_blocks(ruleset: RuleSet, events: list[Event], start: date, end: date) -> list[dict]:
    per_week: Counter = Counter()
    blocks: list[dict] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            week = (day - timedelta(days=day.weekday())).isoformat()
            room = ruleset.focus_max - per_week[week]
            windows = _free_windows(ruleset, events, day)
            if windows and room >= 1:
                left, right = max(windows, key=lambda pair: pair[1] - pair[0])
                minutes = min(120.0, (right - left).total_seconds() / 60, room * 60)
                if minutes >= 60:
                    finish = left + timedelta(minutes=minutes)
                    per_week[week] += minutes / 60
                    blocks.append(
                        {
                            "date": day.isoformat(),
                            "start": left.strftime("%H:%M"),
                            "end": finish.strftime("%H:%M"),
                            "hours": round(minutes / 60, 2),
                            "title": "Фокус: " + (ruleset.focus_areas[0] if ruleset.focus_areas else "стратегия"),
                        }
                    )
        day += timedelta(days=1)
    return blocks


def _protected_blocks(ruleset: RuleSet, events: list[Event], start: date, end: date,
                      focus: list[dict]) -> list[dict]:
    blocks: list[dict] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            blocks.append(
                {
                    "date": day.isoformat(),
                    "start": ruleset.plan_time.strftime("%H:%M"),
                    "end": (datetime.combine(day, ruleset.plan_time) + BLOCK).strftime("%H:%M"),
                    "kind": "ритм",
                    "title": "План дня",
                }
            )
        if day.weekday() == ruleset.review_weekday:
            blocks.append(
                {
                    "date": day.isoformat(),
                    "start": ruleset.review_time.strftime("%H:%M"),
                    "end": (datetime.combine(day, ruleset.review_time) + BLOCK).strftime("%H:%M"),
                    "kind": "ритм",
                    "title": "Итог недели",
                }
            )
        day += timedelta(days=1)
    for event in events:
        if start <= event.start.date() <= end and event.category == "один_на_один":
            blocks.append(
                {
                    "date": event.start.date().isoformat(),
                    "start": event.start.strftime("%H:%M"),
                    "end": event.end.strftime("%H:%M"),
                    "kind": "один_на_один",
                    "title": event.summary,
                }
            )
    for block in focus:
        blocks.append(
            {
                "date": block["date"],
                "start": block["start"],
                "end": block["end"],
                "kind": "фокус",
                "title": block["title"],
            }
        )
    return sorted(blocks, key=lambda item: (item["date"], item["start"]))


def _rhythm_conflicts(ruleset: RuleSet, events: list[Event]) -> list[dict]:
    conflicts: list[dict] = []
    for event in events:
        low = event.summary.lower()
        if "итог" not in low:
            continue
        if event.start.time() != ruleset.review_time or event.start.weekday() != ruleset.review_weekday:
            conflicts.append(
                {
                    "event": event.summary,
                    "date": event.start.strftime("%d.%m"),
                    "calendar_time": event.start.strftime("%H:%M"),
                    "profile_time": f"{WEEKDAY_NAMES[ruleset.review_weekday]} {ruleset.review_time:%H:%M}",
                    "suggestion": "сдвинуть к ритму профиля или подтвердить исключение",
                }
            )
    return conflicts


def _link_task(task: Task, emails: list[Email]) -> tuple[Email | None, list[str]]:
    words = _stems(task.task) - _TASK_STOP_STEMS
    best: Email | None = None
    best_score = 0
    for email in emails:
        score = len(words & _stems(f"{email.subject} {email.body}"))
        if score > best_score:
            best, best_score = email, score
    if best is None or best_score == 0 or task.due is None:
        return best, []
    dates: list[date] = []
    for day, month in _DATES.findall(best.body + "\n" + best.subject):
        try:
            parsed = date(2026, int(month), int(day))
        except ValueError:
            continue
        dates.append(parsed)
    earlier = [parsed for parsed in dates if parsed < task.due.date()]
    return best, [parsed.strftime("%d.%m") for parsed in earlier]


def _task_audit(tasks: list[Task], emails: list[Email], as_of: date) -> dict:
    items: list[dict] = []
    mismatches: list[dict] = []
    conflicts: list[dict] = []
    overdue: list[str] = []
    for task in tasks:
        due = task.due.date() if task.due else None
        computed = bool(due and due < as_of and task.status != "готово")
        stated = task.status.strip().lower() == "просрочена"
        if computed:
            overdue.append(task.id)
        if computed != stated:
            kind = (
                "статус «просрочена», но срок ещё не наступил"
                if stated
                else "срок прошёл, а статус не «просрочена»"
            )
            mismatches.append(
                {
                    "id": task.id,
                    "task": task.task,
                    "kind": kind,
                    "status": task.status,
                    "due": due.strftime("%d.%m.%Y") if due else "",
                }
            )
        linked, earlier = _link_task(task, emails)
        for when in earlier:
            conflicts.append(
                {
                    "task_id": task.id,
                    "task": task.task,
                    "due": due.strftime("%d.%m.%Y") if due else "",
                    "email_id": linked.id if linked else "",
                    "subject": linked.subject if linked else "",
                    "email_date": when,
                    "note": "в письме срок раньше, чем в задачах",
                }
            )
        items.append(
            {
                "id": task.id,
                "task": task.task,
                "owner": task.owner,
                "due": due.isoformat() if due else "",
                "status": task.status,
                "computed_overdue": computed,
                "linked_email": linked.id if linked else "",
            }
        )
    return {
        "as_of": as_of.isoformat(),
        "items": items,
        "overdue": overdue,
        "status_mismatches": mismatches,
        "deadline_conflicts": conflicts,
    }


def _evaluate(decisions: list[Decision]) -> dict:
    hits_threads = 0
    total_threads = 0
    hits_emails = 0
    total_emails = 0
    misses: list[dict] = []
    sensitive_auto: list[dict] = []
    for decision in decisions:
        thread = decision.thread
        expected = thread.first.expected_action
        if expected:
            total_threads += 1
            total_emails += thread.count
            if decision.action.value == expected:
                hits_threads += 1
                hits_emails += thread.count
            else:
                misses.append(
                    {
                        "email_id": thread.first.id,
                        "subject": thread.subject,
                        "want": expected,
                        "got": decision.action.value,
                        "count": thread.count,
                    }
                )
        for email in thread.emails:
            if email.category in SENSITIVE_CATEGORIES and decision.action != Action.ASK:
                sensitive_auto.append(
                    {"email_id": email.id, "category": email.category, "action": decision.action.value}
                )
    return {
        "threads_total": total_threads,
        "threads_hit": hits_threads,
        "thread_accuracy": round(hits_threads / total_threads, 3) if total_threads else 0.0,
        "emails_total": total_emails,
        "emails_hit": hits_emails,
        "email_accuracy": round(hits_emails / total_emails, 3) if total_emails else 0.0,
        "misses": misses,
        "sensitive_auto": sensitive_auto,
    }


def _next_business_day(day: date) -> date:
    candidate = day + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _plan(decisions: list[Decision], tasks: list[Task], events: list[Event],
          ruleset: RuleSet, as_of: date) -> dict:
    day = _next_business_day(as_of)
    day_events = sorted(
        (event for event in events if event.start.date() == day), key=lambda event: event.start
    )
    approvals = [
        {
            "email_id": decision.thread.first.id,
            "subject": decision.thread.subject,
            "topic": decision.topic,
            "count": decision.thread.count,
        }
        for decision in decisions if decision.action == Action.ASK
    ][:5]
    delegated = [
        {
            "email_id": decision.thread.first.id,
            "subject": decision.thread.subject,
            "assignee": decision.target.name if decision.target else "",
        }
        for decision in decisions if decision.action == Action.DELEGATE
    ]
    overdue = [
        {
            "id": task.id,
            "task": task.task,
            "owner": task.owner,
            "days": (day - task.due.date()).days,
        }
        for task in tasks
        if task.due and task.due.date() < day and task.status != "готово"
    ]
    plan_start = datetime.combine(day, ruleset.plan_time)
    plan_end = plan_start + BLOCK
    return {
        "date": day.isoformat(),
        "weekday": WEEKDAY_NAMES[day.weekday()],
        "plan_block": f"{plan_start:%H:%M}–{plan_end:%H:%M}",
        "events": [
            {
                "summary": event.summary,
                "start": event.start.strftime("%H:%M"),
                "end": event.end.strftime("%H:%M"),
                "category": event.category,
            }
            for event in day_events
        ],
        "approvals": approvals,
        "delegated": delegated,
        "overdue": overdue,
    }


def _decision_dict(decision: Decision) -> dict:
    first = decision.thread.first
    return {
        "email_id": first.id,
        "ids": [email.id for email in decision.thread.emails],
        "count": decision.thread.count,
        "sender": first.from_name,
        "role": first.sender_role,
        "subject": first.subject,
        "action": decision.action.value,
        "expected": first.expected_action,
        "topic": decision.topic,
        "risk": decision.risk.value if decision.risk else "",
        "target": decision.target.name if decision.target else "",
        "reason": decision.reason,
        "gates": decision.gates,
        "side": decision.side,
        "citations": [source.as_dict() for source in decision.citations],
    }


def audit(profile: Profile, emails: list[Email], org: list[Person],
          tasks: list[Task], events: list[Event]) -> dict:
    ruleset = load_rules(profile)
    threads = build_threads(emails)
    calendar = [_event_copy(event) for event in events]
    decisions: list[Decision] = []
    changes: list[dict] = []
    for thread in threads:
        decision = decide_thread(thread, ruleset, org, threads, calendar)
        if decision.move:
            changes.append(decision.move)
        decisions.append(decision)

    as_of = max(email.received.date() for email in emails) if emails else date.today()
    start = min(event.start.date() for event in calendar) if calendar else as_of
    end = max(event.start.date() for event in calendar) if calendar else as_of
    focus = _focus_blocks(ruleset, calendar, start, end)
    protected = _protected_blocks(ruleset, calendar, start, end, focus)
    focus_by_week = defaultdict(float)
    for block in focus:
        week = (date.fromisoformat(block["date"]) - timedelta(days=date.fromisoformat(block["date"]).weekday())).isoformat()
        focus_by_week[week] += block["hours"]

    return {
        "version": "rules-1",
        "profile": {
            "name": profile.name,
            "role": profile.role,
            "source": profile.source_path.name,
            "goals": ruleset.goals,
            "focus": {
                "min": ruleset.focus_min,
                "max": ruleset.focus_max,
                "areas": ruleset.focus_areas,
            },
            "rhythm": {
                "plan": ruleset.plan_time.strftime("%H:%M"),
                "review": f"{WEEKDAY_NAMES[ruleset.review_weekday]} {ruleset.review_time:%H:%M}",
            },
            "channels": ruleset.channels,
            "warnings": ruleset.warnings,
        },
        "forbidden": [
            {
                "risk": topic.risk.value,
                "topic": topic.name,
                "keywords": list(topic.keywords),
                "scope": topic.scope,
                "source": topic.source.as_dict(),
            }
            for topic in ruleset.forbidden
        ],
        "auto_rules": [
            {"kind": rule.kind, "text": rule.text, "source": rule.source.as_dict()}
            for rule in ruleset.auto
        ],
        "email": {
            "emails": len(emails),
            "threads_total": len(threads),
            "thread_sizes": dict(Counter(thread.count for thread in threads)),
            "threads": [_decision_dict(decision) for decision in decisions],
            "eval": _evaluate(decisions),
        },
        "calendar": {
            "changes": changes,
            "rhythm_conflicts": _rhythm_conflicts(ruleset, calendar),
            "protected": protected,
            "focus": focus,
            "focus_hours_week": {week: round(hours, 2) for week, hours in sorted(focus_by_week.items())},
        },
        "tasks": _task_audit(tasks, emails, as_of),
        "plan": _plan(decisions, tasks, calendar, ruleset, as_of),
    }


def _fmt(result: dict) -> str:
    profile = result["profile"]
    email = result["email"]
    evaluation = email["eval"]
    calendar = result["calendar"]
    tasks = result["tasks"]
    lines = [
        f"# Правила агента · {profile['name']}",
        "",
        f"Источник профиля: {profile['source']}. Писем: {email['emails']}, тем: {email['threads_total']}.",
        f"Точность по темам: {evaluation['threads_hit']}/{evaluation['threads_total']} "
        f"({evaluation['thread_accuracy']:.0%}), по письмам: "
        f"{evaluation['emails_hit']}/{evaluation['emails_total']} ({evaluation['email_accuracy']:.0%}).",
        f"Чувствительных тем, ушедших в авто: {len(evaluation['sensitive_auto'])}.",
        "",
        "## Цели и защищённое время",
        *[f"- {goal}" for goal in profile["goals"]],
        f"- Фокус: {profile['focus']['min']:.0f}–{profile['focus']['max']:.0f} ч/нед "
        f"({'; '.join(profile['focus']['areas'])})",
        f"- Ритм: план дня {profile['rhythm']['plan']}, итог недели {profile['rhythm']['review']}",
        "",
        "## Запретные темы из профиля",
        *[
            f"- {item['topic']} → {item['risk']} ({item['scope']}); слова: {', '.join(item['keywords'])}"
            for item in result["forbidden"]
        ],
        "",
        "## Почта: решения по темам",
        "| Тема | Писем | Решение | Эталон | Кому |",
        "|---|---|---|---|---|",
        *[
            f"| {item['subject']} | {item['count']} | {item['action']} | {item['expected']} | {item['target'] or '—'} |"
            for item in email["threads"]
        ],
        "",
        "## Календарь",
        f"Автопереносы: {len(calendar['changes'])}",
        *[f"- {item['event']}: {item['from']} → {item['to']}" for item in calendar["changes"]],
        f"Защищённые блоки: {len(calendar['protected'])}",
        f"Фокус по неделям: " + ", ".join(
            f"{week}: {hours:g} ч" for week, hours in calendar["focus_hours_week"].items()
        ),
        *[f"- Ритм: {item['event']} {item['date']} в календаре {item['calendar_time']}, в профиле {item['profile_time']}"
          for item in calendar["rhythm_conflicts"]],
        "",
        "## Задачи",
        f"Просрочены на {tasks['as_of']}: {', '.join(tasks['overdue']) or 'нет'}",
        *[f"- {item['id']}: {item['kind']}" for item in tasks["status_mismatches"]],
        *[f"- {item['task_id']}: в письме {item['email_id']} срок {item['email_date']}, в задаче {item['due']}"
          for item in tasks["deadline_conflicts"]],
        "",
        "## План на следующий рабочий день",
        f"{result['plan']['date']}, {result['plan']['weekday']}; план дня начинается в "
        f"{result['plan']['plan_block']}.",
        f"Решений на руководителя: {len(result['plan']['approvals'])} тем, "
        f"делегировано: {len(result['plan']['delegated'])} тем, "
        f"просрочено задач: {len(result['plan']['overdue'])}.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    from .config import Settings

    settings = Settings.load()
    data = settings.data_dir
    profile = load_profile(data / "leader_profile.md")
    result = audit(
        profile,
        load_inbox(data / "inbox.csv"),
        load_org(data / "org_structure.csv"),
        load_tasks(data / "tasks.csv"),
        load_calendar(data / "calendar.ics"),
    )
    settings.out_dir.mkdir(parents=True, exist_ok=True)
    path = settings.out_dir / "rules.md"
    path.write_text(_fmt(result), encoding="utf-8")
    evaluation = result["email"]["eval"]
    print(
        f"threads={result['email']['threads_total']} "
        f"thread_accuracy={evaluation['thread_accuracy']:.2f} "
        f"email_accuracy={evaluation['email_accuracy']:.2f} "
        f"sensitive_auto={len(evaluation['sensitive_auto'])}"
    )
    print(f"report={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())