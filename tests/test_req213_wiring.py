"""Adversarial & call-chain wiring tests for v2.1.3 (REQ-213).

Covers security invariants, Rule A authorization leak prevention,
supersession cycle/conflict fallthrough, claim contradiction detection,
local projection research_card.md fail-closed verification, legacy 9-key
OracleQuestion digest parity, and boundary-safe scope token stripping.
"""

from __future__ import annotations

import copy
import json
import os

import pytest

from cba_kb.kc_cleanroom import A, B, SEASON, build_cleanroom_fixture
from cba_kb.kc_consume import ConsumeSession, KcConsumeError, UNIFORM_NOT_FOUND, _content_digest
from cba_kb.kc_evidence_status import (
    EVIDENCE_POSTURES,
    UNKNOWN_SUBKINDS,
    classify_claim_independence,
    classify_stats_evidence,
)
from cba_kb.kc_local_producer import build_local_projection, verify_local_projection
from cba_kb.kc_passages import build_routed_passage, evidence_chain
from cba_kb.kc_projection import canonical_json, query_stats, sha256_hex, KcError
from cba_kb.kc_render import parse_rendered
from cba_kb.kc_snapshot import build_snapshot
from cba_kb.oracle import (
    ClientReceipt,
    OracleQuestion,
    OracleSource,
    ScoringApproval,
    build_oracle,
    locator_set_digest,
    score_answer_approved,
    scoring_code_sha,
    validate_receipt_approved,
    verify_manifest,
)


def _setup_session(fx: dict, *, authorized_players=frozenset({A})):
    consume_session = ConsumeSession(
        target_domain="fixture_target",
        authorized_players=set(authorized_players),
        authorized_sources={"fixture_doc_D1"},
        authorized_objects={"fixture_doc_D1"},
    )
    passage = build_routed_passage(
        source_id="fixture_doc_D1",
        revision="rev-1",
        locator="P3",
        text=fx["passages"][0]["text"],
        player_uid=A,
        aliases=["测试甲"],
        title="fixture_doc_D1",
        authorized_url="fixture://authorized-copy/fixture_doc_D1",
    )
    probe = ConsumeSession(
        target_domain="fixture_target",
        authorized_players={A},
        authorized_sources={"fixture_doc_D1"},
        authorized_objects={"fixture_doc_D1"},
    )
    probed = probe.consume_slice(
        fx["stats_records"],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        provider="CBA_OFFICIAL",
        competition="CBA",
    )
    snap = build_snapshot(
        entries=[
            {
                "entry_id": "entry-stats",
                "sha256": _content_digest(probed["payload"]),
                "authority": "canonical",
                "rights": "public",
                "source_id": "fixture_stats",
            },
            {
                "entry_id": "entry-doc-D1",
                "sha256": passage["digest"],
                "authority": "canonical",
                "rights": "public",
                "source_id": "fixture_doc_D1",
            },
        ],
        target_domain="fixture_target",
        capabilities=["stats"],
        build_provenance="simulated-build-wiring",
        build_time="2026-11-11T00:00:00+00:00",
        data_release="SIMULATED_DATA_RELEASE_WIRING",
        engine_candidate="SIMULATED_ENGINE_CANDIDATE_WIRING",
        build_code="SIMULATED_BUILD_CODE_WIRING",
    )
    consume_session.bind_current(snap)
    consume_session.register_passages([passage])
    binding = consume_session.current_binding()
    chain = evidence_chain(
        receipt_id=binding["receipt_binding"],
        snapshot_id=snap.snapshot_id,
        build_version=snap.build_code,
        source_digest=passage["digest"],
        authorization_basis=binding["authorization_basis"],
    )
    return consume_session, snap, passage, chain


