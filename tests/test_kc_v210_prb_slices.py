"""PR-B (Issue #220) slice tests — written FIRST, must FAIL on old HEAD.

New module cba_kb.kc_slices does not exist on base 15867026, so these
tests fail at import on old HEAD (recorded honestly; a bare import
failure alone is NOT claimed as capability falsification — the
full12 file carries the AssertionError-based capability falsification).
Clean-room fixture values only (fictitious). No private data, no real
Drive/ACL data, no production credentials. All artifacts SIMULATED.
"""

import pytest

from cba_kb.kc_cleanroom import A, B, SEASON, build_cleanroom_fixture
from cba_kb.kc_slices import (
    aggregate_unavailable,
    build_slice,
    build_slice_catalog,
    entity_candidates,
    query_slice_metric,
)

PROVIDER = "CBA_OFFICIAL"
COMPETITION = "CBA"


@pytest.fixture(scope="module")
def fx():
    return build_cleanroom_fixture()


def test_alias_resolves_to_all_visible_candidates_single_owner(fx):
    r = entity_candidates(fx["players"], fx["aliases"], query="测试甲",
                          query_kind="alias", authorized_ids={A, B})
    assert r["candidates"] == [A]
    assert r["status"] == "RESOLVED"
    assert r["clarification_required"] is False


def test_canonical_name_returns_all_candidates_with_clarification(fx):
    r = entity_candidates(fx["players"], fx["aliases"], query="林测试",
                          query_kind="name", authorized_ids={A, B})
    assert r["candidates"] == [A, B]
    assert r["status"] == "CLARIFICATION_REQUIRED"
    assert r["clarification_required"] is True
    assert r["relation"] == "UNDECIDED"


def test_unknown_alias_is_unknown_not_a_guess(fx):
    r = entity_candidates(fx["players"], fx["aliases"], query="不存在",
                          query_kind="alias", authorized_ids={A, B})
    assert r["status"] == "UNKNOWN"
    assert r["candidates"] == []


def test_whole_season_slice_complete_key_and_values(fx):
    sl = build_slice(fx["stats_records"], player_uid=A, season=SEASON,
                     match_type="REGULAR", scope="WHOLE_SEASON",
                     provider=PROVIDER, competition=COMPETITION, authorized_ids={A, B})
    assert sl["player_uid"] == A
    assert sl["provider"] == PROVIDER and sl["competition"] == COMPETITION
    assert sl["season"] == SEASON and sl["match_type"] == "REGULAR"
    assert sl["scope"] == "WHOLE_SEASON" and sl["team_id"] is None
    assert sl["metrics"]["points_per_game"]["value"] == 12.5
    assert sl["metrics"]["games_played"]["value"] == 40
    assert sl["complete"] is True and sl["end_marker"] is True
    assert "points_per_game" in sl["supported_metrics"]
    assert sl["row_count"] == 1


def test_zero_is_observed_and_null_is_unknown_never_coerced(fx):
    sl = build_slice(fx["stats_records"], player_uid=A, season=SEASON,
                     match_type="REGULAR", scope="WHOLE_SEASON",
                     provider=PROVIDER, competition=COMPETITION, authorized_ids={A, B})
    assert sl["metrics"]["blocks_per_game"]["value"] == 0
    assert sl["metrics"]["blocks_per_game"]["value"] is not None
    assert sl["metrics"]["steals_per_game"]["value"] is None


def test_whole_and_split_are_never_summed(fx):
    whole = build_slice(fx["stats_records"], player_uid=A, season=SEASON,
                        match_type="REGULAR", scope="WHOLE_SEASON",
                        provider=PROVIDER, competition=COMPETITION, authorized_ids={A, B})
    assert whole["metrics"]["games_played"]["value"] == 40
    assert "total_games" not in whole["metrics"]
    t1 = build_slice(fx["stats_records"], player_uid=A, season=SEASON,
                     match_type="REGULAR", scope="TEAM_SPLIT",
                     team_id="fixture_team_T1",
                     provider=PROVIDER, competition=COMPETITION, authorized_ids={A, B})
    t2 = build_slice(fx["stats_records"], player_uid=A, season=SEASON,
                     match_type="REGULAR", scope="TEAM_SPLIT",
                     team_id="fixture_team_T2",
                     provider=PROVIDER, competition=COMPETITION, authorized_ids={A, B})
    assert t1["metrics"]["points_per_game"]["value"] == 10.0
    assert t2["metrics"]["points_per_game"]["value"] == 15.0


def test_team_split_without_team_id_is_rejected(fx):
    with pytest.raises(ValueError):
        build_slice(fx["stats_records"], player_uid=A, season=SEASON,
                    match_type="REGULAR", scope="TEAM_SPLIT",
                    provider=PROVIDER, competition=COMPETITION, authorized_ids={A, B})


def test_omitted_provider_or_competition_is_a_type_error(fx):
    with pytest.raises(TypeError):
        build_slice(fx["stats_records"], player_uid=A, season=SEASON,
                    match_type="REGULAR", scope="WHOLE_SEASON",
                    authorized_ids={A, B})
    with pytest.raises(TypeError):
        build_slice(fx["stats_records"], player_uid=A, season=SEASON,
                    match_type="REGULAR", scope="WHOLE_SEASON",
                    provider=PROVIDER, authorized_ids={A, B})


