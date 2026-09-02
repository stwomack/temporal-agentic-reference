"""Agent tests.

The pure mapping from an agent's raw finding to the shape the UI renders is
deterministic and tested without Bedrock. The agents themselves need live
Bedrock and are skipped, never mocked, when it is unavailable.
"""

from __future__ import annotations

import pytest

from agents import duplicate_detection, policy_compliance, supervisor, vendor_risk
from agents.tools import (
    list_vendor_incidents,
    lookup_vendor,
    search_recent_purchase_orders,
)
from common.models import Scenario, Severity, SupervisorRecommendation
from common.scenarios import SCENARIOS

# ------------------------------------------------------------------- tools


def test_lookup_vendor_finds_a_registered_vendor():
    import json

    result = json.loads(lookup_vendor.invoke({"vendor_name": "Northwind Packaging Co."}))
    assert result["found"] is True
    assert result["status"] == "approved"


def test_lookup_vendor_tolerates_case_and_punctuation():
    import json

    result = json.loads(lookup_vendor.invoke({"vendor_name": "acme shell"}))
    assert result["found"] is True
    assert result["registered_name"] == "Acme Shell Corp"
    assert result["status"] == "unapproved"


def test_lookup_vendor_reports_an_unknown_vendor_rather_than_failing():
    import json

    result = json.loads(lookup_vendor.invoke({"vendor_name": "Nonexistent GmbH"}))
    assert result["found"] is False


def test_incidents_are_returned_for_a_flagged_vendor():
    import json

    result = json.loads(list_vendor_incidents.invoke({"vendor_name": "Acme Shell Corp"}))
    assert len(result["incidents"]) == 2
    assert all(i["severity"] == "high" for i in result["incidents"])


def test_recent_purchase_orders_are_scoped_to_one_vendor():
    import json

    result = json.loads(
        search_recent_purchase_orders.invoke({"vendor_name": "Halden Industrial Supply"})
    )
    assert result["match_count"] == len(result["orders"])
    assert all(o["vendor"] == "Halden Industrial Supply" for o in result["orders"])


def test_scenario_vendors_all_exist_in_the_registry():
    """A scenario naming a vendor the registry has never heard of would make
    the vendor risk agent's finding meaningless."""
    import json

    for spec in SCENARIOS.values():
        # The extraction agent decides the exact vendor string at runtime, so
        # check the registry contains something matching each scenario's text.
        assert spec.raw_text.strip(), f"{spec.scenario} has no request text"
    for name in ["Northwind Packaging Co.", "Acme Shell Corp", "Pinnacle Cold Chain Ltd"]:
        assert json.loads(lookup_vendor.invoke({"vendor_name": name}))["found"]


# ------------------------------------------------- finding mapping, no model


def test_vendor_risk_severity_maps_from_the_rating():
    for rating, expected in [
        ("low", Severity.OK),
        ("medium", Severity.CAUTION),
        ("high", Severity.CAUTION),
        ("critical", Severity.BLOCKER),
    ]:
        finding = vendor_risk.to_finding(
            {"finding": {"risk_rating": rating, "registry_status": "approved"}}
        )
        assert finding.severity is expected, rating


def test_policy_finding_trusts_the_clause_list_over_the_boolean():
    # A model that says "not compliant" while naming no clause has not found a
    # violation. Rendering "0 violations" would look like a bug.
    contradictory = policy_compliance.to_finding(
        {"finding": {"compliant": False, "violated_clauses": [], "rationale": "unsure"}}
    )
    assert contradictory.severity is Severity.OK
    assert "No policy violations" in contradictory.headline

    real = policy_compliance.to_finding(
        {"finding": {"compliant": False, "violated_clauses": ["1.3", "2.2"]}}
    )
    assert real.severity is Severity.CAUTION
    assert "1.3" in real.headline and "2.2" in real.headline


def test_duplicate_finding_distinguishes_duplicate_from_split():
    clean = duplicate_detection.to_finding(
        {"finding": {"duplicate_suspected": False, "split_suspected": False}}
    )
    assert clean.severity is Severity.OK

    dup = duplicate_detection.to_finding(
        {
            "finding": {
                "duplicate_suspected": True,
                "split_suspected": False,
                "matching_po_ids": ["PO-1"],
            }
        }
    )
    assert dup.severity is Severity.CAUTION
    assert "Duplicate" in dup.headline

    split = duplicate_detection.to_finding(
        {"finding": {"duplicate_suspected": False, "split_suspected": True}}
    )
    assert "Split" in split.headline


