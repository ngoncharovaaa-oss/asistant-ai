# -*- coding: utf-8 -*-
"""Личный чат руководителя: сотрудники, периоды, непрочитанные и черновики."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException

import app.api as api
from app.chat import ChatError, ChatStore, reply_draft


def _iso(days_ago: int, hour: int = 10, minute: int = 0) -> str:
    moment = (datetime.now() - timedelta(days=days_ago)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return moment.isoformat(timespec="seconds")


@pytest.fixture
def chat(tmp_path: Path):
    store = ChatStore(tmp_path / "chat.db", seed=False)
    yield store
    store.close()


def test_chat_unread_and_periods(chat: ChatStore) -> None:
    chat.add("Ольга Смирнова", "Прошу согласовать скидку 12%.", at=_iso(0))
    chat.add("Анна Волкова", "Перенесла статус на среду.", at=_iso(0))
    chat.add("Мария Лебедева", "Клиент жалуется на сроки.", at=_iso(1))
    chat.add("Игорь Фёдоров", "Финалист готов к офферу.", at=_iso(5))
    chat.add("Екатерина Соколова", "Напоминаю про сессию.", at=_iso(12))

    today = chat.threads("today")
    assert today["unread"] == 2
    assert {item["employee"] for item in today["threads"]} == {"Ольга Смирнова", "Анна Волкова"}
    assert today["period"] == "today" and today["period_title"] == "за сегодня"

    week = chat.threads("7d")
    assert week["unread"] == 4
    assert {item["employee"] for item in week["threads"]} == {
        "Ольга Смирнова",
        "Анна Волкова",
        "Мария Лебедева",
        "Игорь Фёдоров",
    }

    month = chat.threads("30d")
    assert month["unread"] == 5
    names = {item["name"] for item in month["employees"]}
    assert {"Ольга Смирнова", "Игорь Фёдоров", "Екатерина Соколова"} <= names
    assert "Иван Петров" not in names


def test_chat_thread_draft_read_and_reply(chat: ChatStore) -> None:
    chat.add("Ольга Смирнова", "Согласуешь скидку 12% на контракт?")
    thread = chat.thread("Ольга Смирнова", "today")
    assert thread["unread"] == 1
    assert thread["draft"] is not None
    assert thread["draft"]["kind"] == "approval"
    assert thread["draft"]["reply"].startswith("Ольга, привет!")
    assert "0 автоотправок" in thread["draft"]["note"]

    assert chat.mark_read("Ольга Смирнова") == 1
    assert chat.threads("today")["unread"] == 0
    assert chat.thread("Ольга Смирнова", "today")["draft"] is None

    chat.add("Ольга Смирнова", "Ещё вопрос по бюджету.")
    chat.reply("Ольга Смирнова", "Отвечу до конца дня.")
    thread = chat.thread("Ольга Смирнова", "today")
    assert thread["messages"][-1]["direction"] == "out"
    assert thread["unread"] == 0


def test_chat_drafts_and_mark_all(chat: ChatStore) -> None:
    chat.add("Ольга Смирнова", "Прошу согласовать бюджет.")
    chat.add("Анна Волкова", "Подготовь план по e-com.")
    chat.add("Мария Лебедева", "Спасибо за помощь!")

    data = chat.drafts("today")
    assert data["total"] == 3
    assert len(data["drafts"]) == 3
    kinds = {item["employee"]: item["kind"] for item in data["drafts"]}
    assert kinds == {
        "Ольга Смирнова": "approval",
        "Анна Волкова": "task",
        "Мария Лебедева": "thanks",
    }
    assert all("0 автоотправок" in item["note"] for item in data["drafts"])

    assert chat.mark_read() == 3
    assert chat.threads("today")["unread"] == 0
    assert chat.drafts("today")["drafts"] == []


def test_chat_validation(chat: ChatStore) -> None:
    with pytest.raises(ChatError):
        chat.add("", "текст")
    with pytest.raises(ChatError):
        chat.add("Ольга Смирнова", "   ")
    with pytest.raises(ChatError):
        chat.add("Ольга Смирнова", "x" * 2001)
    with pytest.raises(ChatError):
        chat.reply("", "текст")
    with pytest.raises(ChatError):
        chat.thread("", "today")


def test_chat_seed_is_demo_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "seeded.db"
    store = ChatStore(path)
    try:
        data = store.threads("all")
        assert data["unread"] == 8
        assert len(data["threads"]) == 7
        assert all(item["demo"] for item in data["threads"])
    finally:
        store.close()
    again = ChatStore(path)
    try:
        data = again.threads("all")
        assert data["unread"] == 8
        assert len(data["threads"]) == 7
    finally:
        again.close()


def test_api_chat_flow(settings, monkeypatch) -> None:
    monkeypatch.setattr(api, "settings", settings)
    monkeypatch.setattr(api, "_chat_store", lambda: ChatStore(settings.db_path, seed=False))

    seed = ChatStore(settings.db_path, seed=False)
    try:
        seed.add("Ольга Смирнова", "Согласуешь скидку 12%?")
    finally:
        seed.close()

    data = api.chat_threads(period="today")
    assert data["unread"] == 1

    thread = api.chat_messages(employee="Ольга Смирнова", period="today")
    assert thread["draft"]["kind"] == "approval"

    added = api.chat_message_add(api.ChatMessageRequest(employee="Анна Волкова", text="Перенесла статус"))
    assert added["ok"] is True
    assert api.chat_threads(period="today")["unread"] == 2

    assert api.chat_read(employee="Анна Волкова")["marked"] == 1
    drafts = api.chat_drafts(period="today")
    assert len(drafts["drafts"]) == 1

    with pytest.raises(HTTPException) as excinfo:
        api.chat_threads(period="year")
    assert excinfo.value.status_code == 400
    with pytest.raises(HTTPException):
        api.chat_message_add(api.ChatMessageRequest(employee="Ольга Смирнова", text="   "))

    journal = api._journal()
    try:
        kinds = [event["kind"] for event in journal.events(limit=20)]
    finally:
        journal.close()
    assert "chat_message" in kinds and "chat_read" in kinds and "chat_drafts" in kinds


def test_reply_draft_is_neutral() -> None:
    reply, kind, reason = reply_draft("Ольга Смирнова", "Когда будет отчёт?")
    assert reply.startswith("Ольга, привет!")
    assert kind == "report" and reason
    reply, kind, _ = reply_draft("", "Привет")
    assert reply.startswith("Привет!")
    assert kind == "info"
