# -*- coding: utf-8 -*-
"""Проверки слоя правил: всё выводится из файлов профиля и данных."""
from __future__ import annotations

from datetime import date, time

from app import rules
from app.config import Settings
from app.datasets import load_calendar, load_inbox, load_org, load_tasks
from app.profile import Profile


def _audit(settings: Settings, profile: Profile) -> dict:
    data = settings.data_dir
    return rules.audit(
        profile,
        load_inbox(data / "inbox.csv"),
        load_org(data / "org_structure.csv"),
        load_tasks(data / "tasks.csv"),
        load_calendar(data / "calendar.ics"),
    )


def _threads(result: dict) -> dict[str, dict]:
    return {item["email_id"]: item for item in result["email"]["threads"]}


def test_profile_rules_come_from_file(profile: Profile) -> None:
    ruleset = rules.load_rules(profile)
    assert len(ruleset.goals) == 3
    assert (ruleset.focus_min, ruleset.focus_max) == (8.0, 12.0)
    assert ruleset.plan_time == time(8, 30)
    assert (ruleset.review_weekday, ruleset.review_time) == (4, time(17, 0))
    assert {topic.risk for topic in ruleset.forbidden} == {
        rules.Risk.MONEY, rules.Risk.HR, rules.Risk.CLIENT, rules.Risk.LEADERSHIP,
    }
    assert {rule.kind for rule in ruleset.auto} == {
        "calendar", "news", "invites", "delegate", "digest",
    }
    assert ruleset.warnings == []


def test_threads_collapse_repeats(settings: Settings) -> None:
    emails = load_inbox(settings.data_dir / "inbox.csv")
    threads = rules.build_threads(emails)
    assert len(emails) == 80
    assert len(threads) == 17
    # повторные напоминания сворачиваются, но не теряются
    assert sum(thread.count for thread in threads) == 80
    assert max(thread.count for thread in threads) == 8


def test_decisions_match_expected_actions(settings: Settings, profile: Profile) -> None:
    result = _audit(settings, profile)
    evaluation = result["email"]["eval"]
    assert evaluation["threads_total"] == 17
    assert evaluation["thread_accuracy"] == 1.0, evaluation["misses"]
    assert evaluation["emails_total"] == 80
    assert evaluation["email_accuracy"] == 1.0, evaluation["misses"]

    by_action: dict[str, int] = {}
    for item in result["email"]["threads"]:
        by_action[item["action"]] = by_action.get(item["action"], 0) + item["count"]
    assert by_action == {
        "спросить_руководителя": 40,
        "делегировать": 9,
        "авто": 5,
        "в_сводку": 18,
        "игнорировать": 8,
    }


def test_sensitive_topics_never_auto(settings: Settings, profile: Profile) -> None:
    result = _audit(settings, profile)
    assert result["email"]["eval"]["sensitive_auto"] == []
    for item in result["email"]["threads"]:
        if item["risk"]:
            assert item["action"] == "спросить_руководителя", item["email_id"]

    threads = _threads(result)
    assert threads["m062"]["action"] == "спросить_руководителя"  # скидка 12%
    assert threads["m009"]["action"] == "спросить_руководителя"  # отпуск сотрудника
    assert threads["m001"]["action"] == "спросить_руководителя"  # сроки клиенту
    assert threads["m005"]["action"] == "спросить_руководителя"  # выступление спикером


def test_delegation_follows_org_structure(settings: Settings, profile: Profile) -> None:
    threads = _threads(_audit(settings, profile))
    assert threads["m002"]["action"] == "делегировать"
    assert threads["m002"]["target"] == "Ольга Смирнова"  # КП → директор по продажам
    assert threads["m044"]["action"] == "делегировать"
    assert threads["m044"]["target"] == "Мария Лебедева"  # жалоба → клиентский сервис
    assert threads["m025"]["action"] == "делегировать"
    assert threads["m025"]["target"] == "Екатерина Соколова"  # доступы → ассистент
    assert threads["m025"]["gates"], "доступ к CRM должен упираться в решение по кадрам"


def test_calendar_moves_and_focus(settings: Settings, profile: Profile) -> None:
    result = _audit(settings, profile)
    calendar = result["calendar"]

    assert len(calendar["changes"]) == 1
    change = calendar["changes"][0]
    assert change["status"] == "applied"
    assert change["to"].startswith("09.12 11:00")

    kinds = [block["kind"] for block in calendar["protected"]]
    assert kinds.count("ритм") == 12  # 10 планов дня + 2 итога недели
    assert kinds.count("один_на_один") == 2
    assert kinds.count("фокус") == 10

    assert set(calendar["focus_hours_week"]) == {"2026-12-07", "2026-12-14"}
    assert all(8.0 <= hours <= 12.0 for hours in calendar["focus_hours_week"].values())

    # фокус не наезжает на встречи и ритм
    events = load_calendar(settings.data_dir / "calendar.ics")
    imported = {(event.summary, event.start, event.end) for event in events}
    for block in calendar["focus"]:
        start = f"{block['date']} {block['start']}"
        assert start not in {f"{event[1]:%Y-%m-%d %H:%M}" for event in imported}
        assert block["hours"] >= 1.0

    conflicts = calendar["rhythm_conflicts"]
    assert len(conflicts) == 2
    assert {item["calendar_time"] for item in conflicts} == {"16:00"}
    assert {item["profile_time"] for item in conflicts} == {"пятница 17:00"}


def test_tasks_audit_finds_real_problems(settings: Settings, profile: Profile) -> None:
    tasks = _audit(settings, profile)["tasks"]
    assert tasks["as_of"] == "2026-12-11"
    assert tasks["overdue"] == ["t003", "t005"]
    assert {item["id"] for item in tasks["status_mismatches"]} == {"t001", "t003", "t005", "t007"}
    conflicts = tasks["deadline_conflicts"]
    assert any(
        item["task_id"] == "t011" and item["email_id"] == "m026" and item["email_date"] == "15.12"
        for item in conflicts
    )


def test_plan_for_next_business_day(settings: Settings, profile: Profile) -> None:
    result = _audit(settings, profile)
    plan = result["plan"]
    assert plan["date"] == date(2026, 12, 14).isoformat()
    assert plan["weekday"] == "понедельник"
    assert plan["plan_block"] == "08:30–09:00"
    assert 1 <= len(plan["approvals"]) <= 5
    assert len(plan["delegated"]) == 3
    assert plan["overdue"]