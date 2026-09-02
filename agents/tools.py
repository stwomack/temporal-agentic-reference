"""Tools the specialist agents can call.

These are the only way an agent reaches outside its own prompt. They read from
the small JSON stores under data/, which stand in for a vendor master and a
purchase order history that would live in an ERP.

The tools are plain functions with typed signatures and docstrings, because
that is what LangChain turns into a Bedrock tool schema. Keep the docstrings
accurate: they are the model's only description of what the tool does.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@lru_cache(maxsize=1)
def _vendors() -> dict[str, Any]:
    return json.loads((DATA_DIR / "vendors.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _po_history() -> list[dict[str, Any]]:
    return json.loads((DATA_DIR / "po_history.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def procurement_policy() -> str:
    return (DATA_DIR / "procurement_policy.md").read_text(encoding="utf-8")


def _match_vendor(name: str) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    """Case and punctuation tolerant vendor lookup."""
    normalize = lambda value: "".join(c for c in value.lower() if c.isalnum())  # noqa: E731
    target = normalize(name)
    for registered, record in _vendors().items():
        if normalize(registered) == target:
            return registered, record
    # Fall back to a containment match so "Acme Shell" finds "Acme Shell Corp".
    for registered, record in _vendors().items():
        registered_norm = normalize(registered)
        if target and (target in registered_norm or registered_norm in target):
            return registered, record
    return None, None


@tool
def lookup_vendor(vendor_name: str) -> str:
    """Look up a vendor in the approved vendor registry.

    Returns the vendor's approval status, whether a contract and a verified tax
    identification are on file, how many years they have been active, their
    spend categories, and their annual spend. Returns a not-found result if the
    vendor is not registered at all, which is itself significant.
    """
    registered, record = _match_vendor(vendor_name)
    if record is None:
        return json.dumps(
            {
                "found": False,
                "queried": vendor_name,
                "note": "Vendor is not present in the registry.",
            }
        )
    return json.dumps(
        {
            "found": True,
            "registered_name": registered,
            "status": record["status"],
            "contract_on_file": record["contract_on_file"],
            "tax_id_verified": record["tax_id_verified"],
            "years_active": record["years_active"],
            "categories": record["categories"],
            "annual_spend_usd": record["annual_spend_usd"],
            "incident_count": len(record["incidents"]),
        }
    )


@tool
def list_vendor_incidents(vendor_name: str) -> str:
    """List recorded compliance or quality incidents for a vendor.

    Each incident has a date, a severity of low, medium, or high, and a summary.
    An empty list means a clean record. Call this after lookup_vendor when the
    vendor has a non zero incident_count, or whenever status is not approved.
    """
    registered, record = _match_vendor(vendor_name)
    if record is None:
        return json.dumps({"found": False, "queried": vendor_name, "incidents": []})
    return json.dumps(
        {"found": True, "registered_name": registered, "incidents": record["incidents"]}
    )


@tool
def search_recent_purchase_orders(vendor_name: str) -> str:
    """Search recently issued purchase orders for one vendor.

    Returns every purchase order on file for that vendor, most recent first,
    with its id, amount, requester, submission date, and description. Use it to
    judge whether a new request duplicates an existing order or looks like a
    purchase split across several orders to stay under an approval threshold.
    """
    registered, _ = _match_vendor(vendor_name)
    name = registered or vendor_name
    matches = [po for po in _po_history() if po["vendor"] == name]
    matches.sort(key=lambda po: po["submitted_at"], reverse=True)
    return json.dumps({"vendor": name, "match_count": len(matches), "orders": matches})


VENDOR_RISK_TOOLS = [lookup_vendor, list_vendor_incidents]
DUPLICATE_TOOLS = [search_recent_purchase_orders]
ALL_TOOLS = {t.name: t for t in [*VENDOR_RISK_TOOLS, *DUPLICATE_TOOLS]}
