"""kc/0.1 business-semantic contract and target-first safe projection.

CBA-KB v2.1.0 slice A (Issue #215), REQ-210-KNOWLEDGE-CONSUMPTION-01,
LACD_v1.0.2. Producer-side contract only: no Drive writes, no ACL
grant/revoke, no production publication, no network, no local Engine
execution at query time. All inputs are plain data; any instruction-like
text inside a document body is data and is never executed.

Reuse note: canonical Stats validation is delegated to
``cba_kb.stats.validate_stats_record`` (PLAYER_SEASON_STATS grain,
0 != null, WHOLE_SEASON vs TEAM_SPLIT separation, trusted identity link).
Identity SAME/NOT_SAME/UNDECIDED semantics follow
``cba_kb.player_identity`` (exact name/alias only, no auto-merge) and
``cba_kb.consumer_projection`` (SAME/NOT_SAME/UNDECIDED/UNAVAILABLE/
NOT_MATERIALIZED). Grain labels follow ``cba_kb.research_view``
(non-overridable authoritative grain).

Digest discipline (never conflated):
- KC_PASSAGE_SHA256: SHA-256 over canonical JSON of
  {source_id, revision, locator, text} — the digest field itself is
  excluded by construction (it is computed, then attached).
- KC_SNAPSHOT_SHA256: SHA-256 over canonical JSON of snapshot content
  (entries, target_domain, capabilities, build_provenance) — snapshot_id
  and digest fields excluded by construction.
- A Git blob SHA (40 hex), Git commit SHA (40 hex), file SHA-256 and a
  spec JSON digest are different kinds with different covered bytes;
  this module never equates them. Reading a digest string is NOT
  computing a digest: validator receipts record producer_validated,
  connector_returned and client_observed separately.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Callable, Dict, List, Optional

from .stats import (DEFAULT_COMPETITION, PROVIDER_CBA_OFFICIAL,
                    validate_stats_record)

GRAINS = frozenset({
    "STATS", "IDENTITY", "CANONICAL_FACT", "DOCUMENT",
    "STATEMENT", "CLAIM", "UNKNOWN",
})
IDENTITY_RELATIONS = frozenset({"SAME", "NOT_SAME", "UNDECIDED"})
STATUSES = frozenset({
    "OK", "PARTIAL", "UNKNOWN", "CONFLICT", "STALE", "NOT_APPLICABLE",
    "CLARIFICATION_REQUIRED",
})
_PERCENTAGE_RE = re.compile(r"^\d+(\.\d+)?%$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class KcError(RuntimeError):
    """Fail-closed kc/0.1 contract violation."""


def canonical_json(value: Any) -> bytes:
    """Deterministic canonical bytes: sorted keys, compact separators."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(float(value))


def validate_metric_value(metric: str, value: Any, unit: str) -> Dict[str, Any]:
    """Classify one metric value; percentage and ratio are distinct types.

    - ``*_percentage`` metrics: value MUST be a percent string ("45.0%").
    - ``*_percentage_rate`` metrics: value MUST be a ratio float in [0, 1].
    - Other metrics: finite number, bool rejected, NaN/inf rejected.
    """
    if isinstance(value, bool):
        raise KcError(f"INVALID_METRIC_VALUE_BOOL:{metric}")
    if metric.endswith("_percentage"):
        if not isinstance(value, str) or not _PERCENTAGE_RE.match(value):
            raise KcError(f"INVALID_PERCENTAGE:{metric}={value!r}")
        return {"metric": metric, "value": value, "unit": unit,
                "value_kind": "PERCENTAGE"}
    if metric.endswith("_percentage_rate"):
        if not _is_number(value) or not (0.0 <= float(value) <= 1.0):
            raise KcError(f"INVALID_RATIO:{metric}={value!r}")
        return {"metric": metric, "value": value, "unit": unit,
                "value_kind": "RATIO"}
    if not _is_number(value):
        raise KcError(f"INVALID_METRIC_VALUE:{metric}={value!r}")
    return {"metric": metric, "value": value, "unit": unit,
            "value_kind": "SCALAR"}


def typed_result(*, caller_grain: str, authoritative_grain: str,
                 payload: Dict[str, Any]) -> Dict[str, Any]:
    """Authoritative grain wins; a caller-supplied grain can never spoof it.

    Q2-fix (PR #216 round 1): the authoritative grain is applied AFTER
    the payload is copied, so a payload ``grain`` key (DOCUMENT, CLAIM,
    or any other) is discarded, never allowed to overwrite it. The
    caller_grain argument is accepted for call-site context only and
    never influences the result grain.
    """
    if authoritative_grain not in GRAINS:
        raise KcError(f"INVALID_GRAIN:{authoritative_grain}")
    result = dict(payload)
    result["grain"] = authoritative_grain
    return result


