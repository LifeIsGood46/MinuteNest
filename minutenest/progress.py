"""One progress bar and one ETA for the whole job.

The pipeline used to map each stage onto a hard-coded slice of the bar and let
the transcriber print its own "ETA" for just that step, so the bar jumped back
and the countdown described an arbitrary sub-step. ``JobProgress`` owns the
whole-job picture instead: every stage reports its own 0..1, the tracker turns
that into one monotonic overall fraction and one remaining-time estimate.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

Emit = Callable[[str, "float | None", "float | None", str], None]


class JobProgress:
    """Weighted multi-stage progress.

    ``plan`` maps stage name -> relative weight (any scale; normalised), in the
    order the stages run. ``emit(message, overall_fraction, eta_seconds, scope)``
    is called on every change. ``scope`` says what the estimate covers: "job",
    or "transcript" while a later stage's length can't be known yet (an LLM's
    speed is only known once it answers; a guess of 15 s per request showed
    "less than a minute" for jobs that then waited minutes).
    """

    def __init__(self, emit: Emit, plan: dict[str, float], clock: Callable[[], float] = time.time):
        self._emit = emit
        self._plan = dict(plan)
        self._total = sum(self._plan.values()) or 1.0
        self._clock = clock
        self._done: set[str] = set()
        self._stage: str | None = None
        self._stage_t0 = 0.0
        self._stage_frac = 0.0
        self._ready = False
        self._first: tuple[float, float] | None = None  # (time, fraction) of first real progress
        self._expect: dict[str, float] = {}
        self._max = 0.0
        # cloud LLM requests report from up to three threads at once
        self._lock = threading.RLock()

    # -- state -------------------------------------------------------------
    @property
    def overall(self) -> float:
        value = sum(w for s, w in self._plan.items() if s in self._done)
        if self._stage and self._stage not in self._done:
            value += self._plan.get(self._stage, 0.0) * self._stage_frac
        self._max = max(self._max, min(1.0, value / self._total))
        return self._max

    def expect(self, stage: str, seconds: float) -> None:
        """How long a stage that has not started yet is expected to take."""
        with self._lock:
            self._expect[stage] = max(0.0, float(seconds))

    def _unknown_later(self) -> list[str]:
        """Stages still to come whose length nobody can tell yet."""
        names = list(self._plan)
        if self._stage not in names:
            return []
        return [s for s in names[names.index(self._stage) + 1:]
                if s not in self._done and s not in self._expect]

    def scope(self) -> str:
        """What ``eta()`` covers: "job", or "transcript" when a later stage is unknown."""
        return "transcript" if "llm" in self._unknown_later() else "job"

    def eta(self) -> float | None:
        """Seconds left for the job (or the part ``scope()`` names), None while there is
        nothing to go on."""
        stage = self._stage
        if stage is None:
            return None
        names = list(self._plan)
        later = 0.0
        if stage in names:
            for s in names[names.index(stage) + 1:]:
                if s not in self._done:
                    later += self._expect.get(s, 0.0)
        frac = 1.0 if stage in self._done else self._stage_frac
        if frac > 0:
            now = self._clock()
            elapsed = max(0.0, now - self._stage_t0)
            current = elapsed / frac * (1.0 - frac) if frac < 1.0 else 0.0
            # Use the speed measured since the first real progress: time spent
            # loading a model before the first segment must not stretch the
            # estimate of the work that follows.
            first = self._first
            if first and frac < 1.0 and frac - first[1] >= 0.01 and now - first[0] >= 2.0:
                current = (now - first[0]) / (frac - first[1]) * (1.0 - frac)
            elif self._ready and frac < 1.0:
                return None  # first tick only: its timing includes decoding the first window
        elif stage in self._expect:
            current = self._expect[stage]
        else:
            return None
        total = current + later
        is_last = bool(names) and stage == names[-1]
        if total <= 0 and not is_last and frac >= 1.0:
            return None  # between stages: nothing to extrapolate from yet
        return total

    # -- transitions ---------------------------------------------------------
    def begin(self, stage: str, msg: str | None = None) -> None:
        with self._lock:
            if self._stage and self._stage != stage:
                self._done.add(self._stage)
            self._stage = stage
            self._stage_t0 = self._clock()
            self._stage_frac = 0.0
            self._first = None
            self._ready = False
            self._done.discard(stage)
            self.say(msg)

    def mark_ready(self) -> None:
        """The slow one-off setup of this stage (loading a model) is over.

        Time spent before this must not count when extrapolating the remaining
        time. Only the first call per stage counts.
        """
        with self._lock:
            if not self._ready and self._stage:
                self._ready = True
                self._stage_t0 = self._clock()

    def update(self, stage_fraction: float, msg: str | None = None) -> None:
        with self._lock:
            f = max(0.0, min(1.0, float(stage_fraction)))
            self._stage_frac = max(self._stage_frac, f)
            if self._first is None and self._stage_frac > 0:
                self._first = (self._clock(), self._stage_frac)
            self.say(msg)

    def finish(self, stage: str | None = None, msg: str | None = None) -> None:
        with self._lock:
            stage = stage or self._stage
            if stage:
                self._done.add(stage)
                if stage == self._stage:
                    self._stage_frac = 1.0
            self.say(msg)

    def complete(self, msg: str | None = None) -> None:
        with self._lock:
            self._done.update(self._plan)
            self._stage_frac = 1.0
            self.say(msg)

    def say(self, msg: str | None) -> None:
        with self._lock:
            if msg is None and not self._stage:
                return
            self._emit(msg or "", self.overall, self.eta(), self.scope())
