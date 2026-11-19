"""REQ-220 (v2.2.0 Operational KB) frozen acceptance test suite (Issue #254).

All fixtures are 100% synthetic. Zero network calls, zero Drive API calls,
zero references to private instances or local user paths.

Covers P0-1 through P0-5 per docs/specs/req-220-operational-kb.md.
"""
from __future__ import annotations

import concurrent.futures
import copy
import datetime
import gzip
import hashlib
import importlib
import importlib.util
import inspect
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest

from cba_kb import (
    cli,
    consumer_surface,
    document_lane,
    document_mentions,
    drive_discovery,
    drive_plan,
    drive_poll,
    drive_resilience,
    drive_search,
    kc_cleanroom,
    kc_evidence_status,
    kc_local_producer,
    kc_passages,
    kc_render,
    source_intake,
    source_watcher,
)
from cba_kb.instance import Instance


REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_BUNDLE_KEYS = frozenset({
    "bundle_schema",
    "item_id",
    "canonical_url",
    "final_url",
    "related_urls",
    "upstream_origin",
    "source_id",
    "source_tier",
    "event_class",
    "matched_persons",
    "published_at",
    "acquired_at",
    "raw_sha256",
    "raw_ref",
    "extracted_sha256",
    "extractor",
    "completeness",
    "rights",
})

SYNTHETIC_TEAMS = (
    "北京控股", "北京首钢", "福建浔兴", "广东宏远", "广州龙狮",
    "江苏肯帝亚", "吉林九台农商行", "辽宁沈阳三生", "南京同曦",
    "宁波富邦", "青岛国信海天", "山东高速", "上海久事", "山西汾酒",
    "深圳新世纪", "四川锦城", "天津荣钢", "新疆广汇", "浙江稠州",
    "浙江广厦",
)


def _require_attr(obj: Any, name: str) -> Any:
    assert hasattr(obj, name), f"Missing required REQ-220 symbol: {obj}.{name}"
    return getattr(obj, name)


def _require_module(module_name: str) -> Any:
    spec = importlib.util.find_spec(module_name)
    assert spec is not None, f"Missing required REQ-220 module: {module_name}"
    return importlib.import_module(module_name)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_instance(root: Path) -> Instance:
    inst_root = root / "synthetic_instance"
    config_root = inst_root / "config"
    config_root.mkdir(parents=True, exist_ok=True)
    data_root = inst_root / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    (inst_root / "inbox" / "documents").mkdir(parents=True, exist_ok=True)
    return Instance(
        root=inst_root.resolve(),
        config_root=config_root.resolve(),
        credentials_store=(inst_root / ".credentials").resolve(),
        real_fixture_root=(inst_root / "fixtures" / "real").resolve(),
        data_root=data_root.resolve(),
    )


def _make_bundle_meta(
    *,
    canonical_url: str = "https://example.org/cba/news/2026-11-18-001",
    final_url: Optional[str] = None,
    related_urls: Optional[List[str]] = None,
    upstream_origin: str = "official_club_release:2026-11-18-alpha",
    source_id: str = "cba-official-feed",
    source_tier: str = "T1",
    event_class: str = "roster_update",
    matched_persons: Optional[List[str]] = None,
    published_at: Any = "2026-11-18",
    acquired_at: Any = "2026-11-18T12:00:00Z",
    body: str = "Synthetic CBA player roster report for Player Alpha in season 2025-2026.",
    raw_ref: Optional[str] = None,
    extractor: str = "synthetic-extractor-v1",
    completeness: str = "full",
    title: str = "Synthetic Roster Report",
) -> Dict[str, Any]:
    return {
        "bundle_schema": "acquisition_bundle_v1",
        "item_id": _sha256_text(canonical_url),
        "canonical_url": canonical_url,
        "final_url": final_url or canonical_url,
        "related_urls": list(related_urls) if related_urls is not None else [],
        "upstream_origin": upstream_origin,
        "source_id": source_id,
        "source_tier": source_tier,
        "event_class": event_class,
        "matched_persons": list(matched_persons) if matched_persons is not None else [],
        "published_at": published_at,
        "acquired_at": acquired_at,
        "raw_sha256": _sha256_text("raw::" + body),
        "raw_ref": raw_ref,
        "extracted_sha256": _sha256_text(body),
        "extractor": extractor,
        "completeness": completeness,
        "rights": {
            "classification": "copyrighted",
            "public_export_allowed": False,
        },
        "title": title,
    }


def _render_bundle_md(meta: Dict[str, Any], body: str) -> str:
    import yaml

    fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{fm}\n---\n\n{body}\n"


