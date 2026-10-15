"""faster-whisper transcription with sane defaults and profile support."""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import TYPE_CHECKING

from . import config
from .errors import CancelledError
from .profiles import Profile

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

log = logging.getLogger("minutenest.transcribe")


# Display names for the language codes accepted by faster-whisper.
# The code list itself always comes from the installed library
# (see supported_languages); this dict only provides labels.
LANGUAGE_NAMES = {
    "af": "Afrikaans", "am": "Amharic", "ar": "Arabic", "as": "Assamese",
    "az": "Azerbaijani", "ba": "Bashkir", "be": "Belarusian", "bg": "Bulgarian",
    "bn": "Bengali", "bo": "Tibetan", "br": "Breton", "bs": "Bosnian",
    "ca": "Catalan", "cs": "Czech", "cy": "Welsh", "da": "Danish",
    "de": "German", "el": "Greek", "en": "English", "es": "Spanish",
    "et": "Estonian", "eu": "Basque", "fa": "Persian", "fi": "Finnish",
    "fo": "Faroese", "fr": "French", "gl": "Galician", "gu": "Gujarati",
    "ha": "Hausa", "haw": "Hawaiian", "he": "Hebrew", "hi": "Hindi",
    "hr": "Croatian", "ht": "Haitian Creole", "hu": "Hungarian", "hy": "Armenian",
    "id": "Indonesian", "is": "Icelandic", "it": "Italian", "ja": "Japanese",
    "jw": "Javanese", "ka": "Georgian", "kk": "Kazakh", "km": "Khmer",
    "kn": "Kannada", "ko": "Korean", "la": "Latin", "lb": "Luxembourgish",
    "ln": "Lingala", "lo": "Lao", "lt": "Lithuanian", "lv": "Latvian",
    "mg": "Malagasy", "mi": "Maori", "mk": "Macedonian", "ml": "Malayalam",
    "mn": "Mongolian", "mr": "Marathi", "ms": "Malay", "mt": "Maltese",
    "my": "Burmese", "ne": "Nepali", "nl": "Dutch", "nn": "Norwegian Nynorsk",
    "no": "Norwegian", "oc": "Occitan", "pa": "Punjabi", "pl": "Polish",
    "ps": "Pashto", "pt": "Portuguese", "ro": "Romanian", "ru": "Russian",
    "sa": "Sanskrit", "sd": "Sindhi", "si": "Sinhala", "sk": "Slovak",
    "sl": "Slovenian", "sn": "Shona", "so": "Somali", "sq": "Albanian",
    "sr": "Serbian", "su": "Sundanese", "sv": "Swedish", "sw": "Swahili",
    "ta": "Tamil", "te": "Telugu", "tg": "Tajik", "th": "Thai",
    "tk": "Turkmen", "tl": "Tagalog", "tr": "Turkish", "tt": "Tatar",
    "uk": "Ukrainian", "ur": "Urdu", "uz": "Uzbek", "vi": "Vietnamese",
    "yi": "Yiddish", "yo": "Yoruba", "zh": "Chinese", "yue": "Cantonese",
}


