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

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .common import atomic
from .overseas_tenure import (
    D01_RIGHTS_TIER,
    OVERSEAS_TENURE_SCHEMA,
    OverseasTenureError,
    validate_overseas_tenure,
)
from .person_registry import validate_person_registry
from .story_record import is_hollow_story_template
from .youth_tenure import (
    YOUTH_NATIONAL_TENURE_SCHEMA,
    YouthTenureError,
    validate_youth_national_tenure,
)

DRAFT_REVIEW_STATUS = "DRAFT_PENDING_HUMAN_REVIEW"
ANCHOR_VERIFICATION_FAILED = "ANCHOR_VERIFICATION_FAILED"
WIKI_DRAFT_CANDIDATES_SCHEMA = "cba-kb.wiki-draft-candidates.v1"

_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")

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


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def verify_quote_anchor(
    quote_span: str,
    raw_sha256: str,
    evidence_raw_bytes: bytes,
) -> Tuple[bool, Optional[str]]:
    """Deterministically verify a quote anchor against raw evidence bytes.

    Requires:
    1. Exact SHA-256 match between `evidence_raw_bytes` and `raw_sha256`.
    2. Exact substring match of whitespace-normalized `quote_span` inside
       whitespace-normalized UTF-8 decoded `evidence_raw_bytes`.
    """
    if not isinstance(evidence_raw_bytes, (bytes, bytearray)):
        return False, "EVIDENCE_BYTES_REQUIRED"
    if not isinstance(raw_sha256, str):
        return False, "RAW_SHA256_REQUIRED"
    expected_sha = raw_sha256.strip().lower()
    if not _HEX64_RE.fullmatch(expected_sha):
        return False, f"RAW_SHA256_INVALID:{expected_sha}"
    actual_sha = hashlib.sha256(bytes(evidence_raw_bytes)).hexdigest()
    if actual_sha != expected_sha:
        return False, f"RAW_SHA256_MISMATCH:expected={expected_sha}:actual={actual_sha}"

    if not isinstance(quote_span, str) or not quote_span.strip():
        return False, "QUOTE_SPAN_EMPTY"
    try:
        evidence_text = bytes(evidence_raw_bytes).decode("utf-8")
    except UnicodeDecodeError:
        return False, "EVIDENCE_NOT_VALID_UTF8"

    norm_evidence = _normalize_whitespace(evidence_text)
    norm_span = _normalize_whitespace(quote_span)
    if not norm_span:
        return False, "QUOTE_SPAN_NOT_FOUND_IN_EVIDENCE"
    if norm_span not in norm_evidence:
        cjk_ws_re = re.compile(r"(?<=[\u4e00-\u9fff，。！？；：、（）])\s+(?=[\u4e00-\u9fff，。！？；：、（）])")
        if cjk_ws_re.sub("", norm_span) not in cjk_ws_re.sub("", norm_evidence):
            return False, "QUOTE_SPAN_NOT_FOUND_IN_EVIDENCE"
    return True, None


def _build_evidence_index(evidence_dir: str | Path) -> Dict[str, bytes]:
    root = Path(evidence_dir)
    if not root.is_dir():
        raise ValueError(f"EVIDENCE_DIR_NOT_FOUND:{root.name}")
    index: Dict[str, bytes] = {}
    for path in sorted(root.rglob("*"), key=lambda p: p.as_posix()):
        if not path.is_file():
            continue
        raw = path.read_bytes()
        index[hashlib.sha256(raw).hexdigest()] = raw
    return index


