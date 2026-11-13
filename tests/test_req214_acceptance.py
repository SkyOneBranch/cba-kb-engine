"""REQ-214 (v2.1.4) Frozen Q1 Acceptance Tests.

Must collect cleanly and FAIL deterministically on baseline SHA
be172d49d2b23a7be4a0c858315480b3e101cc9e (differential falsification), and PASS
unchanged after Commit 2 implementation.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import tomllib

import pytest
import yaml

from cba_kb import common
from cba_kb import document_lane
from cba_kb import drive_resilience
from cba_kb import drive_search_index as si
from cba_kb import evidence_ledger
from cba_kb import ingestion
from cba_kb import kc_current
from cba_kb import kc_projection
from cba_kb import kc_slices
from cba_kb.current_state import security
from cba_kb.evidence_ledger import EvidenceLedgerError, RuntimeEvidenceLedger
from cba_kb.identity_web_evidence import fetch_official_resource
from cba_kb.kc_cleanroom import A, B, SEASON, build_cleanroom_fixture
from cba_kb.kc_current import KcCurrentError, SimulatedCurrentStore
from cba_kb.kc_slices import KcSliceError, build_slice, build_slice_catalog
from cba_kb.kc_snapshot import build_snapshot

ROOT = Path(__file__).resolve().parents[1]


def _grant(file_id: str, principal: str = "alice") -> dict:
    return {
        "approved": True,
        "approver": "auditor",
        "file_id": file_id,
        "scope": "drive:read",
        "granted_at": "2026-11-12T00:00:00Z",
        "note": "",
        "allowed_principals": [principal],
    }


def test_ac214_p01_pyproject_and_makefile_pythonpath_parity():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == "2.0.2"
    py_paths = pyproject["tool"]["pytest"]["ini_options"]["pythonpath"]
    assert "." in py_paths
    assert "src" in py_paths
    assert "legacy" in py_paths

    makefile_lines = (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    py_export = next(
        line for line in makefile_lines if line.startswith("export PYTHONPATH :=")
    )
    make_paths = py_export.split(":=", 1)[1].strip().split(":")
    assert "." in make_paths
    assert "src" in make_paths
    assert "legacy" in make_paths

    clean_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/automation/test_handoff.py", "-q"],
        cwd=ROOT,
        env=clean_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_ac214_p02a_common_atomic_fsyncs_parent_directory(tmp_path, monkeypatch):
    real_fsync = os.fsync
    fsynced_inodes: list[int] = []

    def spy_fsync(fd: int) -> None:
        fsynced_inodes.append(os.fstat(fd).st_ino)
        try:
            real_fsync(fd)
        except OSError:
            pass

    monkeypatch.setattr(os, "fsync", spy_fsync)
    target = tmp_path / "nested" / "artifact.json"
    common.atomic(target, b'{"ok":true}\n')

    assert target.read_bytes() == b'{"ok":true}\n'
    assert os.stat(target).st_ino in fsynced_inodes
    assert os.stat(target.parent).st_ino in fsynced_inodes

    fsynced_inodes.clear()
    wal_file = tmp_path / "nested" / "ledger.jsonl.wal"
    drive_resilience._wal_write(wal_file, {"state": "IN_PROGRESS", "plan_id": "p1"})
    assert json.loads(wal_file.read_text(encoding="utf-8"))["plan_id"] == "p1"
    assert os.stat(wal_file.parent).st_ino in fsynced_inodes


def test_ac214_p02b_build_slice_catalog_validates_records_once(monkeypatch):
    fx = build_cleanroom_fixture()
    records = list(fx["stats_records"])
    expected_keys = [
        (SEASON, "REGULAR", "WHOLE_SEASON"),
        (SEASON, "PLAYOFFS", "WHOLE_SEASON"),
        ("2019-20", "REGULAR", "WHOLE_SEASON"),
        ("2020-21", "REGULAR", "WHOLE_SEASON"),
        ("2021-22", "REGULAR", "WHOLE_SEASON"),
    ]
    reference_slices = []
    reference_missing = []
    for key in expected_keys:
        season, match_type, scope = key[0], key[1], key[2]
        team = key[3] if len(key) > 3 else None
        sl = build_slice(
            records,
            player_uid=A,
            season=season,
            match_type=match_type,
            scope=scope,
            team_id=team,
            provider="CBA_OFFICIAL",
            competition="CBA",
            authorized_ids={A, B},
        )
        reference_slices.append(dict(sl, slice_key=(season, match_type, scope, team)))
        if sl["status"] == "EMPTY":
            reference_missing.append((season, match_type, scope, team))

    canonical_record_count = sum(
        1
        for r in records
        if isinstance(r, dict)
        and r.get("provider") == "CBA_OFFICIAL"
        and r.get("competition") == "CBA"
    )
    assert canonical_record_count >= 2

    real_validate = kc_slices.validate_stats_record
    calls = {"count": 0}

    def counting_validate(record):
        calls["count"] += 1
        return real_validate(record)

    monkeypatch.setattr(kc_slices, "validate_stats_record", counting_validate)
    catalog = build_slice_catalog(
        records,
        player_uid=A,
        expected_keys=expected_keys,
        provider="CBA_OFFICIAL",
        competition="CBA",
        authorized_ids={A, B},
    )

    assert catalog == {
        "slices": reference_slices,
        "missing": reference_missing,
        "complete": not reference_missing,
        "end_marker": not reference_missing,
    }
    assert calls["count"] == canonical_record_count

    with pytest.raises(KcSliceError, match="PLAYER_NOT_AUTHORIZED"):
        build_slice_catalog(
            records,
            player_uid="pid_unauthorized",
            expected_keys=expected_keys,
            provider="CBA_OFFICIAL",
            competition="CBA",
            authorized_ids={A, B},
        )

    with pytest.raises(KcSliceError, match="INVALID_SCOPE"):
        build_slice_catalog(
            records,
            player_uid=A,
            expected_keys=[(SEASON, "REGULAR", "CAREER")],
            provider="CBA_OFFICIAL",
            competition="CBA",
            authorized_ids={A, B},
        )


def test_ac214_p11a_common_canonical_json_consolidation():
    assert hasattr(common, "canonical_json_str")
    assert hasattr(common, "canonical_json_bytes")
    assert hasattr(common, "sha256_hex")

    sample = {"b": [1, "易建联"], "a": {"z": True, "m": None}}
    compact_no_nl = common.canonical_json_bytes(sample, trailing_newline=False)
    compact_with_nl = common.canonical_json_bytes(sample, trailing_newline=True)

    assert compact_no_nl == b'{"a":{"m":null,"z":true},"b":[1,"\xe6\x98\x93\xe5\xbb\xba\xe8\x81\x94"]}'
    assert compact_with_nl == compact_no_nl + b"\n"
    assert common.canonical_json_str(sample).encode("utf-8") == compact_no_nl

    assert evidence_ledger.canonical_bytes(sample) == compact_no_nl
    assert kc_projection.canonical_json(sample) == compact_no_nl
    assert ingestion.canonical_bytes(sample) == compact_with_nl
    assert document_lane.canonical_bytes(sample) == compact_with_nl
    assert common.sha256_hex(compact_no_nl) == kc_projection.sha256_hex(compact_no_nl)
    assert common.sha256_hex(compact_no_nl) == common.digest(compact_no_nl)


def test_ac214_p11a_kc_current_validate_avoids_deepcopy_and_blocks_tampering(monkeypatch):
    snap_obj = build_snapshot(
        entries=[
            {
                "entry_id": "entry-stats",
                "sha256": "b" * 64,
                "authority": "canonical",
                "rights": "public",
                "source_id": "fixture_stats",
            }
        ],
        target_domain="fixture_target",
        capabilities=["stats"],
        build_provenance="simulated-build-214",
        build_time="2026-11-12T00:00:00+00:00",
        data_release="SIMULATED_DATA_RELEASE_214",
        engine_candidate="SIMULATED_ENGINE_CANDIDATE_214",
        build_code="SIMULATED_BUILD_CODE_214",
    )
    snap_dict = dataclasses.asdict(snap_obj)
    snap_dict["capabilities"] = list(snap_obj.capabilities)

    store = SimulatedCurrentStore(target_domain="fixture_target")
    store.register(snap_dict)
    store.stage(snap_dict["snapshot_id"])

    real_deepcopy = copy.deepcopy
    deepcopy_calls = {"count": 0}

    def counting_deepcopy(val, *args, **kwargs):
        deepcopy_calls["count"] += 1
        return real_deepcopy(val, *args, **kwargs)

    monkeypatch.setattr(kc_current.copy, "deepcopy", counting_deepcopy)
    res = store.validate()
    assert res["valid"] is True
    assert deepcopy_calls["count"] == 0

    snap_dict["build_provenance"] = "tampered-after-validate"
    with pytest.raises(KcCurrentError, match="COMMIT_VALIDATION_STALE"):
        store.commit()

    snap_dict["build_provenance"] = "simulated-build-214"
    store.stage(snap_dict["snapshot_id"])
    deepcopy_calls["count"] = 0
    store.validate()
    assert deepcopy_calls["count"] == 0
    receipt = store.commit()
    assert receipt["snapshot_id"] == snap_dict["snapshot_id"]


def test_ac214_p11b_fetch_official_resource_isolates_rate_limit_state():
    now = {"t": 100.0}
    slept: list[float] = []

    def fake_clock() -> float:
        return now["t"]

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now["t"] += seconds

    def ok_transport(url, *, method, timeout, headers, max_bytes):
        return {
            "status": 200,
            "content_type": "text/html",
            "body": b"<html>ok</html>",
            "location": None,
            "redirects": [],
        }

    fetch_official_resource(
        "https://www.cbaleague.com/page-1",
        source_tier="A1",
        per_domain_interval=5.0,
        transport=ok_transport,
        clock=fake_clock,
        sleep=fake_sleep,
        max_retries=0,
    )
    now["t"] = 101.0
    slept.clear()
    fetch_official_resource(
        "https://www.cbaleague.com/page-2",
        source_tier="A1",
        per_domain_interval=5.0,
        transport=ok_transport,
        clock=fake_clock,
        sleep=fake_sleep,
        max_retries=0,
    )
    assert slept == []

    shared_state: dict[str, float] = {}
    now["t"] = 200.0
    slept.clear()
    fetch_official_resource(
        "https://www.cbaleague.com/page-1",
        source_tier="A1",
        per_domain_interval=5.0,
        transport=ok_transport,
        clock=fake_clock,
        sleep=fake_sleep,
        max_retries=0,
        rate_limit_state=shared_state,
    )
    now["t"] = 201.5
    fetch_official_resource(
        "https://www.cbaleague.com/page-2",
        source_tier="A1",
        per_domain_interval=5.0,
        transport=ok_transport,
        clock=fake_clock,
        sleep=fake_sleep,
        max_retries=0,
        rate_limit_state=shared_state,
    )
    assert len(slept) == 1
    assert slept[0] == pytest.approx(3.5)


def test_ac214_p11b_runtime_evidence_ledger_append_scans_only_new_entry(
    tmp_path, monkeypatch
):
    ledger = RuntimeEvidenceLedger(tmp_path / "runtime-ledger", "REQ-214")
    for idx in range(4):
        ledger.append(
            "STEP_COMPLETED",
            {"step": idx},
            recorded_at=f"2026-11-12T00:0{idx}:00Z",
        )

    scanner_mod = security()
    real_scan_bytes = scanner_mod.scan_bytes
    scan_calls = {"count": 0}

    def counting_scan_bytes(data: bytes, label: str):
        scan_calls["count"] += 1
        return real_scan_bytes(data, label)

    monkeypatch.setattr(scanner_mod, "scan_bytes", counting_scan_bytes)
    entry5 = ledger.append(
        "STEP_COMPLETED",
        {"step": 4},
        recorded_at="2026-11-12T00:04:00Z",
    )
    assert entry5["sequence"] == 4
    assert scan_calls["count"] == 1

    scan_calls["count"] = 0
    verified = ledger.verify()
    assert verified["status"] == "PASS"
    assert verified["entries"] == 5
    assert scan_calls["count"] == 5

    first_file = sorted(ledger.root.glob("00000000-*.json"))[0]
    tampered = json.loads(first_file.read_text(encoding="utf-8"))
    tampered["payload"] = {"step": 999}
    first_file.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(EvidenceLedgerError, match="EVIDENCE_LEDGER_HASH_MISMATCH"):
        ledger.append(
            "STEP_COMPLETED",
            {"step": 5},
            recorded_at="2026-11-12T00:05:00Z",
        )


def test_ac214_p12a_search_index_o_query_lookup():
    reg = {
        "f1": {
            "name": "Liaoning Roster",
            "rights_status": "approved",
            "rights_grant": _grant("f1"),
        },
        "f2": {
            "name": "Guangdong Roster",
            "rights_status": "approved",
            "rights_grant": _grant("f2"),
        },
    }
    texts = {
        "f1": "liaoning roster " + " ".join(f"vocab_{i}" for i in range(150)),
        "f2": "guangdong roster",
    }
    idx = si.build_index(reg, texts)

    class GuardedTermsDict(dict):
        def items(self):
            raise AssertionError("FULL_VOCAB_ITEMS_SCAN_FORBIDDEN")

        def __iter__(self):
            raise AssertionError("FULL_VOCAB_ITER_SCAN_FORBIDDEN")

    guarded_index = dict(idx, terms=GuardedTermsDict(idx["terms"]))
    search_out = si.search_index(
        guarded_index, "liaoning roster", principal="alice", registry=reg
    )
    assert search_out["results"] == [
        {
            "file_id": "f1",
            "title": "Liaoning Roster",
            "matched_terms": ["liaoning", "roster"],
        },
        {
            "file_id": "f2",
            "title": "Guangdong Roster",
            "matched_terms": ["roster"],
        },
    ]


def test_ac214_p12a_revoke_grant_persistent_serializes_concurrent_writers(
    tmp_path, monkeypatch
):
    reg = {
        "f1": {
            "name": "Liaoning Roster",
            "rights_status": "approved",
            "rights_grant": _grant("f1"),
        },
        "f2": {
            "name": "Guangdong Roster",
            "rights_status": "approved",
            "rights_grant": _grant("f2"),
        },
    }
    idx = si.build_index(reg, {"f1": "liaoning roster", "f2": "guangdong roster"})
    index_path = tmp_path / "search_index.json"
    si.save_index(index_path, idx)
    barrier = threading.Barrier(2, timeout=0.15)
    real_load_index = si.load_index

    def synchronized_load_index(path):
        loaded = real_load_index(path)
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return loaded

    monkeypatch.setattr(si, "load_index", synchronized_load_index)
    errors: list[BaseException] = []

    def worker(fid: str) -> None:
        try:
            assert si.revoke_grant_persistent(index_path, fid, "alice") is True
        except BaseException as exc:
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=("f1",))
    t2 = threading.Thread(target=worker, args=("f2",))
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)
    assert not errors

    persisted = real_load_index(index_path)
    assert persisted["docs"]["f1"]["visible"] is False
    assert persisted["docs"]["f2"]["visible"] is False


def test_ac214_p12b_file_lock_non_truncating_open(tmp_path):
    target = tmp_path / "state.json"
    lock_file = Path(str(target) + ".lock")
    lock_file.write_bytes(b"keep-existing-lock-bytes\n")

    with drive_resilience.file_lock(target, timeout_s=1.0):
        assert lock_file.read_bytes() == b"keep-existing-lock-bytes\n"
    assert lock_file.read_bytes() == b"keep-existing-lock-bytes\n"


def test_ac214_p12b_offline_tests_workflow_pr_deduplication():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/offline-tests.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["tests"]["steps"]
    offline_step = next(
        s for s in steps if s.get("name") == "Offline regression suite"
    )
    parity_step = next(
        s for s in steps if s.get("name") == "Full pytest suite (evidence parity)"
    )

    assert offline_step.get("if") == "github.event_name != 'pull_request'"
    assert parity_step.get("if") == "github.event_name == 'pull_request'"
    assert "CBA_KB_SOURCE_POLICY_JSON" in (parity_step.get("env") or {})
    assert "legacy" in parity_step.get("run", "")
    assert "pytest.log" in parity_step.get("run", "")
