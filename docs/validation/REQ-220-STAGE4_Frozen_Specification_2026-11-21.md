# REQ-220 Stage 4 Frozen Specification — `v2.2.0-1` Release Spec & Governed Collector Official Digest Surface

- **Requirement ID**: `REQ-220` / `REQ-COL-040-1` (Stage 4 Production Release — `v2.2.0-1`)
- **GitHub Issue**: `#258` (`T2`, `src/**/release/**`)
- **Base `main` SHA (`V220_1_BASELINE_SHA`)**: `fa9793c6db76e15880610ced728a7b0d563da604` (`v2.2.0` Engine Release merge commit)
- **Target Production Release ID**: `v2.2.0-1`
- **Target Product Version**: `v2.2.0`
- **Governance**: `LACD v1.0.4` (`pull_request_required=true`, `human_merge_required=true`, `bind_to_commit_sha=true`, `fail_closed=true`)

## Frozen Contract

1. **`prepare_v215.V215_RELEASE_SPECS["v2.2.0-1"]` Registration**:
   - `cba_kb.orchestration.release.prepare_v215.V215_RELEASE_SPECS["v2.2.0-1"]` equals:
     - `{"product_baseline_sha": "fa9793c6db76e15880610ced728a7b0d563da604", "enable_consumer_surface_v215": True, "enable_operational_kb_v220": True}`
   - `prepare_v215._require_release("v2.2.0-1")` and `prepare_v215._release_spec("v2.2.0-1")` return that exact dictionary.
   - `git merge-base --is-ancestor fa9793c6db76e15880610ced728a7b0d563da604 HEAD` exits `0`.
   - `prepare_v215._uses_closure("v2.2.0-1")` and `prepare_v215._uses_technical_manual("v2.2.0-1")` return `True`.
   - `scripts/prepare_production.py` remains 100% unmodified at SHA-256 `0ccb79429fb4ec5e270a3154af987475ae75bd4ef81de48c1e665a3b93cf4573`.
   - `pyproject.toml` `[project].version` stays `"2.0.2"`.

2. **Governed Collector Official Digest Renderer (`cba_kb.consumer_surface.render_collector_official_digest_md`)**:
   - Exported constant: `COLLECTOR_DIGEST_FILENAME = "CBA_官方采集动态与注册增量摘要_CURRENT.md"`.
   - Deterministically renders `CBA_官方采集动态与注册增量摘要_CURRENT.md` (`consumer/collector_official_digest_current.md`) from `acquisition_bundle_v1` bundle records plus optional `reg_diff` (`collector diff-registration` output).
   - Must prominently declare `evidence / candidate_observation` layer status and `Statement != Fact. Evidence != Fact. Unknown != 0.` (never promoted as canonical facts).
   - Must include:
     - 2026-2027 foreign player registration official snapshot table verbatim when present in bundles;
     - 3-season domestic player registration snapshot summary and cross-season `25-26 -> 26-27` transfer / contract-change / added / removed player observations from `reg_diff` when provided;
     - Structured index and full sanitized excerpts of official announcements (including `doc_id`, `source_tier`, `canonical_url`, `raw_sha256`).
   - Must redact any raw Drive IDs, Drive URLs, and local absolute home paths via `scrub_raw_drive_ids_for_consumer`, and terminate with `end_marker: true\n`.

3. **Idempotent Document Index Merge (`cba_kb.consumer_surface.merge_operational_docs_into_consumer_index`)**:
   - Merges `operational_checkpoint.json` `consumed_bundles` into `CBA_文档索引_脱敏视图_CURRENT.json` (`documents`, `unique_document_count`).
   - Preserves existing index documents, strips any raw `drive_file_id`, `file_id`, and local absolute paths, updates `unique_document_count`, and is strictly idempotent on re-run (zero duplicate `doc_id` entries).

4. **Consumer Surface Routing & Backward Compatibility**:
   - `render_consumer_field_dictionary_md` and `render_consumer_surface_guide_section` document `CBA_官方采集动态与注册增量摘要_CURRENT.md` and `CBA_文档索引_脱敏视图_CURRENT.json` when `release_id="v2.2.0-1"` (or `include_collector_digest=True`).
   - `release_id="v2.1.5-1"` preserves 100% backward compatibility with all existing REQ-215 assertions.
   - `build_consumer_surface_targets_spec` supports `collector_official_digest_bytes` (`consumer/collector_official_digest_current.md`) and `include_collector_digest`.

5. **Stage 4 Instance Consumer Target Wiring (`prepare_v215._build_instance_consumer_targets`)**:
   - When `enable_operational_kb_v220` is truthy and `instance.data_root / "ops_sync_v040_1" / "operational_checkpoint.json"` exists:
     - Merges `consumed_bundles` into `consumer/document_index_current.json` (`CBA_文档索引_脱敏视图_CURRENT.json`);
     - Loads bundle frontmatter + full body from `instance.document_archive_root / <doc_id> / raw / *.md` (falling back to `text.txt` + checkpoint metadata when raw `.md` is absent) plus optional `reg_diff_2526_2627.json`;
     - Produces `consumer/collector_official_digest_current.md` (`CBA_官方采集动态与注册增量摘要_CURRENT.md`) without adding bulk raw bundle files to release targets.

6. **Out-of-Scope / Boundary Constraints**:
   - Pre-existing `tests/**/*.py`, `.github/**`, and `scripts/prepare_production.py` remain untouched.
   - Zero raw Drive IDs, zero private instance paths, and zero bulk raw `.md` files in the Engine repository or consumer targets.
   - Only `src/cba_kb/consumer_surface.py` and `src/cba_kb/orchestration/release/prepare_v215.py` are modified in Commit 2.
