"""Planned, fail-closed watcher for official CBA registration snapshots."""
from __future__ import annotations

from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import random
import time

from .aliases import Clubs
from .adapters import cba_registration
from .ingestion import (
    build_candidate, build_snapshot, canonical_bytes, diff_snapshots,
    publish_decision, schema_drift, validate_candidate, write_evidence,
)


def _utc(value=None):
    value = value or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _previous(path):
    if path is None:
        return None
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError("PREVIOUS_SNAPSHOT_INVALID") from exc
    if not isinstance(value, dict):
        raise RuntimeError("PREVIOUS_SNAPSHOT_OBJECT_REQUIRED")
    return value


def run_source(source, output, *, previous_snapshot=None, fetcher=None,
               started_at=None, ended_at=None, sleeper=time.sleep,
               randomizer=None, clubs=None):
    """Fetch one season discovery root plus every discovered child publication."""
    started = started_at or datetime.now(timezone.utc)
    index_raw = b"{}\n"
    child_raw = {}
    children = []
    requests = 0
    failure = None
    rows = []
    rng = randomizer or random.Random()
    clubs = clubs or Clubs(Path(__file__).resolve().parents[2])
    revision = cba_registration.SOURCE_CONTRACT_REVISION
    try:
        index_raw, attempts = (fetcher or cba_registration.fetch)(source)
        requests += attempts
        children = cba_registration.discover(index_raw, source)
        for child in children:
            sleeper(cba_registration.MIN_DELAY_SECONDS + rng.uniform(0, .5))
            raw, attempts = (fetcher or cba_registration.fetch)(child)
            requests += attempts
            child_raw[child["article_id"]] = raw
        rows = cba_registration.normalize_children(
            child_raw, children, source, clubs=clubs,
        )
        snapshot = build_snapshot(
            source, index_raw, rows, key_fields=cba_registration.ROW_KEY_FIELDS,
            children=child_raw, source_contract_revision=revision,
        )
        diff = diff_snapshots(_previous(previous_snapshot), snapshot)
    except (cba_registration.SourceSchemaDrift, ValueError) as exc:
        failure = str(exc)
        snapshot, diff = schema_drift(
            source, failure, raw=index_raw, children=child_raw,
            source_contract_revision=revision,
        )
    except cba_registration.SourceFetchClosed as exc:
        requests += exc.requests
        failure = f"fetch_fail_closed:{exc}"
        snapshot, diff = schema_drift(
            source, "fetch_fail_closed", raw=index_raw, children=child_raw,
            source_contract_revision=revision,
        )
    candidate = build_candidate(diff)
    validation = validate_candidate(candidate, diff)
    if failure:
        validation.update(result="FAIL_CLOSED", failure_reason=failure)
    publish = publish_decision(candidate, validation)
    ended = ended_at or datetime.now(timezone.utc)
    wall_clock = max(0.0, (ended - started).total_seconds())
    counts = diff["counts"]
    report = {
        "schema_version": 1,
        "run_id": "watcher-" + hashlib.sha256(
            canonical_bytes([source["source_id"], _utc(started)])
        ).hexdigest()[:16],
        "source_id": source["source_id"],
        "started_at": _utc(started),
        "ended_at": _utc(ended),
        "wall_clock_seconds": wall_clock,
        "request_count": requests,
        "provider_tokens": None,
        "snapshot_hash": snapshot["content_sha256"],
        "source_contract_revision": revision,
        "child_count": len(children),
        "canonical_team_count": len({
            row["canonical_team_id"] for row in rows
        }),
        "diff_counts": {name: counts[name] for name in sorted(counts)},
        "candidate_count": candidate["candidate_count"],
        "validation_result": validation["result"],
        "failure_reason": validation["failure_reason"],
        "manual_intervention_count": 0,
        "manual_repair_required": False,
        "release_id": None,
        "candidate_id": "candidate-" + snapshot["content_sha256"][:16],
        "canonical_publish_allowed": False,
    }
    hashes = write_evidence(
        output, raw=index_raw, snapshot=snapshot, diff=diff, candidate=candidate,
        validation=validation, publish=publish, run_report=report,
        raw_files={
            f"children/{article_id}.json": raw
            for article_id, raw in child_raw.items()
        },
    )
    return {"report": report, "artifact_sha256": hashes}


def run_planned(instance, output, *, previous_root=None, sleeper=time.sleep,
                randomizer=None, fetcher=None, clubs=None):
    """Run the three frozen sources sequentially inside private instance data."""
    output = Path(output).resolve()
    try:
        output.relative_to(instance.data_root)
    except ValueError as exc:
        raise RuntimeError("WATCHER_OUTPUT_MUST_BE_PRIVATE_INSTANCE_DATA") from exc
    if output.exists():
        raise ValueError("WATCHER_OUTPUT_MUST_BE_NEW")
    output.mkdir(parents=True)
    rng = randomizer or random.Random()
    reports = []
    sources = list(cba_registration.registry().values())
    for index, source in enumerate(sources):
        if index:
            sleeper(cba_registration.MIN_DELAY_SECONDS + rng.uniform(0, .5))
        previous = None
        if previous_root:
            previous = Path(previous_root) / source["source_id"] / "snapshot.json"
        result = run_source(
            source, output / source["source_id"],
            previous_snapshot=previous, fetcher=fetcher, sleeper=sleeper,
            randomizer=rng, clubs=clubs,
        )
        reports.append(result["report"])
    ledger = {
        "schema_version": 1,
        "cadence": "DAILY@10:00 Asia/Shanghai",
        "planned_source_count": 3,
        "runs": reports,
        "all_fail_closed_or_reviewed": all(
            report["validation_result"] in {"PASS", "REVIEW_REQUIRED", "FAIL_CLOSED"}
            and report["canonical_publish_allowed"] is False
            for report in reports
        ),
    }
    (output / "planned_run_ledger.json").write_bytes(canonical_bytes(ledger))
    return ledger


