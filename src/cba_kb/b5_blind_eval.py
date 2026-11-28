"""Deterministic B5 dual-host split/shared blind evaluator (REQ-251 P0-E4).

100% Python stdlib.
Validates frozen question banks (`PRIMARY_DOC_70PCT` / `PRACTITIONER_REAL_30PCT`),
supports `--mode split | shared`, classifies host responses into 4 deterministic
categories (`SUPPORTED_ACCURATE`, `HONEST_UNKNOWN`, `OVERCLAIMED_BEYOND_CITATION`,
`MISSED_IN_KB`), and enforces `official_fact_error_count == 0`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

B5_CLASS_SUPPORTED = "SUPPORTED_ACCURATE"
B5_CLASS_HONEST_UNKNOWN = "HONEST_UNKNOWN"
B5_CLASS_OVERCLAIMED = "OVERCLAIMED_BEYOND_CITATION"
B5_CLASS_MISSED_IN_KB = "MISSED_IN_KB"

VALID_SOURCE_ORIGINS = frozenset({"PRIMARY_DOC_70PCT", "PRACTITIONER_REAL_30PCT"})
VALID_TRACKS = frozenset({"WORKBUDDY_SCOUT_DEEP", "DINGTALK_PUBLIC_MEDIA_ACL"})
VALID_MODES = frozenset({"split", "shared"})
_UNKNOWN_PHRASES = ("未知", "未收录", "无法提供", "尚无官方", "unknown", "no_basis")


class B5EvalError(ValueError):
    """Raised when a B5 question bank or evaluation request is invalid."""


def validate_question_bank(questions: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    if isinstance(questions, (str, bytes)) or not isinstance(questions, Sequence) or not questions:
        raise B5EvalError("QUESTION_BANK_NON_EMPTY_SEQUENCE_REQUIRED")
    seen_ids: set[str] = set()
    out: List[Dict[str, Any]] = []
    for item in questions:
        if not isinstance(item, Mapping):
            raise B5EvalError("QUESTION_OBJECT_REQUIRED")
        qid = str(item.get("question_id") or "").strip()
        if not qid or qid in seen_ids:
            raise B5EvalError(f"INVALID_OR_DUPLICATE_QUESTION_ID:{qid}")
        seen_ids.add(qid)
        origin = str(item.get("source_origin") or "").strip().upper()
        if origin not in VALID_SOURCE_ORIGINS:
            raise B5EvalError(f"INVALID_SOURCE_ORIGIN:{origin}")
        track = str(item.get("track") or "").strip().upper()
        if track not in VALID_TRACKS:
            raise B5EvalError(f"INVALID_TRACK:{track}")
        prompt = str(item.get("prompt") or "").strip()
        if not prompt:
            raise B5EvalError(f"PROMPT_REQUIRED:{qid}")
        out.append({
            "question_id": qid,
            "source_origin": origin,
            "track": track,
            "prompt": prompt,
            "expected_facts": [str(x) for x in (item.get("expected_facts") or ()) if str(x)],
            "expected_citations": [str(x) for x in (item.get("expected_citations") or ()) if str(x)],
            "kb_has_verified_answer": bool(item.get("kb_has_verified_answer", True)),
            "is_official_fact": bool(item.get("is_official_fact", False)),
            "forbid_overclaim_markers": [
                str(x) for x in (item.get("forbid_overclaim_markers") or ()) if str(x)
            ],
        })
    return out


def _classify_single_response(
    question: Mapping[str, Any],
    response: Mapping[str, Any],
) -> tuple[str, bool]:
    """Return (classification, is_official_fact_error)."""
    ans = str(response.get("answer_text") or "").strip()
    cites = [str(c).strip() for c in (response.get("citations") or ()) if str(c).strip()]
    declared_unknown = bool(response.get("declared_unknown", False)) or (
        any(p in ans.lower() for p in _UNKNOWN_PHRASES)
        and not any(f in ans for f in question.get("expected_facts") or ())
    )

    forbid_markers = question.get("forbid_overclaim_markers") or ()
    if any(m in ans for m in forbid_markers):
        return B5_CLASS_OVERCLAIMED, bool(question.get("is_official_fact"))

    if not question.get("kb_has_verified_answer", True):
        if declared_unknown:
            return B5_CLASS_HONEST_UNKNOWN, False
        return B5_CLASS_OVERCLAIMED, True

    if declared_unknown:
        return B5_CLASS_MISSED_IN_KB, False

    expected_facts = question.get("expected_facts") or ()
    expected_cites = question.get("expected_citations") or ()
    facts_ok = all(f in ans for f in expected_facts)
    cites_ok = (not expected_cites) or any(ec in cites for ec in expected_cites)
    if facts_ok and cites_ok:
        return B5_CLASS_SUPPORTED, False

    return B5_CLASS_OVERCLAIMED, bool(question.get("is_official_fact", True))


def evaluate_b5_responses(
    questions: Sequence[Mapping[str, Any]],
    responses: Sequence[Mapping[str, Any]],
    *,
    mode: str = "split",
) -> Dict[str, Any]:
    """Deterministically score dual-host B5 responses under `split` or `shared` mode."""
    if mode not in VALID_MODES:
        raise B5EvalError(f"INVALID_B5_MODE:{mode}")
    validated_q = validate_question_bank(questions)
    resp_by_qid: Dict[str, Dict[str, Any]] = {
        str(r.get("question_id") or "").strip(): dict(r)
        for r in (responses or ())
        if isinstance(r, Mapping) and r.get("question_id")
    }

    counts = {
        B5_CLASS_SUPPORTED: 0,
        B5_CLASS_HONEST_UNKNOWN: 0,
        B5_CLASS_OVERCLAIMED: 0,
        B5_CLASS_MISSED_IN_KB: 0,
    }
    by_track: Dict[str, Dict[str, int]] = {}
    official_fact_error_count = 0
    details: List[Dict[str, Any]] = []

    for q in validated_q:
        qid = q["question_id"]
        resp = resp_by_qid.get(qid, {"question_id": qid, "answer_text": "", "declared_unknown": True})
        cls_label, is_official_err = _classify_single_response(q, resp)
        counts[cls_label] += 1
        if is_official_err:
            official_fact_error_count += 1
        trk = q["track"] if mode == "split" else "SHARED_ALL"
        trk_bucket = by_track.setdefault(trk, {
            B5_CLASS_SUPPORTED: 0,
            B5_CLASS_HONEST_UNKNOWN: 0,
            B5_CLASS_OVERCLAIMED: 0,
            B5_CLASS_MISSED_IN_KB: 0,
        })
        trk_bucket[cls_label] += 1
        details.append({
            "question_id": qid,
            "track": trk,
            "host": resp.get("host"),
            "classification": cls_label,
            "official_fact_error": is_official_err,
        })

    return {
        "mode": mode,
        "total_questions": len(validated_q),
        "counts": counts,
        "by_track": by_track,
        "official_fact_error_count": official_fact_error_count,
        "gate_passed": official_fact_error_count == 0,
        "details": details,
    }


def run_b5_eval_from_files(
    questions_path: Path | str,
    responses_path: Path | str,
    *,
    mode: str = "split",
) -> Dict[str, Any]:
    q_data = json.loads(Path(questions_path).read_text(encoding="utf-8"))
    r_data = json.loads(Path(responses_path).read_text(encoding="utf-8"))
    q_list = q_data.get("questions") if isinstance(q_data, Mapping) else q_data
    r_list = r_data.get("responses") if isinstance(r_data, Mapping) else r_data
    return evaluate_b5_responses(q_list, r_list, mode=mode)
