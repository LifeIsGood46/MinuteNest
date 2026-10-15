# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Renamed
- **MeetingNotes is now MinuteNest**, with a new logo (sound waves turning into lines of
  text). The app window, guide, release folder and zip (`MinuteNest-<version>-win64.zip`)
  and the app exe (`MinuteNest.exe`) use the display name. Lower-case identifiers follow it
  too: the Python package and commands (`minutenest`, `minutenest-cli`), log and lock files,
  and the `MINUTENEST_*` environment variables (`MEETING_NOTES_*` became `MINUTENEST_*`).
  Settings and history from the old `~/.meetingnotes` folder are still picked up on
  first run. Version 0.4.0.

### Added
- **Checked against published quality standards** (`QUALITY.md`): NASA/JPL's
  Power of Ten rules adapted to Python and JavaScript, ISO/IEC 25010:2023,
  ISO/IEC 5055 (code weaknesses, CWE) and ISO/IEC/IEEE 29119-3 (test
  documentation). ruff, pyright, bandit, ESLint and `scripts/check_power_of_ten.py`
  (no recursion, no function over 60 lines) all run with zero findings; the
  longest functions of the web UI and the pipeline were split to get there.
- **A recording that sounds like another language than the one set is pointed
  out**: "This recording sounds like Russian, but the audio language is set to
  English" on the card, and in `warnings` of the CLI JSON (stderr in human mode).
- The time limits of an LLM answer can be changed (`MINUTENEST_LLM_FIRST_BYTE_S`,
  `MINUTENEST_LLM_SILENCE_S`, `MINUTENEST_LLM_TOTAL_S`); `OPENROUTER_BASE_URL`
  points OpenRouter at a proxy.
- Tests for users who leave the happy path (`tests/test_misuse.py`, section I of
  the test plan): double clicks, an empty prompt, reloading or a second window
  during a job, quitting mid-job, nonsense typed into a dropdown, long file names.
- **Document language, separate from the audio language.** The language the LLM
  writes notes, summaries and test plans in used to be the audio language, so the
  only way to get English notes of a Russian meeting was to tell Whisper the
  meeting was in English, which turned the transcript into a garbled
  pseudo-translation. Settings → Output now has *Document language* ("Same as the
  audio" by default) next to *Audio language*, each job card has its own (for LLM
  outputs), and the CLI has `--document-language` / `settings set document_language=`.
- `OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL` point those providers at a proxy or
  gateway, with the same meaning as in the vendors' own SDKs (OpenAI's includes
  `/v1`, Anthropic's is the host alone).
- `MINUTENEST_APP_DIR` moves settings, job history and logs to another folder.
- The model cache honours `HF_HOME` the same way Hugging Face does (a model
  downloaded there used to show as "not installed").
- Privacy and licence sections in the README, `THIRD_PARTY_NOTICES.md`, and a
  `licenses/` folder inside the release zip (app MIT licence, NVIDIA CUDA terms,
  third-party notices). The release smoke check fails if they are missing.

### Fixed
- **OpenRouter without an API key no longer says "455 models available".** The
  catalog is public, the models are not: the box now says to paste a key.
- **A waiting card says what each option does** ("Fixes its N terms in the
  transcript", "Language spoken in the recording", ...), under the option and read
  out with it, instead of "(this job only)" everywhere.
- **Changing the document language in Settings no longer seems to change a
  card's audio language.** Waiting cards now follow every saved default at once;
  before, a card kept an old "default (…)" label until some other setting rebuilt it.
- **Progress and time left are honest during the LLM step.** While transcribing,
  a job that still needs the LLM says "about 6 min until the transcript, then the
  document" instead of a guessed "less than a minute"; while the model writes, the
  bar moves and the card counts what has arrived.
- **Free OpenRouter models, including the `openrouter/free` router, finish the
  job.** Answers are streamed with time limits (none starts within 300 s, stops
  for 120 s, or runs past 1200 s by default), and an answer that is no document
  (empty, reasoning only, a safety classifier's verdict, cut at the token limit,
  endless) is asked for again: up to 4 times, 6 for the router, whose later tries
  go to free models known to write documents. Before, one call waited up to 240 s
  and an empty answer failed the job.
- Documents are written straight from the transcript: a notes job asks the model
  once for a short meeting (a hidden cleanup pass ran first and doubled the
  waiting). Only the "Clean transcript" output rewrites the transcript.
- **A short phrase Whisper repeats back to back is written once** ("Прикой." six
  times in a row on a low-quality recording, tiny); the same phrase said again
  later, or a whole sentence said twice, is kept.
- **"Custom prompt" with an empty prompt asks for one** instead of quietly running
  a cleanup nobody asked for and saving it as the custom document.
- **Quitting during a job**: after the next start the job says "interrupted by
  app restart" and can be run again. It came back as "ready to start" as if
  nothing had happened.
- **Long file names work.** A recording with a ~150-character name failed with
  "Could not save the file (No such file or directory)": the name was used twice
  in paths longer than Windows allows (260 characters). Names are kept as they
  are and shortened only where a path would not fit.
- Leaving a dropdown's search with Tab brings the chosen value back (it kept the
  typed text, and dropped a model name typed into the LLM model box).
