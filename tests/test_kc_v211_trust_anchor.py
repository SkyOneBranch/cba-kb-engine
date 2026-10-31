"""Trust-anchor tests (Human ruling, 2026-10-31, Q2 BLOCK-01/BLOCK-02).

A trusted verdict needs BOTH data binding AND an authorization
source independent of the call input. The session-bound trust
anchor is preset by the trusted channel (session construction /
test oracle) and immutable to call inputs; canonical bundles and
formal assertions are trusted only when they match it, and
identity disclosure follows the session's visible set V, never
the wider index I. Every test below states which side of the
boundary its materials come from.
"""
import pytest

from cba_kb.kc_cleanroom import (
    A, B, SEASON, build_cleanroom_fixture)
from cba_kb.kc_consume import (
    ConsumeSession, _content_digest, identity_evidence_digest)
from cba_kb.kc_passages import build_routed_passage, evidence_chain
from cba_kb.kc_projection import (
    canonical_json, event_association_digest,
    formal_association_assertion_digest, resolve_claims_by_event,
    sha256_hex)
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


def _claim(cid, text, ref="EV1", key=None, locator="P1"):
    claim = {"claim_id": cid, "player_uid": A,
             "date": "2026-03-10", "text": text, "source_id": "SRC"}
    if key is not None:
        claim["event_key"] = key
    assoc = {"event_ref": ref, "evidence": ["SRC:" + locator],
             "source_id": "SRC", "revision": "r1", "locator": locator,
             "player_uid": A, "digest": ""}
    assoc["digest"] = event_association_digest(claim, assoc)
    claim["event_association"] = assoc
    return claim


def _assert(claim):
    assoc = claim["event_association"]
    aid = "ASSERT-" + claim["claim_id"]
    claim["formal_association_assertion"] = {
        "assertion_id": aid, "source_id": assoc["source_id"],
        "revision": assoc["revision"], "locator": assoc["locator"],
        "event_ref": assoc["event_ref"],
        "claim_id": claim["claim_id"], "player_uid": claim["player_uid"],
        "snapshot_id": assoc.get("snapshot_id"),
        "assertion_digest": formal_association_assertion_digest(
            claim, assoc, aid)}
    return claim


def _texts(*pairs):
    return {("SRC", "r1", loc): text for loc, text in pairs}


def _anchor(claims):
    registry = {}
    for c in claims:
        a = c.get("formal_association_assertion")
        if isinstance(a, dict):
            registry[a["assertion_id"]] = a["assertion_digest"]
    return {"anchor_id": "LOCAL_TEST_ORACLE", "target_domain": TARGET,
            "canonical_digest": None, "assertion_registry": registry}


def _approval(refs, tiers, release_id="REL-1", version="v1"):
    canon = sha256_hex(canonical_json(refs))
    appr = sha256_hex(canonical_json({
        "release_id": release_id, "release_version": version,
        "source_sha256": SRC_SHA, "approved_tiers": sorted(tiers),
        "canonical_digest": canon}))
    return {"release_id": release_id, "release_version": version,
            "source_sha256": SRC_SHA, "approved_tiers": sorted(tiers),
            "canonical_digest": canon, "approval_digest": appr}


def _canon_anchor(refs, approval):
    return {"anchor_id": "LOCAL_TEST_ORACLE", "target_domain": TARGET,
            "canonical_digest": sha256_hex(canonical_json(refs)),
            "assertion_registry": {},
            "release_id": approval.get("release_id"),
            "source_sha256": approval.get("source_sha256")}


# ------------------------------------------------------- BLOCK-01: canonical


def test_anchor_self_built_canonical_without_anchor_unknown():
    # Q2 BLOCK-01 attack, direct form: a fully self-consistent
    # canonical bundle (refs + approval + both digests
    # recomputed) with NO anchor is untrusted.
    refs = {"EV1": {"tier": TIER_A, "confirmed": True}}
    claims = [_claim("c1", "甲"), _claim("c2", "乙")]
    out = resolve_claims_by_event(
        claims, passage_texts=_texts(("P1", "事件 EV1 发生。")),
        canonical_refs=refs, canonical_approval=_approval(refs, [TIER_A]))
    assert out["status"] == "UNKNOWN"


def test_anchor_canonical_digest_mismatch_unknown():
    # The anchor binds DIFFERENT refs than the bundle presents:
    # the bundle's own digests still recompute, but the anchor
    # match fails, so the bundle is untrusted.
    refs = {"EV1": {"tier": TIER_A, "confirmed": True}}
    other = {"EV9": {"tier": TIER_A, "confirmed": True}}
    approval = _approval(refs, [TIER_A])
    claims = [_claim("c1", "甲"), _claim("c2", "乙")]
    out = resolve_claims_by_event(
        claims, passage_texts=_texts(("P1", "事件 EV1 发生。")),
        canonical_refs=refs, canonical_approval=approval,
        trust_anchor=_canon_anchor(other, _approval(other, [TIER_A])))
    assert out["status"] == "UNKNOWN"


