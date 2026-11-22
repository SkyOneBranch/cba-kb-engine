# REQ-221 (v2.2.1): Deterministic O(Δ) Incremental Drive Release, Server-Hash Verification & Single-Bundle Archive

- **Issue**: `#262`
- **Branch**: `muse/issue-262`
- **Risk Tier**: `T2` (Bot Broker + `status:hold` + Independent Q1+Q2 Review + Human Approval)
- **Baseline**: `origin/main @ e58a2787a6176397a142abced682eed5d10f1289`
- **Governance**: LACD v1.0.4 (`Statement != Fact. Evidence != Fact. Unknown != 0.`)
- **Production Drive Isolation**: Zero live Google Drive API write calls during development and PR verification; all verification runs in a local synthetic `FakeDrive` sandbox.

---

## 1. Objective

Eliminate the four systemic performance bottlenecks and archive file-explosion in the Google Drive release pipeline without weakening any existing safety invariant (`Statement != Fact`, zero business-fact delta on protected tables, `PlacementEnforcedDrive`, main-thread single-writer serial mutations with `num_retries=0`, and byte-exact rollback):

1. **P0-1 — Long-lived `RequestsHttp(AuthorizedSession)` Transport & Bounded Read-Only Concurrency**
2. **P0-2 — Server-Native `sha256Checksum` Exposure & Batch Zero-Drift Verification**
3. **P0-3 — True `O(Δ)` Incremental Release Planning & Four-Contract Decoupling**
4. **P0-4 — Deterministic Single-Bundle Archive (`bundle_v1`) & Dual-Path Secure Restore**
5. **P0-5 — Sanitized API Call Telemetry & 550-File Scale End-to-End Benchmark**

---

## 2. Frozen Public Contracts & Acceptance Criteria

### 2.1 P0-1: Transport Pool, Thread-Isolated Read Concurrency & Single-Writer Lock (`AC-221-P0-1`)

#### `src/cba_kb/drive_http.py` (new module, zero new dependencies)
- Uses only locked dependencies (`google.auth.transport.requests.AuthorizedSession`, `requests.adapters.HTTPAdapter`, stdlib).
- Constants:
  - `DRIVE_TRANSPORT = "requests"`
  - `DEFAULT_TIMEOUT_SEC = 300`
  - `DEFAULT_POOL_CONNECTIONS = 16`
  - `DEFAULT_POOL_MAXSIZE = 16`
- `RequestsHttpResponse(dict)`:
  - Lowercased header keys, `.status: int`, `.reason: str` (satisfies `httplib2.Response` contract).
- `RequestsHttp(session, timeout=DEFAULT_TIMEOUT_SEC)`:
  - `request(uri, method="GET", body=None, headers=None, **kwargs) -> (RequestsHttpResponse, bytes)`.
  - Never follows HTTP `308` resumable upload responses (returns `308` directly even if `Location` is present).
  - Follows bounded `301/302/303/307` redirects while stripping `Authorization`, `Proxy-Authorization`, and `Cookie` headers on cross-host hops.
  - Maps `requests.exceptions.SSLError -> ssl.SSLError`, `requests.exceptions.Timeout` (including `ConnectTimeout`/`ReadTimeout`) `-> socket.timeout`, `requests.exceptions.ConnectionError -> ConnectionError`, all recognized by `transport.transient()`.
  - `close()` closes the underlying session cleanly.
- `build_authorized_session(creds, *, pool_connections=16, pool_maxsize=16)`:
  - Rejects service-account credentials fail-closed (`RuntimeError` containing `SERVICE_ACCOUNT_CREDENTIALS_FORBIDDEN`).
  - Mounts `HTTPAdapter(pool_connections=pool_connections, pool_maxsize=pool_maxsize)` on `https://` and `http://`.
- `build_requests_service(service_name, version, creds, *, pool_connections=16, pool_maxsize=16, timeout=DEFAULT_TIMEOUT_SEC)`:
  - Builds a `googleapiclient` service backed by `RequestsHttp(build_authorized_session(...))`.

#### `src/cba_kb/transport.py`
- `READ_RETRY_STATS` access (`retry_read`, `read_retry_stats`, `reset_read_retry_stats`) is protected by a `threading.Lock` so concurrent worker retries lose zero increments.
- `TRANSIENT_TYPES` includes `requests.exceptions.Timeout`, `requests.exceptions.SSLError`, `requests.exceptions.ConnectionError` when importable.

#### `src/cba_kb/drive.py`
- `MAX_READ_WORKERS = 8`.
- `Drive._build_clients()` builds Drive v3 and Docs v1 clients via `build_requests_service` (replacing `httplib2`).
- Worker threads executing read-only operations obtain an isolated per-thread client pair via `threading.local()`, never sharing `googleapiclient.discovery.Resource` across threads.
- `_reset_read_connections()` on a worker thread resets only that thread's local client and never closes the main thread's `self.api` / `self.docs`.
- `Drive.map_readonly(fn, items, *, max_workers=8)` and module-level `map_readonly(drive, fn, items, *, max_workers=8)`:
  - Preserves exact input order.
  - Enforces concurrency bound via `threading.BoundedSemaphore(max_workers)` with `1 <= max_workers <= MAX_READ_WORKERS` (fail-closed `ValueError` outside bounds).
  - Closes worker thread-local clients upon completion (no socket leaks).
