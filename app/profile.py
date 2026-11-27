# -*- coding: utf-8 -*-
"""Профиль руководителя: настройки без кода."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import Risk, Source


def slugify(text: str) -> str:
    table = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    text = text.lower().strip()
    text = "".join(table.get(ch, ch) for ch in text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "leader"


@dataclass
class Rule:
    risk: Risk | None
    text: str
    source: Source


@dataclass
class Profile:
    id: str
    name: str
    role: str
    source_path: Path
    goals: list[str] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    sensitive_rules: list[Rule] = field(default_factory=list)
    auto_rules: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    style: str = ""
    signature: str = ""
    style_sources: list[Source] = field(default_factory=list)

    def greeting_name(self) -> str:
        return self.name.split()[0] if self.name else ""

    def rule_for(self, risk: Risk) -> Rule | None:
        for rule in self.sensitive_rules:
            if rule.risk == risk:
                return rule
        return None


RISK_KEYWORDS = {
    Risk.MONEY: ("деньг", "бюджет", "скидк", "закуп", "перераспредел"),
    Risk.HR: ("кадр", "найм", "оффер", "отпуск", "увольнен"),
    Risk.CLIENT: ("обязательств", "срок", "услови", "гарант"),
    Risk.LEADERSHIP: ("руководств", "директор"),
}

SECTION_LABELS = {
    "цели": "Цели на квартал",
    "никогда": "Никогда без подтверждения руководителя",
    "можно без": "Можно без подтверждения",
    "каналы": "Каналы",
    "стиль": "Стиль общения",
    "время": "Куда направить освобождённое время",
}


def _sections(text: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            current = line[3:].strip()
            result[current] = []
            continue
        if current and line:
            result[current].append(line)
    return result


def _bullets(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        if line.startswith(("- ", "* ")):
            out.append(line[2:].strip())
        elif re.match(r"^\d+\.", line):
            out.append(re.sub(r"^\d+\.\s*", "", line).strip())
    return out


def load_profile(path: Path) -> Profile:
    text = path.read_text(encoding="utf-8")
    sections = _sections(text)

    head = next((line for line in text.splitlines() if "—" in line and line.strip()), "")
    name, _, role = head.partition("—")
    name = name.strip().lstrip("# ").strip()
    role = role.split(".")[0].strip()

    def pick(*keys: str) -> list[str]:
        for title, lines in sections.items():
            low = title.lower()
            if any(key in low for key in keys):
                return _bullets(lines)
        return []

    goals = pick("цели")
    protected = pick("время")
    auto_rules = pick("можно без")
    channels = pick("каналы")

    sensitive: list[Rule] = []
    for title, lines in sections.items():
        if "никогда" not in title.lower():
            continue
        for bullet in _bullets(lines):
            low = bullet.lower()
            risk = None
            for candidate, words in RISK_KEYWORDS.items():
                if any(word in low for word in words):
                    if candidate == Risk.LEADERSHIP and "директор" in low and "коммерческ" in low:
                        risk = Risk.LEADERSHIP
                    elif candidate == Risk.CLIENT and "клиент" in low:
                        risk = Risk.CLIENT
                    elif candidate == Risk.HR and ("кадр" in low or "найм" in low or "оффер" in low or "отпуск" in low):
                        risk = Risk.HR
                    elif candidate == Risk.MONEY and ("деньг" in low or "бюджет" in low or "скидк" in low):
                        risk = Risk.MONEY
                    if risk:
                        break
            sensitive.append(
                Rule(
                    risk=risk,
                    text=bullet,
                    source=Source(
                        kind="profile",
                        title=title,
                        quote=bullet,
                        ref=f"{path.name}#{slugify(title)}",
                    ),
                )
            )

    style_lines = []
    for title, lines in sections.items():
        if "стиль" in title.lower():
            style_lines = [line for line in lines if not line.startswith(("- ", "* "))]
            style_text = " ".join(style_lines)
            break
    else:
        style_text = ""

    signature = f"{name}\n{role}" if role else name
    style_sources = [
        Source(
            kind="profile",
            title="Стиль общения",
            quote=style_text,
            ref=f"{path.name}#style",
        )
    ] if style_text else []

    return Profile(
        id=slugify(name),
        name=name,
        role=role,
        source_path=path,
        goals=goals,
        protected=protected,
        sensitive_rules=sensitive,
        auto_rules=auto_rules,
        channels=channels,
        style=style_text,
        signature=signature,
        style_sources=style_sources,
    )
