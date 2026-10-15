"""FastAPI web app: settings, model management, job queue, SSE live updates."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import queue
import re
import shutil
import socket
import subprocess  # nosec B404 - argument lists only, no shell; each call is marked
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Annotated, NoReturn

from fastapi import APIRouter, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from minutenest import config, names
from minutenest import __version__
from minutenest import models as model_mgr
from minutenest.errors import CancelledError
from minutenest.pipeline import PipelineOptions, run_pipeline
from minutenest.polish import available_tasks, task_default_prompts
from minutenest.profiles import list_profiles
from minutenest.settings import load_settings, update_settings

from .jobs import _remove_artifact_dir, job_queue, Job, mark_output_dir

UPLOAD_DIR = config.WORK_DIR / "_uploads"
APP_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(APP_DIR / "templates"))
STATIC_DIR = APP_DIR / "static"
_started_at = time.strftime("%Y-%m-%d %H:%M:%S")

log = logging.getLogger("minutenest.webapp")

# SSE subscribers (asyncio queues; see /api/events why this must be async)
_sse_subscribers: list = []
_sse_lock = threading.Lock()
_sse_loop: asyncio.AbstractEventLoop | None = None
_llm_models_cache: dict = {}  # 60s TTL cache for model listing

# Client-liveness tracking for the launcher's browser-fallback watchdog:
# with no app window to watch, "did the user close the tab?" is answered by
# the UI event stream (and a pagehide beacon).
_client_seen = False
_client_last_gone: float | None = None


def client_idle_seconds() -> tuple[bool, float | None]:
    """(seen_any_client, seconds_since_all_clients_left).

    idle is None while a client is connected or while no disconnect has been
    recorded yet. Used by the launcher to stop the server when the browser
    session ends (closing the tab must not leave the process behind).
    """
    with _sse_lock:
        if _sse_subscribers:
            return True, None
        if _client_last_gone is None:
            return _client_seen, None
        return True, time.time() - _client_last_gone


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _offer(q, item) -> None:
    # a tab too slow to keep up misses this update; the next one carries the current state
    with contextlib.suppress(asyncio.QueueFull):
        q.put_nowait(item)


def _publish(event: str, data: dict) -> None:
    """Fan out an event to all SSE subscribers from sync or async context."""
    with _sse_lock:
        subs = list(_sse_subscribers)
    if not subs:
        return
    for q in subs:
        try:
            if _sse_loop is not None and _sse_loop.is_running():
                _sse_loop.call_soon_threadsafe(_offer, q, (event, data))
            else:
                _offer(q, (event, data))
        except RuntimeError:
            pass


_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                     *(f"LPT{i}" for i in range(1, 10))}


def _safe_name(filename: str) -> str:
    """The file's own name, changed only where Windows forbids it.

    It is shown on the card and names the results folder, so "Встреча №1
    (финал).mp4" must stay that, not "Встреча _1 _финал_.mp4". Folder parts
    are dropped (no path traversal); reserved device names get a prefix."""
    base = Path(filename.replace("\\", "/")).name
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip().rstrip(". ")
    if base.split(".")[0].upper() in _WINDOWS_RESERVED:
        base = "_" + base
    return base or "upload"


def _set_priority(below_normal: bool) -> None:
    """Run the heavy work below normal priority so the UI process wins CPU contention."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k.SetPriorityClass(k.GetCurrentProcess(), 0x4000 if below_normal else 0x20)
    except (AttributeError, OSError) as e:   # not Windows, or not allowed: the job runs anyway
        log.debug("could not change the process priority: %s", e)


_HEX_ID = re.compile(r"^[0-9a-f]{12}$")


def _upload_path(job: Job) -> Path:
    """Where a job's uploaded file lives: one flat file, no per-job folder,
    named <job id>__<file name> (shortened only past the path limit, names.py)."""
    name = Path(_safe_name(job.filename))
    prefix = f"{job.id}__"
    stem = names.shorten(name.stem, names.room_in(UPLOAD_DIR, len(prefix) + len(name.suffix)))
    return UPLOAD_DIR / f"{prefix}{stem}{name.suffix}"


def _discard_upload(job: Job) -> None:
    # still open elsewhere (antivirus scan): the next start's orphan sweep removes it
    with contextlib.suppress(OSError):
        _upload_path(job).unlink(missing_ok=True)


def _discard_work(job_id: str) -> None:
    """Extracted audio and chunks are scratch data; never keep them."""
    shutil.rmtree(config.WORK_DIR / job_id, ignore_errors=True)


def _sweep_orphans() -> int:
    """Remove scratch left behind by older versions or a crash. Returns count.

    Touches only things the app itself makes: 12-hex job folders in the work
    dir, and the upload store. A live job's upload is kept (legacy per-job
    upload folders are migrated to the flat layout first).
    """
    removed = 0
    jobs = {j.id: j for j in job_queue.list()}
    busy = {i for i, j in jobs.items() if j.status in ("queued", "running")}
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for entry in list(UPLOAD_DIR.iterdir()):
        try:
            if entry.is_dir():
                job = jobs.get(entry.name)
                if job is not None:
                    legacy = entry / job.filename
                    if legacy.is_file() and not _upload_path(job).exists():
                        shutil.move(str(legacy), str(_upload_path(job)))
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
            else:
                owner = entry.name.split("__", 1)[0]
                if owner not in jobs:
                    entry.unlink()
                    removed += 1
        except OSError:
            pass
    work = config.WORK_DIR
    if work.is_dir():
        for entry in list(work.iterdir()):
            if entry.is_dir() and _HEX_ID.match(entry.name) and entry.name not in busy:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
    return removed


def _persist_upload(file: UploadFile, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)


def _resolve_out_dir(settings) -> Path:
    custom = (settings.output_dir or "").strip()
    return Path(custom) if custom else config.OUT_DIR


def _unique_job_dir(out_root: Path, filename: str) -> Path:
    """Per-job output folder: <root>/<name>/, never reusing an existing one.

    The name is the file stem (no extension — a folder called `x.mp4`
    reads as a file). On collision a ` (2)`, ` (3)`, … suffix is added,
    whether the existing folder holds a previous job's artifacts or the
    user's own files — nothing is ever mixed or overwritten.
    """
    stem = names.folder_stem(out_root, Path(filename).stem.strip() or "job", folder_extra=len(" (99)"))
    candidate = out_root / stem
    n = 1
    while candidate.exists():
        n += 1
        candidate = out_root / f"{stem} ({n})"
    return candidate


def _resolve_key(value: str | None, saved: str) -> str:
    """Form value wins; masked placeholder falls back to the saved key."""
    if value and not value.startswith("********"):
        return value.strip()
    return saved or ""


