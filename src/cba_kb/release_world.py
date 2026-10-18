"""Release/Consumer Current World consistency evaluation (read-only).

T2-6 for CBA-KB v2.0.5 (Issue #200). Implements the READ + JUDGE logic for
AC-31~AC-38 (CW-01) against the v2 spec ``Q1-V205-UNIFIED-REALDATA-CANARY-v2``.

This module is PURE EVALUATION:

* it never writes production state,
* it never mutates ``release_status.json``,
* it never touches git tags or old Data Production data,
* it never performs network or Drive I/O (callers supply already-read bytes).

The ``current_world`` output shape follows the v2 spec section 4.1
conceptual YAML. That shape is a CONCEPTUAL VIEW for distinguishing release
identities, NOT a new production schema.

Fail-closed throughout: unknown states -> ``NOT_VERIFIED``; missing
evidence -> ``BLOCKED/NOT_VERIFIED``; never guess JSON; never infer
equality from version strings alone; never silently relax a conflict.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

SPEC_ID = "Q1-V205-UNIFIED-REALDATA-CANARY-v2"

# Release states in which the current candidate must NOT be read.
# Mirrors the release state machine used by src/cba_kb/current_state.py.
TRANSITIONAL_STATES = frozenset({"PUBLISHING", "VERIFYING", "FAILED", "ROLLING_BACK"})

# The only legal capability statuses (AC-34).
CAPABILITY_STATUSES = frozenset({"MATERIALIZED", "NOT_MATERIALIZED", "UNAVAILABLE"})

# AC-38: change targets this module must never authorize.
_PROTECTED_TARGETS = frozenset({
    "git_tag",             # historical git tags (e.g. v2.0.4-2)
    "release_status",      # release_status.json content / version rewrite
    "data_production_old", # old Data Production data
})

# Artifact kinds whose consumability release_status is allowed to judge.
_DATA_PRODUCTION_KINDS = frozenset({"data_production_current"})
# Artifact kinds judged per surface/capability via manifest, materialization,
# rights and provenance -- never via the data-production release age.
_PRIVATE_KINDS = frozenset({"private_document", "private_stats", "private_excel"})


def _reject_duplicate_keys(pairs: List[tuple]) -> Dict[str, Any]:
    seen: Dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key rejected: {key!r}")
        seen[key] = value
    return seen


def _blocked(reason: str) -> Dict[str, Any]:
    return {"verdict": "BLOCKED/NOT_VERIFIED", "reason": reason}


# ---------------------------------------------------------------------------
# AC-31: bind a live release_status.json read (bytes + identity metadata)
# ---------------------------------------------------------------------------

def bind_release_status(
    raw_bytes: Optional[bytes],
    *,
    file_id: str,
    read_timestamp: str,
    revision: Optional[str] = None,
    sha256: Optional[str] = None,
) -> Dict[str, Any]:
    """Bind a live ``release_status.json`` read to its identity evidence.

    AC-31: the caller supplies the raw complete bytes (already read via a
    Local Trusted Runtime or an authorized connector readback). This
    function verifies the binding -- file_id, revision/etag when available,
    sha256, read timestamp, release_id/state and current pointers -- and
    never guesses JSON. Anything missing or unverifiable -> BLOCKED.
    """
    if raw_bytes is None:
        return _blocked("release_status.json bytes not read")
    if not file_id:
        return _blocked("file_id missing: cannot bind read to an identity")
    if not read_timestamp:
        return _blocked("read_timestamp missing: freshness unprovable")

    digest = hashlib.sha256(raw_bytes).hexdigest()
    if sha256 is not None and sha256.lower() != digest:
        return _blocked("sha256_mismatch: bytes do not match declared hash")

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return _blocked("bytes are not utf-8 JSON")
    try:
        doc = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except ValueError as exc:
        return _blocked(f"unparseable_or_duplicate_keys: {exc}")
    if not isinstance(doc, dict):
        return _blocked("top-level JSON is not an object")

    snapshot = doc.get("previous_snapshot")
    pointers = {
        "pending_release_id": doc.get("pending_release_id"),
        "previous_release_id": doc.get("previous_release_id"),
        "previous_snapshot_count": len(snapshot) if isinstance(snapshot, list) else None,
        "code_commit": doc.get("code_commit"),
    }
    return {
        "verdict": "OK",
        "data_production": {
            "release_id": doc.get("current_release_id"),
            "state": doc.get("state"),
            "evidence": {
                "file_id": file_id,
                "revision": revision,
                "sha256": digest,
                "read_timestamp": read_timestamp,
            },
            "pointers": pointers,
        },
        # Parsed document, kept for downstream judges (AC-35 reads it).
        "status_doc": doc,
    }


# ---------------------------------------------------------------------------
# AC-32: present the release tracks independently
# ---------------------------------------------------------------------------

def _track(value: Optional[Dict[str, str]], name: str) -> Dict[str, str]:
    if not value or not value.get("value"):
        return {"value": "NOT_VERIFIED", "source": value.get("source", "unknown") if value else "unknown"}
    return {"value": value["value"], "source": value.get("source", "unknown")}


def present_tracks(
    *,
    data_production_release: Optional[Dict[str, str]],
    engine_release_tag: Optional[Dict[str, str]],
    engine_runtime_sha: Optional[Dict[str, str]],
    python_package_version: Optional[Dict[str, str]],
    consumer_manifest_release: Optional[Dict[str, str]],
    consumer_data_batch: Optional[Dict[str, str]],
) -> Dict[str, Any]:
    """Present each release track explicitly with its own source (AC-32).

    Tracks may legitimately differ; equal version strings across tracks are
    reported side by side with distinct sources -- equality is never
    inferred from version-string equality alone.
    """
    tracks = {
        "data_production_release": _track(data_production_release, "data_production_release"),
        "engine_release_tag": _track(engine_release_tag, "engine_release_tag"),
        "engine_runtime_sha": _track(engine_runtime_sha, "engine_runtime_sha"),
        "python_package_version": _track(python_package_version, "python_package_version"),
        "consumer_manifest_release": _track(consumer_manifest_release, "consumer_manifest_release"),
        "consumer_data_batch": _track(consumer_data_batch, "consumer_data_batch"),
    }
    return {
        "tracks": tracks,
        "note": "each track is independent and source-labelled; "
                "no equality inferred from version strings alone",
    }


# ---------------------------------------------------------------------------
# AC-33: release_status judges only its own artifacts
# ---------------------------------------------------------------------------

def judge_consumability(
    artifact_kind: str,
    *,
    manifest_ok: bool,
    materialized: bool,
    rights_ok: bool,
    provenance_ok: bool,
    data_production_release_id: Optional[str] = None,
    release_state: Optional[str] = None,
) -> Dict[str, Any]:
    """Judge consumability per surface/capability (AC-33).

    ``release_status`` only rules on Data Production current artifacts.
    Private Document/Stats/Excel consumability is judged by the correct
    Consumer manifest, actual materialization, rights and provenance --
    an old Data Production release is never interpreted as a whole-library
    outage.
    """
    if artifact_kind in _DATA_PRODUCTION_KINDS:
        if release_state == "COMPLETE":
            return {"verdict": "CONSUMABLE",
                    "via": "release_status",
                    "release_id": data_production_release_id}
        return {"verdict": "NOT_CONSUMABLE",
                "reason": f"data production state is {release_state}"}
    if artifact_kind in _PRIVATE_KINDS:
        missing = [name for name, ok in (
            ("manifest", manifest_ok), ("materialization", materialized),
            ("rights", rights_ok), ("provenance", provenance_ok)) if not ok]
        if missing:
            return {"verdict": "NOT_CONSUMABLE",
                    "reason": "missing: " + ",".join(missing)}
        return {"verdict": "CONSUMABLE",
                "via": "consumer_manifest+materialization+rights+provenance",
                "note": "data production release age is not consulted"}
    return {"verdict": "NOT_VERIFIED",
            "reason": f"unknown artifact_kind: {artifact_kind}"}


# ---------------------------------------------------------------------------
# AC-34: per-capability MATERIALIZED / NOT_MATERIALIZED / UNAVAILABLE
# ---------------------------------------------------------------------------

def capability_status(
    name: str,
    *,
    status: str,
    reason: Optional[str] = None,
    batch_ref: Optional[str] = None,
) -> Dict[str, Any]:
    """Judge one advertised capability on its own evidence (AC-34).

    Each capability is judged independently: an AVAILABLE/MATERIALIZED
    documents capability never implies that person/stats/claim are also
    materialized -- this module performs no cross-capability inference.
    """
    if status not in CAPABILITY_STATUSES:
        raise ValueError(f"illegal capability status: {status!r}")
    if status == "NOT_MATERIALIZED" and not reason:
        raise ValueError("NOT_MATERIALIZED requires a reason")
    entry: Dict[str, Any] = {"capability": name, "status": status}
    if reason:
        entry["reason"] = reason
    if batch_ref:
        entry["batch_or_catalog_ref"] = batch_ref
    return entry


# ---------------------------------------------------------------------------
# AC-35: last-safe read on transitional states
# ---------------------------------------------------------------------------

def _snapshot_release_id(entry: Any) -> Optional[str]:
    if isinstance(entry, dict):
        rid = entry.get("release_id")
        return rid if isinstance(rid, str) and rid else None
    if isinstance(entry, str) and entry:
        return entry
    return None


def last_safe_read(status_doc: Dict[str, Any]) -> Dict[str, Any]:
    """Read the last safe version for this release track (AC-35).

    On PUBLISHING/VERIFYING/FAILED/ROLLING_BACK the unfinished candidate
    is never read; the previous_snapshot supplies this track's last safe
    version. A malformed snapshot fails closed. Other capabilities that
    are independent and stable are not touched by this judgement.
    """
    state = status_doc.get("state")
    if state == "COMPLETE":
        return {"mode": "current",
                "release_id": status_doc.get("current_release_id")}
    if state not in TRANSITIONAL_STATES:
        return {"verdict": "NOT_VERIFIED",
                "reason": f"unknown release state: {state}"}
    snapshot = status_doc.get("previous_snapshot")
    if not isinstance(snapshot, list) or not snapshot:
        return {"verdict": "NOT_VERIFIED",
                "reason": "malformed_snapshot_fail_closed"}
    release_id = _snapshot_release_id(snapshot[0])
    if release_id is None:
        return {"verdict": "NOT_VERIFIED",
                "reason": "malformed_snapshot_fail_closed"}
    return {
        "mode": "last_safe",
        "release_id": release_id,
        "entries_considered": len(snapshot),
        # The pending candidate is deliberately NOT read.
        "pending_ignored": status_doc.get("pending_release_id"),
    }


# ---------------------------------------------------------------------------
# AC-36: CURRENT_STATE_CONSISTENCY per consumer surface
# ---------------------------------------------------------------------------

_REQUIRED_BINDING_KEYS = frozenset(
    {"source", "track", "release_id", "code_commit", "manifest_binding", "stale"})


def check_surface_consistency(
    surface: str,
    bindings: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Check release_id/code_commit/manifest binding consistency (AC-36).

    All bindings for one Consumer Surface must satisfy the existing
    CURRENT_STATE_CONSISTENCY: identical within a track; across tracks a
    legal difference is marked EXPLICITLY_BOUND (never forced "equal" by
    rewriting versions); a same-track conflict is a CONTRACT_CONFLICT and
    is never silently relaxed. Stale bindings fail the check.
    """
    if not bindings:
        return {"verdict": "NOT_VERIFIED", "reason": "no bindings supplied"}
    for b in bindings:
        if not _REQUIRED_BINDING_KEYS.issubset(b.keys()):
            return {"verdict": "NOT_VERIFIED",
                    "reason": f"incomplete binding from {b.get('source', '?')}"}
    for b in bindings:
        if b["stale"]:
            return {"verdict": "FAIL",
                    "reason": f"stale binding from {b['source']}",
                    "surface": surface}
    by_track: Dict[str, List[Dict[str, Any]]] = {}
    for b in bindings:
        by_track.setdefault(b["track"], []).append(b)
    for track, group in by_track.items():
        first = (group[0]["release_id"], group[0]["code_commit"],
                 group[0]["manifest_binding"])
        for b in group[1:]:
            if (b["release_id"], b["code_commit"], b["manifest_binding"]) != first:
                return {"verdict": "CONTRACT_CONFLICT",
                        "surface": surface,
                        "track": track,
                        "reason": "same-track bindings disagree; "
                                  "locate the contract conflict, do not relax"}
    identities = {(b["release_id"], b["code_commit"], b["manifest_binding"])
                  for b in bindings}
    if len(identities) == 1:
        return {"verdict": "PASS", "surface": surface}
    return {
        "verdict": "EXPLICITLY_BOUND",
        "surface": surface,
        "bindings": [
            {"source": b["source"], "track": b["track"],
             "release_id": b["release_id"], "code_commit": b["code_commit"],
             "manifest_binding": b["manifest_binding"]} for b in bindings],
        "note": "legal cross-track difference; not forced equal",
    }


