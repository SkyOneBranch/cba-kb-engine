"""Frozen red-light remediation tests for REQ-241 Q2 Round 1 findings (Commit 3).

100% synthetic clean-room fixtures (G5 == 0, G6 == 0).
Covers all 1 P0 blocker + 5 Minor findings from Independent Q1+Q2 Round 1 review
on HEAD ca94f39a21f5ee49d24efec9e89e890bd2cd78bd:
- P0-1 Blocker: Infobox / full HTML public-safe bypass (`doc_<hex>` in 3rd honor,
  `english_name`, or `jersey_number` MUST fail closed with
  `PUBLIC_SAFE_REDACTION_VIOLATION` and leave 0 partial output on disk).
- m-1: `compute_related_persons` MUST NOT emit unrelated fallback ("关联成员") and
  MUST NOT match coach+player as "师徒" when either club is empty/unknown.
- m-4: `render_research_card(..., card_style="wiki_v241")` MUST NOT emit
  contradictory top-level `UNKNOWN` when `domain_horizons` has `VERIFIED_PRESENT`.
- m-5a: `build_career_season_stats_tables` resolves numeric `team_name="29140"`
  (with `team_id=None`) to `"浙江稠州金租"`.
- m-5b: `build_career_season_stats_tables` routes `match_type="PLAYOFF"` to
  `playoffs_rows`, does not misclassify unknown/empty `match_type` into
  `regular_season_rows`, and sums multi-row `games_played` accurately.
- m-5c: `extract_wiki_candidates` merges/preserves bio fields across multiple
  sentences in the same source file (both `height_cm` and `birth_date`).
- m-6: `resolve_cba_team_name("29140", prefer_short=True)` returns a concise
  short Chinese name distinct from `prefer_short=False` ("浙江稠州金租").
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict

import pytest

_LEAK_DOC_HEX = "doc_0123456789abcdef01234567"


def _minimal_clean_club_card() -> Dict[str, Any]:
    return {
        "organization_id": "ORG-SYNTH-CLOUD",
        "club_name": "云海新锐俱乐部",
        "visibility": "public_safe",
        "card_style": "wiki_v241",
        "markdown": (
            "# 云海新锐俱乐部\n\n"
            "## 球队概况\n"
            "- 云海新锐俱乐部公开测试页面。\n\n"
            "end_marker: true\n"
        ),
    }


def _minimal_clean_person_card(bio_profile: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "person_uid": bio_profile["person_uid"],
        "person_name": bio_profile["canonical_name"],
        "slug": "pers-cloud-probe01",
        "visibility": "public_safe",
        "card_style": "wiki_v241",
        "bio_profile": bio_profile,
        "lead_plain_text": "林启明是中国职业篮球运动员，司职前锋，现效力于云海新锐俱乐部。",
        "markdown": (
            "# 林启明\n\n"
            "林启明是中国职业篮球运动员，司职前锋，现效力于云海新锐俱乐部。\n\n"
            "end_marker: true\n"
        ),
    }


# =============================================================================
# P0-1 Blocker: Infobox / full HTML public-safe fail-closed guard
# =============================================================================


class TestQ2R1P01InfoboxPublicSafeFailClosed:
    @pytest.mark.parametrize(
        "field_case,bio_overrides",
        [
            (
                "honors_3rd_item",
                {
                    "honors": [
                        "2024-2025赛季合成全明星阵容",
                        "2025-2026赛季合成最佳第六人",
                        f"合成特别奖 {_LEAK_DOC_HEX}",
                    ]
                },
            ),
            (
                "english_name",
                {"english_name": f"Lin Qiming {_LEAK_DOC_HEX}"},
            ),
            (
                "jersey_number",
                {"jersey_number": f"7 {_LEAK_DOC_HEX}"},
            ),
        ],
    )
    def test_build_public_site_blocks_doc_hex_in_bio_infobox_zero_partial_output(
        self,
        tmp_path: Path,
        field_case: str,
        bio_overrides: Dict[str, Any],
    ) -> None:
        from cba_kb.person_registry import PersonBioProfile
        from cba_kb.static_site_builder import build_public_site

        bio_kwargs: Dict[str, Any] = {
            "person_uid": "PERS-CLOUD-PROBE01",
            "canonical_name": "林启明",
            "nationality": "中国",
            "current_club": "云海新锐俱乐部",
            "current_role": "前锋",
        }
        bio_kwargs.update(bio_overrides)
        if "honors" in bio_kwargs and isinstance(bio_kwargs["honors"], list):
            bio_kwargs["honors"] = tuple(bio_kwargs["honors"])
        bio_dict = PersonBioProfile(**bio_kwargs).to_dict()

        site_dir = tmp_path / f"site_leak_{field_case}"
        assert not site_dir.exists()

        with pytest.raises(ValueError, match="PUBLIC_SAFE_REDACTION_VIOLATION"):
            build_public_site(
                club_card=_minimal_clean_club_card(),
                person_cards=[_minimal_clean_person_card(bio_dict)],
                output_dir=site_dir,
                site_title="云海新锐公开百科站",
                observed_until="2026-11-25T00:00:00Z",
            )

        assert not site_dir.exists(), (
            f"Fail-closed violation: output_dir was created despite leak in {field_case}"
        )


# =============================================================================
# m-1: compute_related_persons strict relationship rules
# =============================================================================


class TestQ2R1M1RelatedPersonsStrict:
    def test_unrelated_person_excluded_and_empty_club_not_matched_as_coach_player(
        self,
    ) -> None:
        from cba_kb.static_site_builder import compute_related_persons

        target_player = {
            "person_uid": "PERS-CLOUD-PLAY001",
            "person_name": "林启明",
            "slug": "pers-cloud-play001",
            "entity_role": "PLAYER",
            "bio_profile": {
                "person_uid": "PERS-CLOUD-PLAY001",
                "canonical_name": "林启明",
                "current_club": "云海新锐俱乐部",
                "current_role": "前锋",
                "draft_or_youth_origin": "云海青训体系",
            },
        }
        unrelated_player = {
            "person_uid": "PERS-RIVAL-PLAY099",
            "person_name": "韩牧之",
            "slug": "pers-rival-play099",
            "entity_role": "PLAYER",
            "bio_profile": {
                "person_uid": "PERS-RIVAL-PLAY099",
                "canonical_name": "韩牧之",
                "current_club": "苍梧疾风俱乐部",
                "current_role": "中锋",
                "draft_or_youth_origin": "苍梧青训营",
            },
        }
        unknown_club_coach = {
            "person_uid": "PERS-UNKNOWN-COACH99",
            "person_name": "陆长风",
            "slug": "pers-unknown-coach99",
            "entity_role": "HEAD_COACH",
            "bio_profile": {
                "person_uid": "PERS-UNKNOWN-COACH99",
                "canonical_name": "陆长风",
                "current_club": None,
                "current_role": "主教练",
            },
        }

        related = compute_related_persons(
            target_player,
            [target_player, unrelated_player, unknown_club_coach],
            story_records=(),
            max_related=8,
        )
        related_uids = {r["person_uid"] for r in related}
        assert "PERS-RIVAL-PLAY099" not in related_uids, (
            f"Unrelated person must not appear in compute_related_persons: {related}"
        )
        assert not any(
            r["person_uid"] == "PERS-UNKNOWN-COACH99" and r.get("relation_label") == "师徒"
            for r in related
        ), f"Coach with empty/unknown club must not be matched as 师徒: {related}"
        assert "PERS-UNKNOWN-COACH99" not in related_uids, (
            f"Coach with empty/unknown club and no shared story must not be related: {related}"
        )


# =============================================================================
# m-4: kc_render overall_verification_status non-contradiction
# =============================================================================


class TestQ2R1M4OverallVerificationStatus:
    def test_render_wiki_v241_never_emits_contradictory_unknown_when_domain_verified(
        self,
    ) -> None:
        from cba_kb.kc_render import render_research_card

        payload: Dict[str, Any] = {
            "entity_role": "PLAYER",
            "overall_verification_status": "UNKNOWN",
            "evidence_transparency": {
                "posture": "UNKNOWN",
                "scope_meta": {
                    "player_name": "宋知行",
                    "season": "2026-2027",
                },
            },
            "domain_horizons": {
                "registration": {
                    "status": "VERIFIED_PRESENT",
                    "observed_until": "2026-11-25T00:00:00Z",
                    "note": "Registration records verified in local KB.",
                },
                "statistics": {
                    "status": "OBSERVED_EMPTY",
                    "observed_until": "2026-11-25T00:00:00Z",
                    "note": "No player statistics recorded yet.",
                },
                "official_announcements": {
                    "status": "OBSERVED_EMPTY",
                    "observed_until": "2026-11-25T00:00:00Z",
                    "note": "No official announcements observed.",
                },
            },
            "registration_profile": {
                "player_name": "宋知行",
                "current_club": "云海新锐俱乐部",
            },
        }
        md = render_research_card(
            payload,
            evidence_verified=True,
            visibility="public_safe",
            card_style="wiki_v241",
        )
        assert "- overall_verification_status: UNKNOWN" not in md
        assert "状态：UNKNOWN" not in md
        assert re.search(
            r"overall_verification_status:\s*(PARTIALLY_VERIFIED|VERIFIED_DOMAIN_PRESENT|VERIFIED)",
            md,
        ), f"Expected non-contradictory verified top-level status in:\n{md}"


# =============================================================================
# m-5a & m-5b: build_career_season_stats_tables numeric team_name, match_type & sum
# =============================================================================


class TestQ2R1M5CareerTablesHardening:
    def test_numeric_team_name_without_team_id_resolves_to_chinese_name(self) -> None:
        from cba_kb.person_card_builder import build_career_season_stats_tables

        records = [
            {
                "record_id": "stat-numeric-tname-01",
                "player_uid": "player:cloud-star-01",
                "player_name": "林启明",
                "season": "2025-2026",
                "match_type": "REGULAR",
                "scope": "WHOLE_SEASON",
                "team_id": None,
                "team_name": "29140",
                "metrics": {
                    "games_played": 20,
                    "points_per_game": 15.0,
                },
            }
        ]
        tables = build_career_season_stats_tables(
            {"player_uid": "player:cloud-star-01", "player_name": "林启明"},
            records,
        )
        assert len(tables["regular_season_rows"]) == 1
        assert tables["regular_season_rows"][0]["team_name"] == "浙江稠州金租"

    def test_match_type_playoff_routing_unknown_exclusion_and_multi_row_gp_sum(
        self,
    ) -> None:
        from cba_kb.person_card_builder import build_career_season_stats_tables

        records = [
            {
                "record_id": "stat-reg-2024",
                "player_uid": "player:cloud-star-01",
                "player_name": "林启明",
                "season": "2024-2025",
                "match_type": "REGULAR",
                "scope": "WHOLE_SEASON",
                "team_id": "29140",
                "metrics": {"games_played": 10, "points_per_game": 12.0},
            },
            {
                "record_id": "stat-reg-2025",
                "player_uid": "player:cloud-star-01",
                "player_name": "林启明",
                "season": "2025-2026",
                "match_type": "REGULAR",
                "scope": "WHOLE_SEASON",
                "team_id": "29140",
                "metrics": {"games_played": 5, "points_per_game": 18.0},
            },
            {
                "record_id": "stat-po-singular",
                "player_uid": "player:cloud-star-01",
                "player_name": "林启明",
                "season": "2025-2026",
                "match_type": "PLAYOFF",
                "scope": "WHOLE_SEASON",
                "team_id": "29140",
                "metrics": {"games_played": 4, "points_per_game": 20.0},
            },
            {
                "record_id": "stat-unknown-mt",
                "player_uid": "player:cloud-star-01",
                "player_name": "林启明",
                "season": "2025-2026",
                "match_type": "",
                "scope": "WHOLE_SEASON",
                "team_id": "29140",
                "metrics": {"games_played": 3, "points_per_game": 9.0},
            },
        ]
        tables = build_career_season_stats_tables(
            {"player_uid": "player:cloud-star-01", "player_name": "林启明"},
            records,
        )
        # PLAYOFF (singular) routes to playoffs_rows, NOT regular_season_rows
        assert len(tables["playoffs_rows"]) == 1
        assert tables["playoffs_rows"][0]["record_id"] == "stat-po-singular"
        assert tables["playoffs_career_summary"]["games_played"] == 4

        # Unknown/empty match_type MUST NOT be silently misclassified into regular_season_rows
        reg_ids = [r["record_id"] for r in tables["regular_season_rows"]]
        assert "stat-unknown-mt" not in reg_ids
        assert "stat-po-singular" not in reg_ids
        assert set(reg_ids) == {"stat-reg-2024", "stat-reg-2025"}

        # Multi-row career summary sums all regular rows' games_played: 10 + 5 = 15
        assert tables["regular_career_summary"]["games_played"] == 15


# =============================================================================
# m-5c: wiki_draft_extractor multi-field extraction in the same source file
# =============================================================================


class TestQ2R1M5cDraftExtractorMultiFieldSameFile:
    def test_extract_wiki_candidates_keeps_height_and_birth_date_from_same_file(
        self, tmp_path: Path
    ) -> None:
        from cba_kb.person_registry import PersonProfile, make_person_registry
        from cba_kb.wiki_draft_extractor import extract_wiki_candidates

        registry = make_person_registry(
            [
                PersonProfile(
                    person_uid="PERS-CLOUD-PLAY001",
                    canonical_name="林启明",
                )
            ]
        )
        reg_path = tmp_path / "person_registry.json"
        reg_path.write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")

        sources_dir = tmp_path / "sources"
        sources_dir.mkdir()
        # Two separate sentences in the SAME file, each carrying a distinct bio field
        (sources_dir / "profile_note.md").write_text(
            (
                "# 云海新锐球员资料摘记\n\n"
                "林启明体测显示身高 198cm，臂展出色。\n"
                "公开档案记载林启明于1999年5月12日出生，成长于云海青训体系。\n"
            ),
            encoding="utf-8",
        )

        res = extract_wiki_candidates(
            sources_dir=sources_dir,
            person_registry=reg_path,
        )
        lin_cands = [
            c
            for c in res["bio_profile_candidates"]
            if c.get("person_uid") == "PERS-CLOUD-PLAY001"
        ]
        assert len(lin_cands) >= 1
        merged_fields: Dict[str, Any] = {}
        for c in lin_cands:
            merged_fields.update(c.get("extracted_fields") or {})
        assert merged_fields.get("height_cm") == 198
        assert merged_fields.get("birth_date") == "1999-05-12"


# =============================================================================
# m-6: resolve_cba_team_name prefer_short=True
# =============================================================================


class TestQ2R1M6ResolveTeamNameShort:
    def test_resolve_cba_team_name_prefer_short_returns_concise_chinese_name(self) -> None:
        from cba_kb.aliases import resolve_cba_team_name

        full_name = resolve_cba_team_name("29140", prefer_short=False)
        short_name = resolve_cba_team_name("29140", prefer_short=True)
        assert full_name == "浙江稠州金租"
        assert short_name != full_name
        assert len(short_name) < len(full_name)
        assert short_name.startswith("浙江")
