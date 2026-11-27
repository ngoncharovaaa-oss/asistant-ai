# -*- coding: utf-8 -*-
"""Конфигурация AsiStantAI. Секреты читаются из .env и никогда не логируются."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def load_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


@dataclass
class Settings:
    data_dir: Path = BASE_DIR / "data" / "kuznitsa"
    profiles_dir: Path = BASE_DIR / "profiles"
    out_dir: Path = BASE_DIR / "out"
    db_path: Path = BASE_DIR / "data" / "asistant.db"
    demo_mode: bool = True
    confidence_threshold: float = 0.75
    run_token_budget: int = 60_000
    emails_per_day_min: int = 50
    emails_per_day_max: int = 100
    workdays_per_month: int = 21
    meetings_per_week_min: int = 25
    meetings_per_week_max: int = 40
    meeting_minutes: int = 45
    meeting_manual_minutes: int = 10
    meeting_review_minutes: int = 2
    manual_minutes: int = 12
    check_minutes: int = 2
    hourly_rate: int = 2500
    token_input_price: float = 200.0 / 1_000_000
    token_output_price: float = 500.0 / 1_000_000
    classify_tokens: tuple[int, int] = (800, 80)
    draft_tokens: tuple[int, int] = (2500, 500)
    digest_tokens: tuple[int, int] = (5000, 800)
    digests_per_month: int = 4
    infra_per_150_drafts: int = 3000
    implementation_cost: int = 100_000
    llm_model: str = "offline-rules"
    env: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def monthly_requests(self) -> int:
        """Средний поток для базового сценария: середина диапазона писем в день."""
        mid = (self.emails_per_day_min + self.emails_per_day_max) / 2
        return int(round(mid * self.workdays_per_month))

    @classmethod
    def load(cls, env_path: Path | None = None) -> "Settings":
        env_path = env_path or BASE_DIR / ".env"
        env = load_env(env_path)
        s = cls(env=env)
        if env.get("DATASET_DIR"):
            s.data_dir = Path(env["DATASET_DIR"]).expanduser().resolve()
        if env.get("OUT_DIR"):
            s.out_dir = Path(env["OUT_DIR"]).expanduser().resolve()
        if env.get("DATABASE_PATH"):
            s.db_path = Path(env["DATABASE_PATH"]).expanduser().resolve()
        if env.get("DEMO_MODE"):
            s.demo_mode = env["DEMO_MODE"] not in {"0", "false", "False"}
        if env.get("CONFIDENCE_THRESHOLD"):
            s.confidence_threshold = float(env["CONFIDENCE_THRESHOLD"])
        if env.get("RUN_TOKEN_BUDGET"):
            s.run_token_budget = int(env["RUN_TOKEN_BUDGET"])
        if env.get("EMAILS_PER_DAY_MIN"):
            s.emails_per_day_min = int(env["EMAILS_PER_DAY_MIN"])
        if env.get("EMAILS_PER_DAY_MAX"):
            s.emails_per_day_max = int(env["EMAILS_PER_DAY_MAX"])
        if env.get("WORKDAYS_PER_MONTH"):
            s.workdays_per_month = int(env["WORKDAYS_PER_MONTH"])
        if env.get("MEETINGS_PER_WEEK_MIN"):
            s.meetings_per_week_min = int(env["MEETINGS_PER_WEEK_MIN"])
        if env.get("MEETINGS_PER_WEEK_MAX"):
            s.meetings_per_week_max = int(env["MEETINGS_PER_WEEK_MAX"])
        if env.get("LLM_MODEL") and not s.demo_mode:
            s.llm_model = env["LLM_MODEL"]
        return s

    def has_llm_key(self) -> bool:
        key = self.env.get("LLM_API_KEY", "")
        return bool(key) and not key.startswith("sk-your")

    def api_key(self) -> str:
        """Только для вызова провайдера. Не логировать и не возвращать наружу."""
        return self.env.get("LLM_API_KEY", "")