def _make_registration_snapshot_payload(*, marker: str, observed_at: str) -> Dict[str, Any]:
    links = []
    children = {}
    for idx, team in enumerate(SYNTHETIC_TEAMS):
        article_id = f"{idx + 1:010x}"
        links.append(
            f'<p><a href="https://www.cbaleague.com/#/news-register/detail/{article_id}">{team}</a></p>'
        )
        title = f"2025-2026赛季CBA联赛国内球员注册信息（{team}，9月1日）"
        html = (
            f"<table>"
            f'<tr><td colspan="9">{title}</td></tr>'
            f'<tr><td colspan="9">更新时间：2026年9月1日17:00</td></tr>'
            f"<tr><th>序 号</th><th>运动员</th><th>注册类型</th><th>合同类别</th>"
            f"<th>合同剩余年限</th><th>原CBA俱乐部</th><th>公示截止时间</th><th>备 注</th></tr>"
            f"<tr><td>1</td><td>合成球员{marker}_{idx}</td><td>延续</td><td>续约</td><td>C类</td>"
            f"<td>2027年7月31日</td><td>/</td><td>2026年9月1日17:00</td><td></td></tr>"
            f"</table>"
        )
        children[article_id] = {
            "code": 200,
            "message": "ok",
            "data": {"title": title, "detail_content": html},
        }
    return {
        "source_id": "cba-registration:2025-2026",
        "observed_at": observed_at,
        "index": {
            "code": 200,
            "message": "ok",
            "data": {"detail_content": "".join(links)},
        },
        "children": children,
    }


def _make_player_fixture(player_uid: str, player_name: str, ppg: float) -> Dict[str, Any]:
    fx = copy.deepcopy(kc_cleanroom.build_cleanroom_fixture())
    old_uid = fx["players"][0]["player_uid"]
    fx["players"][0]["player_uid"] = player_uid
    fx["players"][0]["canonical_name"] = player_name
    fx["players"] = [fx["players"][0]]
    for alias in fx.get("aliases", []):
        if alias.get("player_uid") == old_uid:
            alias["player_uid"] = player_uid
    for rec in fx["stats_records"]:
        rec["player_uid"] = player_uid
        rec["player_name"] = player_name
        rec["team_id"] = "29131"
        rec["team_name"] = None
        if rec.get("metric") == "points_per_game":
            rec["value"] = ppg
    fx["fact"]["player_uid"] = player_uid
    for p in fx["passages"]:
        p["player_uid"] = player_uid
    return fx


# ===========================================================================
# P0-1: Native acquisition_bundle_v1, Provenance Propagation, Atomicity, Cache
# ===========================================================================


def test_p0_1_acquisition_bundle_v1_validator_valid_and_rejections():
    schema_name = _require_attr(source_intake, "ACQUISITION_BUNDLE_V1_SCHEMA")
    req_keys = _require_attr(source_intake, "ACQUISITION_BUNDLE_V1_REQUIRED_KEYS")
    validate_fn = _require_attr(source_intake, "validate_acquisition_bundle_v1")
    parse_md = _require_attr(source_intake, "parse_acquisition_bundle_markdown")

    assert schema_name == "acquisition_bundle_v1"
    assert frozenset(req_keys) == EXPECTED_BUNDLE_KEYS
    assert len(req_keys) == 18

    # Valid with PyYAML-parsed date, datetime, and None raw_ref / published_at
    valid_meta = _make_bundle_meta(
        published_at=datetime.date(2026, 11, 18),
        acquired_at=datetime.datetime(2026, 11, 18, 12, 30, tzinfo=datetime.timezone.utc),
        raw_ref=None,
    )
    out = validate_fn(valid_meta)
    assert out["bundle_schema"] == "acquisition_bundle_v1"
    assert out["completeness"] == "full"
    assert out["rights"]["classification"] == "copyrighted"
    assert out["rights"]["public_export_allowed"] is False

    # published_at=None is allowed
    meta_no_pub = _make_bundle_meta(published_at=None)
    assert validate_fn(meta_no_pub)["published_at"] is None

    # Markdown roundtrip
    body = "Synthetic body text for acquisition bundle v1."
    md_text = _render_bundle_md(_make_bundle_meta(body=body), body)
    parsed_meta, parsed_body = parse_md(md_text)
    assert parsed_meta["bundle_schema"] == "acquisition_bundle_v1"
    assert body in parsed_body

    # Reject missing required key
    for missing_key in ("upstream_origin", "raw_sha256", "rights"):
        bad = _make_bundle_meta()
        bad.pop(missing_key)
        with pytest.raises(source_intake.SourceIntakeError):
            validate_fn(bad)

    # Reject mismatched item_id != sha256(canonical_url)
    bad_id = _make_bundle_meta()
    bad_id["item_id"] = "0" * 64
    with pytest.raises(source_intake.SourceIntakeError):
        validate_fn(bad_id)

    # Reject uppercase completeness
    bad_comp = _make_bundle_meta(completeness="FULL")
    with pytest.raises(source_intake.SourceIntakeError):
        validate_fn(bad_comp)

    # Reject invalid source_tier
    bad_tier = _make_bundle_meta(source_tier="T99")
    with pytest.raises(source_intake.SourceIntakeError):
        validate_fn(bad_tier)

    # Reject public_export_allowed=True
    bad_rights = _make_bundle_meta()
    bad_rights["rights"] = {"classification": "copyrighted", "public_export_allowed": True}
    with pytest.raises(source_intake.SourceIntakeError):
        validate_fn(bad_rights)