- Start, Retry and Stop say why when the app refuses them; a double-clicked
  Start starts the job once. Changes on a waiting card that the app refuses are
  shown instead of silently ignored.
- A server address that is not a web address (`file://…`) is refused.
- **Security:** a saved Ollama or LM Studio key (or one from `OLLAMA_API_KEY` /
  `COMPAT_API_KEY`) is sent only to the saved address.
  Any web page could make the browser ask the app for a model list at an address
  of its choosing, and the app sent the saved key along.
- **Security:** other web pages can no longer use the app. Any page open in the
  browser could make it shut down, delete results or fetch from an address of the
  page's choosing; a domain pointed at 127.0.0.1 could even read its answers. The
  app now answers only its own pages, scripts and the command line.
- **The bar moves while a model thinks.** Reasoning models (often what the free
  `openrouter/free` router picks) think for a minute or more before writing a word;
  the bar sat at 60% the whole time. It now creeps forward while the card counts
  the thinking, and jumps ahead once the document streams in.
- No more scrollbar under Settings (and on job cards in smaller windows): the
  hidden list behind each search box was 48 px wide instead of 1 px.
- The setup guide's tip no longer covers the job card it talks about (it grew
  over a running job's card, and sat on a finished card's name).
- The language warning gives advice that can be followed ("Transcribe it again
  with the audio language set to Russian (ru)"), not "press Retry" on a finished job.
- Progress names the parts of a long recording ("Transcribing part 2 of 3 on the
  CPU (int8)") instead of temporary files ("chunk_001.wav"), and no longer says
  "Transcription done" after each part.
- Card hints are whole sentences ("Structured meeting notes with sections.
  Written by the LLM."); the card's document language says "Same as the audio"
  instead of a label cut to "default (same as the au…".
- At 800 px wide the header keeps each text on one line.
- Settings' *Default output for new jobs* shows short names ("Clean transcript")
  with what each does under it, as the cards do; long ones were cut off.
- **No more repetition loops in transcripts from the small models.** On a real
  22-minute meeting, tiny wrote lines like "No, no, no, no, ..." and "cccccc..." in
  place of whole minutes of speech: decoding ran once at temperature 0, and nothing
  re-decoded a passage that came out as a loop. A passage whose text repeats itself
  (compresses more than 2.4x) is decoded again now. Measured on the CPU on three
  real meetings and a 12-minute podcast with a reference text: tiny went from 6
  loops to 0 for 20-35% more time with the same accuracy (WER 28.0% / 28.4%);
  small and large-v3 had no loops and give the same accuracy (11.68%, 7.36%), and
  large-v3 re-decoded nothing.
- **Searching a dropdown and picking a result now shows the result.** After typing
  "eng" in Audio language (or "free" in the LLM model box) and picking an entry, the
  box went on showing the typed search instead of the chosen name.
- Typed fields (the search boxes, Save folder, API keys) are as tall as the
  dropdowns next to them; they were 2 px shorter.
- A file being loaded into the app says "Loading… N%" (badge "loading"), not "Adding…".
- **OpenAI keys work with the current models.** The app asked for `max_tokens`,
  which the GPT-5/GPT-6 and o-series models reject (OpenAI API reference); it now
  sends `max_completion_tokens`, which every chat model accepts, with room for
  reasoning. The model list no longer offers speech, voice, image, realtime,
  embedding or moderation models that can't write a document, and preselects a
  current model (`gpt-5.4-mini`) instead of the retired `gpt-4o` family.
- **Anthropic keys get the whole model list and a sensible default.** Only the
  first page (20 models) was read; and with the old default (`claude-sonnet-4-5`)
  gone, the box preselected the alphabetically first model, `claude-fable-5` -
  the most expensive one. It now preselects `claude-opus-5-5`. Claude's answers
  get room for its thinking (always on for Opus 5.5), an "overloaded" answer (529)
  or a server error (500) is retried like a rate limit, and a safety refusal
  fails the job with "Claude declined this request" instead of "Unexpected
  response shape" (the transcript is kept).
- **Starting MinuteNest could close another program.** The single-instance check
  read the process id from a leftover lock file and "tested" it with
  `os.kill(pid, 0)`, which on Windows *terminates* that process. Quit always left
  the lock file behind and Windows reuses process ids, so a later start could
  kill whatever program had that id (a browser, a game), and then refuse to start
  itself. Two quick double-clicks could also make the second start kill the
  first. The check is now a Windows named mutex, which the system releases when
  the app ends; nothing stale is left behind.
- **The model download percentage only goes up.** It jumped 17 -> 0 -> 33 -> 0 ...
  because the "N of 6 files" counter and the bytes of the model file took turns
  on the same bar; it now counts the bytes of all files.
- **Quit ends the app cleanly** (exit code 0; it used to be killed with code 15).
- **The command line's output is UTF-8 when it goes to a script.** Piped output
  used the Windows ANSI code page: a Russian file name in `--json` came out as
  cp1251 bytes that a UTF-8 reader could not decode, and a PC whose code page has
  no Cyrillic would have crashed printing it.
- Upgrading from MeetingNotes still brings settings and job history into a new
  copy, but no longer copies the old lock file and the WebView2 browser cache
  (18 MB here).
- **Reloading the page while several files were being added no longer leaves
  cards stuck at "adding 0%".** The files still waiting are dropped with their
  cards when the page that was adding them goes away.
- **A model that refuses ("I'm sorry, but I can't help with that request.") no
  longer gets its refusal saved as the meeting notes.** The job fails, says the
  transcript was kept, and the transcript stays in the results folder.
- Screen readers announce the language pickers as "Audio language" (they read
  "Search" / "language").
- **The small models no longer drop minutes of speech.** A 30-second window was
  thrown away as "silence" when Whisper was both unsure it was speech and not
  very confident in its text. The bar for confidence was stricter than
  faster-whisper's default, so the tiny model (what a PC or VM without an NVIDIA
  GPU uses) left five 1-3 minute holes in a real 22-minute Russian meeting: 855
  words instead of 1452. The text it writes there now matches what large-v3
  hears; large-v3's own output is unchanged.
- The model list says "1 model available", not "1 models available", and the
  progress line no longer shows an internal file name while extracting audio.
- **No more gigabytes of leftovers from interrupted uploads.** Closing the window
  while a big video was being added left the partial copy in the app's data
  folder forever (only `minutenest-cli serve` cleaned up at start). The app now
  sweeps such leftovers, and scratch from crashed jobs, every time it starts.
- **A file that can't be saved fails its card with the reason** ("Could not save
  the file (No space left on device)...") instead of sitting at "Adding… 60%"
  forever with a partial file on disk.
- **A stopped or failed job stays stopped on screen.** A model request still
  waiting in the background (OpenRouter's free models retry for about two
  minutes) could replace "Cancelled."/"Failed." with "free models are busy,
  trying again", and a Retry started right away could see its bar jump ahead.
- **A GPU that fails to load is tried once, not before every job.** Each job used
  to try CUDA again, fail again and load a second CPU copy of the model.
- Adding the first file no longer freezes the window for about half a second.
- The model stays loaded after Stop (the 10-minute idle clock restarts), so
  Retry doesn't have to load it again.
- With an Ollama Cloud key still saved but the host switched back to this PC or
  a LAN box, requests go one at a time again instead of queueing three on the
  same GPU.
- **Free OpenRouter models no longer fail a job the moment they are busy.** The
  default free model failed a new user's very first job ("temporarily
  rate-limited upstream") in two packaged test runs while other free models
  answered; the app gave up after about 6 seconds. For a `:free` model,
  OpenRouter may now fall back to up to two other free models from its live
  catalog (never a paid one), and the app waits up to about two minutes before
  failing, showing the wait on the job card. The finished job says which model
  wrote the document when it was not the chosen one; the CLI reports it as
  `llm_models`.
- **Documents are written in the meeting's language.** A Russian meeting came
  back as an English test plan: the document prompts never named a language, so
  the answer depended on the model. The audio language (or, if unknown, "the
  transcript's own language") is now part of every document request.
- The log now records why the app stopped (Shut down button, or the window
  being closed), so "the app went away" can be told apart from a crash.
- A job and a model warm-up (or two jobs) asking for the same model at once
  loaded it twice -- double the VRAM. Loads now happen one at a time.
- **"No LLM is configured" right after the guide.** Finishing the guide sent
  two settings saves at once; the server saved by truncate-and-rewrite with no
  lock, so the second save could read a half-written file and write factory
  defaults back — wiping the LLM provider and key and resetting the Whisper
  model to large-v3 (reproduced 119 times in 200 tries). Saves are now locked
  and atomic, an unreadable settings file is kept as a backup instead of being
  replaced, and every settings change is written to the log.
- **The model box no longer appends your search to the old name**
  ("aion-labs/aion-2.0openrouter/fr") when the model list reloads while you type.
- **No more paid model picked for you.** With no model chosen, OpenRouter now
  preselects a free one (never the first name alphabetically, never the
  `openrouter/free` router), and what the box shows is what jobs use.
- **Switching provider shows the right provider's list.** A slow answer from the
  previous provider (e.g. "Could not reach LM Studio" after a misclick) can no
  longer land under the new one, and a model name no longer carries over to a
  provider that doesn't have it.
- **Every settings field confirms its save** ("Saved"), including model, language,
  profile, output and Visual effects; a failed save says "Not saved".
- **Visual effects moved to its own Appearance section** (it was in Output).
- **With no LLM selected, the model box is hidden** instead of saying "No models
  found. Check the host and key."
- **The open model list is no longer crossed by the guide's highlight.**
- **A job finishing while the page (re)connects is shown as finished** instead of
  staying stale until a reload.
- **Command line:**
  - usage errors exit with 1 as documented (argparse returned 2);
  - `settings set` accepts the dotted keys `settings get` shows
    (`llm.provider=ollama` was silently ignored) and rejects unknown keys and
    invalid values instead of reporting success;
  - every command prints a readable result without `--json` (`models status`,
    `settings get/set/reset` printed nothing);
  - `run` checks options, profile, output folder and the LLM *before*
    transcribing (a typo or a bad key used to fail after the whole
    transcription), and its errors are JSON too under `--json`;
  - `--prompt` alone now means `--task custom` (it was silently ignored);
  - progress lines show the time left.
- **Progress bar and ETA on short recordings and slow PCs.** Progress is now
  reported every segment instead of every 20 s, so a short job no longer ends
  while the bar sits at 8%. The time left is measured from the rate since
  transcription actually started (model loading no longer inflates it) and is
  hidden until a real rate exists.
- **A safety-classifier model can no longer "write" the document.** Models such
  as nemotron content-safety answer "User Safety: safe"; that output is now
  rejected, retried, and ends with a clear "pick another model" message while the
  transcript is kept. Classifier models are hidden from the OpenRouter list and
  transcript clean-up keeps the original text when the model returns a verdict.

### Improved
- **Transcription up to 3x faster, and no less accurate.** Two decoding settings
  were measured on read Russian speech (FLEURS) and a conversational Russian IT
  podcast with large-v3:
  - far less random re-decoding: Whisper used to re-decode every passage it was
    somewhat unsure of by random sampling ("temperature fallback", up to T=1.0).
    On the podcast 37% of the segments went through it, it took 1.7x as long and
    the error rate went *up* (WER 7.6% vs 7.2%). Now only a repetition loop or a
    very unsure passage is re-decoded (see "Fixed": repetition loops); with
    large-v3 that happened to no passage of the podcast;
  - beam 5 instead of 10: 1.35-1.9x faster, same or lower error rate
    (FLEURS 3.57% vs 3.64%, podcast 6.95% vs 7.20%).

  Together: the podcast transcribes 3.3x faster (237 s -> 72 s for 12 minutes on
  an RTX 5060 Laptop) with WER 6.95% instead of 7.57%.
- **Adding a big recording is instant.** The card appears at once ("Loading… N%")
  and the file streams straight into the job folder. Before, a 1-2 GB video was
  written to disk twice (temp spool, then a copy) inside a request handler that
  blocked the server, so live updates and every button stalled until it was done,
  and the card only showed up at the end. A card can be cancelled while adding.
- **The model loads while you set up the job.** Adding a file starts loading its
  Whisper model in the background, so Start no longer waits for it. Warm-up never
  downloads: a missing model is still fetched by the job, visibly.
- **VRAM is given back when idle.** A model now leaves GPU memory after 10 minutes
  without work (it used to stay until the app closed: ~3 GB for large-v3, while
  you game). A model loaded with the Load button stays until you unload it.
- **Cloud LLMs get their requests side by side.** The cleanup pieces and parts of
  long documents are independent, so OpenRouter/OpenAI/Anthropic/Ollama Cloud now
  get up to 3 at once (one 22-min meeting spent 131 s on three requests sent one
  by one). A model on this PC (Ollama, LM Studio) still gets one at a time: parallel
  requests there would only queue on the same GPU. The time-left estimate knows.
- **Audio extraction about 2x faster:** the recording is downmixed to 16 kHz mono
  before the loudness filter instead of after (20 s -> 9 s for a 10-minute
  recording, 40 s -> 22 s for 22 minutes).
- **Snappier on low-end PCs / VMs:** a "Visual effects" setting (Auto turns on a
  light mode with no shadows or animations on PCs with 4 or fewer cores), Whisper
  uses all cores but one on CPU, and jobs run at below-normal priority so the
  window stays responsive.
- Clicking a language or model field clears it so you can type a search straight
  away; the current value shows as a placeholder.
- Removed the "Changes apply instantly" note; only the green "Saved" popup stays.

### Changed
- **A PC without an NVIDIA GPU starts with the `small` model on the CPU.** A new
  install used to default to large-v3 on CUDA everywhere, so the guide's first
  step offered a 3 GB model that then ran on the CPU at ~0.8x the recording's
  length on a 16-thread laptop (hours on a small VM); `small` takes ~0.16x.
  PCs with a GPU still start with large-v3.
- **New tagline: "Every meeting, written down."** The old "recordings →
  documents" line is gone from the UI, README, CLI help and package metadata.
- **One progress bar and one time-left estimate for the whole job.** The bar no
  longer restarts for each step, the LLM stage gets its own share of it (it used
  to be squeezed into the last 10%), and the ETA is the time left for the entire
  job (remaining transcription plus the model work still to come) instead of a
  per-step guess.
- **Type-to-search dropdowns** for the audio language (job card and Settings) and
  the LLM model, which can hold 400+ OpenRouter entries. Typing filters in real
  time, best match first; a model name that isn't in the list can be typed in.
- **The Settings close button floats beside the dialog** instead of sitting in the
  scrolling panel (it tucks into the corner on narrow windows).
- **The Windows title bar matches the app** (dark, same colour as the page)
  instead of staying white.
- **The guide's LLM step** now talks about picking an LLM and an API key, not
  an "account".
- **Settings apply instantly.** The Save and Close buttons are gone: every
  control saves itself (dropdowns at once, text and key fields after a short
  pause) and a small "Saved" tick confirms it. Close with the X in the top
  right, Esc, or by clicking outside. Selecting text in a field and releasing
  the mouse outside the dialog no longer closes it.

### Fixed
- **The LLM stage could hang forever, and Stop hung with it.** The whole
  transcript went out as one request with no timeout. It is now split into
  small requests, each with a deadline ("The model did not answer within ...")
  and each interruptible, so Stop takes effect within a second or two. Long
  texts are summarised in parts and merged. A chunk the model refuses or leaves
  empty (garbled audio) keeps its original text instead of failing the job.
- **Float16 error after switching to CPU.** CPU can't run float16; switching the
  device now moves the compute type to int8 (and greys out the GPU-only
  options), the server never stores an invalid pair, and loading falls back
  safely.
- **A model showed "not installed" even though it was in the folder.** Models
  downloaded or loaded by a job never told the UI. Every change now reaches the
  open window (status badge, the pill in the header, and the stale size).
- **Ollama showed a usage-limit message, then a 402 error after Refresh.** Both
  mean the key works: the model list loads with a warning, and the Refresh
  button asks the provider again instead of replaying a cached answer.
- **Adding a job no longer leaves folders behind.** Uploads are single files, the
  results folder is created only when there is something to put in it, and
  temp audio is removed when a job ends. Unfinished or failed jobs can be
  removed (and Delete + files) from the card. Leftovers from older versions are
  swept at startup.
- **Every LLM provider reported "rate limit" even with fresh, valid keys.**
  The key check was a 1-token chat request, so a busy shared free model
  (OpenRouter `:free`) or used-up Ollama Cloud quota looked like a bad key and
  blocked the model list and any LLM job. OpenRouter keys are now checked with
  the quota-free `/auth/key` endpoint; a 429 anywhere is treated as "key is
  fine, provider is busy" (a warning, never a failure), and real requests retry
  with backoff, honouring `Retry-After`.
- **Models that answer with empty content crashed with a `NoneType` error.**
  This is now a clear "returned an empty answer, pick another model" message.
- **The guide's sample job ignored the profile and task chosen in Settings.**
  It now uses them (falling back to `raw` only when the task needs an LLM that
  isn't configured). The sample stays English, and its card shows that.

### Added
- **Browser end-to-end test layer (`tests/e2e/`).** The suite now boots the
  real app and drives the real UI in a real browser (Playwright, Edge/Chrome/
  Chromium) and asserts what a user can see: the guide highlight's geometry
  after scrolls and async updates, button size/label stability while a click
  is in flight, dialog stacking, click-through under the guide shades, and
  zero uncaught JS errors. Failures save screenshots to `tests/artifacts/`.
  The old source-grep tests that passed while the UI was visibly broken have
  been deleted; only the native-window assertions remain source-level (a
  WebView2 window cannot be driven in CI).

### Added
- **Per-job document format.** Staged cards (and Settings) let you pick
  Markdown, plain text, or both for the formatted document.
- **Debug log for handover.** The backend writes one rotating file
  (`data/logs/meetingnotes.log`, `.local/logs/` in dev) covering startup,
  environment, window strategy, device selection and CPU fallbacks, every
  job's lifecycle, pipeline stages and full error tracebacks. Open it from
  Help → Troubleshooting ("Open log folder"); the UI also reports uncaught
  errors into it. `MEETING_NOTES_DEBUG=1` adds verbose detail.
- Per-job audio-language selector on staged cards, next to profile, output
  and model. Blank means "use the saved default".
- Every job now writes a Markdown document: LLM tasks as before, and `raw`
  jobs as a timestamped transcript (`<name>.raw.md`) so the output shape is
  the same regardless of task.

### Fixed
- **The wizard could wipe saved LLM settings ("No LLM is configured" while the
  UI showed a provider).** The walkthrough opens before the settings form has
  been filled from the server; leaving a step saved the still-empty form over
  the saved config — provider "none", blank host, blank API key. Saving now
  waits for the form to load (`settingsLoaded`), and the init sequence no
  longer runs a save before `loadSettings()`. Reproduced from a real VM log and
  covered by an e2e test.
- **Empty/garbage pipeline settings were persisted.** A fast-clicked wizard
  saved `model_size: ""`, after which every job silently fell back to large-v3
  ("the model failed, I had to update models to get it working again"). The
  server now rejects invalid `model_size`/`device`/`compute_type`/
  `output_format` (keeping the previous values), the settings form populates
  the model dropdown before assigning its value, and the client never posts an
  empty model size.
- **The guide highlight did not re-fit after a window resize.** Shrinking the
  window left the ring around only the card's clipped slice (measured 145px
  short of the card's bottom). A resize now clears the step's fit-scroll and
  re-places immediately.
- **The hello pane was only partly highlighted at "add a recording".** The
  hole covered the dropzone and the sample button but left the strip below the
  buttons dimmed; step 4 now highlights the whole hello pane (dropzone
  included, so files still drop straight in).
- **Step 6 made users press Next for nothing.** While a job ran, step 6
  ("when it finishes") asked for a pointless click and Back from it landed on
  stale copy. It is gone: step 5 is final ("Finish"), and its running/done copy
  already explains where the files land.
- **Deleting a job right after Open folder crashed the endpoint (HTTP 500).**
  Explorer's hold on the folder made `rmtree` raise `WinError 32`; deletion now
  retries briefly and degrades gracefully, leaving the job in place instead of
  failing the request (real traceback in a VM log).
- **Guide geometry at every window size.** The tip used to land on top of the
  settings modal at mid/narrow window sizes, hiding the very card the step
  explains; it now shrinks the modal symmetrically, and when the window is too
  narrow for two columns it stacks under the modal with the modal reserving
  the strip (the tip's text scrolls, its buttons stay pinned). The guide was
  verified at 8 window sizes from 800x500 to 1920x1080.
- **Settings rows misaligned.** A two-line label (the audio-language hint)
  pushed its own select ~20px below its neighbor's in the same row. Labels are
  one line now and the long explanation moved to a tooltip.
- **Guide buttons wrapped to two lines** in narrow tips ("Skip setup"
  stacking inside its own button); the action row keeps every label on one
  line.
- **The dark scrollbar.** The settings modal (and the page) showed the
  browser's default light scrollbar, which read as a foreign element; both are
  now styled to the app palette.
- **The guide highlight went off screen whenever the target was partly
  scrolled out.** The ring was built from the target's raw layout rect, so it
  painted over the dimmed page below the settings modal (and hung off the card
  after a scroll — placement used rects measured BEFORE the scroll). The
  spotlight now covers the target's *visible* slice, clamped to the container
  it lives in, and is re-measured right after scrolling.
- **The LLM card was not fully highlighted.** The card grows when the async
  model list/hint text arrives, but the ring was measured once and never
  followed the new size. A ResizeObserver on the highlighted nodes re-places
  the ring (and re-fits a card that outgrew the modal) whenever a target
  changes size.
- **"Open folder" flickered on the first click.** The in-flight feedback
  replaced the button's whole `innerHTML` with plain text, so for one frame the
  icon vanished and the button shrank ~30px. Only the label span changes now,
  and the width is pinned while the request is in flight.
- **Wizard: closing Settings mid-step left the guide stuck and swallowed
  clicks.** The dimming shades captured pointer events, so anything outside
  the spotlight was dead until the guide retargeted ("buttons take forever");
  they no longer intercept clicks, and closing/opening Settings retargets the
  coach-mark immediately instead of waiting for the next poll tick.
- **Sluggish buttons on VMs.** The guide's 700 ms poll re-measured every
  target (forcing a layout) forever; placement is now event-driven. Job-list
  refreshes are coalesced so SSE bursts cannot stack full rebuilds behind a
  click, and Open folder / Remove / Delete acknowledge the click immediately
  instead of sitting silent while Explorer cold-starts.
- **Settings card order now matches the walkthrough**: Model → Output
  (language/profile/format/save folder) → LLM. The Save folder row spans the
  full width, and the language hint is one plain line instead of a paragraph.
- **Empty transcript / LLM refusal written as the document.** A garbled
  recording used to run the LLM anyway; when it refused ("no cleaned transcript
  was included") that refusal was saved as `<name>.testplan.md`. Refusal-shaped
  replies are now rejected, and the failure message says where the transcript
  was saved. The transcript and segments are written **before** the LLM stage,
  so a failed job no longer leaves an empty folder — and a failed job with
  partial output offers "Open folder" on its card.
- **Two confusing transcript files.** On profiles with no glossary (e.g.
  `generic`) `<name>.raw.txt` and `<name>.txt` were byte-identical duplicates.
  The `.raw.txt` is now written only when glossary correction actually changed
  something.
- **Window content lagged the window for seconds after maximize.** WebView2's
  bounds assignment is asynchronous; it is now re-asserted until it sticks,
  and WM_DPICHANGED is handled when moving between monitors.
- **Wizard coach-marks: tips stranded at the window edge, settings modal
  shoved sideways, whole UI jumping on scroll.** The walkthrough is rebuilt:
  the settings modal never moves (it used to shift left by a reserved column,
  which read as the UI "shifting"), the tip docks beside the modal's real
  on-screen rect and shrinks or docks bottom-center when the window is narrow,
  the Ollama step highlights the entire LLM card (host and API-key fields
  included, not just the two upper selects), and scrolling only ever moves the
  nearest scroll container — never the page. A step keeps the tip on one side
  once chosen, so it no longer hops between corners mid-scroll.
- **The prompt editor was unreachable.** Staged cards only re-rendered when
  the job *status* changed, so switching a job's output to an LLM task
  (which is what shows the prompt box) left the old card in place — the
  editor never appeared. Cards now rebuild when any staged setting changes,
  and the prompt ships open whenever a prompt is already set.
- **Content was not centered in the window.** The main column spanned the
  full width and centered inside the area right of the 64px rail, pushing
  cards 32px off center; the layout now reserves the rail width so content
  centers on the real window.
- **Settings modal rendered 440px narrow.** The base `.modal` max-width
  overrode `.modal-large` later in the stylesheet; the large variant now
  out-specifies it and takes its full 760px (or a symmetric fit when the
  wizard needs a tip column).
- **The window could be larger than the screen.** The process was
  DPI-unaware, so on a scaled display (125%/150%, common on VMs) the
  1180x800 window request became ~1475x1000 physical — taller than a
  1536x960 screen, with the title bar off-desktop. The window now declares
  per-monitor DPI awareness and clamps itself to the monitor work area.
- **"Auto-detect works too" was false.** The pipeline always uses a language
  code; the settings copy and the wizard's step 2 now say so plainly.
- **Fully self-contained: nothing written outside the app folder.** The CUDA
  runtime was caching ~200 MB of JIT-compiled kernels in
  `%APPDATA%\NVIDIA\ComputeCache`; that is now redirected into
  `data/cuda-cache/` beside the exe, so deleting the app folder really
  removes everything. Verified with an isolated run (fake user profile,
  APPDATA and LOCALAPPDATA): a real transcription leaves the user dirs
  untouched.
- **CPU fallback was invisible and misreported.** A run that fell back to CPU
  still printed `[cuda/...]`, and the raw DLL error flashed in the progress
  bar. The real device is now reported, a no-GPU pre-check switches to CPU
  silently before any load is attempted, and the user-facing message is a
  plain "running on CPU (int8)" — the raw error goes to the debug log.
- **Ollama cloud without a key.** A remote host and no key used to hit the
  native daemon API shape and fail confusingly; it now fails fast with a
  clear message, and cloud model listing uses the OpenAI-compatible endpoint
  instead of `/api/tags`.
- **Wizard LLM step covered the fields it described.** The settings modal now
  shifts left, reserving a column where the tip docks; the spotlight follows
  the target. Long settings steps also scroll instantly (the smooth-scroll
  animation raced the highlight placement).
- **Wizard duplicated its instructions.** Step 4 → 5 flow rewritten: step 4
  auto-advances once a recording is added instead of repeating "there's
  already a job", and step 5 explains the job card's per-file controls.
- **UI lag on VMs.** Job-list rendering now updates only changed cards (a
  full rebuild detached the node under the pointer, which is what made
  buttons feel like they needed a second click), the wizard re-places its
  highlight at most once per animation frame, and the infinite pulse
  animation, layout-thrashing progress sweep and heavy outer glows are gone.
- **Closing the browser tab left a python process running.** In browser-
  fallback mode there was no window to watch, so the server lived on. The UI
  now reports liveness (SSE + a `pagehide` beacon) and the launcher stops the
  server once the last tab is gone.
- **Confirm dialogs opened during the walkthrough rendered underneath it**
  (dimmed and unclickable). The modal layer now sits above every wizard layer.
- **The walkthrough appeared late.** It is shown as soon as settings load
  instead of after several sequential round-trips.
- **VM rendering: guide appeared late, buttons needed two clicks, white bands
  on maximize.** Chromium's occlusion/background throttling and GPU
  compositing behave badly under virtualized graphics; the window host
  disables them, re-asserts the WebView bounds on geometry changes, and moves
  focus into the page on window activation.
- **Scrolling over the wizard tip did nothing.** Wheel events on the tip are
  forwarded to the settings pane underneath.
- Wizard step titles update live during the polling tick, not just on entry.

## [0.3.0] - 2026-09

### Added
- Interactive first-launch walkthrough (coach-marks over the real UI:
  pick a model and language, add a recording, press Start). Six reactive,
  skippable steps, including the optional LLM setup. Reopenable from Help.
- **Stop button on running jobs**: abort a queued or running transcription;
  the job lands in a new `cancelled` state and can be retried.
- Help overlay in the rail: walkthrough, folder layout, output type
  reference, quitting notes.
- Per-job model picker on staged cards; profiles can define a default
  model (e.g. a quick "tiny" profile).
- "Try with a sample" button: stages a short built-in recording (English
  speech) so a new user always has something to transcribe.
- CPU fallback: a CUDA failure now switches to CPU + int8 with a clear
  progress message instead of erroring out; Device dropdown offers cpu.
- Post-build smoke gate (`scripts/smoke_release.py`) that unpacks the zip
  and verifies the packaged app like a fresh user would.
- Native window via WebView2 COM (`meetingnotes/native/`), no .NET
  runtime needed; the previous pythonnet-based window could fail on clean
  machines. Fallback chain: WebView2 → Edge app window → browser.

### Fixed
- **Model picked in the wizard was ignored.** Choosing `tiny` in Settings and
  never pressing Save meant jobs still ran the old `large-v3` default. Model,
  language, profile and output dropdowns now save immediately, and the staged
  card's "default" option spells out the model it will actually use.
- **WebView2 window rendered a blank white page.** The controller starts
  invisible and was never made visible, so the window opened but painted
  nothing. Fixed (`controller.IsVisible = True`) and guarded by a render
  check in the release verification: the packaged window's pixels are
  sampled and a blank page fails the gate.
- **Cloud LLM calls failed on clean machines** with
  `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`: Python
  trusted only the system store, which can be missing root CAs. All providers
  now use the bundled certifi CA list, and TLS failures get a plain-language
  message. The smoke gate fails the build if certifi is missing.
- **Dialogs appeared only after a stray click** on VMs: Windows native
  occlusion detection paused rendering in the WebView2 window, leaving stale
  frames. Occlusion detection is now disabled, and the modal backdrop no
  longer uses `backdrop-filter` (which stalls compositing with software
  rendering).
- **ffmpeg is bundled.** The packaged app shipped without ffmpeg/ffprobe,
  so a clean machine could not transcribe anything. Binaries now ship in
  the zip and resolve from there; PATH is only a dev fallback.
- **Sample button was unreachable in the wizard.** The "add a recording"
  step now spotlights the dropzone *and* the sample button together.
- **Wizard highlight drifted and steps were skipped.** Scroll is locked on
  target-less steps, highlights re-place on scroll, and the Start step targets
  the running card (with adaptive text) instead of a Start button that may be
  gone, and no orphaned tooltip is left in the bottom corner.
- Edge app-mode window now stops the server when its window closes
  (previously the app could linger as a background process).
- Closing the native window stops everything; no zombie process (verified).
- Language dropdown sorts by display name (Tibetan no longer sits between
  Bengali and Breton).
- "Transcription language" renamed to "Audio language" with an
  explanation: it steers the model, it isn't the output language.
- Guide's "where everything lives" slide removed; that information now
  lives in Help where it belongs.

### Changed
- README rewritten in plain language, with a before/after correction
  example; development instructions moved to CONTRIBUTING.md.
- Output dropdown shows a short description per option.

## [0.2.0] - 2026-09

### Added
- Headless CLI (`meetingnotes-cli`) with full app parity: run, profiles,
  model management, settings, serve: all with `--json` output and stable
  exit codes (0/1/2) for agents and pipelines.
- Second exe target: `meetingnotes-cli.exe` (console build).
- **None (raw transcripts only)** LLM provider: the app works with zero
  configuration; this is the fresh-install default.
- **LM Studio / OpenAI-compatible** provider (vLLM, llama.cpp, LocalAI) with
  configurable URL; blank key = local server, key = hosted.
- Domain dictionaries in 6 languages (RU/EN/ES/DE/FR/PT), auto-selected by
  transcription language; language-aware profile loading with EN fallback.
- Whisper model management from the UI: download with progress, preload,
  unload (free VRAM), delete, open model cache folder.
- ETA estimate in transcription progress (self-correcting average-speed
  projection with chunk position).
- Word Error Rate evaluation (`meetingnotes.eval`, `meetingnotes/wer.py`).
- Jobs persist across restarts; per-job delete with/without files
  (marker-guarded safe deletion).
- Results saved to `Downloads\meetingnotes\<filename>\`; "Open folder"
  reveals them in Explorer with the file selected.
- Fail-fast LLM credential check before transcription; friendly 401/429
  error messages.

### Fixed
- SSE endpoint made async: enough browser tabs used to starve the thread
  pool and hang the whole app ("stuck loading forever").
- Explorer "Open folder" on paths with spaces (raw command line, DEVNULL
  handles for the windowed exe).
- File-picker chooser stacking on repeated dropzone clicks.
- LLM model dropdown no longer resets to the first option on refresh.
- CUDA DLL discovery inside the PyInstaller bundle (`cublas64_12.dll`).
- Console window popups from ffmpeg subprocesses in the windowed exe.

## [0.1.0] - 2026-08
- Initial release: FastAPI UI, faster-whisper transcription, per-domain
  glossary correction, two-phase LLM cleanup, PyInstaller packaging.