def test_posture_constants_are_frozensets_and_query_stats_untouched():
    """EVIDENCE_POSTURES and UNKNOWN_SUBKINDS are immutable frozensets; primitive query_stats still returns CONFLICT."""
    assert isinstance(EVIDENCE_POSTURES, frozenset)
    assert isinstance(UNKNOWN_SUBKINDS, frozenset)

    fx = build_cleanroom_fixture()
    r1 = copy.deepcopy(fx["stats_records"][0])
    r2 = copy.deepcopy(fx["stats_records"][0])
    raw = query_stats(
        [r1, r2],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        provider="CBA_OFFICIAL",
        competition="CBA",
        metric="points_per_game",
    )
    assert raw["status"] == "CONFLICT"


def test_rule_a_unauthorized_player_leaks_nothing_in_evidence_transparency():
    """Unauthorized vs nonexistent player in consume_answer produces identical UNKNOWN evidence_transparency."""
    fx = build_cleanroom_fixture()
    consume_session, snap, passage, chain = _setup_session(fx, authorized_players={A})

    # Player B exists in fx["stats_records"] (7.0 ppg) but is NOT authorized in consume_session
    out_unauth = consume_session.consume_answer(
        records=fx["stats_records"],
        player_uid=B,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        provider="CBA_OFFICIAL",
        competition="CBA",
        metric="points_per_game",
        fact=fx["fact"],
        passage=passage,
        source_text="mismatch",
        chain=chain,
        claims=[],
    )
    out_nonexistent = consume_session.consume_answer(
        records=fx["stats_records"],
        player_uid="fixture_player_NONEXISTENT",
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        provider="CBA_OFFICIAL",
        competition="CBA",
        metric="points_per_game",
        fact=fx["fact"],
        passage=passage,
        source_text="mismatch",
        chain=chain,
        claims=[],
    )

    et_unauth = out_unauth["payload"]["evidence_transparency"]
    et_nonex = out_nonexistent["payload"]["evidence_transparency"]
    assert et_unauth == et_nonex
    assert et_unauth["posture"] == "UNKNOWN"
    assert et_unauth["adopted_value"] is None
    assert "7.0" not in json.dumps(out_unauth["payload"])
    assert "subject_player_uid" not in et_unauth


def test_revocation_blocks_multi_record_conflict_research_card_even_when_stats_not_ok():
    """When stats_layer is UNIFORM_NOT_FOUND (multi-record conflict) and Fact is UNKNOWN, revoking player still blocks render_verified."""
    fx = build_cleanroom_fixture()
    consume_session, snap, passage, chain = _setup_session(fx, authorized_players={A})

    r1 = copy.deepcopy(fx["stats_records"][0])
    r1["record_id"] = "rec-c1"
    r1["metrics"]["points_per_game"] = 18.0
    r2 = copy.deepcopy(fx["stats_records"][0])
    r2["record_id"] = "rec-c2"
    r2["metrics"]["points_per_game"] = 22.0

    ans = consume_session.consume_answer(
        records=[r1, r2],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        provider="CBA_OFFICIAL",
        competition="CBA",
        metric="points_per_game",
        fact=fx["fact"],  # unbound URI source -> CANONICAL_FACT is UNIFORM_NOT_FOUND
        passage=passage,
        source_text="mismatch",  # DOCUMENT is UNIFORM_CITATION_UNKNOWN
        chain=chain,
        claims=[],
        candidate_stats_records=[r1, r2],
    )
    assert ans["payload"]["grains"]["STATS"] == dict(UNIFORM_NOT_FOUND)
    assert ans["payload"]["grains"]["CANONICAL_FACT"] == dict(UNIFORM_NOT_FOUND)
    assert ans["payload"]["evidence_transparency"]["posture"] == "TRUE_CONFLICT"

    # While authorized, research_card renders both conflicting records cleanly
    card = consume_session.render_verified(ans, kind="research_card")
    assert "TRUE_CONFLICT" in card
    assert "18.0" in card and "22.0" in card
    assert parse_rendered(card, kind="markdown")["end_marker"] is True

    # After revoking player A, render_verified refuses via evidence_transparency subject check
    consume_session.authorized_players.discard(A)
    with pytest.raises(KcConsumeError, match="ARTIFACT_STATS_SUBJECT_NOT_AUTHORIZED"):
        consume_session.render_verified(ans, kind="research_card")


