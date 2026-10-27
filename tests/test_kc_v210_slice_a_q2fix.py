"""CBA-KB v2.1.0 slice A — Q2 FAIL regression tests (PR #216, Q2 round 1).

Bounded fix only, no re-freeze: frozen binding REQ-210-KNOWLEDGE-
CONSUMPTION-01, LACD_v1.0.2, main spec SHA-256
ee4bd5338edba757fd2764df563fe21600d3a3313bb8dab2ec2416d840b3ef09,
addendum SHA-256
3230146ab7668972850a63ea9a3bbc31e125b5fcaaeb2f37c757ea861f3f9071.

These tests were written FIRST against the unfixed slice-A code and
must FAIL there for the three Q2 BLOCKERs (grain override, provider/
competition dimension, multi-owner alias). Follow-up hardening tests
(NaN/inf, slice counts, synthetic two-character case) are regression
guards; some of them may already pass on unfixed code and are recorded
as such, not as falsification evidence.

Synthetic data only. The two-character "陈岚" case in this file is
SYNTHETIC, consistent with existing fixture conventions — the
addendum gives no UID or value for a real 陈岚 record in the material
available to this fix, so no frozen real value is asserted here. That
frozen-real-value question remains NOT_VERIFIED.
"""

import copy
import math

import pytest

from cba_kb.kc_cleanroom import build_cleanroom_fixture
from cba_kb.kc_projection import (
    KcError,
    build_passage,
    query_stats,
    resolve_identity,
    route_passages,
    route_uid_slices,
    typed_result,
    validate_metric_value,
)

A = "fixture_player_0001"
B = "fixture_player_0002"
SEASON = "2025-2026"
QUOTE = "我每天训练一百次罚球。"

# Synthetic two-character case only (see module docstring).
CHEN_LAN = "fixture_player_0003"


@pytest.fixture(scope="module")
def fx():
    return build_cleanroom_fixture()


def _canonical_a_regular_whole(fx):
    for record in fx["stats_records"]:
        if (record["player_uid"] == A and record["season"] == SEASON
                and record["match_type"] == "REGULAR"
                and record["scope"] == "WHOLE_SEASON"):
            return record
    raise AssertionError("fixture canonical A record missing")


# ------------------------------------------------------- Q2 BLOCKER 1: grain
def test_q2fix_grain_payload_cannot_override_authoritative():
    # Exact Q2 counterexample: payload grain DOCUMENT must not overwrite
    # authoritative STATS.
    result = typed_result(
        caller_grain="DOCUMENT", authoritative_grain="STATS",
        payload={"grain": "DOCUMENT", "value": 12.5})
    assert result["grain"] == "STATS"
    assert result["value"] == 12.5


def test_q2fix_grain_payload_claim_cannot_spoof_fact():
    result = typed_result(
        caller_grain="CLAIM", authoritative_grain="CANONICAL_FACT",
        payload={"grain": "CLAIM", "fact_id": "fixture_fact_registration_A"})
    assert result["grain"] == "CANONICAL_FACT"
    # A payload without a grain key must also end up authoritative.
    plain = typed_result(
        caller_grain="UNKNOWN", authoritative_grain="IDENTITY",
        payload={"candidates": [A]})
    assert plain["grain"] == "IDENTITY"


# ------------------------------------------- Q2 BLOCKER 2: provider/competition
def test_q2fix_query_stats_provider_competition_are_exact_dimensions(fx):
    canonical = _canonical_a_regular_whole(fx)
    other = copy.deepcopy(canonical)
    other["provider"] = "OTHER_PROVIDER"
    other["competition"] = "OTHER_LEAGUE"
    other["record_id"] = "stats:OTHER_PROVIDER:2025-2026:REGULAR:WHOLE_SEASON:fixture_player_0001"
    other["metrics"] = dict(canonical["metrics"], points_per_game=99.0)
    records = [canonical, other]

    # Default (canonical) binding must return the canonical value only:
    # no false CONFLICT, no cross-fallback to 99.0.
    default = query_stats(records, player_uid=A, season=SEASON,
                          match_type="REGULAR", scope="WHOLE_SEASON",
                          metric="points_per_game")
    assert default["status"] == "OK" and default["value"] == 12.5
    assert default["key"]["provider"] == "CBA_OFFICIAL"
    assert default["key"]["competition"] == "CBA"

    # Explicit canonical binding agrees.
    explicit = query_stats(records, player_uid=A, season=SEASON,
                           match_type="REGULAR", scope="WHOLE_SEASON",
                           metric="points_per_game",
                           provider="CBA_OFFICIAL", competition="CBA")
    assert explicit["status"] == "OK" and explicit["value"] == 12.5

    # Explicit non-canonical binding must NOT fall back to the canonical
    # record and must NOT return the other provider's value as a verified
    # canonical answer: it is UNKNOWN for this binding.
    other_binding = query_stats(records, player_uid=A, season=SEASON,
                                match_type="REGULAR", scope="WHOLE_SEASON",
                                metric="points_per_game",
                                provider="OTHER_PROVIDER",
                                competition="OTHER_LEAGUE")
    assert other_binding["status"] == "UNKNOWN"
    assert other_binding.get("value") != 99.0


