"""REQ-250 (Issue #288) Frozen Acceptance Test Suite — CBA-KB Engine v2.5.0.

Covers:
1. SnowballMCPServer v2.3.0 backward compatibility.
2. stdio JSON-RPC 2.0 transport (`TieredKnowledgeMCPServer`, protocol negotiation
   `2025-03-26` & `2024-11-05`, `ping`, `tools/list` with Chinese descriptions &
   `additionalProperties: false`, `tools/call`).
3. Stateless Streamable HTTP (`handle_mcp_http_request`) + Aliyun FC 3.0 &
   Tencent SCF adapters (`POST /mcp` single & batch, `notifications/initialized`
   -> 202, `Accept: application/json, text/event-stream`, `GET /healthz` -> 200,
   `GET /mcp` -> 405 with `Allow: POST, OPTIONS`, `OPTIONS`/`DELETE /mcp` -> 204).
4. `--bundle-tarball` secure in-memory loading + rejection of path traversal,
   symlinks, bomb/oversized archives, and forbidden ACL/secret files.
5. Dual-factor ACL gateway (`mcp_auth.py`): Direct Bearer, stdio env, DingTalk
   dual-factor + anti-spoofing, `anonymous_tier` (`deny` vs `public`).
6. Three-tier read-only isolation (`public`, `club_media`, `club_scout`/`owner`).
7. Redacted audit logging (JSONL metadata, 0 plaintext tokens/job numbers).
8. All 6 atomic tools end-to-end (`kb_status`, `resolve_entity`,
   `get_person_dossier`, `get_club_roster`, `query_stats_and_games`,
   `verify_statement`).
9. Deterministic anchor verification (`story_record.py` & `wiki_draft_extractor.py`).
10. `youth_national_tenures` 4-state enum, `person_overseas_tenures` D-01 factual
    index + Markdown card & Wikipedia 2.0 static HTML rendering.
11. `cloud_state.py` bundle allowlist (6 new files) + explicit denylist
    (`mcp_acl.json`, `*secret*`, `*token*`).

All test data is 100% synthetic/redacted.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import pytest

# ---------------------------------------------------------------------------
# Synthetic credentials & constants (non-production, scanner-safe)
# ---------------------------------------------------------------------------
_BEARER_SCHEME = "Bearer"
_OWNER_CRED = "synth-owner-cred-alpha-901"
_SCOUT_CRED = "synth-scout-cred-bravo-902"
_MEDIA_CRED = "synth-media-cred-charlie-903"
_PUBLIC_CRED = "synth-public-cred-delta-904"
_DT_GW_CRED = "synth-dtgw-cred-echo-905"
_WRONG_CRED = "synth-wrong-cred-zulu-999"

_DT_OWNER_UID = "dt-uid-owner-alpha"
_DT_SCOUT_UID = "dt-uid-scout-bravo"
_DT_MEDIA_JOB = "JOB-MEDIA-7741"
_DT_UNKNOWN_UID = "dt-uid-guest-omega"

_PERS_ALPHA = "PERS-SYNTH-ALPHA001"
_PERS_SMITH_A = "PERS-SYNTH-SMITH001"
_PERS_SMITH_B = "PERS-SYNTH-SMITH002"
_PERS_COACH = "PERS-SYNTH-COACH001"
_PERS_EMPTY = "PERS-SYNTH-EMPTY001"

_ORG_SZ = "ORG-CBA-SHENZHEN"

_EVIDENCE_TEXT_STORY = (
    "2025年11月赛后发布会上，  林星宇  表示：\n"
    "我们在第三节通过联防限制了对手的外线投射，\n"
    "全队执行力非常出色。"
)
_EVIDENCE_BYTES_STORY = _EVIDENCE_TEXT_STORY.encode("utf-8")
_EVIDENCE_SHA_STORY = hashlib.sha256(_EVIDENCE_BYTES_STORY).hexdigest()
_EVIDENCE_QUOTE_SPAN = "我们在第三节通过联防限制了对手的外线投射， 全队执行力非常出色。"

_EVIDENCE_BYTES_OVERSEAS = (
    "Official NCAA roster record: Lin Xingyu played for Pacific State University "
    "during 2021-2023 and earned All-Conference Honorable Mention."
).encode("utf-8")
_EVIDENCE_SHA_OVERSEAS = hashlib.sha256(_EVIDENCE_BYTES_OVERSEAS).hexdigest()

_EVIDENCE_BYTES_YOUTH = (
    "中国篮协官方公告：林星宇入选2023年FIBA U19男篮世界杯中国队正赛最终名单，"
    "并参加了前期集训。"
).encode("utf-8")
_EVIDENCE_SHA_YOUTH = hashlib.sha256(_EVIDENCE_BYTES_YOUTH).hexdigest()

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _auth_header(cred: str) -> Dict[str, str]:
    return {"Authorization": f"{_BEARER_SCHEME} {cred}"}


def _make_acl_dict(*, anonymous_tier: str = "deny") -> Dict[str, Any]:
    return {
        "anonymous_tier": anonymous_tier,
        "bearer_tokens": {
            _OWNER_CRED: "owner",
            _SCOUT_CRED: "club_scout",
            _MEDIA_CRED: "club_media",
            _PUBLIC_CRED: "public",
        },
        "dingtalk_gateway": {
            "tokens": [_DT_GW_CRED],
            "user_id_tiers": {
                _DT_OWNER_UID: "owner",
                _DT_SCOUT_UID: "club_scout",
            },
            "job_number_tiers": {
                _DT_MEDIA_JOB: "club_media",
            },
            "default_authenticated_tier": "public",
        },
    }


def _build_synthetic_kb_dir(root: Path) -> Path:
    """Create a complete synthetic KB data directory for REQ-250 end-to-end tests."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "game_logs").mkdir(exist_ok=True)

    # 1. person_registry.json
    person_registry = {
        "schema": "cba-kb.person-registry.v1",
        "persons": [
            {
                "person_uid": _PERS_ALPHA,
                "canonical_name": "林星宇",
                "english_name": "Lin Xingyu",
                "aliases": ["小宇", "Lin XY"],
                "primary_club": "深圳马可波罗",
                "current_club": "深圳马可波罗",
                "primary_role": "PLAYER",
                "roles": ["PLAYER"],
                "role_tenures": [
                    {
                        "role": "PLAYER",
                        "organization_id": _ORG_SZ,
                        "organization_name": "深圳马可波罗",
                        "season_start": "2023-2024",
                        "season_end": "2026-2027",
                        "authority_scope": "LEAGUE_OFFICIAL",
                        "evidence_refs": ["https://example.org/cba/roster/sz-2026"],
                    }
                ],
            },
            {
                "person_uid": _PERS_SMITH_A,
                "canonical_name": "特奎维恩·史密斯",
                "english_name": "Terquavion Smith",
                "aliases": ["史密斯", "T. Smith"],
                "primary_club": "深圳马可波罗",
                "current_club": "深圳马可波罗",
                "primary_role": "PLAYER",
                "roles": ["PLAYER"],
                "role_tenures": [
                    {
                        "role": "PLAYER",
                        "organization_id": _ORG_SZ,
                        "organization_name": "深圳马可波罗",
                        "season_start": "2025-2026",
                        "season_end": "2026-2027",
                        "authority_scope": "LEAGUE_OFFICIAL",
                        "evidence_refs": ["https://example.org/cba/foreign/sz-smith"],
                    }
                ],
            },
            {
                "person_uid": _PERS_SMITH_B,
                "canonical_name": "马库斯·史密斯",
                "english_name": "Marcus Smith",
                "aliases": ["史密斯", "M. Smith"],
                "primary_club": "北京首钢",
                "current_club": "北京首钢",
                "primary_role": "PLAYER",
                "roles": ["PLAYER"],
                "role_tenures": [
                    {
                        "role": "PLAYER",
                        "organization_id": "ORG-CBA-BEIJING",
                        "organization_name": "北京首钢",
                        "season_start": "2025-2026",
                        "season_end": "2026-2027",
                        "authority_scope": "LEAGUE_OFFICIAL",
                        "evidence_refs": ["https://example.org/cba/foreign/bj-smith"],
                    }
                ],
            },
            {
                "person_uid": _PERS_COACH,
                "canonical_name": "郑永刚",
                "english_name": "Zheng Yonggang",
                "aliases": ["郑指导"],
                "primary_club": "深圳马可波罗",
                "current_club": "深圳马可波罗",
                "primary_role": "HEAD_COACH",
                "roles": ["HEAD_COACH", "ASSISTANT_COACH"],
                "role_tenures": [
                    {
                        "role": "HEAD_COACH",
                        "organization_id": _ORG_SZ,
                        "organization_name": "深圳马可波罗",
                        "season_start": "2024-2025",
                        "season_end": "2026-2027",
                        "authority_scope": "LEAGUE_OFFICIAL",
                        "evidence_refs": ["https://example.org/cba/coach/sz-2026"],
                    }
                ],
            },
        ],
    }
    (root / "person_registry.json").write_text(
        json.dumps(person_registry, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 2. person_bio_profiles.json (with verified + NEEDS_SOURCE / INTERNAL_CLUE + internal filename provenance)
    bio_profiles = {
        "schema": "cba-kb.person-bio-profiles.v1",
        "profiles": {
            _PERS_ALPHA: {
                "person_uid": _PERS_ALPHA,
                "canonical_name": "林星宇",
                "birth_date": "2003-04-18",
                "height_cm": 196,
                "weight_kg": 90,
                "position": "得分后卫",
                "youth_origin": "深圳青训体系",
                "verification_status": "VERIFIED",
                "internal_clue": "INTERNAL_CLUE: 球探内部评估认为其具备CBA首发潜力",
                "needs_source_note": "NEEDS_SOURCE: 传闻曾赴欧洲试训但尚无公开出处",
                "attribute_provenance": {
                    "height_cm": {
                        "status": "VERIFIED",
                        "source_file": "internal_scouting_sheet.csv",
                        "source_url": "https://example.org/cba/bio/lin-xingyu",
                    },
                    "draft_rumor": {
                        "status": "NEEDS_SOURCE",
                        "source_file": "rumor_notes.jsonl",
                    },
                },
            }
        },
    }
    (root / "person_bio_profiles.json").write_text(
        json.dumps(bio_profiles, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 3. organization_profiles.json
    org_profiles = {
        "schema": "cba-kb.organization.v1",
        "organizations": [
            {
                "schema": "cba-kb.organization.v1",
                "organization_id": _ORG_SZ,
                "canonical_name": "深圳新世纪烈豹",
                "short_name": "深圳马可波罗",
                "aliases": ["深圳", "深圳队", "深圳马可波罗", "深圳新世纪"],
                "league": "CBA",
                "city": "深圳",
            }
        ],
    }
    (root / "organization_profiles.json").write_text(
        json.dumps(org_profiles, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 4. MASTER.csv (domestic registration)
    master_csv = (
        "player_uid,person_uid,player_name,club,season,contract_type,jersey_number,position,height_cm,weight_kg\n"
        f"PL-SYNTH-001,{_PERS_ALPHA},林星宇,深圳马可波罗,2025-2026,B类,12,得分后卫,196,90\n"
        f"PL-SYNTH-001,{_PERS_ALPHA},林星宇,深圳马可波罗,2026-2027,B类,12,得分后卫,196,90\n"
    )
    (root / "MASTER.csv").write_text(master_csv, encoding="utf-8")

    # 5. season_stats_2025_2026.json (with PARTIAL_SEASONS_OBSERVED & FULL_CBA_CAREER)
    season_stats = [
        {
            "person_uid": _PERS_ALPHA,
            "player_name": "林星宇",
            "club": "深圳马可波罗",
            "season": "2024-2025",
            "match_type": "常规赛",
            "games_played": 30,
            "points_per_game": 14.2,
            "rebounds_per_game": 4.1,
            "assists_per_game": 3.5,
            "career_coverage_mode": "PARTIAL_SEASONS_OBSERVED",
            "source_url": "https://example.org/cba/stats/lin-2024-25",
        },
        {
            "person_uid": _PERS_ALPHA,
            "player_name": "林星宇",
            "club": "深圳马可波罗",
            "season": "2025-2026",
            "match_type": "常规赛",
            "games_played": 36,
            "points_per_game": 18.6,
            "rebounds_per_game": 5.0,
            "assists_per_game": 4.4,
            "career_coverage_mode": "PARTIAL_SEASONS_OBSERVED",
            "source_url": "https://example.org/cba/stats/lin-2025-26",
        },
        {
            "person_uid": _PERS_SMITH_A,
            "player_name": "特奎维恩·史密斯",
            "club": "深圳马可波罗",
            "season": "2025-2026",
            "match_type": "常规赛",
            "games_played": 28,
            "points_per_game": 24.5,
            "rebounds_per_game": 4.8,
            "assists_per_game": 6.2,
            "career_coverage_mode": "FULL_CBA_CAREER",
            "source_url": "https://example.org/cba/stats/smith-2025-26",
        },
    ]
    (root / "season_stats_2025_2026.json").write_text(
        json.dumps(season_stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 6. game_logs/lin_xingyu_2025_2026.jsonl (8 games so we can verify public cap vs scout full multi-filter)
    game_logs = [
        {
            "game_id": f"G-2025-0{idx}",
            "person_uid": _PERS_ALPHA,
            "player_name": "林星宇",
            "club": "深圳马可波罗",
            "opponent": opp,
            "season": "2025-2026",
            "match_type": "常规赛",
            "game_date": f"2025-11-{10 + idx:02d}",
            "points": pts,
            "rebounds": reb,
            "assists": ast,
        }
        for idx, (opp, pts, reb, ast) in enumerate(
            [
                ("广东宏远", 26, 6, 5),
                ("北京首钢", 15, 4, 3),
                ("辽宁本钢", 22, 5, 6),
                ("上海久事", 12, 3, 2),
                ("浙江广厦", 29, 7, 4),
                ("新疆伊力特", 19, 5, 5),
                ("广东宏远", 31, 8, 7),
                ("山东高速", 16, 4, 4),
            ],
            start=1,
        )
    ]
    (root / "game_logs" / "lin_xingyu_2025_2026.jsonl").write_text(
        "\n".join(json.dumps(g, ensure_ascii=False) for g in game_logs) + "\n",
        encoding="utf-8",
    )

    # 7. story_records.jsonl (1 public_safe with quote_anchors + internal_verbatim_or_note; 1 internal-only)
    story_public = {
        "schema": "cba-kb.story-record.v1",
        "story_id": "story_111111111111111111111111",
        "title": "林星宇谈第三节联防执行力",
        "occurred_season": "2025-2026",
        "occurred_date": "2025-11-18",
        "published_at": "2025-11-19",
        "canonical_source": {
            "source_id": "src-postgame-001",
            "title": "深圳队赛后发布会纪要",
            "url": "https://example.org/news/sz-postgame-001",
            "source_kind": "official_press_release",
            "publisher": "CBA官方媒体中心",
        },
        "deduped_reprints": [],
        "participants": [
            {
                "person_uid": _PERS_ALPHA,
                "display_name": "林星宇",
                "role": "SUBJECT_INTERVIEWEE",
                "internal_verbatim_or_note": "INTERNAL_ONLY_NOTE: 内部复盘提到外援体能出现波动 (source: internal_notes.jsonl)",
                "public_perspective_summary": "林星宇赛后肯定了球队第三节的联防执行力。",
            }
        ],
        "organization_ids": [_ORG_SZ],
        "organization_summary": "深圳队赛后总结第三节防守调整。",
        "visibility_policy": {"public_export_allowed": True},
        "quote_anchors": [
            {
                "quote_span": _EVIDENCE_QUOTE_SPAN,
                "author_or_outlet": "CBA官方媒体中心",
                "canonical_url": "https://example.org/news/sz-postgame-001",
                "raw_sha256": _EVIDENCE_SHA_STORY,
            }
        ],
    }
    story_internal = {
        "schema": "cba-kb.story-record.v1",
        "story_id": "story_222222222222222222222222",
        "title": "内部训练伤病观察记录",
        "occurred_season": "2025-2026",
        "occurred_date": "2025-11-20",
        "published_at": "2025-11-20",
        "canonical_source": {
            "source_id": "src-internal-clue-002",
            "title": "内部球探备忘",
            "url": "https://example.org/internal/memo-002",
            "source_kind": "internal_memo",
        },
        "deduped_reprints": [],
        "participants": [
            {
                "person_uid": _PERS_ALPHA,
                "display_name": "林星宇",
                "role": "CO_PARTICIPANT",
                "internal_verbatim_or_note": "INTERNAL_CLUE: 训练中轻微崴脚，未对外公开。",
                "public_perspective_summary": "训练观察记录。",
            }
        ],
        "organization_ids": [_ORG_SZ],
        "organization_summary": "内部训练备忘。",
        "visibility_policy": {"public_export_allowed": False},
    }
    (root / "story_records.jsonl").write_text(
        "\n".join(
            json.dumps(s, ensure_ascii=False) for s in (story_public, story_internal)
        )
        + "\n",
        encoding="utf-8",
    )

    # 8. club_season_roster_ledger.jsonl (5-way ledger)
    roster_ledger = [
        {
            "club": "深圳马可波罗",
            "season": "2025-2026",
            "person_uid": _PERS_ALPHA,
            "player_name": "林星宇",
            "movement_category": "RETAINED",
            "contract_type": "B类",
            "status": "ACTIVE",
        },
        {
            "club": "深圳马可波罗",
            "season": "2025-2026",
            "person_uid": _PERS_SMITH_A,
            "player_name": "特奎维恩·史密斯",
            "movement_category": "NEW_SIGNING_FOREIGN",
            "contract_type": "外援",
            "status": "ACTIVE",
        },
        {
            "club": "深圳马可波罗",
            "season": "2025-2026",
            "person_uid": "PERS-SYNTH-OUT001",
            "player_name": "周明远",
            "movement_category": "DEPARTED_TRANSFER",
            "contract_type": "C类",
            "status": "DEPARTED",
        },
        {
            "club": "深圳马可波罗",
            "season": "2025-2026",
            "person_uid": "PERS-SYNTH-LOAN001",
            "player_name": "陈浩然",
            "movement_category": "LOANED_OUT",
            "contract_type": "A1类",
            "status": "LOANED_OUT",
        },
        {
            "club": "深圳马可波罗",
            "season": "2025-2026",
            "person_uid": "PERS-SYNTH-PROMO001",
            "player_name": "许嘉诚",
            "movement_category": "YOUTH_PROMOTED",
            "contract_type": "A1类",
            "status": "ACTIVE",
        },
    ]
    (root / "club_season_roster_ledger.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in roster_ledger) + "\n",
        encoding="utf-8",
    )

    # 9. coach_role_facets.json (3-facet coaching staff view)
    coach_facets = {
        "schema": "cba-kb.coach-role-facets.v1",
        "clubs": {
            "深圳马可波罗": {
                "season": "2025-2026",
                "official_registered_head_coach": {
                    "person_uid": _PERS_COACH,
                    "name": "郑永刚",
                    "source_url": "https://example.org/cba/official-coach-sz",
                },
                "club_announced_head_coach": {
                    "person_uid": _PERS_COACH,
                    "name": "郑永刚",
                    "source_url": "https://example.org/sz/announced-coach",
                },
                "acting_or_on_court_head_coach": {
                    "person_uid": "PERS-SYNTH-ACT001",
                    "name": "迈克尔·凯利",
                    "note": "常规赛部分场次临场执行主教练",
                    "source_url": "https://example.org/sz/acting-coach",
                },
                "discrepancy_note": "官方注册主教练为郑永刚，部分场次临场指挥由助教迈克尔·凯利执行。",
            }
        },
    }
    (root / "coach_role_facets.json").write_text(
        json.dumps(coach_facets, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 10. person_overseas_tenures.jsonl (D-01 factual index)
    overseas_records = [
        {
            "schema": "cba-kb.person-overseas-tenure.v1",
            "person_uid": _PERS_ALPHA,
            "tenure_type": "COLLEGE_NCAA",
            "season_span": "2021-2023",
            "league_name": "NCAA Division I",
            "organization_name": "Pacific State University",
            "external_standard_ids": {"ncaa_id": "NCAA-SYNTH-8842"},
            "official_honors": ["All-Conference Honorable Mention (2022-23)"],
            "rights_tier": "D01_FACTUAL_INDEX_ONLY",
            "evidence_refs": [
                {
                    "source_title": "Pacific State Men's Basketball Roster",
                    "source_url": "https://example.org/ncaa/pacific-state/lin-xingyu",
                    "raw_sha256": _EVIDENCE_SHA_OVERSEAS,
                    "captured_at": "2026-01-15T00:00:00Z",
                }
            ],
        }
    ]
    (root / "person_overseas_tenures.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in overseas_records) + "\n",
        encoding="utf-8",
    )

    # 11. youth_national_tenures.jsonl (4-state enum)
    youth_records = [
        {
            "schema": "cba-kb.youth-national-tenure.v1",
            "person_uid": _PERS_ALPHA,
            "competition_or_program": "FIBA U19男篮世界杯",
            "season_or_year": "2023",
            "participation_stage": "FINAL_TOURNAMENT_ROSTER",
            "team_or_unit": "中国U19男篮",
            "stats_summary": "场均8.4分2.6篮板",
            "evidence_refs": [
                {
                    "source_title": "中国篮协U19正赛大名单公告",
                    "source_url": "https://example.org/cba/u19-roster-2023",
                    "raw_sha256": _EVIDENCE_SHA_YOUTH,
                    "captured_at": "2026-01-15T00:00:00Z",
                }
            ],
        }
    ]
    (root / "youth_national_tenures.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in youth_records) + "\n",
        encoding="utf-8",
    )

    # 12. operational_checkpoint.json & coverage_ledger.json
    (root / "operational_checkpoint.json").write_text(
        json.dumps(
            {
                "release_id": "v2.5.0-synth-20261128",
                "code_commit": "00000000",
                "observed_until": "2026-11-28",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (root / "coverage_ledger.json").write_text(
        json.dumps(
            {
                "observed_until": "2026-11-28",
                "domains": {"registration": "COMPLETE", "season_stats": "PARTIAL"},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return root


def _pack_dir_to_tarball(src_dir: Path, dest_tarball: Path) -> Path:
    with tarfile.open(dest_tarball, "w:gz") as tf:
        for path in sorted(src_dir.rglob("*")):
            if path.is_file():
                tf.add(path, arcname=path.relative_to(src_dir).as_posix())
    return dest_tarball


def _parse_mcp_tool_text(resp: Mapping[str, Any]) -> Dict[str, Any]:
    assert "result" in resp, f"Expected JSON-RPC result, got: {resp}"
    result = resp["result"]
    assert result.get("isError") is False, f"Tool returned error: {result}"
    content = result.get("content") or []
    assert content and content[0].get("type") == "text"
    return json.loads(content[0]["text"])


# ===========================================================================
# 1. SnowballMCPServer v2.3.0 Backward Compatibility
# ===========================================================================
def test_snowball_mcp_server_v230_backward_compat() -> None:
    """v2.3.0 SnowballMCPServer and MCP_PROTOCOL_VERSION='2024-11-05' must stay intact."""
    from cba_kb.mcp_server import MCP_PROTOCOL_VERSION, SnowballMCPServer

    assert MCP_PROTOCOL_VERSION == "2024-11-05"

    class _DummyOrchestrator:
        def get_or_acquire_person(self, person: str, **kwargs: Any) -> Dict[str, Any]:
            return {"person": person, "status": "FRESH_HIT", "season": kwargs.get("season")}

        def get_league_coverage(self) -> Dict[str, Any]:
            return {"observed_until": "2026-11-28", "domains": 5}

        def check_acquisition_job(self, job_id: str) -> Dict[str, Any]:
            return {"job_id": job_id, "status": "DONE"}

    server = SnowballMCPServer(_DummyOrchestrator())

    init_resp = server.handle_jsonrpc_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init_resp is not None
    assert init_resp["result"]["protocolVersion"] == "2024-11-05"

    list_resp = server.handle_jsonrpc_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert list_resp is not None
    tool_names = [t["name"] for t in list_resp["result"]["tools"]]
    assert tool_names == [
        "get_or_acquire_person",
        "get_league_coverage",
        "check_acquisition_job",
    ]

    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}) + "\n"
        + json.dumps({
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "get_or_acquire_person", "arguments": {"person": "林星宇"}},
        }) + "\n"
    )
    stdout = io.StringIO()
    server.serve_stdio(stdin=stdin, stdout=stdout)
    lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0]["result"] == {}
    payload = _parse_mcp_tool_text(lines[1])
    assert payload["person"] == "林星宇"
    assert payload["status"] == "FRESH_HIT"


# ===========================================================================
# 2. stdio JSON-RPC 2.0 Transport (TieredKnowledgeMCPServer)
# ===========================================================================
def test_stdio_transport_protocol_negotiation_ping_tools_list_and_call(tmp_path: Path) -> None:
    """TieredKnowledgeMCPServer supports stdio JSON-RPC 2.0 with 2025-03-26 & 2024-11-05."""
    from cba_kb.mcp_auth import AuthContext, MCPACLConfig
    from cba_kb.mcp_server import TieredKnowledgeMCPServer
    from cba_kb.mcp_tools import KnowledgeDataset

    kb_dir = _build_synthetic_kb_dir(tmp_path / "kb")
    dataset = KnowledgeDataset.from_data_dir(kb_dir)
    acl = MCPACLConfig.from_dict(_make_acl_dict()) if hasattr(MCPACLConfig, "from_dict") else None
    server = TieredKnowledgeMCPServer(dataset, acl=acl)
    owner_ctx = AuthContext(
        effective_tier="owner",
        client_kind="stdio",
        principal_masked="stdio:owner",
        authenticated=True,
    )

    # Protocol version negotiation: 2025-03-26 and 2024-11-05
    for ver in ("2025-03-26", "2024-11-05"):
        resp = server.handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": f"init-{ver}",
                "method": "initialize",
                "params": {"protocolVersion": ver},
            },
            auth_context=owner_ctx,
        )
        assert resp is not None
        assert resp["result"]["protocolVersion"] == ver

    # notifications/initialized produces None (no reply)
    notif = server.handle_jsonrpc_message(
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        auth_context=owner_ctx,
    )
    assert notif is None

    # stdio loop: ping + tools/list + tools/call
    req_lines = [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 10, "method": "ping"},
        {"jsonrpc": "2.0", "id": 11, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {"name": "kb_status", "arguments": {}},
        },
    ]
    stdin = io.StringIO("\n".join(json.dumps(r, ensure_ascii=False) for r in req_lines) + "\n")
    stdout = io.StringIO()
    server.serve_stdio(stdin=stdin, stdout=stdout, auth_context=owner_ctx)

    out_msgs = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert len(out_msgs) == 3  # notification omitted
    assert out_msgs[0]["id"] == 10 and out_msgs[0]["result"] == {}

    tools = out_msgs[1]["result"]["tools"]
    expected_tools = {
        "kb_status",
        "resolve_entity",
        "get_person_dossier",
        "get_club_roster",
        "query_stats_and_games",
        "verify_statement",
    }
    assert {t["name"] for t in tools} == expected_tools
    for tool in tools:
        assert _CJK_RE.search(tool["description"]), f"Tool {tool['name']} must have Chinese description"
        assert tool["inputSchema"].get("additionalProperties") is False, (
            f"Tool {tool['name']} inputSchema must set additionalProperties=False"
        )

    status_data = _parse_mcp_tool_text(out_msgs[2])
    assert status_data["release_id"] == "v2.5.0-synth-20261128"
    assert status_data["effective_tier"] == "owner"


# ===========================================================================
# 3. Stateless Streamable HTTP + Aliyun FC 3.0 & Tencent SCF Adapters
# ===========================================================================
def test_streamable_http_single_batch_notifications_healthz_405_204(tmp_path: Path) -> None:
    """Stateless Streamable HTTP handles single/batch POST, 202, 200 healthz, 405 GET, 204 OPTIONS/DELETE."""
    from cba_kb.mcp_auth import load_mcp_acl
    from cba_kb.mcp_http import handle_mcp_http_request
    from cba_kb.mcp_server import TieredKnowledgeMCPServer
    from cba_kb.mcp_tools import KnowledgeDataset

    kb_dir = _build_synthetic_kb_dir(tmp_path / "kb")
    acl_path = tmp_path / "mcp_acl.json"
    acl_path.write_text(json.dumps(_make_acl_dict(), ensure_ascii=False), encoding="utf-8")

    acl = load_mcp_acl(acl_path=acl_path)
    server = TieredKnowledgeMCPServer(KnowledgeDataset.from_data_dir(kb_dir), acl=acl)

    # GET /healthz -> 200 redacted readiness JSON
    status, headers, body = handle_mcp_http_request("GET", "/healthz", {}, b"", server=server, acl=acl)
    assert status == 200
    health = json.loads(body.decode("utf-8"))
    assert health["status"] == "ok"
    assert health["release_id"] == "v2.5.0-synth-20261128"
    assert health["observed_until"] == "2026-11-28"
    assert _OWNER_CRED not in body.decode("utf-8")
    assert _DT_GW_CRED not in body.decode("utf-8")

    # GET /mcp -> 405 Method Not Allowed with Allow: POST, OPTIONS
    status, headers, _ = handle_mcp_http_request("GET", "/mcp", {}, b"", server=server, acl=acl)
    assert status == 405
    allow_hdr = {k.lower(): v for k, v in headers.items()}.get("allow", "")
    assert "POST" in allow_hdr and "OPTIONS" in allow_hdr

    # OPTIONS /mcp & DELETE /mcp -> 204 No Content
    for verb in ("OPTIONS", "DELETE"):
        status, _, body = handle_mcp_http_request(verb, "/mcp", {}, b"", server=server, acl=acl)
        assert status == 204
        assert body == b""

    # POST /mcp with notifications/initialized -> 202 Accepted empty body
    auth_hdrs = {
        **_auth_header(_SCOUT_CRED),
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    status, _, body = handle_mcp_http_request(
        "POST",
        "/mcp",
        auth_hdrs,
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode("utf-8"),
        server=server,
        acl=acl,
    )
    assert status == 202
    assert body == b""

    # POST /mcp single initialize with Accept: application/json, text/event-stream (2025-03-26 & 2024-11-05)
    for proto_ver in ("2025-03-26", "2024-11-05"):
        status, headers, body = handle_mcp_http_request(
            "POST",
            "/mcp",
            auth_hdrs,
            json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": proto_ver},
            }).encode("utf-8"),
            server=server,
            acl=acl,
        )
        assert status == 200
        ctype = {k.lower(): v for k, v in headers.items()}.get("content-type", "")
        assert "application/json" in ctype.lower()
        parsed = json.loads(body.decode("utf-8"))
        assert parsed["result"]["protocolVersion"] == proto_ver

    # POST /mcp batch JSON-RPC 2.0
    batch_req = [
        {"jsonrpc": "2.0", "id": 101, "method": "ping"},
        {"jsonrpc": "2.0", "id": 102, "method": "tools/list"},
    ]
    status, _, body = handle_mcp_http_request(
        "POST",
        "/mcp",
        auth_hdrs,
        json.dumps(batch_req).encode("utf-8"),
        server=server,
        acl=acl,
    )
    assert status == 200
    batch_resp = json.loads(body.decode("utf-8"))
    assert isinstance(batch_resp, list) and len(batch_resp) == 2
    assert {item["id"] for item in batch_resp} == {101, 102}


def test_cloud_function_adapters_aliyun_fc3_and_tencent_scf(tmp_path: Path) -> None:
    """Aliyun FC 3.0 and Tencent SCF adapters dispatch stateless Streamable HTTP cleanly."""
    from cba_kb.mcp_auth import load_mcp_acl
    from cba_kb.mcp_http import make_aliyun_fc3_handler, make_tencent_scf_handler
    from cba_kb.mcp_server import TieredKnowledgeMCPServer
    from cba_kb.mcp_tools import KnowledgeDataset

    kb_dir = _build_synthetic_kb_dir(tmp_path / "kb")
    acl_path = tmp_path / "mcp_acl.json"
    acl_path.write_text(json.dumps(_make_acl_dict(), ensure_ascii=False), encoding="utf-8")

    acl = load_mcp_acl(acl_path=acl_path)
    server = TieredKnowledgeMCPServer(KnowledgeDataset.from_data_dir(kb_dir), acl=acl)

    fc3_handler = make_aliyun_fc3_handler(server, acl, mcp_path="/mcp")
    scf_handler = make_tencent_scf_handler(server, acl, mcp_path="/mcp")

    rpc_body = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"})

    # Aliyun FC 3.0 HTTP event dict
    fc3_event = {
        "rawPath": "/mcp",
        "requestContext": {"http": {"method": "POST", "path": "/mcp"}},
        "headers": {**_auth_header(_PUBLIC_CRED), "Content-Type": "application/json"},
        "body": rpc_body,
        "isBase64Encoded": False,
    }
    fc3_resp = fc3_handler(fc3_event, None)
    assert fc3_resp["statusCode"] == 200
    assert json.loads(fc3_resp["body"])["id"] == 7

    # Tencent SCF event dict
    scf_event = {
        "httpMethod": "POST",
        "path": "/mcp",
        "headers": {**_auth_header(_PUBLIC_CRED), "Content-Type": "application/json"},
        "body": rpc_body,
        "isBase64Encoded": False,
    }
    scf_resp = scf_handler(scf_event, None)
    assert scf_resp["statusCode"] == 200
    assert json.loads(scf_resp["body"])["id"] == 7


# ===========================================================================
# 4. --bundle-tarball Secure In-Memory Loading & Attack Rejection
# ===========================================================================
def test_bundle_tarball_safe_loading_and_rejects_traversal_symlink_bomb_and_acl(tmp_path: Path) -> None:
    """from_bundle_tarball loads valid bundles in-memory and rejects traversal, symlinks, bombs, and ACLs."""
    from cba_kb.mcp_tools import KnowledgeDataset

    kb_dir = _build_synthetic_kb_dir(tmp_path / "kb")
    good_tar = _pack_dir_to_tarball(kb_dir, tmp_path / "good_bundle.tar.gz")

    ds = KnowledgeDataset.from_bundle_tarball(good_tar)
    assert ds is not None
    # Verify loaded data is queryable
    from cba_kb.mcp_auth import AuthContext
    from cba_kb.mcp_server import TieredKnowledgeMCPServer

    server = TieredKnowledgeMCPServer(ds)
    resp = server.handle_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "kb_status", "arguments": {}}},
        auth_context=AuthContext("public", "stdio", "stdio:pub", True),
    )
    assert _parse_mcp_tool_text(resp)["release_id"] == "v2.5.0-synth-20261128"

    # 1) Path traversal (../evil.json)
    trav_tar = tmp_path / "traversal.tar.gz"
    with tarfile.open(trav_tar, "w:gz") as tf:
        payload = b'{"evil": true}'
        info = tarfile.TarInfo(name="../evil.json")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    with pytest.raises(ValueError):
        KnowledgeDataset.from_bundle_tarball(trav_tar)

    # 2) Symlink member
    sym_tar = tmp_path / "symlink.tar.gz"
    with tarfile.open(sym_tar, "w:gz") as tf:
        info = tarfile.TarInfo(name="person_registry.json")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    with pytest.raises(ValueError):
        KnowledgeDataset.from_bundle_tarball(sym_tar)

    # 3) Oversized / bomb tarball (exceeding max_extracted_bytes)
    bomb_tar = tmp_path / "bomb.tar.gz"
    with tarfile.open(bomb_tar, "w:gz") as tf:
        big = b"A" * 4096
        info = tarfile.TarInfo(name="operational_checkpoint.json")
        info.size = len(big)
        tf.addfile(info, io.BytesIO(big))
    with pytest.raises(ValueError):
        KnowledgeDataset.from_bundle_tarball(bomb_tar, max_extracted_bytes=1024)

    # 4) Forbidden ACL / secret file inside tarball
    acl_tar = tmp_path / "with_acl.tar.gz"
    with tarfile.open(acl_tar, "w:gz") as tf:
        acl_bytes = b'{"anonymous_tier": "public"}'
        info = tarfile.TarInfo(name="mcp_acl.json")
        info.size = len(acl_bytes)
        tf.addfile(info, io.BytesIO(acl_bytes))
    with pytest.raises(ValueError):
        KnowledgeDataset.from_bundle_tarball(acl_tar)


# ===========================================================================
# 5. Dual-Factor ACL Gateway (Bearer, stdio env, DingTalk, anonymous_tier)
# ===========================================================================
def test_acl_dual_factor_direct_bearer_and_stdio_env(tmp_path: Path) -> None:
    """Direct bearer-scheme token and stdio env resolve exact tiers and ignore X-DingTalk-* on Channel A."""
    from cba_kb.mcp_auth import (
        MCPAuthError,
        authenticate_http_headers,
        authenticate_stdio_env,
        load_mcp_acl,
    )

    acl_path = tmp_path / "mcp_acl.json"
    acl_path.write_text(json.dumps(_make_acl_dict(), ensure_ascii=False), encoding="utf-8")
    acl = load_mcp_acl(acl_path=acl_path)

    # Direct Bearer (Channel A) ignores forged X-DingTalk-User-Id
    ctx = authenticate_http_headers(
        {
            **_auth_header(_MEDIA_CRED),
            "X-DingTalk-User-Id": _DT_OWNER_UID,
        },
        acl,
    )
    assert ctx.effective_tier == "club_media"
    assert ctx.client_kind == "http_bearer"
    assert ctx.authenticated is True
    assert ctx.dingtalk_verified is False

    # stdio env with CBA_KB_MCP_TOKEN
    stdio_ctx = authenticate_stdio_env(acl, environ={"CBA_KB_MCP_TOKEN": _SCOUT_CRED})
    assert stdio_ctx.effective_tier == "club_scout"
    assert stdio_ctx.authenticated is True

    # Invalid stdio token must fail closed
    with pytest.raises(MCPAuthError):
        authenticate_stdio_env(acl, environ={"CBA_KB_MCP_TOKEN": _WRONG_CRED})


def test_acl_dingtalk_dual_factor_and_anti_spoofing(tmp_path: Path) -> None:
    """DingTalk headers without valid gateway auth are ignored/rejected; valid dual-factor maps tier."""
    from cba_kb.mcp_auth import MCPAuthError, authenticate_http_headers, load_mcp_acl

    acl_deny_path = tmp_path / "acl_deny.json"
    acl_deny_path.write_text(
        json.dumps(_make_acl_dict(anonymous_tier="deny"), ensure_ascii=False), encoding="utf-8"
    )
    acl_deny = load_mcp_acl(acl_path=acl_deny_path)

    acl_pub_path = tmp_path / "acl_pub.json"
    acl_pub_path.write_text(
        json.dumps(_make_acl_dict(anonymous_tier="public"), ensure_ascii=False), encoding="utf-8"
    )
    acl_pub = load_mcp_acl(acl_path=acl_pub_path)

    # Spoofed DingTalk UID & job number WITHOUT auth token (deny mode) -> 401
    with pytest.raises(MCPAuthError):
        authenticate_http_headers(
            {
                "X-DingTalk-User-Id": _DT_OWNER_UID,
                "X-DingTalk-User-Job-Number": _DT_MEDIA_JOB,
            },
            acl_deny,
        )

    # Spoofed DingTalk UID WITHOUT auth token (public mode) -> downgraded to public, NO escalation!
    spoof_pub = authenticate_http_headers(
        {
            "X-DingTalk-User-Id": _DT_OWNER_UID,
            "X-DingTalk-User-Job-Number": _DT_MEDIA_JOB,
        },
        acl_pub,
    )
    assert spoof_pub.effective_tier == "public"
    assert spoof_pub.dingtalk_verified is False
    assert spoof_pub.authenticated is False

    # Spoofed DingTalk UID with WRONG auth token -> 401
    with pytest.raises(MCPAuthError):
        authenticate_http_headers(
            {
                **_auth_header(_WRONG_CRED),
                "X-DingTalk-User-Id": _DT_OWNER_UID,
            },
            acl_pub,
        )

    # Valid gateway auth + whitelisted UID -> owner
    dt_owner = authenticate_http_headers(
        {
            **_auth_header(_DT_GW_CRED),
            "X-DingTalk-User-Id": _DT_OWNER_UID,
        },
        acl_deny,
    )
    assert dt_owner.effective_tier == "owner"
    assert dt_owner.client_kind == "dingtalk"
    assert dt_owner.dingtalk_verified is True

    # Valid gateway auth + whitelisted job number -> club_media
    dt_media = authenticate_http_headers(
        {
            **_auth_header(_DT_GW_CRED),
            "X-DingTalk-User-Job-Number": _DT_MEDIA_JOB,
        },
        acl_deny,
    )
    assert dt_media.effective_tier == "club_media"
    assert dt_media.dingtalk_verified is True

    # Valid gateway auth + non-whitelisted UID -> public
    dt_guest = authenticate_http_headers(
        {
            **_auth_header(_DT_GW_CRED),
            "X-DingTalk-User-Id": _DT_UNKNOWN_UID,
        },
        acl_deny,
    )
    assert dt_guest.effective_tier == "public"
    assert dt_guest.dingtalk_verified is True


def test_acl_anonymous_tier_deny_vs_public(tmp_path: Path) -> None:
    """Default anonymous_tier='deny' returns HTTP 401; explicit 'public' allows public_safe access."""
    from cba_kb.mcp_auth import load_mcp_acl
    from cba_kb.mcp_http import handle_mcp_http_request
    from cba_kb.mcp_server import TieredKnowledgeMCPServer
    from cba_kb.mcp_tools import KnowledgeDataset

    kb_dir = _build_synthetic_kb_dir(tmp_path / "kb")
    ds = KnowledgeDataset.from_data_dir(kb_dir)

    # Default deny
    acl_deny = load_mcp_acl(environ={"CBA_KB_MCP_ACL_JSON": json.dumps(_make_acl_dict(anonymous_tier="deny"))})
    srv_deny = TieredKnowledgeMCPServer(ds, acl=acl_deny)
    req_body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "kb_status", "arguments": {}},
    }).encode("utf-8")

    status, _, _ = handle_mcp_http_request("POST", "/mcp", {}, req_body, server=srv_deny, acl=acl_deny)
    assert status == 401

    # Explicit public
    acl_pub = load_mcp_acl(environ={"CBA_KB_MCP_ACL_JSON": json.dumps(_make_acl_dict(anonymous_tier="public"))})
    srv_pub = TieredKnowledgeMCPServer(ds, acl=acl_pub)
    status, _, body = handle_mcp_http_request("POST", "/mcp", {}, req_body, server=srv_pub, acl=acl_pub)
    assert status == 200
    data = _parse_mcp_tool_text(json.loads(body.decode("utf-8")))
    assert data["effective_tier"] == "public"


# ===========================================================================
# 6. Three-Tier Read-Only Isolation (public / club_media / club_scout|owner)
# ===========================================================================
def test_three_tier_read_only_isolation_public_media_scout(tmp_path: Path) -> None:
    """public physically strips internal notes/clues/quote_span/.csv/.jsonl; club_media unlocks quote_span; scout unlocks internal_full."""
    from cba_kb.mcp_auth import AuthContext
    from cba_kb.mcp_server import TieredKnowledgeMCPServer
    from cba_kb.mcp_tools import KnowledgeDataset

    kb_dir = _build_synthetic_kb_dir(tmp_path / "kb")
    server = TieredKnowledgeMCPServer(KnowledgeDataset.from_data_dir(kb_dir))

    def _call(tier: str, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        ctx = AuthContext(tier, "http_bearer", f"test:{tier}", True)
        resp = server.handle_jsonrpc_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}},
            auth_context=ctx,
        )
        return _parse_mcp_tool_text(resp)

    dossier_args = {
        "person_uid": _PERS_ALPHA,
        "sections": ["summary", "bio", "stories", "overseas", "youth_national", "references"],
    }

    # --- public tier ---
    pub_dossier = _call("public", "get_person_dossier", dossier_args)
    pub_serialized = json.dumps(pub_dossier, ensure_ascii=False)
    for forbidden in (
        "NEEDS_SOURCE",
        "INTERNAL_CLUE",
        "INTERNAL_ONLY_NOTE",
        "internal_verbatim_or_note",
        "quote_span",
        "quote_anchors",
        _EVIDENCE_QUOTE_SPAN,
        "internal_scouting_sheet.csv",
        "rumor_notes.jsonl",
        "internal_notes.jsonl",
        ".csv",
        ".jsonl",
        "story_222222222222222222222222",
    ):
        assert forbidden not in pub_serialized, f"Public dossier leaked forbidden token: {forbidden}"

    # --- club_media tier ---
    media_dossier = _call("club_media", "get_person_dossier", dossier_args)
    media_serialized = json.dumps(media_dossier, ensure_ascii=False)
    assert _EVIDENCE_QUOTE_SPAN in media_serialized, "club_media must unlock controlled quote_span"
    assert "INTERNAL_ONLY_NOTE" not in media_serialized
    assert "internal_verbatim_or_note" not in media_serialized
    assert ".csv" not in media_serialized
    assert ".jsonl" not in media_serialized

    # --- club_scout / owner tier ---
    scout_dossier = _call("club_scout", "get_person_dossier", dossier_args)
    scout_serialized = json.dumps(scout_dossier, ensure_ascii=False)
    assert "INTERNAL_ONLY_NOTE" in scout_serialized or "INTERNAL_CLUE" in scout_serialized
    assert "story_222222222222222222222222" in scout_serialized

    # --- game_logs isolation: public capped vs scout full multi-filter ---
    pub_games = _call(
        "public",
        "query_stats_and_games",
        {"mode": "game_logs", "person_uid": _PERS_ALPHA, "limit": 20},
    )
    scout_games = _call(
        "club_scout",
        "query_stats_and_games",
        {"mode": "game_logs", "person_uid": _PERS_ALPHA, "opponent": "广东宏远", "min_points": 25},
    )
    pub_list = pub_games.get("games") or pub_games.get("rows") or pub_games.get("records") or []
    scout_list = scout_games.get("games") or scout_games.get("rows") or scout_games.get("records") or []
    assert len(pub_list) <= 5, "Public game_logs must be capped to recent public summary"
    assert len(scout_list) == 2, "Scout multi-filter must return exact 2 games vs 广东宏远 with >=25 pts"
    assert all(g["points"] >= 25 and "广东" in g["opponent"] for g in scout_list)


