"""CBA-KB v2.1.1 T2-1 — POST_MERGE Q2 corrective tests (Human-authorized
bounded corrective PR, 2026-10-31; v2.1.1 scope retained, no v2.1.2).

Scope, fixed by the Human decision:
- B-01: a self-built association without a trusted basis (approved
  canonical reference OR a separate formal association assertion)
  must NOT be promoted to a trusted CONFLICT/OK.
- B-02: identity RESOLVED / complete candidate sets require
  completeness evidence bound to a formal identity authorization
  object — never caller self-report, page flags, or self-computed
  digests alone.
- B-03: single or identical-value claims invoking an event
  relation without sufficient relation/coverage evidence must
  NOT yield a proven-no-conflict OK; sufficient-evidence OK stays.
- Token-boundary anchor mention: EV-001 must not match EV-0012.
- Candidate inventory: entry rows must sum to the master total.
- Freeze §3.1(3) targeted verification: same key, distinct
  trusted refs must NOT be forced into one event.

Falsification labels are honest per test: BEHAVIORAL means the
call surface existed on the old HEAD and the outcome changes for
business reasons; PIN means the outcome is the same before and
after (kept behavior); NEW-CAPABILITY means the test exercises
the new formal-assertion seam (old code ignored the field).
"""

import pytest

from cba_kb.kc_cleanroom import (
    A, B, SEASON, build_cleanroom_fixture)
from cba_kb.kc_consume import (
    ConsumeSession, _content_digest, identity_evidence_digest)
from cba_kb.kc_local_producer import (
    KcLocalProducerError, _validate_identity_authorization,
    validate_identity_inventory_candidate)
from cba_kb.kc_passages import build_routed_passage, evidence_chain
from cba_kb.kc_projection import (
    canonical_json, event_association_digest,
    formal_association_assertion_digest, resolve_claims_by_event,
    sha256_hex, validate_identity_authorization)
from cba_kb.kc_snapshot import build_snapshot

PROVIDER = "CBA_OFFICIAL"
COMPETITION = "CBA"
TARGET = "fixture_target"
TIER_A = "TIER_A_OFFICIAL_MIRROR"
SRC_SHA = "0" * 64


@pytest.fixture()
def fx():
    return build_cleanroom_fixture()


# ------------------------------------------------------- Claim helpers

def _claim(cid, text, ref=None, key=None, date="2026-03-10",
           locator="P1"):
    claim = {"claim_id": cid, "player_uid": "P1", "date": date,
             "text": text, "source_id": "SRC"}
    if key is not None:
        claim["event_key"] = key
    if ref is not None:
        assoc = {"event_ref": ref, "evidence": ["SRC:" + locator],
                 "source_id": "SRC", "revision": "r1",
                 "locator": locator, "player_uid": "P1",
                 "snapshot_id": "S1", "digest": ""}
        assoc["digest"] = event_association_digest(claim, assoc)
        claim["event_association"] = assoc
    return claim


def _assert(claim):
    assoc = claim["event_association"]
    aid = "ASSERT-" + claim["claim_id"]
    claim["formal_association_assertion"] = {
        "assertion_id": aid,
        "source_id": assoc["source_id"],
        "revision": assoc["revision"],
        "locator": assoc["locator"],
        "event_ref": assoc["event_ref"],
        "claim_id": claim["claim_id"],
        "player_uid": claim["player_uid"],
        "snapshot_id": assoc.get("snapshot_id"),
        "assertion_digest": formal_association_assertion_digest(
            claim, assoc, aid)}
    return claim


def _texts(*pairs):
    return {("SRC", "r1", loc): text for loc, text in pairs}


def _anchor(claims):
    # Trust-anchor migration (Human-authorized, 2026-10-31, Q2
    # BLOCK-01): the oracle-preset anchor whose registry holds
    # the expected digests of the claims' assertions. The
    # registry is fixed before the call under test; call inputs
    # cannot add entries to it.
    registry = {}
    for c in claims:
        a = c.get("formal_association_assertion")
        if isinstance(a, dict):
            registry[a["assertion_id"]] = a["assertion_digest"]
    return {"anchor_id": "LOCAL_TEST_ORACLE", "target_domain": TARGET,
            "canonical_digest": None, "assertion_registry": registry}


