# CBA-KB | Basketball research you can trace back to evidence

[简体中文](README.zh-CN.md) · [5-minute quick start](#5-minute-quick-start) · [Operations guide](docs/operations/OPERATIONS.md)

**CBA-KB is an open-source research and document engine for Chinese Basketball Association (CBA) basketball.** It helps researchers and AI tools organize player records, league notices, interviews, and other documents without confusing an unverified report with a confirmed fact.

**Latest official Engine release: [GitHub Releases](https://github.com/SkyOneBranch/cba-kb-engine/releases/latest).**

## Why CBA-KB?

Suppose one report says a player may join a team, while an official registration lists a different team. A search summary can make the rumor sound authoritative. CBA-KB keeps the layers separate:

- **Source:** Where did a record come from?
- **Document:** What does the original file or article say?
- **Statement / Claim:** What was asserted, and what still needs checking?
- **Fact / Stats:** What can be supported by verified records and an exact statistical grain?

**Core rule: Statement ≠ Fact. Evidence ≠ Fact. Unknown ≠ 0.** Answers should preserve identity, dates, authorization, source links, and uncertainty rather than guess.

## How it works

```text
Authorized source files and records
           ↓
Source / Document / provenance / rights
           ↓
Identity · Fact · Stats · Statement / Claim
           ↓
Versioned, authorized query and evidence views
           ↓
Researcher or AI consumer verifies the answer
```

The public repository provides the **Engine**, its schemas, tools, and synthetic examples. The researcher's real documents, access grants, and credentials belong in a separate **Private Instance**. No real CBA private corpus is included here.

## Key capabilities

| Research task | What the Engine provides |
| --- | --- |
| Organize material | Register sources and documents, detect duplicates, and retain traceable metadata |
| Find evidence | Search indexed content and follow source/document references |
| Manage Drive-backed files | Discover changes, prepare bounded archive/retain plans, perform authorized operations, and verify results |
| Research players | Distinguish player identities, official facts, source claims, and exact season/team statistical grains |
| Prepare AI-readable evidence | Build versioned query slices, passages, snapshot/manifest bindings, and permission-aware consumption records |

The Engine does **not** provide a hosted chatbot or a live HTTP/MCP query service. Local Engine processing prepares evidence; ChatGPT Web can read a separately authorized Google Drive projection through its own connector when that projection has been explicitly published and is visible to the user.

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

This example does not move or publish real files. See the [Operations guide](docs/operations/OPERATIONS.md) for local Inbox, indexing, and authorized Drive workflows. Code in [`main`](https://github.com/SkyOneBranch/cba-kb-engine) may be ahead of the published tag.

## Product progress & roadmap

### Recent milestones

| Version | Main outcome | Status |
| --- | --- | --- |
| v1.5.x–v1.9.0 | Player registration and export, document evidence, source acquisition, Statements and Claims | Historical milestones |
| v2.0.2 | Reusable public Engine, licensing, synthetic fixtures, reproducible tooling | Historical milestone |
| [v2.0.4-2](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.0.4-2) | Inbox and Drive discovery, guarded archiving, document search, recovery | Released milestone |

Evidence-consumption components have been tested with synthetic materials, including isolated ChatGPT Web + Google Drive checks. These tests support development but do not establish readiness for a particular private dataset, user, or production environment.

### What's next

- **Evidence-backed basketball research:** make player identities, facts, statistics, and disputed reports easier to compare and verify against original sources.
- **Reliable AI knowledge access:** improve permission-aware, up-to-date reading of research materials through authorized tools, with traceable citations and clear uncertainty.
- **Simpler research workflows:** make collecting, organizing, updating, and finding materials easier while strengthening privacy boundaries and reusing existing Engine components.

These are directions, not guaranteed release dates. See [Releases](https://github.com/SkyOneBranch/cba-kb-engine/releases) and [Issues](https://github.com/SkyOneBranch/cba-kb-engine/issues) for confirmed changes.

## Limitations, authorization, and safety

- **No automatic promotion to Fact.** Being able to read a Drive file, or seeing a source in a manifest, does not by itself authenticate each claimed Fact. Fact-source mapping and cross-source conflict checks still require careful validation.
- **Identity ambiguity is not a match.** Same-name candidates must remain unresolved until supported by authorized identity evidence. Do not merge players based only on names.
- **Google Drive visibility is conditional.** A producer upload is not proof that ChatGPT observed the current material. Connector indexing, authorization, and revocation must be checked for the target and time of use.
- **Multi-user access is limited.** Some lower-level Engine search paths do not consistently enforce per-user authorization ([Issue #169](https://github.com/SkyOneBranch/cba-kb-engine/issues/169)); do not expose them directly as an authorization boundary to untrusted users.
- **Synthetic tests are not production approval.** Before real-data use, operators must verify the relevant permissions, source bindings, freshness, and consumer behavior for their own environment.
- **Respect upstream rights.** Permission to read or privately analyze documents does not authorize redistribution.

## Documentation, contributing, and licenses

- [Operations Manual](docs/operations/OPERATIONS.md) · [中文操作手册](docs/operations/OPERATIONS.zh-CN.md)
- [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)
- [Releases](https://github.com/SkyOneBranch/cba-kb-engine/releases) · [Issues](https://github.com/SkyOneBranch/cba-kb-engine/issues)
- [Licensing policy](DATA_LICENSE.md) · [Third-party data](THIRD_PARTY_DATA.md)

The **Public Engine** code is licensed under [Apache-2.0](LICENSE). Documentation is under [CC BY 4.0](DATA_LICENSE.md), and repository synthetic fixtures are under [CC0-1.0](DATA_LICENSE.md). Third-party articles, photographs, and other primary materials retain their owners' rights. Private research data and credentials must remain in the operator's separate Private Instance.

**Make basketball research easier to verify—not just more confident-sounding.**