def test_supersession_cycle_and_active_conflict_not_masked_and_team_splits_do_not_mask_conflict():
    """Cyclic supersession or rogue conflicting active record falls through to TRUE_CONFLICT; presence of TEAM_SPLIT never masks WHOLE_SEASON conflict."""
    fx = build_cleanroom_fixture()
    r_old = copy.deepcopy(fx["stats_records"][0])
    r_old["record_id"] = "rec-old"
    r_old["metrics"]["points_per_game"] = 12.0

    r_new = copy.deepcopy(fx["stats_records"][0])
    r_new["record_id"] = "rec-new"
    r_new["metrics"]["points_per_game"] = 14.0
    r_new["provenance"]["request_contract"] = {"fixture": True, "supersedes_record_id": "rec-old"}

    # 1) Cycle: r_old also claims to supersede r_new -> must NOT be REVISION_SUPERSEDED
    r_old_cycle = copy.deepcopy(r_old)
    r_old_cycle["provenance"]["request_contract"] = {"fixture": True, "supersedes_record_id": "rec-new"}
    res_cycle = classify_stats_evidence(
        [r_old_cycle, r_new],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        metric="points_per_game",
    )
    assert res_cycle["posture"] == "TRUE_CONFLICT"

    # 2) Rogue un-superseded conflicting active record alongside valid revision -> TRUE_CONFLICT
    r_rogue = copy.deepcopy(fx["stats_records"][0])
    r_rogue["record_id"] = "rec-rogue"
    r_rogue["metrics"]["points_per_game"] = 29.0
    res_rogue = classify_stats_evidence(
        [r_old, r_new, r_rogue],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        metric="points_per_game",
    )
    assert res_rogue["posture"] == "TRUE_CONFLICT"

    # 3) Conflicting WHOLE_SEASON records accompanied by TEAM_SPLIT records -> TRUE_CONFLICT (not SCOPE_OR_CUTOFF_DIFF)
    res_with_splits = classify_stats_evidence(
        fx["stats_records"] + [r_rogue],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        metric="points_per_game",
    )
    assert res_with_splits["posture"] == "TRUE_CONFLICT"


def test_contradictory_claims_yield_true_conflict_not_corroborated():
    """Two independent sources making contradictory statements must classify as TRUE_CONFLICT, never INDEPENDENTLY_CORROBORATED."""
    claims = [
        {"claim_id": "c1", "source_id": "outlet-a", "upstream_origin": "orig-a", "statement": "Annual salary 6.0M RMB"},
        {"claim_id": "c2", "source_id": "outlet-b", "upstream_origin": "orig-b", "statement": "Annual salary 3.5M RMB"},
    ]
    res = classify_claim_independence(claims)
    assert res["posture"] == "TRUE_CONFLICT"
    assert len(res["discrepancies"]) == 2


def test_verify_local_projection_fails_closed_when_research_card_missing(tmp_path):
    """Omitting or deleting research_card.md from local projection fails verify_local_projection."""
    out = str(tmp_path / "proj")
    build_local_projection(fixture=build_cleanroom_fixture(), output_dir=out)
    assert verify_local_projection(out)["valid"] is True

    # Delete research_card.md and even recompute manifest without it -> must still FAIL via REQUIRED_LOCAL_ARTIFACTS
    os.remove(os.path.join(out, "research_card.md"))
    manifest_path = os.path.join(out, "manifest.json")
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    manifest["files"] = [f for f in manifest["files"] if f["path"] != "research_card.md"]
    without = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    manifest["manifest_sha256"] = sha256_hex(canonical_json(without))
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, sort_keys=True, indent=2)

    verified = verify_local_projection(out)
    assert verified["valid"] is False
    assert any("research_card.md" in f or "RESEARCH_CARD_MISSING" in f for f in verified["failures"])