def _keep_keys_home(provider, key: str | None, host: str | None, saved_host: str) -> None:
    """A saved key, or one from the environment (OLLAMA_API_KEY, COMPAT_API_KEY),
    goes only to the saved address; elsewhere only a key typed in the form is used.
    Anyone who can reach the app can ask for a model list at an address of their
    choosing, and the provider would send its key along (CWE-522)."""
    asked = (host or "").strip().rstrip("/")
    if asked and asked != (saved_host or "").strip().rstrip("/"):
        provider.api_key = _resolve_key(key, "") or None


def _resolve_task(task: str, prompt: str) -> tuple[str, str]:
    """Decide the effective LLM task for a job about to run.

    An untouched preset prompt keeps its task (notes/actions/testplan/…);
    only a user-edited prompt coerces to "custom". (An empty prompt on
    "custom" never gets here: Start refuses it.)
    """
    prompt = (prompt or "").strip()
    if task not in ("raw", "clean", "custom") and prompt and prompt != task_default_prompts().get(task, ""):
        return "custom", prompt
    return task, prompt


def _open_in_explorer(path: Path, select_file: str = "") -> None:
    # explorer parses its own command line, so /select needs the path quoted
    # INSIDE a single raw argument. Passing a list makes subprocess re-quote
    # it (escaped quotes) and explorer silently falls back to Documents.
    target = f'/select,"{path / select_file}"' if select_file else f'"{path}"'
    # Raw command line: a list would double-quote and break explorer. The path is
    # one this app created or a folder the user picked; Windows paths cannot
    # contain '"', so the quoting above cannot be broken out of.
    subprocess.Popen(  # noqa: S603  # nosec B603
        f'explorer {target}',
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0,
    )


# --------------------------------------------------------------------------
# job worker (serial: transcription is GPU-bound)
# --------------------------------------------------------------------------

_pending: queue.Queue = queue.Queue()
_worker_started = False
_upload_sizes: dict[str, int] = {}  # job id -> declared file size, while it is being added
_upload_page: dict[str, str] = {}   # job id -> the page that added it, until its bytes arrive
_upload_writers: set[str] = set()   # job ids whose bytes are arriving right now
_page_streams: dict[str, int] = {}  # page id -> its open live-update streams
PAGE_GONE_GRACE_S = 10.0            # a page whose stream drops and does not come back


def _reap_page_uploads(page_id: str, said_bye: bool = False) -> None:
    """Drop the cards a page created but never sent the bytes for.

    A multi-file add sends files one at a time; when the page is reloaded or
    closed meanwhile, the files still waiting will never come, and their
    cards sat at "Loading… 0%" until a restart. The file being sent at that
    moment cleans up by itself (its connection drops).
    """
    with _sse_lock:
        if not page_id or (not said_bye and _page_streams.get(page_id)):
            return
        orphans = [j for j, p in _upload_page.items() if p == page_id and j not in _upload_writers]
        for job_id in orphans:
            _upload_page.pop(job_id, None)
    for job_id in orphans:
        job = job_queue.get(job_id)
        if job is None or job.status != "uploading":
            continue
        _upload_sizes.pop(job_id, None)
        _discard_upload(job)
        job_queue.remove(job_id)
        _publish("job", {"id": job_id, "status": "deleted"})
        log.info("dropped %s: the page adding it went away before sending it", job.filename)


# Give VRAM back after this long with no work. Before, a model stayed loaded
# until the app closed (large-v3 holds ~3 GB) -- while the user gamed on the
# same GPU hours later. Models loaded with the Load button are pinned and stay.
IDLE_RELEASE_S = 600.0
_idle_loop_started = False


def _job_model_size(job: Job, settings) -> str:
    """Per-job model, then the profile's preset, then the default (as _run_job)."""
    from minutenest.profiles import load_profile

    prof = load_profile(job.profile, language=job.language or settings.language)
    return job.model_size or prof.model or settings.model_size


def _warm_up(job: Job) -> None:
    """Start loading the job's model when its file is added, so pressing Start
    doesn't wait ~9 s for it. Never downloads: the job fetches a missing model
    visibly, as before."""
    try:
        from minutenest import transcribe

        settings = load_settings()
        size = _job_model_size(job, settings)
        if transcribe.is_model_loaded(size) or not model_mgr.is_model_downloaded(size):
            return
        device, compute = settings.device, settings.compute_type
        if device == "cuda" and not transcribe._cuda_available():
            device, compute = "cpu", "int8"  # what the job itself would do
        model_mgr.start_load(size, device, compute,
                             on_update=lambda: _publish("model", model_mgr.model_status(size)))
    except Exception:
        log.exception("model warm-up failed; the job will load the model itself")


