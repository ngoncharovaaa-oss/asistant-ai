# -*- coding: utf-8 -*-
"""Окна периодов для непрочитанных: сегодня, 3, 7, 30 дней или всё время.

Один словарь на почту, Telegram и личный чат, чтобы подписи и границы
окна совпадали. Окно включает текущий день: «3 дня» — сегодня и два
предыдущих.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

PERIODS: dict[str, tuple[str, int | None]] = {
    "today": ("за сегодня", 1),
    "3d": ("за 3 дня", 3),
    "7d": ("за 7 дней", 7),
    "30d": ("за 30 дней", 30),
    "all": ("за всё время", None),
}
DEFAULT_PERIOD = "all"


class PeriodError(ValueError):
    """Неизвестный ключ периода."""


def normalize_period(value: str | None) -> str:
    key = (value or "").strip().lower()
    return key if key in PERIODS else DEFAULT_PERIOD


def require_period(value: str | None) -> str:
    key = (value or "").strip().lower() or DEFAULT_PERIOD
    if key not in PERIODS:
        raise PeriodError("period: неизвестный период")
    return key


def period_title(value: str | None) -> str:
    return PERIODS[normalize_period(value)][0]


def period_days(value: str | None) -> int | None:
    return PERIODS[normalize_period(value)][1]


def period_cutoff(value: str | None, now: datetime | None = None) -> datetime | None:
    """Начало окна в локальном времени (aware); None — без ограничения."""
    days = period_days(value)
    if days is None:
        return None
    moment = now or datetime.now().astimezone()
    if moment.tzinfo is None:
        moment = moment.astimezone()
    start = datetime.combine(moment.date(), time.min, tzinfo=moment.tzinfo)
    return start - timedelta(days=days - 1)


def period_start_date(value: str | None, now: datetime | None = None) -> date | None:
    cutoff = period_cutoff(value, now)
    return cutoff.date() if cutoff else None


def periods_public() -> list[dict[str, str]]:
    return [{"id": key, "title": title} for key, (title, _days) in PERIODS.items()]


def aware(value: datetime) -> datetime:
    """Приводим дату сообщения к aware-времени: naive считаем локальным."""
    return value if value.tzinfo is not None else value.astimezone()
