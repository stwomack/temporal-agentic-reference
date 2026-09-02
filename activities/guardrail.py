"""Story 2.3: deterministic spend guardrail.

This step is intentionally rule based, not model based. The extraction agent is
the probabilistic component; policy enforcement should be auditable and produce
the same answer every time for the same input.

Three outcomes:

  clean             under the approval threshold, goes straight to ERP
  requires_approval over the approval threshold, needs a human decision
  blocked           blocked vendor, or over the hard block threshold, rejected
                    outright with no human path

Thresholds and the blocked vendor list come from the environment. See
common/config.py.
"""

from __future__ import annotations

from temporalio import activity

from common.config import get_settings
from common.models import ExtractedPO, GuardrailResult


@activity.defn
def check_guardrails(
    extracted: ExtractedPO, approval_threshold: float | None = None
) -> GuardrailResult:
    """Evaluate a PO against spend policy.

    Args:
        extracted: Structured PO produced by the extraction agent.
        approval_threshold: Per run override for the approval threshold, so the
            UI can drive the guardrail scenarios without restarting the worker.
            When None the configured value is used.
    """
    settings = get_settings()
    threshold = (
        approval_threshold
        if approval_threshold is not None
        else settings.po_approval_threshold
    )
    hard_threshold = settings.po_hard_block_threshold

    result = GuardrailResult(
        approval_threshold=threshold,
        hard_block_threshold=hard_threshold,
    )

    vendor = extracted.vendor.strip().lower()
    amount = float(extracted.total_amount)

    if vendor and vendor in settings.blocked_vendor_list:
        result.blocked = True
        result.reason = (
            f"Vendor {extracted.vendor!r} is on the blocked vendor list. "
            "Rejected by policy."
        )
    elif amount > hard_threshold:
        result.blocked = True
        result.reason = (
            f"{extracted.currency} {amount:,.2f} exceeds the hard block "
            f"threshold of {hard_threshold:,.2f}. Rejected by policy, no human "
            "override available."
        )
    elif amount > threshold:
        result.requires_approval = True
        result.reason = (
            f"{extracted.currency} {amount:,.2f} exceeds the approval threshold "
            f"of {threshold:,.2f}. A human decision is required."
        )
    else:
        result.reason = (
            f"{extracted.currency} {amount:,.2f} is within the approval "
            f"threshold of {threshold:,.2f}. No approval needed."
        )

    activity.logger.info("guardrail result: %s", result.reason)
    return result
