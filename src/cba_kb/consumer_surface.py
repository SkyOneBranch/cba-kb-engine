"""Self-describing AI consumer surface exporters, redaction, and slice promotion (REQ-215)."""
from __future__ import annotations

import csv
from enum import Enum
import hashlib
import io
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence
import yaml

from .kc_consume import ConsumeSession, _content_digest
from .kc_passages import build_routed_passage, evidence_chain
from .kc_render import (
    research_card_consumer_filename,
    slice_consumer_filename,
)
from .kc_snapshot import build_fact_guarantee, build_snapshot

_DRIVE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_.-])[A-Za-z0-9_-]{19,64}(?![A-Za-z0-9_.-])")
_DRIVE_PREFIXED_ID_RE = re.compile(r"\bdrive:[A-Za-z0-9_-]{4,}")
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_DIGITS_RE = re.compile(r"^\d+$")
_SHORT_SOURCE_ID_RE = re.compile(r"^src:[0-9a-f]{12}$")

_DRIVE_HOST = "drive" + ".google." + "com"
_DOCS_HOST = "docs" + ".google." + "com"
_DRIVE_URL_RE = re.compile(
    r"https?://(?:" + re.escape(_DRIVE_HOST) + r"|" + re.escape(_DOCS_HOST) + r")/[^\s)\]<>\"']+"
)
_POSIX_LOCAL_ROOTS = (
    "Users|home|tmp|var|etc|opt|srv|data|private|root|mnt|Volumes|Library|Applications"
)
_LOCAL_PATH_RE = re.compile(
    r"(?:file:///(?:[A-Za-z]:[/\\])?[^\s)\]<>\"',;]+"
    r"|(?<![A-Za-z0-9_.:/])/(?:" + _POSIX_LOCAL_ROOTS + r")/[^\s)\]<>\"',;]+"
    r"|(?<![A-Za-z0-9_.:/\\])[A-Za-z]:[\\/][^\s)\]<>\"',;]+"
    r"|(?<![A-Za-z0-9_.:/\\])\\\\[A-Za-z0-9_.-]+\\[^\s)\]<>\"',;]+)"
)

CONSUMER_MASTER_FILENAME = "CBA_国内球员注册权威总表_2017-2027_CURRENT.csv"
CONSUMER_DICTIONARY_FILENAME = "CBA_核心数据表字段字典与权威口径_CURRENT.md"
CONSUMER_SOURCE_REGISTRY_FILENAME = "CBA_来源注册索引_脱敏视图_CURRENT.csv"
CONSUMER_DOC_INDEX_FILENAME = "CBA_文档索引_脱敏视图_CURRENT.json"
COLLECTOR_DIGEST_FILENAME = "CBA_官方采集动态与注册增量摘要_CURRENT.md"
FOREIGN_REGISTRATION_CSV_FILENAME = "CBA_外籍球员注册官方快照_CURRENT.csv"
DOMESTIC_REG_DIFF_CSV_FILENAME = "CBA_跨季国内球员注册增量观测_2025-2027_CURRENT.csv"
COLLECTOR_DIGEST_MAX_BYTES = 25 * 1024

COVERAGE_STATUSES = ("COMPLETE", "PARTIAL", "STALE", "UNKNOWN")
EXAGGERATED_COMPLETENESS_PHRASES = (
    "全联盟完整",
    "最新全量",
    "全量完整",
    "100%完整",
    "完整无缺",
)
_LEAGUE_COVERAGE_DOMAINS: tuple[tuple[str, str], ...] = (
    ("domestic_registration", "国内球员注册"),
    ("foreign_registration", "外籍球员注册"),
    ("club_announcements", "俱乐部官方公告"),
    ("league_rules", "联赛规程与规则"),
    ("game_stats", "官方比赛技术统计"),
)


def _parse_utc_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def evaluate_league_coverage(
    coverage_ledger: Optional[Mapping[str, Any]],
    *,
    now_utc: Optional[datetime] = None,
    stale_after_hours: float = 48.0,
) -> Dict[str, Any]:
    """Evaluate observation horizon & coverage status across the 5 CBA proposition domains."""
    if now_utc is None:
        now = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now = now_utc.replace(tzinfo=timezone.utc)
    else:
        now = now_utc.astimezone(timezone.utc)

    stale_delta = timedelta(hours=float(stale_after_hours))
    domains_out: Dict[str, Dict[str, Any]] = {}

    if not isinstance(coverage_ledger, Mapping) or not coverage_ledger:
        for dom_key, label in _LEAGUE_COVERAGE_DOMAINS:
            domains_out[dom_key] = {
                "domain": dom_key,
                "label": label,
                "observed_until": None,
                "coverage_status": "UNKNOWN",
                "missing_items": [],
                "source_count": 0,
                "honest_disclosure": f"{label}缺少覆盖账本（status=UNKNOWN）。",
            }
        return {
            "overall_status": "UNKNOWN",
            "observed_until": None,
            "domains": domains_out,
            "missing_items": [],
            "honest_disclosure": (
                "未提供有效覆盖账本（coverage_ledger），当前各命题域覆盖状态均为 UNKNOWN。"
                " Statement != Fact. Evidence != Fact. Unknown != 0."
            ),
        }

    grouped: Dict[str, list[Dict[str, Any]]] = {}
    raw_sources = coverage_ledger.get("sources")
    if isinstance(raw_sources, (list, tuple)):
        for src in raw_sources:
            if not isinstance(src, Mapping):
                continue
            dom = str(src.get("domain") or "").strip()
            if dom:
                grouped.setdefault(dom, []).append(dict(src))

    all_missing: list[str] = []
    seen_missing: set[str] = set()
    overall_obs_candidates: list[tuple[datetime, str]] = []

    for dom_key, label in _LEAGUE_COVERAGE_DOMAINS:
        dom_sources = grouped.get(dom_key, [])
        if not dom_sources:
            domains_out[dom_key] = {
                "domain": dom_key,
                "label": label,
                "observed_until": None,
                "coverage_status": "UNKNOWN",
                "missing_items": [],
                "source_count": 0,
                "honest_disclosure": f"{label}尚未记录有效观测来源（status=UNKNOWN）。",
            }
            continue

        dom_missing: list[str] = []
        seen_dom_missing: set[str] = set()
        dom_obs_candidates: list[tuple[datetime, str]] = []
        raw_statuses: list[str] = []

        for src in dom_sources:
            raw_st = str(src.get("status") or src.get("coverage_status") or "UNKNOWN").strip().upper()
            if raw_st not in COVERAGE_STATUSES:
                raw_st = "UNKNOWN"
            raw_statuses.append(raw_st)

            obs_raw = src.get("observed_until")
            obs_dt = _parse_utc_timestamp(obs_raw)
            if obs_dt is not None and isinstance(obs_raw, str):
                obs_clean = obs_raw.strip()
                dom_obs_candidates.append((obs_dt, obs_clean))
                overall_obs_candidates.append((obs_dt, obs_clean))

            m_list = src.get("missing") if src.get("missing") is not None else src.get("missing_items")
            if isinstance(m_list, (list, tuple)):
                for m_item in m_list:
                    m_str = str(m_item or "").strip()
                    if m_str:
                        if m_str not in seen_dom_missing:
                            seen_dom_missing.add(m_str)
                            dom_missing.append(m_str)
                        if m_str not in seen_missing:
                            seen_missing.add(m_str)
                            all_missing.append(m_str)

        dom_obs_dt: Optional[datetime] = None
        dom_obs_str: Optional[str] = None
        if dom_obs_candidates:
            dom_obs_dt, dom_obs_str = max(dom_obs_candidates, key=lambda pair: pair[0])

        if all(st == "UNKNOWN" for st in raw_statuses):
            dom_status = "UNKNOWN"
        elif any(st == "PARTIAL" for st in raw_statuses) or dom_missing or any(st == "UNKNOWN" for st in raw_statuses):
            dom_status = "PARTIAL"
        elif any(st == "STALE" for st in raw_statuses):
            dom_status = "STALE"
        else:
            dom_status = "COMPLETE"
            if dom_obs_dt is not None and (now - dom_obs_dt) > stale_delta:
                dom_status = "STALE"

        if dom_status == "COMPLETE":
            dom_disclosure = f"{label}已覆盖至 {dom_obs_str or 'UNKNOWN'}（status=COMPLETE）。"
        elif dom_status == "PARTIAL":
            miss_txt = ", ".join(dom_missing) if dom_missing else "未列明细"
            dom_disclosure = (
                f"{label}当前为部分覆盖（status=PARTIAL，observed_until={dom_obs_str or 'UNKNOWN'}），"
                f"缺口项：{miss_txt}。"
            )
        elif dom_status == "STALE":
            dom_disclosure = (
                f"{label}观测已过期（status=STALE，observed_until={dom_obs_str or 'UNKNOWN'}，"
                f"超过 {stale_after_hours:g}h 保鲜窗口）。"
            )
        else:
            dom_disclosure = f"{label}覆盖状态未知（status=UNKNOWN）。"

        domains_out[dom_key] = {
            "domain": dom_key,
            "label": label,
            "observed_until": dom_obs_str,
            "coverage_status": dom_status,
            "missing_items": dom_missing,
            "source_count": len(dom_sources),
            "honest_disclosure": dom_disclosure,
        }

    dom_statuses = [d["coverage_status"] for d in domains_out.values()]
    if all(st == "UNKNOWN" for st in dom_statuses):
        overall_status = "UNKNOWN"
    elif all(st == "COMPLETE" for st in dom_statuses):
        overall_status = "COMPLETE"
    elif any(st in ("PARTIAL", "UNKNOWN") for st in dom_statuses):
        overall_status = "PARTIAL"
    else:
        overall_status = "STALE"

    overall_obs: Optional[str] = None
    if overall_obs_candidates:
        overall_obs = max(overall_obs_candidates, key=lambda pair: pair[0])[1]
    elif isinstance(coverage_ledger.get("generated_at_utc"), str) and coverage_ledger["generated_at_utc"].strip():
        overall_obs = coverage_ledger["generated_at_utc"].strip()

    if overall_status == "COMPLETE":
        overall_disclosure = (
            f"已评估 5 个命题域（observed_until={overall_obs or 'UNKNOWN'}），当前覆盖状态为 COMPLETE。"
            " Statement != Fact. Evidence != Fact. Unknown != 0."
        )
    elif overall_status == "PARTIAL":
        miss_txt = ", ".join(all_missing) if all_missing else "部分命题域存在缺口"
        overall_disclosure = (
            f"当前覆盖状态为 PARTIAL（observed_until={overall_obs or 'UNKNOWN'}），"
            f"存在未覆盖缺口：{miss_txt}。Statement != Fact. Evidence != Fact. Unknown != 0."
        )
    elif overall_status == "STALE":
        overall_disclosure = (
            f"当前覆盖状态为 STALE（observed_until={overall_obs or 'UNKNOWN'}），"
            f"部分命题域已超过 {stale_after_hours:g}h 保鲜窗口。Statement != Fact. Evidence != Fact. Unknown != 0."
        )
    else:
        overall_disclosure = (
            "当前覆盖状态为 UNKNOWN，缺少有效命题域覆盖观测。"
            " Statement != Fact. Evidence != Fact. Unknown != 0."
        )

    return {
        "overall_status": overall_status,
        "observed_until": overall_obs,
        "domains": domains_out,
        "missing_items": all_missing,
        "honest_disclosure": overall_disclosure,
    }


def assert_no_exaggerated_coverage_claims(
    text: str, coverage_eval: Mapping[str, Any]
) -> None:
    """Fail-closed guard forbidding exaggerated completeness phrases unless coverage is COMPLETE."""
    if not isinstance(text, str) or not text:
        return
    if not isinstance(coverage_eval, Mapping):
        is_complete = False
    else:
        overall = str(
            coverage_eval.get("overall_status")
            or coverage_eval.get("coverage_status")
            or "UNKNOWN"
        ).strip().upper()
        domains = coverage_eval.get("domains")
        all_domains_complete = True
        if isinstance(domains, Mapping) and domains:
            all_domains_complete = all(
                isinstance(d, Mapping)
                and str(d.get("coverage_status") or "UNKNOWN").strip().upper() == "COMPLETE"
                for d in domains.values()
            )
        is_complete = (overall == "COMPLETE") and all_domains_complete
    if is_complete:
        return
    hits = [phrase for phrase in EXAGGERATED_COMPLETENESS_PHRASES if phrase in text]
    if hits:
        raise ValueError("EXAGGERATED_COVERAGE_CLAIM_FORBIDDEN:" + ",".join(hits))


def render_observation_horizon_header(
    coverage_eval: Mapping[str, Any], *, domain: Optional[str] = None
) -> str:
    """Render concise observation horizon header block validated against exaggerated claims."""
    target: Mapping[str, Any] = coverage_eval
    if domain and isinstance(coverage_eval.get("domains"), Mapping):
        dom_entry = coverage_eval["domains"].get(domain)
        if isinstance(dom_entry, Mapping):
            target = dom_entry

    obs_until = target.get("observed_until") or coverage_eval.get("observed_until") or "UNKNOWN"
    cov_status = (
        target.get("coverage_status")
        or target.get("overall_status")
        or coverage_eval.get("overall_status")
        or "UNKNOWN"
    )
    missing = list(target.get("missing_items") or [])
    disclosure = str(target.get("honest_disclosure") or coverage_eval.get("honest_disclosure") or "").strip()

    lines = [
        f"- **Observation Horizon (`observed_until`)**: `{obs_until}`",
        f"- **Coverage Status (`coverage_status`)**: `{cov_status}`",
    ]
    if missing:
        lines.append(f"- **Missing Items (`missing_items`)**: {', '.join(str(x) for x in missing)}")
    if disclosure:
        lines.append(f"- **Honest Disclosure**: {disclosure}")
    rendered = "\n".join(lines)
    assert_no_exaggerated_coverage_claims(rendered, coverage_eval)
    return rendered


class DomainHorizonStatus(str, Enum):
    """4-state per-domain observation horizon enum (REQ-232 P0-2)."""

    VERIFIED_PRESENT = "VERIFIED_PRESENT"
    OBSERVED_EMPTY = "OBSERVED_EMPTY"
    SOURCE_GAP_UNKNOWN = "SOURCE_GAP_UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


_NON_PLAYER_ENTITY_ROLES = frozenset({
    "HEAD_COACH",
    "ASSISTANT_COACH",
    "COACH",
    "EXECUTIVE",
    "GM",
    "GENERAL_MANAGER",
    "REFEREE",
    "STAFF",
    "主教练",
    "助理教练",
    "教练",
    "领队",
    "总经理",
    "裁判",
})


def _is_non_player_role(entity_role: Optional[str]) -> bool:
    if not isinstance(entity_role, str) or not entity_role.strip():
        return False
    raw = entity_role.strip()
    upper = raw.upper()
    if raw in _NON_PLAYER_ENTITY_ROLES or upper in _NON_PLAYER_ENTITY_ROLES:
        return True
    if "COACH" in upper or "教练" in raw:
        return True
    return False


