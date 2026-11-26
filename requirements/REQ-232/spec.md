# REQ-232 (v2.3.2): 12-Metric Season Stats Table & Prior-Season Fallback, 3-Domain Observation Horizon, Historical `_ARCHIVE.md` Migration & `public_safe` Visibility Export

- **Issue**: `#278`
- **Branch**: `muse/issue-278`
- **Risk Tier**: `T2` (Bot Broker + `status:hold` + Independent Q1+Q2 Review + Human Approval)
- **Baseline**: `origin/main @ 985c647a459ef7a7c970445a9a37db082eaecc71` (Release `v2.3.1`)
- **Governance**: LACD v1.0.4 (`Statement != Fact. Evidence != Fact. Unknown != 0. Observed-until != Now; Silence != Absence.`)
- **Production Isolation (Stage A–C)**: Zero live Google Drive `Drive A` writes, zero live Backblaze B2 / Cloudflare R2 writes, zero cross-repo edits.
- **Clean-Room & Frozen-File Guardrails**:
  - `G5 == 0` (zero real Google Drive IDs, zero local absolute paths such as `<local-home>/...`, zero real private corpus).
  - `G6 == 0` (zero real player stats, boxscores, or clippings; 100% synthetic fixtures).
  - 0-byte frozen files: `scripts/prepare_production.py`, `pyproject.toml`, `requirements.lock`, `.github/**`, `legacy/**`, and all pre-existing `tests/**/*.py`.

---

## 1. Objective

Upgrade the CBA-KB person card engine (`cba-kb-engine v2.3.2`) across five cohesive pillars while preserving 100% backward compatibility with `v2.1.5-1..v2.3.1-1`:

1. **P0-1** — 12-metric `PLAYER_SEASON_STATS` season averages table (`CORE_SEASON_STATS_COLUMNS`) with deterministic `(REGULAR before PLAYOFFS, WHOLE_SEASON before TEAM_SPLIT)` ordering, unmerged split rows, `None -> UNKNOWN` (never `0`), and prior full-season baseline fallback (`prior_season_baseline`) when the target season has no recorded season stats yet.
2. **P0-2** — 3-domain independent person observation horizon (`registration`, `statistics`, `official_announcements`) with a 4-state enum (`DomainHorizonStatus`: `VERIFIED_PRESENT`, `OBSERVED_EMPTY`, `SOURCE_GAP_UNKNOWN`, `NOT_APPLICABLE`), non-player coach `NOT_APPLICABLE` handling, and isolation so degraded stats never pollute verified registration.
3. **P0-3** — Historical season `_ARCHIVE.md` naming & top archive banner under `enable_v232_card_pipeline=True`, fail-closed unique `CURRENT` person card guard (`assert_unique_current_card_per_person`), and `archive_compactor._validate_authoritative_release_state` dual-key (`status` / `state`) acceptance.
4. **P0-4** — Card visibility modes (`--visibility internal_full | public_safe`), `_PUBLIC_CURRENT.md` naming, `Visibility: PUBLIC_SAFE` header, automatic stripping of verbatim `Document Passage` blocks / internal IDs / private paths / unverified clues, fail-closed `verify_public_safe_card_text()` leak guard, and new `export-person-cards` CLI subcommand.
5. **P0-5** — `V215_RELEASE_SPECS["v2.3.2-1"]` registration bound to baseline `985c647a459ef7a7c970445a9a37db082eaecc71` with `enable_v232_card_pipeline: True` and `active_league_season: "2026-2027"`, with zero regression on `v2.1.5-1..v2.3.1-1`.
6. **P0-6** — Stage D production cutover runbook (executed ONLY after PR merge, Release `v2.3.2` RRC/PRV PASS, and explicit Human `Cloud & Production GO`).

---

## 2. Explicit Non-Goals

- **No v2.4.0 StoryRecord / 4-tuple / Wiki biography redesign**: Narrative biography synthesis, `StoryRecord` schemas, 4-tuple event extraction, and Wiki-style card layouts are strictly out of scope for REQ-232.
- **No breaking changes to legacy payloads or historical release specs**: Legacy `render_research_card()` calls without v2.3.2 fields and `v2.1.5-1..v2.3.1-1` release specs MUST preserve exact backward compatibility (0-byte diff).

---

## 3. Frozen Public Contracts & Acceptance Criteria

### 3.1 P0-1: 12-Metric Season Averages Table & Prior-Season Baseline Fallback (`AC-232-P0-1`)

