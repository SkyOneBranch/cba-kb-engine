# REQ-230 (v2.3.0): Pure-Cloud Serverless On-Demand Snowball Gateway, Observation Horizon Surface & MCP/OpenAPI Switch

- **Issue**: `#274`
- **Branch**: `muse/issue-274`
- **Risk Tier**: `T2` (Bot Broker + `status:hold` + Independent Q1+Q2 Review + Human Approval)
- **Baseline**: `origin/main @ 3a7239361b2b81f763dce3477b3c3ad81f357b90`
- **Governance**: LACD v1.0.4 (`Statement != Fact. Evidence != Fact. Unknown != 0.`)
- **Authoritative Spec Source**: Notion Slot 00 (`422ad272-535b-8168-b251-d8a8506f35c2`, `North Star v3.1` On-Demand Snowball Step 2 — Plan B Pure-Cloud Serverless Loop)
- **Stage A–C Production Write Isolation**: **Zero** live Google Drive (`Drive A`) mutations and **zero** real production cloud mutations during Stage A–C. Stage D (`P0-6`) requires explicit Human `Cloud & Production GO`.

---

## 1. Objective

Deliver Step 2 of `North Star v3.1` (**Plan B: 100% Pure-Cloud Serverless On-Demand Snowball**) in `cba-kb-engine v2.3.0` while preserving 100% backward compatibility with all existing tests (`tests/**/*.py` and `legacy/**`: 0 bytes modified) and byte-identical `v2.1.5-1` through `v2.2.2-1` consumer surface outputs:

1. **P0-1 — Observation Horizon on Consumer Surface & `evaluate_league_coverage()` Checker**
2. **P0-2 — Headless Drive OAuth Loader, R2 Private Cloud State Store (`cloud_state.py`) & Topology-Safe Single-Card Drive Upserter**
3. **P0-3 — On-Demand Snowball Core Orchestrator (`snowball_orchestrator.py`)**
4. **P0-4 — Dual Switch: Serverless HTTPS Action Gateway (`snowball_gateway.py`), OpenAPI 3.1 + Worker + Runner Templates, Local stdio MCP Server (`mcp_server.py`) & CLI Subcommands**
5. **P0-5 — `v2.3.0-1` Production Release Spec & `O(Δ)` On-Demand Card Batch Promotion (`prepare_v215.py`)**
6. **P0-6 (Stage D, after Human `Cloud & Production GO`) — Pure-Cloud Initialization, `v2.3.0-1` Cutover & 20-Question Live Snowball Verification**

---

## 2. Anti-Pitfall Architectural Guardrails

1. **Zero Drive Search Index Wait**: On cache hit or fresh cloud acquisition, the gateway returns full `card_markdown` (with `observed_until`, `coverage_status`, `card_sha256`, and Drive sync receipt) directly in the HTTP/MCP response body.
2. **Zero 45s HTTP Timeout**: Fast path (`< 0.5s`) reads R2 directly; slow path polls `jobs/<job_id>.json` for a bounded window (default `28s`) and gracefully returns `HTTP 200` with `{"status": "ACQUIRING_IN_CLOUD", "job_id": "...", "next_action": "call check_acquisition_job"}` if still running.
3. **Zero Production Topology Pollution**: Daily on-demand Drive writes target an isolated folder (`CBA_KB_DRIVE_SNOWBALL_FOLDER_ID`, e.g., `25_snowball_按需即时卡/`) strictly outside `zones["current"]` (and `history`/`staging`/`evidence`) to prevent `UNEXPECTED_LIVE_OBJECT` in frozen `scripts/prepare_production.py`.
4. **Public Repo Clean-Room (`G5 == 0`, `G6 == 0`)**: Zero real Cloudflare/R2 secrets, zero Google OAuth credentials, zero Bearer-auth secrets, zero real Drive IDs, zero local private paths, and zero modifications to `.github/**`.

---

## 3. Frozen Public Contracts & Acceptance Criteria

### 3.1 P0-1: Observation Horizon & `evaluate_league_coverage()` (`AC-230-P0-1`)

