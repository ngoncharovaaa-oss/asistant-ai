# -*- coding: utf-8 -*-
"""Zoom: ссылки на встречи. Секреты читаются только из .env.

Два режима: API (Server-to-Server OAuth, создаёт новую встречу) и постоянная
ссылка ZOOM_PERSONAL_LINK (без сети). Наружу не отдаются ключи и хост-ссылка
start_url: только join_url для участников.
"""
from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from .config import Settings


class ZoomError(RuntimeError):
    """Ошибка Zoom без секретов в тексте."""


@dataclass
class ZoomConfig:
    account_id: str = ""
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    user_id: str = "me"
    personal_link: str = ""

    @property
    def api_mode(self) -> bool:
        return bool(self.account_id and self.client_id and self.client_secret)

    @property
    def configured(self) -> bool:
        return self.api_mode or bool(self.personal_link)

    @property
    def mode(self) -> str:
        if self.api_mode:
            return "api"
        if self.personal_link:
            return "link"
        return ""

    @property
    def public(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "mode": self.mode,
            "link_set": bool(self.personal_link),
        }


def load_zoom(settings: Settings) -> ZoomConfig:
    env = settings.env
    return ZoomConfig(
        account_id=env.get("ZOOM_ACCOUNT_ID", "").strip(),
        client_id=env.get("ZOOM_CLIENT_ID", "").strip(),
        client_secret=env.get("ZOOM_CLIENT_SECRET", "").strip(),
        user_id=env.get("ZOOM_USER_ID", "").strip() or "me",
        personal_link=env.get("ZOOM_PERSONAL_LINK", "").strip(),
    )


def _start(value: str) -> str:
    """«2026-11-28 10:00» → «2026-11-28T10:00:00»; пусто — без времени."""
    text = (value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace(" ", "T")).strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        raise ZoomError("zoom: start time must be YYYY-MM-DD HH:MM") from None


class ZoomClient:
    def __init__(self, config: ZoomConfig, urlopen: Callable[..., Any] | None = None):
        self.config = config
        self._urlopen = urlopen or urllib.request.urlopen
        self._access = ""
        self._access_until = datetime.min

    def _token(self) -> str:
        if self._access and datetime.now() < self._access_until:
            return self._access
        basic = base64.b64encode(
            f"{self.config.client_id}:{self.config.client_secret}".encode("utf-8")
        ).decode("ascii")
        query = urllib.parse.urlencode(
            {"grant_type": "account_credentials", "account_id": self.config.account_id}
        )
        request = urllib.request.Request(
            "https://zoom.us/oauth/token?" + query,
            data=b"",
            method="POST",
            headers={"Authorization": "Basic " + basic},
        )
        try:
            with self._urlopen(request, timeout=20) as response:
                body = json.loads(response.read().decode("utf-8"))
        except Exception:
            raise ZoomError("zoom: token request failed") from None
        token = body.get("access_token")
        if not token:
            raise ZoomError("zoom: token missing")
        self._access = token
        try:
            expires = int(body.get("expires_in", 3600))
        except (TypeError, ValueError):
            expires = 3600
        self._access_until = datetime.now() + timedelta(seconds=max(expires - 60, 60))
        return token

    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self._token()
        request = urllib.request.Request(
            "https://api.zoom.us/v2" + path,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            method=method,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
        )
        try:
            with self._urlopen(request, timeout=25) as response:
                raw = response.read().decode("utf-8")
        except Exception:
            raise ZoomError("zoom: api request failed") from None
        try:
            return json.loads(raw or "{}")
        except json.JSONDecodeError:
            raise ZoomError("zoom: bad api response") from None

    def check(self) -> dict[str, Any]:
        if not self.config.configured:
            return {"ok": False, "detail": "не настроен"}
        if self.config.mode == "link":
            return {"ok": True, "detail": "постоянная ссылка задана"}
        try:
            me = self._call("GET", "/users/" + urllib.parse.quote(self.config.user_id))
        except ZoomError as exc:
            return {"ok": False, "detail": str(exc)}
        name = me.get("display_name") or me.get("first_name") or ""
        return {"ok": True, "detail": "доступ к API подтверждён" + (f": {name}" if name else "")}

    def create_meeting(
        self,
        topic: str,
        start: str = "",
        minutes: int = 45,
        agenda: str = "",
    ) -> dict[str, Any]:
        topic = (topic or "").strip() or "Встреча с AsiStantAI"
        if not self.config.configured:
            raise ZoomError("zoom: not configured")
        when = _start(start)
        if self.config.mode == "link":
            return {
                "mode": "link",
                "topic": topic,
                "id": "",
                "join_url": self.config.personal_link,
                "start": when,
            }
        duration = max(15, min(int(minutes or 45), 240))
        payload: dict[str, Any] = {
            "topic": topic[:200],
            "type": 2,
            "duration": duration,
            "agenda": (agenda or "")[:500],
            "settings": {
                "waiting_room": True,
                "join_before_host": False,
                "auto_recording": "none",
            },
        }
        if when:
            payload["start_time"] = when
            payload["timezone"] = "Europe/Moscow"
        data = self._call(
            "POST",
            "/users/" + urllib.parse.quote(self.config.user_id) + "/meetings",
            payload,
        )
        join_url = str(data.get("join_url") or "")
        if not join_url:
            raise ZoomError("zoom: meeting created without join link")
        return {
            "mode": "api",
            "topic": topic,
            "id": str(data.get("id") or ""),
            "join_url": join_url,
            "start": when,
            "minutes": duration,
        }
