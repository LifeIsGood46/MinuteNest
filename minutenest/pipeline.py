"""End-to-end pipeline: extract -> transcribe -> correct -> polish -> export."""

from __future__ import annotations

import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable

from .audio import extract_audio, chunk_audio, probe_duration
from . import config, names
from .errors import CancelledError
from .profiles import Profile, load_profile
from .transcribe import TranscriptionResult, merge_repeated_segments, transcribe
from .correct import correct_segments
from .llm import LLMProvider, get_provider
from . import polish
from .progress import JobProgress

log = logging.getLogger("minutenest.pipeline")


@dataclass
class PipelineOptions:
    profile: str = "generic"
    task: str = "raw"                    # raw | clean | notes | actions | testplan | summary | custom
    custom_prompt: str = ""              # used when task == "custom"
    output_name: str | None = None       # filename suffix for the document (defaults to task)
    llm_provider: str | None = None      # ollama | anthropic | openai | None (no LLM)
    llm_model: str | None = None
    model_size: str | None = None
    device: str | None = None
    compute_type: str | None = None
    language: str | None = None          # spoken in the recording (Whisper)
    document_language: str | None = None  # the LLM writes the document in it; None = `language`
    output_format: str = "both"           # md | txt | both (segments json always)
    normalize_audio: bool = True
    chunk_length_s: int = 600
    keep_work: bool = True
    display_name: str | None = None      # original file name when `source` is a stored copy


@dataclass
class PipelineResult:
    source: Path
    profile: str
    transcript_raw: str = ""
    transcript_corrected: str = ""
    document: str = ""
    segments: list[dict] = field(default_factory=list)
    work_dir: Path | None = None
    outputs: dict[str, Path] = field(default_factory=dict)
    elapsed_s: float = 0.0
    llm_models: list[str] = field(default_factory=list)  # models that actually answered
    warnings: list[str] = field(default_factory=list)    # finished, but the user should know this


def llm_note(configured: str, used: list[str]) -> str:
    """'' when the configured model wrote everything, else who did (free fallbacks)."""
    others = [m for m in dict.fromkeys(used) if m != configured]
    if not others:
        return ""
    if configured == "openrouter/free":      # the router picks a model per request
        return f"Written by {', '.join(others)} (picked by openrouter/free)."
    if configured in used:
        return f"Parts written by {', '.join(others)} ({configured} was busy at times)."
    return f"Written by {', '.join(others)} ({configured} was busy)."


# Below this Whisper's own guess is too unsure to second-guess the user's setting.
LANGUAGE_WARNING_PROBABILITY = 0.7


def _language_warning(res: TranscriptionResult, set_language: str | None) -> str:
    """A sentence for the user when the recording clearly is not in the set language."""
    from .transcribe import LANGUAGE_NAMES

    heard, chosen = (res.heard_language or "").lower(), (set_language or "").lower()
    if not heard or not chosen or heard == chosen or res.heard_probability < LANGUAGE_WARNING_PROBABILITY:
        return ""
    heard_name = LANGUAGE_NAMES.get(heard, heard)
    return (f"This recording sounds like {heard_name}, but the audio language is set to "
            f"{LANGUAGE_NAMES.get(chosen, chosen)}: the transcript may be a translation or garbled. "
            f"Transcribe it again with the audio language set to {heard_name} ({heard}).")


def _log(cb: Callable[..., None] | None, msg: str, fraction: float | None = None) -> None:
    if cb:
        cb(msg, fraction)


def _accepts_eta(cb: Callable[..., None] | None) -> bool:
    if cb is None:
        return False
    try:
        params = inspect.signature(cb).parameters
    except (TypeError, ValueError):
        return False
    return "eta_s" in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


def _stage_plan(needs_llm: bool) -> dict[str, float]:
    """Share of the one whole-job bar that each stage owns."""
    if needs_llm:
        return {"extract": 3, "model": 4, "transcribe": 53, "llm": 40}
    return {"extract": 4, "model": 4, "transcribe": 92}


