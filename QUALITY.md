# Quality: the standards MinuteNest is checked against

MinuteNest is checked against four published standards. This page says how
each one applies to a Python and JavaScript desktop app, how it is checked, and
where the app deviates (with the reason).

| Standard | What it is | How it applies here |
|---|---|---|
| NASA/JPL *The Power of Ten: Rules for Developing Safety-Critical Code* (G. J. Holzmann, IEEE Computer 39(6), 2006) | 10 checkable coding rules, written for C | adapted rule by rule below; checked by ruff, pyright, ESLint and `scripts/check_power_of_ten.py` |
| ISO/IEC 25010:2023, product quality model | 9 quality characteristics | each characteristic traced to the tests and checks that show it |
| ISO/IEC 5055:2021, automated source code quality measures (CISQ) | code weaknesses (CWE) that hurt security, reliability, performance efficiency and maintainability | ruff (with its bandit port, `S`), bandit and pyright, zero findings |
| ISO/IEC/IEEE 29119-3:2021, test documentation | test plan, test case specification, test completion report | `tests/TEST_PLAN.md`, the tests themselves, the release report |

## How to run the checks

```bash
python -m ruff check minutenest tests scripts launcher.py cli_entry.py
```

```bash
python -m pyright
```

```bash
python -m bandit -c pyproject.toml -r minutenest scripts launcher.py cli_entry.py
```

```bash
python scripts/check_power_of_ten.py
```

```bash
npx --yes eslint@9.39.1 minutenest/webapp/static
```

```bash
python -m pytest
```

Settings: `pyproject.toml` (`[tool.ruff]`, `[tool.pyright]`, `[tool.bandit]`) and
`eslint.config.mjs`. Every check passes with **zero findings**; each rule turned
off, and each line-level suppression, carries its reason next to it.

## Power of Ten, rule by rule

| # | Rule (Holzmann) | Here | Checked by | Status |
|---|---|---|---|---|
| 1 | Simple control flow: no goto, setjmp/longjmp or recursion | No recursion, direct or mutual; cyclomatic complexity at most 15 per function; no labels in JavaScript | `check_power_of_ten.py`; ruff `C901` (15), pylint size limits; ESLint `complexity` 15, `max-depth` 4, `no-labels` | met |
| 2 | Every loop has a fixed upper bound | Loops run over finite collections, or carry an explicit limit: stream deadlines (`llm/stream.py` first-byte / silence / total limits), `MAX_MERGE_ROUNDS`, `MAX_MODEL_PAGES`, `FFMPEG_MAX_S`, `MODEL_DOWNLOAD_MAX_S`, retry counts (`TRIES`, `ROUTER_TRIES`) | review; tests D19–D22 prove the stream limits fire | met, with documented exceptions (below) |
| 3 | No dynamic memory allocation after initialisation | Not possible in a garbage-collected language. Adapted: **bounded memory** — every buffer that grows with outside input has a cap: streamed answers (`max_chars` 200 000, `MAX_LINE` 1 MiB, `MAX_BACKLOG` 4096 lines), live-update queues (500 events), uploads written in fixed blocks | review; test D21 (runaway answer cut) | adapted |
| 4 | No function longer than ~60 lines | At most 60 lines from `def` to the last line, docstring and comments included (stricter than "lines of code") | `check_power_of_ten.py`; ESLint `max-lines-per-function` 60 | met |
| 5 | At least two assertions per function | Python `assert` is stripped by `-O` and must not guard anything at run time. Adapted: invariants and inputs are checked with explicit errors where they enter: settings values against their allowed choices, every CLI option before any work, server addresses (http/https only), "only staged jobs can start/change", a finished job must have its results folder. Tests carry the assertions (one behaviour each). | review; tests G2, G4, I2, the D-series error cases | adapted (no per-function count) |
| 6 | Data declared at the smallest possible scope | Process-wide state (one server, one job worker, one live-update loop, the model cache) lives at module scope, written under a lock; everything else is local. JavaScript: `const` by default, no `var`, no shadowing | ruff `PLW0603` (turned off with its reason), ESLint `no-var`, `prefer-const`, `no-shadow` | met |
| 7 | Check every return value and every parameter | No blind or empty `except` / `catch`; each broad catch names why it is broad and logs or reports; every `fetch` answer in the UI is checked (`r.ok`) and a refusal is shown to the user | ruff `BLE`, `TRY`, `S110`; ESLint `no-empty`, `no-unused-vars`; bandit | met |
| 8 | Limited use of the preprocessor | No preprocessor in Python/JavaScript. Adapted: no generated or dynamic code — no `eval`/`exec`, no shell strings, no monkeypatching in the app | ruff `S102`/`S307`, bandit `B307`/`B602` | met |
| 9 | Restricted pointer use | No pointers in Python/JavaScript, except the Win32 calls of the native window (`webview2win.py`, `launcher.py`). There every foreign function has declared argument and result types (`_declare_signatures`) and no pointer arithmetic is done | review; pyright | met |
| 10 | All warnings on; static analysers run with zero warnings | ruff with the rule sets above, pyright (standard mode), bandit, ESLint: zero findings; the full user-level test suite | the commands above | met |

### Deviations

- **Service loops (rule 2).** The job worker, the live-update stream per open window
  and the web server run for as long as the app runs. Holzmann allows exactly this
  for "a process scheduler" when the non-termination is stated; each such loop says
  so in a comment and ends when the app quits.
- **Rules 3, 5, 8, 9** are adapted as described above because the C mechanisms they
  restrict don't exist in Python or JavaScript.
