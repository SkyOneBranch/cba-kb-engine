"""Wiring and regression tests for REQ-240 P0-5 and CLI subcommands (100% synthetic)."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest


def _synth_sha(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: List[str],
) -> tuple[int, str, str]:
    from cba_kb import cli

    monkeypatch.setattr(sys, "argv", ["cba-kb", *argv])
    exit_code = 0
    try:
        cli.main()
    except SystemExit as exc:
        exit_code = int(exc.code) if isinstance(exc.code, int) else 1
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


class _FakeRenameDrive:
    def __init__(self, initial_files: Dict[str, Dict[str, Any]]) -> None:
        self.files = initial_files
        self.renamed: List[tuple[str, str]] = []
        self._copies: Dict[str, str] = {}

    def meta(self, fid: str) -> Dict[str, Any]:
        f = self.files[fid]
        return {k: v for k, v in f.items() if k != "content"}

    def get(self, fid: str) -> bytes:
        return self.files[fid]["content"]

    def get_with_meta(self, fid: str, *, known_meta=None):
        return self.get(fid), self.meta(fid)

    def put(self, fid: str, data: bytes, mime: str):
        f = self.files[fid]
        f["content"] = bytes(data)
        f["version"] = str(int(f["version"]) + 1)
        return self.meta(fid)

    def rename(self, fid: str, new_filename: str):
        f = self.files[fid]
        f["name"] = new_filename
        f["version"] = str(int(f["version"]) + 1)
        self.renamed.append((fid, new_filename))
        return self.meta(fid)

    def list(self, parent: str):
        return []

    def index_cba_keys(self, parent: str):
        return {}

    def ensure(self, parent: str, key: str, name: str, mime: str, content=None, *, index=None):
        cid = f"f_synth_arc_{len(self._copies) + 1}"
        self._copies[key] = cid
        self.files[cid] = {
            "id": cid,
            "name": name,
            "mimeType": mime,
            "parents": [parent],
            "version": "1",
            "modifiedTime": "2026-11-01T00:00:00Z",
            "content": bytes(content) if content is not None else b"",
        }
        return cid

    def ensure_copy(self, parent: str, key: str, fid: str, name: str, *, index=None):
        cid = f"f_synth_copy_{len(self._copies) + 1}"
        self._copies[key] = cid
        src = self.files[fid]
        self.files[cid] = {
            "id": cid,
            "name": name,
            "mimeType": src["mimeType"],
            "parents": [parent],
            "version": "1",
            "modifiedTime": "2026-11-01T00:00:00Z",
            "content": bytes(src["content"]),
        }
        return cid

    def move(self, fid: str, dest: str, prev: str):
        self.files[fid]["parents"] = [dest]
        return self.meta(fid)


def test_o232_01_rename_only_marks_modified_and_restore_reverts_name(tmp_path: Path) -> None:
    from cba_kb.common import digest
    from cba_kb.orchestration.release.prepare_v215 import (
        V240_1_BASELINE_SHA,
        _manifest_candidate,
        project_targets,
    )
    from cba_kb.release import prepare, publish, restore, verify

    old_name = "研究卡_合成球员甲_合成阿尔法_2025-2026_CURRENT.md"
    new_name = "研究卡_合成球员甲_合成阿尔法_2025-2026_ARCHIVE.md"
    same_bytes = b"# Card\n- Visibility: PUBLIC_SAFE\nend_marker: true\n"
    same_sha = digest(same_bytes)

    proj = project_targets(
        release_id="v2.4.0-1",
        engine_sha=V240_1_BASELINE_SHA,
        status_id="f_synth_status",
        archive_id="p_synth_archive",
        previous_status={
            "state": "COMPLETE",
            "current_release_id": "v2.3.2-1",
            "artifacts": [
                {"id": "f_synth_card01", "name": old_name, "sha256": same_sha},
                {"id": "f_synth_code01", "name": "src__a.py", "sha256": _synth_sha("code")},
            ],
        },
        previous_targets={
            "f_synth_card01": {
                "mime": "text/markdown",
                "mode": "binary",
                "publish_parent": "p_synth_data",
            },
            "f_synth_code01": {
                "mime": "text/plain",
                "mode": "binary",
                "publish_parent": "p_synth_code",
            },
        },
        manifest_rows=[
            {"drive_file_id": "f_synth_card01", "uid": "research/card_synth_01"},
            {"drive_file_id": "f_synth_code01", "uid": "code/src/a.py"},
        ],
        tracked=["src/a.py"],
        tracked_hashes={"src/a.py": _synth_sha("code")},
        code_parent="p_synth_code",
        declared_consumer_targets={
            "research/card_synth_01": {
                "name": new_name,
                "mime": "text/markdown",
                "content": same_bytes,
                "publish_parent": "p_synth_data",
            }
        },
    )
    card_target = next(
        t for t in proj["existing_targets"] if t["logical_key"] == "research/card_synth_01"
    )
    assert card_target["name"] == new_name
    assert card_target["rename_from"] == old_name
    assert card_target["disposition"] == "modified"

    # _manifest_candidate syncs name column when present
    prev_manifest = (
        "drive_file_id,uid,name,content_hash\n"
        f"f_synth_card01,research/card_synth_01,{old_name},{same_sha}\n"
    ).encode("utf-8-sig")
    updated_manifest = _manifest_candidate(
        prev_manifest,
        {"release_id": "v2.4.0-1"},
        {"reservations": {}},
        {"research/card_synth_01": "f_synth_card01"},
        {"research/card_synth_01": same_bytes},
        {"research/card_synth_01": card_target},
    )
    rows = list(csv.DictReader(io.StringIO(updated_manifest.decode("utf-8-sig"))))
    assert rows[0]["name"] == new_name

    # Publish applies rename even when bytes are unchanged, and restore reverts filename
    drive = _FakeRenameDrive({
        "f_synth_card01": {
            "id": "f_synth_card01",
            "name": old_name,
            "mimeType": "text/markdown",
            "parents": ["p_synth_data"],
            "version": "1",
            "modifiedTime": "2026-11-01T00:00:00Z",
            "content": same_bytes,
        },
        "f_synth_status": {
            "id": "f_synth_status",
            "name": "release_status.json",
            "mimeType": "application/json",
            "parents": ["p_synth_data"],
            "version": "1",
            "modifiedTime": "2026-11-01T00:00:00Z",
            "content": json.dumps(
                {
                    "state": "COMPLETE",
                    "current_release_id": "v2.3.2-1",
                    "artifacts": [
                        {"id": "f_synth_card01", "name": old_name, "sha256": same_sha}
                    ],
                },
                sort_keys=True,
            ).encode("utf-8"),
        },
    })
    cand = tmp_path / "cand.md"
    cand.write_bytes(same_bytes)
    outbox = tmp_path / "outbox_rename_only"
    prepare(
        drive,
        outbox,
        "v2.4.0-1",
        [
            {
                "id": "f_synth_card01",
                "name": new_name,
                "rename_from": old_name,
                "mime": "text/markdown",
                "path": str(cand),
                "publish_parent": "p_synth_data",
            }
        ],
        "p_synth_archive",
        "f_synth_status",
        carry_forward_artifacts=True,
        environment="sandbox",
    )
    publish(drive, outbox, single_writer=True)
    assert drive.files["f_synth_card01"]["name"] == new_name
    assert verify(drive, outbox)["verified"] == 1

    restore(drive, outbox, single_writer=True)
    assert drive.files["f_synth_card01"]["name"] == old_name


def test_o232_03_cloud_state_allowlist_and_raw_exclusion() -> None:
    from cba_kb.cloud_state import (
        _bundle_allowlisted_exact_names,
        _is_bundle_member_allowed,
    )

    exact = _bundle_allowlisted_exact_names()
    for allowed in (
        "team_staff_snapshots.jsonl",
        "data/person_registry.json",
        "data/story_records.jsonl",
        "organization_profiles.json",
        "clubs/俱乐部全景卡_合成阿尔法_PUBLIC_CURRENT.md",
        "data/clubs/俱乐部全景卡_合成阿尔法_CURRENT.md",
    ):
        assert _is_bundle_member_allowed(allowed, exact) is True

    for blocked in (
        "raw/person_registry.json",
        "raw/clubs/club.md",
        "document_lane/archive/raw/clip.md",
        "production.json",
        "config/production.json",
        ".credentials/oauth.json",
        "evidence/audit.json",
        "inbox/drop.md",
        "clubs/not_markdown.json",
    ):
        assert _is_bundle_member_allowed(blocked, exact) is False


def test_o232_04_kc_visibility_tilde_fence_and_casefold_clue_markers() -> None:
    from cba_kb.kc_visibility import verify_public_safe_card_text

    # Clean public_safe card passes
    verify_public_safe_card_text(
        "> [HISTORICAL_SEASON_ARCHIVE] 2025-2026\n\n"
        "# Card\n- Visibility: PUBLIC_SAFE\nend_marker: true\n"
    )

    # Tilde fences blocked
    with pytest.raises(ValueError, match="PUBLIC_SAFE_REDACTION_VIOLATION:.*fenced_code_block"):
        verify_public_safe_card_text("# Card\n~~~\nsecret\n~~~\nend_marker: true\n")

    # Case-insensitive [INTERNAL_CLUE_ONLY blocked via casefold()
    for variant in (
        "[INTERNAL_CLUE_ONLY: clue]",
        "[internal_clue_only: clue]",
        "[Internal_Clue_Only: clue]",
    ):
        with pytest.raises(ValueError, match="PUBLIC_SAFE_REDACTION_VIOLATION"):
            verify_public_safe_card_text(f"# Card\n- {variant}\nend_marker: true\n")


def test_cli_v240_subcommands_fail_closed_and_output_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 1. build-public-site fails closed (exit code 1, 0 partial files) on internal_full input
    club_json = tmp_path / "club_bad.json"
    club_json.write_text(
        json.dumps(
            {
                "organization_id": "ORG-SYNTH-ALPHA",
                "club_name": "合成阿尔法俱乐部",
                "visibility": "public_safe",
                "markdown": "# Club\n- Visibility: PUBLIC_SAFE\nend_marker: true\n",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    persons_dir = tmp_path / "persons_bad"
    persons_dir.mkdir()
    (persons_dir / "bad.json").write_text(
        json.dumps(
            {
                "person_uid": "PERS-SYNTH-BAD",
                "person_name": "合成人物",
                "visibility": "internal_full",
                "markdown": "# Internal\nend_marker: true\n",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    bad_site_out = tmp_path / "bad_site"
    code, _stdout, stderr = _run_cli(
        monkeypatch,
        capsys,
        [
            "build-public-site",
            "--club-card",
            str(club_json),
            "--person-cards-dir",
            str(persons_dir),
            "--output-dir",
            str(bad_site_out),
        ],
    )
    assert code == 1
    assert "PUBLIC_SITE_VISIBILITY_VIOLATION" in stderr
    assert not bad_site_out.exists() or list(bad_site_out.rglob("*")) == []

    # 2. compose-verified-brief writes --output file and fails closed on empty verified facts
    facts_ok = tmp_path / "facts_ok.json"
    facts_ok.write_text(
        json.dumps(
            [
                {
                    "sentence": "合成球员甲2026-2027赛季效力合成阿尔法俱乐部。",
                    "status": "VERIFIED_PRESENT",
                    "source_kind": "official_registration",
                    "citation_label": "CBA官方注册名单",
                    "source_url": "https://example.invalid/cba/reg",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    brief_out = tmp_path / "brief.json"
    code, stdout, stderr = _run_cli(
        monkeypatch,
        capsys,
        [
            "compose-verified-brief",
            "--entity",
            "合成球员甲",
            "--facts",
            str(facts_ok),
            "--max-chars",
            "120",
            "--visibility",
            "public_safe",
            "--output",
            str(brief_out),
        ],
    )
    assert code == 0, stderr
    assert brief_out.is_file()
    parsed = json.loads(stdout)
    assert parsed == json.loads(brief_out.read_text(encoding="utf-8"))
    assert parsed["char_count"] <= 120

    facts_empty = tmp_path / "facts_empty.json"
    facts_empty.write_text(
        json.dumps(
            [
                {
                    "sentence": "本赛季暂无记录。",
                    "status": "SOURCE_GAP_UNKNOWN",
                    "source_kind": "official_stats",
                    "citation_label": "缺口",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    code, _stdout, stderr = _run_cli(
        monkeypatch,
        capsys,
        [
            "compose-verified-brief",
            "--entity",
            "合成球员甲",
            "--facts",
            str(facts_empty),
        ],
    )
    assert code == 1
    assert "NO_VERIFIED_FACTS_FOR_BRIEF" in stderr
