"""Frozen red acceptance tests for REQ-221 (v2.2.1):
Deterministic O(Δ) Incremental Drive Release, Server-Hash Verification & Single-Bundle Archive.

Written BEFORE Commit 2 implementation for differential falsification on origin/main:
- P0-1: RequestsHttp keep-alive transport adapter, 308 passthrough, cross-host auth strip,
        transient exception mapping, SA credential rejection, 8-worker bounded read-only
        concurrency with threading.local isolation, lock-protected READ_RETRY_STATS,
        and main-thread single-writer enforcement.
- P0-2: Server-native sha256Checksum in drive.FIELDS, batch folder verification with
        fail-closed fallback for Native Docs / missing checksums, and elimination of
        redundant multi-round downloads in verify_protected() / validate_closure().
- P0-3: True O(Δ) incremental release planning in prepare_v215 (v2.2.1-1 spec & flag)
        with closure / topology / manifest / status four-contract decoupling.
- P0-4: Byte-deterministic single-bundle archive (bundle_v1), PlacementEnforcedDrive
        audit, Zip-Slip / size-cap / hash-mismatch extraction guards, and dual-path
        local + cloud-bundle restore().
- P0-5: Sanitized API call counters & 550-file scale end-to-end benchmark proving
        O(Δ) entries, <= 8 archive objects, >= 85% download call reduction, 1-byte
        remote tamper interception on carry_forward and protected assets, and 100%
        byte-exact rollback.
"""
from __future__ import annotations

import copy
import csv
import gzip
import hashlib
import importlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import socket
import ssl
import tarfile
import threading
import time
import tomllib
from types import SimpleNamespace
from typing import Any, Dict, List

from openpyxl import Workbook
import pytest
import requests
import yaml

from cba_kb import drive as drive_module
from cba_kb import release as release_module
from cba_kb import transport as transport_module
from cba_kb.common import digest, read
from cba_kb.native import wrap
from cba_kb.orchestration.release import prepare_v215 as orch
from cba_kb.placement_facade import PlacementEnforcedDrive
from tests.test_release_orchestration import canonical_registry

ROOT = Path(__file__).resolve().parents[1]
V221_1_BASELINE_SHA = "e58a2787a6176397a142abced682eed5d10f1289"
FROZEN_PREPARE_PRODUCTION_SHA256 = (
    "0ccb79429fb4ec5e270a3154af987475ae75bd4ef81de48c1e665a3b93cf4573"
)


def _require_drive_http():
    spec = importlib.util.find_spec("cba_kb.drive_http")
    assert spec is not None, "P0-1 cba_kb.drive_http module not implemented"
    return importlib.import_module("cba_kb.drive_http")


def _make_master_xlsx(row_count: int = 3) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "MASTER"
    ws.append(["record_key", "season", "club_id", "player"])
    for idx in range(row_count):
        ws.append([f"2025-2026|sz|{idx}", "2025-2026", "sz", f"synth_{idx}"])
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


class CountingFakeDrive:
    """Thread-safe synthetic Drive supporting sha256Checksum, Native Docs & call telemetry."""

    index_cba_keys = drive_module.Drive.index_cba_keys

    def __init__(self, *, include_sha256: bool = True):
        self._lock = threading.Lock()
        self.include_sha256 = include_sha256
        self.files: Dict[str, Dict[str, Any]] = {}
        self.counts: Dict[str, int] = {
            "list": 0,
            "meta": 0,
            "download": 0,
            "upload": 0,
            "copy": 0,
            "move": 0,
        }
        self.docs = SimpleNamespace(
            document=self._document_json,
        )

    def __deepcopy__(self, memo):
        clone = CountingFakeDrive(include_sha256=self.include_sha256)
        memo[id(self)] = clone
        with self._lock:
            clone.files = copy.deepcopy(self.files, memo)
            clone.counts = dict(self.counts)
        return clone

    def _document_json(self, file_id: str) -> Dict[str, Any]:
        with self._lock:
            self.counts["download"] += 1
            text = self.files[file_id]["data"].decode("utf-8", errors="replace")
            return {"body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": text}}]}}]}}

    def document_json(self, file_id: str) -> Dict[str, Any]:
        return self._document_json(file_id)

    def add(
        self,
        fid: str,
        data: bytes,
        *,
        name: str | None = None,
        mime: str = "text/plain",
        parent: str = "data",
        include_sha256: bool | None = None,
    ) -> None:
        with self._lock:
            self.files[fid] = {
                "id": fid,
                "name": name or fid,
                "mimeType": mime,
                "parents": [parent],
                "version": "1",
                "modifiedTime": "2026-11-22T00:00:00Z",
                "data": bytes(data),
                "include_sha256": self.include_sha256 if include_sha256 is None else include_sha256,
            }

    def _meta_unlocked(self, fid: str) -> Dict[str, Any]:
        rec = self.files[fid]
        meta = {
            "id": rec["id"],
            "name": rec["name"],
            "mimeType": rec["mimeType"],
            "parents": list(rec["parents"]),
            "version": rec["version"],
            "modifiedTime": rec["modifiedTime"],
            "size": str(len(rec["data"])),
        }
        if "appProperties" in rec:
            meta["appProperties"] = dict(rec["appProperties"])
        is_native = rec["mimeType"].startswith("application/vnd.google-apps.")
        if rec.get("include_sha256", True) and not is_native:
            meta["sha256Checksum"] = digest(rec["data"])
        return meta

    def meta(self, fid: str) -> Dict[str, Any]:
        with self._lock:
            self.counts["meta"] += 1
            return self._meta_unlocked(fid)

    def list(self, parent: str) -> List[Dict[str, Any]]:
        with self._lock:
            self.counts["list"] += 1
            return [
                self._meta_unlocked(fid)
                for fid, rec in self.files.items()
                if parent in rec["parents"]
            ]

    def get(self, fid: str) -> bytes:
        with self._lock:
            rec = self.files[fid]
            if rec["mimeType"].startswith("application/vnd.google-apps."):
                raise ValueError("Native object requires native adapter; raw download rejected")
            self.counts["download"] += 1
            return bytes(rec["data"])

    def get_bounded(self, fid: str, max_bytes: int) -> bytes:
        data = self.get(fid)
        if len(data) > max_bytes:
            raise transport_module.DownloadTooLargeError(
                f"download returned {len(data)} bytes over max_bytes={max_bytes}"
            )
        return data

    def get_with_meta(self, fid: str, *, known_meta: Dict[str, Any] | None = None):
        meta = known_meta if known_meta is not None else self.meta(fid)
        if meta["mimeType"].startswith("application/vnd.google-apps."):
            raise ValueError("Native object requires native adapter; raw download rejected")
        with self._lock:
            self.counts["download"] += 1
            data = bytes(self.files[fid]["data"])
            fresh = self._meta_unlocked(fid)
        expected_sha = meta.get("sha256Checksum")
        if expected_sha and digest(data) != expected_sha.lower():
            raise RuntimeError("Remote changed during download")
        return data, fresh

    def get_managed_doc(self, fid: str) -> bytes:
        with self._lock:
            rec = self.files[fid]
            if rec["mimeType"] != orch.NATIVE_DOCUMENT:
                raise ValueError("Not a native Doc")
            self.counts["download"] += 1
            return bytes(rec["data"])

    def put(self, fid: str, data: bytes, mime: str) -> Dict[str, Any]:
        with self._lock:
            rec = self.files[fid]
            if rec["mimeType"] != mime or rec["mimeType"].startswith("application/vnd.google-apps."):
                raise ValueError("MIME mismatch/native byte update rejected")
            self.counts["upload"] += 1
            rec["data"] = bytes(data)
            rec["version"] = str(int(rec["version"]) + 1)
            return self._meta_unlocked(fid)

    def put_managed_doc(self, fid: str, data: bytes) -> Dict[str, Any]:
        with self._lock:
            rec = self.files[fid]
            if rec["mimeType"] != orch.NATIVE_DOCUMENT:
                raise ValueError("Not a native Doc")
            self.counts["upload"] += 1
            rec["data"] = bytes(data)
            rec["version"] = str(int(rec["version"]) + 1)
            return self._meta_unlocked(fid)

    def move(self, fid: str, destination: str, previous: str) -> Dict[str, Any]:
        with self._lock:
            rec = self.files[fid]
            self.counts["move"] += 1
            rec["parents"] = [destination]
            rec["version"] = str(int(rec["version"]) + 1)
            return self._meta_unlocked(fid)

    def ensure(self, parent: str, key: str, name: str, mime: str, content: bytes | None = None, *, index=None) -> str:
        with self._lock:
            fid = f"{parent}__{key}"
            if fid not in self.files:
                self.counts["upload"] += 1
                self.files[fid] = {
                    "id": fid,
                    "name": name,
                    "mimeType": mime,
                    "parents": [parent],
                    "version": "1",
                    "modifiedTime": "2026-11-22T00:00:00Z",
                    "data": bytes(content or b""),
                    "appProperties": {"cba_key": key},
                    "include_sha256": self.include_sha256,
                }
            elif content is not None:
                if digest(self.files[fid]["data"]) != digest(content):
                    raise RuntimeError("Immutable release object differs")
            meta = self._meta_unlocked(fid)
            if index is not None:
                index[key] = meta
            return fid

    def ensure_copy(self, parent: str, key: str, file_id: str, name: str, *, index=None) -> str:
        with self._lock:
            fid = f"{parent}__{key}"
            src = self.files[file_id]
            if fid not in self.files:
                self.counts["copy"] += 1
                self.files[fid] = {
                    "id": fid,
                    "name": name,
                    "mimeType": src["mimeType"],
                    "parents": [parent],
                    "version": "1",
                    "modifiedTime": "2026-11-22T00:00:00Z",
                    "data": bytes(src["data"]),
                    "appProperties": {"cba_key": key},
                    "include_sha256": src.get("include_sha256", True),
                }
            meta = self._meta_unlocked(fid)
            if index is not None:
                index[key] = meta
            return fid

    def tamper_remote(self, fid: str, extra: bytes = b"!") -> None:
        """Simulate silent 1-byte external mutation on Drive."""
        with self._lock:
            rec = self.files[fid]
            rec["data"] = rec["data"] + extra
            rec["version"] = str(int(rec["version"]) + 1)

    def reset_counts(self) -> None:
        with self._lock:
            for k in self.counts:
                self.counts[k] = 0


