"""LLM-written results from a transcript: the cleaned transcript or a document.

* ``clean``: the transcript rewritten with punctuation and misheard terms fixed;
  sent in small pieces (each answer is as long as its piece).
* documents (notes, actions, testplan, summary, custom): written straight from
  the transcript. Long transcripts are summarised part by part, then merged.
  There used to be a cleanup request in front of every document; its result was
  never saved, it was the slowest kind of request (the answer is as long as the
  meeting), and a weak free model shortened 6000 characters to 1000 before the
  document request saw them. The document instruction now says the text comes
  from speech recognition instead.

Every request streams (``llm.stream``): bounded by deadlines, cancellable, with
progress. An answer that is not a usable result (no text, only thinking, a safety
classifier's verdict, cut at the token limit, a runaway, a silent model) is asked
for again, a bounded number of times.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections.abc import Callable

from .errors import CancelledError
from .llm import LLMProvider
from .llm.base import LLMEmptyAnswer, LLMError, LLMRefusal
from .llm.stream import LLMRunaway, LLMTimeout, Watch

log = logging.getLogger("minutenest.polish")

# Size of one cleanup request. Small enough that a local model answers in a
# minute or two, big enough that the instruction overhead stays negligible.
CLEAN_CHUNK_CHARS = 6000
# Longest input sent to one "make the document" request; longer texts are
# summarised part by part and the parts merged.
DOC_CHUNK_CHARS = 24000
# A partial result is a summary; anything longer than this is cut when the parts
# are merged, so every merge request stays far below the model's context limit.
PARTIAL_CAP_CHARS = 5800
# Merge rounds at most: groups of >= 2, so 20 rounds cover over a million parts.
MAX_MERGE_ROUNDS = 20
# How often one request is asked for again when its answer is not usable. The
# free router picks a random model per try (4 usable answers in 12, measured).
TRIES = 4
ROUTER_TRIES = 6
ROUTER = "openrouter/free"

_ASR_NOTE = ("The transcript comes from automatic speech recognition: words may be misheard, "
             "names and domain terms mangled, and filler or short phrases repeated. Infer what "
             "was meant and never comment on the transcript's errors.")

_CLEAN_SYSTEM = (
    "You are an expert editor cleaning up an automatic speech-to-text transcript "
    "of a technical meeting. Fix misheard domain terms, punctuation, casing, "
    "disfluencies and obvious ASR errors. Preserve meaning — do not add facts. "
    "Keep the original language. Output cleaned prose with short paragraphs."
)

_TASKS: dict[str, str] = {
    "clean": "Return only the cleaned transcript. No commentary.",
    "notes": (
        "From the transcript, produce concise meeting notes: summary, "
        "key discussion points, and decisions. Markdown format."
    ),
    "actions": (
        "Extract all action items: owner (if mentioned), task, and any deadlines. "
        "Return a markdown checklist."
    ),
    "testplan": (
        "Turn the transcript into a structured test plan. Infer features, "
        "scenarios (numbered steps + expected results), and priorities. Markdown."
    ),
    "summary": "Write a 3-6 sentence executive summary of the meeting.",
}

TASK_SYSTEM = (
    "You are a senior technical writer producing documentation from a meeting "
    "transcript. Be structured, accurate, and do not invent details. " + _ASR_NOTE
)


def language_rule(language: str | None, spoken: str | None = None) -> str:
    """Which language the document must be in. Without it, models answered an
    English instruction in English: a Russian meeting got an English test plan.
    ``spoken`` is the meeting's language when the user asked for another one."""
    from .transcribe import LANGUAGE_NAMES

    name = LANGUAGE_NAMES.get((language or "").lower())
    if not name:
        return "Write the whole document in the same language as the transcript."
    said = LANGUAGE_NAMES.get((spoken or "").lower())
    if said and said != name:
        return (f"Write the whole document in {name}, including headings and table text. "
                f"The meeting was held in {said}: translate what was said. "
                "Keep product names and code as spoken.")
    return (f"Write the whole document in {name}, the language of the meeting, "
            "including headings and table text. Keep product names and code as spoken.")


_REFUSAL_MARKERS = (
    "no cleaned transcript was included",
    "no transcript was provided",
    "i can't infer",
    "i cannot infer",
    "please paste the cleaned transcript",
    "please provide the transcript",
    "send the cleaned transcript",
)

