"""Stories 2.1 and 2.4: the purchase order approval orchestrator.

Sequence:

    extract   run the LangGraph extraction agent. Its Bedrock node executes as
              a Temporal Activity via LangGraphPlugin.
    guardrail deterministic policy check Activity.
    approval  only when the guardrail asks for it. The workflow parks on
              workflow.wait_condition and is woken by an Update or Signal.
              No polling.
    erp       simulated ERP submission Activity whose retries are handled
              entirely by Temporal's RetryPolicy.

Every step is recorded in `self._steps`, which the `status` query returns. That
one query drives both the live diagram and the telemetry feed in the UI, so the
UI never has to guess where the workflow is.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from temporalio.contrib.langgraph import graph

    from activities.erp import ERPSubmission, submit_to_erp
    from activities.guardrail import check_guardrails
    from common.constants import (
        EXTRACTION_GRAPH_NAME,
        STEP_APPROVAL,
        STEP_ERP,
        STEP_EXTRACT,
        STEP_GUARDRAIL,
        STEP_LABELS,
        STEP_ORDER,
    )
    from common.models import (
        ApprovalDecision,
        ERPResult,
        ExtractedPO,
        GuardrailResult,
        POWorkflowInput,
        POWorkflowResult,
        POWorkflowStatus,
        StepEvent,
        StepStatus,
        WorkflowState,
    )


@workflow.defn
class POApprovalWorkflow:
    @workflow.init
    def __init__(self, workflow_input: POWorkflowInput) -> None:
        # State is built in __init__ so an Update or Signal that arrives before
        # the first activity completes has somewhere to land.
        self._input = workflow_input
        self._state: WorkflowState = WorkflowState.RUNNING
        self._current_step: Optional[str] = None
        self._steps: dict[str, StepEvent] = {
            name: StepEvent(name=name, label=STEP_LABELS[name])
            for name in STEP_ORDER
        }
        self._extracted: Optional[ExtractedPO] = None
        self._guardrail: Optional[GuardrailResult] = None
        self._decision: Optional[ApprovalDecision] = None
        self._erp: Optional[ERPResult] = None
        self._error: str = ""

    # ----------------------------------------------------------------- steps

    def _begin(self, name: str, detail: str = "") -> None:
        step = self._steps[name]
        step.status = StepStatus.RUNNING
        step.started_at = workflow.now()
        step.detail = detail
        self._current_step = name

    def _finish(
        self,
        name: str,
        status: StepStatus = StepStatus.COMPLETED,
        detail: str = "",
        attempts: int = 0,
    ) -> None:
        step = self._steps[name]
        step.status = status
        step.finished_at = workflow.now()
        if step.started_at is not None:
            delta = step.finished_at - step.started_at
            step.latency_ms = int(delta.total_seconds() * 1000)
        if detail:
            step.detail = detail
        if attempts:
            step.attempts = attempts
        if self._current_step == name:
            self._current_step = None

    def _skip(self, name: str, detail: str) -> None:
        step = self._steps[name]
        step.status = StepStatus.SKIPPED
        step.detail = detail

    # ------------------------------------------------- query, update, signal

    @workflow.query(name="status")
    def status(self) -> POWorkflowStatus:
        """Single source of truth for the UI. Read only, as queries must be."""
        return POWorkflowStatus(
            po_id=self._input.po_id,
            scenario=self._input.scenario,
            state=self._state,
            current_step=self._current_step,
            steps=[self._steps[name] for name in STEP_ORDER],
            extracted=self._extracted,
            guardrail=self._guardrail,
            decision=self._decision,
            erp=self._erp,
            error=self._error,
        )

    @workflow.update(name="submit_decision")
    async def submit_decision(self, decision: ApprovalDecision) -> POWorkflowStatus:
        """Deliver the human approve or reject decision.

        An Update rather than a Signal so the caller gets validation errors and
        a confirmation of the resulting state instead of firing blind.
        """
        self._decision = decision
        # Let the main coroutine act on the decision before reporting back.
        await workflow.wait_condition(
            lambda: self._state != WorkflowState.AWAITING_APPROVAL
        )
        return self.status()

    @submit_decision.validator
    def _validate_decision(self, decision: ApprovalDecision) -> None:
        # Validators must not mutate state or block. Rejecting here means the
        # Update never enters workflow history.
        if self._state != WorkflowState.AWAITING_APPROVAL:
            raise ValueError(
                f"Workflow is not awaiting approval (state={self._state.value})"
            )
        if self._decision is not None:
            raise ValueError("A decision has already been recorded")

    @workflow.signal(name="decide")
    def decide(self, decision: ApprovalDecision) -> None:
        """Signal equivalent of submit_decision, for driving the demo from the
        temporal CLI. Ignored once a decision exists."""
        if self._state == WorkflowState.AWAITING_APPROVAL and self._decision is None:
            self._decision = decision

    # -------------------------------------------------------------- run body

    @workflow.run
    async def run(self, workflow_input: POWorkflowInput) -> POWorkflowResult:
        workflow.logger.info(
            "PO workflow starting: po_id=%s scenario=%s",
            workflow_input.po_id,
            workflow_input.scenario.value,
        )

        self._extracted = await self._extract()
        self._guardrail = await self._check_policy(self._extracted)

        if self._guardrail.blocked:
            self._skip(STEP_APPROVAL, "No human override for a policy block")
            self._skip(STEP_ERP, "Not submitted, rejected by policy")
            self._state = WorkflowState.REJECTED_BY_POLICY
            return self._result("Rejected by policy guardrail")

        if self._guardrail.requires_approval:
            approved = await self._await_human_decision()
            if approved is None:
                self._skip(STEP_ERP, "Not submitted, approval timed out")
                self._state = WorkflowState.APPROVAL_TIMED_OUT
                return self._result("No decision received before the deadline")
            if not approved:
                self._skip(STEP_ERP, "Not submitted, rejected by reviewer")
                self._state = WorkflowState.REJECTED_BY_HUMAN
                return self._result("Rejected by human reviewer")
        else:
            self._skip(STEP_APPROVAL, "Within policy, no approval required")

        self._erp = await self._submit_to_erp(self._extracted)
        self._state = WorkflowState.SUBMITTED
        return self._result(
            f"Submitted to ERP as {self._erp.confirmation_id} "
            f"on attempt {self._erp.attempts_used}"
        )

    # ------------------------------------------------------------- the steps

    async def _extract(self) -> ExtractedPO:
        """Story 2.2. `graph()` returns the StateGraph registered on the
        plugin; its extract_po node runs as a Temporal Activity."""
        self._begin(STEP_EXTRACT, "Invoking Bedrock through the LangGraph agent")
        runnable = graph(EXTRACTION_GRAPH_NAME).compile()
        result = await runnable.ainvoke({"raw_text": self._input.raw_text})
        extracted = ExtractedPO.model_validate(result["extracted"])
        self._finish(
            STEP_EXTRACT,
            detail=(
                f"{extracted.model_id}: vendor={extracted.vendor!r} "
                f"total={extracted.currency} {extracted.total_amount:,.2f} "
                f"lines={len(extracted.line_items)} "
                f"tokens={extracted.input_tokens}in/{extracted.output_tokens}out"
            ),
        )
        return extracted

    async def _check_policy(self, extracted: ExtractedPO) -> GuardrailResult:
        """Story 2.3."""
        self._begin(STEP_GUARDRAIL, "Evaluating spend policy")
        result = await workflow.execute_activity(
            check_guardrails,
            args=[extracted, self._input.approval_threshold],
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        self._finish(STEP_GUARDRAIL, detail=result.reason)
        return result

    async def _await_human_decision(self) -> Optional[bool]:
        """Story 2.4. Park until an Update or Signal arrives.

        Returns True for approved, False for rejected, None if the deadline
        passed with no decision. `wait_condition` suspends the workflow with no
        polling and no timer churn.
        """
        step = self._steps[STEP_APPROVAL]
        step.status = StepStatus.WAITING
        step.started_at = workflow.now()
        step.detail = self._guardrail.reason if self._guardrail else "Awaiting decision"
        self._current_step = STEP_APPROVAL
        self._state = WorkflowState.AWAITING_APPROVAL
        workflow.logger.info("Awaiting human decision for %s", self._input.po_id)

        try:
            await workflow.wait_condition(
                lambda: self._decision is not None,
                timeout=timedelta(seconds=self._input.approval_timeout_seconds),
            )
        except asyncio.TimeoutError:
            self._state = WorkflowState.RUNNING
            self._finish(
                STEP_APPROVAL,
                status=StepStatus.FAILED,
                detail=(
                    "No decision within "
                    f"{self._input.approval_timeout_seconds}s"
                ),
            )
            return None

        decision = self._decision
        assert decision is not None
        # Leave AWAITING_APPROVAL before returning so submit_decision's Update
        # handler can complete and report the resulting state.
        self._state = WorkflowState.RUNNING
        self._finish(
            STEP_APPROVAL,
            detail=(
                f"{'Approved' if decision.approved else 'Rejected'} by "
                f"{decision.decided_by}"
                + (f": {decision.comment}" if decision.comment else "")
            ),
        )
        return decision.approved

    async def _submit_to_erp(self, extracted: ExtractedPO) -> ERPResult:
        """Story 2.5. Retries are Temporal's, configured here and nowhere else."""
        seeded = self._input.erp_seeded_failures or 0
        self._begin(
            STEP_ERP,
            f"Submitting to ERP (seeded failures: {seeded})",
        )
        result = await workflow.execute_activity(
            submit_to_erp,
            ERPSubmission(
                po_id=self._input.po_id,
                vendor=extracted.vendor,
                currency=extracted.currency,
                amount=extracted.total_amount,
                seeded_failures=seeded,
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(seconds=5),
                maximum_attempts=self._input.erp_max_attempts,
            ),
        )
        self._finish(
            STEP_ERP,
            detail=(
                f"Confirmation {result.confirmation_id} after "
                f"{result.attempts_used} attempt(s)"
            ),
            attempts=result.attempts_used,
        )
        return result

    def _result(self, summary: str) -> POWorkflowResult:
        self._current_step = None
        workflow.logger.info(
            "PO workflow finished: po_id=%s state=%s",
            self._input.po_id,
            self._state.value,
        )
        return POWorkflowResult(
            po_id=self._input.po_id,
            state=self._state,
            summary=summary,
            extracted=self._extracted,
            guardrail=self._guardrail,
            decision=self._decision,
            erp=self._erp,
        )
