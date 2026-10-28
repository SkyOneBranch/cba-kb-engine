"""PR-B (Issue #220) current-pointer OFFLINE state machine — synthetic
only.

Procedure: build to staging -> validate references/types/target/slice
completeness -> SIMULATED pointer update in an offline store (never a
real Drive pointer change) -> readback (re-read and compare) ->
consumer observed receipt. The receipt is synthetic/offline evidence.

Real Drive reads, real ACL checks, and real cache propagation are NOT
proven by this PR and are never claimed by it. "Current" means, per
question, the snapshot authorized for this target, legally published in
the SIMULATED sense, and observable by this offline client at question
time — never main-latest, never original-modification-latest, never
chat-memory-latest.

Repair round 1 (Q2 FAIL response): SimulatedCurrentStore.validate()
now resolves the staged identifier against a snapshot registry
(registered via register(), or passed explicitly), verifies the
snapshot digest with validate_snapshot(), and checks the target domain.
Without any registry it FAILS (fail-closed) — it never falls back to
prefix-only checking.

Route A (Q2 round 2 FAIL response, rule B): validate() binds the
validated digest and a deep copy of the validated content; commit()
recomputes against the current registry content and refuses on any
mismatch, wrong target, or registry replacement. Validation proves
the state at validation time only — never a later, mutated state.
"""

from __future__ import annotations

import copy
import dataclasses
from typing import Any, Callable, Dict, Optional

from .kc_projection import KcError, canonical_json
from .kc_snapshot import validate_snapshot


class KcCurrentError(KcError):
    """Fail-closed current-pointer contract violation."""


