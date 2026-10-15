"""A. First start and the setup guide (TEST_PLAN.md)."""

import pytest
from playwright.sync_api import expect

from support import ui
from support.media import EN_SAMPLE_TEXT, RU_SPEECH, repeated, share_heard


def test_a_fresh_install_opens_the_guide_and_skipping_it_is_remembered(app, page):
    ui.open_app(page, app)
    expect(page.locator("#wizard-title")).to_have_text("Welcome to MinuteNest")
    ui.skip_guide(page)
    app.restart()
    ui.open_app(page, app)
    expect(page.locator("#landing-hello")).to_be_visible()
    expect(page.locator("#wizard-tip")).to_be_hidden()


def test_without_a_gpu_the_guide_offers_the_small_model_on_the_cpu(app, page):
    ui.open_app(page, app)
    page.locator("#wizard-next").click()
    expect(page.locator("#wizard-title")).to_have_text("Step 1 — get a model")
    expect(page.locator("#model-size")).to_have_value("small")
    expect(page.locator("#model-device")).to_have_value("cpu")
    expect(page.locator("#model-compute")).to_have_value("int8")


def test_the_guide_can_be_replayed_from_help(app, page):
    ui.open_ready(page, app)
    page.locator("#help-btn").click()
    page.get_by_role("button", name="Open the walkthrough").click()
    expect(page.locator("#wizard-tip")).to_be_visible()
    expect(page.locator("#wizard-title")).to_have_text("Welcome to MinuteNest")


@pytest.mark.parametrize("width, height", [(1024, 640), (800, 600)])
def test_the_guide_stays_on_screen_in_a_small_window(app, page, width, height):
    page.set_viewport_size({"width": width, "height": height})
    ui.open_app(page, app)
    for title in ("Step 1 — get a model", "Step 2 — language & profile",
                  "Step 3 — formatted output (optional)", "Step 4 — add a recording"):
        page.locator("#wizard-next").click()
        expect(page.locator("#wizard-title")).to_have_text(title)
        expect(page.locator("#wizard-tip")).to_be_in_viewport(ratio=1)
        expect(page.locator("#wizard-next")).to_be_in_viewport(ratio=1)


@pytest.mark.network
@pytest.mark.app(models="empty")
def test_the_whole_guide_downloads_a_model_and_runs_the_sample(app, page):
    ui.open_app(page, app)
    page.locator("#wizard-next").click()
    page.locator("#model-size").select_option("tiny")
    page.locator("#download-model-btn").click()
    expect(page.locator("#model-status .badge-done")).to_be_visible(timeout=180_000)
    for title in ("Step 2 — language & profile", "Step 3 — formatted output (optional)",
                  "Step 4 — add a recording"):
        page.locator("#wizard-next").click()
        expect(page.locator("#wizard-title")).to_have_text(title)
    page.locator("#hello-sample-btn").click()
    sample = ui.card(page, "Sample meeting.mp3")
    expect(page.locator("#wizard-title")).to_have_text("Step 5 — run it your way")
    ui.run(sample)
    expect(page.locator("#wizard-title")).to_have_text("Step 5 — done")
    page.locator("#wizard-next").click()
    expect(page.locator("#wizard-tip")).to_be_hidden()
    assert share_heard(EN_SAMPLE_TEXT, ui.transcript(app, "Sample meeting.mp3")) >= 0.6


def test_the_guides_tip_never_covers_the_card_it_talks_about(app, page, tmp_path):
    """Seen by hand on the packaged app: when the job started, the tip's longer
    text grew over the card; when it finished, the tip sat on the card's name."""
    ui.open_app(page, app)
    for _ in range(4):                                    # welcome, model, language, LLM
        page.locator("#wizard-next").click()
    expect(page.locator("#wizard-title")).to_have_text("Step 4 — add a recording")
    ui.add_files(page, repeated(RU_SPEECH, tmp_path / "all hands.mp3", 10))
    job = ui.card(page, "all hands.mp3")
    expect(page.locator("#wizard-title")).to_have_text("Step 5 — run it your way")
    job.locator(".job-model").select_option("tiny")
    ui.start(job)
    expect(page.locator("#wizard-title")).to_have_text("Step 5 — running")
    ui.wait_until_the_guide_tip_is_clear_of(page, job, timeout=5_000)      # well before it finishes
    ui.expect_status(job, "done", timeout=ui.JOB_TIMEOUT_MS)
    expect(page.locator("#wizard-title")).to_have_text("Step 5 — done")
    ui.wait_until_the_guide_tip_is_clear_of(page, job)
