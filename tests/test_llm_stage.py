"""D. The LLM stage survives the answers free models really give (TEST_PLAN.md D15-D27).

Seen on the VM and measured on openrouter/free (2026-10-13, the app's own
request, 12 tries): 4 usable answers; the rest were reasoning-only answers with
no text, a safety classifier's verdict, an "error" finish, a 22k-character
runaway; usable answers took 67-238 s. The stand-in servers (support/llm_server.py)
replay each of those answers in the provider's own streaming format.
"""

import re

import pytest
from playwright.sync_api import expect

from support import ui
from support.media import RU_SPEECH, RU_TEXT, copy_as, repeated, share_heard

# Short limits so a stalled or silent answer is given up in seconds, not minutes.
FAST_LIMITS = {"MINUTENEST_LLM_FIRST_BYTE_S": "6", "MINUTENEST_LLM_SILENCE_S": "5",
               "MINUTENEST_LLM_TOTAL_S": "60"}


def notes_of(app, name: str) -> str:
    stem = name.rsplit(".", 1)[0]
    return (ui.result_folder(app, name) / f"{stem}.notes.md").read_text(encoding="utf-8")


def run_notes(page, app, tmp_path, name: str = "standup.mp3"):
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / name), task="notes", model="tiny")
    ui.run(job)
    return job


# ---- one request, no hidden cleanup pass ------------------------------------------

def test_a_notes_job_asks_the_model_once_for_a_short_meeting(app, page, llm, tmp_path):
    """Documents went through a cleanup request first whose result nobody saw: twice the
    requests, and the slowest, most failure-prone kind (the answer is as long as the meeting)."""
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    run_notes(page, app, tmp_path)
    assert len(llm.requests) == 1
    assert "speech recognition" in llm.requests[0]["system"]


# ---- answers that are not a document are asked again --------------------------------

@pytest.mark.llm(replies=["think", "ok"])
def test_a_model_that_only_thinks_and_writes_nothing_is_asked_again(app, page, llm, tmp_path):
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    run_notes(page, app, tmp_path)
    assert notes_of(app, "standup.mp3").startswith("# Meeting notes")


@pytest.mark.llm(replies=["classifier", "ok"])
def test_a_safety_classifiers_verdict_is_not_taken_for_the_document(app, page, llm, tmp_path):
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    run_notes(page, app, tmp_path)
    assert "User Safety" not in notes_of(app, "standup.mp3")


@pytest.mark.llm(replies=["midstream", "ok"])
def test_an_error_in_the_middle_of_an_answer_is_retried(app, page, llm, tmp_path):
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    run_notes(page, app, tmp_path)
    assert notes_of(app, "standup.mp3").startswith("# Meeting notes")


@pytest.mark.app(env=FAST_LIMITS)
@pytest.mark.llm(replies=["stall", "ok"])
def test_an_answer_that_stops_midway_is_given_up_and_asked_again(app, page, llm, tmp_path):
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    run_notes(page, app, tmp_path)
    assert notes_of(app, "standup.mp3").startswith("# Meeting notes")


@pytest.mark.app(env=FAST_LIMITS)
@pytest.mark.llm(replies=["silent", "ok"])
def test_a_model_that_never_starts_answering_is_given_up_and_asked_again(app, page, llm, tmp_path):
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    run_notes(page, app, tmp_path)
    assert notes_of(app, "standup.mp3").startswith("# Meeting notes")


@pytest.mark.app(env=FAST_LIMITS)
@pytest.mark.llm(replies=["runaway", "ok"])
def test_an_answer_that_never_ends_is_cut_and_asked_again(app, page, llm, tmp_path):
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    run_notes(page, app, tmp_path)
    assert "снова и снова" not in notes_of(app, "standup.mp3")


@pytest.mark.llm(replies=["cut", "ok"])
def test_an_answer_whose_stream_just_stops_is_not_taken_as_finished(app, page, llm, tmp_path):
    """Found in review: a dropped connection mid-answer saved half a document."""
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    run_notes(page, app, tmp_path)
    assert len(llm.requests) == 2


@pytest.mark.llm(replies=["errorstop", "ok"])
def test_an_answer_that_finishes_with_an_error_is_asked_again(app, page, llm, tmp_path):
    """Measured on openrouter/free: some free models finish with "error"."""
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    run_notes(page, app, tmp_path)
    assert len(llm.requests) == 2


@pytest.mark.llm(replies=["refuse"])
def test_a_refused_piece_of_a_clean_transcript_keeps_what_was_said(app, page, llm, tmp_path):
    """Found in review: task "clean" wrote the model's refusal in place of the piece."""
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    ui.run(ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "standup.mp3"), task="clean", model="tiny"))
    cleaned = (ui.result_folder(app, "standup.mp3") / "standup.clean.md").read_text(encoding="utf-8")
    assert "sorry" not in cleaned.lower()
    assert share_heard(RU_TEXT, cleaned) >= 0.5


