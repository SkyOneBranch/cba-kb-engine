# REQ-241 (v2.4.1): Wikipedia 2.0 Site, Multi-Season Career Tables, Sentence-Level Citations, GEO Assets & Local Draft Extractor

- **Issue**: `#283`
- **Branch**: `muse/issue-283`
- **Risk Tier**: `T2` (Bot Broker + `status:hold` + Independent Q1+Q2 Review + Human Approval)
- **Baseline**: `origin/main @ aeb630c8211799fe91f05cd06b18301a55c823f2` (Release `v2.4.0`)
- **Governance**: LACD v1.0.4 (`Statement != Fact. Evidence != Fact. Unknown != 0. Observed-until != Now; Silence != Cause.`)
- **Production Isolation (Stage A–C)**: Zero live Google Drive `Drive A` writes, zero live Backblaze B2 / Cloudflare R2 writes, zero cross-repo edits, zero network calls.
- **Clean-Room & Frozen-File Guardrails**:
  - `G5 == 0` (zero real Google Drive IDs, zero local absolute paths, zero real private corpus).
  - `G6 == 0` (zero real Shenzhen player stats, newspaper/WeChat excerpts, or clippings; 100% synthetic fixtures).
  - 0-byte frozen files: `scripts/prepare_production.py`, `pyproject.toml`, `requirements.lock`, `.github/**`, `legacy/**`, and all pre-existing `tests/**/*.py`.

---

## 1. Objective

Upgrade `cba-kb-engine` to `v2.4.1` across five cohesive pillars while preserving **100% backward compatibility** with `v2.1.5-1..v2.4.0-2` (`classic` and `wiki_v240` card styles):