def test_p0_1_lossless_provenance_and_syndication_dedup(tmp_path):
    ingest_bundle = _require_attr(document_lane.DocumentLane, "ingest_acquisition_bundle")
    assert callable(ingest_bundle)
    sig = inspect.signature(kc_passages.build_routed_passage)
    for param in ("upstream_origin", "related_urls", "source_tier", "raw_sha256", "extracted_sha256"):
        assert param in sig.parameters, f"build_routed_passage missing parameter: {param}"

    inst = _make_instance(tmp_path)
    lane = document_lane.DocumentLane(REPO_ROOT, inst)

    origin = "official_club_statement:2026-11-18-syndi-1"
    body_a = "Club announced Player Alpha signed a two-year extension (Portal A repost)."
    body_b = "Club announced Player Alpha signed a two-year extension (Portal B syndication)."

    meta_a = _make_bundle_meta(
        canonical_url="https://portal-a.example.org/news/1001",
        related_urls=["https://mirror-a.example.org/news/1001"],
        upstream_origin=origin,
        source_id="portal-a",
        source_tier="T1.5",
        body=body_a,
    )
    meta_b = _make_bundle_meta(
        canonical_url="https://portal-b.example.org/news/2002",
        related_urls=["https://mirror-b.example.org/news/2002"],
        upstream_origin=origin,
        source_id="portal-b",
        source_tier="T2",
        body=body_b,
    )
    bundle_a = tmp_path / "bundle_a.md"
    bundle_b = tmp_path / "bundle_b.md"
    bundle_a.write_text(_render_bundle_md(meta_a, body_a), encoding="utf-8")
    bundle_b.write_text(_render_bundle_md(meta_b, body_b), encoding="utf-8")

    res_a = lane.ingest_acquisition_bundle(bundle_a)
    res_b = lane.ingest_acquisition_bundle(bundle_b)
    assert res_a["accepted"] is True
    assert res_b["accepted"] is True

    prov_a = json.loads((Path(res_a["archive_path"]) / "provenance.json").read_text(encoding="utf-8"))
    cap_a = prov_a["captures"][0]
    assert cap_a.get("upstream_origin") == origin
    assert cap_a.get("related_urls") == meta_a["related_urls"]
    assert cap_a.get("source_tier") == "T1.5"
    assert cap_a.get("raw_sha256") == meta_a["raw_sha256"]
    assert cap_a.get("extracted_sha256") == meta_a["extracted_sha256"]

    passage_a = kc_passages.build_routed_passage(
        source_id=res_a["doc_id"],
        revision="r1",
        locator="p1",
        text=body_a,
        upstream_origin=origin,
        related_urls=meta_a["related_urls"],
        source_tier="T1.5",
        raw_sha256=meta_a["raw_sha256"],
        extracted_sha256=meta_a["extracted_sha256"],
    )
    passage_b = kc_passages.build_routed_passage(
        source_id=res_b["doc_id"],
        revision="r1",
        locator="p1",
        text=body_b,
        upstream_origin=origin,
        related_urls=meta_b["related_urls"],
        source_tier="T2",
        raw_sha256=meta_b["raw_sha256"],
        extracted_sha256=meta_b["extracted_sha256"],
    )
    assert passage_a["upstream_origin"] == origin
    assert passage_a["source_tier"] == "T1.5"
    assert passage_a["raw_sha256"] == meta_a["raw_sha256"]
    assert passage_a["extracted_sha256"] == meta_a["extracted_sha256"]

    # Reposted URLs sharing upstream_origin MUST collapse and NOT inflate independent corroboration
    claims = [
        {"claim_id": "c1", "doc_id": res_a["doc_id"], "statement": "Player Alpha extended for 2 years"},
        {"claim_id": "c2", "doc_id": res_b["doc_id"], "statement": "Player Alpha extended for 2 years"},
    ]
    docs_by_id = {
        res_a["doc_id"]: {"provenance": prov_a, "upstream_origin": passage_a["upstream_origin"]},
        res_b["doc_id"]: {"provenance": json.loads((Path(res_b["archive_path"]) / "provenance.json").read_text(encoding="utf-8")),
                          "upstream_origin": passage_b["upstream_origin"]},
    }
    ind = kc_evidence_status.classify_claim_independence(claims, documents_by_id=docs_by_id)
    assert ind["independent_source_count"] == 1
    assert ind["posture"] == "SINGLE_SOURCE_UNCORROBORATED"


