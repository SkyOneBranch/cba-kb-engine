# REQ-223 (v2.2.3): Clean-Room ID/Filter Hygiene, Generic Player-Game Stats & On-Demand Person Card Builder

- **Issue**: `#272`
- **Branch**: `muse/issue-272`
- **Risk Tier**: `T2` (Bot Broker + `status:hold` + Independent Q1+Q2 Review + Human Approval)
- **Baseline**: `origin/main @ 2ca54e60ba57bac97f85b80ac4ec5e4b7d5720e4`
- **Governance**: LACD v1.0.4 (`Statement != Fact. Evidence != Fact. Unknown != 0.`)
- **Authoritative Spec Source**: Notion Slot 00 (`422ad272-535b-81e4-9570-eba1b4cb006a`, `North Star v3.1` On-Demand Snowball Step 1)
- **Production Drive Isolation**: Engine-only release. **Zero** live Google Drive API write calls across Stage A–C and local verification (`Drive A` stays at `v2.2.2-1 / COMPLETE`). Zero modifications to `cba-kb-collector`.

---

## 1. Objective

Deliver Step 1 of `North Star v3.1` (On-Demand Snowball) in `cba-kb-engine v2.2.3` while preserving 100% backward compatibility with all existing tests (`tests/**/*.py` and `legacy/**`: 0 bytes modified) and byte-identical `v2.2.2-1` curated consumer surface output:

1. **P0-1 — Clean-Room Hygiene (`G5 == 0`) & Externalized Curation Filter Profile**
2. **P0-2 — Generic Player-Game Stats Contract (`PLAYER_GAME_STATS`) & Deterministic Recent-N-Game Aggregator (`G6 == 0`)**
3. **P0-3 — On-Demand Person Card Assembler (`person_card_builder.py`: Disambiguation, Multi-Source Assembly & Zero-Stats Graceful Fallback)**
4. **P0-4 — Research Card Single-Game Section Rendering (`kc_render.py`) & Fast Local CLI (`cba-kb build-person-card`)**
5. **P0-5 — `ops-sync` Cross-Repo Sidecar Ingestion (`game_logs_dir`, `coverage_ledger_path`, `negative_receipts_path`)**

---

## 2. Anti-Regression Guardrails (4 Locked Contracts)

1. **Contract A (Zero-Conflict Curation Externalization)**:
   - Move 100% of hardcoded Chinese curation filter keywords out of `src/cba_kb/consumer_surface.py` into declarative `config/curation_profile.yaml` (alongside `config/taxonomy.yaml` and `config/club_aliases.yaml`), plus a pure neutral synthetic example `config/curation_profile.example.yaml`.
   - Runtime lookup prefers `<instance.config_root>/curation_profile.yaml` when present, falling back to repo default `config/curation_profile.yaml`. Explicit missing path or invalid schema fails closed with `ValueError("CURATION_PROFILE_MISSING")` / `ValueError("CURATION_PROFILE_INVALID")`.
   - All existing `tests/test_v222_*.py` pass with 0-byte modification and `v2.2.2-1` curated surface artifacts remain SHA-256 byte-for-byte identical.
2. **Contract B (Zero Residual Real Drive IDs & `test_master.py` Compatibility)**:
   - Remove all hardcoded historical real Google Drive IDs from `src/cba_kb/master.py` and `scripts/accept_v1_5_1.py`; redact historical references in `DATA_LICENSE.md`, `docs/history/V1_5_1_THIRD_PARTY_REVIEW.md`, and `docs/requirements/V1_5_1_THIRD_PARTY_REVIEW.md` using `<REDACTED_HISTORICAL_MASTER_DRIVE_ID>`.
   - `master.inspect()` and `master.build()` fall back to explicit non-ID sentinel `UNBOUND_LOCAL_MASTER_ID = "UNBOUND_LOCAL_MASTER"` (without fabricating a `drive.google.com` URL) when unbound, keeping `tests/test_master.py` 100% compatible; `scripts/accept_v1_5_1.py` calls `resolve_master_file_id(..., require_bound=True)` and fails closed when unbound.
3. **Contract C (On-Demand Assembly Without Breaking `REQ-220` Incremental Producer)**:
   - Preserve `kc_local_producer.build_incremental_player_cards()` strict behavior unchanged.
   - Add `src/cba_kb/person_card_builder.py` for on-demand assembly of any player (domestic or foreign), natively supporting newly signed / zero-stats players with `Posture: UNKNOWN` (`unknown_subkind: NO_SEASON_STATS_RECORDED_YET`).
