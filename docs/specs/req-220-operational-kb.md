# REQ-220: v2.2.0 Operational KB Engine Specification

- **Issue**: #254
- **Branch**: `muse/issue-254`
- **Status**: Frozen Spec & Acceptance Contract (Commit 1)
- **Governance**: LACD v1.0.4 (`Statement != Fact. Evidence != Fact. Unknown != 0.`)
- **Isolation**: Zero real private data, zero production credentials, zero external crawler/network dependencies, strict Drive A isolation on local operational sync.

---

## 1. Scope Overview

REQ-220 upgrades `cba-kb-engine` into a local-first, fail-closed Operational Knowledge Base engine covering five P0 deliverables:

1. **P0-1**: Native `acquisition_bundle_v1` 18-key intake contract, lossless provenance propagation into archive & routed passages (preventing syndication corroboration inflation), atomic `DocumentLane` staging & idempotent `_merge_exact` recovery, and single-pass 5-gram shingle cache for near-dedup.
2. **P0-2**: Content revision supersession chain (`supersedes_doc_id` / `superseded_by_doc_id`, `revision_seq`, `revision_index.json`) driving `LOUD_REVISION` disclosure, plus offline registration snapshot replay (`.json` / `.json.gz`) in `source_watcher.py` with `canonical_publish_allowed == False`.
3. **P0-3**: Dirty-Set computation from new/revised documents + approved identity bindings (strictly keeping ambiguous homonyms / unapproved names `undecided` in `verification_queue`), incremental player card recompute (unimpacted player cards byte-identical), and deterministic external citation ranking & hard cap (`max_external_citations_per_card = 10`) with honest total-vs-displayed disclosure.
4. **P0-4**: `operational_sync.py` and `cba-kb ops-sync` CLI subcommand with atomic `operational_checkpoint.json`, `--dry-run`, `freshness_report.json` (Producer Candidate World vs Published Consumer World), and strict Drive A isolation.
5. **P0-5**: Unified `drive_resilience.file_lock(registry_file)` across all four `registry_file` write paths (`drive_search.py`, `drive_discovery.py`, `drive_poll.py`, `drive_plan.py`) using `_unlocked` inner helpers + outer locked wrappers so concurrent writes lose zero updates and `drive_plan._apply_plan_resilient` never self-deadlocks.

---

## 2. Frozen Public APIs & Data Contracts

### 2.1 P0-1: `acquisition_bundle_v1`, Provenance Propagation, Atomic Staging & Shingle Cache

#### `src/cba_kb/source_intake.py`

- **Constants**:
  - `ACQUISITION_BUNDLE_V1_SCHEMA = "acquisition_bundle_v1"`
  - `ACQUISITION_BUNDLE_V1_REQUIRED_KEYS = frozenset({`
    `"bundle_schema", "item_id", "canonical_url", "final_url", "related_urls",`
    `"upstream_origin", "source_id", "source_tier", "event_class", "matched_persons",`
    `"published_at", "acquired_at", "raw_sha256", "raw_ref", "extracted_sha256",`
    `"extractor", "completeness", "rights"`
    `})` (18 required keys, aligned with `cba-kb-collector v0.3.1`).
  - `ACQUISITION_SOURCE_TIERS = frozenset({"T1", "T1.5", "T2", "MANUAL"})`
  - `ACQUISITION_COMPLETENESS_VALUES = frozenset({"full", "partial", "metadata_only"})`
  - `SOURCE_PROVIDERS` includes `"acquisition_bundle_v1"`.

