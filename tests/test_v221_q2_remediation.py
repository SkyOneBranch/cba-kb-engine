"""Q2 Round 1 remediation regression tests for REQ-221 (v2.2.1).

Covers independent Q2 Round 1 blockers without modifying Commit 1's frozen
acceptance test file:
- B1: Cross-origin redirects on real AuthorizedSession must NEVER reattach OAuth
  Authorization headers (including multi-hop bounce-back and HTTPS->HTTP
  downgrade), must reject non-HTTP(S) redirect schemes, and must cleanly close
  both authenticated and credential-free transports.
- B2: restore() must fail closed on ANY tampered existing local frozen artifact
  (before/*, candidate/*, status.before.json, protected/*) BEFORE falling back
  to cloud bundle_v1 or honoring an explicit bundle_file_id, with 0 Drive writes.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
from typing import Any, Dict, List

import pytest
import requests
from google.oauth2.credentials import Credentials

from cba_kb import drive_http, release as release_module
from cba_kb.common import digest


class _SpyCredentials(Credentials):
    """In-memory synthetic OAuth user credentials that count before_request()."""

    def __init__(self, marker: str = "synthetic-oauth-marker") -> None:
        super().__init__(token=marker)
        self.before_request_urls: List[str] = []

    @property
    def valid(self) -> bool:
        return True

    def before_request(self, request, method, url, headers) -> None:
        self.before_request_urls.append(url)
        headers["authorization"] = f"Bearer {self.token}"


def _make_requests_response(
    status_code: int,
    *,
    headers: Dict[str, str] | None = None,
    content: bytes = b"",
    url: str = "https://example.invalid/",
    reason: str = "OK",
) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status_code
    resp.reason = reason
    resp.url = url
    resp._content = content
    for k, v in (headers or {}).items():
        resp.headers[k] = v
    return resp


def _lower_headers(headers: Any) -> Dict[str, str]:
    return {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}


# ---------------------------------------------------------------------------
# B1: AuthorizedSession cross-origin redirect credential isolation
# ---------------------------------------------------------------------------


def test_b1_authorized_session_cross_origin_redirect_never_reattaches_oauth(monkeypatch):
    """Real AuthorizedSession must never send Authorization after crossing origin."""
    creds = _SpyCredentials()
    auth_client = drive_http.build_authorized_session(creds)
    recorded_sends: List[Dict[str, Any]] = []
    scripted = [
        _make_requests_response(
            302,
            headers={"Location": "https://other-cdn.example/blob"},
            url="https://www.googleapis.com/drive/v3/files/f-1",
            reason="Found",
        ),
        _make_requests_response(
            200,
            headers={"Content-Type": "application/octet-stream"},
            content=b"cross-origin-payload",
            url="https://other-cdn.example/blob",
            reason="OK",
        ),
    ]

    def _fake_adapter_send(self, request, **kwargs):
        recorded_sends.append({
            "adapter": self,
            "method": request.method,
            "url": request.url,
            "headers": _lower_headers(request.headers),
        })
        resp = scripted.pop(0)
        resp.request = request
        return resp

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _fake_adapter_send)

    http = drive_http.RequestsHttp(auth_client)
    try:
        resp, body = http.request(
            "https://www.googleapis.com/drive/v3/files/f-1",
            method="GET",
            headers={
                "Accept": "application/octet-stream",
                "Cookie": "[REDACTED]",
                "Proxy-Authorization": "[REDACTED]",
            },
        )
        assert resp.status == 200
        assert body == b"cross-origin-payload"
        assert len(recorded_sends) == 2

        # Hop 0: authenticated origin gets OAuth header via AuthorizedSession
        assert creds.before_request_urls == [
            "https://www.googleapis.com/drive/v3/files/f-1"
        ]
        assert recorded_sends[0]["url"] == "https://www.googleapis.com/drive/v3/files/f-1"
        assert recorded_sends[0]["headers"]["authorization"].startswith("Bearer ")

        # Hop 1: cross-origin hop uses lazy credential-free pooled Session, NEVER AuthorizedSession
        hop1_headers = recorded_sends[1]["headers"]
        assert recorded_sends[1]["url"] == "https://other-cdn.example/blob"
        assert "authorization" not in hop1_headers
        assert "proxy-authorization" not in hop1_headers
        assert "cookie" not in hop1_headers
        assert hop1_headers.get("accept") == "application/octet-stream"
        assert http._unauth_client is not None
        assert http._unauth_client is not auth_client
        unauth_adapter = http._unauth_client.get_adapter("https://other-cdn.example/")
        assert unauth_adapter._pool_connections == drive_http.DEFAULT_POOL_CONNECTIONS
        assert unauth_adapter._pool_maxsize == drive_http.DEFAULT_POOL_MAXSIZE
    finally:
        http.close()


def test_b1_multihop_bounce_back_and_https_downgrade_stay_unauthenticated(monkeypatch):
    """Once cross-origin occurs, bouncing back to origin A or downgrading stays unauthenticated."""
    creds = _SpyCredentials()
    auth_client = drive_http.build_authorized_session(creds)
    recorded_sends: List[Dict[str, Any]] = []
    scripted = [
        _make_requests_response(
            302,
            headers={"Location": "https://untrusted.example/hop"},
            reason="Found",
        ),
        _make_requests_response(
            302,
            headers={"Location": "https://www.googleapis.com/drive/v3/files/f-bounce"},
            reason="Found",
        ),
        _make_requests_response(
            200,
            headers={"Content-Type": "text/plain"},
            content=b"bounce-ok",
            reason="OK",
        ),
    ]

    def _fake_adapter_send(self, request, **kwargs):
        recorded_sends.append({
            "url": request.url,
            "headers": _lower_headers(request.headers),
        })
        resp = scripted.pop(0)
        resp.request = request
        return resp

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _fake_adapter_send)

    http = drive_http.RequestsHttp(auth_client)
    try:
        resp, body = http.request("https://www.googleapis.com/drive/v3/files/f-start")
        assert resp.status == 200
        assert body == b"bounce-ok"
        assert len(recorded_sends) == 3
        assert len(creds.before_request_urls) == 1
        assert "authorization" in recorded_sends[0]["headers"]
        assert "authorization" not in recorded_sends[1]["headers"]
        assert "authorization" not in recorded_sends[2]["headers"]
    finally:
        http.close()

    # HTTPS -> HTTP downgrade on same host is cross-origin and must drop OAuth
    creds_dg = _SpyCredentials()
    auth_dg = drive_http.build_authorized_session(creds_dg)
    recorded_sends.clear()
    scripted[:] = [
        _make_requests_response(
            302,
            headers={"Location": "http://www.googleapis.com/plain"},
            reason="Found",
        ),
        _make_requests_response(200, content=b"plain-ok", reason="OK"),
    ]
    http_dg = drive_http.RequestsHttp(auth_dg)
    try:
        resp, body = http_dg.request("https://www.googleapis.com/secure")
        assert resp.status == 200
        assert body == b"plain-ok"
        assert len(creds_dg.before_request_urls) == 1
        assert "authorization" in recorded_sends[0]["headers"]
        assert "authorization" not in recorded_sends[1]["headers"]
    finally:
        http_dg.close()


def test_b1_same_origin_keeps_auth_303_clears_body_and_unsafe_scheme_rejected(monkeypatch):
    """Same-origin preserves OAuth; 303 strips body headers; non-HTTP(S) fails closed."""
    creds = _SpyCredentials()
    auth_client = drive_http.build_authorized_session(creds)
    recorded_sends: List[Dict[str, Any]] = []
    scripted = [
        _make_requests_response(
            303,
            headers={"Location": "/drive/v3/files/next"},
            reason="See Other",
        ),
        _make_requests_response(200, content=b"same-origin-ok", reason="OK"),
    ]

    def _fake_adapter_send(self, request, **kwargs):
        recorded_sends.append({
            "method": request.method,
            "url": request.url,
            "body": request.body,
            "headers": _lower_headers(request.headers),
        })
        resp = scripted.pop(0)
        resp.request = request
        return resp

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _fake_adapter_send)

    http = drive_http.RequestsHttp(auth_client)
    try:
        resp, body = http.request(
            "https://www.googleapis.com/drive/v3/files/start",
            method="POST",
            body=b"payload-bytes",
            headers={"Content-Type": "application/json", "Content-Length": "13"},
        )
        assert resp.status == 200
        assert body == b"same-origin-ok"
        assert len(creds.before_request_urls) == 2
        assert "authorization" in recorded_sends[1]["headers"]
        assert recorded_sends[1]["method"] == "GET"
        assert recorded_sends[1]["body"] is None
        assert "content-type" not in recorded_sends[1]["headers"]
    finally:
        http.close()

    # Non-HTTP(S) redirect scheme must fail closed
    creds_bad = _SpyCredentials()
    auth_bad = drive_http.build_authorized_session(creds_bad)
    scripted[:] = [
        _make_requests_response(
            302,
            headers={"Location": "file:///etc/hosts"},
            reason="Found",
        ),
    ]
    http_bad = drive_http.RequestsHttp(auth_bad)
    try:
        with pytest.raises(ValueError, match="UNSAFE_REDIRECT_URL"):
            http_bad.request("https://www.googleapis.com/drive/v3/files/bad")
    finally:
        http_bad.close()


def test_b1a_http_303_discards_body_and_headers_for_get_and_head_with_real_authorized_session(monkeypatch):
    """Every HTTP 303 (including GET/HEAD with body) must discard body and body headers."""
    for orig_method, location, cross_origin in (
        ("GET", "/drive/v3/files/redirected-get", False),
        ("GET", "https://other-cdn.example/redirected-get", True),
        ("HEAD", "/drive/v3/files/redirected-head", False),
        ("HEAD", "https://other-cdn.example/redirected-head", True),
    ):
        creds = _SpyCredentials()
        auth_client = drive_http.build_authorized_session(creds)
        recorded_sends: List[Dict[str, Any]] = []
        scripted = [
            _make_requests_response(
                303,
                headers={"Location": location},
                reason="See Other",
            ),
            _make_requests_response(200, content=b"" if orig_method == "HEAD" else b"ok", reason="OK"),
        ]

        def _fake_adapter_send(self, request, **kwargs):
            recorded_sends.append({
                "method": request.method,
                "url": request.url,
                "body": request.body,
                "headers": _lower_headers(request.headers),
            })
            resp = scripted.pop(0)
            resp.request = request
            return resp

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _fake_adapter_send)

        http = drive_http.RequestsHttp(auth_client)
        try:
            resp, _ = http.request(
                "https://www.googleapis.com/drive/v3/files/start",
                method=orig_method,
                body=b"stale-body-payload",
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": "18",
                    "Content-Encoding": "identity",
                    "Accept": "*/*",
                },
            )
            assert resp.status == 200
            assert len(recorded_sends) == 2

            hop1 = recorded_sends[1]
            assert hop1["method"] == orig_method
            assert hop1["body"] is None
            assert "content-type" not in hop1["headers"]
            assert "content-length" not in hop1["headers"]
            assert "content-encoding" not in hop1["headers"]
            assert hop1["headers"].get("accept") == "*/*"

            if cross_origin:
                assert len(creds.before_request_urls) == 1
                assert "authorization" not in hop1["headers"]
            else:
                assert len(creds.before_request_urls) == 2
                assert "authorization" in hop1["headers"]
        finally:
            http.close()


def test_b1b_unauthenticated_client_injection_removed_and_tampered_slot_fails_closed(monkeypatch):
    """RequestsHttp must not allow injecting an authenticated client for crossed-origin hops."""
    creds = _SpyCredentials()
    auth_client = drive_http.build_authorized_session(creds)
    try:
        # 1. Public constructor rejects unauthenticated_client injection outright
        with pytest.raises(TypeError):
            drive_http.RequestsHttp(auth_client, unauthenticated_client=auth_client)

        # 2. Defense-in-depth: even if internal slot is tampered to AuthorizedSession or
        #    credentialed Session, crossing origins fails closed with 0 extra before_request calls
        for case_kind in ("same_auth", "header_auth"):
            creds_case = _SpyCredentials()
            auth_case = drive_http.build_authorized_session(creds_case)
            scripted = [
                _make_requests_response(
                    302,
                    headers={"Location": "https://other-origin.example/secret"},
                    reason="Found",
                ),
                _make_requests_response(200, content=b"should-never-send", reason="OK"),
            ]
            sends: List[str] = []

            def _fake_adapter_send(self, request, **kwargs):
                sends.append(request.url)
                resp = scripted.pop(0)
                resp.request = request
                return resp

            monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _fake_adapter_send)

            http = drive_http.RequestsHttp(auth_case)
            bad_client = auth_case if case_kind == "same_auth" else _make_credentialed_plain_session()
            http._unauth_client = bad_client
            try:
                with pytest.raises(RuntimeError, match="UNAUTHENTICATED_CLIENT_NOT_ISOLATED"):
                    http.request("https://www.googleapis.com/drive/v3/files/start")
                assert len(creds_case.before_request_urls) == 1
                assert sends == ["https://www.googleapis.com/drive/v3/files/start"]
            finally:
                http._unauth_client = None
                if bad_client is not auth_case:
                    bad_client.close()
                http.close()

        # 3. Embedded userinfo credentials in redirect URL fail closed
        creds_ui = _SpyCredentials()
        auth_ui = drive_http.build_authorized_session(creds_ui)
        unsafe_loc = "https://" + "u" + ":" + "p" + "@other.example/file"
        scripted_ui = [
            _make_requests_response(302, headers={"Location": unsafe_loc}, reason="Found"),
        ]

        def _fake_send_ui(self, request, **kwargs):
            resp = scripted_ui.pop(0)
            resp.request = request
            return resp

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _fake_send_ui)
        http_ui = drive_http.RequestsHttp(auth_ui)
        try:
            with pytest.raises(ValueError, match="UNSAFE_REDIRECT_URL"):
                http_ui.request("https://www.googleapis.com/drive/v3/files/ui")
        finally:
            http_ui.close()
    finally:
        auth_client.close()


class _PlainSessionSubclass(requests.Session):
    """Distinct non-exact requests.Session subclass for B1-B isolation checks."""


def _make_isolated_bad_client(case_kind: str, auth_case) -> requests.Session:
    if case_kind == "same_auth":
        return auth_case
    if case_kind == "session_subclass":
        client = _PlainSessionSubclass()
        client.auth = None
        client.trust_env = False
        return client
    client = requests.Session()
    client.auth = None
    client.trust_env = False
    if case_kind == "credentials_attr":
        client.credentials = object()
    elif case_kind == "non_none_auth":
        client.auth = ("synthetic-user", "synthetic-val")
    elif case_kind == "header_auth":
        client.headers["Authorization"] = "Bearer [REDACTED]"
    elif case_kind == "trust_env_true":
        client.trust_env = True
    else:
        raise AssertionError(f"Unknown case_kind: {case_kind}")
    return client


@pytest.mark.parametrize(
    "case_kind",
    [
        "same_auth",
        "session_subclass",
        "credentials_attr",
        "non_none_auth",
        "header_auth",
        "trust_env_true",
    ],
)
def test_b1b_all_isolation_rejection_conditions_fail_closed_with_netrc(
    monkeypatch, tmp_path, case_kind
):
    """Every isolation violation (incl. trust_env=True with active NETRC) fails closed with 0 crossed sends."""
    netrc_file = tmp_path / "synthetic.netrc"
    netrc_lines = [
        "machine other-origin.example",
        "login synthetic-netrc-user",
        "pass" + "word synthetic-netrc-val",
    ]
    netrc_file.write_text("\n".join(netrc_lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("NETRC", str(netrc_file))

    creds_case = _SpyCredentials()
    auth_case = drive_http.build_authorized_session(creds_case)
    scripted = [
        _make_requests_response(
            302,
            headers={"Location": "https://other-origin.example/secret"},
            reason="Found",
        ),
        _make_requests_response(200, content=b"should-never-send", reason="OK"),
    ]
    sends: List[Dict[str, Any]] = []

    def _fake_adapter_send(self, request, **kwargs):
        sends.append({
            "url": request.url,
            "headers": _lower_headers(request.headers),
        })
        resp = scripted.pop(0)
        resp.request = request
        return resp

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _fake_adapter_send)

    http = drive_http.RequestsHttp(auth_case)
    bad_client = _make_isolated_bad_client(case_kind, auth_case)
    http._unauth_client = bad_client
    try:
        with pytest.raises(RuntimeError, match="UNAUTHENTICATED_CLIENT_NOT_ISOLATED"):
            http.request("https://www.googleapis.com/drive/v3/files/start")
        assert len(creds_case.before_request_urls) == 1
        assert [s["url"] for s in sends] == ["https://www.googleapis.com/drive/v3/files/start"]
    finally:
        http._unauth_client = None
        if bad_client is not auth_case:
            bad_client.close()
        http.close()


def test_b1b_default_crossed_origin_ignores_netrc_and_sends_zero_credentials(
    monkeypatch, tmp_path
):
    """Default internal crossed-origin session sets trust_env=False and never attaches NETRC Basic auth."""
    netrc_file = tmp_path / "synthetic_default.netrc"
    netrc_lines = [
        "machine other-origin.example",
        "login synthetic-netrc-user",
        "pass" + "word synthetic-netrc-val",
    ]
    netrc_file.write_text("\n".join(netrc_lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("NETRC", str(netrc_file))

    creds = _SpyCredentials()
    auth_client = drive_http.build_authorized_session(creds)
    sends: List[Dict[str, Any]] = []
    scripted = [
        _make_requests_response(
            302,
            headers={"Location": "https://other-origin.example/public-blob"},
            reason="Found",
        ),
        _make_requests_response(200, content=b"clean-blob", reason="OK"),
    ]

    def _fake_adapter_send(self, request, **kwargs):
        sends.append({
            "url": request.url,
            "headers": _lower_headers(request.headers),
        })
        resp = scripted.pop(0)
        resp.request = request
        return resp

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _fake_adapter_send)

    http = drive_http.RequestsHttp(auth_client)
    try:
        resp, body = http.request("https://www.googleapis.com/drive/v3/files/start")
        assert resp.status == 200
        assert body == b"clean-blob"
        assert len(creds.before_request_urls) == 1
        assert len(sends) == 2
        assert "authorization" in sends[0]["headers"]
        assert "authorization" not in sends[1]["headers"]
        assert "proxy-authorization" not in sends[1]["headers"]
        assert "cookie" not in sends[1]["headers"]
        assert http._unauth_client is not None
        assert http._unauth_client.trust_env is False
    finally:
        http.close()


def _make_credentialed_plain_session() -> requests.Session:
    client = requests.Session()
    client.headers["Authorization"] = "Bearer [REDACTED]"
    return client


# ---------------------------------------------------------------------------
# B2: restore() must verify ALL existing local frozen files before bundle fallback
# ---------------------------------------------------------------------------


class _MiniBundleDrive:
    """Minimal deterministic in-memory Drive double with write counter for B2 tests."""

    def __init__(self) -> None:
        self.files: Dict[str, Dict[str, Any]] = {}
        self.write_count = 0

    def add_file(
        self,
        fid: str,
        name: str,
        content: bytes,
        *,
        mime: str = "text/plain",
        parent: str = "root",
        cba_key: str | None = None,
    ) -> str:
        rec = {
            "id": fid,
            "name": name,
            "mimeType": mime,
            "parents": [parent],
            "version": 1,
            "modifiedTime": "2026-11-22T00:00:00Z",
            "content": content,
            "appProperties": {"cba_key": cba_key} if cba_key else {},
        }
        self.files[fid] = rec
        return fid

    def add_folder(self, fid: str, name: str, parent: str = "root", cba_key: str | None = None) -> str:
        return self.add_file(
            fid, name, b"", mime=release_module.FOLDER, parent=parent, cba_key=cba_key
        )

    def meta(self, fid: str) -> Dict[str, Any]:
        rec = self.files[fid]
        out = {
            "id": rec["id"],
            "name": rec["name"],
            "mimeType": rec["mimeType"],
            "parents": list(rec["parents"]),
            "version": str(rec["version"]),
            "modifiedTime": rec["modifiedTime"],
            "appProperties": dict(rec.get("appProperties") or {}),
        }
        if rec["mimeType"] != release_module.FOLDER:
            out["sha256Checksum"] = digest(rec["content"])
            out["size"] = str(len(rec["content"]))
        return out

    def get(self, fid: str) -> bytes:
        return self.files[fid]["content"]

    def get_with_meta(self, fid: str, *, known_meta=None):
        return self.files[fid]["content"], self.meta(fid)

    def get_bounded(self, fid: str, max_bytes: int) -> bytes:
        data = self.files[fid]["content"]
        if len(data) > max_bytes:
            raise release_module.ReleaseContractError("BUNDLE_EXTRACT_LIMIT_EXCEEDED")
        return data

    def put(self, fid: str, data: bytes, mime: str) -> None:
        self.write_count += 1
        rec = self.files[fid]
        rec["content"] = data
        rec["mimeType"] = mime
        rec["version"] += 1

    def list(self, parent: str) -> List[Dict[str, Any]]:
        return [
            self.meta(fid)
            for fid, rec in self.files.items()
            if parent in rec.get("parents", [])
        ]

    def index_cba_keys(self, parent: str) -> Dict[str, Dict[str, Any]]:
        out = {}
        for item in self.list(parent):
            key = (item.get("appProperties") or {}).get("cba_key")
            if key is not None:
                out[key] = item
        return out

    def ensure(self, parent: str, key: str, name: str, mime: str, content: bytes | None = None, *, index=None) -> str:
        idx = self.index_cba_keys(parent) if index is None else index
        if key in idx:
            return idx[key]["id"]
        self.write_count += 1
        fid = f"created_{len(self.files) + 1}"
        self.add_file(fid, name, content or b"", mime=mime, parent=parent, cba_key=key)
        idx[key] = self.meta(fid)
        return fid

    def move(self, fid: str, dest: str, src: str) -> None:
        self.write_count += 1
        rec = self.files[fid]
        rec["parents"] = [p for p in rec["parents"] if p != src] + [dest]


def _setup_published_bundle_release(tmp_path: Path):
    drv = _MiniBundleDrive()
    drv.add_folder("root", "root")
    drv.add_folder("archive", "releases", parent="root")
    drv.add_file("f-a", "alpha.txt", b"alpha-v1\n", parent="root")
    drv.add_file("f-b", "beta.txt", b"beta-v1\n", parent="root")
    status_v1 = {
        "state": "COMPLETE",
        "current_release_id": "v2.2.0-1",
        "artifacts": [
            {"id": "f-a", "name": "alpha.txt", "sha256": digest(b"alpha-v1\n")},
            {"id": "f-b", "name": "beta.txt", "sha256": digest(b"beta-v1\n")},
        ],
    }
    drv.add_file(
        "status",
        "release_status.json",
        json.dumps(status_v1, sort_keys=True).encode("utf-8"),
        mime="application/json",
        parent="root",
    )

    cand_dir = tmp_path / "candidates"
    cand_dir.mkdir(parents=True, exist_ok=True)
    alpha_cand = cand_dir / "alpha.txt"
    beta_cand = cand_dir / "beta.txt"
    alpha_cand.write_bytes(b"alpha-v2\n")
    beta_cand.write_bytes(b"beta-v2\n")

    outbox = tmp_path / "outbox"
    release_module.prepare(
        drv,
        outbox,
        release_id="v2.2.1-1",
        status_id="status",
        archive_id="archive",
        entries=[
            {"id": "f-a", "name": "alpha.txt", "mime": "text/plain", "path": str(alpha_cand)},
            {"id": "f-b", "name": "beta.txt", "mime": "text/plain", "path": str(beta_cand)},
        ],
        archive_format=release_module.ARCHIVE_FORMAT_BUNDLE_V1,
    )
    journal = release_module.publish(drv, outbox, single_writer=True)
    assert journal["state"] == "COMPLETE"
    bundle_fid = journal["previous_snapshot"][0]["bundle_file_id"]
    assert drv.get("f-a") == b"alpha-v2\n"
    assert drv.get("f-b") == b"beta-v2\n"
    return drv, outbox, bundle_fid


def _clone_case(drv: _MiniBundleDrive, outbox: Path, dst: Path):
    shutil.copytree(outbox, dst)
    cloned_drv = copy.deepcopy(drv)
    writes_before = cloned_drv.write_count
    return cloned_drv, dst, writes_before


def test_b2_restore_rejects_tampered_local_files_on_partial_deletion_and_explicit_bundle_id(tmp_path):
    """Partial local deletion or explicit bundle_file_id must never bypass local integrity checks."""
    base_drv, base_outbox, bundle_fid = _setup_published_bundle_release(tmp_path)

    # Case 1: Delete before/0 and tamper 1 byte of before/1 -> MUST fail closed, 0 Drive writes
    drv1, ob1, w1 = _clone_case(base_drv, base_outbox, tmp_path / "case1_tamper_before1")
    (ob1 / "before" / "0").unlink()
    (ob1 / "before" / "1").write_bytes(b"beta-v1-TAMPERED\n")
    with pytest.raises(RuntimeError, match="Frozen local artifact was altered"):
        release_module.restore(drv1, ob1, single_writer=True)
    assert drv1.write_count == w1

    # Case 2: Delete before/0 and tamper 1 byte of status.before.json -> MUST fail closed, 0 Drive writes
    drv2, ob2, w2 = _clone_case(base_drv, base_outbox, tmp_path / "case2_tamper_status_before")
    (ob2 / "before" / "0").unlink()
    (ob2 / "status.before.json").write_bytes((ob2 / "status.before.json").read_bytes() + b" ")
    with pytest.raises(RuntimeError, match="Frozen previous status was altered"):
        release_module.restore(drv2, ob2, single_writer=True)
    assert drv2.write_count == w2

    # Case 3: Delete status.before.json and tamper 1 byte of candidate/0 -> MUST fail closed, 0 Drive writes
    drv3, ob3, w3 = _clone_case(base_drv, base_outbox, tmp_path / "case3_tamper_candidate0")
    (ob3 / "status.before.json").unlink()
    (ob3 / "candidate" / "0").write_bytes(b"alpha-v2-TAMPERED\n")
    with pytest.raises(RuntimeError, match="Frozen local artifact was altered"):
        release_module.restore(drv3, ob3, single_writer=True)
    assert drv3.write_count == w3

    # Case 4: Explicit bundle_file_id with tampered local before/0 -> MUST fail closed, 0 Drive writes
    drv4, ob4, w4 = _clone_case(base_drv, base_outbox, tmp_path / "case4_explicit_bundle_tamper_before")
    (ob4 / "before" / "0").write_bytes(b"alpha-v1-TAMPERED\n")
    with pytest.raises(RuntimeError, match="Frozen local artifact was altered"):
        release_module.restore(drv4, ob4, single_writer=True, bundle_file_id=bundle_fid)
    assert drv4.write_count == w4

    # Case 5: Explicit bundle_file_id with tampered local status.before.json -> MUST fail closed, 0 Drive writes
    drv5, ob5, w5 = _clone_case(base_drv, base_outbox, tmp_path / "case5_explicit_bundle_tamper_status")
    (ob5 / "status.before.json").write_bytes((ob5 / "status.before.json").read_bytes() + b"\n")
    with pytest.raises(RuntimeError, match="Frozen previous status was altered"):
        release_module.restore(drv5, ob5, single_writer=True, bundle_file_id=bundle_fid)
    assert drv5.write_count == w5

    # Case 6: Explicit bundle_file_id pointing to tampered cloud bundle -> MUST fail closed, 0 Drive writes
    drv6, ob6, w6 = _clone_case(base_drv, base_outbox, tmp_path / "case6_tampered_cloud_bundle")
    shutil.rmtree(ob6 / "before")
    drv6.files[bundle_fid]["content"] = drv6.files[bundle_fid]["content"] + b"X"
    with pytest.raises(release_module.ReleaseContractError, match="BUNDLE_ARCHIVE_HASH_MISMATCH"):
        release_module.restore(drv6, ob6, single_writer=True, bundle_file_id=bundle_fid)
    assert drv6.write_count == w6

    # Case 7: Tampered local protected/* snapshot fails closed BEFORE any Drive write
    drv_prot, ob_prot, w_prot = _clone_case(base_drv, base_outbox, tmp_path / "case_prot_tamper")
    (ob_prot / "protected").mkdir(exist_ok=True)
    prot_bytes = b"immutable-protected-v1\n"
    (ob_prot / "protected" / "0").write_bytes(b"immutable-protected-TAMPERED\n")
    plan_prot = json.loads((ob_prot / "plan.json").read_text(encoding="utf-8"))
    plan_prot["closure"] = {
        "protected": [
            {"id": "f-prot", "name": "protected.csv", "sha256": digest(prot_bytes), "before": "protected/0"}
        ]
    }
    (ob_prot / "plan.json").write_text(json.dumps(plan_prot, indent=2, sort_keys=True), encoding="utf-8")
    shutil.rmtree(ob_prot / "before")
    with pytest.raises(RuntimeError, match="Frozen protected snapshot was altered"):
        release_module.restore(drv_prot, ob_prot, single_writer=True)
    assert drv_prot.write_count == w_prot

    # Case 8: Genuine disaster recovery (missing before/ + status.before.json, untampered remaining files) succeeds!
    drv7, ob7, _ = _clone_case(base_drv, base_outbox, tmp_path / "case7_clean_disaster_recovery")
    shutil.rmtree(ob7 / "before")
    (ob7 / "status.before.json").unlink()
    journal7 = release_module.restore(drv7, ob7, single_writer=True, bundle_file_id=bundle_fid)
    assert journal7["state"] == "ROLLED_BACK"
    assert drv7.get("f-a") == b"alpha-v1\n"
    assert drv7.get("f-b") == b"beta-v1\n"
    restored_status = json.loads(drv7.get("status"))
    assert restored_status["state"] == "ROLLED_BACK"
    assert restored_status["current_release_id"] == "v2.2.0-1"
    assert restored_status["rolled_back_release_id"] == "v2.2.1-1"
