"""T2-1: Muse handoff JSON adapter tests (SYNTHETIC fixtures only).

AC coverage: AC-01~03, AC-08~11 (RC-01/02/03 handoff packages).
All players, IDs, teams and values are invented. Real RC-01~03 data
MUST NEVER enter this repo (AC-29).

Falsification: these tests were run against the unmodified base first
and failed with ModuleNotFoundError (no adapter) before implementation.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from cba_kb.adapters import muse_handoff
from cba_kb.adapters.muse_handoff import HandoffError, adapt_handoff
from cba_kb.stats import (
    PROVIDER_CBA_OFFICIAL,
    SEMANTIC_GRAIN,
    build_record_id,
    deduplicate_and_reconcile_stats,
    validate_stats_record,
)

FIXTURE = Path(__file__).parent / "fixtures" / "handoff_synth.json"


def load_payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_is_synthetic():
    payload = load_payload()
    assert payload["schema"] == "player_season_stats_handoff_v1"
    assert payload["source_provider"] == "SYNTHETIC_TEST"
    for row in payload["records"]:
        assert row["provider_player_id"].startswith("900"), "must use invented IDs"
        assert "虚构" in row["player_name"], "must use invented names"


def test_whole_season_record():
    payload = load_payload()
    result = adapt_handoff({"schema": payload["schema"], "records": [payload["records"][0]]})
    assert len(result["records"]) == 1
    rec = result["records"][0]
    assert rec["scope"] == "WHOLE_SEASON"
    assert rec["provider"] == PROVIDER_CBA_OFFICIAL
    assert rec["semantic_grain"] == SEMANTIC_GRAIN
    assert rec["identity_status"] == "UNRESOLVED"
    assert rec["player_uid"] is None
    assert rec["rights"]["classification"] == "PRIVATE"
    assert rec["rights"]["public_export_allowed"] is False
    # every produced record must pass the frozen stats validator
    validate_stats_record(rec)


def test_team_split_rows_require_team_id():
    payload = load_payload()
    rows = [r for r in payload["records"] if r["provider_player_id"] == "900002"]
    splits = [r for r in rows if r["scope"] == "TEAM_SPLIT"]
    assert len(splits) == 2
    result = adapt_handoff({"schema": payload["schema"], "records": splits})
    assert len(result["records"]) == 2
    team_ids = {rec["team_id"] for rec in result["records"]}
    assert team_ids == {"T901", "T902"}
    for rec in result["records"]:
        assert rec["scope"] == "TEAM_SPLIT"
        validate_stats_record(rec)


def test_team_split_missing_team_id_rejects():
    payload = load_payload()
    row = copy.deepcopy(payload["records"][1])
    row["team_provider_id"] = None
    with pytest.raises(HandoffError):
        adapt_handoff({"schema": payload["schema"], "records": [row]})
    row2 = copy.deepcopy(payload["records"][1])
    row2["team_provider_id"] = "  "
    with pytest.raises(HandoffError):
        adapt_handoff({"schema": payload["schema"], "records": [row2]})


def test_unknown_metric_key_rejects():
    payload = load_payload()
    row = copy.deepcopy(payload["records"][0])
    row["metrics_normalized"]["points_per_game_hacker"] = 99.9
    with pytest.raises(HandoffError) as exc:
        adapt_handoff({"schema": payload["schema"], "records": [row]})
    assert "UNKNOWN" in str(exc.value) or "REJECT" in str(exc.value)


def test_missing_normalized_key_rejects():
    payload = load_payload()
    row = copy.deepcopy(payload["records"][0])
    del row["metrics_normalized"]["turnovers_per_game"]
    with pytest.raises(HandoffError):
        adapt_handoff({"schema": payload["schema"], "records": [row]})


def test_rc02_three_row_shape_no_double_count():
    """Synthetic reenactment of RC-02: 3 rows -> 2 TEAM_SPLIT + 1 WHOLE_SEASON.

    The whole-season 33 games must not be double-counted: exactly 3 records
    come out, each with a distinct deterministic record_id.
    """
    payload = load_payload()
    rows = [r for r in payload["records"] if r["provider_player_id"] == "900002"]
    assert len(rows) == 3
    result = adapt_handoff({"schema": payload["schema"], "records": rows})
    recs = result["records"]
    assert len(recs) == 3, "no 4th record may be synthesized from the splits"
    scopes = sorted(r["scope"] for r in recs)
    assert scopes == ["TEAM_SPLIT", "TEAM_SPLIT", "WHOLE_SEASON"]
    games = sorted(r["metrics"]["games_played"] for r in recs)
    assert games == [15, 18, 33]
    ids = [r["record_id"] for r in recs]
    assert len(set(ids)) == 3, "record_ids must be distinct per scope/team"
    whole = [r for r in recs if r["scope"] == "WHOLE_SEASON"][0]
    assert whole["metrics"]["games_played"] == 33
    # splits keep their own team-bound ids; whole-season id has no team part
    assert whole["record_id"] == build_record_id(
        provider=PROVIDER_CBA_OFFICIAL, season="2023-2024", match_type="1",
        scope="WHOLE_SEASON", provider_player_id="900002",
    )
    for rec in recs:
        validate_stats_record(rec)


def test_record_id_deterministic():
    payload = load_payload()
    first = adapt_handoff(payload)["records"]
    second = adapt_handoff(load_payload())["records"]
    assert [r["record_id"] for r in first] == [r["record_id"] for r in second]


def test_reimport_idempotent():
    """AC-27: importing the same package twice must be idempotent."""
    payload = load_payload()
    recs = adapt_handoff(payload)["records"]
    merged = deduplicate_and_reconcile_stats(recs + recs)
    assert len(merged) == len(recs)


def test_unknown_match_type_is_review_required_not_rejected():
    payload = load_payload()
    row = copy.deepcopy(payload["records"][0])
    row["match_type_code"] = "99"
    row["match_type_name"] = "未知类型"
    result = adapt_handoff({"schema": payload["schema"], "records": [row]})
    assert len(result["records"]) == 1
    rec = result["records"][0]
    assert rec["identity_status"] == "REVIEW_REQUIRED"
    assert rec["player_uid"] is None
    validate_stats_record(rec)


def test_missing_schema_signature_is_review_required():
    payload = load_payload()
    row = copy.deepcopy(payload["records"][0])
    del row["provider_schema_signature"]
    result = adapt_handoff({"schema": payload["schema"], "records": [row]})
    assert result["records"][0]["identity_status"] == "REVIEW_REQUIRED"
    validate_stats_record(result["records"][0])


def test_missing_hashes_reject_no_fabrication():
    """stats.py mandates sha256 provenance; the adapter must not invent it."""
    payload = load_payload()
    row = copy.deepcopy(payload["records"][0])
    del row["raw_sha256"]
    with pytest.raises(HandoffError):
        adapt_handoff({"schema": payload["schema"], "records": [row]})


def test_wrong_schema_rejects():
    with pytest.raises(HandoffError):
        adapt_handoff({"schema": "something_else_v9", "records": []})


def test_non_cba_provider_rejects():
    payload = load_payload()
    row = copy.deepcopy(payload["records"][0])
    row["provider"] = "UNKNOWN_LEAGUE"
    with pytest.raises(HandoffError):
        adapt_handoff({"schema": payload["schema"], "records": [row]})


def test_null_metric_stays_null_not_zero():
    """RC-03 tri-state: null != absent != 0."""
    payload = load_payload()
    row = copy.deepcopy(payload["records"][0])
    row["metrics_normalized"]["points_per_game"] = None
    row["metrics_normalized"]["blocks_per_game"] = 0
    result = adapt_handoff({"schema": payload["schema"], "records": [row]})
    rec = result["records"][0]
    assert rec["metrics"]["points_per_game"] is None
    assert rec["metrics"]["blocks_per_game"] == 0
    validate_stats_record(rec)


def test_probe_rows_shape_maps():
    """RC-02 probe document shape: provider_rows with row_classification."""
    probe = {
        "record_type": "PROBE_ANALYSIS",
        "provider": "CBA_OFFICIAL",
        "provider_player_id": "900003",
        "player_name": "虚构球员丙",
        "season": "2023-2024",
        "match_type_code": "1",
        "match_type_name": "常规赛",
        "provider_rows": [
            {
                "row_classification": "TEAM_SPLIT",
                "team_provider_id": "T903",
                "team_name": "虚构球队丙",
                "metrics_raw": {
                    "playerTimes": 18, "minutes": 8.3, "points": 1.6,
                    "rebounds": 1.2, "assists": 0.8, "steals": 0.4,
                    "blocked": 0.1, "turnovers": 0.5, "fouls": 0.8,
                    "age": 27, "birthDate": "1998-01-01",
                },
                "games_played": 18,
                "source_uri": "synthetic://test/probe/900003/split1",
                "observed_at": "2026-10-18T00:00:00Z",
                "raw_sha256": "b" * 64,
                "decoded_sha256": "c" * 64,
            },
            {
                "row_classification": "WHOLE_SEASON",
                "team_provider_id": "T903",
                "team_name": "虚构球队丙",
                "metrics_raw": {"playerTimes": 33, "minutes": 8.6, "points": 2.1},
                "games_played": 33,
                "source_uri": "synthetic://test/probe/900003/whole",
                "observed_at": "2026-10-18T00:00:00Z",
                "raw_sha256": "d" * 64,
                "decoded_sha256": "e" * 64,
            },
        ],
    }
    result = adapt_handoff(probe)
    assert len(result["records"]) == 2
    scopes = sorted(r["scope"] for r in result["records"])
    assert scopes == ["TEAM_SPLIT", "WHOLE_SEASON"]
    split = [r for r in result["records"] if r["scope"] == "TEAM_SPLIT"][0]
    # non-stat raw fields (age, birthDate) are dropped; stat fields mapped
    assert split["metrics"]["points_per_game"] == 1.6
    assert split["metrics"]["games_played"] == 18
    assert "raw_metrics" not in split or "age" not in str(split.get("raw_metrics", {}))
    for rec in result["records"]:
        validate_stats_record(rec)
