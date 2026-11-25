"""Real CLI/entry-point wiring tests for REQ-231 (cba-kb-engine v2.3.1).

Exercises ``cba_kb.cli.main()`` end-to-end with in-memory S3/Drive transports
(100% synthetic fixtures, G5 == 0, G6 == 0):
- ``cloud-state-push`` & ``cloud-state-pull`` single-bundle default & ``--legacy-multifile``
- ``snowball-query --lazy-pull-bundle`` (0 pulls on HIT_FRESH, lazy pull + write-back on miss)
- ``compact-drive-archive`` (``--plan-only`` default, ``--execute`` trash-only)
- ``refresh-snowball-cards`` (``--plan-only`` vs ``--execute`` with legacy ``_unassigned_`` retirement)
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import pytest

from cba_kb import cli, cloud_state
from cba_kb.cloud_state import DEFAULT_INSTANCE_BUNDLE_KEY, R2CloudStateStore, R2Config


class _InMemoryS3Transport:
    def __init__(self) -> None:
        self.objects: Dict[str, Tuple[bytes, Dict[str, str], str]] = {}
        self.requests: List[Dict[str, Any]] = []

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
        self.requests.append({"method": method, "key": key})

        if method == "PUT":
            if lower_h.get("if-none-match") == "*" and key in self.objects:
                return 412, {}, b"PreconditionFailed"
            if "if-match" in lower_h:
                cur = self.objects.get(key)
                if cur is None or cur[2] != lower_h["if-match"]:
                    return 412, {}, b"PreconditionFailed"
            meta = {k: v for k, v in lower_h.items() if k.startswith("x-amz-meta-")}
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
        fid = f"drv_wire_{self._counter:04d}"
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


def _set_r2_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("R2_ENDPOINT", "https://synth-wire.example.invalid")
    monkeypatch.setenv("R2_BUCKET", "synth-wire-bucket")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "SYNTH_KEY")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "SYNTH_SECRET")


def _seed_instance(root: Path) -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data" / "MASTER.csv").write_text(
        "season,club_official,player,registration_stage,registration_method,contract_category,contract_term_official,registration_status\n"
        "2026-2027,合成首钢俱乐部,合成接线甲,国内球员注册,延续,D类,2028-07-31,已注册\n",
        encoding="utf-8",
    )
    (root / "config" / "curation_profile.yaml").write_text("compact_announcement_excerpt_chars: 180\n", encoding="utf-8")
    (root / "config" / "production.json").write_text('{"zones": {"history": "hist_root"}}', encoding="utf-8")


def _run_cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: List[str]) -> Dict[str, Any]:
    monkeypatch.setattr(sys, "argv", ["cba-kb", *argv])
    cli.main()
    out = capsys.readouterr().out.strip()
    return json.loads(out)


def test_cli_cloud_state_push_pull_bundle_default_and_legacy_multifile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_r2_env(monkeypatch)
    transport = _InMemoryS3Transport()
    orig_init = R2CloudStateStore.__init__

    def _patched_init(self, config, **kwargs):
        kwargs.setdefault("transport", transport)
        orig_init(self, config, **kwargs)

    monkeypatch.setattr(cloud_state.R2CloudStateStore, "__init__", _patched_init)

    src_dir = tmp_path / "src_inst"
    _seed_instance(src_dir)

    # 1. Default single-bundle push (1 PUT, excludes production.json)
    push_res = _run_cli(monkeypatch, capsys, ["cloud-state-push", "--source-dir", str(src_dir)])
    assert push_res["key"] == DEFAULT_INSTANCE_BUNDLE_KEY
    assert push_res["member_count"] == 2
    assert len([r for r in transport.requests if r["method"] == "PUT"]) == 1

    # 2. Default single-bundle pull (1 GET)
    transport.requests.clear()
    dst_dir = tmp_path / "dst_inst"
    pull_res = _run_cli(monkeypatch, capsys, ["cloud-state-pull", "--output-dir", str(dst_dir)])
    assert pull_res["key"] == DEFAULT_INSTANCE_BUNDLE_KEY
    assert len([r for r in transport.requests if r["method"] == "GET"]) == 1
    assert (dst_dir / "data" / "MASTER.csv").is_file()
    assert not (dst_dir / "config" / "production.json").exists()

    # 3. Explicit --legacy-multifile mode
    legacy_src = tmp_path / "legacy_src"
    (legacy_src / "data").mkdir(parents=True)
    (legacy_src / "data" / "MASTER.csv").write_text("season,player\n2026-2027,合成甲\n", encoding="utf-8")
    leg_push = _run_cli(
        monkeypatch,
        capsys,
        ["cloud-state-push", "--source-dir", str(legacy_src), "--prefix", "legacy_snap/", "--legacy-multifile"],
    )
    assert leg_push["uploaded_count"] == 1
    leg_dst = tmp_path / "legacy_dst"
    leg_pull = _run_cli(
        monkeypatch,
        capsys,
        ["cloud-state-pull", "--output-dir", str(leg_dst), "--prefix", "legacy_snap/", "--legacy-multifile"],
    )
    assert leg_pull["downloaded_count"] == 1
    assert (leg_dst / "data" / "MASTER.csv").is_file()


def test_cli_snowball_query_lazy_pull_bundle_hit_and_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_r2_env(monkeypatch)
    transport = _InMemoryS3Transport()
    orig_init = R2CloudStateStore.__init__

    def _patched_init(self, config, **kwargs):
        kwargs.setdefault("transport", transport)
        orig_init(self, config, **kwargs)

    monkeypatch.setattr(cloud_state.R2CloudStateStore, "__init__", _patched_init)

    # Seed remote bundle & one fresh cached card
    src_dir = tmp_path / "bundle_src"
    _seed_instance(src_dir)
    store = R2CloudStateStore(R2Config.from_env(), transport=transport)
    store.push_instance_bundle(src_dir)
    store.put_card(
        "2026-2027",
        "合成缓存甲",
        {
            "player_name": "合成缓存甲",
            "season": "2026-2027",
            "filename": "研究卡_合成缓存甲_合成首钢俱乐部_2026-2027_CURRENT.md",
            "markdown": "# Cached Card\n\nend_marker: true\n",
            "updated_at_utc": "2099-01-01T00:00:00Z",
        },
    )

    # 1. Cache hit with non-existent local --instance-root -> 0 bundle pulls
    lazy_dir = tmp_path / "not_yet_pulled"
    transport.requests.clear()
    hit = _run_cli(
        monkeypatch,
        capsys,
        [
            "snowball-query",
            "--person",
            "合成缓存甲",
            "--season",
            "2026-2027",
            "--instance-root",
            str(lazy_dir),
            "--lazy-pull-bundle",
        ],
    )
    assert hit["status"] == "HIT_FRESH"
    assert not lazy_dir.exists()
    assert not any(r["key"] == DEFAULT_INSTANCE_BUNDLE_KEY for r in transport.requests)

    # 2. Cache miss -> lazy pull once, build card from pulled MASTER.csv, push bundle back
    transport.requests.clear()
    miss = _run_cli(
        monkeypatch,
        capsys,
        [
            "snowball-query",
            "--person",
            "合成接线甲",
            "--season",
            "2026-2027",
            "--instance-root",
            str(lazy_dir),
            "--lazy-pull-bundle",
        ],
    )
    assert miss["status"] == "ACQUIRED_AND_BUILT"
    assert miss["filename"] == "研究卡_合成接线甲_合成首钢俱乐部_2026-2027_CURRENT.md"
    assert (lazy_dir / "data" / "MASTER.csv").is_file()
    assert len([r for r in transport.requests if r["method"] == "GET" and r["key"] == DEFAULT_INSTANCE_BUNDLE_KEY]) == 1
    assert len([r for r in transport.requests if r["method"] == "PUT" and r["key"] == DEFAULT_INSTANCE_BUNDLE_KEY]) == 1


def test_cli_compact_drive_archive_plan_and_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_drive = _FakeDrive()
    fake_drive.add_folder("hist_root", "90_archive_历史归档")
    fake_drive.add_file("loose_old", "before_old.json", "hist_root", b'{"a":1}', "application/json")
    monkeypatch.setattr(cli, "Drive", lambda *_args, **_kwargs: fake_drive)
    monkeypatch.delenv("CBA_KB_INSTANCE_ROOT", raising=False)

    # 1. Default plan-only -> 0 writes
    plan = _run_cli(
        monkeypatch,
        capsys,
        ["compact-drive-archive", "--history-folder-id", "hist_root", "--tag", "wiretest"],
    )
    assert plan["mode"] == "plan_only"
    assert fake_drive.upload_calls == 0
    assert fake_drive.trashed_ids == []

    # 2. Active overlap -> Fail-Closed (CLI exits non-zero with ARCHIVE_COMPACT_ACTIVE_OVERLAP)
    with pytest.raises(SystemExit) as exc_info:
        _run_cli(
            monkeypatch,
            capsys,
            [
                "compact-drive-archive",
                "--history-folder-id",
                "hist_root",
                "--active-asset-ids",
                "loose_old",
                "--execute",
            ],
        )
    assert exc_info.value.code == 1
    assert "ARCHIVE_COMPACT_ACTIVE_OVERLAP" in capsys.readouterr().err
    assert fake_drive.upload_calls == 0

    # 3. Execute -> uploads bundle and trashes loose_old (never delete)
    executed = _run_cli(
        monkeypatch,
        capsys,
        ["compact-drive-archive", "--history-folder-id", "hist_root", "--tag", "wiretest", "--execute"],
    )
    assert executed["mode"] == "execute"
    assert fake_drive.upload_calls == 1
    assert "loose_old" in fake_drive.trashed_ids
    assert fake_drive.delete_called is False


def test_cli_refresh_snowball_cards_plan_and_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    inst_dir = tmp_path / "inst"
    _seed_instance(inst_dir)
    fake_drive = _FakeDrive()
    fake_drive.add_folder("snowball_dir", "25_snowball_按需即时卡")
    legacy_id = fake_drive.add_file(
        "legacy_unassigned",
        "研究卡_合成接线甲_unassigned_2026-2027_CURRENT.md",
        "snowball_dir",
        b"# old\n\nend_marker: true\n",
    )
    monkeypatch.setattr(cli, "Drive", lambda *_args, **_kwargs: fake_drive)

    # 1. Default plan-only -> builds locally, 0 Drive writes
    plan = _run_cli(
        monkeypatch,
        capsys,
        [
            "--instance-root",
            str(inst_dir),
            "refresh-snowball-cards",
            "--players",
            "合成接线甲",
            "--season",
            "2026-2027",
            "--snowball-folder-id",
            "snowball_dir",
        ],
    )
    assert plan["mode"] == "plan_only"
    assert plan["built_count"] == 1
    assert plan["upserted_count"] == 0
    assert fake_drive.upload_calls == 0
    assert fake_drive.trashed_ids == []

    # 2. Execute -> upserts club-named card and auto-retires legacy _unassigned_ card
    executed = _run_cli(
        monkeypatch,
        capsys,
        [
            "--instance-root",
            str(inst_dir),
            "refresh-snowball-cards",
            "--players",
            "合成接线甲",
            "--season",
            "2026-2027",
            "--snowball-folder-id",
            "snowball_dir",
            "--execute",
        ],
    )
    assert executed["mode"] == "execute"
    assert executed["built_count"] == 1
    assert executed["upserted_count"] == 1
    assert fake_drive.upload_calls == 1
    assert legacy_id in fake_drive.trashed_ids
    assert fake_drive.delete_called is False
