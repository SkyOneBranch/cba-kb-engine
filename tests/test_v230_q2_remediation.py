"""Q2 Round 1 remediation regression & negative tests for REQ-230 (cba-kb-engine v2.3.0).

Covers all 6 independent Q2 blockers without modifying Commit 1's frozen acceptance test file:
1. P0-2-R2-MISSING-METADATA-SHA: R2CloudStateStore.get_bytes fails closed on missing/malformed x-amz-meta-sha256
2. P0-2-DRIVE-PROTECTED-ANCESTOR: upsert_snowball_card_to_drive rejects subfolder descendants of protected zones
3. P0-3-NONATOMIC-DEDUP-LOCK: concurrent 2-thread barrier test verifies atomic single-job dedup & owner release
4. P0-4-WORKER-R2-CONTRACT-DRIFT: JS/Python key parity on Chinese names, SHA-256 verification, 5-domain coverage
5. P0-4-RUNNER-NONFUNCTIONAL: R2Config.from_env endpoint aliases, CLI backend wiring, and --job-id handoff
6. P0-5-SYMLINK-PARENT-ESCAPE: prepare_v215 rejects symlinked on_demand_cards directory / manifest / card escape
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_SHA_V230_1 = "3a7239361b2b81f763dce3477b3c3ad81f357b90"


class _AtomicFakeR2Transport:
    """Thread-safe in-memory R2 transport honoring conditional If-None-Match: *."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.objects: Dict[str, Tuple[bytes, Dict[str, str]]] = {}

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
                meta = {
                    k: v
                    for k, v in lower_headers.items()
                    if k.startswith("x-amz-meta-")
                }
                self.objects[key] = (bytes(data), meta)
                return 200, {"ETag": hashlib.md5(data).hexdigest()}, b""

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
                payload, meta = self.objects[key]
                return 200, dict(meta), payload

            if method == "DELETE":
                self.objects.pop(key, None)
                return 204, {}, b""

        return 405, {}, b"MethodNotAllowed"


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

    # Case A: Object stored without x-amz-meta-sha256 must fail closed even if expected_sha256=None
    transport.objects["jobs/no_meta.json"] = (b'{"status":"RUNNING"}', {})
    with pytest.raises(ValueError, match="R2_OBJECT_SHA256_MISMATCH"):
        store.get_bytes("jobs/no_meta.json")

    # Case B: Malformed (non-64-hex) x-amz-meta-sha256 must fail closed
    transport.objects["jobs/bad_meta.json"] = (
        b'{"status":"RUNNING"}',
        {"x-amz-meta-sha256": "not-a-valid-sha256"},
    )
    with pytest.raises(ValueError, match="R2_OBJECT_SHA256_MISMATCH"):
        store.get_bytes("jobs/bad_meta.json")

    # Case C: Valid put_bytes sets x-amz-meta-sha256 and reads back cleanly
    payload = b'{"ok":true}'
    store.put_bytes("jobs/good_meta.json", payload)
    assert store.get_bytes("jobs/good_meta.json") == payload


# ---------------------------------------------------------------------------
# 2. P0-2-DRIVE-PROTECTED-ANCESTOR
# ---------------------------------------------------------------------------


def test_drive_snowball_rejects_subfolder_descendant_of_protected_zone():
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

    # Deeply nested child inside zones["current"] must be rejected
    with pytest.raises(ValueError, match="SNOWBALL_FOLDER_IN_PROTECTED_ZONE"):
        upsert_snowball_card_to_drive(
            fake_drive,
            folder_id="drv_nested_child_02",
            filename=filename,
            content_bytes=card_bytes,
            production_policy=production_policy,
        )
    assert fake_drive.create_calls == 0

    # Sibling folder under root outside protected zones succeeds
    res = upsert_snowball_card_to_drive(
        fake_drive,
        folder_id="drv_safe_snowball_25",
        filename=filename,
        content_bytes=card_bytes,
        production_policy=production_policy,
    )
    assert res["action"] == "created"
    assert fake_drive.create_calls == 1


