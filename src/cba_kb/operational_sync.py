"""Local-first incremental operational sync & freshness reporting (REQ-220 P0-4).

Strictly Drive A isolated:
- Zero Google Drive API imports or calls (`drive_api_called` is always False).
- Maintains atomic `operational_checkpoint.json` so re-runs with unchanged inputs
  perform 0 re-ingests and 0 card recomputes.
- `--dry-run` computes the report in memory without mutating the checkpoint,
  archive, or any player card files.
- Emits `freshness_report.json` comparing Producer Candidate World against
  Published Consumer World.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .common import atomic
from .consumer_surface import (
    DEFAULT_MAX_EXTERNAL_CITATIONS_PER_CARD,
    enrich_bundle_matched_persons,
)
from .document_lane import DocumentLane
from .document_mentions import compute_dirty_set
from .game_stats import reconcile_game_stat_revisions, validate_player_game_stat_record
from .instance import Instance
from .kc_local_producer import build_incremental_player_cards
from .source_intake import parse_acquisition_bundle_markdown
from .source_watcher import replay_registration_snapshots

_VALID_COVERAGE_STATUSES = frozenset({"COMPLETE", "PARTIAL", "STALE", "UNKNOWN"})


SCHEMA_VERSION = 1
CHECKPOINT_FILENAME = "operational_checkpoint.json"
FRESHNESS_REPORT_FILENAME = "freshness_report.json"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_json_if_exists(path: Optional[str | Path], default: Any) -> Any:
    if path is None:
        return default
    p = Path(path)
    if not p.is_file():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def _discover_bundles(bundles_dir: Path) -> List[Path]:
    if not bundles_dir.is_dir():
        return []
    return sorted(
        (p for p in bundles_dir.rglob("*.md") if p.is_file()),
        key=lambda p: p.relative_to(bundles_dir).as_posix(),
    )


def _discover_registration_snapshots(snapshots_dir: Optional[Path]) -> List[Path]:
    if snapshots_dir is None or not snapshots_dir.is_dir():
        return []
    files = [
        p
        for p in snapshots_dir.iterdir()
        if p.is_file() and (p.name.endswith(".json") or p.name.endswith(".json.gz"))
    ]
    return sorted(files, key=lambda p: p.name)


def _discover_game_log_files(game_logs_dir: Optional[Path]) -> List[Path]:
    if game_logs_dir is None or not game_logs_dir.is_dir():
        return []
    return sorted(
        (
            p
            for p in game_logs_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in (".jsonl", ".json")
        ),
        key=lambda p: p.relative_to(game_logs_dir).as_posix(),
    )


def _parse_json_or_jsonl_bytes(raw_bytes: bytes, *, is_jsonl: bool) -> List[Dict[str, Any]]:
    text = raw_bytes.decode("utf-8").strip()
    if not text:
        return []
    if is_jsonl:
        items: List[Dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError("GAME_LOG_JSONL_OBJECT_REQUIRED")
            items.append(obj)
        return items
    parsed = json.loads(text)
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        if not all(isinstance(x, dict) for x in parsed):
            raise ValueError("GAME_LOG_JSON_OBJECT_REQUIRED")
        return parsed
    raise ValueError("GAME_LOG_JSON_OBJECT_REQUIRED")


def _summarize_coverage_ledger(path: Optional[str | Path]) -> Optional[Dict[str, Any]]:
    if path is None:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
        raise ValueError("COVERAGE_LEDGER_INVALID")
    counts: Dict[str, int] = {st: 0 for st in ("COMPLETE", "PARTIAL", "STALE", "UNKNOWN")}
    non_complete: List[Dict[str, Any]] = []
    for entry in data["sources"]:
        if not isinstance(entry, dict):
            raise ValueError("COVERAGE_LEDGER_SOURCE_INVALID")
        st = str(entry.get("status") or "").strip().upper()
        if st not in _VALID_COVERAGE_STATUSES:
            raise ValueError(f"COVERAGE_LEDGER_INVALID_STATUS:{st}")
        counts[st] += 1
        if st != "COMPLETE":
            non_complete.append({
                "source_id": entry.get("source_id"),
                "status": st,
                "missing": list(entry.get("missing") or []),
            })
    return {
        "schema": data.get("schema"),
        "generated_at_utc": data.get("generated_at_utc"),
        "total_sources": len(data["sources"]),
        "status_counts": counts,
        "non_complete_sources": non_complete,
    }


def _summarize_negative_receipts(
    path: Optional[str | Path],
    *,
    now_utc: datetime,
) -> Optional[Dict[str, Any]]:
    if path is None:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    raw = p.read_bytes()
    records = _parse_json_or_jsonl_bytes(raw, is_jsonl=(p.suffix.lower() == ".jsonl"))
    active_queries: List[str] = []
    expired_count = 0
    for rec in records:
        if rec.get("schema") != "cba-kb.negative-receipt.v1":
            raise ValueError(f"NEGATIVE_RECEIPT_INVALID_SCHEMA:{rec.get('schema')!r}")
        q = rec.get("query_person")
        checked_raw = rec.get("checked_at_utc")
        ttl_hours = rec.get("ttl_hours")
        if (
            not isinstance(q, str)
            or not q.strip()
            or not isinstance(checked_raw, str)
            or not checked_raw.strip()
            or isinstance(ttl_hours, bool)
            or not isinstance(ttl_hours, (int, float))
            or ttl_hours < 0
        ):
            raise ValueError("NEGATIVE_RECEIPT_INVALID_RECORD")
        dt = datetime.fromisoformat(checked_raw.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt + timedelta(hours=float(ttl_hours)) >= now_utc:
            active_queries.append(q.strip())
        else:
            expired_count += 1
    return {
        "total_count": len(records),
        "active_count": len(active_queries),
        "expired_count": expired_count,
        "active_queries": sorted(set(active_queries)),
    }


def _extract_published_world(
    published_status_path: Optional[str | Path],
    consumer_manifest_path: Optional[str | Path],
) -> tuple[Dict[str, str], set[str]]:
    """Extract published card SHA-256 map and published doc_id set."""
    published_cards: Dict[str, str] = {}
    published_doc_ids: set[str] = set()

    pub_status = _load_json_if_exists(published_status_path, {})
    if isinstance(pub_status, dict):
        raw_cards = pub_status.get("published_cards") or {}
        if isinstance(raw_cards, dict):
            for uid, val in raw_cards.items():
                if isinstance(val, str):
                    published_cards[str(uid)] = val
                elif isinstance(val, dict):
                    sha = val.get("consumer_card_sha256") or val.get("sha256") or ""
                    if isinstance(sha, str):
                        published_cards[str(uid)] = sha
        for doc_id in pub_status.get("published_doc_ids") or []:
            if isinstance(doc_id, str) and doc_id:
                published_doc_ids.add(doc_id)

    manifest = _load_json_if_exists(consumer_manifest_path, {})
    if isinstance(manifest, dict):
        for doc_id in manifest.get("published_doc_ids") or []:
            if isinstance(doc_id, str) and doc_id:
                published_doc_ids.add(doc_id)
        raw_manifest_cards = manifest.get("published_cards") or {}
        if isinstance(raw_manifest_cards, dict):
            for uid, val in raw_manifest_cards.items():
                if isinstance(val, str) and uid not in published_cards:
                    published_cards[str(uid)] = val
                elif isinstance(val, dict) and uid not in published_cards:
                    sha = val.get("consumer_card_sha256") or val.get("sha256") or ""
                    if isinstance(sha, str):
                        published_cards[str(uid)] = sha

    return published_cards, published_doc_ids


def _build_external_citations_by_player(
    consumed_bundles: Dict[str, Dict[str, Any]],
    approved_mentions: Sequence[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    docs_by_id: Dict[str, Dict[str, Any]] = {}
    for entry in consumed_bundles.values():
        doc_id = entry.get("doc_id")
        if isinstance(doc_id, str) and doc_id:
            docs_by_id[doc_id] = entry

    by_player: Dict[str, List[Dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()
    for mention in approved_mentions or ():
        if not isinstance(mention, dict):
            continue
        if mention.get("mention_status") != "same":
            continue
        if mention.get("mention_method") not in {"manual", "external_id"}:
            continue
        doc_id = mention.get("doc_id")
        uid = mention.get("player_uid")
        if not isinstance(doc_id, str) or not isinstance(uid, str):
            continue
        doc = docs_by_id.get(doc_id)
        if not doc or (uid, doc_id) in seen:
            continue
        seen.add((uid, doc_id))
        by_player.setdefault(uid, []).append({
            "doc_id": doc_id,
            "source_tier": doc.get("source_tier"),
            "completeness": doc.get("completeness"),
            "published_at": doc.get("published_at"),
            "title": doc.get("title") or doc_id,
            "canonical_url": doc.get("canonical_url"),
            "disclosure_posture": doc.get("disclosure_posture"),
        })
    return by_player


def run_operational_sync(
    *,
    engine_root: str | Path,
    instance: Instance,
    bundles_dir: str | Path,
    output_dir: str | Path,
    registration_snapshots_dir: Optional[str | Path] = None,
    published_status_path: Optional[str | Path] = None,
    consumer_manifest_path: Optional[str | Path] = None,
    player_fixtures_path: Optional[str | Path] = None,
    identity_registry_path: Optional[str | Path] = None,
    approved_mentions_path: Optional[str | Path] = None,
    max_external_citations_per_card: int = DEFAULT_MAX_EXTERNAL_CITATIONS_PER_CARD,
    dry_run: bool = False,
    game_logs_dir: Optional[str | Path] = None,
    coverage_ledger_path: Optional[str | Path] = None,
    negative_receipts_path: Optional[str | Path] = None,
    roster_candidates: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run local-first incremental operational sync and emit freshness_report.json.

    Strictly Drive A isolated: zero Google Drive API calls (`drive_api_called: False`).
    """
    engine_path = Path(engine_root).resolve()
    bundles_path = Path(bundles_dir)
    out_path = Path(output_dir)
    reg_snaps_path = Path(registration_snapshots_dir) if registration_snapshots_dir is not None else None
    game_logs_path = Path(game_logs_dir) if game_logs_dir is not None else None

    identity_registry = _load_json_if_exists(
        identity_registry_path,
        {"schema_version": 1, "players": [], "aliases": []},
    )
    effective_roster_candidates: List[Dict[str, Any]] = []
    if roster_candidates is not None:
        effective_roster_candidates = [dict(c) for c in roster_candidates if isinstance(c, dict)]
    elif isinstance(identity_registry, dict) and isinstance(identity_registry.get("players"), list):
        effective_roster_candidates = [
            dict(p) for p in identity_registry["players"] if isinstance(p, dict)
        ]

    checkpoint_path = out_path / CHECKPOINT_FILENAME
    prev_checkpoint = _load_json_if_exists(checkpoint_path, {})
    if not isinstance(prev_checkpoint, dict):
        prev_checkpoint = {}

    consumed_bundles: Dict[str, Dict[str, Any]] = dict(prev_checkpoint.get("consumed_bundles") or {})
    prev_doc_hashes: Dict[str, str] = dict(prev_checkpoint.get("doc_hashes") or {})
    doc_hashes: Dict[str, str] = dict(prev_doc_hashes)
    consumed_reg_snaps: Dict[str, str] = dict(
        prev_checkpoint.get("consumed_registration_snapshots") or {}
    )
    prev_reg_diffs: List[Dict[str, Any]] = list(
        prev_checkpoint.get("registration_candidate_diffs") or []
    )
    player_cards: Dict[str, Dict[str, Any]] = dict(prev_checkpoint.get("player_cards") or {})
    consumed_game_logs: Dict[str, Dict[str, Any]] = dict(
        prev_checkpoint.get("consumed_game_logs") or {}
    )

    # 1. Incremental bundle intake
    bundle_files = _discover_bundles(bundles_path)
    lane = None if dry_run else DocumentLane(engine_path, instance)
    ingested_count = 0
    skipped_bundles_count = 0
    new_docs_for_dirty_set: List[Dict[str, Any]] = []
    revision_summaries: List[Dict[str, Any]] = []

    for bundle_file in bundle_files:
        rel_key = bundle_file.relative_to(bundles_path).as_posix()
        raw_bytes = bundle_file.read_bytes()
        file_sha = _sha256_bytes(raw_bytes)
        prev_entry = consumed_bundles.get(rel_key)
        if isinstance(prev_entry, dict) and prev_entry.get("file_sha256") == file_sha:
            skipped_bundles_count += 1
            continue

        parsed_meta, parsed_body = parse_acquisition_bundle_markdown(raw_bytes)
        enriched_bundle = enrich_bundle_matched_persons(
            {
                "title": parsed_meta.get("title") or "",
                "body": parsed_body,
                "matched_persons": list(parsed_meta.get("matched_persons") or []),
            },
            roster_candidates=effective_roster_candidates,
        )
        matched_persons = list(enriched_bundle.get("matched_persons") or [])
        if dry_run:
            # Dry-run must not mutate archive or checkpoint; simulate doc_id in memory.
            sim_doc_id = "doc_" + _sha256_bytes(parsed_body.encode("utf-8"))[:24]
            sim_hash = _sha256_bytes(parsed_body.encode("utf-8"))
            new_docs_for_dirty_set.append({
                "doc_id": sim_doc_id,
                "content_hash": sim_hash,
                "text": parsed_body,
                "matched_persons": matched_persons,
            })
            doc_hashes[sim_doc_id] = sim_hash
            consumed_bundles[rel_key] = {
                "file_sha256": file_sha,
                "doc_id": sim_doc_id,
                "content_hash": sim_hash,
                "canonical_url": parsed_meta.get("canonical_url"),
                "source_tier": parsed_meta.get("source_tier"),
                "completeness": parsed_meta.get("completeness"),
                "published_at": parsed_meta.get("published_at"),
                "title": parsed_meta.get("title"),
                "matched_persons": matched_persons,
            }
            continue

        assert lane is not None
        res = lane.ingest_acquisition_bundle(bundle_file)
        ingested_count += 1
        doc_id = res["doc_id"]
        content_hash = res["content_hash"]
        doc_hashes[doc_id] = content_hash
        rev_summary = res.get("revision_summary")
        if isinstance(rev_summary, dict):
            revision_summaries.append(rev_summary)

        consumed_bundles[rel_key] = {
            "file_sha256": file_sha,
            "doc_id": doc_id,
            "content_hash": content_hash,
            "canonical_url": parsed_meta.get("canonical_url"),
            "source_tier": parsed_meta.get("source_tier"),
            "completeness": parsed_meta.get("completeness"),
            "published_at": parsed_meta.get("published_at"),
            "title": parsed_meta.get("title"),
            "matched_persons": matched_persons,
            "disclosure_posture": (
                rev_summary.get("disclosure_posture")
                if isinstance(rev_summary, dict)
                else None
            ),
        }
        new_docs_for_dirty_set.append({
            "doc_id": doc_id,
            "content_hash": content_hash,
            "text": parsed_body,
            "matched_persons": matched_persons,
        })

    # 2. Offline registration snapshot replay (optional)
    registration_candidate_diffs: List[Dict[str, Any]] = list(prev_reg_diffs)
    snap_files = _discover_registration_snapshots(reg_snaps_path)
    if snap_files:
        current_snap_hashes = {
            p.name: _sha256_bytes(p.read_bytes()) for p in snap_files
        }
        if current_snap_hashes != consumed_reg_snaps:
            if dry_run:
                registration_candidate_diffs = [
                    {
                        "snapshot_file": name,
                        "status": "PENDING_REPLAY_DRY_RUN",
                        "canonical_publish_allowed": False,
                    }
                    for name in sorted(current_snap_hashes)
                ]
            else:
                replay_out = out_path / "registration_replay"
                replay_res = replay_registration_snapshots(reg_snaps_path, replay_out)
                registration_candidate_diffs = [
                    {
                        "snapshot_file": run.get("snapshot_file"),
                        "observed_at": run.get("observed_at"),
                        "source_id": run.get("source_id"),
                        "diff_counts": run.get("diff_counts", {}),
                        "candidate_count": run.get("candidate_count", 0),
                        "validation_result": run.get("validation_result"),
                        "canonical_publish_allowed": False,
                    }
                    for run in replay_res.get("runs", [])
                ]
                consumed_reg_snaps = current_snap_hashes

    # 2b. Incremental single-game stats sidecar intake (REQ-223 P0-5, optional)
    game_logs_ingested_count = 0
    game_logs_skipped_count = 0
    dirty_player_uids_from_games: set[str] = set()
    game_log_files = _discover_game_log_files(game_logs_path)
    for gl_file in game_log_files:
        assert game_logs_path is not None
        rel_key = gl_file.relative_to(game_logs_path).as_posix()
        raw_bytes = gl_file.read_bytes()
        file_sha = _sha256_bytes(raw_bytes)
        prev_gl = consumed_game_logs.get(rel_key)
        if isinstance(prev_gl, dict) and prev_gl.get("file_sha256") == file_sha:
            game_logs_skipped_count += 1
            continue
        records = _parse_json_or_jsonl_bytes(
            raw_bytes, is_jsonl=(gl_file.suffix.lower() == ".jsonl")
        )
        for rec in records:
            validate_player_game_stat_record(rec)
        reconciled = reconcile_game_stat_revisions(records)
        if reconciled["revision_summaries"]:
            revision_summaries.extend(reconciled["revision_summaries"])
        file_uids = sorted({
            str(r["player_uid"]).strip()
            for r in records
            if isinstance(r.get("player_uid"), str) and r["player_uid"].strip()
        })
        dirty_player_uids_from_games.update(file_uids)
        game_logs_ingested_count += 1
        consumed_game_logs[rel_key] = {
            "file_sha256": file_sha,
            "record_count": len(records),
            "player_uids": file_uids,
            "revision_count": len(reconciled["revision_summaries"]),
        }

    # 3. Dirty-set computation
    approved_mentions = _load_json_if_exists(approved_mentions_path, [])
    if not isinstance(approved_mentions, list):
        approved_mentions = []

    dirty_result = compute_dirty_set(
        new_docs_for_dirty_set,
        identity_registry,
        approved_mentions=approved_mentions,
        existing_doc_hashes=prev_doc_hashes,
    )
    dirty_player_uids: List[str] = sorted(
        set(dirty_result["dirty_player_uids"]) | dirty_player_uids_from_games
    )
    undecided_mentions: List[Dict[str, Any]] = list(dirty_result["verification_queue"])

    # 4. Incremental player card recompute
    player_fixtures = _load_json_if_exists(player_fixtures_path, {})
    recomputed_player_uids: List[str] = []
    if isinstance(player_fixtures, dict) and player_fixtures and dirty_player_uids and not dry_run:
        citations_by_player = _build_external_citations_by_player(
            consumed_bundles, approved_mentions
        )
        inc_res = build_incremental_player_cards(
            player_fixtures=player_fixtures,
            dirty_player_uids=dirty_player_uids,
            output_dir=out_path / "cards",
            external_citations_by_player=citations_by_player,
            max_external_citations_per_card=max_external_citations_per_card,
        )
        recomputed_player_uids = list(inc_res["recomputed_player_uids"])
        for uid, card_meta in inc_res["cards"].items():
            player_cards[uid] = dict(card_meta)

    # 5. Freshness report: Producer Candidate World vs Published Consumer World
    published_cards, published_doc_ids = _extract_published_world(
        published_status_path, consumer_manifest_path
    )

    pending_player_cards: List[Dict[str, Any]] = []
    for uid in sorted(player_cards):
        cand_sha = player_cards[uid].get("consumer_card_sha256")
        pub_sha = published_cards.get(uid)
        if pub_sha is None:
            pending_player_cards.append({
                "player_uid": uid,
                "candidate_sha256": cand_sha,
                "published_sha256": None,
                "status": "PENDING_PUBLISH",
            })
        elif cand_sha != pub_sha:
            pending_player_cards.append({
                "player_uid": uid,
                "candidate_sha256": cand_sha,
                "published_sha256": pub_sha,
                "status": "STALE",
            })

    # Collect lagged docs from consumed bundles not yet in published_doc_ids
    docs_meta_by_id: Dict[str, Dict[str, Any]] = {}
    for entry in consumed_bundles.values():
        did = entry.get("doc_id")
        if isinstance(did, str) and did:
            docs_meta_by_id[did] = entry

    lagged_docs: List[Dict[str, Any]] = []
    for did in sorted(doc_hashes):
        if did not in published_doc_ids:
            meta = docs_meta_by_id.get(did, {})
            lagged_docs.append({
                "doc_id": did,
                "canonical_url": meta.get("canonical_url"),
                "content_hash": doc_hashes[did],
                "status": "NOT_PUBLISHED",
            })

    # Read revision_index.json summary if present in archive
    rev_index_path = instance.document_archive_root / "revision_index.json"
    revision_index_summary: Dict[str, Any] = {}
    if rev_index_path.is_file():
        try:
            rev_index_data = json.loads(rev_index_path.read_text(encoding="utf-8"))
            if isinstance(rev_index_data, dict):
                revision_index_summary = {
                    "tracked_urls": len(rev_index_data.get("by_canonical_url", rev_index_data)),
                }
        except (OSError, ValueError):
            revision_index_summary = {}

    now_utc = datetime.now(timezone.utc)
    coverage_ledger_summary = _summarize_coverage_ledger(coverage_ledger_path)
    negative_receipts_summary = _summarize_negative_receipts(
        negative_receipts_path, now_utc=now_utc
    )

    freshness_report: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "dry_run": bool(dry_run),
        "drive_api_called": False,
        "ingested_count": ingested_count,
        "skipped_bundles_count": skipped_bundles_count,
        "dirty_player_uids": dirty_player_uids,
        "recomputed_player_uids": recomputed_player_uids,
        "pending_player_cards": pending_player_cards,
        "lagged_docs": lagged_docs,
        "undecided_mentions": undecided_mentions,
        "registration_candidate_diffs": registration_candidate_diffs,
        "revision_summaries": revision_summaries,
        "game_logs_ingested_count": game_logs_ingested_count,
        "game_logs_skipped_count": game_logs_skipped_count,
    }
    if coverage_ledger_summary is not None:
        freshness_report["coverage_ledger_summary"] = coverage_ledger_summary
    if negative_receipts_summary is not None:
        freshness_report["negative_receipts_summary"] = negative_receipts_summary

    if not dry_run:
        out_path.mkdir(parents=True, exist_ok=True)
        checkpoint_payload = {
            "schema_version": SCHEMA_VERSION,
            "consumed_bundles": consumed_bundles,
            "doc_hashes": doc_hashes,
            "consumed_registration_snapshots": consumed_reg_snaps,
            "registration_candidate_diffs": registration_candidate_diffs,
            "player_cards": player_cards,
            "revision_index_summary": revision_index_summary,
            "updated_at": now_utc.isoformat(),
        }
        if consumed_game_logs:
            checkpoint_payload["consumed_game_logs"] = consumed_game_logs
        atomic(
            checkpoint_path,
            (json.dumps(checkpoint_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        )
        atomic(
            out_path / FRESHNESS_REPORT_FILENAME,
            (json.dumps(freshness_report, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        )

    return freshness_report
