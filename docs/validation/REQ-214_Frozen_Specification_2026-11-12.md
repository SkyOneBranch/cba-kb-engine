# REQ-214 Consolidated Effective Specification (v2.1.4 — Deterministic Substrate, Search/Lock Concurrency & CI/Test Parity Hardening)

- **Requirement ID**: `REQ-214`
- **Target Version**: `v2.1.4` (pure technical optimization & hardening; zero new product features; `pyproject.toml` `[project].version` remains `"2.0.2"` per public readiness contract)
- **Baseline HEAD SHA**: `be172d49d2b23a7be4a0c858315480b3e101cc9e` (`v2.1.3`)
- **Governance**: LACD v1.0.2 (`pull_request_required=true`, `human_merge_required=true`, `bind_to_commit_sha=true`, `fail_closed=true`)
- **Authorization**: Scheme B (`P0-1 + P0-2 + P1-1 + P1-2`, single T2 review path via Bot Broker `status:hold` -> Human review & merge) confirmed by Human on 2026-11-12.
- **Debt & Scope Boundary**:
  - Zero new product features, zero external contract/CLI/error-code changes, zero digest changes.
  - Pre-existing `tests/**/*.py` files remain untouched (zero edits/deletions/renames).
  - `REQ-212` open obligations (`#235`, `#237`) and `v2.1.3` post-launch observation items remain independent and are not repackaged under `REQ-214`.

## 1. Core Principles

- `Statement != Fact. Evidence != Fact. Unknown != 0.`
- **Behavior-Preserving Hardening**: Every performance, concurrency, durability, and configuration fix in `v2.1.4` must preserve exact deterministic outputs, SHA-256 digests, and fail-closed rejection semantics across all existing contracts.
- **Single-Source Deterministic Substrate**: Eliminate silent environment divergence between local `pytest`/`Makefile` and hosted CI, close POSIX crash-durability and advisory-lock race windows, and remove redundant $O(K \cdot N)$, $O(|V|)$, and $O(N)$ re-scans in hot verification/query paths without weakening any security or integrity check.

## 2. Frozen Acceptance Criteria (AC-214)

- **AC-214-P0-1 (Local vs CI Pythonpath Parity)**:
  - `pyproject.toml` `[tool.pytest.ini_options].pythonpath` includes `"."` alongside `"src"` and `"legacy"` while preserving `[project].version == "2.0.2"`.
  - `Makefile` `PYTHONPATH` includes `.` alongside `src` and `legacy` (`export PYTHONPATH := .:src:legacy`).
  - Running `python -m pytest` without an externally injected `PYTHONPATH` environment variable resolves repository-root modules (`automation.*`, `scripts.*`) cleanly.

- **AC-214-P0-2 (Crash-Durable Atomic Write & Single-Pass Slice Catalog)**:
  - **AC-214-P0-2a (`common.atomic` Parent Directory `fsync`)**: `cba_kb.common.atomic(path, data)` flushes and `fsync`s the temporary file, atomically replaces `path` via `os.replace`, and `fsync`s `path.parent` directory descriptor (best-effort on platforms/filesystems without directory `fsync` support) so directory-entry metadata is crash-durable. `drive_resilience._wal_write()` delegates to `common.atomic()` instead of maintaining a duplicate atomic-write implementation.
  - **AC-214-P0-2b (`kc_slices.build_slice_catalog` Single-Pass Validation)**: `cba_kb.kc_slices.build_slice_catalog()` validates canonical `(provider, competition)` records in a single pass ($O(N + K)$ rather than $O(K \cdot N)$), calling `validate_stats_record` at most once per matching record while producing output byte-for-byte identical to repeated `build_slice()` calls and preserving `INVALID_SCOPE`, `TEAM_SPLIT_REQUIRES_TEAM_ID`, `PLAYER_NOT_AUTHORIZED`, `MULTIPLE_RECORDS_FOR_SLICE_KEY`, and corrupt-record fail-closed rejections.

