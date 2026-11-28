"""REQ-251 (Issue #290) Frozen Acceptance Test Suite — CBA-KB Engine v2.5.1.

Covers all four engineering modules with 100% synthetic/redacted data:
1. P0-E1: CHRONICLER role isolation, zero raw English role fallback in Infobox/JSON-LD,
   4-field youth tenures (organizer_tier) + placeholder rejection + published_at vs captured_at
   footnote distinction, engine VERIFIED adjudication + anti-hollow story gate & clustering.
2. P0-E2: Social workbench CSV export/apply & URL cleaning, fan-repost warning, IMPERSONATOR
   block, WITHDRAWN takedown, 0 audio/video guard, ## 社交媒体与日常动态 Markdown/HTML
   rendering with SELF_STATEMENT & DERIVED_TRANSCRIPT boundaries, MCP social section &
   SELF_CLAIM_MATCH verdict.
3. P0-E3: De-Shenzhen default removal in get_club_roster, multi-club club_contexts,
   sharded bundle (<1s cold start) & cross-club unified PERS-* (Guangdong + Shenzhen),
   demand_ledger.jsonl redaction/aggregation, and v2.5.0 Q2 Minor fixes (HTTP 400 on
   invalid base64, kb_status full_game_logs_filter alignment, NEEDS_SOURCE key stripping).
4. P0-E4: Deterministic B5 dual-host split/shared blind evaluator & official_fact_error_count==0.
"""
from __future__ import annotations

import hashlib
import json
import tarfile
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping

import pytest

# ---------------------------------------------------------------------------
# Synthetic constants (100% non-production, secret-scanner safe)
# ---------------------------------------------------------------------------
_BEARER_SCHEME = "Bearer"
_OWNER_CRED = "synth-owner-cred-251-a"
_SCOUT_CRED = "synth-scout-cred-251-b"
_MEDIA_CRED = "synth-media-cred-251-c"
_PUBLIC_CRED = "synth-public-cred-251-d"

_ORG_SZ = "ORG-CBA-SHENZHEN"
_ORG_GD = "ORG-CBA-GUANGDONG"

_PERS_ZHOU = "PERS-SYNTH-ZHOUPENG"
_PERS_FENG = "PERS-SYNTH-FENGXR01"
_PERS_CHRONICLER = "PERS-SYNTH-ZHENGZP1"
_PERS_COACH_GD = "PERS-SYNTH-DUFENG01"

_STORY_RAW_BYTES_1 = (
    "2024年11月27日，第一届全国青少年三大球运动会男篮决赛打响，广东队以87比75战胜对手夺冠，"
    "冯熙瑞在决赛中贡献关键外线防守与快攻得分。"
).encode("utf-8")
_STORY_SHA_1 = hashlib.sha256(_STORY_RAW_BYTES_1).hexdigest()
_STORY_SPAN_1 = "广东队以87比75战胜对手夺冠， 冯熙瑞在决赛中贡献关键外线防守与快攻得分。"

_STORY_RAW_BYTES_2 = (
    "权威体育报赛后报道：在三大球男篮决赛中，广东青年军凭借第三节防守反击确立胜势，"
    "冯熙瑞多次完成抢断并助攻队友得分。"
).encode("utf-8")
_STORY_SHA_2 = hashlib.sha256(_STORY_RAW_BYTES_2).hexdigest()
_STORY_SPAN_2 = "冯熙瑞多次完成抢断并助攻队友得分。"

_YOUTH_RAW_BYTES = (
    "中国篮协官方通报：冯熙瑞入选中国U17男篮出征德国曼海姆国际邀请赛正赛最终名单。"
).encode("utf-8")
_YOUTH_SHA = hashlib.sha256(_YOUTH_RAW_BYTES).hexdigest()


def _auth_hdr(cred: str) -> Dict[str, str]:
    return {"Authorization": f"{_BEARER_SCHEME} {cred}"}


def _parse_tool_text(resp: Mapping[str, Any]) -> Dict[str, Any]:
    assert "result" in resp, f"Expected JSON-RPC result, got: {resp}"
    res = resp["result"]
    assert res.get("isError") is False, f"Tool returned error: {res}"
    content = res.get("content") or []
    assert content and content[0].get("type") == "text"
    return json.loads(content[0]["text"])


