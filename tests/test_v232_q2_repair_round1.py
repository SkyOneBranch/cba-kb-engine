"""Differential falsification & regression tests for REQ-232 Q2 Review Round 1 findings.

100% synthetic offline fixtures (G5 == 0, G6 == 0).
Covers:
- B-P04-1: file://host/... bypass in kc_render and kc_visibility
- B-P04-2: Untitled bundle doc_id fallback leak in person_card_builder / kc_render / kc_visibility
- M-P04-1: Case-insensitive forbidden marker matching (WEB 剪藏 / web 剪藏 / ## document passage)
- M-P04-2: Drive-ID heuristic holes (27+ alpha-only mixed-case & dot-glued Drive IDs)
- M-P04-3: render_research_card public_safe gating on §2 claims, §5 revisions, §7 game revisions
           + fenced code block / prior_doc_id / new_record_id rejection in verify_public_safe_card_text
- M-P04-4: 10 sources / 10-sources / 10 SOURCES space & hyphen variants
- m-Q1-1:  archive_compactor dual-key conflict fail-closed check
- m-P02-1: PLAYER_COACH / 球员兼教练 treated as player (not NOT_APPLICABLE)
- m-P01-1: Missing match_type / scope in season_stats_table uses UNKNOWN (never silent REGULAR/WHOLE_SEASON)
- m-P04-5: Exact > [HISTORICAL_SEASON_ARCHIVE] banner allowed while other > blockquotes rejected
"""
from __future__ import annotations

import pytest

from cba_kb.archive_compactor import (
    ArchiveCompactionError,
    _validate_authoritative_release_state,
)
from cba_kb.consumer_surface import DomainHorizonStatus, build_person_domain_horizons
from cba_kb.kc_render import render_research_card
from cba_kb.kc_visibility import verify_public_safe_card_text
from cba_kb.person_card_builder import build_person_card

_CLEAN_PUBLIC_CARD = (
    "# Research Card (Serving Projection)\n"
    "- Visibility: PUBLIC_SAFE\n\n"
    "## Statistical Evidence\n"
    "- Posture: UNKNOWN\n"
    "- Season: 2026-2027\n\n"
    "end_marker: true\n"
)


def test_b_p04_1_file_scheme_host_variant_blocked() -> None:
    # verify_public_safe_card_text must reject file://host/... (not just file:///)
    for sample in (
        _CLEAN_PUBLIC_CARD + "\n- URI: file://internal-host/share/secret.md\n",
        _CLEAN_PUBLIC_CARD + "\n- URI: FILE://localhost/private/notes.txt\n",
    ):
        with pytest.raises(ValueError, match="PUBLIC_SAFE_REDACTION_VIOLATION"):
            verify_public_safe_card_text(sample)

    # render_research_card(visibility="public_safe") must strip file://host/... locators & citations
    payload = {
        "metric": "points_per_game",
        "grains": {"STATS": {"posture": "UNKNOWN", "season": "2026-2027"}},
        "evidence_transparency": {
            "posture": "UNKNOWN",
            "unknown_subkind": "NO_SEASON_STATS_RECORDED_YET",
            "scope_meta": {
                "season": "2026-2027",
                "locator": "file://nas-box/private/clip.md",
            },
        },
        "external_citations": [
            {
                "doc_id": "doc:pub-file-leak",
                "source_tier": "T1",
                "completeness": "full",
                "published_at": "2026-11-20",
                "title": "Mirror file://nas-box/private/clip.md",
                "canonical_url": "https://example.invalid/official/clean-1",
            },
        ],
    }
    md = render_research_card(payload, evidence_verified=True, visibility="public_safe")
    assert "file://" not in md.lower()
    verify_public_safe_card_text(md)


def test_b_p04_2_untitled_bundle_does_not_leak_doc_id() -> None:
    # 1. verify_public_safe_card_text rejects bare doc_id: labels (case-insensitive)
    for sample in (
        _CLEAN_PUBLIC_CARD + "\n- doc_id: internal-ref-42\n",
        _CLEAN_PUBLIC_CARD + "\n- DOC_ID: INTERNAL_99\n",
    ):
        with pytest.raises(ValueError, match="PUBLIC_SAFE_REDACTION_VIOLATION"):
            verify_public_safe_card_text(sample)

    # 2. render_research_card public branch never falls back to cite["doc_id"]
    payload = {
        "metric": "points_per_game",
        "grains": {"STATS": {"posture": "UNKNOWN", "season": "2026-2027"}},
        "evidence_transparency": {
            "posture": "UNKNOWN",
            "unknown_subkind": "NO_SEASON_STATS_RECORDED_YET",
            "scope_meta": {"season": "2026-2027"},
        },
        "external_citations": [
            {
                "doc_id": "internal_opaque_doc_999",
                "source_tier": "T1",
                "completeness": "full",
                "published_at": "2026-11-20",
                "title": "",
                "canonical_url": "https://example.invalid/official/untitled-1",
            }
        ],
    }
    rendered = render_research_card(payload, evidence_verified=True, visibility="public_safe")
    assert "internal_opaque_doc_999" not in rendered
    assert "https://example.invalid/official/untitled-1" in rendered
    verify_public_safe_card_text(rendered)

    # 3. build_person_card public_safe with untitled doc bundle does not leak doc_id as title
    master_rows = [
        {
            "season": "2026-2027",
            "club_official": "合成修补俱乐部",
            "player": "合成修补甲",
            "player_uid": "player:synth-r1-01",
            "player_id": "SYNTH_R101",
            "contract_category": "C类",
            "contract_term_official": "2028-07-31",
            "registration_status": "已注册",
        }
    ]
    doc_bundles = [
        {
            "doc_id": "internal_bundle_secret_777",
            "source_tier": "T1",
            "completeness": "full",
            "published_at": "2026-11-20",
            "title": None,
            "canonical_url": "https://example.invalid/official/untitled-2",
            "matched_persons": ["合成修补甲"],
        }
    ]
    res = build_person_card(
        "合成修补甲",
        season="2026-2027",
        master_rows=master_rows,
        document_bundles=doc_bundles,
        visibility="public_safe",
    )
    assert res["status"] == "BUILT"
    assert "internal_bundle_secret_777" not in res["markdown"]
    assert "https://example.invalid/official/untitled-2" in res["markdown"]
    verify_public_safe_card_text(res["markdown"])


