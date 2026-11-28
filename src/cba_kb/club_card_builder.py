"""Club Panorama Card builder for CBA organizations (REQ-240 P0-2).

Strictly local, zero-network, zero-Drive-API:
- Aggregates multi-season domestic + foreign rosters, 3-facet coaching staff
  governance, and organization-projected StoryRecords into a single markdown card.
- Enforces validate_card_visibility() and verify_public_safe_card_text() under
  visibility="public_safe".
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .common import atomic
from .consumer_surface import scrub_raw_drive_ids_for_consumer
from .kc_render import (
    CitationRegistry,
    _safe_filename_part,
    _sanitize_public_free_text,
    validate_card_style,
)
from .kc_visibility import validate_card_visibility, verify_public_safe_card_text
from .role_facets import build_coach_role_facets


def _norm_token(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split()).casefold()


def club_card_consumer_filename(
    club_label: str,
    seasons_label: str,
    *,
    visibility: str = "internal_full",
) -> str:
    """Build self-describing Chinese consumer filename for club panorama cards."""
    vis = validate_card_visibility(visibility)
    safe_club = _safe_filename_part(club_label, "club_name")
    safe_seasons = _safe_filename_part(seasons_label, "seasons")
    if vis == "public_safe":
        return f"俱乐部全景卡_{safe_club}_{safe_seasons}_PUBLIC_CURRENT.md"
    return f"俱乐部全景卡_{safe_club}_{safe_seasons}_CURRENT.md"


def _normalize_org_profile(
    club_query: str,
    organization_profile: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    if organization_profile is None:
        raw: Dict[str, Any] = {}
    elif hasattr(organization_profile, "to_dict") and callable(organization_profile.to_dict):
        raw = dict(organization_profile.to_dict())
    elif isinstance(organization_profile, Mapping):
        raw = dict(organization_profile)
    else:
        raw = {}

    q = club_query.strip()
    org_id = str(raw.get("organization_id") or "").strip() or q
    canonical_name = str(raw.get("canonical_name") or "").strip() or q
    short_name = str(raw.get("short_name") or "").strip() or canonical_name
    aliases_raw = raw.get("aliases") or ()
    aliases = [str(a).strip() for a in aliases_raw if str(a).strip()]
    league = str(raw.get("league") or "CBA").strip() or "CBA"
    city = str(raw.get("city") or "").strip() or None
    return {
        "organization_id": org_id,
        "canonical_name": canonical_name,
        "short_name": short_name,
        "aliases": aliases,
        "league": league,
        "city": city,
    }


def _club_match_tokens(club_query: str, org: Mapping[str, Any]) -> set[str]:
    tokens = {
        _norm_token(club_query),
        _norm_token(org.get("organization_id")),
        _norm_token(org.get("canonical_name")),
        _norm_token(org.get("short_name")),
        *(_norm_token(a) for a in (org.get("aliases") or ())),
    }
    tokens.discard("")
    return tokens


def _row_matches_club(row: Mapping[str, Any], club_tokens: set[str]) -> bool:
    row_tokens = {
        _norm_token(row.get("organization_id")),
        _norm_token(row.get("club_official")),
        _norm_token(row.get("club")),
        _norm_token(row.get("club_id")),
        _norm_token(row.get("team_name")),
        _norm_token(row.get("team_id")),
    }
    row_tokens.discard("")
    if not row_tokens:
        return False
    return bool(row_tokens & club_tokens)


def _normalize_facet_item(item: Any) -> Optional[Dict[str, Any]]:
    if hasattr(item, "to_dict") and callable(item.to_dict):
        return dict(item.to_dict())
    if isinstance(item, Mapping):
        return dict(item)
    return None


def _project_org_stories(
    story_records: Sequence[Mapping[str, Any]],
    *,
    organization_id: str,
    visibility: str,
) -> List[Dict[str, Any]]:
    if not story_records:
        return []
    try:
        from .story_record import project_stories_for_entity

        return project_stories_for_entity(
            story_records,
            entity_id=organization_id,
            entity_kind="organization",
            visibility=visibility,
        )
    except ImportError:
        projected: List[Dict[str, Any]] = []
        is_public = visibility == "public_safe"
        for st in story_records:
            if not isinstance(st, Mapping):
                continue
            org_ids = [str(x).strip() for x in (st.get("organization_ids") or ())]
            if organization_id not in org_ids:
                continue
            src = st.get("canonical_source") if isinstance(st.get("canonical_source"), Mapping) else {}
            if is_public and str(src.get("source_kind") or "").strip().lower() == "unreferenced_encyclopedia":
                continue
            vis_pol = st.get("visibility_policy") if isinstance(st.get("visibility_policy"), Mapping) else {}
            if is_public and vis_pol.get("public_export_allowed") is False:
                continue
            org_summary = str(st.get("organization_summary") or "").strip()
            if org_summary:
                chosen_summary = org_summary
            else:
                parts = [p for p in (st.get("participants") or ()) if isinstance(p, Mapping)]
                chosen_summary = ""
                for preferred_role in ("ACTION_INITIATOR", "SUBJECT_INTERVIEWEE"):
                    for p in parts:
                        if str(p.get("role") or "").strip() == preferred_role:
                            cand = str(p.get("public_perspective_summary") or "").strip()
                            if cand:
                                chosen_summary = cand
                                break
                    if chosen_summary:
                        break
                if not chosen_summary:
                    for p in parts:
                        cand = str(p.get("public_perspective_summary") or "").strip()
                        if cand:
                            chosen_summary = cand
                            break
            projected.append({
                "story_id": st.get("story_id"),
                "title": st.get("title"),
                "occurred_season": st.get("occurred_season"),
                "occurred_date": st.get("occurred_date"),
                "published_at": st.get("published_at"),
                "canonical_source": src,
                "summary": chosen_summary or str(st.get("title") or ""),
            })
        return projected


CLUB_SEASON_ROSTER_LEDGER_SCHEMA = "cba-kb.club-season-roster-ledger.v1"

_LEDGER_BUCKET_MAP: Dict[str, str] = {
    "RETAINED": "retained",
    "RETAINED_DOMESTIC": "retained",
    "RETAINED_FOREIGN": "retained",
    "留队": "retained",
    "留队球员": "retained",
    "NEW_SIGNING_OR_YOUTH_PROMOTION": "joined",
    "JOINED": "joined",
    "NEW_SIGNING": "joined",
    "YOUTH_PROMOTION": "joined",
    "新援": "joined",
    "青训上调": "joined",
    "RETIRED_TO_CLUB_COACH": "retired_to_club_coach",
    "RETIRED_TO_COACH": "retired_to_club_coach",
    "退役转任本队教练": "retired_to_club_coach",
    "DEPARTED_OR_TRANSFERRED": "departed",
    "DEPARTED": "departed",
    "TRANSFERRED": "departed",
    "离队": "departed",
    "转会": "departed",
    "STAGED_FOREIGN_25_26": "staged_foreign_25_26",
    "STAGED_FOREIGN": "staged_foreign_25_26",
    "阶段性外援": "staged_foreign_25_26",
}


def build_club_panorama_card(
    club_query: str,
    *,
    seasons: Sequence[str] = ("2025-2026", "2026-2027"),
    organization_profile: Optional[Mapping[str, Any]] = None,
    master_rows: Optional[Sequence[Mapping[str, Any]]] = None,
    foreign_rows: Optional[Sequence[Mapping[str, Any]]] = None,
    staff_snapshots: Optional[Sequence[Mapping[str, Any]]] = None,
    coach_role_facets: Optional[Sequence[Any]] = None,
    story_records: Optional[Sequence[Mapping[str, Any]]] = None,
    roster_ledger: Optional[Sequence[Mapping[str, Any]]] = None,
    visibility: str = "internal_full",
    card_style: str = "classic",
    person_registry: Optional[Mapping[str, Any]] = None,
    output_dir: Optional[str | Path] = None,
    observed_until: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble and render a multi-season Club Panorama Card.

    Aggregates 2-season roster (domestic + foreign), 3-facet coaching staff,
    offseason roster changes, and organization-projected story timeline.
    """
    if not isinstance(club_query, str) or not club_query.strip():
        raise ValueError("CLUB_QUERY_REQUIRED")

    vis = validate_card_visibility(visibility)
    style = validate_card_style(card_style)
    is_public = vis == "public_safe"
    is_wiki_v241 = style == "wiki_v241"

    org = _normalize_org_profile(club_query, organization_profile)
    organization_id = org["organization_id"]
    club_name = org["canonical_name"]
    short_name = org["short_name"]
    club_tokens = _club_match_tokens(club_query, org)

    season_list = [str(s).strip() for s in (seasons or ()) if str(s).strip()]
    if not season_list:
        season_list = ["2025-2026", "2026-2027"]
    seasons_label = "_".join(season_list)

    # 1. Multi-season roster aggregation (domestic + foreign)
    roster_by_season: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        s: {"domestic": [], "foreign": []} for s in season_list
    }
    for row in master_rows or ():
        if not isinstance(row, Mapping):
            continue
        if not _row_matches_club(row, club_tokens):
            continue
        row_season = str(row.get("season") or "").strip()
        if row_season in roster_by_season:
            roster_by_season[row_season]["domestic"].append(dict(row))
        elif not row_season and season_list:
            roster_by_season[season_list[-1]]["domestic"].append(dict(row))

    for row in foreign_rows or ():
        if not isinstance(row, Mapping):
            continue
        if not _row_matches_club(row, club_tokens):
            continue
        row_season = str(row.get("season") or "").strip()
        if row_season in roster_by_season:
            roster_by_season[row_season]["foreign"].append(dict(row))
        elif not row_season and season_list:
            roster_by_season[season_list[-1]]["foreign"].append(dict(row))

    # 2. 3-facet coaching staff aggregation
    normalized_facets: List[Dict[str, Any]] = []
    seen_coach_keys: set[tuple[str, str]] = set()
    for raw_facet in coach_role_facets or ():
        fd = _normalize_facet_item(raw_facet)
        if not fd:
            continue
        f_org = _norm_token(fd.get("organization_id"))
        if f_org and f_org not in club_tokens:
            continue
        f_season = str(fd.get("season") or "").strip()
        if f_season and f_season not in season_list:
            continue
        key = (_norm_token(fd.get("person_uid") or fd.get("person_name")), f_season)
        seen_coach_keys.add(key)
        normalized_facets.append(fd)

    for snap in staff_snapshots or ():
        if not isinstance(snap, Mapping):
            continue
        s_org = _norm_token(snap.get("organization_id") or snap.get("club_official") or snap.get("club"))
        if s_org and s_org not in club_tokens:
            continue
        s_season = str(snap.get("season") or "").strip()
        if s_season and s_season not in season_list:
            continue
        pname = str(snap.get("person_name") or "").strip()
        puid = str(snap.get("person_uid") or "").strip() or None
        if not pname and not puid:
            continue
        eff_season = s_season or season_list[-1]
        key = (_norm_token(puid or pname), eff_season)
        if key in seen_coach_keys:
            continue
        seen_coach_keys.add(key)
        evidence_refs = [
            str(snap.get(k)).strip()
            for k in ("source_url", "evidence_ref")
            if isinstance(snap.get(k), str) and str(snap.get(k)).strip()
        ]
        facet_obj = build_coach_role_facets(
            person_uid=puid,
            person_name=pname or str(puid or ""),
            organization_id=organization_id,
            season=eff_season,
            league_listed_role=snap.get("league_listed_role") or snap.get("role"),
            club_appointed_role=snap.get("club_appointed_role"),
            gameday_acting_role=snap.get("gameday_acting_role"),
            evidence_refs=evidence_refs,
        )
        normalized_facets.append(facet_obj.to_dict())

    # 3. Organization-projected story timeline
    projected_stories = _project_org_stories(
        story_records or (),
        organization_id=organization_id,
        visibility=vis,
    )

    # 3b. Offseason roster changes & all_roster_members (REQ-241 P0-2 D-02)
    season_player_sets: Dict[str, set[str]] = {}
    all_members_set: set[str] = set()
    for s in season_list:
        pset: set[str] = set()
        for r in roster_by_season[s]["domestic"]:
            nm = str(r.get("player") or r.get("player_name") or "").strip()
            if nm:
                pset.add(nm)
                all_members_set.add(nm)
        for r in roster_by_season[s]["foreign"]:
            nm = str(r.get("player_zh") or r.get("player_name") or r.get("player_en") or r.get("player") or "").strip()
            if nm:
                pset.add(nm)
                all_members_set.add(nm)
        season_player_sets[s] = pset

    for fd in normalized_facets:
        c_nm = str(fd.get("person_name") or "").strip()
        if c_nm:
            all_members_set.add(c_nm)

    if isinstance(person_registry, Mapping):
        for p_entry in person_registry.get("persons") or ():
            if not isinstance(p_entry, Mapping):
                continue
            cname = str(p_entry.get("canonical_name") or "").strip()
            if not cname:
                continue
            for t in p_entry.get("role_tenures") or ():
                if not isinstance(t, Mapping):
                    continue
                t_tokens = {
                    _norm_token(t.get("organization_id")),
                    _norm_token(t.get("organization_name")),
                }
                t_tokens.discard("")
                if t_tokens & club_tokens:
                    all_members_set.add(cname)
                    break

    from_season = season_list[0]
    to_season = season_list[-1]
    prev_players = season_player_sets.get(from_season, set())
    curr_players = season_player_sets.get(to_season, set())

    retired_to_coach_set: set[str] = set()
    staged_foreign_set: set[str] = set()
    ledger_used = False

    if roster_ledger:
        l_retained: set[str] = set()
        l_joined: set[str] = set()
        l_retired_coach: set[str] = set()
        l_departed: set[str] = set()
        l_staged_foreign: set[str] = set()
        for row in roster_ledger:
            if not isinstance(row, Mapping):
                continue
            row_schema = str(row.get("schema") or "").strip()
            if row_schema and row_schema != CLUB_SEASON_ROSTER_LEDGER_SCHEMA:
                raise ValueError(f"INVALID_ROSTER_LEDGER_SCHEMA:{row_schema}")
            r_tokens = {
                _norm_token(row.get("organization_id")),
                _norm_token(row.get("club_name")),
                _norm_token(row.get("club_official")),
                _norm_token(row.get("club")),
            }
            r_tokens.discard("")
            if r_tokens and not (r_tokens & club_tokens):
                continue
            pname = str(
                row.get("person_name") or row.get("player_name") or row.get("player") or ""
            ).strip()
            if not pname:
                continue
            raw_bucket = str(
                row.get("movement_bucket") or row.get("bucket") or row.get("movement") or ""
            ).strip()
            bucket_key = _LEDGER_BUCKET_MAP.get(raw_bucket.upper()) or _LEDGER_BUCKET_MAP.get(raw_bucket)
            if not bucket_key:
                continue
            ledger_used = True
            all_members_set.add(pname)
            if bucket_key == "retained":
                l_retained.add(pname)
            elif bucket_key == "joined":
                l_joined.add(pname)
            elif bucket_key == "retired_to_club_coach":
                l_retired_coach.add(pname)
            elif bucket_key == "departed":
                l_departed.add(pname)
            elif bucket_key == "staged_foreign_25_26":
                l_staged_foreign.add(pname)

        if ledger_used:
            # Retired player -> club coach and staged foreigns MUST NEVER appear in departed
            l_departed -= l_retired_coach
            l_departed -= l_staged_foreign
            retained_players = sorted(l_retained)
            joined_players = sorted(l_joined)
            retired_to_coach_players = sorted(l_retired_coach)
            departed_players = sorted(l_departed)
            staged_foreign_players = sorted(l_staged_foreign)

    if not ledger_used:
        coach_names_to_season = {
            str(fd.get("person_name") or "").strip()
            for fd in normalized_facets
            if str(fd.get("person_name") or "").strip()
            and (not fd.get("season") or str(fd.get("season")).strip() == to_season)
        }
        if len(season_list) >= 2:
            retained_players = sorted(prev_players & curr_players)
            joined_players = sorted(curr_players - prev_players)
            raw_departed = prev_players - curr_players
            retired_to_coach_set = raw_departed & coach_names_to_season
            departed_players = sorted(raw_departed - retired_to_coach_set)
            retired_to_coach_players = sorted(retired_to_coach_set)
            staged_foreign_players = sorted(staged_foreign_set)
        else:
            retained_players = sorted(curr_players)
            joined_players = []
            retired_to_coach_players = []
            departed_players = []
            staged_foreign_players = []

    offseason_roster_changes: Dict[str, Any] = {
        "from_season": from_season,
        "to_season": to_season,
        "retained": retained_players,
        "joined": joined_players,
        "retired_to_club_coach": retired_to_coach_players,
        "departed": departed_players,
        "staged_foreign_25_26": staged_foreign_players,
    }
    all_roster_members: List[str] = sorted(all_members_set)

    # 4. Render Markdown
    cit_reg = CitationRegistry(visibility=vis)
    if is_wiki_v241:
        lines: List[str] = [f"# {club_name}"]
        lines.extend([
            "",
            "## 俱乐部概况",
            f"- 俱乐部全称：{club_name}",
            f"- 简称：{short_name}",
            f"- 所属联赛：{org['league']}",
        ])
        if org.get("city"):
            lines.append(f"- 所在城市：{org['city']}")

        lines.extend([
            "",
            "## 休赛期阵容变动",
            f"- 对比周期：{from_season} -> {to_season}",
            f"- 留队球员（含留用外援）（{len(retained_players)}）：{'、'.join(retained_players) if retained_players else '无'}",
            f"- 新援与青训上调（{len(joined_players)}）：{'、'.join(joined_players) if joined_players else '无'}",
            f"- 退役转任本队教练（{len(retired_to_coach_players)}）：{'、'.join(retired_to_coach_players) if retired_to_coach_players else '无'}",
            f"- 离队/转会球员（{len(departed_players)}）：{'、'.join(departed_players) if departed_players else '无'}",
            f"- 25-26 阶段性注册/出场外援（{len(staged_foreign_players)}）：{'、'.join(staged_foreign_players) if staged_foreign_players else '无'}",
        ])

        lines.extend([
            "",
            f"## 跨赛季阵容名单（{'、'.join(season_list)}）",
        ])
        for s in season_list:
            dom_rows = roster_by_season[s]["domestic"]
            for_rows = roster_by_season[s]["foreign"]
            lines.extend(["", f"### {s} 国内球员名单"])
            if dom_rows:
                lines.extend([
                    "| 球员 | 合同类别 | 注册状态 |",
                    "| --- | --- | --- |",
                ])
                for r in dom_rows:
                    p_disp = str(r.get("player") or r.get("player_name") or "-").strip()
                    cat = str(r.get("contract_category") or "未公示").strip() or "未公示"
                    reg_st = str(r.get("registration_status") or "-").strip() or "-"
                    if is_public:
                        p_disp = _sanitize_public_free_text(p_disp, fallback="-")
                        cat = _sanitize_public_free_text(cat, fallback="-")
                        reg_st = _sanitize_public_free_text(reg_st, fallback="-")
                    lines.append(f"| {p_disp} | {cat} | {reg_st} |")
            else:
                lines.append("- 当前快照暂无国内球员注册记录。")

            if for_rows:
                lines.extend([
                    "",
                    f"### {s} 外籍球员名单",
                    "| 中文名 | 英文名 | 位置 | 球衣号码 |",
                    "| --- | --- | --- | --- |",
                ])
                for r in for_rows:
                    p_zh = str(r.get("player_zh") or r.get("player_name") or "-").strip()
                    p_en = str(r.get("player_en") or r.get("player_name_en") or r.get("player") or "-").strip()
                    pos = str(r.get("position") or "-").strip()
                    jersey = str(r.get("jersey_number") or "-").strip()
                    if is_public:
                        p_zh = _sanitize_public_free_text(p_zh, fallback="-")
                        p_en = _sanitize_public_free_text(p_en, fallback="-")
                    lines.append(f"| {p_zh} | {p_en} | {pos} | {jersey} |")

        if normalized_facets:
            lines.extend(["", "## 教练组与治理角色"])
            for fd in normalized_facets:
                c_name = str(fd.get("person_name") or fd.get("person_uid") or "-")
                c_season = str(fd.get("season") or "-")
                l_role = str(fd.get("league_listed_role") or "OBSERVED_EMPTY")
                c_role = str(fd.get("club_appointed_role") or "OBSERVED_EMPTY")
                g_role = str(fd.get("gameday_acting_role") or "OBSERVED_EMPTY")
                lines.append(
                    f"- {c_name}（{c_season}）：联赛注册 `{l_role}`｜俱乐部任命 `{c_role}`｜临场执教 `{g_role}`"
                )
                if fd.get("has_institutional_discrepancy"):
                    d_code = str(fd.get("discrepancy_code") or "LEAGUE_VS_CLUB_ROLE_DISCREPANCY")
                    raw_expl = str(fd.get("discrepancy_explanation") or "").strip()
                    expl = _sanitize_public_free_text(raw_expl, fallback="-") if is_public else raw_expl
                    if expl and expl != "-":
                        lines.append(f"  * 制度口径差异（{d_code}）：{expl}")

        if projected_stories:
            lines.extend(["", "## 球队故事与关键事件"])
            for st in projected_stories:
                st_season = str(st.get("occurred_season") or "-")
                st_title = str(st.get("title") or "-")
                raw_sum = str(st.get("summary") or st.get("public_perspective_summary") or "-").strip()
                if is_public:
                    st_title = _sanitize_public_free_text(st_title, fallback="-")
                    st_sum = _sanitize_public_free_text(raw_sum, fallback="-")
                else:
                    st_sum = raw_sum or "-"
                c_src = st.get("canonical_source")
                ref_s = ""
                if isinstance(c_src, Mapping):
                    idx = cit_reg.register(
                        label=str(c_src.get("title") or c_src.get("label") or st_title),
                        url=str(c_src.get("url") or "") or None,
                        source_kind=str(c_src.get("source_kind") or "") or None,
                        published_at=str(c_src.get("published_at") or "") or None,
                        publisher=str(c_src.get("publisher") or "") or None,
                        source_id=str(c_src.get("source_id") or "") or None,
                    )
                    if idx is not None:
                        ref_s = cit_reg.format_ref(idx)
                lines.append(f"- [{st_season}] {st_title}：{st_sum}{ref_s}")
                if not is_public and st.get("internal_verbatim_or_note"):
                    lines.append(f"  * 内部摘录：{st['internal_verbatim_or_note']}")

        ref_md = cit_reg.render_markdown_references(heading="## 参考资料")
        if ref_md:
            lines.extend(["", ref_md])

        lines.extend([
            "",
            '<details class="audit-drawer">',
            "<summary>数据审计与观测边界</summary>",
            "",
            f"- Organization ID: {organization_id}",
            f"- observed_until: {observed_until or 'UNKNOWN'}",
            f"- seasons_covered: {', '.join(season_list)}",
            *(["- Visibility: PUBLIC_SAFE"] if is_public else []),
            "",
            "</details>",
        ])
    else:
        lines = [
            f"# 俱乐部全景卡｜{club_name}（{organization_id}）｜{seasons_label}｜CURRENT"
        ]
        if is_public:
            lines.append("- Visibility: PUBLIC_SAFE")
        lines.append("")

        lines.extend([
            "## Observation Horizon",
            f"- observed_until: {observed_until or 'UNKNOWN'}",
            f"- seasons_covered: {', '.join(season_list)}",
            "",
            "## Organization Profile",
            f"- Organization ID: {organization_id}",
            f"- Canonical Name: {club_name}",
            f"- Short Name: {short_name}",
            f"- League: {org['league']}",
        ])
        if org.get("city"):
            lines.append(f"- City: {org['city']}")

        lines.extend([
            "",
            f"## Multi-Season Roster ({', '.join(season_list)})",
        ])
        for s in season_list:
            dom_rows = roster_by_season[s]["domestic"]
            for_rows = roster_by_season[s]["foreign"]
            lines.extend([
                "",
                f"### {s} Domestic Roster",
            ])
            if dom_rows:
                lines.extend([
                    "| Player | Contract Category | Registration Status |",
                    "| --- | --- | --- |",
                ])
                for r in dom_rows:
                    p_disp = str(r.get("player") or r.get("player_name") or "-").strip()
                    cat = str(r.get("contract_category") or "未公示 (UNKNOWN)").strip() or "未公示 (UNKNOWN)"
                    reg_st = str(r.get("registration_status") or "-").strip() or "-"
                    if is_public:
                        p_disp = _sanitize_public_free_text(p_disp, fallback="-")
                        cat = _sanitize_public_free_text(cat, fallback="-")
                        reg_st = _sanitize_public_free_text(reg_st, fallback="-")
                    lines.append(f"| {p_disp} | {cat} | {reg_st} |")
            else:
                lines.append("- No domestic roster records observed in local snapshot.")

            if for_rows:
                lines.extend([
                    "",
                    f"### {s} Foreign Roster",
                    "| Player (ZH) | Player (EN) | Position | Jersey |",
                    "| --- | --- | --- | --- |",
                ])
                for r in for_rows:
                    p_zh = str(r.get("player_zh") or r.get("player_name") or "-").strip()
                    p_en = str(r.get("player_en") or r.get("player_name_en") or r.get("player") or "-").strip()
                    pos = str(r.get("position") or "-").strip()
                    jersey = str(r.get("jersey_number") or "-").strip()
                    if is_public:
                        p_zh = _sanitize_public_free_text(p_zh, fallback="-")
                        p_en = _sanitize_public_free_text(p_en, fallback="-")
                    lines.append(f"| {p_zh} | {p_en} | {pos} | {jersey} |")

        lines.extend([
            "",
            "## Coaching Staff (3-Facet Role Governance)",
        ])
        if normalized_facets:
            for fd in normalized_facets:
                c_name = str(fd.get("person_name") or fd.get("person_uid") or "-")
                c_season = str(fd.get("season") or "-")
                l_role = str(fd.get("league_listed_role") or "OBSERVED_EMPTY")
                c_role = str(fd.get("club_appointed_role") or "OBSERVED_EMPTY")
                g_role = str(fd.get("gameday_acting_role") or "OBSERVED_EMPTY")
                lines.append(
                    f"- {c_name} ({c_season}): "
                    f"league_listed_role={l_role} | "
                    f"club_appointed_role={c_role} | "
                    f"gameday_acting_role={g_role}"
                )
                if fd.get("has_institutional_discrepancy"):
                    d_code = str(fd.get("discrepancy_code") or "LEAGUE_VS_CLUB_ROLE_DISCREPANCY")
                    raw_expl = str(fd.get("discrepancy_explanation") or "").strip()
                    expl = (
                        _sanitize_public_free_text(raw_expl, fallback="-")
                        if is_public
                        else raw_expl
                    )
                    if expl and expl != "-":
                        lines.append(f"  * Discrepancy [{d_code}]: {expl}")
                    else:
                        lines.append(f"  * Discrepancy [{d_code}]")
        else:
            lines.append("- No coaching staff facets observed in local snapshot.")

        lines.extend([
            "",
            "## Organization Narrative & Story Timeline",
        ])
        if projected_stories:
            for st in projected_stories:
                st_season = str(st.get("occurred_season") or "-")
                st_title = str(st.get("title") or "-")
                raw_sum = str(
                    st.get("summary") or st.get("public_perspective_summary") or "-"
                ).strip()
                if is_public:
                    st_title = _sanitize_public_free_text(st_title, fallback="-")
                    st_sum = _sanitize_public_free_text(raw_sum, fallback="-")
                else:
                    st_sum = raw_sum or "-"
                lines.append(f"- [{st_season}] {st_title}: {st_sum}")
                if not is_public and st.get("internal_verbatim_or_note"):
                    lines.append(f"  * Internal Note: {st['internal_verbatim_or_note']}")
        else:
            lines.append("- No organization stories projected in local snapshot.")

    body_text = scrub_raw_drive_ids_for_consumer("\n".join(lines))
    markdown = body_text.rstrip() + "\n\nend_marker: true\n"

    if is_public:
        verify_public_safe_card_text(markdown)

    filename = club_card_consumer_filename(
        short_name or club_name,
        seasons_label,
        visibility=vis,
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

    return {
        "status": "BUILT",
        "club_query": club_query.strip(),
        "organization_id": organization_id,
        "club_name": club_name,
        "seasons": season_list,
        "visibility": vis,
        "card_style": style,
        "observed_until": observed_until,
        "roster_by_season": roster_by_season,
        "offseason_roster_changes": offseason_roster_changes,
        "all_roster_members": all_roster_members,
        "coaching_staff": normalized_facets,
        "projected_stories": projected_stories,
        "citations": cit_reg.entries() if is_wiki_v241 else [],
        "filename": filename,
        "card_path": card_path_str,
        "card_sha256": card_sha256,
        "markdown": markdown,
        "drive_api_called": False,
        "network_called": False,
    }