# ===========================================================================
# 7. Redacted Audit Logging
# ===========================================================================
def test_redacted_audit_logging_contains_metadata_and_zero_plaintext_secrets(tmp_path: Path) -> None:
    """Audit JSONL records required metadata and NEVER writes plaintext tokens or job numbers."""
    from cba_kb.mcp_auth import MCPAuditLogger, load_mcp_acl
    from cba_kb.mcp_http import handle_mcp_http_request
    from cba_kb.mcp_server import TieredKnowledgeMCPServer
    from cba_kb.mcp_tools import KnowledgeDataset

    kb_dir = _build_synthetic_kb_dir(tmp_path / "kb")
    audit_path = tmp_path / "mcp_audit.jsonl"
    acl = load_mcp_acl(environ={"CBA_KB_MCP_ACL_JSON": json.dumps(_make_acl_dict())})
    logger = MCPAuditLogger(audit_log_path=audit_path)
    server = TieredKnowledgeMCPServer(
        KnowledgeDataset.from_data_dir(kb_dir),
        acl=acl,
        audit_logger=logger,
    )

    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "kb_status", "arguments": {}},
    }).encode("utf-8")

    # 1) Direct auth-header call
    s1, _, _ = handle_mcp_http_request("POST", "/mcp", _auth_header(_SCOUT_CRED), body, server=server, acl=acl)
    assert s1 == 200

    # 2) DingTalk dual-factor call with job number
    s2, _, _ = handle_mcp_http_request(
        "POST",
        "/mcp",
        {**_auth_header(_DT_GW_CRED), "X-DingTalk-User-Job-Number": _DT_MEDIA_JOB},
        body,
        server=server,
        acl=acl,
    )
    assert s2 == 200

    raw_log = audit_path.read_text(encoding="utf-8")
    for secret_val in (_SCOUT_CRED, _DT_GW_CRED, _DT_MEDIA_JOB, _OWNER_CRED):
        assert secret_val not in raw_log, f"Plaintext secret leaked to audit log: {secret_val}"

    entries = [json.loads(line) for line in raw_log.splitlines() if line.strip()]
    assert len(entries) >= 2
    for entry in entries:
        for req_key in (
            "timestamp_utc",
            "client_kind",
            "principal_masked",
            "effective_tier",
            "tool_name",
            "release_id",
            "latency_ms",
        ):
            assert req_key in entry, f"Missing audit field {req_key} in {entry}"
        assert entry["tool_name"] == "kb_status"
        assert entry["release_id"] == "v2.5.0-synth-20261128"
        assert isinstance(entry["latency_ms"], (int, float)) and entry["latency_ms"] >= 0


