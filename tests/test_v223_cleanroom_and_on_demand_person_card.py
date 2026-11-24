"""Frozen red falsification & acceptance tests for REQ-223 (cba-kb-engine v2.2.3).

Covers P0-1..P0-5 of North Star v3.1 On-Demand Snowball Step 1:
- P0-1: Clean-room removal of residual historical Drive IDs + externalized curation_profile.yaml.
- P0-2: Generic PLAYER_GAME_STATS schema & deterministic recent-N-game window aggregator.
- P0-3: On-demand person card assembler (RESOLVED / UNDECIDED / NOT_FOUND_IN_LOCAL_KB + zero-stats fallback).
- P0-4: Lossless kc_render single-game section + fast local-only `cba-kb build-person-card` CLI.
- P0-5: Backward-compatible ops-sync sidecar ingestion (game_logs_dir, coverage_ledger, negative_receipts).

All fixtures are 100% synthetic. Zero network calls, zero Drive API calls,
zero real Drive IDs, zero real game stats, zero local private paths.
"""
from __future__ import annotations

import csv
import importlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest
from openpyxl import Workbook

from cba_kb import consumer_surface, kc_render, master, operational_sync, stats
from cba_kb.instance import Instance

REPO_ROOT = Path(__file__).resolve().parents[1]

# Drive-ID morphology matching scripts/check_secrets.py pattern set v1
_DRIVE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])1[A-Za-z0-9_-]{24,}(?![A-Za-z0-9_-])")
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_DIGITS_RE = re.compile(r"^\d+$")

# Constructed dynamically so this test file itself contains zero literal historical IDs
_LEGACY_MASTER_PREFIX = "1Nb4" + "-4rr"


def _find_drive_id_tokens(text: str) -> List[str]:
    hits: List[str] = []
    for match in _DRIVE_TOKEN_RE.finditer(text):
        tok = match.group(0)
        if _HEX40_RE.match(tok) or _HEX64_RE.match(tok) or _DIGITS_RE.match(tok):
            continue
        hits.append(tok)
    return hits


