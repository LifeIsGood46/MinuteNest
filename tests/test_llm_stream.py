"""D29-D31. A request that is given up lets go of everything it held (llm/stream.py).

The app's real streaming reader and provider against the local stand-in server,
no app window: nothing may be left behind after an answer is cut, so a long
session of retries cannot pile up threads, buffered text and open connections.
"""

import json
import threading
import time
import urllib.request

import pytest

from minutenest.llm.compat import OpenAICompatProvider
from minutenest.llm.stream import Limits, LLMRunaway, Reader, Watch

LET_GO_S = 10


def until(condition, timeout_s: float) -> bool:
    """Wait for ``condition`` (checked every 10 ms), at most ``timeout_s``."""
    tick, deadline = threading.Event(), time.monotonic() + timeout_s
    while not condition() and time.monotonic() < deadline:
        tick.wait(0.01)
    return condition()


def stream_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "llm-stream" and t.is_alive()]


def chat_request(server) -> urllib.request.Request:
    body = {"model": "local-model", "stream": True, "messages": [{"role": "user", "content": "x"}]}
    return urllib.request.Request(f"{server.base_url}/chat/completions", data=json.dumps(body).encode(),
                                  headers={"Content-Type": "application/json"}, method="POST")


@pytest.mark.llm(replies=["runaway"])
def test_a_reader_whose_buffer_is_full_still_stops_when_closed(llm):
    """Found in review: the reader thread waited forever to hand over a line once its
    buffer was full and nobody read any more (a cut runaway on a busy PC)."""
    reader = Reader(chat_request(llm), Limits(30, 30, 60), cancel=None, max_backlog=8)
    lines = reader.lines()
    next(lines)                                   # streaming; the parser now stops reading
    assert until(reader._q.full, LET_GO_S), "the reader never filled its buffer"
    thread = stream_threads()[0]
    reader.close()
    thread.join(timeout=LET_GO_S)
    assert not thread.is_alive()


@pytest.mark.llm(replies=["runaway"])
def test_a_cut_runaway_answer_leaves_no_reader_behind(llm):
    provider = OpenAICompatProvider(model="local-model", base_url=llm.base_url)
    before = set(stream_threads())
    with pytest.raises(LLMRunaway):
        provider.complete("system", "user", watch=Watch())
    mine = [t for t in stream_threads() if t not in before]
    for reader in mine:
        reader.join(timeout=LET_GO_S)
    assert [t for t in mine if t.is_alive()] == []


@pytest.mark.llm(replies=["runaway"])
def test_the_server_sees_the_cut_runaway_request_hang_up(llm):
    provider = OpenAICompatProvider(model="local-model", base_url=llm.base_url)
    with pytest.raises(LLMRunaway):
        provider.complete("system", "user", watch=Watch())
    assert llm.hung_up.wait(LET_GO_S)