def _resolve_llm(opts: PipelineOptions) -> LLMProvider:
    """Resolve the LLM provider, honoring explicit options then app settings.

    Provider/model come from opts when set, else from persisted settings.
    Credentials come from settings/env based on the chosen provider.
    """
    from .settings import load_settings  # local import to avoid cycles

    saved = load_settings()
    provider_name = opts.llm_provider or saved.llm.provider or "ollama"
    model = opts.llm_model or saved.llm.model or None

    kwargs: dict = {}
    if provider_name == "ollama":
        if saved.llm.ollama_host:
            kwargs["host"] = saved.llm.ollama_host
        if saved.llm.ollama_api_key:
            kwargs["api_key"] = saved.llm.ollama_api_key
    elif provider_name == "compat":
        if saved.llm.compat_base_url:
            kwargs["base_url"] = saved.llm.compat_base_url
        if saved.llm.compat_api_key:
            kwargs["api_key"] = saved.llm.compat_api_key
    elif provider_name == "anthropic":
        if saved.llm.anthropic_api_key:
            kwargs["api_key"] = saved.llm.anthropic_api_key
    elif provider_name == "openai":
        if saved.llm.openai_api_key:
            kwargs["api_key"] = saved.llm.openai_api_key
    elif provider_name == "openrouter":
        if saved.llm.openrouter_api_key:
            kwargs["api_key"] = saved.llm.openrouter_api_key

    return get_provider(provider_name, model=model, **kwargs)


def run_pipeline(
    source: Path,
    work_dir: Path,
    out_dir: Path,
    opts: PipelineOptions,
    progress_cb: Callable[..., None] | None = None,
    cancel_cb: Callable[[], bool] | None = None,
) -> PipelineResult:
    """Run the full processing pipeline for one source file.

    Stages run conditionally based on ``opts.task``: ``raw`` stops after
    glossary correction; anything beyond that requires an LLM provider.
    ``cancel_cb`` is polled between stages and inside the transcribe loop;
    when it returns True the run aborts with CancelledError.

    Progress is ONE monotonic bar for the whole job (see ``JobProgress``): each
    stage owns a share of it and ``progress_cb(msg, fraction, eta_s=...)`` also
    gets the estimated seconds left for the entire job.
    """
    t0 = time.time()
    run = _Run(Path(source), work_dir, out_dir, opts, progress_cb, cancel_cb)
    wav_path = run.extract()
    run.ensure_model()
    segments, warning = run.transcribe_all(wav_path)
    result, outputs = run.write_transcripts(segments)
    if warning:
        result.warnings.append(warning)
    document, note = run.llm_document(result) if run.needs_llm else run.raw_document(result)
    result.document = document
    run.write_document(document, outputs)
    result.outputs = outputs
    result.elapsed_s = time.time() - t0
    if note:
        log.info("LLM: %s", note)
    run.jp.complete(f"Done in {result.elapsed_s:.0f}s." + (f" {note}" if note else ""))
    return result


