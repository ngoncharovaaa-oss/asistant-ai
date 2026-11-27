# -*- coding: utf-8 -*-
"""Живые каналы: IMAP-приём, SMTP-отправка, Telegram Bot API и Zoom.

Секреты читаются только из .env. Ни статусы, ни журнал, ни сообщения об ошибках
не содержат паролей и токенов: наружу отдаются только признаки «настроен/ошибка».
"""
from __future__ import annotations

import email as mailparse
import html as htmlmod
import imaplib
import json
import re
import smtplib
import ssl
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr, parsedate_to_datetime
from typing import Any, Callable

from .config import Settings
from .journal import Journal
from .models import Email
from .pipeline import RunResult, run as run_pipeline
from .zoom import ZoomClient, load_zoom


class ChannelError(RuntimeError):
    """Ошибка канала без секретов в тексте."""


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return value.strip()


def _html_to_text(raw: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = htmlmod.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _message_body(msg: mailparse.message.Message) -> str:
    if msg.is_multipart():
        plain = None
        html = None
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if (part.get("Content-Disposition") or "").startswith("attachment"):
                continue
            try:
                payload = part.get_content()
            except Exception:
                payload = ""
            if part.get_content_type() == "text/plain" and plain is None:
                plain = str(payload)
            elif part.get_content_type() == "text/html" and html is None:
                html = str(payload)
        return (plain or _html_to_text(html or "")).strip()
    try:
        content = str(msg.get_content())
    except Exception:
        content = ""
    if msg.get_content_type() == "text/html":
        return _html_to_text(content)
    return content.strip()


def _safe_date(msg: mailparse.message.Message) -> datetime:
    try:
        dt = parsedate_to_datetime(msg.get("Date", ""))
    except (TypeError, ValueError):
        dt = None
    if dt is None:
        return datetime.now()
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


@dataclass
class MailAccount:
    label: str
    address: str
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    user: str
    password: str = field(default="", repr=False)
    from_name: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.user and self.password and self.imap_host and self.smtp_host)

    @property
    def public(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "address": self.address or self.user,
            "imap": self.imap_host,
            "smtp": self.smtp_host,
            "configured": self.configured,
        }


@dataclass
class TelegramConfig:
    token: str = field(default="", repr=False)
    chat_id: str = ""
    allowed: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return bool(self.token)

    @property
    def can_notify(self) -> bool:
        return bool(self.token and self.chat_id)

    @property
    def public(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "chat_id": self.chat_id,
            "allow_list": len(self.allowed),
        }


def _env_int(env: dict[str, str], key: str, default: int) -> int:
    try:
        return int(env.get(key, "") or default)
    except ValueError:
        return default


def load_accounts(settings: Settings) -> dict[str, MailAccount]:
    env = settings.env
    accounts: dict[str, MailAccount] = {}
    for suffix, label in (("A", "a"), ("B", "b")):
        user = env.get(f"MAIL_{suffix}_USER", "").strip()
        password = env.get(f"MAIL_{suffix}_PASSWORD", "").strip()
        imap_host = env.get(f"MAIL_{suffix}_IMAP_HOST", "").strip()
        smtp_host = env.get(f"MAIL_{suffix}_SMTP_HOST", "").strip()
        if not (user or password or imap_host or smtp_host):
            continue
        accounts[label] = MailAccount(
            label=label,
            address=env.get(f"MAIL_{suffix}_ADDRESS", "").strip() or user,
            imap_host=imap_host,
            imap_port=_env_int(env, f"MAIL_{suffix}_IMAP_PORT", 993),
            smtp_host=smtp_host,
            smtp_port=_env_int(env, f"MAIL_{suffix}_SMTP_PORT", 465),
            user=user,
            password=password,
            from_name=env.get(f"MAIL_{suffix}_FROM_NAME", "").strip(),
        )
    return accounts


