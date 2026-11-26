"""PR-B (Issue #220) CANDIDATE renderings — offline/synthetic only.

No format in this module is described as readable by the Drive
connector. Readability is NOT_VERIFIED until separately authorized
Phase-2 measurement (see MEASUREMENT_PROTOCOL). A rendering without
its end marker is an explicit error, never a silent truncation: the
parser raises instead of returning partial content as complete.

Repair round 1 (Q2 FAIL response): the markdown/text check now
requires the marker as the LAST non-empty line (an embedded occurrence
no longer counts); render_catalog raises on incomplete input instead
of stamping it complete; render_passage_doc requires the caller to
assert evidence_verified=True, otherwise it raises.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .aliases import format_club_team_display
from .kc_projection import KcError
from .kc_visibility import validate_card_visibility, verify_public_safe_card_text
from .stats import CORE_SEASON_STATS_COLUMNS

READABILITY_STATUS = "NOT_VERIFIED_PENDING_PHASE_2"


def _safe_filename_part(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"UNSAFE_FILENAME_PART:{label}")
    cleaned = value.strip()
    if (
        not cleaned
        or "/" in cleaned
        or "\\" in cleaned
        or "\x00" in cleaned
        or ".." in cleaned
    ):
        raise ValueError(f"UNSAFE_FILENAME_PART:{label}")
    return "_".join(cleaned.split())


def research_card_consumer_filename(player_name: str, player_id: str, season: str) -> str:
    """Build self-describing Chinese consumer filename for promoted research cards."""
    safe_name = _safe_filename_part(player_name, "player_name")
    safe_id = _safe_filename_part(player_id, "player_id")
    safe_season = _safe_filename_part(season, "season")
    return f"研究卡_{safe_name}_{safe_id}_{safe_season}_CURRENT.md"


def research_card_public_consumer_filename(player_name: str, player_id: str, season: str) -> str:
    """Build self-describing Chinese consumer filename for public_safe research cards."""
    safe_name = _safe_filename_part(player_name, "player_name")
    safe_id = _safe_filename_part(player_id, "player_id")
    safe_season = _safe_filename_part(season, "season")
    return f"研究卡_{safe_name}_{safe_id}_{safe_season}_PUBLIC_CURRENT.md"


def slice_consumer_filename(
    player_name: str, player_id: str, season: str, match_type: str = "REGULAR"
) -> str:
    """Build self-describing Chinese consumer filename for promoted stats slices."""
    safe_name = _safe_filename_part(player_name, "player_name")
    safe_id = _safe_filename_part(player_id, "player_id")
    safe_season = _safe_filename_part(season, "season")
    safe_mt = _safe_filename_part(match_type, "match_type")
    return f"研究切片_{safe_name}_{safe_id}_{safe_season}_{safe_mt}_CURRENT.md"


_SEASON_METRIC_SHORT_LABELS: Dict[str, str] = {
    "games_played": "GP",
    "games_started": "GS",
    "minutes_per_game": "MIN",
    "points_per_game": "PTS",
    "rebounds_per_game": "REB",
    "assists_per_game": "AST",
    "steals_per_game": "STL",
    "blocks_per_game": "BLK",
    "turnovers_per_game": "TOV",
    "field_goals_percentage": "FG%",
    "three_point_percentage": "3P%",
    "free_throws_percentage": "FT%",
}


def _match_type_sort_rank(match_type: Any) -> int:
    mt = str(match_type or "").strip().upper()
    if mt in ("REGULAR", "1", "常规赛"):
        return 0
    if mt in ("PLAYOFFS", "2", "季后赛"):
        return 1
    return 2


def _scope_sort_rank(scope: Any) -> int:
    sc = str(scope or "").strip().upper()
    if sc == "WHOLE_SEASON":
        return 0
    if sc == "TEAM_SPLIT":
        return 1
    return 2


def _fmt_season_metric(val: Any) -> str:
    if val is None:
        return "UNKNOWN"
    return str(val)


def _render_season_stats_table_lines(
    rows: List[Dict[str, Any]],
    *,
    is_public: bool = False,
) -> List[str]:
    valid_rows = [r for r in rows if isinstance(r, dict)]
    if not valid_rows:
        return []
    sorted_rows = sorted(
        valid_rows,
        key=lambda r: (
            _match_type_sort_rank(r.get("match_type")),
            _scope_sort_rank(r.get("scope")),
            str(r.get("team_name") or r.get("team_id") or ""),
            str(r.get("record_id") or ""),
        ),
    )
    metric_headers = [_SEASON_METRIC_SHORT_LABELS[col] for col in CORE_SEASON_STATS_COLUMNS]
    headers = ["Match Type", "Scope", "Team", *metric_headers, "Captured At"]
    if not is_public:
        headers.append("Locator")
    table_lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in sorted_rows:
        metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else row
        mt = str(row.get("match_type") or "-")
        sc = str(row.get("scope") or "-")
        team = str(row.get("team_name") or row.get("team_id") or "-")
        cap = str(row.get("captured_at") or "-")
        metric_vals = [_fmt_season_metric(metrics.get(col)) for col in CORE_SEASON_STATS_COLUMNS]
        cells = [mt, sc, team, *metric_vals, cap]
        if not is_public:
            loc = str(row.get("locator") or row.get("record_id") or "-")
            cells.append(loc)
        table_lines.append("| " + " | ".join(cells) + " |")
    return table_lines


_TEN_SOURCES_RE = re.compile(r"10[\s_-]+sources", re.IGNORECASE)
_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INTERNAL_ID_TOKEN_RE = re.compile(
    r"(?:\bdoc:[A-Za-z0-9_.:-]+|\b(?:prior_doc_id|new_doc_id|prior_record_id|new_record_id|doc_id)\s*[:=]\s*[^\s,;)\]]+|\b(?:prior_doc_id|new_doc_id|prior_record_id|new_record_id)\b)",
    re.IGNORECASE,
)
_PRIVATE_URI_TOKEN_RE = re.compile(r"(?:r2|s3|file)://[^\s)\]<>\"']+", re.IGNORECASE)


def _is_public_safe_citation(cite: Dict[str, Any]) -> bool:
    if not isinstance(cite, dict):
        return False
    tier = str(cite.get("source_tier") or "").strip().upper()
    if tier == "MANUAL":
        return False
    url = str(cite.get("canonical_url") or "").strip()
    if not (url.startswith("https://") or url.startswith("http://")):
        return False
    combined = f"{cite.get('title') or ''}\n{url}"
    if _TEN_SOURCES_RE.search(combined):
        return False
    for marker in ("web 剪藏", "r2://", "s3://", "file://"):
        if marker in combined.lower():
            return False
    return True


def _is_internal_locator(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    if _TEN_SOURCES_RE.search(value):
        return True
    low = value.lower()
    for marker in ("doc:", "doc_id:", "web 剪藏", "r2://", "s3://", "file://"):
        if marker in low:
            return True
    return False


def _public_locator(value: Any) -> Optional[str]:
    """Return locator only if it is a clean public https:// URL; else None."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or not cleaned.lower().startswith("https://"):
        return None
    if _is_internal_locator(cleaned):
        return None
    return cleaned