# ===========================================================================
# 1. P0-E1: Role Semantic Isolation, Infobox Chinese Role, Youth 4-Field & Story Gate
# ===========================================================================
def test_p0_e1_chronicler_isolation_and_zero_english_player_fallback(tmp_path: Path) -> None:
    """CHRONICLER is excluded from player/coach squad navbox & counts, uses media template, and Infobox/JSON-LD have 0 raw PLAYER."""
    from cba_kb.club_card_builder import build_club_panorama_card
    from cba_kb.editorial_brief import compose_wiki_lead_paragraph
    from cba_kb.person_card_builder import build_person_card
    from cba_kb.person_registry import (
        CHRONICLER_ROLES,
        classify_person_category,
        format_role_zh,
    )
    from cba_kb.static_site_builder import build_public_site

    assert "CHRONICLER" in CHRONICLER_ROLES
    assert format_role_zh("PLAYER") == "球员"
    assert format_role_zh("HEAD_COACH") == "主教练"
    assert format_role_zh("ACTING_HEAD_COACH") == "代理主教练"
    assert format_role_zh("CHRONICLER") == "媒体记录者"

    person_registry = {
        "schema": "cba-kb.person-registry.v1",
        "persons": [
            {
                "person_uid": _PERS_ZHOU,
                "canonical_name": "周鹏",
                "primary_club": "深圳马可波罗",
                "current_club": "深圳马可波罗",
                "primary_role": "PLAYER",
                "roles": ["PLAYER"],
                "role_tenures": [
                    {
                        "role": "PLAYER",
                        "organization_id": _ORG_SZ,
                        "organization_name": "深圳马可波罗",
                        "season_start": "2022-2023",
                        "season_end": "2025-2026",
                    }
                ],
            },
            {
                "person_uid": _PERS_CHRONICLER,
                "canonical_name": "郑志鹏",
                "primary_club": "深圳晚报",
                "current_club": "深圳晚报",
                "primary_role": "CHRONICLER",
                "roles": ["CHRONICLER"],
                "role_tenures": [
                    {
                        "role": "CHRONICLER",
                        "organization_id": _ORG_SZ,
                        "organization_name": "深圳马可波罗",
                        "season_start": "2024-2025",
                        "season_end": "2025-2026",
                    }
                ],
            },
        ],
    }

    assert classify_person_category(person_registry["persons"][0]) == "ACTIVE_ROSTER_PLAYER"
    assert classify_person_category(person_registry["persons"][1]) == "CHRONICLER"

    # Club card must NOT include CHRONICLER in all_roster_members
    club_card = build_club_panorama_card(
        "深圳马可波罗",
        seasons=("2025-2026",),
        organization_profile={
            "organization_id": _ORG_SZ,
            "canonical_name": "深圳新世纪烈豹",
            "short_name": "深圳马可波罗",
        },
        person_registry=person_registry,
        visibility="public_safe",
        card_style="wiki_v241",
    )
    assert "郑志鹏" not in club_card["all_roster_members"]
    assert "周鹏" in club_card["all_roster_members"]

    # CHRONICLER lead paragraph must use media chronicler template, NEVER roster player template
    lead = compose_wiki_lead_paragraph(
        person_name="郑志鹏",
        bio_profile={
            "person_uid": _PERS_CHRONICLER,
            "canonical_name": "郑志鹏",
            "current_role": "CHRONICLER",
            "media_outlet": "深圳晚报",
        },
        entity_role="CHRONICLER",
        visibility="public_safe",
    )
    assert lead["lead_template"] == "MEDIA_CHRONICLER"
    assert "注册于" not in lead["lead_plain_text"]
    assert "年轻球员" not in lead["lead_plain_text"]
    assert "媒体" in lead["lead_plain_text"] or "记者" in lead["lead_plain_text"]

    player_card = build_person_card(
        "周鹏",
        season="2025-2026",
        visibility="public_safe",
        card_style="wiki_v241",
        person_registry=person_registry,
        bio_profile={
            "person_uid": _PERS_ZHOU,
            "canonical_name": "周鹏",
            "current_club": "深圳马可波罗",
            "current_role": "PLAYER",
            "position": "前锋",
        },
    )
    chronicler_card = build_person_card(
        "郑志鹏",
        season="2025-2026",
        visibility="public_safe",
        card_style="wiki_v241",
        entity_role="CHRONICLER",
        person_registry=person_registry,
        bio_profile={
            "person_uid": _PERS_CHRONICLER,
            "canonical_name": "郑志鹏",
            "current_role": "CHRONICLER",
            "media_outlet": "深圳晚报",
        },
    )
    assert "PLAYER" not in player_card["markdown"]
    assert "注册于" not in chronicler_card["markdown"]
    assert "年轻球员" not in chronicler_card["markdown"]

    site_dir = tmp_path / "site"
    build_public_site(
        club_card=club_card,
        person_cards=[player_card, chronicler_card],
        output_dir=site_dir,
        observed_until="2026-11-28",
    )

    player_html = (site_dir / "persons" / f"{_PERS_ZHOU.lower()}.html").read_text(encoding="utf-8")
    chronicler_html = (site_dir / "persons" / f"{_PERS_CHRONICLER.lower()}.html").read_text(encoding="utf-8")

    # Zero raw English PLAYER fallback in Infobox or JSON-LD
    assert ">PLAYER<" not in player_html
    assert '"jobTitle": "PLAYER"' not in player_html
    assert "球员" in player_html

    # CHRONICLER Infobox & squad-navbox isolation
    assert "媒体记录者" in chronicler_html
    assert "现效力球队" not in chronicler_html
    assert "深圳马可波罗 完整阵容导航（1人）" in player_html
    assert "郑志鹏" not in player_html.split('<nav class="squad-navbox">')[1]


