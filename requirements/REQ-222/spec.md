# REQ-222 (v2.2.2): Collector v0.4.1 Curated <25KB Surface, Dual CSV Split, INDEX Stamp & Proxy-Aware Concurrent Drive Cutover

- **Issue**: `#268`
- **Branch**: `muse/issue-268`
- **Risk Tier**: `T2` (Bot Broker + `status:hold` + Independent Q1+Q2 Review + Human Approval)
- **Baseline**: `origin/main @ 29589e7d4d90a81a6507ecaa1ec73945a5eecfc1`
- **Governance**: LACD v1.0.4 (`Statement != Fact. Evidence != Fact. Unknown != 0.`)
- **Production Drive Isolation**: Zero live Google Drive API write calls during Stage A–C development and PR verification; all verification runs in local synthetic `FakeDrive` sandboxes.

---

## 1. Objective

Deliver the `v2.2.2-1` curated consumer surface and fast concurrent Drive cutover without breaking any `v2.1.5-1` / `v2.2.0-1` / `v2.2.1-1` compatibility or safety invariant (`Statement != Fact. Evidence != Fact. Unknown != 0.`, zero business-fact delta on protected tables, `PlacementEnforcedDrive`, main-thread single-writer serial mutations with `num_retries=0`, and byte-exact rollback):

1. **P0-1 — Curated `<25KB` Collector Digest, Dual Structured CSV Split & `v2.2.2-1` Release Spec**
2. **P0-2 — Deterministic Operational Announcement Denoising Filter & 100% Index Preservation**
3. **P0-3 — `00_entry/INDEX` Top-10-Line Version & Master-Row-Count Stamp**
4. **P0-4 — Proxy-Aware Credential-Free Cross-Origin Redirects (`_unauth_client.trust_env = False` Preserved)**
5. **P0-5 — Level-by-Level BFS Concurrent `_inventory()`, Read-Only Phase Reuse, Post-Mutation Invalidation & `Δ` Entry Prefetch**
6. **P0-6 — Governance Freeze, Differential Falsification, Secret Scan & LACD v1.0.4 Verification**

---

## 2. Frozen Public Contracts & Acceptance Criteria

### 2.1 P0-1: Curated `<25KB` Surface, Dual CSV Split & `v2.2.2-1` Spec (`AC-222-P0-1`)

#### `src/cba_kb/consumer_surface.py`
- New constants:
  - `FOREIGN_REGISTRATION_CSV_FILENAME = "CBA_外籍球员注册官方快照_CURRENT.csv"`
  - `DOMESTIC_REG_DIFF_CSV_FILENAME = "CBA_跨季国内球员注册增量观测_2025-2027_CURRENT.csv"`
  - `COLLECTOR_DIGEST_MAX_BYTES = 25 * 1024`
- New deterministic CSV exporters (UTF-8-SIG encoded, scrubbed via `scrub_raw_drive_ids_for_consumer`, explicitly marking `evidence / candidate_observation` and `Statement != Fact. Evidence != Fact. Unknown != 0.`):
  - `export_foreign_players_registration_csv(*, bundles, known_drive_ids=()) -> bytes`
    - Parses foreign player registration markdown tables from `foreign_registration` bundles into structured rows.
    - Required columns: `layer`, `statement_notice`, `snapshot_title`, `published_at`, `club`, `player_en`, `player_zh`, `nationality`, `position`, `jersey_number`, `doc_id`, `source_tier`, `raw_sha256`.
  - `export_domestic_registration_diff_csv(*, reg_diff, known_drive_ids=()) -> bytes`
    - Exports `reg_diff` (`transferred_players`, `contract_changed_players`, `added_players`, `removed_players`) into structured rows with `change_type` in `{"TRANSFERRED", "CONTRACT_CHANGED", "ADDED", "REMOVED"}`.
    - Required columns: `layer`, `statement_notice`, `change_type`, `player`, `old_club`, `new_club`, `old_contract_type`, `new_contract_type`, `change_details`, `old_snapshot_sha256`, `new_snapshot_sha256`.