def test_m_p04_1_case_insensitive_forbidden_markers() -> None:
    for variant in ("WEB 剪藏/item.md", "web 剪藏/item.md", "WeB 剪藏/item.md", "## document passage"):
        with pytest.raises(ValueError, match="PUBLIC_SAFE_REDACTION_VIOLATION"):
            verify_public_safe_card_text(_CLEAN_PUBLIC_CARD + f"\n- Ref: {variant}\n")


def test_m_p04_2_drive_id_heuristic_alpha_only_and_dot_glued() -> None:
    # (a) 27+ char mixed-case alpha-only token without digits outside URLs
    alpha_drive_like = "AbCdEfGhIjKlMnOpQrStUvWxYzA"
    with pytest.raises(ValueError, match="PUBLIC_SAFE_REDACTION_VIOLATION"):
        verify_public_safe_card_text(_CLEAN_PUBLIC_CARD + f"\n- Ref: {alpha_drive_like}\n")

    # (b) dot-glued Drive ID
    dot_glued = "ref.1AbCdEfGhIjKlMnOpQrStUv123456.end"
    with pytest.raises(ValueError, match="PUBLIC_SAFE_REDACTION_VIOLATION"):
        verify_public_safe_card_text(_CLEAN_PUBLIC_CARD + f"\n- Ref: {dot_glued}\n")

    # Clean https:// URL with long path segment must still pass
    clean_with_url = (
        _CLEAN_PUBLIC_CARD
        + "\n- Public: https://example.invalid/official/AbCdEfGhIjKlMnOpQrStUvWxYzA/report\n"
    )
    verify_public_safe_card_text(clean_with_url)


def test_m_p04_3_public_safe_gates_claims_revisions_and_game_revisions() -> None:
    # Direct leak guard checks for fenced code blocks and internal revision id keys
    for leaked in (
        _CLEAN_PUBLIC_CARD + "\n```text\nverbatim block\n```\n",
        _CLEAN_PUBLIC_CARD + "\n- prior_doc_id: old-1\n",
        _CLEAN_PUBLIC_CARD + "\n- new_doc_id: new-2\n",
        _CLEAN_PUBLIC_CARD + "\n- prior_record_id: rec-old\n",
        _CLEAN_PUBLIC_CARD + "\n- new_record_id: rec-new\n",
    ):
        with pytest.raises(ValueError, match="PUBLIC_SAFE_REDACTION_VIOLATION"):
            verify_public_safe_card_text(leaked)

    # Full render_research_card(visibility="public_safe") with §2, §5, §7 populated
    payload = {
        "metric": "points_per_game",
        "grains": {"STATS": {"posture": "UNKNOWN", "season": "2026-2027"}},
        "evidence_transparency": {
            "posture": "UNKNOWN",
            "unknown_subkind": "NO_SEASON_STATS_RECORDED_YET",
            "scope_meta": {"season": "2026-2027"},
            "claim_transparency": {
                "posture": "REVISED",
                "revision_history": [
                    {
                        "claim_id": "claim-002",
                        "status": "corroborated",
                        "statement": "合成公告摘要\n> 内部逐字摘录\n```md\nraw\n```",
                        "prior_claim_id": "claim-001",
                    }
                ],
            },
        },
        "claim_layer": {"claims": [{"claim_id": "claim-002"}]},
        "revision_disclosures": [
            {
                "disclosure_posture": "LOUD_REVISION",
                "prior_doc_id": "doc:secret-old-1",
                "new_doc_id": "doc:secret-new-2",
                "revision_seq": 2,
                "disclosure_notice": "doc:secret-new-2 supersedes doc:secret-old-1\n> leaked quote",
            }
        ],
        "recent_games_summary": {
            "window": 5,
            "played_games_in_window": 0,
            "dnp_games_count": 0,
            "averages": {},
            "shooting_totals": {},
            "games": [],
            "revision_summaries": [
                {
                    "disclosure_posture": "LOUD_REVISION",
                    "prior_record_id": "stats:internal-old-1",
                    "new_record_id": "stats:internal-new-2",
                    "disclosure_notice": "new_record_id=stats:internal-new-2 supersedes prior_record_id=stats:internal-old-1",
                }
            ],
        },
    }
    md = render_research_card(payload, evidence_verified=True, visibility="public_safe")
    assert "doc:secret-old-1" not in md
    assert "doc:secret-new-2" not in md
    assert "prior_record_id" not in md
    assert "new_record_id" not in md
    assert "```" not in md
    assert "内部逐字摘录" not in md
    verify_public_safe_card_text(md)


