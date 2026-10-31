"""CBA-KB v2.1.1 T2-1 — provenance-binding repair tests
(Human-authorized, 2026-10-31; Q2 Gate entry, NOT a Q2 itself).

The defect under repair: a formal source claim was provable by
mutual consistency alone. The producer accepted a caller dict of
{revision, digest} copied from the passage itself, and a claim
association was trusted on a recomputed digest over content the
caller invented. Both are self-consistency, not formal proof.

The repair (no new registry, Truth Store, or trust framework):
- Producer: formal status requires a SEPARATELY supplied,
  validated canonical registry + manifest pair (existing
  canonical_registry validation), a canonical route for the
  passage's source, and a passage digest — recomputed from
  content — equal to the manifest row's content_hash.
- Consumer claim path: a trusted association additionally
  requires a passage anchor — its (source, revision, locator)
  must match a digest-validated registered passage.

Falsification labelling (honest, per test):
- BEHAVIORAL tests fail on the ee887943 code for a business
  reason (the old code returns the wrong status on a real call
  chain). Only the unanchored-claim test qualifies: it uses
  only surfaces that exist on the old HEAD.
- NEW-CAPABILITY tests exercise the new formal_registry seam;
  on the old HEAD they fail at the call boundary (unknown
  keyword), which is expected and is NOT claimed as business
  falsification. The old producer attack itself — a passage
  plus a {revision, digest} dict copied from it yielding
  formal — is evidenced by the previous q2round2 formal test,
  which passed on the old code through that dict path.
- PIN tests document preserved positive capability.

All fixtures are clean-room synthetic. All artifacts SIMULATED.
No Drive, no real data, no P0 claim, no production claim.
"""

import json

import pytest

from cba_kb.kc_cleanroom import A, SEASON, build_cleanroom_fixture
from cba_kb.kc_consume import ConsumeSession, _content_digest
from cba_kb.kc_local_producer import (
    KcLocalProducerError,
    build_local_projection,
    verify_local_projection,
)
from cba_kb.kc_passages import build_routed_passage, evidence_chain
from cba_kb.kc_projection import event_association_digest
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


def _bound_session(fx, passages, register=True):
    """Session bound to a snapshot carrying the passages' entries.
    When register=False the passages are snapshot members but NOT
    registered — the exact shape of an unanchored association."""
    # Event-identity repair (2026-10-31): authorize every
    # passage source, so multi-source claim tests can resolve
    # each claim's evidence against a registered passage.
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


def _registry_pair(artifact_key, content_hash, release_id="v-test-1",
                   read_policy="canonical_only"):
    return {
        "registry": {
            "registry_version": "1",
            "registry_release_id": release_id,
            "products": [{
                "product_id": "doc", "semantic_grain": "document",
                "authority": "canonical",
                "business_key": ["source_id"],
                "artifact_key": artifact_key,
                "generated_from": [], "deprecated_by": None,
                "read_policy": read_policy,
                "current_eligible": True,
                "independent_write_allowed": True}]},
        "manifest": [{
            "uid": artifact_key, "drive_file_id": "file-" + artifact_key,
            "content_hash": content_hash}]}


def _eligible_fixture(fx):
    fact = {"fact_id": "prov_fact", "fact_version": "v1",
            "player_uid": A, "season": SEASON,
            "team_id": "fixture_team_T2", "kind": "REGISTRATION",
            "source": "fixture_doc_D1"}
    return {"players": fx["players"], "aliases": fx["aliases"],
            "stats_records": fx["stats_records"], "fact": fact,
            "claims": [], "passages": [fx["passages"][0]],
            "source_objects": []}


# ------------------------------------------------------- Producer

def test_provenance_self_made_pair_without_route_is_synthetic(
        fx, tmp_path):
    # COUNTEREXAMPLE 1 (NEW-CAPABILITY): the old attack was a
    # passage plus a {revision, digest} pair copied from the
    # passage itself, which the old code upgraded to formal.
    # Under the repair there is no such path: a registry pair
    # that does not contain a canonical route for the passage's
    # source leaves the passage synthetic, however well its
    # manifest row matches some other artifact.
    passage = fx["passages"][0]
    out = build_local_projection(
        fixture=_eligible_fixture(fx), output_dir=str(tmp_path),
        formal_registry=_registry_pair(
            "some_other_artifact", passage["digest"]))
    assert verify_local_projection(str(tmp_path))["valid"] is True
    assert out["answer_fact_status"] == "OK"
    assert out["answer_fact_formally_sourced"] is False
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["input_provenance"]["mode"] == "synthetic-only"
    assert manifest["input_provenance"]["formal_sources"] == []


def test_provenance_forged_registry_invalid_raises(fx, tmp_path):
    # NEW-CAPABILITY: the registry pair is validated whole, not
    # rubber-stamped. A canonical product with a non-canonical
    # read policy fails registry validation, so the build fails
    # instead of labelling the passage canonical.
    passage = fx["passages"][0]
    with pytest.raises(KcLocalProducerError):
        build_local_projection(
            fixture=_eligible_fixture(fx), output_dir=str(tmp_path),
            formal_registry=_registry_pair(
                "fixture_doc_D1", passage["digest"],
                read_policy="derived_read_only"))


