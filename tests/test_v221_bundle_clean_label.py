"""Regression test for Issue #266: preserve logical_key/name extension when
secret-scanning bundle_v1 members in build_release_bundle() and
extract_release_bundle().
"""
from __future__ import annotations

import json
from pathlib import Path

from cba_kb import release as release_module
from cba_kb.common import atomic, digest, save


def test_bundle_v1_clean_uses_logical_key_extension_for_python_sources(tmp_path: Path):
    """Python code like src/cba_kb/drive_http.py must pass bundle build & extract clean()."""
    repo_root = Path(__file__).resolve().parents[1]
    py_bytes = (repo_root / "src" / "cba_kb" / "drive_http.py").read_bytes()
    before_bytes = b""

    root = tmp_path / "outbox"
    root.mkdir(parents=True, exist_ok=True)
    atomic(root / "before/0", before_bytes)
    atomic(root / "candidate/0", py_bytes)

    status_before = json.dumps(
        {"state": "COMPLETE", "current_release_id": "v2.2.0-1", "artifacts": []},
        sort_keys=True,
    ).encode("utf-8")
    atomic(root / "status.before.json", status_before)

    plan = {
        "release_id": "v2.2.1-1",
        "previous_release_id": "v2.2.0-1",
        "archive_format": release_module.ARCHIVE_FORMAT_BUNDLE_V1,
        "status_before_hash": digest(status_before),
        "entries": [
            {
                "id": "f-drive-http",
                "logical_key": "code/src/cba_kb/drive_http.py",
                "name": "drive_http.py",
                "mime": "text/x-python",
                "mode": "binary",
                "before": "before/0",
                "candidate": "candidate/0",
                "before_hash": digest(before_bytes),
                "after_hash": digest(py_bytes),
            }
        ],
    }
    save(root / "plan.json", plan)

    bundle = release_module.build_release_bundle(root, plan)
    extracted = release_module.extract_release_bundle(bundle["bytes"])
    assert extracted["members"]["candidate/0"] == py_bytes
    assert extracted["members"]["before/0"] == before_bytes