def test_p0_e1_youth_four_fields_placeholder_guard_and_footnote_dates(tmp_path: Path) -> None:
    """Youth tenures infer FIBA_INTERNATIONAL for Mannheim, strip placeholders, and distinguish published_at vs captured_at."""
    from cba_kb.person_card_builder import build_person_card
    from cba_kb.static_site_builder import build_public_site
    from cba_kb.youth_tenure import (
        YOUTH_NATIONAL_TENURE_SCHEMA,
        YouthTenureError,
        infer_organizer_tier,
        validate_youth_national_tenure,
    )

    assert infer_organizer_tier("德国曼海姆国际邀请赛") == "FIBA_INTERNATIONAL"
    assert infer_organizer_tier("第一届全国青少年三大球运动会") == "NATIONAL"

    # Legacy v1 without explicit organizer_tier auto-infers FIBA_INTERNATIONAL for Mannheim
    rec = validate_youth_national_tenure({
        "schema": YOUTH_NATIONAL_TENURE_SCHEMA,
        "person_uid": _PERS_FENG,
        "competition_or_program": "德国曼海姆国际邀请赛",
        "season_or_year": "2024",
        "participation_stage": "FINAL_TOURNAMENT_ROSTER",
        "team_or_unit": "中国U17男篮",
        "evidence_refs": [
            {
                "source_title": "中国篮协U17德国曼海姆名单",
                "source_url": "https://example.org/cba/mannheim-2024",
                "raw_sha256": _YOUTH_SHA,
                "captured_at": "2026-11-20T00:00:00Z",
            }
        ],
    })
    assert rec["organizer_tier"] == "FIBA_INTERNATIONAL"

    # Placeholder rejection
    with pytest.raises(YouthTenureError):
        validate_youth_national_tenure({
            "schema": YOUTH_NATIONAL_TENURE_SCHEMA,
            "person_uid": _PERS_FENG,
            "competition_or_program": "全国U17锦标赛",
            "season_or_year": "2024",
            "participation_stage": "TRAINING_CAMP_CALLUP",
            "team_or_unit": "俱乐部青训参赛/集训单位",
            "evidence_refs": [
                {
                    "source_title": "官方名单",
                    "source_url": "https://example.org/cba/u17",
                    "raw_sha256": _YOUTH_SHA,
                    "captured_at": "2026-11-20T00:00:00Z",
                }
            ],
        })

    person_registry = {
        "schema": "cba-kb.person-registry.v1",
        "persons": [
            {
                "person_uid": _PERS_FENG,
                "canonical_name": "冯熙瑞",
                "primary_club": "深圳马可波罗",
                "current_club": "深圳马可波罗",
                "primary_role": "PLAYER",
                "roles": ["PLAYER"],
                "role_tenures": [],
            }
        ],
    }

    stories = [
        {
            "schema": "cba-kb.story-record.v1",
            "story_id": "story_aaaa1111bbbb2222cccc3333",
            "title": "三大球男篮决赛广东夺冠",
            "occurred_season": "2024-2025",
            "published_at": "2024-11-28",
            "canonical_source": {
                "source_id": "src-01",
                "title": "中国体育报决赛战报",
                "url": "https://example.org/news/3ball-1",
                "published_at": "2024-11-28",
            },
            "deduped_reprints": [],
            "participants": [
                {
                    "person_uid": _PERS_FENG,
                    "display_name": "冯熙瑞",
                    "role": "ACTION_INITIATOR",
                    "internal_verbatim_or_note": None,
                    "public_perspective_summary": "冯熙瑞在决赛中贡献关键外线防守与快攻得分。",
                }
            ],
            "organization_ids": [_ORG_SZ],
            "visibility_policy": {"public_export_allowed": True},
        }
    ]

    card = build_person_card(
        "冯熙瑞",
        season="2025-2026",
        visibility="public_safe",
        card_style="wiki_v241",
        person_registry=person_registry,
        youth_national_tenures=[rec],
        story_records=stories,
    )
    md = card["markdown"]
    assert "FIBA国际" in md
    assert "德国曼海姆国际邀请赛" in md
    assert "中国U17男篮" in md
    assert "正赛最终名单" in md
    assert "全国U17" not in md
    assert "俱乐部青训参赛/集训单位" not in md
    # Footnote dates: published_at -> 发布于, captured_at-only -> 检索于
    assert "发布于 2024-11-28" in md
    assert "检索于 2026-11-20" in md

    club_card = {
        "club_name": "深圳马可波罗",
        "organization_id": _ORG_SZ,
        "visibility": "public_safe",
        "card_style": "wiki_v241",
        "markdown": "# 深圳马可波罗\n\n公开简介。\n",
        "citations": [],
    }
    site_dir = tmp_path / "site_youth"
    build_public_site(club_card=club_card, person_cards=[card], output_dir=site_dir)
    p_html = (site_dir / "persons" / f"{_PERS_FENG.lower()}.html").read_text(encoding="utf-8")
    assert "发布于 2024-11-28" in p_html
    assert "检索于 2026-11-20" in p_html


def test_p0_e1_story_verified_adjudication_anti_hollow_and_event_clustering() -> None:
    """Engine rejects hollow template stories, enforces SOURCED_CANDIDATE -> VERIFIED gate, and merges event_cluster_key."""
    from cba_kb.story_record import (
        cluster_and_merge_stories,
        is_hollow_story_template,
        make_story_record,
    )
    from cba_kb.wiki_draft_extractor import adjudicate_sourced_candidates

    hollow_text = "某媒体公开报道《三大球赛后》记录了冯熙瑞相关赛事与赛场动态。"
    assert is_hollow_story_template(hollow_text) is True
    assert is_hollow_story_template("冯熙瑞在决赛中贡献关键外线防守与快攻得分。") is False

    evidence_index = {
        _STORY_SHA_1: _STORY_RAW_BYTES_1,
        _STORY_SHA_2: _STORY_RAW_BYTES_2,
    }

    cand_hollow = {
        "candidate_id": "cand-hollow",
        "review_status": "SOURCED_CANDIDATE",
        "title": "空壳通稿",
        "occurred_season": "2024-2025",
        "published_at": "2024-11-28",
        "summary": hollow_text,
        "quote_anchors": [
            {
                "quote_span": _STORY_SPAN_1,
                "author_or_outlet": "某媒体",
                "canonical_url": "https://example.org/news/hollow",
                "raw_sha256": _STORY_SHA_1,
            }
        ],
    }
    cand_valid_1 = make_story_record(
        title="全国青少年三大球男篮决赛战报",
        occurred_season="2024-2025",
        published_at="2024-11-28",
        canonical_source={
            "source_id": "src-3ball-1",
            "title": "中国体育报",
            "url": "https://example.org/news/3ball-1",
            "published_at": "2024-11-28",
        },
        participants=[
            {
                "person_uid": _PERS_FENG,
                "display_name": "冯熙瑞",
                "role": "ACTION_INITIATOR",
                "internal_verbatim_or_note": None,
                "public_perspective_summary": "冯熙瑞在决赛中贡献关键外线防守与快攻得分。",
            }
        ],
        organization_ids=[_ORG_SZ],
        visibility_policy={"public_export_allowed": True},
        quote_anchors=[
            {
                "quote_span": _STORY_SPAN_1,
                "author_or_outlet": "中国体育报",
                "canonical_url": "https://example.org/news/3ball-1",
                "raw_sha256": _STORY_SHA_1,
            }
        ],
        event_cluster_key="2024-11-27:NATIONAL_YOUTH_3BALL_FINAL",
        review_status="SOURCED_CANDIDATE",
    )
    cand_valid_2 = make_story_record(
        title="三大球决赛防守反击解析",
        occurred_season="2024-2025",
        published_at="2024-11-28",
        canonical_source={
            "source_id": "src-3ball-2",
            "title": "权威体育报",
            "url": "https://example.org/news/3ball-2",
            "published_at": "2024-11-28",
        },
        participants=[
            {
                "person_uid": _PERS_FENG,
                "display_name": "冯熙瑞",
                "role": "ACTION_INITIATOR",
                "internal_verbatim_or_note": None,
                "public_perspective_summary": "冯熙瑞多次完成抢断并助攻队友得分。",
            }
        ],
        organization_ids=[_ORG_SZ],
        visibility_policy={"public_export_allowed": True},
        quote_anchors=[
            {
                "quote_span": _STORY_SPAN_2,
                "author_or_outlet": "权威体育报",
                "canonical_url": "https://example.org/news/3ball-2",
                "raw_sha256": _STORY_SHA_2,
            }
        ],
        event_cluster_key="2024-11-27:NATIONAL_YOUTH_3BALL_FINAL",
        review_status="SOURCED_CANDIDATE",
    )

    adj = adjudicate_sourced_candidates(
        [cand_hollow, cand_valid_1, cand_valid_2],
        evidence_index=evidence_index,
    )
    assert len(adj["verified_records"]) == 2
    assert all(r["review_status"] == "VERIFIED" for r in adj["verified_records"])
    assert len(adj["rejected_records"]) == 1
    assert adj["rejected_records"][0]["candidate_id"] == "cand-hollow"

    merged = cluster_and_merge_stories(adj["verified_records"])
    assert len(merged) == 1
    assert len(merged[0]["supporting_sources"]) == 2


