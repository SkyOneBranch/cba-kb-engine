# REQ-231 (v2.3.1): Cloud Bundle Snapshot & Cache-First Short-Circuit, Contract-Rich Person Cards, Archive Cold Compaction & Foreign Rowspan/Mention Fixes

- **Issue**: `#276`
- **Branch**: `muse/issue-276`
- **Risk Tier**: `T2` (Bot Broker + `status:hold` + Independent Q1+Q2 Review + Human Approval)
- **Baseline**: `origin/main @ 7afb18b1e867146ed99cc73fa16e53d990c54e95` (Release `v2.3.0`)
- **Governance**: LACD v1.0.4 (`Statement != Fact. Evidence != Fact. Unknown != 0. Observed-until != Now; Silence != Absence.`)
- **Production Isolation (Stage A–C)**: Zero live Google Drive `Drive A` writes, zero live Backblaze B2 / Cloudflare R2 writes, zero cross-repo edits to `cba-kb-collector`.
- **Clean-Room & Frozen-File Guardrails**:
  - `G5 == 0` (zero real Google Drive IDs, zero local absolute paths such as `<local-home>/...`, zero real private corpus).
  - `G6 == 0` (zero real single-game boxscores, zero real foreign overseas resumes; 100% synthetic fixtures).
  - 0-byte frozen files: `scripts/prepare_production.py`, `pyproject.toml` (`"2.0.2"`), `requirements.lock`, `.github/**`, `legacy/**`, and all pre-existing `tests/**/*.py`.

---

## 1. Objective

Resolve the four concrete bottlenecks and quality defects surfaced by the `v2.3.0-test` blind end-to-end evaluation in a single cohesive engine release (`cba-kb-engine v2.3.1`), while maintaining 100% backward compatibility with `v2.1.5-1..v2.3.0-1`:

1. **P0-1** — Single-bundle cloud state snapshot (`cloud_instance_bundle.tar.gz`), substrate allowlist slimming, cache-first lazy pull (`--lazy-pull-bundle`, 0 pulls on hit, `<= 5` object-store requests on cold path), and S3/Backblaze B2 compatibility (`R2_ENDPOINT`, opt-in `allow_s3_501_lock_fallback`).
2. **P0-2** — Contract-rich person research cards (current season contract & registration block + salary non-disclosure + up to 3-season `Cross-Season Registration History` + full foreign registration block + explicit single-game stats gap disclaimer) and consumer filenames using club name with zero `unassigned` substring.
3. **P0-3** — Controlled `Drive A` `90_archive_历史归档` cold archive compactor (`src/cba_kb/archive_compactor.py` + `cba-kb compact-drive-archive`) with `--plan-only` default, fail-closed active-overlap lock, remote `sha256Checksum` verification before trash-only (`trashed=True`, never `delete()`), plus automatic legacy `_unassigned_` snowball card retirement on new card upsert.
4. **P0-4** — Foreign registration `rowspan` continuation row alignment & secondary-header filtering with fail-closed orphan detection, `< 25 KB` compact per-club foreign quick index in `v2.3.1+` digest, and deterministic homonym-safe `enrich_bundle_matched_persons` across `title + body`.
5. **P0-5** — `V215_RELEASE_SPECS["v2.3.1-1"]` registration bound to baseline `7afb18b1e867146ed99cc73fa16e53d990c54e95` with `enable_v231_surface_enhancements: True` and zero regression on `v2.1.5-1..v2.3.0-1`.
6. **P0-6** — Stage D production cutover runbook (executed ONLY after PR merge, Release `v2.3.1` RRC/PRV PASS, and explicit Human `Cloud & Production GO`).

---

## 2. Frozen Public Contracts & Acceptance Criteria

### 2.1 P0-1: Single-Bundle Cloud State, Cache-First Lazy Pull & S3/B2 Compatibility (`AC-231-P0-1`)

