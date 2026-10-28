"""PR-B (Issue #220) passage tests — written FIRST, must FAIL on old HEAD.

New module cba_kb.kc_passages does not exist on base 15867026.
Clean-room fixture values only (fictitious). No private data, no real
Drive/ACL data. All artifacts SIMULATED.
"""

import pytest

from cba_kb.kc_cleanroom import A, QUOTE, build_cleanroom_fixture
from cba_kb.kc_passages import (
    build_routed_passage,
    cite_verbatim,
    collect_candidates,
    evidence_chain,
    first_complete_set,
    verify_passage_text,
)

TARGET = "fixture_doc_D1"


@pytest.fixture(scope="module")
def fx():
    return build_cleanroom_fixture()


def _routed(fx):
    out = []
    for p in fx["passages"]:
        out.append(build_routed_passage(
            source_id=p["source_id"], revision=p["revision"],
            locator=p["locator"], text=p["text"],
            player_uid=p.get("player_uid"), aliases=p.get("aliases") or [],
            title=p["source_id"],
            speaker="受访球员" if p["source_id"] == TARGET else None,
            authorized_url="fixture://authorized-copy/" + p["source_id"]))
    return out


def test_routed_passage_binds_all_required_fields(fx):
    p = build_routed_passage(
        source_id=TARGET, revision="rev-1", locator="P3", text=QUOTE,
        player_uid=A, aliases=["测试甲"], title="授权中文采访",
        speaker="受访球员", attribution="fixture interview",
        authorized_url="fixture://authorized-copy/" + TARGET)
    assert p["title"] == "授权中文采访"
    assert p["speaker"] == "受访球员"
    assert p["attribution"] == "fixture interview"
    assert p["revision"] == "rev-1" and p["locator"] == "P3"
    assert p["digest_kind"] == "KC_PASSAGE_SHA256"
    assert p["algorithm"] == "SHA-256"
    assert p["consumer_source_id"] == TARGET
    assert p["authorized_url"] == "fixture://authorized-copy/" + TARGET


def test_verbatim_only_when_excerpt_verifies(fx):
    p = _routed(fx)[0]
    assert verify_passage_text(p, QUOTE) is True
    assert verify_passage_text(p, "被篡改的引文。") is False
    ok = cite_verbatim(p, QUOTE)
    assert ok["status"] == "OK" and ok["verbatim"] == QUOTE
    bad = cite_verbatim(p, "被篡改的引文。")
    assert bad["status"] == "UNKNOWN"
    assert bad["verbatim"] is None


def test_collection_completes_across_pages_before_ranking(fx):
    routed = _routed(fx)
    pages = [routed[0:20], routed[20:40], routed[40:]]
    collected = collect_candidates(pages, alias="测试甲",
                                   topic_tokens=["罚球", "训练"],
                                   authorized_ids={q["source_id"]
                                                   for q in routed})
    assert collected["complete"] is True
    assert collected["end_marker"] is True
    assert collected["total_pages"] == 3
    assert collected["candidates"][0]["source_id"] == TARGET


def test_incomplete_collection_is_never_read_as_complete(fx):
    routed = _routed(fx)
    pages = [{"passages": routed[0:20], "end_marker": True},
             {"passages": routed[20:40], "end_marker": False}]
    collected = collect_candidates(pages, alias="测试甲",
                                   topic_tokens=["罚球"],
                                   authorized_ids={q["source_id"]
                                                   for q in routed})
    assert collected["complete"] is False
    assert collected["end_marker"] is False
    first = first_complete_set(collected, limit=5)
    assert first["status"] == "PARTIAL"
    assert first["candidates"] == []


def test_top5_first_complete_set_contains_target(fx):
    routed = _routed(fx)
    collected = collect_candidates([routed], alias="测试甲",
                                   topic_tokens=["罚球", "训练"],
                                   authorized_ids={q["source_id"]
                                                   for q in routed})
    first = first_complete_set(collected, limit=5)
    assert first["complete"] is True and first["end_marker"] is True
    assert len(first["candidates"]) == 5
    assert TARGET in [c["source_id"] for c in first["candidates"]]


def test_file_id_ordered_truncation_would_drop_target(fx):
    routed = _routed(fx)
    by_id = sorted(routed, key=lambda p: p["source_id"])[:5]
    assert TARGET not in [c["source_id"] for c in by_id]
    collected = collect_candidates([routed], alias="测试甲",
                                   topic_tokens=["罚球", "训练"],
                                   authorized_ids={q["source_id"]
                                                   for q in routed})
    first = first_complete_set(collected, limit=5)
    assert TARGET in [c["source_id"] for c in first["candidates"]]


