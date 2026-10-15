"""App entry point: server + native window (no browser required).

This is what the GUI exe runs. The UI opens in a native OS window via
WebView2 COM; if that is unavailable it falls back to a chromeless Edge app
window, then to the system browser as a last resort.

Portable: the lockfile and logs live beside the exe (see minutenest.config).
Debug log (hand this file over after a test run): data/logs/minutenest.log
"""

from __future__ import annotations

import logging
import os
import subprocess  # nosec B404 - argument lists only, no shell; each call is marked
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from minutenest import config
from minutenest import logs as applog
from minutenest.net import http_request, open_url
from minutenest.webapp.main import create_app

HOST = "127.0.0.1"
# MINUTENEST_PORT: a second, isolated copy (tests, a demo beside the real one)
PORT = int(os.environ.get("MINUTENEST_PORT") or 8123)
LOCK_PATH = config.APP_DIR / "minutenest.lock"
LOG_PATH = config.APP_DIR / "launcher.log"

log = logging.getLogger("minutenest.launcher")


def _instance_running(url: str, timeout: float = 2.0) -> bool:
    try:
        with open_url(http_request(url), timeout=timeout) as r:
            return r.status == 200
    except OSError:      # nothing listening (URLError, timeout, refused)
        return False


_MUTEX = None  # Windows: held for the life of the process


def _acquire_lock() -> bool:
    """One app per data folder. False if another instance already runs.

    Windows: a named mutex, which the OS releases when the process ends, so a
    crash or Quit leaves nothing stale behind. (The old PID lockfile was
    checked with os.kill(pid, 0) -- on Windows that TERMINATES the process, so
    after a PID was reused, a new start killed an unrelated program.)
    """
    global _MUTEX
    if sys.platform == "win32":
        import ctypes
        import hashlib

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        folder = str(config.APP_DIR.resolve()).lower().encode("utf-8")
        # a short stable name for this install's lock (not security)
        name = "Local\\MinuteNest-" + hashlib.sha1(folder, usedforsecurity=False).hexdigest()[:16]
        handle = kernel32.CreateMutexW(None, False, name)
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS: another instance holds it
            kernel32.CloseHandle(handle)
            return False
        _MUTEX = handle
        return True
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.is_file():
        try:
            os.kill(int(LOCK_PATH.read_text().strip()), 0)  # POSIX: signal 0 only checks
            return False
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass  # stale lock from a crashed run: take over
    LOCK_PATH.write_text(str(os.getpid()))
    return True


def _release_lock() -> None:
    global _MUTEX
    if _MUTEX is not None:
        import ctypes

        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(_MUTEX))
        _MUTEX = None
        return
    try:
        if LOCK_PATH.is_file() and LOCK_PATH.read_text().strip() == str(os.getpid()):
            LOCK_PATH.unlink()
    except OSError:
        pass  # best effort: a leftover pidfile is ignored once its process is gone


def _log(msg: str) -> None:
    """Portable one-liner (kept for the smoke gate) + the debug log."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass   # the one-line log is unwritable; the line below still reaches the app log
    log.info(msg)


def _wait_ready(url: str, timeout: float = 15.0) -> None:
    """Until the server answers, at most ``timeout`` seconds (then the window opens anyway)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open_url(http_request(url), timeout=1):
                return
        except OSError:  # not listening yet
            time.sleep(0.25)


# ---------------------------------------------------------------------------
# window strategies: WebView2 native window -> edge app-mode -> browser
# ---------------------------------------------------------------------------


def _icon_path() -> Path | None:
    """Window/taskbar icon: bundled copy when frozen, project file in dev."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        p = base / "assets" / "logo.ico"
        if p.is_file():
            return p
        p2 = Path(sys.executable).parent / "_internal" / "assets" / "logo.ico"
        return p2 if p2.is_file() else None
    p = Path(__file__).resolve().parent / "assets" / "logo.ico"
    return p if p.is_file() else None


def _open_native_window(url: str) -> bool:
    """Real desktop window via WebView2 (no .NET). False if unavailable."""
    try:
        from minutenest.webview2win import open_webview2_window, webview2_available

        if not webview2_available():
            _log("window: WebView2 runtime not detected")
            return False
        _wait_ready(url)
        icon = _icon_path()
        _log("window: WebView2 native")
        open_webview2_window(url, "MinuteNest", 1180, 800, icon_ico=icon)
        return True
    except Exception:
        import traceback

        detail = traceback.format_exc()
        log.exception("webview2 window failed")
        _log(f"window: WebView2 failed ({detail.splitlines()[-1][:160]})")
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(f"\n=== webview2 failed {time.strftime('%H:%M:%S')} ===\n{detail}\n")
        except OSError:
            pass   # the one-line log is unwritable; log.exception above already has it
        return False


def _edge_path() -> str | None:
    """Locate Edge (or Chromium WebView2 host) for an app-mode window."""
    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for p in candidates:
        if Path(p).is_file():
            return p
    return None


def _open_edge_app_window(url: str):
    """Chromeless Edge window (no tabs/URL bar). Returns the Popen or None."""
    edge = _edge_path()
    if not edge:
        return None
    # --user-data-dir: isolated profile so the window is standalone, not
    # the user's browsing session; --app gives the chromeless frame.
    profile = config.APP_DIR / "edge-profile"
    return subprocess.Popen(  # noqa: S603  # nosec B603 - Edge from its install path, no shell
        [edge, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1180,800"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _wait_for_edge_and_shutdown(proc, server) -> None:
    """Block until the Edge app window process exits, then stop the server.

    Closing the window ends the process (isolated profile), which is our
    only close signal in this mode — without it the app would linger as a
    zombie background process.
    """
    try:
        proc.wait()
    except OSError as e:     # the process is gone either way: stop the server
        log.warning("waiting for the Edge window failed: %s", e)
    server.should_exit = True


def _focus_existing(url: str) -> None:
    """Another instance is running: open a window onto it, don't start one."""
    _wait_ready(url)
    if _open_edge_app_window(url) is None:
        webbrowser.open(url)


