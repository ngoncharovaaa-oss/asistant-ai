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


RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
EMPTY_DEADLINES = {"не назван", "не назван точно", "не названа", "не указан", "—", "-"}


def _meta(text: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for raw in text.splitlines():
        match = re.match(r"^\*\*(.+?):\*\*\s*(.+?)\s*$", raw.strip())
        if match:
            meta[match.group(1).strip()] = _clean_markdown(match.group(2))
    return meta


def _ru_date(value: str) -> datetime | None:
    match = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", value.lower())
    if match and match.group(2) in RU_MONTHS:
        return datetime(int(match.group(3)), RU_MONTHS[match.group(2)], int(match.group(1)))
    match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", value)
    if match:
        return datetime(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    return None


def _section(text: str, keyword: str) -> str:
    match = re.search(
        rf"##\s*\d*\.?\s*{keyword}.*?\n(.*?)(?=\n##\s|\Z)", text, re.S | re.IGNORECASE
    )
    return match.group(1) if match else ""


def _table(block: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not cells or all(set(cell) <= set("-: ") for cell in cells):
            continue
        if cells[0].lower().startswith("№"):
            continue
        rows.append(cells)
    return rows


def _task_from_row(cells: list[str]) -> ProtocolTask | None:
    if len(cells) < 5:
        return None
    text = _clean_markdown(cells[1])
    if not text or text.startswith("[") or "вставить" in text.lower():
        return None
    deadline = _clean_markdown(cells[-2])
    if deadline.lower() in EMPTY_DEADLINES:
        deadline = ""
    owner = _clean_markdown(cells[-3])
    if owner.lower() in {"не назначен", "не назначена", "не назначено", "—", "-"}:
        owner = ""
    return ProtocolTask(
        text=text,
        owner=owner,
        deadline=deadline,
        status_source=_clean_markdown(cells[-1]),
    )


def parse_protocol_doc(path: Path, text: str) -> Protocol:
    """Разбор полного протокола встречи: шапка, повестка, решения, поручения, риски, шаги."""
    meta = _meta(text)
    when = _ru_date(meta.get("Дата", ""))
    protocol_id = when.strftime("%Y-%m-%d") if when else _protocol_id(path, None)
    decisions = [
        _clean_markdown(match) for match in re.findall(r"\*\*Решени[ея]:\*\*\s*(.+)", text)
    ]
    decisions = [item for item in decisions if item]
    tasks = [
        task
        for cells in _table(_section(text, "Поручения"))
        if (task := _task_from_row(cells))
    ]
    risks: list[str] = []
    questions: list[str] = []
    for cells in _table(_section(text, "Открытые вопросы и риски")):
        if len(cells) < 3:
            continue
        question = _clean_markdown(cells[1])
        need = _clean_markdown(cells[2])
        owner = _clean_markdown(cells[3]) if len(cells) > 3 else ""
        if not question:
            continue
        risks.append(question)
        question_text = f"{question} — требуется: {need}" if need else question
        if owner:
            question_text += f" (владелец: {owner})"
        questions.append(question_text)
    agenda = [
        _clean_markdown(match)
        for match in re.findall(r"^\s*\d+\.\s+(.+)$", _section(text, "Повестка"), re.M)
    ]
    next_steps = [
        _clean_markdown(match)
        for match in re.findall(r"^\s*-\s*\[[ xX]\]\s*(.+)$", _section(text, "Дальнейшие шаги"), re.M)
    ]
    title = meta.get("Тема") or path.stem.replace(" — протокол", "")
    quote = decisions[0] if decisions else title
    return Protocol(
        id=protocol_id,
        title=title,
        decisions=decisions,
        tasks=tasks,
        open_questions=questions,
        sources=[
            Source(kind="meeting_protocol", title=title, quote=quote, ref=f"protocols#{protocol_id}")
        ],
        when=when.strftime("%d.%m.%Y") if when else "",
        host=meta.get("Ведущий") or meta.get("Ведущая") or meta.get("Ведущие") or "",
        participants=meta.get("Участники", ""),
        agenda=agenda,
        risks=risks,
        next_steps=next_steps,
        doc_path=str(path),
    )


def load_protocol_docs(protocols_dir: Path | None) -> dict[str, Protocol]:
    if not protocols_dir or not protocols_dir.exists():
        return {}
    docs: dict[str, Protocol] = {}
    for path in sorted(protocols_dir.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*.md")):
        protocol = parse_protocol_doc(path, path.read_text(encoding="utf-8"))
        docs[protocol.id] = protocol
    return docs


def parse_all(meetings_dir: Path, protocols_dir: Path | None = None) -> list[Protocol]:
    from .datasets import load_meeting_summaries

    docs = load_protocol_docs(protocols_dir)
    protocols: list[Protocol] = []
    seen: set[str] = set()
    for path, text in load_meeting_summaries(meetings_dir):
        summary = parse_summary(path, text)
        protocol = docs.get(summary.id, summary)
        protocols.append(protocol)
        seen.add(protocol.id)
    for protocol_id, protocol in docs.items():
        if protocol_id not in seen:
            protocols.append(protocol)
    return sorted(protocols, key=lambda item: item.id)


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
        if protocol.doc_path and Path(protocol.doc_path).is_file():
            path.write_text(Path(protocol.doc_path).read_text(encoding="utf-8"), encoding="utf-8")
        else:
            path.write_text(to_markdown(protocol, when), encoding="utf-8")
        protocol.path = str(path)
    return protocols
