"""REQ-240 P0-4: 1-1 <=120-char sentence-attributed editorial brief & 4 hard-cases validator.

Enforces:
- `MAX_BRIEF_CHARS = 120` character ceiling on verified editorial briefs.
- Exclusion of `SOURCE_GAP_UNKNOWN`, `UNDECIDED`, and `unreferenced_encyclopedia` facts.
- 1-to-1 sentence attribution mapping every sentence in `body` to its source citation.
- `public_safe` redaction verification via `verify_public_safe_card_text`.
- Fail-closed verification of all 4 Shenzhen pilot hard-case archetypes
  (using 100% synthetic or caller-supplied verification bundles).
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .actor import validate_person_uid
from .kc_visibility import validate_card_visibility, verify_public_safe_card_text

MAX_BRIEF_CHARS = 120

_EXCLUDED_FACT_STATUSES = frozenset({"SOURCE_GAP_UNKNOWN", "UNDECIDED"})
_EXCLUDED_SOURCE_KINDS = frozenset({"unreferenced_encyclopedia"})
_UNVERIFIED_CAUSE_STATUSES = frozenset({"UNSUBSTANTIATED", "RUMOR", "INFERRED_FROM_ABSENCE"})
_CJK_END_PUNCT = frozenset("。！？；")
_INTERNAL_CLUE_MARKER = "[internal_clue_only"
_TILDE_FENCE_MARKER = "~~~"

_REQUIRED_HARD_CASES = (
    "case_1_coach_role_discrepancy",
    "case_2_player_to_coach_unified",
    "case_3_story_one_to_three_projection",
    "case_4_multi_source_discrepancy_and_silence_not_cause",
)


_COMMON_ABBREVIATIONS = frozenset({
    "e.g",
    "i.e",
    "vs",
    "dr",
    "mr",
    "ms",
    "mrs",
    "no",
    "st",
    "jr",
    "sr",
})
_ABBREV_TOKEN_RE = re.compile(r"([A-Za-z](?:\.[A-Za-z])+|[A-Za-z]+)\Z")


def _split_sentences(text: str) -> List[str]:
    """Split multi-sentence text into individual sentences without splitting decimals
    or common abbreviations (e.g., i.e., vs., Dr., Mr., Ms., No.)."""
    s = text.strip()
    if not s:
        return []
    parts: List[str] = []
    start = 0
    n = len(s)
    i = 0
    while i < n:
        ch = s[i]
        is_boundary = False
        if ch in "。！？":
            if i + 1 < n and s[i + 1] not in "。！？":
                is_boundary = True
        elif ch in ".!?":
            next_ch = s[i + 1] if i + 1 < n else ""
            prev_ch = s[i - 1] if i > 0 else ""
            if next_ch and (next_ch.isspace() or (next_ch.isupper() and not prev_ch.isdigit())):
                is_abbrev = False
                if ch == ".":
                    m = _ABBREV_TOKEN_RE.search(s[:i])
                    if m and m.group(1).lower() in _COMMON_ABBREVIATIONS:
                        is_abbrev = True
                if not is_abbrev:
                    is_boundary = True
        if is_boundary:
            chunk = s[start : i + 1].strip()
            if chunk:
                parts.append(chunk)
            start = i + 1
        i += 1
    tail = s[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _join_sentences(existing: str, next_sentence: str) -> str:
    if not existing:
        return next_sentence
    if existing[-1] in _CJK_END_PUNCT:
        return existing + next_sentence
    return f"{existing} {next_sentence}"


def _verify_public_safe_strict(text: str) -> None:
    verify_public_safe_card_text(text)
    if _INTERNAL_CLUE_MARKER in text.lower():
        raise ValueError("PUBLIC_SAFE_REDACTION_VIOLATION:internal_clue_only")
    if _TILDE_FENCE_MARKER in text:
        raise ValueError("PUBLIC_SAFE_REDACTION_VIOLATION:tilde_fenced_code_block")


def compose_verified_brief(
    facts: Sequence[Mapping[str, Any]],
    *,
    entity_name: str,
    max_chars: int = MAX_BRIEF_CHARS,
    visibility: str = "public_safe",
) -> Dict[str, Any]:
    """Compose a <=max_chars brief with 1-to-1 sentence attributions.

    Excludes facts with `status in ("SOURCE_GAP_UNKNOWN", "UNDECIDED")` or
    `source_kind == "unreferenced_encyclopedia"`. Raises
    `ValueError("NO_VERIFIED_FACTS_FOR_BRIEF")` if no eligible sentences remain.
    """
    resolved_visibility = validate_card_visibility(visibility)
    if not isinstance(max_chars, int) or max_chars <= 0:
        raise ValueError(f"INVALID_MAX_CHARS:{max_chars!r}")

    eligible: List[Dict[str, Any]] = []
    excluded_count = 0
    excluded_reasons: List[str] = []

    for idx, fact in enumerate(facts):
        if not isinstance(fact, Mapping):
            excluded_count += 1
            excluded_reasons.append(f"fact[{idx}]:not_mapping")
            continue

        status = str(fact.get("status") or "").strip().upper()
        source_kind = str(fact.get("source_kind") or "").strip().lower()
        sentence = str(fact.get("sentence") or "").strip()

        if status in _EXCLUDED_FACT_STATUSES:
            excluded_count += 1
            excluded_reasons.append(f"fact[{idx}]:status={status}")
            continue
        if source_kind in _EXCLUDED_SOURCE_KINDS:
            excluded_count += 1
            excluded_reasons.append(f"fact[{idx}]:source_kind={source_kind}")
            continue
        if not sentence:
            excluded_count += 1
            excluded_reasons.append(f"fact[{idx}]:empty_sentence")
            continue

        citation_label = str(
            fact.get("citation_label")
            or fact.get("source_name")
            or fact.get("source_kind")
            or "verified_source"
        ).strip()
        for atomic_sentence in _split_sentences(sentence):
            eligible.append(
                {
                    "sentence": atomic_sentence,
                    "citation_label": citation_label,
                    "source_url": fact.get("source_url"),
                    "source_kind": fact.get("source_kind"),
                    "authority_scope": fact.get("authority_scope"),
                    "status": status or "VERIFIED_PRESENT",
                }
            )

    if not eligible:
        raise ValueError("NO_VERIFIED_FACTS_FOR_BRIEF")

    body = ""
    attributions: List[Dict[str, Any]] = []
    omitted_for_length = 0

    for item in eligible:
        sent = item["sentence"]
        candidate = _join_sentences(body, sent)
        if len(candidate) <= max_chars:
            body = candidate
            attributions.append(
                {
                    "index": len(attributions) + 1,
                    "sentence": sent,
                    "citation_label": item["citation_label"],
                    "source_url": item["source_url"],
                    "source_kind": item["source_kind"],
                    "authority_scope": item["authority_scope"],
                    "status": item["status"],
                }
            )
        elif not body:
            # First eligible sentence alone exceeds max_chars: trim safely within budget.
            trimmed = (sent[: max_chars - 1] + "。") if max_chars >= 2 else sent[:max_chars]
            body = trimmed
            attributions.append(
                {
                    "index": 1,
                    "sentence": trimmed,
                    "citation_label": item["citation_label"],
                    "source_url": item["source_url"],
                    "source_kind": item["source_kind"],
                    "authority_scope": item["authority_scope"],
                    "status": item["status"],
                }
            )
        else:
            omitted_for_length += 1

    if not body or not attributions:
        raise ValueError("NO_VERIFIED_FACTS_FOR_BRIEF")

    if resolved_visibility == "public_safe":
        _verify_public_safe_strict(body)

    return {
        "entity_name": str(entity_name),
        "visibility": resolved_visibility,
        "max_chars": max_chars,
        "body": body,
        "char_count": len(body),
        "sentence_attributions": attributions,
        "excluded_count": excluded_count,
        "omitted_for_length_count": omitted_for_length,
        "excluded_reasons": excluded_reasons,
    }


def _check_case_1(payload: Any) -> List[str]:
    errors: List[str] = []
    if not isinstance(payload, Mapping):
        return ["case_1:missing_or_not_mapping"]

    facets = payload.get("coach_role_facets")
    if hasattr(facets, "to_dict") and callable(facets.to_dict):
        facets = facets.to_dict()
    if not isinstance(facets, Mapping):
        errors.append("case_1:missing_coach_role_facets")
    else:
        league_role = str(facets.get("league_listed_role") or "").strip()
        club_role = str(facets.get("club_appointed_role") or "").strip()
        gameday_role = str(facets.get("gameday_acting_role") or "").strip()
        if not league_role or league_role == "OBSERVED_EMPTY":
            errors.append("case_1:missing_league_listed_role")
        if not club_role or club_role == "OBSERVED_EMPTY":
            errors.append("case_1:missing_club_appointed_role")
        if league_role and club_role and league_role == club_role:
            errors.append("case_1:expected_league_vs_club_discrepancy")
        if not gameday_role:
            errors.append("case_1:missing_gameday_acting_role")
        if facets.get("has_institutional_discrepancy") is not True:
            errors.append("case_1:has_institutional_discrepancy_not_true")
        if facets.get("discrepancy_code") != "LEAGUE_VS_CLUB_ROLE_DISCREPANCY":
            errors.append("case_1:invalid_discrepancy_code")
        if not str(facets.get("discrepancy_explanation") or "").strip():
            errors.append("case_1:missing_discrepancy_explanation")

    if payload.get("statistics_domain_status") != "NOT_APPLICABLE":
        errors.append("case_1:statistics_domain_status_not_NOT_APPLICABLE")
    if payload.get("has_player_stats_table") is not False:
        errors.append("case_1:pure_coach_must_not_have_player_stats_table")
    return errors


def _check_case_2(payload: Any) -> List[str]:
    errors: List[str] = []
    if not isinstance(payload, Mapping):
        return ["case_2:missing_or_not_mapping"]

    unified = payload.get("unified_profile")
    if not isinstance(unified, Mapping):
        errors.append("case_2:missing_unified_profile")
    else:
        person_uid = unified.get("person_uid")
        try:
            validate_person_uid(str(person_uid or ""))
        except ValueError:
            errors.append("case_2:invalid_person_uid")
        if unified.get("has_player_tenure") is not True:
            errors.append("case_2:missing_player_tenure")
        if unified.get("has_coach_tenure") is not True:
            errors.append("case_2:missing_coach_tenure")
        if unified.get("is_player_to_coach_transition") is not True:
            errors.append("case_2:is_player_to_coach_transition_not_true")
        linked = unified.get("linked_player_uids")
        if not isinstance(linked, Sequence) or isinstance(linked, (str, bytes)) or len(linked) < 1:
            errors.append("case_2:missing_linked_player_uids")
        tenures = unified.get("role_tenures")
        if not isinstance(tenures, Sequence) or isinstance(tenures, (str, bytes)) or len(tenures) < 2:
            errors.append("case_2:expected_at_least_two_role_tenures")

    homonym = payload.get("homonym_resolution")
    if not isinstance(homonym, Mapping):
        errors.append("case_2:missing_homonym_resolution")
    else:
        if homonym.get("status") != "UNDECIDED":
            errors.append("case_2:homonym_status_not_UNDECIDED")
        if homonym.get("actor_kind") != "unresolved":
            errors.append("case_2:homonym_actor_kind_not_unresolved")
        if homonym.get("person_uid") is not None:
            errors.append("case_2:homonym_person_uid_must_be_None")
        candidates = homonym.get("candidates")
        if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)) or len(candidates) < 2:
            errors.append("case_2:homonym_expected_multiple_candidates")

    return errors


def _check_case_3(payload: Any) -> List[str]:
    errors: List[str] = []
    if not isinstance(payload, Mapping):
        return ["case_3:missing_or_not_mapping"]

    story = payload.get("story")
    if not isinstance(story, Mapping):
        return ["case_3:missing_story"]

    story_id = str(story.get("story_id") or "").strip()
    occurred_season = str(story.get("occurred_season") or "").strip()
    published_at = str(story.get("published_at") or "").strip()
    if not story_id.startswith("story_"):
        errors.append("case_3:invalid_story_id")
    if not occurred_season:
        errors.append("case_3:missing_occurred_season")
    if not published_at:
        errors.append("case_3:missing_published_at")

    internal_notes: List[str] = []
    for p in story.get("participants") or ():
        if isinstance(p, Mapping):
            note = str(p.get("internal_verbatim_or_note") or "").strip()
            if note:
                internal_notes.append(note)

    for target_key in ("person_a_public", "person_b_public", "org_public"):
        projected = payload.get(target_key)
        if (
            not isinstance(projected, Sequence)
            or isinstance(projected, (str, bytes))
            or len(projected) < 1
        ):
            errors.append(f"case_3:{target_key}_empty")
            continue
        first = projected[0]
        if not isinstance(first, Mapping):
            errors.append(f"case_3:{target_key}_invalid_entry")
            continue
        if first.get("story_id") != story_id:
            errors.append(f"case_3:{target_key}_story_id_mismatch")
        if first.get("occurred_season") != occurred_season:
            errors.append(f"case_3:{target_key}_occurred_season_mismatch")
        if first.get("internal_verbatim_or_note"):
            errors.append(f"case_3:{target_key}_leaked_internal_verbatim_field")

        serialized = json.dumps(projected, ensure_ascii=False)
        for note in internal_notes:
            if note in serialized:
                errors.append(f"case_3:{target_key}_leaked_internal_verbatim_text")
                break
        summary = first.get("summary")
        if isinstance(summary, str) and summary:
            try:
                _verify_public_safe_strict(summary)
            except ValueError:
                errors.append(f"case_3:{target_key}_summary_failed_public_safe")

    return errors


def _check_case_4(payload: Any) -> List[str]:
    errors: List[str] = []
    if not isinstance(payload, Mapping):
        return ["case_4:missing_or_not_mapping"]

    filtered = payload.get("filtered_timeline")
    if not isinstance(filtered, Mapping):
        errors.append("case_4:missing_filtered_timeline")
    else:
        main_tl = filtered.get("main_timeline")
        rejected = filtered.get("rejected_rumor_causes")
        if not isinstance(main_tl, Sequence) or isinstance(main_tl, (str, bytes)) or len(main_tl) < 1:
            errors.append("case_4:main_timeline_empty")
        else:
            has_discrepancy = False
            for evt in main_tl:
                if not isinstance(evt, Mapping):
                    continue
                if evt.get("event_kind") == "MULTI_SOURCE_DISCREPANCY":
                    has_discrepancy = True
                if (
                    evt.get("event_kind") == "RUMOR_CAUSE"
                    or str(evt.get("cause_status") or "").strip().upper() in _UNVERIFIED_CAUSE_STATUSES
                    or evt.get("unverified_cause") is True
                ):
                    errors.append("case_4:rumor_cause_leaked_into_main_timeline")
            if not has_discrepancy:
                errors.append("case_4:missing_multi_source_discrepancy_event")

        if not isinstance(rejected, Sequence) or isinstance(rejected, (str, bytes)) or len(rejected) < 1:
            errors.append("case_4:expected_rejected_rumor_causes")

    internal_md = payload.get("internal_markdown")
    if not isinstance(internal_md, str) or "[INTERNAL_CLUE_ONLY:" not in internal_md:
        errors.append("case_4:internal_markdown_missing_quarantine_marker")

    public_md = payload.get("public_markdown")
    if not isinstance(public_md, str):
        errors.append("case_4:missing_public_markdown")
    else:
        try:
            _verify_public_safe_strict(public_md)
        except ValueError as exc:
            errors.append(f"case_4:public_markdown_violation:{exc}")

    return errors


def verify_shenzhen_pilot_hard_cases(bundle: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate all 4 Shenzhen pilot hard-case invariants; fail closed on any tampering."""
    if not isinstance(bundle, Mapping):
        raise ValueError("SHENZHEN_PILOT_HARD_CASE_FAILED:bundle_not_mapping")

    checkers = {
        "case_1_coach_role_discrepancy": _check_case_1,
        "case_2_player_to_coach_unified": _check_case_2,
        "case_3_story_one_to_three_projection": _check_case_3,
        "case_4_multi_source_discrepancy_and_silence_not_cause": _check_case_4,
    }

    passed_cases: List[str] = []
    failed_cases: List[str] = []
    failure_details: List[str] = []

    for case_name in _REQUIRED_HARD_CASES:
        errs = checkers[case_name](bundle.get(case_name))
        if errs:
            failed_cases.append(case_name)
            failure_details.extend(errs)
        else:
            passed_cases.append(case_name)

    if failed_cases:
        raise ValueError(
            "SHENZHEN_PILOT_HARD_CASE_FAILED:" + ";".join(failure_details)
        )

    return {
        "status": "PASS",
        "passed_cases": passed_cases,
        "failed_cases": failed_cases,
    }


