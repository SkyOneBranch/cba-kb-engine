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
    """An authorization bound to one pair of source_refs, plus its audit.

    The wrapped authorization object is NOT handed out (B1): it can
    only be spent through :meth:`apply_pair` / :meth:`put_into_batch`,
    which verify the target pair against the bound pair first. This
    keeps the binding attached to the authorization for its whole life
    on the sanctioned path. (In-boundary code can still reach privates
    — the module docstring's honest boundary applies.)
    """

    _authorization: Any  # AuthorizedHumanDecision | AuthorizedTrustedLink
    kind: str  # "human_decision" | "trusted_link"
    source_refs: Tuple[str, str]
    audit: Dict[str, Any]

    def _check_pair(self, ref_a: Any, ref_b: Any) -> None:
        if {ref_a, ref_b} != set(self.source_refs):
            raise TrustError(
                "MINTED_AUTHORIZATION_PAIR_MISMATCH:"
                f"bound to {self.source_refs!r}")

    def apply_pair(self, left_sighting: Mapping[str, Any],
                   right_sighting: Mapping[str, Any], *,
                   verified_external_ids: Optional[Iterable[str]] = None):
        """Spend this authorization on the two sightings it was minted
        for, via person_candidate.assess_pair. The sightings' own
        source_refs must equal the bound pair (either orientation);
        anything else -> TrustError, and assess_pair is never called.
        """
        from .person_candidate import assess_pair
        self._check_pair(left_sighting.get("source_ref"),
                         right_sighting.get("source_ref"))
        kwargs: Dict[str, Any] = {
            "verified_external_ids": verified_external_ids}
        if self.kind == "human_decision":
            kwargs["human_decision"] = self._authorization
        else:
            kwargs["trusted_link"] = self._authorization
        return assess_pair(left_sighting, right_sighting, **kwargs)

    def put_into_batch(self, mapping: Dict[Tuple[str, str], Any]) -> None:
        """Insert this authorization into a batch decisions/links map
        under its bound pair. Refuses to overwrite a different object
        already present under either orientation of the pair.
        """
        for key in (self.source_refs, self.source_refs[::-1]):
            existing = mapping.get(key)
            if existing is not None and existing is not self._authorization:
                raise TrustError(
                    "MINTED_AUTHORIZATION_BATCH_CONFLICT:"
                    f"{self.source_refs!r}")
        mapping[self.source_refs] = self._authorization


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


def _audit(kind: str, ctx: TrustedOperatorContext, detail: str,
           evidence_refs: Tuple[str, ...],
           extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # The audit_id hashes the COMPLETE record (B2): every field that
    # distinguishes this authorization — including the bound refs or the
    # attested ID set — is part of the hashed content, so distinct
    # authorizations never share an audit_id.
    record = {
        "kind": kind,
        "operator": ctx.operator,
        "established_via": ctx.established_via,
        "detail": detail,
        "evidence_refs": list(evidence_refs),
    }
    if extra:
        record.update(extra)
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
        _authorization=decision, kind="human_decision", source_refs=refs,
        audit=_audit("human_decision", ctx, link, evidence,
                     extra={"source_refs": list(refs)}))


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
        _authorization=trusted, kind="trusted_link", source_refs=refs,
        audit=_audit("trusted_link", ctx, method, evidence,
                     extra={"source_refs": list(refs)}))


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
    audit = _audit(
        "verified_ids", ctx, verification_ref.strip(),
        (verification_ref.strip(),),
        extra={"verification_ref": verification_ref.strip(),
               "external_ids": sorted(set(ids))})
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