4. **Contract D (`G6 == 0` & Strict Grain Separation)**:
   - Do NOT alter `src/cba_kb/stats.py` frozen `ALLOWED_TOP_LEVEL_FIELDS` or `SCOPES = {"WHOLE_SEASON", "TEAM_SPLIT"}`.
   - Implement `PLAYER_GAME_STATS` in a dedicated new module `src/cba_kb/game_stats.py`. Never add or merge `PLAYER_GAME_STATS` into `PLAYER_SEASON_STATS`. All repository fixtures are 100% synthetic.

---

## 3. Frozen Public Contracts & Acceptance Criteria

### 3.1 P0-1: Clean-Room ID Removal & Externalized Curation Profile (`AC-223-P0-1`)

#### `src/cba_kb/master.py` & `scripts/accept_v1_5_1.py`
- Remove hardcoded `MASTER_ID` literal from `src/cba_kb/master.py` and all hardcoded Drive ID literals from `scripts/accept_v1_5_1.py`.
- Export in `src/cba_kb/master.py`:
  - `UNBOUND_LOCAL_MASTER_ID = "UNBOUND_LOCAL_MASTER"`
  - `resolve_master_file_id(master_file_id: Optional[str] = None, *, instance: Any = None, environ: Optional[Mapping[str, str]] = None, require_bound: bool = False) -> str`
    - Order: explicit `master_file_id` -> `environ["CBA_KB_MASTER_FILE_ID"]` -> `instance.read_json("runtime.json")["inputs"]["MASTER.xlsx"]["id"]`.
    - When unresolved: raises `ValueError("MASTER_FILE_ID_REQUIRED")` if `require_bound=True`, else returns `UNBOUND_LOCAL_MASTER_ID`.
  - `inspect(path, *, master_file_id=None, instance=None, environ=None)`
  - `build(master, output, release_id, commit, generated_at, *, master_file_id=None, instance=None, environ=None)`
    - When resolved ID is `UNBOUND_LOCAL_MASTER_ID`, `INDEX.md` MUST NOT contain `drive.google.com/file/d/`.
- Redact residual historical `MASTER_ID` references in `DATA_LICENSE.md`, `docs/history/V1_5_1_THIRD_PARTY_REVIEW.md`, and `docs/requirements/V1_5_1_THIRD_PARTY_REVIEW.md`.

#### `src/cba_kb/consumer_surface.py` + `config/curation_profile.yaml` + `config/curation_profile.example.yaml`
- Zero hardcoded Chinese curation filter keywords (`每日预言家`, `每日名场面`, `图文征集`, `我和我的篮球`, `杭州亚运会`, `亚运会总结`, `亚运会`, `唁电`) in any `.py` file under `src/cba_kb/`.
- Declarative profile files:
  - `config/curation_profile.yaml` (production-equivalent default CBA profile)
  - `config/curation_profile.example.yaml` (pure neutral synthetic example)
- Required YAML keys:
  - `schema_version` (`int == 1`)
  - `noise_event_classes` (`list[str]`)
  - `noise_keywords` (`list[str]`)
  - `historical_pinned_event_classes` (`list[str]`)
  - `historical_pinned_keywords` (`list[str]`)
  - `pinned_marker_keywords` (`list[str]`)
  - `protected_event_classes` (`list[str]`)
  - `active_competitive_keywords` (`list[str]`)
  - `condolence_keywords` (`list[str]`)
  - `condolence_exempt_keywords` (`list[str]`)
  - `compact_announcement_excerpt_chars` (`int > 0`)
- Export in `src/cba_kb/consumer_surface.py`:
  - `load_curation_profile(path_or_instance: Optional[str | Path | Instance] = None) -> Dict[str, Any]`
    - Explicit missing path -> `ValueError("CURATION_PROFILE_MISSING")`.
    - Malformed YAML / missing required key / invalid field type -> `ValueError("CURATION_PROFILE_INVALID")`.
  - `filter_curated_operational_announcements(bundles, *, active_window_start="2025-07-01", curation_profile=None, curation_profile_path=None, instance=None) -> Dict[str, Any]`
  - `render_collector_official_digest_md(..., curation_profile=None, curation_profile_path=None, instance=None) -> bytes`

---

### 3.2 P0-2: Generic Player-Game Stats Contract & Recent-N-Game Aggregator (`AC-223-P0-2`)

#### New Module: `src/cba_kb/game_stats.py`
- Constants:
  - `GAME_STATS_SCHEMA = "cba-kb.player-game-stats.v1"`
  - `GAME_STATS_SCHEMA_VERSION = 1`
  - `SEMANTIC_GRAIN = "PLAYER_GAME_STATS"`
  - `PLAYER_GAME_METRIC_ALLOWLIST` (covers `minutes_played`, `seconds_played`, `points`, `rebounds`, `offensive_rebounds`, `defensive_rebounds`, `assists`, `steals`, `blocks`, `turnovers`, `fouls`, `field_goals_made`, `field_goals_attempted`, `three_point_made`, `three_point_attempted`, `free_throws_made`, `free_throws_attempted`, `plus_minus`, `starter`).