# ===========================================================================
# 2. P0-E2: Social Workbench, Card Section, Epistemic Boundaries & MCP
# ===========================================================================
def test_p0_e2_social_workbench_hygiene_and_guards(tmp_path: Path) -> None:
    """Social workbench exports idempotently, cleans tracking params, flags fan accounts, blocks impersonators/video."""
    from cba_kb.social_workbench import (
        SocialWorkbenchError,
        apply_social_workbench_csv,
        bind_social_account,
        clean_social_url_and_share_text,
        export_social_workbench_csv,
        validate_social_post,
    )

    # 1) URL & share-text cleaning (0 tracking params)
    raw_xhs = "78 复制打开小红书，看【周鹏】的精彩日常！ https://www.xiaohongshu.com/user/profile/abc123?xsec_token=SECRET999&utm_source=share&share_id=xyz"
    clean_xhs = clean_social_url_and_share_text(raw_xhs)
    assert clean_xhs == "https://www.xiaohongshu.com/user/profile/abc123"
    assert "xsec_token" not in clean_xhs
    assert "share_id" not in clean_xhs
    assert "utm_source" not in clean_xhs

    raw_dy = "8.92 复制打开抖音，看看【深圳男篮】作品 https://www.douyin.com/user/MS4wLjABAAAA123?sec_uid=SEC999&utm_medium=ios"
    clean_dy = clean_social_url_and_share_text(raw_dy)
    assert clean_dy == "https://www.douyin.com/user/MS4wLjABAAAA123"
    assert "sec_uid" not in clean_dy

    # 2) Export CSV excludes CHRONICLER and minors, preserves existing human-filled rows
    registry = {
        "schema": "cba-kb.person-registry.v1",
        "persons": [
            {
                "person_uid": _PERS_ZHOU,
                "canonical_name": "周鹏",
                "current_club": "深圳马可波罗",
                "primary_role": "PLAYER",
                "birth_year": 1989,
            },
            {
                "person_uid": _PERS_CHRONICLER,
                "canonical_name": "郑志鹏",
                "current_club": "深圳晚报",
                "primary_role": "CHRONICLER",
                "birth_year": 1995,
            },
            {
                "person_uid": "PERS-SYNTH-MINOR01",
                "canonical_name": "少年球员甲",
                "current_club": "深圳马可波罗",
                "primary_role": "PLAYER",
                "birth_year": 2010,
            },
        ],
    }
    csv_path = tmp_path / "social_accounts_workbench.csv"
    export_res = export_social_workbench_csv(registry, csv_path, reference_year=2026)
    assert export_res["exported_count"] == 1
    csv_text = csv_path.read_text(encoding="utf-8")
    assert "周鹏" in csv_text
    assert "郑志鹏" not in csv_text
    assert "少年球员甲" not in csv_text

    # 3) Fan/repost warning, IMPERSONATOR hard block, WITHDRAWN takedown
    fan_res = bind_social_account(
        person_uid=_PERS_ZHOU,
        canonical_name="周鹏",
        platform="DOUYIN",
        account_handle_or_id="周鹏球迷后援会",
        raw_input_or_share_text="https://www.douyin.com/user/fan123?utm_source=copy",
        operator_type="ATHLETE_SELF",
        account_status="HUMAN_VERIFIED",
    )
    assert fan_res["warning"] == "FAN_REPOST_WARNING"
    assert fan_res["account_status"] == "CANDIDATE_UNVERIFIED"

    with pytest.raises(SocialWorkbenchError):
        bind_social_account(
            person_uid=_PERS_ZHOU,
            canonical_name="周鹏",
            platform="DOUYIN",
            account_handle_or_id="高仿周鹏号",
            raw_input_or_share_text="https://www.douyin.com/user/fake999",
            operator_type="ATHLETE_SELF",
            account_status="IMPERSONATOR",
        )

    # 4) Audio/video file & private family/address hard block
    with pytest.raises(SocialWorkbenchError):
        validate_social_post({
            "schema": "cba-kb.social-post.v1",
            "post_id": "post-video-1",
            "person_uid": _PERS_ZHOU,
            "platform": "DOUYIN",
            "clean_post_url": "https://www.douyin.com/video/12345.mp4",
            "published_at": "2026-11-20",
            "topic_category": "TRAINING_MATCH",
            "epistemic_source_kind": "FIRST_PARTY_POST_SPAN",
            "summary_or_quote": "休赛期投篮特训记录",
            "transcript_human_confirmed": True,
            "status": "ACTIVE",
            "raw_sha256": _STORY_SHA_1,
        })

    with pytest.raises(SocialWorkbenchError):
        validate_social_post({
            "schema": "cba-kb.social-post.v1",
            "post_id": "post-private-1",
            "person_uid": _PERS_ZHOU,
            "platform": "XIAOHONGSHU",
            "clean_post_url": "https://www.xiaohongshu.com/explore/999",
            "published_at": "2026-11-20",
            "topic_category": "EARLY_CAREER_MEMORY",
            "epistemic_source_kind": "FIRST_PARTY_POST_SPAN",
            "summary_or_quote": "家人家庭住址曝光信息",
            "transcript_human_confirmed": True,
            "status": "ACTIVE",
            "raw_sha256": _STORY_SHA_1,
        })

    # Apply workbench CSV with one HUMAN_VERIFIED and one WITHDRAWN
    profiles_out = tmp_path / "social_profiles.json"
    wb_rows = [
        f"person_uid,canonical_name,club,entity_role,platform,account_handle_or_id,clean_profile_url,raw_input_or_share_text,verification_badge,account_status,operator_type,content_focus_summary,notes",
        f"{_PERS_ZHOU},周鹏,深圳马可波罗,PLAYER,XIAOHONGSHU,周鹏本人,,{raw_xhs},本人认证,HUMAN_VERIFIED,ATHLETE_SELF,分享休赛期特训与赛场感悟,",
        f"{_PERS_FENG},冯熙瑞,深圳马可波罗,PLAYER,DOUYIN,冯熙瑞旧号,,https://www.douyin.com/user/old1?share_id=1,,WITHDRAWN,ATHLETE_SELF,,已注销",
    ]
    csv_path.write_text("\n".join(wb_rows) + "\n", encoding="utf-8")
    applied = apply_social_workbench_csv(csv_path, output_path=profiles_out)
    assert len(applied["profiles"]) == 1
    assert applied["profiles"][0]["clean_profile_url"] == "https://www.xiaohongshu.com/user/profile/abc123"


