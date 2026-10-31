"""CBA-KB v2.1.1 T2-1 (Issue #226) — tests written FIRST.

Falsification labelling (honest, per group):
- B-EVENT / B-MISREJECTION tests are BEHAVIORAL: they call the
  pre-existing entry (ConsumeSession.consume_answer) and the
  pre-existing resolver, so on the baseline HEAD they fail because
  the current code returns/refuses the wrong thing (same-event
  different-date is OK; a provenance-only difference is refused).
  No interface absence is involved.
- A-GUARANTEE / C-IDENTITY / D-PRODUCER tests are INTERFACE-DELTA
  on the baseline: the new functions/methods/module do not exist
  yet, so they fail at import/attribute lookup. Each is paired with
  the baseline gap demonstration in
  workspace/cba-kb-v211/e1-receipt/e1_gap_demo.py (run on the
  baseline HEAD), which shows the underlying gap in current code:
  a Fact with no fact->source version/field mapping is accepted,
  there is no controlled identity entry, and there is no local
  producer. The gap demo is evidence, not a test.
- PIN tests pass on both old and new code; they document
  no-false-refusal / backward-compatibility behaviour and are NOT
  falsification evidence.
- 2026-10-31 Q2-227 test-hygiene correction: the getattr
  presence checks in the _guarantee_fns / _event_resolver /
  _identity / _producer helpers were removed — a missing
  attribute now surfaces as an error, never as falsification
  evidence. Old-SHA falsification below is behavioral only.

All fixtures are clean-room synthetic. All artifacts SIMULATED.
No Drive, no real data, no P0 claim, no production claim.
"""

import copy
import importlib
import json
import os
import subprocess
import sys

import pytest

from cba_kb.kc_cleanroom import A, B, QUOTE, SEASON, build_cleanroom_fixture
from cba_kb.kc_consume import ConsumeSession, _content_digest
from cba_kb.kc_passages import build_routed_passage, evidence_chain
from cba_kb.kc_projection import (build_passage, canonical_json,
                                  resolve_claims, sha256_hex)
from cba_kb.kc_snapshot import build_snapshot

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


def _bound_session(fx, event_suffix=""):
    # event_suffix (event-identity repair, 2026-10-31): when
    # given, both passages' texts state the claimed event, so
    # the E4b entry test's associations are content-backed.
    # Other tests keep the original texts byte-identical, so
    # their verbatim/citation assertions are untouched.
    """Session bound to a snapshot that actually contains the cited
    Document source and the justifying slice digest (same corrected
    fixture pattern as the PR-B consume tests). Post-freeze Q2
    B-03 fix: the session also authorizes fixture_doc_D2 and the
    snapshot carries a D2 entry, because the E4b entry test resolves
    a D1+D2 claim pair through the consumer's authorization and
    snapshot context. Provenance-binding repair (2026-10-31):
    the D2 passage itself is registered too — the D2
    association (fixture_doc_D2, rev-1, P7) must anchor to a
    digest-validated registered passage, not merely to a
    snapshot entry and a recomputed digest."""
    session = _session(authorized_sources={"fixture_doc_D1",
                                           "fixture_doc_D2"})
    passage = build_routed_passage(
        source_id="fixture_doc_D1", revision="rev-1", locator="P3",
        text=fx["passages"][0]["text"] + event_suffix,
        player_uid=A, aliases=["测试甲"],
        title="fixture_doc_D1",
        authorized_url="fixture://authorized-copy/fixture_doc_D1")
    passage_d2 = build_routed_passage(
        source_id="fixture_doc_D2", revision="rev-1", locator="P7",
        text="第二来源文本。" + event_suffix,
        player_uid=A, aliases=["测试甲"],
        title="fixture_doc_D2",
        authorized_url="fixture://authorized-copy/fixture_doc_D2")
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
         "source_id": "fixture_doc_D1"},
        {"entry_id": "entry-doc-D2", "sha256": passage_d2["digest"],
         "authority": "canonical", "rights": "public",
         "source_id": "fixture_doc_D2"}]))
    session.bind_current(snap)
    session.register_passages([passage, passage_d2])
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
                metric="points_per_game", fact=_fact() if fact is None else fact,
                passage=passage, source_text=passage["text"],
                chain=chain, claims=claims)
    call.update(kw)
    return session.consume_answer(**call)