# ---------------------------------------------------------------------------
# 3. P0-3-NONATOMIC-DEDUP-LOCK (Real 2-thread Barrier Concurrency Test)
# ---------------------------------------------------------------------------


def test_orchestrator_concurrent_requests_atomic_dedup_single_job_and_owner_release():
    from cba_kb.cloud_state import R2CloudStateStore, R2Config
    from cba_kb.snowball_orchestrator import SnowballOrchestrator

    now = datetime(2026, 11, 24, 13, 0, 0, tzinfo=timezone.utc)
    transport = _AtomicFakeR2Transport()
    store = R2CloudStateStore(
        R2Config("https://syn-r2.example.invalid", "cba-kb-private", "SYN_K", "SYN_S"),
        transport=transport,
    )

    backend_calls: List[str] = []
    backend_entered = threading.Event()
    release_backend = threading.Event()

    class _BlockingAsyncOrSyncBackend:
        def acquire_person(
            self,
            person: str,
            *,
            season: str,
            job_id: str,
            force_refresh: bool = False,
        ) -> Dict[str, Any]:
            backend_calls.append(job_id)
            backend_entered.set()
            release_backend.wait(timeout=5.0)
            md = f"# 人物研究卡｜{person}（SYN_C01）｜{season}｜CURRENT\n\nend_marker: true\n"
            return {
                "status": "ACQUIRED_AND_BUILT",
                "person": {"player_name": person, "player_id": "SYN_C01"},
                "filename": f"研究卡_{person}_SYN_C01_{season}_CURRENT.md",
                "card_markdown": md,
                "card_sha256": hashlib.sha256(md.encode("utf-8")).hexdigest(),
                "observed_until": "2026-11-24T12:00:00Z",
                "coverage_status": "COMPLETE",
            }

    orch = SnowballOrchestrator(
        state_store=store,
        acquisition_backend=_BlockingAsyncOrSyncBackend(),
        now_fn=lambda: now,
    )

    barrier = threading.Barrier(2)
    results: List[Optional[Dict[str, Any]]] = [None, None]
    errors: List[BaseException] = []

    def _worker(idx: int) -> None:
        try:
            barrier.wait(timeout=5.0)
            results[idx] = orch.get_or_acquire_person("并发原子球员", season="2026-2027")
        except BaseException as exc:  # pragma: no cover - diagnostic
            errors.append(exc)

    t1 = threading.Thread(target=_worker, args=(0,))
    t2 = threading.Thread(target=_worker, args=(1,))
    t1.start()
    t2.start()

    assert backend_entered.wait(timeout=5.0), "Winner thread never entered backend"
    # Give loser thread a moment to hit the lock and return deduplicated ACQUIRING_IN_CLOUD
    time.sleep(0.1)
    release_backend.set()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert not errors, f"Worker thread error: {errors}"
    assert len(backend_calls) == 1, f"Expected exactly 1 backend call, got {backend_calls}"

    statuses = {r["status"] for r in results if r is not None}
    assert "ACQUIRED_AND_BUILT" in statuses
    assert statuses <= {"ACQUIRED_AND_BUILT", "ACQUIRING_IN_CLOUD", "HIT_FRESH"}

    # Verify owner-scoped lock release: non-owner cannot release an active lock
    ok, lock_doc = store.acquire_active_job_lock(
        "2026-2027",
        "锁校验球员",
        job_id="job_owner_a",
        owner_id="owner_a",
    )
    assert ok is True
    assert lock_doc["job_id"] == "job_owner_a"

    # Second acquire returns (False, existing_lock) without overwriting
    ok2, existing = store.acquire_active_job_lock(
        "2026-2027",
        "锁校验球员",
        job_id="job_owner_b",
        owner_id="owner_b",
    )
    assert ok2 is False
    assert existing["job_id"] == "job_owner_a"

    # Wrong owner cannot release
    assert store.release_active_job_lock("2026-2027", "锁校验球员", owner_id="owner_b", job_id="job_owner_b") is False
    assert store.find_active_job_for_person("锁校验球员", "2026-2027") is not None

    # Correct owner releases cleanly
    assert store.release_active_job_lock("2026-2027", "锁校验球员", owner_id="owner_a", job_id="job_owner_a") is True
    assert store.find_active_job_for_person("锁校验球员", "2026-2027") is None