def load_telegram(settings: Settings) -> TelegramConfig:
    env = settings.env
    allowed = [item.strip() for item in env.get("TELEGRAM_ALLOWED_CHATS", "").split(",") if item.strip()]
    return TelegramConfig(
        token=env.get("TELEGRAM_BOT_TOKEN", "").strip(),
        chat_id=env.get("TELEGRAM_CHAT_ID", "").strip(),
        allowed=allowed,
    )


class Channels:
    """Фасад каналов. Транспорты подменяемы — на них держатся тесты без сети."""

    def __init__(
        self,
        settings: Settings,
        imap_factory: Callable[[str, int], Any] | None = None,
        smtp_factory: Callable[[str, int], Any] | None = None,
        urlopen: Callable[..., Any] | None = None,
    ):
        self.settings = settings
        self.accounts = load_accounts(settings)
        self.telegram = load_telegram(settings)
        self._imap_factory = imap_factory or (lambda host, port: imaplib.IMAP4_SSL(host, port))
        self._smtp_factory = smtp_factory or self._default_smtp
        self._urlopen = urlopen or urllib.request.urlopen
        self.zoom = ZoomClient(load_zoom(settings), urlopen=self._urlopen)

    @staticmethod
    def _default_smtp(host: str, port: int) -> Any:
        if port == 465:
            return smtplib.SMTP_SSL(host, port, timeout=20)
        conn = smtplib.SMTP(host, port, timeout=20)
        conn.starttls(context=ssl.create_default_context())
        return conn

    @property
    def mail_configured(self) -> bool:
        return any(account.configured for account in self.accounts.values())

    def fetch(self, label: str, limit: int = 20, seen: set[str] | None = None) -> tuple[list[Email], list[str]]:
        account = self.accounts.get(label)
        if not account or not account.configured:
            raise ChannelError(f"mail_{label}: not configured")
        seen = seen or set()
        try:
            conn = self._imap_factory(account.imap_host, account.imap_port)
            conn.login(account.user, account.password)
        except Exception:
            raise ChannelError(f"mail_{label}: imap login failed") from None
        pairs: list[tuple[Email, str]] = []
        try:
            conn.select("INBOX", readonly=True)
            _, data = conn.uid("search", None, "ALL")
            raw_uids = (data[0] or b"").split() if data else []
            for uid_raw in reversed(raw_uids):
                if len(pairs) >= limit:
                    break
                uid = uid_raw.decode() if isinstance(uid_raw, bytes) else str(uid_raw)
                if uid in seen:
                    continue
                _, msg_data = conn.uid("fetch", uid, "(BODY.PEEK[])")
                raw = b""
                for item in msg_data or []:
                    if isinstance(item, tuple) and len(item) > 1:
                        raw = item[1]
                        break
                if not raw:
                    continue
                parsed = self._parse(account, uid, raw)
                if parsed is not None:
                    pairs.append((parsed, uid))
        except ChannelError:
            raise
        except Exception:
            raise ChannelError(f"mail_{label}: imap fetch failed") from None
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        pairs.sort(key=lambda pair: pair[0].received)
        return [item for item, _ in pairs], [uid for _, uid in pairs]

    @staticmethod
    def _parse(account: MailAccount, uid: str, raw: bytes) -> Email | None:
        try:
            msg = mailparse.message_from_bytes(raw)
            from_name, from_email = parseaddr(_decode(msg.get("From", "")))
            from_email = from_email.strip()
            if not from_email or from_email.lower() == account.user.lower():
                return None
            body = _message_body(msg)[:6000]
            return Email(
                id=f"live-{account.label}-{uid}",
                received=_safe_date(msg),
                from_name=from_name or from_email,
                from_email=from_email,
                sender_role="",
                to=_decode(msg.get("To", "")),
                subject=_decode(msg.get("Subject", "")) or "(без темы)",
                body=body,
            )
        except Exception:
            return None

    def send(self, label: str, to_addr: str, subject: str, body: str) -> dict[str, Any]:
        account = self.accounts.get(label)
        if not account or not account.configured:
            raise ChannelError(f"mail_{label}: not configured")
        if not to_addr or "@" not in to_addr:
            raise ChannelError("mail: recipient has no email address")
        msg = EmailMessage()
        msg["From"] = formataddr((account.from_name or account.address, account.user))
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=account.user.split("@")[-1] or None)
        msg.set_content(body)
        try:
            conn = self._smtp_factory(account.smtp_host, account.smtp_port)
            conn.login(account.user, account.password)
            conn.send_message(msg)
            conn.quit()
        except ChannelError:
            raise
        except Exception:
            raise ChannelError(f"mail_{label}: smtp send failed") from None
        return {"channel": "smtp", "sent": True, "to": to_addr, "message_id": msg["Message-ID"]}

    def check_mail(self, label: str) -> dict[str, Any]:
        account = self.accounts.get(label)
        result: dict[str, Any] = {"ok": False, "detail": "не настроен"}
        if not account or not account.configured:
            return result
        try:
            conn = self._imap_factory(account.imap_host, account.imap_port)
            conn.login(account.user, account.password)
            conn.select("INBOX", readonly=True)
            _, data = conn.uid("search", None, "ALL")
            count = len((data[0] or b"").split()) if data else 0
            conn.logout()
            result = {"ok": True, "detail": f"вход выполнен, писем в INBOX: {count}"}
        except Exception:
            return {"ok": False, "detail": "ошибка входа или соединения (IMAP)"}
        try:
            conn = self._smtp_factory(account.smtp_host, account.smtp_port)
            conn.login(account.user, account.password)
            conn.quit()
            result["smtp_ok"] = True
        except Exception:
            result["smtp_ok"] = False
        return result

    def _tg_call(self, method: str, payload: dict[str, Any], timeout: int = 15) -> Any:
        if not self.telegram.configured:
            raise ChannelError("telegram: not configured")
        url = f"https://api.telegram.org/bot{self.telegram.token}/{method}"
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with self._urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except Exception:
            raise ChannelError("telegram: network error") from None
        if not body.get("ok"):
            raise ChannelError("telegram: " + str(body.get("description") or "api error"))
        return body.get("result")

    def tg_send(self, text: str, buttons: list[list[dict[str, str]]] | None = None, chat_id: str | None = None) -> Any:
        target = chat_id or self.telegram.chat_id
        if not target:
            raise ChannelError("telegram: chat_id not configured")
        payload: dict[str, Any] = {
            "chat_id": target,
            "text": text[:4000],
            "disable_web_page_preview": True,
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        return self._tg_call("sendMessage", payload)

    def tg_updates(self, offset: int | None = None, timeout: int = 25) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return self._tg_call("getUpdates", payload, timeout=timeout + 10) or []

    def tg_answer(self, callback_id: str, text: str = "") -> Any:
        return self._tg_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})

    def tg_check(self) -> dict[str, Any]:
        me = self._tg_call("getMe", {})
        return {"ok": True, "username": (me or {}).get("username", "")}