def _guarantee_fns():
    # Q2-227 test-hygiene correction (2026-10-31): no
    # INTERFACE-DELTA presence claim is used as falsification
    # evidence. These functions exist on the current HEAD; a
    # missing attribute surfaces as an AttributeError, not as a
    # business-semantic failure, and no test below counts it as
    # one.
    mod = importlib.import_module("cba_kb.kc_snapshot")
    return mod.build_fact_guarantee, mod.validate_fact_guarantee


def _event_resolver():
    # Same test-hygiene correction as _guarantee_fns: direct
    # attribute access, no presence-as-falsification claim.
    mod = importlib.import_module("cba_kb.kc_projection")
    return mod.resolve_claims_by_event


def _association_digest():
    mod = importlib.import_module("cba_kb.kc_projection")
    return mod.event_association_digest


def _identity_source(players, aliases):
    # Q2-227-04 correction: the digest is recomputed from the
    # claimed source, revision, target domain AND the actual
    # submitted players and aliases (the consumer entry's rule),
    # not from the triple alone and not from an arbitrary 64-hex
    # string — a digest for one payload cannot be replayed onto
    # different identity content.
    from cba_kb.kc_consume import identity_evidence_digest
    return {"source_id": "fixture_canonical_registry",
            "revision": "rev-1",
            "digest": identity_evidence_digest(
                source_id="fixture_canonical_registry",
                revision="rev-1", target_domain=TARGET,
                players=players, aliases=aliases)}


def _e4b_claims(snapshot_id=None):
    # Post-freeze Q2 B-03 fix, completed by the Q2-227-02 repair:
    # the associations are FORMAL assertions bound to each
    # claim's own source, revision, locator, player UID and
    # snapshot, with a digest RECOMPUTED over that bound content
    # — not a fixed hex string, and not a bare event_ref +
    # evidence declaration.
    digest_of = _association_digest()
    base = {"event_ref": "fixture_event_E4b",
            "evidence": ["fixture_doc_D1:P3", "fixture_doc_D2:P7"],
            "revision": "rev-1"}
    claims = [
        {"claim_id": "t21_claim_D1", "player_uid": A, "date": "2026-03-10",
         "text": "事件发生在 2026-03-10", "source_id": "fixture_doc_D1",
         "event_key": "fixture_event_E4b",
         "event_association": dict(
             base, source_id="fixture_doc_D1", locator="P3",
             player_uid=A, snapshot_id=snapshot_id)},
        {"claim_id": "t21_claim_D2", "player_uid": A, "date": "2026-03-15",
         "text": "事件发生在 2026-03-15", "source_id": "fixture_doc_D2",
         "event_key": "fixture_event_E4b",
         "event_association": dict(
             base, source_id="fixture_doc_D2", locator="P7",
             player_uid=A, snapshot_id=snapshot_id)},
    ]
    for claim in claims:
        claim["event_association"]["digest"] = digest_of(
            claim, claim["event_association"])
    return claims


# ------------------------------------------------------- A: Fact guarantee

def test_guarantee_digest_and_tamper(fx):
    build, validate = _guarantee_fns()
    _, snap, _, _ = _bound_session(fx)
    guarantee = build(fact_id="t21_fact", fact_version="v1",
                      source_id="fixture_doc_D1", file_version="rev-1",
                      field_scope=["player_uid", "season", "team_id"],
                      authorization_domain=TARGET,
                      snapshot_id=snap.snapshot_id)
    assert validate(guarantee, snapshot=snap)["valid"] is True
    tampered = dict(guarantee, field_scope=["player_uid"])
    assert validate(tampered, snapshot=snap)["valid"] is False


