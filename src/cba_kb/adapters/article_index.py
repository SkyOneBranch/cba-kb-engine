"""RC-05 article index and data lineage (T2-5, Issue #208).

Builds a consumable candidate index from article records as produced by
``foreign_xlsx.article_rows()``. Every emitted candidate carries full
lineage — (source_file, source_sheet, source_row) — so a consumer can
jump back to the exact original row (AC-25). Records without lineage
are NEVER emitted as consumable candidates; they are reported in the
index's ``rejected`` list with a reason instead.

Contract invariants (fail-closed):
  - ``article_id`` text is preserved verbatim — no numeric
    normalization, no prefix guessing (the 序号 / serial column is a
    row ordinal, never an article identity).
  - Dates are carried verbatim: ``date_interpretation`` is always
    ``None`` and ``date_verified`` always ``False`` here; a date is
    never inferred to be a birthday or a publish time (AC-22).
  - Identity stays ``candidate`` and ``person_id`` stays ``None``:
    titles/nicknames are retrieval candidates only, never Person IDs
    (AC-23). Person -> article associations are ``UNVERIFIED`` until a
    human or a trusted external link confirms them.

This module is a pure converter/indexer: it performs no registry
writes and does not wire itself into ``adapters/__init__`` (that is
T2-8 integration work).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from cba_kb.document_lane import normalize_text

INDEX_SCHEMA = "article_index_v1"


class ArticleIndexError(Exception):
    """Raised when an article index cannot be built or queried safely."""


def _lineage_of(record, source_file):
    """Return the lineage dict for a record, or None if incomplete."""
    sheet = record.get("source_sheet")
    row = record.get("source_row")
    # B-01: whitespace-only strings are NOT lineage — strip-checked.
    if not isinstance(sheet, str) or not sheet.strip():
        return None
    if not isinstance(row, int) or isinstance(row, bool) or row < 1:
        return None
    return {"source_file": source_file, "source_sheet": sheet,
            "source_row": row}


def _index_id(lineage):
    # Identity of a candidate is its source location only. Two records
    # claiming the same (sheet, row) must collide -> duplicate detected.
    # B-03: canonical JSON array serialization — unambiguous encoding,
    # so ("a|b","c") and ("a","b|c") can never produce the same input.
    # R3: hash the RAW UTF-8 bytes — do NOT route through
    # document_lane.content_hash, whose text normalization (NFKC +
    # whitespace folding) would conflate distinct coordinates such as
    # sheet "A" vs "Ａ" (fullwidth) or "a b" vs "a  b".
    import hashlib as _hashlib
    import json as _json
    parts = _json.dumps(
        [INDEX_SCHEMA, lineage["source_file"], lineage["source_sheet"],
         lineage["source_row"]],
        ensure_ascii=False, separators=(",", ":"))
    digest = _hashlib.sha256(parts.encode("utf-8")).hexdigest()
    return "article_" + digest[:24]


def build_article_index(articles, *, source_file):
    """Build the candidate index for one source file's article records.

    ``articles`` are records in the ``foreign_xlsx.article_rows()``
    shape. Returns::

        {"schema": INDEX_SCHEMA, "source_file": source_file,
         "candidates": [...], "rejected": [...]}

    A record lands in ``rejected`` (never in ``candidates``) when it
    lacks complete lineage (``ARTICLE_NO_LINEAGE``) or a usable title
    (``ARTICLE_NO_TITLE``). Two records claiming the same source row
    are a data-integrity conflict and raise ``ArticleIndexError``.
    """
    if not isinstance(articles, list):
        raise ArticleIndexError("ARTICLE_RECORDS_LIST_REQUIRED")
    if not isinstance(source_file, str) or not source_file.strip():
        raise ArticleIndexError("ARTICLE_SOURCE_FILE_REQUIRED")
    candidates: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    seen_ids = set()
    for record in articles:
        if not isinstance(record, dict):
            raise ArticleIndexError("ARTICLE_RECORD_OBJECT_REQUIRED")
        article_id = record.get("article_id")
        lineage = _lineage_of(record, source_file)
        if lineage is None:
            rejected.append({
                "article_id": article_id,
                "source_sheet": record.get("source_sheet"),
                "source_row": record.get("source_row"),
                "reason": "ARTICLE_NO_LINEAGE",
            })
            continue
        # B-02: reserve the source location as soon as lineage is
        # established — BEFORE title validation. A rejected record still
        # occupies its row; a second record claiming the same row is an
        # integrity conflict regardless of the first record's fate.
        index_id = _index_id(lineage)
        if index_id in seen_ids:
            raise ArticleIndexError(
                "ARTICLE_DUPLICATE_SOURCE_ROW:"
                f"{lineage['source_sheet']}!{lineage['source_row']}")
        seen_ids.add(index_id)
        title = record.get("title")
        if not isinstance(title, str) or not title.strip():
            rejected.append({
                "article_id": article_id,
                "source_sheet": lineage["source_sheet"],
                "source_row": lineage["source_row"],
                "reason": "ARTICLE_NO_TITLE",
            })
            continue
        candidates.append({
            "index_id": index_id,
            # Verbatim text: no numeric normalization, no guessing.
            "article_id": article_id,
            "title": title,
            "row_date_text": record.get("row_date_text") or "",
            "row_date_raw": record.get("row_date_raw"),
            # AC-22: date meaning is never inferred by this index.
            "date_interpretation": None,
            "date_verified": False,
            # AC-23: retrieval candidate only; never a Person ID.
            "identity_status": "candidate",
            "person_id": None,
            "lineage": lineage,
        })
    return {"schema": INDEX_SCHEMA, "source_file": source_file,
            "candidates": candidates, "rejected": rejected}


def _candidates(index):
    if not isinstance(index, dict) or index.get("schema") != INDEX_SCHEMA:
        raise ArticleIndexError("ARTICLE_INDEX_REQUIRED")
    return index.get("candidates") or []


def resolve_lineage(candidate):
    """Return the (source_file, source_sheet, source_row) of a candidate.

    This is the final hop of the AC-25 path: from a candidate back to
    the exact original row. A candidate without complete lineage is
    not consumable and raises ``ArticleIndexError``.
    """
    if not isinstance(candidate, dict):
        raise ArticleIndexError("ARTICLE_CANDIDATE_OBJECT_REQUIRED")
    lineage = candidate.get("lineage")
    if not isinstance(lineage, dict):
        raise ArticleIndexError("ARTICLE_LINEAGE_MISSING")
    source_file = lineage.get("source_file")
    source_sheet = lineage.get("source_sheet")
    source_row = lineage.get("source_row")
    if (not isinstance(source_file, str) or not source_file.strip()
            or not isinstance(source_sheet, str)
            or not source_sheet.strip()
            or not isinstance(source_row, int)
            or isinstance(source_row, bool) or source_row < 1):
        raise ArticleIndexError("ARTICLE_LINEAGE_INCOMPLETE")
    return {"source_file": source_file, "source_sheet": source_sheet,
            "source_row": source_row}


def find_by_article_id(index, article_id) -> Optional[Dict[str, Any]]:
    """Exact lookup by verbatim article_id text; None when absent."""
    for cand in _candidates(index):
        if cand.get("article_id") == article_id:
            return cand
    return None


def search_by_title(index, query):
    """Substring title search over index candidates (normalized text)."""
    if not isinstance(query, str) or not query.strip():
        raise ArticleIndexError("ARTICLE_QUERY_REQUIRED")
    needle = normalize_text(query).strip()
    return [cand for cand in _candidates(index)
            if needle in normalize_text(cand.get("title") or "")]


def candidate_articles_for_person(index, person_name):
    """AC-25: articles whose title mentions ``person_name``.

    Returns UNVERIFIED associations — a title mention is a retrieval
    signal, never proof the article is about that person. Each
    association's candidate carries lineage for the jump back to the
    original row via :func:`resolve_lineage`.
    """
    if not isinstance(person_name, str) or not person_name.strip():
        raise ArticleIndexError("ARTICLE_PERSON_NAME_REQUIRED")
    needle = normalize_text(person_name).strip()
    associations = []
    for cand in _candidates(index):
        if needle in normalize_text(cand.get("title") or ""):
            associations.append({
                "association_status": "UNVERIFIED",
                "person_name": person_name,
                "candidate": cand,
            })
    return associations