def test_anchor_canonical_release_mismatch_unknown():
    # Same refs, but the approval claims a different release
    # than the anchor's preset release: untrusted.
    refs = {"EV1": {"tier": TIER_A, "confirmed": True}}
    approval = _approval(refs, [TIER_A], release_id="REL-OTHER")
    anchor = _canon_anchor(refs, _approval(refs, [TIER_A]))
    claims = [_claim("c1", "甲"), _claim("c2", "乙")]
    out = resolve_claims_by_event(
        claims, passage_texts=_texts(("P1", "事件 EV1 发生。")),
        canonical_refs=refs, canonical_approval=approval,
        trust_anchor=anchor)
    assert out["status"] == "UNKNOWN"


def test_anchor_canonical_bound_conflict_and_ok():
    # Positive path preserved: anchor-bound canonical refs give
    # a real CONFLICT (same ref, differing values) and a real
    # OK (distinct refs, differing values).
    refs = {"EV1": {"tier": TIER_A, "confirmed": True},
            "EV2": {"tier": TIER_A, "confirmed": True}}
    approval = _approval(refs, [TIER_A])
    anchor = _canon_anchor(refs, approval)
    claims = [_claim("c1", "甲"), _claim("c2", "乙")]
    out = resolve_claims_by_event(
        claims, passage_texts=_texts(("P1", "事件 EV1 发生。")),
        canonical_refs=refs, canonical_approval=approval,
        trust_anchor=anchor)
    assert out["status"] == "CONFLICT"
    claims = [_claim("c1", "甲", ref="EV1"),
              _claim("c2", "乙", ref="EV2", locator="P2")]
    out = resolve_claims_by_event(
        claims, passage_texts=_texts(("P1", "事件 EV1 发生。"),
                                     ("P2", "事件 EV2 发生。")),
        canonical_refs=refs, canonical_approval=approval,
        trust_anchor=anchor)
    assert out["status"] == "OK"


def test_anchor_malformed_anchor_unknown():
    # A structurally malformed anchor (bad digest shape) is
    # not a trust root: canonical claims stay UNKNOWN.
    refs = {"EV1": {"tier": TIER_A, "confirmed": True}}
    approval = _approval(refs, [TIER_A])
    anchor = {"anchor_id": "LOCAL_TEST_ORACLE", "target_domain": TARGET,
              "canonical_digest": "not-a-digest",
              "assertion_registry": {}}
    claims = [_claim("c1", "甲"), _claim("c2", "乙")]
    out = resolve_claims_by_event(
        claims, passage_texts=_texts(("P1", "事件 EV1 发生。")),
        canonical_refs=refs, canonical_approval=approval,
        trust_anchor=anchor)
    assert out["status"] == "UNKNOWN"


# ------------------------------------------------------- BLOCK-01: assertions


def test_anchor_self_built_assertion_without_anchor_unknown():
    # Q2 BLOCK-01 attack, direct form: self-built associations
    # + self-built assertions + recomputed digests, no anchor.
    claims = [_assert(_claim("c1", "甲")), _assert(_claim("c2", "乙"))]
    out = resolve_claims_by_event(
        claims, passage_texts=_texts(("P1", "事件 EV1 发生。")))
    assert out["status"] == "UNKNOWN"


def test_anchor_registered_assertion_conflict():
    # Positive path preserved, NOT disabled for a surface PASS:
    # assertions registered in the oracle-preset anchor still
    # yield a real CONFLICT.
    claims = [_assert(_claim("c1", "甲")), _assert(_claim("c2", "乙"))]
    out = resolve_claims_by_event(
        claims, passage_texts=_texts(("P1", "事件 EV1 发生。")),
        trust_anchor=_anchor(claims))
    assert out["status"] == "CONFLICT"
    assert out["conflicting_claim_ids"] == ["c1", "c2"]