#### `src/cba_kb/stats.py`
- Constant `CORE_SEASON_STATS_COLUMNS: Tuple[str, ...]` (exact 12 canonical metrics in order):
  1. `games_played`
  2. `games_started`
  3. `minutes_per_game`
  4. `points_per_game`
  5. `rebounds_per_game`
  6. `assists_per_game`
  7. `steals_per_game`
  8. `blocks_per_game`
  9. `turnovers_per_game`
  10. `field_goals_percentage`
  11. `three_point_percentage`
  12. `free_throws_percentage`

#### `src/cba_kb/person_card_builder.py`
- When the resolved person has target-season `PLAYER_SEASON_STATS` records:
  - Populates `payload["season_stats_table"]` (and includes it in the build payload) with one row per matched record, sorted deterministically by `(match_type_rank, scope_rank, team_name_or_id, record_id)` where:
    - `REGULAR` / `"1"` sorts before `PLAYOFFS` / `"2"`
    - `WHOLE_SEASON` sorts before `TEAM_SPLIT`
  - **Never merges** `WHOLE_SEASON` and `TEAM_SPLIT` rows; each record remains its own row.
  - Preserves all 12 `CORE_SEASON_STATS_COLUMNS` values with `None` kept as `None` (never coerced to `0` or `0.0`).
  - Preserves legacy scalar fields (`payload["metric"]`, `payload["grains"]["STATS"]`, `payload["evidence_transparency"]["adopted_value"]`) from the primary row.
- When the resolved person has **NO** target-season `PLAYER_SEASON_STATS` records, but has prior-season records (e.g. `2025-2026 < 2026-2027`):
  - Target season posture MUST remain `"UNKNOWN"` with `unknown_subkind == "NO_SEASON_STATS_RECORDED_YET"`.
  - Selects the latest prior season strictly less than `season`, populates `payload["prior_season_baseline"]` (and returns `"prior_season_baseline"` in the `build_person_card()` result dict):
    ```python
    {
        "season": "<prior_season>",
        "target_season": "<target_season>",
        "rows": [...],
        "captured_at": "<captured_at or None>",
        "locator": "<locator or None>",
    }
    ```
  - If no prior season exists, `prior_season_baseline` is `None` (omitted from payload).

#### `src/cba_kb/kc_render.py`
- `render_research_card(payload, *, evidence_verified=False, max_external_citations_per_card=10, visibility="internal_full") -> str`:
  - Preserves the legacy scalar lines under `## Statistical Evidence`.
  - When `payload.get("season_stats_table")` is non-empty:
    - Renders `### Season Averages (Player-Season Grain)` under `## Statistical Evidence` with a Markdown table containing all 12 metrics (`CORE_SEASON_STATS_COLUMNS`) plus `Match Type`, `Scope`, `Team`, `Captured At`, `Locator`.
    - Sorts rows `(REGULAR before PLAYOFFS, WHOLE_SEASON before TEAM_SPLIT)`.
    - Keeps `WHOLE_SEASON` and `TEAM_SPLIT` on separate rows (never merged).
    - Renders `None` values strictly as `UNKNOWN` (never `0` or `0.0`); explicit `0` / `0.0` renders as `0` / `0.0`.
  - When `payload.get("prior_season_baseline")` is present with non-empty `rows`:
    - Renders `### Prior Full-Season Baseline ({prior_season} — Historical Reference Only, Not {target_season})` under `## Statistical Evidence` with the 12-metric table, `captured_at`, and `locator`.
  - **Backward Compatibility**: a minimal v2.3.1 payload without `season_stats_table`, `prior_season_baseline`, `domain_horizons`, and with default `visibility="internal_full"` produces an exact **0-byte diff** vs v2.3.1 output.

---

### 3.2 P0-2: 3-Domain Independent Observation Horizon & 4-State Enum (`AC-232-P0-2`)

#### `src/cba_kb/consumer_surface.py`
- `DomainHorizonStatus(str, Enum)`:
  - `VERIFIED_PRESENT = "VERIFIED_PRESENT"`
  - `OBSERVED_EMPTY = "OBSERVED_EMPTY"`
  - `SOURCE_GAP_UNKNOWN = "SOURCE_GAP_UNKNOWN"`
  - `NOT_APPLICABLE = "NOT_APPLICABLE"`
