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

# Story 4.1: which source location implements each step, so the UI code panel
# can scroll to and highlight the code that is running right now. Line numbers
# are resolved at request time by searching for the function definition, so
# they never drift out of date.
STEP_SOURCE: dict[str, dict[str, str]] = {
    STEP_EXTRACT: {
        "file": "agents/extraction_graph.py",
        "symbol": "extract_po",
    },
    STEP_VENDOR_RISK: {
        "file": "agents/vendor_risk.py",
        "symbol": "SYSTEM_PROMPT",
    },
    STEP_POLICY: {
        "file": "agents/policy_compliance.py",
        "symbol": "SYSTEM_PROMPT",
    },
    STEP_DUPLICATE: {
        "file": "agents/duplicate_detection.py",
        "symbol": "SYSTEM_PROMPT",
    },
    STEP_SUPERVISOR: {
        "file": "agents/supervisor.py",
        "symbol": "SYSTEM_PROMPT",
    },
    STEP_GUARDRAIL: {
        "file": "activities/guardrail.py",
        "symbol": "check_guardrails",
    },
    STEP_APPROVAL: {
        "file": "workflows/po_approval.py",
        "symbol": "_await_human_decision",
    },
    STEP_ERP: {
        "file": "activities/erp.py",
        "symbol": "submit_to_erp",
    },
}
