"""Frozen red-light acceptance tests for REQ-232 (cba-kb-engine v2.3.2).

Covers P0-1 through P0-5 with 100% offline synthetic fixtures (G5 == 0, G6 == 0):
- P0-1: 12-metric PLAYER_SEASON_STATS table (CORE_SEASON_STATS_COLUMNS), deterministic
        (REGULAR before PLAYOFFS, WHOLE_SEASON before TEAM_SPLIT) ordering, unmerged
        split rows, None -> UNKNOWN (never 0), prior full-season baseline fallback,
        and 0-byte backward compatibility for legacy payloads.
- P0-2: 3-domain independent observation horizon (registration, statistics,
        official_announcements) with 4-state DomainHorizonStatus enum, coach
        NOT_APPLICABLE handling, degraded stats isolation from verified registration,
        and Observation Horizon rendering (0-byte diff when absent).
- P0-3: Historical season _ARCHIVE.md migration & top banner under
        enable_v232_card_pipeline=True, unique CURRENT person card fail-closed guard
        (DUPLICATE_CURRENT_PERSON_CARD), and archive_compactor dual-key
        ({"status": "COMPLETE"} / {"state": "COMPLETE"}) acceptance.
- P0-4: --visibility internal_full | public_safe across build_person_card,
        render_research_card, CLI build-person-card, and CLI export-person-cards;
        _PUBLIC_CURRENT.md naming; Visibility: PUBLIC_SAFE header; redaction of
        Document Passage verbatim blocks, internal IDs, private paths, and unverified
        clues; fail-closed verify_public_safe_card_text leak guard.
- P0-5: V215_RELEASE_SPECS["v2.3.2-1"] registration bound to baseline 985c647a
        with enable_v232_card_pipeline=True and zero regression on v2.1.5-1..v2.3.1-1.

Lazy imports inside test bodies guarantee clean pytest collection and differential
falsification on baseline 985c647a459ef7a7c970445a9a37db082eaecc71.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

BASELINE_SHA_V232_1 = "985c647a459ef7a7c970445a9a37db082eaecc71"

EXPECTED_12_CORE_METRICS = (
    "games_played",
    "games_started",
    "minutes_per_game",
    "points_per_game",
    "rebounds_per_game",
    "assists_per_game",
    "steals_per_game",
    "blocks_per_game",
    "turnovers_per_game",
    "field_goals_percentage",
    "three_point_percentage",
    "free_throws_percentage",
)


def _synth_sha(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _make_season_stats_record(
    *,
    player_uid: str = "player:synth-001",
    provider_player_id: str = "SYNTH_P001",
    player_name: str = "合成球员甲",
    season: str = "2026-2027",
    match_type: str = "REGULAR",
    scope: str = "WHOLE_SEASON",
    team_id: Optional[str] = "TEAM_A",
    team_name: Optional[str] = "合成甲俱乐部",
    metrics: Optional[Dict[str, Any]] = None,
    captured_at: str = "2026-11-20T08:00:00Z",
    locator: Optional[str] = None,
) -> Dict[str, Any]:
    team_part = f":{team_id}" if scope == "TEAM_SPLIT" and team_id else ""
    record_id = f"stats:CBA_OFFICIAL:{season}:{match_type}:{scope}:{provider_player_id}{team_part}"
    default_metrics: Dict[str, Any] = {
        "games_played": 36,
        "games_started": 30,
        "minutes_per_game": 31.4,
        "points_per_game": 18.6,
        "rebounds_per_game": 6.2,
        "assists_per_game": 4.5,
        "steals_per_game": 1.3,
        "blocks_per_game": 0.7,
        "turnovers_per_game": 2.1,
        "field_goals_percentage": "48.5%",
        "three_point_percentage": "37.2%",
        "free_throws_percentage": "82.0%",
    }
    if metrics is not None:
        default_metrics.update(metrics)
    return {
        "record_id": record_id,
        "semantic_grain": "PLAYER_SEASON_STATS",
        "schema_version": 1,
        "player_uid": player_uid,
        "identity_status": "RESOLVED",
        "identity_resolution": {
            "status": "RESOLVED",
            "resolution_method": f"TRUSTED_EXTERNAL_ID_LINK:cba_player_id:{provider_player_id}",
            "candidate_player_uids": [player_uid],
        },
        "provider": "CBA_OFFICIAL",
        "provider_player_id": provider_player_id,
        "player_name": player_name,
        "season": season,
        "competition": "CBA",
        "match_type": match_type,
        "scope": scope,
        "team_id": team_id,
        "team_name": team_name,
        "metrics": default_metrics,
        "provenance": {
            "source_uri": f"https://example.invalid/cba/stats/{season}/{provider_player_id}",
            "endpoint": "/api/player-base-list",
            "request_contract": {"season": season, "match_type": match_type},
            "season": season,
            "match_type": match_type,
            "raw_response_sha256": _synth_sha(f"raw:{record_id}"),
            "decoded_sha256": _synth_sha(f"dec:{record_id}"),
            "captured_at": captured_at,
            "locator": locator or f"synth-loc:{season}:{match_type}:{scope}",
        },
        "rights": {
            "classification": "PUBLIC",
            "public_export_allowed": True,
            "private_ai_consumption": "TARGET_SPECIFIC",
        },
    }


def _make_promoted_slice_fixture(
    *,
    player_uid: str = "player:synth-hist-001",
    player_name: str = "合成历史球员乙",
    player_id: str = "SYNTH_H001",
    season: str = "2025-2026",
) -> Dict[str, Any]:
    passage_text = f"合成官方档案：{player_name} {season} 赛季场均 16.5 分。"
    passage_digest = _synth_sha(passage_text)
    source_id = f"src-synth-{season}"
    return {
        "players": [
            {
                "player_uid": player_uid,
                "canonical_name": player_name,
                "provider_player_id": player_id,
            }
        ],
        "stats_records": [
            _make_season_stats_record(
                player_uid=player_uid,
                provider_player_id=player_id,
                player_name=player_name,
                season=season,
                match_type="REGULAR",
                scope="WHOLE_SEASON",
                metrics={"points_per_game": 16.5, "games_played": 40},
            ),
        ],
        "passages": [
            {
                "source_id": source_id,
                "player_uid": player_uid,
                "title": f"{player_name} {season} 合成数据摘录",
                "text": passage_text,
                "revision": "rev-1",
                "locator": f"loc:{season}:p1",
                "digest": passage_digest,
            }
        ],
        "source_objects": [
            {
                "object_id": source_id,
                "target_domain": "fixture_target",
                "rights": "APPROVED",
            }
        ],
        "fact": {
            "fact_id": f"fact-{player_id}-{season}",
            "fact_version": "v1",
            "source_id": source_id,
            "player_uid": player_uid,
            "season": season,
            "club": "合成历史俱乐部",
        },
        "claims": [],
    }


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: List[str],
) -> tuple[int, str, str]:
    from cba_kb import cli

    monkeypatch.setattr(sys, "argv", ["cba-kb", *argv])
    exit_code = 0
    try:
        cli.main()
    except SystemExit as exc:
        exit_code = int(exc.code) if isinstance(exc.code, int) else 1
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


# =============================================================================
# P0-1: 12-Metric Season Averages Table & Prior Full-Season Baseline Fallback
# =============================================================================


class TestP01SeasonStatsTableAndPriorBaseline:
    def test_core_season_stats_columns_exact_12_metrics(self) -> None:
        from cba_kb import stats as stats_mod

        assert hasattr(stats_mod, "CORE_SEASON_STATS_COLUMNS"), (
            "cba_kb.stats must export CORE_SEASON_STATS_COLUMNS"
        )
        assert tuple(stats_mod.CORE_SEASON_STATS_COLUMNS) == EXPECTED_12_CORE_METRICS
        assert len(stats_mod.CORE_SEASON_STATS_COLUMNS) == 12

    def test_target_season_12_metric_table_sorting_unmerged_splits_and_unknown_none(self) -> None:
        from cba_kb.person_card_builder import build_person_card

        master_rows = [
            {
                "season": "2026-2027",
                "club_official": "合成甲俱乐部",
                "player": "合成球员甲",
                "player_uid": "player:synth-001",
                "player_id": "SYNTH_P001",
                "contract_category": "C类",
                "contract_term_official": "2028-07-31",
                "registration_status": "已注册",
            }
        ]
        # Intentionally pass records out of order: PLAYOFFS/TEAM_SPLIT, REGULAR/TEAM_SPLIT,
        # PLAYOFFS/WHOLE_SEASON, REGULAR/WHOLE_SEASON — and include None metrics.
        season_stats = [
            _make_season_stats_record(
                season="2026-2027",
                match_type="PLAYOFFS",
                scope="TEAM_SPLIT",
                team_id="TEAM_B",
                team_name="合成乙俱乐部",
                metrics={"points_per_game": 14.2, "blocks_per_game": None, "three_point_percentage": None},
                locator="loc-po-split",
            ),
            _make_season_stats_record(
                season="2026-2027",
                match_type="REGULAR",
                scope="TEAM_SPLIT",
                team_id="TEAM_A",
                team_name="合成甲俱乐部",
                metrics={"points_per_game": 17.5, "games_started": None, "steals_per_game": 0.0},
                locator="loc-reg-split",
            ),
            _make_season_stats_record(
                season="2026-2027",
                match_type="PLAYOFFS",
                scope="WHOLE_SEASON",
                team_id="TEAM_B",
                team_name="合成乙俱乐部",
                metrics={"points_per_game": 15.8},
                locator="loc-po-whole",
            ),
            _make_season_stats_record(
                season="2026-2027",
                match_type="REGULAR",
                scope="WHOLE_SEASON",
                team_id="TEAM_A",
                team_name="合成甲俱乐部",
                metrics={"points_per_game": 19.4, "turnovers_per_game": None},
                locator="loc-reg-whole",
            ),
        ]

        res = build_person_card(
            "合成球员甲",
            season="2026-2027",
            master_rows=master_rows,
            season_stats_records=season_stats,
        )
        assert res["status"] == "BUILT"
        assert res["posture"] == "CLEAN_OFFICIAL"
        md = res["markdown"]

        # Legacy scalar line preserved under ## Statistical Evidence
        assert "## Statistical Evidence" in md
        assert "- Metric: points_per_game = 19.4" in md

        # New 12-metric table heading rendered under ## Statistical Evidence
        assert "### Season Averages (Player-Season Grain)" in md
        table_section = md.split("### Season Averages (Player-Season Grain)", 1)[1].split("\n## ", 1)[0]

        # All 4 rows present and never merged (WHOLE_SEASON and TEAM_SPLIT on separate rows)
        idx_reg_whole = table_section.find("loc-reg-whole")
        idx_reg_split = table_section.find("loc-reg-split")
        idx_po_whole = table_section.find("loc-po-whole")
        idx_po_split = table_section.find("loc-po-split")
        assert min(idx_reg_whole, idx_reg_split, idx_po_whole, idx_po_split) >= 0, (
            "All 4 season stats rows (WHOLE_SEASON & TEAM_SPLIT for REGULAR & PLAYOFFS) must appear"
        )
        assert idx_reg_whole < idx_reg_split < idx_po_whole < idx_po_split, (
            "Rows must sort (REGULAR before PLAYOFFS, WHOLE_SEASON before TEAM_SPLIT)"
        )

        # None renders strictly as UNKNOWN (never 0 or 0.0), while explicit 0.0 is preserved
        reg_split_line = next(line for line in table_section.splitlines() if "loc-reg-split" in line)
        assert "UNKNOWN" in reg_split_line
        assert "| 0.0 |" in reg_split_line or "| 0 |" in reg_split_line
        po_split_line = next(line for line in table_section.splitlines() if "loc-po-split" in line)
        assert po_split_line.count("UNKNOWN") >= 2

    def test_prior_season_baseline_fallback_keeps_target_season_unknown(self) -> None:
        from cba_kb.person_card_builder import build_person_card

        master_rows = [
            {
                "season": "2026-2027",
                "club_official": "合成甲俱乐部",
                "player": "合成球员甲",
                "player_uid": "player:synth-001",
                "player_id": "SYNTH_P001",
                "contract_category": "B类",
                "contract_term_official": "2027-07-31",
                "registration_status": "已注册",
            }
        ]
        # Only prior seasons (2024-2025 and 2025-2026); NO 2026-2027 stats yet
        season_stats = [
            _make_season_stats_record(
                season="2024-2025",
                match_type="REGULAR",
                scope="WHOLE_SEASON",
                metrics={"points_per_game": 11.1},
                captured_at="2025-05-01T00:00:00Z",
                locator="loc-2425-reg",
            ),
            _make_season_stats_record(
                season="2025-2026",
                match_type="REGULAR",
                scope="WHOLE_SEASON",
                metrics={"points_per_game": 21.3, "assists_per_game": 5.4, "blocks_per_game": None},
                captured_at="2026-05-15T12:00:00Z",
                locator="loc-2526-reg",
            ),
        ]

        res = build_person_card(
            "合成球员甲",
            season="2026-2027",
            master_rows=master_rows,
            season_stats_records=season_stats,
        )
        assert res["status"] == "BUILT"
        # Target season posture MUST remain UNKNOWN / NO_SEASON_STATS_RECORDED_YET
        assert res["posture"] == "UNKNOWN"
        assert res["unknown_subkind"] == "NO_SEASON_STATS_RECORDED_YET"
        assert isinstance(res.get("prior_season_baseline"), dict)
        assert res["prior_season_baseline"]["season"] == "2025-2026"
        assert res["prior_season_baseline"]["target_season"] == "2026-2027"
        assert len(res["prior_season_baseline"]["rows"]) == 1

        md = res["markdown"]
        assert "- Posture: UNKNOWN" in md
        assert "- Unknown: NO_SEASON_STATS_RECORDED_YET" in md
        expected_heading = (
            "### Prior Full-Season Baseline (2025-2026 — Historical Reference Only, Not 2026-2027)"
        )
        assert expected_heading in md
        baseline_section = md.split(expected_heading, 1)[1].split("\n## ", 1)[0]
        assert "21.3" in baseline_section
        assert "5.4" in baseline_section
        assert "UNKNOWN" in baseline_section
        assert "2026-05-15T12:00:00Z" in baseline_section
        assert "loc-2526-reg" in baseline_section
        assert "loc-2425-reg" not in baseline_section

    def test_legacy_minimal_payload_zero_byte_diff_vs_v231(self) -> None:
        from cba_kb.kc_render import render_research_card

        minimal_payload = {
            "metric": "points_per_game",
            "grains": {
                "STATS": {
                    "posture": "CLEAN_OFFICIAL",
                    "season": "2026-2027",
                    "match_type": "REGULAR",
                    "scope": "WHOLE_SEASON",
                    "metric": "points_per_game",
                    "value": 18.6,
                }
            },
            "evidence_transparency": {
                "posture": "CLEAN_OFFICIAL",
                "adopted_value": 18.6,
                "subject_player_uid": "player:synth-legacy",
                "scope_meta": {
                    "player_name": "合成兼容甲",
                    "player_id": "SYNTH_L01",
                    "season": "2026-2027",
                    "match_type": "REGULAR",
                    "scope": "WHOLE_SEASON",
                    "games_played": 30,
                    "captured_at": "2026-11-20T00:00:00Z",
                    "locator": "loc-legacy-1",
                },
            },
        }

        expected_v231_exact = (
            "# 人物研究卡｜合成兼容甲（SYNTH_L01）｜2026-2027｜CURRENT "
            "— Research Card (Serving Projection)\n"
            "\n"
            "## Statistical Evidence\n"
            "- Posture: CLEAN_OFFICIAL\n"
            "- Player: 合成兼容甲 (SYNTH_L01)\n"
            "- Season: 2026-2027\n"
            "- Match Type: REGULAR\n"
            "- Scope: WHOLE_SEASON\n"
            "- Games Played: 30\n"
            "- Cutoff / Captured At: 2026-11-20T00:00:00Z\n"
            "- Locator: loc-legacy-1\n"
            "- Metric: points_per_game = 18.6\n"
            "- Authority: PRIMARY_OFFICIAL_RECORD\n"
            "\n"
            "end_marker: true\n"
        )
        rendered = render_research_card(minimal_payload, evidence_verified=True)
        assert rendered == expected_v231_exact


# =============================================================================
# P0-2: 3-Domain Independent Observation Horizon & 4-State Enum
# =============================================================================


class TestP02ThreeDomainObservationHorizon:
    def test_domain_horizon_status_enum_and_coach_not_applicable(self) -> None:
        from cba_kb import consumer_surface as cs

        assert hasattr(cs, "DomainHorizonStatus"), "consumer_surface must export DomainHorizonStatus"
        assert hasattr(cs, "build_person_domain_horizons"), (
            "consumer_surface must export build_person_domain_horizons"
        )

        enum_cls = cs.DomainHorizonStatus
        assert enum_cls.VERIFIED_PRESENT == "VERIFIED_PRESENT"
        assert enum_cls.OBSERVED_EMPTY == "OBSERVED_EMPTY"
        assert enum_cls.SOURCE_GAP_UNKNOWN == "SOURCE_GAP_UNKNOWN"
        assert enum_cls.NOT_APPLICABLE == "NOT_APPLICABLE"

        coach_horizons = cs.build_person_domain_horizons(
            entity_role="HEAD_COACH",
            has_registration_records=True,
            registration_observed_until="2026-11-20T00:00:00Z",
            has_season_stats=False,
            has_game_stats=False,
            has_official_announcements=False,
            announcements_observed_until="2026-11-20T00:00:00Z",
        )
        assert set(coach_horizons.keys()) == {"registration", "statistics", "official_announcements"}
        for dom in ("registration", "statistics", "official_announcements"):
            assert {"status", "observed_until", "note"} <= set(coach_horizons[dom].keys())

        assert coach_horizons["registration"]["status"] == "VERIFIED_PRESENT"
        assert coach_horizons["statistics"]["status"] == "NOT_APPLICABLE"
        assert coach_horizons["official_announcements"]["status"] == "OBSERVED_EMPTY"

    def test_degraded_stats_isolation_does_not_pollute_verified_registration(self) -> None:
        from cba_kb.consumer_surface import DomainHorizonStatus, build_person_domain_horizons
        from cba_kb.person_card_builder import build_person_card

        # Direct pure-function check
        horizons = build_person_domain_horizons(
            entity_role="PLAYER",
            has_registration_records=True,
            registration_observed_until="2026-11-20T00:00:00Z",
            has_season_stats=False,
            has_game_stats=False,
            game_stats_degraded=True,
            has_official_announcements=False,
            announcements_observed_until="2026-11-20T00:00:00Z",
        )
        assert horizons["registration"]["status"] == DomainHorizonStatus.VERIFIED_PRESENT
        assert horizons["statistics"]["status"] == DomainHorizonStatus.SOURCE_GAP_UNKNOWN
        assert horizons["official_announcements"]["status"] == DomainHorizonStatus.OBSERVED_EMPTY

        # End-to-end wiring via build_person_card -> render_research_card
        master_rows = [
            {
                "season": "2026-2027",
                "club_official": "合成丙俱乐部",
                "player": "合成球员丙",
                "player_uid": "player:synth-003",
                "player_id": "SYNTH_P003",
                "contract_category": "C类",
                "contract_term_official": "2028-07-31",
                "registration_status": "已注册",
            }
        ]
        res = build_person_card(
            "合成球员丙",
            season="2026-2027",
            master_rows=master_rows,
            game_stats_degraded=True,
        )
        assert res["status"] == "BUILT"
        dh = res.get("domain_horizons")
        assert isinstance(dh, dict)
        assert dh["registration"]["status"] == "VERIFIED_PRESENT"
        assert dh["statistics"]["status"] == "SOURCE_GAP_UNKNOWN"

        md = res["markdown"]
        assert "## Observation Horizon" in md
        obs_section = md.split("## Observation Horizon", 1)[1].split("\n## ", 1)[0]
        assert "registration" in obs_section
        assert "VERIFIED_PRESENT" in obs_section
        assert "statistics" in obs_section
        assert "SOURCE_GAP_UNKNOWN" in obs_section
        assert "official_announcements" in obs_section

    def test_render_research_card_with_domain_horizons_and_zero_byte_diff_when_absent(self) -> None:
        from cba_kb.kc_render import render_research_card

        base_payload = {
            "metric": "points_per_game",
            "grains": {"STATS": {"posture": "UNKNOWN", "season": "2026-2027"}},
            "evidence_transparency": {
                "posture": "UNKNOWN",
                "unknown_subkind": "NO_SEASON_STATS_RECORDED_YET",
                "scope_meta": {"season": "2026-2027"},
            },
        }
        without_dh = render_research_card(base_payload, evidence_verified=True)
        assert "## Observation Horizon" not in without_dh

        with_dh_payload = dict(base_payload)
        with_dh_payload["domain_horizons"] = {
            "registration": {
                "status": "VERIFIED_PRESENT",
                "observed_until": "2026-11-20T00:00:00Z",
                "note": "Domestic registration verified",
            },
            "statistics": {
                "status": "NOT_APPLICABLE",
                "observed_until": None,
                "note": "Coach role does not have player boxscore stats",
            },
            "official_announcements": {
                "status": "OBSERVED_EMPTY",
                "observed_until": "2026-11-20T00:00:00Z",
                "note": "No matching announcements within observation horizon",
            },
        }
        with_dh = render_research_card(with_dh_payload, evidence_verified=True)
        assert "## Observation Horizon" in with_dh
        assert "VERIFIED_PRESENT" in with_dh
        assert "NOT_APPLICABLE" in with_dh
        assert "OBSERVED_EMPTY" in with_dh


# =============================================================================
# P0-3: Historical _ARCHIVE.md Migration, Unique CURRENT Guard & Dual-Key Compactor
# =============================================================================


class TestP03HistoricalArchiveUniqueGuardAndCompactorDualKey:
    def test_historical_season_promoted_artifacts_emit_archive_filename_and_banner(self) -> None:
        from cba_kb.consumer_surface import build_promoted_research_slice_artifacts

        fixture = _make_promoted_slice_fixture(season="2025-2026")
        artifacts = build_promoted_research_slice_artifacts(
            fixture,
            player_uid="player:synth-hist-001",
            player_name="合成历史球员乙",
            player_id="SYNTH_H001",
            season="2025-2026",
            match_type="REGULAR",
            scope="WHOLE_SEASON",
            active_league_season="2026-2027",
            enable_v232_card_pipeline=True,
        )
        assert artifacts, "Expected promoted research slice artifacts"
        expected_banner = (
            "> [HISTORICAL_SEASON_ARCHIVE] 本文件为 2025-2026 历史赛季定格档案，"
            "非当前活跃赛季（2026-2027）主卡"
        )
        for logical_key, spec in artifacts.items():
            name = spec["name"]
            assert name.endswith("_ARCHIVE.md"), (
                f"Historical artifact {logical_key} must end with _ARCHIVE.md, got {name}"
            )
            assert not name.endswith("_CURRENT.md")
            text = spec["content"].decode("utf-8")
            first_nonempty = next(line for line in text.splitlines() if line.strip())
            assert first_nonempty == expected_banner
            assert text.rstrip().endswith("end_marker: true")

        # Legacy mode (enable_v232_card_pipeline=False) preserves _CURRENT.md without archive banner
        legacy_artifacts = build_promoted_research_slice_artifacts(
            fixture,
            player_uid="player:synth-hist-001",
            player_name="合成历史球员乙",
            player_id="SYNTH_H001",
            season="2025-2026",
            match_type="REGULAR",
            scope="WHOLE_SEASON",
        )
        for spec in legacy_artifacts.values():
            assert spec["name"].endswith("_CURRENT.md")
            assert "[HISTORICAL_SEASON_ARCHIVE]" not in spec["content"].decode("utf-8")

    def test_assert_unique_current_card_per_person_fail_closed(self) -> None:
        from cba_kb import consumer_surface as cs

        assert hasattr(cs, "assert_unique_current_card_per_person"), (
            "consumer_surface must export assert_unique_current_card_per_person"
        )

        # Duplicate CURRENT cards across two seasons for same person -> FAIL-CLOSED
        dup_targets = {
            "research/card_old": {
                "name": "研究卡_合成重复丁_SYNTH_D01_2025-2026_CURRENT.md",
            },
            "research/card_new": {
                "name": "研究卡_合成重复丁_合成甲俱乐部_2026-2027_CURRENT.md",
            },
        }
        with pytest.raises(ValueError, match="DUPLICATE_CURRENT_PERSON_CARD"):
            cs.assert_unique_current_card_per_person(dup_targets)

        # Duplicate CURRENT cards across paths (sequence input) -> FAIL-CLOSED
        with pytest.raises(ValueError, match="DUPLICATE_CURRENT_PERSON_CARD"):
            cs.assert_unique_current_card_per_person([
                "current/研究卡_合成重复丁_SYNTH_D01_2026-2027_CURRENT.md",
                "snowball/研究卡_合成重复丁_合成甲俱乐部_2026-2027_CURRENT.md",
            ])

        # Old season migrated to _ARCHIVE.md + single _CURRENT.md -> PASS
        clean_targets = {
            "research/card_old": {
                "name": "研究卡_合成重复丁_SYNTH_D01_2025-2026_ARCHIVE.md",
            },
            "research/card_new": {
                "name": "研究卡_合成重复丁_合成甲俱乐部_2026-2027_CURRENT.md",
            },
            "research/card_other": {
                "name": "研究卡_合成另一人戊_合成乙俱乐部_2026-2027_CURRENT.md",
            },
        }
        cs.assert_unique_current_card_per_person(clean_targets)

    def test_archive_compactor_release_state_accepts_both_status_and_state_keys(self) -> None:
        from cba_kb.archive_compactor import (
            ArchiveCompactionError,
            _validate_authoritative_release_state,
        )

        # Legacy "status": "COMPLETE" still accepted
        assert _validate_authoritative_release_state(
            release_status={"release_id": "v2.3.1-1", "status": "COMPLETE"}
        ) is True

        # New "state": "COMPLETE" (production release_status.json schema) accepted
        assert _validate_authoritative_release_state(
            release_status={"current_release_id": "v2.3.1-1", "state": "COMPLETE"}
        ) is True

        # Missing both status and state -> fail-closed
        with pytest.raises(ArchiveCompactionError, match="ARCHIVE_COMPACT_INVALID_RELEASE_STATE"):
            _validate_authoritative_release_state(
                release_status={"release_id": "v2.3.1-1"}
            )

        # Non-COMPLETE state -> fail-closed
        with pytest.raises(ArchiveCompactionError, match="ARCHIVE_COMPACT_INVALID_RELEASE_STATE"):
            _validate_authoritative_release_state(
                release_status={"release_id": "v2.3.1-1", "state": "PUBLISHING"}
            )


# =============================================================================
# P0-4: --visibility internal_full | public_safe & Fail-Closed Leak Guard
# =============================================================================


class TestP04VisibilityAndPublicSafeLeakGuard:
    def test_invalid_visibility_fails_closed(self) -> None:
        from cba_kb.kc_render import render_research_card
        from cba_kb.person_card_builder import build_person_card

        minimal_payload = {
            "metric": "points_per_game",
            "grains": {"STATS": {"posture": "UNKNOWN", "season": "2026-2027"}},
            "evidence_transparency": {
                "posture": "UNKNOWN",
                "unknown_subkind": "NO_SEASON_STATS_RECORDED_YET",
                "scope_meta": {"season": "2026-2027"},
            },
        }
        with pytest.raises(ValueError, match="INVALID_CARD_VISIBILITY"):
            render_research_card(minimal_payload, evidence_verified=True, visibility="external_raw")

        with pytest.raises(ValueError, match="INVALID_CARD_VISIBILITY"):
            build_person_card("合成球员甲", season="2026-2027", master_rows=[], visibility="invalid_mode")

    def test_public_safe_strips_internal_artifacts_and_passes_leak_guard(self) -> None:
        from cba_kb import kc_visibility
        from cba_kb.kc_render import render_research_card
        from cba_kb.person_card_builder import build_person_card

        assert hasattr(kc_visibility, "verify_public_safe_card_text"), (
            "kc_visibility must export verify_public_safe_card_text"
        )

        # Construct synthetic private markers dynamically so static scanners never false-positive
        synth_local_path = "/" + "Users" + "/synth_tester/private_notes.md"
        synth_raw_sha = _synth_sha("synth-private-doc-sha")

        master_rows = [
            {
                "season": "2026-2027",
                "club_official": "合成公开俱乐部",
                "player": "合成公开甲",
                "player_uid": "player:synth-pub-01",
                "player_id": "SYNTH_PUB01",
                "contract_category": "C类",
                "contract_term_official": "2028-07-31",
                "registration_status": "已注册",
            }
        ]
        foreign_rows = [
            {
                "club": "合成公开俱乐部",
                "player_en": "Synth Public",
                "player_zh": "合成公开甲",
                "nationality": "合成国",
                "position": "后卫",
                "jersey_number": "7",
                "published_at": "2026-10-01",
                "doc_id": "doc:internal-foreign-snap-001",
                "raw_sha256": synth_raw_sha,
            }
        ]
        doc_bundles = [
            {
                "doc_id": "doc:public-ann-001",
                "source_tier": "T1",
                "completeness": "full",
                "published_at": "2026-11-10",
                "title": "合成公开甲官方公告",
                "canonical_url": "https://example.invalid/official/ann-001",
                "matched_persons": ["合成公开甲"],
            },
            {
                "doc_id": "doc:private-clip-002",
                "source_tier": "MANUAL",
                "completeness": "partial",
                "published_at": "2026-11-11",
                "title": "Web 剪藏 内部未核实线索 合成公开甲",
                "canonical_url": f"r2://private-bucket/10_sources/{synth_local_path}",
                "matched_persons": ["合成公开甲"],
            },
        ]

        res = build_person_card(
            "合成公开甲",
            season="2026-2027",
            master_rows=master_rows,
            foreign_rows=foreign_rows,
            document_bundles=doc_bundles,
            visibility="public_safe",
        )
        assert res["status"] == "BUILT"
        assert res["filename"].endswith("_PUBLIC_CURRENT.md")
        md = res["markdown"]
        assert "Visibility: PUBLIC_SAFE" in md
        # Keeps structured facts & public https:// citation
        assert "合成公开甲" in md
        assert "合成公开俱乐部" in md
        assert "https://example.invalid/official/ann-001" in md
        # Strips internal IDs, hashes, private paths, and unverified/non-public clues
        assert "doc:internal-foreign-snap-001" not in md
        assert "doc:public-ann-001" not in md
        assert "doc:private-clip-002" not in md
        assert synth_raw_sha not in md
        assert "Web 剪藏" not in md
        assert "10_sources" not in md
        assert "r2://" not in md
        assert synth_local_path not in md
        assert "## Document Passage" not in md

        # Leak guard passes on clean public_safe card
        kc_visibility.verify_public_safe_card_text(md)

        # Also verify render_research_card strips Document Passage verbatim block in public_safe
        passage_payload = {
            "metric": "points_per_game",
            "grains": {
                "STATS": {"posture": "UNKNOWN", "season": "2026-2027"},
                "DOCUMENT": {
                    "source_id": "doc:internal-passage-01",
                    "locator": "10_sources/Web 剪藏/clip.md",
                    "verbatim": "内部剪藏原文逐字引用内容，不得外泄。",
                },
            },
            "evidence_transparency": {
                "posture": "UNKNOWN",
                "unknown_subkind": "NO_SEASON_STATS_RECORDED_YET",
                "scope_meta": {"season": "2026-2027"},
            },
        }
        pub_rendered = render_research_card(
            passage_payload, evidence_verified=True, visibility="public_safe"
        )
        assert "Visibility: PUBLIC_SAFE" in pub_rendered
        assert "## Document Passage" not in pub_rendered
        assert "内部剪藏原文逐字引用内容" not in pub_rendered
        assert "10_sources" not in pub_rendered
        kc_visibility.verify_public_safe_card_text(pub_rendered)

    def test_verify_public_safe_card_text_rejects_injected_leaks(self) -> None:
        from cba_kb.kc_visibility import verify_public_safe_card_text

        clean_card = (
            "# Research Card (Serving Projection)\n"
            "- Visibility: PUBLIC_SAFE\n\n"
            "## Statistical Evidence\n"
            "- Posture: UNKNOWN\n"
            "- Season: 2026-2027\n\n"
            "end_marker: true\n"
        )
        verify_public_safe_card_text(clean_card)

        synth_local_path = "/" + "Users" + "/synth_tester/secret.md"
        synth_drive_token = "1" + "SynthLeakedDriveIdToken9876543210"
        synth_sha64 = _synth_sha("injected-sha")

        leaked_samples = [
            clean_card + f"\n- Path: {synth_local_path}\n",
            clean_card + "\n- Source: 10_sources/inbox/note.md\n",
            clean_card + "\n- Source: Web 剪藏/article.md\n",
            clean_card + "\n- URI: r2://synth-bucket/cards/x.md\n",
            clean_card + "\n- URI: s3://synth-bucket/cards/x.md\n",
            clean_card + "\n## Document Passage\n> 逐字引用原文\n",
            clean_card + f"\n- raw_sha256: {synth_sha64}\n",
            clean_card + f"\n- Drive: {synth_drive_token}\n",
        ]
        for sample in leaked_samples:
            with pytest.raises(ValueError, match="PUBLIC_SAFE_REDACTION_VIOLATION"):
                verify_public_safe_card_text(sample)

    def test_cli_build_person_card_and_export_person_cards_visibility_wiring(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        master_csv = tmp_path / "MASTER.csv"
        master_csv.write_text(
            "season,club_official,player,player_uid,player_id,contract_category,contract_term_official,registration_status\n"
            "2026-2027,合成导出俱乐部,合成导出甲,player:synth-exp-1,SYNTH_E01,C类,2028-07-31,已注册\n"
            "2026-2027,合成导出俱乐部,合成导出乙,player:synth-exp-2,SYNTH_E02,B类,2027-07-31,已注册\n",
            encoding="utf-8",
        )
        out_single = tmp_path / "single_out"
        code, out, err = _run_cli(
            monkeypatch,
            capsys,
            [
                "build-person-card",
                "--person",
                "合成导出甲",
                "--season",
                "2026-2027",
                "--master-csv",
                str(master_csv),
                "--output-dir",
                str(out_single),
                "--visibility",
                "public_safe",
            ],
        )
        assert code == 0, f"build-person-card failed: {err}"
        payload = json.loads(out)
        assert payload["status"] == "BUILT"
        assert payload["visibility"] == "public_safe"
        assert payload["filename"].endswith("_PUBLIC_CURRENT.md")
        assert "Visibility: PUBLIC_SAFE" in payload["markdown"]
        assert (out_single / payload["filename"]).is_file()

        out_batch = tmp_path / "batch_out"
        code2, out2, err2 = _run_cli(
            monkeypatch,
            capsys,
            [
                "export-person-cards",
                "--players",
                "合成导出甲,合成导出乙",
                "--season",
                "2026-2027",
                "--master-csv",
                str(master_csv),
                "--output-dir",
                str(out_batch),
                "--visibility",
                "public_safe",
            ],
        )
        assert code2 == 0, f"export-person-cards failed: {err2}"
        batch_payload = json.loads(out2)
        assert batch_payload["visibility"] == "public_safe"
        assert batch_payload["exported_count"] == 2
        exported_files = sorted(p.name for p in out_batch.glob("*.md"))
        assert len(exported_files) == 2
        assert all(name.endswith("_PUBLIC_CURRENT.md") for name in exported_files)


# =============================================================================
# P0-5: V215_RELEASE_SPECS["v2.3.2-1"] & Zero Historical Regression
# =============================================================================


class TestP05ReleaseSpecV232:
    def test_v232_1_release_spec_registered_and_historical_specs_intact(self) -> None:
        from cba_kb.orchestration.release.prepare_v215 import V215_RELEASE_SPECS

        assert "v2.3.2-1" in V215_RELEASE_SPECS
        spec = V215_RELEASE_SPECS["v2.3.2-1"]
        assert spec["product_baseline_sha"] == BASELINE_SHA_V232_1
        assert spec.get("enable_v232_card_pipeline") is True
        assert spec.get("active_league_season") == "2026-2027"
        assert spec.get("incremental_delta_only") is True
        assert spec.get("archive_format") == "bundle_v1"

        for historical_rel in ("v2.1.5-1", "v2.2.0-1", "v2.2.1-1", "v2.2.2-1", "v2.3.0-1", "v2.3.1-1"):
            assert historical_rel in V215_RELEASE_SPECS
            assert not V215_RELEASE_SPECS[historical_rel].get("enable_v232_card_pipeline", False)