#### `src/cba_kb/cloud_state.py`
- Constants:
  - `DEFAULT_INSTANCE_BUNDLE_KEY = "cloud_instance/cloud_instance_bundle.tar.gz"`
  - `DEFAULT_MAX_BUNDLE_COMPRESSED_BYTES` (positive int, e.g. `32 * 1024 * 1024`)
  - `DEFAULT_MAX_BUNDLE_EXTRACTED_BYTES` (positive int, e.g. `128 * 1024 * 1024`)
  - `DEFAULT_MAX_BUNDLE_MEMBERS` (positive int, e.g. `5000`)
- `R2Config.from_env(environ=None)`:
  - Resolves endpoint URL from `CBA_KB_R2_ENDPOINT_URL`, `CBA_KB_R2_ENDPOINT`, `R2_ENDPOINT_URL`, **`R2_ENDPOINT`** (new fallback), then `CBA_KB_R2_ACCOUNT_ID` / `R2_ACCOUNT_ID`.
- `R2CloudStateStore.__init__(config, *, session=None, transport=None, timeout_seconds=30.0, allow_s3_501_lock_fallback: Optional[bool] = None)`:
  - When `allow_s3_501_lock_fallback` is `None`, reads `CBA_KB_S3_LOCK_501_FALLBACK` (`"1"` / `"true"` / `"yes"` -> `True`, otherwise `False`).
  - Default is strictly `False` (Fail-Closed): if S3/B2 returns HTTP `501` on conditional `PUT` (`If-None-Match` / `If-Match`), `acquire_active_job_lock` and `release_active_job_lock` raise `RuntimeError("R2_LOCK_PUT_FAILED:501:...")` / `RuntimeError("R2_LOCK_RELEASE_FAILED:501:...")`.
  - When `allow_s3_501_lock_fallback=True`, HTTP `501` on conditional lock `PUT` safely falls back to an unconditional `PUT` that still sends and verifies `x-amz-meta-sha256`.
- `R2CloudStateStore.push_instance_bundle(local_dir, *, key=DEFAULT_INSTANCE_BUNDLE_KEY, max_compressed_bytes=DEFAULT_MAX_BUNDLE_COMPRESSED_BYTES) -> Dict[str, Any]`:
  - **Substrate Allowlist**: packs ONLY structured files required for headless card building and observation horizon calculation:
    - Registration & stats tables: `CONSUMER_MASTER_FILENAME`, `MASTER.csv`, `FOREIGN_REGISTRATION_CSV_FILENAME`, `DOMESTIC_REG_DIFF_CSV_FILENAME`, `season_stats*`, `game_logs/*.json*` (`.json` / `.jsonl`)
    - Identity table: `identity_registry.json`
    - Checkpoints, ledgers & curation config: `operational_checkpoint.json`, `coverage_ledger.json`, `negative_receipts.jsonl`, `snowball_receipts.jsonl`, `on_demand_cards_manifest.json`, `curation_profile.yaml`
    - Generated cards: `cards/*.md` and `on_demand_cards/*.md`
  - **Explicit Exclusions** (never packed):
    - `document_lane/archive/**/raw/*` (and any raw archive blobs)
    - `evidence/**`
    - `inbox/**`
    - `.credentials/**`
    - `config/production.json` (and any `production.json`)
  - Runs `current_state.clean(data, rel)` on every included member prior to archiving; fails closed on any secret or raw Drive ID.
  - Produces a **byte-deterministic** `.tar.gz`: sorted POSIX relative paths, `mtime=0`, `uid=0`, `gid=0`, `uname=""`, `gname=""`, `mode=0o644`, `gzip.GzipFile(filename="", mode="wb", mtime=0)`.
  - Uploads via **exactly 1 `PUT`** (`put_bytes(key, payload, content_type="application/gzip")`) with `x-amz-meta-sha256`.
  - Returns `{"key", "sha256", "size_bytes", "member_count", "members", "excluded_count"}`.
