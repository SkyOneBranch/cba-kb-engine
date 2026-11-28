"""Stateless Streamable HTTP MCP transport and cloud function adapters (REQ-250).

100% Python stdlib.
- ``handle_mcp_http_request``: pure stateless HTTP dispatcher for:
  - ``GET /healthz`` -> 200 redacted readiness JSON
  - ``GET /mcp`` -> 405 Method Not Allowed with ``Allow: POST, OPTIONS``
  - ``OPTIONS /mcp`` & ``DELETE /mcp`` -> 204 No Content
  - ``POST /mcp`` -> single & batch JSON-RPC 2.0 (notifications -> 202 Accepted)
- Zero-framework serverless adapters:
  - ``make_aliyun_fc3_handler`` (Aliyun FC 3.0 HTTP event dict & WSGI)
  - ``make_tencent_scf_handler`` (Tencent SCF API Gateway / Function URL event dict)
- ``serve_streamable_http``: stdlib ``ThreadingHTTPServer`` local/container host.
"""
from __future__ import annotations

import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Mapping, Tuple

from .mcp_auth import MCPACLConfig, MCPAuthError, authenticate_http_headers
from .mcp_server import (
    SUPPORTED_MCP_PROTOCOL_VERSIONS,
    TieredKnowledgeMCPServer,
    _jsonrpc_error,
)

__all__ = [
    "SUPPORTED_MCP_PROTOCOL_VERSIONS",
    "handle_mcp_http_request",
    "make_aliyun_fc3_handler",
    "make_tencent_scf_handler",
    "serve_streamable_http",
]

_CORS_HEADERS: Dict[str, str] = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS, DELETE",
    "Access-Control-Allow-Headers": (
        "Authorization, Content-Type, Accept, Mcp-Session-Id, "
        "MCP-Protocol-Version, X-DingTalk-User-Id, X-DingTalk-User-Job-Number"
    ),
}


def _normalize_path(path: str) -> str:
    clean = (path or "/").split("?", 1)[0].strip()
    if not clean:
        return "/"
    if len(clean) > 1 and clean.endswith("/"):
        clean = clean.rstrip("/")
    return clean