def _sanitize_public_free_text(value: Any, *, fallback: str = "-") -> str:
    """Strip verbatim blockquotes (including lazy continuations & inline `>`),
    fenced code blocks, private URIs, and internal IDs for public_safe."""
    if not isinstance(value, str) or not value.strip():
        return fallback
    cleaned = _FENCED_BLOCK_RE.sub("", value)
    cleaned = cleaned.replace("```", "")
    kept_lines: List[str] = []
    in_blockquote = False
    for raw_line in cleaned.splitlines():
        s = raw_line.strip()
        if not s:
            in_blockquote = False
            continue
        if s.startswith(">"):
            in_blockquote = True
            continue
        if in_blockquote:
            continue
        if ">" in s:
            s = s.split(">", 1)[0].strip()
            if not s:
                continue
        kept_lines.append(s)
    joined = " ".join(kept_lines)
    joined = _PRIVATE_URI_TOKEN_RE.sub("", joined)
    joined = _INTERNAL_ID_TOKEN_RE.sub("", joined)
    joined = _TEN_SOURCES_RE.sub("", joined)
    joined = re.sub(r"web\s*剪藏", "", joined, flags=re.IGNORECASE)
    joined = " ".join(joined.split())
    return joined or fallback


def render_research_card(
    payload: Dict[str, Any],
    *,
    evidence_verified: bool = False,
    max_external_citations_per_card: int = 10,
    visibility: str = "internal_full",
) -> str:
    """Render compact exception-aware research card.

    Enforces evidence_verified=True guard. Renders grains and evidence posture.
    Ends strictly with 'end_marker: true'.
    """
    vis = validate_card_visibility(visibility)
    is_public = vis == "public_safe"
    if evidence_verified is not True:
        raise KcRenderError("RESEARCH_CARD_NOT_VERIFIED")

    grains = payload.get("grains", {})
    stats_layer = grains.get("STATS", {})
    fact_layer = grains.get("CANONICAL_FACT", {})
    doc_layer = grains.get("DOCUMENT", {})
    claim_layer = payload.get("claim_layer", {})
    transparency = payload.get("evidence_transparency", {})

    posture = transparency.get("posture") or (
        stats_layer.get("posture") if isinstance(stats_layer, dict) else None
    ) or "UNKNOWN"

    scope_meta = transparency.get("scope_meta") or {}
    season = scope_meta.get("season") or stats_layer.get("season", "-")
    player_name = scope_meta.get("player_name")
    player_id = scope_meta.get("player_id") or transparency.get("subject_player_uid")

    if posture != "UNKNOWN" and player_name and player_id and season and season != "-":
        title = (
            f"# 人物研究卡｜{player_name}（{player_id}）｜{season}｜CURRENT "
            "— Research Card (Serving Projection)"
        )
    elif posture != "UNKNOWN" and (player_name or player_id) and season and season != "-":
        entity_label = player_name or player_id
        title = f"# 人物研究卡｜{entity_label}｜{season}｜CURRENT — Research Card (Serving Projection)"
    else:
        title = "# Research Card (Serving Projection)"

    lines: List[str] = [title]
    if is_public:
        lines.append("- Visibility: PUBLIC_SAFE")
    lines.append("")

    # 0. Observation Horizon Section (REQ-230 P0-1 / REQ-232 P0-2: optional, 0-byte diff when absent)
    obs_horizon = payload.get("observation_horizon")
    has_obs_horizon = isinstance(obs_horizon, dict) and bool(obs_horizon)
    domain_horizons = payload.get("domain_horizons")
    has_domain_horizons = isinstance(domain_horizons, dict) and bool(domain_horizons)
    if has_obs_horizon or has_domain_horizons:
        lines.append("## Observation Horizon")
        if has_obs_horizon:
            obs_until = obs_horizon.get("observed_until") or "UNKNOWN"
            cov_status = (
                obs_horizon.get("overall_status")
                or obs_horizon.get("coverage_status")
                or "UNKNOWN"
            )
            missing_items = obs_horizon.get("missing_items") or []
            honest_disclosure = str(obs_horizon.get("honest_disclosure") or "").strip()
            lines.append(f"- observed_until: {obs_until}")
            lines.append(f"- coverage_status: {cov_status}")
            if missing_items:
                lines.append(f"- missing_items: {', '.join(str(x) for x in missing_items)}")
            else:
                lines.append("- missing_items: []")
            if honest_disclosure:
                lines.append(f"- honest_disclosure: {honest_disclosure}")
        if has_domain_horizons:
            ordered_doms = ["registration", "statistics", "official_announcements"]
            extra_doms = sorted(k for k in domain_horizons if k not in ordered_doms)
            for dom_key in [*ordered_doms, *extra_doms]:
                dom_val = domain_horizons.get(dom_key)
                if not isinstance(dom_val, dict):
                    continue
                d_status = dom_val.get("status") or "SOURCE_GAP_UNKNOWN"
                d_obs = dom_val.get("observed_until") or "UNKNOWN"
                d_note = str(dom_val.get("note") or "").strip()
                note_part = f" — {d_note}" if d_note else ""
                lines.append(
                    f"- domain[{dom_key}]: status={d_status}, observed_until={d_obs}{note_part}"
                )
        lines.append("")

    # 1. Stats & Scope Section
    lines.append("## Statistical Evidence")
    match_type = scope_meta.get("match_type") or stats_layer.get("match_type", "-")
    scope = scope_meta.get("scope") or stats_layer.get("scope", "-")
    games_played = scope_meta.get("games_played")
    captured_at = scope_meta.get("captured_at")
    locator = scope_meta.get("locator") or (
        doc_layer.get("locator") if isinstance(doc_layer, dict) else "-"
    )
    metric = payload.get("metric") or stats_layer.get("metric", "-")
    val = transparency.get("adopted_value")
    if val is None and isinstance(stats_layer, dict) and posture in ("CLEAN_OFFICIAL", "IDENTICAL_DUPLICATE"):
        val = stats_layer.get("value")

    club_display = format_club_team_display(scope_meta.get("team_id"), scope_meta.get("club_name_zh") or scope_meta.get("team_name"))

    lines.append(f"- Posture: {posture}")
    if posture != "UNKNOWN" and player_id:
        if player_name:
            lines.append(f"- Player: {player_name} ({player_id})")
        else:
            lines.append(f"- Player ID: {player_id}")
    if club_display and posture != "UNKNOWN":
        lines.append(f"- Club / Team: {club_display}")
    lines.append(f"- Season: {season}")
    lines.append(f"- Match Type: {match_type}")
    lines.append(f"- Scope: {scope}")
    if games_played is not None:
        lines.append(f"- Games Played: {games_played}")
    if captured_at:
        lines.append(f"- Cutoff / Captured At: {captured_at}")
    if locator and not (is_public and _is_internal_locator(locator)):
        lines.append(f"- Locator: {locator}")

    if posture in ("CLEAN_OFFICIAL", "IDENTICAL_DUPLICATE"):
        lines.append(f"- Metric: {metric} = {val}")
        lines.append(f"- Authority: {transparency.get('authority_note', 'PRIMARY_OFFICIAL_RECORD')}")
    elif posture == "SCOPE_OR_CUTOFF_DIFF":
        if val is not None:
            lines.append(f"- Metric: {metric} = {val} (latest cutoff)")
        for cr in transparency.get("cutoff_records", []):
            if is_public:
                pub_loc = _public_locator(cr.get("locator"))
                loc_part = f" [Locator: {pub_loc}]" if pub_loc else ""
                lines.append(
                    f"  * Cutoff {cr.get('games_played')} games ({cr.get('captured_at') or '-'}): "
                    f"{cr.get('value')}{loc_part}"
                )
            else:
                lines.append(
                    f"  * Cutoff {cr.get('games_played')} games ({cr.get('captured_at') or '-'}): "
                    f"{cr.get('value')} [Locator: {cr.get('locator')}]"
                )
        for sp in transparency.get("scope_splits", []):
            lines.append(
                f"  * Team Split {sp.get('team_name') or sp.get('team_id')}: "
                f"{sp.get('value')} ({sp.get('games_played')} games)"
            )
    elif posture == "REVISION_SUPERSEDED":
        lines.append(f"- Metric: {metric} = {val} (REVISION_SUPERSEDED)")
        for rev in transparency.get("revision_history", []):
            prior_val = rev.get("prior_value")
            new_val = rev.get("new_value")
            if is_public:
                safe_reason = _sanitize_public_free_text(rev.get("reason"), fallback="-")
                lines.append(
                    f"  * Prior Value: {prior_val} -> New Value: {new_val} "
                    f"(Reason: {safe_reason})"
                )
            else:
                lines.append(
                    f"  * Prior Value: {prior_val} -> New Value: {new_val} "
                    f"(Supersedes: {rev.get('supersedes_record_id')}, Reason: {rev.get('reason')})"
                )
    elif posture == "DATA_DISCREPANCY":
        delta = transparency.get("delta")
        delta_note = f" (Delta: {delta})" if delta is not None else ""
        lines.append(f"- Discrepancy Detected for Metric: {metric}{delta_note}")
        for d in transparency.get("discrepancies", []):
            if is_public:
                pub_loc = _public_locator(d.get("locator"))
                loc_part = f", Locator: {pub_loc}" if pub_loc else ""
                lines.append(
                    f"  * Observed Value: {d.get('value')} "
                    f"(Kind: {d.get('record_kind')}, Games: {d.get('games_played')}{loc_part})"
                )
            else:
                lines.append(
                    f"  * Record {d.get('record_id')}: {d.get('value')} "
                    f"(Kind: {d.get('record_kind')}, Games: {d.get('games_played')}, Locator: {d.get('locator')})"
                )
    elif posture == "TRUE_CONFLICT":
        lines.append(f"- Irreconcilable Conflict for Metric: {metric}")
        for d in transparency.get("discrepancies", []):
            if is_public:
                pub_loc = _public_locator(d.get("locator"))
                loc_part = f" (Locator: {pub_loc})" if pub_loc else ""
                lines.append(f"  * Observed Value: {d.get('value')}{loc_part}")
            else:
                lines.append(f"  * Record {d.get('record_id')}: {d.get('value')} (Locator: {d.get('locator')})")
    elif posture == "SINGLE_SOURCE_UNCORROBORATED":
        lines.append(f"- Metric: {metric} = {val}")
        lines.append("- Note: Single source uncorroborated")
    elif posture == "UNKNOWN":
        subkind = transparency.get("unknown_subkind", "NO_RELIABLE_EVIDENCE_IN_KB")
        lines.append(f"- Unknown: {subkind}")

    # 1b. 12-Metric Season Averages Table (REQ-232 P0-1: optional, 0-byte diff when absent)
    season_stats_table = payload.get("season_stats_table")
    if isinstance(season_stats_table, list) and season_stats_table:
        table_lines = _render_season_stats_table_lines(season_stats_table, is_public=is_public)
        if table_lines:
            lines.extend([
                "",
                "### Season Averages (Player-Season Grain)",
                *table_lines,
            ])

    # 1c. Prior Full-Season Baseline (REQ-232 P0-1: historical reference only, 0-byte diff when absent)
    prior_baseline = payload.get("prior_season_baseline")
    if isinstance(prior_baseline, dict) and isinstance(prior_baseline.get("rows"), list) and prior_baseline["rows"]:
        prior_s = str(prior_baseline.get("season") or "UNKNOWN")
        target_s = str(prior_baseline.get("target_season") or season)
        prior_table = _render_season_stats_table_lines(prior_baseline["rows"], is_public=is_public)
        if prior_table:
            lines.extend([
                "",
                f"### Prior Full-Season Baseline ({prior_s} — Historical Reference Only, Not {target_s})",
            ])
            if prior_baseline.get("captured_at"):
                lines.append(f"- Captured At: {prior_baseline['captured_at']}")
            if prior_baseline.get("locator") and not is_public:
                lines.append(f"- Locator: {prior_baseline['locator']}")
            lines.extend(prior_table)

    # 2. Claim Layer Section (when claims exist)
    claim_trans = transparency.get("claim_transparency") or {}
    claims_list = claim_layer.get("claims") if isinstance(claim_layer, dict) else None
    if claims_list:
        c_posture = claim_trans.get("posture", claim_layer.get("status", "UNKNOWN"))
        lines.extend([
            "",
            "## Claim Evidence",
            f"- Claim Posture: {c_posture}",
        ])
        for rev in claim_trans.get("revision_history", []):
            if is_public:
                safe_stmt = _sanitize_public_free_text(rev.get("statement"))
                lines.append(
                    f"  * Claim ({rev.get('status')}): {safe_stmt}"
                )
            else:
                lines.append(
                    f"  * Claim {rev.get('claim_id')} ({rev.get('status')}): "
                    f"{rev.get('statement')} [prior: {rev.get('prior_claim_id')}]"
                )

    # 3. Document Passage Section (strictly stripped under visibility="public_safe")
    if not is_public and isinstance(doc_layer, dict) and doc_layer.get("verbatim"):
        lines.extend([
            "",
            "## Document Passage",
            f"- Source: {doc_layer.get('source_id')}",
            f"- Locator: {doc_layer.get('locator')}",
            f"> {doc_layer.get('verbatim')}",
        ])

    # 4. External Citations Section (REQ-220 P0-3: ranked & capped with honest disclosure)
    raw_external_citations = payload.get("external_citations")
    if isinstance(raw_external_citations, (list, tuple)) and raw_external_citations:
        effective_citations = (
            [c for c in raw_external_citations if _is_public_safe_citation(c)]
            if is_public
            else list(raw_external_citations)
        )
        if effective_citations:
            from .consumer_surface import select_external_citations

            selection = select_external_citations(
                effective_citations,
                max_external_citations_per_card=max_external_citations_per_card,
            )
            total_linked = selection["total_linked_count"]
            displayed = selection["displayed_count"]
            cap = selection["max_external_citations_per_card"]
            lines.extend([
                "",
                "## External Citations",
                f"- Archive External Evidence: {total_linked} linked, {displayed} displayed (cap={cap})",
            ])
            if selection["truncated"]:
                omitted = total_linked - displayed
                lines.append(
                    f"- Truncated: {omitted} additional linked archive document(s) omitted from card surface"
                )
            for cite in selection["selected"]:
                tier = cite.get("source_tier") or "UNKNOWN"
                comp = cite.get("completeness") or "unknown"
                pub = cite.get("published_at") or "-"
                doc_id = cite.get("doc_id") or "-"
                url = cite.get("canonical_url") or ""
                rev_note = ""
                if cite.get("disclosure_posture") == "LOUD_REVISION" or cite.get("revision_summary"):
                    rev_note = " [LOUD_REVISION]"
                url_part = f" ({url})" if url else ""
                if is_public:
                    raw_cite_title = str(cite.get("title") or "").strip()
                    raw_cite_doc_id = str(cite.get("doc_id") or "").strip()
                    if raw_cite_doc_id and raw_cite_title == raw_cite_doc_id:
                        raw_cite_title = ""
                    pub_title = _sanitize_public_free_text(raw_cite_title, fallback="-")
                    lines.append(
                        f"  * [{tier}] ({comp}, {pub}) {pub_title}{url_part}{rev_note}"
                    )
                else:
                    title_str = cite.get("title") or cite.get("doc_id") or "-"
                    lines.append(
                        f"  * [{tier}] ({comp}, {pub}) {title_str} — {doc_id}{url_part}{rev_note}"
                    )

    # 5. Revision Disclosures Section (REQ-220 P0-2 / P0-3: LOUD_REVISION disclosure)
    rev_disclosures = payload.get("revision_disclosures")
    if not rev_disclosures and isinstance(payload.get("revision_summary"), dict):
        rev_disclosures = [payload["revision_summary"]]
    if isinstance(rev_disclosures, (list, tuple)) and rev_disclosures:
        lines.extend([
            "",
            "## Revision Disclosures",
        ])
        for rev in rev_disclosures:
            if not isinstance(rev, dict):
                continue
            posture_label = rev.get("disclosure_posture") or "LOUD_REVISION"
            seq = rev.get("revision_seq") or "-"
            if is_public:
                safe_notice = _sanitize_public_free_text(
                    rev.get("disclosure_notice"),
                    fallback=f"Document revision {seq} recorded",
                )
                lines.append(f"- [{posture_label}] {safe_notice}")
            else:
                prior_id = rev.get("prior_doc_id") or rev.get("prior_record_id") or "-"
                new_id = rev.get("new_doc_id") or rev.get("new_record_id") or "-"
                notice = rev.get("disclosure_notice") or (
                    f"Document revision {seq}: {new_id} supersedes {prior_id}"
                )
                lines.append(f"- [{posture_label}] {notice}")

    # 6. Registration & Identity Profile Section (REQ-223 P0-3/P0-4: optional, lossless)
    reg_profile = payload.get("registration_profile")
    has_v223_extension = False
    if isinstance(reg_profile, dict) and reg_profile:
        has_v223_extension = True
        lines.extend([
            "",
            "## Registration & Identity Profile",
        ])
        if reg_profile.get("player_name"):
            lines.append(f"- Player Name: {reg_profile['player_name']}")
        if reg_profile.get("player_name_en"):
            lines.append(f"- English Name: {reg_profile['player_name_en']}")
        if reg_profile.get("player_uid"):
            lines.append(f"- Player UID: {reg_profile['player_uid']}")
        if reg_profile.get("player_id"):
            lines.append(f"- Official Player ID: {reg_profile['player_id']}")
        if reg_profile.get("current_club"):
            lines.append(f"- Current Club: {reg_profile['current_club']}")
        clubs_hist = reg_profile.get("clubs_history")
        if isinstance(clubs_hist, list) and clubs_hist:
            lines.append(f"- Clubs History: {', '.join(str(c) for c in clubs_hist)}")
        if reg_profile.get("registration_type"):
            lines.append(f"- Registration Type: {reg_profile['registration_type']}")
        if reg_profile.get("position"):
            lines.append(f"- Position: {reg_profile['position']}")
        if reg_profile.get("jersey_number"):
            lines.append(f"- Jersey Number: {reg_profile['jersey_number']}")

        # REQ-231 P0-2: Rich domestic contract & cross-season history block (optional, 0-byte diff when absent)
        dom_cur = reg_profile.get("domestic_current")
        if isinstance(dom_cur, dict) and dom_cur:
            if dom_cur.get("club_official") and not reg_profile.get("current_club"):
                lines.append(f"- Current Club: {dom_cur['club_official']}")
            if dom_cur.get("registration_stage"):
                lines.append(f"- Registration Stage: {dom_cur['registration_stage']}")
            if dom_cur.get("registration_method"):
                lines.append(f"- Registration Method: {dom_cur['registration_method']}")
            contract_cat = str(dom_cur.get("contract_category") or "").strip() or "未公示 (UNKNOWN)"
            contract_term = str(dom_cur.get("contract_term_official") or "").strip() or "未公示 (UNKNOWN)"
            lines.append(f"- Contract Category: {contract_cat}")
            lines.append(f"- Official Contract Term / Expiry: {contract_term}")
            if dom_cur.get("former_club"):
                lines.append(f"- Former Club: {dom_cur['former_club']}")
            if dom_cur.get("registration_status"):
                lines.append(f"- Registration Status: {dom_cur['registration_status']}")
            if dom_cur.get("remarks"):
                lines.append(f"- Remarks: {dom_cur['remarks']}")
            lines.append(
                "- Official Salary Disclosure: 官方仅公示合同类别与期限，个人实领薪资未公开 (Unknown != 0)"
            )

        dom_hist = reg_profile.get("domestic_history")
        if isinstance(dom_hist, list) and dom_hist:
            sorted_hist = sorted(
                (h for h in dom_hist if isinstance(h, dict)),
                key=lambda h: str(h.get("season") or ""),
                reverse=True,
            )[:3]
            if sorted_hist:
                lines.extend([
                    "",
                    "### Cross-Season Registration History",
                ])
                for h in sorted_hist:
                    h_season = str(h.get("season") or "-")
                    h_club = str(h.get("club_official") or "-")
                    h_cat = str(h.get("contract_category") or "").strip() or "未公示 (UNKNOWN)"
                    h_term = str(h.get("contract_term_official") or "").strip() or "未公示 (UNKNOWN)"
                    h_method = str(h.get("registration_method") or "").strip() or "-"
                    h_former = str(h.get("former_club") or "").strip() or "-"
                    lines.append(
                        f"- {h_season} | {h_club} | {h_cat} | {h_term} | {h_method} | {h_former}"
                    )

        # REQ-231 P0-2: Rich foreign registration block (optional, 0-byte diff when absent)
        for_reg = reg_profile.get("foreign_registration")
        if isinstance(for_reg, dict) and for_reg:
            if for_reg.get("club") and not reg_profile.get("current_club"):
                lines.append(f"- Current Club: {for_reg['club']}")
            if for_reg.get("player_en") and not reg_profile.get("player_name_en"):
                lines.append(f"- English Name: {for_reg['player_en']}")
            if for_reg.get("player_zh"):
                lines.append(f"- Chinese Name: {for_reg['player_zh']}")
            if for_reg.get("nationality"):
                lines.append(f"- Nationality: {for_reg['nationality']}")
            if for_reg.get("position") and not reg_profile.get("position"):
                lines.append(f"- Position: {for_reg['position']}")
            if for_reg.get("jersey_number") and not reg_profile.get("jersey_number"):
                lines.append(f"- Jersey Number: {for_reg['jersey_number']}")
            if for_reg.get("published_at"):
                lines.append(f"- Official Snapshot Published At: {for_reg['published_at']}")
            if not is_public:
                if for_reg.get("doc_id"):
                    lines.append(f"- Snapshot doc_id: {for_reg['doc_id']}")
                if for_reg.get("raw_sha256"):
                    lines.append(f"- Snapshot raw_sha256: {for_reg['raw_sha256']}")
            lines.append(
                "- Data Layer: evidence / candidate_observation (Statement != Fact)"
            )

    # 7. Recent Single-Game Log Section (REQ-223 P0-4: PLAYER_GAME_STATS grain, lossless)
    recent_games = payload.get("recent_games_summary")
    if isinstance(recent_games, dict):
        has_v223_extension = True

        def _fmt_num(v: Any) -> str:
            if v is None:
                return "UNKNOWN"
            if isinstance(v, float):
                return f"{v:.1f}" if not v.is_integer() else f"{v:.1f}"
            return str(v)

        def _fmt_pct(made: Any, att: Any, pct: Any) -> str:
            if made is None or att is None:
                return "UNKNOWN"
            if pct is None:
                return f"{made}/{att} (N/A)"
            return f"{made}/{att} ({float(pct) * 100:.1f}%)"

        avgs = recent_games.get("averages") or {}
        st = recent_games.get("shooting_totals") or {}
        win_n = recent_games.get("window", 5)
        games_list = recent_games.get("games") or []
        played_n = recent_games.get("played_games_in_window", len(games_list))
        dnp_n = recent_games.get("dnp_games_count", 0)
        lines.extend([
            "",
            "## Recent Single-Game Log (Player-Game Grain)",
            "- Grain: PLAYER_GAME_STATS (separate from PLAYER_SEASON_STATS; never merged into season totals)",
            f"- Window Summary (Last {win_n} Played Games): played={played_n}, dnp_excluded={dnp_n}",
            (
                f"- Averages: PTS={_fmt_num(avgs.get('points'))}, REB={_fmt_num(avgs.get('rebounds'))}, "
                f"AST={_fmt_num(avgs.get('assists'))}, STL={_fmt_num(avgs.get('steals'))}, "
                f"BLK={_fmt_num(avgs.get('blocks'))}, MIN={_fmt_num(avgs.get('minutes_played'))}"
            ),
            (
                f"- Shooting Totals: FG={_fmt_pct(st.get('fg_made'), st.get('fg_attempted'), st.get('fg_pct'))}, "
                f"3P={_fmt_pct(st.get('tp_made'), st.get('tp_attempted'), st.get('tp_pct'))}, "
                f"FT={_fmt_pct(st.get('ft_made'), st.get('ft_attempted'), st.get('ft_pct'))}"
            ),
            (
                f"- Latest Game Date: {recent_games.get('latest_game_date') or '-'} | "
                f"Latest Captured At: {recent_games.get('latest_captured_at') or '-'}"
            ),
        ])
        if games_list:
            lines.extend([
                "",
                "| Date | Round | Opponent | MIN | PTS-REB-AST | FG / 3P / FT | Locator | Revision |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ])
            for g in games_list:
                if not isinstance(g, dict):
                    continue
                m = g.get("metrics") or {}
                prov = g.get("provenance") or {}
                g_date = g.get("game_date") or "-"
                rnd = g.get("round_label") or "-"
                opp = g.get("opponent_team_name") or g.get("opponent_team_id") or "-"
                mins = _fmt_num(m.get("minutes_played"))
                pts = "UNKNOWN" if m.get("points") is None else str(m.get("points"))
                reb = "UNKNOWN" if m.get("rebounds") is None else str(m.get("rebounds"))
                ast = "UNKNOWN" if m.get("assists") is None else str(m.get("assists"))
                fg_s = (
                    f"{m.get('field_goals_made')}-{m.get('field_goals_attempted')}"
                    if m.get("field_goals_made") is not None and m.get("field_goals_attempted") is not None
                    else "UNKNOWN"
                )
                tp_s = (
                    f"{m.get('three_point_made')}-{m.get('three_point_attempted')}"
                    if m.get("three_point_made") is not None and m.get("three_point_attempted") is not None
                    else "UNKNOWN"
                )
                ft_s = (
                    f"{m.get('free_throws_made')}-{m.get('free_throws_attempted')}"
                    if m.get("free_throws_made") is not None and m.get("free_throws_attempted") is not None
                    else "UNKNOWN"
                )
                loc = "-" if is_public else (
                    prov.get("doc_id") or (
                        str(prov.get("raw_sha256") or "")[:12] if prov.get("raw_sha256") else "-"
                    )
                )
                rev_mark = "[LOUD_REVISION]" if g.get("is_revised") else "-"
                lines.append(
                    f"| {g_date} | {rnd} | {opp} | {mins} | {pts}-{reb}-{ast} | {fg_s} / {tp_s} / {ft_s} | {loc} | {rev_mark} |"
                )
        else:
            lines.append(
                f"- Played Games in Window: 0 (no played appearances recorded in window; dnp_excluded={dnp_n})"
            )

        game_revs = recent_games.get("revision_summaries") or []
        if game_revs:
            lines.append("")
            for grev in game_revs:
                if not isinstance(grev, dict):
                    continue
                g_posture = grev.get("disclosure_posture") or "LOUD_REVISION"
                if is_public:
                    g_notice = _sanitize_public_free_text(
                        grev.get("disclosure_notice"),
                        fallback="Single-game record revised",
                    )
                else:
                    g_notice = grev.get("disclosure_notice") or (
                        f"{grev.get('new_record_id')} supersedes {grev.get('prior_record_id')}"
                    )
                lines.append(f"- [{g_posture}] {g_notice}")
    elif (
        isinstance(payload.get("single_game_stats_gap"), dict)
        and payload["single_game_stats_gap"].get("available") is False
    ):
        has_v223_extension = True
        degraded = bool(payload["single_game_stats_gap"].get("degraded"))
        gap_line = (
            "- Recent Single-Game Stats: NOT_AVAILABLE_IN_CURRENT_SNAPSHOT "
            "(Unknown != 0; 严禁凭外部记忆编造单场得分)"
        )
        if degraded:
            gap_line += " [云端单场数据源降级 / cloud single-game source degraded]"
        lines.extend([
            "",
            "## Recent Single-Game Log (Player-Game Grain)",
            gap_line,
        ])

    if has_v223_extension:
        from .consumer_surface import scrub_raw_drive_ids_for_consumer

        body_text = scrub_raw_drive_ids_for_consumer("\n".join(lines))
        final_text = body_text + "\n\nend_marker: true\n"
    else:
        lines.extend([
            "",
            "end_marker: true",
        ])
        final_text = "\n".join(lines) + "\n"

    if has_obs_horizon:
        from .consumer_surface import assert_no_exaggerated_coverage_claims

        assert_no_exaggerated_coverage_claims(final_text, obs_horizon)

    if is_public:
        verify_public_safe_card_text(final_text)

    return final_text


MEASUREMENT_PROTOCOL = (
    {"check": "pagination",
     "what_to_measure": "Whether paged reads return every page and "
                         "the consumer can detect a missing page.",
     "pass_criterion": "All pages retrieved; a missing page is detected, "
                        "never silently treated as complete.",
     "phase": "PHASE_2_SEPARATELY_AUTHORIZED", "status": "NOT_VERIFIED"},
    {"check": "folder_children_limits",
     "what_to_measure": "Maximum directly readable children per folder "
                         "and the behaviour past that limit.",
     "pass_criterion": "The limit is measured; exceeding it is detected, "
                        "never silently truncated.",
     "phase": "PHASE_2_SEPARATELY_AUTHORIZED", "status": "NOT_VERIFIED"},
    {"check": "sheets_ranges",
     "what_to_measure": "Whether native range reads return exact rows "
                         "for a routed slice key.",
     "pass_criterion": "Range reads match the routed key exactly.",
     "phase": "PHASE_2_SEPARATELY_AUTHORIZED", "status": "NOT_VERIFIED"},
    {"check": "sheets_cell_limits",
     "what_to_measure": "Maximum cells per scan and the behaviour past "
                         "that limit.",
     "pass_criterion": "The limit is measured; exceeding it is detected, "
                        "never silently truncated.",
     "phase": "PHASE_2_SEPARATELY_AUTHORIZED", "status": "NOT_VERIFIED"},
    {"check": "json_mime_searchability",
     "what_to_measure": "Whether JSON MIME content is searchable and "
                         "fully readable through the connector.",
     "pass_criterion": "JSON content is fully readable with types "
                        "preserved, or the format is rejected.",
     "phase": "PHASE_2_SEPARATELY_AUTHORIZED", "status": "NOT_VERIFIED"},
    {"check": "docs_revision_export_stability",
     "what_to_measure": "Whether a Docs revision and locator survive "
                         "rendering/export unchanged.",
     "pass_criterion": "Revision and locator bind to the same text on "
                        "re-read, or the binding is reported broken.",
     "phase": "PHASE_2_SEPARATELY_AUTHORIZED", "status": "NOT_VERIFIED"},
)


class KcRenderError(KcError):
    """Fail-closed rendering contract violation."""


def _marker_line(end_marker: bool) -> str:
    return "end_marker: true" if end_marker else "end_marker: false"


def render_json_slice(slice_dict: Dict[str, Any]) -> str:
    """JSON candidate rendering. The slice's own end_marker is carried
    verbatim; this function never upgrades an incomplete slice."""
    rendered = dict(slice_dict)
    rendered["rendering_kind"] = "json_slice"
    rendered["readability"] = READABILITY_STATUS
    return json.dumps(rendered, ensure_ascii=False, sort_keys=True,
                      indent=2)


def render_markdown_table(slice_dict: Dict[str, Any]) -> str:
    """Markdown field-table candidate rendering, with an explicit end
    marker line the consumer can verify."""
    lines = [
        "# Slice: %s / %s / %s / %s"
        % (slice_dict["player_uid"], slice_dict["season"],
           slice_dict["match_type"], slice_dict["scope"]),
        "",
        "| metric | value | unit |",
        "| --- | --- | --- |",
    ]
    for metric in slice_dict.get("supported_metrics", []):
        entry = slice_dict["metrics"][metric]
        lines.append("| %s | %s | %s |"
                     % (metric, entry["value"], entry["unit"]))
    lines.append("")
    lines.append(_marker_line(bool(slice_dict.get("end_marker"))))
    return "\n".join(lines) + "\n"


def render_passage_doc(passage: Dict[str, Any], *,
                       evidence_verified: bool = False) -> str:
    """INTERNAL primitive: passage text-doc candidate rendering:
    title, excerpt, revision, locator, digest, and an explicit end
    marker line. The caller must assert evidence_verified=True —
    otherwise this raises instead of stamping an unverified passage as
    complete. The flag is trusted ONLY when set by
    kc_consume.ConsumeSession.render_verified() after its own
    verification; a caller-set True is not a consumer trust signal.
    Consumer rendering must go through kc_consume.render_verified()."""
    if evidence_verified is not True:
        raise KcRenderError("PASSAGE_EVIDENCE_NOT_VERIFIED")
    lines = [
        "# %s" % passage.get("title", passage["source_id"]),
        "",
        passage["text"],
        "",
        "source_id: %s" % passage["source_id"],
        "revision: %s" % passage["revision"],
        "locator: %s" % passage["locator"],
        "digest: %s" % passage["digest"],
        "",
        "end_marker: true",
    ]
    return "\n".join(lines) + "\n"


def render_catalog(catalog: Dict[str, Any]) -> str:
    """Catalog candidate rendering. The input must already be complete
    (complete True AND end_marker True); anything else raises instead
    of being stamped complete. Slice-level completeness remains the
    slices' own end markers."""
    if catalog.get("complete") is not True \
            or catalog.get("end_marker") is not True:
        raise KcRenderError("CATALOG_NOT_COMPLETE")
    rendered = dict(catalog)
    rendered["rendering_kind"] = "json_catalog"
    rendered["readability"] = READABILITY_STATUS
    rendered["end_marker"] = True
    return json.dumps(rendered, ensure_ascii=False, sort_keys=True,
                      indent=2)


def parse_rendered(text: str, *, kind: str) -> Dict[str, Any]:
    """Parse a rendering and enforce its end marker. A missing or
    false end marker raises KcRenderError — partial content is never
    returned as complete. For markdown/text, the marker must be the
    LAST non-empty line, exactly "end_marker: true": an occurrence
    embedded in body text does not count."""
    if kind == "json":
        parsed = json.loads(text)
        if parsed.get("end_marker") is not True:
            raise KcRenderError("RENDERING_END_MARKER_MISSING")
        return parsed
    if kind in ("markdown", "text"):
        nonempty = [line.strip() for line in text.splitlines()
                    if line.strip()]
        if not nonempty or nonempty[-1] != "end_marker: true":
            raise KcRenderError("RENDERING_END_MARKER_MISSING")
        return {"end_marker": True}
    raise KcRenderError(f"UNKNOWN_RENDERING_KIND:{kind}")


def check_completeness(text: str, *, kind: str) -> bool:
    """True only if the rendering parses with a true end marker."""
    try:
        parse_rendered(text, kind=kind)
    except (KcRenderError, ValueError):
        return False
    return True


def measurement_protocol() -> List[Dict[str, Any]]:
    """A copy of the Phase-2 measurement protocol as data. Every check
    is NOT_VERIFIED until separately authorized measurement runs."""
    return [dict(check) for check in MEASUREMENT_PROTOCOL]