- `R2CloudStateStore.pull_instance_bundle(local_dir, *, key=DEFAULT_INSTANCE_BUNDLE_KEY, expected_sha256=None, max_compressed_bytes=DEFAULT_MAX_BUNDLE_COMPRESSED_BYTES, max_extracted_bytes=DEFAULT_MAX_BUNDLE_EXTRACTED_BYTES, max_members=DEFAULT_MAX_BUNDLE_MEMBERS) -> Dict[str, Any]`:
  - Fetches via **exactly 1 `GET`** (`get_bytes(key, expected_sha256=expected_sha256)`), enforcing `x-amz-meta-sha256` integrity. Raises `FileNotFoundError("R2_BUNDLE_NOT_FOUND:...")` if absent.
  - Fails closed before writing any file on:
    - compressed size `> max_compressed_bytes` (`ValueError("R2_BUNDLE_COMPRESSED_SIZE_EXCEEDED")`)
    - member count `> max_members` (`ValueError("R2_BUNDLE_MAX_MEMBERS_EXCEEDED")`)
    - cumulative extracted size `> max_extracted_bytes` (`ValueError("R2_BUNDLE_EXTRACTED_SIZE_EXCEEDED")`)
    - absolute paths, `..` traversal, backslashes, NUL bytes (`ValueError("R2_BUNDLE_PATH_TRAVERSAL")`)
    - symlinks, hardlinks, FIFOs, device nodes (`ValueError("R2_BUNDLE_UNSAFE_MEMBER_TYPE")`)
  - Atomically writes verified regular files to `local_dir` and returns `{"key", "sha256", "size_bytes", "extracted_count", "files"}`.

#### `src/cba_kb/snowball_orchestrator.py`
- `SnowballOrchestrator.__init__(..., ensure_local_instance_fn=None, on_state_mutated_fn=None)`:
  - **Cache-First Fast Path**: when `force_refresh=False`, checks remote card cache (`get_card`) and active negative receipts (`get_negative_receipts`) BEFORE invoking `ensure_local_instance_fn`. On `HIT_FRESH` or `UNKNOWN_NEGATIVE_CACHED`, `ensure_local_instance_fn` is called **0 times** (0 bundle downloads).
  - **Lazy Slow Path**: invokes `ensure_local_instance_fn()` ONLY on cache miss or `force_refresh=True` before running `acquisition_backend.acquire_person(...)`.
  - **State Write-Back**: invokes `on_state_mutated_fn(result)` ONLY when acquisition completes with substantive state mutation (`ACQUIRED_AND_BUILT` or `UNKNOWN_AFTER_WHITELIST_CHECK`).
  - Cold-path object-store request budget: single-bundle lazy pull + card/job/lock/bundle push keeps object-store I/O minimal (total bundle + cache/receipt/state requests `<= 5` for bundle-backed cold acquisition excluding optional lock/job bookkeeping, and strictly `<= 5` total object-store requests in direct bundle-only cold pull/push flows; specifically for a cold query with minimal store calls: card GET + negative_receipts GET + bundle GET + card PUT + bundle PUT `<= 5` when lock/job are not separately counted, or with `--lazy-pull-bundle` fast path `<= 2` GETs and `0` bundle pulls).

#### `src/cba_kb/cli.py` & `config/cloud_snowball_runner.example.yml`
- `cba-kb cloud-state-push`: defaults to `push_instance_bundle` (`--bundle-key`, default `cloud_instance/cloud_instance_bundle.tar.gz`), preserving `--legacy-multifile` for `push_instance_snapshot`.
- `cba-kb cloud-state-pull`: defaults to `pull_instance_bundle` (`--bundle-key`), preserving `--legacy-multifile` for `pull_instance_snapshot`.
- `cba-kb snowball-query`: adds `--lazy-pull-bundle` (and `--bundle-key`) wiring `ensure_local_instance_fn` and `on_state_mutated_fn`.
- `config/cloud_snowball_runner.example.yml` demonstrates `--lazy-pull-bundle` (no unconditional pre-step `cloud-state-pull`) and documents `CBA_KB_S3_LOCK_501_FALLBACK`.

---

### 2.2 P0-2: Contract-Rich Person Cards, Foreign Registration Block & Zero `unassigned` Filenames (`AC-231-P0-2`)

