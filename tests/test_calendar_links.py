# -*- coding: utf-8 -*-
"""Связь встреч календаря с прошедшими протоколами по теме."""
from __future__ import annotations

from app.calendar_links import MEETING_PROTOCOLS, protocol_id_for, topic_for
from app.config import Settings
from app.datasets import load_calendar


def test_every_calendar_event_links_to_protocol(settings: Settings) -> None:
    events = load_calendar(settings.data_dir / "calendar.ics")
    linked = set(MEETING_PROTOCOLS.values())
    assert linked == {"2026-01-15", "2026-01-26", "2026-06-04", "2026-07-14", "2026-08-25"}
    for event in events:
        protocol_id = protocol_id_for(event.summary, event.category)
        assert protocol_id in linked, event.summary
        assert topic_for(protocol_id), event.summary


def test_topic_mapping_examples() -> None:
    assert protocol_id_for("Бюджет и прогноз", "деньги") == "2026-07-14"
    assert protocol_id_for("Синк с ИТ", "операционная") == "2026-01-15"
    assert protocol_id_for("Согласование КП", "операционная") == "2026-01-26"
    assert protocol_id_for("Встреча с партнёром «Облако Плюс»", "партнёр") == "2026-08-25"
    assert protocol_id_for("Маркетинг: кампании", "операционная") == "2026-06-04"


def test_unknown_meeting_falls_back_to_category() -> None:
    assert protocol_id_for("Неизвестная встреча", "клиент") == "2026-01-26"
    assert protocol_id_for("Неизвестная встреча", "деньги") == "2026-07-14"
    assert protocol_id_for("Неизвестная встреча", "") == ""
