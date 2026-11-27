# -*- coding: utf-8 -*-
"""Экономика месяца: ручная обработка против агента с подтверждением.

Модель считает два контура: почту (50–100 писем в день) и встречи
(25–40 в неделю, протокол и поручения). Диапазон объёма задаётся в Settings,
базовый сценарий берёт середину диапазона.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Settings


@dataclass
class Economics:
    requests: int
    autonomous_share: float
    drafts: int
    manual_minutes: int
    check_minutes: int
    hourly_rate: int
    manual_hours: float
    check_hours: float
    manual_cost: int
    review_cost: int
    token_cost: int
    infra_cost: int
    agent_cost: int
    saving: int
    saving_share: float
    cost_per_draft: float
    cost_per_request: float
    implementation_cost: int
    payback_months: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "requests_per_month": self.requests,
            "autonomous_share": round(self.autonomous_share, 2),
            "drafts_per_month": self.drafts,
            "manual_minutes": self.manual_minutes,
            "check_minutes": self.check_minutes,
            "hourly_rate": self.hourly_rate,
            "manual_hours": round(self.manual_hours, 1),
            "check_hours": round(self.check_hours, 1),
            "manual_cost": self.manual_cost,
            "review_cost": self.review_cost,
            "token_cost": self.token_cost,
            "infra_cost": self.infra_cost,
            "agent_cost": self.agent_cost,
            "saving": self.saving,
            "saving_share": round(self.saving_share, 3),
            "cost_per_draft": round(self.cost_per_draft, 1),
            "cost_per_request": round(self.cost_per_request, 1),
            "implementation_cost": self.implementation_cost,
            "payback_months": round(self.payback_months, 1),
        }


def compute(
    settings: Settings,
    autonomous_share: float = 0.5,
    drafts: int | None = None,
    requests: int | None = None,
) -> Economics:
    requests = requests if requests is not None else settings.monthly_requests
    drafts = drafts if drafts is not None else int(round(requests * autonomous_share))
    manual_hours = drafts * settings.manual_minutes / 60
    check_hours = drafts * settings.check_minutes / 60
    manual_cost = int(round(manual_hours * settings.hourly_rate))
    review_cost = int(round(check_hours * settings.hourly_rate))
    input_tokens = (
        requests * settings.classify_tokens[0]
        + drafts * settings.draft_tokens[0]
        + settings.digests_per_month * settings.digest_tokens[0]
    )
    output_tokens = (
        requests * settings.classify_tokens[1]
        + drafts * settings.draft_tokens[1]
        + settings.digests_per_month * settings.digest_tokens[1]
    )
    token_cost = int(
        round(
            input_tokens * settings.token_input_price
            + output_tokens * settings.token_output_price
        )
    )
    infra_cost = int(round(drafts / 150 * settings.infra_per_150_drafts))
    agent_cost = review_cost + token_cost + infra_cost
    saving = manual_cost - agent_cost
    saving_share = saving / manual_cost if manual_cost else 0.0
    payback = settings.implementation_cost / saving if saving > 0 else 0.0
    return Economics(
        requests=requests,
        autonomous_share=drafts / requests if requests else 0.0,
        drafts=drafts,
        manual_minutes=settings.manual_minutes,
        check_minutes=settings.check_minutes,
        hourly_rate=settings.hourly_rate,
        manual_hours=manual_hours,
        check_hours=check_hours,
        manual_cost=manual_cost,
        review_cost=review_cost,
        token_cost=token_cost,
        infra_cost=infra_cost,
        agent_cost=agent_cost,
        saving=saving,
        saving_share=saving_share,
        cost_per_draft=agent_cost / drafts if drafts else 0.0,
        cost_per_request=agent_cost / requests if requests else 0.0,
        implementation_cost=settings.implementation_cost,
        payback_months=payback,
    )


def meeting_contour(settings: Settings) -> dict:
    """Встречи: протокол и поручения вместо ручных заметок."""
    weeks = settings.workdays_per_month / 5
    per_week = {
        "min": settings.meetings_per_week_min,
        "max": settings.meetings_per_week_max,
        "avg": (settings.meetings_per_week_min + settings.meetings_per_week_max) / 2,
    }
    per_month = {name: int(round(value * weeks)) for name, value in per_week.items()}
    rate = settings.hourly_rate

    def money(count: int, minutes: int) -> int:
        return int(round(count * minutes / 60 * rate))

    manual = {name: money(count, settings.meeting_manual_minutes) for name, count in per_month.items()}
    review = {name: money(count, settings.meeting_review_minutes) for name, count in per_month.items()}
    saving = {name: manual[name] - review[name] for name in per_month}
    hours = {name: round(count * settings.meeting_minutes / 60, 1) for name, count in per_month.items()}
    return {
        "per_week": per_week,
        "per_month": per_month,
        "weeks_per_month": round(weeks, 2),
        "minutes_per_meeting": settings.meeting_minutes,
        "manual_minutes": settings.meeting_manual_minutes,
        "review_minutes": settings.meeting_review_minutes,
        "hours_per_month": hours,
        "manual_cost": manual,
        "review_cost": review,
        "saving": saving,
    }


def build_report(settings: Settings, autonomous_share: float = 0.5) -> dict:
    """Сценарии min/avg/max по почте плюс контур встреч."""
    counts = {
        "min": settings.emails_per_day_min * settings.workdays_per_month,
        "avg": settings.monthly_requests,
        "max": settings.emails_per_day_max * settings.workdays_per_month,
    }
    scenarios = {
        name: compute(settings, autonomous_share=autonomous_share, requests=requests)
        for name, requests in counts.items()
    }
    meetings = meeting_contour(settings)
    implementation = settings.implementation_cost

    def payback(saving: int) -> float:
        return implementation / saving if saving > 0 else 0.0

    total_cost = {
        name: scenarios[name].agent_cost + meetings["review_cost"][name] for name in scenarios
    }
    total_saving = {
        name: scenarios[name].saving + meetings["saving"][name] for name in scenarios
    }

    payload = scenarios["avg"].as_dict()
    payload.update(
        emails_per_day_min=settings.emails_per_day_min,
        emails_per_day_max=settings.emails_per_day_max,
        workdays_per_month=settings.workdays_per_month,
        requests_per_month_min=scenarios["min"].requests,
        requests_per_month_max=scenarios["max"].requests,
        drafts_per_month_min=scenarios["min"].drafts,
        drafts_per_month_max=scenarios["max"].drafts,
        manual_cost_min=scenarios["min"].manual_cost,
        manual_cost_max=scenarios["max"].manual_cost,
        agent_cost_min=scenarios["min"].agent_cost,
        agent_cost_max=scenarios["max"].agent_cost,
        saving_min=scenarios["min"].saving,
        saving_max=scenarios["max"].saving,
        payback_months_min=round(payback(scenarios["max"].saving), 1),
        payback_months_max=round(payback(scenarios["min"].saving), 1),
        total_agent_cost_min=total_cost["min"],
        total_agent_cost_avg=total_cost["avg"],
        total_agent_cost_max=total_cost["max"],
        total_saving_min=total_saving["min"],
        total_saving_avg=total_saving["avg"],
        total_saving_max=total_saving["max"],
        total_payback_months_min=round(payback(total_saving["max"]), 1),
        total_payback_months_max=round(payback(total_saving["min"]), 1),
        meetings=meetings,
        scenarios={name: scenario.as_dict() for name, scenario in scenarios.items()},
    )
    return payload


def _rub(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " ₽"


def _rub_range(low: int, high: int) -> str:
    return _rub(low) if low == high else f"{_rub(low)[:-2]}–{_rub(high)}"


def _spaced(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _ru(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


def explain(settings: Settings, report: dict) -> list[str]:
    meetings = report["meetings"]
    return [
        f"Почта: {report['emails_per_day_min']}–{report['emails_per_day_max']} писем в день × "
        f"{report['workdays_per_month']} рабочий день = "
        f"{_spaced(report['requests_per_month_min'])}–{_spaced(report['requests_per_month_max'])} обращений в месяц; "
        f"{_spaced(report['drafts_per_month_min'])}–{_spaced(report['drafts_per_month_max'])} черновиков "
        f"при автодоле {report['autonomous_share']:.0%}.",
        f"Почта вручную: {_rub_range(report['manual_cost_min'], report['manual_cost_max'])}; "
        f"агент: {_rub_range(report['agent_cost_min'], report['agent_cost_max'])} "
        f"({_ru(report['cost_per_draft'])} ₽ за черновик).",
        f"Экономия по почте: {_rub_range(report['saving_min'], report['saving_max'])} в месяц "
        f"({report['saving_share']:.0%}).",
        f"Встречи: {meetings['per_week']['min']}–{meetings['per_week']['max']} в неделю = "
        f"{meetings['per_month']['min']}–{meetings['per_month']['max']} протоколов в месяц; "
        f"проверка {meetings['review_minutes']} мин "
        f"({_rub_range(meetings['review_cost']['min'], meetings['review_cost']['max'])}) против "
        f"{meetings['manual_minutes']} мин вручную "
        f"({_rub_range(meetings['manual_cost']['min'], meetings['manual_cost']['max'])}), "
        f"экономия {_rub_range(meetings['saving']['min'], meetings['saving']['max'])}.",
        f"Итого с встречами: {_rub_range(report['total_saving_min'], report['total_saving_max'])} в месяц; "
        f"внедрение {_rub(settings.implementation_cost)} окупается за "
        f"{_ru(report['total_payback_months_min'])}–{_ru(report['total_payback_months_max'])} мес.",
    ]
