"""Lifetime Person Registry (`cba-kb.person-registry.v1`) for cba-kb-engine v2.4.0.

Provides:
- Lifetime `PERS-*` identity profiles unifying player-to-coach career lifecycles.
- Fail-closed homonym resolution (`UNDECIDED` / `unresolved`) when no explicit
  `player_uid` or `record_links` (`link_status == "same"`) is present.
- Strict schema and uniqueness validation (`PersonRegistryError`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .actor import ActorError, validate_person_uid
from .player_identity import normalize_name

PERSON_REGISTRY_SCHEMA = "cba-kb.person-registry.v1"
PERSON_BIO_PROFILES_SCHEMA = "cba-kb.person-bio-profiles.v1"
PERSON_UID_RE = re.compile(r"PERS-[A-Za-z0-9_-]{8,64}\Z")

PLAYER_ROLES = frozenset({"PLAYER"})
COACH_ROLES = frozenset({
    "HEAD_COACH",
    "ASSISTANT_COACH",
    "ACTING_HEAD_COACH",
    "COACH",
})


class PersonRegistryError(ValueError):
    """Raised when a PersonRegistry or PersonProfile violates schema/uniqueness invariants."""


def _clean_required_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise PersonRegistryError(f"{label}_REQUIRED")
    return value.strip()


def _clean_optional_str(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or "\x00" in value:
        raise PersonRegistryError(f"{label}_INVALID")
    stripped = value.strip()
    return stripped or None


def _clean_str_tuple(values: Any, label: str) -> Tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise PersonRegistryError(f"{label}_SEQUENCE_REQUIRED")
    cleaned: List[str] = []
    for item in values:
        cleaned.append(_clean_required_str(item, f"{label}_ITEM"))
    return tuple(cleaned)


def _validate_pers_uid(value: Any) -> str:
    try:
        return validate_person_uid(value)
    except ActorError as exc:
        raise PersonRegistryError(f"INVALID_PERSON_UID:{exc}") from exc


@dataclass(frozen=True)
class RoleTenure:
    role: str
    organization_id: Optional[str] = None
    organization_name: Optional[str] = None
    season_start: Optional[str] = None
    season_end: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    authority_scope: str = "LEAGUE_OFFICIAL"
    evidence_refs: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", _clean_required_str(self.role, "ROLE"))
        object.__setattr__(
            self, "organization_id", _clean_optional_str(self.organization_id, "ORGANIZATION_ID")
        )
        object.__setattr__(
            self,
            "organization_name",
            _clean_optional_str(self.organization_name, "ORGANIZATION_NAME"),
        )
        object.__setattr__(
            self, "season_start", _clean_optional_str(self.season_start, "SEASON_START")
        )
        object.__setattr__(self, "season_end", _clean_optional_str(self.season_end, "SEASON_END"))
        object.__setattr__(self, "start_date", _clean_optional_str(self.start_date, "START_DATE"))
        object.__setattr__(self, "end_date", _clean_optional_str(self.end_date, "END_DATE"))
        object.__setattr__(
            self,
            "authority_scope",
            _clean_required_str(self.authority_scope, "AUTHORITY_SCOPE"),
        )
        object.__setattr__(
            self, "evidence_refs", _clean_str_tuple(self.evidence_refs, "EVIDENCE_REFS")
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "organization_id": self.organization_id,
            "organization_name": self.organization_name,
            "season_start": self.season_start,
            "season_end": self.season_end,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "authority_scope": self.authority_scope,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class PersonProfile:
    person_uid: str
    canonical_name: str
    aliases: Tuple[str, ...] = ()
    linked_player_uids: Tuple[str, ...] = ()
    role_tenures: Tuple[RoleTenure, ...] = ()
    record_links: Tuple[Dict[str, Any], ...] = ()
    birth_year: Optional[int] = None
    status: str = "ACTIVE"

    def __post_init__(self) -> None:
        object.__setattr__(self, "person_uid", _validate_pers_uid(self.person_uid))
        object.__setattr__(
            self, "canonical_name", _clean_required_str(self.canonical_name, "CANONICAL_NAME")
        )
        object.__setattr__(self, "aliases", _clean_str_tuple(self.aliases, "ALIASES"))
        object.__setattr__(
            self,
            "linked_player_uids",
            _clean_str_tuple(self.linked_player_uids, "LINKED_PLAYER_UIDS"),
        )
        if isinstance(self.role_tenures, (str, bytes)) or not isinstance(
            self.role_tenures, Sequence
        ):
            raise PersonRegistryError("ROLE_TENURES_SEQUENCE_REQUIRED")
        normalized_tenures: List[RoleTenure] = []
        for item in self.role_tenures:
            if isinstance(item, RoleTenure):
                normalized_tenures.append(item)
            elif isinstance(item, Mapping):
                normalized_tenures.append(_tenure_from_mapping(item))
            else:
                raise PersonRegistryError("ROLE_TENURE_OBJECT_REQUIRED")
        object.__setattr__(self, "role_tenures", tuple(normalized_tenures))

        if isinstance(self.record_links, (str, bytes)) or not isinstance(
            self.record_links, Sequence
        ):
            raise PersonRegistryError("RECORD_LINKS_SEQUENCE_REQUIRED")
        normalized_links: List[Dict[str, Any]] = []
        for link in self.record_links:
            if not isinstance(link, Mapping):
                raise PersonRegistryError("RECORD_LINK_OBJECT_REQUIRED")
            normalized_links.append(dict(link))
        object.__setattr__(self, "record_links", tuple(normalized_links))

        if self.birth_year is not None and (
            isinstance(self.birth_year, bool) or not isinstance(self.birth_year, int)
        ):
            raise PersonRegistryError("BIRTH_YEAR_INT_REQUIRED")
        object.__setattr__(self, "status", _clean_required_str(self.status, "STATUS"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "person_uid": self.person_uid,
            "canonical_name": self.canonical_name,
            "aliases": list(self.aliases),
            "linked_player_uids": list(self.linked_player_uids),
            "role_tenures": [t.to_dict() for t in self.role_tenures],
            "record_links": [dict(link) for link in self.record_links],
            "birth_year": self.birth_year,
            "status": self.status,
        }


def _tenure_from_mapping(item: Mapping[str, Any]) -> RoleTenure:
    return RoleTenure(
        role=item.get("role", ""),
        organization_id=item.get("organization_id"),
        organization_name=item.get("organization_name"),
        season_start=item.get("season_start"),
        season_end=item.get("season_end"),
        start_date=item.get("start_date"),
        end_date=item.get("end_date"),
        authority_scope=item.get("authority_scope", "LEAGUE_OFFICIAL"),
        evidence_refs=tuple(item.get("evidence_refs") or ()),
    )


def _normalize_person_entry(entry: PersonProfile | Mapping[str, Any]) -> Dict[str, Any]:
    if isinstance(entry, PersonProfile):
        return entry.to_dict()
    if not isinstance(entry, Mapping):
        raise PersonRegistryError("PERSON_OBJECT_REQUIRED")
    profile = PersonProfile(
        person_uid=entry.get("person_uid", ""),
        canonical_name=entry.get("canonical_name", ""),
        aliases=tuple(entry.get("aliases") or ()),
        linked_player_uids=tuple(entry.get("linked_player_uids") or ()),
        role_tenures=tuple(entry.get("role_tenures") or ()),
        record_links=tuple(entry.get("record_links") or ()),
        birth_year=entry.get("birth_year"),
        status=entry.get("status", "ACTIVE"),
    )
    return profile.to_dict()


def make_person_registry(
    persons: Sequence[PersonProfile | Mapping[str, Any]] = (),
    *,
    schema: str = PERSON_REGISTRY_SCHEMA,
) -> Dict[str, Any]:
    """Build and validate a `cba-kb.person-registry.v1` dictionary."""
    if isinstance(persons, (str, bytes)) or not isinstance(persons, Sequence):
        raise PersonRegistryError("PERSONS_SEQUENCE_REQUIRED")
    raw_persons = [
        p.to_dict() if isinstance(p, PersonProfile) else dict(p)
        if isinstance(p, Mapping)
        else p
        for p in persons
    ]
    return validate_person_registry({"schema": schema, "persons": raw_persons})


def validate_person_registry(registry: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate person registry schema, unique `PERS-*` IDs, and unique linked player UIDs."""
    if not isinstance(registry, Mapping):
        raise PersonRegistryError("PERSON_REGISTRY_OBJECT_REQUIRED")

    schema = registry.get("schema")
    if schema != PERSON_REGISTRY_SCHEMA:
        raise PersonRegistryError(f"INVALID_PERSON_REGISTRY_SCHEMA:{schema}")

    persons = registry.get("persons")
    if isinstance(persons, (str, bytes)) or not isinstance(persons, Sequence):
        raise PersonRegistryError("PERSONS_SEQUENCE_REQUIRED")

    seen_person_uids: set[str] = set()
    seen_linked_player_uids: Dict[str, str] = {}
    normalized_persons: List[Dict[str, Any]] = []

    for entry in persons:
        person = _normalize_person_entry(entry)
        puid = person["person_uid"]
        if puid in seen_person_uids:
            raise PersonRegistryError(f"DUPLICATE_PERSON_UID:{puid}")
        seen_person_uids.add(puid)

        for linked_uid in person["linked_player_uids"]:
            owner = seen_linked_player_uids.get(linked_uid)
            if owner is not None and owner != puid:
                raise PersonRegistryError(f"DUPLICATE_LINKED_PLAYER_UID:{linked_uid}")
            seen_linked_player_uids[linked_uid] = puid

        normalized_persons.append(person)

    return {
        "schema": PERSON_REGISTRY_SCHEMA,
        "persons": normalized_persons,
    }


