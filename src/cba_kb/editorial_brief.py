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
from typing import Any, Dict, List, Mapping, Sequence

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


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？])(?![。！？])|(?<=[.!?])(?=\s+)")


def _split_sentences(text: str) -> List[str]:
    """Split multi-sentence text into individual sentences without splitting decimals."""
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(text.strip()) if p and p.strip()]
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