def channels_status(settings: Settings, check: bool = False, channels: Channels | None = None) -> dict[str, Any]:
    ch = channels or Channels(settings)
    accounts = {label: dict(account.public) for label, account in ch.accounts.items()}
    if check:
        for label, account in ch.accounts.items():
            if account.configured:
                accounts[label].update(ch.check_mail(label))
    telegram = dict(ch.telegram.public)
    if check and ch.telegram.configured:
        try:
            telegram.update(ch.tg_check())
        except ChannelError as exc:
            telegram.update({"ok": False, "detail": str(exc)})
    zoom = dict(ch.zoom.config.public)
    if check and ch.zoom.config.configured:
        zoom.update(ch.zoom.check())
    return {
        "accounts": accounts,
        "telegram": telegram,
        "zoom": zoom,
        "mail_configured": ch.mail_configured,
        "live_ready": ch.mail_configured and ch.telegram.configured,
        "zoom_ready": ch.zoom.config.configured,
    }


def _notify(ch: Channels, text: str) -> None:
    if not ch.telegram.can_notify:
        return
    try:
        ch.tg_send(text)
    except ChannelError:
        pass


def run_live(
    settings: Settings,
    journal: Journal,
    profile_path=None,
    channels: Channels | None = None,
) -> RunResult | None:
    """Снять новые письма из настроенных ящиков и прогнать их через тот же конвейер."""
    ch = channels or Channels(settings)
    if not ch.mail_configured:
        raise ChannelError("mail: not configured")
    limit = _env_int(settings.env, "MAIL_FETCH_LIMIT", 20)
    emails: list[Email] = []
    seen_pairs: list[tuple[str, str]] = []
    used: list[str] = []
    for label, account in ch.accounts.items():
        if not account.configured:
            continue
        batch, uids = ch.fetch(label, limit=limit, seen=journal.seen_uids(label))
        if batch:
            used.append(label)
            emails.extend(batch)
            seen_pairs.extend((label, uid) for uid in uids)
    if not emails:
        return None
    emails.sort(key=lambda item: item.received)
    result = run_pipeline(
        settings,
        profile_path=profile_path,
        journal=journal,
        emails=emails,
        source="imap:" + (",".join(used) or "none"),
    )
    for label, uid in seen_pairs:
        journal.mark_seen(label, [uid])
    _notify(
        ch,
        f"Ася: новых писем {len(emails)}, черновиков {result.metrics['drafts']}, "
        f"на решение {result.metrics['decisions']}. Команда /drafts — очередь.",
    )
    return result