#### `src/cba_kb/consumer_surface.py`
- Export constants:
  - `COVERAGE_STATUSES = ("COMPLETE", "PARTIAL", "STALE", "UNKNOWN")`
  - `EXAGGERATED_COMPLETENESS_PHRASES` (includes `"全联盟完整"`, `"最新全量"`, `"全量完整"`, `"100%完整"`, `"完整无缺"`)
- Export `evaluate_league_coverage(coverage_ledger: Optional[Mapping[str, Any]], *, now_utc: Optional[datetime] = None, stale_after_hours: float = 48.0) -> Dict[str, Any]`:
  - Evaluates the 5 proposition domains: `domestic_registration`, `foreign_registration`, `club_announcements`, `league_rules`, `game_stats`.
  - Returns `{overall_status, observed_until, domains, missing_items, honest_disclosure}`.
  - Each domain entry contains `{domain, label, observed_until, coverage_status, missing_items, source_count, honest_disclosure}`.
  - Upgrades `COMPLETE` to `STALE` when `observed_until` exceeds `stale_after_hours` relative to `now_utc`.
  - When `coverage_ledger` is `None` or empty, returns `overall_status = "UNKNOWN"`.
- Export `assert_no_exaggerated_coverage_claims(text: str, coverage_eval: Mapping[str, Any]) -> None`:
  - When `overall_status != "COMPLETE"` or any domain has `coverage_status != "COMPLETE"`, raises `ValueError("EXAGGERATED_COVERAGE_CLAIM_FORBIDDEN:...")` if any phrase in `EXAGGERATED_COMPLETENESS_PHRASES` appears in `text`.
- Export `render_observation_horizon_header(coverage_eval: Mapping[str, Any], *, domain: Optional[str] = None) -> str`:
  - Renders concise header containing `observed_until` and `coverage_status` (and `missing_items` when non-empty), validated via `assert_no_exaggerated_coverage_claims`.
- Backward-compatible optional `observation_horizon: Optional[Mapping[str, Any]] = None` parameter on:
  - `render_consumer_surface_guide_section(...)`
  - `render_consumer_field_dictionary_md(...)`
  - `render_collector_official_digest_md(...)`
  - `export_consumer_master_csv(...)`, `export_foreign_players_registration_csv(...)`, `export_domestic_registration_diff_csv(...)` (when provided, appends `observed_until` and `coverage_status` columns so CSV remains valid UTF-8-SIG RFC 4180 CSV; when `None`, 100% byte-identical to `3a723936`).
  - `build_consumer_surface_targets_spec(...)`

#### `src/cba_kb/kc_render.py` & `src/cba_kb/person_card_builder.py`
- `render_research_card(payload, ...)`: when `payload.get("observation_horizon")` is a dict, renders `## Observation Horizon` with `observed_until`, `coverage_status`, `missing_items`, and `honest_disclosure`, and enforces `assert_no_exaggerated_coverage_claims`. When absent, output is 100% byte-identical to `3a723936`.
- `build_person_card(..., coverage_ledger=None, observation_horizon=None)`: populates `observation_horizon` when provided or discovered from `instance.data_root` `coverage_ledger.json`.

#### `src/cba_kb/orchestration/release/prepare_v215.py`
- `_content_preserving_candidate(..., observation_horizon=None)`: when `release_spec.get("enable_observation_horizon_v230")` is True and `observation_horizon` is provided, stamps `observed_until` and `coverage_status` into the top 10 lines of `derived/INDEX.md` (0-byte diff for `v2.1.5-1`..`v2.2.2-1`).

---

### 3.2 P0-2: Headless OAuth, R2 Cloud State Store & Topology-Safe Drive Upserter (`AC-230-P0-2`)

#### `src/cba_kb/drive.py`
- Extend `credentials(root, interactive=False, credentials_store=None, *, token_json=None, environ=None)`:
  - Reads `token_json` (dict or JSON string) or `(environ or os.environ).get("CBA_KB_DRIVE_TOKEN_JSON")`.
  - Fail-closed rejects Service Account JSON with `RuntimeError("SERVICE_ACCOUNT_CREDENTIALS_FORBIDDEN: ...")`.
  - Requires `refresh_token`, builds `Credentials.from_authorized_user_info(info, scopes=SCOPES)`, refreshes in-memory if expired (0 local file writes, 0 browser launches).
  - Falls back identically to existing local `token.json` path when env/param is absent.