def test_anchor_recomputed_assertion_id_reuse_unknown():
    # The sharpest BLOCK-01 case: the attacker reuses a
    # REGISTERED assertion_id but recomputes the digest over a
    # different claim. Field binding alone would pass for a
    # freshly built claim; the registry's expected digest for
    # that id does not match, so the claim is untrusted.
    base = [_assert(_claim("c1", "甲")), _assert(_claim("c2", "乙"))]
    anchor = _anchor(base)
    forged = _assert(_claim("c1", "甲"))
    forged["claim_id"] = "cX"
    forged["formal_association_assertion"]["claim_id"] = "cX"
    aid = forged["formal_association_assertion"]["assertion_id"]
    forged["formal_association_assertion"]["assertion_digest"] = \
        formal_association_assertion_digest(
            forged, forged["event_association"], aid)
    # Registry still holds the ORIGINAL claim's digest for a
    # different assertion id; give the forged claim the
    # registered id to simulate id reuse.
    registered_id = base[0]["formal_association_assertion"]["assertion_id"]
    forged["formal_association_assertion"]["assertion_id"] = registered_id
    forged["formal_association_assertion"]["assertion_digest"] = \
        formal_association_assertion_digest(
            forged, forged["event_association"], registered_id)
    out = resolve_claims_by_event(
        [forged, base[1]], passage_texts=_texts(("P1", "事件 EV1 发生。")),
        trust_anchor=anchor)
    assert out["status"] == "UNKNOWN"


def test_anchor_registered_assertion_bidirectional():
    # Bidirectional §3.1(3) shape on the assertion path: same
    # key + distinct trusted refs -> OK; same key + same trusted
    # ref + differing values -> CONFLICT.
    claims = [_assert(_claim("c1", "甲", ref="EV1", key="MATCH-7")),
              _assert(_claim("c2", "乙", ref="EV2", key="MATCH-7",
                             locator="P2"))]
    out = resolve_claims_by_event(
        claims, passage_texts=_texts(("P1", "事件 EV1 发生。"),
                                     ("P2", "事件 EV2 发生。")),
        trust_anchor=_anchor(claims))
    assert out["status"] == "OK"
    claims = [_assert(_claim("c1", "甲", ref="EV1", key="MATCH-7")),
              _assert(_claim("c2", "乙", ref="EV1", key="MATCH-7"))]
    out = resolve_claims_by_event(
        claims, passage_texts=_texts(("P1", "事件 EV1 发生。")),
        trust_anchor=_anchor(claims))
    assert out["status"] == "CONFLICT"


# ------------------------------------------------------- BLOCK-01: session path


def _build_snapshot(fx, passage):
    # Snapshot construction needs no session object - the probe
    # only computes the justifying slice digest.
    probe = ConsumeSession(
        target_domain=TARGET, authorized_players={A},
        authorized_sources={"fixture_doc_D1"},
        authorized_objects={"fixture_doc_D1"})
    probed = probe.consume_slice(
        fx["stats_records"], player_uid=A, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION)
    slice_digest = _content_digest(probed["payload"])
    return build_snapshot(
        entries=[{"entry_id": "entry-stats", "sha256": slice_digest,
                  "authority": "canonical", "rights": "public",
                  "source_id": "fixture_stats"},
                 {"entry_id": "entry-doc-D1", "sha256": passage["digest"],
                  "authority": "canonical", "rights": "public",
                  "source_id": "fixture_doc_D1"}],
        target_domain=TARGET, capabilities=["stats"],
        build_provenance="simulated-build-1",
        build_time="2026-10-28T00:00:00+00:00",
        data_release="SIMULATED_DATA_RELEASE_1",
        engine_candidate="SIMULATED_ENGINE_CANDIDATE_1",
        build_code="SIMULATED_BUILD_CODE_1")


def _bind(session, snap, passage):
    session.bind_current(snap)
    session.register_passages([passage])
    binding = session.current_binding()
    chain = evidence_chain(
        receipt_id=binding["receipt_binding"],
        snapshot_id=snap.snapshot_id,
        build_version=snap.build_code,
        source_digest=passage["digest"],
        authorization_basis=binding["authorization_basis"])
    return chain


def _new_session(anchor):
    return ConsumeSession(
        target_domain=TARGET, authorized_players={A},
        authorized_sources={"fixture_doc_D1"},
        authorized_objects={"fixture_doc_D1"},
        trust_anchor=anchor)


def _answer(session, fx, passage, chain, claims):
    fact = {"fact_id": "anchor_fact", "player_uid": A, "season": SEASON,
            "team_id": "fixture_team_T2", "kind": "REGISTRATION",
            "source": "fixture_doc_D1"}
    return session.consume_answer(
        records=fx["stats_records"], player_uid=A, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION,
        metric="points_per_game", fact=fact,
        passage=passage, source_text=passage["text"],
        chain=chain, claims=claims)