- **`validate_acquisition_bundle_v1(metadata: Dict[str, Any]) -> Dict[str, Any]`**:
  - Raises `SourceIntakeError` on any contract violation:
    - `metadata` not a `dict` or missing any of the 18 keys in `ACQUISITION_BUNDLE_V1_REQUIRED_KEYS`.
    - `bundle_schema != "acquisition_bundle_v1"`.
    - `canonical_url` or `final_url` not a non-empty `http://` or `https://` string.
    - `item_id` not 64-char lowercase hex OR `item_id != sha256(canonical_url.encode("utf-8")).hexdigest()`.
    - `related_urls` not a `list` of `str`.
    - `upstream_origin`, `source_id`, `event_class`, `extractor` not non-empty `str`.
    - `source_tier` not in `{"T1", "T1.5", "T2", "MANUAL"}`.
    - `matched_persons` not a `list` of `str`.
    - `published_at` not `None`, `str`, `datetime.date`, or `datetime.datetime` (normalized to `None` or ISO-8601 `str`).
    - `acquired_at` is `None` or not `str` / `datetime.datetime` (normalized to ISO-8601 `str`).
    - `raw_sha256` or `extracted_sha256` not 64-char lowercase hex.
    - `raw_ref` not `None` and not a non-empty `str`.
    - `completeness` not lowercase `str` strictly in `{"full", "partial", "metadata_only"}` (e.g., `"FULL"` MUST be rejected).
    - `rights` not a `dict`, or `rights.get("classification") != "copyrighted"`, or `rights.get("public_export_allowed") is not False`.
  - Preserves optional keys such as `title` when present.
  - Zero external crawler/network dependencies.

- **`parse_acquisition_bundle_markdown(raw_content: bytes | str) -> Tuple[Dict[str, Any], str]`**:
  - Parses YAML front-matter (`---` ... `---`) with `yaml.safe_load`, validates via `validate_acquisition_bundle_v1`, and returns `(validated_metadata, body_text)`.
  - Raises `SourceIntakeError` if front-matter is missing/malformed or body is empty.

#### `src/cba_kb/document_lane.py`

- `CAPTURE_CHANNELS` includes `"acquisition_bundle_v1"`.
- **`DocumentLane.ingest_acquisition_bundle(self, bundle_path: str | Path, **options) -> Dict[str, Any]`**:
  - Reads markdown file from `bundle_path`, parses & validates via `parse_acquisition_bundle_markdown`, and ingests into `archive_root`.
  - Losslessly persists `upstream_origin`, `related_urls`, `source_tier`, `raw_sha256`, `extracted_sha256`, `item_id`, `source_id`, `completeness`, `event_class`, `matched_persons`, `extractor` into:
    - `provenance.json` (on each capture entry in `provenance["captures"]`, and top-level `provenance["acquisition"]` / mirrored fields).
    - `record.json` (preserving `upstream_origin`, `related_urls`, `source_tier`, `raw_sha256`, `extracted_sha256`, `completeness`, `item_id`, `source_id`).
- **Atomic Staging & Idempotent `_merge_exact`**:
  - `_ingest_parsed` writes all new archive artifacts (`raw/`, `text.txt`, `record.json`, `provenance.json`, `dedup.json`, `metrics.json`, `attachments/`) into `self.archive_root / ".staging" / <tmp>` and atomically publishes via `os.replace(staging_dir, root)`.
  - On mid-write failure, staging dir is cleaned up; NO partial `doc_*` directory exists in `archive_root`; subsequent retry succeeds cleanly without `DOCUMENT_ARCHIVE_PROVENANCE_INVALID` or `DOCUMENT_ARCHIVE_RECORD_INVALID`.
  - `_existing_records()` ignores `.staging` and non-`doc_*` entries.
  - `_merge_exact` is idempotent on retry after an interrupted merge (e.g. raw capture written or partial metadata update), never failing with `DOCUMENT_ARCHIVE_PROVENANCE_INVALID` on valid archive recovery.
- **Single-Pass Near-Dedup Cache**:
  - `DocumentLane` caches existing archive records and their 5-gram `_shingles(text)` sets in memory across `ingest_*` / `ingest_batch` calls, updating the cache incrementally upon publish.
  - Preserves exact near-dedup classification (`jaccard >= 0.92 and length_ratio >= 0.90`) and output schema.

#### `src/cba_kb/kc_passages.py` & `src/cba_kb/kc_evidence_status.py`

- **`build_routed_passage(...)`** accepts optional keyword arguments:
  - `upstream_origin: Optional[str] = None`
  - `related_urls: Optional[List[str]] = None`
  - `source_tier: Optional[str] = None`
  - `raw_sha256: Optional[str] = None`
  - `extracted_sha256: Optional[str] = None`
  - `completeness: Optional[str] = None`
  - Stored on the returned passage dict when provided (keeping `validate_passage(passage)["valid"] is True`).
