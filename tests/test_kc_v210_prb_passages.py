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
                                   topic_tokens=["罚球", "训练"])
    assert collected["complete"] is True
    assert collected["end_marker"] is True
    assert collected["total_pages"] == 3
    assert collected["candidates"][0]["source_id"] == TARGET


def test_incomplete_collection_is_never_read_as_complete(fx):
    routed = _routed(fx)
    pages = [{"passages": routed[0:20], "end_marker": True},
             {"passages": routed[20:40], "end_marker": False}]
    collected = collect_candidates(pages, alias="测试甲",
                                   topic_tokens=["罚球"])
    assert collected["complete"] is False
    assert collected["end_marker"] is False
    first = first_complete_set(collected, limit=5)
    assert first["status"] == "PARTIAL"
    assert first["candidates"] == []


def test_top5_first_complete_set_contains_target(fx):
    routed = _routed(fx)
    collected = collect_candidates([routed], alias="测试甲",
                                   topic_tokens=["罚球", "训练"])
    first = first_complete_set(collected, limit=5)
    assert first["complete"] is True and first["end_marker"] is True
    assert len(first["candidates"]) == 5
    assert TARGET in [c["source_id"] for c in first["candidates"]]


def test_file_id_ordered_truncation_would_drop_target(fx):
    routed = _routed(fx)
    by_id = sorted(routed, key=lambda p: p["source_id"])[:5]
    assert TARGET not in [c["source_id"] for c in by_id]
    collected = collect_candidates([routed], alias="测试甲",
                                   topic_tokens=["罚球", "训练"])
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