# ===========================================================================
# 8. All 6 Atomic Tools End-to-End
# ===========================================================================
def test_atomic_tools_resolve_entity_and_dossier_slicing(tmp_path: Path) -> None:
    """resolve_entity disambiguates shared surname (AMBIGUOUS) and get_person_dossier slices sections."""
    from cba_kb.mcp_auth import AuthContext
    from cba_kb.mcp_server import TieredKnowledgeMCPServer
    from cba_kb.mcp_tools import KnowledgeDataset

    kb_dir = _build_synthetic_kb_dir(tmp_path / "kb")
    server = TieredKnowledgeMCPServer(KnowledgeDataset.from_data_dir(kb_dir))
    ctx = AuthContext("club_scout", "stdio", "stdio:scout", True)

    def _call(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        resp = server.handle_jsonrpc_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}},
            auth_context=ctx,
        )
        return _parse_mcp_tool_text(resp)

    # Exact match -> RESOLVED
    res_exact = _call("resolve_entity", {"query": "林星宇"})
    assert res_exact["status"] == "RESOLVED"
    assert res_exact["clarification_needed"] is False
    assert res_exact["matches"][0]["entity_id"] == _PERS_ALPHA

    # Shared surname -> AMBIGUOUS (must not guess)
    res_ambig = _call("resolve_entity", {"query": "史密斯"})
    assert res_ambig["status"] == "AMBIGUOUS"
    assert res_ambig["clarification_needed"] is True
    assert len(res_ambig["matches"]) >= 2
    assert {_PERS_SMITH_A, _PERS_SMITH_B}.issubset({m["entity_id"] for m in res_ambig["matches"]})

    # Unknown -> NOT_FOUND
    res_none = _call("resolve_entity", {"query": "不存在的球员甲乙丙"})
    assert res_none["status"] == "NOT_FOUND"
    assert res_none["matches"] == []

    # Dossier section slicing
    sliced = _call("get_person_dossier", {"person_uid": _PERS_ALPHA, "sections": ["summary", "overseas"]})
    assert "overseas" in sliced or "overseas_tenures" in sliced
    assert "stories" not in sliced or sliced.get("stories") is None


