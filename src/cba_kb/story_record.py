"""StoryRecord (`cba-kb.story-record.v1`) and OrganizationProfile (`cba-kb.organization.v1`).

Provides:
- Deterministic `story_<24hex>` IDs decoupled from publication timestamps.
- Explicit decoupling of `occurred_season` vs `published_at`.
- 1-source-to-N-targets projection (`project_stories_for_entity`) across
  participants (`entity_kind="person"`) and clubs (`entity_kind="organization"`).
- Strict `public_safe` stripping of `internal_verbatim_or_note` and exclusion
  of non-public or `unreferenced_encyclopedia` stories.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .actor import ActorError, validate_person_uid

STORY_RECORD_SCHEMA = "cba-kb.story-record.v1"
ORGANIZATION_PROFILE_SCHEMA = "cba-kb.organization.v1"
PARTICIPANT_ROLES = frozenset({
    "ACTION_INITIATOR",
    "SUBJECT_INTERVIEWEE",
    "CHRONICLER",
    "CO_PARTICIPANT",
})
STORY_ID_RE = re.compile(r"story_[0-9a-f]{24}\Z")


class StoryRecordError(ValueError):
    """Raised when a StoryRecord, OrganizationProfile, or projection request is invalid."""


def _required_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise StoryRecordError(f"{label}_REQUIRED")
    return value.strip()


def _optional_str(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or "\x00" in value:
        raise StoryRecordError(f"{label}_INVALID")
    stripped = value.strip()
    return stripped or None


@dataclass(frozen=True)
class OrganizationProfile:
    organization_id: str
    canonical_name: str
    short_name: Optional[str] = None
    aliases: Tuple[str, ...] = ()
    league: str = "CBA"
    city: Optional[str] = None
    schema: str = ORGANIZATION_PROFILE_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "organization_id", _required_str(self.organization_id, "ORGANIZATION_ID")
        )
        object.__setattr__(
            self, "canonical_name", _required_str(self.canonical_name, "CANONICAL_NAME")
        )
        object.__setattr__(self, "short_name", _optional_str(self.short_name, "SHORT_NAME"))
        if isinstance(self.aliases, (str, bytes)) or not isinstance(self.aliases, Sequence):
            raise StoryRecordError("ALIASES_SEQUENCE_REQUIRED")
        object.__setattr__(
            self,
            "aliases",
            tuple(_required_str(alias, "ALIAS") for alias in self.aliases),
        )
        object.__setattr__(self, "league", _required_str(self.league, "LEAGUE"))
        object.__setattr__(self, "city", _optional_str(self.city, "CITY"))
        if self.schema != ORGANIZATION_PROFILE_SCHEMA:
            raise StoryRecordError(f"INVALID_ORGANIZATION_SCHEMA:{self.schema}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "organization_id": self.organization_id,
            "canonical_name": self.canonical_name,
            "short_name": self.short_name,
            "aliases": list(self.aliases),
            "league": self.league,
            "city": self.city,
        }


def generate_story_id(
    *,
    title: str,
    occurred_season: str,
    canonical_source: Mapping[str, Any] | str,
) -> str:
    """Deterministically generate `story_<24hex>` from title, occurred_season, and canonical_source."""
    clean_title = _required_str(title, "TITLE")
    clean_season = _required_str(occurred_season, "OCCURRED_SEASON")

    if isinstance(canonical_source, Mapping):
        source_key = (
            canonical_source.get("source_id")
            or canonical_source.get("url")
            or canonical_source.get("title")
            or canonical_source
        )
    elif isinstance(canonical_source, str) and canonical_source.strip():
        source_key = canonical_source.strip()
    else:
        raise StoryRecordError("CANONICAL_SOURCE_REQUIRED")

    payload = json.dumps(
        {
            "title": clean_title,
            "occurred_season": clean_season,
            "canonical_source": source_key,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"story_{hashlib.sha256(payload).hexdigest()[:24]}"


def _validate_participant(participant: Any) -> Dict[str, Any]:
    if not isinstance(participant, Mapping):
        raise StoryRecordError("PARTICIPANT_OBJECT_REQUIRED")

    raw_uid = participant.get("person_uid")
    try:
        person_uid = validate_person_uid(raw_uid)
    except ActorError as exc:
        raise StoryRecordError(f"INVALID_PARTICIPANT_PERSON_UID:{exc}") from exc

    display_name = _required_str(participant.get("display_name"), "PARTICIPANT_DISPLAY_NAME")
    role = _required_str(participant.get("role"), "PARTICIPANT_ROLE")
    if role not in PARTICIPANT_ROLES:
        raise StoryRecordError(f"INVALID_PARTICIPANT_ROLE:{role}")

    if "internal_verbatim_or_note" not in participant:
        raise StoryRecordError("PARTICIPANT_INTERNAL_VERBATIM_OR_NOTE_REQUIRED")
    if "public_perspective_summary" not in participant:
        raise StoryRecordError("PARTICIPANT_PUBLIC_PERSPECTIVE_SUMMARY_REQUIRED")

    internal_note = _optional_str(
        participant.get("internal_verbatim_or_note"), "INTERNAL_VERBATIM_OR_NOTE"
    )
    public_summary = _optional_str(
        participant.get("public_perspective_summary"), "PUBLIC_PERSPECTIVE_SUMMARY"
    )

    return {
        "person_uid": person_uid,
        "display_name": display_name,
        "role": role,
        "internal_verbatim_or_note": internal_note,
        "public_perspective_summary": public_summary,
    }


def validate_story_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate and normalize a `cba-kb.story-record.v1` record."""
    if not isinstance(record, Mapping):
        raise StoryRecordError("STORY_RECORD_OBJECT_REQUIRED")

    schema = record.get("schema")
    if schema != STORY_RECORD_SCHEMA:
        raise StoryRecordError(f"INVALID_STORY_RECORD_SCHEMA:{schema}")

    story_id = _required_str(record.get("story_id"), "STORY_ID")
    if not STORY_ID_RE.fullmatch(story_id):
        raise StoryRecordError(f"INVALID_STORY_ID:{story_id}")

    title = _required_str(record.get("title"), "TITLE")
    occurred_season = _required_str(record.get("occurred_season"), "OCCURRED_SEASON")
    occurred_date = _optional_str(record.get("occurred_date"), "OCCURRED_DATE")
    published_at = _required_str(record.get("published_at"), "PUBLISHED_AT")

    canonical_source_raw = record.get("canonical_source")
    if isinstance(canonical_source_raw, Mapping):
        if not canonical_source_raw:
            raise StoryRecordError("CANONICAL_SOURCE_REQUIRED")
        canonical_source: Any = dict(canonical_source_raw)
    elif isinstance(canonical_source_raw, str) and canonical_source_raw.strip():
        canonical_source = canonical_source_raw.strip()
    else:
        raise StoryRecordError("CANONICAL_SOURCE_REQUIRED")

    reprints_raw = record.get("deduped_reprints")
    if isinstance(reprints_raw, (str, bytes)) or not isinstance(reprints_raw, Sequence):
        raise StoryRecordError("DEDUPED_REPRINTS_LIST_REQUIRED")
    deduped_reprints = [
        dict(item) if isinstance(item, Mapping) else item for item in reprints_raw
    ]

    participants_raw = record.get("participants")
    if (
        isinstance(participants_raw, (str, bytes))
        or not isinstance(participants_raw, Sequence)
        or len(participants_raw) == 0
    ):
        raise StoryRecordError("PARTICIPANTS_NON_EMPTY_LIST_REQUIRED")
    participants = [_validate_participant(p) for p in participants_raw]

    org_ids_raw = record.get("organization_ids")
    if isinstance(org_ids_raw, (str, bytes)) or not isinstance(org_ids_raw, Sequence):
        raise StoryRecordError("ORGANIZATION_IDS_LIST_REQUIRED")
    organization_ids = [_required_str(oid, "ORGANIZATION_ID") for oid in org_ids_raw]

    visibility_policy_raw = record.get("visibility_policy")
    if not isinstance(visibility_policy_raw, Mapping):
        raise StoryRecordError("VISIBILITY_POLICY_OBJECT_REQUIRED")
    visibility_policy = dict(visibility_policy_raw)

    return {
        "schema": STORY_RECORD_SCHEMA,
        "story_id": story_id,
        "title": title,
        "occurred_season": occurred_season,
        "occurred_date": occurred_date,
        "published_at": published_at,
        "canonical_source": canonical_source,
        "deduped_reprints": deduped_reprints,
        "participants": participants,
        "organization_ids": organization_ids,
        "visibility_policy": visibility_policy,
    }


