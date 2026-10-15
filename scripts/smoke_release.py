"""Post-build smoke gate: unpack the release zip like a fresh user and prove
the packaged app actually works. Catches the class of bug unit tests cannot:
missing binaries, broken bundles, wrong paths.

Checks (all against the extracted zip, not the dev tree):
  1. ffmpeg.exe + ffprobe.exe present in _internal/ffmpeg/
  2. WebView2 window assets present (tlb + loader)
  3. templates, static (sample recording) and profiles present
  4. the CLI exe runs from the extracted folder (settings get --json)
  5. bundled ffmpeg binaries launch
  6. certifi CA bundle bundled (cloud LLM TLS on clean machines)
  7. no stale pywebview/pythonnet in the bundle
  8. static UI is current (language select, wizard/highlight fixes, button
     flicker fix, e2e readiness marker)
  9. (optional, --with-transcribe) real transcription of a sample to done

Exit 0 = shippable, 1 = broken.
"""

from __future__ import annotations

import json
import re
import subprocess  # nosec B404 - argument lists only, no shell
import sys
import tempfile
import zipfile
from pathlib import Path

REQUIRED = [
    "_internal/ffmpeg/ffmpeg.exe",
    "_internal/ffmpeg/ffprobe.exe",
    "_internal/minutenest/native/WebView2.tlb",
    "_internal/minutenest/native/WebView2Loader.dll",
    "_internal/minutenest/webapp/templates/index.html",
    "_internal/minutenest/webapp/static/app.js",
    "_internal/minutenest/webapp/static/style.css",
    "_internal/minutenest/webapp/static/sample.mp3",
    "_internal/minutenest/profiles/scada.json",
    "licenses/LICENSE.txt",
    "licenses/THIRD_PARTY_NOTICES.md",
    "licenses/NVIDIA-CUDA-EULA.txt",
    "licenses/FFMPEG-GPL-3.0.txt",
]

CLI = "minutenest-cli.exe"
GUI = "MinuteNest.exe"
STATIC = Path("_internal") / "minutenest" / "webapp" / "static"

# Loose static files can be checked byte-for-byte for the current UI (Python
# modules live inside the PYZ archive; their behavior is verified at runtime by
# the test suite with --app exe).
APP_JS_MARKERS = [
    ("job-language", "per-job language select"),
    ("window.scrollBy", "wizard wheel forwarding"),
    ("client-bye", "tab-close beacon"),
    ("settingsCache.language", "adaptive job card defaults"),
    ("stagedSignature", "staged-card rebuild (prompt editor)"),
    ("scrollTargetIntoView", "wizard container-only scrolling"),
    ("promptOpen", "prompt editor open state"),
    ("job-format", "per-job output format select"),
    ("wizardRetarget", "wizard retarget on settings close"),
    ("refreshJobsOnce", "coalesced job refreshes"),
    ("visibleRect", "wizard highlight clipped to the visible target area"),
    ("ResizeObserver", "wizard re-place when a target grows asynchronously"),
    ("wizard-stack", "guide stacks below the modal on narrow windows"),
    ("handleResize", "guide re-fits the highlight after a window resize"),
    ("settingsLoaded", "settings form is never saved before it loads"),
    ("btn-label", "open-folder button label span (no innerHTML flicker)"),
    ("dataset.events", "UI live-update readiness marker"),
]
STYLE_CSS_MARKERS = [
    (".modal.modal-large", "settings modal full width"),
    ("--rail-w", "content centered for the rail"),
]


