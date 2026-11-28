"""D-01 Overseas Factual Index (`cba-kb.person-overseas-tenure.v1`).

Stores structured, factual overseas basketball tenures (pro club, NCAA college,
overseas coaching, draft entry) strictly under `D01_FACTUAL_INDEX_ONLY`.
Copyrighted long-form narrative bodies and unverified full-text scrapes are
rejected fail-closed.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Union
from urllib.parse import urlsplit

from .actor import ActorError, validate_person_uid

OVERSEAS_TENURE_SCHEMA = "cba-kb.person-overseas-tenure.v1"
OVERSEAS_TENURE_TYPES = frozenset({
    "PRO_CLUB",
    "COLLEGE_NCAA",
    "COACHING_OVERSEAS",
    "DRAFT_ENTRY",
})
D01_RIGHTS_TIER = "D01_FACTUAL_INDEX_ONLY"
DEFAULT_MAX_NARRATIVE_CHARS = 280

_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")
_FORBIDDEN_COPYRIGHT_FIELDS = frozenset({
    "full_text",
    "article_body",
    "body",
    "verbatim",
    "verbatim_text",
    "copyright_body",
    "copyright_text",
    "raw_text",
    "raw_html",
    "content",
})
_NARRATIVE_TEXT_FIELDS = frozenset({
    "notes",
    "summary",
    "narrative",
    "description",
    "excerpt",
    "bio_text",
    "comment",
    "remarks",
})


class OverseasTenureError(ValueError):
    """Raised when a `cba-kb.person-overseas-tenure.v1` record violates D-01 rules."""


def _required_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise OverseasTenureError(f"{label}_REQUIRED")
    return value.strip()


def _validate_https_url(value: Any, label: str) -> str:
    url = _required_str(value, label)
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise OverseasTenureError(f"{label}_HTTPS_REQUIRED")
    return url


def _is_public_https_url(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        return False
    host = parsed.netloc.rsplit("@", 1)[-1].split(":", 1)[0].rstrip(".").lower()
    if host in ("drive.google.com", "docs.google.com") or host.endswith(
        (".drive.google.com", ".docs.google.com")
    ):
        return False
    return True


def _validate_evidence_ref(ref: Any) -> Dict[str, str]:
    if not isinstance(ref, Mapping):
        raise OverseasTenureError("EVIDENCE_REF_OBJECT_REQUIRED")
    source_title = _required_str(ref.get("source_title"), "EVIDENCE_SOURCE_TITLE")
    source_url = _validate_https_url(ref.get("source_url"), "EVIDENCE_SOURCE_URL")
    raw_sha = _required_str(ref.get("raw_sha256"), "EVIDENCE_RAW_SHA256").lower()
    if not _HEX64_RE.fullmatch(raw_sha):
        raise OverseasTenureError("EVIDENCE_RAW_SHA256_INVALID")
    captured_at = _required_str(ref.get("captured_at"), "EVIDENCE_CAPTURED_AT")
    return {
        "source_title": source_title,
        "source_url": source_url,
        "raw_sha256": raw_sha,
        "captured_at": captured_at,
    }


def validate_overseas_tenure(
    record: Mapping[str, Any],
    *,
    max_narrative_chars: int = DEFAULT_MAX_NARRATIVE_CHARS,
) -> Dict[str, Any]:
    """Validate and normalize a `cba-kb.person-overseas-tenure.v1` record."""
    if not isinstance(record, Mapping):
        raise OverseasTenureError("OVERSEAS_TENURE_OBJECT_REQUIRED")

    if isinstance(max_narrative_chars, bool) or not isinstance(max_narrative_chars, int) or max_narrative_chars < 0:
        raise OverseasTenureError("MAX_NARRATIVE_CHARS_INVALID")

    schema = record.get("schema")
    if schema != OVERSEAS_TENURE_SCHEMA:
        raise OverseasTenureError(f"INVALID_OVERSEAS_TENURE_SCHEMA:{schema}")

    try:
        person_uid = validate_person_uid(record.get("person_uid"))
    except ActorError as exc:
        raise OverseasTenureError(f"INVALID_PERSON_UID:{exc}") from exc

    tenure_type = _required_str(record.get("tenure_type"), "TENURE_TYPE")
    if tenure_type not in OVERSEAS_TENURE_TYPES:
        raise OverseasTenureError(f"INVALID_TENURE_TYPE:{tenure_type}")

    season_span = _required_str(record.get("season_span"), "SEASON_SPAN")
    league_name = _required_str(record.get("league_name"), "LEAGUE_NAME")
    organization_name = _required_str(record.get("organization_name"), "ORGANIZATION_NAME")

    rights_tier = _required_str(record.get("rights_tier"), "RIGHTS_TIER")
    if rights_tier != D01_RIGHTS_TIER:
        raise OverseasTenureError(f"INVALID_RIGHTS_TIER:{rights_tier}")

    # Reject forbidden copyright body fields fail-closed
    for key in _FORBIDDEN_COPYRIGHT_FIELDS:
        if key in record and record[key] not in (None, "", [], {}):
            raise OverseasTenureError(f"FORBIDDEN_COPYRIGHT_BODY_FIELD:{key}")

    # Reject long narrative text fields (> max_narrative_chars)
    for key in _NARRATIVE_TEXT_FIELDS:
        if key in record and record[key] is not None:
            val = record[key]
            if not isinstance(val, str):
                raise OverseasTenureError(f"NARRATIVE_FIELD_INVALID:{key}")
            if len(val.strip()) > max_narrative_chars:
                raise OverseasTenureError(
                    f"NARRATIVE_TOO_LONG:{key}:len={len(val.strip())}:max={max_narrative_chars}"
                )

    raw_ext_ids = record.get("external_standard_ids")
    if raw_ext_ids is None:
        external_standard_ids: Dict[str, str] = {}
    elif isinstance(raw_ext_ids, Mapping):
        external_standard_ids = {
            _required_str(k, "EXTERNAL_ID_KEY"): _required_str(v, "EXTERNAL_ID_VALUE")
            for k, v in raw_ext_ids.items()
        }
    else:
        raise OverseasTenureError("EXTERNAL_STANDARD_IDS_DICT_REQUIRED")

    raw_honors = record.get("official_honors")
    if raw_honors is None:
        official_honors: List[str] = []
    elif isinstance(raw_honors, (str, bytes)) or not isinstance(raw_honors, Sequence):
        raise OverseasTenureError("OFFICIAL_HONORS_LIST_REQUIRED")
    else:
        official_honors = [_required_str(h, "OFFICIAL_HONOR") for h in raw_honors]

    raw_refs = record.get("evidence_refs")
    if (
        isinstance(raw_refs, (str, bytes))
        or not isinstance(raw_refs, Sequence)
        or len(raw_refs) == 0
    ):
        raise OverseasTenureError("EVIDENCE_REFS_NON_EMPTY_LIST_REQUIRED")
    evidence_refs = [_validate_evidence_ref(ref) for ref in raw_refs]

    out: Dict[str, Any] = {
        "schema": OVERSEAS_TENURE_SCHEMA,
        "person_uid": person_uid,
        "tenure_type": tenure_type,
        "season_span": season_span,
        "league_name": league_name,
        "organization_name": organization_name,
        "external_standard_ids": external_standard_ids,
        "official_honors": official_honors,
        "rights_tier": D01_RIGHTS_TIER,
        "evidence_refs": evidence_refs,
    }
    for key in ("notes", "summary"):
        if key in record and isinstance(record[key], str) and record[key].strip():
            out[key] = record[key].strip()
    return out


def load_overseas_tenures(
    path_or_records: Union[str, Path, Sequence[Mapping[str, Any]]],
    *,
    max_narrative_chars: int = DEFAULT_MAX_NARRATIVE_CHARS,
) -> List[Dict[str, Any]]:
    """Load and validate overseas tenure records from a JSONL/JSON file or sequence."""
    raw_items: List[Mapping[str, Any]] = []
    if isinstance(path_or_records, (str, Path)):
        path = Path(path_or_records)
        if not path.is_file():
            raise OverseasTenureError(f"OVERSEAS_TENURES_FILE_NOT_FOUND:{path.name}")
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return []
        if path.suffix.lower() == ".jsonl":
            for line in text.splitlines():
                s = line.strip()
                if not s:
                    continue
                obj = json.loads(s)
                if not isinstance(obj, Mapping):
                    raise OverseasTenureError("OVERSEAS_TENURE_OBJECT_REQUIRED")
                raw_items.append(obj)
        else:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                raw_items = list(parsed)
            elif isinstance(parsed, Mapping):
                for list_key in ("records", "tenures", "items", "overseas_tenures"):
                    if isinstance(parsed.get(list_key), list):
                        raw_items = list(parsed[list_key])
                        break
                else:
                    raw_items = [parsed]
            else:
                raise OverseasTenureError("OVERSEAS_TENURES_JSON_INVALID")
    elif isinstance(path_or_records, Sequence) and not isinstance(path_or_records, (bytes, bytearray)):
        raw_items = list(path_or_records)
    else:
        raise OverseasTenureError("OVERSEAS_TENURES_INPUT_INVALID")

    return [
        validate_overseas_tenure(item, max_narrative_chars=max_narrative_chars)
        for item in raw_items
    ]


def project_overseas_tenures_for_person(
    records: Sequence[Mapping[str, Any]],
    person_uid: str,
    *,
    visibility: str = "public_safe",
    max_narrative_chars: int = DEFAULT_MAX_NARRATIVE_CHARS,
) -> List[Dict[str, Any]]:
    """Filter and project D-01 overseas tenures for a single person."""
    if visibility not in ("public_safe", "internal_full"):
        raise OverseasTenureError(f"INVALID_VISIBILITY:{visibility}")
    try:
        clean_uid = validate_person_uid(person_uid)
    except ActorError as exc:
        raise OverseasTenureError(f"INVALID_PERSON_UID:{exc}") from exc

    projected: List[Dict[str, Any]] = []
    for raw in records or ():
        rec = validate_overseas_tenure(raw, max_narrative_chars=max_narrative_chars)
        if rec["person_uid"] != clean_uid:
            continue
        if visibility == "public_safe":
            pub_refs = [
                {
                    "source_title": ref["source_title"],
                    "source_url": ref["source_url"],
                    "captured_at": ref["captured_at"],
                }
                for ref in rec["evidence_refs"]
                if _is_public_https_url(ref["source_url"])
            ]
            projected.append({
                "schema": rec["schema"],
                "person_uid": rec["person_uid"],
                "tenure_type": rec["tenure_type"],
                "season_span": rec["season_span"],
                "league_name": rec["league_name"],
                "organization_name": rec["organization_name"],
                "external_standard_ids": dict(rec["external_standard_ids"]),
                "official_honors": list(rec["official_honors"]),
                "rights_tier": rec["rights_tier"],
                "evidence_refs": pub_refs,
            })
        else:
            projected.append(rec)

    projected.sort(
        key=lambda item: (
            item["season_span"],
            item["league_name"],
            item["organization_name"],
            item["tenure_type"],
        )
    )
    return projected
