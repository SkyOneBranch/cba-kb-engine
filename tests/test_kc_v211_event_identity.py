"""CBA-KB v2.1.1 T2-1 — Q2 H01/H02/H03 repair tests (Human-authorized,
2026-10-31). Two minimal groups, no new architecture:

Group A (Claim event identity, consumer path): a trusted
association now requires the anchor passage's TEXT to mention
the claimed event_ref, and every evidence entry to resolve to
a registered passage. Anchor membership + a recomputed digest
alone no longer prove event identity.

Group B (Identity formal candidate completeness, producer
path): the producer no longer derives identity authorization
from the players being resolved. A separate formal identity
authorization object is the baseline; without one, identity
stays UNKNOWN (HOLD) — never a trusted RESOLVED from a
players-derived session.

Falsification labels are honest per test: BEHAVIORAL means
the test's call surface existed on the old HEAD and the
outcome changes for business reasons; NEW-CAPABILITY means
the test exercises a seam the old code did not have.
"""

import pytest

from cba_kb.kc_cleanroom import A, SEASON, build_cleanroom_fixture
from cba_kb.kc_consume import ConsumeSession, _content_digest
from cba_kb.kc_local_producer import (
    KcLocalProducerError, build_local_projection,
    verify_local_projection)
from cba_kb.kc_passages import build_routed_passage, evidence_chain
from cba_kb.kc_projection import (
    canonical_json, event_association_digest, sha256_hex)
from cba_kb.kc_snapshot import build_snapshot

PROVIDER = "CBA_OFFICIAL"
COMPETITION = "CBA"
TARGET = "fixture_target"
U1 = "fixture_player_0001"
U2 = "fixture_player_0002"


@pytest.fixture()
def fx():
    return build_cleanroom_fixture()


# ------------------------------------------------------- Group A helpers

def _session(**over):
    kw = dict(target_domain=TARGET,
              authorized_players={A},
              authorized_sources={"fixture_doc_D1", "fixture_doc_D2"},
              authorized_objects={"fixture_doc_D1", "fixture_doc_D2"})
    kw.update(over)
    return ConsumeSession(**kw)


def _bound_session(fx, passages, register=True):
    session = _session()
    probe = _session()
    probed = probe.consume_slice(
        fx["stats_records"], player_uid=A, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION)
    slice_digest = _content_digest(probed["payload"])
    entries = [{"entry_id": "entry-stats", "sha256": slice_digest,
                "authority": "canonical", "rights": "public",
                "source_id": "fixture_stats"}]
    for passage in passages:
        entries.append({"entry_id": "entry-" + passage["source_id"],
                        "sha256": passage["digest"],
                        "authority": "canonical", "rights": "public",
                        "source_id": passage["source_id"]})
    snap = build_snapshot(
        entries=entries, target_domain=TARGET, capabilities=["stats"],
        build_provenance="simulated-build-1",
        build_time="2026-10-28T00:00:00+00:00",
        data_release="SIMULATED_DATA_RELEASE_1",
        engine_candidate="SIMULATED_ENGINE_CANDIDATE_1",
        build_code="SIMULATED_BUILD_CODE_1")
    session.bind_current(snap)
    if register:
        session.register_passages(passages)
    binding = session.current_binding()
    chain = evidence_chain(
        receipt_id=binding["receipt_binding"],
        snapshot_id=snap.snapshot_id,
        build_version=snap.build_code,
        source_digest=passages[0]["digest"],
        authorization_basis=binding["authorization_basis"])
    return session, snap, chain


def _passage(source_id, revision, locator, text):
    return build_routed_passage(
        source_id=source_id, revision=revision, locator=locator,
        text=text, player_uid=A, aliases=["测试甲"],
        title=source_id,
        authorized_url="fixture://authorized-copy/" + source_id)