def _session_claim(cid, snap, text):
    claim = {"claim_id": cid, "player_uid": A,
             "date": "2026-03-10", "text": text,
             "source_id": "fixture_doc_D1"}
    assoc = {"event_ref": "event_Q", "evidence": ["fixture_doc_D1:P3"],
             "source_id": "fixture_doc_D1", "revision": "rev-1",
             "locator": "P3", "player_uid": A,
             "snapshot_id": snap.snapshot_id, "digest": ""}
    assoc["digest"] = event_association_digest(claim, assoc)
    claim["event_association"] = assoc
    return _assert(claim)


def test_anchor_session_uses_only_session_anchor(fx):
    # The session path passes ONLY the session's own anchor to
    # the resolver. Claims carrying self-built assertions, in a
    # session with NO anchor, stay UNKNOWN; the same claims in
    # a session whose anchor registers them yield CONFLICT.
    # A claim cannot smuggle its own anchor through the call.
    passage = build_routed_passage(
        source_id="fixture_doc_D1", revision="rev-1", locator="P3",
        text=fx["passages"][0]["text"] + " 事件 event_Q。",
        player_uid=A, aliases=["测试甲"], title="fixture_doc_D1",
        authorized_url="fixture://authorized-copy/fixture_doc_D1")
    # No-anchor session - self-built assertions stay UNKNOWN.
    snap = _build_snapshot(fx, passage)
    session = _new_session(None)
    chain = _bind(session, snap, passage)
    claims = [_session_claim("c1", snap, "事件发生在 2026-03-10"),
              _session_claim("c2", snap, "事件发生在 2026-03-15")]
    layer = _answer(session, fx, passage, chain, claims
                    )["payload"]["claim_layer"]
    assert layer["status"] == "UNKNOWN"
    # Anchored session - the oracle presets the registry for
    # these claims before the call; the same claims yield
    # CONFLICT. (Snapshot rebuilt so the claims' snapshot
    # binding matches the bound snapshot.)
    snap = _build_snapshot(fx, passage)
    claims = [_session_claim("c1", snap, "事件发生在 2026-03-10"),
              _session_claim("c2", snap, "事件发生在 2026-03-15")]
    session = _new_session(_anchor(claims))
    chain = _bind(session, snap, passage)
    layer = _answer(session, fx, passage, chain, claims
                    )["payload"]["claim_layer"]
    assert layer["status"] == "CONFLICT"


def test_anchor_session_target_mismatch_invalid(fx):
    # An anchor bound to another target domain is invalid at
    # session construction: canonical claims through it stay
    # UNKNOWN, not trusted on the bundle alone.
    passage = build_routed_passage(
        source_id="fixture_doc_D1", revision="rev-1", locator="P3",
        text=fx["passages"][0]["text"] + " 事件 EV1。",
        player_uid=A, aliases=["测试甲"], title="fixture_doc_D1",
        authorized_url="fixture://authorized-copy/fixture_doc_D1")
    refs = {"EV1": {"tier": TIER_A, "confirmed": True}}
    approval = _approval(refs, [TIER_A])
    anchor = _canon_anchor(refs, approval)
    anchor["target_domain"] = "OTHER_TARGET"
    session = ConsumeSession(
        target_domain=TARGET, authorized_players={A},
        authorized_sources={"fixture_doc_D1"},
        authorized_objects={"fixture_doc_D1"},
        canonical_event_refs=refs, canonical_approval=approval,
        trust_anchor=anchor)
    assert session._trust_anchor is None
    assert session._trust_anchor_invalid is True


# ------------------------------------------------------- BLOCK-02: I/V


def _auth(uids, aliases=None):
    body = {"index_id": "fixture_index", "index_version": "v1",
            "authorized_players": sorted(uids),
            "approved_aliases": aliases, "index_digest": ""}
    normalized = None
    if aliases is not None:
        normalized = sorted(
            aliases, key=lambda p: (p["alias"], p["player_uid"]))
    body["approved_aliases"] = normalized
    body["index_digest"] = sha256_hex(canonical_json({
        "index_id": "fixture_index", "index_version": "v1",
        "authorized_players": sorted(uids),
        "approved_aliases": normalized}))
    return body


def _session(**over):
    kw = dict(target_domain=TARGET, authorized_players={A},
              authorized_sources={"fixture_doc_D1"},
              authorized_objects={"fixture_doc_D1"})
    kw.update(over)
    return ConsumeSession(**kw)


def _identity_source(players, aliases):
    return {"source_id": "fixture_canonical_registry", "revision": "rev-1",
            "digest": identity_evidence_digest(
                source_id="fixture_canonical_registry",
                revision="rev-1", target_domain=TARGET,
                players=players, aliases=aliases)}


