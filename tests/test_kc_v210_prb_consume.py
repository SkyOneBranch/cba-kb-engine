"""Route A unified consumption entry — attack tests, written FIRST.

Falsification labelling (honest, per test):
- ENTRY tests (rules A, C, D, E, F at the entry surface) fail on the
  rejected HEAD by INTERFACE-DELTA: module cba_kb.kc_consume does not
  exist yet, so the test fails asserting the entry's presence. Each
  such test is paired with a current-code behavioral demonstration in
  prb-evidence/consume-gap-demo.py (run on the rejected HEAD), which
  shows the underlying gap in the raw primitives: raw slice paths
  distinguish unauthorized from missing, raw cite_verbatim returns OK
  for a non-member passage, a naive claim merge succeeds silently, and
  the raw citation path ignores evidence chains entirely.
- STORE tests (rule B) fail on the rejected HEAD BEHAVIORALLY:
  SimulatedCurrentStore.commit() accepts a dict snapshot mutated after
  validate(), and accepts a registry entry replaced after validate(),
  so pytest.raises fails with DID NOT RAISE. No interface absence is
  involved; the store API already exists on the rejected HEAD.
- PIN tests pass on both old and new code; they document
  no-false-refusal behavior and are NOT falsification evidence.

All fixtures are clean-room synthetic. All artifacts SIMULATED.
No real Drive UAT, no P0 claim, no production claim.
"""

import copy
import importlib

import pytest

from cba_kb.kc_cleanroom import (
    A,
    B,
    QUOTE,
    SEASON,
    build_cleanroom_fixture,
)
from cba_kb.kc_current import KcCurrentError, SimulatedCurrentStore
from cba_kb.kc_snapshot import build_snapshot
from cba_kb.kc_passages import build_routed_passage, cite_verbatim
from cba_kb.kc_projection import build_passage
from cba_kb.kc_slices import build_slice

PROVIDER = "CBA_OFFICIAL"
COMPETITION = "CBA"
TARGET = "fixture_target"


@pytest.fixture(scope="module")
def fx():
    return build_cleanroom_fixture()


def _consume():
    """The Route A entry module, or None on the rejected HEAD."""
    try:
        return importlib.import_module("cba_kb.kc_consume")
    except ImportError:
        return None


def _entry_or_fail():
    mod = _consume()
    assert mod is not None, (
        "kc_consume entry missing on rejected HEAD "
        "(INTERFACE-DELTA: new entry point, not yet built)")
    return mod


def _session(mod, **over):
    kw = dict(target_domain=TARGET,
              authorized_players={A},
              authorized_sources={"fixture_doc_D1"},
              authorized_objects={"fixture_doc_D1"})
    kw.update(over)
    return mod.ConsumeSession(**kw)


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


def _snapshot_dict_of(snap):
    receipt = snap.simulated_validator_receipt
    receipt_d = dict(receipt) if isinstance(receipt, dict) \
        else dict(vars(receipt))
    return {
        "entries": [{"entry_id": e.entry_id, "sha256": e.sha256,
                     "authority": e.authority, "rights": e.rights,
                     "source_id": e.source_id} for e in snap.entries],
        "target_domain": snap.target_domain,
        "capabilities": list(snap.capabilities),
        "build_provenance": snap.build_provenance,
        "build_time": snap.build_time,
        "simulated_published_state": snap.simulated_published_state,
        "data_release": snap.data_release,
        "engine_candidate": snap.engine_candidate,
        "build_code": snap.build_code,
        "target_authorization_domain": snap.target_authorization_domain,
        "catalog_references": list(snap.catalog_references),
        "source_digests": list(snap.source_digests),
        "simulated_validator_receipt": receipt_d,
        "content_sha256": snap.content_sha256,
        "snapshot_id": snap.snapshot_id,
        "digest_kind": snap.digest_kind,
        "algorithm": snap.algorithm,
        "covered_bytes": snap.covered_bytes,
    }


def _routed_target(fx):
    target = [p for p in fx["passages"]
              if p["source_id"] == "fixture_doc_D1"][0]
    return build_routed_passage(
        source_id=target["source_id"], revision=target["revision"],
        locator=target["locator"], text=target["text"],
        player_uid=target.get("player_uid"),
        aliases=target.get("aliases") or [],
        title=target["source_id"],
        authorized_url="fixture://authorized-copy/fixture_doc_D1")


