"""REQ-241 P0-5: Local zero-network Wikipedia draft candidate extractor (stdlib only).

Scans local `.md`, `.txt`, `.json`, and `.jsonl` files under `sources_dir` against
a read-only `person_registry`, producing human-review-only candidate drafts for:
- `bio_profile_candidates`
- `story_and_timeline_candidates`

Strict governance invariants:
- 100% local, zero network calls (`network_called=False`).
- 0 auto-writes to canonical stores (`canonical_stores_modified=False`).
- Every candidate is tagged `review_status="DRAFT_PENDING_HUMAN_REVIEW"`.
- Never emits local absolute paths (records relative paths only).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .common import atomic
from .person_registry import validate_person_registry

DRAFT_REVIEW_STATUS = "DRAFT_PENDING_HUMAN_REVIEW"
WIKI_DRAFT_CANDIDATES_SCHEMA = "cba-kb.wiki-draft-candidates.v1"

_SUPPORTED_SUFFIXES = frozenset({".md", ".txt", ".json", ".jsonl"})
_BIRTH_CN_RE = re.compile(r"((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_BIRTH_ISO_RE = re.compile(r"\b((?:19|20)\d{2}-\d{2}-\d{2})\b")
_HEIGHT_RE = re.compile(r"身高\s*(\d{3})\s*(?:cm|CM|厘米)")
_WEIGHT_RE = re.compile(r"体重\s*(\d{2,3})\s*(?:kg|KG|公斤)")
_POSITION_RE = re.compile(r"司职\s*([前锋后卫中锋控球得分小大/]{2,10})")
_YOUTH_RE = re.compile(r"出自\s*([^\s，。；、！？]{2,20}(?:青训体系|青训|梯队|选秀))")
_SEASON_RE = re.compile(r"(20\d{2}-20\d{2})\s*赛季")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？\n])")


def _load_person_registry(
    person_registry: Mapping[str, Any] | str | Path,
) -> Dict[str, Any]:
    if isinstance(person_registry, Mapping):
        return validate_person_registry(person_registry)
    reg_path = Path(person_registry)
    if not reg_path.is_file():
        raise ValueError(f"PERSON_REGISTRY_NOT_FOUND:{reg_path.name}")
    loaded = json.loads(reg_path.read_text(encoding="utf-8"))
    return validate_person_registry(loaded)


def _read_source_documents(sources_root: Path) -> List[Tuple[str, str, str]]:
    """Return list of (rel_posix_path, title, body_text) from supported local files."""
    docs: List[Tuple[str, str, str]] = []
    for path in sorted(sources_root.rglob("*"), key=lambda p: p.as_posix()):
        if not path.is_file() or path.suffix.lower() not in _SUPPORTED_SUFFIXES:
            continue
        rel = path.relative_to(sources_root).as_posix()
        suffix = path.suffix.lower()
        raw_text = path.read_text(encoding="utf-8")
        if suffix in (".md", ".txt"):
            title = path.stem
            for line in raw_text.splitlines():
                s = line.strip()
                if s.startswith("#"):
                    title = s.lstrip("#").strip() or title
                    break
            docs.append((rel, title, raw_text))
        elif suffix == ".jsonl":
            for idx, line in enumerate(raw_text.splitlines(), start=1):
                s = line.strip()
                if not s:
                    continue
                obj = json.loads(s)
                if isinstance(obj, Mapping):
                    title = str(obj.get("title") or f"{path.stem}#{idx}").strip()
                    body = str(
                        obj.get("text")
                        or obj.get("body")
                        or obj.get("content")
                        or obj.get("summary")
                        or ""
                    )
                    docs.append((rel, title, f"{title}\n{body}".strip()))
        else:
            parsed = json.loads(raw_text)
            items: Sequence[Any] = parsed if isinstance(parsed, list) else [parsed]
            for idx, obj in enumerate(items, start=1):
                if isinstance(obj, Mapping):
                    title = str(obj.get("title") or f"{path.stem}#{idx}").strip()
                    body = str(
                        obj.get("text")
                        or obj.get("body")
                        or obj.get("content")
                        or obj.get("summary")
                        or ""
                    )
                    docs.append((rel, title, f"{title}\n{body}".strip()))
    return docs


def _extract_bio_fields_from_text(text: str) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    m_birth = _BIRTH_CN_RE.search(text)
    if m_birth:
        y, mo, d = int(m_birth.group(1)), int(m_birth.group(2)), int(m_birth.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            fields["birth_date"] = f"{y:04d}-{mo:02d}-{d:02d}"
    else:
        m_iso = _BIRTH_ISO_RE.search(text)
        if m_iso:
            fields["birth_date"] = m_iso.group(1)

    m_h = _HEIGHT_RE.search(text)
    if m_h:
        h_val = int(m_h.group(1))
        if h_val > 0:
            fields["height_cm"] = h_val

    m_w = _WEIGHT_RE.search(text)
    if m_w:
        w_val = int(m_w.group(1))
        if w_val > 0:
            fields["weight_kg"] = w_val

    m_pos = _POSITION_RE.search(text)
    if m_pos:
        fields["position"] = m_pos.group(1).strip()

    m_youth = _YOUTH_RE.search(text)
    if m_youth:
        fields["draft_or_youth_origin"] = m_youth.group(1).strip()

    return fields


def _find_mentioned_persons(
    text: str,
    persons: Sequence[Mapping[str, Any]],
) -> List[Dict[str, str]]:
    hits: List[Dict[str, str]] = []
    seen_uids: set[str] = set()
    for p in persons:
        puid = str(p["person_uid"])
        cname = str(p["canonical_name"])
        names = [cname, *(str(a) for a in (p.get("aliases") or ()))]
        if any(n and n in text for n in names):
            if puid not in seen_uids:
                seen_uids.add(puid)
                hits.append({"person_uid": puid, "canonical_name": cname})
    return hits


def extract_wiki_candidates(
    *,
    sources_dir: str | Path,
    person_registry: Mapping[str, Any] | str | Path,
    output_path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """Extract draft bio & story/timeline candidates locally for human review.

    Never performs network calls and never modifies canonical stores.
    """
    sources_root = Path(sources_dir)
    if not sources_root.is_dir():
        raise ValueError(f"SOURCES_DIR_NOT_FOUND:{sources_root.name}")

    validated_registry = _load_person_registry(person_registry)
    persons = validated_registry["persons"]
    docs = _read_source_documents(sources_root)

    bio_candidates: List[Dict[str, Any]] = []
    story_candidates: List[Dict[str, Any]] = []
    seen_bio_keys: set[Tuple[str, str]] = set()
    seen_story_keys: set[Tuple[str, str, str]] = set()

    for rel_path, title, body in docs:
        sentences = [
            seg.strip()
            for seg in _SENTENCE_SPLIT_RE.split(body)
            if seg.strip() and not seg.strip().startswith("#")
        ]
        for sent in sentences:
            mentioned = _find_mentioned_persons(sent, persons)
            if not mentioned:
                continue

            bio_fields = _extract_bio_fields_from_text(sent)
            if bio_fields:
                # Attach bio fields to the first person mentioned in that sentence
                primary = mentioned[0]
                bkey = (primary["person_uid"], rel_path)
                if bkey not in seen_bio_keys:
                    seen_bio_keys.add(bkey)
                    bio_candidates.append({
                        "person_uid": primary["person_uid"],
                        "canonical_name": primary["canonical_name"],
                        "extracted_fields": bio_fields,
                        "source_file": rel_path,
                        "source_title": title,
                        "evidence_snippet": sent[:200],
                        "review_status": DRAFT_REVIEW_STATUS,
                    })

            season_match = _SEASON_RE.search(sent)
            if season_match or len(mentioned) >= 2:
                occ_season = season_match.group(1) if season_match else "UNKNOWN"
                skey = (rel_path, occ_season, sent[:80])
                if skey not in seen_story_keys:
                    seen_story_keys.add(skey)
                    story_candidates.append({
                        "title": title,
                        "occurred_season": occ_season,
                        "mentioned_person_uids": [m["person_uid"] for m in mentioned],
                        "mentioned_names": [m["canonical_name"] for m in mentioned],
                        "summary": sent[:240],
                        "source_file": rel_path,
                        "review_status": DRAFT_REVIEW_STATUS,
                    })

    result: Dict[str, Any] = {
        "schema": WIKI_DRAFT_CANDIDATES_SCHEMA,
        "review_status": DRAFT_REVIEW_STATUS,
        "canonical_stores_modified": False,
        "network_called": False,
        "sources_scanned": len(docs),
        "bio_profile_candidates": bio_candidates,
        "story_and_timeline_candidates": story_candidates,
    }

    if output_path is not None:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        atomic(
            out,
            (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )

    return result