def test_oracle_legacy_9key_digest_parity_and_boundary_safe_scope_token_stripping():
    """Legacy OracleQuestion preserves exact 9-key dict digest; numeric scope token does not corrupt multi-digit numbers."""
    src = OracleSource(source_id="src_leg", provenance="Synthetic", rights="PUBLIC")
    q_legacy = OracleQuestion(
        question_id="q_leg",
        question_text="Games played?",
        comparison="exact",
        expected=40,
    )
    assert set(q_legacy.to_dict().keys()) == {
        "question_id",
        "question_text",
        "comparison",
        "expected",
        "unit",
        "precision",
        "tolerance",
        "alias_map",
        "mandatory_locator",
    }
    oracle_leg = build_oracle(version="v_leg", sources=[src], questions=[q_legacy])
    assert verify_manifest(oracle_leg.to_manifest()) == (True, "OK")

    # Boundary-safe scope token stripping: tok="40" must NOT turn "140.5" into "1 .5"
    q_scope = OracleQuestion(
        question_id="q_num_tok",
        question_text="PPG over 40 games?",
        comparison="rate",
        expected=12.5,
        unit="points_per_game",
        precision=1,
        tolerance=0.05,
        mandatory_locator=True,
        required_scope_tokens=("2025-2026", "40场"),
        expected_posture="QUIET_CLEAN_OFFICIAL",
        designated_locators=("fixture_doc_D1",),
    )
    oracle_ext = build_oracle(version="v_ext", sources=[src], questions=[q_scope])
    assert verify_manifest(oracle_ext.to_manifest()) == (True, "OK")
    approval = ScoringApproval(
        manifest_digest=oracle_ext.artifact_digest,
        version=oracle_ext.version,
        scoring_code_sha=scoring_code_sha(),
        snapshot_digest="b" * 64,
        authorization_domain="fixture_target",
        locator_set_digest=locator_set_digest(["fixture_doc_D1"]),
    )

    # Contradictory number 140.5 alongside scope token "40场" must fail consistency!
    bad_ans = {
        "text": "2025-2026 常规赛 40场 场均 140.5 分",
        "value": 12.5,
        "unit": "points_per_game",
        "cited_locators": ["fixture_doc_D1"],
    }
    res_bad = score_answer_approved(
        q_scope,
        bad_ans,
        oracle=oracle_ext,
        approval=approval,
        snapshot_digest="b" * 64,
        authorized_locators=["fixture_doc_D1"],
    )
    assert res_bad.verdict == "FAIL"
    assert res_bad.dimension == "consistency"

    # Valid verbatim answer & receipt pass end-to-end
    good_text = "2025-2026 常规赛 40场 场均 12.5 分"
    good_ans = {
        "text": good_text,
        "value": 12.5,
        "unit": "points_per_game",
        "cited_locators": ["fixture_doc_D1"],
    }
    res_good = score_answer_approved(
        q_scope,
        good_ans,
        oracle=oracle_ext,
        approval=approval,
        snapshot_digest="b" * 64,
        authorized_locators=["fixture_doc_D1"],
    )
    assert res_good.verdict == "PASS"

    receipt = ClientReceipt(
        engine_sha="c" * 40,
        snapshot_digest="b" * 64,
        manifest_digest=oracle_ext.artifact_digest,
        question_set_version=oracle_ext.version,
        authorization_domain="fixture_target",
        question_id="q_num_tok",
        verbatim_answer=good_text,
        cited_locators=("fixture_doc_D1",),
        receipt_mode="VERBATIM",
    )
    assert validate_receipt_approved(
        receipt,
        oracle=oracle_ext,
        approval=approval,
        snapshot_digest="b" * 64,
        authorization_domain="fixture_target",
        authorized_locators=["fixture_doc_D1"],
    ) == []
