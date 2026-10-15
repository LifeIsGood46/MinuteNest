"""Local LLM servers on real sockets, speaking the providers' own HTTP APIs.

LLMServer        the OpenAI-compatible dialect of LM Studio / vLLM / llama.cpp
OpenAIServer     api.openai.com: Bearer key, chat models only accept
                 max_completion_tokens on reasoning models (GPT-5/6, o-series)
AnthropicServer  api.anthropic.com: x-api-key + anthropic-version, paginated
                 /v1/models, Messages API answers with a thinking block first
OpenRouterServer openrouter.ai/api/v1: public /models, /auth/key, the free
                 router (openrouter/free answers as the model it picked),
                 ": OPENROUTER PROCESSING" keep-alive comments, `reasoning` deltas
OllamaServer     the local Ollama daemon: /api/tags, /api/chat (NDJSON stream)

The OpenAI and Anthropic servers enforce the rules those APIs document
(https://developers.openai.com/api/reference, https://platform.claude.com/docs),
with the same error bodies, so a request the real service would reject fails
here too. The app reaches them through OPENAI_BASE_URL (with /v1, as the OpenAI
SDK uses it) / ANTHROPIC_BASE_URL (the host alone, as the Anthropic SDK uses it) /
OPENROUTER_BASE_URL (with /api/v1, like OpenRouter's own examples).

Every chat endpoint answers ``"stream": true`` in its provider's wire format
(OpenAI/OpenRouter/LM Studio: SSE ``data:`` chunks ending in ``[DONE]``;
Anthropic: named SSE events; Ollama: one JSON object per line) and a plain JSON
body otherwise.

``replies`` decides the answer to each generation request in order (the last
entry repeats):

  "ok"         a short Markdown document quoting the start of the transcript
  "slow"       a longer document, streamed in pieces over about 12 seconds
  "think"      only reasoning, then the token limit: no text at all
  "classifier" a safety classifier's verdict ("User Safety: safe")
  "runaway"    text that never ends (until the client hangs up)
  "stall"      a few words, then nothing more (the connection stays open)
  "silent"     keep-alive comments only, never a word (OpenAI-style dialects)
  "midstream"  a few words, then an upstream error inside the 200 stream
  "cut"        a few words, then the stream just ends (no end marker: a dropped connection)
  "errorstop"  a few words, then finish_reason "error" with no error object
  "refuse"     a refusal (Anthropic: stop_reason "refusal" with no text)
  "429"        HTTP 429 rate limited, Retry-After: 0
  "529"        HTTP 529 overloaded (Anthropic)
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from collections.abc import Iterator, Sequence
from urllib.parse import parse_qs, urlparse

REFUSAL = "I'm sorry, but I can't help with that request."
CLASSIFIER = "User Safety: safe"
REASONING_PREFIXES = ("gpt-5", "gpt-6", "o1", "o3", "o4")
HANG_LIMIT_S = 600          # a stalled / silent answer gives up after this, if nobody hung up first
RUNAWAY_LIMIT = 2_000_000   # characters of runaway text before the server stops by itself

OPENAI_MODELS = [
    "gpt-6-sol", "gpt-6-luna", "gpt-5.5", "gpt-5.4-mini", "gpt-4o-mini", "gpt-3.5-turbo",
    "gpt-4o-transcribe", "gpt-4o-mini-tts", "gpt-image-1", "gpt-realtime", "gpt-audio",
    "text-embedding-3-small", "whisper-1", "dall-e-3", "omni-moderation-latest",
]
# newest first, as the Models API lists them; more than one page of 20
ANTHROPIC_MODELS = [
    "claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5",
    "claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-sonnet-4-6", "claude-haiku-4-5", "claude-opus-4-5-20251101", "claude-sonnet-4-5-20250929",
    "claude-haiku-4-5-20251001", "claude-opus-4-1-20250805", "claude-opus-4-20250514",
    "claude-sonnet-4-20250514", "claude-3-7-sonnet-20250219", "claude-3-5-haiku-20241022",
    "claude-3-haiku-20240307", "claude-3-opus-20240229",
]
OPENROUTER_MODELS = [
    "openrouter/free", "google/gemma-4-31b-it:free", "nvidia/nemotron-3-ultra-550b-a55b:free",
    "google/gemma-4-26b-a4b-it:free", "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3.5-content-safety:free", "anthropic/claude-opus-5.5", "openai/gpt-5.5",
]
ROUTER_PICK = "nvidia/nemotron-3-ultra-550b-a55b:free"   # what openrouter/free "chose"


def _document(user_text: str) -> str:
    return "# Meeting notes\n\n- " + user_text.rsplit("---", maxsplit=1)[-1].strip()[:300]


def _long_document(user_text: str) -> str:
    return _document(user_text) + "".join(f"\n- point {i}: the team agreed on step {i}." for i in range(1, 31))


def _pieces(text: str, size: int = 40) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


# An answer is a list of events: ("text"|"think"|"comment", str), ("wait", seconds),
# ("hang", None) = say nothing more, ("error", (code, message)), ("end", finish_reason).
Event = tuple[str, Any]


def script(reply: str, user: str) -> Sequence[Event]:
    if reply == "slow":
        return [e for p in _pieces(_long_document(user), 50) for e in (("wait", 0.4), ("text", p))] + [("end", "stop")]
    if reply == "slowthink":   # a reasoning model: a long think (16 s), then the document
        thinking = [e for _ in range(40) for e in (("wait", 0.4), ("think", "Let me weigh what was said. "))]
        return [*thinking, *[("text", p) for p in _pieces(_document(user))], ("end", "stop")]
    if reply == "think":
        return [("think", "Let me think about this transcript. ")] * 30 + [("end", "length")]
    if reply == "classifier":
        return [("text", CLASSIFIER), ("end", "stop")]
    if reply == "refuse":
        return [("text", REFUSAL), ("end", "stop")]
    if reply == "stall":
        return [("text", p) for p in _pieces(_document(user))[:3]] + [("hang", None)]
    if reply == "silent":
        return [("silent", None)]
    if reply == "midstream":
        return [*[("text", p) for p in _pieces(_document(user))[:2]],
                ("error", (502, "Provider disconnected unexpectedly"))]
    if reply == "runaway":
        return [("runaway", None)]
    if reply == "cut":
        return [("text", p) for p in _pieces(_document(user))[:2]] + [("drop", None)]
    if reply == "errorstop":
        return [("text", p) for p in _pieces(_document(user))[:2]] + [("end", "error")]
    return [("text", p) for p in _pieces(_document(user))] + [("end", "stop")]


def expand(events: Sequence[Event], gone: threading.Event) -> Iterator[Event]:
    """Turn the special events into the plain ones, honouring waits and hang-ups."""
    for kind, value in events:
        if gone.is_set():
            return
        if kind == "wait":
            time.sleep(float(value))
        elif kind == "hang":
            gone.wait(HANG_LIMIT_S)
            return
        elif kind == "silent":
            deadline = time.monotonic() + HANG_LIMIT_S
            while not gone.is_set() and time.monotonic() < deadline:
                yield ("comment", "OPENROUTER PROCESSING")
                gone.wait(1.0)
            return
        elif kind == "runaway":
            sent = 0
            while not gone.is_set() and sent < RUNAWAY_LIMIT:
                yield ("text", "снова и снова, ")
                sent += 15
            return
        else:
            yield kind, value


def full_answer(events: Sequence[Event]) -> tuple[str, str, str]:
    """(text, thinking, finish) of an answer that is not streamed (no waits, no hangs)."""
    text = "".join(str(v) for k, v in events if k == "text")
    think = "".join(str(v) for k, v in events if k == "think")
    finish = next((str(v) for k, v in events if k == "end"), "stop")
    return text, think, finish


class Stream:
    """A streamed HTTP body: ``chunks`` yields bytes; ``gone`` is set when the client hangs up."""

    def __init__(self, content_type: str, make_chunks):
        self.content_type = content_type
        self.gone = threading.Event()
        self.chunks = make_chunks(self.gone)


class _Server:
    """A threaded HTTP server; subclasses implement ``handle(method, path, query, headers, body)``.

    ``handle`` returns (status, payload, headers) where payload is a dict (sent as JSON)
    or a ``Stream`` (sent with chunked transfer encoding, as the real services do).
    """

    def __init__(self, models, replies=("ok",)):
        self.models = list(models)
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.hung_up = threading.Event()   # a client went away in the middle of a streamed answer
        self.authorizations: list[str] = []   # the Authorization header of every request ("" when none)
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):
                return

            def _serve(self, method: str):
                outer.authorizations.append(self.headers.get("Authorization") or "")
                url = urlparse(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                code, payload, headers = outer.handle(method, url.path.rstrip("/"), parse_qs(url.query),
                                                      self.headers, body)
                if isinstance(payload, Stream):
                    self._stream(code, payload, headers)
                    return
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def _stream(self, code: int, stream: Stream, headers):
                self.send_response(code)
                self.send_header("Content-Type", stream.content_type)
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Cache-Control", "no-cache")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    for chunk in stream.chunks:
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except OSError:          # the client hung up: that is what "stall"/"runaway" wait for
                    stream.gone.set()
                    outer.hung_up.set()
                    self.close_connection = True

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def handle(self, method, path, query, headers, body) -> tuple[int, dict | Stream, dict | None]:
        raise NotImplementedError

    def next_reply(self, record: dict) -> str:
        with self._lock:
            self.requests.append(record)
            return self.replies[min(len(self.requests), len(self.replies)) - 1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


# ---- the OpenAI chat-completions dialect (LM Studio, OpenAI, OpenRouter, Ollama Cloud) ----

def openai_stream(events: Sequence[Event], model: str, reasoning_field: str = "reasoning_content") -> Stream:
    def chunks(gone: threading.Event) -> Iterator[bytes]:
        def data(obj: dict) -> bytes:
            return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()

        def chunk(delta: dict, finish=None) -> dict:
            return {"id": "chatcmpl-test", "object": "chat.completion.chunk", "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        yield data(chunk({"role": "assistant", "content": ""}))
        for kind, value in expand(events, gone):
            if kind == "drop":
                return                           # no end marker: the answer just stops
            if kind == "comment":
                yield f": {value}\n\n".encode()
            elif kind == "text":
                yield data(chunk({"content": value}))
            elif kind == "think":
                yield data(chunk({reasoning_field: value}))
            elif kind == "error":
                code, message = value
                err = chunk({"content": ""}, "error")
                err["error"] = {"code": code, "message": message}
                yield data(err)
                return
            elif kind == "end":
                yield data(chunk({}, value))
        if not gone.is_set():
            yield b"data: [DONE]\n\n"

    return Stream("text/event-stream", chunks)


def openai_reply(body: dict, reply: str, user: str, model: str, reasoning_field: str = "reasoning_content"):
    """(status, payload, headers) of a chat completion in the OpenAI dialect."""
    events = script(reply, user)
    if body.get("stream"):
        return 200, openai_stream(events, model, reasoning_field), None
    text, think, finish = full_answer(events)
    msg = {"role": "assistant", "content": text or None}
    if think:
        msg[reasoning_field] = think
    return 200, {"object": "chat.completion", "model": model,
                 "choices": [{"index": 0, "finish_reason": finish, "message": msg}]}, None


class LLMServer(_Server):
    """LM Studio and friends: no key, GET /v1/models, POST /v1/chat/completions."""

    def __init__(self, models=("local-model",), replies=("ok",)):
        super().__init__(models, replies)

    def handle(self, method, path, query, headers, body):
        if method == "GET" and path.endswith("/models"):
            return 200, {"data": [{"id": m} for m in self.models]}, None
        if method != "POST" or not path.endswith("/chat/completions"):
            return 404, {"error": {"message": "not found"}}, None
        msgs = {m.get("role"): m.get("content", "") for m in body.get("messages", [])}
        reply = self.next_reply({"model": body.get("model"), "system": msgs.get("system", ""),
                                 "user": msgs.get("user", ""), "body": body})
        if reply == "429":
            return 429, {"error": {"message": "Rate limit exceeded"}}, {"Retry-After": "0"}
        return openai_reply(body, reply, msgs.get("user", ""), body.get("model") or self.models[0])


class OpenAIServer(_Server):
    def __init__(self, key="sk-test-openai", models=OPENAI_MODELS, replies=("ok",)):
        super().__init__(models, replies)
        self.key = key

    def handle(self, method, path, query, headers, body):
        if headers.get("Authorization") != f"Bearer {self.key}":
            return 401, {"error": {"message": "Incorrect API key provided.", "type": "invalid_request_error",
                                   "code": "invalid_api_key"}}, None
        if method == "GET" and path == "/v1/models":
            return 200, {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "openai"}
                                                    for m in self.models]}, None
        if method != "POST" or path != "/v1/chat/completions":
            return 404, {"error": {"message": "Unknown request URL."}}, None
        model = body.get("model", "")
        if model not in self.models:
            return 404, {"error": {"message": f"The model `{model}` does not exist or you do not have access to it.",
                                   "code": "model_not_found"}}, None
        if "max_tokens" in body and model.startswith(REASONING_PREFIXES):
            return 400, {"error": {"message": "Unsupported parameter: 'max_tokens' is not supported with this "
                                              "model. Use 'max_completion_tokens' instead.",
                                   "type": "invalid_request_error", "param": "max_tokens",
                                   "code": "unsupported_parameter"}}, None
        if "stream_options" in body and not body.get("stream"):
            return 400, {"error": {"message": "The 'stream_options' parameter is only allowed when 'stream' "
                                              "is enabled.", "type": "invalid_request_error",
                                   "param": "stream_options", "code": None}}, None
        msgs = {m.get("role"): m.get("content", "") for m in body.get("messages", [])}
        reply = self.next_reply({"model": model, "system": msgs.get("system", ""), "user": msgs.get("user", ""),
                                 "body": body})
        if reply == "429":
            return 429, {"error": {"message": "Rate limit reached", "type": "requests"}}, {"Retry-After": "0"}
        return openai_reply(body, reply, msgs.get("user", ""), model)


class OpenRouterServer(_Server):
    """openrouter.ai/api/v1 as the app uses it, including the free router."""

    def __init__(self, key="sk-or-test", models=OPENROUTER_MODELS, replies=("ok",)):
        super().__init__(models, replies)
        self.key = key

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/v1"

    def handle(self, method, path, query, headers, body):
        if method == "GET" and path == "/api/v1/models":     # public, no key needed
            def price(m):
                return "0" if m.endswith(":free") or m == "openrouter/free" else "0.000002"
            return 200, {"data": [{"id": m, "pricing": {"prompt": price(m), "completion": price(m)}}
                                  for m in self.models]}, None
        if headers.get("Authorization") != f"Bearer {self.key}":
            return 401, {"error": {"message": "No cookie auth credentials found", "code": 401}}, None
        if method == "GET" and path == "/api/v1/auth/key":
            return 200, {"data": {"label": "test", "usage": 0, "limit": 0.1}}, None
        if method != "POST" or path != "/api/v1/chat/completions":
            return 404, {"error": {"message": "Not Found", "code": 404}}, None
        wanted = [body["model"]] if body.get("model") else list(body.get("models") or [])
        if not wanted or any(m not in self.models for m in wanted):
            return 400, {"error": {"message": f"{wanted} is not a valid model ID", "code": 400}}, None
        msgs = {m.get("role"): m.get("content", "") for m in body.get("messages", [])}
        reply = self.next_reply({"model": wanted[0], "models": wanted, "system": msgs.get("system", ""),
                                 "user": msgs.get("user", ""), "body": body})
        if reply == "429":   # Retry-After keeps the test short; the count of tries is what matters
            return 429, {"error": {"message": "Provider returned error", "code": 429,
                                   "metadata": {"raw": f"{wanted[0]} is temporarily rate-limited upstream."}}}, \
                {"Retry-After": "1"}
        answered = ROUTER_PICK if wanted[0] == "openrouter/free" else wanted[0]
        if reply == "classifier" and wanted[0] == "openrouter/free":
            answered = "nvidia/nemotron-3.5-content-safety:free"
        return openai_reply(body, reply, msgs.get("user", ""), answered, reasoning_field="reasoning")


# ---- Anthropic Messages API ----------------------------------------------------------------

def anthropic_stream(events: Sequence[Event], model: str) -> Stream:
    def chunks(gone: threading.Event) -> Iterator[bytes]:
        def ev(name: str, obj: dict) -> bytes:
            return f"event: {name}\ndata: {json.dumps({'type': name, **obj}, ensure_ascii=False)}\n\n".encode()

        yield ev("message_start", {"message": {"id": "msg_test", "type": "message", "role": "assistant",
                                               "model": model, "content": [], "stop_reason": None,
                                               "usage": {"input_tokens": 100, "output_tokens": 1}}})
        block, index = None, -1
        for kind, value in expand(events, gone):
            if kind == "drop":
                return                           # no end marker: the answer just stops
            if kind in ("text", "think"):
                want = "text" if kind == "text" else "thinking"
                if block != want:
                    if block is not None:
                        yield ev("content_block_stop", {"index": index})
                    index += 1
                    block = want
                    yield ev("content_block_start", {"index": index, "content_block":
                             {"type": "text", "text": ""} if want == "text"
                             else {"type": "thinking", "thinking": "", "signature": ""}})
                delta = {"type": "text_delta", "text": value} if want == "text" \
                    else {"type": "thinking_delta", "thinking": value}
                yield ev("content_block_delta", {"index": index, "delta": delta})
            elif kind == "comment":
                yield ev("ping", {})
            elif kind == "error":
                yield ev("error", {"error": {"type": "overloaded_error", "message": "Overloaded"}})
                return
            elif kind == "end":
                if block is not None:
                    yield ev("content_block_stop", {"index": index})
                stop = "max_tokens" if value == "length" else str(value)
                stop = "end_turn" if stop == "stop" else stop
                yield ev("message_delta", {"delta": {"stop_reason": stop, "stop_sequence": None},
                                           "usage": {"output_tokens": 50}})
                yield ev("message_stop", {})

    return Stream("text/event-stream", chunks)


class AnthropicServer(_Server):
    def __init__(self, key="sk-ant-test", models=ANTHROPIC_MODELS, replies=("ok",)):
        super().__init__(models, replies)
        self.key = key

    @property
    def base_url(self) -> str:
        """As ANTHROPIC_BASE_URL is meant: the host, without /v1 (the SDK convention)."""
        return f"http://127.0.0.1:{self.port}"

    @staticmethod
    def _error(code: int, kind: str, message: str, headers=None):
        return code, {"type": "error", "error": {"type": kind, "message": message}}, headers

    def handle(self, method, path, query, headers, body):
        if not headers.get("anthropic-version"):
            return self._error(400, "invalid_request_error", "anthropic-version: header is required")
        if headers.get("x-api-key") != self.key:
            return self._error(401, "authentication_error", "invalid x-api-key")
        if method == "GET" and path == "/v1/models":
            limit = int((query.get("limit") or ["20"])[0])
            start = self.models.index(query["after_id"][0]) + 1 if "after_id" in query else 0
            page = self.models[start:start + limit]
            return 200, {"data": [{"type": "model", "id": m, "display_name": m} for m in page],
                         "has_more": start + limit < len(self.models),
                         "first_id": page[0] if page else None, "last_id": page[-1] if page else None}, None
        if method != "POST" or path != "/v1/messages":
            return self._error(404, "not_found_error", "Not found")
        model = body.get("model", "")
        if model not in self.models:
            return self._error(404, "not_found_error", f"model: {model}")
        if not isinstance(body.get("max_tokens"), int):
            return self._error(400, "invalid_request_error", "max_tokens: Field required")
        user = "".join(m.get("content", "") for m in body.get("messages", []) if m.get("role") == "user")
        reply = self.next_reply({"model": model, "system": body.get("system", ""), "user": user, "body": body})
        if reply == "429":
            return self._error(429, "rate_limit_error", "Number of requests has exceeded your rate limit",
                               {"retry-after": "0"})
        if reply == "529":
            return self._error(529, "overloaded_error", "Overloaded")
        if reply == "refuse":
            if body.get("stream"):
                return 200, anthropic_stream([("end", "refusal")], model), None
            return 200, {"id": "msg_test", "type": "message", "role": "assistant", "model": model,
                         "content": [], "stop_reason": "refusal",
                         "stop_details": {"type": "refusal", "category": None,
                                          "explanation": "This request was declined."},
                         "usage": {"input_tokens": 100, "output_tokens": 0}}, None
        # current Claude models think first: a (summarised, here empty) thinking block, then the text
        events = [("think", ""), *script(reply, user)]
        if body.get("stream"):
            return 200, anthropic_stream(events, model), None
        text, _think, finish = full_answer(events)
        stop = {"stop": "end_turn", "length": "max_tokens"}.get(finish, finish)
        content = [{"type": "thinking", "thinking": "", "signature": "sig"}]
        if text:
            content.append({"type": "text", "text": text})
        return 200, {"id": "msg_test", "type": "message", "role": "assistant", "model": model,
                     "content": content, "stop_reason": stop, "stop_details": None,
                     "usage": {"input_tokens": 100, "output_tokens": 50}}, None


# ---- the local Ollama daemon ---------------------------------------------------------------

def ollama_stream(events: Sequence[Event], model: str) -> Stream:
    def chunks(gone: threading.Event) -> Iterator[bytes]:
        def line(obj: dict) -> bytes:
            return (json.dumps({"model": model, "created_at": "2026-10-13T00:00:00Z", **obj},
                               ensure_ascii=False) + "\n").encode()

        for kind, value in expand(events, gone):
            if kind == "drop":
                return                           # no end marker: the answer just stops
            if kind == "text":
                yield line({"message": {"role": "assistant", "content": value}, "done": False})
            elif kind == "think":
                yield line({"message": {"role": "assistant", "content": "", "thinking": value}, "done": False})
            elif kind == "error":
                yield line({"error": str(value[1])})
                return
            elif kind == "end":
                yield line({"message": {"role": "assistant", "content": ""}, "done": True,
                            "done_reason": value})

    return Stream("application/x-ndjson", chunks)


class OllamaServer(_Server):
    """`ollama serve`: GET /api/tags, POST /api/chat (streams unless "stream": false)."""

    def __init__(self, models=("gemma4:12b",), replies=("ok",)):
        super().__init__(models, replies)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def handle(self, method, path, query, headers, body):
        if method == "GET" and path == "/api/tags":
            return 200, {"models": [{"name": m, "model": m} for m in self.models]}, None
        if method != "POST" or path != "/api/chat":
            return 404, {"error": "404 page not found"}, None
        model = body.get("model", "")
        if model not in self.models:
            return 404, {"error": f"model '{model}' not found"}, None
        msgs = {m.get("role"): m.get("content", "") for m in body.get("messages", [])}
        reply = self.next_reply({"model": model, "system": msgs.get("system", ""), "user": msgs.get("user", ""),
                                 "body": body})
        if reply == "429":
            return 429, {"error": "too many requests"}, None
        events = script(reply, msgs.get("user", ""))
        if body.get("stream", True):          # Ollama streams unless told not to
            return 200, ollama_stream(events, model), None
        text, think, finish = full_answer(events)
        return 200, {"model": model, "message": {"role": "assistant", "content": text, "thinking": think},
                     "done": True, "done_reason": finish}, None
