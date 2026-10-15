"""Recordings for tests: committed fixtures, and files made with the real ffmpeg."""

from __future__ import annotations

import re
import shutil
import subprocess
import unicodedata
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
EN_SAMPLE = Path(__file__).resolve().parents[2] / "minutenest" / "webapp" / "static" / "sample.mp3"
RU_SPEECH = FIXTURES / "ru_speech.mp3"       # two FLEURS sentences, CC-BY-4.0
RU_TEXT = (FIXTURES / "ru_speech.txt").read_text(encoding="utf-8")
EN_SAMPLE_TEXT = (FIXTURES / "sample_en.txt").read_text(encoding="utf-8")


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def copy_as(src: Path, dst: Path) -> Path:
    """The same recording under the name a user gave it."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return dst


def silence(dst: Path, seconds: int = 15) -> Path:
    ffmpeg("-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", str(seconds), str(dst))
    return dst


def left_channel_only(src: Path, dst: Path) -> Path:
    """A one-microphone stereo recording: the voice on the left, nothing on the right."""
    ffmpeg("-i", str(src), "-af", "pan=stereo|c0=c0|c1=0*c0", "-ac", "2", str(dst))
    return dst


def repeated(src: Path, dst: Path, times: int) -> Path:
    """A longer recording made of the same speech several times over."""
    ffmpeg("-stream_loop", str(times - 1), "-i", str(src), "-c", "copy", str(dst))
    return dst


def not_media(dst: Path) -> Path:
    dst.write_text("meeting agenda, not a recording\n", encoding="utf-8")
    return dst


def big_file(dst: Path, megabytes: int) -> Path:
    """Large enough that adding it takes a while on a throttled connection."""
    with dst.open("wb") as f:
        f.truncate(megabytes * 1024 * 1024)
    return dst


def _stems(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", text).lower().replace("ё", "е")
    return {w[:5] for w in re.findall(r"\w+", text) if len(w) >= 5}


def share_heard(reference: str, transcript: str) -> float:
    """Share of the reference's content words (5+ letters, compared by their
    first 5 letters) that the transcript contains."""
    ref = _stems(reference)
    return len(ref & _stems(transcript)) / len(ref)


def _then(src: Path, dst: Path, trouble: str) -> Path:
    """The speech said clearly, then a stretch of the same speech spoiled by
    `trouble` (an ffmpeg filter): a meeting with one bad part."""
    ffmpeg("-i", str(src), "-filter_complex",
           f"[0:a]asplit[clear][bad];[bad]{trouble}[spoiled];[clear][spoiled]concat=n=2:v=0:a=1",
           str(dst))
    return dst


def in_an_echoing_room(src: Path, dst: Path) -> Path:
    """The speech, then the same speech twice over in a room with a strong echo.
    Whisper tiny used to loop on the echoing part ("Сезавра, Сезавра, ...")."""
    return _then(src, dst, "aecho=0.8:0.9:500|900:0.5|0.4,aloop=loop=2:size=2e6")
