"""ERP activity tests.

The activity must fail purely on the basis of Temporal's attempt number and
must never retry internally. ActivityEnvironment lets us set the attempt.
"""

from __future__ import annotations

import dataclasses

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from activities.erp import ERPSubmission, submit_to_erp


def submission(seeded_failures: int = 0) -> ERPSubmission:
    return ERPSubmission(
        po_id="PO-TEST0001",
        vendor="Halden Industrial Supply",
        currency="USD",
        amount=6_240.0,
        seeded_failures=seeded_failures,
    )


def run_at_attempt(attempt: int, seeded_failures: int):
    """Run the activity as if Temporal had scheduled the given attempt."""
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, attempt=attempt)
    return env.run(submit_to_erp, submission(seeded_failures))


def test_succeeds_on_first_attempt_when_nothing_is_seeded():
    result = ActivityEnvironment().run(submit_to_erp, submission(0))
    assert result.confirmation_id.startswith("ERP-")
    assert result.attempts_used == 1
    assert result.submitted_amount == 6_240.0


def test_fails_while_inside_the_seeded_window():
    with pytest.raises(ApplicationError) as caught:
        ActivityEnvironment().run(submit_to_erp, submission(2))
    assert caught.value.type == "ERPGatewayTimeout"
    # Retryable, so Temporal will call the activity again.
    assert not caught.value.non_retryable


def test_seeded_failure_message_names_the_attempt():
    with pytest.raises(ApplicationError) as caught:
        ActivityEnvironment().run(submit_to_erp, submission(1))
    assert "attempt 1" in str(caught.value)


def test_succeeds_on_the_attempt_after_the_seeded_window():
    # Two seeded failures means attempts 1 and 2 fail and attempt 3 succeeds,
    # which is exactly what Temporal's RetryPolicy drives in the demo.
    for attempt in (1, 2):
        with pytest.raises(ApplicationError):
            run_at_attempt(attempt, seeded_failures=2)
    result = run_at_attempt(3, seeded_failures=2)
    assert result.attempts_used == 3
    assert result.confirmation_id.startswith("ERP-")


def test_confirmation_ids_are_unique_per_submission():
    first = run_at_attempt(1, seeded_failures=0)
    second = run_at_attempt(1, seeded_failures=0)
    assert first.confirmation_id != second.confirmation_id
