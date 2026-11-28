"""4-State Youth National Team Tenures (`cba-kb.youth-national-tenure.v1`).

Enforces strict separation of four youth national participation stages so
training camp callups are never conflated with final tournament rosters or
official tournament stats:
- `TRAINING_CAMP_CALLUP` -> 入选集训
- `WARMUP_APPEARANCE` -> 热身赛出场
- `FINAL_TOURNAMENT_ROSTER` -> 正赛最终名单
- `OFFICIAL_TOURNAMENT_STATS` -> 正赛技术统计
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union
from urllib.parse import urlsplit

from .actor import ActorError, validate_person_uid

YOUTH_NATIONAL_TENURE_SCHEMA = "cba-kb.youth-national-tenure.v1"
YOUTH_PARTICIPATION_STAGES = frozenset({
    "TRAINING_CAMP_CALLUP",
    "WARMUP_APPEARANCE",
    "FINAL_TOURNAMENT_ROSTER",
    "OFFICIAL_TOURNAMENT_STATS",
})
YOUTH_STAGE_LABELS_ZH: Dict[str, str] = {
    "TRAINING_CAMP_CALLUP": "入选集训",
    "WARMUP_APPEARANCE": "热身赛出场",
    "FINAL_TOURNAMENT_ROSTER": "正赛最终名单",
    "OFFICIAL_TOURNAMENT_STATS": "正赛技术统计",
}

_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")


class YouthTenureError(ValueError):
    """Raised when a `cba-kb.youth-national-tenure.v1` record is invalid."""


def _required_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise YouthTenureError(f"{label}_REQUIRED")
    return value.strip()


def _optional_str(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or "\x00" in value:
        raise YouthTenureError(f"{label}_INVALID")
    stripped = value.strip()
    return stripped or None


def _validate_https_url(value: Any, label: str) -> str:
    url = _required_str(value, label)
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise YouthTenureError(f"{label}_HTTPS_REQUIRED")
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
        raise YouthTenureError("EVIDENCE_REF_OBJECT_REQUIRED")
    source_title = _required_str(ref.get("source_title"), "EVIDENCE_SOURCE_TITLE")
    source_url = _validate_https_url(ref.get("source_url"), "EVIDENCE_SOURCE_URL")
    raw_sha = _required_str(ref.get("raw_sha256"), "EVIDENCE_RAW_SHA256").lower()
    if not _HEX64_RE.fullmatch(raw_sha):
        raise YouthTenureError("EVIDENCE_RAW_SHA256_INVALID")
    captured_at = _required_str(ref.get("captured_at"), "EVIDENCE_CAPTURED_AT")
    return {
        "source_title": source_title,
        "source_url": source_url,
        "raw_sha256": raw_sha,
        "captured_at": captured_at,
    }


def validate_youth_national_tenure(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate and normalize a `cba-kb.youth-national-tenure.v1` record."""
    if not isinstance(record, Mapping):
        raise YouthTenureError("YOUTH_TENURE_OBJECT_REQUIRED")

    schema = record.get("schema")
    if schema != YOUTH_NATIONAL_TENURE_SCHEMA:
        raise YouthTenureError(f"INVALID_YOUTH_TENURE_SCHEMA:{schema}")

    try:
        person_uid = validate_person_uid(record.get("person_uid"))
    except ActorError as exc:
        raise YouthTenureError(f"INVALID_PERSON_UID:{exc}") from exc

    competition_or_program = _required_str(
        record.get("competition_or_program"), "COMPETITION_OR_PROGRAM"
    )

    raw_season_or_year = record.get("season_or_year")
    if isinstance(raw_season_or_year, int) and not isinstance(raw_season_or_year, bool):
        season_or_year = str(raw_season_or_year)
    else:
        season_or_year = _required_str(raw_season_or_year, "SEASON_OR_YEAR")

    participation_stage = _required_str(
        record.get("participation_stage"), "PARTICIPATION_STAGE"
    )
    if participation_stage not in YOUTH_PARTICIPATION_STAGES:
        raise YouthTenureError(f"INVALID_PARTICIPATION_STAGE:{participation_stage}")

    team_or_unit = _required_str(record.get("team_or_unit"), "TEAM_OR_UNIT")
    stats_summary = _optional_str(record.get("stats_summary"), "STATS_SUMMARY")

    raw_refs = record.get("evidence_refs")
    if (
        isinstance(raw_refs, (str, bytes))
        or not isinstance(raw_refs, Sequence)
        or len(raw_refs) == 0
    ):
        raise YouthTenureError("EVIDENCE_REFS_NON_EMPTY_LIST_REQUIRED")
    evidence_refs = [_validate_evidence_ref(ref) for ref in raw_refs]

    return {
        "schema": YOUTH_NATIONAL_TENURE_SCHEMA,
        "person_uid": person_uid,
        "competition_or_program": competition_or_program,
        "season_or_year": season_or_year,
        "participation_stage": participation_stage,
        "team_or_unit": team_or_unit,
        "stats_summary": stats_summary,
        "evidence_refs": evidence_refs,
    }


