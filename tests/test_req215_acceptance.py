"""Frozen acceptance test suite for REQ-215 (v2.1.5).

Written BEFORE Commit 2 implementation (Commit 1 differential falsification):
all tests collect cleanly with zero ImportError/SyntaxError on baseline main
and fail solely because REQ-215 business requirements are not yet implemented.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import importlib
import io
import json
from pathlib import Path
from types import MappingProxyType
import pytest

from cba_kb import aliases, kc_cleanroom, kc_render, oracle
from cba_kb.kc_local_producer import build_local_projection, verify_local_projection

ROOT = Path(__file__).resolve().parents[1]
FROZEN_PREPARE_PRODUCTION_SHA = (
    "0ccb79429fb4ec5e270a3154af987475ae75bd4ef81de48c1e665a3b93cf4573"
)


def _import_consumer_surface():
    try:
        return importlib.import_module("cba_kb.consumer_surface")
    except ModuleNotFoundError as exc:
        pytest.fail(f"cba_kb.consumer_surface not implemented yet: {exc}")


def _import_prepare_v215():
    try:
        return importlib.import_module("cba_kb.orchestration.release.prepare_v215")
    except ModuleNotFoundError as exc:
        pytest.fail(
            f"cba_kb.orchestration.release.prepare_v215 not implemented yet: {exc}"
        )


# ---------------------------------------------------------------------------
# AC-215-P0-3A: Authoritative 20-club team_id -> Chinese club name mapping
# ---------------------------------------------------------------------------


def test_ac215_p03a_authoritative_20_club_team_id_mapping_and_fallback():
    assert hasattr(aliases, "CBA_TEAM_ID_TO_ZH"), "Missing CBA_TEAM_ID_TO_ZH"
    assert hasattr(aliases, "CBA_TEAM_ID_TO_CANONICAL_ID"), "Missing CBA_TEAM_ID_TO_CANONICAL_ID"
    assert hasattr(aliases, "resolve_team_id_zh"), "Missing resolve_team_id_zh"
    assert hasattr(aliases, "format_club_team_display"), "Missing format_club_team_display"

    team_map = aliases.CBA_TEAM_ID_TO_ZH
    assert isinstance(team_map, MappingProxyType)
    assert len(team_map) == 20
    assert team_map["29131"] == "深圳马可波罗"
    assert team_map["29129"] == "辽宁本钢"
    assert team_map["100074683"] == "宁波町渥"

    clubs = aliases.Clubs(ROOT).clubs
    for team_id, canon_id in aliases.CBA_TEAM_ID_TO_CANONICAL_ID.items():
        assert team_id in team_map
        assert canon_id in clubs

    assert aliases.resolve_team_id_zh("29131") == "深圳马可波罗"
    assert aliases.resolve_team_id_zh(29131) == "深圳马可波罗"
    assert aliases.resolve_team_id_zh("999999") is None
    assert aliases.resolve_team_id_zh(None) is None

    known_disp = aliases.format_club_team_display("29131")
    assert "深圳马可波罗" in known_disp and "29131" in known_disp
    unknown_disp = aliases.format_club_team_display("999999")
    assert unknown_disp is not None and "999999" in unknown_disp


# ---------------------------------------------------------------------------
# AC-215-P0-3B: Self-describing research card + backward-compatible local proj
# ---------------------------------------------------------------------------


def test_ac215_p03b_research_card_self_describing_and_local_compat(tmp_path):
    assert hasattr(kc_render, "research_card_consumer_filename")
    assert hasattr(kc_render, "slice_consumer_filename")

    fname = kc_render.research_card_consumer_filename(
        player_name="林测试", player_id="907766", season="2025-2026"
    )
    assert fname == "研究卡_林测试_907766_2025-2026_CURRENT.md"
    sname = kc_render.slice_consumer_filename(
        player_name="林测试", player_id="907766", season="2025-2026", match_type="REGULAR"
    )
    assert sname == "研究切片_林测试_907766_2025-2026_REGULAR_CURRENT.md"

    fx = copy.deepcopy(kc_cleanroom.build_cleanroom_fixture())
    # Set numeric official team_id 29131 to test Chinese club resolution in card output
    fx["stats_records"][0]["team_id"] = "29131"
    fx["stats_records"][0]["team_name"] = None

    out_dir = tmp_path / "proj"
    build_local_projection(fixture=fx, output_dir=str(out_dir), engine_candidate="test-215")
    assert verify_local_projection(str(out_dir))["valid"] is True

    # Guardrail 2: keep research_card.md and 9-file manifest intact by default
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["files"]) == 9
    assert (out_dir / "research_card.md").is_file()
    assert manifest.get("consumer_research_card_filename", "").startswith("研究卡_林测试_")

    card_text = (out_dir / "research_card.md").read_text(encoding="utf-8")
    first_line = card_text.splitlines()[0]
    assert first_line.startswith("# 人物研究卡｜林测试")
    assert "CURRENT" in first_line
    assert "Research Card (Serving Projection)" in first_line
    assert "深圳马可波罗" in card_text and "29131" in card_text

    # Stage 4 promoted research slice artifact builder
    cs = _import_consumer_surface()
    assert hasattr(cs, "build_promoted_research_slice_artifacts")
    promoted = cs.build_promoted_research_slice_artifacts(
        fx,
        player_uid=kc_cleanroom.A,
        player_name="林测试",
        player_id="synth_player_01",
        season="2025-2026",
        match_type="REGULAR",
        passage_label="训练摘录",
    )
    names = {spec["name"] for spec in promoted.values()}
    assert "研究卡_林测试_synth_player_01_2025-2026_CURRENT.md" in names
    assert "研究切片_林测试_synth_player_01_2025-2026_REGULAR_CURRENT.md" in names
    assert "研究摘录_林测试_synth_player_01_2025-2026_训练摘录_CURRENT.md" in names
    for spec in promoted.values():
        assert isinstance(spec["content"], bytes) and len(spec["content"]) > 0
        assert spec["mime"] == "text/markdown"


# ---------------------------------------------------------------------------
# AC-215-P0-2: Consumer surface naming, dynamic row binding & disambiguation
# ---------------------------------------------------------------------------


def test_ac215_p02_consumer_surface_dynamic_row_count_and_disambiguation(tmp_path):
    cs = _import_consumer_surface()
    for sym in (
        "compute_master_row_count",
        "render_consumer_surface_guide_section",
        "render_consumer_field_dictionary_md",
    ):
        assert hasattr(cs, sym), f"Missing {sym} in cba_kb.consumer_surface"

    header = "record_key,season,club,player_name\n"
    sample_csv = (header + "".join(f"rk_{i},2025-2026,深圳新世纪,示例球员\n" for i in range(123))).encode("utf-8")
    assert cs.compute_master_row_count(sample_csv) == 123

    guide = cs.render_consumer_surface_guide_section(release_id="v2.1.5-1", master_row_count=3465)
    assert "CBA_国内球员注册权威总表_2017-2027_CURRENT.csv" in guide
    assert "CBA_核心数据表字段字典与权威口径_CURRENT.md" in guide
    assert "3,465" in guide
    assert "3,451" in guide  # marked explicitly as superseded historical count
    assert "date_year_inferred" in guide
    assert "source_registry" in guide

    dict_md = cs.render_consumer_field_dictionary_md(
        release_id="v2.1.5-1", engine_sha="a" * 40, master_row_count=3465
    ).decode("utf-8")
    assert "3,465" in dict_md
    assert "date_year_inferred" in dict_md
    assert "registration_status_events" in dict_md
    assert "source_registry" in dict_md

    # Integration with prepare_v215._content_preserving_candidate (dynamic count)
    # while scripts/prepare_production.py stays byte-frozen and forbids v2.1.5-1
    prep_v215 = _import_prepare_v215()
    assert (
        hashlib.sha256((ROOT / "scripts/prepare_production.py").read_bytes()).hexdigest()
        == FROZEN_PREPARE_PRODUCTION_SHA
    )
    with pytest.raises(prep_v215.ProjectionError, match="RELEASE_ID_FORBIDDEN:v2.1.5"):
        prep_v215._require_release("v2.1.5")

    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs/OPERATIONS_V1_5.md").write_text("ops body\n", encoding="utf-8")
    (tmp_path / "docs/MASTER_MIGRATION_NOTE.md").write_text(
        (ROOT / "docs/MASTER_MIGRATION_NOTE.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    cand = prep_v215._content_preserving_candidate(
        "entry/README",
        b"legacy line\n",
        {"release_id": "v2.1.5-1", "engine_sha": "a" * 40, "active_release_id": "v2.1.4"},
        {"status_id": "status"},
        {"entry/README": "id-readme", "derived/MASTER.csv": "id-master"},
        tmp_path,
        master_row_count=3465,
        item_by_key={
            "entry/README": {"name": "00_README_先读这个"},
            "derived/MASTER.csv": {"name": "MASTER.csv"},
        },
    ).decode("utf-8")
    assert "3,465" in cand
    assert "date_year_inferred" in cand
    assert "CURRENT" in cand

    # Historical note in repo doc clarifies 3451 is historical v1.1 baseline
    mig = (ROOT / "docs/MASTER_MIGRATION_NOTE.md").read_text(encoding="utf-8")
    assert "历史" in mig and ("动态" in mig or "3451" in mig)
    assert "2026-09-10" in mig


# ---------------------------------------------------------------------------
# AC-215-P1: Raw drive_file_id redaction in consumer-facing exports & controls
# ---------------------------------------------------------------------------


def test_ac215_p1_consumer_exports_and_control_mapping_hide_raw_drive_ids(tmp_path):
    cs = _import_consumer_surface()
    for sym in (
        "export_consumer_source_registry_csv",
        "export_consumer_document_index",
        "scrub_raw_drive_ids_for_consumer",
    ):
        assert hasattr(cs, sym), f"Missing {sym}"

    fake_raw_id = "1" + "AbCdEfGhIjKlMnOpQrStUvWxYz234567"
    fake_parent = "1" + "ZyXwVuTsRqPoNmLkJiHgFeDcBa987654"
    raw_sr = (
        "source_id,drive_file_id,current_parent_id,source_title,source_type,season,authority_tier,evidence_status,usage_policy,discovered_at_utc,sha256\n"
        f"drive:{fake_raw_id},{fake_raw_id},{fake_parent},2025-2026国内注册名单.pdf,league_notice,2025-2026,T1,EXTRACTED,canonical_allowed,2026-10-01T00:00:00Z,{'a'*64}\n"
    ).encode("utf-8-sig")

    redacted_csv = cs.export_consumer_source_registry_csv(raw_sr).decode("utf-8")
    assert fake_raw_id not in redacted_csv
    assert fake_parent not in redacted_csv
    assert "drive_file_id" not in redacted_csv.splitlines()[0]
    rows = list(csv.DictReader(io.StringIO(redacted_csv)))
    assert len(rows) == 1
    assert rows[0]["source_id"].startswith("src:")
    assert rows[0]["source_title"] == "2025-2026国内注册名单.pdf"

    raw_doc_idx = json.dumps({
        "schema_version": 1,
        "documents": [
            {
                "doc_id": "doc_001",
                "drive_file_id": fake_raw_id,
                "current_parent_id": fake_parent,
                "title": "02_叙事采访_测试.md",
                "season": "2025-2026",
                "doc_type": "interview",
                "sha256": "b" * 64,
            }
        ],
    }, ensure_ascii=False).encode("utf-8")
    redacted_idx = cs.export_consumer_document_index(raw_doc_idx).decode("utf-8")
    assert fake_raw_id not in redacted_idx
    assert fake_parent not in redacted_idx
    assert "doc_001" in redacted_idx
    assert "02_叙事采访_测试.md" in redacted_idx

    prep_v215 = _import_prepare_v215()
    cand = prep_v215._content_preserving_candidate(
        "derived/INDEX.md",
        f"- derived/MASTER.csv: {fake_raw_id}\n".encode("utf-8"),
        {"release_id": "v2.1.5-1", "engine_sha": "a" * 40, "active_release_id": "v2.1.4"},
        {"status_id": "status"},
        {"derived/MASTER.csv": fake_raw_id},
        tmp_path,
        master_row_count=3465,
        item_by_key={"derived/MASTER.csv": {"name": "MASTER.csv"}},
    ).decode("utf-8")
    assert fake_raw_id not in cand
    assert "MASTER.csv" in cand


# ---------------------------------------------------------------------------
# AC-215-P0-4: Oracle v0.1.23 inline link protocol & lexical decoupling
# ---------------------------------------------------------------------------


def test_ac215_p04_oracle_v8_inline_protocol_and_lexical_decoupling():
    assert oracle.SCORING_SPEC_VERSION == "oracle-scoring-v8"
    assert hasattr(oracle, "CONNECTOR_CITATION_PROTOCOLS")
    assert "INLINE_LINK_CHIP_V0123" in oracle.CONNECTOR_CITATION_PROTOCOLS
    assert hasattr(oracle, "extract_v0123_inline_locators")
    assert hasattr(oracle, "build_v0123_client_receipt")

    drive_prefix = "https://" + "drive.google.com/file/d/"
    docs_prefix = "https://" + "docs.google.com/document/d/"
    authorized = (
        "研究卡_林测试_907766_2025-2026_CURRENT.md",
        "00_README_先读这个",
        "03_DATA_SYNTH_PLAYER_2025-2026.json",
    )

    verbatim_inline = (
        f"场均得分 20.1（参见 [研究卡_林测试_907766_2025-2026_CURRENT.md]({drive_prefix}synth-card-01/view?usp=drivesdk) "
        f"与 [00_README_先读这个]({docs_prefix}synth-doc-01/edit)）。"
    )
    extracted = oracle.extract_v0123_inline_locators(
        verbatim_inline, authorized_locators=authorized
    )
    assert extracted == (
        "研究卡_林测试_907766_2025-2026_CURRENT.md",
        "00_README_先读这个",
    )

    # Plain prose mention WITHOUT Drive/Docs inline link must NOT extract as citation
    prose_only = "场均得分 20.1（来源文件：03_DATA_SYNTH_PLAYER_2025-2026.json，00_README_先读这个）。"
    assert oracle.extract_v0123_inline_locators(prose_only, authorized_locators=authorized) == ()

    q = oracle.OracleQuestion(
        question_id="Q215-RATE",
        question_text="2025-2026 PPG?",
        comparison="rate",
        expected=20.1,
        unit="per_game",
        precision=1,
        tolerance=0.0,
        mandatory_locator=True,
    )
    ans_ok = {
        "text": (
            f"常规赛场均得分 20.1，参考 [研究卡_林测试_907766_2025-2026_CURRENT.md]({drive_prefix}synth-907766/view) "
            f"和 [03_DATA_SYNTH_PLAYER_2025-2026.json]({drive_prefix}synth-03/view)，02-说明文档。"
        ),
        "value": 20.1,
        "unit": "per_game",
        "cited_locators": ["研究卡_林测试_907766_2025-2026_CURRENT.md"],
    }
    res_ok = oracle.score_answer(q, ans_ok, authorized_locators=authorized)
    assert res_ok.verdict == "PASS", res_ok

    # Contradictory prose number (with Chinese unit) MUST STILL FAIL consistency
    ans_bad = {
        "text": "场均得分 20.1（另有说法 99.9分），参考研究卡_林测试_907766_2025-2026_CURRENT.md。",
        "value": 20.1,
        "unit": "per_game",
        "cited_locators": ["研究卡_林测试_907766_2025-2026_CURRENT.md"],
    }
    res_bad = oracle.score_answer(q, ans_bad, authorized_locators=authorized)
    assert res_bad.verdict == "FAIL" and res_bad.dimension == "consistency"


# ---------------------------------------------------------------------------
# AC-215-T0: README milestone table & #169 boundary clarification
# ---------------------------------------------------------------------------


def test_ac215_t0_readme_milestones_and_issue169_calibration():
    en = (ROOT / "README.md").read_text(encoding="utf-8")
    zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

    assert "v2.0.2" in en and "2.0.2" in zh
    assert "v2.1.4-1" in en and "v2.1.4-1" in zh
    assert "v2.1.4" in en and "v2.1.4" in zh
    # #169 clarification reflects closed per-principal ACL & trusted caller identity boundary
    assert "#169" in en and ("closed" in en.lower() or "per-principal" in en.lower())
    assert "#169" in zh and ("关闭" in zh or "逐" in zh or "principal" in zh)
