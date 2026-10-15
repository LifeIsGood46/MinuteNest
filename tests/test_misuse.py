"""I. Users who do not do what the happy path expects (TEST_PLAN.md).

Double clicks, a prompt left empty, typing and starting at once, formatted
output with no LLM or a wrong key, the same file twice, reloading or a second
window while a job runs, quitting mid-job, nonsense typed into a dropdown and
a very long file name. Each test checks what the user sees and gets.
"""

import pytest
from playwright.sync_api import expect

from support import ui
from support.media import RU_SPEECH, copy_as, repeated

def test_double_clicking_start_runs_the_job_once_without_an_error(app, page, tmp_path):
    ui.open_ready(page, app)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "standup.mp3"), model="tiny")
    job.get_by_role("button", name="Start").dblclick()
    ui.expect_status(job, "done", timeout=ui.JOB_TIMEOUT_MS)
    expect(page.locator("#modal-overlay")).to_be_hidden()
    assert ui.result_files(app, "standup.mp3") == ["standup.json", "standup.raw.md", "standup.txt"]


@pytest.mark.console_errors(r"status of 400")   # the refused start, as the browser logs it
def test_a_custom_prompt_job_left_without_a_prompt_asks_for_one_instead_of_starting(app, page, llm, tmp_path):
    """It used to run a cleanup nobody asked for and save it as the custom
    document (the command line refuses the same: --task custom needs --prompt)."""
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "retro.mp3"), task="custom", model="tiny")
    job.get_by_role("button", name="Start").click()
    dialog = page.locator("#modal-overlay")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("Prompt")
    ui.confirm(page, "OK")
    ui.expect_status(job, "staged")
    assert llm.requests == []


@pytest.mark.console_errors(r"status of 400")   # the refused start, as the browser logs it
def test_after_being_asked_for_a_prompt_typing_one_and_starting_runs_the_job(app, page, llm, tmp_path):
    """The Start button stayed disabled after the refusal (output picked last,
    so nothing else rebuilt the card)."""
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "retro.mp3"), model="tiny")
    job.locator(".job-task").select_option("custom")
    expect(job.locator(".job-prompt")).to_be_visible()
    job.get_by_role("button", name="Start").click()
    ui.confirm(page, "OK")
    job.locator(".job-prompt").fill("List the decisions")
    ui.run(job)
    assert "List the decisions" in llm.requests[-1]["user"]


@pytest.mark.llm(replies=["ok"])
def test_a_prompt_typed_and_started_straight_away_is_the_one_used(app, page, llm, tmp_path):
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "planning.mp3"), task="custom", model="tiny")
    job.locator(".job-prompt").fill("List every date mentioned")
    job.get_by_role("button", name="Start").click()       # no pause: the prompt's own save is in flight
    ui.expect_status(job, "done", timeout=ui.JOB_TIMEOUT_MS)
    assert "List every date mentioned" in llm.requests[-1]["user"]


def test_a_notes_job_without_an_llm_fails_before_transcribing_and_says_where_to_set_one(app, page, tmp_path):
    ui.open_ready(page, app)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "review.mp3"), task="notes", model="tiny")
    ui.start(job)
    ui.expect_status(job, "failed", timeout=30_000)
    expect(job.locator(".error")).to_contain_text("Settings")
    assert not ui.result_folder(app, "review.mp3").exists()


def test_a_notes_job_with_a_wrong_key_fails_before_transcribing_naming_the_key(app, page, openai_api, tmp_path):
    app.restart(OPENAI_BASE_URL=openai_api.base_url)
    ui.open_ready(page, app)
    ui.use_cloud_llm(page, "openai", "sk-proj-typo-in-the-key")
    ui.close_settings(page)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "budget.mp3"), task="notes", model="tiny")
    ui.start(job)
    ui.expect_status(job, "failed", timeout=30_000)
    expect(job.locator(".error")).to_contain_text("API key")
    assert not ui.result_folder(app, "budget.mp3").exists()


