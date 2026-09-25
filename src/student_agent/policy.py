"""Deterministic dispute policy, cross-field verification and calibration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class PolicyError(ValueError):
    """Raised when the public policy or a candidate decision is inconsistent."""


CAUSES = {
    "canceled_order_paid": "ORDER_CANCELED_AFTER_PAYMENT",
    "unavailable_order_paid": "ORDER_UNAVAILABLE_AFTER_PAYMENT",
    "late_delivery_seller": "SELLER_HANDOFF_AFTER_LIMIT",
    "late_delivery_logistics": "CARRIER_DELIVERED_AFTER_ESTIMATE",
    "valid_split_payment": "MULTIPLE_PAYMENTS_RECONCILED",
    "payment_mismatch": "PAYMENT_CAPTURE_MISMATCH",
    "duplicate_charge": "DUPLICATE_PAYMENT_CAPTURE",
    "refund_pending": "REFUND_PENDING",
    "refund_failed": "REFUND_FAILED",
    "unsupported_claim": "DELIVERY_WITHIN_ESTIMATE",
    "insufficient_evidence": "INSUFFICIENT_EVIDENCE",
}

ACTION_REQUIRED = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
}

REQUIRED_PURPOSES = {
    "canceled_order_paid": {"order", "payment"},
    "unavailable_order_paid": {"order", "payment"},
    "late_delivery_seller": {"order", "items", "shipment"},
    "late_delivery_logistics": {"order", "items", "shipment"},
    "valid_split_payment": {"order", "items", "payment"},
    "payment_mismatch": {"order", "items", "payment"},
    "duplicate_charge": {"order", "items", "payment"},
    "refund_pending": {"order", "payment", "refund"},
    "refund_failed": {"order", "payment", "refund"},
    "unsupported_claim": {"order", "shipment"},
    "insufficient_evidence": {"order"},
}


@dataclass(frozen=True)
class PolicyFacts:
    order_id: str | None
    order_status: str | None
    shipment_verdict: str
    payment_verdict: str
    payment_count: int
    captured_total: float | None
    refunded_total: float | None
    expected_total: float | None
    freight_total: float | None
    late_seller_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyDecision:
    primary_issue: str
    case_status: str
    cause_code: str
    responsible_parties: tuple[dict[str, Any], ...]
    recommended_refund_brl: float
    refund_lines: tuple[dict[str, Any], ...]
    resolution_actions: tuple[str, ...]


class PolicyEngine:
    """Load the released scoring policy and produce a deterministic decision."""

    def __init__(self, policy_path: Path) -> None:
        try:
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PolicyError("public scoring policy is missing or invalid") from exc
        if policy.get("policy_version") != "day09-scoring-v2":
            raise PolicyError("unsupported public scoring policy version")
        weights = policy.get("variant_weights", {}).get("l3b")
        if not isinstance(weights, dict) or abs(sum(weights.values()) - 1.0) > 1e-9:
            raise PolicyError("invalid L3B scoring weights")
        self.policy = policy

    @classmethod
    def from_repository(cls) -> PolicyEngine:
        root = Path(__file__).resolve().parents[2]
        return cls(root / "contracts" / "scoring" / "scoring-policy-v2.json")

    def decide(
        self, facts: PolicyFacts, available_purposes: set[str] | None = None
    ) -> PolicyDecision:
        primary = self._primary_issue(facts)
        if (
            available_purposes is not None
            and primary != "insufficient_evidence"
            and not REQUIRED_PURPOSES[primary].issubset(available_purposes)
        ):
            primary = "insufficient_evidence"
        refund = self._refund(primary, facts)
        parties = self._responsibility(primary, facts.late_seller_ids)
        actions = self._actions(primary, facts.shipment_verdict, refund)
        status = (
            "needs_investigation"
            if primary == "insufficient_evidence"
            else "action_required"
            if primary in ACTION_REQUIRED
            else "no_action"
        )
        lines: tuple[dict[str, Any], ...] = ()
        if refund > 0:
            lines = (
                {
                    "reason_code": primary,
                    "amount_brl": refund,
                    "entity_id": facts.order_id,
                },
            )
        return PolicyDecision(
            primary,
            status,
            CAUSES[primary],
            tuple(parties),
            refund,
            lines,
            tuple(actions),
        )

    @staticmethod
    def _primary_issue(facts: PolicyFacts) -> str:
        status = (facts.order_status or "").lower()
        if status in {"canceled", "cancelled"} and (facts.captured_total or 0) > 0:
            return "canceled_order_paid"
        if status == "unavailable" and (facts.captured_total or 0) > 0:
            return "unavailable_order_paid"
        if facts.shipment_verdict == "seller_delay":
            return "late_delivery_seller"
        if facts.shipment_verdict == "logistics_delay":
            return "late_delivery_logistics"
        payment_issues = {
            "refund_failed": "refund_failed",
            "refund_pending": "refund_pending",
            "duplicate_capture": "duplicate_charge",
            "capture_mismatch": "payment_mismatch",
        }
        if facts.payment_verdict in payment_issues:
            return payment_issues[facts.payment_verdict]
        if facts.payment_count > 1 and facts.payment_verdict == "reconciled":
            return "valid_split_payment"
        if facts.order_id and facts.shipment_verdict == "on_time":
            return "unsupported_claim"
        return "insufficient_evidence"

    @staticmethod
    def _responsibility(primary: str, seller_ids: tuple[str, ...]) -> list[dict[str, Any]]:
        if primary == "late_delivery_seller":
            return [{"party_type": "seller", "party_id": value} for value in seller_ids[:5]]
        if primary == "late_delivery_logistics":
            return [{"party_type": "logistics_provider", "party_id": "LOGISTICS_PROVIDER"}]
        if primary in {"canceled_order_paid", "unavailable_order_paid"}:
            return [{"party_type": "platform", "party_id": "OLIST_PLATFORM"}]
        if primary in {
            "payment_mismatch",
            "duplicate_charge",
            "refund_pending",
            "refund_failed",
        }:
            return [{"party_type": "payment_provider", "party_id": "PAYMENT_PROVIDER"}]
        if primary in {"valid_split_payment", "unsupported_claim"}:
            return [{"party_type": "customer", "party_id": None}]
        return [{"party_type": "unknown", "party_id": None}]

    @staticmethod
    def _refund(primary: str, facts: PolicyFacts) -> float:
        captured = max(facts.captured_total or 0, 0)
        refunded = max(facts.refunded_total or 0, 0)
        expected = max(facts.expected_total or 0, 0)
        outstanding = max(captured - refunded, 0)
        if primary in {"canceled_order_paid", "unavailable_order_paid"}:
            return round(outstanding, 2)
        if primary in {"late_delivery_seller", "late_delivery_logistics"}:
            freight = max(facts.freight_total or 0, 0)
            return round(min(freight, outstanding) if captured else freight, 2)
        if primary in {"duplicate_charge", "payment_mismatch"}:
            return round(max(captured - expected - refunded, 0), 2)
        if primary in {"refund_pending", "refund_failed"}:
            return round(outstanding, 2)
        return 0.0

    @staticmethod
    def _actions(primary: str, shipment_verdict: str, refund: float) -> list[str]:
        if primary in {"canceled_order_paid", "unavailable_order_paid"}:
            return ["issue_full_refund", "verify_refund_completion"]
        if primary in {"late_delivery_seller", "late_delivery_logistics"}:
            review = (
                "review_seller_handoff"
                if shipment_verdict == "seller_delay"
                else "review_carrier_delay"
            )
            actions = [review]
            if refund > 0:
                actions.extend(["refund_freight", "verify_refund_completion"])
            return actions
        if primary == "valid_split_payment":
            return ["explain_valid_split_payment"]
        if primary == "payment_mismatch":
            actions = ["review_payment_reconciliation", "verify_payment_allocation"]
            if refund > 0:
                actions.append("issue_overcharge_refund")
            return actions
        if primary == "duplicate_charge":
            return ["refund_duplicate_charge", "verify_refund_completion"]
        if primary == "refund_pending":
            return ["verify_refund_completion"]
        if primary == "refund_failed":
            return ["retry_refund", "escalate_refund"]
        if primary == "unsupported_claim":
            return ["reject_late_refund"]
        return ["collect_missing_evidence"]


class Verifier:
    """Calibrate confidence and reject cross-field inconsistencies."""

    def calibrate(
        self,
        *,
        primary_issue: str,
        available_purposes: set[str],
        entity_resolved: bool,
        timeline_complete: bool,
        conflict_count: int,
    ) -> float:
        required = REQUIRED_PURPOSES[primary_issue]
        coverage = len(required & available_purposes) / len(required)
        confidence = 0.20 + 0.55 * coverage
        confidence += 0.10 if entity_resolved else 0
        confidence += 0.05 if timeline_complete else 0
        confidence += 0.05 if "policy" in available_purposes else 0
        confidence -= min(conflict_count, 2) * 0.15
        if coverage < 1:
            confidence = min(confidence, 0.69)
        if not entity_resolved:
            confidence = min(confidence, 0.35)
        if primary_issue == "insufficient_evidence":
            confidence = min(confidence, 0.40)
        if conflict_count:
            confidence = min(confidence, 0.75)
        return round(max(0.05, min(confidence, 0.95)), 2)

    def verify(self, output: dict[str, Any], consumed_refs: list[str]) -> None:
        assessment = output["assessment"]
        primary = assessment["primary_issue"]
        if primary not in CAUSES:
            raise PolicyError("unknown primary issue")
        if output["evidence_refs"] != consumed_refs:
            raise PolicyError("output evidence refs do not match consumed evidence")
        if not consumed_refs:
            raise PolicyError("case has no authoritative MCP evidence")

        parties = output["root_cause_analysis"]["responsible_parties"]
        party_types = {party["party_type"] for party in parties}
        expected_types = {
            "late_delivery_seller": {"seller"},
            "late_delivery_logistics": {"logistics_provider"},
            "canceled_order_paid": {"platform"},
            "unavailable_order_paid": {"platform"},
            "payment_mismatch": {"payment_provider"},
            "duplicate_charge": {"payment_provider"},
            "refund_pending": {"payment_provider"},
            "refund_failed": {"payment_provider"},
            "valid_split_payment": {"customer"},
            "unsupported_claim": {"customer"},
            "insufficient_evidence": {"unknown"},
        }[primary]
        if party_types != expected_types:
            raise PolicyError("responsible party conflicts with primary issue")

        financial = output["financial_resolution"]
        refund = round(float(financial["recommended_refund_brl"]), 2)
        line_total = round(sum(float(line["amount_brl"]) for line in financial["refund_lines"]), 2)
        if refund != line_total:
            raise PolicyError("refund lines do not equal recommended refund")
        if refund == 0 and financial["refund_lines"]:
            raise PolicyError("zero refund must not contain refund lines")
        if len(output["resolution_actions"]) != len(set(output["resolution_actions"])):
            raise PolicyError("resolution actions must be unique")
        expected_status = (
            "needs_investigation"
            if primary == "insufficient_evidence"
            else "action_required"
            if primary in ACTION_REQUIRED
            else "no_action"
        )
        if assessment["case_status"] != expected_status:
            raise PolicyError("case status conflicts with primary issue")
        confidence = float(assessment["confidence"])
        if output["data_conflicts"] and confidence > 0.75:
            raise PolicyError("confidence is too high for conflicting evidence")
