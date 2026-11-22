"""Regression test for Issue #264: resolve legacy_annotations protected file IDs
from canonical_products.yaml + manifest.csv in freeze_closure() under bundle_v1.

On real production Drive, the evidence file referenced by
``legacy_annotations.artifact_key`` (``evidence/bayi_legacy_context.md``) has a
localized Chinese display name rather than ``bayi_legacy_context.md``.
``freeze_closure()`` must resolve its Drive file ID via the frozen candidate
registry + manifest (never by hardcoded filename) and download its local bytes
into ``protected/<index>``, while keeping unrelated protected evidence files on
server-side ``sha256Checksum`` verification.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from cba_kb import release as release_module
from cba_kb.common import atomic, digest


class _BundleClosureDrive:
    """Minimal Drive double with server-native sha256Checksum support."""

    def __init__(self) -> None:
        self.files: Dict[str, Dict[str, Any]] = {}
        self.downloads: List[str] = []

    def add_folder(self, fid: str, name: str, parent: str = "root") -> str:
        self.files[fid] = {
            "id": fid,
            "name": name,
            "mimeType": release_module.FOLDER,
            "parents": [parent],
            "version": "1",
            "modifiedTime": "2026-11-22T00:00:00Z",
            "content": b"",
        }
        return fid

    def add_file(
        self,
        fid: str,
        name: str,
        content: bytes,
        *,
        mime: str = "text/plain",
        parent: str = "evidence",
    ) -> str:
        self.files[fid] = {
            "id": fid,
            "name": name,
            "mimeType": mime,
            "parents": [parent],
            "version": "1",
            "modifiedTime": "2026-11-22T00:00:00Z",
            "content": content,
        }
        return fid

    def meta(self, fid: str) -> Dict[str, Any]:
        rec = self.files[fid]
        out = {
            "id": rec["id"],
            "name": rec["name"],
            "mimeType": rec["mimeType"],
            "parents": list(rec["parents"]),
            "version": rec["version"],
            "modifiedTime": rec["modifiedTime"],
        }
        if rec["mimeType"] != release_module.FOLDER:
            out["sha256Checksum"] = digest(rec["content"])
            out["size"] = str(len(rec["content"]))
        return out

    def get(self, fid: str) -> bytes:
        self.downloads.append(fid)
        return self.files[fid]["content"]

    def get_with_meta(self, fid: str, *, known_meta=None):
        self.downloads.append(fid)
        return self.files[fid]["content"], self.meta(fid)

    def list(self, parent: str) -> List[Dict[str, Any]]:
        return [
            self.meta(fid)
            for fid, rec in self.files.items()
            if parent in rec.get("parents", [])
        ]


def _build_minimal_registry_bytes() -> bytes:
    reg = {
        "registry_version": "1",
        "registry_release_id": "v2.2.1-1",
        "products": [
            {
                "product_id": "domestic_season_snapshots",
                "semantic_grain": "player_season",
                "authority": "canonical",
                "business_key": ["uid"],
                "artifact_key": "derived/MASTER.csv",
                "generated_from": [],
                "deprecated_by": None,
                "read_policy": "canonical_only",
                "current_eligible": True,
                "independent_write_allowed": True,
            },
            {
                "product_id": "registration_status_events",
                "semantic_grain": "event",
                "authority": "canonical",
                "business_key": ["event_id"],
                "artifact_key": "data/registration_events.csv",
                "generated_from": [],
                "deprecated_by": None,
                "read_policy": "canonical_only",
                "current_eligible": True,
                "independent_write_allowed": True,
            },
            {
                "product_id": "legacy_registration_events_compat",
                "semantic_grain": "legacy_event",
                "authority": "compatibility",
                "business_key": ["event_id"],
                "artifact_key": "data/legacy_events.csv",
                "generated_from": ["registration_status_events"],
                "deprecated_by": "registration_status_events",
                "read_policy": "compatibility_only",
                "current_eligible": True,
                "independent_write_allowed": False,
                "projection": {
                    "kind": "legacy_registration_events_v1",
                    "source": "registration_status_events",
                    "relationship_source": "domestic_season_snapshots",
                    "master_source": "domestic_season_snapshots",
                    "legacy_annotations": {
                        "artifact_key": "evidence/bayi_legacy_context.md",
                        "parser": "midseason_md",
                        "fields": ["club_announcement_date"],
                    },
                    "scope": {
                        "foreign": {
                            "season": "2024-2025",
                            "player_type": "foreign",
                            "event_domain": "foreign_registration",
                            "event_type": "registration_cancelled",
                        },
                        "domestic": {
                            "season": "2020-2021",
                            "player_type": "domestic",
                            "event_domain": "domestic_movement",
                            "from_club_id": "bayi",
                        },
                    },
                },
            },
        ],
    }
    return yaml.safe_dump(reg, allow_unicode=True, sort_keys=False).encode("utf-8")


def _build_manifest_bytes(rows: List[Dict[str, str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["uid", "drive_file_id", "content_hash"])
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def test_freeze_closure_downloads_localized_legacy_annotation_evidence_under_bundle_v1(tmp_path: Path):
    """Issue #264: localized Drive name for legacy_annotations evidence must still freeze local bytes."""
    drv = _BundleClosureDrive()
    drv.add_folder("root", "root")
    drv.add_folder("current", "00_current", parent="root")
    drv.add_folder("evidence", "20_evidence", parent="root")

    master_bytes = b"uid,name\np1,PlayerA\n"
    six_bytes = b"event_id,uid\ne1,p1\n"
    sr_bytes = b"source_id,name\ns1,SourceA\n"
    ann_bytes = b"# Localized legacy midseason context\n"
    normal_ev_bytes = b"# Unrelated evidence doc\n"

    drv.add_file("f-master", "MASTER.csv", master_bytes, mime="text/csv", parent="current")
    drv.add_file("f-six", "registration_events.csv", six_bytes, mime="text/csv", parent="current")
    drv.add_file("f-sr", "source_registry.csv", sr_bytes, mime="text/csv", parent="current")
    # Real production scenario: Drive display name is Chinese, NOT "bayi_legacy_context.md"
    drv.add_file(
        "f-ev-ann",
        "八一球员_2021赛季中期转会核验.md",
        ann_bytes,
        mime="text/markdown",
        parent="evidence",
    )
    drv.add_file(
        "f-ev-normal",
        "unrelated_evidence.md",
        normal_ev_bytes,
        mime="text/markdown",
        parent="evidence",
    )

    reg_bytes = _build_minimal_registry_bytes()
    man_bytes = _build_manifest_bytes([
        {"uid": "derived/MASTER.csv", "drive_file_id": "f-master", "content_hash": digest(master_bytes)},
        {"uid": "data/registration_events.csv", "drive_file_id": "f-six", "content_hash": digest(six_bytes)},
        {"uid": "input/source_registry.csv", "drive_file_id": "f-sr", "content_hash": digest(sr_bytes)},
        {"uid": "evidence/bayi_legacy_context.md", "drive_file_id": "f-ev-ann", "content_hash": digest(ann_bytes)},
        {"uid": "evidence/unrelated_evidence.md", "drive_file_id": "f-ev-normal", "content_hash": digest(normal_ev_bytes)},
    ])

    root = tmp_path / "outbox"
    root.mkdir(parents=True, exist_ok=True)

    control_specs = [
        ("f-reg", "config/canonical_products.yaml", "canonical_products.yaml", reg_bytes),
        ("f-man", "input/manifest.csv", "manifest.csv", man_bytes),
        ("f-readme", "entry/README", "README.md", b"readme\n"),
        ("f-index", "derived/INDEX.md", "INDEX.md", b"index\n"),
        ("f-ctx", "ai/CONTEXT_CARD.md", "CONTEXT_CARD.md", b"ctx\n"),
        ("f-ver", "CURRENT_VERSION_DOC", "CURRENT_VERSION.md", b"v2.2.1-1\n"),
    ]
    entries = []
    for idx, (fid, lkey, name, content) in enumerate(control_specs):
        atomic(root / f"before/{idx}", content)
        atomic(root / f"candidate/{idx}", content)
        entries.append({
            "id": fid,
            "logical_key": lkey,
            "name": name,
            "mime": "text/plain",
            "mode": "binary",
            "before": f"before/{idx}",
            "candidate": f"candidate/{idx}",
            "before_hash": digest(content),
            "after_hash": digest(content),
        })

    plan = {
        "release_id": "v2.2.1-1",
        "previous_release_id": "v2.2.0-1",
        "archive_format": release_module.ARCHIVE_FORMAT_BUNDLE_V1,
        "entries": entries,
        "closure": {
            "code_commit": "d" * 40,
            "previous_code_commit": "e" * 40,
            "baseline_release_id": "v2.2.0-1",
            "registry_key": "config/canonical_products.yaml",
            "manifest_key": "input/manifest.csv",
            "documents": {
                "readme": "entry/README",
                "index": "derived/INDEX.md",
                "context_card": "ai/CONTEXT_CARD.md",
                "current_version_doc": "CURRENT_VERSION_DOC",
            },
            "zones": {
                "current": ["current"],
                "evidence": ["evidence"],
                "history": [],
                "staging": [],
            },
            "protected": [
                {"id": "f-master", "name": "MASTER.csv", "kind": "master", "sha256": digest(master_bytes)},
                {"id": "f-six", "name": "registration_events.csv", "kind": "six_table", "sha256": digest(six_bytes)},
                {"id": "f-sr", "name": "source_registry.csv", "kind": "source_registry", "sha256": digest(sr_bytes)},
                {"id": "f-ev-ann", "name": "八一球员_2021赛季中期转会核验.md", "kind": "evidence", "sha256": digest(ann_bytes)},
                {"id": "f-ev-normal", "name": "unrelated_evidence.md", "kind": "evidence", "sha256": digest(normal_ev_bytes)},
            ],
        },
    }

    release_module.freeze_closure(drv, root, plan)

    by_id = {item["id"]: item for item in plan["closure"]["protected"]}
    # Localized legacy_annotations evidence MUST have local frozen bytes
    assert "before" in by_id["f-ev-ann"], "legacy_annotations evidence missing local before path"
    ann_local = root / by_id["f-ev-ann"]["before"]
    assert ann_local.is_file()
    assert ann_local.read_bytes() == ann_bytes

    # Unrelated evidence MUST remain server_sha256 without downloading bytes
    assert "before" not in by_id["f-ev-normal"]
    assert by_id["f-ev-normal"].get("verification_mode") == "server_sha256"
    assert "f-ev-normal" not in drv.downloads
    assert "f-ev-ann" in drv.downloads
