"""Frozen red-light acceptance tests for REQ-242 (cba-kb-engine v2.4.2).

Covers P0-E1 through P0-E6 plus real end-to-end CLI/pipeline wiring using
100% offline synthetic/anonymized fixtures (zero real Drive IDs, zero private
instance paths, zero network calls):

- P0-E1: Exact `provider_player_ids` matching against `season_stats.jsonl`
         (including homonym disambiguation with 100% zero cross-contamination)
         and career total row semantics (`FULL_CBA_CAREER` vs
         `PARTIAL_SEASONS_OBSERVED` / unspecified).
- P0-E2: Default-collapsed `<details class="gamelog-drawer">` with `<summary>`
         displaying latest `game_date`:
         `展开查看近 N 场已记录单场数据（最新比赛日：YYYY-MM-DD｜场均 X.X 分 Y.Y 篮板 Z.Z 助攻）`.
- P0-E3: Consuming `club_season_roster_ledger.jsonl`
         (`cba-kb.club-season-roster-ledger.v1`) for 5-way movement split in
         club panorama card & static site `index.html` (retired player -> head
         coach MUST appear in `退役转任本队教练` and NEVER in `离队/转会球员`).
- P0-E4: 4-template `editorial_brief.py` lead paragraph with anti-duplication
         guard (no `效力于离队`, no `曾入选2026年8月加盟...`, no
         `曾入选多次CBA总冠军`), plus `static_site_builder.py` related-person
         labels abolishing `师徒`, using `同期教练与球员`, adding `大学校友`,
         and keeping `青训同梯队`/`同届选秀`/`共同经历`.
- P0-E5: Adjacent `[1][2]` / `[1][2][3]` inline citation rendering into
         independent `<sup class="cite-ref"><a ... href="#fn-N">[N]</a></sup>`,
         clickable `https://` links in footer `参考资料` & JSON-LD `citation`,
         Chinese public source name mapping for internal keys, and fail-closed
         `verify_public_safe_card_text()` + static site export gate blocking
         `.csv`, `.jsonl`, `team_staff_snapshots`, and `club_announcement:`.
- P0-E6: `person_bio_profiles.json` (`cba-kb.person-bio-profile.v2`) provenance
         gate rejecting any `VERIFIED` entry/attribute missing 64-hex
         `raw_sha256` or source anchor (`source_url` / `bundle_id`).
- E2E Wiring: Full pipeline from synthetic data dir -> CLI `build-person-card`
         + `build-club-card` + `build-public-site` -> `index.html`,
         `persons/*.html`, JSON-LD, `llms.txt`, `sitemap.xml`.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest


def _synth_sha(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


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


def _extract_json_ld_blocks(html_text: str) -> List[Dict[str, Any]]:
    blocks = re.findall(
        r'<script\s+type="application/ld\+json">\s*(.*?)\s*</script>',
        html_text,
        flags=re.DOTALL,
    )
    return [json.loads(b.replace("<\\/", "</")) for b in blocks]


def _make_homonym_season_stats() -> List[Dict[str, Any]]:
    """Two distinct foreign players sharing surname '约翰逊' with different provider_player_id."""
    return [
        {
            "record_id": "stat-johnson-a-2024-reg",
            "player_uid": "player:synth-johnson-alpha",
            "provider_player_id": "910001",
            "player_name": "约翰逊",
            "season": "2024-2025",
            "match_type": "REGULAR",
            "scope": "WHOLE_SEASON",
            "team_id": "29131",
            "team_name": "云海新锐",
            "metrics": {
                "games_played": 32,
                "games_started": 30,
                "minutes_per_game": 33.5,
                "points_per_game": 24.5,
                "rebounds_per_game": 11.2,
                "assists_per_game": 3.4,
                "steals_per_game": 1.1,
                "blocks_per_game": 1.8,
                "turnovers_per_game": 2.3,
                "field_goals_percentage": 56.2,
                "three_point_percentage": 31.0,
                "free_throws_percentage": 78.5,
            },
            "provenance": {
                "captured_at": "2025-04-12T00:00:00Z",
                "locator": "https://example.invalid/cba/stats/2024-2025/910001",
            },
        },
        {
            "record_id": "stat-johnson-a-2025-reg",
            "player_uid": "player:synth-johnson-alpha",
            "provider_player_id": "910001",
            "player_name": "约翰逊",
            "season": "2025-2026",
            "match_type": "REGULAR",
            "scope": "WHOLE_SEASON",
            "team_id": "29131",
            "team_name": "云海新锐",
            "metrics": {
                "games_played": 28,
                "games_started": 28,
                "minutes_per_game": 34.0,
                "points_per_game": 22.0,
                "rebounds_per_game": 10.5,
                "assists_per_game": 4.0,
                "steals_per_game": 1.3,
                "blocks_per_game": 1.5,
                "turnovers_per_game": 2.1,
                "field_goals_percentage": 54.0,
                "three_point_percentage": 33.5,
                "free_throws_percentage": 80.0,
            },
            "provenance": {
                "captured_at": "2026-04-20T00:00:00Z",
                "locator": "https://example.invalid/cba/stats/2025-2026/910001",
            },
        },
        {
            "record_id": "stat-johnson-b-2025-reg",
            "player_uid": "player:synth-johnson-beta",
            "provider_player_id": "920002",
            "player_name": "约翰逊",
            "season": "2025-2026",
            "match_type": "REGULAR",
            "scope": "WHOLE_SEASON",
            "team_id": "29140",
            "team_name": "山河领航",
            "metrics": {
                "games_played": 10,
                "games_started": 2,
                "minutes_per_game": 14.0,
                "points_per_game": 7.1,
                "rebounds_per_game": 2.0,
                "assists_per_game": 1.5,
                "steals_per_game": 0.4,
                "blocks_per_game": 0.1,
                "turnovers_per_game": 1.0,
                "field_goals_percentage": 41.0,
                "three_point_percentage": 28.0,
                "free_throws_percentage": 70.0,
            },
            "provenance": {
                "captured_at": "2026-04-20T00:00:00Z",
                "locator": "https://example.invalid/cba/stats/2025-2026/920002",
            },
        },
    ]


def _make_synth_roster_ledger_rows() -> List[Dict[str, Any]]:
    """Synthetic club_season_roster_ledger.jsonl rows (cba-kb.club-season-roster-ledger.v1)."""
    return [
        {
            "schema": "cba-kb.club-season-roster-ledger.v1",
            "organization_id": "ORG-SYNTH-CLOUD",
            "club_name": "云海新锐俱乐部",
            "from_season": "2025-2026",
            "to_season": "2026-2027",
            "person_uid": "PERS-SYNTH-RET001",
            "person_name": "林启明",
            "movement_bucket": "RETAINED",
            "player_type": "DOMESTIC",
            "note": "2025-2026 与 2026-2027 连续在册国内球员",
        },
        {
            "schema": "cba-kb.club-season-roster-ledger.v1",
            "organization_id": "ORG-SYNTH-CLOUD",
            "club_name": "云海新锐俱乐部",
            "from_season": "2025-2026",
            "to_season": "2026-2027",
            "person_uid": "PERS-SYNTH-RETFOR1",
            "person_name": "卡特·布鲁克斯",
            "movement_bucket": "RETAINED",
            "player_type": "FOREIGN",
            "note": "留用外援",
        },
        {
            "schema": "cba-kb.club-season-roster-ledger.v1",
            "organization_id": "ORG-SYNTH-CLOUD",
            "club_name": "云海新锐俱乐部",
            "from_season": "2025-2026",
            "to_season": "2026-2027",
            "person_uid": "PERS-SYNTH-NEW001",
            "person_name": "许晨阳",
            "movement_bucket": "NEW_SIGNING_OR_YOUTH_PROMOTION",
            "player_type": "DOMESTIC",
            "note": "青训上调一队",
        },
        {
            "schema": "cba-kb.club-season-roster-ledger.v1",
            "organization_id": "ORG-SYNTH-CLOUD",
            "club_name": "云海新锐俱乐部",
            "from_season": "2025-2026",
            "to_season": "2026-2027",
            "person_uid": "PERS-SYNTH-COACH01",
            "person_name": "周景行",
            "movement_bucket": "RETIRED_TO_CLUB_COACH",
            "player_type": "DOMESTIC",
            "note": "退役转任本队主教练",
        },
        {
            "schema": "cba-kb.club-season-roster-ledger.v1",
            "organization_id": "ORG-SYNTH-CLOUD",
            "club_name": "云海新锐俱乐部",
            "from_season": "2025-2026",
            "to_season": "2026-2027",
            "person_uid": "PERS-SYNTH-DEP001",
            "person_name": "宋知行",
            "movement_bucket": "DEPARTED_OR_TRANSFERRED",
            "player_type": "DOMESTIC",
            "note": "转会离队",
        },
        {
            "schema": "cba-kb.club-season-roster-ledger.v1",
            "organization_id": "ORG-SYNTH-CLOUD",
            "club_name": "云海新锐俱乐部",
            "from_season": "2025-2026",
            "to_season": "2026-2027",
            "person_uid": "PERS-SYNTH-STG001",
            "person_name": "马克斯·约翰逊",
            "movement_bucket": "STAGED_FOREIGN_25_26",
            "player_type": "FOREIGN",
            "note": "2025-2026 阶段性注册出场外援",
        },
    ]


# ===========================================================================
# P0-E1: provider_player_ids matching & Career Total Row Semantics
# ===========================================================================


class TestP0E1ProviderPlayerIdMatchingAndCareerTotals:
    """P0-E1: Exact provider_player_ids matching + FULL_CBA_CAREER vs PARTIAL_SEASONS_OBSERVED."""

    def test_provider_player_ids_homonym_zero_cross_contamination(self) -> None:
        from cba_kb.person_card_builder import build_career_season_stats_tables

        stats = _make_homonym_season_stats()

        # Person A has only provider_player_ids=["910001"] and shares Chinese name "约翰逊"
        person_a = {
            "person_uid": "PERS-SYNTH-JOHN01",
            "player_name": "约翰逊",
            "provider_player_ids": ["910001"],
        }
        # Person B has only provider_player_ids=["920002"] and shares Chinese name "约翰逊"
        person_b = {
            "person_uid": "PERS-SYNTH-JOHN02",
            "player_name": "约翰逊",
            "provider_player_ids": ["920002"],
        }

        res_a = build_career_season_stats_tables(person_a, stats)
        res_b = build_career_season_stats_tables(person_b, stats)

        assert len(res_a["regular_season_rows"]) == 2
        assert {r["record_id"] for r in res_a["regular_season_rows"]} == {
            "stat-johnson-a-2024-reg",
            "stat-johnson-a-2025-reg",
        }
        assert res_a["regular_career_summary"]["games_played"] == 60  # 32 + 28, zero bleed from 10

        assert len(res_b["regular_season_rows"]) == 1
        assert {r["record_id"] for r in res_b["regular_season_rows"]} == {
            "stat-johnson-b-2025-reg",
        }
        assert res_b["regular_career_summary"]["games_played"] == 10

    def test_career_total_row_labels_full_vs_partial_and_unspecified(self) -> None:
        from cba_kb.kc_render import render_research_card
        from cba_kb.person_card_builder import build_career_season_stats_tables

        stats = _make_homonym_season_stats()
        person_a = {
            "person_uid": "PERS-SYNTH-JOHN01",
            "player_name": "达卡里·约翰逊",
            "provider_player_ids": ["910001"],
        }

        # 1. FULL_CBA_CAREER -> "CBA生涯总计 / 场均（截至 2026-04-20）"
        career_full = build_career_season_stats_tables(
            person_a,
            stats,
            career_coverage_mode="FULL_CBA_CAREER",
            observed_until="2026-04-20",
        )
        payload_full = {
            "entity_role": "PLAYER",
            "bio_profile": {"canonical_name": "达卡里·约翰逊", "person_uid": "PERS-SYNTH-JOHN01"},
            "career_season_stats": career_full,
            "grains": {"STATS": {"posture": "CLEAN_OFFICIAL", "season": "2025-2026"}},
            "evidence_transparency": {"posture": "CLEAN_OFFICIAL", "scope_meta": {"player_name": "达卡里·约翰逊", "season": "2025-2026"}},
        }
        md_full = render_research_card(
            payload_full, evidence_verified=True, visibility="public_safe", card_style="wiki_v241"
        )
        assert "CBA生涯总计 / 场均（截至 2026-04-20）" in md_full
        assert "已收录赛季合计" not in md_full

        # 2. PARTIAL_SEASONS_OBSERVED -> "已收录赛季合计（2024–2026，共 2 季，截至 2026-04-20）"
        career_partial = build_career_season_stats_tables(
            person_a,
            stats,
            career_coverage_mode="PARTIAL_SEASONS_OBSERVED",
            observed_until="2026-04-20",
        )
        payload_partial = dict(payload_full, career_season_stats=career_partial)
        md_partial = render_research_card(
            payload_partial, evidence_verified=True, visibility="public_safe", card_style="wiki_v241"
        )
        assert "已收录赛季合计（2024–2026，共 2 季，截至 2026-04-20）" in md_partial
        assert "CBA生涯总计 / 场均" not in md_partial

        # 3. Unspecified coverage mode defaults to PARTIAL_SEASONS_OBSERVED wording (never claims full career)
        career_default = build_career_season_stats_tables(person_a, stats)
        payload_default = dict(payload_full, career_season_stats=career_default)
        md_default = render_research_card(
            payload_default, evidence_verified=True, visibility="public_safe", card_style="wiki_v241"
        )
        assert "已收录赛季合计（2024–2026，共 2 季，截至 2026-04-20）" in md_default
        assert "CBA生涯总计 / 场均" not in md_default


# ===========================================================================
# P0-E2: Default-Collapsed Gamelog Drawer Summary with Latest Game Date
# ===========================================================================


class TestP0E2GamelogDrawerSummary:
    """P0-E2: <details class="gamelog-drawer"> summary with latest game_date."""

    def test_gamelog_drawer_collapsed_and_summary_has_latest_game_date(self) -> None:
        from cba_kb.kc_render import render_research_card

        payload = {
            "entity_role": "PLAYER",
            "bio_profile": {"canonical_name": "林启明", "person_uid": "PERS-SYNTH-LIN001"},
            "grains": {"STATS": {"posture": "CLEAN_OFFICIAL", "season": "2025-2026"}},
            "evidence_transparency": {
                "posture": "CLEAN_OFFICIAL",
                "scope_meta": {"player_name": "林启明", "season": "2025-2026"},
            },
            "recent_games_summary": {
                "played_games_in_window": 3,
                "averages": {"points": 18.7, "rebounds": 6.3, "assists": 4.0},
                "games": [
                    {
                        "game_date": "2026-03-10",
                        "round_label": "第35轮",
                        "opponent_team_name": "山河领航",
                        "metrics": {"minutes_played": 32.0, "points": 16, "rebounds": 5, "assists": 3},
                    },
                    {
                        "game_date": "2026-03-18",
                        "round_label": "第38轮",
                        "opponent_team_name": "江南飞马",
                        "metrics": {"minutes_played": 35.0, "points": 22, "rebounds": 8, "assists": 5},
                    },
                    {
                        "game_date": "2026-03-14",
                        "round_label": "第36轮",
                        "opponent_team_name": "北辰雄狮",
                        "metrics": {"minutes_played": 30.0, "points": 18, "rebounds": 6, "assists": 4},
                    },
                ],
            },
        }
        md = render_research_card(
            payload, evidence_verified=True, visibility="public_safe", card_style="wiki_v241"
        )
        assert '<details class="gamelog-drawer">' in md
        assert '<details class="gamelog-drawer" open' not in md
        expected_summary = (
            "<summary>展开查看近 3 场已记录单场数据"
            "（最新比赛日：2026-03-18｜场均 18.7 分 6.3 篮板 4.0 助攻）</summary>"
        )
        assert expected_summary in md


# ===========================================================================
# P0-E3: club_season_roster_ledger.jsonl 5-Way Movement Split
# ===========================================================================


class TestP0E3ClubSeasonRosterLedgerFiveWaySplit:
    """P0-E3: 5-way movement split in club card & static site index."""

    def test_club_card_and_site_index_render_five_way_movement_split(self, tmp_path: Path) -> None:
        from cba_kb.club_card_builder import build_club_panorama_card
        from cba_kb.static_site_builder import build_public_site

        ledger_rows = _make_synth_roster_ledger_rows()
        club_card = build_club_panorama_card(
            "云海新锐俱乐部",
            seasons=("2025-2026", "2026-2027"),
            organization_profile={
                "organization_id": "ORG-SYNTH-CLOUD",
                "canonical_name": "云海新锐俱乐部",
                "short_name": "云海新锐",
                "city": "云海市",
            },
            master_rows=[
                {"season": "2025-2026", "club_official": "云海新锐俱乐部", "player": "林启明", "contract_category": "C类"},
                {"season": "2025-2026", "club_official": "云海新锐俱乐部", "player": "周景行", "contract_category": "D类"},
                {"season": "2025-2026", "club_official": "云海新锐俱乐部", "player": "宋知行", "contract_category": "B类"},
                {"season": "2026-2027", "club_official": "云海新锐俱乐部", "player": "林启明", "contract_category": "C类"},
                {"season": "2026-2027", "club_official": "云海新锐俱乐部", "player": "许晨阳", "contract_category": "A1类"},
            ],
            foreign_rows=[
                {"season": "2025-2026", "club": "云海新锐俱乐部", "player_zh": "卡特·布鲁克斯", "player_en": "Carter Brooks"},
                {"season": "2025-2026", "club": "云海新锐俱乐部", "player_zh": "马克斯·约翰逊", "player_en": "Max Johnson"},
                {"season": "2026-2027", "club": "云海新锐俱乐部", "player_zh": "卡特·布鲁克斯", "player_en": "Carter Brooks"},
            ],
            roster_ledger=ledger_rows,
            visibility="public_safe",
            card_style="wiki_v241",
            observed_until="2026-09-01",
        )

        md = club_card["markdown"]
        for bucket_label in (
            "留队球员（含留用外援）",
            "新援与青训上调",
            "退役转任本队教练",
            "离队/转会球员",
            "25-26 阶段性注册/出场外援",
        ):
            assert bucket_label in md, f"Missing 5-way movement bucket label: {bucket_label}"

        changes = club_card["offseason_roster_changes"]
        assert "周景行" in changes["retired_to_club_coach"]
        assert "周景行" not in changes["departed"]
        assert "宋知行" in changes["departed"]
        assert "卡特·布鲁克斯" in changes["retained"]
        assert "许晨阳" in changes["joined"]
        assert "马克斯·约翰逊" in changes["staged_foreign_25_26"]

        # Verify retired coach does not appear on the departed line in rendered markdown
        departed_lines = [ln for ln in md.splitlines() if "离队/转会球员" in ln]
        assert departed_lines
        assert "周景行" not in departed_lines[0]
        assert "宋知行" in departed_lines[0]

        # Also verify static site index.html renders all 5 buckets cleanly
        site_res = build_public_site(
            club_card=club_card,
            person_cards=[],
            output_dir=tmp_path / "site",
            observed_until="2026-09-01",
        )
        index_html = Path(site_res["index_path"]).read_text(encoding="utf-8")
        for bucket_label in (
            "留队球员（含留用外援）",
            "新援与青训上调",
            "退役转任本队教练",
            "离队/转会球员",
            "25-26 阶段性注册/出场外援",
        ):
            assert bucket_label in index_html


# ===========================================================================
# P0-E4: 4-Template Lead Paragraph + Anti-Duplication + Related-Person Labels
# ===========================================================================


class TestP0E4EditorialBriefAndRelatedPersonLabels:
    """P0-E4: 4-template lead paragraph, anti-duplication guard, and related-person labels."""

    def test_four_templates_and_anti_duplication_guard(self) -> None:
        from cba_kb.editorial_brief import compose_wiki_lead_paragraph

        # Template 1: 现役在册球员 (with polluted honor strings that must trigger anti-duplication guard)
        res_active = compose_wiki_lead_paragraph(
            person_name="林启明",
            bio_profile={
                "birth_date": "2001-05-14",
                "nationality": "中国",
                "height_cm": 198,
                "weight_kg": 94,
                "position": "前锋",
                "jersey_number": "7",
                "current_club": "云海新锐俱乐部",
                "current_role": "球员",
                "honors": [
                    "2026年8月加盟云海新锐俱乐部",
                    "多次CBA总冠军",
                    "2024-2025赛季合成全明星阵容",
                ],
            },
            registration_profile={"domestic_current": {"season": "2026-2027", "club_official": "云海新锐俱乐部"}},
            career_season_stats={
                "seasons_count": 3,
                "regular_career_summary": {
                    "games_played": 90,
                    "points_per_game": 15.2,
                    "rebounds_per_game": 6.1,
                    "assists_per_game": 3.0,
                },
            },
            visibility="public_safe",
        )
        lead_active = res_active["lead_plain_text"]
        assert "曾入选2026年8月加盟" not in lead_active
        assert "曾入选多次CBA总冠军" not in lead_active
        assert "效力于离队" not in lead_active
        assert res_active.get("lead_template") == "ACTIVE_ROSTER_PLAYER"

        # Template 2: 教练与转型主帅
        res_coach = compose_wiki_lead_paragraph(
            person_name="周景行",
            bio_profile={
                "birth_date": "1988-09-20",
                "nationality": "中国",
                "current_club": "云海新锐俱乐部",
                "current_role": "主教练",
            },
            coach_role_facets={"club_appointed_role": "HEAD_COACH", "league_listed_role": "ASSISTANT_COACH"},
            unified_profile={"is_player_to_coach_transition": True},
            visibility="public_safe",
        )
        assert res_coach.get("lead_template") == "COACH_OR_TRANSITIONED_HEAD_COACH"
        assert "主教练" in res_coach["lead_plain_text"]
        assert "转型" in res_coach["lead_plain_text"]

        # Template 3: 新秀或未出场年轻球员
        res_rookie = compose_wiki_lead_paragraph(
            person_name="许晨阳",
            bio_profile={
                "birth_date": "2006-02-11",
                "nationality": "中国",
                "height_cm": 195,
                "position": "后卫",
                "current_club": "云海新锐俱乐部",
                "current_role": "球员",
                "draft_or_youth_origin": "云海青训体系",
            },
            registration_profile={"domestic_current": {"season": "2026-2027", "club_official": "云海新锐俱乐部"}},
            career_season_stats={"seasons_count": 0, "regular_season_rows": [], "regular_career_summary": None},
            visibility="public_safe",
        )
        assert res_rookie.get("lead_template") == "ROOKIE_OR_UNPLAYED_YOUTH"
        assert "青训" in res_rookie["lead_plain_text"] or "年轻" in res_rookie["lead_plain_text"] or "新秀" in res_rookie["lead_plain_text"]

        # Template 4: 离队或阶段性球员 (must NEVER emit "效力于离队")
        res_departed = compose_wiki_lead_paragraph(
            person_name="宋知行",
            bio_profile={
                "birth_date": "2000-03-11",
                "nationality": "中国",
                "position": "后卫",
                "current_club": "离队",
                "current_role": "球员",
                "roster_status": "DEPARTED",
            },
            visibility="public_safe",
        )
        assert res_departed.get("lead_template") == "DEPARTED_OR_STAGED_PLAYER"
        assert "效力于离队" not in res_departed["lead_plain_text"]

    def test_related_persons_abolishes_shitu_and_adds_college_alumni(self) -> None:
        from cba_kb.static_site_builder import compute_related_persons

        target = {
            "person_uid": "PERS-SYNTH-P1",
            "person_name": "林启明",
            "slug": "lin-qiming",
            "entity_role": "PLAYER",
            "bio_profile": {
                "person_uid": "PERS-SYNTH-P1",
                "canonical_name": "林启明",
                "current_club": "云海新锐俱乐部",
                "current_role": "前锋",
                "draft_or_youth_origin": "云海青训体系",
                "alma_mater": "星海体育大学",
            },
            "projected_stories": [{"story_id": "story_shared_01"}],
        }
        all_persons = [
            target,
            {
                "person_uid": "PERS-SYNTH-C1",
                "person_name": "周景行",
                "slug": "zhou-jingxing",
                "entity_role": "HEAD_COACH",
                "bio_profile": {
                    "person_uid": "PERS-SYNTH-C1",
                    "canonical_name": "周景行",
                    "current_club": "云海新锐俱乐部",
                    "current_role": "主教练",
                },
            },
            {
                "person_uid": "PERS-SYNTH-ALUM1",
                "person_name": "陆明远",
                "slug": "lu-mingyuan",
                "entity_role": "PLAYER",
                "bio_profile": {
                    "person_uid": "PERS-SYNTH-ALUM1",
                    "canonical_name": "陆明远",
                    "current_club": "山河领航俱乐部",
                    "current_role": "后卫",
                    "alma_mater": "星海体育大学",
                },
            },
            {
                "person_uid": "PERS-SYNTH-YOUTH1",
                "person_name": "许晨阳",
                "slug": "xu-chenyang",
                "entity_role": "PLAYER",
                "bio_profile": {
                    "person_uid": "PERS-SYNTH-YOUTH1",
                    "canonical_name": "许晨阳",
                    "current_club": "江南飞马俱乐部",
                    "current_role": "后卫",
                    "draft_or_youth_origin": "云海青训体系",
                },
            },
            {
                "person_uid": "PERS-SYNTH-STORY1",
                "person_name": "顾长风",
                "slug": "gu-changfeng",
                "entity_role": "PLAYER",
                "bio_profile": {
                    "person_uid": "PERS-SYNTH-STORY1",
                    "canonical_name": "顾长风",
                    "current_club": "北辰雄狮俱乐部",
                    "current_role": "中锋",
                },
                "projected_stories": [{"story_id": "story_shared_01"}],
            },
        ]

        related = compute_related_persons(target, all_persons, max_related=8)
        by_name = {item["person_name"]: item["relation_label"] for item in related}

        assert "师徒" not in by_name.values()
        assert by_name.get("周景行") == "同期教练与球员"
        assert by_name.get("陆明远") == "大学校友"
        assert by_name.get("许晨阳") == "青训同梯队"
        assert by_name.get("顾长风") == "共同经历"


# ===========================================================================
# P0-E5: Adjacent Citations, Clickable Links, Source Name Mapping, Leak Gate
# ===========================================================================


class TestP0E5CitationsAndPublicSafeLeakGate:
    """P0-E5: Adjacent [1][2][3] refs, clickable URLs, Chinese source mapping, fail-closed gate."""

    def test_replace_inline_fn_refs_handles_adjacent_citations(self) -> None:
        from cba_kb.static_site_builder import _replace_inline_fn_refs

        seen: set[int] = set()
        rendered_two = _replace_inline_fn_refs(
            "该球员完成了赛季注册[1][2]。",
            max_citation_index=3,
            seen_fnrefs=seen,
        )
        assert '<sup class="cite-ref"><a' in rendered_two
        assert 'href="#fn-1"' in rendered_two
        assert 'href="#fn-2"' in rendered_two
        assert "[1]" in rendered_two and "[2]" in rendered_two
        assert rendered_two.count('<sup class="cite-ref">') == 2

        seen_three: set[int] = set()
        rendered_three = _replace_inline_fn_refs(
            "多源交叉核验结果[1][2][3]。",
            max_citation_index=3,
            seen_fnrefs=seen_three,
        )
        assert rendered_three.count('<sup class="cite-ref">') == 3
        for n in (1, 2, 3):
            assert f'href="#fn-{n}"' in rendered_three

    def test_citation_registry_maps_internal_keys_and_preserves_https_links(self, tmp_path: Path) -> None:
        from cba_kb.kc_render import CitationRegistry
        from cba_kb.static_site_builder import build_public_site

        reg = CitationRegistry(visibility="public_safe")
        idx1 = reg.register(
            label="team_staff_snapshots",
            url="https://example.invalid/cba/official/staff-2026",
            source_kind="league_official",
        )
        idx2 = reg.register(
            label="club_announcement:cloud_coach_2026",
            url="https://example.invalid/club/cloud/coach-announcement",
            source_kind="club_official",
        )
        idx3 = reg.register(
            label="season_stats.jsonl",
            url="https://example.invalid/cba/official/stats-2026",
            source_kind="league_official",
        )
        assert idx1 == 1 and idx2 == 2 and idx3 == 3

        entries = reg.entries()
        labels = [e["label"] for e in entries]
        for forbidden in ("team_staff_snapshots", "club_announcement:", ".jsonl", ".csv"):
            assert all(forbidden not in lbl for lbl in labels), f"Internal key leaked in label: {labels}"

        club_card = {
            "visibility": "public_safe",
            "card_style": "wiki_v241",
            "organization_id": "ORG-SYNTH-CLOUD",
            "club_name": "云海新锐俱乐部",
            "markdown": "# 云海新锐俱乐部\n\n俱乐部完成了教练组与球员注册[1][2][3]。\n\nend_marker: true\n",
            "citations": entries,
        }
        person_card = {
            "visibility": "public_safe",
            "card_style": "wiki_v241",
            "person_uid": "PERS-SYNTH-LIN001",
            "person_name": "林启明",
            "markdown": "# 林启明\n\n林启明为云海新锐俱乐部前锋[1][2]。\n\nend_marker: true\n",
            "citations": entries[:2],
        }
        site_res = build_public_site(
            club_card=club_card,
            person_cards=[person_card],
            output_dir=tmp_path / "site",
            observed_until="2026-09-01",
        )
        person_html = Path(site_res["person_pages"][0]["path"]).read_text(encoding="utf-8")
        assert '<a href="https://example.invalid/cba/official/staff-2026" target="_blank" rel="noopener noreferrer">' in person_html
        assert '<a href="https://example.invalid/club/cloud/coach-announcement" target="_blank" rel="noopener noreferrer">' in person_html

        ld_blocks = _extract_json_ld_blocks(person_html)
        assert ld_blocks
        citations_ld = ld_blocks[0].get("citation") or []
        assert "https://example.invalid/cba/official/staff-2026" in str(citations_ld)

    @pytest.mark.parametrize(
        "leaked_token",
        [
            "master_domestic_2026.csv",
            "season_stats.jsonl",
            "team_staff_snapshots",
            "club_announcement:synth_2026_08",
        ],
    )
    def test_verify_public_safe_and_site_gate_fail_closed_on_internal_tokens(
        self, tmp_path: Path, leaked_token: str
    ) -> None:
        from cba_kb.kc_visibility import verify_public_safe_card_text
        from cba_kb.static_site_builder import build_public_site

        with pytest.raises(ValueError, match="PUBLIC_SAFE_REDACTION_VIOLATION"):
            verify_public_safe_card_text(f"# 合成人物\n\n来源：{leaked_token}\n\nend_marker: true\n")

        out_dir = tmp_path / f"blocked_{abs(hash(leaked_token))}"
        club_card = {
            "visibility": "public_safe",
            "card_style": "wiki_v241",
            "organization_id": "ORG-SYNTH-CLOUD",
            "club_name": "云海新锐俱乐部",
            "markdown": f"# 云海新锐俱乐部\n\n来源记录：{leaked_token}\n\nend_marker: true\n",
            "citations": [],
        }
        with pytest.raises(ValueError, match="PUBLIC_SITE_VISIBILITY_VIOLATION"):
            build_public_site(club_card=club_card, person_cards=[], output_dir=out_dir)
        assert not out_dir.exists()


# ===========================================================================
# P0-E6: person_bio_profiles.json (v2) Provenance Gate
# ===========================================================================


class TestP0E6PersonBioProfilesV2ProvenanceGate:
    """P0-E6: cba-kb.person-bio-profile.v2 provenance gate requires 64-hex raw_sha256 + anchor."""

    def test_v2_provenance_gate_accepts_valid_and_rejects_missing_sha_or_anchor(self) -> None:
        from cba_kb.person_registry import PersonRegistryError, validate_person_bio_profiles

        valid_sha = _synth_sha("synth-bio-evidence-001")
        valid_bundle = {
            "schema": "cba-kb.person-bio-profile.v2",
            "profiles": [
                {
                    "person_uid": "PERS-SYNTH-LIN001",
                    "canonical_name": "林启明",
                    "birth_date": "2001-05-14",
                    "nationality": "中国",
                    "current_club": "云海新锐俱乐部",
                    "current_role": "前锋",
                    "review_status": "VERIFIED",
                    "provenance": {
                        "raw_sha256": valid_sha,
                        "source_url": "https://example.invalid/cba/bio/lin-qiming",
                    },
                    "attribute_provenance": {
                        "birth_date": {
                            "status": "VERIFIED",
                            "raw_sha256": valid_sha,
                            "bundle_id": "bundle-synth-001",
                        }
                    },
                }
            ],
        }
        validated = validate_person_bio_profiles(valid_bundle)
        assert validated["schema"] == "cba-kb.person-bio-profile.v2"
        assert len(validated["profiles"]) == 1

        # Missing raw_sha256 on VERIFIED entry -> must fail closed
        missing_sha_bundle = {
            "schema": "cba-kb.person-bio-profile.v2",
            "profiles": [
                {
                    "person_uid": "PERS-SYNTH-LIN001",
                    "canonical_name": "林启明",
                    "review_status": "VERIFIED",
                    "provenance": {
                        "source_url": "https://example.invalid/cba/bio/lin-qiming",
                    },
                }
            ],
        }
        with pytest.raises((PersonRegistryError, ValueError)):
            validate_person_bio_profiles(missing_sha_bundle)

        # Invalid (non-64-hex) raw_sha256 -> must fail closed
        bad_sha_bundle = {
            "schema": "cba-kb.person-bio-profile.v2",
            "profiles": [
                {
                    "person_uid": "PERS-SYNTH-LIN001",
                    "canonical_name": "林启明",
                    "review_status": "VERIFIED",
                    "provenance": {
                        "raw_sha256": "not-a-64-hex-sha",
                        "source_url": "https://example.invalid/cba/bio/lin-qiming",
                    },
                }
            ],
        }
        with pytest.raises((PersonRegistryError, ValueError)):
            validate_person_bio_profiles(bad_sha_bundle)

        # Missing source anchor (neither source_url nor bundle_id) -> must fail closed
        missing_anchor_bundle = {
            "schema": "cba-kb.person-bio-profile.v2",
            "profiles": [
                {
                    "person_uid": "PERS-SYNTH-LIN001",
                    "canonical_name": "林启明",
                    "review_status": "VERIFIED",
                    "provenance": {
                        "raw_sha256": valid_sha,
                    },
                }
            ],
        }
        with pytest.raises((PersonRegistryError, ValueError)):
            validate_person_bio_profiles(missing_anchor_bundle)

        # Attribute-level VERIFIED entry missing raw_sha256 -> must fail closed
        bad_attr_bundle = {
            "schema": "cba-kb.person-bio-profile.v2",
            "profiles": [
                {
                    "person_uid": "PERS-SYNTH-LIN001",
                    "canonical_name": "林启明",
                    "review_status": "VERIFIED",
                    "provenance": {
                        "raw_sha256": valid_sha,
                        "source_url": "https://example.invalid/cba/bio/lin-qiming",
                    },
                    "attribute_provenance": {
                        "birth_date": {
                            "status": "VERIFIED",
                            "source_url": "https://example.invalid/cba/bio/lin-qiming",
                        }
                    },
                }
            ],
        }
        with pytest.raises((PersonRegistryError, ValueError)):
            validate_person_bio_profiles(bad_attr_bundle)

    def test_build_person_card_rejects_unprovenanced_verified_bio(self) -> None:
        from cba_kb.person_card_builder import build_person_card
        from cba_kb.person_registry import PersonRegistryError

        unprovenanced_v2 = {
            "schema": "cba-kb.person-bio-profile.v2",
            "profiles": [
                {
                    "person_uid": "PERS-SYNTH-LIN001",
                    "canonical_name": "林启明",
                    "birth_date": "2001-05-14",
                    "current_club": "云海新锐俱乐部",
                    "current_role": "前锋",
                    "review_status": "VERIFIED",
                    "provenance": {},
                }
            ],
        }
        registry = {
            "schema": "cba-kb.person-registry.v1",
            "persons": [
                {
                    "person_uid": "PERS-SYNTH-LIN001",
                    "canonical_name": "林启明",
                    "role_tenures": [
                        {
                            "role": "PLAYER",
                            "organization_id": "ORG-SYNTH-CLOUD",
                            "organization_name": "云海新锐俱乐部",
                            "season_start": "2025-2026",
                            "authority_scope": "LEAGUE_OFFICIAL",
                        }
                    ],
                }
            ],
        }
        # Either raises fail-closed on v2 provenance violation OR strips/downgrades VERIFIED bio
        try:
            res = build_person_card(
                "林启明",
                season="2025-2026",
                person_registry=registry,
                person_bio_profiles=unprovenanced_v2,
                visibility="public_safe",
                card_style="wiki_v241",
            )
        except (PersonRegistryError, ValueError):
            return
        # If non-raising fallback is used, bio must NOT be accepted as VERIFIED
        bio = res.get("bio_profile") or {}
        assert bio.get("review_status") != "VERIFIED"
        assert "2001年5月14日" not in (res.get("markdown") or "")


# ===========================================================================
# Real End-to-End CLI & Pipeline Wiring Test
# ===========================================================================


class TestReq242EndToEndPipelineWiring:
    """Real wiring test: synthetic data dir -> CLI build-person-card + build-club-card + build-public-site."""

    def test_full_cli_pipeline_from_synthetic_data_dir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from cba_kb.kc_visibility import verify_public_safe_card_text

        data_dir = tmp_path / "synth_data"
        data_dir.mkdir(parents=True)
        game_logs_dir = data_dir / "game_logs"
        game_logs_dir.mkdir()

        valid_sha = _synth_sha("synth-e2e-bio-proof")

        # 1. person_registry.json
        person_registry_path = data_dir / "person_registry.json"
        person_registry_path.write_text(
            json.dumps(
                {
                    "schema": "cba-kb.person-registry.v1",
                    "persons": [
                        {
                            "person_uid": "PERS-SYNTH-JOHN01",
                            "canonical_name": "达卡里·约翰逊",
                            "aliases": ["约翰逊"],
                            "linked_player_uids": ["player:synth-johnson-alpha"],
                            "provider_player_ids": ["910001"],
                            "role_tenures": [
                                {
                                    "role": "PLAYER",
                                    "organization_id": "ORG-SYNTH-CLOUD",
                                    "organization_name": "云海新锐俱乐部",
                                    "season_start": "2024-2025",
                                    "season_end": "2025-2026",
                                    "authority_scope": "LEAGUE_OFFICIAL",
                                    "evidence_refs": [
                                        "https://example.invalid/cba/reg/2025-2026/910001"
                                    ],
                                }
                            ],
                        },
                        {
                            "person_uid": "PERS-SYNTH-COACH01",
                            "canonical_name": "周景行",
                            "aliases": ["周指导"],
                            "linked_player_uids": [],
                            "role_tenures": [
                                {
                                    "role": "HEAD_COACH",
                                    "organization_id": "ORG-SYNTH-CLOUD",
                                    "organization_name": "云海新锐俱乐部",
                                    "season_start": "2026-2027",
                                    "season_end": None,
                                    "authority_scope": "CLUB_OFFICIAL",
                                    "evidence_refs": [
                                        "https://example.invalid/club/cloud/coach-2026"
                                    ],
                                }
                            ],
                        },
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        # 2. person_bio_profiles.json (v2 with full provenance)
        bio_profiles_path = data_dir / "person_bio_profiles.json"
        bio_profiles_path.write_text(
            json.dumps(
                {
                    "schema": "cba-kb.person-bio-profile.v2",
                    "profiles": [
                        {
                            "person_uid": "PERS-SYNTH-JOHN01",
                            "canonical_name": "达卡里·约翰逊",
                            "nationality": "美国",
                            "height_cm": 211,
                            "weight_kg": 115,
                            "position": "中锋",
                            "current_club": "云海新锐俱乐部",
                            "current_role": "中锋",
                            "review_status": "VERIFIED",
                            "provenance": {
                                "raw_sha256": valid_sha,
                                "source_url": "https://example.invalid/cba/bio/910001",
                            },
                            "evidence_refs": [
                                {
                                    "label": "CBA官方外援资料",
                                    "url": "https://example.invalid/cba/bio/910001",
                                },
                                {
                                    "label": "云海新锐俱乐部注册公示",
                                    "url": "https://example.invalid/club/cloud/reg-910001",
                                },
                            ],
                        },
                        {
                            "person_uid": "PERS-SYNTH-COACH01",
                            "canonical_name": "周景行",
                            "nationality": "中国",
                            "current_club": "云海新锐俱乐部",
                            "current_role": "主教练",
                            "review_status": "VERIFIED",
                            "provenance": {
                                "raw_sha256": valid_sha,
                                "bundle_id": "bundle-synth-coach-01",
                            },
                            "evidence_refs": [
                                {
                                    "label": "云海新锐主教练任命公告",
                                    "url": "https://example.invalid/club/cloud/coach-2026",
                                }
                            ],
                        },
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        # 3. season_stats.jsonl (with homonym distractor 920002)
        season_stats_path = data_dir / "season_stats.jsonl"
        season_stats_path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in _make_homonym_season_stats()) + "\n",
            encoding="utf-8",
        )

        # 4. game_logs JSONL for 910001
        (game_logs_dir / "game_stats_2025_2026.jsonl").write_text(
            "\n".join(
                json.dumps(r, ensure_ascii=False)
                for r in [
                    {
                        "player_uid": "player:synth-johnson-alpha",
                        "provider_player_id": "910001",
                        "player_name": "约翰逊",
                        "season": "2025-2026",
                        "game_date": "2026-04-05",
                        "round_label": "第42轮",
                        "opponent_team_name": "江南飞马",
                        "metrics": {"minutes_played": 34.0, "points": 24, "rebounds": 11, "assists": 4},
                    },
                    {
                        "player_uid": "player:synth-johnson-alpha",
                        "provider_player_id": "910001",
                        "player_name": "约翰逊",
                        "season": "2025-2026",
                        "game_date": "2026-04-12",
                        "round_label": "第44轮",
                        "opponent_team_name": "北辰雄狮",
                        "metrics": {"minutes_played": 36.0, "points": 26, "rebounds": 13, "assists": 6},
                    },
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        # 5. club_season_roster_ledger.jsonl
        roster_ledger_path = data_dir / "club_season_roster_ledger.jsonl"
        roster_ledger_path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in _make_synth_roster_ledger_rows()) + "\n",
            encoding="utf-8",
        )

        # 6. organization_profiles.json
        org_profiles_path = data_dir / "organization_profiles.json"
        org_profiles_path.write_text(
            json.dumps(
                {
                    "organizations": [
                        {
                            "organization_id": "ORG-SYNTH-CLOUD",
                            "canonical_name": "云海新锐俱乐部",
                            "short_name": "云海新锐",
                            "city": "云海市",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        cards_dir = tmp_path / "cards_json"
        cards_dir.mkdir()
        site_dir = tmp_path / "public_site"

        # Step A: CLI build-person-card for player & coach
        for pname, slug_file in (("达卡里·约翰逊", "johnson.json"), ("周景行", "zhou.json")):
            rc, out, err = _run_cli(
                monkeypatch,
                capsys,
                [
                    "build-person-card",
                    "--person",
                    pname,
                    "--season",
                    "2025-2026",
                    "--person-registry",
                    str(person_registry_path),
                    "--bio-profiles",
                    str(bio_profiles_path),
                    "--season-stats",
                    str(season_stats_path),
                    "--game-logs-dir",
                    str(game_logs_dir),
                    "--visibility",
                    "public_safe",
                    "--card-style",
                    "wiki_v241",
                ],
            )
            assert rc == 0, f"build-person-card failed for {pname}: {err}"
            card_obj = json.loads(out)
            assert card_obj["status"] == "BUILT"
            verify_public_safe_card_text(card_obj["markdown"])
            (cards_dir / slug_file).write_text(
                json.dumps(card_obj, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        # Step B: CLI build-club-card with --roster-ledger
        club_card_json = tmp_path / "club_card.json"
        rc_club, out_club, err_club = _run_cli(
            monkeypatch,
            capsys,
            [
                "build-club-card",
                "--club",
                "云海新锐俱乐部",
                "--seasons",
                "2025-2026,2026-2027",
                "--organization-profiles",
                str(org_profiles_path),
                "--roster-ledger",
                str(roster_ledger_path),
                "--person-registry",
                str(person_registry_path),
                "--visibility",
                "public_safe",
                "--card-style",
                "wiki_v241",
                "--observed-until",
                "2026-09-01",
            ],
        )
        assert rc_club == 0, f"build-club-card failed: {err_club}"
        club_obj = json.loads(out_club)
        assert club_obj["status"] == "BUILT"
        verify_public_safe_card_text(club_obj["markdown"])
        club_card_json.write_text(json.dumps(club_obj, ensure_ascii=False, indent=2), encoding="utf-8")

        # Step C: CLI build-public-site
        rc_site, out_site, err_site = _run_cli(
            monkeypatch,
            capsys,
            [
                "build-public-site",
                "--club-card",
                str(club_card_json),
                "--person-cards-dir",
                str(cards_dir),
                "--output-dir",
                str(site_dir),
                "--observed-until",
                "2026-09-01",
            ],
        )
        assert rc_site == 0, f"build-public-site failed: {err_site}"
        site_res = json.loads(out_site)
        assert site_res["status"] == "BUILT"
        assert site_res["broken_links_count"] == 0

        index_html = (site_dir / "index.html").read_text(encoding="utf-8")
        llms_txt = (site_dir / "llms.txt").read_text(encoding="utf-8")
        sitemap_xml = (site_dir / "sitemap.xml").read_text(encoding="utf-8")

        # Verify 5-way movement split in index.html
        assert "退役转任本队教练" in index_html
        assert "25-26 阶段性注册/出场外援" in index_html

        # Verify person page has gamelog summary with latest date, adjacent cite-ref, no 师徒
        johnson_page = next(
            Path(p["path"]).read_text(encoding="utf-8")
            for p in site_res["person_pages"]
            if p["person_name"] == "达卡里·约翰逊"
        )
        assert "最新比赛日：2026-04-12" in johnson_page
        assert '<sup class="cite-ref">' in johnson_page
        assert "同期教练与球员" in johnson_page
        assert "师徒" not in johnson_page

        # Verify zero leaked internal tokens across all exported artifacts
        for exported_text in (index_html, johnson_page, llms_txt, sitemap_xml):
            verify_public_safe_card_text(exported_text)
