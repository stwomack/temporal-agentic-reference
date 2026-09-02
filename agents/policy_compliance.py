"""Policy compliance agent.

Reads the written procurement policy and judges the request against it. This is
deliberately separate from the deterministic guardrail in
activities/guardrail.py: the guardrail enforces a numeric threshold that must
give the same answer every time, while this agent reads prose clauses that
cannot be reduced to a comparison, such as whether a capital purchase cites a
project code or whether a lump sum is itemized.

The guardrail is the hard floor. This agent can add concerns and can cause an
escalation, but it never overrides a deterministic block.

No tools: the whole policy fits in the prompt, so one turn is enough.
"""

from __future__ import annotations

import json
from typing import Any

from langgraph.graph import StateGraph
from pydantic import BaseModel, Field

from agents.react_agent import AgentSpec, AgentState, assemble_graph, run_model_turn
from agents.tools import procurement_policy
from common.models import AgentFinding, Severity

AGENT = "policy_compliance"
LABEL = "Policy compliance agent"

SYSTEM_PROMPT = """You review purchase order requests against the written \
procurement policy below. Judge only what the policy actually says.

{policy}

Rules for your review:
- Cite clause numbers, for example "1.3", for every violation you report.
- A clause you cannot evaluate from the information given is not a violation. \
Say so in your rationale instead of assuming the worst.
- Do not re-derive spending approval thresholds. A separate deterministic \
check owns those. Your job is the clauses that need reading.
- If you found no violations, set compliant to true and leave violated_clauses \
empty. Never set compliant to false without naming at least one clause number \
in violated_clauses. Those two fields must agree.

Call PolicyComplianceFinding when you have finished your review."""


class PolicyComplianceFinding(BaseModel):
    """The policy review result. Call this when your review is complete."""

    compliant: bool = Field(
        description="True only if you found no violations at all"
    )
    violated_clauses: list[str] = Field(
        default_factory=list,
        description="Clause numbers violated, e.g. ['1.3', '2.2']",
    )
    concerns: list[str] = Field(
        default_factory=list,
        description="One short sentence per violation, in clause order",
    )
    rationale: str = Field(description="What you checked and what you concluded")


SPEC = AgentSpec(
    name=AGENT,
    system_prompt=SYSTEM_PROMPT.format(policy=procurement_policy()),
    finding_schema=PolicyComplianceFinding,
)


# The plugin identifies nodes by module and qualified name, so these must live
# at module level rather than inside a factory. See agents/react_agent.py.
def call_model(state: AgentState) -> dict[str, Any]:
    """One Bedrock turn for this agent. Runs as a Temporal Activity."""
    return run_model_turn(SPEC, state)


def build_graph() -> StateGraph:
    return assemble_graph(SPEC, call_model)


def task(extracted: dict[str, Any], vendor_context: str = "") -> str:
    lines = [
        "Review this purchase order request against the policy.",
        json.dumps(
            {
                "vendor": extracted.get("vendor"),
                "requester": extracted.get("requester"),
                "currency": extracted.get("currency"),
                "total_amount": extracted.get("total_amount"),
                "line_items": extracted.get("line_items"),
                "notes": extracted.get("notes"),
            },
            indent=2,
        ),
    ]
    if vendor_context:
        lines.append(f"Vendor registry context: {vendor_context}")
    return "\n".join(lines)


def to_finding(state: dict[str, Any]) -> AgentFinding:
    raw = state.get("finding") or {}
    clauses = list(raw.get("violated_clauses") or [])
    # Trust the clause list over the boolean. A model that sets compliant to
    # false while naming no clause has not actually found a violation, and
    # rendering that as "0 violations" would read like a bug to a viewer.
    if not clauses:
        headline = "No policy violations found"
        severity = Severity.OK
    else:
        headline = f"{len(clauses)} clause violation(s): {', '.join(clauses)}"
        severity = Severity.CAUTION
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