def test_p0_1_document_lane_atomic_staging_and_merge_recovery(tmp_path, monkeypatch):
    inst = _make_instance(tmp_path)
    lane = document_lane.DocumentLane(REPO_ROOT, inst)

    doc_file = inst.document_input_root / "atomic_test.txt"
    doc_file.write_text("Synthetic atomic staging document body for REQ-220.", encoding="utf-8")

    # Simulate interrupted write mid-way through archive publish (when writing provenance.json)
    real_atomic = document_lane.atomic
    failed_once = {"triggered": False}

    def flaky_atomic(path, data):
        if Path(path).name == "provenance.json" and not failed_once["triggered"]:
            failed_once["triggered"] = True
            raise OSError("SIMULATED_POWER_LOSS")
        return real_atomic(path, data)

    monkeypatch.setattr(document_lane, "atomic", flaky_atomic)

    with pytest.raises(OSError, match="SIMULATED_POWER_LOSS"):
        lane.ingest_file(doc_file, capture_channel="local_file")

    # No partial doc_* directory may be published in archive_root
    published_dirs = [p for p in inst.document_archive_root.glob("doc_*") if p.is_dir()]
    assert published_dirs == [], f"Partial archive directory leaked: {published_dirs}"

    # Retry succeeds cleanly without DOCUMENT_ARCHIVE_PROVENANCE_INVALID
    monkeypatch.setattr(document_lane, "atomic", real_atomic)
    res = lane.ingest_file(doc_file, capture_channel="local_file")
    assert res["accepted"] is True
    assert res["disposition"] == "new"

    # Simulate interrupted _merge_exact: copy same text to second input file, fail after writing raw capture
    doc_file_2 = inst.document_input_root / "atomic_test_copy.txt"
    doc_file_2.write_text("Synthetic atomic staging document body for REQ-220.", encoding="utf-8")

    merge_failed = {"triggered": False}

    def flaky_merge_atomic(path, data):
        if Path(path).name == "dedup.json" and not merge_failed["triggered"]:
            merge_failed["triggered"] = True
            raise OSError("SIMULATED_MERGE_INTERRUPT")
        return real_atomic(path, data)

    monkeypatch.setattr(document_lane, "atomic", flaky_merge_atomic)
    with pytest.raises(OSError, match="SIMULATED_MERGE_INTERRUPT"):
        lane.ingest_file(doc_file_2, capture_channel="local_file")

    # Retry merge must recover idempotently without DOCUMENT_ARCHIVE_PROVENANCE_INVALID
    monkeypatch.setattr(document_lane, "atomic", real_atomic)
    res_retry = lane.ingest_file(doc_file_2, capture_channel="local_file")
    assert res_retry["accepted"] is True
    assert res_retry["disposition"] == "exact_merge"


def test_p0_1_document_lane_single_pass_shingle_cache(tmp_path, monkeypatch):
    inst = _make_instance(tmp_path)
    lane = document_lane.DocumentLane(REPO_ROOT, inst)

    # Seed 6 distinct documents into archive
    base_texts = [
        f"Synthetic unique CBA basketball scouting report number {i} covering defensive rotations and spacing."
        for i in range(6)
    ]
    for i, text in enumerate(base_texts):
        f = inst.document_input_root / f"seed_{i}.txt"
        f.write_text(text, encoding="utf-8")
        assert lane.ingest_file(f, capture_channel="local_file")["accepted"] is True

    # Prepare a batch of 5 new documents (including 1 near-duplicate of seed_0)
    batch_entries = []
    for i in range(4):
        f = inst.document_input_root / f"batch_{i}.txt"
        f.write_text(
            f"Brand new distinct CBA training camp summary item {i} with completely different phrasing and statistics.",
            encoding="utf-8",
        )
        batch_entries.append({"path": f.name, "capture_channel": "local_file"})
    near_file = inst.document_input_root / "batch_near.txt"
    near_file.write_text(base_texts[0] + "!", encoding="utf-8")
    batch_entries.append({"path": near_file.name, "capture_channel": "local_file"})

    manifest = inst.document_input_root / "batch_manifest.json"
    manifest.write_text(json.dumps({"documents": batch_entries}), encoding="utf-8")

    # Count _shingles calls on a fresh DocumentLane reading the 6 existing + 5 batch docs.
    # Without caching, 5 batch items x ~8 existing docs x 2 = ~80+ _shingles calls.
    # With single-pass caching, each existing doc is shingled at most once + each new doc once (<= 15 calls).
    fresh_lane = document_lane.DocumentLane(REPO_ROOT, inst)
    shingle_calls = {"count": 0}
    real_shingles = document_lane._shingles

    def counting_shingles(text, width=5):
        shingle_calls["count"] += 1
        return real_shingles(text, width=width)

    monkeypatch.setattr(document_lane, "_shingles", counting_shingles)
    report = fresh_lane.ingest_batch(manifest)

    assert report["documents"] == 5
    # Near-duplicate classification must be preserved accurately
    dispositions = [r["disposition"] for r in report["results"]]
    assert dispositions[-1] == "review_required"
    assert dispositions[:4] == ["new", "new", "new", "new"]
    assert shingle_calls["count"] <= 15, (
        f"Expected single-pass shingle caching (<=15 calls), got {shingle_calls['count']}"
    )