def test_guarantee_binding_failures(fx):
    build, validate = _guarantee_fns()
    _, snap, _, _ = _bound_session(fx)
    good = build(fact_id="t21_fact", fact_version="v1",
                 source_id="fixture_doc_D1", file_version="rev-1",
                 field_scope=["player_uid"],
                 authorization_domain=TARGET,
                 snapshot_id=snap.snapshot_id)
    assert validate(dict(good, snapshot_id="kc-snapshot:" + "0" * 64),
                    snapshot=snap)["valid"] is False
    assert validate(dict(good, authorization_domain="other_target"),
                    snapshot=snap)["valid"] is False
    assert validate(dict(good, source_id="fixture_doc_DOES_NOT_EXIST"),
                    snapshot=snap)["valid"] is False
    assert validate(dict(good, source_digest="c" * 64),
                    snapshot=snap)["valid"] is False
    assert validate(dict(good, field_scope=[]),
                    snapshot=snap)["valid"] is False


def test_answer_requires_guarantee_when_asked(fx):
    build, validate = _guarantee_fns()
    session, snap, passage, chain = _bound_session(fx)
    # Post-freeze Q2 B-02 fix: the scope must cover EVERY output
    # Fact field; Q2-227-01 repair: the Fact itself must carry
    # the version the sidecar claims, and the sidecar must carry
    # the snapshot-bound source digest — a version-less Fact or
    # a digest-less sidecar no longer passes the gate.
    fact = dict(_fact(), fact_version="v1")
    guarantee = build(fact_id="t21_fact", fact_version="v1",
                      source_id="fixture_doc_D1", file_version="rev-1",
                      field_scope=["fact_id", "player_uid", "season",
                                   "team_id", "kind", "source",
                                   "fact_version"],
                      authorization_domain=TARGET,
                      snapshot_id=snap.snapshot_id,
                      source_digest=passage["digest"])
    assert validate(guarantee, snapshot=snap)["valid"] is True
    with_guarantee = _answer(session, fx, passage, chain, [],
                             fact=fact, fact_guarantee=guarantee)
    grains = with_guarantee["payload"]["grains"]
    assert grains["CANONICAL_FACT"].get("fact_id") == "t21_fact"
    # B-01 fix: the gate is unconditional now; the no-guarantee
    # call below passes no kwargs at all (the old opt-in kwarg is
    # removed) and still yields Fact UNKNOWN.
    without = _answer(session, fx, passage, chain, [])
    grains = without["payload"]["grains"]
    assert grains["CANONICAL_FACT"]["status"] == "UNKNOWN"
    assert grains["CANONICAL_FACT"].get("fact_id") is None
    # Stats and Document are untouched by the Fact guarantee failure.
    assert grains["STATS"]["status"] == "OK"
    assert grains["STATS"]["value"] == 12.5
    assert grains["DOCUMENT"]["status"] == "OK"
    assert grains["DOCUMENT"]["verbatim"] == QUOTE


def test_answer_tampered_guarantee_means_unknown_fact_only(fx):
    build, _validate = _guarantee_fns()
    session, snap, passage, chain = _bound_session(fx)
    guarantee = build(fact_id="t21_fact", fact_version="v1",
                      source_id="fixture_doc_D1", file_version="rev-1",
                      field_scope=["player_uid", "season", "team_id"],
                      authorization_domain=TARGET,
                      snapshot_id=snap.snapshot_id)
    tampered = dict(guarantee, file_version="rev-999")
    out = _answer(session, fx, passage, chain, [],
                  fact_guarantee=tampered)
    grains = out["payload"]["grains"]
    assert grains["CANONICAL_FACT"]["status"] == "UNKNOWN"
    assert grains["STATS"]["status"] == "OK"
    assert grains["DOCUMENT"]["status"] == "OK"


def test_answer_default_requires_guarantee(fx):
    # B-01 fix (Human option (a), 2026-10-31): this test's old
    # expectation — default path returns Fact OK without a
    # guarantee — WAS the B-01 defect itself, so it is superseded,
    # not preserved. The default path now enforces the gate: no
    # guarantee means Fact UNKNOWN, with Stats and Document
    # untouched (contract §2.1(5)).
    session, _snap, passage, chain = _bound_session(fx)
    out = _answer(session, fx, passage, chain, [])
    grains = out["payload"]["grains"]
    assert grains["CANONICAL_FACT"]["status"] == "UNKNOWN"
    assert grains["CANONICAL_FACT"].get("fact_id") is None
    assert grains["STATS"]["status"] == "OK"
    assert grains["DOCUMENT"]["status"] == "OK"


# ------------------------------------------------------- B: Claim events