- Exceptions: `GameStatsError(RuntimeError)`, `GameStatsValidationError(GameStatsError)`.
- Public API:
  - `build_game_stat_record_id(*, provider: str, season: str, match_type: str, game_id: str, player_key: str, revision_seq: Optional[int] = None) -> str`
  - `validate_player_game_stat_record(rec: Dict[str, Any]) -> Dict[str, Any]`
    - Enforces `schema == "cba-kb.player-game-stats.v1"`, `semantic_grain == "PLAYER_GAME_STATS"`, required top-level fields (`record_id`, `provider`, `competition`, `season`, `match_type`, `game_id`, `game_date`, `team_id`, `opponent_team_id`, `metrics`, `provenance`, `rights`).
    - Enforces `provenance.raw_sha256` is valid 64-hex and `provenance.canonical_url` is non-empty.
    - Preserves `None` for missing metrics (`Unknown != 0`; never converts `None` to `0`). Rejects booleans and negative values on non-signed metrics.
  - `reconcile_game_stat_revisions(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]`
    - Resolves `supersedes_record_id` chains per `(player_key, game_id)` into `active_records`, `superseded_records`, and `revision_summaries` (each tagged `disclosure_posture = "LOUD_REVISION"`). Never silently overwrites prior records.
  - `summarize_recent_games(records: Sequence[Dict[str, Any]], *, window: int = 5) -> Dict[str, Any]`
    - Validates `window >= 1`.
    - Reconciles supersession chains, excludes DNP games (`dnp_reason` present or `did_not_play is True`) from played-game averages denominator, sorts active played games descending by `(game_date, game_id, record_id)`, and slices the latest `window` games.
    - Returns deterministic dict with `semantic_grain`, `window`, `played_games_in_window`, `total_played_games`, `dnp_games_count`, `averages` (`points`, `rebounds`, `assists`, `steals`, `blocks`, `minutes_played` — `None` if all missing), `shooting_totals` (`fg_made`, `fg_attempted`, `fg_pct`, `tp_made`, `tp_attempted`, `tp_pct`, `ft_made`, `ft_attempted`, `ft_pct`), `latest_game_date`, `latest_captured_at`, `games`, and `revision_summaries`.

---

### 3.3 P0-3: On-Demand Person Card Assembler (`AC-223-P0-3`)

#### New Module: `src/cba_kb/person_card_builder.py`
- Public API:
  - `resolve_person_query(query: str, *, instance: Optional[Instance] = None, master_rows: Optional[Sequence[Dict[str, Any]]] = None, foreign_rows: Optional[Sequence[Dict[str, Any]]] = None, identity_registry: Optional[Dict[str, Any]] = None, negative_receipts: Optional[Sequence[Dict[str, Any]]] = None, data_cutoff: Optional[str] = None, now_utc: Optional[datetime] = None) -> Dict[str, Any]`
    - Searches domestic MASTER, foreign registration CSV rows, and `player_identity` registry by Chinese name, English name, alias, `player_uid`, or official `player_id`.
    - Unique hit -> `status = "RESOLVED"` with `person` skeleton (`player_uid`, `player_id`, `player_name`, `player_name_en`, `current_club`, `clubs_history`, `registration_type`, `jersey_number`, `position`, `domestic_records`, `foreign_records`).
    - Ambiguous / homonym multi-candidate hit -> Fail-Closed `status = "UNDECIDED"` with structured `candidates` list (never auto-merges).
    - No match -> `status = "NOT_FOUND_IN_LOCAL_KB"` with `local_data_cutoff`, `negative_receipt_hit` (`bool`), and matching active `negative_receipt` if within TTL (never fabricates a card).
  - `build_person_card(query: str, *, season: str = "2026-2027", recent_games_window: int = 5, instance: Optional[Instance] = None, master_rows: Optional[Sequence[Dict[str, Any]]] = None, foreign_rows: Optional[Sequence[Dict[str, Any]]] = None, identity_registry: Optional[Dict[str, Any]] = None, season_stats_records: Optional[Sequence[Dict[str, Any]]] = None, game_stats_records: Optional[Sequence[Dict[str, Any]]] = None, document_bundles: Optional[Sequence[Dict[str, Any]]] = None, negative_receipts: Optional[Sequence[Dict[str, Any]]] = None, output_dir: Optional[str | Path] = None, register_for_sync: bool = False, known_drive_ids: Sequence[str] = ()) -> Dict[str, Any]`
    - Auto-assembles registration facts, season stats (`PLAYER_SEASON_STATS`), recent single-game stats (`PLAYER_GAME_STATS`), and `DocumentLane` / `operational_checkpoint` announcements + revision disclosures.
    - When a resolved player has registration/announcement evidence but no season stats yet, builds a valid card with `posture = "UNKNOWN"` and `unknown_subkind = "NO_SEASON_STATS_RECORDED_YET"`.
    - When `output_dir` is given, atomically writes `研究卡_<姓名>_<ID>_<赛季>_CURRENT.md` (`drive_api_called: False`, `network_called: False`).