def handle_mcp_http_request(
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes | str,
    *,
    server: TieredKnowledgeMCPServer,
    acl: MCPACLConfig,
    mcp_path: str = "/mcp",
) -> Tuple[int, Dict[str, str], bytes]:
    """Stateless Streamable HTTP request handler for MCP JSON-RPC 2.0."""
    verb = (method or "GET").strip().upper()
    clean_path = _normalize_path(path)
    norm_mcp_path = _normalize_path(mcp_path)

    # Readiness probe (redacted, zero secrets)
    if clean_path == "/healthz":
        if verb != "GET":
            return (
                405,
                {"Allow": "GET", "Content-Type": "application/json; charset=utf-8"},
                json.dumps({"error": "METHOD_NOT_ALLOWED"}).encode("utf-8"),
            )
        health_payload = {
            "status": "ok",
            "release_id": server.dataset.release_id,
            "observed_until": server.dataset.observed_until,
            "entity_count": server.dataset.entity_count,
        }
        return (
            200,
            {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"},
            json.dumps(health_payload, ensure_ascii=False).encode("utf-8"),
        )

    if clean_path != norm_mcp_path:
        return (
            404,
            {"Content-Type": "application/json; charset=utf-8", **_CORS_HEADERS},
            json.dumps({"error": "NOT_FOUND"}).encode("utf-8"),
        )

    if verb in ("OPTIONS", "DELETE"):
        return (
            204,
            {"Allow": "POST, OPTIONS, DELETE", **_CORS_HEADERS},
            b"",
        )

    if verb == "GET":
        # Critical for DingTalk Qwen / Streamable HTTP clients that probe SSE on GET /mcp
        return (
            405,
            {
                "Allow": "POST, OPTIONS",
                "Content-Type": "application/json; charset=utf-8",
                **_CORS_HEADERS,
            },
            json.dumps({"error": "METHOD_NOT_ALLOWED", "message": "Use POST for MCP JSON-RPC"}).encode("utf-8"),
        )

    if verb != "POST":
        return (
            405,
            {
                "Allow": "POST, OPTIONS",
                "Content-Type": "application/json; charset=utf-8",
                **_CORS_HEADERS,
            },
            json.dumps({"error": "METHOD_NOT_ALLOWED"}).encode("utf-8"),
        )

    # Authenticate POST /mcp via dual-factor ACL gateway
    try:
        auth_ctx = authenticate_http_headers(headers or {}, acl)
    except MCPAuthError as exc:
        return (
            exc.status_code,
            {
                "Content-Type": "application/json; charset=utf-8",
                "WWW-Authenticate": "Bearer",
                **_CORS_HEADERS,
            },
            json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False).encode("utf-8"),
        )

    # Decode body
    if isinstance(body, bytes):
        raw_text = body.decode("utf-8", errors="replace").strip()
    else:
        raw_text = str(body or "").strip()

    if not raw_text:
        err = _jsonrpc_error(None, -32700, "Parse error: empty body")
        return (
            400,
            {"Content-Type": "application/json; charset=utf-8", **_CORS_HEADERS},
            json.dumps(err, ensure_ascii=False).encode("utf-8"),
        )

    try:
        payload = json.loads(raw_text)
    except ValueError:
        err = _jsonrpc_error(None, -32700, "Parse error: invalid JSON")
        return (
            400,
            {"Content-Type": "application/json; charset=utf-8", **_CORS_HEADERS},
            json.dumps(err, ensure_ascii=False).encode("utf-8"),
        )

    rpc_response = server.handle_jsonrpc_payload(payload, auth_context=auth_ctx)
    if rpc_response is None:
        # Pure notification(s) such as notifications/initialized -> 202 Accepted empty body
        return (202, {**_CORS_HEADERS}, b"")

    return (
        200,
        {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store",
            **_CORS_HEADERS,
        },
        json.dumps(rpc_response, ensure_ascii=False).encode("utf-8"),
    )


def _decode_event_body(event: Mapping[str, Any]) -> bytes:
    raw_body = event.get("body")
    if raw_body is None:
        return b""
    if isinstance(raw_body, bytes):
        body_bytes = raw_body
    else:
        body_bytes = str(raw_body).encode("utf-8")
    if event.get("isBase64Encoded"):
        try:
            return base64.b64decode(body_bytes, validate=True)
        except Exception as exc:
            raise ValueError("INVALID_BASE64_BODY") from exc
    return body_bytes


def make_aliyun_fc3_handler(
    server: TieredKnowledgeMCPServer,
    acl: MCPACLConfig,
    mcp_path: str = "/mcp",
) -> Callable[..., Any]:
    """Create a zero-framework Aliyun Function Compute 3.0 HTTP handler (event dict or WSGI)."""

    def _handler(arg1: Any, arg2: Any = None) -> Any:
        # WSGI mode: (environ, start_response)
        if callable(arg2) and isinstance(arg1, Mapping) and "REQUEST_METHOD" in arg1:
            environ = arg1
            start_response = arg2
            method = str(environ.get("REQUEST_METHOD") or "GET")
            path = str(environ.get("PATH_INFO") or "/")
            headers: Dict[str, str] = {}
            if environ.get("CONTENT_TYPE"):
                headers["Content-Type"] = str(environ["CONTENT_TYPE"])
            for k, v in environ.items():
                if k.startswith("HTTP_"):
                    hdr_name = k[5:].replace("_", "-")
                    headers[hdr_name] = str(v)
            try:
                content_length = int(environ.get("CONTENT_LENGTH") or 0)
            except (TypeError, ValueError):
                content_length = 0
            wsgi_input = environ.get("wsgi.input")
            body = wsgi_input.read(content_length) if wsgi_input is not None and content_length > 0 else b""
            status, resp_headers, resp_body = handle_mcp_http_request(
                method, path, headers, body, server=server, acl=acl, mcp_path=mcp_path
            )
            reason = {200: "OK", 202: "Accepted", 204: "No Content", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 405: "Method Not Allowed"}.get(status, "OK")
            start_response(f"{status} {reason}", list(resp_headers.items()))
            return [resp_body]

        # FC 3.0 HTTP Event dict mode: (event, context)
        event = arg1 if isinstance(arg1, Mapping) else {}
        req_ctx = event.get("requestContext") or {}
        http_ctx = req_ctx.get("http") if isinstance(req_ctx, Mapping) else {}
        if not isinstance(http_ctx, Mapping):
            http_ctx = {}
        method = str(http_ctx.get("method") or event.get("httpMethod") or "GET")
        path = str(event.get("rawPath") or http_ctx.get("path") or event.get("path") or "/")
        headers = event.get("headers") or {}
        try:
            body = _decode_event_body(event)
        except ValueError:
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json; charset=utf-8", **_CORS_HEADERS},
                "body": json.dumps({"error": "INVALID_BASE64_BODY"}, ensure_ascii=False),
                "isBase64Encoded": False,
            }

        status, resp_headers, resp_body = handle_mcp_http_request(
            method, path, headers, body, server=server, acl=acl, mcp_path=mcp_path
        )
        return {
            "statusCode": status,
            "headers": resp_headers,
            "body": resp_body.decode("utf-8"),
            "isBase64Encoded": False,
        }

    return _handler


