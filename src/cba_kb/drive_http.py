"""Keep-alive Drive/Docs HTTP transport adapter over requests (REQ-221 P0-1).

Replaces stock httplib2 transport with an httplib2-compatible adapter backed by
``google.auth.transport.requests.AuthorizedSession`` and ``requests`` connection
pooling (``pool_connections=16, pool_maxsize=16``), using zero new third-party
dependencies.

Security & correctness invariants:
- Rejects service-account credentials fail-closed (personal Drive OAuth only).
- Never follows HTTP 308 resumable-upload responses (even if Location is set).
- Follows bounded 301/302/303/307 redirects over HTTP(S) only; once a redirect
  leaves the original origin, strips sensitive headers (Authorization,
  Proxy-Authorization, Cookie) and switches permanently for that request chain
  to a credential-free requests.Session so AuthorizedSession never reattaches
  OAuth credentials on cross-origin hops.
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
_ALLOWED_REDIRECT_SCHEMES = frozenset({"http", "https"})
_SENSITIVE_REDIRECT_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})
_BODY_HEADERS = frozenset({"content-length", "content-type", "content-encoding", "transfer-encoding"})


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
    scheme_a = a.scheme.lower()
    scheme_b = b.scheme.lower()
    if scheme_a not in _ALLOWED_REDIRECT_SCHEMES or scheme_b not in _ALLOWED_REDIRECT_SCHEMES:
        return False
    if not a.hostname or not b.hostname:
        return False
    port_a = a.port or (443 if scheme_a == "https" else 80)
    port_b = b.port or (443 if scheme_b == "https" else 80)
    return (
        scheme_a == scheme_b
        and a.hostname.lower() == b.hostname.lower()
        and port_a == port_b
    )


def _validate_redirect_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme.lower() not in _ALLOWED_REDIRECT_SCHEMES
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(
            "UNSAFE_REDIRECT_URL: redirect target must use http or https with a valid host and no embedded credentials"
        )


def _strip_headers(headers: dict, names: frozenset[str]) -> dict:
    return {
        k: v for k, v in headers.items()
        if str(k).lower() not in names
    }


def _strip_sensitive_headers(headers: dict) -> dict:
    return _strip_headers(headers, _SENSITIVE_REDIRECT_HEADERS)


def _needs_credential_free_transport(client) -> bool:
    import requests

    return (
        isinstance(client, requests.Session)
        or getattr(client, "credentials", None) is not None
        or getattr(client, "auth", None) is not None
    )


def _assert_credential_free_session(client, auth_client) -> None:
    import requests

    if (
        client is auth_client
        or type(client) is not requests.Session
        or getattr(client, "credentials", None) is not None
        or getattr(client, "auth", None) is not None
        or any(
            str(k).lower() in _SENSITIVE_REDIRECT_HEADERS
            for k in dict(getattr(client, "headers", {}) or {})
        )
    ):
        raise RuntimeError(
            "UNAUTHENTICATED_CLIENT_NOT_ISOLATED: crossed-origin redirects require an independent credential-free requests.Session"
        )


def _header_get(headers, name: str):
    target = name.lower()
    for k, v in dict(headers or {}).items():
        if str(k).lower() == target:
            return v
    return None


class RequestsHttp:
    """httplib2-compatible ``http`` object backed by a pooled requests session."""

    def __init__(
        self,
        session,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        *,
        pool_connections: int = DEFAULT_POOL_CONNECTIONS,
        pool_maxsize: int = DEFAULT_POOL_MAXSIZE,
    ) -> None:
        self._http_client = session
        self._unauth_client = None
        self._pool_connections = pool_connections
        self._pool_maxsize = pool_maxsize
        self.timeout = timeout

    @property
    def session(self):
        return self._http_client

    def _get_unauthenticated_client(self):
        if self._unauth_client is None:
            import requests
            from requests.adapters import HTTPAdapter

            client = requests.Session()
            client.auth = None
            client.trust_env = False
            adapter = HTTPAdapter(
                pool_connections=self._pool_connections,
                pool_maxsize=self._pool_maxsize,
            )
            client.mount("https://", adapter)
            client.mount("http://", adapter)
            self._unauth_client = client
        _assert_credential_free_session(self._unauth_client, self._http_client)
        return self._unauth_client

    def close(self) -> None:
        for client in (self._http_client, self._unauth_client):
            if client is None:
                continue
            close_fn = getattr(client, "close", None)
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
        crossed_origin = False

        for hop in range(max_redirects + 1):
            use_unauth = crossed_origin and _needs_credential_free_transport(self._http_client)
            if crossed_origin:
                current_headers = _strip_sensitive_headers(current_headers)
            active_client = (
                self._get_unauthenticated_client() if use_unauth else self._http_client
            )
            req_kwargs = {
                "data": current_body,
                "headers": current_headers,
                "allow_redirects": False,
                "timeout": self.timeout,
            }
            if use_unauth and isinstance(active_client, requests.Session):
                active_client.cookies.clear()
                req_kwargs["auth"] = None

            try:
                resp = active_client.request(
                    current_method,
                    current_uri,
                    **req_kwargs,
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
                _validate_redirect_url(next_uri)
                if not _same_origin(current_uri, next_uri) or not _same_origin(uri, next_uri):
                    crossed_origin = True
                    current_headers = _strip_sensitive_headers(current_headers)
                if status == 303:
                    if current_method.upper() not in ("GET", "HEAD"):
                        current_method = "GET"
                    current_body = None
                    current_headers = _strip_headers(current_headers, _BODY_HEADERS)
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
        http=RequestsHttp(
            session,
            timeout=timeout,
            pool_connections=pool_connections,
            pool_maxsize=pool_maxsize,
        ),
        cache_discovery=False,
    )
