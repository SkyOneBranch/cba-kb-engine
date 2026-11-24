"""Generic single-game player stats contract & deterministic recent-N-game aggregator (REQ-223 P0-2).

Strict invariants:
- Semantic grain: PLAYER_GAME_STATS (strictly separate from PLAYER_SEASON_STATS in stats.py).
- Never adds or merges PLAYER_GAME_STATS into PLAYER_SEASON_STATS.
- Unknown != 0: missing/undisclosed metrics remain None and are never coerced to 0.
- DNP games (did_not_play=True or non-empty dnp_reason) never pollute per-game average denominators.
- Post-game official revisions (supersedes_record_id) emit explicit LOUD_REVISION summaries
  and never silently overwrite prior records.
"""
from __future__ import annotations

import datetime
import math
import re
from typing import Any, Dict, List, Optional, Sequence

from .evidence_ledger import canonical_bytes

GAME_STATS_SCHEMA = "cba-kb.player-game-stats.v1"
GAME_STATS_SCHEMA_VERSION = 1
SEMANTIC_GRAIN = "PLAYER_GAME_STATS"

PLAYER_GAME_METRIC_ALLOWLIST = frozenset({
    "minutes_played",
    "seconds_played",
    "points",
    "rebounds",
    "offensive_rebounds",
    "defensive_rebounds",
    "assists",
    "steals",
    "blocks",
    "turnovers",
    "fouls",
    "field_goals_made",
    "field_goals_attempted",
    "three_point_made",
    "three_point_attempted",
    "free_throws_made",
    "free_throws_attempted",
    "plus_minus",
    "starter",
})

ALLOWED_TOP_LEVEL_FIELDS = frozenset({
    "schema",
    "schema_version",
    "semantic_grain",
    "record_id",
    "player_uid",
    "provider_player_id",
    "player_name",
    "provider",
    "competition",
    "season",
    "match_type",
    "game_id",
    "game_date",
    "round_label",
    "home_away",
    "team_id",
    "team_name",
    "opponent_team_id",
    "opponent_team_name",
    "dnp_reason",
    "did_not_play",
    "supersedes_record_id",
    "revision_reason",
    "metrics",
    "provenance",
    "rights",
})

REQUIRED_TOP_LEVEL_FIELDS = frozenset({
    "schema",
    "semantic_grain",
    "record_id",
    "provider",
    "competition",
    "season",
    "match_type",
    "game_id",
    "game_date",
    "team_id",
    "opponent_team_id",
    "metrics",
    "provenance",
    "rights",
})

_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class GameStatsError(RuntimeError):
    """Base error for CBA-KB single-game stats domain."""


class GameStatsValidationError(GameStatsError):
    """Validation error for PLAYER_GAME_STATS domain invariants."""


def build_game_stat_record_id(
    *,
    provider: str,
    season: str | int,
    match_type: str | int,
    game_id: str | int,
    player_key: str | int,
    revision_seq: Optional[int] = None,
) -> str:
    """Generate a deterministic canonical record identifier for a player single-game record."""
    base = f"game_stats:{provider}:{season}:{match_type}:{game_id}:{player_key}"
    if revision_seq is not None:
        if isinstance(revision_seq, bool) or not isinstance(revision_seq, int) or revision_seq < 1:
            raise GameStatsValidationError(f"INVALID_REVISION_SEQ:{revision_seq!r}")
        return f"{base}:rev{revision_seq}"
    return base


def _require_non_empty_scalar(value: Any, field_name: str) -> str:
    if (
        value is None
        or isinstance(value, bool)
        or not isinstance(value, (str, int))
        or not str(value).strip()
    ):
        raise GameStatsValidationError(f"INVALID_{field_name.upper()}:{value!r}")
    return str(value).strip()