@pytest.mark.llm(replies=["think"])
def test_a_model_that_never_writes_fails_the_job_saying_so_and_keeps_the_transcript(app, page, llm, tmp_path):
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "standup.mp3"), task="notes", model="tiny")
    ui.start(job)
    ui.expect_status(job, "failed", timeout=ui.JOB_TIMEOUT_MS)
    expect(job.locator(".error")).to_contain_text("no text")
    expect(job.locator(".error")).to_contain_text("transcript")
    assert "standup.txt" in ui.result_files(app, "standup.mp3")
    assert len(llm.requests) == 4


# ---- progress while the model writes ---------------------------------------------------

@pytest.mark.llm(replies=["slow"])
def test_the_bar_moves_while_the_model_is_writing(app, page, llm, tmp_path):
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "standup.mp3"), task="notes", model="tiny")
    ui.start(job)
    stage = job.locator(".job-stage")
    expect(stage).to_contain_text(re.compile(r"\d+ characters so far"), timeout=ui.JOB_TIMEOUT_MS)
    fill = job.locator(".job-progress .progress-fill")
    width = fill.get_attribute("style")
    expect(fill).not_to_have_attribute("style", width or "")
    ui.expect_status(job, "done", timeout=ui.JOB_TIMEOUT_MS)


@pytest.mark.llm(replies=["slowthink"])
def test_the_bar_moves_while_the_model_is_thinking(app, page, llm, tmp_path):
    """Seen with openrouter/free in the packaged app: 95 s of hidden thinking with the
    bar frozen at 60%, which looks exactly like a hang."""
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "standup.mp3"), task="notes", model="tiny")
    ui.start(job)
    stage = job.locator(".job-stage")
    expect(stage).to_contain_text("is thinking", timeout=ui.JOB_TIMEOUT_MS)
    fill = job.locator(".job-progress .progress-fill")
    width = fill.get_attribute("style")
    expect(fill).not_to_have_attribute("style", width or "")
    expect(stage).to_contain_text("is thinking")          # it moved before any text arrived
    ui.expect_status(job, "done", timeout=ui.JOB_TIMEOUT_MS)


@pytest.mark.llm(replies=["slow"])
def test_while_transcribing_the_time_left_says_the_document_comes_after(app, page, llm, tmp_path):
    """Seen on the VM: "60% · less than a minute left", then minutes of waiting for the LLM."""
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    longer = repeated(RU_SPEECH, tmp_path / "standup.mp3", times=12)
    job = ui.add_job(page, longer, task="notes", model="tiny")
    ui.start(job)
    expect(job.locator(".job-eta")).to_contain_text("then the document", timeout=ui.JOB_TIMEOUT_MS)
    ui.expect_status(job, "done", timeout=ui.JOB_TIMEOUT_MS)


# ---- OpenRouter's free router ------------------------------------------------------------

def use_openrouter(page, app, server, model: str) -> None:
    app.restart(OPENROUTER_BASE_URL=server.base_url)
    ui.open_app(page, app)
    box = ui.use_cloud_llm(page, "openrouter", server.key)
    ui.choose(page, box, model, model)
    ui.expect_saved(page)
    ui.close_settings(page)


@pytest.mark.llm(replies=["429", "429", "429", "429", "ok"])
def test_the_free_router_gets_the_patience_free_models_need(app, page, openrouter_api, tmp_path):
    ui.open_ready(page, app)
    use_openrouter(page, app, openrouter_api, "openrouter/free")
    run_notes(page, app, tmp_path)
    assert notes_of(app, "standup.mp3").startswith("# Meeting notes")


@pytest.mark.llm(replies=["think", "classifier", "think", "think", "think", "ok"])
def test_the_free_router_keeps_trying_until_a_model_writes_the_document(app, page, openrouter_api, tmp_path):
    ui.open_ready(page, app)
    use_openrouter(page, app, openrouter_api, "openrouter/free")
    run_notes(page, app, tmp_path)
    assert notes_of(app, "standup.mp3").startswith("# Meeting notes")
    assert openrouter_api.requests[-1]["model"] != "openrouter/free"   # later tries: models that write


# ---- the local Ollama daemon streams too -------------------------------------------------

@pytest.mark.llm(models=["gemma4:12b"], replies=["slow"])
def test_a_local_ollama_model_writes_the_document(app, page, ollama_api, tmp_path):
    ui.open_ready(page, app)
    ui.open_settings(page)
    page.locator("#llm-provider").select_option("ollama")
    page.locator("#ollama-host").fill(ollama_api.base_url)   # Ollama on another port
    expect(page.locator("#llm-model-hint")).to_contain_text("1 model available")
    ui.expect_saved(page)
    ui.close_settings(page)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "standup.mp3"), task="notes", model="tiny")
    ui.start(job)
    expect(job.locator(".job-stage")).to_contain_text(re.compile(r"\d+ characters so far"),
                                                      timeout=ui.JOB_TIMEOUT_MS)
    ui.expect_status(job, "done", timeout=ui.JOB_TIMEOUT_MS)
    assert ollama_api.requests[-1]["body"]["stream"] is True