- All write methods (`put`, `put_managed_doc`, `move`, `ensure`, `ensure_copy`) enforce main-thread-only serial execution (`num_retries=0`), raising `RuntimeError("WRITE_OFF_MAIN_THREAD_FORBIDDEN:...")` if invoked from a non-main thread.

---

### 2.2 P0-2: Server-Native `sha256Checksum` & Batch Zero-Drift Verification (`AC-221-P0-2`)

- `cba_kb.drive.FIELDS` includes `sha256Checksum`.
- `Drive.get_with_meta(file_id, *, known_meta=None)` downloads binary bytes once, eliminates redundant pre-download `meta()` when `known_meta` is supplied, and verifies `digest(data) == meta['sha256Checksum'].lower()` when available (falling back to post-download `meta()` fingerprint verification otherwise, failing closed on any drift).
- `release.verify_remote_objects_batch(drive, expected_items, *, remote_index=None, folders=None, error_cls=RuntimeError, error_code='REMOTE_HASH_MISMATCH')`:
  - Uses batch `drive.list(folder)` (`pageSize=1000`) metadata to verify `id`, `mimeType`, `parents`, and `sha256Checksum` for binary objects with **0 content downloads**.
  - **Fail-Closed Fallback**: Automatically falls back to real content download (`payload`) + `clean()` + `digest()` whenever an object is a Native Doc (`application/vnd.google-apps.*` / `mode == 'managed_doc'`) or lacks a valid 64-hex `sha256Checksum`.
  - Raises `error_cls(f'{error_code}:{fid}')` on any 1-byte mismatch, missing object, or MIME/parent mismatch.
- `verify_protected()` and `validate_closure()`:
  - Reuse a single batch `_inventory()` index to verify all `protected` assets via `sha256Checksum` without re-downloading 109 protected files across 4–5 rounds.
  - Eliminate the redundant per-entry `drive.meta(entry['id'])` loop in `validate_closure()` by reusing the `_inventory()` index (calling `drive.meta()` only for entries not yet in current/evidence inventory, such as staged targets prior to relocation).

---

### 2.3 P0-3: True `O(Δ)` Incremental Release Planning & Four-Contract Decoupling (`AC-221-P0-3`)

- In `src/cba_kb/orchestration/release/prepare_v215.py`:
  - Existing `v2.1.5-1` and `v2.2.0-1` release specs remain unchanged (100% backward-compatible).
  - Register `"v2.2.1-1"` in `V215_RELEASE_SPECS` with:
    - `product_baseline_sha`: `"e58a2787a6176397a142abced682eed5d10f1289"`
    - `enable_consumer_surface_v215`: `True`
    - `enable_operational_kb_v220`: `True`
    - `incremental_delta_only`: `True`
    - `archive_format`: `"bundle_v1"`
  - `freeze_plan(...)` and `_candidate_inputs(...)` also accept explicit `incremental: bool | None = None` and `archive_format: str | None = None`.
  - Under incremental mode (`incremental=True`):
    - `plan['entries']` contains **only `Δ`**: new targets (`NEW_CODE_MIRROR`, `NEW_CONSUMER_SURFACE`), modified code/consumer targets (`CODE_MIRROR_UPDATE`, `CONSUMER_SURFACE_UPDATE`), and required control-plane updates (`CONTROL_METADATA_UPDATE`).
    - All unchanged targets (`unchanged` code/consumer targets, `carried_forward` business/protected targets) remain in `plan['carry_forward_artifacts']` and are excluded from `entries`, `before/*`, `candidate/*`, and `publish` write loops.
    - Every `carry_forward` target is proven zero-drift against remote Drive via `verify_remote_objects_batch` during `freeze_plan`, `publish` preflight, and `verify`; any 1-byte remote drift fails closed immediately.
- Four-Contract Decoupling:
  1. `_build_closure_contract`: resolves the 6 core protected baselines (`input/MASTER.xlsx`, 3 six-tables, `input/source_registry.csv`, `evidence/bayi_legacy_context.md`) from `entries` OR `projection['existing_targets']` verified via remote hash check, and builds `_evidence_baseline` using batch `sha256Checksum` (with fail-closed snapshot fallback for Native Docs or missing checksums).
  2. `validate_release_topology(nodes, *, release_id, planned_target_ids=None, carry_forward_target_ids=None)`: supports validating `active_carry_forward_ids ∪ planned_delta_entry_ids` without weakening duplicate/missing/unexpected/ancestor checks.
  3. `_manifest_candidate`: emits ALL active rows (`carry_forward ∪ Δ entries`), writing newly computed SHA-256 for `Δ` rows and verified `previous_sha256` for `carry_forward` rows.
  4. `set_status`: writes the complete active artifact set (`carry_forward_artifacts ∪ entries` minus `retired_artifacts`) into `release_status.json`, preserving 100% consumer transparency.

