# L3B Architecture Record

**Người thực hiện:** Trần Quốc Vượng (`2A202602522`)

**Lớp:** `H209`

**Scope:** Coordinator/Router, all specialist agents, MCP Evidence Gateway,
policy/verifier flow, trace audit, validation and submission packaging.

| Workstream | Owner |
| --- | --- |
| Orchestration, state and A2A handoff | Trần Quốc Vượng (`2A202602522`) |
| Entity, order, item, seller and customer agents | Trần Quốc Vượng (`2A202602522`) |
| Shipment, payment and refund agents | Trần Quốc Vượng (`2A202602522`) |
| MCP gateway and evidence provenance | Trần Quốc Vượng (`2A202602522`) |
| Policy, verification and calibration | Trần Quốc Vượng (`2A202602522`) |
| Validation, packaging, tests and documentation | Trần Quốc Vượng (`2A202602522`) |

This document records the observable design for the L3B multi-agent workflow.
The public JSON schemas under `contracts/schemas/` are authoritative. Internal
state and A2A messages must be projected into those schemas before an artifact
is written; no internal field is added to a public output.

## 1. System overview

```text
                         ┌──────────────────────────┐
                         │   Coordinator / Router   │
                         └─────────────┬────────────┘
                                       │ task / handoff
       ┌──────────────────────────────┼──────────────────────────────┐
       ▼                              ▼                              ▼
┌──────────────────┐          ┌──────────────────┐          ┌──────────────────┐
│ Order/Item Agent │          │  Payment Agent   │          │  Shipment Agent  │
└────────┬─────────┘          └────────┬─────────┘          └────────┬─────────┘
         └─────────────────────────────┼─────────────────────────────┘
                                       │ validated MCP evidence
                                       ▼
                              ┌──────────────────┐
                              │   Policy Agent   │
                              └────────┬─────────┘
                                       ▼
                              ┌──────────────────┐
                              │  Verifier Agent  │
                              └────────┬─────────┘
                                       ▼
                                  [END OUTPUT]

       Every MCP call includes case_id and every consumed response emits
       tool_result_consumed with the gateway-issued evidence_ref.
```

The coordinator owns the case lifecycle. It discovers tools once, resolves the
case entity, assigns the specialist tasks, collects evidence, invokes policy,
and asks the verifier to finalize. Specialist calls are bounded and scoped to
one case. The workflow does not use a shared cross-case cache.

## 2. Public contracts and internal state

The following files are locked public contracts and must be validated before
use or packaging:

| Contract | Use | Validator |
| --- | --- | --- |
| `l3b-output-v2.schema.json` | One final output per case | `Contracts.validate_output` |
| `trace-event-v1.schema.json` | Observable `trace.jsonl` events | `Contracts.validate_trace` |
| `submission-manifest-v2.schema.json` | ZIP manifest | `Contracts.validate_manifest` |
| `mcp-evidence-response-v1.schema.json` | MCP evidence envelope | `Contracts.validate_evidence` |

`l3a-output-v2.schema.json` remains available for the L3A variant and is not
used by this L3B run. `additionalProperties: false` in the public schemas is a
hard boundary: source code may keep richer facts internally, but final JSON
must contain only fields accepted by the selected schema.

`student_agent.state.CaseState` is the internal state contract. It contains the
case ID, lifecycle phase, resolved and rejected candidates, deduplicated
evidence refs, tool-call count, event IDs and a per-case query budget.
`student_agent.state.Handoff` is the internal A2A envelope. It carries the case
ID, correlation ID, source and target agents, message type, normalized payload
and evidence refs. Neither dataclass is serialized directly as a submission
artifact.

## 3. Agent ownership and least privilege

| Actor | Input | Responsibility | MCP permission | Handoff |
| --- | --- | --- | --- | --- |
| Coordinator | Case and specialist results | Route work, enforce scope/budget, collect results | Discovery only | Assignments and final state |
| Entity agent | Claimed/candidate IDs | Resolve order and classify ambiguity | Resolver/search tools | Resolved IDs and rejected candidates |
| Order/item agent | Resolved order ID | Collect order, items, products and sellers | Order, item and seller tools | Entity facts and evidence refs |
| Payment agent | Order and item totals | Reconcile captured/refunded amounts | Payment and refund tools | Payment verdict and totals |
| Shipment agent | Order and item facts | Evaluate delivery and seller handoff timeline | Shipment tools | Shipment verdict and late sellers |
| Policy agent | Normalized specialist facts | Apply issue precedence, responsibility and actions | Policy read tool | Decision package |
| Verifier agent | Candidate output and state | Validate schema, scope, refs and consistency | No MCP calls | `verification_completed` |

