"""Interpreter check for the command line entrypoints.

Standard library only, on purpose. This module has to be importable by the very
Python that is missing the project's dependencies, so it can explain the
problem instead of letting a raw ModuleNotFoundError traceback do it.

The usual cause is running "python worker.py" with the system interpreter
instead of "uv run python worker.py", which uses the project's .venv.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# One representative import per dependency group, enough to tell "wrong
# interpreter" apart from "dependencies not installed yet".
REQUIRED_MODULES = ("temporalio", "botocore", "langchain_aws", "pydantic")


def require_dependencies() -> None:
    """Exit with an actionable message if the project's packages are missing."""
    missing = [
        name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None
    ]
    if not missing:
        return

    repo_root = Path(__file__).resolve().parent.parent
    entrypoint = Path(sys.argv[0]).name or "the script"
    venv = repo_root / ".venv"

    print(
        f"FAIL: this interpreter cannot import {', '.join(missing)}.",
        file=sys.stderr,
    )
    print(f"      Interpreter: {sys.executable}", file=sys.stderr)
    if venv.is_dir():
        print(
            f"Fix:  run it through uv so the project's .venv is used:\n"
            f"        uv run python {sys.argv[0] or entrypoint}",
            file=sys.stderr,
        )
    else:
        print(
            f"Fix:  install the dependencies first, then run through uv:\n"
            f"        uv sync --extra dev\n"
            f"        uv run python {sys.argv[0] or entrypoint}",
            file=sys.stderr,
        )
    raise SystemExit(1)
