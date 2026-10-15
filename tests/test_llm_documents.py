"""D. Documents written by an LLM (TEST_PLAN.md).

The LLM is a local OpenAI-compatible server (support/llm_server.py), set up in
Settings exactly as a user sets up LM Studio. Cloud providers: test_cloud_llms.py.
"""

import urllib.request

import pytest
from playwright.sync_api import expect

from support import ui
from support.media import RU_SPEECH, RU_TEXT, copy_as, share_heard


@pytest.mark.parametrize("hint", [
    pytest.param("1 model available", marks=pytest.mark.llm(models=["only-model"])),
    pytest.param("2 models available", marks=pytest.mark.llm(models=["model-a", "model-b"])),
])
def test_an_lm_studio_address_lists_its_models(app, page, llm, hint):
    ui.open_ready(page, app)
    ui.open_settings(page)
    page.locator("#llm-provider").select_option("compat")
    page.locator("#compat-url").fill(llm.base_url)
    expect(page.locator("#llm-model-hint")).to_have_text(hint)


def test_a_saved_key_is_never_sent_to_another_address(app, page, ollama_api):
    """Security review: any web page can make the browser ask the app for a model
    list at an address of its choosing, and the app sent the saved key along."""
    ui.open_ready(page, app)
    ui.open_settings(page)
    page.locator("#llm-provider").select_option("ollama")
    page.locator("#ollama-host").fill("http://127.0.0.1:9")          # the saved address
    page.locator("#ollama-key").fill("ollama-key-of-the-user")
    page.locator("#ollama-key").press("Tab")
    page.wait_for_function("async () => (await (await fetch('/api/settings')).json()).llm.ollama_api_key !== ''")
    # what another site's page can make the browser request (an <img src=...> is enough)
    urllib.request.urlopen(f"{app.url}/api/llm/models?provider=ollama&host={ollama_api.base_url}", timeout=60).read()
    assert ollama_api.authorizations, "the app never asked that address"
    assert not any("ollama-key-of-the-user" in a for a in ollama_api.authorizations)


def test_a_key_from_the_environment_is_never_sent_to_another_address(app, ollama_api):
    """The same leak with the key in OLLAMA_API_KEY (AGENTS.md): the provider filled
    it in when the saved key was held back."""
    app.restart(OLLAMA_API_KEY="ollama-env-key-of-the-user")
    urllib.request.urlopen(f"{app.url}/api/llm/models?provider=ollama&host={ollama_api.base_url}", timeout=60).read()
    assert ollama_api.authorizations, "the app never asked that address"
    assert not any("ollama-env-key-of-the-user" in a for a in ollama_api.authorizations)


def test_a_server_address_that_is_not_a_web_address_is_refused(app, page):
    """Found by the security scan (bandit B310, CWE-73): the address went straight to
    urllib, which also opens file:// paths on this PC."""
    ui.open_ready(page, app)
    ui.open_settings(page)
    page.locator("#llm-provider").select_option("compat")
    page.locator("#compat-url").fill("file:///C:/Windows/win.ini")
    expect(page.locator("#llm-model-hint")).to_contain_text("not a web address")


def test_a_notes_job_writes_the_models_document_asked_for_in_the_meetings_language(app, page, llm, tmp_path):
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    ui.run(ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "standup.mp3"), task="notes", model="tiny"))
    notes = (ui.result_folder(app, "standup.mp3") / "standup.notes.md").read_text(encoding="utf-8")
    assert notes.startswith("# Meeting notes")
    assert "Russian" in llm.requests[-1]["system"]
    assert "standup.notes.txt" in ui.result_files(app, "standup.mp3")


def test_a_model_server_that_went_away_fails_the_job_with_a_clear_message(app, page, llm, tmp_path):
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    llm.stop()                                   # the user closed LM Studio
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "standup.mp3"), task="notes", model="tiny")
    ui.start(job)
    ui.expect_status(job, "failed", timeout=ui.JOB_TIMEOUT_MS)
    expect(job.locator(".error")).to_contain_text(f"Could not reach {llm.base_url}")


@pytest.mark.llm(replies=["refuse"])
def test_a_refusing_model_fails_the_job_and_keeps_the_transcript(app, page, llm, tmp_path):
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "standup.mp3"), task="notes", model="tiny")
    ui.start(job)
    ui.expect_status(job, "failed", timeout=ui.JOB_TIMEOUT_MS)
    expect(job.locator(".error")).to_contain_text("transcript")
    assert "standup.txt" in ui.result_files(app, "standup.mp3")


@pytest.mark.llm(replies=["429", "ok"])
def test_a_busy_model_is_asked_again_and_the_job_finishes(app, page, llm, tmp_path):
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    ui.run(ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "standup.mp3"), task="notes", model="tiny"))
    assert "standup.notes.md" in ui.result_files(app, "standup.mp3")


def test_switching_provider_does_not_carry_the_previous_model_over(app, page, llm):
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    ui.open_settings(page)
    page.locator("#llm-provider").select_option("openrouter")
    ui.expect_saved(page)
    expect(page.get_by_role("combobox", name="LLM model")).not_to_have_value(llm.models[0])


def test_the_cards_document_language_is_offered_only_for_documents_an_llm_writes(app, page, llm, tmp_path):
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "standup.mp3"), task="raw")
    expect(job.get_by_role("combobox", name="Document language")).to_be_hidden()
    job.locator(".job-task").select_option("notes")
    expect(job.get_by_role("combobox", name="Document language")).to_be_visible()


def test_a_document_language_picked_on_the_card_changes_the_document_not_the_transcript(app, page, llm, tmp_path):
    """Seen on the VM: to get English notes of a Russian meeting the only knob was
    the audio language, and Whisper then wrote an English pseudo-translation."""
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "standup.mp3"), task="notes", model="tiny")
    ui.choose(page, job.get_by_role("combobox", name="Document language"), "English", "English (en)")
    ui.run(job)
    assert "Write the whole document in English" in llm.requests[-1]["system"]
    assert share_heard(RU_TEXT, ui.transcript(app, "standup.mp3")) >= 0.5


def test_the_document_language_from_settings_is_used_for_new_jobs(app, page, llm, tmp_path):
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    ui.open_settings(page)
    ui.choose(page, page.get_by_role("combobox", name="Document language"), "English", "English (en)")
    ui.expect_saved(page)
    ui.close_settings(page)
    ui.run(ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "standup.mp3"), task="summary", model="tiny"))
    assert "Write the whole document in English" in llm.requests[-1]["system"]
