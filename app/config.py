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
    monthly_requests: int = 350
    manual_minutes: int = 12
    check_minutes: int = 2
    hourly_rate: int = 2500
    token_price_per_draft: float = 196.0 / 175.0
    infra_per_150_drafts: int = 3000
    implementation_cost: int = 100_000
    llm_model: str = "offline-rules"
    env: dict[str, str] = field(default_factory=dict, repr=False)

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
        if env.get("MONTHLY_REQUESTS"):
            s.monthly_requests = int(env["MONTHLY_REQUESTS"])
        if env.get("LLM_MODEL") and not s.demo_mode:
            s.llm_model = env["LLM_MODEL"]
        return s

    def has_llm_key(self) -> bool:
        key = self.env.get("LLM_API_KEY", "")
        return bool(key) and not key.startswith("sk-your")

    def api_key(self) -> str:
        """Только для вызова провайдера. Не логировать и не возвращать наружу."""
        return self.env.get("LLM_API_KEY", "")
