"""End-to-end wiring tests for REQ-220 (v2.2.0 Operational KB, Issue #254).

Covers real call chains:
1. Real `python -m cba_kb.cli ops-sync` subprocess execution across Run 1
   (initial ingest + card build + checkpoint + freshness report with
   drive_api_called == False), Run 2 (idempotent 0-op), and Run 3 (same-URL
   content revision -> bidirectional revision chain + LOUD_REVISION +
   incremental recompute of ONLY the impacted player card while unimpacted
   player card SHA-256 stays identical).
2. Simulated mid-write crash during DocumentLane archive creation + retry
   recovery (verifying `.staging` cleanup and no DOCUMENT_ARCHIVE_PROVENANCE_INVALID).
3. Concurrent multi-thread registry writes across `drive_search.grant_rights`,
   `drive_discovery.run_discovery`, and `drive_poll.poll_once` (zero lost
   updates) AND full `drive_plan.create_plan` -> `approve_plan` ->
   `apply_plan(..., resilient=True)` on `purge_tombstones` (zero self-deadlock).
"""
from __future__ import annotations

import concurrent.futures
import copy
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from cba_kb import document_lane, drive_discovery, drive_plan, drive_poll, drive_search, kc_cleanroom
from cba_kb.document_lane import DocumentLane
from cba_kb.instance import Instance


REPO_ROOT = Path(__file__).resolve().parents[1]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_synthetic_instance(tmp_path: Path) -> Instance:
    inst_root = tmp_path / "synthetic_instance"
    config_root = inst_root / "config"
    config_root.mkdir(parents=True, exist_ok=True)
    data_root = inst_root / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    (inst_root / "inbox" / "documents").mkdir(parents=True, exist_ok=True)
    return Instance(
        root=inst_root.resolve(),
        config_root=config_root.resolve(),
        credentials_store=(inst_root / ".credentials").resolve(),
        real_fixture_root=(inst_root / "fixtures" / "real").resolve(),
        data_root=data_root.resolve(),
    )


