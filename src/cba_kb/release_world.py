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

TRUSTED-UPSTREAM CONTRACT (AC-37, explicit -- no new auth/signature
platform is introduced by this module):
  Trusted upstream is responsible for the REAL verifications:
    (a) performing the actual consumer-side reads,
    (b) computing the manifest SHA-256 digests from read bytes,
    (c) generating evidence receipts that bind
        source / target / sha / read_id / read_timestamp.
  This module is responsible ONLY for:
    (d) validating receipt STRUCTURE (all five fields present, well-formed),
    (e) validating BINDING consistency (receipt.sha == compared digest,
        receipt.target == judged consumer target, expected/observed from
        DISTINCT read_ids).
  This module does NOT verify that the upstream actually performed the
  reads -- that trust is established by the pipeline OUTSIDE this module.
  A caller-supplied SHA, evidence string, or boolean is NEVER promoted to
  a real Consumer materialization proof. Until verifiable upstream
  evidence is established and bound, the verdict is ``NOT_VERIFIED`` or
  ``EXTERNAL_PENDING`` -- never ``MATERIALIZED_ALLOWED``.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
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

# AC-38 (Q2-R2 NEW-2): this READ+JUDGE module authorizes NO writes.
# Only explicitly allowlisted (target, kind) pairs are permitted, and they
# are all read-only. There is no generic write allowlist: new_data_release
# is NOT a lawful-write target for this module; real release writes go
# through the independent authorized publish path.
_READONLY_OPERATIONS = frozenset({
    ("evaluation_output", "read"),
    ("evaluation_output", "evaluate"),
    ("evaluation_output", "view"),
    ("evaluation_output", "inspect"),
})
# Mutating or ambiguous kinds: always refused, on ANY target.
# (Kind matching is case-insensitive after strip+lower normalization;
# "DELETE" == "delete" == " delete ".)
_REFUSED_KINDS = frozenset({
    "delete", "rewrite", "overwrite", "mutate",
    "write", "update", "create", "force_version",
    "publish", "remove", "purge", "drop", "erase",
    "modify", "clear", "reset",
})

# Historical-target patterns: any target matching these is protected
# regardless of the kind claimed. Catches "release_status.json",
# "v2.0.1-1", "previous_snapshot", etc. -- not just the exact names.
_VERSION_PATTERN = re.compile(r"v\d+\.\d+")
_PROTECTED_TARGET_SUBSTRINGS = frozenset({
    "release_status", "git_tag", "previous_snapshot",
    "data_production_old", "snapshot_history",
})


def _is_protected_target(target: Any) -> bool:
    """Whether a target names protected history (exact or pattern).

    R4 hardening: pattern matching, not just exact-name membership, so
    "release_status.json", "v2.0.1-1" or "previous_snapshot" cannot slip
    past the guard by using a near-miss name.
    """
    if not isinstance(target, str):
        return False
    t = target.strip().lower()
    if t in _PROTECTED_TARGETS:
        return True
    if any(s in t for s in _PROTECTED_TARGET_SUBSTRINGS):
        return True
    if _VERSION_PATTERN.search(t):
        return True
    return False


def _detail_targets_history(detail: Any) -> bool:
    """Detect whether a change detail describes touching protected history.

    B7: the guard examines WHAT the operation proposes, not just the
    target name. A hostile detail (e.g. "overwrite protected history")
    is denied even when the target name itself is allowlisted.
    """
    d = detail if isinstance(detail, str) else ""
    d = d.lower()
    return any(k in d for k in (
        "git_tag", "release_status", "data_production_old",
        "protected histor", "overwrite histor", "rewrite histor",
        "force version", "force_version", "v2.0.4-2"))


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


def _nonempty_str(value: Any) -> bool:
    """Non-empty, non-blank string check (fail-closed identity validation)."""
    return isinstance(value, str) and bool(value.strip())