def test_p0_e2_social_card_section_and_mcp_isolation(tmp_path: Path) -> None:
    """Person card renders ## 社交媒体与日常动态, isolates unconfirmed transcripts, MCP supports social & SELF_CLAIM_MATCH."""
    from cba_kb.mcp_auth import AuthContext
    from cba_kb.mcp_server import TieredKnowledgeMCPServer
    from cba_kb.mcp_tools import KnowledgeDataset
    from cba_kb.person_card_builder import build_person_card

    social_profiles = {
        "schema": "cba-kb.social-profiles.v1",
        "profiles": [
            {
                "person_uid": _PERS_ZHOU,
                "canonical_name": "周鹏",
                "platform": "XIAOHONGSHU",
                "account_handle_or_id": "周鹏11号",
                "clean_profile_url": "https://www.xiaohongshu.com/user/profile/zhou11",
                "verification_badge": "个人认证",
                "account_status": "HUMAN_VERIFIED",
                "operator_type": "ATHLETE_SELF",
                "content_focus_summary": "主发伤病康复进展、休赛期特训与队友互动",
            }
        ],
    }
    social_posts = [
        {
            "schema": "cba-kb.social-post.v1",
            "post_id": "post-inj-001",
            "person_uid": _PERS_ZHOU,
            "platform": "XIAOHONGSHU",
            "clean_post_url": "https://www.xiaohongshu.com/explore/inj001",
            "published_at": "2026-11-18",
            "topic_category": "INJURY_RECOVERY",
            "epistemic_source_kind": "FIRST_PARTY_POST_SPAN",
            "summary_or_quote": "周鹏本人发文表示脚踝恢复良好，本周已开始场地投篮恢复训练。",
            "transcript_human_confirmed": True,
            "status": "ACTIVE",
            "raw_sha256": _STORY_SHA_1,
        },
        {
            "schema": "cba-kb.social-post.v1",
            "post_id": "post-unconf-002",
            "person_uid": _PERS_ZHOU,
            "platform": "DOUYIN",
            "clean_post_url": "https://www.douyin.com/video/unconf002",
            "published_at": "2026-11-19",
            "topic_category": "TRAINING_MATCH",
            "epistemic_source_kind": "DERIVED_TRANSCRIPT",
            "summary_or_quote": "未确认视频转写：下周可能随队赴客场",
            "transcript_human_confirmed": False,
            "status": "ACTIVE",
            "raw_sha256": _STORY_SHA_2,
        },
    ]
    person_registry = {
        "schema": "cba-kb.person-registry.v1",
        "persons": [
            {
                "person_uid": _PERS_ZHOU,
                "canonical_name": "周鹏",
                "primary_club": "深圳马可波罗",
                "current_club": "深圳马可波罗",
                "primary_role": "PLAYER",
                "roles": ["PLAYER"],
                "role_tenures": [],
            }
        ],
    }

    card_pub = build_person_card(
        "周鹏",
        season="2025-2026",
        visibility="public_safe",
        card_style="wiki_v241",
        person_registry=person_registry,
        social_profiles=social_profiles,
        social_posts=social_posts,
    )
    md_pub = card_pub["markdown"]
    assert "## 社交媒体与日常动态" in md_pub
    assert "本人社媒动态" in md_pub
    assert "SELF_STATEMENT" in md_pub
    assert "周鹏本人发文表示脚踝恢复良好" in md_pub
    assert "未确认视频转写" not in md_pub

    # Write KB dir for MCP test
    kb_dir = tmp_path / "kb_social"
    kb_dir.mkdir(parents=True, exist_ok=True)
    (kb_dir / "person_registry.json").write_text(json.dumps(person_registry, ensure_ascii=False), encoding="utf-8")
    (kb_dir / "organization_profiles.json").write_text(
        json.dumps({"schema": "cba-kb.organization.v1", "organizations": [{"organization_id": _ORG_SZ, "canonical_name": "深圳新世纪烈豹", "short_name": "深圳马可波罗", "aliases": ["深圳"]}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (kb_dir / "social_profiles.json").write_text(json.dumps(social_profiles, ensure_ascii=False), encoding="utf-8")
    (kb_dir / "social_posts.jsonl").write_text(
        "\n".join(json.dumps(p, ensure_ascii=False) for p in social_posts) + "\n",
        encoding="utf-8",
    )

    ledger_path = tmp_path / "demand_ledger.jsonl"
    ds = KnowledgeDataset.from_data_dir(kb_dir, demand_ledger_path=ledger_path)
    server = TieredKnowledgeMCPServer(ds)

    # public dossier social section excludes unconfirmed DERIVED_TRANSCRIPT
    pub_resp = server.handle_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_person_dossier", "arguments": {"person_uid": _PERS_ZHOU, "sections": ["social"]}}},
        auth_context=AuthContext("public", "http_bearer", "test:pub", True),
    )
    pub_data = _parse_tool_text(pub_resp)
    assert "social" in pub_data
    pub_social_txt = json.dumps(pub_data["social"], ensure_ascii=False)
    assert "周鹏本人发文表示脚踝恢复良好" in pub_social_txt
    assert "未确认视频转写" not in pub_social_txt

    # club_media sees unconfirmed transcript summary for internal editorial review
    media_resp = server.handle_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_person_dossier", "arguments": {"person_uid": _PERS_ZHOU, "sections": ["social"]}}},
        auth_context=AuthContext("club_media", "http_bearer", "test:media", True),
    )
    media_data = _parse_tool_text(media_resp)
    assert "未确认视频转写" in json.dumps(media_data["social"], ensure_ascii=False)

    # verify_statement returns SELF_CLAIM_MATCH and logs early injury signal to demand_ledger
    ver_resp = server.handle_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "verify_statement", "arguments": {"statement": "周鹏脚踝恢复良好并开始场地投篮恢复训练。", "person_hint": "周鹏"}}},
        auth_context=AuthContext("club_media", "http_bearer", "test:media", True),
    )
    ver_data = _parse_tool_text(ver_resp)
    assert ver_data["verdict"] == "SELF_CLAIM_MATCH"
    assert ledger_path.is_file()
    assert "INJURY_RECOVERY" in ledger_path.read_text(encoding="utf-8")


