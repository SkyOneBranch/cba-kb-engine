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
    _is_prior_season,
    _parse_season_start_year,
    build_person_domain_horizons,
    evaluate_league_coverage,
    find_boundary_safe_name_spans,
    scrub_raw_drive_ids_for_consumer,
)
from .game_stats import summarize_recent_games
from .instance import Instance
from .kc_render import (
    render_research_card,
    research_card_consumer_filename,
    research_card_public_consumer_filename,
)
from .kc_visibility import validate_card_visibility, verify_public_safe_card_text
from .stats import CORE_SEASON_STATS_COLUMNS


def _norm_key(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split()).casefold()


def _safe_id_for_filename(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "待核俱乐部"
    cleaned = raw.replace(":", "_").replace("/", "_").replace("\\", "_").replace("\x00", "")
    cleaned = cleaned.replace("..", "_")
    cleaned = "_".join(cleaned.split())
    if not cleaned or "unassigned" in cleaned.lower():
        return "待核俱乐部"
    return cleaned


def _resolve_filename_middle_segment(
    person: Dict[str, Any],
    *,
    current_club: Optional[str] = None,
) -> str:
    """Derive consumer card filename middle segment without ever emitting `unassigned`.

    Priority order (REQ-231 P0-2):
    1. Sanitized current club name
    2. Non-empty player_id (not `unassigned`)
    3. Non-empty player_uid (not `unassigned`)
    4. Literal `待核俱乐部`
    """
    for candidate in (
        current_club,
        person.get("current_club"),
        person.get("player_id"),
        person.get("player_uid"),
    ):
        raw = str(candidate or "").strip()
        if not raw:
            continue
        cleaned = raw.replace(":", "_").replace("/", "_").replace("\\", "_").replace("\x00", "")
        cleaned = cleaned.replace("..", "_")
        cleaned = "_".join(cleaned.split())
        if cleaned and "unassigned" not in cleaned.lower():
            return cleaned
    return "待核俱乐部"


def _build_domestic_registration_blocks(
    domestic_records: Sequence[Dict[str, Any]],
    *,
    target_season: str,
) -> tuple[Optional[Dict[str, str]], List[Dict[str, str]]]:
    """Build current-season domestic contract summary and up to 3-season descending history."""
    by_season: Dict[str, Dict[str, str]] = {}
    for rec in domestic_records or ():
        if not isinstance(rec, dict):
            continue
        season_str = str(rec.get("season") or "").strip()
        summary = {
            "season": season_str,
            "club_official": str(rec.get("club_official") or rec.get("club") or "").strip(),
            "registration_stage": str(rec.get("registration_stage") or "").strip(),
            "registration_method": str(rec.get("registration_method") or "").strip(),
            "contract_category": str(rec.get("contract_category") or "").strip(),
            "contract_term_official": str(rec.get("contract_term_official") or "").strip(),
            "former_club": str(rec.get("former_club") or "").strip(),
            "registration_status": str(rec.get("registration_status") or "").strip(),
            "remarks": str(rec.get("remarks") or "").strip(),
        }
        by_season[season_str] = summary

    if not by_season:
        return None, []

    sorted_desc = sorted(
        by_season.values(),
        key=lambda item: item["season"],
        reverse=True,
    )
    target_key = str(target_season or "").strip()
    domestic_current = by_season.get(target_key) or sorted_desc[0]
    domestic_history = sorted_desc[:3]
    return domestic_current, domestic_history


def _build_foreign_registration_block(
    foreign_records: Sequence[Dict[str, Any]],
    *,
    person: Dict[str, Any],
    player_name: str,
) -> Optional[Dict[str, str]]:
    """Build structured foreign registration block from the latest snapshot record."""
    valid = [r for r in (foreign_records or ()) if isinstance(r, dict)]
    if not valid:
        return None
    latest = max(
        enumerate(valid),
        key=lambda pair: (str(pair[1].get("published_at") or ""), pair[0]),
    )[1]
    return {
        "club": str(latest.get("club") or person.get("current_club") or "").strip(),
        "player_en": str(latest.get("player_en") or person.get("player_name_en") or "").strip(),
        "player_zh": str(latest.get("player_zh") or player_name or "").strip(),
        "nationality": str(latest.get("nationality") or "").strip(),
        "position": str(latest.get("position") or person.get("position") or "").strip(),
        "jersey_number": str(latest.get("jersey_number") or person.get("jersey_number") or "").strip(),
        "published_at": str(latest.get("published_at") or "").strip(),
        "doc_id": str(latest.get("doc_id") or "").strip(),
        "raw_sha256": str(latest.get("raw_sha256") or "").strip(),
    }


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

    # 8. Coverage ledger (REQ-230 P0-1)
    coverage_ledger: Optional[Dict[str, Any]] = None
    ledger_candidate = instance.data_root / "coverage_ledger.json"
    if ledger_candidate.is_file():
        try:
            loaded_ledger = json.loads(ledger_candidate.read_text(encoding="utf-8"))
            if isinstance(loaded_ledger, dict):
                coverage_ledger = loaded_ledger
        except (OSError, ValueError):
            pass

    return {
        "master_rows": master_rows,
        "foreign_rows": foreign_rows,
        "identity_registry": identity_registry,
        "season_stats_records": season_stats_records,
        "game_stats_records": game_stats_records,
        "document_bundles": document_bundles,
        "negative_receipts": negative_receipts,
        "data_cutoff": data_cutoff,
        "coverage_ledger": coverage_ledger,
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
    by_pid: Dict[str, Dict[str, Any]] = {}

    # 1. Seed from identity_registry
    reg = identity_registry if isinstance(identity_registry, dict) else {}
    aliases_by_uid: Dict[str, List[str]] = {}
    for a in reg.get("aliases") or ():
        if isinstance(a, dict) and a.get("player_uid") and a.get("alias"):
            aliases_by_uid.setdefault(str(a["player_uid"]), []).append(str(a["alias"]).strip())

    trusted_record_links: Dict[str, str] = {}
    for link in reg.get("record_links") or ():
        if (
            isinstance(link, dict)
            and link.get("link_status") == "same"
            and link.get("record_key")
            and link.get("player_uid")
        ):
            trusted_record_links[str(link["record_key"]).strip()] = str(link["player_uid"]).strip()

    for p in reg.get("players") or ():
        if not isinstance(p, dict):
            continue
        uid = str(p.get("player_uid") or "").strip()
        cname = str(p.get("canonical_name") or "").strip()
        pid = str(p.get("provider_player_id") or p.get("player_id") or "").strip() or None
        club_primary = str(p.get("club") or p.get("club_official") or p.get("club_id") or "").strip() or None
        clubs_hist: List[str] = []
        for raw_c in (
            p.get("club"),
            p.get("club_official"),
            *(p.get("clubs_history") or () if isinstance(p.get("clubs_history"), list) else ()),
        ):
            sc = str(raw_c or "").strip()
            if sc and sc not in clubs_hist:
                clubs_hist.append(sc)
        if not clubs_hist and club_primary:
            clubs_hist.append(club_primary)
        reg_club_tokens = {
            _norm_key(p.get("club")),
            _norm_key(p.get("club_official")),
            _norm_key(p.get("club_id")),
            *(_norm_key(x) for x in clubs_hist),
        }
        reg_club_tokens.discard("")
        if not uid and not cname:
            continue
        entry: Dict[str, Any] = {
            "player_uid": uid or None,
            "player_id": pid,
            "player_name": cname or uid,
            "player_name_en": str(p.get("name_en") or "").strip() or None,
            "aliases": list(aliases_by_uid.get(uid, [])),
            "current_club": club_primary,
            "clubs_history": clubs_hist,
            "_reg_club_tokens": reg_club_tokens,
            "registration_type": None,
            "jersey_number": None,
            "position": None,
            "domestic_records": [],
            "foreign_records": [],
        }
        candidates.append(entry)
        if uid:
            by_uid[uid] = entry
        if pid:
            by_pid.setdefault(pid, entry)

    def _candidate_clubs_norm(c: Dict[str, Any]) -> set[str]:
        clubs = {_norm_key(c.get("current_club"))}
        clubs.update(_norm_key(x) for x in (c.get("clubs_history") or ()))
        clubs.update(c.get("_reg_club_tokens") or ())
        for rec in c.get("domestic_records") or ():
            if isinstance(rec, dict):
                clubs.add(_norm_key(rec.get("club_official")))
                clubs.add(_norm_key(rec.get("club_id")))
        clubs.discard("")
        return clubs

    def _is_club_compatible(
        c: Dict[str, Any],
        row_clubs_norm: set[str],
        *,
        former_club_norm: str = "",
    ) -> bool:
        # Fail closed when either side lacks club evidence: missing club is never compatibility proof.
        if not row_clubs_norm:
            return False
        c_clubs = _candidate_clubs_norm(c)
        if not c_clubs:
            return False
        if c_clubs & row_clubs_norm:
            return True
        if former_club_norm and former_club_norm in c_clubs:
            return True
        return False

    # 2. Enrich or add from domestic MASTER rows (fail-closed on missing/conflicting club or unknown explicit ID)
    by_name_domestic: Dict[str, List[Dict[str, Any]]] = {}
    for c in candidates:
        nk = _norm_key(c["player_name"])
        if nk:
            by_name_domestic.setdefault(nk, []).append(c)

    unmatched_master_by_name_club: Dict[tuple[str, str], Dict[str, Any]] = {}
    for row in master_rows or ():
        if not isinstance(row, dict):
            continue
        pname = str(row.get("player") or row.get("player_name") or "").strip()
        if not pname:
            continue
        club_official = str(row.get("club_official") or row.get("club") or "").strip()
        club_id = str(row.get("club_id") or "").strip()
        club = club_official or club_id
        row_clubs_norm = {_norm_key(club_official), _norm_key(club_id)}
        row_clubs_norm.discard("")
        former_club_norm = _norm_key(row.get("former_club"))
        nk = _norm_key(pname)

        target: Optional[Dict[str, Any]] = None
        # Explicit stable identifier binding takes precedence and NEVER falls back to name merge
        row_uid = str(row.get("player_uid") or "").strip()
        row_pid = str(row.get("provider_player_id") or row.get("player_id") or "").strip()
        if not row_uid:
            rk = str(row.get("record_key") or "").strip()
            if rk and rk in trusted_record_links:
                row_uid = trusted_record_links[rk]

        if row_uid:
            if row_uid in by_uid:
                target = by_uid[row_uid]
            else:
                new_c = {
                    "player_uid": row_uid,
                    "player_id": row_pid or None,
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
                candidates.append(new_c)
                by_uid[row_uid] = new_c
                if row_pid:
                    by_pid.setdefault(row_pid, new_c)
                by_name_domestic.setdefault(nk, []).append(new_c)
                target = new_c
        elif row_pid:
            if row_pid in by_pid:
                target = by_pid[row_pid]
            else:
                new_c = {
                    "player_uid": None,
                    "player_id": row_pid,
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
                candidates.append(new_c)
                by_pid[row_pid] = new_c
                by_name_domestic.setdefault(nk, []).append(new_c)
                target = new_c
        else:
            existing = by_name_domestic.get(nk, [])
            compatible = [
                c for c in existing
                if _is_club_compatible(c, row_clubs_norm, former_club_norm=former_club_norm)
            ]
            if len(compatible) == 1:
                target = compatible[0]

        if target is None:
            # Never auto-merge across missing/conflicting clubs or ambiguous candidates
            key = (nk, _norm_key(club))
            if not club or key not in unmatched_master_by_name_club:
                new_c = {
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
                if club:
                    unmatched_master_by_name_club[key] = new_c
                candidates.append(new_c)
                by_name_domestic.setdefault(nk, []).append(new_c)
                target = new_c
            else:
                target = unmatched_master_by_name_club[key]

        target["domestic_records"].append(dict(row))
        if not target.get("registration_type"):
            target["registration_type"] = "DOMESTIC"
        if row.get("position") and not target.get("position"):
            target["position"] = str(row.get("position")).strip() or None
        if row.get("jersey_number") and not target.get("jersey_number"):
            target["jersey_number"] = str(row.get("jersey_number")).strip() or None
        if club:
            if club not in target["clubs_history"]:
                target["clubs_history"].append(club)
            target["current_club"] = club

    # 3. Enrich or add from foreign registration rows (fail-closed on missing/conflicting club or unknown explicit ID)
    unmatched_foreign_by_key: Dict[tuple[str, str, str], Dict[str, Any]] = {}
    for row in foreign_rows or ():
        if not isinstance(row, dict):
            continue
        p_zh = str(row.get("player_zh") or "").strip()
        p_en = str(row.get("player_en") or "").strip()
        if not p_zh and not p_en:
            continue
        club = str(row.get("club") or "").strip()
        row_clubs_norm = {_norm_key(club)} if club else set()
        jersey = str(row.get("jersey_number") or "").strip() or None
        pos = str(row.get("position") or "").strip() or None

        target = None
        row_uid = str(row.get("player_uid") or "").strip()
        row_pid = str(row.get("provider_player_id") or row.get("player_id") or "").strip()
        if row_uid:
            if row_uid in by_uid:
                target = by_uid[row_uid]
            else:
                new_f = {
                    "player_uid": row_uid,
                    "player_id": row_pid or None,
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
                candidates.append(new_f)
                by_uid[row_uid] = new_f
                if row_pid:
                    by_pid.setdefault(row_pid, new_f)
                target = new_f
        elif row_pid:
            if row_pid in by_pid:
                target = by_pid[row_pid]
            else:
                new_f = {
                    "player_uid": None,
                    "player_id": row_pid,
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
                candidates.append(new_f)
                by_pid[row_pid] = new_f
                target = new_f
        else:
            matched_c: List[Dict[str, Any]] = []
            for c in candidates:
                c_keys = {
                    _norm_key(c.get("player_name")),
                    _norm_key(c.get("player_name_en")),
                    *(_norm_key(a) for a in c.get("aliases") or ()),
                }
                c_keys.discard("")
                if (p_zh and _norm_key(p_zh) in c_keys) or (p_en and _norm_key(p_en) in c_keys):
                    if _is_club_compatible(c, row_clubs_norm):
                        matched_c.append(c)
            if len(matched_c) == 1:
                target = matched_c[0]

        if target is None:
            fkey = (_norm_key(p_zh), _norm_key(p_en), _norm_key(club))
            if not club or fkey not in unmatched_foreign_by_key:
                new_f = {
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
                if club:
                    unmatched_foreign_by_key[fkey] = new_f
                candidates.append(new_f)
                target = new_f
            else:
                target = unmatched_foreign_by_key[fkey]

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

    for c in candidates:
        c.pop("_reg_club_tokens", None)
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


def _season_stats_sort_key(rec: Dict[str, Any]) -> tuple[int, int, str, str]:
    mt = str(rec.get("match_type") or "").strip().upper()
    if mt in ("REGULAR", "1", "常规赛"):
        mt_rank = 0
    elif mt in ("PLAYOFFS", "2", "季后赛"):
        mt_rank = 1
    else:
        mt_rank = 2
    sc = str(rec.get("scope") or "").strip().upper()
    if sc == "WHOLE_SEASON":
        sc_rank = 0
    elif sc == "TEAM_SPLIT":
        sc_rank = 1
    else:
        sc_rank = 2
    team_key = str(rec.get("team_name") or rec.get("team_id") or "")
    rec_id = str(rec.get("record_id") or "")
    return (mt_rank, sc_rank, team_key, rec_id)


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
    matched.sort(key=_season_stats_sort_key)
    return matched


def _match_prior_season_stats(
    person: Dict[str, Any],
    season_stats_records: Sequence[Dict[str, Any]],
    *,
    target_season: str,
) -> tuple[Optional[str], List[Dict[str, Any]]]:
    """Find the latest prior season (< target_season) stats records for a resolved person."""
    uid = _norm_key(person.get("player_uid"))
    pid = _norm_key(person.get("player_id"))
    pname = _norm_key(person.get("player_name"))

    by_season: Dict[str, List[Dict[str, Any]]] = {}
    for rec in season_stats_records or ():
        if not isinstance(rec, dict):
            continue
        rec_season = str(rec.get("season") or "").strip()
        if not rec_season or not _is_prior_season(rec_season, target_season):
            continue
        r_uid = _norm_key(rec.get("player_uid"))
        r_pid = _norm_key(rec.get("provider_player_id"))
        r_name = _norm_key(rec.get("player_name"))
        if (uid and r_uid == uid) or (pid and r_pid == pid) or (not uid and not pid and pname and r_name == pname):
            by_season.setdefault(rec_season, []).append(rec)

    if not by_season:
        return None, []

    latest_prior = max(
        by_season.keys(),
        key=lambda s: (_parse_season_start_year(s) or -1, s),
    )
    rows = sorted(by_season[latest_prior], key=_season_stats_sort_key)
    return latest_prior, rows


def _build_season_stats_table_rows(
    records: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for rec in sorted(records, key=_season_stats_sort_key):
        if not isinstance(rec, dict):
            continue
        rec_metrics = rec.get("metrics") if isinstance(rec.get("metrics"), dict) else {}
        prov = rec.get("provenance") if isinstance(rec.get("provenance"), dict) else {}
        core_metrics = {col: rec_metrics.get(col) for col in CORE_SEASON_STATS_COLUMNS}
        rows.append({
            "season": str(rec.get("season") or ""),
            "match_type": str(rec.get("match_type") or "REGULAR"),
            "scope": str(rec.get("scope") or "WHOLE_SEASON"),
            "team_id": rec.get("team_id"),
            "team_name": rec.get("team_name"),
            "record_id": rec.get("record_id"),
            "captured_at": prov.get("captured_at"),
            "locator": prov.get("locator") or rec.get("record_id"),
            "metrics": core_metrics,
        })
    return rows


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


def _is_club_adjacent_to_name(text: str, player_name: str, club: str) -> bool:
    if not text or not player_name or not club:
        return False
    t_norm = _norm_key(text)
    p_norm = _norm_key(player_name)
    c_norm = _norm_key(club)
    if not p_norm or not c_norm or c_norm not in t_norm:
        return False
    for pattern in (
        f"{p_norm}（{c_norm}）",
        f"{p_norm}({c_norm})",
        f"{p_norm}（{c_norm}",
        f"{p_norm}({c_norm}",
        f"{c_norm}{p_norm}",
        f"{c_norm} {p_norm}",
    ):
        if pattern in t_norm:
            return True
    return False


def _match_document_bundles(
    person: Dict[str, Any],
    document_bundles: Sequence[Dict[str, Any]],
    *,
    roster_candidates: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    tokens = _person_match_tokens(person)
    id_tokens = {
        _norm_key(person.get("player_uid")),
        _norm_key(person.get("player_id")),
    }
    id_tokens.discard("")

    person_name_raw = str(person.get("player_name") or "").strip()
    person_name_norm = _norm_key(person_name_raw)
    person_clubs_raw: List[str] = []
    for raw_c in (
        person.get("current_club"),
        *(person.get("clubs_history") or ()),
    ):
        sc = str(raw_c or "").strip()
        if sc and sc not in person_clubs_raw:
            person_clubs_raw.append(sc)
    person_clubs_norm = {_norm_key(c) for c in person_clubs_raw} - {""}

    is_homonym = False
    other_homonym_clubs: List[str] = []
    enrich_fn = None
    if roster_candidates is not None:
        from .consumer_surface import enrich_bundle_matched_persons

        enrich_fn = enrich_bundle_matched_persons
        if person_name_norm:
            same_name = [
                c
                for c in roster_candidates
                if isinstance(c, dict)
                and _norm_key(c.get("player_name") or c.get("player")) == person_name_norm
            ]
            distinct_keys = {
                (
                    _norm_key(c.get("player_uid")),
                    _norm_key(c.get("player_id")),
                    _norm_key(c.get("current_club") or c.get("club_official") or c.get("club")),
                )
                for c in same_name
            }
            if len(distinct_keys) >= 2:
                is_homonym = True
                p_uid = _norm_key(person.get("player_uid"))
                for c in same_name:
                    c_club = str(
                        c.get("current_club") or c.get("club_official") or c.get("club") or ""
                    ).strip()
                    c_uid = _norm_key(c.get("player_uid"))
                    if c_club and _norm_key(c_club) not in person_clubs_norm:
                        if not (p_uid and c_uid and p_uid == c_uid):
                            if c_club not in other_homonym_clubs:
                                other_homonym_clubs.append(c_club)

    matched: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    for b in document_bundles or ():
        if not isinstance(b, dict):
            continue
        doc_id = str(b.get("doc_id") or "").strip()
        if doc_id and doc_id in seen_ids:
            continue

        effective_bundle = (
            enrich_fn(b, roster_candidates=roster_candidates)
            if enrich_fn is not None
            else b
        )
        mp = {_norm_key(x) for x in (effective_bundle.get("matched_persons") or ()) if x}

        if is_homonym:
            hit = bool(id_tokens and (mp & id_tokens))
            if not hit and (mp & tokens):
                text = f"{b.get('title') or ''}\n{b.get('body') or ''}"
                text_norm = _norm_key(text)
                own_adjacent = any(
                    _is_club_adjacent_to_name(text, person_name_raw, c)
                    for c in person_clubs_raw
                )
                other_adjacent = any(
                    _is_club_adjacent_to_name(text, person_name_raw, c)
                    for c in other_homonym_clubs
                )
                if own_adjacent:
                    hit = True
                elif other_adjacent:
                    hit = False
                else:
                    has_own = any(cn in text_norm for cn in person_clubs_norm)
                    has_other = any(
                        _norm_key(c) in text_norm
                        for c in other_homonym_clubs
                        if _norm_key(c)
                    )
                    hit = bool(has_own and not has_other)
        else:
            hit = bool(mp & tokens)
            if not hit:
                title_norm = _norm_key(b.get("title"))
                candidate_names: List[str] = []
                if roster_candidates:
                    candidate_names = sorted(
                        {
                            _norm_key(
                                c.get("player_name")
                                or c.get("canonical_name")
                                or c.get("player")
                                or c.get("player_zh")
                            )
                            for c in roster_candidates
                            if isinstance(c, dict)
                        }
                        - tokens
                        - {""},
                        key=lambda n: (-len(n), n),
                    )
                for tok in sorted(tokens, key=lambda t: (-len(t), t)):
                    if len(tok) < 2:
                        continue
                    occupied_title_spans: List[tuple[int, int]] = []
                    for lname in candidate_names:
                        if len(lname) > len(tok):
                            occupied_title_spans.extend(
                                find_boundary_safe_name_spans(
                                    title_norm,
                                    lname,
                                    occupied_spans=occupied_title_spans,
                                )
                            )
                    if find_boundary_safe_name_spans(
                        title_norm,
                        tok,
                        occupied_spans=occupied_title_spans,
                    ):
                        hit = True
                        break

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
    coverage_ledger: Optional[Dict[str, Any]] = None,
    observation_horizon: Optional[Dict[str, Any]] = None,
    game_stats_degraded: bool = False,
    entity_role: Optional[str] = None,
    visibility: str = "internal_full",
) -> Dict[str, Any]:
    """Assemble and render an on-demand research card for any player in local KB.

    Zero network calls, zero Google Drive API calls.
    """
    vis = validate_card_visibility(visibility)
    discovered = _discover_instance_inputs(instance) if instance is not None else {}
    obs_horizon: Optional[Dict[str, Any]] = None
    if isinstance(observation_horizon, dict):
        obs_horizon = dict(observation_horizon)
    elif isinstance(coverage_ledger, dict):
        obs_horizon = evaluate_league_coverage(coverage_ledger)
    elif isinstance(discovered.get("coverage_ledger"), dict):
        obs_horizon = evaluate_league_coverage(discovered["coverage_ledger"])
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
    roster_pool = (
        _build_candidate_pool(
            master_rows=m_rows,
            foreign_rows=f_rows,
            identity_registry=id_reg,
        )
        if doc_bundles
        else None
    )
    citations = _match_document_bundles(person, doc_bundles, roster_candidates=roster_pool)

    player_name = str(person.get("player_name") or query.strip())
    player_id = person.get("player_id") or person.get("player_uid") or "unassigned"
    domestic_current, domestic_history = _build_domestic_registration_blocks(
        person.get("domestic_records") or (),
        target_season=season,
    )
    foreign_registration = _build_foreign_registration_block(
        person.get("foreign_records") or (),
        person=person,
        player_name=player_name,
    )
    effective_club = (
        (domestic_current or {}).get("club_official")
        or (foreign_registration or {}).get("club")
        or person.get("current_club")
    )

    prior_season_baseline: Optional[Dict[str, Any]] = None
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
        payload["season_stats_table"] = _build_season_stats_table_rows(matched_season)
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
        prior_season, prior_recs = _match_prior_season_stats(
            person, s_stats, target_season=season
        )
        if prior_season and prior_recs:
            prior_rows = _build_season_stats_table_rows(prior_recs)
            prior_primary = prior_recs[0]
            prior_prov = (
                prior_primary.get("provenance")
                if isinstance(prior_primary.get("provenance"), dict)
                else {}
            )
            prior_season_baseline = {
                "season": prior_season,
                "target_season": season,
                "rows": prior_rows,
                "captured_at": prior_prov.get("captured_at"),
                "locator": prior_prov.get("locator") or prior_primary.get("record_id"),
            }
            payload["prior_season_baseline"] = prior_season_baseline

    effective_role = entity_role or str(person.get("entity_role") or "PLAYER")
    has_reg_records = bool(person.get("domestic_records") or person.get("foreign_records"))
    reg_obs_until = (foreign_registration or {}).get("published_at") or discovered.get("data_cutoff")
    stats_obs_until = None
    if matched_season:
        first_prov = matched_season[0].get("provenance")
        if isinstance(first_prov, dict):
            stats_obs_until = first_prov.get("captured_at")
    if not stats_obs_until and isinstance(recent_summary, dict):
        stats_obs_until = recent_summary.get("latest_captured_at")
    if not stats_obs_until:
        stats_obs_until = discovered.get("data_cutoff")
    ann_obs_until = discovered.get("data_cutoff")

    domain_horizons = build_person_domain_horizons(
        entity_role=effective_role,
        has_registration_records=has_reg_records,
        registration_observed_until=reg_obs_until,
        has_season_stats=bool(matched_season),
        has_game_stats=bool(matched_games),
        stats_observed_until=stats_obs_until,
        game_stats_degraded=bool(game_stats_degraded),
        has_official_announcements=bool(citations),
        announcements_observed_until=ann_obs_until,
        coverage_eval=obs_horizon,
    )
    payload["domain_horizons"] = domain_horizons

    reg_profile: Dict[str, Any] = {
        "player_name": player_name,
        "player_name_en": person.get("player_name_en"),
        "player_uid": person.get("player_uid"),
        "player_id": person.get("player_id"),
        "current_club": effective_club,
        "clubs_history": person.get("clubs_history") or [],
        "registration_type": person.get("registration_type"),
        "position": person.get("position"),
        "jersey_number": person.get("jersey_number"),
    }
    if domestic_current is not None:
        reg_profile["domestic_current"] = domestic_current
    if domestic_history:
        reg_profile["domestic_history"] = domestic_history
    if foreign_registration is not None:
        reg_profile["foreign_registration"] = foreign_registration
    payload["registration_profile"] = reg_profile

    if recent_summary is not None:
        payload["recent_games_summary"] = recent_summary
    else:
        payload["single_game_stats_gap"] = {
            "available": False,
            "degraded": bool(game_stats_degraded),
        }
    if citations:
        payload["external_citations"] = citations
    if obs_horizon is not None:
        payload["observation_horizon"] = obs_horizon

    raw_markdown = render_research_card(payload, evidence_verified=True, visibility=vis)
    markdown = scrub_raw_drive_ids_for_consumer(raw_markdown, known_drive_ids=known_drive_ids)
    if not markdown.rstrip().endswith("end_marker: true"):
        markdown = markdown.rstrip() + "\n\nend_marker: true\n"
    if vis == "public_safe":
        verify_public_safe_card_text(markdown)

    filename_middle = _resolve_filename_middle_segment(person, current_club=effective_club)
    if vis == "public_safe":
        filename = research_card_public_consumer_filename(
            player_name,
            filename_middle,
            season,
        )
    else:
        filename = research_card_consumer_filename(
            player_name,
            filename_middle,
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

    result: Dict[str, Any] = {
        "status": "BUILT",
        "query": resolution["query"],
        "season": season,
        "visibility": vis,
        "person": person,
        "candidates": resolution["candidates"],
        "posture": posture,
        "unknown_subkind": unknown_subkind,
        "prior_season_baseline": prior_season_baseline,
        "domain_horizons": domain_horizons,
        "recent_games_summary": recent_summary,
        "filename": filename,
        "card_path": card_path_str,
        "card_sha256": card_sha256,
        "markdown": markdown,
        "drive_api_called": False,
        "network_called": False,
    }
    if obs_horizon is not None:
        result["observation_horizon"] = obs_horizon
    return result
