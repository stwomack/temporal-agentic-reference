"""Shared data contracts.

These models cross the workflow, activity, and HTTP boundaries. They are
Pydantic models and the Temporal client/worker are configured with
`pydantic_data_converter`, so they serialize without custom converters.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class Scenario(str, Enum):
    """The demo scenarios that the UI can trigger."""

    HAPPY_PATH = "happy_path"
    GUARDRAIL_VIOLATION = "guardrail_violation"
    HUMAN_APPROVAL = "human_approval"
    HUMAN_REJECTION = "human_rejection"
    ERP_RETRY = "erp_retry"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    WAITING = "waiting"


class WorkflowState(str, Enum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUBMITTED = "submitted"
    REJECTED_BY_POLICY = "rejected_by_policy"
    REJECTED_BY_HUMAN = "rejected_by_human"
    APPROVAL_TIMED_OUT = "approval_timed_out"
    FAILED = "failed"


class LineItem(BaseModel):
    description: str = ""
    quantity: float = 0.0
    unit_price: float = 0.0
    amount: float = 0.0


class ExtractedPO(BaseModel):
    """Structured fields the extraction agent pulls out of the raw request."""

    vendor: str = ""
    requester: str = ""
    currency: str = "USD"
    total_amount: float = 0.0
    line_items: list[LineItem] = Field(default_factory=list)
    notes: str = ""

    # Provenance so the UI can prove the Bedrock call was real.
    model_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class GuardrailResult(BaseModel):
    requires_approval: bool = False
    blocked: bool = False
    reason: str = ""
    approval_threshold: float = 0.0
    hard_block_threshold: float = 0.0


class ERPResult(BaseModel):
    confirmation_id: str = ""
    attempts_used: int = 0
    submitted_amount: float = 0.0
    vendor: str = ""


class ApprovalDecision(BaseModel):
    approved: bool
    decided_by: str = "demo-operator"
    comment: str = ""


class StepEvent(BaseModel):
    """One entry in the telemetry feed and one box in the UI diagram."""

    name: str
    label: str
    status: StepStatus = StepStatus.PENDING
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    latency_ms: Optional[int] = None
    detail: str = ""
    attempts: int = 0


class POWorkflowInput(BaseModel):
    po_id: str
    raw_text: str
    scenario: Scenario = Scenario.HAPPY_PATH
    # Per run overrides so the UI can drive every scenario without restarting
    # the worker. When None, the value from common.config is used.
    approval_threshold: Optional[float] = None
    erp_seeded_failures: Optional[int] = None
    # Values that drive workflow control flow are carried in the input rather
    # than read from the environment inside the workflow. That keeps replay
    # deterministic even if a worker restarts with different env vars.
    erp_max_attempts: int = 5
    approval_timeout_seconds: int = 900


class POWorkflowStatus(BaseModel):
    """Everything the UI needs, returned by a single workflow query."""

    po_id: str
    scenario: Scenario
    state: WorkflowState
    current_step: Optional[str] = None
    steps: list[StepEvent] = Field(default_factory=list)
    extracted: Optional[ExtractedPO] = None
    guardrail: Optional[GuardrailResult] = None
    decision: Optional[ApprovalDecision] = None
    erp: Optional[ERPResult] = None
    error: str = ""


class POWorkflowResult(BaseModel):
    po_id: str
    state: WorkflowState
    summary: str
    extracted: Optional[ExtractedPO] = None
    guardrail: Optional[GuardrailResult] = None
    decision: Optional[ApprovalDecision] = None
    erp: Optional[ERPResult] = None


class StartRequest(BaseModel):
    scenario: Scenario = Scenario.HAPPY_PATH
    raw_text: Optional[str] = None
    approval_threshold: Optional[float] = None
    erp_seeded_failures: Optional[int] = None


class StatusResponse(BaseModel):
    workflow_id: str
    run_id: str
    temporal_ui_url: str
    execution_status: str
    pending_activities: list[dict[str, Any]] = Field(default_factory=list)
    status: Optional[POWorkflowStatus] = None
