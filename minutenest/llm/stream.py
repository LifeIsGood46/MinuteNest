"""Reading a model's answer while it streams: bounded, cancellable, with progress.

Every provider streams its answer (OpenAI-style SSE, Anthropic's named SSE
events, Ollama's one-JSON-per-line). Reading it piece by piece gives three
things a single blocking request could not:

* progress inside one request (the job card's bar moves, "thinking…" shows);
* a dead request is noticed by silence instead of a fixed total timeout, so a
  slow-but-steady free model is not killed at 240 s while a stalled one is
  given up in two minutes;
* a runaway answer (22 000 characters for a 6 000-character input, measured on
  openrouter/free) is cut as soon as it is clearly one.

All waits are bounded (``Limits``): first piece, silence between pieces, and the
whole request. The socket is read on a helper thread so that Stop and the
deadlines are checked four times a second however the network behaves.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from collections.abc import Callable, Iterator

from ..errors import CancelledError
from ..net import http_request, open_url
from .base import LLMEmptyAnswer, LLMError, LLMRefusal, LLMResponse

POLL_S = 0.25            # how often deadlines and Stop are checked
MAX_BACKLOG = 4096       # lines read ahead of the parser at most
MAX_LINE = 1 << 20       # bytes in one line of the stream at most
DEFAULT_MAX_CHARS = 200_000


class LLMTimeout(LLMError):
    """The model did not start, or stopped, answering in time (worth asking again)."""


class LLMRunaway(LLMError):
    """The answer kept going far past any sensible length (worth asking again)."""


@dataclass(frozen=True)
class Limits:
    first_byte_s: float   # until the first piece of the answer (text or thinking)
    silence_s: float      # the longest gap between pieces after that
    total_s: float        # the whole request, however steadily it streams

    @classmethod
    def for_provider(cls, local: bool) -> Limits:
        """Defaults: a model on this PC may need minutes before its first word (prompt
        processing on a CPU); a cloud model that is silent that long is gone.
        ``MINUTENEST_LLM_FIRST_BYTE_S`` / ``_SILENCE_S`` / ``_TOTAL_S`` override them."""
        first, silence, total = (1800.0, 600.0, 3600.0) if local else (300.0, 120.0, 1200.0)

        def env(name: str, default: float) -> float:
            raw = os.environ.get(name, "").strip()
            return float(raw) if raw else default

        return cls(env("MINUTENEST_LLM_FIRST_BYTE_S", first), env("MINUTENEST_LLM_SILENCE_S", silence),
                   env("MINUTENEST_LLM_TOTAL_S", total))


@dataclass
class Watch:
    """What the caller of ``complete`` wants while the answer streams."""
    cancel: Callable[[], bool] | None = None
    on_progress: Callable[[int, int], None] | None = None   # (text chars, thinking chars)
    max_chars: int = DEFAULT_MAX_CHARS                       # beyond this the answer is a runaway
    attempt: int = 1                                          # 1-based try number of this request
    limits: Limits | None = None                              # None: the provider's defaults


@dataclass
class Answer:
    """The answer as it arrives."""
    provider: str
    model: str
    watch: Watch
    text: list[str] = field(default_factory=list)
    text_chars: int = 0
    thinking_chars: int = 0
    finish: str = ""

    def add(self, text: str = "", thinking: str = "") -> None:
        if text:
            self.text.append(text)
            self.text_chars += len(text)
            if self.text_chars > self.watch.max_chars:
                raise LLMRunaway(f"{self.model} kept writing past {self.watch.max_chars} characters "
                                 "without finishing.")
        self.thinking_chars += len(thinking)
        if (text or thinking) and self.watch.on_progress:
            self.watch.on_progress(self.text_chars, self.thinking_chars)

    def result(self) -> LLMResponse:
        text = "".join(self.text).strip()
        if not text:
            why = (" (it spent its whole budget thinking)" if self.thinking_chars
                   else "")
            raise LLMEmptyAnswer(f"{self.model} answered with no text{why}.")
        return LLMResponse(text=text, model=self.model, provider=self.provider,
                           finish=self.finish, thinking_chars=self.thinking_chars)


class Reader:
    """Lines of a streamed HTTP response, read on a helper thread.

    ``lines()`` yields raw lines and raises ``LLMTimeout`` / ``CancelledError``
    when a limit passes or the user stops the job. The parser calls ``alive()``
    for every real piece of the answer (keep-alive comments do not count).
    HTTP errors are re-raised for the provider to translate.
    """

    def __init__(self, req: urllib.request.Request, limits: Limits, cancel: Callable[[], bool] | None,
                 context=None, max_backlog: int = MAX_BACKLOG):
        self._limits = limits
        self._cancel = cancel
        self._q: queue.Queue = queue.Queue(maxsize=max_backlog)   # bounded memory ahead of the parser
        self._stop = threading.Event()
        self._resp = None
        self._last: float | None = None
        self._t0 = time.monotonic()
        # the reader itself never blocks longer than this on one socket read
        sock_timeout = max(limits.first_byte_s, limits.silence_s) + 5.0
        threading.Thread(target=self._read, args=(req, sock_timeout, context), daemon=True,
                         name="llm-stream").start()

    def _read(self, req, sock_timeout: float, context) -> None:
        try:
            with open_url(req, timeout=sock_timeout, context=context) as resp:
                self._resp = resp
                while not self._stop.is_set():
                    # one line at most MAX_LINE bytes: a server that never sends a
                    # newline can't make this thread hold unbounded memory
                    line = resp.readline(MAX_LINE)
                    if not line:
                        break
                    self._hand_over(("line", line))
            self._hand_over(("end", None))
        except BaseException as e:  # noqa: BLE001 - handed to the consuming thread, re-raised there
            self._hand_over(("error", e))

    def _hand_over(self, item: tuple) -> None:
        """Queue ``item`` for the parser; give up when the request was closed.

        A plain ``put`` waited forever once the buffer was full and the parser had
        stopped reading (a cut runaway): the thread and its lines leaked.
        """
        while not self._stop.is_set():
            try:
                self._q.put(item, timeout=POLL_S)
                return
            except queue.Full:
                continue

    def alive(self) -> None:
        self._last = time.monotonic()

    def _check(self) -> None:
        now = time.monotonic()
        lim = self._limits
        if self._cancel and self._cancel():
            raise CancelledError("cancelled by user")
        if now - self._t0 > lim.total_s:
            raise LLMTimeout(f"The model was still writing after {int(lim.total_s)} seconds; "
                             "the request was given up.")
        if self._last is None and now - self._t0 > lim.first_byte_s:
            raise LLMTimeout(f"The model did not start answering within {int(lim.first_byte_s)} seconds.")
        if self._last is not None and now - self._last > lim.silence_s:
            raise LLMTimeout(f"The model stopped answering for {int(lim.silence_s)} seconds.")

    def lines(self) -> Iterator[bytes]:
        deadline = self._t0 + self._limits.total_s     # the loop's bound (Power of Ten rule 2)
        while time.monotonic() <= deadline:
            self._check()
            try:
                kind, value = self._q.get(timeout=POLL_S)
            except queue.Empty:
                continue
            if kind == "line":
                yield value
            elif kind == "end":
                return
            else:
                raise value
        self._check()      # past the deadline: raises LLMTimeout

    def close(self) -> None:
        self._stop.set()
        resp = self._resp
        if resp is not None:
            with contextlib.suppress(OSError):   # already closed by the server or the reader
                resp.close()   # unblocks the reader thread's socket read


def _json(raw: bytes) -> dict:
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise LLMError(f"Unreadable piece of the answer: {raw[:120]!r}", http_code=502) from e
    if not isinstance(obj, dict):
        raise LLMError(f"Unexpected piece of the answer: {raw[:120]!r}", http_code=502)
    return obj


def _cut_off(answer: Answer) -> LLMError:
    """The stream ended without its end marker: a dropped connection, not a finished answer."""
    return LLMError(f"{answer.model}: the answer was cut off (the connection closed before the model "
                    "finished)", http_code=502)


def _error_code(err: dict) -> int:
    code = err.get("code")
    if isinstance(code, int):
        return code
    return 502   # "server_error", "provider disconnected": the upstream failed, worth retrying


def read_openai_sse(reader: Reader, answer: Answer) -> None:
    """OpenAI chat-completions chunks (OpenAI, OpenRouter, LM Studio, vLLM, Ollama's /v1)."""
    for raw in reader.lines():
        line = raw.strip()
        if not line or line.startswith(b":") or not line.startswith(b"data:"):
            continue                       # blank separators, keep-alive comments, other fields
        data = line[5:].strip()
        if data == b"[DONE]":
            return
        obj = _json(data)
        if obj.get("model"):
            answer.model = str(obj["model"])
        if isinstance(obj.get("error"), dict):
            err = obj["error"]
            raise LLMError(f"{answer.model}: {err.get('message') or 'the provider failed mid-answer'}",
                           http_code=_error_code(err))
        for choice in obj.get("choices") or []:
            delta = choice.get("delta") or {}
            thinking = delta.get("reasoning") or delta.get("reasoning_content") or ""
            text = delta.get("content") or ""
            if text or thinking:
                reader.alive()
                answer.add(text=text, thinking=thinking if isinstance(thinking, str) else "")
            if choice.get("finish_reason"):
                answer.finish = str(choice["finish_reason"])
    if not answer.finish:      # EOF without [DONE] and without a finish reason
        raise _cut_off(answer)


_ANTHROPIC_ERRORS = {"overloaded_error": 529, "rate_limit_error": 429, "api_error": 500,
                     "timeout_error": 504}


def read_anthropic_sse(reader: Reader, answer: Answer) -> None:
    """Anthropic Messages API events: content_block_delta (text/thinking), message_delta, error."""
    for raw in reader.lines():
        line = raw.strip()
        if not line.startswith(b"data:"):
            continue                       # "event:" names repeat the data's "type"
        obj = _json(line[5:].strip())
        kind = obj.get("type")
        if kind == "content_block_delta":
            delta = obj.get("delta") or {}
            text = delta.get("text") if delta.get("type") == "text_delta" else ""
            thinking = delta.get("thinking") if delta.get("type") == "thinking_delta" else ""
            reader.alive()
            answer.add(text=text or "", thinking=thinking or "")
        elif kind == "message_delta":
            answer.finish = str((obj.get("delta") or {}).get("stop_reason") or answer.finish)
            if answer.finish == "refusal":
                why = str(((obj.get("delta") or {}).get("stop_details") or {}).get("explanation") or "").strip()
                raise LLMRefusal(f"Claude declined this request{': ' + why if why else '.'} "
                                 "Pick another model in Settings. The transcript itself was saved.")
        elif kind == "message_stop":
            return
        elif kind == "error":
            err = obj.get("error") or {}
            raise LLMError(f"Anthropic: {err.get('message') or err.get('type') or 'error'}",
                           http_code=_ANTHROPIC_ERRORS.get(str(err.get("type")), 502))
    raise _cut_off(answer)     # EOF without message_stop


def read_ollama_ndjson(reader: Reader, answer: Answer) -> None:
    """Ollama's native /api/chat stream: one JSON object per line, the last with "done": true."""
    for raw in reader.lines():
        line = raw.strip()
        if not line:
            continue
        obj = _json(line)
        if obj.get("error"):
            raise LLMError(f"Ollama: {obj['error']}", http_code=502)
        msg = obj.get("message") or {}
        text, thinking = msg.get("content") or "", msg.get("thinking") or ""
        if text or thinking:
            reader.alive()
            answer.add(text=text, thinking=thinking)
        if obj.get("done"):
            answer.finish = str(obj.get("done_reason") or "stop")
            return
    raise _cut_off(answer)     # EOF without the "done": true line


def stream_answer(req: urllib.request.Request, parse: Callable[[Reader, Answer], None], *,
                  provider: str, model: str, watch: Watch, limits: Limits, context=None) -> LLMResponse:
    """Send ``req`` and read the streamed answer with ``parse``; HTTP errors propagate."""
    reader = Reader(req, limits, watch.cancel, context)
    answer = Answer(provider=provider, model=model, watch=watch)
    try:
        parse(reader, answer)
    finally:
        reader.close()
    return answer.result()


def post_streamed(url: str, headers: dict, payload: dict,  # noqa: PLR0913 - keyword-only, named by every caller
                  parse: Callable[[Reader, Answer], None], *,
                  provider, model: str, watch: Watch | None,
                  http_error: Callable[[urllib.error.HTTPError], LLMError],
                  conn_error: Callable[[Exception], LLMError]) -> LLMResponse:
    """POST a streaming chat request for ``provider`` and read the answer.

    ``http_error`` / ``conn_error`` turn transport failures into the provider's own
    messages (a bad key, a busy model, a server that is not running).
    """
    from ..net import ssl_context

    watch = watch or Watch()
    req = http_request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        return stream_answer(req, parse, provider=provider.provider_name, model=model, watch=watch,
                             limits=watch.limits or provider.limits(), context=ssl_context())
    except urllib.error.HTTPError as e:
        raise http_error(e) from e
    except (LLMError, CancelledError):
        raise
    except Exception as e:
        raise conn_error(e) from e