def test_event_resolver_same_event_diff_date_conflict():
    fn = _event_resolver()
    out = fn(_e4b_claims())
    assert out["status"] == "CONFLICT"
    assert out["conflicting_claim_ids"] == ["t21_claim_D1", "t21_claim_D2"]


def test_event_resolver_different_refs_bare_declarations_unknown():
    # Post-freeze Q2 B-03 correction: bare declarations (event_ref
    # + evidence strings only, no formal source/revision/locator/
    # digest binding) cannot prove that two refs are different
    # events either — event identity invoked but unproven means
    # UNKNOWN, not a proven-no-conflict OK.
    fn = _event_resolver()
    claims = [
        {"claim_id": "ev_a", "player_uid": A, "date": "2026-03-10",
         "text": "事件 A", "event_key": "event_A",
         "event_association": {"event_ref": "event_A",
                               "evidence": ["src_A:P1"]}},
        {"claim_id": "ev_b", "player_uid": A, "date": "2026-03-15",
         "text": "事件 B", "event_key": "event_B",
         "event_association": {"event_ref": "event_B",
                               "evidence": ["src_B:P1"]}},
    ]
    out = fn(claims)
    assert out["status"] == "UNKNOWN"
    assert out["conflicting_claim_ids"] == []


def test_event_resolver_same_ref_missing_evidence_unknown():
    fn = _event_resolver()
    claims = [
        {"claim_id": "ev_a", "player_uid": A, "date": "2026-03-10",
         "text": "事件 A", "event_key": "event_X",
         "event_association": {"event_ref": "event_X", "evidence": []}},
        {"claim_id": "ev_b", "player_uid": A, "date": "2026-03-15",
         "text": "事件 B", "event_key": "event_X",
         "event_association": {"event_ref": "event_X",
                               "evidence": ["src_X:P1"]}},
    ]
    out = fn(claims)
    assert out["status"] == "UNKNOWN"
    assert out["conflicting_claim_ids"] == []


def test_event_resolver_key_without_association_unknown():
    fn = _event_resolver()
    claims = [
        {"claim_id": "ev_a", "player_uid": A, "date": "2026-03-10",
         "text": "事件 A", "event_key": "event_X"},
        {"claim_id": "ev_b", "player_uid": A, "date": "2026-03-15",
         "text": "事件 B", "event_key": "event_X"},
    ]
    out = fn(claims)
    assert out["status"] == "UNKNOWN"


def test_event_resolver_three_claims_precise_members():
    fn = _event_resolver()
    digest_of = _association_digest()
    # Q2-227-02 correction: the association is formal (source
    # bound to each claim's own source, revision, locator, UID)
    # AND its digest is recomputed over that bound content per
    # claim — the old fixed "f"*64 string was exactly the
    # format-only defect, so it cannot remain the fixture for a
    # trusted group. The shared-ref group is trusted and the
    # conflict lists exactly its members.
    base = {"event_ref": "event_T",
            "evidence": ["src_T:P1"], "source_id": "src_T",
            "revision": "rev-1", "locator": "P1", "player_uid": A}
    claims = [
        {"claim_id": "t_a1", "player_uid": A, "date": "2026-03-10",
         "text": "同一说法", "source_id": "src_T",
         "event_association": dict(base)},
        {"claim_id": "t_a2", "player_uid": A, "date": "2026-03-10",
         "text": "同一说法", "source_id": "src_T",
         "event_association": dict(base)},
        {"claim_id": "t_b", "player_uid": A, "date": "2026-03-15",
         "text": "不同说法", "source_id": "src_T",
         "event_association": dict(base)},
    ]
    for claim in claims:
        claim["event_association"]["digest"] = digest_of(
            claim, claim["event_association"])
    out = fn(claims)
    assert out["status"] == "CONFLICT"
    assert out["conflicting_claim_ids"] == ["t_a1", "t_a2", "t_b"]


