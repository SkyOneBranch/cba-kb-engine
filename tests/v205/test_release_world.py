"""T2-6 tests: release_world AC-31~38 (CW-01), Issue #200.

All fixtures are SYNTHETIC. No real private data (AC-29).
Each new test was verified to FAIL on the unmodified base (import error)
before the implementation was written (falsification).
"""
import json

import pytest

from cba_kb import release_world as rw


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------

COMPLETE_STATUS = {
    "current_release_id": "v9.9.9-1",
    "state": "COMPLETE",
    "pending_release_id": None,
    "previous_release_id": "v9.9.8-2",
    "previous_snapshot": [
        {"release_id": "v9.9.8-2", "state": "COMPLETE"},
        {"release_id": "v9.9.7-1", "state": "COMPLETE"},
    ],
    "code_commit": "aaabbbcccdddeeefff0001112223334445556666",
}

PUBLISHING_STATUS = {
    "current_release_id": "v9.9.9-1",
    "state": "PUBLISHING",
    "pending_release_id": "v9.9.9-2",
    "previous_release_id": "v9.9.8-2",
    "previous_snapshot": [
        {"release_id": "v9.9.8-2", "state": "COMPLETE"},
        {"release_id": "v9.9.7-1", "state": "COMPLETE"},
    ],
    "code_commit": "aaabbbcccdddeeefff0001112223334445556666",
}

FAILED_STATUS = dict(PUBLISHING_STATUS, state="FAILED")


def _raw(doc):
    return json.dumps(doc).encode("utf-8")


BINDING = {
    "file_id": "synth-file-id-001",
    "revision": "42",
    "read_timestamp": "2026-10-18T14:00:00Z",
}


# ---------------------------------------------------------------------------
# AC-31: live release_status.json binding
# ---------------------------------------------------------------------------

class TestAC31BindReleaseStatus:
    def test_complete_status_binds_evidence(self):
        raw = _raw(COMPLETE_STATUS)
        out = rw.bind_release_status(raw, file_id=BINDING["file_id"],
                                     revision=BINDING["revision"],
                                     read_timestamp=BINDING["read_timestamp"])
        assert out["verdict"] == "OK"
        dp = out["data_production"]
        assert dp["release_id"] == "v9.9.9-1"
        assert dp["state"] == "COMPLETE"
        assert dp["evidence"]["file_id"] == "synth-file-id-001"
        assert dp["evidence"]["revision"] == "42"
        assert len(dp["evidence"]["sha256"]) == 64
        assert dp["evidence"]["read_timestamp"] == "2026-10-18T14:00:00Z"

    def test_sha256_mismatch_is_blocked(self):
        raw = _raw(COMPLETE_STATUS)
        out = rw.bind_release_status(raw, file_id="x", read_timestamp="t",
                                     sha256="0" * 64)
        assert out["verdict"] == "BLOCKED/NOT_VERIFIED"
        assert "sha256" in out["reason"]

    def test_missing_bytes_is_blocked_not_guessed(self):
        out = rw.bind_release_status(None, file_id="x", read_timestamp="t")
        assert out["verdict"] == "BLOCKED/NOT_VERIFIED"

    def test_missing_file_id_is_blocked(self):
        out = rw.bind_release_status(_raw(COMPLETE_STATUS), file_id="",
                                     read_timestamp="t")
        assert out["verdict"] == "BLOCKED/NOT_VERIFIED"

    def test_duplicate_keys_rejected(self):
        raw = b'{"state": "COMPLETE", "state": "FAILED"}'
        out = rw.bind_release_status(raw, file_id="x", read_timestamp="t")
        assert out["verdict"] == "BLOCKED/NOT_VERIFIED"
        assert "duplicate" in out["reason"]

    def test_unparseable_bytes_blocked(self):
        out = rw.bind_release_status(b"\x00\x01 not json", file_id="x",
                                     read_timestamp="t")
        assert out["verdict"] == "BLOCKED/NOT_VERIFIED"


# ---------------------------------------------------------------------------
# AC-32: four tracks presented independently
# ---------------------------------------------------------------------------