def _claim(snap, cid, text, ref, src, loc, evidence):
    claim = {"claim_id": cid, "player_uid": A,
             "date": "2026-03-10", "text": text, "source_id": src}
    assoc = {"event_ref": ref, "evidence": evidence,
             "source_id": src, "revision": "rev-1",
             "locator": loc, "player_uid": A,
             "snapshot_id": snap.snapshot_id, "digest": ""}
    assoc["digest"] = event_association_digest(claim, assoc)
    claim["event_association"] = assoc
    return claim


def _answer(session, fx, passage, chain, claims):
    fact = {"fact_id": "ev_fact", "fact_version": "v1",
            "player_uid": A, "season": SEASON,
            "team_id": "fixture_team_T2", "kind": "REGISTRATION",
            "source": "fixture_doc_D1"}
    return session.consume_answer(
        records=fx["stats_records"], player_uid=A, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION,
        metric="points_per_game", fact=fact,
        passage=passage, source_text=passage["text"],
        chain=chain, claims=claims)


def _layer(out):
    return out["payload"]["claim_layer"]


# ------------------------------------------------------- Group A: Claim

def test_event_identity_ordinary_passage_same_ref_unknown(fx):
    # COUNTEREXAMPLE H01 (BEHAVIORAL): the anchor passage is
    # registered and digest-valid, its text is ordinary info
    # that never states event_Q — yet two caller-built claims
    # share event_Q with recomputed digests. The old code
    # returned CONFLICT from triple membership + digest alone.
    # The passage content does not support the event, so the
    # association is untrusted and the result is UNKNOWN.
    passage = _passage("fixture_doc_D1", "rev-1", "P3",
                       fx["passages"][0]["text"])
    session, snap, chain = _bound_session(fx, [passage])
    claims = [
        _claim(snap, "c1", "事件发生在 2026-03-10", "event_Q",
               "fixture_doc_D1", "P3", ["fixture_doc_D1:P3"]),
        _claim(snap, "c2", "事件发生在 2026-03-15", "event_Q",
               "fixture_doc_D1", "P3", ["fixture_doc_D1:P3"])]
    layer = _layer(_answer(session, fx, passage, chain, claims))
    assert layer["status"] == "UNKNOWN"
    assert layer["conflicting_claim_ids"] == []


def test_event_identity_content_backed_same_ref_conflict(fx):
    # PIN (positive): the same shape, but the anchor passage's
    # text states the claimed event — a content-backed
    # association. Same event, differing values: CONFLICT.
    passage = _passage("fixture_doc_D1", "rev-1", "P3",
                       fx["passages"][0]["text"] + " 事件 event_Q。")
    session, snap, chain = _bound_session(fx, [passage])
    claims = [
        _claim(snap, "c1", "事件发生在 2026-03-10", "event_Q",
               "fixture_doc_D1", "P3", ["fixture_doc_D1:P3"]),
        _claim(snap, "c2", "事件发生在 2026-03-15", "event_Q",
               "fixture_doc_D1", "P3", ["fixture_doc_D1:P3"])]
    layer = _layer(_answer(session, fx, passage, chain, claims))
    assert layer["status"] == "CONFLICT"
    assert layer["conflicting_claim_ids"] == ["c1", "c2"]


def test_event_identity_distinct_refs_unmentioned_unknown(fx):
    # COUNTEREXAMPLE H02 (BEHAVIORAL): the Q2 counterexample —
    # one game described under two event numbers with
    # different locators. Neither passage's text states its
    # claimed event. The old code returned OK from string
    # inequality + disjoint evidence. Different strings are
    # not proof of different events: UNKNOWN.
    passage = _passage("fixture_doc_D1", "rev-1", "P3",
                       fx["passages"][0]["text"])
    passage2 = _passage("fixture_doc_D2", "rev-1", "P9",
                        "第二来源文本。")
    session, snap, chain = _bound_session(fx, [passage, passage2])
    claims = [
        _claim(snap, "c1", "事件发生在 2026-03-10", "game_01",
               "fixture_doc_D1", "P3", ["fixture_doc_D1:P3"]),
        _claim(snap, "c2", "事件发生在 2026-03-15", "game_02",
               "fixture_doc_D2", "P9", ["fixture_doc_D2:P9"])]
    layer = _layer(_answer(session, fx, passage, chain, claims))
    assert layer["status"] == "UNKNOWN"
    assert layer["conflicting_claim_ids"] == []