def test_missing_metric_is_unknown_never_guessed(fx):
    sl = build_slice(fx["stats_records"], player_uid=A, season=SEASON,
                     match_type="REGULAR", scope="WHOLE_SEASON",
                     provider=PROVIDER, competition=COMPETITION, authorized_ids={A, B})
    r = query_slice_metric(sl, "assists_per_game")
    assert r["status"] == "UNKNOWN"
    assert r["value"] is None


def test_empty_slice_is_marked_empty_but_lookup_completed(fx):
    sl = build_slice(fx["stats_records"], player_uid=A, season="1999-2000",
                     match_type="REGULAR", scope="WHOLE_SEASON",
                     provider=PROVIDER, competition=COMPETITION, authorized_ids={A, B})
    assert sl["status"] == "EMPTY"
    assert sl["row_count"] == 0
    assert sl["complete"] is True and sl["end_marker"] is True


def test_slice_catalog_detects_missing_slice(fx):
    expected = [
        (SEASON, "REGULAR", "WHOLE_SEASON", None),
        (SEASON, "REGULAR", "TEAM_SPLIT", "fixture_team_T1"),
        (SEASON, "PLAYOFF", "WHOLE_SEASON", None),
        ("1999-2000", "REGULAR", "WHOLE_SEASON", None),
    ]
    cat = build_slice_catalog(fx["stats_records"], player_uid=A,
                              expected_keys=expected,
                              provider=PROVIDER, competition=COMPETITION, authorized_ids={A, B})
    assert cat["complete"] is False and cat["end_marker"] is False
    assert ("1999-2000", "REGULAR", "WHOLE_SEASON", None) in cat["missing"]


def test_slice_catalog_complete_when_nothing_missing(fx):
    expected = [
        (SEASON, "REGULAR", "WHOLE_SEASON", None),
        (SEASON, "PLAYOFF", "WHOLE_SEASON", None),
    ]
    cat = build_slice_catalog(fx["stats_records"], player_uid=A,
                              expected_keys=expected,
                              provider=PROVIDER, competition=COMPETITION, authorized_ids={A, B})
    assert cat["complete"] is True and cat["end_marker"] is True
    assert cat["missing"] == []


def test_unmaterialized_aggregation_reports_unavailable(fx):
    r = aggregate_unavailable(metric="points_per_game",
                              scope="WHOLE_SEASON")
    assert r["status"] == "NOT_APPLICABLE"
    assert r["reason"] == "UNMATERIALIZED_AGGREGATION_UNAVAILABLE"
    assert "value" not in r


# ---- Repair round 1 (Q2 FAIL response): authorization boundary (R5) ----
# Repair-falsification bridge used below: on pre-repair code these
# builders have no authorized_ids parameter, so the new-style call raises
# TypeError; the except branch then asserts the required behavior on the
# old call, which fails behaviorally (unauthorized data leaks / no gate).
# On repaired code the try branch runs and asserts filtering.

def test_authorized_view_hides_unauthorized_name_candidate(fx):
    try:
        r = entity_candidates(fx["players"], fx["aliases"], query="林测试",
                              query_kind="name", authorized_ids={A})
    except TypeError:
        r = entity_candidates(fx["players"], fx["aliases"], query="林测试",
                              query_kind="name")
        assert B not in r["candidates"], (
            "pre-repair routing leaks unauthorized UID into candidates")
        return
    assert r["candidates"] == [A]
    assert B not in r["candidates"]


def test_alias_for_unauthorized_owner_is_unknown(fx):
    try:
        r = entity_candidates(fx["players"], fx["aliases"], query="测试甲",
                              query_kind="alias", authorized_ids={B})
    except TypeError:
        r = entity_candidates(fx["players"], fx["aliases"], query="测试甲",
                              query_kind="alias")
        assert r["candidates"] == [], (
            "pre-repair routing resolves an alias whose owner is not "
            "authorized")
        return
    assert r["status"] == "UNKNOWN"
    assert r["candidates"] == []


def test_build_slice_for_unauthorized_player_is_rejected(fx):
    try:
        with pytest.raises(ValueError):
            build_slice(fx["stats_records"], player_uid=A, season=SEASON,
                        match_type="REGULAR", scope="WHOLE_SEASON",
                        provider=PROVIDER, competition=COMPETITION,
                        authorized_ids={B})
    except TypeError:
        build_slice(fx["stats_records"], player_uid=A, season=SEASON,
                    match_type="REGULAR", scope="WHOLE_SEASON",
                    provider=PROVIDER, competition=COMPETITION)
        assert False, "pre-repair build_slice has no authorization gate"


def test_slice_catalog_for_unauthorized_player_is_rejected(fx):
    try:
        with pytest.raises(ValueError):
            build_slice_catalog(
                fx["stats_records"], player_uid=A,
                expected_keys=[(SEASON, "REGULAR", "WHOLE_SEASON", None)],
                provider=PROVIDER, competition=COMPETITION,
                authorized_ids={B})
    except TypeError:
        build_slice_catalog(
            fx["stats_records"], player_uid=A,
            expected_keys=[(SEASON, "REGULAR", "WHOLE_SEASON", None)],
            provider=PROVIDER, competition=COMPETITION)
        assert False, "pre-repair build_slice_catalog has no authorization gate"