class _Run:
    """One pipeline run: its options, progress and folders, one method per stage."""

    def __init__(self, source: Path, work_dir: Path, out_dir: Path, opts: PipelineOptions,
                 progress_cb: Callable[..., None] | None, cancel_cb: Callable[[], bool] | None):
        self.source, self.work_dir, self.out_dir, self.opts = source, work_dir, out_dir, opts
        self._cancel_cb = cancel_cb
        work_dir.mkdir(parents=True, exist_ok=True)
        # out_dir is created only once there is something to put in it, so a job
        # that is cancelled or fails early leaves no empty folder behind.
        self.label = Path(opts.display_name) if opts.display_name else source
        # what results are called: the recording's name, shortened only when
        # the paths would pass Windows' limit (see names.py)
        self.stem = names.shorten(self.label.stem, names.room_in(out_dir, names.LONGEST_SUFFIX))
        self.needs_llm = opts.task != "raw"
        self.jp = JobProgress(_emitter(progress_cb), _stage_plan(self.needs_llm))
        self.profile: Profile = load_profile(opts.profile, language=opts.language)
        self.jp.begin("extract", f"Profile: {self.profile.name} — {self.profile.description}")
        log.info("pipeline start: source=%s task=%s profile=%s lang=%s model=%s "
                 "device=%s compute=%s normalize=%s chunk_s=%s",
                 source.name, opts.task, self.profile.name, opts.language or "-",
                 opts.model_size or config.DEFAULT_MODEL_SIZE, opts.device or config.DEFAULT_DEVICE,
                 opts.compute_type or config.DEFAULT_COMPUTE_TYPE, opts.normalize_audio, opts.chunk_length_s)

    def cancelled(self) -> bool:
        return bool(self._cancel_cb and self._cancel_cb())

    def stop_if_cancelled(self) -> None:
        if self.cancelled():
            raise CancelledError("cancelled by user")

    # --- stage 1: audio -----------------------------------------------------
    def extract(self) -> Path:
        self.stop_if_cancelled()
        wav_path = names.file_in(self.work_dir, self.source.stem, ".wav")
        self.jp.say("[1/4] Extracting + normalizing audio")
        t = time.time()
        prep = extract_audio(self.source, wav_path, normalize=self.opts.normalize_audio,
                             cancel_cb=self.cancelled, name=self.label.name)
        self.jp.finish("extract", f"      duration: {prep.duration_s:.0f}s")
        log.info("extract done in %.1fs: %.1fs of audio", time.time() - t, prep.duration_s)
        return wav_path

    # --- the model exists BEFORE transcribing: a first run downloads it visibly
    # (with %) instead of stalling silently inside faster-whisper -------------
    def ensure_model(self) -> None:
        from .models import KNOWN_MODELS, ensure_downloaded

        model_size = self.opts.model_size or config.DEFAULT_MODEL_SIZE
        self.jp.begin("model")
        if model_size in KNOWN_MODELS:
            ensure_downloaded(model_size, self._model_progress, should_cancel=self.cancelled)
            log.info("model %s present locally", model_size)
        self.jp.finish("model")
        self.stop_if_cancelled()

    def _model_progress(self, msg: str, frac: float | None = None) -> None:
        if frac is None:
            self.jp.say(msg)
        else:
            self.jp.update(frac, msg)

    # --- stage 2: transcription (chunked if long) ---------------------------
    def transcribe_all(self, wav_path: Path) -> tuple[list[dict], str]:
        """All segments of the recording, and a warning when it is not in the set language."""
        self.jp.begin("transcribe", "[2/4] Transcribing")
        chunks = chunk_audio(wav_path, self.work_dir / "chunks", chunk_length_s=self.opts.chunk_length_s)
        if len(chunks) > 1:
            self.jp.say(f"      long audio: {len(chunks)} chunks")
        log.info("transcribing %d chunk(s)", len(chunks))
        segments: list[dict] = []
        warning = ""
        meta = {"chunks": len(chunks), "audio_total": sum(probe_duration(c) for c in chunks),
                "audio_done": 0.0, "stage_started": time.time(), "model_ready": self.jp.mark_ready}
        for i, chunk_path in enumerate(chunks):
            self.stop_if_cancelled()
            res = self._transcribe_chunk(chunk_path, i, len(chunks), meta)
            if i == 0:
                warning = _language_warning(res, self.opts.language or self.profile.language)
                if warning:
                    self.jp.say(warning)
            meta["audio_done"] += probe_duration(chunk_path)
            segments.extend(res.as_dicts())
        self.stop_if_cancelled()
        # a short phrase Whisper repeated back to back ("Прикой." x6) is written once
        segments = merge_repeated_segments(segments)
        if not any((s.get("text") or "").strip() for s in segments):
            # Nothing was transcribed: silence, wrong language, or an all-noise
            # recording. Fail loudly here — feeding an empty transcript to the LLM
            # produced confident refusals ("no cleaned transcript was included")
            # that got written to disk as if they were the document.
            raise RuntimeError(
                "No speech was detected in this recording. Check the audio language "
                "setting matches what is spoken, and that the file actually contains "
                "audible speech."
            )
        log.info("transcribe done in %.1fs: %d segments", time.time() - meta["stage_started"], len(segments))
        return segments, warning

    def _transcribe_chunk(self, chunk_path: Path, i: int, n: int, meta: dict) -> TranscriptionResult:
        opts = self.opts
        return transcribe(
            chunk_path, self.profile, model_size=opts.model_size, device=opts.device,
            compute_type=opts.compute_type, language=opts.language,
            time_offset=i * opts.chunk_length_s if n > 1 else 0.0,
            progress_cb=self._model_progress, progress_meta={**meta, "chunk": i + 1},
            should_cancel=self.cancelled, check_language=(i == 0),
        )

    # --- stage 3: glossary + transcripts on disk BEFORE the optional LLM -----
    def write_transcripts(self, segments: list[dict]) -> tuple[PipelineResult, dict[str, Path]]:
        """If the model then fails or refuses, the transcripts are already on disk
        (a failed job used to leave nothing at all). A separate .raw.txt is only
        written when glossary correction actually changed something — otherwise
        it is a byte-identical duplicate of the corrected transcript."""
        self.jp.finish("transcribe", "[3/4] Applying glossary corrections")
        corrected = correct_segments(segments, self.profile.corrections)
        raw_text = "\n\n".join(s["text"] for s in segments)
        corrected_text = "\n\n".join(s["text"] for s in corrected)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        stem = self.stem
        outputs = {"transcript_corrected": self.out_dir / f"{stem}.txt",
                   "segments_json": self.out_dir / f"{stem}.json"}
        outputs["transcript_corrected"].write_text(corrected_text, encoding="utf-8")
        outputs["segments_json"].write_text(json.dumps(corrected, ensure_ascii=False, indent=2), encoding="utf-8")
        if corrected_text != raw_text:
            outputs["transcript_raw"] = self.out_dir / f"{stem}.raw.txt"
            outputs["transcript_raw"].write_text(raw_text, encoding="utf-8")
        result = PipelineResult(source=self.label, profile=self.profile.name, transcript_raw=raw_text,
                                transcript_corrected=corrected_text, segments=corrected, work_dir=self.work_dir)
        return result, outputs

    # --- stage 4: the LLM document -------------------------------------------
    def llm_document(self, result: PipelineResult) -> tuple[str, str]:
        """(document, note on which model wrote it). A refusal is a failed job, not a
        document — but the transcripts are already saved, and the message says where."""
        self.stop_if_cancelled()
        opts, transcript = self.opts, result.transcript_corrected
        llm: LLMProvider = _resolve_llm(opts)
        n_calls = polish.plan_calls(transcript, opts.task)
        self.jp.begin("llm", f"[4/4] LLM stage: provider={llm.provider_name} model={llm.model} task={opts.task}")
        log.info("LLM stage: provider=%s model=%s task=%s calls~%d", llm.provider_name, llm.model, opts.task,
                 n_calls)
        # free-model waits show on the card, not as a frozen bar
        llm.status_cb = lambda msg: self.jp.say(f"LLM stage: {msg}")
        # The bar moves with every streamed character; the time left follows from
        # the measured speed (JobProgress), never from a guess per request.
        progress = polish.Progress(n_calls, self.jp.update)
        spoken = opts.language or self.profile.language
        doc_language = opts.document_language or spoken  # the document is written in it
        if opts.task == "clean":       # the cleaned transcript IS the document
            document = polish.clean_transcript(transcript, llm, progress=progress, cancel_cb=self.cancelled,
                                               language=doc_language if doc_language != spoken else None)
            if not document.strip():
                raise RuntimeError(_COULD_NOT_PROCESS.format(out_dir=self.out_dir))
        elif opts.task == "custom":
            if not opts.custom_prompt.strip():
                raise ValueError("task=custom requires a non-empty custom_prompt")
            document = polish.generate_custom(transcript, opts.custom_prompt, llm, progress=progress,
                                              cancel_cb=self.cancelled, language=doc_language, spoken=spoken)
        else:
            document = polish.generate_document(transcript, opts.task, llm, progress=progress,
                                                cancel_cb=self.cancelled, language=doc_language, spoken=spoken)
        document = _reject_refusal(document, self.out_dir)
        result.llm_models = list(dict.fromkeys(getattr(llm, "models_used", None) or [llm.model]))
        self.jp.finish("llm")
        return document, llm_note(llm.model, result.llm_models)

    def raw_document(self, result: PipelineResult) -> tuple[str, str]:
        self.jp.say("[4/4] LLM stage skipped (task=raw)")
        return _raw_markdown(result, self.profile, self.opts), ""

    # --- the document, in the requested format(s) ----------------------------
    def write_document(self, document: str, outputs: dict[str, Path]) -> None:
        """Transcripts + segments are always written (above); the document follows the
        requested format: Markdown, plain text, or both."""
        stem, suffix = self.stem, self.opts.output_name or self.opts.task or "raw"
        fmt = (self.opts.output_format or "both").lower()
        if fmt in ("md", "both"):
            outputs["document"] = self.out_dir / f"{stem}.{suffix}.md"
            outputs["document"].write_text(document, encoding="utf-8")
        if fmt in ("txt", "both") and suffix != "raw":
            # For raw tasks the document IS the transcript, already written as
            # transcript_corrected — writing it again would just duplicate it.
            outputs["document_txt"] = self.out_dir / f"{stem}.{suffix}.txt"
            outputs["document_txt"].write_text(_plain_text(document), encoding="utf-8")


