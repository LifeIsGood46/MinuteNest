"""Power of Ten rules 1 and 4 for the Python sources (QUALITY.md).

Rule 1: no recursion, direct or through other functions of the same module.
Rule 4: no function longer than 60 lines (counted from `def` to its last line,
docstring and comments included: stricter than Holzmann's "60 lines of code").
ruff and pyright check the other rules; ESLint does the same two for the web UI.

    python scripts/check_power_of_ten.py        exit 0 = no finding, 1 = findings listed
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [*sorted((ROOT / "minutenest").rglob("*.py")), ROOT / "launcher.py", ROOT / "cli_entry.py",
           *sorted((ROOT / "scripts").glob("*.py"))]
MAX_LINES = 60

Function = ast.FunctionDef | ast.AsyncFunctionDef


def _called_names(fn: Function, cls: str | None) -> set[str]:
    """What this function calls, as graph names: f(...) is the module function f;
    self.f(...) / cls.f(...) inside class C is the method C.f."""
    names = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif (cls and isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
              and target.value.id in ("self", "cls")):
            names.add(f"{cls}.{target.attr}")
    return names


def _call_graph(tree: ast.Module) -> dict[str, set[str]]:
    """Module functions by name, methods by Class.name (so same-named methods of
    different classes stay apart). Calls from nested functions count for the
    function they are nested in."""
    graph: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, Function):
            graph[node.name] = _called_names(node, None)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, Function):
                    graph[f"{node.name}.{item.name}"] = _called_names(item, node.name)
    return graph


def _cycles(calls: dict[str, set[str]]) -> list[list[str]]:
    """Call cycles among one module's functions, found without recursion:
    from each function, follow calls breadth-first and report a path back to it."""
    found = []
    for start in sorted(calls):
        paths = [[start]]
        seen = {start}
        while paths:
            path = paths.pop(0)
            for callee in sorted(calls.get(path[-1], ())):
                if callee == start:
                    found.append([*path, start])
                    paths = []
                    break
                if callee in calls and callee not in seen:
                    seen.add(callee)
                    paths.append([*path, callee])
    return found


def check(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    rel = path.relative_to(ROOT)
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, Function):
            continue
        length = (node.end_lineno or node.lineno) - node.lineno + 1
        if length > MAX_LINES:
            problems.append(f"{rel}:{node.lineno}: {node.name} has {length} lines (rule 4: at most {MAX_LINES})")
    problems.extend(f"{rel}: recursion {' -> '.join(cycle)} (rule 1)" for cycle in _cycles(_call_graph(tree))
                    if cycle[0] == min(cycle[:-1]))          # each cycle once, from its first name
    return problems


def main() -> int:
    problems = [p for src in SOURCES for p in check(src)]
    for p in problems:
        print(p)
    print(f"{len(SOURCES)} files, {len(problems)} finding(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
