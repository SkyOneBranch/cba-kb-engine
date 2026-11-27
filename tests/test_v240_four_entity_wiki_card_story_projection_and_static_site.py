"""Frozen red-light acceptance tests for REQ-240 (cba-kb-engine v2.4.0).

Covers P0-1 through P0-5 with 100% offline synthetic fixtures (G5 == 0, G6 == 0):
- P0-1: Four-entity core model (PersonRegistry PERS-* lifetime identity & homonym
        fail-closed, CoachRoleFacets 3-facet institutional discrepancy, StoryRecord
        decoupled occurred_season & 1-to-3 projection, OrganizationProfile).
- P0-2: Wiki-style elastic person cards (card_style="wiki_v240", 0-byte diff on
        card_style="classic"), pure coach omitting player stats table while keeping
        statistics=NOT_APPLICABLE, 4 code-enforced editorial iron rules, and
        Club Panorama Card (build_club_panorama_card + CLI build-club-card).
- P0-3: PUBLIC_SAFE static HTML preview site (build_public_site + CLI
        build-public-site), stdlib-only, fail-closed PUBLIC_SITE_VISIBILITY_VIOLATION
        with 0 partial output, bidirectional links, footnote anchors, Schema.org JSON-LD.
- P0-4: 1-1 <=120-char sentence-attributed brief (compose_verified_brief + CLI
        compose-verified-brief) and 4-hard-case pilot validator
        (verify_shenzhen_pilot_hard_cases).
- P0-5: V215_RELEASE_SPECS["v2.4.0-1"] bound to 3b65bcfcc9365d3f081150f516b9dba679ec965b,
        O-232-01 controlled remote filename rename (CURRENT -> ARCHIVE),
        O-232-03 cloud_state single-bundle allowlist expansion,
        O-232-04 kc_visibility .casefold() + ~~~ fence + [INTERNAL_CLUE_ONLY guard.

Lazy imports inside test bodies guarantee clean pytest collection and differential
falsification on baseline 3b65bcfcc9365d3f081150f516b9dba679ec965b.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

BASELINE_SHA_V240_1 = "3b65bcfcc9365d3f081150f516b9dba679ec965b"


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


def _make_synthetic_person_registry() -> Dict[str, Any]:
    from cba_kb.person_registry import PersonProfile, RoleTenure, make_person_registry

    p_coach = PersonProfile(
        person_uid="PERS-SYNTH-COACH01",
        canonical_name="合成主帅甲",
        aliases=("合成老帅",),
        linked_player_uids=("player:synth-p2c-001",),
        role_tenures=(
            RoleTenure(
                role="PLAYER",
                organization_id="ORG-SYNTH-ALPHA",
                organization_name="合成阿尔法俱乐部",
                season_start="2014-2015",
                season_end="2021-2022",
                authority_scope="LEAGUE_OFFICIAL",
                evidence_refs=("https://example.invalid/reg/2014-2022/p2c-001",),
            ),
            RoleTenure(
                role="HEAD_COACH",
                organization_id="ORG-SYNTH-ALPHA",
                organization_name="合成阿尔法俱乐部",
                season_start="2024-2025",
                season_end=None,
                authority_scope="CLUB_OFFICIAL",
                evidence_refs=("https://example.invalid/club/coach-2024",),
            ),
        ),
        record_links=(
            {"record_key": "staff:synth-2026:coach01", "link_status": "same"},
        ),
    )
    p_homonym_a = PersonProfile(
        person_uid="PERS-SYNTH-HOM0001",
        canonical_name="合成同名丙",
        linked_player_uids=("player:synth-hom-a",),
        role_tenures=(
            RoleTenure(
                role="PLAYER",
                organization_id="ORG-SYNTH-ALPHA",
                organization_name="合成阿尔法俱乐部",
                season_start="2023-2024",
            ),
        ),
        record_links=({"record_key": "rec:synth:hom-a", "link_status": "same"},),
    )
    p_homonym_b = PersonProfile(
        person_uid="PERS-SYNTH-HOM0002",
        canonical_name="合成同名丙",
        linked_player_uids=("player:synth-hom-b",),
        role_tenures=(
            RoleTenure(
                role="PLAYER",
                organization_id="ORG-SYNTH-BETA",
                organization_name="合成贝塔俱乐部",
                season_start="2024-2025",
            ),
        ),
    )
    return make_person_registry([p_coach, p_homonym_a, p_homonym_b])


def _make_synthetic_jersey_story() -> Dict[str, Any]:
    from cba_kb.story_record import make_story_record

    return make_story_record(
        title="合成阿尔法俱乐部9号球衣传承故事",
        occurred_season="2018-2019",
        occurred_date="2018-10-15",
        published_at="2026-09-01T10:00:00Z",
        canonical_source={
            "source_id": "src-synth-jersey-001",
            "title": "合成体育专题：球衣号码的背后",
            "url": "https://example.invalid/media/jersey-story-2026",
            "source_kind": "public_media",
            "authority_scope": "PUBLIC_MEDIA",
        },
        deduped_reprints=[
            {
                "source_id": "src-synth-jersey-reprint-1",
                "url": "https://example.invalid/reprint/jersey-story-2026",
            }
        ],
        participants=[
            {
                "person_uid": "PERS-SYNTH-COACH01",
                "display_name": "合成主帅甲",
                "role": "ACTION_INITIATOR",
                "internal_verbatim_or_note": "内部摘录：合成主帅甲赛后直言把9号留给更年轻的后卫。",
                "public_perspective_summary": "合成主帅甲在2018-2019赛季主动让出9号球衣，象征队内传承。",
            },
            {
                "person_uid": "PERS-SYNTH-PLAY002",
                "display_name": "合成新星乙",
                "role": "SUBJECT_INTERVIEWEE",
                "internal_verbatim_or_note": "内部摘录：合成新星乙回忆接过9号球衣时的紧张与激励。",
                "public_perspective_summary": "合成新星乙在2018-2019赛季接过9号球衣并延续主力轮换角色。",
            },
        ],
        organization_ids=["ORG-SYNTH-ALPHA"],
        visibility_policy={"public_export_allowed": True},
    )


# =============================================================================
# P0-1: Four-Entity Core Model
# =============================================================================


class TestP01FourEntityCoreModel:
    def test_person_registry_lifetime_uid_player_to_coach_unification_and_homonym_fail_closed(
        self,
    ) -> None:
        from cba_kb.actor import validate_person_uid
        from cba_kb.person_registry import (
            PERSON_REGISTRY_SCHEMA,
            resolve_person_identity,
            unify_player_and_coach_profile,
            validate_person_registry,
        )

        registry = _make_synthetic_person_registry()
        validated = validate_person_registry(registry)
        assert validated["schema"] == PERSON_REGISTRY_SCHEMA
        for p in validated["persons"]:
            assert validate_person_uid(p["person_uid"]) == p["person_uid"]

        # Unify player->coach under single lifetime PERS-* profile via player_uid
        res_by_player_uid = resolve_person_identity(
            "合成主帅甲",
            person_registry=validated,
            player_uid="player:synth-p2c-001",
        )
        assert res_by_player_uid["status"] == "RESOLVED"
        assert res_by_player_uid["actor_kind"] == "person"
        assert res_by_player_uid["person_uid"] == "PERS-SYNTH-COACH01"

        unified = unify_player_and_coach_profile(
            "PERS-SYNTH-COACH01", person_registry=validated
        )
        assert unified["person_uid"] == "PERS-SYNTH-COACH01"
        assert "player:synth-p2c-001" in unified["linked_player_uids"]
        assert unified["has_player_tenure"] is True
        assert unified["has_coach_tenure"] is True
        assert unified["is_player_to_coach_transition"] is True
        assert len(unified["role_tenures"]) == 2

        # Homonym without player_uid or explicit record_links MUST fail closed -> UNDECIDED / unresolved
        res_ambig = resolve_person_identity("合成同名丙", person_registry=validated)
        assert res_ambig["status"] == "UNDECIDED"
        assert res_ambig["actor_kind"] == "unresolved"
        assert res_ambig["person_uid"] is None
        assert len(res_ambig["candidates"]) == 2

        # Homonym WITH explicit record_key resolves deterministically
        res_explicit = resolve_person_identity(
            "合成同名丙",
            person_registry=validated,
            record_key="rec:synth:hom-a",
        )
        assert res_explicit["status"] == "RESOLVED"
        assert res_explicit["person_uid"] == "PERS-SYNTH-HOM0001"

    def test_person_registry_rejects_invalid_uid_and_duplicate_linked_player_uid(
        self,
    ) -> None:
        from cba_kb.person_registry import (
            PERSON_REGISTRY_SCHEMA,
            PersonRegistryError,
            validate_person_registry,
        )

        bad_uid_reg = {
            "schema": PERSON_REGISTRY_SCHEMA,
            "persons": [
                {
                    "person_uid": "BAD-ID-123",
                    "canonical_name": "合成人物",
                    "linked_player_uids": [],
                    "role_tenures": [],
                }
            ],
        }
        with pytest.raises((PersonRegistryError, ValueError)):
            validate_person_registry(bad_uid_reg)

        dup_link_reg = {
            "schema": PERSON_REGISTRY_SCHEMA,
            "persons": [
                {
                    "person_uid": "PERS-SYNTH-DUP0001",
                    "canonical_name": "合成甲",
                    "linked_player_uids": ["player:synth-shared"],
                    "role_tenures": [],
                },
                {
                    "person_uid": "PERS-SYNTH-DUP0002",
                    "canonical_name": "合成乙",
                    "linked_player_uids": ["player:synth-shared"],
                    "role_tenures": [],
                },
            ],
        }
        with pytest.raises(PersonRegistryError, match="DUPLICATE_LINKED_PLAYER_UID"):
            validate_person_registry(dup_link_reg)

    def test_coach_role_facets_from_staff_snapshots_and_institutional_discrepancy(
        self,
    ) -> None:
        from cba_kb.role_facets import (
            INSTITUTIONAL_DISCREPANCY_CODE,
            OBSERVED_EMPTY,
            CoachRoleFacets,
            build_coach_role_facets,
        )

        staff_snapshots = [
            {
                "season": "2026-2027",
                "organization_id": "ORG-SYNTH-ALPHA",
                "person_uid": "PERS-SYNTH-COACH01",
                "person_name": "合成主帅甲",
                "league_listed_role": "ASSISTANT_COACH",
                "captured_at": "2026-10-12T08:00:00Z",
                "source_url": "https://example.invalid/cba/staff/2026-2027",
            }
        ]
        facets = build_coach_role_facets(
            person_uid="PERS-SYNTH-COACH01",
            person_name="合成主帅甲",
            organization_id="ORG-SYNTH-ALPHA",
            season="2026-2027",
            staff_snapshots=staff_snapshots,
            club_appointed_role="HEAD_COACH",
            gameday_acting_role=None,
        )
        assert isinstance(facets, CoachRoleFacets)
        assert facets.league_listed_role == "ASSISTANT_COACH"
        assert facets.club_appointed_role == "HEAD_COACH"
        assert facets.gameday_acting_role == OBSERVED_EMPTY
        assert facets.has_institutional_discrepancy is True
        assert facets.discrepancy_code == INSTITUTIONAL_DISCREPANCY_CODE
        assert facets.discrepancy_explanation
        assert "ASSISTANT_COACH" in facets.discrepancy_explanation
        assert "HEAD_COACH" in facets.discrepancy_explanation

        # Aligned roles -> no discrepancy
        aligned = build_coach_role_facets(
            person_uid="PERS-SYNTH-COACH02",
            person_name="合成助教丁",
            organization_id="ORG-SYNTH-ALPHA",
            season="2026-2027",
            league_listed_role="ASSISTANT_COACH",
            club_appointed_role="ASSISTANT_COACH",
            gameday_acting_role="ACTING_HEAD_COACH",
        )
        assert aligned.has_institutional_discrepancy is False
        assert aligned.discrepancy_explanation is None
        assert aligned.gameday_acting_role == "ACTING_HEAD_COACH"

    def test_story_record_schema_decoupled_season_and_one_to_three_projection(
        self,
    ) -> None:
        from cba_kb.story_record import (
            STORY_ID_RE,
            STORY_RECORD_SCHEMA,
            project_stories_for_entity,
            validate_story_record,
        )

        story = _make_synthetic_jersey_story()
        validated = validate_story_record(story)
        assert validated["schema"] == STORY_RECORD_SCHEMA
        assert STORY_ID_RE.fullmatch(validated["story_id"])
        # occurred_season (2018-2019) is explicitly decoupled from published_at (2026-09-01)
        assert validated["occurred_season"] == "2018-2019"
        assert validated["published_at"].startswith("2026-09-01")

        # 1-source-to-3-targets projection: Initiator, Interviewee, Organization
        proj_initiator_pub = project_stories_for_entity(
            [validated],
            entity_id="PERS-SYNTH-COACH01",
            entity_kind="person",
            visibility="public_safe",
        )
        proj_interviewee_int = project_stories_for_entity(
            [validated],
            entity_id="PERS-SYNTH-PLAY002",
            entity_kind="person",
            visibility="internal_full",
        )
        proj_org_pub = project_stories_for_entity(
            [validated],
            entity_id="ORG-SYNTH-ALPHA",
            entity_kind="organization",
            visibility="public_safe",
        )

        assert len(proj_initiator_pub) == 1
        assert proj_initiator_pub[0]["participant_role"] == "ACTION_INITIATOR"
        assert proj_initiator_pub[0]["occurred_season"] == "2018-2019"
        assert "主动让出9号球衣" in proj_initiator_pub[0]["summary"]
        assert "内部摘录" not in json.dumps(proj_initiator_pub, ensure_ascii=False)

        assert len(proj_interviewee_int) == 1
        assert proj_interviewee_int[0]["participant_role"] == "SUBJECT_INTERVIEWEE"
        assert "内部摘录" in (proj_interviewee_int[0].get("internal_verbatim_or_note") or "")

        assert len(proj_org_pub) == 1
        assert proj_org_pub[0]["story_id"] == validated["story_id"]
        assert "内部摘录" not in json.dumps(proj_org_pub, ensure_ascii=False)

    def test_organization_profile_schema_and_serialization(self) -> None:
        from cba_kb.story_record import ORGANIZATION_PROFILE_SCHEMA, OrganizationProfile

        org = OrganizationProfile(
            organization_id="ORG-SYNTH-ALPHA",
            canonical_name="合成阿尔法俱乐部",
            short_name="合成阿尔法",
            aliases=("阿尔法男篮",),
            league="CBA",
            city="合成市",
        )
        d = org.to_dict()
        assert d["schema"] == ORGANIZATION_PROFILE_SCHEMA
        assert d["organization_id"] == "ORG-SYNTH-ALPHA"
        assert d["canonical_name"] == "合成阿尔法俱乐部"


# =============================================================================
# P0-2: Wiki-Style Elastic Cards, Club Panorama Card & 4 Editorial Iron Rules
# =============================================================================


class TestP02WikiCardsClubPanoramaAndEditorialRules:
    def test_classic_card_style_zero_byte_backward_compatibility(self) -> None:
        from cba_kb.kc_render import render_research_card

        legacy_payload: Dict[str, Any] = {
            "metric": "points_per_game",
            "grains": {
                "STATS": {
                    "posture": "CLEAN_OFFICIAL",
                    "season": "2026-2027",
                    "match_type": "REGULAR",
                    "scope": "WHOLE_SEASON",
                    "metric": "points_per_game",
                    "value": 19.4,
                }
            },
            "evidence_transparency": {
                "posture": "CLEAN_OFFICIAL",
                "adopted_value": 19.4,
                "subject_player_uid": "player:synth-legacy-01",
                "scope_meta": {
                    "player_name": "合成兼容球员",
                    "player_id": "SYNTH_L01",
                    "team_name": "合成阿尔法俱乐部",
                    "season": "2026-2027",
                    "match_type": "REGULAR",
                    "scope": "WHOLE_SEASON",
                    "games_played": 30,
                    "captured_at": "2026-11-01T08:00:00Z",
                    "locator": "synth-loc-legacy",
                },
            },
        }
        default_md = render_research_card(legacy_payload, evidence_verified=True)
        classic_md = render_research_card(
            legacy_payload, evidence_verified=True, card_style="classic"
        )
        assert default_md == classic_md

        with pytest.raises(ValueError, match="INVALID_CARD_STYLE"):
            render_research_card(
                legacy_payload, evidence_verified=True, card_style="unknown_style"
            )

    def test_wiki_v240_pure_coach_omits_player_stats_and_renders_role_facets(
        self,
    ) -> None:
        from cba_kb.person_card_builder import build_person_card
        from cba_kb.role_facets import build_coach_role_facets

        facets = build_coach_role_facets(
            person_uid="PERS-SYNTH-PURECOACH",
            person_name="合成纯教练戊",
            organization_id="ORG-SYNTH-ALPHA",
            season="2026-2027",
            league_listed_role="ASSISTANT_COACH",
            club_appointed_role="HEAD_COACH",
            gameday_acting_role=None,
        )
        identity_registry = {
            "schema_version": 1,
            "players": [
                {
                    "player_uid": "player:synth-purecoach",
                    "canonical_name": "合成纯教练戊",
                    "club_official": "合成阿尔法俱乐部",
                    "status": "ACTIVE",
                }
            ],
            "aliases": [],
        }
        res = build_person_card(
            "合成纯教练戊",
            season="2026-2027",
            identity_registry=identity_registry,
            entity_role="HEAD_COACH",
            card_style="wiki_v240",
            coach_role_facets=facets,
            visibility="public_safe",
        )
        assert res["status"] == "BUILT"
        assert res["domain_horizons"]["statistics"]["status"] == "NOT_APPLICABLE"
        md = res["markdown"]
        assert "### Season Averages (Player-Season Grain)" not in md
        assert "## Recent Single-Game Log" not in md
        assert "domain[statistics]: status=NOT_APPLICABLE" in md
        assert "## Coaching Role Facets" in md
        assert "ASSISTANT_COACH" in md
        assert "HEAD_COACH" in md
        assert "OBSERVED_EMPTY" in md
        assert md.rstrip().endswith("end_marker: true")

    def test_editorial_iron_rules_authority_scope_silence_not_cause_and_reprint_dedup(
        self,
    ) -> None:
        from cba_kb.kc_render import (
            dedup_reprints_and_preserve_discrepancies,
            enforce_authority_scope,
            filter_timeline_events_silence_not_cause,
        )

        # Rule 1: authority_scope jurisdiction binding
        scoped = enforce_authority_scope([
            {
                "claim": "CBA官方注册名单确认",
                "authority_scope": "LEAGUE_OFFICIAL",
                "target_slot": "official_registration",
            },
            {
                "claim": "自媒体传言已完成注册",
                "authority_scope": "PUBLIC_MEDIA",
                "target_slot": "official_registration",
            },
        ])
        assert len(scoped["accepted_official"]) == 1
        assert scoped["accepted_official"][0]["authority_scope"] == "LEAGUE_OFFICIAL"
        assert len(scoped["downgraded_or_rejected"]) == 1
        assert scoped["downgraded_or_rejected"][0]["authority_scope"] == "PUBLIC_MEDIA"

        # Rule 2: Silence != Cause (reject unsubstantiated rumor/injury cause; allow multi-source discrepancy)
        filtered = filter_timeline_events_silence_not_cause([
            {
                "event_id": "evt-rumor-1",
                "event_kind": "RUMOR_CAUSE",
                "cause_status": "UNSUBSTANTIATED",
                "summary": "网传因伤缺席大名单",
            },
            {
                "event_id": "evt-u17-discrepancy",
                "event_kind": "MULTI_SOURCE_DISCREPANCY",
                "summary": "合成U17青年队多源名单差异：初选/热身/终选/俱乐部官微存在出入",
                "sources": [
                    {"stage": "candidate_list", "present": True, "url": "https://example.invalid/u17/cand"},
                    {"stage": "warmup_roster", "present": True, "url": "https://example.invalid/u17/warm"},
                    {"stage": "final_roster", "present": False, "url": "https://example.invalid/u17/final"},
                    {"stage": "club_post", "present": True, "url": "https://example.invalid/u17/club"},
                ],
            },
        ])
        assert [e["event_id"] for e in filtered["main_timeline"]] == ["evt-u17-discrepancy"]
        assert [e["event_id"] for e in filtered["rejected_rumor_causes"]] == ["evt-rumor-1"]

        # Rule 3: Same-source reprint dedup + multi-source side-by-side discrepancy
        deduped = dedup_reprints_and_preserve_discrepancies([
            {
                "item_id": "i1",
                "canonical_source_id": "src-A",
                "source_name": "合成官方通告A",
                "claim": "2026-10-01入选12人名单",
            },
            {
                "item_id": "i2",
                "canonical_source_id": "src-A",
                "reprint_of": "src-A",
                "source_name": "转载媒体A1",
                "claim": "2026-10-01入选12人名单",
            },
            {
                "item_id": "i3",
                "canonical_source_id": "src-B",
                "source_name": "合成俱乐部官微B",
                "claim": "2026-10-01随队合练未登场",
            },
        ])
        assert len(deduped["canonical_items"]) == 2
        src_a = next(x for x in deduped["canonical_items"] if x["canonical_source_id"] == "src-A")
        assert len(src_a["deduped_reprints"]) == 1
        assert deduped["has_multi_source_discrepancy"] is True

    def test_unreferenced_encyclopedia_quarantined_internal_and_stripped_public_safe(
        self,
    ) -> None:
        from cba_kb.kc_visibility import verify_public_safe_card_text
        from cba_kb.person_card_builder import build_person_card

        identity_registry = {
            "schema_version": 1,
            "players": [
                {
                    "player_uid": "player:synth-enc-01",
                    "canonical_name": "合成球员己",
                    "club_official": "合成阿尔法俱乐部",
                    "status": "ACTIVE",
                }
            ],
            "aliases": [],
        }
        clues = [
            {
                "source_kind": "unreferenced_encyclopedia",
                "title": "无来源百科词条",
                "summary": "百科未附参考资料称其童年曾获区赛MVP",
                "url": "https://example.invalid/wiki/unreferenced-entry",
            }
        ]
        internal_res = build_person_card(
            "合成球员己",
            season="2026-2027",
            identity_registry=identity_registry,
            card_style="wiki_v240",
            encyclopedia_clues=clues,
            visibility="internal_full",
        )
        assert "[INTERNAL_CLUE_ONLY:" in internal_res["markdown"]
        assert "区赛MVP" in internal_res["markdown"]

        public_res = build_person_card(
            "合成球员己",
            season="2026-2027",
            identity_registry=identity_registry,
            card_style="wiki_v240",
            encyclopedia_clues=clues,
            visibility="public_safe",
        )
        assert "[INTERNAL_CLUE_ONLY" not in public_res["markdown"]
        assert "区赛MVP" not in public_res["markdown"]
        verify_public_safe_card_text(public_res["markdown"])

        with pytest.raises(ValueError, match="PUBLIC_SAFE_REDACTION_VIOLATION"):
            verify_public_safe_card_text(
                "# Card\n- Visibility: PUBLIC_SAFE\n- [INTERNAL_CLUE_ONLY: 未核实百科线索]\nend_marker: true\n"
            )

    def test_build_club_panorama_card_and_cli_build_club_card(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from cba_kb.club_card_builder import build_club_panorama_card
        from cba_kb.role_facets import build_coach_role_facets
        from cba_kb.story_record import OrganizationProfile

        org = OrganizationProfile(
            organization_id="ORG-SYNTH-ALPHA",
            canonical_name="合成阿尔法俱乐部",
            short_name="合成阿尔法",
        )
        master_rows = [
            {
                "season": "2025-2026",
                "club_official": "合成阿尔法俱乐部",
                "player": "合成老将甲",
                "player_uid": "player:synth-001",
                "contract_category": "C类",
                "registration_status": "已注册",
            },
            {
                "season": "2026-2027",
                "club_official": "合成阿尔法俱乐部",
                "player": "合成新星乙",
                "player_uid": "player:synth-002",
                "contract_category": "A1类",
                "registration_status": "已注册",
            },
        ]
        facets = [
            build_coach_role_facets(
                person_uid="PERS-SYNTH-COACH01",
                person_name="合成主帅甲",
                organization_id="ORG-SYNTH-ALPHA",
                season="2026-2027",
                league_listed_role="ASSISTANT_COACH",
                club_appointed_role="HEAD_COACH",
            )
        ]
        stories = [_make_synthetic_jersey_story()]

        res = build_club_panorama_card(
            "合成阿尔法俱乐部",
            seasons=("2025-2026", "2026-2027"),
            organization_profile=org.to_dict(),
            master_rows=master_rows,
            coach_role_facets=facets,
            story_records=stories,
            visibility="public_safe",
            output_dir=tmp_path / "clubs",
            observed_until="2026-11-20T00:00:00Z",
        )
        assert res["status"] == "BUILT"
        assert res["organization_id"] == "ORG-SYNTH-ALPHA"
        assert res["visibility"] == "public_safe"
        assert res["filename"].endswith("_PUBLIC_CURRENT.md")
        assert Path(res["card_path"]).is_file()
        md = res["markdown"]
        assert "Visibility: PUBLIC_SAFE" in md
        assert "2025-2026" in md and "2026-2027" in md
        assert "合成老将甲" in md and "合成新星乙" in md
        assert "合成主帅甲" in md and "ASSISTANT_COACH" in md and "HEAD_COACH" in md
        assert "9号球衣传承故事" in md
        assert "内部摘录" not in md
        assert md.rstrip().endswith("end_marker: true")

        # CLI build-club-card
        org_path = tmp_path / "organization_profiles.json"
        org_path.write_text(json.dumps([org.to_dict()], ensure_ascii=False), encoding="utf-8")
        story_path = tmp_path / "story_records.jsonl"
        story_path.write_text(json.dumps(stories[0], ensure_ascii=False) + "\n", encoding="utf-8")
        cli_out = tmp_path / "cli_clubs"
        code, stdout, stderr = _run_cli(
            monkeypatch,
            capsys,
            [
                "build-club-card",
                "--club",
                "ORG-SYNTH-ALPHA",
                "--seasons",
                "2025-2026,2026-2027",
                "--organization-profiles",
                str(org_path),
                "--story-records",
                str(story_path),
                "--visibility",
                "public_safe",
                "--output-dir",
                str(cli_out),
            ],
        )
        assert code == 0, stderr
        payload = json.loads(stdout)
        assert payload["status"] == "BUILT"
        assert payload["organization_id"] == "ORG-SYNTH-ALPHA"


# =============================================================================
# P0-3: PUBLIC_SAFE Static HTML Preview Site
# =============================================================================


class TestP03PublicSafeStaticSite:
    def test_build_public_site_generates_html_css_bidirectional_links_footnotes_and_jsonld(
        self, tmp_path: Path
    ) -> None:
        from cba_kb.static_site_builder import build_public_site

        club_card = {
            "organization_id": "ORG-SYNTH-ALPHA",
            "club_name": "合成阿尔法俱乐部",
            "visibility": "public_safe",
            "markdown": (
                "# 俱乐部全景卡｜合成阿尔法俱乐部\n"
                "- Visibility: PUBLIC_SAFE\n\n"
                "## 球队概况\n"
                "- 核心人物：合成主帅甲、合成新星乙\n"
                "- 公开来源：https://example.invalid/cba/club/alpha\n\n"
                "end_marker: true\n"
            ),
        }
        person_cards = [
            {
                "person_uid": "PERS-SYNTH-COACH01",
                "person_name": "合成主帅甲",
                "slug": "pers-synth-coach01",
                "visibility": "public_safe",
                "citations": [
                    {
                        "label": "CBA官方教练注册快照",
                        "url": "https://example.invalid/cba/staff/2026",
                    }
                ],
                "markdown": (
                    "# 人物研究卡｜合成主帅甲\n"
                    "- Visibility: PUBLIC_SAFE\n\n"
                    "## 执教概况\n"
                    "- 所属俱乐部：合成阿尔法俱乐部，搭档合成新星乙。\n\n"
                    "end_marker: true\n"
                ),
            },
            {
                "person_uid": "PERS-SYNTH-PLAY002",
                "person_name": "合成新星乙",
                "slug": "pers-synth-play002",
                "visibility": "public_safe",
                "citations": [
                    {
                        "label": "合成体育公开专访",
                        "url": "https://example.invalid/media/interview-02",
                    }
                ],
                "markdown": (
                    "# 人物研究卡｜合成新星乙\n"
                    "- Visibility: PUBLIC_SAFE\n\n"
                    "## 球员概况\n"
                    "- 所属俱乐部：合成阿尔法俱乐部，师从合成主帅甲。\n\n"
                    "end_marker: true\n"
                ),
            },
        ]
        site_dir = tmp_path / "public_site"
        res = build_public_site(
            club_card=club_card,
            person_cards=person_cards,
            output_dir=site_dir,
            site_title="合成阿尔法公开知识站",
            observed_until="2026-11-20T00:00:00Z",
        )
        assert res["status"] == "BUILT"
        index_file = site_dir / "index.html"
        css_file = site_dir / "assets" / "style.css"
        p1_file = site_dir / "persons" / "pers-synth-coach01.html"
        p2_file = site_dir / "persons" / "pers-synth-play002.html"
        assert index_file.is_file()
        assert css_file.is_file()
        assert p1_file.is_file()
        assert p2_file.is_file()

        index_html = index_file.read_text(encoding="utf-8")
        p1_html = p1_file.read_text(encoding="utf-8")

        # Bidirectional entity internal links
        assert "persons/pers-synth-coach01.html" in index_html
        assert "persons/pers-synth-play002.html" in index_html
        assert "../index.html" in p1_html
        assert "pers-synth-play002.html" in p1_html

        # Footnote anchors [1]
        assert "#fn-1" in p1_html
        assert 'id="fn-1"' in p1_html
        assert "[1]" in p1_html

        # Schema.org JSON-LD (SportsTeam / Person + observed_until)
        assert "application/ld+json" in index_html
        assert '"SportsTeam"' in index_html
        assert "2026-11-20T00:00:00Z" in index_html
        assert "application/ld+json" in p1_html
        assert '"Person"' in p1_html
        assert "2026-11-20T00:00:00Z" in p1_html

    def test_build_public_site_fail_closed_zero_partial_output_on_visibility_violation(
        self, tmp_path: Path
    ) -> None:
        from cba_kb.static_site_builder import build_public_site

        clean_club = {
            "organization_id": "ORG-SYNTH-ALPHA",
            "club_name": "合成阿尔法俱乐部",
            "visibility": "public_safe",
            "markdown": "# Club\n- Visibility: PUBLIC_SAFE\nend_marker: true\n",
        }

        # Case A: internal_full card rejected with 0 partial output
        out_a = tmp_path / "site_fail_internal"
        with pytest.raises(ValueError, match="PUBLIC_SITE_VISIBILITY_VIOLATION"):
            build_public_site(
                club_card=clean_club,
                person_cards=[
                    {
                        "person_uid": "PERS-SYNTH-BAD1",
                        "person_name": "合成人物",
                        "visibility": "internal_full",
                        "markdown": "# Internal\nend_marker: true\n",
                    }
                ],
                output_dir=out_a,
            )
        assert not out_a.exists() or list(out_a.rglob("*")) == []

        # Case B: public_safe labeled card containing [INTERNAL_CLUE_ONLY] or blockquote -> rejected with 0 partial output
        out_b = tmp_path / "site_fail_clue"
        with pytest.raises(ValueError, match="PUBLIC_SITE_VISIBILITY_VIOLATION"):
            build_public_site(
                club_card=clean_club,
                person_cards=[
                    {
                        "person_uid": "PERS-SYNTH-BAD2",
                        "person_name": "合成人物二",
                        "visibility": "public_safe",
                        "markdown": (
                            "# Card\n- Visibility: PUBLIC_SAFE\n"
                            "- [INTERNAL_CLUE_ONLY: 未核实线索]\nend_marker: true\n"
                        ),
                    }
                ],
                output_dir=out_b,
            )
        assert not out_b.exists() or list(out_b.rglob("*")) == []

    def test_cli_build_public_site_success_and_fail_closed_exit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        club_json = tmp_path / "club_card.json"
        club_json.write_text(
            json.dumps(
                {
                    "organization_id": "ORG-SYNTH-ALPHA",
                    "club_name": "合成阿尔法俱乐部",
                    "visibility": "public_safe",
                    "markdown": "# 合成阿尔法俱乐部\n- Visibility: PUBLIC_SAFE\nend_marker: true\n",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        persons_dir = tmp_path / "person_cards"
        persons_dir.mkdir()
        (persons_dir / "p1.json").write_text(
            json.dumps(
                {
                    "person_uid": "PERS-SYNTH-P1",
                    "person_name": "合成新星乙",
                    "slug": "pers-synth-p1",
                    "visibility": "public_safe",
                    "markdown": "# 合成新星乙\n- Visibility: PUBLIC_SAFE\nend_marker: true\n",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        out_dir = tmp_path / "cli_site"
        code, stdout, stderr = _run_cli(
            monkeypatch,
            capsys,
            [
                "build-public-site",
                "--club-card",
                str(club_json),
                "--person-cards-dir",
                str(persons_dir),
                "--output-dir",
                str(out_dir),
                "--observed-until",
                "2026-11-20T00:00:00Z",
            ],
        )
        assert code == 0, stderr
        payload = json.loads(stdout)
        assert payload["status"] == "BUILT"
        assert (out_dir / "index.html").is_file()


# =============================================================================
# P0-4: 1-1 <=120-Char Sentence-Attributed Brief & 4 Hard-Cases Validator
# =============================================================================


class TestP04EditorialBriefAndPilotHardCases:
    def test_compose_verified_brief_120_char_limit_attribution_and_gap_exclusion(
        self,
    ) -> None:
        from cba_kb.editorial_brief import MAX_BRIEF_CHARS, compose_verified_brief
        from cba_kb.kc_visibility import verify_public_safe_card_text

        assert MAX_BRIEF_CHARS == 120
        facts = [
            {
                "sentence": "合成新星乙于2026-2027赛季注册于合成阿尔法俱乐部。",
                "status": "VERIFIED_PRESENT",
                "source_kind": "official_registration",
                "citation_label": "CBA官方注册公示",
                "source_url": "https://example.invalid/cba/reg/2026",
                "authority_scope": "LEAGUE_OFFICIAL",
            },
            {
                "sentence": "2025-2026赛季常规赛场均16.8分4.2助攻。",
                "status": "VERIFIED_PRESENT",
                "source_kind": "official_stats",
                "citation_label": "CBA官方技术统计",
                "source_url": "https://example.invalid/cba/stats/2025",
                "authority_scope": "LEAGUE_OFFICIAL",
            },
            {
                "sentence": "本赛季单场数据暂缺。",
                "status": "SOURCE_GAP_UNKNOWN",
                "source_kind": "official_stats",
                "citation_label": "数据源缺口",
                "source_url": None,
            },
            {
                "sentence": "同名人物尚未裁决归属。",
                "status": "UNDECIDED",
                "source_kind": "public_media",
                "citation_label": "待核线索",
                "source_url": "https://example.invalid/ambig",
            },
            {
                "sentence": "无来源百科称其曾获校园扣篮王。",
                "status": "VERIFIED_PRESENT",
                "source_kind": "unreferenced_encyclopedia",
                "citation_label": "百科词条",
                "source_url": "https://example.invalid/wiki/unverified",
            },
        ]
        brief = compose_verified_brief(
            facts, entity_name="合成新星乙", max_chars=120, visibility="public_safe"
        )
        assert len(brief["body"]) <= 120
        assert brief["char_count"] == len(brief["body"])
        assert brief["excluded_count"] == 3
        assert len(brief["sentence_attributions"]) == 2
        assert "暂缺" not in brief["body"]
        assert "尚未裁决" not in brief["body"]
        assert "扣篮王" not in brief["body"]
        for attr in brief["sentence_attributions"]:
            assert attr["sentence"] in brief["body"]
            assert attr["citation_label"]
        verify_public_safe_card_text(brief["body"])

    def test_cli_compose_verified_brief(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        facts_path = tmp_path / "facts.json"
        facts_path.write_text(
            json.dumps(
                [
                    {
                        "sentence": "合成主帅甲2026-2027赛季执教合成阿尔法俱乐部。",
                        "status": "VERIFIED_PRESENT",
                        "source_kind": "official_registration",
                        "citation_label": "俱乐部官宣",
                        "source_url": "https://example.invalid/club/coach",
                        "authority_scope": "CLUB_OFFICIAL",
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        code, stdout, stderr = _run_cli(
            monkeypatch,
            capsys,
            [
                "compose-verified-brief",
                "--entity",
                "合成主帅甲",
                "--facts",
                str(facts_path),
                "--max-chars",
                "120",
                "--visibility",
                "public_safe",
            ],
        )
        assert code == 0, stderr
        payload = json.loads(stdout)
        assert payload["char_count"] <= 120
        assert len(payload["sentence_attributions"]) == 1

    def test_verify_shenzhen_pilot_hard_cases_passes_valid_and_rejects_tampered(
        self,
    ) -> None:
        from cba_kb.editorial_brief import verify_shenzhen_pilot_hard_cases
        from cba_kb.kc_render import filter_timeline_events_silence_not_cause
        from cba_kb.person_registry import resolve_person_identity, unify_player_and_coach_profile
        from cba_kb.role_facets import build_coach_role_facets
        from cba_kb.story_record import project_stories_for_entity

        registry = _make_synthetic_person_registry()
        facets = build_coach_role_facets(
            person_uid="PERS-SYNTH-COACH01",
            person_name="合成主帅甲",
            organization_id="ORG-SYNTH-ALPHA",
            season="2026-2027",
            league_listed_role="ASSISTANT_COACH",
            club_appointed_role="HEAD_COACH",
            gameday_acting_role=None,
        )
        story = _make_synthetic_jersey_story()
        filtered = filter_timeline_events_silence_not_cause([
            {
                "event_id": "evt-rumor",
                "event_kind": "RUMOR_CAUSE",
                "cause_status": "UNSUBSTANTIATED",
                "summary": "网传因伤落选",
            },
            {
                "event_id": "evt-u17",
                "event_kind": "MULTI_SOURCE_DISCREPANCY",
                "summary": "合成U17多源名单差异",
                "sources": [
                    {"stage": "candidate_list", "present": True},
                    {"stage": "final_roster", "present": False},
                ],
            },
        ])
        valid_bundle = {
            "case_1_coach_role_discrepancy": {
                "coach_role_facets": facets.to_dict(),
                "statistics_domain_status": "NOT_APPLICABLE",
                "has_player_stats_table": False,
            },
            "case_2_player_to_coach_unified": {
                "unified_profile": unify_player_and_coach_profile(
                    "PERS-SYNTH-COACH01", person_registry=registry
                ),
                "homonym_resolution": resolve_person_identity(
                    "合成同名丙", person_registry=registry
                ),
            },
            "case_3_story_one_to_three_projection": {
                "story": story,
                "person_a_public": project_stories_for_entity(
                    [story], entity_id="PERS-SYNTH-COACH01", entity_kind="person", visibility="public_safe"
                ),
                "person_b_public": project_stories_for_entity(
                    [story], entity_id="PERS-SYNTH-PLAY002", entity_kind="person", visibility="public_safe"
                ),
                "org_public": project_stories_for_entity(
                    [story], entity_id="ORG-SYNTH-ALPHA", entity_kind="organization", visibility="public_safe"
                ),
            },
            "case_4_multi_source_discrepancy_and_silence_not_cause": {
                "filtered_timeline": filtered,
                "internal_markdown": "# Card\n- [INTERNAL_CLUE_ONLY: 无来源百科线索]\nend_marker: true\n",
                "public_markdown": "# Card\n- Visibility: PUBLIC_SAFE\nend_marker: true\n",
            },
        }
        report = verify_shenzhen_pilot_hard_cases(valid_bundle)
        assert report["status"] == "PASS"
        assert len(report["passed_cases"]) == 4
        assert report["failed_cases"] == []

        # Tampered Case 1: pure coach erroneously includes player stats table -> must fail closed
        tampered = json.loads(json.dumps(valid_bundle))
        tampered["case_1_coach_role_discrepancy"]["has_player_stats_table"] = True
        with pytest.raises(ValueError, match="SHENZHEN_PILOT_HARD_CASE_FAILED"):
            verify_shenzhen_pilot_hard_cases(tampered)

        # Tampered Case 4: public markdown leaks [INTERNAL_CLUE_ONLY -> must fail closed
        tampered_leak = json.loads(json.dumps(valid_bundle))
        tampered_leak["case_4_multi_source_discrepancy_and_silence_not_cause"]["public_markdown"] = (
            "# Card\n- Visibility: PUBLIC_SAFE\n- [INTERNAL_CLUE_ONLY: leak]\nend_marker: true\n"
        )
        with pytest.raises(ValueError, match="SHENZHEN_PILOT_HARD_CASE_FAILED"):
            verify_shenzhen_pilot_hard_cases(tampered_leak)


# =============================================================================
# P0-5: v2.4.0-1 Release Spec & O-232-01 / O-232-03 / O-232-04 Closure
# =============================================================================


class TestP05ReleaseSpecAndO232Closure:
    def test_v240_1_release_spec_bound_to_baseline_and_preserves_history(self) -> None:
        from cba_kb.orchestration.release import prepare_v215

        assert getattr(prepare_v215, "V240_1_BASELINE_SHA", None) == BASELINE_SHA_V240_1
        assert "v2.4.0-1" in prepare_v215.V215_RELEASE_SPECS
        spec = prepare_v215.V215_RELEASE_SPECS["v2.4.0-1"]
        assert spec["product_baseline_sha"] == BASELINE_SHA_V240_1
        assert spec["enable_v240_shenzhen_narrative_pilot"] is True
        assert spec["enable_v232_card_pipeline"] is True
        assert spec["incremental_delta_only"] is True
        assert spec["archive_format"] == "bundle_v1"
        assert spec["active_league_season"] == "2026-2027"

        # Historical specs preserved without v2.4.0 pilot flag
        for old_rel in ("v2.1.5-1", "v2.2.0-1", "v2.2.1-1", "v2.2.2-1", "v2.3.0-1", "v2.3.1-1", "v2.3.2-1"):
            assert old_rel in prepare_v215.V215_RELEASE_SPECS
            assert not prepare_v215.V215_RELEASE_SPECS[old_rel].get(
                "enable_v240_shenzhen_narrative_pilot", False
            )

    def test_o232_01_controlled_remote_filename_rename_in_project_publish_and_verify(
        self, tmp_path: Path
    ) -> None:
        from cba_kb.common import digest
        from cba_kb.orchestration.release.prepare_v215 import project_targets
        from cba_kb.release import prepare, publish, verify

        old_name = "研究卡_合成球员甲_合成阿尔法_2025-2026_CURRENT.md"
        new_name = "研究卡_合成球员甲_合成阿尔法_2025-2026_ARCHIVE.md"
        old_bytes = b"# Card\nend_marker: true\n"
        new_bytes = (
            "> [HISTORICAL_SEASON_ARCHIVE] 2025-2026\n\n# Card\nend_marker: true\n"
        ).encode("utf-8")
        old_sha = digest(old_bytes)
        new_sha = digest(new_bytes)

        # 1. project_targets allows controlled rename of existing non-protected consumer logical_key
        previous_status = {
            "state": "COMPLETE",
            "current_release_id": "v2.3.2-1",
            "artifacts": [
                {"id": "f_synth_card01", "name": old_name, "sha256": old_sha},
                {"id": "f_synth_code01", "name": "src__a.py", "sha256": _synth_sha("code")},
            ],
        }
        previous_targets = {
            "f_synth_card01": {
                "mime": "text/markdown",
                "mode": "binary",
                "publish_parent": "p_synth_data",
            },
            "f_synth_code01": {
                "mime": "text/plain",
                "mode": "binary",
                "publish_parent": "p_synth_code",
            },
        }
        manifest_rows = [
            {"drive_file_id": "f_synth_card01", "uid": "research/card_synth_01"},
            {"drive_file_id": "f_synth_code01", "uid": "code/src/a.py"},
        ]
        proj = project_targets(
            release_id="v2.4.0-1",
            engine_sha=BASELINE_SHA_V240_1,
            status_id="f_synth_status",
            archive_id="p_synth_archive",
            previous_status=previous_status,
            previous_targets=previous_targets,
            manifest_rows=manifest_rows,
            tracked=["src/a.py"],
            tracked_hashes={"src/a.py": _synth_sha("code")},
            code_parent="p_synth_code",
            declared_consumer_targets={
                "research/card_synth_01": {
                    "name": new_name,
                    "mime": "text/markdown",
                    "content": new_bytes,
                    "publish_parent": "p_synth_data",
                }
            },
        )
        card_target = next(
            t for t in proj["existing_targets"] if t["logical_key"] == "research/card_synth_01"
        )
        assert card_target["name"] == new_name
        assert card_target.get("rename_from") == old_name
        assert card_target["disposition"] == "modified"

        # 2. publish() + verify() execute and verify remote rename + sync release_status.json
        class _FakeDrive:
            def __init__(self) -> None:
                self.files: Dict[str, Dict[str, Any]] = {
                    "f_synth_card01": {
                        "id": "f_synth_card01",
                        "name": old_name,
                        "mimeType": "text/markdown",
                        "parents": ["p_synth_data"],
                        "version": "1",
                        "modifiedTime": "2026-11-01T00:00:00Z",
                        "content": old_bytes,
                    },
                    "f_synth_status": {
                        "id": "f_synth_status",
                        "name": "release_status.json",
                        "mimeType": "application/json",
                        "parents": ["p_synth_data"],
                        "version": "1",
                        "modifiedTime": "2026-11-01T00:00:00Z",
                        "content": json.dumps(
                            {
                                "state": "COMPLETE",
                                "current_release_id": "v2.3.2-1",
                                "artifacts": [
                                    {"id": "f_synth_card01", "name": old_name, "sha256": old_sha}
                                ],
                            },
                            sort_keys=True,
                        ).encode("utf-8"),
                    },
                }
                self.renamed: List[tuple[str, str]] = []
                self._copies: Dict[str, str] = {}

            def meta(self, fid: str) -> Dict[str, Any]:
                f = self.files[fid]
                return {k: v for k, v in f.items() if k != "content"}

            def get(self, fid: str) -> bytes:
                return self.files[fid]["content"]

            def get_with_meta(self, fid: str, *, known_meta=None):
                return self.get(fid), self.meta(fid)

            def put(self, fid: str, data: bytes, mime: str):
                f = self.files[fid]
                f["content"] = bytes(data)
                f["version"] = str(int(f["version"]) + 1)
                return self.meta(fid)

            def rename(self, fid: str, new_filename: str):
                f = self.files[fid]
                f["name"] = new_filename
                f["version"] = str(int(f["version"]) + 1)
                self.renamed.append((fid, new_filename))
                return self.meta(fid)

            def list(self, parent: str):
                return []

            def index_cba_keys(self, parent: str):
                return {}

            def ensure(self, parent: str, key: str, name: str, mime: str, content=None, *, index=None):
                cid = f"f_synth_arc_{len(self._copies) + 1}"
                self._copies[key] = cid
                self.files[cid] = {
                    "id": cid,
                    "name": name,
                    "mimeType": mime,
                    "parents": [parent],
                    "version": "1",
                    "modifiedTime": "2026-11-01T00:00:00Z",
                    "content": bytes(content) if content is not None else b"",
                }
                return cid

            def ensure_copy(self, parent: str, key: str, fid: str, name: str, *, index=None):
                cid = f"f_synth_copy_{len(self._copies) + 1}"
                self._copies[key] = cid
                src = self.files[fid]
                self.files[cid] = {
                    "id": cid,
                    "name": name,
                    "mimeType": src["mimeType"],
                    "parents": [parent],
                    "version": "1",
                    "modifiedTime": "2026-11-01T00:00:00Z",
                    "content": bytes(src["content"]),
                }
                return cid

            def move(self, fid: str, dest: str, prev: str):
                self.files[fid]["parents"] = [dest]
                return self.meta(fid)

        drive = _FakeDrive()
        cand_file = tmp_path / "cand.md"
        cand_file.write_bytes(new_bytes)
        outbox = tmp_path / "outbox_v240"
        prepare(
            drive,
            outbox,
            "v2.4.0-1",
            [
                {
                    "id": "f_synth_card01",
                    "name": new_name,
                    "rename_from": old_name,
                    "mime": "text/markdown",
                    "path": str(cand_file),
                    "publish_parent": "p_synth_data",
                }
            ],
            "p_synth_archive",
            "f_synth_status",
            carry_forward_artifacts=True,
            environment="sandbox",
        )
        publish(drive, outbox, single_writer=True)
        assert ("f_synth_card01", new_name) in drive.renamed
        assert drive.files["f_synth_card01"]["name"] == new_name
        status_after = json.loads(drive.get("f_synth_status").decode("utf-8"))
        assert status_after["artifacts"][0]["name"] == new_name
        assert status_after["artifacts"][0]["sha256"] == new_sha
        verify_res = verify(drive, outbox)
        assert verify_res["verified"] == 1

        # Tamper remote name back to old_name -> verify() must fail closed
        drive.files["f_synth_card01"]["name"] = old_name
        with pytest.raises(RuntimeError, match="Remote verification failed"):
            verify(drive, outbox)

    def test_o232_03_cloud_state_bundle_allowlist_v240_files_and_raw_block(self) -> None:
        from cba_kb.cloud_state import (
            _bundle_allowlisted_exact_names,
            _is_bundle_member_allowed,
        )

        exact = _bundle_allowlisted_exact_names()
        for required_name in (
            "team_staff_snapshots.jsonl",
            "person_registry.json",
            "story_records.jsonl",
            "organization_profiles.json",
        ):
            assert required_name in exact
            assert _is_bundle_member_allowed(required_name, exact) is True
            assert _is_bundle_member_allowed(f"data/{required_name}", exact) is True

        assert _is_bundle_member_allowed("clubs/俱乐部全景卡_合成阿尔法_CURRENT.md", exact) is True

        # Fail-closed blocks remain enforced
        assert _is_bundle_member_allowed("document_lane/archive/raw/clip.html", exact) is False
        assert _is_bundle_member_allowed("production.json", exact) is False
        assert _is_bundle_member_allowed(".credentials/token.json", exact) is False
        assert _is_bundle_member_allowed("evidence/secret.json", exact) is False

    def test_o232_04_kc_visibility_casefold_doc_id_and_tilde_fence_and_clue_guard(
        self,
    ) -> None:
        from cba_kb.kc_render import render_research_card
        from cba_kb.kc_visibility import verify_public_safe_card_text

        # 1. ~~~ fenced block blocked by verify_public_safe_card_text
        with pytest.raises(ValueError, match="PUBLIC_SAFE_REDACTION_VIOLATION"):
            verify_public_safe_card_text(
                "# Card\n- Visibility: PUBLIC_SAFE\n~~~text\nverbatim\n~~~\nend_marker: true\n"
            )

        # 2. Case-variant title == doc_id stripped via .casefold() under public_safe
        payload: Dict[str, Any] = {
            "metric": "points_per_game",
            "grains": {"STATS": {"posture": "UNKNOWN", "season": "2026-2027"}},
            "evidence_transparency": {
                "posture": "UNKNOWN",
                "unknown_subkind": "NO_SEASON_STATS_RECORDED_YET",
                "scope_meta": {"player_name": "合成球员庚", "season": "2026-2027"},
            },
            "external_citations": [
                {
                    "doc_id": "doc:Synth-Case-001",
                    "title": "DOC:synth-case-001",
                    "source_tier": "T1",
                    "completeness": "full",
                    "published_at": "2026-10-01",
                    "canonical_url": "https://example.invalid/cba/casefold-check",
                }
            ],
        }
        pub_md = render_research_card(payload, evidence_verified=True, visibility="public_safe")
        assert "doc:synth-case-001" not in pub_md.lower()
        verify_public_safe_card_text(pub_md)
