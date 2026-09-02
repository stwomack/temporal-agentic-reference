"""Live Bedrock extraction tests.

Skipped, never mocked, when the account cannot invoke the configured model.
The project constraint is explicit: no stubbed or hardcoded LLM responses
anywhere, including in tests.
"""

from __future__ import annotations

from agents.extraction_graph import build_extraction_graph, normalize
from common.config import get_settings
from common.models import ExtractedPO, Scenario
from common.scenarios import SCENARIOS


def test_normalize_is_deterministic_and_needs_no_model():
    """The workflow-side node must stay pure. This one runs without Bedrock."""
    state = {
        "extracted": {
            "vendor": "  Northwind Packaging Co. ",
            "requester": " Dana Whitfield ",
            "currency": "usd",
            "total_amount": 0,
            "line_items": [
                {"description": "lids", "quantity": 10, "unit_price": 2.5, "amount": 0}
            ],
        }
    }
    first = normalize(state)["extracted"]
    second = normalize(state)["extracted"]
    assert first == second
    assert first["currency"] == "USD"
    assert first["vendor"] == "Northwind Packaging Co."
    # Amount filled from quantity times unit price, total filled from the lines.
    assert first["line_items"][0]["amount"] == 25.0
    assert first["total_amount"] == 25.0


def test_normalize_keeps_a_stated_total_that_differs_from_the_lines():
    state = {
        "extracted": {
            "vendor": "V",
            "requester": "R",
            "currency": "USD",
            "total_amount": 500.0,
            "line_items": [
                {"description": "a", "quantity": 1, "unit_price": 100.0, "amount": 100.0}
            ],
        }
    }
    assert normalize(state)["extracted"]["total_amount"] == 500.0


def test_live_extraction_returns_structured_fields(live_bedrock):
    spec = SCENARIOS[Scenario.HAPPY_PATH]
    result = build_extraction_graph().compile().invoke({"raw_text": spec.raw_text})
    extracted = ExtractedPO.model_validate(result["extracted"])

    assert extracted.model_id == get_settings().bedrock_model_id
    # Proof the response came from the model rather than a canned value.
    assert extracted.input_tokens > 0
    assert extracted.output_tokens > 0

    assert "northwind" in extracted.vendor.lower()
    assert "whitfield" in extracted.requester.lower()
    assert extracted.currency == "USD"
    assert extracted.total_amount == 4660.0
    assert len(extracted.line_items) >= 2


def test_live_extraction_reads_a_blocked_vendor_order(live_bedrock):
    spec = SCENARIOS[Scenario.GUARDRAIL_VIOLATION]
    result = build_extraction_graph().compile().invoke({"raw_text": spec.raw_text})
    extracted = ExtractedPO.model_validate(result["extracted"])
    assert "acme" in extracted.vendor.lower()
    assert extracted.total_amount == 19200.0
