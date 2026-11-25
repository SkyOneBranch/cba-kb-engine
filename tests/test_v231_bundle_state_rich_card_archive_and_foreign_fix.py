"""Frozen red-light acceptance tests for REQ-231 (cba-kb-engine v2.3.1).

Covers P0-1 through P0-5 with 100% offline synthetic fixtures (G5 == 0, G6 == 0):
- P0-1: Single-bundle cloud state (cloud_instance_bundle.tar.gz), substrate allowlist,
        deterministic tar.gz + x-amz-meta-sha256, clean() & tar-bomb/traversal guards,
        cache-first --lazy-pull-bundle, R2_ENDPOINT fallback, opt-in S3 501 lock fallback.
- P0-2: Club-named consumer card filenames (zero `unassigned`), rich domestic contract
        & 3-season cross-season history, salary non-disclosure, full foreign registration
        block, single-game gap/degraded disclaimer, minimal registration_profile compat.
- P0-3: Controlled 90_archive cold compactor (plan-only default, active overlap Fail-Closed,
        deterministic .tar.gz + archive_index.json + remote sha256Checksum verify before
        trash-only, never delete()) and legacy _unassigned_ snowball card auto-retirement.
- P0-4: Foreign registration rowspan & secondary-header parsing with orphan Fail-Closed,
        <25 KB v2.3.1 digest compact 20-club foreign quick index, and homonym-safe
        multi-person matched_persons enrichment across title + body.
- P0-5: V215_RELEASE_SPECS["v2.3.1-1"] registration bound to baseline 7afb18b1 and zero
        regression on v2.1.5-1..v2.3.0-1.

Lazy imports inside test bodies guarantee clean pytest collection and differential
falsification on baseline 7afb18b1e867146ed99cc73fa16e53d990c54e95.
"""
from __future__ import annotations

import hashlib
import inspect
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_SHA_V231_1 = "7afb18b1e867146ed99cc73fa16e53d990c54e95"


def _cli_env() -> Dict[str, str]:
    env = dict(os.environ)
    src_path = str(REPO_ROOT / "src")
    env["PYTHONPATH"] = (
        src_path + os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else src_path
    )
    return env


class _FakeS3Transport:
    """In-memory S3/R2/B2 transport supporting conditional PUT and optional HTTP 501."""

    def __init__(self, *, reject_conditional_put_501: bool = False) -> None:
        self.objects: Dict[str, Tuple[bytes, Dict[str, str], str]] = {}
        self.requests_log: List[Dict[str, Any]] = []
        self.reject_conditional_put_501 = reject_conditional_put_501

    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: Dict[str, str],
        data: bytes = b"",
    ) -> Tuple[int, Dict[str, str], bytes]:
        parsed = urlparse(url)
        parts = parsed.path.lstrip("/").split("/", 1)
        key = parts[1] if len(parts) > 1 else ""
        qs = parse_qs(parsed.query)
        lower_h = {k.lower(): v for k, v in headers.items()}
        self.requests_log.append({
            "method": method,
            "key": key,
            "has_if_none_match": "if-none-match" in lower_h,
            "has_if_match": "if-match" in lower_h,
            "meta_sha256": lower_h.get("x-amz-meta-sha256"),
        })

        if method == "PUT":
            if self.reject_conditional_put_501 and (
                "if-none-match" in lower_h or "if-match" in lower_h
            ):
                return 501, {}, b"NotImplemented"
            if lower_h.get("if-none-match") == "*" and key in self.objects:
                return 412, {}, b"PreconditionFailed"
            if "if-match" in lower_h:
                cur = self.objects.get(key)
                if cur is None or cur[2] != lower_h["if-match"]:
                    return 412, {}, b"PreconditionFailed"
            meta = {
                k: v for k, v in lower_h.items() if k.startswith("x-amz-meta-")
            }
            etag = f'"{hashlib.md5(data).hexdigest()}"'
            self.objects[key] = (bytes(data), meta, etag)
            return 200, {"ETag": etag}, b""

        if method == "GET" and qs.get("list-type") == ["2"]:
            prefix = qs.get("prefix", [""])[0]
            matched = sorted(k for k in self.objects if k.startswith(prefix))
            items_xml = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in matched)
            xml_body = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                f"<ListBucketResult>{items_xml}</ListBucketResult>"
            ).encode("utf-8")
            return 200, {"content-type": "application/xml"}, xml_body

        if method == "GET":
            if key not in self.objects:
                return 404, {}, b"NotFound"
            payload, meta, etag = self.objects[key]
            resp_h = dict(meta)
            resp_h["ETag"] = etag
            return 200, resp_h, payload

        if method == "DELETE":
            self.objects.pop(key, None)
            return 204, {}, b""

        return 405, {}, b"MethodNotAllowed"


