from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import student_agent.cases as cases_module
from student_agent.cases import CaseSet
from student_agent.contracts import Contracts
from student_agent.submission import package_submission, validate_artifacts
from student_agent.trace import TraceWriter


def valid_output(case_id: str, evidence_ref: str) -> dict[str, object]:
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": "unsupported_claim",
            "secondary_issues": [],
            "case_status": "no_action",
            "confidence": 0.8,
        },
        "affected_entities": {
            "order_ids": ["ORDER_001"],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "entity_resolution": {
            "status": "resolved",
            "resolved_order_ids": ["ORDER_001"],
            "rejected_candidates": [],
            "confidence": 0.95,
        },
        "customer_context": {"customer_unique_id": None, "related_order_ids": []},
        "shipment_analysis": {
            "verdict": "on_time",
            "late_seller_ids": [],
            "timeline_complete": True,
        },
        "payment_analysis": {
            "verdict": "reconciled",
            "captured_total_brl": 10.0,
            "refunded_total_brl": 0.0,
            "refundable_total_brl": 0.0,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "DELIVERY_WITHIN_ESTIMATE", "rank": 1}],
            "responsible_parties": [{"party_type": "customer", "party_id": None}],
        },
        "evidence_refs": [evidence_ref],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        "resolution_actions": ["reject_late_refund"],
    }


def write_complete_trace(root: Path, contracts: Contracts, case_id: str, ref: str) -> None:
    trace = TraceWriter(root / "traces" / "trace.jsonl", contracts)
    trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order-agent",
    )
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor="order-agent",
        tool_name="get_order",
        evidence_refs=[ref],
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="order-agent",
        target="policy-agent",
        evidence_refs=[ref],
    )
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code="unsupported_claim",
        evidence_refs=[ref],
    )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        decision_code="CROSS_FIELD_CONSISTENT",
        evidence_refs=[ref],
    )
    trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")


def test_package_contains_only_contract_artifacts(tmp_path: Path, monkeypatch: object) -> None:
    repository = Path(__file__).resolve().parents[1]
    contracts = Contracts(repository / "contracts" / "schemas")
    case_id = "CASE_001"
    evidence_ref = "ev_" + "a" * 24
    case_set = CaseSet("test-v1", "l3b", (case_id,), {case_id: {"case_id": case_id}})
    output_path = tmp_path / "outputs" / f"{case_id}.json"
    output_path.parent.mkdir()
    output_path.write_text(
        json.dumps(valid_output(case_id, evidence_ref)), encoding="utf-8"
    )
    write_complete_trace(tmp_path, contracts, case_id, evidence_ref)
    shutil.copytree(repository / "contracts", tmp_path / "contracts")
    monkeypatch.setattr(cases_module, "load_case_set", lambda root: case_set)  # type: ignore[attr-defined]

    destination = package_submission(tmp_path, tmp_path / "dist" / "submission.zip")

    with zipfile.ZipFile(destination) as archive:
        assert set(archive.namelist()) == {
            "manifest.json",
            "trace.jsonl",
            f"outputs/{case_id}.json",
        }
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["client"] == {
            "name": "tran-quoc-vuong-l3b-agent",
            "version": "1.0.0",
        }


def test_validation_rejects_output_evidence_missing_from_trace(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    contracts = Contracts(repository / "contracts" / "schemas")
    case_id = "CASE_001"
    output_ref = "ev_" + "a" * 24
    trace_ref = "ev_" + "b" * 24
    case_set = CaseSet("test-v1", "l3b", (case_id,), {case_id: {"case_id": case_id}})
    output_path = tmp_path / "outputs" / f"{case_id}.json"
    output_path.parent.mkdir()
    output_path.write_text(json.dumps(valid_output(case_id, output_ref)), encoding="utf-8")
    write_complete_trace(tmp_path, contracts, case_id, trace_ref)

    try:
        validate_artifacts(tmp_path, case_set, contracts)
    except ValueError as exc:
        assert "not linked to trace" in str(exc)
    else:
        raise AssertionError("unlinked output evidence was accepted")
