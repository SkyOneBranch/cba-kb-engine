"""Differential falsification & regression tests for REQ-232 Round 2 residual hardening.

100% synthetic offline fixtures (G5 == 0, G6 == 0).
Covers:
- M-R2-1(a): Section-1 transparency fields (SCOPE_OR_CUTOFF_DIFF, REVISION_SUPERSEDED,
             DATA_DISCREPANCY, TRUE_CONFLICT) in public_safe mode
- M-R2-1(b): _sanitize_public_free_text CommonMark blockquote lazy continuation lines
             and inline `>` truncation
- m-R2-1:    Degraded bundle where title == doc_id never leaks doc_id in public_safe
- m-R2-2:    Digitless mixed-case Drive-ID fallback boundary (>= 25 chars, <= 2 `_`/`-`)
"""
from __future__ import annotations

import pytest

from cba_kb.kc_render import _sanitize_public_free_text, render_research_card
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


def test_m_r2_1a_public_safe_section1_transparency_strips_internal_ids_and_locators() -> None:
    # 1. SCOPE_OR_CUTOFF_DIFF
    cutoff_payload = {
        "metric": "points_per_game",
        "grains": {"STATS": {"posture": "SCOPE_OR_CUTOFF_DIFF", "season": "2026-2027"}},
        "evidence_transparency": {
            "posture": "SCOPE_OR_CUTOFF_DIFF",
            "adopted_value": 18.2,
            "scope_meta": {"season": "2026-2027", "player_name": "合成甲", "player_id": "SYNTH_1"},
            "cutoff_records": [
                {
                    "games_played": 20,
                    "captured_at": "2026-11-01T00:00:00Z",
                    "value": 17.5,
                    "locator": "internal-loc-42",
                },
                {
                    "games_played": 25,
                    "captured_at": "2026-11-15T00:00:00Z",
                    "value": 18.2,
                    "locator": "https://example.invalid/official/cutoff-25",
                },
            ],
        },
    }
    md_cutoff = render_research_card(cutoff_payload, evidence_verified=True, visibility="public_safe")
    assert "internal-loc-42" not in md_cutoff
    assert "https://example.invalid/official/cutoff-25" in md_cutoff
    verify_public_safe_card_text(md_cutoff)

    # 2. REVISION_SUPERSEDED
    rev_payload = {
        "metric": "points_per_game",
        "grains": {"STATS": {"posture": "REVISION_SUPERSEDED", "season": "2026-2027"}},
        "evidence_transparency": {
            "posture": "REVISION_SUPERSEDED",
            "adopted_value": 19.0,
            "scope_meta": {"season": "2026-2027", "player_name": "合成甲", "player_id": "SYNTH_1"},
            "revision_history": [
                {
                    "prior_value": 18.5,
                    "new_value": 19.0,
                    "supersedes_record_id": "stats-internal-old-9",
                    "reason": "官方口径更正\n> 内部逐字摘录\n续行泄露内容",
                }
            ],
        },
    }
    md_rev = render_research_card(rev_payload, evidence_verified=True, visibility="public_safe")
    assert "stats-internal-old-9" not in md_rev
    assert "内部逐字摘录" not in md_rev
    assert "续行泄露内容" not in md_rev
    assert "官方口径更正" in md_rev
    verify_public_safe_card_text(md_rev)

    # 3. DATA_DISCREPANCY & TRUE_CONFLICT
    for posture in ("DATA_DISCREPANCY", "TRUE_CONFLICT"):
        disc_payload = {
            "metric": "points_per_game",
            "grains": {"STATS": {"posture": posture, "season": "2026-2027"}},
            "evidence_transparency": {
                "posture": posture,
                "delta": 0.4,
                "scope_meta": {"season": "2026-2027", "player_name": "合成甲", "player_id": "SYNTH_1"},
                "discrepancies": [
                    {
                        "record_id": "rec-internal-77",
                        "value": 18.0,
                        "record_kind": "OFFICIAL",
                        "games_played": 20,
                        "locator": "internal-loc-42",
                    }
                ],
            },
        }
        md_disc = render_research_card(disc_payload, evidence_verified=True, visibility="public_safe")
        assert "rec-internal-77" not in md_disc
        assert "internal-loc-42" not in md_disc
        verify_public_safe_card_text(md_disc)


def test_m_r2_1b_sanitize_public_free_text_lazy_continuation_and_inline_gt() -> None:
    lazy_text = "合法摘要第一段\n> 引用原文第一行\n延迟续行逐字内容\n第二行延迟续行\n\n恢复公开正文"
    cleaned_lazy = _sanitize_public_free_text(lazy_text)
    assert "合法摘要第一段" in cleaned_lazy
    assert "恢复公开正文" in cleaned_lazy
    assert "引用原文第一行" not in cleaned_lazy
    assert "延迟续行逐字内容" not in cleaned_lazy
    assert "第二行延迟续行" not in cleaned_lazy

    inline_text = "公开摘要内容 > 行内逐字引用不应出现"
    cleaned_inline = _sanitize_public_free_text(inline_text)
    assert cleaned_inline == "公开摘要内容"
    assert ">" not in cleaned_inline
    assert "行内逐字引用不应出现" not in cleaned_inline


def test_m_r2_1_title_equals_doc_id_redacted_in_public_safe() -> None:
    # 1. render_research_card public citation branch
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
                "doc_id": "internal_bundle_secret_777",
                "source_tier": "T1",
                "completeness": "full",
                "published_at": "2026-11-20",
                "title": "internal_bundle_secret_777",
                "canonical_url": "https://example.invalid/official/eq-1",
            }
        ],
    }
    md = render_research_card(payload, evidence_verified=True, visibility="public_safe")
    assert "internal_bundle_secret_777" not in md
    assert "https://example.invalid/official/eq-1" in md
    verify_public_safe_card_text(md)

    # 2. build_person_card public citation builder
    master_rows = [
        {
            "season": "2026-2027",
            "club_official": "合成修补俱乐部",
            "player": "合成修补甲",
            "player_uid": "player:synth-r2-01",
            "player_id": "SYNTH_R201",
            "contract_category": "C类",
            "contract_term_official": "2028-07-31",
            "registration_status": "已注册",
        }
    ]
    doc_bundles = [
        {
            "doc_id": "internal_bundle_secret_888",
            "source_tier": "T1",
            "completeness": "full",
            "published_at": "2026-11-20",
            "title": "internal_bundle_secret_888",
            "canonical_url": "https://example.invalid/official/eq-2",
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
    assert "internal_bundle_secret_888" not in res["markdown"]
    assert "https://example.invalid/official/eq-2" in res["markdown"]
    verify_public_safe_card_text(res["markdown"])


def test_m_r2_2_drive_id_fallback_25_chars_and_two_separators() -> None:
    for token in (
        "AbCdEfGhIjKlMnOpQrStUvWxY",          # 25 chars, digitless mixed-case
        "AbCdEfGhIjKlMnOpQrStUvWxYz",         # 26 chars, digitless mixed-case
        "AbCdEfGh_IjKlMnOp-QrStUvWxYz",       # 28 chars, digitless mixed-case with 2 seps
    ):
        with pytest.raises(ValueError, match="PUBLIC_SAFE_REDACTION_VIOLATION"):
            verify_public_safe_card_text(_CLEAN_PUBLIC_CARD + f"\n- Ref: {token}\n")

    # Clean card + public URL still pass
    verify_public_safe_card_text(
        _CLEAN_PUBLIC_CARD
        + "\n- Public: https://example.invalid/official/AbCdEfGh_IjKlMnOp-QrStUvWxYz\n"
    )