def test_event_identity_content_backed_distinct_refs_ok(fx):
    # PIN (positive): each claim anchors to its own
    # registered passage whose text states its own event_ref,
    # with disjoint resolving evidence. Distinctness is proven
    # by distinct content-backed associations, not by string
    # inequality: OK.
    passage = _passage("fixture_doc_D1", "rev-1", "P3",
                       fx["passages"][0]["text"] + " 事件 game_01。")
    passage2 = _passage("fixture_doc_D2", "rev-1", "P9",
                        "第二来源文本。事件 game_02。")
    session, snap, chain = _bound_session(fx, [passage, passage2])
    claims = [
        _claim(snap, "c1", "事件发生在 2026-03-10", "game_01",
               "fixture_doc_D1", "P3", ["fixture_doc_D1:P3"]),
        _claim(snap, "c2", "事件发生在 2026-03-15", "game_02",
               "fixture_doc_D2", "P9", ["fixture_doc_D2:P9"])]
    layer = _layer(_answer(session, fx, passage, chain, claims))
    assert layer["status"] == "OK"
    assert layer["conflicting_claim_ids"] == []


def test_event_identity_unresolvable_evidence_unknown(fx):
    # COUNTEREXAMPLE (BEHAVIORAL): the anchor passage states
    # the event, but one claim's evidence points at a locator
    # that was never registered. Arbitrary evidence strings
    # prove nothing, however well the digest recomputes.
    # Old code: CONFLICT.
    passage = _passage("fixture_doc_D1", "rev-1", "P3",
                       fx["passages"][0]["text"] + " 事件 event_Q。")
    session, snap, chain = _bound_session(fx, [passage])
    claims = [
        _claim(snap, "c1", "事件发生在 2026-03-10", "event_Q",
               "fixture_doc_D1", "P3", ["fixture_doc_D1:P3"]),
        _claim(snap, "c2", "事件发生在 2026-03-15", "event_Q",
               "fixture_doc_D1", "P3", ["fixture_doc_D1:P9"])]
    layer = _layer(_answer(session, fx, passage, chain, claims))
    assert layer["status"] == "UNKNOWN"
    assert layer["conflicting_claim_ids"] == []


# ------------------------------------------------------- Group B helpers

def _auth(uids, approved=None, index_id="formal-index-1",
          version="v1"):
    # Digest over the SAME normalized form the producer
    # validates: sorted player list, approved aliases as
    # dicts sorted by (alias, player_uid) (or None).
    normalized = None
    if approved is not None:
        normalized = sorted(
            [{"alias": a["alias"], "player_uid": a["player_uid"]}
             for a in approved],
            key=lambda p: (p["alias"], p["player_uid"]))
    body = {"index_id": index_id, "index_version": version,
            "authorized_players": sorted(uids),
            "approved_aliases": normalized}
    body["index_digest"] = sha256_hex(canonical_json(body))
    return body


def _producer_fixture(fx, players=None, aliases=None):
    return {"players": players if players is not None else fx["players"],
            "aliases": aliases if aliases is not None else fx["aliases"],
            "stats_records": fx["stats_records"],
            "fact": fx["fact"],
            "claims": [], "passages": fx["passages"],
            "source_objects": []}


# ------------------------------------------------------- Group B: Identity

def test_identity_no_authorization_object_holds(fx, tmp_path):
    # COUNTEREXAMPLE H03 (BEHAVIORAL): no separate formal
    # authorization object is supplied. The old producer
    # derived authorized_players from the players being
    # resolved and emitted a trusted RESOLVED. Now identity
    # stays UNKNOWN (HOLD); the build itself still verifies.
    out = build_local_projection(
        fixture=_producer_fixture(fx), output_dir=str(tmp_path))
    assert verify_local_projection(str(tmp_path))["valid"] is True
    assert out["identity"]["status"] == "UNKNOWN"
    assert out["formal_identity_authorization"] is None


