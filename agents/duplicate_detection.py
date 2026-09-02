"""Duplicate and split purchase detection agent.

A tool-using specialist. It queries the recent purchase order history for the
same vendor and judges whether the new request duplicates an existing order, or
looks like one purchase deliberately split across several orders to stay under
an approval threshold, which policy clause 4.1 treats as a single purchase.

This is the agent whose value is hardest to fake with a rule: near duplicates
differ in wording and rounding, and a split is a judgment about intent.
"""

from __future__ import annotations

import json
from typing import Any

from langgraph.graph import StateGraph
from pydantic import BaseModel, Field

from agents.react_agent import (
    AgentSpec,
    AgentState,
    assemble_graph,
    run_model_turn,
    run_tool_calls,
)
from agents.tools import DUPLICATE_TOOLS
from common.models import AgentFinding, Severity

AGENT = "duplicate_detection"
LABEL = "Duplicate detection agent"

SYSTEM_PROMPT = """You look for duplicate and split purchase orders.

Call search_recent_purchase_orders for the vendor named in the request, then \
compare what comes back against the new request.

Report duplicate_suspected only when BOTH of these hold for the same past \
order:
1. The goods or services are substantially the same thing. Wording will differ, \
so judge the substance, but "shrink film and case tape" and "carton dividers" \
are different products, not the same one described differently.
2. The totals are close, within roughly 15 percent of each other.

If only one of those holds, it is not a duplicate.

Report split_suspected only when several recent orders to the same vendor, \
close together in time, look like one larger purchase divided up to stay under \
an approval threshold.

Repeat business with the same vendor is normal and expected. A vendor supplying \
several different products, or the same product on a regular restocking \
cadence months apart, is not a duplicate and not a split. Most requests are \
clean, and saying so is the right answer.

In your rationale, name the order ids you compared against and say what made \
them match or not match. Call DuplicateFinding when you have compared the \
history."""


class DuplicateFinding(BaseModel):
    """The duplicate and split review. Call this once you have the history."""

    duplicate_suspected: bool = Field(
        description="True if this request duplicates a recent order"
    )
    split_suspected: bool = Field(
        description="True if recent orders look like a deliberately split purchase"
    )
    matching_po_ids: list[str] = Field(
        default_factory=list, description="Ids of the orders you matched against"
    )
    rationale: str = Field(description="What you compared and what you concluded")


SPEC = AgentSpec(
    name=AGENT,
    system_prompt=SYSTEM_PROMPT,
    finding_schema=DuplicateFinding,
    tools=DUPLICATE_TOOLS,
)


# The plugin identifies nodes by module and qualified name, so these must live
# at module level rather than inside a factory. See agents/react_agent.py.
def call_model(state: AgentState) -> dict[str, Any]:
    """One Bedrock turn for this agent. Runs as a Temporal Activity."""
    return run_model_turn(SPEC, state)


def run_tools(state: AgentState) -> dict[str, Any]:
    """Execute this agent's tool calls. Runs as a Temporal Activity."""
    return run_tool_calls(SPEC, state)


def build_graph() -> StateGraph:
    return assemble_graph(SPEC, call_model, run_tools)


def task(extracted: dict[str, Any]) -> str:
    return "\n".join(
        [
            "Check this new purchase order request for duplication or splitting.",
            json.dumps(
                {
                    "vendor": extracted.get("vendor"),
                    "requester": extracted.get("requester"),
                    "currency": extracted.get("currency"),
                    "total_amount": extracted.get("total_amount"),
                    "line_items": extracted.get("line_items"),
                },
                indent=2,
            ),
        ]
    )


def to_finding(state: dict[str, Any]) -> AgentFinding:
    raw = state.get("finding") or {}
    duplicate = bool(raw.get("duplicate_suspected"))
    split = bool(raw.get("split_suspected"))
    matches = list(raw.get("matching_po_ids") or [])
    if duplicate or split:
        kind = "Duplicate" if duplicate else "Split purchase"
        headline = f"{kind} suspected: {', '.join(matches) or 'no ids given'}"
        severity = Severity.CAUTION
    else:
        headline = "No duplicate or split detected"
        severity = Severity.OK
    return AgentFinding(
        agent=AGENT,
        label=LABEL,
        model_id=state.get("model_id", ""),
        turns=int(state.get("turns", 0)),
        tool_calls=list(state.get("tool_calls") or []),
        input_tokens=int(state.get("input_tokens", 0)),
        output_tokens=int(state.get("output_tokens", 0)),
        headline=headline,
        detail=raw.get("rationale", ""),
        severity=severity,
        raw=raw,
    )
