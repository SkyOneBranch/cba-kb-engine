# CBA-KB | Basketball research you can trace back to evidence

[简体中文](README.zh-CN.md) · [Quick start](#5-minute-quick-start) · [Operations guide](docs/operations/OPERATIONS.md)

**A headline is not a verified fact.** CBA-KB is an open-source, evidence-first knowledge engine for research on the Chinese Basketball Association (CBA). It helps researchers and developers organize reports, rosters, official notices, and documents without losing their sources, identities, rights, or verification status. The question behind every result is simple: **“Where did this come from?”**

> **Current release (October 18, 2026):** [v2.0.4-2](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.0.4-2) is the latest published Engine Release. The project owner has **closed this delivery cycle with documented limitations carried forward**. An owner-authorized private deployment completed real Google Drive file organization and an accepted, source-traceable private Document Consumer query. This does **not** enable production writes for other installations, certify every search or security path, or authorize public redistribution of private material.
>
> **Version identities are separate:** the Python package metadata remains `2.0.2`; the distinct Drive Data Production release remains `v2.0.1-1`. The `v2.0.4-2` tag is the published Engine snapshot; `main` can contain subsequent changes.

## Why CBA-KB exists

Imagine researching whether a player has transferred. One report says a move is rumored; a separate official roster still lists the previous club. A search result or an AI summary could easily turn that rumor into a fact.

CBA-KB is designed to keep the pieces separate:

- **Source:** Where did the information originate?
- **Document:** What was the original file, and has its revision changed?
- **Statement:** What did the source actually say?
- **Claim:** What assertion still needs independent verification?
- **Fact:** What conclusion has adequate authoritative evidence and a verification record?

**Core rule: Statement ≠ Fact. Evidence ≠ Fact. Unknown ≠ 0.** If identity, rights, or evidence cannot be established, the result should remain unresolved or go to human review—not be guessed.

## What can it do?

| Your goal | How CBA-KB helps | Current boundary |
| --- | --- | --- |
| Organize research | Link sources, documents, player identity, and provenance | Public Engine includes data models and CLI tools |
| Examine a report | Separate statements, claims, verified facts, and outstanding checks | AI-generated summaries never become verified facts automatically |
| Work with a local Inbox | Discover `.txt` files, register Source/Document, deduplicate, inspect status, and search | Requires valid provenance and rights metadata; see the synthetic example below |
| Discover Google Drive material | Discover files, track revisions, build searchable document indexes, and prepare guarded plans | Capabilities require appropriate credentials and permissions; direct Engine search still has a per-principal isolation limitation |
| Organize and archive Drive files | Prepare exact plans, require approval, perform guarded operations, and inspect readback | Successfully exercised in an authorized private deployment; other deployments need their own authorization |
| Support evidence-based AI reading | Preserve Source/Document references and original-file provenance for a Consumer | A representative private Document query was accepted; this is not a hosted public Q&A service or a guarantee of every Consumer capability |

**CBA-KB is not a chatbot or a service that automatically crawls the whole web and declares basketball facts.** It is a reusable engine; you choose appropriate data sources, run it in your environment, and define human-review processes.

## Product progress & roadmap

### Recent milestones

- **Evidence-first basketball research (v1.8.x–v1.9.0):** Source and Document evidence, player identity, Statements, Claims, verification queues, and research views.
- **Reusable public Engine (v2.0.2):** Open-source code, licensing, a sanitized repository, synthetic fixtures, and reproducible tooling.
- **Drive-backed Inbox-to-Knowledge workflow (v2.0.4-2):** Local Inbox processing, Drive discovery and polling, guarded file operations and recovery, and document search improvements. The Engine Release is published and the owner has closed this delivery cycle; actual Drive organization and a private Document Consumer query were accepted in an authorized private instance. Known limitations remain documented below.

### What's next

These are **areas under consideration**, not an approved next-release scope or a delivery schedule:

- **Principal-aware search authorization:** Close the Engine-level cross-principal isolation gap tracked in [Issue #169](https://github.com/SkyOneBranch/cba-kb-engine/issues/169).
- **More dependable Consumer source navigation:** Keep displayed document identities and original-file links aligned with authoritative source mappings.
- **Incremental knowledge availability:** Make it easier to carry newly ingested, authorized documents through indexing and into Consumer data without rebuilding an entire collection.

See [Releases](https://github.com/SkyOneBranch/cba-kb-engine/releases) for version history and [Issues](https://github.com/SkyOneBranch/cba-kb-engine/issues) for tracked work. Plans may change.

## 5-minute quick start

Requirements: Python **3.11+** and Git. Commands below target Linux/macOS or a compatible terminal. You do not need private CBA data or Google credentials.

```bash
# 1. Clone the public repository
git clone https://github.com/SkyOneBranch/cba-kb-engine.git
cd cba-kb-engine

# 2. Install into an isolated Python environment
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps --no-build-isolation -e .

# 3. Environment check and offline tests
make doctor
make test

# 4. Explore available commands
cba-kb --help
```

The default clone uses **`main`**, which may include changes made after the published release (including the merged Chinese-search fix in PR #191). To reproduce the exact published **v2.0.4-2 Engine snapshot**, run `git checkout v2.0.4-2` **before installation**. To reproduce the v2.0.2 source instead, check out the `v2.0.2` tag. The package's `2.0.2` metadata does not identify which of these source snapshots is installed.

### Try 1: an entirely synthetic research workflow

```bash
# Keep private-instance environment variables out of the public smoke test
unset CBA_KB_INSTANCE_ROOT CBA_KB_SOURCE_POLICY_JSON CBA_STATS_AES_KEY GOOGLE_APPLICATION_CREDENTIALS

# Uses public synthetic fixtures, not your private source material
python scripts/run_cleanroom_smoke.py --output .cleanroom/ci-smoke
```

For individual Source, Statement, Claim, and Research View commands, see the [public operations guide](docs/operations/OPERATIONS.md).

### Try 2: local Inbox discovery and search (development capability)

```bash
# Keep instance data outside the repository
mkdir -p /tmp/cbakb-demo/config /tmp/cbakb-demo/inbox/documents

cat > /tmp/cbakb-demo/inbox/documents/demo.txt <<'TXT'
Synthetic CBA Bulletin
The Harbor Falcons won a fictional preseason game.
TXT

# Local TXT files need accompanying provenance/rights metadata
cat > /tmp/cbakb-demo/inbox/documents/demo.txt.source.json <<'JSON'
{
  "schema_version": 1,
  "title": "Synthetic CBA Bulletin",
  "origin": "synthetic_fixture",
  "source_url": null,
  "rights": {
    "classification": "private",
    "public_export_allowed": false,
    "evidence": []
  }
}
JSON

# Run a second scan after the stability interval
cba-kb --instance-root /tmp/cbakb-demo inbox-scan
sleep 2
cba-kb --instance-root /tmp/cbakb-demo inbox-scan

# Inspect status and try a keyword query
cba-kb --instance-root /tmp/cbakb-demo archive-status --all
cba-kb --instance-root /tmp/cbakb-demo archive-query --keyword "Harbor Falcons"
```

This demo does not move, delete, or publish originals. Actual results depend on the command output; invalid rights metadata, an unstable file, or a review requirement may prevent ingestion. See the [operations and safety details](docs/operations/OPERATIONS.md) before running with real content.

## Google Drive: organize files with approval

CBA-KB can discover and track Drive files, then prepare an explicit plan for archiving or retaining them. **A file move requires authorization and a check of the actual result.** If required file or revision evidence is missing, the operation must stop.

The guarded move implementation was released in v2.0.4-2. The project owner's private production instance has since executed authorized file organization and readback. This does **not** grant other installations permission to write to production Drive: each real operation remains subject to that environment's authorization and safeguards. The owner-selected operating mode is manual/on-demand; an always-running background worker is not a prerequisite. Keep OAuth credentials and private folder information outside this public repository.

## Limitations, privacy, and security

- **Known search authorization gap:** The current public Engine's direct search/index paths do not consistently enforce a trusted caller identity against per-document `allowed_principals`. Do **not** expose those paths as an authorization boundary to untrusted or multi-user clients. Follow [Issue #169](https://github.com/SkyOneBranch/cba-kb-engine/issues/169) for the outstanding fix. Local OS permissions and a private portal can reduce exposure in a particular deployment, but they do not prove Engine-level per-principal isolation.
- **Private reading is not public export:** Authorized private ingestion, indexing, and AI-assisted reading do not imply permission to publish or redistribute source files. Retain applicable rights and provenance checks.
- **Scope of acceptance:** A representative, source-traceable private Document Consumer query has been accepted by the project owner. This does not independently certify every document, file format, rights-negative case, or Statement/Claim/Fact/Timeline research capability. Any unresolved evidence stays unresolved.
- **Version and deployment scope:** An Engine Release tag, installed package metadata, a private runtime commit, a Drive Data Production release, and a Consumer dataset can refer to different valid states. Do not equate a successful private deployment with activation for every installation.

## Who is it for?

- **Basketball researchers and curators** who want every claim connected to its source and verification status.
- **Developers** who want to reuse CBA data contracts, identity mapping, ingestion tools, and synthetic examples.
- **AI application builders** who want to keep rumors, source statements, and verified facts separate.

The repository provides a **Public Engine**. Real private material is operated separately in a user's **Private Instance** under applicable permissions and law. Code uses [Apache-2.0](LICENSE), docs use [CC BY 4.0](DATA_LICENSE.md), and synthetic fixtures use [CC0-1.0](DATA_LICENSE.md). Third-party reporting and photographs retain their owners' rights; see [third-party data](THIRD_PARTY_DATA.md).

## Documentation and contributing

- [Operations Manual](docs/operations/OPERATIONS.md) · [中文操作手册](docs/operations/OPERATIONS.zh-CN.md)
- [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)
- [Data and license policy](DATA_LICENSE.md) · [Third-party materials](THIRD_PARTY_DATA.md)
- [GitHub Issues](https://github.com/SkyOneBranch/cba-kb-engine/issues) · [Pull Requests](https://github.com/SkyOneBranch/cba-kb-engine/pulls)

**The aim is not to make AI sound more certain. It is to make basketball research easier to check against real evidence.**
