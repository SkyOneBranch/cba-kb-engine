"""Frozen red acceptance tests for incremental in-place consumer/research updates (Issue #260).

Written BEFORE Commit 2 implementation for differential falsification:
- Test A: Incremental v2.2.0-1 projection, reservation, state validation, candidate
  inputs, manifest update, and freeze_plan with existing consumer/* and research/* keys
  plus one new consumer/collector_official_digest_current.md target.
- Test B: Non-consumer/non-research existing keys still fail closed with DECLARED_TARGET_CONFLICT.
- Test C: Retiring consumer/research keys still fail closed with DECLARED_TARGET_CONFLICT.
"""
from __future__ import annotations

import copy
import csv
import io
import json
from pathlib import Path
from typing import Any, Dict

from openpyxl import Workbook
import pytest
import yaml

from cba_kb.common import digest
from cba_kb.native import wrap
from cba_kb.orchestration.release import prepare_v215 as orch
from tests.test_release_orchestration import canonical_registry

ROOT = Path(__file__).resolve().parents[1]
V220_1_BASELINE_SHA = "fa9793c6db76e15880610ced728a7b0d563da604"


def _make_master_xlsx(row_count: int = 2) -> bytes:
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


def _build_incremental_fixture(tmp_path: Path):
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs/OPERATIONS_V1_5.md").write_text("ops body\n", encoding="utf-8")
    (tmp_path / "docs/MASTER_MIGRATION_NOTE.md").write_text("migration note\n", encoding="utf-8")

    reg = canonical_registry()
    reg_bytes = yaml.safe_dump(reg, allow_unicode=True, sort_keys=False).encode("utf-8")
    master_csv = (
        "record_key,season,club_id,player\n"
        "2025-2026|sz|0,2025-2026,sz,synth_0\n"
        "2025-2026|sz|1,2025-2026,sz,synth_1\n"
    ).encode("utf-8-sig")
    master_xlsx = _make_master_xlsx(2)
    sr_csv = b"source_id,source_title\nsrc_1,title_1\n"

    old_master_consumer = b"record_key,season\n2025-2026|sz|0,2025-2026\n"
    old_dict_md = b"# Old Dict v2.1.5-1\nend_marker: true\n"
    new_dict_md = b"# New Dict v2.2.0-1\nend_marker: true\n"
    old_sr_consumer = b"source_id,source_title\nsrc_1,title_1\n"
    old_doc_idx = b'{"documents": [], "unique_document_count": 0}\n'
    new_doc_idx = b'{"documents": [{"doc_id": "doc_001"}], "unique_document_count": 1}\n'
    old_research_card = b"# Research Card\nend_marker: true\n"
    new_digest_md = b"# Collector Digest v2.2.0-1\nend_marker: true\n"

    artifacts = [
        ("config/canonical_products.yaml", "id-reg", "canonical_products.yaml", "text/yaml", "binary", "config", reg_bytes),
        ("input/manifest.csv", "id-manifest", "manifest.csv", "text/csv", "binary", "config", b""),
        ("entry/README", "id-readme", "00_README_先读这个", orch.NATIVE_DOCUMENT, "managed_doc", "root", wrap("readme old\n").encode("utf-8")),
        ("derived/INDEX.md", "id-index", "INDEX", orch.NATIVE_DOCUMENT, "managed_doc", "data", wrap("index old\n").encode("utf-8")),
        ("entry/context", "id-context", "02_AI_PROJECT_CONTEXT_技术手册.md", "text/markdown", "binary", "root", b"ctx old\n"),
        ("ai/CONTEXT_CARD.md", "id-ctx-card", "CONTEXT_CARD.md", "text/markdown", "binary", "ai", b"card old\n"),
        ("CURRENT_VERSION_DOC", "id-ver-doc", "CBA-KB_v2.1.5.md", "text/markdown", "binary", "root", b"ver old\n"),
        ("derived/MASTER.csv", "id-master-csv", "MASTER.csv", "text/csv", "binary", "data", master_csv),
        ("input/MASTER.xlsx", "id-master-xlsx", "MASTER.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "binary", "data", master_xlsx),
        ("facts/CBA_注册领域_六表.xlsx", "id-six", "CBA_注册领域_六表.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "binary", "data", b"six"),
        ("facts/CBA_球员注册_EVENTS.xlsx", "id-events", "CBA_球员注册_EVENTS.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "binary", "data", b"events"),
        ("facts/CBA_外籍球员注册_SNAPSHOTS.xlsx", "id-foreign", "CBA_外籍球员注册_SNAPSHOTS.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "binary", "data", b"foreign"),
        ("facts/events.jsonl", "event-file", "events.jsonl", "application/x-ndjson", "binary", "data", b"{}\n"),
        ("facts/old_events.jsonl", "compat-file", "old_events.jsonl", "application/x-ndjson", "binary", "data", b"{}\n"),
        ("input/source_registry.csv", "id-sr", "source_registry.csv", "text/csv", "binary", "data", sr_csv),
        ("evidence/bayi_legacy_context.md", "id-ev", "bayi_legacy_context.md", "text/markdown", "binary", "evidence", b"ev"),
        ("consumer/master_domestic_2017_2027_current.csv", "id-c-master", "CBA_国内球员注册权威总表_2017-2027_CURRENT.csv", "text/csv", "binary", "data", old_master_consumer),
        ("consumer/field_dictionary_current.md", "id-c-dict", "CBA_核心数据表字段字典与权威口径_CURRENT.md", "text/markdown", "binary", "data", old_dict_md),
        ("consumer/source_registry_index_current.csv", "id-c-sr", "CBA_来源注册索引_脱敏视图_CURRENT.csv", "text/csv", "binary", "data", old_sr_consumer),
        ("consumer/document_index_current.json", "id-c-docidx", "CBA_文档索引_脱敏视图_CURRENT.json", "application/json", "binary", "data", old_doc_idx),
        ("research/card_907766_2025_2026", "id-r-card", "研究卡_林测试_907766_2025-2026_CURRENT.md", "text/markdown", "binary", "data", old_research_card),
    ]

    manifest_csv_text = (
        "uid,asset_type,source_type,source_url,local_path,content_hash,drive_file_id,ima_item_id,status,synced_at,published_release,hash_scope\n"
        + "\n".join(
            f"{lk},release_artifact,generated,,{lk},{digest(content)},{fid},,published,,v2.1.5-1,bytes"
            for lk, fid, _name, _mime, _mode, _parent, content in artifacts
        )
        + "\n"
    ).encode("utf-8-sig")

    bytes_by_id: Dict[str, bytes] = {}
    manifest_rows = []
    status_artifacts = []
    previous_targets = {}
    for lk, fid, name, mime, mode, parent, content in artifacts:
        actual_bytes = manifest_csv_text if lk == "input/manifest.csv" else content
        bytes_by_id[fid] = actual_bytes
        manifest_rows.append({"drive_file_id": fid, "uid": lk})
        status_artifacts.append({"id": fid, "name": name, "sha256": digest(actual_bytes)})
        previous_targets[fid] = {
            "mime": mime,
            "mode": mode,
            "allowed_parents": [parent, "staging"],
            "publish_parent": parent,
        }

    previous_status = {
        "state": "COMPLETE",
        "current_release_id": "v2.1.5-1",
        "code_commit": "a" * 40,
        "artifacts": status_artifacts,
    }
    bytes_by_id["status"] = json.dumps(previous_status).encode("utf-8")

    declared_consumer_targets = {
        "consumer/master_domestic_2017_2027_current.csv": {
            "name": "CBA_国内球员注册权威总表_2017-2027_CURRENT.csv",
            "mime": "text/csv",
            "mode": "binary",
            "publish_parent": "data",
            "content": old_master_consumer,
        },
        "consumer/field_dictionary_current.md": {
            "name": "CBA_核心数据表字段字典与权威口径_CURRENT.md",
            "mime": "text/markdown",
            "mode": "binary",
            "publish_parent": "data",
            "content": new_dict_md,
        },
        "consumer/source_registry_index_current.csv": {
            "name": "CBA_来源注册索引_脱敏视图_CURRENT.csv",
            "mime": "text/csv",
            "mode": "binary",
            "publish_parent": "data",
            "content": old_sr_consumer,
        },
        "consumer/document_index_current.json": {
            "name": "CBA_文档索引_脱敏视图_CURRENT.json",
            "mime": "application/json",
            "mode": "binary",
            "publish_parent": "data",
            "content": new_doc_idx,
        },
        "research/card_907766_2025_2026": {
            "name": "研究卡_林测试_907766_2025-2026_CURRENT.md",
            "mime": "text/markdown",
            "mode": "binary",
            "publish_parent": "data",
            "content": old_research_card,
        },
        "consumer/collector_official_digest_current.md": {
            "name": "CBA_官方采集动态与注册增量摘要_CURRENT.md",
            "mime": "text/markdown",
            "mode": "binary",
            "publish_parent": "data",
            "content": new_digest_md,
        },
    }

    return {
        "previous_status": previous_status,
        "previous_targets": previous_targets,
        "manifest_rows": manifest_rows,
        "bytes_by_id": bytes_by_id,
        "declared_consumer_targets": declared_consumer_targets,
        "old_bytes": {
            "consumer/master_domestic_2017_2027_current.csv": old_master_consumer,
            "consumer/field_dictionary_current.md": old_dict_md,
            "consumer/source_registry_index_current.csv": old_sr_consumer,
            "consumer/document_index_current.json": old_doc_idx,
            "research/card_907766_2025_2026": old_research_card,
        },
    }


def test_incremental_consumer_and_research_updates_end_to_end(monkeypatch, tmp_path):
    fx = _build_incremental_fixture(tmp_path)
    status_meta = {
        "id": "status",
        "version": "1",
        "modifiedTime": "2026-11-21T00:00:00Z",
        "mimeType": "application/json",
        "parents": ["config"],
    }
    status_hash = digest(fx["bytes_by_id"]["status"])

    proj = orch.project_targets(
        release_id="v2.2.0-1",
        engine_sha=V220_1_BASELINE_SHA,
        status_id="status",
        archive_id="archive",
        previous_status=fx["previous_status"],
        previous_targets=fx["previous_targets"],
        manifest_rows=fx["manifest_rows"],
        tracked=[],
        tracked_hashes={},
        code_parent="scripts",
        status_hash=status_hash,
        status_meta=orch.fingerprint(status_meta),
        declared_consumer_targets=fx["declared_consumer_targets"],
    )

    assert proj["new_target_count"] == 1
    assert [t["logical_key"] for t in proj["new_targets"]] == [
        "consumer/collector_official_digest_current.md"
    ]
    assert proj["final_artifact_count"] == len(fx["previous_status"]["artifacts"]) + 1

    existing_by_key = {item["logical_key"]: item for item in proj["existing_targets"]}
    for key in (
        "consumer/master_domestic_2017_2027_current.csv",
        "consumer/field_dictionary_current.md",
        "consumer/source_registry_index_current.csv",
        "consumer/document_index_current.json",
        "research/card_907766_2025_2026",
    ):
        item = existing_by_key[key]
        expected_sha = digest(fx["declared_consumer_targets"][key]["content"])
        assert item["consumer_target_kind"] == "consumer_surface"
        assert item["current_sha256"] == expected_sha

    assert existing_by_key["consumer/field_dictionary_current.md"]["disposition"] == "modified"
    assert existing_by_key["consumer/document_index_current.json"]["disposition"] == "modified"
    assert existing_by_key["consumer/master_domestic_2017_2027_current.csv"]["disposition"] == "unchanged"
    assert existing_by_key["consumer/source_registry_index_current.csv"]["disposition"] == "unchanged"
    assert existing_by_key["research/card_907766_2025_2026"]["disposition"] == "unchanged"

    # Changing an updated consumer target's content MUST change semantic_delta_signature
    alt_targets = copy.deepcopy(fx["declared_consumer_targets"])
    alt_targets["consumer/document_index_current.json"]["content"] = b'{"documents": [{"doc_id": "doc_999"}], "unique_document_count": 1}\n'
    proj_alt = orch.project_targets(
        release_id="v2.2.0-1",
        engine_sha=V220_1_BASELINE_SHA,
        status_id="status",
        archive_id="archive",
        previous_status=fx["previous_status"],
        previous_targets=fx["previous_targets"],
        manifest_rows=fx["manifest_rows"],
        tracked=[],
        tracked_hashes={},
        code_parent="scripts",
        status_hash=status_hash,
        status_meta=orch.fingerprint(status_meta),
        declared_consumer_targets=alt_targets,
    )
    assert proj_alt["semantic_delta_signature"] != proj["semantic_delta_signature"]

    # Reservation covers ONLY the 1 brand-new target
    allocation = {
        "schema_version": 1,
        "state": "RESERVED",
        "release_id": "v2.2.0-1",
        "status_id": "status",
        "archive_id": "archive",
        "staging_id": "staging",
        "semantic_delta_signature": proj["semantic_delta_signature"],
        "status_before_hash": status_hash,
        "status_before_meta": orch.fingerprint(status_meta),
        "active_release_id": "v2.1.5-1",
        "active_production_targets": proj["active_target_ids"],
        "reserved_staging_targets": ["res-digest-01"],
        "reservations": {"consumer/collector_official_digest_current.md": "res-digest-01"},
    }
    assert orch.validate_reservation(proj, allocation) is True
    reprojected = orch.reproject_targets(proj, allocation)
    assert reprojected["state"] == "REPROJECTED"

    # Fake instance + snapshot + prepare_release for validate_release_state, _candidate_inputs, and freeze_plan
    production_policy = {
        "status_id": "status",
        "archive_id": "archive",
        "targets": {
            **{
                fid: copy.deepcopy(spec)
                for fid, spec in fx["previous_targets"].items()
            },
            "res-digest-01": {
                "mime": "text/markdown",
                "mode": "binary",
                "allowed_parents": ["data", "staging"],
                "staging_parent": "staging",
                "publish_parent": "data",
            },
        },
        "dependencies": [{"id": "dep-1", "name": "dep1"}],
    }
    import_inventory = {
        "parents": {
            "root": "root",
            "ai": "ai",
            "scripts": "scripts",
            "config": "config",
            "data": "data",
            "archive": "archive",
            "evidence": "evidence",
        }
    }

    class _FakeInstance:
        data_root = tmp_path / "inst_data"

        def read_json(self, name: str) -> Dict[str, Any]:
            if name == "production.json":
                return copy.deepcopy(production_policy)
            if name == "import_inventory.json":
                return copy.deepcopy(import_inventory)
            if name == "runtime.json":
                return {"inputs": {"manifest.csv": {"id": "id-manifest"}}}
            raise AssertionError(f"Unexpected config read: {name}")

    fake_instance = _FakeInstance()
    fake_instance.data_root.mkdir(parents=True, exist_ok=True)

    class _FakeDrive:
        def list(self, *_args, **_kwargs):
            return []

        def get(self, file_id: str) -> bytes:
            return fx["bytes_by_id"][file_id]

        def meta(self, file_id: str) -> Dict[str, Any]:
            if file_id == "status":
                return status_meta
            return {"id": file_id, "version": "1", "modifiedTime": "t", "mimeType": "application/octet-stream", "parents": ["data"]}

    def fake_snapshot(_drive, file_id: str, _mode: str = "binary"):
        return fx["bytes_by_id"][file_id], _FakeDrive().meta(file_id)

    monkeypatch.setattr(orch, "snapshot", fake_snapshot)
    monkeypatch.setattr(
        orch, "verify_execution_sha",
        lambda root, sha, baseline=None: {"head": sha, "clean": True},
    )
    monkeypatch.setattr(orch, "load_production_dependencies", lambda _d, _p: [{"id": "dep-1", "name": "dep1"}])

    state = orch.validate_release_state(_FakeDrive(), fake_instance, reprojected, allocation)
    assert state["status"]["current_release_id"] == "v2.1.5-1"

    consumer_contents = {
        k: v["content"] for k, v in fx["declared_consumer_targets"].items()
    }

    # Fail closed on missing or mismatched consumer content for existing updated target
    missing_contents = dict(consumer_contents)
    missing_contents.pop("consumer/document_index_current.json")
    with pytest.raises(orch.ProjectionError, match="CONSUMER_SURFACE_CONTENT_MISSING:consumer/document_index_current.json"):
        orch._candidate_inputs(
            _FakeDrive(), tmp_path, reprojected, allocation, tmp_path / "cand_missing",
            consumer_target_contents=missing_contents,
        )

    tampered_contents = dict(consumer_contents)
    tampered_contents["consumer/field_dictionary_current.md"] = b"tampered"
    with pytest.raises(orch.ProjectionError, match="CONSUMER_SURFACE_HASH_MISMATCH:consumer/field_dictionary_current.md"):
        orch._candidate_inputs(
            _FakeDrive(), tmp_path, reprojected, allocation, tmp_path / "cand_tampered",
            consumer_target_contents=tampered_contents,
        )

    entries = orch._candidate_inputs(
        _FakeDrive(), tmp_path, reprojected, allocation, tmp_path / "cand_ok",
        consumer_target_contents=consumer_contents,
    )
    by_key = {e["logical_key"]: e for e in entries}

    assert by_key["consumer/collector_official_digest_current.md"]["change_class"] == "NEW_CONSUMER_SURFACE"
    assert by_key["consumer/collector_official_digest_current.md"]["before_hash"] is None

    for mod_key in ("consumer/field_dictionary_current.md", "consumer/document_index_current.json"):
        assert by_key[mod_key]["change_class"] == "CONSUMER_SURFACE_UPDATE"
        assert by_key[mod_key]["before_hash"] == digest(fx["old_bytes"][mod_key])
        assert Path(by_key[mod_key]["path"]).read_bytes() == consumer_contents[mod_key]

    for unch_key in (
        "consumer/master_domestic_2017_2027_current.csv",
        "consumer/source_registry_index_current.csv",
        "research/card_907766_2025_2026",
    ):
        assert by_key[unch_key]["change_class"] == "CONSUMER_SURFACE_CARRY_FORWARD"
        assert by_key[unch_key]["before_hash"] == digest(fx["old_bytes"][unch_key])

    manifest_rows_out = {
        r["uid"]: r
        for r in csv.DictReader(
            io.StringIO(Path(by_key["input/manifest.csv"]["path"]).read_text(encoding="utf-8-sig"))
        )
    }
    assert manifest_rows_out["consumer/document_index_current.json"]["content_hash"] == digest(
        consumer_contents["consumer/document_index_current.json"]
    )
    assert manifest_rows_out["consumer/collector_official_digest_current.md"]["published_release"] == "v2.2.0-1"

    # Full freeze_plan completion
    def fake_prepare(drive, outbox, release_id, plan_entries, archive, status, dependencies, **options):
        outbox.mkdir(parents=True)
        plan = {
            "release_id": release_id,
            "dependencies": dependencies,
            "entries": [
                {
                    **entry,
                    "before": f"before/{idx}",
                    "candidate": f"candidate/{idx}",
                    "after_hash": digest(Path(entry["path"]).read_bytes()),
                }
                for idx, entry in enumerate(plan_entries)
            ],
        }
        (outbox / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        (outbox / "journal.json").write_text(json.dumps({"state": "PREPARED"}), encoding="utf-8")
        return plan

    monkeypatch.setattr(orch, "prepare_release", fake_prepare)
    freeze_res = orch.freeze_plan(
        _FakeDrive(),
        instance=fake_instance,
        engine_root=tmp_path,
        release_id="v2.2.0-1",
        projection=proj,
        allocation=allocation,
        output=tmp_path / "freeze_out",
        consumer_target_contents=consumer_contents,
    )
    assert freeze_res["state"] == "FROZEN_PLAN"
    assert freeze_res["entries"] == len(fx["previous_status"]["artifacts"]) + 1


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "derived/MASTER.csv",
        "input/source_registry.csv",
        "facts/CBA_注册领域_六表.xlsx",
        "entry/README",
    ],
)
def test_non_consumer_existing_target_still_raises_conflict(tmp_path, forbidden_key):
    fx = _build_incremental_fixture(tmp_path)
    bad_declared = {
        forbidden_key: {
            "name": "bad_override",
            "mime": "text/plain",
            "content": b"override",
        }
    }
    with pytest.raises(orch.ProjectionError, match=f"DECLARED_TARGET_CONFLICT:{forbidden_key}"):
        orch.project_targets(
            release_id="v2.2.0-1",
            engine_sha=V220_1_BASELINE_SHA,
            status_id="status",
            archive_id="archive",
            previous_status=fx["previous_status"],
            previous_targets=fx["previous_targets"],
            manifest_rows=fx["manifest_rows"],
            tracked=[],
            tracked_hashes={},
            code_parent="scripts",
            declared_consumer_targets=bad_declared,
        )


def test_retiring_consumer_target_still_raises_conflict(tmp_path):
    fx = _build_incremental_fixture(tmp_path)
    prev_targets = copy.deepcopy(fx["previous_targets"])
    prev_targets["id-r-card"]["retire_in_release"] = "v2.2.0-1"

    bad_declared = {
        "research/card_907766_2025_2026": {
            "name": "研究卡_林测试_907766_2025-2026_CURRENT.md",
            "mime": "text/markdown",
            "content": b"revive",
        }
    }
    with pytest.raises(orch.ProjectionError, match="DECLARED_TARGET_CONFLICT:research/card_907766_2025_2026"):
        orch.project_targets(
            release_id="v2.2.0-1",
            engine_sha=V220_1_BASELINE_SHA,
            status_id="status",
            archive_id="archive",
            previous_status=fx["previous_status"],
            previous_targets=prev_targets,
            manifest_rows=fx["manifest_rows"],
            tracked=[],
            tracked_hashes={},
            code_parent="scripts",
            declared_consumer_targets=bad_declared,
        )
