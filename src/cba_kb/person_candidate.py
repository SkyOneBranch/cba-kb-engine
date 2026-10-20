"""Person Candidate layer: cross-source identity ambiguity resolution.

This module NEVER promotes a candidate to a confirmed Person identity.
Ambiguous inputs (same name across sources, nickname overlap, same birth
month, same team) yield ``REVIEW_REQUIRED``; only an authoritative
external ID association, a validated trusted association, or an explicit
Human decision permits merging. Dual-source conflicts keep BOTH records
plus conflict status (AC-24); last-write-wins is forbidden.

Frozen contracts honored (Base db47586, v2.0.5 T2-4):
- AC-04~07: candidate-only output never leaks or asserts a Person
  identity (``identity_assertion`` is always ``"NONE"``; no ``player_uid``
  is ever emitted).
- AC-23: titles/aliases ("大狙"/"小卢"-style nicknames) are searchable
  candidate signals only; a nickname hit never resolves a Person ID.
- AC-24: contradictory fields under one authoritative identity keep
  both source records plus conflict status. No last-write-wins.
- AC-05: malformed input -> REJECTED (PersonCandidateError); ambiguity ->
  REVIEW_REQUIRED; nothing is silently dropped or fabricated.
- Name normalization reuses ``player_identity.normalize_name`` (NFKC,
  whitespace collapse) so "same name" means exactly what the identity
  registry means. The ``player_identity`` WRITE path is never touched:
  this module performs no registry mutation and emits no player_uid.
- ``merged_view`` is a provenance-carrying candidate grouping, NOT a
  Person identity promotion. Human review remains the final arbiter.

T2-8 will wire registry lookups; this module takes sightings as plain
dicts and performs no registry I/O.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

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


def _validate_sighting(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise PersonCandidateError("SIGHTING_NOT_A_DICT")
    sighting = dict(value)
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


def _validate_trusted_link(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise PersonCandidateError("TRUSTED_LINK_INVALID:not_a_dict")
    method = value.get("method")
    if method not in _TRUSTED_METHODS:
        raise PersonCandidateError("TRUSTED_LINK_INVALID:method")
    if value.get("confidence") != "HIGH":
        raise PersonCandidateError("TRUSTED_LINK_INVALID:confidence_must_be_HIGH")
    refs = value.get("evidence_refs")
    if (
        not isinstance(refs, list)
        or not refs
        or any(not isinstance(r, str) or not r.strip() for r in refs)
    ):
        raise PersonCandidateError("TRUSTED_LINK_INVALID:evidence_refs_required")
    return {
        "method": method,
        "confidence": "HIGH",
        "evidence_refs": sorted({r.strip() for r in refs}),
    }


def _validate_human_decision(value: Any) -> str:
    if not isinstance(value, dict) or value.get("link") not in ("same", "not_same"):
        raise PersonCandidateError("HUMAN_DECISION_INVALID:link_must_be_same_or_not_same")
    return value["link"]


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
    human_decision: Optional[Mapping[str, Any]] = None,
    trusted_link: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Assess whether two person sightings may describe the same person.

    Pure function: no registry I/O, no mutation. Precedence
    (fail-closed): human decision > trusted link > authoritative external
    ID > birth-month/name evidence. The module never asserts a Person
    identity: ``identity_assertion`` is always ``"NONE"``.
    """
    left_s = _validate_sighting(left)
    right_s = _validate_sighting(right)
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

    # Human review is the final arbiter (rule 3).
    if human_decision is not None:
        link = _validate_human_decision(human_decision)
        if link == "same":
            return result(MERGE_ALLOWED, "HUMAN_DECISION_SAME", merged=True)
        return result(DISTINCT, "HUMAN_DECISION_NOT_SAME")

    # A validated trusted association permits merge (rule 3).
    if trusted_link is not None:
        _validate_trusted_link(trusted_link)
        return result(MERGE_ALLOWED, "TRUSTED_ASSOCIATION", merged=True)

    left_id = left_s.get("external_id")
    right_id = right_s.get("external_id")
    if left_id and right_id:
        if left_id != right_id:
            return result(DISTINCT, "DIFFERENT_EXTERNAL_IDS")
        # Same authoritative ID: fields must agree, else AC-24 conflict.
        conflicts: List[str] = []
        left_name = normalize_name(left_s["display_name"])
        right_name = normalize_name(right_s["display_name"])
        if left_name != right_name:
            conflicts.append("display_name")
        left_team = (left_s.get("team") or "").strip()
        right_team = (right_s.get("team") or "").strip()
        if left_team and right_team and left_team != right_team:
            conflicts.append("team")
        if conflicts:
            return result(CONFLICT, "CONFLICT_SAME_ID_CONTRADICTORY_FIELDS",
                          conflict_fields=conflicts)
        return result(MERGE_ALLOWED, "AUTHORITATIVE_EXTERNAL_ID_MATCH", merged=True)

    left_names = _name_set(left_s)
    right_names = _name_set(right_s)
    name_equal = normalize_name(left_s["display_name"]) == normalize_name(right_s["display_name"])
    name_overlap = bool(left_names & right_names)

    if not name_overlap:
        return result(DISTINCT, "DIFFERENT_NAMES")

    # Related names: a differing birth month is dispositive (one person,
    # one birth month); agreeing month/team never resolves (rule 1).
    left_month = left_s.get("birth_month")
    right_month = right_s.get("birth_month")
    if left_month and right_month and left_month != right_month:
        return result(DISTINCT, "DIFFERENT_BIRTH_MONTHS")

    signals: List[str] = []
    left_source = (left_s.get("source_id") or "").strip()
    right_source = (right_s.get("source_id") or "").strip()
    if name_equal and left_source and left_source == right_source:
        return result(MERGE_ALLOWED, "SAME_NAME_SAME_SOURCE", merged=True)
    if name_equal:
        signals.append("SAME_NAME_DIFFERENT_SOURCE")
    else:
        signals.append("NICKNAME_OVERLAP")
    if left_month and right_month and left_month == right_month:
        signals.append("SAME_BIRTH_MONTH_NOT_DISAMBIGUATING")
    left_team = (left_s.get("team") or "").strip()
    right_team = (right_s.get("team") or "").strip()
    if left_team and right_team and left_team == right_team:
        signals.append("SAME_TEAM_NOT_DISAMBIGUATING")
    return result(AMBIGUOUS, "AMBIGUOUS_" + signals[0], ambiguity_signals=signals)