def test_atomic_tools_club_roster_and_stats_coverage(tmp_path: Path) -> None:
    """get_club_roster returns 5-way ledger + 3-facet coaches; query_stats_and_games reports coverage mode."""
    from cba_kb.mcp_auth import AuthContext
    from cba_kb.mcp_server import TieredKnowledgeMCPServer
    from cba_kb.mcp_tools import KnowledgeDataset

    kb_dir = _build_synthetic_kb_dir(tmp_path / "kb")
    server = TieredKnowledgeMCPServer(KnowledgeDataset.from_data_dir(kb_dir))
    ctx = AuthContext("club_scout", "stdio", "stdio:scout", True)

    def _call(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        resp = server.handle_jsonrpc_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}},
            auth_context=ctx,
        )
        return _parse_mcp_tool_text(resp)

    roster = _call("get_club_roster", {"club": "深圳", "season": "2025-2026"})
    roster_text = json.dumps(roster, ensure_ascii=False)
    assert "林星宇" in roster_text
    assert "郑永刚" in roster_text
    assert "迈克尔·凯利" in roster_text
    # 5-way ledger categories represented
    for cat_marker in ("RETAINED", "NEW_SIGNING_FOREIGN", "DEPARTED_TRANSFER", "LOANED_OUT", "YOUTH_PROMOTED"):
        assert cat_marker in roster_text

    # Season stats deterministic calculation + PARTIAL_SEASONS_OBSERVED vs FULL_CBA_CAREER
    partial_stats = _call(
        "query_stats_and_games",
        {"mode": "season_stats", "person_uid": _PERS_ALPHA},
    )
    assert "PARTIAL_SEASONS_OBSERVED" in json.dumps(partial_stats, ensure_ascii=False)

    full_stats = _call(
        "query_stats_and_games",
        {"mode": "season_stats", "person_uid": _PERS_SMITH_A},
    )
    assert "FULL_CBA_CAREER" in json.dumps(full_stats, ensure_ascii=False)


