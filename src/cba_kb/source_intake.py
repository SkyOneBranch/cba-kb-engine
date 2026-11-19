"""Source Intake Contract v1 and normalized document binding.

Follows REQ-190-STATEMENT-CLAIM-01 Deliverable D05:
- source_provider: provider transport name
- source_item_id_or_locator: provider-specific item ID or path
- acquired_at: ISO 8601 timestamp
- media_type: MIME media type
- content_hash: sha256 hex of normalized content
- rights: rights classification dict
- provenance: provenance dictionary
- raw_ref: raw source reference
- normalized_document_ref: doc_id
- intake_status: accepted | review_required | rejected

Invariants:
- Provider-specific source transport must not become knowledge semantics.
- Deduplication is content/provenance aware, not provider-name based.
- Persist/normalize before Statement/Claim consumption (R5).
- Downstream Statement/Claim IDs depend strictly on normalized content, not provider.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import yaml

from .common import atomic
from .document_lane import content_hash as doc_content_hash, doc_id as compute_doc_id, normalize_text
from .document_mentions import validate_doc_id
from .evidence_ledger import canonical_bytes

SCHEMA_VERSION = 1
SOURCE_INTAKE_VERSION = "v1.9"

ACQUISITION_BUNDLE_V1_SCHEMA = "acquisition_bundle_v1"
ACQUISITION_BUNDLE_V1_REQUIRED_KEYS = frozenset({
    "bundle_schema",
    "item_id",
    "canonical_url",
    "final_url",
    "related_urls",
    "upstream_origin",
    "source_id",
    "source_tier",
    "event_class",
    "matched_persons",
    "published_at",
    "acquired_at",
    "raw_sha256",
    "raw_ref",
    "extracted_sha256",
    "extractor",
    "completeness",
    "rights",
})
ACQUISITION_SOURCE_TIERS = frozenset({"T1", "T1.5", "T2", "MANUAL"})
ACQUISITION_COMPLETENESS_VALUES = frozenset({"full", "partial", "metadata_only"})

SOURCE_PROVIDERS = frozenset({
    "local_file",
    "google_drive_doc",
    "wechat_browser_clip",
    "ima_file_export",
    "synthetic_fixture",
    "acquisition_bundle_v1",
})

INTAKE_STATUSES = frozenset({
    "accepted",
    "review_required",
    "rejected",
})

_HASH_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

NORMALIZED_DOCUMENT_FIELDS = frozenset({
    "schema_version",
    "doc_id",
    "content_hash",
    "normalized_text",
    "source_provider",
    "source_locator",
    "rights",
    "provenance",
    "acquired_at",
    "media_type",
})


class SourceIntakeError(RuntimeError):
    pass


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceIntakeError(f"{label}_REQUIRED")
    if "\x00" in value:
        raise SourceIntakeError(f"{label}_INVALID")
    return value.strip()


def validate_content_hash(value: str) -> str:
    value = _required_text(value, "CONTENT_HASH")
    if not _HASH_SHA256.fullmatch(value):
        raise SourceIntakeError("CONTENT_HASH_SHA256_INVALID")
    return value


def validate_normalized_document(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Validate normalized document artifact produced before Statement consumption (R5)."""
    if not isinstance(doc, dict):
        raise SourceIntakeError("NORMALIZED_DOCUMENT_OBJECT_REQUIRED")

    missing = NORMALIZED_DOCUMENT_FIELDS - set(doc)
    if missing:
        raise SourceIntakeError(f"NORMALIZED_DOCUMENT_FIELDS_MISSING:{sorted(missing)}")

    doc_id = validate_doc_id(doc["doc_id"])
    content_hash = validate_content_hash(doc["content_hash"])
    text = doc["normalized_text"]
    if not isinstance(text, str):
        raise SourceIntakeError("NORMALIZED_TEXT_STRING_REQUIRED")

    expected_hash = doc_content_hash(text)
    if content_hash != expected_hash:
        raise SourceIntakeError(f"CONTENT_HASH_MISMATCH: expected {expected_hash} got {content_hash}")

    expected_doc_id = compute_doc_id(text)
    if doc_id != expected_doc_id:
        raise SourceIntakeError(f"DOC_ID_MISMATCH: expected {expected_doc_id} got {doc_id}")

    rights = doc["rights"]
    if not isinstance(rights, dict) or "classification" not in rights:
        raise SourceIntakeError("RIGHTS_OBJECT_REQUIRED")

    return {
        "schema_version": SCHEMA_VERSION,
        "doc_id": doc_id,
        "content_hash": content_hash,
        "normalized_text": text,
        "source_provider": _required_text(doc["source_provider"], "SOURCE_PROVIDER"),
        "source_locator": _required_text(doc["source_locator"], "SOURCE_LOCATOR"),
        "rights": rights,
        "provenance": doc["provenance"],
        "acquired_at": _required_text(doc["acquired_at"], "ACQUIRED_AT"),
        "media_type": _required_text(doc["media_type"], "MEDIA_TYPE"),
    }