---

### 3.4 P0-4: Research Card Single-Game Rendering & `build-person-card` CLI (`AC-223-P0-4`)

#### `src/cba_kb/kc_render.py`
- `render_research_card(payload, *, evidence_verified=False, max_external_citations_per_card=10) -> str`:
  - Without `recent_games_summary` (and without `registration_profile`), output is 100% byte-for-byte identical to baseline `2ca54e60`.
  - When `recent_games_summary` is provided, renders `## Recent Single-Game Log (Player-Game Grain)` including window averages, shooting totals/rates, latest game date & capture cutoff, per-game breakdown (date, round, opponent, minutes, PTS/REB/AST, shooting, `doc_id` or `raw_sha256` prefix locator, `[LOUD_REVISION]` tag when revised), scrubbed via `scrub_raw_drive_ids_for_consumer`, ending with `end_marker: true\n`.

#### `src/cba_kb/cli.py`
- Register `cba-kb build-person-card` subcommand with:
  - `--person` (required)
  - `--season` (default `"2026-2027"`)
  - `--recent-games-window` (default `5`)
  - `--output-dir` (optional Path)
  - `--register-for-sync` (flag)
  - `--format` (`json` | `markdown`, default `json`)
  - Optional local data overrides (`--master-csv`, `--foreign-csv`, `--identity-registry`, `--season-stats`, `--game-logs-dir`, `--ops-checkpoint`, `--negative-receipts`).
- Pure local execution: 0 network calls, 0 Drive API calls, `< 0.5s` single-person build time.

---

### 3.5 P0-5: `ops-sync` Cross-Repo Sidecar Ingestion (`AC-223-P0-5`)

#### `src/cba_kb/operational_sync.py` & `src/cba_kb/cli.py`
- Extend `run_operational_sync(...)` with optional backward-compatible keyword arguments:
  - `game_logs_dir: Optional[str | Path] = None`
  - `coverage_ledger_path: Optional[str | Path] = None`
  - `negative_receipts_path: Optional[str | Path] = None`
- Behavior:
  - `game_logs_dir`: incrementally scans & validates `PLAYER_GAME_STATS` JSONL/JSON files, tracks SHA-256 hashes in `operational_checkpoint.json["consumed_game_logs"]` (idempotent on unchanged re-run), and adds players with new or revised game logs to `dirty_player_uids`.
  - `coverage_ledger_path`: validates `COMPLETE / PARTIAL / STALE / UNKNOWN` source statuses and writes `coverage_ledger_summary` to `freshness_report.json`.
  - `negative_receipts_path`: validates `cba-kb.negative-receipt.v1` entries, counts total and active (within TTL) receipts, and writes `negative_receipts_summary` to `freshness_report.json`.
- Wire corresponding optional CLI flags (`--game-logs-dir`, `--coverage-ledger`, `--negative-receipts`) in `cba-kb ops-sync`.

---

## 4. Verification Plan & Hard Guardrails

- **Differential Falsification (Commit 1 on `2ca54e60`)**: `pytest tests/test_v223_cleanroom_and_on_demand_person_card.py -q` MUST fail cleanly on baseline `2ca54e60` with 0 collection/syntax errors.
- **Full Regression & Governance (Commit 2)**:
  1. `pytest tests/ legacy/ -q` — 100% PASS, 0 new skips/xfails, 0 bytes modified in existing `tests/**/*.py` and `legacy/**`.
  2. `python scripts/check_secrets.py --tracked` — exit 0.
  3. `python .github/scripts/gate_selftest.py` — 100% PASS.
  4. `lacd verify --local --strict` — exit 0 (`VALIDATED`, `drift: clean`).
- **Frozen Files (0-byte modification)**: `scripts/prepare_production.py`, `pyproject.toml`, `requirements.lock`, `.github/**`, all existing `tests/**/*.py`, `legacy/**`.
- **Zero Production Drive Writes**: 0 live Google Drive API mutations throughout Stage A–C.