def _watch_browser_session(server, grace_s: float = 20.0,
                           poll_s: float = 2.0, opened: bool = True,
                           never_seen_timeout_s: float = 60.0) -> None:
    """Browser-fallback watchdog: stop the server when the last tab closes.

    In this mode there is no window handle to watch, so the web UI reports
    liveness itself (SSE connection + sendBeacon on pagehide). Without this,
    closing the tab left a python process in Task Manager forever.

    The grace window covers the gap between opening the browser and the page
    connecting. If no client ever connects and the browser failed to launch,
    we stop rather than idle forever in the background.
    """
    from minutenest.webapp.main import client_idle_seconds

    t0 = time.time()
    ever_seen = False
    while not server.should_exit:
        seen, idle = client_idle_seconds()
        ever_seen = ever_seen or seen
        if ever_seen and idle is not None and idle > grace_s:
            _log(f"browser session ended (idle {idle:.0f}s) — stopping server")
            server.should_exit = True
            return
        if not ever_seen and not opened and time.time() - t0 > never_seen_timeout_s:
            _log("no browser opened and no client connected — stopping server")
            server.should_exit = True
            return
        time.sleep(poll_s)


def _start_server(host: str, port: int):
    """The web app on a background thread: (uvicorn server, its thread)."""
    applog.setup_logging()
    log.info("launcher starting (host=%s port=%s)", host, port)
    app = create_app()
    # log_config=None: the windowed exe has no stdout; the default
    # logging config would crash binding a stream formatter.
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning", log_config=None))
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    log.info("server thread started")
    return server, server_thread


def _exit_now() -> None:
    """Exit hard once the window is gone. Graceful interpreter shutdown hangs ~30 s
    on CUDA teardown / stray non-daemon threads; state (jobs.json, settings) is
    already persisted incrementally, and the lock is released explicitly first."""
    _release_lock()
    os._exit(0)


def _show_window(url: str, server, server_thread: threading.Thread) -> None:
    """1st choice: WebView2 native window. 2nd: chromeless Edge. 3rd: system browser."""
    _wait_ready(url)
    if _open_native_window(url):
        log.info("window closed: stopping server and exiting")
        server.should_exit = True
        server_thread.join(timeout=10)
        _exit_now()
    edge_proc = _open_edge_app_window(url)
    if edge_proc is None:
        _log("window: system-browser fallback (WebView2 + edge-app unavailable)")
        webbrowser.open(url)
        # No window handle exists in this mode: watch the web UI's own liveness
        # signal so closing the tab stops the server instead of leaving a python
        # process behind.
        threading.Thread(target=_watch_browser_session, args=(server,), daemon=True).start()
    else:
        _log("window: edge app-mode fallback")
        # The Edge window's process ends when the user closes it; that's our
        # signal to stop the server (no zombie).
        _wait_for_edge_and_shutdown(edge_proc, server)
    server_thread.join()
    _exit_now()


def run(host: str = HOST, port: int = PORT) -> int:
    url = f"http://{host}:{port}"
    no_window = os.environ.get("MINUTENEST_NO_BROWSER", "").strip().lower() in ("1", "true", "yes")
    # If an instance is already up, focus it instead of starting a second copy.
    if _instance_running(url) or not _acquire_lock():
        if not no_window:
            _focus_existing(url)
        return 0
    try:
        server, server_thread = _start_server(host, port)
        if no_window:
            server_thread.join()
        else:
            _show_window(url, server, server_thread)
    finally:
        _release_lock()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception:
        import traceback

        err = traceback.format_exc()
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n{err}\n")
        except OSError:
            pass   # the log itself is unwritable: the traceback below still reaches stderr
        raise
