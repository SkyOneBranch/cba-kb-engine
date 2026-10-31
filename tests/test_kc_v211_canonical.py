"""CBA-KB v2.1.1 T2-1 — canonical-layer wiring tests (Human-authorized
continuous execution, 2026-10-31). Real-data candidates (A1 event
references, B1 identity inventory) are consumed through explicit
gates, never as formal releases:

Claim: resolve_claims_by_event gains an optional canonical layer
(canonical_refs + canonical_approval). The approval binds the
release fields AND the refs content with two digests; a ref is
trusted only when present, in an approved tier, and confirmed.
Without valid approval the layer trusts nothing — it never
upgrades content-support/binding into formal identity, and it
never overrides the earlier checks.

Identity: validate_identity_inventory_candidate validates a
CANDIDATE inventory's internal reconciliation only. It refuses
anything labeled formal and never returns an authorization object;
the trusted path still requires a separate formal
identity_authorization, without which identity stays HOLD.

Falsification labels: NEW-CAPABILITY means the test exercises a
seam the old code did not have (canonical params / validator).
The BUSINESS falsification for this round is demonstrated by the
workspace real-candidate script, which runs the old path (no
canonical params) and the new path on the same real claims and
shows verdicts changing for business reasons (canonical gate).
"""

import copy

import pytest

from cba_kb.kc_cleanroom import A, SEASON, build_cleanroom_fixture
from cba_kb.kc_consume import ConsumeSession, _content_digest
from cba_kb.kc_local_producer import validate_identity_inventory_candidate
from cba_kb.kc_passages import build_routed_passage, evidence_chain
from cba_kb.kc_projection import (
    canonical_json, event_association_digest, resolve_claims_by_event,
    sha256_hex, validate_canonical_approval)
from cba_kb.kc_snapshot import build_snapshot

PROVIDER = "CBA_OFFICIAL"
COMPETITION = "CBA"
TARGET = "fixture_target"
TIER_A = "TIER_A_OFFICIAL_MIRROR"
TIER_B = "TIER_B_SECONDARY_CROSSCHECK"
SRC_SHA = "0" * 64


# ------------------------------------------------------- Claim helpers

def _claim(cid, text, ref, locator, date="2026-03-10"):
    claim = {"claim_id": cid, "player_uid": "P1", "date": date,
             "text": text, "source_id": "SRC"}
    assoc = {"event_ref": ref, "evidence": ["SRC:" + locator],
             "source_id": "SRC", "revision": "r1", "locator": locator,
             "player_uid": "P1", "snapshot_id": "S1", "digest": ""}
    assoc["digest"] = event_association_digest(claim, assoc)
    claim["event_association"] = assoc
    return claim


def _texts(*pairs):
    # pairs: (locator, ref) — anchor text mentions its claimed ref.
    return {("SRC", "r1", loc): "事件 %s 发生。" % ref
            for loc, ref in pairs}


def _approval(refs, tiers, release_id="REL-1", version="v1"):
    canon = sha256_hex(canonical_json(refs))
    appr = sha256_hex(canonical_json({
        "release_id": release_id, "release_version": version,
        "source_sha256": SRC_SHA, "approved_tiers": sorted(tiers),
        "canonical_digest": canon}))
    return {"release_id": release_id, "release_version": version,
            "source_sha256": SRC_SHA, "approved_tiers": list(tiers),
            "canonical_digest": canon, "approval_digest": appr}


def _resolve(claims, texts, refs=None, approval=None):
    return resolve_claims_by_event(
        claims, passage_texts=texts,
        canonical_refs=refs, canonical_approval=approval)


# ------------------------------------------------------- Claim: canonical gate

def test_canonical_approved_same_ref_conflict():
    # NEW-CAPABILITY positive: same canonical ref, differing
    # values, content-backed — CONFLICT on a canonical basis.
    refs = {"EV1": {"tier": TIER_A, "confirmed": True}}
    claims = [_claim("c1", "甲", "EV1", "P1"),
              _claim("c2", "乙", "EV1", "P1")]
    out = _resolve(claims, _texts(("P1", "EV1")),
                   refs, _approval(refs, [TIER_A]))
    assert out["status"] == "CONFLICT"
    assert out["conflicting_claim_ids"] == ["c1", "c2"]


