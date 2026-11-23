"""Frozen red acceptance & wiring tests for REQ-222 (v2.2.2):
Collector v0.4.1 Curated <25KB Surface, Dual CSV Split, INDEX Stamp &
Proxy-Aware Concurrent Drive Cutover.

Written BEFORE Commit 2 implementation for differential falsification on origin/main:
- P0-1: v2.2.2-1 release spec, 2 new UTF-8-SIG structured CSVs (foreign snapshot &
        domestic reg diff) with evidence/candidate_observation markers & redaction,
        <25KB fail-closed curated digest gate, guide/dictionary updates, ops_sync_v041
        with ops_sync_v040_1 fallback, and 100% v2.1.5/v2.2.0/v2.2.1 backward compat.
- P0-2: Deterministic operational announcement denoising filter + 100% document index
        preservation (0 loss).
- P0-3: derived/INDEX.md (and entry control docs) top-10-line version + exact master
        row-count stamp with 100% validate_current_state() compatibility.
- P0-4: _unauth_client.trust_env strictly False + _resolve_sanitized_env_proxies()
        honoring NO_PROXY and fail-closed rejecting embedded credentials / unsafe schemes.
- P0-5: _inventory() level-by-level BFS 8-thread concurrency, read-only phase reuse,
        strict post-mutation invalidation, and prepare()/verify() Δ-entry concurrent
        read-only prefetch with 100% main-thread serial writes.

All fixtures are 100% synthetic. Zero network calls, zero real Drive API calls,
zero local home-directory paths, zero raw Drive IDs.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import threading
import time
import tomllib
from typing import Any, Dict, List

from openpyxl import Workbook
import pytest
import requests
import yaml
from google.oauth2.credentials import Credentials

from cba_kb import consumer_surface, current_state, drive_http
from cba_kb import release as release_module
from cba_kb.common import digest
from cba_kb.instance import Instance
from cba_kb.native import wrap
from cba_kb.orchestration.release import prepare_v215 as orch
from tests.test_release_orchestration import projection_inputs

ROOT = Path(__file__).resolve().parents[1]
V222_1_BASELINE_SHA = "29589e7d4d90a81a6507ecaa1ec73945a5eecfc1"
FROZEN_PREPARE_PRODUCTION_SHA256 = (
    "0ccb79429fb4ec5e270a3154af987475ae75bd4ef81de48c1e665a3b93cf4573"
)


def _make_synthetic_instance(tmp_path: Path) -> Instance:
    inst_root = tmp_path / "synth_instance"
    config_root = inst_root / "config"
    data_root = inst_root / "data"
    config_root.mkdir(parents=True, exist_ok=True)
    data_root.mkdir(parents=True, exist_ok=True)
    return Instance(
        root=inst_root.resolve(),
        config_root=config_root.resolve(),
        credentials_store=(inst_root / ".credentials").resolve(),
        real_fixture_root=(inst_root / "fixtures" / "real").resolve(),
        data_root=data_root.resolve(),
    )


def _synthetic_v222_bundles(*, huge_domestic: bool = False) -> list[Dict[str, Any]]:
    fake_drive_id = "1" + "SynthV222RawDriveToken9876543210"
    fake_user_path = "/Users/" + "synth_operator/private/v222_raw.md"
    fake_home_path = "/home/" + "synth_runner/private/v222_secret.json"

    foreign_table = "\n".join([
        "2026-2027赛季CBA联赛外籍球员注册信息（截至11月21日17:00）",
        "| 俱乐部 | 外籍球员 |  | 国籍（以注册时提供护照为准） | 场上位置 | 球衣号码 |",
        "| --- | --- | --- | --- | --- | --- |",
        f"| 深圳新世纪 | SYNTH POWELL | 合成鲍威尔 | 美国 | PG | 11 |",
        f"| 广东宏远 | SYNTH MACON | 合成梅肯 | 美国 | SG | 23 |",
        f"| 辽宁沈阳三生 | SYNTH WHARTON | 合成沃顿 | 美国 | C | 55 |",
    ])
    big_filler = ("\n深圳新世纪 | 合成国内球员 | 延续 | B类 | 2028-07-31" * 600) if huge_domestic else ""

    return [
        # 3-season domestic registration masters (exempt from denoising; large body omitted from compact .md)
        {
            "doc_id": "doc_dom_2425_v222_001",
            "title": "2024-2025赛季CBA联赛国内球员注册信息",
            "source_tier": "T1",
            "event_class": "注册与转会",
            "published_at": "2024-08-06",
            "acquired_at": "2026-11-22T02:00:00Z",
            "canonical_url": "https://example.org/register/2024-2025",
            "upstream_origin": "CBA官方注册公示",
            "raw_sha256": "1" * 64,
            "matched_persons": [],
            "body": "2024-2025赛季CBA联赛国内球员注册信息\n深圳新世纪 | 合成甲 | B类" + big_filler,
        },
        {
            "doc_id": "doc_dom_2526_v222_002",
            "title": "2025-2026赛季CBA联赛国内球员注册信息",
            "source_tier": "T1",
            "event_class": "注册与转会",
            "published_at": "2025-08-06",
            "acquired_at": "2026-11-22T02:00:00Z",
            "canonical_url": "https://example.org/register/2025-2026",
            "upstream_origin": "CBA官方注册公示",
            "raw_sha256": "2" * 64,
            "matched_persons": [],
            "body": "2025-2026赛季CBA联赛国内球员注册信息\n深圳新世纪 | 合成乙 | A1类" + big_filler,
        },
        {
            "doc_id": "doc_dom_2627_v222_003",
            "title": "2026-2027赛季CBA联赛国内球员注册信息",
            "source_tier": "T1",
            "event_class": "注册与转会",
            "published_at": "2026-08-05",
            "acquired_at": "2026-11-22T02:00:00Z",
            "canonical_url": "https://example.org/register/2026-2027",
            "upstream_origin": "CBA官方注册公示",
            "raw_sha256": "3" * 64,
            "matched_persons": [],
            "body": "2026-2027赛季CBA联赛国内球员注册信息\n广州龙狮 | 合成乙 | B类" + big_filler,
        },
        # Foreign registration official snapshot (exempt from denoising; routed to CSV)
        {
            "doc_id": "doc_for_2627_v222_004",
            "title": "2026-2027赛季CBA联赛外籍球员注册信息（截至11月21日17点）",
            "source_tier": "T1",
            "event_class": "注册与转会",
            "published_at": "2026-11-21",
            "acquired_at": "2026-11-22T02:00:00Z",
            "canonical_url": "https://example.org/register/foreign-2026-2027",
            "upstream_origin": "CBA官方外籍球员注册公示",
            "raw_sha256": "4" * 64,
            "matched_persons": [],
            "body": foreign_table + f"\n泄露标记: {fake_drive_id} {fake_user_path}",
        },
        # Admitted curated operational announcements
        {
            "doc_id": "doc_admit_signing_005",
            "title": "俱乐部正式签约外援鲍威尔、梅肯与沃顿试训公告",
            "source_tier": "T1.5",
            "event_class": "注册与转会",
            "published_at": "2026-11-19",
            "acquired_at": "2026-11-22T02:00:00Z",
            "canonical_url": "https://example.org/news/signing-005",
            "upstream_origin": "俱乐部官微",
            "raw_sha256": "5" * 64,
            "matched_persons": ["鲍威尔", "梅肯", "沃顿"],
            "body": f"俱乐部官宣完成外援鲍威尔签约并安排梅肯、沃顿试训（待CBA审核）。{fake_home_path}",
        },
        {
            "doc_id": "doc_admit_roster_006",
            "title": "国家队与辽篮出征名单及贺希宁、赵睿伤情更新",
            "source_tier": "T1",
            "event_class": "伤病与名单",
            "published_at": "2026-11-20",
            "acquired_at": "2026-11-22T02:00:00Z",
            "canonical_url": "https://example.org/news/roster-006",
            "upstream_origin": "中国篮协官微",
            "raw_sha256": "6" * 64,
            "matched_persons": ["贺希宁", "赵睿"],
            "body": "贺希宁与赵睿入选集训观察名单，辽篮出征客场轮换名单同步公布。",
        },
        {
            "doc_id": "doc_admit_officiating_007",
            "title": "#判罚解析# 2026-2027赛季常规赛第5轮关键回合说明",
            "source_tier": "T1",
            "event_class": "判罚与规则",
            "published_at": "2026-11-18",
            "acquired_at": "2026-11-22T02:00:00Z",
            "canonical_url": "https://example.org/news/ref-007",
            "upstream_origin": "CBA联赛官微",
            "raw_sha256": "7" * 64,
            "matched_persons": [],
            "body": "CBA裁判办公室发布#判罚解析#，明确末节最后两分钟掩护犯规判罚尺度。",
        },
        {
            "doc_id": "doc_admit_nbl_meeting_008",
            "title": "2026-2027赛季NBL-CBA规则对接与联赛官方工作会议",
            "source_tier": "T1",
            "event_class": "联赛政策",
            "published_at": "2026-11-15",
            "acquired_at": "2026-11-22T02:00:00Z",
            "canonical_url": "https://example.org/news/nbl-008",
            "upstream_origin": "CBA官方公告",
            "raw_sha256": "8" * 64,
            "matched_persons": [],
            "body": "联赛工作会议明确NBL-CBA规则对接与准入审核流程。",
        },
        # Noise announcements (MUST be filtered out of .md body, retained in JSON index)
        {
            "doc_id": "doc_noise_prophet_009",
            "title": "【每日预言家】今晚焦点战谁能取胜？有奖互动抽奖来袭",
            "source_tier": "T1",
            "event_class": "社媒互动",
            "published_at": "2026-11-20",
            "acquired_at": "2026-11-22T02:00:00Z",
            "canonical_url": "https://example.org/weibo/noise-009",
            "upstream_origin": "CBA联赛官微",
            "raw_sha256": "9" * 64,
            "matched_persons": [],
            "body": "参与每日预言家有奖互动并转发抽奖赢取周边礼品。",
        },
        {
            "doc_id": "doc_noise_moment_010",
            "title": "【每日名场面】图文征集：我和我的篮球故事投票评选最佳高光",
            "source_tier": "T1",
            "event_class": "社媒互动",
            "published_at": "2026-11-19",
            "acquired_at": "2026-11-22T02:00:00Z",
            "canonical_url": "https://example.org/weibo/noise-010",
            "upstream_origin": "CBA联赛官微",
            "raw_sha256": "a" * 64,
            "matched_persons": [],
            "body": "欢迎投稿我和我的篮球图文征集，投票选出每日名场面最佳瞬间。",
        },
        {
            "doc_id": "doc_noise_condolence_011",
            "title": "关于沉痛悼念老前辈的唁电",
            "source_tier": "T1",
            "event_class": "官方公告",
            "published_at": "2026-11-10",
            "acquired_at": "2026-11-22T02:00:00Z",
            "canonical_url": "https://example.org/news/noise-011",
            "upstream_origin": "中国篮协官微",
            "raw_sha256": "b" * 64,
            "matched_persons": [],
            "body": "谨发唁电深切缅怀篮球界老前辈。",
        },
        {
            "doc_id": "doc_noise_old_date_012",
            "title": "2023-2024赛季旧置顶公告",
            "source_tier": "T1",
            "event_class": "官方公告",
            "published_at": "2024-03-15",
            "acquired_at": "2026-11-22T02:00:00Z",
            "canonical_url": "https://example.org/news/noise-012",
            "upstream_origin": "CBA官方公告",
            "raw_sha256": "c" * 64,
            "matched_persons": [],
            "body": "2023-2024赛季历史置顶公告内容。",
        },
        {
            "doc_id": "doc_noise_asiad_013",
            "title": "杭州亚运会中国男篮参赛情况与亚运会总结",
            "source_tier": "T1",
            "event_class": "历史总结",
            "published_at": None,
            "acquired_at": "2026-11-22T02:00:00Z",
            "canonical_url": "https://example.org/news/noise-013",
            "upstream_origin": "中国篮协官微",
            "raw_sha256": "d" * 64,
            "matched_persons": [],
            "body": "关于杭州亚运会备战与亚运会总结的置顶回顾文章。",
        },
    ]


def _synthetic_v222_reg_diff() -> Dict[str, Any]:
    fake_drive_id = "1" + "SynthDiffDriveToken123456789012"
    return {
        "statement_notice": (
            "Candidate diff derived from CBA official public registration pages. "
            "Statement != Fact. Evidence != Fact. Unknown != 0."
        ),
        "old_bundle": {
            "bundle_id": "old_2526",
            "title": "2025-2026赛季CBA联赛国内球员注册信息",
            "clubs_count": 20,
            "players_count": 335,
            "raw_sha256": "2" * 64,
        },
        "new_bundle": {
            "bundle_id": "new_2627",
            "title": "2026-2027赛季CBA联赛国内球员注册信息",
            "clubs_count": 20,
            "players_count": 330,
            "raw_sha256": "3" * 64,
        },
        "summary": {
            "added_count": 1,
            "removed_count": 1,
            "transferred_count": 1,
            "contract_changed_count": 1,
        },
        "transferred_players": [
            {
                "player": "合成球员乙",
                "from_club": "深圳新世纪",
                "to_club": "广州龙狮",
                "old_contract_type": "A1类",
                "new_contract_type": "B类",
            }
        ],
        "contract_changed_players": [
            {
                "player": "合成球员甲",
                "club": "深圳新世纪",
                "changed_fields": {
                    "合同类别": {"old": "A1类", "new": "B类"},
                    "备注": {"old": "原注", "new": f"更新 {fake_drive_id}"},
                },
            }
        ],
        "added_players": [
            {
                "player": "合成新秀丁",
                "club": "上海久事",
                "fields": {"合同类别": "A1类", "注册类型": "首次"},
            }
        ],
        "removed_players": [
            {
                "player": "合成老将戊",
                "club": "广东宏远",
                "fields": {"合同类别": "C类"},
            }
        ],
    }


def _make_master_xlsx(row_count: int = 3) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "MASTER"
    ws.append(["record_key", "season", "club_id", "player"])
    for idx in range(row_count):
        ws.append([f"2025-2026|sz|{idx}", "2025-2026", "sz", f"synth_{idx}"])
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# P0-1: v2.2.2-1 spec, dual CSV exporters, <25KB compact digest, wiring
# ---------------------------------------------------------------------------


def test_p0_1_v222_release_spec_registration_and_backward_compat():
    expected = {
        "product_baseline_sha": V222_1_BASELINE_SHA,
        "enable_consumer_surface_v215": True,
        "enable_operational_kb_v220": True,
        "enable_curated_compact_surface_v222": True,
        "incremental_delta_only": True,
        "archive_format": "bundle_v1",
    }
    assert orch.V215_RELEASE_SPECS.get("v2.2.2-1") == expected
    assert orch._require_release("v2.2.2-1") == expected
    assert orch._release_spec("v2.2.2-1") == expected
    assert orch._uses_closure("v2.2.2-1") is True
    assert orch._uses_technical_manual("v2.2.2-1") is True

    # Existing release specs remain 100% unchanged
    assert "enable_curated_compact_surface_v222" not in orch.V215_RELEASE_SPECS["v2.1.5-1"]
    assert "enable_curated_compact_surface_v222" not in orch.V215_RELEASE_SPECS["v2.2.0-1"]
    assert "enable_curated_compact_surface_v222" not in orch.V215_RELEASE_SPECS["v2.2.1-1"]

    # Hard guardrails
    actual_sha = hashlib.sha256(
        (ROOT / "scripts" / "prepare_production.py").read_bytes()
    ).hexdigest()
    assert actual_sha == FROZEN_PREPARE_PRODUCTION_SHA256
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == "2.0.2"


def test_p0_1_dual_csv_exporters_utf8_sig_evidence_markers_and_redaction():
    assert getattr(consumer_surface, "FOREIGN_REGISTRATION_CSV_FILENAME", None) == (
        "CBA_外籍球员注册官方快照_CURRENT.csv"
    )
    assert getattr(consumer_surface, "DOMESTIC_REG_DIFF_CSV_FILENAME", None) == (
        "CBA_跨季国内球员注册增量观测_2025-2027_CURRENT.csv"
    )
    assert getattr(consumer_surface, "COLLECTOR_DIGEST_MAX_BYTES", None) == 25 * 1024

    export_foreign = getattr(consumer_surface, "export_foreign_players_registration_csv", None)
    export_diff = getattr(consumer_surface, "export_domestic_registration_diff_csv", None)
    assert callable(export_foreign), "Missing consumer_surface.export_foreign_players_registration_csv"
    assert callable(export_diff), "Missing consumer_surface.export_domestic_registration_diff_csv"

    bundles = _synthetic_v222_bundles()
    reg_diff = _synthetic_v222_reg_diff()
    fake_drive_id = "1" + "SynthV222RawDriveToken9876543210"
    fake_diff_drive_id = "1" + "SynthDiffDriveToken123456789012"

    foreign_bytes_1 = export_foreign(bundles=bundles, known_drive_ids=(fake_drive_id,))
    foreign_bytes_2 = export_foreign(bundles=list(reversed(bundles)), known_drive_ids=(fake_drive_id,))
    assert isinstance(foreign_bytes_1, bytes)
    assert foreign_bytes_1.startswith(b"\xef\xbb\xbf"), "Foreign CSV must be UTF-8-SIG encoded"
    assert foreign_bytes_1 == foreign_bytes_2, "Foreign CSV must be deterministic"

    foreign_text = foreign_bytes_1.decode("utf-8-sig")
    assert fake_drive_id not in foreign_text
    assert "/Users/" + "synth_operator" not in foreign_text
    foreign_rows = list(csv.DictReader(io.StringIO(foreign_text)))
    assert len(foreign_rows) == 3
    required_foreign_cols = {
        "layer",
        "statement_notice",
        "snapshot_title",
        "published_at",
        "club",
        "player_en",
        "player_zh",
        "nationality",
        "position",
        "jersey_number",
        "doc_id",
        "source_tier",
        "raw_sha256",
    }
    assert required_foreign_cols <= set(foreign_rows[0].keys())
    for row in foreign_rows:
        assert "evidence" in row["layer"] and "candidate_observation" in row["layer"]
        assert "Statement != Fact" in row["statement_notice"]
        assert "Evidence != Fact" in row["statement_notice"]
        assert "Unknown != 0" in row["statement_notice"]
    assert {r["player_en"] for r in foreign_rows} == {"SYNTH POWELL", "SYNTH MACON", "SYNTH WHARTON"}

    diff_bytes = export_diff(reg_diff=reg_diff, known_drive_ids=(fake_diff_drive_id,))
    assert isinstance(diff_bytes, bytes)
    assert diff_bytes.startswith(b"\xef\xbb\xbf"), "Domestic diff CSV must be UTF-8-SIG encoded"
    diff_text = diff_bytes.decode("utf-8-sig")
    assert fake_diff_drive_id not in diff_text
    diff_rows = list(csv.DictReader(io.StringIO(diff_text)))
    assert len(diff_rows) == 4
    required_diff_cols = {
        "layer",
        "statement_notice",
        "change_type",
        "player",
        "old_club",
        "new_club",
        "old_contract_type",
        "new_contract_type",
        "change_details",
        "old_snapshot_sha256",
        "new_snapshot_sha256",
    }
    assert required_diff_cols <= set(diff_rows[0].keys())
    assert {r["change_type"] for r in diff_rows} == {
        "TRANSFERRED",
        "CONTRACT_CHANGED",
        "ADDED",
        "REMOVED",
    }
    for row in diff_rows:
        assert "evidence" in row["layer"] and "candidate_observation" in row["layer"]
        assert "Statement != Fact" in row["statement_notice"]


def test_p0_1_curated_compact_digest_under_25kb_fail_closed_gate_and_v220_compat():
    bundles = _synthetic_v222_bundles(huge_domestic=True)
    reg_diff = _synthetic_v222_reg_diff()
    fake_drive_id = "1" + "SynthV222RawDriveToken9876543210"

    compact_1 = consumer_surface.render_collector_official_digest_md(
        bundles=bundles,
        reg_diff=reg_diff,
        release_id="v2.2.2-1",
        engine_sha=V222_1_BASELINE_SHA,
        known_drive_ids=(fake_drive_id,),
        compact_curated=True,
    )
    compact_2 = consumer_surface.render_collector_official_digest_md(
        bundles=list(reversed(bundles)),
        reg_diff=reg_diff,
        release_id="v2.2.2-1",
        engine_sha=V222_1_BASELINE_SHA,
        known_drive_ids=(fake_drive_id,),
        compact_curated=True,
    )
    assert compact_1 == compact_2
    assert len(compact_1) < 25 * 1024, f"Compact curated digest must be <25KB, got {len(compact_1)}"

    text = compact_1.decode("utf-8")
    assert "CBA_外籍球员注册官方快照_CURRENT.csv" in text
    assert "CBA_跨季国内球员注册增量观测_2025-2027_CURRENT.csv" in text
    assert "CBA_文档索引_脱敏视图_CURRENT.json" in text
    assert "evidence / candidate_observation" in text
    assert "Statement != Fact" in text
    assert "Evidence != Fact" in text
    assert "Unknown != 0" in text
    assert text.endswith("end_marker: true\n")
    # Noise items excluded from curated .md body
    assert "每日预言家" not in text
    assert "每日名场面" not in text
    assert "杭州亚运会" not in text
    assert "沉痛悼念老前辈的唁电" not in text
    # Curated items admitted
    assert "鲍威尔" in text
    assert "贺希宁" in text
    assert "#判罚解析#" in text
    assert "NBL-CBA" in text
    # Header discloses total/admitted/filtered counts (13 total, 8 admitted, 5 filtered)
    assert "13" in text and "8" in text and "5" in text
    # Redaction
    assert fake_drive_id not in text
    assert "/Users/" + "synth_operator" not in text
    assert "/home/" + "synth_runner" not in text

    # Fail-closed size gate raises ValueError("COLLECTOR_DIGEST_SIZE_LIMIT_EXCEEDED")
    with pytest.raises(ValueError, match="COLLECTOR_DIGEST_SIZE_LIMIT_EXCEEDED"):
        consumer_surface.render_collector_official_digest_md(
            bundles=bundles,
            reg_diff=reg_diff,
            release_id="v2.2.2-1",
            engine_sha=V222_1_BASELINE_SHA,
            compact_curated=True,
            max_bytes=256,
        )

    # Default (compact_curated=False) preserves v2.2.0-1 full rendering compatibility
    legacy_md = consumer_surface.render_collector_official_digest_md(
        bundles=_synthetic_v222_bundles(huge_domestic=False),
        reg_diff=reg_diff,
        release_id="v2.2.0-1",
        engine_sha=orch.V220_1_BASELINE_SHA,
    ).decode("utf-8")
    assert "SYNTH POWELL" in legacy_md


def test_p0_1_guide_dictionary_and_stage4_wiring_with_ops_sync_v041_fallback(monkeypatch, tmp_path):
    guide_v222 = consumer_surface.render_consumer_surface_guide_section(
        release_id="v2.2.2-1", master_row_count=3465
    )
    dict_v222 = consumer_surface.render_consumer_field_dictionary_md(
        release_id="v2.2.2-1", engine_sha=V222_1_BASELINE_SHA, master_row_count=3465
    ).decode("utf-8")
    for doc_text in (guide_v222, dict_v222):
        assert "CBA_外籍球员注册官方快照_CURRENT.csv" in doc_text
        assert "CBA_跨季国内球员注册增量观测_2025-2027_CURRENT.csv" in doc_text
        assert "Python" in doc_text or "CSV" in doc_text

    # v2.2.0-1 and v2.2.1-1 backward compatibility
    for old_rel in ("v2.1.5-1", "v2.2.0-1", "v2.2.1-1"):
        old_guide = consumer_surface.render_consumer_surface_guide_section(
            release_id=old_rel, master_row_count=3465
        )
        assert "CBA_外籍球员注册官方快照_CURRENT.csv" not in old_guide
        assert "CBA_跨季国内球员注册增量观测_2025-2027_CURRENT.csv" not in old_guide

    # Stage 4 wiring: ops_sync_v041 preferred over ops_sync_v040_1, with fallback
    bundles = _synthetic_v222_bundles(huge_domestic=True)
    reg_diff = _synthetic_v222_reg_diff()
    master_csv = (
        "record_key,season,club_id,player,source_file_id\n"
        "2025-2026|sz|p1,2025-2026,sz,合成球员,src_01\n"
    ).encode("utf-8-sig")
    sr_csv = b"source_id,source_title,season\nsrc_01,synth.pdf,2025-2026\n"
    existing_doc_idx = json.dumps({"schema_version": 1, "documents": []}).encode("utf-8")
    bytes_by_id = {"m-id": master_csv, "sr-id": sr_csv, "idx-id": existing_doc_idx}
    monkeypatch.setattr(orch, "snapshot", lambda _d, fid, _m="binary": (bytes_by_id[fid], {"id": fid}))
    manifest_rows = [
        {"drive_file_id": "m-id", "uid": "derived/MASTER.csv"},
        {"drive_file_id": "sr-id", "uid": "input/source_registry.csv"},
        {"drive_file_id": "idx-id", "uid": "data/normalized_document_index.json"},
    ]

    def _populate_ops_dir(inst: Instance, dirname: str, bundle_list: list[Dict[str, Any]]) -> None:
        ops_dir = inst.data_root / dirname
        ops_dir.mkdir(parents=True, exist_ok=True)
        consumed: Dict[str, Any] = {}
        for idx, b in enumerate(bundle_list):
            doc_dir = inst.document_archive_root / b["doc_id"] / "raw"
            doc_dir.mkdir(parents=True, exist_ok=True)
            fm = {k: b[k] for k in ("title", "source_tier", "event_class", "published_at", "acquired_at", "canonical_url", "upstream_origin", "raw_sha256", "matched_persons")}
            md_text = "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n" + b["body"]
            (doc_dir / f"cap_{idx:03d}.md").write_text(md_text, encoding="utf-8")
            consumed[f"2026-11/b_{idx:02d}.md"] = {
                "doc_id": b["doc_id"],
                "title": b["title"],
                "canonical_url": b["canonical_url"],
                "source_tier": b["source_tier"],
                "completeness": "full",
                "published_at": b["published_at"],
                "content_hash": b["raw_sha256"],
                "file_sha256": b["raw_sha256"],
                "matched_persons": b["matched_persons"],
                "disclosure_posture": None,
            }
        (ops_dir / "operational_checkpoint.json").write_text(
            json.dumps({"schema_version": 1, "consumed_bundles": consumed}, ensure_ascii=False),
            encoding="utf-8",
        )
        (ops_dir / "reg_diff_2526_2627.json").write_text(
            json.dumps(reg_diff, ensure_ascii=False),
            encoding="utf-8",
        )

    # Case A: Both ops_sync_v041 and ops_sync_v040_1 exist -> ops_sync_v041 wins
    inst_pref = _make_synthetic_instance(tmp_path / "pref")
    _populate_ops_dir(inst_pref, "ops_sync_v040_1", bundles[:4])
    _populate_ops_dir(inst_pref, "ops_sync_v041", bundles)
    targets_pref = orch._build_instance_consumer_targets(
        object(),
        inst_pref,
        release_id="v2.2.2-1",
        engine_sha=V222_1_BASELINE_SHA,
        manifest_rows=manifest_rows,
        data_parent="data-folder",
    )
    assert "consumer/foreign_players_registration_current.csv" in targets_pref
    assert "consumer/domestic_registration_diff_current.csv" in targets_pref
    assert "consumer/collector_official_digest_current.md" in targets_pref
    assert targets_pref["consumer/foreign_players_registration_current.csv"]["name"] == (
        "CBA_外籍球员注册官方快照_CURRENT.csv"
    )
    assert targets_pref["consumer/domestic_registration_diff_current.csv"]["name"] == (
        "CBA_跨季国内球员注册增量观测_2025-2027_CURRENT.csv"
    )
    assert targets_pref["consumer/foreign_players_registration_current.csv"]["publish_parent"] == "data-folder"
    assert targets_pref["consumer/domestic_registration_diff_current.csv"]["publish_parent"] == "data-folder"
    assert len(targets_pref["consumer/collector_official_digest_current.md"]["content"]) < 25 * 1024
    merged_idx = json.loads(targets_pref["consumer/document_index_current.json"]["content"].decode("utf-8"))
    assert merged_idx["unique_document_count"] == len(bundles)  # used v041 (13), not v040_1 (4)

    # Case B: Only ops_sync_v040_1 exists -> fallback works
    inst_fb = _make_synthetic_instance(tmp_path / "fallback")
    _populate_ops_dir(inst_fb, "ops_sync_v040_1", bundles)
    targets_fb = orch._build_instance_consumer_targets(
        object(),
        inst_fb,
        release_id="v2.2.2-1",
        engine_sha=V222_1_BASELINE_SHA,
        manifest_rows=manifest_rows,
        data_parent="data-folder",
    )
    assert "consumer/foreign_players_registration_current.csv" in targets_fb
    assert "consumer/domestic_registration_diff_current.csv" in targets_fb
    assert len(targets_fb["consumer/collector_official_digest_current.md"]["content"]) < 25 * 1024

    # Full project_targets integration with v2.2.2-1
    inputs = projection_inputs()
    projected = orch.project_targets(
        release_id="v2.2.2-1",
        engine_sha=V222_1_BASELINE_SHA,
        status_id=inputs["status_id"],
        archive_id=inputs["archive_id"],
        previous_status=inputs["previous_status"],
        previous_targets=inputs["previous_targets"],
        manifest_rows=inputs["manifest_rows"],
        tracked=inputs["tracked"],
        tracked_hashes=inputs["tracked_hashes"],
        code_parent="scripts",
        declared_consumer_targets=targets_pref,
    )
    assert projected["state"] == "PROJECTED"
    assert "consumer/foreign_players_registration_current.csv" in projected["final_current_logical_keys"]
    assert "consumer/domestic_registration_diff_current.csv" in projected["final_current_logical_keys"]


# ---------------------------------------------------------------------------
# P0-2: Deterministic operational announcement denoising & 100% index retention
# ---------------------------------------------------------------------------


def test_p0_2_deterministic_denoising_filter_and_100pct_index_preservation():
    filter_fn = getattr(consumer_surface, "filter_curated_operational_announcements", None)
    assert callable(filter_fn), "Missing consumer_surface.filter_curated_operational_announcements"

    bundles = _synthetic_v222_bundles()
    res_1 = filter_fn(bundles, active_window_start="2025-07-01")
    res_2 = filter_fn(list(reversed(bundles)), active_window_start="2025-07-01")
    assert res_1 == res_2, "Denoising filter must be deterministic regardless of input order"

    assert res_1["total_count"] == len(bundles)
    assert res_1["admitted_count"] + res_1["filtered_count"] == res_1["total_count"]

    admitted_ids = {b["doc_id"] for b in res_1["admitted"]}
    filtered_ids = {b["doc_id"] for b in res_1["filtered"]}
    assert admitted_ids.isdisjoint(filtered_ids)

    # Registration masters (including 2024-2025 domestic) + curated operational announcements MUST be admitted
    expected_admitted = {
        "doc_dom_2425_v222_001",
        "doc_dom_2526_v222_002",
        "doc_dom_2627_v222_003",
        "doc_for_2627_v222_004",
        "doc_admit_signing_005",
        "doc_admit_roster_006",
        "doc_admit_officiating_007",
        "doc_admit_nbl_meeting_008",
    }
    expected_filtered = {
        "doc_noise_prophet_009",
        "doc_noise_moment_010",
        "doc_noise_condolence_011",
        "doc_noise_old_date_012",
        "doc_noise_asiad_013",
    }
    assert admitted_ids == expected_admitted
    assert filtered_ids == expected_filtered
    reasons = res_1["reasons_by_doc_id"]
    for doc_id in expected_filtered:
        assert isinstance(reasons.get(doc_id), str) and reasons[doc_id].strip()

    # 100% of all N bundles (admitted + filtered) MUST remain in consumer document index
    checkpoint = {
        "schema_version": 1,
        "consumed_bundles": {
            f"2026-11/item_{i:02d}.md": {
                "doc_id": b["doc_id"],
                "title": b["title"],
                "canonical_url": b["canonical_url"],
                "source_tier": b["source_tier"],
                "completeness": "full",
                "published_at": b["published_at"],
                "content_hash": b["raw_sha256"],
                "file_sha256": b["raw_sha256"],
                "matched_persons": b["matched_persons"],
                "disclosure_posture": None,
            }
            for i, b in enumerate(bundles)
        },
    }
    merged = json.loads(
        consumer_surface.merge_operational_docs_into_consumer_index(None, checkpoint).decode("utf-8")
    )
    assert merged["unique_document_count"] == len(bundles)
    assert {d["doc_id"] for d in merged["documents"]} == (expected_admitted | expected_filtered)


# ---------------------------------------------------------------------------
# P0-3: INDEX top-10-line stamp & validate_current_state() compatibility
# ---------------------------------------------------------------------------


def test_p0_3_index_top_10_line_stamp_and_validate_current_state(tmp_path):
    engine_dir = tmp_path / "engine"
    (engine_dir / "docs").mkdir(parents=True, exist_ok=True)
    (engine_dir / "docs/OPERATIONS_V1_5.md").write_text("ops guide\n", encoding="utf-8")
    (engine_dir / "docs/MASTER_MIGRATION_NOTE.md").write_text("migration note\n", encoding="utf-8")

    registry = {
        "registry_version": "1",
        "registry_release_id": "v2.2.2-1",
        "products": [
            {
                "product_id": "domestic_season_snapshots",
                "semantic_grain": "domestic_season_registration_snapshot",
                "authority": "canonical",
                "business_key": ["record_key"],
                "artifact_key": "input/MASTER.xlsx",
                "generated_from": [],
                "deprecated_by": None,
                "read_policy": "canonical_only",
                "current_eligible": True,
                "independent_write_allowed": True,
                "selector": "MASTER",
            }
        ],
    }
    manifest_bytes = (
        "uid,drive_file_id,content_hash,published_release,hash_scope\n"
        f"input/MASTER.xlsx,f-master,{'a'*64},v2.2.2-1,bytes\n"
    ).encode("utf-8-sig")

    projection = {
        "release_id": "v2.2.2-1",
        "engine_sha": V222_1_BASELINE_SHA,
        "active_release_id": "v2.2.1-1",
    }
    allocation = {"status_id": "f-status"}
    id_by_key = {
        "entry/README": "f-readme",
        "derived/INDEX.md": "f-index",
        "entry/context": "f-context",
        "CURRENT_VERSION_DOC": "f-ver",
        "ai/CONTEXT_CARD.md": "f-card",
        "input/MASTER.xlsx": "f-master",
    }
    item_by_key = {
        "entry/README": {"name": "00_README_先读这个"},
        "derived/INDEX.md": {"name": "INDEX"},
        "entry/context": {"name": "02_AI_PROJECT_CONTEXT_技术手册.md"},
        "CURRENT_VERSION_DOC": {"name": "CBA-KB_v2.2.2.md"},
        "ai/CONTEXT_CARD.md": {"name": "CONTEXT_CARD.md"},
        "input/MASTER.xlsx": {"name": "MASTER.xlsx"},
    }

    expected_stamps = [
        "当前发布：v2.2.2-1",
        f"代码提交：{V222_1_BASELINE_SHA}",
        "权威国内注册主表规模：3,465 行（2017-2027 权威总表，精确行数 3465）",
    ]

    rendered_docs: Dict[str, str] = {}
    role_map = {
        "entry/README": ("readme", True),
        "derived/INDEX.md": ("index", True),
        "entry/context": ("technical_manual", False),
        "CURRENT_VERSION_DOC": ("current_version_doc", False),
    }
    for logical_key, (role, is_managed) in role_map.items():
        prev_bytes = wrap("old navigation body\n").encode("utf-8") if is_managed else b"old body\n"
        out_bytes = orch._content_preserving_candidate(
            logical_key,
            prev_bytes,
            projection,
            allocation,
            id_by_key,
            engine_dir,
            registry=registry,
            item_by_key=item_by_key,
            master_row_count=3465,
        )
        if is_managed:
            out_bytes = orch._managed_candidate(out_bytes)
        text = out_bytes.decode("utf-8")
        rendered_docs[role] = text
        top_10 = "\n".join(text.splitlines()[:10])
        for stamp in expected_stamps:
            assert stamp in top_10, f"Missing stamp {stamp!r} in top 10 lines of {logical_key}:\n{top_10}"

    metadata = current_state.target_metadata("v2.2.2-1", V222_1_BASELINE_SHA, registry)
    rendered_docs["context_card"] = current_state.generate_context_card(
        metadata, registry, manifest_bytes
    )
    status = {
        "state": "COMPLETE",
        "current_release_id": "v2.2.2-1",
        "code_commit": V222_1_BASELINE_SHA,
    }
    res = current_state.validate_current_state(status, registry, manifest_bytes, rendered_docs)
    assert res["status"] == "PASS"


# ---------------------------------------------------------------------------
# P0-4: Proxy-aware credential-free cross-origin redirects (trust_env=False)
# ---------------------------------------------------------------------------


class _SpyCredentials(Credentials):
    def __init__(self, marker: str = "synthetic-oauth-v222") -> None:
        super().__init__(token=marker)
        self.before_request_urls: List[str] = []

    @property
    def valid(self) -> bool:
        return True

    def before_request(self, request, method, url, headers) -> None:
        self.before_request_urls.append(url)
        headers["authorization"] = f"Bearer {self.token}"


def _make_requests_response(
    status_code: int,
    *,
    headers: Dict[str, str] | None = None,
    content: bytes = b"",
    url: str = "https://example.invalid/",
    reason: str = "OK",
) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status_code
    resp.reason = reason
    resp.url = url
    resp._content = content
    for k, v in (headers or {}).items():
        resp.headers[k] = v
    return resp


def test_p0_4_resolve_sanitized_env_proxies_no_proxy_and_cred_rejection(monkeypatch):
    helper = getattr(drive_http, "_resolve_sanitized_env_proxies", None)
    assert callable(helper), "Missing drive_http._resolve_sanitized_env_proxies"

    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)

    # 1. Clean HTTPS_PROXY returned for https target
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    proxies = helper("https://other-cdn.example/blob")
    assert proxies == {"https": "http://127.0.0.1:7890"}

    # 2. NO_PROXY honors exact host, suffix, and wildcard
    monkeypatch.setenv("NO_PROXY", "other-cdn.example,.internal.local")
    assert not helper("https://other-cdn.example/blob")
    assert not helper("https://sub.internal.local/blob")
    assert helper("https://external.example/blob") == {"https": "http://127.0.0.1:7890"}

    monkeypatch.setenv("NO_PROXY", "*")
    assert not helper("https://external.example/blob")
    monkeypatch.delenv("NO_PROXY", raising=False)

    # 3. Fail-closed rejection of embedded proxy credentials
    cred_proxy = "http://" + "user" + ":" + "secret" + "@127.0.0.1:7890"
    monkeypatch.setenv("HTTPS_PROXY", cred_proxy)
    with pytest.raises(ValueError, match="UNSAFE_PROXY_URL"):
        helper("https://other-cdn.example/blob")

    user_only_proxy = "http://" + "user" + "@127.0.0.1:7890"
    monkeypatch.setenv("HTTPS_PROXY", user_only_proxy)
    with pytest.raises(ValueError, match="UNSAFE_PROXY_URL"):
        helper("https://other-cdn.example/blob")

    # 4. Fail-closed rejection of invalid proxy scheme
    monkeypatch.setenv("HTTPS_PROXY", "ftp://127.0.0.1:2121")
    with pytest.raises(ValueError, match="UNSAFE_PROXY_URL"):
        helper("https://other-cdn.example/blob")


def test_p0_4_cross_origin_redirect_passes_sanitized_proxies_with_trust_env_false(monkeypatch, tmp_path):
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)

    netrc_file = tmp_path / "synth_v222.netrc"
    netrc_file.write_text(
        "machine other-cdn.example\nlogin synth-user\npass" + "word synth-val\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NETRC", str(netrc_file))
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")

    creds = _SpyCredentials()
    auth_client = drive_http.build_authorized_session(creds)
    recorded_sends: List[Dict[str, Any]] = []
    scripted = [
        _make_requests_response(
            302,
            headers={"Location": "https://other-cdn.example/blob"},
            url="https://www.googleapis.com/drive/v3/files/f-1",
            reason="Found",
        ),
        _make_requests_response(
            200,
            headers={"Content-Type": "application/octet-stream"},
            content=b"proxied-cross-origin",
            url="https://other-cdn.example/blob",
            reason="OK",
        ),
    ]

    def _fake_adapter_send(self, request, **kwargs):
        recorded_sends.append({
            "url": request.url,
            "headers": {str(k).lower(): str(v) for k, v in dict(request.headers or {}).items()},
            "proxies": dict(kwargs.get("proxies") or {}),
        })
        resp = scripted.pop(0)
        resp.request = request
        return resp

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _fake_adapter_send)

    http = drive_http.RequestsHttp(auth_client)
    try:
        resp, body = http.request(
            "https://www.googleapis.com/drive/v3/files/f-1",
            headers={"Cookie": "[REDACTED]", "Proxy-Authorization": "[REDACTED]"},
        )
        assert resp.status == 200
        assert body == b"proxied-cross-origin"
        assert http._unauth_client is not None
        assert http._unauth_client.trust_env is False
        assert len(recorded_sends) == 2
        hop1 = recorded_sends[1]
        assert hop1["proxies"].get("https") == "http://127.0.0.1:7890"
        assert "authorization" not in hop1["headers"]
        assert "proxy-authorization" not in hop1["headers"]
        assert "cookie" not in hop1["headers"]
    finally:
        http.close()

    # Embedded credentials in HTTPS_PROXY must fail closed before sending hop 1
    monkeypatch.setenv("HTTPS_PROXY", "http://" + "u" + ":" + "p" + "@127.0.0.1:7890")
    creds2 = _SpyCredentials()
    auth_client2 = drive_http.build_authorized_session(creds2)
    recorded_sends.clear()
    scripted[:] = [
        _make_requests_response(
            302,
            headers={"Location": "https://other-cdn.example/blob"},
            reason="Found",
        ),
        _make_requests_response(200, content=b"must-not-send", reason="OK"),
    ]
    http2 = drive_http.RequestsHttp(auth_client2)
    try:
        with pytest.raises(ValueError, match="UNSAFE_PROXY_URL"):
            http2.request("https://www.googleapis.com/drive/v3/files/f-1")
        assert [s["url"] for s in recorded_sends] == [
            "https://www.googleapis.com/drive/v3/files/f-1"
        ]
    finally:
        http2.close()


# ---------------------------------------------------------------------------
# P0-5: Concurrent BFS _inventory(), read-only reuse, post-mutation invalidation,
#       and prepare()/verify() Δ-entry concurrent prefetch
# ---------------------------------------------------------------------------


class _ConcurrentTelemetryDrive:
    """Thread-safe FakeDrive measuring peak concurrency, list counts, and writer thread ID."""

    index_cba_keys = orch.Drive.index_cba_keys

    def __init__(self, *, include_sha256: bool = True, read_delay_sec: float = 0.0) -> None:
        self._lock = threading.Lock()
        self.main_thread_id = threading.get_ident()
        self.include_sha256 = include_sha256
        self.read_delay_sec = read_delay_sec
        self.files: Dict[str, Dict[str, Any]] = {}
        self.list_calls = 0
        self.meta_calls = 0
        self.download_calls = 0
        self.active_list = 0
        self.peak_list = 0
        self.active_read = 0
        self.peak_read = 0
        self.off_main_writes: List[str] = []
        self.on_first_put = None
        self._put_fired = False

    def __deepcopy__(self, memo):
        clone = _ConcurrentTelemetryDrive(
            include_sha256=self.include_sha256,
            read_delay_sec=self.read_delay_sec,
        )
        memo[id(self)] = clone
        with self._lock:
            clone.main_thread_id = self.main_thread_id
            clone.files = copy.deepcopy(self.files, memo)
            clone.list_calls = self.list_calls
            clone.meta_calls = self.meta_calls
            clone.download_calls = self.download_calls
            clone.off_main_writes = list(self.off_main_writes)
            clone.on_first_put = self.on_first_put
            clone._put_fired = self._put_fired
        return clone

    def add_folder(self, fid: str, name: str, parent: str, *, role: str | None = None) -> None:
        with self._lock:
            rec = {
                "id": fid,
                "name": name,
                "mimeType": release_module.FOLDER,
                "parents": [parent] if parent else [],
                "version": "1",
                "modifiedTime": "2026-11-22T00:00:00Z",
                "data": b"",
            }
            if role:
                rec["artifact_role"] = role
            self.files[fid] = rec

    def add_file(
        self,
        fid: str,
        name: str,
        data: bytes,
        *,
        mime: str = "text/plain",
        parent: str = "data",
        include_sha256: bool | None = None,
    ) -> None:
        with self._lock:
            self.files[fid] = {
                "id": fid,
                "name": name,
                "mimeType": mime,
                "parents": [parent],
                "version": "1",
                "modifiedTime": "2026-11-22T00:00:00Z",
                "data": bytes(data),
                "include_sha256": self.include_sha256 if include_sha256 is None else include_sha256,
            }

    def _meta_unlocked(self, fid: str) -> Dict[str, Any]:
        rec = self.files[fid]
        out = {
            "id": rec["id"],
            "name": rec["name"],
            "mimeType": rec["mimeType"],
            "parents": list(rec["parents"]),
            "version": rec["version"],
            "modifiedTime": rec["modifiedTime"],
            "size": str(len(rec["data"])),
        }
        if "appProperties" in rec:
            out["appProperties"] = dict(rec["appProperties"])
        if "artifact_role" in rec:
            out["artifact_role"] = rec["artifact_role"]
        if rec.get("include_sha256", True) and not rec["mimeType"].startswith("application/vnd.google-apps."):
            out["sha256Checksum"] = digest(rec["data"])
        return out

    def _check_main_writer(self, op: str) -> None:
        if threading.get_ident() != self.main_thread_id:
            with self._lock:
                self.off_main_writes.append(op)
            raise RuntimeError(f"WRITE_OFF_MAIN_THREAD_FORBIDDEN:{op}")

    def list(self, parent: str) -> List[Dict[str, Any]]:
        with self._lock:
            self.list_calls += 1
            self.active_list += 1
            self.peak_list = max(self.peak_list, self.active_list)
        try:
            if self.read_delay_sec:
                time.sleep(self.read_delay_sec)
            with self._lock:
                return [
                    self._meta_unlocked(fid)
                    for fid, rec in self.files.items()
                    if parent in rec.get("parents", [])
                ]
        finally:
            with self._lock:
                self.active_list -= 1

    def meta(self, fid: str) -> Dict[str, Any]:
        with self._lock:
            self.meta_calls += 1
            self.active_read += 1
            self.peak_read = max(self.peak_read, self.active_read)
        try:
            if self.read_delay_sec:
                time.sleep(self.read_delay_sec)
            with self._lock:
                return self._meta_unlocked(fid)
        finally:
            with self._lock:
                self.active_read -= 1

    def get(self, fid: str) -> bytes:
        with self._lock:
            self.download_calls += 1
            self.active_read += 1
            self.peak_read = max(self.peak_read, self.active_read)
        try:
            if self.read_delay_sec:
                time.sleep(self.read_delay_sec)
            with self._lock:
                return bytes(self.files[fid]["data"])
        finally:
            with self._lock:
                self.active_read -= 1

    def get_with_meta(self, fid: str, *, known_meta=None):
        data = self.get(fid)
        with self._lock:
            meta = self._meta_unlocked(fid)
        return data, meta

    def get_managed_doc(self, fid: str) -> bytes:
        return self.get(fid)

    def document_json(self, fid: str) -> Dict[str, Any]:
        text = self.get(fid).decode("utf-8", errors="replace")
        return {"body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": text}}]}}]}}

    def put(self, fid: str, data: bytes, mime: str) -> Dict[str, Any]:
        self._check_main_writer("put")
        with self._lock:
            rec = self.files[fid]
            rec["data"] = bytes(data)
            rec["version"] = str(int(rec["version"]) + 1)
            meta = self._meta_unlocked(fid)
        if self.on_first_put is not None and not self._put_fired and fid != "status":
            self._put_fired = True
            self.on_first_put(self)
        return meta

    def put_managed_doc(self, fid: str, data: bytes) -> Dict[str, Any]:
        self._check_main_writer("put_managed_doc")
        with self._lock:
            rec = self.files[fid]
            rec["data"] = bytes(data)
            rec["version"] = str(int(rec["version"]) + 1)
            return self._meta_unlocked(fid)

    def move(self, fid: str, dest: str, src: str) -> Dict[str, Any]:
        self._check_main_writer("move")
        with self._lock:
            rec = self.files[fid]
            rec["parents"] = [dest]
            rec["version"] = str(int(rec["version"]) + 1)
            return self._meta_unlocked(fid)

    def ensure(self, parent: str, key: str, name: str, mime: str, content: bytes | None = None, *, index=None) -> str:
        self._check_main_writer("ensure")
        with self._lock:
            fid = f"{parent}__{key}"
            if fid not in self.files:
                self.files[fid] = {
                    "id": fid,
                    "name": name,
                    "mimeType": mime,
                    "parents": [parent],
                    "version": "1",
                    "modifiedTime": "2026-11-22T00:00:00Z",
                    "data": bytes(content or b""),
                    "appProperties": {"cba_key": key},
                    "include_sha256": self.include_sha256,
                }
            meta = self._meta_unlocked(fid)
            if index is not None:
                index[key] = meta
            return fid

    def ensure_copy(self, parent: str, key: str, file_id: str, name: str, *, index=None) -> str:
        self._check_main_writer("ensure_copy")
        with self._lock:
            fid = f"{parent}__{key}"
            src = self.files[file_id]
            if fid not in self.files:
                self.files[fid] = {
                    "id": fid,
                    "name": name,
                    "mimeType": src["mimeType"],
                    "parents": [parent],
                    "version": "1",
                    "modifiedTime": "2026-11-22T00:00:00Z",
                    "data": bytes(src["data"]),
                    "appProperties": {"cba_key": key},
                    "include_sha256": src.get("include_sha256", True),
                }
            meta = self._meta_unlocked(fid)
            if index is not None:
                index[key] = meta
            return fid


def test_p0_5_inventory_bfs_8_thread_concurrency_and_deterministic_parity():
    drv = _ConcurrentTelemetryDrive(read_delay_sec=0.015)
    # Build 2-level tree with 12 branches under current/evidence + forbidden history/staging folders
    for i in range(12):
        sub_id = f"sub_{i:02d}"
        leaf_id = f"leaf_{i:02d}"
        drv.add_folder(sub_id, f"sub_{i:02d}", parent="cur_root")
        drv.add_folder(leaf_id, f"leaf_{i:02d}", parent=sub_id)
        drv.add_file(f"file_{i:02d}", f"file_{i:02d}.txt", f"payload-{i}".encode(), parent=leaf_id)
    drv.add_folder("hist_root", "90_archive", parent="cur_root")
    drv.add_file("hist_file", "old.txt", b"archive", parent="hist_root")
    drv.add_folder("stg_root", "staging", parent="cur_root")
    drv.add_file("stg_file", "staged.txt", b"staged", parent="stg_root")
    drv.add_folder("noncur_dir", "historical_sub", parent="cur_root", role="historical")
    drv.add_file("noncur_file", "hist.txt", b"hist", parent="noncur_dir")

    zones = {
        "current": ["cur_root"],
        "evidence": ["ev_root"],
        "history": ["hist_root"],
        "staging": ["stg_root"],
    }
    drv.add_file("ev_01", "ev_01.md", b"evidence", parent="ev_root")

    items_1, out_zones_1 = release_module._inventory(drv, zones)
    assert 2 <= drv.peak_list <= 8, (
        f"Expected concurrent BFS _inventory peak workers in [2, 8], got {drv.peak_list}"
    )

    ids = {item["id"] for item in items_1}
    assert all(f"file_{i:02d}" in ids for i in range(12))
    assert "ev_01" in ids
    # History, staging, and NON_CURRENT folders must never be crawled
    assert "hist_file" not in ids
    assert "stg_file" not in ids
    assert "noncur_file" not in ids
    assert out_zones_1["folders"]["leaf_00"] == ["sub_00"]

    # Deterministic output on second run
    items_2, out_zones_2 = release_module._inventory(drv, zones)
    assert [i["id"] for i in items_1] == [i["id"] for i in items_2]
    assert out_zones_1 == out_zones_2


def test_p0_5_inventory_readonly_reuse_and_strict_post_mutation_invalidation(monkeypatch, tmp_path):
    monkeypatch.setattr(
        release_module,
        "verify_code_provenance",
        lambda repo, code_commit, exec_sha=None: {"status": "PASS", "code_commit": code_commit},
    )

    drv = _ConcurrentTelemetryDrive(include_sha256=True)
    reg = {
        "registry_version": "1",
        "registry_release_id": "v2.2.2-1",
        "products": [
            {
                "product_id": "domestic_season_snapshots",
                "semantic_grain": "domestic_season_registration_snapshot",
                "authority": "canonical",
                "business_key": ["record_key"],
                "artifact_key": "input/MASTER.xlsx",
                "generated_from": [],
                "deprecated_by": None,
                "read_policy": "canonical_only",
                "current_eligible": True,
                "independent_write_allowed": True,
                "selector": "MASTER",
            }
        ],
    }
    reg_bytes = yaml.safe_dump(reg, allow_unicode=True, sort_keys=False).encode("utf-8")
    master_xlsx = _make_master_xlsx(2)
    sr_csv = b"source_file_id,processing_status\nf-ev-0,accepted\n"

    # Build minimal closure-valid set across 5 zones + 1 carry-forward file
    metadata = current_state.target_metadata("v2.2.2-1", V222_1_BASELINE_SHA, reg)
    state_block = current_state.render_current_state(metadata)

    # Create 4 extra subfolders under data so each _inventory() costs 9 list() calls
    for idx in range(4):
        drv.add_folder(f"sub_{idx}", f"sub_{idx}", parent="data")

    drv.add_file("f-reg", "canonical_products.yaml", reg_bytes, mime="text/yaml", parent="config")
    drv.add_file("f-readme", "README", wrap(state_block + "# README\n").encode(), mime=orch.NATIVE_DOCUMENT, parent="root")
    drv.add_file("f-index", "INDEX", wrap(state_block + "# INDEX\n").encode(), mime=orch.NATIVE_DOCUMENT, parent="data")
    drv.add_file("f-ver", "VER.md", (state_block + "# VER\n").encode(), mime="text/markdown", parent="root")
    drv.add_file("f-master", "MASTER.xlsx", master_xlsx, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", parent="data")
    drv.add_file("f-six", "six.xlsx", _make_master_xlsx(1), mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", parent="data")
    drv.add_file("f-sr", "source_registry.csv", sr_csv, mime="text/csv", parent="data")
    drv.add_file("f-ev-0", "ev0.md", b"evidence-0\n", mime="text/markdown", parent="evidence")
    drv.add_file("f-cf-0", "cf0.txt", b"carry-forward-0\n", mime="text/plain", parent="data")
    drv.add_file("f-delta", "delta.txt", b"delta-old\n", mime="text/plain", parent="data")

    manifest_rows = [
        ("config/canonical_products.yaml", "f-reg", digest(reg_bytes), "bytes"),
        ("input/manifest.csv", "f-manifest", "", "bytes"),
        ("entry/README", "f-readme", digest(drv.files["f-readme"]["data"]), "managed_prefix"),
        ("derived/INDEX.md", "f-index", digest(drv.files["f-index"]["data"]), "managed_prefix"),
        ("CURRENT_VERSION_DOC", "f-ver", digest(drv.files["f-ver"]["data"]), "bytes"),
        ("ai/CONTEXT_CARD.md", "f-card", "", "bytes"),
        ("input/MASTER.xlsx", "f-master", digest(master_xlsx), "bytes"),
        ("facts/six.xlsx", "f-six", digest(drv.files["f-six"]["data"]), "bytes"),
        ("input/source_registry.csv", "f-sr", digest(sr_csv), "bytes"),
        ("evidence/ev0.md", "f-ev-0", digest(b"evidence-0\n"), "bytes"),
        ("code/cf0.txt", "f-cf-0", digest(b"carry-forward-0\n"), "bytes"),
        ("code/delta.txt", "f-delta", digest(b"delta-new\n"), "bytes"),
    ]
    manifest_tmp = "uid,drive_file_id,content_hash,published_release,hash_scope\n" + "\n".join(
        f"{u},{fid},{h},v2.2.2-1,{sc}" for u, fid, h, sc in manifest_rows
    ) + "\n"
    card_bytes = current_state.generate_context_card(metadata, reg, manifest_tmp.encode("utf-8-sig")).encode("utf-8")
    manifest_rows = [
        (u, fid, digest(card_bytes) if u == "ai/CONTEXT_CARD.md" else h, sc)
        for u, fid, h, sc in manifest_rows
    ]
    manifest_bytes = (
        "uid,drive_file_id,content_hash,published_release,hash_scope\n"
        + "\n".join(f"{u},{fid},{h},v2.2.2-1,{sc}" for u, fid, h, sc in manifest_rows)
        + "\n"
    ).encode("utf-8-sig")

    drv.add_file("f-card", "CONTEXT_CARD.md", card_bytes, mime="text/markdown", parent="ai")
    drv.add_file("f-manifest", "manifest.csv", manifest_bytes, mime="text/csv", parent="config")

    prev_status = {
        "state": "COMPLETE",
        "current_release_id": "v2.2.1-1",
        "code_commit": "b" * 40,
        "artifacts": [
            {"id": fid, "name": drv.files[fid]["name"], "sha256": digest(drv.files[fid]["data"])}
            for fid in ("f-reg", "f-manifest", "f-readme", "f-index", "f-ver", "f-card", "f-master", "f-six", "f-sr", "f-ev-0", "f-cf-0", "f-delta")
        ],
    }
    drv.add_file("status", "release_status.json", json.dumps(prev_status, sort_keys=True).encode(), mime="application/json", parent="config")

    cand_dir = tmp_path / "cand"
    cand_dir.mkdir()
    entry_specs = []
    for lk, fid, new_data in (
        ("config/canonical_products.yaml", "f-reg", reg_bytes),
        ("input/manifest.csv", "f-manifest", manifest_bytes),
        ("entry/README", "f-readme", drv.files["f-readme"]["data"]),
        ("derived/INDEX.md", "f-index", drv.files["f-index"]["data"]),
        ("CURRENT_VERSION_DOC", "f-ver", drv.files["f-ver"]["data"]),
        ("ai/CONTEXT_CARD.md", "f-card", card_bytes),
        ("code/delta.txt", "f-delta", b"delta-new\n"),
    ):
        p = cand_dir / f"{fid}.bin"
        p.write_bytes(new_data)
        entry_specs.append({
            "logical_key": lk,
            "id": fid,
            "name": drv.files[fid]["name"],
            "mime": drv.files[fid]["mimeType"],
            "mode": "managed_doc" if drv.files[fid]["mimeType"] == orch.NATIVE_DOCUMENT else "binary",
            "path": str(p),
        })

    closure = {
        "code_commit": V222_1_BASELINE_SHA,
        "previous_code_commit": "b" * 40,
        "baseline_release_id": "v2.2.1-1",
        "registry_key": "config/canonical_products.yaml",
        "manifest_key": "input/manifest.csv",
        "documents": {
            "readme": "entry/README",
            "index": "derived/INDEX.md",
            "context_card": "ai/CONTEXT_CARD.md",
            "current_version_doc": "CURRENT_VERSION_DOC",
        },
        "zones": {
            "current": ["root", "ai", "config", "data"],
            "history": ["archive"],
            "staging": ["staging"],
            "evidence": ["evidence"],
        },
        "protected": [
            {"id": "f-master", "name": "MASTER.xlsx", "sha256": digest(master_xlsx), "mode": "binary", "kind": "master"},
            {"id": "f-six", "name": "six.xlsx", "sha256": digest(drv.files["f-six"]["data"]), "mode": "binary", "kind": "six_table"},
            {"id": "f-sr", "name": "source_registry.csv", "sha256": digest(sr_csv), "mode": "binary", "kind": "source_registry"},
            {"id": "f-ev-0", "name": "ev0.md", "sha256": digest(b"evidence-0\n"), "mode": "binary", "kind": "evidence"},
        ],
    }

    # 1. During prepare() read-only phase (freeze_closure + validate_closure + _verify_carry_forward_if_incremental),
    #    1 single _inventory() traverses 9 folders (4 current + 1 evidence + 4 sub_*) = 9 list() calls.
    #    Without reuse, 3 separate _inventory() calls would execute 27 list() calls!
    outbox = tmp_path / "outbox"
    drv.list_calls = 0
    release_module.prepare(
        drv,
        outbox,
        "v2.2.2-1",
        entry_specs,
        "archive",
        "status",
        carry_forward_artifacts=True,
        closure=closure,
        environment="production",
        code_commit=V222_1_BASELINE_SHA,
        archive_format="bundle_v1",
        incremental_mode=True,
    )
    assert drv.list_calls <= 12, (
        f"Expected single reused _inventory() during prepare() (<=12 list calls), got {drv.list_calls}"
    )

    # 2. Strict post-mutation invalidation: tamper f-cf-0 right after first write in publish()
    #    Post-write verify() MUST fetch a fresh _inventory() and catch the tamper fail-closed!
    tamper_drv = copy.deepcopy(drv)

    def _tamper_after_write(d: _ConcurrentTelemetryDrive) -> None:
        with d._lock:
            d.files["f-cf-0"]["data"] = b"carry-forward-TAMPERED\n"
            d.files["f-cf-0"]["version"] = "99"

    tamper_drv.on_first_put = _tamper_after_write
    tamper_outbox = tmp_path / "outbox_tamper"
    import shutil
    shutil.copytree(outbox, tamper_outbox)
    with pytest.raises((RuntimeError, ValueError), match="CARRY_FORWARD_REMOTE_HASH_MISMATCH|REMOTE_HASH_MISMATCH"):
        release_module.publish(tamper_drv, tamper_outbox, single_writer=True)

    # 3. Clean publish() succeeds and reuses preflight _inventory() (not 2x in preflight)
    drv.list_calls = 0
    journal = release_module.publish(drv, outbox, single_writer=True)
    assert journal["state"] == "COMPLETE"
    assert drv.off_main_writes == []


def test_p0_5_prepare_and_verify_delta_prefetch_concurrent_and_main_thread_writes(tmp_path):
    drv = _ConcurrentTelemetryDrive(include_sha256=False, read_delay_sec=0.015)
    cand_dir = tmp_path / "cand_prefetch"
    cand_dir.mkdir()
    entries = []
    for idx in range(10):
        fid = f"f-delta-{idx:02d}"
        drv.add_file(fid, f"delta_{idx:02d}.txt", f"old-{idx}\n".encode(), parent="data", include_sha256=False)
        p = cand_dir / f"delta_{idx:02d}.txt"
        p.write_bytes(f"new-{idx}\n".encode())
        entries.append({
            "id": fid,
            "name": f"delta_{idx:02d}.txt",
            "mime": "text/plain",
            "mode": "binary",
            "path": str(p),
        })
    drv.add_file(
        "status",
        "release_status.json",
        json.dumps({"state": "INITIAL", "current_release_id": "v2.2.1-1"}).encode(),
        mime="application/json",
        parent="config",
    )

    # 1. prepare() must prefetch Δ entries concurrently (2 <= peak <= 8)
    drv.peak_read = 0
    outbox = tmp_path / "outbox_prefetch"
    release_module.prepare(drv, outbox, "v2.2.2-1", entries, "archive", "status", archive_format="bundle_v1")
    assert 2 <= drv.peak_read <= 8, (
        f"Expected concurrent Δ-entry prefetch in prepare() with peak in [2, 8], got {drv.peak_read}"
    )

    # 2. publish() keeps all writes 100% on main thread
    journal = release_module.publish(drv, outbox, single_writer=True)
    assert journal["state"] == "COMPLETE"
    assert drv.off_main_writes == []

    # 3. verify() must prefetch fallback readbacks concurrently (2 <= peak <= 8)
    drv.peak_read = 0
    res = release_module.verify(drv, outbox)
    assert res["verified"] == 10
    assert 2 <= drv.peak_read <= 8, (
        f"Expected concurrent Δ-entry fallback readback in verify() with peak in [2, 8], got {drv.peak_read}"
    )