- Export `upsert_snowball_card_to_drive(drive, *, folder_id: str, filename: str, content_bytes: bytes, forbidden_folder_ids: Sequence[str] = (), production_policy: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]`:
  - Enforces main-thread single-writer (`_assert_main_thread_writer`).
  - Collects protected zone IDs from `forbidden_folder_ids` and `production_policy["zones"]` (`current`, `history`, `staging`, `evidence`). Raises `ValueError("SNOWBALL_FOLDER_IN_PROTECTED_ZONE")` if `folder_id` is protected.
  - Runs `clean(content_bytes, filename)` before write.
  - Atomically updates existing file with matching `name == filename` via `drive.put(...)`, or creates a new file in `folder_id`.
  - Returns sanitized receipt `{action, filename, sha256, size_bytes, folder_role, drive_id_redacted: True}`.

#### `src/cba_kb/cloud_state.py` (new, zero new third-party dependencies)
- `sign_aws_sigv4_headers(*, method, url, access_key_id, secret_access_key, region="auto", service="s3", payload_bytes=b"", headers=None, now_utc=None) -> Dict[str, str]`
- `R2Config` dataclass (`endpoint_url`, `bucket`, `access_key_id`, `secret_access_key`, `region="auto"`, `prefix=""`) with `R2Config.from_env(environ=None)`.
- `R2CloudStateStore(config: R2Config, *, session=None, transport=None)`:
  - `put_bytes(key, data, *, content_type="application/octet-stream")` / `get_bytes(key, *, expected_sha256=None)` (verifies SHA-256 against `x-amz-meta-sha256` & `expected_sha256`; raises `ValueError("R2_OBJECT_SHA256_MISMATCH")` on mismatch)
  - `list_keys(prefix="")`
  - `put_json(key, obj)` / `get_json(key)`
  - `put_card(season, person_key, card_record)` / `get_card(season, person_key)` (`cards/<season>/<safe_key>.json`, enforces `card_sha256 == sha256(markdown)`)
  - `put_coverage_ledger(ledger)` / `get_coverage_ledger()` (`coverage_ledger.json`)
  - `append_negative_receipt(receipt)` / `get_negative_receipts()` (`negative_receipts.jsonl`)
  - `put_job(job_id, job_doc)` / `get_job(job_id)` (`jobs/<job_id>.json`)
  - `find_active_job_for_person(person_key, season)` / `set_active_job_lock(person_key, season, job_doc)` / `clear_active_job_lock(person_key, season)`
  - `push_instance_snapshot(local_dir, *, prefix="cloud_instance/")` / `pull_instance_snapshot(local_dir, *, prefix="cloud_instance/")` (runs `clean()` before upload).

---

### 3.3 P0-3: On-Demand Snowball Core Orchestrator (`AC-230-P0-3`)

#### `src/cba_kb/snowball_orchestrator.py` (new)
- `AcquisitionBackend`, `LocalCliAcquisitionBackend`, `AsyncDispatchAcquisitionBackend`
- `SnowballOrchestrator`:
  - `get_or_acquire_person(person: str, *, season: str = "2026-2027", force_refresh: bool = False, freshness_ttl_hours: float = 24.0) -> Dict[str, Any]`
    - `UNDECIDED`: homonym/multi-candidate match -> returns candidate list, 0 network calls.
    - `HIT_FRESH`: cached card within `freshness_ttl_hours` (and `not force_refresh`) -> `< 0.5s`, returns full `card_markdown`, 0 network calls.
    - `UNKNOWN_NEGATIVE_CACHED`: active `cba-kb.negative-receipt.v1` within TTL (and `not force_refresh`) -> 0 network calls.
    - Concurrency dedup: active `RUNNING` lock for `(person, season)` -> returns `ACQUIRING_IN_CLOUD` reusing existing `job_id` (`deduplicated: True`).
    - `ACQUIRED_AND_BUILT`: runs `acquisition_backend`, writes card to R2 `cards/`, syncs to Drive snowball folder (if configured), updates job `COMPLETED`, returns full `card_markdown`.
    - `UNKNOWN_AFTER_WHITELIST_CHECK`: backend reports not found -> persists new `cba-kb.negative-receipt.v1` to R2, marks job `COMPLETED_UNKNOWN`.
    - `ACQUIRING_IN_CLOUD`: async backend dispatched -> persists `RUNNING` job & lock, returns `job_id` + `next_action`.
  - `get_league_coverage() -> Dict[str, Any]`
  - `check_acquisition_job(job_id: str) -> Dict[str, Any]`

