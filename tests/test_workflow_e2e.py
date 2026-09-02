"""End to end workflow tests against a real Temporal server and real Bedrock.

Each test starts its own worker on a unique task queue so runs cannot collide.
Both fixtures skip rather than fake anything: no Temporal server, or no Bedrock
model access, means skipped.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import uuid
from contextlib import asynccontextmanager

import pytest
from temporalio.worker import Worker

from activities.erp import submit_to_erp
from activities.guardrail import check_guardrails
from common.models import (
    ApprovalDecision,
    POWorkflowInput,
    Scenario,
    WorkflowState,
)
from common.scenarios import SCENARIOS
from common.temporal_client import build_workflow_runner
from workflows.po_approval import POApprovalWorkflow

pytestmark = pytest.mark.usefixtures("live_bedrock")


@asynccontextmanager
async def running_worker(client, task_queue: str):
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        worker = Worker(
            client,
            task_queue=task_queue,
            workflows=[POApprovalWorkflow],
            activities=[check_guardrails, submit_to_erp],
            activity_executor=executor,
            workflow_runner=build_workflow_runner(),
        )
        async with worker:
            yield


def workflow_input(scenario: Scenario, **overrides) -> POWorkflowInput:
    spec = SCENARIOS[scenario]
    values = {
        "po_id": f"PO-TEST-{uuid.uuid4().hex[:8].upper()}",
        "raw_text": spec.raw_text,
        "scenario": scenario,
        "approval_threshold": spec.approval_threshold,
        "erp_seeded_failures": spec.erp_seeded_failures,
        "erp_max_attempts": 5,
        "approval_timeout_seconds": 60,
    }
    values.update(overrides)
    return POWorkflowInput(**values)


async def start(client, scenario: Scenario, task_queue: str, **overrides):
    return await client.start_workflow(
        POApprovalWorkflow.run,
        workflow_input(scenario, **overrides),
        id=f"test-{uuid.uuid4()}",
        task_queue=task_queue,
    )


async def wait_for_approval(handle, timeout: float = 45.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        status = await handle.query(POApprovalWorkflow.status)
        if status.state is WorkflowState.AWAITING_APPROVAL:
            return
        await asyncio.sleep(0.2)
    raise AssertionError("workflow never reached awaiting_approval")


async def test_happy_path_submits_to_erp(temporal_client):
    task_queue = f"test-{uuid.uuid4()}"
    async with running_worker(temporal_client, task_queue):
        handle = await start(temporal_client, Scenario.HAPPY_PATH, task_queue)
        result = await handle.result()
        # Queries are served by a worker, so they must happen before the worker
        # shuts down.
        status = await handle.query(POApprovalWorkflow.status)

    assert result.state is WorkflowState.SUBMITTED
    assert result.erp is not None
    assert result.erp.attempts_used == 1
    assert result.extracted is not None
    # Live model output, not a canned value.
    assert result.extracted.output_tokens > 0
    assert result.guardrail is not None
    assert not result.guardrail.requires_approval

    by_name = {step.name: step for step in status.steps}
    assert by_name["extract"].status.value == "completed"
    assert by_name["approval"].status.value == "skipped"
    assert by_name["erp"].status.value == "completed"
    assert by_name["extract"].latency_ms is not None


async def test_blocked_vendor_never_reaches_erp(temporal_client):
    task_queue = f"test-{uuid.uuid4()}"
    async with running_worker(temporal_client, task_queue):
        handle = await start(temporal_client, Scenario.GUARDRAIL_VIOLATION, task_queue)
        result = await handle.result()
        status = await handle.query(POApprovalWorkflow.status)

    assert result.state is WorkflowState.REJECTED_BY_POLICY
    assert result.erp is None
    assert result.guardrail is not None and result.guardrail.blocked

    by_name = {step.name: step for step in status.steps}
    assert by_name["erp"].status.value == "skipped"


async def test_human_approval_resumes_and_submits(temporal_client):
    task_queue = f"test-{uuid.uuid4()}"
    async with running_worker(temporal_client, task_queue):
        handle = await start(temporal_client, Scenario.HUMAN_APPROVAL, task_queue)
        await wait_for_approval(handle)
        await handle.execute_update(
            POApprovalWorkflow.submit_decision,
            ApprovalDecision(approved=True, decided_by="pytest", comment="ok"),
        )
        result = await handle.result()

    assert result.state is WorkflowState.SUBMITTED
    assert result.decision is not None and result.decision.approved
    assert result.erp is not None


async def test_human_rejection_ends_in_a_distinct_state(temporal_client):
    task_queue = f"test-{uuid.uuid4()}"
    async with running_worker(temporal_client, task_queue):
        handle = await start(temporal_client, Scenario.HUMAN_REJECTION, task_queue)
        await wait_for_approval(handle)
        await handle.execute_update(
            POApprovalWorkflow.submit_decision,
            ApprovalDecision(approved=False, decided_by="pytest", comment="no"),
        )
        result = await handle.result()

    assert result.state is WorkflowState.REJECTED_BY_HUMAN
    assert result.erp is None


async def test_decision_is_rejected_before_the_workflow_is_waiting(temporal_client):
    task_queue = f"test-{uuid.uuid4()}"
    async with running_worker(temporal_client, task_queue):
        handle = await start(temporal_client, Scenario.HUMAN_APPROVAL, task_queue)
        with pytest.raises(Exception) as caught:
            await handle.execute_update(
                POApprovalWorkflow.submit_decision,
                ApprovalDecision(approved=True, decided_by="pytest"),
            )
        assert "not awaiting approval" in str(caught.value.cause or caught.value)

        # The workflow is unaffected and still completes normally.
        await wait_for_approval(handle)
        await handle.execute_update(
            POApprovalWorkflow.submit_decision,
            ApprovalDecision(approved=True, decided_by="pytest"),
        )
        result = await handle.result()
    assert result.state is WorkflowState.SUBMITTED


async def test_seeded_erp_failures_are_retried_by_temporal(temporal_client):
    task_queue = f"test-{uuid.uuid4()}"
    async with running_worker(temporal_client, task_queue):
        handle = await start(temporal_client, Scenario.ERP_RETRY, task_queue)
        result = await handle.result()
        status = await handle.query(POApprovalWorkflow.status)

    assert result.state is WorkflowState.SUBMITTED
    assert result.erp is not None
    # Two seeded failures, so the third attempt is the one that succeeds.
    assert result.erp.attempts_used == 3

    by_name = {step.name: step for step in status.steps}
    assert by_name["erp"].attempts == 3


async def test_approval_deadline_expires_without_a_decision(temporal_client):
    task_queue = f"test-{uuid.uuid4()}"
    async with running_worker(temporal_client, task_queue):
        handle = await start(
            temporal_client,
            Scenario.HUMAN_APPROVAL,
            task_queue,
            approval_timeout_seconds=3,
        )
        result = await handle.result()
        status = await handle.query(POApprovalWorkflow.status)

    assert result.state is WorkflowState.APPROVAL_TIMED_OUT
    assert result.erp is None

    by_name = {step.name: step for step in status.steps}
    assert by_name["approval"].status.value == "failed"
    assert by_name["erp"].status.value == "skipped"
