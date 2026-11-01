"""Release-ruling tests (Human ruling, 2026-11-01).

The ruling reclassifies the trust-anchor Q2's BLOCK-01/02/03 instead of
opening another unbounded repair round:

- BLOCK-02 (V outside I leaking a specific reason) is a confirmed
  CODE_BLOCKER: the external result must be generic, the specific cause
  internal-only.
- BLOCK-03's anchor/registry mutation is a confirmed CODE_BLOCKER: the
  session-stored anchor is frozen at construction.
- BLOCK-01's direct-resolver forged anchor and the authorized-set
  mutation are LOCAL_SCOPE_LIMITATIONs with per-call guards: no product
  path bypasses the trusted consumer, and this file proves it with
  code/tests rather than comments alone (call-graph test, direct-vs-
  session divergence test, signature test, widening-guard test).
- The authorized sets stay owner-mutable on purpose: the revocation
  feature (render revalidates CURRENT authorization) depends on
  discard(); widening is re-checked per call by the V-subset-I rule.

Positive paths are preserved, not disabled: the frozen-anchor session
still yields CONFLICT for genuinely registered assertions.
"""

import inspect
import json
from pathlib import Path

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


@pytest.fixture()
def fx():
    return build_cleanroom_fixture()


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


def _anchor(claims):
    registry = {}
    for c in claims:
        a = c.get("formal_association_assertion")
        if isinstance(a, dict):
            registry[a["assertion_id"]] = a["assertion_digest"]
    return {"anchor_id": "LOCAL_TEST_ORACLE", "target_domain": TARGET,
            "canonical_digest": None, "assertion_registry": registry}


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


def _build_snapshot(fx, passage):
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
    return evidence_chain(
        receipt_id=binding["receipt_binding"],
        snapshot_id=snap.snapshot_id,
        build_version=snap.build_code,
        source_digest=passage["digest"],
        authorization_basis=binding["authorization_basis"])


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


def _answer(session, fx, passage, chain, claims):
    fact = {"fact_id": "ruling_fact", "player_uid": A, "season": SEASON,
            "team_id": "fixture_team_T2", "kind": "REGISTRATION",
            "source": "fixture_doc_D1"}
    return session.consume_answer(
        records=fx["stats_records"], player_uid=A, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION,
        metric="points_per_game", fact=fact,
        passage=passage, source_text=passage["text"],
        chain=chain, claims=claims)


def _passage(fx):
    return build_routed_passage(
        source_id="fixture_doc_D1", revision="rev-1", locator="P3",
        text=fx["passages"][0]["text"] + " 事件 event_Q。",
        player_uid=A, aliases=["测试甲"], title="fixture_doc_D1",
        authorized_url="fixture://authorized-copy/fixture_doc_D1")


# ------------------------------------------------------- BLOCK-02: reason internalization


def test_ruling_v_mismatch_external_generic_internal_specific(fx):
    # V reaching outside I: the external result is the generic uniform
    # reason, and the specific configuration cause appears ONLY in the
    # session's internal diagnostic (F-02: no observable error class
    # that distinguishes this configuration failure).
    session = _session(authorized_players={B},
                       identity_authorization=_auth([A]))
    out = _identify(session, fx["players"], fx["aliases"],
                    "测试甲", "alias")
    assert out["status"] == "UNKNOWN"
    assert out["reason"] == "IDENTITY_COVERAGE_UNPROVEN"
    assert "SCOPE_MISMATCH" not in json.dumps(out, ensure_ascii=False)
    assert session._last_diagnostic["cause"] == \
        "IDENTITY_AUTHORIZATION_SCOPE_MISMATCH"
    assert session._last_diagnostic["entry"] == "consume_identity"


# ------------------------------------------------------- BLOCK-03: frozen anchor


def test_ruling_anchor_registry_immutable(fx):
    # The session-stored anchor is frozen at construction: item
    # assignment raises even for code holding the session reference.
    claims = [_session_claim("c1", _build_snapshot(fx, _passage(fx)),
                             "事件发生在 2026-03-10")]
    session = _session(trust_anchor=_anchor(claims))
    anchor = session._trust_anchor
    assert anchor is not None
    with pytest.raises(TypeError):
        anchor["assertion_registry"]["ASSERT-c1"] = "0" * 64
    with pytest.raises(TypeError):
        anchor["anchor_id"] = "FORGED"
    # The frozen anchor still validates through the session path:
    # shape checks accept mappings, not only plain dicts.
    assert anchor["assertion_registry"]["ASSERT-c1"] == \
        claims[0]["formal_association_assertion"]["assertion_digest"]