#### `src/cba_kb/person_card_builder.py`
- **Zero `unassigned` Consumer Filename**:
  - `build_person_card()` derives the filename middle segment in strict priority order:
    1. Sanitized current club name (`current_club`)
    2. Non-empty `player_id` (not `"unassigned"`)
    3. Non-empty `player_uid` (not `"unassigned"`)
    4. Literal `"待核俱乐部"`
  - Calls `kc_render.research_card_consumer_filename(player_name, middle_segment, season)` producing `研究卡_<姓名>_<俱乐部>_<赛季>_CURRENT.md`.
  - **Invariant**: `"unassigned"` (case-insensitive) MUST NEVER appear in any consumer card filename returned by `build_person_card()`.
  - `kc_render.research_card_consumer_filename(player_name, player_id, season)` preserves its exact 3-argument signature for 100% compatibility with frozen tests.
- **Rich `registration_profile` Population**:
  - Domestic records (`person["domestic_records"]`):
    - Populates `registration_profile["domestic_current"]` from target `season` (or latest available season descending if target season is absent) containing `season`, `club_official`, `registration_stage`, `registration_method`, `contract_category`, `contract_term_official`, `former_club`, `registration_status`, `remarks`.
    - Populates `registration_profile["domestic_history"]` with up to 3 most recent seasons sorted descending by `season`.
  - Foreign records (`person["foreign_records"]`):
    - Populates `registration_profile["foreign_registration"]` with `club`, `player_en`, `player_zh`, `nationality`, `position`, `jersey_number`, `published_at`, `doc_id`, `raw_sha256`.
- **Single-Game Stats Gap Disclosure**:
  - `build_person_card(..., game_stats_degraded: bool = False)` accepts optional `game_stats_degraded: bool = False`.
  - When `matched_games` is empty, populates `payload["single_game_stats_gap"] = {"available": False, "degraded": bool(game_stats_degraded)}`.

#### `src/cba_kb/kc_render.py`
- `render_research_card(payload, *, evidence_verified=False, max_external_citations_per_card=10)`:
  - **Domestic Contract & Cross-Season Block**: when `registration_profile.get("domestic_current")` is present, deterministically renders:
    - `Current Club`, `Registration Stage`, `Registration Method`, `Contract Category` (`未公示 (UNKNOWN)` if empty), `Official Contract Term / Expiry` (`未公示 (UNKNOWN)` if empty), `Former Club` (when non-empty), `Registration Status`, `Remarks` (when non-empty), and fixed anti-hallucination line:
      `- Official Salary Disclosure: 官方仅公示合同类别与期限，个人实领薪资未公开 (Unknown != 0)`
  - When `registration_profile.get("domestic_history")` is present, renders `### Cross-Season Registration History` with at most 3 seasons in descending order (`season | club_official | contract_category | contract_term_official | registration_method | former_club`).
  - **Foreign Registration Block**: when `registration_profile.get("foreign_registration")` is present, deterministically renders:
    - `Current Club`, `English Name`, `Chinese Name`, `Nationality`, `Position`, `Jersey Number`, `Official Snapshot Published At`, `Snapshot doc_id`, `Snapshot raw_sha256`, and `- Data Layer: evidence / candidate_observation (Statement != Fact)`.
  - **Single-Game Gap Disclaimer**: when `payload.get("single_game_stats_gap")` has `available is False` (and `recent_games_summary` is absent), renders:
    - `- Recent Single-Game Stats: NOT_AVAILABLE_IN_CURRENT_SNAPSHOT (Unknown != 0; 严禁凭外部记忆编造单场得分)` (appending cloud single-game source degraded notice when `degraded is True`).
  - **100% Backward Compatibility**: when called with a minimal v2.2.3/v2.3.0 `registration_profile` (without `domestic_current`, `domestic_history`, `foreign_registration`, or `single_game_stats_gap`), output is byte-identical to v2.3.0.

---

### 2.3 P0-3: Drive A `90_archive` Cold Archive Compactor & Legacy `_unassigned_` Card Retirement (`AC-231-P0-3`)

#### `src/cba_kb/archive_compactor.py` (new module)
- Constants & Exceptions:
  - `DEFAULT_PROTECTED_BUNDLE_RELEASES = ("v2.2.1-1", "v2.2.2-1", "v2.3.0-1")`
  - `ArchiveCompactionError(RuntimeError)`