def make_normalized_document(
    doc_id: str,
    content_hash: str,
    normalized_text: str,
    *,
    source_provider: str,
    source_locator: str,
    rights: Dict[str, Any],
    provenance: Dict[str, Any],
    acquired_at: str,
    media_type: str = "text/plain",
) -> Dict[str, Any]:
    """Construct and validate a normalized document record."""
    return validate_normalized_document({
        "schema_version": SCHEMA_VERSION,
        "doc_id": doc_id,
        "content_hash": content_hash,
        "normalized_text": normalized_text,
        "source_provider": source_provider,
        "source_locator": source_locator,
        "rights": rights,
        "provenance": provenance,
        "acquired_at": acquired_at,
        "media_type": media_type,
    })


def validate_source_intake(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a normalized source intake envelope dict."""
    if not isinstance(envelope, dict):
        raise SourceIntakeError("SOURCE_INTAKE_OBJECT_REQUIRED")

    required = {
        "source_provider",
        "source_item_id_or_locator",
        "acquired_at",
        "media_type",
        "content_hash",
        "rights",
        "provenance",
        "raw_ref",
        "normalized_document_ref",
        "intake_status",
    }
    missing = required - set(envelope)
    if missing:
        raise SourceIntakeError(f"SOURCE_INTAKE_FIELDS_MISSING:{sorted(missing)}")

    provider = _required_text(envelope["source_provider"], "SOURCE_PROVIDER")
    if provider not in SOURCE_PROVIDERS:
        raise SourceIntakeError("SOURCE_PROVIDER_INVALID")

    locator = _required_text(envelope["source_item_id_or_locator"], "SOURCE_LOCATOR")
    acquired_at = _required_text(envelope["acquired_at"], "ACQUIRED_AT")
    media_type = _required_text(envelope["media_type"], "MEDIA_TYPE")
    chash = validate_content_hash(envelope["content_hash"])

    rights = envelope["rights"]
    if not isinstance(rights, dict) or "classification" not in rights:
        raise SourceIntakeError("RIGHTS_OBJECT_REQUIRED")

    provenance = envelope["provenance"]
    if provenance in (None, "", {}):
        raise SourceIntakeError("PROVENANCE_REQUIRED")

    raw_ref = _required_text(str(envelope["raw_ref"]), "RAW_REF")

    doc_ref = envelope["normalized_document_ref"]
    if doc_ref is not None:
        doc_ref = validate_doc_id(doc_ref)

    status = _required_text(envelope["intake_status"], "INTAKE_STATUS")
    if status not in INTAKE_STATUSES:
        raise SourceIntakeError("INTAKE_STATUS_INVALID")

    result = {
        "source_provider": provider,
        "source_item_id_or_locator": locator,
        "acquired_at": acquired_at,
        "media_type": media_type,
        "content_hash": chash,
        "rights": rights,
        "provenance": provenance,
        "raw_ref": raw_ref,
        "normalized_document_ref": doc_ref,
        "intake_status": status,
    }
    if "normalized_document" in envelope and envelope["normalized_document"] is not None:
        result["normalized_document"] = validate_normalized_document(envelope["normalized_document"])

    return result


def normalize_source_payload(
    raw_content: bytes | str,
    *,
    source_provider: str,
    source_item_id_or_locator: str,
    media_type: str = "text/plain",
    rights: Optional[Dict[str, Any]] = None,
    acquired_at: Optional[str] = None,
    provenance: Optional[Dict[str, Any]] = None,
    raw_ref: Optional[str] = None,
) -> Tuple[Dict[str, Any], str]:
    """Normalize raw intake payload into envelope and canonical normalized text.

    Decouples source provider details from canonical document semantics.
    Creates auditable normalized_document artifact (R5).
    """
    if isinstance(raw_content, bytes):
        if media_type == "application/pdf" or raw_content.startswith(b"%PDF-") or source_item_id_or_locator.lower().endswith(".pdf"):
            from .document_sources import parse_pdf
            res = parse_pdf(raw_content, source_locator=source_item_id_or_locator)
            text = res.get("text", "")
        else:
            try:
                text = raw_content.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw_content.decode("gb18030", errors="replace")
    elif isinstance(raw_content, str):
        text = raw_content
    else:
        raise SourceIntakeError("RAW_CONTENT_BYTES_OR_STR_REQUIRED")

    norm_text = normalize_text(text)
    c_hash = doc_content_hash(norm_text)
    doc_id = compute_doc_id(norm_text)

    ts = acquired_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    r_dict = rights or {
        "classification": "private",
        "public_export_allowed": False,
        "evidence": [],
    }
    prov = provenance or {
        "intake_version": SOURCE_INTAKE_VERSION,
        "provider": source_provider,
        "locator": source_item_id_or_locator,
    }

    norm_doc = make_normalized_document(
        doc_id=doc_id,
        content_hash=c_hash,
        normalized_text=norm_text,
        source_provider=source_provider,
        source_locator=source_item_id_or_locator,
        rights=r_dict,
        provenance=prov,
        acquired_at=ts,
        media_type=media_type,
    )

    envelope = validate_source_intake({
        "source_provider": source_provider,
        "source_item_id_or_locator": source_item_id_or_locator,
        "acquired_at": ts,
        "media_type": media_type,
        "content_hash": c_hash,
        "rights": r_dict,
        "provenance": prov,
        "raw_ref": raw_ref or source_item_id_or_locator,
        "normalized_document_ref": doc_id,
        "intake_status": "accepted" if norm_text.strip() else "review_required",
        "normalized_document": norm_doc,
    })

    return envelope, norm_text


def save_normalized_document(path: Path | str, doc: Dict[str, Any]) -> None:
    validated = validate_normalized_document(doc)
    atomic(Path(path), json.dumps(validated, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n")


def load_normalized_document(path: Path | str) -> Dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise SourceIntakeError(f"NORMALIZED_DOCUMENT_FILE_NOT_FOUND:{path}")
    data = json.loads(p.read_text(encoding="utf-8"))
    return validate_normalized_document(data)


def canonicalize_bundle_url(url: Any) -> str:
    """Validate and return a canonical http/https URL for acquisition_bundle_v1."""
    if not isinstance(url, str) or not url.strip():
        raise SourceIntakeError("BUNDLE_URL_REQUIRED")
    if "\x00" in url:
        raise SourceIntakeError("BUNDLE_URL_INVALID")
    cleaned = url.strip()
    parts = urlsplit(cleaned)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise SourceIntakeError("BUNDLE_URL_HTTP_HTTPS_REQUIRED")
    return cleaned


def compute_bundle_item_id(canonical_url: str) -> str:
    """Compute deterministic 64-char sha256 item_id from canonical_url."""
    cleaned = canonicalize_bundle_url(canonical_url)
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def _normalize_bundle_timestamp(
    value: Any,
    *,
    label: str,
    allow_none: bool,
    allow_date: bool = True,
) -> Optional[str]:
    if value is None:
        if allow_none:
            return None
        raise SourceIntakeError(f"{label}_REQUIRED")
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return value.isoformat()
    if isinstance(value, date):
        if not allow_date:
            raise SourceIntakeError(f"{label}_DATETIME_OR_STR_REQUIRED")
        return value.isoformat()
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned or "\x00" in cleaned:
            raise SourceIntakeError(f"{label}_INVALID")
        return cleaned
    raise SourceIntakeError(f"{label}_INVALID")


def _validate_sha256_hex(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HASH_SHA256.fullmatch(value):
        raise SourceIntakeError(f"{label}_SHA256_INVALID")
    return value


def validate_acquisition_bundle_v1(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Validate an acquisition_bundle_v1 18-key metadata contract (cba-kb-collector v0.3.1)."""
    if not isinstance(metadata, dict):
        raise SourceIntakeError("ACQUISITION_BUNDLE_OBJECT_REQUIRED")

    missing = ACQUISITION_BUNDLE_V1_REQUIRED_KEYS - set(metadata)
    if missing:
        raise SourceIntakeError(f"ACQUISITION_BUNDLE_KEYS_MISSING:{sorted(missing)}")

    if metadata.get("bundle_schema") != ACQUISITION_BUNDLE_V1_SCHEMA:
        raise SourceIntakeError("ACQUISITION_BUNDLE_SCHEMA_INVALID")

    canonical_url = canonicalize_bundle_url(metadata["canonical_url"])
    final_url = canonicalize_bundle_url(metadata["final_url"])

    item_id = _validate_sha256_hex(metadata["item_id"], "ITEM_ID")
    expected_item_id = compute_bundle_item_id(canonical_url)
    if item_id != expected_item_id:
        raise SourceIntakeError(
            f"ITEM_ID_MISMATCH: expected {expected_item_id} got {item_id}"
        )

    related_urls = metadata["related_urls"]
    if not isinstance(related_urls, list) or not all(
        isinstance(u, str) and u.strip() and "\x00" not in u for u in related_urls
    ):
        raise SourceIntakeError("RELATED_URLS_LIST_OF_STR_REQUIRED")

    upstream_origin = metadata["upstream_origin"]
    if upstream_origin is not None:
        upstream_origin = _required_text(upstream_origin, "UPSTREAM_ORIGIN")

    source_id = _required_text(metadata["source_id"], "SOURCE_ID")
    event_class = _required_text(metadata["event_class"], "EVENT_CLASS")
    extractor = _required_text(metadata["extractor"], "EXTRACTOR")

    source_tier = metadata["source_tier"]
    if not isinstance(source_tier, str) or source_tier not in ACQUISITION_SOURCE_TIERS:
        raise SourceIntakeError("SOURCE_TIER_INVALID")

    matched_persons = metadata["matched_persons"]
    if not isinstance(matched_persons, list) or not all(
        isinstance(p, str) and p.strip() and "\x00" not in p for p in matched_persons
    ):
        raise SourceIntakeError("MATCHED_PERSONS_LIST_OF_STR_REQUIRED")

    published_at = _normalize_bundle_timestamp(
        metadata["published_at"], label="PUBLISHED_AT", allow_none=True, allow_date=True
    )
    acquired_at = _normalize_bundle_timestamp(
        metadata["acquired_at"], label="ACQUIRED_AT", allow_none=False, allow_date=False
    )

    raw_sha256 = _validate_sha256_hex(metadata["raw_sha256"], "RAW_SHA256")
    extracted_sha256 = _validate_sha256_hex(metadata["extracted_sha256"], "EXTRACTED_SHA256")

    raw_ref = metadata["raw_ref"]
    if raw_ref is not None:
        raw_ref = _required_text(raw_ref, "RAW_REF")

    completeness = metadata["completeness"]
    if not isinstance(completeness, str) or completeness not in ACQUISITION_COMPLETENESS_VALUES:
        raise SourceIntakeError("COMPLETENESS_INVALID")

    rights = metadata["rights"]
    if not isinstance(rights, dict):
        raise SourceIntakeError("RIGHTS_OBJECT_REQUIRED")
    if rights.get("classification") != "copyrighted":
        raise SourceIntakeError("RIGHTS_CLASSIFICATION_MUST_BE_COPYRIGHTED")
    if rights.get("public_export_allowed") is not False:
        raise SourceIntakeError("RIGHTS_PUBLIC_EXPORT_MUST_BE_FALSE")

    validated = dict(metadata)
    validated.update({
        "bundle_schema": ACQUISITION_BUNDLE_V1_SCHEMA,
        "item_id": item_id,
        "canonical_url": canonical_url,
        "final_url": final_url,
        "related_urls": [u.strip() for u in related_urls],
        "upstream_origin": upstream_origin,
        "source_id": source_id,
        "source_tier": source_tier,
        "event_class": event_class,
        "matched_persons": [p.strip() for p in matched_persons],
        "published_at": published_at,
        "acquired_at": acquired_at,
        "raw_sha256": raw_sha256,
        "raw_ref": raw_ref,
        "extracted_sha256": extracted_sha256,
        "extractor": extractor,
        "completeness": completeness,
        "rights": {
            **rights,
            "classification": "copyrighted",
            "public_export_allowed": False,
        },
    })
    return validated


def parse_acquisition_bundle_markdown(
    raw_content: bytes | str,
) -> Tuple[Dict[str, Any], str]:
    """Parse and validate a markdown file with acquisition_bundle_v1 YAML front-matter."""
    if isinstance(raw_content, bytes):
        try:
            text = raw_content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SourceIntakeError("ACQUISITION_BUNDLE_UTF8_REQUIRED") from exc
    elif isinstance(raw_content, str):
        text = raw_content.lstrip("\ufeff")
    else:
        raise SourceIntakeError("ACQUISITION_BUNDLE_CONTENT_BYTES_OR_STR_REQUIRED")

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        raise SourceIntakeError("ACQUISITION_BUNDLE_FRONTMATTER_REQUIRED")

    lines = normalized.split("\n")
    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        raise SourceIntakeError("ACQUISITION_BUNDLE_FRONTMATTER_UNTERMINATED")

    fm_text = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1:]).strip()
    if not body:
        raise SourceIntakeError("ACQUISITION_BUNDLE_BODY_REQUIRED")

    try:
        metadata = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        raise SourceIntakeError("ACQUISITION_BUNDLE_YAML_INVALID") from exc

    validated = validate_acquisition_bundle_v1(metadata)
    return validated, body