---

### 3.4 P0-4: Dual Switch — HTTPS Gateway, OpenAPI 3.1, Worker, Runner Template, MCP Server & CLI (`AC-230-P0-4`)

- `src/cba_kb/snowball_gateway.py`:
  - `verify_bearer_token(authorization_header: Optional[str], expected_token: str) -> bool` (constant-time `hmac.compare_digest`, fail-closed).
  - `SnowballGatewayApp(orchestrator, *, bearer_token, wait_timeout_seconds=28.0, poll_interval_seconds=1.0, sleep_fn=None, monotonic_fn=None)`:
    - `handle_request(method, path, headers, body=b"") -> Tuple[int, Dict[str, str], Dict[str, Any]]`
    - Routes: `POST /v1/person`, `GET /v1/coverage`, `GET /v1/jobs/<job_id>`.
    - Missing/invalid Bearer-scheme auth -> `401` `{"error": "UNAUTHORIZED"}`.
    - Bounded wait up to `wait_timeout_seconds` on `ACQUIRING_IN_CLOUD`; returns `200` with completed card if finished in window, or `200` with `status="ACQUIRING_IN_CLOUD"` + `job_id` + `next_action` if window expires.
- `src/cba_kb/mcp_server.py`:
  - `SnowballMCPServer(orchestrator)`: JSON-RPC 2.0 over stdio supporting `initialize`, `ping`, `tools/list`, `tools/call` for `get_or_acquire_person`, `get_league_coverage`, `check_acquisition_job`.
- `src/cba_kb/cli.py`:
  - New subcommands: `snowball-query`, `cloud-state-push`, `cloud-state-pull`, `mcp-serve`.
- Clean-room config artifacts:
  - `config/chatgpt_snowball_openapi.yaml` (OpenAPI 3.1.0 for Custom GPT Actions)
  - `config/cloud_snowball_worker.js` (Cloudflare Worker edge gateway)
  - `config/cloud_snowball_runner.example.yml` (Sanitized private collector Actions runner template)

---

### 3.5 P0-5: `v2.3.0-1` Production Release Spec & `O(Δ)` On-Demand Card Promotion (`AC-230-P0-5`)

#### `src/cba_kb/orchestration/release/prepare_v215.py`
- Register `"v2.3.0-1"` in `V215_RELEASE_SPECS`:
  - `product_baseline_sha`: `"3a7239361b2b81f763dce3477b3c3ad81f357b90"`
  - `enable_consumer_surface_v215`: `True`
  - `enable_operational_kb_v220`: `True`
  - `enable_curated_compact_surface_v222`: `True`
  - `enable_observation_horizon_v230`: `True`
  - `enable_on_demand_cards_v230`: `True`
  - `incremental_delta_only`: `True`
  - `archive_format`: `"bundle_v1"`
- `_build_instance_consumer_targets(...)` reads `coverage_ledger.json` and `on_demand_cards_manifest.json` (when `enable_on_demand_cards_v230` is True), rejects symlinks and parent/path escapes across `data_root`, `on_demand_dir`, `on_demand_cards_manifest.json`, and every card file (with `O_NOFOLLOW` open-time enforcement), verifies `card_sha256` fail-closed (`ProjectionError("ON_DEMAND_CARD_SHA256_MISMATCH")`), scrubs raw Drive IDs, and promotes cards into `20_data` via `O(Δ)` `bundle_v1`.

---

