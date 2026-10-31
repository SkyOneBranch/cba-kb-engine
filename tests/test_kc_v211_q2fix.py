"""CBA-KB v2.1.1 T2-1 — bounded repair counterexamples for the
post-freeze independent Q2 (2026-10-31) blockers B-01..B-06.

RED-FIRST: every test in the Q2FIX sections below is written to
FAIL on the old HEAD (PR #227, d1f746e6) because the old code
returns the wrong business result, and to PASS only after the
bounded repair. The PIN test at the end passes on both heads and
documents preservation, not falsification.

Scope notes (read before touching these tests):
- B-01 (Fact default bypass) is NOT fixed here: making the
  guarantee mandatory by default would break main-baseline
  consume_answer tests that assert Fact OK without a guarantee,
  and migrating those tests exceeds this repair's authorized
  scope. The default path is therefore left unchanged and
  reported as blocked; B-02/B-06 below strengthen the gate
  itself and wire the producer through it.
- B-02 fact_version binding is limited by the data model:
  Fact dicts carry no version field, so there is no actual
  Fact version to compare against (adding one would be a
  canonical schema change, D-class, not approved). The tests
  below therefore cover fact_id, source, field_scope and
  file_version binding, which ARE checkable with existing data.
- Claim tests use formal associations (source_id, revision,
  locator, 64-hex digest) per contract §3.1(2); bare
  declarations must NOT yield CONFLICT or proven-no-conflict OK.
- Identity tests use a recomputed digest (sha256 of the
  canonical JSON of {source_id, revision, target_domain}),
  reusing the producer's existing digest rule; an arbitrary
  64-hex string must NOT pass.

All fixtures are clean-room synthetic. No Drive, no real data.
"""

import copy
import json

import pytest

from cba_kb.kc_cleanroom import A, B, QUOTE, SEASON, build_cleanroom_fixture
from cba_kb.kc_consume import ConsumeSession, _content_digest
from cba_kb.kc_local_producer import (
    build_local_projection,
    verify_local_projection,
)
from cba_kb.kc_passages import build_routed_passage, evidence_chain
from cba_kb.kc_projection import (
    canonical_json,
    resolve_claims_by_event,
    sha256_hex,
)
from cba_kb.kc_snapshot import (
    build_fact_guarantee,
    build_snapshot,
    validate_fact_guarantee,
)

PROVIDER = "CBA_OFFICIAL"
COMPETITION = "CBA"
TARGET = "fixture_target"


@pytest.fixture(scope="module")
def fx():
    return build_cleanroom_fixture()


def _session(**over):
    kw = dict(target_domain=TARGET,
              authorized_players={A},
              authorized_sources={"fixture_doc_D1"},
              authorized_objects={"fixture_doc_D1"})
    kw.update(over)
    return ConsumeSession(**kw)


def _snapshot_kwargs(**over):
    kw = dict(
        entries=[{"entry_id": "entry-stats", "sha256": "a" * 64,
                  "authority": "canonical", "rights": "public",
                  "source_id": "fixture_stats"}],
        target_domain=TARGET, capabilities=["stats"],
        build_provenance="simulated-build-1",
        build_time="2026-10-28T00:00:00+00:00",
        data_release="SIMULATED_DATA_RELEASE_1",
        engine_candidate="SIMULATED_ENGINE_CANDIDATE_1",
        build_code="SIMULATED_BUILD_CODE_1")
    kw.update(over)
    return kw


def _bound_session(fx):
    session = _session()
    passage = build_routed_passage(
        source_id="fixture_doc_D1", revision="rev-1", locator="P3",
        text=fx["passages"][0]["text"], player_uid=A, aliases=["测试甲"],
        title="fixture_doc_D1",
        authorized_url="fixture://authorized-copy/fixture_doc_D1")
    probe = _session()
    probed = probe.consume_slice(
        fx["stats_records"], player_uid=A, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION)
    slice_digest = _content_digest(probed["payload"])
    snap = build_snapshot(**_snapshot_kwargs(entries=[
        {"entry_id": "entry-stats", "sha256": slice_digest,
         "authority": "canonical", "rights": "public",
         "source_id": "fixture_stats"},
        {"entry_id": "entry-doc-D1", "sha256": passage["digest"],
         "authority": "canonical", "rights": "public",
         "source_id": "fixture_doc_D1"}]))
    session.bind_current(snap)
    session.register_passages([passage])
    binding = session.current_binding()
    chain = evidence_chain(
        receipt_id=binding["receipt_binding"],
        snapshot_id=snap.snapshot_id,
        build_version=snap.build_code,
        source_digest=passage["digest"],
        authorization_basis=binding["authorization_basis"])
    return session, snap, passage, chain


def _fact():
    return {"fact_id": "t21_fact", "player_uid": A, "season": SEASON,
            "team_id": "fixture_team_T2", "kind": "REGISTRATION",
            "source": "fixture_doc_D1"}


