"""Dedicated regression tests for REQ-223 Q2 Round 1 remediation (PR #273).

Covers all 4 Round 1 blockers without modifying Commit 1 frozen tests or spec:
- Blocker 1 (P0-1-SCHEMA-VERSION-TYPE): strict int check on `schema_version` (reject 1.0, True, "1").
- Blocker 2 (P0-2-CROSS-KEY-REVISION): fail-closed revision chain binding (missing target,
  cross-player, cross-game, cross-season, forked chain, self/multi-hop cycles).
- Blocker 3 (P0-3-HOMONYM-AUTO-MERGE): never auto-merge same-name players across conflicting
  clubs into a single RESOLVED identity; return UNDECIDED unless explicit link/transfer exists.
- Blocker 4 (P0-4-DNP-ONLY-LOG-OMITTED): always render `## Recent Single-Game Log (Player-Game Grain)`
  when `recent_games_summary` is provided, including DNP-only players (played=0, dnp_excluded=1).

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
# Blocker 1: P0-1-SCHEMA-VERSION-TYPE
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_version", ["1.0", "true", '"1"', "2", "0"])
def test_q2_r1_blocker1_curation_profile_rejects_non_int_schema_version(
    tmp_path: Path, bad_version: str
):
    profile_file = tmp_path / "bad_version_profile.yaml"
    profile_file.write_text(
        "\n".join([
            f"schema_version: {bad_version}",
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


@pytest.mark.parametrize("bad_chars", ["140.0", "true", "0", "-5"])
def test_q2_r1_blocker1_curation_profile_rejects_non_int_excerpt_chars(
    tmp_path: Path, bad_chars: str
):
    profile_file = tmp_path / "bad_chars_profile.yaml"
    profile_file.write_text(
        "\n".join([
            "schema_version: 1",
            "noise_event_classes: ['SYNTH_NOISE']",
            "noise_keywords: ['SYNTH_KW']",
            "historical_pinned_event_classes: ['SYNTH_HIST']",
            "historical_pinned_keywords: ['SYNTH_OLD']",
            "pinned_marker_keywords: ['SYNTH_PIN']",
            "protected_event_classes: ['注册与转会']",
            "active_competitive_keywords: ['签约']",
            "condolence_keywords: ['SYNTH_COND']",
            "condolence_exempt_keywords: ['签约']",
            f"compact_announcement_excerpt_chars: {bad_chars}",
        ]) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="CURATION_PROFILE_INVALID"):
        consumer_surface.load_curation_profile(profile_file)


# ---------------------------------------------------------------------------
# Blocker 2: P0-2-CROSS-KEY-REVISION
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


# ---------------------------------------------------------------------------
# Blocker 3: P0-3-HOMONYM-AUTO-MERGE
# ---------------------------------------------------------------------------

def test_q2_r1_blocker3_same_name_different_club_returns_undecided_not_merged():
    # Registry has ONLY ONE Zhang San (Club A), while MASTER has both Club A and Club B
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

    # Explicit transfer evidence (former_club) or trusted record_links allows valid merge
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


def test_q2_r1_blocker3_foreign_same_name_different_club_returns_undecided():
    identity_registry = {
        "schema_version": 1,
        "players": [
            {
                "player_uid": "player:synth-for-a",
                "canonical_name": "合成外援同名",
                "provider_player_id": "992001",
                "club": "合成俱乐部甲",
            }
        ],
        "aliases": [{"alias": "SYNTH FOREIGN DUP", "player_uid": "player:synth-for-a"}],
    }
    foreign_rows = [
        {
            "club": "合成俱乐部乙",
            "player_en": "SYNTH FOREIGN DUP",
            "player_zh": "合成外援同名",
            "nationality": "SYNTH_NATION",
            "position": "SG",
            "jersey_number": "7",
        }
    ]
    res = person_card_builder.resolve_person_query(
        "合成外援同名",
        foreign_rows=foreign_rows,
        identity_registry=identity_registry,
    )
    assert res["status"] == "UNDECIDED"
    assert len(res["candidates"]) == 2


def test_q2_r1_blocker3_registry_club_official_and_master_only_transfer_edge_cases():
    # 1. Registry uses club_official/club_id instead of `club` -> must NOT silently merge with conflicting MASTER club
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
    master_conflict = [
        {
            "record_key": "2026-2027|SYNTH_CLUB_A|合成同名字段甲",
            "season": "2026-2027",
            "club_id": "SYNTH_CLUB_A",
            "club_official": "合成俱乐部甲",
            "player": "合成同名字段甲",
        }
    ]
    res_conflict = person_card_builder.resolve_person_query(
        "合成同名字段甲",
        master_rows=master_conflict,
        identity_registry=reg,
    )
    assert res_conflict["status"] == "UNDECIDED"
    assert len(res_conflict["candidates"]) == 2
    assert {c["current_club"] for c in res_conflict["candidates"]} == {"合成俱乐部甲", "合成俱乐部乙"}

    # 2. Player absent from identity_registry transferring across 2 MASTER rows with explicit former_club -> RESOLVED
    master_transfer_only = [
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
    ]
    res_xfr = person_card_builder.resolve_person_query(
        "合成转会球员",
        master_rows=master_transfer_only,
        identity_registry={"schema_version": 1, "players": [], "aliases": []},
    )
    assert res_xfr["status"] == "RESOLVED"
    assert res_xfr["person"]["current_club"] == "合成俱乐部乙"
    assert set(res_xfr["person"]["clubs_history"]) == {"合成俱乐部甲", "合成俱乐部乙"}


# ---------------------------------------------------------------------------
# Blocker 4: P0-4-DNP-ONLY-LOG-OMITTED
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
