"""Frozen red tests for v2.4.0-2 hotfix: Drive.rename & Stage 4 post-rollback recovery (#280).

100% synthetic fixtures; zero real Drive IDs or local filesystem paths.
"""
from __future__ import annotations

import json
import threading
from typing import Any, Dict, List

import pytest

from cba_kb.common import digest
from cba_kb.drive import Drive, FIELDS
from cba_kb.orchestration.release import prepare_v215
from cba_kb.placement_facade import placement_enforced
from cba_kb.release import _apply_controlled_rename, fingerprint
from cba_kb.transport import API_CALL_STATS


# ---------------------------------------------------------------------------
# Synthetic mock Google Drive v3 API + Drive constructor helper
# ---------------------------------------------------------------------------


class _MockExec:
    def __init__(self, fn):
        self._fn = fn

    def execute(self, num_retries: int = 0):
        return self._fn(num_retries=num_retries)


class _MockFilesResource:
    def __init__(self, files_store: Dict[str, Dict[str, Any]]) -> None:
        self._files = files_store
        self.update_calls: List[Dict[str, Any]] = []
        self.get_calls: List[Dict[str, Any]] = []

    def update(self, **kwargs):
        def _exec(num_retries: int = 0):
            call = {**kwargs, "num_retries": num_retries}
            self.update_calls.append(call)
            fid = kwargs["fileId"]
            body = kwargs.get("body") or {}
            record = self._files[fid]
            if "name" in body:
                record["name"] = body["name"]
                record["version"] = str(int(record.get("version", "1")) + 1)
            return dict(record)

        return _MockExec(_exec)

    def get(self, **kwargs):
        def _exec(num_retries: int = 0):
            call = {**kwargs, "num_retries": num_retries}
            self.get_calls.append(call)
            fid = kwargs["fileId"]
            return dict(self._files[fid])

        return _MockExec(_exec)


class _MockDriveApi:
    def __init__(self, files_store: Dict[str, Dict[str, Any]]) -> None:
        self._files_resource = _MockFilesResource(files_store)

    def files(self) -> _MockFilesResource:
        return self._files_resource


def _make_drive_with_mock_api(
    files_store: Dict[str, Dict[str, Any]],
) -> tuple[Drive, _MockFilesResource]:
    """Construct a real Drive instance without OAuth I/O, backed by a synthetic API."""
    api = _MockDriveApi(files_store)
    drive = object.__new__(Drive)
    drive._credentials = None
    drive._main_thread_id = threading.get_ident()
    drive._read_local = threading.local()
    drive._read_semaphore = threading.BoundedSemaphore(1)
    drive._worker_clients_lock = threading.Lock()
    drive._worker_clients = []
    drive.api = api
    drive.docs = None
    drive._read_clients = (api, None)
    return drive, api._files_resource


# ---------------------------------------------------------------------------
# 1. Drive.rename unit tests
# ---------------------------------------------------------------------------


def test_drive_rename_off_main_thread_forbidden() -> None:
    store = {
        "synth_file_001": {
            "id": "synth_file_001",
            "name": "card_old.md",
            "mimeType": "text/markdown",
            "parents": ["synth_parent_data"],
            "version": "1",
            "modifiedTime": "2026-11-27T00:00:00Z",
        }
    }
    drive, files_res = _make_drive_with_mock_api(store)

    caught: List[BaseException] = []

    def _worker() -> None:
        try:
            drive.rename("synth_file_001", "card_new.md")
        except BaseException as exc:  # noqa: BLE001
            caught.append(exc)

    t = threading.Thread(target=_worker)
    t.start()
    t.join()

    assert len(caught) == 1
    assert isinstance(caught[0], RuntimeError)
    assert "WRITE_OFF_MAIN_THREAD_FORBIDDEN:rename" in str(caught[0])
    assert files_res.update_calls == []


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        "   ",
        "subdir/card.md",
        "subdir\\card.md",
        "card\x00.md",
        "card\n.md",
    ],
)
def test_drive_rename_rejects_invalid_filenames_fail_closed(bad_name: str) -> None:
    store = {
        "synth_file_002": {
            "id": "synth_file_002",
            "name": "card_old.md",
            "mimeType": "text/markdown",
            "parents": ["synth_parent_data"],
            "version": "1",
            "modifiedTime": "2026-11-27T00:00:00Z",
        }
    }
    drive, files_res = _make_drive_with_mock_api(store)

    with pytest.raises(ValueError):
        drive.rename("synth_file_002", bad_name)

    assert files_res.update_calls == []
    assert store["synth_file_002"]["name"] == "card_old.md"


