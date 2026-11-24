"""On-demand person card assembler for any CBA domestic or foreign player (REQ-223 P0-3).

Strictly local, zero-network, zero-Drive-API:
- Multi-source identity lookup & fail-closed disambiguation across domestic MASTER,
  foreign registration snapshots, and player_identity registry:
    * Unique hit -> RESOLVED
    * Ambiguous homonym / multi-candidate -> UNDECIDED (never auto-merges)
    * Missing -> NOT_FOUND_IN_LOCAL_KB (with data cutoff & active Negative-Receipt hit)
- Auto-assembles registration facts, PLAYER_SEASON_STATS, PLAYER_GAME_STATS recent window,
  and DocumentLane / operational_checkpoint announcements.
- Natively supports newly signed / unplayed players with Posture: UNKNOWN
  (unknown_subkind: NO_SEASON_STATS_RECORDED_YET) without raising.
"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .common import atomic
from .consumer_surface import (
    CONSUMER_MASTER_FILENAME,
    FOREIGN_REGISTRATION_CSV_FILENAME,
    scrub_raw_drive_ids_for_consumer,
)
from .game_stats import summarize_recent_games
from .instance import Instance
from .kc_render import render_research_card, research_card_consumer_filename


def _norm_key(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split()).casefold()


def _safe_id_for_filename(value: Any) -> str:
    raw = str(value or "unassigned").strip()
    if not raw:
        return "unassigned"
    return raw.replace(":", "_").replace("/", "_").replace("\\", "_")


def _read_csv_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def _read_json_or_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if path.suffix.lower() == ".jsonl":
        items: List[Dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if isinstance(obj, dict):
                items.append(obj)
        return items
    parsed = json.loads(text)
    if isinstance(parsed, list):
        return [x for x in parsed if isinstance(x, dict)]
    if isinstance(parsed, dict):
        return [parsed]
    return []


def _load_game_logs_from_dir(game_logs_dir: Path) -> List[Dict[str, Any]]:
    if not game_logs_dir.is_dir():
        return []
    records: List[Dict[str, Any]] = []
    for p in sorted(game_logs_dir.rglob("*"), key=lambda x: x.as_posix()):
        if not p.is_file() or p.suffix.lower() not in (".jsonl", ".json"):
            continue
        records.extend(_read_json_or_jsonl(p))
    return records


def _discover_instance_inputs(instance: Instance) -> Dict[str, Any]:
    """Read-only local discovery of standard Private Instance data files."""
    master_rows: List[Dict[str, Any]] = []
    foreign_rows: List[Dict[str, Any]] = []
    identity_registry: Dict[str, Any] = {"schema_version": 1, "players": [], "aliases": []}
    season_stats_records: List[Dict[str, Any]] = []
    game_stats_records: List[Dict[str, Any]] = []
    document_bundles: List[Dict[str, Any]] = []
    negative_receipts: List[Dict[str, Any]] = []
    data_cutoff: Optional[str] = None

    # 1. Domestic MASTER CSV
    for candidate in (
        instance.data_root / CONSUMER_MASTER_FILENAME,
        instance.data_root / "MASTER.csv",
        instance.root / "derived" / "MASTER.csv",
    ):
        if candidate.is_file():
            master_rows = _read_csv_rows(candidate)
            break

    # 2. Foreign registration CSV
    for candidate in (
        instance.data_root / FOREIGN_REGISTRATION_CSV_FILENAME,
        instance.data_root / "ops_sync_v041" / FOREIGN_REGISTRATION_CSV_FILENAME,
        instance.data_root / "foreign_players_registration_current.csv",
    ):
        if candidate.is_file():
            foreign_rows = _read_csv_rows(candidate)
            break

    # 3. Player identity registry
    for candidate in (
        instance.data_root / "player_identity" / "identity_registry.json",
        instance.data_root / "identity_registry.json",
    ):
        if candidate.is_file():
            try:
                loaded = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    identity_registry = loaded
            except (OSError, ValueError):
                pass
            break

    # 4. Season stats records (if present)
    for candidate in (
        instance.data_root / "season_stats.json",
        instance.data_root / "player_season_stats.json",
        instance.data_root / "season_stats.jsonl",
    ):
        if candidate.is_file():
            season_stats_records = _read_json_or_jsonl(candidate)
            break

    # 5. Game logs directory
    game_logs_dir = instance.data_root / "game_logs"
    if game_logs_dir.is_dir():
        game_stats_records = _load_game_logs_from_dir(game_logs_dir)

    # 6. Operational checkpoint bundles + cutoff timestamp
    for ckpt_candidate in (
        instance.data_root / "ops_sync_v041" / "operational_checkpoint.json",
        instance.data_root / "ops_sync" / "operational_checkpoint.json",
    ):
        if ckpt_candidate.is_file():
            try:
                ckpt = json.loads(ckpt_candidate.read_text(encoding="utf-8"))
                if isinstance(ckpt, dict):
                    if isinstance(ckpt.get("updated_at"), str):
                        data_cutoff = ckpt["updated_at"]
                    raw_bundles = ckpt.get("consumed_bundles")
                    if isinstance(raw_bundles, dict):
                        document_bundles = [
                            dict(v) for _, v in sorted(raw_bundles.items()) if isinstance(v, dict)
                        ]
                    elif isinstance(raw_bundles, list):
                        document_bundles = [dict(v) for v in raw_bundles if isinstance(v, dict)]
            except (OSError, ValueError):
                pass
            break

    # 7. Negative receipts
    for nr_candidate in (
        instance.data_root / "negative_receipts.jsonl",
        instance.data_root / "ledger" / "negative_receipts.jsonl",
    ):
        if nr_candidate.is_file():
            negative_receipts = _read_json_or_jsonl(nr_candidate)
            break

    return {
        "master_rows": master_rows,
        "foreign_rows": foreign_rows,
        "identity_registry": identity_registry,
        "season_stats_records": season_stats_records,
        "game_stats_records": game_stats_records,
        "document_bundles": document_bundles,
        "negative_receipts": negative_receipts,
        "data_cutoff": data_cutoff,
    }


def _find_active_negative_receipt(
    query: str,
    negative_receipts: Sequence[Dict[str, Any]],
    *,
    now_utc: datetime,
) -> Optional[Dict[str, Any]]:
    q_norm = _norm_key(query)
    if not q_norm:
        return None
    best: Optional[tuple[datetime, Dict[str, Any]]] = None
    for rec in negative_receipts or ():
        if not isinstance(rec, dict):
            continue
        if rec.get("schema") != "cba-kb.negative-receipt.v1":
            continue
        if _norm_key(rec.get("query_person")) != q_norm:
            continue
        checked_raw = rec.get("checked_at_utc")
        ttl_hours = rec.get("ttl_hours")
        if not isinstance(checked_raw, str) or isinstance(ttl_hours, bool) or not isinstance(ttl_hours, (int, float)):
            continue
        try:
            checked_dt = datetime.fromisoformat(checked_raw.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if checked_dt.tzinfo is None:
            checked_dt = checked_dt.replace(tzinfo=timezone.utc)
        expires_at = checked_dt + timedelta(hours=float(ttl_hours))
        if expires_at >= now_utc:
            if best is None or checked_dt > best[0]:
                best = (checked_dt, dict(rec))
    return best[1] if best is not None else None


def _build_candidate_pool(
    *,
    master_rows: Sequence[Dict[str, Any]],
    foreign_rows: Sequence[Dict[str, Any]],
    identity_registry: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build unified person candidate skeletons without merging distinct identities."""
    candidates: List[Dict[str, Any]] = []
    by_uid: Dict[str, Dict[str, Any]] = {}

    # 1. Seed from identity_registry
    reg = identity_registry if isinstance(identity_registry, dict) else {}
    aliases_by_uid: Dict[str, List[str]] = {}
    for a in reg.get("aliases") or ():
        if isinstance(a, dict) and a.get("player_uid") and a.get("alias"):
            aliases_by_uid.setdefault(str(a["player_uid"]), []).append(str(a["alias"]).strip())

    for p in reg.get("players") or ():
        if not isinstance(p, dict):
            continue
        uid = str(p.get("player_uid") or "").strip()
        cname = str(p.get("canonical_name") or "").strip()
        pid = str(p.get("provider_player_id") or p.get("player_id") or "").strip() or None
        club = str(p.get("club") or "").strip() or None
        if not uid and not cname:
            continue
        entry: Dict[str, Any] = {
            "player_uid": uid or None,
            "player_id": pid,
            "player_name": cname or uid,
            "player_name_en": str(p.get("name_en") or "").strip() or None,
            "aliases": list(aliases_by_uid.get(uid, [])),
            "current_club": club,
            "clubs_history": [club] if club else [],
            "registration_type": None,
            "jersey_number": None,
            "position": None,
            "domestic_records": [],
            "foreign_records": [],
        }
        candidates.append(entry)
        if uid:
            by_uid[uid] = entry

    # 2. Enrich or add from domestic MASTER rows
    by_name_domestic: Dict[str, List[Dict[str, Any]]] = {}
    for c in candidates:
        nk = _norm_key(c["player_name"])
        if nk:
            by_name_domestic.setdefault(nk, []).append(c)

    unmatched_master_by_name_club: Dict[tuple[str, str], Dict[str, Any]] = {}
    for row in master_rows or ():
        if not isinstance(row, dict):
            continue
        pname = str(row.get("player") or "").strip()
        if not pname:
            continue
        club = str(row.get("club_official") or row.get("club_id") or "").strip()
        nk = _norm_key(pname)
        existing = by_name_domestic.get(nk, [])
        target: Optional[Dict[str, Any]] = None
        if len(existing) == 1:
            target = existing[0]
        elif len(existing) > 1 and club:
            club_matches = [
                c for c in existing
                if _norm_key(c.get("current_club")) == _norm_key(club)
            ]
            if len(club_matches) == 1:
                target = club_matches[0]
        if target is None and not existing:
            key = (nk, _norm_key(club))
            if key not in unmatched_master_by_name_club:
                new_c: Dict[str, Any] = {
                    "player_uid": None,
                    "player_id": None,
                    "player_name": pname,
                    "player_name_en": None,
                    "aliases": [],
                    "current_club": club or None,
                    "clubs_history": [club] if club else [],
                    "registration_type": "DOMESTIC",
                    "jersey_number": None,
                    "position": None,
                    "domestic_records": [],
                    "foreign_records": [],
                }
                unmatched_master_by_name_club[key] = new_c
                candidates.append(new_c)
            target = unmatched_master_by_name_club[key]

        if target is not None:
            target["domestic_records"].append(dict(row))
            if not target.get("registration_type"):
                target["registration_type"] = "DOMESTIC"
            if club:
                if club not in target["clubs_history"]:
                    target["clubs_history"].append(club)
                target["current_club"] = club

    # 3. Enrich or add from foreign registration rows
    for row in foreign_rows or ():
        if not isinstance(row, dict):
            continue
        p_zh = str(row.get("player_zh") or "").strip()
        p_en = str(row.get("player_en") or "").strip()
        if not p_zh and not p_en:
            continue
        club = str(row.get("club") or "").strip()
        jersey = str(row.get("jersey_number") or "").strip() or None
        pos = str(row.get("position") or "").strip() or None

        matched_c: List[Dict[str, Any]] = []
        for c in candidates:
            c_keys = {
                _norm_key(c.get("player_name")),
                _norm_key(c.get("player_name_en")),
                *(_norm_key(a) for a in c.get("aliases") or ()),
            }
            c_keys.discard("")
            if (p_zh and _norm_key(p_zh) in c_keys) or (p_en and _norm_key(p_en) in c_keys):
                matched_c.append(c)

        if len(matched_c) == 1:
            target = matched_c[0]
        elif not matched_c:
            target = {
                "player_uid": None,
                "player_id": None,
                "player_name": p_zh or p_en,
                "player_name_en": p_en or None,
                "aliases": [p_en] if p_en and p_zh else [],
                "current_club": club or None,
                "clubs_history": [club] if club else [],
                "registration_type": "FOREIGN",
                "jersey_number": jersey,
                "position": pos,
                "domestic_records": [],
                "foreign_records": [],
            }
            candidates.append(target)
        else:
            # Ambiguous foreign match -> keep distinct rather than auto-merging
            target = None

        if target is not None:
            target["foreign_records"].append(dict(row))
            target["registration_type"] = "FOREIGN"
            if p_en and not target.get("player_name_en"):
                target["player_name_en"] = p_en
            if p_en and p_en not in target["aliases"]:
                target["aliases"].append(p_en)
            if jersey:
                target["jersey_number"] = jersey
            if pos:
                target["position"] = pos
            if club:
                if club not in target["clubs_history"]:
                    target["clubs_history"].append(club)
                target["current_club"] = club

    return candidates


