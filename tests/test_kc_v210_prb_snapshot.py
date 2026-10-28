"""PR-B (Issue #220) snapshot tests — written FIRST, must FAIL on old HEAD.

New module cba_kb.kc_snapshot does not exist on base 15867026.
All artifacts SIMULATED: simulated published state, simulated validator
receipt. Nothing here is real publication, real authorization, or real
Drive readback. Clean-room values only.
"""

import pytest
from dataclasses import FrozenInstanceError

from cba_kb.kc_snapshot import (
    KcSnapshotError,
    build_catalog_snapshot,
    build_snapshot,
    rebuild_snapshot,
    validate_snapshot,
)

SHA_A = "a" * 64
SHA_B = "b" * 64


def _entries():
    return [
        {"entry_id": "entry-1", "sha256": SHA_A, "authority": "canonical",
         "rights": "public", "source_id": "fixture_source_1"},
        {"entry_id": "entry-2", "sha256": SHA_B, "authority": "derived",
         "rights": "copyrighted", "source_id": "fixture_source_2"},
    ]


def _snap(**over):
    kw = dict(entries=_entries(), target_domain="fixture_target",
              capabilities=["stats", "passages"],
              build_provenance="simulated-build-1",
              build_time="2026-10-28T00:00:00+00:00",
              data_release="SIMULATED_DATA_RELEASE_1",
              engine_candidate="SIMULATED_ENGINE_CANDIDATE_1",
              build_code="SIMULATED_BUILD_CODE_1")
    kw.update(over)
    return build_snapshot(**kw)


def test_snapshot_id_is_content_sha256_and_tracks_separate(tmp_path=None):
    snap = _snap()
    assert snap.snapshot_id == "kc-snapshot:" + snap.content_sha256
    assert len(snap.content_sha256) == 64
    assert snap.digest_kind == "KC_SNAPSHOT_SHA256"
    assert snap.algorithm == "SHA-256"
    assert snap.data_release != snap.engine_candidate
    assert snap.engine_candidate != snap.build_code
    assert snap.simulated_published_state == "SIMULATED_PUBLISHED"
    receipt = snap.simulated_validator_receipt
    assert receipt.simulated is True
    assert receipt.producer_validated is True
    assert receipt.connector_returned == "NOT_OBSERVED"
    assert receipt.client_observed == "NOT_OBSERVED"
    assert receipt.client_computed_digest is False


def test_snapshot_is_immutable():
    snap = _snap()
    with pytest.raises(FrozenInstanceError):
        snap.target_domain = "other_target"


def test_content_change_changes_snapshot_id():
    snap1 = _snap()
    entries = _entries()
    entries[1] = dict(entries[1], sha256="c" * 64)
    snap2 = _snap(entries=entries)
    assert snap2.snapshot_id != snap1.snapshot_id


def test_deterministic_rebuild_is_identical():
    snap = _snap()
    result = rebuild_snapshot(snap, entries=_entries(),
                              target_domain="fixture_target",
                              capabilities=["stats", "passages"],
                              build_provenance="simulated-build-1",
                              build_time="2026-10-28T00:00:00+00:00",
                              data_release="SIMULATED_DATA_RELEASE_1",
                              engine_candidate="SIMULATED_ENGINE_CANDIDATE_1",
                              build_code="SIMULATED_BUILD_CODE_1")
    assert result["identical"] is True
    assert result["rebuilt_snapshot_id"] == snap.snapshot_id


def test_tampered_snapshot_fails_validation():
    snap = _snap()
    tampered = dict(snap.__dict__)
    tampered["content_sha256"] = "0" * 64
    assert validate_snapshot(snap)["valid"] is True
    assert validate_snapshot(tampered)["valid"] is False


def test_invalid_entry_is_a_build_failure():
    bad = _entries()
    bad[0] = dict(bad[0], sha256="not-a-sha256")
    with pytest.raises(KcSnapshotError):
        _snap(entries=bad)
    bad2 = _entries()
    bad2[0] = dict(bad2[0], authority="bogus")
    with pytest.raises(KcSnapshotError):
        _snap(entries=bad2)


def test_missing_slice_is_a_build_failure_not_silent_omission():
    slices = [
        {"slice_key": ("2025-2026", "REGULAR", "WHOLE_SEASON", None),
         "end_marker": True},
    ]
    expected = [
        ("2025-2026", "REGULAR", "WHOLE_SEASON", None),
        ("2025-2026", "PLAYOFF", "WHOLE_SEASON", None),
    ]
    with pytest.raises(KcSnapshotError):
        build_catalog_snapshot(slices=slices, expected_slice_keys=expected,
                               target_domain="fixture_target",
                               capabilities=["stats"],
                               build_provenance="simulated-build-1",
                               build_time="2026-10-28T00:00:00+00:00",
                               data_release="SIMULATED_DATA_RELEASE_1",
                               engine_candidate="SIMULATED_ENGINE_CANDIDATE_1",
                               build_code="SIMULATED_BUILD_CODE_1")
    ok = build_catalog_snapshot(
        slices=slices, expected_slice_keys=expected[:1],
        target_domain="fixture_target", capabilities=["stats"],
        build_provenance="simulated-build-1",
        build_time="2026-10-28T00:00:00+00:00",
        data_release="SIMULATED_DATA_RELEASE_1",
        engine_candidate="SIMULATED_ENGINE_CANDIDATE_1",
        build_code="SIMULATED_BUILD_CODE_1")
    assert ok.snapshot_id.startswith("kc-snapshot:")


def test_hash_kinds_are_never_conflated():
    snap = _snap()
    assert snap.digest_kind == "KC_SNAPSHOT_SHA256"
    assert snap.covered_bytes.startswith("UTF-8 bytes of canonical JSON")
    assert "snapshot_id" not in snap.covered_bytes
    assert "content_sha256" not in snap.covered_bytes
