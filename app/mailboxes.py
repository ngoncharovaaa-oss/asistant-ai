# -*- coding: utf-8 -*-
"""Реестр почтовых ящиков.

Ящики из .env (MAIL_A_*, MAIL_B_*) остаются как есть; дополнительные ящики
хранятся локально в data/mailboxes.json. Пароли не отдаются наружу и не
попадают в журнал: API возвращает только признак «пароль задан».
"""
from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

PROVIDERS: dict[str, dict[str, Any]] = {
    "gmail": {
        "title": "Gmail",
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 465,
        "note": "Нужен пароль приложения: Google-аккаунт → Безопасность → Двухэтапная проверка → Пароли приложений.",
    },
    "mailru": {
        "title": "Mail.ru (в т. ч. домены на emx.mail.ru)",
        "imap_host": "imap.mail.ru",
        "imap_port": 993,
        "smtp_host": "smtp.mail.ru",
        "smtp_port": 465,
        "note": "Пароль для внешнего приложения: Настройки почты → Безопасность → Пароли для внешних приложений.",
    },
    "yandex": {
        "title": "Яндекс 360",
        "imap_host": "imap.yandex.ru",
        "imap_port": 993,
        "smtp_host": "smtp.yandex.ru",
        "smtp_port": 465,
        "note": "Пароль приложения: Яндекс ID → Безопасность → Пароли приложений; в почте включить IMAP.",
    },
    "outlook": {
        "title": "Outlook / Microsoft 365",
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "note": "SMTP через STARTTLS, порт 587. Может требоваться пароль приложения или разрешение в тенанте.",
    },
    "custom": {
        "title": "Другой сервер",
        "imap_host": "",
        "imap_port": 993,
        "smtp_host": "",
        "smtp_port": 465,
        "note": "Укажите хосты и порты вручную. IMAP — SSL, SMTP 465 — SSL, 587 — STARTTLS.",
    },
}

ADDRESS_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class MailboxError(RuntimeError):
    """Ошибка реестра без секретов в тексте."""


@dataclass
class Mailbox:
    id: str
    address: str
    label: str = ""
    provider: str = "custom"
    imap_host: str = ""
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 465
    user: str = ""
    password: str = field(default="", repr=False)
    from_name: str = ""
    created_at: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.user and self.password and self.imap_host and self.smtp_host)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "address": self.address or self.user,
            "label": self.label or self.address or self.user,
            "provider": self.provider,
            "provider_title": PROVIDERS.get(self.provider, {}).get("title", self.provider),
            "imap": self.imap_host,
            "imap_port": self.imap_port,
            "smtp": self.smtp_host,
            "smtp_port": self.smtp_port,
            "from_name": self.from_name,
            "configured": self.configured,
            "password_set": bool(self.password),
            "created_at": self.created_at,
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "address": self.address,
            "label": self.label,
            "provider": self.provider,
            "imap_host": self.imap_host,
            "imap_port": self.imap_port,
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
            "user": self.user,
            "password": self.password,
            "from_name": self.from_name,
            "created_at": self.created_at,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Mailbox":
        return cls(
            id=str(raw.get("id", "")),
            address=str(raw.get("address", "")),
            label=str(raw.get("label", "")),
            provider=str(raw.get("provider", "custom")),
            imap_host=str(raw.get("imap_host", "")),
            imap_port=int(raw.get("imap_port", 993) or 993),
            smtp_host=str(raw.get("smtp_host", "")),
            smtp_port=int(raw.get("smtp_port", 465) or 465),
            user=str(raw.get("user", "")),
            password=str(raw.get("password", "")),
            from_name=str(raw.get("from_name", "")),
            created_at=str(raw.get("created_at", "")),
        )


class MailboxStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = data.get("mailboxes") if isinstance(data, dict) else data
        return items if isinstance(items, list) else []

    def _write(self, items: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"mailboxes": items}, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def list(self) -> list[Mailbox]:
        return [Mailbox.from_json(item) for item in self._read()]

    def get(self, mailbox_id: str) -> Mailbox | None:
        return next((mb for mb in self.list() if mb.id == mailbox_id), None)

    def add(
        self,
        address: str,
        password: str,
        provider: str = "custom",
        label: str = "",
        from_name: str = "",
        imap_host: str = "",
        imap_port: int | None = None,
        smtp_host: str = "",
        smtp_port: int | None = None,
    ) -> Mailbox:
        address = (address or "").strip().lower()
        if not ADDRESS_RE.match(address):
            raise MailboxError("Некорректный адрес почты")
        if not password:
            raise MailboxError("Нужен пароль приложения")
        preset = PROVIDERS.get(provider) or PROVIDERS["custom"]
        host_in = (imap_host or preset["imap_host"] or "").strip()
        host_out = (smtp_host or preset["smtp_host"] or "").strip()
        if not host_in or not host_out:
            raise MailboxError("Укажите IMAP- и SMTP-серверы")
        port_in = int(imap_port or preset["imap_port"] or 993)
        port_out = int(smtp_port or preset["smtp_port"] or 465)
        if any(mb.address.lower() == address for mb in self.list()):
            raise MailboxError("Этот ящик уже добавлен")
        mb = Mailbox(
            id="mb" + secrets.token_hex(3),
            address=address,
            label=(label or "").strip() or address,
            provider=provider if provider in PROVIDERS else "custom",
            imap_host=host_in,
            imap_port=port_in,
            smtp_host=host_out,
            smtp_port=port_out,
            user=address,
            password=password,
            from_name=(from_name or "").strip(),
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
        items = self._read()
        items.append(mb.to_json())
        self._write(items)
        return mb

    def update_password(self, mailbox_id: str, password: str) -> Mailbox | None:
        if not (password or "").strip():
            raise MailboxError("Нужен пароль приложения")
        items = self._read()
        for item in items:
            if str(item.get("id")) == mailbox_id:
                item["password"] = password.strip()
                self._write(items)
                return Mailbox.from_json(item)
        return None

    def remove(self, mailbox_id: str) -> bool:
        items = self._read()
        keep = [item for item in items if str(item.get("id")) != mailbox_id]
        if len(keep) == len(items):
            return False
        self._write(keep)
        return True
