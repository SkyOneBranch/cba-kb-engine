"""Q2 Round 1 & Round 2 remediation regression & adversarial tests for REQ-230 (cba-kb-engine v2.3.0).

Covers all Round 1 and Round 2 Q2 blockers without modifying Commit 1's frozen acceptance test file:
1. P0-2-R2-MISSING-METADATA-SHA: R2CloudStateStore.get_bytes fails closed on missing/malformed x-amz-meta-sha256
2. P0-2-DRIVE-PROTECTED-ANCESTOR & P0-2-NEW-R2-EMPTY-PROTECTION-SET-FAILOPEN: ancestor walk + empty protected set fail-closed
3. P0-3-NONATOMIC-DEDUP-LOCK: real 2-process HTTP mock R2 race, lease expiry recovery, conditional If-Match delete, Worker conditional put
4. P0-4-WORKER-R2-CONTRACT-DRIFT: JS/Python key parity (Chinese + Unicode \\u0085/\\u001c-\\u001f), SHA-256 verification (including embedded job.card_markdown), 5-domain coverage
5. P0-4-RUNNER-NONFUNCTIONAL & P0-4-NEW-R2-RUNNER-LOCK-OWNER-MISMATCH: Worker->Runner --job-id + --owner-id handoff & lock release, real --collector-cmd execution for uncached player
6. P0-5-SYMLINK-PARENT-ESCAPE: openat(O_DIRECTORY | O_NOFOLLOW) rejects symlinked directory/manifest/card and TOCTOU swap
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_SHA_V230_1 = "3a7239361b2b81f763dce3477b3c3ad81f357b90"


class _AtomicFakeR2Transport:
    """Thread-safe in-memory R2 transport honoring If-None-Match: * and If-Match: <etag>."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.objects: Dict[str, Tuple[bytes, Dict[str, str], str]] = {}
        self._etag_seq = 0

    def _next_etag(self, data: bytes) -> str:
        self._etag_seq += 1
        return f'"{hashlib.md5(data).hexdigest()}-{self._etag_seq}"'

    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: Dict[str, str],
        data: bytes = b"",
    ) -> Tuple[int, Dict[str, str], bytes]:
        parsed = urlparse(url)
        parts = parsed.path.lstrip("/").split("/", 1)
        key = parts[1] if len(parts) > 1 else ""
        qs = parse_qs(parsed.query)
        lower_headers = {k.lower(): v for k, v in headers.items()}

        with self._lock:
            if method == "PUT":
                if lower_headers.get("if-none-match") == "*" and key in self.objects:
                    return 412, {}, b"PreconditionFailed"
                if_match = lower_headers.get("if-match")
                if if_match is not None:
                    cur = self.objects.get(key)
                    if cur is None or cur[2].strip('"') != if_match.strip('"'):
                        return 412, {}, b"PreconditionFailed"
                meta = {
                    k: v
                    for k, v in lower_headers.items()
                    if k.startswith("x-amz-meta-")
                }
                etag = self._next_etag(data)
                self.objects[key] = (bytes(data), meta, etag)
                return 200, {"ETag": etag}, b""

            if method == "GET" and qs.get("list-type") == ["2"]:
                prefix = qs.get("prefix", [""])[0]
                matched = sorted(k for k in self.objects if k.startswith(prefix))
                items_xml = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in matched)
                xml_body = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    f"<ListBucketResult>{items_xml}</ListBucketResult>"
                ).encode("utf-8")
                return 200, {"content-type": "application/xml"}, xml_body

            if method == "GET":
                if key not in self.objects:
                    return 404, {}, b"NotFound"
                payload, meta, etag = self.objects[key]
                resp_hdrs = dict(meta)
                resp_hdrs["ETag"] = etag
                return 200, resp_hdrs, payload

            if method == "DELETE":
                # Real Cloudflare R2 does NOT support If-Match on DeleteObject; fail loudly if used.
                if "if-match" in lower_headers:
                    return 501, {}, b"NotImplemented:R2DeleteIfMatchUnsupported"
                self.objects.pop(key, None)
                return 204, {}, b""

        return 405, {}, b"MethodNotAllowed"


def _start_local_r2_http_server(transport: _AtomicFakeR2Transport) -> Tuple[ThreadingHTTPServer, str]:
    """Start a real local HTTP mock R2 server backed by _AtomicFakeR2Transport."""

    class _Handler(BaseHTTPRequestHandler):
        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length > 0 else b""
            hdrs = {k: v for k, v in self.headers.items()}
            status, resp_hdrs, payload = transport(
                self.command,
                f"http://127.0.0.1{self.path}",
                headers=hdrs,
                data=body,
            )
            self.send_response(status)
            for k, v in resp_hdrs.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch()

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# 1. P0-2-R2-MISSING-METADATA-SHA
# ---------------------------------------------------------------------------


def test_r2_missing_or_malformed_sha256_metadata_fails_closed():
    from cba_kb.cloud_state import R2CloudStateStore, R2Config

    transport = _AtomicFakeR2Transport()
    store = R2CloudStateStore(
        R2Config("https://syn-r2.example.invalid", "cba-kb-private", "SYN_K", "SYN_S"),
        transport=transport,
    )

    transport.objects["jobs/no_meta.json"] = (b'{"status":"RUNNING"}', {}, '"e1"')
    with pytest.raises(ValueError, match="R2_OBJECT_SHA256_MISMATCH"):
        store.get_bytes("jobs/no_meta.json")

    transport.objects["jobs/bad_meta.json"] = (
        b'{"status":"RUNNING"}',
        {"x-amz-meta-sha256": "not-a-valid-sha256"},
        '"e2"',
    )
    with pytest.raises(ValueError, match="R2_OBJECT_SHA256_MISMATCH"):
        store.get_bytes("jobs/bad_meta.json")

    payload = b'{"ok":true}'
    store.put_bytes("jobs/good_meta.json", payload)
    assert store.get_bytes("jobs/good_meta.json") == payload