def _draft_account(ch: Channels, draft: dict[str, Any]) -> str:
    preferred = "b" if str(draft.get("email_id", "")).startswith("live-b-") else "a"
    account = ch.accounts.get(preferred)
    if account and account.configured:
        return preferred
    return next((label for label, acc in ch.accounts.items() if acc.configured), "")


def approve_draft(
    settings: Settings,
    journal: Journal,
    draft_id: str,
    comment: str = "",
    channels: Channels | None = None,
) -> dict[str, Any]:
    """Утверждение — единственная точка отправки.

    Живой прогон (runs.dataset начинается с imap:) отправляет письмо через SMTP.
    Демо-прогон на датасете остаётся моком и в сеть не выходит.
    """
    ch = channels or Channels(settings)
    draft = journal.draft(draft_id)
    if not draft:
        return {"ok": False, "error": "draft_not_found"}
    if draft.get("status") == "rejected":
        return {"ok": False, "error": "draft_rejected"}
    if draft.get("status") == "approved":
        return {"ok": True, "status": "approved", "sent": bool(draft.get("sent")), "already": True}
    if not journal.run_dataset().startswith("imap:"):
        return journal.approve(draft_id, comment)

    to_email = (draft.get("to_email") or "").strip()
    if draft.get("kind") == "decision" or not to_email:
        channel = "telegram" if ch.telegram.can_notify else "none"
        result = journal.approve(
            draft_id,
            comment,
            delivery={"channel": channel, "sent": False, "reason": "no_recipient_email"},
        )
        result["note"] = "Решение отмечено утверждённым. Письмо не отправлялось: у решения нет адресата."
        _notify(ch, f"Решение утверждено: {draft.get('subject', '')}")
        return result

    label = _draft_account(ch, draft)
    if not label:
        raise ChannelError("mail: not configured")
    delivery = ch.send(label, to_email, draft.get("subject", ""), draft.get("body", ""))
    result = journal.approve(draft_id, comment, delivery=delivery)
    _notify(ch, f"Отправлено: {draft.get('recipient') or to_email} — {draft.get('subject', '')}")
    return result


TELEGRAM_HELP = "Команды: /status, /drafts, /approve <id>, /reject <id>."


def _is_allowed(telegram: TelegramConfig, chat_id: str) -> bool:
    if telegram.allowed:
        return chat_id in telegram.allowed
    if telegram.chat_id:
        return chat_id == telegram.chat_id
    return False


