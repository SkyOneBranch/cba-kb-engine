# REQ-213 Consolidated Effective Specification (v2.1.3 — Evidence Transparency & Corrigible Research)

- **Requirement ID**: `REQ-213`
- **Target Version**: `v2.1.3`
- **Baseline HEAD SHA**: `aee743cbae9f8f6c8c8ce7fa6fb21750df88344c`
- **Governance**: LACD v1.0.2 (`pull_request_required=true`, `human_merge_required=true`, `bind_to_commit_sha=true`, `fail_closed=true`)
- **Authorization**: Scope & Freeze boundary confirmed by Human on 2026-11-11 (Three P0 + One Bounded P1: `P0-1`, `P0-2`, `P0-3`, `P1`).
- **Debt Boundary**: REQ-212 open obligations remain under REQ-212 and are not retroactively signed or repackaged under REQ-213.

## 1. Core Principles & Three-Layer Separation

- `Statement != Fact. Evidence != Fact. Unknown != 0.`
- **Record Accuracy vs Real-world Truth**: Official league statistics (`provider == "CBA_OFFICIAL"`) serve as the primary authoritative record (`PRIMARY_OFFICIAL_RECORD`) for corresponding statistical metrics; clean official records are rendered **quiet by default** (without resident "no second independent source" boilerplate warnings), while never asserting eternal unconditional truth.
- **Fold at presentation, never discard at evidence layer**:
  1. **Layer 1 (Machine Evidence)**: `snapshot.json`, `slices/*`, `passages/*`, `answer.json`, `guarantee.json`, SHA-256 digests, authorization bindings, and `end_marker: true` remain intact.
  2. **Layer 2 (Serving Projection — Compact Exception-Aware Research Card)**: Controlled `ConsumeSession.render_verified(artifact, kind="research_card")` delegates to `kc_render.render_research_card(payload, evidence_verified=True)`, preserving session/digest/snapshot/live-authorization guards and `end_marker: true`.
  3. **Layer 3 (Consumer Answer & Independent Oracle Verification)**: Quiet on clean official records, loud on single-source media, repost deduplication, UNKNOWN gaps, data discrepancies, irreconcilable conflicts, and bounded revisions.

## 2. Frozen Acceptance Criteria (AC-213)

- **AC-213-P0-1 (Compact Evidence Answer & Three-Layer Guards)**:
  - `ConsumeSession.render_verified(ans, kind="research_card")` outputs compact Markdown carrying metric value, `season`, `match_type`, `scope`, games/cutoff timestamp, official authority/source locator, and trailing `end_marker: true` validated by `parse_rendered(..., kind="markdown")`.
  - Direct `kc_render.render_research_card(payload)` without `evidence_verified=True` raises `KcRenderError("RESEARCH_CARD_NOT_VERIFIED")`.
  - Revoking `authorized_players` or `authorized_sources` prior to `render_verified(..., kind="research_card")` raises `KcConsumeError` (`ARTIFACT_STATS_SUBJECT_NOT_AUTHORIZED` / `ARTIFACT_PLAYER_NOT_AUTHORIZED` / `ARTIFACT_SOURCE_NOT_AUTHORIZED`).
  - `build_local_projection()` emits `research_card.md` and `verify_local_projection()` verifies its completeness and manifest digest.
  - `ResearchView.render_markdown()` includes `match_type` and `provenance.captured_at` / `endpoint` in the `STATS` section.

- **AC-213-P0-2 (Exception-Aware Evidence Classification & Anti-False-Conflict)**:
  - `kc_projection.query_stats()` primitive fail-closed contract remains untouched.
  - Pure T1 module `src/cba_kb/kc_evidence_status.py` provides `classify_stats_evidence(...)` and `classify_claim_independence(...)`:
    - `CLEAN_OFFICIAL`: single valid official record -> `warnings == []`, `adopted_value == published_value`.
    - `IDENTICAL_DUPLICATE`: multiple records for exact key with identical metric value -> idempotent fold, never `TRUE_CONFLICT`.
    - `SCOPE_OR_CUTOFF_DIFF`: mid-season vs full-season cutoff progression or `TEAM_SPLIT` breakdown -> explicit scope/cutoff explanation, never `TRUE_CONFLICT`.
    - `DATA_DISCREPANCY`: same season/scope/games cutoff discrepancy (e.g. `OFFICIAL_SUMMARY` vs `GAME_LOG_SUM`) -> `adopted_value is None`, exposes both records and exact `delta`.
    - `TRUE_CONFLICT`: irreconcilable same-key, same-cutoff contradictory records/claims -> `adopted_value is None`, preserves all sides and locators.
    - `SINGLE_SOURCE_UNCORROBORATED` vs `INDEPENDENTLY_CORROBORATED`: deduplicates syndicated claims sharing the same `upstream_origin` / `canonical_url` / upstream source so reposts never count as independent corroboration.
    - `UNKNOWN`: distinguishes `NO_RELIABLE_EVIDENCE_IN_KB` from `CONFIRMED_UNPUBLISHED`; `adopted_value` is strictly `None` (never `0`).

- **AC-213-P0-3 (Independent Oracle v2.1.3 & Verbatim Receipt Guard)**:
  - `OracleQuestion` backwards-compatibly adds `required_scope_tokens: tuple = ()`, `expected_posture: str | None = None`, and `designated_locators: tuple = ()` (default empty values preserve exact legacy 9-key `to_dict()` digest).
  - `score_answer` / `score_answer_approved` enforce scope token presence (`dimension="scope"`), designated locator subset coverage (`dimension="locator"`), and evidence posture discipline (`dimension="posture"`, rejecting noisy second-source warnings on `QUIET_CLEAN_OFFICIAL` and rejecting false "0 / confirmed unpublished" claims on `LOUD_UNKNOWN_KB_GAP`).
  - `ClientReceipt` adds `receipt_mode: str = "VERBATIM"`; `validate_receipt` / `validate_receipt_approved` reject `SUMMARY_ONLY`, `SIMULATED_CLIENT`, `[SUMMARY]`, or `REAL_CLIENT_NOT_EXECUTED` placeholders and enforce `designated_locators`.

- **AC-213-P1 (Bounded Revision Explanation)**:
  - Explicit `supersedes_record_id` stats revisions and `status="superseded"` / `prior_claim_id` claim chains classify as `REVISION_SUPERSEDED`, adopt the new value, retain `prior_value` + `new_value` + `revision_reason` in `revision_history`, and render both old and new values in `research_card`.

- **AC-213-REG (Zero Regression & T1 Path Discipline)**:
  - Zero existing tests deleted or weakened; all changes strictly confined to `T1` (`src/cba_kb/*.py`) and `T0` (`tests/test_req213_*.py`, `docs/validation/*.md`).