def verify_candidate_anchors(
    candidates: Sequence[Mapping[str, Any]],
    *,
    evidence_index: Mapping[str, bytes],
) -> Dict[str, Any]:
    """Verify quote anchors and evidence_refs across candidate draft records.

    Supports candidate StoryRecords (`quote_anchors`), `person_overseas_tenures`
    (`evidence_refs` + `D01_FACTUAL_INDEX_ONLY`), and `youth_national_tenures`
    (`evidence_refs` + 4-state enum). Any failure marks the candidate
    `review_status = ANCHOR_VERIFICATION_FAILED` and excludes it from
    `approvable_candidates` / `approved_candidates`.
    """
    norm_index: Dict[str, bytes] = {
        str(k).strip().lower(): bytes(v)
        for k, v in (evidence_index or {}).items()
        if isinstance(v, (bytes, bytearray))
    }

    all_candidates: List[Dict[str, Any]] = []
    approvable: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    all_errors: List[Dict[str, Any]] = []

    for idx, raw_cand in enumerate(candidates or ()):
        if not isinstance(raw_cand, Mapping):
            err_rec = {"index": idx, "candidate_id": f"idx-{idx}", "error": "CANDIDATE_OBJECT_REQUIRED"}
            all_errors.append(err_rec)
            continue
        item = dict(raw_cand)
        cand_id = str(
            item.get("candidate_id")
            or item.get("story_id")
            or item.get("person_uid")
            or f"candidate-{idx}"
        )
        item_errors: List[str] = []

        schema = str(item.get("schema") or "").strip()

        # 1. Overseas tenure D-01 validation
        if schema == OVERSEAS_TENURE_SCHEMA or (
            "rights_tier" in item and "tenure_type" in item
        ):
            if item.get("rights_tier") != D01_RIGHTS_TIER:
                item_errors.append(f"INVALID_RIGHTS_TIER:{item.get('rights_tier')}")
            try:
                validate_overseas_tenure(item)
            except OverseasTenureError as exc:
                item_errors.append(f"OVERSEAS_TENURE_INVALID:{exc}")

        # 2. Youth national tenure 4-state validation
        if schema == YOUTH_NATIONAL_TENURE_SCHEMA or "participation_stage" in item:
            try:
                validate_youth_national_tenure(item)
            except YouthTenureError as exc:
                item_errors.append(f"YOUTH_TENURE_INVALID:{exc}")

        # 3. Story quote_anchors deterministic verification
        if "quote_anchors" in item:
            raw_anchors = item.get("quote_anchors")
            if (
                isinstance(raw_anchors, (str, bytes))
                or not isinstance(raw_anchors, Sequence)
                or len(raw_anchors) == 0
            ):
                item_errors.append("QUOTE_ANCHORS_NON_EMPTY_LIST_REQUIRED")
            else:
                for a_idx, anchor in enumerate(raw_anchors):
                    if not isinstance(anchor, Mapping):
                        item_errors.append(f"QUOTE_ANCHOR[{a_idx}]:OBJECT_REQUIRED")
                        continue
                    sha = str(anchor.get("raw_sha256") or "").strip().lower()
                    span = anchor.get("quote_span")
                    if not _HEX64_RE.fullmatch(sha):
                        item_errors.append(f"QUOTE_ANCHOR[{a_idx}]:RAW_SHA256_INVALID")
                        continue
                    ev_bytes = norm_index.get(sha)
                    if ev_bytes is None:
                        item_errors.append(f"QUOTE_ANCHOR[{a_idx}]:EVIDENCE_SHA256_NOT_FOUND:{sha}")
                        continue
                    ok, reason = verify_quote_anchor(str(span or ""), sha, ev_bytes)
                    if not ok:
                        item_errors.append(f"QUOTE_ANCHOR[{a_idx}]:{reason}")

        # 4. evidence_refs raw_sha256 verification (overseas/youth/general)
        if "evidence_refs" in item:
            raw_refs = item.get("evidence_refs")
            if (
                isinstance(raw_refs, (str, bytes))
                or not isinstance(raw_refs, Sequence)
                or len(raw_refs) == 0
            ):
                item_errors.append("EVIDENCE_REFS_NON_EMPTY_LIST_REQUIRED")
            else:
                for r_idx, ref in enumerate(raw_refs):
                    if not isinstance(ref, Mapping):
                        item_errors.append(f"EVIDENCE_REF[{r_idx}]:OBJECT_REQUIRED")
                        continue
                    sha = str(ref.get("raw_sha256") or "").strip().lower()
                    if not _HEX64_RE.fullmatch(sha):
                        item_errors.append(f"EVIDENCE_REF[{r_idx}]:RAW_SHA256_INVALID")
                        continue
                    ev_bytes = norm_index.get(sha)
                    if ev_bytes is None:
                        item_errors.append(f"EVIDENCE_REF[{r_idx}]:EVIDENCE_SHA256_NOT_FOUND:{sha}")
                        continue
                    actual_sha = hashlib.sha256(ev_bytes).hexdigest()
                    if actual_sha != sha:
                        item_errors.append(f"EVIDENCE_REF[{r_idx}]:RAW_SHA256_MISMATCH")

        if item_errors:
            item["review_status"] = ANCHOR_VERIFICATION_FAILED
            item["anchor_verification_errors"] = list(item_errors)
            rejected.append(item)
            for err in item_errors:
                all_errors.append({"candidate_id": cand_id, "error": err})
        else:
            approvable.append(item)
        all_candidates.append(item)

    return {
        "candidates": all_candidates,
        "approvable_candidates": approvable,
        "approved_candidates": approvable,
        "rejected_candidates": rejected,
        "anchor_verification_errors": all_errors,
        "verified_count": len(approvable),
        "failed_count": len(rejected),
    }


