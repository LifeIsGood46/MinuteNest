"""B. Adding recordings (TEST_PLAN.md)."""

import re

import pytest
from playwright.sync_api import expect

from support import ui
from support.media import RU_SPEECH, big_file, copy_as


def test_add_a_recording_with_several_files_makes_a_card_for_each(app, page, tmp_path):
    files = [copy_as(RU_SPEECH, tmp_path / f"{name}.mp3") for name in ("standup", "retro", "planning")]
    ui.open_ready(page, app)
    ui.add_files(page, *files, via="#hello-add-btn")
    for f in files:
        ui.expect_status(ui.card(page, f.name), "staged")
    expect(page.locator("article.job")).to_have_count(3)


def test_a_dropped_file_makes_a_card(app, page, tmp_path):
    ui.open_ready(page, app)
    ui.drop_files(page, copy_as(RU_SPEECH, tmp_path / "dropped.mp3"))
    ui.expect_status(ui.card(page, "dropped.mp3"), "staged")


def test_file_names_are_shown_as_the_user_wrote_them(app, page, tmp_path):
    names = ["Встреча №1 (финал), ч.2.mp3", "Q3 review & plan #2.mp3", "O'Brien – interview.mp3"]
    ui.open_ready(page, app)
    ui.add_files(page, *(copy_as(RU_SPEECH, tmp_path / n) for n in names))
    for name in names:
        ui.expect_status(ui.card(page, name), "staged")


def test_try_with_a_sample_adds_the_english_sample(app, page):
    ui.open_ready(page, app)
    page.locator("#hello-sample-btn").click()
    sample = ui.card(page, "Sample meeting.mp3")
    ui.expect_status(sample, "staged")
    expect(sample.get_by_role("combobox", name="Audio language")).to_have_value("English (en)")


CARD_HINTS = [   # what each option on a waiting card does, shown under it and read out with it
    ("Profile", "SCADA platform meetings"),
    ("Profile", "terms in the transcript"),
    ("Audio language", "Language spoken in the recording"),
    ("Output", "QA test plan with cases. Written by the LLM."),     # whole sentences
    ("Model", "more accurate"),
    ("Output file format", "saved as"),
    ("Document language", "Language the document is written in"),
]


@pytest.mark.parametrize("control, hint", CARD_HINTS)
def test_a_waiting_card_says_what_each_option_does(app, page, tmp_path, control, hint):
    """Seen on the VM: options said "(this job only)" instead of what they do."""
    ui.open_ready(page, app)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "standup.mp3"), task="testplan")
    job.locator(".job-profile").select_option("scada")
    box = job.get_by_role("combobox", name=control, exact=True)
    expect(box).to_have_accessible_description(re.compile(re.escape(hint)))
    expect(job.get_by_text(hint)).to_be_visible()


def test_a_waiting_card_does_not_say_this_job_only(app, page, tmp_path):
    ui.open_ready(page, app)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "standup.mp3"), task="testplan")
    expect(job).not_to_contain_text("this job only")


def test_removing_a_staged_card_asks_first_and_discards_the_stored_copy(app, page, tmp_path):
    ui.open_ready(page, app)
    ui.add_files(page, copy_as(RU_SPEECH, tmp_path / "draft.mp3"))
    draft = ui.card(page, "draft.mp3")
    ui.expect_status(draft, "staged")
    draft.get_by_role("button", name="Remove").click()
    ui.confirm(page, "Cancel")
    ui.expect_status(draft, "staged")
    draft.get_by_role("button", name="Remove").click()
    ui.confirm(page, "Remove")
    expect(draft).to_have_count(0)
    assert list(app.uploads_dir.iterdir()) == []


def test_a_big_file_shows_its_card_at_once_and_can_be_cancelled_while_adding(app, page, tmp_path):
    ui.open_ready(page, app)
    ui.throttle_uploads(page, 4 * 1024 * 1024)
    ui.add_files(page, big_file(tmp_path / "all-hands.mp4", 120))
    adding = ui.card(page, "all-hands.mp4")
    ui.expect_status(adding, "uploading")
    expect(adding).to_contain_text("Loading…")
    expect(adding.locator(".badge")).to_have_text("loading")
    adding.get_by_role("button", name="Cancel").click()
    expect(adding).to_have_count(0)
    expect(page.locator("article.job")).to_have_count(0)
    assert list(app.uploads_dir.iterdir()) == []


def test_reloading_while_files_are_being_added_leaves_no_card_stuck_adding(app, page, tmp_path):
    ui.open_ready(page, app)
    ui.throttle_uploads(page, 4 * 1024 * 1024)
    ui.add_files(page, big_file(tmp_path / "first.mp4", 120), copy_as(RU_SPEECH, tmp_path / "second.mp3"))
    ui.expect_status(ui.card(page, "first.mp4"), "uploading")
    page.reload()
    expect(page.locator("body")).to_have_attribute("data-events", "on")
    expect(page.locator("article.job")).to_have_count(0)
    assert list(app.uploads_dir.iterdir()) == []
