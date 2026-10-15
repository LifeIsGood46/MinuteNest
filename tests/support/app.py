"""Start and stop a real MinuteNest process for one test.

Every test gets its own app: own port, own empty data folder (settings, jobs,
logs), own results and work folders. The source build runs ``launcher.py``,
the same entry point the exe is built from; ``--app exe`` runs the packaged
``dist/MinuteNest/MinuteNest.exe``. No window unless a test asks for one
(MINUTENEST_NO_BROWSER=1).
The app never sees the GPU (CUDA_VISIBLE_DEVICES=-1, the VM profile; an EMPTY
value does not hide it on Windows).
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXE = ROOT / "dist" / "MinuteNest" / "MinuteNest.exe"
CLI_EXE = ROOT / "dist" / "MinuteNest" / "minutenest-cli.exe"
SHARED_MODELS = Path(os.environ.get("HF_HUB_CACHE")
                     or (Path(os.environ["HF_HOME"]) / "hub" if os.environ.get("HF_HOME")
                         else Path.home() / ".cache" / "huggingface" / "hub"))
STARTUP_S = 60


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def app_env(root: Path, models: Path, extra: dict | None = None) -> dict:
    """Environment of an isolated app (also used for the CLI)."""
    env = dict(os.environ)
    for key in [k for k in env if k.startswith("MINUTENEST_")]:
        env.pop(key)
    home = root / "home"   # a brand-new user profile: no ~/.meetingnotes to upgrade from
    home.mkdir(parents=True, exist_ok=True)
    env.update({
        "USERPROFILE": str(home),
        "HOME": str(home),
        "MINUTENEST_APP_DIR": str(root / "data"),
        "MINUTENEST_OUT_DIR": str(root / "results"),
        "MINUTENEST_WORK_DIR": str(root / "data" / "work"),
        "HF_HUB_CACHE": str(models),
        "CUDA_VISIBLE_DEVICES": "-1",
    })
    env.pop("PYTHONIOENCODING", None)  # users don't have it; it hid the CLI's code-page output
    # a fresh PC has no provider keys or endpoints; a test hands one over when it needs it
    for var in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OLLAMA_API_KEY",
                "OLLAMA_HOST", "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL"):
        env.pop(var, None)
    env.update(extra or {})
    return env


def cli_command(kind: str) -> list[str]:
    return [str(CLI_EXE)] if kind == "exe" else [sys.executable, "-m", "minutenest.cli"]


@dataclass
class App:
    kind: str                      # "source" | "exe"
    root: Path                     # everything this app writes lives under here
    models: Path                   # Hugging Face cache the app downloads to / loads from
    extra_env: dict = field(default_factory=dict)
    port: int = 0
    proc: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def results_dir(self) -> Path:
        return self.root / "results"

    @property
    def home_dir(self) -> Path:
        """The user profile the app sees (Path.home())."""
        return self.root / "home"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "work" / "_uploads"

    @property
    def log_text(self) -> str:
        log = self.data_dir / "logs" / "minutenest.log"
        return log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""

    def start(self) -> App:
        self.port = self.port or free_port()
        env = app_env(self.root, self.models)
        env.update(MINUTENEST_PORT=str(self.port), MINUTENEST_NO_BROWSER="1")
        cmd = [str(EXE)] if self.kind == "exe" else [sys.executable, str(ROOT / "launcher.py")]
        env.update(self.extra_env)
        self.root.mkdir(parents=True, exist_ok=True)
        console = (self.root / "console.log").open("ab")
        self.proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdout=console,
                                     stderr=subprocess.STDOUT)
        deadline = time.monotonic() + STARTUP_S
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"app exited with {self.proc.returncode} during startup; "
                                   f"see {self.root / 'console.log'}")
            try:
                self.get("/api/version")
                return self
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.2)
        raise RuntimeError(f"app did not answer within {STARTUP_S}s; see {self.root / 'console.log'}")

    def get(self, path: str):
        with urllib.request.urlopen(self.url + path, timeout=10) as r:
            return json.loads(r.read())

    def jobs(self) -> list[dict]:
        return self.get("/api/jobs")["jobs"]

    def stop(self) -> None:
        """Shut the app down the way the Quit button does and wait for the process."""
        if self.proc is None or self.proc.poll() is not None:
            return
        req = urllib.request.Request(self.url + "/api/shutdown", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        # the server may close the connection while it stops
        with contextlib.suppress(urllib.error.URLError, ConnectionError):
            urllib.request.urlopen(req, timeout=10).read()
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired as e:
            self.proc.kill()
            self.proc.wait(timeout=10)
            raise AssertionError("the app did not stop within 20 s of a shutdown request") from e

    def restart(self, **env: str) -> App:
        """Stop and start again on the same data folder and port, like a new launch.
        Extra environment variables (a key, a server address) apply from now on."""
        self.extra_env.update(env)
        self.stop()
        return self.start()