def test_the_same_recording_added_twice_gets_two_cards_and_two_result_folders(app, page, tmp_path):
    ui.open_ready(page, app)
    path = copy_as(RU_SPEECH, tmp_path / "weekly.mp3")
    ui.add_files(page, path)
    ui.add_files(page, path)
    cards = page.locator("article.job")
    expect(cards).to_have_count(2)
    for i in range(2):
        cards.nth(i).locator(".job-model").select_option("tiny")
        cards.nth(i).get_by_role("button", name="Start").click()
    for i in range(2):
        ui.expect_status(cards.nth(i), "done", timeout=ui.JOB_TIMEOUT_MS)
    assert (app.results_dir / "weekly" / "weekly.txt").is_file()
    assert (app.results_dir / "weekly (2)" / "weekly.txt").is_file()


def test_reloading_the_page_while_a_job_runs_keeps_it_running_to_the_end(app, page, tmp_path):
    ui.open_ready(page, app)
    job = ui.add_job(page, repeated(RU_SPEECH, tmp_path / "town hall.mp3", 6), model="tiny")
    ui.start(job)
    ui.expect_status(job, "running")
    page.reload()
    ui.open_app(page, app)
    job = ui.card(page, "town hall.mp3")
    ui.expect_status(job, "done", timeout=ui.JOB_TIMEOUT_MS)
    expect(job.get_by_role("button", name="Open folder")).to_be_visible()


def test_a_second_window_follows_a_job_started_in_the_first(app, page, context, tmp_path):
    ui.open_ready(page, app)
    other = context.new_page()
    ui.open_app(other, app)
    job = ui.add_job(page, repeated(RU_SPEECH, tmp_path / "sync.mp3", 3), model="tiny")
    mirror = ui.card(other, "sync.mp3")
    ui.expect_status(mirror, "staged")
    ui.start(job)
    ui.expect_status(mirror, "running")
    ui.expect_status(mirror, "done", timeout=ui.JOB_TIMEOUT_MS)
    other.close()


def test_quitting_during_a_job_and_starting_again_lets_the_job_be_run_again(app, page, tmp_path):
    ui.open_ready(page, app)
    job = ui.add_job(page, repeated(RU_SPEECH, tmp_path / "offsite.mp3", 10), model="tiny")
    ui.start(job)
    ui.expect_status(job, "running")
    page.locator("#quit-btn").click()
    ui.confirm(page, "Shut down")
    assert app.proc.wait(timeout=30) == 0
    app.start()
    ui.open_app(page, app)
    job = ui.card(page, "offsite.mp3")
    ui.expect_status(job, "failed")
    expect(job.locator(".error")).to_contain_text("interrupted")
    job.get_by_role("button", name="Retry").click()
    ui.run(job)


def test_nonsense_typed_into_the_audio_language_keeps_the_previous_choice(app, page, tmp_path):
    ui.open_ready(page, app)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "chat.mp3"))
    box = job.get_by_role("combobox", name="Audio language")
    box.click()
    box.fill("klingon")
    expect(page.get_by_text("No match")).to_be_visible()
    box.press("Tab")
    expect(box).to_have_value("default (Russian)")
    expect(page.locator("#modal-overlay")).to_be_hidden()


def test_a_very_long_file_name_fits_its_card_and_the_results_are_saved(app, page, tmp_path):
    name = "Weekly sync with the infrastructure team about the migration plan, the budget, " \
           "hiring and the on-call rotation for next quarter - recording 2026-10-13.mp3"
    ui.open_ready(page, app)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / name), model="tiny")
    page.wait_for_function("(el) => el.scrollWidth <= el.clientWidth", arg=job.element_handle())
    ui.run(job)
    folders = list(app.results_dir.iterdir())
    assert len(folders) == 1
    assert name.startswith(folders[0].name)      # the recording's name, cut short at most
    transcripts = [f for f in folders[0].iterdir() if f.suffix == ".txt"]
    assert transcripts and transcripts[0].read_text(encoding="utf-8").strip()
    assert all(len(str(f)) < 260 for f in folders[0].iterdir())
