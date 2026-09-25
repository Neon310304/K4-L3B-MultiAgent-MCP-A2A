from __future__ import annotations

from pathlib import Path

import pytest

from student_agent.policy import PolicyEngine, PolicyError, PolicyFacts, Verifier
from student_agent.workflow import _payment_summary


class EvidenceData:
    def __init__(self, values: dict[str, list[object]]) -> None:
        self.values = values

    def data(self, purpose: str) -> list[object]:
        return self.values.get(purpose, [])


def facts(**overrides: object) -> PolicyFacts:
    values = {
        "order_id": "ORDER_001",
        "order_status": "delivered",
        "shipment_verdict": "on_time",
        "payment_verdict": "reconciled",
        "payment_count": 1,
        "captured_total": 120.0,
        "refunded_total": 0.0,
        "expected_total": 120.0,
        "freight_total": 20.0,
        "late_seller_ids": (),
    }
    values.update(overrides)
    return PolicyFacts(**values)  # type: ignore[arg-type]


def engine() -> PolicyEngine:
    root = Path(__file__).resolve().parents[1]
    return PolicyEngine(root / "contracts" / "scoring" / "scoring-policy-v2.json")


def test_canceled_paid_refunds_only_outstanding_and_assigns_platform() -> None:
    decision = engine().decide(
        facts(order_status="canceled", captured_total=120.0, refunded_total=20.0)
    )

    assert decision.primary_issue == "canceled_order_paid"
    assert decision.recommended_refund_brl == 100.0
    assert decision.refund_lines[0]["amount_brl"] == 100.0
    assert decision.responsible_parties[0]["party_type"] == "platform"
    assert decision.case_status == "action_required"


def test_duplicate_charge_refunds_only_supported_overcharge() -> None:
    decision = engine().decide(
        facts(
            payment_verdict="duplicate_capture",
            captured_total=200.0,
            expected_total=100.0,
        )
    )

    assert decision.primary_issue == "duplicate_charge"
    assert decision.recommended_refund_brl == 100.0
    assert decision.responsible_parties[0]["party_type"] == "payment_provider"
    assert "refund_duplicate_charge" in decision.resolution_actions


def test_payment_reconciliation_preserves_duplicate_transactions() -> None:
    payments = [
        {"transaction_id": "TXN_001", "payment_value": 50.0},
        {"transaction_id": "TXN_001", "payment_value": 50.0},
    ]
    collector = EvidenceData({"refund": []})

    verdict, captured, _, expected, _ = _payment_summary(  # type: ignore[arg-type]
        collector,
        [{"price": 100.0, "freight_value": 0.0}],
        payments,
    )

    assert verdict == "duplicate_capture"
    assert captured == expected == 100.0


def test_seller_delay_assigns_only_evidenced_seller() -> None:
    decision = engine().decide(
        facts(
            shipment_verdict="seller_delay",
            late_seller_ids=("SELLER_001",),
        )
    )

    assert decision.primary_issue == "late_delivery_seller"
    assert decision.responsible_parties == (
        {"party_type": "seller", "party_id": "SELLER_001"},
    )


def test_conflicts_and_missing_evidence_reduce_confidence() -> None:
    verifier = Verifier()
    complete = verifier.calibrate(
        primary_issue="late_delivery_logistics",
        available_purposes={"order", "shipment", "policy"},
        entity_resolved=True,
        timeline_complete=True,
        conflict_count=0,
    )
    conflicting = verifier.calibrate(
        primary_issue="late_delivery_logistics",
        available_purposes={"order", "shipment", "policy"},
        entity_resolved=True,
        timeline_complete=True,
        conflict_count=1,
    )
    incomplete = verifier.calibrate(
        primary_issue="late_delivery_logistics",
        available_purposes={"order"},
        entity_resolved=True,
        timeline_complete=False,
        conflict_count=0,
    )

    assert complete <= 0.95
    assert conflicting <= 0.75
    assert incomplete <= 0.69
    assert conflicting < complete
    assert incomplete < complete


def test_policy_downgrades_when_required_evidence_domain_is_missing() -> None:
    decision = engine().decide(
        facts(shipment_verdict="logistics_delay"),
        available_purposes={"order", "shipment"},
    )

    assert decision.primary_issue == "insufficient_evidence"
    assert decision.case_status == "needs_investigation"
    assert decision.recommended_refund_brl == 0.0


def test_verifier_rejects_cross_field_responsibility_conflict() -> None:
    output = {
        "assessment": {
            "primary_issue": "late_delivery_seller",
            "case_status": "action_required",
            "confidence": 0.7,
        },
        "root_cause_analysis": {
            "responsible_parties": [
                {"party_type": "logistics_provider", "party_id": "LOGISTICS_PROVIDER"}
            ]
        },
        "evidence_refs": ["ev_" + "a" * 24],
        "data_conflicts": [],
        "financial_resolution": {
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        "resolution_actions": ["review_seller_handoff"],
    }

    with pytest.raises(PolicyError, match="responsible party"):
        Verifier().verify(output, ["ev_" + "a" * 24])