- `compact_drive_archive(drive, *, history_folder_id: str, active_asset_ids: Sequence[str] = (), protected_folder_ids: Sequence[str] = (), previous_release_id: Optional[str] = None, keep_release_dirs: Sequence[str] = DEFAULT_PROTECTED_BUNDLE_RELEASES, execute: bool = False, tag: str = "v231", release_status: Optional[Mapping[str, Any]] = None, manifest_rows: Optional[Sequence[Mapping[str, Any]]] = None, production_policy: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]`:
  - **Read-Only Default (`execute=False`)**: performs 0 remote write/upload/update/trash calls; returns full compaction plan.
  - **Scan & Skip Rules**:
    - Traverses `history_folder_id`, collecting loose non-compressed files and legacy pre-`bundle_v1` subdirectories (such as `v1.5.0`–`v2.2.0-1` containing `before_*` / `candidate_*`).
    - Skips already-compressed `.zip` and `.tar.gz` files.
    - Skips protected recent `bundle_v1` directories (`keep_release_dirs` + `previous_release_id`).
  - **Triple Fail-Closed Safety Locks**:
    1. **Zero Active/Protected Overlap**: intersects all candidate file/folder IDs against `active_asset_ids`, `protected_folder_ids`, `release_status` artifacts, `manifest_rows` IDs, and `production_policy` targets & zones (`current`, `evidence`, `staging`). Any overlap raises `ArchiveCompactionError("ARCHIVE_COMPACT_ACTIVE_OVERLAP:...")` with **0 writes**.
    2. **Deterministic Pack + Remote `sha256Checksum` Verification**: builds byte-deterministic `cold_archive_legacy_releases_<tag>.tar.gz` containing all packed files plus internal `archive_index.json` (`logical_path`, `sha256`, `modifiedTime`, etc. — no plaintext index left on Drive), uploads to `history_folder_id`, and reads back remote metadata verifying `sha256Checksum == local_sha256` before trashing anything (mismatch raises `ArchiveCompactionError("ARCHIVE_COMPACT_REMOTE_SHA256_MISMATCH")` with **0 trashes**).
    3. **Trash-Only (Never Hard Delete)**: moves archived source files/directories to Trash ONLY via `trashed=True` (`drive.trash(fid)` or `files().update(fileId=fid, body={"trashed": True})`). **Code MUST NEVER call `files().delete()`**.

#### `src/cba_kb/drive.py` & `src/cba_kb/cli.py`
- `Drive.trash(file_id)`: main-thread-only writer enforcing `_assert_main_thread_writer`, setting `body={'trashed': True}` via `files().update(...)`.
- `upsert_snowball_card_to_drive(...)`: after writing `研究卡_<姓名>_<俱乐部>_<赛季>_CURRENT.md` (non-`unassigned`), detects any legacy `研究卡_<姓名>_unassigned_<赛季>_CURRENT.md` in the same folder and trashes it (`trashed=True`, never `delete()`), reporting `retired_legacy_files` in the receipt.
- CLI subcommands: `cba-kb compact-drive-archive` (default `--plan-only`, explicit `--execute`) and `cba-kb refresh-snowball-cards`.

---

### 2.4 P0-4: Foreign Registration `rowspan` Fix, `< 25 KB` Foreign Quick Index & Homonym-Safe Multi-Person Enrichment (`AC-231-P0-4`)

#### `src/cba_kb/consumer_surface.py`
- `_parse_foreign_registration_rows(bundle)`:
  - Explicitly skips secondary header rows (`cells[0] == "英文名"` or `cells[1] == "中文名"`).
  - Maintains `current_club` state machine:
    - Valid 6-column row starting with club name updates `current_club = cells[0]`.
    - Continuation `rowspan` row (6 cells with `cells[5] == ""` and Latin player name in `cells[0]`, or 5 cells with Latin player name in `cells[0]`) inherits `club = current_club` and aligns `cells[0..4]` to `(player_en, player_zh, nationality, position, jersey_number)`.
    - **Fail-Closed**: if a continuation row appears before any `current_club` is established, raises `ValueError("FOREIGN_ROW_ORPHAN_CONTINUATION")`.
  - Preserves 100% compatibility with standard 6-column tables (including `test_v222` fixtures).