def test_evidence_chain_requires_all_links(fx):
    chain = evidence_chain(receipt_id="receipt-1",
                           snapshot_id="kc-snapshot:abc",
                           build_version="build-1",
                           source_digest="0" * 64,
                           authorization_basis="SIMULATED_TARGET_SCOPE")
    assert chain["chain"] == ["receipt", "snapshot", "build",
                              "source_digest", "authorization_basis"]
    with pytest.raises(ValueError):
        evidence_chain(receipt_id="receipt-1", snapshot_id="",
                       build_version="build-1", source_digest="0" * 64,
                       authorization_basis="SIMULATED_TARGET_SCOPE")


# ---- Repair round 1: authorization filtering (R5) + chain verification (R6)
# For R5, the same repair-falsification bridge as the slices tests: a
# TypeError on the new authorized_ids call falls back to the old call,
# whose leak then fails the assertion behaviorally. For R6, the new
# verify function is absent pre-repair, so the bridge asserts False
# with the gap documented; the behavioral proof for the old gap is the
# A6 attack reproduction (inconsistent chain constructed successfully).

def test_collect_excludes_unauthorized_passages(fx):
    routed = _routed(fx)
    intruder = build_routed_passage(
        source_id="fixture_doc_SECRET", revision="rev-1", locator="P1",
        text=QUOTE, player_uid="fixture_player_X",
        aliases=["测试甲"], title="secret",
        authorized_url="fixture://authorized-copy/fixture_doc_SECRET")
    pages = [[intruder] + routed]
    authorized = {q["source_id"] for q in routed}
    try:
        collected = collect_candidates(pages, alias="测试甲",
                                       topic_tokens=["罚球", "训练"],
                                       authorized_ids=authorized)
    except TypeError:
        collected = collect_candidates(pages, alias="测试甲",
                                       topic_tokens=["罚球", "训练"])
        assert "fixture_doc_SECRET" not in [
            c["source_id"] for c in collected["candidates"]], (
            "pre-repair collection leaks unauthorized passages")
        return
    assert "fixture_doc_SECRET" not in [
        c["source_id"] for c in collected["candidates"]]
    assert collected["complete"] is True


def _chain_setup(fx):
    from cba_kb.kc_snapshot import build_snapshot
    passage = _routed(fx)[0]
    snap = build_snapshot(
        entries=[{"entry_id": "entry-stats", "sha256": "a" * 64,
                  "authority": "canonical", "rights": "public",
                  "source_id": "fixture_stats"}],
        target_domain="fixture_target", capabilities=["stats"],
        build_provenance="simulated-build-1",
        build_time="2026-10-28T00:00:00+00:00",
        data_release="SIMULATED_DATA_RELEASE_1",
        engine_candidate="SIMULATED_ENGINE_CANDIDATE_1",
        build_code="SIMULATED_BUILD_CODE_1")
    chain = evidence_chain(receipt_id="receipt-1",
                           snapshot_id=snap.snapshot_id,
                           build_version="build-1",
                           source_digest=passage["digest"],
                           authorization_basis="SIMULATED_TARGET_SCOPE")
    return passage, snap, chain


def test_verify_evidence_chain_accepts_consistent_chain(fx):
    try:
        from cba_kb.kc_passages import verify_evidence_chain
    except ImportError:
        assert False, ("pre-repair code has no chain verification "
                       "(inconsistent chains accepted, see A6)")
    passage, snap, chain = _chain_setup(fx)
    result = verify_evidence_chain(chain, passage=passage, snapshot=snap)
    assert result["valid"] is True


def test_verify_evidence_chain_rejects_wrong_snapshot(fx):
    try:
        from cba_kb.kc_passages import verify_evidence_chain
    except ImportError:
        assert False, ("pre-repair code has no chain verification "
                       "(inconsistent chains accepted, see A6)")
    passage, snap, chain = _chain_setup(fx)
    bad = dict(chain, snapshot_id="kc-snapshot:other")
    with pytest.raises(ValueError):
        verify_evidence_chain(bad, passage=passage, snapshot=snap)


def test_verify_evidence_chain_rejects_wrong_digest_and_tampered_passage(fx):
    try:
        from cba_kb.kc_passages import verify_evidence_chain
    except ImportError:
        assert False, ("pre-repair code has no chain verification "
                       "(inconsistent chains accepted, see A6)")
    passage, snap, chain = _chain_setup(fx)
    bad = dict(chain, source_digest="0" * 64)
    with pytest.raises(ValueError):
        verify_evidence_chain(bad, passage=passage, snapshot=snap)
    tampered = dict(passage, digest="0" * 64)
    with pytest.raises(ValueError):
        verify_evidence_chain(chain, passage=tampered, snapshot=snap)


def test_verify_evidence_chain_rejects_empty_basis(fx):
    try:
        from cba_kb.kc_passages import verify_evidence_chain
    except ImportError:
        assert False, ("pre-repair code has no chain verification "
                       "(inconsistent chains accepted, see A6)")
    passage, snap, chain = _chain_setup(fx)
    bad = dict(chain, authorization_basis="")
    with pytest.raises(ValueError):
        verify_evidence_chain(bad, passage=passage, snapshot=snap)
