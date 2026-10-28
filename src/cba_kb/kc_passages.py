"""PR-B (Issue #220) Document/Passage routing — offline/synthetic only.

Candidate collection is COMPLETED across all pages/slices BEFORE any
ranking or selection. A passage is a validated derived view, never a
second Truth: verbatim conclusions are emitted only when the excerpt
verifies against the source text and digest. A Top-5 selection is made
only from a first COMPLETE consumable candidate set; selecting from an
incomplete set is refused. File-ID order is a deterministic tie-breaker
at most, never relevance. All artifacts are SIMULATED; nothing here is
real publication, real authorization, or real Drive readback.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .kc_projection import (
    KcError,
    build_passage,
    route_passages,
    validate_passage,
)


class KcPassageError(KcError, ValueError):
    """Fail-closed passage contract violation (also a ValueError, so a
    missing evidence-chain link is catchable as a value rejection)."""


def build_routed_passage(*, source_id: str, revision: str, locator: str,
                         text: str, player_uid: Optional[str] = None,
                         aliases: Optional[List[str]] = None,
                         title: Optional[str] = None,
                         speaker: Optional[str] = None,
                         attribution: Optional[str] = None,
                         authorized_url: Optional[str] = None
                         ) -> Dict[str, Any]:
    """A routed passage: Slice A digest binding plus the consumer-safe
    representation (consumer_source_id), authorized title, speaker /
    attribution where present, and the authorized-copy/original URL."""
    passage = build_passage(source_id=source_id, revision=revision,
                            locator=locator, text=text,
                            player_uid=player_uid, aliases=aliases)
    passage["title"] = title if title is not None else source_id
    passage["speaker"] = speaker
    passage["attribution"] = attribution
    passage["authorized_url"] = authorized_url
    passage["consumer_source_id"] = source_id
    return passage


def verify_passage_text(passage: Dict[str, Any],
                        source_text: str) -> bool:
    """True only when the excerpt equals the source text AND the digest
    validates. Either failure refuses a verbatim conclusion."""
    return (passage.get("text") == source_text
            and validate_passage(passage)["valid"])


def cite_verbatim(passage: Dict[str, Any],
                  source_text: str) -> Dict[str, Any]:
    """Verbatim citation, or UNKNOWN with no text. A failed verification
    never yields a partial or paraphrased quote presented as verbatim."""
    if verify_passage_text(passage, source_text):
        return {"status": "OK", "grain": "DOCUMENT",
                "verbatim": passage["text"],
                "source_id": passage["source_id"],
                "revision": passage["revision"],
                "locator": passage["locator"],
                "digest": passage["digest"]}
    return {"status": "UNKNOWN", "grain": "DOCUMENT",
            "reason": "PASSAGE_NOT_VERIFIED", "verbatim": None}


def _page_passages(page: Any) -> tuple:
    """A page is either a plain list of passages (complete by
    construction) or a dict {passages, end_marker}."""
    if isinstance(page, dict):
        return page.get("passages", []), bool(page.get("end_marker"))
    return list(page), True


def collect_candidates(pages: List[Any], *, alias: str,
                       topic_tokens: List[str]) -> Dict[str, Any]:
    """Flatten EVERY page first, then rank. Ranking uses Slice A
    route_passages scoring (approved-alias match, then topic-token
    matches); source_id order is only a tie-breaker inside that
    function. If any page is incomplete, the collection is PARTIAL and
    must never be read as complete."""
    flat: List[Dict[str, Any]] = []
    all_complete = True
    for page in pages:
        passages, complete = _page_passages(page)
        flat.extend(passages)
        all_complete = all_complete and complete
    ranked = route_passages(flat, alias=alias, topic_tokens=topic_tokens)
    complete = all_complete and ranked["complete"]
    return {"status": "COMPLETE" if complete else "PARTIAL",
            "complete": complete, "end_marker": complete,
            "total_pages": len(pages),
            "total_candidates": ranked["total_candidates"],
            "candidates": ranked["candidates"]}


def first_complete_set(collected: Dict[str, Any], *,
                       limit: int = 5) -> Dict[str, Any]:
    """The first consumable candidate set, of at most ``limit`` entries,
    taken ONLY from a complete collection. An incomplete collection is
    refused (PARTIAL, no candidates) rather than silently truncated."""
    if not collected.get("complete"):
        return {"status": "PARTIAL", "complete": False,
                "end_marker": False, "truncated": False,
                "total_candidates": collected.get("total_candidates", 0),
                "candidates": []}
    candidates = collected["candidates"]
    total = len(candidates)
    return {"status": "COMPLETE", "complete": True, "end_marker": True,
            "truncated": total > limit,
            "total_candidates": collected.get("total_candidates", total),
            "candidates": candidates[:limit]}


def evidence_chain(*, receipt_id: str, snapshot_id: str,
                   build_version: str, source_digest: str,
                   authorization_basis: str) -> Dict[str, Any]:
    """Query receipt -> snapshot -> build -> source digest ->
    authorization basis. Every link is required; a missing link is a
    ValueError, never a gap silently bridged."""
    links = {"receipt_id": receipt_id, "snapshot_id": snapshot_id,
             "build_version": build_version,
             "source_digest": source_digest,
             "authorization_basis": authorization_basis}
    for name, value in links.items():
        if not value:
            raise KcPassageError(f"EVIDENCE_CHAIN_LINK_MISSING:{name}")
    return dict(links, chain=["receipt", "snapshot", "build",
                              "source_digest", "authorization_basis"])