def _make_synthetic_instance(tmp_path: Path, *, runtime_json: Dict[str, Any] | None = None) -> Instance:
    root = tmp_path / "synth_instance"
    config_root = root / "config"
    creds = root / ".credentials"
    real_fx = root / "fixtures" / "real"
    data_root = root / "data"
    for d in (config_root, creds, real_fx, data_root):
        d.mkdir(parents=True, exist_ok=True)
    if runtime_json is not None:
        (config_root / "runtime.json").write_text(
            json.dumps(runtime_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return Instance(
        root=root,
        config_root=config_root,
        credentials_store=creds,
        real_fixture_root=real_fx,
        data_root=data_root,
    )


def _write_synthetic_master_xlsx(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "MASTER"
    ws.append(list(master.HEADERS))
    row = {h: None for h in master.HEADERS}
    row.update({
        "record_key": "2026-2027|SYNTH_CLUB_A|合成球员甲",
        "season": "2026-2027",
        "club_id": "SYNTH_CLUB_A",
        "club_official": "合成俱乐部甲",
        "sequence": 1,
        "player": "合成球员甲",
        "registration_stage": "常规注册",
        "registration_method": "自行培养",
        "contract_category": "B",
        "registration_status": "已注册",
        "source_url": "https://example.org/synth/master/2026-2027",
        "verification_level": "auto_validated",
    })
    ws.append([row[h] for h in master.HEADERS])
    wb.save(path)


def _make_game_stat_record(
    *,
    game_id: str,
    game_date: str,
    player_uid: str = "player:synth-001",
    provider_player_id: str = "990001",
    player_name: str = "合成球员甲",
    team_id: str = "SYNTH_CLUB_A",
    team_name: str = "合成俱乐部甲",
    opponent_team_id: str = "SYNTH_CLUB_B",
    opponent_team_name: str = "合成俱乐部乙",
    points: int | None = 18,
    rebounds: int | None = 5,
    assists: int | None = 4,
    steals: int | None = 2,
    blocks: int | None = None,
    minutes_played: float | None = 30.0,
    fg_made: int | None = 7,
    fg_att: int | None = 14,
    tp_made: int | None = 2,
    tp_att: int | None = 5,
    ft_made: int | None = 2,
    ft_att: int | None = 2,
    dnp_reason: str | None = None,
    did_not_play: bool = False,
    record_id: str | None = None,
    supersedes_record_id: str | None = None,
    revision_reason: str | None = None,
    raw_sha256: str | None = None,
    captured_at: str = "2026-11-24T06:00:00Z",
    doc_id: str | None = None,
) -> Dict[str, Any]:
    rec_id = record_id or f"game_stats:CBA_OFFICIAL:2026-2027:1:{game_id}:{player_uid}"
    rec: Dict[str, Any] = {
        "schema": "cba-kb.player-game-stats.v1",
        "schema_version": 1,
        "semantic_grain": "PLAYER_GAME_STATS",
        "record_id": rec_id,
        "player_uid": player_uid,
        "provider_player_id": provider_player_id,
        "player_name": player_name,
        "provider": "CBA_OFFICIAL",
        "competition": "CBA",
        "season": "2026-2027",
        "match_type": "1",
        "game_id": game_id,
        "game_date": game_date,
        "round_label": f"R{game_id[-2:]}",
        "team_id": team_id,
        "team_name": team_name,
        "opponent_team_id": opponent_team_id,
        "opponent_team_name": opponent_team_name,
        "dnp_reason": dnp_reason,
        "did_not_play": did_not_play,
        "metrics": {
            "minutes_played": minutes_played,
            "points": points,
            "rebounds": rebounds,
            "assists": assists,
            "steals": steals,
            "blocks": blocks,
            "field_goals_made": fg_made,
            "field_goals_attempted": fg_att,
            "three_point_made": tp_made,
            "three_point_attempted": tp_att,
            "free_throws_made": ft_made,
            "free_throws_attempted": ft_att,
        },
        "provenance": {
            "canonical_url": f"https://example.org/cba/game/{game_id}",
            "raw_sha256": raw_sha256 or ("a" * 64),
            "captured_at": captured_at,
            "doc_id": doc_id or f"doc_game_{game_id}",
        },
        "rights": {
            "classification": "UNKNOWN_FOR_REDISTRIBUTION",
            "public_export_allowed": False,
        },
    }
    if supersedes_record_id is not None:
        rec["supersedes_record_id"] = supersedes_record_id
    if revision_reason is not None:
        rec["revision_reason"] = revision_reason
    return rec


# ---------------------------------------------------------------------------
# P0-1: Clean-Room ID Removal & Externalized Curation Profile
# ---------------------------------------------------------------------------

def test_p0_1_cleanroom_zero_residual_real_drive_ids_and_master_resolution(tmp_path: Path):
    # 1. Zero raw Drive ID morphology in master.py and accept_v1_5_1.py
    for rel in ("src/cba_kb/master.py", "scripts/accept_v1_5_1.py"):
        content = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert _find_drive_id_tokens(content) == [], f"Residual Drive ID token in {rel}"
        assert _LEGACY_MASTER_PREFIX not in content, f"Residual legacy MASTER ID prefix in {rel}"

    # 2. Zero residual legacy MASTER ID prefix in the 3 docs files
    for rel in (
        "DATA_LICENSE.md",
        "docs/history/V1_5_1_THIRD_PARTY_REVIEW.md",
        "docs/requirements/V1_5_1_THIRD_PARTY_REVIEW.md",
    ):
        content = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert _LEGACY_MASTER_PREFIX not in content, f"Residual legacy MASTER ID prefix in {rel}"

    # 3. master.resolve_master_file_id resolution order & Fail-Closed behavior
    assert hasattr(master, "UNBOUND_LOCAL_MASTER_ID")
    assert master.UNBOUND_LOCAL_MASTER_ID == "UNBOUND_LOCAL_MASTER"
    assert hasattr(master, "resolve_master_file_id")

    inst = _make_synthetic_instance(
        tmp_path,
        runtime_json={"inputs": {"MASTER.xlsx": {"id": "synth_instance_master_id"}}},
    )

    assert master.resolve_master_file_id(environ={}) == "UNBOUND_LOCAL_MASTER"
    with pytest.raises(ValueError, match="MASTER_FILE_ID_REQUIRED"):
        master.resolve_master_file_id(environ={}, require_bound=True)

    assert master.resolve_master_file_id(instance=inst, environ={}) == "synth_instance_master_id"
    assert (
        master.resolve_master_file_id(
            instance=inst,
            environ={"CBA_KB_MASTER_FILE_ID": "synth_env_master_id"},
        )
        == "synth_env_master_id"
    )
    assert (
        master.resolve_master_file_id(
            "synth_explicit_master_id",
            instance=inst,
            environ={"CBA_KB_MASTER_FILE_ID": "synth_env_master_id"},
        )
        == "synth_explicit_master_id"
    )

    # 4. master.inspect & master.build in local unbound mode emit UNBOUND_LOCAL_MASTER and no fake drive URL
    xlsx_path = tmp_path / "MASTER.xlsx"
    _write_synthetic_master_xlsx(xlsx_path)
    rows, summary = master.inspect(xlsx_path, environ={})
    assert len(rows) == 1
    assert summary["master_file_id"] == "UNBOUND_LOCAL_MASTER"

    out_dir = tmp_path / "build_out"
    master.build(xlsx_path, out_dir, "v2.2.3-test", "a" * 40, "2026-11-24T00:00:00Z", environ={})
    index_text = (out_dir / "INDEX.md").read_text(encoding="utf-8")
    assert "UNBOUND_LOCAL_MASTER" in index_text
    assert "drive.google.com/file/d/" not in index_text


def test_p0_1_externalized_curation_profile_zero_src_hardcoding_and_fail_closed(tmp_path: Path):
    # 1. Zero hardcoded Chinese business filter terms under src/cba_kb/**/*.py
    forbidden_terms = (
        "每日预言家",
        "每日名场面",
        "图文征集",
        "我和我的篮球",
        "杭州亚运会",
        "亚运会总结",
        "亚运会",
        "唁电",
    )
    for py_file in sorted((REPO_ROOT / "src" / "cba_kb").rglob("*.py")):
        src_text = py_file.read_text(encoding="utf-8")
        for term in forbidden_terms:
            assert term not in src_text, f"Hardcoded curation keyword {term!r} found in {py_file.name}"

    # 2. Declarative YAML profiles exist
    default_profile_path = REPO_ROOT / "config" / "curation_profile.yaml"
    example_profile_path = REPO_ROOT / "config" / "curation_profile.example.yaml"
    assert default_profile_path.is_file(), "config/curation_profile.yaml missing"
    assert example_profile_path.is_file(), "config/curation_profile.example.yaml missing"

    # 3. load_curation_profile API & fail-closed validation
    assert hasattr(consumer_surface, "load_curation_profile")
    default_profile = consumer_surface.load_curation_profile()
    assert default_profile["schema_version"] == 1
    assert isinstance(default_profile["noise_keywords"], list)
    assert default_profile["compact_announcement_excerpt_chars"] == 140

    example_profile = consumer_surface.load_curation_profile(example_profile_path)
    assert example_profile["schema_version"] == 1

    with pytest.raises(ValueError, match="CURATION_PROFILE_MISSING"):
        consumer_surface.load_curation_profile(tmp_path / "does_not_exist.yaml")

    bad_yaml = tmp_path / "invalid_profile.yaml"
    bad_yaml.write_text("schema_version: 1\nnoise_keywords: not_a_list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="CURATION_PROFILE_INVALID"):
        consumer_surface.load_curation_profile(bad_yaml)

    # 4. Private instance override takes precedence over repo default
    inst = _make_synthetic_instance(tmp_path)
    custom_profile_path = inst.config_root / "curation_profile.yaml"
    custom_profile_path.write_text(
        "\n".join([
            "schema_version: 1",
            "noise_event_classes: ['SYNTH_NOISE_CLASS']",
            "noise_keywords: ['SYNTH_CUSTOM_NOISE_TOKEN']",
            "historical_pinned_event_classes: ['SYNTH_HIST']",
            "historical_pinned_keywords: ['SYNTH_OLD_TOURNEY']",
            "pinned_marker_keywords: ['SYNTH_PINNED']",
            "protected_event_classes: ['注册与转会']",
            "active_competitive_keywords: ['签约']",
            "condolence_keywords: ['SYNTH_CONDOLENCE']",
            "condolence_exempt_keywords: ['签约']",
            "compact_announcement_excerpt_chars: 140",
        ]) + "\n",
        encoding="utf-8",
    )
    loaded_inst = consumer_surface.load_curation_profile(inst)
    assert loaded_inst["noise_keywords"] == ["SYNTH_CUSTOM_NOISE_TOKEN"]

    bundles = [
        {
            "doc_id": "doc_custom_noise_01",
            "title": "SYNTH_CUSTOM_NOISE_TOKEN promotion",
            "event_class": "官方公告",
            "published_at": "2026-11-20",
            "body": "SYNTH_CUSTOM_NOISE_TOKEN giveaway",
        },
        {
            "doc_id": "doc_custom_keep_02",
            "title": "俱乐部完成合成外援签约",
            "event_class": "注册与转会",
            "published_at": "2026-11-20",
            "body": "俱乐部完成合成外援签约公告",
        },
    ]
    res = consumer_surface.filter_curated_operational_announcements(bundles, instance=inst)
    assert {b["doc_id"] for b in res["filtered"]} == {"doc_custom_noise_01"}
    assert {b["doc_id"] for b in res["admitted"]} == {"doc_custom_keep_02"}


# ---------------------------------------------------------------------------
# P0-2: Generic Player-Game Stats Contract & Recent-N-Game Aggregator
# ---------------------------------------------------------------------------

def test_p0_2_game_stats_validation_unknown_not_zero_and_stats_py_untouched():
    # Frozen stats.py constants must remain untouched (Contract D)
    assert stats.SEMANTIC_GRAIN == "PLAYER_SEASON_STATS"
    assert stats.SCOPES == frozenset({"WHOLE_SEASON", "TEAM_SPLIT"})
    assert "game_id" not in stats.ALLOWED_TOP_LEVEL_FIELDS

    game_stats = importlib.import_module("cba_kb.game_stats")
    assert game_stats.GAME_STATS_SCHEMA == "cba-kb.player-game-stats.v1"
    assert game_stats.SEMANTIC_GRAIN == "PLAYER_GAME_STATS"

    valid = _make_game_stat_record(game_id="SYNTH_G001", game_date="2026-11-10", steals=None, blocks=None)
    validated = game_stats.validate_player_game_stat_record(valid)
    # Unknown != 0: None must stay None
    assert validated["metrics"]["steals"] is None
    assert validated["metrics"]["blocks"] is None

    # Invalid raw_sha256 fails closed
    bad_sha = _make_game_stat_record(game_id="SYNTH_G002", game_date="2026-11-11", raw_sha256="not-a-sha256")
    with pytest.raises(game_stats.GameStatsValidationError):
        game_stats.validate_player_game_stat_record(bad_sha)

    # Empty canonical_url fails closed
    bad_url = _make_game_stat_record(game_id="SYNTH_G003", game_date="2026-11-12")
    bad_url["provenance"]["canonical_url"] = "   "
    with pytest.raises(game_stats.GameStatsValidationError):
        game_stats.validate_player_game_stat_record(bad_url)

    # Negative non-signed metric fails closed
    bad_neg = _make_game_stat_record(game_id="SYNTH_G004", game_date="2026-11-13", points=-3)
    with pytest.raises(game_stats.GameStatsValidationError):
        game_stats.validate_player_game_stat_record(bad_neg)

    # Boolean metric fails closed
    bad_bool = _make_game_stat_record(game_id="SYNTH_G005", game_date="2026-11-14")
    bad_bool["metrics"]["points"] = True
    with pytest.raises(game_stats.GameStatsValidationError):
        game_stats.validate_player_game_stat_record(bad_bool)


def test_p0_2_recent_games_aggregator_dnp_exclusion_and_loud_revision():
    game_stats = importlib.import_module("cba_kb.game_stats")

    g1_orig = _make_game_stat_record(
        game_id="SYNTH_G01",
        game_date="2026-11-01",
        points=10,
        rebounds=4,
        assists=2,
        steals=1,
        blocks=None,
        minutes_played=25.0,
        fg_made=4,
        fg_att=10,
        tp_made=1,
        tp_att=4,
        ft_made=1,
        ft_att=2,
        record_id="game_stats:CBA_OFFICIAL:2026-2027:1:SYNTH_G01:player:synth-001",
        captured_at="2026-11-02T01:00:00Z",
    )
    # Official post-game revision superseding g1_orig (points revised 10 -> 12, fg_made 4 -> 5)
    g1_rev = _make_game_stat_record(
        game_id="SYNTH_G01",
        game_date="2026-11-01",
        points=12,
        rebounds=4,
        assists=2,
        steals=1,
        blocks=None,
        minutes_played=25.0,
        fg_made=5,
        fg_att=10,
        tp_made=1,
        tp_att=4,
        ft_made=1,
        ft_att=2,
        record_id="game_stats:CBA_OFFICIAL:2026-2027:1:SYNTH_G01:player:synth-001:rev2",
        supersedes_record_id=g1_orig["record_id"],
        revision_reason="OFFICIAL_BOXSCORE_CORRECTION",
        captured_at="2026-11-03T02:00:00Z",
    )
    g2 = _make_game_stat_record(
        game_id="SYNTH_G02",
        game_date="2026-11-05",
        points=20,
        rebounds=6,
        assists=4,
        steals=2,
        blocks=None,
        minutes_played=32.0,
        fg_made=8,
        fg_att=16,
        tp_made=2,
        tp_att=6,
        ft_made=2,
        ft_att=2,
        captured_at="2026-11-06T01:00:00Z",
    )
    # DNP game must NOT pollute per-game average denominators
    g3_dnp = _make_game_stat_record(
        game_id="SYNTH_G03",
        game_date="2026-11-08",
        points=None,
        rebounds=None,
        assists=None,
        steals=None,
        blocks=None,
        minutes_played=0.0,
        fg_made=None,
        fg_att=None,
        tp_made=None,
        tp_att=None,
        ft_made=None,
        ft_att=None,
        dnp_reason="INJURY_REST",
        did_not_play=True,
        captured_at="2026-11-09T01:00:00Z",
    )
    g4 = _make_game_stat_record(
        game_id="SYNTH_G04",
        game_date="2026-11-12",
        points=16,
        rebounds=5,
        assists=6,
        steals=0,
        blocks=None,
        minutes_played=33.0,
        fg_made=6,
        fg_att=14,
        tp_made=2,
        tp_att=5,
        ft_made=2,
        ft_att=4,
        captured_at="2026-11-13T05:00:00Z",
    )

    summary = game_stats.summarize_recent_games([g1_orig, g1_rev, g2, g3_dnp, g4], window=5)
    assert summary["semantic_grain"] == "PLAYER_GAME_STATS"
    assert summary["played_games_in_window"] == 3
    assert summary["total_played_games"] == 3
    assert summary["dnp_games_count"] == 1
    # Average points across (12, 20, 16) = 16.0 (NOT 10, and NOT divided by 4)
    assert summary["averages"]["points"] == pytest.approx(16.0)
    assert summary["averages"]["rebounds"] == pytest.approx(5.0)
    assert summary["averages"]["assists"] == pytest.approx(4.0)
    assert summary["averages"]["steals"] == pytest.approx(1.0)
    # All blocks are None -> Unknown != 0 -> average must stay None
    assert summary["averages"]["blocks"] is None

    # Shooting totals: FG 19/40 = 0.475, 3P 5/15 = 0.3333, FT 5/8 = 0.625
    st = summary["shooting_totals"]
    assert st["fg_made"] == 19 and st["fg_attempted"] == 40
    assert st["fg_pct"] == pytest.approx(0.475, abs=1e-4)
    assert st["tp_made"] == 5 and st["tp_attempted"] == 15
    assert st["tp_pct"] == pytest.approx(5 / 15, abs=1e-4)
    assert st["ft_made"] == 5 and st["ft_attempted"] == 8
    assert st["ft_pct"] == pytest.approx(0.625, abs=1e-4)

    assert summary["latest_game_date"] == "2026-11-12"
    assert summary["latest_captured_at"] == "2026-11-13T05:00:00Z"

    # LOUD_REVISION must be emitted, never silent overwrite
    revs = summary["revision_summaries"]
    assert len(revs) == 1
    assert revs[0]["disclosure_posture"] == "LOUD_REVISION"
    assert revs[0]["prior_record_id"] == g1_orig["record_id"]
    assert revs[0]["new_record_id"] == g1_rev["record_id"]


# ---------------------------------------------------------------------------
# P0-3: On-Demand Person Card Assembler
# ---------------------------------------------------------------------------

def _synthetic_roster_fixtures():
    master_rows = [
        {
            "record_key": "2026-2027|SYNTH_CLUB_A|合成球员甲",
            "season": "2026-2027",
            "club_id": "SYNTH_CLUB_A",
            "club_official": "合成俱乐部甲",
            "player": "合成球员甲",
            "registration_stage": "常规注册",
            "contract_category": "B",
            "registration_status": "已注册",
        },
        {
            "record_key": "2026-2027|SYNTH_CLUB_A|合成重名球员",
            "season": "2026-2027",
            "club_id": "SYNTH_CLUB_A",
            "club_official": "合成俱乐部甲",
            "player": "合成重名球员",
            "registration_stage": "常规注册",
            "contract_category": "A1",
            "registration_status": "已注册",
        },
        {
            "record_key": "2026-2027|SYNTH_CLUB_B|合成重名球员",
            "season": "2026-2027",
            "club_id": "SYNTH_CLUB_B",
            "club_official": "合成俱乐部乙",
            "player": "合成重名球员",
            "registration_stage": "常规注册",
            "contract_category": "A2",
            "registration_status": "已注册",
        },
    ]
    foreign_rows = [
        {
            "club": "合成俱乐部乙",
            "player_en": "SYNTH ROOKIE FOREIGN",
            "player_zh": "合成新外援丙",
            "nationality": "SYNTH_NATION",
            "position": "PG",
            "jersey_number": "11",
            "published_at": "2026-11-21",
        }
    ]
    identity_registry = {
        "schema_version": 1,
        "players": [
            {
                "player_uid": "player:synth-001",
                "canonical_name": "合成球员甲",
                "provider_player_id": "990001",
                "club": "合成俱乐部甲",
            },
            {
                "player_uid": "player:synth-dup-a",
                "canonical_name": "合成重名球员",
                "provider_player_id": "990101",
                "club": "合成俱乐部甲",
            },
            {
                "player_uid": "player:synth-dup-b",
                "canonical_name": "合成重名球员",
                "provider_player_id": "990102",
                "club": "合成俱乐部乙",
            },
            {
                "player_uid": "player:synth-foreign-001",
                "canonical_name": "合成新外援丙",
                "provider_player_id": "990201",
                "club": "合成俱乐部乙",
            },
        ],
        "aliases": [
            {"alias": "SYNTH ROOKIE FOREIGN", "player_uid": "player:synth-foreign-001"},
        ],
    }
    negative_receipts = [
        {
            "schema": "cba-kb.negative-receipt.v1",
            "query_person": "不存在的合成人物",
            "checked_sources": ["cba_official_register", "club_weibo"],
            "checked_at_utc": "2026-11-24T05:00:00Z",
            "ttl_hours": 24,
            "result": "NO_NEW_WHITELIST_EVIDENCE",
        }
    ]
    return master_rows, foreign_rows, identity_registry, negative_receipts


def test_p0_3_person_query_resolution_resolved_undecided_and_not_found():
    builder = importlib.import_module("cba_kb.person_card_builder")
    master_rows, foreign_rows, identity_registry, negative_receipts = _synthetic_roster_fixtures()
    now_utc = datetime(2026, 11, 24, 6, 30, 0, tzinfo=timezone.utc)

    # 1. Unique domestic player
    r_dom = builder.resolve_person_query(
        "合成球员甲",
        master_rows=master_rows,
        foreign_rows=foreign_rows,
        identity_registry=identity_registry,
    )
    assert r_dom["status"] == "RESOLVED"
    assert r_dom["person"]["player_uid"] == "player:synth-001"
    assert r_dom["person"]["current_club"] == "合成俱乐部甲"

    # 2. Unique foreign player by English alias
    r_for = builder.resolve_person_query(
        "synth rookie foreign",
        master_rows=master_rows,
        foreign_rows=foreign_rows,
        identity_registry=identity_registry,
    )
    assert r_for["status"] == "RESOLVED"
    assert r_for["person"]["player_uid"] == "player:synth-foreign-001"
    assert r_for["person"]["jersey_number"] == "11"

    # 3. Homonym Chinese name -> Fail-Closed UNDECIDED with candidates
    r_dup = builder.resolve_person_query(
        "合成重名球员",
        master_rows=master_rows,
        foreign_rows=foreign_rows,
        identity_registry=identity_registry,
    )
    assert r_dup["status"] == "UNDECIDED"
    assert r_dup["person"] is None
    assert len(r_dup["candidates"]) == 2
    assert {c["player_uid"] for c in r_dup["candidates"]} == {
        "player:synth-dup-a",
        "player:synth-dup-b",
    }

    # 4. Not found -> NOT_FOUND_IN_LOCAL_KB with active Negative-Receipt hit
    r_miss = builder.resolve_person_query(
        "不存在的合成人物",
        master_rows=master_rows,
        foreign_rows=foreign_rows,
        identity_registry=identity_registry,
        negative_receipts=negative_receipts,
        data_cutoff="2026-11-24T06:00:00Z",
        now_utc=now_utc,
    )
    assert r_miss["status"] == "NOT_FOUND_IN_LOCAL_KB"
    assert r_miss["person"] is None
    assert r_miss["local_data_cutoff"] == "2026-11-24T06:00:00Z"
    assert r_miss["negative_receipt_hit"] is True


def test_p0_3_build_person_card_full_assembly_and_zero_stats_rookie_fallback(tmp_path: Path):
    builder = importlib.import_module("cba_kb.person_card_builder")
    master_rows, foreign_rows, identity_registry, negative_receipts = _synthetic_roster_fixtures()

    season_stats_records = [
        {
            "record_id": "stats:CBA_OFFICIAL:2026-2027:1:WHOLE_SEASON:990001",
            "semantic_grain": "PLAYER_SEASON_STATS",
            "schema_version": 1,
            "player_uid": "player:synth-001",
            "provider": "CBA_OFFICIAL",
            "provider_player_id": "990001",
            "player_name": "合成球员甲",
            "season": "2026-2027",
            "competition": "CBA",
            "match_type": "1",
            "scope": "WHOLE_SEASON",
            "team_id": "SYNTH_CLUB_A",
            "team_name": "合成俱乐部甲",
            "metrics": {"games_played": 10, "points_per_game": 15.4},
            "provenance": {"captured_at": "2026-11-24T05:00:00Z", "locator": "synth_stats_001"},
        }
    ]
    game_stats_records = [
        _make_game_stat_record(game_id="SYNTH_G10", game_date="2026-11-20", points=22, rebounds=6, assists=5),
        _make_game_stat_record(game_id="SYNTH_G09", game_date="2026-11-17", points=14, rebounds=4, assists=3),
    ]
    doc_bundles = [
        {
            "doc_id": "doc_synth_announce_01",
            "title": "合成俱乐部乙正式签约合成新外援丙",
            "source_tier": "T1.5",
            "completeness": "full",
            "published_at": "2026-11-21",
            "canonical_url": "https://example.org/synth/signing-foreign",
            "matched_persons": ["合成新外援丙"],
        }
    ]

    # 1. Full assembly for player with season + recent game stats
    out_dir = tmp_path / "cards_out"
    res_full = builder.build_person_card(
        "合成球员甲",
        season="2026-2027",
        recent_games_window=5,
        master_rows=master_rows,
        foreign_rows=foreign_rows,
        identity_registry=identity_registry,
        season_stats_records=season_stats_records,
        game_stats_records=game_stats_records,
        document_bundles=doc_bundles,
        output_dir=out_dir,
    )
    assert res_full["status"] == "BUILT"
    assert res_full["drive_api_called"] is False
    assert res_full["network_called"] is False
    card_path = Path(res_full["card_path"])
    assert card_path.is_file()
    assert card_path.name.startswith("研究卡_合成球员甲_")
    assert card_path.name.endswith("_2026-2027_CURRENT.md")
    card_text = card_path.read_text(encoding="utf-8")
    assert "## Recent Single-Game Log (Player-Game Grain)" in card_text
    assert "SYNTH_G10" in card_text or "2026-11-20" in card_text
    assert kc_render.check_completeness(card_text, kind="markdown") is True

    # 2. Newly signed foreign player with ZERO season stats -> graceful UNKNOWN card (no crash!)
    res_rookie = builder.build_person_card(
        "合成新外援丙",
        season="2026-2027",
        master_rows=master_rows,
        foreign_rows=foreign_rows,
        identity_registry=identity_registry,
        season_stats_records=season_stats_records,
        game_stats_records=game_stats_records,
        document_bundles=doc_bundles,
        output_dir=out_dir,
    )
    assert res_rookie["status"] == "BUILT"
    assert res_rookie["posture"] == "UNKNOWN"
    assert res_rookie["unknown_subkind"] == "NO_SEASON_STATS_RECORDED_YET"
    rookie_text = Path(res_rookie["card_path"]).read_text(encoding="utf-8")
    assert "Posture: UNKNOWN" in rookie_text
    assert "NO_SEASON_STATS_RECORDED_YET" in rookie_text
    assert "合成新外援丙" in rookie_text
    assert "doc_synth_announce_01" in rookie_text
    assert kc_render.check_completeness(rookie_text, kind="markdown") is True

    # 3. UNDECIDED and NOT_FOUND_IN_LOCAL_KB must NOT fabricate card files
    empty_dir = tmp_path / "no_stub_cards"
    r_undecided = builder.build_person_card(
        "合成重名球员",
        master_rows=master_rows,
        identity_registry=identity_registry,
        output_dir=empty_dir,
    )
    assert r_undecided["status"] == "UNDECIDED"
    assert r_undecided["card_path"] is None
    assert not empty_dir.exists() or not any(empty_dir.iterdir())

    r_missing = builder.build_person_card(
        "不存在的合成人物",
        master_rows=master_rows,
        identity_registry=identity_registry,
        negative_receipts=negative_receipts,
        output_dir=empty_dir,
    )
    assert r_missing["status"] == "NOT_FOUND_IN_LOCAL_KB"
    assert r_missing["card_path"] is None
    assert not empty_dir.exists() or not any(empty_dir.iterdir())


# ---------------------------------------------------------------------------
# P0-4: kc_render Single-Game Section & `cba-kb build-person-card` CLI
# ---------------------------------------------------------------------------

def test_p0_4_render_research_card_backward_compat_and_single_game_section():
    base_payload: Dict[str, Any] = {
        "metric": "points_per_game",
        "grains": {
            "STATS": {
                "posture": "CLEAN_OFFICIAL",
                "season": "2026-2027",
                "match_type": "1",
                "scope": "WHOLE_SEASON",
                "metric": "points_per_game",
                "value": 15.4,
            }
        },
        "evidence_transparency": {
            "posture": "CLEAN_OFFICIAL",
            "adopted_value": 15.4,
            "scope_meta": {
                "player_name": "合成球员甲",
                "player_id": "990001",
                "season": "2026-2027",
                "match_type": "1",
                "scope": "WHOLE_SEASON",
                "games_played": 10,
                "captured_at": "2026-11-24T05:00:00Z",
                "locator": "synth_loc_001",
            },
        },
    }
    legacy_card = kc_render.render_research_card(base_payload, evidence_verified=True)
    assert "## Recent Single-Game Log (Player-Game Grain)" not in legacy_card
    assert kc_render.check_completeness(legacy_card, kind="markdown") is True

    # With recent_games_summary (and a synthetic Drive URL to verify scrubbing)
    game_stats = importlib.import_module("cba_kb.game_stats")
    g_orig = _make_game_stat_record(
        game_id="SYNTH_G21",
        game_date="2026-11-22",
        points=19,
        record_id="game_stats:CBA_OFFICIAL:2026-2027:1:SYNTH_G21:player:synth-001",
    )
    g_rev = _make_game_stat_record(
        game_id="SYNTH_G21",
        game_date="2026-11-22",
        points=21,
        record_id="game_stats:CBA_OFFICIAL:2026-2027:1:SYNTH_G21:player:synth-001:rev2",
        supersedes_record_id=g_orig["record_id"],
        revision_reason="POST_GAME_RECOUNT",
        raw_sha256="b" * 64,
        doc_id="doc_synth_g21",
    )
    recent_summary = game_stats.summarize_recent_games([g_orig, g_rev], window=5)
    extended_payload = dict(base_payload)
    extended_payload["recent_games_summary"] = recent_summary
    extended_payload["grains"] = dict(base_payload["grains"])
    extended_payload["grains"]["DOCUMENT"] = {
        "source_id": "synth_src_01",
        "locator": "line:1",
        "verbatim": "Reference link https://drive.google.com/file/d/SYNTH_FAKE_TOKEN_XYZ/view",
    }

    extended_card = kc_render.render_research_card(extended_payload, evidence_verified=True)
    assert "## Recent Single-Game Log (Player-Game Grain)" in extended_card
    assert "2026-11-22" in extended_card
    assert "21" in extended_card
    assert "LOUD_REVISION" in extended_card
    assert "doc_synth_g21" in extended_card or ("b" * 12) in extended_card
    assert "drive.google.com" not in extended_card
    assert kc_render.check_completeness(extended_card, kind="markdown") is True


def test_p0_4_cli_build_person_card_local_fast_and_offline(tmp_path: Path):
    builder = importlib.import_module("cba_kb.person_card_builder")
    master_rows, foreign_rows, identity_registry, _ = _synthetic_roster_fixtures()

    # Write synthetic local inputs
    master_csv = tmp_path / "MASTER.csv"
    with master_csv.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(master_rows[0].keys()))
        writer.writeheader()
        writer.writerows(master_rows)

    foreign_csv = tmp_path / "foreign.csv"
    with foreign_csv.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(foreign_rows[0].keys()))
        writer.writeheader()
        writer.writerows(foreign_rows)

    id_reg_path = tmp_path / "identity_registry.json"
    id_reg_path.write_text(json.dumps(identity_registry, ensure_ascii=False), encoding="utf-8")

    game_logs_dir = tmp_path / "game_logs"
    game_logs_dir.mkdir()
    (game_logs_dir / "synth_games.jsonl").write_text(
        json.dumps(
            _make_game_stat_record(game_id="SYNTH_G30", game_date="2026-11-23", points=24),
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )

    out_dir = tmp_path / "cli_cards"

    # Timing check on in-process builder (< 0.5s SLA)
    t0 = time.perf_counter()
    in_proc = builder.build_person_card(
        "合成球员甲",
        season="2026-2027",
        recent_games_window=5,
        master_rows=master_rows,
        foreign_rows=foreign_rows,
        identity_registry=identity_registry,
        game_stats_records=[_make_game_stat_record(game_id="SYNTH_G30", game_date="2026-11-23", points=24)],
        output_dir=out_dir,
        register_for_sync=True,
    )
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.5, f"build_person_card exceeded 0.5s SLA: {elapsed:.3f}s"
    assert in_proc["status"] == "BUILT"
    assert (out_dir / "on_demand_cards_manifest.json").is_file()

    # CLI subprocess invocation
    cli_out_dir = tmp_path / "cli_subproc_cards"
    cmd = [
        sys.executable,
        "-m",
        "cba_kb.cli",
        "build-person-card",
        "--person",
        "合成球员甲",
        "--season",
        "2026-2027",
        "--recent-games-window",
        "5",
        "--master-csv",
        str(master_csv),
        "--foreign-csv",
        str(foreign_csv),
        "--identity-registry",
        str(id_reg_path),
        "--game-logs-dir",
        str(game_logs_dir),
        "--output-dir",
        str(cli_out_dir),
        "--register-for-sync",
        "--format",
        "json",
    ]
    subproc_env = dict(os.environ)
    src_dir = str(REPO_ROOT / "src")
    existing_pp = subproc_env.get("PYTHONPATH", "")
    subproc_env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{existing_pp}" if existing_pp else src_dir
    proc = subprocess.run(
        cmd, cwd=str(REPO_ROOT), env=subproc_env, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"CLI failed: {proc.stderr}"
    payload = json.loads(proc.stdout)
    assert payload["status"] == "BUILT"
    assert payload["drive_api_called"] is False
    assert payload["network_called"] is False
    assert Path(payload["card_path"]).is_file()
    assert (cli_out_dir / "on_demand_cards_manifest.json").is_file()


# ---------------------------------------------------------------------------
# P0-5: `ops-sync` Cross-Repo Sidecar Ingestion
# ---------------------------------------------------------------------------

def test_p0_5_ops_sync_sidecars_game_logs_coverage_ledger_and_negative_receipts(tmp_path: Path):
    inst = _make_synthetic_instance(tmp_path)
    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir()
    out_dir = tmp_path / "ops_out"

    game_logs_dir = tmp_path / "game_logs"
    game_logs_dir.mkdir()
    log_file = game_logs_dir / "player_synth_001.jsonl"
    rec1 = _make_game_stat_record(
        game_id="SYNTH_G41",
        game_date="2026-11-20",
        player_uid="player:synth-001",
        points=17,
        record_id="game_stats:CBA_OFFICIAL:2026-2027:1:SYNTH_G41:player:synth-001",
    )
    log_file.write_text(json.dumps(rec1, ensure_ascii=False) + "\n", encoding="utf-8")

    coverage_ledger_path = tmp_path / "coverage_ledger.json"
    coverage_ledger_path.write_text(
        json.dumps(
            {
                "schema": "cba-kb.coverage-ledger.v1",
                "generated_at_utc": "2026-11-24T06:00:00Z",
                "sources": [
                    {"source_id": "cba_domestic_2026", "status": "PARTIAL", "missing": ["SYNTH_CLUB_Z"]},
                    {"source_id": "cba_foreign_2026", "status": "COMPLETE", "missing": []},
                    {"source_id": "club_weibo_feed", "status": "STALE", "missing": []},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    negative_receipts_path = tmp_path / "negative_receipts.jsonl"
    now_iso = datetime.now(timezone.utc).isoformat()
    negative_receipts_path.write_text(
        "\n".join([
            json.dumps({
                "schema": "cba-kb.negative-receipt.v1",
                "query_person": "不存在的合成人物A",
                "checked_sources": ["cba_official"],
                "checked_at_utc": now_iso,
                "ttl_hours": 24,
                "result": "NO_NEW_WHITELIST_EVIDENCE",
            }, ensure_ascii=False),
            json.dumps({
                "schema": "cba-kb.negative-receipt.v1",
                "query_person": "过期空回执合成人物B",
                "checked_sources": ["cba_official"],
                "checked_at_utc": "2020-01-01T00:00:00Z",
                "ttl_hours": 24,
                "result": "NO_NEW_WHITELIST_EVIDENCE",
            }, ensure_ascii=False),
        ]) + "\n",
        encoding="utf-8",
    )

    # First run: ingests game log, marks player dirty, summarizes coverage & negative receipts
    rep1 = operational_sync.run_operational_sync(
        engine_root=REPO_ROOT,
        instance=inst,
        bundles_dir=bundles_dir,
        output_dir=out_dir,
        game_logs_dir=game_logs_dir,
        coverage_ledger_path=coverage_ledger_path,
        negative_receipts_path=negative_receipts_path,
    )
    assert rep1["drive_api_called"] is False
    assert rep1["game_logs_ingested_count"] == 1
    assert rep1["game_logs_skipped_count"] == 0
    assert "player:synth-001" in rep1["dirty_player_uids"]
    assert rep1["coverage_ledger_summary"]["status_counts"]["PARTIAL"] == 1
    assert rep1["coverage_ledger_summary"]["status_counts"]["COMPLETE"] == 1
    assert rep1["coverage_ledger_summary"]["status_counts"]["STALE"] == 1
    assert rep1["negative_receipts_summary"]["total_count"] == 2
    assert rep1["negative_receipts_summary"]["active_count"] == 1

    checkpoint = json.loads((out_dir / "operational_checkpoint.json").read_text(encoding="utf-8"))
    assert "player_synth_001.jsonl" in checkpoint["consumed_game_logs"]

    # Second run (unchanged): strictly idempotent
    rep2 = operational_sync.run_operational_sync(
        engine_root=REPO_ROOT,
        instance=inst,
        bundles_dir=bundles_dir,
        output_dir=out_dir,
        game_logs_dir=game_logs_dir,
        coverage_ledger_path=coverage_ledger_path,
        negative_receipts_path=negative_receipts_path,
    )
    assert rep2["game_logs_ingested_count"] == 0
    assert rep2["game_logs_skipped_count"] == 1
    assert rep2["dirty_player_uids"] == []

    # Third run (append post-game revision): re-ingests and marks player dirty again with LOUD_REVISION
    rec1_rev = _make_game_stat_record(
        game_id="SYNTH_G41",
        game_date="2026-11-20",
        player_uid="player:synth-001",
        points=19,
        record_id="game_stats:CBA_OFFICIAL:2026-2027:1:SYNTH_G41:player:synth-001:rev2",
        supersedes_record_id=rec1["record_id"],
        revision_reason="OFFICIAL_CORRECTION",
    )
    log_file.write_text(
        json.dumps(rec1, ensure_ascii=False) + "\n" + json.dumps(rec1_rev, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    rep3 = operational_sync.run_operational_sync(
        engine_root=REPO_ROOT,
        instance=inst,
        bundles_dir=bundles_dir,
        output_dir=out_dir,
        game_logs_dir=game_logs_dir,
        coverage_ledger_path=coverage_ledger_path,
        negative_receipts_path=negative_receipts_path,
    )
    assert rep3["game_logs_ingested_count"] == 1
    assert "player:synth-001" in rep3["dirty_player_uids"]
    assert any(r.get("disclosure_posture") == "LOUD_REVISION" for r in rep3["revision_summaries"])
