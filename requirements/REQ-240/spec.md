# REQ-240 (v2.4.0): Four-Entity Core Model, Wiki-Style Elastic Cards, Story Projection, `PUBLIC_SAFE` Static Site & `v2.4.0-1` Release Spec

- **Issue**: `#280`
- **Branch**: `muse/issue-280`
- **Risk Tier**: `T2` (Bot Broker + `status:hold` + Independent Q1+Q2 Review + Human Approval)
- **Baseline**: `origin/main @ 3b65bcfcc9365d3f081150f516b9dba679ec965b` (Release `v2.3.2`)
- **Governance**: LACD v1.0.4 (`Statement != Fact. Evidence != Fact. Unknown != 0. Observed-until != Now; Silence != Cause.`)
- **Production Isolation (Stage A–C)**: Zero live Google Drive `Drive A` writes, zero live Backblaze B2 / Cloudflare R2 writes, zero cross-repo edits.
- **Clean-Room & Frozen-File Guardrails**:
  - `G5 == 0` (zero real Google Drive IDs, zero local absolute paths such as `<local-home>/...`, zero real private corpus).
  - `G6 == 0` (zero real Shenzhen player stats, newspaper/WeChat excerpts, or clippings; 100% synthetic fixtures).
  - 0-byte frozen files: `scripts/prepare_production.py`, `pyproject.toml`, `requirements.lock`, `.github/**`, `legacy/**`, and all pre-existing `tests/**/*.py`.

---

## 1. Objective

Upgrade `cba-kb-engine` to `v2.4.0` across five cohesive pillars while preserving **100% backward compatibility (0-byte diff on default calls)** with `v2.1.5-1..v2.3.2-1`:

1. **P0-1** — Four-entity core model: lifetime `PersonRegistry` (`cba-kb.person-registry.v1`, `PERS-*`), 3-facet `CoachRoleFacets` (`league_listed_role`, `club_appointed_role`, `gameday_acting_role`), `StoryRecord` (`cba-kb.story-record.v1`, `story_<24hex>`) with 1-source-to-3-targets projection (`project_stories_for_entity`), and `OrganizationProfile` (`cba-kb.organization.v1`).
2. **P0-2** — Wiki-style elastic person cards (`card_style="wiki_v240"`), club panorama card (`club_card_builder.py` + `cba-kb build-club-card`), and 4 code-enforced editorial iron rules.
3. **P0-3** — `PUBLIC_SAFE` static HTML preview site builder (`static_site_builder.py` + `cba-kb build-public-site`, Python stdlib only, fail-closed with 0 partial output on visibility violation).
4. **P0-4** — `1-1` `<=120`-character sentence-attributed brief (`compose_verified_brief` + `cba-kb compose-verified-brief`) and 4-hard-case pilot validator (`verify_shenzhen_pilot_hard_cases`).
5. **P0-5** — `V215_RELEASE_SPECS["v2.4.0-1"]` registration bound to `3b65bcfcc9365d3f081150f516b9dba679ec965b` with `enable_v240_shenzhen_narrative_pilot=True`, plus closure of O-232-01 (controlled remote filename rename), O-232-03 (`cloud_state.py` allowlist expansion), and O-232-04 (`kc_visibility.py` `.casefold()` + `~~~` fence + `[INTERNAL_CLUE_ONLY` guard).

---

## 2. Explicit Non-Goals

- **No breaking changes to legacy payloads or historical release specs**: Default `render_research_card()` / `build_person_card()` calls (without `card_style="wiki_v240"`) and `v2.1.5-1..v2.3.2-1` release specs MUST preserve exact backward compatibility (0-byte diff).
- **No external runtime dependencies**: `static_site_builder.py` and all new modules MUST use Python standard library only (`pyproject.toml` and `requirements.lock` remain 0-byte frozen).

---

## 3. Frozen Public Contracts & Acceptance Criteria

