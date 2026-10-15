"""Fixtures: a real app process per test, a real Edge page, the shared model cache.

pytest                      source build (python -m minutenest.cli serve)
pytest --app exe            the packaged dist/MinuteNest/MinuteNest.exe
pytest --headed             watch the browser (pytest-playwright; also --slowmo N)
Failure screenshots and traces land in tests/artifacts (pyproject addopts).
See TEST_PLAN.md for what is covered and why.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from support.app import EXE, ROOT, SHARED_MODELS, App, app_env, cli_command
from support.llm_server import AnthropicServer, LLMServer, OllamaServer, OpenAIServer, OpenRouterServer

ARTIFACTS = Path(__file__).parent / "artifacts"
# Connection-level noise while the app restarts or stops (the page reconnects).
_RECONNECT_NOISE = re.compile(r"Failed to load resource: net::ERR_(CONNECTION_REFUSED|CONNECTION_RESET|EMPTY_RESPONSE)")


def pytest_addoption(parser):
    parser.addoption("--app", choices=("source", "exe"), default="source",
                     help="run the dev checkout or the packaged dist/MinuteNest/MinuteNest.exe")


def pytest_configure(config):
    for line in (
        "app(models='shared'|'empty', env={...}): how the app under test starts",
        "network: needs the internet (model downloads)",
        "openrouter: needs OPENROUTER_KEY (real free OpenRouter models)",
        "ollama_cloud: needs OLLAMA_KEY (real Ollama Cloud, free-plan models)",
        "exe_only: needs the packaged app (--app exe)",
        "console_errors(pattern): console errors matching the pattern are expected",
        "llm(models=[...], replies=[...]): how the local LLM server answers",
    ):
        config.addinivalue_line("markers", line)


def pytest_collection_modifyitems(config, items):
    kind = config.getoption("--app")
    for item in items:
        if kind == "exe" and not EXE.is_file():
            item.add_marker(pytest.mark.skip(reason=f"{EXE} not built (run build.bat)"))
        if item.get_closest_marker("exe_only") and kind != "exe":
            item.add_marker(pytest.mark.skip(reason="packaged-app test: run with --app exe"))
        if item.get_closest_marker("openrouter") and not os.environ.get("OPENROUTER_KEY"):
            item.add_marker(pytest.mark.skip(reason="set OPENROUTER_KEY to run real OpenRouter tests"))
        if item.get_closest_marker("ollama_cloud") and not os.environ.get("OLLAMA_KEY"):
            item.add_marker(pytest.mark.skip(reason="set OLLAMA_KEY to run real Ollama Cloud tests"))


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    report = yield
    setattr(item, f"rep_{report.when}", report)
    return report


@pytest.fixture(scope="session")
def shared_models() -> Path:
    """The user's Hugging Face cache with the tiny model in it (downloaded once if missing)."""
    if not (SHARED_MODELS / "models--Systran--faster-whisper-tiny" / "snapshots").is_dir():
        subprocess.run([sys.executable, "-m", "minutenest.cli", "models", "download", "tiny"],
                       cwd=str(ROOT), env=dict(os.environ, HF_HUB_CACHE=str(SHARED_MODELS)), check=True)
    return SHARED_MODELS


@pytest.fixture()
def app(request, tmp_path, shared_models) -> Iterator[App]:
    """A freshly installed app (empty data folder) running for this test only."""
    marker = request.node.get_closest_marker("app")
    opts = marker.kwargs if marker else {}
    models = shared_models
    if opts.get("models") == "empty":
        models = tmp_path / "models"
        models.mkdir()
    instance = App(kind=request.config.getoption("--app"), root=tmp_path / "install",
                   models=models, extra_env=opts.get("env", {}))
    instance.start()
    yield instance
    instance.stop()


@pytest.fixture()
def cli(request, tmp_path, shared_models):
    """minutenest-cli against an isolated install: ``cli.run(*args)`` -> CompletedProcess."""
    root = tmp_path / "cli-install"
    env = app_env(root, shared_models)
    base = cli_command(request.config.getoption("--app"))

    def run(*args, timeout: int = 300, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        p = subprocess.run([*base, *map(str, args)], check=False, cwd=str(tmp_path), capture_output=True,
                           timeout=timeout, env={**env, **(extra_env or {})})
        for name in ("stdout", "stderr"):          # AGENTS.md: output is UTF-8
            raw = getattr(p, name)
            try:
                setattr(p, name, raw.decode("utf-8"))
            except UnicodeDecodeError as e:
                raise AssertionError(f"minutenest-cli {name} is not UTF-8 ({e}): {raw[:300]!r}") from None
        return p

    return SimpleNamespace(run=run, root=root)


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    """Scrollbars take room, as in the app's own window (WebView2); headless
    Chromium hides them by default, which hid a layout jump."""
    return {**browser_type_launch_args, "ignore_default_args": ["--hide-scrollbars"]}


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {**browser_context_args, "viewport": {"width": 1366, "height": 768}}


@pytest.fixture()
def page(request, context, app):
    """A browser tab for the app under test. Fails the test on any uncaught
    JavaScript error or console error (except reconnect noise while the app
    restarts, and errors the test declares with @pytest.mark.console_errors)."""
    pg = context.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(f"uncaught: {e}"))
    pg.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)
    yield pg
    if getattr(request.node, "rep_call", None) is not None and request.node.rep_call.failed:
        log = app.data_dir / "logs" / "minutenest.log"
        if log.is_file():
            ARTIFACTS.mkdir(exist_ok=True)
            shutil.copyfile(log, ARTIFACTS / f"{request.node.name}.log")
    allowed = request.node.get_closest_marker("console_errors")
    pattern = re.compile(allowed.args[0]) if allowed else None
    real = [e for e in errors if not _RECONNECT_NOISE.search(e) and not (pattern and pattern.search(e))]
    assert not real, "JavaScript errors in the page:\n" + "\n".join(real)


def _server_from_marker(request, cls):
    marker = request.node.get_closest_marker("llm")
    return cls(**(marker.kwargs if marker else {}))


@pytest.fixture()
def openai_api(request):
    """Stand-in for api.openai.com (support/llm_server.py); @pytest.mark.llm shapes it."""
    server = _server_from_marker(request, OpenAIServer)
    yield server
    server.stop()


@pytest.fixture()
def anthropic_api(request):
    """Stand-in for api.anthropic.com (support/llm_server.py); @pytest.mark.llm shapes it."""
    server = _server_from_marker(request, AnthropicServer)
    yield server
    server.stop()


@pytest.fixture()
def openrouter_api(request):
    """Stand-in for openrouter.ai/api/v1 (support/llm_server.py); @pytest.mark.llm shapes it."""
    server = _server_from_marker(request, OpenRouterServer)
    yield server
    server.stop()


@pytest.fixture()
def ollama_api(request):
    """Stand-in for a local `ollama serve` (support/llm_server.py); @pytest.mark.llm shapes it."""
    server = _server_from_marker(request, OllamaServer)
    yield server
    server.stop()


@pytest.fixture()
def llm(request):
    """The local LM Studio-style server; @pytest.mark.llm(models=[...], replies=[...]) shapes it."""
    marker = request.node.get_closest_marker("llm")
    server = LLMServer(**(marker.kwargs if marker else {}))
    yield server
    server.stop()