def test_identity_formal_index_resolves(fx, tmp_path):
    # NEW-CAPABILITY (positive): a separate formal index
    # covering both fixture players, with the approved alias,
    # yields the genuine RESOLVED — the capability is kept,
    # the self-derived baseline is not. (Old HEAD has no
    # identity_authorization seam, so this fails at the call
    # boundary there — not claimed as business falsification.)
    auth = _auth([U1, U2],
                 approved=[{"alias": "测试甲", "player_uid": U1}])
    out = build_local_projection(
        fixture=_producer_fixture(fx), output_dir=str(tmp_path),
        identity_authorization=auth)
    assert verify_local_projection(str(tmp_path))["valid"] is True
    assert out["identity"]["status"] == "RESOLVED"
    assert out["identity"]["candidates"] == [U1]
    assert out["formal_identity_authorization"]["index_id"] == \
        "formal-index-1"


def test_identity_formal_index_subset_input_unknown(fx, tmp_path):
    # COUNTEREXAMPLE H03 (BEHAVIORAL): the formal index holds
    # two UIDs for one name, but the input carries only one.
    # The old producer derived its baseline from the input
    # and resolved. Coverage against the SEPARATE index fails,
    # so identity stays UNKNOWN — it must not fake a unique
    # identity from a partial submission.
    u1 = [p for p in fx["players"] if p["player_uid"] == U1]
    auth = _auth([U1, U2],
                 approved=[{"alias": "测试甲", "player_uid": U1}])
    out = build_local_projection(
        fixture=_producer_fixture(fx, players=u1),
        output_dir=str(tmp_path), identity_authorization=auth)
    assert verify_local_projection(str(tmp_path))["valid"] is True
    assert out["identity"]["status"] == "UNKNOWN"


def test_identity_formal_index_clarification(fx, tmp_path):
    # NEW-CAPABILITY (positive): the same alias approved for
    # two UIDs under the formal index must clarify, never pick
    # one. (New seam — see the resolves test's note.)
    aliases = [{"alias": "测试甲", "player_uid": U1, "approved": True},
               {"alias": "测试甲", "player_uid": U2, "approved": True}]
    auth = _auth([U1, U2],
                 approved=[{"alias": "测试甲", "player_uid": U1},
                           {"alias": "测试甲", "player_uid": U2}])
    out = build_local_projection(
        fixture=_producer_fixture(fx, aliases=aliases),
        output_dir=str(tmp_path), identity_authorization=auth)
    assert out["identity"]["status"] == "CLARIFICATION_REQUIRED"
    assert sorted(out["identity"]["candidates"]) == [U1, U2]


def test_identity_unauthorized_player_no_leak(fx, tmp_path):
    # NEW-CAPABILITY (positive, F-02): a player outside the
    # formal index is filtered before resolution — an
    # approved-looking alias for an unauthorized UID resolves
    # to UNKNOWN, never to a candidate, a count, or a leak of
    # existence. (New seam — see the resolves test's note.)
    u3 = {"player_uid": "fixture_player_0003",
          "canonical_name": "林测试丙"}
    aliases = [{"alias": "测试丙", "player_uid": "fixture_player_0003",
                "approved": True}]
    auth = _auth([U1, U2])
    out = build_local_projection(
        fixture=_producer_fixture(
            fx, players=list(fx["players"]) + [u3], aliases=aliases),
        output_dir=str(tmp_path), identity_authorization=auth)
    assert out["identity"]["status"] == "UNKNOWN"
    assert out["identity"]["candidates"] == []


def test_identity_forged_index_digest_raises(fx, tmp_path):
    # NEW-CAPABILITY (fail-closed): an authorization object
    # whose index_digest does not recompute over its claimed
    # index is a build failure, never a trusted baseline.
    auth = _auth([U1, U2])
    auth["index_digest"] = "0" * 64
    with pytest.raises(KcLocalProducerError):
        build_local_projection(
            fixture=_producer_fixture(fx), output_dir=str(tmp_path),
            identity_authorization=auth)