### 3.1 P0-1: Four-Entity Core Model (`AC-240-P0-1`)

#### `src/cba_kb/person_registry.py`
- Constants:
  - `PERSON_REGISTRY_SCHEMA = "cba-kb.person-registry.v1"`
  - `PERSON_UID_RE` matching `actor.py:_PERSON_UID` (`r"PERS-[A-Za-z0-9_-]{8,64}\Z"`)
- Exception:
  - `class PersonRegistryError(ValueError): ...`
- Dataclasses:
  - `RoleTenure`:
    - `role: str` (e.g. `"PLAYER"`, `"HEAD_COACH"`, `"ASSISTANT_COACH"`, `"ACTING_HEAD_COACH"`, `"COACH"`, `"EXECUTIVE"`, `"STAFF"`)
    - `organization_id: Optional[str] = None`
    - `organization_name: Optional[str] = None`
    - `season_start: Optional[str] = None`
    - `season_end: Optional[str] = None`
    - `start_date: Optional[str] = None`
    - `end_date: Optional[str] = None`
    - `authority_scope: str = "LEAGUE_OFFICIAL"`
    - `evidence_refs: Tuple[str, ...] = ()`
    - `.to_dict() -> Dict[str, Any]`
  - `PersonProfile`:
    - `person_uid: str` (validated with `actor.validate_person_uid`)
    - `canonical_name: str`
    - `aliases: Tuple[str, ...] = ()`
    - `linked_player_uids: Tuple[str, ...] = ()`
    - `role_tenures: Tuple[RoleTenure, ...] = ()`
    - `record_links: Tuple[Dict[str, Any], ...] = ()`
    - `birth_year: Optional[int] = None`
    - `status: str = "ACTIVE"`
    - `.to_dict() -> Dict[str, Any]`
- Functions:
  - `make_person_registry(persons: Sequence[PersonProfile | Mapping[str, Any]] = (), *, schema: str = PERSON_REGISTRY_SCHEMA) -> Dict[str, Any]`
  - `validate_person_registry(registry: Mapping[str, Any]) -> Dict[str, Any]`:
    - Enforces `schema == "cba-kb.person-registry.v1"`, valid unique `PERS-*` IDs, and no duplicate `linked_player_uids` across distinct persons (raises `PersonRegistryError("DUPLICATE_LINKED_PLAYER_UID:...")`).
  - `resolve_person_identity(query: str, *, person_registry: Mapping[str, Any], player_uid: Optional[str] = None, record_key: Optional[str] = None, organization_id: Optional[str] = None) -> Dict[str, Any]`:
    - Returns `{"status": "RESOLVED" | "UNDECIDED" | "NOT_FOUND", "actor_kind": "person" | "unresolved", "person_uid": Optional[str], "person": Optional[Dict[str, Any]], "candidates": List[Dict[str, Any]], "reason": str}`.
    - Resolution priority:
      1. Exact `person_uid` match -> `RESOLVED`.
      2. Exact `player_uid` match in `linked_player_uids` -> `RESOLVED` (unifies player->coach lifecycle under the single `PERS-*` profile).
      3. Exact `record_key` match in explicit `record_links` (`link_status == "same"`) -> `RESOLVED`.
      4. Name/alias match: unique hit -> `RESOLVED`; `>= 2` homonym candidates without `player_uid` or explicit `record_links` -> **Fail-Closed** `status="UNDECIDED"`, `actor_kind="unresolved"`, `person_uid=None`, `reason="HOMONYM_WITHOUT_EXPLICIT_LINK"`.
      5. No match -> `status="NOT_FOUND"`, `actor_kind="unresolved"`, `person_uid=None`.
  - `unify_player_and_coach_profile(person_uid: str, *, person_registry: Mapping[str, Any]) -> Dict[str, Any]`:
    - Returns unified lifetime view with `person_uid`, `canonical_name`, `linked_player_uids`, chronologically sorted `role_tenures`, `has_player_tenure`, `has_coach_tenure`, `is_player_to_coach_transition`.