# ---------------------------------------------------------------------------
# AC-37: gate new Agent/Excel batches (no premature AVAILABLE/MATERIALIZED)
# ---------------------------------------------------------------------------

def gate_new_batch(
    *,
    expected_manifest_sha: Optional[str],
    observed_manifest_sha: Optional[str],
    required_artifacts: List[str],
    artifacts_present: List[str],
    silent_truncation: int,
    third_party_read: bool,
) -> Dict[str, Any]:
    """Judge whether a new batch may be claimed AVAILABLE/MATERIALIZED.

    AC-37: only after lawful intake AND an actual consumer projection.
    Expected vs observed manifest must match in the consumer target, the
    full required artifact set must be reachable, and silent_truncation
    must be 0. A third party not yet read is EXTERNAL_PENDING, not PASS.
    """
    if not third_party_read:
        return {"verdict": "EXTERNAL_PENDING",
                "reason": "third party not yet read; not a PASS"}
    if not expected_manifest_sha or expected_manifest_sha != observed_manifest_sha:
        return {"verdict": "REFUSED", "reason": "manifest_mismatch"}
    missing = [a for a in required_artifacts if a not in set(artifacts_present)]
    if missing:
        return {"verdict": "REFUSED",
                "reason": f"incomplete_artifact_set: {missing}"}
    if silent_truncation != 0:
        return {"verdict": "REFUSED",
                "reason": f"silent_truncation={silent_truncation}"}
    return {"verdict": "MATERIALIZED_ALLOWED",
            "note": "judgement only; this module marks nothing itself"}