def test_provenance_tampered_passage_digest_raises(fx, tmp_path):
    # NEW-CAPABILITY: the manifest row must match a digest that
    # recomputes from the passage content. A passage whose
    # digest field was overwritten to match the manifest row
    # without matching its content fails digest validation.
    fixture = _eligible_fixture(fx)
    passage = dict(fixture["passages"][0])
    passage["text"] = "被篡改的文本。"
    fixture["passages"] = [passage]
    with pytest.raises(KcLocalProducerError):
        build_local_projection(
            fixture=fixture, output_dir=str(tmp_path),
            formal_registry=_registry_pair(
                "fixture_doc_D1", passage["digest"]))


# ------------------------------------------------------- Claim path

def _claims_with_assoc(snap, passage, texts, refs):
    claims = []
    for cid, text, ref in zip(("c1", "c2"), texts, refs):
        claim = {"claim_id": cid, "player_uid": A,
                 "date": "2026-03-10", "text": text,
                 "source_id": "fixture_doc_D1"}
        assoc = {"event_ref": ref,
                 "evidence": ["fixture_doc_D1:P3"],
                 "source_id": "fixture_doc_D1", "revision": "rev-1",
                 "locator": "P3", "player_uid": A,
                 "snapshot_id": snap.snapshot_id, "digest": ""}
        assoc["digest"] = event_association_digest(claim, assoc)
        claim["event_association"] = assoc
        claims.append(claim)
    return claims


def _answer(session, fx, passage, chain, claims):
    fact = {"fact_id": "prov_fact", "fact_version": "v1",
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


def test_provenance_claim_unanchored_unknown(fx):
    # COUNTEREXAMPLE 2 (BEHAVIORAL): a fully self-consistent
    # association — recomputed digest, authorized source,
    # snapshot member, matching snapshot id — whose source
    # passage was never registered is content binding, not a
    # formal association. Differing values must be UNKNOWN,
    # never a CONFLICT built on an unanchored assertion.
    # Old code: CONFLICT.
    # The passage text DOES state the event, so the missing
    # registration is the only reason this is UNKNOWN.
    passage = _passage("fixture_doc_D1", "rev-1", "P3",
                       fx["passages"][0]["text"] + " 事件 event_Q。")
    session, snap, chain = _bound_session(fx, [passage],
                                          register=False)
    claims = _claims_with_assoc(
        snap, passage, ("事件发生在 2026-03-10", "事件发生在 2026-03-15"),
        ("event_Q", "event_Q"))
    out = _answer(session, fx, passage, chain, claims)
    layer = out["payload"]["claim_layer"]
    assert layer["status"] == "UNKNOWN"
    assert layer["conflicting_claim_ids"] == []


def test_provenance_claim_anchored_conflict(fx):
    # PIN: the same shape with the source passage registered
    # (digest-validated) keeps its precise CONFLICT — the
    # repair removes the bypass, not the capability.
    # Event-identity repair (2026-10-31): the passage text
    # now states the claimed event, so the association is
    # content-backed, not merely triple-anchored.
    passage = _passage("fixture_doc_D1", "rev-1", "P3",
                       fx["passages"][0]["text"] + " 事件 event_Q。")
    session, snap, chain = _bound_session(fx, [passage],
                                          register=True)
    claims = _claims_with_assoc(
        snap, passage, ("事件发生在 2026-03-10", "事件发生在 2026-03-15"),
        ("event_Q", "event_Q"))
    out = _answer(session, fx, passage, chain, claims)
    layer = out["payload"]["claim_layer"]
    assert layer["status"] == "CONFLICT"
    assert layer["conflicting_claim_ids"] == ["c1", "c2"]


def test_provenance_claim_anchored_distinct_refs_ok(fx):
    # PIN, rewritten for the event-identity repair
    # (2026-10-31, Q2 H02): distinctness is proven by distinct
    # CONTENT-BACKED associations, never by string inequality
    # alone. Each claim anchors to its own registered passage
    # whose text states its own event_ref, with disjoint
    # resolving evidence — so OK is a proven no-conflict, not
    # a "no shared strings" inference.
    passage = _passage("fixture_doc_D1", "rev-1", "P3",
                       fx["passages"][0]["text"] + " 事件 event_1。")
    passage2 = _passage("fixture_doc_D2", "rev-1", "P9",
                        "第二来源文本。事件 event_2。")
    session, snap, chain = _bound_session(fx, [passage, passage2],
                                          register=True)
    claims = []
    for cid, text, ref, src, loc, ev in (
            ("c1", "事件发生在 2026-03-10", "event_1",
             "fixture_doc_D1", "P3", ["fixture_doc_D1:P3"]),
            ("c2", "事件发生在 2026-03-15", "event_2",
             "fixture_doc_D2", "P9", ["fixture_doc_D2:P9"])):
        claim = {"claim_id": cid, "player_uid": A,
                 "date": "2026-03-10", "text": text,
                 "source_id": src}
        assoc = {"event_ref": ref, "evidence": ev,
                 "source_id": src, "revision": "rev-1",
                 "locator": loc, "player_uid": A,
                 "snapshot_id": snap.snapshot_id, "digest": ""}
        assoc["digest"] = event_association_digest(claim, assoc)
        claim["event_association"] = assoc
        claims.append(claim)
    out = _answer(session, fx, passage, chain, claims)
    layer = out["payload"]["claim_layer"]
    assert layer["status"] == "OK"
    assert layer["conflicting_claim_ids"] == []
