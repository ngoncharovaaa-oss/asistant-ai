# -*- coding: utf-8 -*-
"""Zoom: вход через приложение Marketplace и ссылки на встречи.

Три режима без пароля Zoom-аккаунта:
* login — пользователь один раз входит на стороне Zoom (OAuth authorization
  code): приложение получает токен, встречи создаются от его имени;
* api — Server-to-Server OAuth (Account ID + Client ID + Secret);
* link — постоянная ссылка на комнату, без сети.

Секреты живут только в .env и data/zoom_oauth.json (0600, в git не попадает).
Наружу отдаются join_url и факт входа, но никогда start_url и токены.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .links import saved as saved_link

AUTHORIZE_URL = "https://zoom.us/oauth/authorize"
TOKEN_URL = "https://zoom.us/oauth/token"


class ZoomError(RuntimeError):
    """Ошибка Zoom без секретов в тексте."""


@dataclass
class ZoomConfig:
    account_id: str = ""
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    user_id: str = "me"
    personal_link: str = ""
    redirect_uri: str = ""
    oauth: dict[str, Any] = field(default_factory=dict, repr=False)
    oauth_file: Path | None = field(default=None, repr=False)

    @property
    def client_ready(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def api_mode(self) -> bool:
        return bool(self.account_id and self.client_id and self.client_secret)

    @property
    def logged_in(self) -> bool:
        return bool(self.oauth.get("access_token"))

    @property
    def configured(self) -> bool:
        return self.logged_in or self.api_mode or bool(self.personal_link)

    @property
    def mode(self) -> str:
        if self.logged_in:
            return "login"
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
            "logged_in": self.logged_in,
            "name": str(self.oauth.get("name") or ""),
        }


def oauth_path(settings: Settings) -> Path:
    return settings.db_path.parent / "zoom_oauth.json"


def load_oauth(settings: Settings) -> dict[str, Any]:
    file = oauth_path(settings)
    if not file.exists():
        return {}
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_oauth(settings: Settings, data: dict[str, Any]) -> None:
    save_oauth_file(oauth_path(settings), data)


def clear_oauth(settings: Settings) -> None:
    try:
        oauth_path(settings).unlink()
    except FileNotFoundError:
        pass


def load_zoom(settings: Settings) -> ZoomConfig:
    env = settings.env
    return ZoomConfig(
        account_id=env.get("ZOOM_ACCOUNT_ID", "").strip(),
        client_id=env.get("ZOOM_CLIENT_ID", "").strip(),
        client_secret=env.get("ZOOM_CLIENT_SECRET", "").strip(),
        user_id=env.get("ZOOM_USER_ID", "").strip() or "me",
        personal_link=env.get("ZOOM_PERSONAL_LINK", "").strip() or saved_link(settings, "zoom"),
        redirect_uri=env.get("ZOOM_REDIRECT_URI", "").strip(),
        oauth=load_oauth(settings),
        oauth_file=oauth_path(settings),
    )


_pending: dict[str, float] = {}


def authorize_url(config: ZoomConfig) -> str:
    if not (config.client_id and config.redirect_uri):
        raise ZoomError("zoom: oauth app is not configured")
    now = time.time()
    for key in [key for key, until in _pending.items() if until < now]:
        _pending.pop(key, None)
    state = secrets.token_urlsafe(16)
    _pending[state] = now + 600
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "state": state,
        }
    )
    return AUTHORIZE_URL + "?" + query


def check_state(state: str) -> bool:
    until = _pending.pop(state or "", 0.0)
    return bool(until) and time.time() <= until


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

    def _oauth_token(self, params: dict[str, str]) -> dict[str, Any]:
        basic = base64.b64encode(
            f"{self.config.client_id}:{self.config.client_secret}".encode("utf-8")
        ).decode("ascii")
        request = urllib.request.Request(
            TOKEN_URL + "?" + urllib.parse.urlencode(params),
            data=b"",
            method="POST",
            headers={"Authorization": "Basic " + basic},
        )
        try:
            with self._urlopen(request, timeout=20) as response:
                body = json.loads(response.read().decode("utf-8"))
        except Exception:
            raise ZoomError("zoom: token request failed") from None
        if not isinstance(body, dict):
            raise ZoomError("zoom: bad token response")
        return body

    def _store(self, body: dict[str, Any]) -> None:
        token = str(body.get("access_token") or "")
        if not token:
            raise ZoomError("zoom: token missing")
        try:
            expires = int(body.get("expires_in", 3600))
        except (TypeError, ValueError):
            expires = 3600
        data = {
            "access_token": token,
            "refresh_token": str(body.get("refresh_token") or self.config.oauth.get("refresh_token") or ""),
            "expires_at": datetime.now().timestamp() + max(expires - 60, 60),
            "name": str(self.config.oauth.get("name") or ""),
        }
        self.config.oauth = data
        if self.config.oauth_file:
            save_oauth_file(self.config.oauth_file, data)

    def _refresh(self) -> str:
        refresh = str(self.config.oauth.get("refresh_token") or "")
        if not refresh or not self.config.client_ready:
            raise ZoomError("zoom: session expired, log in again")
        body = self._oauth_token({"grant_type": "refresh_token", "refresh_token": refresh})
        self._store(body)
        return str(self.config.oauth["access_token"])

    def _token(self) -> str:
        if self.config.logged_in:
            token = str(self.config.oauth.get("access_token") or "")
            expires = float(self.config.oauth.get("expires_at") or 0)
            if token and (not expires or datetime.now().timestamp() < expires - 60):
                return token
            return self._refresh()
        if self._access and datetime.now() < self._access_until:
            return self._access
        if not self.config.api_mode:
            raise ZoomError("zoom: not configured")
        body = self._oauth_token(
            {"grant_type": "account_credentials", "account_id": self.config.account_id}
        )
        token = str(body.get("access_token") or "")
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

    def login(self, code: str) -> dict[str, Any]:
        if not self.config.redirect_uri:
            raise ZoomError("zoom: redirect uri is not configured")
        body = self._oauth_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.config.redirect_uri,
            }
        )
        self._store(body)
        name = ""
        try:
            me = self._call("GET", "/users/me")
            name = str(me.get("display_name") or me.get("first_name") or "")
        except ZoomError:
            pass
        if name:
            self.config.oauth["name"] = name
            if self.config.oauth_file:
                save_oauth_file(self.config.oauth_file, self.config.oauth)
        return {"name": name, "mode": "login"}

    def logout(self) -> None:
        self.config.oauth = {}
        if self.config.oauth_file:
            try:
                self.config.oauth_file.unlink()
            except FileNotFoundError:
                pass

    def check(self) -> dict[str, Any]:
        if not self.config.configured:
            return {"ok": False, "detail": "не настроен"}
        if self.config.mode == "link":
            return {"ok": True, "detail": "постоянная ссылка задана"}
        try:
            if self.config.logged_in:
                me = self._call("GET", "/users/me")
            else:
                me = self._call("GET", "/users/" + urllib.parse.quote(self.config.user_id))
        except ZoomError as exc:
            return {"ok": False, "detail": str(exc)}
        name = me.get("display_name") or me.get("first_name") or ""
        if self.config.logged_in:
            self.config.oauth["name"] = str(name)
            if name and self.config.oauth_file:
                save_oauth_file(self.config.oauth_file, self.config.oauth)
            return {"ok": True, "detail": "вход выполнен" + (f": {name}" if name else "")}
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
        user = "me" if self.config.logged_in else self.config.user_id
        data = self._call(
            "POST",
            "/users/" + urllib.parse.quote(user) + "/meetings",
            payload,
        )
        join_url = str(data.get("join_url") or "")
        if not join_url:
            raise ZoomError("zoom: meeting created without join link")
        return {
            "mode": "login" if self.config.logged_in else "api",
            "topic": topic,
            "id": str(data.get("id") or ""),
            "join_url": join_url,
            "start": when,
            "minutes": duration,
        }


def save_oauth_file(file: Path, data: dict[str, Any]) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, file)