- **`classify_claim_independence(...)`**:
  - Resolves `upstream_origin` from `documents_by_id` (top-level `upstream_origin`, `provenance.upstream_origin`, or `provenance.captures[0].upstream_origin`) and `claim.get("upstream_origin")`.
  - Two reposted URLs sharing the same `upstream_origin` collapse to `independent_source_count == 1` (`posture == "SINGLE_SOURCE_UNCORROBORATED"`), never `INDEPENDENTLY_CORROBORATED`.

---

### 2.2 P0-2: Revision Supersession Chain (`LOUD_REVISION`) & Offline Registration Snapshot Replay

#### `src/cba_kb/document_lane.py`

- When a newly ingested document shares `canonical_url` (or `item_id`) with an existing archived document whose `content_hash` / `extracted_sha256` differs:
  - The old `doc_id` archive (`text.txt`, `raw/`, `content_hash`) is preserved untouched.
  - Deterministic bidirectional chain links are recorded in both archives (`record.json` and `dedup.json`):
    - New archive: `supersedes_doc_id = prior_doc_id`, `superseded_by_doc_id = None`, `revision_seq = prior_revision_seq + 1` (initial doc has `revision_seq = 1`).
    - Prior archive: `superseded_by_doc_id = new_doc_id` (atomically updated without altering `text.txt` or `content_hash`).
  - Revision index written atomically to `self.archive_root / "revision_index.json"` containing deterministic ordered chains keyed by `canonical_url`.
  - Ingest result includes `revision_summary`:
    ```json
    {
      "prior_doc_id": "doc_...",
      "new_doc_id": "doc_...",
      "canonical_url": "https://...",
      "revision_seq": 2,
      "prior_content_hash": "...",
      "new_content_hash": "...",
      "disclosure_posture": "LOUD_REVISION"
    }
    ```

#### `src/cba_kb/kc_evidence_status.py`

- **`build_revision_disclosure(revision_summary: Dict[str, Any]) -> Dict[str, Any]`**:
  - Validates `revision_summary` (`prior_doc_id`, `new_doc_id`, `revision_seq`) and returns a structured disclosure dict:
    - `posture`: `"REVISION_SUPERSEDED"`
    - `disclosure_posture`: `"LOUD_REVISION"`
    - `loud_disclosure_required`: `True`
    - `prior_doc_id`, `new_doc_id`, `revision_seq`, `canonical_url`
    - `disclosure_notice`: human-readable bilingual/structured notice containing disclosure tokens (`"修订"`, `"revision"`, `"supersede"`).
    - `warnings`: `["LOUD_REVISION"]`
- `classify_stats_evidence(...)` and `classify_claim_independence(...)` when returning `posture == "REVISION_SUPERSEDED"` also include `"disclosure_posture": "LOUD_REVISION"` and `"loud_disclosure_required": True`.

#### `src/cba_kb/source_watcher.py`

- **`replay_registration_snapshots(snapshots_dir: str | Path, output_dir: str | Path, *, source_id: Optional[str] = None, clubs: Any = None) -> Dict[str, Any]`**:
  - Discovers `.json` and `.json.gz` files in `snapshots_dir` in observed chronological order (sorted by `observed_at` when present, tie-broken by filename).
  - Each replay snapshot payload is a JSON object:
    ```json
    {
      "source_id": "cba-registration:2025-2026",
      "observed_at": "2026-11-01T10:00:00Z",
      "index": { "code": 200, "data": { ... } },
      "children": {
        "<article_id>": { "code": 200, "data": { ... } }
      }
    }
    ```
  - Constructs an offline `fetcher` serving `index` and `children` from the snapshot (zero network calls) and invokes `source_watcher.run_source(..., previous_snapshot=prev_snapshot_path, fetcher=offline_fetcher, sleeper=lambda _: None, clubs=clubs)`.
  - Returns deterministic summary:
    ```json
    {
      "schema_version": 1,
      "replayed_count": 2,
      "canonical_publish_allowed": false,
      "runs": [
        {
          "snapshot_file": "...",
          "observed_at": "...",
          "source_id": "...",
          "diff_counts": {"added": 1, "modified": 0, "removed": 0, "unchanged": 0},
          "candidate_count": 1,
          "validation_result": "PASS",
          "canonical_publish_allowed": false
        }
      ]
    }
    ```
  - Strictly maintains `canonical_publish_allowed is False` on every run and at top level.

