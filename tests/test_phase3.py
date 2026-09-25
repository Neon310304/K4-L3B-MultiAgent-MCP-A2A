from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.agents import EvidenceCollector
from student_agent.contracts import Contracts
from student_agent.state import CaseState, StateError
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


class FakeGateway:
    tools = [
        "get_customer_history",
        "get_order",
        "get_order_items",
        "get_order_payments",
        "get_payment_timeline",
        "get_policy",
        "get_product_context",
        "get_refund",
        "get_sellers",
        "get_shipment",
    ]

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any], str]] = []

    async def list_tools(self) -> list[str]:
        return self.tools

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        assert case_id == "CASE_001"
        if tool_name == "get_customer_history":
            assert arguments["customer_unique_id"] == "CUSTOMER_001"
        elif tool_name != "get_policy":
            assert arguments["order_id"] == "ORDER_001"
        evidence_ref = "ev_" + (tool_name.replace("_", "") * 4)[:24]
        self.calls.append((tool_name, case_id, arguments, evidence_ref))
        domain = {
            "get_customer_history": "customer",
            "get_order": "order",
            "get_order_items": "item",
            "get_order_payments": "payment",
            "get_payment_timeline": "payment",
            "get_policy": "policy",
            "get_product_context": "product",
            "get_refund": "refund",
            "get_sellers": "seller",
            "get_shipment": "shipment",
        }[tool_name]
        data: Any = {
            "get_order": {
                "order_id": "ORDER_001",
                "order_status": "delivered",
                "customer_unique_id": "CUSTOMER_001",
                "order_delivered_customer_date": "2024-01-03 00:00:00",
                "order_estimated_delivery_date": "2024-01-01 00:00:00",
                "order_delivered_carrier_date": "2024-01-02 00:00:00",
            },
            "get_order_items": [
                {"order_item_id": 1, "price": 10.0, "freight_value": 2.0, "seller_id": "SELLER_001"}
            ],
            "get_order_payments": [
                {"payment_sequential": 1, "payment_value": 12.0, "payment_type": "credit_card"}
            ],
            "get_payment_timeline": {"events": [{"status": "captured"}]},
            "get_product_context": {"product_category_name": "books"},
            "get_sellers": [{"seller_id": "SELLER_001"}],
            "get_customer_history": {
                "customer_unique_id": "CUSTOMER_001",
                "related_order_ids": ["ORDER_002"],
            },
            "get_shipment": {"shipment_id": "SHIPMENT_001"},
            "get_refund": {"status": "none"},
            "get_policy": {"policy_version": "EC_POLICY_V2"},
        }[tool_name]
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": evidence_ref,
            "result_hash": "sha256:" + "0" * 64,
            "domain": domain,
            "data": data,
        }


def test_specialists_keep_case_scope_and_evidence_refs(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    case = {
        "case_id": "CASE_001",
        "customer_request": {
            "claimed_order_id": "ORDER_001",
            "claims": [
                {"claim_id": "claim-a", "topic": "late_delivery_logistics"},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ],
        },
        "claimed_order_id": "ORDER_001",
        "policy_version": "EC_POLICY_V2",
    }

    trace.emit(case_id="CASE_001", event_type="case_received", actor="coordinator")
    gateway = FakeGateway()
    output = asyncio.run(solve_case(case, gateway, trace))
    trace.emit(case_id="CASE_001", event_type="case_finalized", actor="coordinator")

    contracts.validate_output(output, "phase3 output")
    assert output["case_id"] == "CASE_001"
    assert output["evidence_refs"]
    assert output["claim_assessments"][0]["verdict"] == "supported"
    assert output["claim_assessments"][1]["verdict"] == "partially_supported"
    events = [
        json.loads(line)
        for line in (tmp_path / "trace.jsonl").read_text().splitlines()
        if line
    ]
    consumed = [event for event in events if event["event_type"] == "tool_result_consumed"]
    assert len(consumed) == 10
    gateway_refs = {call[3] for call in gateway.calls}
    trace_refs = {ref for event in consumed for ref in event["evidence_refs"]}
    assert all(call[1] == "CASE_001" for call in gateway.calls)
    assert all(event["case_id"] == "CASE_001" for event in consumed)
    assert trace_refs == gateway_refs
    assert set(output["evidence_refs"]) == gateway_refs
    lifecycle = [
        "case_received",
        "task_assigned",
        "tool_result_consumed",
        "handoff",
        "policy_decided",
        "verification_completed",
        "case_finalized",
    ]
    position = -1
    event_types = [event["event_type"] for event in events]
    for event_type in lifecycle:
        position = event_types.index(event_type, position + 1)


def test_collector_enforces_specialist_tool_scope(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    gateway = FakeGateway()
    collector = EvidenceCollector(
        "CASE_001",
        gateway,
        TraceWriter(tmp_path / "trace.jsonl", contracts),
        CaseState("CASE_001"),
        gateway.tools,
    )

    async def call_outside_scope() -> None:
        await collector.call("payment", "shipment-agent", order_id="ORDER_001")

    with pytest.raises(StateError, match="not allowed"):
        asyncio.run(call_outside_scope())
    assert gateway.calls == []