def _handle_update(ch: Channels, journal: Journal, update: dict[str, Any]) -> None:
    telegram = ch.telegram
    if "callback_query" in update:
        query = update["callback_query"]
        chat_id = str((query.get("message") or {}).get("chat", {}).get("id", ""))
        if not _is_allowed(telegram, chat_id):
            ch.tg_answer(query.get("id", ""), "Нет доступа: добавьте chat_id в allow-list")
            return
        action, _, draft_id = (query.get("data") or "").partition(":")
        try:
            if action == "approve":
                result = approve_draft(ch.settings, journal, draft_id, channels=ch)
                ch.tg_answer(query.get("id", ""), "Отправлено" if result.get("sent") else "Утверждено")
            elif action == "reject":
                journal.reject(draft_id)
                ch.tg_answer(query.get("id", ""), "Отклонено")
            else:
                ch.tg_answer(query.get("id", ""), "Неизвестное действие")
        except ChannelError:
            ch.tg_answer(query.get("id", ""), "Ошибка отправки, проверьте каналы")
        return

    message = update.get("message")
    if not message:
        return
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()
    command = text.split()[0] if text else ""
    if command in {"/start", "/id"}:
        ch.tg_send(f"Чат подключён. chat_id: {chat_id}\n{TELEGRAM_HELP}", chat_id=chat_id)
        return
    if not _is_allowed(telegram, chat_id):
        ch.tg_send("Нет доступа: добавьте этот chat_id в TELEGRAM_ALLOWED_CHATS.", chat_id=chat_id)
        return
    _, _, argument = text.partition(" ")
    argument = argument.strip()
    if command == "/status":
        metrics = journal.run_metrics() or {}
        waiting = [item for item in journal.drafts() if item.get("status") == "waiting_approval"]
        ch.tg_send(
            f"Ася: писем {metrics.get('emails', 0)}, черновиков {metrics.get('drafts', 0)}, "
            f"ждут {len(waiting)}, на решение {metrics.get('decisions', 0)}, "
            f"автоотправок {metrics.get('auto_sends', 0)}.",
            chat_id=chat_id,
        )
    elif command == "/drafts":
        items = [item for item in journal.drafts() if item.get("status") == "waiting_approval"][:5]
        if not items:
            ch.tg_send("Очередь пуста.", chat_id=chat_id)
            return
        for item in items:
            ch.tg_send(
                f"{item.get('subject', '')}\nКому: {item.get('recipient', '—')}",
                buttons=[
                    [
                        {"text": "Утвердить", "callback_data": f"approve:{item['id']}"},
                        {"text": "Отклонить", "callback_data": f"reject:{item['id']}"},
                    ]
                ],
                chat_id=chat_id,
            )
    elif command == "/approve" and argument:
        try:
            result = approve_draft(ch.settings, journal, argument, channels=ch)
            ch.tg_send("Отправлено." if result.get("sent") else "Утверждено.", chat_id=chat_id)
        except ChannelError:
            ch.tg_send("Не удалось отправить: проверьте каналы.", chat_id=chat_id)
    elif command == "/reject" and argument:
        journal.reject(argument)
        ch.tg_send("Отклонено.", chat_id=chat_id)
    else:
        ch.tg_send(TELEGRAM_HELP, chat_id=chat_id)


def telegram_listen(
    settings: Settings,
    journal: Journal,
    channels: Channels | None = None,
    once: bool = False,
) -> int:
    """Долгое опрос-цикл бота. once=True — обработать один пакет (для тестов)."""
    ch = channels or Channels(settings)
    if not ch.telegram.configured:
        raise ChannelError("telegram: not configured")
    offset = None
    handled = 0
    while True:
        updates = ch.tg_updates(offset)
        for update in updates:
            offset = max(offset or 0, int(update.get("update_id", 0)) + 1)
            try:
                _handle_update(ch, journal, update)
                handled += 1
            except ChannelError:
                continue
        if once:
            return handled