def test_event_resolver_legacy_same_date_unknown():
    # Post-freeze Q2 B-04 correction: the legacy (player_uid,
    # date) heuristic must NOT decide CONFLICT. Differing texts
    # inside a legacy group are undetermined (the grouping is only
    # a candidate heuristic, and date itself may be the disputed
    # field), so they are UNRESOLVED. The legacy resolve_claims()
    # function itself is unchanged; only the new consumer-path
    # resolver stops promoting the heuristic to a decision.
    fn = _event_resolver()
    out = fn([
        {"claim_id": "lg_1", "player_uid": A, "date": "2026-03-10",
         "text": "说法一"},
        {"claim_id": "lg_2", "player_uid": A, "date": "2026-03-10",
         "text": "说法二"},
    ])
    assert out["status"] == "UNKNOWN"
    assert out["conflicting_claim_ids"] == []


def test_event_resolver_legacy_diff_date_no_event_fields_unknown():
    # Q2-227-03 correction (BEHAVIORAL red-first): with no event
    # fields at all and differing business values, the old
    # legacy OK was itself the defect — absence of a proven link
    # is not proof of no conflict, so the result is UNKNOWN, not
    # a proven-no-conflict OK. This test's old OK expectation is
    # superseded, not preserved. (Identical values and
    # single-claim inputs still keep OK.)
    fn = _event_resolver()
    out = fn([
        {"claim_id": "lg_1", "player_uid": A, "date": "2026-03-10",
         "text": "说法一"},
        {"claim_id": "lg_2", "player_uid": A, "date": "2026-03-15",
         "text": "说法二"},
    ])
    assert out["status"] == "UNKNOWN"
    assert out["conflicting_claim_ids"] == []


def test_answer_event_conflict_via_entry(fx):
    # BEHAVIORAL: baseline consume_answer resolves these as OK
    # (different dates, different groups) — the miss itself.
    # Q2-227-02: the consumer path binds associations to the
    # bound snapshot, so the fixture carries that snapshot id.
    session, _snap, passage, chain = _bound_session(
        fx, event_suffix=" 事件 fixture_event_E4b。")
    out = _answer(session, fx, passage, chain,
                  _e4b_claims(snapshot_id=_snap.snapshot_id))
    layer = out["payload"]["claim_layer"]
    assert layer["status"] == "CONFLICT"
    assert layer["conflicting_claim_ids"] == ["t21_claim_D1", "t21_claim_D2"]
    assert layer["refusals"] == []


def test_answer_provenance_only_claim_not_refused(fx):
    # BEHAVIORAL: baseline refuses this claim (kind differs from the
    # Fact kind), although only provenance/kind fields differ.
    session, _snap, passage, chain = _bound_session(fx)
    claim = {"claim_id": "prov_claim", "player_uid": A,
             "date": "2026-10-20", "text": "无关",
             "source_id": "fixture_doc_OTHER", "locator": "P9",
             "kind": "STATEMENT"}
    out = _answer(session, fx, passage, chain, [claim])
    layer = out["payload"]["claim_layer"]
    assert layer["refusals"] == []
    assert layer["status"] == "OK"


def test_answer_business_diff_still_refused(fx):
    # PIN (passes pre- and post-fix): a genuine business-field
    # difference (team_id) is still a refusal, not a mis-rejection fix
    # that swallows real collisions.
    session, _snap, passage, chain = _bound_session(fx)
    claim = {"claim_id": "biz_claim", "player_uid": A,
             "date": "2026-10-20", "text": "无关",
             "team_id": "fixture_team_OTHER"}
    out = _answer(session, fx, passage, chain, [claim])
    layer = out["payload"]["claim_layer"]
    assert any(r.get("refused") is True for r in layer["refusals"])


# ------------------------------------------------------- C: Identity entry

def _identity(session, fx, **kw):
    # Test-hygiene correction: direct method access, no
    # presence-as-falsification claim. The identity digest is
    # computed over the ACTUAL players/aliases being passed
    # (after kw overrides), unless the caller explicitly supplies
    # its own identity_source — otherwise a subset-players test
    # would fail on digest mismatch instead of on coverage.
    fn = session.consume_identity
    call = dict(players=fx["players"], aliases=fx["aliases"],
                query="测试甲", query_kind="alias",
                all_pages_received=True)
    call.update(kw)
    if "identity_source" not in kw:
        call["identity_source"] = _identity_source(
            call["players"], call["aliases"])
    return fn(**call)