class TestAC32FourTracks:
    def _tracks(self):
        return rw.present_tracks(
            data_production_release={"value": "v9.9.9-1", "source": "release_status.json"},
            engine_release_tag={"value": "v9.9.4-2", "source": "github_release"},
            engine_runtime_sha={"value": "aaabbbccc", "source": "deployed_sha"},
            python_package_version={"value": "9.9.2", "source": "package_metadata"},
            consumer_manifest_release={"value": "v9.9.4-2", "source": "consumer_manifest"},
            consumer_data_batch={"value": "batch-77", "source": "consumer_manifest"},
        )

    def test_all_six_tracks_present_with_sources(self):
        out = self._tracks()
        for key in ("data_production_release", "engine_release_tag",
                    "engine_runtime_sha", "python_package_version",
                    "consumer_manifest_release", "consumer_data_batch"):
            assert out["tracks"][key]["source"], key

    def test_same_version_string_not_conflated(self):
        # engine_release_tag and consumer_manifest_release share "v9.9.4-2"
        # but must remain separate, source-labelled tracks.
        out = self._tracks()
        assert out["tracks"]["engine_release_tag"]["value"] == \
               out["tracks"]["consumer_manifest_release"]["value"]
        assert out["tracks"]["engine_release_tag"]["source"] != \
               out["tracks"]["consumer_manifest_release"]["source"]
        assert "no equality inferred" in out["note"]

    def test_missing_track_is_not_verified_not_guessed(self):
        out = rw.present_tracks(
            data_production_release={"value": "v9.9.9-1", "source": "s"},
            engine_release_tag=None, engine_runtime_sha=None,
            python_package_version=None, consumer_manifest_release=None,
            consumer_data_batch=None)
        assert out["tracks"]["engine_release_tag"]["value"] == "NOT_VERIFIED"


# ---------------------------------------------------------------------------
# AC-33: release_status judges only its own artifacts
# ---------------------------------------------------------------------------

class TestAC33Scope:
    def test_old_data_release_does_not_block_authorized_document(self):
        out = rw.judge_consumability(
            "private_document",
            manifest_ok=True, materialized=True,
            rights_ok=True, provenance_ok=True,
            data_production_release_id="v9.9.9-1")  # old release, irrelevant
        assert out["verdict"] == "CONSUMABLE"

    def test_missing_rights_blocks_with_reason(self):
        out = rw.judge_consumability(
            "private_stats",
            manifest_ok=True, materialized=True,
            rights_ok=False, provenance_ok=True,
            data_production_release_id="v9.9.9-1")
        assert out["verdict"] == "NOT_CONSUMABLE"
        assert "rights" in out["reason"]

    def test_data_production_current_uses_release_state(self):
        out = rw.judge_consumability(
            "data_production_current",
            manifest_ok=True, materialized=True,
            rights_ok=True, provenance_ok=True,
            release_state="COMPLETE",
            data_production_release_id="v9.9.9-1")
        assert out["verdict"] == "CONSUMABLE"

    def test_unknown_artifact_kind_is_not_verified(self):
        out = rw.judge_consumability("mystery_kind", manifest_ok=True,
                                     materialized=True, rights_ok=True,
                                     provenance_ok=True,
                                     data_production_release_id="v1")
        assert out["verdict"] == "NOT_VERIFIED"


# ---------------------------------------------------------------------------
# AC-34: per-capability status
# ---------------------------------------------------------------------------

class TestAC34CapabilityStatus:
    def test_materialized(self):
        e = rw.capability_status("documents", status="MATERIALIZED",
                                 batch_ref="batch-77")
        assert e["status"] == "MATERIALIZED"

    def test_not_materialized_requires_reason(self):
        e = rw.capability_status("person", status="NOT_MATERIALIZED",
                                 reason="not_yet_indexed", batch_ref="batch-77")
        assert e["status"] == "NOT_MATERIALIZED"
        assert e["reason"] == "not_yet_indexed"

    def test_not_materialized_without_reason_fails_closed(self):
        with pytest.raises(ValueError):
            rw.capability_status("person", status="NOT_MATERIALIZED")

    def test_unavailable(self):
        e = rw.capability_status("claim", status="UNAVAILABLE",
                                 reason="no_source")
        assert e["status"] == "UNAVAILABLE"

    def test_unknown_status_rejected(self):
        with pytest.raises(ValueError):
            rw.capability_status("documents", status="AVAILABLE")

    def test_no_cross_capability_inference(self):
        # documents MATERIALIZED must not imply stats MATERIALIZED:
        # each capability is judged only on its own evidence.
        docs = rw.capability_status("documents", status="MATERIALIZED",
                                    batch_ref="batch-77")
        stats = rw.capability_status("stats", status="NOT_MATERIALIZED",
                                     reason="not_yet_computed")
        assert docs["status"] == "MATERIALIZED"
        assert stats["status"] == "NOT_MATERIALIZED"