Tool discovery does not grant every agent access to every tool. The concrete
tool name is selected from the discovered catalog, and the coordinator passes
only the minimum arguments needed for the current case. `PURPOSE_PERMISSIONS`
is enforced before tool selection, so an agent cannot call a discovered tool
outside its declared evidence domain.

## 4. A2A message and lifecycle protocol

All handoffs share the originating `case_id` and a correlation ID. A task is
sent only once to each specialist; a result must target the coordinator or the
next declared stage. The allowed lifecycle is:

```text
received → resolving → investigating → policy → verifying → finalized
```

The trace contains only observable lifecycle events: `case_received`,
`task_assigned`, `handoff`, `tool_result_consumed`, `policy_decided`,
`verification_completed` and `case_finalized`. Prompts, chain-of-thought and
API keys are never placed in the envelope, output or trace.

Entity resolution prefers an explicit claimed order ID. When it is absent, the
entity agent may issue one resolver call using the case candidates. A candidate
is accepted only when an MCP response confirms it. No candidate is guessed;
unresolved cases are represented with `ambiguous` or `not_found` state and a
lower confidence.

## 5. Evidence and conflict lifecycle

`EvidenceGateway` validates each MCP response against
`mcp-evidence-response-v1` before the response enters state. The gateway-issued
`evidence_ref` is copied unchanged into the output and trace. Refs are
deduplicated and remain associated with their case. A ref from another case is
never reused.

Specialists return normalized facts plus refs to the coordinator. The policy
agent chooses source precedence for a conflict and preserves an unresolved
conflict when no source is authoritative. The verifier checks that every
submitted ref has the required format, belongs to the current case, and is
linked to a consumed tool result.

## 6. Failure, retry and efficiency policy

| Failure | Retry budget | Fallback | Observable code |
| --- | ---: | --- | --- |
| MCP timeout or transport error | 1 bounded retry | Continue with missing domain and lower confidence | `MCP_UNAVAILABLE` |
| Entity not found or ambiguous | 0 | Emit `not_found`/`ambiguous`; do not invent an ID | `ENTITY_UNRESOLVED` |
| Source conflict | 0 | Apply documented precedence or preserve conflict | `SOURCE_CONFLICT` |
| Invalid specialist result | 0 | Drop invalid result and verify remaining evidence | `INVALID_SPECIALIST_RESULT` |
| Query budget exhausted | 0 | Finalize with available evidence and `needs_investigation` | `QUERY_BUDGET_EXHAUSTED` |

Retries are idempotent and occur only for a timeout or transport failure. A
per-case cache key of `(tool_name, canonical_arguments)` prevents duplicate
audited calls. The default budget is 12 calls per case, including discovery
work that is case-scoped. Missing evidence is never replaced with a fabricated
amount, timestamp, entity ID or evidence ref.

For every successful call, the collector stores the validated envelope,
copies its `evidence_ref` byte-for-byte, and immediately emits exactly one
`tool_result_consumed` event. Failed calls emit no evidence ref. The final
output is built only from records accepted by this collector.

## 7. Verification invariants

Before finalization the verifier checks:

1. The selected L3B output passes the public schema and contains no extra key.
2. `case_id`, resolved IDs, rejected candidates and all evidence refs stay in
   the current case scope.
3. Evidence refs are gateway-issued, unique, and linked to trace events.
4. Shipment status, payment/refund totals, responsibility and actions agree.
5. Refund amounts are non-negative, bounded by supported evidence, and have
   matching resolution actions.
6. Confidence values stay within `[0, 1]`, and unresolved facts reduce
   confidence rather than becoming assertions.

The policy engine loads `scoring-policy-v2.json` and rejects an unexpected
policy version or invalid L3B weight set. Issue precedence is deterministic:
paid canceled/unavailable orders, shipment responsibility, refund state,
duplicate/mismatched capture, valid split payment, then unsupported or
insufficient claims. Refund lines always sum exactly to the recommended refund;
already-refunded value is not recommended again.

Calibration uses evidence-domain coverage, successful entity resolution,
timeline completeness, policy evidence and conflict penalties. It never emits
`1.0`; missing required evidence caps confidence at `0.69`, any source conflict
caps it at `0.75`, and insufficient evidence caps it at `0.40`.

## 8. Reproducibility

The workflow uses Python 3.11+, dependencies pinned by the version ranges in
`pyproject.toml`, sequential specialist calls, a fixed query budget and no
random seed or hidden model state. Run `pytest -q`, `day09 mcp-tools`, then
`day09 run`, `day09 validate` and `day09 package`. Secrets stay in `.env` and
are excluded from source control and submission ZIPs.
