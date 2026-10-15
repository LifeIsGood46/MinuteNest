"""C. Transcribing (TEST_PLAN.md). Real Whisper on the CPU, real ffmpeg."""

import json
import re

import pytest
from playwright.sync_api import expect

from support import ui
from support.media import (EN_SAMPLE, EN_SAMPLE_TEXT, RU_SPEECH, RU_TEXT, copy_as, in_an_echoing_room,
                           left_channel_only, not_media, repeated, share_heard, silence)

# 15 identical letters, or one word 8 times running: no speaker does either
_LOOP = re.compile(r"(\w)\1{14,}|\b(\w+)\b(?:\W+\2\b){7,}", re.I)


def test_a_russian_recording_becomes_a_transcript_of_what_was_said(app, page, tmp_path):
    ui.open_ready(page, app)
    ui.add_files(page, copy_as(RU_SPEECH, tmp_path / "lecture.mp3"))
    ui.run(ui.card(page, "lecture.mp3"))
    assert share_heard(RU_TEXT, ui.transcript(app, "lecture.mp3")) >= 0.6


def test_a_transcript_never_repeats_itself_in_a_loop(app, page, tmp_path):
    """Seen on the VM: a tiny-model transcript of a real meeting had lines of
    "No, no, no, ..." and "cccccc..." in place of whole minutes of speech."""
    ui.open_ready(page, app)
    ui.run(ui.add_job(page, in_an_echoing_room(RU_SPEECH, tmp_path / "room.wav"), model="tiny"))
    loop = _LOOP.search(ui.transcript(app, "room.wav"))
    assert loop is None, f"repetition loop in the transcript: {loop.group(0)[:80]}"


def test_the_language_chosen_on_the_card_is_the_one_transcribed(app, page, tmp_path):
    ui.open_ready(page, app)                       # default language: Russian
    ui.add_files(page, copy_as(EN_SAMPLE, tmp_path / "weekly sync.mp3"))
    job = ui.card(page, "weekly sync.mp3")
    ui.set_job_language(page, job, "English (en)")
    ui.run(job)
    assert share_heard(EN_SAMPLE_TEXT, ui.transcript(app, "weekly sync.mp3")) >= 0.6


def test_a_recording_in_another_language_than_set_says_so_on_the_card(app, page, tmp_path):
    """His check: a Russian video with English audio settings. Whisper then writes an
    English pseudo-translation without a word of warning."""
    ui.open_ready(page, app)
    ui.add_files(page, copy_as(RU_SPEECH, tmp_path / "planning.mp3"))
    job = ui.card(page, "planning.mp3")
    ui.set_job_language(page, job, "English (en)")
    ui.run(job)
    expect(job.locator(".job-warning")).to_contain_text("sounds like Russian")
    # advice the user can follow: a finished job has no Retry button
    expect(job.locator(".job-warning")).to_contain_text("again with the audio language set to Russian (ru)")
    expect(job.locator(".job-warning")).not_to_contain_text("Retry")


def test_a_recording_in_the_language_set_has_no_language_warning(app, page, tmp_path):
    ui.open_ready(page, app)                       # default language: Russian
    ui.add_files(page, copy_as(RU_SPEECH, tmp_path / "planning.mp3"))
    job = ui.card(page, "planning.mp3")
    ui.run(job)
    expect(job.get_by_role("button", name="Open folder")).to_be_visible()
    expect(job.locator(".job-warning")).to_have_count(0)


@pytest.mark.parametrize("fmt, documents", [
    ("md", ["talk.raw.md"]),
    ("txt", []),                                  # the plain .txt transcript is the text version
    ("both", ["talk.raw.md"]),
])
def test_the_output_format_decides_which_documents_are_written(app, page, tmp_path, fmt, documents):
    ui.open_ready(page, app)
    ui.open_settings(page)
    page.locator("#output-format").select_option(fmt)
    ui.expect_saved(page)
    ui.close_settings(page)
    ui.run(ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "talk.mp3"), model="tiny"))
    assert ui.result_files(app, "talk.mp3") == sorted(["talk.json", "talk.txt", *documents])


def test_the_segments_file_has_timestamps_in_order(app, page, tmp_path):
    ui.open_ready(page, app)
    ui.run(ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "talk.mp3"), model="tiny"))
    segments = json.loads((ui.result_folder(app, "talk.mp3") / "talk.json").read_text(encoding="utf-8"))
    starts = [s["start"] for s in segments]
    assert starts == sorted(starts)
    assert all(0 <= s["start"] < s["end"] <= 22.5 and s["text"].strip() for s in segments)