def test_q2fix_query_stats_missing_provider_dimension_is_not_a_match(fx):
    # A record with no provider/competition fields at all must not match
    # the canonical binding (missing dimension -> UNKNOWN, not a guess).
    canonical = _canonical_a_regular_whole(fx)
    stripped = copy.deepcopy(canonical)
    stripped.pop("provider", None)
    stripped.pop("competition", None)
    result = query_stats([stripped], player_uid=A, season=SEASON,
                         match_type="REGULAR", scope="WHOLE_SEASON",
                         metric="points_per_game")
    assert result["status"] == "UNKNOWN"


# ------------------------------------------------- Q2 BLOCKER 3: multi-owner alias
def test_q2fix_multi_owner_approved_alias_requires_clarification():
    aliases = [
        {"alias": "共享名", "player_uid": A, "approved": True},
        {"alias": "共享名", "player_uid": B, "approved": True},
    ]
    result = resolve_identity([], aliases, query="共享名", query_kind="alias")
    assert result["status"] == "CLARIFICATION_REQUIRED"
    assert result["candidates"] == [A, B]
    assert result["relation"] == "UNDECIDED"
    assert "value" not in result  # no single picked answer


def test_q2fix_single_owner_alias_still_resolves(fx):
    # The fix must not break the legitimate single-owner case.
    result = resolve_identity(fx["players"], fx["aliases"],
                              query="测试甲", query_kind="alias")
    assert result["status"] == "RESOLVED"
    assert result["candidates"] == [A]


# ------------------------------------------- Follow-up (a): synthetic 陈岚 case
def test_q2fix_chen_lan_synthetic_two_char_alias_and_name():
    # SYNTHETIC ONLY (see module docstring): no frozen real UID/value
    # for 陈岚 is asserted. This guards two-character handling only.
    players = [
        {"player_uid": CHEN_LAN, "canonical_name": "陈岚"},
        {"player_uid": A, "canonical_name": "林测试"},
    ]
    aliases = [{"alias": "陈岚", "player_uid": CHEN_LAN, "approved": True}]
    by_alias = resolve_identity(players, aliases, query="陈岚",
                                query_kind="alias")
    assert by_alias["status"] == "RESOLVED"
    assert by_alias["candidates"] == [CHEN_LAN]
    by_name = resolve_identity(players, aliases, query="陈岚",
                               query_kind="name")
    assert by_name["status"] == "RESOLVED"
    assert by_name["candidates"] == [CHEN_LAN]
    # Negative: an unapproved 陈岚 binding must not resolve.
    unapproved = resolve_identity(
        players,
        [{"alias": "陈岚", "player_uid": CHEN_LAN, "approved": False}],
        query="陈岚", query_kind="alias")
    assert unapproved["status"] == "UNKNOWN"
    # Negative: two players sharing the name 陈岚 must clarify, not pick.
    two = resolve_identity(
        players + [{"player_uid": B, "canonical_name": "陈岚"}],
        aliases, query="陈岚", query_kind="name")
    assert two["status"] == "CLARIFICATION_REQUIRED"
    assert two["candidates"] == [B, CHEN_LAN]
    assert two["relation"] == "UNDECIDED"