def test_canonical_approved_distinct_refs_ok():
    # NEW-CAPABILITY positive: two distinct canonical refs,
    # differing values — a proven-no-conflict OK, now on a
    # canonical basis rather than string inequality.
    refs = {"EV1": {"tier": TIER_A, "confirmed": True},
            "EV2": {"tier": TIER_A, "confirmed": True}}
    claims = [_claim("c1", "甲", "EV1", "P1"),
              _claim("c2", "乙", "EV2", "P2")]
    out = _resolve(claims, _texts(("P1", "EV1"), ("P2", "EV2")),
                   refs, _approval(refs, [TIER_A]))
    assert out["status"] == "OK"


def test_canonical_ref_absent_unknown():
    # NEW-CAPABILITY gate: same shape as the conflict positive,
    # but the ref is absent from the canonical set. The old
    # (no-canonical-params) path returns CONFLICT here; the
    # canonical layer refuses it: UNKNOWN, not CONFLICT.
    refs = {"EV9": {"tier": TIER_A, "confirmed": True}}
    claims = [_claim("c1", "甲", "EV1", "P1"),
              _claim("c2", "乙", "EV1", "P1")]
    out = _resolve(claims, _texts(("P1", "EV1")),
                   refs, _approval(refs, [TIER_A]))
    assert out["status"] == "UNKNOWN"
    assert out["conflicting_claim_ids"] == []
    base = _resolve(claims, _texts(("P1", "EV1")))
    assert base["status"] == "CONFLICT"


def test_canonical_tier_unapproved_unknown():
    # Tier gating: the ref is present but its tier is not in
    # the approved set — UNKNOWN, not CONFLICT.
    refs = {"EV1": {"tier": TIER_B, "confirmed": True}}
    claims = [_claim("c1", "甲", "EV1", "P1"),
              _claim("c2", "乙", "EV1", "P1")]
    out = _resolve(claims, _texts(("P1", "EV1")),
                   refs, _approval(refs, [TIER_A]))
    assert out["status"] == "UNKNOWN"


def test_canonical_unconfirmed_unknown():
    # Confirmed gating: an approved-tier ref marked unconfirmed
    # (e.g. an uncompleted event) is not usable: UNKNOWN.
    refs = {"EV1": {"tier": TIER_A, "confirmed": False}}
    claims = [_claim("c1", "甲", "EV1", "P1"),
              _claim("c2", "乙", "EV1", "P1")]
    out = _resolve(claims, _texts(("P1", "EV1")),
                   refs, _approval(refs, [TIER_A]))
    assert out["status"] == "UNKNOWN"


def test_canonical_approval_digest_tampered():
    # Tamper drill (approval side): a recomputed-looking
    # approval_digest that does not match the fields is
    # rejected, with an explicit marker — never silently
    # downgraded to the weaker path.
    refs = {"EV1": {"tier": TIER_A, "confirmed": True}}
    approval = _approval(refs, [TIER_A])
    approval["approval_digest"] = "1" * 64
    claims = [_claim("c1", "甲", "EV1", "P1"),
              _claim("c2", "乙", "EV1", "P1")]
    out = _resolve(claims, _texts(("P1", "EV1")), refs, approval)
    assert out["status"] == "UNKNOWN"
    assert "CANONICAL_APPROVAL_INVALID" in out["unresolved"]


def test_canonical_refs_tampered_after_approval():
    # Tamper drill (refs side): refs swapped under a valid
    # approval break the canonical digest binding: UNKNOWN.
    refs = {"EV1": {"tier": TIER_A, "confirmed": True}}
    approval = _approval(refs, [TIER_A])
    tampered = copy.deepcopy(refs)
    tampered["EV1"] = {"tier": TIER_A, "confirmed": True}
    tampered["EV2"] = {"tier": TIER_A, "confirmed": True}
    claims = [_claim("c1", "甲", "EV1", "P1"),
              _claim("c2", "乙", "EV1", "P1")]
    out = _resolve(claims, _texts(("P1", "EV1")), tampered, approval)
    assert out["status"] == "UNKNOWN"
    assert "CANONICAL_APPROVAL_INVALID" in out["unresolved"]


@pytest.mark.parametrize("mutate", [
    lambda a: a.pop("release_version"),
    lambda a: a.update(approved_tiers=[]),
    lambda a: a.update(source_sha256="not-a-sha"),
    lambda a: a.update(canonical_digest="f" * 64),
])
def test_canonical_approval_malformed(mutate):
    # Every malformed approval shape is rejected explicitly.
    refs = {"EV1": {"tier": TIER_A, "confirmed": True}}
    approval = _approval(refs, [TIER_A])
    mutate(approval)
    claims = [_claim("c1", "甲", "EV1", "P1"),
              _claim("c2", "乙", "EV1", "P1")]
    out = _resolve(claims, _texts(("P1", "EV1")), refs, approval)
    assert out["status"] == "UNKNOWN"
    assert "CANONICAL_APPROVAL_INVALID" in out["unresolved"]
    assert validate_canonical_approval(approval, refs) is False