- Pure function `build_person_domain_horizons(...) -> Dict[str, Dict[str, Any]]`:
  - Signature:
    ```python
    def build_person_domain_horizons(
        *,
        entity_role: str = "PLAYER",
        has_registration_records: bool = False,
        registration_observed_until: Optional[str] = None,
        registration_source_gap: bool = False,
        has_season_stats: bool = False,
        has_game_stats: bool = False,
        stats_observed_until: Optional[str] = None,
        game_stats_degraded: bool = False,
        stats_source_gap: bool = False,
        has_official_announcements: bool = False,
        announcements_observed_until: Optional[str] = None,
        announcements_source_gap: bool = False,
        coverage_eval: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Dict[str, Any]]
    ```
  - Returns exact 3 domains: `"registration"`, `"statistics"`, `"official_announcements"`, each containing `{"status": str, "observed_until": Optional[str], "note": str}`.
  - Coach / non-player roles (e.g. `HEAD_COACH`, `ASSISTANT_COACH`, `COACH`, `主教练`, `助理教练`) return `DomainHorizonStatus.NOT_APPLICABLE` for `"statistics"`.
  - Degraded stats (`game_stats_degraded=True` or `stats_source_gap=True`) returns `DomainHorizonStatus.SOURCE_GAP_UNKNOWN` for `"statistics"` **without** polluting `"registration"` (which remains `DomainHorizonStatus.VERIFIED_PRESENT` when registration records exist).
  - Observed domain with valid `observed_until`, no source gap, and no records returns `DomainHorizonStatus.OBSERVED_EMPTY`.

#### `src/cba_kb/person_card_builder.py` & `src/cba_kb/kc_render.py`
- `build_person_card(..., entity_role: Optional[str] = None, ...)` computes `domain_horizons` via `build_person_domain_horizons(...)`, attaches `payload["domain_horizons"]`, and returns `result["domain_horizons"]`.
- `render_research_card()` renders `domain_horizons` under `## Observation Horizon` when present (and produces 0-byte diff when absent).

---

### 3.3 P0-3: Historical `_ARCHIVE.md` Migration, Unique `CURRENT` Guard & `archive_compactor` Dual-Key (`AC-232-P0-3`)

#### `src/cba_kb/consumer_surface.py` & `src/cba_kb/orchestration/release/prepare_v215.py`
- Historical archive banner format:
  `> [HISTORICAL_SEASON_ARCHIVE] 本文件为 {season} 历史赛季定格档案，非当前活跃赛季（{active_league_season}）主卡`
- `build_promoted_research_slice_artifacts(..., active_league_season: Optional[str] = None, enable_v232_card_pipeline: bool = False)`:
  - When `enable_v232_card_pipeline=True` and `active_league_season` is provided and `season < active_league_season` (e.g. `2025-2026 < 2026-2027`):
    - Emits `*_ARCHIVE.md` filenames (instead of `*_CURRENT.md`) for promoted card, slice, and excerpt artifacts.
    - Injects the exact top banner line above into the rendered Markdown while preserving trailing `end_marker: true`.
  - When `enable_v232_card_pipeline=False`, preserves exact v2.3.1 behavior (`*_CURRENT.md`, no archive banner).
- `assert_unique_current_card_per_person(targets) -> None`:
  - Accepts either a mapping `{logical_key: {"name": ...}}` or a sequence of filenames/paths/dicts.
  - Raises `ValueError` containing `"DUPLICATE_CURRENT_PERSON_CARD"` whenever the same person has two or more `研究卡_*_CURRENT.md` files (across seasons or paths).
  - Passes when historical seasons use `*_ARCHIVE.md` and at most one `*_CURRENT.md` exists per person.
  - Wired into `prepare_v215._build_instance_consumer_targets` when `enable_v232_card_pipeline=True`.

#### `src/cba_kb/archive_compactor.py`
- `_validate_authoritative_release_state(...)`:
  - Accepts both `{"status": "COMPLETE"}` and `{"state": "COMPLETE"}` in `release_status`.
  - Still fail-closes (`ArchiveCompactionError("ARCHIVE_COMPACT_INVALID_RELEASE_STATE:...")`) when both keys are missing, when the value is not `"COMPLETE"`, or when both keys are present with conflicting values.

---

### 3.4 P0-4: `--visibility internal_full | public_safe` & Leak Guard (`AC-232-P0-4`)

