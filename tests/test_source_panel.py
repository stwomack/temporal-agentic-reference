"""Story 4.1: the code panel must always resolve to real source.

These tests fail if a step's function is renamed or moved without updating
common/constants.py, which is the failure mode that would silently leave the
code panel pointing at nothing.
"""

from __future__ import annotations

import pytest

from api.source import SourceLookupError, load_step_source
from common.constants import STEP_ORDER, STEP_SOURCE


@pytest.mark.parametrize("step", STEP_ORDER)
def test_every_step_resolves_to_real_source(step: str):
    assert step in STEP_SOURCE, f"step {step} has no source mapping"
    source = load_step_source(step)
    assert source["file"] == STEP_SOURCE[step]["file"]
    assert source["symbol"] == STEP_SOURCE[step]["symbol"]
    assert 1 <= source["start_line"] <= source["end_line"]

    lines = source["code"].split("\n")
    assert source["end_line"] <= len(lines)
    highlighted = "\n".join(lines[source["start_line"] - 1 : source["end_line"]])
    symbol = source["symbol"]
    assert (
        f"def {symbol}" in highlighted
        or f"class {symbol}" in highlighted
        or f"{symbol} =" in highlighted
    ), f"highlighted range for {step} does not contain {symbol}"


def test_highlight_includes_decorators():
    source = load_step_source("erp")
    lines = source["code"].split("\n")
    assert lines[source["start_line"] - 1].strip() == "@activity.defn"


def test_unknown_step_raises():
    with pytest.raises(SourceLookupError):
        load_step_source("not-a-step")
