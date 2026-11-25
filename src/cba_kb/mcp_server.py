"""Local stdio JSON-RPC 2.0 MCP Server for REQ-230 (CBA-KB v2.3.0).

Exposes the three On-Demand Snowball tools over standard input/output using
zero third-party dependencies:
- ``get_or_acquire_person``
- ``get_league_coverage``
- ``check_acquisition_job``
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict, IO, List, Mapping, Optional

MCP_PROTOCOL_VERSION = "2024-11-05"

_TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "get_or_acquire_person",
        "description": (
            "Query or acquire a CBA person research card with Observation Horizon "
            "(observed_until, coverage_status) from the cloud snowball loop."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "person": {
                    "type": "string",
                    "description": "Person name or player_id / player_uid to query.",
                },
                "season": {
                    "type": "string",
                    "description": "Target CBA season (default: 2026-2027).",
                    "default": "2026-2027",
                },
                "force_refresh": {
                    "type": "boolean",
                    "description": "Bypass fresh cache and negative receipt TTL.",
                    "default": False,
                },
                "freshness_ttl_hours": {
                    "type": "number",
                    "description": "Cache freshness TTL in hours (default: 24.0).",
                    "default": 24.0,
                },
            },
            "required": ["person"],
        },
    },
    {
        "name": "get_league_coverage",
        "description": (
            "Return league-wide Observation Horizon coverage status across the 5 "
            "CBA proposition domains."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "check_acquisition_job",
        "description": (
            "Check status of an asynchronous cloud acquisition job and retrieve "
            "the completed person research card markdown when ready."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Cloud acquisition job identifier.",
                },
            },
            "required": ["job_id"],
        },
    },
]


def _jsonrpc_error(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


class SnowballMCPServer:
    """JSON-RPC 2.0 MCP server wrapping ``SnowballOrchestrator``."""

    def __init__(
        self,
        orchestrator: Any,
        *,
        server_name: str = "cba-kb-snowball",
        server_version: str = "2.3.0",
    ) -> None:
        self.orchestrator = orchestrator
        self.server_name = server_name
        self.server_version = server_version
        self.tools = [dict(t) for t in _TOOL_DEFINITIONS]

    def handle_jsonrpc_message(
        self,
        message: Mapping[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Process a single JSON-RPC 2.0 request dict and return a response dict (or None for notifications)."""
        if not isinstance(message, Mapping):
            return _jsonrpc_error(None, -32600, "Invalid Request")

        msg_id = message.get("id")
        method = message.get("method")
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(method, str) or not method:
            return _jsonrpc_error(msg_id, -32600, "Invalid Request: missing method")

        # Notifications have no id and expect no response.
        if msg_id is None and method.startswith("notifications/"):
            return None

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": self.server_name,
                        "version": self.server_version,
                    },
                },
            }

        if method == "ping":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {},
            }

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "tools": [dict(t) for t in self.tools],
                },
            }

        if method == "tools/call":
            if not isinstance(params, Mapping):
                return _jsonrpc_error(msg_id, -32602, "Invalid params")
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(tool_name, str) or not isinstance(arguments, Mapping):
                return _jsonrpc_error(msg_id, -32602, "Invalid tool call params")
            return self._call_tool(msg_id, tool_name, arguments)

        return _jsonrpc_error(msg_id, -32601, f"Method not found: {method}")

    def _call_tool(
        self,
        msg_id: Any,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> Dict[str, Any]:
        try:
            if tool_name == "get_or_acquire_person":
                person = arguments.get("person")
                if not isinstance(person, str) or not person.strip():
                    return _jsonrpc_error(msg_id, -32602, "Missing required argument: person")
                season = str(arguments.get("season") or "2026-2027")
                force_refresh = bool(arguments.get("force_refresh", False))
                freshness_ttl_hours = float(arguments.get("freshness_ttl_hours", 24.0))
                result = self.orchestrator.get_or_acquire_person(
                    person.strip(),
                    season=season,
                    force_refresh=force_refresh,
                    freshness_ttl_hours=freshness_ttl_hours,
                )
            elif tool_name == "get_league_coverage":
                result = self.orchestrator.get_league_coverage()
            elif tool_name == "check_acquisition_job":
                job_id = arguments.get("job_id")
                if not isinstance(job_id, str) or not job_id.strip():
                    return _jsonrpc_error(msg_id, -32602, "Missing required argument: job_id")
                result = self.orchestrator.check_acquisition_job(job_id.strip())
            else:
                return _jsonrpc_error(msg_id, -32602, f"Unknown tool: {tool_name}")
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"error": type(exc).__name__, "message": str(exc)},
                                ensure_ascii=False,
                            ),
                        }
                    ],
                    "isError": True,
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, ensure_ascii=False),
                    }
                ],
                "isError": False,
            },
        }

    def serve_stdio(
        self,
        stdin: Optional[IO[str]] = None,
        stdout: Optional[IO[str]] = None,
    ) -> None:
        """Run line-delimited JSON-RPC 2.0 loop over stdio."""
        in_stream = stdin if stdin is not None else sys.stdin
        out_stream = stdout if stdout is not None else sys.stdout

        for raw_line in in_stream:
            line = raw_line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                err = _jsonrpc_error(None, -32700, "Parse error")
                out_stream.write(json.dumps(err, ensure_ascii=False) + "\n")
                out_stream.flush()
                continue

            resp = self.handle_jsonrpc_message(msg)
            if resp is not None:
                out_stream.write(json.dumps(resp, ensure_ascii=False) + "\n")
                out_stream.flush()
