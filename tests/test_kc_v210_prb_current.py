"""PR-B (Issue #220) current-pointer tests — written FIRST, must FAIL on
old HEAD. New module cba_kb.kc_current does not exist on base 15867026.

OFFLINE state machine ONLY. The pointer update is SIMULATED, the
observed receipt is synthetic/offline evidence. Real Drive reads, real
ACL checks, and real cache propagation are NOT proven by this PR.
Clean-room values only.
"""

import pytest

from cba_kb.kc_current import (
    KcCurrentError,
    SimulatedCurrentStore,
    classify_status,
    fallback_snapshot,
    resolve_current,
)


def _snap_id(name):
    return "kc-snapshot:" + name


def test_stage_validate_commit_readback_flow():
    from cba_kb.kc_snapshot import build_snapshot
    snap = build_snapshot(
        entries=[{"entry_id": "entry-stats", "sha256": "a" * 64,
                  "authority": "canonical", "rights": "public",
                  "source_id": "fixture_stats"}],
        target_domain="fixture_target", capabilities=["stats"],
        build_provenance="simulated-build-1",
        build_time="2026-10-28T00:00:00+00:00",
        data_release="SIMULATED_DATA_RELEASE_1",
        engine_candidate="SIMULATED_ENGINE_CANDIDATE_1",
        build_code="SIMULATED_BUILD_CODE_1")
    store = SimulatedCurrentStore(target_domain="fixture_target")
    store.register(snap)
    store.stage(snap.snapshot_id)
    assert store.state == "STAGED"
    store.validate()
    assert store.state == "VALIDATED"
    receipt = store.commit()
    assert store.state == "CURRENT"
    assert store.read_current() == snap.snapshot_id
    assert receipt["snapshot_id"] == snap.snapshot_id
    assert receipt["simulated"] is True
    assert receipt["evidence_kind"] == "SYNTHETIC_OFFLINE"


def test_commit_without_validation_is_refused():
    store = SimulatedCurrentStore(target_domain="fixture_target")
    store.stage(_snap_id("v1"))
    with pytest.raises(KcCurrentError):
        store.commit()


def test_failed_validation_blocks_commit():
    store = SimulatedCurrentStore(target_domain="fixture_target")
    store.stage("")
    with pytest.raises(KcCurrentError):
        store.validate()
    assert store.state == "FAILED"
    with pytest.raises(KcCurrentError):
        store.commit()


def test_unchanged_entry_is_ok_without_reread():
    reads = iter([_snap_id("v1")])
    r = resolve_current(captured_snapshot_id=_snap_id("v1"),
                        read_entry=lambda: next(reads))
    assert r["status"] == "OK"
    assert r["snapshot_id"] == _snap_id("v1")
    assert r["reread_used"] is False


def test_changed_then_stable_entry_is_ok_after_one_reread():
    reads = iter([_snap_id("v2"), _snap_id("v2")])
    r = resolve_current(captured_snapshot_id=_snap_id("v1"),
                        read_entry=lambda: next(reads))
    assert r["status"] == "OK"
    assert r["snapshot_id"] == _snap_id("v2")
    assert r["reread_used"] is True


def test_changed_then_unstable_entry_is_stale_never_mixed():
    reads = iter([_snap_id("v2"), _snap_id("v3")])
    r = resolve_current(captured_snapshot_id=_snap_id("v1"),
                        read_entry=lambda: next(reads))
    assert r["status"] == "STALE"
    assert r["snapshot_id"] is None


def test_revoked_authorization_never_falls_back():
    r = resolve_current(captured_snapshot_id=_snap_id("v1"),
                        read_entry=lambda: _snap_id("v1"),
                        authorization_revoked=True,
                        last_safe_snapshot_id=_snap_id("v0"))
    assert r["status"] == "UNAVAILABLE"
    assert r["reason"] == "AUTHORIZATION_REVOKED_NO_FALLBACK"
    assert r["snapshot_id"] is None


def test_transitional_fallback_only_to_authorized_last_safe():
    ok = fallback_snapshot(last_safe_snapshot_id=_snap_id("v0"),
                           authorized=True)
    assert ok["status"] == "OK"
    assert ok["fallback"] == "LAST_SAFE_PREVIOUS"
    assert ok["labelled"] is True
    denied = fallback_snapshot(last_safe_snapshot_id=_snap_id("v0"),
                               authorized=False)
    assert denied["status"] == "STALE"
    assert denied["snapshot_id"] is None