def test_stop_cancels_a_running_job_and_retry_runs_it_to_the_end(app, page, tmp_path):
    ui.open_ready(page, app)
    job = ui.add_job(page, repeated(RU_SPEECH, tmp_path / "long meeting.mp3", 10), model="tiny")
    ui.start(job)
    ui.expect_status(job, "running")
    job.get_by_role("button", name="Stop").click()
    ui.expect_status(job, "cancelled", timeout=30_000)
    job.get_by_role("button", name="Retry").click()
    ui.run(job)


def test_a_second_job_waits_its_turn_and_then_runs(app, page, tmp_path):
    ui.open_ready(page, app)
    first = ui.add_job(page, repeated(RU_SPEECH, tmp_path / "first.mp3", 6), model="tiny")
    second = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "second.mp3"), model="tiny")
    ui.start(first)
    ui.expect_status(first, "running")
    ui.start(second)
    ui.expect_status(second, "queued")
    expect(second).to_contain_text("Waiting")
    expect(second.locator(".job-progress")).to_have_count(0)
    ui.expect_status(second, "done", timeout=ui.JOB_TIMEOUT_MS)


def test_a_recording_without_speech_fails_and_says_so(app, page, tmp_path):
    ui.open_ready(page, app)
    ui.add_files(page, silence(tmp_path / "muted mic.wav"))
    job = ui.card(page, "muted mic.wav")
    ui.start(job)
    ui.expect_status(job, "failed", timeout=ui.JOB_TIMEOUT_MS)
    expect(job.locator(".error")).to_contain_text("No speech was detected")


def test_a_file_that_is_not_media_fails_naming_the_users_file(app, page, tmp_path):
    ui.open_ready(page, app)
    ui.add_files(page, not_media(tmp_path / "agenda.mp4"))
    job = ui.card(page, "agenda.mp4")
    ui.start(job)
    ui.expect_status(job, "failed", timeout=ui.JOB_TIMEOUT_MS)
    expect(job.locator(".error")).to_contain_text("'agenda.mp4'")
    expect(job.locator(".error")).not_to_contain_text("__")


def test_speech_on_the_left_channel_only_is_transcribed(app, page, tmp_path):
    ui.open_ready(page, app)
    ui.add_files(page, left_channel_only(RU_SPEECH, tmp_path / "one mic.wav"))
    ui.run(ui.card(page, "one mic.wav"))
    assert share_heard(RU_TEXT, ui.transcript(app, "one mic.wav")) >= 0.6


def test_the_results_folder_carries_the_users_file_name(app, page, tmp_path):
    name = "Встреча №1 (финал), итоги.mp3"
    ui.open_ready(page, app)
    ui.run(ui.add_job(page, copy_as(RU_SPEECH, tmp_path / name), model="tiny"))
    assert "Встреча №1 (финал), итоги.txt" in ui.result_files(app, name)


def test_remove_keeps_the_result_files(app, page, tmp_path):
    ui.open_ready(page, app)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "keep.mp3"), model="tiny")
    ui.run(job)
    job.get_by_role("button", name="Remove").click()
    ui.confirm(page, "Remove")
    expect(job).to_have_count(0)
    assert "keep.txt" in ui.result_files(app, "keep.mp3")


def test_delete_with_files_removes_only_that_jobs_folder(app, page, tmp_path):
    ui.open_ready(page, app)
    ui.run(ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "keep.mp3"), model="tiny"))
    drop = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "drop.mp3"), model="tiny")
    ui.run(drop)
    drop.get_by_role("button", name="Delete + files").click()
    ui.confirm(page, "Delete")
    expect(drop).to_have_count(0)
    assert not ui.result_folder(app, "drop.mp3").exists()
    assert "keep.txt" in ui.result_files(app, "keep.mp3")


def test_finished_jobs_are_still_listed_after_a_restart(app, page, tmp_path):
    ui.open_ready(page, app)
    ui.run(ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "monday.mp3"), model="tiny"))
    app.restart()
    ui.open_app(page, app)
    job = ui.card(page, "monday.mp3")
    ui.expect_status(job, "done")
    expect(job.get_by_role("button", name="Open folder")).to_be_visible()
