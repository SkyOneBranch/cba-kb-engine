"""Keep-alive Drive/Docs HTTP transport adapter over requests (REQ-221 P0-1).

Replaces stock httplib2 transport with an httplib2-compatible adapter backed by
``google.auth.transport.requests.AuthorizedSession`` and ``requests`` connection
pooling (``pool_connections=16, pool_maxsize=16``), using zero new third-party
dependencies.

Security & correctness invariants:
- Rejects service-account credentials fail-closed (personal Drive OAuth only).
- Never follows HTTP 308 resumable-upload responses (even if Location is set).
- Follows bounded 301/302/303/307 redirects while stripping sensitive headers
  (Authorization, Proxy-Authorization, Cookie) on cross-host hops.
- Maps requests TLS/timeout/connection failures to stdlib transient error types
  consumed by ``cba_kb.transport.retry_read``.
- Never logs, prints, or stores credentials.
"""
from __future__ import annotations

import http.client
import socket
import ssl
from urllib.parse import urljoin, urlsplit

DRIVE_TRANSPORT = "requests"
DEFAULT_TIMEOUT_SEC = 300
DEFAULT_POOL_CONNECTIONS = 16
DEFAULT_POOL_MAXSIZE = 16
DEFAULT_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307})
_SENSITIVE_REDIRECT_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})


class RequestsHttpResponse(dict):
    """httplib2.Response-shaped view over a requests response."""

    def __init__(self, status: int, headers, reason: str | None = None) -> None:
        super().__init__({str(k).lower(): v for k, v in dict(headers or {}).items()})
        self.status = int(status)
        self.reason = (
            str(reason)
            if reason
            else http.client.responses.get(self.status, "")
        )


def _same_origin(url_a: str, url_b: str) -> bool:
    a = urlsplit(url_a)
    b = urlsplit(url_b)
    port_a = a.port or (443 if a.scheme.lower() == "https" else 80)
    port_b = b.port or (443 if b.scheme.lower() == "https" else 80)
    return (
        a.scheme.lower() == b.scheme.lower()
        and (a.hostname or "").lower() == (b.hostname or "").lower()
        and port_a == port_b
    )


def _strip_sensitive_headers(headers: dict) -> dict:
    return {
        k: v for k, v in headers.items()
        if str(k).lower() not in _SENSITIVE_REDIRECT_HEADERS
    }


def _header_get(headers, name: str):
    target = name.lower()
    for k, v in dict(headers or {}).items():
        if str(k).lower() == target:
            return v
    return None


class RequestsHttp:
    """httplib2-compatible ``http`` object backed by a pooled requests session."""

    def __init__(self, session, timeout: float = DEFAULT_TIMEOUT_SEC) -> None:
        self._http_client = session
        self.timeout = timeout

    @property
    def session(self):
        return self._http_client

    def close(self) -> None:
        close_fn = getattr(self._http_client, "close", None)
        if callable(close_fn):
            close_fn()

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        """Implement the httplib2 ``Http.request`` contract: ``(response, content)``."""
        import requests

        max_redirects = kwargs.get("redirections", DEFAULT_MAX_REDIRECTS)
        try:
            max_redirects = max(0, int(max_redirects))
        except (TypeError, ValueError):
            max_redirects = DEFAULT_MAX_REDIRECTS

        current_uri = uri
        current_method = method
        current_body = body
        current_headers = dict(headers) if headers else {}

        for hop in range(max_redirects + 1):
            try:
                resp = self._http_client.request(
                    current_method,
                    current_uri,
                    data=current_body,
                    headers=current_headers,
                    allow_redirects=False,
                    timeout=self.timeout,
                )
            except requests.exceptions.SSLError as exc:
                raise ssl.SSLError(str(exc)) from exc
            except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, requests.exceptions.Timeout) as exc:
                raise socket.timeout(str(exc)) from exc
            except requests.exceptions.ConnectionError as exc:
                raise ConnectionError(str(exc)) from exc

            status = int(resp.status_code)
            location = _header_get(resp.headers, "location")
            # Never follow HTTP 308 (resumable upload protocol) or non-redirect statuses.
            if status in _REDIRECT_STATUSES and location and hop < max_redirects:
                next_uri = urljoin(current_uri, str(location))
                if not _same_origin(current_uri, next_uri):
                    current_headers = _strip_sensitive_headers(current_headers)
                if status == 303 and current_method.upper() not in ("GET", "HEAD"):
                    current_method = "GET"
                    current_body = None
                current_uri = next_uri
                continue

            return (
                RequestsHttpResponse(
                    status,
                    resp.headers,
                    getattr(resp, "reason", None),
                ),
                resp.content,
            )

        raise RuntimeError("Exceeded maximum HTTP redirects")


def _reject_service_account(creds) -> None:
    if creds is None:
        raise RuntimeError("SERVICE_ACCOUNT_CREDENTIALS_FORBIDDEN: missing credentials")
    if getattr(creds, "service_account_email", None):
        raise RuntimeError(
            "SERVICE_ACCOUNT_CREDENTIALS_FORBIDDEN: service-account credentials are not permitted; use personal user OAuth credentials"
        )
    mod = getattr(type(creds), "__module__", "")
    if "service_account" in mod:
        raise RuntimeError(
            "SERVICE_ACCOUNT_CREDENTIALS_FORBIDDEN: service-account credentials are not permitted; use personal user OAuth credentials"
        )


def build_authorized_session(
    creds,
    *,
    pool_connections: int = DEFAULT_POOL_CONNECTIONS,
    pool_maxsize: int = DEFAULT_POOL_MAXSIZE,
):
    """Build a keep-alive AuthorizedSession with bounded connection pooling."""
    from google.auth.transport.requests import AuthorizedSession
    from requests.adapters import HTTPAdapter

    _reject_service_account(creds)
    session = AuthorizedSession(creds)
    adapter = HTTPAdapter(
        pool_connections=pool_connections,
        pool_maxsize=pool_maxsize,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def build_requests_service(
    service_name: str,
    version: str,
    creds,
    *,
    pool_connections: int = DEFAULT_POOL_CONNECTIONS,
    pool_maxsize: int = DEFAULT_POOL_MAXSIZE,
    timeout: float = DEFAULT_TIMEOUT_SEC,
):
    """Build a googleapiclient service backed by a pooled RequestsHttp transport."""
    from googleapiclient.discovery import build

    session = build_authorized_session(
        creds,
        pool_connections=pool_connections,
        pool_maxsize=pool_maxsize,
    )
    return build(
        service_name,
        version,
        http=RequestsHttp(session, timeout=timeout),
        cache_discovery=False,
    )