- `render_collector_official_digest_md(*, bundles, reg_diff=None, release_id="v2.2.0-1", engine_sha="", known_drive_ids=(), compact_curated=False, max_bytes=COLLECTOR_DIGEST_MAX_BYTES, active_window_start="2025-07-01") -> bytes`:
  - When `compact_curated=False` (default): preserves 100% backward compatibility for `v2.2.0-1` / `v2.2.1-1`.
  - When `compact_curated=True`:
    - Removes duplicate 3-season domestic registration verbatim tables (~75KB) and full foreign/diff tables from the `.md` body, replacing them with summary statistics and explicit routing to `CBA_外籍球员注册官方快照_CURRENT.csv` and `CBA_跨季国内球员注册增量观测_2025-2027_CURRENT.csv`.
    - Applies `filter_curated_operational_announcements(bundles, active_window_start=active_window_start)` to the `.md` announcement section.
    - Discloses in the header: total bundles `N`, admitted `K`, filtered `M`, and notes that 100% of all `N` bundles remain indexed in `CBA_文档索引_脱敏视图_CURRENT.json`.
    - Enforces fail-closed size gate: `len(rendered_bytes) < max_bytes` (default `25 * 1024`), raising `ValueError("COLLECTOR_DIGEST_SIZE_LIMIT_EXCEEDED")` if `len(rendered_bytes) >= max_bytes`.
- `build_consumer_surface_targets_spec(...)`:
  - Accepts optional `foreign_registration_csv_bytes=None`, `domestic_reg_diff_csv_bytes=None`, `include_curated_csvs=None`.
  - Registers:
    - `"consumer/foreign_players_registration_current.csv"` -> `CBA_外籍球员注册官方快照_CURRENT.csv` (`text/csv`, `binary`, `parent_role="data"`)
    - `"consumer/domestic_registration_diff_current.csv"` -> `CBA_跨季国内球员注册增量观测_2025-2027_CURRENT.csv` (`text/csv`, `binary`, `parent_role="data"`)
- `render_consumer_surface_guide_section(...)` and `render_consumer_field_dictionary_md(...)`:
  - Accept optional `include_curated_csvs=None` (auto-enabled for `v2.2.2+`, disabled by default for `v2.1.5-1`, `v2.2.0-1`, `v2.2.1-1`).
  - Document the 2 new `.csv` targets (`evidence / candidate_observation`) and recommend Python/CSV reads.

#### `src/cba_kb/orchestration/release/prepare_v215.py`
- `V222_1_BASELINE_SHA = "29589e7d4d90a81a6507ecaa1ec73945a5eecfc1"`.
- Register `V215_RELEASE_SPECS["v2.2.2-1"]`:
  ```python
  {
      "product_baseline_sha": V222_1_BASELINE_SHA,
      "enable_consumer_surface_v215": True,
      "enable_operational_kb_v220": True,
      "enable_curated_compact_surface_v222": True,
      "incremental_delta_only": True,
      "archive_format": "bundle_v1",
  }
  ```
- Existing `v2.1.5-1`, `v2.2.0-1`, `v2.2.1-1` specs remain 100% unchanged.
- `_build_instance_consumer_targets(...)` prefers `instance.data_root / "ops_sync_v041"` (when `operational_checkpoint.json` exists there) and falls back to `instance.data_root / "ops_sync_v040_1"`.

---

### 2.2 P0-2: Deterministic Operational Announcement Denoising & 100% Index Preservation (`AC-222-P0-2`)

- `filter_curated_operational_announcements(bundles, *, active_window_start="2025-07-01") -> dict`:
  - Returns `{"admitted": [...], "filtered": [...], "total_count": N, "admitted_count": K, "filtered_count": M, "reasons_by_doc_id": {...}}`.
  - Official registration master snapshots (`foreign_registration`, `domestic_registration`) are exempt from denoising and always admitted.
  - Excludes from `.md` digest body:
    1. Official Weibo interaction / entertainment / voting / lottery / solicitation & non-active-roster condolence posts (`每日预言家`, `每日名场面`, `图文征集`, `我和我的篮球`, `有奖互动`, `抽奖`, voting highlights, non-active-competitive `唁电`).
    2. Out-of-window historical pinned posts (`published_at` earlier than `active_window_start`, or missing `published_at` with explicit past tournament summary such as `杭州亚运会` / `亚运会总结`).
  - Admits: official registration/deregistration, club signings/waivers/tryouts, national team & club roster/injury notices, CBA `#判罚解析#`, NBL-CBA rule alignment & league official work meetings.
- `merge_operational_docs_into_consumer_index(...)` retains 100% of all `N` bundles (both admitted and filtered, 0 loss).