def stats_key(*, player_uid: str, provider: str, competition: str,
              season: str, match_type: str, scope: str,
              team_id: Optional[str], metric: str, value: Any,
              unit: str, provenance: Any) -> Dict[str, Any]:
    """The full Stats binding key. No dimension may be silently defaulted."""
    if scope == "TEAM_SPLIT" and not team_id:
        raise KcError("TEAM_SPLIT_REQUIRES_TEAM_ID")
    return {
        "player_uid": player_uid, "provider": provider,
        "competition": competition, "season": season,
        "match_type": match_type, "scope": scope,
        "team_id": team_id if scope == "TEAM_SPLIT" else None,
        "metric": metric, "value": value, "unit": unit,
        "provenance": provenance,
    }


def query_stats(records: List[Dict[str, Any]], *, player_uid: str,
                season: str, match_type: str, scope: str, metric: str,
                provider: str, competition: str,
                team_id: Optional[str] = None) -> Dict[str, Any]:
    """Exact-key Stats lookup. No fallback across season, match_type,
    scope, team, player, provider or competition. Missing dimension ->
    UNKNOWN, never a guess.

    Q2-fix (PR #216 round 2): provider and competition are REQUIRED
    keyword-only arguments with NO defaults, matching the full binding
    in ``stats_key`` and the frozen no-silent-default invariant.
    Omitting either raises TypeError at call time, before any lookup,
    so a caller can never silently obtain the canonical binding: the
    canonical binding (CBA_OFFICIAL / CBA) must be passed explicitly.
    A record whose raw provider/competition differs from the requested
    binding is skipped BEFORE canonical validation, so a mixed corpus
    can never produce a false CONFLICT, a cross-provider fallback, or
    a validation crash from the non-matching record. An explicit
    non-canonical binding returns UNKNOWN immediately, without
    validating non-canonical records: the canonical validator admits
    exactly CBA_OFFICIAL / CBA, so such a binding is provably empty
    and no verified record can match it. After validation the binding
    is re-checked on the validated record as defence in depth.
    """
    if scope not in ("WHOLE_SEASON", "TEAM_SPLIT"):
        raise KcError(f"INVALID_SCOPE:{scope}")
    if scope == "TEAM_SPLIT" and not team_id:
        return {"status": "UNKNOWN", "grain": "STATS",
                "reason": "TEAM_SPLIT_REQUIRES_TEAM_ID"}
    if provider != PROVIDER_CBA_OFFICIAL or competition != DEFAULT_COMPETITION:
        return {"status": "UNKNOWN", "grain": "STATS",
                "reason": "NON_CANONICAL_PROVIDER_COMPETITION_BINDING"}
    matches = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("provider") != provider:
            continue
        if record.get("competition") != competition:
            continue
        validated = validate_stats_record(record)
        if validated.get("provider") != provider:
            continue
        if validated.get("competition") != competition:
            continue
        if validated["player_uid"] != player_uid:
            continue
        if validated["season"] != season:
            continue
        if validated["match_type"] != match_type:
            continue
        if validated["scope"] != scope:
            continue
        if scope == "TEAM_SPLIT" and validated.get("team_id") != team_id:
            continue
        if metric not in validated["metrics"]:
            continue
        matches.append(validated)
    if not matches:
        return {"status": "UNKNOWN", "grain": "STATS",
                "reason": "NO_VERIFIED_RECORD_FOR_EXACT_KEY"}
    if len(matches) > 1:
        return {"status": "CONFLICT", "grain": "STATS",
                "reason": "MULTIPLE_RECORDS_FOR_EXACT_KEY"}
    record = matches[0]
    value = record["metrics"][metric]
    if value is None:
        # Null is unknown, never coerced to 0.
        return {"status": "UNKNOWN", "grain": "STATS",
                "reason": "METRIC_VALUE_NULL",
                "key": stats_key(player_uid=player_uid,
                                 provider=record["provider"],
                                 competition=record["competition"],
                                 season=season, match_type=match_type,
                                 scope=scope, team_id=team_id,
                                 metric=metric, value=None,
                                 unit=_metric_unit(metric),
                                 provenance=record["provenance"]),
                "value": None, "unit": _metric_unit(metric),
                "source_record_id": record["record_id"]}
    typed = validate_metric_value(metric, value, _metric_unit(metric))
    return {"status": "OK", "grain": "STATS",
            "key": stats_key(player_uid=player_uid,
                             provider=record["provider"],
                             competition=record["competition"],
                             season=season, match_type=match_type,
                             scope=scope, team_id=team_id,
                             metric=metric, value=value,
                             unit=typed["unit"],
                             provenance=record["provenance"]),
            "value": value, "unit": typed["unit"],
            "value_kind": typed["value_kind"],
            "source_record_id": record["record_id"]}


def _metric_unit(metric: str) -> str:
    if metric.endswith("_percentage"):
        return "percent"
    if metric.endswith("_percentage_rate"):
        return "ratio"
    if metric == "games_played" or metric == "games_started":
        return "games"
    return "per_game"


def query_stats_from_document(*, document_kind: str,
                              requested_metric: str) -> Dict[str, Any]:
    """A Document cannot answer a Stats question: NOT_APPLICABLE, not a
    guessed number and not UNKNOWN-as-missing-data."""
    return {"status": "NOT_APPLICABLE", "grain": "STATS",
            "reason": "DOCUMENT_REQUESTING_STATS",
            "document_kind": document_kind,
            "requested_metric": requested_metric}