# ---------------------------------------------------------------------------
# REQ-241 P0-1: Wikipedia 2.0 Inverted-Pyramid Chinese Lead Paragraph
# ---------------------------------------------------------------------------

_WIKI_ROLE_ZH: Dict[str, str] = {
    "HEAD_COACH": "主教练",
    "ASSISTANT_COACH": "助理教练",
    "ACTING_HEAD_COACH": "代理主教练",
    "COACH": "教练",
    "PLAYER": "球员",
    "STAFF": "教练组成员",
    "EXECUTIVE": "俱乐部管理层",
}
_WIKI_PLACEHOLDER_VALUES = frozenset({"", "0", "-", "none", "null", "unknown", "unassigned", "observed_empty"})
_WIKI_PID_RE = re.compile(r"\bpid_[0-9a-f]{16,64}\b", re.IGNORECASE)
_WIKI_INLINE_CITE_RE = re.compile(r"\[\d+\]")


def _clean_wiki_field(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text.casefold() in _WIKI_PLACEHOLDER_VALUES:
        return None
    if "(unassigned)" in text.casefold() or _WIKI_PID_RE.search(text):
        return None
    return text


_DEPARTED_CLUB_SENTINELS = frozenset({
    "离队", "已离队", "转会", "转会离队", "自由球员", "自由身", "阶段性外援", "暂离", "departed", "transferred", "free_agent",
})
_DEPARTED_ROSTER_STATUSES = frozenset({
    "DEPARTED", "DEPARTED_OR_TRANSFERRED", "TRANSFERRED", "STAGED",
    "STAGED_FOREIGN", "STAGED_FOREIGN_25_26", "WAIVED", "FORMER", "INACTIVE",
})
_HONOR_TRANSACTION_RE = re.compile(
    r"(?:加盟|签约|转会|租借|离队|注册|解约|退役|复出|上调一队|买断|被裁)"
)
_HONOR_AWARD_RE = re.compile(
    r"(?:总冠军|冠军|MVP|最有价值球员|最佳|奖|金牌|银牌|铜牌|得分王|篮板王|助攻王|抢断王|盖帽王)"
)


def _classify_honors_for_lead(honors: Sequence[str]) -> tuple[List[str], List[str]]:
    """Separate awards (曾获) from selections (曾入选), filtering out transfer/transaction lines."""
    awards: List[str] = []
    selections: List[str] = []
    seen: set[str] = set()
    for raw in honors:
        item = raw.strip()
        if not item or item in seen:
            continue
        if _HONOR_TRANSACTION_RE.search(item):
            continue
        seen.add(item)
        if _HONOR_AWARD_RE.search(item):
            awards.append(item)
        else:
            selections.append(item)
    return awards, selections


def _format_birth_date_zh(birth_date: Optional[str]) -> Optional[str]:
    cleaned = _clean_wiki_field(birth_date)
    if not cleaned:
        return None
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", cleaned)
    if m:
        return f"{int(m.group(1))}年{int(m.group(2))}月{int(m.group(3))}日"
    return cleaned


def _register_cite_duck(
    citation_registry: Optional[Any],
    *,
    label: str,
    url: Optional[str] = None,
    source_kind: Optional[str] = None,
    published_at: Optional[str] = None,
    publisher: Optional[str] = None,
    source_id: Optional[str] = None,
) -> Optional[int]:
    if citation_registry is None or not hasattr(citation_registry, "register"):
        return None
    clean_label = _clean_wiki_field(label)
    if not clean_label:
        return None
    idx = citation_registry.register(
        label=clean_label,
        url=url,
        source_kind=source_kind,
        published_at=published_at,
        publisher=publisher,
        source_id=source_id,
    )
    return idx if isinstance(idx, int) and idx > 0 else None


def _register_evidence_ref_item(citation_registry: Optional[Any], ref: Any) -> Optional[int]:
    if citation_registry is None or not hasattr(citation_registry, "register"):
        return None
    if isinstance(ref, Mapping):
        label = str(ref.get("label") or ref.get("title") or ref.get("url") or "").strip()
        return _register_cite_duck(
            citation_registry,
            label=label,
            url=ref.get("url") or ref.get("canonical_url"),
            source_kind=ref.get("source_kind"),
            published_at=ref.get("published_at"),
            publisher=ref.get("publisher"),
            source_id=ref.get("source_id"),
        )
    if isinstance(ref, str) and ref.strip():
        s = ref.strip()
        is_url = s.startswith(("https://", "http://"))
        return _register_cite_duck(
            citation_registry,
            label=s,
            url=s if is_url else None,
            source_kind="official_reference",
        )
    return None


def _ensure_period(text: str) -> str:
    s = text.strip()
    if not s:
        return ""
    if s[-1] not in "。！？":
        return s + "。"
    return s


def compose_wiki_lead_paragraph(
    *,
    person_name: str,
    bio_profile: Optional[Any] = None,
    registration_profile: Optional[Mapping[str, Any]] = None,
    coach_role_facets: Optional[Any] = None,
    unified_profile: Optional[Mapping[str, Any]] = None,
    career_season_stats: Optional[Mapping[str, Any] | Sequence[Mapping[str, Any]]] = None,
    projected_stories: Optional[Sequence[Mapping[str, Any]]] = None,
    citation_registry: Optional[Any] = None,
    min_chars: int = 80,
    max_chars: int = 180,
    visibility: str = "public_safe",
) -> Dict[str, Any]:
    """Compose an 80-180 char inverted-pyramid Chinese Wikipedia lead paragraph.

    Duck-types `citation_registry` (via `.register(...)`) to avoid circular imports.
    Never emits `0`, `unassigned`, `UNKNOWN`, or `pid_...` in lead text.
    """
    resolved_visibility = validate_card_visibility(visibility)
    if not isinstance(max_chars, int) or max_chars <= 0:
        raise ValueError(f"INVALID_MAX_CHARS:{max_chars!r}")
    if not isinstance(min_chars, int) or min_chars < 0:
        raise ValueError(f"INVALID_MIN_CHARS:{min_chars!r}")

    name = _clean_wiki_field(person_name) or str(person_name or "").strip()
    if not name:
        raise ValueError("PERSON_NAME_REQUIRED")

    bio: Dict[str, Any] = {}
    if hasattr(bio_profile, "to_dict") and callable(bio_profile.to_dict):
        bio = dict(bio_profile.to_dict())
    elif isinstance(bio_profile, Mapping):
        bio = dict(bio_profile)

    reg: Dict[str, Any] = dict(registration_profile) if isinstance(registration_profile, Mapping) else {}
    dom_cur = reg.get("domestic_current") if isinstance(reg.get("domestic_current"), Mapping) else {}
    for_reg = reg.get("foreign_registration") if isinstance(reg.get("foreign_registration"), Mapping) else {}

    facets_list: List[Dict[str, Any]] = []
    if coach_role_facets is not None:
        raw_seq = list(coach_role_facets) if isinstance(coach_role_facets, (list, tuple)) else [coach_role_facets]
        for item in raw_seq:
            if hasattr(item, "to_dict") and callable(item.to_dict):
                facets_list.append(dict(item.to_dict()))
            elif isinstance(item, Mapping):
                facets_list.append(dict(item))

    unified = dict(unified_profile) if isinstance(unified_profile, Mapping) else {}

    # Extract key non-empty fields
    birth_zh = _format_birth_date_zh(bio.get("birth_date"))
    nationality = _clean_wiki_field(bio.get("nationality") or for_reg.get("nationality"))
    birth_place = _clean_wiki_field(bio.get("birth_place"))
    height_cm = bio.get("height_cm")
    weight_kg = bio.get("weight_kg")
    valid_height = (
        height_cm
        if isinstance(height_cm, int) and not isinstance(height_cm, bool) and height_cm > 0
        else None
    )
    valid_weight = (
        weight_kg
        if isinstance(weight_kg, int) and not isinstance(weight_kg, bool) and weight_kg > 0
        else None
    )
    position = _clean_wiki_field(bio.get("position") or reg.get("position") or for_reg.get("position"))
    jersey = _clean_wiki_field(bio.get("jersey_number") or reg.get("jersey_number") or for_reg.get("jersey_number"))
    raw_club = _clean_wiki_field(
        bio.get("current_club")
        or dom_cur.get("club_official")
        or for_reg.get("club")
        or reg.get("current_club")
    )
    roster_status = str(bio.get("roster_status") or reg.get("roster_status") or "").strip().upper()
    is_departed_club_token = bool(raw_club and raw_club.casefold() in _DEPARTED_CLUB_SENTINELS)
    club = None if is_departed_club_token else raw_club
    current_role_raw = _clean_wiki_field(bio.get("current_role"))
    youth_origin = _clean_wiki_field(bio.get("draft_or_youth_origin"))
    honors = [
        h for h in (_clean_wiki_field(x) for x in (bio.get("honors") or ())) if h
    ]
    season_label = _clean_wiki_field(dom_cur.get("season"))
    contract_cat = _clean_wiki_field(dom_cur.get("contract_category"))

    # Infer coaching role if coach_role_facets present
    coach_role_zh: Optional[str] = None
    if facets_list:
        f0 = facets_list[0]
        raw_c_role = _clean_wiki_field(f0.get("club_appointed_role") or f0.get("league_listed_role"))
        if raw_c_role:
            coach_role_zh = _WIKI_ROLE_ZH.get(raw_c_role.upper(), raw_c_role)
    if not coach_role_zh and current_role_raw:
        coach_role_zh = _WIKI_ROLE_ZH.get(current_role_raw.upper(), current_role_raw)

    # Pre-inspect career_season_stats so 4-template selection knows whether player has games played
    stats_cites: List[int] = []
    reg_summary: Optional[Mapping[str, Any]] = None
    seasons_count: Optional[int] = None
    if isinstance(career_season_stats, Mapping):
        if isinstance(career_season_stats.get("regular_career_summary"), Mapping):
            reg_summary = career_season_stats["regular_career_summary"]
        if isinstance(career_season_stats.get("seasons_count"), int):
            seasons_count = career_season_stats["seasons_count"]
        for row in career_season_stats.get("regular_season_rows") or ():
            if isinstance(row, Mapping):
                loc = _clean_wiki_field(row.get("locator"))
                if loc and loc.startswith(("https://", "http://")):
                    idx = _register_cite_duck(
                        citation_registry,
                        label="CBA官方技术统计",
                        url=loc,
                        source_kind="official_stats",
                    )
                    if idx is not None and idx not in stats_cites:
                        stats_cites.append(idx)
                        break

    reg_gp = reg_summary.get("games_played") if isinstance(reg_summary, Mapping) else None
    has_played_games = isinstance(reg_gp, (int, float)) and reg_gp > 0

    # Classify into one of 4 canonical lead templates (REQ-242 P0-E4)
    is_coach_template = bool(
        coach_role_zh and (facets_list or any(k in coach_role_zh for k in ("教练", "主帅")))
    )
    is_departed_or_staged = bool(
        not is_coach_template
        and (
            is_departed_club_token
            or roster_status in _DEPARTED_ROSTER_STATUSES
        )
    )
    is_rookie_or_unplayed = bool(
        not is_coach_template
        and not is_departed_or_staged
        and not has_played_games
        and (
            youth_origin
            or (isinstance(career_season_stats, Mapping) and not career_season_stats.get("regular_season_rows"))
        )
    )

    if is_coach_template:
        lead_template = "COACH_OR_TRANSITIONED_HEAD_COACH"
    elif is_departed_or_staged:
        lead_template = "DEPARTED_OR_STAGED_PLAYER"
    elif is_rookie_or_unplayed:
        lead_template = "ROOKIE_OR_UNPLAYED_YOUTH"
    else:
        lead_template = "ACTIVE_ROSTER_PLAYER"

    # Clauses: list of (plain_sentence, cite_indices_for_sentence)
    clauses: List[tuple[str, List[int]]] = []

    # Clause 1: Identity + Bio + Template-specific Club/Role
    bio_cites: List[int] = []
    for ref in bio.get("evidence_refs") or ():
        idx = _register_evidence_ref_item(citation_registry, ref)
        if idx is not None and idx not in bio_cites:
            bio_cites.append(idx)
    if not bio_cites and facets_list:
        for ref in facets_list[0].get("evidence_refs") or ():
            idx = _register_evidence_ref_item(citation_registry, ref)
            if idx is not None and idx not in bio_cites:
                bio_cites.append(idx)

    paren_parts: List[str] = []
    if birth_zh:
        paren_parts.append(f"{birth_zh}出生")
    if nationality and birth_place:
        paren_parts.append(f"{nationality}{birth_place}人")
    elif nationality:
        paren_parts.append(f"{nationality}籍")
    elif birth_place:
        paren_parts.append(f"{birth_place}人")

    c1_parts: List[str] = [f"{name}（{'，'.join(paren_parts)}）" if paren_parts else name]
    if lead_template == "COACH_OR_TRANSITIONED_HEAD_COACH":
        if club:
            c1_parts.append(f"现担任{club}{coach_role_zh}")
        else:
            c1_parts.append(f"现担任{coach_role_zh}")
        if unified.get("is_player_to_coach_transition"):
            c1_parts.append("球员时期曾征战CBA联赛，退役后转型执教")
        elif youth_origin:
            c1_parts.append(f"出自{youth_origin}")
    elif lead_template == "DEPARTED_OR_STAGED_PLAYER":
        if position:
            c1_parts.append(f"司职{position}")
        if valid_height and valid_weight:
            c1_parts.append(f"身高{valid_height}厘米、体重{valid_weight}公斤")
        elif valid_height:
            c1_parts.append(f"身高{valid_height}厘米")
        elif valid_weight:
            c1_parts.append(f"体重{valid_weight}公斤")
        if roster_status in ("STAGED", "STAGED_FOREIGN", "STAGED_FOREIGN_25_26"):
            if club:
                c1_parts.append(f"曾阶段性注册并代表{club}出战CBA联赛")
            else:
                c1_parts.append("曾阶段性注册出战CBA联赛")
        else:
            if club:
                c1_parts.append(f"曾效力于{club}，目前已离队")
            else:
                c1_parts.append("曾效力CBA联赛，目前已离队")
    elif lead_template == "ROOKIE_OR_UNPLAYED_YOUTH":
        if position:
            c1_parts.append(f"司职{position}")
        if valid_height and valid_weight:
            c1_parts.append(f"身高{valid_height}厘米、体重{valid_weight}公斤")
        elif valid_height:
            c1_parts.append(f"身高{valid_height}厘米")
        elif valid_weight:
            c1_parts.append(f"体重{valid_weight}公斤")
        if youth_origin:
            c1_parts.append(f"出自{youth_origin}")
        if club:
            season_prefix = f"{season_label}赛季" if season_label else ""
            c1_parts.append(f"{season_prefix}注册于{club}一队名单，系球队年轻球员")
        else:
            c1_parts.append("系CBA注册年轻球员")
    else:
        if position:
            c1_parts.append(f"司职{position}")
        if valid_height and valid_weight:
            c1_parts.append(f"身高{valid_height}厘米、体重{valid_weight}公斤")
        elif valid_height:
            c1_parts.append(f"身高{valid_height}厘米")
        elif valid_weight:
            c1_parts.append(f"体重{valid_weight}公斤")
        if youth_origin:
            c1_parts.append(f"出自{youth_origin}")
        if club:
            season_prefix = f"{season_label}赛季" if season_label else ""
            jersey_part = f"身披{jersey}号球衣" if jersey else ""
            c1_parts.append(f"{season_prefix}{jersey_part}效力于{club}")
        elif contract_cat and season_label:
            c1_parts.append(f"{season_label}赛季以{contract_cat}合同完成联赛注册")

    # Anti-duplication on clause 1 parts
    dedup_c1: List[str] = []
    for part in c1_parts:
        if part and part not in dedup_c1:
            dedup_c1.append(part)
    clause1 = _ensure_period("，".join(dedup_c1))
    clauses.append((clause1, bio_cites))

    # Clause 2: Career Season Stats summary
    if isinstance(reg_summary, Mapping) and has_played_games:
        gp = reg_summary.get("games_played")
        ppg = reg_summary.get("points_per_game")
        rpg = reg_summary.get("rebounds_per_game")
        apg = reg_summary.get("assists_per_game")
        stat_bits: List[str] = []
        if isinstance(ppg, (int, float)):
            stat_bits.append(f"{float(ppg):.1f}分")
        if isinstance(rpg, (int, float)):
            stat_bits.append(f"{float(rpg):.1f}个篮板")
        if isinstance(apg, (int, float)):
            stat_bits.append(f"{float(apg):.1f}次助攻")
        season_txt = f"（{seasons_count}个赛季）" if seasons_count and seasons_count > 1 else ""
        if stat_bits:
            stat_sent = f"CBA常规赛累计出战{int(gp)}场{season_txt}，场均贡献{'、'.join(stat_bits)}。"
            clauses.append((stat_sent, stats_cites))

    # Clause 3: Honors (with anti-duplication & grammar guard: no 曾入选2026年8月加盟 / no 曾入选多次CBA总冠军)
    if honors:
        awards, selections = _classify_honors_for_lead(honors)
        honor_bits: List[str] = []
        if awards:
            honor_bits.append(f"曾获{'、'.join(awards[:2])}")
        if selections:
            honor_bits.append(f"曾入选{'、'.join(selections[:2])}")
        if honor_bits:
            clauses.append((_ensure_period("，".join(honor_bits)), []))

    # Clause 4: Key Story
    if isinstance(projected_stories, Sequence) and not isinstance(projected_stories, (str, bytes)):
        for st in projected_stories:
            if not isinstance(st, Mapping):
                continue
            raw_sum = _clean_wiki_field(st.get("summary") or st.get("public_perspective_summary"))
            if not raw_sum:
                continue
            story_cites: List[int] = []
            src = st.get("canonical_source")
            if isinstance(src, Mapping):
                s_label = str(src.get("title") or src.get("label") or st.get("title") or "").strip()
                idx = _register_cite_duck(
                    citation_registry,
                    label=s_label,
                    url=src.get("url"),
                    source_kind=src.get("source_kind"),
                    published_at=src.get("published_at") or st.get("published_at"),
                    publisher=src.get("publisher"),
                    source_id=src.get("source_id"),
                )
                if idx is not None:
                    story_cites.append(idx)
            clauses.append((_ensure_period(raw_sum), story_cites))
            break

    # Assemble within max_chars (plain text budget)
    plain_parts: List[str] = []
    md_parts: List[str] = []
    used_indices: List[int] = []
    current_plain_len = 0

    for plain_sent, cites in clauses:
        cite_suffix = "".join(f"[{i}]" for i in cites)
        if current_plain_len + len(plain_sent) <= max_chars:
            plain_parts.append(plain_sent)
            md_parts.append(f"{plain_sent}{cite_suffix}")
            current_plain_len += len(plain_sent)
            for i in cites:
                if i not in used_indices:
                    used_indices.append(i)
        elif not plain_parts:
            trimmed = (plain_sent[: max_chars - 1] + "。") if max_chars >= 2 else plain_sent[:max_chars]
            plain_parts.append(trimmed)
            md_parts.append(f"{trimmed}{cite_suffix}")
            current_plain_len = len(trimmed)
            for i in cites:
                if i not in used_indices:
                    used_indices.append(i)
            break

    lead_plain_text = "".join(plain_parts)
    lead_markdown = "".join(md_parts)
    # Strip any stray [n] from plain text defensively
    lead_plain_text = _WIKI_INLINE_CITE_RE.sub("", lead_plain_text)

    if resolved_visibility == "public_safe":
        _verify_public_safe_strict(lead_plain_text)
        _verify_public_safe_strict(lead_markdown)

    return {
        "lead_markdown": lead_markdown,
        "lead_plain_text": lead_plain_text,
        "lead_template": lead_template,
        "char_count": len(lead_plain_text),
        "citation_indices": used_indices,
        "visibility": resolved_visibility,
    }
