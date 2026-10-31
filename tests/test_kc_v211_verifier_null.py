"""CBA-KB v2.1.1 T2-1 Q2-round-2 repair — JSON null bypass tests.

Written FIRST, before the fix. A parsed JSON null is a value,
not an absence: the verifier used None both as "not parsed"
and as the parsed value, so a null answer.json or
guarantee.json skipped semantic validation entirely and
verified valid True. Each test pins the EXPLICIT failure
reason, not just valid False, so an indirect failure
(e.g. snapshot null failing only via the guarantee check)
does not pass.

Scope: verify_local_projection null handling ONLY (Q2
round-2 blocker D-02 remainder). No other behaviour is
changed or re-tested here.

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


def _rewrite_file_entry(out, rel, raw_bytes):
    full = os.path.join(out, rel)
    with open(full, "wb") as fh:
        fh.write(raw_bytes)
    manifest_path = os.path.join(out, "manifest.json")
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    for entry in manifest["files"]:
        if entry["path"] == rel:
            entry["sha256"] = sha256_hex(raw_bytes)
            entry["bytes"] = len(raw_bytes)
    m = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    manifest["manifest_sha256"] = sha256_hex(canonical_json(m))
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, sort_keys=True,
                  indent=2)


@pytest.mark.parametrize("rel,reason", [
    ("snapshot.json", "SNAPSHOT_NOT_OBJECT"),
    ("answer.json", "ANSWER_NOT_OBJECT"),
    ("guarantee.json", "GUARANTEE_NOT_OBJECT"),
])
def test_null_artifact_fails_explicitly(tmp_path, rel, reason):
    out = _sample(tmp_path)
    _rewrite_file_entry(out, rel, b"null")
    result = verify_local_projection(out)
    assert result["valid"] is False
    assert reason in result["failures"]


def test_clean_sample_verifies(tmp_path):
    out = _sample(tmp_path)
    result = verify_local_projection(out)
    assert result["valid"] is True
    assert result["failures"] == []