# ---------------------------------------------------------------------------
# P0-1: RequestsHttp keep-alive adapter, 308, redirects, exceptions, SA guard
# ---------------------------------------------------------------------------


def test_p0_1_requests_http_adapter_pooling_308_redirects_exceptions_and_sa_rejection():
    drive_http = _require_drive_http()
    assert drive_http.DRIVE_TRANSPORT == "requests"
    assert drive_http.DEFAULT_POOL_CONNECTIONS == 16
    assert drive_http.DEFAULT_POOL_MAXSIZE == 16

    # 1. RequestsHttpResponse contract
    resp = drive_http.RequestsHttpResponse(200, {"Content-Type": "application/json", "X-Test": "1"}, "OK")
    assert resp.status == 200
    assert resp.reason == "OK"
    assert resp["content-type"] == "application/json"
    assert "x-test" in resp

    # 2. HTTP 308 resumable upload response MUST NOT follow Location redirect
    seen_requests: List[Dict[str, Any]] = []

    class FakeSession:
        def __init__(self, responses):
            self._responses = list(responses)
            self.closed = False

        def request(self, method, uri, data=None, headers=None, allow_redirects=False, timeout=None):
            seen_requests.append({
                "method": method,
                "uri": uri,
                "headers": dict(headers or {}),
                "allow_redirects": allow_redirects,
                "timeout": timeout,
            })
            item = self._responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        def close(self):
            self.closed = True

    res_308 = SimpleNamespace(
        status_code=308,
        headers={"Range": "bytes=0-1023", "Location": "https://upload.example/next"},
        content=b"",
        reason="Resume Incomplete",
    )
    http_308 = drive_http.RequestsHttp(FakeSession([res_308]))
    r_out, content_out = http_308.request("https://upload.example/session", method="PUT", body=b"abc")
    assert r_out.status == 308
    assert r_out["range"] == "bytes=0-1023"
    assert content_out == b""
    assert len(seen_requests) == 1

    # 3. Cross-host redirect strips Authorization & Cookie; same-host preserves Authorization
    seen_requests.clear()
    res_302_cross = SimpleNamespace(
        status_code=302,
        headers={"Location": "https://other-host.example/file"},
        content=b"",
        reason="Found",
    )
    res_200 = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "text/plain"},
        content=b"ok-bytes",
        reason="OK",
    )
    http_redir = drive_http.RequestsHttp(FakeSession([res_302_cross, res_200]))
    r2, body2 = http_redir.request(
        "https://api-host.example/start",
        method="GET",
        headers={"Authorization": "Bearer [REDACTED]", "Cookie": "[REDACTED]", "Accept": "*/*"},
    )
    assert r2.status == 200
    assert body2 == b"ok-bytes"
    assert len(seen_requests) == 2
    assert "Authorization" not in seen_requests[1]["headers"]
    assert "Cookie" not in seen_requests[1]["headers"]
    assert seen_requests[1]["headers"].get("Accept") == "*/*"

    # 4. Transient exception mapping to transport.TRANSIENT_TYPES
    for req_exc, expected_type in (
        (requests.exceptions.SSLError("tls eof"), ssl.SSLError),
        (requests.exceptions.ConnectTimeout("connect timeout"), socket.timeout),
        (requests.exceptions.ReadTimeout("read timeout"), socket.timeout),
        (requests.exceptions.ConnectionError("conn reset"), ConnectionError),
    ):
        http_err = drive_http.RequestsHttp(FakeSession([req_exc]))
        with pytest.raises(expected_type) as exc_info:
            http_err.request("https://api.example/test")
        assert transport_module.transient(exc_info.value) is True

    # 5. Service-account credentials rejected; user credentials get pooled adapter (16/16)
    fake_sa = SimpleNamespace(service_account_email="sa@example.iam.gserviceaccount.com")
    with pytest.raises(RuntimeError, match="SERVICE_ACCOUNT_CREDENTIALS_FORBIDDEN"):
        drive_http.build_authorized_session(fake_sa)

    from google.oauth2.credentials import Credentials

    user_creds = Credentials(token="placeholder-token")
    session = drive_http.build_authorized_session(user_creds)
    try:
        adapter = session.get_adapter("https://www.googleapis.com/")
        assert adapter._pool_connections == 16
        assert adapter._pool_maxsize == 16
    finally:
        session.close()


