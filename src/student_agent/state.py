"""Internal state and A2A message contracts.

These dataclasses are orchestration state only.  They are deliberately kept
separate from the public JSON schemas: only the schema files in
``contracts/schemas`` may be serialized as competition artifacts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal


class StateError(ValueError):
    """Raised when an agent attempts an invalid state transition."""


AgentName = Literal[
    "coordinator",
    "entity-agent",
    "order-agent",
    "shipment-agent",
    "payment-agent",
    "customer-agent",
    "policy-agent",
    "verifier-agent",
]

Phase = Literal[
    "received",
    "resolving",
    "investigating",
    "policy",
    "verifying",
    "finalized",
]

CASE_ID_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,63}$")
EVIDENCE_REF_PATTERN = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")

PURPOSE_PERMISSIONS: dict[AgentName, frozenset[str]] = {
    "coordinator": frozenset(),
    "entity-agent": frozenset({"resolve"}),
    "order-agent": frozenset({"order", "items", "product", "seller"}),
    "shipment-agent": frozenset({"shipment"}),
    "payment-agent": frozenset({"payment", "payment_timeline", "refund"}),
    "customer-agent": frozenset({"customer"}),
    "policy-agent": frozenset({"policy"}),
    "verifier-agent": frozenset(),
}


@dataclass(frozen=True)
class Handoff:
    """A2A envelope used between local agents.

    ``payload`` contains normalized facts, never prompts or private reasoning.
    The envelope is not written directly to ``trace.jsonl``; trace events are
    emitted through :class:`student_agent.trace.TraceWriter`.
    """

    case_id: str
    correlation_id: str
    source: AgentName
    target: AgentName
    message_type: Literal["task", "result", "escalation"]
    payload: dict[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not CASE_ID_PATTERN.fullmatch(self.case_id):
            raise StateError(f"invalid case_id: {self.case_id!r}")
        if not self.correlation_id:
            raise StateError("correlation_id must be non-empty")
        if self.source == self.target:
            raise StateError("handoff source and target must differ")
        if any(not EVIDENCE_REF_PATTERN.fullmatch(ref) for ref in self.evidence_refs):
            raise StateError("handoff contains an invalid evidence_ref")


@dataclass
class CaseState:
    """Bounded mutable state for exactly one case."""

    case_id: str
    phase: Phase = "received"
    resolved_order_ids: list[str] = field(default_factory=list)
    rejected_candidates: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    tool_calls: int = 0
    event_ids: set[str] = field(default_factory=set)
    query_budget: int = 12

    def __post_init__(self) -> None:
        if not CASE_ID_PATTERN.fullmatch(self.case_id):
            raise StateError(f"invalid case_id: {self.case_id!r}")
        if self.query_budget < 0:
            raise StateError("query_budget must be non-negative")

    def advance(self, phase: Phase) -> None:
        order = ["received", "resolving", "investigating", "policy", "verifying", "finalized"]
        if order.index(phase) < order.index(self.phase):
            raise StateError(f"phase cannot move backwards: {self.phase} -> {phase}")
        self.phase = phase

    def add_evidence(self, evidence_ref: str) -> None:
        if not EVIDENCE_REF_PATTERN.fullmatch(evidence_ref):
            raise StateError(f"invalid evidence_ref: {evidence_ref!r}")
        if evidence_ref not in self.evidence_refs:
            self.evidence_refs.append(evidence_ref)
        if len(self.evidence_refs) > 30:
            raise StateError("case evidence_ref limit exceeded")

    def reserve_tool_call(self) -> None:
        if self.tool_calls >= self.query_budget:
            raise StateError("per-case MCP query budget exhausted")
        self.tool_calls += 1

    def record_event(self, event_id: str) -> None:
        if not event_id or event_id in self.event_ids:
            raise StateError("event_id must be unique and non-empty")
        self.event_ids.add(event_id)
