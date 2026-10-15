# MinuteNest test plan

## Why a new suite

The previous suite (≈390 tests) was green while the owner kept finding bugs by
clicking through the packaged app on a low-end VM. Reviewing it showed why:

| Gap in the old suite | Consequence |
|---|---|
| The browser tests replaced the whole pipeline with a stand-in (`_fake_run_pipeline`) and patched `transcribe`, `extract_audio`, `probe_duration`, `ensure_downloaded`. | No UI test ever transcribed anything; every bug between the card and the result files was invisible. |
| State was set through the API (`fetch('/api/settings')`, `/api/jobs/sample`) instead of the controls a user touches. | Broken controls (a select that does not save, a button behind an overlay) passed. |
| The app ran in-process, sharing one interpreter, one queue and module globals between tests, with paths monkeypatched. | Startup code (orphan sweep, settings migration, hardware defaults) never ran the way the exe runs it; tests depended on each other's leftovers. |
| Always the dev checkout, on a GPU machine, at one window size. | Packaging, CPU-only and small-window bugs (the VM) were out of reach. |
| Many assertions checked internals (settings JSON, private functions, source text). | Refactors broke tests while real regressions passed. |

## Principles of the new suite

1. **One real app per test.** Every UI and CLI test starts its own app process —
   the source build (`python -m minutenest.cli serve`) or the packaged exe
   (`pytest --app exe`) — with an empty data folder. No monkeypatching, no shared
   state, any test can run alone.
2. **Real components.** Real Whisper (`tiny`, on the CPU like a no-GPU VM:
   `CUDA_VISIBLE_DEVICES=-1`), real ffmpeg, real recordings, a real Edge browser.
   The only stand-in is the LLM: a local OpenAI-compatible HTTP server
   (`support/llm_server.py`), the same protocol LM Studio speaks. Real OpenRouter
   runs only when `OPENROUTER_KEY` is set.
3. **Drive the UI like a user**: click, type, choose files, drag and drop, confirm
   dialogs. The API is only read to wait for a job to finish, never to set state.
4. **Assert what the user gets**: visible text, enabled/hidden controls, files in
   the results folder and their content. Expected values come from the
   requirements (README, AGENTS.md, the in-app texts, the recording's published
   transcript), never from the code under test.
5. **Playwright discipline**: only auto-retrying `expect(...)` assertions; no
   sleeps as synchronisation, no instant `.count()`/`.is_visible()` checks, no
   swallowed exceptions; zero uncaught JavaScript errors in every test.
6. **One behaviour per test**, named as the user-visible rule it protects.

## Environments

| Option | Meaning |
|---|---|
| `--app source` (default) / `--app exe` | dev checkout / `dist/MinuteNest/MinuteNest.exe` |
| `--headed` | watch the browser |
| `CUDA_VISIBLE_DEVICES=-1` | always set for the app under test (VM profile); GPU-only behaviour is marked `gpu` |
| markers `network`, `openrouter`, `ollama_cloud` | need internet / `OPENROUTER_KEY` / `OLLAMA_KEY` (free models only); skipped with the reason otherwise |

Viewports: 1366×768 by default; layout tests also run 1024×640 and 800×600.

## Test data

| File | Content | Expected text from |
|---|---|---|
| `fixtures/ru_speech.mp3` | two Russian sentences (FLEURS, CC-BY-4.0) | the FLEURS transcription (`fixtures/ru_speech.txt`) |
| app `sample.mp3` | English meeting snippet (self-made) | its script (README: "release is on track for Friday") |
| generated at test time with ffmpeg | silence, left-channel-only speech, a renamed text file, long files | the requirement under test |

## Coverage (risk-based)

P1 = a user loses work, gets a wrong/no result or is stuck; P2 = wrong
information or friction; P3 = cosmetic. Type: E2E = UI in the browser against a
real app process; CLI = the real `minutenest-cli` process; PKG = packaged exe only.

### A. First start and the guide — `test_first_start.py`
| ID | Rule | P | Type |
|---|---|---|---|
| A1 | A fresh install opens the guide; "Skip setup" closes it and it stays closed after a restart | P1 | E2E |
| A2 | The whole guide works: download a model, pick language, skip LLM, add the sample, run it, finish | P1 | E2E, network |
| A3 | Without an NVIDIA GPU a fresh install offers `small` on the CPU (int8), not large-v3 on CUDA | P1 | E2E |
| A4 | The guide can be replayed from Help | P2 | E2E |
| A5 | The guide's tip and buttons stay on screen at 1024×640 and 800×600 | P2 | E2E |
| A6 | The guide's tip never covers the card it talks about, while the job runs (its text grows) and when it is done | P2 | E2E |