#### `src/cba_kb/kc_visibility.py`
- `CARD_VISIBILITY_MODES = ("internal_full", "public_safe")`
- `validate_card_visibility(visibility: str) -> str`:
  - Accepts only `"internal_full"` or `"public_safe"`.
  - Any other value raises `ValueError("INVALID_CARD_VISIBILITY")` (fail-closed).
- `verify_public_safe_card_text(markdown: str) -> None`:
  - Passes on clean `public_safe` rendered card text.
  - Raises `ValueError` containing `"PUBLIC_SAFE_REDACTION_VIOLATION"` if any leaked internal/private artifact appears:
    - Private paths (`10_sources`, `Web 剪藏`, `r2://`, `s3://`, local home/system paths, `file:///`)
    - Verbatim `## Document Passage` section or verbatim quote block lines (`> ...`)
    - Internal `doc_id` or `raw_sha256` / 64-hex hash digests
    - Raw Google Drive IDs or Drive URLs

#### `src/cba_kb/kc_render.py`, `src/cba_kb/person_card_builder.py` & `src/cba_kb/cli.py`
- `research_card_consumer_filename(player_name, player_id, season, *, visibility="internal_full") -> str`:
  - `internal_full` -> `研究卡_<name>_<id>_<season>_CURRENT.md`
  - `public_safe` -> `研究卡_<name>_<id>_<season>_PUBLIC_CURRENT.md`
- `render_research_card(..., visibility="internal_full")` and `build_person_card(..., visibility="internal_full")`:
  - Default is `"internal_full"`; invalid visibility raises `ValueError("INVALID_CARD_VISIBILITY")`.
  - Under `"public_safe"`:
    - Header includes `Visibility: PUBLIC_SAFE`.
    - Strips `## Document Passage` verbatim quote blocks (`> ...`), internal `doc_id`, `raw_sha256`, private paths (`10_sources`, `Web 剪藏`, `r2://`, `s3://`, local paths), and unverified/non-public clues.
    - Retains structured facts and public `https://` source citations.
    - Enforces `verify_public_safe_card_text(markdown)` before returning.
- CLI:
  - `cba-kb build-person-card --visibility <internal_full|public_safe>`
  - `cba-kb export-person-cards --players <csv> --season <season> --output-dir <dir> --visibility <internal_full|public_safe>` (plus standard local input flags)

---

### 3.5 P0-5: `V215_RELEASE_SPECS["v2.3.2-1"]` Registration & Zero Historical Regression (`AC-232-P0-5`)

#### `src/cba_kb/orchestration/release/prepare_v215.py`
- `V232_1_BASELINE_SHA = "985c647a459ef7a7c970445a9a37db082eaecc71"`
- `V215_RELEASE_SPECS["v2.3.2-1"]`:
  - `product_baseline_sha`: `"985c647a459ef7a7c970445a9a37db082eaecc71"`
  - `enable_consumer_surface_v215`: `True`
  - `enable_operational_kb_v220`: `True`
  - `enable_curated_compact_surface_v222`: `True`
  - `enable_observation_horizon_v230`: `True`
  - `enable_on_demand_cards_v230`: `True`
  - `enable_v231_surface_enhancements`: `True`
  - `enable_v232_card_pipeline`: `True`
  - `active_league_season`: `"2026-2027"`
  - `incremental_delta_only`: `True`
  - `archive_format`: `"bundle_v1"`
- Existing `v2.1.5-1..v2.3.1-1` entries remain intact with `enable_v232_card_pipeline` False/absent.

---

### 3.6 P0-6: Stage D Production Cutover Scope (Post-Merge & Human `Cloud & Production GO` Only)

Executed ONLY after PR merge, Release `v2.3.2` RRC/PRV PASS, and explicit Human `Cloud & Production GO`:
1. **D1**: Dry-run & execute `v2.3.2-1` `O(Δ)` release projection migrating legacy 2025-2026 promoted cards/slices/excerpts to `*_ARCHIVE.md` with archive banner and enforcing `assert_unique_current_card_per_person`.
2. **D2**: Refresh active 2026-2027 person cards with 12-metric tables / 2025-2026 prior-season baselines and 3-domain independent observation horizons.
3. **D3**: Verify `cba-kb compact-drive-archive` against live `release_status.json` (`status` / `state` dual-key) and run `export-person-cards --visibility public_safe` smoke audit with zero redaction violations.
4. **D4**: Sanitized receipt closeout on GitHub Issue `#278` and release documentation update.