def test_p0_1_bounded_readonly_concurrency_thread_isolation_stats_lock_and_single_writer():
    assert hasattr(drive_module, "MAX_READ_WORKERS"), "P0-1 MAX_READ_WORKERS not defined"
    assert drive_module.MAX_READ_WORKERS == 8
    assert hasattr(drive_module.Drive, "map_readonly"), "P0-1 Drive.map_readonly not implemented"
    assert hasattr(drive_module, "map_readonly"), "P0-1 module map_readonly not implemented"

    # Build a Drive without hitting real credentials
    drv = drive_module.Drive.__new__(drive_module.Drive)
    drv._credentials = object()
    drv._main_thread_id = threading.get_ident()
    drv._read_local = threading.local()
    drv._read_semaphore = threading.BoundedSemaphore(drive_module.MAX_READ_WORKERS)
    main_closed = {"closed": False}

    class DummyHttp:
        def close(self):
            main_closed["closed"] = True

    drv.api = SimpleNamespace(_http=DummyHttp(), marker="main")
    drv.docs = SimpleNamespace(api=SimpleNamespace(_http=DummyHttp()))
    drv._read_clients = (drv.api, drv.docs.api)

    created_per_thread: Dict[int, List[Any]] = {}
    creation_lock = threading.Lock()

    def fake_build_pair():
        tid = threading.get_ident()
        closed_flag = {"closed": False}

        class WorkerHttp:
            def close(self):
                closed_flag["closed"] = True

        api = SimpleNamespace(_http=WorkerHttp(), tid=tid, closed=closed_flag)
        docs = SimpleNamespace(api=SimpleNamespace(_http=WorkerHttp()))
        with creation_lock:
            created_per_thread.setdefault(tid, []).append(api)
        return api, docs

    drv._build_readonly_client_pair = fake_build_pair

    # 1. Order preservation, concurrency bound <= 8, and worker-thread reset isolation
    active = {"now": 0, "peak": 0}
    active_lock = threading.Lock()

    def worker_fn(idx: int):
        with active_lock:
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
        try:
            api, _ = drv._get_read_clients()
            assert api is not drv.api, "Worker thread must not share main thread Resource"
            assert api.tid == threading.get_ident()
            if idx % 7 == 0:
                drv._reset_read_connections()
            time.sleep(0.01)
            return idx * 3
        finally:
            with active_lock:
                active["now"] -= 1

    results = drv.map_readonly(worker_fn, list(range(32)), max_workers=8)
    assert results == [i * 3 for i in range(32)]
    assert 1 <= active["peak"] <= 8
    assert main_closed["closed"] is False, "Worker reset must never close main thread client"

    # Invalid max_workers fails closed
    with pytest.raises(ValueError):
        drv.map_readonly(lambda x: x, [1, 2], max_workers=0)
    with pytest.raises(ValueError):
        drv.map_readonly(lambda x: x, [1, 2], max_workers=9)

    # 2. Thread-safe READ_RETRY_STATS under concurrent retries
    transport_module.reset_read_retry_stats()

    def retry_task(_):
        state = {"n": 0}

        def flaky():
            state["n"] += 1
            if state["n"] == 1:
                raise ConnectionError("transient")
            return "ok"

        return transport_module.retry_read(flaky, attempts=3, base=0.0, sleep=lambda _: None)

    out = drive_module.map_readonly(drv, retry_task, list(range(40)), max_workers=8)
    assert out == ["ok"] * 40
    stats = transport_module.read_retry_stats()
    assert stats["attempts"] == 80
    assert stats["retries"] == 40
    assert stats["failures"] == 0

    # 3. Main-thread single-writer enforcement: writes from worker thread MUST fail closed
    errors: List[str] = []

    def try_writes():
        for name, call in (
            ("put", lambda: drv.put("f1", b"x", "text/plain")),
            ("put_managed_doc", lambda: drv.put_managed_doc("d1", b"x")),
            ("move", lambda: drv.move("f1", "dst", "src")),
            ("ensure", lambda: drv.ensure("p", "k", "n", "text/plain", b"x")),
            ("ensure_copy", lambda: drv.ensure_copy("p", "k", "f1", "n")),
        ):
            try:
                call()
            except RuntimeError as exc:
                errors.append(f"{name}:{exc}")

    t = threading.Thread(target=try_writes)
    t.start()
    t.join()
    assert len(errors) == 5
    assert all("WRITE_OFF_MAIN_THREAD_FORBIDDEN" in err for err in errors)


# ---------------------------------------------------------------------------
# P0-2: Server-native sha256Checksum & batch zero-drift verification
# ---------------------------------------------------------------------------


def test_p0_2_server_sha256_field_and_batch_verifier_fail_closed_fallback():
    assert "sha256Checksum" in drive_module.FIELDS.split(","), (
        "P0-2 sha256Checksum missing from cba_kb.drive.FIELDS"
    )
    assert hasattr(release_module, "verify_remote_objects_batch"), (
        "P0-2 release.verify_remote_objects_batch not implemented"
    )

    drv = CountingFakeDrive(include_sha256=True)
    drv.add("f-bin-1", b"alpha-bytes", name="alpha.txt", mime="text/plain", parent="data")
    drv.add("f-bin-2", b"beta-bytes", name="beta.txt", mime="text/plain", parent="data")
    drv.add(
        "f-no-sha",
        b"gamma-no-server-sha",
        name="gamma.txt",
        mime="text/plain",
        parent="data",
        include_sha256=False,
    )
    doc_payload = wrap("managed native body").encode("utf-8")
    drv.add("f-native", doc_payload, name="INDEX", mime=orch.NATIVE_DOCUMENT, parent="data")

    expected = [
        {"id": "f-bin-1", "name": "alpha.txt", "sha256": digest(b"alpha-bytes"), "mode": "binary"},
        {"id": "f-bin-2", "name": "beta.txt", "sha256": digest(b"beta-bytes"), "mode": "binary"},
        {"id": "f-no-sha", "name": "gamma.txt", "sha256": digest(b"gamma-no-server-sha"), "mode": "binary"},
        {"id": "f-native", "name": "INDEX", "sha256": digest(doc_payload), "mode": "managed_doc"},
    ]
    drv.reset_counts()
    res = release_module.verify_remote_objects_batch(drv, expected, folders=["data"])
    assert res["status"] == "PASS"
    assert res["server_hash_verified"] == 2
    assert res["content_fallback_verified"] == 2
    assert drv.counts["list"] == 1
    assert drv.counts["download"] == 2  # ONLY f-no-sha + f-native; 0 downloads for f-bin-1 & f-bin-2

    # 1-byte tamper on binary file caught via sha256Checksum with 0 downloads
    drv.tamper_remote("f-bin-1")
    drv.reset_counts()
    with pytest.raises(RuntimeError, match="REMOTE_HASH_MISMATCH"):
        release_module.verify_remote_objects_batch(drv, expected[:2], folders=["data"])
    assert drv.counts["download"] == 0

    # 1-byte tamper on missing-sha file caught via content fallback
    drv.tamper_remote("f-no-sha")
    with pytest.raises(RuntimeError, match="REMOTE_HASH_MISMATCH"):
        release_module.verify_remote_objects_batch(drv, [expected[2]], folders=["data"])

    # 1-byte tamper on Native Doc caught via managed_doc fallback
    drv.tamper_remote("f-native")
    with pytest.raises(RuntimeError, match="REMOTE_HASH_MISMATCH"):
        release_module.verify_remote_objects_batch(drv, [expected[3]], folders=["data"])