def _release_idle_models(now: float | None = None) -> list[str]:
    """Unload models idle for IDLE_RELEASE_S, but never while a job is active."""
    from minutenest import transcribe

    if any(j.status in ("queued", "running") for j in job_queue.list()):
        return []
    released = transcribe.release_idle_models(IDLE_RELEASE_S, now=now)
    for size in released:
        log.info("model %s released after %d min idle: VRAM given back", size, IDLE_RELEASE_S // 60)
        _publish("model", model_mgr.model_status(size))
    return released


def _idle_release_loop() -> None:
    # Meant never to end (a daemon for the app's lifetime): Power of Ten rule 2's
    # stated exception for non-terminating service loops.
    while True:
        time.sleep(30)
        try:
            _release_idle_models()
        except Exception:
            log.exception("idle model release failed")


def _worker_loop() -> None:
    # Meant never to end (the job queue's one worker, a daemon for the app's
    # lifetime): Power of Ten rule 2's exception for service loops.
    while True:
        job, source = _pending.get()
        try:
            _run_job(job, source)
        except Exception:
            # _run_job records every failure of the job itself; reaching this
            # means a bug in that bookkeeping, which must not pass unseen.
            log.exception("job %s: unexpected error in the job runner", job.id)
        finally:
            _pending.task_done()


def _enqueue(job: Job, source: Path) -> None:
    global _worker_started
    with _sse_lock:
        if not _worker_started:
            threading.Thread(target=_worker_loop, daemon=True).start()
            _worker_started = True
    _pending.put((job, source))


_job_runs: dict[str, int] = {}  # job id -> number of its current run (Retry starts a new one)


class _JobRun:
    """One run of one job: its progress reports, Stop and its results folder."""

    def __init__(self, job: Job):
        self.job = job
        self.run = _job_runs[job.id] = _job_runs.get(job.id, 0) + 1
        self.job_dir: Path | None = None

    def progress(self, msg: str, fraction: float | None = None, eta_s: float | None = None,
                 eta_scope: str = "job") -> None:
        # LLM requests abandoned by Stop, a timeout or a failed sibling keep
        # running (and OpenRouter's free-model retries keep reporting waits for
        # ~2 min): they must not rewrite a finished card or a later Retry's bar.
        job = self.job
        if job.status != "running" or _job_runs.get(job.id) != self.run:
            return
        job_queue.log(job, msg, fraction=fraction, eta_s=eta_s, eta_scope=eta_scope)
        _publish("job", {"id": job.id, "status": job.status, "stage": job.stage,
                         "fraction": job.fraction, "eta_s": job.eta_s, "eta_scope": job.eta_scope})

    def cancelled(self) -> bool:
        ev = job_queue.cancel_event(self.job.id)
        return bool(ev is not None and ev.is_set())

    def keep_partial_results(self) -> None:
        """Transcripts written before a failure or Stop stay, and the card can open them;
        an empty folder from a run that got nowhere is removed."""
        d = self.job_dir
        if d is None or not d.is_dir():
            return
        if any(d.iterdir()):
            mark_output_dir(d)
            self.job.out_dir = str(d)
            return
        try:
            d.rmdir()
        except OSError as e:
            log.warning("job %s: could not remove its empty folder %s: %s", self.job.id, d, e)


def _precheck_llm(job: Job, task: str) -> None:
    """Fail fast on bad LLM credentials before transcription burns minutes."""
    from minutenest.llm.base import LLMError
    from minutenest.pipeline import _resolve_llm

    try:
        _resolve_llm(PipelineOptions(task=task)).verify_credentials()
        log.info("job %s: LLM credentials OK", job.id)
    except LLMError as e:
        # 429 means the key was accepted but the provider is throttling.
        # Don't abort: the real call retries with backoff and reports
        # a clear error if the limit persists (transcripts are saved first).
        if not e.is_quota:
            raise
        log.warning("job %s: LLM rate-limited during precheck, continuing: %s", job.id, e)


def _job_options(job: Job, settings, task: str, prompt: str) -> PipelineOptions:
    """The pipeline settings of a job: per-job choices win, then the profile, then Settings."""
    from minutenest.profiles import load_profile

    language = job.language or settings.language
    prof = load_profile(job.profile, language=language)
    return PipelineOptions(
        profile=job.profile, task=task, custom_prompt=prompt,
        output_name=job.task if job.task != "raw" else None,
        output_format=job.output_format or settings.output_format,
        model_size=job.model_size or prof.model or settings.model_size, device=settings.device,
        compute_type=settings.compute_type,
        language=language, display_name=job.filename,
        document_language=job.document_language or settings.document_language or None,
    )


def _job_done(job_run: _JobRun, result) -> None:
    job, job_dir = job_run.job, job_run.job_dir
    if job_dir is None:   # set before the pipeline ran (Power of Ten rule 5: check the invariant)
        raise RuntimeError("the job finished without a results folder")
    mark_output_dir(job_dir)
    job.outputs = {name: str(p) for name, p in result.outputs.items()}
    job.out_dir = str(job_dir)
    job.warnings = list(result.warnings)
    job_queue.set_status(job, "done")
    job_queue.persist()
    log.info("job %s done in %.1fs -> %s (files: %s)", job.id, result.elapsed_s, job_dir,
             ", ".join(sorted(p.name for p in result.outputs.values())))
    _publish("job", {"id": job.id, "status": "done",
                     "outputs": job.outputs, "out_dir": job.out_dir, "warnings": job.warnings})


def _job_cancelled(job_run: _JobRun) -> None:
    job = job_run.job
    job_run.keep_partial_results()
    job_queue.set_status(job, "cancelled")
    job_queue.log(job, "Cancelled.")
    job_queue.clear_cancel(job.id)
    job_queue.persist()
    log.info("job %s cancelled by user", job.id)
    _publish("job", {"id": job.id, "status": "cancelled", "out_dir": job.out_dir})


def _job_failed(job_run: _JobRun, e: Exception) -> None:
    job = job_run.job
    job_run.keep_partial_results()
    job_queue.set_status(job, "failed", error=str(e))
    job_queue.log(job, "Failed.")
    job_queue.persist()
    log.error("job %s failed: %s\n%s", job.id, e, traceback.format_exc())
    _publish("job", {"id": job.id, "status": "failed", "error": str(e), "out_dir": job.out_dir})


def _cancel_before_start(job: Job) -> bool:
    """A Stop that arrived while the job sat in the queue: True when it ended the job."""
    ev = job_queue.cancel_event(job.id)
    if ev is None or not ev.is_set():
        return False
    job_queue.set_status(job, "cancelled")
    job_queue.log(job, "Cancelled.")
    job_queue.clear_cancel(job.id)
    job_queue.persist()
    _publish("job", {"id": job.id, "status": "cancelled"})
    return True


def _run_job(job: Job, source: Path) -> None:
    if _cancel_before_start(job):
        return
    job_queue.set_status(job, "running")
    job_queue.persist()
    _publish("job", {"id": job.id, "status": "running"})
    _set_priority(True)
    job_run = _JobRun(job)
    try:
        settings = load_settings()
        task, prompt = _resolve_task(job.task, job.custom_prompt.strip())
        log.info("job %s (%s) starting: task=%s profile=%s lang=%s model=%s device=%s compute=%s",
                 job.id, job.filename, task, job.profile, job.language or settings.language,
                 job.model_size or settings.model_size, settings.device, settings.compute_type)
        if task != "raw":
            job_run.progress("Checking LLM credentials...")
            _precheck_llm(job, task)
        out_root = _resolve_out_dir(settings)
        try:
            out_root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RuntimeError(f"Cannot create output folder: {out_root}. Check Settings → Save folder.") from e
        opts = _job_options(job, settings, task, prompt)
        job_run.job_dir = _unique_job_dir(out_root, job.filename)
        result = run_pipeline(source, config.WORK_DIR / job.id, job_run.job_dir, opts,
                              progress_cb=job_run.progress, cancel_cb=job_run.cancelled)
        _job_done(job_run, result)
    except CancelledError:
        _job_cancelled(job_run)
    except Exception as e:  # noqa: BLE001 - any failure ends the job and is shown on its card
        _job_failed(job_run, e)
    finally:
        _set_priority(False)
        _discard_work(job.id)
        if job.status == "done":
            _discard_upload(job)  # the result is saved; failed/cancelled keep it for Retry


# --------------------------------------------------------------------------
# LLM model listing (per provider, form values override saved settings)
# --------------------------------------------------------------------------


def _list_keyed(name: str, label: str, saved_key: str, key: str | None, settings) -> dict:
    """OpenAI / Anthropic: nothing can be listed without a key."""
    from minutenest.llm import get_provider

    api_key = _resolve_key(key, saved_key)
    if not api_key:
        return {"models": [], "error": f"Paste an {label} API key first, then refresh."}
    return {"models": get_provider(name, model=settings.llm.model or "", api_key=api_key).list_models()}


def _list_openai(_host: str | None, key: str | None, settings) -> dict:
    return _list_keyed("openai", "OpenAI", settings.llm.openai_api_key, key, settings)


def _list_anthropic(_host: str | None, key: str | None, settings) -> dict:
    return _list_keyed("anthropic", "Anthropic", settings.llm.anthropic_api_key, key, settings)


def _list_compat(host: str | None, key: str | None, settings) -> dict:
    from minutenest.llm import get_provider

    p = get_provider("compat", model=settings.llm.model or "",
                     base_url=(host or "").strip() or settings.llm.compat_base_url,
                     api_key=_resolve_key(key, settings.llm.compat_api_key) or None)
    _keep_keys_home(p, key, host, settings.llm.compat_base_url)
    return {"models": p.list_models()}


def _list_ollama(host: str | None, key: str | None, settings) -> dict:
    from minutenest.llm import get_provider

    p = get_provider("ollama", model=settings.llm.model or "gemma4:31b",
                     host=(host or "").strip() or settings.llm.ollama_host,
                     api_key=_resolve_key(key, settings.llm.ollama_api_key) or None)
    _keep_keys_home(p, key, host, settings.llm.ollama_host)
    warning = _verify_with_fallback(p, "gemma4:31b") if p.api_key else None
    return _with_warning({"models": p.list_models()}, warning)


def _list_openrouter(_host: str | None, key: str | None, settings) -> dict:
    """/models is public: listed first so users can browse before pasting a key;
    the key is checked only when there is one."""
    from minutenest.llm import get_provider

    api_key = _resolve_key(key, settings.llm.openrouter_api_key) or None
    p = get_provider("openrouter", model=settings.llm.model or "google/gemma-4-31b-it:free", api_key=api_key)
    warning = _verify_with_fallback(p, "google/gemma-4-31b-it:free") if api_key else None
    # p.api_key also covers OPENROUTER_API_KEY from the environment
    return _with_warning({"models": p.list_models(), "key_missing": not p.api_key}, warning)


_MODEL_LISTERS = {"openai": _list_openai, "anthropic": _list_anthropic, "compat": _list_compat,
                  "ollama": _list_ollama, "openrouter": _list_openrouter}


def _llm_models_response(provider: str, host: str | None, key: str | None) -> dict:
    """Unified model listing. Only ever returns models/error/key_invalid."""
    settings = load_settings()
    lister = _MODEL_LISTERS.get((provider or settings.llm.provider or "none").lower())
    if lister is None:      # "none" and anything unknown: nothing to list
        return {"models": []}
    try:
        return lister(host, key, settings)
    except Exception as e:  # noqa: BLE001 - whatever went wrong is shown under the model box
        return _llm_error_response(e)


# What to preselect when the user has not chosen a model yet. Never "the first
# one alphabetically": on OpenRouter that silently picked a PAID model
# (aion-labs/aion-2.0, real report), and openrouter/free is a router that can
# land on a safety classifier ("User Safety: safe").
_PREFERRED_MODELS = {
    "openrouter": ["google/gemma-4-31b-it:free"],
    "ollama": ["gemma4:31b"],
    # current, cost-efficient first; never the alphabetically first id (for
    # Anthropic that was claude-fable-5, the most expensive model)
    "openai": ["gpt-5.4-mini", "gpt-5.5", "gpt-4o-mini"],
    "anthropic": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
}


def _recommended_model(provider: str, models: list[str]) -> str:
    for m in _PREFERRED_MODELS.get(provider, []):
        if m in models:
            return m
    if provider == "openrouter":
        free = [m for m in models if m.endswith(":free")]
        return free[0] if free else ""
    return models[0] if models else ""


def _with_warning(result: dict, warning: str | None) -> dict:
    if warning:
        result["warning"] = warning
    return result


def _verify_with_fallback(p, fallback_model: str) -> str | None:
    """Verify credentials; returns a warning string for non-fatal problems.

    - 400/404 (unknown model id) re-probes with a known model.
    - 429 is NOT a bad key: the server authenticated us and is throttling.
      It becomes a warning so the model list still loads and the key is kept.
    """
    from minutenest.llm.base import LLMError

    try:
        p.verify_credentials(timeout=8)
    except LLMError as e:
        if getattr(e, "http_code", None) in (400, 404):
            p.model = fallback_model
            try:
                p.verify_credentials(timeout=8)
            except LLMError as e2:
                if e2.is_quota:
                    return str(e2)
                raise
        elif e.is_quota:
            return str(e)
        else:
            raise
    return None


def _llm_error_response(e: Exception) -> dict:
    code = getattr(e, "http_code", None)
    if code in (401, 403):
        return {"models": [], "key_invalid": True, "error": str(e)}
    return {"models": [], "error": str(e)}


# --------------------------------------------------------------------------
# app factory
# --------------------------------------------------------------------------


class NoCacheStaticFiles(StaticFiles):
    """Serve static files without caching (local app, files change between runs)."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


# ---- only this app's own pages may use it ---------------------------------
# The app listens on 127.0.0.1, and any web page the user has open can make the
# browser send requests there (an <img src>, a form, a fetch): shut the app
# down, delete results, make it fetch from an address the page chooses
# (CWE-352). A page whose domain is pointed at 127.0.0.1 can even read the
# answers (DNS rebinding, CWE-350). Scripts and the command line send neither
# Origin nor Sec-Fetch-Site and are not affected.

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_OWN_PAGE = ("same-origin", "none")      # Sec-Fetch-Site of the app's own pages / a typed address
# A hostile page can send thousands of requests a second; logging each refusal
# would rotate the lines a bug report needs out of the 2 MB log.
REFUSALS_LOGGED_MAX = 20
_refusals_logged = 0


def _is_own_address(host_header: str) -> bool:
    """localhost, an IP address (the app's, or the LAN address `serve --host` was
    given) or this PC's own name -- never a domain an outside page controls."""
    bracketed = host_header.startswith("[")            # an IPv6 address: [::1]:8123
    name = host_header[1:].split("]", 1)[0] if bracketed else host_header.rsplit(":", 1)[0]
    name = name.strip().lower().rstrip(".")
    if name in ("localhost", socket.gethostname().lower()):
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def _refusal(method: str, path: str, scheme: str, headers: dict[str, str]) -> str:
    """Why a request is refused, or "" to serve it."""
    host = headers.get("host", "")
    if host and not _is_own_address(host):
        return "this address is not MinuteNest's"
    if path.startswith("/api/") and headers.get("sec-fetch-site", "same-origin") not in _OWN_PAGE:
        return "requests from other web pages are not accepted"
    origin = headers.get("origin")
    if method in _UNSAFE_METHODS and origin and origin != f"{scheme}://{host}":
        return "requests from other web pages are not accepted"   # browsers without Sec-Fetch-Site
    return ""


def _log_refusal(scope, headers: dict[str, str], reason: str) -> None:
    global _refusals_logged
    if _refusals_logged >= REFUSALS_LOGGED_MAX:
        return
    _refusals_logged += 1
    log.warning("refused %s %r (host %r, origin %r): %s%s", scope["method"], scope["path"],
                headers.get("host"), headers.get("origin"), reason,
                "; further refusals are not logged" if _refusals_logged == REFUSALS_LOGGED_MAX else "")


class _OnlyThisApp:
    """Plain ASGI middleware (not BaseHTTPMiddleware), so live updates and uploads
    stream through untouched."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            reason = _refusal(scope["method"], scope["path"], scope.get("scheme", "http"), headers)
            if reason:
                _log_refusal(scope, headers, reason)
                await JSONResponse({"detail": f"Refused: {reason}."}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app() -> FastAPI:
    from minutenest import logs as applog

    applog.setup_logging()
    app = FastAPI(title="MinuteNest", version=__version__)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    try:
        # Scratch nobody owns any more: a crash, or the window closed mid-upload
        # (the process ends before the upload handler can delete the partial file).
        _sweep_orphans()
    except Exception:
        log.exception("startup sweep of leftover scratch files failed")
    # Any change to a model's state (a job downloaded it, a load finished)
    # reaches the open UI, so the status badge can't go stale.
    model_mgr.set_notifier(lambda size: _publish("model", model_mgr.model_status(size)))
    global _idle_loop_started
    if not _idle_loop_started:
        _idle_loop_started = True
        threading.Thread(target=_idle_release_loop, daemon=True, name="idle-release").start()
    if STATIC_DIR.is_dir():
        app.mount("/static", NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(router)
    app.add_middleware(_OnlyThisApp)
    return app


# --------------------------------------------------------------------------
# routes (registered on the app by create_app)
# --------------------------------------------------------------------------

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    return TEMPLATES.TemplateResponse(request, "index.html")


@router.get("/api/version")
def version():
    return {"version": __version__, "started_at": _started_at}


@router.get("/api/ffmpeg")
def ffmpeg():
    from minutenest import ffbin

    return ffbin.ffmpeg_status()

# ------------------------------- debug log -----------------------------


@router.get("/api/log")
def get_log(tail: int = 0):
    """The debug log for handover after a test run.

    Returns the whole file by default, or the last `tail` bytes.
    """
    from minutenest import logs as applog

    path = applog.log_path_for_ui()
    if not path.is_file():
        return {"path": str(path), "exists": False, "text": ""}
    text = path.read_text(encoding="utf-8", errors="replace")
    if tail and tail > 0 and len(text) > tail:
        text = text[-tail:]
    return {"path": str(path), "exists": True, "bytes": len(text), "text": text}


@router.post("/api/log/open-folder")
def open_log_folder():
    from minutenest import logs as applog

    folder = applog.log_path_for_ui().parent
    if not folder.is_dir():
        raise HTTPException(404, "no log folder yet")
    try:
        _open_in_explorer(folder)
    except OSError as e:
        raise HTTPException(500, f"Explorer failed to start: {e}") from e
    return {"ok": True, "folder": str(folder)}


@router.post("/api/log/ui-error")
def log_ui_error(payload: dict):
    """The UI posts caught errors here so they land in the handover log."""
    log.error("UI error [%s]: %s", payload.get("where", "?"),
              payload.get("message", "?"))
    return {"ok": True}

# ------------------------------ settings -------------------------------


@router.get("/api/settings")
def get_settings():
    return load_settings().to_public_dict()


@router.post("/api/settings")
def post_settings(payload: dict):
    return update_settings(payload).to_public_dict()


@router.get("/api/languages")
def list_languages():
    from minutenest.transcribe import supported_languages

    return {"languages": supported_languages()}

# ------------------------- whisper models ------------------------------


def _known(size: str) -> None:
    if size not in model_mgr.KNOWN_MODELS:
        raise HTTPException(404, "unknown model size")


@router.get("/api/models")
def get_models():
    return {"models": model_mgr.list_models()}


@router.get("/api/models/{size}/status")
def model_status(size: str):
    _known(size)
    return model_mgr.model_status(size)


@router.post("/api/models/{size}/download")
def model_download(size: str):
    _known(size)
    dl = model_mgr.download_model(size, on_update=lambda _: _publish("model", model_mgr.model_status(size)))
    return {"size": size, "status": dl.status}


@router.post("/api/models/{size}/load")
def model_load(size: str):
    _known(size)
    if not model_mgr.is_model_downloaded(size):
        raise HTTPException(400, "model not downloaded yet")
    from minutenest import transcribe

    transcribe.pin_model(size)  # loaded on purpose: never released automatically
    s = load_settings()
    started = model_mgr.start_load(size, s.device, s.compute_type,
                                   on_update=lambda: _publish("model", model_mgr.model_status(size)))
    return {"size": size, "started": started}


@router.post("/api/models/{size}/unload")
def model_unload(size: str):
    _known(size)
    from minutenest import transcribe

    return {"size": size, "unloaded": transcribe.unload_models(size)}


@router.post("/api/models/{size}/delete")
def model_delete(size: str):
    _known(size)
    ok = model_mgr.delete_model(size)
    _publish("model", model_mgr.model_status(size))
    return {"size": size, "deleted": ok}


@router.post("/api/models/open-folder")
def models_folder():
    folder = model_mgr.hub_cache_dir()
    if not folder.is_dir():
        raise HTTPException(404, "model cache folder does not exist yet")
    _open_in_explorer(folder)
    return {"ok": True, "folder": str(folder)}


@router.post("/api/settings/browse-folder")
def browse_folder(payload: dict):
    """Open a native folder picker (server runs locally, so the dialog
    is the real Explorer one) and return the chosen path, if any."""
    current = (payload.get("current") or "").strip()
    start = Path(current) if current and Path(current).is_dir() else Path.home()
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        raise HTTPException(501, "folder picker unavailable in this build — type the path") from None
    root = tk.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(initialdir=str(start),
                                        title="Choose save folder")
    finally:
        with contextlib.suppress(tk.TclError):   # the dialog already took its window down
            root.destroy()
    if not chosen:
        return {"path": None}  # user cancelled
    if not Path(chosen).is_dir():
        raise HTTPException(400, "chosen path is not a folder")
    return {"path": chosen}

# --------------------------- LLM models --------------------------------


@router.get("/api/llm/models")
def llm_models(provider: str | None = None, host: str | None = None,
               key: str | None = None, fresh: bool = False):
    """Model list for the given (or saved) provider; 60s cache, never blocks the UI."""
    settings = load_settings()
    provider = (provider or settings.llm.provider or "none").lower()
    if provider in ("none",):  # static or empty lists
        return _llm_models_response(provider, host, key)
    cache_key = (provider, host or "", (key or "")[-8:], settings.llm.model or "")
    cached = _llm_models_cache.get(cache_key)
    if cached and not fresh and time.time() - cached[0] < 60:
        return cached[1]
    result = _llm_models_response(provider, host, key)
    result["recommended"] = _recommended_model(provider, result.get("models") or [])
    if not result.get("error"):  # never replay a failure for a minute
        _llm_models_cache[cache_key] = (time.time(), result)
    return result

# ------------------------------ profiles -------------------------------


@router.get("/api/profiles")
def get_profiles():
    return {
        "profiles": [{"name": p.name, "description": p.description,
                      "model": p.model, "corrections": len(p.corrections)}
                     for p in list_profiles()],
        "tasks": ["raw", *available_tasks(), "custom"],
        "task_prompts": task_default_prompts(),
        "task_descriptions": {
            "raw": "Plain transcript. No LLM needed.",
            "clean": "Transcript with punctuation and paragraphs fixed.",
            "notes": "Structured meeting notes with sections.",
            "actions": "Action items only, as a checklist.",
            "testplan": "QA test plan with cases.",
            "summary": "Short summary of the meeting.",
            "custom": "Your own prompt.",
        },
    }

# ------------------------------- jobs ----------------------------------


@router.post("/api/jobs")
def create_jobs(files: Annotated[list[UploadFile], File()]):
    """Stage uploads; nothing runs until /start. (Scripts and tests; the UI uses
    /api/jobs/new + PUT .../file.) A plain def runs on a worker thread, so the
    file copy no longer blocks the event loop."""
    settings = load_settings()
    presets = task_default_prompts()
    created = []
    for file in files:
        orig = _safe_name(file.filename or "upload")
        job = job_queue.create(orig, profile=settings.default_profile,
                               task=settings.default_task,
                               custom_prompt=presets.get(settings.default_task, ""))
        _persist_upload(file, _upload_path(job))
        job_queue.set_status(job, "staged")
        job_queue.persist()
        _publish("job", {"id": job.id, "filename": job.filename, "status": "staged"})
        _warm_up(job)
        created.append({"id": job.id, "filename": job.filename})
    return {"created": created}


@router.post("/api/jobs/new")
def new_upload(payload: dict):
    """Step 1 of adding a file: the card exists BEFORE its bytes arrive.

    A 1-2 GB video takes a while to hand over; the user sees "Loading… N%"
    instead of nothing. Step 2 is PUT /api/jobs/{id}/file.
    """
    settings = load_settings()
    presets = task_default_prompts()
    job = job_queue.create(_safe_name(str(payload.get("filename") or "upload")),
                           profile=settings.default_profile, task=settings.default_task,
                           custom_prompt=presets.get(settings.default_task, ""))
    job_queue.set_status(job, "uploading")
    job.stage, job.fraction = "Loading… 0%", 0.0  # ticks are not worth keeping in the job log
    if payload.get("page"):
        _upload_page[job.id] = str(payload["page"])
    try:
        _upload_sizes[job.id] = max(0, int(payload.get("size") or 0))
    except (TypeError, ValueError):
        _upload_sizes[job.id] = 0
    job_queue.persist()
    _publish("job", {"id": job.id, "filename": job.filename, "status": "uploading",
                     "stage": job.stage, "fraction": 0.0})
    return {"id": job.id}


UPLOAD_WRITE_BYTES = 4 * 1024 * 1024   # bytes collected before one write to disk
UPLOAD_TICK_S = 0.4                    # "Loading… N%" is updated at most this often


async def _upload_failed(job: Job, dest: Path, got: int, err: Exception, f=None) -> NoReturn:
    """A write error (disk full, path too long): fail the card with the reason
    instead of leaving it at "Loading… N%" with a partial file."""
    from starlette.concurrency import run_in_threadpool

    if f is not None:
        try:
            await run_in_threadpool(f.close)
        except OSError as close_err:   # flushing the rest hit the same error; the file goes anyway
            log.warning("closing the partial upload of %s failed too: %s", job.filename, close_err)
    try:
        dest.unlink(missing_ok=True)
    except OSError as unlink_err:
        log.warning("could not delete the partial upload %s: %s", dest, unlink_err)
    _upload_sizes.pop(job.id, None)
    reason = getattr(err, "strerror", None) or str(err)
    msg = f"Could not save the file ({reason}). Remove this card and add the file again."
    log.error("upload of %s failed after %d bytes: %r", job.filename, got, err)
    job.stage, job.fraction = "Failed.", None
    job_queue.set_status(job, "failed", error=msg)
    job_queue.persist()
    _publish("job", {"id": job.id, "status": "failed", "error": msg})
    raise HTTPException(500, msg)


async def _upload_cancelled(job: Job, dest: Path, got: int, f) -> dict:
    """The user removed the card (or the page went away) while the file came in."""
    from starlette.concurrency import run_in_threadpool

    await run_in_threadpool(f.close)
    dest.unlink(missing_ok=True)
    _upload_sizes.pop(job.id, None)
    if job_queue.get(job.id) is not None:
        job_queue.remove(job.id)
        _publish("job", {"id": job.id, "status": "deleted"})
    log.info("upload of %s cancelled after %d bytes", job.filename, got)
    return {"ok": False, "cancelled": True}


def _upload_tick(job: Job, got: int, total: int) -> None:
    frac = min(0.99, got / total)
    job.stage, job.fraction = f"Loading… {int(frac * 100)}%", frac
    _publish("job", {"id": job.id, "status": "uploading", "stage": job.stage, "fraction": frac})


async def _receive_upload(request: Request, job: Job, f, total: int) -> int:
    """Copy the request body into ``f`` in big writes; returns the byte count.

    Raises ClientDisconnect when the card was removed meanwhile.
    """
    from starlette.concurrency import run_in_threadpool
    from starlette.requests import ClientDisconnect

    got, last_tick, buf = 0, 0.0, bytearray()
    async for chunk in request.stream():
        if job_queue.get(job.id) is None:
            raise ClientDisconnect()  # the user cancelled: stop reading
        buf += chunk
        got += len(chunk)
        if len(buf) >= UPLOAD_WRITE_BYTES:
            await run_in_threadpool(f.write, bytes(buf))
            buf.clear()
        if total and time.monotonic() - last_tick >= UPLOAD_TICK_S:
            last_tick = time.monotonic()
            _upload_tick(job, got, total)
    if buf:
        await run_in_threadpool(f.write, bytes(buf))
    return got


@router.put("/api/jobs/{job_id}/file")
async def upload_file(job_id: str, request: Request):
    """Step 2: stream the bytes straight into the job's file.

    No temp spool and no second copy (the multipart path wrote a 1 GB video
    to disk twice), and every write runs off the event loop, so the UI and
    live updates keep flowing. Cancel (removing the card) or a dropped
    connection deletes the partial file and the job.
    """
    from starlette.concurrency import run_in_threadpool
    from starlette.requests import ClientDisconnect

    job = job_queue.get(job_id)
    if not job or job.status != "uploading":
        raise HTTPException(409, "this job is not waiting for a file")
    dest = _upload_path(job)
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = _upload_sizes.get(job_id) or int(request.headers.get("content-length") or 0)
    try:
        f = await run_in_threadpool(open, dest, "wb")
    except OSError as e:
        await _upload_failed(job, dest, 0, e)
    _upload_writers.add(job_id)
    got = 0
    try:
        got = await _receive_upload(request, job, f, total)
    except ClientDisconnect:
        return await _upload_cancelled(job, dest, got, f)
    except OSError as e:
        await _upload_failed(job, dest, got, e, f)
    finally:
        _upload_writers.discard(job_id)
        _upload_page.pop(job_id, None)
        if not f.closed:
            await run_in_threadpool(f.close)
    _upload_sizes.pop(job_id, None)
    job.stage, job.fraction = "staged", None
    job_queue.set_status(job, "staged")
    await run_in_threadpool(job_queue.persist)
    _publish("job", {"id": job.id, "filename": job.filename, "status": "staged"})
    # off the event loop: the first call imports CTranslate2 and probes CUDA
    # (~0.5 s), which froze live updates and every click while it ran
    await run_in_threadpool(_warm_up, job)
    return {"ok": True, "bytes": got}


@router.post("/api/jobs/sample")
def create_sample_job():
    """Stage the bundled sample recording as a job (wizard / Try sample)."""
    src = STATIC_DIR / "sample.mp3"
    if not src.is_file():
        raise HTTPException(404, "sample recording missing from this build")
    settings = load_settings()
    presets = task_default_prompts()
    # Honor what the user chose in Settings / the guide. Only the task
    # falls back to raw when it needs an LLM that isn't configured, so the
    # demo can't fail on a missing key.
    task = settings.default_task or "raw"
    if task != "raw" and (settings.llm.provider or "none") == "none":
        task = "raw"
    job = job_queue.create("Sample meeting.mp3", profile=settings.default_profile or "generic",
                           task=task,
                           custom_prompt="" if task in ("raw", "custom") else presets.get(task, ""))
    job.language = "en"  # the bundled sample is English speech (shown on the card)
    dest = _upload_path(job)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    job_queue.set_status(job, "staged")
    job_queue.persist()
    _publish("job", {"id": job.id, "filename": job.filename, "status": "staged"})
    _warm_up(job)
    return {"created": [{"id": job.id, "filename": job.filename}]}


@router.get("/api/jobs")
def list_jobs():
    return {"jobs": [
        {"id": j.id, "filename": j.filename, "profile": j.profile, "task": j.task,
         "custom_prompt": j.custom_prompt, "status": j.status, "stage": j.stage,
         "fraction": j.fraction, "eta_s": j.eta_s, "eta_scope": j.eta_scope,
         "model_size": j.model_size, "language": j.language,
         "document_language": j.document_language, "output_format": j.output_format,
         "error": j.error, "warnings": j.warnings, "outputs": list(j.outputs.keys()),
         "out_dir": j.out_dir, "created_at": j.created_at}
        for j in job_queue.list()
    ]}


@router.get("/api/jobs/{job_id}")
def job_detail(job_id: str):
    j = _job_or_404(job_id)
    return {"id": j.id, "filename": j.filename, "profile": j.profile, "task": j.task,
            "status": j.status, "stage": j.stage, "fraction": j.fraction,
            "eta_s": j.eta_s, "eta_scope": j.eta_scope, "model_size": j.model_size, "language": j.language,
            "document_language": j.document_language, "error": j.error, "warnings": j.warnings,
            "log": j.progress_log, "outputs": list(j.outputs.keys()),
            "out_dir": j.out_dir}


@router.post("/api/jobs/{job_id}/start")
def start_job(job_id: str):
    job = _job_or_404(job_id)
    if job.status != "staged":
        raise HTTPException(400, f"job is {job.status}, only staged jobs can start")
    if job.task == "custom" and not job.custom_prompt.strip():
        raise HTTPException(400, "Write what you want from the transcript in the Prompt box, "
                                 "or pick another output.")
    src = _upload_path(job)
    if not src.is_file():
        raise HTTPException(404, "uploaded file missing")
    job_queue.clear_cancel(job.id)  # a stale cancel must not kill a new start
    job.stage, job.fraction = "Waiting — starts when the job before it is done.", None
    job_queue.set_status(job, "queued")
    job_queue.persist()   # on disk, so a quit now brings it back as interrupted
    _publish("job", {"id": job.id, "status": "queued", "stage": job.stage})
    _enqueue(job, src)
    return {"ok": True}


@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    """Stop a queued/running job. The worker notices between stages
    (and inside the transcribe loop) and marks it cancelled."""
    job = _job_or_404(job_id)
    if not job_queue.request_cancel(job.id):
        raise HTTPException(400, f"job is {job.status}, only queued or running jobs can be stopped")
    _publish("job", {"id": job.id, "status": job.status, "stage": "Stopping…"})
    return {"ok": True}


@router.post("/api/jobs/{job_id}/update")
def update_job(job_id: str, payload: dict):
    """Edit a staged job's settings before it starts."""
    job = _job_or_404(job_id)
    if job.status != "staged":
        raise HTTPException(400, "only staged jobs can be edited")
    if "profile" in payload:
        if payload["profile"] not in [p.name for p in list_profiles()]:
            raise HTTPException(400, "unknown profile")
        job.profile = payload["profile"]
    if "task" in payload:
        task = payload["task"]
        if task not in ["raw", *available_tasks(), "custom"]:
            raise HTTPException(400, "unknown task")
        job.task = task
        job.custom_prompt = "" if task in ("raw", "custom") else task_default_prompts().get(task, "")
    if "custom_prompt" in payload:
        job.custom_prompt = str(payload["custom_prompt"])
    if "model_size" in payload:
        from minutenest.models import KNOWN_MODELS

        model = str(payload["model_size"] or "")
        if model and model not in KNOWN_MODELS:
            raise HTTPException(400, "unknown model size")
        job.model_size = model
    if "language" in payload:
        from minutenest.transcribe import LANGUAGE_NAMES

        lang = str(payload["language"] or "")
        if lang and lang not in LANGUAGE_NAMES:
            raise HTTPException(400, "unknown language code")
        job.language = lang
    if "document_language" in payload:
        from minutenest.transcribe import LANGUAGE_NAMES

        lang = str(payload["document_language"] or "")
        if lang and lang not in LANGUAGE_NAMES:
            raise HTTPException(400, "unknown language code")
        job.document_language = lang
    if "output_format" in payload:
        fmt = str(payload["output_format"] or "")
        if fmt and fmt not in ("md", "txt", "both"):
            raise HTTPException(400, "unknown output format")
        job.output_format = fmt
    job_queue.persist()
    return {"ok": True}


@router.post("/api/jobs/{job_id}/delete")
def delete_job(job_id: str, delete_artifacts: bool = False):
    """Remove one job; optionally delete its output folder (marker-guarded)."""
    job = _job_or_404(job_id)
    if job.status == "running":
        raise HTTPException(400, "cannot delete a running job")
    removed = _remove_artifact_dir(job.out_dir) if delete_artifacts else False
    _discard_upload(job)
    _discard_work(job.id)
    job_queue.remove(job.id)
    _publish("job", {"id": job.id, "status": "deleted"})
    return {"ok": True, "removed_dir": removed}


@router.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str):
    """Restart a failed/cancelled job: back to staged, profile/task/prompt kept."""
    job = _job_or_404(job_id)
    if not job_queue.retry(job.id):
        raise HTTPException(400, f"only failed or cancelled jobs can restart (job is {job.status})")
    _publish("job", {"id": job.id, "status": "staged"})
    return {"ok": True}