# ===========================================================================
# P0-2: Revision Supersession Chain (LOUD_REVISION) & Offline Snapshot Replay
# ===========================================================================


def test_p0_2_revision_supersession_chain_and_loud_revision(tmp_path):
    build_disclosure = _require_attr(kc_evidence_status, "build_revision_disclosure")
    ingest_bundle = _require_attr(document_lane.DocumentLane, "ingest_acquisition_bundle")
    assert callable(ingest_bundle)

    inst = _make_instance(tmp_path)
    lane = document_lane.DocumentLane(REPO_ROOT, inst)

    url = "https://example.org/cba/official/contract-notice-99"
    body_v1 = "Official notice v1: Player Alpha signed a 1-year contract."
    body_v2 = "Official notice v2 (corrected): Player Alpha signed a 3-year contract."

    b1 = tmp_path / "rev_v1.md"
    b2 = tmp_path / "rev_v2.md"
    b1.write_text(_render_bundle_md(_make_bundle_meta(canonical_url=url, body=body_v1), body_v1), encoding="utf-8")
    b2.write_text(_render_bundle_md(_make_bundle_meta(canonical_url=url, body=body_v2), body_v2), encoding="utf-8")

    r1 = lane.ingest_acquisition_bundle(b1)
    r2 = lane.ingest_acquisition_bundle(b2)

    assert r1["accepted"] is True
    assert r2["accepted"] is True
    assert r2["disposition"] == "revision"
    assert r1["doc_id"] != r2["doc_id"]

    # Old archive text & content_hash preserved untouched
    old_root = Path(r1["archive_path"])
    new_root = Path(r2["archive_path"])
    assert old_root.joinpath("text.txt").read_text(encoding="utf-8") == document_lane.normalize_text(body_v1)

    old_rec = json.loads(old_root.joinpath("record.json").read_text(encoding="utf-8"))
    new_rec = json.loads(new_root.joinpath("record.json").read_text(encoding="utf-8"))
    assert old_rec["content_hash"] == r1["content_hash"]
    assert old_rec.get("superseded_by_doc_id") == r2["doc_id"]
    assert new_rec.get("supersedes_doc_id") == r1["doc_id"]
    assert new_rec.get("superseded_by_doc_id") is None
    assert new_rec.get("revision_seq") == 2

    # Revision index persisted deterministically
    rev_index_path = inst.document_archive_root / "revision_index.json"
    assert rev_index_path.is_file(), "Missing revision_index.json in archive_root"
    rev_index = json.loads(rev_index_path.read_text(encoding="utf-8"))
    chain = rev_index.get(url) or rev_index.get("by_canonical_url", {}).get(url)
    assert isinstance(chain, list) and len(chain) == 2
    assert [entry["doc_id"] for entry in chain] == [r1["doc_id"], r2["doc_id"]]

    # Structured revision_summary drives LOUD_REVISION disclosure
    rev_summary = r2.get("revision_summary")
    assert isinstance(rev_summary, dict)
    assert rev_summary["prior_doc_id"] == r1["doc_id"]
    assert rev_summary["new_doc_id"] == r2["doc_id"]
    assert rev_summary["revision_seq"] == 2
    assert rev_summary["disclosure_posture"] == "LOUD_REVISION"

    disclosure = build_disclosure(rev_summary)
    assert disclosure["posture"] == "REVISION_SUPERSEDED"
    assert disclosure["disclosure_posture"] == "LOUD_REVISION"
    assert disclosure["loud_disclosure_required"] is True
    assert "LOUD_REVISION" in disclosure.get("warnings", [])