def _canon_anchor(refs, approval):
    return {"anchor_id": "LOCAL_TEST_ORACLE", "target_domain": TARGET,
            "canonical_digest": sha256_hex(canonical_json(refs)),
            "assertion_registry": {},
            "release_id": approval.get("release_id"),
            "source_sha256": approval.get("source_sha256")}


def _approval(refs, tiers, release_id="REL-1", version="v1"):
    canon = sha256_hex(canonical_json(refs))
    appr = sha256_hex(canonical_json({
        "release_id": release_id, "release_version": version,
        "source_sha256": SRC_SHA, "approved_tiers": sorted(tiers),
        "canonical_digest": canon}))
    return {"release_id": release_id, "release_version": version,
            "source_sha256": SRC_SHA, "approved_tiers": list(tiers),
            "canonical_digest": canon, "approval_digest": appr}


# ------------------------------------------------------- B-01

def test_corrective_no_basis_differing_values_unknown():
    # BEHAVIORAL: content-backed + recomputed digests, but no
    # canonical params and no formal assertion. Old code:
    # CONFLICT. New code: UNKNOWN.
    claims = [_claim("c1", "甲", ref="EV1"),
              _claim("c2", "乙", ref="EV1")]
    out = resolve_claims_by_event(
        claims, passage_texts=_texts(("P1", "事件 EV1 发生。")))
    assert out["status"] == "UNKNOWN"


def test_corrective_assertion_backed_differing_conflict():
    # Second contract-allowed positive path: a separate formal
    # assertion binds each association, so CONFLICT stands.
    claims = [_assert(_claim("c1", "甲", ref="EV1")),
              _assert(_claim("c2", "乙", ref="EV1"))]
    out = resolve_claims_by_event(
        claims, passage_texts=_texts(("P1", "事件 EV1 发生。")),
        trust_anchor=_anchor(claims))
    assert out["status"] == "CONFLICT"
    assert out["conflicting_claim_ids"] == ["c1", "c2"]


def test_corrective_assertion_tampered_or_replayed_unknown():
    # Tampered digest, replay onto another claim, and a ref
    # mismatch each void the assertion -> UNKNOWN.
    base = [_assert(_claim("c1", "甲", ref="EV1")),
            _assert(_claim("c2", "乙", ref="EV1"))]
    tampered = [dict(c) for c in base]
    tampered[0] = dict(tampered[0])
    tampered[0]["formal_association_assertion"] = dict(
        tampered[0]["formal_association_assertion"])
    tampered[0]["formal_association_assertion"]["assertion_digest"] \
        = "3" * 64
    out = resolve_claims_by_event(
        tampered, passage_texts=_texts(("P1", "事件 EV1 发生。")))
    assert out["status"] == "UNKNOWN"
    replayed = [dict(c) for c in base]
    replayed[1] = dict(replayed[1])
    replayed[1]["claim_id"] = "cX"
    out = resolve_claims_by_event(
        replayed, passage_texts=_texts(("P1", "事件 EV1 发生。")))
    assert out["status"] == "UNKNOWN"
    mismatched = [dict(c) for c in base]
    mismatched[0] = dict(mismatched[0])
    mismatched[0]["formal_association_assertion"] = dict(
        mismatched[0]["formal_association_assertion"])
    mismatched[0]["formal_association_assertion"]["event_ref"] = "EV2"
    out = resolve_claims_by_event(
        mismatched, passage_texts=_texts(("P1", "事件 EV1 发生。")))
    assert out["status"] == "UNKNOWN"


