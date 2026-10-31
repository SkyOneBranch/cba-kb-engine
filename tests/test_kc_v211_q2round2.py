"""CBA-KB v2.1.1 T2-1 — Q2-227-01..05 concentrated repair tests
(Human-authorized, 2026-10-31).

Falsification labelling (honest, per test):
- Tests marked BEHAVIORAL fail on the 944cf1f code for a business
  reason (the current code returns the wrong status on a real call
  chain), not because an interface or module is missing. They use
  only surfaces that exist on the old HEAD.
- Tests marked PIN pass on both old and new code; they document
  preserved positive capability and are NOT falsification evidence.
- Tests marked NEW-CAPABILITY exercise a new seam (formal source
  binding, now via a validated canonical registry pair per the
  2026-10-31 provenance-binding repair, which supersedes the
  original caller-dict mechanism); on the old HEAD they fail at
  the call boundary, which is expected and is NOT claimed as
  business falsification.

All fixtures are clean-room synthetic. All artifacts SIMULATED.
No Drive, no real data, no P0 claim, no production claim.
"""

import json

import pytest

from cba_kb.kc_cleanroom import A, QUOTE, SEASON, build_cleanroom_fixture
from cba_kb.kc_consume import ConsumeSession, _content_digest
from cba_kb.kc_local_producer import (
    KcLocalProducerError,
    build_local_projection,
    verify_local_projection,
)
from cba_kb.kc_passages import build_routed_passage, evidence_chain
from cba_kb.kc_projection import (
    canonical_json,
    resolve_claims_by_event,
    sha256_hex,
)
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


def _bound_session(fx, passages):
    """Session bound to a snapshot carrying every passage digest,
    with all passages registered. Returns (session, snap, chain)
    keyed to the FIRST passage as the cited passage."""
    session = _session(authorized_sources={p["source_id"]
                                           for p in passages})
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
    snap = build_snapshot(**_snapshot_kwargs(entries=entries))
    session.bind_current(snap)
    session.register_passages(passages)
    binding = session.current_binding()
    chain = evidence_chain(
        receipt_id=binding["receipt_binding"],
        snapshot_id=snap.snapshot_id,
        build_version=snap.build_code,
        source_digest=passages[0]["digest"],
        authorization_basis=binding["authorization_basis"])
    return session, snap, chain


def _passage(source_id, revision, text):
    return build_routed_passage(
        source_id=source_id, revision=revision, locator="P3",
        text=text, player_uid=A, aliases=["测试甲"],
        title=source_id,
        authorized_url="fixture://authorized-copy/" + source_id)


def _fact(source, **over):
    fact = {"fact_id": "q2r2_fact", "player_uid": A, "season": SEASON,
            "team_id": "fixture_team_T2", "kind": "REGISTRATION",
            "source": source}
    fact.update(over)
    return fact


def _answer(session, fx, passage, chain, fact, **kw):
    call = dict(records=fx["stats_records"], player_uid=A, season=SEASON,
                match_type="REGULAR", scope="WHOLE_SEASON",
                provider=PROVIDER, competition=COMPETITION,
                metric="points_per_game", fact=fact,
                passage=passage, source_text=passage["text"],
                chain=chain, claims=[])
    call.update(kw)
    return session.consume_answer(**call)


def _guarantee(snap, fact, passage, **over):
    kw = dict(fact_id=fact["fact_id"],
              fact_version=fact.get("fact_version", "v1"),
              source_id=fact["source"],
              file_version=passage["revision"],
              field_scope=sorted(fact.keys()),
              authorization_domain=TARGET,
              snapshot_id=snap.snapshot_id,
              source_digest=passage["digest"])
    kw.update(over)
    return build_fact_guarantee(**kw)


def _fact_grain(out):
    return out["payload"]["grains"]["CANONICAL_FACT"]


# ------------------------------------------------------- Q2-227-01: Fact

