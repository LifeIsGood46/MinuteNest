"""MinuteNest CLI — full-featured headless interface.

Parity with the web app: run, profiles/tasks, whisper model management,
settings get/set, and the web UI. Every command supports ``--json`` for
machine-readable output (agents / pipelines), and exit codes are stable:

    0  success
    1  usage / input error (bad flags, unknown key or value, missing file,
       no LLM configured for a formatted output) -- nothing was processed
    2  processing failure (transcription, LLM, network...)

Everything that can be checked up front is checked before any work starts:
a typo in a flag must not cost a long transcription.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, config, names
from .pipeline import PipelineOptions, run_pipeline
from .polish import available_tasks, task_default_prompts
from .profiles import list_profiles
from .settings import AppSettings, LLMSettings, load_settings, update_settings

PROVIDERS = ["none", "ollama", "compat", "anthropic", "openai", "openrouter"]
DEVICES = ["cuda", "cpu"]
COMPUTE_TYPES = ["float16", "int8", "int8_float16", "float32"]
OUTPUT_FORMATS = ["md", "txt", "both"]
SECRET_KEYS = {"ollama_api_key", "compat_api_key", "anthropic_api_key",
               "openai_api_key", "openrouter_api_key"}
MIN_CHUNK_S = 30


def _print(msg: str) -> None:
    print(msg, flush=True)


def _wants_json(args: argparse.Namespace | None = None) -> bool:
    if args is not None:
        return bool(getattr(args, "json", False))
    return "--json" in sys.argv[1:]


def _fail(args: argparse.Namespace | None, code: int, msg: str, hint: str = "") -> int:
    """Report an error: always on stderr; with --json also as JSON on stdout,
    so a script parsing stdout never gets an empty string."""
    print(f"error: {msg}", file=sys.stderr)
    if hint:
        print(f"hint: {hint}", file=sys.stderr)
    if _wants_json(args):
        payload = {"error": msg, "exit_code": code}
        if hint:
            payload["hint"] = hint
        print(json.dumps(payload, ensure_ascii=False))
    return code


def _emit_json(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _known_models() -> list[str]:
    from . import models as model_mgr

    return model_mgr.KNOWN_MODELS


def _language_codes() -> list[str]:
    from .transcribe import LANGUAGE_NAMES

    return sorted(LANGUAGE_NAMES)


def _profile_names() -> list[str]:
    return sorted(p.name for p in list_profiles())


def _tasks() -> list[str]:
    return ["raw", *available_tasks(), "custom"]


def _fmt_eta(seconds: float, scope: str = "job") -> str:
    """'about 3 min left'; with scope "transcript", the LLM step comes after it."""
    s = round(seconds)
    if s < 60:
        left = "under a minute"
    elif s < 3600:
        left = f"about {s // 60} min"
    else:
        left = f"about {s // 3600} h {s // 60 % 60:02d} min"
    return f"{left} until the transcript, then the document" if scope == "transcript" else f"{left} left"


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def _llm_hint(task: str) -> str:
    return (f"--task {task} writes a document with an LLM. Pass --llm (and --llm-model), "
            "or save one: minutenest-cli settings set llm.provider=ollama llm.model=gemma4:31b "
            "(see AGENTS.md). Use --task raw for a transcript without an LLM.")


def _run_options(args: argparse.Namespace, settings: AppSettings) -> PipelineOptions:
    """Settings provide the defaults (same source of truth as the UI); flags override."""
    task = args.task or ("custom" if args.prompt else settings.default_task)
    opts = PipelineOptions(
        profile=args.profile or settings.default_profile,
        task=task,
        custom_prompt=args.prompt or "",
        llm_provider=args.llm,
        llm_model=args.llm_model,
        model_size=args.model or settings.model_size,
        device=args.device or settings.device,
        compute_type=args.compute_type or settings.compute_type,
        language=args.language or settings.language,
        document_language=args.document_language or settings.document_language or None,
        output_format=args.output_format or settings.output_format,
        normalize_audio=not args.no_normalize,
        chunk_length_s=args.chunk_length if args.chunk_length is not None else 600,
    )
    # LLM settings default to the persisted provider unless --llm overrides.
    if opts.llm_provider is None and opts.task != "raw":
        opts.llm_provider = settings.llm.provider
    return opts


def _option_problem(args: argparse.Namespace, opts: PipelineOptions) -> tuple[str, str] | None:
    """(error, hint) for the first invalid option, before any work is done."""
    if args.prompt and opts.task != "custom":
        return f"--prompt only applies to --task custom (got --task {opts.task})", "drop --task, or use --task custom"
    if opts.task == "custom" and not (args.prompt or "").strip():
        return "--task custom needs --prompt \"what to write\"", ""
    checks = [
        ("--model", opts.model_size, _known_models()),
        ("--device", opts.device, DEVICES),
        ("--compute-type", opts.compute_type, COMPUTE_TYPES),
        ("--language", opts.language, _language_codes()),
        ("--document-language", opts.document_language or opts.language, _language_codes()),
        ("--profile", opts.profile, _profile_names()),
    ]
    for flag, value, allowed in checks:
        if value not in allowed:
            shown = ", ".join(allowed) if len(allowed) <= 20 else ", ".join(allowed[:20]) + ", ..."
            return f"{flag} {value!r} is not valid (choose from: {shown})", ""
    if opts.chunk_length_s < MIN_CHUNK_S:
        return f"--chunk-length {opts.chunk_length_s} is too short (at least {MIN_CHUNK_S} seconds)", ""
    if opts.task != "raw" and (opts.llm_provider or "none") == "none":
        return f"no LLM is configured for --task {opts.task}", _llm_hint(opts.task)
    return None


def _llm_problem(args: argparse.Namespace, opts: PipelineOptions) -> str:
    """Same fail-fast check as the app: a bad key or an unreachable server must not
    cost a long transcription first. '' when the LLM answers (or is merely busy)."""
    from .llm.base import LLMError
    from .pipeline import _resolve_llm

    if not args.json:
        _print(f"Checking the LLM ({opts.llm_provider})...")
    try:
        _resolve_llm(opts).verify_credentials(timeout=20)
    except LLMError as e:
        return "" if e.is_quota else str(e)   # 429 = key accepted, provider busy: the real call retries
    except Exception as e:  # noqa: BLE001 - any failure of the check is reported, exit code 2
        return f"LLM check failed: {e}"
    return ""


def _run_progress(args: argparse.Namespace):
    def progress(msg: str | None, fraction: float | None = None, eta_s: float | None = None,  # noqa: ARG001
                 eta_scope: str = "job") -> None:
        if args.json or not msg:
            return  # keep stdout machine-pure under --json
        if eta_s is not None and msg.lstrip().startswith("..."):
            msg = f"{msg}  ({_fmt_eta(eta_s, eta_scope)})"
        _print(msg)
    return progress


def _report_run(args: argparse.Namespace, source: Path, opts: PipelineOptions, result) -> int:
    payload = {
        "source": str(source),
        "profile": result.profile,
        "task": opts.task,
        "elapsed_s": round(result.elapsed_s, 1),
        "outputs": {k: str(v) for k, v in result.outputs.items()},
    }
    if result.llm_models:
        payload["llm_models"] = result.llm_models
    if result.warnings:
        payload["warnings"] = result.warnings
    if args.json:
        return _emit_json(payload)
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr, flush=True)
    print("\nOutputs:")
    for name, path in result.outputs.items():
        print(f"  {name:20} {path}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    source = Path(args.input)
    if not source.is_file():
        return _fail(args, 1, f"input not found: {source}")
    settings = load_settings()
    opts = _run_options(args, settings)
    problem = _option_problem(args, opts)
    if problem:
        return _fail(args, 1, *problem)
    if opts.task != "raw":
        llm_problem = _llm_problem(args, opts)
        if llm_problem:
            return _fail(args, 2, llm_problem)
    out_dir = Path(args.out_dir or _resolve_out(settings))
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _fail(args, 1, f"cannot create the output folder {out_dir}: {e.strerror or e}")
    work_root = Path(args.work_dir or config.WORK_DIR)
    work_dir = work_root / names.folder_stem(work_root, source.stem, file_suffix=len(".wav"))
    try:
        result = run_pipeline(source, work_dir, out_dir, opts, progress_cb=_run_progress(args))
    except Exception as e:  # noqa: BLE001 - any processing failure is exit code 2 with its message
        return _fail(args, 2, str(e))
    return _report_run(args, source, opts, result)


def _resolve_out(settings) -> Path:
    custom = (settings.output_dir or "").strip()
    return Path(custom) if custom else config.OUT_DIR


# ---------------------------------------------------------------------------
# profiles / tasks
# ---------------------------------------------------------------------------

def cmd_profiles(args: argparse.Namespace) -> int:
    profiles = [
        {"name": p.name, "description": p.description, "language": p.language,
         "corrections": len(p.corrections)}
        for p in list_profiles()
    ]
    tasks = _tasks()
    prompts = task_default_prompts()
    if args.json:
        return _emit_json({"profiles": profiles, "tasks": tasks, "task_prompts": prompts})
    for p in profiles:
        print(f"{p['name']:12}  {p['description']}  [corrections: {p['corrections']}]")
    print(f"\nTasks: {', '.join(tasks)}")
    print("Prompt presets:")
    for t, prompt in prompts.items():
        print(f"  {t:10} {prompt}")
    return 0


# ---------------------------------------------------------------------------
# whisper models
# ---------------------------------------------------------------------------

def _model_line(st: dict) -> str:
    if st.get("installed"):
        size = f" ({st['size_mb']} MB)" if st.get("size_mb") else ""
        mem = " [in memory]" if st.get("loaded") else ""
        return f"{st['size']:10} installed{size}{mem}"
    if st.get("downloading"):
        return f"{st['size']:10} downloading {st.get('progress', 0) * 100:.0f}%"
    err = f" (last download failed: {st['error']})" if st.get("error") else ""
    return f"{st['size']:10} not installed{err}"


MODEL_DOWNLOAD_MAX_S = 6 * 3600   # large-v3 is ~3 GB; a download still running after this has stalled


def _models_list(args: argparse.Namespace, model_mgr, _size: str) -> int:
    data = {"models": model_mgr.list_models()}
    if args.json:
        return _emit_json(data)
    for m in data["models"]:
        print(_model_line(m))
    return 0


def _models_download(args: argparse.Namespace, model_mgr, size: str) -> int:
    if not args.json:
        _print(f"Downloading {size} (this can take a while)...")
    dl = model_mgr.download_model(size)
    if dl._thread is not None:
        dl._thread.join(timeout=MODEL_DOWNLOAD_MAX_S)
    st = model_mgr.model_status(size)
    if not st.get("installed"):
        return _fail(args, 2, f"download of {size} failed: {st.get('error') or 'unknown error'}")
    if args.json:
        return _emit_json(st)
    print(_model_line(st))
    return 0


def _models_delete(args: argparse.Namespace, model_mgr, size: str) -> int:
    was = model_mgr.is_model_downloaded(size)
    ok = model_mgr.delete_model(size)
    if args.json:
        _emit_json({"size": size, "deleted": bool(ok and was)})
    else:
        print(f"Deleted {size}." if was else f"{size} was not installed; nothing to delete.")
    return 0 if ok else 2


def _models_open_folder(args: argparse.Namespace, model_mgr, _size: str) -> int:
    folder = model_mgr.hub_cache_dir()
    if not folder.is_dir():
        return _fail(args, 2, f"model cache folder does not exist yet: {folder}")
    from .webapp.main import _open_in_explorer

    _open_in_explorer(folder)
    if args.json:
        return _emit_json({"folder": str(folder)})
    print(folder)
    return 0


def _models_status(args: argparse.Namespace, model_mgr, size: str) -> int:
    st = model_mgr.model_status(size)
    if args.json:
        return _emit_json(st)
    print(_model_line(st))
    if not st.get("installed") and not st.get("downloading"):
        print(f"  get it with: minutenest-cli models download {size}")
    return 0


_MODEL_ACTIONS = {"list": _models_list, "download": _models_download, "delete": _models_delete,
                  "open-folder": _models_open_folder, "status": _models_status}


def cmd_models(args: argparse.Namespace) -> int:
    from . import models as model_mgr

    if args.action not in ("list", "open-folder"):
        known = ", ".join(model_mgr.KNOWN_MODELS)
        if not args.size:
            return _fail(args, 1, f"'models {args.action}' needs a model size ({known})")
        if args.size not in model_mgr.KNOWN_MODELS:
            return _fail(args, 1, f"unknown model size '{args.size}' (known: {known})")
    return _MODEL_ACTIONS[args.action](args, model_mgr, args.size or "")


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

_TOP_KEYS = [k for k in AppSettings.__dataclass_fields__ if k != "llm"]
_LLM_KEYS = list(LLMSettings.__dataclass_fields__)
_BOOL_KEYS = {"onboarded", "wizard_done"}


def _all_keys() -> list[str]:
    return _TOP_KEYS + [f"llm.{k}" for k in _LLM_KEYS]


def _allowed_values(key: str) -> list[str] | None:
    return {
        "model_size": _known_models(),
        "device": DEVICES,
        "compute_type": COMPUTE_TYPES,
        "language": _language_codes(),
        "document_language": ["", *_language_codes()],   # "" = the audio language
        "default_profile": _profile_names(),
        "default_task": _tasks(),
        "output_format": OUTPUT_FORMATS,
        "llm.provider": PROVIDERS,
    }.get(key)


def _print_settings(d: dict, prefix: str = "") -> None:
    """``key = value`` lines, nested sections as dotted keys (no recursion: Power of Ten rule 1)."""
    pending = [(prefix, d)]
    while pending:
        base, node = pending.pop(0)
        for k, v in node.items():
            if isinstance(v, dict):
                pending.append((f"{base}{k}.", v))
            else:
                print(f"{base}{k} = {v if v != '' else '(empty)'}")


def _settings_get(args: argparse.Namespace) -> int:
    public = load_settings().to_public_dict()
    if not args.key:
        if args.json:
            return _emit_json(public)
        _print_settings(public)
        return 0
    node: dict | object = public          # dotted path: llm.model, language, ...
    for part in args.key.split("."):
        if not (isinstance(node, dict) and part in node):
            return _fail(args, 1, f"unknown settings key '{args.key}'", f"known keys: {', '.join(_all_keys())}")
        node = node[part]
    if args.json:
        return _emit_json({"key": args.key, "value": node})
    if isinstance(node, dict):
        _print_settings(node, f"{args.key}.")
    else:
        print(node if node != "" else "(empty)")
    return 0


def _parse_setting(pair: str) -> tuple[str, object]:
    """``KEY=VALUE`` -> (key, value); raises ValueError(error, hint) for anything invalid."""
    if "=" not in pair:
        raise ValueError(f"expected KEY=VALUE, got '{pair}'", "")
    k, _, v = pair.partition("=")
    k = k.strip()
    if k not in _all_keys():
        raise ValueError(f"unknown settings key '{k}'", f"known keys: {', '.join(_all_keys())}")
    allowed = _allowed_values(k)
    if allowed is not None and v not in allowed:
        names = [a or "'' (empty)" for a in allowed]
        shown = ", ".join(names) if len(names) <= 20 else ", ".join(names[:20]) + ", ..."
        raise ValueError(f"'{v}' is not a valid {k} (choose from: {shown})", "")
    if k in _BOOL_KEYS:
        if v.lower() not in ("true", "false"):
            raise ValueError(f"{k} must be true or false", "")
        return k, v.lower() == "true"
    return k, v  # text stays text ("output_dir=2026" is a folder name)


def _settings_set(args: argparse.Namespace) -> int:
    pairs = ([args.key] if args.key else []) + list(args.set_args or [])
    if not pairs:
        return _fail(args, 1, "nothing to set — pass KEY=VALUE pairs",
                     "e.g. minutenest-cli settings set language=en llm.provider=ollama")
    top: dict = {}
    llm: dict = {}
    for pair in pairs:
        try:
            k, value = _parse_setting(pair)
        except ValueError as e:
            return _fail(args, 1, *e.args)
        (llm if k.startswith("llm.") else top)[k.removeprefix("llm.")] = value
    before = load_settings().to_public_dict()
    updated = update_settings({**top, "llm": llm} if llm else top).to_public_dict()
    if args.json:
        return _emit_json(updated)
    for k in top:
        print(f"{k}: {before.get(k)!r} -> {updated.get(k)!r}")
    for k in llm:
        if k in SECRET_KEYS:
            print(f"llm.{k}: {'set' if updated['llm'][k] else 'cleared'}")
        else:
            print(f"llm.{k}: {before['llm'].get(k)!r} -> {updated['llm'].get(k)!r}")
    return 0


def _settings_reset(args: argparse.Namespace) -> int:
    from .settings import save_settings

    save_settings(AppSettings())
    public = load_settings().to_public_dict()
    if args.json:
        return _emit_json(public)
    print("Settings reset to factory defaults. Saved API keys were cleared.")
    return 0


def cmd_settings(args: argparse.Namespace) -> int:
    action = {"get": _settings_get, "set": _settings_set, "reset": _settings_reset}.get(args.action)
    return action(args) if action else 1


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------

def cmd_serve(args: argparse.Namespace) -> int:
    from minutenest.webapp.main import create_app

    import uvicorn

    app = create_app()  # also sweeps scratch left by a crash or older versions
    print(f"MinuteNest is running at http://{args.host}:{args.port}/ (Ctrl+C to stop)", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning",
                log_config=None)  # log_config=None: windowed exe has no stdout
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    """argparse exits with 2 on bad flags; the contract says usage errors
    are 1 (2 means a processing failure that a script may want to retry)."""

    def error(self, message: str):  # type: ignore[override]
        self.print_usage(sys.stderr)
        _fail(None, 1, message)
        raise SystemExit(1)


def _add_run_parser(sub) -> None:
    p_run = sub.add_parser("run", help="Transcribe a file (and optionally LLM-format it)")
    p_run.add_argument("input", help="Audio or video file")
    p_run.add_argument("--profile", help="Domain profile (default from settings; see 'profiles')")
    p_run.add_argument("--task", choices=_tasks(),
                       help="raw = transcript only, no LLM (default from settings)")
    p_run.add_argument("--prompt", help="Your own instruction for the LLM (implies --task custom)")
    p_run.add_argument("--llm", choices=PROVIDERS, help="LLM provider (default from settings)")
    p_run.add_argument("--llm-model", help="LLM model name")
    p_run.add_argument("--model", help=f"Whisper size: {', '.join(_known_models())}")
    p_run.add_argument("--device", help="cuda | cpu")
    p_run.add_argument("--compute-type", help="float16 | int8 | int8_float16 | float32")
    p_run.add_argument("--language", help="Language spoken in the audio, e.g. ru, en (default from settings)")
    p_run.add_argument("--document-language",
                       help="Language the LLM writes the document in, e.g. en "
                            "(default from settings; else the audio language)")
    p_run.add_argument("--output-format", choices=OUTPUT_FORMATS,
                       help="Document format (default from settings; transcripts always written)")
    p_run.add_argument("--no-normalize", action="store_true", help="Skip audio normalization")
    p_run.add_argument("--chunk-length", type=int,
                       help=f"Chunk length in seconds (default 600, at least {MIN_CHUNK_S})")
    p_run.add_argument("--out-dir", help="Where to save results (default: Save folder from settings, "
                                         "else results/ next to the app)")
    p_run.add_argument("--work-dir", help="Working dir for intermediate audio")
    p_run.add_argument("--json", action="store_true", help="Machine-readable output")
    p_run.set_defaults(func=cmd_run)


def _add_other_parsers(sub) -> None:
    p_prof = sub.add_parser("profiles", help="List profiles, tasks and prompt presets")
    p_prof.add_argument("--json", action="store_true")
    p_prof.set_defaults(func=cmd_profiles)

    p_mod = sub.add_parser("models", help="Manage whisper models")
    p_mod.add_argument("action", choices=["list", "download", "delete", "status", "open-folder"])
    p_mod.add_argument("size", nargs="?", help=f"Model size: {', '.join(_known_models())}")
    p_mod.add_argument("--json", action="store_true")
    p_mod.set_defaults(func=cmd_models)

    p_set = sub.add_parser("settings", help="Get or set app settings")
    p_set.add_argument("action", choices=["get", "set", "reset"])
    p_set.add_argument("key", nargs="?", help="Dotted key for 'get' (e.g. llm.model)")
    p_set.add_argument("set_args", nargs="*", help="KEY=VALUE pairs for 'set' (e.g. llm.provider=ollama)")
    p_set.add_argument("--json", action="store_true")
    p_set.set_defaults(func=cmd_settings)

    p_serve = sub.add_parser("serve", help="Run the web UI (FastAPI)")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8123)
    p_serve.set_defaults(func=cmd_serve)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="minutenest-cli",
        description="Every meeting, written down. "
                    "Add --json to any command for machine-readable output. "
                    "Exit codes: 0 ok, 1 usage/input error, 2 processing failure.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    _add_run_parser(sub)
    _add_other_parsers(sub)
    return parser


def _utf8_when_piped() -> None:
    """Scripts and agents read our output as UTF-8 (AGENTS.md)."""
    # A pipe gets the Windows ANSI code page by default: a Russian file name
    # came out as cp1251 bytes, or crashed the CLI where the code page has no
    # Cyrillic. A console window already prints Unicode, so leave that alone.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)   # io.TextIOWrapper only
        if stream is not None and not stream.isatty() and reconfigure is not None:
            reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    _utf8_when_piped()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