def test_canonical_half_configured_trusts_nothing():
    # A half-configured layer (refs without approval, or
    # approval without refs) trusts nothing, with a marker.
    refs = {"EV1": {"tier": TIER_A, "confirmed": True}}
    claims = [_claim("c1", "甲", "EV1", "P1"),
              _claim("c2", "乙", "EV1", "P1")]
    out = _resolve(claims, _texts(("P1", "EV1")),
                   refs, None)
    assert out["status"] == "UNKNOWN"
    assert "CANONICAL_LAYER_HALF_CONFIGURED" in out["unresolved"]
    out = _resolve(claims, _texts(("P1", "EV1")),
                   None, _approval(refs, [TIER_A]))
    assert out["status"] == "UNKNOWN"
    assert "CANONICAL_LAYER_HALF_CONFIGURED" in out["unresolved"]


def test_canonical_absent_fallback_unchanged():
    # PIN: with neither param, behavior is exactly the
    # pre-canonical behavior — same-ref differing values
    # content-backed still CONFLICT. The layer adds a gate;
    # it does not change the fallback.
    claims = [_claim("c1", "甲", "EV1", "P1"),
              _claim("c2", "乙", "EV1", "P1")]
    out = _resolve(claims, _texts(("P1", "EV1")))
    assert out["status"] == "CONFLICT"


def test_canonical_does_not_override_content_or_digest():
    # Layers compose with AND: a canonical ref does not rescue
    # an association whose anchor text never states the ref,
    # nor one whose digest is wrong.
    refs = {"EV1": {"tier": TIER_A, "confirmed": True}}
    approval = _approval(refs, [TIER_A])
    claims = [_claim("c1", "甲", "EV1", "P1"),
              _claim("c2", "乙", "EV1", "P1")]
    out = _resolve(claims, _texts(("P1", "OTHER")), refs, approval)
    assert out["status"] == "UNKNOWN"
    broken = [_claim("c1", "甲", "EV1", "P1"),
              _claim("c2", "乙", "EV1", "P1")]
    broken[0]["event_association"]["digest"] = "2" * 64
    out = _resolve(broken, _texts(("P1", "EV1")), refs, approval)
    assert out["status"] == "UNKNOWN"


# ------------------------------------------------------- Claim: consumer pass-through

def _consumer_session(**over):
    kw = dict(target_domain=TARGET,
              authorized_players={A},
              authorized_sources={"fixture_doc_D1", "fixture_doc_D2"},
              authorized_objects={"fixture_doc_D1", "fixture_doc_D2"})
    kw.update(over)
    return ConsumeSession(**kw)


