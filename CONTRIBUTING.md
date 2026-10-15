# Contributing

Thanks for your interest in improving MinuteNest!

## Development setup

```powershell
git clone <repo-url>
cd transcribe
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
python launcher.py
```

Running from source needs ffmpeg on PATH (the packaged exe bundles its own
copy). On Windows: `winget install ffmpeg`.

## Running tests

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest                 # against the source checkout
.venv\Scripts\python.exe -m pytest --app exe       # against dist\MinuteNest\MinuteNest.exe
.venv\Scripts\python.exe -m pytest --headed        # watch the browser
```

The suite tests the app the way a user meets it; read `tests/TEST_PLAN.md` for
what it covers and why. In short:

- **One real app per test.** Each test starts its own MinuteNest process
  (`launcher.py`, or the packaged exe with `--app exe`) with an empty data
  folder, and drives it in a real Edge window through Playwright: clicks,
  typing, the file chooser, drag and drop, confirmation dialogs.
- **Nothing is stubbed in the app.** Whisper really transcribes (the CPU, like a
  PC without a GPU), ffmpeg really extracts, files really land in a results
  folder and the tests read them. The only stand-in is the LLM: a local
  OpenAI-compatible server that speaks the same protocol as LM Studio.
- **Expected results come from the requirements**, e.g. the published
  transcript of the test recording (`tests/fixtures/`), never from the code.
- Markers: `network` (downloads a model), `openrouter` (set `OPENROUTER_KEY` to
  run free OpenRouter models for real); both skip with the reason when they
  can't run. Failures leave a screenshot, a Playwright trace and the app's log
  in `tests/artifacts/`.

When you fix a bug: write the test first, watch it fail against the broken
code, then fix. Put user steps in `tests/support/ui.py`, not in test files.
Assert only with Playwright's auto-retrying `expect(...)`; no sleeps, no
instant `.count()`/`.is_visible()` checks, no swallowed exceptions.

## Static checks

The code is held to NASA/JPL's Power of Ten rules (adapted) and ISO/IEC 5055;
[QUALITY.md](QUALITY.md) says how and why. All of these must report zero findings:

```powershell
.venv\Scripts\python.exe -m ruff check minutenest tests scripts launcher.py cli_entry.py
.venv\Scripts\python.exe -m pyright
.venv\Scripts\python.exe -m bandit -c pyproject.toml -r minutenest scripts launcher.py cli_entry.py
.venv\Scripts\python.exe scripts\check_power_of_ten.py
npx --yes eslint@9.39.1 minutenest/webapp/static
```

A rule is turned off, or a line suppressed, only with its reason written next to it.

## Building the release

`build.bat` builds both exes and the portable zip. It also runs a smoke gate
that unpacks the zip and checks the packaged app the way a fresh user would:
bundled ffmpeg runs, the CLI responds, the certifi CA bundle ships, the old
window stack is gone, and the WebView2 assets are present. Don't ship a build
that fails the gate.

```powershell
build.bat
```

The build downloads ffmpeg into `vendor/` on first run (gitignored, ~200 MB).
The WebView2 loader + type library live in `minutenest/native/`.

## Project layout

```
minutenest/            core package
  webapp/                FastAPI app + templates/static (GUI)
  llm/                   one file per LLM provider
  profiles/              domain dictionaries (JSON, 6 languages)
  native/                WebView2 loader + typelib (window hosting)
  errors.py              shared exception types (cancellation)
  ffbin.py               ffmpeg/ffprobe resolution (bundled > PATH)
  net.py                 SSL context (certifi) for providers
  webview2win.py         native window (COM, no .NET)
  cli.py                 headless interface (see AGENTS.md)
launcher.py              GUI exe entry point
cli_entry.py             CLI exe entry point
build.bat                builds both exes with PyInstaller
scripts/smoke_release.py post-build gate
tests/                   user-level tests (see tests/TEST_PLAN.md)
  support/               the app process, user steps, local LLM server, media
  fixtures/              test recordings with their published transcripts
tests/artifacts/         screenshots, traces and logs of failed tests (gitignored)
```

## Guidelines

- **No secrets in code or tests.** Keys belong in `.env` (gitignored) or the
  settings file.
- **Every user-visible behavior change** gets a test that drives the app like a
  user and fails against the old code.
- **Packaging changes need a smoke run.** Anything that touches bundled files
  (ffmpeg, native/, templates, static) must be verified through `build.bat`,
  not just pytest: unit tests can't see a missing binary in the zip.
- **Corrections maps** (domain dictionaries) contain only real normalizations,
  observed mishearings, or documented Whisper artifacts. No invented entries.
- Keep `--json` output stable: agents parse it. Additive changes are fine;
  renaming/removing fields is a breaking change.
- Error messages should say what to do next ("open Settings → LLM"), not just
  what broke.

## Pull requests

1. Fork, create a feature branch.
2. `pytest` must pass (and `pytest --app exe` on a fresh build), and the static
   checks above must report nothing.
3. If you touched the webapp, packaging, or profiles, run `build.bat` and
   smoke-test the resulting exe.
4. Keep PRs focused: one feature or fix each.

## Reporting bugs

Include: the command or UI action, the error text, and (for transcription issues)
the model size and device. Audio samples that reproduce ASR problems are gold;
trim to the failing 30 seconds.