def test_q2r2_fact_versionless_unknown(fx):
    # BEHAVIORAL: a version-less Fact is legacy per §2.1(6) and
    # must stay UNKNOWN even under an otherwise fully valid
    # guarantee — the sidecar's declared "v1" proves nothing about
    # a Fact that carries no version. Old code: Fact OK.
    passage = _passage("fixture_doc_D1", "rev-1", fx["passages"][0]["text"])
    session, snap, chain = _bound_session(fx, [passage])
    fact = _fact("fixture_doc_D1")  # no fact_version
    out = _answer(session, fx, passage, chain, fact,
                  fact_guarantee=_guarantee(snap, fact, passage))
    assert _fact_grain(out).get("status") == "UNKNOWN"
    assert out["payload"]["grains"]["STATS"]["status"] == "OK"
    assert out["payload"]["grains"]["DOCUMENT"]["status"] == "OK"


def test_q2r2_fact_missing_source_digest_unknown(fx):
    # BEHAVIORAL: a guarantee without a source digest used to pass
    # the validator (digest None allowed) and authorize the Fact.
    # The gate now requires a snapshot-bound digest. Old: Fact OK.
    passage = _passage("fixture_doc_D1", "rev-1", fx["passages"][0]["text"])
    session, snap, chain = _bound_session(fx, [passage])
    fact = _fact("fixture_doc_D1", fact_version="v1")
    guarantee = _guarantee(snap, fact, passage, source_digest=None)
    out = _answer(session, fx, passage, chain, fact,
                  fact_guarantee=guarantee)
    assert _fact_grain(out).get("status") == "UNKNOWN"


def test_q2r2_fact_cross_source_wrong_file_version_unknown(fx):
    # BEHAVIORAL: the old gate checked file_version ONLY when the
    # Fact's source equalled the cited passage's source, so a
    # cross-source Fact could carry any declared file_version.
    # The gate now corroborates it against the passage carrying
    # the guarantee's source digest. Old: Fact OK.
    d1 = _passage("fixture_doc_D1", "rev-1", fx["passages"][0]["text"])
    d2 = _passage("fixture_doc_D2", "rev-2", "第二来源文本。")
    session, snap, chain = _bound_session(fx, [d1, d2])
    fact = _fact("fixture_doc_D2", fact_version="v1")
    guarantee = _guarantee(snap, fact, d2, file_version="rev-1")
    out = _answer(session, fx, d1, chain, fact,
                  fact_guarantee=guarantee)
    assert _fact_grain(out).get("status") == "UNKNOWN"


def test_q2r2_fact_cross_source_matching_file_version_ok(fx):
    # PIN: the same cross-source shape with the CORRECT file
    # version (the D2 passage's own revision) still authorizes —
    # the repair removes the bypass, not the capability.
    d1 = _passage("fixture_doc_D1", "rev-1", fx["passages"][0]["text"])
    d2 = _passage("fixture_doc_D2", "rev-2", "第二来源文本。")
    session, snap, chain = _bound_session(fx, [d1, d2])
    fact = _fact("fixture_doc_D2", fact_version="v1")
    out = _answer(session, fx, d1, chain, fact,
                  fact_guarantee=_guarantee(snap, fact, d2))
    assert _fact_grain(out).get("fact_id") == "q2r2_fact"


# ------------------------------------------------------- Q2-227-02: Claim

def _assoc(claim, source_id, revision, locator, digest, player_uid=A):
    return {"event_ref": "event_Q", "evidence": ["src_Q:P1"],
            "source_id": source_id, "revision": revision,
            "locator": locator, "digest": digest,
            "player_uid": player_uid}


def test_q2r2_event_fixed_hex_digest_unknown():
    # BEHAVIORAL: a merely well-formed 64-hex digest is NOT a
    # bound assertion — the digest must recompute over the bound
    # content. Old code: CONFLICT on the fixed string.
    claims = []
    for cid, text in (("q_a", "说法一"), ("q_b", "说法二")):
        claim = {"claim_id": cid, "player_uid": A,
                 "date": "2026-03-10", "text": text,
                 "source_id": "src_Q",
                 "event_association": _assoc(None, "src_Q", "rev-1",
                                             "P1", "f" * 64)}
        claims.append(claim)
    out = resolve_claims_by_event(claims)
    assert out["status"] == "UNKNOWN"
    assert out["conflicting_claim_ids"] == []