---

### 2.3 P0-3: `00_entry/INDEX` Top-10-Line Version & Master-Row-Count Stamp (`AC-222-P0-3`)

- In `prepare_v215._content_preserving_candidate(...)` for `derived/INDEX.md` (`00_entry/INDEX`) and entry control docs (`entry/README`, `entry/context`, `CURRENT_VERSION_DOC`):
  - Stamp within the **top 10 lines**:
    - `当前发布：{release_id}`
    - `代码提交：{engine_sha}`
    - `权威国内注册主表规模：{master_row_count:,} 行（2017-2027 权威总表，精确行数 {master_row_count}）`
  - Preserve `[CBA-KB CURRENT RELEASE BEGIN]` managed wrapper and `[CBA-KB CURRENT STATE BEGIN] ... [CBA-KB CURRENT STATE END]` JSON block so `current_state.validate_current_state()` passes 100%.

---

### 2.4 P0-4: Proxy-Aware Credential-Free Cross-Origin Redirect (`AC-222-P0-4`)

- In `src/cba_kb/drive_http.py`:
  - `_unauth_client.trust_env` MUST remain strictly `False` (frozen test `tests/test_v221_q2_remediation.py` enforces this and tampering raises `UNAUTHENTICATED_CLIENT_NOT_ISOLATED`).
  - Add pure helper `_resolve_sanitized_env_proxies(url: str) -> dict[str, str]`:
    - Reads `HTTPS_PROXY` / `https_proxy`, `HTTP_PROXY` / `http_proxy`, `ALL_PROXY` / `all_proxy`, honoring `NO_PROXY` / `no_proxy`.
    - Fail-closed rejects any selected proxy URL with embedded credentials (`username is not None or password is not None`) or invalid scheme/host, raising `ValueError("UNSAFE_PROXY_URL: ...")`.
  - On crossed-origin redirects, `RequestsHttp.request()` passes the sanitized `proxies` explicitly to `_unauth_client.request(..., proxies=proxies)` while maintaining 0 `.netrc` reads, 0 `Authorization`, 0 `Proxy-Authorization`, and 0 `Cookie`.

---

### 2.5 P0-5: Concurrent BFS `_inventory()`, Read-Only Reuse, Post-Mutation Invalidation & Delta Prefetch (`AC-222-P0-5`)

- In `src/cba_kb/release.py` and `src/cba_kb/orchestration/release/prepare_v215.py`:
  - `_inventory(drive, zones)`:
    - Level-by-level BFS calling `map_readonly(drive, drive.list, sorted_frontier, max_workers=8)` on each deduplicated, sorted frontier level.
    - Deterministic output identical to serial traversal; never crawls `history`, `staging`, or `NON_CURRENT` folders.
  - Read-only phase reuse & strict post-mutation invalidation:
    - Reuse a single `_inventory()` result within purely read-only phases (`prepare` / `freeze_plan`, and pre-write `publish` `preflight`).
    - NEVER reuse pre-write inventory cache across any Drive write or move mutation (`PUBLISHING`, `write_payload`, `relocate`, `set_status`); refresh a fresh `_inventory()` after mutations (which may be shared between post-write read-only `verify` + `validate_closure`).
  - Concurrent read-only prefetch of `Δ entries`:
    - `prepare()` uses `map_readonly(..., max_workers=8)` to prefetch remote `meta` + `snapshot` for `Δ entries`, keeping `clean()`, hash checks, `atomic()`, and plan/journal persistence 100% single-threaded on the main thread.
    - `verify()` uses `map_readonly(..., max_workers=8)` for fallback content readback of `Δ entries` without valid server `sha256Checksum`, keeping `clean()` and validation deterministic on the main thread.
    - All Drive write/move operations remain 100% main-thread serial (`num_retries=0`).

---

## 3. Non-Goals & Hard Guardrails

- `scripts/prepare_production.py`: **0 bytes modified**.
- `pyproject.toml` (remains `"2.0.2"`), `requirements.lock`, `.github/**`: **0 bytes modified**.
- Existing `tests/**/*.py` and `legacy/**`: **0 modified, 0 deleted, 0 new skip/xfail**.
- Zero local home-directory paths and zero raw Google Drive IDs in repository artifacts.
- Zero live Google Drive API mutations in Stage A–C.
