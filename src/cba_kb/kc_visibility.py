"""PR-B (Issue #220) target-first rights filtering — offline/synthetic
only.

The authorized ID set is applied BEFORE any consumer-visible surface
(catalog, index, count, summary, path, diagnostic) is built — by
target_first_view/query_visible in this module, and (repair round 1)
as a required argument of the PR-B builders in kc_slices, kc_passages,
and kc_snapshot. Narrowing (Route A, Q2 round 2 FAIL response): the
required argument constrains the builders that take it, but the raw
builders remain directly callable, so builder-level filtering alone is
NOT the consumer trust boundary. The consumer boundary is
kc_consume.ConsumeSession, which translates every consumer surface
(including raw distinguishing outcomes) into uniform results and
refuses unverified artifacts. F-02:
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
    and this function performs no write at all). Route A: the guard is
    invoked on the integrated path by
    kc_consume.ConsumeSession.consume_answer() for every
    fact-colliding claim; the refusal is recorded in the answer's
    claim layer, and the claim is reported only there."""
    fact_id = fact.get("fact_id", "unknown") if isinstance(fact, dict) \
        else "unknown"
    raise ValueError(
        f"CLAIM_MERGE_INTO_FACT_REFUSED:{fact_id}")


# ---------------------------------------------------------------------------
# REQ-232 P0-4: Dual-Visibility Card Export & Fail-Closed Public-Safe Guard
# ---------------------------------------------------------------------------

CARD_VISIBILITY_MODES = ("internal_full", "public_safe")

_PUBLIC_SAFE_FORBIDDEN_SUBSTRINGS = (
    "web 剪藏",
    "## document passage",
    "[internal_clue_only",
)
_PUBLIC_SAFE_TEN_SOURCES_RE = re.compile(r"10[\s_-]+sources", re.IGNORECASE)
_PUBLIC_SAFE_SCHEME_RE = re.compile(r"(?:r2|s3|file)://", re.IGNORECASE)
_PUBLIC_SAFE_POSIX_LOCAL_ROOTS = (
    "Users|home|tmp|var|etc|opt|srv|data|private|root|mnt|Volumes|Library|Applications"
)
_PUBLIC_SAFE_LOCAL_PATH_RE = re.compile(
    r"(?:(?<![A-Za-z0-9_.:/])/(?:" + _PUBLIC_SAFE_POSIX_LOCAL_ROOTS + r")/[^\s)\]<>\"',;]+"
    r"|(?<![A-Za-z0-9_.:/\\])[A-Za-z]:[\\/][^\s)\]<>\"',;]+"
    r"|(?<![A-Za-z0-9_.:/\\])\\\\[A-Za-z0-9_.-]+\\[^\s)\]<>\"',;]+)"
)
_PUBLIC_SAFE_SHA256_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")
_PUBLIC_SAFE_RAW_SHA_KEY_RE = re.compile(r"\braw_sha256\b", re.IGNORECASE)
_PUBLIC_SAFE_DOC_ID_RE = re.compile(
    r"(?:\bdoc:[A-Za-z0-9_.:-]+|\bSnapshot doc_id:|\bdoc_id\s*:|"
    r"\b(?:prior_doc_id|new_doc_id|prior_record_id|new_record_id)\b)",
    re.IGNORECASE,
)
_PUBLIC_SAFE_DOC_HEX_RE = re.compile(r"\bdoc_[0-9a-f]{16,64}\b", re.IGNORECASE)
_PUBLIC_SAFE_UNASSIGNED_RE = re.compile(r"\(unassigned\)", re.IGNORECASE)
_PUBLIC_SAFE_HTTP_URL_RE = re.compile(r"https?://[^\s)\]<>\"']+", re.IGNORECASE)
_PUBLIC_SAFE_DRIVE_CANDIDATE_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{25,}(?![A-Za-z0-9_-])")
_HISTORICAL_ARCHIVE_BANNER_PREFIX = "> [HISTORICAL_SEASON_ARCHIVE] "


def _is_public_safe_drive_like_token(token: str, is_raw_drive_token_fn: Any) -> bool:
    if is_raw_drive_token_fn(token):
        return True
    if len(token) >= 25 and token.count("_") <= 2 and token.count("-") <= 2:
        upper_count = sum(1 for c in token if c.isupper())
        lower_count = sum(1 for c in token if c.islower())
        if upper_count >= 2 and lower_count >= 2:
            return True
    return False


def validate_card_visibility(visibility: str) -> str:
    """Validate card visibility mode; fail closed on any unknown value."""
    if not isinstance(visibility, str) or visibility not in CARD_VISIBILITY_MODES:
        raise ValueError(f"INVALID_CARD_VISIBILITY:{visibility!r}")
    return visibility


def verify_public_safe_card_text(markdown: str) -> None:
    """Fail-closed leak guard for public_safe rendered research cards (REQ-232 P0-4).

    Raises ValueError("PUBLIC_SAFE_REDACTION_VIOLATION:...") if any private path,
    cloud bucket URI, Document Passage verbatim quote, fenced code block, internal
    doc_id / revision record id, raw_sha256 / 64-hex hash, or raw Google Drive ID / URL
    appears in the text.
    """
    if not isinstance(markdown, str):
        raise ValueError("PUBLIC_SAFE_REDACTION_VIOLATION:non_string_markdown")

    violations: List[str] = []
    lowered = markdown.casefold()

    for forbidden in _PUBLIC_SAFE_FORBIDDEN_SUBSTRINGS:
        if forbidden in lowered:
            violations.append(f"forbidden_marker:{forbidden}")

    if _PUBLIC_SAFE_TEN_SOURCES_RE.search(markdown):
        violations.append("forbidden_marker:10_sources")

    if _PUBLIC_SAFE_SCHEME_RE.search(markdown):
        violations.append("private_uri_scheme")

    if _PUBLIC_SAFE_LOCAL_PATH_RE.search(markdown):
        violations.append("local_path")

    if _PUBLIC_SAFE_RAW_SHA_KEY_RE.search(markdown):
        violations.append("raw_sha256_field")

    if _PUBLIC_SAFE_SHA256_RE.search(markdown):
        violations.append("hex64_digest")

    if _PUBLIC_SAFE_DOC_ID_RE.search(markdown):
        violations.append("internal_doc_id")

    if _PUBLIC_SAFE_DOC_HEX_RE.search(markdown):
        violations.append("internal_doc_hex_id")

    if _PUBLIC_SAFE_UNASSIGNED_RE.search(markdown):
        violations.append("unassigned_placeholder")

    if "```" in markdown or "~~~" in markdown:
        violations.append("fenced_code_block")

    for line in markdown.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(">") and not stripped.startswith(_HISTORICAL_ARCHIVE_BANNER_PREFIX):
            violations.append("verbatim_blockquote")
            break

    from .consumer_surface import (
        _DRIVE_PREFIXED_ID_RE,
        _DRIVE_URL_RE,
        _is_raw_drive_token,
    )

    if _DRIVE_URL_RE.search(markdown):
        violations.append("drive_url")
    if _DRIVE_PREFIXED_ID_RE.search(markdown):
        violations.append("drive_prefixed_id")
    text_without_public_urls = _PUBLIC_SAFE_HTTP_URL_RE.sub(" ", markdown)
    for match in _PUBLIC_SAFE_DRIVE_CANDIDATE_RE.finditer(text_without_public_urls):
        if _is_public_safe_drive_like_token(match.group(0), _is_raw_drive_token):
            violations.append("raw_drive_id")
            break

    if violations:
        raise ValueError("PUBLIC_SAFE_REDACTION_VIOLATION:" + ",".join(violations))