def test_q2r2_event_missing_player_uid_unknown():
    # BEHAVIORAL: the UID binding is mandatory, not conditional on
    # the association happening to carry a UID. An association
    # without player_uid cannot bind this claim. Old: CONFLICT
    # (format checks alone passed).
    claims = []
    for cid, text in (("q_a", "说法一"), ("q_b", "说法二")):
        assoc = {"event_ref": "event_Q", "evidence": ["src_Q:P1"],
                 "source_id": "src_Q", "revision": "rev-1",
                 "locator": "P1", "digest": "e" * 64}
        claims.append({"claim_id": cid, "player_uid": A,
                       "date": "2026-03-10", "text": text,
                       "source_id": "src_Q",
                       "event_association": assoc})
    out = resolve_claims_by_event(claims)
    assert out["status"] == "UNKNOWN"
    assert out["conflicting_claim_ids"] == []


def test_q2r2_event_bound_digests_conflict():
    # PIN: properly bound per-claim digests still yield a precise
    # CONFLICT for a shared event with differing values. The
    # digest helper is imported lazily so that, on the old HEAD,
    # this positive test fails at the import boundary (a new
    # seam) rather than masquerading as business falsification.
    from cba_kb.kc_projection import event_association_digest
    claims = []
    for cid, text in (("q_a", "说法一"), ("q_b", "说法二")):
        claim = {"claim_id": cid, "player_uid": A,
                 "date": "2026-03-10", "text": text,
                 "source_id": "src_Q"}
        assoc = _assoc(claim, "src_Q", "rev-1", "P1", "")
        assoc["digest"] = event_association_digest(claim, assoc)
        claim["event_association"] = assoc
        claims.append(claim)
    out = resolve_claims_by_event(claims)
    assert out["status"] == "CONFLICT"
    assert out["conflicting_claim_ids"] == ["q_a", "q_b"]


# ------------------------------------------------------- Q2-227-03: OK kept

def test_q2r2_event_distinct_trusted_refs_ok():
    # PIN: proven-no-conflict OK is preserved when every claim is
    # trusted under a pairwise-distinct event ref. Lazy import
    # for the same reason as test_q2r2_event_bound_digests_conflict.
    from cba_kb.kc_projection import event_association_digest
    claims = []
    for cid, ref, ev, text in (("q_a", "event_1", "src_Q:P1", "说法一"),
                               ("q_b", "event_2", "src_Q:P2", "说法二")):
        claim = {"claim_id": cid, "player_uid": A,
                 "date": "2026-03-10", "text": text,
                 "source_id": "src_Q"}
        assoc = {"event_ref": ref, "evidence": [ev],
                 "source_id": "src_Q", "revision": "rev-1",
                 "locator": "P1", "player_uid": A, "digest": ""}
        assoc["digest"] = event_association_digest(claim, assoc)
        claim["event_association"] = assoc
        claims.append(claim)
    out = resolve_claims_by_event(claims)
    assert out["status"] == "OK"
    assert out["conflicting_claim_ids"] == []


# ------------------------------------------------------- Q2-227-04: Identity

def _old_rule_digest():
    return sha256_hex(canonical_json({
        "source_id": "fixture_canonical_registry",
        "revision": "rev-1", "target_domain": TARGET}))


def _identity(session, fx, **kw):
    call = dict(players=fx["players"], aliases=fx["aliases"],
                query="测试甲", query_kind="alias",
                all_pages_received=True)
    call.update(kw)
    return session.consume_identity(**call)


def test_q2r2_identity_old_rule_digest_unknown(fx):
    # BEHAVIORAL: a digest recomputed from the triple alone used
    # to pass — it binds no identity content, so one digest
    # covered any payload. Old code: RESOLVED.
    session = _session()
    out = _identity(session, fx, identity_source={
        "source_id": "fixture_canonical_registry", "revision": "rev-1",
        "digest": _old_rule_digest()})
    assert out["status"] == "UNKNOWN"
    assert out["candidates"] == []


def test_q2r2_identity_replayed_digest_unknown(fx):
    # BEHAVIORAL: the same triple-only digest presented with
    # TAMPERED identity content (a changed canonical name) must
    # not resolve — the digest must bind the submitted content.
    # Old code: RESOLVED (content never entered the digest).
    session = _session()
    players = [dict(p) for p in fx["players"]]
    players[0] = dict(players[0], canonical_name="被篡改名")
    out = _identity(session, fx, players=players, identity_source={
        "source_id": "fixture_canonical_registry", "revision": "rev-1",
        "digest": _old_rule_digest()})
    assert out["status"] == "UNKNOWN"
    assert out["candidates"] == []