def test_corrective_consumer_no_basis_claim_unknown(fx):
    # BEHAVIORAL, consumer entry: the same no-basis claims run
    # through consume_answer (bound session, registered passage
    # mentioning the ref) must NOT yield a trusted CONFLICT.
    session = ConsumeSession(
        target_domain=TARGET, authorized_players={A},
        authorized_sources={"fixture_doc_D1", "fixture_doc_D2"},
        authorized_objects={"fixture_doc_D1", "fixture_doc_D2"})
    passage = build_routed_passage(
        source_id="fixture_doc_D1", revision="rev-1", locator="P1",
        text="事件 EV1 发生。", player_uid=A, aliases=["测试甲"],
        title="fixture_doc_D1",
        authorized_url="fixture://authorized-copy/fixture_doc_D1")
    probe = ConsumeSession(
        target_domain=TARGET, authorized_players={A},
        authorized_sources={"fixture_doc_D1"},
        authorized_objects={"fixture_doc_D1"})
    probed = probe.consume_slice(
        fx["stats_records"], player_uid=A, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION)
    slice_digest = _content_digest(probed["payload"])
    snap = build_snapshot(
        entries=[{"entry_id": "entry-stats", "sha256": slice_digest,
                  "authority": "canonical", "rights": "public",
                  "source_id": "fixture_stats"},
                 {"entry_id": "entry-doc-D1",
                  "sha256": passage["digest"],
                  "authority": "canonical", "rights": "public",
                  "source_id": "fixture_doc_D1"}],
        target_domain=TARGET, capabilities=["stats"],
        build_provenance="simulated-build-1",
        build_time="2026-10-28T00:00:00+00:00",
        data_release="SIMULATED_DATA_RELEASE_1",
        engine_candidate="SIMULATED_ENGINE_CANDIDATE_1",
        build_code="SIMULATED_BUILD_CODE_1")
    session.bind_current(snap)
    session.register_passages([passage])
    binding = session.current_binding()
    chain = evidence_chain(
        receipt_id=binding["receipt_binding"],
        snapshot_id=snap.snapshot_id,
        build_version=snap.build_code,
        source_digest=passage["digest"],
        authorization_basis=binding["authorization_basis"])
    claims = []
    for cid, text in (("c1", "甲"), ("c2", "乙")):
        claim = {"claim_id": cid, "player_uid": A,
                 "date": "2026-03-10", "text": text,
                 "source_id": "fixture_doc_D1"}
        assoc = {"event_ref": "EV1", "evidence": ["fixture_doc_D1:P1"],
                 "source_id": "fixture_doc_D1", "revision": "rev-1",
                 "locator": "P1", "player_uid": A,
                 "snapshot_id": snap.snapshot_id, "digest": ""}
        assoc["digest"] = event_association_digest(claim, assoc)
        claim["event_association"] = assoc
        claims.append(claim)
    fact = {"fact_id": "ev_fact", "fact_version": "v1",
            "player_uid": A, "season": SEASON,
            "team_id": "fixture_team_T2", "kind": "REGISTRATION",
            "source": "fixture_doc_D1"}
    out = session.consume_answer(
        records=fx["stats_records"], player_uid=A, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION,
        metric="points_per_game", fact=fact,
        passage=passage, source_text=passage["text"],
        chain=chain, claims=claims)
    assert out["payload"]["claim_layer"]["status"] == "UNKNOWN"


# ------------------------------------------------------- B-02

def _auth(uids, approved=None):
    auth = {"index_id": "fixture_index", "index_version": "v1",
            "authorized_players": sorted(uids),
            "approved_aliases": approved,
            "index_digest": ""}
    normalized = (sorted(approved, key=lambda p: (p["alias"],
                                                 p["player_uid"]))
                  if approved is not None else None)
    auth["index_digest"] = sha256_hex(canonical_json({
        "index_id": "fixture_index", "index_version": "v1",
        "authorized_players": sorted(uids),
        "approved_aliases": normalized}))
    return auth


def _identity_source(players, aliases):
    return {"source_id": "fixture_canonical_registry",
            "revision": "rev-1",
            "digest": identity_evidence_digest(
                source_id="fixture_canonical_registry",
                revision="rev-1", target_domain=TARGET,
                players=players, aliases=aliases)}


def _session(**over):
    kw = dict(target_domain=TARGET, authorized_players={A},
              authorized_sources={"fixture_doc_D1"},
              authorized_objects={"fixture_doc_D1"})
    kw.update(over)
    return ConsumeSession(**kw)


def test_corrective_identity_no_authorization_unknown(fx):
    # BEHAVIORAL: full players, valid source digest, all pages
    # received — but no formal authorization object. Old code:
    # RESOLVED. New code: UNKNOWN (HOLD).
    session = _session()
    out = session.consume_identity(
        players=fx["players"], aliases=fx["aliases"],
        query="测试甲", query_kind="alias",
        identity_source=_identity_source(fx["players"],
                                         fx["aliases"]),
        all_pages_received=True)
    assert out["status"] == "UNKNOWN"
    assert out["candidates"] == []
    assert out["reason"] == "IDENTITY_COVERAGE_UNPROVEN"