---

### 2.3 P0-3: Dirty-Set, Incremental Card Recompute & External Citation Cap (`<= 10`)

#### `src/cba_kb/document_mentions.py`

- **`compute_dirty_set(documents: Sequence[Dict[str, Any]], identity_registry: Dict[str, Any], *, approved_mentions: Optional[Sequence[Dict[str, Any]]] = None, existing_doc_hashes: Optional[Dict[str, str]] = None) -> Dict[str, Any]`**:
  - Each item in `documents` has `doc_id`, `text`, optional `content_hash`, optional `matched_persons`.
  - Skips documents whose `doc_id` is unchanged in `existing_doc_hashes`.
  - Uses `approved_mentions` (validated: `mention_status == "same"` AND `mention_method in {"manual", "external_id"}`) to add affected `player_uid`s to `dirty_player_uids`.
  - Discovers candidate mentions via `discover_document_mentions(text, identity_registry, doc_id=doc_id)` and checks `matched_persons`. Any unapproved name or ambiguous homonym (e.g., two active players sharing the same name/alias) MUST remain `"mention_status": "undecided"` and be placed in `verification_queue` — NEVER auto-bound as `"same"` and NEVER added to `dirty_player_uids`.
  - Returns:
    ```json
    {
      "dirty_player_uids": ["player-a"],
      "dirty_doc_ids": ["doc_..."],
      "verification_queue": [
        {
          "doc_id": "doc_...",
          "mention_status": "undecided",
          "matched_name": "...",
          "candidate_player_uids": ["player-x", "player-y"],
          "reason": "AMBIGUOUS_HOMONYM" | "UNAPPROVED_MENTION"
        }
      ],
      "undecided_count": 1
    }
    ```

#### `src/cba_kb/consumer_surface.py`

- **`DEFAULT_MAX_EXTERNAL_CITATIONS_PER_CARD = 10`**
- **`select_external_citations(citations: Sequence[Dict[str, Any]], *, max_external_citations_per_card: int = DEFAULT_MAX_EXTERNAL_CITATIONS_PER_CARD) -> Dict[str, Any]`**:
  - Deterministically ranks citations by:
    1. `source_tier`: `T1` (0) < `T1.5` (1) < `T2` (2) < `MANUAL` (3) < other (4)
    2. `completeness`: `full` (0) < `partial` (1) < `metadata_only` (2) < other (3)
    3. `published_at` descending (ISO string; `None`/empty sorts last)
    4. `doc_id` ascending
  - Truncates to `max_external_citations_per_card` (must be `>= 1`; raises `ValueError` if `< 1`).
  - Returns:
    ```json
    {
      "selected": [...],
      "total_linked_count": 15,
      "displayed_count": 10,
      "truncated": true,
      "max_external_citations_per_card": 10
    }
    ```

#### `src/cba_kb/kc_render.py` & `src/cba_kb/kc_local_producer.py`

- `kc_render.render_research_card(payload, *, evidence_verified=False, max_external_citations_per_card=10)`:
  - When `payload` includes `"external_citations"` (non-empty list), applies `select_external_citations` and renders an `## External Citations` section showing only the selected citations (`<= max_external_citations_per_card`) and disclosing both `total_linked_count` and `displayed_count` (e.g., `- Archive External Evidence: {total_linked_count} linked, {displayed_count} displayed (cap={cap})`).