_COULD_NOT_PROCESS = ("The model could not process this recording — the audio quality "
                      "may be too low, or the selected language may not match what is "
                      "spoken. The transcript was saved to: {out_dir}")


def _emitter(progress_cb: Callable[..., None] | None):
    """JobProgress's emit for ``progress_cb`` (which may not take the time left)."""
    send_eta = _accepts_eta(progress_cb)

    def emit(msg: str, frac: float | None, eta: float | None, scope: str) -> None:
        if not progress_cb:
            return
        if send_eta:
            progress_cb(msg, frac, eta_s=eta, eta_scope=scope)
        else:
            progress_cb(msg, frac)
    return emit


def _reject_refusal(text: str, out_dir: Path) -> str:
    """Fail instead of saving a model's refusal as if it were the document.

    A small/weak model handed a poor-quality transcript sometimes replies with
    "I can't infer ... without the transcript" plus a template (a real reported
    case on a noisy Russian meeting). Detect the common refusal openings and
    raise — the transcripts are already on disk, so the message points there.
    """
    if polish.looks_like_refusal(text):
        raise RuntimeError(
            "The model could not process this recording — the audio quality "
            "may be too low, or the selected language may not match what is "
            f"spoken. The transcript was saved to: {out_dir}"
        )
    return text


# Markdown syntax removed for the .txt document, in this order.
_MARKDOWN_TO_TEXT = (
    (r"^#{1,6}\s*", ""),              # headings
    (r"^\s*[-*+]\s+", "- "),          # bullets
    (r"\*\*(.+?)\*\*", r"\1"),        # bold
    (r"\*(.+?)\*", r"\1"),            # italics
    (r"`(.+?)`", r"\1"),              # code
    (r"\[(.+?)\]\((.+?)\)", r"\1 (\2)"),  # links
)


