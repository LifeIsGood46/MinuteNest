"""Audio extraction, preprocessing, and optional chunking via ffmpeg."""

from __future__ import annotations

import logging
import subprocess  # nosec B404 - argument lists only, no shell; each call is marked
import sys
from dataclasses import dataclass
from typing import Any
from pathlib import Path

from .ffbin import FFmpegMissing, find_ffmpeg

log = logging.getLogger("minutenest.audio")


@dataclass
class AudioPrepResult:
    """Result of preparing input audio."""

    wav_path: Path
    duration_s: float
    source: Path


def _binary(name: str) -> str:
    p = find_ffmpeg(name)
    if not p:
        raise FFmpegMissing(name)
    return str(p)


# ffmpeg/ffprobe never run longer than this (Power of Ten rule 2: every wait has a
# bound). Extracting a 6-hour recording takes minutes; a process still running
# after this has hung on a broken file.
FFMPEG_MAX_S = 4 * 3600
POLL_S = 0.3


def _run(cmd: list[str], cancel_cb=None) -> subprocess.CompletedProcess:
    kwargs: dict[str, Any] = {"capture_output": True, "text": True}
    if sys.platform.startswith("win"):
        # Prevent console window popups in windowed exe
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log.debug("run: %s", " ".join(cmd))
    try:
        if cancel_cb is None:
            # the bundled ffmpeg/ffprobe (ffbin) with an argument list: no shell
            r = subprocess.run(cmd, check=False, timeout=FFMPEG_MAX_S, **kwargs)  # noqa: S603  # nosec B603
        else:
            r = _run_cancellable(cmd, kwargs, cancel_cb)
    except FileNotFoundError:
        raise FFmpegMissing(cmd[0]) from None
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"{Path(cmd[0]).stem} did not finish within {FFMPEG_MAX_S // 3600} hours; "
                           "the file may be damaged.") from e
    if r.returncode != 0:
        log.warning("ffmpeg exit %s: %s", r.returncode, (r.stderr or "").strip()[-500:])
    return r


def _run_cancellable(cmd, kwargs, cancel_cb) -> subprocess.CompletedProcess:
    """Like subprocess.run, but kills the process as soon as Stop is pressed."""
    from .errors import CancelledError

    kw = {k: v for k, v in kwargs.items() if k not in ("capture_output", "check")}
    # the bundled ffmpeg (ffbin) with an argument list: no shell
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)  # noqa: S603  # nosec B603
    for _ in range(int(FFMPEG_MAX_S / POLL_S)):
        try:
            out, err = proc.communicate(timeout=POLL_S)
            return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            if cancel_cb():
                proc.kill()
                proc.communicate()
                raise CancelledError("cancelled by user") from None
    proc.kill()
    proc.communicate()
    raise subprocess.TimeoutExpired(cmd, FFMPEG_MAX_S)


def probe_duration(path: Path) -> float:
    """Return duration in seconds via ffprobe (0.0 on failure)."""
    r = _run(
        [
            _binary("ffprobe"), "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def extract_audio(
    source: Path,
    out_wav: Path,
    *,
    normalize: bool = True,
    sample_rate: int = 16000,
    cancel_cb=None,
    name: str | None = None,  # the user's file name, when `source` is a stored copy
) -> AudioPrepResult:
    """Extract audio from any ffmpeg-supported input into 16kHz mono WAV.

    Optionally applies loudness normalization + a gentle highpass filter,
    which noticeably improves meeting-recording transcription quality.
    """
    out_wav.parent.mkdir(parents=True, exist_ok=True)

    # Downmix + resample FIRST: the filters then process one 16 kHz channel
    # instead of 48 kHz stereo/5.1. loudnorm dominated extraction (16 s for a
    # 10-min recording, 34 s for 22 min); this is ~2.4x faster. aformat (not
    # pan) downmixes any layout, including mono files and 5.1 video.
    filters = [f"aformat=sample_rates={sample_rate}:channel_layouts=mono", "highpass=f=80"]
    if normalize:
        filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    af = ",".join(filters)

    cmd = [
        _binary("ffmpeg"), "-y", "-i", str(source),
        "-vn",
        "-af", af,
        "-ar", str(sample_rate),
        "-ac", "1",
        "-c:a", "pcm_s16le",
        str(out_wav),
    ]
    r = _run(cmd, cancel_cb=cancel_cb)
    if r.returncode != 0:
        detail = _clean_ffmpeg_error(r.stderr)
        raise RuntimeError(f"Could not process '{name or source.name}': {detail}")

    return AudioPrepResult(
        wav_path=out_wav,
        duration_s=probe_duration(out_wav),
        source=source,
    )


def chunk_audio(
    wav_path: Path,
    chunk_dir: Path,
    *,
    chunk_length_s: int = 600,
) -> list[Path]:
    """Split a WAV into fixed-length chunks (last chunk may be shorter).

    Returns an ordered list of chunk paths. Skips chunking if the file is
    already shorter than one chunk.
    """
    duration = probe_duration(wav_path)
    if duration <= chunk_length_s:
        return [wav_path]

    chunk_dir.mkdir(parents=True, exist_ok=True)
    pattern = chunk_dir / "chunk_%03d.wav"
    cmd = [
        _binary("ffmpeg"), "-y", "-i", str(wav_path),
        "-f", "segment",
        "-segment_time", str(chunk_length_s),
        "-c", "copy",
        str(pattern),
    ]
    r = _run(cmd)
    if r.returncode != 0:
        detail = _clean_ffmpeg_error(r.stderr)
        raise RuntimeError(f"Audio chunking failed: {detail}")

    return sorted(chunk_dir.glob("chunk_*.wav"))


def _clean_ffmpeg_error(stderr: str) -> str:
    """Return a concise, user-friendly ffmpeg error without version banners."""
    if not stderr:
        return "unknown error"
    lower = stderr.lower()
    if "invalid data found" in lower:
        return "Invalid file format or corrupted file. Please upload a valid audio or video file."
    if "no such file" in lower:
        return "File not found."
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    banner = ("ffmpeg version", "built with", "configuration:", "libav", "  ")
    filtered = [ln for ln in lines if not ln.startswith(banner)]
    for keyword in ("Error opening", "Conversion failed", "Invalid"):
        for ln in reversed(filtered):
            if keyword.lower() in ln.lower():
                return ln
    return filtered[-1] if filtered else "unknown error"
