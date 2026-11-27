# -*- coding: utf-8 -*-
"""Постоянные комнаты Zoom и Телемоста, сохранённые из дашборда.

Ссылка вставляется один раз в панель канала и ложится в data/rooms.json
с правами 0600; файл в git не попадает. Через API наружу отдаётся только
факт «сохранена», сам адрес показывается в результате создания встречи.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

from .config import Settings

PROVIDERS = ("zoom", "telemost")
HOSTS = {"zoom": "zoom.us", "telemost": "telemost.yandex.ru"}


class LinkError(ValueError):
    """Ссылка не подходит: другой хост или нет пути комнаты."""


def _host_ok(provider: str, host: str) -> bool:
    if provider == "zoom":
        return host == "zoom.us" or host.endswith(".zoom.us")
    return host == "telemost.yandex.ru"


def normalize(provider: str, value: str) -> str:
    if provider not in PROVIDERS:
        raise LinkError("provider must be zoom or telemost")
    text = (value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not _host_ok(provider, host):
        raise LinkError(f"{provider}: link must be https://{HOSTS[provider]}/…")
    paths = ("/j/",) if provider == "telemost" else ("/j/", "/my/", "/w/", "/wc/")
    if not parsed.path.startswith(paths):
        raise LinkError(f"{provider}: link must open a room")
    return text


def path(settings: Settings) -> Path:
    return settings.db_path.parent / "rooms.json"


def load(settings: Settings) -> dict[str, str]:
    file = path(settings)
    if not file.exists():
        return {}
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items() if key in PROVIDERS and isinstance(value, str)}


def _write(settings: Settings, data: dict[str, str]) -> None:
    file = path(settings)
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, file)


def saved(settings: Settings, provider: str) -> str:
    return load(settings).get(provider, "")


def set_link(settings: Settings, provider: str, value: str) -> str:
    text = normalize(provider, value)
    data = load(settings)
    if text:
        data[provider] = text
    else:
        data.pop(provider, None)
    _write(settings, data)
    return text