# ---------------------------------------------------------------------------
# 2. P0-2-DRIVE-PROTECTED-ANCESTOR & P0-2-NEW-R2-EMPTY-PROTECTION-SET-FAILOPEN
# ---------------------------------------------------------------------------


def test_drive_snowball_rejects_subfolder_descendant_and_empty_protected_zone_fails_closed(
    tmp_path: Path, monkeypatch
):
    from cba_kb import cloud_state
    from cba_kb.cli import main as cli_main
    from cba_kb.drive import upsert_snowball_card_to_drive

    class _HierarchyFakeDrive:
        def __init__(self) -> None:
            self._main_thread_id = threading.get_ident()
            self.folder_parents = {
                "drv_nested_child_02": ["drv_nested_child_01"],
                "drv_nested_child_01": ["drv_zone_current_20data"],
                "drv_zone_current_20data": ["drv_root_00"],
                "drv_safe_snowball_25": ["drv_root_00"],
                "drv_root_00": [],
            }
            self.files: Dict[str, Dict[str, Any]] = {}
            self.create_calls = 0

        def get_parents(self, file_id: str) -> List[str]:
            return list(self.folder_parents.get(file_id, []))

        def list(self, parent: str) -> List[Dict[str, Any]]:
            return [dict(m) for m in self.files.values() if parent in m.get("parents", [])]

        def create_binary(self, parent: str, name: str, content: bytes, mime: str) -> Dict[str, Any]:
            self.create_calls += 1
            fid = f"drv_obj_{self.create_calls:03d}"
            meta = {"id": fid, "name": name, "mimeType": mime, "parents": [parent]}
            self.files[fid] = meta
            return dict(meta)

        def put(self, file_id: str, content: bytes, mime: str) -> Dict[str, Any]:
            return dict(self.files[file_id])

    fake_drive = _HierarchyFakeDrive()
    production_policy = {
        "zones": {
            "current": ["drv_zone_current_20data"],
            "history": ["drv_zone_history_90archive"],
            "staging": ["drv_zone_staging"],
            "evidence": ["drv_zone_evidence"],
        }
    }
    card_bytes = b"# Synthetic Card\n\nend_marker: true\n"
    filename = "研究卡_测试球员甲_SYN_P001_2026-2027_CURRENT.md"

    # A. Empty protected set (neither production_policy nor forbidden_folder_ids) -> FAIL-CLOSED
    with pytest.raises(ValueError, match="SNOWBALL_PROTECTED_ZONE_UNCONFIGURED"):
        upsert_snowball_card_to_drive(
            fake_drive,
            folder_id="drv_safe_snowball_25",
            filename=filename,
            content_bytes=card_bytes,
            forbidden_folder_ids=(),
            production_policy=None,
        )
    assert fake_drive.create_calls == 0

    # B. Deeply nested child inside zones["current"] -> rejected
    with pytest.raises(ValueError, match="SNOWBALL_FOLDER_IN_PROTECTED_ZONE"):
        upsert_snowball_card_to_drive(
            fake_drive,
            folder_id="drv_nested_child_02",
            filename=filename,
            content_bytes=card_bytes,
            production_policy=production_policy,
        )
    assert fake_drive.create_calls == 0

    # C. Sibling folder under root outside protected zones succeeds
    res = upsert_snowball_card_to_drive(
        fake_drive,
        folder_id="drv_safe_snowball_25",
        filename=filename,
        content_bytes=card_bytes,
        production_policy=production_policy,
    )
    assert res["action"] == "created"
    assert fake_drive.create_calls == 1

    # D. CLI headless Drive branch with CBA_KB_DRIVE_SNOWBALL_FOLDER_ID but NO protected folder IDs fails closed
    transport = _AtomicFakeR2Transport()
    fake_store = cloud_state.R2CloudStateStore(
        cloud_state.R2Config("https://syn-r2.example.invalid", "cba-kb-private", "SYN_K", "SYN_S"),
        transport=transport,
    )
    inst_dir = tmp_path / "headless_no_policy_inst"
    (inst_dir / "data").mkdir(parents=True)
    (inst_dir / "config").mkdir(parents=True)
    (inst_dir / "data" / "MASTER.csv").write_text(
        "season,club,player_name,player_id,position\n"
        "2026-2027,测试俱乐部甲,无保护区测试员,SYN_NOPROT01,G\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cloud_state, "R2CloudStateStore", lambda *a, **kw: fake_store)
    monkeypatch.setattr(
        cloud_state.R2Config,
        "from_env",
        classmethod(lambda cls, env=None: fake_store.config),
    )
    monkeypatch.setenv("CBA_KB_DRIVE_SNOWBALL_FOLDER_ID", "drv_safe_snowball_25")
    monkeypatch.delenv("CBA_KB_DRIVE_PROTECTED_FOLDER_IDS", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cba-kb",
            "--instance-root",
            str(inst_dir),
            "snowball-query",
            "--person",
            "无保护区测试员",
            "--season",
            "2026-2027",
        ],
    )
    with pytest.raises(SystemExit) as exc_info:
        cli_main()
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# 3. P0-3-NONATOMIC-DEDUP-LOCK (Real 2-Process HTTP Race, Lease Recovery & Worker Conditional Put)
# ---------------------------------------------------------------------------


