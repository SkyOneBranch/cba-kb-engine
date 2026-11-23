"""Frozen red falsification tests for Issue #270 (v2.2.2-1 Stage 4 curated filter hotfix).

Covers the 3 real-world Stage 4 findings caught by the <25KB fail-closed gate:
1. `【唁电】` condolence posts mentioning historical `"国家队"` must still be filtered
   from the curated `.md` digest (while retained 100% in the JSON document index).
2. Asian Games (`亚运会`) posts (e.g. `"在今天结束的亚运会三、四名决赛中..."`, even with
   `published_at >= 2025-07-01`) must be filtered from the curated `.md` digest
   (while retained 100% in the JSON document index).
3. A realistic 30-bundle stress fixture including 7 long `#判罚解析#` cases (3-6 KB each)
   plus official club/league announcements must render `compact_curated=True` comfortably
   under 22,000 bytes (< 25,600 B hard gate) while preserving key operational items
   (`鲍威尔`, `梅肯`, `沃顿`, `#判罚解析#`) and excluding all noise.

All fixtures are 100% synthetic. Zero network calls, zero Drive API calls,
zero local paths, zero raw Drive IDs.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from cba_kb import consumer_surface

ENGINE_SHA = "1d33e01cf1f7bfca34d9417f470341e3e568da80"


def test_hotfix_condolence_with_national_team_mention_is_filtered_from_md_and_kept_in_index():
    condolence_bundle = {
        "doc_id": "doc_hotfix_condolence_nt_001",
        "title": "【唁电】沉痛悼念中国篮球名宿、前国家队教练员",
        "source_tier": "T1",
        "event_class": "官方公告",
        "published_at": "2026-11-12",
        "acquired_at": "2026-11-23T08:00:00Z",
        "canonical_url": "https://example.org/weibo/condolence-001",
        "upstream_origin": "中国篮协官微",
        "raw_sha256": "1" * 64,
        "matched_persons": [],
        "body": "惊悉前国家队老前辈辞世，特发唁电深切哀悼，致敬其为中国篮球国家队作出的卓越贡献。",
    }
    signing_bundle = {
        "doc_id": "doc_hotfix_signing_002",
        "title": "俱乐部正式签约外援鲍威尔公告",
        "source_tier": "T1.5",
        "event_class": "注册与转会",
        "published_at": "2026-11-19",
        "acquired_at": "2026-11-23T08:00:00Z",
        "canonical_url": "https://example.org/news/signing-002",
        "upstream_origin": "俱乐部官微",
        "raw_sha256": "2" * 64,
        "matched_persons": ["鲍威尔"],
        "body": "俱乐部完成外援鲍威尔签约，待CBA公司审核公示。",
    }

    res = consumer_surface.filter_curated_operational_announcements(
        [condolence_bundle, signing_bundle], active_window_start="2025-07-01"
    )
    assert {b["doc_id"] for b in res["filtered"]} == {"doc_hotfix_condolence_nt_001"}
    assert {b["doc_id"] for b in res["admitted"]} == {"doc_hotfix_signing_002"}

    digest_md = consumer_surface.render_collector_official_digest_md(
        bundles=[condolence_bundle, signing_bundle],
        release_id="v2.2.2-1",
        engine_sha=ENGINE_SHA,
        compact_curated=True,
    ).decode("utf-8")
    assert "唁电" not in digest_md
    assert "doc_hotfix_condolence_nt_001" not in digest_md
    assert "鲍威尔" in digest_md

    checkpoint = {
        "schema_version": 1,
        "consumed_bundles": {
            "2026-11/condolence.md": condolence_bundle,
            "2026-11/signing.md": signing_bundle,
        },
    }
    merged = json.loads(
        consumer_surface.merge_operational_docs_into_consumer_index(None, checkpoint).decode("utf-8")
    )
    assert {d["doc_id"] for d in merged["documents"]} == {
        "doc_hotfix_condolence_nt_001",
        "doc_hotfix_signing_002",
    }


def test_hotfix_asian_games_post_is_filtered_regardless_of_published_at():
    asiad_bundle = {
        "doc_id": "doc_hotfix_asiad_001",
        "title": "在今天结束的亚运会三、四名决赛中，中国男篮70:79负于伊朗男篮",
        "source_tier": "T1",
        "event_class": "官方公告",
        "published_at": "2026-09-20",
        "acquired_at": "2026-11-23T08:00:00Z",
        "canonical_url": "https://example.org/weibo/asiad-001",
        "upstream_origin": "中国篮协官微",
        "raw_sha256": "3" * 64,
        "matched_persons": [],
        "body": "在今天结束的亚运会三、四名决赛中，中国男篮70:79负于伊朗男篮，回顾本届亚运会征程。",
    }
    ref_bundle = {
        "doc_id": "doc_hotfix_ref_002",
        "title": "#判罚解析# 2026-2027赛季常规赛关键判罚说明",
        "source_tier": "T1",
        "event_class": "判罚与规则",
        "published_at": "2026-11-18",
        "acquired_at": "2026-11-23T08:00:00Z",
        "canonical_url": "https://example.org/weibo/ref-002",
        "upstream_origin": "CBA联赛官微",
        "raw_sha256": "4" * 64,
        "matched_persons": [],
        "body": "CBA裁判办公室发布#判罚解析#，明确末节最后两分钟犯规尺度。",
    }

    res = consumer_surface.filter_curated_operational_announcements(
        [asiad_bundle, ref_bundle], active_window_start="2025-07-01"
    )
    assert {b["doc_id"] for b in res["filtered"]} == {"doc_hotfix_asiad_001"}
    assert {b["doc_id"] for b in res["admitted"]} == {"doc_hotfix_ref_002"}

    digest_md = consumer_surface.render_collector_official_digest_md(
        bundles=[asiad_bundle, ref_bundle],
        release_id="v2.2.2-1",
        engine_sha=ENGINE_SHA,
        compact_curated=True,
    ).decode("utf-8")
    assert "亚运会" not in digest_md
    assert "doc_hotfix_asiad_001" not in digest_md
    assert "#判罚解析#" in digest_md

    checkpoint = {
        "schema_version": 1,
        "consumed_bundles": {
            "2026-09/asiad.md": asiad_bundle,
            "2026-11/ref.md": ref_bundle,
        },
    }
    merged = json.loads(
        consumer_surface.merge_operational_docs_into_consumer_index(None, checkpoint).decode("utf-8")
    )
    assert {d["doc_id"] for d in merged["documents"]} == {
        "doc_hotfix_asiad_001",
        "doc_hotfix_ref_002",
    }


def test_hotfix_30_bundle_stress_with_long_officiating_cases_under_22kb():
    bundles: List[Dict[str, Any]] = [
        # 3 domestic registration masters
        {
            "doc_id": f"doc_stress_dom_{i:02d}",
            "title": f"{2024+i}-{2025+i}赛季CBA联赛国内球员注册信息",
            "source_tier": "T1",
            "event_class": "注册与转会",
            "published_at": f"{2024+i}-08-06",
            "acquired_at": "2026-11-23T08:00:00Z",
            "canonical_url": f"https://example.org/register/dom-{i}",
            "upstream_origin": "CBA官方注册公示",
            "raw_sha256": f"{i+1}" * 64,
            "matched_persons": [],
            "body": ("国内球员注册明细行示例 | 深圳新世纪 | B类\n" * 400),
        }
        for i in range(3)
    ]

    # 1 foreign registration snapshot
    bundles.append({
        "doc_id": "doc_stress_for_04",
        "title": "2026-2027赛季CBA联赛外籍球员注册信息（截至11月21日17点）",
        "source_tier": "T1",
        "event_class": "注册与转会",
        "published_at": "2026-11-21",
        "acquired_at": "2026-11-23T08:00:00Z",
        "canonical_url": "https://example.org/register/foreign",
        "upstream_origin": "CBA官方外籍球员注册公示",
        "raw_sha256": "4" * 64,
        "matched_persons": [],
        "body": "\n".join([
            "| 俱乐部 | 外籍球员 |  | 国籍（以注册时提供护照为准） | 场上位置 | 球衣号码 |",
            "| --- | --- | --- | --- | --- | --- |",
            "| 深圳新世纪 | SYNTH POWELL | 合成鲍威尔 | 美国 | PG | 11 |",
            "| 广东宏远 | SYNTH MACON | 合成梅肯 | 美国 | SG | 23 |",
            "| 辽宁沈阳三生 | SYNTH WHARTON | 合成沃顿 | 美国 | C | 55 |",
        ]),
    })

    # 7 long #判罚解析# bundles (3-6 KB each)
    long_ref_paragraph = (
        "在第四节最后两分钟关键攻防回合中，进攻队员在罚球线弧顶实施无球掩护时双脚未完全站稳，"
        "裁判员依据CBA联赛最新裁判规则手册与临场判罚尺度判定进攻掩护犯规，并经即时回放中心复核确认。"
    )
    for idx in range(7):
        repeat = 25 + idx * 5  # ~3.5KB to ~6.5KB UTF-8 each
        bundles.append({
            "doc_id": f"doc_stress_ref_{idx+1:02d}",
            "title": f"#判罚解析# 第{idx+1}期：常规赛焦点战最后两分钟裁判报告详解",
            "source_tier": "T1",
            "event_class": "判罚与规则",
            "published_at": f"2026-11-{10+idx:02d}",
            "acquired_at": "2026-11-23T08:00:00Z",
            "canonical_url": f"https://example.org/weibo/ref-case-{idx+1:02d}",
            "upstream_origin": "CBA联赛官微",
            "raw_sha256": f"{idx:x}" * 64,
            "matched_persons": [],
            "body": long_ref_paragraph * repeat,
        })

    # 12 curated operational announcements (signings, waivers, rosters, NBL-CBA meetings)
    curated_specs = [
        ("doc_stress_op_01", "俱乐部官宣外援鲍威尔正式签约公告", "注册与转会", ["鲍威尔"], "俱乐部完成与外援鲍威尔签约手续，待联赛审核公示。"),
        ("doc_stress_op_02", "俱乐部官宣外援梅肯试训及注册进展", "注册与转会", ["梅肯"], "外援梅肯已抵队合练并完成体检，即将提交注册材料。"),
        ("doc_stress_op_03", "俱乐部官宣与外援沃顿完成签约", "注册与转会", ["沃顿"], "球队正式签下内线外援沃顿，补强新赛季内线轮换。"),
        ("doc_stress_op_04", "国家队集训名单公布：贺希宁、赵睿入选", "伤病与名单", ["贺希宁", "赵睿"], "中国篮协公布最新一期男篮集训名单，贺希宁与赵睿顺利入选。"),
        ("doc_stress_op_05", "NBL-CBA规则对接与联赛工作会议纪要", "联赛政策", [], "联赛官方召开工作会议，明确NBL-CBA规则对接及准入标准。"),
    ]
    for idx in range(6, 13):
        curated_specs.append((
            f"doc_stress_op_{idx:02d}",
            f"2026-2027赛季俱乐部轮换与伤情更新公告第{idx}号",
            "伤病与名单",
            [],
            ("俱乐部公布最新伤情康复进展与客场出征轮换安排，主教练强调防守专注度与健康管理。" * 10),
        ))
    for doc_id, title, ev_cls, persons, body in curated_specs:
        bundles.append({
            "doc_id": doc_id,
            "title": title,
            "source_tier": "T1.5",
            "event_class": ev_cls,
            "published_at": "2026-11-19",
            "acquired_at": "2026-11-23T08:00:00Z",
            "canonical_url": f"https://example.org/news/{doc_id}",
            "upstream_origin": "俱乐部官微",
            "raw_sha256": "a" * 64,
            "matched_persons": persons,
            "body": body,
        })

    # 7 noise bundles (bringing total to exactly 30)
    noise_specs = [
        ("doc_stress_noise_01", "【每日预言家】今晚焦点战竞猜有奖互动", "社媒互动", "2026-11-20", "参与每日预言家抽奖赢周边。"),
        ("doc_stress_noise_02", "【每日名场面】精彩暴扣集锦投票", "社媒互动", "2026-11-19", "每日名场面投票评选最佳高光。"),
        ("doc_stress_noise_03", "图文征集：我和我的篮球故事", "社媒互动", "2026-11-18", "欢迎投稿我和我的篮球图文征集。"),
        ("doc_stress_noise_04", "【每日预言家】次轮预测抽奖活动", "社媒互动", "2026-11-17", "有奖互动转发抽奖。"),
        ("doc_stress_noise_05", "【每日名场面】经典三分回顾", "社媒互动", "2026-11-16", "每日名场面粉丝互动。"),
        ("doc_stress_noise_06", "【唁电】沉痛悼念前国家队功勋名宿", "官方公告", "2026-11-12", "特发唁电沉痛悼念前国家队老前辈。"),
        ("doc_stress_noise_07", "在今天结束的亚运会三、四名决赛中中国男篮负于伊朗", "官方公告", "2026-09-20", "亚运会赛程回顾与总结。"),
    ]
    for doc_id, title, ev_cls, pub, body in noise_specs:
        bundles.append({
            "doc_id": doc_id,
            "title": title,
            "source_tier": "T1",
            "event_class": ev_cls,
            "published_at": pub,
            "acquired_at": "2026-11-23T08:00:00Z",
            "canonical_url": f"https://example.org/weibo/{doc_id}",
            "upstream_origin": "官方微博",
            "raw_sha256": "f" * 64,
            "matched_persons": [],
            "body": body,
        })

    assert len(bundles) == 30

    reg_diff = {
        "statement_notice": "Candidate diff. Statement != Fact. Evidence != Fact. Unknown != 0.",
        "old_bundle": {"title": "2025-2026国内注册", "clubs_count": 20, "players_count": 335, "raw_sha256": "2" * 64},
        "new_bundle": {"title": "2026-2027国内注册", "clubs_count": 20, "players_count": 330, "raw_sha256": "3" * 64},
        "summary": {"added_count": 74, "removed_count": 79, "transferred_count": 45, "contract_changed_count": 166},
    }

    digest_bytes = consumer_surface.render_collector_official_digest_md(
        bundles=bundles,
        reg_diff=reg_diff,
        release_id="v2.2.2-1",
        engine_sha=ENGINE_SHA,
        compact_curated=True,
    )
    assert len(digest_bytes) < 22_000, f"Expected < 22,000 B for 30-bundle stress fixture, got {len(digest_bytes)} B"

    text = digest_bytes.decode("utf-8")
    for expected in ("鲍威尔", "梅肯", "沃顿", "#判罚解析#"):
        assert expected in text
    for excluded in ("每日预言家", "每日名场面", "图文征集", "我和我的篮球", "唁电", "亚运会"):
        assert excluded not in text
