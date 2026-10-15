# AGENTS.md: MinuteNest CLI contract

`minutenest-cli.exe` (or `python -m minutenest.cli`) is the headless interface for
autonomous agents and automated pipelines. Every command is non-interactive, every
command supports `--json` for machine-readable output, and exit codes are stable.

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | success |
| `1` | usage / input error: bad flag or value, missing file, unknown settings key, unknown profile, no LLM configured for a formatted `--task`. Nothing was processed. |
| `2` | processing failure: transcription error, LLM error (bad key, unreachable server), network |

Output is UTF-8 (also when piped). Check the exit code, not stdout content. Errors are always printed to stderr as
`error: ...` (often followed by `hint: ...`). With `--json`, stdout also gets a
JSON object, so a script never parses an empty string:

```json
{"error": "no LLM is configured for --task notes", "exit_code": 1, "hint": "..."}
```

`run` checks everything it can **before** any work: option values, profile,
output folder, and for formatted tasks the LLM itself (a bad key or an
unreachable server fails in seconds, not after a long transcription).

## First-run requirements

1. **ffmpeg**: bundled in the packaged builds (`_internal/ffmpeg/`), so nothing
   to install. Running from source (`python -m minutenest.cli`) needs ffmpeg
   on `PATH`.
2. **Whisper model**: the first `run` downloads the needed model automatically
   (blocking, with progress lines; silent under `--json`). Pre-fetch with
   `models download <size>` to keep later runs fast; `models status <size>`
   checks without downloading. large-v3 is ≈ 3 GB, tiny 75 MB. A fresh install
   defaults to large-v3 on CUDA when an NVIDIA GPU is present, otherwise to
   `small` on the CPU (int8); `--model` / `settings set model_size=...` override.