def test_r2_two_process_atomic_lock_race_lease_recovery_and_worker_conditional_put(tmp_path: Path):
    from cba_kb.cloud_state import R2CloudStateStore, R2Config

    transport = _AtomicFakeR2Transport()
    server, base_url = _start_local_r2_http_server(transport)
    try:
        # Part A: Real 2-process OS subprocess race against live HTTP mock R2 server
        ready_0 = tmp_path / "ready_0"
        ready_1 = tmp_path / "ready_1"
        go_file = tmp_path / "go"
        out_0 = tmp_path / "out_0.json"
        out_1 = tmp_path / "out_1.json"

        child_code = r"""
import hashlib, json, sys, time
from pathlib import Path
from datetime import datetime, timezone
from cba_kb.cloud_state import R2CloudStateStore, R2Config
from cba_kb.snowball_orchestrator import SnowballOrchestrator

base_url, ready_path, go_path, out_path = sys.argv[1:5]
store = R2CloudStateStore(R2Config(base_url, "cba-kb-private", "SYN_K", "SYN_S"))
calls = []

class _SlowBackend:
    def acquire_person(self, person, *, season, job_id, force_refresh=False):
        calls.append(job_id)
        time.sleep(0.35)
        md = f"# 人物研究卡｜{person}（SYN_MP01）｜{season}｜CURRENT\n\nend_marker: true\n"
        return {
            "status": "ACQUIRED_AND_BUILT",
            "person": {"player_name": person, "player_id": "SYN_MP01"},
            "filename": f"研究卡_{person}_SYN_MP01_{season}_CURRENT.md",
            "card_markdown": md,
            "card_sha256": hashlib.sha256(md.encode("utf-8")).hexdigest(),
            "observed_until": "2026-11-24T12:00:00Z",
            "coverage_status": "COMPLETE",
        }

orch = SnowballOrchestrator(
    state_store=store,
    acquisition_backend=_SlowBackend(),
    now_fn=lambda: datetime(2026, 11, 24, 13, 0, 0, tzinfo=timezone.utc),
)
Path(ready_path).write_text("ready", encoding="utf-8")
while not Path(go_path).exists():
    time.sleep(0.005)
res = orch.get_or_acquire_person("双进程竞态球员", season="2026-2027")
Path(out_path).write_text(json.dumps({"result": res, "backend_calls": len(calls)}), encoding="utf-8")
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = f"{REPO_ROOT / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}"
        p0 = subprocess.Popen(
            [sys.executable, "-c", child_code, base_url, str(ready_0), str(go_file), str(out_0)],
            env=env,
        )
        p1 = subprocess.Popen(
            [sys.executable, "-c", child_code, base_url, str(ready_1), str(go_file), str(out_1)],
            env=env,
        )
        deadline = time.monotonic() + 10.0
        while not (ready_0.exists() and ready_1.exists()):
            assert time.monotonic() < deadline, "Subprocesses failed to reach barrier"
            time.sleep(0.02)
        go_file.write_text("go", encoding="utf-8")
        assert p0.wait(timeout=15) == 0
        assert p1.wait(timeout=15) == 0

        r0 = json.loads(out_0.read_text(encoding="utf-8"))
        r1 = json.loads(out_1.read_text(encoding="utf-8"))
        total_backend_calls = r0["backend_calls"] + r1["backend_calls"]
        assert total_backend_calls == 1, f"Expected exactly 1 winner across 2 processes, got r0={r0}, r1={r1}"
        statuses = {r0["result"]["status"], r1["result"]["status"]}
        assert "ACQUIRED_AND_BUILT" in statuses
        assert "ACQUIRING_IN_CLOUD" in statuses
        loser = r0["result"] if r0["backend_calls"] == 0 else r1["result"]
        assert loser.get("deduplicated") is True

        # Part B: Default 900s lease TTL, stale/crashed lease recovery, conditional PUT RELEASED, missing ETag fail-closed
        store = R2CloudStateStore(R2Config(base_url, "cba-kb-private", "SYN_K", "SYN_S"))
        t0 = datetime(2026, 11, 24, 13, 0, 0, tzinfo=timezone.utc)
        ok_def, lock_def = store.acquire_active_job_lock(
            "2026-2027",
            "默认租约球员",
            job_id="job_def_ttl",
            owner_id="owner_def_ttl",
            now_utc=t0,
        )
        assert ok_def is True
        dt_created = datetime.fromisoformat(lock_def["created_at_utc"].replace("Z", "+00:00"))
        dt_expires = datetime.fromisoformat(lock_def["expires_at_utc"].replace("Z", "+00:00"))
        assert (dt_expires - dt_created).total_seconds() == 900.0

        ok1, lock1 = store.acquire_active_job_lock(
            "2026-2027",
            "租约恢复球员",
            job_id="job_crashed_1",
            owner_id="owner_crashed_1",
            lock_ttl_seconds=60.0,
            now_utc=t0,
        )
        assert ok1 is True and lock1["owner_id"] == "owner_crashed_1"

        # Before expiry -> blocked
        ok_mid, cur_mid = store.acquire_active_job_lock(
            "2026-2027",
            "租约恢复球员",
            job_id="job_early_2",
            owner_id="owner_early_2",
            lock_ttl_seconds=60.0,
            now_utc=t0 + timedelta(seconds=30),
        )
        assert ok_mid is False and cur_mid["job_id"] == "job_crashed_1"

        # After expiry -> atomically recovered by new owner
        ok_rec, lock2 = store.acquire_active_job_lock(
            "2026-2027",
            "租约恢复球员",
            job_id="job_recovered_2",
            owner_id="owner_recovered_2",
            lock_ttl_seconds=60.0,
            now_utc=t0 + timedelta(seconds=120),
        )
        assert ok_rec is True and lock2["job_id"] == "job_recovered_2" and lock2["owner_id"] == "owner_recovered_2"

        # Old crashed owner cannot release new owner's lock
        assert (
            store.release_active_job_lock(
                "2026-2027",
                "租约恢复球员",
                owner_id="owner_crashed_1",
                job_id="job_crashed_1",
            )
            is False
        )
        # New owner releases cleanly via conditional PUT (status="RELEASED", never DELETE If-Match)
        assert (
            store.release_active_job_lock(
                "2026-2027",
                "租约恢复球员",
                owner_id="owner_recovered_2",
                job_id="job_recovered_2",
            )
            is True
        )
        lock_key = store._lock_key("租约恢复球员", "2026-2027")
        released_doc = store.get_json(lock_key)
        assert released_doc is not None and released_doc.get("status") == "RELEASED"
        assert store.find_active_job_for_person("租约恢复球员", "2026-2027") is None

        # Subsequent acquisition takes over RELEASED lock cleanly via conditional PUT If-Match
        ok_takeover, lock3 = store.acquire_active_job_lock(
            "2026-2027",
            "租约恢复球员",
            job_id="job_after_release_3",
            owner_id="owner_after_release_3",
            now_utc=t0 + timedelta(seconds=130),
        )
        assert ok_takeover is True and lock3["job_id"] == "job_after_release_3"

        # Missing ETag on GET must fail closed on release_active_job_lock (never unconditionally delete)
        raw_bytes, raw_meta, _ = transport.objects[lock_key]
        transport.objects[lock_key] = (raw_bytes, raw_meta, "")
        assert (
            store.release_active_job_lock(
                "2026-2027",
                "租约恢复球员",
                owner_id="owner_after_release_3",
                job_id="job_after_release_3",
            )
            is False
        )
        assert lock_key in transport.objects
    finally:
        server.shutdown()
        server.server_close()

    # Part C: Worker conditional put (onlyIf: { etagDoesNotMatch: "*" }) concurrent race in Node
    node_bin = shutil.which("node")
    if node_bin is not None:
        worker_path = REPO_ROOT / "config" / "cloud_snowball_worker.js"
        js_race = r"""
