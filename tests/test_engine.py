# -*- coding: utf-8 -*-
"""Проверки движка на датасете ИИкузницы."""
from __future__ import annotations

from datetime import datetime

from app import economics as econ
from app.classify import classify
from app.config import Settings
from app.datasets import load_calendar, load_inbox, load_org, load_tasks
from app.journal import Journal
from app.meetings import parse_all
from app.models import SENSITIVE_RISKS, Action, Email
from app.pipeline import run
from app.profile import load_profile


def _email(subject: str, body: str, when: datetime, role: str = "подчинённый") -> Email:
    return Email(
        id="test-1",
        received=when,
        from_name="Тест",
        from_email="test@example.com",
        sender_role=role,
        to="leader@example.com",
        subject=subject,
        body=body,
    )


def test_dataset_shapes(settings: Settings) -> None:
    emails = load_inbox(settings.data_dir / "inbox.csv")
    events = load_calendar(settings.data_dir / "calendar.ics")
    tasks = load_tasks(settings.data_dir / "tasks.csv")
    protocols = parse_all(settings.data_dir / "meetings")
    assert len(emails) == 80
    assert len(events) == 50
    assert len(tasks) == 12
    assert len(protocols) == 5


def test_classification_matches_expected(settings: Settings) -> None:
    result = run(settings)
    accuracy = result.metrics["accuracy"]
    assert accuracy["total"] == 80
    assert accuracy["accuracy"] == 1.0, accuracy["misses"]
    assert result.metrics["actions"] == {
        "спросить_руководителя": 40,
        "делегировать": 9,
        "авто": 5,
        "в_сводку": 18,
        "игнорировать": 8,
    }


def test_sensitive_topics_never_auto(settings: Settings, profile) -> None:
    org = load_org(settings.data_dir / "org_structure.csv")
    tasks = load_tasks(settings.data_dir / "tasks.csv")
    events = load_calendar(settings.data_dir / "calendar.ics")
    emails = load_inbox(settings.data_dir / "inbox.csv")
    result = run(settings)
    for item in result.classifications:
        if set(item.risks) & SENSITIVE_RISKS:
            assert item.action == Action.ASK, item.email_id
    assert result.metrics["safety_violations"] == 0

    money = _email("Скидка 12% для «Ромашка-Ритейл»", "Нужно согласование скидки.", datetime(2026, 12, 7, 9, 0))
    assert classify(money, profile, org, tasks, events).action == Action.ASK

    newsletter = _email(
        "Новости продуктов за декабрь",
        "Дайджест продуктовых новостей.",
        datetime(2026, 12, 7, 9, 0),
        role="рассылка",
    )
    assert classify(newsletter, profile, org, tasks, events).action == Action.IGNORE

    # чувствительная тема в календарном письме не уходит в авто
    protected_event = next(event for event in events if event.category == "клиент")
    protected = _email(
        "Перенос встречи с клиентом",
        "Можем сдвинуть стратегическую встречу с клиентом на час?",
        protected_event.start.replace(hour=9, minute=0),
    )
    assert classify(protected, profile, org, tasks, events).action == Action.ASK

    operational = _email(
        "Перенос встречи по e-com",
        "Можем сдвинуть статус e-commerce с понедельника на среду 11:00?",
        datetime(2026, 12, 7, 10, 34),
    )
    assert classify(operational, profile, org, tasks, events).action == Action.AUTO


def test_drafts_are_cited_and_never_sent(settings: Settings) -> None:
    result = run(settings)
    assert result.metrics["drafts"] == 54
    assert result.metrics["auto_sends"] == 0
    assert result.metrics["citations_coverage"] == 1.0
    assert all(draft.sources for draft in result.drafts)
    assert all(draft.sent is False for draft in result.drafts)


def test_approval_is_the_only_send_point(settings: Settings) -> None:
    run(settings)
    journal = Journal(settings.db_path)
    try:
        drafts = [item for item in journal.drafts() if item["status"] == "waiting_approval"]
        assert len(drafts) == 54
        first, second = drafts[0]["id"], drafts[1]["id"]
        assert journal.approve(first)["sent"] is True
        assert journal.draft(first)["sent"] is True
        assert journal.draft(second)["sent"] is False
        assert journal.approve(first)["sent"] is True  # идемпотентно
        assert journal.reject(second)["ok"] is True
        assert journal.approve(second)["ok"] is False
        assert journal.draft(second)["sent"] is False
        assert journal.draft(second)["status"] == "rejected"
    finally:
        journal.close()


def test_learning_on_edit_changes_next_drafts(settings: Settings) -> None:
    first = run(settings)
    sales = [draft for draft in first.drafts if draft.topic == "продажи"]
    assert sales, "в датасете должны быть запросы КП"
    target = sales[0]
    learned = "Ольга, добрый день. КП подготовим до пятницы, вернусь со сроком. — Иван"
    journal = Journal(settings.db_path)
    try:
        assert journal.save_edit(target.id, learned)["ok"] is True
        assert "продажи" in journal.learning_hints()
    finally:
        journal.close()
    second = run(settings)
    updated = [draft for draft in second.drafts if draft.topic == "продажи"]
    assert updated
    assert all(draft.body == learned for draft in updated)


def test_protocols_parse_decisions_tasks_questions(settings: Settings) -> None:
    protocols = parse_all(settings.data_dir / "meetings")
    metrics = run(settings).metrics
    assert metrics["protocols"] == 5
    assert metrics["protocol_tasks"] >= 30
    assert metrics["protocol_open_questions"] >= 12
    by_id = {protocol.id: protocol for protocol in protocols}
    january = by_id["2026-01-26"]
    owners = {task.owner for task in january.tasks}
    assert "Настя" in owners
    assert "Яна, Саша" in owners
    july = by_id["2026-07-14"]
    assert any(task.owner == "Speaker 2" for task in july.tasks)
    assert any(task.owner == "Маша, Ваня" for task in july.tasks)
    assert july.decisions
    assert july.open_questions


def test_second_leader_without_code(settings: Settings) -> None:
    profile = load_profile(settings.profiles_dir / "leader2.md")
    assert profile.name == "Мария Орлова"
    assert profile.role == "коммерческий директор"
    result = run(settings, profile_path=settings.profiles_dir / "leader2.md")
    assert result.metrics["emails"] == 80
    assert result.metrics["auto_sends"] == 0
    assignments = [draft for draft in result.drafts if draft.kind == "assignment"]
    assert assignments
    assert any("Мария Орлова" in draft.body for draft in assignments)


def test_economics_matches_calculator() -> None:
    model = econ.compute(Settings())
    assert model.drafts == 175
    assert model.manual_cost == 87_500
    assert model.agent_cost == 18_279
    assert model.saving == 69_221
    assert round(model.cost_per_draft, 1) == 104.5
    assert round(model.cost_per_request, 1) == 52.2
    assert round(model.payback_months, 1) == 1.4


def test_morning_and_weekly_digest(settings: Settings) -> None:
    result = run(settings)
    morning = result.morning
    assert morning["date"] == "2026-12-07"
    assert morning["events"]
    assert len(morning["focus"]) <= 4
    assert morning["decisions"]
    digest = result.digest
    assert digest["emails"] == 80
    assert digest["without_leader"] == 40
    assert digest["meetings"] == 25
    assert digest["protected_hours"] > 0
    assert digest["protocols"]
    assert len(digest["tasks_overdue"]) >= 1