- **AC-214-P1-1 (Canonical JSON Consolidation, Zero-Deepcopy Stage Validation, Scoped Rate Limiter & $O(1)$ Append Secret Scan)**:
  - **AC-214-P1-1a (`common` Canonical JSON Helpers & `kc_current` Validation Binding)**:
    - `cba_kb.common` provides `canonical_json_str(value)`, `canonical_json_bytes(value, *, trailing_newline=False)`, and `sha256_hex(data)`, delegated to by `evidence_ledger.canonical_bytes`, `kc_projection.canonical_json`, `ingestion.canonical_bytes`, and `document_lane.canonical_bytes` with zero digest drift.
    - `SimulatedCurrentStore.validate()` binds canonical snapshot bytes (`_canonical_state(snap)`) directly without calling `copy.deepcopy(snap)`, while `commit()` still rejects any post-`validate()` in-place mutation (`COMMIT_VALIDATION_STALE`), registry replacement, or post-`commit()` tampering.
  - **AC-214-P1-1b (`identity_web_evidence` Isolated Rate-Limit State & `RuntimeEvidenceLedger.append` Single-Scan)**:
    - `fetch_official_resource()` accepts optional keyword-only `rate_limit_state: dict[str, float] | None = None`; when `None`, rate-limiting state is scoped to that call (eliminating cross-call/cross-test global `_DOMAIN_LAST_REQUEST` bleed while still throttling same-domain redirects/retries within a call, and supporting explicit shared `rate_limit_state` across calls when desired).
    - `RuntimeEvidenceLedger.entries(*, scan_secrets: bool = True)` reads each entry file once instead of twice; `append()` scans the newly appended entry via `security().scan_bytes` once and verifies historical hash-chain & filename continuity via `entries(scan_secrets=False)` (reducing per-append secret scans from $N + 1$ to $1$), while default `entries()` and `verify()` continue to run full secret scanning (`scan_secrets=True`) across all entries.

- **AC-214-P1-2 (Search Index $O(|Q|)$ Lookup, Locked Persistent Revocation, Non-Truncating `file_lock` & PR CI Deduplication)**:
  - **AC-214-P1-2a (`drive_search_index` $O(|Q|)$ Matched Terms & Locked `revoke_grant_persistent`)**:
    - `search_index()` computes `matched_terms` for each surviving hit directly from `query_terms` (`sorted(qt for qt in query_terms if fid in terms.get(qt, ()))`) in $O(|Q|)$ lookups without iterating the entire `index["terms"]` vocabulary.
    - `revoke_grant_persistent(index_path, file_id, principal)` serializes its load-modify-save critical section under `drive_resilience.file_lock(index_path)` so concurrent revocations on distinct `file_id`s never suffer read-modify-write lost updates.
  - **AC-214-P1-2b (`drive_resilience.file_lock` Non-Truncating Open & `offline-tests.yml` PR Deduplication)**:
    - `drive_resilience.file_lock()` opens `<path>.lock` in append mode (`"a"`) rather than truncating write mode (`"w"`), matching `common.lock()` and avoiding `O_TRUNC` races under contention.
    - `.github/workflows/offline-tests.yml` skips the duplicate `Offline regression suite` step on `pull_request` events (`if: github.event_name != 'pull_request'`) while `Full pytest suite (evidence parity)` on `pull_request` runs `tests/` and `legacy/` with `CBA_KB_SOURCE_POLICY_JSON` and `--deselect tests/test_prepare_v1_5_2.py::test_v1_5_2_registry_proposal_is_complete`, preserving `pytest.log`, `test_security_guards.py` step-order invariants, and full push-to-`main` coverage.

- **AC-214-REG (Zero Regression & T2 Governance Discipline)**:
  - Zero existing `tests/**/*.py` files deleted, renamed, or modified; test count and assertion count strictly increase; `skips` and `xfails` do not increase.
  - T2 path touches (`pyproject.toml`, `Makefile`, `src/cba_kb/drive_search_index.py`, `src/cba_kb/drive_resilience.py`, `.github/workflows/offline-tests.yml`) route through Bot Broker (`status:hold`) and Human review/merge.