def validate_player_game_stat_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a single-game player stats record against cba-kb.player-game-stats.v1.

    Never coerces None to 0 (Unknown != 0).
    """
    if not isinstance(rec, dict):
        raise GameStatsValidationError("GAME_STATS_RECORD_OBJECT_REQUIRED")

    unknown_top = set(rec.keys()) - ALLOWED_TOP_LEVEL_FIELDS
    if unknown_top:
        raise GameStatsValidationError(
            f"UNKNOWN_TOP_LEVEL_FIELDS:{','.join(sorted(unknown_top))}"
        )

    missing_top = REQUIRED_TOP_LEVEL_FIELDS - set(rec.keys())
    if missing_top:
        raise GameStatsValidationError(
            f"MISSING_REQUIRED_KEYS:{','.join(sorted(missing_top))}"
        )

    if rec.get("schema") != GAME_STATS_SCHEMA:
        raise GameStatsValidationError(
            f"INVALID_SCHEMA:{rec.get('schema')!r}!={GAME_STATS_SCHEMA!r}"
        )

    if rec.get("semantic_grain") != SEMANTIC_GRAIN:
        raise GameStatsValidationError(
            f"INVALID_SEMANTIC_GRAIN:{rec.get('semantic_grain')!r}!={SEMANTIC_GRAIN!r}"
        )

    if "schema_version" in rec and rec["schema_version"] is not None:
        sv = rec["schema_version"]
        if isinstance(sv, bool) or sv != GAME_STATS_SCHEMA_VERSION:
            raise GameStatsValidationError(f"INVALID_SCHEMA_VERSION:{sv!r}")

    for fld in (
        "record_id",
        "provider",
        "competition",
        "season",
        "match_type",
        "game_id",
        "team_id",
        "opponent_team_id",
    ):
        _require_non_empty_scalar(rec.get(fld), fld)

    game_date = rec.get("game_date")
    if not isinstance(game_date, str) or not _ISO_DATE_RE.match(game_date.strip()):
        raise GameStatsValidationError(f"INVALID_GAME_DATE:{game_date!r}")
    try:
        datetime.date.fromisoformat(game_date.strip())
    except ValueError as exc:
        raise GameStatsValidationError(f"INVALID_GAME_DATE:{game_date!r}") from exc

    did_not_play = rec.get("did_not_play")
    if did_not_play is not None and not isinstance(did_not_play, bool):
        raise GameStatsValidationError(f"INVALID_DID_NOT_PLAY:{did_not_play!r}")

    dnp_reason = rec.get("dnp_reason")
    if dnp_reason is not None and not isinstance(dnp_reason, str):
        raise GameStatsValidationError(f"INVALID_DNP_REASON:{dnp_reason!r}")

    supersedes = rec.get("supersedes_record_id")
    if supersedes is not None and (not isinstance(supersedes, str) or not supersedes.strip()):
        raise GameStatsValidationError(f"INVALID_SUPERSEDES_RECORD_ID:{supersedes!r}")

    # Validate metrics (Unknown != 0: None stays None)
    metrics = rec.get("metrics")
    if not isinstance(metrics, dict):
        raise GameStatsValidationError("METRICS_DICT_REQUIRED")

    unknown_metrics = set(metrics.keys()) - PLAYER_GAME_METRIC_ALLOWLIST
    if unknown_metrics:
        raise GameStatsValidationError(
            f"UNKNOWN_GAME_METRIC_KEYS:{','.join(sorted(unknown_metrics))}"
        )

    for mk, mv in metrics.items():
        if mv is None:
            continue
        if mk == "starter":
            if not isinstance(mv, bool):
                raise GameStatsValidationError(f"INVALID_STARTER_METRIC:{mv!r}")
            continue
        if isinstance(mv, bool) or not isinstance(mv, (int, float)):
            raise GameStatsValidationError(f"INVALID_METRIC_TYPE:{mk}={mv!r}")
        if math.isnan(mv) or math.isinf(mv):
            raise GameStatsValidationError(f"NON_FINITE_METRIC:{mk}={mv!r}")
        if mk != "plus_minus" and mv < 0:
            raise GameStatsValidationError(f"NEGATIVE_METRIC_VALUE:{mk}={mv!r}")

    # Validate provenance
    provenance = rec.get("provenance")
    if not isinstance(provenance, dict):
        raise GameStatsValidationError("PROVENANCE_DICT_REQUIRED")

    raw_sha = provenance.get("raw_sha256")
    if not isinstance(raw_sha, str) or not _SHA256_HEX_RE.match(raw_sha.strip()):
        raise GameStatsValidationError("PROVENANCE_RAW_SHA256_REQUIRED")

    canonical_url = provenance.get("canonical_url")
    if not isinstance(canonical_url, str) or not canonical_url.strip():
        raise GameStatsValidationError("PROVENANCE_CANONICAL_URL_REQUIRED")

    captured_at = provenance.get("captured_at")
    if captured_at is not None:
        if not isinstance(captured_at, str) or not captured_at.strip():
            raise GameStatsValidationError("PROVENANCE_CAPTURED_AT_STR_REQUIRED")
        try:
            dt = datetime.datetime.fromisoformat(captured_at.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise GameStatsValidationError(f"INVALID_PROVENANCE_CAPTURED_AT:{captured_at!r}") from exc
        if dt.tzinfo is None:
            raise GameStatsValidationError(
                f"PROVENANCE_CAPTURED_AT_TIMEZONE_REQUIRED:{captured_at!r}"
            )

    # Validate rights
    rights = rec.get("rights")
    if not isinstance(rights, dict):
        raise GameStatsValidationError("RIGHTS_DICT_REQUIRED")
    if not isinstance(rights.get("classification"), str) or not rights["classification"].strip():
        raise GameStatsValidationError("RIGHTS_CLASSIFICATION_REQUIRED")
    if not isinstance(rights.get("public_export_allowed"), bool):
        raise GameStatsValidationError("PUBLIC_EXPORT_ALLOWED_BOOL_REQUIRED")

    return rec


def _is_dnp_record(rec: Dict[str, Any]) -> bool:
    if rec.get("did_not_play") is True:
        return True
    reason = rec.get("dnp_reason")
    return isinstance(reason, str) and bool(reason.strip())


def _record_player_key(rec: Dict[str, Any]) -> tuple[str, str]:
    uid = rec.get("player_uid")
    if isinstance(uid, str) and uid.strip():
        return ("player_uid", uid.strip())
    pid = rec.get("provider_player_id")
    if pid is not None and not isinstance(pid, bool) and str(pid).strip():
        return ("provider_player_id", str(pid).strip())
    pname = rec.get("player_name")
    if isinstance(pname, str) and pname.strip():
        return ("player_name", pname.strip())
    return ("record_id", str(rec["record_id"]).strip())


def _same_player_identity(rec: Dict[str, Any], prior_rec: Dict[str, Any]) -> bool:
    if _record_player_key(rec) != _record_player_key(prior_rec):
        return False
    u1, u2 = rec.get("player_uid"), prior_rec.get("player_uid")
    if isinstance(u1, str) and u1.strip() and isinstance(u2, str) and u2.strip():
        if u1.strip() != u2.strip():
            return False
    p1, p2 = rec.get("provider_player_id"), prior_rec.get("provider_player_id")
    if p1 is not None and p2 is not None and str(p1).strip() and str(p2).strip():
        if str(p1).strip() != str(p2).strip():
            return False
    return True


def reconcile_game_stat_revisions(
    records: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Resolve supersession chains across PLAYER_GAME_STATS records with LOUD_REVISION output.

    Fail-closed invariants:
    - Target `supersedes_record_id` must exist in the record set.
    - Revision must bind to the exact same player identity, game_id, and season.
    - Cyclic and forked revision chains are rejected with GameStatsValidationError.
    - Never silently overwrites prior records: superseded records are preserved in
      `superseded_records` and every valid supersession link produces a `LOUD_REVISION` summary.
    """
    by_id: Dict[str, Dict[str, Any]] = {}
    for rec in records or ():
        validate_player_game_stat_record(rec)
        rid = str(rec["record_id"]).strip()
        if rid in by_id:
            if canonical_bytes(by_id[rid]) != canonical_bytes(rec):
                raise GameStatsValidationError(f"CONFLICTING_DUPLICATE_RECORD_ID:{rid}")
            continue
        by_id[rid] = dict(rec)

    superseded_ids: set[str] = set()
    superseded_by: Dict[str, str] = {}
    supersedes_map: Dict[str, str] = {}
    revision_summaries: List[Dict[str, Any]] = []

    for rid in sorted(by_id):
        rec = by_id[rid]
        prior_id = rec.get("supersedes_record_id")
        if isinstance(prior_id, str) and prior_id.strip():
            prior_clean = prior_id.strip()
            if prior_clean == rid:
                raise GameStatsValidationError(f"REVISION_CYCLE_DETECTED:{rid}")
            if prior_clean not in by_id:
                raise GameStatsValidationError(
                    f"SUPERSEDED_RECORD_NOT_FOUND:{rid}->{prior_clean}"
                )
            prior_rec = by_id[prior_clean]
            if not _same_player_identity(rec, prior_rec):
                raise GameStatsValidationError(
                    f"CROSS_PLAYER_REVISION_REJECTED:{rid}->{prior_clean}"
                )
            if str(rec["game_id"]).strip() != str(prior_rec["game_id"]).strip():
                raise GameStatsValidationError(
                    f"CROSS_GAME_REVISION_REJECTED:{rid}->{prior_clean}"
                )
            if str(rec["season"]).strip() != str(prior_rec["season"]).strip():
                raise GameStatsValidationError(
                    f"CROSS_SEASON_REVISION_REJECTED:{rid}->{prior_clean}"
                )
            if prior_clean in superseded_by:
                raise GameStatsValidationError(
                    f"FORKED_REVISION_CHAIN:{prior_clean}:{superseded_by[prior_clean]},{rid}"
                )
            superseded_by[prior_clean] = rid
            supersedes_map[rid] = prior_clean
            superseded_ids.add(prior_clean)
            reason = rec.get("revision_reason") or "OFFICIAL_POST_GAME_REVISION"
            revision_summaries.append({
                "disclosure_posture": "LOUD_REVISION",
                "prior_record_id": prior_clean,
                "new_record_id": rid,
                "game_id": str(rec["game_id"]),
                "game_date": str(rec["game_date"]),
                "player_uid": rec.get("player_uid"),
                "reason": reason,
                "disclosure_notice": (
                    f"Post-game official stat revision for game {rec['game_id']} ({rec['game_date']}): "
                    f"{rid} supersedes {prior_clean} ({reason})"
                ),
            })

    # Detect multi-hop cycles (e.g. A -> B -> A)
    for start_rid in sorted(supersedes_map):
        seen: set[str] = {start_rid}
        curr = supersedes_map[start_rid]
        while curr in supersedes_map:
            if curr in seen:
                raise GameStatsValidationError(f"REVISION_CYCLE_DETECTED:{start_rid}")
            seen.add(curr)
            curr = supersedes_map[curr]
        if curr in seen:
            raise GameStatsValidationError(f"REVISION_CYCLE_DETECTED:{start_rid}")

    active_records: List[Dict[str, Any]] = []
    superseded_records: List[Dict[str, Any]] = []
    for rid, rec in by_id.items():
        if rid in superseded_ids:
            superseded_records.append(rec)
        else:
            active_records.append(rec)

    def _desc_key(r: Dict[str, Any]) -> tuple[str, str, str]:
        return (str(r.get("game_date") or ""), str(r.get("game_id") or ""), str(r.get("record_id") or ""))

    active_records.sort(key=_desc_key, reverse=True)
    superseded_records.sort(key=_desc_key, reverse=True)
    revision_summaries.sort(key=lambda s: (s["game_date"], s["game_id"], s["new_record_id"]), reverse=True)

    return {
        "active_records": active_records,
        "superseded_records": superseded_records,
        "revision_summaries": revision_summaries,
    }


