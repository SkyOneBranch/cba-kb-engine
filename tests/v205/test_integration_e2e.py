"""T2-8 integration E2E (Issue #212): adapters wiring, B5 trusted supply
path, AC-25 chain, AC-27 determinism, no auto-promotion.

All fixtures are synthetic (AC-29). The trusted-path tests use an
operator established from a test-controlled environment mapping — the
same fail-closed contract production callers get.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cba_kb import person_candidate as pc
from cba_kb import person_candidate_trust as trust
from cba_kb.adapters import (
    ADAPTERS,
    adapter_for,
    article_index,
    muse_handoff,
    probe,
    xls_adapter,
)

FIXTURES = Path(__file__).parent / "fixtures"
OPERATOR_ENV = {trust.OPERATOR_ENV_VAR: "synthetic-operator-1"}


def _ctx():
    return trust.establish_operator_context(OPERATOR_ENV)


# ---------------------------------------------------------------- wiring

def test_xls_probe_routing():
    assert probe("roster.xls") == "xls_adapter"
    assert probe("ROSTER.XLS") == "xls_adapter"
    assert ADAPTERS["xls_adapter"] is xls_adapter
    assert adapter_for(path="roster.xls") is xls_adapter
    assert adapter_for(name="xls_adapter") is xls_adapter


def test_payload_and_projector_modules_exported_not_probed():
    # muse_handoff / article_index are integration surface only: they do
    # not claim a path suffix and are not in ADAPTERS.
    assert "muse_handoff" not in ADAPTERS
    assert "article_index" not in ADAPTERS
    assert muse_handoff.adapt_handoff is not None
    assert article_index.build_article_index is not None


# ------------------------------------------------------- AC-25 chain

def _article_records():
    return [
        {"article_id": "WX001", "title": "Synthetic profile of Alex",
         "source_sheet": "Articles", "source_row": 2,
         "row_date_text": "2024-01-15"},
        {"article_id": "WX002", "title": "Match report",
         "source_sheet": "Articles", "source_row": 3},
    ]


def test_article_index_lineage_hop():
    index = article_index.build_article_index(
        _article_records(), source_file="synthetic.xlsx")
    assert len(index["candidates"]) == 2
    lineage = article_index.resolve_lineage(index["candidates"][0])
    assert lineage == {"source_file": "synthetic.xlsx",
                       "source_sheet": "Articles", "source_row": 2}
    # Association path: person hits stay UNVERIFIED candidates.
    hits = article_index.candidate_articles_for_person(index, "Alex")
    assert hits and all(
        h["association_status"] == "UNVERIFIED" for h in hits)


def test_handoff_to_stats_records():
    payload = json.loads(
        (FIXTURES / "handoff_synth.json").read_text(encoding="utf-8"))
    out = muse_handoff.adapt_handoff(payload)
    assert out["records"], "handoff must yield stats records"


# ------------------------------------------------- B5 trusted supply

def _sighting(ref, **kw):
    base = {"source_ref": ref, "display_name": "Synthetic Alex"}
    base.update(kw)
    return base


def test_minted_trusted_link_merges():
    ctx = _ctx()
    minted = trust.mint_trusted_link(
        ctx, left_ref="stats#1", right_ref="article#1",
        method="SOURCE_DECLARED", evidence_refs=["synthetic://audit-1"])
    out = minted.apply_pair(
        _sighting("article#1"), _sighting("stats#1"))  # 反向亦可
    assert out["verdict"] == "MERGE_ALLOWED"
    assert minted.audit["operator"] == "synthetic-operator-1"
    assert minted.audit["audit_id"].startswith("audit_")


def test_minted_not_same_is_cannot_link():
    ctx = _ctx()
    minted = trust.mint_human_decision(
        ctx, left_ref="a#1", right_ref="b#1", link="not_same",
        evidence_refs=["synthetic://review-1"])
    out = minted.apply_pair(
        _sighting("a#1", external_id="EXT-1"),
        _sighting("b#1", external_id="EXT-1"),
        verified_external_ids={"EXT-1"})
    assert out["verdict"] == "DISTINCT"


def test_attested_verified_ids_enable_merge():
    ctx = _ctx()
    attestation = trust.attest_verified_ids(
        ctx, ["EXT-1"], verification_ref="synthetic://registry-check-1")
    out = pc.assess_pair(
        _sighting("a#1", external_id="EXT-1"),
        _sighting("b#1", external_id="EXT-1"),
        verified_external_ids=trust.attested_ids(attestation))
    assert out["verdict"] == "MERGE_ALLOWED"


# ------------------------------------------------------- forgery paths

def test_minting_requires_established_context():
    with pytest.raises(trust.TrustError):
        trust.establish_operator_context({})
    with pytest.raises(trust.TrustError):
        trust.establish_operator_context({trust.OPERATOR_ENV_VAR: "  "})
    with pytest.raises(trust.TrustError):
        trust.TrustedOperatorContext("mallory", established_via="self")
    for fake_ctx in ({"operator": "mallory"}, "mallory", None):
        with pytest.raises(trust.TrustError):
            trust.mint_human_decision(
                fake_ctx, left_ref="a#1", right_ref="b#1", link="same",
                evidence_refs=["x"])


def test_minting_requires_evidence_and_bound_pair():
    ctx = _ctx()
    with pytest.raises(trust.TrustError):
        trust.mint_trusted_link(
            ctx, left_ref="a#1", right_ref="b#1",
            method="SOURCE_DECLARED", evidence_refs=[])
    with pytest.raises(trust.TrustError):
        trust.attest_verified_ids(ctx, ["EXT-1"], verification_ref="")
    minted = trust.mint_human_decision(
        ctx, left_ref="a#1", right_ref="b#1", link="same",
        evidence_refs=["synthetic://r"])
    with pytest.raises(trust.TrustError):
        minted.apply_pair(_sighting("a#1"), _sighting("c#1"))


def test_r2_pair_binding_cannot_be_spent_cross_pair():
    """B1: 铸造的决定只能花在绑定 pair 上（经包装器消费）。"""
    ctx = _ctx()
    minted = trust.mint_human_decision(
        ctx, left_ref="a#1", right_ref="b#1", link="same",
        evidence_refs=["synthetic://r"])
    with pytest.raises(trust.TrustError):
        minted.apply_pair(_sighting("c#1"), _sighting("d#1"))


def test_r3_batch_spend_through_wrapper_only():
    """B1/R3: batch 只能经 apply_batch 消费；无任何公共出口外发裸对象。"""
    ctx = _ctx()
    minted = trust.mint_human_decision(
        ctx, left_ref="a#1", right_ref="b#1", link="same",
        evidence_refs=["synthetic://r"])
    # R1/R2 的提取路线全部关闭
    assert not hasattr(minted, "for_pair")
    assert not hasattr(minted, "put_into_batch")
    assert not hasattr(minted, "authorization")
    # 正向：apply_batch 在绑定 pair 上生效
    out = trust.apply_batch(
        [minted], [_sighting("a#1"), _sighting("b#1")])
    assert out["groups"][0]["verdict"] == "MERGE_ALLOWED"
    # 冲突：同 pair 两个不同铸造决定 -> 拒绝
    other = trust.mint_human_decision(
        ctx, left_ref="b#1", right_ref="a#1", link="not_same",
        evidence_refs=["synthetic://r2"])
    with pytest.raises(trust.TrustError):
        trust.apply_batch(
            [minted, other], [_sighting("a#1"), _sighting("b#1")])
    # 裸对象/普通 dict 混入 apply_batch -> 拒绝
    with pytest.raises(trust.TrustError):
        trust.apply_batch(
            [{"link": "same"}], [_sighting("a#1"), _sighting("b#1")])


def test_r4_no_reconstruction_or_mutation_route():
    """B1/R4: dataclasses.replace 重绑定、属性赋值、直接构造全部关闭。"""
    import dataclasses

    ctx = _ctx()
    minted = trust.mint_human_decision(
        ctx, left_ref="a#1", right_ref="b#1", link="same",
        evidence_refs=["synthetic://r"])
    with pytest.raises(TypeError):
        dataclasses.replace(minted, source_refs=("c#1", "d#1"))
    with pytest.raises(AttributeError):
        minted.source_refs = ("c#1", "d#1")
    with pytest.raises(AttributeError):
        ctx.operator = "forged"
    with pytest.raises(trust.TrustError):
        trust.MintedAuthorization(
            minted, kind="human_decision",
            source_refs=("c#1", "d#1"), audit={})
    att = trust.attest_verified_ids(ctx, ["EXT-1"],
                                    verification_ref="chk-1")
    with pytest.raises(TypeError):
        dataclasses.replace(att, external_ids=frozenset({"FORGED"}))
    with pytest.raises(AttributeError):
        att.external_ids = frozenset({"FORGED"})
    # 审计以副本外发：改副本不影响原件
    minted.audit["operator"] = "tampered"
    assert minted.audit["operator"] == "synthetic-operator-1"


def test_r5_wrappers_are_final_and_consumption_is_exact_typed():
    """B1/R5: __class__ 替换路线——子类化本身被禁，消费点精确类型校验。"""
    ctx = _ctx()
    # 三个包装类全部禁止子类化（审查员的 Rebound* 类定义即失败）
    for base in (trust.MintedAuthorization,
                 trust.TrustedOperatorContext,
                 trust.VerifiedIdAttestation):
        with pytest.raises(TypeError):
            type("Rebound", (base,), {"__slots__": ()})
    # 正向路径不受影响
    minted = trust.mint_human_decision(
        ctx, left_ref="a#1", right_ref="b#1", link="same",
        evidence_refs=["synthetic://r"])
    out = trust.apply_batch(
        [minted], [_sighting("a#1"), _sighting("b#1")])
    assert out["groups"][0]["verdict"] == "MERGE_ALLOWED"
    att = trust.attest_verified_ids(ctx, ["EXT-1"],
                                    verification_ref="chk-1")
    assert trust.attested_ids(att) == frozenset({"EXT-1"})
    with pytest.raises(trust.TrustError):
        trust.attested_ids({"external_ids": {"FORGED"}})


def test_r2_verified_id_audit_ids_are_distinct():
    """B2: 不同 ID 集合的 attestation 审计 ID 必须不同；相同输入确定一致。"""
    ctx = _ctx()
    a1 = trust.attest_verified_ids(ctx, ["EXT-1"],
                                   verification_ref="chk-1")
    a2 = trust.attest_verified_ids(ctx, ["EXT-2", "EXT-3"],
                                   verification_ref="chk-1")
    a1_again = trust.attest_verified_ids(ctx, ["EXT-1"],
                                         verification_ref="chk-1")
    assert a1.audit["audit_id"] != a2.audit["audit_id"]
    assert a1.audit["audit_id"] == a1_again.audit["audit_id"]


def test_plain_dict_authorizations_still_rejected():
    with pytest.raises(pc.PersonCandidateError):
        pc.assess_pair(
            _sighting("a#1"), _sighting("b#1"),
            human_decision={"link": "same", "decided_by": "mallory"})
    with pytest.raises(pc.PersonCandidateError):
        pc.assess_pair(
            _sighting("a#1"), _sighting("b#1"),
            trusted_link={"method": "SOURCE_DECLARED",
                          "asserted_by": "mallory",
                          "evidence_refs": ["x"], "verified": True})


# ------------------------------------------- no auto-promotion / AC-27

def test_no_auto_promotion_without_trusted_path():
    out = pc.assess_pair(
        _sighting("a#1", external_id="EXT-1"),
        _sighting("b#1", external_id="EXT-1"))
    assert out["verdict"] != "MERGE_ALLOWED"
    assert out["status"] == "REVIEW_REQUIRED"
    assert out["automatic_merge_allowed"] is False


def test_determinism_across_runs():
    first = article_index.build_article_index(
        _article_records(), source_file="synthetic.xlsx")
    second = article_index.build_article_index(
        _article_records(), source_file="synthetic.xlsx")
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == \
        json.dumps(second, ensure_ascii=False, sort_keys=True)
    payload = json.loads(
        (FIXTURES / "handoff_synth.json").read_text(encoding="utf-8"))
    assert muse_handoff.payload_sha256(payload) == \
        muse_handoff.payload_sha256(payload)