def test_drive_rename_valid_call_and_api_call_stats_unchanged() -> None:
    store = {
        "synth_file_003": {
            "id": "synth_file_003",
            "name": "研究卡_合成球员甲_2025-2026_CURRENT.md",
            "mimeType": "text/markdown",
            "parents": ["synth_parent_data"],
            "version": "3",
            "modifiedTime": "2026-11-27T00:00:00Z",
        }
    }
    drive, files_res = _make_drive_with_mock_api(store)
    before_stat_keys = set(API_CALL_STATS.keys())

    new_name = "研究卡_合成球员甲_2025-2026_ARCHIVE.md"
    result = drive.rename("synth_file_003", new_name)

    assert len(files_res.update_calls) == 1
    call = files_res.update_calls[0]
    assert call["fileId"] == "synth_file_003"
    assert call["body"] == {"name": new_name}
    assert call["fields"] == FIELDS
    assert call["supportsAllDrives"] is True
    assert call["num_retries"] == 0
    assert result["id"] == "synth_file_003"
    assert result["name"] == new_name
    assert set(API_CALL_STATS.keys()) == before_stat_keys


# ---------------------------------------------------------------------------
# 2. Real wiring: placement_enforced(Drive).rename & _apply_controlled_rename
# ---------------------------------------------------------------------------


def test_placement_enforced_drive_rename_and_apply_controlled_rename_end_to_end() -> None:
    old_name = "研究卡_合成球员乙_2025-2026_CURRENT.md"
    new_name = "研究卡_合成球员乙_2025-2026_ARCHIVE.md"
    store = {
        "synth_file_004": {
            "id": "synth_file_004",
            "name": old_name,
            "mimeType": "text/markdown",
            "parents": ["synth_parent_data"],
            "version": "5",
            "modifiedTime": "2026-11-27T00:00:00Z",
        }
    }
    raw_drive, files_res = _make_drive_with_mock_api(store)
    enforced = placement_enforced(raw_drive)

    entry = {
        "id": "synth_file_004",
        "name": new_name,
        "rename_from": old_name,
    }
    _apply_controlled_rename(enforced, entry)

    assert store["synth_file_004"]["name"] == new_name
    assert len(files_res.update_calls) == 1
    assert files_res.update_calls[0]["body"] == {"name": new_name}
    # Pre-rename check + post-rename readback verification = 2 meta() calls
    assert len(files_res.get_calls) == 2

    # Direct placement_enforced.rename forwarding check
    third_name = "研究卡_合成球员乙_2025-2026_ARCHIVE_V2.md"
    meta = enforced.rename("synth_file_004", third_name)
    assert meta["name"] == third_name
    assert store["synth_file_004"]["name"] == third_name


# ---------------------------------------------------------------------------
# 3. Stage 4 post-rollback recovery: v2.4.0-2 spec & validate_release_state
# ---------------------------------------------------------------------------


class _FakeInstance:
    def __init__(self, production_policy: Dict[str, Any]) -> None:
        self._policy = production_policy

    def read_json(self, name: str) -> Dict[str, Any]:
        assert name == "production.json"
        return self._policy


class _FakeStatusDrive:
    def __init__(self, status_id: str, status_bytes: bytes, status_meta: Dict[str, Any]) -> None:
        self._status_id = status_id
        self._status_bytes = status_bytes
        self._status_meta = status_meta

    def meta(self, file_id: str) -> Dict[str, Any]:
        assert file_id == self._status_id
        return dict(self._status_meta)

    def get(self, file_id: str) -> bytes:
        assert file_id == self._status_id
        return self._status_bytes

    def get_with_meta(self, file_id: str, *, known_meta=None):
        assert file_id == self._status_id
        return self._status_bytes, dict(self._status_meta)


