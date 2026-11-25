"""Regression tests for Round 1 Q1+Q2 remediation findings on REQ-231 (v2.3.1).

100% synthetic fixtures (G5 == 0, G6 == 0). Covers all 6 blocking findings plus
non-blocking hardenings:
1. pull_instance_bundle: pull-side allowlist/exclusion enforcement, pre-existing
   target & parent directory symlink rejection (O_NOFOLLOW), incremental tar cap.
2. allow_s3_501_lock_fallback=True: live-lock contention rejection & mandatory
   post-PUT read-back SHA-256 / owner_id / fencing_token verification.
3. archive_compactor: v2.3.1-1 default protection, release_status dual protection,
   fail-closed ARCHIVE_COMPACT_MISSING_RELEASE_STATE on unanchored/omitted dirs,
   nested .zip/.tar.gz skip without trashing parent dir.
4. upsert_snowball_card_to_drive: exact structured filename parsing preventing
   prefix-collision retirement (`A_B` vs `A`).
5. _parse_foreign_registration_rows cross-table club reset (orphan raises
   FOREIGN_ROW_ORPHAN_CONTINUATION) and <25 KB byte-budget guard for long names.
6. enrich_bundle_matched_persons & _match_document_bundles: CJK boundary +
   longest-match guard blocking `王一` inside `王一鸣` / `小王一` while matching
   natural prose `王一回归俱乐部`.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import pytest

from cba_kb.archive_compactor import (
    ArchiveCompactionError,
    DEFAULT_PROTECTED_BUNDLE_RELEASES,
    compact_drive_archive,
)
from cba_kb.cloud_state import (
    DEFAULT_INSTANCE_BUNDLE_KEY,
    R2CloudStateStore,
    R2Config,
    R2LockContentionError,
)
from cba_kb.consumer_surface import (
    COLLECTOR_DIGEST_MAX_BYTES,
    _parse_foreign_registration_rows,
    enrich_bundle_matched_persons,
    render_collector_official_digest_md,
)
from cba_kb.drive import upsert_snowball_card_to_drive
from cba_kb.person_card_builder import _match_document_bundles


def _make_tar_gz(entries: List[Tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in entries:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _r2_cfg() -> R2Config:
    return R2Config(
        endpoint_url="https://synth-q2.example.invalid",
        bucket="synth-q2-bucket",
        access_key_id="SYNTH_KEY",
        secret_access_key="SYNTH_SECRET",
    )


class _FakeDrive:
    FOLDER_MIME = "application/vnd.google-apps.folder"

    def __init__(self) -> None:
        self._main_thread_id = threading.get_ident()
        self.files: Dict[str, Dict[str, Any]] = {}
        self.contents: Dict[str, bytes] = {}
        self.trashed_ids: List[str] = []
        self.upload_calls = 0
        self.delete_called = False
        self._counter = 0

    def add_folder(self, folder_id: str, name: str, parents: Optional[List[str]] = None) -> str:
        self.files[folder_id] = {
            "id": folder_id,
            "name": name,
            "mimeType": self.FOLDER_MIME,
            "parents": list(parents or []),
            "trashed": False,
            "modifiedTime": "2026-11-20T00:00:00Z",
        }
        return folder_id

    def add_file(
        self,
        file_id: str,
        name: str,
        parent: str,
        content: bytes,
        mime: str = "text/markdown",
    ) -> str:
        self.files[file_id] = {
            "id": file_id,
            "name": name,
            "mimeType": mime,
            "parents": [parent],
            "size": str(len(content)),
            "sha256Checksum": hashlib.sha256(content).hexdigest(),
            "trashed": False,
            "modifiedTime": "2026-11-20T00:00:00Z",
        }
        self.contents[file_id] = bytes(content)
        return file_id

    def list(self, parent: str) -> List[Dict[str, Any]]:
        return [
            dict(meta)
            for meta in self.files.values()
            if parent in meta.get("parents", []) and not meta.get("trashed", False)
        ]

    def meta(self, file_id: str) -> Dict[str, Any]:
        return dict(self.files[file_id])

    def get(self, file_id: str) -> bytes:
        return self.contents[file_id]

    def get_with_meta(self, file_id: str, *, known_meta=None) -> Tuple[bytes, Dict[str, Any]]:
        return self.contents[file_id], self.meta(file_id)

    def get_parents(self, file_id: str) -> List[str]:
        return list(self.files.get(file_id, {}).get("parents") or [])

    def put(self, file_id: str, content: bytes, mime: str) -> Dict[str, Any]:
        self.contents[file_id] = bytes(content)
        self.files[file_id]["mimeType"] = mime
        self.files[file_id]["size"] = str(len(content))
        self.files[file_id]["sha256Checksum"] = hashlib.sha256(content).hexdigest()
        return dict(self.files[file_id])

    def create_binary(self, parent: str, name: str, content: bytes, mime: str) -> Dict[str, Any]:
        self._counter += 1
        fid = f"drv_q2_{self._counter:04d}"
        self.upload_calls += 1
        meta = {
            "id": fid,
            "name": name,
            "mimeType": mime,
            "parents": [parent],
            "size": str(len(content)),
            "sha256Checksum": hashlib.sha256(content).hexdigest(),
            "trashed": False,
            "modifiedTime": "2026-11-25T00:00:00Z",
        }
        self.files[fid] = meta
        self.contents[fid] = bytes(content)
        return dict(meta)

    def trash(self, file_id: str) -> Dict[str, Any]:
        self.files[file_id]["trashed"] = True
        self.trashed_ids.append(file_id)
        return dict(self.files[file_id])

    def delete(self, file_id: str) -> None:
        self.delete_called = True
        raise AssertionError("HARD_DELETE_FORBIDDEN")


# ---------------------------------------------------------------------------
# Q2 Blocking #1: pull_instance_bundle allowlist + symlink/O_NOFOLLOW + caps
# ---------------------------------------------------------------------------


def test_q2_1_pull_instance_bundle_allowlist_symlink_redirection_and_incremental_cap(tmp_path: Path) -> None:
    objs: Dict[str, Tuple[bytes, Dict[str, str], str]] = {}

    def _put(key: str, payload: bytes) -> None:
        objs[key] = (
            payload,
            {"x-amz-meta-sha256": hashlib.sha256(payload).hexdigest()},
            '"etag1"',
        )

    def _transport(method: str, url: str, *, headers: Dict[str, str], data: bytes = b""):
        key = urlparse(url).path.lstrip("/").split("/", 1)[1]
        if method == "GET":
            if key not in objs:
                return 404, {}, b"NotFound"
            p, m, e = objs[key]
            return 200, {**m, "ETag": e}, p
        return 405, {}, b""

    store = R2CloudStateStore(_r2_cfg(), transport=_transport)

    # 1. Disallowed/excluded members rejected on pull before any write
    for bad_path in (
        "config/production.json",
        ".credentials/token.json",
        "evidence/note.md",
        "inbox/drop.csv",
        "data/document_lane/archive/doc_1/raw/body.html",
        "data/arbitrary_unlisted.txt",
    ):
        _put(
            DEFAULT_INSTANCE_BUNDLE_KEY,
            _make_tar_gz([("data/MASTER.csv", b"ok\n"), (bad_path, b"forbidden")]),
        )
        out_dir = tmp_path / f"disallow_{hashlib.sha256(bad_path.encode()).hexdigest()[:8]}"
        with pytest.raises(ValueError, match="R2_BUNDLE_DISALLOWED_MEMBER"):
            store.pull_instance_bundle(out_dir)
        assert not (out_dir / "data" / "MASTER.csv").exists()

    # 2. Pre-existing file symlink redirection (data/MASTER.csv -> ../config/production.json)
    out_file_sym = tmp_path / "file_sym"
    (out_file_sym / "data").mkdir(parents=True)
    (out_file_sym / "config").mkdir(parents=True)
    prod_file = out_file_sym / "config" / "production.json"
    prod_file.write_text('{"sentinel": "UNTOUCHED"}', encoding="utf-8")
    os.symlink("../config/production.json", out_file_sym / "data" / "MASTER.csv")
    _put(DEFAULT_INSTANCE_BUNDLE_KEY, _make_tar_gz([("data/MASTER.csv", b"POISONED")]))
    with pytest.raises(ValueError, match="R2_BUNDLE_SYMLINK_TARGET"):
        store.pull_instance_bundle(out_file_sym)
    assert prod_file.read_text(encoding="utf-8") == '{"sentinel": "UNTOUCHED"}'

    # 3. Pre-existing parent directory symlink redirection (data -> outside_dir)
    out_dir_sym = tmp_path / "dir_sym"
    outside_dir = tmp_path / "outside_dir"
    out_dir_sym.mkdir()
    outside_dir.mkdir()
    os.symlink(str(outside_dir), out_dir_sym / "data")
    with pytest.raises(ValueError, match="R2_BUNDLE_SYMLINK_TARGET|R2_BUNDLE_PATH_TRAVERSAL"):
        store.pull_instance_bundle(out_dir_sym)
    assert not (outside_dir / "MASTER.csv").exists()

    # 4. Incremental member cap & extracted size cap enforced cleanly
    _put(
        DEFAULT_INSTANCE_BUNDLE_KEY,
        _make_tar_gz([(f"data/cards/c_{i}.md", b"x" * 16) for i in range(6)]),
    )
    with pytest.raises(ValueError, match="R2_BUNDLE_MAX_MEMBERS_EXCEEDED"):
        store.pull_instance_bundle(tmp_path / "cap_mem", max_members=3)
    with pytest.raises(ValueError, match="R2_BUNDLE_EXTRACTED_SIZE_EXCEEDED"):
        store.pull_instance_bundle(tmp_path / "cap_ext", max_extracted_bytes=32)


# ---------------------------------------------------------------------------
# Q2 Blocking #2: 501 lock fallback contention & read-back verification
# ---------------------------------------------------------------------------


def test_q2_2_s3_501_lock_fallback_contention_and_readback_verification() -> None:
    objs: Dict[str, Tuple[bytes, Dict[str, str], str]] = {}

    def _transport_501(method: str, url: str, *, headers: Dict[str, str], data: bytes = b""):
        key = urlparse(url).path.lstrip("/").split("/", 1)[1]
        lh = {k.lower(): v for k, v in headers.items()}
        if method == "GET":
            if key not in objs:
                return 404, {}, b"NotFound"
            p, m, e = objs[key]
            return 200, {**m, "ETag": e}, p
        if method == "PUT":
            if "if-none-match" in lh or "if-match" in lh:
                return 501, {}, b"NotImplemented"
            meta = {k: v for k, v in lh.items() if k.startswith("x-amz-meta-")}
            etag = f'"{hashlib.md5(data).hexdigest()}"'
            objs[key] = (bytes(data), meta, etag)
            return 200, {"ETag": etag}, b""
        return 405, {}, b""

    store = R2CloudStateStore(_r2_cfg(), transport=_transport_501, allow_s3_501_lock_fallback=True)

    # 1. Owner A acquires lock; Owner B must NOT overwrite live lock
    ok_a, doc_a = store.acquire_active_job_lock("2026-2027", "合成球员甲", job_id="job_a", owner_id="owner_a")
    assert ok_a is True
    assert doc_a.get("fencing_token")

    ok_b, cur_b = store.acquire_active_job_lock("2026-2027", "合成球员甲", job_id="job_b", owner_id="owner_b")
    assert ok_b is False
    assert cur_b["owner_id"] == "owner_a"

    # 2. Simulated race during 501 fallback: another writer overwrites right after unconditional PUT
    race_objs: Dict[str, Tuple[bytes, Dict[str, str], str]] = {}

    def _racing_transport(method: str, url: str, *, headers: Dict[str, str], data: bytes = b""):
        key = urlparse(url).path.lstrip("/").split("/", 1)[1]
        lh = {k.lower(): v for k, v in headers.items()}
        if method == "GET":
            if key not in race_objs:
                return 404, {}, b"NotFound"
            p, m, e = race_objs[key]
            return 200, {**m, "ETag": e}, p
        if method == "PUT":
            if "if-none-match" in lh or "if-match" in lh:
                return 501, {}, b"NotImplemented"
            # Concurrent winner overwrites object immediately
            winner_payload = json.dumps(
                {
                    "job_id": "job_winner",
                    "owner_id": "other_owner",
                    "fencing_token": "other_token",
                    "status": "RUNNING",
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            race_objs[key] = (
                winner_payload,
                {"x-amz-meta-sha256": hashlib.sha256(winner_payload).hexdigest()},
                '"etag_winner"',
            )
            return 200, {"ETag": '"etag_winner"'}, b""
        return 405, {}, b""

    race_store = R2CloudStateStore(_r2_cfg(), transport=_racing_transport, allow_s3_501_lock_fallback=True)
    with pytest.raises(R2LockContentionError, match="R2_LOCK_READBACK"):
        race_store.acquire_active_job_lock("2026-2027", "合成球员乙", job_id="job_loser", owner_id="owner_loser")


# ---------------------------------------------------------------------------
# Q2 Blocking #3: archive_compactor protection & fail-closed release state
# ---------------------------------------------------------------------------


def test_q2_3_archive_compactor_protects_v231_fail_closed_missing_state_and_nested_archives() -> None:
    assert "v2.3.1-1" in DEFAULT_PROTECTED_BUNDLE_RELEASES

    # 1. Protects v2.3.1-1 by default AND protects both release_id & previous_release_id from release_status
    d1 = _FakeDrive()
    d1.add_folder("hist_root", "90_archive_历史归档")
    v231_dir = d1.add_folder("dir_v231", "v2.3.1-1", ["hist_root"])
    d1.add_file("b_v231", "release_bundle.tar.gz", v231_dir, b"gz", "application/gzip")
    v232_dir = d1.add_folder("dir_v232", "v2.3.2-1", ["hist_root"])
    d1.add_file("b_v232", "release_bundle.tar.gz", v232_dir, b"gz", "application/gzip")
    d1.add_file("loose_1", "before_old.json", "hist_root", b"{}", "application/json")

    plan = compact_drive_archive(
        d1,
        history_folder_id="hist_root",
        release_status={"release_id": "v2.3.2-1", "previous_release_id": "v2.3.1-1"},
        execute=False,
    )
    assert "dir_v231" not in plan["candidate_ids"]
    assert "dir_v232" not in plan["candidate_ids"]
    assert "loose_1" in plan["candidate_ids"]

    # 2. Fail-closed ARCHIVE_COMPACT_MISSING_RELEASE_STATE when execute=True omits release state on unanchored dir
    d2 = _FakeDrive()
    d2.add_folder("hist_root", "90_archive_历史归档")
    unanchored = d2.add_folder("dir_future", "v2.9.0-1", ["hist_root"])
    d2.add_file("f_future", "snapshot.json", unanchored, b"{}", "application/json")
    with pytest.raises(ArchiveCompactionError, match="ARCHIVE_COMPACT_MISSING_RELEASE_STATE"):
        compact_drive_archive(d2, history_folder_id="hist_root", execute=True)
    assert d2.upload_calls == 0
    assert d2.trashed_ids == []

    # 3. Fail-closed when execute=True omits DEFAULT_PROTECTED_BUNDLE_RELEASES without release_status
    d3 = _FakeDrive()
    d3.add_folder("hist_root", "90_archive_历史归档")
    d3.add_file("loose_2", "before_x.json", "hist_root", b"{}", "application/json")
    with pytest.raises(ArchiveCompactionError, match="ARCHIVE_COMPACT_MISSING_RELEASE_STATE"):
        compact_drive_archive(d3, history_folder_id="hist_root", keep_release_dirs=(), execute=True)
    assert d3.upload_calls == 0

    # 4. Nested .zip/.tar.gz inside legacy dir is skipped and parent folder is NOT trashed
    d4 = _FakeDrive()
    d4.add_folder("hist_root", "90_archive_历史归档")
    v215_dir = d4.add_folder("dir_v215", "v2.1.5-1", ["hist_root"])
    nested_plain = d4.add_file("plain_1", "candidate_note.md", v215_dir, b"# note\n")
    nested_zip = d4.add_file("zip_1", "backup.zip", v215_dir, b"PK\x03\x04", "application/zip")
    res4 = compact_drive_archive(
        d4,
        history_folder_id="hist_root",
        release_status={"release_id": "v2.3.1-1", "previous_release_id": "v2.3.0-1", "status": "COMPLETE"},
        execute=True,
        tag="q2nested",
    )
    assert res4["mode"] == "execute"
    assert nested_plain in d4.trashed_ids
    assert nested_zip not in d4.trashed_ids
    assert v215_dir not in d4.trashed_ids
    assert d4.delete_called is False


# ---------------------------------------------------------------------------
# Q2 Blocking #4: upsert_snowball_card_to_drive exact structured matching
# ---------------------------------------------------------------------------


def test_q2_4_upsert_snowball_card_prevents_prefix_collision_retirement() -> None:
    drive = _FakeDrive()
    drive.add_folder("snowball_dir", "25_snowball_按需即时卡")
    # Two distinct players sharing prefix `A` vs `A_B`
    legacy_a = drive.add_file(
        "legacy_a",
        "研究卡_合成甲_unassigned_2026-2027_CURRENT.md",
        "snowball_dir",
        b"# legacy A\n\nend_marker: true\n",
    )
    legacy_ab = drive.add_file(
        "legacy_ab",
        "研究卡_合成甲_乙_unassigned_2026-2027_CURRENT.md",
        "snowball_dir",
        b"# legacy A_B\n\nend_marker: true\n",
    )

    receipt = upsert_snowball_card_to_drive(
        drive,
        folder_id="snowball_dir",
        filename="研究卡_合成甲_乙_合成俱乐部_2026-2027_CURRENT.md",
        content_bytes=b"# new A_B card\n\nend_marker: true\n",
        forbidden_folder_ids=["protected_root"],
        player_name="合成甲_乙",
        season="2026-2027",
    )
    assert drive.delete_called is False
    assert legacy_ab in drive.trashed_ids
    assert legacy_a not in drive.trashed_ids
    assert receipt["retired_legacy_files"] == ["研究卡_合成甲_乙_unassigned_2026-2027_CURRENT.md"]


# ---------------------------------------------------------------------------
# Q2 Blocking #5: Foreign table reset across tables + <25 KB byte-budget guard
# ---------------------------------------------------------------------------


def test_q2_5_foreign_table_resets_club_across_tables_and_digest_byte_budget_guard() -> None:
    # 1. Second table starts with orphan continuation -> must NOT inherit table 1's club
    multi_table_body = "\n".join([
        "### 表一",
        "| 俱乐部 | 外籍球员 |  | 国籍 | 位置 | 球衣号 |",
        "| --- | --- | --- | --- | --- | --- |",
        "| 合成首钢 | SYNTH ONE | 外援一 | 合成国 | PG | 1 |",
        "",
        "### 表二",
        "| 俱乐部 | 外籍球员 |  | 国籍 | 位置 | 球衣号 |",
        "| --- | --- | --- | --- | --- | --- |",
        "| ORPHAN TWO | 孤儿外援二 | 合成国 | SG | 2 |  |",
    ])
    with pytest.raises(ValueError, match="FOREIGN_ROW_ORPHAN_CONTINUATION"):
        _parse_foreign_registration_rows({"body": multi_table_body})

    # 2. 20 clubs x 4 foreign players with very long club & player names still stays strictly < 25 KB
    table_lines = [
        "| 俱乐部 | 外籍球员 |  | 国籍 | 位置 | 球衣号 |",
        "| --- | --- | --- | --- | --- | --- |",
        "| 英文名 | 中文名 |  |  |  |  |",
    ]
    for c_idx in range(1, 21):
        club = f"合成超长名称测试篮球职业俱乐部第{c_idx:02d}分队"
        for p_idx in range(1, 5):
            en = f"SYNTHETIC VERY LONG FOREIGN PLAYER NAME {c_idx:02d} {p_idx:02d}"
            zh = f"合成超长译名外籍球员{c_idx:02d}号{p_idx}"
            if p_idx == 1:
                table_lines.append(f"| {club} | {en} | {zh} | 合成国 | PG | {p_idx} |")
            else:
                table_lines.append(f"| {en} | {zh} | 合成国 | SG | {p_idx} |  |")

    digest_bytes = render_collector_official_digest_md(
        bundles=[
            {
                "doc_id": "doc_long_foreign_001",
                "title": "2026-2027赛季CBA联赛外籍球员注册信息",
                "source_tier": "T1",
                "event_class": "foreign_registration",
                "published_at": "2026-11-24",
                "raw_sha256": "c" * 64,
                "body": "\n".join(table_lines),
            }
        ],
        release_id="v2.3.1-1",
        engine_sha="7afb18b1e867146ed99cc73fa16e53d990c54e95",
        compact_curated=True,
        include_foreign_quick_index=True,
    )
    assert len(digest_bytes) < COLLECTOR_DIGEST_MAX_BYTES


# ---------------------------------------------------------------------------
# Q2 Blocking #6: Chinese name substring boundary safety (title + body)
# ---------------------------------------------------------------------------


def test_q2_6_chinese_name_boundary_blocks_substring_false_positives_in_title_and_body() -> None:
    roster_only_short = [
        {"player_name": "王一", "current_club": "合成俱乐部甲", "player_uid": "p:wy"},
    ]

    # 1. `王一鸣` and `小王一` must NOT trigger `王一` even when `王一鸣` is not in roster
    bundle_fp = {
        "doc_id": "doc_fp_001",
        "title": "关于王一鸣转会公示",
        "body": "本轮公告涉及王一鸣与小王一的注册手续办理。",
        "matched_persons": [],
    }
    enriched_fp = enrich_bundle_matched_persons(bundle_fp, roster_candidates=roster_only_short)
    assert "王一" not in enriched_fp["matched_persons"]

    matched_fp = _match_document_bundles(
        {"player_name": "王一", "current_club": "合成俱乐部甲", "player_uid": "p:wy"},
        [bundle_fp],
        roster_candidates=roster_only_short,
    )
    assert matched_fp == []

    # 2. Natural Chinese prose `王一回归俱乐部` MUST still match `王一`
    bundle_tp = {
        "doc_id": "doc_tp_001",
        "title": "王一回归俱乐部训练",
        "body": "据公告，王一回归俱乐部并完成本赛季注册。",
        "matched_persons": [],
    }
    enriched_tp = enrich_bundle_matched_persons(bundle_tp, roster_candidates=roster_only_short)
    assert "王一" in enriched_tp["matched_persons"]

    matched_tp = _match_document_bundles(
        {"player_name": "王一", "current_club": "合成俱乐部甲", "player_uid": "p:wy"},
        [bundle_tp],
        roster_candidates=roster_only_short,
    )
    assert any(c["doc_id"] == "doc_tp_001" for c in matched_tp)


# ---------------------------------------------------------------------------
# Round 2 Q1+Q2 Remediation Regression Tests
# ---------------------------------------------------------------------------


def test_r2_1_concurrent_dual_client_501_lock_acquire_release_and_reserved_extra_stripping() -> None:
    objs: Dict[str, Tuple[bytes, Dict[str, str], str]] = {}
    objs_guard = threading.Lock()

    def _transport_501(method: str, url: str, *, headers: Dict[str, str], data: bytes = b""):
        key = urlparse(url).path.lstrip("/").split("/", 1)[1]
        lh = {k.lower(): v for k, v in headers.items()}
        with objs_guard:
            if method == "GET":
                if key not in objs:
                    return 404, {}, b"NotFound"
                p, m, e = objs[key]
                return 200, {**m, "ETag": e}, p
            if method == "PUT":
                if "if-none-match" in lh or "if-match" in lh:
                    return 501, {}, b"NotImplemented"
                meta = {k: v for k, v in lh.items() if k.startswith("x-amz-meta-")}
                etag = f'"{hashlib.md5(data).hexdigest()}"'
                objs[key] = (bytes(data), meta, etag)
                return 200, {"ETag": etag}, b""
        return 405, {}, b""

    store_a = R2CloudStateStore(_r2_cfg(), transport=_transport_501, allow_s3_501_lock_fallback=True)
    store_b = R2CloudStateStore(_r2_cfg(), transport=_transport_501, allow_s3_501_lock_fallback=True)

    # 1. Reserved keys in extra cannot forge fencing_token, owner_id, status, or lock_mode
    ok_forge, doc_forge = store_a.acquire_active_job_lock(
        "2026-2027",
        "合成防伪甲",
        job_id="job_forge",
        owner_id="real_owner",
        extra={
            "fencing_token": "forged",
            "owner_id": "evil",
            "status": "RELEASED",
            "lock_mode": "bypass",
        },
    )
    assert ok_forge is True
    assert doc_forge["fencing_token"] != "forged"
    assert doc_forge["owner_id"] == "real_owner"
    assert doc_forge["status"] == "RUNNING"
    assert "lock_mode" not in doc_forge

    # 2. True concurrent dual-client race via threading.Barrier -> ONLY ONE acquires
    barrier = threading.Barrier(2)
    outcomes: List[Tuple[str, bool, Optional[Exception]]] = []
    outcomes_guard = threading.Lock()

    def _worker(tag: str, st: R2CloudStateStore) -> None:
        barrier.wait()
        try:
            acquired, _ = st.acquire_active_job_lock(
                "2026-2027", "合成并发乙", job_id=f"job_{tag}", owner_id=f"owner_{tag}"
            )
            with outcomes_guard:
                outcomes.append((tag, acquired, None))
        except Exception as exc:
            with outcomes_guard:
                outcomes.append((tag, False, exc))

    t1 = threading.Thread(target=_worker, args=("A", store_a))
    t2 = threading.Thread(target=_worker, args=("B", store_b))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    winners = [o for o in outcomes if o[1] is True]
    losers = [o for o in outcomes if o[1] is False]
    assert len(winners) == 1, f"Expected exactly 1 lock winner, got {outcomes}"
    assert len(losers) == 1
    assert losers[0][2] is None or isinstance(losers[0][2], R2LockContentionError)

    # 3. release_active_job_lock in 501 fallback fails closed if remote fencing_token changed mid-release
    winner_tag = winners[0][0]
    winner_store = store_a if winner_tag == "A" else store_b
    lock_key = winner_store._lock_key("合成并发乙", "2026-2027")

    get_count = {"n": 0}

    def _tampering_release_transport(method: str, url: str, *, headers: Dict[str, str], data: bytes = b""):
        key = urlparse(url).path.lstrip("/").split("/", 1)[1]
        lh = {k.lower(): v for k, v in headers.items()}
        if method == "GET":
            get_count["n"] += 1
            if key == lock_key and get_count["n"] >= 2:
                # Simulate another process replacing the lock token between initial GET and 501 fallback pre-check
                tampered = json.dumps(
                    {
                        "job_id": f"job_{winner_tag}",
                        "owner_id": f"owner_{winner_tag}",
                        "fencing_token": "hijacked_token",
                        "status": "RUNNING",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                return (
                    200,
                    {"x-amz-meta-sha256": hashlib.sha256(tampered).hexdigest(), "ETag": '"etag_hijack"'},
                    tampered,
                )
            p, m, e = objs[key]
            return 200, {**m, "ETag": e}, p
        if method == "PUT":
            if "if-none-match" in lh or "if-match" in lh:
                return 501, {}, b"NotImplemented"
            return 200, {"ETag": '"e_put"'}, b""
        return 405, {}, b""

    tampered_store = R2CloudStateStore(
        _r2_cfg(), transport=_tampering_release_transport, allow_s3_501_lock_fallback=True
    )
    with pytest.raises(R2LockContentionError, match="owner_or_token_changed"):
        tampered_store.release_active_job_lock(
            "2026-2027", "合成并发乙", owner_id=f"owner_{winner_tag}", job_id=f"job_{winner_tag}"
        )


def test_r2_2_tar_pax_header_bomb_and_nonzero_dir_size_rejected(tmp_path: Path) -> None:
    objs: Dict[str, Tuple[bytes, Dict[str, str], str]] = {}

    def _put(payload: bytes) -> None:
        objs[DEFAULT_INSTANCE_BUNDLE_KEY] = (
            payload,
            {"x-amz-meta-sha256": hashlib.sha256(payload).hexdigest()},
            '"e1"',
        )

    def _transport(method: str, url: str, *, headers: Dict[str, str], data: bytes = b""):
        key = urlparse(url).path.lstrip("/").split("/", 1)[1]
        if method == "GET" and key in objs:
            p, m, e = objs[key]
            return 200, {**m, "ETag": e}, p
        return 404, {}, b""

    store = R2CloudStateStore(_r2_cfg(), transport=_transport)

    # 1. Huge PAX extended header exceeding max_extracted_bytes -> _BoundedReadStream fails closed
    buf_pax = io.BytesIO()
    with tarfile.open(fileobj=buf_pax, mode="w:gz", format=tarfile.PAX_FORMAT) as tf:
        ti = tarfile.TarInfo("data/MASTER.csv")
        ti.pax_headers = {"comment": "P" * 32768}
        ti.size = 2
        tf.addfile(ti, io.BytesIO(b"a\n"))
    _put(buf_pax.getvalue())
    with pytest.raises(ValueError, match="R2_BUNDLE_EXTRACTED_SIZE_EXCEEDED"):
        store.pull_instance_bundle(tmp_path / "out_pax", max_extracted_bytes=4096)

    # 2. Directory member with non-zero size -> R2_BUNDLE_INVALID_DIR_SIZE
    buf_dir = io.BytesIO()
    with tarfile.open(fileobj=buf_dir, mode="w:gz") as tf:
        ti_dir = tarfile.TarInfo("data")
        ti_dir.type = tarfile.DIRTYPE
        ti_dir.size = 32
        tf.addfile(ti_dir, io.BytesIO(b"0" * 32))
    _put(buf_dir.getvalue())
    with pytest.raises(ValueError, match="R2_BUNDLE_INVALID_DIR_SIZE"):
        store.pull_instance_bundle(tmp_path / "out_dir")


def test_r2_3_archive_compactor_unanchored_v231_and_forged_release_status_fail_closed() -> None:
    # 1. History dir containing v2.3.1-1/ + loose file with NO release-state anchor -> fail closed (0 writes)
    d1 = _FakeDrive()
    d1.add_folder("hist_root", "90_archive_历史归档")
    v231_dir = d1.add_folder("dir_v231", "v2.3.1-1", ["hist_root"])
    d1.add_file("b_v231", "release_bundle.tar.gz", v231_dir, b"gz", "application/gzip")
    d1.add_file("loose_1", "before_old.json", "hist_root", b"{}", "application/json")
    with pytest.raises(ArchiveCompactionError, match="ARCHIVE_COMPACT_MISSING_RELEASE_STATE"):
        compact_drive_archive(d1, history_folder_id="hist_root", execute=True)
    assert d1.upload_calls == 0
    assert d1.trashed_ids == []

    # 2. Forged release_status (unknown release_id) -> ARCHIVE_COMPACT_INVALID_RELEASE_STATE (0 writes)
    with pytest.raises(ArchiveCompactionError, match="ARCHIVE_COMPACT_INVALID_RELEASE_STATE"):
        compact_drive_archive(
            d1,
            history_folder_id="hist_root",
            release_status={"release_id": "v2.9.9-1", "status": "COMPLETE"},
            execute=True,
        )
    assert d1.upload_calls == 0

    # 3. release_status missing status == COMPLETE -> ARCHIVE_COMPACT_INVALID_RELEASE_STATE (0 writes)
    with pytest.raises(ArchiveCompactionError, match="ARCHIVE_COMPACT_INVALID_RELEASE_STATE"):
        compact_drive_archive(
            d1,
            history_folder_id="hist_root",
            release_status={"release_id": "v2.3.1-1"},
            execute=True,
        )
    assert d1.upload_calls == 0


def test_r2_4_foreign_table_malformed_continuation_and_short_columns_fail_closed() -> None:
    # 1. Empty first col + non-Latin col 1 -> FOREIGN_ROW_MALFORMED_CONTINUATION
    bad_cont = "\n".join([
        "| 俱乐部 | 外籍球员 |  | 国籍 | 位置 | 球衣号 |",
        "| --- | --- | --- | --- | --- | --- |",
        "| 合成首钢 | SYNTH ONE | 外援一 | 合成国 | PG | 1 |",
        "|  | 合成外援二 | 美国 | PG | 7 |  |",
    ])
    with pytest.raises(ValueError, match="FOREIGN_ROW_MALFORMED_CONTINUATION"):
        _parse_foreign_registration_rows({"body": bad_cont})

    # 2. <5 column data row inside foreign table -> FOREIGN_ROW_MALFORMED_COLUMNS
    bad_cols = "\n".join([
        "| 俱乐部 | 外籍球员 |  | 国籍 | 位置 | 球衣号 |",
        "| --- | --- | --- | --- | --- | --- |",
        "| 合成首钢 | SYNTH ONE | 外援一 | 合成国 | PG | 1 |",
        "| 合成首钢 | 缺列行 | PG |",
    ])
    with pytest.raises(ValueError, match="FOREIGN_ROW_MALFORMED_COLUMNS"):
        _parse_foreign_registration_rows({"body": bad_cols})


def test_r2_5_p0_2_legacy_minimal_registration_profile_byte_identical_golden() -> None:
    from cba_kb import kc_render

    minimal_payload = {
        "metric": "points_per_game",
        "grains": {"STATS": {"posture": "UNKNOWN", "season": "2026-2027", "match_type": "-", "scope": "-"}},
        "evidence_transparency": {
            "posture": "UNKNOWN",
            "unknown_subkind": "NO_SEASON_STATS_RECORDED_YET",
            "scope_meta": {"player_name": "合成兼容甲", "player_id": "990009", "season": "2026-2027"},
        },
        "registration_profile": {
            "player_name": "合成兼容甲",
            "current_club": "合成俱乐部甲",
            "registration_type": "DOMESTIC",
        },
    }
    expected_golden = (
        "# Research Card (Serving Projection)\n\n"
        "## Statistical Evidence\n"
        "- Posture: UNKNOWN\n"
        "- Season: 2026-2027\n"
        "- Match Type: -\n"
        "- Scope: -\n"
        "- Unknown: NO_SEASON_STATS_RECORDED_YET\n\n"
        "## Registration & Identity Profile\n"
        "- Player Name: 合成兼容甲\n"
        "- Current Club: 合成俱乐部甲\n"
        "- Registration Type: DOMESTIC\n\n"
        "end_marker: true\n"
    ).encode("utf-8")

    rendered = kc_render.render_research_card(minimal_payload, evidence_verified=True)
    assert rendered.encode("utf-8") == expected_golden


def test_r2_6_p0_4_twenty_club_sixty_player_foreign_quick_index_completeness_under_25kb() -> None:
    table_lines = [
        "| 俱乐部 | 外籍球员 |  | 国籍 | 位置 | 球衣号 |",
        "| --- | --- | --- | --- | --- | --- |",
        "| 英文名 | 中文名 |  |  |  |  |",
    ]
    expected_clubs: List[str] = []
    expected_players: List[str] = []
    for c_idx in range(1, 21):
        club = f"合成队{c_idx:02d}"
        expected_clubs.append(club)
        for p_idx, suffix in ((1, "甲"), (2, "乙"), (3, "丙")):
            en = f"SYNTH P{c_idx:02d}{p_idx}"
            zh = f"外援{c_idx:02d}{suffix}"
            expected_players.append(zh)
            jersey = c_idx + (p_idx - 1) * 20
            if p_idx == 1:
                table_lines.append(f"| {club} | {en} | {zh} | 合成国 | PG | {jersey} |")
            else:
                table_lines.append(f"| {en} | {zh} | 合成国 | SG | {jersey} |  |")

    digest_bytes = render_collector_official_digest_md(
        bundles=[
            {
                "doc_id": "doc_foreign_20x3_001",
                "title": "2026-2027赛季CBA联赛外籍球员注册信息",
                "source_tier": "T1",
                "event_class": "foreign_registration",
                "published_at": "2026-11-24",
                "raw_sha256": "d" * 64,
                "body": "\n".join(table_lines),
            }
        ],
        release_id="v2.3.1-1",
        engine_sha="7afb18b1e867146ed99cc73fa16e53d990c54e95",
        compact_curated=True,
        include_foreign_quick_index=True,
    )
    assert len(digest_bytes) < COLLECTOR_DIGEST_MAX_BYTES
    text = digest_bytes.decode("utf-8")
    for club in expected_clubs:
        assert f"{club}（3人）" in text
    for zh in expected_players:
        assert zh in text

