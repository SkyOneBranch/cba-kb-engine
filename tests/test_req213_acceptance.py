"""Acceptance tests for v2.1.3 (REQ-213: Evidence Transparency & Corrigible Research).

Follows LACD v1.0.2 Mode 2.1 Acceptance Test standards.
Baseline commit: aee743cbae9f8f6c8c8ce7fa6fb21750df88344c.
Tests are collected cleanly on base commit and fail deterministically until v2.1.3 is implemented.
"""

from __future__ import annotations

import copy
import pytest

from cba_kb.kc_cleanroom import A, B, SEASON, build_cleanroom_fixture
from cba_kb.kc_consume import ConsumeSession, KcConsumeError, _content_digest
from cba_kb.kc_passages import build_routed_passage, evidence_chain
from cba_kb.kc_render import KcRenderError, parse_rendered
from cba_kb.kc_snapshot import build_fact_guarantee, build_snapshot
from cba_kb.kc_local_producer import build_local_projection, verify_local_projection
from cba_kb.research_view import ResearchView
from cba_kb.oracle import (
    ClientReceipt,
    OracleError,
    OracleQuestion,
    OracleSource,
    ScoringApproval,
    build_oracle,
    locator_set_digest,
    score_answer_approved,
    scoring_code_sha,
    validate_receipt_approved,
)


