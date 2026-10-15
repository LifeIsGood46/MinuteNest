"""What a user does in the MinuteNest window, as reusable steps.

Only user-level actions (click, type, choose files, drop, confirm) and
auto-retrying expectations. Nothing here sets state through the API.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

from playwright.sync_api import Locator, Page, expect

JOB_TIMEOUT_MS = 120_000  # tiny on a CPU; an upper bound for expect, not a sleep


# ---- app & guide ----------------------------------------------------------

def open_app(page: Page, app) -> None:
    """Load the app and wait until it has its settings and live updates."""
    page.goto(app.url)
    expect(page.locator("body")).to_have_attribute("data-events", "on")
    expect(page.locator("#model-pill")).not_to_have_text("…")


def skip_guide(page: Page) -> None:
    tip = page.locator("#wizard-tip")
    expect(tip).to_be_visible()
    page.locator("#wizard-skip").click()
    expect(tip).to_be_hidden()


def open_ready(page: Page, app) -> None:
    """A fresh install, guide skipped: the state a returning user starts from."""
    open_app(page, app)
    skip_guide(page)


def confirm(page: Page, button: str) -> None:
    """Answer the app's own confirmation dialog with one of its buttons."""
    dialog = page.locator("#modal-overlay")
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name=button, exact=True).click()
    expect(dialog).to_be_hidden()


# ---- adding recordings ----------------------------------------------------

def add_files(page: Page, *paths: Path, via: str = "#dropzone") -> None:
    """Click the drop zone (or another add button) and pick files in the chooser."""
    with page.expect_file_chooser() as chooser:
        page.locator(via).click()
    chooser.value.set_files([str(p) for p in paths])


def drop_files(page: Page, *paths: Path) -> None:
    """Drag files from Explorer onto the drop zone."""
    files = [{"name": p.name, "b64": base64.b64encode(p.read_bytes()).decode()} for p in paths]
    page.locator("#dropzone").evaluate(
        """(zone, files) => {
            const dt = new DataTransfer();
            for (const f of files) {
                const bytes = Uint8Array.from(atob(f.b64), (c) => c.charCodeAt(0));
                dt.items.add(new File([bytes], f.name));
            }
            for (const type of ["dragenter", "dragover", "drop"]) {
                zone.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dt }));
            }
        }""",
        files,
    )


def throttle_uploads(page: Page, bytes_per_s: int) -> None:
    """A slow disk or network: uploads from this tab go at most this fast."""
    cdp = page.context.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.send("Network.emulateNetworkConditions", {"offline": False, "latency": 0,
                                                  "downloadThroughput": -1, "uploadThroughput": bytes_per_s})


def card(page: Page, filename: str) -> Locator:
    """The job card whose title is exactly this file name."""
    title = page.locator(".job-name", has_text=re.compile(rf"^{re.escape(filename)}$"))
    return page.locator("article.job", has=title)


def add_job(page: Page, path: Path, task: str | None = None, model: str | None = None) -> Locator:
    """Add one recording and optionally pick the card's output (task) and model."""
    add_files(page, path)
    job = card(page, path.name)
    expect_status(job, "staged")
    if task:
        job.locator(".job-task").select_option(task)
        expect(job.locator(".job-task")).to_have_value(task)
    if model:
        job.locator(".job-model").select_option(model)
        expect(job.locator(".job-model")).to_have_value(model)
    return job


def wait_until_no_dropdown_cuts_its_text(page: Page, scope: Locator) -> None:
    """Every closed dropdown in ``scope`` shows its whole chosen text (auto-retrying):
    plain dropdowns and the type-to-search boxes (language, LLM model) alike.

    The dropdown's arrow is drawn in its right padding, so the room for text is
    the width inside the padding.
    """
    page.wait_for_function(
        """(root) => [...root.querySelectorAll("select:not(.cbx-native), .cbx-input")].every((s) => {
            const cs = getComputedStyle(s);
            const ctx = document.createElement("canvas").getContext("2d");
            ctx.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
            const text = s.tagName === "SELECT" ? (s.selectedOptions[0] || {}).textContent || "" : s.value;
            const room = s.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
            return ctx.measureText(text).width <= room;
        })""", arg=scope.element_handle())


def wait_until_the_guide_tip_is_clear_of(page: Page, target: Locator, timeout: int = 15_000) -> None:
    """The guide's tip does not cover ``target`` (auto-retrying)."""
    page.wait_for_function(
        """(el) => {
            const t = document.querySelector("#wizard-tip").getBoundingClientRect();
            const c = el.getBoundingClientRect();
            return t.bottom <= c.top || t.top >= c.bottom || t.right <= c.left || t.left >= c.right;
        }""", arg=target.element_handle(), timeout=timeout)


def expect_status(job: Locator, status: str, timeout: int = 15_000) -> None:
    expect(job).to_have_attribute("data-status", status, timeout=timeout)


# ---- running jobs ---------------------------------------------------------

def start(job: Locator) -> None:
    expect_status(job, "staged")
    job.get_by_role("button", name="Start").click()


def run(job: Locator) -> None:
    """Start a staged job and wait until it is done."""
    start(job)
    expect_status(job, "done", timeout=JOB_TIMEOUT_MS)


def choose(page: Page, combobox: Locator, text: str, option: str) -> None:
    """Pick from a type-to-search dropdown: click it, type, click the option."""
    combobox.click()
    combobox.fill(text)
    page.get_by_role("option", name=option, exact=True).click()


def set_job_language(page: Page, job: Locator, option: str) -> None:
    choose(page, job.get_by_role("combobox", name="Audio language"), option.split(" (", maxsplit=1)[0], option)


def result_folder(app, filename: str) -> Path:
    return app.results_dir / Path(filename).stem


def result_files(app, filename: str) -> list[str]:
    folder = result_folder(app, filename)
    return sorted(p.name for p in folder.iterdir() if not p.name.startswith("."))


def transcript(app, filename: str) -> str:
    stem = Path(filename).stem
    return (result_folder(app, filename) / f"{stem}.txt").read_text(encoding="utf-8")


# ---- settings -------------------------------------------------------------

def open_settings(page: Page) -> Locator:
    page.locator("#settings-btn").click()
    dialog = page.locator("#settings-overlay")
    expect(dialog).to_be_visible()
    return dialog


def close_settings(page: Page) -> None:
    page.locator("#settings-close-btn").click()
    expect(page.locator("#settings-overlay")).to_be_hidden()


def expect_saved(page: Page) -> None:
    expect(page.locator("#settings-saved")).to_be_visible()
    expect(page.locator("#settings-saved")).to_contain_text("Saved")


def use_local_llm(page: Page, server) -> None:
    """Settings -> LLM: LM Studio / OpenAI-compatible at the test server, its first model."""
    open_settings(page)
    page.locator("#llm-provider").select_option("compat")
    page.locator("#compat-url").fill(server.base_url)
    hint = page.locator("#llm-model-hint")
    expect(hint).to_contain_text("available")
    choose(page, page.get_by_role("combobox", name="LLM model"), server.models[0], server.models[0])
    expect_saved(page)
    close_settings(page)


def use_cloud_llm(page: Page, provider: str, key: str) -> Locator:
    """Settings -> LLM: pick a cloud provider and paste the key; returns the model box."""
    open_settings(page)
    page.locator("#llm-provider").select_option(provider)
    page.locator("#cloud-key").fill(key)
    expect_saved(page)
    return page.get_by_role("combobox", name="LLM model")