# ---------------------------------------------------------------------------
# AC-35: last-safe read on transitional states
# ---------------------------------------------------------------------------

class TestAC35LastSafe:
    def test_complete_reads_current(self):
        out = rw.last_safe_read(COMPLETE_STATUS)
        assert out["mode"] == "current"
        assert out["release_id"] == "v9.9.9-1"

    def test_publishing_reads_last_safe_not_pending(self):
        out = rw.last_safe_read(PUBLISHING_STATUS)
        assert out["mode"] == "last_safe"
        assert out["release_id"] == "v9.9.8-2"
        assert out["pending_ignored"] == "v9.9.9-2"

    def test_failed_reads_last_safe(self):
        out = rw.last_safe_read(FAILED_STATUS)
        assert out["mode"] == "last_safe"
        assert out["release_id"] == "v9.9.8-2"

    def test_malformed_snapshot_fails_closed(self):
        bad = dict(PUBLISHING_STATUS, previous_snapshot="not-a-list")
        out = rw.last_safe_read(bad)
        assert out["verdict"] == "NOT_VERIFIED"
        assert "snapshot" in out["reason"]

    def test_empty_snapshot_fails_closed(self):
        bad = dict(PUBLISHING_STATUS, previous_snapshot=[])
        out = rw.last_safe_read(bad)
        assert out["verdict"] == "NOT_VERIFIED"

    def test_unknown_state_is_not_verified(self):
        bad = dict(COMPLETE_STATUS, state="SOMETHING_ELSE")
        out = rw.last_safe_read(bad)
        assert out["verdict"] == "NOT_VERIFIED"


# ---------------------------------------------------------------------------
# AC-36: CURRENT_STATE_CONSISTENCY per surface
# ---------------------------------------------------------------------------

class TestAC36SurfaceConsistency:
    def _binding(self, source, track, **kw):
        b = {"source": source, "track": track, "release_id": "v9.9.4-2",
             "code_commit": "aaabbb", "manifest_binding": "m-1", "stale": False}
        b.update(kw)
        return b

    def test_all_equal_passes(self):
        out = rw.check_surface_consistency("documents", [
            self._binding("live_status", "data_production"),
            self._binding("consumer_manifest", "data_production"),
            self._binding("context_card", "data_production"),
        ])
        assert out["verdict"] == "PASS"

    def test_cross_track_legal_difference_is_explicitly_bound(self):
        out = rw.check_surface_consistency("documents", [
            self._binding("live_status", "data_production"),
            self._binding("engine_release", "engine", release_id="v9.9.4-2",
                          code_commit="dddeee", manifest_binding="m-2"),
        ])
        assert out["verdict"] == "EXPLICITLY_BOUND"
        assert "not forced equal" in out["note"]

    def test_same_track_conflict_is_contract_conflict(self):
        out = rw.check_surface_consistency("documents", [
            self._binding("live_status", "data_production"),
            self._binding("consumer_manifest", "data_production",
                          code_commit="zzz999"),
        ])
        assert out["verdict"] == "CONTRACT_CONFLICT"

    def test_stale_binding_fails(self):
        out = rw.check_surface_consistency("documents", [
            self._binding("live_status", "data_production"),
            self._binding("context_card", "data_production", stale=True),
        ])
        assert out["verdict"] == "FAIL"
        assert "stale" in out["reason"]

    def test_incomplete_binding_is_not_verified(self):
        out = rw.check_surface_consistency("documents", [
            {"source": "live_status", "track": "data_production"},
        ])
        assert out["verdict"] == "NOT_VERIFIED"


