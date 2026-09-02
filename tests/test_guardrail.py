"""Guardrail policy tests. Fully deterministic, no Bedrock and no Temporal
server needed. ActivityEnvironment supplies the activity context the logger
and activity.info() require.
"""

from __future__ import annotations

import pytest
from temporalio.testing import ActivityEnvironment

from activities.guardrail import check_guardrails
from common.config import get_settings
from common.models import ExtractedPO


def run_guardrail(extracted: ExtractedPO, threshold: float | None = None):
    return ActivityEnvironment().run(check_guardrails, extracted, threshold)


def po(vendor: str = "Northwind Packaging Co.", amount: float = 1_000.0) -> ExtractedPO:
    return ExtractedPO(vendor=vendor, requester="Dana Whitfield", total_amount=amount)


def test_under_threshold_is_clean():
    result = run_guardrail(po(amount=4_660.0), 10_000.0)
    assert not result.requires_approval
    assert not result.blocked
    assert "within the approval threshold" in result.reason


def test_over_threshold_requires_approval():
    result = run_guardrail(po(amount=108_450.0), 25_000.0)
    assert result.requires_approval
    assert not result.blocked
    assert result.approval_threshold == 25_000.0


def test_exactly_at_threshold_is_clean():
    # The policy is "exceeds", so the boundary itself must not trip it.
    result = run_guardrail(po(amount=10_000.0), 10_000.0)
    assert not result.requires_approval


def test_blocked_vendor_is_rejected_without_human_path():
    result = run_guardrail(po(vendor="Acme Shell Corp", amount=100.0), 10_000.0)
    assert result.blocked
    assert not result.requires_approval


def test_blocked_vendor_match_is_case_insensitive():
    result = run_guardrail(po(vendor="ACME SHELL CORP", amount=100.0), 10_000.0)
    assert result.blocked


def test_above_hard_block_threshold_is_rejected():
    hard = get_settings().po_hard_block_threshold
    result = run_guardrail(po(amount=hard + 1), 10_000.0)
    assert result.blocked
    assert not result.requires_approval


def test_threshold_falls_back_to_configuration():
    configured = get_settings().po_approval_threshold
    result = run_guardrail(po(amount=configured + 1), None)
    assert result.requires_approval
    assert result.approval_threshold == configured


@pytest.mark.parametrize("amount", [0.0, -5.0])
def test_non_positive_amounts_are_clean_not_blocked(amount: float):
    result = run_guardrail(po(amount=amount), 10_000.0)
    assert not result.blocked
    assert not result.requires_approval
