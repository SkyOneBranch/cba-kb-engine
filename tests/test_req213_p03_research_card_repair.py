"""Targeted falsification and wiring regression tests for REQ-213 research_card stats posture alignment.

Verifies that when `ConsumeSession.consume_answer` resolves `grains.STATS` as `OK`
(e.g., `scope="WHOLE_SEASON"` with a caller-supplied `team_id`, `int` vs `str` `team_id`
on `TEAM_SPLIT`, or an empty `candidate_stats_records=[]`), `evidence_transparency` and
`render_verified(..., kind="research_card")` consistently emit `CLEAN_OFFICIAL` and the
verified metric value rather than contradicting `STATS=OK` with `Posture: UNKNOWN`.
"""

from __future__ import annotations

import copy

from cba_kb.kc_cleanroom import A, SEASON, build_cleanroom_fixture
from cba_kb.kc_evidence_status import classify_stats_evidence
from cba_kb.kc_render import parse_rendered
from tests.test_req213_acceptance import _bound_session


def test_whole_season_research_card_not_marked_unknown_when_team_id_supplied():
    fx = build_cleanroom_fixture()
    session, _snap, passage, chain, fact, guarantee = _bound_session(fx)

    rec = copy.deepcopy(fx["stats_records"][0])
    rec["metrics"]["points_per_game"] = 19.8
    rec["metrics"]["games_played"] = 41
    rec["provenance"]["captured_at"] = "2026-10-04T00:00:00Z"
    rec["provenance"]["endpoint"] = "https://stats.cba.net.cn/synth/p213"

    ans = session.consume_answer(
        records=[rec],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        team_id="fixture_team_T2",
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

    assert ans["payload"]["grains"]["STATS"]["status"] == "OK"
    assert ans["payload"]["grains"]["STATS"]["value"] == 19.8
    assert ans["payload"]["evidence_transparency"]["posture"] == "CLEAN_OFFICIAL"
    assert ans["payload"]["evidence_transparency"]["adopted_value"] == 19.8

    card_md = session.render_verified(ans, kind="research_card")
    assert "- Posture: CLEAN_OFFICIAL" in card_md
    assert "- Metric: points_per_game = 19.8" in card_md
    assert "Posture: UNKNOWN" not in card_md
    assert "NO_RELIABLE_EVIDENCE_IN_KB" not in card_md
    assert parse_rendered(card_md, kind="markdown")["end_marker"] is True


def test_team_split_normalizes_int_and_str_team_id_and_rejects_other_team():
    fx = build_cleanroom_fixture()
    split_rec = copy.deepcopy(fx["stats_records"][1])  # TEAM_SPLIT fixture record
    split_rec["team_id"] = 30020
    split_rec["metrics"]["points_per_game"] = 16.4

    matched = classify_stats_evidence(
        [split_rec],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="TEAM_SPLIT",
        team_id="30020",
        provider="CBA_OFFICIAL",
        competition="CBA",
        metric="points_per_game",
    )
    assert matched["posture"] == "CLEAN_OFFICIAL"
    assert matched["adopted_value"] == 16.4

    other_team = classify_stats_evidence(
        [split_rec],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="TEAM_SPLIT",
        team_id="99999",
        provider="CBA_OFFICIAL",
        competition="CBA",
        metric="points_per_game",
    )
    assert other_team["posture"] == "UNKNOWN"
    assert other_team["adopted_value"] is None


def test_empty_candidate_stats_records_does_not_override_verified_records_to_unknown():
    fx = build_cleanroom_fixture()
    session, _snap, passage, chain, fact, guarantee = _bound_session(fx)

    rec = copy.deepcopy(fx["stats_records"][0])
    rec["metrics"]["points_per_game"] = 21.3

    ans = session.consume_answer(
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
        candidate_stats_records=[],
    )

    assert ans["payload"]["grains"]["STATS"]["status"] == "OK"
    assert ans["payload"]["evidence_transparency"]["posture"] == "CLEAN_OFFICIAL"
    card_md = session.render_verified(ans, kind="research_card")
    assert "- Posture: CLEAN_OFFICIAL" in card_md
    assert "- Metric: points_per_game = 21.3" in card_md
    assert "Posture: UNKNOWN" not in card_md

    # Genuine empty records must still return UNKNOWN fail-closed
    ans_empty = session.consume_answer(
        records=[],
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
        candidate_stats_records=[],
    )
    assert ans_empty["payload"]["grains"]["STATS"]["status"] == "UNKNOWN"
    assert ans_empty["payload"]["evidence_transparency"]["posture"] == "UNKNOWN"
