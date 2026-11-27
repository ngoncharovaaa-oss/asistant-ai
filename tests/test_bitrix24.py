# -*- coding: utf-8 -*-
"""Битрикс24 без сети: вебхук, статус портала, контакты и таймлайн."""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

import app.api as api
from app.bitrix24 import BitrixError
from app.journal import Journal
from app.live import Channels, channels_status

WEBHOOK = "https://acme.bitrix24.ru/rest/1/secret-code/"
SECRET = "secret-code"


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeBitrix:
    def __init__(self):
        self.requests = []
        self.fail = False
        self.contact_exists = False
        self.has_email_contact = True

    def __call__(self, request, timeout: int = 10):
        self.requests.append(request)
        if self.fail:
            raise OSError("network down")
        method = request.full_url.rsplit("/", 1)[-1].removesuffix(".json")
        body = json.loads(request.data.decode("utf-8") or "{}")
        if method == "profile":
            return FakeResponse({"result": {"ID": "1", "NAME": "Иван", "LAST_NAME": "Крутько"}})
        if method == "crm.contact.list":
            rows = [
                {
                    "ID": "7",
                    "NAME": "Ольга",
                    "LAST_NAME": "Петрова",
                    "EMAIL": [{"VALUE": "o@romashka.ru", "VALUE_TYPE": "WORK"}],
                    "PHONE": [{"VALUE": "+7 900 000-00-00"}],
                    "COMPANY_TITLE": "Ромашка",
                },
                {"ID": "8", "NAME": "Без почты"},
            ]
            return FakeResponse({"result": rows})
        if method == "crm.duplicate.findbycomm":
            assert body["values"] == ["o@romashka.ru"]
            found = {"CONTACT": ["7"]} if self.contact_exists else None
            return FakeResponse({"result": found})
        if method == "crm.contact.add":
            assert body["fields"]["EMAIL"][0]["VALUE"] == "o@romashka.ru"
            return FakeResponse({"result": "9"})
        if method == "crm.timeline.comment.add":
            return FakeResponse({"result": "100"})
        raise AssertionError(request.full_url)


def configure(settings, webhook: str = WEBHOOK) -> None:
    settings.env["BITRIX24_WEBHOOK_URL"] = webhook


def test_unconfigured_bitrix(settings) -> None:
    channel = Channels(settings).bitrix
    assert channel.config.configured is False
    assert channel.check() == {"ok": False, "detail": "не настроен"}
    assert channels_status(settings)["bitrix24_ready"] is False
    with pytest.raises(BitrixError) as error:
        channel.sync_contact("o@romashka.ru")
    assert str(error.value) == "bitrix24: not configured"


def test_bad_webhook_is_invalid_and_hidden(settings) -> None:
    for bad in (
        "http://acme.bitrix24.ru/rest/1/secret-code/",
        "https://acme.bitrix24.ru/rest/1/",
        "https://acme.bitrix24.ru/oauth/1/secret-code/",
    ):
        configure(settings, bad)
        status = channels_status(settings)
        assert status["bitrix24"]["configured"] is False
        assert status["bitrix24"]["invalid"] is True
        assert status["bitrix24_ready"] is False
        payload = json.dumps(status, ensure_ascii=False)
        assert SECRET not in payload


def test_webhook_check_sees_portal_without_leaking(settings) -> None:
    configure(settings)
    fake = FakeBitrix()
    ch = Channels(settings, urlopen=fake)
    status = channels_status(settings, check=True, channels=ch)
    assert status["bitrix24"]["configured"] is True
    assert status["bitrix24"]["mode"] == "webhook"
    assert status["bitrix24"]["portal"] == "acme.bitrix24.ru"
    assert status["bitrix24"]["ok"] is True
    assert "acme.bitrix24.ru" in status["bitrix24"]["detail"]
    assert "Иван Крутько" in status["bitrix24"]["detail"]
    assert status["bitrix24_ready"] is True
    payload = json.dumps(status, ensure_ascii=False)
    assert SECRET not in payload
    assert WEBHOOK not in payload
    assert fake.requests[0].full_url == WEBHOOK + "profile.json"


def test_import_contacts_maps_fields(settings) -> None:
    configure(settings)
    ch = Channels(settings, urlopen=FakeBitrix())
    items = ch.bitrix.contacts()
    assert items == [
        {
            "id": "7",
            "email": "o@romashka.ru",
            "name": "Ольга Петрова",
            "phone": "+7 900 000-00-00",
            "company": "Ромашка",
            "source": "bitrix24",
        }
    ]


