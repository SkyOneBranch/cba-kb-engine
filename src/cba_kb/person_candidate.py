"""Person Candidate layer: cross-source identity ambiguity resolution.

This module NEVER promotes a candidate to a confirmed Person identity.
Ambiguous inputs (same name across sources, nickname overlap, same birth
month, same team) yield ``REVIEW_REQUIRED``. A merge is permitted ONLY
through an authorized channel defined below; every self-asserted claim
inside ordinary data fails closed.

Frozen contracts honored (Base db47586, v2.0.5 T2-4; Q2 R1 remediation):
- AC-04~07: candidate-only output never leaks or asserts a Person
  identity (``identity_assertion`` is always ``"NONE"``; no
  ``player_uid`` is ever emitted, and identity-bearing input fields are
  rejected outright — B7).
- AC-23: titles/aliases ("大狙"/"小卢"-style nicknames) are searchable
  candidate signals only; a nickname hit never resolves a Person ID.
- AC-24: contradictory fields under one claimed identity keep both
  source records plus conflict status — including birth-month
  contradictions (B6). No last-write-wins.
- AC-05: malformed input -> REJECTED (PersonCandidateError); ambiguity
  -> REVIEW_REQUIRED; nothing is silently dropped or fabricated.
- Name normalization reuses ``player_identity.normalize_name`` (NFKC,
  whitespace collapse) so "same name" means exactly what the identity
  registry means. The ``player_identity`` WRITE path is never touched:
  this module performs no registry mutation and emits no player_uid.
- ``merged_view`` is a provenance-carrying candidate grouping, NOT a
  Person identity promotion. Human review remains the final arbiter.

Authorization model (Q2 R1: B2/B4/B5)
------------------------------------
- B2: same name + same source (+ same team) is NEVER merge authority.
  One document can mention two different people with the same name;
  shared provenance is an ambiguity signal, not identity evidence.
- B4: an ``external_id`` carried inside a sighting is an OBSERVATION —
  candidate evidence only. Equal unverified IDs cannot merge; differing
  unverified IDs cannot separate. An external ID becomes authoritative
  ONLY when the trusted upstream supplies it in ``verified_external_ids``
  (its own registry cross-check against a real, namespaced source).
  Self-reported ``issuer`` / ``namespace`` / ``trusted`` fields inside a
  sighting are not even legal input (see the field whitelist, B7).
- B5: Human decisions and trusted associations are honored ONLY as
  :class:`AuthorizedHumanDecision` / :class:`AuthorizedTrustedLink`
  objects. A plain dict — ``{"link": "same"}``, a ``confidence=HIGH``
  link claim, with or without forged reviewer/timestamp/hash metadata —
  is rejected fail-closed: unverified data can never impersonate an
  authorization. Only the trusted upstream constructs these objects,
  after authenticating the operator, confirming their authority, and
  auditing the decision (see the classes' docstrings).

HONEST STATUS — T2-8 TRUSTED WIRING NOT YET ACCEPTED: the trusted
upstream that mints ``Authorized*`` objects and supplies
``verified_external_ids`` is T2-8 integration work and has NOT been
connected or accepted yet. These types define the contract; they are
not evidence that a trusted path exists today, and this module's lack
of I/O is not an authorization proof. Until T2-8 lands with its own
review, no production caller can obtain merge authorization through a
reviewed path.

Batch semantics (Q2 R1: B1/B3): pair verdicts are keyed canonically, so
group verdicts are invariant under input permutation. ``DISTINCT`` and
Human ``not_same`` verdicts are cannot-link constraints: a group
containing a hard DISTINCT pair (verified-ID mismatch, authorized
``not_same``, birth-month contradiction) is a CONFLICT group, never a
merge; any other DISTINCT pair inside a group also blocks merging.

T2-8 will wire registry lookups; this module takes sightings as plain
dicts and performs no registry I/O.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .player_identity import normalize_name

SCHEMA_VERSION = 1

# Pair verdicts.
MERGE_ALLOWED = "MERGE_ALLOWED"
AMBIGUOUS = "AMBIGUOUS"
CONFLICT = "CONFLICT"
DISTINCT = "DISTINCT"

# Batch/group statuses.
REVIEW_REQUIRED = "REVIEW_REQUIRED"
CONFLICT_STATUS = "CONFLICT"
RESOLVED = "RESOLVED"
SINGLETON = "SINGLETON"

#: Verdicts that group sightings into one candidate group.
_GROUPING_VERDICTS = frozenset({MERGE_ALLOWED, AMBIGUOUS, CONFLICT})

#: DISTINCT reasons backed by positive authoritative/factual evidence.
#: Inside a group they are hard cannot-link violations -> CONFLICT.
_HARD_DISTINCT_REASONS = frozenset({
    "DIFFERENT_EXTERNAL_IDS",
    "HUMAN_DECISION_NOT_SAME",
    "DIFFERENT_BIRTH_MONTHS",
})

#: The ONLY fields a sighting may carry (B7). Identity-bearing fields
#: (``player_uid``, ``person_id``, ...) and self-asserted authority
#: fields (``issuer``, ``trusted``, ``verified``, ...) are rejected.
_SIGHTING_FIELDS = frozenset({
    "source_ref",
    "source_id",
    "source_kind",
    "display_name",
    "nicknames",
    "birth_month",
    "team",
    "external_id",
})

_BIRTH_MONTH = re.compile(r"\A\d{4}-(0[1-9]|1[0-2])\Z")
_TRUSTED_METHODS = frozenset({"EXTERNAL_IDENTIFIER", "SOURCE_DECLARED"})


class PersonCandidateError(RuntimeError):
    """Fail-closed input/contract violation in the candidate layer."""


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PersonCandidateError(f"SIGHTING_FIELD_REQUIRED:{label}")
    if "\x00" in value:
        raise PersonCandidateError(f"SIGHTING_FIELD_INVALID:{label}")
    return value.strip()


def _clean_refs(refs: Any, label: str, *, allow_empty: bool) -> Tuple[str, ...]:
    if isinstance(refs, str) or not isinstance(refs, Iterable):
        raise PersonCandidateError(f"{label}:refs_must_be_a_collection")
    cleaned = set()
    for ref in refs:
        if not isinstance(ref, str) or not ref.strip():
            raise PersonCandidateError(f"{label}:refs_must_be_non_empty_strings")
        cleaned.add(ref.strip())
    if not cleaned and not allow_empty:
        raise PersonCandidateError(f"{label}:evidence_refs_required")
    return tuple(sorted(cleaned))


class AuthorizedHumanDecision:
    """A Human same/not-same decision delivered via the trusted channel.

    TRUST BOUNDARY (B5): this module performs no authentication, no
    network verification and no cryptographic signing — it has no I/O
    by design. What it enforces is the CHANNEL: only an instance of
    this class is honored as a Human decision. A plain dict decoded
    from untrusted data is rejected fail-closed, however it is
    decorated; data can never impersonate an authorization.

    The trusted upstream (T2-8 wiring) is responsible for, BEFORE
    constructing this object: authenticating the operator, confirming
    the operator is authorized to arbitrate identity decisions, and
    recording the decision in its audit trail. ``decided_by`` names
    that operator for provenance; it is evidence ABOUT the upstream's
    process, not something this module verifies.

    HONEST STATUS: the T2-8 trusted wiring is NOT yet connected or
    accepted. This class defines the contract — it is not proof that a
    trusted path exists, and this module's lack of I/O is not an
    authorization proof.
    """

    __slots__ = ("link", "decided_by", "evidence_refs")

    def __init__(
        self,
        link: str,
        *,
        decided_by: str,
        evidence_refs: Iterable[str] = (),
    ) -> None:
        if link not in ("same", "not_same"):
            raise PersonCandidateError(
                "AUTHORIZED_DECISION_INVALID:link_must_be_same_or_not_same")
        if not isinstance(decided_by, str) or not decided_by.strip():
            raise PersonCandidateError(
                "AUTHORIZED_DECISION_INVALID:decided_by_required")
        self.link = link
        self.decided_by = decided_by.strip()
        self.evidence_refs = _clean_refs(
            evidence_refs, "AUTHORIZED_DECISION_INVALID", allow_empty=True)


class AuthorizedTrustedLink:
    """A trusted association verified and delivered by the trusted upstream.

    TRUST BOUNDARY (B5): same channel rule as
    :class:`AuthorizedHumanDecision` — only instances of this class are
    honored; plain dict claims (``method`` / ``confidence=HIGH`` /
    ``evidence_refs``, optionally dressed with ``verified=true``) are
    rejected fail-closed. Caller-asserted confidence establishes
    nothing: the upstream must have verified the evidence and the
    asserting party's authority BEFORE constructing this object, and
    records that verification in its own audit trail.

    HONEST STATUS: the T2-8 trusted wiring is NOT yet connected or
    accepted; see :class:`AuthorizedHumanDecision`.
    """

    __slots__ = ("method", "evidence_refs", "asserted_by")

    def __init__(
        self,
        method: str,
        *,
        evidence_refs: Iterable[str],
        asserted_by: str,
    ) -> None:
        if method not in _TRUSTED_METHODS:
            raise PersonCandidateError(
                "AUTHORIZED_LINK_INVALID:method_not_trusted")
        if not isinstance(asserted_by, str) or not asserted_by.strip():
            raise PersonCandidateError(
                "AUTHORIZED_LINK_INVALID:asserted_by_required")
        self.method = method
        self.asserted_by = asserted_by.strip()
        self.evidence_refs = _clean_refs(
            evidence_refs, "AUTHORIZED_LINK_INVALID", allow_empty=False)


def _validate_sighting(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise PersonCandidateError("SIGHTING_NOT_A_DICT")
    # B7: minimal field whitelist. Identity-bearing fields and
    # self-asserted authority fields are rejected, never carried into
    # any output (previously a shallow copy leaked them verbatim).
    extra = sorted(set(value) - _SIGHTING_FIELDS)
    if extra:
        raise PersonCandidateError(
            f"SIGHTING_FIELD_NOT_ALLOWED:{','.join(extra)}")
    sighting = {field: value[field] for field in value}
    sighting["source_ref"] = _required_text(value.get("source_ref"), "source_ref")
    sighting["display_name"] = _required_text(value.get("display_name"), "display_name")
    nicknames = sighting.get("nicknames", [])
    if not isinstance(nicknames, list) or any(
        not isinstance(n, str) or not n.strip() for n in nicknames
    ):
        raise PersonCandidateError("SIGHTING_FIELD_INVALID:nicknames")
    birth_month = sighting.get("birth_month")
    if birth_month is not None:
        if not isinstance(birth_month, str) or not _BIRTH_MONTH.match(birth_month):
            raise PersonCandidateError("SIGHTING_FIELD_INVALID:birth_month")
    for field in ("team", "source_id", "source_kind", "external_id"):
        item = sighting.get(field)
        if item is not None and (not isinstance(item, str) or not item.strip()):
            raise PersonCandidateError(f"SIGHTING_FIELD_INVALID:{field}")
    return sighting


def _name_set(sighting: Dict[str, Any]) -> set:
    """Normalized display name plus normalized nicknames."""
    names = {normalize_name(sighting["display_name"])}
    for nick in sighting.get("nicknames", []):
        names.add(normalize_name(nick))
    return names


def _validate_verified_ids(verified_external_ids: Any) -> frozenset:
    """Normalize the trusted upstream's verified-authoritative ID set (B4).

    This set is the ONLY route by which an observed ``external_id``
    gains authority. It must be produced by the trusted upstream from
    a real, namespaced registry cross-check — never copied out of the
    sightings under assessment.
    """
    if verified_external_ids is None:
        return frozenset()
    if isinstance(verified_external_ids, str) or not isinstance(
        verified_external_ids, Iterable
    ):
        raise PersonCandidateError("VERIFIED_EXTERNAL_IDS_INVALID:not_a_collection")
    cleaned = set()
    for item in verified_external_ids:
        if not isinstance(item, str) or not item.strip():
            raise PersonCandidateError(
                "VERIFIED_EXTERNAL_IDS_INVALID:entries_must_be_non_empty_strings")
        cleaned.add(item.strip())
    return frozenset(cleaned)


def _identity_conflicts(
    left_s: Dict[str, Any], right_s: Dict[str, Any]
) -> List[str]:
    """Substantive contradictions under one claimed identity (AC-24/B6).

    Evaluated BEFORE any merge verdict: display name, team, and birth
    month. One person has exactly one birth month, so a birth-month
    contradiction is a conflict even when every other field agrees.
    """
    conflicts: List[str] = []
    if normalize_name(left_s["display_name"]) != normalize_name(
        right_s["display_name"]
    ):
        conflicts.append("display_name")
    left_team = (left_s.get("team") or "").strip()
    right_team = (right_s.get("team") or "").strip()
    if left_team and right_team and left_team != right_team:
        conflicts.append("team")
    left_month = left_s.get("birth_month")
    right_month = right_s.get("birth_month")
    if left_month and right_month and left_month != right_month:
        conflicts.append("birth_month")
    return conflicts


def _merged_view(sightings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Provenance-carrying grouping view. Never a Person identity."""
    names: set = set()
    refs: List[str] = []
    external_ids: set = set()
    for sighting in sightings:
        names.update(_name_set(sighting))
        refs.append(sighting["source_ref"])
        if sighting.get("external_id"):
            external_ids.add(sighting["external_id"])
    view: Dict[str, Any] = {
        "display_names": sorted(names),
        "source_refs": sorted(set(refs)),
        "sighting_count": len(sightings),
    }
    if external_ids:
        view["external_id"] = sorted(external_ids)[0] if len(external_ids) == 1 else sorted(external_ids)
    return view


