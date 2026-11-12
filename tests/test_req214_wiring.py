"""REQ-214 (v2.1.4) Real Call-Chain Wiring & Integration Tests."""
from __future__ import annotations

from pathlib import Path
import pytest

from cba_kb import drive_resilience as dr
from cba_kb import drive_search_index as si
from cba_kb.evidence_ledger import (
    EvidenceLedgerError,
    append_runtime_evidence,
    verify_runtime_ledger,
)
from cba_kb.identity_web_evidence import fetch_official_resource
from cba_kb.kc_cleanroom import build_cleanroom_fixture
from cba_kb.kc_local_producer import build_local_projection, verify_local_projection


def test_req214_wiring_local_projection_end_to_end(tmp_path):
    fx = build_cleanroom_fixture()
    out_dir = tmp_path / "projection"
    summary = build_local_projection(fixture=fx, output_dir=str(out_dir))
    assert summary["snapshot_id"].startswith("kc-snapshot:")
    assert (out_dir / "snapshot.json").is_file()
    assert (out_dir / "research_card.md").is_file()

    verdict = verify_local_projection(str(out_dir))
    assert verdict["valid"] is True


def test_req214_wiring_wal_lifecycle_with_atomic_and_file_lock(tmp_path):
    ledger_path = tmp_path / "ops" / "plan_ledger.jsonl"
    wal_lock = Path(str(ledger_path) + ".wal.lock")
    wal_lock.parent.mkdir(parents=True, exist_ok=True)
    wal_lock.write_bytes(b"lock-marker\n")

    started = dr.wal_begin(
        ledger_path,
        "plan-214",
        [{"op": "rebuild_index"}, {"op": "revoke_index_grant"}],
    )
    assert started["status"] == "IN_PROGRESS"
    assert wal_lock.read_bytes() == b"lock-marker\n"
    assert dr.wal_check_incomplete(ledger_path) is not None

    dr.wal_mark_complete(ledger_path, 0)
    resumed = dr.wal_check_incomplete(ledger_path)
    assert resumed is not None and resumed["completed"] == [0]

    dr.wal_finish(ledger_path)
    assert dr.wal_check_incomplete(ledger_path) is None


def test_req214_wiring_search_build_save_query_revoke_roundtrip(tmp_path):
    grant_f1 = {
        "approved": True,
        "approver": "auditor",
        "file_id": "f1",
        "scope": "drive:read",
        "granted_at": "2026-11-12T00:00:00Z",
        "note": "",
        "allowed_principals": ["alice"],
    }
    grant_f2 = dict(grant_f1, file_id="f2")
    reg = {
        "f1": {"name": "Alpha", "rights_status": "approved", "rights_grant": grant_f1},
        "f2": {"name": "Beta", "rights_status": "approved", "rights_grant": grant_f2},
    }
    idx_path = tmp_path / "idx.json"
    si.save_index(
        idx_path,
        si.build_index(reg, {"f1": "season roster", "f2": "season archive"}),
    )

    before = si.search_index(
        si.load_index(idx_path), "season roster", principal="alice", registry=reg
    )
    assert [r["file_id"] for r in before["results"]] == ["f1", "f2"]
    assert before["results"][0]["matched_terms"] == ["roster", "season"]
    assert before["results"][1]["matched_terms"] == ["season"]

    assert si.revoke_grant_persistent(idx_path, "f1", "alice") is True
    assert si.revoke_grant_persistent(idx_path, "f1", "alice") is False

    after = si.search_index(
        si.load_index(idx_path), "season roster", principal="alice", registry=reg
    )
    assert [r["file_id"] for r in after["results"]] == ["f2"]


def test_req214_wiring_runtime_evidence_ledger_chain_and_secret_rejection(tmp_path):
    ledger_root = tmp_path / "evidence" / "REQ-214" / "runtime-ledger"
    e0 = append_runtime_evidence(
        ledger_root,
        "REQ-214",
        "INIT",
        {"ok": True},
        recorded_at="2026-11-12T01:00:00Z",
    )
    e1 = append_runtime_evidence(
        ledger_root,
        "REQ-214",
        "DONE",
        {"prev": e0["entry_sha256"]},
        recorded_at="2026-11-12T01:01:00Z",
    )
    assert e1["previous_entry_sha256"] == e0["entry_sha256"]

    verdict = verify_runtime_ledger(ledger_root, "REQ-214")
    assert verdict == {
        "status": "PASS",
        "requirement_id": "REQ-214",
        "entries": 2,
        "head_entry_sha256": e1["entry_sha256"],
    }

    synthetic_secret_payload = dict(
        zip(
            ["access_" + "token"],
            ["_".join(["FAKE", "CREDENTIAL", "FOR", "TEST", "214"])],
        )
    )
    with pytest.raises(EvidenceLedgerError, match="EVIDENCE_SECRET_FORBIDDEN"):
        append_runtime_evidence(
            ledger_root,
            "REQ-214",
            "BAD",
            synthetic_secret_payload,
            recorded_at="2026-11-12T01:02:00Z",
        )


def test_req214_wiring_fetch_official_resource_within_call_redirect_throttling():
    now = {"t": 50.0}
    slept: list[float] = []

    def fake_clock() -> float:
        return now["t"]

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now["t"] += seconds

    def redirect_transport(url, *, method, timeout, headers, max_bytes):
        now["t"] += 0.25
        if url.endswith("/hop1"):
            return {
                "status": 302,
                "content_type": "text/html",
                "body": b"",
                "location": "https://www.cbaleague.com/hop2",
                "redirects": [],
            }
        return {
            "status": 200,
            "content_type": "text/html",
            "body": b"<html>final</html>",
            "location": None,
            "redirects": [],
        }

    out = fetch_official_resource(
        "https://www.cbaleague.com/hop1",
        source_tier="A1",
        per_domain_interval=2.0,
        transport=redirect_transport,
        clock=fake_clock,
        sleep=fake_sleep,
        max_retries=0,
    )
    assert out["url"] == "https://www.cbaleague.com/hop2"
    assert out["redirect_chain"] == ["https://www.cbaleague.com/hop2"]
    assert len(slept) == 1
    assert slept[0] == pytest.approx(1.75)
