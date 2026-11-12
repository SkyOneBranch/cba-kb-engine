"""Evidence posture classification for stats and claims (REQ-213 / v2.1.3).

Core principles:
- Statement != Fact. Evidence != Fact. Unknown != 0.
- Clean official league records are primary authoritative records and quiet by default.
- Fold at presentation, never discard at evidence layer.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence


EVIDENCE_POSTURES = frozenset({
    "CLEAN_OFFICIAL",
    "IDENTICAL_DUPLICATE",
    "SCOPE_OR_CUTOFF_DIFF",
    "REVISION_SUPERSEDED",
    "DATA_DISCREPANCY",
    "TRUE_CONFLICT",
    "SINGLE_SOURCE_UNCORROBORATED",
    "INDEPENDENTLY_CORROBORATED",
    "UNKNOWN",
})

UNKNOWN_SUBKINDS = frozenset({
    "NO_RELIABLE_EVIDENCE_IN_KB",
    "CONFIRMED_UNPUBLISHED",
})


def _extract_meta(rec: Dict[str, Any], key: str, default: Any = None) -> Any:
    """Extract metadata from top-level or rec.provenance.request_contract."""
    if key in rec:
        return rec[key]
    prov = rec.get("provenance")
    if isinstance(prov, dict):
        req_contract = prov.get("request_contract")
        if isinstance(req_contract, dict) and key in req_contract:
            return req_contract[key]
    return default


def _resolve_valid_supersession(valid_items: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Resolve explicit acyclic supersession chain when remaining active records agree.

    Returns None if no valid in-set supersession exists, if a cycle/self-loop exists,
    or if multiple non-superseded active records disagree on value (which must fall
    through to discrepancy/conflict rather than masking an active conflict).
    """
    id_to_item: Dict[str, Dict[str, Any]] = {}
    for it in valid_items:
        rid = it.get("record_id")
        if isinstance(rid, str) and rid:
            if rid in id_to_item and id_to_item[rid]["value"] != it["value"]:
                return None
            id_to_item[rid] = it

    edges: List[tuple[Dict[str, Any], Dict[str, Any]]] = []
    superseded_ids: set[str] = set()
    parent_of: Dict[str, str] = {}

    for it in valid_items:
        rid = it.get("record_id")
        sup_id = it.get("supersedes_record_id")
        if not (isinstance(rid, str) and rid and isinstance(sup_id, str) and sup_id):
            continue
        if sup_id == rid:
            return None  # self-loop is invalid
        if sup_id not in id_to_item:
            continue
        # Detect cycle by walking supersedes pointers from sup_id
        seen = {rid, sup_id}
        cur = id_to_item[sup_id].get("supersedes_record_id")
        while isinstance(cur, str) and cur in id_to_item:
            if cur in seen:
                return None  # cycle detected
            seen.add(cur)
            cur = id_to_item[cur].get("supersedes_record_id")
        parent_of[rid] = sup_id
        superseded_ids.add(sup_id)
        edges.append((id_to_item[sup_id], it))

    if not edges or not superseded_ids:
        return None

    active_items = [
        it for it in valid_items
        if it.get("record_id") not in superseded_ids
    ]
    if not active_items:
        return None

    active_values = {it["value"] for it in active_items}
    if len(active_values) != 1:
        # Unresolved disagreement among active records cannot be masked by a revision edge
        return None

    # Prefer active item that actually participates as head of supersession chain
    superseding_active = [
        it for it in active_items if it.get("record_id") in parent_of
    ]
    adopted_item = superseding_active[-1] if superseding_active else active_items[-1]
    rev_history = [
        {
            "prior_record_id": prior_item["record_id"],
            "prior_value": prior_item["value"],
            "new_record_id": rev_item["record_id"],
            "new_value": rev_item["value"],
            "captured_at": rev_item["captured_at"],
            "supersedes_record_id": rev_item["supersedes_record_id"],
            "reason": rev_item["revision_reason"],
        }
        for prior_item, rev_item in edges
    ]
    return {"adopted_item": adopted_item, "revision_history": rev_history}