def summarize_recent_games(
    records: Sequence[Dict[str, Any]],
    *,
    window: int = 5,
) -> Dict[str, Any]:
    """Deterministically summarize recent N played single-game records.

    - Strictly PLAYER_GAME_STATS grain (never merged with PLAYER_SEASON_STATS).
    - Reconciles supersession chains and surfaces LOUD_REVISION summaries.
    - Excludes DNP games from played-game window & per-game average denominators.
    - Preserves None for any metric missing across all games in the window (Unknown != 0).
    """
    if isinstance(window, bool) or not isinstance(window, int) or window < 1:
        raise ValueError(f"INVALID_RECENT_GAMES_WINDOW:{window!r}")

    reconciled = reconcile_game_stat_revisions(records)
    active = reconciled["active_records"]
    revision_summaries = reconciled["revision_summaries"]
    revised_new_ids = {s["new_record_id"] for s in revision_summaries}

    played: List[Dict[str, Any]] = []
    dnp_count = 0
    for rec in active:
        if _is_dnp_record(rec):
            dnp_count += 1
        else:
            played.append(rec)

    window_games = played[:window]

    avg_keys = ("points", "rebounds", "assists", "steals", "blocks", "minutes_played")
    averages: Dict[str, Optional[float]] = {}
    for k in avg_keys:
        vals = [
            float(g["metrics"][k])
            for g in window_games
            if isinstance(g.get("metrics"), dict) and g["metrics"].get(k) is not None
        ]
        averages[k] = round(sum(vals) / len(vals), 4) if vals else None

    def _sum_pair(made_key: str, att_key: str) -> tuple[Optional[int], Optional[int], Optional[float]]:
        made_vals = [
            int(g["metrics"][made_key])
            for g in window_games
            if isinstance(g.get("metrics"), dict) and g["metrics"].get(made_key) is not None
        ]
        att_vals = [
            int(g["metrics"][att_key])
            for g in window_games
            if isinstance(g.get("metrics"), dict) and g["metrics"].get(att_key) is not None
        ]
        total_made = sum(made_vals) if made_vals else None
        total_att = sum(att_vals) if att_vals else None
        pct = (
            round(total_made / total_att, 4)
            if total_made is not None and total_att is not None and total_att > 0
            else None
        )
        return total_made, total_att, pct

    fg_made, fg_att, fg_pct = _sum_pair("field_goals_made", "field_goals_attempted")
    tp_made, tp_att, tp_pct = _sum_pair("three_point_made", "three_point_attempted")
    ft_made, ft_att, ft_pct = _sum_pair("free_throws_made", "free_throws_attempted")

    latest_game_date: Optional[str] = None
    if window_games:
        latest_game_date = max(str(g["game_date"]) for g in window_games)
    elif active:
        latest_game_date = max(str(g["game_date"]) for g in active)

    captured_dates = [
        str(g["provenance"]["captured_at"]).strip()
        for g in active
        if isinstance(g.get("provenance"), dict) and g["provenance"].get("captured_at")
    ]
    latest_captured_at = max(captured_dates) if captured_dates else None

    decorated_games: List[Dict[str, Any]] = []
    for g in window_games:
        copied = dict(g)
        copied["is_revised"] = str(g["record_id"]) in revised_new_ids
        decorated_games.append(copied)

    return {
        "semantic_grain": SEMANTIC_GRAIN,
        "window": window,
        "played_games_in_window": len(window_games),
        "total_played_games": len(played),
        "dnp_games_count": dnp_count,
        "averages": averages,
        "shooting_totals": {
            "fg_made": fg_made,
            "fg_attempted": fg_att,
            "fg_pct": fg_pct,
            "tp_made": tp_made,
            "tp_attempted": tp_att,
            "tp_pct": tp_pct,
            "ft_made": ft_made,
            "ft_attempted": ft_att,
            "ft_pct": ft_pct,
        },
        "latest_game_date": latest_game_date,
        "latest_captured_at": latest_captured_at,
        "games": decorated_games,
        "revision_summaries": revision_summaries,
    }