_CLASSIFIER_RE = re.compile(
    r"^\s*(?:(?:user|agent|assistant|response)\s+)?safety\s*:\s*(?:safe|unsafe)\b|^\s*(?:safe|unsafe)\s*$",
    re.I)


def looks_like_classifier(text: str) -> bool:
    """A safety classifier's verdict ("User Safety: safe") instead of text."""
    t = (text or "").strip()
    return len(t) < 200 and bool(_CLASSIFIER_RE.search(t))


# A plain refusal ("I'm sorry, but I can't help with that request."): only when
# that is all there is, so a document that opens with "I'm sorry to report..." stays.
_SHORT_REFUSAL_RE = re.compile(
    r"\b(?:i'?m sorry|i am sorry|sorry|unfortunately)\b.{0,40}\b(?:can'?t|cannot|can not|unable|won'?t)\b"
    r"|\bi (?:can'?t|cannot|am unable to|won'?t) (?:help|assist|do that|comply|provide)"
    r"|\bas an ai\b"
    r"|(?:извините|простите|к сожалению)[^.]{0,40}не могу"
    r"|я не могу (?:помочь|выполнить|с этим)",  # noqa: RUF001 - Russian words, on purpose
    re.I)
_SHORT_REFUSAL_MAX = 400


def looks_like_refusal(text: str) -> bool:
    """True when the model refused instead of working: it asked for the
    transcript, or answered with nothing but a short refusal."""
    t = (text or "").strip().lower()
    if any(m in t[:300] for m in _REFUSAL_MARKERS):
        return True
    return len(t) <= _SHORT_REFUSAL_MAX and bool(_SHORT_REFUSAL_RE.search(t))


# --- splitting -----------------------------------------------------------------