def test_q2fix_chen_lan_synthetic_passage_routing_two_char():
    target = build_passage(source_id="fixture_doc_CHENLAN", revision="rev-1",
                           locator="P1", text="陈岚每天训练罚球。",
                           player_uid=CHEN_LAN, aliases=["陈岚"])
    distractors = [
        build_passage(source_id=f"fixture_chenlan_distractor_{i:02d}",
                      revision="rev-1", locator="P1",
                      text="这位球员喜欢罚球。", player_uid=None, aliases=[])
        for i in range(10)
    ]
    routed = route_passages([target] + distractors, alias="陈岚",
                            topic_tokens=["陈岚", "罚球"])
    assert routed["candidates"][0]["source_id"] == "fixture_doc_CHENLAN"
    assert routed["candidates"][0]["text"] == "陈岚每天训练罚球。"


# --------------------------------------------- Follow-up (b): NaN / inf
def test_q2fix_nan_and_infinity_are_rejected():
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(KcError):
            validate_metric_value("points_per_game", bad, "per_game")
        with pytest.raises(KcError):
            validate_metric_value("field_goals_percentage_rate", bad, "ratio")
    # Finite values still pass, so the guard is not over-broad.
    assert validate_metric_value("points_per_game", 12.5, "per_game")["value"] == 12.5
    assert validate_metric_value("field_goals_percentage_rate", 0.45, "ratio")["value"] == 0.45


# --------------------------------- Follow-up (c): authoritative slice counts
def _authoritative_counts(fx):
    full = route_uid_slices(fx["stats_records"], player_uid=A)
    counts = {}
    for sl in full["slices"]:
        counts[(sl["season"], sl["match_type"], sl["scope"], sl["team_id"])] = len(sl["rows"])
    return counts


def test_q2fix_uid_slice_authoritative_counts_detect_omission(fx):
    counts = _authoritative_counts(fx)
    full = route_uid_slices(fx["stats_records"], player_uid=A,
                            expected_counts=counts)
    assert full["complete"] is True and full["end_marker"] is True
    assert full["count_mismatches"] == []

    # Omit one TEAM_SPLIT team record: the authoritative catalog (not a
    # caller-supplied ad-hoc expected set) must detect it.
    omitted = [r for r in fx["stats_records"]
               if not (r["scope"] == "TEAM_SPLIT"
                       and r.get("team_id") == "fixture_team_T2")]
    partial = route_uid_slices(omitted, player_uid=A,
                               expected_counts=counts)
    assert partial["complete"] is False
    assert partial["end_marker"] is False
    assert any(m["key"] == (SEASON, "REGULAR", "TEAM_SPLIT", "fixture_team_T2")
               for m in partial["count_mismatches"])
    assert partial["count_mismatches"][0]["actual"] != partial["count_mismatches"][0]["expected"]


def test_q2fix_uid_slice_authoritative_counts_detect_duplication(fx):
    counts = _authoritative_counts(fx)
    duplicated = list(fx["stats_records"]) + [_canonical_a_regular_whole(fx)]
    partial = route_uid_slices(duplicated, player_uid=A,
                               expected_counts=counts)
    assert partial["complete"] is False
    assert any(m["key"] == (SEASON, "REGULAR", "WHOLE_SEASON", None)
               for m in partial["count_mismatches"])


# ------------------------------------------------- End-to-end Q2 counterexamples
def test_q2fix_call_chain_all_three_blockers_together(fx):
    # One real call chain exercising all three fixed behaviours.
    grain = typed_result(caller_grain="DOCUMENT",
                         authoritative_grain="STATS",
                         payload={"grain": "DOCUMENT", "value": 12.5})
    assert grain["grain"] == "STATS"
    stats = query_stats(fx["stats_records"], player_uid=A, season=SEASON,
                        match_type="REGULAR", scope="WHOLE_SEASON",
                        metric="points_per_game",
                        provider="CBA_OFFICIAL", competition="CBA")
    assert stats["status"] == "OK" and stats["value"] == 12.5
    ident = resolve_identity(
        [], [{"alias": "共享名", "player_uid": A, "approved": True},
             {"alias": "共享名", "player_uid": B, "approved": True}],
        query="共享名", query_kind="alias")
    assert ident["status"] == "CLARIFICATION_REQUIRED"
    assert ident["candidates"] == [A, B]