def resolve_identity(players: List[Dict[str, Any]],
                     aliases: List[Dict[str, Any]], *, query: str,
                     query_kind: str) -> Dict[str, Any]:
    """Exact name/alias only. An approved alias binds to its owner only;
    a canonical name matching several visible UIDs returns ALL of them
    with relation UNDECIDED and clarification required — never the most
    similar pick, never an auto-merge."""
    if query_kind == "alias":
        approved = [a for a in aliases
                    if a["alias"] == query and a.get("approved") is True]
        candidates = sorted({a["player_uid"] for a in approved})
        if not candidates:
            return {"status": "UNKNOWN", "grain": "IDENTITY",
                    "candidates": [], "relation": "UNDECIDED",
                    "reason": "NO_APPROVED_ALIAS_BINDING"}
        if len(candidates) > 1:
            # Q2-fix (PR #216 round 1): a multi-owner approved alias is
            # ambiguous exactly like a multi-candidate canonical name.
            # It must return ALL candidates with CLARIFICATION_REQUIRED
            # and relation UNDECIDED, never RESOLVED to a single pick.
            return {"status": "CLARIFICATION_REQUIRED", "grain": "IDENTITY",
                    "candidates": candidates, "relation": "UNDECIDED",
                    "reason": "MULTIPLE_APPROVED_ALIAS_BINDINGS"}
        return {"status": "RESOLVED", "grain": "IDENTITY",
                "candidates": candidates, "relation": "SINGLE_CANDIDATE"}
    if query_kind == "name":
        candidates = sorted(p["player_uid"] for p in players
                            if p["canonical_name"] == query)
        if not candidates:
            return {"status": "UNKNOWN", "grain": "IDENTITY",
                    "candidates": [], "relation": "UNDECIDED",
                    "reason": "NO_VISIBLE_CANDIDATE"}
        if len(candidates) > 1:
            return {"status": "CLARIFICATION_REQUIRED", "grain": "IDENTITY",
                    "candidates": candidates, "relation": "UNDECIDED"}
        return {"status": "RESOLVED", "grain": "IDENTITY",
                "candidates": candidates, "relation": "SINGLE_CANDIDATE"}
    raise KcError(f"INVALID_IDENTITY_QUERY_KIND:{query_kind}")


def declare_identity_relation(uid_a: str, uid_b: str, relation: str,
                              *, evidence: List[str]) -> Dict[str, Any]:
    """Formal identity relations only. SAME/NOT_SAME/UNDECIDED, with
    non-empty evidence; an empty-evidence SAME is a refused auto-merge."""
    if relation not in IDENTITY_RELATIONS:
        raise KcError(f"INVALID_IDENTITY_RELATION:{relation}")
    if not evidence:
        raise KcError("IDENTITY_RELATION_EVIDENCE_REQUIRED")
    if uid_a == uid_b and relation != "SAME":
        raise KcError("SELF_RELATION_MUST_BE_SAME")
    return {"player_uid_a": uid_a, "player_uid_b": uid_b,
            "relation": relation, "evidence": list(evidence),
            "grain": "IDENTITY"}


