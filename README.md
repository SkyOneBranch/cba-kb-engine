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

## v2.1.0 PR-B (in development, offline and synthetic only)

This work adds the building blocks for answering questions from
already-published, already-authorized material — without running a
live query service:

- **Query-ready slices** (`kc_slices`): find a player's records by
  exact season, competition, match type, and scope, with the full
  lookup key shown. Missing details are rejected, never guessed.
- **Passages** (`kc_passages`): find interview or document quotes
  with their source, revision, and location, and check the quote
  against the source before repeating it word for word.
- **Snapshots and manifests** (`kc_snapshot`): bundle a set of
  records into a versioned, checkable package that can be rebuilt
  exactly the same way again.
- **Current pointer (simulated)** (`kc_current`): practise the
  "which version is current right now?" check offline, including
  what happens when a version changes or permission is removed.
- **Candidate renderings** (`kc_render`): small JSON, table, and
  text formats for the material above, plus a checklist of what a
  real connector measurement would still need to prove.
- **Permission-first views** (`kc_visibility`): a reader only ever
  sees material they are authorized for; something they may not
  see looks exactly the same as something that does not exist.
- **Unified consumption entry** (`kc_consume`, Route A + directed
  repair): the only consumer-trusted surface. Raw builders are
  internal primitives; a consumer result must pass the entry's
  re-checked authorization, digest, snapshot-manifest membership,
  receipt/build binding, and (for answers) Fact binding — not caller
  flags. The session current is a validated commit plus observed
  readback, never a retained caller object. Rendering additionally
  requires the record's producer to match the requested kind, and
  passage rendering re-checks snapshot membership.

What this does **not** mean yet: these are offline implementations
tested on synthetic (made-up) examples. They are **not** verified on
the real ChatGPT Web + Google Drive client — that real-client check
stays NOT_VERIFIED until it is separately authorized and run. Nothing
here claims a P0 pass, a real Drive test pass, or a production
release.

Known limits after independent Q2 round 2 (FAIL) and the Route A
repair: round 1 closed the snapshot-ID payload binding (B1) and full
manifest digest coverage (B2); round 2 found B3–B7 only partially
closed and raised five new blockers (raw construction/citation paths
bypassing authorization, unauthorized-vs-nonexistent distinguishable
on raw slice paths, validate-to-commit not atomically bound,
evidence-chain verification not gating citation, Claim guard not
invoked by any integrated path). The Route A repair answers with one
unified entry (`kc_consume`): raw builders are internal primitives
and their outputs are not consumer-trusted; only entry-verified
outputs are. Unauthorized-vs-nonexistent uniformity holds ON THE
ENTRY surface — raw primitive error codes (for example
PLAYER_NOT_AUTHORIZED vs EMPTY) remain internal diagnostics and are
not consumer surfaces. The entry proves consistency with
session-registered state (recomputed digests, registry membership,
snapshot/receipt/build bindings); it does NOT prove real Drive
authorization, producer-source authenticity, ACL eligibility, or
upstream pagination completeness, which stay NOT_VERIFIED as above.
Remaining residual risks: upstream pagination completeness is proven
only over supplied pages; within-process session tokens are capability
records, not cryptographic authentication; and falsification evidence
for the brand-new entry partly rests on interface-delta tests
(labelled honestly in the tests) paired with behavioral
demonstrations of the raw-primitive gaps.

Independent Q2 round 3 (FAIL — the third consecutive FAIL, which
stopped the automatic patch cycle) found the Route A entry still
incomplete in five ways: session registration alone could confer
citation trust (the bound snapshot's entries did not gate
membership); catalog and passage surfaces were not uniformly
leak-free; answers trusted a caller-supplied Fact; session Current
could detach from committed state; and rendering checked digest and
session but not kind or provenance. The directed repair in this PR
answers exactly those five points on the existing call chains:
citations and passage rendering now check the snapshot-bound source
manifest; catalogs with no verifiable record and passage failures of
every cause return the uniform consumer result (diagnostics stay
internal); answers accept a Fact only when its UID, season, verified
slice, committed snapshot, and source bind to the answer; session
Current is a validated commit plus observed readback, recomputed on
every use; and rendering binds records to their producer kind with
passage membership re-checked. What the entry proves, precisely:
consistency with the committed snapshot and the session's authorized
sets under the five checks above. It does NOT prove: producer-source
authenticity beyond session-registered state; Fact field-level
authenticity beyond the UID/season/slice/snapshot/source-presence
bindings (for example team_id against a WHOLE_SEASON slice); real
Drive authorization, ACL eligibility, revocation timing, or upstream
pagination completeness; or any real-client (P0/production) behavior
— all stay NOT_VERIFIED.
