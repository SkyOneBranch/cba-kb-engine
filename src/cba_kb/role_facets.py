"""Three-facet coaching role representation for cba-kb-engine v2.4.0.

Separates:
- `league_listed_role`: official CBA league registration slot (e.g. from `team_staff_snapshots.jsonl`)
- `club_appointed_role`: club-announced operational coaching appointment
- `gameday_acting_role`: gameday acting bench role (defaults to `OBSERVED_EMPTY`)

Automatically flags `LEAGUE_VS_CLUB_ROLE_DISCREPANCY` when league-registered
and club-appointed roles are both observed and differ.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .player_identity import normalize_name

OBSERVED_EMPTY = "OBSERVED_EMPTY"
INSTITUTIONAL_DISCREPANCY_CODE = "LEAGUE_VS_CLUB_ROLE_DISCREPANCY"


def _normalize_role(value: Optional[str]) -> str:
    if value is None:
        return OBSERVED_EMPTY
    text = str(value).strip()
    return text if text else OBSERVED_EMPTY


def _safe_normalize_name(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return normalize_name(value)
    except Exception:
        return value.strip()


@dataclass(frozen=True)
class CoachRoleFacets:
    person_uid: Optional[str]
    person_name: str
    organization_id: Optional[str]
    season: str
    league_listed_role: str
    club_appointed_role: str
    gameday_acting_role: str = OBSERVED_EMPTY
    has_institutional_discrepancy: bool = False
    discrepancy_code: Optional[str] = None
    discrepancy_explanation: Optional[str] = None
    evidence_refs: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "person_uid": self.person_uid,
            "person_name": self.person_name,
            "organization_id": self.organization_id,
            "season": self.season,
            "league_listed_role": self.league_listed_role,
            "club_appointed_role": self.club_appointed_role,
            "gameday_acting_role": self.gameday_acting_role,
            "has_institutional_discrepancy": self.has_institutional_discrepancy,
            "discrepancy_code": self.discrepancy_code,
            "discrepancy_explanation": self.discrepancy_explanation,
            "evidence_refs": list(self.evidence_refs),
        }


def _select_matching_snapshot(
    staff_snapshots: Sequence[Mapping[str, Any]],
    *,
    person_name: str,
    season: str,
    person_uid: Optional[str],
    organization_id: Optional[str],
) -> Optional[Mapping[str, Any]]:
    norm_target_name = _safe_normalize_name(person_name)
    matches: List[Mapping[str, Any]] = []

    for snap in staff_snapshots:
        if not isinstance(snap, Mapping):
            continue
        snap_season = snap.get("season")
        if snap_season is not None and str(snap_season).strip() != season:
            continue
        snap_org = snap.get("organization_id")
        if organization_id and snap_org and str(snap_org).strip() != organization_id:
            continue

        snap_uid = snap.get("person_uid")
        if person_uid:
            if not snap_uid or str(snap_uid).strip() != person_uid:
                continue
            matches.append(snap)
            continue

        snap_name = _safe_normalize_name(snap.get("person_name") or snap.get("name"))
        if norm_target_name and snap_name == norm_target_name:
            matches.append(snap)

    if not matches:
        return None
    matches.sort(key=lambda item: str(item.get("captured_at") or ""))
    return matches[-1]


def build_coach_role_facets(
    *,
    person_name: str,
    season: str,
    person_uid: Optional[str] = None,
    organization_id: Optional[str] = None,
    staff_snapshots: Sequence[Mapping[str, Any]] = (),
    league_listed_role: Optional[str] = None,
    club_appointed_role: Optional[str] = None,
    gameday_acting_role: Optional[str] = None,
    evidence_refs: Sequence[str] = (),
) -> CoachRoleFacets:
    """Build 3-facet `CoachRoleFacets` with automatic institutional discrepancy detection."""
    refs: List[str] = []
    seen_refs: set[str] = set()
    for ref in evidence_refs:
        if isinstance(ref, str) and ref.strip() and ref.strip() not in seen_refs:
            seen_refs.add(ref.strip())
            refs.append(ref.strip())

    resolved_league_role = league_listed_role
    if _normalize_role(resolved_league_role) == OBSERVED_EMPTY and staff_snapshots:
        matched = _select_matching_snapshot(
            staff_snapshots,
            person_name=person_name,
            season=season,
            person_uid=person_uid,
            organization_id=organization_id,
        )
        if matched is not None:
            resolved_league_role = matched.get("league_listed_role") or matched.get("role")
            for key in ("source_url", "evidence_ref"):
                val = matched.get(key)
                if isinstance(val, str) and val.strip() and val.strip() not in seen_refs:
                    seen_refs.add(val.strip())
                    refs.append(val.strip())

    norm_league = _normalize_role(resolved_league_role)
    norm_club = _normalize_role(club_appointed_role)
    norm_gameday = _normalize_role(gameday_acting_role)

    has_discrepancy = (
        norm_league != OBSERVED_EMPTY
        and norm_club != OBSERVED_EMPTY
        and norm_league != norm_club
    )
    if has_discrepancy:
        discrepancy_code: Optional[str] = INSTITUTIONAL_DISCREPANCY_CODE
        discrepancy_explanation: Optional[str] = (
            f"League registration lists role as {norm_league}, whereas club appointment "
            f"lists role as {norm_club}; this reflects the institutional governance "
            f"distinction between league-registered coaching qualification slots and "
            f"club-appointed operational duties."
        )
    else:
        discrepancy_code = None
        discrepancy_explanation = None

    return CoachRoleFacets(
        person_uid=person_uid.strip() if isinstance(person_uid, str) and person_uid.strip() else None,
        person_name=str(person_name).strip(),
        organization_id=(
            organization_id.strip()
            if isinstance(organization_id, str) and organization_id.strip()
            else None
        ),
        season=str(season).strip(),
        league_listed_role=norm_league,
        club_appointed_role=norm_club,
        gameday_acting_role=norm_gameday,
        has_institutional_discrepancy=has_discrepancy,
        discrepancy_code=discrepancy_code,
        discrepancy_explanation=discrepancy_explanation,
        evidence_refs=tuple(refs),
    )