def _valid_timestamp(value: Any) -> bool:
    """Strict ISO-8601 timestamp check. Malformed values are rejected --
    freshness must be provable, never assumed."""
    if not _nonempty_str(value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _valid_sha256(value: Any) -> bool:
    """SHA-256 digest format: exactly 64 hex characters.

    NEW-1: pseudo-hashes like "x" are rejected before any comparison.
    """
    return (isinstance(value, str) and len(value) == 64 and
            all(c in "0123456789abcdefABCDEF" for c in value))


# NEW-1 (AC-37): structured evidence receipt schema.
# A receipt is produced by a TRUSTED UPSTREAM read (e.g. consumer-target
# readback, engine manifest publish). This module validates the receipt's
# STRUCTURE and BINDING (internal consistency + cross-consistency with the
# judged inputs). It does NOT independently verify that the upstream
# actually performed the read -- that trust is established by the pipeline
# outside this module. Without a well-formed, bound receipt, the verdict
# is fail-closed. An arbitrary dict, a bare True, or a non-empty string
# is NEVER accepted as independent proof.
_RECEIPT_REQUIRED_FIELDS = ("source", "target", "sha", "read_id",
                            "read_timestamp")


def _valid_evidence_receipt(receipt: Any) -> Optional[str]:
    """Validate a structured evidence receipt.

    Returns None if valid, else a machine-readable reason.
    """
    if not isinstance(receipt, dict):
        return ("evidence_must_be_structured_receipt_dict: "
                f"got {type(receipt).__name__}")
    for field in _RECEIPT_REQUIRED_FIELDS:
        if not _nonempty_str(receipt.get(field)):
            return f"receipt_missing_binding_field: {field}"
    if not _valid_sha256(receipt["sha"]):
        return "receipt_sha_not_valid_sha256"
    if not _valid_timestamp(receipt["read_timestamp"]):
        return "receipt_read_timestamp_not_iso8601"
    return None


def _valid_inventory_receipt(receipt: Any) -> Optional[str]:
    """Validate a structured artifact-inventory receipt."""
    if not isinstance(receipt, dict):
        return ("inventory_must_be_structured_receipt_dict: "
                f"got {type(receipt).__name__}")
    for field in ("source", "target", "read_id", "read_timestamp"):
        if not _nonempty_str(receipt.get(field)):
            return f"inventory_missing_binding_field: {field}"
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts or not all(
            _nonempty_str(a) for a in artifacts):
        return "inventory_artifacts_must_be_nonempty_list"
    if not _valid_timestamp(receipt["read_timestamp"]):
        return "inventory_read_timestamp_not_iso8601"
    return None


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

    # B1 (fail-closed identity binding): an anonymous or malformed status
    # document must never bind as OK. current_release_id, state and a
    # provable read timestamp are mandatory; revision is optional but its
    # availability is explicitly distinguished (BOUND vs NOT_AVAILABLE).
    if not _valid_timestamp(read_timestamp):
        return _blocked("malformed read_timestamp: freshness unprovable")
    release_id = doc.get("current_release_id")
    if not _nonempty_str(release_id):
        return _blocked("current_release_id missing or empty: cannot bind identity")
    state = doc.get("state")
    if not _nonempty_str(state):
        return _blocked("state missing or empty: cannot bind identity")
    if "previous_snapshot" in doc and not isinstance(doc["previous_snapshot"], list):
        return _blocked("previous_snapshot present but not a list")

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
            "release_id": release_id,
            "state": state,
            "evidence": {
                "file_id": file_id,
                "revision": revision,
                "revision_status": "BOUND" if _nonempty_str(revision) else "NOT_AVAILABLE",
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
            # B2 (fail-closed): a COMPLETE state with a missing/empty
            # release identity must NOT return CONSUMABLE. Positive
            # consumability requires a valid bound release identity.
            if not _nonempty_str(data_production_release_id):
                return {"verdict": "NOT_CONSUMABLE",
                        "reason": "COMPLETE state but release identity "
                                  "missing: fail closed"}
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
    """Format one advertised capability status entry (AC-34).

    FORMATTER, NOT CERTIFIER: this function does NOT independently
    establish or certify materialization. It formats a status entry for
    the CW-01 view. A ``MATERIALIZED`` entry REQUIRES a non-empty
    ``batch_or_catalog_ref`` binding it to materialized artifacts --
    callers must have established materialization through an evidence
    path (e.g. ``gate_new_batch`` for new batches, consumer manifest
    readback for existing capabilities) BEFORE formatting. Formatting
    ``MATERIALIZED`` without evidence is refused.

    Each capability is judged independently: an AVAILABLE/MATERIALIZED
    documents capability never implies that person/stats/claim are also
    materialized -- this module performs no cross-capability inference.
    """
    if status not in CAPABILITY_STATUSES:
        raise ValueError(f"illegal capability status: {status!r}")
    if status == "NOT_MATERIALIZED" and not reason:
        raise ValueError("NOT_MATERIALIZED requires a reason")
    if status == "MATERIALIZED" and not _nonempty_str(batch_ref):
        raise ValueError(
            "MATERIALIZED requires a batch_or_catalog_ref binding it to "
            "materialized artifacts; this formatter does not certify "
            "materialization without evidence")
    entry: Dict[str, Any] = {"capability": name, "status": status}
    if reason:
        entry["reason"] = reason
    if batch_ref:
        entry["batch_or_catalog_ref"] = batch_ref
    return entry


# ---------------------------------------------------------------------------
# AC-35: last-safe read on transitional states
# ---------------------------------------------------------------------------

# Snapshot entries usable as last-safe must be in a safe terminal state.
_SAFE_SNAPSHOT_STATES = frozenset({"COMPLETE", "ROLLED_BACK"})


def _safe_snapshot_release_id(entry: Any) -> Optional[str]:
    """Return the release_id iff the snapshot entry is safe AND complete.

    B4: a snapshot entry is usable as last-safe only when it is a dict
    with a non-empty release_id AND in a safe terminal state (COMPLETE
    or ROLLED_BACK). Anything else -> None (entry unusable).
    """
    if not isinstance(entry, dict):
        return None
    rid = entry.get("release_id")
    if not _nonempty_str(rid):
        return None
    if entry.get("state") not in _SAFE_SNAPSHOT_STATES:
        return None
    return rid


def last_safe_read(status_doc: Dict[str, Any]) -> Dict[str, Any]:
    """Read the last safe version for this release track (AC-35).

    On PUBLISHING/VERIFYING/FAILED/ROLLING_BACK the unfinished candidate
    is never read; the previous_snapshot supplies this track's last safe
    version. A malformed snapshot fails closed. Other capabilities that
    are independent and stable are not touched by this judgement.

    B4: snapshot entries are VALIDATED (safe state, completeness,
    pointer consistency with previous_release_id) -- previous_snapshot[0]
    is never blindly trusted.
    """
    state = status_doc.get("state")
    if state == "COMPLETE":
        rid = status_doc.get("current_release_id")
        if not _nonempty_str(rid):
            return {"verdict": "NOT_VERIFIED",
                    "reason": "COMPLETE without release identity"}
        return {"mode": "current", "release_id": rid}
    if state not in TRANSITIONAL_STATES:
        return {"verdict": "NOT_VERIFIED",
                "reason": f"unknown release state: {state}"}
    snapshot = status_doc.get("previous_snapshot")
    if not isinstance(snapshot, list) or not snapshot:
        return {"verdict": "NOT_VERIFIED",
                "reason": "malformed_snapshot_fail_closed"}
    # Validate entries; take the first safe+complete one. Never blindly [0].
    chosen: Optional[str] = None
    for entry in snapshot:
        rid = _safe_snapshot_release_id(entry)
        if rid is not None:
            chosen = rid
            break
    if chosen is None:
        return {"verdict": "NOT_VERIFIED",
                "reason": "no safe snapshot entry"}
    # Pointer consistency: the chosen entry must be the previous release.
    prev = status_doc.get("previous_release_id")
    if not _nonempty_str(prev):
        return {"verdict": "NOT_VERIFIED",
                "reason": "previous_release_id missing: "
                          "pointer consistency unverifiable"}
    if chosen != prev:
        return {"verdict": "NOT_VERIFIED",
                "reason": f"snapshot/pointer mismatch: {chosen!r} != "
                          f"previous_release_id {prev!r}"}
    return {
        "mode": "last_safe",
        "release_id": chosen,
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
    # B5: keys present is not enough -- identity VALUES must be non-empty.
    # A binding with empty/None release_id, code_commit, manifest_binding,
    # source or track can never PASS.
    for b in bindings:
        for field in ("source", "track", "release_id", "code_commit",
                      "manifest_binding"):
            if not _nonempty_str(b.get(field)):
                return {"verdict": "NOT_VERIFIED",
                        "reason": f"empty identity field {field!r} from "
                                  f"{b.get('source') or '?'}: cannot verify"}
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
    # B5: a cross-track difference is legitimized as EXPLICITLY_BOUND ONLY
    # with demonstrable binding evidence for the legal difference -- never
    # auto-legitimized. Each binding must carry binding_evidence describing
    # how the binding was established (e.g. manifest readback receipt).
    for b in bindings:
        if not _nonempty_str(b.get("binding_evidence")):
            return {"verdict": "NOT_VERIFIED",
                    "surface": surface,
                    "reason": f"cross-track difference without binding "
                              f"evidence from {b['source']}: not legitimized"}
    return {
        "verdict": "EXPLICITLY_BOUND",
        "surface": surface,
        "bindings": [
            {"source": b["source"], "track": b["track"],
             "release_id": b["release_id"], "code_commit": b["code_commit"],
             "manifest_binding": b["manifest_binding"],
             "binding_evidence": b["binding_evidence"]} for b in bindings],
        "note": "legal cross-track difference with evidence; not forced equal",
    }


# ---------------------------------------------------------------------------
# AC-37: gate new Agent/Excel batches (no premature AVAILABLE/MATERIALIZED)
# ---------------------------------------------------------------------------

def gate_new_batch(
    *,
    expected_manifest_sha: Optional[str],
    observed_manifest_sha: Optional[str],
    expected_manifest_evidence: Optional[Dict[str, Any]] = None,
    observed_manifest_evidence: Optional[Dict[str, Any]] = None,
    required_artifacts: List[str],
    artifact_inventory: Optional[Dict[str, Any]] = None,
    silent_truncation: int,
    third_party_read: bool,
    consumer_target: str,
) -> Dict[str, Any]:
    """Judge whether a new batch may be claimed AVAILABLE/MATERIALIZED.

    AC-37: only after lawful intake AND an actual consumer projection.

    NEW-1 (fail-closed invariant): MATERIALIZED_ALLOWED is NEVER derived
    from unverified caller assertions. The manifest SHAs must be valid
    SHA-256 digests; each side needs a STRUCTURED evidence receipt (dict,
    not a string/bool) that is BOUND to the compared digest, to the
    consumer target, and to a distinct read operation. The artifact
    inventory must be a bound receipt too -- a caller-supplied name list
    matching required_artifacts is self-proof and is rejected.

    TRUST BOUNDARY (explicit; see also module docstring TRUSTED-UPSTREAM
    CONTRACT): this module validates receipt STRUCTURE and BINDING. It
    does not independently verify that the trusted upstream actually
    performed the reads -- that trust is established by the pipeline
    outside this module. Callers must supply receipts produced by the
    trusted upstream; anything else fails closed. A caller-supplied SHA,
    evidence string, or boolean is NEVER promoted to a real Consumer
    materialization proof: without established, bound upstream evidence
    the verdict is NOT_VERIFIED or EXTERNAL_PENDING, never
    MATERIALIZED_ALLOWED.

    A third party not yet read is EXTERNAL_PENDING, not PASS.
    """
    if not third_party_read:
        return {"verdict": "EXTERNAL_PENDING",
                "reason": "third party not yet read; not a PASS"}
    if not _nonempty_str(consumer_target):
        return {"verdict": "REFUSED",
                "reason": "missing_consumer_target: nothing to bind evidence to"}
    # R4: no upstream evidence established at all (as opposed to malformed
    # evidence) -> NOT_VERIFIED, not MATERIALIZED_ALLOWED. This is the
    # correct behavior when the trusted upstream has not yet produced
    # receipts; it is not a feature gap. Malformed receipts below are
    # REFUSED (actively wrong); absent receipts are NOT_VERIFIED
    # (not yet established).
    if expected_manifest_evidence is None and observed_manifest_evidence is None:
        return {"verdict": "NOT_VERIFIED",
                "reason": "upstream evidence not established: no trusted "
                          "receipts bound; judgement-only, no proof claimed"}
    if not required_artifacts:
        return {"verdict": "REFUSED",
                "reason": "empty_required_artifact_set: nothing to verify"}
    # SHA-256 format first: pseudo-hashes ("x") are rejected before any
    # comparison. No format -> no judgement.
    if not _valid_sha256(expected_manifest_sha):
        return {"verdict": "REFUSED",
                "reason": "expected_manifest_sha_not_valid_sha256"}
    if not _valid_sha256(observed_manifest_sha):
        return {"verdict": "REFUSED",
                "reason": "observed_manifest_sha_not_valid_sha256"}
    if expected_manifest_sha != observed_manifest_sha:
        return {"verdict": "REFUSED", "reason": "manifest_mismatch"}
    # Structured receipts, not strings/bools/arbitrary dicts.
    err = _valid_evidence_receipt(expected_manifest_evidence)
    if err:
        return {"verdict": "REFUSED",
                "reason": f"expected_manifest_evidence_invalid: {err}"}
    err = _valid_evidence_receipt(observed_manifest_evidence)
    if err:
        return {"verdict": "REFUSED",
                "reason": f"observed_manifest_evidence_invalid: {err}"}
    # Binding: each receipt must attest to the digest being compared.
    if expected_manifest_evidence["sha"] != expected_manifest_sha:
        return {"verdict": "REFUSED",
                "reason": "expected_evidence_not_bound_to_manifest_sha"}
    if observed_manifest_evidence["sha"] != observed_manifest_sha:
        return {"verdict": "REFUSED",
                "reason": "observed_evidence_not_bound_to_manifest_sha"}
    # Independence: expected and observed must come from DIFFERENT reads.
    # The same receipt supplied twice is self-proof, not corroboration.
    if (expected_manifest_evidence["read_id"] ==
            observed_manifest_evidence["read_id"]):
        return {"verdict": "REFUSED",
                "reason": "expected_and_observed_not_independent_reads: "
                          "same read_id supplied twice"}
    # Target binding: evidence must apply to the judged consumer target.
    if expected_manifest_evidence["target"] != consumer_target:
        return {"verdict": "REFUSED",
                "reason": "expected_evidence_target_mismatch"}
    if observed_manifest_evidence["target"] != consumer_target:
        return {"verdict": "REFUSED",
                "reason": "observed_evidence_target_mismatch"}
    # Artifact inventory: a bound receipt, not a caller-supplied name list.
    # required_artifacts matching a bare artifacts_present list is
    # self-proof and is no longer accepted.
    err = _valid_inventory_receipt(artifact_inventory)
    if err:
        return {"verdict": "REFUSED",
                "reason": f"artifact_inventory_invalid: {err}"}
    if artifact_inventory["target"] != consumer_target:
        return {"verdict": "REFUSED",
                "reason": "artifact_inventory_target_mismatch"}
    inventory_artifacts = set(artifact_inventory["artifacts"])
    missing = [a for a in required_artifacts if a not in inventory_artifacts]
    if missing:
        return {"verdict": "REFUSED",
                "reason": f"incomplete_artifact_set: {missing}"}
    if silent_truncation != 0:
        return {"verdict": "REFUSED",
                "reason": f"silent_truncation={silent_truncation}"}
    return {"verdict": "MATERIALIZED_ALLOWED",
            "note": ("judgement only; this module marks nothing itself. "
                     "Receipt structure and binding validated; the trust "
                     "that the upstream performed the reads is established "
                     "outside this module.")}


# ---------------------------------------------------------------------------
# AC-38: zero-diff guard for history
# ---------------------------------------------------------------------------

def zero_diff_guard(proposed_changes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Refuse any change to historical tags/status/old data (AC-38).

    Historical git tags are never rewritten, release_status.json is never
    force-written to a new version, old Data Production data is never
    mutated.

    NEW-2 (fail-closed invariant): ALL_ALLOWED must NEVER contain a state
    change that lacks independent authorization. This READ+JUDGE module
    authorizes NO writes: only explicitly allowlisted (target, kind) pairs
    are permitted, and every allowlisted pair is read-only. Mutating kinds
    (delete/rewrite/overwrite/mutate/...), unknown kinds, and ambiguous
    operations are refused on ANY target. new_data_release is NOT a
    generic write allowlist -- real release writes go through the
    independent authorized publish path, never through this function.

    Decision order (structural, no free-text keyword gating):
      0. proposal not a dict, or has undefined extra fields -> REFUSED;
         target missing/non-string/empty -> REFUSED;
         kind missing/non-string/empty -> REFUSED;
         detail not None and not a string -> REFUSED (never crash,
         never silently discard);
      1. protected target (exact name OR pattern: release_status, version
         tags like v2.0.1-1, previous_snapshot, ...) -> REFUSED (any kind,
         even "read": the target itself must not be touched);
      2. mutating/ambiguous kind (case-insensitive after strip+lower:
         delete/rewrite/overwrite/mutate/write/update/create/publish/
         remove/purge/...) -> REFUSED on any target;
      3. detail targets protected history -> REFUSED (defense in depth);
      4. explicitly allowlisted (target, kind) read-only pair AND detail
         is missing or None -> ALLOWED. Any non-None detail is REFUSED:
         the (target, kind) pair IS the complete operation description.
         (R6 strict contract: free-text details are not classified, so
         the verb-blacklist is removed -- single security logic.)
      5. anything else -> REFUSED (fail closed).

    Verdicts describe the PROPOSALS only (judgement-only): this function
    never claims to have verified actual execution effects.
    """
    decisions = []
    for change in proposed_changes:
        # A proposal that is not even a dict cannot be judged safe.
        if not isinstance(change, dict):
            decisions.append({"change": change, "decision": "REFUSED",
                              "reason": "malformed proposal: not a dict; "
                                        "judgement-only"})
            continue
        # 0. Strict proposal schema: only target/kind/detail are defined.
        #    Undefined extra fields are refused -- they must not smuggle
        #    operation semantics past the guard (e.g. {"action": "delete"}).
        _ALLOWED_PROPOSAL_KEYS = frozenset({"target", "kind", "detail"})
        extra_keys = set(change.keys()) - _ALLOWED_PROPOSAL_KEYS
        if extra_keys:
            decisions.append({"change": change, "decision": "REFUSED",
                              "reason": f"undefined proposal fields: "
                                        f"{sorted(extra_keys)}; judgement-only"})
            continue
        target = change.get("target")
        kind = change.get("kind")
        detail = change.get("detail")
        # 1. Target must be a non-empty string. Missing/non-string targets
        #    fail closed (never silently skipped).
        if not isinstance(target, str) or not target.strip():
            decisions.append({"change": change, "decision": "REFUSED",
                              "reason": f"missing or invalid target: "
                                        f"{target!r}; judgement-only"})
            continue
        target_norm = target.strip()
        target_key = target_norm.lower()
        # 2. Protected history (exact name OR pattern): any kind refused.
        #    The target itself must not be touched by this module.
        if _is_protected_target(target_norm):
            decisions.append({"change": change, "decision": "REFUSED",
                              "reason": f"protected target: {target_norm!r}; "
                                        "judgement-only, no authorization"})
            continue
        # 3. Kind must be a string. Non-string kinds (list/dict/int/None)
        #    are refused -- never crash, never pass through.
        if not isinstance(kind, str):
            decisions.append({"change": change, "decision": "REFUSED",
                              "reason": f"invalid kind type: {type(kind).__name__}; "
                                        "judgement-only"})
            continue
        kind_norm = kind.strip().lower()
        if not kind_norm:
            decisions.append({"change": change, "decision": "REFUSED",
                              "reason": "empty kind after normalization; "
                                        "judgement-only"})
            continue
        # 4. Mutating/ambiguous kind: refused on ANY target.
        if kind_norm in _REFUSED_KINDS:
            decisions.append({"change": change, "decision": "REFUSED",
                              "reason": f"mutating/ambiguous kind refused on "
                                        f"any target: {kind!r}; judgement-only"})
            continue
        # 4b. Detail must be None or a string. Structured/typed details
        #     (dict/list/int/bool) are refused outright -- their contents
        #     must never be silently discarded to reach an allowlist.
        if detail is not None and not isinstance(detail, str):
            decisions.append({"change": change, "decision": "REFUSED",
                              "reason": f"unsupported detail type: "
                                        f"{type(detail).__name__}; "
                                        "judgement-only"})
            continue
        # 5. Detail targets protected history (defense in depth, checked
        #    before the allowlist so a hostile detail cannot ride along).
        if _detail_targets_history(detail):
            decisions.append({"change": change, "decision": "REFUSED",
                              "reason": "detail indicates protected-history "
                                        "tampering: fail closed; "
                                        "judgement-only"})
            continue
        # 6. Explicitly allowlisted (target, kind) read-only pair only.
        #    STRICT CONTRACT (R6): a read-only proposal carries NO detail.
        #    detail must be missing or None. Any non-None detail -- empty
        #    string, non-empty string, or any other value -- is REFUSED.
        #    The (target, kind) pair IS the complete operation description;
        #    free-text details cannot be reliably classified as benign,
        #    so they are not accepted. This replaces the verb-blacklist
        #    as the primary guarantee (single security logic, no dual).
        if (target_key, kind_norm) in _READONLY_OPERATIONS:
            if detail is not None:
                decisions.append({"change": change, "decision": "REFUSED",
                                  "reason": "allowlisted read must not carry "
                                            "detail: strict schema requires "
                                            "detail missing or None; "
                                            "judgement-only"})
            else:
                decisions.append({"change": change, "decision": "ALLOWED",
                                  "note": "read-only operation; no state "
                                          "change authorized by this module; "
                                          "judgement-only"})
        else:
            # Fail closed on unknown kinds, unknown targets, and read
            # kinds on non-allowlisted targets.
            decisions.append({"change": change, "decision": "REFUSED",
                              "reason": f"not an allowlisted read-only "
                                        f"operation: target={target_norm!r} "
                                        f"kind={kind_norm!r}; judgement-only"})
    refused = [d for d in decisions if d["decision"] == "REFUSED"]
    if not decisions:
        verdict = "ALL_ALLOWED"
    elif not refused:
        verdict = "ALL_ALLOWED"
    elif len(refused) == len(decisions):
        verdict = "ALL_REFUSED"
    else:
        verdict = "MIXED"
    return {"verdict": verdict,
            "decisions": decisions,
            "note": ("judgement-only: ALLOWED means 'read-only and "
                     "allowlisted'; this module authorizes no state "
                     "change and verifies no execution effects")}


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