def test_m_p04_4_ten_sources_space_and_hyphen_variants_blocked() -> None:
    for variant in ("10 sources/inbox.md", "10-sources/inbox.md", "10 SOURCES/inbox.md", "10_sources/inbox.md"):
        with pytest.raises(ValueError, match="PUBLIC_SAFE_REDACTION_VIOLATION"):
            verify_public_safe_card_text(_CLEAN_PUBLIC_CARD + f"\n- Path: {variant}\n")

    payload = {
        "metric": "points_per_game",
        "grains": {"STATS": {"posture": "UNKNOWN", "season": "2026-2027"}},
        "evidence_transparency": {
            "posture": "UNKNOWN",
            "unknown_subkind": "NO_SEASON_STATS_RECORDED_YET",
            "scope_meta": {"season": "2026-2027", "locator": "10 sources/inbox/note.md"},
        },
    }
    md = render_research_card(payload, evidence_verified=True, visibility="public_safe")
    assert "10 sources" not in md.lower()
    verify_public_safe_card_text(md)


def test_m_q1_1_archive_compactor_dual_key_conflict_fails_closed() -> None:
    with pytest.raises(ArchiveCompactionError, match="ARCHIVE_COMPACT_INVALID_RELEASE_STATE"):
        _validate_authoritative_release_state(
            release_status={"release_id": "v2.3.1-1", "status": "COMPLETE", "state": "IN_PROGRESS"}
        )
    with pytest.raises(ArchiveCompactionError, match="ARCHIVE_COMPACT_INVALID_RELEASE_STATE"):
        _validate_authoritative_release_state(
            release_status={"release_id": "v2.3.1-1", "status": "FAILED", "state": "COMPLETE"}
        )


def test_m_p02_1_player_coach_treated_as_player() -> None:
    for role in ("PLAYER_COACH", "球员兼教练"):
        horizons = build_person_domain_horizons(
            entity_role=role,
            has_registration_records=True,
            registration_observed_until="2026-11-20T00:00:00Z",
            has_season_stats=False,
            stats_observed_until="2026-11-20T00:00:00Z",
        )
        assert horizons["statistics"]["status"] == DomainHorizonStatus.OBSERVED_EMPTY


def test_m_p01_1_missing_match_type_or_scope_uses_unknown() -> None:
    master_rows = [
        {
            "season": "2026-2027",
            "club_official": "合成修补俱乐部",
            "player": "合成修补甲",
            "player_uid": "player:synth-r1-01",
            "player_id": "SYNTH_R101",
            "contract_category": "C类",
            "contract_term_official": "2028-07-31",
            "registration_status": "已注册",
        }
    ]
    season_stats = [
        {
            "record_id": "stats:CBA_OFFICIAL:2026-2027:UNK:UNK:SYNTH_R101",
            "player_uid": "player:synth-r1-01",
            "provider_player_id": "SYNTH_R101",
            "player_name": "合成修补甲",
            "season": "2026-2027",
            "match_type": None,
            "scope": None,
            "team_name": "合成修补俱乐部",
            "metrics": {"points_per_game": 12.0, "games_played": 10},
            "provenance": {"captured_at": "2026-11-20T00:00:00Z", "locator": "loc-unk-1"},
        }
    ]
    res = build_person_card(
        "合成修补甲",
        season="2026-2027",
        master_rows=master_rows,
        season_stats_records=season_stats,
    )
    table_sec = res["markdown"].split("### Season Averages (Player-Season Grain)", 1)[1].split("\n## ", 1)[0]
    row_line = next(line for line in table_sec.splitlines() if "loc-unk-1" in line)
    assert row_line.startswith("| UNKNOWN | UNKNOWN | 合成修补俱乐部 |")


def test_m_p04_5_historical_archive_banner_allowed_other_blockquotes_rejected() -> None:
    banner = (
        "> [HISTORICAL_SEASON_ARCHIVE] 本文件为 2025-2026 历史赛季定格档案，"
        "非当前活跃赛季（2026-2027）主卡\n\n"
    )
    verify_public_safe_card_text(banner + _CLEAN_PUBLIC_CARD)

    with pytest.raises(ValueError, match="PUBLIC_SAFE_REDACTION_VIOLATION"):
        verify_public_safe_card_text(banner + _CLEAN_PUBLIC_CARD + "\n> 逐字引用内容\n")
