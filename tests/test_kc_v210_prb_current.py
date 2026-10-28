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
    store = SimulatedCurrentStore(target_domain="fixture_target")
    store.stage(_snap_id("v1"))
    assert store.state == "STAGED"
    store.validate()
    assert store.state == "VALIDATED"
    receipt = store.commit()
    assert store.state == "CURRENT"
    assert store.read_current() == _snap_id("v1")
    assert receipt["snapshot_id"] == _snap_id("v1")
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