def classify_stats_evidence(
    records: Sequence[Dict[str, Any]],
    *,
    player_uid: str,
    season: str,
    match_type: str,
    scope: str = "WHOLE_SEASON",
    team_id: Optional[str] = None,
    provider: str = "CBA_OFFICIAL",
    competition: str = "CBA",
    metric: str,
    unknown_basis: Optional[str] = None,
) -> Dict[str, Any]:
    """Classify stats evidence posture for a query target."""
    matching: List[Dict[str, Any]] = []
    scope_splits: List[Dict[str, Any]] = []

    for rec in records:
        if not isinstance(rec, dict):
            continue
        if rec.get("player_uid") != player_uid:
            continue
        if str(rec.get("season")) != str(season):
            continue
        if str(rec.get("match_type")) != str(match_type):
            continue
        if rec.get("provider") != provider:
            continue
        if rec.get("competition") != competition:
            continue

        rec_scope = rec.get("scope", "WHOLE_SEASON")
        rec_team = rec.get("team_id")
        team_matches = (
            True
            if scope != "TEAM_SPLIT" or team_id is None
            else (rec_team is not None and str(rec_team) == str(team_id))
        )

        if rec_scope == scope and team_matches:
            matching.append(rec)
        elif rec_scope == "TEAM_SPLIT" and scope == "WHOLE_SEASON":
            m_split = rec.get("metrics") or {}
            if m_split.get(metric) is not None:
                scope_splits.append({
                    "record_id": rec.get("record_id"),
                    "team_id": rec_team,
                    "team_name": rec.get("team_name"),
                    "games_played": m_split.get("games_played"),
                    "value": m_split.get(metric),
                })

    def _unknown_result() -> Dict[str, Any]:
        subkind = (
            "CONFIRMED_UNPUBLISHED"
            if unknown_basis == "CONFIRMED_UNPUBLISHED"
            else "NO_RELIABLE_EVIDENCE_IN_KB"
        )
        return {
            "posture": "UNKNOWN",
            "unknown_subkind": subkind,
            "adopted_value": None,
            "unit": None,
            "scope_meta": {
                "season": season,
                "match_type": match_type,
                "scope": scope,
                "team_id": team_id,
            },
            "discrepancies": [],
            "revision_history": [],
            "warnings": [subkind],
        }

    if not matching:
        return _unknown_result()

    items: List[Dict[str, Any]] = []
    for r in matching:
        metrics = r.get("metrics") or {}
        val = metrics.get(metric)
        prov = r.get("provenance") or {}
        locator = prov.get("endpoint") or prov.get("source_uri")
        items.append({
            "record": r,
            "record_id": r.get("record_id"),
            "team_id": r.get("team_id"),
            "value": val,
            "games_played": metrics.get("games_played"),
            "captured_at": prov.get("captured_at"),
            "locator": locator,
            "record_kind": _extract_meta(r, "record_kind"),
            "supersedes_record_id": _extract_meta(r, "supersedes_record_id"),
            "revision_reason": _extract_meta(r, "revision_reason"),
        })

    valid_items = [it for it in items if it["value"] is not None]
    if not valid_items:
        return _unknown_result()

    # Single valid record
    if len(valid_items) == 1:
        it = valid_items[0]
        val = it["value"]
        r = it["record"]
        m = r.get("metrics") or {}
        prov = r.get("provenance") or {}
        scope_meta = {
            "season": season,
            "match_type": match_type,
            "scope": scope,
            "team_id": team_id,
            "games_played": m.get("games_played"),
            "captured_at": prov.get("captured_at"),
            "locator": it["locator"],
        }
        if provider == "CBA_OFFICIAL":
            out: Dict[str, Any] = {
                "posture": "CLEAN_OFFICIAL",
                "adopted_value": val,
                "published_value": val,
                "provider": provider,
                "authority_note": "PRIMARY_OFFICIAL_RECORD",
                "warnings": [],
                "discrepancies": [],
                "revision_history": [],
                "scope_meta": scope_meta,
            }
        else:
            out = {
                "posture": "SINGLE_SOURCE_UNCORROBORATED",
                "adopted_value": val,
                "published_value": val,
                "provider": provider,
                "authority_note": "NON_OFFICIAL_SINGLE_PROVIDER",
                "warnings": ["SINGLE_SOURCE_UNCORROBORATED"],
                "discrepancies": [],
                "revision_history": [],
                "scope_meta": scope_meta,
            }
        if scope_splits:
            out["scope_splits"] = scope_splits
        return out

    # Multiple valid records:
    # 1) Explicit acyclic supersession where all remaining active records agree
    sup_resolved = _resolve_valid_supersession(valid_items)
    if sup_resolved is not None:
        rev_item = sup_resolved["adopted_item"]
        prov = rev_item["record"].get("provenance") or {}
        m = rev_item["record"].get("metrics") or {}
        scope_meta = {
            "season": season,
            "match_type": match_type,
            "scope": scope,
            "team_id": team_id,
            "games_played": m.get("games_played"),
            "captured_at": prov.get("captured_at"),
            "locator": rev_item["locator"],
        }
        return {
            "posture": "REVISION_SUPERSEDED",
            "adopted_value": rev_item["value"],
            "published_value": rev_item["value"],
            "provider": provider,
            "authority_note": "PRIMARY_OFFICIAL_RECORD",
            "warnings": [],
            "discrepancies": [],
            "revision_history": sup_resolved["revision_history"],
            "scope_meta": scope_meta,
        }

    # 2) Identical duplicate values across exact scope key
    distinct_vals = {it["value"] for it in valid_items}
    if len(distinct_vals) == 1:
        it = valid_items[0]
        val = it["value"]
        prov = it["record"].get("provenance") or {}
        m = it["record"].get("metrics") or {}
        scope_meta = {
            "season": season,
            "match_type": match_type,
            "scope": scope,
            "team_id": team_id,
            "games_played": m.get("games_played"),
            "captured_at": prov.get("captured_at"),
            "locator": it["locator"],
        }
        return {
            "posture": "IDENTICAL_DUPLICATE",
            "adopted_value": val,
            "published_value": val,
            "provider": provider,
            "authority_note": "PRIMARY_OFFICIAL_RECORD",
            "warnings": [],
            "discrepancies": [],
            "revision_history": [],
            "scope_meta": scope_meta,
        }

    # 3a) Unfiltered TEAM_SPLIT across distinct teams (no intra-team value conflict)
    if scope == "TEAM_SPLIT" and team_id is None:
        by_team: Dict[Any, List[Dict[str, Any]]] = {}
        for it in valid_items:
            by_team.setdefault(it["team_id"], []).append(it)
        if None not in by_team and len(by_team) > 1 and all(
            len({x["value"] for x in bucket}) == 1 for bucket in by_team.values()
        ):
            return {
                "posture": "SCOPE_OR_CUTOFF_DIFF",
                "adopted_value": None,
                "provider": provider,
                "warnings": [],
                "discrepancies": [],
                "scope_splits": [
                    {
                        "record_id": it["record_id"],
                        "team_id": it["team_id"],
                        "games_played": it["games_played"],
                        "value": it["value"],
                        "locator": it["locator"],
                    }
                    for it in valid_items
                ],
                "revision_history": [],
                "scope_meta": {
                    "season": season,
                    "match_type": match_type,
                    "scope": scope,
                    "team_id": None,
                },
            }

    # 3b) Cutoff progression: distinct games_played cutoffs with no intra-cutoff value conflict
    by_gp: Dict[Any, List[Dict[str, Any]]] = {}
    for it in valid_items:
        by_gp.setdefault(it["games_played"], []).append(it)
    if None not in by_gp and len(by_gp) > 1 and all(
        len({x["value"] for x in bucket}) == 1 for bucket in by_gp.values()
    ):
        sorted_by_gp = sorted(
            valid_items,
            key=lambda x: (
                int(x["games_played"]),
                x["captured_at"] or "",
            ),
        )
        latest_item = sorted_by_gp[-1]
        prov = latest_item["record"].get("provenance") or {}
        m = latest_item["record"].get("metrics") or {}
        scope_meta = {
            "season": season,
            "match_type": match_type,
            "scope": scope,
            "team_id": team_id,
            "games_played": m.get("games_played"),
            "captured_at": prov.get("captured_at"),
            "locator": latest_item["locator"],
        }
        cutoff_records = [
            {
                "record_id": x["record_id"],
                "value": x["value"],
                "games_played": x["games_played"],
                "captured_at": x["captured_at"],
                "locator": x["locator"],
            }
            for x in sorted_by_gp
        ]
        return {
            "posture": "SCOPE_OR_CUTOFF_DIFF",
            "adopted_value": latest_item["value"],
            "published_value": latest_item["value"],
            "provider": provider,
            "authority_note": "PRIMARY_OFFICIAL_RECORD",
            "warnings": [],
            "discrepancies": [],
            "cutoff_records": cutoff_records,
            "scope_splits": scope_splits,
            "revision_history": [],
            "scope_meta": scope_meta,
        }

    # 4) Different record_kind at same cutoff (e.g. OFFICIAL_SUMMARY vs GAME_LOG_SUM)
    record_kinds = {it["record_kind"] for it in valid_items if it["record_kind"] is not None}
    discrepancies = [
        {
            "record_id": it["record_id"],
            "value": it["value"],
            "games_played": it["games_played"],
            "locator": it["locator"],
            "record_kind": it["record_kind"],
        }
        for it in valid_items
    ]
    numeric_vals = [
        float(it["value"]) for it in valid_items
        if isinstance(it["value"], (int, float)) and not isinstance(it["value"], bool)
    ]
    delta: Optional[float] = None
    if len(numeric_vals) == len(valid_items) and len(numeric_vals) >= 2:
        delta = round(max(numeric_vals) - min(numeric_vals), 6)

    if len(record_kinds) > 1 and len(record_kinds) == len(valid_items):
        return {
            "posture": "DATA_DISCREPANCY",
            "adopted_value": None,
            "delta": delta,
            "discrepancies": discrepancies,
            "warnings": ["DATA_DISCREPANCY_DETECTED"],
            "revision_history": [],
            "scope_meta": {
                "season": season,
                "match_type": match_type,
                "scope": scope,
                "team_id": team_id,
                "games_played": valid_items[0]["games_played"],
            },
        }

    # 5) Remaining irreconcilable contradiction
    return {
        "posture": "TRUE_CONFLICT",
        "adopted_value": None,
        "delta": delta,
        "discrepancies": discrepancies,
        "warnings": ["IRRECONCILABLE_DATA_CONFLICT"],
        "revision_history": [],
        "scope_meta": {
            "season": season,
            "match_type": match_type,
            "scope": scope,
            "team_id": team_id,
        },
    }


