# -*- coding: utf-8 -*-
"""Голос Аси: системный русский синтез Windows (SAPI) с кэшем WAV по тексту.

Голос синтезируется локально, без клонирования и без отправки реплик наружу.
Текст передаётся в PowerShell только через файл UTF-8, поэтому реплика не может
стать частью команды. Файлы кэша лежат рядом с базой и в git не попадают.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from .config import Settings

RATE = 1
MAX_CHARS = 600
_LOCK = threading.Lock()


class VoiceError(RuntimeError):
    """Синтез недоступен; текст ошибки не содержит путей и секретов."""


def clean(text: str) -> str:
    return " ".join((text or "").split())


def cache_dir(settings: Settings) -> Path:
    return settings.db_path.parent / "voice"


def _key(text: str) -> str:
    return hashlib.sha1(f"sapi|{RATE}|{text}".encode("utf-8")).hexdigest()


def _quote(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _script(text_file: Path, wav_file: Path) -> str:
    return (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Rate = {RATE}; "
        "$v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'ru*' } "
        "| Select-Object -First 1; "
        "if ($v) { $s.SelectVoice($v.VoiceInfo.Name) }; "
        f"$s.SetOutputToWaveFile({_quote(wav_file)}); "
        f"$s.Speak([IO.File]::ReadAllText({_quote(text_file)}, [Text.Encoding]::UTF8)); "
        "$s.Dispose()"
    )


def synthesize(settings: Settings, text: str) -> Path:
    text = clean(text)
    if not text or len(text) > MAX_CHARS:
        raise VoiceError("voice: text out of range")
    if not sys.platform.startswith("win"):
        raise VoiceError("voice: system speech synthesis is windows only")
    folder = cache_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (_key(text) + ".wav")
    if target.is_file() and target.stat().st_size > 1000:
        return target
    with _LOCK:
        if target.is_file() and target.stat().st_size > 1000:
            return target
        tag = uuid.uuid4().hex[:8]
        text_file = folder / (_key(text) + f".{tag}.txt")
        wav_file = folder / (_key(text) + f".{tag}.part.wav")
        text_file.write_text(text, encoding="utf-8")
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", _script(text_file, wav_file)],
                capture_output=True,
                timeout=90,
                creationflags=flags,
                check=False,
            )
            if not wav_file.is_file() or wav_file.stat().st_size <= 1000:
                raise VoiceError("voice: system speech synthesis failed")
            os.replace(wav_file, target)
            return target
        except (OSError, subprocess.SubprocessError):
            raise VoiceError("voice: system speech synthesis failed") from None
        finally:
            for leftover in (text_file, wav_file):
                try:
                    leftover.unlink()
                except OSError:
                    pass
