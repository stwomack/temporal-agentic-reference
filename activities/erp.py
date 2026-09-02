"""Story 2.5: simulated ERP submission with a seeded failure count.

The point of this activity is to show Temporal's native retry behavior. There
is deliberately no retry loop, no sleep-and-try-again, and no attempt counter
of its own in here. The activity looks at `activity.info().attempt`, which
Temporal supplies, and fails while that attempt number is inside the seeded
failure window. The RetryPolicy on the workflow side decides when to try again.

Watch it in the Temporal Web UI: the pending activity shows Attempt 2 of N with
the last failure message, then an ActivityTaskCompleted event once the seeded
window is past.
"""

from __future__ import annotations

import time
import uuid

from pydantic import BaseModel
from temporalio import activity
from temporalio.exceptions import ApplicationError

from common.models import ERPResult

# Enough delay that a human watching the UI can see the step in flight.
SIMULATED_LATENCY_SECONDS = 0.75


class ERPSubmission(BaseModel):
    po_id: str
    vendor: str
    currency: str
    amount: float
    seeded_failures: int = 0


@activity.defn
def submit_to_erp(submission: ERPSubmission) -> ERPResult:
    """Submit a PO to the (simulated) ERP system.

    Fails on attempts 1..seeded_failures, succeeds on attempt
    seeded_failures + 1. Temporal owns the retrying.
    """
    attempt = activity.info().attempt
    activity.logger.info(
        "ERP submit attempt %d for %s (seeded_failures=%d)",
        attempt,
        submission.po_id,
        submission.seeded_failures,
    )

    time.sleep(SIMULATED_LATENCY_SECONDS)

    if attempt <= submission.seeded_failures:
        # Retryable on purpose. Temporal's RetryPolicy backs off and calls us
        # again; nothing in this function schedules the next attempt.
        raise ApplicationError(
            f"ERP gateway timeout on attempt {attempt}. Seeded to fail the "
            f"first {submission.seeded_failures} attempt(s).",
            type="ERPGatewayTimeout",
        )

    confirmation_id = f"ERP-{uuid.uuid4().hex[:10].upper()}"
    activity.logger.info(
        "ERP submit succeeded on attempt %d: %s", attempt, confirmation_id
    )
    return ERPResult(
        confirmation_id=confirmation_id,
        attempts_used=attempt,
        submitted_amount=submission.amount,
        vendor=submission.vendor,
    )
