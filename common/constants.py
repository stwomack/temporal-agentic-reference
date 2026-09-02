"""Names shared between the workflow, the API, and the UI.

Kept free of heavy imports so the workflow sandbox can import it cheaply.
"""

from __future__ import annotations

EXTRACTION_GRAPH_NAME = "po-extraction"

STEP_EXTRACT = "extract"
STEP_GUARDRAIL = "guardrail"
STEP_APPROVAL = "approval"
STEP_ERP = "erp"

STEP_LABELS: dict[str, str] = {
    STEP_EXTRACT: "Extraction agent (LangGraph + Bedrock)",
    STEP_GUARDRAIL: "Policy guardrail",
    STEP_APPROVAL: "Human approval",
    STEP_ERP: "ERP submission",
}

STEP_ORDER: list[str] = [STEP_EXTRACT, STEP_GUARDRAIL, STEP_APPROVAL, STEP_ERP]

# Story 4.1: which source location implements each step, so the UI code panel
# can scroll to and highlight the code that is running right now. Line numbers
# are resolved at request time by searching for the function definition, so
# they never drift out of date.
STEP_SOURCE: dict[str, dict[str, str]] = {
    STEP_EXTRACT: {
        "file": "agents/extraction_graph.py",
        "function": "extract_po",
    },
    STEP_GUARDRAIL: {
        "file": "activities/guardrail.py",
        "function": "check_guardrails",
    },
    STEP_APPROVAL: {
        "file": "workflows/po_approval.py",
        "function": "_await_human_decision",
    },
    STEP_ERP: {
        "file": "activities/erp.py",
        "function": "submit_to_erp",
    },
}