# ---------------------------------------------------------------------------
# P0-3: Topology validation over carry_forward ∪ delta entries
# ---------------------------------------------------------------------------


def test_p0_3_topology_validation_supports_carry_forward_union():
    nodes = [
        {"id": "root", "role": "ROOT", "parents": []},
        {"id": "cur-zone", "role": "CURRENT_ZONE", "parents": ["root"]},
        {"id": "stg-zone", "role": "STAGING_ZONE", "parents": ["root"]},
        {"id": "his-zone", "role": "HISTORY_ZONE", "parents": ["root"]},
        {"id": "ev-zone", "role": "EVIDENCE_ZONE", "parents": ["root"]},
        {"id": "t-delta", "role": "CURRENT_TARGET", "parents": ["cur-zone"]},
        {"id": "t-cf-1", "role": "CURRENT_TARGET", "parents": ["cur-zone"]},
        {"id": "t-cf-ev", "role": "EVIDENCE_TARGET", "parents": ["ev-zone"]},
    ]
    res = release_module.validate_release_topology(
        nodes,
        release_id="v2.2.1-1",
        planned_target_ids=["t-delta"],
        carry_forward_target_ids=["t-cf-1", "t-cf-ev"],
    )
    assert res["status"] == "PASS"

    # Missing carry-forward node fails closed
    with pytest.raises(release_module.ReleaseContractError, match="RELEASE_TOPOLOGY_TARGET_MISSING"):
        release_module.validate_release_topology(
            nodes[:-1],
            release_id="v2.2.1-1",
            planned_target_ids=["t-delta"],
            carry_forward_target_ids=["t-cf-1", "t-cf-ev"],
        )

    # Duplicate overlap between delta and carry_forward fails closed
    with pytest.raises(release_module.ReleaseContractError, match="RELEASE_TOPOLOGY_TARGET_DUPLICATE"):
        release_module.validate_release_topology(
            nodes,
            release_id="v2.2.1-1",
            planned_target_ids=["t-delta"],
            carry_forward_target_ids=["t-delta", "t-cf-1", "t-cf-ev"],
        )


# ---------------------------------------------------------------------------
# P0-4: Deterministic bundle_v1, PlacementEnforcedDrive audit & security guards
# ---------------------------------------------------------------------------


def test_p0_4_bundle_v1_determinism_placement_audit_and_security_guards(tmp_path):
    assert hasattr(release_module, "build_release_bundle"), "P0-4 build_release_bundle not implemented"
    assert hasattr(release_module, "extract_release_bundle"), "P0-4 extract_release_bundle not implemented"

    drv = CountingFakeDrive()
    drv.add("f-a", b"old-a", name="a.txt", mime="text/plain", parent="data")
    doc_old = wrap("old native doc").encode("utf-8")
    doc_new = wrap("new native doc").encode("utf-8")
    drv.add("f-doc", doc_old, name="README", mime=orch.NATIVE_DOCUMENT, parent="root")
    drv.add(
        "status",
        json.dumps({"state": "INITIAL", "current_release_id": "v2.2.0-1"}).encode("utf-8"),
        name="release_status.json",
        mime="application/json",
        parent="config",
    )

    cand_a = tmp_path / "new_a.txt"
    cand_a.write_bytes(b"new-a")
    cand_doc = tmp_path / "new_doc.txt"
    cand_doc.write_bytes(doc_new)
    entries = [
        {"id": "f-a", "name": "a.txt", "mime": "text/plain", "path": str(cand_a)},
        {"id": "f-doc", "name": "README", "mime": orch.NATIVE_DOCUMENT, "mode": "managed_doc", "path": str(cand_doc)},
    ]

    # Invalid archive_format fails closed
    with pytest.raises(ValueError, match="ARCHIVE_FORMAT_INVALID"):
        release_module.prepare(drv, tmp_path / "bad", "r-bad", entries, "archive", "status", archive_format="zip_invalid")

    root1 = tmp_path / "rel1"
    root2 = tmp_path / "rel2"
    plan1 = release_module.prepare(drv, root1, "r-b1", entries, "archive", "status", archive_format="bundle_v1")
    plan2 = release_module.prepare(drv, root2, "r-b1", entries, "archive", "status", archive_format="bundle_v1")
    assert plan1["archive_format"] == "bundle_v1"

    # Byte-level determinism across two independent builds
    b1 = release_module.build_release_bundle(root1, plan1)
    b2 = release_module.build_release_bundle(root2, plan2)
    assert b1["bytes"] == b2["bytes"]
    assert b1["sha256"] == b2["sha256"]

    # Archive via PlacementEnforcedDrive: 1 folder + 1 bundle + 1 native backup = 3 guarded creations
    enforced = PlacementEnforcedDrive(drv)
    prev = release_module.archive_snapshot(enforced, root1, plan1)
    release_module.validate_previous_snapshot(plan1, prev)
    archive_children = drv.list("archive__r-b1")
    assert len(archive_children) == 2  # release_bundle.tar.gz + before_README native copy
    names = {c["name"] for c in archive_children}
    assert "release_bundle.tar.gz" in names
    assert "before_README" in names
    guarded_names = [rec.name for rec in enforced.guarded_creations()]
    assert "release_bundle.tar.gz" in guarded_names
    assert "before_README" in guarded_names

    # Safe extractor guards: Zip-Slip, absolute path, symlink, decompression limit, hash mismatch
    def _make_tar_gz(members_spec):
        buf = io.BytesIO()
        with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tar:
                for info, payload_bytes in members_spec:
                    tar.addfile(info, io.BytesIO(payload_bytes) if payload_bytes is not None else None)
        return buf.getvalue()

    ti_escape = tarfile.TarInfo("../escape.txt")
    ti_escape.size = 4
    with pytest.raises(release_module.ReleaseContractError, match="BUNDLE_PATH_TRAVERSAL_FORBIDDEN"):
        release_module.extract_release_bundle(_make_tar_gz([(ti_escape, b"evil")]))

    ti_abs = tarfile.TarInfo("/etc/passwd")
    ti_abs.size = 4
    with pytest.raises(release_module.ReleaseContractError, match="BUNDLE_PATH_TRAVERSAL_FORBIDDEN"):
        release_module.extract_release_bundle(_make_tar_gz([(ti_abs, b"evil")]))

    ti_sym = tarfile.TarInfo("before/0")
    ti_sym.type = tarfile.SYMTYPE
    ti_sym.linkname = "/etc/passwd"
    with pytest.raises(release_module.ReleaseContractError, match="BUNDLE_UNSAFE_MEMBER_TYPE"):
        release_module.extract_release_bundle(_make_tar_gz([(ti_sym, None)]))

    ti_big = tarfile.TarInfo("before/0")
    ti_big.size = 2048
    with pytest.raises(release_module.ReleaseContractError, match="BUNDLE_EXTRACT_LIMIT_EXCEEDED"):
        release_module.extract_release_bundle(_make_tar_gz([(ti_big, b"x" * 2048)]), max_bytes=1024)

    # Tampered member digest inside valid manifest fails closed
    extracted_ok = release_module.extract_release_bundle(b1["bytes"])
    tampered_members = dict(extracted_ok["members"])
    tampered_members["before/0"] = b"tampered-before-bytes"
    tampered_specs = []
    for mpath in sorted(tampered_members):
        raw = tampered_members[mpath]
        ti = tarfile.TarInfo(mpath)
        ti.size = len(raw)
        tampered_specs.append((ti, raw))
    with pytest.raises(release_module.ReleaseContractError, match="BUNDLE_MEMBER_HASH_MISMATCH"):
        release_module.extract_release_bundle(_make_tar_gz(tampered_specs))

    # Legacy default mode (archive_format omitted) preserves per-file archival
    root_legacy = tmp_path / "rel_legacy"
    plan_legacy = release_module.prepare(drv, root_legacy, "r-leg", entries, "archive", "status")
    assert "archive_format" not in plan_legacy
    prev_leg = release_module.archive_snapshot(PlacementEnforcedDrive(drv), root_legacy, plan_legacy)
    release_module.validate_previous_snapshot(plan_legacy, prev_leg)
    legacy_children = drv.list("archive__r-leg")
    assert len(legacy_children) >= 5  # native + before/candidate x2 + plan.json