def _answer(session, fx, passage, chain, claims, fact=None, **kw):
    call = dict(records=fx["stats_records"], player_uid=A, season=SEASON,
                match_type="REGULAR", scope="WHOLE_SEASON",
                provider=PROVIDER, competition=COMPETITION,
                metric="points_per_game",
                fact=_fact() if fact is None else fact,
                passage=passage, source_text=passage["text"],
                chain=chain, claims=claims)
    call.update(kw)
    return session.consume_answer(**call)


def _guarantee(snap, **over):
    kw = dict(fact_id="t21_fact", fact_version="v1",
              source_id="fixture_doc_D1", file_version="rev-1",
              field_scope=["fact_id", "player_uid", "season",
                           "team_id", "kind", "source"],
              authorization_domain=TARGET,
              snapshot_id=snap.snapshot_id)
    kw.update(over)
    return build_fact_guarantee(**kw)


# ------------------------------------------------------- B-02: Fact binding

def test_q2fix_fact_partial_scope_unknown(fx):
    # Q2 B-02: a self-consistent sidecar with a partial field_scope
    # must NOT validate the Fact. Old code: Fact OK (scope only
    # checked non-empty + fact_id match). New code: UNKNOWN.
    session, snap, passage, chain = _bound_session(fx)
    guarantee = _guarantee(snap, field_scope=["player_uid", "season"])
    assert validate_fact_guarantee(guarantee, snapshot=snap)["valid"] \
        is True  # sidecar self-consistency alone still holds
    out = _answer(session, fx, passage, chain, [],
                  fact_guarantee=guarantee)
    assert out["payload"]["grains"]["CANONICAL_FACT"].get("status") \
        == "UNKNOWN"
    assert out["payload"]["grains"]["STATS"]["status"] == "OK"


def test_q2fix_fact_wrong_source_unknown(fx):
    # Q2 B-02: a guarantee for a DIFFERENT snapshot source must NOT
    # validate this Fact, even though that source is in the snapshot.
    session, snap, passage, chain = _bound_session(fx)
    guarantee = _guarantee(snap, source_id="fixture_stats")
    assert validate_fact_guarantee(guarantee, snapshot=snap)["valid"] \
        is True  # fixture_stats IS in the snapshot; binding is the gap
    out = _answer(session, fx, passage, chain, [],
                  fact_guarantee=guarantee)
    assert out["payload"]["grains"]["CANONICAL_FACT"].get("status") \
        == "UNKNOWN"


def test_q2fix_fact_wrong_file_version_unknown(fx):
    # Q2 B-02: a FRESHLY BUILT (digest-valid) guarantee whose
    # file_version does not match the cited passage revision must
    # NOT validate the Fact. (Tampering would fail on digest alone;
    # building fresh isolates the revision check.)
    session, snap, passage, chain = _bound_session(fx)
    guarantee = _guarantee(snap, file_version="rev-999")
    assert validate_fact_guarantee(guarantee, snapshot=snap)["valid"] \
        is True  # digest is valid for rev-999; revision binding is gap
    out = _answer(session, fx, passage, chain, [],
                  fact_guarantee=guarantee)
    assert out["payload"]["grains"]["CANONICAL_FACT"].get("status") \
        == "UNKNOWN"


# ------------------------------------------------------- B-03/B-04: Claim

def _bare_e4b_claims():
    # Old-shape E4b input: non-empty declarations only, no formal
    # association fields (no locator, no digest). Must NOT yield
    # CONFLICT under contract §3.1(2).
    assoc = {"event_ref": "fixture_event_E4b",
             "evidence": ["fixture_doc_D1:P3", "fixture_doc_D2:P7"],
             "source_id": "fixture_doc_D1", "revision": "rev-1"}
    return [
        {"claim_id": "t21_claim_D1", "player_uid": A, "date": "2026-03-10",
         "text": "事件发生在 2026-03-10", "source_id": "fixture_doc_D1",
         "event_key": "fixture_event_E4b",
         "event_association": dict(assoc)},
        {"claim_id": "t21_claim_D2", "player_uid": A, "date": "2026-03-15",
         "text": "事件发生在 2026-03-15", "source_id": "fixture_doc_D2",
         "event_key": "fixture_event_E4b",
         "event_association": dict(assoc, source_id="fixture_doc_D2")},
    ]


def test_q2fix_claim_bare_declarations_unknown():
    # Q2 B-03: bare declarations are not formal proof.
    out = resolve_claims_by_event(_bare_e4b_claims())
    assert out["status"] == "UNKNOWN"
    assert out["conflicting_claim_ids"] == []


def test_q2fix_claim_legacy_same_date_unknown():
    # Q2 B-04: the legacy (player_uid, date) heuristic must NOT
    # decide CONFLICT by itself; differing texts with no formal
    # evidence are UNDETERMINED, not a proven conflict.
    out = resolve_claims_by_event([
        {"claim_id": "lg_1", "player_uid": A, "date": "2026-03-10",
         "text": "说法一"},
        {"claim_id": "lg_2", "player_uid": A, "date": "2026-03-10",
         "text": "说法二"},
    ])
    assert out["status"] == "UNKNOWN"
    assert out["conflicting_claim_ids"] == []


