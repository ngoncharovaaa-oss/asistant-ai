# -*- coding: utf-8 -*-
"""Личный Telegram через MTProto (Telethon): непрочитанные и черновики ответов.

Доступ появляется только после входа в аккаунт: TELEGRAM_API_ID и
TELEGRAM_API_HASH из my.telegram.org, затем один раз `python -m app.cli tg-login`.
Сессия хранится локально и в git не попадает; телефон, код и api_hash не
логируются. История читается через messages.getHistory, поэтому непрочитанные
не помечаются прочитанными.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import Settings
from .periods import aware, normalize_period, period_cutoff, period_title

_LOCK = threading.Lock()


class TelegramUserError(RuntimeError):
    """Ошибка личного Telegram без секретов в тексте."""


def _int(value: str, default: int = 0) -> int:
    try:
        return int(str(value).strip() or default)
    except ValueError:
        return default


@dataclass
class TelegramUserConfig:
    api_id: int = 0
    api_hash: str = field(default="", repr=False)
    session_path: Path = Path("data/telegram_user.session")

    @property
    def configured(self) -> bool:
        return bool(self.api_id and self.api_hash)

    @property
    def session_set(self) -> bool:
        return self.session_path.exists() and self.session_path.stat().st_size > 0

    @property
    def authorized(self) -> bool:
        return self.configured and self.session_set

    @property
    def username(self) -> str:
        meta = self.session_path.with_suffix(".json")
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        return str(data.get("username", "") or "")

    @property
    def public(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "authorized": self.authorized,
            "session_set": self.session_set,
            "username": self.username,
            "mode": "user" if self.authorized else "",
        }


def load_telegram_user(settings: Settings) -> TelegramUserConfig:
    env = settings.env
    raw = env.get("TELEGRAM_SESSION", "").strip()
    path = Path(raw).expanduser() if raw else settings.db_path.parent / "telegram_user.session"
    return TelegramUserConfig(
        api_id=_int(env.get("TELEGRAM_API_ID", "")),
        api_hash=env.get("TELEGRAM_API_HASH", "").strip(),
        session_path=path,
    )


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _write_meta(session_path: Path, username: str) -> None:
    meta = session_path.with_suffix(".json")
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(
        json.dumps(
            {"username": username, "signed_in_at": datetime.now().isoformat(timespec="seconds")},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _chmod(meta, 0o600)


def _make_client(config: TelegramUserConfig) -> Any:
    from telethon import TelegramClient

    return TelegramClient(str(config.session_path), config.api_id, config.api_hash)


def login(settings: Settings, phone: str = "") -> dict[str, Any]:
    """Интерактивный вход: телефон, код и пароль вводит человек в терминале."""
    config = load_telegram_user(settings)
    if not config.configured:
        raise TelegramUserError("telegram: укажите TELEGRAM_API_ID и TELEGRAM_API_HASH")
    try:
        from telethon.sync import TelegramClient
    except ImportError:
        raise TelegramUserError("telegram: установите telethon (pip install telethon)") from None
    config.session_path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(config.session_path), config.api_id, config.api_hash)
    try:
        client.start(phone=phone or None)
        me = client.get_me()
    except Exception:
        raise TelegramUserError("telegram: вход не выполнен") from None
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
    username = str(getattr(me, "username", "") or "")
    _write_meta(config.session_path, username)
    _chmod(config.session_path, 0o600)
    return {"ok": True, "username": username, "user_id": str(getattr(me, "id", "") or "")}


def _text_of(message: Any) -> str:
    text = str(getattr(message, "message", "") or "").strip()
    if text:
        return text
    if getattr(message, "media", None):
        return "[медиа]"
    if getattr(message, "action", None):
        return "[служебное сообщение]"
    return ""


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def reply_draft(name: str, kind: str, text: str) -> str:
    low = (text or "").lower()
    greeting = "Добрый день!"
    if kind == "user" and name:
        first = name.split()[0]
        if 1 < len(first) <= 20 and not first.startswith("@"):
            greeting = f"Добрый день, {first}!"
    if any(word in low for word in ("поздравляю", "с днём рождения", "с днем рождения", "с праздником")):
        body = "Спасибо большое! Очень приятно."
    elif any(word in low for word in ("созвон", "встреч", "zoom", "телемост", "удобное время", "календар")):
        body = "Давайте созвонимся — предложите удобное время, подстроюсь."
    elif "?" in (text or ""):
        body = "Вижу вопрос — отвечу сегодня, уточню детали и вернусь."
    elif any(word in low for word in ("спасибо", "благодар")):
        body = "Пожалуйста! Рада помочь."
    else:
        body = "Спасибо, увидела сообщение — отвечу в ближайшее время."
    return f"{greeting} {body}"


class TelegramUser:
    def __init__(
        self,
        config: TelegramUserConfig,
        client_factory: Callable[[TelegramUserConfig], Any] | None = None,
    ):
        self.config = config
        self._factory = client_factory or _make_client

    def _call(self, fn: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        if not self.config.configured:
            raise TelegramUserError("telegram: not configured")
        if not self.config.session_set:
            raise TelegramUserError("telegram: session not authorized")

        async def work() -> Any:
            client = self._factory(self.config)
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    raise TelegramUserError("telegram: session not authorized")
                return await fn(client, *args)
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        def runner() -> Any:
            async def go() -> Any:
                return await asyncio.wait_for(work(), timeout=45)

            with _LOCK:
                return asyncio.run(go())

        try:
            return runner()
        except TelegramUserError:
            raise
        except Exception:
            raise TelegramUserError("telegram: request failed") from None

    def check(self) -> dict[str, Any]:
        try:
            data = self._call(self._me)
        except TelegramUserError as exc:
            return {"ok": False, "detail": str(exc)}
        username = data.get("username", "")
        return {"ok": True, "detail": "вход выполнен" + (f": @{username}" if username else "")}

    @staticmethod
    async def _me(client: Any) -> dict[str, Any]:
        me = await client.get_me()
        name = " ".join(
            part for part in (getattr(me, "first_name", ""), getattr(me, "last_name", "")) if part
        )
        return {"username": str(getattr(me, "username", "") or ""), "name": name}

    def unread(self, limit: int = 20, period: str = "all") -> dict[str, Any]:
        return self._call(self._unread, max(1, min(int(limit or 20), 50)), normalize_period(period))

    def drafts(self, limit: int = 5, period: str = "all") -> dict[str, Any]:
        return self._call(self._drafts, max(1, min(int(limit or 5), 20)), normalize_period(period))

    @staticmethod
    def _within(message: Any, cutoff: datetime | None) -> bool:
        if cutoff is None:
            return True
        when = getattr(message, "date", None)
        if not hasattr(when, "isoformat"):
            return False
        return aware(when) >= cutoff

    @staticmethod
    async def _collect(client: Any, limit: int, period: str = "all") -> dict[str, Any]:
        cutoff = period_cutoff(period)
        dialogs = await client.get_dialogs(limit=200)
        total = 0
        chats = 0
        items: list[dict[str, Any]] = []
        for dialog in dialogs:
            count = int(getattr(dialog, "unread_count", 0) or 0)
            if count <= 0:
                continue
            entity = getattr(dialog, "entity", None)
            try:
                messages = await client.get_messages(
                    entity or dialog, limit=min(count, 100) if cutoff else min(count, 20)
                )
            except Exception:
                messages = []
            incoming: list[Any] = []
            if cutoff is not None:
                incoming = [
                    item
                    for item in messages
                    if not getattr(item, "out", False) and TelegramUser._within(item, cutoff)
                ]
                if not incoming:
                    continue
                shown_count = len(incoming)
            else:
                shown_count = count
            total += shown_count
            chats += 1
            if len(items) >= limit:
                continue
            name = str(getattr(dialog, "name", "") or "")
            username = str(getattr(entity, "username", "") or "")
            if getattr(dialog, "is_group", False):
                kind = "group"
            elif getattr(dialog, "is_channel", False):
                kind = "channel"
            else:
                kind = "user"
            if cutoff is not None:
                shown = incoming[0] if incoming else None
            else:
                shown = next((item for item in messages if not getattr(item, "out", False)), None)
                shown = shown or (messages[0] if messages else None)
            when = getattr(shown, "date", None)
            items.append(
                {
                    "id": str(getattr(dialog, "id", "") or ""),
                    "name": name,
                    "username": username,
                    "kind": kind,
                    "unread": shown_count,
                    "mentions": int(getattr(dialog, "unread_mentions_count", 0) or 0),
                    "last_text": _clip(_text_of(shown), 200) if shown else "",
                    "last_at": when.isoformat() if hasattr(when, "isoformat") else "",
                    "outgoing": bool(shown and getattr(shown, "out", False)),
                }
            )
        return {
            "total": total,
            "dialogs": chats,
            "items": items,
            "period": period,
            "period_title": period_title(period),
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }

    @staticmethod
    async def _unread(client: Any, limit: int, period: str = "all") -> dict[str, Any]:
        return await TelegramUser._collect(client, limit, period)

    @staticmethod
    async def _drafts(client: Any, limit: int, period: str = "all") -> dict[str, Any]:
        data = await TelegramUser._collect(client, limit, period)
        drafts = []
        for item in data["items"]:
            drafts.append(
                {
                    **item,
                    "basis": item["last_text"],
                    "reply": reply_draft(item["name"], item["kind"], item["last_text"]),
                    "note": "Отправка — вручную из Telegram.",
                }
            )
        return {
            "total": data["total"],
            "dialogs": data["dialogs"],
            "drafts": drafts,
            "period": data["period"],
            "period_title": data["period_title"],
            "checked_at": data["checked_at"],
        }