def test_corrective_identity_two_uid_subset_unknown(fx):
    # BEHAVIORAL, the Q2 B-02 counterexample: the formal index
    # holds two UIDs for one name, but only one is submitted
    # with a valid digest. Old code: RESOLVED. New code:
    # UNKNOWN — a self-consistent subset is not completeness.
    session = _session()
    players = [p for p in fx["players"] if p["player_uid"] == A]
    out = session.consume_identity(
        players=players, aliases=[],
        query="林测试", query_kind="name",
        identity_source=_identity_source(players, []),
        all_pages_received=True)
    assert out["status"] == "UNKNOWN"
    assert out["candidates"] == []


def test_corrective_identity_authorized_subset_unknown(fx):
    # An authorization object covering {A, B} is NOT satisfied
    # by submitting only A, even with a valid digest.
    # Trust-anchor migration (2026-10-31, Q2 BLOCK-02, I/V
    # separation): coverage is judged within the session's
    # visible range V, so this test now scopes the session to
    # the full range {A, B}; submitting only A then fails
    # V-coverage, as before. A narrow session (V={A}) resolving
    # A alone is the separate scoped case, covered by the
    # session-scope test below.
    session = _session(authorized_players={A, B},
                       identity_authorization=_auth([A, B]))
    players = [p for p in fx["players"] if p["player_uid"] == A]
    out = session.consume_identity(
        players=players, aliases=fx["aliases"],
        query="测试甲", query_kind="alias",
        identity_source=_identity_source(players, fx["aliases"]),
        all_pages_received=True)
    assert out["status"] == "UNKNOWN"
    assert out["reason"] == "IDENTITY_COVERAGE_INCOMPLETE"


def test_corrective_identity_authorized_resolves(fx):
    session = _session(identity_authorization=_auth([A, B]))
    out = session.consume_identity(
        players=fx["players"], aliases=fx["aliases"],
        query="测试甲", query_kind="alias",
        identity_source=_identity_source(fx["players"],
                                         fx["aliases"]),
        all_pages_received=True)
    assert out["payload"]["status"] == "RESOLVED"
    assert out["payload"]["candidates"] == [A]
    assert out["payload"]["evidence_binding"]["index_id"] \
        == "fixture_index"


def test_corrective_identity_authorized_clarification(fx):
    # Trust-anchor migration (2026-10-31, Q2 BLOCK-02, I/V
    # separation): both same-name UIDs are visible in this
    # full-scope session, so clarification lists both.
    session = _session(authorized_players={A, B},
                       identity_authorization=_auth([A, B]))
    out = session.consume_identity(
        players=fx["players"], aliases=fx["aliases"],
        query="林测试", query_kind="name",
        identity_source=_identity_source(fx["players"],
                                         fx["aliases"]),
        all_pages_received=True)
    assert out["payload"]["status"] == "CLARIFICATION_REQUIRED"
    assert out["payload"]["candidates"] == sorted([A, B])


def test_corrective_identity_session_scope_not_leaked(fx):
    # REWRITTEN premise (Human trust-anchor ruling, 2026-10-31,
    # Q2 BLOCK-02, I/V separation): the previous version of
    # this test pinned wide-index filtering, which the ruling
    # reverses — disclosure must follow the session's visible
    # set V, never the wider index I. A session visible only
    # to {A} resolves the shared name within its scope only:
    # candidates [A], explicitly session-scoped, with no trace
    # of the out-of-scope UID anywhere in the result.
    session = _session(authorized_players={A},
                       identity_authorization=_auth([A, B]))
    out = session.consume_identity(
        players=fx["players"], aliases=fx["aliases"],
        query="林测试", query_kind="name",
        identity_source=_identity_source(fx["players"],
                                         fx["aliases"]),
        all_pages_received=True)
    assert out["payload"]["status"] == "RESOLVED"
    assert out["payload"]["candidates"] == [A]
    assert out["payload"]["resolution_scope"] == "SESSION_VISIBLE_SCOPE"
    assert B not in str(out)


