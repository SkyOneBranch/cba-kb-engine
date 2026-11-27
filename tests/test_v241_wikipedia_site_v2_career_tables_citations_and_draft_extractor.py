"""Frozen red-light acceptance tests for REQ-241 (cba-kb-engine v2.4.1).

Covers P0-1 through P0-5 with 100% offline synthetic fixtures (G5 == 0, G6 == 0):
- P0-1: Wikipedia 2.0 Hero (card_style="wiki_v241", clean Chinese <h1> including
        coach-only & transitioned coach with 0 current-season player stats,
        compose_wiki_lead_paragraph 80-180 chars with inline [1][2] + meta
        description, PersonBioProfile + right-floating <aside class="wiki-infobox">
        auto-hiding missing/0/unassigned fields, default-collapsed
        <details class="audit-drawer"> with non-contradictory top-level domain summary).
- P0-2: Multi-season Regular Season + Playoffs career tables (no double-counting
        WHOLE_SEASON vs TEAM_SPLIT, cross-team seasons & transitioned coach history,
        pure coach omits player stats gracefully), resolve_cba_team_name numeric ID
        mapping, default-collapsed <details class="gamelog-drawer">, D-02 two-season
        club card (active + departed + offseason_roster_changes) & 0 broken links.
- P0-3: Unified CitationRegistry (deduping registration, stats, coach, bio,
        StoryRecord.canonical_source, timeline including URL-less newspaper/WeChat),
        inline superscript <a class="fn-ref" href="#fn-k" id="fnref-k">[k]</a> with
        back-link to single bottom 参考资料 (no dangling Sources: or duplicate
        External Citations), verify_public_safe_card_text blocking doc_<hex> and
        (unassigned), StoryRecord.organization_summary & single-summary fallback,
        natural Chinese Wikipedia section headings.
- P0-4: GEO sitemap.xml + llms.txt (public-safe verified), enhanced Person JSON-LD,
        top-8 relationship-driven 关联人物 with relation labels, collapsible bottom
        <nav class="squad-navbox">.
- P0-5: Local draft extractor (wiki_draft_extractor.py + CLI draft-wiki-candidates,
        zero network, 0 canonical writes, all candidates DRAFT_PENDING_HUMAN_REVIEW),
        V215_RELEASE_SPECS["v2.4.1-1"] & ["v2.4.1-2"] bound to baseline aeb630c8...,
        cloud_state.py allowlist includes person_bio_profiles.json.

Lazy imports inside test bodies guarantee clean pytest collection and differential
falsification on baseline aeb630c8211799fe91f05cd06b18301a55c823f2.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List

import pytest

BASELINE_SHA_V241_1 = "aeb630c8211799fe91f05cd06b18301a55c823f2"


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


def _make_synth_registry_and_bios() -> tuple[Dict[str, Any], Dict[str, Any]]:
    from cba_kb.person_registry import (
        PersonBioProfile,
        PersonProfile,
        RoleTenure,
        make_person_bio_profiles,
        make_person_registry,
    )

    persons = [
        PersonProfile(
            person_uid="PERS-CLOUD-COACH01",
            canonical_name="周景行",
            aliases=("周指导",),
            linked_player_uids=("player:cloud-p2c-01",),
            role_tenures=(
                RoleTenure(
                    role="PLAYER",
                    organization_id="ORG-SYNTH-CLOUD",
                    organization_name="云海新锐俱乐部",
                    season_start="2016-2017",
                    season_end="2023-2024",
                    authority_scope="LEAGUE_OFFICIAL",
                    evidence_refs=("https://example.invalid/reg/2016-2024/cloud-p2c-01",),
                ),
                RoleTenure(
                    role="HEAD_COACH",
                    organization_id="ORG-SYNTH-CLOUD",
                    organization_name="云海新锐俱乐部",
                    season_start="2025-2026",
                    season_end=None,
                    authority_scope="CLUB_OFFICIAL",
                    evidence_refs=("https://example.invalid/club/cloud/coach-2025",),
                ),
            ),
        ),
        PersonProfile(
            person_uid="PERS-CLOUD-PUREC02",
            canonical_name="陈致远",
            linked_player_uids=(),
            role_tenures=(
                RoleTenure(
                    role="ASSISTANT_COACH",
                    organization_id="ORG-SYNTH-CLOUD",
                    organization_name="云海新锐俱乐部",
                    season_start="2025-2026",
                    season_end=None,
                    authority_scope="CLUB_OFFICIAL",
                    evidence_refs=("https://example.invalid/club/cloud/asst-2025",),
                ),
            ),
        ),
        PersonProfile(
            person_uid="PERS-CLOUD-PLAY001",
            canonical_name="林启明",
            linked_player_uids=("player:cloud-star-01",),
            role_tenures=(
                RoleTenure(
                    role="PLAYER",
                    organization_id="ORG-SYNTH-CLOUD",
                    organization_name="云海新锐俱乐部",
                    season_start="2023-2024",
                    season_end=None,
                    authority_scope="LEAGUE_OFFICIAL",
                    evidence_refs=("https://example.invalid/reg/2023-2026/cloud-star-01",),
                ),
            ),
        ),
        PersonProfile(
            person_uid="PERS-CLOUD-PLAY002",
            canonical_name="宋知行",
            linked_player_uids=("player:cloud-guard-02",),
            role_tenures=(
                RoleTenure(
                    role="PLAYER",
                    organization_id="ORG-SYNTH-CLOUD",
                    organization_name="云海新锐俱乐部",
                    season_start="2024-2025",
                    season_end="2025-2026",
                    authority_scope="LEAGUE_OFFICIAL",
                ),
            ),
        ),
    ]
    registry = make_person_registry(persons)

    bios = make_person_bio_profiles(
        [
            PersonBioProfile(
                person_uid="PERS-CLOUD-PLAY001",
                canonical_name="林启明",
                english_name="Lin Qiming",
                birth_date="2001-05-14",
                birth_place="云海市",
                nationality="中国",
                height_cm=198,
                weight_kg=94,
                position="前锋",
                jersey_number="7",
                current_club="云海新锐俱乐部",
                current_role="前锋",
                draft_or_youth_origin="云海青训体系",
                honors=("2024-2025赛季合成全明星阵容",),
                evidence_refs=(
                    {
                        "label": "云海新锐官方球员档案",
                        "url": "https://example.invalid/club/cloud/bio/lin-qiming",
                    },
                ),
            ),
            PersonBioProfile(
                person_uid="PERS-CLOUD-COACH01",
                canonical_name="周景行",
                birth_date="1988-09-20",
                nationality="中国",
                height_cm=192,
                weight_kg=88,
                current_club="云海新锐俱乐部",
                current_role="主教练",
                draft_or_youth_origin="云海青训体系",
                evidence_refs=(
                    {
                        "label": "云海晚报专访：周景行转型执教",
                        "publisher": "云海晚报",
                        "published_at": "2025-08-18",
                        "source_kind": "newspaper",
                    },
                ),
            ),
            PersonBioProfile(
                person_uid="PERS-CLOUD-PUREC02",
                canonical_name="陈致远",
                nationality="中国",
                current_club="云海新锐俱乐部",
                current_role="助理教练",
                # Intentionally leave height_cm, weight_kg, birth_date, jersey_number None
            ),
            PersonBioProfile(
                person_uid="PERS-CLOUD-PLAY002",
                canonical_name="宋知行",
                birth_date="2000-03-11",
                nationality="中国",
                height_cm=186,
                weight_kg=82,
                position="后卫",
                jersey_number="12",
                current_club="云海新锐俱乐部",
                current_role="后卫",
                draft_or_youth_origin="云海青训体系",
            ),
        ]
    )
    return registry, bios


def _make_synth_career_stats() -> List[Dict[str, Any]]:
    """Multi-season stats including Regular + Playoffs + cross-team season (WHOLE_SEASON + TEAM_SPLIT)."""
    return [
        # 林启明 2024-2025 cross-team season: WHOLE_SEASON (40 GP) + 2 TEAM_SPLIT (18 + 22 GP)
        {
            "record_id": "stat-lin-2024-reg-whole",
            "player_uid": "player:cloud-star-01",
            "player_name": "林启明",
            "season": "2024-2025",
            "match_type": "REGULAR",
            "scope": "WHOLE_SEASON",
            "team_id": None,
            "team_name": "总计",
            "metrics": {
                "games_played": 40,
                "games_started": 35,
                "minutes_per_game": 31.0,
                "points_per_game": 15.0,
                "rebounds_per_game": 6.0,
                "assists_per_game": 3.0,
                "steals_per_game": 1.2,
                "blocks_per_game": 0.6,
                "turnovers_per_game": 1.8,
                "field_goals_percentage": 48.5,
                "three_point_percentage": 37.0,
                "free_throws_percentage": 82.0,
            },
            "provenance": {
                "captured_at": "2025-04-10T00:00:00Z",
                "locator": "https://example.invalid/cba/stats/2024-2025/lin",
            },
        },
        {
            "record_id": "stat-lin-2024-reg-split-1",
            "player_uid": "player:cloud-star-01",
            "player_name": "林启明",
            "season": "2024-2025",
            "match_type": "REGULAR",
            "scope": "TEAM_SPLIT",
            "team_id": "29140",
            "team_name": None,
            "metrics": {
                "games_played": 18,
                "games_started": 15,
                "minutes_per_game": 29.0,
                "points_per_game": 13.5,
                "rebounds_per_game": 5.5,
                "assists_per_game": 2.5,
                "steals_per_game": 1.0,
                "blocks_per_game": 0.5,
                "turnovers_per_game": 1.7,
                "field_goals_percentage": 47.0,
                "three_point_percentage": 35.0,
                "free_throws_percentage": 80.0,
            },
            "provenance": {
                "captured_at": "2025-04-10T00:00:00Z",
                "locator": "https://example.invalid/cba/stats/2024-2025/lin-split1",
            },
        },
        {
            "record_id": "stat-lin-2024-reg-split-2",
            "player_uid": "player:cloud-star-01",
            "player_name": "林启明",
            "season": "2024-2025",
            "match_type": "REGULAR",
            "scope": "TEAM_SPLIT",
            "team_id": "29131",
            "team_name": "云海新锐",
            "metrics": {
                "games_played": 22,
                "games_started": 20,
                "minutes_per_game": 32.6,
                "points_per_game": 16.2,
                "rebounds_per_game": 6.4,
                "assists_per_game": 3.4,
                "steals_per_game": 1.3,
                "blocks_per_game": 0.7,
                "turnovers_per_game": 1.9,
                "field_goals_percentage": 49.5,
                "three_point_percentage": 38.5,
                "free_throws_percentage": 83.5,
            },
            "provenance": {
                "captured_at": "2025-04-10T00:00:00Z",
                "locator": "https://example.invalid/cba/stats/2024-2025/lin-split2",
            },
        },
        # 林启明 2025-2026 Regular (20 GP) + Playoffs (6 GP)
        {
            "record_id": "stat-lin-2025-reg-whole",
            "player_uid": "player:cloud-star-01",
            "player_name": "林启明",
            "season": "2025-2026",
            "match_type": "REGULAR",
            "scope": "WHOLE_SEASON",
            "team_id": "29124",
            "team_name": None,
            "metrics": {
                "games_played": 20,
                "games_started": 20,
                "minutes_per_game": 34.0,
                "points_per_game": 18.0,
                "rebounds_per_game": 7.5,
                "assists_per_game": 4.5,
                "steals_per_game": 1.5,
                "blocks_per_game": 0.8,
                "turnovers_per_game": 2.0,
                "field_goals_percentage": 51.0,
                "three_point_percentage": 40.0,
                "free_throws_percentage": 85.0,
            },
            "provenance": {
                "captured_at": "2026-03-15T00:00:00Z",
                "locator": "https://example.invalid/cba/stats/2025-2026/lin",
            },
        },
        {
            "record_id": "stat-lin-2025-po-whole",
            "player_uid": "player:cloud-star-01",
            "player_name": "林启明",
            "season": "2025-2026",
            "match_type": "PLAYOFFS",
            "scope": "WHOLE_SEASON",
            "team_id": "29124",
            "team_name": None,
            "metrics": {
                "games_played": 6,
                "games_started": 6,
                "minutes_per_game": 36.5,
                "points_per_game": 21.0,
                "rebounds_per_game": 8.0,
                "assists_per_game": 5.0,
                "steals_per_game": 1.8,
                "blocks_per_game": 1.0,
                "turnovers_per_game": 2.1,
                "field_goals_percentage": 50.0,
                "three_point_percentage": 41.2,
                "free_throws_percentage": 88.0,
            },
            "provenance": {
                "captured_at": "2026-04-20T00:00:00Z",
                "locator": "https://example.invalid/cba/stats/2025-2026/lin-playoffs",
            },
        },
        # 周景行 (transitioned coach) historical player season 2022-2023 (30 GP)
        {
            "record_id": "stat-zhou-2022-reg",
            "player_uid": "player:cloud-p2c-01",
            "player_name": "周景行",
            "season": "2022-2023",
            "match_type": "REGULAR",
            "scope": "WHOLE_SEASON",
            "team_id": "29140",
            "team_name": None,
            "metrics": {
                "games_played": 30,
                "games_started": 28,
                "minutes_per_game": 28.5,
                "points_per_game": 11.4,
                "rebounds_per_game": 3.2,
                "assists_per_game": 5.6,
                "steals_per_game": 1.4,
                "blocks_per_game": 0.2,
                "turnovers_per_game": 1.6,
                "field_goals_percentage": 46.2,
                "three_point_percentage": 38.4,
                "free_throws_percentage": 86.0,
            },
            "provenance": {
                "captured_at": "2023-04-05T00:00:00Z",
                "locator": "https://example.invalid/cba/stats/2022-2023/zhou",
            },
        },
    ]


def _make_synth_game_logs() -> List[Dict[str, Any]]:
    games = []
    opponents = ["29140", "29124", "29115", "29129", "100074683"]
    for idx, opp_id in enumerate(opponents, start=1):
        games.append(
            {
                "game_id": f"game-synth-2026-0{idx}",
                "player_uid": "player:cloud-star-01",
                "player_name": "林启明",
                "season": "2026-2027",
                "game_date": f"2026-11-0{idx}",
                "round_label": f"第{idx}轮",
                "opponent_team_id": opp_id,
                "opponent_team_name": None,
                "played": True,
                "metrics": {
                    "minutes_played": 32.0 + idx,
                    "points": 16 + idx,
                    "rebounds": 6 + (idx % 3),
                    "assists": 4,
                    "steals": 1,
                    "blocks": 1,
                    "field_goals_made": 7,
                    "field_goals_attempted": 14,
                    "three_point_made": 2,
                    "three_point_attempted": 5,
                    "free_throws_made": 2,
                    "free_throws_attempted": 2,
                },
                "provenance": {
                    "captured_at": f"2026-11-0{idx}T23:00:00Z",
                    "doc_id": f"doc_{_synth_sha(f'game-{idx}')[:24]}",
                },
            }
        )
    return games


def _make_synth_story() -> Dict[str, Any]:
    from cba_kb.story_record import make_story_record

    return make_story_record(
        title="云海新锐冬训战术转型纪实",
        occurred_season="2025-2026",
        occurred_date="2025-09-12",
        published_at="2026-02-01T08:00:00Z",
        organization_summary="云海新锐在2025-2026赛季前由主帅周景行主导提速攻防体系，确立林启明为锋线核心。",
        canonical_source={
            "source_id": "src-cloud-winter-camp",
            "title": "云海日报：新锐男篮冬训提速",
            "publisher": "云海日报",
            "published_at": "2026-02-01",
            "source_kind": "newspaper",
            "authority_scope": "PUBLIC_MEDIA",
        },
        participants=[
            {
                "person_uid": "PERS-CLOUD-COACH01",
                "display_name": "周景行",
                "role": "ACTION_INITIATOR",
                "internal_verbatim_or_note": "内部笔记：周景行要求全场紧逼提速。",
                "public_perspective_summary": "周景行在2025-2026赛季季前冬训中推动全队提速转型。",
            },
            {
                "person_uid": "PERS-CLOUD-PLAY001",
                "display_name": "林启明",
                "role": "SUBJECT_INTERVIEWEE",
                "internal_verbatim_or_note": "内部笔记：林启明表示体能储备充足。",
                "public_perspective_summary": "林启明在2025-2026赛季承担更多外线持球与转换进攻任务。",
            },
            {
                "person_uid": "PERS-CLOUD-PLAY002",
                "display_name": "宋知行",
                "role": "CO_PARTICIPANT",
                "internal_verbatim_or_note": "内部笔记：宋知行负责第二阵容推进。",
                "public_perspective_summary": "宋知行在冬训中配合完成轮换提速演练。",
            },
        ],
        organization_ids=["ORG-SYNTH-CLOUD"],
        visibility_policy={"public_export_allowed": True},
    )


# =============================================================================
# P0-1: Wikipedia 2.0 Hero (wiki_v241, Lead Paragraph, Bio Infobox, Audit Drawer)
# =============================================================================


class TestP01Wikipedia2Hero:
    def test_wiki_v241_clean_h1_for_player_transitioned_coach_and_pure_coach(self) -> None:
        from cba_kb.kc_render import CARD_STYLES, validate_card_style
        from cba_kb.person_card_builder import build_person_card
        from cba_kb.role_facets import build_coach_role_facets

        assert "wiki_v241" in CARD_STYLES
        assert validate_card_style("wiki_v241") == "wiki_v241"

        registry, bios = _make_synth_registry_and_bios()
        career_stats = _make_synth_career_stats()

        # 1. Pure coach (not in master_rows / foreign_rows, 0 player stats)
        pure_coach_facets = build_coach_role_facets(
            person_uid="PERS-CLOUD-PUREC02",
            person_name="陈致远",
            organization_id="ORG-SYNTH-CLOUD",
            season="2026-2027",
            league_listed_role="ASSISTANT_COACH",
            club_appointed_role="ASSISTANT_COACH",
            evidence_refs=("https://example.invalid/club/cloud/asst-2025",),
        )
        res_pure = build_person_card(
            "陈致远",
            season="2026-2027",
            master_rows=[],
            foreign_rows=[],
            season_stats_records=career_stats,
            entity_role="ASSISTANT_COACH",
            visibility="public_safe",
            card_style="wiki_v241",
            person_registry=registry,
            person_bio_profiles=bios,
            coach_role_facets=pure_coach_facets,
        )
        assert res_pure["status"] == "BUILT"
        md_pure = res_pure["markdown"]
        assert md_pure.splitlines()[0].strip() == "# 陈致远"
        for forbidden in ("Research Card (Serving Projection)", "人物研究卡｜", "(unassigned)"):
            assert forbidden not in md_pure
        assert not re.search(r"\bpid_[0-9a-f]{32}\b", md_pure)
        # Pure coach omits player career stats section gracefully
        assert "## 球员生涯数据" not in md_pure
        assert "## 执教履历与角色" in md_pure

        # 2. Transitioned coach with 0 current-season stats but historical player stats
        trans_facets = build_coach_role_facets(
            person_uid="PERS-CLOUD-COACH01",
            person_name="周景行",
            organization_id="ORG-SYNTH-CLOUD",
            season="2026-2027",
            league_listed_role="ASSISTANT_COACH",
            club_appointed_role="HEAD_COACH",
            evidence_refs=("https://example.invalid/club/cloud/coach-2025",),
        )
        res_trans = build_person_card(
            "周景行",
            season="2026-2027",
            master_rows=[],
            foreign_rows=[],
            season_stats_records=career_stats,
            entity_role="HEAD_COACH",
            visibility="public_safe",
            card_style="wiki_v241",
            person_registry=registry,
            person_bio_profiles=bios,
            coach_role_facets=trans_facets,
        )
        assert res_trans["status"] == "BUILT"
        md_trans = res_trans["markdown"]
        assert md_trans.splitlines()[0].strip() == "# 周景行"
        assert "Research Card (Serving Projection)" not in md_trans
        assert "(unassigned)" not in md_trans
        # Transitioned coach keeps historical player career stats via linked_player_uids
        assert "## 球员生涯数据" in md_trans
        assert "2022-2023" in md_trans
        assert "浙江稠州金租" in md_trans

    def test_person_bio_profile_validation_and_zero_rejection(self) -> None:
        from cba_kb.person_registry import (
            PERSON_BIO_PROFILES_SCHEMA,
            PersonBioProfile,
            PersonRegistryError,
            make_person_bio_profiles,
            validate_person_bio_profiles,
        )

        _, bios = _make_synth_registry_and_bios()
        validated = validate_person_bio_profiles(bios)
        assert validated["schema"] == PERSON_BIO_PROFILES_SCHEMA
        assert len(validated["profiles"]) == 4

        with pytest.raises((PersonRegistryError, ValueError)):
            PersonBioProfile(
                person_uid="PERS-CLOUD-BAD0001",
                canonical_name="无效身高",
                height_cm=0,
            )

        with pytest.raises((PersonRegistryError, ValueError)):
            make_person_bio_profiles(
                [
                    PersonBioProfile(person_uid="PERS-CLOUD-DUP0001", canonical_name="重复甲"),
                    PersonBioProfile(person_uid="PERS-CLOUD-DUP0001", canonical_name="重复乙"),
                ]
            )

    def test_compose_wiki_lead_paragraph_80_to_180_chars_with_inline_citations(self) -> None:
        from cba_kb.editorial_brief import compose_wiki_lead_paragraph
        from cba_kb.kc_render import CitationRegistry

        _, bios = _make_synth_registry_and_bios()
        lin_bio = next(p for p in bios["profiles"] if p["person_uid"] == "PERS-CLOUD-PLAY001")
        reg = CitationRegistry(visibility="public_safe")
        story = _make_synth_story()

        lead = compose_wiki_lead_paragraph(
            person_name="林启明",
            bio_profile=lin_bio,
            registration_profile={
                "current_club": "云海新锐俱乐部",
                "registration_type": "DOMESTIC",
                "position": "前锋",
                "jersey_number": "7",
                "domestic_current": {
                    "season": "2026-2027",
                    "club_official": "云海新锐俱乐部",
                    "contract_category": "B类",
                    "registration_status": "已注册",
                },
            },
            career_season_stats={
                "regular_career_summary": {
                    "games_played": 60,
                    "points_per_game": 16.0,
                    "rebounds_per_game": 6.5,
                    "assists_per_game": 3.5,
                },
                "seasons_count": 2,
            },
            projected_stories=[
                {
                    "title": story["title"],
                    "occurred_season": story["occurred_season"],
                    "summary": "林启明在2025-2026赛季承担更多外线持球与转换进攻任务。",
                    "canonical_source": story["canonical_source"],
                }
            ],
            citation_registry=reg,
            visibility="public_safe",
        )
        plain = lead["lead_plain_text"]
        md = lead["lead_markdown"]
        assert 80 <= len(plain) <= 180, f"Expected 80..180 chars, got {len(plain)}: {plain}"
        assert lead["char_count"] == len(plain)
        assert "林启明" in plain
        assert "云海新锐" in plain
        assert re.search(r"\[\d+\]", md), f"Expected inline [n] citation in lead_markdown: {md}"
        assert not re.search(r"\[\d+\]", plain), "lead_plain_text must not contain [n] markers"
        assert len(lead["citation_indices"]) >= 1
        assert "(unassigned)" not in plain
        assert "UNKNOWN" not in plain

    def test_audit_drawer_and_non_contradictory_domain_summary(self) -> None:
        from cba_kb.person_card_builder import build_person_card

        registry, bios = _make_synth_registry_and_bios()
        master_rows = [
            {
                "season": "2026-2027",
                "player": "宋知行",
                "player_uid": "player:cloud-guard-02",
                "club_official": "云海新锐俱乐部",
                "contract_category": "A1类",
                "registration_status": "已注册",
            }
        ]
        # Registered in 2026-2027 (registration=VERIFIED_PRESENT), but 0 stats in 2026-2027
        res = build_person_card(
            "宋知行",
            season="2026-2027",
            master_rows=master_rows,
            foreign_rows=[],
            season_stats_records=[],
            game_stats_records=[],
            visibility="public_safe",
            card_style="wiki_v241",
            person_registry=registry,
            person_bio_profiles=bios,
        )
        assert res["status"] == "BUILT"
        assert res["domain_horizons"]["registration"]["status"] == "VERIFIED_PRESENT"
        assert res["overall_verification_status"] in ("PARTIALLY_VERIFIED", "VERIFIED_DOMAIN_PRESENT")
        md = res["markdown"]
        # Audit metadata tucked inside <details class="audit-drawer"> (collapsed by default)
        assert '<details class="audit-drawer">' in md
        assert '<details class="audit-drawer" open' not in md
        # Outside audit drawer, must NOT emit contradictory blanket "Posture: UNKNOWN"
        outside_audit = re.sub(
            r'<details class="audit-drawer">.*?</details>', "", md, flags=re.DOTALL
        )
        assert "Posture: UNKNOWN" not in outside_audit


# =============================================================================
# P0-2: Multi-Season Career Tables + Chinese Team Mapping + D-02 + 0 Broken Links
# =============================================================================


class TestP02CareerTablesTeamMappingAndClubD02:
    def test_career_tables_no_double_counting_whole_season_vs_team_split(self) -> None:
        from cba_kb.person_card_builder import build_career_season_stats_tables

        career_stats = _make_synth_career_stats()
        tables = build_career_season_stats_tables(
            {"player_uid": "player:cloud-star-01", "player_name": "林启明"},
            career_stats,
        )
        # Regular season: 2024-2025 WHOLE_SEASON (40 GP) + 2025-2026 WHOLE_SEASON (20 GP) = 60 GP
        # MUST NOT double-count 2024-2025 TEAM_SPLIT (18 + 22 = 40 GP -> would be 100 GP if buggy)
        reg_summary = tables["regular_career_summary"]
        assert reg_summary is not None
        assert reg_summary["games_played"] == 60
        # Weighted PPG: (40 * 15.0 + 20 * 18.0) / 60 = 16.0
        assert abs(float(reg_summary["points_per_game"]) - 16.0) < 0.05

        po_summary = tables["playoffs_career_summary"]
        assert po_summary is not None
        assert po_summary["games_played"] == 6
        assert abs(float(po_summary["points_per_game"]) - 21.0) < 0.05

        # Numeric team IDs mapped to Chinese names in career table rows
        team_names = [str(r.get("team_name") or "") for r in tables["regular_season_rows"]]
        assert "浙江稠州金租" in team_names
        assert "广东东阳光" in team_names
        assert "29140" not in team_names
        assert "29124" not in team_names

    def test_resolve_cba_team_name_maps_numeric_ids_and_falls_back_honestly(self) -> None:
        from cba_kb.aliases import resolve_cba_team_name

        assert resolve_cba_team_name("29140") == "浙江稠州金租"
        assert resolve_cba_team_name(29140) == "浙江稠州金租"
        assert resolve_cba_team_name("29124") == "广东东阳光"
        assert resolve_cba_team_name("100074683") == "宁波町渥"
        assert resolve_cba_team_name("zhejiang_chouzhou") == "浙江稠州金租"
        assert resolve_cba_team_name("999999", fallback_name="未知球队") == "未知球队"
        assert resolve_cba_team_name("999999") == "999999"

    def test_gamelog_drawer_collapsed_and_chinese_opponents_in_wiki_v241(self) -> None:
        from cba_kb.person_card_builder import build_person_card

        registry, bios = _make_synth_registry_and_bios()
        master_rows = [
            {
                "season": "2026-2027",
                "player": "林启明",
                "player_uid": "player:cloud-star-01",
                "club_official": "云海新锐俱乐部",
                "contract_category": "B类",
                "registration_status": "已注册",
            }
        ]
        res = build_person_card(
            "林启明",
            season="2026-2027",
            master_rows=master_rows,
            foreign_rows=[],
            season_stats_records=_make_synth_career_stats(),
            game_stats_records=_make_synth_game_logs(),
            visibility="public_safe",
            card_style="wiki_v241",
            person_registry=registry,
            person_bio_profiles=bios,
        )
        assert res["status"] == "BUILT"
        md = res["markdown"]
        assert '<details class="gamelog-drawer">' in md
        assert '<details class="gamelog-drawer" open' not in md
        # All 5 numeric opponent IDs mapped to Chinese team names
        for zh_team in ("浙江稠州金租", "广东东阳光", "北京北汽", "辽宁本钢", "宁波町渥"):
            assert zh_team in md
        for raw_id in ("| 29140 |", "| 29124 |", "| 29115 |", "| 29129 |", "| 100074683 |"):
            assert raw_id not in md

    def test_club_card_two_season_offseason_changes_and_zero_broken_links_site(
        self, tmp_path: Path
    ) -> None:
        from cba_kb.club_card_builder import build_club_panorama_card
        from cba_kb.person_card_builder import build_person_card
        from cba_kb.static_site_builder import build_public_site

        registry, bios = _make_synth_registry_and_bios()
        master_rows = [
            # 2025-2026: 林启明 (retained), 宋知行 (departed)
            {
                "season": "2025-2026",
                "player": "林启明",
                "player_uid": "player:cloud-star-01",
                "club_official": "云海新锐俱乐部",
                "contract_category": "B类",
                "registration_status": "已注册",
            },
            {
                "season": "2025-2026",
                "player": "宋知行",
                "player_uid": "player:cloud-guard-02",
                "club_official": "云海新锐俱乐部",
                "contract_category": "A1类",
                "registration_status": "已注册",
            },
            # 2026-2027: 林启明 (retained), 赵子昂 (joined)
            {
                "season": "2026-2027",
                "player": "林启明",
                "player_uid": "player:cloud-star-01",
                "club_official": "云海新锐俱乐部",
                "contract_category": "B类",
                "registration_status": "已注册",
            },
            {
                "season": "2026-2027",
                "player": "赵子昂",
                "player_uid": "player:cloud-rookie-03",
                "club_official": "云海新锐俱乐部",
                "contract_category": "A2类",
                "registration_status": "已注册",
            },
        ]
        club_res = build_club_panorama_card(
            "云海新锐俱乐部",
            seasons=("2025-2026", "2026-2027"),
            organization_profile={
                "organization_id": "ORG-SYNTH-CLOUD",
                "canonical_name": "云海新锐俱乐部",
                "short_name": "云海新锐",
                "city": "云海市",
            },
            master_rows=master_rows,
            story_records=[_make_synth_story()],
            visibility="public_safe",
            card_style="wiki_v241",
            observed_until="2026-11-25T00:00:00Z",
        )
        changes = club_res["offseason_roster_changes"]
        assert "林启明" in changes["retained"]
        assert "赵子昂" in changes["joined"]
        assert "宋知行" in changes["departed"]
        assert "## 休赛期阵容变动" in club_res["markdown"]
        assert set(club_res["all_roster_members"]) >= {"林启明", "赵子昂", "宋知行"}

        # Build person cards for ALL active + departed members (including departed 宋知行)
        person_cards = []
        for pname in ("林启明", "赵子昂", "宋知行"):
            pc = build_person_card(
                pname,
                season="2026-2027",
                master_rows=master_rows,
                season_stats_records=_make_synth_career_stats(),
                visibility="public_safe",
                card_style="wiki_v241",
                person_registry=registry,
                person_bio_profiles=bios,
            )
            assert pc["status"] == "BUILT"
            person_cards.append(pc)

        site_dir = tmp_path / "site_d02"
        site_res = build_public_site(
            club_card=club_res,
            person_cards=person_cards,
            output_dir=site_dir,
            site_title="云海新锐百科站",
            observed_until="2026-11-25T00:00:00Z",
        )
        assert site_res["status"] == "BUILT"
        assert site_res["broken_links_count"] == 0
        assert len(site_res["person_pages"]) == 3

        # Verify every internal href in index.html resolves to a generated file on disk
        index_html = (site_dir / "index.html").read_text(encoding="utf-8")
        internal_hrefs = re.findall(r'href="(persons/[^"#]+)"', index_html)
        assert len(internal_hrefs) >= 3
        for rel_href in internal_hrefs:
            assert (site_dir / rel_href).is_file(), f"Broken link in index.html: {rel_href}"


# =============================================================================
# P0-3: CitationRegistry, Inline [n] Footnotes, Story Dedup, doc_<hex> Block
# =============================================================================


class TestP03CitationsStoryDedupAndVisibilityGuard:
    def test_citation_registry_dedup_including_urlless_newspaper_and_wechat(self) -> None:
        from cba_kb.kc_render import CitationRegistry

        reg = CitationRegistry(visibility="public_safe")
        idx1 = reg.register(
            label="CBA官网注册公示",
            url="https://example.invalid/cba/reg/2026",
            source_kind="league_official",
        )
        idx1_dup = reg.register(
            label="CBA官网注册公示（重复引用）",
            url="https://example.invalid/cba/reg/2026",
            source_kind="league_official",
        )
        assert idx1 == 1
        assert idx1_dup == 1

        # URL-less newspaper citation
        idx2 = reg.register(
            label="云海日报：新锐男篮冬训提速",
            publisher="云海日报",
            published_at="2026-02-01",
            source_kind="newspaper",
        )
        idx2_dup = reg.register(
            label="云海日报：新锐男篮冬训提速",
            publisher="云海日报",
            published_at="2026-02-01",
            source_kind="newspaper",
        )
        assert idx2 == 2
        assert idx2_dup == 2

        # URL-less official WeChat citation
        idx3 = reg.register(
            label="云海新锐官方微信公众号：新赛季阵容官宣",
            publisher="云海新锐官方微信公众号",
            published_at="2026-08-28",
            source_kind="club_wechat",
        )
        assert idx3 == 3

        # Unreferenced encyclopedia MUST be skipped under public_safe
        idx_skip = reg.register(
            label="无来源百科词条",
            url="https://example.invalid/wiki/unreferenced",
            source_kind="unreferenced_encyclopedia",
        )
        assert idx_skip is None

        entries = reg.entries()
        assert len(entries) == 3
        ref_md = reg.render_markdown_references()
        assert "## 参考资料" in ref_md
        assert "云海日报" in ref_md
        assert "云海新锐官方微信公众号" in ref_md

    def test_wiki_v241_inline_superscripts_single_references_no_dangling_sources(
        self, tmp_path: Path
    ) -> None:
        from cba_kb.club_card_builder import build_club_panorama_card
        from cba_kb.person_card_builder import build_person_card
        from cba_kb.static_site_builder import build_public_site

        registry, bios = _make_synth_registry_and_bios()
        story = _make_synth_story()
        master_rows = [
            {
                "season": "2026-2027",
                "player": "林启明",
                "player_uid": "player:cloud-star-01",
                "club_official": "云海新锐俱乐部",
                "contract_category": "B类",
                "registration_status": "已注册",
            }
        ]
        pcard = build_person_card(
            "林启明",
            season="2026-2027",
            master_rows=master_rows,
            season_stats_records=_make_synth_career_stats(),
            game_stats_records=_make_synth_game_logs(),
            story_records=[story],
            document_bundles=[
                {
                    "doc_id": f"doc_{_synth_sha('ann-01')[:24]}",
                    "title": "云海新锐新赛季注册名单公告",
                    "source_tier": "T1",
                    "completeness": "full",
                    "published_at": "2026-08-30",
                    "canonical_url": "https://example.invalid/club/cloud/roster-2026",
                    "matched_persons": ["林启明"],
                }
            ],
            visibility="public_safe",
            card_style="wiki_v241",
            person_registry=registry,
            person_bio_profiles=bios,
        )
        assert pcard["status"] == "BUILT"
        md = pcard["markdown"]
        assert "## 参考资料" in md
        assert "## External Citations" not in md
        assert "## Statistical Evidence" not in md
        assert "## Registration & Identity Profile" not in md
        assert "## 球员生涯数据" in md
        assert "## 人物故事与关键事件" in md

        club_card = build_club_panorama_card(
            "云海新锐俱乐部",
            seasons=("2025-2026", "2026-2027"),
            organization_profile={
                "organization_id": "ORG-SYNTH-CLOUD",
                "canonical_name": "云海新锐俱乐部",
                "short_name": "云海新锐",
            },
            master_rows=master_rows,
            story_records=[story],
            visibility="public_safe",
            card_style="wiki_v241",
        )
        site_dir = tmp_path / "site_citations"
        site_res = build_public_site(
            club_card=club_card,
            person_cards=[pcard],
            output_dir=site_dir,
            observed_until="2026-11-25T00:00:00Z",
        )
        p_html = Path(site_res["person_pages"][0]["path"]).read_text(encoding="utf-8")
        # Inline superscript <a class="fn-ref" href="#fn-1" id="fnref-1">[1]</a> + back-link
        assert '<a class="fn-ref" href="#fn-1" id="fnref-1">[1]</a>' in p_html
        assert 'id="fn-1"' in p_html
        assert 'href="#fnref-1"' in p_html
        assert "&#8617;" in p_html or "↩" in p_html
        # Single bottom 参考资料 section; NO dangling Sources: bar and NO duplicate External Citations
        assert p_html.count('id="footnotes"') == 1
        assert "参考资料" in p_html
        assert "Sources:" not in p_html
        assert "External Citations" not in p_html

    def test_verify_public_safe_blocks_doc_hex_and_unassigned_fail_closed(self) -> None:
        from cba_kb.kc_visibility import verify_public_safe_card_text

        clean_text = "# 林启明\n\n云海新锐俱乐部前锋，2025-2026赛季场均18.0分。\n\nend_marker: true\n"
        verify_public_safe_card_text(clean_text)

        doc_hex_text = f"# 林启明\n- 来源编号：doc_{_synth_sha('leak')[:24]}\n\nend_marker: true\n"
        with pytest.raises(ValueError, match="PUBLIC_SAFE_REDACTION_VIOLATION"):
            verify_public_safe_card_text(doc_hex_text)

        unassigned_text = "# 人物\n- 球员：林启明 (unassigned)\n\nend_marker: true\n"
        with pytest.raises(ValueError, match="PUBLIC_SAFE_REDACTION_VIOLATION"):
            verify_public_safe_card_text(unassigned_text)

    def test_story_organization_summary_and_no_triple_concatenation(self) -> None:
        from cba_kb.story_record import make_story_record, project_stories_for_entity

        # 1. Explicit organization_summary is used directly
        story_with_org_sum = _make_synth_story()
        proj_explicit = project_stories_for_entity(
            [story_with_org_sum],
            entity_id="ORG-SYNTH-CLOUD",
            entity_kind="organization",
            visibility="public_safe",
        )
        assert len(proj_explicit) == 1
        assert proj_explicit[0]["summary"] == (
            "云海新锐在2025-2026赛季前由主帅周景行主导提速攻防体系，确立林启明为锋线核心。"
        )

        # 2. When organization_summary is omitted, picks single primary summary (NEVER concatenates 3x)
        story_no_org_sum = make_story_record(
            title="云海新锐青训交流赛",
            occurred_season="2025-2026",
            published_at="2026-03-01T00:00:00Z",
            canonical_source={
                "source_id": "src-cloud-youth-01",
                "title": "云海体育周报",
                "source_kind": "newspaper",
            },
            participants=[
                {
                    "person_uid": "PERS-CLOUD-COACH01",
                    "display_name": "周景行",
                    "role": "ACTION_INITIATOR",
                    "internal_verbatim_or_note": None,
                    "public_perspective_summary": "周景行带队完成青训交流赛评估。",
                },
                {
                    "person_uid": "PERS-CLOUD-PLAY001",
                    "display_name": "林启明",
                    "role": "SUBJECT_INTERVIEWEE",
                    "internal_verbatim_or_note": None,
                    "public_perspective_summary": "林启明在交流赛中分享锋线经验。",
                },
                {
                    "person_uid": "PERS-CLOUD-PLAY002",
                    "display_name": "宋知行",
                    "role": "CO_PARTICIPANT",
                    "internal_verbatim_or_note": None,
                    "public_perspective_summary": "宋知行参与全场对抗演练。",
                },
            ],
            organization_ids=["ORG-SYNTH-CLOUD"],
            visibility_policy={"public_export_allowed": True},
        )
        proj_fallback = project_stories_for_entity(
            [story_no_org_sum],
            entity_id="ORG-SYNTH-CLOUD",
            entity_kind="organization",
            visibility="public_safe",
        )
        assert len(proj_fallback) == 1
        summary = proj_fallback[0]["summary"]
        assert summary == "周景行带队完成青训交流赛评估。"
        assert "林启明在交流赛中分享锋线经验" not in summary
        assert "宋知行参与全场对抗演练" not in summary


# =============================================================================
# P0-4: GEO sitemap.xml + llms.txt + Related Persons (Top-8) + Navbox + JSON-LD
# =============================================================================


class TestP04GeoAssetsRelatedPersonsAndNavbox:
    def test_geo_sitemap_llms_txt_infobox_related_top8_navbox_and_enhanced_jsonld(
        self, tmp_path: Path
    ) -> None:
        from cba_kb.club_card_builder import build_club_panorama_card
        from cba_kb.kc_visibility import verify_public_safe_card_text
        from cba_kb.person_card_builder import build_person_card
        from cba_kb.person_registry import (
            PersonBioProfile,
            PersonProfile,
            RoleTenure,
            make_person_bio_profiles,
            make_person_registry,
        )
        from cba_kb.static_site_builder import build_public_site, compute_related_persons

        story = _make_synth_story()
        # Create 12 synthetic persons (1 coach + 11 players) to verify top-8 cap on 关联人物
        extra_names = [
            ("PERS-CLOUD-PLAY003", "何清扬", "player:cloud-p03"),
            ("PERS-CLOUD-PLAY004", "苏明澈", "player:cloud-p04"),
            ("PERS-CLOUD-PLAY005", "陆见川", "player:cloud-p05"),
            ("PERS-CLOUD-PLAY006", "许安澜", "player:cloud-p06"),
            ("PERS-CLOUD-PLAY007", "顾星河", "player:cloud-p07"),
            ("PERS-CLOUD-PLAY008", "沈知微", "player:cloud-p08"),
            ("PERS-CLOUD-PLAY009", "唐景明", "player:cloud-p09"),
            ("PERS-CLOUD-PLAY010", "叶舒白", "player:cloud-p10"),
            ("PERS-CLOUD-PLAY011", "程砚秋", "player:cloud-p11"),
        ]
        base_reg, base_bios = _make_synth_registry_and_bios()
        all_profiles = list(base_reg["persons"])
        all_bios = list(base_bios["profiles"])
        master_rows = [
            {
                "season": "2026-2027",
                "player": "林启明",
                "player_uid": "player:cloud-star-01",
                "club_official": "云海新锐俱乐部",
                "contract_category": "B类",
                "registration_status": "已注册",
            },
            {
                "season": "2026-2027",
                "player": "宋知行",
                "player_uid": "player:cloud-guard-02",
                "club_official": "云海新锐俱乐部",
                "contract_category": "A1类",
                "registration_status": "已注册",
            },
        ]
        for puid, pname, pluid in extra_names:
            all_profiles.append(
                PersonProfile(
                    person_uid=puid,
                    canonical_name=pname,
                    linked_player_uids=(pluid,),
                    role_tenures=(
                        RoleTenure(
                            role="PLAYER",
                            organization_id="ORG-SYNTH-CLOUD",
                            organization_name="云海新锐俱乐部",
                            season_start="2026-2027",
                        ),
                    ),
                ).to_dict()
            )
            all_bios.append(
                PersonBioProfile(
                    person_uid=puid,
                    canonical_name=pname,
                    nationality="中国",
                    current_club="云海新锐俱乐部",
                    current_role="前锋",
                ).to_dict()
            )
            master_rows.append(
                {
                    "season": "2026-2027",
                    "player": pname,
                    "player_uid": pluid,
                    "club_official": "云海新锐俱乐部",
                    "contract_category": "A1类",
                    "registration_status": "已注册",
                }
            )
        full_registry = make_person_registry(all_profiles)
        full_bios = make_person_bio_profiles(all_bios)

        club_card = build_club_panorama_card(
            "云海新锐俱乐部",
            seasons=("2025-2026", "2026-2027"),
            organization_profile={
                "organization_id": "ORG-SYNTH-CLOUD",
                "canonical_name": "云海新锐俱乐部",
                "short_name": "云海新锐",
            },
            master_rows=master_rows,
            story_records=[story],
            visibility="public_safe",
            card_style="wiki_v241",
            observed_until="2026-11-25T00:00:00Z",
        )

        person_cards = []
        for p in full_registry["persons"]:
            is_coach = p["person_uid"] in ("PERS-CLOUD-COACH01", "PERS-CLOUD-PUREC02")
            pc = build_person_card(
                p["canonical_name"],
                season="2026-2027",
                master_rows=master_rows,
                season_stats_records=_make_synth_career_stats(),
                game_stats_records=_make_synth_game_logs(),
                story_records=[story],
                entity_role="HEAD_COACH" if p["person_uid"] == "PERS-CLOUD-COACH01" else (
                    "ASSISTANT_COACH" if p["person_uid"] == "PERS-CLOUD-PUREC02" else "PLAYER"
                ),
                visibility="public_safe",
                card_style="wiki_v241",
                person_registry=full_registry,
                person_bio_profiles=full_bios,
            )
            assert pc["status"] == "BUILT"
            person_cards.append(pc)

        # Unit check compute_related_persons top-8 cap & relation labels
        lin_card = next(c for c in person_cards if c["person_name"] == "林启明")
        related = compute_related_persons(
            lin_card, person_cards, story_records=[story], max_related=8
        )
        assert len(related) == 8
        top_names = [r["person_name"] for r in related[:3]]
        assert "周景行" in top_names
        assert "宋知行" in top_names
        assert all(r.get("relation_label") for r in related)

        site_dir = tmp_path / "site_geo"
        site_res = build_public_site(
            club_card=club_card,
            person_cards=person_cards,
            output_dir=site_dir,
            site_title="云海新锐公开百科站",
            observed_until="2026-11-25T00:00:00Z",
            base_url="https://example.invalid/cloud-wiki",
            story_records=[story],
        )
        assert site_res["status"] == "BUILT"
        assert site_res["broken_links_count"] == 0

        # 1. sitemap.xml
        sitemap_path = Path(site_res["sitemap_path"])
        assert sitemap_path.is_file()
        sitemap_text = sitemap_path.read_text(encoding="utf-8")
        verify_public_safe_card_text(sitemap_text)
        root = ET.fromstring(sitemap_text)
        locs = [el.text for el in root.iter() if el.tag.endswith("loc") and el.text]
        assert any(u.endswith("/index.html") or u.endswith("index.html") for u in locs)
        assert len(locs) == 1 + len(person_cards)

        # 2. llms.txt
        llms_path = Path(site_res["llms_txt_path"])
        assert llms_path.is_file()
        llms_text = llms_path.read_text(encoding="utf-8")
        verify_public_safe_card_text(llms_text)
        assert "云海新锐" in llms_text
        assert "林启明" in llms_text
        assert "persons/" in llms_text

        # 3. Lin Qiming person page: meta description, wiki-infobox, related-persons (<=8),
        #    collapsible squad-navbox, enhanced JSON-LD
        lin_page = next(p for p in site_res["person_pages"] if p["person_name"] == "林启明")
        lin_html = Path(lin_page["path"]).read_text(encoding="utf-8")
        assert '<meta name="description"' in lin_html
        assert '<aside class="wiki-infobox">' in lin_html
        assert "198" in lin_html
        assert "94" in lin_html
        assert "云海青训体系" in lin_html
        assert '<section class="related-persons">' in lin_html
        assert "关联人物" in lin_html
        assert '<nav class="squad-navbox">' in lin_html
        # Collapsible navbox (no open attribute on details)
        navbox_match = re.search(
            r'<nav class="squad-navbox">.*?</nav>', lin_html, flags=re.DOTALL
        )
        assert navbox_match is not None
        assert "<details>" in navbox_match.group(0)

        # Pure coach page: infobox auto-hides missing height/weight/birth_date/jersey
        pure_page = next(p for p in site_res["person_pages"] if p["person_name"] == "陈致远")
        pure_html = Path(pure_page["path"]).read_text(encoding="utf-8")
        assert '<aside class="wiki-infobox">' in pure_html
        infobox_pure = re.search(
            r'<aside class="wiki-infobox">.*?</aside>', pure_html, flags=re.DOTALL
        )
        assert infobox_pure is not None
        assert "0 cm" not in infobox_pure.group(0)
        assert "0 kg" not in infobox_pure.group(0)
        assert "unassigned" not in infobox_pure.group(0).lower()
        assert "身高" not in infobox_pure.group(0)
        assert "体重" not in infobox_pure.group(0)

        # Enhanced JSON-LD for 林启明
        ld_blocks = _extract_json_ld_blocks(lin_html)
        person_ld = next(b for b in ld_blocks if b.get("@type") == "Person")
        assert person_ld.get("description")
        assert person_ld.get("jobTitle")
        assert person_ld.get("birthDate") == "2001-05-14"
        assert person_ld.get("height", {}).get("value") == 198
        assert person_ld.get("weight", {}).get("value") == 94
        assert person_ld.get("citation")


# =============================================================================
# P0-5: Local Draft Extractor + CLI + v2.4.1-1/2 Release Spec + cloud_state.py
# =============================================================================


class TestP05DraftExtractorReleaseSpecAndCloudState:
    def test_local_draft_extractor_api_and_cli_zero_canonical_writes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from cba_kb.wiki_draft_extractor import (
            DRAFT_REVIEW_STATUS,
            WIKI_DRAFT_CANDIDATES_SCHEMA,
            extract_wiki_candidates,
        )

        assert DRAFT_REVIEW_STATUS == "DRAFT_PENDING_HUMAN_REVIEW"

        registry, _ = _make_synth_registry_and_bios()
        reg_path = tmp_path / "person_registry.json"
        reg_bytes_before = json.dumps(registry, ensure_ascii=False, indent=2).encode("utf-8")
        reg_path.write_bytes(reg_bytes_before)

        sources_dir = tmp_path / "local_sources"
        sources_dir.mkdir()
        src_doc = sources_dir / "cloud_media_note.md"
        src_doc.write_text(
            (
                "# 云海体育专访：林启明与周景行的2025-2026赛季\n\n"
                "林启明，2001年5月14日出生，身高198cm，体重94kg，司职前锋，出自云海青训体系。\n"
                "2025-2026赛季季前，主教练周景行率领云海新锐完成战术提速，林启明成为核心锋线。\n"
            ),
            encoding="utf-8",
        )

        out_api = tmp_path / "draft_api.json"
        res = extract_wiki_candidates(
            sources_dir=sources_dir,
            person_registry=reg_path,
            output_path=out_api,
        )
        assert res["schema"] == WIKI_DRAFT_CANDIDATES_SCHEMA
        assert res["review_status"] == "DRAFT_PENDING_HUMAN_REVIEW"
        assert res["canonical_stores_modified"] is False
        assert res["network_called"] is False
        assert res["sources_scanned"] >= 1
        assert len(res["bio_profile_candidates"]) >= 1
        assert len(res["story_and_timeline_candidates"]) >= 1
        for cand in res["bio_profile_candidates"]:
            assert cand["review_status"] == "DRAFT_PENDING_HUMAN_REVIEW"
        for cand in res["story_and_timeline_candidates"]:
            assert cand["review_status"] == "DRAFT_PENDING_HUMAN_REVIEW"
        assert out_api.is_file()
        # Canonical registry untouched (0-byte diff)
        assert reg_path.read_bytes() == reg_bytes_before

        # CLI `cba-kb draft-wiki-candidates`
        out_cli = tmp_path / "draft_cli.json"
        code, stdout, stderr = _run_cli(
            monkeypatch,
            capsys,
            [
                "draft-wiki-candidates",
                "--sources-dir",
                str(sources_dir),
                "--person-registry",
                str(reg_path),
                "--output",
                str(out_cli),
            ],
        )
        assert code == 0, f"CLI failed: stderr={stderr}"
        assert out_cli.is_file()
        cli_payload = json.loads(out_cli.read_text(encoding="utf-8"))
        assert cli_payload["review_status"] == "DRAFT_PENDING_HUMAN_REVIEW"
        assert cli_payload["canonical_stores_modified"] is False
        assert stdout.strip()

    def test_v241_release_specs_and_cloud_state_bio_profiles_allowlist(self) -> None:
        from cba_kb.cloud_state import (
            _bundle_allowlisted_exact_names,
            _is_bundle_member_allowed,
        )
        from cba_kb.orchestration.release import prepare_v215

        assert prepare_v215.V241_1_BASELINE_SHA == BASELINE_SHA_V241_1

        for rel_id in ("v2.4.1-1", "v2.4.1-2"):
            assert rel_id in prepare_v215.V215_RELEASE_SPECS
            spec = prepare_v215.V215_RELEASE_SPECS[rel_id]
            assert spec["product_baseline_sha"] == BASELINE_SHA_V241_1
            assert spec["enable_v240_shenzhen_narrative_pilot"] is True
            assert spec["enable_v241_wikipedia_site_v2"] is True
            assert spec["active_league_season"] == "2026-2027"
            assert spec["incremental_delta_only"] is True
            assert spec["archive_format"] == "bundle_v1"

        assert ("v2.4.1-2", "v2.4.1-1") in prepare_v215._POST_ROLLBACK_RECOVERY_ALLOWLIST

        # Historical specs remain untouched
        assert not prepare_v215.V215_RELEASE_SPECS["v2.4.0-1"].get("enable_v241_wikipedia_site_v2")
        assert not prepare_v215.V215_RELEASE_SPECS["v2.3.2-1"].get("enable_v241_wikipedia_site_v2")

        # cloud_state allowlist includes person_bio_profiles.json
        allowlisted = _bundle_allowlisted_exact_names()
        assert "person_bio_profiles.json" in allowlisted
        assert _is_bundle_member_allowed("person_bio_profiles.json", allowlisted) is True
        assert _is_bundle_member_allowed("evidence/secret.json", allowlisted) is False