def _build_stage4_fixtures(
    *,
    release_id: str,
    status_state: str,
    active_release_id: str,
    rolled_back_release_id: str | None,
):
    status_id = "synth_status_file_001"
    target_id = "synth_target_file_001"
    reserved_id = "synth_reserved_file_001"
    staging_id = "synth_staging_folder_001"
    data_parent = "synth_data_folder_001"

    status_payload: Dict[str, Any] = {
        "state": status_state,
        "current_release_id": active_release_id,
        "artifacts": [
            {
                "id": target_id,
                "name": "synth_card.md",
                "sha256": digest(b"synth-content"),
            }
        ],
    }
    if rolled_back_release_id is not None:
        status_payload["rolled_back_release_id"] = rolled_back_release_id

    status_bytes = json.dumps(status_payload, sort_keys=True).encode("utf-8")
    status_hash = digest(status_bytes)
    status_meta = {
        "id": status_id,
        "name": "RELEASE_STATUS.json",
        "mimeType": "application/json",
        "parents": [data_parent],
        "version": "10",
        "modifiedTime": "2026-11-27T01:00:00Z",
    }
    status_fp = fingerprint(status_meta)
    sem_sig = digest(f"sem:{release_id}".encode("utf-8"))

    projection = {
        "release_id": release_id,
        "status_id": status_id,
        "semantic_delta_signature": sem_sig,
        "status_before_hash": status_hash,
        "status_before_meta": status_fp,
        "active_release_id": active_release_id,
        "active_target_ids": [target_id],
        "existing_targets": [
            {
                "id": target_id,
                "logical_key": "research/synth_card",
                "mime": "text/markdown",
                "mode": "binary",
                "allowed_parents": [data_parent],
                "publish_parent": data_parent,
            }
        ],
        "new_targets": [
            {
                "logical_key": "research/synth_new_card",
                "name": "synth_new_card.md",
                "mime": "text/markdown",
                "mode": "binary",
                "publish_parent": data_parent,
            }
        ],
        "historical_retired_policy_target_ids": [],
    }
    allocation = {
        "release_id": release_id,
        "status_id": status_id,
        "staging_id": staging_id,
        "semantic_delta_signature": sem_sig,
        "status_before_hash": status_hash,
        "status_before_meta": status_fp,
        "active_release_id": active_release_id,
        "active_production_targets": [target_id],
        "reservations": {"research/synth_new_card": reserved_id},
    }
    policy = {
        "status_id": status_id,
        "targets": {
            target_id: {
                "mime": "text/markdown",
                "mode": "binary",
                "allowed_parents": [data_parent],
                "publish_parent": data_parent,
            },
            reserved_id: {
                "mime": "text/markdown",
                "mode": "binary",
                "allowed_parents": [data_parent, staging_id],
                "staging_parent": staging_id,
                "publish_parent": data_parent,
            },
        },
    }
    drive = _FakeStatusDrive(status_id, status_bytes, status_meta)
    instance = _FakeInstance(policy)
    return drive, instance, projection, allocation


def test_v240_2_release_spec_registered_and_inherits_v240_1() -> None:
    assert "v2.4.0-2" in prepare_v215.V215_RELEASE_SPECS
    spec_2 = prepare_v215.V215_RELEASE_SPECS["v2.4.0-2"]
    spec_1 = prepare_v215.V215_RELEASE_SPECS["v2.4.0-1"]

    assert spec_2 == spec_1
    assert spec_2["product_baseline_sha"] == "3b65bcfcc9365d3f081150f516b9dba679ec965b"
    assert spec_2["enable_v240_shenzhen_narrative_pilot"] is True
    assert prepare_v215._release_spec("v2.4.0-2") == spec_2


def test_validate_release_state_accepts_v240_2_after_v240_1_rollback() -> None:
    drive, instance, projection, allocation = _build_stage4_fixtures(
        release_id="v2.4.0-2",
        status_state="ROLLED_BACK",
        active_release_id="v2.3.2-1",
        rolled_back_release_id="v2.4.0-1",
    )
    result = prepare_v215.validate_release_state(drive, instance, projection, allocation)

    assert result["result"] == "PASS"
    assert result["state"] == "ROLLED_BACK"
    assert result["rolled_back_release_id"] == "v2.4.0-1"
    assert result["active_production_target_count"] == 1
    assert result["reserved_staging_target_count"] == 1


@pytest.mark.parametrize(
    ("release_id", "rolled_back_release_id"),
    [
        ("v2.4.0-2", "v2.3.2-1"),
        ("v2.4.0-1", "v2.4.0-1"),
        ("v2.4.0-3", "v2.4.0-1"),
    ],
)
def test_validate_release_state_rejects_mismatched_rollback_pair(
    release_id: str,
    rolled_back_release_id: str,
) -> None:
    drive, instance, projection, allocation = _build_stage4_fixtures(
        release_id=release_id,
        status_state="ROLLED_BACK",
        active_release_id="v2.3.2-1",
        rolled_back_release_id=rolled_back_release_id,
    )
    with pytest.raises(prepare_v215.ProjectionError, match="STAGE4_RELEASE_STATE_DRIFT"):
        prepare_v215.validate_release_state(drive, instance, projection, allocation)


def test_validate_release_state_v240_2_still_fails_on_status_hash_drift() -> None:
    drive, instance, projection, allocation = _build_stage4_fixtures(
        release_id="v2.4.0-2",
        status_state="ROLLED_BACK",
        active_release_id="v2.3.2-1",
        rolled_back_release_id="v2.4.0-1",
    )
    allocation["status_before_hash"] = "0" * 64
    with pytest.raises(prepare_v215.ProjectionError, match="STAGE4_RELEASE_STATE_DRIFT"):
        prepare_v215.validate_release_state(drive, instance, projection, allocation)