def _pair_key(ref_a: str, ref_b: str) -> Tuple[str, str]:
    return (ref_a, ref_b) if ref_a <= ref_b else (ref_b, ref_a)


def assess_batch(
    sightings: List[Mapping[str, Any]],
    *,
    human_decisions: Optional[Mapping[Tuple[str, str], Mapping[str, Any]]] = None,
    trusted_links: Optional[Mapping[Tuple[str, str], Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Group sightings into candidate groups with fail-closed verdicts.

    Union-find over pairwise assessments: MERGE_ALLOWED/AMBIGUOUS/CONFLICT
    edges join a group; DISTINCT edges never join. Group verdict is the
    worst edge verdict (CONFLICT > REVIEW_REQUIRED > clean). Every
    sighting lands in exactly one group; nothing is silently dropped.
    Conflict groups keep BOTH records and carry no merged view (AC-24).
    """
    validated = [_validate_sighting(s) for s in sightings]
    refs = [s["source_ref"] for s in validated]
    if len(set(refs)) != len(refs):
        raise PersonCandidateError("BATCH_DUPLICATE_SOURCE_REF")

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
            )
            edge_verdict[(i, j)] = out["verdict"]
            edge_reason[(i, j)] = out["verdict_reason"]
            if out["verdict"] in _GROUPING_VERDICTS:
                union(i, j)

    members: Dict[int, List[int]] = {}
    for i in range(n):
        members.setdefault(find(i), []).append(i)

    rank = {MERGE_ALLOWED: 0, AMBIGUOUS: 1, CONFLICT: 2}
    groups: List[Dict[str, Any]] = []
    for root in sorted(members, key=lambda r: min(refs[i] for i in members[r])):
        idxs = sorted(members[root], key=lambda i: refs[i])
        worst = MERGE_ALLOWED
        reasons: set = set()
        for a_pos in range(len(idxs)):
            for b_pos in range(a_pos + 1, len(idxs)):
                v = edge_verdict.get((idxs[a_pos], idxs[b_pos]))
                if v is None:
                    continue
                if rank.get(v, -1) > rank.get(worst, -1):
                    worst = v
        for a_pos in range(len(idxs)):
            for b_pos in range(a_pos + 1, len(idxs)):
                v = edge_verdict.get((idxs[a_pos], idxs[b_pos]))
                if v is not None and v == worst:
                    reasons.add(edge_reason[(idxs[a_pos], idxs[b_pos])])
        group_sightings = [validated[i] for i in idxs]
        if len(idxs) == 1:
            verdict, status = SINGLETON, RESOLVED
        elif worst == CONFLICT:
            verdict, status = CONFLICT, CONFLICT_STATUS
        elif worst == AMBIGUOUS:
            # Group disposition vocabulary: an ambiguous edge dispositions
            # the whole group to REVIEW_REQUIRED (never auto-merge).
            verdict, status = REVIEW_REQUIRED, REVIEW_REQUIRED
        else:
            verdict, status = MERGE_ALLOWED, RESOLVED
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