def test_status_vocabulary_maps_to_rules():
    assert classify_status(evidence_present=False)["status"] == "UNKNOWN"
    assert classify_status(evidence_present=True,
                           claim_conflict=True)["status"] == "CONFLICT"
    assert classify_status(evidence_present=True,
                           freshness_ok=False)["status"] == "STALE"
    assert classify_status(evidence_present=True,
                           freshness_ok=True)["status"] == "OK"


# ---- Repair round 1: snapshot registry validation (R3) ----
# Bridge pattern: on pre-repair code SimulatedCurrentStore has no
# register() method, so the setup raises AttributeError; the except
# branch then runs the old prefix-only validation, which succeeds on
# ghost/tampered input and fails the assertion behaviorally (see A3).
# The no-registry test needs no bridge: old validate() accepts a bare
# prefixed ID, so pytest.raises fails directly on pre-repair code.

def _registered_snapshot():
    from cba_kb.kc_snapshot import build_snapshot
    return build_snapshot(
        entries=[{"entry_id": "entry-stats", "sha256": "a" * 64,
                  "authority": "canonical", "rights": "public",
                  "source_id": "fixture_stats"}],
        target_domain="fixture_target", capabilities=["stats"],
        build_provenance="simulated-build-1",
        build_time="2026-10-28T00:00:00+00:00",
        data_release="SIMULATED_DATA_RELEASE_1",
        engine_candidate="SIMULATED_ENGINE_CANDIDATE_1",
        build_code="SIMULATED_BUILD_CODE_1")


def _snapshot_dict_of(snap):
    receipt = snap.simulated_validator_receipt
    receipt_d = dict(receipt) if isinstance(receipt, dict) else dict(vars(receipt))
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


def test_ghost_snapshot_id_fails_validation():
    try:
        snap = _registered_snapshot()
        store = SimulatedCurrentStore(target_domain="fixture_target")
        store.register(snap)
    except AttributeError:
        store = SimulatedCurrentStore(target_domain="fixture_target")
        store.stage("kc-snapshot:ghost")
        store.validate()
        assert False, "pre-repair validate accepts unregistered snapshot ids"
    store.stage("kc-snapshot:ghost")
    with pytest.raises(KcCurrentError):
        store.validate()
    assert store.state == "FAILED"
    with pytest.raises(KcCurrentError):
        store.commit()


def test_tampered_snapshot_fails_validation():
    try:
        snap = _registered_snapshot()
        store = SimulatedCurrentStore(target_domain="fixture_target")
        tampered = _snapshot_dict_of(snap)
        tampered["content_sha256"] = "0" * 64
        store.register(tampered)
    except AttributeError:
        store = SimulatedCurrentStore(target_domain="fixture_target")
        store.stage("kc-snapshot:tampered")
        store.validate()
        assert False, "pre-repair validate never checks snapshot digests"
    store.stage(snap.snapshot_id)
    with pytest.raises(KcCurrentError):
        store.validate()
    assert store.state == "FAILED"


def test_target_mismatch_fails_validation():
    from cba_kb.kc_snapshot import build_snapshot
    try:
        other = build_snapshot(
            entries=[{"entry_id": "entry-stats", "sha256": "a" * 64,
                      "authority": "canonical", "rights": "public",
                      "source_id": "fixture_stats"}],
            target_domain="other_target", capabilities=["stats"],
            build_provenance="simulated-build-1",
            build_time="2026-10-28T00:00:00+00:00",
            data_release="SIMULATED_DATA_RELEASE_1",
            engine_candidate="SIMULATED_ENGINE_CANDIDATE_1",
            build_code="SIMULATED_BUILD_CODE_1")
        store = SimulatedCurrentStore(target_domain="fixture_target")
        store.register(other)
    except AttributeError:
        store = SimulatedCurrentStore(target_domain="fixture_target")
        store.stage("kc-snapshot:other-target")
        store.validate()
        assert False, "pre-repair validate never checks target domain"
    store.stage(other.snapshot_id)
    with pytest.raises(KcCurrentError):
        store.validate()
    assert store.state == "FAILED"


def test_validate_without_any_registry_fails_closed():
    store = SimulatedCurrentStore(target_domain="fixture_target")
    store.stage("kc-snapshot:v1")
    with pytest.raises(KcCurrentError):
        store.validate()
    assert store.state == "FAILED"
    with pytest.raises(KcCurrentError):
        store.commit()
