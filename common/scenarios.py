"""The five demo scenarios the UI can trigger.

Each scenario is a real purchase order request plus the knobs that steer the
workflow down a particular path. The extraction step is a live Bedrock call in
every one of them; only the policy thresholds and the seeded ERP failure count
differ.
"""

from __future__ import annotations

from pydantic import BaseModel

from common.models import Scenario


class ScenarioSpec(BaseModel):
    scenario: Scenario
    title: str
    description: str
    expected_outcome: str
    raw_text: str
    approval_threshold: float
    erp_seeded_failures: int


HAPPY_PATH_TEXT = """Purchase order request from Dana Whitfield, Beverage Ops.
Vendor: Northwind Packaging Co.
Please order 12,000 aluminum can lids at $0.18 each and 400 corrugated pallet
sleeves at $6.25 each. Total comes to $4,660.00.
Needed for the line changeover on the 20th."""

LARGE_ORDER_TEXT = """Purchase order request from Marcus Oyelaran, Plant
Engineering, Site 14.
Vendor: Continental Filling Systems
One refurbished rotary filler head assembly, unit price $86,400.00, plus
installation labor of $9,750.00 and a two year service contract at $12,300.00.
Order total $108,450.00. Capital project CX-2291."""

BLOCKED_VENDOR_TEXT = """Purchase order request from Priya Raghunathan,
Regional Logistics.
Vendor: Acme Shell Corp
Expedited freight brokerage for 6 truckloads, $3,200.00 per load, order total
$19,200.00. Vendor was recommended by a broker; no prior contract on file."""

PROBATIONARY_TEXT = """Purchase order request from Priya Raghunathan,
Regional Logistics.
Vendor: Pinnacle Cold Chain Ltd
Refrigerated linehaul for the September schedule, four lanes at $2,150.00 per
lane. Order total $8,600.00. Same lanes we have been running since spring."""

DUPLICATE_TEXT = """Purchase order request from Dana Whitfield, Beverage Ops.
Vendor: Northwind Packaging Co.
Need 240 rolls of shrink film and 180 cases of printed case tape for the export
line. Quarterly bulk buy, order total $18,240.00. Please expedite, the last one
may not have gone through."""

RETRY_TEXT = """Purchase order request from Tomas Lindqvist, Maintenance.
Vendor: Halden Industrial Supply
Order 60 cases of food grade chain lubricant at $74.50 per case and 15
replacement conveyor belts at $118.00 each. Order total $6,240.00.
Routine restock for the Q4 maintenance window."""


SCENARIOS: dict[Scenario, ScenarioSpec] = {
    Scenario.HAPPY_PATH: ScenarioSpec(
        scenario=Scenario.HAPPY_PATH,
        title="Happy path",
        description=(
            "A routine PO well under the approval threshold. Extraction, "
            "guardrail, ERP submission, done."
        ),
        expected_outcome="submitted",
        raw_text=HAPPY_PATH_TEXT,
        approval_threshold=10_000.0,
        erp_seeded_failures=0,
    ),
    Scenario.GUARDRAIL_VIOLATION: ScenarioSpec(
        scenario=Scenario.GUARDRAIL_VIOLATION,
        title="Guardrail violation",
        description=(
            "A blocked vendor. The guardrail rejects the PO outright, with no "
            "human override path, and the ERP step never runs."
        ),
        expected_outcome="rejected_by_policy",
        raw_text=BLOCKED_VENDOR_TEXT,
        approval_threshold=10_000.0,
        erp_seeded_failures=0,
    ),
    Scenario.HUMAN_APPROVAL: ScenarioSpec(
        scenario=Scenario.HUMAN_APPROVAL,
        title="Human in the loop: approve",
        description=(
            "A capital order over the approval threshold. The workflow parks "
            "on wait_condition until an Update carries the decision, then "
            "submits."
        ),
        expected_outcome="submitted",
        raw_text=LARGE_ORDER_TEXT,
        approval_threshold=25_000.0,
        erp_seeded_failures=0,
    ),
    Scenario.HUMAN_REJECTION: ScenarioSpec(
        scenario=Scenario.HUMAN_REJECTION,
        title="Human in the loop: reject",
        description=(
            "Same pause as the approve path, but the reviewer rejects. The "
            "workflow ends in a distinct state and never calls ERP."
        ),
        expected_outcome="rejected_by_human",
        raw_text=LARGE_ORDER_TEXT,
        approval_threshold=25_000.0,
        erp_seeded_failures=0,
    ),
    Scenario.AGENT_ESCALATION: ScenarioSpec(
        scenario=Scenario.AGENT_ESCALATION,
        title="Agent judgment escalation",
        description=(
            "Well under the spending threshold, so the deterministic guardrail "
            "passes it. The vendor risk and policy agents find a probationary "
            "vendor with open cold chain findings, and the supervisor "
            "escalates anyway."
        ),
        expected_outcome="awaiting_approval",
        raw_text=PROBATIONARY_TEXT,
        approval_threshold=10_000.0,
        erp_seeded_failures=0,
    ),
    Scenario.DUPLICATE_REQUEST: ScenarioSpec(
        scenario=Scenario.DUPLICATE_REQUEST,
        title="Duplicate catch",
        description=(
            "A re-submission of an order already placed nine days ago, with "
            "different wording and a slightly different total. No rule catches "
            "this. The duplicate detection agent reads the history and does."
        ),
        expected_outcome="awaiting_approval",
        raw_text=DUPLICATE_TEXT,
        approval_threshold=25_000.0,
        erp_seeded_failures=0,
    ),
    Scenario.ERP_RETRY: ScenarioSpec(
        scenario=Scenario.ERP_RETRY,
        title="Failure and retry",
        description=(
            "The ERP gateway fails the first two attempts. Temporal's "
            "RetryPolicy retries with backoff and the third attempt succeeds. "
            "No retry code in the activity."
        ),
        expected_outcome="submitted",
        raw_text=RETRY_TEXT,
        approval_threshold=10_000.0,
        erp_seeded_failures=2,
    ),
}
