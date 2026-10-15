"""G. The command line (TEST_PLAN.md). The contract is AGENTS.md."""

import json
import time
from pathlib import Path

import pytest

from support.app import free_port
from support.media import RU_SPEECH, RU_TEXT, copy_as, repeated, share_heard


def _json(p):
    return json.loads(p.stdout)


def test_the_json_is_utf8_even_for_a_russian_file_name(cli, tmp_path):
    """Seen with the packaged CLI: piped output was in the Windows ANSI code
    page ("Встреча" as cp1251 bytes), unreadable as UTF-8 JSON, and a crash on
    PCs whose code page has no Cyrillic."""
    src = copy_as(RU_SPEECH, tmp_path / "Встреча №1.mp3")
    p = cli.run("run", src, "--model", "tiny", "--out-dir", tmp_path / "out", "--json")
    assert p.returncode == 0, p.stderr
    assert _json(p)["source"].endswith("Встреча №1.mp3")


def test_a_raw_run_answers_in_json_and_lists_files_that_exist(cli, tmp_path):
    src = copy_as(RU_SPEECH, tmp_path / "planning.mp3")
    p = cli.run("run", src, "--task", "raw", "--out-dir", tmp_path / "out", "--json")
    assert p.returncode == 0, p.stderr
    payload = _json(p)
    assert {"source", "profile", "task", "elapsed_s", "outputs"} <= payload.keys()
    assert all(Path(f).is_file() for f in payload["outputs"].values())
    text = Path(payload["outputs"]["transcript_corrected"]).read_text(encoding="utf-8")
    assert share_heard(RU_TEXT, text) >= 0.6


def test_progress_of_a_recording_in_parts_names_the_parts_not_internal_files(cli, tmp_path):
    """Seen by hand: "Transcribing chunk_001.wav [cpu/int8]", and "Transcription
    done" after each part although the job went on."""
    src = repeated(RU_SPEECH, tmp_path / "planning.mp3", 6)
    p = cli.run("run", src, "--model", "tiny", "--chunk-length", "30", "--out-dir", tmp_path / "out")
    assert p.returncode == 0, p.stderr
    progress = p.stdout + p.stderr
    assert "chunk_" not in progress
    assert "Transcribing part 1 of " in progress
    assert "Transcription done" not in progress


def test_a_file_whose_name_fills_the_windows_path_limit_is_transcribed(cli, tmp_path):
    """Windows paths stop at 259 characters unless long paths are switched on
    (they are off by default). The source itself fits; results and work files
    named <name>/<name>.wav or out/<name>.txt did not."""
    words = "Quarterly planning with the platform team about budgets and hiring " * 4
    name = words[: 250 - len(str(tmp_path))].rstrip() + ".mp3"
    src = copy_as(RU_SPEECH, tmp_path / name)
    p = cli.run("run", src, "--model", "tiny", "--out-dir", tmp_path / "out", "--json")
    assert p.returncode == 0, p.stderr
    outputs = [Path(f) for f in _json(p)["outputs"].values()]
    assert outputs
    assert all(f.is_file() and len(str(f)) < 260 for f in outputs), outputs


def test_a_recording_in_another_language_than_set_is_reported_in_the_json(cli, tmp_path):
    src = copy_as(RU_SPEECH, tmp_path / "planning.mp3")
    p = cli.run("run", src, "--model", "tiny", "--language", "en", "--out-dir", tmp_path / "out", "--json")
    assert p.returncode == 0, p.stderr
    assert any("sounds like Russian" in w for w in _json(p).get("warnings", [])), p.stdout


@pytest.mark.parametrize("args", [
    ["run", "missing.mp3"],
    ["run", "{clip}", "--model", "enormous"],
    ["run", "{clip}", "--profile", "no-such-profile"],
    ["run", "{clip}", "--task", "notes"],                  # no LLM configured
], ids=["missing file", "bad option value", "unknown profile", "formatted task without an LLM"])
def test_a_usage_error_exits_1_with_an_error_line_and_json(cli, tmp_path, args):
    clip = copy_as(RU_SPEECH, tmp_path / "clip.mp3")
    p = cli.run(*[a.format(clip=clip) for a in args], "--json")
    assert p.returncode == 1, p.stderr
    assert p.stderr.startswith("error:"), p.stderr
    assert _json(p)["exit_code"] == 1


def test_an_unreachable_llm_exits_2_before_transcribing(cli, tmp_path):
    closed = f"http://127.0.0.1:{free_port()}/v1"
    assert cli.run("settings", "set", f"llm.compat_base_url={closed}").returncode == 0
    t0 = time.monotonic()
    p = cli.run("run", copy_as(RU_SPEECH, tmp_path / "clip.mp3"), "--task", "notes", "--llm", "compat",
                "--llm-model", "any", "--out-dir", tmp_path / "out", "--json")
    assert p.returncode == 2, p.stderr
    assert not (tmp_path / "out").exists(), "it transcribed before checking the LLM"
    assert time.monotonic() - t0 < 60


def test_document_language_asks_the_model_for_that_language(cli, llm, tmp_path):
    assert cli.run("settings", "set", f"llm.compat_base_url={llm.base_url}").returncode == 0
    p = cli.run("run", copy_as(RU_SPEECH, tmp_path / "clip.mp3"), "--model", "tiny", "--task", "summary",
                "--llm", "compat", "--llm-model", llm.models[0], "--document-language", "en",
                "--out-dir", tmp_path / "out", "--json")
    assert p.returncode == 0, p.stderr
    assert "Write the whole document in English" in llm.requests[-1]["system"]


def test_an_unknown_document_language_is_a_usage_error(cli, tmp_path):
    p = cli.run("run", copy_as(RU_SPEECH, tmp_path / "clip.mp3"), "--document-language", "klingon", "--json")
    assert p.returncode == 1
    assert "klingon" in _json(p)["error"]


def test_settings_set_and_get_use_the_same_dotted_keys(cli):
    assert cli.run("settings", "set", "language=en", "llm.provider=ollama").returncode == 0
    assert _json(cli.run("settings", "get", "language", "--json"))["value"] == "en"
    assert _json(cli.run("settings", "get", "llm.provider", "--json"))["value"] == "ollama"


def test_an_unknown_setting_is_rejected_with_the_valid_keys(cli):
    p = cli.run("settings", "set", "colour=red")
    assert p.returncode == 1
    assert "language" in p.stderr


def test_an_api_key_can_be_set_but_never_read_back(cli):
    assert cli.run("settings", "set", "llm.openrouter_api_key=sk-or-v1-secret-9876").returncode == 0
    shown = json.dumps(_json(cli.run("settings", "get", "llm", "--json")))
    assert "secret" not in shown and "sk-or" not in shown


def test_settings_reset_restores_the_defaults(cli):
    cli.run("settings", "set", "default_task=summary")
    assert cli.run("settings", "reset").returncode == 0
    assert _json(cli.run("settings", "get", "default_task", "--json"))["value"] == "raw"


def test_models_status_and_profiles_answer_in_json(cli):
    assert _json(cli.run("models", "status", "tiny", "--json"))["installed"] is True
    profiles = _json(cli.run("profiles", "--json"))
    assert "generic" in [p["name"] for p in profiles["profiles"]]
    assert {"raw", "notes", "actions", "testplan", "summary"} <= set(profiles["tasks"])
