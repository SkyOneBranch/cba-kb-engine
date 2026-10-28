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
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from .kc_projection import KcError


class KcCurrentError(KcError):
    """Fail-closed current-pointer contract violation."""


class SimulatedCurrentStore:
    """Offline pointer store. States: EMPTY -> STAGED -> VALIDATED ->
    CURRENT, or FAILED. A commit is only possible from VALIDATED."""

    def __init__(self, *, target_domain: str):
        self.target_domain = target_domain
        self.state = "EMPTY"
        self._staged: Optional[str] = None
        self._current: Optional[str] = None
        self._seq = 0

    def stage(self, snapshot_id: str) -> None:
        self._staged = snapshot_id
        self.state = "STAGED"

    def validate(self) -> Dict[str, Any]:
        staged = self._staged
        if (not isinstance(staged, str) or not staged
                or not staged.startswith("kc-snapshot:")):
            self.state = "FAILED"
            raise KcCurrentError("STAGED_SNAPSHOT_INVALID")
        self.state = "VALIDATED"
        return {"valid": True, "snapshot_id": staged}

    def commit(self) -> Dict[str, Any]:
        if self.state != "VALIDATED" or self._staged is None:
            raise KcCurrentError("COMMIT_REQUIRES_VALIDATED_STAGE")
        self._current = self._staged
        self._staged = None
        self.state = "CURRENT"
        self._seq += 1
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
