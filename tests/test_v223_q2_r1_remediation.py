"""Differential remediation regression tests for REQ-223 Q2 Round 1 & Round 2 (PR #273).

Every remediation test in this file targets a concrete defect found in Round 1 (`d45605d3`)
or Round 2 (`85adb102`) without modifying Commit 1 frozen tests or spec:
- Blocker 1 (diff vs `d45605d3`): `schema_version: 1.0` float rejected as `CURATION_PROFILE_INVALID`.
- Blocker 2 (diff vs `d45605d3`): fail-closed revision chain binding (missing target,
  cross-player, cross-game, cross-season, forked chain, self/multi-hop cycles) + valid 3-hop chain.
- Blocker 3 (diff vs `d45605d3` & `85adb102`): fail-closed homonym disambiguation across
  conflicting clubs (MASTER+identity, MASTER+MASTER, MASTER+foreign, foreign+foreign),
  missing-club registry/MASTER rows, and unknown explicit `player_uid`/`player_id` rows.
- Blocker 4 (diff vs `d45605d3`): DNP-only `recent_games_summary` renders
  `## Recent Single-Game Log (Player-Game Grain)` ending with exact `end_marker: true`.

All fixtures are 100% synthetic. Zero network calls, zero Drive API calls, zero private paths.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from cba_kb import consumer_surface, game_stats, kc_render, person_card_builder


def _make_game_rec(
    *,
    record_id: str,
    player_uid: str = "player:synth-a",
    provider_player_id: str = "990101",
    player_name: str = "合成球员甲",
    season: str = "2026-2027",
    game_id: str = "SYNTH_G01",
    game_date: str = "2026-11-20",
    points: int | None = 15,
    dnp_reason: str | None = None,
    did_not_play: bool = False,
    supersedes_record_id: str | None = None,
) -> Dict[str, Any]:
    rec: Dict[str, Any] = {
        "schema": "cba-kb.player-game-stats.v1",
        "schema_version": 1,
        "semantic_grain": "PLAYER_GAME_STATS",
        "record_id": record_id,
        "player_uid": player_uid,
        "provider_player_id": provider_player_id,
        "player_name": player_name,
        "provider": "CBA_OFFICIAL",
        "competition": "CBA",
        "season": season,
        "match_type": "1",
        "game_id": game_id,
        "game_date": game_date,
        "team_id": "SYNTH_CLUB_A",
        "team_name": "合成俱乐部甲",
        "opponent_team_id": "SYNTH_CLUB_B",
        "opponent_team_name": "合成俱乐部乙",
        "dnp_reason": dnp_reason,
        "did_not_play": did_not_play,
        "metrics": {
            "minutes_played": 0.0 if did_not_play else 28.0,
            "points": points,
            "rebounds": None if did_not_play else 4,
            "assists": None if did_not_play else 3,
        },
        "provenance": {
            "canonical_url": f"https://example.org/cba/game/{game_id}",
            "raw_sha256": "c" * 64,
            "captured_at": "2026-11-24T06:00:00Z",
            "doc_id": f"doc_{game_id}",
        },
        "rights": {
            "classification": "UNKNOWN_FOR_REDISTRIBUTION",
            "public_export_allowed": False,
        },
    }
    if supersedes_record_id is not None:
        rec["supersedes_record_id"] = supersedes_record_id
    return rec


# ---------------------------------------------------------------------------
# Blocker 1 (Differential vs Round 1 HEAD d45605d3): P0-1-SCHEMA-VERSION-TYPE
# ---------------------------------------------------------------------------

def test_q2_r1_blocker1_curation_profile_rejects_float_schema_version(tmp_path: Path):
    """Differential test vs d45605d3: float 1.0 passed `!= 1` in R1, must now fail closed."""
    profile_file = tmp_path / "float_version_profile.yaml"
    profile_file.write_text(
        "\n".join([
            "schema_version: 1.0",
            "noise_event_classes: ['SYNTH_NOISE']",
            "noise_keywords: ['SYNTH_KW']",
            "historical_pinned_event_classes: ['SYNTH_HIST']",
            "historical_pinned_keywords: ['SYNTH_OLD']",
            "pinned_marker_keywords: ['SYNTH_PIN']",
            "protected_event_classes: ['注册与转会']",
            "active_competitive_keywords: ['签约']",
            "condolence_keywords: ['SYNTH_COND']",
            "condolence_exempt_keywords: ['签约']",
            "compact_announcement_excerpt_chars: 140",
        ]) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="CURATION_PROFILE_INVALID"):
        consumer_surface.load_curation_profile(profile_file)


# ---------------------------------------------------------------------------
# Blocker 2 (Differential vs Round 1 HEAD d45605d3): P0-2-CROSS-KEY-REVISION
# ---------------------------------------------------------------------------

def test_q2_r1_blocker2_rejects_cross_player_cross_game_missing_forked_and_cyclic_revisions():
    r1 = _make_game_rec(record_id="r1", player_uid="player:synth-a", game_id="SYNTH_G01")

    # 1. Cross-player + cross-game supersession
    r_cross_player = _make_game_rec(
        record_id="r2_other_player",
        player_uid="player:synth-b",
        provider_player_id="990202",
        player_name="合成球员乙",
        game_id="SYNTH_G02",
        supersedes_record_id="r1",
    )
    with pytest.raises(game_stats.GameStatsValidationError, match="CROSS_PLAYER_REVISION_REJECTED"):
        game_stats.reconcile_game_stat_revisions([r1, r_cross_player])

    # 2. Same player, cross-game supersession
    r_cross_game = _make_game_rec(
        record_id="r2_other_game",
        player_uid="player:synth-a",
        game_id="SYNTH_G02",
        supersedes_record_id="r1",
    )
    with pytest.raises(game_stats.GameStatsValidationError, match="CROSS_GAME_REVISION_REJECTED"):
        game_stats.reconcile_game_stat_revisions([r1, r_cross_game])

    # 3. Same player & game, cross-season supersession
    r_cross_season = _make_game_rec(
        record_id="r2_other_season",
        player_uid="player:synth-a",
        game_id="SYNTH_G01",
        season="2025-2026",
        supersedes_record_id="r1",
    )
    with pytest.raises(game_stats.GameStatsValidationError, match="CROSS_SEASON_REVISION_REJECTED"):
        game_stats.reconcile_game_stat_revisions([r1, r_cross_season])

    # 4. Missing superseded record target
    r_orphan = _make_game_rec(
        record_id="r_orphan",
        player_uid="player:synth-a",
        game_id="SYNTH_G01",
        supersedes_record_id="nonexistent_record_id",
    )
    with pytest.raises(game_stats.GameStatsValidationError, match="SUPERSEDED_RECORD_NOT_FOUND"):
        game_stats.reconcile_game_stat_revisions([r_orphan])

    # 5. Forked revision chain (two records superseding the same prior record)
    r_fork_a = _make_game_rec(record_id="r_fork_a", points=17, supersedes_record_id="r1")
    r_fork_b = _make_game_rec(record_id="r_fork_b", points=18, supersedes_record_id="r1")
    with pytest.raises(game_stats.GameStatsValidationError, match="FORKED_REVISION_CHAIN"):
        game_stats.reconcile_game_stat_revisions([r1, r_fork_a, r_fork_b])

    # 6. Self-supersession cycle
    r_self = _make_game_rec(record_id="r_self", supersedes_record_id="r_self")
    with pytest.raises(game_stats.GameStatsValidationError, match="REVISION_CYCLE_DETECTED"):
        game_stats.reconcile_game_stat_revisions([r_self])

    # 7. Two-hop cycle (c1 -> c2 -> c1)
    c1 = _make_game_rec(record_id="c1", supersedes_record_id="c2")
    c2 = _make_game_rec(record_id="c2", supersedes_record_id="c1")
    with pytest.raises(game_stats.GameStatsValidationError, match="REVISION_CYCLE_DETECTED"):
        game_stats.reconcile_game_stat_revisions([c1, c2])


def test_q2_r2_p0_2_valid_multi_hop_revision_chain_converges_loudly():
    """Positive multi-hop regression: r3 -> r2 -> r1 retains r3 active and emits 2 LOUD_REVISIONs."""
    r1 = _make_game_rec(record_id="r1", points=14)
    r2 = _make_game_rec(record_id="r2", points=16, supersedes_record_id="r1")
    r3 = _make_game_rec(record_id="r3", points=18, supersedes_record_id="r2")

    res = game_stats.reconcile_game_stat_revisions([r1, r2, r3])
    assert [r["record_id"] for r in res["active_records"]] == ["r3"]
    assert {r["record_id"] for r in res["superseded_records"]} == {"r1", "r2"}
    assert len(res["revision_summaries"]) == 2
    assert all(s["disclosure_posture"] == "LOUD_REVISION" for s in res["revision_summaries"])
    edges = {(s["new_record_id"], s["prior_record_id"]) for s in res["revision_summaries"]}
    assert edges == {("r2", "r1"), ("r3", "r2")}

    summary = game_stats.summarize_recent_games([r1, r2, r3], window=5)
    assert summary["played_games_in_window"] == 1
    assert summary["averages"]["points"] == pytest.approx(18.0)


# ---------------------------------------------------------------------------
# Blocker 3 (Differential vs Round 1 d45605d3 & Round 2 85adb102): P0-3
# ---------------------------------------------------------------------------

def test_q2_r1_blocker3_same_name_different_club_returns_undecided_not_merged():
    identity_registry = {
        "schema_version": 1,
        "players": [
            {
                "player_uid": "player:synth-homonym-a",
                "canonical_name": "合成同名球员",
                "provider_player_id": "991001",
                "club": "合成俱乐部甲",
            }
        ],
        "aliases": [],
    }
    master_rows = [
        {
            "record_key": "2026-2027|SYNTH_CLUB_A|合成同名球员",
            "season": "2026-2027",
            "club_id": "SYNTH_CLUB_A",
            "club_official": "合成俱乐部甲",
            "player": "合成同名球员",
        },
        {
            "record_key": "2026-2027|SYNTH_CLUB_B|合成同名球员",
            "season": "2026-2027",
            "club_id": "SYNTH_CLUB_B",
            "club_official": "合成俱乐部乙",
            "player": "合成同名球员",
        },
    ]

    res = person_card_builder.resolve_person_query(
        "合成同名球员",
        master_rows=master_rows,
        identity_registry=identity_registry,
    )
    assert res["status"] == "UNDECIDED"
    assert res["person"] is None
    assert len(res["candidates"]) == 2
    assert {c["current_club"] for c in res["candidates"]} == {"合成俱乐部甲", "合成俱乐部乙"}

    # Explicit ID query still disambiguates cleanly
    res_by_uid = person_card_builder.resolve_person_query(
        "player:synth-homonym-a",
        master_rows=master_rows,
        identity_registry=identity_registry,
    )
    assert res_by_uid["status"] == "RESOLVED"
    assert res_by_uid["person"]["current_club"] == "合成俱乐部甲"

    # Explicit transfer evidence (former_club) allows valid merge
    master_rows_transfer = [
        master_rows[0],
        {
            "record_key": "2026-2027|SYNTH_CLUB_B|合成同名球员",
            "season": "2026-2027",
            "club_id": "SYNTH_CLUB_B",
            "club_official": "合成俱乐部乙",
            "former_club": "合成俱乐部甲",
            "player": "合成同名球员",
        },
    ]
    res_transfer = person_card_builder.resolve_person_query(
        "合成同名球员",
        master_rows=master_rows_transfer,
        identity_registry=identity_registry,
    )
    assert res_transfer["status"] == "RESOLVED"
    assert set(res_transfer["person"]["clubs_history"]) == {"合成俱乐部甲", "合成俱乐部乙"}


def test_q2_r2_blocker3_missing_club_never_merges_fail_closed():
    """Differential test vs 85adb102: candidate without club + MASTER with club must return UNDECIDED."""
    reg_no_club = {
        "schema_version": 1,
        "players": [
            {
                "player_uid": "player:synth-a",
                "canonical_name": "合成缺俱乐部同名",
            }
        ],
        "aliases": [],
    }
    master_with_club = [
        {
            "record_key": "2026-2027|SYNTH_CLUB_B|合成缺俱乐部同名",
            "season": "2026-2027",
            "club_id": "SYNTH_CLUB_B",
            "club_official": "合成俱乐部乙",
            "player": "合成缺俱乐部同名",
        }
    ]
    res = person_card_builder.resolve_person_query(
        "合成缺俱乐部同名",
        master_rows=master_with_club,
        identity_registry=reg_no_club,
    )
    assert res["status"] == "UNDECIDED"
    assert res["person"] is None
    assert len(res["candidates"]) == 2

    # Converse: registry has club, MASTER row lacks club -> also fail closed to UNDECIDED
    reg_with_club = {
        "schema_version": 1,
        "players": [
            {
                "player_uid": "player:synth-a",
                "canonical_name": "合成缺俱乐部同名",
                "club": "合成俱乐部甲",
            }
        ],
        "aliases": [],
    }
    master_no_club = [
        {
            "record_key": "2026-2027||合成缺俱乐部同名",
            "season": "2026-2027",
            "club_id": "",
            "club_official": "",
            "player": "合成缺俱乐部同名",
        }
    ]
    res_rev = person_card_builder.resolve_person_query(
        "合成缺俱乐部同名",
        master_rows=master_no_club,
        identity_registry=reg_with_club,
    )
    assert res_rev["status"] == "UNDECIDED"
    assert len(res_rev["candidates"]) == 2


def test_q2_r2_blocker3_unknown_explicit_player_uid_never_falls_back_to_name_merge():
    """Differential test vs 85adb102: MASTER row with unknown player_uid=B must NOT merge into UID=A."""
    reg = {
        "schema_version": 1,
        "players": [
            {
                "player_uid": "player:synth-a",
                "canonical_name": "合成同队重名",
                "provider_player_id": "993001",
                "club": "合成俱乐部甲",
            }
        ],
        "aliases": [],
    }
    master_unknown_uid = [
        {
            "record_key": "2026-2027|SYNTH_CLUB_A|合成同队重名",
            "season": "2026-2027",
            "club_id": "SYNTH_CLUB_A",
            "club_official": "合成俱乐部甲",
            "player": "合成同队重名",
            "player_uid": "player:synth-b",
        }
    ]
    res_by_name = person_card_builder.resolve_person_query(
        "合成同队重名",
        master_rows=master_unknown_uid,
        identity_registry=reg,
    )
    assert res_by_name["status"] == "UNDECIDED"
    assert len(res_by_name["candidates"]) == 2
    assert {c["player_uid"] for c in res_by_name["candidates"]} == {
        "player:synth-a",
        "player:synth-b",
    }

    # Exact UID queries resolve cleanly to each distinct identity
    res_b = person_card_builder.resolve_person_query(
        "player:synth-b",
        master_rows=master_unknown_uid,
        identity_registry=reg,
    )
    assert res_b["status"] == "RESOLVED"
    assert res_b["person"]["player_uid"] == "player:synth-b"


def test_q2_r2_blocker3_cross_source_master_foreign_and_foreign_foreign_collisions():
    # 1. MASTER + foreign same-name different-club -> UNDECIDED
    res_mf = person_card_builder.resolve_person_query(
        "合成跨源同名",
        master_rows=[{
            "record_key": "2026-2027|SYNTH_CLUB_A|合成跨源同名",
            "season": "2026-2027",
            "club_id": "SYNTH_CLUB_A",
            "club_official": "合成俱乐部甲",
            "player": "合成跨源同名",
        }],
        foreign_rows=[{
            "club": "合成俱乐部乙",
            "player_en": "SYNTH CROSS SOURCE",
            "player_zh": "合成跨源同名",
            "nationality": "SYNTH_NATION",
            "position": "SF",
            "jersey_number": "9",
        }],
    )
    assert res_mf["status"] == "UNDECIDED"
    assert len(res_mf["candidates"]) == 2

    # 2. foreign + foreign same-name different-club -> UNDECIDED
    res_ff = person_card_builder.resolve_person_query(
        "合成双外援同名",
        foreign_rows=[
            {
                "club": "合成俱乐部甲",
                "player_en": "SYNTH FOR ONE",
                "player_zh": "合成双外援同名",
                "nationality": "SYNTH_NATION",
                "position": "PG",
                "jersey_number": "1",
            },
            {
                "club": "合成俱乐部乙",
                "player_en": "SYNTH FOR TWO",
                "player_zh": "合成双外援同名",
                "nationality": "SYNTH_NATION",
                "position": "SG",
                "jersey_number": "2",
            },
        ],
    )
    assert res_ff["status"] == "UNDECIDED"
    assert len(res_ff["candidates"]) == 2

    # 3. Registry uses club_official/club_id + MASTER-only transfer with former_club
    reg = {
        "schema_version": 1,
        "players": [
            {
                "player_uid": "player:synth-club-b",
                "canonical_name": "合成同名字段甲",
                "club_id": "SYNTH_CLUB_B",
                "club_official": "合成俱乐部乙",
            }
        ],
        "aliases": [],
    }
    res_conflict = person_card_builder.resolve_person_query(
        "合成同名字段甲",
        master_rows=[{
            "record_key": "2026-2027|SYNTH_CLUB_A|合成同名字段甲",
            "season": "2026-2027",
            "club_id": "SYNTH_CLUB_A",
            "club_official": "合成俱乐部甲",
            "player": "合成同名字段甲",
        }],
        identity_registry=reg,
    )
    assert res_conflict["status"] == "UNDECIDED"
    assert len(res_conflict["candidates"]) == 2

    res_xfr = person_card_builder.resolve_person_query(
        "合成转会球员",
        master_rows=[
            {
                "record_key": "2025-2026|SYNTH_CLUB_A|合成转会球员",
                "season": "2025-2026",
                "club_id": "SYNTH_CLUB_A",
                "club_official": "合成俱乐部甲",
                "player": "合成转会球员",
            },
            {
                "record_key": "2026-2027|SYNTH_CLUB_B|合成转会球员",
                "season": "2026-2027",
                "club_id": "SYNTH_CLUB_B",
                "club_official": "合成俱乐部乙",
                "former_club": "合成俱乐部甲",
                "player": "合成转会球员",
            },
        ],
        identity_registry={"schema_version": 1, "players": [], "aliases": []},
    )
    assert res_xfr["status"] == "RESOLVED"
    assert res_xfr["person"]["current_club"] == "合成俱乐部乙"
    assert set(res_xfr["person"]["clubs_history"]) == {"合成俱乐部甲", "合成俱乐部乙"}


# ---------------------------------------------------------------------------
# Blocker 4 (Differential vs Round 1 HEAD d45605d3): P0-4-DNP-ONLY-LOG-OMITTED
# ---------------------------------------------------------------------------

def test_q2_r1_blocker4_dnp_only_recent_games_summary_still_renders_section(tmp_path: Path):
    dnp_game = _make_game_rec(
        record_id="game_stats:CBA_OFFICIAL:2026-2027:1:SYNTH_G99:player:synth-a",
        game_id="SYNTH_G99",
        game_date="2026-11-22",
        points=None,
        dnp_reason="COACH_DECISION",
        did_not_play=True,
    )
    summary = game_stats.summarize_recent_games([dnp_game], window=5)
    assert summary["games"] == []
    assert summary["played_games_in_window"] == 0
    assert summary["dnp_games_count"] == 1
    assert summary["latest_game_date"] == "2026-11-22"

    payload: Dict[str, Any] = {
        "metric": "points_per_game",
        "grains": {
            "STATS": {
                "posture": "UNKNOWN",
                "season": "2026-2027",
                "match_type": "-",
                "scope": "-",
            }
        },
        "evidence_transparency": {
            "posture": "UNKNOWN",
            "unknown_subkind": "NO_SEASON_STATS_RECORDED_YET",
            "scope_meta": {
                "player_name": "合成球员甲",
                "player_id": "990101",
                "season": "2026-2027",
            },
        },
        "recent_games_summary": summary,
    }
    card = kc_render.render_research_card(payload, evidence_verified=True)
    assert "## Recent Single-Game Log (Player-Game Grain)" in card
    assert "played=0, dnp_excluded=1" in card
    assert "PTS=UNKNOWN" in card
    assert "2026-11-22" in card
    assert card.rstrip().splitlines()[-1] == "end_marker: true"
    assert kc_render.check_completeness(card, kind="markdown") is True

    # End-to-end via build_person_card
    built = person_card_builder.build_person_card(
        "合成球员甲",
        season="2026-2027",
        master_rows=[{
            "record_key": "2026-2027|SYNTH_CLUB_A|合成球员甲",
            "season": "2026-2027",
            "club_id": "SYNTH_CLUB_A",
            "club_official": "合成俱乐部甲",
            "player": "合成球员甲",
        }],
        identity_registry={
            "schema_version": 1,
            "players": [{
                "player_uid": "player:synth-a",
                "canonical_name": "合成球员甲",
                "provider_player_id": "990101",
                "club": "合成俱乐部甲",
            }],
            "aliases": [],
        },
        game_stats_records=[dnp_game],
        output_dir=tmp_path / "dnp_cards",
    )
    assert built["status"] == "BUILT"
    assert "## Recent Single-Game Log (Player-Game Grain)" in built["markdown"]
    assert "played=0, dnp_excluded=1" in built["markdown"]
    assert built["markdown"].rstrip().splitlines()[-1] == "end_marker: true"
