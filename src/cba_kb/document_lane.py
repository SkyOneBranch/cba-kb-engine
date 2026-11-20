"""Deterministic private archive for documentary evidence.

Document Lane observes and retains source material only. It cannot write
canonical facts or enter the release orchestration path.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
import re
import shutil
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

from .common import atomic, canonical_json_bytes
from .document_sources import (
    PARSER_VERSION,
    ReviewRequired,
    parse_drive_document,
    parse_ima,
    parse_local,
    parse_markdown,
)


SCHEMA_VERSION = 1
CAPTURE_CHANNELS = (
    "wechat_browser_clip",
    "ima_file_export",
    "local_file",
    "google_drive_doc",
    "acquisition_bundle_v1",
)
RIGHTS_CLASSIFICATIONS = ("public", "copyrighted", "private", "unknown")
DEFAULT_RIGHTS = {
    "wechat_browser_clip": "copyrighted",
    "ima_file_export": "unknown",
    "local_file": "private",
    "google_drive_doc": "private",
    "acquisition_bundle_v1": "copyrighted",
}


def canonical_bytes(value):
    return canonical_json_bytes(value, trailing_newline=True)


def normalize_text(text):
    """Return deterministic UTF-8-ready text without semantic rewriting."""
    if not isinstance(text, str):
        raise TypeError("DOCUMENT_TEXT_REQUIRED")
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[^\S\n]+", " ", line).strip()
        lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + ("\n" if lines else "")


def content_hash(text):
    normalized = normalize_text(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def doc_id(text):
    return "doc_" + content_hash(text)[:24]


def normalize_canonical_url(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ReviewRequired("CANONICAL_URL_REQUIRED")
    parts = urlsplit(value.strip())
    if not parts.scheme or not parts.netloc:
        raise ReviewRequired("CANONICAL_URL_ABSOLUTE_REQUIRED")
    hostname = parts.hostname
    if not hostname:
        raise ReviewRequired("CANONICAL_URL_HOST_REQUIRED")
    netloc = hostname.lower()
    try:
        port = parts.port
    except ValueError as exc:
        raise ReviewRequired("CANONICAL_URL_PORT_INVALID") from exc
    if port is not None:
        netloc += f":{port}"
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    ]
    query.sort()
    return urlunsplit((
        parts.scheme.lower(),
        netloc,
        parts.path,
        urlencode(query, doseq=True),
        "",
    ))


def _timestamp(value, *, label, required):
    if value in (None, ""):
        if required:
            raise ReviewRequired(f"{label.upper()}_REQUIRED")
        return None
    if isinstance(value, datetime):
        value = value.isoformat()
    elif isinstance(value, date):
        value = value.isoformat()
    if not isinstance(value, str):
        raise ReviewRequired(f"{label.upper()}_ISO8601_REQUIRED")
    text = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            date.fromisoformat(text)
            return text
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReviewRequired(f"{label.upper()}_ISO8601_REQUIRED") from exc
    if parsed.tzinfo is None:
        return parsed.replace(microsecond=0).isoformat()
    parsed = parsed.astimezone(timezone.utc).replace(microsecond=0)
    return parsed.isoformat().replace("+00:00", "Z")


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return str(value)


def load_taxonomy(engine_root):
    path = Path(engine_root) / "config/taxonomy.yaml"
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError("DOCUMENT_TAXONOMY_INVALID") from exc
    if not isinstance(value, dict) or not isinstance(value.get("topic"), list):
        raise RuntimeError("DOCUMENT_TAXONOMY_TOPIC_REQUIRED")
    return value


def validate_tags(engine_root, values):
    allowed = set(load_taxonomy(engine_root)["topic"])
    tags = []
    candidates = []
    for value in values or []:
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        if value in allowed:
            tags.append(value)
        else:
            candidates.append(value)
    return sorted(set(tags)), sorted(set(candidates))


def rights_decision(parsed, *, classification=None, public_export_allowed=None,
                    evidence=None):
    source = parsed.get("rights")
    if isinstance(source, str):
        source = {"classification": source}
    if not isinstance(source, dict):
        source = {}
    selected = (
        classification
        or source.get("classification")
        or DEFAULT_RIGHTS.get(parsed.get("capture_channel"), "unknown")
    )
    if selected not in RIGHTS_CLASSIFICATIONS:
        raise ReviewRequired("RIGHTS_CLASSIFICATION_INVALID")
    selected_evidence = evidence if evidence is not None else source.get("evidence") or []
    if isinstance(selected_evidence, str):
        selected_evidence = [selected_evidence]
    elif isinstance(selected_evidence, dict):
        selected_evidence = [selected_evidence]
    elif not isinstance(selected_evidence, (list, tuple)):
        selected_evidence = [selected_evidence]
    selected_evidence = [_jsonable(item) for item in selected_evidence]
    requested = (
        bool(public_export_allowed)
        if public_export_allowed is not None
        else bool(source.get("public_export_allowed", False))
    )
    if requested and (selected != "public" or not selected_evidence):
        raise ReviewRequired("PUBLIC_EXPORT_EVIDENCE_REQUIRED")
    return {
        "classification": selected,
        "public_export_allowed": requested,
        "evidence": selected_evidence,
    }


def assert_public_export_allowed(record):
    rights = record.get("rights") or {}
    allowed = (
        rights.get("classification") == "public"
        and rights.get("public_export_allowed") is True
        and bool(rights.get("evidence"))
    )
    if not allowed:
        raise RuntimeError("DOCUMENT_PUBLIC_EXPORT_BLOCKED")
    return True


def _shingles(text, width=5):
    points = list(normalize_text(text))
    if not points:
        return set()
    if len(points) <= width:
        return {"".join(points)}
    return {"".join(points[index:index + width])
            for index in range(len(points) - width + 1)}


def _compare_shingles(left_text, left_shingles, right_text, right_shingles):
    if not left_text or not right_text:
        return {"jaccard": 0.0, "length_ratio": 0.0, "candidate": False}
    union = left_shingles | right_shingles
    score = len(left_shingles & right_shingles) / len(union) if union else 0.0
    ratio = min(len(left_text), len(right_text)) / max(len(left_text), len(right_text))
    return {
        "jaccard": round(score, 12),
        "length_ratio": round(ratio, 12),
        "candidate": score >= 0.92 and ratio >= 0.90,
    }


def near_duplicate(left, right):
    left_text, right_text = normalize_text(left), normalize_text(right)
    if not left_text or not right_text:
        return {"jaccard": 0.0, "length_ratio": 0.0, "candidate": False}
    left_shingles, right_shingles = _shingles(left_text), _shingles(right_text)
    return _compare_shingles(left_text, left_shingles, right_text, right_shingles)


def _capture_id(parsed, capture_channel):
    payload = {
        "raw_sha256": hashlib.sha256(parsed["raw"]).hexdigest(),
        "source_locator": parsed.get("source_locator"),
        "capture_channel": capture_channel,
    }
    return "capture_" + hashlib.sha256(canonical_bytes(payload)).hexdigest()[:20]


def _raw_suffix(parsed):
    return {
        "ima_file": ".md",
        "txt": ".txt",
        "docx": ".docx",
        "pdf": ".pdf",
        "google_docs": ".json",
        "acquisition_bundle_v1": ".md",
    }.get(parsed.get("parser_name"), ".bin")


def _safe_name(value):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(value).name).strip("._")
    return name or "attachment"


class DocumentLane:
    def __init__(self, engine_root, instance):
        self.engine_root = Path(engine_root).resolve()
        self.instance = instance
        self.input_root = Path(instance.document_input_root).resolve()
        self.archive_root = Path(instance.document_archive_root).resolve()
        self.review_root = Path(instance.document_review_root).resolve()
        self.report_root = Path(instance.document_report_root).resolve()
        self._records_cache = {}
        self._text_cache = {}
        self._shingles_cache = {}

    def _parse_acquisition_bundle_raw(self, raw, *, source_locator):
        from .source_intake import parse_acquisition_bundle_markdown

        meta, body = parse_acquisition_bundle_markdown(raw)
        return {
            "capture_channel": "acquisition_bundle_v1",
            "source_type": "acquisition_bundle_v1",
            "parser_name": "acquisition_bundle_v1",
            "parser_version": meta.get("extractor", PARSER_VERSION),
            "source_locator": source_locator or meta["canonical_url"],
            "raw": raw,
            "text": body,
            "title": meta.get("title"),
            "canonical_url": meta["canonical_url"],
            "published_at": meta["published_at"],
            "captured_at": meta["acquired_at"],
            "rights": meta["rights"],
            "front_matter": meta,
            "acquisition_meta": meta,
        }

    def ingest_acquisition_bundle(self, bundle_path, **options):
        source = Path(bundle_path)
        if not source.is_absolute():
            candidate = self.input_root / source
            source = candidate.resolve() if candidate.is_file() else source.resolve()
        else:
            source = source.resolve()
        if not source.is_file():
            raise RuntimeError("DOCUMENT_SOURCE_MISSING")
        raw = source.read_bytes()
        locator = (
            source.relative_to(self.input_root).as_posix()
            if source.is_relative_to(self.input_root)
            else None
        )
        parsed = self._parse_acquisition_bundle_raw(raw, source_locator=locator)
        call_options = dict(options)
        call_options.setdefault("captured_at", parsed["captured_at"])
        try:
            return self._ingest_parsed(
                parsed, capture_channel="acquisition_bundle_v1", **call_options
            )
        except ReviewRequired as exc:
            if not exc.raw:
                exc.raw = parsed.get("raw", b"")
            return self._review(
                exc,
                source_locator=parsed["source_locator"],
                capture_channel="acquisition_bundle_v1",
            )

    def ingest_file(self, source, *, capture_channel, **options):
        source = Path(source)
        if not source.is_absolute():
            source = self.input_root / source
        source = source.resolve()
        if not source.is_relative_to(self.input_root):
            raise RuntimeError("DOCUMENT_SOURCE_MUST_BE_PRIVATE_INPUT")
        if not source.is_file():
            raise RuntimeError("DOCUMENT_SOURCE_MISSING")
        raw = source.read_bytes()
        locator = source.relative_to(self.input_root).as_posix()
        try:
            if capture_channel == "wechat_browser_clip":
                parsed = parse_markdown(
                    raw, channel=capture_channel, source_locator=locator,
                    base_dir=source.parent,
                )
            elif capture_channel == "ima_file_export":
                parsed = parse_ima(
                    raw, source_locator=locator, base_dir=source.parent,
                )
            elif capture_channel == "local_file":
                parsed = parse_local(
                    raw, suffix=source.suffix, source_locator=locator,
                    channel=capture_channel,
                )
            elif capture_channel == "acquisition_bundle_v1":
                from .source_intake import SourceIntakeError
                try:
                    parsed = self._parse_acquisition_bundle_raw(
                        raw, source_locator=locator
                    )
                except SourceIntakeError as exc:
                    raise ReviewRequired(str(exc), raw=raw) from exc
            else:
                raise ReviewRequired("DOCUMENT_FILE_CHANNEL_INVALID", raw=raw)
        except ReviewRequired as exc:
            return self._review(exc, source_locator=locator,
                                capture_channel=capture_channel)
        call_options = dict(options)
        if capture_channel == "acquisition_bundle_v1" and parsed.get("captured_at"):
            call_options.setdefault("captured_at", parsed["captured_at"])
        try:
            return self._ingest_parsed(parsed, **call_options)
        except ReviewRequired as exc:
            if not exc.raw:
                exc.raw = parsed.get("raw", b"")
            return self._review(exc, source_locator=locator,
                                capture_channel=capture_channel)

    def ingest_drive_document(self, drive, file_id, **options):
        try:
            parsed = parse_drive_document(drive, file_id)
        except ReviewRequired as exc:
            return self._review(
                exc, source_locator=f"google-drive:{file_id}",
                capture_channel="google_drive_doc",
            )
        try:
            return self._ingest_parsed(parsed, **options)
        except ReviewRequired as exc:
            if not exc.raw:
                exc.raw = parsed.get("raw", b"")
            return self._review(
                exc, source_locator=f"google-drive:{file_id}",
                capture_channel="google_drive_doc",
            )

    def ingest_batch(self, manifest, *, drive=None, drive_factory=None, **options):
        path = Path(manifest)
        if not path.is_absolute():
            path = self.input_root / path
        path = path.resolve()
        if not path.is_relative_to(self.input_root):
            raise RuntimeError("DOCUMENT_MANIFEST_MUST_BE_PRIVATE_INPUT")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("DOCUMENT_MANIFEST_INVALID") from exc
        entries = value.get("documents") if isinstance(value, dict) else value
        if not isinstance(entries, list):
            raise RuntimeError("DOCUMENT_MANIFEST_ITEMS_REQUIRED")
        results = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise RuntimeError("DOCUMENT_MANIFEST_ITEM_OBJECT_REQUIRED")
            item_options = {
                key: value
                for key, value in entry.items()
                if key not in {"path", "drive_id", "capture_channel"}
            }
            item_options.update(options)
            if entry.get("drive_id"):
                if drive is None and drive_factory is not None:
                    drive = drive_factory()
                if drive is None:
                    raise RuntimeError("DOCUMENT_DRIVE_REQUIRED")
                result = self.ingest_drive_document(
                    drive, entry["drive_id"], **item_options,
                )
            else:
                result = self.ingest_file(
                    entry.get("path"),
                    capture_channel=entry.get("capture_channel", "local_file"),
                    **item_options,
                )
            results.append(result)
        report = {
            "schema_version": SCHEMA_VERSION,
            "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "documents": len(results),
            "accepted": sum(item["accepted"] for item in results),
            "review_required": sum(not item["accepted"] for item in results),
            "results": results,
        }
        report_path = (
            self.report_root
            / f"document-report-{report['manifest_sha256'][:24]}.json"
        )
        atomic(report_path, canonical_bytes(report))
        return report

    def _review(self, error, *, source_locator, capture_channel):
        reason = getattr(error, "reason", str(error))
        raw = getattr(error, "raw", b"") or b""
        review_id = "review_" + hashlib.sha256(
            canonical_bytes({
                "reason": reason,
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "source_locator": source_locator,
            })
        ).hexdigest()[:24]
        root = self.review_root / review_id
        record = {
            "schema_version": SCHEMA_VERSION,
            "review_id": review_id,
            "reason": reason,
            "capture_channel": capture_channel,
            "source_locator": source_locator,
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "metadata": _jsonable(getattr(error, "metadata", {})),
            "canonical_write_performed": False,
        }
        if raw:
            atomic(root / "raw/original.bin", raw)
        atomic(root / "review.json", canonical_bytes(record))
        return {
            "status": "REVIEW_REQUIRED",
            "accepted": False,
            "reason": reason,
            "review_id": review_id,
            "review_path": str(root),
        }

    def _existing_records(self):
        if not self.archive_root.is_dir():
            self._records_cache.clear()
            self._text_cache.clear()
            self._shingles_cache.clear()
            return []
        disk_ids = set()
        for entry in sorted(self.archive_root.iterdir(), key=lambda p: p.name):
            if not entry.is_dir() or not entry.name.startswith("doc_"):
                continue
            did = entry.name
            disk_ids.add(did)
            if did not in self._records_cache:
                path = entry / "record.json"
                try:
                    self._records_cache[did] = json.loads(
                        path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError) as exc:
                    raise RuntimeError("DOCUMENT_ARCHIVE_RECORD_INVALID") from exc
        stale_ids = set(self._records_cache) - disk_ids
        for did in stale_ids:
            self._records_cache.pop(did, None)
            self._text_cache.pop(did, None)
            self._shingles_cache.pop(did, None)
        return [self._records_cache[did] for did in sorted(self._records_cache)]

    def _get_cached_text_and_shingles(self, doc_id_str):
        if doc_id_str not in self._text_cache:
            path = self.archive_root / doc_id_str / "text.txt"
            try:
                candidate_text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise RuntimeError("DOCUMENT_ARCHIVE_TEXT_INVALID") from exc
            self._text_cache[doc_id_str] = normalize_text(candidate_text)
        if doc_id_str not in self._shingles_cache:
            self._shingles_cache[doc_id_str] = _shingles(self._text_cache[doc_id_str])
        return self._text_cache[doc_id_str], self._shingles_cache[doc_id_str]

    def _write_revision_index(self):
        records = self._existing_records()
        by_url = {}
        by_item_id = {}
        for rec in records:
            entry = {
                "doc_id": rec["doc_id"],
                "revision_seq": int(rec.get("revision_seq") or 1),
                "content_hash": rec.get("content_hash"),
                "supersedes_doc_id": rec.get("supersedes_doc_id"),
                "superseded_by_doc_id": rec.get("superseded_by_doc_id"),
                "captured_at": rec.get("captured_at"),
            }
            url = rec.get("canonical_url")
            if url:
                by_url.setdefault(url, []).append(entry)
            iid = rec.get("item_id")
            if iid:
                by_item_id.setdefault(iid, []).append(entry)

        def _sort_chain(items):
            return sorted(
                items,
                key=lambda x: (
                    x["revision_seq"],
                    str(x.get("captured_at") or ""),
                    x["doc_id"],
                ),
            )

        index_payload = {
            "schema_version": SCHEMA_VERSION,
            "by_canonical_url": {
                url: _sort_chain(items) for url, items in sorted(by_url.items())
            },
            "by_item_id": {
                iid: _sort_chain(items) for iid, items in sorted(by_item_id.items())
            },
        }
        atomic(
            self.archive_root / "revision_index.json",
            canonical_bytes(index_payload),
        )

    @staticmethod
    def _revision_index_has_edge(
        rev_index, *, prior_doc_id, new_doc_id, canonical_url, item_id
    ):
        def _chain_has_edge(entries):
            if not isinstance(entries, list):
                return False
            has_prior = any(
                isinstance(e, dict)
                and e.get("doc_id") == prior_doc_id
                and e.get("superseded_by_doc_id") == new_doc_id
                for e in entries
            )
            has_new = any(
                isinstance(e, dict)
                and e.get("doc_id") == new_doc_id
                and e.get("supersedes_doc_id") == prior_doc_id
                for e in entries
            )
            return has_prior and has_new

        checked = False
        if canonical_url:
            checked = True
            by_url = rev_index.get("by_canonical_url")
            if not isinstance(by_url, dict) or not _chain_has_edge(by_url.get(canonical_url)):
                return False
        if item_id:
            checked = True
            by_item = rev_index.get("by_item_id")
            if not isinstance(by_item, dict) or not _chain_has_edge(by_item.get(item_id)):
                return False
        return checked

    def _validate_prior_archive_integrity(
        self,
        prior_doc_id: str,
    ) -> Tuple[Path, Dict[str, Any], Optional[Dict[str, Any]]]:
        """Read-only integrity preflight for a superseded prior archive.

        Verifies record.json, text.txt (UTF-8 & sha256 == content_hash), and
        optional dedup.json with zero filesystem writes. Raises ReviewRequired
        ('DOCUMENT_ARCHIVE_REVISION_CHAIN_INVALID') on any failure.
        """
        prior_root = self.archive_root / prior_doc_id
        prior_record_path = prior_root / "record.json"
        prior_text_path = prior_root / "text.txt"
        prior_dedup_path = prior_root / "dedup.json"

        if not prior_record_path.is_file() or not prior_text_path.is_file():
            raise ReviewRequired("DOCUMENT_ARCHIVE_REVISION_CHAIN_INVALID")
        try:
            prior_rec = json.loads(prior_record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReviewRequired("DOCUMENT_ARCHIVE_REVISION_CHAIN_INVALID") from exc
        if not isinstance(prior_rec, dict) or prior_rec.get("doc_id") != prior_doc_id:
            raise ReviewRequired("DOCUMENT_ARCHIVE_REVISION_CHAIN_INVALID")

        prior_content_hash = prior_rec.get("content_hash")
        if not isinstance(prior_content_hash, str) or not prior_content_hash:
            raise ReviewRequired("DOCUMENT_ARCHIVE_REVISION_CHAIN_INVALID")
        try:
            prior_text_bytes = prior_text_path.read_bytes()
            prior_text_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ReviewRequired("DOCUMENT_ARCHIVE_REVISION_CHAIN_INVALID") from exc
        if hashlib.sha256(prior_text_bytes).hexdigest() != prior_content_hash:
            raise ReviewRequired("DOCUMENT_ARCHIVE_REVISION_CHAIN_INVALID")

        prior_dedup: Optional[Dict[str, Any]] = None
        if prior_dedup_path.is_file():
            try:
                loaded_dedup = json.loads(prior_dedup_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ReviewRequired("DOCUMENT_ARCHIVE_REVISION_CHAIN_INVALID") from exc
            if not isinstance(loaded_dedup, dict):
                raise ReviewRequired("DOCUMENT_ARCHIVE_REVISION_CHAIN_INVALID")
            prior_dedup = loaded_dedup

        return prior_root, prior_rec, prior_dedup

    def _reconcile_revision_chain(self, record):
        """Idempotently reconcile prior-doc links and revision_index.json.

        Fail-closed: raises ReviewRequired('DOCUMENT_ARCHIVE_REVISION_CHAIN_INVALID')
        if the prior archive is missing or corrupt. Performs zero file writes
        when the revision chain and revision_index.json are already consistent.
        """
        prior_doc_id = record.get("supersedes_doc_id")
        if not prior_doc_id:
            return False, None

        new_doc_id = record["doc_id"]
        revision_seq = int(record.get("revision_seq") or 2)
        expected_prior_seq = max(1, revision_seq - 1)

        prior_root, prior_rec, prior_dedup = self._validate_prior_archive_integrity(
            prior_doc_id
        )
        prior_record_path = prior_root / "record.json"
        prior_dedup_path = prior_root / "dedup.json"

        repaired = False
        if (
            prior_rec.get("superseded_by_doc_id") != new_doc_id
            or prior_rec.get("revision_seq") is None
        ):
            prior_updated = dict(prior_rec)
            prior_updated["superseded_by_doc_id"] = new_doc_id
            prior_updated.setdefault("revision_seq", expected_prior_seq)
            atomic(prior_record_path, canonical_bytes(prior_updated))
            prior_rec = prior_updated
            repaired = True

        self._records_cache[prior_doc_id] = prior_rec
        self._records_cache[new_doc_id] = record

        if prior_dedup is not None:
            if (
                prior_dedup.get("superseded_by_doc_id") != new_doc_id
                or prior_dedup.get("revision_seq") is None
            ):
                prior_dedup["superseded_by_doc_id"] = new_doc_id
                prior_dedup.setdefault("revision_seq", expected_prior_seq)
                atomic(prior_dedup_path, canonical_bytes(prior_dedup))
                repaired = True
        else:
            prior_dedup = {
                "schema_version": SCHEMA_VERSION,
                "doc_id": prior_doc_id,
                "content_hash": prior_rec.get("content_hash"),
                "canonical_url": prior_rec.get("canonical_url"),
                "disposition": prior_rec.get("dedup_disposition", "new"),
                "capture_ids": [],
                "revision_of": [],
                "supersedes_doc_id": prior_rec.get("supersedes_doc_id"),
                "superseded_by_doc_id": new_doc_id,
                "revision_seq": int(prior_rec.get("revision_seq") or expected_prior_seq),
                "near_duplicates": [],
            }
            atomic(prior_dedup_path, canonical_bytes(prior_dedup))
            repaired = True

        rev_index_path = self.archive_root / "revision_index.json"
        index_consistent = False
        if rev_index_path.is_file():
            try:
                rev_index = json.loads(rev_index_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                rev_index = None
            if isinstance(rev_index, dict):
                index_consistent = self._revision_index_has_edge(
                    rev_index,
                    prior_doc_id=prior_doc_id,
                    new_doc_id=new_doc_id,
                    canonical_url=record.get("canonical_url"),
                    item_id=record.get("item_id"),
                )
        if not index_consistent:
            self._write_revision_index()
            repaired = True

        revision_summary = {
            "prior_doc_id": prior_doc_id,
            "new_doc_id": new_doc_id,
            "canonical_url": record.get("canonical_url"),
            "item_id": record.get("item_id"),
            "revision_seq": revision_seq,
            "prior_content_hash": prior_rec.get("content_hash"),
            "new_content_hash": record.get("content_hash"),
            "disclosure_posture": "LOUD_REVISION",
        }
        return repaired, revision_summary

    def _capture(self, parsed, capture_channel):
        capture_id = _capture_id(parsed, capture_channel)
        raw_name = capture_id + _raw_suffix(parsed)
        attachments = []
        for item in parsed.get("attachments") or []:
            if item.get("kind") == "remote":
                attachments.append({
                    "kind": "remote",
                    "name": item.get("name"),
                    "url": item.get("url"),
                })
                continue
            path = Path(item["path"]).resolve()
            if not path.is_relative_to(self.input_root):
                raise ReviewRequired("LOCAL_ATTACHMENT_MUST_BE_PRIVATE_INPUT")
            data = path.read_bytes()
            name = _safe_name(item.get("name") or path.name)
            archived = f"attachments/{capture_id}/{name}"
            attachments.append({
                "kind": "local",
                "name": name,
                "source_locator": path.relative_to(self.input_root).as_posix(),
                "archive_path": archived,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "_data": data,
            })
        capture_dict = {
            "capture_id": capture_id,
            "capture_channel": capture_channel,
            "source_type": parsed.get("source_type"),
            "source_locator": parsed.get("source_locator"),
            "raw_path": f"raw/{raw_name}",
            "raw_sha256": hashlib.sha256(parsed["raw"]).hexdigest(),
            "raw_bytes": len(parsed["raw"]),
            "parser_name": parsed.get("parser_name", capture_channel),
            "parser_version": parsed.get("parser_version", PARSER_VERSION),
            "source_metadata": _jsonable(parsed.get("front_matter") or {}),
            "document_metadata": _jsonable(parsed.get("document_metadata") or {}),
            "tab_topology": _jsonable(parsed.get("tab_topology") or []),
            "remote_references": sorted(set(parsed.get("remote_references") or [])),
            "relationship_provenance": _jsonable(
                parsed.get("relationship_provenance") or []
            ),
            "attachments": attachments,
        }
        acq = parsed.get("acquisition_meta")
        if isinstance(acq, dict):
            capture_dict["bundle_raw_sha256"] = capture_dict["raw_sha256"]
            capture_dict["raw_sha256"] = acq["raw_sha256"]
            capture_dict["extracted_sha256"] = acq["extracted_sha256"]
            capture_dict["upstream_origin"] = acq.get("upstream_origin")
            capture_dict["related_urls"] = list(acq.get("related_urls") or [])
            capture_dict["source_tier"] = acq.get("source_tier")
            capture_dict["item_id"] = acq.get("item_id")
            capture_dict["source_id"] = acq.get("source_id")
            capture_dict["completeness"] = acq.get("completeness")
            capture_dict["event_class"] = acq.get("event_class")
            capture_dict["matched_persons"] = list(acq.get("matched_persons") or [])
            capture_dict["extractor"] = acq.get("extractor")
            capture_dict["final_url"] = acq.get("final_url")
            capture_dict["raw_ref"] = acq.get("raw_ref")
            capture_dict["canonical_url"] = acq.get("canonical_url")
        return capture_dict

    def _write_capture(self, root, capture, raw):
        atomic(root / capture["raw_path"], raw)
        for item in capture["attachments"]:
            if item["kind"] == "local":
                atomic(root / item["archive_path"], item["_data"])

    def _public_capture(self, capture):
        return {
            key: value
            for key, value in capture.items()
            if key != "_data"
            and not (key == "attachments" and any(
                isinstance(item, dict) and "_data" in item
                for item in value
            ))
        }

    def _capture_provenance(self, capture):
        public = self._public_capture(capture)
        public["attachments"] = [
            {key: value for key, value in item.items() if key != "_data"}
            for item in capture["attachments"]
        ]
        return public

    def _ingest_parsed(self, parsed, *, capture_channel=None, captured_at=None,
                       published_at=None, canonical_url=None, title=None,
                       tags=None, rights_classification=None,
                       public_export_allowed=None, rights_evidence=None,
                       latency_seconds=0.0, **_ignored):
        channel = capture_channel or parsed.get("capture_channel")
        if channel not in CAPTURE_CHANNELS:
            raise ReviewRequired("CAPTURE_CHANNEL_INVALID")
        normalized = normalize_text(parsed["text"])
        normalized_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        identifier = "doc_" + normalized_hash[:24]
        selected_url = normalize_canonical_url(
            canonical_url if canonical_url is not None else parsed.get("canonical_url")
        )
        selected_published = _timestamp(
            published_at if published_at is not None else parsed.get("published_at"),
            label="published_at", required=False,
        )
        selected_captured = _timestamp(
            captured_at if captured_at is not None
            else datetime.now(timezone.utc).isoformat(),
            label="captured_at", required=True,
        )
        selected_tags, review_tags = validate_tags(
            self.engine_root, tags if tags is not None else parsed.get("tags")
        )
        rights = rights_decision(
            {**parsed, "capture_channel": channel},
            classification=rights_classification,
            public_export_allowed=public_export_allowed,
            evidence=rights_evidence,
        )
        capture = self._capture({**parsed, "capture_channel": channel}, channel)
        capture["rights"] = rights
        acq = parsed.get("acquisition_meta") if isinstance(parsed.get("acquisition_meta"), dict) else None
        selected_item_id = acq.get("item_id") if acq else None
        record = {
            "schema_version": SCHEMA_VERSION,
            "doc_id": identifier,
            "canonical_url": selected_url,
            "published_at": selected_published,
            "captured_at": selected_captured,
            "content_hash": normalized_hash,
            "capture_channel": channel,
            "rights": rights,
            "title": title if title is not None else parsed.get("title"),
            "tags": selected_tags,
            "review_candidates": review_tags,
            "source_type": parsed.get("source_type"),
            "parser_version": parsed.get("parser_version", PARSER_VERSION),
            "canonical_write_allowed": False,
        }
        if acq is not None:
            record.update({
                "item_id": acq["item_id"],
                "source_id": acq["source_id"],
                "source_tier": acq["source_tier"],
                "upstream_origin": acq.get("upstream_origin"),
                "related_urls": list(acq.get("related_urls") or []),
                "raw_sha256": acq["raw_sha256"],
                "extracted_sha256": acq["extracted_sha256"],
                "completeness": acq["completeness"],
                "event_class": acq["event_class"],
                "matched_persons": list(acq.get("matched_persons") or []),
                "extractor": acq["extractor"],
                "final_url": acq["final_url"],
                "raw_ref": acq.get("raw_ref"),
            })
        root = self.archive_root / identifier
        existing = root.is_dir()
        if existing:
            try:
                previous = json.loads((root / "record.json").read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ReviewRequired("DOCUMENT_ARCHIVE_RECORD_INVALID") from exc
            if previous.get("content_hash") != normalized_hash:
                raise ReviewRequired("DOCUMENT_ID_COLLISION")
            return self._merge_exact(root, previous, capture, parsed["raw"])

        existing_records = self._existing_records()
        related_records = [
            item for item in existing_records
            if (selected_url and item.get("canonical_url") == selected_url)
            or (selected_item_id and item.get("item_id") == selected_item_id)
        ]
        related = sorted(item["doc_id"] for item in related_records)
        near = []
        normalized_shingles = None
        if not related:
            normalized_shingles = _shingles(normalized)
            for item in existing_records:
                cand_text, cand_shingles = self._get_cached_text_and_shingles(
                    item["doc_id"]
                )
                comparison = _compare_shingles(
                    normalized, normalized_shingles, cand_text, cand_shingles
                )
                if comparison["candidate"]:
                    near.append({"doc_id": item["doc_id"], **comparison})
        near.sort(key=lambda item: item["doc_id"])
        disposition = (
            "revision" if related else
            "review_required" if near else
            "new"
        )
        prior_rec = None
        prior_doc_id = None
        revision_seq = 1
        if related_records:
            active_heads = [
                item for item in related_records
                if not item.get("superseded_by_doc_id")
            ]
            candidates = active_heads if active_heads else related_records
            candidates.sort(
                key=lambda item: (
                    int(item.get("revision_seq") or 1),
                    str(item.get("captured_at") or ""),
                    item["doc_id"],
                )
            )
            prior_rec = candidates[-1]
            prior_doc_id = prior_rec["doc_id"]
            revision_seq = int(prior_rec.get("revision_seq") or 1) + 1

        record["dedup_disposition"] = disposition
        record["supersedes_doc_id"] = prior_doc_id
        record["superseded_by_doc_id"] = None
        record["revision_seq"] = revision_seq

        provenance = {
            "schema_version": SCHEMA_VERSION,
            "doc_id": identifier,
            "captures": [self._capture_provenance(capture)],
        }
        if acq is not None:
            provenance["acquisition"] = {
                "item_id": acq["item_id"],
                "source_id": acq["source_id"],
                "source_tier": acq["source_tier"],
                "upstream_origin": acq.get("upstream_origin"),
                "related_urls": list(acq.get("related_urls") or []),
                "raw_sha256": acq["raw_sha256"],
                "extracted_sha256": acq["extracted_sha256"],
                "completeness": acq["completeness"],
                "event_class": acq["event_class"],
                "matched_persons": list(acq.get("matched_persons") or []),
                "extractor": acq["extractor"],
                "final_url": acq["final_url"],
                "raw_ref": acq.get("raw_ref"),
            }
            provenance["upstream_origin"] = acq.get("upstream_origin")
            provenance["related_urls"] = list(acq.get("related_urls") or [])
            provenance["source_tier"] = acq["source_tier"]
            provenance["raw_sha256"] = acq["raw_sha256"]
            provenance["extracted_sha256"] = acq["extracted_sha256"]
            provenance["completeness"] = acq["completeness"]
            provenance["item_id"] = acq["item_id"]
            provenance["source_id"] = acq["source_id"]

        dedup = {
            "schema_version": SCHEMA_VERSION,
            "doc_id": identifier,
            "content_hash": normalized_hash,
            "canonical_url": selected_url,
            "disposition": disposition,
            "capture_ids": [capture["capture_id"]],
            "revision_of": related,
            "supersedes_doc_id": prior_doc_id,
            "superseded_by_doc_id": None,
            "revision_seq": revision_seq,
            "near_duplicates": near,
        }
        latency = float(latency_seconds or 0.0)
        if not math.isfinite(latency) or latency < 0:
            raise ReviewRequired("DOCUMENT_LATENCY_INVALID")
        metrics = {
            "schema_version": SCHEMA_VERSION,
            "doc_id": identifier,
            "tokens": 0,
            "tokens_per_document": 0,
            "cost_usd": 0.0,
            "cost_per_document_usd": 0.0,
            "latency_seconds": latency,
            "latency_seconds_per_document": latency,
            "review_minutes": 0.0,
            "review_minutes_per_document": 0.0,
            "llm_used": False,
        }

        if disposition == "revision" and prior_doc_id is not None:
            self._validate_prior_archive_integrity(prior_doc_id)

        staging_parent = self.archive_root / ".staging"
        staging_dir = staging_parent / f"{identifier}.{uuid.uuid4().hex[:12]}"
        try:
            staging_dir.mkdir(parents=True, exist_ok=True)
            self._write_capture(staging_dir, capture, parsed["raw"])
            atomic(staging_dir / "text.txt", normalized.encode("utf-8"))
            atomic(staging_dir / "record.json", canonical_bytes(record))
            atomic(staging_dir / "provenance.json", canonical_bytes(provenance))
            atomic(staging_dir / "dedup.json", canonical_bytes(dedup))
            atomic(staging_dir / "metrics.json", canonical_bytes(metrics))
            (staging_dir / "attachments").mkdir(parents=True, exist_ok=True)
            self.archive_root.mkdir(parents=True, exist_ok=True)
            os.replace(staging_dir, root)
        except BaseException:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

        self._records_cache[identifier] = record
        self._text_cache[identifier] = normalized
        if normalized_shingles is not None:
            self._shingles_cache[identifier] = normalized_shingles

        revision_summary = None
        if disposition == "revision" and prior_rec is not None and prior_doc_id is not None:
            _, revision_summary = self._reconcile_revision_chain(record)

        result = {
            "status": "REVIEW_REQUIRED" if disposition == "review_required"
            else "ACCEPTED",
            "accepted": disposition != "review_required",
            "doc_id": identifier,
            "content_hash": normalized_hash,
            "disposition": disposition,
            "archive_path": str(root),
            "review_candidates": review_tags,
        }
        if revision_summary is not None:
            result["revision_summary"] = revision_summary
        return result

    def _merge_exact(self, root, record, capture, raw):
        prior_doc_id = record.get("supersedes_doc_id")
        if prior_doc_id:
            self._validate_prior_archive_integrity(prior_doc_id)
        record_path = root / "record.json"
        provenance_path = root / "provenance.json"
        dedup_path = root / "dedup.json"
        text_path = root / "text.txt"
        if not text_path.is_file():
            raise ReviewRequired("DOCUMENT_ARCHIVE_PROVENANCE_INVALID")
        needs_repair = False
        try:
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            if not isinstance(provenance, dict) or not isinstance(
                provenance.get("captures"), list
            ):
                raise ValueError("INVALID_PROVENANCE_SHAPE")
        except (OSError, ValueError):
            provenance = {
                "schema_version": SCHEMA_VERSION,
                "doc_id": record["doc_id"],
                "captures": [],
            }
            needs_repair = True
        try:
            dedup = json.loads(dedup_path.read_text(encoding="utf-8"))
            if not isinstance(dedup, dict):
                raise ValueError("INVALID_DEDUP_SHAPE")
        except (OSError, ValueError):
            dedup = {
                "schema_version": SCHEMA_VERSION,
                "doc_id": record["doc_id"],
                "content_hash": record["content_hash"],
                "canonical_url": record.get("canonical_url"),
                "disposition": record.get("dedup_disposition", "new"),
                "capture_ids": [
                    item["capture_id"]
                    for item in provenance.get("captures", [])
                    if isinstance(item, dict) and item.get("capture_id")
                ],
                "revision_of": [],
                "near_duplicates": [],
            }
            needs_repair = True
        capture_ids = {
            item["capture_id"]
            for item in provenance.get("captures", [])
            if isinstance(item, dict) and item.get("capture_id")
        }
        dedup_capture_ids = set(dedup.get("capture_ids") or [])
        if capture["capture_id"] in capture_ids:
            if (
                not needs_repair
                and capture["capture_id"] in dedup_capture_ids
                and (len(capture_ids) <= 1 or dedup.get("disposition") == "exact_merge")
            ):
                repaired, revision_summary = self._reconcile_revision_chain(record)
                result = {
                    "status": "ACCEPTED",
                    "accepted": True,
                    "doc_id": record["doc_id"],
                    "content_hash": record["content_hash"],
                    "disposition": "exact_merge",
                    "archive_path": str(root),
                    "changed": repaired,
                }
                if repaired and revision_summary is not None:
                    result["revision_summary"] = revision_summary
                return result
            # Recover interrupted merge where provenance had capture_id but dedup/record lagged
            dedup["disposition"] = "exact_merge"
            dedup["capture_ids"] = sorted(dedup_capture_ids | capture_ids)
            if needs_repair:
                atomic(provenance_path, canonical_bytes(provenance))
            atomic(dedup_path, canonical_bytes(dedup))
            self._records_cache[record["doc_id"]] = record
            repaired, revision_summary = self._reconcile_revision_chain(record)
            result = {
                "status": "ACCEPTED",
                "accepted": True,
                "doc_id": record["doc_id"],
                "content_hash": record["content_hash"],
                "disposition": "exact_merge",
                "archive_path": str(root),
                "changed": True,
            }
            if repaired and revision_summary is not None:
                result["revision_summary"] = revision_summary
            return result
        public = self._capture_provenance(capture)
        self._write_capture(root, capture, raw)
        provenance["captures"].append(public)
        provenance["captures"].sort(key=lambda item: item["capture_id"])
        all_rights = [
            item.get("rights")
            for item in provenance["captures"]
            if isinstance(item.get("rights"), dict)
        ]
        classifications = {
            item.get("classification") for item in all_rights
        }
        if "private" in classifications:
            classification = "private"
        elif "copyrighted" in classifications:
            classification = "copyrighted"
        elif classifications == {"public"}:
            classification = "public"
        else:
            classification = "unknown"
        evidence = sorted({
            json.dumps(item, ensure_ascii=False, sort_keys=True)
            for rights in all_rights
            for item in rights.get("evidence", [])
        })
        rights = {
            "classification": classification,
            "public_export_allowed": bool(
                classification == "public"
                and evidence
                and all(
                    item.get("public_export_allowed") is True
                    for item in all_rights
                )
            ),
            "evidence": [json.loads(item) for item in evidence],
        }
        record["rights"] = rights
        dedup["disposition"] = "exact_merge"
        dedup["capture_ids"] = sorted(
            set(dedup.get("capture_ids", [])) | {capture["capture_id"]}
        )
        atomic(record_path, canonical_bytes(record))
        atomic(provenance_path, canonical_bytes(provenance))
        atomic(dedup_path, canonical_bytes(dedup))
        self._records_cache[record["doc_id"]] = record
        repaired, revision_summary = self._reconcile_revision_chain(record)
        result = {
            "status": "ACCEPTED",
            "accepted": True,
            "doc_id": record["doc_id"],
            "content_hash": record["content_hash"],
            "disposition": "exact_merge",
            "archive_path": str(root),
            "changed": True,
        }
        if repaired and revision_summary is not None:
            result["revision_summary"] = revision_summary
        return result