- `render_collector_official_digest_md(..., include_foreign_quick_index: bool = False)`:
  - When `compact_curated=True` and `include_foreign_quick_index=True` (enabled for `v2.3.1+`), renders a compact sorted per-club foreign quick index in Section 1 (`- **<club>（<N>人）**: <player_zh> / <player_en> (#<jersey> <pos>) · ...`).
  - Strictly preserves `len(rendered_bytes) < COLLECTOR_DIGEST_MAX_BYTES` (`25 * 1024`) even at 20 clubs / 60+ foreign players.
- `enrich_bundle_matched_persons(bundle, *, roster_candidates) -> Dict[str, Any]`:
  - Pure deterministic function scanning `title + "\n" + body`.
  - Appends all unambiguous matched player names (length `>= 2`, deduplicated preserving order) to `matched_persons`.
  - **Homonym Safety (Fail-Closed)**: when a name belongs to `>= 2` distinct candidates (e.g. `乔文瀚` across two clubs), refuses to tag/claim it unless uniquely disambiguated by an adjacent club qualifier in the text (e.g. `乔文瀚（合成俱乐部甲）`).
  - Wired into `operational_sync`, `consumer_surface` (`render_collector_official_digest_md`, `merge_operational_docs_into_consumer_index`), and `person_card_builder._match_document_bundles()`.

---

### 2.5 P0-5: `v2.3.1-1` Release Spec Registration & Zero Historical Regression (`AC-231-P0-5`)

#### `src/cba_kb/orchestration/release/prepare_v215.py`
- `V231_1_BASELINE_SHA = "7afb18b1e867146ed99cc73fa16e53d990c54e95"`
- Register `"v2.3.1-1"` in `V215_RELEASE_SPECS`:
  - `product_baseline_sha`: `"7afb18b1e867146ed99cc73fa16e53d990c54e95"`
  - `enable_consumer_surface_v215`: `True`
  - `enable_operational_kb_v220`: `True`
  - `enable_curated_compact_surface_v222`: `True`
  - `enable_observation_horizon_v230`: `True`
  - `enable_on_demand_cards_v230`: `True`
  - `enable_v231_surface_enhancements`: `True`
  - `incremental_delta_only`: `True`
  - `archive_format`: `"bundle_v1"`
- Existing `v2.1.5-1`, `v2.2.0-1`, `v2.2.1-1`, `v2.2.2-1`, `v2.3.0-1` specs remain untouched (`enable_v231_surface_enhancements` absent/falsy).

---

### 2.6 P0-6: Stage D Production Cutover (Post-Merge & Human `Cloud & Production GO` Only)

Executed ONLY after PR merge, Release `v2.3.1` RRC/PRV PASS, and explicit Human `Cloud & Production GO`:
1. **D1**: Run `cba-kb compact-drive-archive --plan-only`, verify zero active overlap, then `--execute` to cold-pack legacy `90_archive_历史归档` loose files and `v1.5.0`–`v2.2.0-1` directories into SHA-256-verified `.tar.gz` and trash originals (`trashed=True`).
2. **D2**: Run `Drive A` `v2.3.1-1` `O(Δ)` 5-step cutover (`project -> reserve-staging -> freeze -> publish -> verify`), confirming `release_status.json == "v2.3.1-1" / "COMPLETE"` and `109/109` protected tables zero drift.
3. **D3**: Switch cloud object storage to `cloud_instance/cloud_instance_bundle.tar.gz`, refresh existing snowball cards (retiring legacy `_unassigned_` copies to trash), and build remaining 5 cards verifying `<= 5` object-store requests and `< 30s` latency.
4. **D4**: Targeted regression verification, sanitized receipt closeout on GitHub Issue `#276`, and Notion `v2.3.1` slots `01/02/05/06/07/08`, directory matrix, and North Star update.