def _jsonable(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def _canonical_state(snapshot: Any) -> bytes:
    """Canonical bytes of a snapshot's full content, for
    validate-to-commit comparison. Dict snapshots are compared
    field-for-field; frozen dataclass snapshots are compared via
    their field mapping (they cannot mutate in place, so the digest
    check below is their binding)."""
    if isinstance(snapshot, dict):
        return canonical_json(_jsonable(snapshot))
    try:
        plain = dataclasses.asdict(snapshot)
    except (TypeError, ValueError):
        plain = {k: getattr(snapshot, k) for k in dir(snapshot)
                 if not k.startswith("_")}
    return canonical_json(_jsonable(plain))


class SimulatedCurrentStore:
    """Offline pointer store. States: EMPTY -> STAGED -> VALIDATED ->
    CURRENT, or FAILED. A commit is only possible from VALIDATED.
    Validation resolves the staged identifier against a snapshot
    registry: the snapshot must exist in the registry, its digest must
    validate, and its target domain must equal this store's domain."""

    def __init__(self, *, target_domain: str):
        self.target_domain = target_domain
        self.state = "EMPTY"
        self._staged: Optional[str] = None
        self._current: Optional[str] = None
        self._seq = 0
        self._registry: Dict[str, Any] = {}
        # Route A (rule B): validate() binds the validated digest,
        # the validated content (deep copy), and the registry mapping
        # used. commit() recomputes against the CURRENT registry
        # content and refuses on any mismatch — a dict mutated after
        # validation, a registry entry replaced after validation, a
        # wrong target, or a missing entry can no longer commit.
        self._validated_id: Optional[str] = None
        self._validated_copy: Any = None
        self._validated_registry: Optional[Dict[str, Any]] = None
        self._validated_digest: Optional[str] = None

    def _clear_validation(self) -> None:
        self._validated_id = None
        self._validated_copy = None
        self._validated_registry = None
        self._validated_digest = None

    def register(self, snapshot: Any) -> str:
        """Register a snapshot object (or dict) by its snapshot_id so
        validate() can resolve it. Returns the registered ID."""
        if isinstance(snapshot, dict):
            sid = snapshot.get("snapshot_id")
        else:
            sid = getattr(snapshot, "snapshot_id", None)
        if not isinstance(sid, str) or not sid:
            raise KcCurrentError("REGISTERED_SNAPSHOT_HAS_NO_ID")
        self._registry[sid] = snapshot
        return sid

    def stage(self, snapshot_id: str) -> None:
        # Staging a new id invalidates any prior validation binding.
        self._clear_validation()
        self._staged = snapshot_id
        self.state = "STAGED"

    def validate(self, *, snapshots: Optional[Dict[str, Any]] = None
                 ) -> Dict[str, Any]:
        """Resolve the staged identifier against the registry (the
        explicitly passed mapping, else this store's own registry).
        Fail-closed at every step: a missing registry, an unregistered
        ID, a digest failure, or a target-domain mismatch all set FAILED
        and raise. Prefix shape alone never validates."""
        staged = self._staged
        if (not isinstance(staged, str) or not staged
                or not staged.startswith("kc-snapshot:")):
            self.state = "FAILED"
            self._clear_validation()
            raise KcCurrentError("STAGED_SNAPSHOT_INVALID")
        registry = snapshots if snapshots is not None else self._registry
        if not registry:
            self.state = "FAILED"
            self._clear_validation()
            raise KcCurrentError("STAGED_SNAPSHOT_NO_REGISTRY")
        snap = registry.get(staged)
        if snap is None:
            self.state = "FAILED"
            self._clear_validation()
            raise KcCurrentError("STAGED_SNAPSHOT_NOT_REGISTERED")
        if validate_snapshot(snap)["valid"] is not True:
            self.state = "FAILED"
            self._clear_validation()
            raise KcCurrentError("STAGED_SNAPSHOT_DIGEST_INVALID")
        if isinstance(snap, dict):
            domain = snap.get("target_domain")
            digest = snap.get("content_sha256")
        else:
            domain = getattr(snap, "target_domain", None)
            digest = getattr(snap, "content_sha256", None)
        if domain != self.target_domain:
            self.state = "FAILED"
            self._clear_validation()
            raise KcCurrentError("STAGED_SNAPSHOT_TARGET_MISMATCH")
        # Bind validation to this exact content and registry mapping.
        self._validated_id = staged
        self._validated_registry = registry
        self._validated_copy = copy.deepcopy(snap)
        self._validated_digest = digest
        self.state = "VALIDATED"
        return {"valid": True, "snapshot_id": staged}

    def commit(self) -> Dict[str, Any]:
        if self.state != "VALIDATED" or self._staged is None:
            raise KcCurrentError("COMMIT_REQUIRES_VALIDATED_STAGE")
        # Revalidate against the CURRENT registry content: a dict
        # mutated after validate(), a registry entry replaced after
        # validate(), a digest change, or a target change all refuse
        # here — validation proves nothing about a later state.
        staged = self._staged
        registry = self._validated_registry
        if registry is None or self._validated_id != staged:
            self.state = "FAILED"
            self._clear_validation()
            raise KcCurrentError("COMMIT_VALIDATION_STALE")
        current = registry.get(staged)
        if current is None:
            self.state = "FAILED"
            self._clear_validation()
            raise KcCurrentError("COMMIT_VALIDATION_MISSING")
        if _canonical_state(current) != _canonical_state(
                self._validated_copy):
            self.state = "FAILED"
            self._clear_validation()
            raise KcCurrentError("COMMIT_VALIDATION_STALE")
        if validate_snapshot(current)["valid"] is not True:
            self.state = "FAILED"
            self._clear_validation()
            raise KcCurrentError("COMMIT_VALIDATION_DIGEST_INVALID")
        if isinstance(current, dict):
            domain = current.get("target_domain")
            digest = current.get("content_sha256")
        else:
            domain = getattr(current, "target_domain", None)
            digest = getattr(current, "content_sha256", None)
        if domain != self.target_domain:
            self.state = "FAILED"
            self._clear_validation()
            raise KcCurrentError("COMMIT_VALIDATION_TARGET_MISMATCH")
        if digest != self._validated_digest:
            self.state = "FAILED"
            self._clear_validation()
            raise KcCurrentError("COMMIT_VALIDATION_STALE")
        self._current = self._staged
        self._staged = None
        self.state = "CURRENT"
        self._seq += 1
        self._clear_validation()
        return self.observed_receipt()

    def read_current(self) -> Optional[str]:
        return self._current

    def observed_receipt(self) -> Dict[str, Any]:
        return {"snapshot_id": self._current,
                "target_domain": self.target_domain,
                "simulated": True,
                "evidence_kind": "SYNTHETIC_OFFLINE",
                "observation_seq": self._seq}


def resolve_current(*, captured_snapshot_id: str,
                    read_entry: Callable[[], str],
                    authorization_revoked: bool = False,
                    last_safe_snapshot_id: Optional[str] = None
                    ) -> Dict[str, Any]:
    """Per-question current resolution. The finishing re-read is the
    one re-read the contract allows: if the entry changed and a
    stability confirmation read differs again, the result is STALE —
    never a mixed snapshot. If business authorization is revoked, there
    is NO old-snapshot fallback, even when a last-safe snapshot exists
    (last_safe_snapshot_id is accepted here only so callers cannot
    silently pass it through; it is never used when revoked)."""
    if authorization_revoked:
        return {"status": "UNAVAILABLE",
                "reason": "AUTHORIZATION_REVOKED_NO_FALLBACK",
                "snapshot_id": None, "reread_used": False}
    first = read_entry()
    if first == captured_snapshot_id:
        return {"status": "OK", "snapshot_id": first,
                "reread_used": False}
    second = read_entry()
    if second == first:
        return {"status": "OK", "snapshot_id": first,
                "reread_used": True}
    return {"status": "STALE",
            "reason": "ENTRY_UNSTABLE_AFTER_ONE_REREAD",
            "snapshot_id": None, "reread_used": True}


def fallback_snapshot(*, last_safe_snapshot_id: Optional[str],
                      authorized: bool) -> Dict[str, Any]:
    """Transitional/failed-state fallback: ONLY to a last-safe previous
    snapshot that is still authorized, and always labelled as a
    fallback. Anything else is STALE, never a silent old version."""
    if authorized and last_safe_snapshot_id:
        return {"status": "OK", "snapshot_id": last_safe_snapshot_id,
                "fallback": "LAST_SAFE_PREVIOUS", "labelled": True}
    return {"status": "STALE",
            "reason": "LAST_SAFE_NOT_AUTHORIZED_OR_ABSENT",
            "snapshot_id": None, "fallback": None, "labelled": False}


def classify_status(*, evidence_present: bool, claim_conflict: bool = False,
                    freshness_ok: bool = True) -> Dict[str, Any]:
    """Status vocabulary, each mapped to its own rule (AC-09):
    UNKNOWN = the authorized view lacks sufficient evidence;
    CONFLICT = Claim-layer only, never a substitute for identity
    disambiguation; STALE = a freshness failure; OK = evidence present,
    no conflict, freshness holds."""
    if not evidence_present:
        return {"status": "UNKNOWN",
                "reason": "AUTHORIZED_VIEW_LACKS_SUFFICIENT_EVIDENCE"}
    if claim_conflict:
        return {"status": "CONFLICT",
                "reason": "CLAIM_LAYER_CONFLICT_ONLY"}
    if not freshness_ok:
        return {"status": "STALE", "reason": "FRESHNESS_FAILURE"}
    return {"status": "OK", "reason": "EVIDENCE_PRESENT_NO_CONFLICT_FRESH"}
