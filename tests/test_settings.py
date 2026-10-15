"""E. Settings (TEST_PLAN.md): every control saves, confirms it, and survives a restart."""

import pytest
from playwright.sync_api import expect

from support import ui
from support.media import RU_SPEECH, copy_as

SELECTS = [
    ("#model-size", "base"),
    ("#model-compute", "float32"),
    ("#default-profile", "software"),
    ("#default-task", "summary"),
    ("#output-format", "txt"),
    ("#fx-mode", "lite"),
]


@pytest.mark.parametrize("control, value", SELECTS)
def test_a_changed_setting_is_saved_and_still_set_after_a_restart(app, page, control, value):
    ui.open_ready(page, app)
    ui.open_settings(page)
    page.locator(control).select_option(value)
    if control != "#fx-mode":                      # Visual effects is kept by this PC's browser
        ui.expect_saved(page)
    app.restart()
    ui.open_app(page, app)
    ui.open_settings(page)
    expect(page.locator(control)).to_have_value(value)


def test_the_audio_language_is_saved_and_still_set_after_a_restart(app, page):
    ui.open_ready(page, app)
    ui.open_settings(page)
    ui.choose(page, page.get_by_role("combobox", name="Audio language"), "German", "German (de)")
    ui.expect_saved(page)
    app.restart()
    ui.open_app(page, app)
    ui.open_settings(page)
    expect(page.get_by_role("combobox", name="Audio language")).to_have_value("German (de)")


def test_a_language_found_by_typing_shows_its_full_name_once_picked(app, page):
    """Seen on the VM: typed "eng", picked English, the box kept showing "eng"."""
    ui.open_ready(page, app)
    ui.open_settings(page)
    box = page.get_by_role("combobox", name="Audio language")
    ui.choose(page, box, "eng", "English (en)")
    expect(box).to_have_value("English (en)")


@pytest.mark.llm(models=["qwen3-8b", "gemma-4-12b-it", "llama-4-scout"])
def test_a_model_found_by_typing_shows_its_full_name_once_picked(app, page, llm):
    """Seen on the VM: typed "free", picked an OpenRouter model, the box kept showing "free"."""
    ui.open_ready(page, app)
    ui.open_settings(page)
    page.locator("#llm-provider").select_option("compat")
    page.locator("#compat-url").fill(llm.base_url)
    expect(page.locator("#llm-model-hint")).to_contain_text("3 models available")
    box = page.get_by_role("combobox", name="LLM model")
    ui.choose(page, box, "gemma", "gemma-4-12b-it")
    expect(box).to_have_value("gemma-4-12b-it")


def test_the_lm_studio_address_is_saved_and_still_set_after_a_restart(app, page, llm):
    ui.open_ready(page, app)
    ui.use_local_llm(page, llm)
    app.restart()
    ui.open_app(page, app)
    ui.open_settings(page)
    expect(page.locator("#llm-provider")).to_have_value("compat")
    expect(page.locator("#compat-url")).to_have_value(llm.base_url)
    expect(page.get_by_role("combobox", name="LLM model")).to_have_value(llm.models[0])


@pytest.mark.parametrize("how", ["x button", "escape", "click outside"])
def test_settings_close(app, page, how):
    ui.open_ready(page, app)
    dialog = ui.open_settings(page)
    if how == "x button":
        page.locator("#settings-close-btn").click()
    elif how == "escape":
        page.keyboard.press("Escape")
    else:
        page.mouse.click(5, 5)
    expect(dialog).to_be_hidden()


def test_results_go_to_the_chosen_save_folder(app, page, tmp_path):
    folder = tmp_path / "Мои стенограммы"
    ui.open_ready(page, app)
    ui.open_settings(page)
    page.locator("#output-dir").fill(str(folder))
    ui.expect_saved(page)
    ui.close_settings(page)
    ui.run(ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "sync.mp3"), model="tiny"))
    assert (folder / "sync" / "sync.txt").is_file()


def test_a_waiting_cards_default_audio_language_follows_the_settings_at_once(app, page, tmp_path):
    """Seen on the VM: changing the document language in Settings "switched" the card's
    audio language; the card had only been showing a stale default until then."""
    ui.open_ready(page, app)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "standup.mp3"), task="notes")
    card_audio = job.get_by_role("combobox", name="Audio language")
    expect(card_audio).to_have_value("default (Russian)")
    settings = ui.open_settings(page)
    ui.choose(page, settings.get_by_role("combobox", name="Audio language"), "English", "English (en)")
    ui.expect_saved(page)
    ui.close_settings(page)
    expect(card_audio).to_have_value("default (English)")
    settings = ui.open_settings(page)
    ui.choose(page, settings.get_by_role("combobox", name="Document language"), "Russian", "Russian (ru)")
    ui.expect_saved(page)
    ui.close_settings(page)
    expect(card_audio).to_have_value("default (English)")


def test_a_waiting_cards_default_model_follows_the_settings_at_once(app, page, tmp_path):
    ui.open_ready(page, app)
    job = ui.add_job(page, copy_as(RU_SPEECH, tmp_path / "standup.mp3"))
    expect(job.locator(".job-model option:checked")).to_have_text("default (small)")
    ui.open_settings(page)
    page.locator("#model-size").select_option("base")
    ui.expect_saved(page)
    ui.close_settings(page)
    expect(job.locator(".job-model option:checked")).to_have_text("default (base)")


def test_the_document_language_is_saved_and_still_set_after_a_restart(app, page):
    ui.open_ready(page, app)
    ui.open_settings(page)
    box = page.get_by_role("combobox", name="Document language")
    expect(box).to_have_value("Same as the audio")
    ui.choose(page, box, "English", "English (en)")
    ui.expect_saved(page)
    app.restart()
    ui.open_app(page, app)
    ui.open_settings(page)
    expect(page.get_by_role("combobox", name="Document language")).to_have_value("English (en)")