@router.get("/api/jobs/{job_id}/download/{name}")
def download(job_id: str, name: str):
    job = _job_or_404(job_id)
    if name not in job.outputs:
        raise HTTPException(404, "download not found")
    path = Path(job.outputs[name])
    if not path.is_file():
        raise HTTPException(404, "file missing")
    stem, ext = Path(job.filename).stem, path.suffix
    media = {".txt": "text/plain; charset=utf-8", ".json": "application/json",
             ".md": "text/markdown; charset=utf-8"}.get(ext, "application/octet-stream")
    return FileResponse(path, filename=path.name if name == "document" else f"{stem}.{name.split('_', 1)[-1]}{ext}",
                        media_type=media, content_disposition_type="attachment")


@router.post("/api/jobs/{job_id}/open-folder")
def open_folder(job_id: str):
    job = _job_or_404(job_id)
    if not job.out_dir:
        raise HTTPException(404, "no output folder for this job")
    folder = Path(job.out_dir)
    if not folder.is_dir():
        raise HTTPException(404, f"folder no longer exists: {folder}")
    try:
        _open_in_explorer(folder, _first_output(job))
    except OSError as e:
        raise HTTPException(500, f"Explorer failed to start: {e}") from e
    return {"ok": True, "folder": str(folder)}

# ------------------------------ shutdown / events ----------------------


