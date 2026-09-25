from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import httpx2
from jsonschema import Draft202012Validator
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from .contracts import ContractError, Contracts
from .state import CASE_ID_PATTERN


class GatewayError(RuntimeError):
    """Safe failure code; server messages/credentials are not copied to trace."""

    def __init__(
        self, code: str, *, retryable: bool = False, detail: str | None = None
    ) -> None:
        safe_detail = re.sub(r"sk-team-[A-Za-z0-9_-]+", "[REDACTED]", detail or "")[:240]
        super().__init__(f"{code}: {safe_detail}" if safe_detail else code)
        self.code = code
        self.retryable = retryable
        self.detail = safe_detail


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool | None = None

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        if not Draft202012Validator(self.input_schema).is_valid(arguments):
            raise GatewayError("INVALID_TOOL_ARGUMENTS")


class EvidenceGateway:
    def __init__(
        self, session: ClientSession, contracts: Contracts, *, timeout_seconds: float = 30
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._session = session
        self._contracts = contracts
        self._timeout_seconds = timeout_seconds
        self._catalog: dict[str, ToolSpec] | None = None
        self._ref_scope: dict[str, tuple[str, str]] = {}

    async def discover_tools(self) -> dict[str, ToolSpec]:
        """Keep descriptions and argument schemas, including paginated tools."""
        if self._catalog is not None:
            return deepcopy(self._catalog)
        catalog: dict[str, ToolSpec] = {}
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(20):
            async with asyncio.timeout(self._timeout_seconds):
                result = await self._session.list_tools(
                    params=types.PaginatedRequestParams(cursor=cursor) if cursor else None
                )
            # MCP 2 uses snake_case attributes; aliases are the MCP wire contract.
            if hasattr(result, "model_dump"):
                response = result.model_dump(by_alias=True)
            else:
                response = {
                    "tools": [self._tool_payload(tool) for tool in result.tools],
                    "nextCursor": getattr(result, "nextCursor", None),
                }
            for tool in response["tools"]:
                schema = tool["inputSchema"]
                Draft202012Validator.check_schema(schema)
                annotations = tool.get("annotations") or {}
                if hasattr(annotations, "model_dump"):
                    annotations = annotations.model_dump(by_alias=True)
                if not isinstance(annotations, dict):
                    annotations = {}
                catalog[tool["name"]] = ToolSpec(
                    tool["name"], tool.get("description") or "", schema,
                    annotations.get("readOnlyHint"),
                )
            cursor = response.get("nextCursor")
            if not cursor:
                self._catalog = catalog
                return deepcopy(catalog)
            if cursor in seen_cursors:
                raise GatewayError("INVALID_DISCOVERY_PAGINATION")
            seen_cursors.add(cursor)
        raise GatewayError("DISCOVERY_PAGE_LIMIT")

    @staticmethod
    def _tool_payload(tool: Any) -> dict[str, Any]:
        if hasattr(tool, "model_dump"):
            return tool.model_dump(by_alias=True)
        annotations = getattr(tool, "annotations", None)
        return {
            "name": tool.name,
            "description": getattr(tool, "description", None),
            "inputSchema": getattr(tool, "inputSchema", getattr(tool, "input_schema", {})),
            "annotations": annotations,
        }

    async def list_tools(self) -> list[str]:
        return sorted(await self.discover_tools())

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        if not isinstance(case_id, str) or not CASE_ID_PATTERN.fullmatch(case_id):
            raise GatewayError("INVALID_CASE_ID")
        catalog = await self.discover_tools()
        if tool_name not in catalog:
            raise GatewayError("UNDISCOVERED_TOOL")
        payload = {"case_id": case_id, **arguments}
        catalog[tool_name].validate_arguments(payload)
        try:
            async with asyncio.timeout(self._timeout_seconds):
                result = await self._session.call_tool(tool_name, arguments=payload)
        except (TimeoutError, httpx2.TimeoutException):
            raise GatewayError("MCP_TIMEOUT", retryable=True) from None
        except httpx2.HTTPStatusError as exc:
            status = exc.response.status_code
            raise GatewayError(
                "MCP_AUTH_DENIED" if status in {401, 403} else "MCP_HTTP_ERROR",
                retryable=status in {502, 503, 504},
            ) from None
        except httpx2.TransportError:
            raise GatewayError("MCP_TRANSPORT_ERROR", retryable=True) from None
        except MCPError:
            # Tool/protocol errors (including access denial) are not transient.
            raise GatewayError("MCP_PROTOCOL_ERROR") from None
        except RuntimeError:
            raise GatewayError("MCP_PROTOCOL_ERROR") from None
        if hasattr(result, "model_dump"):
            response = result.model_dump(by_alias=True)
        else:
            structured = getattr(result, "structuredContent", None)
            if structured is None:
                structured = getattr(result, "structured_content", None)
            response = {
                "isError": getattr(result, "isError", getattr(result, "is_error", False)),
                "structuredContent": structured,
                "content": [
                    {
                        "type": "text",
                        "text": block.text,
                    }
                    for block in getattr(result, "content", [])
                    if getattr(block, "text", None)
                ],
            }
        if response.get("isError"):
            detail = "; ".join(
                block.get("text", "")
                for block in response.get("content", [])
                if block.get("type") == "text"
            )
            raise GatewayError("MCP_TOOL_ERROR", detail=detail)
        evidence = response.get("structuredContent")
        if evidence is None:
            text_blocks = [
                block["text"] for block in response.get("content", [])
                if block.get("type") == "text"
            ]
            if len(text_blocks) != 1:
                raise GatewayError("INVALID_EVIDENCE_RESPONSE")
            try:
                evidence = json.loads(text_blocks[0])
            except (TypeError, json.JSONDecodeError):
                raise GatewayError("INVALID_EVIDENCE_JSON") from None
        try:
            self._contracts.validate_evidence(evidence, "MCP response")
        except ContractError:
            raise GatewayError("INVALID_EVIDENCE_CONTRACT") from None
        ref = evidence["evidence_ref"]
        # The envelope has no case/team field. Bind it to the authenticated
        # request locally; only the server audit can verify global provenance.
        binding = (case_id, evidence["result_hash"])
        previous = self._ref_scope.get(ref)
        if previous is not None and previous != binding:
            raise GatewayError("EVIDENCE_SCOPE_OR_HASH_MISMATCH")
        self._ref_scope[ref] = binding
        return deepcopy(evidence)


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts)
