"""CBA-KB v2.1.1 T2-1 — B-01 default guarantee gate tests (Human
option (a), 2026-10-31).

Red-first labelling (honest):
- test_b01_default_no_guarantee_fact_unknown,
  test_b01_default_tampered_guarantee_fact_unknown and
  test_b01_versioned_fact_version_mismatch_unknown are BEHAVIORAL:
  they call the pre-existing entry (ConsumeSession.consume_answer)
  on the normal default path (no guarantee kwargs at all) and fail
  on the old HEAD because the old default skips the guarantee gate,
  returning the Fact as authoritative. No interface absence is
  involved.
- test_b01_default_valid_guarantee_fact_ok and
  test_b01_versioned_fact_matching_version_ok are PINs: they pass
  on both old and new code (old code returns Fact OK because the
  gate is skipped; new code returns Fact OK because the guarantee
  genuinely validates). They prove the gate does not refuse legal
  guarantees, and are NOT falsification evidence.

Scope note: these tests pin the DEFAULT path. The old opt-in
parameter (require_fact_guarantee) is removed by this fix because
its only remaining use would be an explicit bypass of the gate,
which contract §2.1 leaves unapproved; no in-repo caller needed
explicit opt-out (producer real path and v211 tests all passed
True, updated mechanically to drop the kwarg).

All fixtures are clean-room synthetic. All artifacts SIMULATED.
No Drive, no real data, no P0 claim, no production claim.
"""

import pytest

from cba_kb.kc_cleanroom import A, QUOTE, SEASON, build_cleanroom_fixture
from cba_kb.kc_consume import ConsumeSession, _content_digest
from cba_kb.kc_passages import build_routed_passage, evidence_chain
from cba_kb.kc_snapshot import build_fact_guarantee, build_snapshot

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


def _bound_session(fx):
    """Session bound to a snapshot containing the cited Document
    source and the justifying slice digest (same corrected fixture
    pattern as the PR-B consume tests)."""
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
    snap = build_snapshot(
        entries=[
            {"entry_id": "entry-stats", "sha256": slice_digest,
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


def _fact(**over):
    fact = {"fact_id": "b01_fact", "player_uid": A, "season": SEASON,
            "team_id": "fixture_team_T2", "kind": "REGISTRATION",
            "source": "fixture_doc_D1"}
    fact.update(over)
    return fact


def _guarantee(snap, passage, fact, **over):
    kw = dict(fact_id=fact["fact_id"], fact_version="v1",
              source_id=fact["source"],
              file_version=passage["revision"],
              field_scope=sorted(fact.keys()),
              authorization_domain=TARGET,
              snapshot_id=snap.snapshot_id,
              source_digest=passage["digest"])
    kw.update(over)
    return build_fact_guarantee(**kw)


def _answer(session, fx, passage, chain, fact, **kw):
    call = dict(records=fx["stats_records"], player_uid=A, season=SEASON,
                match_type="REGULAR", scope="WHOLE_SEASON",
                provider=PROVIDER, competition=COMPETITION,
                metric="points_per_game", fact=fact, passage=passage,
                source_text=passage["text"], chain=chain, claims=[])
    call.update(kw)
    return session.consume_answer(**call)


def test_b01_default_no_guarantee_fact_unknown(fx):
    # BEHAVIORAL red-first: the normal default path (no guarantee
    # kwargs at all) must NOT authorise a Fact. Only the Fact layer
    # goes UNKNOWN; Stats and Document are untouched (contract
    # §2.1(5)).
    session, _snap, passage, chain = _bound_session(fx)
    out = _answer(session, fx, passage, chain, _fact())
    grains = out["payload"]["grains"]
    assert grains["CANONICAL_FACT"]["status"] == "UNKNOWN"
    assert grains["CANONICAL_FACT"].get("fact_id") is None
    assert grains["STATS"]["status"] == "OK"
    assert grains["STATS"]["value"] == 12.5
    assert grains["DOCUMENT"]["status"] == "OK"
    assert grains["DOCUMENT"]["verbatim"] == QUOTE


def test_b01_default_valid_guarantee_fact_ok(fx):
    # PIN: a genuinely valid guarantee on the default path passes.
    # Q2-227-01 correction (2026-10-31): the positive path now
    # requires a VERSIONED Fact — a version-less Fact is legacy
    # and stays UNKNOWN per §2.1(6), so the old version-less
    # positive expectation was itself the defect, not a PIN.
    session, snap, passage, chain = _bound_session(fx)
    fact = _fact(fact_version="v1")
    out = _answer(session, fx, passage, chain, fact,
                  fact_guarantee=_guarantee(snap, passage, fact))
    grains = out["payload"]["grains"]
    assert grains["CANONICAL_FACT"].get("fact_id") == "b01_fact"
    assert grains["STATS"]["status"] == "OK"
    assert grains["DOCUMENT"]["status"] == "OK"


def test_b01_default_tampered_guarantee_fact_unknown(fx):
    # BEHAVIORAL red-first: a guarantee bound to the wrong file
    # version must NOT authorise the Fact on the default path,
    # while Stats and Document stay OK.
    session, snap, passage, chain = _bound_session(fx)
    fact = _fact()
    guarantee = _guarantee(snap, passage, fact, file_version="rev-999")
    out = _answer(session, fx, passage, chain, fact,
                  fact_guarantee=guarantee)
    grains = out["payload"]["grains"]
    assert grains["CANONICAL_FACT"]["status"] == "UNKNOWN"
    assert grains["STATS"]["status"] == "OK"
    assert grains["DOCUMENT"]["status"] == "OK"


def test_b01_versioned_fact_version_mismatch_unknown(fx):
    # BEHAVIORAL red-first: when the Fact itself declares a version,
    # a self-consistent sidecar carrying a DIFFERENT version must
    # NOT authorise it. This proves the version check is live, not
    # dead code, for versioned Facts.
    session, snap, passage, chain = _bound_session(fx)
    fact = _fact(fact_version="v1")
    guarantee = _guarantee(snap, passage, fact, fact_version="v2")
    out = _answer(session, fx, passage, chain, fact,
                  fact_guarantee=guarantee)
    grains = out["payload"]["grains"]
    assert grains["CANONICAL_FACT"]["status"] == "UNKNOWN"
    assert grains["STATS"]["status"] == "OK"


def test_b01_versioned_fact_matching_version_ok(fx):
    # PIN: a versioned Fact with a matching-version guarantee passes.
    session, snap, passage, chain = _bound_session(fx)
    fact = _fact(fact_version="v1")
    out = _answer(session, fx, passage, chain, fact,
                  fact_guarantee=_guarantee(snap, passage, fact))
    grains = out["payload"]["grains"]
    assert grains["CANONICAL_FACT"].get("fact_id") == "b01_fact"
