"""Persistent job queue and state for the web app.

Jobs are saved to jobs.json after every meaningful change, so the queue
survives app restarts. "Clear" operations are deliberately conservative:
they only ever touch folders the app itself created (marker-file guarded).
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

from minutenest import config

log = logging.getLogger("minutenest.webapp.jobs")

STATE_PATH = config.APP_DIR / "jobs.json"
MARKER_NAME = ".minutenest-output"


@dataclass
class Job:
    id: str
    filename: str
    profile: str = "generic"
    task: str = "raw"               # raw | clean | notes | actions | testplan | summary | custom
    custom_prompt: str = ""
    status: str = "queued"          # queued | running | done | failed | cancelled
    stage: str = "queued"
    fraction: float | None = None   # 0..1 overall progress (None = unknown)
    eta_s: float | None = None      # seconds left (None = unknown)
    eta_scope: str = "job"          # what eta_s covers: "job", or "transcript" (LLM step after it)
    model_size: str = ""            # per-job override; blank = profile/settings
    language: str = ""              # per-job override; blank = settings
    document_language: str = ""     # per-job override; blank = settings
    output_format: str = ""         # md | txt | both; blank = settings default
    progress_log: list[str] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)
    out_dir: str = ""               # where final artifacts were saved on disk
    error: str = ""
    warnings: list[str] = field(default_factory=list)  # done, but the user should know this
    created_at: float = field(default_factory=time.time)


class JobQueue:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._load()

    # ---------------- persistence ----------------

    def _load(self) -> None:
        if not STATE_PATH.is_file():
            return
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            for jd in data.get("jobs", []):
                job = Job(**jd)
                if job.status == "uploading":
                    continue  # its file never finished arriving; the orphan sweep removes it
                # Jobs interrupted by a restart are dead; staged ones survive.
                if job.status in ("queued", "running"):
                    job.status = "failed"
                    job.error = "interrupted by app restart"
                    job.stage = "failed"
                # Legacy entries stored the error text as the stage too
                # (rendered the message twice); collapse to a neutral stage.
                if job.status == "failed" and job.stage.startswith("ERROR:"):
                    job.stage = "Failed."
                self._jobs[job.id] = job
        except (json.JSONDecodeError, TypeError, KeyError):
            pass  # corrupt state file — start fresh rather than crash

    def _save(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {"jobs": [asdict(j) for j in self._jobs.values()]}
        STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---------------- queue ops ----------------

    def create(self, filename: str, profile: str, task: str, custom_prompt: str = "") -> Job:
        job = Job(id=uuid.uuid4().hex[:12], filename=filename,
                  profile=profile, task=task, custom_prompt=custom_prompt)
        with self._lock:
            self._jobs[job.id] = job
            self._save()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def log(self, job: Job, msg: str, fraction: float | None = None,
            eta_s: float | None = None, eta_scope: str = "job") -> None:
        with self._lock:
            if msg:
                if not msg.startswith("  ... "):  # routine position ticks stay out of the saved log
                    job.progress_log.append(msg)
                job.stage = msg
            if fraction is not None:
                # one bar for the whole job: it only ever moves forward
                job.fraction = min(1.0, max(job.fraction or 0.0, fraction))
            job.eta_s = None if eta_s is None else max(0.0, float(eta_s))
            job.eta_scope = eta_scope

    def set_status(self, job: Job, status: str, error: str = "") -> None:
        with self._lock:
            job.status = status
            job.error = error
            if status != "running":
                job.eta_s = None

    def persist(self) -> None:
        with self._lock:
            self._save()

    def remove(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)
            self._cancels.pop(job_id, None)
            self._save()

    def retry(self, job_id: str):
        """Reset a failed/cancelled job to staged so it can start again.

        Profile/task/prompt are kept; error, stage and stale outputs are
        cleared. Returns the job, or None when it isn't retryable.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status not in ("failed", "cancelled"):
                return None
            job.status = "staged"
            job.stage = ""
            job.error = ""
            job.outputs = {}
            job.out_dir = ""
            job.warnings = []
            job.fraction = None
            job.eta_s = None
            self._save()
            return job

    # ---------------- cancellation ----------------

    def request_cancel(self, job_id: str) -> bool:
        """Signal a queued/running job to stop. Returns False if not cancellable.

        Set-only (never cleared except on start/retry), so a cancel that
        lands between enqueue and the worker picking the job up still wins.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status not in ("queued", "running"):
                return False
            self._cancels.setdefault(job_id, threading.Event()).set()
            return True

    def cancel_event(self, job_id: str) -> threading.Event | None:
        with self._lock:
            return self._cancels.get(job_id)

    def clear_cancel(self, job_id: str) -> None:
        with self._lock:
            self._cancels.pop(job_id, None)

    # ---------------- clearing ----------------

    def clear(self, delete_artifacts: bool) -> int:
        """Remove all jobs from the list (and memory).

        When delete_artifacts is True, also remove each job's output folder —
        but ONLY folders containing our marker file, and each such folder is
        verified to be a direct, app-created child before removal.
        """
        with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
            self._save()
        removed = 0
        if delete_artifacts:
            for job in jobs:
                if _remove_artifact_dir(job.out_dir):
                    removed += 1
        return removed


def mark_output_dir(path: Path) -> None:
    """Stamp the marker file into a freshly created output folder."""
    marker = path / MARKER_NAME
    try:
        marker.write_text("created by minutenest", encoding="utf-8")
        if sys.platform.startswith("win"):
            import ctypes

            FILE_ATTRIBUTE_HIDDEN = 0x2
            ctypes.windll.kernel32.SetFileAttributesW(str(marker), FILE_ATTRIBUTE_HIDDEN)
    except OSError:
        pass


def _removable_job_folder(out_dir: str) -> Path | None:
    """``out_dir`` when every safety rule below allows deleting it, else None."""
    folder = Path(out_dir) if out_dir else None
    if folder is None or not folder.is_dir() or not (folder / MARKER_NAME).is_file():
        return None  # missing, or not ours: never touch it
    forbidden = {
        Path.home(),
        Path(folder.anchor),  # drive root e.g. C:\
        config.WORK_DIR.resolve(),
        config.WORK_DIR.parent.resolve(),
        config.OUT_DIR,
        config.OUT_DIR.parent,
    }
    try:
        resolved = folder.resolve()
        if any(resolved == Path(f).resolve() for f in forbidden):
            return None
    except OSError:
        return None
    return folder


def _remove_artifact_dir(out_dir: str) -> bool:
    """Safely delete one job's artifact folder.

    Safety rules (in order):
      1. Must be non-empty string and an existing directory.
      2. Must contain our marker file.
      3. Must not be a filesystem root, the home dir, or any of the app's own
         working dirs.
      4. Only removes the folder itself; nothing outside it is touched.
    """
    folder = _removable_job_folder(out_dir)
    if folder is None:
        return False
    shutil.rmtree(folder, ignore_errors=True)
    # Explorer commonly has a handle on the folder (the user pressed "Open
    # folder" a moment ago), which makes rmtree raise WinError 32. Retry
    # briefly so the usual transient lock succeeds, then give up gracefully:
    # the job stays but the endpoint must not 500 (real report: deleting
    # right after Open folder crashed the request).
    import time as _time

    for _ in range(5):
        if not folder.exists():
            return True
        _time.sleep(0.2)
        shutil.rmtree(folder, ignore_errors=True)
    try:
        shutil.rmtree(folder)
    except FileNotFoundError:
        return True
    except OSError as e:
        log.warning("could not fully delete %s: %s", folder, e)
        return False
    return True


job_queue = JobQueue()
