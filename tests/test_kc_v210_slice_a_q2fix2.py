"""CBA-KB v2.1.0 slice A — Q2 FAIL regression tests (PR #216, Q2 round 2).

Bounded fix only, no re-freeze: frozen binding REQ-210-KNOWLEDGE-
CONSUMPTION-01, LACD_v1.0.2, main spec SHA-256
ee4bd5338edba757fd2764df563fe21600d3a3313bb8dab2ec2416d840b3ef09,
addendum SHA-256
3230146ab7668972850a63ea9a3bbc31e125b5fcaaeb2f37c757ea861f3f9071.

Round-2 Q2 single BLOCKER: query_stats() accepted provider/competition
as optional arguments defaulting to the canonical binding, so a caller
could omit both dimensions and still get OK for the canonical binding —
an impermissible silent default under the frozen no-silent-default
invariant (stats_key() documents it). The fix makes provider and
competition REQUIRED keyword-only arguments: omitting either must NOT
yield OK. In Python, omitting a required keyword-only argument raises
TypeError at call time, before any lookup; that is the asserted
omission-rejection behaviour, documented here so the TypeError is not
mistaken for an environment failure.

These tests were written FIRST against the unfixed round-2 code and
must FAIL there: the omission calls below return OK (12.5) on unfixed
code instead of raising TypeError.

Synthetic data only. No frozen real value is asserted.
"""

import pytest

from cba_kb.kc_cleanroom import build_cleanroom_fixture
from cba_kb.kc_projection import query_stats

A = "fixture_player_0001"
SEASON = "2025-2026"


@pytest.fixture(scope="module")
def fx():
    return build_cleanroom_fixture()


def test_q2fix2_omitting_provider_and_competition_is_rejected(fx):
    # Omission must NOT yield OK for the canonical binding.
    with pytest.raises(TypeError):
        query_stats(fx["stats_records"], player_uid=A, season=SEASON,
                    match_type="REGULAR", scope="WHOLE_SEASON",
                    metric="points_per_game")


def test_q2fix2_omitting_only_provider_is_rejected(fx):
    with pytest.raises(TypeError):
        query_stats(fx["stats_records"], player_uid=A, season=SEASON,
                    match_type="REGULAR", scope="WHOLE_SEASON",
                    metric="points_per_game", competition="CBA")


def test_q2fix2_omitting_only_competition_is_rejected(fx):
    with pytest.raises(TypeError):
        query_stats(fx["stats_records"], player_uid=A, season=SEASON,
                    match_type="REGULAR", scope="WHOLE_SEASON",
                    metric="points_per_game", provider="CBA_OFFICIAL")


def test_q2fix2_explicit_canonical_still_ok_and_other_still_unknown(fx):
    # Explicit canonical binding: the dual-source counterexample from
    # round 1 is preserved, not weakened.
    explicit = query_stats(fx["stats_records"], player_uid=A, season=SEASON,
                           match_type="REGULAR", scope="WHOLE_SEASON",
                           metric="points_per_game",
                           provider="CBA_OFFICIAL", competition="CBA")
    assert explicit["status"] == "OK" and explicit["value"] == 12.5
    assert explicit["key"]["provider"] == "CBA_OFFICIAL"
    assert explicit["key"]["competition"] == "CBA"
    other = query_stats(fx["stats_records"], player_uid=A, season=SEASON,
                        match_type="REGULAR", scope="WHOLE_SEASON",
                        metric="points_per_game",
                        provider="OTHER_PROVIDER", competition="OTHER_LEAGUE")
    assert other["status"] == "UNKNOWN"
    assert other.get("value") != 99.0
