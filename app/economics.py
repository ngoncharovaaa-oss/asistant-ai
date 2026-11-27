# -*- coding: utf-8 -*-
"""Экономика месяца: ручная обработка против агента с подтверждением.

Модель совпадает с калькулятором даки и считает только почтовый контур:
ручная обработка N обращений, затем проверка черновиков руководителем.
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


def compute(settings: Settings, autonomous_share: float = 0.5, drafts: int | None = None) -> Economics:
    requests = settings.monthly_requests
    drafts = drafts if drafts is not None else int(round(requests * autonomous_share))
    manual_hours = drafts * settings.manual_minutes / 60
    check_hours = drafts * settings.check_minutes / 60
    manual_cost = int(round(manual_hours * settings.hourly_rate))
    review_cost = int(round(check_hours * settings.hourly_rate))
    token_cost = int(round(drafts * settings.token_price_per_draft))
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


def _rub(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " ₽"


def _ru(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


def explain(econ: Economics) -> list[str]:
    return [
        f"{econ.drafts} черновиков в месяц: {econ.requests} обращений × {econ.autonomous_share:.0%}.",
        f"Вручную {_ru(econ.manual_hours)} ч × {_rub(econ.hourly_rate)} = {_rub(econ.manual_cost)}.",
        f"Агент: проверка {_ru(econ.check_hours)} ч ({_rub(econ.review_cost)}) + токены {_rub(econ.token_cost)} "
        f"+ инфраструктура {_rub(econ.infra_cost)} = {_rub(econ.agent_cost)}.",
        f"Экономия {_rub(econ.saving)} в месяц ({econ.saving_share:.0%}); внедрение {_rub(econ.implementation_cost)} "
        f"окупается за {_ru(econ.payback_months)} мес.",
    ]
