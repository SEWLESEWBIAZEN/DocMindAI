"""Every package the application imports must be installed in production (not only in dev).

A clean production image installs only the runtime dependencies. This found
``httpx`` imported by the OIDC, Slack and Drive clients while declared as dev-only,
which crashed the production API at startup.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _runtime_closure() -> set[str]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {_norm(p["name"]): p for p in lock["package"]}
    project = packages["documind"]
    todo = [_norm(d["name"]) for d in project.get("dependencies", [])]
    seen: set[str] = set()
    while todo:
        name = todo.pop()
        if name in seen or name not in packages:
            continue
        seen.add(name)
        pkg = packages[name]
        todo += [_norm(d["name"]) for d in pkg.get("dependencies", [])]
        for extra in pkg.get("optional-dependencies", {}).values():
            todo += [_norm(d["name"]) for d in extra]
    return seen


def _imports() -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "src" / "documind").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                found.add(node.module.split(".")[0])
    return {m for m in found if m not in sys.stdlib_module_names and m != "documind"}


def test_every_import_is_a_runtime_dependency() -> None:
    runtime = _runtime_closure()
    dists = packages_distributions()
    missing = {
        module: sorted(dists.get(module, [module]))
        for module in _imports()
        if not any(_norm(d) in runtime for d in dists.get(module, [module]))
    }
    assert missing == {}, f"imported but not a runtime dependency: {missing}"