# ------------------------------------------------------- Q2-227-05: Producer

def _eligible_fixture(fx):
    fact = {"fact_id": "eligible_fact", "fact_version": "v1",
            "player_uid": A, "season": SEASON,
            "team_id": "fixture_team_T2", "kind": "REGISTRATION",
            "source": "fixture_doc_D1"}
    return {"players": fx["players"], "aliases": fx["aliases"],
            "stats_records": fx["stats_records"], "fact": fact,
            "claims": [], "passages": [fx["passages"][0]],
            "source_objects": []}


def _formal_registry_for(passage, content_hash=None):
    # A validated canonical registry + manifest pair whose
    # canonical route is the passage's own source and whose
    # manifest row carries the passage's digest. This pair is a
    # SEPARATE artifact from the passage: the producer validates
    # it whole and checks the passage digest against its manifest
    # row. (Provenance-binding repair, Human-authorized
    # 2026-10-31: the previous caller {revision, digest} dict
    # taken from the passage itself was mutual consistency, not
    # formal proof, and no longer confers formal status.)
    return {
        "registry": {
            "registry_version": "1",
            "registry_release_id": "v-test-1",
            "products": [{
                "product_id": "doc_d1", "semantic_grain": "document",
                "authority": "canonical",
                "business_key": ["source_id"],
                "artifact_key": "fixture_doc_D1",
                "generated_from": [], "deprecated_by": None,
                "read_policy": "canonical_only",
                "current_eligible": True,
                "independent_write_allowed": True}]},
        "manifest": [{
            "uid": "fixture_doc_D1", "drive_file_id": "file-D1",
            "content_hash": passage["digest"]
            if content_hash is None else content_hash}]}


def test_q2r2_producer_formal_path(fx, tmp_path):
    # NEW-CAPABILITY (mechanism replaced 2026-10-31 by the
    # provenance-binding repair): with a validated canonical
    # registry pair whose manifest row matches the passage
    # digest, the passage is formally bound, the manifest says
    # so, and the answer Fact is reported as formally sourced.
    passage = fx["passages"][0]
    out = build_local_projection(
        fixture=_eligible_fixture(fx), output_dir=str(tmp_path),
        formal_registry=_formal_registry_for(passage))
    assert verify_local_projection(str(tmp_path))["valid"] is True
    assert out["answer_fact_status"] == "OK"
    assert out["answer_fact_guaranteed"] is True
    assert out["answer_fact_formally_sourced"] is True
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["input_provenance"]["mode"] == "formal"
    assert manifest["input_provenance"]["formal_sources"] == \
        ["fixture_doc_D1"]
    assert manifest["answer_fact"]["formally_sourced"] is True
    assert manifest["input_provenance"]["formal_registry"][
        "registry_release_id"] == "v-test-1"


def test_q2r2_producer_synthetic_path_labelled(fx, tmp_path):
    # NEW-CAPABILITY: without a registry pair the same eligible
    # run still works (sidecar coverage intact) but is labelled
    # synthetic-only and never claims formal sourcing.
    out = build_local_projection(
        fixture=_eligible_fixture(fx), output_dir=str(tmp_path))
    assert verify_local_projection(str(tmp_path))["valid"] is True
    assert out["answer_fact_status"] == "OK"
    assert out["answer_fact_guaranteed"] is True
    assert out["answer_fact_formally_sourced"] is False
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["input_provenance"]["mode"] == "synthetic-only"
    assert manifest["input_provenance"]["synthetic_sources"] == \
        ["fixture_doc_D1"]


def test_q2r2_producer_binding_mismatch_raises(fx, tmp_path):
    # NEW-CAPABILITY: a registry route whose manifest row does
    # NOT match the passage digest is a fail-closed build
    # failure, never a silent downgrade to synthetic.
    passage = fx["passages"][0]
    with pytest.raises(KcLocalProducerError):
        build_local_projection(
            fixture=_eligible_fixture(fx), output_dir=str(tmp_path),
            formal_registry=_formal_registry_for(
                passage, content_hash="0" * 64))
