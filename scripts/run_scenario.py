"""Command line driver for the demo scenarios.

Useful for testing without the UI:

    uv run python scripts/run_scenario.py happy_path
    uv run python scripts/run_scenario.py human_approval --decision approve
    uv run python scripts/run_scenario.py erp_retry
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.preflight import require_dependencies  # noqa: E402

require_dependencies()

from common.config import get_settings  # noqa: E402
from common.models import (  # noqa: E402
    ApprovalDecision,
    POWorkflowInput,
    Scenario,
    WorkflowState,
)
from common.scenarios import SCENARIOS  # noqa: E402
from common.temporal_client import TemporalConnectionError, connect  # noqa: E402
from workflows.po_approval import POApprovalWorkflow  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=[s.value for s in Scenario])
    parser.add_argument(
        "--decision",
        choices=["approve", "reject"],
        default=None,
        help="Decision to send when the workflow pauses for approval.",
    )
    args = parser.parse_args()

    settings = get_settings()
    spec = SCENARIOS[Scenario(args.scenario)]
    decision = args.decision
    if decision is None:
        decision = "reject" if spec.scenario is Scenario.HUMAN_REJECTION else "approve"

    po_id = f"PO-{uuid.uuid4().hex[:8].upper()}"
    try:
        client = await connect()
    except TemporalConnectionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    handle = await client.start_workflow(
        POApprovalWorkflow.run,
        POWorkflowInput(
            po_id=po_id,
            raw_text=spec.raw_text,
            scenario=spec.scenario,
            approval_threshold=spec.approval_threshold,
            erp_seeded_failures=spec.erp_seeded_failures,
            erp_max_attempts=settings.erp_max_attempts,
            approval_timeout_seconds=settings.approval_timeout_seconds,
        ),
        id=f"po-{po_id}",
        task_queue=settings.temporal_task_queue,
    )
    print(f"started workflow {handle.id} (scenario={spec.scenario.value})")

    # Wait for the pause, if this scenario has one, then deliver the decision.
    for _ in range(120):
        status = await handle.query(POApprovalWorkflow.status)
        if status.state is WorkflowState.AWAITING_APPROVAL:
            print(f"awaiting approval: {status.guardrail.reason if status.guardrail else ''}")
            await handle.execute_update(
                POApprovalWorkflow.submit_decision,
                ApprovalDecision(
                    approved=decision == "approve",
                    decided_by="cli-operator",
                    comment=f"{decision} via scripts/run_scenario.py",
                ),
            )
            print(f"sent decision: {decision}")
            break
        if status.state not in (WorkflowState.RUNNING,):
            break
        await asyncio.sleep(0.5)

    result = await handle.result()
    print(f"\nfinal state: {result.state.value}")
    print(f"summary:     {result.summary}")
    if result.extracted:
        print(
            f"extracted:   vendor={result.extracted.vendor!r} "
            f"total={result.extracted.currency} {result.extracted.total_amount:,.2f} "
            f"model={result.extracted.model_id}"
        )
    print("\nsteps:")
    status = await handle.query(POApprovalWorkflow.status)
    for step in status.steps:
        latency = f"{step.latency_ms}ms" if step.latency_ms is not None else "-"
        print(f"  {step.name:<10} {step.status.value:<10} {latency:>8}  {step.detail}")
    return 0 if result.state is not WorkflowState.FAILED else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