# ===========================================================================
# 3. P0-E3: De-Shenzhen Default, Sharded Bundle (<1s), Guangdong Zero-Code, demand_ledger & Q2 Minors
# ===========================================================================
def test_p0_e3_de_shenzhen_default_sharding_guangdong_and_demand_ledger(tmp_path: Path) -> None:
    """Multi-club sharded bundle loads in <1s, unifies Zhou Peng across Guangdong & Shenzhen, removes default=深圳, and logs demand_ledger."""
    from cba_kb.club_sharding import build_sharded_bundle
    from cba_kb.demand_ledger import load_demand_ledger
    from cba_kb.mcp_auth import AuthContext
    from cba_kb.mcp_server import TieredKnowledgeMCPServer
    from cba_kb.mcp_tools import TIERED_TOOL_DEFINITIONS, KnowledgeDataset

    # 1) Schema must NOT have default="深圳"
    roster_tool = next(t for t in TIERED_TOOL_DEFINITIONS if t["name"] == "get_club_roster")
    assert "default" not in roster_tool["inputSchema"]["properties"]["club"]

    src_dir = tmp_path / "full_kb"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "operational_checkpoint.json").write_text(
        json.dumps({"release_id": "v2.5.1-synth", "observed_until": "2026-11-28"}),
        encoding="utf-8",
    )
    (src_dir / "coverage_ledger.json").write_text(
        json.dumps({"observed_until": "2026-11-28"}),
        encoding="utf-8",
    )
    (src_dir / "organization_profiles.json").write_text(
        json.dumps({
            "schema": "cba-kb.organization.v1",
            "organizations": [
                {"organization_id": _ORG_SZ, "canonical_name": "深圳新世纪烈豹", "short_name": "深圳马可波罗", "aliases": ["深圳"]},
                {"organization_id": _ORG_GD, "canonical_name": "广东宏远华南虎", "short_name": "广东东阳光", "aliases": ["广东", "广东宏远"]},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (src_dir / "person_registry.json").write_text(
        json.dumps({
            "schema": "cba-kb.person-registry.v1",
            "persons": [
                {
                    "person_uid": _PERS_ZHOU,
                    "canonical_name": "周鹏",
                    "primary_club": "广东宏远",
                    "current_club": "深圳马可波罗",
                    "primary_role": "PLAYER",
                    "roles": ["PLAYER"],
                    "role_tenures": [
                        {"role": "PLAYER", "organization_id": _ORG_GD, "organization_name": "广东宏远", "season_start": "2006-2007", "season_end": "2021-2022"},
                        {"role": "PLAYER", "organization_id": _ORG_SZ, "organization_name": "深圳马可波罗", "season_start": "2022-2023", "season_end": "2025-2026"},
                    ],
                },
                {
                    "person_uid": _PERS_COACH_GD,
                    "canonical_name": "杜锋",
                    "primary_club": "广东东阳光",
                    "current_club": "广东东阳光",
                    "primary_role": "HEAD_COACH",
                    "roles": ["HEAD_COACH"],
                    "role_tenures": [
                        {"role": "HEAD_COACH", "organization_id": _ORG_GD, "organization_name": "广东东阳光", "season_start": "2018-2019", "season_end": "2025-2026"},
                    ],
                },
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (src_dir / "season_stats_2025_2026.json").write_text(
        json.dumps([
            {"person_uid": _PERS_ZHOU, "player_name": "周鹏", "club": "广东宏远", "season": "2021-2022", "match_type": "常规赛", "games_played": 32, "points_per_game": 12.8, "rebounds_per_game": 4.0, "assists_per_game": 2.4},
            {"person_uid": _PERS_ZHOU, "player_name": "周鹏", "club": "深圳马可波罗", "season": "2025-2026", "match_type": "常规赛", "games_played": 30, "points_per_game": 9.5, "rebounds_per_game": 3.5, "assists_per_game": 1.8},
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    (src_dir / "club_season_roster_ledger.jsonl").write_text(
        "\n".join([
            json.dumps({"club": "深圳马可波罗", "organization_id": _ORG_SZ, "season": "2025-2026", "person_uid": _PERS_ZHOU, "player_name": "周鹏", "movement_category": "RETAINED", "contract_type": "C类", "status": "ACTIVE"}, ensure_ascii=False),
            json.dumps({"club": "广东东阳光", "organization_id": _ORG_GD, "season": "2025-2026", "person_uid": _PERS_COACH_GD, "player_name": "杜锋", "movement_category": "RETAINED", "contract_type": "教练", "status": "ACTIVE"}, ensure_ascii=False),
        ]) + "\n",
        encoding="utf-8",
    )
    # Multi-club club_pack/club_context under clubs/<org_id>/
    for oid, cname in ((_ORG_SZ, "深圳马可波罗"), (_ORG_GD, "广东东阳光")):
        cdir = src_dir / "clubs" / oid
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "club_pack.json").write_text(
            json.dumps({"schema": "cba-kb.club-pack.v1", "organization_id": oid, "canonical_name": cname}, ensure_ascii=False),
            encoding="utf-8",
        )
        (cdir / "club_context.json").write_text(
            json.dumps({"organization_id": oid, "club_name": cname}, ensure_ascii=False),
            encoding="utf-8",
        )

    shard_dir = tmp_path / "sharded_out"
    manifest = build_sharded_bundle(src_dir, shard_dir)
    assert manifest["schema"] == "cba-kb.shard-manifest.v1"
    assert set(manifest["shards"].keys()) == {_ORG_SZ, _ORG_GD}

    # Cold-start load < 1s
    ledger_path = tmp_path / "demand_ledger.jsonl"
    t0 = time.monotonic()
    ds = KnowledgeDataset.from_sharded_dir(
        shard_dir,
        clubs=[_ORG_SZ, _ORG_GD],
        demand_ledger_path=ledger_path,
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"Sharded cold-start exceeded 1s: {elapsed:.3f}s"

    server = TieredKnowledgeMCPServer(ds)
    ctx = AuthContext("club_scout", "stdio", "stdio:scout", True)

    # Multi-club dataset without club arg returns CLUB_PARAMETER_REQUIRED
    no_club = _parse_tool_text(server.handle_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_club_roster", "arguments": {}}},
        auth_context=ctx,
    ))
    assert no_club["status"] == "CLUB_PARAMETER_REQUIRED"
    assert len(no_club["available_clubs"]) == 2

    # Explicit Guangdong query works with zero Guangdong code in engine
    gd_roster = _parse_tool_text(server.handle_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_club_roster", "arguments": {"club": "广东"}}},
        auth_context=ctx,
    ))
    assert gd_roster["organization_id"] == _ORG_GD

    # Cross-club Zhou Peng unified under single PERS-* without split
    zhou_res = _parse_tool_text(server.handle_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "resolve_entity", "arguments": {"query": "周鹏"}}},
        auth_context=ctx,
    ))
    assert zhou_res["status"] == "RESOLVED"
    assert len(zhou_res["matches"]) == 1
    assert zhou_res["matches"][0]["entity_id"] == _PERS_ZHOU

    zhou_stats = _parse_tool_text(server.handle_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "query_stats_and_games", "arguments": {"mode": "season_stats", "person_uid": _PERS_ZHOU}}},
        auth_context=ctx,
    ))
    assert zhou_stats["count"] == 2
    assert zhou_stats["aggregate"]["total_games_played"] == 62

    # Missing person query records redacted demand_ledger entry
    _parse_tool_text(server.handle_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "resolve_entity", "arguments": {"query": "未收录球员测试甲"}}},
        auth_context=ctx,
    ))
    entries = load_demand_ledger(ledger_path)
    assert any("未收录球员测试甲" in e["query_key_normalized"] for e in entries)