#### `src/cba_kb/role_facets.py`
- Constants:
  - `OBSERVED_EMPTY = "OBSERVED_EMPTY"`
  - `INSTITUTIONAL_DISCREPANCY_CODE = "LEAGUE_VS_CLUB_ROLE_DISCREPANCY"`
- Dataclass `CoachRoleFacets`:
  - `person_uid: Optional[str]`
  - `person_name: str`
  - `organization_id: Optional[str]`
  - `season: str`
  - `league_listed_role: str`
  - `club_appointed_role: str`
  - `gameday_acting_role: str = "OBSERVED_EMPTY"`
  - `has_institutional_discrepancy: bool = False`
  - `discrepancy_code: Optional[str] = None`
  - `discrepancy_explanation: Optional[str] = None`
  - `evidence_refs: Tuple[str, ...] = ()`
  - `.to_dict() -> Dict[str, Any]`
- Function:
  ```python
  def build_coach_role_facets(
      *,
      person_name: str,
      season: str,
      person_uid: Optional[str] = None,
      organization_id: Optional[str] = None,
      staff_snapshots: Sequence[Mapping[str, Any]] = (),
      league_listed_role: Optional[str] = None,
      club_appointed_role: Optional[str] = None,
      gameday_acting_role: Optional[str] = None,
      evidence_refs: Sequence[str] = (),
  ) -> CoachRoleFacets
  ```
  - Reads `league_listed_role` from `team_staff_snapshots.jsonl` records (`staff_snapshots`) when not explicitly supplied.
  - Normalizes absent/empty `gameday_acting_role`, `league_listed_role`, or `club_appointed_role` to `"OBSERVED_EMPTY"`.
  - When `league_listed_role` and `club_appointed_role` are both non-empty (neither `"OBSERVED_EMPTY"`) and differ, automatically sets `has_institutional_discrepancy=True`, `discrepancy_code="LEAGUE_VS_CLUB_ROLE_DISCREPANCY"`, and a non-empty `discrepancy_explanation` citing both roles and explaining the league-registration vs club-appointment governance distinction.

#### `src/cba_kb/story_record.py`
- Constants:
  - `STORY_RECORD_SCHEMA = "cba-kb.story-record.v1"`
  - `ORGANIZATION_PROFILE_SCHEMA = "cba-kb.organization.v1"`
  - `PARTICIPANT_ROLES = frozenset({"ACTION_INITIATOR", "SUBJECT_INTERVIEWEE", "CHRONICLER", "CO_PARTICIPANT"})`
  - `STORY_ID_RE = re.compile(r"story_[0-9a-f]{24}\Z")`
- Exception:
  - `class StoryRecordError(ValueError): ...`
- Dataclass `OrganizationProfile`:
  - `organization_id: str`
  - `canonical_name: str`
  - `short_name: Optional[str] = None`
  - `aliases: Tuple[str, ...] = ()`
  - `league: str = "CBA"`
  - `city: Optional[str] = None`
  - `schema: str = ORGANIZATION_PROFILE_SCHEMA`
  - `.to_dict() -> Dict[str, Any]`
