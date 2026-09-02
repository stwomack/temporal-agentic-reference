"""Story 4.1 support: locate the source that implements each workflow step.

Line numbers are resolved by parsing the module with `ast` at request time, so
the code panel keeps pointing at the right function even after the files are
edited. Nothing is hardcoded.
"""

from __future__ import annotations

import ast
from pathlib import Path

from common.constants import STEP_SOURCE

REPO_ROOT = Path(__file__).resolve().parent.parent


class SourceLookupError(Exception):
    pass


def _find_symbol(tree: ast.AST, name: str) -> ast.AST | None:
    """Find a def, class, or module level assignment by name.

    Functions are the obvious target, but for an agent the most useful code to
    put on screen is its system prompt, which is a module level constant. This
    handles all three so common/constants.py can point at whichever best
    explains a step.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == name
        ):
            return node
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return node
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return node
    return None


def load_step_source(step: str) -> dict:
    location = STEP_SOURCE.get(step)
    if location is None:
        raise SourceLookupError(f"No source mapping for step {step!r}")

    relative = location["file"]
    symbol = location["symbol"]
    path = (REPO_ROOT / relative).resolve()
    # Never serve anything outside the repo.
    if not path.is_relative_to(REPO_ROOT) or not path.is_file():
        raise SourceLookupError(f"Source file not found: {relative}")

    text = path.read_text(encoding="utf-8")
    try:
        node = _find_symbol(ast.parse(text), symbol)
    except SyntaxError as exc:
        raise SourceLookupError(f"Cannot parse {relative}: {exc}") from exc

    if node is None:
        raise SourceLookupError(f"Symbol {symbol!r} not found in {relative}")

    # Include the decorator lines so @activity.defn is visible in the panel.
    start = min(
        [node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]
    )
    end = getattr(node, "end_lineno", None) or start

    return {
        "step": step,
        "file": relative,
        "symbol": symbol,
        "start_line": start,
        "end_line": end,
        "code": text,
    }