# ---------------------------------------------------------------------------
# P0-5: Sanitized API call telemetry counters
# ---------------------------------------------------------------------------


def test_p0_5_api_call_telemetry_counters_sanitized_and_thread_safe():
    assert hasattr(transport_module, "api_call_stats"), "P0-5 api_call_stats not implemented"
    assert hasattr(transport_module, "record_api_call"), "P0-5 record_api_call not implemented"
    assert hasattr(transport_module, "reset_api_call_stats"), "P0-5 reset_api_call_stats not implemented"

    expected_keys = {"list_calls", "meta_calls", "download_calls", "upload_calls", "copy_calls", "move_calls"}
    transport_module.reset_api_call_stats()
    stats0 = transport_module.api_call_stats()
    assert set(stats0) == expected_keys
    assert all(v == 0 for v in stats0.values())

    def bump(_):
        for k in expected_keys:
            transport_module.record_api_call(k)

    threads = [threading.Thread(target=bump, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stats1 = transport_module.api_call_stats()
    assert all(stats1[k] == 16 for k in expected_keys)

    with pytest.raises(ValueError):
        transport_module.record_api_call("unknown_counter_kind")


# ---------------------------------------------------------------------------
# P0-2 + P0-3 + P0-4 + P0-5: 550-file full Stage 4 benchmark, tamper & restore
# ---------------------------------------------------------------------------


def _build_550_file_fixture(tmp_path: Path):
    """Build a realistic 550-file Drive fixture (109 protected + 6 Native Docs, 20 deltas)."""
    engine_dir = tmp_path / "engine"
    (engine_dir / "docs").mkdir(parents=True, exist_ok=True)
    (engine_dir / "docs/OPERATIONS_V1_5.md").write_text("ops guide\n", encoding="utf-8")
    (engine_dir / "docs/MASTER_MIGRATION_NOTE.md").write_text("migration note\n", encoding="utf-8")
    (engine_dir / "src").mkdir(parents=True, exist_ok=True)

    reg = {
        "registry_version": "1",
        "registry_release_id": "v2.2.0-1",
        "products": [
            {
                "product_id": "domestic_season_snapshots",
                "semantic_grain": "domestic_season_registration_snapshot",
                "authority": "canonical",
                "business_key": ["record_key"],
                "artifact_key": "input/MASTER.xlsx",
                "generated_from": [],
                "deprecated_by": None,
                "read_policy": "canonical_only",
                "current_eligible": True,
                "independent_write_allowed": True,
                "selector": "MASTER",
            },
            {
                "product_id": "master_csv",
                "semantic_grain": "domestic_season_registration_snapshot",
                "authority": "compatibility",
                "business_key": ["record_key"],
                "artifact_key": "derived/MASTER.csv",
                "generated_from": ["domestic_season_snapshots"],
                "deprecated_by": "domestic_season_snapshots",
                "read_policy": "compatibility_only",
                "current_eligible": True,
                "independent_write_allowed": False,
                "projection": {
                    "source": "domestic_season_snapshots",
                    "fields": {
                        "record_key": "record_key",
                        "season": "season",
                        "club_id": "club_id",
                        "player": "player",
                    },
                },
            },
        ],
    }
    reg_bytes = yaml.safe_dump(reg, allow_unicode=True, sort_keys=False).encode("utf-8")
    master_csv = (
        "record_key,season,club_id,player\n"
        "2025-2026|sz|0,2025-2026,sz,synth_0\n"
        "2025-2026|sz|1,2025-2026,sz,synth_1\n"
        "2025-2026|sz|2,2025-2026,sz,synth_2\n"
    ).encode("utf-8-sig")
    master_xlsx = _make_master_xlsx(3)
    ev_row = json.dumps({"event_key": "ev-0", "date": None, "official": "known"}).encode("utf-8") + b"\n"
    compat_row = json.dumps({"event_key": "ev-0", "date": None}).encode("utf-8") + b"\n"
    sr_csv = b"source_file_id,processing_status\nf-ev-000,accepted\n"

    # Build 550 active artifacts:
    # - 9 control plane targets (2 Native Docs: entry/README, derived/INDEX.md)
    # - 4 additional unchanged Native Docs (total 6 Native Docs)
    # - 109 protected targets: MASTER.xlsx (1) + 3 six_table + source_registry.csv (1) + 104 evidence files
    # - 3 canonical/derived business tables: MASTER.csv, events.jsonl, old_events.jsonl
    # - 420 existing code mirrors (15 modified, 405 unchanged) + 5 existing consumer/research targets (1 modified, 4 unchanged)
    # Total existing active = 9 + 4 + 109 + 3 + 420 + 5 = 550!
    # Plus 4 brand-new targets (3 new code mirrors + 1 new consumer target) -> 20 real non-control deltas!
    artifacts: List[tuple[str, str, str, str, str, str, bytes]] = [
        ("config/canonical_products.yaml", "f-reg", "canonical_products.yaml", "text/yaml", "binary", "config", reg_bytes),
        ("input/manifest.csv", "f-manifest", "manifest.csv", "text/csv", "binary", "config", b""),
        ("control/drive_map.yaml", "f-dmap", "drive_map.yaml", "text/yaml", "binary", "config", b"v1_5_release: {}\n"),
        ("entry/code", "f-entry-code", "CODE_MANIFEST.md", "text/markdown", "binary", "scripts", b"# code\n"),
        ("entry/README", "f-readme", "00_README_先读这个", orch.NATIVE_DOCUMENT, "managed_doc", "root", wrap("readme old\n").encode("utf-8")),
        ("derived/INDEX.md", "f-index", "INDEX", orch.NATIVE_DOCUMENT, "managed_doc", "data", wrap("index old\n").encode("utf-8")),
        ("entry/context", "f-context", "02_AI_PROJECT_CONTEXT_技术手册.md", "text/markdown", "binary", "root", b"ctx old\n"),
        ("ai/CONTEXT_CARD.md", "f-ctx-card", "CONTEXT_CARD.md", "text/markdown", "binary", "ai", b"card old\n"),
        ("CURRENT_VERSION_DOC", "f-ver-doc", "CBA-KB_v2.2.0.md", "text/markdown", "binary", "root", b"ver old\n"),
        ("derived/MASTER.csv", "f-master-csv", "MASTER.csv", "text/csv", "binary", "data", master_csv),
        ("facts/events.jsonl", "f-events-jsonl", "events.jsonl", "application/x-ndjson", "binary", "data", ev_row),
        ("facts/old_events.jsonl", "f-compat-jsonl", "old_events.jsonl", "application/x-ndjson", "binary", "data", compat_row),
        ("input/MASTER.xlsx", "f-master-xlsx", "MASTER.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "binary", "data", master_xlsx),
        ("facts/CBA_注册领域_六表.xlsx", "f-six", "CBA_注册领域_六表.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "binary", "data", _make_master_xlsx(1)),
        ("facts/CBA_球员注册_EVENTS.xlsx", "f-events-xlsx", "CBA_球员注册_EVENTS.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "binary", "data", _make_master_xlsx(1)),
        ("facts/CBA_外籍球员注册_SNAPSHOTS.xlsx", "f-foreign-xlsx", "CBA_外籍球员注册_SNAPSHOTS.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "binary", "data", _make_master_xlsx(1)),
        ("input/source_registry.csv", "f-sr", "source_registry.csv", "text/csv", "binary", "data", sr_csv),
        ("evidence/bayi_legacy_context.md", "f-ev-000", "bayi_legacy_context.md", "text/markdown", "binary", "evidence", b"bayi legacy evidence\n"),
    ]

    # 103 more evidence files -> 104 evidence + 5 core protected = 109 protected
    for idx in range(1, 104):
        artifacts.append((
            f"evidence/ev_{idx:03d}.md",
            f"f-ev-{idx:03d}",
            f"ev_{idx:03d}.md",
            "text/markdown",
            "binary",
            "evidence",
            f"evidence payload {idx:03d}\n".encode("utf-8"),
        ))

    # 4 unchanged Native Docs -> 2 control + 4 = 6 Native Docs total
    for idx in range(1, 5):
        artifacts.append((
            f"docs/native_guide_{idx}.doc",
            f"f-ndoc-{idx}",
            f"native_guide_{idx}",
            orch.NATIVE_DOCUMENT,
            "managed_doc",
            "data",
            wrap(f"unchanged native doc {idx}\n").encode("utf-8"),
        ))

    # 420 existing code mirrors (15 modified, 405 unchanged)
    tracked: List[str] = []
    tracked_hashes: Dict[str, str] = {}
    for idx in range(420):
        rel = f"src/mod_{idx:03d}.py"
        old_bytes = f"# module {idx:03d} v1\nVALUE = {idx}\n".encode("utf-8")
        new_bytes = (
            f"# module {idx:03d} v2\nVALUE = {idx + 1000}\n".encode("utf-8")
            if idx < 15 else old_bytes
        )
        p = engine_dir / rel
        p.write_bytes(new_bytes)
        tracked.append(rel)
        tracked_hashes[rel] = digest(new_bytes)
        artifacts.append((
            f"code/{rel}",
            f"f-code-{idx:03d}",
            rel.replace("/", "__"),
            "text/plain",
            "binary",
            "scripts",
            old_bytes,
        ))

    # 3 new code mirrors (part of 20 non-control deltas)
    for idx in range(420, 423):
        rel = f"src/mod_{idx:03d}.py"
        new_bytes = f"# brand new module {idx:03d}\n".encode("utf-8")
        (engine_dir / rel).write_bytes(new_bytes)
        tracked.append(rel)
        tracked_hashes[rel] = digest(new_bytes)

    # 5 existing consumer/research targets (1 modified, 4 unchanged)
    consumer_specs = [
        ("consumer/master_domestic_2017_2027_current.csv", "f-c-master", "CBA_MASTER_CURRENT.csv", "text/csv", b"c-master-v1\n", b"c-master-v1\n"),
        ("consumer/field_dictionary_current.md", "f-c-dict", "CBA_DICT_CURRENT.md", "text/markdown", b"# dict v1\n", b"# dict v2\n"),
        ("consumer/source_registry_index_current.csv", "f-c-sr", "CBA_SR_CURRENT.csv", "text/csv", b"sr-v1\n", b"sr-v1\n"),
        ("consumer/document_index_current.json", "f-c-docidx", "CBA_DOCIDX_CURRENT.json", "application/json", b'{"docs": []}\n', b'{"docs": []}\n'),
        ("research/card_900001_2025_2026", "f-r-card-1", "CARD_001_CURRENT.md", "text/markdown", b"# card 1\n", b"# card 1\n"),
    ]
    declared_consumer_targets: Dict[str, Dict[str, Any]] = {}
    for lk, fid, name, mime, old_b, new_b in consumer_specs:
        artifacts.append((lk, fid, name, mime, "binary", "data", old_b))
        declared_consumer_targets[lk] = {
            "name": name,
            "mime": mime,
            "mode": "binary",
            "publish_parent": "data",
            "content": new_b,
        }
    # 1 new consumer target -> total 15 mod code + 3 new code + 1 mod consumer + 1 new consumer = 20 deltas!
    declared_consumer_targets["consumer/collector_official_digest_current.md"] = {
        "name": "CBA_DIGEST_CURRENT.md",
        "mime": "text/markdown",
        "mode": "binary",
        "publish_parent": "data",
        "content": b"# digest v2.2.1\n",
    }

    assert len(artifacts) == 550, f"Expected exactly 550 existing active files, got {len(artifacts)}"

    manifest_lines = [
        "uid,asset_type,source_type,source_url,local_path,content_hash,drive_file_id,ima_item_id,status,synced_at,published_release,hash_scope"
    ]
    for lk, fid, _name, _mime, mode, _parent, content in artifacts:
        h = "" if lk == "input/manifest.csv" else digest(content)
        scope = "managed_prefix" if mode == "managed_doc" else "bytes"
        manifest_lines.append(f"{lk},release_artifact,generated,,{lk},{h},{fid},,published,,v2.2.0-1,{scope}")
    manifest_bytes = ("\n".join(manifest_lines) + "\n").encode("utf-8-sig")

    drive = CountingFakeDrive(include_sha256=True)
    manifest_rows = []
    status_artifacts = []
    previous_targets: Dict[str, Dict[str, Any]] = {}
    for lk, fid, name, mime, mode, parent, content in artifacts:
        actual = manifest_bytes if lk == "input/manifest.csv" else content
        drive.add(fid, actual, name=name, mime=mime, parent=parent)
        manifest_rows.append({"drive_file_id": fid, "uid": lk})
        status_artifacts.append({"id": fid, "name": name, "sha256": digest(actual)})
        previous_targets[fid] = {
            "mime": mime,
            "mode": mode,
            "allowed_parents": [parent, "staging"],
            "publish_parent": parent,
        }

    previous_status = {
        "state": "COMPLETE",
        "current_release_id": "v2.2.0-1",
        "code_commit": "b" * 40,
        "artifacts": status_artifacts,
    }
    drive.add(
        "status",
        json.dumps(previous_status, sort_keys=True).encode("utf-8"),
        name="release_status.json",
        mime="application/json",
        parent="config",
    )

    import_inventory = {
        "parents": {
            "root": "root",
            "ai": "ai",
            "scripts": "scripts",
            "config": "config",
            "data": "data",
            "archive": "archive",
            "evidence": "evidence",
        }
    }

    return {
        "engine_dir": engine_dir,
        "drive": drive,
        "previous_status": previous_status,
        "previous_targets": previous_targets,
        "manifest_rows": manifest_rows,
        "tracked": tracked,
        "tracked_hashes": tracked_hashes,
        "declared_consumer_targets": declared_consumer_targets,
        "import_inventory": import_inventory,
    }


def _make_fake_instance(tmp_path: Path, name: str, previous_targets: Dict[str, Any], import_inventory: Dict[str, Any]):
    inst_dir = tmp_path / name
    cfg_dir = inst_dir / "config"
    data_dir = inst_dir / "data"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    prod_policy = {
        "status_id": "status",
        "archive_id": "archive",
        "root_folder_id": "kb-root",
        "zones": {
            "current": ["root", "ai", "scripts", "config", "data"],
            "history": ["archive"],
            "staging": ["staging"],
            "evidence": ["evidence"],
        },
        "targets": copy.deepcopy(previous_targets),
        "dependency_ids": ["f-sr"],
    }
    (cfg_dir / "production.json").write_text(json.dumps(prod_policy, indent=2), encoding="utf-8")
    (cfg_dir / "import_inventory.json").write_text(json.dumps(import_inventory, indent=2), encoding="utf-8")
    (cfg_dir / "runtime.json").write_text(
        json.dumps({"inputs": {"manifest.csv": {"id": "f-manifest"}}}, indent=2),
        encoding="utf-8",
    )

    class FakeInstance:
        data_root = data_dir

        def config_path(self, fname: str) -> Path:
            if fname not in {"production.json", "import_inventory.json", "runtime.json"}:
                raise ValueError(f"UNKNOWN_INSTANCE_CONFIG: {fname}")
            return cfg_dir / fname

        def read_json(self, fname: str) -> Dict[str, Any]:
            return json.loads(self.config_path(fname).read_text(encoding="utf-8"))

    return FakeInstance()


def _run_stage4_cycle(
    fx,
    tmp_path: Path,
    *,
    run_name: str,
    release_id: str,
    engine_sha: str,
    incremental: bool | None = None,
    archive_format: str | None = None,
):
    drive = copy.deepcopy(fx["drive"])
    instance = _make_fake_instance(tmp_path, f"inst_{run_name}", fx["previous_targets"], fx["import_inventory"])
    status_raw = drive.get("status")
    status_meta = drive.meta("status")
    drive.reset_counts()

    proj = orch.project_targets(
        release_id=release_id,
        engine_sha=engine_sha,
        status_id="status",
        archive_id="archive",
        previous_status=fx["previous_status"],
        previous_targets=fx["previous_targets"],
        manifest_rows=fx["manifest_rows"],
        tracked=fx["tracked"],
        tracked_hashes=fx["tracked_hashes"],
        code_parent="scripts",
        status_hash=digest(status_raw),
        status_meta=orch.fingerprint(status_meta),
        declared_consumer_targets=fx["declared_consumer_targets"],
    )
    res_dir = instance.data_root / f"reserve_{run_name}"
    enforced_reserve = PlacementEnforcedDrive(drive)
    reserved = orch.reserve_staging(
        enforced_reserve,
        instance,
        release_id=release_id,
        projection=proj,
        output=res_dir,
        single_writer=True,
    )
    allocation = reserved["allocation"]
    freeze_dir = instance.data_root / f"freeze_{run_name}"
    consumer_contents = {k: v["content"] for k, v in fx["declared_consumer_targets"].items()}

    freeze_kwargs: Dict[str, Any] = {
        "instance": instance,
        "engine_root": fx["engine_dir"],
        "release_id": release_id,
        "projection": proj,
        "allocation": allocation,
        "output": freeze_dir,
        "consumer_target_contents": consumer_contents,
    }
    if incremental is not None:
        freeze_kwargs["incremental"] = incremental
    if archive_format is not None:
        freeze_kwargs["archive_format"] = archive_format

    freeze_res = orch.freeze_plan(drive, **freeze_kwargs)
    outbox = freeze_dir / "outbox"
    enforced_pub = PlacementEnforcedDrive(drive)
    pub_journal = release_module.publish(enforced_pub, outbox, single_writer=True)
    verify_res = release_module.verify(drive, outbox)
    return {
        "drive": drive,
        "instance": instance,
        "projection": proj,
        "allocation": allocation,
        "freeze_dir": freeze_dir,
        "outbox": outbox,
        "freeze_res": freeze_res,
        "publish_journal": pub_journal,
        "verify_res": verify_res,
        "enforced_pub": enforced_pub,
        "counts": dict(drive.counts),
    }


def test_p0_2_p0_3_p0_4_p0_5_550_file_incremental_stage4_benchmark_tamper_and_bundle_restore(
    monkeypatch, tmp_path
):
    assert "v2.2.1-1" in orch.V215_RELEASE_SPECS, "P0-3 v2.2.1-1 release spec not registered"
    monkeypatch.setattr(
        orch, "verify_execution_sha",
        lambda root, sha, baseline=None: {"head": sha, "clean": True},
    )
    monkeypatch.setattr(
        release_module, "verify_code_provenance",
        lambda repo, code_commit, exec_sha=None: {"status": "PASS", "code_commit": code_commit},
    )

    fx = _build_550_file_fixture(tmp_path)

    # 1. Legacy full-run baseline (v2.2.0-1, non-incremental, legacy archive, no server sha optimization)
    fx_legacy = copy.deepcopy(fx)
    fx_legacy["drive"].include_sha256 = False
    for rec in fx_legacy["drive"].files.values():
        rec["include_sha256"] = False
    legacy_run = _run_stage4_cycle(
        fx_legacy,
        tmp_path,
        run_name="legacy_550",
        release_id="v2.2.0-1",
        engine_sha=orch.V220_1_BASELINE_SHA,
    )
    legacy_plan = read(legacy_run["outbox"] / "plan.json")
    legacy_archive_objs = legacy_run["drive"].list("archive__v2.2.0-1")
    assert len(legacy_plan["entries"]) == 554
    assert len(legacy_archive_objs) >= 1100

    # 2. v2.2.1-1 incremental O(Δ) + server sha256 + bundle_v1 full Stage 4 cycle
    v221_run = _run_stage4_cycle(
        fx,
        tmp_path,
        run_name="v221_550",
        release_id="v2.2.1-1",
        engine_sha=V221_1_BASELINE_SHA,
    )
    v221_plan = read(v221_run["outbox"] / "plan.json")
    v221_drive = v221_run["drive"]
    v221_archive_objs = v221_drive.list("archive__v2.2.1-1")

    # Assert O(Δ) plan entries (20 real deltas + 9 control docs = 29, NOT 554!)
    assert len(v221_plan["entries"]) <= 35
    assert len(v221_plan["entries"]) < len(legacy_plan["entries"]) // 10

    # Assert releases/v2.2.1-1/ has <= 8 created objects (1 bundle + 2 changed Native Docs = 3)
    assert len(v221_archive_objs) <= 8
    assert any(obj["name"] == "release_bundle.tar.gz" for obj in v221_archive_objs)

    # Assert >= 85% download call reduction vs legacy full-run baseline
    legacy_downloads = legacy_run["counts"]["download"]
    v221_downloads = v221_run["counts"]["download"]
    reduction = 1.0 - (v221_downloads / legacy_downloads)
    assert reduction >= 0.85, (
        f"Expected >= 85% download reduction, got {reduction:.1%} "
        f"(legacy={legacy_downloads}, v221={v221_downloads})"
    )

    # Assert PlacementEnforcedDrive audited release_bundle.tar.gz and changed Native Doc backups
    audited_names = {r.name for r in v221_run["enforced_pub"].guarded_creations()}
    assert "release_bundle.tar.gz" in audited_names
    assert "before_00_README_先读这个" in audited_names
    assert "before_INDEX" in audited_names

    # Assert final release_status.json & manifest.csv retain ALL 554 active artifacts!
    final_status = json.loads(v221_drive.get("status"))
    assert final_status["state"] == "COMPLETE"
    assert final_status["current_release_id"] == "v2.2.1-1"
    assert len(final_status["artifacts"]) == 554
    status_hash_by_id = {item["id"]: item["sha256"] for item in final_status["artifacts"]}
    assert status_hash_by_id["f-code-000"] == fx["tracked_hashes"]["src/mod_000.py"]
    assert status_hash_by_id["f-code-400"] == fx["tracked_hashes"]["src/mod_400.py"]

    manifest_out_rows = list(csv.DictReader(io.StringIO(v221_drive.get("f-manifest").decode("utf-8-sig"))))
    assert len(manifest_out_rows) == 554
    manifest_by_uid = {r["uid"]: r for r in manifest_out_rows}
    assert manifest_by_uid["code/src/mod_400.py"]["content_hash"] == fx["tracked_hashes"]["src/mod_400.py"]
    assert manifest_by_uid["code/src/mod_000.py"]["content_hash"] == fx["tracked_hashes"]["src/mod_000.py"]

    # 3. 1-byte remote tamper on unchanged carry_forward file -> verify() fails closed!
    tamper_verify_drive = copy.deepcopy(v221_drive)
    tamper_verify_drive.tamper_remote("f-code-400")
    with pytest.raises((RuntimeError, ValueError), match="REMOTE_HASH_MISMATCH|CARRY_FORWARD"):
        release_module.verify(tamper_verify_drive, v221_run["outbox"])

    # 4. 1-byte remote tamper on protected evidence file -> verify() fails closed!
    tamper_ev_drive = copy.deepcopy(v221_drive)
    tamper_ev_drive.tamper_remote("f-ev-050")
    with pytest.raises(ValueError, match="ZERO_BUSINESS_FACT_DELTA"):
        release_module.verify(tamper_ev_drive, v221_run["outbox"])

    # 5. 1-byte remote tamper on carry_forward BEFORE freeze_plan -> freeze_plan fails closed!
    fx_tamper_cf = copy.deepcopy(fx)
    fx_tamper_cf["drive"].tamper_remote("f-code-400")
    with pytest.raises(orch.ProjectionError, match="CARRY_FORWARD_REMOTE_DRIFT"):
        _run_stage4_cycle(
            fx_tamper_cf,
            tmp_path,
            run_name="tamper_cf_freeze",
            release_id="v2.2.1-1",
            engine_sha=V221_1_BASELINE_SHA,
        )

    # 6. 1-byte remote tamper on carry_forward AFTER freeze, BEFORE publish -> publish fails closed!
    pre_pub_drive = copy.deepcopy(fx["drive"])
    pre_pub_inst = _make_fake_instance(tmp_path, "inst_prepub_tamper", fx["previous_targets"], fx["import_inventory"])
    status_raw = pre_pub_drive.get("status")
    status_meta = pre_pub_drive.meta("status")
    proj_tp = orch.project_targets(
        release_id="v2.2.1-1",
        engine_sha=V221_1_BASELINE_SHA,
        status_id="status",
        archive_id="archive",
        previous_status=fx["previous_status"],
        previous_targets=fx["previous_targets"],
        manifest_rows=fx["manifest_rows"],
        tracked=fx["tracked"],
        tracked_hashes=fx["tracked_hashes"],
        code_parent="scripts",
        status_hash=digest(status_raw),
        status_meta=orch.fingerprint(status_meta),
        declared_consumer_targets=fx["declared_consumer_targets"],
    )
    res_tp = orch.reserve_staging(
        PlacementEnforcedDrive(pre_pub_drive),
        pre_pub_inst,
        release_id="v2.2.1-1",
        projection=proj_tp,
        output=pre_pub_inst.data_root / "res_tp",
        single_writer=True,
    )
    freeze_tp = pre_pub_inst.data_root / "freeze_tp"
    orch.freeze_plan(
        pre_pub_drive,
        instance=pre_pub_inst,
        engine_root=fx["engine_dir"],
        release_id="v2.2.1-1",
        projection=proj_tp,
        allocation=res_tp["allocation"],
        output=freeze_tp,
        consumer_target_contents={k: v["content"] for k, v in fx["declared_consumer_targets"].items()},
    )
    pre_pub_drive.tamper_remote("f-code-400")
    with pytest.raises((RuntimeError, ValueError), match="REMOTE_HASH_MISMATCH|CARRY_FORWARD"):
        release_module.publish(pre_pub_drive, freeze_tp / "outbox", single_writer=True)

    # 7. Cloud-bundle restore: delete local before/* & status.before.json, restore from release_bundle.tar.gz!
    outbox = v221_run["outbox"]
    shutil.rmtree(outbox / "before")
    (outbox / "status.before.json").unlink()
    restored_journal = release_module.restore(v221_drive, outbox, single_writer=True)
    assert restored_journal["state"] == "ROLLED_BACK"

    # Verify 100% byte-exact restoration of all 550 original files + status!
    restored_status = json.loads(v221_drive.get("status"))
    assert restored_status["state"] == "ROLLED_BACK"
    assert restored_status["current_release_id"] == "v2.2.0-1"
    assert restored_status["rolled_back_release_id"] == "v2.2.1-1"
    for item in fx["previous_status"]["artifacts"]:
        fid = item["id"]
        if v221_drive.files[fid]["mimeType"] == orch.NATIVE_DOCUMENT:
            actual_bytes = v221_drive.get_managed_doc(fid)
        else:
            actual_bytes = v221_drive.get(fid)
        assert digest(actual_bytes) == item["sha256"], f"Rollback mismatch on {fid}"


# ---------------------------------------------------------------------------
# Hard Guardrails: frozen files untouched
# ---------------------------------------------------------------------------


def test_req221_hard_guardrails_frozen_files_untouched():
    actual_sha = hashlib.sha256(
        (ROOT / "scripts" / "prepare_production.py").read_bytes()
    ).hexdigest()
    assert actual_sha == FROZEN_PREPARE_PRODUCTION_SHA256

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == "2.0.2"