def test_sync_contact_comments_existing(settings) -> None:
    configure(settings)
    fake = FakeBitrix()
    fake.contact_exists = True
    ch = Channels(settings, urlopen=fake)
    result = ch.bitrix.sync_contact("o@romashka.ru", "Ольга", "клиент", "Ася: письмо про счёт")
    assert result == {"id": "7", "created": False, "commented": True, "portal": "acme.bitrix24.ru"}
    assert SECRET not in json.dumps(result, ensure_ascii=False)
    methods = [request.full_url.rsplit("/", 1)[-1] for request in fake.requests]
    assert methods == ["crm.duplicate.findbycomm.json", "crm.timeline.comment.add.json"]
    comment = json.loads(fake.requests[-1].data.decode("utf-8"))
    assert comment["fields"]["ENTITY_TYPE"] == "CONTACT"
    assert comment["fields"]["ENTITY_ID"] == "7"
    assert "письмо про счёт" in comment["fields"]["COMMENT"]


def test_sync_contact_creates_then_comments(settings) -> None:
    configure(settings)
    fake = FakeBitrix()
    fake.contact_exists = False
    ch = Channels(settings, urlopen=fake)
    result = ch.bitrix.sync_contact("o@romashka.ru", "Ольга", "клиент", "")
    assert result["id"] == "9"
    assert result["created"] is True
    assert result["commented"] is False
    methods = [request.full_url.rsplit("/", 1)[-1] for request in fake.requests]
    assert methods == ["crm.duplicate.findbycomm.json", "crm.contact.add.json"]


def test_errors_are_generic(settings) -> None:
    configure(settings)
    fake = FakeBitrix()
    fake.fail = True
    ch = Channels(settings, urlopen=fake)
    status = channels_status(settings, check=True, channels=ch)
    assert status["bitrix24"]["ok"] is False
    assert SECRET not in status["bitrix24"]["detail"]
    with pytest.raises(BitrixError) as error:
        ch.bitrix.contacts()
    assert SECRET not in str(error.value)


def test_api_crm_reports_bitrix_status(settings, monkeypatch) -> None:
    monkeypatch.setattr(api, "settings", settings)
    configure(settings)
    data = api.crm()
    assert data["bitrix"]["configured"] is True
    assert data["bitrix"]["portal"] == "acme.bitrix24.ru"
    assert SECRET not in json.dumps(data, ensure_ascii=False)


def test_api_endpoints_require_webhook(settings, monkeypatch) -> None:
    monkeypatch.setattr(api, "settings", settings)
    with pytest.raises(HTTPException) as contact:
        api.crm_bitrix_contact(api.BitrixContactRequest(email="o@romashka.ru"))
    assert contact.value.status_code == 409
    with pytest.raises(HTTPException) as imported:
        api.crm_bitrix_import()
    assert imported.value.status_code == 409
    with pytest.raises(HTTPException) as bad_email:
        api.crm_bitrix_contact(api.BitrixContactRequest(email="не-почта"))
    assert bad_email.value.status_code == 400
    checked = api.crm_bitrix_check()
    assert checked["configured"] is False


def test_api_push_contact_and_import(settings, monkeypatch) -> None:
    monkeypatch.setattr(api, "settings", settings)
    configure(settings)
    fake = FakeBitrix()
    fake.contact_exists = True
    monkeypatch.setattr(api, "Channels", lambda s: Channels(s, urlopen=fake))
    checked = api.crm_bitrix_check()
    assert checked["ok"] is True
    pushed = api.crm_bitrix_contact(
        api.BitrixContactRequest(
            email="o@romashka.ru", name="Ольга", role="клиент", note="Ася: письмо про счёт"
        )
    )
    assert pushed == {"id": "7", "created": False, "commented": True, "portal": "acme.bitrix24.ru"}
    imported = api.crm_bitrix_import()
    assert imported["count"] == 1
    assert imported["contacts"][0]["email"] == "o@romashka.ru"
    journal = Journal(settings.db_path)
    try:
        events = journal.events(limit=10)
    finally:
        journal.close()
    kinds = [event["kind"] for event in events]
    assert "bitrix_contact" in kinds
    assert "bitrix_import" in kinds
    assert SECRET not in json.dumps(events, ensure_ascii=False)