def make_story_record(
    *,
    title: str,
    occurred_season: str,
    published_at: str,
    canonical_source: Mapping[str, Any] | str,
    participants: Sequence[Mapping[str, Any]],
    organization_ids: Sequence[str] = (),
    occurred_date: Optional[str] = None,
    deduped_reprints: Sequence[Any] = (),
    visibility_policy: Optional[Mapping[str, Any]] = None,
    story_id: Optional[str] = None,
    schema: str = STORY_RECORD_SCHEMA,
) -> Dict[str, Any]:
    """Construct and validate a `cba-kb.story-record.v1` record."""
    resolved_story_id = story_id or generate_story_id(
        title=title,
        occurred_season=occurred_season,
        canonical_source=canonical_source,
    )
    policy = (
        dict(visibility_policy)
        if visibility_policy is not None
        else {"public_export_allowed": False}
    )
    return validate_story_record({
        "schema": schema,
        "story_id": resolved_story_id,
        "title": title,
        "occurred_season": occurred_season,
        "occurred_date": occurred_date,
        "published_at": published_at,
        "canonical_source": canonical_source,
        "deduped_reprints": list(deduped_reprints),
        "participants": list(participants),
        "organization_ids": list(organization_ids),
        "visibility_policy": policy,
    })


def _is_public_export_allowed(story: Mapping[str, Any]) -> bool:
    policy = story.get("visibility_policy")
    if not isinstance(policy, Mapping) or policy.get("public_export_allowed") is not True:
        return False
    source = story.get("canonical_source")
    if isinstance(source, Mapping):
        if str(source.get("source_kind") or "").strip().lower() == "unreferenced_encyclopedia":
            return False
    return True


