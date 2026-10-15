"""D. Cloud LLM providers with the user's own key (TEST_PLAN.md).

OpenAI and Anthropic: the app is pointed at local servers that speak their
APIs and enforce their documented rules (support/llm_server.py), plus one
check each against the REAL service with a wrong key, which proves the app
reaches it with correctly formed requests (no key needed, nothing billed).
Ollama Cloud and OpenRouter run for real with free models when OLLAMA_KEY /
OPENROUTER_KEY are set; the keys go to the app as environment variables only.
"""

import os
import re

import pytest
from playwright.sync_api import expect

from support import ui
from support.media import RU_SPEECH, copy_as

NON_CHAT_OPENAI = ["gpt-4o-transcribe", "gpt-4o-mini-tts", "gpt-image-1", "gpt-realtime", "gpt-audio",
                   "text-embedding-3-small", "whisper-1", "dall-e-3", "omni-moderation-latest"]


def _cyrillic_share(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    return sum("а" <= c.lower() <= "я" or c in "ёЁ" for c in letters) / max(1, len(letters))


# ---- OpenAI ---------------------------------------------------------------

def test_openai_lists_only_models_that_can_write_a_document(app, page, openai_api):
    app.restart(OPENAI_BASE_URL=openai_api.base_url)
    ui.open_ready(page, app)
    ui.use_cloud_llm(page, "openai", openai_api.key)
    expect(page.locator("#llm-model option[value='gpt-6-sol']")).to_have_count(1)
    for model in NON_CHAT_OPENAI:
        expect(page.locator(f"#llm-model option[value='{model}']")).to_have_count(0)


def test_openai_preselects_a_current_cost_efficient_model(app, page, openai_api):
    app.restart(OPENAI_BASE_URL=openai_api.base_url)
    ui.open_ready(page, app)
    models = ui.use_cloud_llm(page, "openai", openai_api.key)
    expect(models).to_have_value("gpt-5.4-mini")


def test_a_notes_job_with_a_current_openai_model_writes_the_document(app, page, openai_api, tmp_path):
    """GPT-5/6 and o-series models reject max_tokens (OpenAI API reference)."""
    app.restart(OPENAI_BASE_URL=openai_api.base_url)
    ui.open_ready(page, app)
    models = ui.use_cloud_llm(page, "openai", openai_api.key)
    ui.choose(page, models, "gpt-6-sol", "gpt-6-sol")
    ui.expect_saved(page)
    ui.close_settings(page)
    ui.run(ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "sync.mp3"), task="notes", model="tiny"))
    notes = (ui.result_folder(app, "sync.mp3") / "sync.notes.md").read_text(encoding="utf-8")
    assert notes.startswith("# Meeting notes")


@pytest.mark.network
def test_a_wrong_key_is_reported_by_the_real_openai(app, page):
    ui.open_ready(page, app)
    ui.use_cloud_llm(page, "openai", "sk-proj-not-a-real-key-0000")
    expect(page.locator("#llm-model-hint")).to_contain_text("Invalid or expired OpenAI API key", timeout=30_000)


# ---- Anthropic ------------------------------------------------------------

def test_anthropic_lists_every_model_not_just_the_first_page(app, page, anthropic_api):
    app.restart(ANTHROPIC_BASE_URL=anthropic_api.base_url)
    ui.open_ready(page, app)
    ui.use_cloud_llm(page, "anthropic", anthropic_api.key)
    expect(page.locator("#llm-model option[value='claude-3-opus-20240229']")).to_have_count(1)


def test_anthropic_preselects_the_current_opus(app, page, anthropic_api):
    app.restart(ANTHROPIC_BASE_URL=anthropic_api.base_url)
    ui.open_ready(page, app)
    models = ui.use_cloud_llm(page, "anthropic", anthropic_api.key)
    expect(models).to_have_value("claude-opus-5-5")


def test_a_notes_job_with_claude_writes_the_document(app, page, anthropic_api, tmp_path):
    app.restart(ANTHROPIC_BASE_URL=anthropic_api.base_url)
    ui.open_ready(page, app)
    ui.use_cloud_llm(page, "anthropic", anthropic_api.key)
    ui.close_settings(page)
    ui.run(ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "sync.mp3"), task="notes", model="tiny"))
    notes = (ui.result_folder(app, "sync.mp3") / "sync.notes.md").read_text(encoding="utf-8")
    assert notes.startswith("# Meeting notes")


