from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp import types

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway, GatewayError


class FakeSession:
    def __init__(self) -> None:
        self.list_count = 0
        self.calls: list[dict[str, Any]] = []

    async def list_tools(self, *, params: Any = None) -> types.ListToolsResult:
        self.list_count += 1
        schema = {
            "type": "object",
            "required": ["case_id", "order_id"],
            "properties": {"case_id": {"type": "string"}, "order_id": {"type": "string"}},
        }
        return types.ListToolsResult(
            tools=[types.Tool(name="get_order", description="order", inputSchema=schema)]
        )

    async def call_tool(self, name: str, *, arguments: dict[str, Any]) -> types.CallToolResult:
        self.calls.append({"name": name, "arguments": arguments})
        envelope = {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_" + "a" * 24,
            "result_hash": "sha256:" + "0" * 64,
            "domain": "order",
            "data": {"order_id": arguments["order_id"]},
        }
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(envelope))]
        )


def test_gateway_discovers_once_passes_case_id_and_binds_ref(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    gateway = EvidenceGateway(FakeSession(), Contracts(root / "contracts" / "schemas"))

    async def run() -> None:
        assert await gateway.list_tools() == ["get_order"]
        evidence = await gateway.call("get_order", case_id="CASE_001", order_id="ORDER_001")
        assert evidence["evidence_ref"].startswith("ev_")
        with pytest.raises(GatewayError, match="EVIDENCE_SCOPE_OR_HASH_MISMATCH"):
            await gateway.call("get_order", case_id="CASE_002", order_id="ORDER_002")

    asyncio.run(run())
    session = gateway._session  # noqa: SLF001 - inspect the fake audit calls.
    assert session.list_count == 1
    assert session.calls[0]["arguments"]["case_id"] == "CASE_001"
