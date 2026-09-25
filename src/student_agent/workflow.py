"""Coordinator and deterministic specialist-agent workflow for L3B."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .agents import (
    CustomerAgent,
    EntityAgent,
    EvidenceCollector,
    OrderItemAgent,
    PaymentAgent,
    PolicyAgent,
    SellerAgent,
    ShipmentAgent,
    strings,
)
from .mcp_gateway import EvidenceGateway
from .policy import PolicyEngine, PolicyFacts, Verifier
from .state import CaseState
from .trace import TraceWriter

ORDER_KEYS = {"orderid", "orderids", "resolvedorderids", "relatedorderids", "historyorderids"}
ITEM_KEYS = {"itemid", "itemids", "orderitemid", "orderitemids"}
SELLER_KEYS = {"sellerid", "sellerids"}
PAYMENT_KEYS = {
    "paymentid",
    "paymentids",
    "paymentreference",
    "paymentreferences",
    "paymentsequential",
}
SHIPMENT_KEYS = {"shipmentid", "shipmentids", "trackingid", "trackingids"}


def _records(value: Any, keys: set[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if {_norm(key) for key in current} & keys and current not in result:
                result.append(current)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return result


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _field(record: dict[str, Any], keys: set[str], default: Any = None) -> Any:
    for key, value in record.items():
        if _norm(key) in keys:
            return value
    return default


def _unique(values: list[str], limit: int = 20) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))[:limit]


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:[.,]\d+)?", value.replace(" ", ""))
        if match:
            return float(match.group(0).replace(",", "."))
    return None


def _sum_field(records: list[dict[str, Any]], keys: set[str]) -> float | None:
    values = [_number(_field(record, keys)) for record in records]
    numbers = [value for value in values if value is not None]
    return round(sum(numbers), 2) if numbers else None


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(value.strip(), fmt)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return None
    return parsed.replace(tzinfo=None)


def _first_date(records: list[dict[str, Any]], keys: set[str]) -> datetime | None:
    for record in records:
        parsed = _date(_field(record, keys))
        if parsed is not None:
            return parsed
    return None


def _status(records: list[dict[str, Any]]) -> str | None:
    values = strings(records, {"orderstatus", "status", "fulfillmentstatus"}, limit=1)
    return values[0].lower() if values else None


def _order_records(collector: EvidenceCollector) -> list[dict[str, Any]]:
    keys = {"orderstatus", "orderid", "customerid", "customeruniqueid"}
    return [record for data in collector.data("order") for record in _records(data, keys)]


def _item_records(collector: EvidenceCollector) -> list[dict[str, Any]]:
    keys = {"price", "freightvalue", "orderitemid", "productid", "sellerid"}
    return [record for data in collector.data("items") for record in _records(data, keys)]


def _payment_records(collector: EvidenceCollector) -> list[dict[str, Any]]:
    keys = {"paymentvalue", "paymenttype", "paymentsequential", "transactionid"}
    return [record for data in collector.data("payment") for record in _records(data, keys)]


def _customer_id(collector: EvidenceCollector, order_records: list[dict[str, Any]]) -> str | None:
    for data in collector.data("customer") + collector.data("order"):
        values = strings(data, {"customeruniqueid"}, limit=1)
        if values:
            return values[0]
    values = strings(order_records, {"customeruniqueid"}, limit=1)
    return values[0] if values else None


def _entity_ids(
    order_id: str | None,
    order_records: list[dict[str, Any]],
    item_records: list[dict[str, Any]],
    payment_records: list[dict[str, Any]],
    shipment_data: list[Any],
) -> dict[str, list[str]]:
    order_ids = _unique(([order_id] if order_id else []) + strings(order_records, ORDER_KEYS))
    item_ids: list[str] = []
    for record in item_records:
        value = _field(record, ITEM_KEYS)
        if value is None:
            continue
        item_id = str(value)
        if order_id and not item_id.startswith(f"{order_id}:"):
            item_id = f"{order_id}:{item_id}"
        item_ids.append(item_id)
    payment_ids: list[str] = []
    for record in payment_records:
        value = _field(record, PAYMENT_KEYS)
        if value is None:
            continue
        payment_id = str(value)
        if order_id and not payment_id.startswith(f"{order_id}:"):
            payment_id = f"{order_id}:{payment_id}"
        payment_ids.append(payment_id)
    return {
        "order_ids": order_ids[:20],
        "item_ids": _unique(item_ids),
        "seller_ids": _unique(
            [item for record in item_records for item in strings(record, SELLER_KEYS)]
        ),
        "payment_references": _unique(payment_ids),
        "shipment_ids": _unique(
            [item for data in shipment_data for item in strings(data, SHIPMENT_KEYS)]
        ),
    }


def _related_orders(collector: EvidenceCollector, order_id: str | None) -> list[str]:
    values = [
        value
        for data in collector.data("customer")
        for value in strings(data, ORDER_KEYS)
        if value != order_id
    ]
    return _unique(values)


def _shipment_summary(
    collector: EvidenceCollector,
    item_records: list[dict[str, Any]],
    status: str | None,
) -> tuple[str, list[str], bool]:
    shipment_records = [
        record
        for data in collector.data("shipment") + collector.data("order")
        for record in _records(
            data,
            {
                "orderdeliveredcustomerdate",
                "orderestimateddeliverydate",
                "orderdeliveredcarrierdate",
                "deliveredat",
                "estimateddate",
            },
        )
    ]
    delivered = _first_date(
        shipment_records,
        {"orderdeliveredcustomerdate", "deliveredat", "delivereddate"},
    )
    estimated = _first_date(
        shipment_records,
        {"orderestimateddeliverydate", "estimateddeliveryat", "estimateddate"},
    )
    carrier = _first_date(
        shipment_records,
        {
            "orderdeliveredcarrierdate",
            "orderdeliveredcarrierdatetime",
            "carrierhandoffat",
            "shippedat",
        },
    )
    complete = all(value is not None for value in (delivered, estimated, carrier))
    if status in {"returned", "returning"}:
        return "returned", [], complete
    if status in {"lost", "lost_in_transit"}:
        return "lost", [], complete
    if delivered is None or estimated is None:
        return "insufficient_evidence", [], complete
    if delivered <= estimated:
        return "on_time", [], complete
    late_sellers: list[str] = []
    for item in item_records:
        seller_id = _field(item, SELLER_KEYS)
        limit = _date(_field(item, {"shippinglimitdate", "shippinglimitat"}))
        if isinstance(seller_id, str) and carrier and limit and carrier > limit:
            late_sellers.append(seller_id)
    return ("seller_delay" if late_sellers else "logistics_delay"), _unique(late_sellers), complete


def _payment_summary(
    collector: EvidenceCollector,
    item_records: list[dict[str, Any]],
    payment_records: list[dict[str, Any]],
) -> tuple[str, float | None, float | None, float | None, float | None]:
    item_total = _sum_field(item_records, {"price", "itemprice"})
    freight_total = _sum_field(item_records, {"freightvalue", "freight", "shippingcost"})
    expected = None
    if item_total is not None or freight_total is not None:
        expected = round((item_total or 0) + (freight_total or 0), 2)
    captured = _sum_field(payment_records, {"paymentvalue", "capturedamount", "amount"})
    refund_data = collector.data("refund")
    refund_amount_keys = {
        "refundedamount",
        "refundvalue",
        "refundedtotal",
        "amountrefunded",
        "refundamount",
    }
    refund_records = [
        record for data in refund_data for record in _records(data, refund_amount_keys)
    ]
    refund_amounts = [_number(_field(record, refund_amount_keys)) for record in refund_records]
    refunded_values = [value for value in refund_amounts if value is not None]
    refunded = round(sum(refunded_values), 2) if refunded_values else None
    refund_statuses = {
        value.lower()
        for data in refund_data
        for value in strings(data, {"refundstatus", "status"})
    }
    if "failed" in refund_statuses or "rejected" in refund_statuses:
        return "refund_failed", captured, refunded, expected, freight_total
    if "pending" in refund_statuses or "processing" in refund_statuses:
        return "refund_pending", captured, refunded, expected, freight_total
    if "refunded" in refund_statuses or "completed" in refund_statuses:
        return "refunded", captured, refunded, expected, freight_total
    if not payment_records:
        return "insufficient_evidence", captured, refunded, expected, freight_total
    transactions = [
        str(value)
        for record in payment_records
        if (value := _field(record, {"transactionid", "paymentid"})) is not None
    ]
    if transactions and len(transactions) != len(set(transactions)):
        return "duplicate_capture", captured, refunded, expected, freight_total
    if expected is not None and captured is not None and abs(captured - expected) > 0.10:
        return "capture_mismatch", captured, refunded, expected, freight_total
    return "reconciled", captured, refunded, expected, freight_total


def _secondary(
    item_records: list[dict[str, Any]],
    seller_ids: list[str],
    payment_records: list[dict[str, Any]],
    related_orders: list[str],
    collector: EvidenceCollector,
) -> list[str]:
    result: list[str] = []
    if len(item_records) >= 2:
        result.append("multi_item_order")
    if len(seller_ids) >= 2:
        result.append("multi_seller_order")
    if len(payment_records) >= 2:
        result.append("split_payment")
    if related_orders:
        result.append("repeat_customer")
    categories = _unique(
        [
            value
            for data in collector.data("items") + collector.data("product")
            for value in strings(data, {"category", "categoryname", "productcategoryname"})
        ]
    )
    if len(categories) >= 2:
        result.append("multiple_categories")
    return result


def _claim_assessments(
    case: dict[str, Any],
    primary_issue: str,
    recommended_refund: float,
    captured_total: float | None,
    confidence: float,
    evidence_refs: list[str],
) -> list[dict[str, Any]]:
    request = case.get("customer_request")
    claims = request.get("claims", []) if isinstance(request, dict) else []
    result: list[dict[str, Any]] = []
    for claim in claims[:5] if isinstance(claims, list) else []:
        if not isinstance(claim, dict):
            continue
        claim_id = claim.get("claim_id")
        topic = claim.get("topic")
        if not isinstance(claim_id, str) or not claim_id:
            continue
        if primary_issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
        elif topic == primary_issue:
            verdict = "supported"
        elif topic == "requested_full_refund":
            captured = max(captured_total or 0, 0)
            if recommended_refund <= 0:
                verdict = "unsupported"
            elif captured > 0 and abs(recommended_refund - captured) <= 0.10:
                verdict = "supported"
            else:
                verdict = "partially_supported"
        else:
            verdict = "unsupported"
        result.append(
            {
                "claim_id": claim_id[:64],
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": evidence_refs,
            }
        )
    return result


def _conflicts(
    order_records: list[dict[str, Any]],
    shipment_data: list[Any],
    expected_total: float | None,
    captured_total: float | None,
) -> list[dict[str, Any]]:
    order_statuses = {value.lower() for value in strings(order_records, {"orderstatus", "status"})}
    shipment_statuses = {
        value.lower()
        for data in shipment_data
        for value in strings(data, {"orderstatus", "status", "shipmentstatus"})
    }
    conflicts: list[dict[str, Any]] = []
    if order_statuses and shipment_statuses and order_statuses != shipment_statuses:
        conflicts.append(
            {
                "field": "order_status",
                "sources": ["order", "shipment"],
                "selected_source": "order",
                "resolution_code": "ORDER_SOURCE_PRECEDENCE",
            }
        )
    if (
        expected_total is not None
        and captured_total is not None
        and abs(expected_total - captured_total) > 0.10
    ):
        conflicts.append(
            {
                "field": "order_total_brl",
                "sources": ["order_items", "payment"],
                "selected_source": "order_items",
                "resolution_code": "PAYMENT_CAPTURE_MISMATCH",
            }
        )
    return conflicts[:5]


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run entity resolution and all scoped evidence-collection specialists."""
    case_id = str(case.get("case_id", "")).strip()
    state = CaseState(case_id)
    tools = await gateway.list_tools()
    collector = EvidenceCollector(case_id, gateway, trace, state, tools)
    entity_agent = EntityAgent(collector)

    state.advance("resolving")
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-agent",
    )
    order_id, candidates = await entity_agent.resolve(case)
    if order_id:
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="entity-agent",
            target="coordinator",
            decision_code="ENTITY_RESOLVED",
        )
    else:
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="entity-agent",
            target="coordinator",
            decision_code="ENTITY_UNRESOLVED",
        )

    if order_id:
        state.resolved_order_ids = [order_id]
        state.rejected_candidates = [value for value in candidates if value != order_id]
        state.advance("investigating")
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="order-agent",
        )
        await OrderItemAgent(collector).collect(order_id)
        await SellerAgent(collector).collect(order_id)
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="order-agent",
            target="shipment-agent",
            evidence_refs=collector.refs()[-20:],
        )
        await ShipmentAgent(collector).collect(order_id)
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="payment-agent",
        )
        await PaymentAgent(collector).collect(order_id)
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="customer-agent",
        )
        preliminary_orders = _order_records(collector)
        customer_unique_id = _customer_id(collector, preliminary_orders)
        if customer_unique_id is None:
            hint = case.get("customer_unique_id_hint")
            customer_unique_id = hint if isinstance(hint, str) and hint else None
        if customer_unique_id:
            await CustomerAgent(collector).collect(customer_unique_id)

    await PolicyAgent(collector).collect(case.get("policy_version"))

    order_records = _order_records(collector)
    item_records = _item_records(collector)
    payment_records = _payment_records(collector)
    shipment_data = collector.data("shipment")
    status = _status(order_records)
    shipment_verdict, late_sellers, timeline_complete = _shipment_summary(
        collector, item_records, status
    )
    payment_verdict, captured, refunded, expected, freight = _payment_summary(
        collector, item_records, payment_records
    )
    entities = _entity_ids(
        order_id,
        order_records,
        item_records,
        payment_records,
        shipment_data,
    )
    customer_id = _customer_id(collector, order_records)
    related_orders = _related_orders(collector, order_id)
    secondary = _secondary(
        item_records,
        entities["seller_ids"],
        payment_records,
        related_orders,
        collector,
    )
    conflicts = _conflicts(order_records, shipment_data, expected, captured)
    policy_engine = PolicyEngine.from_repository()
    available_purposes = {record.purpose for record in collector.records}
    decision = policy_engine.decide(
        PolicyFacts(
            order_id=order_id,
            order_status=status,
            shipment_verdict=shipment_verdict,
            payment_verdict=payment_verdict,
            payment_count=len(payment_records),
            captured_total=captured,
            refunded_total=refunded,
            expected_total=expected,
            freight_total=freight,
            late_seller_ids=tuple(late_sellers),
        ),
        available_purposes,
    )
    primary = decision.primary_issue
    entity_resolved = bool(order_id and order_records)
    entity_status = "resolved" if entity_resolved else "ambiguous" if candidates else "not_found"
    verifier = Verifier()
    confidence = verifier.calibrate(
        primary_issue=primary,
        available_purposes=available_purposes,
        entity_resolved=entity_resolved,
        timeline_complete=timeline_complete,
        conflict_count=len(conflicts),
    )

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="policy-agent",
        decision_code="EVIDENCE_COLLECTION_COMPLETED",
        evidence_refs=collector.refs()[:20],
    )
    state.advance("policy")
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=primary,
        evidence_refs=collector.refs()[:20],
    )
    output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary,
            "secondary_issues": secondary[:10],
            "case_status": decision.case_status,
            "confidence": confidence,
        },
        "affected_entities": entities,
        "entity_resolution": {
            "status": entity_status,
            "resolved_order_ids": state.resolved_order_ids,
            "rejected_candidates": state.rejected_candidates,
            "confidence": 0.95 if entity_resolved else 0.35 if candidates else 0.1,
        },
        "customer_context": {
            "customer_unique_id": customer_id,
            "related_order_ids": related_orders[:20],
        },
        "shipment_analysis": {
            "verdict": shipment_verdict,
            "late_seller_ids": late_sellers[:20],
            "timeline_complete": timeline_complete,
        },
        "payment_analysis": {
            "verdict": payment_verdict,
            "captured_total_brl": captured,
            "refunded_total_brl": refunded,
            "refundable_total_brl": decision.recommended_refund_brl,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": decision.cause_code, "rank": 1}],
            "responsible_parties": list(decision.responsible_parties),
        },
        "evidence_refs": collector.refs(),
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": decision.recommended_refund_brl,
            "refund_lines": list(decision.refund_lines),
        },
        "resolution_actions": list(decision.resolution_actions),
    }
    claim_assessments = _claim_assessments(
        case,
        primary,
        decision.recommended_refund_brl,
        captured,
        confidence,
        collector.refs(),
    )
    if claim_assessments:
        output["claim_assessments"] = claim_assessments
    state.advance("verifying")
    if not collector.refs():
        failures = ", ".join(
            f"{purpose}/{tool}/{code}" for purpose, tool, code in collector.failures
        )
        raise RuntimeError(f"case {case_id} produced no MCP evidence; failures=[{failures}]")
    verifier.verify(output, collector.refs())
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        decision_code="CROSS_FIELD_CONSISTENT",
        evidence_refs=collector.refs()[:20],
        attributes={"confidence": confidence, "conflicts": len(conflicts)},
    )
    state.advance("finalized")
    return output
