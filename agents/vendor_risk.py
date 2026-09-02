"""Vendor risk agent.

A tool-using specialist. It looks the vendor up in the registry, pulls the
incident history when the registry gives it a reason to, and rates the risk.
It has no access to the purchase order amount policy; judging the vendor is its
only job.

Runs as its own LangGraph graph, fanned out concurrently with the other
specialists by the orchestrator workflow.
"""

from __future__ import annotations

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
from agents.tools import VENDOR_RISK_TOOLS
from common.models import AgentFinding, Severity

AGENT = "vendor_risk"
LABEL = "Vendor risk agent"

SYSTEM_PROMPT = """You assess supplier risk for a beverage manufacturer's \
procurement team.

Use lookup_vendor first. If the vendor is not found, or its status is not \
approved, or it reports any incidents, also call list_vendor_incidents before \
deciding. Do not guess at facts you can look up.

Rate the risk:
- low: approved status, contract and verified tax id on file, and no incidents \
above low severity. A single resolved low severity incident does not raise the \
rating; approved vendors with a clean or near clean record are low.
- medium: approved but with medium severity incidents, or probationary status \
with a corrective action plan in place.
- high: probationary with unresolved issues, or missing a contract or a \
verified tax id.
- critical: unapproved status, absent from the registry, or incidents \
indicating possible fraud.

When you have enough evidence, call VendorRiskFinding. Cite the specific \
registry facts you relied on. Do not call it before you have looked the vendor \
up."""


class VendorRiskFinding(BaseModel):
    """The vendor risk assessment. Call this when you are done investigating."""

    risk_rating: str = Field(
        description="One of: low, medium, high, critical"
    )
    registry_status: str = Field(
        description="The vendor's status from the registry, or 'not_registered'"
    )
    concerns: list[str] = Field(
        default_factory=list, description="Specific issues found, one per entry"
    )
    rationale: str = Field(description="Why this rating, citing registry facts")


_SEVERITY = {
    "low": Severity.OK,
    "medium": Severity.CAUTION,
    "high": Severity.CAUTION,
    "critical": Severity.BLOCKER,
}


SPEC = AgentSpec(
    name=AGENT,
    system_prompt=SYSTEM_PROMPT,
    finding_schema=VendorRiskFinding,
    tools=VENDOR_RISK_TOOLS,
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
    return (
        f"Assess the supplier risk for this purchase order request.\n"
        f"Vendor: {extracted.get('vendor') or '(not stated)'}\n"
        f"Requester: {extracted.get('requester') or '(not stated)'}\n"
        f"Amount: {extracted.get('currency', 'USD')} {extracted.get('total_amount', 0):,.2f}"
    )


def to_finding(state: dict[str, Any]) -> AgentFinding:
    raw = state.get("finding") or {}
    rating = str(raw.get("risk_rating", "")).lower()
    return AgentFinding(
        agent=AGENT,
        label=LABEL,
        model_id=state.get("model_id", ""),
        turns=int(state.get("turns", 0)),
        tool_calls=list(state.get("tool_calls") or []),
        input_tokens=int(state.get("input_tokens", 0)),
        output_tokens=int(state.get("output_tokens", 0)),
        headline=f"{rating or 'unknown'} risk, registry status {raw.get('registry_status', 'unknown')}",
        detail=raw.get("rationale", ""),
        severity=_SEVERITY.get(rating, Severity.CAUTION),
        raw=raw,
    )
