# -*- coding: utf-8 -*-
"""Яндекс Телемост: ссылка на встречу из постоянной комнаты.

У Телемоста нет публичного API создания встреч, поэтому канал работает от
комнаты TELEMOST_LINK: ссылку создаёт человек в telemost.yandex.ru, а система
добавляет к ней тему, время и текст приглашения. Секретов у канала нет.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .links import saved as saved_link

TELEMOST_URL = "https://telemost.yandex.ru/"
TELEMOST_HOST = "telemost.yandex.ru"


class TelemostError(RuntimeError):
    """Ошибка Телемоста без секретов в тексте."""


def _link(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != TELEMOST_HOST:
        raise TelemostError("telemost: link must be telemost.yandex.ru")
    return text


def _start(value: str) -> str:
    """«2026-11-28 10:00» → «2026-11-28T10:00:00»; пусто — без времени."""
    text = (value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace(" ", "T")).strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        raise TelemostError("telemost: start time must be YYYY-MM-DD HH:MM") from None


@dataclass
class TelemostConfig:
    personal_link: str = ""

    @property
    def link(self) -> str:
        try:
            return _link(self.personal_link)
        except TelemostError:
            return ""

    @property
    def configured(self) -> bool:
        return bool(self.link)

    @property
    def public(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "mode": "link" if self.configured else "",
            "link_set": bool(self.personal_link),
            "invalid": bool(self.personal_link) and not self.link,
        }


def load_telemost(settings: Settings) -> TelemostConfig:
    env = settings.env
    return TelemostConfig(
        personal_link=env.get("TELEMOST_LINK", "").strip() or saved_link(settings, "telemost")
    )


class TelemostClient:
    def __init__(self, config: TelemostConfig):
        self.config = config

    def check(self) -> dict[str, Any]:
        if not self.config.personal_link:
            return {"ok": False, "detail": "не настроен"}
        if not self.config.link:
            return {"ok": False, "detail": "ссылка должна быть на telemost.yandex.ru"}
        return {"ok": True, "detail": "комната Телемоста задана"}

    def create_meeting(self, topic: str, start: str = "") -> dict[str, Any]:
        topic = (topic or "").strip() or "Встреча с AsiStantAI"
        if not self.config.configured:
            raise TelemostError("telemost: not configured")
        when = _start(start)
        link = self.config.link
        pretty = when.replace("T", " ")
        if pretty.endswith(":00"):
            pretty = pretty[:-3]
        lines = [topic]
        if when:
            lines.append(pretty)
        lines.append(link)
        return {
            "mode": "link",
            "platform": "telemost",
            "topic": topic,
            "join_url": link,
            "start": when,
            "invite": "\n".join(lines),
        }