def _load_replay_snapshot(path: Path) -> dict:
    if path.name.endswith(".json.gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"REPLAY_SNAPSHOT_OBJECT_REQUIRED:{path.name}")
    if "index" not in payload or not isinstance(payload.get("children"), dict):
        raise ValueError(f"REPLAY_SNAPSHOT_SCHEMA_INVALID:{path.name}")
    return payload


def _encode_replay_payload(value) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _parse_observed_at(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolve_replay_source(sid: str) -> dict:
    reg = cba_registration.registry()
    if sid in reg:
        return dict(reg[sid])
    season = sid.split(":", 1)[-1] if ":" in sid else "2025-2026"
    article_id = "replay-index"
    return {
        "source_id": sid,
        "season": season,
        "article_id": article_id,
        "kind": "discovery",
        "source_contract_revision": cba_registration.SOURCE_CONTRACT_REVISION,
        "api_url": f"{cba_registration.API}?id={article_id}",
        "page_url": cba_registration.PAGE.format(article_id=article_id),
        "rights_basis": "official_public_read_only",
    }


def replay_registration_snapshots(
    snapshots_dir,
    output_dir,
    *,
    source_id=None,
    clubs=None,
):
    """Replay offline .json / .json.gz registration snapshots chronologically via run_source.

    Performs zero network calls and strictly preserves canonical_publish_allowed == False.
    """
    snap_root = Path(snapshots_dir)
    if not snap_root.is_dir():
        raise ValueError(f"REPLAY_SNAPSHOTS_DIR_NOT_FOUND:{snap_root}")
    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    candidates = []
    for path in sorted(snap_root.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.name.endswith(".json.gz") or path.name.endswith(".json"):
            payload = _load_replay_snapshot(path)
            observed_at = payload.get("observed_at")
            sort_key = (str(observed_at) if observed_at is not None else "", path.name)
            candidates.append((sort_key, path, payload))

    candidates.sort(key=lambda item: item[0])

    prev_snapshot_by_source = {}
    runs = []
    for idx, (_sort_key, path, payload) in enumerate(candidates, start=1):
        sid = source_id or payload.get("source_id") or "cba-registration:2025-2026"
        source = _resolve_replay_source(sid)
        index_bytes = _encode_replay_payload(payload["index"])
        children_map = payload["children"]

        def _offline_fetcher(target, _idx_bytes=index_bytes, _children=children_map):
            if target.get("kind") == "discovery":
                return _idx_bytes, 1
            art_id = target.get("article_id")
            if art_id not in _children:
                raise cba_registration.SourceSchemaDrift(f"REPLAY_CHILD_MISSING:{art_id}")
            return _encode_replay_payload(_children[art_id]), 1

        stem = path.name[:-8] if path.name.endswith(".json.gz") else path.stem
        run_out = out_root / f"{idx:03d}_{stem}"
        obs_dt = _parse_observed_at(payload.get("observed_at"))
        res = run_source(
            source,
            run_out,
            previous_snapshot=prev_snapshot_by_source.get(sid),
            fetcher=_offline_fetcher,
            started_at=obs_dt,
            ended_at=obs_dt,
            sleeper=lambda _: None,
            clubs=clubs,
        )
        report = res["report"]
        prev_snapshot_by_source[sid] = run_out / "snapshot.json"
        raw_counts = report["diff_counts"]
        snap_rows = json.loads(
            (run_out / "snapshot.json").read_text(encoding="utf-8")
        ).get("rows", [])
        added = raw_counts.get("add", 0)
        modified = raw_counts.get("modify", 0)
        removed = raw_counts.get("delete", 0)
        schema_drift_count = raw_counts.get("schema_drift", 0)
        unchanged = max(0, len(snap_rows) - added - modified)
        replay_validation = report["validation_result"]
        if (
            replay_validation == "FAIL_CLOSED"
            and report.get("failure_reason") == "destructive_or_schema_change"
            and schema_drift_count == 0
        ):
            replay_validation = "REVIEW_REQUIRED"
        runs.append({
            "snapshot_file": path.name,
            "observed_at": payload.get("observed_at"),
            "source_id": sid,
            "run_id": report["run_id"],
            "snapshot_hash": report["snapshot_hash"],
            "diff_counts": {
                "added": added,
                "modified": modified,
                "removed": removed,
                "unchanged": unchanged,
                "schema_drift": schema_drift_count,
            },
            "candidate_count": report["candidate_count"],
            "validation_result": replay_validation,
            "raw_validation_result": report["validation_result"],
            "failure_reason": report["failure_reason"],
            "canonical_publish_allowed": False,
            "output_path": str(run_out),
        })

    summary = {
        "schema_version": 1,
        "replayed_count": len(runs),
        "canonical_publish_allowed": False,
        "runs": runs,
    }
    (out_root / "replay_summary.json").write_bytes(canonical_bytes(summary))
    return summary