def _resolved_result(person: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return {
        "status": "RESOLVED",
        "actor_kind": "person",
        "person_uid": person["person_uid"],
        "person": person,
        "candidates": [person],
        "reason": reason,
    }


def _undecided_result(candidates: List[Dict[str, Any]], reason: str) -> Dict[str, Any]:
    return {
        "status": "UNDECIDED",
        "actor_kind": "unresolved",
        "person_uid": None,
        "person": None,
        "candidates": candidates,
        "reason": reason,
    }


def _not_found_result(reason: str) -> Dict[str, Any]:
    return {
        "status": "NOT_FOUND",
        "actor_kind": "unresolved",
        "person_uid": None,
        "person": None,
        "candidates": [],
        "reason": reason,
    }


def resolve_person_identity(
    query: str,
    *,
    person_registry: Mapping[str, Any],
    player_uid: Optional[str] = None,
    record_key: Optional[str] = None,
    organization_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve a query to a lifetime `PERS-*` identity with fail-closed homonym handling.

    Priority:
    1. Exact `person_uid` match.
    2. Exact `player_uid` match in `linked_player_uids`.
    3. Exact `record_key` match in explicit `record_links` (`link_status == "same"`).
    4. Exact normalized name/alias match (unique -> RESOLVED; >=2 -> UNDECIDED).
    5. Otherwise NOT_FOUND.
    """
    validated = validate_person_registry(person_registry)
    persons = validated["persons"]

    query_stripped = query.strip() if isinstance(query, str) else ""

    # 1. Exact person_uid match
    if query_stripped:
        for person in persons:
            if person["person_uid"] == query_stripped:
                return _resolved_result(person, "EXACT_PERSON_UID")

    # 2. Exact player_uid match in linked_player_uids
    if isinstance(player_uid, str) and player_uid.strip():
        target_player_uid = player_uid.strip()
        for person in persons:
            if target_player_uid in person["linked_player_uids"]:
                return _resolved_result(person, "EXACT_LINKED_PLAYER_UID")

    # 3. Exact record_key match in explicit record_links (link_status == "same")
    if isinstance(record_key, str) and record_key.strip():
        target_record_key = record_key.strip()
        link_hits = [
            person
            for person in persons
            if any(
                link.get("record_key") == target_record_key
                and link.get("link_status") == "same"
                for link in person.get("record_links", [])
            )
        ]
        if len(link_hits) == 1:
            return _resolved_result(link_hits[0], "EXACT_RECORD_LINK")
        if len(link_hits) > 1:
            return _undecided_result(link_hits, "AMBIGUOUS_RECORD_LINK")

    # 4. Name/alias match (never guess across homonyms without explicit link)
    if query_stripped:
        normalized_query = normalize_name(query_stripped)
        name_hits: List[Dict[str, Any]] = []
        for person in persons:
            names = {normalize_name(person["canonical_name"])}
            for alias in person.get("aliases", []):
                names.add(normalize_name(alias))
            if normalized_query in names:
                name_hits.append(person)

        if len(name_hits) == 1:
            return _resolved_result(name_hits[0], "UNIQUE_NAME_OR_ALIAS")
        if len(name_hits) >= 2:
            # Fail-closed even if organization_id is supplied: homonyms require
            # explicit player_uid or record_links.
            _ = organization_id
            return _undecided_result(name_hits, "HOMONYM_WITHOUT_EXPLICIT_LINK")

    # 5. Not found
    return _not_found_result("NO_MATCH")


def _tenure_sort_key(tenure: Mapping[str, Any]) -> Tuple[str, str, str]:
    start = str(tenure.get("start_date") or tenure.get("season_start") or "9999")
    end = str(tenure.get("end_date") or tenure.get("season_end") or "9999")
    role = str(tenure.get("role") or "")
    return (start, end, role)


def unify_player_and_coach_profile(
    person_uid: str,
    *,
    person_registry: Mapping[str, Any],
) -> Dict[str, Any]:
    """Return a chronologically sorted lifetime view unifying player and coach tenures."""
    validated_uid = _validate_pers_uid(person_uid)
    validated = validate_person_registry(person_registry)

    target: Optional[Dict[str, Any]] = None
    for person in validated["persons"]:
        if person["person_uid"] == validated_uid:
            target = person
            break

    if target is None:
        raise PersonRegistryError(f"PERSON_NOT_FOUND:{validated_uid}")

    sorted_tenures = sorted(target["role_tenures"], key=_tenure_sort_key)
    player_tenures = [t for t in sorted_tenures if str(t.get("role", "")).upper() in PLAYER_ROLES]
    coach_tenures = [t for t in sorted_tenures if str(t.get("role", "")).upper() in COACH_ROLES]

    has_player_tenure = bool(player_tenures)
    has_coach_tenure = bool(coach_tenures)

    is_player_to_coach_transition = False
    if has_player_tenure and has_coach_tenure:
        earliest_player = _tenure_sort_key(player_tenures[0])[0]
        latest_coach = _tenure_sort_key(coach_tenures[-1])[0]
        is_player_to_coach_transition = earliest_player <= latest_coach

    return {
        "person_uid": target["person_uid"],
        "canonical_name": target["canonical_name"],
        "aliases": list(target["aliases"]),
        "linked_player_uids": list(target["linked_player_uids"]),
        "role_tenures": sorted_tenures,
        "has_player_tenure": has_player_tenure,
        "has_coach_tenure": has_coach_tenure,
        "is_player_to_coach_transition": is_player_to_coach_transition,
        "status": target["status"],
    }


def _clean_positive_optional_int(value: Any, label: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PersonRegistryError(f"{label}_POSITIVE_INT_REQUIRED")
    return value


def _clean_evidence_refs_tuple(values: Any, label: str) -> Tuple[Any, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise PersonRegistryError(f"{label}_SEQUENCE_REQUIRED")
    cleaned: List[Any] = []
    for item in values:
        if isinstance(item, str):
            cleaned.append(_clean_required_str(item, f"{label}_ITEM"))
        elif isinstance(item, Mapping):
            cleaned.append(dict(item))
        else:
            raise PersonRegistryError(f"{label}_ITEM_INVALID")
    return tuple(cleaned)


@dataclass(frozen=True)
class PersonBioProfile:
    person_uid: str
    canonical_name: str
    english_name: Optional[str] = None
    birth_date: Optional[str] = None
    birth_place: Optional[str] = None
    nationality: Optional[str] = None
    height_cm: Optional[int] = None
    weight_kg: Optional[int] = None
    position: Optional[str] = None
    jersey_number: Optional[str] = None
    current_club: Optional[str] = None
    current_role: Optional[str] = None
    draft_or_youth_origin: Optional[str] = None
    honors: Tuple[str, ...] = ()
    evidence_refs: Tuple[Any, ...] = ()
    review_status: str = "VERIFIED"

    def __post_init__(self) -> None:
        object.__setattr__(self, "person_uid", _validate_pers_uid(self.person_uid))
        object.__setattr__(
            self, "canonical_name", _clean_required_str(self.canonical_name, "CANONICAL_NAME")
        )
        object.__setattr__(
            self, "english_name", _clean_optional_str(self.english_name, "ENGLISH_NAME")
        )
        object.__setattr__(
            self, "birth_date", _clean_optional_str(self.birth_date, "BIRTH_DATE")
        )
        object.__setattr__(
            self, "birth_place", _clean_optional_str(self.birth_place, "BIRTH_PLACE")
        )
        object.__setattr__(
            self, "nationality", _clean_optional_str(self.nationality, "NATIONALITY")
        )
        object.__setattr__(
            self, "height_cm", _clean_positive_optional_int(self.height_cm, "HEIGHT_CM")
        )
        object.__setattr__(
            self, "weight_kg", _clean_positive_optional_int(self.weight_kg, "WEIGHT_KG")
        )
        object.__setattr__(self, "position", _clean_optional_str(self.position, "POSITION"))
        object.__setattr__(
            self, "jersey_number", _clean_optional_str(self.jersey_number, "JERSEY_NUMBER")
        )
        object.__setattr__(
            self, "current_club", _clean_optional_str(self.current_club, "CURRENT_CLUB")
        )
        object.__setattr__(
            self, "current_role", _clean_optional_str(self.current_role, "CURRENT_ROLE")
        )
        object.__setattr__(
            self,
            "draft_or_youth_origin",
            _clean_optional_str(self.draft_or_youth_origin, "DRAFT_OR_YOUTH_ORIGIN"),
        )
        object.__setattr__(self, "honors", _clean_str_tuple(self.honors, "HONORS"))
        object.__setattr__(
            self, "evidence_refs", _clean_evidence_refs_tuple(self.evidence_refs, "EVIDENCE_REFS")
        )
        object.__setattr__(
            self, "review_status", _clean_required_str(self.review_status, "REVIEW_STATUS")
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "person_uid": self.person_uid,
            "canonical_name": self.canonical_name,
            "english_name": self.english_name,
            "birth_date": self.birth_date,
            "birth_place": self.birth_place,
            "nationality": self.nationality,
            "height_cm": self.height_cm,
            "weight_kg": self.weight_kg,
            "position": self.position,
            "jersey_number": self.jersey_number,
            "current_club": self.current_club,
            "current_role": self.current_role,
            "draft_or_youth_origin": self.draft_or_youth_origin,
            "honors": list(self.honors),
            "evidence_refs": [
                dict(ref) if isinstance(ref, Mapping) else ref for ref in self.evidence_refs
            ],
            "review_status": self.review_status,
        }


def _normalize_bio_profile_entry(entry: PersonBioProfile | Mapping[str, Any]) -> Dict[str, Any]:
    if isinstance(entry, PersonBioProfile):
        return entry.to_dict()
    if not isinstance(entry, Mapping):
        raise PersonRegistryError("BIO_PROFILE_OBJECT_REQUIRED")
    profile = PersonBioProfile(
        person_uid=entry.get("person_uid", ""),
        canonical_name=entry.get("canonical_name", ""),
        english_name=entry.get("english_name"),
        birth_date=entry.get("birth_date"),
        birth_place=entry.get("birth_place"),
        nationality=entry.get("nationality"),
        height_cm=entry.get("height_cm"),
        weight_kg=entry.get("weight_kg"),
        position=entry.get("position"),
        jersey_number=entry.get("jersey_number"),
        current_club=entry.get("current_club"),
        current_role=entry.get("current_role"),
        draft_or_youth_origin=entry.get("draft_or_youth_origin"),
        honors=tuple(entry.get("honors") or ()),
        evidence_refs=tuple(entry.get("evidence_refs") or ()),
        review_status=entry.get("review_status", "VERIFIED"),
    )
    return profile.to_dict()


def make_person_bio_profiles(
    profiles: Sequence[PersonBioProfile | Mapping[str, Any]] = (),
    *,
    schema: str = PERSON_BIO_PROFILES_SCHEMA,
) -> Dict[str, Any]:
    """Build and validate a `cba-kb.person-bio-profiles.v1` dictionary."""
    if isinstance(profiles, (str, bytes)) or not isinstance(profiles, Sequence):
        raise PersonRegistryError("BIO_PROFILES_SEQUENCE_REQUIRED")
    raw_profiles = [
        p.to_dict() if isinstance(p, PersonBioProfile) else dict(p)
        if isinstance(p, Mapping)
        else p
        for p in profiles
    ]
    return validate_person_bio_profiles({"schema": schema, "profiles": raw_profiles})


def validate_person_bio_profiles(bundle: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate person bio profiles schema and unique `PERS-*` person_uid entries."""
    if not isinstance(bundle, Mapping):
        raise PersonRegistryError("PERSON_BIO_PROFILES_OBJECT_REQUIRED")

    schema = bundle.get("schema")
    if schema != PERSON_BIO_PROFILES_SCHEMA:
        raise PersonRegistryError(f"INVALID_PERSON_BIO_PROFILES_SCHEMA:{schema}")

    profiles = bundle.get("profiles")
    if isinstance(profiles, (str, bytes)) or not isinstance(profiles, Sequence):
        raise PersonRegistryError("BIO_PROFILES_SEQUENCE_REQUIRED")

    seen_person_uids: set[str] = set()
    normalized_profiles: List[Dict[str, Any]] = []
    for entry in profiles:
        profile = _normalize_bio_profile_entry(entry)
        puid = profile["person_uid"]
        if puid in seen_person_uids:
            raise PersonRegistryError(f"DUPLICATE_BIO_PROFILE_PERSON_UID:{puid}")
        seen_person_uids.add(puid)
        normalized_profiles.append(profile)

    return {
        "schema": PERSON_BIO_PROFILES_SCHEMA,
        "profiles": normalized_profiles,
    }