3. **LLM is optional.** Default `--task raw` needs no LLM at all. Formatted output
   (`--task clean|notes|actions|testplan|summary`) requires a configured provider.
   See [Settings](#settings).

## Commands

### transcribe a file

```powershell
minutenest-cli run meeting.mp4 --task raw
```

Key flags (all optional; defaults come from saved settings):

| Flag | Values |
|------|--------|
| `--task` | `raw` (no LLM) · `clean` · `notes` · `actions` · `testplan` · `summary` · `custom` |
| `--profile` | see `minutenest-cli profiles` (default: `generic`) |
| `--llm` | `none` · `ollama` · `compat` (LM Studio/vLLM/llama.cpp) · `anthropic` · `openai` · `openrouter` |
| `--llm-model` | model name for the provider |
| `--prompt` | your own instruction for the LLM (implies `--task custom`) |
| `--model` | whisper size: `tiny base small medium large-v1 large-v2 large-v3` |
| `--device` | `cuda` · `cpu` (a failed CUDA load falls back to CPU automatically) |
| `--compute-type` | `float16` · `int8` · `int8_float16` · `float32` |
| `--language` | ISO code (`ru`, `en`, …): the language spoken in the audio |
| `--document-language` | ISO code the LLM writes the document in (default: the `document_language` setting; empty = the audio language). The transcript stays in the spoken language. |
| `--output-format` | `md` · `txt` · `both` (document format; default from settings) |
| `--no-normalize` | skip loudness normalization |
| `--chunk-length` | chunk size in seconds (default 600) |
| `--out-dir` | output directory (default: `results/` next to the app) |
| `--work-dir` | where intermediate audio lives (default: `data/work/`) |
| `--json` | machine-readable output |

**Agent-friendly one-liner**: raw transcript, no LLM, JSON output:

```powershell
minutenest-cli run meeting.mp4 --task raw --json
```

JSON payload:

```json
{
  "source": "meeting.mp4",
  "profile": "scada",
  "task": "raw",
  "elapsed_s": 942.1,
  "outputs": {
    "transcript_raw": "C:\\...\\meeting.raw.txt",
    "transcript_corrected": "C:\\...\\meeting.txt",
    "segments_json": "C:\\...\\meeting.json"
  }
}
```

`segments_json` is an array of `{start, end, text}`: the machine-readable
transcript for downstream tooling. `--task raw` never calls any LLM and never needs keys.
Formatted tasks add `"llm_models"`: the model(s) that actually answered (see the
OpenRouter note below). `"warnings"` (when present) lists things the user should
know although the run succeeded, e.g. a recording that sounds like another
language than the one set (`This recording sounds like Russian, but the audio
language is set to English: ...`); human mode prints them to stderr as `warning: ...`.

Custom output shaping:

```powershell
minutenest-cli run meeting.mp4 --task custom --prompt "Extract decisions as a bullet list" --llm ollama --llm-model gemma4:31b --json
```

### discover profiles / tasks / prompts

```powershell
minutenest-cli profiles --json
```

Returns `profiles` (name, description, language, correction count), `tasks`, and
`task_prompts` (built-in prompt per task, useful as templates for `--task custom`).

### manage whisper models

```powershell
minutenest-cli models list
minutenest-cli models status large-v3
minutenest-cli models download large-v3      # blocks until done
minutenest-cli models delete small
minutenest-cli models open-folder
```

All accept `--json`. `download` blocks until finished; poll `status` instead if
you need progress from another process. (Memory preload/unload is GUI-only. A
one-shot CLI process cannot keep a model warm for later invocations.)

### settings

```powershell
minutenest-cli settings get language --json          # single value
minutenest-cli settings get llm --json               # whole section
minutenest-cli settings set default_profile=scada language=ru
minutenest-cli settings set document_language=en     # English documents; "document_language=" = same as the audio
minutenest-cli settings set llm.provider=ollama llm.model=gemma4:31b
minutenest-cli settings set llm.openrouter_api_key=sk-or-...   # write-only
minutenest-cli settings reset                        # factory defaults (clears API keys)
```

Settings persist to `data/settings.json` next to the app (portable builds) or
`.local/settings.json` in a dev checkout; set `MINUTENEST_APP_DIR` to keep settings,
jobs and logs somewhere else. `set` takes the same dotted keys `get` shows
(`llm.provider`, `llm.model`, ...). Unknown keys and invalid values (a model
size, provider, language, profile or task that doesn't exist) are rejected with
exit code 1 and the list of valid choices, never silently ignored. Secrets are
write-only from the CLI (`get` returns masked values). Without `--json`, `set`
prints each change (`language: 'ru' -> 'en'`).

### run the web UI

```powershell
minutenest-cli serve --port 8123
```

Same app as the GUI exe; useful when you want the UI on a headless box you RDP into.

The server answers only requests addressed to `localhost`, an IP address or the PC's
own name, and refuses (HTTP 403) API requests a browser marks as coming from another
web page (`Sec-Fetch-Site`, or a foreign `Origin` on POST/PUT/DELETE). Scripts and
HTTP clients that send neither header are not affected; with `--host 0.0.0.0`, open
the UI by IP address or by the machine's own short name (what `hostname` prints),
not by a fully qualified or other DNS name.

## Automation recipes

**Nightly folder watch (PowerShell):**

```powershell
Get-ChildItem "C:\Recordings\*.mp4" | ForEach-Object {
    $out = "C:\Processed\$($_.BaseName)"
    minutenest-cli run $_.FullName --task raw --out-dir $out --json
    if ($LASTEXITCODE -ne 0) { Write-Warning "failed: $($_.Name)" }
}
```

**Transcribe + LLM test plan with Ollama local:**

```powershell
minutenest-cli run meeting.mp4 --profile scada --task testplan --llm ollama --llm-model gemma4:31b --json
```

**Check readiness before batching:**

```powershell
minutenest-cli models status large-v3 --json   # .installed == true required
minutenest-cli settings get llm --json         # provider configured?
```

## Notes for agent authors

- Long files are chunked automatically (10-minute chunks). Human progress lines
  show a time-left estimate once the speed is known (`... transcribed 4:30 of
  1:02:10  (about 6 min left)`); for a formatted task the estimate covers the
  transcript only until the LLM has answered (`about 6 min until the transcript,
  then the document`); `--json` mode prints nothing until the result.
- Formatted documents (notes, actions, testplan, summary, custom) are written
  straight from the transcript; only `--task clean` rewrites the transcript
  itself. LLM answers stream: a request that does not start answering within
  300 s (cloud) / 1800 s (local), or stops for 120 s / 600 s, or runs past
  1200 s / 3600 s is given up and asked again. `MINUTENEST_LLM_FIRST_BYTE_S`,
  `MINUTENEST_LLM_SILENCE_S` and `MINUTENEST_LLM_TOTAL_S` change those limits.
  An answer that is not a document (no text, only reasoning, a safety
  classifier's verdict, cut at the token limit, endless) is asked for again up
  to 4 times (6 for `openrouter/free`) before the run fails with exit code 2.
- Results always land in the output dir as `<name>.txt` (glossary-corrected) and
  `<name>.json` (timestamped segments), plus `<name>.raw.txt` when the chosen
  profile's glossary actually changed something (otherwise it would be a
  byte-identical duplicate). The formatted document is `<name>.<task>.md`,
  `<name>.<task>.txt`, or both, per the `output_format` setting (`md` / `txt` /
  `both`, default `both`) or the per-job override. For `--task raw` the document
  is the timestamped transcript `<name>.raw.md` (with `txt` alone there is no
  separate document: `<name>.txt` is the text version); otherwise it is the LLM document.
- A recording whose audio contains no detectable speech fails with a clear error
  instead of producing a document. Likewise, if the LLM refuses (common on very
  garbled audio), the job fails — but the transcript and segments are written
  BEFORE the LLM stage, so they are always available in the results folder.
- OpenRouter `:free` models are often throttled upstream. For a `:free` model the
  request lists up to two other free models from OpenRouter's live catalog as
  fallbacks (cost stays $0) and waits up to about two minutes before failing.
  `openrouter/free` (OpenRouter's free router, a random free model per request)
  counts as free too; its first two tries keep the router's own pick, later tries
  go to free models that write documents. `llm_models` and the final progress
  line say which model wrote the document. Paid models are never given fallbacks.
- `OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL` / `OPENROUTER_BASE_URL` redirect those
  providers (a proxy or gateway) with the vendors' SDK meaning: OpenAI's and
  OpenRouter's include the API path (`/v1`, `/api/v1`), Anthropic's is the host
  alone. Keys can also come from `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
  `OPENROUTER_API_KEY`, `OLLAMA_API_KEY`.
- The GUI exe and CLI share settings and the model cache: downloads in one are
  visible to the other.
- A second instance is prevented for the GUI exe only; parallel CLI runs are your
  responsibility (GPU memory is finite, so serialize `run` commands).

## Testing (for agents working on this repo)

`python -m pytest` runs the user-level suite described in `tests/TEST_PLAN.md`:
every test starts its own real MinuteNest process (`launcher.py`; the packaged
exe with `--app exe`) with an empty data folder, drives it in a real Edge window
through Playwright, lets Whisper (tiny/small on the CPU) and ffmpeg really run,
and checks what the user gets: what the window shows and the files in the
results folder. The LLM is a local OpenAI-compatible test server
(`tests/support/llm_server.py`); `OPENROUTER_KEY` enables real free OpenRouter
runs. Run it against the packaged build too before calling a release done:
`pytest --app exe`.

A green suite is not the finish line: before handing a build over, also click
through the packaged exe yourself (fresh data folder, CPU-only:
`CUDA_VISIBLE_DEVICES=-1` — an empty value does NOT hide the GPU on Windows)
and look at every screen.

Rules for new tests: write the test first and watch it fail against the broken
code; one behaviour per test; user steps live in `tests/support/ui.py`; only
auto-retrying `expect(...)` assertions; expected values from the requirements,
never computed by the code under test; no mocks of the app, no sleeps, no
swallowed exceptions, no source-text (grep) tests.