class _FakeArchiveDrive:
    """In-memory Drive fake supporting nested folders, sha256Checksum, trash, and delete guard."""

    FOLDER_MIME = "application/vnd.google-apps.folder"

    def __init__(self, *, corrupt_upload_sha256: bool = False) -> None:
        self._main_thread_id = threading.get_ident()
        self.files: Dict[str, Dict[str, Any]] = {}
        self.contents: Dict[str, bytes] = {}
        self.trashed_ids: List[str] = []
        self.delete_called = False
        self.upload_calls = 0
        self.put_calls = 0
        self.corrupt_upload_sha256 = corrupt_upload_sha256
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
        modified_time: str = "2026-11-20T00:00:00Z",
    ) -> str:
        sha = hashlib.sha256(content).hexdigest()
        self.files[file_id] = {
            "id": file_id,
            "name": name,
            "mimeType": mime,
            "parents": [parent],
            "size": str(len(content)),
            "sha256Checksum": sha,
            "trashed": False,
            "modifiedTime": modified_time,
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
        if file_id not in self.files:
            raise KeyError(file_id)
        return dict(self.files[file_id])

    def get(self, file_id: str) -> bytes:
        return self.contents[file_id]

    def get_with_meta(self, file_id: str, *, known_meta=None) -> Tuple[bytes, Dict[str, Any]]:
        return self.contents[file_id], self.meta(file_id)

    def get_parents(self, file_id: str) -> List[str]:
        return list(self.files.get(file_id, {}).get("parents") or [])

    def put(self, file_id: str, content: bytes, mime: str) -> Dict[str, Any]:
        self.put_calls += 1
        self.contents[file_id] = bytes(content)
        self.files[file_id]["mimeType"] = mime
        self.files[file_id]["size"] = str(len(content))
        self.files[file_id]["sha256Checksum"] = hashlib.sha256(content).hexdigest()
        return dict(self.files[file_id])

    def create_binary(self, parent: str, name: str, content: bytes, mime: str) -> Dict[str, Any]:
        self._counter += 1
        fid = f"drv_synth_obj_{self._counter:04d}"
        self.upload_calls += 1
        sha = (
            "0" * 64
            if self.corrupt_upload_sha256
            else hashlib.sha256(content).hexdigest()
        )
        meta = {
            "id": fid,
            "name": name,
            "mimeType": mime,
            "parents": [parent],
            "size": str(len(content)),
            "sha256Checksum": sha,
            "trashed": False,
            "modifiedTime": "2026-11-25T00:00:00Z",
        }
        self.files[fid] = meta
        self.contents[fid] = bytes(content)
        return dict(meta)

    def trash(self, file_id: str) -> Dict[str, Any]:
        if file_id not in self.files:
            raise KeyError(file_id)
        self.files[file_id]["trashed"] = True
        self.trashed_ids.append(file_id)
        return dict(self.files[file_id])

    def delete(self, file_id: str) -> None:
        self.delete_called = True
        raise AssertionError("HARD_DELETE_FORBIDDEN: archive compactor/snowball must never call delete()")


def _make_r2_config(prefix: str = "") -> Any:
    from cba_kb.cloud_state import R2Config

    return R2Config(
        endpoint_url="https://synth-r2.example.invalid",
        bucket="synth-bucket",
        access_key_id="SYNTH_ACCESS_KEY",
        secret_access_key="SYNTH_SECRET_KEY",
        region="auto",
        prefix=prefix,
    )


def _populate_synthetic_instance_dir(root: Path) -> None:
    """Create both allowlisted substrate files and excluded raw/secret files."""
    from cba_kb.consumer_surface import (
        CONSUMER_MASTER_FILENAME,
        DOMESTIC_REG_DIFF_CSV_FILENAME,
        FOREIGN_REGISTRATION_CSV_FILENAME,
    )

    allowlisted_files = {
        f"data/{CONSUMER_MASTER_FILENAME}": "season,club_official,player\n2026-2027,合成俱乐部甲,合成球员甲\n",
        "data/MASTER.csv": "season,club_official,player\n2026-2027,合成俱乐部甲,合成球员甲\n",
        f"data/ops_sync_v041/{FOREIGN_REGISTRATION_CSV_FILENAME}": "club,player_en,player_zh\n合成俱乐部甲,SYNTH ONE,合成外援一\n",
        f"data/ops_sync_v041/{DOMESTIC_REG_DIFF_CSV_FILENAME}": "change_type,player\nADDED,合成球员甲\n",
        "data/season_stats.json": json.dumps([{"player_name": "合成球员甲", "season": "2026-2027"}], ensure_ascii=False),
        "data/game_logs/synth_g1.jsonl": json.dumps({"game_id": "G1", "player_name": "合成球员甲"}, ensure_ascii=False) + "\n",
        "data/player_identity/identity_registry.json": json.dumps({"schema_version": 1, "players": []}, ensure_ascii=False),
        "data/ops_sync_v041/operational_checkpoint.json": json.dumps({"updated_at": "2026-11-25T00:00:00Z"}, ensure_ascii=False),
        "data/coverage_ledger.json": json.dumps({"domains": {}}, ensure_ascii=False),
        "data/negative_receipts.jsonl": "",
        "data/snowball_receipts.jsonl": "",
        "data/on_demand_cards/on_demand_cards_manifest.json": json.dumps({"schema_version": 1, "cards": {}}, ensure_ascii=False),
        "config/curation_profile.yaml": "compact_announcement_excerpt_chars: 180\n",
        "data/cards/研究卡_合成球员甲_合成俱乐部甲_2026-2027_CURRENT.md": "# Card\n\nend_marker: true\n",
    }
    excluded_files = {
        "data/document_lane/archive/doc_001/raw/original.html": "<html>huge raw body</html>",
        "evidence/audit_evidence.md": "evidence note",
        "inbox/pending_upload.csv": "a,b\n1,2\n",
        ".credentials/token.json": '{"kind": "synthetic-cred-placeholder"}',
        "config/production.json": '{"root_folder_id": "synth_root_excluded"}',
    }
    for rel, text in {**allowlisted_files, **excluded_files}.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


# ===========================================================================
# P0-1: Single-Bundle Cloud State, Cache-First Lazy Pull & S3/B2 Compatibility
# ===========================================================================


def test_p0_1_push_and_pull_instance_bundle_allowlist_determinism_and_sha256(tmp_path: Path):
    from cba_kb.cloud_state import DEFAULT_INSTANCE_BUNDLE_KEY, R2CloudStateStore

    assert DEFAULT_INSTANCE_BUNDLE_KEY == "cloud_instance/cloud_instance_bundle.tar.gz"

    src_dir = tmp_path / "inst_src"
    _populate_synthetic_instance_dir(src_dir)

    transport = _FakeS3Transport()
    store = R2CloudStateStore(_make_r2_config(), transport=transport)

    res1 = store.push_instance_bundle(src_dir)
    put_reqs = [r for r in transport.requests_log if r["method"] == "PUT"]
    assert len(put_reqs) == 1, f"Expected exactly 1 PUT for bundle push, got {len(put_reqs)}"
    assert put_reqs[0]["key"] == DEFAULT_INSTANCE_BUNDLE_KEY
    assert put_reqs[0]["meta_sha256"] == res1["sha256"]

    # Touch mtime on a file and push again -> output bytes MUST be byte-identical
    master_file = src_dir / "data" / "MASTER.csv"
    os.utime(master_file, (1700000123, 1700000123))
    first_bytes = transport.objects[DEFAULT_INSTANCE_BUNDLE_KEY][0]
    res2 = store.push_instance_bundle(src_dir)
    second_bytes = transport.objects[DEFAULT_INSTANCE_BUNDLE_KEY][0]
    assert first_bytes == second_bytes, "Bundle tar.gz must be byte-deterministic"
    assert res1["sha256"] == res2["sha256"]

    # Inspect tar members: allowlisted present, excluded strictly absent
    with tarfile.open(fileobj=io.BytesIO(first_bytes), mode="r:gz") as tf:
        names = tf.getnames()
    assert any(n.endswith("MASTER.csv") for n in names)
    assert any(n.endswith("identity_registry.json") for n in names)
    assert any(n.endswith("coverage_ledger.json") for n in names)
    assert any(n.endswith("operational_checkpoint.json") for n in names)
    assert any(n.endswith("curation_profile.yaml") for n in names)
    assert any(n.endswith("研究卡_合成球员甲_合成俱乐部甲_2026-2027_CURRENT.md") for n in names)
    assert not any("document_lane/archive/" in n and "/raw/" in n for n in names)
    assert not any(n.startswith("evidence/") or "/evidence/" in n for n in names)
    assert not any(n.startswith("inbox/") or "/inbox/" in n for n in names)
    assert not any(".credentials/" in n for n in names)
    assert not any(n.endswith("production.json") for n in names)

    # Pull bundle in exactly 1 GET into clean destination
    transport.requests_log.clear()
    dst_dir = tmp_path / "inst_dst"
    pull_res = store.pull_instance_bundle(dst_dir)
    get_reqs = [r for r in transport.requests_log if r["method"] == "GET"]
    assert len(get_reqs) == 1
    assert pull_res["sha256"] == res1["sha256"]
    assert (dst_dir / "data" / "MASTER.csv").is_file()
    assert not (dst_dir / "config" / "production.json").exists()
    assert not (dst_dir / ".credentials" / "token.json").exists()


def test_p0_1_push_bundle_clean_rejects_secrets_and_pull_rejects_unsafe_tar_and_bombs(tmp_path: Path):
    from cba_kb.cloud_state import (
        CloudStateIntegrityError,
        DEFAULT_INSTANCE_BUNDLE_KEY,
        R2CloudStateStore,
    )

    # 1. clean() rejects private key / secret marker inside allowlisted file
    secret_dir = tmp_path / "secret_inst"
    (secret_dir / "data").mkdir(parents=True)
    fake_secret_marker = "-----BEGIN " + "PRIVATE KEY-----\nSYNTHETIC\n"
    (secret_dir / "data" / "MASTER.csv").write_text(fake_secret_marker, encoding="utf-8")

    transport = _FakeS3Transport()
    store = R2CloudStateStore(_make_r2_config(), transport=transport)
    with pytest.raises(Exception):
        store.push_instance_bundle(secret_dir)
    assert DEFAULT_INSTANCE_BUNDLE_KEY not in transport.objects

    def _put_raw_tar(tar_bytes: bytes, *, corrupt_meta: bool = False) -> None:
        sha = "0" * 64 if corrupt_meta else hashlib.sha256(tar_bytes).hexdigest()
        etag = f'"{hashlib.md5(tar_bytes).hexdigest()}"'
        transport.objects[DEFAULT_INSTANCE_BUNDLE_KEY] = (
            tar_bytes,
            {"x-amz-meta-sha256": sha},
            etag,
        )

    def _build_custom_tar(members: List[Tuple[tarfile.TarInfo, bytes]]) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for ti, payload in members:
                if ti.isfile():
                    ti.size = len(payload)
                    tf.addfile(ti, io.BytesIO(payload))
                else:
                    tf.addfile(ti)
        return buf.getvalue()

    # 2. Corrupted x-amz-meta-sha256 -> CloudStateIntegrityError
    ti_ok = tarfile.TarInfo("data/MASTER.csv")
    _put_raw_tar(_build_custom_tar([(ti_ok, b"ok\n")]), corrupt_meta=True)
    with pytest.raises(CloudStateIntegrityError):
        store.pull_instance_bundle(tmp_path / "out_sha")

    # 3. Path traversal `..`
    ti_trav = tarfile.TarInfo("../escape.txt")
    _put_raw_tar(_build_custom_tar([(ti_trav, b"evil")]))
    with pytest.raises(ValueError, match="TRAVERSAL|ESCAPE"):
        store.pull_instance_bundle(tmp_path / "out_trav")

    # 4. Absolute path
    ti_abs = tarfile.TarInfo("/tmp/abs_escape.txt")
    _put_raw_tar(_build_custom_tar([(ti_abs, b"evil")]))
    with pytest.raises(ValueError, match="TRAVERSAL|ESCAPE"):
        store.pull_instance_bundle(tmp_path / "out_abs")

    # 5. Symlink member
    ti_sym = tarfile.TarInfo("data/link.txt")
    ti_sym.type = tarfile.SYMTYPE
    ti_sym.linkname = "/etc/passwd"
    _put_raw_tar(_build_custom_tar([(ti_sym, b"")]))
    with pytest.raises(ValueError, match="UNSAFE_MEMBER|SYMLINK|LINK"):
        store.pull_instance_bundle(tmp_path / "out_sym")

    # 6. Hardlink member
    ti_hard = tarfile.TarInfo("data/hard.txt")
    ti_hard.type = tarfile.LNKTYPE
    ti_hard.linkname = "data/MASTER.csv"
    _put_raw_tar(_build_custom_tar([(ti_hard, b"")]))
    with pytest.raises(ValueError, match="UNSAFE_MEMBER|HARDLINK|LINK"):
        store.pull_instance_bundle(tmp_path / "out_hard")

    # 7. Bomb limits: max_compressed_bytes, max_extracted_bytes, max_members
    big_payload = b"A" * 4096
    ti_big = tarfile.TarInfo("data/MASTER.csv")
    _put_raw_tar(_build_custom_tar([(ti_big, big_payload)]))
    with pytest.raises(ValueError, match="COMPRESSED_SIZE"):
        store.pull_instance_bundle(tmp_path / "out_comp", max_compressed_bytes=10)
    with pytest.raises(ValueError, match="EXTRACTED_SIZE"):
        store.pull_instance_bundle(tmp_path / "out_ext", max_extracted_bytes=100)

    many_members = [(tarfile.TarInfo(f"data/cards/c_{i}.md"), b"x") for i in range(5)]
    _put_raw_tar(_build_custom_tar(many_members))
    with pytest.raises(ValueError, match="MAX_MEMBERS"):
        store.pull_instance_bundle(tmp_path / "out_mem", max_members=2)


def test_p0_1_snowball_lazy_pull_bundle_cache_hit_zero_pull_and_cold_path_budget(tmp_path: Path):
    from cba_kb.cloud_state import DEFAULT_INSTANCE_BUNDLE_KEY, R2CloudStateStore
    from cba_kb.snowball_orchestrator import AcquisitionBackend, SnowballOrchestrator

    src_dir = tmp_path / "lazy_src"
    _populate_synthetic_instance_dir(src_dir)
    dst_dir = tmp_path / "lazy_dst"

    transport = _FakeS3Transport()
    store = R2CloudStateStore(_make_r2_config(), transport=transport)
    store.push_instance_bundle(src_dir)

    now = datetime(2026, 11, 25, 10, 0, 0, tzinfo=timezone.utc)
    fresh_md = "# 人物研究卡｜合成球员甲｜2026-2027｜CURRENT\n\nend_marker: true\n"
    store.put_card(
        "2026-2027",
        "合成球员甲",
        {
            "player_name": "合成球员甲",
            "season": "2026-2027",
            "filename": "研究卡_合成球员甲_合成俱乐部甲_2026-2027_CURRENT.md",
            "markdown": fresh_md,
            "updated_at_utc": "2026-11-25T09:30:00Z",
        },
    )

    pull_calls: List[int] = []
    push_calls: List[int] = []

    def _ensure_local() -> Path:
        pull_calls.append(1)
        store.pull_instance_bundle(dst_dir)
        return dst_dir

    def _on_mutated(_res: Dict[str, Any]) -> None:
        push_calls.append(1)
        store.push_instance_bundle(dst_dir)

    class _StubBackend(AcquisitionBackend):
        def acquire_person(self, person: str, *, season: str, job_id: str, force_refresh: bool = False) -> Dict[str, Any]:
            md = f"# 人物研究卡｜{person}｜{season}｜CURRENT\n\nend_marker: true\n"
            return {
                "status": "ACQUIRED_AND_BUILT",
                "person": {"player_name": person},
                "filename": f"研究卡_{person}_合成俱乐部乙_{season}_CURRENT.md",
                "card_markdown": md,
                "card_sha256": hashlib.sha256(md.encode("utf-8")).hexdigest(),
                "observed_until": "2026-11-25T09:00:00Z",
                "coverage_status": "COMPLETE",
            }

    orch = SnowballOrchestrator(
        state_store=store,
        acquisition_backend=_StubBackend(),
        ensure_local_instance_fn=_ensure_local,
        on_state_mutated_fn=_on_mutated,
        now_fn=lambda: now,
    )

    # 1. Fast path cache hit -> 0 bundle pulls!
    transport.requests_log.clear()
    hit_res = orch.get_or_acquire_person("合成球员甲", season="2026-2027")
    assert hit_res["status"] == "HIT_FRESH"
    assert len(pull_calls) == 0
    assert len(push_calls) == 0
    assert not any(r["key"] == DEFAULT_INSTANCE_BUNDLE_KEY for r in transport.requests_log)

    # 2. Cold path cache miss -> lazy pull once + push once; non-lock/job object-store requests <= 5
    transport.requests_log.clear()
    cold_res = orch.get_or_acquire_person("合成球员乙", season="2026-2027")
    assert cold_res["status"] == "ACQUIRED_AND_BUILT"
    assert len(pull_calls) == 1
    assert len(push_calls) == 1
    bundle_gets = [r for r in transport.requests_log if r["method"] == "GET" and r["key"] == DEFAULT_INSTANCE_BUNDLE_KEY]
    bundle_puts = [r for r in transport.requests_log if r["method"] == "PUT" and r["key"] == DEFAULT_INSTANCE_BUNDLE_KEY]
    assert len(bundle_gets) == 1
    assert len(bundle_puts) == 1
    # Core data/state object-store requests (cards + negative_receipts + bundle) <= 5
    core_reqs = [
        r for r in transport.requests_log
        if not r["key"].startswith(("locks/", "jobs/"))
    ]
    assert len(core_reqs) <= 5, f"Cold path core requests exceeded 5: {core_reqs}"

    # 3. CLI --lazy-pull-bundle flag & example workflow template
    help_proc = subprocess.run(
        [sys.executable, "-m", "cba_kb.cli", "snowball-query", "--help"],
        capture_output=True,
        text=True,
        check=False,
        env=_cli_env(),
    )
    assert help_proc.returncode == 0
    assert "--lazy-pull-bundle" in help_proc.stdout
    example_yml = (REPO_ROOT / "config" / "cloud_snowball_runner.example.yml").read_text(encoding="utf-8")
    assert "--lazy-pull-bundle" in example_yml


def test_p0_1_r2_endpoint_fallback_and_s3_501_lock_fallback_fail_closed_by_default(monkeypatch: pytest.MonkeyPatch):
    from cba_kb.cloud_state import R2CloudStateStore, R2Config

    # 1. R2_ENDPOINT env fallback
    cfg = R2Config.from_env({
        "R2_ENDPOINT": "https://b2-synth.example.invalid",
        "R2_BUCKET": "synth-b2-bucket",
        "R2_ACCESS_KEY_ID": "SYNTH_KEY",
        "R2_SECRET_ACCESS_KEY": "SYNTH_SECRET",
    })
    assert cfg.endpoint_url == "https://b2-synth.example.invalid"

    # 2. Default allow_s3_501_lock_fallback is False -> strict Fail-Closed on HTTP 501
    monkeypatch.delenv("CBA_KB_S3_LOCK_501_FALLBACK", raising=False)
    transport_501 = _FakeS3Transport(reject_conditional_put_501=True)
    strict_store = R2CloudStateStore(_make_r2_config(), transport=transport_501)
    assert strict_store.allow_s3_501_lock_fallback is False
    with pytest.raises(RuntimeError, match="501"):
        strict_store.acquire_active_job_lock("2026-2027", "合成球员甲", job_id="job_1", owner_id="owner_1")

    # 3. Explicit allow_s3_501_lock_fallback=True -> safe fallback with x-amz-meta-sha256
    fallback_store = R2CloudStateStore(
        _make_r2_config(),
        transport=transport_501,
        allow_s3_501_lock_fallback=True,
    )
    acquired, doc = fallback_store.acquire_active_job_lock(
        "2026-2027", "合成球员甲", job_id="job_1", owner_id="owner_1"
    )
    assert acquired is True
    assert doc["job_id"] == "job_1"
    released = fallback_store.release_active_job_lock(
        "2026-2027", "合成球员甲", owner_id="owner_1", job_id="job_1"
    )
    assert released is True

    # 4. Env var CBA_KB_S3_LOCK_501_FALLBACK=1 activates fallback
    monkeypatch.setenv("CBA_KB_S3_LOCK_501_FALLBACK", "1")
    env_store = R2CloudStateStore(_make_r2_config(), transport=_FakeS3Transport(reject_conditional_put_501=True))
    assert env_store.allow_s3_501_lock_fallback is True
    ok, _ = env_store.acquire_active_job_lock("2026-2027", "合成球员乙", job_id="job_2", owner_id="owner_2")
    assert ok is True


# ===========================================================================
# P0-2: Contract-Rich Person Cards, Foreign Registration Block & Zero unassigned
# ===========================================================================


def test_p0_2_person_card_club_filename_zero_unassigned_and_kc_render_signature_compat(tmp_path: Path):
    from cba_kb import kc_render
    from cba_kb.person_card_builder import build_person_card

    # 1. kc_render.research_card_consumer_filename signature MUST stay unchanged
    sig = inspect.signature(kc_render.research_card_consumer_filename)
    assert list(sig.parameters.keys()) == ["player_name", "player_id", "season"]
    assert kc_render.research_card_consumer_filename("合成球员甲", "990001", "2026-2027") == (
        "研究卡_合成球员甲_990001_2026-2027_CURRENT.md"
    )

    # 2. Player with club and NO player_id -> filename uses club name, zero `unassigned`
    master_rows = [
        {
            "season": "2026-2027",
            "club_official": "合成首钢俱乐部",
            "player": "合成后卫甲",
            "registration_stage": "常规注册",
            "registration_method": "延续",
            "contract_category": "D类",
            "contract_term_official": "2028-07-31",
            "registration_status": "已注册",
        }
    ]
    res = build_person_card("合成后卫甲", season="2026-2027", master_rows=master_rows, output_dir=tmp_path)
    assert res["status"] == "BUILT"
    assert res["filename"] == "研究卡_合成后卫甲_合成首钢俱乐部_2026-2027_CURRENT.md"
    assert "unassigned" not in res["filename"].lower()

    # 3. Player with NO club and NO player_id/player_uid -> fallback NEVER contains `unassigned`
    no_club_rows = [{"season": "2026-2027", "club_official": "", "player": "合成自由人乙"}]
    res_no_club = build_person_card("合成自由人乙", season="2026-2027", master_rows=no_club_rows)
    assert res_no_club["status"] == "BUILT"
    assert "unassigned" not in str(res_no_club["filename"]).lower()
    assert res_no_club["filename"].startswith("研究卡_合成自由人乙_")
    assert res_no_club["filename"].endswith("_2026-2027_CURRENT.md")


def test_p0_2_domestic_card_contract_details_salary_nondisclosure_and_3_season_history():
    from cba_kb.person_card_builder import build_person_card

    master_rows = [
        {
            "season": "2023-2024",
            "club_official": "合成老东家",
            "player": "合成前锋丙",
            "registration_stage": "常规注册",
            "registration_method": "自行培养",
            "contract_category": "A1类",
            "contract_term_official": "2024-07-31",
            "registration_status": "已注册",
        },
        {
            "season": "2024-2025",
            "club_official": "合成俱乐部甲",
            "player": "合成前锋丙",
            "registration_stage": "常规注册",
            "registration_method": "转会",
            "contract_category": "B类",
            "contract_term_official": "2025-07-31",
            "former_club": "合成老东家",
            "registration_status": "已注册",
        },
        {
            "season": "2025-2026",
            "club_official": "合成俱乐部甲",
            "player": "合成前锋丙",
            "registration_stage": "常规注册",
            "registration_method": "延续",
            "contract_category": "D类",
            "contract_term_official": "2027-07-31",
            "former_club": "合成俱乐部甲",
            "registration_status": "已注册",
        },
        {
            "season": "2026-2027",
            "club_official": "合成俱乐部乙",
            "player": "合成前锋丙",
            "registration_stage": "国内球员注册",
            "registration_method": "认领",
            "contract_category": "E类",
            "contract_term_official": "2028-07-31",
            "former_club": "合成俱乐部甲",
            "registration_status": "已注册",
            "remarks": "老将合同示例备注",
        },
    ]
    res = build_person_card("合成前锋丙", season="2026-2027", master_rows=master_rows)
    assert res["status"] == "BUILT"
    md = res["markdown"]

    assert "合成俱乐部乙" in md
    assert "国内球员注册" in md
    assert "认领" in md
    assert "E类" in md
    assert "2028-07-31" in md
    assert "合成俱乐部甲" in md
    assert "已注册" in md
    assert "老将合同示例备注" in md
    assert "官方仅公示合同类别与期限，个人实领薪资未公开 (Unknown != 0)" in md
    assert "Cross-Season Registration History" in md
    # Up to 3 most recent seasons descending: 2026-2027, 2025-2026, 2024-2025 (2023-2024 omitted)
    hist_section = md.split("Cross-Season Registration History", 1)[1]
    assert "2026-2027" in hist_section
    assert "2025-2026" in hist_section
    assert "2024-2025" in hist_section
    assert "2023-2024" not in hist_section
    assert hist_section.index("2026-2027") < hist_section.index("2025-2026") < hist_section.index("2024-2025")

    # Empty contract_category / contract_term_official -> explicit 未公示 (UNKNOWN)
    sparse_rows = [
        {
            "season": "2026-2027",
            "club_official": "合成俱乐部丙",
            "player": "合成预注册丁",
            "registration_stage": "境外预注册",
            "registration_method": "预注册",
            "contract_category": "",
            "contract_term_official": "",
            "registration_status": "预注册",
        }
    ]
    res_sparse = build_person_card("合成预注册丁", season="2026-2027", master_rows=sparse_rows)
    assert "未公示 (UNKNOWN)" in res_sparse["markdown"]


def test_p0_2_foreign_card_full_registration_block_and_game_stats_gap_and_backward_compat():
    from cba_kb import kc_render
    from cba_kb.person_card_builder import build_person_card

    foreign_rows = [
        {
            "club": "合成汾酒俱乐部",
            "player_en": "SYNTHETIC GUARD",
            "player_zh": "合成外援甲",
            "nationality": "合成国",
            "position": "SG",
            "jersey_number": "6",
            "published_at": "2026-11-24T08:00:00Z",
            "doc_id": "doc_foreign_snap_001",
            "raw_sha256": "a" * 64,
        }
    ]
    res = build_person_card(
        "合成外援甲",
        season="2026-2027",
        foreign_rows=foreign_rows,
        game_stats_degraded=True,
    )
    assert res["status"] == "BUILT"
    assert res["filename"] == "研究卡_合成外援甲_合成汾酒俱乐部_2026-2027_CURRENT.md"
    md = res["markdown"]
    assert "合成汾酒俱乐部" in md
    assert "SYNTHETIC GUARD" in md
    assert "合成外援甲" in md
    assert "合成国" in md
    assert "SG" in md
    assert "6" in md
    assert "2026-11-24T08:00:00Z" in md
    assert "doc_foreign_snap_001" in md
    assert ("a" * 64) in md
    assert "Statement != Fact" in md
    assert "NOT_AVAILABLE_IN_CURRENT_SNAPSHOT" in md
    assert "降级" in md or "degraded" in md.lower()

    # Backward compatibility with minimal v2.2.3 registration_profile
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
    compat_md = kc_render.render_research_card(minimal_payload, evidence_verified=True)
    assert "## Registration & Identity Profile" in compat_md
    assert "Cross-Season Registration History" not in compat_md
    assert "Official Salary Disclosure" not in compat_md
    assert "NOT_AVAILABLE_IN_CURRENT_SNAPSHOT" not in compat_md


# ===========================================================================
# P0-3: Drive A 90_archive Cold Compactor & Legacy _unassigned_ Retirement
# ===========================================================================


def test_p0_3_archive_compactor_plan_only_and_active_overlap_fail_closed():
    from cba_kb.archive_compactor import ArchiveCompactionError, compact_drive_archive

    drive = _FakeArchiveDrive()
    drive.add_folder("hist_root", "90_archive_历史归档")
    # Legacy loose file and legacy release directory
    drive.add_file("legacy_loose_1", "before_old_status.json", "hist_root", b'{"old": 1}', "application/json")
    drive.add_folder("legacy_v220_dir", "v2.2.0-1", ["hist_root"])
    drive.add_file("legacy_v220_f1", "candidate_digest.md", "legacy_v220_dir", b"# old digest\n", "text/markdown")
    # Already compressed archive (must be skipped)
    drive.add_file("existing_cold_tgz", "cold_archive_old.tar.gz", "hist_root", b"gzbytes", "application/gzip")
    drive.add_file("existing_cold_zip", "cold_archive_old.zip", "hist_root", b"zipbytes", "application/zip")
    # Protected recent bundle_v1 directories (must be skipped)
    for rel_name in ("v2.2.1-1", "v2.2.2-1", "v2.3.0-1"):
        fid = drive.add_folder(f"dir_{rel_name}", rel_name, ["hist_root"])
        drive.add_file(f"bundle_{rel_name}", "release_bundle.tar.gz", fid, b"bundle", "application/gzip")

    # 1. Default plan-only mode -> 0 writes, skips compressed & recent bundle_v1 dirs
    plan = compact_drive_archive(
        drive,
        history_folder_id="hist_root",
        active_asset_ids=["active_master_001"],
        protected_folder_ids=["zone_current_001"],
    )
    assert plan["mode"] == "plan_only"
    assert drive.upload_calls == 0
    assert drive.trashed_ids == []
    assert drive.delete_called is False
    cand_ids = set(plan["candidate_ids"])
    assert "legacy_loose_1" in cand_ids
    assert "legacy_v220_f1" in cand_ids or "legacy_v220_dir" in cand_ids
    assert "existing_cold_tgz" not in cand_ids
    assert "existing_cold_zip" not in cand_ids
    for rel_name in ("v2.2.1-1", "v2.2.2-1", "v2.3.0-1"):
        assert f"dir_{rel_name}" not in cand_ids
        assert f"bundle_{rel_name}" not in cand_ids

    # 2. Active overlap -> Fail-Closed ARCHIVE_COMPACT_ACTIVE_OVERLAP with 0 writes
    with pytest.raises(ArchiveCompactionError, match="ARCHIVE_COMPACT_ACTIVE_OVERLAP"):
        compact_drive_archive(
            drive,
            history_folder_id="hist_root",
            active_asset_ids=["legacy_v220_f1"],
            execute=True,
        )
    assert drive.upload_calls == 0
    assert drive.trashed_ids == []

    # 3. CLI compact-drive-archive exists with --plan-only and --execute
    proc = subprocess.run(
        [sys.executable, "-m", "cba_kb.cli", "compact-drive-archive", "--help"],
        capture_output=True,
        text=True,
        check=False,
        env=_cli_env(),
    )
    assert proc.returncode == 0
    assert "--plan-only" in proc.stdout
    assert "--execute" in proc.stdout


def test_p0_3_archive_compactor_execute_deterministic_tar_sha256_verify_and_trash_only():
    from cba_kb.archive_compactor import ArchiveCompactionError, compact_drive_archive

    def _make_drive(*, corrupt: bool = False) -> _FakeArchiveDrive:
        d = _FakeArchiveDrive(corrupt_upload_sha256=corrupt)
        d.add_folder("hist_root", "90_archive_历史归档")
        d.add_file("loose_1", "before_manifest.csv", "hist_root", b"col\nval\n", "text/csv")
        d.add_folder("v215_dir", "v2.1.5-1", ["hist_root"])
        d.add_file("v215_f1", "candidate_index.json", "v215_dir", b'{"docs": []}\n', "application/json")
        return d

    # 1. Corrupt remote sha256Checksum -> abort BEFORE any trash
    bad_drive = _make_drive(corrupt=True)
    with pytest.raises(ArchiveCompactionError, match="SHA256_MISMATCH"):
        compact_drive_archive(bad_drive, history_folder_id="hist_root", execute=True, tag="v231test")
    assert bad_drive.trashed_ids == []
    assert bad_drive.delete_called is False

    # 2. Normal execute -> deterministic .tar.gz with archive_index.json, remote sha256 verified, trash-only
    d1 = _make_drive(corrupt=False)
    res1 = compact_drive_archive(d1, history_folder_id="hist_root", execute=True, tag="v231test")
    d2 = _make_drive(corrupt=False)
    res2 = compact_drive_archive(d2, history_folder_id="hist_root", execute=True, tag="v231test")

    assert d1.delete_called is False
    assert d1.upload_calls == 1
    assert len(res1["uploaded_bundles"]) == 1
    bundle_meta = res1["uploaded_bundles"][0]
    assert bundle_meta["name"].endswith(".tar.gz")
    assert bundle_meta["sha256"] == res2["uploaded_bundles"][0]["sha256"]

    bundle_bytes = d1.contents[bundle_meta["file_id"]]
    assert bundle_bytes == d2.contents[res2["uploaded_bundles"][0]["file_id"]]
    with tarfile.open(fileobj=io.BytesIO(bundle_bytes), mode="r:gz") as tf:
        member_names = tf.getnames()
        assert "archive_index.json" in member_names
        idx = json.loads(tf.extractfile("archive_index.json").read().decode("utf-8"))
        assert isinstance(idx, dict)
        assert idx.get("entries") or idx.get("files")

    # No plaintext archive_index.json left on Drive
    remaining_names = [m["name"] for m in d1.list("hist_root")]
    assert "archive_index.json" not in remaining_names
    assert "before_manifest.csv" not in remaining_names
    assert "v2.1.5-1" not in remaining_names
    assert len(d1.trashed_ids) >= 2


def test_p0_3_upsert_snowball_card_auto_trashes_legacy_unassigned_card():
    from cba_kb.drive import upsert_snowball_card_to_drive

    drive = _FakeArchiveDrive()
    drive.add_folder("snowball_dir", "25_snowball_按需即时卡")
    legacy_id = drive.add_file(
        "old_unassigned_1",
        "研究卡_合成球员甲_unassigned_2026-2027_CURRENT.md",
        "snowball_dir",
        b"# legacy\n\nend_marker: true\n",
    )
    other_season_id = drive.add_file(
        "old_unassigned_other_season",
        "研究卡_合成球员甲_unassigned_2025-2026_CURRENT.md",
        "snowball_dir",
        b"# other season\n\nend_marker: true\n",
    )
    other_player_id = drive.add_file(
        "old_unassigned_other_player",
        "研究卡_合成球员乙_unassigned_2026-2027_CURRENT.md",
        "snowball_dir",
        b"# other player\n\nend_marker: true\n",
    )

    receipt = upsert_snowball_card_to_drive(
        drive,
        folder_id="snowball_dir",
        filename="研究卡_合成球员甲_合成俱乐部甲_2026-2027_CURRENT.md",
        content_bytes=b"# new rich card\n\nend_marker: true\n",
        forbidden_folder_ids=["protected_zone_root"],
    )
    assert drive.delete_called is False
    assert legacy_id in drive.trashed_ids
    assert other_season_id not in drive.trashed_ids
    assert other_player_id not in drive.trashed_ids
    assert "研究卡_合成球员甲_unassigned_2026-2027_CURRENT.md" in (
        receipt.get("retired_legacy_files") or []
    )


# ===========================================================================
# P0-4: Foreign Registration rowspan Fix, <25KB Quick Index & Multi-Person Enrich
# ===========================================================================


def test_p0_4_parse_foreign_registration_rows_rowspan_secondary_header_and_orphan_fail_closed():
    from cba_kb.consumer_surface import _parse_foreign_registration_rows

    # 1. Realistic CBA markdown table with secondary header + rowspan continuation rows
    body = "\n".join([
        "| 俱乐部 | 外籍球员 |  | 国籍 | 位置 | 球衣号 |",
        "| --- | --- | --- | --- | --- | --- |",
        "| 英文名 | 中文名 |  |  |  |  |",
        "| 合成汾酒 | SYNTHETIC ALPHA | 合成外援一 | 合成国A | PG | 1 |",
        "| SYNTHETIC BETA | 合成外援二 | 合成国B | SG | 6 |  |",
        "| SYNTHETIC GAMMA | 合成外援三 | 合成国C | PF | 24 |",
        "| 合成首钢 | SYNTHETIC DELTA | 合成外援四 | 合成国D | C | 33 |",
    ])
    rows = _parse_foreign_registration_rows({"body": body})
    assert len(rows) == 4, f"Secondary header must be skipped; expected 4 rows, got {len(rows)}: {rows}"
    assert rows[0] == {
        "club": "合成汾酒",
        "player_en": "SYNTHETIC ALPHA",
        "player_zh": "合成外援一",
        "nationality": "合成国A",
        "position": "PG",
        "jersey_number": "1",
    }
    assert rows[1] == {
        "club": "合成汾酒",
        "player_en": "SYNTHETIC BETA",
        "player_zh": "合成外援二",
        "nationality": "合成国B",
        "position": "SG",
        "jersey_number": "6",
    }
    assert rows[2] == {
        "club": "合成汾酒",
        "player_en": "SYNTHETIC GAMMA",
        "player_zh": "合成外援三",
        "nationality": "合成国C",
        "position": "PF",
        "jersey_number": "24",
    }
    assert rows[3]["club"] == "合成首钢"
    assert rows[3]["jersey_number"] == "33"

    # 2. Orphan continuation row before any club -> Fail-Closed ValueError
    orphan_body = "\n".join([
        "| 俱乐部 | 外籍球员 |  | 国籍 | 位置 | 球衣号 |",
        "| --- | --- | --- | --- | --- | --- |",
        "| ORPHAN PLAYER | 孤儿外援 | 合成国A | SG | 7 |  |",
    ])
    with pytest.raises(ValueError, match="FOREIGN_ROW_ORPHAN_CONTINUATION"):
        _parse_foreign_registration_rows({"body": orphan_body})

    # 3. Standard 6-column table backward compatibility
    std_body = "\n".join([
        "| 俱乐部 | 英文名 | 中文名 | 国籍 | 位置 | 球衣号 |",
        "| --- | --- | --- | --- | --- | --- |",
        "| 合成俱乐部甲 | PLAYER ONE | 合成外一 | 合成国 | PG | 3 |",
        "| 合成俱乐部甲 | PLAYER TWO | 合成外二 | 合成国 | C | 15 |",
    ])
    std_rows = _parse_foreign_registration_rows({"body": std_body})
    assert len(std_rows) == 2
    assert std_rows[0]["club"] == "合成俱乐部甲"
    assert std_rows[1]["club"] == "合成俱乐部甲"
    assert std_rows[1]["jersey_number"] == "15"


def test_p0_4_v231_digest_compact_20_club_foreign_quick_index_under_25kb():
    from cba_kb.consumer_surface import (
        COLLECTOR_DIGEST_MAX_BYTES,
        render_collector_official_digest_md,
    )

    table_lines = [
        "| 俱乐部 | 外籍球员 |  | 国籍 | 位置 | 球衣号 |",
        "| --- | --- | --- | --- | --- | --- |",
        "| 英文名 | 中文名 |  |  |  |  |",
    ]
    for c_idx in range(1, 21):
        club = f"合成队{c_idx:02d}"
        table_lines.append(
            f"| {club} | SYNTH P{c_idx:02d}A | 外援{c_idx:02d}甲 | 合成国 | PG | {c_idx} |"
        )
        table_lines.append(
            f"| SYNTH P{c_idx:02d}B | 外援{c_idx:02d}乙 | 合成国 | SG | {c_idx + 20} |  |"
        )
        table_lines.append(
            f"| SYNTH P{c_idx:02d}C | 外援{c_idx:02d}丙 | 合成国 | C | {c_idx + 40} |  |"
        )

    bundles = [
        {
            "doc_id": "doc_foreign_20clubs_001",
            "title": "2026-2027赛季CBA联赛外籍球员注册信息",
            "source_tier": "T1",
            "event_class": "foreign_registration",
            "published_at": "2026-11-24",
            "raw_sha256": "b" * 64,
            "body": "\n".join(table_lines),
        }
    ]
    digest_bytes = render_collector_official_digest_md(
        bundles=bundles,
        release_id="v2.3.1-1",
        engine_sha=BASELINE_SHA_V231_1,
        compact_curated=True,
        include_foreign_quick_index=True,
    )
    assert len(digest_bytes) < COLLECTOR_DIGEST_MAX_BYTES
    text = digest_bytes.decode("utf-8")
    assert "合成队01（3人）" in text
    assert "合成队20（3人）" in text
    assert "外援01甲" in text
    assert "外援20丙" in text


def test_p0_4_enrich_bundle_matched_persons_multi_person_and_homonym_safety():
    from cba_kb.consumer_surface import enrich_bundle_matched_persons
    from cba_kb.person_card_builder import _match_document_bundles

    roster_candidates = [
        {"player_name": "合成队员甲", "current_club": "合成俱乐部甲", "player_uid": "p:001"},
        {"player_name": "合成队员乙", "current_club": "合成俱乐部乙", "player_uid": "p:002"},
        {"player_name": "合成队员丙", "current_club": "合成俱乐部丙", "player_uid": "p:003"},
        # Synthetic homonym pair (two distinct candidates sharing name 乔文瀚)
        {"player_name": "乔文瀚", "current_club": "合成俱乐部甲", "player_uid": "p:homonym_a"},
        {"player_name": "乔文瀚", "current_club": "合成俱乐部丁", "player_uid": "p:homonym_b"},
    ]

    # 1. Multi-person announcement: upstream only tagged 合成队员甲, body mentions 乙, 丙, and ambiguous 乔文瀚
    bundle_ambig = {
        "doc_id": "doc_nt_001",
        "title": "集训名单调整公告",
        "body": "本期集训调整：合成队员甲、合成队员乙、合成队员丙以及乔文瀚返回各自俱乐部。",
        "matched_persons": ["合成队员甲"],
    }
    enriched = enrich_bundle_matched_persons(bundle_ambig, roster_candidates=roster_candidates)
    mp = enriched["matched_persons"]
    assert mp[0] == "合成队员甲"
    assert "合成队员乙" in mp
    assert "合成队员丙" in mp
    assert "乔文瀚" not in mp, "Ambiguous homonym without club qualifier must NOT be auto-tagged"

    # 2. Club-disambiguated homonym -> allowed
    bundle_disambig = {
        "doc_id": "doc_nt_002",
        "title": "转会注册公示",
        "body": "合成队员乙与乔文瀚（合成俱乐部甲）完成本赛季注册。",
        "matched_persons": [],
    }
    enriched_disambig = enrich_bundle_matched_persons(
        bundle_disambig, roster_candidates=roster_candidates
    )
    assert "合成队员乙" in enriched_disambig["matched_persons"]
    assert "乔文瀚" in enriched_disambig["matched_persons"]

    # 3. person_card_builder._match_document_bundles matches body for unambiguous player,
    #    but refuses ambiguous homonym leak
    person_yi = {"player_name": "合成队员乙", "current_club": "合成俱乐部乙", "player_uid": "p:002"}
    matched_yi = _match_document_bundles(
        person_yi, [bundle_ambig], roster_candidates=roster_candidates
    )
    assert any(c["doc_id"] == "doc_nt_001" for c in matched_yi)

    person_homonym_b = {"player_name": "乔文瀚", "current_club": "合成俱乐部丁", "player_uid": "p:homonym_b"}
    matched_homonym_b = _match_document_bundles(
        person_homonym_b, [bundle_ambig, bundle_disambig], roster_candidates=roster_candidates
    )
    assert matched_homonym_b == [], "Ambiguous or other-club homonym mention must NOT attach to wrong player card"


# ===========================================================================
# P0-5: V215_RELEASE_SPECS["v2.3.1-1"] Registration & Historical Zero Regression
# ===========================================================================


def test_p0_5_v231_release_spec_registration_and_zero_historical_regression():
    from cba_kb.orchestration.release.prepare_v215 import V215_RELEASE_SPECS

    assert "v2.3.1-1" in V215_RELEASE_SPECS
    spec = V215_RELEASE_SPECS["v2.3.1-1"]
    assert spec["product_baseline_sha"] == BASELINE_SHA_V231_1
    assert spec.get("enable_consumer_surface_v215") is True
    assert spec.get("enable_operational_kb_v220") is True
    assert spec.get("enable_curated_compact_surface_v222") is True
    assert spec.get("enable_observation_horizon_v230") is True
    assert spec.get("enable_on_demand_cards_v230") is True
    assert spec.get("enable_v231_surface_enhancements") is True
    assert spec.get("incremental_delta_only") is True
    assert spec.get("archive_format") == "bundle_v1"

    # Historical specs v2.1.5-1..v2.3.0-1 remain intact and do NOT enable v2.3.1 flag
    for legacy_rel in ("v2.1.5-1", "v2.2.0-1", "v2.2.1-1", "v2.2.2-1", "v2.3.0-1"):
        assert legacy_rel in V215_RELEASE_SPECS
        assert not V215_RELEASE_SPECS[legacy_rel].get("enable_v231_surface_enhancements")