def _bound_session(mod, fx):
    """Session with a validated current snapshot and the D1 passage
    registered. Returns (session, snapshot, passage, chain)."""
    session = _session(mod)
    snap = build_snapshot(**_snapshot_kwargs())
    session.bind_current(snap)
    passage = _routed_target(fx)
    session.register_passages([passage])
    binding = session.current_binding()
    from cba_kb.kc_passages import evidence_chain
    chain = evidence_chain(
        receipt_id=binding["receipt_binding"],
        snapshot_id=snap.snapshot_id,
        build_version=snap.build_code,
        source_digest=passage["digest"],
        authorization_basis=binding["authorization_basis"])
    return session, snap, passage, chain


# ------------------------------------------------------- RULE A (entry)

def test_entry_slice_unauthorized_equals_nonexistent(fx):
    mod = _entry_or_fail()
    session = _session(mod)
    unauthorized = session.consume_slice(
        fx["stats_records"], player_uid="fixture_player_UNAUTHORIZED",
        season=SEASON, match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION)
    nonexistent = session.consume_slice(
        fx["stats_records"], player_uid="fixture_player_DOES_NOT_EXIST",
        season=SEASON, match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION)
    assert unauthorized == nonexistent
    assert unauthorized["status"] == "UNKNOWN"
    assert unauthorized["value"] is None
    # No distinguishing diagnostic, count, path, or error code.
    assert "PLAYER_NOT_AUTHORIZED" not in str(unauthorized)
    assert "EMPTY" not in str(unauthorized)


def test_entry_slice_authorized_player_still_answers(fx):
    mod = _entry_or_fail()
    session = _session(mod)
    out = session.consume_slice(
        fx["stats_records"], player_uid=A, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION)
    assert out["payload"]["status"] == "OK"
    assert out["payload"]["metrics"]["points_per_game"]["value"] == 12.5
    assert out["verification"]["verified_by"] == "kc_consume"


def test_entry_object_unauthorized_equals_nonexistent(fx):
    mod = _entry_or_fail()
    session = _session(mod)
    blocked = session.consume_object(fx["source_objects"],
                                     "fixture_doc_BLOCKED")
    missing = session.consume_object(fx["source_objects"],
                                     "fixture_doc_DOES_NOT_EXIST")
    assert blocked == missing
    assert blocked["status"] == "UNKNOWN"


def test_entry_catalog_unauthorized_equals_nonexistent(fx):
    mod = _entry_or_fail()
    session = _session(mod)
    key = (SEASON, "REGULAR", "WHOLE_SEASON", None)
    unauthorized = session.consume_catalog(
        fx["stats_records"], player_uid="fixture_player_UNAUTHORIZED",
        expected_keys=[key], provider=PROVIDER, competition=COMPETITION)
    nonexistent = session.consume_catalog(
        fx["stats_records"], player_uid="fixture_player_DOES_NOT_EXIST",
        expected_keys=[key], provider=PROVIDER, competition=COMPETITION)
    assert unauthorized == nonexistent
    assert unauthorized["status"] == "UNKNOWN"


def test_entry_citation_failures_indistinguishable(fx):
    mod = _entry_or_fail()
    session, snap, passage, chain = _bound_session(mod, fx)
    # (1) non-member passage with a valid digest for its own fields.
    outsider = build_routed_passage(
        source_id="fixture_doc_OUTSIDER", revision="rev-1", locator="P9",
        text=QUOTE, player_uid=A, aliases=["测试甲"],
        title="outsider",
        authorized_url="fixture://authorized-copy/fixture_doc_OUTSIDER")
    # (2) forged receipt binding.
    from cba_kb.kc_passages import evidence_chain
    forged_chain = evidence_chain(
        receipt_id="forged-receipt", snapshot_id=snap.snapshot_id,
        build_version=snap.build_code, source_digest=passage["digest"],
        authorization_basis="SIMULATED_TARGET_SCOPE")
    # (3) wrong snapshot binding.
    wrong_snap_chain = evidence_chain(
        receipt_id="forged-receipt", snapshot_id="kc-snapshot:other",
        build_version=snap.build_code, source_digest=passage["digest"],
        authorization_basis="SIMULATED_TARGET_SCOPE")
    # (4) tampered text (digest no longer matches the presented text).
    tampered = dict(passage, text="篡改后的文本。")
    results = [
        session.consume_citation(passage=outsider, source_text=QUOTE,
                                 chain=chain),
        session.consume_citation(passage=passage, source_text=QUOTE,
                                 chain=forged_chain),
        session.consume_citation(passage=passage, source_text=QUOTE,
                                 chain=wrong_snap_chain),
        session.consume_citation(passage=tampered, source_text=QUOTE,
                                 chain=chain),
    ]
    assert all(r == results[0] for r in results)
    assert results[0]["status"] == "UNKNOWN"
    assert results[0]["verbatim"] is None


