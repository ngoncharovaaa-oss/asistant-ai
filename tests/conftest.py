# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.profile import load_profile

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "kuznitsa"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings.load()
    s.data_dir = DATA
    s.profiles_dir = ROOT / "profiles"
    s.out_dir = tmp_path / "out"
    s.db_path = tmp_path / "asistant.db"
    s.env = {}
    return s


@pytest.fixture
def profile():
    return load_profile(DATA / "leader_profile.md")