@router.post("/api/shutdown")
def shutdown():
    log.info("shutdown requested (Shut down button or /api/shutdown)")
    threading.Thread(target=lambda: (time.sleep(0.3), _kill()), daemon=True).start()
    return {"ok": True}


@router.post("/api/client-bye")
def client_bye(page: str = ""):
    """Beacon sent via navigator.sendBeacon on pagehide/close.

    The browser fallback mode has no window to watch, so this (plus the
    SSE disconnect below) is how the launcher learns the tab is gone.
    """
    global _client_last_gone
    with _sse_lock:
        if not _sse_subscribers:
            _client_last_gone = time.time()
    _reap_page_uploads(page, said_bye=True)
    return {"ok": True}


@router.get("/api/events")
async def events(page: str = ""):
    """Live update stream (SSE). Async is essential: idle streams park on
    the event loop; a sync generator would eat one thread-pool worker per
    open tab until the pool starves and the whole app hangs."""
    global _client_seen, _client_last_gone
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    _bind_sse_loop(asyncio.get_running_loop())
    with _sse_lock:
        _client_seen = True
        _client_last_gone = None  # a client is here again (reload, new tab)
        _sse_subscribers.append(q)
        if page:
            _page_streams[page] = _page_streams.get(page, 0) + 1
    loop = asyncio.get_running_loop()

    async def stream():
        global _client_last_gone
        try:
            while True:   # until the browser disconnects (CancelledError): a service loop
                event, data = await q.get()
                yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            with _sse_lock:
                if q in _sse_subscribers:
                    _sse_subscribers.remove(q)
                if not _sse_subscribers:
                    _client_last_gone = time.time()  # all tabs closed
                if page:
                    _page_streams[page] -= 1
                    if not _page_streams[page]:
                        del _page_streams[page]
                        # no bye (a crash, a killed window): give a reconnect a chance
                        loop.call_later(PAGE_GONE_GRACE_S, _reap_page_uploads, page)

    return StreamingResponse(stream(), media_type="text/event-stream")

# --------------------------------------------------------------------------
# small shared pieces
# --------------------------------------------------------------------------


def _bind_sse_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _sse_loop
    if _sse_loop is None:
        _sse_loop = loop


def _job_or_404(job_id: str) -> Job:
    job = job_queue.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


def _first_output(job: Job) -> str:
    """Any existing file in the job's output folder, for Explorer to select."""
    folder = Path(job.out_dir)
    if not folder.is_dir():
        return ""
    for p in sorted(folder.iterdir()):
        if p.is_file():
            return p.name
    return ""


def _kill() -> None:
    """Quit: end the process now, with exit code 0 (a requested stop).

    Jobs and settings are already on disk; a graceful interpreter shutdown can
    hang on CUDA teardown. (os.kill(own pid, SIGTERM) was a TerminateProcess
    with exit code 15 on Windows, indistinguishable from being killed.)
    """
    import os

    logging.shutdown()
    os._exit(0)