def _consumer_case(fx, refs, approval):
    session = _consumer_session(
        canonical_event_refs=refs, canonical_approval=approval)
    probe = _consumer_session()
    probed = probe.consume_slice(
        fx["stats_records"], player_uid=A, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION)
    slice_digest = _content_digest(probed["payload"])
    passage = build_routed_passage(
        source_id="fixture_doc_D1", revision="rev-1", locator="P3",
        text=fx["passages"][0]["text"] + " 事件 EV1。",
        player_uid=A, aliases=["测试甲"], title="fixture_doc_D1",
        authorized_url="fixture://authorized-copy/fixture_doc_D1")
    entries = [{"entry_id": "entry-stats", "sha256": slice_digest,
                "authority": "canonical", "rights": "public",
                "source_id": "fixture_stats"},
               {"entry_id": "entry-fixture_doc_D1",
                "sha256": passage["digest"],
                "authority": "canonical", "rights": "public",
                "source_id": "fixture_doc_D1"}]
    snap = build_snapshot(
        entries=entries, target_domain=TARGET, capabilities=["stats"],
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

    def _claim2(cid, text):
        claim = {"claim_id": cid, "player_uid": A,
                 "date": "2026-03-10", "text": text,
                 "source_id": "fixture_doc_D1"}
        assoc = {"event_ref": "EV1", "evidence": ["fixture_doc_D1:P3"],
                 "source_id": "fixture_doc_D1", "revision": "rev-1",
                 "locator": "P3", "player_uid": A,
                 "snapshot_id": snap.snapshot_id, "digest": ""}
        assoc["digest"] = event_association_digest(claim, assoc)
        claim["event_association"] = assoc
        return claim

    claims = [_claim2("c1", "事件发生在 2026-03-10"),
              _claim2("c2", "事件发生在 2026-03-15")]
    fact = {"fact_id": "ev_fact", "fact_version": "v1",
            "player_uid": A, "season": SEASON,
            "team_id": "fixture_team_T2", "kind": "REGISTRATION",
            "source": "fixture_doc_D1"}
    out = session.consume_answer(
        records=fx["stats_records"], player_uid=A, season=SEASON,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION,
        metric="points_per_game", fact=fact,
        passage=passage, source_text=passage["text"],
        chain=chain, claims=claims)
    return out["payload"]["claim_layer"]


def test_consumer_canonical_passthrough():
    # The consumer session forwards its canonical params to
    # the resolver: absent ref -> UNKNOWN through the consumer;
    # approved ref -> CONFLICT through the consumer.
    fx = build_cleanroom_fixture()
    layer = _consumer_case(fx, {"EV9": {"tier": TIER_A,
                                        "confirmed": True}},
                           _approval({"EV9": {"tier": TIER_A,
                                               "confirmed": True}},
                                     [TIER_A]))
    assert layer["status"] == "UNKNOWN"
    refs = {"EV1": {"tier": TIER_A, "confirmed": True}}
    layer = _consumer_case(fx, refs, _approval(refs, [TIER_A]))
    assert layer["status"] == "CONFLICT"


# ------------------------------------------------------- Identity: candidate validator

def _candidate(**over):
    cand = {
        "candidate_label": "CANDIDATE_NOT_FORMAL_INDEX",
        "candidate_version": "cand-b1-test",
        "sources": {"master_sha256": "a" * 64},
        "coverage_proof": {"master_rows": 10,
                           "per_season_counts": {"2024-2025": 6,
                                                 "2025-2026": 4},
                           "distinct_player_names": 2},
        "inventory": {
            "P_A": {"rows": 6,
                    "seasons": {"2024-2025": 6},
                    "clubs": {"C1": 6},
                    "tiers": {"official_api": 6}},
            "P_B": {"rows": 4,
                    "seasons": {"2025-2026": 4},
                    "clubs": {"C2": 4},
                    "tiers": {"auto_validated": 4}},
        },
    }
    cand.update(over)
    return cand


def test_inventory_candidate_valid():
    out = validate_identity_inventory_candidate(_candidate())
    assert out["valid"] is True
    assert out["master_rows"] == 10
    assert out["distinct_player_names"] == 2


def test_inventory_candidate_season_sum_mismatch():
    cand = _candidate()
    cand["coverage_proof"]["per_season_counts"] = {"2024-2025": 5,
                                                  "2025-2026": 4}
    out = validate_identity_inventory_candidate(cand)
    assert out["valid"] is False
    assert out["reason"] == "COVERAGE_SEASON_SUM_MISMATCH"


def test_inventory_candidate_name_count_mismatch():
    cand = _candidate()
    cand["coverage_proof"]["distinct_player_names"] = 3
    out = validate_identity_inventory_candidate(cand)
    assert out["valid"] is False
    assert out["reason"] == "COVERAGE_NAME_COUNT_MISMATCH"


def test_inventory_candidate_formal_label_refused():
    # Label laundering is refused: a candidate claiming to be
    # formal is not validated as a candidate at all.
    cand = _candidate(candidate_label="FORMAL_INDEX_V1")
    out = validate_identity_inventory_candidate(cand)
    assert out["valid"] is False
    assert out["reason"] == "CANDIDATE_LABEL_NOT_CANDIDATE"


def test_inventory_candidate_entry_sum_mismatch():
    cand = _candidate()
    cand["inventory"]["P_A"]["clubs"] = {"C1": 5}
    out = validate_identity_inventory_candidate(cand)
    assert out["valid"] is False
    assert out["reason"] == "INVENTORY_ENTRY_SUM_MISMATCH"


@pytest.mark.parametrize("bad", [
    None, [], "x", {},
    _candidate(candidate_version=""),
    _candidate(sources={"master_sha256": "not-a-sha"}),
    _candidate(coverage_proof={"master_rows": True,
                               "per_season_counts": {"s": 1}}),
    _candidate(inventory={}),
    _candidate(inventory={"P_A": {"rows": 0, "seasons": {},
                                  "clubs": {}, "tiers": {}}}),
])
def test_inventory_candidate_malformed_never_raises(bad):
    # Every malformed shape returns an explicit invalid result,
    # never an exception.
    out = validate_identity_inventory_candidate(bad)
    assert out["valid"] is False
    assert isinstance(out["reason"], str) and out["reason"]
