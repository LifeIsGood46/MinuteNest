"""F. Starting and stopping the app (TEST_PLAN.md)."""

import subprocess
import sys

import pytest
from playwright.sync_api import expect

from support import native, ui
from support.app import EXE, ROOT, App, app_env
from support.media import big_file


def test_quit_asks_first_and_then_stops_the_app(app, page):
    ui.open_ready(page, app)
    page.locator("#quit-btn").click()
    ui.confirm(page, "Shut down")
    expect(page.locator("body")).to_contain_text("Shutting down")
    assert app.proc.wait(timeout=20) == 0


def test_a_partial_upload_from_a_killed_app_is_gone_after_the_next_start(app, page, tmp_path):
    ui.open_ready(page, app)
    ui.throttle_uploads(page, 4 * 1024 * 1024)
    ui.add_files(page, big_file(tmp_path / "all-hands.mp4", 200))
    ui.expect_status(ui.card(page, "all-hands.mp4"), "uploading")
    app.proc.kill()                                # the window was closed mid-upload
    app.proc.wait(timeout=10)
    app.start()
    ui.open_app(page, app)
    expect(page.locator("article.job")).to_have_count(0)
    assert list(app.uploads_dir.iterdir()) == []


def test_starting_the_app_never_touches_an_unrelated_process(tmp_path, shared_models, request):
    """The app used to check a leftover lock file with os.kill(pid, 0), which on
    Windows TERMINATES that process. Quit leaves the lock file behind, Windows
    reuses PIDs, so the next start could kill the user's browser or game."""
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    install = App(kind=request.config.getoption("--app"), root=tmp_path / "install", models=shared_models)
    install.data_dir.mkdir(parents=True)
    (install.data_dir / "minutenest.lock").write_text(str(bystander.pid))
    install.start()
    install.stop()
    assert bystander.poll() is None, f"the app killed an unrelated process (exit code {bystander.returncode})"
    bystander.kill()


@pytest.mark.exe_only
def test_an_upgrade_from_meetingnotes_keeps_the_settings_but_not_the_old_caches(tmp_path, shared_models, request):
    """A new portable copy picks up the old ~/.meetingnotes settings (CHANGELOG:
    "Settings and history from the old ~/.meetingnotes folder are still picked
    up"). Its lock file and 18 MB WebView2 browser cache used to be copied too."""
    install = App(kind=request.config.getoption("--app"), root=tmp_path / "install", models=shared_models)
    legacy = install.home_dir / ".meetingnotes"
    (legacy / "webview2").mkdir(parents=True)
    (legacy / "webview2" / "cache.bin").write_bytes(b"0" * 1024)
    (legacy / "meetingnotes.lock").write_text("4242")
    (legacy / "settings.json").write_text('{"language": "de", "default_profile": "software"}', encoding="utf-8")
    install.start()
    settings = install.get("/api/settings")
    install.stop()
    assert (settings["language"], settings["default_profile"]) == ("de", "software")
    assert not (install.data_dir / "meetingnotes.lock").exists()
    assert not (install.data_dir / "webview2" / "cache.bin").exists()


@pytest.mark.app(env={"MINUTENEST_NO_BROWSER": ""})
def test_the_app_opens_a_window_titled_minutenest_with_the_page_in_it(app):
    hwnd = native.wait_for_window()
    assert native.title(hwnd) == "MinuteNest"
    assert native.has_content(native.wait_for_content(hwnd))


def test_starting_the_app_twice_does_not_start_a_second_server(app):
    first_start = app.get("/api/version")["started_at"]
    env = app_env(app.root, app.models, {"MINUTENEST_PORT": str(app.port), "MINUTENEST_NO_BROWSER": "1"})
    cmd = [str(EXE)] if app.kind == "exe" else [sys.executable, str(ROOT / "launcher.py")]
    second = subprocess.run(cmd, check=False, cwd=str(ROOT), env=env, timeout=30)
    assert second.returncode == 0
    assert app.get("/api/version")["started_at"] == first_start
