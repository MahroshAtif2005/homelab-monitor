"""CI gate: no broad except blocks with silent continuations in backend/."""
import ast
import os
import textwrap
from pathlib import Path


BACKEND_ROOT = Path(__file__).parent.parent / "backend"

# Narrow typed catches that are intentionally silent (idempotent DDL migrations).
INTENTIONAL_SILENT = {
    "schema.py",
}


def _collect_py_files():
    for p in BACKEND_ROOT.rglob("*.py"):
        if p.name not in INTENTIONAL_SILENT:
            yield p


def _has_silent_broad_except(source: str) -> list[tuple[int, str]]:
    """Return (lineno, handler_text) for broad except blocks with no logging."""
    hits = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return hits
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Try,)):
            continue
        for handler in node.handlers:
            # Only broad catches: bare except or except Exception
            if handler.type is not None:
                if not (isinstance(handler.type, ast.Name) and handler.type.id == "Exception"):
                    continue
            body = handler.body
            if not body:
                continue
            # Check if ANY statement in the body is a print/log call
            has_log = False
            for stmt in body:
                src = ast.unparse(stmt)
                if "print(" in src or "logging." in src:
                    has_log = True
                    break
            if not has_log:
                # Check if body is only pass/return/continue/raise
                silent_stmts = (ast.Pass, ast.Return, ast.Continue, ast.Raise)
                if all(isinstance(s, silent_stmts) for s in body):
                    hits.append((handler.lineno, ast.unparse(handler)))
    return hits


def test_no_silent_broad_except():
    """Fail if any backend .py file has a broad except with no logging."""
    violations = []
    for path in _collect_py_files():
        source = path.read_text()
        hits = _has_silent_broad_except(source)
        for lineno, snippet in hits:
            violations.append(f"{path.relative_to(BACKEND_ROOT.parent)}:{lineno}\n  {snippet[:120]}")
    assert not violations, (
        "Silent broad except blocks found in backend/:\n"
        + "\n".join(violations)
    )