def _extract_domain_obs_from_eval(
    coverage_eval: Optional[Mapping[str, Any]],
    domain_keys: Sequence[str],
) -> Optional[str]:
    if not isinstance(coverage_eval, Mapping):
        return None
    domains = coverage_eval.get("domains")
    if isinstance(domains, Mapping):
        for dk in domain_keys:
            entry = domains.get(dk)
            if isinstance(entry, Mapping):
                obs = entry.get("observed_until")
                if isinstance(obs, str) and obs.strip():
                    return obs.strip()
    overall_obs = coverage_eval.get("observed_until")
    if isinstance(overall_obs, str) and overall_obs.strip():
        return overall_obs.strip()
    return None


def build_person_domain_horizons(
    *,
    entity_role: str = "PLAYER",
    has_registration_records: bool = False,
    registration_observed_until: Optional[str] = None,
    registration_source_gap: bool = False,
    has_season_stats: bool = False,
    has_game_stats: bool = False,
    stats_observed_until: Optional[str] = None,
    game_stats_degraded: bool = False,
    stats_source_gap: bool = False,
    has_official_announcements: bool = False,
    announcements_observed_until: Optional[str] = None,
    announcements_source_gap: bool = False,
    coverage_eval: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Build 3-domain independent observation horizon for a person card (REQ-232 P0-2).

    Returns exact keys: ``registration``, ``statistics``, ``official_announcements``,
    each with ``{status, observed_until, note}``.
    Degraded statistics NEVER pollute verified registration.
    """
    reg_obs = (
        registration_observed_until.strip()
        if isinstance(registration_observed_until, str) and registration_observed_until.strip()
        else _extract_domain_obs_from_eval(
            coverage_eval, ("domestic_registration", "foreign_registration")
        )
    )
    stats_obs = (
        stats_observed_until.strip()
        if isinstance(stats_observed_until, str) and stats_observed_until.strip()
        else _extract_domain_obs_from_eval(coverage_eval, ("game_stats",))
    )
    ann_obs = (
        announcements_observed_until.strip()
        if isinstance(announcements_observed_until, str) and announcements_observed_until.strip()
        else _extract_domain_obs_from_eval(coverage_eval, ("club_announcements",))
    )

    # 1. Registration domain (isolated from statistics degradation)
    if has_registration_records:
        reg_entry = {
            "status": DomainHorizonStatus.VERIFIED_PRESENT.value,
            "observed_until": reg_obs,
            "note": "Registration records verified in local KB.",
        }
    elif registration_source_gap:
        reg_entry = {
            "status": DomainHorizonStatus.SOURCE_GAP_UNKNOWN.value,
            "observed_until": reg_obs,
            "note": "Registration source gap or unavailable.",
        }
    elif reg_obs:
        reg_entry = {
            "status": DomainHorizonStatus.OBSERVED_EMPTY.value,
            "observed_until": reg_obs,
            "note": "Observed through horizon; no registration records found.",
        }
    else:
        reg_entry = {
            "status": DomainHorizonStatus.SOURCE_GAP_UNKNOWN.value,
            "observed_until": None,
            "note": "No registration observation horizon recorded.",
        }

    # 2. Statistics domain
    if _is_non_player_role(entity_role):
        stats_entry = {
            "status": DomainHorizonStatus.NOT_APPLICABLE.value,
            "observed_until": None,
            "note": f"Non-player role ({entity_role}) does not have player boxscore statistics.",
        }
    elif game_stats_degraded or stats_source_gap:
        stats_entry = {
            "status": DomainHorizonStatus.SOURCE_GAP_UNKNOWN.value,
            "observed_until": stats_obs,
            "note": "Statistics source degraded or gap present; isolated from registration domain.",
        }
    elif has_season_stats or has_game_stats:
        stats_entry = {
            "status": DomainHorizonStatus.VERIFIED_PRESENT.value,
            "observed_until": stats_obs,
            "note": "Player statistics verified in local KB.",
        }
    elif stats_obs:
        stats_entry = {
            "status": DomainHorizonStatus.OBSERVED_EMPTY.value,
            "observed_until": stats_obs,
            "note": "Observed through horizon; no player statistics recorded yet.",
        }
    else:
        stats_entry = {
            "status": DomainHorizonStatus.SOURCE_GAP_UNKNOWN.value,
            "observed_until": None,
            "note": "No statistics observation horizon recorded.",
        }

    # 3. Official announcements domain
    if has_official_announcements:
        ann_entry = {
            "status": DomainHorizonStatus.VERIFIED_PRESENT.value,
            "observed_until": ann_obs,
            "note": "Matching official announcements verified.",
        }
    elif announcements_source_gap:
        ann_entry = {
            "status": DomainHorizonStatus.SOURCE_GAP_UNKNOWN.value,
            "observed_until": ann_obs,
            "note": "Official announcements source gap or unavailable.",
        }
    elif ann_obs:
        ann_entry = {
            "status": DomainHorizonStatus.OBSERVED_EMPTY.value,
            "observed_until": ann_obs,
            "note": "Observed through horizon; no matching official announcements.",
        }
    else:
        ann_entry = {
            "status": DomainHorizonStatus.SOURCE_GAP_UNKNOWN.value,
            "observed_until": None,
            "note": "No official announcements observation horizon recorded.",
        }

    return {
        "registration": reg_entry,
        "statistics": stats_entry,
        "official_announcements": ann_entry,
    }


_SEASON_YEAR_RE = re.compile(r"^(\d{4})")


def _parse_season_start_year(season: Any) -> Optional[int]:
    if not isinstance(season, str):
        return None
    m = _SEASON_YEAR_RE.match(season.strip())
    return int(m.group(1)) if m else None


def _is_prior_season(season: str, active_league_season: str) -> bool:
    s_clean = str(season or "").strip()
    a_clean = str(active_league_season or "").strip()
    if not s_clean or not a_clean or s_clean == a_clean:
        return False
    s_yr = _parse_season_start_year(s_clean)
    a_yr = _parse_season_start_year(a_clean)
    if s_yr is not None and a_yr is not None:
        return s_yr < a_yr
    return s_clean < a_clean


def assert_unique_current_card_per_person(targets: Any) -> None:
    """Fail-closed guard ensuring at most one ``研究卡_*_CURRENT.md`` per person (REQ-232 P0-3).

    Accepts either a mapping ``{logical_key: {"name": ...}}`` or a sequence of
    filenames / paths / dicts. Historical ``*_ARCHIVE.md`` cards are allowed
    alongside a single active ``*_CURRENT.md`` card.
    """
    names: list[str] = []
    if isinstance(targets, Mapping):
        for val in targets.values():
            if isinstance(val, Mapping):
                raw_name = val.get("name") or val.get("filename") or val.get("path")
            else:
                raw_name = val
            if isinstance(raw_name, (str, Path)) and str(raw_name).strip():
                names.append(Path(str(raw_name).strip()).name)
    elif isinstance(targets, (list, tuple, set)):
        for item in targets:
            if isinstance(item, Mapping):
                raw_name = item.get("name") or item.get("filename") or item.get("path")
            else:
                raw_name = item
            if isinstance(raw_name, (str, Path)) and str(raw_name).strip():
                names.append(Path(str(raw_name).strip()).name)

    seen_by_person: Dict[tuple[str, str], list[str]] = {}
    for basename in names:
        if not basename.startswith("研究卡_"):
            continue
        if basename.endswith("_PUBLIC_CURRENT.md"):
            vis_bucket = "public_safe"
        elif basename.endswith("_CURRENT.md"):
            vis_bucket = "internal_full"
        else:
            continue
        parts = basename.split("_")
        if len(parts) < 3 or not parts[1].strip():
            continue
        person_key = parts[1].strip()
        seen_by_person.setdefault((person_key, vis_bucket), []).append(basename)

    for (person_key, _vis), files in sorted(seen_by_person.items()):
        if len(files) > 1:
            raise ValueError(
                f"DUPLICATE_CURRENT_PERSON_CARD:{person_key}:{','.join(sorted(files))}"
            )


def _extract_horizon_csv_fields(
    observation_horizon: Mapping[str, Any], *, domain: Optional[str] = None
) -> tuple[str, str]:
    target: Mapping[str, Any] = observation_horizon
    if domain and isinstance(observation_horizon.get("domains"), Mapping):
        dom_entry = observation_horizon["domains"].get(domain)
        if isinstance(dom_entry, Mapping):
            target = dom_entry
    obs_until = str(target.get("observed_until") or observation_horizon.get("observed_until") or "UNKNOWN")
    cov_status = str(
        target.get("coverage_status")
        or target.get("overall_status")
        or observation_horizon.get("overall_status")
        or "UNKNOWN"
    )
    return obs_until, cov_status

_EVIDENCE_LAYER_LABEL = "evidence / candidate_observation"
_DEFAULT_STATEMENT_NOTICE = (
    "Candidate observation derived from official public CBA captures. "
    "Statement != Fact. Evidence != Fact. Unknown != 0."
)
_RELEASE_SEMVER_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)")
_ISO_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_MD_SEP_CELL_RE = re.compile(r"^:?-{2,}:?$")

_DEFAULT_CURATION_PROFILE_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "curation_profile.yaml"
)
_CURATION_PROFILE_LIST_KEYS = (
    "noise_event_classes",
    "noise_keywords",
    "historical_pinned_event_classes",
    "historical_pinned_keywords",
    "pinned_marker_keywords",
    "protected_event_classes",
    "active_competitive_keywords",
    "condolence_keywords",
    "condolence_exempt_keywords",
)
_CURATION_PROFILE_REQUIRED_KEYS = frozenset(
    {"schema_version", "compact_announcement_excerpt_chars", *_CURATION_PROFILE_LIST_KEYS}
)


def _validate_curation_profile(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("CURATION_PROFILE_INVALID: expected YAML mapping")
    missing = sorted(_CURATION_PROFILE_REQUIRED_KEYS - set(raw.keys()))
    if missing:
        raise ValueError(f"CURATION_PROFILE_INVALID: missing keys {','.join(missing)}")
    schema_version = raw.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != 1:
        raise ValueError("CURATION_PROFILE_INVALID: schema_version must be integer 1")
    excerpt_chars = raw.get("compact_announcement_excerpt_chars")
    if isinstance(excerpt_chars, bool) or not isinstance(excerpt_chars, int) or excerpt_chars <= 0:
        raise ValueError("CURATION_PROFILE_INVALID: compact_announcement_excerpt_chars must be positive int")
    validated: Dict[str, Any] = {
        "schema_version": 1,
        "compact_announcement_excerpt_chars": excerpt_chars,
    }
    for key in _CURATION_PROFILE_LIST_KEYS:
        val = raw.get(key)
        if not isinstance(val, list) or not all(isinstance(item, str) and item.strip() for item in val):
            raise ValueError(f"CURATION_PROFILE_INVALID: {key} must be list of non-empty strings")
        validated[key] = list(val)
    return validated


def load_curation_profile(path_or_instance: Any = None) -> Dict[str, Any]:
    """Load and strictly validate declarative curation profile YAML.

    Resolution order:
    - Explicit str/Path: must exist (else ValueError("CURATION_PROFILE_MISSING")).
    - Instance (object with .config_root): prefers <config_root>/curation_profile.yaml
      when present, falling back to repo default config/curation_profile.yaml.
    - None: repo default config/curation_profile.yaml.
    """
    target_path: Path
    if path_or_instance is None:
        target_path = _DEFAULT_CURATION_PROFILE_PATH
    elif isinstance(path_or_instance, (str, Path)):
        target_path = Path(path_or_instance)
        if not target_path.is_file():
            raise ValueError(f"CURATION_PROFILE_MISSING: {target_path}")
    elif hasattr(path_or_instance, "config_root"):
        candidate = Path(path_or_instance.config_root) / "curation_profile.yaml"
        target_path = candidate if candidate.is_file() else _DEFAULT_CURATION_PROFILE_PATH
    else:
        raise ValueError("CURATION_PROFILE_INVALID: unsupported path_or_instance type")

    if not target_path.is_file():
        raise ValueError(f"CURATION_PROFILE_MISSING: {target_path}")
    try:
        parsed = yaml.safe_load(target_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"CURATION_PROFILE_INVALID: {exc}") from exc
    return _validate_curation_profile(parsed)


def _resolve_curation_profile(
    *,
    curation_profile: Optional[Dict[str, Any]] = None,
    curation_profile_path: Optional[str | Path] = None,
    instance: Any = None,
) -> Dict[str, Any]:
    if curation_profile is not None:
        return _validate_curation_profile(curation_profile)
    if curation_profile_path is not None:
        return load_curation_profile(curation_profile_path)
    return load_curation_profile(instance)

DEFAULT_MAX_EXTERNAL_CITATIONS_PER_CARD = 10

_SOURCE_TIER_RANK = {
    "T1": 0,
    "T1.5": 1,
    "T2": 2,
    "MANUAL": 3,
}
_COMPLETENESS_RANK = {
    "full": 0,
    "partial": 1,
    "metadata_only": 2,
}


class _DescStr:
    """Wrapper for descending lexicographic string ordering in sort keys."""

    __slots__ = ("val",)

    def __init__(self, val: str) -> None:
        self.val = val

    def __lt__(self, other: "_DescStr") -> bool:
        return self.val > other.val

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _DescStr) and self.val == other.val


def _published_at_sort_key(value: Any) -> tuple[int, _DescStr]:
    if value is None:
        return (1, _DescStr(""))
    if hasattr(value, "isoformat") and callable(value.isoformat):
        text = str(value.isoformat()).strip()
    elif isinstance(value, str):
        text = value.strip()
    else:
        text = ""
    if not text:
        return (1, _DescStr(""))
    return (0, _DescStr(text))


def select_external_citations(
    citations: Sequence[Dict[str, Any]],
    *,
    max_external_citations_per_card: int = DEFAULT_MAX_EXTERNAL_CITATIONS_PER_CARD,
) -> Dict[str, Any]:
    """Deterministically rank and cap external citations for a player research card.

    Rank order:
      1. source_tier: T1 > T1.5 > T2 > MANUAL > other
      2. completeness: full > partial > metadata_only > other
      3. published_at descending (None / empty sorts last)
      4. doc_id ascending
    """
    if (
        isinstance(max_external_citations_per_card, bool)
        or not isinstance(max_external_citations_per_card, int)
        or max_external_citations_per_card < 1
    ):
        raise ValueError("MAX_EXTERNAL_CITATIONS_PER_CARD_INVALID")

    items = [dict(c) for c in (citations or ()) if isinstance(c, dict)]

    def _sort_key(c: Dict[str, Any]) -> tuple[int, int, tuple[int, _DescStr], str]:
        tier = str(c.get("source_tier") or "")
        comp = str(c.get("completeness") or "")
        doc_id = str(c.get("doc_id") or "")
        return (
            _SOURCE_TIER_RANK.get(tier, 4),
            _COMPLETENESS_RANK.get(comp, 3),
            _published_at_sort_key(c.get("published_at")),
            doc_id,
        )

    ranked = sorted(items, key=_sort_key)
    selected = ranked[:max_external_citations_per_card]
    total_linked = len(ranked)
    displayed = len(selected)
    return {
        "selected": selected,
        "total_linked_count": total_linked,
        "displayed_count": displayed,
        "truncated": total_linked > displayed,
        "max_external_citations_per_card": max_external_citations_per_card,
    }

_REDACT_JSON_KEYS = frozenset({
    "drive_file_id",
    "file_id",
    "current_parent_id",
    "parent_id",
    "folder_id",
    "root_folder_id",
    "source_file_id",
    "web_view_link",
    "webViewLink",
    "drive_url",
})


def _is_raw_drive_token(token: str) -> bool:
    if not token or _HEX40_RE.match(token) or _HEX64_RE.match(token) or _DIGITS_RE.match(token):
        return False
    if token.startswith("1") and len(token) >= 25:
        return True
    if token.startswith("0") and len(token) >= 19 and any(c.isupper() for c in token) and any(c.islower() for c in token):
        return True
    if (
        len(token) >= 25
        and token.count("_") <= 2
        and any(c.isupper() for c in token)
        and any(c.islower() for c in token)
        and any(c.isdigit() for c in token)
    ):
        return True
    return False


def _short_logical_source_id(raw_id: str) -> str:
    return "src:" + hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:12]


def _normalize_provenance_ids(known_drive_ids: Sequence[str]) -> list[str]:
    cleaned: set[str] = set()
    for raw in known_drive_ids or ():
        if not isinstance(raw, str):
            continue
        tok = raw.strip()
        if not tok:
            continue
        if tok.startswith("drive:"):
            tok = tok[len("drive:"):].strip()
        if not tok or len(tok) < 6:
            continue
        if _HEX40_RE.match(tok) or _HEX64_RE.match(tok) or _DIGITS_RE.match(tok):
            continue
        cleaned.add(tok)
    return sorted(cleaned, key=len, reverse=True)


def _compile_provenance_pattern(
    known_drive_ids: Sequence[str],
) -> Optional[re.Pattern[str]]:
    provenance_tokens = _normalize_provenance_ids(known_drive_ids)
    if not provenance_tokens:
        return None
    return re.compile(
        r"(?<![A-Za-z0-9_-])(?:"
        + "|".join(re.escape(tok) for tok in provenance_tokens)
        + r")(?![A-Za-z0-9_-])"
    )


def _scrub_with_pattern(
    text: str, prov_re: Optional[re.Pattern[str]]
) -> str:
    if not isinstance(text, str) or not text:
        return ""
    scrubbed = _DRIVE_URL_RE.sub("<redacted-drive-url>", text)
    scrubbed = _DRIVE_PREFIXED_ID_RE.sub("<redacted-drive-id>", scrubbed)
    scrubbed = _LOCAL_PATH_RE.sub("<redacted-local-path>", scrubbed)
    if prov_re is not None:
        scrubbed = prov_re.sub("<redacted-drive-id>", scrubbed)

    def _replace_tok(match: re.Match[str]) -> str:
        tok = match.group(0)
        if _is_raw_drive_token(tok):
            return "<redacted-drive-id>"
        return tok

    return _DRIVE_TOKEN_RE.sub(_replace_tok, scrubbed)


def scrub_raw_drive_ids_for_consumer(
    text: str,
    *,
    known_drive_ids: Sequence[str] = (),
) -> str:
    """Redact raw Drive IDs (provenance-known + alternate shapes) and Drive URLs.

    Preserves 40-hex Git SHAs, 64-hex SHA-256 digests, filenames, and ordinary numbers.
    """
    return _scrub_with_pattern(text, _compile_provenance_pattern(known_drive_ids))


def _require_positive_master_row_count(master_row_count: object) -> int:
    """Require an exact positive integer row count; fail closed on missing/bool/zero."""
    if (
        isinstance(master_row_count, bool)
        or not isinstance(master_row_count, int)
        or master_row_count <= 0
    ):
        raise ValueError("MASTER_ROW_COUNT_REQUIRED")
    return master_row_count


def compute_master_row_count(data: bytes, *, is_xlsx: bool = False) -> int:
    """Compute exact data-row count from MASTER CSV or XLSX bytes at build time."""
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("MASTER_BYTES_REQUIRED")
    raw = bytes(data)
    if is_xlsx:
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        try:
            if "MASTER" not in wb.sheetnames:
                raise ValueError("MASTER_SHEET_MISSING")
            ws = wb["MASTER"]
            rows_iter = ws.values
            header = next(rows_iter, None)
            if not header or not any(
                cell is not None and (not isinstance(cell, str) or cell.strip())
                for cell in header
            ):
                raise ValueError("MASTER_ROW_COUNT_EMPTY")
            count = sum(
                1
                for row in rows_iter
                if any(
                    cell is not None and (not isinstance(cell, str) or cell.strip())
                    for cell in row
                )
            )
        finally:
            wb.close()
    else:
        text = raw.decode("utf-8-sig")
        reader = csv.reader(io.StringIO(text))
        header = next(reader, None)
        if not header or not any(cell.strip() for cell in header):
            raise ValueError("MASTER_ROW_COUNT_EMPTY")
        count = sum(1 for row in reader if any(cell.strip() for cell in row))
    if count <= 0:
        raise ValueError("MASTER_ROW_COUNT_EMPTY")
    return count


def _should_include_collector_digest(
    release_id: str, include_collector_digest: Optional[bool]
) -> bool:
    if include_collector_digest is not None:
        return bool(include_collector_digest)
    return isinstance(release_id, str) and release_id.startswith("v2.2")


def _should_include_curated_csvs(
    release_id: str, include_curated_csvs: Optional[bool]
) -> bool:
    if include_curated_csvs is not None:
        return bool(include_curated_csvs)
    if not isinstance(release_id, str):
        return False
    m = _RELEASE_SEMVER_RE.match(release_id.strip())
    if not m:
        return False
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) >= (2, 2, 2)


def render_consumer_surface_guide_section(
    *,
    release_id: str,
    master_row_count: int,
    include_collector_digest: Optional[bool] = None,
    include_curated_csvs: Optional[bool] = None,
    observation_horizon: Optional[Mapping[str, Any]] = None,
) -> str:
    """Render standardized consumer-surface guide block with exact dynamic MASTER count."""
    exact_count = _require_positive_master_row_count(master_row_count)
    count_label = f"{exact_count:,} 条"
    lines = [
        "## 当前生产权威消费面导览 (CURRENT Canonical Surface)",
        "",
        f"- 权威发布标识：`{release_id}`（仅读取处于 `COMPLETE` 状态且位于 `current` 消费区的 `CURRENT` / 权威产物；`90_archive_历史归档`、回收站与关闭沙盒绝非当前真值）。",
        f"- 当前 `MASTER` 国内球员注册总表数据行数：**{count_label}**（由构建期自动统计；历史迁移文档中提及的 `3,451` / `3451` 仅为 `2026-09-10` 早期 v1.1 载体对照基线，已由当前动态行数取代）。",
    ]
    if observation_horizon is not None:
        lines.extend(render_observation_horizon_header(observation_horizon).splitlines())
    lines.extend([
        "- **自描述核心消费主表与字典**（面向全盘中文检索优先读取）：",
        f"  - `{CONSUMER_MASTER_FILENAME}`（对应底层 `derived/MASTER.csv` / `input/MASTER.xlsx`，全量国内球员注册权威总表）",
        f"  - `{CONSUMER_DICTIONARY_FILENAME}`（核心数据表字段字典、缩写释义与权威口径说明）",
        f"  - `{CONSUMER_SOURCE_REGISTRY_FILENAME}`（来源文件注册脱敏索引，隐去底层物理 `drive_file_id`，保留逻辑 `source_id` 与可读文件名）",
    ])
    if _should_include_collector_digest(release_id, include_collector_digest):
        lines.extend([
            f"  - `{CONSUMER_DOC_INDEX_FILENAME}`（已合并最新受控采集文档索引的脱敏视图，隐去物理 Drive ID、`file_id` 与本地路径）",
            f"  - `{COLLECTOR_DIGEST_FILENAME}`（`evidence / candidate_observation` 层级官方采集动态与注册增量摘要：含 2026-2027 外籍球员注册快照、近三赛季国内注册与跨季增量比对及官方公告摘要；**遵守 `Statement != Fact. Evidence != Fact. Unknown != 0.`，不得直接视作已晋升的 `canonical` 事实**）",
        ])
    if _should_include_curated_csvs(release_id, include_curated_csvs):
        lines.extend([
            f"  - `{FOREIGN_REGISTRATION_CSV_FILENAME}`（`evidence / candidate_observation` 层级 2026-2027 外籍球员官方注册快照结构化 UTF-8-SIG CSV，推荐使用 Python CSV 直接读取解析）",
            f"  - `{DOMESTIC_REG_DIFF_CSV_FILENAME}`（`evidence / candidate_observation` 层级 2025-2026 → 2026-2027 跨季国内球员注册增量观测结构化 UTF-8-SIG CSV，推荐使用 Python CSV 直接读取解析）",
        ])
    lines.extend([
        "- **人物研究切片（`CURRENT` 正式入列）**：正式上线的已授权球员研究卡采用 `研究卡_<中文姓名>_<球员ID>_<赛季>_CURRENT.md`（及对应 `研究切片_..._CURRENT.md`、`研究摘录_..._CURRENT.md`）命名并置于 `current`；全联盟通用赛季技术统计总表与球员人民币精确薪资未在公开事实层发布，未入列对象一律按 `UNKNOWN` 诚实拒答（`Unknown != 0`）。",
        "- **关键字段语义消歧（严禁跨表混淆）**：",
        "  - `date_year_inferred` 属于事件表 `facts/CBA_球员注册_EVENTS.xlsx`（工作表 `registration_status_events`）的**事件行级布尔字段**：仅在跨季中段“取消注册”等仅写月日、年份由赛季边界推导时为 `True`。",
        "  - `source_registry`（及脱敏索引 `CBA_来源注册索引_脱敏视图_CURRENT.csv`）中的 `season`、`source_title` 与 `discovered_at_utc` 属于**来源文件级元数据**，与事件行级 `date_year_inferred` 相互独立，不得用来源文件字段替代或否定事件行级推导标记。",
    ])
    rendered = "\n".join(lines)
    if observation_horizon is not None:
        assert_no_exaggerated_coverage_claims(rendered, observation_horizon)
    return rendered


def render_consumer_field_dictionary_md(
    *,
    release_id: str,
    engine_sha: str,
    master_row_count: int,
    include_collector_digest: Optional[bool] = None,
    include_curated_csvs: Optional[bool] = None,
    observation_horizon: Optional[Mapping[str, Any]] = None,
) -> bytes:
    """Generate the self-describing CURRENT consumer field dictionary Markdown document."""
    exact_count = _require_positive_master_row_count(master_row_count)
    include_digest = _should_include_collector_digest(release_id, include_collector_digest)
    include_csvs = _should_include_curated_csvs(release_id, include_curated_csvs)
    lines = [
        f"# CBA-KB 核心数据表字段字典与权威口径（{release_id} / CURRENT）",
        "",
        f"- **发布版本**: `{release_id}`",
        f"- **代码基线 Commit**: `{engine_sha}`",
        f"- **国内球员注册权威总表 (`MASTER`) 动态构建行数**: **{exact_count:,} 条**（历史文档中的 `3,451` 仅指 `2026-09-10` v1.1 迁移对照基线，非当前实时行数）。",
        "- **核心原则**: `Statement != Fact. Evidence != Fact. Unknown != 0.`",
    ]
    if observation_horizon is not None:
        lines.extend(render_observation_horizon_header(observation_horizon).splitlines())
    lines.extend([
        "",
        "## 1. 权威产物路由与命名规范（CURRENT Canonical Surface）",
        "",
        "| 消费文件名 / 路径 | 权威层级 | 覆盖范围与用途 |",
        "| --- | --- | --- |",
        f"| `{CONSUMER_MASTER_FILENAME}` (`derived/MASTER.csv`, `input/MASTER.xlsx`) | `canonical` | 国内球员赛季注册权威总表（{exact_count:,} 行，覆盖 2017–2027） |",
        "| `facts/CBA_注册领域_六表.xlsx` | `canonical` | 注册域结构化六表（国内注册、交易窗口、外援优先权与注册快照、状态事件） |",
        "| `facts/CBA_球员注册_EVENTS.xlsx` | `canonical` | 全量注册状态变更与跨季事件表（`registration_status_events`） |",
        "| `facts/CBA_外籍球员注册_SNAPSHOTS.xlsx` | `canonical` | 外籍球员官方注册快照 |",
        f"| `{CONSUMER_SOURCE_REGISTRY_FILENAME}` | `control / metadata` | 来源文件注册脱敏索引（隐藏原始物理 Drive ID，保留逻辑 `source_id` 与可读标题） |",
    ])
    if include_digest:
        lines.extend([
            f"| `{CONSUMER_DOC_INDEX_FILENAME}` | `control / metadata` | 官方文档脱敏索引（含 `consumed_bundles` 合并视图，隐去原始 Drive ID、`file_id` 与本地路径） |",
            f"| `{COLLECTOR_DIGEST_FILENAME}` | `evidence / candidate_observation` | 受控官方采集动态与注册增量摘要（2026-2027 外籍注册快照、三赛季国内注册与跨季 `25-26 -> 26-27` 增量观察、官方公告摘录；`Statement != Fact. Evidence != Fact. Unknown != 0.`） |",
        ])
    if include_csvs:
        lines.extend([
            f"| `{FOREIGN_REGISTRATION_CSV_FILENAME}` | `evidence / candidate_observation` | 2026-2027 外籍球员官方注册快照结构化 UTF-8-SIG CSV（推荐 Python CSV 读取；`Statement != Fact. Evidence != Fact. Unknown != 0.`） |",
            f"| `{DOMESTIC_REG_DIFF_CSV_FILENAME}` | `evidence / candidate_observation` | 跨季国内球员注册增量观测结构化 UTF-8-SIG CSV（含 `TRANSFERRED` / `CONTRACT_CHANGED` / `ADDED` / `REMOVED`，推荐 Python CSV 读取） |",
        ])
    lines.extend([
        "| `研究卡_<中文姓名>_<球员ID>_<赛季>_CURRENT.md` | `research` | 已获授权并正式入列 `current` 的人物技术统计与多源证据研究卡（仅限已入列切片，未入列球员统计与精确薪资均属 `UNKNOWN`） |",
        "",
        "## 2. 易混淆字段明确消歧",
        "",
        "| 字段名 | 所属产物与粒度 | 真实语义 | 常见误读警示 |",
        "| --- | --- | --- | --- |",
        "| `date_year_inferred` | `facts/CBA_球员注册_EVENTS.xlsx#registration_status_events`（**事件行级**） | `True` 表示该行事件日期（如外援取消注册仅记月日）的年份系依据赛季起止边界推导得出；`False` 表示原始来源明确写出年份。 | **禁止**在 `source_registry` 文件清单中寻找该字段，或以文件年份推断覆盖事件行级 `date_year_inferred`。 |",
        "| `season` / `discovered_at_utc` | `CBA_来源注册索引_脱敏视图_CURRENT.csv`（**来源文件级**） | 描述原始公告/PDF/报道文件的归属赛季与采集入库时间。 | 仅描述源文件元数据，不代表具体事件行的年份推导属性。 |",
        "| `verification_level` | `MASTER`（**注册记录级**） | 注册条目机器核验等级（如 `auto_validated` 仅表示通过确定性规则核验，不等同于人工复核）。 | 不得将空值或 `UNKNOWN` 解释为 `0`。 |",
        "",
        "## 3. 全盘检索优先规则",
        "",
        "1. 在全盘搜索模式下，仅采信文件名带 `CURRENT` 或位于正式 `current` 消费区的权威主表与研究卡。",
        "2. 历史快照仅保留于受控备份归档，不得作为当前事实出处引用。",
    ])
    if include_digest:
        lines.append(
            f"3. `{COLLECTOR_DIGEST_FILENAME}` 属于 `evidence / candidate_observation` 证据观察层，引用时须显式区分官方公告/候选项与 `canonical` 主表事实（`Statement != Fact. Evidence != Fact. Unknown != 0.`）。"
        )
    if include_csvs:
        step_no = 4 if include_digest else 3
        lines.append(
            f"{step_no}. 检索外籍球员注册明细与跨季国内注册增量时，优先使用 Python CSV 工具直接读取 `{FOREIGN_REGISTRATION_CSV_FILENAME}` 与 `{DOMESTIC_REG_DIFF_CSV_FILENAME}`，避免依赖大篇幅 Markdown 表格截断。"
        )
    lines.extend([
        "",
        "end_marker: true",
    ])
    rendered_text = "\n".join(lines) + "\n"
    if observation_horizon is not None:
        assert_no_exaggerated_coverage_claims(rendered_text, observation_horizon)
    return rendered_text.encode("utf-8")


def export_consumer_master_csv(
    raw_csv_bytes: bytes,
    *,
    known_drive_ids: Sequence[str] = (),
    observation_horizon: Optional[Mapping[str, Any]] = None,
) -> bytes:
    """Create self-describing CURRENT MASTER CSV copy with provenance-based ID redaction."""
    text = raw_csv_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("MASTER_CSV_HEADER_REQUIRED")
    fieldnames = list(reader.fieldnames)
    horizon_vals: Optional[tuple[str, str]] = None
    if observation_horizon is not None:
        horizon_vals = _extract_horizon_csv_fields(
            observation_horizon, domain="domestic_registration"
        )
        for hcol in ("observed_until", "coverage_status"):
            if hcol not in fieldnames:
                fieldnames.append(hcol)
    rows = list(reader)
    provenance_ids: set[str] = set(known_drive_ids or ())
    for row in rows:
        sfid = (row.get("source_file_id") or "").strip()
        if sfid:
            if sfid.startswith("drive:"):
                sfid = sfid[len("drive:"):].strip()
            if sfid and not _SHORT_SOURCE_ID_RE.match(sfid):
                provenance_ids.add(sfid)
    prov_re = _compile_provenance_pattern(sorted(provenance_ids))

    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        cleaned_row: Dict[str, Any] = {}
        for col in fieldnames:
            if horizon_vals is not None and col == "observed_until":
                cleaned_row[col] = horizon_vals[0]
                continue
            if horizon_vals is not None and col == "coverage_status":
                cleaned_row[col] = horizon_vals[1]
                continue
            val = row.get(col, "")
            if col == "source_file_id" and isinstance(val, str) and val.strip():
                raw_sfid = val.strip()
                if _SHORT_SOURCE_ID_RE.match(raw_sfid):
                    cleaned_row[col] = raw_sfid
                else:
                    if raw_sfid.startswith("drive:"):
                        raw_sfid = raw_sfid[len("drive:"):].strip()
                    cleaned_row[col] = (
                        _short_logical_source_id(raw_sfid)
                        if raw_sfid
                        else _scrub_with_pattern(val, prov_re)
                    )
            elif isinstance(val, str):
                cleaned_row[col] = _scrub_with_pattern(val, prov_re)
            else:
                cleaned_row[col] = val
        writer.writerow(cleaned_row)
    return out.getvalue().encode("utf-8-sig")


def export_consumer_source_registry_csv(
    raw_csv_bytes: bytes, *, known_drive_ids: Sequence[str] = ()
) -> bytes:
    """Export AI consumer view of source_registry.csv with provenance-based ID removal."""
    text = raw_csv_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("SOURCE_REGISTRY_HEADER_REQUIRED")
    drop_cols = {
        "drive_file_id",
        "current_parent_id",
        "parent_id",
        "folder_id",
        "root_folder_id",
        "web_view_link",
        "webViewLink",
    }
    kept_cols = [col for col in reader.fieldnames if col not in drop_cols]
    if not kept_cols:
        raise ValueError("SOURCE_REGISTRY_NO_KEPT_COLUMNS")
    rows = list(reader)
    provenance_ids: set[str] = set(known_drive_ids or ())
    row_raw_drive_ids: list[Optional[str]] = []
    for row in rows:
        raw_fid = (row.get("drive_file_id") or "").strip() or None
        row_raw_drive_ids.append(raw_fid)
        for dcol in drop_cols:
            dval = (row.get(dcol) or "").strip()
            if dval:
                provenance_ids.add(dval)
        sid_val = (row.get("source_id") or "").strip()
        if sid_val.startswith("drive:"):
            raw_sid = sid_val[len("drive:"):].strip()
            if raw_sid:
                provenance_ids.add(raw_sid)
    prov_re = _compile_provenance_pattern(sorted(provenance_ids))

    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=kept_cols, lineterminator="\n")
    writer.writeheader()
    for row, raw_fid in zip(rows, row_raw_drive_ids):
        out_row: Dict[str, str] = {}
        for col in kept_cols:
            val = row.get(col) or ""
            if col == "source_id":
                stripped = val.strip()
                if stripped.startswith("drive:"):
                    raw_id = stripped[len("drive:"):].strip()
                    val = _short_logical_source_id(raw_id) if raw_id else stripped
                elif raw_fid and stripped == raw_fid and not _SHORT_SOURCE_ID_RE.match(stripped):
                    val = _short_logical_source_id(raw_fid)
                else:
                    val = _scrub_with_pattern(stripped, prov_re)
            else:
                val = _scrub_with_pattern(val, prov_re)
            out_row[col] = val
        writer.writerow(out_row)
    return out.getvalue().encode("utf-8")


def _collect_json_provenance_ids(obj: Any, out_ids: list[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _REDACT_JSON_KEYS and isinstance(v, str) and v.strip():
                out_ids.append(v.strip())
            else:
                _collect_json_provenance_ids(v, out_ids)
    elif isinstance(obj, list):
        for item in obj:
            _collect_json_provenance_ids(item, out_ids)


def _scrub_json_obj(obj: Any, *, prov_re: Optional[re.Pattern[str]] = None) -> Any:
    if isinstance(obj, dict):
        return {
            k: _scrub_json_obj(v, prov_re=prov_re)
            for k, v in obj.items()
            if k not in _REDACT_JSON_KEYS
        }
    if isinstance(obj, list):
        return [_scrub_json_obj(item, prov_re=prov_re) for item in obj]
    if isinstance(obj, str):
        return _scrub_with_pattern(obj, prov_re)
    return obj


def export_consumer_document_index(
    raw_index_bytes: bytes, *, known_drive_ids: Sequence[str] = ()
) -> bytes:
    """Export normalized document index JSON with provenance-based ID & URL redaction."""
    parsed = json.loads(raw_index_bytes.decode("utf-8"))
    provenance_ids: list[str] = list(known_drive_ids or ())
    _collect_json_provenance_ids(parsed, provenance_ids)
    prov_re = _compile_provenance_pattern(provenance_ids)
    scrubbed = _scrub_json_obj(parsed, prov_re=prov_re)
    return (json.dumps(scrubbed, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _extract_consumed_bundle_items(operational_checkpoint: Dict[str, Any]) -> list[Dict[str, Any]]:
    raw_bundles = (operational_checkpoint or {}).get("consumed_bundles")
    items: list[Dict[str, Any]] = []
    if isinstance(raw_bundles, dict):
        for rel_key in sorted(raw_bundles):
            entry = raw_bundles[rel_key]
            if isinstance(entry, dict):
                copied = dict(entry)
                copied.setdefault("_bundle_key", str(rel_key))
                items.append(copied)
    elif isinstance(raw_bundles, list):
        for entry in raw_bundles:
            if isinstance(entry, dict):
                items.append(dict(entry))
    items.sort(key=lambda d: (str(d.get("doc_id") or ""), str(d.get("_bundle_key") or "")))
    return items


def merge_operational_docs_into_consumer_index(
    existing_index_bytes: Optional[bytes],
    operational_checkpoint: Dict[str, Any],
    *,
    known_drive_ids: Sequence[str] = (),
    roster_candidates: Optional[Sequence[Mapping[str, Any]]] = None,
) -> bytes:
    """Idempotently merge operational checkpoint consumed_bundles into the consumer document index."""
    if existing_index_bytes and existing_index_bytes.strip():
        parsed = json.loads(existing_index_bytes.decode("utf-8"))
        if not isinstance(parsed, dict):
            parsed = {"schema_version": 1, "documents": []}
    else:
        parsed = {"schema_version": 1, "documents": []}

    provenance_ids: list[str] = list(known_drive_ids or ())
    _collect_json_provenance_ids(parsed, provenance_ids)
    _collect_json_provenance_ids(operational_checkpoint, provenance_ids)

    raw_docs = parsed.get("documents")
    existing_docs = list(raw_docs) if isinstance(raw_docs, list) else []

    merged_docs: list[Dict[str, Any]] = []
    index_by_doc_id: Dict[str, int] = {}
    for doc in existing_docs:
        if not isinstance(doc, dict):
            continue
        cleaned = dict(doc)
        did = str(cleaned.get("doc_id") or "").strip()
        if did:
            if did in index_by_doc_id:
                continue
            index_by_doc_id[did] = len(merged_docs)
        merged_docs.append(cleaned)

    for bundle in _extract_consumed_bundle_items(operational_checkpoint):
        doc_id = str(bundle.get("doc_id") or "").strip()
        if not doc_id:
            continue
        if roster_candidates:
            bundle = enrich_bundle_matched_persons(
                bundle, roster_candidates=roster_candidates
            )
        raw_sha = bundle.get("raw_sha256") or bundle.get("file_sha256")
        persons = bundle.get("matched_persons")
        evidence_fields: Dict[str, Any] = {
            k: v
            for k, v in bundle.items()
            if k != "_bundle_key" and k not in _REDACT_JSON_KEYS
        }
        evidence_fields.update({
            "doc_id": doc_id,
            "title": bundle.get("title"),
            "canonical_url": bundle.get("canonical_url"),
            "source_tier": bundle.get("source_tier"),
            "completeness": bundle.get("completeness"),
            "published_at": bundle.get("published_at"),
            "content_hash": bundle.get("content_hash"),
            "raw_sha256": raw_sha,
            "file_sha256": bundle.get("file_sha256"),
            "matched_persons": list(persons) if isinstance(persons, list) else [],
            "disclosure_posture": bundle.get("disclosure_posture"),
            "layer": "evidence",
        })
        if doc_id in index_by_doc_id:
            existing_entry = dict(merged_docs[index_by_doc_id[doc_id]])
            existing_entry.update(evidence_fields)
            merged_docs[index_by_doc_id[doc_id]] = existing_entry
        else:
            index_by_doc_id[doc_id] = len(merged_docs)
            merged_docs.append(evidence_fields)

    parsed["schema_version"] = parsed.get("schema_version") or 1
    parsed["documents"] = merged_docs
    parsed["unique_document_count"] = len(merged_docs)
    payload = json.dumps(parsed, ensure_ascii=False).encode("utf-8")
    return export_consumer_document_index(payload, known_drive_ids=provenance_ids)


def _md_cell(value: Any) -> str:
    if value is None:
        return "—"
    text = str(value).strip()
    if not text:
        return "—"
    return text.replace("\r", " ").replace("\n", " ").replace("|", "\\|")


def _classify_digest_bundle(bundle: Dict[str, Any]) -> str:
    title = str(bundle.get("title") or "")
    body = str(bundle.get("body") or "")
    if "外籍球员注册" in title or ("外籍球员注册信息" in body and "| 俱乐部 |" in body):
        return "foreign_registration"
    if "国内球员注册信息" in title:
        return "domestic_registration"
    return "announcement"


def _bundle_sort_key(bundle: Dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(bundle.get("doc_id") or ""),
        str(bundle.get("published_at") or ""),
        str(bundle.get("canonical_url") or ""),
    )


def _render_digest_reg_diff_section(reg_diff: Dict[str, Any]) -> list[str]:
    summary = reg_diff.get("summary") or {}
    old_b = reg_diff.get("old_bundle") or {}
    new_b = reg_diff.get("new_bundle") or {}
    notice = str(
        reg_diff.get("statement_notice")
        or "Candidate diff derived from official registration pages. Statement != Fact. Evidence != Fact. Unknown != 0."
    ).strip()

    lines = [
        "### 2.2 跨季国内球员注册增量观察（2025-2026 → 2026-2027）",
        "",
        f"> **声明边界**: {notice}",
        "",
        f"- **旧赛季基线**: `{old_b.get('title') or '2025-2026'}`（俱乐部 {old_b.get('clubs_count', '—')} 家，球员 {old_b.get('players_count', '—')} 人）",
        f"- **新赛季快照**: `{new_b.get('title') or '2026-2027'}`（俱乐部 {new_b.get('clubs_count', '—')} 家，球员 {new_b.get('players_count', '—')} 人）",
        (
            f"- **增量统计**: 新增 `{summary.get('added_count', 0)}` 人、"
            f"移除 `{summary.get('removed_count', 0)}` 人、"
            f"转会/俱乐部变更 `{summary.get('transferred_count', 0)}` 人、"
            f"合同字段变更 `{summary.get('contract_changed_count', 0)}` 人"
        ),
        "",
    ]

    transferred = reg_diff.get("transferred_players") or []
    lines.append(f"#### 转会 / 俱乐部变更观察（共 {len(transferred)} 条）")
    lines.append("")
    if transferred:
        lines.extend([
            "| 球员 | 原俱乐部 | 新俱乐部 | 原合同类别 | 新合同类别 |",
            "| --- | --- | --- | --- | --- |",
        ])
        for row in transferred:
            lines.append(
                f"| {_md_cell(row.get('player'))} | {_md_cell(row.get('from_club'))} | "
                f"{_md_cell(row.get('to_club'))} | {_md_cell(row.get('old_contract_type'))} | "
                f"{_md_cell(row.get('new_contract_type'))} |"
            )
    else:
        lines.append("- （无转会记录）")
    lines.append("")

    contract_changed = reg_diff.get("contract_changed_players") or []
    lines.append(f"#### 合同与注册属性变更观察（共 {len(contract_changed)} 条）")
    lines.append("")
    if contract_changed:
        lines.extend([
            "| 球员 | 俱乐部 | 变更字段明细 |",
            "| --- | --- | --- |",
        ])
        for row in contract_changed:
            changed_fields = row.get("changed_fields") or {}
            detail_parts = []
            for fname in sorted(changed_fields):
                delta = changed_fields[fname] or {}
                detail_parts.append(f"{fname}: {delta.get('old', '—')} → {delta.get('new', '—')}")
            lines.append(
                f"| {_md_cell(row.get('player'))} | {_md_cell(row.get('club'))} | "
                f"{_md_cell('；'.join(detail_parts))} |"
            )
    else:
        lines.append("- （无合同字段变更记录）")
    lines.append("")

    added = reg_diff.get("added_players") or []
    lines.append(f"#### 新增注册观察（共 {len(added)} 条）")
    lines.append("")
    if added:
        lines.extend([
            "| 球员 | 俱乐部 | 关键字段摘要 |",
            "| --- | --- | --- |",
        ])
        for row in added:
            fields = row.get("fields") or {}
            summary_bits = [
                f"{k}={v}" for k, v in fields.items()
                if k in ("注册类型", "注册子类型", "合同类别", "合同到期日") and v
            ]
            lines.append(
                f"| {_md_cell(row.get('player'))} | {_md_cell(row.get('club'))} | "
                f"{_md_cell('；'.join(summary_bits))} |"
            )
    else:
        lines.append("- （无新增注册记录）")
    lines.append("")

    removed = reg_diff.get("removed_players") or []
    lines.append(f"#### 移出名单观察（共 {len(removed)} 条）")
    lines.append("")
    if removed:
        lines.extend([
            "| 球员 | 原俱乐部 | 关键字段摘要 |",
            "| --- | --- | --- |",
        ])
        for row in removed:
            fields = row.get("fields") or {}
            summary_bits = [
                f"{k}={v}" for k, v in fields.items()
                if k in ("注册类型", "注册子类型", "合同类别", "合同到期日") and v
            ]
            lines.append(
                f"| {_md_cell(row.get('player'))} | {_md_cell(row.get('club'))} | "
                f"{_md_cell('；'.join(summary_bits))} |"
            )
    else:
        lines.append("- （无移出名单记录）")
    lines.append("")
    return lines


def _normalize_date_prefix(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat") and callable(value.isoformat):
        text = str(value.isoformat()).strip()
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    if not text:
        return None
    m = _ISO_DATE_PREFIX_RE.match(text)
    return m.group(1) if m else None


def _ensure_statement_notice(notice: Optional[str]) -> str:
    text = str(notice or "").strip()
    if not text:
        return _DEFAULT_STATEMENT_NOTICE
    required = ("Statement != Fact", "Evidence != Fact", "Unknown != 0")
    if all(tok in text for tok in required):
        return text
    return f"{text} Statement != Fact. Evidence != Fact. Unknown != 0."


_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
_ASCII_LETTER_RE = re.compile(r"[A-Za-z]")
_CLAUSE_BREAK_RE = re.compile(r"[。！？；\n]")

# CJK name-boundary delimiters: prevent 2-char Chinese names (e.g. 王一) from
# matching as unseparated substrings inside longer personal names (e.g. 王一鸣, 小王一)
# while allowing natural Chinese sports/official prose.
_CJK_SAFE_BOTH_CHARS = frozenset(
    "的得地与和及并等在从向对被把将为是也又都已曾于由给同跟携"
)
_CJK_SAFE_LEFT_CHARS = _CJK_SAFE_BOTH_CHARS | frozenset(
    "约含括迎送贺谢布宣报称说请访员援队部"
)
_CJK_SAFE_RIGHT_CHARS = _CJK_SAFE_BOTH_CHARS | frozenset(
    "回返复出转续认入缺受离告官签报称表训集完获砍拿送抢盖助打参赛战伤停休退赴抵留走"
)
_CJK_SAFE_LEFT_BIGRAMS = frozenset({
    "签约", "官宣", "报道", "包含", "包括", "认领", "转会", "续约", "加盟",
    "入选", "回归", "返回", "复出", "告别", "离队", "训练", "集训", "名单",
    "球员", "外援", "前锋", "后卫", "中锋", "队长", "教练", "以及", "关于",
    "针对", "涉及", "乐部", "男篮", "球队", "表示", "确认", "宣布", "公布",
    "通报", "注册", "引进", "租借", "交易", "裁掉", "买断",
})
_CJK_SAFE_RIGHT_BIGRAMS = frozenset({
    "回归", "返回", "复出", "加盟", "转会", "续约", "认领", "入选", "出战",
    "缺席", "受伤", "离队", "告别", "官宣", "签约", "报道", "表示", "训练",
    "集训", "名单", "球员", "外援", "前锋", "后卫", "中锋", "队长", "教练",
    "以及", "完成", "正式", "确认", "注册", "参赛", "登场", "首发", "替补",
    "砍下", "拿到", "贡献", "得到", "表现", "伤停", "休战", "退赛", "退役",
    "离任", "解约", "买断", "租借", "交易", "效力", "宣布", "透露", "接受",
    "参加", "代表", "前往", "抵达", "留队",
})


def _is_cjk_left_boundary_ok(text: str, start: int) -> bool:
    if start <= 0:
        return True
    prev_ch = text[start - 1]
    if not _CJK_CHAR_RE.match(prev_ch):
        return True
    if prev_ch in _CJK_SAFE_LEFT_CHARS:
        return True
    if start >= 2 and text[start - 2 : start] in _CJK_SAFE_LEFT_BIGRAMS:
        return True
    return False


def _is_cjk_right_boundary_ok(text: str, end: int) -> bool:
    if end >= len(text):
        return True
    next_ch = text[end]
    if not _CJK_CHAR_RE.match(next_ch):
        return True
    if next_ch in _CJK_SAFE_RIGHT_CHARS:
        return True
    if end + 2 <= len(text) and text[end : end + 2] in _CJK_SAFE_RIGHT_BIGRAMS:
        return True
    return False


def _is_latin_player_name(value: str) -> bool:
    """Return True when value looks like a Latin/English player name (not a Chinese club name)."""
    text = str(value or "").strip()
    if not text:
        return False
    return bool(_ASCII_LETTER_RE.search(text)) and not bool(_CJK_CHAR_RE.search(text))


def _parse_foreign_registration_rows(bundle: Dict[str, Any]) -> list[Dict[str, str]]:
    """Extract structured foreign player registration rows from a markdown table body.

    Handles both standard 6-column tables and real CBA markdown tables with secondary
    header rows (``| 英文名 | 中文名 | ... |``) and HTML ``rowspan`` continuation rows.
    Resets ``current_club`` across table boundaries (non-table lines, headings,
    markdown table separators, and header rows) and fails closed with
    ``ValueError("FOREIGN_ROW_ORPHAN_CONTINUATION")`` if a continuation row appears
    without an active club in the current table.
    """
    body = str(bundle.get("body") or "")
    rows: list[Dict[str, str]] = []
    current_club: str = ""
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            current_club = ""
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not cells or all(not c or _MD_SEP_CELL_RE.match(c) for c in cells):
            if any(_MD_SEP_CELL_RE.match(c) for c in cells):
                current_club = ""
            continue
        first = cells[0]
        second = cells[1] if len(cells) > 1 else ""
        if first == "俱乐部" or "外籍球员" in cells:
            current_club = ""
            continue
        if first == "英文名" or second == "中文名":
            current_club = ""
            continue

        if len(cells) == 6:
            if cells[5] == "" and _is_latin_player_name(first):
                if not any(cells[1:5]):
                    continue
                if not current_club:
                    raise ValueError("FOREIGN_ROW_ORPHAN_CONTINUATION")
                club = current_club
                player_en, player_zh, nationality, position, jersey_number = cells[:5]
            elif first == "":
                if not _is_latin_player_name(second) or not cells[2]:
                    current_club = ""
                    raise ValueError("FOREIGN_ROW_MALFORMED_CONTINUATION")
                if not current_club:
                    raise ValueError("FOREIGN_ROW_ORPHAN_CONTINUATION")
                club = current_club
                player_en, player_zh, nationality, position, jersey_number = cells[1:6]
            else:
                club, player_en, player_zh, nationality, position, jersey_number = cells[:6]
                if club:
                    current_club = club
        elif len(cells) == 5:
            if _is_latin_player_name(first):
                if not any(cells[1:5]):
                    continue
                if not current_club:
                    raise ValueError("FOREIGN_ROW_ORPHAN_CONTINUATION")
                club = current_club
                player_en, player_zh, nationality, position, jersey_number = cells[:5]
            elif first == "":
                current_club = ""
                raise ValueError("FOREIGN_ROW_MALFORMED_CONTINUATION")
            else:
                club, player_en, nationality, position, jersey_number = cells[:5]
                player_zh = ""
                if club:
                    current_club = club
        else:
            current_club = ""
            raise ValueError("FOREIGN_ROW_MALFORMED_COLUMNS")

        if not club or (not player_en and not player_zh):
            continue
        rows.append({
            "club": club,
            "player_en": player_en,
            "player_zh": player_zh,
            "nationality": nationality,
            "position": position,
            "jersey_number": jersey_number,
        })
    return rows


def _extract_candidate_clubs(candidate: Mapping[str, Any]) -> set[str]:
    clubs: set[str] = set()
    for field in ("current_club", "club_official", "club"):
        val = str(candidate.get(field) or "").strip()
        if val:
            clubs.add(val)
    hist = candidate.get("clubs_history")
    if isinstance(hist, (list, tuple, set)):
        for item in hist:
            val = str(item or "").strip()
            if val:
                clubs.add(val)
    return clubs


def find_boundary_safe_name_spans(
    text: str,
    name: str,
    *,
    occupied_spans: Sequence[tuple[int, int]] = (),
) -> list[tuple[int, int]]:
    """Find non-overlapping, boundary-safe occurrences of ``name`` in ``text``.

    - ASCII/Latin names require non-alphanumeric boundaries.
    - CJK names require non-overlap with ``occupied_spans`` (longest-match first)
      and safe left/right CJK boundaries so shorter names (e.g. ``王一``) cannot
      falsely match inside longer unseparated names (e.g. ``王一鸣``, ``小王一``).
    """
    if not text or not name:
        return []

    def _overlaps(start: int, end: int) -> bool:
        return any(not (end <= s or start >= e) for s, e in occupied_spans)

    if not _CJK_CHAR_RE.search(name) and _ASCII_LETTER_RE.search(name):
        pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])")
        return [m.span() for m in pattern.finditer(text) if not _overlaps(*m.span())]

    spans: list[tuple[int, int]] = []
    start = 0
    step = len(name)
    while True:
        idx = text.find(name, start)
        if idx < 0:
            break
        end = idx + step
        if (
            not _overlaps(idx, end)
            and _is_cjk_left_boundary_ok(text, idx)
            and _is_cjk_right_boundary_ok(text, end)
        ):
            spans.append((idx, end))
            start = end
        else:
            start = idx + 1
    return spans


def _find_name_spans(
    text: str,
    name: str,
    *,
    occupied_spans: Sequence[tuple[int, int]] = (),
) -> list[tuple[int, int]]:
    return find_boundary_safe_name_spans(text, name, occupied_spans=occupied_spans)


def _adjacent_clause_window(text: str, start: int, end: int, radius: int = 24) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    before = text[left:start]
    after = text[end:right]
    # Trim across hard sentence/clause boundaries
    before_breaks = list(_CLAUSE_BREAK_RE.finditer(before))
    if before_breaks:
        before = before[before_breaks[-1].end():]
    after_break = _CLAUSE_BREAK_RE.search(after)
    if after_break:
        after = after[:after_break.start()]
    return before + text[start:end] + after


def enrich_bundle_matched_persons(
    bundle: Mapping[str, Any],
    *,
    roster_candidates: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Deterministically enrich a bundle's ``matched_persons`` across title + body.

    - Scans ``title + "\\n" + body`` for roster candidate names of length >= 2.
    - Preserves existing ``matched_persons`` order and deduplicates deterministically.
    - Homonym safety (Fail-Closed): when a name maps to >= 2 distinct roster candidates,
      refuses to auto-tag unless uniquely disambiguated by an adjacent club qualifier
      matching exactly one candidate.
    """
    out = dict(bundle)
    existing_raw = bundle.get("matched_persons")
    merged: list[str] = []
    seen: set[str] = set()
    if isinstance(existing_raw, (list, tuple)):
        for item in existing_raw:
            name = str(item or "").strip()
            if name and name not in seen:
                seen.add(name)
                merged.append(name)

    if not roster_candidates:
        out["matched_persons"] = merged
        return out

    title = str(bundle.get("title") or "")
    body = str(bundle.get("body") or "")
    text = f"{title}\n{body}"
    if not text.strip():
        out["matched_persons"] = merged
        return out

    # Group distinct candidates by player name (len >= 2)
    by_name: Dict[str, Dict[tuple[str, ...], set[str]]] = {}
    for cand in roster_candidates:
        if not isinstance(cand, Mapping):
            continue
        name = str(
            cand.get("player_name")
            or cand.get("canonical_name")
            or cand.get("player")
            or cand.get("player_zh")
            or ""
        ).strip()
        if len(name) < 2:
            continue
        uid = str(cand.get("player_uid") or "").strip()
        pid = str(cand.get("player_id") or cand.get("provider_player_id") or "").strip()
        clubs = _extract_candidate_clubs(cand)
        if uid:
            cand_key: tuple[str, ...] = ("uid", uid)
        elif pid:
            cand_key = ("pid", pid)
        else:
            primary_club = sorted(clubs)[0] if clubs else ""
            cand_key = ("name_club", name, primary_club)
        by_name.setdefault(name, {}).setdefault(cand_key, set()).update(clubs)

    new_hits: list[tuple[int, str]] = []
    occupied_spans: list[tuple[int, int]] = []
    all_names_sorted = sorted(
        set(by_name.keys()) | {n for n in seen if len(n) >= 2},
        key=lambda n: (-len(n), n),
    )
    for name in all_names_sorted:
        spans = find_boundary_safe_name_spans(
            text, name, occupied_spans=occupied_spans
        )
        if not spans:
            continue
        occupied_spans.extend(spans)
        if name in seen:
            continue
        cand_map = by_name[name]
        if len(cand_map) == 1:
            new_hits.append((spans[0][0], name))
        else:
            # Homonym safety (Fail-Closed): require unique club qualifier in adjacent context
            matched_cand_keys: set[tuple[str, ...]] = set()
            first_disambig_pos: Optional[int] = None
            for start, end in spans:
                window = _adjacent_clause_window(text, start, end)
                for cand_key, clubs in cand_map.items():
                    if any(club and club in window for club in clubs):
                        matched_cand_keys.add(cand_key)
                        if first_disambig_pos is None:
                            first_disambig_pos = start
            if len(matched_cand_keys) == 1 and first_disambig_pos is not None:
                new_hits.append((first_disambig_pos, name))

    new_hits.sort(key=lambda pair: (pair[0], pair[1]))
    for _pos, name in new_hits:
        if name not in seen:
            seen.add(name)
            merged.append(name)

    out["matched_persons"] = merged
    return out


def export_foreign_players_registration_csv(
    *,
    bundles: Sequence[Dict[str, Any]],
    known_drive_ids: Sequence[str] = (),
    observation_horizon: Optional[Mapping[str, Any]] = None,
) -> bytes:
    """Deterministically export foreign player registration snapshots to UTF-8-SIG CSV."""
    fieldnames = [
        "layer",
        "statement_notice",
        "snapshot_title",
        "published_at",
        "club",
        "player_en",
        "player_zh",
        "nationality",
        "position",
        "jersey_number",
        "doc_id",
        "source_tier",
        "raw_sha256",
    ]
    horizon_vals: Optional[tuple[str, str]] = None
    if observation_horizon is not None:
        horizon_vals = _extract_horizon_csv_fields(
            observation_horizon, domain="foreign_registration"
        )
        fieldnames.extend(["observed_until", "coverage_status"])
    prov_re = _compile_provenance_pattern(known_drive_ids)
    sorted_bundles = sorted(
        (dict(b) for b in (bundles or ()) if isinstance(b, dict)),
        key=_bundle_sort_key,
    )
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for b in sorted_bundles:
        if _classify_digest_bundle(b) != "foreign_registration":
            continue
        parsed_rows = _parse_foreign_registration_rows(b)
        snapshot_title = str(b.get("title") or b.get("doc_id") or "")
        published_at = str(b.get("published_at") or "")
        doc_id = str(b.get("doc_id") or "")
        source_tier = str(b.get("source_tier") or "")
        raw_sha256 = str(b.get("raw_sha256") or "")
        for parsed in parsed_rows:
            row = {
                "layer": _EVIDENCE_LAYER_LABEL,
                "statement_notice": _DEFAULT_STATEMENT_NOTICE,
                "snapshot_title": _scrub_with_pattern(snapshot_title, prov_re),
                "published_at": _scrub_with_pattern(published_at, prov_re),
                "club": _scrub_with_pattern(parsed["club"], prov_re),
                "player_en": _scrub_with_pattern(parsed["player_en"], prov_re),
                "player_zh": _scrub_with_pattern(parsed["player_zh"], prov_re),
                "nationality": _scrub_with_pattern(parsed["nationality"], prov_re),
                "position": _scrub_with_pattern(parsed["position"], prov_re),
                "jersey_number": _scrub_with_pattern(parsed["jersey_number"], prov_re),
                "doc_id": _scrub_with_pattern(doc_id, prov_re),
                "source_tier": _scrub_with_pattern(source_tier, prov_re),
                "raw_sha256": _scrub_with_pattern(raw_sha256, prov_re),
            }
            if horizon_vals is not None:
                row["observed_until"] = horizon_vals[0]
                row["coverage_status"] = horizon_vals[1]
            writer.writerow(row)
    return out.getvalue().encode("utf-8-sig")


def export_domestic_registration_diff_csv(
    *,
    reg_diff: Optional[Dict[str, Any]],
    known_drive_ids: Sequence[str] = (),
    observation_horizon: Optional[Mapping[str, Any]] = None,
) -> bytes:
    """Deterministically export cross-season domestic registration diff to UTF-8-SIG CSV."""
    fieldnames = [
        "layer",
        "statement_notice",
        "change_type",
        "player",
        "old_club",
        "new_club",
        "old_contract_type",
        "new_contract_type",
        "change_details",
        "old_snapshot_sha256",
        "new_snapshot_sha256",
    ]
    horizon_vals: Optional[tuple[str, str]] = None
    if observation_horizon is not None:
        horizon_vals = _extract_horizon_csv_fields(
            observation_horizon, domain="domestic_registration"
        )
        fieldnames.extend(["observed_until", "coverage_status"])
    prov_re = _compile_provenance_pattern(known_drive_ids)
    diff = reg_diff if isinstance(reg_diff, dict) else {}
    notice = _ensure_statement_notice(diff.get("statement_notice"))
    old_sha = str((diff.get("old_bundle") or {}).get("raw_sha256") or "")
    new_sha = str((diff.get("new_bundle") or {}).get("raw_sha256") or "")

    rows: list[Dict[str, str]] = []

    def _row_sort_key(r: Dict[str, str]) -> tuple[str, str, str, str]:
        return (r["player"], r["old_club"], r["new_club"], r["change_details"])

    transferred_rows: list[Dict[str, str]] = []
    for item in diff.get("transferred_players") or ():
        if not isinstance(item, dict):
            continue
        old_ct = str(item.get("old_contract_type") or "")
        new_ct = str(item.get("new_contract_type") or "")
        details = f"合同类别: {old_ct} → {new_ct}" if (old_ct or new_ct) else ""
        transferred_rows.append({
            "change_type": "TRANSFERRED",
            "player": str(item.get("player") or ""),
            "old_club": str(item.get("from_club") or ""),
            "new_club": str(item.get("to_club") or ""),
            "old_contract_type": old_ct,
            "new_contract_type": new_ct,
            "change_details": details,
        })
    rows.extend(sorted(transferred_rows, key=_row_sort_key))

    changed_rows: list[Dict[str, str]] = []
    for item in diff.get("contract_changed_players") or ():
        if not isinstance(item, dict):
            continue
        changed_fields = item.get("changed_fields") or {}
        ct_delta = changed_fields.get("合同类别") if isinstance(changed_fields, dict) else None
        old_ct = str((ct_delta or {}).get("old") or "") if isinstance(ct_delta, dict) else ""
        new_ct = str((ct_delta or {}).get("new") or "") if isinstance(ct_delta, dict) else ""
        detail_parts = []
        if isinstance(changed_fields, dict):
            for fname in sorted(changed_fields):
                delta = changed_fields[fname] or {}
                if isinstance(delta, dict):
                    detail_parts.append(f"{fname}: {delta.get('old', '—')} → {delta.get('new', '—')}")
        club = str(item.get("club") or "")
        changed_rows.append({
            "change_type": "CONTRACT_CHANGED",
            "player": str(item.get("player") or ""),
            "old_club": club,
            "new_club": club,
            "old_contract_type": old_ct,
            "new_contract_type": new_ct,
            "change_details": "；".join(detail_parts),
        })
    rows.extend(sorted(changed_rows, key=_row_sort_key))

    added_rows: list[Dict[str, str]] = []
    for item in diff.get("added_players") or ():
        if not isinstance(item, dict):
            continue
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        detail_parts = [f"{k}={fields[k]}" for k in sorted(fields) if fields[k]]
        added_rows.append({
            "change_type": "ADDED",
            "player": str(item.get("player") or ""),
            "old_club": "",
            "new_club": str(item.get("club") or ""),
            "old_contract_type": "",
            "new_contract_type": str(fields.get("合同类别") or ""),
            "change_details": "；".join(detail_parts),
        })
    rows.extend(sorted(added_rows, key=_row_sort_key))

    removed_rows: list[Dict[str, str]] = []
    for item in diff.get("removed_players") or ():
        if not isinstance(item, dict):
            continue
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        detail_parts = [f"{k}={fields[k]}" for k in sorted(fields) if fields[k]]
        removed_rows.append({
            "change_type": "REMOVED",
            "player": str(item.get("player") or ""),
            "old_club": str(item.get("club") or ""),
            "new_club": "",
            "old_contract_type": str(fields.get("合同类别") or ""),
            "new_contract_type": "",
            "change_details": "；".join(detail_parts),
        })
    rows.extend(sorted(removed_rows, key=_row_sort_key))

    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for r in rows:
        out_row = {
            "layer": _EVIDENCE_LAYER_LABEL,
            "statement_notice": _scrub_with_pattern(notice, prov_re),
            "change_type": r["change_type"],
            "player": _scrub_with_pattern(r["player"], prov_re),
            "old_club": _scrub_with_pattern(r["old_club"], prov_re),
            "new_club": _scrub_with_pattern(r["new_club"], prov_re),
            "old_contract_type": _scrub_with_pattern(r["old_contract_type"], prov_re),
            "new_contract_type": _scrub_with_pattern(r["new_contract_type"], prov_re),
            "change_details": _scrub_with_pattern(r["change_details"], prov_re),
            "old_snapshot_sha256": _scrub_with_pattern(old_sha, prov_re),
            "new_snapshot_sha256": _scrub_with_pattern(new_sha, prov_re),
        }
        if horizon_vals is not None:
            out_row["observed_until"] = horizon_vals[0]
            out_row["coverage_status"] = horizon_vals[1]
        writer.writerow(out_row)
    return out.getvalue().encode("utf-8-sig")


def _is_protected_operational_announcement(
    bundle: Dict[str, Any], *, event_class: str, combined: str, profile: Dict[str, Any]
) -> bool:
    if event_class in profile["protected_event_classes"]:
        return True
    return any(kw in combined for kw in profile["active_competitive_keywords"])


def _explicit_historical_pinned_reason(
    bundle: Dict[str, Any], *, event_class: str, combined: str, profile: Dict[str, Any]
) -> Optional[str]:
    if bundle.get("is_pinned") is True or bundle.get("pinned") is True:
        return "out_of_window_historical_pinned:pinned_flag"
    if event_class in profile["historical_pinned_event_classes"]:
        return "out_of_window_historical_pinned:historical_summary"
    for kw in profile["historical_pinned_keywords"]:
        if kw in combined:
            return f"out_of_window_historical_pinned:{kw}"
    for kw in profile["pinned_marker_keywords"]:
        if kw in combined:
            return f"out_of_window_historical_pinned:{kw}"
    return None


def _denoising_filter_reason(
    bundle: Dict[str, Any], *, active_window_start: str, profile: Dict[str, Any]
) -> Optional[str]:
    """Return a non-empty filter reason if an announcement should be excluded from curated .md."""
    kind = _classify_digest_bundle(bundle)
    if kind in ("foreign_registration", "domestic_registration"):
        return None

    title = str(bundle.get("title") or "")
    body = str(bundle.get("body") or "")
    event_class = str(bundle.get("event_class") or "")
    combined = f"{title}\n{body}"

    if event_class in profile["noise_event_classes"]:
        return "noise_social_interaction"
    for kw in profile["noise_keywords"]:
        if kw in combined:
            return f"noise_keyword:{kw}"
    if any(kw in combined for kw in profile["condolence_keywords"]) and not any(
        kw in combined for kw in profile["condolence_exempt_keywords"]
    ):
        return "noise_non_active_condolence"

    pinned_reason = _explicit_historical_pinned_reason(
        bundle, event_class=event_class, combined=combined, profile=profile
    )
    pub_date = _normalize_date_prefix(bundle.get("published_at"))
    if pub_date is not None:
        if pub_date < active_window_start:
            if pinned_reason is not None:
                return pinned_reason
            if _is_protected_operational_announcement(
                bundle, event_class=event_class, combined=combined, profile=profile
            ):
                return None
            return f"out_of_window_published_at:{pub_date}<{active_window_start}"
        for kw in profile["historical_pinned_keywords"]:
            if kw in combined:
                return f"out_of_window_historical_pinned:{kw}"
    elif pinned_reason is not None:
        return pinned_reason

    return None


def filter_curated_operational_announcements(
    bundles: Sequence[Dict[str, Any]],
    *,
    active_window_start: str = "2025-07-01",
    curation_profile: Optional[Dict[str, Any]] = None,
    curation_profile_path: Optional[str | Path] = None,
    instance: Any = None,
) -> Dict[str, Any]:
    """Deterministically partition operational bundles into admitted and filtered sets."""
    profile = _resolve_curation_profile(
        curation_profile=curation_profile,
        curation_profile_path=curation_profile_path,
        instance=instance,
    )
    sorted_bundles = sorted(
        (dict(b) for b in (bundles or ()) if isinstance(b, dict)),
        key=_bundle_sort_key,
    )
    admitted: list[Dict[str, Any]] = []
    filtered: list[Dict[str, Any]] = []
    reasons_by_doc_id: Dict[str, str] = {}
    for b in sorted_bundles:
        reason = _denoising_filter_reason(
            b, active_window_start=active_window_start, profile=profile
        )
        if reason is None:
            admitted.append(b)
        else:
            filtered.append(b)
            doc_id = str(b.get("doc_id") or "")
            if doc_id:
                reasons_by_doc_id[doc_id] = reason
    return {
        "admitted": admitted,
        "filtered": filtered,
        "total_count": len(sorted_bundles),
        "admitted_count": len(admitted),
        "filtered_count": len(filtered),
        "reasons_by_doc_id": reasons_by_doc_id,
    }


def _truncate_utf8_bytes(text: str, max_bytes: int, suffix: str = "…") -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    suffix_bytes = suffix.encode("utf-8")
    if max_bytes <= len(suffix_bytes):
        return suffix_bytes[: max(0, max_bytes)].decode("utf-8", errors="ignore")
    budget = max_bytes - len(suffix_bytes)
    trimmed = raw[:budget].decode("utf-8", errors="ignore").rstrip()
    return trimmed + suffix


def _render_compact_foreign_quick_index_lines(
    foreign_bundles: Sequence[Dict[str, Any]],
    *,
    max_bytes: Optional[int] = None,
) -> list[str]:
    """Render one compact sorted line per club for v2.3.1+ collector digest Section 1.

    When ``max_bytes`` is provided, deterministically compacts/truncates overly long
    club/player entries so the quick index strictly stays within ``max_bytes``.
    """
    by_club: Dict[str, list[Dict[str, str]]] = {}
    for b in foreign_bundles:
        for row in _parse_foreign_registration_rows(b):
            club = str(row.get("club") or "").strip()
            if not club:
                continue
            by_club.setdefault(club, []).append(row)
    if not by_club:
        return []

    header_lines = [
        "### 1.1 各俱乐部外籍球员速查索引（按俱乐部排序）",
        "",
    ]
    sorted_clubs = sorted(by_club)

    def _build_lines(*, compact: bool) -> list[str]:
        out_lines = list(header_lines)
        for club in sorted_clubs:
            club_rows = by_club[club]
            club_label = _md_cell(club)
            if compact and len(club_label) > 24:
                club_label = club_label[:23].rstrip() + "…"
            player_labels: list[str] = []
            for r in club_rows:
                zh_raw = str(r.get("player_zh") or "").strip()
                en_raw = str(r.get("player_en") or "").strip()
                zh = _md_cell(zh_raw) if zh_raw else ""
                en = _md_cell(en_raw) if en_raw else ""
                if compact:
                    name_part = zh or en or "—"
                    if len(name_part) > 24:
                        name_part = name_part[:23].rstrip() + "…"
                    player_labels.append(name_part)
                else:
                    if zh and en:
                        name_part = f"{zh} / {en}"
                    else:
                        name_part = zh or en or "—"
                    jersey = str(r.get("jersey_number") or "").strip()
                    pos = str(r.get("position") or "").strip()
                    meta_bits: list[str] = []
                    if jersey:
                        meta_bits.append(f"#{jersey}")
                    if pos:
                        meta_bits.append(pos)
                    if meta_bits:
                        player_labels.append(f"{name_part} ({' '.join(meta_bits)})")
                    else:
                        player_labels.append(name_part)
            out_lines.append(
                f"- **{club_label}（{len(club_rows)}人）**: {' · '.join(player_labels)}"
            )
        out_lines.append("")
        return out_lines

    def _lines_bytes(lines_seq: Sequence[str]) -> int:
        return len(("\n".join(lines_seq) + "\n").encode("utf-8"))

    full_lines = _build_lines(compact=False)
    if max_bytes is None or _lines_bytes(full_lines) <= max_bytes:
        return full_lines

    compact_lines = _build_lines(compact=True)
    if _lines_bytes(compact_lines) <= max_bytes:
        return compact_lines

    # Stage 2: Deterministic per-club byte-budgeted truncation
    club_entries = compact_lines[len(header_lines) : -1]
    base_overhead = _lines_bytes(header_lines + [""])
    omission_template = (
        f"- …（其余 {{n}} 家俱乐部详见 `{FOREIGN_REGISTRATION_CSV_FILENAME}`）"
    )
    omission_reserve = len(
        (omission_template.format(n=len(club_entries)) + "\n").encode("utf-8")
    )
    avail = max_bytes - base_overhead - omission_reserve
    if avail <= 0:
        return []

    per_club_cap = max(32, avail // max(1, len(club_entries)))
    kept: list[str] = []
    used = base_overhead
    for entry in club_entries:
        candidate = _truncate_utf8_bytes(entry, per_club_cap)
        cand_bytes = len((candidate + "\n").encode("utf-8"))
        if used + cand_bytes + omission_reserve > max_bytes:
            break
        kept.append(candidate)
        used += cand_bytes

    if not kept:
        return []

    result = list(header_lines) + kept
    omitted = len(club_entries) - len(kept)
    if omitted > 0:
        note = omission_template.format(n=omitted)
        if _lines_bytes(result + [note, ""]) <= max_bytes:
            result.append(note)
    result.append("")
    return result


def _render_compact_curated_digest_md(
    *,
    bundles: Sequence[Dict[str, Any]],
    reg_diff: Optional[Dict[str, Any]],
    release_id: str,
    engine_sha: str,
    known_drive_ids: Sequence[str],
    max_bytes: int,
    active_window_start: str,
    profile: Dict[str, Any],
    observation_horizon: Optional[Mapping[str, Any]] = None,
    include_foreign_quick_index: bool = False,
    roster_candidates: Optional[Sequence[Mapping[str, Any]]] = None,
) -> bytes:
    """Render <25KB curated collector digest with CSV routing & fail-closed size gate."""
    filtered_res = filter_curated_operational_announcements(
        bundles, active_window_start=active_window_start, curation_profile=profile
    )
    excerpt_cap = int(profile["compact_announcement_excerpt_chars"])
    admitted = filtered_res["admitted"]
    if roster_candidates:
        admitted = [
            enrich_bundle_matched_persons(b, roster_candidates=roster_candidates)
            for b in admitted
        ]
    total_count = filtered_res["total_count"]
    admitted_count = filtered_res["admitted_count"]
    filtered_count = filtered_res["filtered_count"]

    foreign_bundles: list[Dict[str, Any]] = []
    domestic_bundles: list[Dict[str, Any]] = []
    announcement_bundles: list[Dict[str, Any]] = []
    for b in admitted:
        kind = _classify_digest_bundle(b)
        if kind == "foreign_registration":
            foreign_bundles.append(b)
        elif kind == "domestic_registration":
            domestic_bundles.append(b)
        else:
            announcement_bundles.append(b)

    lines: list[str] = [
        f"# CBA 官方采集动态与注册增量摘要（{release_id} / CURRENT）",
        "",
        f"- **发布版本**: `{release_id}`",
        f"- **代码基线 Commit**: `{engine_sha or 'unknown'}`",
        f"- **数据层级声明**: `{_EVIDENCE_LAYER_LABEL}`（来源自受控官方采集束 `acquisition_bundle_v1` 与跨季增量比对，**非**已晋升的 `canonical` 事实表）",
        "- **核心原则**: `Statement != Fact. Evidence != Fact. Unknown != 0.`",
    ]
    if observation_horizon is not None:
        lines.extend(render_observation_horizon_header(observation_horizon).splitlines())
    lines.extend([
        (
            f"- **采集文档入池与降噪统计**: 总计 **{total_count}** 份，"
            f"精华摘要准入 **{admitted_count}** 份"
            f"（外籍注册快照 {len(foreign_bundles)} 份、国内注册快照 {len(domestic_bundles)} 份、精选官方公告 {len(announcement_bundles)} 份），"
            f"降噪过滤 **{filtered_count}** 份"
            f"（全量 {total_count} 份文档脱敏元数据均 100% 完整保留于 `{CONSUMER_DOC_INDEX_FILENAME}`）"
        ),
        "- **结构化大表直读路由（推荐 Python CSV 读取）**:",
        f"  - 外籍球员官方注册快照明细：`{FOREIGN_REGISTRATION_CSV_FILENAME}`（UTF-8-SIG，`{_EVIDENCE_LAYER_LABEL}`）",
        f"  - 跨季国内球员注册增量明细：`{DOMESTIC_REG_DIFF_CSV_FILENAME}`（UTF-8-SIG，`{_EVIDENCE_LAYER_LABEL}`）",
        "",
        "## 1. 2026-2027 外籍球员注册官方快照概览",
        "",
    ])

    quick_index_insert_at: Optional[int] = None
    if foreign_bundles:
        total_foreign_rows = 0
        for b in foreign_bundles:
            parsed_count = len(_parse_foreign_registration_rows(b))
            total_foreign_rows += parsed_count
            lines.append(
                f"- **{_md_cell(b.get('title') or b.get('doc_id'))}**"
                f"（`doc_id`: `{b.get('doc_id')}` | `published_at`: `{b.get('published_at') or '—'}`"
                f" | 解析外籍球员条目：**{parsed_count}** 条 | `raw_sha256`: `{b.get('raw_sha256') or '—'}`）"
            )
        lines.extend([
            f"- 完整结构化外籍球员明细（共 **{total_foreign_rows}** 条）已拆分至 `{FOREIGN_REGISTRATION_CSV_FILENAME}`，请优先使用 Python CSV 直接读取。",
            "",
        ])
        if include_foreign_quick_index:
            quick_index_insert_at = len(lines)
    else:
        lines.extend(["- （本次采集未包含外籍球员注册快照）", ""])

    lines.extend([
        "## 2. 近三赛季国内球员注册快照索引与跨季增量概览",
        "",
        "### 2.1 国内球员注册官方快照元数据索引（正文大表已去重，完整事实以 MASTER 总表为准）",
        "",
    ])
    if domestic_bundles:
        lines.extend([
            "| doc_id | 标题 | 来源等级 | 发布日期 | raw_sha256 |",
            "| --- | --- | --- | --- | --- |",
        ])
        for b in domestic_bundles:
            lines.append(
                f"| `{_md_cell(b.get('doc_id'))}` | {_md_cell(b.get('title'))} | "
                f"`{_md_cell(b.get('source_tier'))}` | {_md_cell(b.get('published_at'))} | "
                f"`{_md_cell(b.get('raw_sha256'))}` |"
            )
        lines.append("")
    else:
        lines.extend(["- （本次采集未包含国内球员注册快照）", ""])

    lines.append("### 2.2 跨季国内球员注册增量观察统计（2025-2026 → 2026-2027）")
    lines.append("")
    if isinstance(reg_diff, dict):
        summary = reg_diff.get("summary") or {}
        old_b = reg_diff.get("old_bundle") or {}
        new_b = reg_diff.get("new_bundle") or {}
        lines.extend([
            f"- **旧赛季基线**: `{old_b.get('title') or '2025-2026'}`（俱乐部 {old_b.get('clubs_count', '—')} 家，球员 {old_b.get('players_count', '—')} 人）",
            f"- **新赛季快照**: `{new_b.get('title') or '2026-2027'}`（俱乐部 {new_b.get('clubs_count', '—')} 家，球员 {new_b.get('players_count', '—')} 人）",
            (
                f"- **增量统计**: 新增 `{summary.get('added_count', 0)}` 人、"
                f"移除 `{summary.get('removed_count', 0)}` 人、"
                f"转会/俱乐部变更 `{summary.get('transferred_count', 0)}` 人、"
                f"合同字段变更 `{summary.get('contract_changed_count', 0)}` 人"
            ),
            f"- 完整结构化跨季增量明细（`TRANSFERRED` / `CONTRACT_CHANGED` / `ADDED` / `REMOVED`）已拆分至 `{DOMESTIC_REG_DIFF_CSV_FILENAME}`，请优先使用 Python CSV 直接读取。",
            "",
        ])
    else:
        lines.extend(["- （本次采集未包含跨季国内球员注册增量比对）", ""])

    lines.extend([
        f"## 3. 精选官方公告与赛事动态（降噪后 {len(announcement_bundles)} 份）",
        "",
    ])
    if announcement_bundles:
        for b in announcement_bundles:
            persons = b.get("matched_persons") or []
            persons_str = ", ".join(str(p) for p in persons) if persons else "—"
            raw_body = " ".join(str(b.get("body") or "").split())
            excerpt = (
                raw_body
                if len(raw_body) <= excerpt_cap
                else raw_body[:excerpt_cap].rstrip() + "…"
            )
            lines.extend([
                f"### {_md_cell(b.get('title') or b.get('doc_id'))}",
                (
                    f"- `doc_id`: `{b.get('doc_id')}` | `source_tier`: `{b.get('source_tier')}` | "
                    f"`event_class`: `{b.get('event_class')}` | `published_at`: `{b.get('published_at') or '—'}` | "
                    f"`matched_persons`: {persons_str}"
                ),
                f"- `canonical_url`: {b.get('canonical_url') or '—'} | `raw_sha256`: `{b.get('raw_sha256') or '—'}`",
                f"- 摘要：{excerpt or '—'}",
                "",
            ])
    else:
        lines.extend(["- （降噪后暂无精选官方公告）", ""])

    def _finalize(candidate_lines: Sequence[str]) -> tuple[str, bytes]:
        text_out = scrub_raw_drive_ids_for_consumer(
            "\n".join(candidate_lines).rstrip() + "\n",
            known_drive_ids=known_drive_ids,
        )
        if not text_out.endswith("\n"):
            text_out += "\n"
        text_out += "\nend_marker: true\n"
        return text_out, text_out.encode("utf-8")

    if quick_index_insert_at is not None:
        full_quick = _render_compact_foreign_quick_index_lines(foreign_bundles)
        candidate_lines = (
            lines[:quick_index_insert_at] + full_quick + lines[quick_index_insert_at:]
        )
        rendered, rendered_bytes = _finalize(candidate_lines)
        if len(rendered_bytes) >= max_bytes:
            base_rendered, base_bytes = _finalize(lines)
            index_budget = max_bytes - 1 - len(base_bytes) - 16
            if index_budget > 0:
                budgeted_quick = _render_compact_foreign_quick_index_lines(
                    foreign_bundles, max_bytes=index_budget
                )
                candidate_lines = (
                    lines[:quick_index_insert_at]
                    + budgeted_quick
                    + lines[quick_index_insert_at:]
                )
                rendered, rendered_bytes = _finalize(candidate_lines)
            else:
                rendered, rendered_bytes = base_rendered, base_bytes
    else:
        rendered, rendered_bytes = _finalize(lines)

    if observation_horizon is not None:
        assert_no_exaggerated_coverage_claims(rendered, observation_horizon)
    if len(rendered_bytes) >= max_bytes:
        raise ValueError(
            f"COLLECTOR_DIGEST_SIZE_LIMIT_EXCEEDED: {len(rendered_bytes)} >= {max_bytes}"
        )
    return rendered_bytes


def render_collector_official_digest_md(
    *,
    bundles: Sequence[Dict[str, Any]],
    reg_diff: Optional[Dict[str, Any]] = None,
    release_id: str = "v2.2.0-1",
    engine_sha: str = "",
    known_drive_ids: Sequence[str] = (),
    compact_curated: bool = False,
    max_bytes: int = COLLECTOR_DIGEST_MAX_BYTES,
    active_window_start: str = "2025-07-01",
    curation_profile: Optional[Dict[str, Any]] = None,
    curation_profile_path: Optional[str | Path] = None,
    instance: Any = None,
    observation_horizon: Optional[Mapping[str, Any]] = None,
    include_foreign_quick_index: bool = False,
    roster_candidates: Optional[Sequence[Mapping[str, Any]]] = None,
) -> bytes:
    """Deterministically render governed collector official digest Markdown in evidence layer."""
    if compact_curated:
        profile = _resolve_curation_profile(
            curation_profile=curation_profile,
            curation_profile_path=curation_profile_path,
            instance=instance,
        )
        return _render_compact_curated_digest_md(
            bundles=bundles,
            reg_diff=reg_diff,
            release_id=release_id,
            engine_sha=engine_sha,
            known_drive_ids=known_drive_ids,
            max_bytes=max_bytes,
            active_window_start=active_window_start,
            profile=profile,
            observation_horizon=observation_horizon,
            include_foreign_quick_index=include_foreign_quick_index,
            roster_candidates=roster_candidates,
        )
    sorted_bundles = sorted((dict(b) for b in (bundles or ()) if isinstance(b, dict)), key=_bundle_sort_key)
    if roster_candidates:
        sorted_bundles = [
            enrich_bundle_matched_persons(b, roster_candidates=roster_candidates)
            for b in sorted_bundles
        ]

    foreign_bundles: list[Dict[str, Any]] = []
    domestic_bundles: list[Dict[str, Any]] = []
    announcement_bundles: list[Dict[str, Any]] = []
    for b in sorted_bundles:
        kind = _classify_digest_bundle(b)
        if kind == "foreign_registration":
            foreign_bundles.append(b)
        elif kind == "domestic_registration":
            domestic_bundles.append(b)
        else:
            announcement_bundles.append(b)

    lines: list[str] = [
        f"# CBA 官方采集动态与注册增量摘要（{release_id} / CURRENT）",
        "",
        f"- **发布版本**: `{release_id}`",
        f"- **代码基线 Commit**: `{engine_sha or 'unknown'}`",
        "- **数据层级声明**: `evidence / candidate_observation`（来源自受控官方采集束 `acquisition_bundle_v1` 与跨季增量比对，**非**已晋升的 `canonical` 事实表）",
        "- **核心原则**: `Statement != Fact. Evidence != Fact. Unknown != 0.`",
    ]
    if observation_horizon is not None:
        lines.extend(render_observation_horizon_header(observation_horizon).splitlines())
    lines.extend([
        f"- **纳入采集文档总数**: **{len(sorted_bundles)}** 份（外籍注册快照 {len(foreign_bundles)} 份、国内注册快照 {len(domestic_bundles)} 份、官方公告与赛事动态 {len(announcement_bundles)} 份）",
        "",
        "## 1. 2026-2027 外籍球员注册官方快照",
        "",
    ])

    if foreign_bundles:
        for b in foreign_bundles:
            lines.extend([
                f"### {_md_cell(b.get('title') or b.get('doc_id'))}",
                "",
                f"- `doc_id`: `{b.get('doc_id')}` | `source_tier`: `{b.get('source_tier')}` | `published_at`: `{b.get('published_at')}` | `canonical_url`: {b.get('canonical_url') or '—'} | `raw_sha256`: `{b.get('raw_sha256') or '—'}`",
                "",
                str(b.get("body") or "").strip(),
                "",
            ])
    else:
        lines.extend(["- （本次采集未包含外籍球员注册快照）", ""])

    lines.extend([
        "## 2. 近三赛季国内球员注册快照与跨季增量观察",
        "",
        "### 2.1 国内球员注册官方快照索引",
        "",
    ])
    if domestic_bundles:
        lines.extend([
            "| doc_id | 标题 | 来源等级 | 发布日期 | canonical_url | raw_sha256 |",
            "| --- | --- | --- | --- | --- | --- |",
        ])
        for b in domestic_bundles:
            lines.append(
                f"| `{_md_cell(b.get('doc_id'))}` | {_md_cell(b.get('title'))} | "
                f"`{_md_cell(b.get('source_tier'))}` | {_md_cell(b.get('published_at'))} | "
                f"{_md_cell(b.get('canonical_url'))} | `{_md_cell(b.get('raw_sha256'))}` |"
            )
        lines.append("")
        for b in domestic_bundles:
            body = str(b.get("body") or "").strip()
            if body:
                excerpt = body if len(body) <= 1200 else body[:1200].rstrip() + "\n...（完整原文存于受控 document_archive）"
                lines.extend([
                    f"#### {_md_cell(b.get('title') or b.get('doc_id'))}",
                    "",
                    excerpt,
                    "",
                ])
    else:
        lines.extend(["- （本次采集未包含国内球员注册快照）", ""])

    if isinstance(reg_diff, dict):
        lines.extend(_render_digest_reg_diff_section(reg_diff))

    lines.extend([
        "## 3. 官方公告与赛事动态全量索引及摘录",
        "",
        "| doc_id | 标题 | 来源等级 | 事件分类 | 发布日期 | 采集时间 | canonical_url | raw_sha256 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ])
    for b in sorted_bundles:
        lines.append(
            f"| `{_md_cell(b.get('doc_id'))}` | {_md_cell(b.get('title'))} | "
            f"`{_md_cell(b.get('source_tier'))}` | {_md_cell(b.get('event_class'))} | "
            f"{_md_cell(b.get('published_at'))} | {_md_cell(b.get('acquired_at'))} | "
            f"{_md_cell(b.get('canonical_url'))} | `{_md_cell(b.get('raw_sha256'))}` |"
        )
    lines.append("")

    for b in announcement_bundles:
        persons = b.get("matched_persons") or []
        persons_str = ", ".join(str(p) for p in persons) if persons else "—"
        lines.extend([
            f"### {_md_cell(b.get('title') or b.get('doc_id'))}",
            "",
            (
                f"- `doc_id`: `{b.get('doc_id')}` | `source_tier`: `{b.get('source_tier')}` | "
                f"`event_class`: `{b.get('event_class')}` | `upstream_origin`: `{b.get('upstream_origin') or '—'}` | "
                f"`published_at`: `{b.get('published_at')}` | `acquired_at`: `{b.get('acquired_at')}`"
            ),
            f"- `canonical_url`: {b.get('canonical_url') or '—'} | `raw_sha256`: `{b.get('raw_sha256') or '—'}` | `matched_persons`: {persons_str}",
            "",
            str(b.get("body") or "").strip(),
            "",
        ])

    rendered = scrub_raw_drive_ids_for_consumer(
        "\n".join(lines).rstrip() + "\n",
        known_drive_ids=known_drive_ids,
    )
    if not rendered.endswith("\n"):
        rendered += "\n"
    rendered += "\nend_marker: true\n"
    if observation_horizon is not None:
        assert_no_exaggerated_coverage_claims(rendered, observation_horizon)
    return rendered.encode("utf-8")


def _safe_ascii_key(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_").lower() or "item"


def build_promoted_research_slice_artifacts(
    fixture: Dict[str, Any],
    *,
    player_uid: str,
    player_name: str,
    player_id: str,
    season: str,
    match_type: str = "REGULAR",
    scope: str = "WHOLE_SEASON",
    passage_label: str = "主要场均数据表",
    target_domain: str = "fixture_target",
    provider: str = "CBA_OFFICIAL",
    competition: str = "CBA",
    metric: str = "points_per_game",
    additional_match_types: Sequence[str] = (),
    active_league_season: Optional[str] = None,
    enable_v232_card_pipeline: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """Build self-describing CURRENT research card, slice, and excerpt artifacts via ConsumeSession."""
    players = fixture["players"]
    records = fixture["stats_records"]
    raw_passages = fixture["passages"]
    fact = fixture.get("fact")
    claims = fixture.get("claims") or []
    source_objects = fixture.get("source_objects") or []
    if not players or not records or not raw_passages:
        raise ValueError("PROMOTED_RESEARCH_SLICE_INPUT_INCOMPLETE")

    passages: list[Dict[str, Any]] = []
    for raw_p in raw_passages:
        if not isinstance(raw_p, dict):
            passages.append(raw_p)
            continue
        if raw_p.get("digest_kind") == "KC_PASSAGE_SHA256":
            passages.append(raw_p)
            continue
        p_text = raw_p.get("text")
        p_digest = raw_p.get("digest")
        if (
            isinstance(p_text, str)
            and (
                p_digest is None
                or p_digest == hashlib.sha256(p_text.encode("utf-8")).hexdigest()
            )
            and all(raw_p.get(k) for k in ("source_id", "revision", "locator"))
        ):
            sealed = build_routed_passage(
                source_id=str(raw_p["source_id"]),
                revision=str(raw_p["revision"]),
                locator=str(raw_p["locator"]),
                text=p_text,
                player_uid=raw_p.get("player_uid"),
                aliases=raw_p.get("aliases"),
                title=raw_p.get("title"),
            )
            passages.append(sealed)
        else:
            passages.append(raw_p)

    authorized_players = {player_uid}
    authorized_sources = {p["source_id"] for p in passages}
    authorized_objects = {
        o["object_id"]
        for o in source_objects
        if o.get("target_domain") == target_domain and o.get("rights") == "APPROVED"
    }
    if not authorized_objects:
        authorized_objects = set(authorized_sources)

    ordered_match_types: list[str] = [match_type]
    for extra_mt in additional_match_types or ():
        if isinstance(extra_mt, str) and extra_mt and extra_mt not in ordered_match_types:
            ordered_match_types.append(extra_mt)

    probe = ConsumeSession(
        target_domain=target_domain,
        authorized_players=authorized_players,
        authorized_sources=authorized_sources,
        authorized_objects=authorized_objects,
    )
    entries = []
    for mt in ordered_match_types:
        probed = probe.consume_slice(
            records,
            player_uid=player_uid,
            season=season,
            match_type=mt,
            scope=scope,
            provider=provider,
            competition=competition,
        )
        if "payload" not in probed or probed["payload"].get("status") != "OK":
            raise ValueError(f"PROMOTED_RESEARCH_SLICE_STATS_NOT_OK:{mt}")
        entries.append({
            "entry_id": f"slice:{player_uid}|{season}|{mt}|{scope}",
            "sha256": _content_digest(probed["payload"]),
            "authority": "derived",
            "rights": "public",
            "source_id": "promoted-research-slice",
        })
    for p in passages:
        entries.append({
            "entry_id": "passage:" + p["source_id"],
            "sha256": p["digest"],
            "authority": "canonical",
            "rights": "public",
            "source_id": p["source_id"],
        })

    snap = build_snapshot(
        entries=entries,
        target_domain=target_domain,
        capabilities=["stats", "identity", "document", "answer"],
        build_provenance="consumer_surface",
        build_time="2026-11-17T00:00:00+00:00",
        data_release="PROMOTED_CURRENT_SLICE",
        engine_candidate="v2.1.5",
        build_code="v2.1.5",
    )
    session = ConsumeSession(
        target_domain=target_domain,
        authorized_players=authorized_players,
        authorized_sources=authorized_sources,
        authorized_objects=authorized_objects,
    )
    session.bind_current(snap)
    session.register_passages(passages)
    sliced_by_mt: Dict[str, Dict[str, Any]] = {}
    for mt in ordered_match_types:
        sliced = session.consume_slice(
            records,
            player_uid=player_uid,
            season=season,
            match_type=mt,
            scope=scope,
            provider=provider,
            competition=competition,
        )
        if "payload" not in sliced or sliced["payload"].get("status") != "OK":
            raise ValueError(f"PROMOTED_RESEARCH_SLICE_STATS_NOT_OK:{mt}")
        sliced_by_mt[mt] = sliced

    target_passage = next(
        (p for p in passages if p.get("player_uid") == player_uid),
        passages[0],
    )
    binding = session.current_binding()
    chain = evidence_chain(
        receipt_id=binding["receipt_binding"],
        snapshot_id=snap.snapshot_id,
        build_version=snap.build_code,
        source_digest=target_passage["digest"],
        authorization_basis=binding["authorization_basis"],
    )
    cited = session.consume_citation(
        passage=target_passage,
        source_text=target_passage["text"],
        chain=chain,
    )

    guarantee = None
    fact_source = (fact.get("source_id") or fact.get("source")) if isinstance(fact, dict) else None
    fact_passage = next((p for p in passages if p.get("source_id") == fact_source), None)
    if (
        isinstance(fact, dict)
        and isinstance(fact.get("fact_id"), str)
        and fact.get("fact_id")
        and isinstance(fact.get("fact_version"), str)
        and fact.get("fact_version")
        and fact_passage is not None
    ):
        guarantee = build_fact_guarantee(
            fact_id=fact["fact_id"],
            fact_version=fact["fact_version"],
            source_id=fact_source,
            file_version=fact_passage["revision"],
            field_scope=sorted(fact.keys()),
            authorization_domain=target_domain,
            snapshot_id=snap.snapshot_id,
            source_digest=fact_passage["digest"],
        )

    ans = session.consume_answer(
        records=records,
        player_uid=player_uid,
        season=season,
        match_type=match_type,
        scope=scope,
        provider=provider,
        competition=competition,
        metric=metric,
        fact=fact,
        passage=target_passage,
        source_text=target_passage["text"],
        chain=chain,
        claims=claims,
        fact_guarantee=guarantee,
    )

    is_historical_archive = bool(
        enable_v232_card_pipeline
        and active_league_season
        and _is_prior_season(season, active_league_season)
    )
    archive_banner = (
        f"> [HISTORICAL_SEASON_ARCHIVE] 本文件为 {season} 历史赛季定格档案，"
        f"非当前活跃赛季（{active_league_season}）主卡\n\n"
        if is_historical_archive
        else ""
    )

    def _maybe_archive_name(filename: str) -> str:
        if is_historical_archive and filename.endswith("_CURRENT.md"):
            return filename[: -len("_CURRENT.md")] + "_ARCHIVE.md"
        return filename

    card_text = session.render_verified(ans, kind="research_card")
    passage_text = session.render_verified(cited, kind="passage")
    card_md = (archive_banner + card_text).encode("utf-8")
    passage_md = (archive_banner + passage_text).encode("utf-8")

    pid_slug = _safe_ascii_key(player_id)
    season_slug = _safe_ascii_key(season)

    card_name = _maybe_archive_name(
        research_card_consumer_filename(player_name, player_id, season)
    )
    passage_name = _maybe_archive_name(
        f"研究摘录_{'_'.join(player_name.split())}_{'_'.join(player_id.split())}_{'_'.join(season.split())}_{'_'.join(passage_label.split())}_CURRENT.md"
    )

    result: Dict[str, Dict[str, Any]] = {
        f"research/card_{pid_slug}_{season_slug}": {
            "name": card_name,
            "mime": "text/markdown",
            "mode": "binary",
            "parent_role": "data",
            "content": card_md,
        },
        f"research/passage_{pid_slug}_{season_slug}": {
            "name": passage_name,
            "mime": "text/markdown",
            "mode": "binary",
            "parent_role": "data",
            "content": passage_md,
        },
    }
    for mt in ordered_match_types:
        mt_slug = _safe_ascii_key(mt)
        slice_text = session.render_verified(sliced_by_mt[mt], kind="slice_markdown")
        slice_md = (archive_banner + slice_text).encode("utf-8")
        result[f"research/slice_{pid_slug}_{season_slug}_{mt_slug}"] = {
            "name": _maybe_archive_name(slice_consumer_filename(player_name, player_id, season, mt)),
            "mime": "text/markdown",
            "mode": "binary",
            "parent_role": "data",
            "content": slice_md,
        }
    return result


def build_consumer_surface_targets_spec(
    *,
    release_id: str,
    engine_sha: str,
    master_csv_bytes: bytes,
    source_registry_bytes: bytes,
    doc_index_bytes: Optional[bytes] = None,
    collector_official_digest_bytes: Optional[bytes] = None,
    include_collector_digest: Optional[bool] = None,
    foreign_registration_csv_bytes: Optional[bytes] = None,
    domestic_reg_diff_csv_bytes: Optional[bytes] = None,
    include_curated_csvs: Optional[bool] = None,
    promoted_research_artifacts: Optional[Dict[str, Dict[str, Any]]] = None,
    observation_horizon: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Build declared consumer-surface target contents keyed by logical_key for Stage 4."""
    row_count = compute_master_row_count(master_csv_bytes)
    targets: Dict[str, Dict[str, Any]] = {
        "consumer/master_domestic_2017_2027_current.csv": {
            "name": CONSUMER_MASTER_FILENAME,
            "mime": "text/csv",
            "mode": "binary",
            "parent_role": "data",
            "content": export_consumer_master_csv(
                master_csv_bytes,
                observation_horizon=observation_horizon,
            ),
        },
        "consumer/field_dictionary_current.md": {
            "name": CONSUMER_DICTIONARY_FILENAME,
            "mime": "text/markdown",
            "mode": "binary",
            "parent_role": "data",
            "content": render_consumer_field_dictionary_md(
                release_id=release_id,
                engine_sha=engine_sha,
                master_row_count=row_count,
                include_collector_digest=include_collector_digest,
                include_curated_csvs=include_curated_csvs,
                observation_horizon=observation_horizon,
            ),
        },
        "consumer/source_registry_index_current.csv": {
            "name": CONSUMER_SOURCE_REGISTRY_FILENAME,
            "mime": "text/csv",
            "mode": "binary",
            "parent_role": "data",
            "content": export_consumer_source_registry_csv(source_registry_bytes),
        },
    }
    if doc_index_bytes is not None:
        targets["consumer/document_index_current.json"] = {
            "name": CONSUMER_DOC_INDEX_FILENAME,
            "mime": "application/json",
            "mode": "binary",
            "parent_role": "data",
            "content": export_consumer_document_index(doc_index_bytes),
        }
    if collector_official_digest_bytes is not None:
        targets["consumer/collector_official_digest_current.md"] = {
            "name": COLLECTOR_DIGEST_FILENAME,
            "mime": "text/markdown",
            "mode": "binary",
            "parent_role": "data",
            "content": collector_official_digest_bytes,
        }
    if foreign_registration_csv_bytes is not None:
        targets["consumer/foreign_players_registration_current.csv"] = {
            "name": FOREIGN_REGISTRATION_CSV_FILENAME,
            "mime": "text/csv",
            "mode": "binary",
            "parent_role": "data",
            "content": foreign_registration_csv_bytes,
        }
    if domestic_reg_diff_csv_bytes is not None:
        targets["consumer/domestic_registration_diff_current.csv"] = {
            "name": DOMESTIC_REG_DIFF_CSV_FILENAME,
            "mime": "text/csv",
            "mode": "binary",
            "parent_role": "data",
            "content": domestic_reg_diff_csv_bytes,
        }
    if promoted_research_artifacts:
        for key, spec in sorted(promoted_research_artifacts.items()):
            targets[key] = dict(spec)
    return targets