# ------------------------------------------------------- RULE B (behavioral)

def test_commit_refuses_dict_mutated_after_validate():
    snap = build_snapshot(**_snapshot_kwargs())
    store = SimulatedCurrentStore(target_domain=TARGET)
    snap_d = _snapshot_dict_of(snap)
    store.register(snap_d)
    store.stage(snap.snapshot_id)
    store.validate()
    assert store.state == "VALIDATED"
    # Attack: mutate the registered dict AFTER validation.
    snap_d["target_domain"] = "attacker_target"
    with pytest.raises(KcCurrentError):
        store.commit()
    assert store.state == "FAILED"
    assert store.read_current() is None


def test_commit_refuses_registry_replacement_after_validate():
    snap = build_snapshot(**_snapshot_kwargs())
    store = SimulatedCurrentStore(target_domain=TARGET)
    snap_d = _snapshot_dict_of(snap)
    store.register(snap_d)
    store.stage(snap.snapshot_id)
    store.validate()
    assert store.state == "VALIDATED"
    # Attack: replace the registered entry with different content
    # under the same snapshot_id (digest now invalid for the content).
    replaced = _snapshot_dict_of(snap)
    replaced["entries"] = [dict(replaced["entries"][0], sha256="c" * 64)]
    store.register(replaced)
    with pytest.raises(KcCurrentError):
        store.commit()
    assert store.state == "FAILED"
    assert store.read_current() is None


def test_commit_refuses_entry_sha_changed_after_validate():
    snap = build_snapshot(**_snapshot_kwargs())
    store = SimulatedCurrentStore(target_domain=TARGET)
    snap_d = _snapshot_dict_of(snap)
    store.register(snap_d)
    store.stage(snap.snapshot_id)
    store.validate()
    # Attack: change one entry hash in place, keep the old digest.
    snap_d["entries"][0]["sha256"] = "d" * 64
    with pytest.raises(KcCurrentError):
        store.commit()
    assert store.state == "FAILED"


def test_commit_still_succeeds_when_unchanged_pin():
    # PIN (passes pre- and post-fix): no false refusal for a clean flow.
    snap = build_snapshot(**_snapshot_kwargs())
    store = SimulatedCurrentStore(target_domain=TARGET)
    snap_d = _snapshot_dict_of(snap)
    store.register(snap_d)
    store.stage(snap.snapshot_id)
    store.validate()
    receipt = store.commit()
    assert receipt["snapshot_id"] == snap.snapshot_id
    assert store.read_current() == snap.snapshot_id


def test_entry_commit_binding_refuses_mutation(fx):
    mod = _entry_or_fail()
    session = _session(mod)
    snap = build_snapshot(**_snapshot_kwargs())
    store = SimulatedCurrentStore(target_domain=TARGET)
    snap_d = _snapshot_dict_of(snap)
    store.register(snap_d)
    store.stage(snap.snapshot_id)
    store.validate()
    session.attach_store(store)
    snap_d["target_domain"] = "attacker_target"
    with pytest.raises(KcCurrentError):
        session.commit_current()
    assert store.read_current() is None


# ------------------------------------------------------- RULE C (entry)

def test_entry_citation_accepts_registered_member(fx):
    mod = _entry_or_fail()
    session, snap, passage, chain = _bound_session(mod, fx)
    out = session.consume_citation(passage=passage, source_text=QUOTE,
                                   chain=chain)
    assert out["payload"]["status"] == "OK"
    assert out["payload"]["verbatim"] == QUOTE
    assert out["payload"]["snapshot_id"] == snap.snapshot_id
    assert out["verification"]["verified_by"] == "kc_consume"