def _identify(session, players, aliases, query, kind):
    return session.consume_identity(
        players=players, aliases=aliases, query=query,
        query_kind=kind, identity_source=_identity_source(players, aliases),
        all_pages_received=True)


def test_anchor_identity_visible_subset_of_index_mismatch(fx):
    # V reaching outside I is a session misconfiguration:
    # uniform UNKNOWN, never a wider disclosure license.
    session = _session(authorized_players={B},
                       identity_authorization=_auth([A]))
    out = _identify(session, fx["players"], fx["aliases"],
                    "测试甲", "alias")
    assert out["status"] == "UNKNOWN"
    assert out["reason"] == "IDENTITY_AUTHORIZATION_SCOPE_MISMATCH"


def test_anchor_identity_narrow_session_scoped_resolve(fx):
    # Ruling's scoped case: V={A} within I={A, B}, full players
    # submitted, shared name queried — resolves within scope
    # only, explicitly marked, with no trace of B.
    session = _session(authorized_players={A},
                       identity_authorization=_auth([A, B]))
    out = _identify(session, fx["players"], fx["aliases"],
                    "林测试", "name")
    assert out["payload"]["status"] == "RESOLVED"
    assert out["payload"]["candidates"] == [A]
    assert out["payload"]["resolution_scope"] == "SESSION_VISIBLE_SCOPE"
    assert B not in str(out)


def test_anchor_identity_narrow_session_visible_only_submit(fx):
    # Same scoped session, submitting only the visible player:
    # coverage within V holds, so scoped RESOLVED (the old
    # I-coverage rule refused this; the ruling's scoped clause
    # allows it, marked as scoped).
    players = [p for p in fx["players"] if p["player_uid"] == A]
    session = _session(authorized_players={A},
                       identity_authorization=_auth([A, B]))
    out = _identify(session, players, fx["aliases"],
                    "测试甲", "alias")
    assert out["payload"]["status"] == "RESOLVED"
    assert out["payload"]["resolution_scope"] == "SESSION_VISIBLE_SCOPE"


def test_anchor_identity_full_scope_clarification(fx):
    # Both same-name UIDs visible: clarification lists both.
    session = _session(authorized_players={A, B},
                       identity_authorization=_auth([A, B]))
    out = _identify(session, fx["players"], fx["aliases"],
                    "林测试", "name")
    assert out["payload"]["status"] == "CLARIFICATION_REQUIRED"
    assert out["payload"]["candidates"] == sorted([A, B])
    assert out["payload"]["resolution_scope"] == "AUTHORIZED_INDEX_SCOPE"


def test_anchor_identity_full_scope_subset_unknown(fx):
    # Full-scope session, subset submitted: V-coverage fails.
    players = [p for p in fx["players"] if p["player_uid"] == A]
    session = _session(authorized_players={A, B},
                       identity_authorization=_auth([A, B]))
    out = _identify(session, players, fx["aliases"],
                    "测试甲", "alias")
    assert out["status"] == "UNKNOWN"
    assert out["reason"] == "IDENTITY_COVERAGE_INCOMPLETE"


def test_anchor_identity_out_of_scope_alias_filtered(fx):
    # An alias pointing at an out-of-scope UID is filtered with
    # the players: querying it yields UNKNOWN, never B.
    aliases = [{"alias": "测试甲", "player_uid": A},
               {"alias": "测试乙", "player_uid": B}]
    session = _session(authorized_players={A},
                       identity_authorization=_auth(
                           [A, B],
                           aliases=[{"alias": "测试甲", "player_uid": A},
                                    {"alias": "测试乙", "player_uid": B}]))
    out = _identify(session, fx["players"], aliases,
                    "测试乙", "alias")
    assert out["status"] == "UNKNOWN"
    assert B not in str(out)


def test_anchor_identity_f02_uniform(fx):
    # F-02: an out-of-scope existing alias and a nonexistent
    # name return the IDENTICAL uniform UNKNOWN shape — the
    # caller cannot tell whether the name exists elsewhere.
    aliases = [{"alias": "测试甲", "player_uid": A},
               {"alias": "测试乙", "player_uid": B}]
    session = _session(authorized_players={A},
                       identity_authorization=_auth(
                           [A, B],
                           aliases=[{"alias": "测试甲", "player_uid": A},
                                    {"alias": "测试乙", "player_uid": B}]))
    out_unauth = _identify(session, fx["players"], aliases,
                           "测试乙", "alias")
    out_missing = _identify(session, fx["players"], aliases,
                            "查无此人", "alias")
    assert out_unauth == out_missing
    assert out_unauth["status"] == "UNKNOWN"
    assert out_unauth["candidates"] == []
    assert out_unauth["relation"] == "UNDECIDED"
