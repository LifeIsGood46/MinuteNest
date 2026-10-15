"""H. Layout at the window sizes users have (TEST_PLAN.md)."""

import pytest
from playwright.sync_api import expect

from support import ui
from support.media import RU_SPEECH, copy_as

SIZES = [(1366, 768), (1024, 640), (800, 600)]


@pytest.mark.parametrize("width, height", SIZES)
def test_the_main_screen_fits_without_sideways_scrolling(app, page, tmp_path, width, height):
    page.set_viewport_size({"width": width, "height": height})
    ui.open_ready(page, app)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "Квартальный отчёт отдела продаж (финал).mp3"))
    page.wait_for_function("document.documentElement.scrollWidth <= window.innerWidth")
    for control in ("#dropzone", "#help-btn", "#settings-btn", "#quit-btn"):
        expect(page.locator(control)).to_be_in_viewport()
    job.get_by_role("button", name="Start").scroll_into_view_if_needed()
    expect(job.get_by_role("button", name="Start")).to_be_in_viewport(ratio=1)


NO_SIDEWAYS_SCROLL = "(box) => box.scrollWidth <= box.clientWidth"


@pytest.mark.parametrize("width, height", SIZES)
def test_the_header_keeps_each_of_its_texts_on_one_line(app, page, width, height):
    """Seen by hand at 800x600: "tiny · in / memory", "Every meeting, written / down."."""
    page.set_viewport_size({"width": width, "height": height})
    ui.open_ready(page, app)
    page.wait_for_function("""() => [...document.querySelectorAll(".topbar-title, .tagline, #model-pill, #app-version")]
        .filter((e) => e.offsetParent !== null && e.textContent.trim())
        .every((e) => {
            const r = document.createRange();
            r.selectNodeContents(e);
            return r.getClientRects().length === 1;          // one line box
        })""")


@pytest.mark.parametrize("width, height", SIZES)
def test_settings_never_scroll_sideways(app, page, width, height):
    """Seen by hand on the packaged app: a scrollbar under Settings, from the
    hidden list behind a type-to-search box poking 5 px out of the dialog."""
    page.set_viewport_size({"width": width, "height": height})
    ui.open_ready(page, app)
    ui.open_settings(page)
    page.wait_for_function(NO_SIDEWAYS_SCROLL, arg=page.locator("#settings-overlay .modal-large").element_handle())


@pytest.mark.parametrize("width, height", SIZES)
def test_a_card_never_scrolls_sideways(app, page, tmp_path, width, height):
    page.set_viewport_size({"width": width, "height": height})
    ui.open_ready(page, app)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "standup.mp3"), task="notes")   # every control shown
    page.wait_for_function(NO_SIDEWAYS_SCROLL, arg=job.element_handle())


@pytest.mark.parametrize("field", ["Audio language", "LLM model", "Save folder"])
def test_typed_fields_are_as_tall_as_the_dropdowns_in_settings(app, page, field):
    """Seen on the VM: the type-to-search boxes were 2 px shorter than the dropdowns."""
    ui.open_ready(page, app)
    dialog = ui.open_settings(page)
    page.locator("#llm-provider").select_option("compat")    # shows the model box
    dropdown = page.locator("#llm-provider").evaluate("e => e.offsetHeight")
    expect(dialog.get_by_role("combobox", name=field).or_(dialog.get_by_role("textbox", name=field))
           ).to_have_js_property("offsetHeight", dropdown)


def test_the_cards_language_box_is_as_tall_as_its_dropdowns(app, page, tmp_path):
    ui.open_ready(page, app)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "standup.mp3"))
    dropdown = job.locator(".job-profile").evaluate("e => e.offsetHeight")
    expect(job.get_by_role("combobox", name="Audio language")).to_have_js_property("offsetHeight", dropdown)


@pytest.mark.parametrize("width, height", SIZES)
def test_no_dropdown_on_a_card_cuts_its_text(app, page, tmp_path, width, height):
    """Seen on the VM: "scada — SCADA platform r", "testplan — QA test plan wi"."""
    page.set_viewport_size({"width": width, "height": height})
    ui.open_ready(page, app)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "standup.mp3"), task="testplan")
    job.locator(".job-profile").select_option("scada")
    expect(job.locator(".job-profile")).to_have_value("scada")
    ui.wait_until_no_dropdown_cuts_its_text(page, job)


@pytest.mark.parametrize("width, height", SIZES)
def test_no_dropdown_in_settings_cuts_its_text(app, page, width, height):
    page.set_viewport_size({"width": width, "height": height})
    ui.open_ready(page, app)
    dialog = ui.open_settings(page)
    page.locator("#llm-provider").select_option("compat")
    page.locator("#default-task").select_option("clean")      # the longest output name and description
    ui.wait_until_no_dropdown_cuts_its_text(page, dialog)


@pytest.mark.parametrize("width, height", SIZES)
def test_a_cards_controls_line_up_in_equal_columns(app, page, tmp_path, width, height):
    """Seen on the VM: "Output file format", alone on its row, stretched over the whole card."""
    page.set_viewport_size({"width": width, "height": height})
    ui.open_ready(page, app)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "standup.mp3"), task="raw")
    page.wait_for_function(
        """(card) => {
            const widths = [...card.querySelectorAll(".staged-controls > .ctl")].map((c) => c.offsetWidth);
            return widths.length > 1 && Math.max(...widths) - Math.min(...widths) <= 1;
        }""", arg=job.element_handle())


def test_the_page_does_not_shift_sideways_when_it_starts_scrolling(app, page, tmp_path):
    """Seen in the manual pass: header and cards jumped ~5 px left when the first card
    made the page taller than the window (the scrollbar took its room)."""
    ui.open_ready(page, app)
    left = page.locator("#dropzone").evaluate("e => e.getBoundingClientRect().left")
    for i in range(3):
        ui.add_job(page, copy_as(RU_SPEECH, tmp_path / f"meeting {i}.mp3"), task="notes")
    page.wait_for_function("document.documentElement.scrollHeight > window.innerHeight")
    page.wait_for_function("(x) => document.querySelector('#dropzone').getBoundingClientRect().left === x",
                           arg=left)


@pytest.mark.parametrize("section",["#card-model", "#card-output", "#card-llm", "#card-appearance"])
def test_every_settings_section_can_be_reached_in_a_small_window(app, page, section):
    page.set_viewport_size({"width": 800, "height": 600})
    ui.open_ready(page, app)
    ui.open_settings(page)
    page.locator(section).scroll_into_view_if_needed()
    expect(page.locator(section)).to_be_in_viewport()
    expect(page.locator("#settings-close-btn")).to_be_in_viewport(ratio=1)
