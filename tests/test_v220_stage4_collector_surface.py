"""Frozen acceptance & wiring test suite for v2.2.0-1 Stage 4 (REQ-COL-040-1).

All fixtures are 100% synthetic. Zero network calls, zero Drive API calls,
zero real private instance paths, zero raw Drive IDs.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tomllib
from typing import Any, Dict

import pytest
import yaml

from automation.hardening import validate_release_identity
from cba_kb import consumer_surface
from cba_kb.instance import Instance
from cba_kb.orchestration.release import prepare_v215
from tests.test_release_orchestration import canonical_registry, projection_inputs

ROOT = Path(__file__).resolve().parents[1]
V220_1_BASELINE_SHA = "fa9793c6db76e15880610ced728a7b0d563da604"
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


def _synthetic_bundles() -> list[Dict[str, Any]]:
    foreign_table = "\n".join([
        "2026-2027赛季CBA联赛外籍球员注册信息（截至11月18日17:00）",
        "| 俱乐部 | 外籍球员 |  | 国籍（以注册时提供护照为准） | 场上位置 | 球衣号码 |",
        "| --- | --- | --- | --- | --- | --- |",
        "| 深圳新世纪 | SYNTH ALPHA | 合成阿尔法 | 美国 | PG | 11 |",
        "| 青岛国信海天 | SYNTH BETA | 合成贝塔 | 美国 | C | 55 |",
    ])
    fake_drive_id = "1" + "SynthRawDriveTokenAlpha9876543210"
    fake_local_user = "/Users/" + "synth_operator/private/raw_capture.md"
    fake_home_path = "/home/" + "synth_runner/workspace/secret.json"
    return [
        {
            "doc_id": "doc_dom_2425_synth0000001",
            "title": "2024-2025赛季CBA联赛国内球员注册信息",
            "source_tier": "T1",
            "event_class": "注册与转会",
            "published_at": "2024-08-06",
            "acquired_at": "2026-11-21T05:58:37Z",
            "canonical_url": "https://example.org/register/2024-2025",
            "upstream_origin": "CBA官方注册公示",
            "raw_sha256": "1" * 64,
            "matched_persons": [],
            "body": "2024-2025赛季CBA联赛国内球员注册信息\n深圳新世纪\n合成球员甲 | 延续 | B类\n",
        },
        {
            "doc_id": "doc_dom_2526_synth0000002",
            "title": "2025-2026赛季CBA联赛国内球员注册信息",
            "source_tier": "T1",
            "event_class": "注册与转会",
            "published_at": "2025-08-06",
            "acquired_at": "2026-11-21T05:58:37Z",
            "canonical_url": "https://example.org/register/2025-2026",
            "upstream_origin": "CBA官方注册公示",
            "raw_sha256": "2" * 64,
            "matched_persons": [],
            "body": "2025-2026赛季CBA联赛国内球员注册信息\n深圳新世纪\n合成球员乙 | 合同期内 | A1类\n",
        },
        {
            "doc_id": "doc_dom_2627_synth0000003",
            "title": "2026-2027赛季CBA联赛国内球员注册信息",
            "source_tier": "T1",
            "event_class": "注册与转会",
            "published_at": "2026-08-05",
            "acquired_at": "2026-11-21T05:58:37Z",
            "canonical_url": "https://example.org/register/2026-2027",
            "upstream_origin": "CBA官方注册公示",
            "raw_sha256": "3" * 64,
            "matched_persons": [],
            "body": "2026-2027赛季CBA联赛国内球员注册信息\n广州龙狮\n合成球员乙 | 延续 | B类\n",
        },
        {
            "doc_id": "doc_for_2627_synth0000004",
            "title": "2026-2027赛季CBA联赛外籍球员注册信息（截至11月18日17点）",
            "source_tier": "T1",
            "event_class": "注册与转会",
            "published_at": "2026-11-18",
            "acquired_at": "2026-11-21T05:58:37Z",
            "canonical_url": "https://example.org/register/foreign-2026-2027",
            "upstream_origin": "CBA官方外籍球员注册公示",
            "raw_sha256": "4" * 64,
            "matched_persons": [],
            "body": foreign_table,
        },
        {
            "doc_id": "doc_ann_synth00000000005",
            "title": "测试俱乐部签约外援合成公告",
            "source_tier": "T1.5",
            "event_class": "注册与转会",
            "published_at": None,
            "acquired_at": "2026-11-19T02:16:38Z",
            "canonical_url": "https://example.org/news/ann-001",
            "upstream_origin": "测试俱乐部官微",
            "raw_sha256": "5" * 64,
            "matched_persons": ["合成球员丙"],
            "body": (
                "测试俱乐部正式与外籍球员合成外援完成签约（待CBA公司正式审核公示）。\n"
                f"泄露测试标记: {fake_drive_id} {fake_local_user} {fake_home_path}\n"
            ),
        },
        {
            "doc_id": "doc_ann_synth00000000006",
            "title": "2026-2027赛季NBL联赛工作会议",
            "source_tier": "T1",
            "event_class": "联赛政策",
            "published_at": None,
            "acquired_at": "2026-11-21T05:58:37Z",
            "canonical_url": "https://example.org/news/ann-002",
            "upstream_origin": "CBA官方公告",
            "raw_sha256": "6" * 64,
            "matched_persons": [],
            "body": "2026-2027赛季NBL联赛工作会议召开，讨论升降级对接与赛风赛纪要求。",
        },
    ]


def _synthetic_reg_diff() -> Dict[str, Any]:
    return {
        "statement_notice": (
            "Candidate diff derived from CBA official public registration pages. "
            "Statement != Fact. Evidence != Fact. Unknown != 0."
        ),
        "club_filter": None,
        "old_bundle": {
            "bundle_id": "9f6f58006e04",
            "title": "2025-2026赛季CBA联赛国内球员注册信息",
            "clubs_count": 20,
            "players_count": 335,
            "raw_sha256": "2" * 64,
        },
        "new_bundle": {
            "bundle_id": "6189d1b38b59",
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
        "added_players": [
            {
                "player": "合成新秀丁",
                "club": "上海久事",
                "new_line": 12,
                "fields": {"合同类别": "A1类", "注册类型": "首次"},
            }
        ],
        "removed_players": [
            {
                "player": "合成老将戊",
                "club": "广东宏远",
                "old_line": 88,
                "fields": {"合同类别": "C类"},
            }
        ],
        "transferred_players": [
            {
                "player": "合成球员乙",
                "from_club": "深圳新世纪",
                "to_club": "广州龙狮",
                "old_line": 42,
                "new_line": 55,
                "old_contract_type": "A1类",
                "new_contract_type": "B类",
            }
        ],
        "contract_changed_players": [
            {
                "player": "合成球员甲",
                "club": "深圳新世纪",
                "old_line": 10,
                "new_line": 11,
                "changed_fields": {
                    "合同类别": {"old": "A1类", "new": "B类"},
                    "合同到期日": {"old": "2026年7月31日", "new": "2030年7月31日"},
                },
            }
        ],
    }


# ---------------------------------------------------------------------------
# (a) Release spec registration, baseline ancestry & invariants
# ---------------------------------------------------------------------------


def test_v220_1_release_spec_registration_and_invariants():
    expected = {
        "product_baseline_sha": V220_1_BASELINE_SHA,
        "enable_consumer_surface_v215": True,
        "enable_operational_kb_v220": True,
    }
    assert prepare_v215.V215_RELEASE_SPECS.get("v2.2.0-1") == expected
    assert prepare_v215._require_release("v2.2.0-1") == expected
    assert prepare_v215._release_spec("v2.2.0-1") == expected
    assert prepare_v215._uses_closure("v2.2.0-1") is True
    assert prepare_v215._uses_technical_manual("v2.2.0-1") is True

    identity = validate_release_identity({
        "target_product_version": "v2.2.0",
        "proposed_release_attempt": "v2.2.0-1",
        "product_candidate_sha": V220_1_BASELINE_SHA,
        "release_execution_role": "RELEASE_EXECUTOR",
    })
    assert identity["status"] == "PASS"
    assert identity["facts"]["canonical_product_baseline_sha"] == V220_1_BASELINE_SHA

    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", V220_1_BASELINE_SHA, head],
        cwd=ROOT,
        check=False,
    ).returncode == 0

    actual_sha = hashlib.sha256(
        (ROOT / "scripts" / "prepare_production.py").read_bytes()
    ).hexdigest()
    assert actual_sha == FROZEN_PREPARE_PRODUCTION_SHA256

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == "2.0.2"


# ---------------------------------------------------------------------------
# (b) Collector official digest deterministic rendering & redaction
# ---------------------------------------------------------------------------


def test_render_collector_official_digest_md_content_redaction_and_determinism():
    assert getattr(consumer_surface, "COLLECTOR_DIGEST_FILENAME", None) == (
        "CBA_官方采集动态与注册增量摘要_CURRENT.md"
    )
    render_fn = getattr(consumer_surface, "render_collector_official_digest_md", None)
    assert callable(render_fn), "Missing consumer_surface.render_collector_official_digest_md"

    bundles = _synthetic_bundles()
    reg_diff = _synthetic_reg_diff()
    fake_drive_id = "1" + "SynthRawDriveTokenAlpha9876543210"
    extra_drive_id = "1" + "ExtraKnownDriveIdToken1234567890"

    digest_1 = render_fn(
        bundles=bundles,
        reg_diff=reg_diff,
        release_id="v2.2.0-1",
        engine_sha=V220_1_BASELINE_SHA,
        known_drive_ids=(extra_drive_id,),
    )
    digest_2 = render_fn(
        bundles=list(reversed(bundles)),
        reg_diff=reg_diff,
        release_id="v2.2.0-1",
        engine_sha=V220_1_BASELINE_SHA,
        known_drive_ids=(extra_drive_id,),
    )
    assert isinstance(digest_1, bytes)
    assert digest_1 == digest_2, "Digest rendering must be deterministic regardless of bundle input order"

    text = digest_1.decode("utf-8")
    assert "Statement != Fact" in text
    assert "Evidence != Fact" in text
    assert "Unknown != 0" in text
    assert "evidence / candidate_observation" in text

    # Foreign registration snapshot table verbatim
    assert "SYNTH ALPHA" in text and "合成阿尔法" in text
    assert "SYNTH BETA" in text and "合成贝塔" in text

    # 3-season domestic registration & 25-26 -> 26-27 reg_diff observations
    assert "2024-2025赛季CBA联赛国内球员注册信息" in text
    assert "2025-2026赛季CBA联赛国内球员注册信息" in text
    assert "2026-2027赛季CBA联赛国内球员注册信息" in text
    assert "合成球员乙" in text and "深圳新世纪" in text and "广州龙狮" in text
    assert "合成球员甲" in text and "2030年7月31日" in text
    assert "合成新秀丁" in text and "合成老将戊" in text

    # Structured official announcements index + excerpts
    for b in bundles:
        assert b["doc_id"] in text
        assert b["canonical_url"] in text
        assert b["raw_sha256"] in text
        assert b["source_tier"] in text
    assert "测试俱乐部正式与外籍球员合成外援完成签约" in text
    assert "升降级对接与赛风赛纪要求" in text

    # Redaction of raw Drive IDs & local paths
    assert fake_drive_id not in text
    assert extra_drive_id not in text
    assert "/Users/" + "synth_operator" not in text
    assert "/home/" + "synth_runner" not in text
    assert V220_1_BASELINE_SHA in text  # 40-hex commit SHA preserved
    assert text.endswith("end_marker: true\n")


# ---------------------------------------------------------------------------
# (c) Idempotent merge of operational_checkpoint into consumer document index
# ---------------------------------------------------------------------------


def test_merge_operational_docs_into_consumer_index_idempotent_and_redacted():
    merge_fn = getattr(consumer_surface, "merge_operational_docs_into_consumer_index", None)
    assert callable(merge_fn), "Missing consumer_surface.merge_operational_docs_into_consumer_index"

    fake_drive_id = "1" + "SynthIndexDriveToken987654321012"
    existing_index = json.dumps({
        "schema_version": 1,
        "documents": [
            {
                "doc_id": "doc_existing_001",
                "title": "历史访谈记录",
                "season": "2024-2025",
                "drive_file_id": fake_drive_id,
                "file_id": fake_drive_id,
                "local_path": "/Users/" + "synth_operator/doc_existing_001.md",
            }
        ],
    }).encode("utf-8")

    checkpoint = {
        "schema_version": 1,
        "consumed_bundles": {
            "2026-11/ann01.md": {
                "doc_id": "doc_ann_synth00000000005",
                "title": "测试俱乐部签约外援合成公告",
                "canonical_url": "https://example.org/news/ann-001",
                "source_tier": "T1.5",
                "completeness": "full",
                "published_at": None,
                "content_hash": "a" * 64,
                "file_sha256": "b" * 64,
                "matched_persons": ["合成球员丙"],
                "disclosure_posture": None,
                "drive_file_id": fake_drive_id,
                "file_id": fake_drive_id,
                "local_path": "/home/" + "synth_runner/ann01.md",
            },
            "2026-11/for01.md": {
                "doc_id": "doc_for_2627_synth0000004",
                "title": "2026-2027赛季CBA联赛外籍球员注册信息（截至11月18日17点）",
                "canonical_url": "https://example.org/register/foreign-2026-2027",
                "source_tier": "T1",
                "completeness": "full",
                "published_at": "2026-11-18",
                "content_hash": "c" * 64,
                "file_sha256": "d" * 64,
                "matched_persons": [],
                "disclosure_posture": None,
            },
        },
    }

    merged_1 = merge_fn(existing_index, checkpoint, known_drive_ids=(fake_drive_id,))
    merged_2 = merge_fn(merged_1, checkpoint, known_drive_ids=(fake_drive_id,))
    assert merged_1 == merged_2, "Re-running merge must be strictly idempotent"

    decoded = merged_1.decode("utf-8")
    assert fake_drive_id not in decoded
    assert "drive_file_id" not in decoded
    assert '"file_id"' not in decoded
    assert "/Users/" + "synth_operator" not in decoded
    assert "/home/" + "synth_runner" not in decoded

    parsed = json.loads(decoded)
    assert parsed["unique_document_count"] == 3
    doc_ids = [d["doc_id"] for d in parsed["documents"]]
    assert sorted(doc_ids) == [
        "doc_ann_synth00000000005",
        "doc_existing_001",
        "doc_for_2627_synth0000004",
    ]
    assert len(set(doc_ids)) == len(doc_ids)


# ---------------------------------------------------------------------------
# (d) Guide & field dictionary routing guidance + v2.1.5-1 backward compat
# ---------------------------------------------------------------------------


def test_consumer_guide_and_dictionary_v220_routing_and_v215_backward_compat():
    guide_v220 = consumer_surface.render_consumer_surface_guide_section(
        release_id="v2.2.0-1", master_row_count=3465
    )
    dict_v220 = consumer_surface.render_consumer_field_dictionary_md(
        release_id="v2.2.0-1", engine_sha=V220_1_BASELINE_SHA, master_row_count=3465
    ).decode("utf-8")

    for text in (guide_v220, dict_v220):
        assert "CBA_官方采集动态与注册增量摘要_CURRENT.md" in text
        assert "CBA_文档索引_脱敏视图_CURRENT.json" in text
        assert "evidence / candidate_observation" in text
        assert "Statement != Fact" in text

    # Backward compatibility for v2.1.5-1 (must NOT add collector digest line unless requested)
    guide_v215 = consumer_surface.render_consumer_surface_guide_section(
        release_id="v2.1.5-1", master_row_count=3465
    )
    dict_v215 = consumer_surface.render_consumer_field_dictionary_md(
        release_id="v2.1.5-1", engine_sha="a" * 40, master_row_count=3465
    ).decode("utf-8")
    assert "CBA_官方采集动态与注册增量摘要_CURRENT.md" not in guide_v215
    assert "CBA_官方采集动态与注册增量摘要_CURRENT.md" not in dict_v215
    assert "3,465" in guide_v215 and "3,465" in dict_v215
    assert "date_year_inferred" in guide_v215 and "date_year_inferred" in dict_v215

    # Explicit opt-in flag also works
    guide_opt_in = consumer_surface.render_consumer_surface_guide_section(
        release_id="v2.1.5-1", master_row_count=3465, include_collector_digest=True
    )
    assert "CBA_官方采集动态与注册增量摘要_CURRENT.md" in guide_opt_in


# ---------------------------------------------------------------------------
# (e) Real Stage 4 wiring: _build_instance_consumer_targets + project_targets
# ---------------------------------------------------------------------------


def test_stage4_wiring_v220_1_builds_digest_and_merged_index_without_bulk_raw(
    monkeypatch, tmp_path
):
    instance = _make_synthetic_instance(tmp_path)
    ops_dir = instance.data_root / "ops_sync_v040_1"
    ops_dir.mkdir(parents=True, exist_ok=True)

    bundles = _synthetic_bundles()
    consumed_bundles: Dict[str, Any] = {}
    for idx, b in enumerate(bundles):
        doc_id = b["doc_id"]
        doc_dir = instance.document_archive_root / doc_id
        if idx < len(bundles) - 1:
            # Normal archive layout: raw/*.md with YAML frontmatter + body
            raw_dir = doc_dir / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            fm = {
                "bundle_schema": "acquisition_bundle_v1",
                "item_id": f"item_{idx}",
                "canonical_url": b["canonical_url"],
                "upstream_origin": b["upstream_origin"],
                "source_id": "synth-source",
                "source_tier": b["source_tier"],
                "event_class": b["event_class"],
                "matched_persons": b["matched_persons"],
                "published_at": b["published_at"],
                "acquired_at": b["acquired_at"],
                "raw_sha256": b["raw_sha256"],
                "completeness": "full",
                "title": b["title"],
            }
            md_text = "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n" + b["body"]
            (raw_dir / f"capture_{idx:04d}.md").write_text(md_text, encoding="utf-8")
        else:
            # Fallback layout: no raw/*.md, only text.txt + checkpoint metadata
            doc_dir.mkdir(parents=True, exist_ok=True)
            (doc_dir / "text.txt").write_text(b["body"], encoding="utf-8")

        consumed_bundles[f"2026-11/bundle_{idx:02d}.md"] = {
            "doc_id": doc_id,
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
        json.dumps({"schema_version": 1, "consumed_bundles": consumed_bundles}, ensure_ascii=False),
        encoding="utf-8",
    )
    (ops_dir / "reg_diff_2526_2627.json").write_text(
        json.dumps(_synthetic_reg_diff(), ensure_ascii=False),
        encoding="utf-8",
    )

    master_csv = (
        "record_key,season,club_id,player,source_file_id\n"
        "2025-2026|shenzhen|p1,2025-2026,shenzhen,合成球员,src_01\n"
    ).encode("utf-8-sig")
    sr_csv = (
        "source_id,drive_file_id,source_title,season\n"
        "drive:1SynthDriveIdAlpha1234567890123,1SynthDriveIdAlpha1234567890123,合成公示.pdf,2025-2026\n"
    ).encode("utf-8-sig")
    existing_doc_idx = json.dumps({
        "schema_version": 1,
        "documents": [{"doc_id": "doc_legacy_000", "title": "旧版文档索引条目"}],
    }).encode("utf-8")

    bytes_by_id = {
        "master-csv-id": master_csv,
        "sr-id": sr_csv,
        "doc-idx-id": existing_doc_idx,
    }
    monkeypatch.setattr(prepare_v215, "snapshot", lambda _d, fid, _m="binary": (bytes_by_id[fid], {"id": fid}))

    manifest_rows = [
        {"drive_file_id": "master-csv-id", "uid": "derived/MASTER.csv"},
        {"drive_file_id": "sr-id", "uid": "input/source_registry.csv"},
        {"drive_file_id": "doc-idx-id", "uid": "data/normalized_document_index.json"},
    ]
    targets = prepare_v215._build_instance_consumer_targets(
        object(),
        instance,
        release_id="v2.2.0-1",
        engine_sha=V220_1_BASELINE_SHA,
        manifest_rows=manifest_rows,
        data_parent="data-folder",
    )

    assert "consumer/document_index_current.json" in targets
    assert "consumer/collector_official_digest_current.md" in targets
    digest_spec = targets["consumer/collector_official_digest_current.md"]
    assert digest_spec["name"] == "CBA_官方采集动态与注册增量摘要_CURRENT.md"
    assert digest_spec["publish_parent"] == "data-folder"

    # Zero bulk raw bundle files in declared consumer targets
    assert not any("raw/" in k or "bundle_" in k for k in targets)

    merged_idx = json.loads(targets["consumer/document_index_current.json"]["content"].decode("utf-8"))
    assert merged_idx["unique_document_count"] == 7
    merged_ids = {d["doc_id"] for d in merged_idx["documents"]}
    assert "doc_legacy_000" in merged_ids
    assert "doc_ann_synth00000000006" in merged_ids

    digest_text = digest_spec["content"].decode("utf-8")
    assert "SYNTH ALPHA" in digest_text
    assert "升降级对接与赛风赛纪要求" in digest_text  # text.txt fallback worked
    assert "合成球员乙" in digest_text
    assert digest_text.endswith("end_marker: true\n")

    # Full project_targets integration with v2.2.0-1
    inputs = projection_inputs()
    projected = prepare_v215.project_targets(
        release_id="v2.2.0-1",
        engine_sha=V220_1_BASELINE_SHA,
        status_id=inputs["status_id"],
        archive_id=inputs["archive_id"],
        previous_status=inputs["previous_status"],
        previous_targets=inputs["previous_targets"],
        manifest_rows=inputs["manifest_rows"],
        tracked=inputs["tracked"],
        tracked_hashes=inputs["tracked_hashes"],
        code_parent="scripts",
        declared_consumer_targets=targets,
    )
    assert projected["state"] == "PROJECTED"
    assert projected["release_id"] == "v2.2.0-1"
    assert "consumer/collector_official_digest_current.md" in projected["final_current_logical_keys"]
    assert "consumer/document_index_current.json" in projected["final_current_logical_keys"]