def test_atomic_tool_verify_statement_four_verdicts_and_tier_stripping(tmp_path: Path) -> None:
    """verify_statement returns all 4 verdict types and strips quote_span for public tier."""
    from cba_kb.mcp_auth import AuthContext
    from cba_kb.mcp_server import TieredKnowledgeMCPServer
    from cba_kb.mcp_tools import KnowledgeDataset

    kb_dir = _build_synthetic_kb_dir(tmp_path / "kb")
    server = TieredKnowledgeMCPServer(KnowledgeDataset.from_data_dir(kb_dir))

    def _verify(tier: str, statement: str, **extra: Any) -> Dict[str, Any]:
        ctx = AuthContext(tier, "http_bearer", f"test:{tier}", True)
        resp = server.handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "verify_statement", "arguments": {"statement": statement, **extra}},
            },
            auth_context=ctx,
        )
        return _parse_mcp_tool_text(resp)

    def _extract_verdicts(res: Mapping[str, Any]) -> List[str]:
        items = res.get("claims") or res.get("sentences") or res.get("results") or []
        verdicts = [str(it.get("verdict") or "") for it in items if isinstance(it, Mapping)]
        if not verdicts and res.get("verdict"):
            verdicts = [str(res["verdict"])]
        return verdicts

    # 1) VERIFIED_MATCH
    v_match = _verify(
        "club_media",
        "林星宇在2025-2026赛季效力于深圳马可波罗。",
        person_hint="林星宇",
        club_hint="深圳马可波罗",
        season="2025-2026",
    )
    assert "VERIFIED_MATCH" in _extract_verdicts(v_match)

    # 2) CONTRADICTS_CANONICAL_DATA
    v_contra = _verify(
        "club_media",
        "林星宇在2025-2026赛季效力于北京首钢。",
        person_hint="林星宇",
        season="2025-2026",
    )
    assert "CONTRADICTS_CANONICAL_DATA" in _extract_verdicts(v_contra)

    # 3) MULTI_SOURCE_DISCREPANCY (3-facet coach: registered Zheng Yonggang vs acting Michael Kelly)
    v_disc = _verify(
        "club_media",
        "迈克尔·凯利是深圳马可波罗2025-2026赛季官方注册主教练。",
        club_hint="深圳马可波罗",
        season="2025-2026",
    )
    assert any(
        v in ("MULTI_SOURCE_DISCREPANCY", "CONTRADICTS_CANONICAL_DATA")
        for v in _extract_verdicts(v_disc)
    )
    assert "MULTI_SOURCE_DISCREPANCY" in _extract_verdicts(v_disc)

    # 4) UNVERIFIED_NO_BASIS
    v_unv = _verify(
        "club_media",
        "林星宇曾在火星篮球超级联赛夺得总决赛MVP。",
        person_hint="林星宇",
    )
    assert "UNVERIFIED_NO_BASIS" in _extract_verdicts(v_unv)

    # Public tier must NEVER receive quote_span from verify_statement
    v_pub = _verify(
        "public",
        "林星宇赛后肯定了球队第三节的联防执行力。",
        person_hint="林星宇",
    )
    pub_text = json.dumps(v_pub, ensure_ascii=False)
    assert "quote_span" not in pub_text
    assert _EVIDENCE_QUOTE_SPAN not in pub_text


