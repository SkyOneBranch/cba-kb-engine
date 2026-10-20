"""B5 trusted supply path for person_candidate (T2-8, Issue #212).

Human Decision 2026-10-20: APPROVED_WITH_TRUST_BOUNDARY (implements the
standing B5 Option A ruling; not a new Requirement).

WHAT THIS MODULE IS
-------------------
The single sanctioned minting entry for the typed authorization objects
that :mod:`cba_kb.person_candidate` honors:

- :class:`~cba_kb.person_candidate.AuthorizedHumanDecision`
- :class:`~cba_kb.person_candidate.AuthorizedTrustedLink`
- verified external-ID sets for ``assess_pair`` / ``assess_batch``

Minting verifies and records, for every authorization: the two concrete
``source_ref`` values it binds, the decision kind, the operator
established by the controlled execution context, non-empty traceable
``evidence_refs``, and a deterministic audit record (``audit_id`` is a
content hash of the audit fields; hashing uses raw UTF-8 bytes — no text
normalization, cf. the #210 B-03 lesson).

OPERATOR TRUST — HOW IT IS ESTABLISHED, AND ITS LIMITS
------------------------------------------------------
Operator identity NEVER comes from per-request caller input: fields such
as ``operator_id``, ``attested_by`` or ``verified=true`` inside request
data are self-declarations and establish nothing here. The operator is
established ONCE, from the controlled execution environment, via
:func:`establish_operator_context`, which reads the operator identity
injected by the environment (``CBA_KB_TRUSTED_OPERATOR``). If the
environment provides no operator, minting fails closed — there is no
anonymous or self-declared path.

HONEST BOUNDARY (residual risk, stated plainly): this is an in-process
trusted-call convention, NOT cryptographic authentication and NOT an
unforgeable credential. Code already running inside this trust boundary
can construct the ``Authorized*`` classes directly; this module cannot
and does not claim to prevent that. What it provides is the one audited,
fail-closed minting path that legitimate callers use, so that
authorizations carry provenance instead of arriving as bare
self-declared data. Real Consumer identity authentication remains
PRE_EXPOSURE and is NOT established by this module; per the Human stop
condition, the trusted authorization path for untrusted Consumers is
not claimed as accepted.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterable, Mapping, Optional, Tuple

from .person_candidate import (
    AuthorizedHumanDecision,
    AuthorizedTrustedLink,
    PersonCandidateError,
)

OPERATOR_ENV_VAR = "CBA_KB_TRUSTED_OPERATOR"

_MINT_TOKEN = object()


class TrustError(RuntimeError):
    """Raised when a trusted authorization cannot be minted safely."""


class TrustedOperatorContext:
    """Operator identity established by the controlled environment.

    Constructible only via :func:`establish_operator_context` (a
    module-private token is required). This is a convention guard
    against accidental or data-driven construction, not a security
    proof — see the module docstring's honest boundary.
    """

    __slots__ = ("operator", "established_via")

    def __init__(self, operator: str, *, established_via: str,
                 _token: object = None) -> None:
        if _token is not _MINT_TOKEN:
            raise TrustError(
                "TRUSTED_CONTEXT_DIRECT_CONSTRUCTION:use "
                "establish_operator_context()")
        self.operator = operator
        self.established_via = established_via


def establish_operator_context(
        env: Optional[Mapping[str, str]] = None) -> TrustedOperatorContext:
    """Establish the operator from the controlled execution environment.

    Reads ``CBA_KB_TRUSTED_OPERATOR`` from ``env`` (default
    ``os.environ``). Missing or blank -> TrustError (fail-closed):
    without an environment-established operator, nothing may be minted.
    """
    source = os.environ if env is None else env
    operator = source.get(OPERATOR_ENV_VAR, "")
    if not isinstance(operator, str) or not operator.strip():
        raise TrustError(
            "TRUSTED_OPERATOR_NOT_ESTABLISHED:set "
            f"{OPERATOR_ENV_VAR} in the controlled environment")
    return TrustedOperatorContext(
        operator.strip(), established_via="environment",
        _token=_MINT_TOKEN)


@dataclass(frozen=True)
class MintedAuthorization:
    """An authorization object plus its binding and audit record."""

    authorization: Any  # AuthorizedHumanDecision | AuthorizedTrustedLink
    source_refs: Tuple[str, str]
    audit: Dict[str, Any]

    def for_pair(self, ref_a: str, ref_b: str) -> Any:
        """Return the authorization only for its bound pair of refs.

        Orientation-free: (a, b) and (b, a) are the same pair. Any other
        pair -> TrustError; a minted decision must never drift onto a
        pair it was not made for.
        """
        if {ref_a, ref_b} != set(self.source_refs):
            raise TrustError(
                "MINTED_AUTHORIZATION_PAIR_MISMATCH:"
                f"bound to {self.source_refs!r}")
        return self.authorization


@dataclass(frozen=True)
class VerifiedIdAttestation:
    """A verified external-ID set with its verification provenance."""

    external_ids: FrozenSet[str]
    audit: Dict[str, Any]


def _require_context(ctx: Any) -> TrustedOperatorContext:
    if not isinstance(ctx, TrustedOperatorContext):
        raise TrustError(
            "TRUSTED_CONTEXT_REQUIRED:operator context must come from "
            "establish_operator_context(); caller-supplied dicts/strings "
            "are self-declarations and establish nothing")
    return ctx


def _require_pair(left_ref: Any, right_ref: Any) -> Tuple[str, str]:
    for ref in (left_ref, right_ref):
        if not isinstance(ref, str) or not ref.strip():
            raise TrustError("MINT_INVALID_SOURCE_REF:non-empty required")
    if left_ref == right_ref:
        raise TrustError("MINT_INVALID_SOURCE_REF:pair_must_be_distinct")
    return (left_ref, right_ref)


def _require_evidence(evidence_refs: Any) -> Tuple[str, ...]:
    if isinstance(evidence_refs, (str, bytes)) or evidence_refs is None:
        raise TrustError(
            "MINT_EVIDENCE_REQUIRED:non-empty evidence_refs required")
    refs = tuple(evidence_refs)
    if not refs or any(
            not isinstance(r, str) or not r.strip() for r in refs):
        raise TrustError(
            "MINT_EVIDENCE_REQUIRED:non-empty evidence_refs required")
    return refs


def _audit(kind: str, ctx: TrustedOperatorContext,
           refs: Tuple[str, str], detail: str,
           evidence_refs: Tuple[str, ...]) -> Dict[str, Any]:
    record = {
        "kind": kind,
        "operator": ctx.operator,
        "established_via": ctx.established_via,
        "source_refs": list(refs),
        "detail": detail,
        "evidence_refs": list(evidence_refs),
    }
    canonical = json.dumps(record, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    record["audit_id"] = (
        "audit_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24])
    return record


def mint_human_decision(
        ctx: TrustedOperatorContext, *, left_ref: str, right_ref: str,
        link: str, evidence_refs: Iterable[str]) -> MintedAuthorization:
    """Mint an AuthorizedHumanDecision bound to (left_ref, right_ref)."""
    ctx = _require_context(ctx)
    refs = _require_pair(left_ref, right_ref)
    evidence = _require_evidence(evidence_refs)
    decision = AuthorizedHumanDecision(
        link, decided_by=ctx.operator, evidence_refs=evidence)
    return MintedAuthorization(
        authorization=decision, source_refs=refs,
        audit=_audit("human_decision", ctx, refs, link, evidence))


def mint_trusted_link(
        ctx: TrustedOperatorContext, *, left_ref: str, right_ref: str,
        method: str, evidence_refs: Iterable[str]) -> MintedAuthorization:
    """Mint an AuthorizedTrustedLink bound to (left_ref, right_ref)."""
    ctx = _require_context(ctx)
    refs = _require_pair(left_ref, right_ref)
    evidence = _require_evidence(evidence_refs)
    trusted = AuthorizedTrustedLink(
        method, evidence_refs=evidence, asserted_by=ctx.operator)
    return MintedAuthorization(
        authorization=trusted, source_refs=refs,
        audit=_audit("trusted_link", ctx, refs, method, evidence))


def attest_verified_ids(
        ctx: TrustedOperatorContext, external_ids: Iterable[str], *,
        verification_ref: str) -> VerifiedIdAttestation:
    """Attest a set of external IDs as authoritatively verified.

    ``verification_ref`` must point at the authoritative check that
    verified these IDs (a non-empty, traceable reference). Copying IDs
    out of ordinary sightings into this call without such a check is a
    contract violation — this function records WHO attests and ON WHAT
    verification, so the provenance is auditable; it cannot make an
    unverified ID verified by being called.
    """
    ctx = _require_context(ctx)
    if not isinstance(verification_ref, str) or not verification_ref.strip():
        raise TrustError(
            "VERIFIED_IDS_REQUIRE_VERIFICATION_REF:an authoritative "
            "verification reference is required")
    if isinstance(external_ids, (str, bytes)) or external_ids is None:
        raise TrustError("VERIFIED_IDS_INVALID:iterable_of_ids_required")
    ids = tuple(external_ids)
    if not ids or any(not isinstance(i, str) or not i.strip() for i in ids):
        raise TrustError("VERIFIED_IDS_INVALID:non-empty ids required")
    audit = _audit("verified_ids", ctx, ("", ""), verification_ref.strip(),
                   (verification_ref.strip(),))
    audit.pop("source_refs")
    audit["verification_ref"] = verification_ref.strip()
    audit["external_id_count"] = len(set(ids))
    return VerifiedIdAttestation(
        external_ids=frozenset(ids), audit=audit)


__all__ = [
    "OPERATOR_ENV_VAR",
    "TrustError",
    "TrustedOperatorContext",
    "MintedAuthorization",
    "VerifiedIdAttestation",
    "establish_operator_context",
    "mint_human_decision",
    "mint_trusted_link",
    "attest_verified_ids",
    "PersonCandidateError",
]