def test_p0_2_offline_registration_snapshot_replay(tmp_path):
    replay_fn = _require_attr(source_watcher, "replay_registration_snapshots")

    snap_dir = tmp_path / "reg_snapshots"
    snap_dir.mkdir()

    snap1 = _make_registration_snapshot_payload(marker="V1", observed_at="2026-11-01T10:00:00Z")
    snap2 = _make_registration_snapshot_payload(marker="V2", observed_at="2026-11-02T10:00:00Z")

    # One plain .json and one gzipped .json.gz in chronological order
    (snap_dir / "01_2026-11-01.json").write_text(json.dumps(snap1, ensure_ascii=False), encoding="utf-8")
    with gzip.open(snap_dir / "02_2026-11-02.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(snap2, fh, ensure_ascii=False)

    out_dir = tmp_path / "replay_out"
    result = replay_fn(snap_dir, out_dir)

    assert result["replayed_count"] == 2
    assert result["canonical_publish_allowed"] is False
    assert len(result["runs"]) == 2
    assert all(run["canonical_publish_allowed"] is False for run in result["runs"])
    assert result["runs"][0]["validation_result"] in {"PASS", "REVIEW_REQUIRED"}
    assert result["runs"][1]["validation_result"] in {"PASS", "REVIEW_REQUIRED"}
    # Second snapshot modified all 20 synthetic player rows relative to snapshot 1
    assert result["runs"][1]["diff_counts"]["modified"] > 0
    assert result["runs"][1]["candidate_count"] > 0


# ===========================================================================
# P0-3: Dirty-Set, Homonym Safety, Incremental Card Recompute & Citation Cap
# ===========================================================================


def test_p0_3_dirty_set_and_ambiguous_homonym_undecided():
    compute_dirty = _require_attr(document_mentions, "compute_dirty_set")

    identity_registry = {
        "schema_version": 1,
        "players": [
            {"player_uid": "p-alpha", "canonical_name": "林星河", "status": "ACTIVE"},
            {"player_uid": "p-beta", "canonical_name": "陈浩然", "status": "ACTIVE"},
            {"player_uid": "p-homonym-1", "canonical_name": "张伟", "status": "ACTIVE"},
            {"player_uid": "p-homonym-2", "canonical_name": "张伟", "status": "ACTIVE"},
        ],
        "aliases": [
            {"player_uid": "p-alpha", "alias": "星河"},
        ],
    }

    doc_approved_id = "doc_" + "a" * 24
    doc_homonym_id = "doc_" + "b" * 24

    documents = [
        {
            "doc_id": doc_approved_id,
            "content_hash": "1" * 64,
            "text": "林星河在本轮比赛中贡献22分并完成续约。",
            "matched_persons": ["林星河"],
        },
        {
            "doc_id": doc_homonym_id,
            "content_hash": "2" * 64,
            "text": "张伟和陈浩然出现在热身赛名单中。",
            "matched_persons": ["张伟", "陈浩然"],
        },
    ]
    approved_mentions = [
        {
            "schema_version": 1,
            "doc_id": doc_approved_id,
            "player_uid": "p-alpha",
            "mention_status": "same",
            "mention_role": "subject",
            "mention_method": "manual",
            "mention_confidence": "HIGH",
            "evidence_ref": "manual-review:req220-1",
        }
    ]

    result = compute_dirty(
        documents,
        identity_registry,
        approved_mentions=approved_mentions,
    )

    assert result["dirty_player_uids"] == ["p-alpha"]
    assert set(result["dirty_doc_ids"]) == {doc_approved_id, doc_homonym_id}
    assert result["undecided_count"] >= 2

    # Ambiguous homonyms ("张伟") and unapproved ("陈浩然") MUST remain undecided in verification_queue
    vq = result["verification_queue"]
    assert all(item["mention_status"] == "undecided" for item in vq)
    homonym_entries = [item for item in vq if item.get("matched_name") == "张伟"]
    assert homonym_entries, "Ambiguous homonym 张伟 must appear in verification_queue"
    assert set(homonym_entries[0]["candidate_player_uids"]) == {"p-homonym-1", "p-homonym-2"}
    assert "p-homonym-1" not in result["dirty_player_uids"]
    assert "p-homonym-2" not in result["dirty_player_uids"]
    assert "p-beta" not in result["dirty_player_uids"]


def test_p0_3_incremental_card_recompute_and_citation_cap_10(tmp_path):
    default_cap = _require_attr(consumer_surface, "DEFAULT_MAX_EXTERNAL_CITATIONS_PER_CARD")
    select_cites = _require_attr(consumer_surface, "select_external_citations")
    build_inc = _require_attr(kc_local_producer, "build_incremental_player_cards")

    assert default_cap == 10

    # Build 14 synthetic citations with mixed tiers, completeness, dates, doc_ids
    citations = [
        {"doc_id": "doc_" + f"{i:024x}", "source_tier": tier, "completeness": comp,
         "published_at": pub, "title": f"Article {i}", "canonical_url": f"https://example.org/a/{i}"}
        for i, (tier, comp, pub) in enumerate([
            ("MANUAL", "full", "2026-11-18"),
            ("T2", "full", "2026-11-18"),
            ("T1", "metadata_only", "2026-11-18"),
            ("T1", "full", "2026-11-10"),
            ("T1", "full", "2026-11-18"),
            ("T1", "partial", "2026-11-18"),
            ("T1.5", "full", "2026-11-17"),
            ("T1.5", "partial", "2026-11-18"),
            ("T2", "partial", "2026-11-15"),
            ("T1", "full", "2026-11-15"),
            ("T1.5", "full", "2026-11-18"),
            ("T2", "metadata_only", "2026-11-18"),
            ("MANUAL", "partial", "2026-11-18"),
            ("T1", "full", "2026-11-18"),  # ties with #4 on tier/comp/pub -> doc_id asc tie-break
        ])
    ]
    ranked = select_cites(citations, max_external_citations_per_card=10)
    assert ranked["total_linked_count"] == 14
    assert ranked["displayed_count"] == 10
    assert ranked["truncated"] is True
    assert len(ranked["selected"]) == 10
    # Top 2 must be T1 + full + 2026-11-18 ordered by doc_id asc (#4 before #13)
    assert ranked["selected"][0]["doc_id"] == citations[4]["doc_id"]
    assert ranked["selected"][1]["doc_id"] == citations[13]["doc_id"]
    # MANUAL entries must be truncated out
    assert all(c["source_tier"] != "MANUAL" for c in ranked["selected"])

    # Incremental card recompute across 2 players
    fx_a = _make_player_fixture("p-alpha", "林星河", 18.5)
    fx_b = _make_player_fixture("p-beta", "陈浩然", 11.2)
    fixtures = {"p-alpha": fx_a, "p-beta": fx_b}
    cards_dir = tmp_path / "cards"

    # Initial build of both players
    r_init = build_inc(
        player_fixtures=fixtures,
        dirty_player_uids=["p-alpha", "p-beta"],
        output_dir=cards_dir,
        external_citations_by_player={"p-alpha": citations, "p-beta": citations[:2]},
        max_external_citations_per_card=10,
    )
    assert set(r_init["recomputed_player_uids"]) == {"p-alpha", "p-beta"}
    beta_card_path = Path(r_init["cards"]["p-beta"]["consumer_card_path"])
    beta_research_path = Path(r_init["cards"]["p-beta"]["research_card_path"])
    assert beta_card_path.is_file() and beta_card_path.name.startswith("研究卡_")
    beta_bytes_before = beta_card_path.read_bytes()
    beta_sha_before = hashlib.sha256(beta_bytes_before).hexdigest()
    beta_research_before = beta_research_path.read_bytes()

    # Check honest disclosure of total (14) vs displayed (10) inside alpha's consumer card
    alpha_card_text = Path(r_init["cards"]["p-alpha"]["consumer_card_path"]).read_text(encoding="utf-8")
    assert "14" in alpha_card_text and "10" in alpha_card_text

    # Recompute ONLY p-alpha; p-beta must be 100% unchanged byte-for-byte
    r_inc = build_inc(
        player_fixtures=fixtures,
        dirty_player_uids=["p-alpha"],
        output_dir=cards_dir,
        external_citations_by_player={"p-alpha": citations, "p-beta": citations[:2]},
        max_external_citations_per_card=10,
    )
    assert r_inc["recomputed_player_uids"] == ["p-alpha"]
    assert r_inc["skipped_player_uids"] == ["p-beta"]
    assert beta_card_path.read_bytes() == beta_bytes_before
    assert hashlib.sha256(beta_card_path.read_bytes()).hexdigest() == beta_sha_before
    assert beta_research_path.read_bytes() == beta_research_before


# ===========================================================================
# P0-4: operational_sync.py & cba-kb ops-sync CLI
# ===========================================================================


def test_p0_4_ops_sync_cli_checkpoint_dry_run_freshness_and_drive_a_isolation(tmp_path):
    ops_sync = _require_module("cba_kb.operational_sync")
    run_ops = _require_attr(ops_sync, "run_operational_sync")
    assert callable(run_ops)

    # Verify CLI registers ops-sync subcommand
    parser = cli.build_parser() if hasattr(cli, "build_parser") else None
    cli_src = (REPO_ROOT / "src" / "cba_kb" / "cli.py").read_text(encoding="utf-8")
    assert '"ops-sync"' in cli_src or "'ops-sync'" in cli_src, "cli.py missing ops-sync subcommand"

    inst = _make_instance(tmp_path)
    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir()
    body1 = "Synthetic bundle 1 mentioning 林星河 for ops-sync."
    meta1 = _make_bundle_meta(
        canonical_url="https://example.org/cba/ops/1",
        matched_persons=["林星河"],
        body=body1,
    )
    (bundles_dir / "001.md").write_text(_render_bundle_md(meta1, body1), encoding="utf-8")

    reg_dir = tmp_path / "reg_snaps"
    reg_dir.mkdir()
    snap1 = _make_registration_snapshot_payload(marker="OPS1", observed_at="2026-11-18T10:00:00Z")
    (reg_dir / "01.json").write_text(json.dumps(snap1, ensure_ascii=False), encoding="utf-8")

    published_status = tmp_path / "published_status.json"
    published_status.write_text(json.dumps({
        "published_cards": {},
        "published_doc_ids": [],
    }), encoding="utf-8")

    out_dir = inst.data_root / "ops_out"

    # Dry-run MUST NOT create/mutate checkpoint or archive
    dry = run_ops(
        engine_root=REPO_ROOT,
        instance=inst,
        bundles_dir=bundles_dir,
        output_dir=out_dir,
        registration_snapshots_dir=reg_dir,
        published_status_path=published_status,
        dry_run=True,
    )
    assert dry["dry_run"] is True
    assert dry["drive_api_called"] is False
    checkpoint_path = out_dir / "operational_checkpoint.json"
    assert not checkpoint_path.exists(), "--dry-run must not write operational_checkpoint.json"

    # Run 1: ingests bundle & replays registration snapshot, writes checkpoint & freshness report
    with mock.patch("cba_kb.drive_io.DriveClient", side_effect=AssertionError("Drive API forbidden")) if hasattr(
        importlib.import_module("cba_kb"), "drive_io"
    ) else mock.MagicMock():
        r1 = run_ops(
            engine_root=REPO_ROOT,
            instance=inst,
            bundles_dir=bundles_dir,
            output_dir=out_dir,
            registration_snapshots_dir=reg_dir,
            published_status_path=published_status,
            dry_run=False,
        )

    assert r1["drive_api_called"] is False
    assert r1["ingested_count"] == 1
    assert checkpoint_path.is_file()
    freshness_path = out_dir / "freshness_report.json"
    assert freshness_path.is_file()
    freshness = json.loads(freshness_path.read_text(encoding="utf-8"))
    assert freshness["drive_api_called"] is False
    assert "pending_player_cards" in freshness
    assert "lagged_docs" in freshness and len(freshness["lagged_docs"]) >= 1
    assert "undecided_mentions" in freshness
    assert "registration_candidate_diffs" in freshness and len(freshness["registration_candidate_diffs"]) >= 1

    # Run 2 with no changes: 0 re-ingests and 0 card recomputes
    r2 = run_ops(
        engine_root=REPO_ROOT,
        instance=inst,
        bundles_dir=bundles_dir,
        output_dir=out_dir,
        registration_snapshots_dir=reg_dir,
        published_status_path=published_status,
        dry_run=False,
    )
    assert r2["ingested_count"] == 0
    assert r2["recomputed_player_uids"] == []


# ===========================================================================
# P0-5: Unified registry_file Locking & Deadlock Prevention
# ===========================================================================


def test_p0_5_registry_lock_unification_concurrent_writes_and_no_self_deadlock(tmp_path):
    # Verify _unlocked helpers exist across all 4 registry write paths
    _require_attr(drive_search, "_grant_rights_unlocked")
    _require_attr(drive_discovery, "_save_registry_unlocked")
    _require_attr(drive_poll, "_save_registry_unlocked")
    _require_attr(drive_plan, "_apply_purge_tombstones_unlocked")
    sig_purge = inspect.signature(drive_plan._apply_purge_tombstones)
    assert "_registry_locked" in sig_purge.parameters, (
        "_apply_purge_tombstones must accept _registry_locked parameter"
    )
    for mod in (drive_search, drive_discovery, drive_poll, drive_plan):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "file_lock" in src, f"{mod.__name__} must coordinate on drive_resilience.file_lock"

    # 1. Concurrent grant_rights on distinct file_ids must lose zero updates
    reg_path = tmp_path / "registry.json"
    ledger_path = tmp_path / "ledger.jsonl"
    n_files = 12
    initial_registry = {
        f"file_{i:03d}": {
            "file_id": f"file_{i:03d}",
            "name": f"doc_{i:03d}.md",
            "mime_type": "text/markdown",
            "rights_status": "review",
            "tombstoned": False,
        }
        for i in range(n_files)
    }
    # Add an old tombstoned record for purge test
    initial_registry["file_old_tomb"] = {
        "file_id": "file_old_tomb",
        "name": "old.md",
        "mime_type": "text/markdown",
        "rights_status": "denied",
        "tombstoned": True,
        "tombstoned_ts": "2020-01-01T00:00:00+00:00",
    }
    reg_path.write_text(json.dumps(initial_registry, indent=2), encoding="utf-8")

    def _grant_one(idx: int):
        return drive_search.grant_rights(
            reg_path,
            ledger_path,
            f"file_{idx:03d}",
            approver="human-operator",
            note=f"concurrent grant {idx}",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(_grant_one, i) for i in range(n_files)]
        for fut in futs:
            res = fut.result()
            assert res["rights_status"] == "approved"

    final_reg = json.loads(reg_path.read_text(encoding="utf-8"))
    approved_ids = [
        fid for fid, rec in final_reg.items()
        if fid.startswith("file_0") and rec.get("rights_status") == "approved"
    ]
    assert len(approved_ids) == n_files, f"Lost concurrent updates: {len(approved_ids)}/{n_files}"

    # 2. _apply_plan_resilient invoking _apply_purge_tombstones MUST NOT self-deadlock
    plan_path = tmp_path / "purge_plan.json"
    operations = [
        {
            "op": "purge_tombstones",
            "params": {
                "registry": str(reg_path),
                "older_than_days": 30,
            },
        }
    ]
    plan = {
        "plan_id": drive_plan._plan_id(operations),
        "approval": {"approver": "human-operator"},
        "operations": operations,
    }
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    res_plan = drive_plan._apply_plan_resilient(
        plan_path=plan_path,
        ledger_path=ledger_path,
        plan=plan,
        ctx={},
        actor="muse",
        timeout_s=5.0,
    )
    assert res_plan["failed"] is None
    assert len(res_plan["completed"]) == 1
    assert "file_old_tomb" in res_plan["completed"][0]["result"]["purged"]
    post_purge_reg = json.loads(reg_path.read_text(encoding="utf-8"))
    assert "file_old_tomb" not in post_purge_reg