# ---------------------------------------------------------------------------
# 4. P0-4-WORKER-R2-CONTRACT-DRIFT (JS/Python Key Parity, SHA-256, 5-Domain Coverage)
# ---------------------------------------------------------------------------


def test_worker_js_key_parity_sha256_check_and_five_domain_coverage():
    from cba_kb.cloud_state import R2CloudStateStore, R2Config

    worker_path = REPO_ROOT / "config" / "cloud_snowball_worker.js"
    worker_src = worker_path.read_text(encoding="utf-8")

    # Compute expected Python key for Chinese name "测试球员甲"
    py_store = R2CloudStateStore(
        R2Config("https://syn-r2.example.invalid", "cba-kb-private", "SYN_K", "SYN_S"),
        transport=_AtomicFakeR2Transport(),
    )
    py_card_key = py_store._card_key("2026-2027", "测试球员甲")
    py_ascii_key = py_store._card_key("2026-2027", "SYN_P001")

    node_bin = shutil.which("node")
    if node_bin is None:
        # Static contract fallback if node is unavailable
        assert "crypto.subtle.digest" in worker_src
        assert "R2_OBJECT_SHA256_MISMATCH" in worker_src
        for dom in (
            "domestic_registration",
            "foreign_registration",
            "club_announcements",
            "league_rules",
            "game_stats",
        ):
            assert dom in worker_src
        return

    # Execute actual Cloudflare Worker module in Node 24 with an in-memory mock R2Bucket
    js_driver = r"""
const path = require('path');
const crypto = require('crypto');
const { pathToFileURL } = require('url');

(async () => {
  const workerUrl = pathToFileURL(process.argv[1]).href;
  const mod = await import(workerUrl);
  const worker = mod.default;
  const expectedPyZhKey = process.argv[2];
  const expectedPyAsciiKey = process.argv[3];

  const secret = 'syn_worker_' + 'secret_v230';
  const authHeader = 'Bear' + 'er ' + secret;

  const bucketMap = new Map();
  const makeObj = (rawStr, meta) => ({
    async arrayBuffer() {
      const buf = Buffer.from(rawStr, 'utf8');
      return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
    },
    async text() { return rawStr; },
    async json() { return JSON.parse(rawStr); },
    customMetadata: meta || {},
  });

  const fakeBucket = {
    async get(key) {
      return bucketMap.has(key) ? bucketMap.get(key) : null;
    },
    async put(key, value, opts) {
      const str = typeof value === 'string' ? value : Buffer.from(value).toString('utf8');
      bucketMap.set(key, makeObj(str, (opts && opts.customMetadata) || {}));
    },
  };

  const env = { CBA_KB_R2: fakeBucket, GATEWAY_AUTH_SECRET: secret };
  const sha256 = (s) => crypto.createHash('sha256').update(s, 'utf8').digest('hex');

  // 1. Key parity for Chinese name "测试球员甲"
  const md = '# 人物研究卡｜测试球员甲（SYN_P001）｜2026-2027｜CURRENT\n\nend_marker: true\n';
  const mdSha = sha256(md);
  const cardPayload = JSON.stringify({
    player_name: '测试球员甲',
    season: '2026-2027',
    markdown: md,
    card_markdown: md,
    card_sha256: mdSha,
    updated_at_utc: new Date().toISOString(),
    observed_until: new Date().toISOString(),
    coverage_status: 'COMPLETE',
  });
  bucketMap.set(expectedPyZhKey, makeObj(cardPayload, { sha256: sha256(cardPayload) }));

  const hitReq = new Request('https://worker.example.invalid/v1/person', {
    method: 'POST',
    headers: { Authorization: authHeader, 'Content-Type': 'application/json' },
    body: JSON.stringify({ person: '测试球员甲', season: '2026-2027' }),
  });
  const hitResp = await worker.fetch(hitReq, env);
  const hitJson = await hitResp.json();
  if (hitResp.status !== 200 || hitJson.status !== 'HIT_FRESH') {
    throw new Error('ZH_KEY_PARITY_FAILED:' + hitResp.status + ':' + JSON.stringify(hitJson));
  }

  // 2. ASCII key parity
  const asciiPayload = JSON.stringify({
    player_name: 'SYN_P001',
    season: '2026-2027',
    markdown: md,
    card_markdown: md,
    card_sha256: mdSha,
    updated_at_utc: new Date().toISOString(),
  });
  bucketMap.set(expectedPyAsciiKey, makeObj(asciiPayload, { sha256: sha256(asciiPayload) }));
  const asciiResp = await worker.fetch(new Request('https://worker.example.invalid/v1/person', {
    method: 'POST',
    headers: { Authorization: authHeader, 'Content-Type': 'application/json' },
    body: JSON.stringify({ person: 'SYN_P001', season: '2026-2027' }),
  }), env);
  const asciiJson = await asciiResp.json();
  if (asciiResp.status !== 200 || asciiJson.status !== 'HIT_FRESH') {
    throw new Error('ASCII_KEY_PARITY_FAILED:' + JSON.stringify(asciiJson));
  }

  // 3. Tampered object (missing/mismatched metadata sha256) fails closed
  bucketMap.set(expectedPyZhKey, makeObj(cardPayload, {}));
  const badMetaResp = await worker.fetch(new Request('https://worker.example.invalid/v1/person', {
    method: 'POST',
    headers: { Authorization: authHeader, 'Content-Type': 'application/json' },
    body: JSON.stringify({ person: '测试球员甲', season: '2026-2027' }),
  }), env);
  if (badMetaResp.status < 400) {
    throw new Error('MISSING_SHA_METADATA_DID_NOT_FAIL_CLOSED:' + badMetaResp.status);
  }

  // 4. Tampered card_sha256 inside valid outer JSON fails closed
  const badCardPayload = JSON.stringify({
    player_name: '测试球员甲',
    season: '2026-2027',
    markdown: md + '\ntampered',
    card_markdown: md + '\ntampered',
    card_sha256: mdSha,
    updated_at_utc: new Date().toISOString(),
  });
  bucketMap.set(expectedPyZhKey, makeObj(badCardPayload, { sha256: sha256(badCardPayload) }));
  const badCardResp = await worker.fetch(new Request('https://worker.example.invalid/v1/person', {
    method: 'POST',
    headers: { Authorization: authHeader, 'Content-Type': 'application/json' },
    body: JSON.stringify({ person: '测试球员甲', season: '2026-2027' }),
  }), env);
  if (badCardResp.status < 400) {
    throw new Error('TAMPERED_CARD_SHA_DID_NOT_FAIL_CLOSED:' + badCardResp.status);
  }

  // 5. 5-domain coverage evaluation ignores forged ledger.overall_status="COMPLETE" when a domain is PARTIAL/STALE
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
  if (covResp.status !== 200 || covJson.overall_status !== 'PARTIAL') {
    throw new Error('FIVE_DOMAIN_COVERAGE_FAILED:' + JSON.stringify(covJson));
  }
  if (!covJson.domains || covJson.domains.game_stats.coverage_status !== 'STALE') {
    throw new Error('STALE_DOMAIN_NOT_DETECTED:' + JSON.stringify(covJson));
  }

  console.log('WORKER_JS_PARITY_OK');
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        [node_bin, "-e", js_driver, str(worker_path), py_card_key, py_ascii_key],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert proc.returncode == 0, f"Node Worker check failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    assert "WORKER_JS_PARITY_OK" in proc.stdout


# ---------------------------------------------------------------------------
# 5. P0-4-RUNNER-NONFUNCTIONAL (Env Alias, CLI Backend Wiring & --job-id Handoff)
# ---------------------------------------------------------------------------


def test_runner_env_alias_cli_backend_wiring_and_job_id_handoff(tmp_path: Path, monkeypatch, capsys):
    from cba_kb import cloud_state
    from cba_kb.cli import main as cli_main

    # 1. R2Config.from_env accepts CBA_KB_R2_ENDPOINT_URL, CBA_KB_R2_ENDPOINT, and R2_ENDPOINT_URL
    for env_key in ("CBA_KB_R2_ENDPOINT_URL", "CBA_KB_R2_ENDPOINT", "R2_ENDPOINT_URL"):
        cfg = cloud_state.R2Config.from_env({
            env_key: "https://syn-r2.example.invalid",
            "CBA_KB_R2_BUCKET": "cba-kb-private",
            "CBA_KB_R2_ACCESS_KEY_ID": "SYN_K",
            "CBA_KB_R2_SECRET_ACCESS_KEY": "SYN_S",
        })
        assert cfg.endpoint_url == "https://syn-r2.example.invalid"

    # 2. Runner workflow template uses CBA_KB_R2_ENDPOINT_URL and passes --job-id & --instance-root
    runner_text = (REPO_ROOT / "config" / "cloud_snowball_runner.example.yml").read_text(encoding="utf-8")
    assert "CBA_KB_R2_ENDPOINT_URL" in runner_text
    assert "--job-id" in runner_text
    assert "--instance-root" in runner_text
    parsed_runner = yaml.safe_load(runner_text)
    assert isinstance(parsed_runner, dict) and "jobs" in parsed_runner

    # 3. CLI snowball-query wires LocalCliAcquisitionBackend and writes exact jobs/<job_id>.json to R2
    transport = _AtomicFakeR2Transport()
    fake_store = cloud_state.R2CloudStateStore(
        cloud_state.R2Config("https://syn-r2.example.invalid", "cba-kb-private", "SYN_K", "SYN_S"),
        transport=transport,
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
            "job_worker_handoff_99",
        ],
    )
    cli_main()
    out_json = json.loads(capsys.readouterr().out)
    assert out_json["status"] == "ACQUIRED_AND_BUILT"
    assert out_json["job_id"] == "job_worker_handoff_99"
    job_doc = fake_store.get_job("job_worker_handoff_99")
    assert job_doc is not None
    assert job_doc["status"] == "COMPLETED"


# ---------------------------------------------------------------------------
# 6. P0-5-SYMLINK-PARENT-ESCAPE
# ---------------------------------------------------------------------------


def test_prepare_v215_rejects_symlink_on_demand_cards_directory_escape(tmp_path: Path):
    from cba_kb.orchestration.release import prepare_v215

    # Prepare an outside directory containing a valid manifest + card file
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

    data_root = tmp_path / "instance" / "data"
    data_root.mkdir(parents=True)
    # Symlink on_demand_cards -> outside_secret_dir
    os.symlink(outside_dir, data_root / "on_demand_cards")

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

    with pytest.raises(prepare_v215.ProjectionError, match="ON_DEMAND_CARD_SYMLINK_OR_PATH_ESCAPE"):
        prepare_v215._build_instance_consumer_targets(
            _MinimalSnapshotDrive(),
            _FakeInstance(tmp_path / "instance"),
            release_id="v2.3.0-1",
            engine_sha=BASELINE_SHA_V230_1,
            manifest_rows=manifest_rows,
            data_parent="drv_zone_current_20data",
        )