def test_q2fix_claim_bare_different_refs_unknown():
    # Contract §3.1(5): insufficient evidence must NOT be reported
    # as proven-no-conflict OK.
    out = resolve_claims_by_event([
        {"claim_id": "ev_a", "player_uid": A, "date": "2026-03-10",
         "text": "事件 A", "event_key": "event_A",
         "event_association": {"event_ref": "event_A",
                               "evidence": ["src_A:P1"]}},
        {"claim_id": "ev_b", "player_uid": A, "date": "2026-03-15",
         "text": "事件 B", "event_key": "event_B",
         "event_association": {"event_ref": "event_B",
                               "evidence": ["src_B:P1"]}},
    ])
    assert out["status"] == "UNKNOWN"


# ------------------------------------------------------- B-05: Identity

def _identity_source_real():
    return {"source_id": "fixture_canonical_registry",
            "revision": "rev-1",
            "digest": sha256_hex(canonical_json({
                "source_id": "fixture_canonical_registry",
                "revision": "rev-1", "target_domain": TARGET}))}


def _identity(session, fx, **kw):
    call = dict(players=fx["players"], aliases=fx["aliases"],
                query="测试甲", query_kind="alias",
                identity_source=_identity_source_real(),
                all_pages_received=True)
    call.update(kw)
    return session.consume_identity(**call)


def test_q2fix_identity_coverage_gap_unknown(fx):
    # Q2 B-05: all_pages_received=True alone must NOT prove
    # pagination completeness. Inputs that do not cover the
    # authorized player set are an unproven page set -> UNKNOWN.
    session = _session(authorized_players={A, B})
    players = [p for p in fx["players"] if p["player_uid"] == A]
    out = _identity(session, fx, players=players)
    assert out["status"] == "UNKNOWN"
    assert out["candidates"] == []


def test_q2fix_identity_arbitrary_digest_unknown(fx):
    # Q2 B-05: a merely well-formed 64-hex digest must NOT pass
    # evidence binding; it must be recomputable from the claimed
    # source, revision and target domain.
    session = _session()
    out = _identity(session, fx, identity_source={
        "source_id": "fixture_canonical_registry", "revision": "rev-1",
        "digest": "b" * 64})
    assert out["status"] == "UNKNOWN"
    assert out["candidates"] == []


def test_q2fix_identity_real_digest_resolved_pin(fx):
    # PIN (passes pre- and post-fix): a recomputed digest with full
    # authorized coverage still resolves. Not falsification.
    session = _session()
    out = _identity(session, fx)
    assert out["payload"]["status"] == "RESOLVED"
    assert out["payload"]["candidates"] == [A]


# ------------------------------------------------------- B-06: Producer wiring

def test_q2fix_producer_cleanroom_separation(fx, tmp_path):
    # Q2 B-06: the clean-room fixture Fact is source-unbound, so the
    # sample answer's Fact is UNKNOWN. The sample guarantee sidecar
    # must NOT be presented as validation of that answer Fact: the
    # summary must extract the actual Fact identifiers and state
    # explicitly that the answer Fact is NOT guaranteed.
    out = build_local_projection(fixture=fx, output_dir=str(tmp_path))
    assert verify_local_projection(str(tmp_path))["valid"] is True
    assert out.get("answer_fact_id") == "fixture_fact_registration_A"
    assert out.get("answer_fact_status") == "UNKNOWN"
    assert out.get("answer_fact_guaranteed") is False
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["guarantee"]["fact_id"] != out.get("answer_fact_id")
    assert manifest["guarantee"]["covers_answer_fact"] is False


def test_q2fix_producer_eligible_fact_wired(fx, tmp_path):
    # Q2 B-06 positive path: when the fixture Fact IS guarantee-
    # eligible (declared version + snapshot-bound source), the
    # producer must wire the REAL guarantee through the gate, so
    # the answer Fact is OK *because* its own guarantee validated,
    # and the guarantee's fact_id IS the answer's fact_id.
    fact = {"fact_id": "eligible_fact", "fact_version": "v1",
            "player_uid": A, "season": SEASON,
            "team_id": "fixture_team_T2", "kind": "REGISTRATION",
            "source": "fixture_doc_D1"}
    fixture = {"players": fx["players"], "aliases": fx["aliases"],
               "stats_records": fx["stats_records"], "fact": fact,
               "claims": [], "passages": [fx["passages"][0]],
               "source_objects": []}
    out = build_local_projection(fixture=fixture,
                                 output_dir=str(tmp_path))
    assert verify_local_projection(str(tmp_path))["valid"] is True
    assert out.get("answer_fact_status") == "OK"
    assert out.get("answer_fact_id") == "eligible_fact"
    assert out.get("answer_fact_guaranteed") is True
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["guarantee"]["fact_id"] == "eligible_fact"
    assert manifest["guarantee"]["covers_answer_fact"] is True
    guarantee = json.loads((tmp_path / "guarantee.json").read_text())
    assert guarantee["fact_id"] == "eligible_fact"
    assert guarantee["fact_version"] == "v1"