- **`kc_local_producer.build_incremental_player_cards(*, player_fixtures: Dict[str, Dict[str, Any]], dirty_player_uids: Sequence[str], output_dir: str | Path, external_citations_by_player: Optional[Dict[str, Sequence[Dict[str, Any]]]] = None, max_external_citations_per_card: int = 10, **build_kwargs) -> Dict[str, Any]`**:
  - Recomputes ONLY players in `dirty_player_uids`; players not in `dirty_player_uids` are skipped and their existing files under `output_dir / <player_uid>` are NOT touched (bytes and SHA-256 remain 100% identical).
  - For each recomputed player, writes `output_dir / <player_uid> / research_card.md` and `output_dir / <player_uid> / 研究卡_{name}_{id}_{season}_CURRENT.md` (via `kc_render.research_card_consumer_filename`), including ranked/capped external citations with honest total-vs-displayed disclosure.
  - Returns:
    ```json
    {
      "recomputed_player_uids": [...],
      "skipped_player_uids": [...],
      "cards": {
        "<player_uid>": {
          "research_card_path": "...",
          "consumer_card_path": "...",
          "consumer_card_sha256": "...",
          "total_linked_citations": 15,
          "displayed_citations": 10
        }
      }
    }
    ```

---

### 2.4 P0-4: `operational_sync.py` & `cba-kb ops-sync` CLI

#### `src/cba_kb/operational_sync.py`

- Constants:
  - `CHECKPOINT_FILENAME = "operational_checkpoint.json"`
  - `FRESHNESS_REPORT_FILENAME = "freshness_report.json"`
- **`run_operational_sync(*, engine_root: str | Path, instance: Instance, bundles_dir: str | Path, output_dir: str | Path, registration_snapshots_dir: Optional[str | Path] = None, published_status_path: Optional[str | Path] = None, consumer_manifest_path: Optional[str | Path] = None, player_fixtures_path: Optional[str | Path] = None, identity_registry_path: Optional[str | Path] = None, approved_mentions_path: Optional[str | Path] = None, max_external_citations_per_card: int = 10, dry_run: bool = False) -> Dict[str, Any]`**:
  - Strictly Drive A isolated: zero Google Drive API imports or invocations (`"drive_api_called": False`).
  - Reads existing `operational_checkpoint.json` from `output_dir` if present.
  - Idempotent incremental execution:
    - Run 1 ingests new bundles, optionally replays registration snapshots, computes dirty set & incremental player cards, and atomically writes `operational_checkpoint.json` and `freshness_report.json`.
    - Run 2 with no input changes performs `ingested_count == 0` and `recomputed_player_uids == []`.
    - `dry_run=True` computes the report in memory without mutating `operational_checkpoint.json`, archive, or any cards.
  - `freshness_report.json` compares Producer Candidate World against Published Consumer World (`published_status_path` / `consumer_manifest_path`) and reports:
    - `schema_version`: `1`
    - `dry_run`: `bool`
    - `drive_api_called`: `False`
    - `ingested_count`: `int`
    - `skipped_bundles_count`: `int`
    - `recomputed_player_uids`: `list[str]`
    - `pending_player_cards`: `list[dict]`
    - `lagged_docs`: `list[dict]`
    - `undecided_mentions`: `list[dict]`
    - `registration_candidate_diffs`: `list[dict]`

#### `src/cba_kb/cli.py`

- Subcommand `ops-sync`:
  - Arguments: `--bundles-dir` (required), `--output-dir`, `--registration-snapshots-dir`, `--published-status`, `--consumer-manifest`, `--player-fixtures`, `--identity-registry`, `--approved-mentions`, `--max-external-citations-per-card` (int, default 10), `--dry-run`, `--instance-root`.
  - Runs `operational_sync.run_operational_sync(...)` and prints JSON report.

---

### 2.5 P0-5: Unified `registry_file` Locking & Deadlock Prevention

All four `registry_file` write paths MUST coordinate on `drive_resilience.file_lock(registry_file)` using `_unlocked` inner helpers + outer locked wrappers:

1. **`src/cba_kb/drive_search.py`**:
   - `_grant_rights_unlocked(...)`: performs load -> validate -> append ledger -> update -> atomic write without acquiring `file_lock`.
   - `grant_rights(...)`: acquires `with file_lock(registry_file):` and delegates to `_grant_rights_unlocked(...)`.