def _claim_statement_text(c: Dict[str, Any]) -> Optional[str]:
    val = c.get("statement") or c.get("claim_text") or c.get("text")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def _claim_origin(
    c: Dict[str, Any],
    *,
    claim_origins: Dict[str, str],
    statements_by_id: Dict[str, Any],
    documents_by_id: Dict[str, Any],
) -> Optional[str]:
    cid = c.get("claim_id")
    if isinstance(cid, str) and cid in claim_origins:
        return str(claim_origins[cid])
    if c.get("upstream_origin"):
        return str(c["upstream_origin"])
    if c.get("canonical_url"):
        return str(c["canonical_url"])
    src_id = c.get("source_id") or c.get("source")
    if isinstance(src_id, str) and src_id in documents_by_id:
        doc = documents_by_id[src_id]
        if isinstance(doc, dict):
            return str(
                doc.get("upstream_origin")
                or doc.get("canonical_url")
                or doc.get("source_locator")
                or src_id
            )
    stmt_ids = c.get("supporting_statement_ids")
    if isinstance(stmt_ids, (list, tuple)) and stmt_ids:
        resolved_stmt_origins: List[str] = []
        for sid in stmt_ids:
            stmt = statements_by_id.get(sid)
            if isinstance(stmt, dict):
                sref = stmt.get("source_ref")
                if isinstance(sref, str) and sref in documents_by_id:
                    doc = documents_by_id[sref]
                    if isinstance(doc, dict):
                        resolved_stmt_origins.append(str(
                            doc.get("upstream_origin")
                            or doc.get("canonical_url")
                            or doc.get("source_locator")
                            or sref
                        ))
                        continue
                if isinstance(sref, str) and sref:
                    resolved_stmt_origins.append(sref)
        if resolved_stmt_origins:
            return resolved_stmt_origins[0]
    if src_id:
        return str(src_id)
    if cid:
        return str(cid)
    return None