### 3.6 P0-6: Stage D Pure-Cloud Deployment, `v2.3.0-1` Cutover & 20-Question Closed-Loop Acceptance (`AC-230-P0-6`, Deferred Post-Merge / Post-GO)

#### Explicit Deferral Boundary
- **Stage A–C Scope Boundary**: PR merge into `main` and Engine Release `v2.3.0` verify `P0-1` through `P0-5` strictly offline/clean-room (`0` real Google Drive `Drive A` mutations, `0` real Cloudflare R2 / Worker / GitHub Actions production mutations). Merging the Stage A–C PR **does NOT** claim that `P0-6` has been executed or verified in live cloud/production.
- **Stage D Trigger**: `P0-6` executes **exclusively after**:
  1. PR `#275` is merged to `main` (Human removes `status:hold` after independent Q1+Q2 PASS),
  2. Engine Release `v2.3.0` is published with annotated tag `v2.3.0`, `RRC` (Release Readiness Check), and `PRV` (Post-Release Verification) receipts bound to the exact merge commit SHA, and
  3. Human explicitly issues **`Cloud & Production GO`** in the session after verifying the Stage D preflight checklist.

#### Human `Cloud & Production GO` Authorization Protocol & Evidence Binding
Before any Stage D live cloud or `Drive A` mutation is permitted, the executor MUST present and bind:
1. **Engine Release Evidence**: Exact `v2.3.0` tag commit SHA, GitHub Release URL, `RRC` receipt, and `PRV` readback receipt.
2. **Stage A–C Zero-Production-Write Proof**: Explicit confirmation that `Drive A` remained untouched at `v2.2.2-1 / COMPLETE` (`565/565` active assets) and no live R2/Worker state was modified prior to `GO`.
3. **Minimum Private Secrets Checklist** (stored exclusively in Cloudflare Worker Encrypted Secrets and private `cba-kb-collector` GitHub Actions Secrets — never committed to `cba-kb-engine`):
   - Private R2 bucket credentials (`CBA_KB_R2_ENDPOINT_URL`, `CBA_KB_R2_BUCKET`, `CBA_KB_R2_ACCESS_KEY_ID`, `CBA_KB_R2_SECRET_ACCESS_KEY`)
   - Gateway authorization secret (`GATEWAY_AUTH_SECRET` / `SNOWBALL_API_BEARER_TOKEN`)
   - Private collector dispatch credential (`GITHUB_DISPATCH_REPO`, `GITHUB_DISPATCH_PAT`)
   - Headless personal Drive OAuth JSON (`CBA_KB_DRIVE_TOKEN_JSON`, personal authorized user only; Service Accounts strictly forbidden)
   - Dedicated isolated snowball folder ID (`CBA_KB_DRIVE_SNOWBALL_FOLDER_ID` for `25_snowball_按需即时卡/`, verified strictly outside `zones["current"|"history"|"staging"|"evidence"]`)
4. **Stage D Execution Sequence** (post-`GO` only):
   - Run `cba-kb cloud-state-push` to initialize sanitized `cloud_instance/` and `coverage_ledger.json` in private R2.
   - Deploy private Cloudflare Worker edge gateway (`config/cloud_snowball_worker.js`) and private `cba-kb-collector` runner workflow (`config/cloud_snowball_runner.example.yml`).
   - Execute `v2.3.0-1` `O(Δ)` cutover on `Drive A` (stamping Observation Horizon onto `00_entry/INDEX` and `_CURRENT` surfaces).
   - Run the frozen 20-question closed-loop acceptance test on the 100% pure-cloud path (zero local Mac daemon dependency) and archive receipts to Notion Slots `07` & `08`.

#### Frozen 20-Question Stage D Acceptance Matrix & Pass/Fail Thresholds
All 20 questions MUST be executed against the live pure-cloud endpoint (with local machine offline/no local daemon) and recorded with exact timestamps, latencies, `job_id`s, R2 `card_sha256` digests, and `Drive A` sync receipts:

| Category | Question IDs | Test Scenario | Concrete Pass Criteria |
| --- | --- | --- | --- |
| **Cat 1: Known / Cached Persons & League Coverage Horizon** | `Q01`–`Q10` (10 questions) | Query pre-existing players in KB + league-wide domestic/foreign/announcement coverage status | - Edge fast-path response `< 1.0s` (`HIT_FRESH` / `/v1/coverage`)<br>- Accurately reports `observed_until` and `COMPLETE` / `PARTIAL` / `STALE` gaps<br>- Zero exaggerated completeness wording when any domain is non-`COMPLETE`<br>- Official confirmed facts `FAIL = 0` |
| **Cat 2: Uncached Active / Foreign Players (First-Ask Acquire + Second-Ask Hit)** | `Q11`–`Q15` (5 questions) | Ask about active domestic / new foreign players not yet carded in KB, then immediately re-ask the same player a 2nd time | - 1st ask triggers private cloud runner (`COL v0.6.0` whitelist + recent single-game stats -> R2 -> `build-person-card` -> R2 `cards/` + `Drive A` `25_snowball_按需即时卡/`)<br>- Returns full `card_markdown` directly in dialog (via 28s bounded wait or 1 `check_acquisition_job` follow-up, `0` HTTP `504` timeouts)<br>- Immediate 2nd ask hits R2 `HIT_FRESH` in `< 1.0s` (`network_called == false`) |
| **Cat 3: Homonym Disambiguation** | `Q16`–`Q17` (2 questions) | Ambiguous homonym player queries | - Returns `status == "UNDECIDED"` with structured `candidates` list<br>- Never auto-merges profiles and never triggers blind external fetch |
| **Cat 4: Whitelist-Absent Adversarial Queries & Negative-Receipt Cache** | `Q18`–`Q20` (3 questions) | Non-existent / unannounced players not present on official whitelist sources, followed by immediate re-query within 24h | - 1st ask returns `UNKNOWN_AFTER_WHITELIST_CHECK` (no fabricated stats/facts, no non-whitelist scraping) and persists `cba-kb.negative-receipt.v1` (24h TTL)<br>- Immediate re-query returns `UNKNOWN_NEGATIVE_CACHED` with `0` outbound network calls |

#### Stage D Overall Release Gate Thresholds (All Mandatory)
- **Official Confirmed Facts**: `FAIL == 0` (zero hallucinations, zero fabricated numbers, `Statement != Fact. Evidence != Fact. Unknown != 0.`).
- **Honest Abstention Rate (HAR)**: `HAR >= 80%` across all unanswerable / out-of-scope / missing-coverage prompts.
- **Production `Drive A` Integrity**: `v2.3.0-1` `release_status.json == COMPLETE`, `109/109` protected tables zero drift, `G1 == 0` (zero `UNEXPECTED_LIVE_OBJECT` in `20_data`), `25_snowball_按需即时卡/` strictly isolated from `zones["current"]`.
- **Clean-Room & Security**: `G5 == 0`, `G6 == 0`, unauthorized gateway requests return `401` Fail-Closed.

---

## 4. Verification Plan & Hard Guardrails

- **Differential Falsification (Commit 1 on `3a723936`)**: `pytest tests/test_v230_cloud_snowball_and_observation_horizon.py -q` MUST collect 11/11 tests with 0 collection/syntax errors and FAIL 11/11 differentially on baseline `3a723936`.
- **Full Regression & Governance (Commit 2)**:
  1. `pytest tests/ legacy/ -q` — 100% PASS, 0 new skips/xfails, 0 bytes modified in Commit 1 test file, existing `tests/**/*.py`, and `legacy/**`.
  2. `python scripts/check_secrets.py --tracked` — exit 0.
  3. `python .github/scripts/gate_selftest.py` — 100% PASS.
  4. `lacd verify --local --strict` — exit 0 (`VALIDATED`, `drift: clean`).
- **Frozen Files (0-byte modification)**: `scripts/prepare_production.py` (SHA-256 `0ccb79429fb4ec5e270a3154af987475ae75bd4ef81de48c1e665a3b93cf4573`), `pyproject.toml` (`version = "2.0.2"`), `requirements.lock`, `.github/**`, all existing `tests/**/*.py`, `legacy/**`.
- **Zero Production Drive / Cloud Writes**: 0 live mutations throughout Stage A–C.
