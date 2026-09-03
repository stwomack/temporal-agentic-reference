"""Names shared between the workflow, the API, and the UI.

Kept free of heavy imports so the workflow sandbox can import it cheaply.
"""

from __future__ import annotations

EXTRACTION_GRAPH_NAME = "po-extraction"
VENDOR_RISK_GRAPH_NAME = "vendor-risk"
POLICY_GRAPH_NAME = "policy-compliance"
DUPLICATE_GRAPH_NAME = "duplicate-detection"
SUPERVISOR_GRAPH_NAME = "supervisor"

STEP_EXTRACT = "extract"
STEP_VENDOR_RISK = "vendor_risk"
STEP_POLICY = "policy_compliance"
STEP_DUPLICATE = "duplicate_detection"
STEP_SUPERVISOR = "supervisor"
STEP_GUARDRAIL = "guardrail"
STEP_APPROVAL = "approval"
STEP_ERP = "erp"

# The three specialists that fan out concurrently after extraction.
FANOUT_STEPS: list[str] = [STEP_VENDOR_RISK, STEP_POLICY, STEP_DUPLICATE]

STEP_LABELS: dict[str, str] = {
    STEP_EXTRACT: "Extraction agent",
    STEP_VENDOR_RISK: "Vendor risk agent",
    STEP_POLICY: "Policy compliance agent",
    STEP_DUPLICATE: "Duplicate detection agent",
    STEP_SUPERVISOR: "Supervisor agent",
    STEP_GUARDRAIL: "Policy guardrail (deterministic)",
    STEP_APPROVAL: "Human approval",
    STEP_ERP: "ERP submission",
}

STEP_ORDER: list[str] = [
    STEP_EXTRACT,
    STEP_VENDOR_RISK,
    STEP_POLICY,
    STEP_DUPLICATE,
    STEP_SUPERVISOR,
    STEP_GUARDRAIL,
    STEP_APPROVAL,
    STEP_ERP,
]

# How the UI lays the diagram out: each entry is one column, and a column with
# more than one step is drawn as a concurrent fan out.
DIAGRAM_COLUMNS: list[list[str]] = [
    [STEP_EXTRACT],
    FANOUT_STEPS,
    [STEP_SUPERVISOR],
    [STEP_GUARDRAIL],
    [STEP_APPROVAL],
    [STEP_ERP],
]

# Which steps are model backed agents, for the UI to badge them.
AGENT_STEPS: set[str] = {
    STEP_EXTRACT,
    STEP_VENDOR_RISK,
    STEP_POLICY,
    STEP_DUPLICATE,
    STEP_SUPERVISOR,
}