### B. Adding recordings — `test_adding_files.py`
| ID | Rule | P | Type |
|---|---|---|---|
| B1 | "Add a recording" with several files makes one card per file | P1 | E2E |
| B2 | A dropped file makes a card | P1 | E2E |
| B3 | File names are kept as written (Cyrillic, №, brackets, commas) on the card and in the results folder | P2 | E2E |
| B4 | "Try with a sample" adds the built-in recording | P2 | E2E |
| B5 | Removing a staged card asks first; Cancel keeps it, Remove deletes it and its stored copy | P1 | E2E |
| B6 | A big file shows its card at once with "Loading… N%" (badge "loading") and can be cancelled while loading; nothing is left behind | P1 | E2E |
| B7 | Reloading the page while a multi-file add is in progress leaves no card stuck at "adding" | P2 | E2E |
| B8 | A waiting card says what each option does (profile = its dictionary, audio language, output, model, file format, document language), in whole sentences, shown under it and read out with it | P2 | E2E |
| B9 | A waiting card no longer says "(this job only)" | P3 | E2E |

### C. Transcribing — `test_transcription.py`
| ID | Rule | P | Type |
|---|---|---|---|
| C1 | A Russian recording becomes a Russian transcript containing what was said | P1 | E2E |
| C2 | The card's language override is used for that job | P1 | E2E |
| C3 | Output format md / txt / both writes exactly those document files, plus `.txt` and `.json` | P1 | E2E |
| C4 | The `.json` segments are timestamped and in order | P2 | E2E |
| C5 | Stop ends a running job as "cancelled"; Retry runs it to the end | P1 | E2E |
| C6 | A second started job waits and says so, then runs | P2 | E2E |
| C7 | A recording with no speech fails with "No speech was detected" | P2 | E2E |
| C8 | A file that is not media fails naming the user's file (no internal ids) | P2 | E2E |
| C9 | Speech on the left channel only is still transcribed | P2 | E2E |
| C10 | "Remove" keeps result files; "Delete + files" asks and removes only the job's folder | P1 | E2E |
| C11 | Jobs and their results survive an app restart | P1 | E2E |
| C12 | The results folder carries the user's file name | P2 | E2E |
| C13 | A stretch Whisper can't make out (echoing room) never becomes a repetition loop in the transcript (tiny) | P1 | E2E |
| C14 | A short phrase Whisper repeated back to back ("Прикой." x6) is written once; the same phrase said again later, or a whole sentence said twice, is kept | P1 | rule (`test_transcript_rules.py`) |
| C15 | A recording in another language than the one set says so on the card, with advice the user can follow (no Retry on a finished job); none when the language matches | P1 | E2E |