def supported_languages() -> list[dict]:
    """Accepted languages from the installed faster-whisper build.

    Single source of truth for the UI dropdown, so a typo'd language
    can never reach the model.
    """
    try:
        from faster_whisper.tokenizer import _LANGUAGE_CODES

        codes = list(_LANGUAGE_CODES)
    except ImportError:       # an older faster-whisper without that list: our own names
        codes = list(LANGUAGE_NAMES)
    return [{"code": c, "name": LANGUAGE_NAMES.get(c, c)} for c in codes]


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class TranscriptionResult:
    segments: list[Segment] = field(default_factory=list)
    language: str = "ru"
    duration_s: float = 0.0
    elapsed_s: float = 0.0
    heard_language: str = ""        # what Whisper hears in the first speech (check_language)
    heard_probability: float = 0.0

    def as_dicts(self) -> list[dict]:
        return [
            {"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text}
            for s in self.segments
        ]

    def text(self) -> str:
        return "\n\n".join(s.text for s in self.segments)


_MODEL_CACHE: dict[tuple[str, str, str], WhisperModel] = {}
# One load at a time: a warm-up and a job (or two jobs) asking at once used to
# load the same model twice -- double the VRAM for large-v3.
_LOAD_LOCK = threading.RLock()
_LAST_USED: dict[tuple[str, str, str], float] = {}  # key -> time.monotonic() of last use
_PINNED: set[str] = set()  # sizes loaded with the Load button: never released automatically
_CUDA_UNUSABLE = False  # a CUDA load failed in this process: use the CPU from then on
_CPU_FALLBACK_NOTE = ("The GPU could not be used; running on CPU (int8) instead. "
                      "Transcription will be slower.")


def touch_model(model_size: str) -> None:
    """Mark a cached model as just used (idle release counts from here)."""
    now = time.monotonic()
    for k in list(_MODEL_CACHE):
        if k[0] == model_size:
            _LAST_USED[k] = now


def pin_model(model_size: str) -> None:
    _PINNED.add(model_size)


def release_idle_models(idle_s: float, now: float | None = None) -> list[str]:
    """Unload models unused for ``idle_s`` seconds (pinned ones stay).

    Gives VRAM back to games and other apps instead of holding ~3 GB for
    large-v3 until the app closes. Returns the sizes that were released.
    """
    now = time.monotonic() if now is None else now
    with _LOAD_LOCK:
        stale = sorted({k[0] for k in _MODEL_CACHE
                        if k[0] not in _PINNED and now - _LAST_USED.get(k, now) >= idle_s})
        for size in stale:
            unload_models(size)
    return stale


def loaded_model_keys() -> list[tuple[str, str, str]]:
    """(model_size, device, compute_type) triples currently held in memory."""
    return list(_MODEL_CACHE.keys())


def is_model_loaded(model_size: str) -> bool:
    """True if any cached entry matches this model size."""
    return any(k[0] == model_size for k in _MODEL_CACHE)


def unload_models(model_size: str | None = None) -> int:
    """Drop cached models to free VRAM/RAM. Returns number unloaded."""
    keys = [k for k in _MODEL_CACHE if model_size is None or k[0] == model_size]
    for k in keys:
        _MODEL_CACHE.pop(k, None)
        _LAST_USED.pop(k, None)
    if model_size is None:
        _PINNED.clear()
    else:
        _PINNED.discard(model_size)
    if keys:
        import gc

        gc.collect()
        try:
            import torch  # pyright: ignore[reportMissingImports] - optional, not bundled

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass
    return len(keys)


def _cuda_available() -> bool:
    """True when a CUDA device is actually present.

    Checked before attempting a GPU load so no-GPU machines (and VMs without
    passthrough) quietly run on CPU instead of logging a scary DLL error and
    then falling back. If the check itself fails we say "available" and let
    the load attempt decide.
    """
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001 - "can't tell": the load attempt decides (see above)
        return True


def _thread_kwargs(device: str) -> dict:
    """Keep one core free on CPU so the window stays responsive during a job.

    By default CTranslate2 uses every core; on a small PC or VM that starves
    the UI and the buttons feel dead while a file is being transcribed.
    """
    import os

    cores = os.cpu_count() or 1
    if device == "cuda" or cores < 4:
        return {}
    return {"cpu_threads": cores - 1}


def resolve_compute_type(device: str, compute_type: str) -> str:
    """Make the compute type valid for the device.

    float16 / int8_float16 need a GPU; on CPU ctranslate2 refuses them
    ("Requested float16 compute type, but the target device or backend do not
    support efficient float16 computation"). Switching the device to CPU used
    to leave float16 selected and every job failed.
    """
    if device != "cuda" and compute_type in ("float16", "int8_float16"):
        return "int8"
    return compute_type


def _get_model(model_size: str, device: str, compute_type: str,
               on_fallback=None) -> tuple[WhisperModel, str, str]:
    """Load (and cache) a faster-whisper model, lazily importing the dep.

    Returns ``(model, device, compute_type)`` with the device ACTUALLY used:
    a CUDA load that fails (driver mismatch, DLL trouble) falls back to
    CPU + int8 instead of dying, and callers must report the real device.
    ``on_fallback(reason)`` is invoked (if given) so callers can log it.
    """
    compute_type = resolve_compute_type(device, compute_type)
    with _LOAD_LOCK:
        return _get_model_locked(model_size, device, compute_type, on_fallback)


def _get_model_locked(model_size: str, device: str, compute_type: str,
                      on_fallback=None) -> tuple[WhisperModel, str, str]:
    global _CUDA_UNUSABLE
    if device == "cuda" and _CUDA_UNUSABLE:
        # Already failed once in this process: go straight to the CPU model
        # instead of failing again and loading a second CPU copy per job.
        if on_fallback:
            on_fallback(_CPU_FALLBACK_NOTE)
        device, compute_type = "cpu", "int8"
    key = (model_size, device, compute_type)
    if key in _MODEL_CACHE:
        _LAST_USED[key] = time.monotonic()
        return _MODEL_CACHE[key], device, compute_type

    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError(
            "faster-whisper is not installed. Install with `pip install faster-whisper` "
            "or use an environment that has it."
        ) from e

    log.info("loading model %s (%s, %s)", model_size, device, compute_type)
    t0 = time.time()
    try:
        model = WhisperModel(model_size, device=device, compute_type=compute_type,
                             **_thread_kwargs(device))
    except Exception:
        if device == "cuda":
            # Short, non-alarming message for the progress line; the full
            # exception goes to the debug log for diagnosis.
            log.exception("CUDA load failed, falling back to CPU")
            _CUDA_UNUSABLE = True
            if on_fallback:
                on_fallback(_CPU_FALLBACK_NOTE)
            device, compute_type = "cpu", "int8"
            model = WhisperModel(model_size, device=device, compute_type=compute_type,
                                 **_thread_kwargs(device))
        else:
            raise
    log.info("model %s loaded in %.1fs on %s/%s", model_size, time.time() - t0,
             device, compute_type)
    _MODEL_CACHE[(model_size, device, compute_type)] = model
    _LAST_USED[(model_size, device, compute_type)] = time.monotonic()
    try:
        from . import models as _models

        _models.notify_changed(model_size)
    except Exception:
        logging.getLogger("minutenest.transcribe").exception("model status notification failed")
    return model, device, compute_type


def decode_options(profile: Profile, language: str) -> dict:
    """faster-whisper decoding settings for audio spoken in ``language``.

    Beam 5 at temperature 0. A 30-second window whose text compresses more than
    2.4x (a repetition loop: "No, no, no, ...", "cccc...") or whose average
    log-probability is under -1.0 is decoded again at T=0.2..1.0. Without that
    second try tiny kept loops in place of minutes of real speech. Measured on
    the CPU (3 real meetings + a 12-min podcast with a reference text):
    tiny 6 loops -> 0 for 20-35% more time, WER 28.0 -> 28.4% (noise);
    small 0 loops either way, WER 11.68% both; large-v3 never re-decoded a
    window, identical text (WER 7.36%). A beam of 10 was slower without being
    more accurate. A threshold of 2.0 re-decoded ordinary sentences too:
    4x slower and far less accurate (tiny podcast WER 49%).
    """
    return dict(
        language=language,
        temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(
            min_silence_duration_ms=300,
            speech_pad_ms=400,
            threshold=0.35,
        ),
        initial_prompt=profile.initial_prompt or None,
        condition_on_previous_text=False,
        # -0.5 made the small models drop whole minutes of real speech as
        # "silence"; -1.0 (faster-whisper's default) changes nothing for large-v3
        log_prob_threshold=-1.0,
        no_speech_threshold=0.5,
        compression_ratio_threshold=2.4,
    )


REPEAT_MAX_WORDS = 4      # longer phrases said twice in a row are real speech
REPEAT_MAX_GAP_S = 2.0    # copies further apart than this are said again, not repeated


def _same_words(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.lower()).strip()


def merge_repeated_segments(segments: list[dict]) -> list[dict]:
    """Write a short phrase Whisper repeated back to back only once.

    Small models on poor audio emit the same 1-3 words as several touching
    segments ("Прикой." x6, measured on real meetings: every run was 1-3 words,
    copies 0-2 s apart). The temperature fallback catches loops inside one
    segment, not across segments. A run of identical short segments becomes one
    segment covering the whole run; anything longer, or said again after a
    pause, is kept as spoken.
    """
    merged: list[dict] = []
    for seg in segments:
        prev = merged[-1] if merged else None
        words = _same_words(seg.get("text") or "")
        if (prev is not None and words and words == _same_words(prev.get("text") or "")
                and len(words.split()) <= REPEAT_MAX_WORDS
                and float(seg["start"]) - float(prev["end"]) <= REPEAT_MAX_GAP_S):
            merged[-1] = {**prev, "end": max(float(prev["end"]), float(seg["end"]))}
            continue
        merged.append(dict(seg))
    return merged


def transcribe(  # noqa: PLR0913 - keyword-only options, named by every caller
    audio_path: Path,
    profile: Profile,
    *,
    model_size: str | None = None,
    device: str | None = None,
    compute_type: str | None = None,
    language: str | None = None,
    time_offset: float = 0.0,
    progress_cb: Callable[..., None] | None = None,
    progress_meta: dict | None = None,
    should_cancel: Callable[[], bool] | None = None,
    check_language: bool = False,
) -> TranscriptionResult:
    """Transcribe an audio file with the given profile.

    Defaults favor meeting recordings: VAD on, aggressive silence handling,
    beam search, no conditioning on previous text (reduces drift/hallucination
    loops on poor audio), and a domain vocabulary prompt.
    ``check_language``: also say which language the first speech sounds like
    (``heard_language``): a recording transcribed as the wrong language comes
    out as a garbled translation, without any error.
    """
    model_size = model_size or config.DEFAULT_MODEL_SIZE
    language = language or profile.language or config.DEFAULT_LANGUAGE
    log = progress_cb or _quiet
    model, dev, comp = _ready_model(model_size, device or config.DEFAULT_DEVICE,
                                    compute_type or config.DEFAULT_COMPUTE_TYPE, log, progress_meta)
    part = _part(progress_meta)
    log(f"Transcribing{part} on the {'GPU' if dev == 'cuda' else 'CPU'} ({comp})")
    start = time.time()
    result = TranscriptionResult(language=language)
    if check_language:
        result.heard_language, result.heard_probability = _heard_language(model, audio_path)
    try:
        segments_iter, info = model.transcribe(str(audio_path), **decode_options(profile, language))
        _collect_segments(segments_iter, result, time_offset, log, progress_meta, should_cancel)
    finally:
        # The idle-release clock starts when work ends -- also when it was
        # stopped or failed, so a Retry right after finds the model loaded.
        touch_model(model_size)
    result.duration_s = getattr(info, "duration", 0.0) or 0.0
    result.elapsed_s = time.time() - start
    log(f"Transcribed{part}: {len(result.segments)} segments in {result.elapsed_s:.0f}s")
    return result


def _part(meta: dict | None) -> str:
    """' part 2 of 3' while a long recording is transcribed in parts, else ''
    (the parts' temporary file names mean nothing to the user)."""
    if not meta or meta.get("chunks", 1) < 2:
        return ""
    return f" part {meta.get('chunk', 1)} of {meta['chunks']}"


def _quiet(_msg: str, _frac: float | None = None) -> None:
    """A progress callback that reports nowhere."""


def _ready_model(model_size: str, dev: str, comp: str, log: Callable[..., None], progress_meta: dict | None):
    """(model, device, compute type) actually loaded, falling back to the CPU when needed."""
    # No CUDA device present: switch before loading so no-GPU machines and
    # passthrough-less VMs never surface a CUDA error, even briefly.
    if dev == "cuda" and not _cuda_available():
        log("No GPU detected — running on CPU (int8). Transcription will be slower.")
        dev, comp = "cpu", "int8"
    model, dev, comp = _get_model(model_size, dev, comp, on_fallback=log)
    ready = (progress_meta or {}).get("model_ready")
    if ready:
        ready()  # model is loaded: load time must not inflate the time-left estimate
    return model, dev, comp


def _collect_segments(segments_iter, result: TranscriptionResult, time_offset: float, log: Callable[..., None],
                      progress_meta: dict | None, should_cancel: Callable[[], bool] | None) -> None:
    last_log = 0.0
    for seg in segments_iter:
        if should_cancel and should_cancel():
            raise CancelledError("cancelled by user")
        result.segments.append(Segment(start=seg.start + time_offset, end=seg.end + time_offset,
                                       text=seg.text.strip()))
        # Report as segments arrive (not every 20 s: a short file used to
        # finish with the bar still near the start). Throttled so a slow PC
        # isn't flooded with updates.
        now = time.time()
        if now - last_log >= PROGRESS_TICK_S:
            pos = seg.end + time_offset
            log(f"  ... transcribed {_progress_line(pos, progress_meta)}", _audio_fraction(pos, progress_meta))
            last_log = now


PROGRESS_TICK_S = 0.7   # "... transcribed 4:30 of 22:05" at most this often


def _heard_language(model, audio_path: Path) -> tuple[str, float]:
    """(language code, probability) of the first speech, or ("", 0.0) if it can't tell.

    One extra encoder pass over the first 30 s of speech. It only feeds a
    warning, so a failure here is logged and the transcription goes on.
    """
    try:
        lang, prob, _ = model.detect_language(audio=_first_seconds(audio_path, 90.0), vad_filter=True)
        return str(lang or ""), float(prob or 0.0)
    except Exception:
        logging.getLogger("minutenest.transcribe").warning("language check failed", exc_info=True)
        return "", 0.0


def _first_seconds(audio_path: Path, seconds: float):
    """The start of a recording as 16 kHz float32 samples, without decoding all of it.

    The app's own chunks are 16 kHz mono 16-bit WAV (audio.extract_audio): read
    just the frames needed. Anything else is decoded in full by faster-whisper.
    """
    samples = _wav_start(audio_path, seconds)
    if samples is not None:
        return samples
    from faster_whisper.audio import decode_audio

    return decode_audio(str(audio_path), sampling_rate=16000)[: int(seconds * 16000)]


def _wav_start(audio_path: Path, seconds: float):
    """The first ``seconds`` of a 16 kHz mono 16-bit WAV as float32, or None for any other file."""
    import wave

    import numpy as np

    try:
        with wave.open(str(audio_path), "rb") as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
                return None
            raw = w.readframes(int(seconds * 16000))
    except (wave.Error, EOFError):
        return None
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _audio_fraction(audio_pos: float, meta: dict | None) -> float | None:
    """Share of the whole job's audio transcribed so far (0..1), if knowable."""
    total = (meta or {}).get("audio_total") or 0.0
    if total <= 0:
        return None
    base = meta.get("audio_done", 0.0) if meta else 0.0
    done = base + max(0.0, audio_pos - base)
    return min(0.999, max(0.0, done / total))


def _progress_line(audio_pos: float, meta: dict | None) -> str:
    """Position within the whole job's audio, e.g. '12:30 of 45:00'.

    No time estimate here: the job-wide ETA is computed once, by JobProgress,
    across every stage (this used to print its own ETA for just this step).
    """
    if not meta or not meta.get("audio_total"):
        return f"{audio_pos:.0f}s"
    done = meta.get("audio_done", 0.0) + max(0.0, audio_pos - meta.get("audio_done", 0.0))
    return f"{_clock(min(done, meta['audio_total']))} of {_clock(meta['audio_total'])}"


def _clock(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