- Functions:
  - `generate_story_id(*, title: str, occurred_season: str, canonical_source: Mapping[str, Any] | str) -> str` (deterministic `story_<24hex>`).
  - `validate_story_record(record: Mapping[str, Any]) -> Dict[str, Any]`:
    - Requires `schema == "cba-kb.story-record.v1"`, `story_id` (`story_<24hex>`), `title`, `occurred_season` (explicitly decoupled from `published_at` / clip filename date), `occurred_date` (nullable), `published_at`, `canonical_source`, `deduped_reprints` (list), `participants` (non-empty list of dicts with `person_uid`, `display_name`, `role` in `PARTICIPANT_ROLES`, `internal_verbatim_or_note`, `public_perspective_summary`), `organization_ids` (list), and `visibility_policy`.
  - `make_story_record(...) -> Dict[str, Any]`
  - `project_stories_for_entity(stories: Sequence[Mapping[str, Any]], *, entity_id: str, entity_kind: str = "person", visibility: str = "internal_full") -> List[Dict[str, Any]]`:
    - Projects 1 `StoryRecord` to all referenced targets (each participant `person_uid` with `entity_kind="person"` and each `organization_id` with `entity_kind="organization"`).
    - Preserves `occurred_season` independently of `published_at`.
    - Under `visibility="public_safe"`, strips `internal_verbatim_or_note`, emits `public_perspective_summary`, and excludes stories whose `visibility_policy` disallows public export or whose `canonical_source.source_kind == "unreferenced_encyclopedia"`.

---

### 3.2 P0-2: Wiki-Style Elastic Cards, Club Panorama Card & 4 Editorial Iron Rules (`AC-240-P0-2`)

#### `src/cba_kb/kc_render.py` & `src/cba_kb/person_card_builder.py`
- New optional parameter `card_style: str = "classic"` (valid values: `"classic"`, `"wiki_v240"`; invalid values raise `ValueError("INVALID_CARD_STYLE:...")`).
- **0-Byte Backward Compatibility**: Default `card_style="classic"` produces an exact 0-byte diff with v2.3.2.
- Under `card_style="wiki_v240"`:
  - **Elastic section growth**:
    - Pure coach (`entity_role` in coach roles and no player season stats) omits `### Season Averages (Player-Season Grain)` and `## Recent Single-Game Log (Player-Game Grain)` while keeping `domain[statistics]: status=NOT_APPLICABLE` in `## Observation Horizon`, and renders `## Coaching Role Facets` when `coach_role_facets` is provided.
    - Player-to-coach unified profile renders `## Career Role Tenures` under the single `PERS-*` profile.
    - Projected stories render under `## Narrative & Key Stories`.
  - **4 Code-Enforced Editorial Iron Rules** (exported as testable helpers in `cba_kb.kc_render` and wired into `wiki_v240` rendering):
    1. `enforce_authority_scope(records)`: binds every claim/fact/timeline record to its declared `authority_scope`; prevents non-official scopes (e.g. `PUBLIC_MEDIA`) from asserting official league registration slots.
    2. `filter_timeline_events_silence_not_cause(events)`: rejects unsubstantiated rumor/injury/absence causes (`cause_status in ("UNSUBSTANTIATED", "RUMOR", "INFERRED_FROM_ABSENCE")` or `event_kind == "RUMOR_CAUSE"` or `unverified_cause is True`) from the main timeline while preserving multi-source factual discrepancies (`event_kind == "MULTI_SOURCE_DISCREPANCY"` across candidate/warmup/final roster/club post).
    3. `dedup_reprints_and_preserve_discrepancies(items)`: deduplicates same-source reprints (`reprint_of` / shared `canonical_source_id`) into a single canonical entry with `deduped_reprints`, while keeping independent conflicting sources side-by-side.
    4. `unreferenced_encyclopedia` quarantine: items with `source_kind == "unreferenced_encyclopedia"` are quarantined as `[INTERNAL_CLUE_ONLY: ...]` in `internal_full` and 100% physically stripped in `public_safe` (and blocked by `verify_public_safe_card_text()`).

#### `src/cba_kb/club_card_builder.py` & CLI `cba-kb build-club-card`
- Function:
  ```python
  def build_club_panorama_card(
      club_query: str,
      *,
      seasons: Sequence[str] = ("2025-2026", "2026-2027"),
      organization_profile: Optional[Mapping[str, Any]] = None,
      master_rows: Optional[Sequence[Mapping[str, Any]]] = None,
      foreign_rows: Optional[Sequence[Mapping[str, Any]]] = None,
      staff_snapshots: Optional[Sequence[Mapping[str, Any]]] = None,
      coach_role_facets: Optional[Sequence[Any]] = None,
      story_records: Optional[Sequence[Mapping[str, Any]]] = None,
      visibility: str = "internal_full",
      output_dir: Optional[str | Path] = None,
      observed_until: Optional[str] = None,
  ) -> Dict[str, Any]
  ```