# ---------------------------------------------------------------------------
# AC-38: zero-diff guard for history
# ---------------------------------------------------------------------------

def zero_diff_guard(proposed_changes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Refuse any change to historical tags/status/old data (AC-38).

    Historical git tags are never rewritten, release_status.json is never
    force-written to a new version, old Data Production data is never
    mutated. Only an independent, authorized new Data Release through the
    normal single-writer/rollback/readback path may update release status
    -- and this module performs no such update itself.
    """
    decisions = []
    for change in proposed_changes:
        target = change.get("target")
        if target in _PROTECTED_TARGETS:
            decisions.append({"change": change, "decision": "REFUSED",
                              "reason": f"protected target: {target}"})
        elif target in ("evaluation_output", "new_data_release"):
            decisions.append({"change": change, "decision": "ALLOWED",
                              "note": "no history rewritten; any real release "
                                      "update needs its own authorized path"})
        else:
            # Fail closed on anything unrecognized.
            decisions.append({"change": change, "decision": "REFUSED",
                              "reason": f"unknown target fails closed: {target}"})
    refused = [d for d in decisions if d["decision"] == "REFUSED"]
    if not decisions:
        verdict = "ALL_ALLOWED"
    elif not refused:
        verdict = "ALL_ALLOWED"
    elif len(refused) == len(decisions):
        verdict = "ALL_REFUSED"
    else:
        verdict = "MIXED"
    return {"verdict": verdict, "decisions": decisions}


# ---------------------------------------------------------------------------
# Output assembly: v2 spec section 4.1 conceptual view
# ---------------------------------------------------------------------------

def build_current_world(
    *,
    data_production: Dict[str, Any],
    engine: Dict[str, Any],
    consumer: Dict[str, Any],
    reconciliation: Dict[str, Any],
    spec_id: str = SPEC_ID,
) -> Dict[str, Any]:
    """Assemble the CW-01 conceptual ``current_world`` view.

    Follows the v2 spec section 4.1 conceptual YAML
    (data_production / engine / consumer / reconciliation). This is a
    read-only view for distinguishing release identities, NOT a new
    production schema.
    """
    for name, section in (("data_production", data_production),
                          ("engine", engine),
                          ("consumer", consumer),
                          ("reconciliation", reconciliation)):
        if not isinstance(section, dict):
            raise ValueError(f"{name} section must be a dict")
    return {
        "spec_id": spec_id,
        "current_world": {
            "data_production": data_production,
            "engine": engine,
            "consumer": consumer,
            "reconciliation": reconciliation,
        },
    }
