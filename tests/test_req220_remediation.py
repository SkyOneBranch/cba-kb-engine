"""Regression & remediation tests for REQ-220 Q2 Round 1, Round 2 & Round 3 FAIL findings.

Covers:
- P0-1-F01: `upstream_origin` must be a non-empty string in acquisition_bundle_v1
  (None, "", "   ", and non-str values fail closed with SourceIntakeError).
- P0-2-F01: Crash-safe, idempotent, fail-closed revision-chain reconciliation when
  interrupted between `os.replace(staging_dir, root)` and prior-doc /
  `revision_index.json` updates.
- P0-2-F02: Prior archive `text.txt` UTF-8 & `content_hash` integrity verification
  before any revision-chain mutation, plus zero-write counter proof on consistent retry.
- P0-2-F02-R3-01: Read-only prior archive integrity preflight BEFORE any filesystem
  write across `_ingest_parsed()`, `_merge_exact()` interrupted-merge recovery, and
  `_merge_exact()` new-capture merge paths.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest
import yaml

from cba_kb import document_lane, kc_passages, source_intake
from cba_kb.document_lane import DocumentLane
from cba_kb.document_sources import ReviewRequired
from cba_kb.instance import Instance
from cba_kb.source_intake import (
    SourceIntakeError,
    parse_acquisition_bundle_markdown,
    validate_acquisition_bundle_v1,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _sha256_str(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _make_instance(tmp_path: Path) -> Instance:
    inst_root = tmp_path / "synthetic_instance"
    (inst_root / "config").mkdir(parents=True, exist_ok=True)
    (inst_root / "data").mkdir(parents=True, exist_ok=True)
    (inst_root / "inbox" / "documents").mkdir(parents=True, exist_ok=True)
    return Instance(
        root=inst_root.resolve(),
        config_root=(inst_root / "config").resolve(),
        credentials_store=(inst_root / ".credentials").resolve(),
        real_fixture_root=(inst_root / "fixtures" / "real").resolve(),
        data_root=(inst_root / "data").resolve(),
    )


def _make_bundle_meta(
    *,
    canonical_url: str = "https://example.invalid/cba/remediation-doc",
    body: str = "Synthetic CBA remediation test document body.",
    upstream_origin: Any = "official_club_release:remediation-001",
    source_id: str = "synthetic-remediation-src",
) -> Dict[str, Any]:
    return {
        "bundle_schema": "acquisition_bundle_v1",
        "item_id": _sha256_str(canonical_url),
        "canonical_url": canonical_url,
        "final_url": canonical_url,
        "related_urls": [],
        "upstream_origin": upstream_origin,
        "source_id": source_id,
        "source_tier": "T1",
        "event_class": "roster_update",
        "matched_persons": ["测试球员甲"],
        "published_at": "2026-10-15",
        "acquired_at": "2026-10-15T13:00:00Z",
        "raw_sha256": _sha256_str("raw::" + body),
        "raw_ref": None,
        "extracted_sha256": _sha256_str(body),
        "extractor": "synthetic-remediation-v1",
        "completeness": "full",
        "rights": {"classification": "copyrighted", "public_export_allowed": False},
        "title": "Synthetic Remediation Doc",
    }


def _render_bundle_markdown(meta: Dict[str, Any], body: str) -> str:
    fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{fm}\n---\n\n{body}\n"


def _write_bundle(path: Path, meta: Dict[str, Any], body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_bundle_markdown(meta, body), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Replay A (P0-1-F01): upstream_origin fail-closed validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_origin",
    [None, "", "   ", "\t\n", 123, ["wire-origin"], {"origin": "x"}, True],
)
def test_p0_1_f01_validate_rejects_empty_null_or_non_str_upstream_origin(bad_origin: Any) -> None:
    meta = _make_bundle_meta(upstream_origin=bad_origin)
    with pytest.raises(SourceIntakeError, match="UPSTREAM_ORIGIN"):
        validate_acquisition_bundle_v1(meta)


@pytest.mark.parametrize(
    "bad_origin",
    [None, "", "   ", 42, ["origin-a"]],
)
def test_p0_1_f01_parse_markdown_rejects_empty_null_or_non_str_upstream_origin(
    bad_origin: Any,
) -> None:
    body = "Synthetic CBA remediation markdown body."
    raw = _render_bundle_markdown(_make_bundle_meta(upstream_origin=bad_origin, body=body), body)
    with pytest.raises(SourceIntakeError, match="UPSTREAM_ORIGIN"):
        parse_acquisition_bundle_markdown(raw)


def test_p0_1_f01_valid_upstream_origin_succeeds_and_propagates(tmp_path: Path) -> None:
    body = "Synthetic CBA valid upstream origin body."
    origin = "official_club_release:2026-10-15-valid"
    meta = _make_bundle_meta(body=body, upstream_origin=f"  {origin}  ")

    validated = validate_acquisition_bundle_v1(meta)
    assert validated["upstream_origin"] == origin

    parsed_meta, parsed_body = parse_acquisition_bundle_markdown(
        _render_bundle_markdown(meta, body)
    )
    assert parsed_meta["upstream_origin"] == origin
    assert parsed_body == body

    inst = _make_instance(tmp_path)
    lane = DocumentLane(REPO_ROOT, inst)
    bpath = _write_bundle(inst.document_input_root / "valid_origin.md", meta, body)
    res = lane.ingest_acquisition_bundle(bpath)
    assert res["status"] == "ACCEPTED"
    assert res["accepted"] is True

    doc_root = Path(res["archive_path"])
    rec = json.loads((doc_root / "record.json").read_text(encoding="utf-8"))
    prov = json.loads((doc_root / "provenance.json").read_text(encoding="utf-8"))
    assert rec["upstream_origin"] == origin
    assert rec["canonical_write_allowed"] is False
    assert prov["upstream_origin"] == origin
    assert prov["acquisition"]["upstream_origin"] == origin

    passage = kc_passages.build_routed_passage(
        source_id=rec["doc_id"],
        revision="r1",
        locator="body:0",
        text=body,
        upstream_origin=rec["upstream_origin"],
    )
    assert passage["upstream_origin"] == origin


def test_p0_1_f01_document_lane_rejects_null_upstream_origin(tmp_path: Path) -> None:
    body = "Synthetic CBA null upstream origin body."
    meta = _make_bundle_meta(body=body, upstream_origin=None)
    inst = _make_instance(tmp_path)
    lane = DocumentLane(REPO_ROOT, inst)
    bpath = _write_bundle(inst.document_input_root / "null_origin.md", meta, body)

    with pytest.raises(SourceIntakeError, match="UPSTREAM_ORIGIN"):
        lane.ingest_acquisition_bundle(bpath)

    file_res = lane.ingest_file(bpath, capture_channel="acquisition_bundle_v1")
    assert file_res["status"] == "REVIEW_REQUIRED"
    assert file_res["accepted"] is False
    assert "UPSTREAM_ORIGIN" in file_res["reason"]


# ---------------------------------------------------------------------------
# Replay B (P0-2-F01): crash-safe idempotent revision-chain reconciliation
# ---------------------------------------------------------------------------


def _fault_inject_revision_b(
    tmp_path: Path,
    *,
    url: str = "https://example.invalid/cba/revision-crash-recovery",
    body_a: str = "Revision 1 synthetic article body about player training.",
    body_b: str = "Revision 2 synthetic article body with updated injury status.",
):
    inst = _make_instance(tmp_path)
    lane = DocumentLane(REPO_ROOT, inst)

    meta_a = _make_bundle_meta(canonical_url=url, body=body_a, upstream_origin="origin:rev-chain")
    bpath_a = _write_bundle(inst.document_input_root / "doc_a.md", meta_a, body_a)
    res_a = lane.ingest_acquisition_bundle(bpath_a)
    assert res_a["status"] == "ACCEPTED"
    doc_a_id = res_a["doc_id"]
    prior_root = lane.archive_root / doc_a_id
    prior_text_before = (prior_root / "text.txt").read_bytes()
    prior_hash_before = json.loads((prior_root / "record.json").read_text(encoding="utf-8"))[
        "content_hash"
    ]

    meta_b = _make_bundle_meta(canonical_url=url, body=body_b, upstream_origin="origin:rev-chain")
    bpath_b = _write_bundle(inst.document_input_root / "doc_b.md", meta_b, body_b)

    orig_replace = os.replace
    crashed = {"done": False}

    def _crash_after_replace(src, dst):
        orig_replace(src, dst)
        dst_path = Path(dst)
        if not crashed["done"] and dst_path.parent == lane.archive_root and dst_path.name.startswith("doc_"):
            crashed["done"] = True
            raise RuntimeError("SIMULATED_CRASH_AFTER_OS_REPLACE")

    with pytest.raises(RuntimeError, match="SIMULATED_CRASH_AFTER_OS_REPLACE"):
        with patch("cba_kb.document_lane.os.replace", side_effect=_crash_after_replace):
            lane.ingest_acquisition_bundle(bpath_b)

    assert crashed["done"] is True
    # Confirm post-crash broken state before recovery
    prior_rec_crashed = json.loads((prior_root / "record.json").read_text(encoding="utf-8"))
    assert prior_rec_crashed.get("superseded_by_doc_id") is None
    assert not (lane.archive_root / "revision_index.json").exists()

    return inst, bpath_a, bpath_b, doc_a_id, prior_text_before, prior_hash_before, meta_b, body_b


def test_p0_2_f01_crash_after_os_replace_recovery_and_double_retry_idempotency(
    tmp_path: Path,
) -> None:
    (
        inst,
        _bpath_a,
        bpath_b,
        doc_a_id,
        prior_text_before,
        prior_hash_before,
        _meta_b,
        _body_b,
    ) = _fault_inject_revision_b(tmp_path)

    # Retry 1 (fresh DocumentLane instance, same capture_id -> _merge_exact early-return branch)
    lane_retry_1 = DocumentLane(REPO_ROOT, inst)
    res_1 = lane_retry_1.ingest_acquisition_bundle(bpath_b)
    assert res_1["status"] == "ACCEPTED"
    assert res_1["accepted"] is True
    assert res_1["disposition"] == "exact_merge"
    assert res_1["changed"] is True
    doc_b_id = res_1["doc_id"]
    assert doc_b_id != doc_a_id
    assert res_1["revision_summary"]["prior_doc_id"] == doc_a_id
    assert res_1["revision_summary"]["new_doc_id"] == doc_b_id
    assert res_1["revision_summary"]["disclosure_posture"] == "LOUD_REVISION"

    prior_root = lane_retry_1.archive_root / doc_a_id
    new_root = lane_retry_1.archive_root / doc_b_id
    rev_idx_path = lane_retry_1.archive_root / "revision_index.json"

    prior_rec = json.loads((prior_root / "record.json").read_text(encoding="utf-8"))
    prior_dedup = json.loads((prior_root / "dedup.json").read_text(encoding="utf-8"))
    new_rec = json.loads((new_root / "record.json").read_text(encoding="utf-8"))
    rev_idx = json.loads(rev_idx_path.read_text(encoding="utf-8"))

    assert prior_rec["superseded_by_doc_id"] == doc_b_id
    assert prior_rec["revision_seq"] == 1
    assert prior_rec["content_hash"] == prior_hash_before
    assert prior_rec["canonical_write_allowed"] is False
    assert (prior_root / "text.txt").read_bytes() == prior_text_before
    assert prior_dedup["superseded_by_doc_id"] == doc_b_id
    assert prior_dedup["revision_seq"] == 1
    assert new_rec["supersedes_doc_id"] == doc_a_id
    assert new_rec["canonical_write_allowed"] is False

    url_chain = rev_idx["by_canonical_url"]["https://example.invalid/cba/revision-crash-recovery"]
    assert [entry["doc_id"] for entry in url_chain] == [doc_a_id, doc_b_id]
    assert url_chain[0]["superseded_by_doc_id"] == doc_b_id
    assert url_chain[1]["supersedes_doc_id"] == doc_a_id

    # Snapshot bytes after Retry 1
    snap_prior_rec = (prior_root / "record.json").read_bytes()
    snap_prior_dedup = (prior_root / "dedup.json").read_bytes()
    snap_new_rec = (new_root / "record.json").read_bytes()
    snap_rev_idx = rev_idx_path.read_bytes()

    # Retry 2 (double-recovery idempotency: zero writes, byte-identical artifacts)
    lane_retry_2 = DocumentLane(REPO_ROOT, inst)
    res_2 = lane_retry_2.ingest_acquisition_bundle(bpath_b)
    assert res_2["status"] == "ACCEPTED"
    assert res_2["accepted"] is True
    assert res_2["disposition"] == "exact_merge"
    assert res_2["changed"] is False

    assert (prior_root / "record.json").read_bytes() == snap_prior_rec
    assert (prior_root / "dedup.json").read_bytes() == snap_prior_dedup
    assert (new_root / "record.json").read_bytes() == snap_new_rec
    assert rev_idx_path.read_bytes() == snap_rev_idx


def test_p0_2_f01_crash_recovery_via_new_capture_merge_branch(tmp_path: Path) -> None:
    """Recovery also reconciles the revision chain when retried with a new capture_id."""
    (
        inst,
        _bpath_a,
        _bpath_b,
        doc_a_id,
        prior_text_before,
        prior_hash_before,
        meta_b,
        body_b,
    ) = _fault_inject_revision_b(tmp_path)

    # Write identical content to a different locator so capture_id differs (new-capture merge branch)
    bpath_b_alt = _write_bundle(inst.document_input_root / "doc_b_alt_locator.md", meta_b, body_b)
    lane_retry = DocumentLane(REPO_ROOT, inst)
    res = lane_retry.ingest_acquisition_bundle(bpath_b_alt)

    assert res["status"] == "ACCEPTED"
    assert res["accepted"] is True
    assert res["disposition"] == "exact_merge"
    assert res["changed"] is True
    doc_b_id = res["doc_id"]
    assert res["revision_summary"]["prior_doc_id"] == doc_a_id
    assert res["revision_summary"]["new_doc_id"] == doc_b_id

    prior_root = lane_retry.archive_root / doc_a_id
    prior_rec = json.loads((prior_root / "record.json").read_text(encoding="utf-8"))
    assert prior_rec["superseded_by_doc_id"] == doc_b_id
    assert prior_rec["content_hash"] == prior_hash_before
    assert (prior_root / "text.txt").read_bytes() == prior_text_before
    assert (lane_retry.archive_root / "revision_index.json").is_file()


@pytest.mark.parametrize("corruption_mode", ["missing_record", "corrupt_record", "missing_text", "corrupt_dedup"])
def test_p0_2_f01_recovery_fails_closed_when_prior_archive_missing_or_corrupt(
    tmp_path: Path,
    corruption_mode: str,
) -> None:
    (
        inst,
        _bpath_a,
        bpath_b,
        doc_a_id,
        _prior_text_before,
        _prior_hash_before,
        _meta_b,
        _body_b,
    ) = _fault_inject_revision_b(tmp_path)

    prior_root = inst.document_archive_root / doc_a_id
    if corruption_mode == "missing_record":
        (prior_root / "record.json").unlink()
    elif corruption_mode == "corrupt_record":
        (prior_root / "record.json").write_text("{not valid json", encoding="utf-8")
    elif corruption_mode == "missing_text":
        (prior_root / "text.txt").unlink()
    elif corruption_mode == "corrupt_dedup":
        (prior_root / "dedup.json").write_text("{not valid json", encoding="utf-8")

    lane_retry = DocumentLane(REPO_ROOT, inst)
    res = lane_retry.ingest_acquisition_bundle(bpath_b)
    assert res["status"] == "REVIEW_REQUIRED"
    assert res["accepted"] is False
    assert res["reason"] == "DOCUMENT_ARCHIVE_REVISION_CHAIN_INVALID"


def test_p0_2_f01_uninterrupted_revision_control_preserves_old_text_and_hash(
    tmp_path: Path,
) -> None:
    inst = _make_instance(tmp_path)
    lane = DocumentLane(REPO_ROOT, inst)
    url = "https://example.invalid/cba/uninterrupted-revision"
    body_1 = "Uninterrupted revision 1 synthetic body."
    body_2 = "Uninterrupted revision 2 synthetic body with changes."

    b1 = _write_bundle(
        inst.document_input_root / "r1.md",
        _make_bundle_meta(canonical_url=url, body=body_1, upstream_origin="origin:unint"),
        body_1,
    )
    b2 = _write_bundle(
        inst.document_input_root / "r2.md",
        _make_bundle_meta(canonical_url=url, body=body_2, upstream_origin="origin:unint"),
        body_2,
    )

    res1 = lane.ingest_acquisition_bundle(b1)
    assert res1["status"] == "ACCEPTED"
    doc1_root = Path(res1["archive_path"])
    doc1_text_before = (doc1_root / "text.txt").read_bytes()
    doc1_hash_before = res1["content_hash"]

    res2 = lane.ingest_acquisition_bundle(b2)
    assert res2["status"] == "ACCEPTED"
    assert res2["disposition"] == "revision"
    assert res2["revision_summary"]["prior_doc_id"] == res1["doc_id"]
    assert res2["revision_summary"]["new_doc_id"] == res2["doc_id"]
    assert res2["revision_summary"]["disclosure_posture"] == "LOUD_REVISION"

    # Old archive text.txt and content_hash are never overwritten
    assert (doc1_root / "text.txt").read_bytes() == doc1_text_before
    doc1_rec = json.loads((doc1_root / "record.json").read_text(encoding="utf-8"))
    assert doc1_rec["content_hash"] == doc1_hash_before
    assert doc1_rec["superseded_by_doc_id"] == res2["doc_id"]
    assert doc1_rec["canonical_write_allowed"] is False

    rev_idx = json.loads((lane.archive_root / "revision_index.json").read_text(encoding="utf-8"))
    assert [e["doc_id"] for e in rev_idx["by_canonical_url"][url]] == [
        res1["doc_id"],
        res2["doc_id"],
    ]


# ---------------------------------------------------------------------------
# Replay C (P0-2-F02): prior text.txt integrity & zero-write retry proof
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tamper_mode",
    [
        "tampered_text_utf8",
        "corrupt_text_non_utf8",
        "missing_content_hash",
        "empty_content_hash",
        "non_str_content_hash",
    ],
)
def test_p0_2_f02_recovery_fails_closed_on_tampered_prior_text_or_missing_hash(
    tmp_path: Path,
    tamper_mode: str,
) -> None:
    (
        inst,
        _bpath_a,
        bpath_b,
        doc_a_id,
        _prior_text_before,
        _prior_hash_before,
        _meta_b,
        _body_b,
    ) = _fault_inject_revision_b(tmp_path)

    prior_root = inst.document_archive_root / doc_a_id
    prior_record_path = prior_root / "record.json"
    prior_dedup_path = prior_root / "dedup.json"
    prior_text_path = prior_root / "text.txt"
    rev_idx_path = inst.document_archive_root / "revision_index.json"

    if tamper_mode == "tampered_text_utf8":
        prior_text_path.write_text("Tampered prior archive text.\n", encoding="utf-8")
    elif tamper_mode == "corrupt_text_non_utf8":
        prior_text_path.write_bytes(b"\xff\xfe\x00corrupt-non-utf8-bytes")
    elif tamper_mode == "missing_content_hash":
        rec = json.loads(prior_record_path.read_text(encoding="utf-8"))
        rec.pop("content_hash", None)
        prior_record_path.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    elif tamper_mode == "empty_content_hash":
        rec = json.loads(prior_record_path.read_text(encoding="utf-8"))
        rec["content_hash"] = ""
        prior_record_path.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    elif tamper_mode == "non_str_content_hash":
        rec = json.loads(prior_record_path.read_text(encoding="utf-8"))
        rec["content_hash"] = 12345
        prior_record_path.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")

    new_doc_dirs = [
        p for p in inst.document_archive_root.iterdir()
        if p.is_dir() and p.name.startswith("doc_") and p.name != doc_a_id
    ]
    assert len(new_doc_dirs) == 1
    new_root = new_doc_dirs[0]

    snap_prior_text = prior_text_path.read_bytes()
    snap_prior_rec = prior_record_path.read_bytes()
    snap_prior_dedup = prior_dedup_path.read_bytes()
    snap_new_rec = (new_root / "record.json").read_bytes()
    snap_new_prov = (new_root / "provenance.json").read_bytes()
    snap_new_dedup = (new_root / "dedup.json").read_bytes()
    snap_new_text = (new_root / "text.txt").read_bytes()
    assert not rev_idx_path.exists()

    lane_retry = DocumentLane(REPO_ROOT, inst)
    res = lane_retry.ingest_acquisition_bundle(bpath_b)
    assert res["status"] == "REVIEW_REQUIRED"
    assert res["accepted"] is False
    assert res["reason"] == "DOCUMENT_ARCHIVE_REVISION_CHAIN_INVALID"

    # Zero mutations to prior archive, new archive, or revision_index.json
    assert prior_text_path.read_bytes() == snap_prior_text
    assert prior_record_path.read_bytes() == snap_prior_rec
    assert prior_dedup_path.read_bytes() == snap_prior_dedup
    assert (new_root / "record.json").read_bytes() == snap_new_rec
    assert (new_root / "provenance.json").read_bytes() == snap_new_prov
    assert (new_root / "dedup.json").read_bytes() == snap_new_dedup
    assert (new_root / "text.txt").read_bytes() == snap_new_text
    assert not rev_idx_path.exists()


def test_p0_2_f02_r3_01_normal_revision_preflight_writes_nothing_on_corrupt_prior_text(
    tmp_path: Path,
) -> None:
    """_ingest_parsed() must fail closed BEFORE creating staging/new archive dir."""
    inst = _make_instance(tmp_path)
    lane = DocumentLane(REPO_ROOT, inst)
    url = "https://example.invalid/cba/r3-normal-revision-preflight"
    body_a = "R3 normal revision 1 synthetic body."
    body_b = "R3 normal revision 2 synthetic body with updates."

    bpath_a = _write_bundle(
        inst.document_input_root / "r3_a.md",
        _make_bundle_meta(canonical_url=url, body=body_a, upstream_origin="origin:r3"),
        body_a,
    )
    res_a = lane.ingest_acquisition_bundle(bpath_a)
    assert res_a["status"] == "ACCEPTED"
    doc_a_id = res_a["doc_id"]
    prior_root = lane.archive_root / doc_a_id

    # Tamper prior text.txt before ingesting revision B
    (prior_root / "text.txt").write_text("Tampered prior archive text.\n", encoding="utf-8")

    snap_prior_text = (prior_root / "text.txt").read_bytes()
    snap_prior_rec = (prior_root / "record.json").read_bytes()
    snap_prior_dedup = (prior_root / "dedup.json").read_bytes()
    rev_idx_path = lane.archive_root / "revision_index.json"
    assert not rev_idx_path.exists()

    bpath_b = _write_bundle(
        inst.document_input_root / "r3_b.md",
        _make_bundle_meta(canonical_url=url, body=body_b, upstream_origin="origin:r3"),
        body_b,
    )
    lane_b = DocumentLane(REPO_ROOT, inst)
    res_b = lane_b.ingest_acquisition_bundle(bpath_b)
    assert res_b["status"] == "REVIEW_REQUIRED"
    assert res_b["accepted"] is False
    assert res_b["reason"] == "DOCUMENT_ARCHIVE_REVISION_CHAIN_INVALID"

    # New archive dir must NOT exist and .staging must have no leftover dirs
    doc_dirs = sorted(
        p.name for p in lane.archive_root.iterdir()
        if p.is_dir() and p.name.startswith("doc_")
    )
    assert doc_dirs == [doc_a_id]
    staging_parent = lane.archive_root / ".staging"
    assert not staging_parent.exists() or list(staging_parent.iterdir()) == []

    # Prior archive files and revision_index.json remain byte-identical / untouched
    assert (prior_root / "text.txt").read_bytes() == snap_prior_text
    assert (prior_root / "record.json").read_bytes() == snap_prior_rec
    assert (prior_root / "dedup.json").read_bytes() == snap_prior_dedup
    assert not rev_idx_path.exists()


@pytest.mark.parametrize("new_dedup_mode", ["missing", "corrupt"])
def test_p0_2_f02_r3_01_merge_exact_interrupted_recovery_writes_nothing_on_corrupt_prior_text(
    tmp_path: Path,
    new_dedup_mode: str,
) -> None:
    """_merge_exact() interrupted-merge recovery must fail closed BEFORE any writes."""
    (
        inst,
        _bpath_a,
        bpath_b,
        doc_a_id,
        _prior_text_before,
        _prior_hash_before,
        _meta_b,
        _body_b,
    ) = _fault_inject_revision_b(tmp_path)

    prior_root = inst.document_archive_root / doc_a_id
    new_doc_dirs = [
        p for p in inst.document_archive_root.iterdir()
        if p.is_dir() and p.name.startswith("doc_") and p.name != doc_a_id
    ]
    assert len(new_doc_dirs) == 1
    new_root = new_doc_dirs[0]

    # Force _merge_exact() interrupted-merge recovery branch (same capture_id, broken dedup.json)
    new_dedup_path = new_root / "dedup.json"
    if new_dedup_mode == "missing":
        new_dedup_path.unlink()
    else:
        new_dedup_path.write_text("{corrupt dedup json", encoding="utf-8")

    # Tamper prior archive text.txt
    (prior_root / "text.txt").write_text("Tampered prior text in interrupted merge.\n", encoding="utf-8")

    snap_prior_text = (prior_root / "text.txt").read_bytes()
    snap_prior_rec = (prior_root / "record.json").read_bytes()
    snap_prior_dedup = (prior_root / "dedup.json").read_bytes()
    snap_new_rec = (new_root / "record.json").read_bytes()
    snap_new_prov = (new_root / "provenance.json").read_bytes()
    snap_new_dedup = new_dedup_path.read_bytes() if new_dedup_path.exists() else None
    rev_idx_path = inst.document_archive_root / "revision_index.json"
    assert not rev_idx_path.exists()

    lane_retry = DocumentLane(REPO_ROOT, inst)
    res = lane_retry.ingest_acquisition_bundle(bpath_b)
    assert res["status"] == "REVIEW_REQUIRED"
    assert res["accepted"] is False
    assert res["reason"] == "DOCUMENT_ARCHIVE_REVISION_CHAIN_INVALID"

    # ALL pre-existing archive artifacts must remain byte-identical / untouched
    assert (prior_root / "text.txt").read_bytes() == snap_prior_text
    assert (prior_root / "record.json").read_bytes() == snap_prior_rec
    assert (prior_root / "dedup.json").read_bytes() == snap_prior_dedup
    assert (new_root / "record.json").read_bytes() == snap_new_rec
    assert (new_root / "provenance.json").read_bytes() == snap_new_prov
    if snap_new_dedup is None:
        assert not new_dedup_path.exists()
    else:
        assert new_dedup_path.read_bytes() == snap_new_dedup
    assert not rev_idx_path.exists()


def test_p0_2_f02_r3_01_merge_exact_new_capture_writes_nothing_on_corrupt_prior_text(
    tmp_path: Path,
) -> None:
    """_merge_exact() new-capture merge branch must fail closed BEFORE any writes."""
    (
        inst,
        _bpath_a,
        _bpath_b,
        doc_a_id,
        _prior_text_before,
        _prior_hash_before,
        meta_b,
        body_b,
    ) = _fault_inject_revision_b(tmp_path)

    prior_root = inst.document_archive_root / doc_a_id
    new_doc_dirs = [
        p for p in inst.document_archive_root.iterdir()
        if p.is_dir() and p.name.startswith("doc_") and p.name != doc_a_id
    ]
    assert len(new_doc_dirs) == 1
    new_root = new_doc_dirs[0]

    # Tamper prior archive text.txt
    (prior_root / "text.txt").write_text("Tampered prior text in new capture merge.\n", encoding="utf-8")

    # Snapshot all archive artifacts including raw/ capture files
    raw_dir = new_root / "raw"
    snap_raw_files = sorted(p.name for p in raw_dir.iterdir())
    assert len(snap_raw_files) == 1
    snap_prior_text = (prior_root / "text.txt").read_bytes()
    snap_prior_rec = (prior_root / "record.json").read_bytes()
    snap_prior_dedup = (prior_root / "dedup.json").read_bytes()
    snap_new_rec = (new_root / "record.json").read_bytes()
    snap_new_prov = (new_root / "provenance.json").read_bytes()
    snap_new_dedup = (new_root / "dedup.json").read_bytes()
    rev_idx_path = inst.document_archive_root / "revision_index.json"
    assert not rev_idx_path.exists()

    # Alternate locator -> new capture_id on existing revision archive -> new-capture merge branch
    bpath_b_alt = _write_bundle(inst.document_input_root / "doc_b_alt_r3.md", meta_b, body_b)
    lane_retry = DocumentLane(REPO_ROOT, inst)
    res = lane_retry.ingest_acquisition_bundle(bpath_b_alt)
    assert res["status"] == "REVIEW_REQUIRED"
    assert res["accepted"] is False
    assert res["reason"] == "DOCUMENT_ARCHIVE_REVISION_CHAIN_INVALID"

    # Zero writes to raw/, new archive metadata, prior archive, or revision_index.json
    assert sorted(p.name for p in raw_dir.iterdir()) == snap_raw_files
    assert (new_root / "record.json").read_bytes() == snap_new_rec
    assert (new_root / "provenance.json").read_bytes() == snap_new_prov
    assert (new_root / "dedup.json").read_bytes() == snap_new_dedup
    assert (prior_root / "text.txt").read_bytes() == snap_prior_text
    assert (prior_root / "record.json").read_bytes() == snap_prior_rec
    assert (prior_root / "dedup.json").read_bytes() == snap_prior_dedup
    assert not rev_idx_path.exists()


def test_p0_2_f02_consistent_second_retry_makes_zero_atomic_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        inst,
        _bpath_a,
        bpath_b,
        _doc_a_id,
        _prior_text_before,
        _prior_hash_before,
        _meta_b,
        _body_b,
    ) = _fault_inject_revision_b(tmp_path)

    # Retry 1 reconciles the revision chain
    lane_retry_1 = DocumentLane(REPO_ROOT, inst)
    res_1 = lane_retry_1.ingest_acquisition_bundle(bpath_b)
    assert res_1["status"] == "ACCEPTED"
    assert res_1["changed"] is True

    # Retry 2 must perform 0 calls to document_lane.atomic()
    orig_atomic = document_lane.atomic
    atomic_calls: list[Path] = []

    def _counting_atomic(path, data):
        atomic_calls.append(Path(path))
        return orig_atomic(path, data)

    monkeypatch.setattr(document_lane, "atomic", _counting_atomic)

    lane_retry_2 = DocumentLane(REPO_ROOT, inst)
    res_2 = lane_retry_2.ingest_acquisition_bundle(bpath_b)
    assert res_2["status"] == "ACCEPTED"
    assert res_2["accepted"] is True
    assert res_2["disposition"] == "exact_merge"
    assert res_2["changed"] is False
    assert atomic_calls == []