# ===========================================================================
# 9. Deterministic Anchor Verification (story_record + wiki_draft_extractor)
# ===========================================================================
def test_story_record_quote_anchors_and_public_safe_stripping() -> None:
    """StoryRecord validates quote_anchors and strips them in public_safe projection."""
    from cba_kb.story_record import make_story_record, project_stories_for_entity

    story = make_story_record(
        title="林星宇谈第三节联防执行力",
        occurred_season="2025-2026",
        published_at="2025-11-19",
        canonical_source={
            "source_id": "src-001",
            "title": "赛后发布会",
            "url": "https://example.org/news/postgame-001",
        },
        participants=[
            {
                "person_uid": _PERS_ALPHA,
                "display_name": "林星宇",
                "role": "SUBJECT_INTERVIEWEE",
                "internal_verbatim_or_note": "INTERNAL_NOTE_SECRET",
                "public_perspective_summary": "肯定了联防执行力。",
            }
        ],
        organization_ids=[_ORG_SZ],
        visibility_policy={"public_export_allowed": True},
        quote_anchors=[
            {
                "quote_span": _EVIDENCE_QUOTE_SPAN,
                "author_or_outlet": "CBA官方媒体中心",
                "canonical_url": "https://example.org/news/postgame-001",
                "raw_sha256": _EVIDENCE_SHA_STORY,
            }
        ],
    )
    assert len(story["quote_anchors"]) == 1

    pub_proj = project_stories_for_entity([story], entity_id=_PERS_ALPHA, visibility="public_safe")
    assert len(pub_proj) == 1
    assert "quote_anchors" not in pub_proj[0]
    assert "internal_verbatim_or_note" not in pub_proj[0]

    int_proj = project_stories_for_entity([story], entity_id=_PERS_ALPHA, visibility="internal_full")
    assert len(int_proj) == 1
    assert int_proj[0]["quote_anchors"][0]["quote_span"] == _EVIDENCE_QUOTE_SPAN