def test_identity_alias_resolved_authorized(fx):
    session = _session()
    out = _identity(session, fx)
    assert out["payload"]["status"] == "RESOLVED"
    assert out["payload"]["candidates"] == [A]
    assert out["payload"]["relation"] == "SINGLE_CANDIDATE"
    assert out["payload"]["evidence_binding"]["source_id"] \
        == "fixture_canonical_registry"


def test_identity_same_name_clarification(fx):
    session = _session(authorized_players={A, B})
    out = _identity(session, fx, query="林测试", query_kind="name")
    assert out["payload"]["status"] == "CLARIFICATION_REQUIRED"
    assert out["payload"]["candidates"] == sorted([A, B])
    assert out["payload"]["relation"] == "UNDECIDED"


def test_identity_unauthorized_equals_nonexistent(fx):
    session = _session()
    players = list(fx["players"]) + [
        {"player_uid": "fixture_player_UNAUTHORIZED",
         "canonical_name": "未授权名"}]
    unauthorized = _identity(session, fx, players=players,
                             query="未授权名", query_kind="name")
    nonexistent = _identity(session, fx, players=players,
                            query="不存在名", query_kind="name")
    assert unauthorized == nonexistent
    assert unauthorized["status"] == "UNKNOWN"
    assert unauthorized["candidates"] == []
    assert "未授权名" not in str(unauthorized)
    assert "fixture_player_UNAUTHORIZED" not in str(unauthorized)


def test_identity_pagination_incomplete_unknown(fx):
    session = _session()
    out = _identity(session, fx, all_pages_received=False)
    assert out["status"] == "UNKNOWN"
    assert out["reason"] == "PAGINATION_INCOMPLETE"
    assert out["candidates"] == []


def test_identity_evidence_not_bound_unknown(fx):
    session = _session()
    out = _identity(session, fx, identity_source=None)
    assert out["status"] == "UNKNOWN"
    assert out["reason"] == "IDENTITY_EVIDENCE_NOT_BOUND"
    assert out["candidates"] == []
    out2 = _identity(session, fx, identity_source={
        "source_id": "fixture_canonical_registry", "revision": "rev-1"})
    assert out2["status"] == "UNKNOWN"
    assert out2["reason"] == "IDENTITY_EVIDENCE_NOT_BOUND"


def test_identity_alias_for_unauthorized_player_not_resolved(fx):
    session = _session()
    aliases = list(fx["aliases"]) + [
        {"alias": "未授权别名", "player_uid": "fixture_player_UNAUTHORIZED",
         "approved": True}]
    out = _identity(session, fx, aliases=aliases, query="未授权别名",
                    query_kind="alias")
    assert out["status"] == "UNKNOWN"
    assert out["candidates"] == []


# ------------------------------------------------------- D: Local producer

def _producer():
    # Test-hygiene correction: direct attribute access, no
    # presence-as-falsification claim (see _guarantee_fns).
    mod = importlib.import_module("cba_kb.kc_local_producer")
    return mod.build_local_projection, mod.verify_local_projection


def test_local_producer_builds_and_verifies(fx, tmp_path):
    build, verify = _producer()
    out = build(fixture=fx, output_dir=str(tmp_path))
    assert verify(str(tmp_path))["valid"] is True
    assert out["snapshot_id"]
    assert (tmp_path / "index.md").exists()
    assert (tmp_path / "manifest.json").exists()
    assert (tmp_path / "answer.json").exists()
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["snapshot_id"] == out["snapshot_id"]
    assert out["snapshot_id"] in (tmp_path / "index.md").read_text()
    assert manifest["local_only"] is True
    assert manifest["drive_publication"] is False


def test_local_producer_tamper_detected(fx, tmp_path):
    build, verify = _producer()
    build(fixture=fx, output_dir=str(tmp_path))
    target = tmp_path / "answer.json"
    target.write_text(target.read_text() + "\n<!-- tampered -->")
    result = verify(str(tmp_path))
    assert result["valid"] is False


def test_local_producer_script(fx, tmp_path):
    repo_root = os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))
    script = os.path.join(repo_root, "scripts", "build_local_projection.py")
    assert os.path.exists(script), "producer script missing"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(
        [sys.executable, script, "--output-dir", str(tmp_path)],
        capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    _build, verify = _producer()
    assert verify(str(tmp_path))["valid"] is True