def make_tencent_scf_handler(
    server: TieredKnowledgeMCPServer,
    acl: MCPACLConfig,
    mcp_path: str = "/mcp",
) -> Callable[[Mapping[str, Any], Any], Dict[str, Any]]:
    """Create a zero-framework Tencent Cloud SCF HTTP/API Gateway handler."""

    def _handler(event: Mapping[str, Any], context: Any = None) -> Dict[str, Any]:
        ev = event if isinstance(event, Mapping) else {}
        req_ctx = ev.get("requestContext") or {}
        http_ctx = req_ctx.get("http") if isinstance(req_ctx, Mapping) else {}
        if not isinstance(http_ctx, Mapping):
            http_ctx = {}
        method = str(ev.get("httpMethod") or http_ctx.get("method") or "GET")
        path = str(ev.get("path") or ev.get("rawPath") or http_ctx.get("path") or "/")
        headers = ev.get("headers") or {}
        try:
            body = _decode_event_body(ev)
        except ValueError:
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json; charset=utf-8", **_CORS_HEADERS},
                "body": json.dumps({"error": "INVALID_BASE64_BODY"}, ensure_ascii=False),
                "isBase64Encoded": False,
            }

        status, resp_headers, resp_body = handle_mcp_http_request(
            method, path, headers, body, server=server, acl=acl, mcp_path=mcp_path
        )
        return {
            "statusCode": status,
            "headers": resp_headers,
            "body": resp_body.decode("utf-8"),
            "isBase64Encoded": False,
        }

    return _handler


def serve_streamable_http(
    server: TieredKnowledgeMCPServer,
    acl: MCPACLConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    path: str = "/mcp",
) -> None:
    """Run stdlib ``ThreadingHTTPServer`` serving stateless Streamable HTTP MCP."""

    class _MCPRequestHandler(BaseHTTPRequestHandler):
        def _dispatch(self) -> None:
            length_raw = self.headers.get("Content-Length") or "0"
            try:
                length = max(0, int(length_raw))
            except ValueError:
                length = 0
            body = self.rfile.read(length) if length > 0 else b""
            hdr_dict = {k: v for k, v in self.headers.items()}
            status, resp_headers, resp_body = handle_mcp_http_request(
                self.command,
                self.path,
                hdr_dict,
                body,
                server=server,
                acl=acl,
                mcp_path=path,
            )
            self.send_response(status)
            for k, v in resp_headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            if resp_body:
                self.wfile.write(resp_body)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._dispatch()

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Suppress default stderr HTTP access logs to avoid leaking headers/paths
            return

    httpd = ThreadingHTTPServer((host, int(port)), _MCPRequestHandler)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