def test_wiki_draft_extractor_deterministic_anchor_verification() -> None:
    """verify_quote_anchor & verify_candidate_anchors enforce exact sha256 + normalized substring match."""
    from cba_kb.wiki_draft_extractor import (
        ANCHOR_VERIFICATION_FAILED,
        verify_candidate_anchors,
        verify_quote_anchor,
    )

    # Valid anchor (whitespace normalized across newlines/spaces)
    ok, reason = verify_quote_anchor(_EVIDENCE_QUOTE_SPAN, _EVIDENCE_SHA_STORY, _EVIDENCE_BYTES_STORY)
    assert ok is True and reason is None

    # Fake / mismatched raw_sha256
    fake_sha = "0" * 64
    ok_bad_sha, reason_sha = verify_quote_anchor(_EVIDENCE_QUOTE_SPAN, fake_sha, _EVIDENCE_BYTES_STORY)
    assert ok_bad_sha is False and reason_sha

    # Valid sha256 but quote_span NOT a substring of evidence
    ok_bad_span, reason_span = verify_quote_anchor(
        "这段话根本没有出现在原始证据文本里", _EVIDENCE_SHA_STORY, _EVIDENCE_BYTES_STORY
    )
    assert ok_bad_span is False and reason_span

    # verify_candidate_anchors blocks invalid candidates and keeps valid ones
    evidence_index = {_EVIDENCE_SHA_STORY: _EVIDENCE_BYTES_STORY}
    candidates = [
        {
            "candidate_id": "cand-good",
            "review_status": "DRAFT_PENDING_HUMAN_REVIEW",
            "quote_anchors": [
                {
                    "quote_span": _EVIDENCE_QUOTE_SPAN,
                    "author_or_outlet": "CBA官方媒体中心",
                    "canonical_url": "https://example.org/news/postgame-001",
                    "raw_sha256": _EVIDENCE_SHA_STORY,
                }
            ],
        },
        {
            "candidate_id": "cand-fake-sha",
            "review_status": "DRAFT_PENDING_HUMAN_REVIEW",
            "quote_anchors": [
                {
                    "quote_span": _EVIDENCE_QUOTE_SPAN,
                    "author_or_outlet": "CBA官方媒体中心",
                    "canonical_url": "https://example.org/news/postgame-001",
                    "raw_sha256": fake_sha,
                }
            ],
        },
        {
            "candidate_id": "cand-fake-span",
            "review_status": "DRAFT_PENDING_HUMAN_REVIEW",
            "quote_anchors": [
                {
                    "quote_span": "凭空捏造的引文内容",
                    "author_or_outlet": "CBA官方媒体中心",
                    "canonical_url": "https://example.org/news/postgame-001",
                    "raw_sha256": _EVIDENCE_SHA_STORY,
                }
            ],
        },
    ]

    report = verify_candidate_anchors(candidates, evidence_index=evidence_index)
    approvable = report.get("approvable_candidates") or report.get("approved_candidates") or []
    assert [c["candidate_id"] for c in approvable] == ["cand-good"]

    rejected = report.get("rejected_candidates") or [
        c for c in (report.get("candidates") or []) if c.get("review_status") == ANCHOR_VERIFICATION_FAILED
    ]
    assert {c["candidate_id"] for c in rejected} == {"cand-fake-sha", "cand-fake-span"}
    for item in rejected:
        assert item["review_status"] == ANCHOR_VERIFICATION_FAILED
    assert report.get("anchor_verification_errors")


# ===========================================================================
# 10. Youth 4-State Enum, Overseas D-01, Markdown Card & Static HTML Rendering
# ===========================================================================
def test_youth_national_tenures_four_state_enum_validation() -> None:
    """youth_national_tenures enforces strict 4-state participation_stage enum."""
    from cba_kb.youth_tenure import (
        YOUTH_NATIONAL_TENURE_SCHEMA,
        YOUTH_PARTICIPATION_STAGES,
        YouthTenureError,
        validate_youth_national_tenure,
    )

    assert YOUTH_PARTICIPATION_STAGES == frozenset({
        "TRAINING_CAMP_CALLUP",
        "WARMUP_APPEARANCE",
        "FINAL_TOURNAMENT_ROSTER",
        "OFFICIAL_TOURNAMENT_STATS",
    })

    for stage in YOUTH_PARTICIPATION_STAGES:
        rec = validate_youth_national_tenure({
            "schema": YOUTH_NATIONAL_TENURE_SCHEMA,
            "person_uid": _PERS_ALPHA,
            "competition_or_program": "FIBA U19男篮世界杯",
            "season_or_year": "2023",
            "participation_stage": stage,
            "team_or_unit": "中国U19男篮",
            "evidence_refs": [
                {
                    "source_title": "官方公告",
                    "source_url": "https://example.org/cba/u19-2023",
                    "raw_sha256": _EVIDENCE_SHA_YOUTH,
                    "captured_at": "2026-01-15T00:00:00Z",
                }
            ],
        })
        assert rec["participation_stage"] == stage

    # Reject conflated / invalid stage
    for bad_stage in ("NATIONAL_TEAM_PLAYER", "CALLUP_AND_STATS", "入选集训", ""):
        with pytest.raises(YouthTenureError):
            validate_youth_national_tenure({
                "schema": YOUTH_NATIONAL_TENURE_SCHEMA,
                "person_uid": _PERS_ALPHA,
                "competition_or_program": "FIBA U19男篮世界杯",
                "season_or_year": "2023",
                "participation_stage": bad_stage,
                "team_or_unit": "中国U19男篮",
                "evidence_refs": [
                    {
                        "source_title": "官方公告",
                        "source_url": "https://example.org/cba/u19-2023",
                        "raw_sha256": _EVIDENCE_SHA_YOUTH,
                        "captured_at": "2026-01-15T00:00:00Z",
                    }
                ],
            })


