"""Supervisor agent.

The only agent that sees the whole picture. It reads the extracted purchase
order and the three specialist findings, then routes the request: approve it
automatically, escalate it to a human, or reject it.

It is a router, not an enforcer. The deterministic guardrail runs after it and
has the final say on a hard block, so a supervisor that hallucinates an
approval cannot push a blocked vendor through. That ordering is the point: the
agent adds judgment on top of rules it cannot weaken.

No tools. Its input is the specialists' work, and asking it to re-fetch what
they already fetched would only add latency and a chance to disagree with them.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from langgraph.graph import StateGraph
from pydantic import BaseModel, Field

from agents.react_agent import AgentSpec, AgentState, assemble_graph, run_model_turn
from common.models import (
    AgentFinding,
    Severity,
    SupervisorRecommendation,
    SupervisorVerdict,
)

AGENT = "supervisor"
LABEL = "Supervisor agent"

SYSTEM_PROMPT = """You are the procurement supervisor. Three specialist agents \
have reviewed a purchase order request and reported their findings. Decide how \
the request should be routed.

Choose one recommendation:
- auto_approve: no material concerns. Every specialist is clean, or the only \
findings are minor and documented. This is the common case. A request where all \
three specialists report nothing material should be auto approved, not \
escalated.
- escalate_to_human: there is a real concern that a person should weigh. \
Probationary vendors, unresolved policy violations, suspected duplicates or \
splits, or a large amount combined with any concern at all.
- reject: the request cannot proceed on any reading. An unapproved vendor, a \
vendor absent from the registry, or evidence pointing at fraud.

Guidance:
- Weigh the specialists, do not just count them. One critical vendor risk \
outweighs two clean reports.
- When two specialists conflict, prefer the more specific evidence and say in \
your rationale that they disagreed.
- Escalating is cheap and rejecting is expensive. When genuinely torn between \
those two, escalate.
- A deterministic threshold check runs after you and owns spending limits. It \
escalates large amounts on its own, so do not escalate on the amount alone. A \
large request with three clean specialist reports is an auto_approve from you; \
the threshold check will still route it to a human if policy requires one.
- Escalate on evidence, not on caution in general. "Nothing found, but the \
order is sizeable" is not a reason.

Set confidence between 0 and 1. State your reasoning in two or three sentences, \
naming the specialists you relied on. Call SupervisorFinding when decided."""


class SupervisorFinding(BaseModel):
    """The routing decision. Call this once you have weighed the findings."""

    recommendation: str = Field(
        description="One of: auto_approve, escalate_to_human, reject"
    )
    rationale: str = Field(
        description="Two or three sentences naming the specialists you relied on"
    )
    confidence: float = Field(
        description="Your confidence in this routing, between 0.0 and 1.0"
    )
    key_concerns: list[str] = Field(
        default_factory=list, description="The concerns that drove the decision"
    )


_SEVERITY = {
    SupervisorRecommendation.AUTO_APPROVE: Severity.OK,
    SupervisorRecommendation.ESCALATE_TO_HUMAN: Severity.CAUTION,
    SupervisorRecommendation.REJECT: Severity.BLOCKER,
}


SPEC = AgentSpec(
    name=AGENT,
    system_prompt=SYSTEM_PROMPT,
    finding_schema=SupervisorFinding,
)


# The plugin identifies nodes by module and qualified name, so these must live
# at module level rather than inside a factory. See agents/react_agent.py.
def call_model(state: AgentState) -> dict[str, Any]:
    """One Bedrock turn for this agent. Runs as a Temporal Activity."""
    return run_model_turn(SPEC, state)


def build_graph() -> StateGraph:
    return assemble_graph(SPEC, call_model)


def task(extracted: dict[str, Any], findings: Sequence[AgentFinding]) -> str:
    return "\n".join(
        [
            "Purchase order request:",
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
            "",
            "Specialist findings:",
            json.dumps(
                [
                    {
                        "agent": finding.agent,
                        "headline": finding.headline,
                        "rationale": finding.detail,
                        "details": finding.raw,
                    }
                    for finding in findings
                ],
                indent=2,
            ),
        ]
    )


def _parse_recommendation(value: Any) -> SupervisorRecommendation:
    """Map the model's string onto the enum, defaulting to the safe option.

    Escalating on an unrecognized value keeps a malformed answer from either
    auto approving or hard rejecting a request.
    """
    try:
        return SupervisorRecommendation(str(value).strip().lower())
    except ValueError:
        return SupervisorRecommendation.ESCALATE_TO_HUMAN


def to_verdict(state: dict[str, Any]) -> SupervisorVerdict:
    raw = state.get("finding") or {}
    confidence = raw.get("confidence", 0.0)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.0
    return SupervisorVerdict(
        recommendation=_parse_recommendation(raw.get("recommendation")),
        rationale=raw.get("rationale", ""),
        confidence=confidence,
    )


def to_finding(state: dict[str, Any]) -> AgentFinding:
    raw = state.get("finding") or {}
    verdict = to_verdict(state)
    return AgentFinding(
        agent=AGENT,
        label=LABEL,
        model_id=state.get("model_id", ""),
        turns=int(state.get("turns", 0)),
        tool_calls=list(state.get("tool_calls") or []),
        input_tokens=int(state.get("input_tokens", 0)),
        output_tokens=int(state.get("output_tokens", 0)),
        headline=(
            f"{verdict.recommendation.value} "
            f"(confidence {verdict.confidence:.0%})"
        ),
        detail=verdict.rationale,
        severity=_SEVERITY[verdict.recommendation],
        raw=raw,
    )
