"""Specialist agents and the case-scoped MCP evidence collector."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .mcp_gateway import EvidenceGateway, GatewayError
from .state import PURPOSE_PERMISSIONS, AgentName, CaseState, StateError
from .trace import TraceWriter

EVIDENCE_REF = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")

TOOL_ALIASES: dict[str, tuple[str, ...]] = {
    "resolve": (
        "resolve_order_candidates",
        "resolve_order",
        "find_order",
        "search_orders",
        "get_order_candidates",
    ),
    "order": ("get_order", "get_order_details", "fetch_order", "lookup_order"),
    "items": (
        "get_order_items",
        "get_items",
        "get_order_products",
    ),
    "product": ("get_product_context", "get_products", "get_product"),
    "seller": ("get_seller", "get_seller_details", "lookup_seller"),
    "shipment": (
        "get_shipment",
        "get_order_shipment",
        "get_shipping",
        "get_delivery",
        "get_shipments",
    ),
    "payment": (
        "get_payment",
        "get_order_payment",
        "get_order_payments",
        "get_payments",
    ),
    "payment_timeline": ("get_payment_timeline", "get_payment_events"),
    "refund": ("get_refund", "get_order_refund", "get_refunds", "get_refund_status"),
    "customer": (
        "get_customer_history",
        "get_customer_context",
        "get_customer",
        "lookup_customer",
    ),
    "policy": ("get_policy", "get_case_policy", "get_business_policy"),
}


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _walk(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        result = [value]
        for child in value.values():
            result.extend(_walk(child))
        return result
    if isinstance(value, list):
        result: list[dict[str, Any]] = []
        for child in value:
            result.extend(_walk(child))
        return result
    return []


def _values(value: Any, keys: set[str]) -> list[Any]:
    result: list[Any] = []
    for obj in _walk(value):
        for key, child in obj.items():
            if _norm(key) in keys:
                result.extend(child if isinstance(child, list) else [child])
    return result


def strings(value: Any, keys: set[str], limit: int = 20) -> list[str]:
    result: list[str] = []
    for child in _values(value, keys):
        if isinstance(child, (str, int, float)) and not isinstance(child, bool):
            text = str(child)
            if text and text not in result:
                result.append(text)
        if len(result) >= limit:
            break
    return result


@dataclass(frozen=True)
class EvidenceRecord:
    purpose: str
    tool_name: str
    envelope: dict[str, Any]

    @property
    def evidence_ref(self) -> str:
        return str(self.envelope["evidence_ref"])

    @property
    def data(self) -> Any:
        return self.envelope.get("data")


@dataclass
class EvidenceCollector:
    """Case-scoped, least-privilege access to discovered MCP tools."""

    case_id: str
    gateway: EvidenceGateway
    trace: TraceWriter
    state: CaseState
    tool_names: list[str]
    records: list[EvidenceRecord] = field(default_factory=list)
    failures: list[tuple[str, str, str]] = field(default_factory=list)
    _cache: dict[tuple[str, str], EvidenceRecord] = field(default_factory=dict)

    def choose_tool(self, purpose: str) -> str | None:
        names = {_norm(name): name for name in self.tool_names}
        for alias in TOOL_ALIASES[purpose]:
            if _norm(alias) in names:
                return names[_norm(alias)]
        matches = [
            (len(_norm(alias)), name)
            for name in self.tool_names
            for alias in TOOL_ALIASES[purpose]
            if _norm(alias) in _norm(name)
        ]
        return max(matches)[1] if matches else None

    async def call(
        self, purpose: str, actor: AgentName, **arguments: Any
    ) -> EvidenceRecord | None:
        if purpose not in PURPOSE_PERMISSIONS[actor]:
            raise StateError(f"{actor} is not allowed to request {purpose} evidence")
        tool_name = self.choose_tool(purpose)
        if tool_name is None:
            return None
        cache_key = (tool_name, json.dumps(arguments, sort_keys=True, default=str))
        if cache_key in self._cache:
            return self._cache[cache_key]

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                self.state.reserve_tool_call()
                envelope = await self.gateway.call(
                    tool_name,
                    case_id=self.case_id,
                    **arguments,
                )
                evidence_ref = envelope.get("evidence_ref")
                if not isinstance(evidence_ref, str) or not EVIDENCE_REF.fullmatch(evidence_ref):
                    raise StateError("gateway response contains an invalid evidence_ref")
                record = EvidenceRecord(purpose, tool_name, envelope)
                self.records.append(record)
                self._cache[cache_key] = record
                self.state.add_evidence(evidence_ref)
                self.trace.emit(
                    case_id=self.case_id,
                    event_type="tool_result_consumed",
                    actor=actor,
                    tool_name=tool_name,
                    evidence_refs=[evidence_ref],
                    attributes={"attempt": attempt + 1},
                )
                return record
            except GatewayError as exc:
                last_error = exc
                if attempt == 0 and exc.retryable:
                    continue
                break
            except (ValueError, TypeError, StateError) as exc:
                last_error = exc
                break
            except RuntimeError as exc:
                last_error = exc
                break
        code = (
            str(last_error)
            if isinstance(last_error, GatewayError)
            else type(last_error).__name__
        )
        self.failures.append((purpose, tool_name, code))
        return None

    def data(self, purpose: str) -> list[Any]:
        return [record.data for record in self.records if record.purpose == purpose]

    def refs(self) -> list[str]:
        return list(dict.fromkeys(record.evidence_ref for record in self.records))[:30]


@dataclass
class EntityAgent:
    collector: EvidenceCollector

    async def resolve(self, case: dict[str, Any]) -> tuple[str | None, list[str]]:
        order_keys = {
            "orderid",
            "orderids",
            "claimedorderid",
            "candidateorderids",
            "ordercandidates",
        }
        candidates = strings(case, order_keys)
        claimed = strings(case, {"claimedorderid"})
        if claimed:
            return claimed[0], candidates
        result = await self.collector.call(
            "resolve",
            "entity-agent",
            candidate_order_ids=candidates,
            query=json.dumps(case, ensure_ascii=False, default=str),
        )
        resolved = strings(result.data if result else {}, order_keys)
        return (resolved[0] if resolved else None), candidates


@dataclass
class OrderItemAgent:
    collector: EvidenceCollector

    async def collect(self, order_id: str) -> None:
        await self.collector.call("order", "order-agent", order_id=order_id)
        await self.collector.call("items", "order-agent", order_id=order_id)
        await self.collector.call("product", "order-agent", order_id=order_id)


@dataclass
class SellerAgent:
    collector: EvidenceCollector

    async def collect(self, order_id: str) -> None:
        await self.collector.call("seller", "order-agent", order_id=order_id)


@dataclass
class ShipmentAgent:
    collector: EvidenceCollector

    async def collect(self, order_id: str) -> None:
        await self.collector.call("shipment", "shipment-agent", order_id=order_id)


@dataclass
class PaymentAgent:
    collector: EvidenceCollector

    async def collect(self, order_id: str) -> None:
        await self.collector.call("payment", "payment-agent", order_id=order_id)
        await self.collector.call("payment_timeline", "payment-agent", order_id=order_id)
        await self.collector.call("refund", "payment-agent", order_id=order_id)


@dataclass
class CustomerAgent:
    collector: EvidenceCollector

    async def collect(self, customer_unique_id: str) -> None:
        await self.collector.call(
            "customer",
            "customer-agent",
            customer_unique_id=customer_unique_id,
        )


@dataclass
class PolicyAgent:
    collector: EvidenceCollector

    async def collect(self, policy_version: str | None) -> None:
        if policy_version:
            await self.collector.call(
                "policy",
                "policy-agent",
                policy_version=policy_version,
            )