def assess_pair(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    human_decision: Optional[AuthorizedHumanDecision] = None,
    trusted_link: Optional[AuthorizedTrustedLink] = None,
    verified_external_ids: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Assess whether two person sightings may describe the same person.

    Pure function: no registry I/O, no mutation. Precedence
    (fail-closed): authorized Human decision > authorized trusted link
    > verified authoritative external ID > name/birth-month evidence.
    Unverified external IDs are observation evidence only (B4); plain
    dict decisions/links are rejected, never honored (B5). The module
    never asserts a Person identity: ``identity_assertion`` is always
    ``"NONE"``.
    """
    left_s = _validate_sighting(left)
    right_s = _validate_sighting(right)
    verified_ids = _validate_verified_ids(verified_external_ids)
    left_ref = left_s["source_ref"]
    right_ref = right_s["source_ref"]

    def result(
        verdict: str,
        reason: str,
        *,
        ambiguity_signals: Optional[List[str]] = None,
        conflict_fields: Optional[List[str]] = None,
        merged: bool = False,
    ) -> Dict[str, Any]:
        status = (
            REVIEW_REQUIRED
            if verdict == AMBIGUOUS
            else CONFLICT_STATUS
            if verdict == CONFLICT
            else RESOLVED
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "verdict": verdict,
            "verdict_reason": reason,
            "status": status,
            "identity_assertion": "NONE",
            "automatic_merge_allowed": verdict == MERGE_ALLOWED,
            "ambiguity_signals": sorted(ambiguity_signals or []),
            "conflict_fields": sorted(conflict_fields or []),
            "merged_view": _merged_view([left_s, right_s]) if merged else None,
            "sightings": [left_s, right_s],
            "left_source_ref": left_ref,
            "right_source_ref": right_ref,
        }

    # Authorized Human decision is the final arbiter (rule 3, B5):
    # honored ONLY as an AuthorizedHumanDecision from the trusted
    # channel. Anything else claiming to be a decision fails closed.
    if human_decision is not None:
        if not isinstance(human_decision, AuthorizedHumanDecision):
            raise PersonCandidateError(
                "HUMAN_DECISION_UNTRUSTED_CHANNEL:plain declarations are "
                "not decisions; an AuthorizedHumanDecision from the "
                "trusted upstream is required (T2-8 wiring pending)")
        if human_decision.link == "same":
            return result(MERGE_ALLOWED, "HUMAN_DECISION_SAME", merged=True)
        return result(DISTINCT, "HUMAN_DECISION_NOT_SAME")

    # An authorized trusted association permits merge (rule 3, B5).
    if trusted_link is not None:
        if not isinstance(trusted_link, AuthorizedTrustedLink):
            raise PersonCandidateError(
                "TRUSTED_LINK_UNTRUSTED_CHANNEL:plain declarations are "
                "not trusted links; an AuthorizedTrustedLink from the "
                "trusted upstream is required (T2-8 wiring pending)")
        return result(MERGE_ALLOWED, "TRUSTED_ASSOCIATION", merged=True)

    left_id = left_s.get("external_id")
    right_id = right_s.get("external_id")
    id_signal: Optional[str] = None
    if left_id and right_id:
        if left_id == right_id:
            # Same claimed ID: substantive contradictions are AC-24
            # conflicts in EVERY verification state (B6) — checked
            # before any merge verdict is considered.
            conflicts = _identity_conflicts(left_s, right_s)
            if conflicts:
                return result(
                    CONFLICT, "CONFLICT_SAME_ID_CONTRADICTORY_FIELDS",
                    conflict_fields=conflicts)
            if left_id in verified_ids:
                return result(
                    MERGE_ALLOWED, "AUTHORITATIVE_EXTERNAL_ID_MATCH",
                    merged=True)
            # B4: an unverified ID match is observation evidence only.
            id_signal = "UNVERIFIED_EXTERNAL_ID_MATCH"
        else:
            if left_id in verified_ids and right_id in verified_ids:
                return result(DISTINCT, "DIFFERENT_EXTERNAL_IDS")
            # B4: differing UNVERIFIED IDs prove nothing either way.
            id_signal = "UNVERIFIED_EXTERNAL_IDS_DIFFER"

    left_names = _name_set(left_s)
    right_names = _name_set(right_s)
    name_equal = normalize_name(left_s["display_name"]) == normalize_name(right_s["display_name"])
    name_overlap = bool(left_names & right_names)

    if not name_overlap:
        return result(DISTINCT, "DIFFERENT_NAMES")

    # Related names: a differing birth month is dispositive evidence of
    # two different people; agreeing month/team never resolves (rule 1).
    left_month = left_s.get("birth_month")
    right_month = right_s.get("birth_month")
    if left_month and right_month and left_month != right_month:
        return result(DISTINCT, "DIFFERENT_BIRTH_MONTHS")

    signals: List[str] = []
    left_source = (left_s.get("source_id") or "").strip()
    right_source = (right_s.get("source_id") or "").strip()
    if name_equal:
        if left_source and left_source == right_source:
            # B2: same name + same source is an ambiguity SIGNAL only —
            # shared provenance never proves shared identity.
            signals.append("SAME_NAME_SAME_SOURCE")
        else:
            signals.append("SAME_NAME_DIFFERENT_SOURCE")
    else:
        signals.append("NICKNAME_OVERLAP")
    if left_month and right_month and left_month == right_month:
        signals.append("SAME_BIRTH_MONTH_NOT_DISAMBIGUATING")
    left_team = (left_s.get("team") or "").strip()
    right_team = (right_s.get("team") or "").strip()
    if left_team and right_team and left_team == right_team:
        signals.append("SAME_TEAM_NOT_DISAMBIGUATING")
    if id_signal:
        signals.append(id_signal)
    return result(AMBIGUOUS, "AMBIGUOUS_" + signals[0], ambiguity_signals=signals)


def _pair_key(ref_a: str, ref_b: str) -> Tuple[str, str]:
    return (ref_a, ref_b) if ref_a <= ref_b else (ref_b, ref_a)


def _index_key(i: int, j: int) -> Tuple[int, int]:
    """Canonical (min, max) index pair (B1): storage and retrieval of
    pair verdicts use the SAME key regardless of input ordering or
    source_ref sort order."""
    return (i, j) if i < j else (j, i)


def assess_batch(
    sightings: List[Mapping[str, Any]],
    *,
    human_decisions: Optional[Mapping[Tuple[str, str], AuthorizedHumanDecision]] = None,
    trusted_links: Optional[Mapping[Tuple[str, str], AuthorizedTrustedLink]] = None,
    verified_external_ids: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Group sightings into candidate groups with fail-closed verdicts.

    Union-find over pairwise assessments: MERGE_ALLOWED/AMBIGUOUS/CONFLICT
    edges join a group; DISTINCT edges never join. Group verdicts are
    invariant under input permutation (B1, canonical pair keys).

    Cannot-link constraints (B3): a DISTINCT pair inside a group blocks
    merging unconditionally. A hard DISTINCT verdict (verified-ID
    mismatch, authorized Human ``not_same``, birth-month contradiction)
    makes the whole group a CONFLICT group — evidence pulls the records
    together while authoritative/factual evidence says they are apart,
    so both records are retained under conflict status for Human
    arbitration. Every sighting lands in exactly one group; nothing is
    silently dropped.
    """
    validated = [_validate_sighting(s) for s in sightings]
    refs = [s["source_ref"] for s in validated]
    if len(set(refs)) != len(refs):
        raise PersonCandidateError("BATCH_DUPLICATE_SOURCE_REF")
    verified_ids = _validate_verified_ids(verified_external_ids)

    human_decisions = dict(human_decisions or {})
    trusted_links = dict(trusted_links or {})

    parent = list(range(len(validated)))
    edge_verdict: Dict[Tuple[int, int], str] = {}
    edge_reason: Dict[Tuple[int, int], str] = {}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    n = len(validated)
    for i in range(n):
        for j in range(i + 1, n):
            key = _pair_key(refs[i], refs[j])
            out = assess_pair(
                validated[i],
                validated[j],
                human_decision=human_decisions.get(key),
                trusted_link=trusted_links.get(key),
                verified_external_ids=verified_ids,
            )
            edge_verdict[_index_key(i, j)] = out["verdict"]
            edge_reason[_index_key(i, j)] = out["verdict_reason"]
            if out["verdict"] in _GROUPING_VERDICTS:
                union(i, j)

    members: Dict[int, List[int]] = {}
    for i in range(n):
        members.setdefault(find(i), []).append(i)

    rank = {MERGE_ALLOWED: 0, AMBIGUOUS: 1, CONFLICT: 2}
    groups: List[Dict[str, Any]] = []
    for root in sorted(members, key=lambda r: min(refs[i] for i in members[r])):
        idxs = sorted(members[root], key=lambda i: refs[i])
        pair_verdicts: Dict[Tuple[int, int], str] = {}
        for a_pos in range(len(idxs)):
            for b_pos in range(a_pos + 1, len(idxs)):
                pair = _index_key(idxs[a_pos], idxs[b_pos])
                if pair in edge_verdict:
                    pair_verdicts[pair] = edge_verdict[pair]
        hard_distinct = [
            p for p, v in pair_verdicts.items()
            if v == DISTINCT and edge_reason[p] in _HARD_DISTINCT_REASONS
        ]
        any_distinct = [p for p, v in pair_verdicts.items() if v == DISTINCT]
        grouping = [v for v in pair_verdicts.values() if v in _GROUPING_VERDICTS]
        worst = max(grouping, key=lambda v: rank[v]) if grouping else None

        if len(idxs) == 1:
            verdict, status = SINGLETON, RESOLVED
            reasons = set()
        elif hard_distinct:
            # B3: cannot-link violated inside the group. Never a merge;
            # the contradiction is surfaced as CONFLICT with all
            # records retained for Human arbitration.
            verdict, status = CONFLICT, CONFLICT_STATUS
            reasons = {edge_reason[p] for p in hard_distinct}
            reasons.add("CANNOT_LINK_VIOLATED")
        elif worst == CONFLICT:
            verdict, status = CONFLICT, CONFLICT_STATUS
            reasons = {
                edge_reason[p] for p, v in pair_verdicts.items() if v == CONFLICT
            }
        elif worst == AMBIGUOUS:
            # An ambiguous edge dispositions the whole group to
            # REVIEW_REQUIRED (never auto-merge).
            verdict, status = REVIEW_REQUIRED, REVIEW_REQUIRED
            reasons = {
                edge_reason[p] for p, v in pair_verdicts.items() if v == AMBIGUOUS
            }
        elif any_distinct:
            # B3: even a soft DISTINCT pair inside the group blocks
            # merging; the group degrades to REVIEW_REQUIRED.
            verdict, status = REVIEW_REQUIRED, REVIEW_REQUIRED
            reasons = {
                edge_reason[p] for p, v in pair_verdicts.items()
                if v in (MERGE_ALLOWED, DISTINCT)
            }
        else:
            verdict, status = MERGE_ALLOWED, RESOLVED
            reasons = {
                edge_reason[p] for p, v in pair_verdicts.items()
                if v == MERGE_ALLOWED
            }
        group_sightings = [validated[i] for i in idxs]
        digest = hashlib.sha256(
            "|".join(sorted(refs[i] for i in idxs)).encode("utf-8")
        ).hexdigest()[:16]
        groups.append({
            "group_id": f"grp-{digest}",
            "verdict": verdict,
            "verdict_reasons": sorted(reasons),
            "status": status,
            "identity_assertion": "NONE",
            "automatic_merge_allowed": verdict == MERGE_ALLOWED,
            "sightings": group_sightings,
            "merged_view": _merged_view(group_sightings) if verdict == MERGE_ALLOWED else None,
        })

    batch_status = RESOLVED
    for group in groups:
        if group["status"] == CONFLICT_STATUS:
            batch_status = CONFLICT_STATUS
            break
        if group["status"] == REVIEW_REQUIRED:
            batch_status = REVIEW_REQUIRED

    return {
        "schema_version": SCHEMA_VERSION,
        "status": batch_status,
        "identity_assertion": "NONE",
        "automatic_merge_allowed": False,
        "group_count": len(groups),
        "sighting_count": n,
        "groups": groups,
    }
