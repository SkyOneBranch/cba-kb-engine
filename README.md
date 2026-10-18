# CBA-KB | Basketball research you can trace back to evidence

[简体中文](README.zh-CN.md) · [Quick start](#5-minute-quick-start) · [Operations guide](docs/operations/OPERATIONS.md)

**CBA-KB is an open-source research and document engine for Chinese Basketball Association (CBA) basketball.** It helps you organize player records, news, league notices, and other files so people and AI tools can find, compare, and check information against its sources.

**Current release: [v2.0.4-2](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.0.4-2) (published; this delivery cycle is complete).**

## Why CBA-KB?

Imagine looking into a player's reported transfer. One article says the move is possible, while an official roster still lists the previous team. A search summary can easily turn that rumor into a fact.

CBA-KB helps keep these things separate:

- **Source:** Where did the information come from?
- **Document:** What is the original report or file?
- **Statement:** What did the source actually say?
- **Claim:** Which assertions still need checking?
- **Fact:** Which conclusions have enough supporting evidence?

**Core rule: Statement ≠ Fact. Evidence ≠ Fact. Unknown ≠ 0.** An AI answer should point back to its sources rather than present a guess as a verified fact.

## What can it do?

| What you want to do | How CBA-KB helps |
| --- | --- |
| Organize material | Register files and sources, detect duplicates, and keep traceable records |
| Find content | Search indexed documents by keyword and follow results back to their sources |
| Manage Google Drive files | Discover changes, plan archive or retain actions, execute authorized moves, and verify results |
| Research players and teams | Use identity, statement, claim, and fact models to keep different kinds of evidence distinct |
| Support AI-assisted reading | Provide authorized document content and source information for checking answers |

CBA-KB is **not** a chatbot that crawls the entire web and declares basketball facts automatically. It provides tools for working with documents and evidence; users remain responsible for data, verification, and permissions.

## Major versions and current status

| Version | Main changes | Status |
| --- | --- | --- |
| **v1.5.x** | Player registration model, reproducible Excel exports, and early Drive publication tools | Historical milestone |
| **v1.8.x** | Document evidence, stable player identities, and consumer checks | Historical milestone |
| **v1.9.0** | Source ingestion, Statements, Claims, verification queues, and research views | Historical milestone |
| **v2.0.2** | Open-source Engine, licensing, repository sanitization, synthetic fixtures, and reproducible tooling | Historical milestone |
| **v2.0.4-2** | Local Inbox, Drive discovery and polling, guarded archiving, search indexes, and recovery | **Released · Delivery complete** |

In the project owner's authorized private environment, this cycle included real Drive file organization and an accepted private document question-answering check with traceable sources. **Private documents are not included in the public repository.** [See official releases](https://github.com/SkyOneBranch/cba-kb-engine/releases).

## 5-minute quick start

You need **Python 3.11+** and **Git**. These commands are for macOS, Linux, or a compatible terminal. They use public synthetic data—**no real CBA documents or Google Drive account required**.

```bash
# Get the public code
git clone https://github.com/SkyOneBranch/cba-kb-engine.git
cd cba-kb-engine

# Use the published v2.0.4-2 snapshot
git checkout v2.0.4-2

# Create an isolated Python environment and install
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps --no-build-isolation -e .

# Check your environment and available commands
make doctor
cba-kb --help

# Try the research workflow with fictional data
unset CBA_KB_INSTANCE_ROOT CBA_KB_SOURCE_POLICY_JSON CBA_STATS_AES_KEY GOOGLE_APPLICATION_CREDENTIALS
python scripts/run_cleanroom_smoke.py --output .cleanroom/ci-smoke
```

This example does not move or publish real files. To try a local Inbox, keyword search, or Drive workflows, follow the [operations guide](docs/operations/OPERATIONS.md). The [`main` branch](https://github.com/SkyOneBranch/cba-kb-engine) may contain changes made after the published tag.

## Before using real data

- **Obtain authorization.** Access to private files and real Drive moves requires appropriate permissions. Verify the outcome of each real operation.
- **Private is not public.** This repository contains code and synthetic examples, not the project owner's private basketball documents. Permission to read private material is not permission to republish it.
- **Multi-user search has a known limitation.** The Engine's lower-level search entry points do not yet consistently enforce per-user authorization ([Issue #169](https://github.com/SkyOneBranch/cba-kb-engine/issues/169)). Until fixed, do not expose them directly as an authorization boundary to untrusted multiple users.
- **A source is not proof by itself.** AI summaries and source statements still need verification.

## What's next?

- Improve search permission isolation between users.
- Make document identities and original-source links more reliable.
- Make newly added material easier to index and query for authorized users.

These are directions for improvement, **not a committed scope or release date**. Follow [Issues](https://github.com/SkyOneBranch/cba-kb-engine/issues) for updates.

## Who is it for?

- **Basketball researchers and curators** who want to trace conclusions back to their sources.
- **Developers** who want to reuse data models, ingestion tools, and search capabilities.
- **AI application builders** who want evidence-aware answers instead of rumors presented as facts.

## Documentation and contributing

- [Operations Manual](docs/operations/OPERATIONS.md) · [中文操作手册](docs/operations/OPERATIONS.zh-CN.md)
- [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)
- [Data and licenses](DATA_LICENSE.md) · [Third-party materials](THIRD_PARTY_DATA.md)
- [Releases](https://github.com/SkyOneBranch/cba-kb-engine/releases) · [Issues](https://github.com/SkyOneBranch/cba-kb-engine/issues)

The repository provides a **Public Engine**. Real private material is operated separately in a user's **Private Instance** under applicable permissions and law. Code uses [Apache-2.0](LICENSE), docs use [CC BY 4.0](DATA_LICENSE.md), and synthetic fixtures use [CC0-1.0](DATA_LICENSE.md). Third-party reporting and photographs retain their owners' rights; see [third-party data](THIRD_PARTY_DATA.md).

**Make basketball research easier to verify—not just more confident-sounding.**
