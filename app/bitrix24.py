# -*- coding: utf-8 -*-
"""Битрикс24: входящий вебхук REST, контакты и комментарии в таймлайне.

Адрес вебхука содержит секретный код портала, поэтому живёт только в .env.
Наружу отдаются домен портала и признак «настроен», но никогда сам адрес.
Черновики и письма не отправляются без утверждения: интеграция добавляет
контакт и короткий комментарий о последнем обращении.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from .config import Settings

REQUEST_TIMEOUT = 25


class BitrixError(RuntimeError):
    """Ошибка Битрикс24 без секретов в тексте."""


def _webhook(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if not text.endswith("/"):
        text += "/"
    parsed = urlparse(text)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.scheme != "https" or not parsed.hostname or len(parts) != 3 or parts[0] != "rest":
        raise BitrixError("bitrix24: webhook must be https://<portal>/rest/<id>/<code>/")
    return text


@dataclass
class BitrixConfig:
    webhook: str = field(default="", repr=False)

    @property
    def url(self) -> str:
        try:
            return _webhook(self.webhook)
        except BitrixError:
            return ""

    @property
    def configured(self) -> bool:
        return bool(self.url)

    @property
    def portal(self) -> str:
        if not self.url:
            return ""
        return (urlparse(self.url).hostname or "").lower()

    @property
    def public(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "mode": "webhook" if self.configured else "",
            "portal": self.portal,
            "invalid": bool(self.webhook) and not self.url,
        }


def load_bitrix24(settings: Settings) -> BitrixConfig:
    return BitrixConfig(webhook=settings.env.get("BITRIX24_WEBHOOK_URL", "").strip())


def _first(value: Any) -> str:
    """Поле Битрикс24 приходит списком словарей — берём первое значение."""
    if isinstance(value, list):
        if not value:
            return ""
        item = value[0]
    else:
        item = value
    if isinstance(item, dict):
        return str(item.get("VALUE") or "").strip()
    return str(item or "").strip()


def _name(row: dict[str, Any]) -> str:
    parts = [str(row.get("NAME") or "").strip(), str(row.get("LAST_NAME") or "").strip()]
    return " ".join(part for part in parts if part)


class Bitrix24Client:
    def __init__(self, config: BitrixConfig, urlopen: Callable[..., Any] | None = None):
        self.config = config
        self._urlopen = urlopen or urllib.request.urlopen

    def _call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        base = self.config.url
        if not base:
            raise BitrixError("bitrix24: not configured")
        request = urllib.request.Request(
            base + method + ".json",
            data=json.dumps(params or {}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with self._urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                raw = response.read().decode("utf-8")
        except Exception:
            raise BitrixError("bitrix24: network error") from None
        try:
            body = json.loads(raw or "{}")
        except json.JSONDecodeError:
            raise BitrixError("bitrix24: bad response") from None
        if not isinstance(body, dict):
            raise BitrixError("bitrix24: bad response")
        if body.get("error"):
            reason = str(body.get("error_description") or body.get("error") or "api error")
            reason = reason.replace(base, "").strip()
            raise BitrixError("bitrix24: " + reason[:200])
        return body.get("result")

    def check(self) -> dict[str, Any]:
        if not self.config.configured:
            return {"ok": False, "detail": "не настроен"}
        try:
            me = self._call("profile") or {}
        except BitrixError as exc:
            return {"ok": False, "detail": str(exc)}
        name = _name(me)
        detail = "портал: " + self.config.portal
        if name:
            detail += ", пользователь: " + name
        return {"ok": True, "detail": detail}

    def contacts(self, limit: int = 50) -> list[dict[str, Any]]:
        result = self._call(
            "crm.contact.list",
            {
                "select": ["ID", "NAME", "LAST_NAME", "EMAIL", "PHONE", "COMPANY_TITLE"],
                "order": {"ID": "DESC"},
            },
        )
        rows = result if isinstance(result, list) else []
        items: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            email = _first(row.get("EMAIL"))
            if not email:
                continue
            items.append(
                {
                    "id": str(row.get("ID") or ""),
                    "email": email,
                    "name": _name(row) or email,
                    "phone": _first(row.get("PHONE")),
                    "company": str(row.get("COMPANY_TITLE") or "").strip(),
                    "source": "bitrix24",
                }
            )
        return items[: max(1, limit)]

    def find_contact(self, email: str) -> str:
        result = self._call(
            "crm.duplicate.findbycomm",
            {"entity_type": "CONTACT", "type": "EMAIL", "values": [email]},
        )
        if not isinstance(result, dict):
            return ""
        ids = result.get("CONTACT") or []
        return str(ids[0]) if ids else ""

    def add_contact(self, name: str, email: str, role: str = "") -> str:
        if "@" not in (email or ""):
            raise BitrixError("bitrix24: contact needs an email")
        comment = "AsiStantAI" + (f" · {role}" if role else "")
        result = self._call(
            "crm.contact.add",
            {
                "fields": {
                    "NAME": (name or email).strip()[:200],
                    "EMAIL": [{"VALUE": email.strip(), "VALUE_TYPE": "WORK"}],
                    "COMMENTS": comment[:2000],
                }
            },
        )
        contact_id = str(result or "")
        if not contact_id:
            raise BitrixError("bitrix24: contact created without id")
        return contact_id

    def add_comment(self, contact_id: str, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        result = self._call(
            "crm.timeline.comment.add",
            {
                "fields": {
                    "ENTITY_TYPE": "CONTACT",
                    "ENTITY_ID": contact_id,
                    "COMMENT": text[:4000],
                }
            },
        )
        return str(result or "")

    def sync_contact(self, email: str, name: str = "", role: str = "", note: str = "") -> dict[str, Any]:
        """Контакт из панели в Битрикс24: найти или создать, затем комментарий."""
        if "@" not in (email or ""):
            raise BitrixError("bitrix24: contact needs an email")
        contact_id = self.find_contact(email)
        created = False
        if not contact_id:
            contact_id = self.add_contact(name, email, role)
            created = True
        commented = False
        if note:
            self.add_comment(contact_id, note)
            commented = True
        return {
            "id": contact_id,
            "created": created,
            "commented": commented,
            "portal": self.config.portal,
        }