### D. Documents with an LLM — `test_llm_documents.py`, `test_cloud_llms.py`
| ID | Rule | P | Type |
|---|---|---|---|
| D1 | Typing an LM Studio URL lists the server's models ("1 model available" / "N models available") | P2 | E2E |
| D2 | A "notes" job writes the model's document, asked for in the meeting's language | P1 | E2E |
| D3 | An unreachable LLM fails the job quickly with a clear message, transcript files kept | P1 | E2E |
| D4 | A refusing model fails the job, says where the transcript is, and keeps it | P2 | E2E |
| D5 | A busy model (HTTP 429) is retried and the job still finishes | P2 | E2E |
| D6 | Switching provider never carries the previous provider's model over | P2 | E2E |
| D7 | A free OpenRouter model writes a real document in the meeting's language | P1 | E2E, openrouter |
| D8 | Ollama Cloud (free plan, `gemma4:31b`) writes a real document in the meeting's language | P1 | E2E, ollama_cloud |
| D9 | OpenAI: only models that can write a document are listed; a current one is preselected; a GPT-6 "notes" job works (stand-in that enforces OpenAI's rules) | P1 | E2E |
| D10 | Anthropic: the whole (paged) model list, Opus 5.5 preselected, a "notes" job works, an overloaded answer is retried, a refusal fails clearly and keeps the transcript (stand-in that enforces Anthropic's rules) | P1 | E2E |
| D11 | A wrong key is reported as such by the REAL OpenAI and Anthropic servers (proves address, headers and auth scheme; nothing billed) | P1 | E2E, network |
| D12 | The card offers a document language only for outputs an LLM writes | P2 | E2E |
| D13 | A document language picked on the card changes the language the model is asked for, not the transcript (still Russian) | P1 | E2E |
| D14 | The document language from Settings is used for new jobs | P1 | E2E |
| D15 | A notes job asks the model once for a short meeting (no hidden cleanup request) | P1 | E2E |
| D16 | A model that only thinks and writes no text is asked again; the job finishes | P1 | E2E |
| D17 | A safety classifier's verdict ("User Safety: safe") is never taken for the document | P1 | E2E |
| D18 | An upstream error in the middle of a streamed answer is retried | P2 | E2E |
| D19 | An answer that stops midway (silence) is given up and asked again | P1 | E2E |
| D20 | A model that never starts answering (keep-alives only) is given up and asked again | P1 | E2E |
| D21 | A runaway answer is cut and asked again; none of it reaches the document | P2 | E2E |
| D22 | A model that never writes fails the job after 4 tries, saying "no text", and keeps the transcript | P1 | E2E |
| D23 | The bar moves and the card counts characters while the model writes | P1 | E2E |
| D24 | While transcribing a job with an LLM step, the time left says "then the document" (no invented LLM time) | P1 | E2E |
| D25 | openrouter/free gets the free-model patience (5 tries through 429s) | P1 | E2E |
| D26 | openrouter/free keeps trying until a model writes; later tries go to free models that write | P1 | E2E |
| D27 | A local Ollama model streams the document (native API) | P2 | E2E |
| D28 | OpenRouter without a key says one is needed instead of "N models available" | P1 | E2E, network |
| D29 | A server address that is not a web address (`file://…`) is refused, not opened | P1 | E2E |
| D30 | The bar moves while a reasoning model thinks, before any text arrives | P1 | E2E |
| D31 | After a long think the bar moves on as soon as the document starts | P2 | rule (`test_progress_rules.py`) |
| D32 | A saved Ollama/LM Studio key is never sent to an address other than the saved one (a web page can make the browser ask the app for a model list anywhere) | P1 | E2E |
| D33 | The same for a key from the environment (`OLLAMA_API_KEY`) | P1 | HTTP |

### E. Settings and models — `test_settings.py`, `test_models.py`
| ID | Rule | P | Type |
|---|---|---|---|
| E1 | Every setting confirms "Saved" and is still set after a restart (incl. document language, default "Same as the audio") | P1 | E2E |
| E7 | A language or LLM model found by typing shows its full name once picked, not the typed search | P1 | E2E |
| E8 | A waiting card's "default (…)" audio language and model follow the Settings at once; a document-language change leaves them alone | P1 | E2E |
| E2 | Settings close with X, Esc and a click outside | P2 | E2E |
| E3 | A custom save folder receives the results | P1 | E2E |
| E4 | Downloading a model shows progress and ends "installed"; Delete removes it | P1 | E2E, network |
| E6 | The download percentage never goes backwards | P2 | E2E, network |
| E5 | Load puts a model in memory, Unload frees it (header pill follows) | P2 | E2E |

### F. App lifecycle — `test_lifecycle.py`
| ID | Rule | P | Type |
|---|---|---|---|
| F1 | Quit asks first, then stops the app | P1 | E2E |
| F2 | A partial upload left by a closed window is gone after the next start | P2 | E2E |
| F3 | No uncaught JavaScript error on any page in any test (fixture-wide) | P1 | E2E |
| F4 | The app opens a native window titled MinuteNest with the page painted in it | P1 | E2E (launcher / exe) |
| F5 | A second start does not start a second server | P2 | E2E (launcher / exe) |
| F6 | Starting the app never touches an unrelated process (a leftover lock with a reused PID) | P1 | E2E (launcher / exe) |
| F7 | An upgrade from MeetingNotes keeps settings and history, not the old lock or browser cache | P2 | PKG |

### G. Command line — `test_cli.py` (contract: AGENTS.md)
| ID | Rule | P | Type |
|---|---|---|---|
| G1 | `run --task raw --json` exits 0 and lists files that exist | P1 | CLI |
| G2 | Usage errors exit 1 with `error:` on stderr and JSON on stdout (missing file, bad option, unknown profile, formatted task without LLM) | P1 | CLI |
| G3 | An unreachable LLM exits 2 before transcribing | P1 | CLI |
| G4 | `settings set/get/reset`: dotted keys, unknown keys rejected, secrets masked | P2 | CLI |
| G5 | `models status` and `profiles` answer in JSON | P3 | CLI |
| G6 | The JSON is UTF-8, also for a Russian file name (piped, no PYTHONIOENCODING) | P1 | CLI |
| G7 | `--document-language en` asks the model for English; an unknown code is a usage error (exit 1) | P1 | CLI |
| G8 | A file whose name fills the Windows path limit is transcribed; every output path stays under 260 characters | P1 | CLI |
| G9 | The JSON lists a language warning under `warnings` | P2 | CLI |
| G10 | Progress of a recording in parts names the parts ("part 1 of 3"), never the temporary chunk files | P3 | CLI |

### H. Layout — `test_layout.py`
| ID | Rule | P | Type |
|---|---|---|---|
| H1 | No horizontal scrolling and every main control reachable at 1366×768, 1024×640, 800×600 | P2 | E2E |
| H2 | Every Settings section can be scrolled into view and used at 800×600 | P2 | E2E |
| H3 | Typed fields (search boxes, Save folder) are exactly as tall as the dropdowns beside them, in Settings and on the card | P3 | E2E |
| H4 | No dropdown or search box on a card or in Settings cuts its chosen text (incl. the longest output, "clean"), at every size | P2 | E2E |
| H5 | A card's controls line up in equal columns (none stretched over a whole row) | P3 | E2E |
| H6 | The page does not shift sideways when it starts scrolling (scrollbars shown, as in the app's window) | P3 | E2E |
| H7 | Settings and the job cards never scroll sideways, at every size | P2 | E2E |
| H8 | Each header text (title, tagline, model, version) stays on one line, at every size | P3 | E2E |

### I. Users who leave the happy path — `test_misuse.py`
| ID | Rule | P | Type |
|---|---|---|---|
| I1 | Double-clicking Start runs the job once, without an error | P2 | E2E |
| I2 | Start on "Custom prompt" with an empty prompt asks for one; nothing runs, no LLM call | P1 | E2E |
| I3 | A prompt typed and started straight away (no pause) is the one the model gets | P1 | E2E |
| I4 | A notes job with no LLM set fails before transcribing and says to set one in Settings | P1 | E2E |
| I5 | A notes job with a wrong key fails before transcribing, naming the API key | P1 | E2E |
| I6 | The same recording added twice: two cards, two result folders (`x`, `x (2)`) | P2 | E2E |
| I7 | Reloading the page while a job runs: the job keeps running and finishes | P1 | E2E |
| I8 | A second window follows a job started in the first (staged, running, done) | P2 | E2E |
| I9 | Quit during a job: after the next start it says "interrupted" and runs again on Retry + Start | P1 | E2E |
| I10 | Nonsense typed into the audio language box and left with Tab: the previous choice comes back, no error | P2 | E2E |
| I11 | A very long file name: the card shows it without sideways scrolling and the results are saved under the path limit | P1 | E2E |

### J. Requests from other web pages — `test_security.py`
| ID | Rule | P | Type |
|---|---|---|---|
| J1 | Another site's page cannot shut the app down (current and older browsers) | P1 | HTTP |
| J2 | Another site's page cannot make the app fetch from an address it chooses | P1 | HTTP |
| J3 | A domain pointed at this PC (DNS rebinding) gets no answer | P1 | HTTP |
| J4 | The app still answers its own addresses, its own pages and scripts | P1 | HTTP |
| J5 | A flood of refused requests writes at most 20 log lines | P3 | HTTP |

## Not automated (manual release checklist)

- Opening Explorer ("Open folder", "Models folder", "Browse…"): opens windows on the tester's desktop.
- Real GPU speed and VRAM release after 10 idle minutes.
- Visual polish of animations ("Visual effects").
- Glossary corrections ("скандала" → SCADA): needs a licence-free recording with a
  known mishearing; there is none yet (no Russian TTS voice on the build PC).
These are walked through by hand on the packaged build before each release.

## Bugs this suite found while it was written (2026-10-10)

| Test | Bug | Fixed in |
|---|---|---|
| B7 | Reloading during a multi-file add left cards stuck at "adding 0%" | `webapp/main.py`, `app.js` (page-owned uploads) |
| D4 | A model's refusal ("I'm sorry, but I can't help…") was saved as the notes | `polish.looks_like_refusal` |
| F1 | Quit killed the app (exit code 15) instead of ending it | `webapp/main.py::_kill` |
| F6 | A start could TERMINATE an unrelated process (`os.kill(pid, 0)` on Windows) | `launcher.py` named mutex |
| B4/E1 | Language pickers announced as "Search"/"language" to screen readers | `app.js`, `index.html` |
| C6 | A second "Add" within 300 ms of the first opened no file chooser (timer-based guard) | `app.js` (guard released when the dialog closes) |
| E6 | Download % jumped 17 → 0 → 33 → 0 (file counter vs bytes on one bar) | `models._make_progress_tqdm` |
| D9 | OpenAI's current models rejected the app's `max_tokens`; non-chat models were offered | `llm/openai.py` |
| D10 | Anthropic: first page of models only; preselected the most expensive model; refusal shown as "unexpected response shape"; 529 not retried | `llm/anthropic.py`, `llm/base.py`, `webapp/main.py` |
| D11 | `ANTHROPIC_BASE_URL` (set by e.g. Claude Code) was read with the wrong meaning: every Anthropic request went to a 404 | `llm/anthropic.py` (host without /v1, like the SDK) |
| G6 | Piped CLI output was in the ANSI code page, not UTF-8 (hidden by a test env var) | `cli._utf8_when_piped` |
| A3, G2 (`--app exe` only) | Every new packaged copy copied the old `~/.meetingnotes` (settings with API keys, lock, browser cache): fresh installs were not fresh | tests isolate the user profile; `config._migrate_legacy_home` skips lock + cache (F7) |

## Bugs found by hand on the VM (2026-10-12), now covered

| Test | Bug | Fixed in |
|---|---|---|
| E7 | After typing "eng" / "free" and picking an option, the box kept showing the typed text | `cbx.js` (`pick` closes the search before writing the value) |
| H3 | Type-to-search boxes were 2 px shorter than the dropdowns | `style.css` (one height for selects and inputs) |
| B6 | A file being loaded said "Adding…" | `webapp/main.py`, `app.js` |
| C13 | Tiny wrote "No, no, no, …" / "cccc…" for minutes of a real meeting: one decoding pass at T=0, nothing re-decoded a loop | `transcribe.decode_options` (temperature fallback on a compression ratio over 2.4) |
| D13, D14, G7 | The audio language also decided the document language: English notes of a Russian meeting meant a garbled English transcript | separate document language (settings, card, CLI, `pipeline`, `polish.language_rule`) |

## Bugs the misuse tests found (2026-10-14), now covered

| Test | Bug | Fixed in |
|---|---|---|
| I2 | "Custom prompt" with an empty prompt quietly ran a cleanup nobody asked for and saved it as the custom document | `webapp/main.py::start_job` refuses; the card shows why |
| I9 | Quit during a job brought it back as "ready to start": queued/running were never saved, so the "interrupted" path never ran | `webapp/main.py` (state saved at queued and running) |
| I10 | Leaving a dropdown's search with Tab kept the typed text (and dropped a typed custom model name) | `cbx.js` (blur handles leaving, whatever the key) |
| I11, G8 | A ~150-character file name failed: "Could not save the file (No such file or directory)" — the name was used twice in paths over Windows' 260-character limit | `names.py` (shortened only when a path would not fit), used by uploads, results, work files and the CLI |

## Bugs found by hand in the packaged app (2026-10-15), now covered

| Test | Bug | Fixed in |
|---|---|---|
| H7 | A scrollbar under Settings (and on cards at 1024/800): the hidden list behind a search box kept the dropdowns' arrow padding, 48 px instead of 1 px | `style.css` (`.cbx-native`) |
| A6 | The guide's tip grew over the running job's card, and sat on a finished card's name | `app.js` (re-placed when its text changes; points at the whole card) |
| C15 | The language warning said "press Retry" on a finished job, which has no Retry button | `pipeline._language_warning` |
| G10 | Progress said "Transcribing chunk_001.wav [cpu/int8]" and "Transcription done" after each part | `transcribe.transcribe` |
| B8, H4 | "Structured meeting notes with sections Written by the LLM." (no full stop); the card's document language cut to "default (same as the au…" | `webapp/main.py` task descriptions; `app.js` ("Same as the audio") |
| D30 | 95 s of a free model's hidden thinking at a frozen 60% | `polish.Slot` (the bar creeps while the model thinks) |
| H8 | At 800 px the header wrapped "tiny · in / memory" | `style.css` |
| H4 | Settings' default output cut "clean — Transcript with punctuation and paragraphs fi…" | `app.js`, `index.html` (short names, description as a hint) |
| D31 | After a long think the bar stood still until the document caught up with the thinking's share | `polish.Slot` |
