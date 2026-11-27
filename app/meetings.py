# -*- coding: utf-8 -*-
"""Протоколы встреч: разбор готовых summary в решения, задачи и открытые вопросы."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .models import Protocol, ProtocolTask, Source

TASK_RE = re.compile(r"^[-*]\s*\[\s*\]\s*(?P<rest>.+)$")
COLON_OWNER_RE = re.compile(r"^(?P<who>[A-ZА-ЯЁ][^:]{1,40}?)\s*:\s*(?P<text>.+)$")
DATE_DOTTED_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")
DEADLINE_RE = re.compile(
    r"(до\s+\d{2}\.\d{2}\.\d{4}|до\s+конца\s+\w+|к\s+концу\s+\w+|"
    r"к\s+следующему\s+\w+|до\s+\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|"
    r"июля|августа|сентября|октября|ноября|декабря))",
    re.IGNORECASE,
)
DECISION_MARKERS = (
    "решено", "решение", "решени", "договорились", "принято", "согласовали",
    "подтвержд", "будет создан", "договоренность", "договорённость",
)
SKIP_TASK_FRAGMENTS = ("[вставить", "[укажите", "[указать")
SKIP_QUESTION_PREFIXES = ("ии определил", "нерешенные вопросы", "нерешённые вопросы", "предложения ии")

DATIVES = {
    "насте": "Настя", "яне": "Яна", "диме": "Дима", "ивану": "Иван",
    "егору": "Егор", "вадиму": "Вадим", "оле": "Оля", "саше": "Саша", "маше": "Маша",
    "ване": "Ваня", "ане": "Аня", "полине": "Полина", "лене": "Лена", "ире": "Ира",
    "наташе": "Наташа", "наталье": "Наталья", "андрею": "Андрей", "паше": "Паша",
    "грише": "Гриша", "оли": "Оля", "жене": "Женя", "лизе": "Лиза",
}
COLON_NAME_RE = re.compile(r"^Speaker\s*\d+$", re.IGNORECASE)
NOMINATIVE_NAMES = {
    "Настя", "Яна", "Дима", "Иван", "Егор", "Вадим", "Оля", "Саша", "Маша",
    "Ваня", "Аня", "Полина", "Лена", "Ира", "Наташа", "Наталья", "Андрей",
    "Паша", "Гриша", "Женя", "Лиза",
}


def _clean_markdown(text: str) -> str:
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _title(lines: list[str], path: Path) -> str:
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith(("#", ">", "-", "`")):
            return _clean_markdown(stripped)
    return path.stem.replace("-Summary", "")


def _meeting_date(text: str, path: Path) -> datetime | None:
    match = re.search(r"Дата:\s*(\d{4}-\d{2}-\d{2})", text)
    if match:
        return datetime.strptime(match.group(1), "%Y-%m-%d")
    match = re.match(r"(\d{2})-(\d{2})\b", path.name)
    if match:
        month, day = int(match.group(1)), int(match.group(2))
        return datetime(2026, month, day)
    return None


def _protocol_id(path: Path, when: datetime | None) -> str:
    if when:
        return when.strftime("%Y-%m-%d")
    return re.sub(r"[^a-zа-я0-9]+", "-", path.stem.lower()).strip("-")[:40]


def _sections(text: str) -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    title = ""
    body: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## ") or line.startswith("### "):
            if title or body:
                out.append((title, body))
            title = _clean_markdown(line.lstrip("#").strip())
            body = []
            continue
        body.append(line)
    if title or body:
        out.append((title, body))
    return out


def _strip_quote(line: str) -> str:
    return line.lstrip("> ").strip()


def _owner_of(rest: str) -> tuple[str, str]:
    colon = COLON_OWNER_RE.match(rest)
    if colon:
        who = _clean_markdown(colon.group("who")).strip()
        if COLON_NAME_RE.match(who) or _looks_like_name(who) or ("(" in who and ")" in who):
            return who, colon.group("text").strip()
    speaker = re.match(r"^(Speaker\s*\d+)\s+(.+)$", rest, re.IGNORECASE)
    if speaker:
        return speaker.group(1), speaker.group(2).strip()
    first, _, tail = rest.partition(" ")
    key = first.lower().strip(",.:")
    if key in DATIVES:
        owner = DATIVES[key]
        second = re.match(r"^и\s+(\S+?)[\s,]+(.+)$", tail)
        if second and second.group(1).lower().strip(",.") in DATIVES:
            owner += ", " + DATIVES[second.group(1).lower().strip(",.")]
            return owner, second.group(2).strip()
        surname = re.match(r"^([А-ЯЁ][а-яё]+)\s+(.+)$", tail)
        if surname and surname.group(1).endswith(("у", "ю", "ой")):
            return owner + " " + surname.group(1), surname.group(2).strip()
        return owner, tail.strip()
    if first in NOMINATIVE_NAMES:
        owner = first
        second = re.match(r"^и\s+([А-ЯЁ][а-яё]+)\s+(.+)$", tail)
        if second and second.group(1) in NOMINATIVE_NAMES:
            owner += ", " + second.group(1)
            return owner, second.group(2).strip()
        return owner, tail.strip()
    return "", rest.strip()


def _looks_like_name(text: str) -> bool:
    words = text.replace(",", " ").split()
    if not words or len(words) > 4:
        return False
    return all(word[0].isupper() for word in words if word)


def _deadline(text: str) -> str:
    match = DEADLINE_RE.search(text)
    return _clean_markdown(match.group(1)) if match else ""


def _parse_task(line: str) -> ProtocolTask | None:
    match = TASK_RE.match(line)
    if not match:
        return None
    rest = _clean_markdown(match.group("rest"))
    if not rest or any(fragment in rest.lower() for fragment in SKIP_TASK_FRAGMENTS):
        return None
    owner, text = _owner_of(rest)
    return ProtocolTask(text=text, owner=owner, deadline=_deadline(text))


def _decisions(sections: list[tuple[str, list[str]]]) -> list[str]:
    found: list[str] = []
    for title, lines in sections:
        low_title = title.lower()
        if "дальнейшие" in low_title or "предложени" in low_title or "нерешен" in low_title:
            continue
        for line in lines:
            if not line.startswith(("- ", "* ")):
                continue
            bullet = _clean_markdown(line[2:])
            if not bullet:
                continue
            if any(marker in bullet.lower() for marker in DECISION_MARKERS):
                if bullet not in found:
                    found.append(bullet)
            if len(found) >= 6:
                return found
    return found


def _open_questions(sections: list[tuple[str, list[str]]]) -> list[str]:
    questions: list[str] = []
    active = False
    for title, lines in sections:
        if "предложени" in title.lower() or "нерешен" in title.lower():
            active = True
        if not active:
            continue
        for line in lines:
            stripped = _strip_quote(line)
            if not stripped:
                continue
            low = stripped.lower()
            if any(low.startswith(prefix) or prefix in low[:40] for prefix in SKIP_QUESTION_PREFIXES):
                continue
            match = re.match(r"^(?:\d+\.|[-*])\s*(.+)$", stripped)
            if not match:
                continue
            question = _clean_markdown(match.group(1))
            if "**" in match.group(1):
                question = _clean_markdown(match.group(1))
            if len(question) > 12 and question not in questions:
                questions.append(question)
    return questions[:8]


def _fallback_decisions(sections: list[tuple[str, list[str]]]) -> list[str]:
    out: list[str] = []
    for title, lines in sections:
        if any(key in title.lower() for key in ("информация", "ключевые слова", "протокол")):
            continue
        for line in lines:
            if not line.startswith(("- ", "* ")):
                continue
            bullet = _clean_markdown(line[2:])
            if bullet and not bullet.startswith("["):
                out.append(bullet)
            if len(out) >= 3:
                return out
    return out


def parse_summary(path: Path, text: str) -> Protocol:
    lines = text.splitlines()
    when = _meeting_date(text, path)
    sections = _sections(text)
    decisions = _decisions(sections) or _fallback_decisions(sections)
    tasks = [task for line in lines if (task := _parse_task(line.strip()))]
    questions = _open_questions(sections)
    title = _title(lines, path)
    protocol_id = _protocol_id(path, when)
    quote = decisions[0] if decisions else (tasks[0].text if tasks else title)
    sources = [
        Source(
            kind="meeting",
            title=title,
            quote=quote,
            ref=f"meetings#{protocol_id}",
        )
    ]
    return Protocol(
        id=protocol_id,
        title=title,
        decisions=decisions,
        tasks=tasks,
        open_questions=questions,
        sources=sources,
    )


def parse_all(meetings_dir: Path) -> list[Protocol]:
    from .datasets import load_meeting_summaries

    return [parse_summary(path, text) for path, text in load_meeting_summaries(meetings_dir)]


def to_markdown(protocol: Protocol, when: datetime | None = None) -> str:
    lines = [f"# Протокол: {protocol.title}", ""]
    if when:
        lines.append(f"Дата встречи: {when:%d.%m.%Y}")
    lines.append("")
    lines.append("## Решения")
    if protocol.decisions:
        lines += [f"- {item}" for item in protocol.decisions]
    else:
        lines.append("- Решения не зафиксированы явно, см. обсуждение.")
    lines += ["", "## Задачи"]
    if protocol.tasks:
        for task in protocol.tasks:
            owner = f"{task.owner} — " if task.owner else ""
            deadline = f" (срок: {task.deadline})" if task.deadline else ""
            lines.append(f"- [ ] {owner}{task.text}{deadline}")
    else:
        lines.append("- Задач нет.")
    lines += ["", "## Открытые вопросы"]
    if protocol.open_questions:
        lines += [f"{n}. {item}" for n, item in enumerate(protocol.open_questions, 1)]
    else:
        lines.append("Открытых вопросов нет.")
    lines += ["", "## Источники"]
    for source in protocol.sources:
        lines.append(f"- {source.title} — {source.ref}")
    lines.append("")
    return "\n".join(lines)


def write_protocols(protocols: list[Protocol], out_dir: Path) -> list[Protocol]:
    out_dir.mkdir(parents=True, exist_ok=True)
    for protocol in protocols:
        try:
            when = datetime.strptime(protocol.id, "%Y-%m-%d")
        except ValueError:
            when = None
        path = out_dir / f"{protocol.id} — протокол.md"
        path.write_text(to_markdown(protocol, when), encoding="utf-8")
        protocol.path = str(path)
    return protocols