def test_overseas_tenures_d01_factual_index_and_long_narrative_rejection() -> None:
    """person_overseas_tenures enforces D01_FACTUAL_INDEX_ONLY and rejects long narratives."""
    from cba_kb.overseas_tenure import (
        D01_RIGHTS_TIER,
        OVERSEAS_TENURE_SCHEMA,
        OverseasTenureError,
        validate_overseas_tenure,
    )

    base_rec = {
        "schema": OVERSEAS_TENURE_SCHEMA,
        "person_uid": _PERS_ALPHA,
        "tenure_type": "COLLEGE_NCAA",
        "season_span": "2021-2023",
        "league_name": "NCAA Division I",
        "organization_name": "Pacific State University",
        "external_standard_ids": {"ncaa_id": "NCAA-SYNTH-8842"},
        "official_honors": ["All-Conference Honorable Mention"],
        "rights_tier": D01_RIGHTS_TIER,
        "evidence_refs": [
            {
                "source_title": "Pacific State Roster",
                "source_url": "https://example.org/ncaa/pacific-state/lin",
                "raw_sha256": _EVIDENCE_SHA_OVERSEAS,
                "captured_at": "2026-01-15T00:00:00Z",
            }
        ],
    }
    valid = validate_overseas_tenure(base_rec)
    assert valid["rights_tier"] == "D01_FACTUAL_INDEX_ONLY"

    # Reject non-D01 rights_tier
    bad_rights = {**base_rec, "rights_tier": "FULL_TEXT_SCRAPE"}
    with pytest.raises(OverseasTenureError):
        validate_overseas_tenure(bad_rights)

    # Reject long narrative (> 280 chars)
    long_narrative = {**base_rec, "narrative": "这是一段很长的海外媒体版权长文摘录。" * 30}
    with pytest.raises(OverseasTenureError):
        validate_overseas_tenure(long_narrative)


def test_person_card_and_static_site_wiki_v241_deep_bio_rendering(tmp_path: Path) -> None:
    """Markdown card & Wikipedia 2.0 HTML render overseas/youth sections cleanly with no empty headers or .csv/.jsonl leaks."""
    from cba_kb.person_card_builder import build_person_card
    from cba_kb.static_site_builder import build_public_site

    overseas_tenures = [
        {
            "schema": "cba-kb.person-overseas-tenure.v1",
            "person_uid": _PERS_ALPHA,
            "tenure_type": "COLLEGE_NCAA",
            "season_span": "2021-2023",
            "league_name": "NCAA Division I",
            "organization_name": "Pacific State University",
            "external_standard_ids": {"ncaa_id": "NCAA-SYNTH-8842"},
            "official_honors": ["All-Conference Honorable Mention"],
            "rights_tier": "D01_FACTUAL_INDEX_ONLY",
            "evidence_refs": [
                {
                    "source_title": "Pacific State Roster",
                    "source_url": "https://example.org/ncaa/pacific-state/lin",
                    "raw_sha256": _EVIDENCE_SHA_OVERSEAS,
                    "captured_at": "2026-01-15T00:00:00Z",
                }
            ],
        }
    ]
    youth_tenures = [
        {
            "schema": "cba-kb.youth-national-tenure.v1",
            "person_uid": _PERS_ALPHA,
            "competition_or_program": "FIBA U19男篮世界杯",
            "season_or_year": "2023",
            "participation_stage": "FINAL_TOURNAMENT_ROSTER",
            "team_or_unit": "中国U19男篮",
            "stats_summary": "场均8.4分2.6篮板",
            "evidence_refs": [
                {
                    "source_title": "中国篮协U19正赛大名单公告",
                    "source_url": "https://example.org/cba/u19-roster-2023",
                    "raw_sha256": _EVIDENCE_SHA_YOUTH,
                    "captured_at": "2026-01-15T00:00:00Z",
                }
            ],
        }
    ]
    person_registry = {
        "schema": "cba-kb.person-registry.v1",
        "persons": [
            {
                "person_uid": _PERS_ALPHA,
                "canonical_name": "林星宇",
                "english_name": "Lin Xingyu",
                "primary_role": "PLAYER",
                "roles": ["PLAYER"],
                "role_tenures": [],
            },
            {
                "person_uid": _PERS_EMPTY,
                "canonical_name": "周清扬",
                "english_name": "Zhou Qingyang",
                "primary_role": "PLAYER",
                "roles": ["PLAYER"],
                "role_tenures": [],
            },
        ],
    }

    # Card WITH overseas & youth tenures
    card_with = build_person_card(
        "林星宇",
        season="2025-2026",
        visibility="public_safe",
        card_style="wiki_v241",
        person_registry=person_registry,
        overseas_tenures=overseas_tenures,
        youth_national_tenures=youth_tenures,
    )
    md_with = card_with["markdown"]
    assert "## 海外及旅外经历（D-01 事实索引）" in md_with
    assert "## 国字号及青年赛事经历" in md_with
    assert "正赛最终名单" in md_with
    assert "Pacific State University" in md_with
    assert ".csv" not in md_with.lower()
    assert ".jsonl" not in md_with.lower()

    # Card WITHOUT overseas & youth tenures -> NO empty section headers
    card_without = build_person_card(
        "周清扬",
        season="2025-2026",
        visibility="public_safe",
        card_style="wiki_v241",
        person_registry=person_registry,
        overseas_tenures=[],
        youth_national_tenures=[],
    )
    md_without = card_without["markdown"]
    assert "## 海外及旅外经历（D-01 事实索引）" not in md_without
    assert "## 国字号及青年赛事经历" not in md_without

    # Static HTML site rendering
    club_card = {
        "club": "深圳马可波罗",
        "season": "2025-2026",
        "visibility": "public_safe",
        "card_style": "wiki_v241",
        "markdown": "# 深圳马可波罗\n\n## 球队简介\n\n深圳马可波罗是CBA联赛俱乐部。\n",
        "citations": [],
    }
    site_dir = tmp_path / "public_site"
    build_public_site(
        club_card=club_card,
        person_cards=[card_with, card_without],
        output_dir=site_dir,
        site_title="CBA-KB 公开预览",
        observed_until="2026-11-28",
    )

    html_files = list(site_dir.rglob("*.html"))
    assert html_files
    combined_html = "\n".join(p.read_text(encoding="utf-8") for p in html_files)
    assert 'href="https://example.org/ncaa/pacific-state/lin"' in combined_html
    assert 'href="https://example.org/cba/u19-roster-2023"' in combined_html
    assert "正赛最终名单" in combined_html
    assert ".csv" not in combined_html.lower()
    assert ".jsonl" not in combined_html.lower()


# ===========================================================================
# 11. cloud_state.py Bundle Allowlist & Secret/ACL Denylist
# ===========================================================================
def test_cloud_state_bundle_allowlist_and_secret_denylist() -> None:
    """cloud_state allowlists the 6 new REQ-250 files and fail-closed blocks mcp_acl.json, *secret*, *token*."""
    from cba_kb.cloud_state import _bundle_allowlisted_exact_names, _is_bundle_member_allowed

    allowlist = _bundle_allowlisted_exact_names()
    required_new_files = {
        "club_season_roster_ledger.jsonl",
        "registration_time_slices.jsonl",
        "person_overseas_tenures.jsonl",
        "youth_national_tenures.jsonl",
        "shenzhen_pilot_context.json",
        "coach_role_facets.json",
    }
    assert required_new_files.issubset(allowlist), (
        f"Missing REQ-250 bundle allowlist files: {required_new_files - allowlist}"
    )

    for fname in required_new_files:
        assert _is_bundle_member_allowed(fname, allowlist) is True
        assert _is_bundle_member_allowed(f"snapshot/{fname}", allowlist) is True

    forbidden_members = [
        "mcp_acl.json",
        "config/mcp_acl.json",
        "MCP_ACL.JSON",
        "my_secret.json",
        "secrets/runtime.json",
        "api_token.json",
        "nested/bearer_tokens.jsonl",
        "season_stats_secret.json",
        "game_logs/token_dump.jsonl",
    ]
    for bad in forbidden_members:
        assert _is_bundle_member_allowed(bad, allowlist) is False, (
            f"Forbidden secret/ACL file was allowed into bundle: {bad}"
        )
