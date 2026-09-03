"""The purchase order approval orchestrator.

Temporal is the multi-agent orchestrator here. Five model-backed agents run
under it, and the fan out, the retries, and the durability of partial results
all belong to Temporal rather than to any agent framework.

    extract       Extraction agent reads the raw request.

    fan out       Three specialists run concurrently, each its own LangGraph
                  agent with its own model, timeout, and retry policy:
                  vendor risk (tool using), policy compliance, and duplicate
                  detection (tool using). They dispatch on a single workflow
                  task, so history shows three activities in flight at once.
                  If the worker dies mid fan out, the specialists that already
                  finished are not re-run and their Bedrock calls are not paid
                  for twice.

    supervisor    Reads all three findings and routes the request.

    guardrail     Deterministic threshold and blocked vendor check. It runs
                  after the supervisor and outranks it: an agent cannot talk a
                  hard block into an approval. The agents add judgment on top
                  of rules they cannot weaken.

    approval      Human decision, delivered by Temporal Update or Signal, only
                  when the supervisor escalates or the guardrail asks for it.

    erp           Simulated submission whose retries are Temporal's.

Every step is recorded in `self._steps`, which the `status` query returns. That
one query drives the diagram, the agent findings, and the telemetry feed in the
UI, so the UI cannot disagree with the workflow.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Callable, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from temporalio.contrib.langgraph import graph

    from activities.erp import ERPSubmission, submit_to_erp
    from activities.guardrail import check_guardrails
    from agents import (
        duplicate_detection,
        policy_compliance,
        vendor_risk,
    )
    from agents import (
        supervisor as supervisor_agent,
    )
    from common.constants import (
        DUPLICATE_GRAPH_NAME,
        EXTRACTION_GRAPH_NAME,
        POLICY_GRAPH_NAME,
        STEP_APPROVAL,
        STEP_DUPLICATE,
        STEP_ERP,
        STEP_EXTRACT,
        STEP_GUARDRAIL,
        STEP_LABELS,
        STEP_ORDER,
        STEP_POLICY,
        STEP_SUPERVISOR,
        STEP_VENDOR_RISK,
        SUPERVISOR_GRAPH_NAME,
        VENDOR_RISK_GRAPH_NAME,
    )
    from common.models import (
        AgentFinding,
        ApprovalDecision,
        ERPResult,
        ExtractedPO,
        GuardrailResult,
        POWorkflowInput,
        POWorkflowResult,
        POWorkflowStatus,
        StepEvent,
        StepStatus,
        SupervisorRecommendation,
        SupervisorVerdict,
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
        self._findings: dict[str, AgentFinding] = {}
        self._supervisor: Optional[SupervisorVerdict] = None
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
            agent_findings=[
                self._findings[name] for name in STEP_ORDER if name in self._findings
            ],
            supervisor=self._supervisor,
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
        await self._run_specialists(self._extracted)
        self._supervisor = await self._supervise(self._extracted)
        self._guardrail = await self._check_policy(self._extracted)

        # The deterministic guardrail outranks the supervisor. An agent can
        # escalate or reject, but it cannot approve past a hard block.
        if self._guardrail.blocked:
            self._skip(STEP_APPROVAL, "No human override for a policy block")
            self._skip(STEP_ERP, "Not submitted, rejected by policy")
            self._state = WorkflowState.REJECTED_BY_POLICY
            return self._result("Rejected by the deterministic policy guardrail")

        recommendation = self._supervisor.recommendation
        if recommendation is SupervisorRecommendation.REJECT:
            self._skip(STEP_APPROVAL, "Supervisor rejected outright")
            self._skip(STEP_ERP, "Not submitted, rejected by the supervisor agent")
            self._state = WorkflowState.REJECTED_BY_SUPERVISOR
            return self._result(
                f"Rejected by the supervisor agent: {self._supervisor.rationale}"
            )

        needs_human = (
            recommendation is SupervisorRecommendation.ESCALATE_TO_HUMAN
            or self._guardrail.requires_approval
        )
        if needs_human:
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
            self._skip(
                STEP_APPROVAL,
                "Supervisor auto approved and the request is within policy",
            )

        self._erp = await self._submit_to_erp(self._extracted)
        self._state = WorkflowState.SUBMITTED
        return self._result(
            f"Submitted to ERP as {self._erp.confirmation_id} "
            f"on attempt {self._erp.attempts_used}"
        )

    # ------------------------------------------------------------- the steps

    async def _extract(self) -> ExtractedPO:
        """`graph()` returns the StateGraph registered on the plugin; its
        extract_po node runs as a Temporal Activity."""
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

    async def _run_agent(
        self,
        step: str,
        graph_name: str,
        task: str,
        to_finding: Callable[[dict[str, Any]], AgentFinding],
    ) -> AgentFinding:
        """Run one specialist agent end to end and record it as a step."""
        self._begin(step, "Running")
        runnable = graph(graph_name).compile()
        state = await runnable.ainvoke({"task": task})
        finding = to_finding(state)
        self._findings[step] = finding
        tools = ", ".join(finding.tool_calls) or "none"
        self._finish(
            step,
            detail=(
                f"{finding.headline} | {finding.turns} turn(s), tools: {tools}, "
                f"tokens={finding.input_tokens}in/{finding.output_tokens}out"
            ),
        )
        return finding

    async def _run_specialists(self, extracted: ExtractedPO) -> None:
        """Fan out the three specialists concurrently.

        asyncio.gather in a workflow is deterministic: the SDK schedules all
        three activities from one workflow task and replays their completions
        in recorded order. The visible effect in the Temporal UI is three
        activities in flight at once, and the durable effect is that a worker
        crash mid fan out does not re-run the ones that already finished.
        """
        vendor_context = ""
        self._current_step = STEP_VENDOR_RISK

        findings = await asyncio.gather(
            self._run_agent(
                STEP_VENDOR_RISK,
                VENDOR_RISK_GRAPH_NAME,
                vendor_risk.task(extracted.model_dump()),
                vendor_risk.to_finding,
            ),
            self._run_agent(
                STEP_POLICY,
                POLICY_GRAPH_NAME,
                policy_compliance.task(extracted.model_dump(), vendor_context),
                policy_compliance.to_finding,
            ),
            self._run_agent(
                STEP_DUPLICATE,
                DUPLICATE_GRAPH_NAME,
                duplicate_detection.task(extracted.model_dump()),
                duplicate_detection.to_finding,
            ),
        )
        workflow.logger.info(
            "specialists complete: %s",
            ", ".join(f"{f.agent}={f.severity.value}" for f in findings),
        )

    async def _supervise(self, extracted: ExtractedPO) -> SupervisorVerdict:
        """The routing agent. Reads the specialists, decides where this goes."""
        ordered = [
            self._findings[name]
            for name in (STEP_VENDOR_RISK, STEP_POLICY, STEP_DUPLICATE)
            if name in self._findings
        ]
        self._begin(STEP_SUPERVISOR, "Weighing the specialist findings")
        runnable = graph(SUPERVISOR_GRAPH_NAME).compile()
        state = await runnable.ainvoke(
            {"task": supervisor_agent.task(extracted.model_dump(), ordered)}
        )
        verdict = supervisor_agent.to_verdict(state)
        finding = supervisor_agent.to_finding(state)
        self._findings[STEP_SUPERVISOR] = finding
        self._finish(
            STEP_SUPERVISOR,
            detail=(
                f"{verdict.recommendation.value} "
                f"(confidence {verdict.confidence:.0%}): {verdict.rationale}"
            ),
        )
        return verdict

    async def _check_policy(self, extracted: ExtractedPO) -> GuardrailResult:
        """Deterministic threshold and blocked vendor check."""
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
        """Park until an Update or Signal arrives.

        Returns True for approved, False for rejected, None if the deadline
        passed with no decision. `wait_condition` suspends the workflow with no
        polling and no timer churn.
        """
        step = self._steps[STEP_APPROVAL]
        step.status = StepStatus.WAITING
        step.started_at = workflow.now()
        step.detail = self._approval_reason()
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

    def _approval_reason(self) -> str:
        """Why a human is being asked, which can be the agent or the rule."""
        reasons = []
        if (
            self._supervisor
            and self._supervisor.recommendation
            is SupervisorRecommendation.ESCALATE_TO_HUMAN
        ):
            reasons.append(f"Supervisor agent escalated: {self._supervisor.rationale}")
        if self._guardrail and self._guardrail.requires_approval:
            reasons.append(self._guardrail.reason)
        return " ".join(reasons) or "Awaiting decision"

    async def _submit_to_erp(self, extracted: ExtractedPO) -> ERPResult:
        """Retries are Temporal's, configured here and nowhere else."""
        seeded = self._input.erp_seeded_failures or 0
        self._begin(STEP_ERP, f"Submitting to ERP (seeded failures: {seeded})")
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
            agent_findings=[
                self._findings[name] for name in STEP_ORDER if name in self._findings
            ],
            supervisor=self._supervisor,
            guardrail=self._guardrail,
            decision=self._decision,
            erp=self._erp,
        )
