"""Redacted demand-ledger (`cba-kb.demand-ledger.v1`) for REQ-251 P0-E3.

Records normalized, redacted MCP misses (unknown person / unloaded club / missing season)
and early social injury/signing signals into `demand_ledger.jsonl` with idempotent
`hit_count` aggregation and zero plaintext credentials or private UIDs.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .common import atomic

DEMAND_LEDGER_SCHEMA = "cba-kb.demand-ledger.v1"
_SECRET_LIKE_RE = re.compile(r"(?:synth-|ghp_|sk-|dt-uid-|JOB-|[A-Za-z0-9_./+~=-]{32,})")


def normalize_and_redact_query_key(raw_query: str) -> str:
    """Normalize query text and strip any credential/token-shaped substrings."""
    text = re.sub(r"\s+", " ", str(raw_query or "").strip())
    if not text:
        return "empty_query"
    if _SECRET_LIKE_RE.search(text):
        return "[REDACTED_KEY]"
    return text[:120]


def load_demand_ledger(ledger_path: Path | str) -> List[Dict[str, Any]]:
    p = Path(ledger_path)
    if not p.is_file():
        return []
    out: List[Dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        obj = json.loads(s)
        if isinstance(obj, dict):
            out.append(obj)
    return out


def record_demand_miss(
    ledger_path: Path | str,
    *,
    query_key: str,
    miss_reason: str,
    club_hint: Optional[str] = None,
    season_hint: Optional[str] = None,
    signal_kind: Optional[str] = None,
    now_utc: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Idempotently aggregate a redacted demand miss/signal into `demand_ledger.jsonl`."""
    p = Path(ledger_path)
    norm_key = normalize_and_redact_query_key(query_key)
    reason = str(miss_reason or "UNKNOWN_MISS").strip().upper()
    clean_club = normalize_and_redact_query_key(club_hint) if club_hint else ""
    clean_season = str(season_hint or "").strip()[:16]
    now = now_utc if now_utc is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    ts = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    entries = load_demand_ledger(p)
    matched: Optional[Dict[str, Any]] = None
    for entry in entries:
        if (
            entry.get("query_key_normalized") == norm_key
            and entry.get("miss_reason") == reason
            and (entry.get("club_hint") or "") == clean_club
        ):
            matched = entry
            break

    if matched is not None:
        matched["hit_count"] = int(matched.get("hit_count") or 1) + 1
        matched["last_seen_utc"] = ts
        if signal_kind:
            matched["signal_kind"] = str(signal_kind).strip().upper()
        result_entry = matched
    else:
        result_entry = {
            "schema": DEMAND_LEDGER_SCHEMA,
            "query_key_normalized": norm_key,
            "miss_reason": reason,
            "club_hint": clean_club or None,
            "season_hint": clean_season or None,
            "signal_kind": str(signal_kind).strip().upper() if signal_kind else None,
            "first_seen_utc": ts,
            "last_seen_utc": ts,
            "hit_count": 1,
        }
        entries.append(result_entry)

    p.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(json.dumps(e, ensure_ascii=False, sort_keys=True) for e in entries) + "\n"
    atomic(p, payload.encode("utf-8"))
    return result_entry