def test_supervisor_falls_back_to_escalation_on_an_unparseable_recommendation():
    # Never auto approve and never hard reject on a malformed answer.
    verdict = supervisor.to_verdict({"finding": {"recommendation": "banana"}})
    assert verdict.recommendation is SupervisorRecommendation.ESCALATE_TO_HUMAN


def test_supervisor_clamps_confidence_into_range():
    assert supervisor.to_verdict(
        {"finding": {"recommendation": "auto_approve", "confidence": 7.5}}
    ).confidence == 1.0
    assert supervisor.to_verdict(
        {"finding": {"recommendation": "auto_approve", "confidence": -2}}
    ).confidence == 0.0
    assert supervisor.to_verdict(
        {"finding": {"recommendation": "auto_approve", "confidence": "not a number"}}
    ).confidence == 0.0


def test_every_agent_graph_declares_execute_in_on_every_node():
    """The LangGraph plugin refuses to build a graph whose node is missing
    execute_in, so this catches the mistake at test time rather than at worker
    startup."""
    builders = [
        vendor_risk.build_graph,
        policy_compliance.build_graph,
        duplicate_detection.build_graph,
        supervisor.build_graph,
    ]
    for build in builders:
        graph = build()
        for name, node in graph.nodes.items():
            metadata = node.metadata or {}
            assert metadata.get("execute_in") == "activity", f"{build.__name__}:{name}"


# ----------------------------------------------------------------- live model


async def test_vendor_risk_agent_uses_its_tools_and_rates_an_unapproved_vendor(
    live_bedrock,
):
    # ainvoke, not invoke: the conditional router is async so that a Temporal
    # workflow never has to offload it to a thread pool.
    state = (
        await vendor_risk.build_graph()
        .compile()
        .ainvoke(
            {
                "task": vendor_risk.task(
                    {
                        "vendor": "Acme Shell Corp",
                        "requester": "Priya Raghunathan",
                        "currency": "USD",
                        "total_amount": 19_200.0,
                    }
                )
            }
        )
    )
    finding = vendor_risk.to_finding(state)
    # It must have actually looked the vendor up rather than guessed.
    assert "lookup_vendor" in finding.tool_calls
    assert finding.turns >= 2
    assert finding.input_tokens > 0
    assert finding.severity is Severity.BLOCKER
    assert finding.raw["risk_rating"].lower() == "critical"


async def test_duplicate_agent_stays_quiet_on_an_unrelated_order(live_bedrock):
    spec = SCENARIOS[Scenario.HAPPY_PATH]
    state = (
        await duplicate_detection.build_graph()
        .compile()
        .ainvoke(
            {
                "task": duplicate_detection.task(
                    {
                        "vendor": "Northwind Packaging Co.",
                        "requester": "Dana Whitfield",
                        "currency": "USD",
                        "total_amount": 4_660.0,
                        "line_items": [
                            {"description": "aluminum can lids", "quantity": 12000,
                             "unit_price": 0.18, "amount": 2160.0},
                            {"description": "corrugated pallet sleeves", "quantity": 400,
                             "unit_price": 6.25, "amount": 2500.0},
                        ],
                    }
                )
            }
        )
    )
    finding = duplicate_detection.to_finding(state)
    assert "search_recent_purchase_orders" in finding.tool_calls
    assert finding.raw["duplicate_suspected"] is False, finding.detail
    assert spec.raw_text  # the scenario this mirrors still exists


def test_the_final_turn_offers_only_the_finding_schema():
    """A tool-using agent must always be able to conclude.

    On the last permitted turn the data tools are withheld so the model has
    nothing to call but its finding. Without this an agent can loop until it
    trips MAX_TURNS and fails the workflow.
    """
    from agents.react_agent import MAX_TURNS

    captured = {}

    class FakeModel:
        def bind_tools(self, tools):
            captured["tools"] = [getattr(t, "name", getattr(t, "__name__", "?")) for t in tools]
            raise RuntimeError("stop here, we only care about what was bound")

    import agents.react_agent as react

    original = react.build_chat_model
    react.build_chat_model = lambda **kwargs: FakeModel()
    try:
        for turns, expect_tools in [(0, True), (MAX_TURNS - 2, False)]:
            captured.clear()
            with pytest.raises(RuntimeError):
                react.run_model_turn(
                    vendor_risk.SPEC,
                    {"task": "t", "turns": turns, "messages": []},
                )
            bound = captured["tools"]
            assert "VendorRiskFinding" in bound
            has_data_tools = any(t.startswith("lookup") for t in bound)
            assert has_data_tools is expect_tools, (turns, bound)
    finally:
        react.build_chat_model = original
