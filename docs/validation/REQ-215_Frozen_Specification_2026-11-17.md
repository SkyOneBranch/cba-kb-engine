# REQ-215 Consolidated Effective Specification (v2.1.5 — Whole-Drive Search Hygiene, Self-Describing Consumer Surface & Research Card Promotion)

- **Requirement ID**: `REQ-215`
- **Target Release**: Engine Release `v2.1.5` (PR 1) followed by Stage 4 Production Release `v2.1.5-1` (PR 2, keeping `scripts/prepare_production.py` and all pre-existing `tests/**/*.py` unaltered)
- **Baseline HEAD SHA**: `77711241828450bbff969ce65adfa464932a9877` (`v2.1.4-1`, `LACD v1.0.4`)
- **Package Version Invariant**: `pyproject.toml` `[project].version` remains `"2.0.2"` per public readiness contract
- **GitHub Issue**: `#250` (`T2`)
- **Governance**: `LACD v1.0.4` (`pull_request_required=true`, `human_merge_required=true`, `bind_to_commit_sha=true`, `fail_closed=true`)
- **Debt & Scope Boundary**:
  - Pre-existing `tests/**/*.py` files remain strictly unmodified (zero edits, renames, symlinks, or deletions).
  - `scripts/prepare_production.py` remains byte-frozen at SHA-256 `0ccb79429fb4ec5e270a3154af987475ae75bd4ef81de48c1e665a3b93cf4573` (bound by `tests/test_req214_1_lacd_upgrade_acceptance.py`), with `RELEASE_SPECS` untouched (`tests/test_v214_release_spec.py`). New `v2.1.5+` orchestration lives in `src/cba_kb/orchestration/release/prepare_v215.py` (`src/**/release/**` -> `T2`), with `V215_RELEASE_SPECS = {}` in PR 1 and `"v2.1.5-1"` registered in Stage 4 PR 2.
  - Local `build_local_projection` continues to emit `research_card.md` and 9 manifest files by default for backward compatibility with existing frozen contracts.
  - `REQ-212` open security obligations (`#235`, `#237`) and offline collector automation (`cba-kb-collector`) remain independent and are excluded from `REQ-215`.
  - No private corpus, local filesystem paths, credentials, or raw Drive IDs may enter the public repository.

## 1. Core Principles

- `Statement != Fact. Evidence != Fact. Unknown != 0.`
- **Whole-Drive Search First**: OpenAI Google Drive Connector `v0.1.23` operates without a manual picker (`PICKER_BLOCKED`), searching across the connected account by keyword and citing via inline file links/chips. Every consumer artifact must be self-describing in Chinese, free of raw Drive ID noise, and uncontested by archived loose spreadsheets.
- **Deterministic Provenance & Honest Fallback**: Dynamic control-surface counts replace stale static literals at build time; known league club IDs resolve cleanly to official Chinese club names while unknown IDs fail cleanly to explicit unresolved markers without guessing.

## 2. Frozen Acceptance Criteria (`AC-215`)

- **`AC-215-P0-1` (Whole-Drive Search Hygiene — Plan A, Operational Stage B)**:
  - Read-only inventory of legacy loose native Google Spreadsheets and search-polluting closed canary/sandbox directories on `Drive A` (zero overlap with active production `524` targets, `archive_id`, or `releases/` rollback snapshots) presented for explicit Human approval prior to in-place `.zip` cold archival and trashing.

- **`AC-215-P0-2` (Consumer Surface Self-Describing Naming, Dynamic Row Binding & Field Disambiguation)**:
  - **`AC-215-P0-2A` (Self-Describing `CURRENT` Consumer Surface Exports)**: `cba_kb.consumer_surface` provides deterministic builders for consumer-facing self-describing `CURRENT` artifacts including `CBA_国内球员注册权威总表_2017-2027_CURRENT.csv` and `CBA_核心数据表字段字典与权威口径_CURRENT.md` plus control-document consumer guide blocks (`render_consumer_surface_guide_section`, `render_consumer_field_dictionary_md`).
  - **`AC-215-P0-2B` (Dynamic `MASTER` Row Count Binding)**: `compute_master_row_count(data, is_xlsx=False)` computes exact `MASTER` data rows at build time (e.g. `3,465`, failing closed on empty data). `cba_kb.orchestration.release.prepare_v215` dynamically passes the computed count into generated consumer control surfaces (`entry/README`, `derived/INDEX.md`, `entry/context`, `CURRENT_VERSION_DOC`) instead of relying on static `3,451`; `docs/MASTER_MIGRATION_NOTE.md` and consumer guides explicitly mark `3,451` (`3451`) as historical `2026-09-10` v1.1 baseline superseded by live build-time counts.
  - **`AC-215-P0-2C` (`date_year_inferred` vs `source_registry` Disambiguation)**: Consumer guide sections and field dictionary explicitly distinguish row-level `date_year_inferred` (`facts/CBA_球员注册_EVENTS.xlsx#registration_status_events`, inferred event year on month-day cancellation rows) from file-level `season` and discovery metadata in `source_registry`.