- **Line-level suppressions**, each with its reason in the line:
  - `BLE001` (12): broad catches at a boundary that must not take the app down: a UI
    log write, a status check that falls back to "can't tell", the CLI turning any
    processing failure into exit code 2.
  - `S603`/`B603`/`B404` (subprocess, 4+4+3): argument lists, never a shell; the
    programs are ffmpeg/ffprobe from the app's own folder, Explorer and Edge.
  - `S310`/`B310` (2+2): `urllib` opens only addresses checked to be http(s) the line
    before (`net.py`; test D29 shows a `file://` address refused).
  - `ARG001`/`ARG002` (8): callbacks whose signature is fixed by the caller.
  - `RUF012`, `PLR0913`, `RUF001`, `PLW0108`: ctypes structure fields, the pipeline's
    option set, Cyrillic text that is data, a lambda kept for late binding.

## ISO/IEC 25010:2023, characteristic by characteristic

| Characteristic | What shows it |
|---|---|
| Functional suitability (completeness, correctness, appropriateness) | `tests/TEST_PLAN.md` A–I: real Whisper and ffmpeg on real recordings; expected values taken from the requirements (the words spoken, the files promised in AGENTS.md), never from the code under test |
| Performance efficiency (time behaviour, resource use, capacity) | CPU int8 and `small` without a GPU (A3); one core left free for the window; streamed LLM answers with time limits (D19–D21); progress and time left shown honestly (D23, D24) |
| Compatibility (co-existence, interoperability) | one instance per machine without touching other processes (F5, F6); OpenAI, Anthropic, OpenRouter, Ollama and LM Studio spoken to as their APIs require — stand-ins that enforce each vendor's documented rules (D9, D10, D27) plus real wrong-key checks against the real services (D11) |
| Interaction capability (learnability, operability, user error protection, inclusivity) | the first-start guide (A1–A5); what each option does, under it (B8); layout at 800×600 to 1366×768 (H1–H6); screen-reader names for every control; users leaving the happy path (I1–I11) |
| Reliability (faultlessness, availability, fault tolerance, recoverability) | LLM retries and limits (D5, D16–D27); a failed GPU load falls back to the CPU; a job interrupted by a quit comes back as interrupted (I9); transcripts are written before the LLM step and kept on any failure (D4, D22); partial uploads cleaned up (F2) |
| Security (confidentiality, integrity, authenticity, resistance) | zero ISO/IEC 5055 security findings (below); API keys write-only from the CLI and never written to the log, only whether one was set (G4); the window's server listens on 127.0.0.1 only (`serve --host` listens elsewhere only when asked); downloaded model revisions pinned; only http(s) server addresses accepted |
| Maintainability (modularity, reusability, analysability, modifiability, testability) | function size and complexity limits (rules 1, 4); type-checked (pyright); every LLM provider behind one interface (`llm/base.py`); one test per behaviour, each traced to an ID in the test plan |
| Flexibility (adaptability, installability, replaceability) | portable folder (unzip and run), `MINUTENEST_APP_DIR`, provider base URLs and LLM time limits from the environment (AGENTS.md); the CLI and the window share settings and models |
| Safety (operational constraint, risk identification, fail safe, hazard warning) | "Delete + files" removes only folders the app marked as its own (C10); Stop keeps the transcripts already written and removes a folder left empty; a job cut off by Quit comes back marked interrupted (I9); a recording in another language than set is pointed out (warning on the card and in the CLI JSON) |

## ISO/IEC 5055: code weaknesses (CWE)

The complete CISQ measure set needs a certified analyser. What the tools above
check, with **zero findings**, covers the weaknesses that matter for this app:

| CWE | Weakness | How it is avoided |
|---|---|---|
| CWE-78 | OS command injection | subprocess with argument lists only, never `shell=True` |
| CWE-22 | Path traversal | uploaded file names lose any folder part (`_safe_name`); results go to a fresh folder per job |
| CWE-73 / CWE-918 | External control of file name or URL | server addresses must be http(s) (`net.py`), not `file://` |
| CWE-295 | Improper certificate validation | TLS verification always on (certifi bundle in the packaged app) |
| CWE-327 | Broken cryptography | the one hash (a window class name) is declared not-for-security |
| CWE-400 / CWE-770 | Uncontrolled resource consumption | bounded buffers and time limits (rules 2 and 3) |
| CWE-703 / CWE-391 | Unchecked errors | no blind or empty except/catch (rule 7) |
| CWE-494 | Download without integrity check | model downloads pinned to a revision |
| CWE-352 | Cross-site request forgery | API requests a browser marks as coming from another page are refused (`Sec-Fetch-Site`; foreign `Origin` on state-changing methods), tests J1-J2 |
| CWE-350 / CWE-346 | Trusting a host name (DNS rebinding) | only `localhost`, IP addresses and the PC's own name are answered, test J3 |
| CWE-522 | Credentials sent to an untrusted party | a saved Ollama/LM Studio key goes only to the saved address, test D32 |

## ISO/IEC/IEEE 29119-3: test documentation

- **Test plan:** `tests/TEST_PLAN.md`: why the suite looks the way it does, its
  principles, the environments, the test data and its licences, and risk-based
  coverage items (A–I) with IDs and priorities. It also lists what is not
  automated (a manual release checklist) and every bug the suite found, with the
  test that now covers it.
- **Test case specification:** each test is one behaviour, named as the rule it
  checks, with its ID in the plan; user steps live in `tests/support/ui.py`.
- **Test completion report:** the release report: suite results on the source and
  on the packaged app (`pytest --app exe`), plus the hand-run checklist.