def _public_canonical_source(source: Any) -> Any:
    if not isinstance(source, Mapping):
        return source
    return {
        k: v
        for k, v in source.items()
        if k in {"source_id", "title", "url", "source_kind", "authority_scope"}
    }


def project_stories_for_entity(
    stories: Sequence[Mapping[str, Any]],
    *,
    entity_id: str,
    entity_kind: str = "person",
    visibility: str = "internal_full",
) -> List[Dict[str, Any]]:
    """Project `StoryRecord` entries onto a target person or organization."""
    if entity_kind not in ("person", "organization"):
        raise StoryRecordError(f"INVALID_ENTITY_KIND:{entity_kind}")
    if visibility not in ("internal_full", "public_safe"):
        raise StoryRecordError(f"INVALID_VISIBILITY:{visibility}")
    clean_entity_id = _required_str(entity_id, "ENTITY_ID")

    projected: List[Dict[str, Any]] = []
    for raw_story in stories:
        story = validate_story_record(raw_story)
        if visibility == "public_safe" and not _is_public_export_allowed(story):
            continue

        source_payload = (
            _public_canonical_source(story["canonical_source"])
            if visibility == "public_safe"
            else story["canonical_source"]
        )

        if entity_kind == "person":
            for participant in story["participants"]:
                if participant["person_uid"] != clean_entity_id:
                    continue
                entry: Dict[str, Any] = {
                    "story_id": story["story_id"],
                    "title": story["title"],
                    "occurred_season": story["occurred_season"],
                    "occurred_date": story["occurred_date"],
                    "published_at": story["published_at"],
                    "entity_id": clean_entity_id,
                    "entity_kind": "person",
                    "participant_role": participant["role"],
                    "display_name": participant["display_name"],
                    "summary": participant["public_perspective_summary"] or "",
                    "public_perspective_summary": participant["public_perspective_summary"],
                    "organization_ids": list(story["organization_ids"]),
                    "canonical_source": source_payload,
                    "deduped_reprints_count": len(story["deduped_reprints"]),
                    "visibility": visibility,
                }
                if visibility == "internal_full":
                    entry["internal_verbatim_or_note"] = participant["internal_verbatim_or_note"]
                projected.append(entry)
        else:
            if clean_entity_id not in story["organization_ids"]:
                continue
            projected_participants: List[Dict[str, Any]] = []
            summaries: List[str] = []
            for participant in story["participants"]:
                p_view: Dict[str, Any] = {
                    "person_uid": participant["person_uid"],
                    "display_name": participant["display_name"],
                    "role": participant["role"],
                    "public_perspective_summary": participant["public_perspective_summary"],
                }
                if visibility == "internal_full":
                    p_view["internal_verbatim_or_note"] = participant["internal_verbatim_or_note"]
                projected_participants.append(p_view)
                if participant["public_perspective_summary"]:
                    summaries.append(participant["public_perspective_summary"])

            projected.append({
                "story_id": story["story_id"],
                "title": story["title"],
                "occurred_season": story["occurred_season"],
                "occurred_date": story["occurred_date"],
                "published_at": story["published_at"],
                "entity_id": clean_entity_id,
                "entity_kind": "organization",
                "summary": " ".join(summaries),
                "participants": projected_participants,
                "organization_ids": list(story["organization_ids"]),
                "canonical_source": source_payload,
                "deduped_reprints_count": len(story["deduped_reprints"]),
                "visibility": visibility,
            })

    projected.sort(
        key=lambda item: (
            item["occurred_season"],
            item.get("occurred_date") or "",
            item["published_at"],
            item["story_id"],
        )
    )
    return projected