const crypto = require('crypto');
const { pathToFileURL } = require('url');
(async () => {
  const mod = await import(pathToFileURL(process.argv[1]).href);
  const worker = mod.default;
  const secret = 'syn_race_' + 'tok_v230';
  const authHeader = 'Bear' + 'er ' + secret;
  const bucketMap = new Map();
  let etagSeq = 0;
  let dispatchCount = 0;
  const origFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    dispatchCount += 1;
    return new Response(null, { status: 204 });
  };
  const makeObj = (rawStr, meta, etag) => ({
    etag,
    async arrayBuffer() { const b = Buffer.from(rawStr, 'utf8'); return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength); },
    async text() { return rawStr; },
    async json() { return JSON.parse(rawStr); },
    customMetadata: meta || {},
  });
  const fakeBucket = {
    async get(key) { return bucketMap.has(key) ? bucketMap.get(key) : null; },
    async put(key, value, opts) {
      const onlyIf = opts && opts.onlyIf;
      if (onlyIf && onlyIf.etagDoesNotMatch === '*' && bucketMap.has(key)) return null;
      if (onlyIf && onlyIf.etagMatches && (!bucketMap.has(key) || bucketMap.get(key).etag !== onlyIf.etagMatches)) return null;
      etagSeq += 1;
      const etag = 'etag-' + etagSeq;
      const str = typeof value === 'string' ? value : Buffer.from(value).toString('utf8');
      const obj = makeObj(str, (opts && opts.customMetadata) || {}, etag);
      bucketMap.set(key, obj);
      return obj;
    },
  };
  const env = {
    CBA_KB_R2: fakeBucket,
    GATEWAY_AUTH_SECRET: secret,
    GITHUB_DISPATCH_REPO: 'SkyOneBranch/syn-private-collector',
    GITHUB_DISPATCH_PAT: 'syn_pat_placeholder',
    WAIT_TIMEOUT_MS: '50',
    POLL_INTERVAL_MS: '25',
  };
  const mkReq = () => new Request('https://worker.example.invalid/v1/person', {
    method: 'POST',
    headers: { Authorization: authHeader, 'Content-Type': 'application/json' },
    body: JSON.stringify({ person: 'Worker竞态球员', season: '2026-2027' }),
  });
  const [r1, r2] = await Promise.all([worker.fetch(mkReq(), env), worker.fetch(mkReq(), env)]);
  const [j1, j2] = await Promise.all([r1.json(), r2.json()]);
  globalThis.fetch = origFetch;
  if (dispatchCount !== 1) throw new Error('EXPECTED_1_DISPATCH_GOT_' + dispatchCount);
  if (j1.job_id !== j2.job_id) throw new Error('JOB_ID_MISMATCH:' + JSON.stringify({ j1, j2 }));
  if (!(j1.deduplicated || j2.deduplicated)) throw new Error('NO_DEDUP_FLAG');
  // Verify Worker default lock lease TTL is 900s (> 10min Runner timeout)
  const lockEntry = Array.from(bucketMap.entries()).find(([k]) => k.startsWith('locks/'));
  if (!lockEntry) throw new Error('MISSING_WORKER_LOCK');
  const lockJson = await lockEntry[1].json();
  const ttlSec = Math.round((Date.parse(lockJson.expires_at_utc) - Date.parse(lockJson.created_at_utc)) / 1000);
  if (ttlSec !== 900) throw new Error('EXPECTED_900S_WORKER_TTL_GOT_' + ttlSec);
  console.log('WORKER_CONDITIONAL_RACE_OK');
})().catch((e) => { console.error(e); process.exit(1); });
"""
        proc = subprocess.run(
            [node_bin, "-e", js_race, str(worker_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert proc.returncode == 0, f"Worker race test failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
        assert "WORKER_CONDITIONAL_RACE_OK" in proc.stdout


# ---------------------------------------------------------------------------
# 4. P0-4-WORKER-R2-CONTRACT-DRIFT (Unicode Parity + Embedded job.card_markdown Tamper + 5-Domain Coverage)
# ---------------------------------------------------------------------------


def test_worker_js_key_parity_unicode_embedded_card_tamper_and_five_domain_coverage():
    from cba_kb.cloud_state import R2CloudStateStore, R2Config

    worker_path = REPO_ROOT / "config" / "cloud_snowball_worker.js"
    py_store = R2CloudStateStore(
        R2Config("https://syn-r2.example.invalid", "cba-kb-private", "SYN_K", "SYN_S"),
        transport=_AtomicFakeR2Transport(),
    )
    py_card_key = py_store._card_key("2026-2027", "测试球员甲")
    py_ascii_key = py_store._card_key("2026-2027", "SYN_P001")
    py_nel_key = py_store._card_key("2026-2027", "\u0085A\u0085")
    py_fs_key = py_store._card_key("2026-2027", "\u001cA\u001f")
    py_bom_key = py_store._card_key("2026-2027", "\ufeffA\ufeff")
    assert py_nel_key == py_fs_key == py_bom_key == "cards/2026-2027/a.json"

    node_bin = shutil.which("node")
    if node_bin is None:
        worker_src = worker_path.read_text(encoding="utf-8")
        assert "crypto.subtle.digest" in worker_src
        assert "R2_OBJECT_SHA256_MISMATCH" in worker_src
        assert "\\u0085" in worker_src
        assert "\\ufeff" in worker_src.lower()
        return

    js_driver = r"""