def classify_claim_independence(
    claims: Sequence[Dict[str, Any]],
    *,
    statements_by_id: Optional[Dict[str, Any]] = None,
    documents_by_id: Optional[Dict[str, Any]] = None,
    claim_origins: Optional[Dict[str, str]] = None,
    unknown_basis: Optional[str] = None,
) -> Dict[str, Any]:
    """Classify claim independence, syndication deduplication, conflict, and revision posture."""
    valid_claims = [c for c in claims if isinstance(c, dict)]
    if not valid_claims:
        subkind = (
            "CONFIRMED_UNPUBLISHED"
            if unknown_basis == "CONFIRMED_UNPUBLISHED"
            else "NO_RELIABLE_EVIDENCE_IN_KB"
        )
        return {
            "posture": "UNKNOWN",
            "unknown_subkind": subkind,
            "independent_source_count": 0,
            "warnings": [subkind],
            "revision_history": [],
        }

    claim_origins = claim_origins or {}
    statements_by_id = statements_by_id or {}
    documents_by_id = documents_by_id or {}

    # Check explicit superseded / prior_claim_id chain
    superseded_ids: set[str] = set()
    rev_history: List[Dict[str, Any]] = []
    has_revision_marker = False
    for c in valid_claims:
        cid = c.get("claim_id")
        prior_id = c.get("prior_claim_id")
        if not prior_id and isinstance(c.get("provenance"), dict):
            prior_id = c.get("provenance", {}).get("transitions", {}).get("prior_claim_id")
        is_sup = c.get("status") == "superseded"
        if is_sup and isinstance(cid, str):
            superseded_ids.add(cid)
        if isinstance(prior_id, str) and prior_id:
            superseded_ids.add(prior_id)
        if is_sup or prior_id:
            has_revision_marker = True
            rev_history.append({
                "claim_id": cid,
                "status": c.get("status"),
                "prior_claim_id": prior_id,
                "statement": _claim_statement_text(c),
            })

    active_claims = [
        c for c in valid_claims
        if c.get("claim_id") not in superseded_ids and c.get("status") != "superseded"
    ]

    if has_revision_marker:
        active_stmts = {
            st for st in (_claim_statement_text(c) for c in active_claims) if st is not None
        }
        if len(active_stmts) <= 1:
            return {
                "posture": "REVISION_SUPERSEDED",
                "revision_history": rev_history,
                "independent_source_count": len(active_claims) or 1,
                "warnings": [],
            }

    target_claims = active_claims if active_claims else valid_claims
    origins: set[str] = set()
    statements: set[str] = set()
    claim_summaries: List[Dict[str, Any]] = []

    for c in target_claims:
        orig = _claim_origin(
            c,
            claim_origins=claim_origins,
            statements_by_id=statements_by_id,
            documents_by_id=documents_by_id,
        )
        if orig:
            origins.add(orig)
        stmt_text = _claim_statement_text(c)
        if stmt_text:
            statements.add(stmt_text)
        claim_summaries.append({
            "claim_id": c.get("claim_id"),
            "origin": orig,
            "statement": stmt_text,
        })

    # Contradictory active claim statements must NEVER be reported as corroborated
    if len(statements) > 1:
        return {
            "posture": "TRUE_CONFLICT",
            "independent_source_count": len(origins),
            "origins": sorted(origins),
            "discrepancies": claim_summaries,
            "warnings": ["CONFLICTING_CLAIMS"],
            "revision_history": rev_history,
        }

    if len(origins) <= 1:
        return {
            "posture": "SINGLE_SOURCE_UNCORROBORATED",
            "independent_source_count": len(origins),
            "origins": sorted(origins),
            "warnings": ["SINGLE_SOURCE_UNCORROBORATED"],
            "revision_history": [],
        }

    return {
        "posture": "INDEPENDENTLY_CORROBORATED",
        "independent_source_count": len(origins),
        "origins": sorted(origins),
        "warnings": [],
        "revision_history": [],
    }