- **`AC-215-P0-3` (Research Card Self-Describing Upgrade, Chinese Club Name Dictionary & Production `current` Promotion)**:
  - **`AC-215-P0-3A` (Authoritative 20-Club `team_id -> 中文俱乐部标准名称` Mapping)**: `cba_kb.aliases` defines immutable `CBA_TEAM_ID_TO_ZH` and `CBA_TEAM_ID_TO_CANONICAL_ID` aligned with `config/club_aliases.yaml` across all 20 active clubs (including `"29131": "深圳马可波罗"`), plus `resolve_team_id_zh(team_id)` (returns `None` on unknown IDs without guessing) and `format_club_team_display(team_id, team_name=None)`.
  - **`AC-215-P0-3B` (Self-Describing Research Card & Backward-Compatible Promotion)**:
    - `cba_kb.kc_render` provides `research_card_consumer_filename(player_name, player_id, season)` (`研究卡_{player_name}_{player_id}_{season}_CURRENT.md`) and `slice_consumer_filename(...)`.
    - When `posture != "UNKNOWN"`, `ConsumeSession.consume_answer` enriches `scope_meta` with `player_id`, `player_name`, and resolved `club_name_zh` while leaving `UNKNOWN` unauthorized vs nonexistent outputs byte-identical; `render_research_card` renders self-describing H1 (`# 人物研究卡｜{player_name}（{player_id}）｜{season}｜CURRENT — Research Card (Serving Projection)`) and `- Club / Team: <zh> (team_id=<id>)` (or explicit unresolved fallback for unknown IDs).
    - `build_local_projection` continues to emit `research_card.md` and 9 manifest files by default while recording `consumer_research_card_filename` and `consumer_slice_filename` in `manifest.json`.
    - `cba_kb.consumer_surface.build_promoted_research_slice_artifacts(...)` deterministically materializes verified `CURRENT` research slice markdown artifacts (`research/card_*`, `research/slice_*`, `research/passage_*`) for Stage 4 production `current` publishing.

- **`AC-215-P0-4` (Oracle Connector `v0.1.23` Protocol Adaptation & Lexical Decoupling)**:
  - **`AC-215-P0-4A` (`oracle-scoring-v8` Inline Citation Protocol)**: `cba_kb.oracle.SCORING_SPEC_VERSION == "oracle-scoring-v8"` bound in `Oracle.artifact_digest`. `CONNECTOR_CITATION_PROTOCOLS` includes `"INLINE_LINK_CHIP_V0123"`. `extract_v0123_inline_locators(verbatim_answer, authorized_locators=())` extracts locator identifiers strictly from structured inline Drive/Docs links or citation chips and returns `()` for unlinked plain prose mentions (`validate_receipt` and `score_answer` still fail closed on empty `cited_locators`). `build_v0123_client_receipt(...)` constructs `ClientReceipt` bound to inline citations.
  - **`AC-215-P0-4B` (Filename & URL Lexical Decoupling)**: `_verbatim_numbers()` in `cba_kb.oracle` excludes digits embedded in inline citation URLs, data/doc filenames with extensions (including Unicode CJK research card filenames containing `player_id` and `season`), and numbered prefixes (`00_`, `02_`, `03_`, `03-`), while preserving strict numeric consistency checking on prose numbers (including CJK-suffixed values such as `41场`, `20.1分`).

- **`AC-215-P1` (Consumer View Raw `drive_file_id` Redaction)**:
  - `export_consumer_source_registry_csv(raw_csv_bytes)` removes raw `drive_file_id` and `current_parent_id` columns, hashes `drive:<raw_id>` into deterministic `src:<hash12>`, and retains logical metadata and readable titles.
  - `export_consumer_document_index(raw_index_bytes)` removes raw `drive_file_id`, `current_parent_id`, and raw Drive links while retaining `doc_id` and titles.
  - `_content_preserving_candidate` in `cba_kb.orchestration.release.prepare_v215` renders `## Current target mapping` with human-readable artifact names instead of raw Drive file IDs and scrubs raw Drive-ID morphology tokens from preserved consumer navigation blocks (preserving 40-hex Git commit SHAs and internal `control/drive_map.yaml` / `input/manifest.csv` transport bindings).

- **`AC-215-T0` (Documentation & Boundary Alignment)**:
  - `README.md` and `README.zh-CN.md` update recent milestones through `v2.1.4-1` (retaining `"2.0.2"` package-version reference) and clarify that Issue `#169` is closed and `drive_search` / `drive_search_index` enforce fail-closed per-`principal` authorization assuming a trusted caller-supplied identity context.