def _bound_session(fx: dict, *, player_uid: str = A):
    """Construct an entry-verified ConsumeSession aligned with tests/test_kc_v211_b01.py."""
    consume_session = ConsumeSession(
        target_domain="fixture_target",
        authorized_players={player_uid},
        authorized_sources={"fixture_doc_D1"},
        authorized_objects={"fixture_doc_D1"},
    )
    passage = build_routed_passage(
        source_id="fixture_doc_D1",
        revision="rev-1",
        locator="P3",
        text=fx["passages"][0]["text"],
        player_uid=player_uid,
        aliases=["测试甲"],
        title="fixture_doc_D1",
        authorized_url="fixture://authorized-copy/fixture_doc_D1",
    )
    probe = ConsumeSession(
        target_domain="fixture_target",
        authorized_players={player_uid},
        authorized_sources={"fixture_doc_D1"},
        authorized_objects={"fixture_doc_D1"},
    )
    probed = probe.consume_slice(
        fx["stats_records"],
        player_uid=player_uid,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        provider="CBA_OFFICIAL",
        competition="CBA",
    )
    slice_digest = _content_digest(probed["payload"])
    snap = build_snapshot(
        entries=[
            {
                "entry_id": "entry-stats",
                "sha256": slice_digest,
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
        build_provenance="simulated-build-1",
        build_time="2026-10-28T00:00:00+00:00",
        data_release="SIMULATED_DATA_RELEASE_1",
        engine_candidate="SIMULATED_ENGINE_CANDIDATE_1",
        build_code="SIMULATED_BUILD_CODE_1",
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
    fact = {
        "fact_id": "v213_fact",
        "fact_version": "v1",
        "player_uid": player_uid,
        "season": SEASON,
        "team_id": "fixture_team_T2",
        "kind": "REGISTRATION",
        "source": "fixture_doc_D1",
    }
    guarantee = build_fact_guarantee(
        fact_id=fact["fact_id"],
        fact_version="v1",
        source_id=fact["source"],
        file_version=passage["revision"],
        field_scope=sorted(fact.keys()),
        authorization_domain="fixture_target",
        snapshot_id=snap.snapshot_id,
        source_digest=passage["digest"],
    )
    return consume_session, snap, passage, chain, fact, guarantee


def _get_verdict_and_dimension(res) -> tuple[str, str | None]:
    if isinstance(res, dict):
        return str(res.get("verdict", "")), res.get("dimension")
    return str(getattr(res, "verdict", "")), getattr(res, "dimension", None)


def test_ac213_p01_research_card_rendering_and_completeness():
    """AC-213-P0-1: render_verified(kind='research_card') produces valid markdown with end_marker."""
    fx = build_cleanroom_fixture()
    consume_session, snap, passage, chain, fact, guarantee = _bound_session(fx)

    rec = copy.deepcopy(fx["stats_records"][0])
    rec["metrics"]["points_per_game"] = 18.5
    rec["metrics"]["games_played"] = 38
    rec["provenance"]["captured_at"] = "2026-03-30T10:00:00Z"
    rec["provenance"]["endpoint"] = "https://stats.cba.net.cn/records/p01"

    ans = consume_session.consume_answer(
        records=[rec],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        provider="CBA_OFFICIAL",
        competition="CBA",
        metric="points_per_game",
        fact=fact,
        passage=passage,
        source_text=passage["text"],
        chain=chain,
        claims=[],
        fact_guarantee=guarantee,
        candidate_stats_records=[rec],
    )

    card_md = consume_session.render_verified(ans, kind="research_card")
    assert isinstance(card_md, str)
    assert "18.5" in card_md
    assert "REGULAR" in card_md
    assert "WHOLE_SEASON" in card_md
    assert "https://stats.cba.net.cn/records/p01" in card_md
    assert "未取得第二来源" not in card_md
    assert "无独立佐证" not in card_md

    parsed = parse_rendered(card_md, kind="markdown")
    assert parsed.get("end_marker") is True
    lines = [ln.strip() for ln in card_md.strip().splitlines() if ln.strip()]
    assert lines[-1] == "end_marker: true"


def test_ac213_p01_render_research_card_direct_call_rejected():
    """AC-213-P0-1: Calling render_research_card without evidence_verified=True raises KcRenderError."""
    import cba_kb.kc_render as kc_render

    if not hasattr(kc_render, "render_research_card"):
        pytest.fail("kc_render.render_research_card not implemented yet")

    dummy_payload = {
        "answer_text": "Sample text",
        "metric": "points_per_game",
        "value": 12.5,
        "evidence_transparency": {"posture": "CLEAN_OFFICIAL"},
    }
    with pytest.raises(KcRenderError) as exc_info:
        kc_render.render_research_card(dummy_payload)
    assert "RESEARCH_CARD_NOT_VERIFIED" in str(exc_info.value)


def test_ac213_p01_revoked_authorization_blocks_research_card():
    """AC-213-P0-1: Revoking authorized_players before render_verified blocks research_card."""
    fx = build_cleanroom_fixture()
    consume_session, snap, passage, chain, fact, guarantee = _bound_session(fx)

    ans = consume_session.consume_answer(
        records=fx["stats_records"],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        provider="CBA_OFFICIAL",
        competition="CBA",
        metric="points_per_game",
        fact=fact,
        passage=passage,
        source_text=passage["text"],
        chain=chain,
        claims=[],
        fact_guarantee=guarantee,
    )

    consume_session.authorized_players = {B}
    with pytest.raises(KcConsumeError) as exc_info:
        consume_session.render_verified(ans, kind="research_card")
    err_str = str(exc_info.value)
    assert ("ARTIFACT_STATS_SUBJECT_NOT_AUTHORIZED" in err_str) or ("ARTIFACT_PLAYER_NOT_AUTHORIZED" in err_str)


def test_ac213_p01_build_local_projection_and_research_view(tmp_path):
    """AC-213-P0-1: build_local_projection includes research_card.md and ResearchView renders match_type."""
    fx = build_cleanroom_fixture()

    summary = build_local_projection(fixture=fx, output_dir=str(tmp_path))
    assert any("research_card.md" in f for f in summary.get("files", []))
    assert (tmp_path / "research_card.md").is_file()

    verified = verify_local_projection(str(tmp_path))
    assert verified.get("valid") is True

    rec = copy.deepcopy(fx["stats_records"][0])
    rec["match_type"] = "REGULAR"
    rec["provenance"]["captured_at"] = "2026-03-31T12:00:00Z"
    rec["provenance"]["endpoint"] = "https://stats.cba.net.cn/api/p01"

    view = ResearchView(subject_player_uid=A, stats=[rec])
    md = view.render_markdown()
    assert "REGULAR" in md
    assert "2026-03-31" in md


def test_ac213_p02_clean_official_is_quiet_and_clean():
    """AC-213-P0-2: Single clean official record yields CLEAN_OFFICIAL with empty warnings."""
    import cba_kb.kc_evidence_status as es

    fx = build_cleanroom_fixture()
    rec = copy.deepcopy(fx["stats_records"][0])
    rec["metrics"]["points_per_game"] = 14.2
    rec["provenance"]["captured_at"] = "2026-04-01T08:00:00Z"
    rec["provenance"]["endpoint"] = "https://stats.cba.net.cn/p/clean"

    res = es.classify_stats_evidence(
        [rec],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        metric="points_per_game",
    )
    assert res["posture"] == "CLEAN_OFFICIAL"
    assert res["adopted_value"] == 14.2
    assert res["warnings"] == []
    assert res["discrepancies"] == []
    assert res["scope_meta"]["season"] == SEASON
    assert res["scope_meta"]["locator"] == "https://stats.cba.net.cn/p/clean"


def test_ac213_p02_identical_duplicate_and_scope_cutoff_not_conflict():
    """AC-213-P0-2: Duplicate records and scope/cutoff splits are not treated as TRUE_CONFLICT."""
    import cba_kb.kc_evidence_status as es

    fx = build_cleanroom_fixture()
    rec1 = copy.deepcopy(fx["stats_records"][0])
    rec1["metrics"]["points_per_game"] = 12.5
    rec1["record_id"] = "rec-dup-001"

    rec2 = copy.deepcopy(fx["stats_records"][0])
    rec2["metrics"]["points_per_game"] = 12.5
    rec2["record_id"] = "rec-dup-002"

    res_dup = es.classify_stats_evidence(
        [rec1, rec2],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        metric="points_per_game",
    )
    assert res_dup["posture"] == "IDENTICAL_DUPLICATE"
    assert res_dup["adopted_value"] == 12.5
    assert res_dup["posture"] != "TRUE_CONFLICT"

    rec_mid = copy.deepcopy(fx["stats_records"][0])
    rec_mid["record_id"] = "rec-mid-01"
    rec_mid["metrics"]["games_played"] = 20
    rec_mid["metrics"]["points_per_game"] = 10.0
    rec_mid["provenance"]["captured_at"] = "2026-01-15T00:00:00Z"
    rec_mid["provenance"]["endpoint"] = "loc-mid"

    rec_full = copy.deepcopy(fx["stats_records"][0])
    rec_full["record_id"] = "rec-full-02"
    rec_full["metrics"]["games_played"] = 42
    rec_full["metrics"]["points_per_game"] = 15.0
    rec_full["provenance"]["captured_at"] = "2026-04-10T00:00:00Z"
    rec_full["provenance"]["endpoint"] = "loc-full"

    res_cutoff = es.classify_stats_evidence(
        [rec_mid, rec_full],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        metric="points_per_game",
    )
    assert res_cutoff["posture"] == "SCOPE_OR_CUTOFF_DIFF"
    assert res_cutoff["posture"] != "TRUE_CONFLICT"


def test_ac213_p02_data_discrepancy_and_true_conflict():
    """AC-213-P0-2: Summary vs game log discrepancy calculates delta; contradictory records yield TRUE_CONFLICT."""
    import cba_kb.kc_evidence_status as es

    fx = build_cleanroom_fixture()
    rec_summary = copy.deepcopy(fx["stats_records"][0])
    rec_summary["record_id"] = "rec-sum-01"
    rec_summary["provenance"]["request_contract"] = {"fixture": True, "record_kind": "OFFICIAL_SUMMARY"}
    rec_summary["metrics"]["games_played"] = 40
    rec_summary["metrics"]["points_per_game"] = 20.0

    rec_gamelog = copy.deepcopy(fx["stats_records"][0])
    rec_gamelog["record_id"] = "rec-log-01"
    rec_gamelog["provenance"]["request_contract"] = {"fixture": True, "record_kind": "GAME_LOG_SUM"}
    rec_gamelog["metrics"]["games_played"] = 40
    rec_gamelog["metrics"]["points_per_game"] = 19.5

    res_disc = es.classify_stats_evidence(
        [rec_summary, rec_gamelog],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        metric="points_per_game",
    )
    assert res_disc["posture"] == "DATA_DISCREPANCY"
    assert res_disc["adopted_value"] is None
    assert len(res_disc["discrepancies"]) == 2
    delta = abs(res_disc["discrepancies"][0]["value"] - res_disc["discrepancies"][1]["value"])
    assert round(delta, 2) == 0.5

    rec_alt = copy.deepcopy(rec_summary)
    rec_alt["record_id"] = "rec-conflict-02"
    rec_alt["provenance"]["request_contract"] = {"fixture": True, "record_kind": "OFFICIAL_SUMMARY"}
    rec_alt["metrics"]["points_per_game"] = 28.0

    res_conflict = es.classify_stats_evidence(
        [rec_summary, rec_alt],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        metric="points_per_game",
    )
    assert res_conflict["posture"] == "TRUE_CONFLICT"
    assert res_conflict["adopted_value"] is None
    assert len(res_conflict["discrepancies"]) == 2


def test_ac213_p02_claim_independence_syndication_and_unknown_kinds():
    """AC-213-P0-2: Syndicated claims with same upstream_origin deduplicate to SINGLE_SOURCE; UNKNOWN subkinds tested."""
    import cba_kb.kc_evidence_status as es

    claims_syndicated = [
        {"claim_id": "c1", "source_id": "outlet-alpha", "upstream_origin": "wire-orig-99", "statement": "Contract signed"},
        {"claim_id": "c2", "source_id": "outlet-beta", "upstream_origin": "wire-orig-99", "statement": "Contract signed"},
    ]
    res_single = es.classify_claim_independence(claims_syndicated)
    assert res_single["posture"] == "SINGLE_SOURCE_UNCORROBORATED"

    claims_independent = [
        {"claim_id": "c1", "source_id": "outlet-alpha", "upstream_origin": "wire-alpha-01", "statement": "Contract signed"},
        {"claim_id": "c3", "source_id": "club-press-release", "upstream_origin": "club-official-01", "statement": "Contract signed"},
    ]
    res_indep = es.classify_claim_independence(claims_independent)
    assert res_indep["posture"] == "INDEPENDENTLY_CORROBORATED"

    res_unk_gap = es.classify_stats_evidence(
        [],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        metric="points_per_game",
    )
    assert res_unk_gap["posture"] == "UNKNOWN"
    assert res_unk_gap["unknown_subkind"] == "NO_RELIABLE_EVIDENCE_IN_KB"

    res_unk_unpub = es.classify_stats_evidence(
        [],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        metric="points_per_game",
        unknown_basis="CONFIRMED_UNPUBLISHED",
    )
    assert res_unk_unpub["posture"] == "UNKNOWN"
    assert res_unk_unpub["unknown_subkind"] == "CONFIRMED_UNPUBLISHED"


def test_ac213_p03_oracle_scope_tokens_enforcement():
    """AC-213-P0-3: OracleQuestion with required_scope_tokens rejects answers missing scope tokens."""
    source = OracleSource(source_id="src_01", provenance="Synthetic Scope Auth", rights="LAWFUL_PUBLIC_SYNTHETIC")
    q = OracleQuestion(
        question_id="q_scope_01",
        question_text="Points per game in regular season?",
        comparison="rate",
        expected=12.5,
        unit="points_per_game",
        precision=1,
        tolerance=0.05,
        mandatory_locator=True,
        required_scope_tokens=("2025-2026", "REGULAR", "WHOLE_SEASON"),
    )
    oracle = build_oracle(version="v213_synth_01", sources=[source], questions=[q])
    approval = ScoringApproval(
        manifest_digest=oracle.artifact_digest,
        version=oracle.version,
        scoring_code_sha=scoring_code_sha(),
        snapshot_digest="a" * 64,
        authorization_domain="synth_domain_213",
        locator_set_digest=locator_set_digest(["https://cba.example.com/scope"]),
    )

    res_fail = score_answer_approved(
        q,
        {
            "text": "Player averaged 12.5 points per game.",
            "value": 12.5,
            "unit": "points_per_game",
            "cited_locators": ["https://cba.example.com/scope"],
        },
        oracle=oracle,
        approval=approval,
        snapshot_digest="a" * 64,
        authorized_locators=["https://cba.example.com/scope"],
    )
    verdict, dimension = _get_verdict_and_dimension(res_fail)
    assert verdict == "FAIL"
    assert dimension == "scope"

    res_pass = score_answer_approved(
        q,
        {
            "text": "In 2025-2026 REGULAR season WHOLE_SEASON scope player averaged 12.5 points per game.",
            "value": 12.5,
            "unit": "points_per_game",
            "cited_locators": ["https://cba.example.com/scope"],
        },
        oracle=oracle,
        approval=approval,
        snapshot_digest="a" * 64,
        authorized_locators=["https://cba.example.com/scope"],
    )
    verdict_ok, _ = _get_verdict_and_dimension(res_pass)
    assert verdict_ok == "PASS"


def test_ac213_p03_oracle_designated_locators_enforcement():
    """AC-213-P0-3: Answers citing authorized locators but missing designated_locators are rejected."""
    source = OracleSource(source_id="src_01", provenance="Synthetic Auth", rights="LAWFUL_PUBLIC_SYNTHETIC")
    q = OracleQuestion(
        question_id="q_loc_01",
        question_text="Query points per game?",
        comparison="rate",
        expected=12.5,
        unit="points_per_game",
        precision=1,
        tolerance=0.05,
        mandatory_locator=True,
        designated_locators=("https://cba.example.com/designated",),
    )
    oracle = build_oracle(version="v213_synth_02", sources=[source], questions=[q])
    approval = ScoringApproval(
        manifest_digest=oracle.artifact_digest,
        version=oracle.version,
        scoring_code_sha=scoring_code_sha(),
        snapshot_digest="a" * 64,
        authorization_domain="synth_domain_213",
        locator_set_digest=locator_set_digest([
            "https://cba.example.com/designated",
            "https://cba.example.com/other",
        ]),
    )

    res_miss = score_answer_approved(
        q,
        {
            "text": "Player averaged 12.5 points per game.",
            "value": 12.5,
            "unit": "points_per_game",
            "cited_locators": ["https://cba.example.com/other"],
        },
        oracle=oracle,
        approval=approval,
        snapshot_digest="a" * 64,
        authorized_locators=[
            "https://cba.example.com/designated",
            "https://cba.example.com/other",
        ],
    )
    verdict_miss, dimension_miss = _get_verdict_and_dimension(res_miss)
    assert verdict_miss == "FAIL"
    assert dimension_miss == "locator"

    res_hit = score_answer_approved(
        q,
        {
            "text": "Player averaged 12.5 points per game.",
            "value": 12.5,
            "unit": "points_per_game",
            "cited_locators": ["https://cba.example.com/designated"],
        },
        oracle=oracle,
        approval=approval,
        snapshot_digest="a" * 64,
        authorized_locators=[
            "https://cba.example.com/designated",
            "https://cba.example.com/other",
        ],
    )
    verdict_hit, _ = _get_verdict_and_dimension(res_hit)
    assert verdict_hit == "PASS"


def test_ac213_p03_oracle_expected_postures_and_receipt_validation():
    """AC-213-P0-3: Enforce expected_posture rules and validate_receipt_approved receipt_mode checks."""
    source = OracleSource(source_id="src_01", provenance="Synthetic Auth", rights="LAWFUL_PUBLIC_SYNTHETIC")
    q_clean = OracleQuestion(
        question_id="q_clean_01",
        question_text="Points per game?",
        comparison="rate",
        expected=12.5,
        unit="points_per_game",
        precision=1,
        tolerance=0.05,
        mandatory_locator=True,
        expected_posture="QUIET_CLEAN_OFFICIAL",
    )
    q_gap = OracleQuestion(
        question_id="q_gap_01",
        question_text="Salary amount?",
        comparison="unknown",
        expected=None,
        unit="none",
        precision=0,
        tolerance=0.0,
        mandatory_locator=True,
        expected_posture="LOUD_UNKNOWN_KB_GAP",
    )
    oracle = build_oracle(version="v213_synth_03", sources=[source], questions=[q_clean, q_gap])
    approval = ScoringApproval(
        manifest_digest=oracle.artifact_digest,
        version=oracle.version,
        scoring_code_sha=scoring_code_sha(),
        snapshot_digest="a" * 64,
        authorization_domain="synth_domain_213",
        locator_set_digest=locator_set_digest(["https://cba.example.com/posture"]),
    )

    res_noisy = score_answer_approved(
        q_clean,
        {
            "text": "12.5 points per game (注意：未取得第二来源佐证)",
            "value": 12.5,
            "unit": "points_per_game",
            "cited_locators": ["https://cba.example.com/posture"],
        },
        oracle=oracle,
        approval=approval,
        snapshot_digest="a" * 64,
        authorized_locators=["https://cba.example.com/posture"],
    )
    verdict_noisy, dim_noisy = _get_verdict_and_dimension(res_noisy)
    assert verdict_noisy == "FAIL"
    assert dim_noisy == "posture"

    res_falsely_zero = score_answer_approved(
        q_gap,
        {
            "text": "暂无可确认金额，但该球员实际薪资为0元且确实未公开",
            "value": None,
            "unit": "none",
            "cited_locators": ["https://cba.example.com/posture"],
        },
        oracle=oracle,
        approval=approval,
        snapshot_digest="a" * 64,
        authorized_locators=["https://cba.example.com/posture"],
    )
    verdict_zero, dim_zero = _get_verdict_and_dimension(res_falsely_zero)
    assert verdict_zero == "FAIL"
    assert dim_zero == "posture"

    bad_receipt = ClientReceipt(
        engine_sha="a" * 40,
        snapshot_digest="a" * 64,
        manifest_digest=oracle.artifact_digest,
        question_set_version=oracle.version,
        authorization_domain="synth_domain_213",
        question_id="q_clean_01",
        verbatim_answer="REAL_CLIENT_NOT_EXECUTED: summary",
        cited_locators=("https://cba.example.com/posture",),
        receipt_mode="SUMMARY_ONLY",
    )
    errs = validate_receipt_approved(
        bad_receipt,
        oracle=oracle,
        approval=approval,
        snapshot_digest="a" * 64,
        authorization_domain="synth_domain_213",
        authorized_locators=["https://cba.example.com/posture"],
    )
    assert len(errs) > 0


def test_ac213_p1_revision_superseded_stats_and_claim_rendered():
    """AC-213-P1: Explicit supersedes revisions preserve prior history and render both values in card."""
    import cba_kb.kc_evidence_status as es

    fx = build_cleanroom_fixture()
    rec_prior = copy.deepcopy(fx["stats_records"][0])
    rec_prior["record_id"] = "rec-rev-prior-01"
    rec_prior["metrics"]["points_per_game"] = 12.0
    rec_prior["provenance"]["captured_at"] = "2026-03-01T00:00:00Z"
    rec_prior["provenance"]["endpoint"] = "loc-prior"

    rec_revised = copy.deepcopy(fx["stats_records"][0])
    rec_revised["record_id"] = "rec-rev-new-02"
    rec_revised["metrics"]["points_per_game"] = 13.5
    rec_revised["provenance"]["request_contract"] = {
        "fixture": True,
        "supersedes_record_id": "rec-rev-prior-01",
        "revision_reason": "Official scorekeeping audit correction",
    }
    rec_revised["provenance"]["captured_at"] = "2026-03-05T00:00:00Z"
    rec_revised["provenance"]["endpoint"] = "loc-revised"

    res_stat = es.classify_stats_evidence(
        [rec_prior, rec_revised],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        metric="points_per_game",
    )
    assert res_stat["posture"] == "REVISION_SUPERSEDED"
    assert res_stat["adopted_value"] == 13.5
    assert len(res_stat["revision_history"]) == 1
    assert res_stat["revision_history"][0]["prior_value"] == 12.0
    assert res_stat["revision_history"][0]["new_value"] == 13.5

    claims = [
        {"claim_id": "c-old", "status": "superseded", "statement": "Contract expires 2026"},
        {"claim_id": "c-new", "status": "active", "prior_claim_id": "c-old", "statement": "Contract extended to 2028"},
    ]
    res_claim = es.classify_claim_independence(claims)
    assert res_claim["posture"] == "REVISION_SUPERSEDED"
    assert len(res_claim["revision_history"]) >= 1

    consume_session, snap, passage, chain, fact, guarantee = _bound_session(fx)
    ans = consume_session.consume_answer(
        records=[rec_revised],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        provider="CBA_OFFICIAL",
        competition="CBA",
        metric="points_per_game",
        fact=fact,
        passage=passage,
        source_text=passage["text"],
        chain=chain,
        claims=[],
        fact_guarantee=guarantee,
        candidate_stats_records=[rec_prior, rec_revised],
    )
    card_md = consume_session.render_verified(ans, kind="research_card")
    assert "13.5" in card_md
    assert "12.0" in card_md
    assert ("REVISION" in card_md) or ("SUPERSEDED" in card_md)
