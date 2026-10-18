"""Muse research-package handoff adapter: handoff JSON -> canonical stats records.

Converts ``player_season_stats_handoff_v1`` envelopes (RC-01/RC-03 row shape,
``records``) and multi-team probe documents (RC-02 row shape, ``provider_rows``
with ``row_classification``) into canonical PLAYER_SEASON_STATS records.

Frozen contracts honored (FREEZE_PACKAGE_v2.0.5.md §4.1, Base daedba86):
- Reuses stats.py grain validation (``validate_stats_record``,
  ``build_record_id``, ``resolve_stats_player_identity``); the stats fact
  model is never redefined and stats.py itself is untouched.
- ``metrics_normalized``: fixed 22-key set; a missing or extra key ->
  REJECTED (fail-closed). Unknown keys are never silently mapped.
- ``metrics_raw``: dropped from the contract (provenance reference only).
- TEAM_SPLIT requires team_id (stats.py ``TEAM_SPLIT_REQUIRES_TEAM_ID``).
- Unknown match_type code -> identity REVIEW_REQUIRED (never silent).
- Provenance: stats.py mandates non-empty source_uri and valid sha256
  hashes; those cannot be fabricated, so a missing/invalid uri or hash ->
  REJECTED. A missing ``provider_schema_signature`` (not required by
  stats.py) -> identity REVIEW_REQUIRED with the gap recorded.
- Rights default private (``decide_visibility`` semantics: PRIVATE,
  public_export_allowed=False, TARGET_SPECIFIC).
- No identity auto-merge: resolution is read-only via
  ``resolve_stats_player_identity`` with no registry -> UNRESOLVED.
  ``player_identity`` write path is never touched.
- record_id is deterministic via ``build_record_id``; re-import of the
  same payload is idempotent through ``deduplicate_and_reconcile_stats``.

Tri-state (RC-03): null stays null, absent normalized keys are REJECTED
per the fixed 22-key rule, explicit zero stays zero. Unknown != 0.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..stats import (
    DEFAULT_COMPETITION,
    PROVIDER_CBA_OFFICIAL,
    SEMANTIC_GRAIN,
    STATS_SCHEMA_VERSION,
    StatsValidationError,
    build_record_id,
    resolve_stats_player_identity,
    validate_stats_record,
)

HANDOFF_SCHEMA = "player_season_stats_handoff_v1"

#: Fixed 22-key normalized -> canonical metric mapping (frozen §4.1).
#: Any normalized key outside this table -> REJECTED. Any missing key -> REJECTED.
NORMALIZED_TO_CANONICAL: Dict[str, str] = {
    "games_played": "games_played",
    "games_started": "games_started",
    "minutes_per_game": "minutes_per_game",
    "seconds_per_game": "seconds_per_game",
    "points_per_game": "points_per_game",
    "rebounds_per_game": "rebounds_per_game",
    "rebounds_offensive_per_game": "offensive_rebounds_per_game",
    "rebounds_defensive_per_game": "defensive_rebounds_per_game",
    "assists_per_game": "assists_per_game",
    "steals_per_game": "steals_per_game",
    "blocks_per_game": "blocks_per_game",
    "turnovers_per_game": "turnovers_per_game",
    "fouls_per_game": "fouls_per_game",
    "field_goals_made_per_game": "field_goals_made_per_game",
    "field_goals_attempted_per_game": "field_goals_attempted_per_game",
    "field_goal_pct_display": "field_goals_percentage",
    "three_point_made_per_game": "three_point_made_per_game",
    "three_point_attempted_per_game": "three_point_attempted_per_game",
    "three_point_pct_display": "three_point_percentage",
    "free_throws_made_per_game": "free_throws_made_per_game",
    "free_throws_attempted_per_game": "free_throws_attempted_per_game",
    "free_throw_pct_display": "free_throws_percentage",
}

#: Probe ``provider_rows`` raw provider key -> canonical metric mapping.
#: Raw keys that are not stat fields at all (age, birthDate, ...) are dropped
#: with the rest of metrics_raw. A raw key that IS a stat field but has no
#: mapping here -> REJECTED (fail-closed: mapping table incomplete).
RAW_TO_CANONICAL: Dict[str, str] = {
    "playerTimes": "games_played",
    "gameStartNum": "games_started",
    "minutes": "minutes_per_game",
    "seconds": "seconds_per_game",
    "points": "points_per_game",
    "rebounds": "rebounds_per_game",
    "reboundsOffensive": "offensive_rebounds_per_game",
    "reboundsDefensive": "defensive_rebounds_per_game",
    "assists": "assists_per_game",
    "steals": "steals_per_game",
    "blocked": "blocks_per_game",
    "turnovers": "turnovers_per_game",
    "fouls": "fouls_per_game",
    "fieldGoals": "field_goals_made_per_game",
    "fieldGoalsAttempted": "field_goals_attempted_per_game",
    "fieldGoalsPercentage": "field_goals_percentage",
    "threePointGoals": "three_point_made_per_game",
    "threePointAttempted": "three_point_attempted_per_game",
    "threePointPercentage": "three_point_percentage",
    "freeThrows": "free_throws_made_per_game",
    "freeThrowsAttempted": "free_throws_attempted_per_game",
    "freeThrowsPercentage": "free_throws_percentage",
}

#: Raw provider keys that are stat fields but intentionally have no canonical
#: mapping (ordering/sort helpers, not per-game metrics). Encountering one in
#: probe rows -> REJECTED so a silent drop of real stat signal is impossible.
RAW_STAT_KEYS_WITHOUT_MAPPING = frozenset({
    "fieldGoalsPercentageSort",
    "threePointPercentageSort",
    "freeThrowsPercentageSort",
})

#: match_type_code -> (canonical match_type, match_type_label).
#: Unknown codes -> identity REVIEW_REQUIRED (frozen §4.1).
MATCH_TYPE_MAP: Dict[str, Tuple[str, str]] = {
    "1": ("1", "REGULAR_SEASON"),
    "2": ("2", "PLAYOFFS"),
}

PROVENANCE_ENDPOINT = "muse_handoff_adapter/player_season_stats_handoff_v1"


class HandoffError(StatsValidationError):
    """Raised when a handoff row is REJECTED (fail-closed)."""
    pass


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or isinstance(value, bool) or not value.strip():
        raise HandoffError(f"HANDOFF_{label}_REQUIRED:{value!r}")
    return value.strip()


def _map_match_type(code: Any, name: Any) -> Tuple[str, Optional[str], bool]:
    """Return (match_type, match_type_label, known). Unknown -> review flag."""
    code_s = str(code).strip() if code is not None else ""
    if code_s in MATCH_TYPE_MAP:
        mt, label = MATCH_TYPE_MAP[code_s]
        return mt, label, True
    # Unknown code: keep the raw code as match_type (non-empty, traceable),
    # flag for human review per frozen §4.1.
    return code_s or "UNKNOWN", None, False


def _map_normalized_metrics(normalized: Any, where: str) -> Dict[str, Any]:
    if not isinstance(normalized, dict):
        raise HandoffError(f"HANDOFF_METRICS_NORMALIZED_DICT_REQUIRED:{where}")
    keys = set(normalized.keys())
    expected = set(NORMALIZED_TO_CANONICAL.keys())
    missing = expected - keys
    extra = keys - expected
    if missing:
        raise HandoffError(
            f"HANDOFF_METRICS_MISSING_KEYS:{where}:{','.join(sorted(missing))}"
        )
    if extra:
        raise HandoffError(
            f"HANDOFF_METRICS_UNKNOWN_KEYS:{where}:{','.join(sorted(extra))}"
        )
    # null stays null, zero stays zero (tri-state preserved by stats.py)
    return {NORMALIZED_TO_CANONICAL[k]: normalized[k] for k in sorted(expected)}


def _map_probe_metrics(raw: Any, where: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise HandoffError(f"HANDOFF_METRICS_RAW_DICT_REQUIRED:{where}")
    metrics: Dict[str, Any] = {}
    for k, v in raw.items():
        if k in RAW_TO_CANONICAL:
            metrics[RAW_TO_CANONICAL[k]] = v
        elif k in RAW_STAT_KEYS_WITHOUT_MAPPING:
            raise HandoffError(
                f"HANDOFF_PROBE_RAW_STAT_KEY_UNMAPPED:{where}:{k}"
            )
        # else: non-stat provider metadata (age, birthDate, ...) -> dropped
    return metrics


def _provenance(row: Mapping[str, Any], season: str, match_type: str,
                where: str) -> Tuple[Dict[str, Any], bool]:
    """Build provenance. Returns (provenance, provenance_review_flag).

    stats.py mandates source_uri + sha256 hashes; those are REJECTED when
    missing/invalid (never fabricated). A missing provider_schema_signature
    -> review flag (record stays valid, identity REVIEW_REQUIRED).
    """
    source_uri = row.get("source_uri")
    raw_sha = row.get("raw_sha256")
    decoded_sha = row.get("decoded_sha256")
    review = False
    if not isinstance(source_uri, str) or not source_uri.strip():
        raise HandoffError(f"HANDOFF_PROVENANCE_SOURCE_URI_REQUIRED:{where}")
    for label, val in (("raw_response_sha256", raw_sha),
                       ("decoded_sha256", decoded_sha)):
        if not isinstance(val, str) or not val.strip():
            raise HandoffError(f"HANDOFF_PROVENANCE_{label.upper()}_REQUIRED:{where}")
    if not row.get("provider_schema_signature"):
        review = True
    captured = row.get("observed_at")
    return {
        "source_uri": source_uri.strip(),
        "endpoint": PROVENANCE_ENDPOINT,
        "request_contract": {},
        "season": season,
        "match_type": match_type,
        "raw_response_sha256": str(raw_sha).strip(),
        "decoded_sha256": str(decoded_sha).strip(),
        "captured_at": captured,
        "provider_schema_signature": row.get("provider_schema_signature"),
    }, review


def _identity(provider_player_id: str, player_name: str,
              review_reasons: List[str]) -> Tuple[str, Optional[str], Dict[str, Any]]:
    status, uid, candidates, reason = resolve_stats_player_identity(
        provider_player_id, player_name, registry=None
    )
    # No registry in the adapter -> UNRESOLVED; any review flag escalates.
    reasons = [reason] + review_reasons
    if review_reasons:
        status = "REVIEW_REQUIRED"
    return status, None, {
        "status": status,
        "reason": ";".join(reasons),
        "candidate_player_uids": candidates,
    }


def _build_record(row: Mapping[str, Any], ctx: Mapping[str, Any],
                  metrics: Dict[str, Any], where: str) -> Dict[str, Any]:
    provider = _require_text(row.get("provider", ctx.get("provider")),
                             "PROVIDER")
    if provider != PROVIDER_CBA_OFFICIAL:
        raise HandoffError(f"HANDOFF_INVALID_PROVIDER:{provider!r}:{where}")
    provider_player_id = _require_text(
        row.get("provider_player_id", ctx.get("provider_player_id")),
        "PROVIDER_PLAYER_ID")
    player_name = _require_text(
        row.get("player_name", ctx.get("player_name")), "PLAYER_NAME")
    season = _require_text(row.get("season", ctx.get("season")), "SEASON")

    match_type, match_type_label, match_known = _map_match_type(
        row.get("match_type_code", ctx.get("match_type_code")),
        row.get("match_type_name", ctx.get("match_type_name")),
    )
    review_reasons: List[str] = []
    if not match_known:
        review_reasons.append(
            f"UNKNOWN_MATCH_TYPE_CODE:{row.get('match_type_code', ctx.get('match_type_code'))!r}")

    scope = row.get("scope", ctx.get("scope"))
    if scope not in ("WHOLE_SEASON", "TEAM_SPLIT"):
        raise HandoffError(f"HANDOFF_INVALID_SCOPE:{scope!r}:{where}")

    team_id = row.get("team_provider_id", ctx.get("team_provider_id"))
    if scope == "TEAM_SPLIT":
        if team_id is None or (isinstance(team_id, str) and not team_id.strip()):
            raise HandoffError(f"HANDOFF_TEAM_SPLIT_REQUIRES_TEAM_ID:{where}")
        team_id = str(team_id).strip()
    elif team_id is None or (isinstance(team_id, str) and not team_id.strip()):
        team_id = None
    else:
        team_id = str(team_id).strip()

    provenance, prov_review = _provenance(row, season, match_type, where)
    if prov_review:
        review_reasons.append("PROVENANCE_SCHEMA_SIGNATURE_MISSING")

    identity_status, player_uid, identity_resolution = _identity(
        provider_player_id, player_name, review_reasons)

    record_id = build_record_id(
        provider=provider,
        season=season,
        match_type=match_type,
        scope=scope,
        provider_player_id=provider_player_id,
        team_id=team_id if scope == "TEAM_SPLIT" else None,
    )

    record = {
        "record_id": record_id,
        "semantic_grain": SEMANTIC_GRAIN,
        "schema_version": STATS_SCHEMA_VERSION,
        "player_uid": player_uid,
        "identity_status": identity_status,
        "identity_resolution": identity_resolution,
        "provider": provider,
        "provider_player_id": provider_player_id,
        "player_name": player_name,
        "season": season,
        "competition": DEFAULT_COMPETITION,
        "match_type": match_type,
        "scope": scope,
        "metrics": metrics,
        "provenance": provenance,
        "rights": {
            "classification": "PRIVATE",
            "public_export_allowed": False,
            "private_ai_consumption": "TARGET_SPECIFIC",
            "materialization_authorized": False,
        },
    }
    if match_type_label is not None:
        record["match_type_label"] = match_type_label
    if team_id is not None:
        record["team_id"] = team_id
        team_name = row.get("team_name", ctx.get("team_name"))
        if isinstance(team_name, str) and team_name.strip():
            record["team_name"] = team_name.strip()
    # Frozen grain validation is the final gate; never bypassed.
    return validate_stats_record(record)


def _iter_rows(payload: Mapping[str, Any]):
    """Yield (row, context, metrics, where) for both envelope shapes."""
    records = payload.get("records")
    probe_rows = payload.get("provider_rows")
    if isinstance(records, list) and records:
        for i, row in enumerate(records):
            where = f"records[{i}]"
            if not isinstance(row, dict):
                raise HandoffError(f"HANDOFF_RECORD_OBJECT_REQUIRED:{where}")
            rtype = row.get("record_type")
            if rtype is not None and rtype != "PLAYER_SEASON_STATS":
                raise HandoffError(f"HANDOFF_UNEXPECTED_RECORD_TYPE:{rtype!r}:{where}")
            scope = row.get("scope")
            if scope not in ("WHOLE_SEASON", "TEAM_SPLIT"):
                raise HandoffError(f"HANDOFF_INVALID_SCOPE:{scope!r}:{where}")
            metrics = _map_normalized_metrics(row.get("metrics_normalized"), where)
            yield row, {}, metrics, where
    elif isinstance(probe_rows, list) and probe_rows:
        # RC-02 probe shape: top-level carries player/match context,
        # rows carry row_classification + team + raw metrics.
        ctx = {k: payload.get(k) for k in (
            "provider", "provider_player_id", "player_name", "season",
            "match_type_code", "match_type_name")}
        for i, prow in enumerate(probe_rows):
            where = f"provider_rows[{i}]"
            if not isinstance(prow, dict):
                raise HandoffError(f"HANDOFF_RECORD_OBJECT_REQUIRED:{where}")
            classification = prow.get("row_classification")
            if classification not in ("WHOLE_SEASON", "TEAM_SPLIT"):
                raise HandoffError(
                    f"HANDOFF_INVALID_ROW_CLASSIFICATION:{classification!r}:{where}")
            row = dict(prow)
            row["scope"] = classification
            metrics = _map_probe_metrics(prow.get("metrics_raw"), where)
            yield row, ctx, metrics, where
    else:
        raise HandoffError("HANDOFF_NO_RECORDS:need records[] or provider_rows[]")


def adapt_handoff(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Convert a handoff payload into validated canonical stats records.

    Args:
        payload: parsed JSON of a ``player_season_stats_handoff_v1`` envelope
            (``records``) or a multi-team probe document (``provider_rows``).

    Returns:
        {"records": [validated canonical records]}. Rows flagged
        REVIEW_REQUIRED are included with identity_status set; REJECTED rows
        raise HandoffError (fail-closed: no partial silent output).

    Raises:
        HandoffError: schema mismatch, unknown provider, unknown metric key,
            missing normalized key, missing team_id on TEAM_SPLIT, or any
            provenance element stats.py mandates but the row lacks.
    """
    if not isinstance(payload, Mapping):
        raise HandoffError("HANDOFF_PAYLOAD_OBJECT_REQUIRED")
    schema = payload.get("schema")
    # Probe documents carry no schema marker; envelopes must match exactly.
    if payload.get("records") is not None and schema != HANDOFF_SCHEMA:
        raise HandoffError(f"HANDOFF_SCHEMA_MISMATCH:{schema!r}")
    records: List[Dict[str, Any]] = []
    for row, ctx, metrics, where in _iter_rows(payload):
        records.append(_build_record(row, ctx, metrics, where))
    return {"records": records}


def payload_sha256(payload: Mapping[str, Any]) -> str:
    """Stable sha256 over canonical JSON bytes (for idempotency receipts)."""
    import json as _json
    canonical = _json.dumps(payload, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