def _write_bundle(
    path: Path,
    *,
    source_id: str,
    canonical_url: str,
    title: str,
    body: str,
    matched_persons: List[str],
    published_at: str = "2026-10-15",
    source_tier: str = "T1",
    completeness: str = "full",
) -> Path:
    item_id = _sha256_bytes(canonical_url.encode("utf-8"))
    raw_sha = _sha256_bytes(("raw::" + body).encode("utf-8"))
    ext_sha = _sha256_bytes(body.encode("utf-8"))
    persons_yaml = "[" + ", ".join(json.dumps(p, ensure_ascii=False) for p in matched_persons) + "]"
    frontmatter = "\n".join([
        "---",
        'bundle_schema: "acquisition_bundle_v1"',
        f'item_id: "{item_id}"',
        f'canonical_url: "{canonical_url}"',
        f'final_url: "{canonical_url}"',
        "related_urls: []",
        f'upstream_origin: "{source_id}:origin"',
        f'source_id: "{source_id}"',
        f'source_tier: "{source_tier}"',
        'event_class: "roster_update"',
        f"matched_persons: {persons_yaml}",
        f'published_at: "{published_at}"',
        'acquired_at: "2026-10-15T13:00:00Z"',
        f'raw_sha256: "{raw_sha}"',
        "raw_ref: null",
        f'extracted_sha256: "{ext_sha}"',
        'extractor: "synthetic-wiring-v1"',
        f'completeness: "{completeness}"',
        'rights: {"classification": "copyrighted", "public_export_allowed": false}',
        f'title: "{title}"',
        "---",
        "",
        body,
        "",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter, encoding="utf-8")
    return path


def _make_player_fixture(player_uid: str, name: str, ppg: float) -> Dict[str, Any]:
    fx = copy.deepcopy(kc_cleanroom.build_cleanroom_fixture())
    old_uid = fx["players"][0]["player_uid"]
    fx["players"][0]["player_uid"] = player_uid
    fx["players"][0]["canonical_name"] = name
    fx["players"] = [fx["players"][0]]
    for alias in fx.get("aliases", []):
        if alias.get("player_uid") == old_uid:
            alias["player_uid"] = player_uid
    for rec in fx["stats_records"]:
        rec["player_uid"] = player_uid
        rec["player_name"] = name
        rec["team_id"] = "29131"
        rec["team_name"] = None
        if rec.get("metric") == "points_per_game":
            rec["value"] = ppg
    return fx


def _run_cli_ops_sync(
    *,
    instance_root: Path,
    bundles_dir: Path,
    output_dir: Path,
    player_fixtures: Path,
    identity_registry: Path,
    approved_mentions: Path,
    published_status: Path,
) -> Dict[str, Any]:
    env = dict(os.environ)
    src_path = str(REPO_ROOT / "src")
    env["PYTHONPATH"] = src_path + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["CBA_KB_INSTANCE_ROOT"] = str(instance_root)
    cmd = [
        sys.executable,
        "-m",
        "cba_kb.cli",
        "ops-sync",
        "--bundles-dir",
        str(bundles_dir),
        "--output-dir",
        str(output_dir),
        "--player-fixtures",
        str(player_fixtures),
        "--identity-registry",
        str(identity_registry),
        "--approved-mentions",
        str(approved_mentions),
        "--published-status",
        str(published_status),
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"ops-sync failed (rc={proc.returncode}):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    return json.loads(proc.stdout)


def test_wiring_ops_sync_cli_three_runs_idempotent_and_loud_revision(tmp_path: Path) -> None:
    """Real subprocess `python -m cba_kb.cli ops-sync`:
    Run 1: initial ingest + cards + checkpoint + freshness report (drive_api_called=False)
    Run 2: idempotent (0 re-ingests, 0 recomputes)
    Run 3: same-URL content revision -> bidirectional revision chain + LOUD_REVISION
           + ONLY impacted player card recomputed while unimpacted player SHA-256 unchanged.
    """
    instance = _make_synthetic_instance(tmp_path)
    bundles_dir = tmp_path / "bundles"
    output_dir = tmp_path / "ops_out"

    p1_uid = "player_alpha_001"
    p2_uid = "player_beta_002"

    identity_registry_path = tmp_path / "identity_registry.json"
    identity_registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "players": [
                    {"player_uid": p1_uid, "canonical_name": "林锐锋"},
                    {"player_uid": p2_uid, "canonical_name": "周明远"},
                ],
                "aliases": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    player_fixtures_path = tmp_path / "player_fixtures.json"
    player_fixtures_path.write_text(
        json.dumps(
            {
                p1_uid: _make_player_fixture(p1_uid, "林锐锋", 18.4),
                p2_uid: _make_player_fixture(p2_uid, "周明远", 12.1),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    p1_body_v1 = "林锐锋在华南猛虎季前赛出战两场，表现稳定，预计首轮先发。"
    p2_body_v1 = "周明远在华北雄狮训练营完成全部体测项目，身体状态良好。"
    p1_body_v2 = "更正：林锐锋在华南猛虎季前赛因轻伤休战一场，首轮出战待定。"

    approved_mentions_path = tmp_path / "approved_mentions.json"
    approved_mentions_path.write_text(
        json.dumps(
            [
                {
                    "doc_id": document_lane.doc_id(p1_body_v1),
                    "player_uid": p1_uid,
                    "mention_status": "same",
                    "mention_method": "manual",
                },
                {
                    "doc_id": document_lane.doc_id(p2_body_v1),
                    "player_uid": p2_uid,
                    "mention_status": "same",
                    "mention_method": "manual",
                },
                {
                    "doc_id": document_lane.doc_id(p1_body_v2),
                    "player_uid": p1_uid,
                    "mention_status": "same",
                    "mention_method": "manual",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    published_status_path = tmp_path / "published_status.json"
    published_status_path.write_text(
        json.dumps({"published_cards": {}, "published_doc_ids": []}),
        encoding="utf-8",
    )

    # Initial bundles: one for player 1, one for player 2
    _write_bundle(
        bundles_dir / "p1_report.md",
        source_id="src_league_news",
        canonical_url="https://example.org/cba/news/p1-update",
        title="林锐锋赛季初状态报告",
        body=p1_body_v1,
        matched_persons=["林锐锋"],
    )
    _write_bundle(
        bundles_dir / "p2_report.md",
        source_id="src_league_news",
        canonical_url="https://example.org/cba/news/p2-update",
        title="周明远训练营简报",
        body=p2_body_v1,
        matched_persons=["周明远"],
    )

    # --- Run 1: initial ingest & card build ---
    r1 = _run_cli_ops_sync(
        instance_root=instance.root,
        bundles_dir=bundles_dir,
        output_dir=output_dir,
        player_fixtures=player_fixtures_path,
        identity_registry=identity_registry_path,
        approved_mentions=approved_mentions_path,
        published_status=published_status_path,
    )
    assert r1["drive_api_called"] is False
    assert r1["ingested_count"] == 2
    assert r1["skipped_bundles_count"] == 0
    assert sorted(r1["recomputed_player_uids"]) == sorted([p1_uid, p2_uid])

    checkpoint_path = output_dir / "operational_checkpoint.json"
    freshness_path = output_dir / "freshness_report.json"
    assert checkpoint_path.is_file()
    assert freshness_path.is_file()
    cp1 = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    old_doc_id = cp1["consumed_bundles"]["p1_report.md"]["doc_id"]

    p1_card_path = output_dir / "cards" / p1_uid / "research_card.md"
    p2_card_path = output_dir / "cards" / p2_uid / "research_card.md"
    assert p1_card_path.is_file()
    assert p2_card_path.is_file()
    p2_sha_run1 = _sha256_bytes(p2_card_path.read_bytes())

    # --- Run 2: idempotent (0 re-ingests, 0 recomputes) ---
    r2 = _run_cli_ops_sync(
        instance_root=instance.root,
        bundles_dir=bundles_dir,
        output_dir=output_dir,
        player_fixtures=player_fixtures_path,
        identity_registry=identity_registry_path,
        approved_mentions=approved_mentions_path,
        published_status=published_status_path,
    )
    assert r2["drive_api_called"] is False
    assert r2["ingested_count"] == 0
    assert r2["skipped_bundles_count"] == 2
    assert r2["recomputed_player_uids"] == []
    assert _sha256_bytes(p2_card_path.read_bytes()) == p2_sha_run1

    # --- Run 3: same-URL content revision for player 1 ONLY ---
    _write_bundle(
        bundles_dir / "p1_report.md",
        source_id="src_league_news",
        canonical_url="https://example.org/cba/news/p1-update",
        title="林锐锋赛季初状态报告（更正）",
        body="更正：林锐锋在华南猛虎季前赛因轻伤休战一场，首轮出战待定。",
        matched_persons=["林锐锋"],
    )

    r3 = _run_cli_ops_sync(
        instance_root=instance.root,
        bundles_dir=bundles_dir,
        output_dir=output_dir,
        player_fixtures=player_fixtures_path,
        identity_registry=identity_registry_path,
        approved_mentions=approved_mentions_path,
        published_status=published_status_path,
    )
    assert r3["drive_api_called"] is False
    assert r3["ingested_count"] == 1
    assert r3["skipped_bundles_count"] == 1
    assert r3["recomputed_player_uids"] == [p1_uid]
    # Unimpacted player card SHA-256 MUST remain byte-identical
    assert _sha256_bytes(p2_card_path.read_bytes()) == p2_sha_run1

    # LOUD_REVISION must be emitted in freshness_report.json
    rev_summaries = r3["revision_summaries"]
    assert len(rev_summaries) == 1
    assert rev_summaries[0]["disclosure_posture"] == "LOUD_REVISION"
    new_doc_id = rev_summaries[0]["new_doc_id"]
    assert rev_summaries[0]["prior_doc_id"] == old_doc_id
    assert new_doc_id != old_doc_id

    # Verify bidirectional revision chain in archive record.json files
    archive_root = instance.document_archive_root
    old_rec = json.loads((archive_root / old_doc_id / "record.json").read_text(encoding="utf-8"))
    new_rec = json.loads((archive_root / new_doc_id / "record.json").read_text(encoding="utf-8"))
    assert old_rec["superseded_by_doc_id"] == new_doc_id
    assert new_rec["supersedes_doc_id"] == old_doc_id


def test_wiring_document_lane_crash_staging_cleanup_and_retry_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulated mid-write crash during DocumentLane archive creation + retry recovery."""
    instance = _make_synthetic_instance(tmp_path)
    lane = DocumentLane(REPO_ROOT, instance)

    bundle_path = _write_bundle(
        tmp_path / "bundles" / "crash_test.md",
        source_id="src_crash_wire",
        canonical_url="https://example.org/cba/news/crash-recovery",
        title="原子落盘恢复测试",
        body="用于验证中途中断后无残留半成品档案且重试可干净恢复。",
        matched_persons=["林锐锋"],
    )

    real_replace = os.replace
    crash_fired = {"count": 0}

    def _flaky_replace(src: Any, dst: Any) -> None:
        if crash_fired["count"] == 0 and ".staging" in str(src):
            crash_fired["count"] += 1
            raise OSError("Simulated power loss during atomic archive publish")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _flaky_replace)

    with pytest.raises(OSError, match="Simulated power loss"):
        lane.ingest_acquisition_bundle(bundle_path)

    assert crash_fired["count"] == 1
    archive_root = instance.document_archive_root
    # No partial doc_* directory leaked into archive root
    leaked_docs = [p for p in archive_root.iterdir() if p.is_dir() and p.name.startswith("doc_")]
    assert leaked_docs == []
    # Staging directory cleaned up
    staging_dir = archive_root / ".staging"
    if staging_dir.exists():
        assert list(staging_dir.iterdir()) == []

    # Retry succeeds cleanly with valid provenance (no DOCUMENT_ARCHIVE_PROVENANCE_INVALID)
    res = lane.ingest_acquisition_bundle(bundle_path)
    doc_id = res["doc_id"]
    doc_dir = archive_root / doc_id
    assert (doc_dir / "record.json").is_file()
    assert (doc_dir / "provenance.json").is_file()
    record = json.loads((doc_dir / "record.json").read_text(encoding="utf-8"))
    prov = json.loads((doc_dir / "provenance.json").read_text(encoding="utf-8"))
    assert record["doc_id"] == doc_id
    assert record["capture_channel"] == "acquisition_bundle_v1"
    assert prov["captures"][0]["extracted_sha256"] == record["extracted_sha256"]


class _FakeDrive:
    def __init__(self, files: Dict[str, Dict[str, Any]]) -> None:
        self._files = dict(files)

    def list(self, folder_id: str) -> List[Dict[str, Any]]:
        return list(self._files.values())

    def meta(self, file_id: str) -> Dict[str, Any]:
        return dict(self._files[file_id])


class _FakeChangeSource:
    def __init__(self, start_token: str, changes: List[Dict[str, Any]], next_token: str) -> None:
        self._start = start_token
        self._changes = list(changes)
        self._next = next_token

    def get_start_token(self) -> str:
        return self._start

    def list_changes(self, since_token: str) -> tuple[List[Dict[str, Any]], str]:
        return list(self._changes), self._next


def test_wiring_concurrent_registry_writers_and_resilient_purge_no_deadlock(
    tmp_path: Path,
) -> None:
    """Concurrent multi-thread registry writes across grant_rights + run_discovery +
    poll_once must lose zero updates, and full create_plan -> approve_plan ->
    apply_plan(..., resilient=True) on purge_tombstones must complete with zero
    self-deadlock.
    """
    reg_path = tmp_path / "discovery_registry.json"
    ledger_path = tmp_path / "drive_ledger.jsonl"
    poll_state_path = tmp_path / "poll_state.json"

    old_ts = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    initial_registry: Dict[str, Any] = {
        "file_old_tomb": {
            "file_id": "file_old_tomb",
            "name": "stale_tombstone.pdf",
            "tombstoned": True,
            "tombstoned_ts": old_ts,
            "rights_status": "review",
        }
    }
    n_grant = 8
    for i in range(n_grant):
        fid = f"file_grant_{i:02d}"
        initial_registry[fid] = {
            "file_id": fid,
            "name": f"doc_{i:02d}.pdf",
            "rights_status": "review",
        }
    reg_path.write_text(json.dumps(initial_registry), encoding="utf-8")

    disc_files = {
        f"file_disc_{i:02d}": {
            "id": f"file_disc_{i:02d}",
            "name": f"discovered_{i:02d}.pdf",
            "mimeType": "application/pdf",
            "parents": ["folder_root"],
            "version": "1",
            "md5Checksum": f"{i:032x}",
            "size": "1024",
            "modifiedTime": "2026-10-10T00:00:00Z",
        }
        for i in range(4)
    }
    poll_files = {
        f"file_poll_{i:02d}": {
            "id": f"file_poll_{i:02d}",
            "name": f"polled_{i:02d}.pdf",
            "mimeType": "application/pdf",
            "parents": ["folder_root"],
            "version": "1",
            "md5Checksum": f"{100 + i:032x}",
            "size": "2048",
            "modifiedTime": "2026-10-11T00:00:00Z",
        }
        for i in range(4)
    }
    poll_changes = [
        {
            "change_id": f"chg_{i:02d}",
            "file_id": fid,
            "removed": False,
            "file": meta,
        }
        for i, (fid, meta) in enumerate(poll_files.items())
    ]

    # Initialize poll state token first (first poll_once records start token)
    poll_source = _FakeChangeSource("tok_0", poll_changes, "tok_1")
    poll_drive = _FakeDrive(poll_files)
    init_poll = drive_poll.poll_once(
        poll_source,
        poll_drive,
        registry_path=reg_path,
        ledger_path=ledger_path,
        state_path=poll_state_path,
    )
    assert init_poll["initialized"] is True

    # Run grant_rights + run_discovery + poll_once concurrently against the SAME registry_path
    disc_drive = _FakeDrive(disc_files)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futs = [
            pool.submit(
                drive_search.grant_rights,
                reg_path,
                ledger_path,
                f"file_grant_{i:02d}",
                approver="human-operator",
                note=f"wiring grant {i}",
            )
            for i in range(n_grant)
        ]
        futs.append(
            pool.submit(
                drive_discovery.run_discovery,
                disc_drive,
                "folder_root",
                ledger_path=ledger_path,
                registry_path=reg_path,
            )
        )
        futs.append(
            pool.submit(
                drive_poll.poll_once,
                poll_source,
                poll_drive,
                registry_path=reg_path,
                ledger_path=ledger_path,
                state_path=poll_state_path,
            )
        )
        for fut in futs:
            fut.result()

    merged_reg = json.loads(reg_path.read_text(encoding="utf-8"))
    for i in range(n_grant):
        fid = f"file_grant_{i:02d}"
        assert merged_reg[fid]["rights_status"] == "approved", f"Lost grant_rights for {fid}"
    for fid in disc_files:
        assert fid in merged_reg, f"Lost run_discovery entry {fid}"
    for fid in poll_files:
        assert fid in merged_reg, f"Lost poll_once entry {fid}"
    assert "file_old_tomb" in merged_reg

    # Full public drive_plan flow: create_plan -> approve_plan -> apply_plan(resilient=True)
    plan_path = tmp_path / "purge_plan.json"
    ops = [
        {
            "op": "purge_tombstones",
            "params": {
                "registry": str(reg_path),
                "older_than_days": 30,
            },
        }
    ]
    created = drive_plan.create_plan(ops, creator="muse")
    drive_plan.save_plan(plan_path, created)
    drive_plan.approve_plan(
        plan_path,
        "human-operator",
        ledger_path,
        actor="muse",
    )
    res = drive_plan.apply_plan(
        plan_path,
        ledger_path,
        ctx={},
        actor="muse",
        resilient=True,
        timeout_s=5.0,
    )
    assert res["plan_id"] == created["plan_id"]
    assert res["failed"] is None
    assert len(res["completed"]) == 1
    assert "file_old_tomb" in res["completed"][0]["result"]["purged"]
    post_reg = json.loads(reg_path.read_text(encoding="utf-8"))
    assert "file_old_tomb" not in post_reg
    # All concurrent writes survived the purge
    for i in range(n_grant):
        assert post_reg[f"file_grant_{i:02d}"]["rights_status"] == "approved"
