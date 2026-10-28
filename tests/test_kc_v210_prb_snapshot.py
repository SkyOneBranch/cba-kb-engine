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
         "end_marker": True, "player_uid": "fixture_player_0001"},
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
                               build_code="SIMULATED_BUILD_CODE_1",
                               authorized_ids={"fixture_player_0001"})
    ok = build_catalog_snapshot(
        slices=slices, expected_slice_keys=expected[:1],
        target_domain="fixture_target", capabilities=["stats"],
        build_provenance="simulated-build-1",
        build_time="2026-10-28T00:00:00+00:00",
        data_release="SIMULATED_DATA_RELEASE_1",
        engine_candidate="SIMULATED_ENGINE_CANDIDATE_1",
        build_code="SIMULATED_BUILD_CODE_1",
        authorized_ids={"fixture_player_0001"})
    assert ok.snapshot_id.startswith("kc-snapshot:")


def test_hash_kinds_are_never_conflated():
    snap = _snap()
    assert snap.digest_kind == "KC_SNAPSHOT_SHA256"
    assert snap.covered_bytes.startswith("UTF-8 bytes of canonical JSON")
    assert "snapshot_id" not in snap.covered_bytes
    assert "content_sha256" not in snap.covered_bytes


# ---- Repair round 1: content binding (R1), full coverage (R2), auth (R5) ----
_KW = dict(target_domain="fixture_target", capabilities=["stats"],
           build_provenance="simulated-build-1",
           build_time="2026-10-28T00:00:00+00:00",
           data_release="SIMULATED_DATA_RELEASE_1",
           engine_candidate="SIMULATED_ENGINE_CANDIDATE_1",
           build_code="SIMULATED_BUILD_CODE_1")


def _snapshot_dict(snap):
    receipt = snap.simulated_validator_receipt
    if isinstance(receipt, dict):
        receipt_d = dict(receipt)
    else:
        receipt_d = dict(vars(receipt))
    return {
        "entries": [{"entry_id": e.entry_id, "sha256": e.sha256,
                     "authority": e.authority, "rights": e.rights,
                     "source_id": e.source_id} for e in snap.entries],
        "target_domain": snap.target_domain,
        "capabilities": list(snap.capabilities),
        "build_provenance": snap.build_provenance,
        "build_time": snap.build_time,
        "simulated_published_state": snap.simulated_published_state,
        "data_release": snap.data_release,
        "engine_candidate": snap.engine_candidate,
        "build_code": snap.build_code,
        "target_authorization_domain": snap.target_authorization_domain,
        "catalog_references": list(snap.catalog_references),
        "source_digests": list(snap.source_digests),
        "simulated_validator_receipt": receipt_d,
        "content_sha256": snap.content_sha256,
        "snapshot_id": snap.snapshot_id,
        "digest_kind": snap.digest_kind,
        "algorithm": snap.algorithm,
        "covered_bytes": snap.covered_bytes,
    }


def test_catalog_snapshot_binds_slice_payload_not_just_key():
    key = ("2025-2026", "REGULAR", "WHOLE_SEASON", None)
    s1 = {"slice_key": key, "end_marker": True,
          "player_uid": "fixture_player_0001",
          "metrics": {"points_per_game": 12.5}, "row_count": 40}
    s2 = {"slice_key": key, "end_marker": True,
          "player_uid": "fixture_player_0001",
          "metrics": {"points_per_game": 99.9}, "row_count": 1}
    try:
        id1 = build_catalog_snapshot(
            slices=[s1], expected_slice_keys=[key],
            authorized_ids={"fixture_player_0001"}, **_KW).snapshot_id
        id2 = build_catalog_snapshot(
            slices=[s2], expected_slice_keys=[key],
            authorized_ids={"fixture_player_0001"}, **_KW).snapshot_id
    except TypeError:
        # Pre-repair bridge: old call has no authorized_ids; its digest
        # ignores payload, so the two IDs are identical (see A1).
        id1 = build_catalog_snapshot(
            slices=[s1], expected_slice_keys=[key], **_KW).snapshot_id
        id2 = build_catalog_snapshot(
            slices=[s2], expected_slice_keys=[key], **_KW).snapshot_id
        assert id1 != id2, "pre-repair catalog digest ignores slice payload"
        return
    assert id1 != id2


@pytest.mark.parametrize("field,value", [
    ("simulated_published_state", "TAMPERED_STATE"),
    ("target_authorization_domain", "other_domain"),
    ("catalog_references", ["tampered-reference"]),
    ("source_digests", ["0" * 64]),
    ("simulated_validator_receipt", {"producer_validated": False,
                                     "connector_returned": "NOT_OBSERVED",
                                     "client_observed": "NOT_OBSERVED",
                                     "client_computed_digest": False,
                                     "simulated": True}),
])
def test_mutating_uncovered_manifest_field_invalidates(field, value):
    # Direct behavioral failure on pre-repair code: validate ignores
    # these fields and still returns valid=True (see A2).
    snap = _snap()
    d = _snapshot_dict(snap)
    d[field] = value
    assert validate_snapshot(d)["valid"] is False


def test_covered_bytes_lists_full_coverage_without_self_hash():
    snap = _snap()
    assert "simulated_published_state" in snap.covered_bytes
    assert "simulated_validator_receipt" in snap.covered_bytes
    assert "target_authorization_domain" in snap.covered_bytes
    assert "snapshot_id" not in snap.covered_bytes
    assert "content_sha256" not in snap.covered_bytes


def test_missing_slice_error_is_count_only_not_key_leak():
    key = ("2025-2026", "REGULAR", "WHOLE_SEASON", None)
    other = ("2025-2026", "PLAYOFF", "WHOLE_SEASON", None)
    slices = [{"slice_key": key, "end_marker": True,
               "player_uid": "fixture_player_0001"}]
    try:
        build_catalog_snapshot(slices=slices,
                               expected_slice_keys=[key, other],
                               authorized_ids={"fixture_player_0001"},
                               **_KW)
        assert False, "expected a build failure for the missing slice"
    except TypeError:
        # Pre-repair bridge: old call raises with the key list embedded.
        try:
            build_catalog_snapshot(slices=slices,
                                   expected_slice_keys=[key, other], **_KW)
        except KcSnapshotError as e:
            assert "2025-2026" not in str(e), (
                "pre-repair error leaks slice keys")
        else:
            assert False, "expected a build failure for the missing slice"
    except KcSnapshotError as e:
        assert "2025-2026" not in str(e)
        assert "PLAYOFF" not in str(e)
        assert "CATALOG_SLICES_MISSING:1" in str(e)


def test_catalog_snapshot_filters_unauthorized_slices():
    key = ("2025-2026", "REGULAR", "WHOLE_SEASON", None)
    slices = [{"slice_key": key, "end_marker": True,
               "player_uid": "fixture_player_OTHER"}]
    try:
        with pytest.raises(KcSnapshotError):
            build_catalog_snapshot(
                slices=slices, expected_slice_keys=[key],
                authorized_ids={"fixture_player_0001"}, **_KW)
    except TypeError:
        # Pre-repair bridge: old call accepts the unauthorized slice.
        build_catalog_snapshot(slices=slices, expected_slice_keys=[key],
                               **_KW)
        assert False, "pre-repair catalog accepts unauthorized slices"