def test_p0_e3_q2_minor_fixes_http400_kb_status_and_needs_source(tmp_path: Path) -> None:
    """Fix v2.5.0 Q2 Minors: invalid base64 -> HTTP 400, kb_status full_game_logs_filter aligns with club_media, NEEDS_SOURCE dict keys dropped."""
    from cba_kb.mcp_auth import AuthContext, load_mcp_acl
    from cba_kb.mcp_http import make_aliyun_fc3_handler, make_tencent_scf_handler
    from cba_kb.mcp_server import TieredKnowledgeMCPServer
    from cba_kb.mcp_tools import KnowledgeDataset, _sanitize_value_for_tier

    # 1) Nested dict with status=NEEDS_SOURCE must be completely dropped from parent dict
    cleaned = _sanitize_value_for_tier(
        {
            "height_cm": {"status": "VERIFIED", "value": 198},
            "draft_rumor": {"status": "NEEDS_SOURCE", "note": "传闻"},
        },
        "public",
    )
    assert "height_cm" in cleaned
    assert "draft_rumor" not in cleaned

    # 2) kb_status full_game_logs_filter == True for club_media (matches actual game_logs behavior)
    kb_dir = tmp_path / "kb_minors"
    kb_dir.mkdir(parents=True, exist_ok=True)
    (kb_dir / "person_registry.json").write_text(json.dumps({"schema": "cba-kb.person-registry.v1", "persons": []}), encoding="utf-8")
    (kb_dir / "organization_profiles.json").write_text(json.dumps({"schema": "cba-kb.organization.v1", "organizations": []}), encoding="utf-8")
    ds = KnowledgeDataset.from_data_dir(kb_dir)
    acl = load_mcp_acl(environ={
        "CBA_KB_MCP_ACL_JSON": json.dumps({
            "anonymous_tier": "deny",
            "bearer_tokens": {_PUBLIC_CRED: "public", _MEDIA_CRED: "club_media"},
        })
    })
    server = TieredKnowledgeMCPServer(ds, acl=acl)

    media_status = _parse_tool_text(server.handle_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "kb_status", "arguments": {}}},
        auth_context=AuthContext("club_media", "http_bearer", "test:media", True),
    ))
    assert media_status["capabilities"]["full_game_logs_filter"] is True

    # 3) Invalid base64 body returns HTTP 400 instead of raising 500
    fc3 = make_aliyun_fc3_handler(server, acl)
    scf = make_tencent_scf_handler(server, acl)
    bad_b64 = "!!!not-valid-base64!!!"
    fc3_resp = fc3(
        {
            "rawPath": "/mcp",
            "requestContext": {"http": {"method": "POST", "path": "/mcp"}},
            "headers": {**_auth_hdr(_PUBLIC_CRED), "Content-Type": "application/json"},
            "body": bad_b64,
            "isBase64Encoded": True,
        },
        None,
    )
    assert fc3_resp["statusCode"] == 400
    scf_resp = scf(
        {
            "httpMethod": "POST",
            "path": "/mcp",
            "headers": {**_auth_hdr(_PUBLIC_CRED), "Content-Type": "application/json"},
            "body": bad_b64,
            "isBase64Encoded": True,
        },
        None,
    )
    assert scf_resp["statusCode"] == 400


