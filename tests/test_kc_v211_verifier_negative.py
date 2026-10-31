"""CBA-KB v2.1.1 T2-1 Q2-round-1 repair — verifier negative tests.

Written FIRST, before the verify_local_projection fix. Each test
asserts the verifier returns valid=False (never True, never an
exception) for a tampered or malformed local materialisation.

Scope: verify_local_projection fail-closed behaviour ONLY (Q2
blockers D-01 mandatory artifacts, D-02 malformed manifests).
No A/B/C behaviour is changed or re-tested here.

All fixtures are clean-room synthetic. No Drive, no real data.
"""

import json
import os

import pytest

from cba_kb.kc_cleanroom import build_cleanroom_fixture
from cba_kb.kc_local_producer import (
    build_local_projection,
    verify_local_projection,
)
from cba_kb.kc_projection import canonical_json, sha256_hex


def _sample(tmp_path):
    out = str(tmp_path / "s")
    build_local_projection(fixture=build_cleanroom_fixture(),
                           output_dir=out)
    return out


def _manifest_path(out):
    return os.path.join(out, "manifest.json")


def _load_manifest(out):
    with open(_manifest_path(out), encoding="utf-8") as fh:
        return json.load(fh)


def _save_manifest(out, manifest, recompute=True):
    if recompute:
        m = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
        manifest["manifest_sha256"] = sha256_hex(canonical_json(m))
    with open(_manifest_path(out), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, sort_keys=True,
                  indent=2)


def _drop_from_manifest(out, rel):
    manifest = _load_manifest(out)
    manifest["files"] = [f for f in manifest["files"]
                         if f["path"] != rel]
    _save_manifest(out, manifest)


def _rewrite_file_entry(out, rel, raw_bytes):
    full = os.path.join(out, rel)
    with open(full, "wb") as fh:
        fh.write(raw_bytes)
    manifest = _load_manifest(out)
    for entry in manifest["files"]:
        if entry["path"] == rel:
            entry["sha256"] = sha256_hex(raw_bytes)
            entry["bytes"] = len(raw_bytes)
    _save_manifest(out, manifest)


def _assert_invalid(out):
    result = verify_local_projection(out)
    assert result["valid"] is False
    assert result["failures"]


@pytest.mark.parametrize("rel", ["snapshot.json", "guarantee.json",
                                 "answer.json", "index.md",
                                 "catalog.md"])
def test_missing_required_artifact_fails(tmp_path, rel):
    out = _sample(tmp_path)
    os.remove(os.path.join(out, rel))
    _drop_from_manifest(out, rel)
    _assert_invalid(out)


def test_manifest_files_empty_fails(tmp_path):
    out = _sample(tmp_path)
    manifest = _load_manifest(out)
    manifest["files"] = []
    _save_manifest(out, manifest)
    _assert_invalid(out)


def test_manifest_files_absent_fails(tmp_path):
    out = _sample(tmp_path)
    manifest = {k: v for k, v in _load_manifest(out).items()
                if k != "files"}
    _save_manifest(out, manifest)
    _assert_invalid(out)


@pytest.mark.parametrize("raw", ["null", "[]", '"x"', "42"])
def test_manifest_root_not_object_fails(tmp_path, raw):
    out = _sample(tmp_path)
    with open(_manifest_path(out), "w", encoding="utf-8") as fh:
        fh.write(raw)
    _assert_invalid(out)


@pytest.mark.parametrize("files", [[None], {"a": 1}, "snapshot.json", 42,
                                   [{"path": 7, "sha256": "0" * 64,
                                     "bytes": 0}],
                                   [{"path": "", "sha256": "0" * 64,
                                     "bytes": 0}],
                                   [{"path": "../secret",
                                     "sha256": "0" * 64, "bytes": 0}]])
def test_manifest_files_malformed_fails(tmp_path, files):
    out = _sample(tmp_path)
    manifest = _load_manifest(out)
    manifest["files"] = files
    _save_manifest(out, manifest)
    _assert_invalid(out)


@pytest.mark.parametrize("rel", ["snapshot.json", "answer.json",
                                 "guarantee.json"])
def test_artifact_not_object_fails(tmp_path, rel):
    out = _sample(tmp_path)
    _rewrite_file_entry(out, rel, b"[]")
    _assert_invalid(out)


def test_manifest_snapshot_id_not_string_fails(tmp_path):
    out = _sample(tmp_path)
    manifest = _load_manifest(out)
    manifest["snapshot_id"] = 123
    _save_manifest(out, manifest)
    _assert_invalid(out)


def test_manifest_digest_mismatch_fails(tmp_path):
    out = _sample(tmp_path)
    manifest = _load_manifest(out)
    manifest["files"][0]["bytes"] += 1
    _save_manifest(out, manifest, recompute=False)
    _assert_invalid(out)


def test_tampered_file_bytes_fails(tmp_path):
    out = _sample(tmp_path)
    slice_dir = os.path.join(out, "slices")
    target = os.path.join(slice_dir, sorted(os.listdir(slice_dir))[0])
    with open(target, "a", encoding="utf-8") as fh:
        fh.write("tamper")
    _assert_invalid(out)


@pytest.mark.parametrize("prefix", ["slices/", "passages/"])
def test_missing_artifact_category_fails(tmp_path, prefix):
    out = _sample(tmp_path)
    manifest = _load_manifest(out)
    manifest["files"] = [f for f in manifest["files"]
                         if not f["path"].startswith(prefix)]
    _save_manifest(out, manifest)
    import shutil
    shutil.rmtree(os.path.join(out, prefix.rstrip("/")), ignore_errors=True)
    _assert_invalid(out)


def test_clean_sample_verifies(tmp_path):
    out = _sample(tmp_path)
    result = verify_local_projection(out)
    assert result["valid"] is True
    assert result["failures"] == []