def test_corrective_identity_forged_authorization_unknown(fx):
    auth = _auth([A, B])
    auth["index_digest"] = "4" * 64
    session = _session(identity_authorization=auth)
    out = session.consume_identity(
        players=fx["players"], aliases=fx["aliases"],
        query="测试甲", query_kind="alias",
        identity_source=_identity_source(fx["players"],
                                         fx["aliases"]),
        all_pages_received=True)
    assert out["status"] == "UNKNOWN"
    assert out["reason"] == "IDENTITY_AUTHORIZATION_INVALID"


def test_corrective_identity_authorization_validators_agree():
    # The shared validator moved to kc_projection; the producer
    # wrapper must report the same codes as build errors.
    auth = _auth([A, B])
    assert validate_identity_authorization(auth)["index_id"] \
        == "fixture_index"
    assert _validate_identity_authorization(auth)["index_id"] \
        == "fixture_index"
    forged = _auth([A, B])
    forged["index_digest"] = "4" * 64
    with pytest.raises(ValueError):
        validate_identity_authorization(forged)
    with pytest.raises(KcLocalProducerError):
        _validate_identity_authorization(forged)


# ------------------------------------------------------- B-03

def test_corrective_identical_event_key_pair_unknown():
    # BEHAVIORAL: identical values, event_key invoked, no
    # trusted association. Old code: OK. New code: UNKNOWN.
    claims = [_claim("c1", "同值", key="MATCH-7"),
              _claim("c2", "同值", key="MATCH-7")]
    out = resolve_claims_by_event(claims)
    assert out["status"] == "UNKNOWN"
    assert "EVENT_ASSOCIATION_UNPROVEN" in out["unresolved"]


def test_corrective_single_event_key_claim_unknown():
    # BEHAVIORAL: a single claim invoking an event relation
    # without proof is unproven, not a proven no-conflict OK.
    out = resolve_claims_by_event([_claim("c1", "单条",
                                          key="MATCH-7")])
    assert out["status"] == "UNKNOWN"
    assert "EVENT_ASSOCIATION_UNPROVEN" in out["unresolved"]


def test_corrective_identical_assertion_backed_ok():
    # Sufficient-evidence OK is preserved: identical values
    # under one trusted association are no conflict.
    claims = [_assert(_claim("c1", "同值", key="MATCH-7", ref="EV1")),
              _assert(_claim("c2", "同值", key="MATCH-7", ref="EV1"))]
    out = resolve_claims_by_event(
        claims, passage_texts=_texts(("P1", "事件 EV1 发生。")),
        trust_anchor=_anchor(claims))
    assert out["status"] == "OK"


def test_corrective_identical_unlabelled_ok():
    # PIN: identical values invoking no event relation keep
    # the legacy OK (no relation claimed, nothing to prove).
    claims = [_claim("c1", "同值"), _claim("c2", "同值")]
    out = resolve_claims_by_event(claims)
    assert out["status"] == "OK"


# ------------------------------------------------------- Token boundary

def test_corrective_ref_substring_no_match():
    # BEHAVIORAL: EV-001 must not match EV-0012, even with a
    # valid assertion. Old code: CONFLICT (substring).
    claims = [_assert(_claim("c1", "甲", ref="EV-001")),
              _assert(_claim("c2", "乙", ref="EV-001"))]
    out = resolve_claims_by_event(
        claims, passage_texts=_texts(("P1", "Event EV-0012 stats")))
    assert out["status"] == "UNKNOWN"


def test_corrective_ref_token_match():
    # PIN: a discrete token still counts — trailing period,
    # string edges, and a hyphen delimiter all mention the ref.
    for text in ("Event EV-001.", "EV-001", "EV-001-A event"):
        claims = [_assert(_claim("c1", "甲", ref="EV-001")),
                  _assert(_claim("c2", "乙", ref="EV-001"))]
        out = resolve_claims_by_event(
            claims, passage_texts=_texts(("P1", text)),
            trust_anchor=_anchor(claims))
        assert out["status"] == "CONFLICT", text


# ------------------------------------------------------- Candidate total