def test_entry_citation_refuses_wrong_revision(fx):
    mod = _entry_or_fail()
    session, snap, passage, chain = _bound_session(mod, fx)
    # Attacker recomputes a valid digest for an altered revision, but
    # the altered passage is not a registered member.
    wrong_rev = build_routed_passage(
        source_id="fixture_doc_D1", revision="rev-999", locator="P3",
        text=QUOTE, player_uid=A, aliases=["测试甲"],
        title="fixture_doc_D1",
        authorized_url="fixture://authorized-copy/fixture_doc_D1")
    out = session.consume_citation(passage=wrong_rev, source_text=QUOTE,
                                   chain=chain)
    assert out["status"] == "UNKNOWN"
    assert out["verbatim"] is None


def test_entry_citation_refuses_unregistered_source(fx):
    mod = _entry_or_fail()
    session, snap, passage, chain = _bound_session(mod, fx)
    # Valid digest, authorized-looking source, but never registered
    # with this session: not a member of the authorized source set.
    other = build_routed_passage(
        source_id="fixture_doc_D1", revision="rev-1", locator="P3",
        text=QUOTE, player_uid=A, aliases=["测试甲"],
        title="fixture_doc_D1",
        authorized_url="fixture://authorized-copy/fixture_doc_D1")
    session2 = _session(mod, authorized_sources=set())
    session2.bind_current(snap)
    out = session2.consume_citation(passage=other, source_text=QUOTE,
                                    chain=chain)
    assert out["status"] == "UNKNOWN"


# ------------------------------------------------------- RULE D (entry)

def test_entry_answer_claim_conflict_keeps_fact(fx):
    mod = _entry_or_fail()
    session, snap, passage, chain = _bound_session(mod, fx)
    fact_before = copy.deepcopy(fx["fact"])
    conflicting_claim = {
        "claim_id": "fixture_claim_ATTACK", "player_uid": A,
        "date": "2026-10-20", "text": "attacker claim text",
        "team_id": "OTHER_TEAM",
        "claim_only_marker": "ATTACKER_MARKER"}
    out = session.consume_answer(
        records=fx["stats_records"], player_uid=A, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION,
        metric="points_per_game", fact=fx["fact"], passage=passage,
        source_text=QUOTE, chain=chain, claims=[conflicting_claim])
    payload = out["payload"]
    # Authoritative layers byte-identical to their inputs.
    assert payload["grains"]["CANONICAL_FACT"] == fact_before
    assert payload["grains"]["CANONICAL_FACT"]["team_id"] \
        == "fixture_team_T2"
    assert payload["grains"]["STATS"]["value"] == 12.5
    # No claim field anywhere in the authoritative layers.
    assert "OTHER_TEAM" not in str(payload["grains"]["CANONICAL_FACT"])
    assert "ATTACKER_MARKER" not in str(payload["grains"])
    # The conflict is reported ONLY in the claim layer, with the
    # guard's refusal recorded (the integrated path invoked the guard).
    claim_layer = payload["claim_layer"]
    assert claim_layer["status"] == "CONFLICT"
    assert any(r.get("refused") is True for r in claim_layer["refusals"])
    assert "OTHER_TEAM" in str(claim_layer)
    # The caller's fact dict itself was never mutated.
    assert fx["fact"] == fact_before
    assert out["verification"]["verified_by"] == "kc_consume"


def test_entry_answer_injection_text_is_data_only(fx):
    mod = _entry_or_fail()
    session, snap, passage, chain = _bound_session(mod, fx)
    store = SimulatedCurrentStore(target_domain=TARGET)
    store.register(snap)
    store.stage(snap.snapshot_id)
    store.validate()
    store.commit()
    assert store.read_current() == snap.snapshot_id
    # The malicious passage text enters the real consumption chain.
    malicious = [p for p in fx["passages"]
                 if p["source_id"] == "fixture_doc_MALICIOUS"][0]
    session.register_passages([malicious])
    # The malicious source is NOT in the authorized source set, so a
    # citation through the entry is refused; the answer path must
    # still treat the text as data, never execute it, and never change
    # controlled state.
    from cba_kb.kc_passages import evidence_chain
    binding = session.current_binding()
    bad_chain = evidence_chain(
        receipt_id=binding["receipt_binding"],
        snapshot_id=snap.snapshot_id,
        build_version=snap.build_code,
        source_digest=malicious["digest"],
        authorization_basis=binding["authorization_basis"])
    out = session.consume_answer(
        records=fx["stats_records"], player_uid=A, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION,
        metric="points_per_game", fact=fx["fact"], passage=malicious,
        source_text=malicious["text"], chain=bad_chain, claims=[])
    payload = out["payload"]
    assert payload["instruction_handling"]["executed"] is False
    assert payload["instruction_handling"]["action_taken"] == "NONE"
    assert payload["grains"]["DOCUMENT"]["verbatim"] is None
    assert store.read_current() == snap.snapshot_id


