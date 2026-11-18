# REQ-215 Stage 4 Frozen Specification — `v2.1.5-1` Release Spec & Consumer Surface Binding

- **Requirement ID**: `REQ-215` (Stage 4 Release Spec — PR 2)
- **GitHub Issue**: `#252` (`T2`, `src/**/release/**`)
- **Base `main` SHA (`V215_BASELINE_SHA`)**: `3874ebb4b20fdfd5c1d1fd7f9ed9426d9f26036f` (`v2.1.5` Engine Release merge commit)
- **Target Production Release ID**: `v2.1.5-1`
- **Target Product Version**: `v2.1.5`
- **Governance**: `LACD v1.0.4` (`pull_request_required=true`, `human_merge_required=true`, `bind_to_commit_sha=true`, `fail_closed=true`)

## Frozen Contract

1. **`prepare_v215.V215_RELEASE_SPECS["v2.1.5-1"]` Registration**:
   - `cba_kb.orchestration.release.prepare_v215.V215_RELEASE_SPECS["v2.1.5-1"]` equals:
     - `{"product_baseline_sha": "3874ebb4b20fdfd5c1d1fd7f9ed9426d9f26036f", "enable_consumer_surface_v215": True}`
   - `prepare_v215._require_release("v2.1.5-1")` and `prepare_v215._release_spec("v2.1.5-1")` return that exact dictionary.
   - Unregistered release IDs (`"v2.1.5"`, `"v2.1.5-2"`, `"v9.9.9"`) raise `prepare_v215.ProjectionError("RELEASE_ID_FORBIDDEN:<id>")`.

2. **Frozen Base Orchestration Script Preservation**:
   - `scripts/prepare_production.py` remains 100% unmodified at SHA-256 `0ccb79429fb4ec5e270a3154af987475ae75bd4ef81de48c1e665a3b93cf4573`.
   - `scripts.prepare_production._require_release` continues to reject `"v2.1.5"` and `"v2.1.5-1"` (`RELEASE_ID_FORBIDDEN`).

3. **Release Identity Preflight (`automation.hardening.validate_release_identity`)**:
   - Without monkeypatching `V215_RELEASE_SPECS`, calling `validate_release_identity` with `target_product_version="v2.1.5"`, `proposed_release_attempt="v2.1.5-1"`, `product_candidate_sha="3874ebb4b20fdfd5c1d1fd7f9ed9426d9f26036f"`, and `release_execution_role="RELEASE_EXECUTOR"` returns `status == "PASS"` and `canonical_product_baseline_sha == "3874ebb4b20fdfd5c1d1fd7f9ed9426d9f26036f"`.
   - Unregistered attempt `"v2.1.5-2"` raises `HardeningFailure` with classification `RELEASE_IDENTITY_NOT_READY`.

4. **Projection & Git Ancestry**:
   - `prepare_v215.project_targets(..., release_id="v2.1.5-1", engine_sha="3874ebb4b20fdfd5c1d1fd7f9ed9426d9f26036f", ...)` returns `state == "PROJECTED"`, `release_id == "v2.1.5-1"`, and `engine_sha == "3874ebb4b20fdfd5c1d1fd7f9ed9426d9f26036f"`.
   - `git merge-base --is-ancestor 3874ebb4b20fdfd5c1d1fd7f9ed9426d9f26036f HEAD` exits `0`.

5. **Closure Contract & Technical Manual Binding**:
   - `prepare_v215._uses_closure("v2.1.5-1")` and `prepare_v215._uses_technical_manual("v2.1.5-1")` return `True`.
   - `prepare_v215._build_closure_contract` for `release_id="v2.1.5-1"` fails closed with `CLOSURE_CONTROL_TARGET_MISSING` when `entry/context` is absent and binds `documents["technical_manual"] == "entry/context"` when present.

6. **Out-of-Scope / Boundary Constraints**:
   - `pyproject.toml` `[project].version` stays `"2.0.2"`.
   - Pre-existing `tests/**/*.py` files remain untouched.
   - Only `src/cba_kb/orchestration/release/prepare_v215.py` is modified in Commit 2.