const crypto = require('crypto');
const { pathToFileURL } = require('url');

(async () => {
  const mod = await import(pathToFileURL(process.argv[1]).href);
  const worker = mod.default;
  const [pyZhKey, pyAsciiKey, pyNelKey, pyFsKey, pyBomKey] = process.argv.slice(2, 7);

  const secret = 'syn_worker_' + 'secret_v230';
  const authHeader = 'Bear' + 'er ' + secret;
  const sha256 = (s) => crypto.createHash('sha256').update(s, 'utf8').digest('hex');

  const bucketMap = new Map();
  const makeObj = (rawStr, meta) => ({
    etag: 'e-' + sha256(rawStr).slice(0, 8),
    async arrayBuffer() { const b = Buffer.from(rawStr, 'utf8'); return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength); },
    async text() { return rawStr; },
    async json() { return JSON.parse(rawStr); },
    customMetadata: meta || {},
  });
  const fakeBucket = {
    async get(key) { return bucketMap.has(key) ? bucketMap.get(key) : null; },
    async put(key, value, opts) {
      const onlyIf = opts && opts.onlyIf;
      if (onlyIf && onlyIf.etagDoesNotMatch === '*' && bucketMap.has(key)) return null;
      if (onlyIf && onlyIf.etagMatches && (!bucketMap.has(key) || bucketMap.get(key).etag !== onlyIf.etagMatches)) return null;
      const str = typeof value === 'string' ? value : Buffer.from(value).toString('utf8');
      const obj = makeObj(str, (opts && opts.customMetadata) || {});
      bucketMap.set(key, obj);
      return obj;
    },
  };
  const env = { CBA_KB_R2: fakeBucket, GATEWAY_AUTH_SECRET: secret, WAIT_TIMEOUT_MS: '50', POLL_INTERVAL_MS: '10' };

  const md = '# 人物研究卡｜测试球员甲（SYN_P001）｜2026-2027｜CURRENT\n\nend_marker: true\n';
  const mdSha = sha256(md);
  const putCard = (key, name) => {
    const payload = JSON.stringify({
      player_name: name, season: '2026-2027', markdown: md, card_markdown: md,
      card_sha256: mdSha, updated_at_utc: new Date().toISOString(),
    });
    bucketMap.set(key, makeObj(payload, { sha256: sha256(payload) }));
  };

  // 1. Key parity across Chinese, ASCII, U+0085 (NEL), U+001C/U+001F, and U+FEFF (BOM)
  putCard(pyZhKey, '测试球员甲');
  putCard(pyAsciiKey, 'SYN_P001');
  putCard(pyNelKey, '\u0085A\u0085');
  putCard(pyFsKey, '\u001cA\u001f');
  putCard(pyBomKey, '\ufeffA\ufeff');

  for (const person of ['测试球员甲', 'SYN_P001', '\u0085A\u0085', '\u001cA\u001f', '\ufeffA\ufeff']) {
    const resp = await worker.fetch(new Request('https://worker.example.invalid/v1/person', {
      method: 'POST',
      headers: { Authorization: authHeader, 'Content-Type': 'application/json' },
      body: JSON.stringify({ person, season: '2026-2027' }),
    }), env);
    const body = await resp.json();
    if (resp.status !== 200 || body.status !== 'HIT_FRESH') {
      throw new Error('KEY_PARITY_FAILED_FOR_' + JSON.stringify(person) + ':' + JSON.stringify(body));
    }
  }

  // 1b. Missing or malformed R2 customMetadata.sha256 MUST fail closed with 500 R2_OBJECT_SHA256_MISMATCH
  const validCardJson = JSON.stringify({
    player_name: '测试球员甲', season: '2026-2027', markdown: md, card_markdown: md,
    card_sha256: mdSha, updated_at_utc: new Date().toISOString(),
  });
  for (const badMeta of [{}, { sha256: 'not-a-valid-64-hex' }]) {
    bucketMap.set(pyZhKey, makeObj(validCardJson, badMeta));
    const badMetaResp = await worker.fetch(new Request('https://worker.example.invalid/v1/person', {
      method: 'POST',
      headers: { Authorization: authHeader, 'Content-Type': 'application/json' },
      body: JSON.stringify({ person: '测试球员甲', season: '2026-2027' }),
    }), env);
    const badMetaBody = await badMetaResp.json();
    if (badMetaResp.status !== 500 || badMetaBody.error !== 'R2_OBJECT_SHA256_MISMATCH') {
      throw new Error('BAD_META_NOT_REJECTED:' + badMetaResp.status + ':' + JSON.stringify(badMetaBody));
    }
  }
  putCard(pyZhKey, '测试球员甲');

  // 2. Embedded tampered job.card_markdown in jobs/<id>.json MUST fail closed on GET /v1/jobs/<id>
  const tamperedJob = JSON.stringify({
    job_id: 'job_tamper_embed',
    status: 'COMPLETED',
    person: '测试球员甲',
    season: '2026-2027',
    card_markdown: md + '\nTAMPERED_DIRECTLY',
    card_sha256: mdSha,
  });
  bucketMap.set('jobs/job_tamper_embed.json', makeObj(tamperedJob, { sha256: sha256(tamperedJob) }));
  const jobResp = await worker.fetch(new Request('https://worker.example.invalid/v1/jobs/job_tamper_embed', {
    method: 'GET',
    headers: { Authorization: authHeader },
  }), env);
  if (jobResp.status < 400) {
    throw new Error('EMBEDDED_JOB_CARD_TAMPER_NOT_REJECTED:' + jobResp.status);
  }

  // 3. Embedded tampered job.card_markdown MUST ALSO fail closed during POST /v1/person poll completion
  const lockKey = 'locks/2026-2027/syn_poll_tamper.json';
  const lockPayload = JSON.stringify({
    job_id: 'job_tamper_embed',
    owner_id: 'owner_t1',
    status: 'RUNNING',
    person: 'syn_poll_tamper',
    season: '2026-2027',
    created_at_utc: new Date().toISOString(),
    expires_at_utc: new Date(Date.now() + 300000).toISOString(),
  });
  bucketMap.set(lockKey, makeObj(lockPayload, { sha256: sha256(lockPayload) }));
  const pollResp = await worker.fetch(new Request('https://worker.example.invalid/v1/person', {
    method: 'POST',
    headers: { Authorization: authHeader, 'Content-Type': 'application/json' },
    body: JSON.stringify({ person: 'syn_poll_tamper', season: '2026-2027' }),
  }), env);
  if (pollResp.status < 400) {
    throw new Error('POLL_EMBEDDED_CARD_TAMPER_NOT_REJECTED:' + pollResp.status);
  }

  // 4. 5-domain coverage evaluation ignores forged overall_status="COMPLETE"
  const ledgerPayload = JSON.stringify({
    schema: 'cba-kb.coverage-ledger.v1',
    overall_status: 'COMPLETE',
    generated_at_utc: new Date().toISOString(),
    sources: [
      { source_id: 'dom', domain: 'domestic_registration', status: 'PARTIAL', observed_until: new Date().toISOString(), missing: ['SYN_CLUB_20'] },
      { source_id: 'for', domain: 'foreign_registration', status: 'COMPLETE', observed_until: new Date().toISOString(), missing: [] },
      { source_id: 'ann', domain: 'club_announcements', status: 'COMPLETE', observed_until: new Date().toISOString(), missing: [] },
      { source_id: 'rul', domain: 'league_rules', status: 'COMPLETE', observed_until: new Date().toISOString(), missing: [] },
      { source_id: 'gam', domain: 'game_stats', status: 'COMPLETE', observed_until: '2020-01-01T00:00:00Z', missing: [] },
    ],
  });
  bucketMap.set('coverage_ledger.json', makeObj(ledgerPayload, { sha256: sha256(ledgerPayload) }));
  const covResp = await worker.fetch(new Request('https://worker.example.invalid/v1/coverage', {
    method: 'GET',
    headers: { Authorization: authHeader },
  }), env);
  const covJson = await covResp.json();
  if (covResp.status !== 200 || covJson.overall_status !== 'PARTIAL' || covJson.domains.game_stats.coverage_status !== 'STALE') {
    throw new Error('FIVE_DOMAIN_COVERAGE_FAILED:' + JSON.stringify(covJson));
  }

  console.log('WORKER_JS_PARITY_AND_TAMPER_OK');
})().catch((e) => { console.error(e); process.exit(1); });
"""
    proc = subprocess.run(
        [node_bin, "-e", js_driver, str(worker_path), py_card_key, py_ascii_key, py_nel_key, py_fs_key, py_bom_key],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert proc.returncode == 0, f"Node Worker check failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    assert "WORKER_JS_PARITY_AND_TAMPER_OK" in proc.stdout


# ---------------------------------------------------------------------------
# 5. P0-4-RUNNER-NONFUNCTIONAL & P0-4-NEW-R2-RUNNER-LOCK-OWNER-MISMATCH
# ---------------------------------------------------------------------------


def test_worker_to_runner_cli_job_id_and_owner_id_handoff_releases_lock(tmp_path: Path, monkeypatch, capsys):
    from cba_kb import cloud_state
    from cba_kb.cli import main as cli_main

    # 1. R2Config.from_env accepts all 3 endpoint aliases
    for env_key in ("CBA_KB_R2_ENDPOINT_URL", "CBA_KB_R2_ENDPOINT", "R2_ENDPOINT_URL"):
        cfg = cloud_state.R2Config.from_env({
            env_key: "https://syn-r2.example.invalid",
            "CBA_KB_R2_BUCKET": "cba-kb-private",
            "CBA_KB_R2_ACCESS_KEY_ID": "SYN_K",
            "CBA_KB_R2_SECRET_ACCESS_KEY": "SYN_S",
        })
        assert cfg.endpoint_url == "https://syn-r2.example.invalid"

    # 2. Runner workflow template wires CBA_KB_R2_ENDPOINT_URL, --job-id, --owner-id, --instance-root, --collector-cmd
    runner_text = (REPO_ROOT / "config" / "cloud_snowball_runner.example.yml").read_text(encoding="utf-8")
    for required_tok in ("CBA_KB_R2_ENDPOINT_URL", "--job-id", "--owner-id", "--instance-root", "--collector-cmd"):
        assert required_tok in runner_text, f"Missing {required_tok} in cloud_snowball_runner.example.yml"
    parsed_runner = yaml.safe_load(runner_text)
    assert isinstance(parsed_runner, dict) and "jobs" in parsed_runner

    # 3. Pre-create Worker-format RUNNING lock + job with owner_id, invoke Runner CLI, verify COMPLETED & lock RELEASED
    transport = _AtomicFakeR2Transport()
    fake_store = cloud_state.R2CloudStateStore(
        cloud_state.R2Config("https://syn-r2.example.invalid", "cba-kb-private", "SYN_K", "SYN_S"),
        transport=transport,
    )
    now_utc = datetime.now(timezone.utc)
    fake_store.acquire_active_job_lock(
        "2026-2027",
        "测试外援交接",
        job_id="job_worker_handoff_42",
        owner_id="owner_worker_42",
        lock_ttl_seconds=300.0,
        now_utc=now_utc,
    )
    fake_store.put_job(
        "job_worker_handoff_42",
        {
            "job_id": "job_worker_handoff_42",
            "owner_id": "owner_worker_42",
            "status": "RUNNING",
            "person": "测试外援交接",
            "season": "2026-2027",
            "created_at_utc": now_utc.isoformat(),
        },
    )

    inst_dir = tmp_path / "syn_instance"
    (inst_dir / "data").mkdir(parents=True)
    (inst_dir / "config").mkdir(parents=True)
    (inst_dir / "data" / "MASTER.csv").write_text(
        "season,club,player_name,player_id,position\n"
        "2026-2027,测试俱乐部甲,测试外援交接,SYN_HANDOFF01,G\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(cloud_state, "R2CloudStateStore", lambda *a, **kw: fake_store)
    monkeypatch.setattr(
        cloud_state.R2Config,
        "from_env",
        classmethod(lambda cls, env=None: fake_store.config),
    )
    monkeypatch.delenv("CBA_KB_DRIVE_SNOWBALL_FOLDER_ID", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cba-kb",
            "--instance-root",
            str(inst_dir),
            "snowball-query",
            "--person",
            "测试外援交接",
            "--season",
            "2026-2027",
            "--job-id",
            "job_worker_handoff_42",
            "--owner-id",
            "owner_worker_42",
        ],
    )
    cli_main()
    out_json = json.loads(capsys.readouterr().out)
    assert out_json["status"] == "ACQUIRED_AND_BUILT"
    assert out_json["job_id"] == "job_worker_handoff_42"
    job_doc = fake_store.get_job("job_worker_handoff_42")
    assert job_doc is not None and job_doc["status"] == "COMPLETED"
    assert job_doc.get("card_sha256")
    # Lock MUST be cleanly released so subsequent force_refresh starts a fresh job
    assert fake_store.find_active_job_for_person("测试外援交接", "2026-2027") is None

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cba-kb",
            "--instance-root",
            str(inst_dir),
            "snowball-query",
            "--person",
            "测试外援交接",
            "--season",
            "2026-2027",
            "--force-refresh",
        ],
    )
    cli_main()
    refresh_json = json.loads(capsys.readouterr().out)
    assert refresh_json["status"] == "ACQUIRED_AND_BUILT"
    assert refresh_json["job_id"] != "job_worker_handoff_42"


def test_real_collector_cmd_execution_for_uncached_player_not_in_master(tmp_path: Path, monkeypatch, capsys):
    from cba_kb import cloud_state
    from cba_kb.cli import main as cli_main

    transport = _AtomicFakeR2Transport()
    fake_store = cloud_state.R2CloudStateStore(
        cloud_state.R2Config("https://syn-r2.example.invalid", "cba-kb-private", "SYN_K", "SYN_S"),
        transport=transport,
    )
    inst_dir = tmp_path / "collector_inst"
    (inst_dir / "data").mkdir(parents=True)
    (inst_dir / "config").mkdir(parents=True)
    # MASTER.csv intentionally does NOT contain "测试新外援采集"
    master_csv = inst_dir / "data" / "MASTER.csv"
    master_csv.write_text(
        "season,club,player_name,player_id,position\n"
        "2026-2027,测试俱乐部甲,既有球员甲,SYN_EXIST01,G\n",
        encoding="utf-8",
    )

    # Executable fake COL collector script that populates MASTER.csv with the requested person
    collector_script = tmp_path / "fake_col.py"
    collector_script.write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "person = '测试新外援采集'\n"
        "for i, arg in enumerate(sys.argv):\n"
        "    if arg == '--person' and i + 1 < len(sys.argv):\n"
        "        person = sys.argv[i + 1]\n"
        f"master = Path({str(master_csv)!r})\n"
        "with master.open('a', encoding='utf-8') as fh:\n"
        "    fh.write(f'2026-2027,测试俱乐部乙,{person},SYN_COL99,F\\n')\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(cloud_state, "R2CloudStateStore", lambda *a, **kw: fake_store)
    monkeypatch.setattr(
        cloud_state.R2Config,
        "from_env",
        classmethod(lambda cls, env=None: fake_store.config),
    )
    monkeypatch.delenv("CBA_KB_DRIVE_SNOWBALL_FOLDER_ID", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cba-kb",
            "--instance-root",
            str(inst_dir),
            "snowball-query",
            "--person",
            "测试新外援采集",
            "--season",
            "2026-2027",
            "--collector-cmd",
            f"{sys.executable} {collector_script}",
        ],
    )
    cli_main()
    out_json = json.loads(capsys.readouterr().out)
    assert out_json["status"] == "ACQUIRED_AND_BUILT"
    assert "测试新外援采集" in out_json["card_markdown"]
    assert fake_store.get_card("2026-2027", "测试新外援采集") is not None


# ---------------------------------------------------------------------------
# 6. P0-5-SYMLINK-PARENT-ESCAPE (openat O_DIRECTORY | O_NOFOLLOW + TOCTOU Swap)
# ---------------------------------------------------------------------------


def test_prepare_v215_rejects_symlink_on_demand_cards_directory_and_toctou_swap(tmp_path: Path, monkeypatch):
    from cba_kb.orchestration.release import prepare_v215

    outside_dir = tmp_path / "outside_secret_dir"
    outside_dir.mkdir()
    card_filename = "研究卡_逃逸测试_SYN_ESC01_2026-2027_CURRENT.md"
    card_bytes = b"# Synthetic Escape Card\n\nend_marker: true\n"
    card_sha = hashlib.sha256(card_bytes).hexdigest()
    (outside_dir / card_filename).write_bytes(card_bytes)
    (outside_dir / "on_demand_cards_manifest.json").write_text(
        json.dumps({
            "schema_version": 1,
            "cards": {
                "SYN_ESC01": {
                    "player_uid": "SYN_ESC01",
                    "player_id": "SYN_ESC01",
                    "player_name": "逃逸测试",
                    "season": "2026-2027",
                    "filename": card_filename,
                    "card_sha256": card_sha,
                }
            },
        }),
        encoding="utf-8",
    )

    class _FakeInstance:
        def __init__(self, root: Path):
            self.root = root
            self.data_root = root / "data"
            self.config_root = root / "config"

    class _MinimalSnapshotDrive:
        def __init__(self):
            self._blobs = {
                "drv_master_csv": b"season,club,player_name\n2026-2027,SYN_CLUB_01,SYN_P01\n",
                "drv_sr_csv": b"source_id,source_title\nsrc_01,Synthetic Source\n",
            }

        def meta(self, file_id: str) -> Dict[str, Any]:
            return {"id": file_id, "mimeType": "text/csv", "name": f"{file_id}.csv"}

        def get(self, file_id: str) -> bytes:
            return self._blobs[file_id]

        def _download(self, file_id: str) -> bytes:
            return self._blobs[file_id]

    manifest_rows = [
        {"logical_key": "derived/MASTER.csv", "drive_file_id": "drv_master_csv"},
        {"logical_key": "derived/source_registry.csv", "drive_file_id": "drv_sr_csv"},
    ]

    # Case A: Static symlink on_demand_cards -> outside_dir rejected
    inst_a = tmp_path / "inst_a"
    (inst_a / "data").mkdir(parents=True)
    os.symlink(outside_dir, inst_a / "data" / "on_demand_cards")
    with pytest.raises(prepare_v215.ProjectionError, match="ON_DEMAND_CARD_SYMLINK_OR_PATH_ESCAPE"):
        prepare_v215._build_instance_consumer_targets(
            _MinimalSnapshotDrive(),
            _FakeInstance(inst_a),
            release_id="v2.3.0-1",
            engine_sha=BASELINE_SHA_V230_1,
            manifest_rows=manifest_rows,
            data_parent="drv_zone_current_20data",
        )

    # Case B: TOCTOU symlink swap right before opening on_demand_cards via openat(O_DIRECTORY | O_NOFOLLOW)
    inst_b = tmp_path / "inst_b"
    od_b = inst_b / "data" / "on_demand_cards"
    od_b.mkdir(parents=True)
    orig_os_open = os.open
    swapped = {"done": False}

    def _toctou_open(path, flags, *args, **kwargs):
        if not swapped["done"] and str(path) == "on_demand_cards" and kwargs.get("dir_fd") is not None:
            swapped["done"] = True
            shutil.rmtree(od_b)
            os.symlink(outside_dir, od_b)
        return orig_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", _toctou_open)
    with pytest.raises(prepare_v215.ProjectionError, match="ON_DEMAND_CARD_SYMLINK_OR_PATH_ESCAPE"):
        prepare_v215._build_instance_consumer_targets(
            _MinimalSnapshotDrive(),
            _FakeInstance(inst_b),
            release_id="v2.3.0-1",
            engine_sha=BASELINE_SHA_V230_1,
            manifest_rows=manifest_rows,
            data_parent="drv_zone_current_20data",
        )
    assert swapped["done"] is True