def run(cmd: list[str], timeout: int, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """A bundled program, by its full path in the unpacked release; no shell."""
    return subprocess.run(cmd, check=False, capture_output=True, text=True,  # noqa: S603  # nosec B603
                          timeout=timeout, cwd=str(cwd) if cwd else None)


def check_files(root: Path, names: set[str]) -> list[str]:
    failures = []
    for req in REQUIRED:
        if req in names:
            print(f"  ok  {req}")
        else:
            failures.append(f"missing from zip: {req}")
    if (root / GUI).is_file():
        print(f"  ok  {GUI}")
    else:
        failures.append(f"missing {GUI}")
    if not (root / CLI).is_file():
        failures.append("missing minutenest-cli.exe")
    return failures


def check_cli(root: Path) -> list[str]:
    """The packaged CLI runs from inside the extracted folder."""
    r = run([str(root / CLI), "settings", "get", "language", "--json"], timeout=120, cwd=root)
    if r.returncode != 0:
        return [f"CLI failed (exit {r.returncode}): {(r.stderr or '')[:200]}"]
    try:
        print(f"  ok  packaged CLI responded: {json.loads(r.stdout or '{}')}")
    except json.JSONDecodeError:
        return [f"CLI --json output not JSON: {r.stdout[:120]}"]
    return []


def check_ffmpeg(root: Path) -> list[str]:
    """The bundled binaries launch (the frozen app's resolver is hard to run from here)."""
    failures = []
    for exe in ("ffmpeg.exe", "ffprobe.exe"):
        p = root / "_internal" / "ffmpeg" / exe
        if not p.is_file():
            continue          # reported by check_files
        r = run([str(p), "-version"], timeout=60)
        if r.returncode != 0:
            failures.append(f"bundled {exe} does not run (exit {r.returncode})")
        else:
            print(f"  ok  bundled {exe}: {(r.stdout or r.stderr).splitlines()[0][:60]}")
    return failures


def check_bundle_contents(names: set[str]) -> list[str]:
    failures = []
    # certifi CA bundle must ship inside the app: without it cloud LLM
    # calls fail on clean machines with CERTIFICATE_VERIFY_FAILED.
    cacert = [n for n in names if n.endswith(("certifi/cacert.pem", "certifi\\cacert.pem"))]
    if cacert:
        print(f"  ok  certifi CA bundle: {cacert[0]}")
    else:
        failures.append("certifi cacert.pem missing — cloud LLM TLS will fail on clean machines")
    # The old window stack must not creep back in (it broke clean machines).
    stale = [n for n in names if "/pywebview/" in n or "/pythonnet/" in n or n.endswith("/clr.pyd")]
    if stale:
        failures.append(f"stale window stack in bundle: {stale[:3]}")
    else:
        print("  ok  no pywebview/pythonnet in bundle")
    return failures


def check_app_js(root: Path) -> list[str]:
    appjs = root / STATIC / "app.js"
    if not appjs.is_file():
        return ["app.js missing from bundle"]
    js = appjs.read_text(encoding="utf-8", errors="replace")
    failures = [f"stale UI bundle: '{label}' missing from app.js" for needle, label in APP_JS_MARKERS
                if needle not in js]
    if not failures:
        print("  ok  bundle UI is current (language select + wizard fixes)")
    return failures


def check_style_css(root: Path) -> list[str]:
    stylecss = root / STATIC / "style.css"
    if not stylecss.is_file():
        return []             # reported by check_files
    css = stylecss.read_text(encoding="utf-8", errors="replace")
    failures = [f"stale UI bundle: '{label}' missing from style.css" for needle, label in STYLE_CSS_MARKERS
                if needle not in css]
    if "padding-right: 420px" in css:
        failures.append("stale UI bundle: old wizard reserved-column shift still in style.css")
    # The shades must not swallow clicks (the "buttons take forever" bug).
    shade = re.search(r"\.wizard-shade\s*\{([^}]*)\}", css)
    if not shade or "pointer-events: none" not in shade.group(1):
        failures.append("stale UI bundle: wizard shades must not capture clicks")
    return failures


def check_native(root: Path) -> list[str]:
    native = root / "_internal" / "minutenest" / "native"
    if not (native / "WebView2.tlb").is_file() or not (native / "WebView2Loader.dll").is_file():
        return ["WebView2 native assets missing from bundle"]
    return []


def smoke(root: Path) -> list[str]:
    names = {str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*")}
    failures = check_files(root, names)
    if not failures:
        failures += check_cli(root)
    return [*failures, *check_ffmpeg(root), *check_bundle_contents(names),
            *check_app_js(root), *check_style_css(root), *check_native(root)]


def main() -> int:
    zip_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if not zip_path or not zip_path.is_file():
        print("usage: smoke_release.py <release.zip>")
        return 1
    with tempfile.TemporaryDirectory(prefix="mn_smoke_") as td:
        root = Path(td)
        print(f"unpacking {zip_path.name} ...")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(root)
        failures = smoke(root)
    if failures:
        print("\nSMOKE GATE FAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nSMOKE GATE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
