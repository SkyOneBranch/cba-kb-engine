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
    out = pc.assess_pair(
        _sighting("stats#1"), _sighting("article#1"),
        trusted_link=minted.for_pair("article#1", "stats#1"))
    assert out["verdict"] == "MERGE_ALLOWED"
    assert minted.audit["operator"] == "synthetic-operator-1"
    assert minted.audit["audit_id"].startswith("audit_")


def test_minted_not_same_is_cannot_link():
    ctx = _ctx()
    minted = trust.mint_human_decision(
        ctx, left_ref="a#1", right_ref="b#1", link="not_same",
        evidence_refs=["synthetic://review-1"])
    out = pc.assess_pair(
        _sighting("a#1", external_id="EXT-1"),
        _sighting("b#1", external_id="EXT-1"),
        human_decision=minted.for_pair("a#1", "b#1"),
        verified_external_ids={"EXT-1"})
    assert out["verdict"] == "DISTINCT"


def test_attested_verified_ids_enable_merge():
    ctx = _ctx()
    attestation = trust.attest_verified_ids(
        ctx, ["EXT-1"], verification_ref="synthetic://registry-check-1")
    out = pc.assess_pair(
        _sighting("a#1", external_id="EXT-1"),
        _sighting("b#1", external_id="EXT-1"),
        verified_external_ids=attestation.external_ids)
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
        minted.for_pair("a#1", "c#1")


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