1. **P0-1 (Wikipedia 2.0 Hero)** — `card_style="wiki_v241"`, clean Chinese `<h1>` (including pure coaches and transitioned coaches with 0 current-season player stats), inverted-pyramid Chinese lead paragraph (`compose_wiki_lead_paragraph`, 80–180 chars with inline `[1][2]` superscripts + `<meta name="description">`), `PersonBioProfile` (`cba-kb.person-bio-profiles.v1`) + right-floating `<aside class="wiki-infobox">` (auto-hiding missing fields, never emitting `0` or `unassigned`), and default-collapsed `<details class="audit-drawer">` with non-contradictory top-level domain summary when any domain is `VERIFIED_PRESENT`.
2. **P0-2 (Multi-Season Career Tables + Chinese Opponent Mapping + D-02 + Zero Broken Links)** — Cross-season Regular Season + Playoffs career tables (`build_career_season_stats_tables`, no double-counting `WHOLE_SEASON` vs `TEAM_SPLIT`, supports cross-team seasons & transitioned coaches' historical player stats, gracefully omits player stats for pure coaches), `resolve_cba_team_name` numeric team ID -> Chinese team name mapping, default-collapsed `<details class="gamelog-drawer">` for recent 5 games, two-season club card covering active + departed members with offseason roster changes (`offseason_roster_changes`), and static site generating pages for all active + departed members with 0 broken internal links.
3. **P0-3 (Sentence-Level `[n]` Citations, Wiki References, Story Dedup, `doc_<hex>` Block)** — Unified `CitationRegistry` deduplicating registration, stats, coach role, bio profile, `StoryRecord.canonical_source`, and multi-source timeline citations (including URL-less newspaper/official WeChat citations); inline superscript `<a class="fn-ref" href="#fn-k" id="fnref-k">[k]</a>` with back-link `↩` to a single bottom `参考资料` section (removing dangling `Sources: [1]...` and duplicate `External Citations` for `wiki_v241`); `verify_public_safe_card_text()` fail-closed blocking `\bdoc_[0-9a-f]{16,64}\b` and `\(unassigned\)`; `StoryRecord.organization_summary` (and single primary summary fallback when omitted, never concatenating multiple participant summaries); natural Chinese Wikipedia section headings.
4. **P0-4 (GEO `sitemap.xml` + `llms.txt` + Relationship-Driven Related Persons & Navbox)** — Top-8 relationship-driven `关联人物` (shared StoryRecord / coach-player / draft-youth cohort with Chinese relation labels), collapsible bottom `<nav class="squad-navbox">`, GEO `sitemap.xml` and `llms.txt` (both public-safe verified), and enhanced Person JSON-LD (`description`, `jobTitle`, `height`, `weight`, `birthDate`, `citation`).
5. **P0-5 (Local Draft Extractor & `v2.4.1-1` / `v2.4.1-2` Release Spec)** — `src/cba_kb/wiki_draft_extractor.py` + CLI `cba-kb draft-wiki-candidates` (100% local, zero network, all candidates tagged `review_status="DRAFT_PENDING_HUMAN_REVIEW"`, 0 auto-writes to canonical stores); `V215_RELEASE_SPECS["v2.4.1-1"]` & `"v2.4.1-2"` bound to `aeb630c8211799fe91f05cd06b18301a55c823f2` with `enable_v241_wikipedia_site_v2=True`; `cloud_state.py` single-bundle allowlist includes `person_bio_profiles.json`.

---

## 2. Explicit Non-Goals

- **No breaking changes to legacy contracts**: Default `card_style="classic"` and `card_style="wiki_v240"`, plus `v2.1.5-1..v2.4.0-2` release specs, MUST remain 100% backward compatible.
- **No third-party dependencies**: All new and modified code MUST use Python standard library only (`pyproject.toml` and `requirements.lock` remain 0-byte frozen).
- **No automated canonical writes**: `wiki_draft_extractor.py` is strictly read-only on canonical stores and outputs human-review drafts only.

---

## 3. Frozen Public Contracts & Acceptance Criteria

### 3.1 P0-1: Wikipedia 2.0 Hero (`AC-241-P0-1`)

#### `src/cba_kb/kc_render.py`
- `CARD_STYLES = ("classic", "wiki_v240", "wiki_v241")`
- `validate_card_style("wiki_v241") -> "wiki_v241"`
- Under `card_style="wiki_v241"`:
  - **Clean Chinese `<h1>`**: Top markdown heading MUST be `# {person_name}` (e.g. `# 林启明`), including for pure coaches and transitioned coaches with 0 current-season player stats (`posture == "UNKNOWN"`).
  - **Eliminated internal tokens**: Rendered markdown MUST NOT contain `Research Card (Serving Projection)`, `人物研究卡｜`, `pid_[0-9a-f]{32}`, or `(unassigned)`.
  - **Natural Chinese Wikipedia section headings**:
    - `## 球员生涯数据` (with `### 常规赛` and `### 季后赛` subsections when stats exist; omitted gracefully for pure coaches with 0 player stats)
    - `## 近期单场数据` (wrapped in `<details class="gamelog-drawer">` in HTML)
    - `## 执教履历与角色` (when `coach_role_facets` present)
    - `## 职业履历` (when `unified_profile` / `role_tenures` / registration history present)
    - `## 注册与合同信息` (when registration profile present)
    - `## 人物故事与关键事件` (when projected stories present)
    - `## 动态时间线` (when timeline events present)
    - `## 参考资料` (single unified numbered references section; NO `## External Citations` duplicate)
  - **Audit Drawer & Non-Contradictory Top-Level Domain Summary**:
    - Technical audit fields (Observation Horizon, Posture, Locator, `PERS-*`, per-domain statuses) are placed in a default-collapsed `<details class="audit-drawer">` block (no `open` attribute).
    - When ANY domain in `domain_horizons` has `status == "VERIFIED_PRESENT"`, the top-level summary MUST report a non-contradictory status (e.g. `PARTIALLY_VERIFIED` or `VERIFIED_DOMAIN_PRESENT`) and MUST NOT emit a blanket top-level `Posture: UNKNOWN` outside the audit drawer.

#### `src/cba_kb/person_registry.py`
- Constant: `PERSON_BIO_PROFILES_SCHEMA = "cba-kb.person-bio-profiles.v1"`
- Dataclass `PersonBioProfile` (frozen):
  - `person_uid: str` (validated via `validate_person_uid`)
  - `canonical_name: str`
  - `english_name: Optional[str] = None`
  - `birth_date: Optional[str] = None` (`YYYY-MM-DD`)
  - `birth_place: Optional[str] = None`
  - `nationality: Optional[str] = None`
  - `height_cm: Optional[int] = None` (must be positive int `> 0` if provided; `0` or negative raises `PersonRegistryError`)
  - `weight_kg: Optional[int] = None` (must be positive int `> 0` if provided; `0` or negative raises `PersonRegistryError`)
  - `position: Optional[str] = None`
  - `jersey_number: Optional[str] = None`
  - `current_club: Optional[str] = None`
  - `current_role: Optional[str] = None`
  - `draft_or_youth_origin: Optional[str] = None`
  - `honors: Tuple[str, ...] = ()`
  - `evidence_refs: Tuple[Any, ...] = ()`
  - `review_status: str = "VERIFIED"`
  - `.to_dict() -> Dict[str, Any]`
- Functions:
  - `make_person_bio_profiles(profiles: Sequence[PersonBioProfile | Mapping[str, Any]] = (), *, schema: str = PERSON_BIO_PROFILES_SCHEMA) -> Dict[str, Any]`
  - `validate_person_bio_profiles(bundle: Mapping[str, Any]) -> Dict[str, Any]`

#### `src/cba_kb/editorial_brief.py`
- Function:
  ```python
  def compose_wiki_lead_paragraph(
      *,
      person_name: str,
      bio_profile: Optional[Any] = None,
      registration_profile: Optional[Mapping[str, Any]] = None,
      coach_role_facets: Optional[Any] = None,
      unified_profile: Optional[Mapping[str, Any]] = None,
      career_season_stats: Optional[Mapping[str, Any] | Sequence[Mapping[str, Any]]] = None,
      projected_stories: Optional[Sequence[Mapping[str, Any]]] = None,
      citation_registry: Optional[Any] = None,
      min_chars: int = 80,
      max_chars: int = 180,
      visibility: str = "public_safe",
  ) -> Dict[str, Any]
  ```
- Returns:
  - `lead_markdown`: inverted-pyramid Chinese lead paragraph with inline `[1][2]` markers when citations are registered.
  - `lead_plain_text`: clean plain text without `[n]` markers (`len(lead_plain_text) <= max_chars`; when rich profile/stats/stories are supplied, `min_chars <= len(lead_plain_text) <= max_chars`, default 80–180 Chinese characters).
  - `char_count`: `len(lead_plain_text)`
  - `citation_indices`: `List[int]`
  - `visibility`: `"public_safe"` or `"internal_full"`
- Never emits `0`, `unassigned`, `UNKNOWN`, or `pid_...` in lead text; enforces `verify_public_safe_card_text()` under `public_safe`.

#### `src/cba_kb/static_site_builder.py` (Hero, Infobox, Audit Drawer)
- Person HTML page `<head>` includes `<meta name="description" content="...">` derived from `lead_plain_text` (or clean summary).
- Person HTML page renders `<aside class="wiki-infobox">` displaying non-empty bio/registration fields only; missing/`None`/`""`/`0`/`"0"`/`"unassigned"`/`"UNKNOWN"` fields are auto-hidden.
- Audit details are wrapped in default-collapsed `<details class="audit-drawer">` (no `open` attribute).
- `assets/style.css` includes rules for `.wiki-infobox`, `.audit-drawer`, `.gamelog-drawer`, `.related-persons`, and `.squad-navbox`.

---

### 3.2 P0-2: Multi-Season Career Tables + Chinese Opponent Mapping + D-02 + Zero Broken Links (`AC-241-P0-2`)

#### `src/cba_kb/person_card_builder.py`
- New helper:
  ```python
  def build_career_season_stats_tables(
      person: Mapping[str, Any],
      season_stats_records: Sequence[Mapping[str, Any]],
      *,
      linked_player_uids: Sequence[str] = (),
  ) -> Dict[str, Any]
  ```
  - Matches all seasons across `person`'s `player_uid`, `player_id`, `player_name`, and `linked_player_uids` (enabling transitioned coaches to retain full historical player career tables).
  - Returns:
    - `regular_season_rows`: chronologically sorted Regular Season rows (`match_type` in `REGULAR`/`1`/`常规赛`), with Chinese team names resolved via `resolve_cba_team_name`.
    - `playoffs_rows`: chronologically sorted Playoffs rows (`match_type` in `PLAYOFFS`/`2`/`季后赛`).
    - `regular_career_summary`: `Optional[Dict[str, Any]]` with deduplicated `games_played` and weighted per-game averages (`points_per_game`, `rebounds_per_game`, `assists_per_game`, etc.).
    - `playoffs_career_summary`: `Optional[Dict[str, Any]]` with deduplicated `games_played` and weighted per-game averages.
    - `seasons_count`: `int` (distinct seasons with stats).
  - **No Double-Counting Invariant**: For any `(season, match_type)` that contains BOTH a `WHOLE_SEASON` record AND `TEAM_SPLIT` records (mid-season trade / cross-team season), career totals MUST use ONLY the `WHOLE_SEASON` row (never `WHOLE_SEASON + TEAM_SPLIT`). If only `TEAM_SPLIT` rows exist for a `(season, match_type)`, sum those `TEAM_SPLIT` rows.
- `build_person_card(...)` extensions (all new kwargs optional, 100% backward compatible):
  - Accepts `bio_profile: Optional[Any] = None` and `person_bio_profiles: Optional[Mapping[str, Any]] = None`.
  - When `person_registry` is provided and `query` matches a coach-only person in `person_registry` not present in `master_rows`/`foreign_rows`, resolves the person cleanly instead of returning `NOT_FOUND_IN_LOCAL_KB`.
  - Under `card_style="wiki_v241"`:
    - Populates `career_season_stats`, `lead_paragraph`, `lead_plain_text`, `bio_profile`, `citations`, `person_uid`, `person_name`, and `overall_verification_status` in the returned dict.
    - Pure coaches with 0 historical player stats omit the player career stats section gracefully.

#### `src/cba_kb/aliases.py`
- New function:
  ```python
  def resolve_cba_team_name(
      team_id_or_name: object,
      *,
      fallback_name: Optional[str] = None,
      prefer_short: bool = False,
  ) -> str
  ```
  - Maps official numeric CBA team IDs (str or int, e.g. `"29140"` / `29140` -> `"浙江稠州金租"`, `"29131"` -> `"深圳马可波罗"`, `"29124"` -> `"广东东阳光"`, `"100074683"` -> `"宁波町渥"`) via `CBA_TEAM_ID_TO_ZH`.
  - Maps canonical `club_id` (e.g. `"zhejiang_chouzhou"` -> `"浙江稠州金租"`) via reverse lookup of `CBA_TEAM_ID_TO_CANONICAL_ID`.
  - Falls back honestly to `fallback_name` (if provided) or `str(team_id_or_name).strip()` (never crashes or fabricates).
- Wired into `kc_render.py` game log opponent rendering and career stats table team column under `wiki_v241`.

#### Default-Collapsed Recent 5 Games Drawer
- Under `wiki_v241` / `static_site_builder.py`, recent single-game log (default 5 games) renders inside `<details class="gamelog-drawer"><summary>...</summary>...</details>` without an `open` attribute.

#### `src/cba_kb/club_card_builder.py` (D-02 Two-Season Roster + Offseason Changes)
- `build_club_panorama_card(...)` accepts optional `card_style: str = "classic"` (`"classic"` or `"wiki_v241"`) and `person_registry: Optional[Mapping[str, Any]] = None`.
- Computes and returns `offseason_roster_changes`:
  ```python
  {
      "from_season": str,
      "to_season": str,
      "retained": List[str],
      "joined": List[str],
      "departed": List[str],
  }
  ```
  and `all_roster_members: List[str]` (union of active + departed players + coaches across covered seasons).
- Under `card_style="wiki_v241"`, renders natural Chinese headings including `## 休赛期阵容变动` listing retained, joined, and departed members.
- `build_public_site` generates `persons/<slug>.html` for all provided person cards (active + departed + coaches) and ensures **0 broken internal links** in `index.html` and all `persons/*.html` (`res["broken_links_count"] == 0`).

---

### 3.3 P0-3: Sentence-Level `[n]` Citations, Wiki References, Story Dedup, `doc_<hex>` Block (`AC-241-P0-3`)

#### `src/cba_kb/kc_render.py` (`CitationRegistry`)
- Class `CitationRegistry`:
  - `__init__(self, *, visibility: str = "public_safe")`
  - `register(*, label: str, url: Optional[str] = None, source_kind: Optional[str] = None, published_at: Optional[str] = None, publisher: Optional[str] = None, source_id: Optional[str] = None) -> Optional[int]`
    - Deduplicates by normalized `url` when `url` is non-empty, or by normalized `(label, publisher, published_at)` when `url` is absent/empty (supporting URL-less newspaper & official WeChat citations).
    - Returns 1-based integer index `k` (`1, 2, ...`).
    - Under `visibility="public_safe"`, skips/returns `None` for `source_kind == "unreferenced_encyclopedia"` or internal/private sources (`r2://`, `file://`, `10_sources`, `web 剪藏`, `doc_id` leaks).
  - `entries() -> List[Dict[str, Any]]`: returns ordered list of citation dicts with `index`, `label`, `url`, `source_kind`, `published_at`, `publisher`.
  - `format_ref(index: int) -> str`: returns `f"[{index}]"`.
  - `render_markdown_references(heading: str = "## 参考资料") -> str`: renders a single numbered Markdown references section.
- In `static_site_builder.py`:
  - Inline `[k]` markers in body text are rendered as `<sup><a class="fn-ref" href="#fn-k" id="fnref-k">[k]</a></sup>` (unique `id="fnref-k"` on first occurrence).
  - Single bottom `<section class="footnotes" id="footnotes"><h2>参考资料</h2><ol>...</ol></section>` where each `<li id="fn-k">` contains a back-link `<a class="fn-back" href="#fnref-k" ...>&#8617;</a>`.
  - For `wiki_v241` cards (or cards with inline `[k]` markers), MUST NOT emit dangling `<p class="citation-refs">Sources: ...</p>` and MUST NOT duplicate `External Citations` / `References`.

#### `src/cba_kb/kc_visibility.py`
- `verify_public_safe_card_text(markdown: str)` MUST fail-closed (raising `ValueError("PUBLIC_SAFE_REDACTION_VIOLATION:...")`) on:
  - `\bdoc_[0-9a-f]{16,64}\b` (case-insensitive)
  - `\(unassigned\)` (case-insensitive)

#### `src/cba_kb/story_record.py`
- `validate_story_record` and `make_story_record` accept optional `organization_summary: Optional[str] = None`.
- `project_stories_for_entity(..., entity_kind="organization", ...)`:
  - When `organization_summary` is non-empty, uses `organization_summary` as `summary`.
  - When `organization_summary` is omitted/`None`, picks a **single primary participant summary** (preferring `ACTION_INITIATOR`, then `SUBJECT_INTERVIEWEE`, then first non-empty `public_perspective_summary`), NEVER concatenating multiple participant summaries together.

---

### 3.4 P0-4: GEO `sitemap.xml` + `llms.txt` + Relationship-Driven Related Persons & Navbox (`AC-241-P0-4`)

#### `src/cba_kb/static_site_builder.py`
- New helper:
  ```python
  def compute_related_persons(
      target_person: Mapping[str, Any],
      all_persons: Sequence[Mapping[str, Any]],
      *,
      story_records: Sequence[Mapping[str, Any]] = (),
      max_related: int = 8,
  ) -> List[Dict[str, Any]]
  ```
  - Scores and ranks relationships:
    1. Shared `StoryRecord` / `projected_stories` (`relation_label="共同故事"`)
    2. Coach-player relationship (`relation_label="师徒"`)
    3. Shared `draft_or_youth_origin` cohort (`relation_label="青训同梯队"` or `"同届选秀"`)
    4. Same-club teammate (`relation_label="同队队友"`)
  - Returns at most `max_related` (default 8) items, each with `person_uid`, `person_name`, `slug`, `relation_label`, `score`.
- `build_public_site(..., base_url: Optional[str] = None, story_records: Optional[Sequence[Mapping[str, Any]]] = None)`:
  - Person page renders `<section class="related-persons">` (heading `关联人物`, capped at top-8 with relation labels).
  - Person page renders collapsible bottom `<nav class="squad-navbox"><details><summary>...</summary>...</details></nav>` (no `open` attribute).
  - Generates `output_dir / "sitemap.xml"` (valid XML `<urlset>` containing index + all `persons/<slug>.html`, public-safe verified) and `output_dir / "llms.txt"` (structured Markdown summary for LLM crawlers, public-safe verified).
  - Enhanced Person JSON-LD (`@type: "Person"`): includes `description`, `jobTitle`, `height` (`QuantitativeValue` `CMT`), `weight` (`QuantitativeValue` `KGM`), `birthDate`, and `citation` when present (omitting missing fields).
  - Result dict includes `sitemap_path`, `llms_txt_path`, and `broken_links_count` (`0`).

---

### 3.5 P0-5: Local Draft Extractor & `v2.4.1-1` / `v2.4.1-2` Release Spec (`AC-241-P0-5`)

#### `src/cba_kb/wiki_draft_extractor.py` & CLI `cba-kb draft-wiki-candidates`
- Constants:
  - `DRAFT_REVIEW_STATUS = "DRAFT_PENDING_HUMAN_REVIEW"`
  - `WIKI_DRAFT_CANDIDATES_SCHEMA = "cba-kb.wiki-draft-candidates.v1"`
- Function:
  ```python
  def extract_wiki_candidates(
      *,
      sources_dir: str | Path,
      person_registry: Mapping[str, Any] | str | Path,
      output_path: Optional[str | Path] = None,
  ) -> Dict[str, Any]
  ```
  - 100% local, zero network, Python stdlib only.
  - Scans local `.md`, `.txt`, `.json`, `.jsonl` files under `sources_dir` without modifying `sources_dir`, `person_registry`, or any canonical store (`canonical_stores_modified=False`, `network_called=False`).
  - Outputs `bio_profile_candidates` and `story_and_timeline_candidates`; every candidate MUST carry `review_status == "DRAFT_PENDING_HUMAN_REVIEW"`.
  - When `output_path` is supplied, writes JSON atomically to `output_path` only.
- CLI: `cba-kb draft-wiki-candidates --sources-dir <dir> --person-registry <path> --output <path>`.

#### `src/cba_kb/orchestration/release/prepare_v215.py`
- `V241_1_BASELINE_SHA = "aeb630c8211799fe91f05cd06b18301a55c823f2"`
- `V215_RELEASE_SPECS["v2.4.1-1"]` and `V215_RELEASE_SPECS["v2.4.1-2"]`:
  - `product_baseline_sha`: `"aeb630c8211799fe91f05cd06b18301a55c823f2"`
  - `enable_consumer_surface_v215`: `True`
  - `enable_operational_kb_v220`: `True`
  - `enable_curated_compact_surface_v222`: `True`
  - `enable_observation_horizon_v230`: `True`
  - `enable_on_demand_cards_v230`: `True`
  - `enable_v231_surface_enhancements`: `True`
  - `enable_v232_card_pipeline`: `True`
  - `enable_v240_shenzhen_narrative_pilot`: `True`
  - `enable_v241_wikipedia_site_v2`: `True`
  - `active_league_season`: `"2026-2027"`
  - `incremental_delta_only`: `True`
  - `archive_format`: `"bundle_v1"`
- `_POST_ROLLBACK_RECOVERY_ALLOWLIST` includes `("v2.4.1-2", "v2.4.1-1")`.
- Historical `v2.1.5-1..v2.4.0-2` remain intact with `enable_v241_wikipedia_site_v2` absent/False.

#### `src/cba_kb/cloud_state.py`
- `_bundle_allowlisted_exact_names()` includes `"person_bio_profiles.json"`.