# ------------------------------------------------------- RULE E (entry)

def test_entry_render_accepts_verified_artifact(fx):
    mod = _entry_or_fail()
    session = _session(mod)
    out = session.consume_slice(
        fx["stats_records"], player_uid=A, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION)
    rendered = session.render_verified(out, kind="slice_markdown")
    assert "end_marker: true" in rendered
    assert "12.5" in rendered
    rendered_json = session.render_verified(out, kind="slice_json")
    assert "NOT_VERIFIED_PENDING_PHASE_2" in rendered_json


def test_entry_render_refuses_raw_output(fx):
    mod = _entry_or_fail()
    session = _session(mod)
    # Raw primitives produce output with no entry verification record.
    raw_slice = build_slice(
        fx["stats_records"], player_uid=A, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION,
        authorized_ids={A})
    from cba_kb.kc_render import render_markdown_table
    raw_text = render_markdown_table(raw_slice)
    with pytest.raises(mod.KcConsumeError):
        session.render_verified(raw_text, kind="slice_markdown")
    with pytest.raises(mod.KcConsumeError):
        session.render_verified(raw_slice, kind="slice_markdown")
    # A raw citation (no entry gate) is likewise not renderable.
    target = _routed_target(fx)
    raw_cite = cite_verbatim(target, QUOTE)
    with pytest.raises(mod.KcConsumeError):
        session.render_verified(raw_cite, kind="passage")


def test_entry_render_refuses_forged_verification(fx):
    mod = _entry_or_fail()
    session = _session(mod)
    out = session.consume_slice(
        fx["stats_records"], player_uid=A, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION)
    # Attack: copy a genuine verification record onto different content.
    forged_payload = dict(out["payload"])
    forged_payload["metrics"] = dict(out["payload"]["metrics"])
    forged_payload["metrics"]["points_per_game"] = {
        "value": 99.9, "unit": "per_game", "value_kind": "FLOAT",
        "source_record_id": "forged"}
    forged = {"payload": forged_payload,
              "verification": dict(out["verification"])}
    with pytest.raises(mod.KcConsumeError):
        session.render_verified(forged, kind="slice_markdown")


def test_entry_render_refuses_cross_session_artifact(fx):
    mod = _entry_or_fail()
    session = _session(mod)
    other = _session(mod)
    out = session.consume_slice(
        fx["stats_records"], player_uid=A, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION)
    with pytest.raises(mod.KcConsumeError):
        other.render_verified(out, kind="slice_markdown")


def test_entry_render_refuses_partial_catalog(fx):
    mod = _entry_or_fail()
    session = _session(mod)
    # A catalog with a missing slice is PARTIAL and must never render
    # as a trusted complete catalog.
    key_ok = (SEASON, "REGULAR", "WHOLE_SEASON", None)
    # No record exists for 2023-2024 (A's prior season is 2024-2025).
    key_missing = ("2023-2024", "REGULAR", "WHOLE_SEASON", None)
    out = session.consume_catalog(
        fx["stats_records"], player_uid=A,
        expected_keys=[key_ok, key_missing],
        provider=PROVIDER, competition=COMPETITION)
    assert out["payload"]["complete"] is False
    with pytest.raises(Exception):
        session.render_verified(out, kind="catalog")


def test_entry_passage_render_requires_entry_verification(fx):
    mod = _entry_or_fail()
    session, snap, passage, chain = _bound_session(mod, fx)
    verified_passage = session.consume_passage(passage)
    rendered = session.render_verified(verified_passage, kind="passage")
    assert QUOTE in rendered
    assert "end_marker: true" in rendered
    # The same passage object, unwrapped (raw primitive output shape),
    # is refused by the consumer render path.
    with pytest.raises(mod.KcConsumeError):
        session.render_verified(passage, kind="passage")