@pytest.mark.llm(replies=["529", "ok"])
def test_an_overloaded_claude_is_asked_again_and_the_job_finishes(app, page, anthropic_api, tmp_path):
    app.restart(ANTHROPIC_BASE_URL=anthropic_api.base_url)
    ui.open_ready(page, app)
    ui.use_cloud_llm(page, "anthropic", anthropic_api.key)
    ui.close_settings(page)
    ui.run(ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "sync.mp3"), task="notes", model="tiny"))


@pytest.mark.llm(replies=["refuse"])
def test_a_claude_refusal_fails_the_job_saying_so_and_keeps_the_transcript(app, page, anthropic_api, tmp_path):
    app.restart(ANTHROPIC_BASE_URL=anthropic_api.base_url)
    ui.open_ready(page, app)
    ui.use_cloud_llm(page, "anthropic", anthropic_api.key)
    ui.close_settings(page)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "sync.mp3"), task="notes", model="tiny")
    ui.start(job)
    ui.expect_status(job, "failed", timeout=ui.JOB_TIMEOUT_MS)
    expect(job.locator(".error")).to_contain_text("declined")
    assert "sync.txt" in ui.result_files(app, "sync.mp3")


@pytest.mark.network
def test_a_wrong_key_is_reported_by_the_real_anthropic(app, page):
    ui.open_ready(page, app)
    ui.use_cloud_llm(page, "anthropic", "sk-ant-api03-not-a-real-key-0000")
    expect(page.locator("#llm-model-hint")).to_contain_text("Invalid or expired Anthropic API key", timeout=30_000)


# ---- real services, free models only ---------------------------------------

@pytest.mark.network
def test_openrouter_without_a_key_says_one_is_needed(app, page):
    """Seen on the VM: "455 models available" under an empty API key box."""
    ui.open_ready(page, app)
    ui.open_settings(page)
    page.locator("#llm-provider").select_option("openrouter")
    hint = page.locator("#llm-model-hint")
    expect(hint).to_contain_text("paste your OpenRouter API key", timeout=60_000)
    expect(hint).not_to_contain_text("available")


@pytest.mark.ollama_cloud
def test_ollama_cloud_writes_a_real_document_in_russian(app, page, tmp_path):
    app.restart(OLLAMA_API_KEY=os.environ["OLLAMA_KEY"])     # never typed, never saved
    ui.open_ready(page, app)
    ui.open_settings(page)
    page.locator("#llm-provider").select_option("ollama")
    page.get_by_role("button", name="Cloud", exact=True).click()
    models = page.get_by_role("combobox", name="LLM model")
    expect(page.locator("#llm-model-hint")).to_contain_text("available", timeout=60_000)
    ui.choose(page, models, "gemma4:31b", "gemma4:31b")      # on Ollama's free plan
    ui.expect_saved(page)
    ui.close_settings(page)
    ui.run(ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "sync.mp3"), task="notes", model="tiny"))
    notes = (ui.result_folder(app, "sync.mp3") / "sync.notes.md").read_text(encoding="utf-8")
    assert len(notes) > 200 and _cyrillic_share(notes) > 0.5, notes[:300]


@pytest.mark.openrouter
def test_a_free_openrouter_model_writes_a_real_document_in_russian(app, page, tmp_path):
    app.restart(OPENROUTER_API_KEY=os.environ["OPENROUTER_KEY"])   # never typed, never saved
    ui.open_ready(page, app)
    ui.open_settings(page)
    page.locator("#llm-provider").select_option("openrouter")
    expect(page.get_by_role("combobox", name="LLM model")).to_have_value(re.compile(":free$"), timeout=60_000)
    ui.close_settings(page)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "sync.mp3"), task="notes", model="tiny")
    ui.run(job)
    notes = (ui.result_folder(app, "sync.mp3") / "sync.notes.md").read_text(encoding="utf-8")
    assert len(notes) > 200 and _cyrillic_share(notes) > 0.5, notes[:300]
