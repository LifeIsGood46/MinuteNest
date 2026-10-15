"""E. Whisper models in Settings (TEST_PLAN.md)."""

import pytest
from playwright.sync_api import expect

from support import ui


@pytest.mark.network
@pytest.mark.app(models="empty")
def test_a_model_downloads_with_progress_and_can_be_deleted(app, page):
    ui.open_ready(page, app)
    ui.open_settings(page)
    page.locator("#model-size").select_option("tiny")
    expect(page.locator("#model-status")).to_contain_text("not installed")
    page.locator("#download-model-btn").click()
    expect(page.locator("#model-progress-text")).to_contain_text("Downloading tiny")
    expect(page.locator("#model-status .badge-done")).to_be_visible(timeout=180_000)
    page.locator("#delete-model-btn").click()
    ui.confirm(page, "Delete")
    expect(page.locator("#model-status")).to_contain_text("not installed")


@pytest.mark.network
@pytest.mark.app(models="empty")
def test_the_download_percentage_never_goes_backwards(app, page):
    """Seen in the packaged app: "Downloading small: 83%", then 39%. The file
    counter ("5 of 6 files") and the byte counter of model.bin took turns."""
    ui.open_ready(page, app)
    ui.open_settings(page)
    page.locator("#model-size").select_option("tiny")
    page.evaluate("""() => {
        window.shownPercents = [];
        const text = document.querySelector('#model-progress-text');
        new MutationObserver(() => {
            const m = text.innerText.match(/(\\d+)%/);
            if (m) window.shownPercents.push(Number(m[1]));
        }).observe(text, { childList: true, characterData: true, subtree: true });
    }""")
    page.locator("#download-model-btn").click()
    expect(page.locator("#model-status .badge-done")).to_be_visible(timeout=180_000)
    shown = page.evaluate("window.shownPercents")
    assert shown, "no percentage was shown while downloading"
    assert shown == sorted(shown), f"the percentage went backwards: {shown}"


def test_load_puts_the_model_in_memory_and_unload_frees_it(app, page):
    ui.open_ready(page, app)
    ui.open_settings(page)
    page.locator("#model-size").select_option("tiny")
    ui.expect_saved(page)
    page.locator("#load-model-btn").click()
    expect(page.locator("#model-pill")).to_have_text("tiny · in memory", timeout=60_000)
    page.locator("#unload-model-btn").click()
    expect(page.locator("#model-pill")).to_have_text("tiny · installed")
