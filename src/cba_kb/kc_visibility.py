"""PR-B (Issue #220) target-first rights filtering — offline/synthetic
only.

The authorized ID set is applied BEFORE any consumer-visible surface
(catalog, index, count, summary, path, diagnostic) is built — by
target_first_view/query_visible in this module, and (repair round 1)
as a required argument of the PR-B builders in kc_slices, kc_passages,
and kc_snapshot, so no exported builder can be invoked on raw
collections. F-02:
an unauthorized-existing object and a non-existent object return the
IDENTICAL uniform not-found result — no leaked count, title, ID, or
path. Instruction-like source text is DATA and is never executed.
private_ai_consumption, materialization_authorized, and
public_export_allowed are three SEPARATE flags: consumer usability for
a target requires private_ai_consumption AND materialization_authorized;
public_export_allowed neither grants nor removes target use.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

NOT_FOUND_DETAIL = "在当前可查询的授权范围内未取得可验证结果"

_INSTRUCTION_PATTERNS = ("忽略规则", "ignore rules", "ignore the rules",
                         "输出隐藏", "bypass authorization", "绕过授权",
                         "修改指针", "change pointers")


def filter_by_authorization(objects: List[Dict[str, Any]], *,
                            authorized_ids: Any) -> List[Dict[str, Any]]:
    """The target-first filter. Only objects whose object_id is in the
    authorized set survive; everything else is dropped here, before
    any surface is built from the result."""
    allowed = set(authorized_ids)
    return [obj for obj in objects if obj.get("object_id") in allowed]


def _tokenize(text: str) -> List[str]:
    tokens = [t.lower() for t in re.findall(r"[a-z0-9]+", text.lower())]
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.extend(list(run))
        for i in range(len(run) - 1):
            tokens.append(run[i:i + 2])
    return tokens


def target_first_view(objects: List[Dict[str, Any]], *,
                      authorized_ids: Any) -> Dict[str, Any]:
    """Build the consumer view ONLY from pre-filtered objects. Callers
    must pass the full corpus here and let this function filter first;
    no surface in the result is ever derived from an unfiltered list."""
    visible = filter_by_authorization(objects, authorized_ids=authorized_ids)
    visible_ids = sorted(obj["object_id"] for obj in visible)
    index_terms: Dict[str, List[str]] = {}
    for obj in visible:
        for token in (set(_tokenize(obj.get("title", ""))) |
                      set(_tokenize(obj.get("text", "")))):
            index_terms.setdefault(token, []).append(obj["object_id"])
    index_terms = {k: sorted(v) for k, v in sorted(index_terms.items())}
    return {
        "visible_object_ids": visible_ids,
        "visible_count": len(visible_ids),
        "summaries": {obj["object_id"]: obj.get("title")
                      for obj in visible},
        "index_terms": index_terms,
        "diagnostics": {"visible_count": len(visible_ids)},
    }


def query_visible(view: Dict[str, Any], object_id: str) -> Dict[str, Any]:
    """Visible objects return OK. Unauthorized-existing and
    non-existent objects return the IDENTICAL uniform result."""
    if object_id in view["visible_object_ids"]:
        return {"status": "OK", "grain": "DOCUMENT",
                "reason": "VISIBLE_IN_CURRENT_AUTHORIZED_SCOPE"}
    return {"status": "UNKNOWN", "grain": "UNKNOWN",
            "reason": "NO_VERIFIED_RESULT_IN_CURRENT_AUTHORIZED_SCOPE",
            "detail": NOT_FOUND_DETAIL}


def separate_flags(*, private_ai_consumption: bool,
                   materialization_authorized: bool,
                   public_export_allowed: bool) -> Dict[str, Any]:
    """Three flags, kept separate. Target consumer usability requires
    the first two; public export is an independent permission that
    neither grants nor removes target use."""
    return {
        "private_ai_consumption": bool(private_ai_consumption),
        "materialization_authorized": bool(materialization_authorized),
        "public_export_allowed": bool(public_export_allowed),
        "consumer_usable": bool(private_ai_consumption
                                and materialization_authorized),
    }


def treat_as_data(text: str) -> Dict[str, Any]:
    """Instruction-like text is returned as DATA with no action taken.
    Detection only labels the text; it never executes, forwards, or
    applies any instruction found inside it."""
    lowered = text.lower()
    instruction_like = any(p in text or p in lowered
                           for p in _INSTRUCTION_PATTERNS)
    return {"kind": "DATA", "text": text, "executed": False,
            "action_taken": "NONE", "instruction_like": instruction_like}


def refuse_claim_merge(fact: Dict[str, Any], claim: Dict[str, Any]) -> None:
    """F-01 guard (repair round 1): Claims resolve in a separate
    namespace and are NEVER merged into a Fact. There is no legal
    Claim-to-Fact application, so any code path attempting one must go
    through this guard and be refused. This function always raises:
    its existence is the choke point, and the raise is the enforcement.
    The Fact is never modified (the raise happens before any write,
    and this function performs no write at all)."""
    fact_id = fact.get("fact_id", "unknown") if isinstance(fact, dict) \
        else "unknown"
    raise ValueError(
        f"CLAIM_MERGE_INTO_FACT_REFUSED:{fact_id}")
