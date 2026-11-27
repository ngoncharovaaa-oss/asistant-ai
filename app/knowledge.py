# -*- coding: utf-8 -*-
"""Поиск по базе знаний: профиль, оргструктура, задачи, календарь, протоколы."""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from .models import Event, Person, Protocol, Source, Task
from .profile import Profile

STOP = {
    "и", "в", "во", "на", "по", "с", "со", "к", "о", "об", "от", "до", "за", "из",
    "не", "что", "как", "это", "для", "при", "или", "но", "же", "бы", "мы", "вы",
    "они", "она", "его", "её", "их", "наш", "ваш", "вас", "нас", "мне", "вам",
}


def tokens(text: str) -> list[str]:
    words = re.findall(r"[a-zа-яё0-9]+", text.lower())
    return [w[:6] for w in words if len(w) > 2 and w not in STOP]


@dataclass
class Document:
    source: Source
    tokens: list[str]


class KnowledgeBase:
    def __init__(self, documents: list[Document]):
        self.documents = documents
        df: Counter[str] = Counter()
        for doc in documents:
            df.update(set(doc.tokens))
        n = max(len(documents), 1)
        self.idf = {word: math.log((n + 1) / (count + 0.5)) for word, count in df.items()}

    @classmethod
    def build(
        cls,
        profile: Profile,
        org: list[Person],
        tasks: list[Task],
        events: list[Event],
        protocols: list[Protocol],
    ) -> "KnowledgeBase":
        docs: list[Document] = []
        for source in profile.style_sources:
            docs.append(Document(source, tokens(source.quote)))
        for rule in profile.sensitive_rules:
            docs.append(Document(rule.source, tokens(rule.text)))
        for person in org:
            source = Source(
                kind="org",
                title=f"{person.name} — {person.position}",
                quote=f"роль: {person.role}",
                ref=f"org_structure.csv#{person.id}",
            )
            docs.append(Document(source, tokens(f"{person.name} {person.position} {person.role}")))
        for task in tasks:
            quote = f"владелец {task.owner}, статус «{task.status}»"
            if task.due:
                quote += f", срок {task.due:%d.%m.%Y}"
            source = Source(kind="task", title=task.task, quote=quote, ref=f"tasks.csv#{task.id}")
            docs.append(Document(source, tokens(f"{task.task} {task.owner} {task.status}")))
        for event in events:
            source = Source(
                kind="calendar",
                title=event.summary,
                quote=f"{event.start:%d.%m %H:%M}–{event.end:%H:%M}, {event.category}",
                ref=f"calendar.ics#{event.uid}",
            )
            docs.append(Document(source, tokens(f"{event.summary} {event.category}")))
        for protocol in protocols:
            for decision in protocol.decisions[:4]:
                source = Source(
                    kind="meeting",
                    title=protocol.title,
                    quote=decision,
                    ref=f"meetings#{protocol.id}",
                )
                docs.append(Document(source, tokens(decision)))
        return cls(docs)

    def search(
        self,
        query: str,
        limit: int = 3,
        kinds: set[str] | None = None,
        min_overlap: int = 1,
        min_score: float = 0.0,
    ) -> list[Source]:
        query_tokens = set(tokens(query))
        if not query_tokens:
            return []
        scored: list[tuple[float, Document]] = []
        for doc in self.documents:
            if kinds and doc.source.kind not in kinds:
                continue
            overlap = query_tokens & set(doc.tokens)
            if len(overlap) < min_overlap:
                continue
            score = sum(self.idf.get(word, 0.5) for word in overlap) / math.sqrt(len(doc.tokens) + 1)
            if score < min_score:
                continue
            scored.append((score, doc))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [doc.source for _, doc in scored[:limit]]