2. **`src/cba_kb/drive_discovery.py`**:
   - `_save_registry_unlocked(registry_file, updates)`: reloads current on-disk registry, merges updates, writes atomically.
   - `run_discovery(...)`: wraps the read-merge-write critical section with `with file_lock(registry_file):` delegating to `_save_registry_unlocked` so concurrent writers lose zero updates.
3. **`src/cba_kb/drive_poll.py`**:
   - `_save_registry_unlocked(registry_file, updates)`: reloads current on-disk registry, applies change update, writes atomically.
   - `poll_once(...)`: wraps each registry read-merge-write critical section with `with file_lock(registry_file):` delegating to `_save_registry_unlocked` so concurrent writers lose zero updates.
4. **`src/cba_kb/drive_plan.py`**:
   - `_apply_purge_tombstones_unlocked(params, ctx)`: performs load -> purge -> atomic write without acquiring `file_lock`.
   - `_apply_purge_tombstones(params, ctx, *, _registry_locked: bool = False)`: acquires `with file_lock(Path(params["registry"])):` only when `_registry_locked is False`, then delegates to `_apply_purge_tombstones_unlocked(params, ctx)`.
   - `_apply_plan_resilient(...)`: passes `_registry_locked=True` when invoking `_apply_purge_tombstones` because `ExitStack` already holds `file_lock(params["registry"])`, preventing self-deadlock.

---

## 3. Acceptance Test Matrix (`tests/test_req220_acceptance.py`)

| Test ID | Area | Verification |
|---|---|---|
| `test_p0_1_acquisition_bundle_v1_validator_valid_and_rejections` | P0-1 | 18-key schema validation, `date`/`datetime`/`None` handling, rejection of missing keys, bad `item_id`, uppercase `completeness`, bad `source_tier`, `public_export_allowed=True`, zero network/crawler deps |
| `test_p0_1_lossless_provenance_and_syndication_dedup` | P0-1 | Lossless provenance in `provenance.json` + `build_routed_passage`, reposted URLs sharing `upstream_origin` collapse to `independent_source_count == 1` |
| `test_p0_1_document_lane_atomic_staging_and_merge_recovery` | P0-1 | Interrupted staging write leaves zero partial `doc_*` dir, retry succeeds cleanly; interrupted `_merge_exact` recovers idempotently |
| `test_p0_1_document_lane_single_pass_shingle_cache` | P0-1 | `ingest_batch` caches existing records & 5-gram `_shingles` across batch items while preserving exact near-dedup classification |
| `test_p0_2_revision_supersession_chain_and_loud_revision` | P0-2 | Same `canonical_url`/`item_id` with changed body preserves old archive untouched, builds bidirectional `supersedes_doc_id`/`superseded_by_doc_id` + `revision_seq` + `revision_index.json`, emits `LOUD_REVISION` disclosure |
| `test_p0_2_offline_registration_snapshot_replay` | P0-2 | Replays `.json` and `.json.gz` snapshots chronologically via `source_watcher.run_source(..., fetcher=...)`, deterministic diff & validation, `canonical_publish_allowed is False` |
| `test_p0_3_dirty_set_and_ambiguous_homonym_undecided` | P0-3 | Approved bindings enter `dirty_player_uids`; ambiguous homonyms & unapproved names remain `undecided` in `verification_queue` |
| `test_p0_3_incremental_card_recompute_and_citation_cap_10` | P0-3 | Only dirty players recomputed (untouched player card bytes & SHA-256 unchanged); `>10` external citations ranked deterministically (`source_tier -> completeness -> published_at desc -> doc_id asc`), capped at 10, honest total vs displayed disclosure |
| `test_p0_4_ops_sync_cli_checkpoint_dry_run_freshness_and_drive_a_isolation` | P0-4 | `cba-kb ops-sync` Run 1 ingests & builds, Run 2 is 0-op idempotent, `--dry-run` mutates nothing, `freshness_report.json` compares Candidate vs Published worlds, zero Drive API calls |
| `test_p0_5_registry_lock_unification_concurrent_writes_and_no_self_deadlock` | P0-5 | Concurrent `grant_rights` / `run_discovery` / `poll_once` / `_apply_purge_tombstones` lose zero updates; `_apply_plan_resilient` invoking `_apply_purge_tombstones` completes without self-deadlock |
