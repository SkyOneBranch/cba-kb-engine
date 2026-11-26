"""End-to-end wiring & integration tests for REQ-232 (cba-kb-engine v2.3.2).

100% offline synthetic fixtures (G5 == 0, G6 == 0).
Covers real call chains:
- CLI `build-person-card` & `export-person-cards` (12-metric stats, prior-season
  fallback, coach NOT_APPLICABLE domain horizon, dual visibility + leak guard)
- `SnowballOrchestrator` + `LocalCliAcquisitionBackend` card acquisition wiring
- `prepare_v215._build_instance_consumer_targets` under `v2.3.2-1` (historical
  `_ARCHIVE.md` migration + `assert_unique_current_card_per_person` enforcement)
- `archive_compactor.compact_drive_archive` with `"state": "COMPLETE"` release_status
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from cba_kb import cli
from cba_kb.archive_compactor import (
    DEFAULT_PROTECTED_BUNDLE_RELEASES,
    FOLDER_MIME,
    compact_drive_archive,
)
from cba_kb.instance import Instance
from cba_kb.kc_visibility import verify_public_safe_card_text
from cba_kb.orchestration.release import prepare_v215
from cba_kb.snowball_orchestrator import LocalCliAcquisitionBackend, SnowballOrchestrator


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: List[str],
) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["cba-kb", *argv])
    code = 0
    try:
        cli.main()
    except SystemExit as exc:
        code = int(exc.code) if isinstance(exc.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _synth_season_record(
    *,
    player_uid: str = "player:synth-w01",
    provider_player_id: str = "SYNTH_W01",
    player_name: str = "合成接线甲",
    season: str = "2026-2027",
    match_type: str = "REGULAR",
    scope: str = "WHOLE_SEASON",
    points: float = 17.8,
) -> Dict[str, Any]:
    record_id = f"stats:CBA_OFFICIAL:{season}:{match_type}:{scope}:{provider_player_id}"
    return {
        "record_id": record_id,
        "semantic_grain": "PLAYER_SEASON_STATS",
        "schema_version": 1,
        "player_uid": player_uid,
        "identity_status": "RESOLVED",
        "identity_resolution": {
            "status": "RESOLVED",
            "resolution_method": f"TRUSTED_EXTERNAL_ID_LINK:cba_player_id:{provider_player_id}",
            "candidate_player_uids": [player_uid],
        },
        "provider": "CBA_OFFICIAL",
        "provider_player_id": provider_player_id,
        "player_name": player_name,
        "season": season,
        "competition": "CBA",
        "match_type": match_type,
        "scope": scope,
        "team_id": "TEAM_W",
        "team_name": "合成接线俱乐部",
        "metrics": {
            "games_played": 32,
            "games_started": 28,
            "minutes_per_game": 29.5,
            "points_per_game": points,
            "rebounds_per_game": 5.1,
            "assists_per_game": 3.8,
            "steals_per_game": 1.1,
            "blocks_per_game": 0.4,
            "turnovers_per_game": 1.9,
            "field_goals_percentage": "47.2%",
            "three_point_percentage": "36.5%",
            "free_throws_percentage": "84.0%",
        },
        "provenance": {
            "source_uri": f"https://example.invalid/stats/{season}/{provider_player_id}",
            "endpoint": "/api/player-base-list",
            "request_contract": {"season": season, "match_type": match_type},
            "season": season,
            "match_type": match_type,
            "raw_response_sha256": _sha(f"raw:{record_id}"),
            "decoded_sha256": _sha(f"dec:{record_id}"),
            "captured_at": "2026-11-22T08:00:00Z",
            "locator": f"loc:{season}:{match_type}:{scope}",
        },
        "rights": {
            "classification": "PUBLIC",
            "public_export_allowed": True,
            "private_ai_consumption": "TARGET_SPECIFIC",
        },
    }


def test_cli_build_and_export_person_cards_end_to_end_wiring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    master_csv = tmp_path / "MASTER.csv"
    master_csv.write_text(
        "season,club_official,player,player_uid,player_id,contract_category,contract_term_official,registration_status\n"
        "2026-2027,合成接线俱乐部,合成接线甲,player:synth-w01,SYNTH_W01,C类,2028-07-31,已注册\n"
        "2026-2027,合成接线俱乐部,合成教练乙,player:synth-w02,SYNTH_W02,C类,2027-07-31,已注册\n",
        encoding="utf-8",
    )
    stats_json = tmp_path / "season_stats.json"
    stats_json.write_text(
        json.dumps([_synth_season_record(season="2025-2026", points=15.2)], ensure_ascii=False),
        encoding="utf-8",
    )

    # 1. Prior-season baseline + public_safe via build-person-card
    out_card = tmp_path / "card_out"
    code, out, err = _run_cli(
        monkeypatch,
        capsys,
        [
            "build-person-card",
            "--person",
            "合成接线甲",
            "--season",
            "2026-2027",
            "--master-csv",
            str(master_csv),
            "--season-stats",
            str(stats_json),
            "--output-dir",
            str(out_card),
            "--visibility",
            "public_safe",
        ],
    )
    assert code == 0, err
    res = json.loads(out)
    assert res["status"] == "BUILT"
    assert res["posture"] == "UNKNOWN"
    assert res["prior_season_baseline"]["season"] == "2025-2026"
    assert "### Prior Full-Season Baseline (2025-2026 — Historical Reference Only, Not 2026-2027)" in res["markdown"]
    verify_public_safe_card_text(res["markdown"])

    # 2. Coach entity-role -> statistics NOT_APPLICABLE
    code_coach, out_coach, err_coach = _run_cli(
        monkeypatch,
        capsys,
        [
            "build-person-card",
            "--person",
            "合成教练乙",
            "--season",
            "2026-2027",
            "--master-csv",
            str(master_csv),
            "--entity-role",
            "HEAD_COACH",
        ],
    )
    assert code_coach == 0, err_coach
    coach_res = json.loads(out_coach)
    assert coach_res["domain_horizons"]["registration"]["status"] == "VERIFIED_PRESENT"
    assert coach_res["domain_horizons"]["statistics"]["status"] == "NOT_APPLICABLE"

    # 3. Batch export-person-cards with public_safe leak verification on written files
    out_batch = tmp_path / "batch_export"
    code_b, out_b, err_b = _run_cli(
        monkeypatch,
        capsys,
        [
            "export-person-cards",
            "--players",
            "合成接线甲,合成教练乙",
            "--season",
            "2026-2027",
            "--master-csv",
            str(master_csv),
            "--season-stats",
            str(stats_json),
            "--output-dir",
            str(out_batch),
            "--visibility",
            "public_safe",
        ],
    )
    assert code_b == 0, err_b
    batch_res = json.loads(out_b)
    assert batch_res["exported_count"] == 2
    assert batch_res["visibility"] == "public_safe"
    for md_path in sorted(out_batch.glob("*.md")):
        assert md_path.name.endswith("_PUBLIC_CURRENT.md")
        verify_public_safe_card_text(md_path.read_text(encoding="utf-8"))


class _InMemoryStateStore:
    def __init__(self) -> None:
        self.cards: Dict[tuple[str, str], Dict[str, Any]] = {}
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.locks: Dict[tuple[str, str], Dict[str, Any]] = {}
        self.neg: List[Dict[str, Any]] = []

    def get_card(self, season: str, key: str) -> Any:
        return self.cards.get((season, key))

    def put_card(self, season: str, key: str, card: Dict[str, Any]) -> None:
        self.cards[(season, key)] = dict(card)

    def get_job(self, job_id: str) -> Any:
        return self.jobs.get(job_id)

    def put_job(self, job_id: str, job: Dict[str, Any]) -> None:
        self.jobs[job_id] = dict(job)

    def find_active_job_for_person(self, person: str, season: str) -> Any:
        return self.locks.get((season, person))

    def set_active_job_lock(self, person: str, season: str, lock_doc: Dict[str, Any]) -> None:
        self.locks[(season, person)] = dict(lock_doc)

    def clear_active_job_lock(self, person: str, season: str) -> None:
        self.locks.pop((season, person), None)

    def get_negative_receipts(self) -> List[Dict[str, Any]]:
        return list(self.neg)

    def append_negative_receipt(self, rec: Dict[str, Any]) -> None:
        self.neg.append(dict(rec))

    def get_coverage_ledger(self) -> Any:
        return None


def test_snowball_orchestrator_wiring_produces_v232_12_metric_and_domain_horizon() -> None:
    store = _InMemoryStateStore()
    master_rows = [
        {
            "season": "2026-2027",
            "club_official": "合成接线俱乐部",
            "player": "合成接线甲",
            "player_uid": "player:synth-w01",
            "player_id": "SYNTH_W01",
            "contract_category": "C类",
            "contract_term_official": "2028-07-31",
            "registration_status": "已注册",
        }
    ]
    backend = LocalCliAcquisitionBackend(
        local_resolver_inputs={
            "master_rows": master_rows,
            "season_stats_records": [_synth_season_record(season="2026-2027", points=20.4)],
        }
    )
    orch = SnowballOrchestrator(
        state_store=store,
        acquisition_backend=backend,
        local_resolver_inputs={"master_rows": master_rows},
        now_fn=lambda: datetime(2026, 11, 26, 10, 0, 0, tzinfo=timezone.utc),
    )
    res = orch.get_or_acquire_person("合成接线甲", season="2026-2027")
    assert res["status"] == "ACQUIRED_AND_BUILT"
    md = res["card_markdown"]
    assert "### Season Averages (Player-Season Grain)" in md
    assert "20.4" in md
    assert "## Observation Horizon" in md
    assert "domain[registration]: status=VERIFIED_PRESENT" in md
    assert "domain[statistics]: status=VERIFIED_PRESENT" in md


class _FakeDrive:
    def __init__(self, files: Dict[str, bytes], folders: Dict[str, List[Dict[str, Any]]] | None = None) -> None:
        self._files = dict(files)
        self._folders = dict(folders or {})
        self.created: List[Dict[str, Any]] = []
        self.trashed: List[str] = []

    def get(self, file_id: str) -> bytes:
        return self._files[file_id]

    def meta(self, file_id: str) -> Dict[str, Any]:
        raw = self._files[file_id]
        return {"id": file_id, "sha256Checksum": hashlib.sha256(raw).hexdigest()}

    def list(self, folder_id: str) -> List[Dict[str, Any]]:
        return list(self._folders.get(folder_id, []))

    def create_binary(self, parent_id: str, name: str, data: bytes, mime: str) -> Dict[str, Any]:
        fid = f"uploaded_{len(self.created) + 1}"
        self._files[fid] = data
        meta = {"id": fid, "name": name, "mimeType": mime, "sha256Checksum": hashlib.sha256(data).hexdigest()}
        self.created.append(meta)
        return meta

    def trash(self, file_id: str) -> None:
        self.trashed.append(file_id)


def test_prepare_v215_v232_1_consumer_targets_archive_migration_and_unique_guard(tmp_path: Path) -> None:
    inst = Instance(
        root=tmp_path,
        config_root=tmp_path / "config",
        credentials_store=tmp_path / ".credentials",
        real_fixture_root=tmp_path / "fixtures" / "real",
        data_root=tmp_path / "data",
    )
    inst.data_root.mkdir(parents=True, exist_ok=True)

    passage_text = "合成官方记录：合成归档甲 2025-2026 赛季场均 14.0 分。"
    passage_digest = _sha(passage_text)
    slice_spec = [
        {
            "player_uid": "player:synth-arch-1",
            "player_name": "合成归档甲",
            "player_id": "SYNTH_A01",
            "season": "2025-2026",
            "match_type": "REGULAR",
            "scope": "WHOLE_SEASON",
            "target_domain": "production",
            "fixture": {
                "players": [
                    {
                        "player_uid": "player:synth-arch-1",
                        "canonical_name": "合成归档甲",
                        "provider_player_id": "SYNTH_A01",
                    }
                ],
                "stats_records": [
                    _synth_season_record(
                        player_uid="player:synth-arch-1",
                        provider_player_id="SYNTH_A01",
                        player_name="合成归档甲",
                        season="2025-2026",
                        points=14.0,
                    )
                ],
                "passages": [
                    {
                        "source_id": "src-arch-1",
                        "player_uid": "player:synth-arch-1",
                        "title": "合成归档甲 2025-2026 摘录",
                        "text": passage_text,
                        "revision": "rev-1",
                        "locator": "loc:arch:p1",
                        "digest": passage_digest,
                    }
                ],
                "source_objects": [
                    {"object_id": "src-arch-1", "target_domain": "production", "rights": "APPROVED"}
                ],
                "fact": {
                    "fact_id": "fact-arch-1",
                    "fact_version": "v1",
                    "source_id": "src-arch-1",
                    "player_uid": "player:synth-arch-1",
                    "season": "2025-2026",
                },
                "claims": [],
            },
        }
    ]
    (inst.data_root / "promoted_research_slices.json").write_text(
        json.dumps(slice_spec, ensure_ascii=False), encoding="utf-8"
    )

    master_csv_bytes = (
        "season,club_official,player,contract_category\n"
        "2026-2027,合成接线俱乐部,合成归档甲,C类\n"
    ).encode("utf-8")
    sr_csv_bytes = "source_id,season,source_title,discovered_at_utc\nsrc-1,2026-2027,合成来源,2026-11-20T00:00:00Z\n".encode("utf-8")
    drive = _FakeDrive({"fid_master": master_csv_bytes, "fid_sr": sr_csv_bytes})
    manifest_rows = [
        {"drive_file_id": "fid_master", "logical_key": "derived/MASTER.csv"},
        {"drive_file_id": "fid_sr", "logical_key": "input/source_registry.csv"},
    ]

    targets = prepare_v215._build_instance_consumer_targets(
        drive,
        inst,
        release_id="v2.3.2-1",
        engine_sha="985c647a459ef7a7c970445a9a37db082eaecc71",
        manifest_rows=manifest_rows,
        data_parent="parent_data_01",
    )
    research_targets = {k: v for k, v in targets.items() if k.startswith("research/")}
    assert research_targets
    for spec in research_targets.values():
        assert spec["name"].endswith("_ARCHIVE.md")
        assert "[HISTORICAL_SEASON_ARCHIVE]" in spec["content"].decode("utf-8")

    # Add two duplicate CURRENT on-demand cards for the same person -> must raise DUPLICATE_CURRENT_PERSON_CARD
    od_dir = inst.data_root / "on_demand_cards"
    od_dir.mkdir(parents=True, exist_ok=True)
    card_bytes = b"# Card\n\nend_marker: true\n"
    card_sha = hashlib.sha256(card_bytes).hexdigest()
    f1 = "研究卡_合成归档甲_SYNTH_A01_2026-2027_CURRENT.md"
    f2 = "研究卡_合成归档甲_合成接线俱乐部_2026-2027_CURRENT.md"
    (od_dir / f1).write_bytes(card_bytes)
    (od_dir / f2).write_bytes(card_bytes)
    (od_dir / "on_demand_cards_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cards": {
                    "k1": {"filename": f1, "card_sha256": card_sha, "season": "2026-2027"},
                    "k2": {"filename": f2, "card_sha256": card_sha, "season": "2026-2027"},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="DUPLICATE_CURRENT_PERSON_CARD"):
        prepare_v215._build_instance_consumer_targets(
            drive,
            inst,
            release_id="v2.3.2-1",
            engine_sha="985c647a459ef7a7c970445a9a37db082eaecc71",
            manifest_rows=manifest_rows,
            data_parent="parent_data_01",
        )


def test_archive_compactor_live_state_complete_and_v232_protected() -> None:
    assert "v2.3.2-1" in DEFAULT_PROTECTED_BUNDLE_RELEASES
    rs_bytes = json.dumps(
        {"current_release_id": "v2.3.2-1", "state": "COMPLETE"},
        ensure_ascii=False,
    ).encode("utf-8")
    legacy_payload = b"synthetic legacy archive content"
    drive = _FakeDrive(
        files={"fid_rs": rs_bytes, "fid_legacy": legacy_payload},
        folders={
            "folder_hist": [
                {"id": "dir_v214", "name": "v2.1.4", "mimeType": FOLDER_MIME, "trashed": False},
                {"id": "dir_v232", "name": "v2.3.2-1", "mimeType": FOLDER_MIME, "trashed": False},
            ],
            "dir_v214": [
                {
                    "id": "fid_legacy",
                    "name": "legacy.txt",
                    "mimeType": "text/plain",
                    "sha256Checksum": hashlib.sha256(legacy_payload).hexdigest(),
                    "trashed": False,
                }
            ],
        },
    )
    res = compact_drive_archive(
        drive,
        history_folder_id="folder_hist",
        release_status_file_id="fid_rs",
        execute=True,
        tag="v232_wiring",
    )
    assert res["mode"] == "execute"
    assert res["skipped_protected_dir_count"] == 1
    assert len(res["uploaded_bundles"]) == 1
    assert res["trashed_ids"] == ["dir_v214"]
