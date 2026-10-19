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
        # Updated post-Q2-B5: EXPLICITLY_BOUND now requires demonstrable
        # binding_evidence per binding; the old version auto-legitimized
        # any cross-track difference (fail-open).
        out = rw.check_surface_consistency("documents", [
            self._binding("live_status", "data_production",
                          binding_evidence="release_status readback 2026-10-18"),
            self._binding("engine_release", "engine", release_id="v9.9.4-2",
                          code_commit="dddeee", manifest_binding="m-2",
                          binding_evidence="github release readback 2026-10-18"),
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
    def _receipt(self, **kw):
        # Trusted-upstream evidence receipt (synthetic): structured, bound.
        # This is a UNIT-TEST stand-in proving the algorithm; real consumer
        # readback receipts are produced by the trusted upstream in the
        # applicable phase.
        r = {"source": "consumer-drive-readback",
             "target": "consumer-docs",
             "sha": "a" * 64,
             "read_id": "read-001",
             "read_timestamp": "2026-10-18T15:00:00Z"}
        r.update(kw)
        return r

    def _inventory(self, **kw):
        inv = {"source": "consumer-drive-readback",
               "target": "consumer-docs",
               "artifacts": ["doc-1", "doc-2"],
               "read_id": "inv-001",
               "read_timestamp": "2026-10-18T15:00:00Z"}
        inv.update(kw)
        return inv

    def _ok(self, **kw):
        args = {"expected_manifest_sha": "a" * 64,
                "observed_manifest_sha": "a" * 64,
                "expected_manifest_evidence": self._receipt(read_id="read-001"),
                "observed_manifest_evidence": self._receipt(read_id="read-002"),
                "required_artifacts": ["doc-1", "doc-2"],
                "artifact_inventory": self._inventory(),
                "silent_truncation": 0,
                "third_party_read": True,
                "consumer_target": "consumer-docs"}
        args.update(kw)
        return rw.gate_new_batch(**args)

    def test_full_match_allows_materialized(self):
        assert self._ok()["verdict"] == "MATERIALIZED_ALLOWED"

    def test_manifest_mismatch_refused(self):
        out = self._ok(observed_manifest_sha="b" * 64)
        assert out["verdict"] == "REFUSED"
        assert "manifest" in out["reason"]

    def test_incomplete_artifacts_refused(self):
        out = self._ok(required_artifacts=["doc-1", "ghost"])
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
# Adversarial regression tests (Q2-FAIL B1~B7 attack rehearsal)
#
# Each test replicates the independent reviewer's EXACT counterexample.
# They were written BEFORE the fix and confirmed to FAIL on the unfixed
# code (demonstrating the fail-open defect), then confirmed to PASS after.
# Synthetic fixtures only. No real data.
# ---------------------------------------------------------------------------

class TestAdversarialB1:
    def test_empty_dict_with_file_id_is_blocked_not_ok(self):
        # Reviewer B1: bind_release_status accepts {} + any file_id as OK.
        out = rw.bind_release_status(b"{}", file_id="any-file-id",
                                     read_timestamp="2026-10-18T14:00:00Z")
        assert out["verdict"] == "BLOCKED/NOT_VERIFIED"

    def test_malformed_timestamp_is_blocked(self):
        # Reviewer B1: read_timestamp="not-a-timestamp" must be rejected.
        out = rw.bind_release_status(_raw(COMPLETE_STATUS), file_id="x",
                                     read_timestamp="not-a-timestamp")
        assert out["verdict"] == "BLOCKED/NOT_VERIFIED"
        assert "timestamp" in out["reason"]

    def test_missing_release_id_is_blocked(self):
        doc = dict(COMPLETE_STATUS)
        del doc["current_release_id"]
        out = rw.bind_release_status(_raw(doc), file_id="x",
                                     read_timestamp="2026-10-18T14:00:00Z")
        assert out["verdict"] == "BLOCKED/NOT_VERIFIED"

    def test_missing_state_is_blocked(self):
        doc = dict(COMPLETE_STATUS)
        del doc["state"]
        out = rw.bind_release_status(_raw(doc), file_id="x",
                                     read_timestamp="2026-10-18T14:00:00Z")
        assert out["verdict"] == "BLOCKED/NOT_VERIFIED"

    def test_revision_availability_is_distinguished(self):
        # Reviewer B1: genuinely-available revision vs missing revision (None).
        with_rev = rw.bind_release_status(
            _raw(COMPLETE_STATUS), file_id="x",
            read_timestamp="2026-10-18T14:00:00Z", revision="42")
        assert with_rev["verdict"] == "OK"
        assert with_rev["data_production"]["evidence"]["revision_status"] == "BOUND"
        without_rev = rw.bind_release_status(
            _raw(COMPLETE_STATUS), file_id="x",
            read_timestamp="2026-10-18T14:00:00Z", revision=None)
        assert without_rev["verdict"] == "OK"
        assert without_rev["data_production"]["evidence"]["revision_status"] == "NOT_AVAILABLE"
        assert without_rev["data_production"]["evidence"]["revision"] is None


class TestAdversarialB2:
    def test_complete_with_none_release_id_is_not_consumable(self):
        # Reviewer B2: state=COMPLETE + release_id=None must NOT be CONSUMABLE.
        out = rw.judge_consumability(
            "data_production_current",
            manifest_ok=True, materialized=True,
            rights_ok=True, provenance_ok=True,
            release_state="COMPLETE",
            data_production_release_id=None)
        assert out["verdict"] != "CONSUMABLE"

    def test_complete_with_empty_release_id_is_not_consumable(self):
        out = rw.judge_consumability(
            "data_production_current",
            manifest_ok=True, materialized=True,
            rights_ok=True, provenance_ok=True,
            release_state="COMPLETE",
            data_production_release_id="")
        assert out["verdict"] != "CONSUMABLE"


class TestAdversarialB3:
    def test_materialized_without_evidence_is_refused(self):
        # Reviewer B3: capability_status("documents", status="MATERIALIZED")
        # with no evidence must not return MATERIALIZED.
        with pytest.raises(ValueError):
            rw.capability_status("documents", status="MATERIALIZED")

    def test_materialized_with_batch_ref_is_allowed(self):
        e = rw.capability_status("documents", status="MATERIALIZED",
                                 batch_ref="batch-77")
        assert e["status"] == "MATERIALIZED"
        assert e["batch_or_catalog_ref"] == "batch-77"


class TestAdversarialB4:
    def test_unsafe_snapshot_entry_is_not_trusted(self):
        # Reviewer B4: previous_snapshot[0] in FAILED state must not be
        # returned as last-safe.
        bad = dict(PUBLISHING_STATUS)
        bad["previous_snapshot"] = [
            {"release_id": "v9.9.8-2", "state": "FAILED"},
            {"release_id": "v9.9.7-1", "state": "COMPLETE"},
        ]
        out = rw.last_safe_read(bad)
        # Either NOT_VERIFIED, or last_safe pointing at the safe entry --
        # never last_safe pointing at the FAILED entry.
        assert not (out.get("mode") == "last_safe"
                    and out.get("release_id") == "v9.9.8-2"), \
            "must not trust an unsafe snapshot entry"

    def test_snapshot_pointer_mismatch_fails_closed(self):
        # Reviewer B4: snapshot entries must be pointer-consistent with
        # previous_release_id.
        bad = dict(PUBLISHING_STATUS)
        bad["previous_release_id"] = "v9.9.0-0"  # does not match snapshot
        out = rw.last_safe_read(bad)
        assert out.get("verdict") == "NOT_VERIFIED" or \
               out.get("release_id") == "v9.9.0-0", \
            "pointer inconsistency must fail closed, never silently use [0]"

    def test_entry_missing_release_id_fails_closed(self):
        bad = dict(PUBLISHING_STATUS)
        bad["previous_snapshot"] = [{"state": "COMPLETE"}, {"nope": 1}]
        out = rw.last_safe_read(bad)
        assert out.get("verdict") == "NOT_VERIFIED"


class TestAdversarialB4Compatibility:
    """Reconciliation with current_state.consumer_artifacts() semantics."""

    def test_transitional_agrees_on_snapshot_source(self):
        from cba_kb import current_state as cs
        # consumer_artifacts() returns the snapshot list on transitional states.
        snaps = cs.consumer_artifacts(PUBLISHING_STATUS)
        assert isinstance(snaps, list) and len(snaps) == 2
        # last_safe_read() must pick its release_id from that same snapshot,
        # validated, never the pending candidate.
        out = rw.last_safe_read(PUBLISHING_STATUS)
        assert out["mode"] == "last_safe"
        assert out["release_id"] in {
            e.get("release_id") for e in snaps if isinstance(e, dict)}
        assert out["release_id"] != PUBLISHING_STATUS["pending_release_id"]

    def test_complete_agrees_on_current_source(self):
        from cba_kb import current_state as cs
        status = dict(COMPLETE_STATUS, artifacts=["a1"])
        assert cs.consumer_artifacts(status) == ["a1"]
        out = rw.last_safe_read(status)
        assert out["mode"] == "current"
        assert out["release_id"] == "v9.9.9-1"

    def test_both_fail_closed_on_missing_snapshot(self):
        from cba_kb import current_state as cs
        bad = dict(PUBLISHING_STATUS, previous_snapshot=[])
        with pytest.raises(ValueError):
            cs.consumer_artifacts(bad)
        out = rw.last_safe_read(bad)
        assert out["verdict"] == "NOT_VERIFIED"


class TestAdversarialB5:
    def _binding(self, **kw):
        b = {"source": "live_status", "track": "data_production",
             "release_id": "v9.9.4-2", "code_commit": "aaabbb",
             "manifest_binding": "m-1", "stale": False}
        b.update(kw)
        return b

    def test_empty_identity_values_rejected(self):
        # Reviewer B5: binding keys present but values empty/None must not PASS.
        out = rw.check_surface_consistency("documents", [
            self._binding(release_id=""),
            self._binding(code_commit=None),
        ])
        assert out["verdict"] in ("NOT_VERIFIED", "FAIL", "CONTRACT_CONFLICT")
        assert out["verdict"] != "PASS"

    def test_cross_track_difference_without_evidence_not_legitimized(self):
        # Reviewer B5: cross-track difference must not auto-legitimize to
        # EXPLICITLY_BOUND without demonstrable binding evidence.
        out = rw.check_surface_consistency("documents", [
            self._binding(source="live_status", track="data_production"),
            self._binding(source="engine_release", track="engine",
                          release_id="v9.9.4-2", code_commit="dddeee",
                          manifest_binding="m-2"),
        ])
        assert out["verdict"] != "EXPLICITLY_BOUND", \
            "cross-track difference without binding evidence must not be legitimized"

    def test_cross_track_difference_with_evidence_is_explicitly_bound(self):
        out = rw.check_surface_consistency("documents", [
            self._binding(source="live_status", track="data_production",
                          binding_evidence="release_status readback 2026-10-18"),
            self._binding(source="engine_release", track="engine",
                          release_id="v9.9.4-2", code_commit="dddeee",
                          manifest_binding="m-2",
                          binding_evidence="github release readback 2026-10-18"),
        ])
        assert out["verdict"] == "EXPLICITLY_BOUND"


class TestAdversarialB6:
    def _receipt(self, **kw):
        r = {"source": "consumer-drive-readback",
             "target": "consumer-docs",
             "sha": "a" * 64,
             "read_id": "read-001",
             "read_timestamp": "2026-10-18T15:00:00Z"}
        r.update(kw)
        return r

    def _inventory(self, **kw):
        inv = {"source": "consumer-drive-readback",
               "target": "consumer-docs",
               "artifacts": ["doc-1"],
               "read_id": "inv-001",
               "read_timestamp": "2026-10-18T15:00:00Z"}
        inv.update(kw)
        return inv

    def _args(self, **kw):
        args = {"expected_manifest_sha": "a" * 64,
                "observed_manifest_sha": "a" * 64,
                "expected_manifest_evidence": self._receipt(read_id="read-001"),
                "observed_manifest_evidence": self._receipt(read_id="read-002"),
                "required_artifacts": ["doc-1"],
                "artifact_inventory": self._inventory(),
                "silent_truncation": 0,
                "third_party_read": True,
                "consumer_target": "consumer-docs"}
        args.update(kw)
        return args

    def test_empty_required_artifacts_rejected(self):
        # Reviewer B6: empty required_artifacts=[] must be rejected,
        # never MATERIALIZED_ALLOWED.
        out = rw.gate_new_batch(**self._args(required_artifacts=[]))
        assert out["verdict"] == "REFUSED"

    def test_self_reported_hashes_without_evidence_rejected(self):
        # Reviewer B6: manifest SHAs without structured evidence receipts
        # must be rejected (fail closed).
        # R4 semantics: ABSENT upstream evidence -> NOT_VERIFIED (not yet
        # established); MALFORMED evidence -> REFUSED (actively wrong).
        # Both are fail-closed; neither is MATERIALIZED_ALLOWED.
        args = self._args()
        del args["expected_manifest_evidence"]
        del args["observed_manifest_evidence"]
        out = rw.gate_new_batch(**args)
        assert out["verdict"] in ("REFUSED", "NOT_VERIFIED")
        assert out["verdict"] != "MATERIALIZED_ALLOWED"
        assert "evidence" in out["reason"]

    def test_independently_established_manifest_allowed(self):
        # Updated post-Q2-R2-NEW-1: "independently established" now means
        # structured, bound, independent-read receipts (not bare strings).
        out = rw.gate_new_batch(**self._args())
        assert out["verdict"] == "MATERIALIZED_ALLOWED"


class TestAdversarialB7:
    def test_write_to_evaluation_output_with_hostile_detail_denied(self):
        # Reviewer B7 EXACT counterexample: must be DENIED, not ALLOWED.
        out = rw.zero_diff_guard([
            {"target": "evaluation_output", "kind": "write",
             "detail": "overwrite protected history"}])
        assert out["verdict"] == "ALL_REFUSED"

    def test_mutating_kind_on_non_designated_target_denied(self):
        out = rw.zero_diff_guard([
            {"target": "evaluation_output", "kind": "delete",
             "detail": "cleanup"}])
        assert out["verdict"] == "ALL_REFUSED"

    def test_read_only_evaluation_still_allowed(self):
        out = rw.zero_diff_guard([
            {"target": "evaluation_output", "kind": "read",
             "detail": "current_world view"}])
        assert out["verdict"] == "ALL_ALLOWED"


# ---------------------------------------------------------------------------
# Output shape: v2 §4.1 conceptual view
# ---------------------------------------------------------------------------

class TestOutputShape:
    def test_build_current_world_shape(self):
        # Updated post-Q2-B1: read_timestamp must be a valid ISO-8601
        # timestamp; the old "t" placeholder no longer binds.
        dp = rw.bind_release_status(
            _raw(COMPLETE_STATUS), file_id="f",
            read_timestamp="2026-10-18T14:00:00Z")
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


# ---------------------------------------------------------------------------
# Q2-R2定向修复：NEW-1 / NEW-2 对抗回归测试（先红后绿）
#
# NEW-1 (AC-37): gate_new_batch 不得接受未验证的调用方声明作为材料化证据。
#   不变量：MATERIALIZED_ALLOWED 不得来源于未验证的调用方声明。
# NEW-2 (AC-38): zero_diff_guard 不得授权未经独立授权的状态变更。
#   不变量：ALL_ALLOWED 不得包含未经独立授权的状态变更。
#
# 这些测试在 e8aa422 上先 FAIL（记录真实输出），修复后 PASS。
# 合成 fixture。无真实数据。
# ---------------------------------------------------------------------------

class TestAdversarialNEW1:
    """NEW-1: AC-37 batch gate must not trust caller assertions."""

    def _receipt(self, **kw):
        r = {"source": "consumer-drive-readback",
             "target": "consumer-docs",
             "sha": "a" * 64,
             "read_id": "read-001",
             "read_timestamp": "2026-10-18T15:00:00Z"}
        r.update(kw)
        return r

    def _inventory(self, **kw):
        inv = {"source": "consumer-drive-readback",
               "target": "consumer-docs",
               "artifacts": ["doc-1", "doc-2"],
               "read_id": "inv-001",
               "read_timestamp": "2026-10-18T15:00:00Z"}
        inv.update(kw)
        return inv

    def _args(self, **kw):
        args = {"expected_manifest_sha": "a" * 64,
                "observed_manifest_sha": "a" * 64,
                "expected_manifest_evidence": self._receipt(read_id="read-001"),
                "observed_manifest_evidence": self._receipt(read_id="read-002"),
                "required_artifacts": ["doc-1", "doc-2"],
                "artifact_inventory": self._inventory(),
                "silent_truncation": 0,
                "third_party_read": True,
                "consumer_target": "consumer-docs"}
        args.update(kw)
        return rw.gate_new_batch(**args)

    def test_pseudo_hash_x_rejected(self):
        # 负例1: "x"/"x" 伪哈希 -> 格式非法，拒绝。
        out = self._args(expected_manifest_sha="x", observed_manifest_sha="x")
        assert out["verdict"] == "REFUSED"

    def test_trust_me_evidence_rejected(self):
        # 负例2: "trust me" 自报证据 -> 非结构化，拒绝。
        out = self._args(expected_manifest_evidence="trust me",
                         observed_manifest_evidence="trust me")
        assert out["verdict"] == "REFUSED"

    def test_valid_sha_without_binding_rejected(self):
        # 负例3: 格式合法但 receipt 未绑定到被比较的 SHA -> 拒绝。
        out = self._args(
            expected_manifest_evidence=self._receipt(sha="b" * 64,
                                                     read_id="read-001"),
            observed_manifest_evidence=self._receipt(sha="b" * 64,
                                                     read_id="read-002"))
        assert out["verdict"] == "REFUSED"

    def test_same_receipt_twice_rejected(self):
        # 变体: 同一份 receipt 用两次（非独立读取）-> 拒绝。
        r = self._receipt(read_id="read-001")
        out = self._args(expected_manifest_evidence=r,
                         observed_manifest_evidence=dict(r))
        assert out["verdict"] == "REFUSED"

    def test_ghost_document_rejected(self):
        # 负例4: ghost-document 虚构工件 -> 无可达性证明，拒绝。
        out = self._args(required_artifacts=["ghost-document"])
        assert out["verdict"] == "REFUSED"

    def test_evidence_target_mismatch_rejected(self):
        # 负例5: Manifest 与消费端目标不匹配 -> 拒绝。
        out = self._args(
            expected_manifest_evidence=self._receipt(target="other-target",
                                                     read_id="read-001"))
        assert out["verdict"] == "REFUSED"

    def test_third_party_unread_not_allowed(self):
        # 负例6: 第三方未实际读回 -> 不可是 MATERIALIZED_ALLOWED。
        out = self._args(third_party_read=False)
        assert out["verdict"] != "MATERIALIZED_ALLOWED"
        assert out["verdict"] in ("EXTERNAL_PENDING", "NOT_VERIFIED")

    def test_bound_independent_receipts_allow(self):
        # 正向: 结构化、已绑定、独立读取的可信上游证据 -> 允许。
        # 注意：这是合成单元测试，证明算法；真实消费端核验在适用阶段。
        out = self._args()
        assert out["verdict"] == "MATERIALIZED_ALLOWED"


class TestAdversarialNEW2:
    """NEW-2: AC-38 zero-diff guard must not authorize state changes."""

    def test_new_data_release_delete_refused(self):
        # 负例1: new_data_release + delete -> 拒绝。
        out = rw.zero_diff_guard([
            {"target": "new_data_release", "kind": "delete",
             "detail": "remove archived records"}])
        assert out["verdict"] == "ALL_REFUSED"

    def test_new_data_release_overwrite_refused(self):
        # 负例2: new_data_release + overwrite -> 拒绝。
        out = rw.zero_diff_guard([
            {"target": "new_data_release", "kind": "overwrite",
             "detail": "replace release"}])
        assert out["verdict"] == "ALL_REFUSED"

    def test_read_labeled_write_refused(self):
        # 负例3: read 标签掩盖写入语义 -> 拒绝，不能 ALL_ALLOWED。
        out = rw.zero_diff_guard([
            {"target": "evaluation_output", "kind": "read",
             "detail": "remove archived records"}])
        assert out["verdict"] == "ALL_REFUSED"

    def test_unknown_kind_refused(self):
        # 负例4: 未知操作类型 -> 拒绝。
        out = rw.zero_diff_guard([
            {"target": "evaluation_output", "kind": "frobnicator",
             "detail": "?"}])
        assert out["verdict"] == "ALL_REFUSED"

    def test_readonly_target_history_mutation_refused(self):
        # 负例5: 只读目标搭配修改历史的声明 -> 拒绝。
        out = rw.zero_diff_guard([
            {"target": "evaluation_output", "kind": "read",
             "detail": "rewrite release_status.json history"}])
        assert out["verdict"] == "ALL_REFUSED"

    def test_historical_mutation_attempts_refused(self):
        # 负例6: 修改旧 tag / 旧 Data Production / 历史 release_status.json。
        cases = [
            {"target": "git_tag", "kind": "rewrite", "detail": "v2.0.4-2"},
            {"target": "data_production_old", "kind": "delete",
             "detail": "purge"},
            {"target": "release_status", "kind": "overwrite",
             "detail": "bump version"},
            {"target": "evaluation_output", "kind": "evaluate",
             "detail": "force_version release_status.json to v99"},
        ]
        for c in cases:
            out = rw.zero_diff_guard([c])
            assert out["verdict"] == "ALL_REFUSED", c

    def test_genuine_readonly_still_allowed(self):
        # 正向: 真正只读的 evaluation_output 操作继续 PASS。
        out = rw.zero_diff_guard([
            {"target": "evaluation_output", "kind": "read",
             "detail": "current_world view"}])
        assert out["verdict"] == "ALL_ALLOWED"

    def test_case_variant_delete_refused(self):
        # 变体: 大小写变体 ("DELETE") -> 拒绝。
        out = rw.zero_diff_guard([
            {"target": "new_data_release", "kind": "DELETE",
             "detail": "cleanup"}])
        assert out["verdict"] == "ALL_REFUSED"


# ---------------------------------------------------------------------------
# R4: NEW-2 adversarial hardening (unhashable kinds, pattern targets,
# expanded refused kinds, normalization, malformed proposals)
# ---------------------------------------------------------------------------

class TestAdversarialNEW2R4:
    """R4: zero_diff_guard structural hardening beyond R2/R3.

    Covers: unhashable kind types (must not crash), non-dict proposals,
    pattern-based protected targets, expanded mutating-kind set,
    strip+lower normalization, missing/empty target/kind.
    """

    def test_unhashable_kind_refused_no_crash(self):
        # R4红线: kind 为 list/dict 时旧代码抛 TypeError；必须干净拒绝。
        for bad_kind in (["read"], {"k": "read"}, ("read",)):
            out = rw.zero_diff_guard([
                {"target": "evaluation_output", "kind": bad_kind}])
            assert out["verdict"] == "ALL_REFUSED", bad_kind

    def test_non_dict_proposal_refused(self):
        # proposal 不是 dict -> 拒绝，不崩溃。
        for bad in (["just a string"], [None], [123]):
            out = rw.zero_diff_guard(bad)
            assert out["verdict"] == "ALL_REFUSED", bad

    def test_pattern_protected_targets_refused(self):
        # 模式匹配的历史目标：任何 kind 都拒绝。
        cases = [
            {"target": "release_status.json", "kind": "read"},
            {"target": "Release_Status.JSON", "kind": "read"},
            {"target": "v2.0.1-1", "kind": "read"},
            {"target": "v2.0.4-2", "kind": "evaluate"},
            {"target": "previous_snapshot", "kind": "read"},
            {"target": "snapshot_history", "kind": "view"},
            {"target": "archive/git_tag_backup", "kind": "read"},
        ]
        for c in cases:
            out = rw.zero_diff_guard([c])
            assert out["verdict"] == "ALL_REFUSED", c

    def test_expanded_refused_kinds(self):
        # 扩展的变异 kind 集：在任何 target 上拒绝。
        for k in ("publish", "remove", "purge", "drop", "erase",
                  "modify", "clear", "reset", "PUBLISH", "Remove"):
            out = rw.zero_diff_guard([
                {"target": "evaluation_output", "kind": k}])
            assert out["verdict"] == "ALL_REFUSED", k
            out = rw.zero_diff_guard([
                {"target": "new_data_release", "kind": k}])
            assert out["verdict"] == "ALL_REFUSED", k

    def test_whitespace_kind_normalized(self):
        # " delete " -> strip+lower -> "delete" -> 显式拒绝（非 fallthrough）。
        out = rw.zero_diff_guard([
            {"target": "new_data_release", "kind": " delete "}])
        assert out["verdict"] == "ALL_REFUSED"
        assert "mutating/ambiguous kind" in out["decisions"][0]["reason"]
        # 合法的 read 加空白 -> 归一化后仍放行。
        out = rw.zero_diff_guard([
            {"target": "  evaluation_output  ", "kind": "  Read  ",
             "detail": "view"}])
        assert out["verdict"] == "ALL_ALLOWED"

    def test_missing_or_empty_target_kind_refused(self):
        cases = [
            {"kind": "read"},                       # 缺 target
            {"target": None, "kind": "read"},        # target None
            {"target": "   ", "kind": "read"},       # target 全空白
            {"target": "evaluation_output"},         # 缺 kind
            {"target": "evaluation_output", "kind": None},
            {"target": "evaluation_output", "kind": ""},
            {"target": "evaluation_output", "kind": "   "},
            {"target": 123, "kind": "read"},         # target 非字符串
        ]
        for c in cases:
            out = rw.zero_diff_guard([c])
            assert out["verdict"] == "ALL_REFUSED", c

    def test_mixed_proposals_never_all_allowed(self):
        # 1 个合法 read + 1 个 delete -> MIXED，绝不 ALL_ALLOWED。
        out = rw.zero_diff_guard([
            {"target": "evaluation_output", "kind": "read"},
            {"target": "new_data_release", "kind": "delete"}])
        assert out["verdict"] == "MIXED"
        assert out["verdict"] != "ALL_ALLOWED"

    def test_detail_deception_variants(self):
        # kind=delete 配无辜 detail -> 仍拒绝（kind 是主要判断）。
        out = rw.zero_diff_guard([
            {"target": "evaluation_output", "kind": "delete",
             "detail": "just reading, definitely not deleting"}])
        assert out["verdict"] == "ALL_REFUSED"
        # 近似只读词配真实写入 detail -> 拒绝。
        for k in ("readonly", "view"):
            out = rw.zero_diff_guard([
                {"target": "evaluation_output", "kind": k,
                 "detail": "delete archived records"}])
            assert out["verdict"] == "ALL_REFUSED", k
        # 超长 detail 不影响只读放行（detail 只是次要信号）。
        out = rw.zero_diff_guard([
            {"target": "evaluation_output", "kind": "read",
             "detail": "x" * 10000}])
        assert out["verdict"] == "ALL_ALLOWED"


# ---------------------------------------------------------------------------
# R4: NEW-1 proof-semantics correction (NOT_VERIFIED without upstream,
# upstream->receipt->judge integration chain)
# ---------------------------------------------------------------------------

class TestAdversarialNEW1R4:
    """R4: trusted-upstream proof semantics.

    No new auth/signature platform. The module validates receipt structure
    + binding; the pipeline establishes upstream trust. Without established
    upstream evidence -> NOT_VERIFIED/EXTERNAL_PENDING, never
    MATERIALIZED_ALLOWED.
    """

    def _receipt(self, **kw):
        r = {"source": "consumer-drive-readback",
             "target": "consumer-docs",
             "sha": "a" * 64,
             "read_id": "read-001",
             "read_timestamp": "2026-10-18T15:00:00Z"}
        r.update(kw)
        return r

    def _inventory(self, **kw):
        inv = {"source": "consumer-drive-readback",
               "target": "consumer-docs",
               "artifacts": ["doc-1", "doc-2"],
               "read_id": "inv-001",
               "read_timestamp": "2026-10-18T15:00:00Z"}
        inv.update(kw)
        return inv

    def _args(self, **kw):
        args = {"expected_manifest_sha": "a" * 64,
                "observed_manifest_sha": "a" * 64,
                "expected_manifest_evidence": self._receipt(read_id="read-001"),
                "observed_manifest_evidence": self._receipt(read_id="read-002"),
                "required_artifacts": ["doc-1", "doc-2"],
                "artifact_inventory": self._inventory(),
                "silent_truncation": 0,
                "third_party_read": True,
                "consumer_target": "consumer-docs"}
        args.update(kw)
        return rw.gate_new_batch(**args)

    def test_no_upstream_evidence_is_not_verified(self):
        # 暂时没有可信上游收据时 -> NOT_VERIFIED。这是正确行为，不是功能缺失：
        # 本模块不把"无证据"包装成通过，也不伪造正向 PASS。
        out = self._args(expected_manifest_evidence=None,
                         observed_manifest_evidence=None)
        assert out["verdict"] == "NOT_VERIFIED"
        assert out["verdict"] != "MATERIALIZED_ALLOWED"

    def test_upstream_chain_integration(self):
        # 集成形态：模拟"可信上游 -> 收据 -> 判定模块"证据传递链。
        # 上游执行两次独立读取，生成绑定收据；判定模块验证字段映射。
        def fake_trusted_upstream_read(target, read_id, payload=b"manifest-bytes"):
            import hashlib
            sha = hashlib.sha256(payload).hexdigest()
            return {"source": "consumer-drive-readback",
                    "target": target,
                    "sha": sha,
                    "read_id": read_id,
                    "read_timestamp": "2026-10-18T15:00:00Z"}, sha

        r1, sha1 = fake_trusted_upstream_read("consumer-docs", "up-001")
        r2, sha2 = fake_trusted_upstream_read("consumer-docs", "up-002")
        assert sha1 == sha2  # 同一内容，两次独立读取
        inv = {"source": "consumer-drive-readback",
               "target": "consumer-docs",
               "artifacts": ["doc-1", "doc-2"],
               "read_id": "up-inv-001",
               "read_timestamp": "2026-10-18T15:00:00Z"}
        out = rw.gate_new_batch(
            expected_manifest_sha=sha1, observed_manifest_sha=sha2,
            expected_manifest_evidence=r1, observed_manifest_evidence=r2,
            required_artifacts=["doc-1", "doc-2"], artifact_inventory=inv,
            silent_truncation=0, third_party_read=True,
            consumer_target="consumer-docs")
        assert out["verdict"] == "MATERIALIZED_ALLOWED"
        # 链断裂：上游只读了一次（同一收据复用）-> 拒绝。
        out2 = rw.gate_new_batch(
            expected_manifest_sha=sha1, observed_manifest_sha=sha2,
            expected_manifest_evidence=r1, observed_manifest_evidence=dict(r1),
            required_artifacts=["doc-1", "doc-2"], artifact_inventory=inv,
            silent_truncation=0, third_party_read=True,
            consumer_target="consumer-docs")
        assert out2["verdict"] == "REFUSED"

    def test_caller_sha_without_receipt_never_proves(self):
        # 调用方只给 SHA（即使格式合法）但无收据 -> 不可是 MATERIALIZED_ALLOWED。
        out = self._args(expected_manifest_evidence=None,
                         observed_manifest_evidence=self._receipt(read_id="read-002"))
        assert out["verdict"] != "MATERIALIZED_ALLOWED"