def split_text(text: str, max_chars: int) -> list[str]:
    """Split on paragraph (then sentence, then word) boundaries; never drops text."""
    text = (text or "").strip()
    if not text:
        return []
    pieces: list[str] = []
    for raw_para in re.split(r"\n\s*\n", text):
        para = raw_para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            pieces.append(para)
            continue
        buf = ""
        for raw_sent in re.split(r"(?<=[.!?…])\s+", para):
            sent = raw_sent
            while len(sent) > max_chars:  # no punctuation at all: cut on whitespace
                cut = sent.rfind(" ", 0, max_chars)
                cut = cut if cut > max_chars // 2 else max_chars
                if buf:
                    pieces.append(buf)
                    buf = ""
                pieces.append(sent[:cut].strip())
                sent = sent[cut:].strip()
            if buf and len(buf) + 1 + len(sent) > max_chars:
                pieces.append(buf)
                buf = sent
            else:
                buf = f"{buf} {sent}".strip()
        if buf:
            pieces.append(buf)
    chunks: list[str] = []
    cur = ""
    for p in pieces:
        if cur and len(cur) + 2 + len(p) > max_chars:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def _merge_group_size() -> int:
    return max(2, DOC_CHUNK_CHARS // (PARTIAL_CAP_CHARS + 2))


def _doc_call_count(n_parts: int) -> int:
    """Requests needed for a document built from ``n_parts`` input parts."""
    if n_parts <= 1:
        return 1
    calls, level = n_parts, n_parts
    while level > 1:
        level = -(-level // _merge_group_size())
        calls += level
    return calls


def plan_calls(text: str, task: str) -> int:
    """Requests the LLM stage will make for ``text`` (drives the progress bar)."""
    if task == "clean":
        return len(split_text(text, CLEAN_CHUNK_CHARS)) or 1
    return _doc_call_count(len(split_text(text, DOC_CHUNK_CHARS)) or 1)


# --- progress ---------------------------------------------------------------------

def _chars(n: int) -> str:
    return f"{n:,} characters".replace(",", " ")


class Progress:
    """The LLM stage's share of the job, request by request and character by character.

    ``emit(fraction, message)`` gets the stage fraction (0..1, never backwards) and a
    line for the card. A request counts by the characters it has streamed against
    the length expected of its answer, capped below 1 until it is finished.
    """

    def __init__(self, total_calls: int, emit: Callable[[float, str], None] | None):
        self.total = max(1, total_calls)
        self._emit = emit
        self._lock = threading.Lock()
        self._done = 0
        self._live: dict[int, float] = {}   # request id -> share of it received
        self._next_id = 0
        self._best = 0.0

    def start(self, what: str, expected_chars: int) -> Slot:
        with self._lock:
            self._next_id += 1
            self._live[self._next_id] = 0.0
            slot = Slot(self, self._next_id, what, max(500, expected_chars))
        slot.say("waiting for the model's answer…")
        return slot

    def _update(self, key: int, share: float | None, done: bool, message: str) -> None:
        with self._lock:
            if done:
                self._live.pop(key, None)
                self._done += 1
            elif share is not None:
                self._live[key] = max(self._live.get(key, 0.0), min(0.95, share))
            value = min(1.0, (self._done + sum(self._live.values())) / self.total)
            self._best = max(self._best, value)
            best = self._best
        if self._emit:
            self._emit(best, message)


class Slot:
    """One request's line on the card."""

    TICK_S = 0.5   # streamed progress is shown at most this often
    # While a reasoning model thinks, its request's share creeps toward a third
    # of the whole, flattening out: a frozen bar during minutes of hidden
    # thinking looked like a hang (openrouter/free, 95 s at 60%). Thinking runs
    # several times longer than the answer (measured: 16 000 chars for 1 500).
    THINKING_SHARE = 1 / 3
    THINKING_SCALE = 4     # thinking this many answers' worth gets ~63% of THINKING_SHARE

    def __init__(self, progress: Progress, key: int, what: str, expected: int):
        self._p, self._key, self.what, self._expected = progress, key, what, expected
        self.model = ""
        self._last_tick = 0.0
        self._was_thinking = False
        self._thought = 0.0   # share reached while the model thought

    def say(self, status: str, share: float | None = None, *, routine: bool = False) -> None:
        # "  ... " marks a routine tick: shown on the card, kept out of the job's log
        prefix = "  ... " if routine else ""
        self._p._update(self._key, share, False, f"{prefix}LLM stage: {self.what} — {status}")

    def streamed(self, text_chars: int, thinking_chars: int) -> None:
        thinking = not text_chars
        now = time.monotonic()
        if thinking == self._was_thinking and now - self._last_tick < self.TICK_S:
            return
        self._last_tick, self._was_thinking = now, thinking
        if thinking:
            who = f"{self.model} " if self.model else "the model "
            self._thought = self.THINKING_SHARE * (
                1 - math.exp(-thinking_chars / (self.THINKING_SCALE * self._expected)))
            self.say(f"{who}is thinking ({_chars(thinking_chars)})…", self._thought, routine=True)
        else:
            # the document fills what the thinking left, so the bar moves on at once
            share = self._thought + (1 - self._thought) * text_chars / self._expected
            self.say(f"{_chars(text_chars)} so far", share, routine=True)

    def finished(self) -> None:
        self._p._update(self._key, None, True, f"LLM stage: {self.what} — done")


# --- one request, asked again until its answer is usable ---------------------------

class NoUsableAnswer(LLMError):
    """Every try of one request gave something that is not a usable result."""


def _tries_for(llm: LLMProvider) -> int:
    return ROUTER_TRIES if getattr(llm, "model", "") == ROUTER else TRIES


def _complete(llm: LLMProvider, system: str, user: str, watch: Watch,
              cancel_cb: Callable[[], bool] | None):
    """``llm.complete`` on a helper thread, so Stop ends the job at once even while a
    provider waits out a busy model's back-off. Every wait inside is bounded."""
    box: dict = {}
    finished = threading.Event()

    def work():
        try:
            box["answer"] = llm.complete(system=system, user=user, watch=watch)
        except BaseException as e:  # noqa: BLE001 - re-raised in the caller
            box["error"] = e
        finally:
            finished.set()

    threading.Thread(target=work, daemon=True, name="llm-call").start()
    while not finished.wait(0.25):
        if cancel_cb and cancel_cb():
            raise CancelledError("cancelled by user")
    if "error" in box:
        raise box["error"]
    return box["answer"]


def ask_usable(llm: LLMProvider, system: str, user: str, *, slot: Slot,
               cancel_cb: Callable[[], bool] | None, min_chars: int, max_chars: int) -> str:
    """One request's usable answer, asking again (bounded) when it is not one.

    Raises ``LLMError`` naming the last problem when no try was usable; a
    refusal from a model the user picked is final (asking it again won't help).
    """
    router = getattr(llm, "model", "") == ROUTER
    tries = _tries_for(llm)
    problem = ""
    for attempt in range(1, tries + 1):
        watch = Watch(cancel=cancel_cb, on_progress=slot.streamed, max_chars=max_chars, attempt=attempt)
        try:
            answer = _complete(llm, system, user, watch, cancel_cb)
        except (LLMEmptyAnswer, LLMTimeout, LLMRunaway) as e:
            problem = str(e)
        except LLMRefusal as e:
            if not router:
                raise
            problem = str(e)
        except LLMError as e:
            if not router or attempt == tries:
                raise                       # the provider already waited out a busy model
            problem = str(e)
        else:
            slot.model = answer.model if answer.model != getattr(llm, "model", "") else ""
            text = answer.text.strip()
            if answer.finish in ("length", "max_tokens"):
                problem = f"{answer.model} was cut off at its token limit"
            elif answer.finish in ("error", "content_filter"):
                problem = f"{answer.model} stopped with “{answer.finish}” before finishing"
            elif looks_like_classifier(text):
                problem = f"{answer.model} is a safety classifier, it answered “{text[:40]}”"
            elif router and looks_like_refusal(text):
                problem = f"{answer.model} refused"
            elif len(text) < min_chars and not looks_like_refusal(text):
                problem = f"{answer.model} answered with only {len(text)} characters"
            else:
                slot.finished()
                return text
        log.warning("%s: try %d of %d not usable: %s", slot.what, attempt, tries, problem)
        if attempt < tries:
            slot.say(f"{problem.rstrip('.')}; asking again (try {attempt + 1} of {tries})", 0.0)
    raise NoUsableAnswer(f"The model gave no usable answer in {tries} tries (last: {problem.rstrip('.')}). "
                   "Pick another model in Settings. The transcript itself was saved.")


def parallel_requests(llm: LLMProvider) -> int:
    """How many requests ``llm`` takes at once (at least 1)."""
    return max(1, llm.parallel_requests())


def _in_parallel(fn: Callable, items: list, llm: LLMProvider,
                 cancel_cb: Callable[[], bool] | None) -> list:
    """``fn(item, cancel_cb)`` for every item; results in input order.

    Independent requests (cleanup pieces, document parts, merges) go out side
    by side when the provider is a cloud API, one by one for a model on this
    PC (see ``LLMProvider.parallel_requests``). The first failure or Stop ends
    the batch at once instead of waiting for the rest.
    """
    workers = max(1, min(parallel_requests(llm), len(items)))
    if workers == 1:
        results = []
        for item in items:
            if cancel_cb and cancel_cb():
                raise CancelledError("cancelled by user")
            results.append(fn(item, cancel_cb))
        return results

    from concurrent.futures import ThreadPoolExecutor, as_completed

    stop = threading.Event()

    def cancelled() -> bool:
        return stop.is_set() or bool(cancel_cb and cancel_cb())

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="llm-part")
    futures = {pool.submit(fn, item, cancelled): i for i, item in enumerate(items)}
    results: list = [None] * len(items)
    try:
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()
    except BaseException:
        stop.set()  # the other requests notice within a poll tick and give up
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results


# --- the cleaned transcript (task "clean") -----------------------------------------

def clean_transcript(transcript_text: str, llm: LLMProvider, *,
                     progress: Progress | None = None,
                     cancel_cb: Callable[[], bool] | None = None,
                     language: str | None = None) -> str:
    """The transcript, cleaned piece by piece (it IS the result of task "clean").

    A piece no try could clean (typical for garbled audio) keeps its original text
    instead of failing the job. ``language``: write it in this language instead
    of the original one (a document language of its own).
    """
    chunks = split_text(transcript_text, CLEAN_CHUNK_CHARS)
    if not chunks:
        return ""
    progress = progress or Progress(len(chunks), None)
    system = _CLEAN_SYSTEM
    if language:
        from .transcribe import LANGUAGE_NAMES

        system = system.replace("Keep the original language.",
                                f"Translate it into {LANGUAGE_NAMES.get(language, language)}.")

    def clean_one(item: tuple[int, str], cancel: Callable[[], bool] | None) -> str:
        i, chunk = item
        what = "cleaning the transcript" + (f" (part {i} of {len(chunks)})" if len(chunks) > 1 else "")
        slot = progress.start(what, len(chunk))
        try:
            cleaned = ask_usable(llm, system, f"{_TASKS['clean']}\n\n---\n{chunk}", slot=slot, cancel_cb=cancel,
                                 min_chars=int(0.25 * len(chunk)), max_chars=3 * len(chunk) + 2000).strip()
            if not looks_like_refusal(cleaned):
                return cleaned
            log.warning("cleanup of part %d/%d was refused; keeping its original text", i, len(chunks))
            return chunk
        except NoUsableAnswer as e:
            log.warning("cleanup of part %d/%d failed (%s); keeping its original text", i, len(chunks), e)
            slot.finished()
            return chunk

    return "\n\n".join(_in_parallel(clean_one, list(enumerate(chunks, 1)), llm, cancel_cb))


# --- documents ----------------------------------------------------------------------

def _expected_doc_chars(source_chars: int) -> int:
    """About how long a document is, for the progress bar only."""
    return max(1500, min(8000, int(0.3 * source_chars)))


def _run_doc(instruction: str, text: str, llm: LLMProvider, *, progress: Progress | None, cancel_cb,
             language: str | None = None, spoken: str | None = None) -> str:
    system = f"{TASK_SYSTEM} {language_rule(language, spoken)}"
    parts = split_text(text, DOC_CHUNK_CHARS) or [text]
    progress = progress or Progress(_doc_call_count(len(parts)), None)

    def ask(instr: str, body: str, what: str, cancel: Callable[[], bool] | None) -> str:
        slot = progress.start(what, _expected_doc_chars(len(body)))
        return ask_usable(llm, system, f"{instr}\n\n---\n{body}", slot=slot, cancel_cb=cancel,
                          min_chars=12, max_chars=max(30_000, int(1.5 * len(body))))

    if len(parts) == 1:
        return ask(instruction, parts[0], "writing the document", cancel_cb)

    def capped(s: str) -> str:
        s = s.strip()
        return s if len(s) <= PARTIAL_CAP_CHARS else s[:PARTIAL_CAP_CHARS].rsplit(" ", 1)[0]

    def part_summary(item: tuple[int, str], cancel: Callable[[], bool] | None) -> str:
        i, part = item
        return capped(ask(f"{instruction}\n\n(This is part {i} of {len(parts)} of a long transcript. "
                          "Cover only this part, and keep the result compact.)", part,
                          f"writing part {i} of {len(parts)}", cancel))

    level = _in_parallel(part_summary, list(enumerate(parts, 1)), llm, cancel_cb)
    merge = (f"{instruction}\n\nThe text below holds partial results for consecutive "
             "parts of one long transcript. Merge them into a single, final result "
             "for the whole transcript. Remove repetition; keep it complete.")
    size = _merge_group_size()
    # each round merges groups of `size` (>= 2) parts, so log_size(parts) rounds are enough
    for _ in range(MAX_MERGE_ROUNDS):
        groups = [level[i:i + size] for i in range(0, len(level), size)]
        merged = _in_parallel(lambda g, cancel: ask(merge, "\n\n".join(g), "merging the parts", cancel),
                              groups, llm, cancel_cb)
        if len(merged) == 1:
            return merged[0]
        level = [capped(m) for m in merged]
    raise LLMError(f"The transcript is too long to merge into one document ({len(parts)} parts).")


def generate_document(transcript: str, task: str, llm: LLMProvider, *,
                      progress: Progress | None = None,
                      cancel_cb: Callable[[], bool] | None = None,
                      language: str | None = None, spoken: str | None = None) -> str:
    """A task-specific document written from the transcript."""
    instruction = _TASKS.get(task)
    if instruction is None:
        raise ValueError(f"Unknown task '{task}'. Choose from: {', '.join(_TASKS)}")
    return _run_doc(instruction, transcript, llm, progress=progress, cancel_cb=cancel_cb,
                    language=language, spoken=spoken)


def generate_custom(transcript: str, custom_prompt: str, llm: LLMProvider, *,
                    progress: Progress | None = None,
                    cancel_cb: Callable[[], bool] | None = None,
                    language: str | None = None, spoken: str | None = None) -> str:
    """A user-supplied instruction run against the transcript."""
    return _run_doc(custom_prompt, transcript, llm, progress=progress, cancel_cb=cancel_cb,
                    language=language, spoken=spoken)


def available_tasks() -> list[str]:
    return list(_TASKS)


def task_default_prompts() -> dict[str, str]:
    """Return the built-in instruction text for each structured task.

    Shown to the user (editable) when they pick a preset; ``custom`` has none.
    """
    prompts = {name: instr for name, instr in _TASKS.items() if name != "clean"}
    # `clean` is driven by the cleanup system prompt; expose a friendly stub
    prompts["clean"] = "Clean up the transcript: fix misheard terms, punctuation, casing."
    return prompts