def adjudicate_sourced_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    evidence_index: Mapping[str, bytes],
) -> Dict[str, Any]:
    """Engine-side adjudication: promote `SOURCED_CANDIDATE` -> `VERIFIED` only when
    anchors verify and content is non-hollow."""
    anchor_report = verify_candidate_anchors(candidates, evidence_index=evidence_index)
    verified: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = list(anchor_report["rejected_candidates"])

    for cand in anchor_report["approvable_candidates"]:
        item = dict(cand)
        texts = [
            item.get("title"),
            item.get("summary"),
            item.get("organization_summary"),
            *(
                p.get("public_perspective_summary")
                for p in (item.get("participants") or ())
                if isinstance(p, Mapping)
            ),
        ]
        if any(is_hollow_story_template(t) for t in texts):
            item["review_status"] = "REJECTED_HOLLOW_TEMPLATE"
            rejected.append(item)
            continue
        item["review_status"] = "VERIFIED"
        verified.append(item)

    return {
        "verified_records": verified,
        "rejected_records": rejected,
        "verified_count": len(verified),
        "rejected_count": len(rejected),
    }


def extract_wiki_candidates(
    *,
    sources_dir: str | Path,
    person_registry: Mapping[str, Any] | str | Path,
    output_path: Optional[str | Path] = None,
    verify_anchors: bool = True,
    evidence_dir: Optional[str | Path] = None,
    candidate_records: Optional[Mapping[str, Any] | Sequence[Mapping[str, Any]]] = None,
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
    bio_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
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
                # Merge bio fields across all sentences for the same (person_uid, source_file)
                primary = mentioned[0]
                bkey = (primary["person_uid"], rel_path)
                existing_bio = bio_by_key.get(bkey)
                if existing_bio is None:
                    entry = {
                        "person_uid": primary["person_uid"],
                        "canonical_name": primary["canonical_name"],
                        "extracted_fields": dict(bio_fields),
                        "source_file": rel_path,
                        "source_title": title,
                        "evidence_snippet": sent[:200],
                        "review_status": DRAFT_REVIEW_STATUS,
                    }
                    bio_by_key[bkey] = entry
                    bio_candidates.append(entry)
                else:
                    for fk, fv in bio_fields.items():
                        existing_bio["extracted_fields"].setdefault(fk, fv)
                    snippet_chunk = sent[:200]
                    if snippet_chunk not in existing_bio["evidence_snippet"]:
                        combined = f"{existing_bio['evidence_snippet']} {snippet_chunk}".strip()
                        existing_bio["evidence_snippet"] = combined[:400]

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

    if verify_anchors and (candidate_records is not None or evidence_dir is not None):
        evidence_index = _build_evidence_index(evidence_dir) if evidence_dir is not None else {}
        flat_candidates: List[Mapping[str, Any]] = []
        if isinstance(candidate_records, Mapping):
            for key in (
                "candidates",
                "story_records",
                "story_and_timeline_candidates",
                "person_overseas_tenures",
                "youth_national_tenures",
            ):
                seq = candidate_records.get(key)
                if isinstance(seq, Sequence) and not isinstance(seq, (str, bytes)):
                    flat_candidates.extend(x for x in seq if isinstance(x, Mapping))
        elif isinstance(candidate_records, Sequence) and not isinstance(candidate_records, (str, bytes)):
            flat_candidates.extend(x for x in candidate_records if isinstance(x, Mapping))
        if flat_candidates:
            result["anchor_verification"] = verify_candidate_anchors(
                flat_candidates,
                evidence_index=evidence_index,
            )

    if output_path is not None:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        atomic(
            out,
            (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )

    return result
