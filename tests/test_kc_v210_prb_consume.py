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

Directed repair (round-3 FAIL response): the tests appended at the end
of this file are BEHAVIORAL counterexamples — each fails on the
9a850953 code because the current code returns, wraps, or refuses the
wrong thing on a real call chain (a trusted citation for a
non-member passage, a wrapped catalog for an authorized-but-empty
player, a copied unverified Fact, a binding that follows a mutated
caller object, a rendered passage from a passage-only wrapper).
The answer-render end-to-end test is a PIN (it passes pre- and
post-fix) and is labelled as such. The _bound_session fixture was
corrected so the bound snapshot actually contains the cited Document
source; the updated passage-render test reflects the corrected
contract (passage rendering requires citation verification).
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
    registered. Returns (session, snapshot, passage, chain).

    Fixture correction (directed repair): the bound snapshot's entries
    now actually contain the cited Document source (entry-doc-D1 with
    the passage digest), so the snapshot-bound manifest includes it.
    The previous fixture bound a stats-only snapshot while citing an
    independently registered Document passage — that encoded the
    round-3 membership bug, not a valid positive case."""
    session = _session(mod)
    passage = _routed_target(fx)
    # Directed repair 2 (round-4 FAIL response): the stats entry
    # digest must bind the actual WHOLE_SEASON slice payload the
    # answer path computes, so the slice justifying a Fact is
    # snapshot-bound rather than caller-supplied. The digest is
    # computed with the entry's own content-digest function over a
    # probe slice for the exact answer query; the probe session is
    # unbound, so this binds content, not session state.
    probe = _session(mod)
    probed = probe.consume_slice(
        fx["stats_records"], player_uid=A, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION)
    slice_digest = mod._content_digest(probed["payload"])
    stats_entry = {"entry_id": "entry-stats", "sha256": slice_digest,
                   "authority": "canonical", "rights": "public",
                   "source_id": "fixture_stats"}
    doc_entry = {"entry_id": "entry-doc-D1", "sha256": passage["digest"],
                 "authority": "canonical", "rights": "public",
                 "source_id": "fixture_doc_D1"}
    snap = build_snapshot(**_snapshot_kwargs(
        entries=[stats_entry, doc_entry]))
    session.bind_current(snap)
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
    # Demanding case (directed repair): an AUTHORIZED player with no
    # verifiable record for any expected key must return the identical
    # uniform result — not a wrapped catalog with a missing list.
    missing_key = ("2023-2024", "REGULAR", "WHOLE_SEASON", None)
    authorized_missing = session.consume_catalog(
        fx["stats_records"], player_uid=A,
        expected_keys=[missing_key], provider=PROVIDER,
        competition=COMPETITION)
    assert authorized_missing == unauthorized
    assert authorized_missing["status"] == "UNKNOWN"


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
    # with this session — not a member of the authorized source set.
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
    # Contract correction (directed repair 2): the positive Fact
    # must carry a snapshot-bound authorized source. The fixture
    # Fact's URI source ("fixture://canonical-registration") is
    # absent from the bound snapshot's manifest — accepting it as
    # authoritative was exactly the R3-NB3 gap. The corrected input
    # keeps every other field (including team_id) identical, so the
    # byte-identity, team_id, and claim-separation properties below
    # are preserved, not weakened; the unmodified fixture Fact is
    # asserted untouched at the end.
    bound_fact = dict(fx["fact"], source="fixture_doc_D1")
    fact_before = copy.deepcopy(bound_fact)
    conflicting_claim = {
        "claim_id": "fixture_claim_ATTACK", "player_uid": A,
        "date": "2026-10-20", "text": "attacker claim text",
        "team_id": "OTHER_TEAM",
        "claim_only_marker": "ATTACKER_MARKER"}
    out = session.consume_answer(
        records=fx["stats_records"], player_uid=A, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION,
        metric="points_per_game", fact=bound_fact, passage=passage,
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
    assert bound_fact == fact_before
    assert fx["fact"]["source"] == "fixture://canonical-registration"
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
    # Contract correction (directed repair): passage rendering
    # requires CITATION verification, not a consume_passage wrapper
    # alone. A citation-verified artifact renders; a passage-only
    # wrapper is refused for kind="passage".
    cited = session.consume_citation(passage=passage, source_text=QUOTE,
                                     chain=chain)
    assert cited["payload"]["status"] == "OK"
    rendered = session.render_verified(cited, kind="passage")
    assert QUOTE in rendered
    assert "end_marker: true" in rendered
    verified_passage = session.consume_passage(passage)
    with pytest.raises(mod.KcConsumeError):
        session.render_verified(verified_passage, kind="passage")
    # The same passage object, unwrapped (raw primitive output shape),
    # is refused by the consumer render path.
    with pytest.raises(mod.KcConsumeError):
        session.render_verified(passage, kind="passage")


# ------------------------------------------------------- DIRECTED REPAIR
# (round-3 FAIL response — behavioral counterexamples, one per defect)

def _answer_args(mod, fx, session, snap, passage, chain, fact, claims):
    return dict(records=fx["stats_records"], player_uid=A,
                season=SEASON, match_type="REGULAR",
                scope="WHOLE_SEASON", provider=PROVIDER,
                competition=COMPETITION, metric="points_per_game",
                fact=fact, passage=passage, source_text=QUOTE,
                chain=chain, claims=claims)


def test_directed_citation_requires_snapshot_membership(fx):
    mod = _entry_or_fail()
    session, snap, passage, chain = _bound_session(mod, fx)
    # Outsider: digest-valid and source-authorized in THIS session,
    # but absent from the bound snapshot's entries. Registration
    # succeeds — that alone must not confer trusted status.
    outsider_session = _session(
        mod, authorized_sources={"fixture_doc_D1",
                                 "fixture_doc_OUTSIDER"})
    outsider_session.bind_current(snap)
    outsider = build_routed_passage(
        source_id="fixture_doc_OUTSIDER", revision="rev-1", locator="P9",
        text=QUOTE, player_uid=A, aliases=["测试甲"],
        title="outsider",
        authorized_url="fixture://authorized-copy/fixture_doc_OUTSIDER")
    assert outsider_session.register_passages([outsider]) == 1
    from cba_kb.kc_passages import evidence_chain
    binding = outsider_session.current_binding()
    outsider_chain = evidence_chain(
        receipt_id=binding["receipt_binding"],
        snapshot_id=snap.snapshot_id,
        build_version=snap.build_code,
        source_digest=outsider["digest"],
        authorization_basis=binding["authorization_basis"])
    cited = outsider_session.consume_citation(
        passage=outsider, source_text=QUOTE, chain=outsider_chain)
    assert cited == dict(mod.UNIFORM_CITATION_UNKNOWN)
    passed = outsider_session.consume_passage(outsider)
    assert passed == dict(mod.UNIFORM_CITATION_UNKNOWN)
    with pytest.raises(mod.KcConsumeError):
        outsider_session.render_verified(passed, kind="passage")


def test_directed_passage_failures_uniform(fx):
    mod = _entry_or_fail()
    session, snap, passage, chain = _bound_session(mod, fx)
    unknown = dict(mod.UNIFORM_CITATION_UNKNOWN)
    # (a) Valid digest, authorized-looking source, never registered.
    unregistered = build_routed_passage(
        source_id="fixture_doc_D1", revision="rev-2", locator="P3",
        text=QUOTE, player_uid=A, aliases=["测试甲"],
        title="fixture_doc_D1",
        authorized_url="fixture://authorized-copy/fixture_doc_D1")
    # (b) Registered while authorized, then authorization removed:
    # the source-authorization failure cause, distinctly.
    session2 = _session(
        mod, authorized_sources={"fixture_doc_D1",
                                 "fixture_doc_OUTSIDER"})
    session2.bind_current(snap)
    outsider = build_routed_passage(
        source_id="fixture_doc_OUTSIDER", revision="rev-1", locator="P9",
        text=QUOTE, player_uid=A, aliases=["测试甲"],
        title="outsider",
        authorized_url="fixture://authorized-copy/fixture_doc_OUTSIDER")
    assert session2.register_passages([outsider]) == 1
    session2.authorized_sources.discard("fixture_doc_OUTSIDER")
    # (c) Citation with a forged chain: already uniform pre-fix (pin
    # within this test); the passage causes above are the new behavior.
    from cba_kb.kc_passages import evidence_chain
    forged = evidence_chain(
        receipt_id="forged-receipt", snapshot_id=snap.snapshot_id,
        build_version=snap.build_code, source_digest=passage["digest"],
        authorization_basis="SIMULATED_TARGET_SCOPE")
    results = [
        session.consume_passage(unregistered),
        session2.consume_passage(outsider),
        session.consume_citation(passage=passage, source_text=QUOTE,
                                 chain=forged),
    ]
    assert all(r == unknown for r in results)
    # No distinguishing diagnostic leaks into any consumer result.
    assert "PASSAGE_NOT_REGISTERED_MEMBER" not in str(results)
    assert "PASSAGE_SOURCE_NOT_AUTHORIZED" not in str(results)


def test_directed_answer_rejects_unbound_fact_uid(fx):
    mod = _entry_or_fail()
    session, snap, passage, chain = _bound_session(mod, fx)
    bad_fact = dict(fx["fact"], player_uid="fixture_player_UNAUTHORIZED")
    out = session.consume_answer(
        **_answer_args(mod, fx, session, snap, passage, chain,
                       bad_fact, []))
    assert out["payload"]["grains"]["CANONICAL_FACT"] == \
        dict(mod.UNIFORM_NOT_FOUND)


def test_directed_answer_rejects_fact_season_mismatch(fx):
    mod = _entry_or_fail()
    session, snap, passage, chain = _bound_session(mod, fx)
    bad_fact = dict(fx["fact"], season="2023-2024")
    out = session.consume_answer(
        **_answer_args(mod, fx, session, snap, passage, chain,
                       bad_fact, []))
    assert out["payload"]["grains"]["CANONICAL_FACT"] == \
        dict(mod.UNIFORM_NOT_FOUND)


def test_directed_answer_rejects_fact_without_verified_slice(fx):
    mod = _entry_or_fail()
    session, snap, passage, chain = _bound_session(mod, fx)
    norec = "fixture_player_NOREC"
    session2 = _session(mod, authorized_players={A, norec})
    session2.bind_current(snap)
    session2.register_passages([passage])
    fact_norec = {"fact_id": "fixture_fact_NOREC", "player_uid": norec,
                  "season": SEASON, "team_id": "fixture_team_T2",
                  "kind": "REGISTRATION",
                  "source": "fixture://canonical-registration"}
    out = session2.consume_answer(
        records=fx["stats_records"], player_uid=norec, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION,
        metric="points_per_game", fact=fact_norec, passage=passage,
        source_text=QUOTE, chain=chain, claims=[])
    assert out["payload"]["grains"]["CANONICAL_FACT"] == \
        dict(mod.UNIFORM_NOT_FOUND)


def test_directed_binding_ignores_caller_mutation(fx):
    mod = _entry_or_fail()
    session = _session(mod)
    snap = build_snapshot(**_snapshot_kwargs())
    snap_d = _snapshot_dict_of(snap)
    session.bind_current(snap_d)
    original_sid = snap.snapshot_id
    # Attack: mutate the caller's dict AFTER binding.
    snap_d["snapshot_id"] = "kc-snapshot:attacker"
    binding = session.current_binding()
    assert binding["snapshot_id"] == original_sid
    assert session._store.read_current() == original_sid


def test_directed_store_swap_clears_binding(fx):
    mod = _entry_or_fail()
    session, snap, passage, chain = _bound_session(mod, fx)
    assert session.current_binding()["snapshot_id"] == snap.snapshot_id
    # Attack: swap in a different store after binding. The old
    # binding must not survive detached from committed state.
    session.attach_store(SimulatedCurrentStore(target_domain=TARGET))
    with pytest.raises(mod.KcConsumeError):
        session.current_binding()


def test_directed_commit_refuses_registry_mapping_replacement():
    snap = build_snapshot(**_snapshot_kwargs())
    store = SimulatedCurrentStore(target_domain=TARGET)
    snap_d = _snapshot_dict_of(snap)
    store.register(snap_d)
    store.stage(snap.snapshot_id)
    store.validate()
    assert store.state == "VALIDATED"
    # Attack: replace the registry mapping object itself (same
    # content, new identity) after validation.
    store._registry = {snap.snapshot_id: _snapshot_dict_of(snap)}
    with pytest.raises(KcCurrentError):
        store.commit()
    assert store.state == "FAILED"


def test_directed_render_refuses_kind_mismatch(fx):
    mod = _entry_or_fail()
    session, snap, passage, chain = _bound_session(mod, fx)
    sliced = session.consume_slice(
        fx["stats_records"], player_uid=A, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION)
    with pytest.raises(mod.KcConsumeError):
        session.render_verified(sliced, kind="passage")
    cited = session.consume_citation(passage=passage, source_text=QUOTE,
                                     chain=chain)
    assert cited["payload"]["status"] == "OK"
    with pytest.raises(mod.KcConsumeError):
        session.render_verified(cited, kind="slice_markdown")


def test_directed_answer_renders_end_to_end(fx):
    # PIN (passes pre- and post-fix): a full consume_answer artifact
    # renders through kind="answer". Not falsification evidence.
    mod = _entry_or_fail()
    session, snap, passage, chain = _bound_session(mod, fx)
    out = session.consume_answer(
        **_answer_args(mod, fx, session, snap, passage, chain,
                       fx["fact"], []))
    rendered = session.render_verified(out, kind="answer")
    assert "CANONICAL_FACT" in rendered
    assert "end_marker" in rendered


# ------------------------------------------------------- DIRECTED REPAIR 2
# (round-4 FAIL response — behavioral counterexamples, one per
# residual defect; each fails on the 77cc0cb5 code because the
# current code returns, wraps, or renders the wrong thing on a real
# call chain: a fabricated-source Fact copied as authoritative, a
# Fact justified by a slice whose digest is absent from the bound
# snapshot, and a verified artifact rendered after revocation.)

def _bound_fact(fx):
    """The positive answer Fact with a snapshot-bound authorized
    source (see the contract correction on
    test_entry_answer_claim_conflict_keeps_fact)."""
    return dict(fx["fact"], source="fixture_doc_D1")


def test_directed2_answer_rejects_fabricated_source(fx):
    mod = _entry_or_fail()
    session, snap, passage, chain = _bound_session(mod, fx)
    # Attack: UID, season, Stats records, and committed snapshot all
    # unchanged; only the Fact's source is a fabricated truthy value.
    # Presence-only source validation accepts it pre-fix.
    bad_fact = dict(fx["fact"], source="fixture://fabricated-attacker")
    out = session.consume_answer(
        **_answer_args(mod, fx, session, snap, passage, chain,
                       bad_fact, []))
    assert out["payload"]["grains"]["CANONICAL_FACT"] == \
        dict(mod.UNIFORM_NOT_FOUND)


def test_directed2_answer_rejects_fabricated_source_and_changed_field(fx):
    mod = _entry_or_fail()
    session, snap, passage, chain = _bound_session(mod, fx)
    # Round-4 counterexample, verbatim: fabricated source AND a
    # changed authoritative field, UID and season retained.
    bad_fact = dict(fx["fact"], source="fixture://fabricated-attacker",
                    team_id="OTHER_TEAM")
    out = session.consume_answer(
        **_answer_args(mod, fx, session, snap, passage, chain,
                       bad_fact, []))
    assert out["payload"]["grains"]["CANONICAL_FACT"] == \
        dict(mod.UNIFORM_NOT_FOUND)


def test_directed2_answer_rejects_unbound_slice_records(fx):
    mod = _entry_or_fail()
    session, snap, passage, chain = _bound_session(mod, fx)
    # Attack: the caller supplies different Stats records, so the
    # justifying slice's digest is absent from the bound snapshot —
    # yet the Fact (bound source, matching UID/season) would still be
    # copied pre-fix because the slice is merely "OK", not bound.
    records = copy.deepcopy(fx["stats_records"])
    for record in records:
        if record["player_uid"] == A and record["season"] == SEASON \
                and record["match_type"] == "REGULAR" \
                and record["scope"] == "WHOLE_SEASON":
            record["metrics"]["games_played"] = 41
    out = session.consume_answer(
        records=records, player_uid=A, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION,
        metric="points_per_game", fact=_bound_fact(fx),
        passage=passage, source_text=QUOTE, chain=chain, claims=[])
    # The attacker slice IS computed from the supplied records
    # (games_played 41, not the snapshot-bound 40), but the Fact
    # must not ride on an unbound slice.
    attacker_slice = session.consume_slice(
        records, player_uid=A, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION)
    assert attacker_slice["payload"]["metrics"]["games_played"] \
        ["value"] == 41
    assert out["payload"]["grains"]["CANONICAL_FACT"] == \
        dict(mod.UNIFORM_NOT_FOUND)


def test_directed2_render_refuses_revoked_passage_source(fx):
    mod = _entry_or_fail()
    session, snap, passage, chain = _bound_session(mod, fx)
    cited = session.consume_citation(passage=passage, source_text=QUOTE,
                                     chain=chain)
    assert cited["payload"]["status"] == "OK"
    # Attack: revoke the source AFTER verification. Digest, kind,
    # session, and snapshot membership are all unchanged — only the
    # current authorization set moved, and rendering must observe it.
    session.authorized_sources.discard("fixture_doc_D1")
    with pytest.raises(mod.KcConsumeError):
        session.render_verified(cited, kind="passage")


def test_directed2_render_refuses_revoked_slice_player(fx):
    mod = _entry_or_fail()
    session = _session(mod)
    out = session.consume_slice(
        fx["stats_records"], player_uid=A, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION)
    session.authorized_players.discard(A)
    with pytest.raises(mod.KcConsumeError):
        session.render_verified(out, kind="slice_markdown")
    with pytest.raises(mod.KcConsumeError):
        session.render_verified(out, kind="slice_json")


def test_directed2_render_refuses_revoked_catalog_player(fx):
    mod = _entry_or_fail()
    session = _session(mod)
    key = (SEASON, "REGULAR", "WHOLE_SEASON", None)
    out = session.consume_catalog(
        fx["stats_records"], player_uid=A, expected_keys=[key],
        provider=PROVIDER, competition=COMPETITION)
    assert out["payload"]["complete"] is True
    session.authorized_players.discard(A)
    with pytest.raises(mod.KcConsumeError):
        session.render_verified(out, kind="catalog")


def test_directed2_render_refuses_revoked_answer_source(fx):
    mod = _entry_or_fail()
    session, snap, passage, chain = _bound_session(mod, fx)
    out = session.consume_answer(
        **_answer_args(mod, fx, session, snap, passage, chain,
                       _bound_fact(fx), []))
    assert out["payload"]["grains"]["DOCUMENT"]["status"] == "OK"
    session.authorized_sources.discard("fixture_doc_D1")
    with pytest.raises(mod.KcConsumeError):
        session.render_verified(out, kind="answer")


def test_directed2_render_refuses_revoked_answer_player(fx):
    mod = _entry_or_fail()
    session, snap, passage, chain = _bound_session(mod, fx)
    out = session.consume_answer(
        **_answer_args(mod, fx, session, snap, passage, chain,
                       _bound_fact(fx), []))
    assert out["payload"]["grains"]["CANONICAL_FACT"]["player_uid"] == A
    session.authorized_players.discard(A)
    with pytest.raises(mod.KcConsumeError):
        session.render_verified(out, kind="answer")