def load_youth_national_tenures(
    path_or_records: Union[str, Path, Sequence[Mapping[str, Any]]],
) -> List[Dict[str, Any]]:
    """Load and validate youth national tenure records from a JSONL/JSON file or sequence."""
    raw_items: List[Mapping[str, Any]] = []
    if isinstance(path_or_records, (str, Path)):
        path = Path(path_or_records)
        if not path.is_file():
            raise YouthTenureError(f"YOUTH_TENURES_FILE_NOT_FOUND:{path.name}")
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
                    raise YouthTenureError("YOUTH_TENURE_OBJECT_REQUIRED")
                raw_items.append(obj)
        else:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                raw_items = list(parsed)
            elif isinstance(parsed, Mapping):
                for list_key in ("records", "tenures", "items", "youth_national_tenures"):
                    if isinstance(parsed.get(list_key), list):
                        raw_items = list(parsed[list_key])
                        break
                else:
                    raw_items = [parsed]
            else:
                raise YouthTenureError("YOUTH_TENURES_JSON_INVALID")
    elif isinstance(path_or_records, Sequence) and not isinstance(path_or_records, (bytes, bytearray)):
        raw_items = list(path_or_records)
    else:
        raise YouthTenureError("YOUTH_TENURES_INPUT_INVALID")

    return [validate_youth_national_tenure(item) for item in raw_items]


def project_youth_tenures_for_person(
    records: Sequence[Mapping[str, Any]],
    person_uid: str,
    *,
    visibility: str = "public_safe",
) -> List[Dict[str, Any]]:
    """Filter and project 4-state youth national tenures for a single person."""
    if visibility not in ("public_safe", "internal_full"):
        raise YouthTenureError(f"INVALID_VISIBILITY:{visibility}")
    try:
        clean_uid = validate_person_uid(person_uid)
    except ActorError as exc:
        raise YouthTenureError(f"INVALID_PERSON_UID:{exc}") from exc

    projected: List[Dict[str, Any]] = []
    for raw in records or ():
        rec = validate_youth_national_tenure(raw)
        if rec["person_uid"] != clean_uid:
            continue
        stage_zh = YOUTH_STAGE_LABELS_ZH[rec["participation_stage"]]
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
                "competition_or_program": rec["competition_or_program"],
                "season_or_year": rec["season_or_year"],
                "participation_stage": rec["participation_stage"],
                "participation_stage_label_zh": stage_zh,
                "team_or_unit": rec["team_or_unit"],
                "stats_summary": rec["stats_summary"],
                "evidence_refs": pub_refs,
            })
        else:
            projected.append({
                **rec,
                "participation_stage_label_zh": stage_zh,
            })

    projected.sort(
        key=lambda item: (
            item["season_or_year"],
            item["competition_or_program"],
            item["participation_stage"],
            item["team_or_unit"],
        )
    )
    return projected