- Aggregates 2-season roster (domestic + foreign), 3-facet coaching staff, and organization-projected story timeline.
- Enforces `validate_card_visibility(visibility)` and `verify_public_safe_card_text(markdown)` when `visibility == "public_safe"`.
- CLI: `cba-kb build-club-card --club <club> --seasons <csv> --visibility <internal_full|public_safe> [--organization-profiles <path>] [--master-csv <path>] [--foreign-csv <path>] [--staff-snapshots <path>] [--story-records <path>] [--output-dir <dir>] [--format json|markdown]`.

---

### 3.3 P0-3: `PUBLIC_SAFE` Static HTML Preview Site (`AC-240-P0-3`)

#### `src/cba_kb/static_site_builder.py` & CLI `cba-kb build-public-site`
- Python stdlib only.
- Function:
  ```python
  def build_public_site(
      *,
      club_card: Mapping[str, Any],
      person_cards: Sequence[Mapping[str, Any]],
      output_dir: str | Path,
      site_title: Optional[str] = None,
      observed_until: Optional[str] = None,
  ) -> Dict[str, Any]
  ```
- **Fail-Closed Visibility Guard (0 partial output)**:
  - Rejects any card whose `visibility != "public_safe"` or whose markdown fails `verify_public_safe_card_text(...)` (including `[INTERNAL_CLUE_ONLY]`, private paths, blockquotes, `~~~` fences, `doc_id`, `raw_sha256`, Drive IDs) by raising `ValueError("PUBLIC_SITE_VISIBILITY_VIOLATION:...")` **before** writing any file to `output_dir`.
- Outputs:
  - `index.html`: club panorama with links to `persons/<slug>.html` and Schema.org JSON-LD (`@type: "SportsTeam"` + `observed_until`).
  - `persons/<slug>.html`: person page with bidirectional link to `../index.html`, co-entity links, footnote anchors `[1]` (`href="#fn-1"` / `id="fn-1"`), and Schema.org JSON-LD (`@type: "Person"` + `observed_until`).
  - `assets/style.css`: self-contained CSS.
- CLI: `cba-kb build-public-site --club-card <json_path> --person-cards-dir <dir> --output-dir <dir> [--site-title <title>] [--observed-until <iso>]`.

---

### 3.4 P0-4: `1-1` `<=120`-Char Sentence-Attributed Brief & 4 Hard-Cases Validator (`AC-240-P0-4`)

#### `src/cba_kb/editorial_brief.py` & CLI `cba-kb compose-verified-brief`
- Constant `MAX_BRIEF_CHARS = 120`.
- `compose_verified_brief(facts: Sequence[Mapping[str, Any]], *, entity_name: str, max_chars: int = MAX_BRIEF_CHARS, visibility: str = "public_safe") -> Dict[str, Any]`:
  - Excludes any fact with `status in ("SOURCE_GAP_UNKNOWN", "UNDECIDED")` or `source_kind == "unreferenced_encyclopedia"`.
  - Raises `ValueError("NO_VERIFIED_FACTS_FOR_BRIEF")` if no eligible verified sentences remain.
  - Produces `body` with `len(body) <= max_chars`, `sentence_attributions` mapping 1-to-1 to every sentence in `body`, and verifies `verify_public_safe_card_text(body)` under `public_safe`.