def _candidate(**over):
    cand = {
        "candidate_label": "CANDIDATE_NOT_FORMAL_INDEX",
        "candidate_version": "cand-b1-test",
        "sources": {"master_sha256": "a" * 64},
        "coverage_proof": {"master_rows": 10,
                           "per_season_counts": {"2024-2025": 6,
                                                 "2025-2026": 4},
                           "distinct_player_names": 2},
        "inventory": {
            "P_A": {"rows": 6, "seasons": {"2024-2025": 6},
                    "clubs": {"C1": 6}, "tiers": {"official_api": 6}},
            "P_B": {"rows": 4, "seasons": {"2025-2026": 4},
                    "clubs": {"C2": 4}, "tiers": {"auto_validated": 4}},
        },
    }
    cand.update(over)
    return cand


def test_corrective_candidate_entry_total_mismatch():
    # BEHAVIORAL: season sums reconcile (6+4=10) and every entry
    # is internally consistent, but entry rows sum to 9, not
    # the declared master total 10. Old code: valid.
    cand = _candidate()
    cand["inventory"]["P_A"]["rows"] = 5
    cand["inventory"]["P_A"]["seasons"] = {"2024-2025": 5}
    cand["inventory"]["P_A"]["clubs"] = {"C1": 5}
    cand["inventory"]["P_A"]["tiers"] = {"official_api": 5}
    out = validate_identity_inventory_candidate(cand)
    assert out["valid"] is False
    assert out["reason"] == "COVERAGE_ENTRY_TOTAL_MISMATCH"


# ------------------------------------------------------- §3.1(3) verification

def test_corrective_same_key_distinct_trusted_refs_ok():
    # Targeted verification: two claims share ONE event_key but
    # carry distinct canonical-approved refs with differing
    # values. Grouping is by (player_uid, event_ref), not by
    # key alone, so they are distinct events -> OK, not
    # CONFLICT. This is the canonical path's positive
    # same-key-different-events capability, proven on code.
    refs = {"EV1": {"tier": TIER_A, "confirmed": True},
            "EV2": {"tier": TIER_A, "confirmed": True}}
    approval = _approval(refs, [TIER_A])
    claims = [_claim("c1", "甲", ref="EV1", key="MATCH-7",
                     locator="P1"),
              _claim("c2", "乙", ref="EV2", key="MATCH-7",
                     locator="P2")]
    out = resolve_claims_by_event(
        claims, passage_texts=_texts(("P1", "事件 EV1 发生。"),
                                     ("P2", "事件 EV2 发生。")),
        canonical_refs=refs, canonical_approval=approval,
        trust_anchor=_canon_anchor(refs, approval))
    assert out["status"] == "OK"
    assert out["conflicting_claim_ids"] == []


def test_corrective_same_key_same_ref_conflict():
    # Control: the same key AND the same trusted ref with
    # differing values is one event in conflict.
    refs = {"EV1": {"tier": TIER_A, "confirmed": True}}
    approval = _approval(refs, [TIER_A])
    claims = [_claim("c1", "甲", ref="EV1", key="MATCH-7"),
              _claim("c2", "乙", ref="EV1", key="MATCH-7")]
    out = resolve_claims_by_event(
        claims, passage_texts=_texts(("P1", "事件 EV1 发生。")),
        canonical_refs=refs, canonical_approval=approval,
        trust_anchor=_canon_anchor(refs, approval))
    assert out["status"] == "CONFLICT"
    assert out["conflicting_claim_ids"] == ["c1", "c2"]


def test_corrective_same_key_distinct_refs_unapproved_unknown():
    # Control: distinct refs do not prove distinct events when
    # one ref lacks canonical approval -> UNKNOWN, not OK.
    refs = {"EV1": {"tier": TIER_A, "confirmed": True}}
    approval = _approval(refs, [TIER_A])
    claims = [_claim("c1", "甲", ref="EV1", key="MATCH-7",
                     locator="P1"),
              _claim("c2", "乙", ref="EV2", key="MATCH-7",
                     locator="P2")]
    out = resolve_claims_by_event(
        claims, passage_texts=_texts(("P1", "事件 EV1 发生。"),
                                     ("P2", "事件 EV2 发生。")),
        canonical_refs=refs, canonical_approval=approval)
    assert out["status"] == "UNKNOWN"