def resolve_person_query(
    query: str,
    *,
    instance: Optional[Instance] = None,
    master_rows: Optional[Sequence[Dict[str, Any]]] = None,
    foreign_rows: Optional[Sequence[Dict[str, Any]]] = None,
    identity_registry: Optional[Dict[str, Any]] = None,
    negative_receipts: Optional[Sequence[Dict[str, Any]]] = None,
    data_cutoff: Optional[str] = None,
    now_utc: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Resolve a person query across domestic MASTER, foreign registration CSV, and identity registry.

    Returns status in {"RESOLVED", "UNDECIDED", "NOT_FOUND_IN_LOCAL_KB"}.
    Never auto-merges homonym candidates; never fabricates unknown persons.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("PERSON_QUERY_REQUIRED")

    discovered = _discover_instance_inputs(instance) if instance is not None else {}
    m_rows = list(master_rows) if master_rows is not None else list(discovered.get("master_rows") or [])
    f_rows = list(foreign_rows) if foreign_rows is not None else list(discovered.get("foreign_rows") or [])
    id_reg = identity_registry if identity_registry is not None else discovered.get("identity_registry")
    neg_recs = (
        list(negative_receipts)
        if negative_receipts is not None
        else list(discovered.get("negative_receipts") or [])
    )
    cutoff = data_cutoff if data_cutoff is not None else discovered.get("data_cutoff")
    now = now_utc if now_utc is not None else datetime.now(timezone.utc)

    q_clean = query.strip()
    q_norm = _norm_key(q_clean)
    pool = _build_candidate_pool(
        master_rows=m_rows,
        foreign_rows=f_rows,
        identity_registry=id_reg,
    )

    # 1. Exact ID match (player_uid or official player_id) disambiguates immediately
    id_matches = [
        c for c in pool
        if (c.get("player_uid") and _norm_key(c["player_uid"]) == q_norm)
        or (c.get("player_id") and _norm_key(c["player_id"]) == q_norm)
    ]
    if len(id_matches) == 1:
        person = id_matches[0]
        return {
            "status": "RESOLVED",
            "query": q_clean,
            "person": person,
            "candidates": [person],
            "local_data_cutoff": cutoff,
            "negative_receipt_hit": False,
            "negative_receipt": None,
        }

    # 2. Name / English name / alias match
    matched: List[Dict[str, Any]] = []
    for c in pool:
        keys = {
            _norm_key(c.get("player_name")),
            _norm_key(c.get("player_name_en")),
            *(_norm_key(a) for a in c.get("aliases") or ()),
        }
        keys.discard("")
        if q_norm in keys:
            matched.append(c)

    if len(matched) == 1:
        person = matched[0]
        return {
            "status": "RESOLVED",
            "query": q_clean,
            "person": person,
            "candidates": [person],
            "local_data_cutoff": cutoff,
            "negative_receipt_hit": False,
            "negative_receipt": None,
        }

    if len(matched) > 1:
        sorted_cands = sorted(
            matched,
            key=lambda c: (
                str(c.get("player_uid") or ""),
                str(c.get("player_id") or ""),
                str(c.get("current_club") or ""),
            ),
        )
        return {
            "status": "UNDECIDED",
            "query": q_clean,
            "person": None,
            "candidates": sorted_cands,
            "local_data_cutoff": cutoff,
            "negative_receipt_hit": False,
            "negative_receipt": None,
        }

    active_nr = _find_active_negative_receipt(q_clean, neg_recs, now_utc=now)
    return {
        "status": "NOT_FOUND_IN_LOCAL_KB",
        "query": q_clean,
        "person": None,
        "candidates": [],
        "local_data_cutoff": cutoff,
        "negative_receipt_hit": active_nr is not None,
        "negative_receipt": active_nr,
    }


def _person_match_tokens(person: Dict[str, Any]) -> set[str]:
    tokens = {
        _norm_key(person.get("player_uid")),
        _norm_key(person.get("player_id")),
        _norm_key(person.get("player_name")),
        _norm_key(person.get("player_name_en")),
        *(_norm_key(a) for a in person.get("aliases") or ()),
    }
    tokens.discard("")
    return tokens


def _match_season_stats(
    person: Dict[str, Any],
    season_stats_records: Sequence[Dict[str, Any]],
    *,
    season: str,
) -> List[Dict[str, Any]]:
    uid = _norm_key(person.get("player_uid"))
    pid = _norm_key(person.get("player_id"))
    pname = _norm_key(person.get("player_name"))

    matched: List[Dict[str, Any]] = []
    for rec in season_stats_records or ():
        if not isinstance(rec, dict):
            continue
        if season and rec.get("season") and str(rec["season"]) != str(season):
            continue
        r_uid = _norm_key(rec.get("player_uid"))
        r_pid = _norm_key(rec.get("provider_player_id"))
        r_name = _norm_key(rec.get("player_name"))
        if (uid and r_uid == uid) or (pid and r_pid == pid) or (not uid and not pid and pname and r_name == pname):
            matched.append(rec)
    matched.sort(
        key=lambda r: (
            0 if str(r.get("scope") or "").upper() == "WHOLE_SEASON" else 1,
            str(r.get("record_id") or ""),
        )
    )
    return matched


def _match_game_stats(
    person: Dict[str, Any],
    game_stats_records: Sequence[Dict[str, Any]],
    *,
    season: str,
) -> List[Dict[str, Any]]:
    uid = _norm_key(person.get("player_uid"))
    pid = _norm_key(person.get("player_id"))
    pname = _norm_key(person.get("player_name"))

    matched: List[Dict[str, Any]] = []
    for rec in game_stats_records or ():
        if not isinstance(rec, dict):
            continue
        if season and rec.get("season") and str(rec["season"]) != str(season):
            continue
        r_uid = _norm_key(rec.get("player_uid"))
        r_pid = _norm_key(rec.get("provider_player_id"))
        r_name = _norm_key(rec.get("player_name"))
        if (uid and r_uid == uid) or (pid and r_pid == pid) or (not uid and not pid and pname and r_name == pname):
            matched.append(rec)
    return matched


def _match_document_bundles(
    person: Dict[str, Any],
    document_bundles: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    tokens = _person_match_tokens(person)
    matched: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    for b in document_bundles or ():
        if not isinstance(b, dict):
            continue
        doc_id = str(b.get("doc_id") or "").strip()
        if doc_id and doc_id in seen_ids:
            continue
        mp = {_norm_key(x) for x in (b.get("matched_persons") or ()) if x}
        hit = bool(mp & tokens)
        if not hit:
            title_norm = _norm_key(b.get("title"))
            if any(len(tok) >= 2 and tok in title_norm for tok in tokens):
                hit = True
        if hit:
            if doc_id:
                seen_ids.add(doc_id)
            matched.append({
                "doc_id": doc_id or "-",
                "source_tier": b.get("source_tier") or "T2",
                "completeness": b.get("completeness") or "full",
                "published_at": b.get("published_at"),
                "title": b.get("title") or doc_id or "-",
                "canonical_url": b.get("canonical_url"),
                "disclosure_posture": b.get("disclosure_posture"),
            })
    return matched


def build_person_card(
    query: str,
    *,
    season: str = "2026-2027",
    recent_games_window: int = 5,
    instance: Optional[Instance] = None,
    master_rows: Optional[Sequence[Dict[str, Any]]] = None,
    foreign_rows: Optional[Sequence[Dict[str, Any]]] = None,
    identity_registry: Optional[Dict[str, Any]] = None,
    season_stats_records: Optional[Sequence[Dict[str, Any]]] = None,
    game_stats_records: Optional[Sequence[Dict[str, Any]]] = None,
    document_bundles: Optional[Sequence[Dict[str, Any]]] = None,
    negative_receipts: Optional[Sequence[Dict[str, Any]]] = None,
    output_dir: Optional[str | Path] = None,
    register_for_sync: bool = False,
    known_drive_ids: Sequence[str] = (),
) -> Dict[str, Any]:
    """Assemble and render an on-demand research card for any player in local KB.

    Zero network calls, zero Google Drive API calls.
    """
    discovered = _discover_instance_inputs(instance) if instance is not None else {}
    m_rows = list(master_rows) if master_rows is not None else list(discovered.get("master_rows") or [])
    f_rows = list(foreign_rows) if foreign_rows is not None else list(discovered.get("foreign_rows") or [])
    id_reg = identity_registry if identity_registry is not None else discovered.get("identity_registry")
    s_stats = (
        list(season_stats_records)
        if season_stats_records is not None
        else list(discovered.get("season_stats_records") or [])
    )
    g_stats = (
        list(game_stats_records)
        if game_stats_records is not None
        else list(discovered.get("game_stats_records") or [])
    )
    doc_bundles = (
        list(document_bundles)
        if document_bundles is not None
        else list(discovered.get("document_bundles") or [])
    )
    neg_recs = (
        list(negative_receipts)
        if negative_receipts is not None
        else list(discovered.get("negative_receipts") or [])
    )

    resolution = resolve_person_query(
        query,
        master_rows=m_rows,
        foreign_rows=f_rows,
        identity_registry=id_reg,
        negative_receipts=neg_recs,
        data_cutoff=discovered.get("data_cutoff"),
    )

    if resolution["status"] != "RESOLVED":
        return {
            "status": resolution["status"],
            "query": resolution["query"],
            "season": season,
            "person": None,
            "candidates": resolution["candidates"],
            "local_data_cutoff": resolution["local_data_cutoff"],
            "negative_receipt_hit": resolution["negative_receipt_hit"],
            "negative_receipt": resolution["negative_receipt"],
            "posture": None,
            "unknown_subkind": None,
            "recent_games_summary": None,
            "filename": None,
            "card_path": None,
            "card_sha256": None,
            "markdown": None,
            "drive_api_called": False,
            "network_called": False,
        }

    person = resolution["person"]
    matched_season = _match_season_stats(person, s_stats, season=season)
    matched_games = _match_game_stats(person, g_stats, season=season)
    recent_summary = (
        summarize_recent_games(matched_games, window=recent_games_window)
        if matched_games
        else None
    )
    citations = _match_document_bundles(person, doc_bundles)

    player_name = str(person.get("player_name") or query.strip())
    player_id = person.get("player_id") or person.get("player_uid") or "unassigned"

    if matched_season:
        primary = matched_season[0]
        metrics = primary.get("metrics") or {}
        prov = primary.get("provenance") or {}
        metric_name = "points_per_game" if "points_per_game" in metrics else (
            next(iter(metrics), "points_per_game")
        )
        adopted_val = metrics.get(metric_name)
        posture = "CLEAN_OFFICIAL"
        unknown_subkind: Optional[str] = None
        payload: Dict[str, Any] = {
            "metric": metric_name,
            "grains": {
                "STATS": {
                    "posture": posture,
                    "season": str(primary.get("season") or season),
                    "match_type": str(primary.get("match_type") or "1"),
                    "scope": str(primary.get("scope") or "WHOLE_SEASON"),
                    "metric": metric_name,
                    "value": adopted_val,
                },
            },
            "evidence_transparency": {
                "posture": posture,
                "adopted_value": adopted_val,
                "subject_player_uid": person.get("player_uid"),
                "scope_meta": {
                    "player_name": player_name,
                    "player_id": str(player_id),
                    "team_id": primary.get("team_id"),
                    "team_name": primary.get("team_name") or person.get("current_club"),
                    "season": str(primary.get("season") or season),
                    "match_type": str(primary.get("match_type") or "1"),
                    "scope": str(primary.get("scope") or "WHOLE_SEASON"),
                    "games_played": metrics.get("games_played"),
                    "captured_at": prov.get("captured_at"),
                    "locator": prov.get("locator") or primary.get("record_id"),
                },
            },
        }
    else:
        posture = "UNKNOWN"
        unknown_subkind = "NO_SEASON_STATS_RECORDED_YET"
        payload = {
            "metric": "points_per_game",
            "grains": {
                "STATS": {
                    "posture": posture,
                    "season": season,
                    "match_type": "-",
                    "scope": "-",
                },
            },
            "evidence_transparency": {
                "posture": posture,
                "unknown_subkind": unknown_subkind,
                "subject_player_uid": person.get("player_uid"),
                "scope_meta": {
                    "player_name": player_name,
                    "player_id": str(player_id),
                    "team_name": person.get("current_club"),
                    "season": season,
                },
            },
        }

    payload["registration_profile"] = {
        "player_name": player_name,
        "player_name_en": person.get("player_name_en"),
        "player_uid": person.get("player_uid"),
        "player_id": person.get("player_id"),
        "current_club": person.get("current_club"),
        "clubs_history": person.get("clubs_history") or [],
        "registration_type": person.get("registration_type"),
        "position": person.get("position"),
        "jersey_number": person.get("jersey_number"),
    }
    if recent_summary is not None:
        payload["recent_games_summary"] = recent_summary
    if citations:
        payload["external_citations"] = citations

    raw_markdown = render_research_card(payload, evidence_verified=True)
    markdown = scrub_raw_drive_ids_for_consumer(raw_markdown, known_drive_ids=known_drive_ids)
    if not markdown.rstrip().endswith("end_marker: true"):
        markdown = markdown.rstrip() + "\n\nend_marker: true\n"

    filename = research_card_consumer_filename(
        player_name,
        _safe_id_for_filename(player_id),
        season,
    )
    card_bytes = markdown.encode("utf-8")
    card_sha256 = hashlib.sha256(card_bytes).hexdigest()
    card_path_str: Optional[str] = None

    if output_dir is not None:
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        card_file = out_path / filename
        atomic(card_file, card_bytes)
        card_path_str = str(card_file)

        if register_for_sync:
            manifest_path = out_path / "on_demand_cards_manifest.json"
            manifest: Dict[str, Any] = {"schema_version": 1, "cards": {}}
            if manifest_path.is_file():
                try:
                    loaded_m = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if isinstance(loaded_m, dict) and isinstance(loaded_m.get("cards"), dict):
                        manifest = loaded_m
                except (OSError, ValueError):
                    pass
            key = str(person.get("player_uid") or player_id)
            manifest["cards"][key] = {
                "player_uid": person.get("player_uid"),
                "player_id": person.get("player_id"),
                "player_name": player_name,
                "season": season,
                "filename": filename,
                "card_sha256": card_sha256,
                "posture": posture,
                "unknown_subkind": unknown_subkind,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            atomic(
                manifest_path,
                (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
            )

    return {
        "status": "BUILT",
        "query": resolution["query"],
        "season": season,
        "person": person,
        "candidates": resolution["candidates"],
        "posture": posture,
        "unknown_subkind": unknown_subkind,
        "recent_games_summary": recent_summary,
        "filename": filename,
        "card_path": card_path_str,
        "card_sha256": card_sha256,
        "markdown": markdown,
        "drive_api_called": False,
        "network_called": False,
    }