def test_ruling_constructor_argument_isolation(fx):
    # Defensive copy at construction: mutating the caller's own
    # anchor dict and authorized set afterwards cannot move the
    # session's stored authorization.
    anchor = _anchor([])
    anchor["assertion_registry"] = {"AID": "0" * 64}
    players = {A}
    session = ConsumeSession(
        target_domain=TARGET, authorized_players=players,
        authorized_sources={"fixture_doc_D1"},
        authorized_objects={"fixture_doc_D1"},
        trust_anchor=anchor)
    anchor["assertion_registry"]["AID"] = "1" * 64
    anchor["anchor_id"] = "FORGED"
    players.add(B)
    players.discard(A)
    assert session._trust_anchor["assertion_registry"]["AID"] == "0" * 64
    assert session._trust_anchor["anchor_id"] == "LOCAL_TEST_ORACLE"
    assert session.authorized_players == {A}


def test_ruling_post_construction_widening_guarded(fx):
    # Owner-side widening beyond the authorized index is re-checked
    # on every consume_identity call: it cannot yield a trusted
    # result, only the generic UNKNOWN with the specific cause kept
    # internal. (Narrowing via discard is the intentional revocation
    # feature and is covered by the existing directed-2 tests.)
    session = _session(authorized_players={A},
                       identity_authorization=_auth([A]))
    session.authorized_players.add(B)
    out = _identify(session, fx["players"], fx["aliases"],
                    "测试甲", "alias")
    assert out["status"] == "UNKNOWN"
    assert out["reason"] == "IDENTITY_COVERAGE_UNPROVEN"
    assert session._last_diagnostic["cause"] == \
        "IDENTITY_AUTHORIZATION_SCOPE_MISMATCH"


# ------------------------------------------------------- BLOCK-01: resolver boundary


def test_ruling_consumer_does_not_adopt_direct_verdict(fx):
    # The function-level fact, stated honestly: a direct resolver
    # call with a self-built anchor yields CONFLICT. The consumer
    # fact that matters: the SAME material through the trusted
    # entry (a session with NO anchor) yields UNKNOWN — final
    # trusted consumption does not adopt the direct result.
    passage = _passage(fx)
    snap = _build_snapshot(fx, passage)
    claims = [_session_claim("c1", snap, "事件发生在 2026-03-10"),
              _session_claim("c2", snap, "事件发生在 2026-03-15")]
    direct = resolve_claims_by_event(
        claims,
        passage_texts={("fixture_doc_D1", "rev-1", "P3"): passage["text"]},
        trust_anchor=_anchor(claims))
    assert direct["status"] == "CONFLICT"
    session = _session()
    chain = _bind(session, snap, passage)
    layer = _answer(session, fx, passage, chain, claims
                    )["payload"]["claim_layer"]
    assert layer["status"] == "UNKNOWN"


def test_ruling_resolver_call_graph():
    # Structural guard: in product source, exactly one caller may
    # invoke the resolver — ConsumeSession.consume_answer. Any future
    # product caller fails this test and must be treated as a new
    # trusted-entry decision (BLOCK-01 escalation condition), not a
    # silent reuse.
    src = Path(__file__).resolve().parents[1] / "src" / "cba_kb"
    assert src.is_dir()
    for path in sorted(src.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if path.name in ("kc_consume.py", "kc_projection.py"):
            continue
        assert "resolve_claims_by_event" not in text, path.name
    consume = (src / "kc_consume.py").read_text(encoding="utf-8")
    assert consume.count("resolve_claims_by_event(") == 1
    projection = (src / "kc_projection.py").read_text(encoding="utf-8")
    assert "def resolve_claims_by_event(" in projection


def test_ruling_consume_entry_has_no_anchor_parameter():
    # The trusted entries take no per-call anchor/authorization
    # override: a caller has nowhere to inject an anchor into
    # trusted consumption.
    assert "trust_anchor" not in inspect.signature(
        ConsumeSession.consume_answer).parameters
    assert "identity_authorization" not in inspect.signature(
        ConsumeSession.consume_identity).parameters
    assert "authorized_players" not in inspect.signature(
        ConsumeSession.consume_identity).parameters


def test_ruling_frozen_anchor_positive_preserved(fx):
    # Positive path preserved through the frozen anchor: a session
    # whose preset registry matches the claims still yields a real
    # CONFLICT via the trusted entry.
    passage = _passage(fx)
    snap = _build_snapshot(fx, passage)
    claims = [_session_claim("c1", snap, "事件发生在 2026-03-10"),
              _session_claim("c2", snap, "事件发生在 2026-03-15")]
    session = _session(trust_anchor=_anchor(claims))
    chain = _bind(session, snap, passage)
    layer = _answer(session, fx, passage, chain, claims
                    )["payload"]["claim_layer"]
    assert layer["status"] == "CONFLICT"