def _plain_text(markdown: str) -> str:
    """Strip the most common Markdown syntax for the .txt document variant.

    Deliberately small: headings, emphasis, links and table pipes. The raw and
    corrected transcripts remain untouched as their own .txt files.
    """
    import re

    out_lines = []
    for line in markdown.splitlines():
        plain = line
        for pattern, repl in _MARKDOWN_TO_TEXT:
            plain = re.sub(pattern, repl, plain)
        if plain.strip() == "|" or re.fullmatch(r"[\s|:-]+", plain or ""):
            continue  # table separator rows
        out_lines.append(plain.replace("|", " | ").rstrip())
    text = "\n".join(out_lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def _raw_markdown(result: PipelineResult, profile: Profile, opts: PipelineOptions) -> str:
    """Readable Markdown for a raw transcript (no LLM involved).

    Timestamps are included when segments have them, so the .md is more than
    a copy of the .txt. Header carries the facts a reader needs to trust it.
    """
    lines = [
        f"# {result.source.stem}",
        "",
        f"- Profile: {profile.name} ({profile.description})",
        f"- Language: {opts.language or profile.language}",
        f"- Source: `{result.source.name}`",
        "",
        "## Transcript",
        "",
    ]
    for seg in result.segments:
        start = _fmt_timestamp(seg.get("start", 0.0))
        lines.append(f"**[{start}]** {seg.get('text', '').strip()}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _fmt_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"
