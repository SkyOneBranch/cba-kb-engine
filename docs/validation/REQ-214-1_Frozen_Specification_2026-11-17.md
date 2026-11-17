# REQ-214-1 Frozen Specification (v2.1.4-1 — Upgrade LACD governance baseline from v1.0.2 to v1.0.4)

- **Requirement ID**: `REQ-214-1`
- **Target Version**: `v2.1.4-1` (Engine-only / Governance Patch Release; zero business semantics, zero Google Drive Production change; Production `current` stays `v2.1.4`)
- **Baseline HEAD SHA**: `7f99c6bff0a2506c845584ffb7ac2098a0e150d3` (main, PR #247 merge)
- **Issue**: `#248` (`[v2.1.4-1][T2] Upgrade LACD governance baseline from v1.0.2 to v1.0.4 (REQ-214-1)`)
- **Branch**: `muse/issue-248`
- **Upstream**: LACD `v1.0.4`, `SkyOneBranch/LACD@v1.0.4`, commit `5aaf063c451b17ca01657d06c8c2eddf8cf68ff7`
- **Governance**: LACD v1.0.2 at baseline (`pull_request_required=true`, `human_merge_required=true`, `bind_to_commit_sha=true`, `fail_closed=true`); `FINAL_TIER = T2`
- **Routing**: Bot Broker only, bot-authored PR, `status:hold` until Human adjudication; Human Approve/Merge only. No self-approval, no Required Check bypass, no Merge by Developer. Never use `lacd upgrade --prepare-pr`.
- **Frozen by**: this file + `tests/test_req214_1_lacd_upgrade_acceptance.py` (Commit 1). Commit 2 must not modify either file by one byte.

## 1. Hard boundaries (forbidden changes)

- Do NOT modify `scripts/prepare_production.py` (existing test asserts `v2.1.4-1` triggers `RELEASE_ID_FORBIDDEN:v2.1.4-1`).
- Do NOT modify `pyproject.toml` (existing test asserts `[project].version == "2.0.2"`).
- Do NOT modify any existing `tests/**/*.py` file.
- Do NOT include any change from the original candidate `v2.1.5` or any automated information-collection module.
- Do NOT commit `.lacd/backup/` (upgrade backup must be deleted locally before commit).
- Do NOT hand-edit any byte inside the `AGENTS.md` LACD managed block after `lacd upgrade` writes it.
- Docs in `docs/**` must be fully de-identified: no local filesystem paths, no Drive IDs, no tokens.

## 2. Frozen Acceptance Criteria (AC-214-1)

- **AC-1 (managed workflow upgrade)**:
  - `.github/workflows/lacd-verify.yml` is the LACD 1.0.4 template, file SHA-256 `7eea80607c0a91eff1416253127db0801e1cbf5e944f6090f00e44896cab3c2b`.
  - Install source is `git+https://github.com/SkyOneBranch/LACD.git@v1.0.4`; no `SkyCjq/LACD.git@v1.0.2` remains.
- **AC-2 (managed protocol block + project wording sync)**:
  - `AGENTS.md` managed block is `<!-- LACD:MANAGED:BEGIN section="lacd-agent-protocol" version="1.0.4" --> ... <!-- LACD:MANAGED:END -->`; the section bytes exactly between the BEGIN and END markers (LACD `migrate.py` `_extract_section` semantics, no added/stripped newline; the 1.0.4 upgrade writes the BEGIN marker directly followed by the template bytes) have SHA-256 `f02da4680881afc425e5c67f33ae7bf085e38a3b66b174f3f3bb09b6cb3ea0e0`. Do NOT insert a newline after the BEGIN marker: that would change the section hash and fail `lacd verify`.
  - Project-owned lines 1, 3, 26, 47 outside the block change `LACD v1.0.2` to `LACD v1.0.4`; no `LACD v1.0.2` remains anywhere in `AGENTS.md`.
- **AC-3 (lock sync + strong upstream binding)**:
  - `.lacd/lock.json`: `lacd_cli_version == "1.0.4"`, `template_version == "1.0.4"`, `generated_by == "lacd 1.0.4"`.
  - Managed entry `.github/workflows/lacd-verify.yml`: `template_version == "1.0.4"`, `sha256 == "7eea80607c0a91eff1416253127db0801e1cbf5e944f6090f00e44896cab3c2b"`.
  - Shared entry `AGENTS.md`: `template_version == "1.0.4"`, `sha256 == "f02da4680881afc425e5c67f33ae7bf085e38a3b66b174f3f3bb09b6cb3ea0e0"`.
  - `source == {"type": "git-tag", "repo": "SkyOneBranch/LACD", "ref": "v1.0.4", "commit": "5aaf063c451b17ca01657d06c8c2eddf8cf68ff7"}`.
  - `lacd status --json` reports `source_pin.strength == "strong"`, `version_consistency.status == "consistent"` (LACD 1.0.4 enum is `consistent` / `mismatch` / `unknown`, per `src/lacd/core/state.py`; there is no `match` value), `state == "VALIDATED"`, `drift.status == "clean"`.
- **AC-4 (local gates + no residue)**:
  - `lacd verify --local --strict` exits 0.
  - No `.lacd/backup` directory and no untracked residue in the worktree.
  - `.lacd/project-rules.yaml` SHA-256 `3546ae7753cf0ecbc28f793e0532f9ba0eee673b7d63927d5186142419b57f1e` unchanged; `lacd.yaml` SHA-256 `830587bef042655c847c6ec4e740cb04f4dfffd2097ea26e111d3a96e21a68f2` unchanged.
  - `scripts/prepare_production.py` SHA-256 `0ccb79429fb4ec5e270a3154af987475ae75bd4ef81de48c1e665a3b93cf4573` unchanged; `pyproject.toml` `[project].version == "2.0.2"`.
  - New wiring test (`tests/test_req214_1_wiring.py`) proves a real call chain: `tiering.py` classifies the changed paths as `T2`, `gate_selftest.py` passes, and a real `lacd` CLI call (when present) returns 0 for `lacd verify --local --strict`, otherwise the lock/template contract is verified structurally so offline CI stays green.

## 3. Falsification (Commit 1 red)

- The acceptance suite in `tests/test_req214_1_lacd_upgrade_acceptance.py` must FAIL on baseline `7f99c6bff0a2506c845584ffb7ac2098a0e150d3` because the 1.0.4 assertions are violated (0 collection/import errors), and PASS after Commit 2 with no modification to the suite itself.
- Evidence hashes in this file were verified against the LACD 1.0.4 template bundle manifest (`managed/agents-md.section.md` and `workflows/lacd-verify.yml`) before Commit 1.

## 4. Review and release

- Independent Q1 on this frozen spec + Commit 1; independent Q2 on the final HEAD (at most 3 executions per review task, then STOP with limit marker).
- Q2 receipt must carry the six bounded-review fields (`reviewer`, `model`, `time`, `reviewed_head_sha`, `verdict`, `raw`).
- After Human Merge, RRC then tag `v2.1.4-1` + Engine-only Release, then PRV. RRC evidence is never written back to `main`.