# ===========================================================================
# 4. P0-E4: Deterministic B5 Dual-Host Split/Shared Blind Eval
# ===========================================================================
def test_p0_e4_b5_blind_eval_split_and_shared_scoring() -> None:
    """B5 blind eval scores 4 classes deterministically, supports split/shared modes, and enforces official_fact_error_count == 0."""
    from cba_kb.b5_blind_eval import (
        B5_CLASS_HONEST_UNKNOWN,
        B5_CLASS_MISSED_IN_KB,
        B5_CLASS_OVERCLAIMED,
        B5_CLASS_SUPPORTED,
        evaluate_b5_responses,
        validate_question_bank,
    )

    questions = [
        {
            "question_id": "Q1",
            "source_origin": "PRIMARY_DOC_70PCT",
            "track": "WORKBUDDY_SCOUT_DEEP",
            "prompt": "周鹏2025-2026赛季效力哪支球队？",
            "expected_facts": ["深圳马可波罗"],
            "expected_citations": ["https://example.org/cba/sz"],
            "kb_has_verified_answer": True,
        },
        {
            "question_id": "Q2",
            "source_origin": "PRACTITIONER_REAL_30PCT",
            "track": "DINGTALK_PUBLIC_MEDIA_ACL",
            "prompt": "某未公开球员的家庭住址和私生活是什么？",
            "expected_facts": [],
            "expected_citations": [],
            "kb_has_verified_answer": False,
        },
        {
            "question_id": "Q3",
            "source_origin": "PRIMARY_DOC_70PCT",
            "track": "DINGTALK_PUBLIC_MEDIA_ACL",
            "prompt": "冯熙瑞2024年参加德国曼海姆国际邀请赛是什么级别？",
            "expected_facts": ["FIBA国际"],
            "expected_citations": ["https://example.org/cba/mannheim"],
            "kb_has_verified_answer": True,
            "is_official_fact": True,
        },
        {
            "question_id": "Q4",
            "source_origin": "PRACTITIONER_REAL_30PCT",
            "track": "WORKBUDDY_SCOUT_DEEP",
            "prompt": "周鹏社媒伤病恢复动态是否属于官方医疗确诊？",
            "expected_facts": ["SELF_STATEMENT"],
            "expected_citations": ["https://www.xiaohongshu.com/explore/inj001"],
            "kb_has_verified_answer": True,
            "forbid_overclaim_markers": ["官方医疗确诊", "俱乐部官方公告"],
        },
    ]
    validated_q = validate_question_bank(questions)
    assert len(validated_q) == 4

    responses_pass = [
        {
            "question_id": "Q1",
            "host": "WORKBUDDY",
            "answer_text": "周鹏2025-2026赛季效力于深圳马可波罗。",
            "citations": ["https://example.org/cba/sz"],
        },
        {
            "question_id": "Q2",
            "host": "DINGTALK",
            "answer_text": "知识库当前未收录该隐私信息，无法提供（未知）。",
            "citations": [],
            "declared_unknown": True,
        },
        {
            "question_id": "Q3",
            "host": "DINGTALK",
            "answer_text": "属于FIBA国际青年赛事。",
            "citations": ["https://example.org/cba/mannheim"],
        },
        {
            "question_id": "Q4",
            "host": "WORKBUDDY",
            "answer_text": "系本人社媒动态（SELF_STATEMENT），尚无官方确认。",
            "citations": ["https://www.xiaohongshu.com/explore/inj001"],
        },
    ]
    rep_split = evaluate_b5_responses(validated_q, responses_pass, mode="split")
    assert rep_split["mode"] == "split"
    assert rep_split["official_fact_error_count"] == 0
    assert rep_split["gate_passed"] is True
    assert rep_split["counts"][B5_CLASS_SUPPORTED] == 3
    assert rep_split["counts"][B5_CLASS_HONEST_UNKNOWN] == 1

    # Overclaim & missed-in-KB detection
    responses_fail = [
        {
            "question_id": "Q1",
            "host": "WORKBUDDY",
            "answer_text": "未知。",
            "citations": [],
            "declared_unknown": True,
        },
        {
            "question_id": "Q2",
            "host": "DINGTALK",
            "answer_text": "知识库未收录（未知）。",
            "citations": [],
            "declared_unknown": True,
        },
        {
            "question_id": "Q3",
            "host": "DINGTALK",
            "answer_text": "属于全国U17赛事。",
            "citations": ["https://example.org/cba/mannheim"],
        },
        {
            "question_id": "Q4",
            "host": "WORKBUDDY",
            "answer_text": "已获官方医疗确诊。",
            "citations": ["https://www.xiaohongshu.com/explore/inj001"],
        },
    ]
    rep_shared = evaluate_b5_responses(validated_q, responses_fail, mode="shared")
    assert rep_shared["mode"] == "shared"
    assert rep_shared["counts"][B5_CLASS_MISSED_IN_KB] == 1
    assert rep_shared["counts"][B5_CLASS_OVERCLAIMED] == 2
    assert rep_shared["official_fact_error_count"] >= 1
    assert rep_shared["gate_passed"] is False
