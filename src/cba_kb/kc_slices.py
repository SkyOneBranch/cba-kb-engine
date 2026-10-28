"""PR-B (Issue #220) query-ready slices — offline/synthetic only.

Builds on the Slice A kc/0.1 contract (cba_kb.kc_projection): validated
Stats records, exact-key lookup, 0 != null, WHOLE_SEASON vs TEAM_SPLIT
separation, no silent defaults. Provider and competition are REQUIRED
keyword-only arguments with NO defaults: omitting either raises
TypeError at call time, never a canonical fallback. An unmaterialized
aggregation is reported unavailable, never computed. All artifacts are
SIMULATED; nothing here is real publication, real authorization, or
real Drive readback.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .kc_projection import (
    KcError,
    resolve_identity,
    validate_metric_value,
    validate_stats_record,
)

CANONICAL_PROVIDER = "CBA_OFFICIAL"
CANONICAL_COMPETITION = "CBA"


class KcSliceError(KcError, ValueError):
    """Fail-closed slice contract violation (also a ValueError, so a
    rejected dimension is catchable as a value rejection)."""


def _unit(metric: str) -> str:
    if metric.endswith("_percentage"):
        return "percent"
    if metric.endswith("_percentage_rate"):
        return "ratio"
    if metric in ("games_played", "games_started"):
        return "games"
    return "per_game"


def entity_candidates(players: List[Dict[str, Any]],
                      aliases: List[Dict[str, Any]], *, query: str,
                      query_kind: str,
                      authorized_ids: Any) -> Dict[str, Any]:
    """Approved-alias / canonical-name -> ALL visible UID candidates.

    Delegates to Slice A resolve_identity: a multi-candidate name or
    alias returns every candidate with CLARIFICATION_REQUIRED, never a
    single most-similar pick and never an auto-merge. Players and
    aliases are filtered by authorized_ids FIRST: a UID outside the
    authorized set never appears in candidates, so within the
    authorized view an unauthorized UID is indistinguishable from a
    non-existent one (F-02).
    """
    allowed = set(authorized_ids)
    players = [p for p in players if p.get("player_uid") in allowed]
    aliases = [a for a in aliases if a.get("player_uid") in allowed]
    result = resolve_identity(players, aliases, query=query,
                              query_kind=query_kind)
    return {
        "status": result["status"],
        "grain": "IDENTITY",
        "candidates": list(result["candidates"]),
        "relation": result["relation"],
        "clarification_required":
            result["status"] == "CLARIFICATION_REQUIRED",
        "reason": result.get("reason"),
    }


def build_slice(records: List[Dict[str, Any]], *, player_uid: str,
                season: str, match_type: str, scope: str,
                provider: str, competition: str,
                authorized_ids: Any,
                team_id: Optional[str] = None) -> Dict[str, Any]:
    """One query-ready slice for an exact key. Lookup is completed
    (complete/end_marker True) even when the slice is EMPTY; an empty
    slice means no verified record for the exact key, never a guess.

    provider and competition have NO defaults: omitting either raises
    TypeError before any lookup. The requested player_uid must be in
    authorized_ids, else the slice is refused (PLAYER_NOT_AUTHORIZED)
    before any record is read. That refusal code and the EMPTY return
    for an authorized-but-missing key are INTERNAL diagnostics: they
    distinguish unauthorized from nonexistent, so they are NOT the
    consumer surface. The consumer surface is
    kc_consume.ConsumeSession.consume_slice(), which translates both
    outcomes into one identical uniform result.

    Residual risk (Q2 round 1, NOTE): a non-canonical
    provider/competition combination currently returns EMPTY,
    indistinguishable from a valid-but-empty key; no provider vocabulary
    yet distinguishes invalid dimensions from empty results.
    """
    if scope not in ("WHOLE_SEASON", "TEAM_SPLIT"):
        raise KcSliceError(f"INVALID_SCOPE:{scope}")
    if scope == "TEAM_SPLIT" and not team_id:
        raise KcSliceError("TEAM_SPLIT_REQUIRES_TEAM_ID")
    if player_uid not in set(authorized_ids):
        raise KcSliceError("PLAYER_NOT_AUTHORIZED")
    base = {
        "player_uid": player_uid, "provider": provider,
        "competition": competition, "season": season,
        "match_type": match_type, "scope": scope,
        "team_id": team_id if scope == "TEAM_SPLIT" else None,
        "metrics": {}, "supported_metrics": [], "row_count": 0,
        "total_rows": 0,
    }
    if provider != CANONICAL_PROVIDER or competition != CANONICAL_COMPETITION:
        return dict(base, status="EMPTY", complete=True, end_marker=True)
    matches = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("provider") != provider:
            continue
        if record.get("competition") != competition:
            continue
        validated = validate_stats_record(record)
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
        matches.append(validated)
    if not matches:
        return dict(base, status="EMPTY", complete=True, end_marker=True)
    if len(matches) > 1:
        raise KcSliceError("MULTIPLE_RECORDS_FOR_SLICE_KEY")
    record = matches[0]
    metrics: Dict[str, Any] = {}
    for metric, value in record["metrics"].items():
        if value is None:
            metrics[metric] = {"value": None, "unit": _unit(metric),
                               "value_kind": "NULL",
                               "source_record_id": record["record_id"]}
        else:
            typed = validate_metric_value(metric, value, _unit(metric))
            metrics[metric] = {"value": value, "unit": typed["unit"],
                               "value_kind": typed["value_kind"],
                               "source_record_id": record["record_id"]}
    return dict(base, status="OK", metrics=metrics,
                supported_metrics=sorted(metrics), row_count=1,
                total_rows=1, complete=True, end_marker=True)


def build_slice_catalog(records: List[Dict[str, Any]], *, player_uid: str,
                        expected_keys: List[Tuple[Any, ...]],
                        provider: str,
                        competition: str,
                        authorized_ids: Any) -> Dict[str, Any]:
    """Catalog of declared slices. An expected slice with no verified
    record is listed in ``missing`` and makes the catalog PARTIAL with
    end_marker False — never a silent omission. The player_uid
    authorization rule of build_slice applies to every slice."""
    slices = []
    missing = []
    for key in expected_keys:
        season, match_type, scope = key[0], key[1], key[2]
        team = key[3] if len(key) > 3 else None
        sl = build_slice(records, player_uid=player_uid, season=season,
                         match_type=match_type, scope=scope, team_id=team,
                         provider=provider, competition=competition,
                         authorized_ids=authorized_ids)
        sl = dict(sl, slice_key=(season, match_type, scope, team))
        slices.append(sl)
        if sl["status"] == "EMPTY":
            missing.append((season, match_type, scope, team))
    complete = not missing
    return {"slices": slices, "missing": missing, "complete": complete,
            "end_marker": complete}


def query_slice_metric(slice_dict: Dict[str, Any],
                       metric: str) -> Dict[str, Any]:
    """Exact-metric lookup inside a slice. A missing metric is UNKNOWN,
    never a guessed or neighbouring value."""
    metrics = slice_dict.get("metrics", {})
    if metric in metrics:
        entry = metrics[metric]
        return {"status": "OK", "grain": "STATS", "metric": metric,
                "value": entry["value"], "unit": entry["unit"],
                "value_kind": entry["value_kind"],
                "source_record_id": entry["source_record_id"]}
    return {"status": "UNKNOWN", "grain": "STATS", "metric": metric,
            "value": None, "reason": "METRIC_NOT_IN_SLICE"}


def aggregate_unavailable(*, metric: str, scope: str) -> Dict[str, Any]:
    """Unmaterialized aggregation is NOT computed. P0 reports it
    unavailable; no on-the-fly total, average, or ranking is produced."""
    return {"status": "NOT_APPLICABLE", "grain": "STATS",
            "reason": "UNMATERIALIZED_AGGREGATION_UNAVAILABLE",
            "metric": metric, "scope": scope}
