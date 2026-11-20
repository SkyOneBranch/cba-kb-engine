# CBA-KB | Basketball research you can trace back to evidence

[简体中文](README.zh-CN.md) · [5-minute quick start](#5-minute-quick-start) · [Operations guide](docs/operations/OPERATIONS.md)

**CBA-KB is an open-source research and document engine for Chinese Basketball Association (CBA) basketball.** It helps researchers and AI tools organize player records, league notices, interviews, and media reports without confusing an unverified statement with a confirmed fact.

**Latest official Engine release: [v2.2.0](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.2.0) ([all GitHub Releases](https://github.com/SkyOneBranch/cba-kb-engine/releases/latest)).**

## Why CBA-KB?

Suppose one report says a player may join a team, while an official registration lists a different team—or an article is silently edited hours after publication. A plain search summary can make rumors or stale text sound authoritative. CBA-KB keeps the layers separate:

- **Source:** Where did a record come from, what is its upstream origin, and did a later revision supersede it?
- **Document:** What does the archived file or `acquisition_bundle_v1` package actually say?
- **Statement / Claim:** What was asserted, and what still needs checking?
- **Fact / Stats:** What is backed by verified records and an exact statistical grain?

**Core rule: Statement ≠ Fact. Evidence ≠ Fact. Unknown ≠ 0.** Answers preserve player identity boundaries, dates, authorization, revision history, source links, and uncertainty rather than guess.

## How it works

```text
Authorized records, official snapshots & acquisition_bundle_v1 packages
                               ↓
     Atomic DocumentLane archive · dedup · same-URL revision chains
                               ↓
          Identity · Fact · Stats · Statement / Claim
                               ↓
  Dirty-set incremental research cards (<=10 citations) & cba-kb ops-sync
                               ↓
   Explicitly published, self-describing consumer views (Drive / local)
                               ↓
         Researcher or AI consumer verifies the cited answer
```

The public repository provides the deterministic **Engine** (zero web-crawler or live LLM runtime dependencies), schemas, CLI tools, and synthetic fixtures. Real corpora, external acquisition sidecars, access grants, and credentials stay in a separate **Private Instance**. No private CBA corpus is included here.

## Key capabilities

| Research task | What the Engine provides |
| --- | --- |
| Ingest & archive material | Validate 18-field `acquisition_bundle_v1` packages and local documents with staging-atomic archiving, single-pass deduplication, and upstream-origin folding |
| Track source revisions | Preserve prior versions when a `canonical_url` changes text, link bidirectional revision chains (`LOUD_REVISION`), and replay archived registration snapshots offline |
| Maintain player research cards | Recompute only affected approved players (dirty-set) while keeping ambiguous names `UNDECIDED` and capping per-card external citations (`<= 10`) |
| Run operational sync & Drive plans | Execute deterministic local `cba-kb ops-sync` checkpoints with consumer freshness lag reports, plus lock-guarded Drive discovery, archive planning, and atomic publishing |
| Prepare AI-readable evidence | Emit self-describing `CURRENT` tables, research cards, slices, snapshot/manifest bindings, and inline-citation verification (`oracle-scoring-v8`) |

The Engine does **not** run a background daemon, hosted chatbot, or live HTTP/MCP query server, and `cba-kb ops-sync` never mutates a production Google Drive directly. Local runs prepare verified candidate projections and freshness reports; operators publish to Google Drive only through explicit release cutovers.

## 5-minute quick start

Requires **Python 3.11+** and **Git** on macOS, Linux, or a compatible shell. This example uses public synthetic data; it does **not** require a Google Drive account or any private basketball documents.

```bash
git clone https://github.com/SkyOneBranch/cba-kb-engine.git
cd cba-kb-engine

# Check out the latest official release tag from GitHub Releases.
git checkout "$(python3.11 -c 'import json,urllib.request; print(json.load(urllib.request.urlopen("https://api.github.com/repos/SkyOneBranch/cba-kb-engine/releases/latest"))["tag_name"])')"

python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps --no-build-isolation -e .

make doctor
cba-kb --help

# Run a clean-room example with fictional records.
unset CBA_KB_INSTANCE_ROOT CBA_KB_SOURCE_POLICY_JSON CBA_STATS_AES_KEY GOOGLE_APPLICATION_CREDENTIALS
python scripts/run_cleanroom_smoke.py --output .cleanroom/ci-smoke
```

This example does not move or publish real files. See the [Operations guide](docs/operations/OPERATIONS.md) for local Inbox, `cba-kb ops-sync`, indexing, and authorized Drive workflows. Code in [`main`](https://github.com/SkyOneBranch/cba-kb-engine) may be ahead of the published tag.

## Product progress & roadmap

### Recent milestones

| Version | Main outcome | Status |
| --- | --- | --- |
| v1.5.x–v1.9.0 | Player registration and export, document evidence, source acquisition, Statements and Claims | Historical milestones |
| v2.0.2 | Reusable public Engine, licensing, synthetic fixtures, reproducible tooling | Historical milestone |
| [v2.0.4-2](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.0.4-2) | Inbox and Drive discovery, guarded archiving, document search, recovery | Historical milestone |
| [v2.1.0](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.1.0)–[v2.1.1](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.1.1) | Drive-first controlled consumption entry (`ConsumeSession`), snapshot/fact guarantees | Historical milestones |
| [v2.1.3](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.1.3) | Exception-aware research card projections and independent scoring oracle | Historical milestone |
| [v2.1.4](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.1.4) / [v2.1.4-1](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.1.4-1) | Deterministic test/CI substrate, file lock hardening, LACD v1.0.4 governance alignment | Historical milestones |
| [v2.1.5](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.1.5) | Drive search-hygiene archive isolation, self-describing `CURRENT` consumer surfaces, promoted player research cards, inline-citation Oracle v8 | Released milestone |
| [v2.2.0](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.2.0) | Operational KB: native `acquisition_bundle_v1` contract, same-URL revision chains (`LOUD_REVISION`), dirty-set incremental card recompute (`<= 10` citations), unified registry locking & `cba-kb ops-sync` freshness lag reports | Latest release |

Engine releases and private Google Drive data cutovers are decoupled by design: an Engine release ships verified compiler and CLI capabilities without mutating live consumer projections until an operator explicitly runs a staged Drive release.

### What's next

- **Broader approved player & multi-season coverage:** expand human-verified identity mappings, research cards, and longitudinal registration/statistical comparisons across seasons.
- **Low-friction operational cadence:** use `cba-kb ops-sync` freshness lag reports and `verification_queue` summaries to batch routine document intake locally and trigger Drive cutovers only when meaningful updates accumulate.
- **Stronger citation & connector resilience:** refine consumer surface formatting and oracle diagnostics against real-world AI connector behaviors (such as link-label paraphrasing and conversation context limits).

These are directions, not guaranteed release dates. See [Releases](https://github.com/SkyOneBranch/cba-kb-engine/releases) and [Issues](https://github.com/SkyOneBranch/cba-kb-engine/issues) for confirmed changes.

## Limitations, authorization, and safety

- **No automatic promotion to Fact.** Being able to read a Drive file, or ingesting an `acquisition_bundle_v1` document, does not authenticate claimed Facts or overwrite `MASTER` records. Fact-source mapping and conflict checks require explicit validation.
- **Identity ambiguity is not a match.** Same-name candidates remain `UNDECIDED` in the verification queue until backed by authorized identity evidence. Do not merge players based only on names.
- **Google Drive visibility is conditional.** Local `cba-kb ops-sync` runs and producer uploads are separate from what an AI connector observes; connector indexing, authorization, and revocation must be verified for the target environment.
- **Caller identity boundary for search authorization.** [Issue #169](https://github.com/SkyOneBranch/cba-kb-engine/issues/169) is **closed** (PR `#170` implemented fail-closed per-`principal` ACL enforcement in `drive_search` and `drive_search_index`). Because those helpers trust the caller-supplied `principal` string without an authentication server, do not expose them directly as an unauthenticated multi-user public endpoint.
- **Synthetic tests are not production approval.** Before real-data use, operators must verify permissions, source bindings, freshness, and consumer behavior in their own environment.
- **Respect upstream rights.** Permission to read or privately analyze documents does not authorize public redistribution.

## Documentation, contributing, and licenses

- [Operations Manual](docs/operations/OPERATIONS.md) · [中文操作手册](docs/operations/OPERATIONS.zh-CN.md)
- [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)
- [Releases](https://github.com/SkyOneBranch/cba-kb-engine/releases) · [Issues](https://github.com/SkyOneBranch/cba-kb-engine/issues)
- [Licensing policy](DATA_LICENSE.md) · [Third-party data](THIRD_PARTY_DATA.md)

The **Public Engine** code is licensed under [Apache-2.0](LICENSE). Documentation is under [CC BY 4.0](DATA_LICENSE.md), and repository synthetic fixtures are under [CC0-1.0](DATA_LICENSE.md). Third-party articles, photographs, and other primary materials retain their owners' rights. Private research data and credentials must remain in the operator's separate Private Instance.

**Make basketball research easier to verify—not just more confident-sounding.**