def resolve_claims(claims: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Claim-layer conflict only. Groups by (player_uid, date); distinct
    texts for the same subject+date are CONFLICT. Claims never mutate
    Stats, Facts or Identity — F-01 is enforced by the caller keeping
    grains separate, and tested as such."""
    groups: Dict[Any, List[Dict[str, Any]]] = {}
    for claim in claims:
        groups.setdefault((claim["player_uid"], claim["date"]), []).append(claim)
    conflicting: List[str] = []
    for members in groups.values():
        texts = {m["text"] for m in members}
        if len(texts) > 1:
            conflicting.extend(sorted(m["claim_id"] for m in members))
    if conflicting:
        return {"status": "CONFLICT", "grain": "CLAIM",
                "conflicting_claim_ids": sorted(set(conflicting))}
    return {"status": "OK", "grain": "CLAIM", "conflicting_claim_ids": []}


# ------------------------------------------------- v2.1.1 T2-1 (Issue #226):
# Event-aware claim resolution (Freeze Delta Candidate Δ-02, C + A).
#
# resolve_claims() above is KEPT unchanged as the legacy heuristic:
# it groups by (player_uid, date) and can neither find a same-event
# conflict across different dates nor prove that different dates mean
# different events. resolve_claims_by_event() is the new entry,
# strengthened by the post-freeze Q2 (2026-10-31) B-03/B-04 fixes and
# by the Human-authorized Q2-227-02/03 repair (2026-10-31):
# - A claim carries a TRUSTED event association only when its
#   event_association dict is a FORMAL assertion: a non-empty
#   event_ref, a non-empty evidence list, AND source_id, revision,
#   locator and a digest, with the association's source_id bound
#   to this claim's source_id and its player_uid bound to this
#   claim's player_uid. Bare declarations (event_ref + evidence
#   strings only) are NOT formal proof. An event_key alone is a
#   candidate index, never proof: it must not group claims, and it
#   must not separate them.
# - The digest is NO LONGER format-only: it must equal
#   event_association_digest() recomputed over the bound content
#   (claim_id, player_uid, source_id, revision, locator,
#   event_ref, evidence, snapshot_id). A merely well-formed
#   64-hex string (e.g. a fixed "f"*64) is NOT a trusted
#   association. This binds the digest to this claim and this
#   snapshot instead of letting any formatted string stand in for
#   an assertion. The digest proves CONTENT BINDING only: it
#   does NOT prove the association's formal identity, because
#   anyone can recompute a digest over content they invented.
#   Formal association identity requires the consumer path's
#   passage anchoring (below). It does NOT prove the assertion
#   against an event registry — no such registry exists in this
#   data model, and building one would be a Truth Store, out of
#   scope; the assertion form itself is what contract §3.1
#   allows, and the recomputation is what makes it checkable
#   rather than self-declared.
# - When the consumer path supplies authorized_sources,
#   snapshot_sources and snapshot_id, the association's source
#   must additionally be a member of both sets and its
#   snapshot_id must equal the bound snapshot (contract §3.1(2)).
#   A standalone call without context enforces the recomputed
#   digest over the association's own snapshot_id (or none); that
#   limitation is documented, and the consumer path always
#   supplies context.
# - Provenance-binding repair (Human-authorized, 2026-10-31):
#   when the consumer path additionally supplies passage_anchors
#   — the (source_id, revision, locator) triples of its
#   digest-validated registered passages — an association is
#   trusted only when its claimed source, revision and locator
#   match one such anchor. A self-consistent digest over an
#   unregistered source passage is content binding, not a formal
#   association, and is untrusted. The anchors come from the
#   consumer's passage registry (registration already enforces
#   digest validity), so the resolver itself never re-validates
#   passage digests; a standalone call without anchors proves
#   binding only, never formal association identity.
# - Event-identity repair (Human-authorized, 2026-10-31, Q2
#   H01/H02): anchor membership alone is NOT event-identity
#   proof — it says nothing about passage content. When the
#   consumer path additionally supplies passage_texts — the
#   digest-bound texts of its registered passages, keyed by the
#   same triples — a trusted association additionally requires:
#   (a) CONTENT SUPPORT: the anchor passage's text mentions the
#   claimed event_ref (the passage content itself must state the
#   event; a triple match over ordinary info is not support);
#   (b) EVIDENCE RESOLUTION: every evidence entry must resolve
#   to a registered passage ("source_id:locator" form, and that
#   source must be authorized and a snapshot member when those
#   contexts are supplied). Arbitrary evidence strings prove
#   nothing. Under this rule, distinctness is proven by distinct
#   CONTENT-BACKED associations (each claim's own anchor text
#   states its own event_ref, refs differ, evidence disjoint) —
#   never by string inequality alone; and a same-ref CONFLICT
#   requires both associations content-backed to the SHARED ref.
#   Residual, stated plainly: mention is a content-support
#   signal, not semantic event understanding, and passage
#   authenticity itself remains a Phase-2 / real-source matter
#   (the same (a)/(b) distinction the Q2 accepted for
#   formal_registry). Same-key-different-events positive
#   identification (contract §3.1(3) bullet 1) has no in-repo
#   mechanism — no canonical event reference registry exists —
#   and is NOT faked here; it is carried as a remaining gap.
# - Trusted claims sharing (player_uid, event_ref) form one event
#   group; differing normalized business values (date, text) inside
#   one group are CONFLICT, with every participating claim listed.
# - Claims without a trusted association NO LONGER decide CONFLICT
#   through the legacy (player_uid, date) heuristic: differing
#   texts inside a legacy group are UNRESOLVED (UNKNOWN), because
#   date grouping is only a candidate heuristic and cannot prove
#   event identity, especially when date itself is disputed
#   (contract §3.1(6)). Unlabelled claims with identical values
#   keep the legacy OK.
# - Q2-227-03 repair: unlabelled pairs on DIFFERENT dates NO
#   LONGER keep the legacy OK either. Differing business values
#   for one player where any claim lacks a trusted association
#   are UNRESOLVED (EVENT_RELATIONSHIP_UNPROVEN): absence of a
#   proven link is not proof of no conflict. A proven-no-conflict
#   OK now requires positive evidence — identical values, or
#   every claim trusted under pairwise-distinct event refs.
#   Single claims and identical-value groups are unaffected.
# - When event identity is explicitly invoked (an event_key or an
#   event_association is present) but the association is unproven, or
#   when different event refs share evidence, and the business values
#   differ, the result is UNKNOWN — never a proven-no-conflict OK.
def event_association_digest(claim: Dict[str, Any],
                             assoc: Dict[str, Any]) -> str:
    """Recomputable binding digest for a formal event association:
    SHA-256 over the canonical JSON of exactly the content the
    association claims to bind — this claim's id and player, the
    association's source, revision, locator, event ref and
    evidence, and the snapshot the association claims. Any change
    to the bound content changes the digest, so a digest cannot be
    replayed onto a different claim, event, or snapshot."""
    return sha256_hex(canonical_json({
        "claim_id": claim.get("claim_id"),
        "player_uid": claim.get("player_uid"),
        "source_id": assoc.get("source_id"),
        "revision": assoc.get("revision"),
        "locator": assoc.get("locator"),
        "event_ref": assoc.get("event_ref"),
        "evidence": assoc.get("evidence"),
        "snapshot_id": assoc.get("snapshot_id"),
    }))


def _event_association_valid(claim: Dict[str, Any], *,
                             authorized_sources=None,
                             snapshot_sources=None,
                             snapshot_id=None,
                             passage_anchors=None,
                             passage_texts=None) -> bool:
    assoc = claim.get("event_association")
    if not isinstance(assoc, dict):
        return False
    ref = assoc.get("event_ref")
    evidence = assoc.get("evidence")
    if not (isinstance(ref, str) and bool(ref)
            and isinstance(evidence, list) and len(evidence) > 0
            and all(isinstance(e, str) and bool(e) for e in evidence)):
        return False
    source_id = assoc.get("source_id")
    revision = assoc.get("revision")
    locator = assoc.get("locator")
    digest = assoc.get("digest")
    hexdigits = set("0123456789abcdef")
    if not (isinstance(source_id, str) and bool(source_id)
            and isinstance(revision, str) and bool(revision)
            and isinstance(locator, str) and bool(locator)
            and isinstance(digest, str) and len(digest) == 64
            and all(ch in hexdigits for ch in digest)):
        return False
    if source_id != claim.get("source_id"):
        return False
    # Q2-227-02: the UID binding is mandatory, not conditional on
    # the association happening to carry it. A claim with a player
    # UID is trusted only under an association bound to that UID.
    if claim.get("player_uid") is not None \
            and assoc.get("player_uid") != claim.get("player_uid"):
        return False
    if authorized_sources is not None \
            and source_id not in authorized_sources:
        return False
    if snapshot_sources is not None \
            and source_id not in snapshot_sources:
        return False
    if snapshot_id is not None \
            and assoc.get("snapshot_id") != snapshot_id:
        return False
    if passage_anchors is not None \
            and (source_id, revision, locator) not in passage_anchors:
        # The claimed source passage is not a digest-validated
        # registered passage: the digest binds content the caller
        # invented, not a formal association. Untrusted.
        return False
    if passage_texts is not None:
        # Event-identity repair (2026-10-31, Q2 H01/H02): anchor
        # membership says nothing about content. The anchor
        # passage's own text must mention the claimed event_ref,
        # and every evidence entry must resolve to a registered
        # passage ("source_id:locator" form; that source must be
        # authorized and a snapshot member when those contexts are
        # supplied). An association whose evidence is arbitrary
        # strings, or whose passage never states the event, is
        # untrusted even when its digest recomputes.
        anchor_text = passage_texts.get((source_id, revision, locator))
        if not isinstance(anchor_text, str) or ref not in anchor_text:
            return False
        registered_locators = {(s, l) for (s, _r, l) in passage_texts}
        for entry in evidence:
            if not isinstance(entry, str) or ":" not in entry:
                return False
            ev_source, ev_locator = entry.split(":", 1)
            if not ev_source or not ev_locator:
                return False
            if (ev_source, ev_locator) not in registered_locators:
                return False
            if authorized_sources is not None and \
                    ev_source not in authorized_sources:
                return False
            if snapshot_sources is not None and \
                    ev_source not in snapshot_sources:
                return False
    try:
        expected = event_association_digest(claim, assoc)
    except (TypeError, ValueError):
        return False
    if digest != expected:
        return False
    return True


def _claim_business_value(claim: Dict[str, Any]):
    return (claim.get("date"), claim.get("text"))


def resolve_claims_by_event(claims: List[Dict[str, Any]], *,
                            authorized_sources=None,
                            snapshot_sources=None,
                            snapshot_id=None,
                            passage_anchors=None,
                            passage_texts=None) -> Dict[str, Any]:
    def _valid(claim: Dict[str, Any]) -> bool:
        return _event_association_valid(
            claim, authorized_sources=authorized_sources,
            snapshot_sources=snapshot_sources,
            passage_texts=passage_texts,
            snapshot_id=snapshot_id,
            passage_anchors=passage_anchors)

    conflicting: set = set()
    unresolved: set = set()
    trusted: Dict[Any, List[Dict[str, Any]]] = {}
    for claim in claims:
        if _valid(claim):
            key = (claim.get("player_uid"),
                   claim["event_association"]["event_ref"])
            trusted.setdefault(key, []).append(claim)
    for members in trusted.values():
        values = {_claim_business_value(m) for m in members}
        if len(values) > 1:
            conflicting.update(m.get("claim_id") for m in members
                               if m.get("claim_id") is not None)
    legacy: Dict[Any, List[Dict[str, Any]]] = {}
    for claim in claims:
        if not _valid(claim):
            legacy.setdefault((claim.get("player_uid"),
                               claim.get("date")), []).append(claim)
    for members in legacy.values():
        texts = {m.get("text") for m in members}
        if len(texts) > 1:
            # Post-freeze Q2 B-04 fix: the legacy heuristic must
            # NOT decide CONFLICT. Differing texts inside a legacy
            # group are undetermined (the grouping is only a
            # candidate heuristic), so they are UNRESOLVED.
            unresolved.add("LEGACY_GROUPING_UNPROVEN")
    by_player: Dict[Any, List[Dict[str, Any]]] = {}
    for claim in claims:
        by_player.setdefault(claim.get("player_uid"), []).append(claim)
    for members in by_player.values():
        if len(members) < 2:
            continue
        values = {_claim_business_value(m) for m in members}
        if len(values) <= 1:
            continue
        invoking = [m for m in members
                    if "event_key" in m or "event_association" in m]
        if invoking and any(not _valid(m) for m in invoking):
            unresolved.add("EVENT_ASSOCIATION_UNPROVEN")
        # Q2-227-03: differing business values with ANY claim
        # lacking a trusted association cannot be proven to be
        # about different events. No event fields at all is the
        # sharpest case: the old code returned a proven-no-conflict
        # OK here. Undetermined is UNKNOWN, not OK; a proven OK
        # still stands when every claim is trusted under distinct
        # event refs (no unresolved marker is added then).
        if any(not _valid(m) for m in members):
            unresolved.add("EVENT_RELATIONSHIP_UNPROVEN")
        evidence_by_ref: Dict[Any, set] = {}
        for member in members:
            if _valid(member):
                ref = member["event_association"]["event_ref"]
                evidence_by_ref.setdefault(ref, set()).update(
                    member["event_association"]["evidence"])
        refs = list(evidence_by_ref)
        for i in range(len(refs)):
            for j in range(i + 1, len(refs)):
                if evidence_by_ref[refs[i]] & evidence_by_ref[refs[j]]:
                    unresolved.add("SHARED_EVIDENCE_DIFFERENT_EVENT_KEYS")
    if conflicting:
        status = "CONFLICT"
    elif unresolved:
        status = "UNKNOWN"
    else:
        status = "OK"
    return {"status": status, "grain": "CLAIM",
            "conflicting_claim_ids": sorted(conflicting),
            "unresolved": sorted(unresolved)}


# ------------------------------------------------------------ passages
def build_passage(*, source_id: str, revision: str, locator: str,
                  text: str, player_uid: Optional[str] = None,
                  aliases: Optional[List[str]] = None) -> Dict[str, Any]:
    """A passage is a validated derived view, never a second Truth.
    Digest covers {source_id, revision, locator, text} only; the digest
    field itself is excluded by construction (computed, then attached)."""
    covered = {"source_id": source_id, "revision": revision,
               "locator": locator, "text": text}
    passage = dict(covered)
    passage["player_uid"] = player_uid
    passage["aliases"] = list(aliases or [])
    passage["digest_kind"] = "KC_PASSAGE_SHA256"
    passage["algorithm"] = "SHA-256"
    passage["covered_fields"] = ["source_id", "revision", "locator", "text"]
    passage["covered_bytes"] = (
        "UTF-8 bytes of canonical JSON of "
        "{source_id, revision, locator, text}")
    passage["digest"] = sha256_hex(canonical_json(covered))
    return passage


def validate_passage(passage: Dict[str, Any]) -> Dict[str, Any]:
    covered = {k: passage[k] for k in
               ("source_id", "revision", "locator", "text")}
    expected = sha256_hex(canonical_json(covered))
    valid = (passage.get("digest") == expected
             and passage.get("digest_kind") == "KC_PASSAGE_SHA256"
             and passage.get("algorithm") == "SHA-256")
    return {"valid": valid, "grain": "DOCUMENT",
            "digest_kind": passage.get("digest_kind"),
            "algorithm": passage.get("algorithm"),
            "covered_bytes": passage.get("covered_bytes")}


def _tokenize(text: str) -> List[str]:
    import re as _re
    tokens = [t.lower() for t in _re.findall(r"[a-z0-9]+", text.lower())]
    for run in _re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.extend(list(run))
        for i in range(len(run) - 1):
            tokens.append(run[i:i + 2])
    return tokens


def route_passages(passages: List[Dict[str, Any]], *, alias: str,
                   topic_tokens: List[str],
                   limit: Optional[int] = None) -> Dict[str, Any]:
    """Collect ALL candidates first, then rank. Relevance is the score
    (approved-alias match, then topic-token matches); source_id order is
    only a deterministic tie-breaker, never relevance. A limited page is
    PARTIAL with end_marker False — it must never be read as complete."""
    scored = []
    for passage in passages:
        score = 0
        if alias and alias in (passage.get("aliases") or []):
            score += 10
        text = passage.get("text", "")
        for token in topic_tokens:
            if token in text:
                score += 3
        if score > 0:
            scored.append((score, passage))
    # Score desc, then source_id asc as tie-breaker only.
    scored.sort(key=lambda item: (-item[0], item[1]["source_id"]))
    ordered = [p for _, p in scored]
    total = len(ordered)
    if limit is not None and total > limit:
        return {"status": "PARTIAL", "complete": False,
                "end_marker": False, "truncated": True,
                "total_candidates": total, "candidates": ordered[:limit]}
    return {"status": "COMPLETE", "complete": True,
            "end_marker": True, "truncated": False,
            "total_candidates": total, "candidates": ordered}


# ------------------------------------------------- target-first projection
def build_target_projection(*, source_objects: List[Dict[str, Any]],
                            target_domain: str,
                            authorized: Callable[[Dict[str, Any], str], bool]
                            ) -> Dict[str, Any]:
    """Rights + target filtering happens BEFORE any index, summary, path,
    count, manifest or log materialisation. Blocked objects are never
    inserted anywhere, so they are indistinguishable from non-existent
    objects in every consumer-visible surface (F-02)."""
    visible = [obj for obj in source_objects
               if authorized(obj, target_domain)]
    visible_ids = sorted(obj["object_id"] for obj in visible)
    passages = [obj["passage"] for obj in visible
                if obj.get("passage") is not None]
    index_terms: Dict[str, List[str]] = {}
    for obj in visible:
        for token in (set(_tokenize(obj.get("title", ""))) |
                      set(_tokenize(obj.get("text", "")))):
            index_terms.setdefault(token, []).append(obj["object_id"])
    index_terms = {k: sorted(v) for k, v in sorted(index_terms.items())}
    consumer_view = {
        "target_domain": target_domain,
        "visible_object_ids": visible_ids,
        "visible_count": len(visible_ids),
        "index_terms": index_terms,
        "passage_source_ids": sorted(p["source_id"] for p in passages),
        "manifest_entry_ids": visible_ids,
    }
    return {"target_domain": target_domain,
            "visible_object_ids": visible_ids,
            "passages": passages,
            "index_terms": index_terms,
            "consumer_view": consumer_view}


def query_projection(projection: Dict[str, Any],
                     object_id: str) -> Dict[str, Any]:
    """Blocked and non-existent queries return the IDENTICAL dict, with
    no id, title, count or diagnostic that could distinguish them."""
    if object_id in projection["visible_object_ids"]:
        return {"status": "OK", "grain": "DOCUMENT",
                "reason": "VISIBLE_IN_CURRENT_AUTHORIZED_SCOPE"}
    return {"status": "UNKNOWN", "grain": "UNKNOWN",
            "reason": "NO_VERIFIED_RESULT_IN_CURRENT_AUTHORIZED_SCOPE"}


def search_projection(projection: Dict[str, Any],
                      term: str) -> List[str]:
    """Search only over the target-first index (visible objects only)."""
    return list(projection["index_terms"].get(term, []))


# ------------------------------------------------------------ UID slices
def route_uid_slices(records: List[Dict[str, Any]], *, player_uid: str,
                     expected_keys: Optional[Any] = None,
                     expected_counts: Optional[Dict[Any, int]] = None) -> Dict[str, Any]:
    """UID -> season/match_type/scope slices, each with an explicit
    completeness/end marker. A slice whose declared total exceeds its
    rows, or an expected slice that is absent, is PARTIAL, never silent
    truncation.

    Q2-fix (PR #216 round 1): ``expected_counts`` is an authoritative
    slice catalog (key -> expected row count, e.g. derived from a
    manifest/snapshot, not an ad-hoc caller guess). Keys may be
    3-tuples (season, match_type, scope) or 4-tuples including the
    TEAM_SPLIT team_id. An absent slice or a row-count mismatch is
    reported in ``count_mismatches`` and makes the result PARTIAL, so
    an omitted or duplicated record is detected even when no
    caller-supplied ``expected_keys`` set would have caught it.
    """
    slices: Dict[Any, Dict[str, Any]] = {}
    for record in records:
        validated = validate_stats_record(record)
        if validated["player_uid"] != player_uid:
            continue
        key = (validated["season"], validated["match_type"],
               validated["scope"],
               validated.get("team_id") if validated["scope"] == "TEAM_SPLIT" else None)
        slices.setdefault(key, {"season": validated["season"],
                                "match_type": validated["match_type"],
                                "scope": validated["scope"],
                                "team_id": key[3], "rows": []})
        slices[key]["rows"].append(validated["record_id"])
    out = []
    for key in sorted(slices, key=str):
        sl = slices[key]
        sl = dict(sl, total_rows=len(sl["rows"]),
                  complete=True, end_marker=True)
        out.append(sl)
    missing = []
    if expected_keys is not None:
        # Expected keys may be 3-tuples (season, match_type, scope) or
        # 4-tuples including the TEAM_SPLIT team_id; both forms are
        # compared against the matching present-key form.
        present_triple = {(s["season"], s["match_type"], s["scope"])
                          for s in out}
        present_full = {(s["season"], s["match_type"], s["scope"],
                         s["team_id"]) for s in out}
        for key in expected_keys:
            present = present_full if len(key) == 4 else present_triple
            if key not in present:
                missing.append(key)
        missing = sorted(missing, key=str)
    count_mismatches = []
    if expected_counts is not None:
        rows_by_full: Dict[Any, int] = {}
        rows_by_triple: Dict[Any, int] = {}
        for sl in out:
            full_key = (sl["season"], sl["match_type"], sl["scope"],
                        sl["team_id"])
            triple_key = (sl["season"], sl["match_type"], sl["scope"])
            rows_by_full[full_key] = rows_by_full.get(full_key, 0) + len(sl["rows"])
            rows_by_triple[triple_key] = rows_by_triple.get(triple_key, 0) + len(sl["rows"])
        for key, expected in sorted(expected_counts.items(), key=str):
            if len(key) == 4:
                actual = rows_by_full.get(key, 0)
            else:
                actual = rows_by_triple.get(key, 0)
            if actual != expected:
                count_mismatches.append({"key": key, "expected": expected,
                                         "actual": actual})
                if actual == 0 and key not in missing:
                    missing.append(key)
        missing = sorted(missing, key=str)
    complete = (not missing and not count_mismatches
                and all(s["complete"] for s in out))
    return {"status": "COMPLETE" if complete else "PARTIAL",
            "complete": complete, "end_marker": complete,
            "slices": out, "missing": missing,
            "count_mismatches": count_mismatches}


# ------------------------------------------------------------ cross-source
def answer_cross_source(*, fact: Dict[str, Any],
                        stats_result: Dict[str, Any],
                        passage: Dict[str, Any]) -> Dict[str, Any]:
    """Cross-source answer with grains kept separate. Correlation between
    a Fact, a Stats value and a Document quote never becomes a causal
    claim: causal inference is UNKNOWN by construction."""
    if not validate_passage(passage)["valid"]:
        raise KcError("PASSAGE_DIGEST_MISMATCH")
    return {
        "grains": {
            "CANONICAL_FACT": fact,
            "STATS": {"value": stats_result.get("value"),
                      "status": stats_result.get("status"),
                      "key": stats_result.get("key")},
            "DOCUMENT": {"text": passage["text"],
                         "source_id": passage["source_id"],
                         "revision": passage["revision"],
                         "locator": passage["locator"],
                         "digest": passage["digest"]},
        },
        "causal_inference": {
            "status": "UNKNOWN",
            "reason": "CAUSAL_INFERENCE_NOT_DERIVED_FROM_CORRELATED_SOURCES",
        },
    }


# ------------------------------------------------------------ snapshot
def build_snapshot(*, entries: List[Dict[str, Any]], target_domain: str,
                   capabilities: List[str],
                   build_provenance: str) -> Dict[str, Any]:
    """Immutable snapshot binding. snapshot_id is derived from the
    content hash; snapshot_id/digest fields are excluded from the hash
    by construction, so any entry change changes the binding."""
    for entry in entries:
        if not _SHA256_RE.match(entry.get("sha256", "")):
            raise KcError(f"INVALID_ENTRY_SHA256:{entry.get('entry_id')}")
        if entry.get("authority") not in ("canonical", "derived", "control"):
            raise KcError(f"INVALID_ENTRY_AUTHORITY:{entry.get('entry_id')}")
        if entry.get("rights") not in ("public", "copyrighted", "private"):
            raise KcError(f"INVALID_ENTRY_RIGHTS:{entry.get('entry_id')}")
    content = {"entries": sorted(entries, key=lambda e: e["entry_id"]),
               "target_domain": target_domain,
               "capabilities": sorted(capabilities),
               "build_provenance": build_provenance}
    digest = sha256_hex(canonical_json(content))
    return {"snapshot_id": f"kc-snapshot:{digest}",
            "digest_kind": "KC_SNAPSHOT_SHA256", "algorithm": "SHA-256",
            "covered_bytes": "UTF-8 bytes of canonical JSON of "
            "{entries, target_domain, capabilities, build_provenance}",
            "entries": content["entries"],
            "target_domain": target_domain,
            "capabilities": content["capabilities"],
            "build_provenance": build_provenance,
            "content_sha256": digest}


def validate_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    content = {"entries": sorted(snapshot["entries"],
                                 key=lambda e: e["entry_id"]),
               "target_domain": snapshot["target_domain"],
               "capabilities": sorted(snapshot["capabilities"]),
               "build_provenance": snapshot["build_provenance"]}
    expected = sha256_hex(canonical_json(content))
    valid = (snapshot.get("content_sha256") == expected
             and snapshot.get("snapshot_id") == f"kc-snapshot:{expected}"
             and snapshot.get("digest_kind") == "KC_SNAPSHOT_SHA256")
    return {"valid": valid, "digest_kind": snapshot.get("digest_kind"),
            "algorithm": snapshot.get("algorithm")}


def validator_receipt(*, subject_kind: str, subject_id: str,
                      algorithm: str, covered_bytes: str,
                      producer_validated: bool,
                      connector_returned: Any = None,
                      client_observed: Any = None) -> Dict[str, Any]:
    """Three-way receipt kept separate. Reading a digest string in a
    client is NOT client-side digest computation."""
    return {
        "subject_kind": subject_kind, "subject_id": subject_id,
        "algorithm": algorithm, "covered_bytes": covered_bytes,
        "producer_validated": producer_validated,
        "connector_returned": connector_returned
        if connector_returned is not None else "NOT_OBSERVED",
        "client_observed": client_observed
        if client_observed is not None else "NOT_OBSERVED",
        "client_computed_digest": False,
    }