---

### 2.4 P0-4: Deterministic Single-Bundle Archive (`bundle_v1`) & Dual-Path Secure Restore (`AC-221-P0-4`)

- `prepare(..., archive_format='legacy')`:
  - Valid values: `'legacy'` (default — 100% unchanged legacy behavior and `plan.json` bytes) and `'bundle_v1'`. Invalid values raise `ValueError('ARCHIVE_FORMAT_INVALID')`.
- `build_release_bundle(root, plan) -> dict`:
  - Packages locally verified `Δ` `before/*`, `candidate/*`, `status.before.json`, `plan.json`, and self-describing `bundle_manifest.json` into a **byte-deterministic** `release_bundle.tar.gz` (lexicographically sorted POSIX paths, `mtime=0, uid=0, gid=0, uname='', gname='', mode=0o644`, `gzip.GzipFile(filename='', mtime=0)`).
  - Scans every uncompressed member and the final archive with `clean()`.
- `archive_snapshot(drive, root, plan)`:
  - Under `archive_format == 'bundle_v1'`:
    - Uploads exactly **1** archive file `release_bundle.tar.gz` (`application/gzip`) under `releases/<release_id>/` via `PlacementEnforcedDrive`.
    - Creates `ensure_copy` native backups only for `managed_doc` entries present in `plan['entries']`.
    - Returns a `previous_snapshot` list satisfying `validate_previous_snapshot(plan, previous)` (native entries point to their native backup `snapshot_id`; binary & protected entries point to `bundle_file_id` with `archive_format='bundle_v1'`, `bundle_file_id`, `bundle_sha256`, and `bundle_member`).
  - Under `archive_format == 'legacy'` (default): preserves exact legacy per-file archival behavior.
- `extract_release_bundle(bundle_bytes, *, max_bytes=MAX_BUNDLE_EXTRACT_BYTES, max_members=MAX_BUNDLE_MEMBERS) -> dict`:
  - Fail-closed guards: rejects compressed/uncompressed sizes exceeding `max_bytes`, member count exceeding `max_members`, non-regular files (symlinks/hardlinks/devices), path traversal (`..`, leading `/`, backslashes, `:` or NUL), duplicate members, missing/invalid `bundle_manifest.json`, and per-member SHA-256/size mismatches; runs `clean()` on extracted bytes.
- `restore(drive, root, single_writer=False, *, bundle_file_id=None, max_bundle_bytes=MAX_BUNDLE_EXTRACT_BYTES)`:
  - Restores from local frozen files when present (preserving exact tamper detection on altered local files).
  - When local `before/*` or `status.before.json` is absent (or `bundle_file_id` is explicitly provided), fetches `release_bundle.tar.gz` from Drive, validates its SHA-256 and extracts safely via `extract_release_bundle`, and restores all mutated targets and `release_status.json` byte-for-byte.

---

### 2.5 P0-5: Sanitized API Call Telemetry & 550-File Scale Benchmark (`AC-221-P0-5`)

- `transport.py` exposes thread-safe sanitized counters:
  - `API_CALL_STATS` keys: `list_calls`, `meta_calls`, `download_calls`, `upload_calls`, `copy_calls`, `move_calls`.
  - Functions: `record_api_call(kind, count=1)`, `api_call_stats()`, `reset_api_call_stats()`.
  - `Drive` methods automatically increment the corresponding counters without recording any file IDs, paths, or payloads.
- Synthetic 550-file full-chain benchmark (`tests/test_v221_incremental_release.py`):
  - 550 active files including 109 protected assets and 6 Native Docs, with 20 files modified/added.
  - Proves: `plan['entries']` is `O(Δ)`, `releases/<id>/` created objects `<= 8`, `download_calls` reduction `>= 85%` vs legacy full-run baseline, 1-byte remote tamper on any `carry_forward` or `protected` asset is 100% blocked fail-closed, `PlacementEnforcedDrive` audits `release_bundle.tar.gz` and native backups, and single-bundle `restore()` recovers 100% of bytes and status.

---

## 3. Non-Goals & Hard Guardrails

- `scripts/prepare_production.py`: **0 bytes modified**.
- `pyproject.toml` (remains `"2.0.2"`), `requirements.lock`, `.github/**`: **0 bytes modified**.
- Existing `tests/**/*.py` and `legacy/**`: **0 modified, 0 deleted, 0 new skip/xfail**.
- No cloud relay VM, no credential export, no home-directory paths, and no real Drive IDs in repository artifacts.