# ---------------------------------------------------------------------------
# AC-37: no premature AVAILABLE/MATERIALIZED
# ---------------------------------------------------------------------------

class TestAC37BatchGate:
    def _ok(self, **kw):
        args = {"expected_manifest_sha": "a" * 64,
                "observed_manifest_sha": "a" * 64,
                "required_artifacts": ["doc-1", "doc-2"],
                "artifacts_present": ["doc-1", "doc-2"],
                "silent_truncation": 0,
                "third_party_read": True}
        args.update(kw)
        return rw.gate_new_batch(**args)

    def test_full_match_allows_materialized(self):
        assert self._ok()["verdict"] == "MATERIALIZED_ALLOWED"

    def test_manifest_mismatch_refused(self):
        out = self._ok(observed_manifest_sha="b" * 64)
        assert out["verdict"] == "REFUSED"
        assert "manifest" in out["reason"]

    def test_incomplete_artifacts_refused(self):
        out = self._ok(artifacts_present=["doc-1"])
        assert out["verdict"] == "REFUSED"

    def test_silent_truncation_refused(self):
        out = self._ok(silent_truncation=3)
        assert out["verdict"] == "REFUSED"
        assert "truncation" in out["reason"]

    def test_third_party_unread_is_external_pending(self):
        out = self._ok(third_party_read=False)
        assert out["verdict"] == "EXTERNAL_PENDING"


# ---------------------------------------------------------------------------
# AC-38: zero-diff guard
# ---------------------------------------------------------------------------

class TestAC38ZeroDiffGuard:
    def test_tag_rewrite_refused(self):
        out = rw.zero_diff_guard([
            {"target": "git_tag", "kind": "rewrite", "detail": "v2.0.4-2"}])
        assert out["verdict"] == "ALL_REFUSED"

    def test_forced_release_status_version_refused(self):
        out = rw.zero_diff_guard([
            {"target": "release_status", "kind": "force_version",
             "detail": "write v2.0.5"}])
        assert out["verdict"] == "ALL_REFUSED"

    def test_old_data_mutation_refused(self):
        out = rw.zero_diff_guard([
            {"target": "data_production_old", "kind": "mutate",
             "detail": "backfill"}])
        assert out["verdict"] == "ALL_REFUSED"

    def test_unknown_target_fails_closed(self):
        out = rw.zero_diff_guard([
            {"target": "something_new", "kind": "write", "detail": "?"}])
        assert out["verdict"] == "ALL_REFUSED"

    def test_read_only_evaluation_allowed(self):
        out = rw.zero_diff_guard([
            {"target": "evaluation_output", "kind": "read",
             "detail": "current_world view"}])
        assert out["verdict"] == "ALL_ALLOWED"

    def test_empty_proposal_list_allowed(self):
        assert rw.zero_diff_guard([])["verdict"] == "ALL_ALLOWED"


# ---------------------------------------------------------------------------
# Output shape: v2 §4.1 conceptual view
# ---------------------------------------------------------------------------

class TestOutputShape:
    def test_build_current_world_shape(self):
        dp = rw.bind_release_status(
            _raw(COMPLETE_STATUS), file_id="f", read_timestamp="t")
        world = rw.build_current_world(
            data_production=dp["data_production"],
            engine={"release_tag": {"value": "v9.9.4-2", "source": "github"},
                    "runtime_code_sha": {"value": "aaabbb", "source": "deploy"}},
            consumer={"manifest_release_id": {"value": "v9.9.4-2",
                                              "source": "manifest"},
                      "capabilities": {"documents": rw.capability_status(
                          "documents", status="MATERIALIZED",
                          batch_ref="batch-77")}},
            reconciliation={"same_surface_consistency": "PASS",
                            "separate_release_tracks": "EXPLICITLY_BOUND"})
        cw = world["current_world"]
        assert set(cw) == {"data_production", "engine", "consumer",
                           "reconciliation"}
        assert world["spec_id"] == "Q1-V205-UNIFIED-REALDATA-CANARY-v2"
        assert cw["data_production"]["release_id"] == "v9.9.9-1"
