"""HTTPS Action Gateway for REQ-230 (CBA-KB v2.3.0) On-Demand Snowball.

Provides:
- ``verify_bearer_token``: fail-closed constant-time authorization check.
- ``SnowballGatewayApp``: framework-free HTTP request router supporting:
    - ``POST /v1/person`` (fast path + bounded 28s async poll)
    - ``GET /v1/coverage``
    - ``GET /v1/jobs/<job_id>``
- ``serve_http``: optional stdlib ``http.server`` wrapper (zero third-party deps).
"""
from __future__ import annotations

import hmac
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, Mapping, Optional, Tuple
from urllib.parse import unquote

_JSON_HEADERS: Dict[str, str] = {"Content-Type": "application/json; charset=utf-8"}
_TERMINAL_JOB_STATUSES = frozenset({
    "COMPLETED",
    "ACQUIRED_AND_BUILT",
    "COMPLETED_UNKNOWN",
    "UNKNOWN_AFTER_WHITELIST_CHECK",
    "FAILED",
    "ERROR",
})


def verify_bearer_token(
    authorization_header: Optional[str],
    expected_token: str,
) -> bool:
    """Verify an HTTP Authorization header using constant-time comparison.

    Fails closed when ``expected_token`` is empty/whitespace, when the header
    is missing, or when the scheme is not ``bearer`` (case-insensitive).
    """
    if not isinstance(expected_token, str) or not expected_token.strip():
        return False
    if not isinstance(authorization_header, str) or not authorization_header.strip():
        return False
    parts = authorization_header.strip().split(None, 1)
    if len(parts) != 2:
        return False
    scheme, supplied = parts[0], parts[1].strip()
    if scheme.lower() != "bearer" or not supplied:
        return False
    return hmac.compare_digest(
        supplied.encode("utf-8"),
        expected_token.encode("utf-8"),
    )


def _lookup_header(headers: Optional[Mapping[str, str]], name: str) -> Optional[str]:
    if not headers:
        return None
    target = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == target:
            return value
    return None


class SnowballGatewayApp:
    """Framework-free HTTPS gateway router for the On-Demand Snowball loop."""

    def __init__(
        self,
        orchestrator: Any,
        *,
        bearer_token: str,
        wait_timeout_seconds: float = 28.0,
        poll_interval_seconds: float = 1.0,
        sleep_fn: Optional[Callable[[float], None]] = None,
        monotonic_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.bearer_token = bearer_token
        self.wait_timeout_seconds = max(0.0, float(wait_timeout_seconds))
        self.poll_interval_seconds = max(0.0, float(poll_interval_seconds))
        self._sleep = sleep_fn or time.sleep
        self._monotonic = monotonic_fn or time.monotonic

    def handle_request(
        self,
        method: str,
        path: str,
        headers: Optional[Mapping[str, str]],
        body: bytes = b"",
    ) -> Tuple[int, Dict[str, str], Dict[str, Any]]:
        """Dispatch an HTTP request and return ``(status_code, headers, json_body)``."""
        resp_headers = dict(_JSON_HEADERS)
        auth_header = _lookup_header(headers, "Authorization")
        if not verify_bearer_token(auth_header, self.bearer_token):
            return 401, resp_headers, {"error": "UNAUTHORIZED"}

        verb = (method or "").upper()
        clean_path = (path or "").split("?", 1)[0]

        if verb == "GET" and clean_path == "/v1/coverage":
            cov = self.orchestrator.get_league_coverage()
            return 200, resp_headers, dict(cov)

        if verb == "GET" and clean_path.startswith("/v1/jobs/"):
            raw_job_id = clean_path[len("/v1/jobs/"):].strip("/")
            job_id = unquote(raw_job_id)
            if not job_id or "/" in job_id or "\\" in job_id or ".." in job_id:
                return 400, resp_headers, {"error": "INVALID_JOB_ID"}
            job_res = self.orchestrator.check_acquisition_job(job_id)
            if job_res.get("status") == "NOT_FOUND":
                return 404, resp_headers, dict(job_res)
            return 200, resp_headers, dict(job_res)

        if verb == "POST" and clean_path == "/v1/person":
            return self._handle_post_person(body, resp_headers)

        return 404, resp_headers, {"error": "NOT_FOUND"}

    def _handle_post_person(
        self,
        body: Any,
        resp_headers: Dict[str, str],
    ) -> Tuple[int, Dict[str, str], Dict[str, Any]]:
        if isinstance(body, dict):
            payload = body
        else:
            raw_bytes = body.encode("utf-8") if isinstance(body, str) else bytes(body or b"")
            try:
                payload = json.loads(raw_bytes.decode("utf-8")) if raw_bytes else {}
            except (UnicodeDecodeError, ValueError):
                return 400, resp_headers, {"error": "INVALID_JSON"}
            if not isinstance(payload, dict):
                return 400, resp_headers, {"error": "INVALID_JSON_OBJECT"}

        person = payload.get("person")
        if not isinstance(person, str) or not person.strip():
            return 400, resp_headers, {"error": "PERSON_REQUIRED"}

        season = str(payload.get("season") or "2026-2027")
        force_refresh = bool(payload.get("force_refresh", False))
        kwargs: Dict[str, Any] = {
            "season": season,
            "force_refresh": force_refresh,
        }
        if "freshness_ttl_hours" in payload and payload["freshness_ttl_hours"] is not None:
            kwargs["freshness_ttl_hours"] = float(payload["freshness_ttl_hours"])

        res = dict(self.orchestrator.get_or_acquire_person(person.strip(), **kwargs))
        if res.get("status") == "ACQUIRING_IN_CLOUD" and res.get("job_id"):
            return self._bounded_wait_for_job(res, resp_headers)
        return 200, resp_headers, res

    def _bounded_wait_for_job(
        self,
        initial_response: Dict[str, Any],
        resp_headers: Dict[str, str],
    ) -> Tuple[int, Dict[str, str], Dict[str, Any]]:
        job_id = str(initial_response["job_id"])
        deadline = self._monotonic() + self.wait_timeout_seconds
        poll_interval = self.poll_interval_seconds if self.poll_interval_seconds > 0 else 1.0

        while self._monotonic() < deadline:
            remaining = deadline - self._monotonic()
            step = min(poll_interval, remaining)
            if step <= 0:
                break
            self._sleep(step)
            polled = self.orchestrator.check_acquisition_job(job_id)
            if polled.get("status") in _TERMINAL_JOB_STATUSES:
                return 200, resp_headers, dict(polled)

        out = dict(initial_response)
        out["status"] = "ACQUIRING_IN_CLOUD"
        out["job_id"] = job_id
        if not out.get("next_action"):
            out["next_action"] = "call check_acquisition_job"
        return 200, resp_headers, out


def serve_http(
    app: SnowballGatewayApp,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
) -> None:
    """Serve ``SnowballGatewayApp`` using Python's stdlib ``HTTPServer``."""

    class _Handler(BaseHTTPRequestHandler):
        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw_body = self.rfile.read(length) if length > 0 else b""
            hdrs = {k: v for k, v in self.headers.items()}
            status, resp_hdrs, payload = app.handle_request(
                self.command,
                self.path,
                hdrs,
                raw_body,
            )
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            for k, v in resp_hdrs.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = HTTPServer((host, port), _Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