- `verify_shenzhen_pilot_hard_cases(bundle: Mapping[str, Any]) -> Dict[str, Any]`:
  - Validates all 4 hard cases (`case_1_coach_role_discrepancy`, `case_2_player_to_coach_unified`, `case_3_story_one_to_three_projection`, `case_4_multi_source_discrepancy_and_silence_not_cause`); returns `{"status": "PASS", "passed_cases": [...], "failed_cases": []}` on valid bundle and raises `ValueError("SHENZHEN_PILOT_HARD_CASE_FAILED:...")` on tampered inputs.
- CLI: `cba-kb compose-verified-brief --entity <name> --facts <json_path> [--max-chars 120] [--visibility public_safe] [--output <path>]`.

---

### 3.5 P0-5: `v2.4.0-1` Release Spec & O-232-01 / O-232-03 / O-232-04 Closure (`AC-240-P0-5`)

#### `src/cba_kb/orchestration/release/prepare_v215.py`
- `V240_1_BASELINE_SHA = "3b65bcfcc9365d3f081150f516b9dba679ec965b"`
- `V215_RELEASE_SPECS["v2.4.0-1"]`:
  - `product_baseline_sha`: `"3b65bcfcc9365d3f081150f516b9dba679ec965b"`
  - `enable_consumer_surface_v215`: `True`
  - `enable_operational_kb_v220`: `True`
  - `enable_curated_compact_surface_v222`: `True`
  - `enable_observation_horizon_v230`: `True`
  - `enable_on_demand_cards_v230`: `True`
  - `enable_v231_surface_enhancements`: `True`
  - `enable_v232_card_pipeline`: `True`
  - `enable_v240_shenzhen_narrative_pilot`: `True`
  - `active_league_season`: `"2026-2027"`
  - `incremental_delta_only`: `True`
  - `archive_format`: `"bundle_v1"`
- Historical `v2.1.5-1..v2.3.2-1` entries remain intact with `enable_v240_shenzhen_narrative_pilot` absent/False.

#### O-232-01: Controlled Remote Filename Rename (`src/cba_kb/release.py` & `src/cba_kb/orchestration/release/prepare_v215.py`)
- `project_targets()` supports renaming an existing non-protected consumer `logical_key` (`consumer/*` or `research/*`, not in `CONTROL_KEYS` or `retiring_keys`) when `spec["name"] != existing[ckey]["name"]` (e.g. `*_CURRENT.md -> *_ARCHIVE.md`), recording `rename_from` and marking disposition `"modified"`.
- `release.publish()` applies the controlled remote filename rename (`drive.rename(entry["id"], entry["name"])`) during publication, updates `release_status.json` with the new filename, and `release.verify()` asserts remote `meta["name"] == entry["name"]` (failing closed if rename was not applied).
- `_manifest_candidate()` keeps `manifest.csv` synchronized with the updated consumer entry.

#### O-232-03: `src/cba_kb/cloud_state.py` Single-Bundle Allowlist
- `_bundle_allowlisted_exact_names()` adds:
  - `team_staff_snapshots.jsonl`
  - `person_registry.json`
  - `story_records.jsonl`
  - `organization_profiles.json`
- `_is_bundle_member_allowed()` allows `clubs/*.md` in addition to `cards/*.md` and `on_demand_cards/*.md`.
- Continues to fail-closed on `document_lane/archive/raw/*`, `evidence/*`, `inbox/*`, `.credentials/*`, `production.json`, and secrets.

#### O-232-04: `src/cba_kb/kc_visibility.py` & `src/cba_kb/kc_render.py` Hardening
- `verify_public_safe_card_text()` blocks `~~~` fenced code blocks and `[INTERNAL_CLUE_ONLY` markers (case-insensitive), raising `ValueError("PUBLIC_SAFE_REDACTION_VIOLATION:...")`.
- `kc_render.py` and `person_card_builder.py` use `.casefold()` when comparing citation `title == doc_id` under `public_safe`, and `_sanitize_public_free_text()` strips `~~~` fenced blocks.
