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


# =============================================================================
# Round 1 Q2 Remediation Regression Tests (B-Q2-1, M-Q2-1..3, m-Q2-1..4)
# =============================================================================


def test_bq2_1_sanitize_public_free_text_unclosed_fences_fail_closed() -> None:
    from cba_kb.kc_render import _sanitize_public_free_text

    # Unclosed ``` at line start discards opener and everything after it
    out1 = _sanitize_public_free_text("公开摘要\n```\nSECRET VERBATIM INTERNAL NOTE")
    assert "SECRET VERBATIM" not in out1
    assert "```" not in out1
    assert "公开摘要" in out1

    # Unclosed ~~~ at line start discards opener and everything after it
    out2 = _sanitize_public_free_text("公开摘要\n~~~\nSECRET TILDE BODY")
    assert "SECRET TILDE BODY" not in out2
    assert "~~~" not in out2
    assert "公开摘要" in out2

    # Inline unclosed ``` or ~~~ also truncates from opener to end
    out3 = _sanitize_public_free_text("前文摘要 ```leaked tail")
    assert "leaked tail" not in out3
    assert "```" not in out3
    out4 = _sanitize_public_free_text("前文摘要 ~~~leaked tilde tail")
    assert "leaked tilde tail" not in out4
    assert "~~~" not in out4


def test_mq2_1_wiki_v240_wires_iron_rules_1_and_3() -> None:
    from cba_kb.kc_render import render_research_card

    payload: Dict[str, Any] = {
        "metric": "points_per_game",
        "grains": {"STATS": {"posture": "UNKNOWN", "season": "2026-2027"}},
        "evidence_transparency": {
            "posture": "UNKNOWN",
            "unknown_subkind": "NO_SEASON_STATS_RECORDED_YET",
            "scope_meta": {"player_name": "合成主帅甲", "season": "2026-2027"},
        },
        "role_tenures": [
            {
                "role": "HEAD_COACH",
                "organization_name": "合成阿尔法俱乐部",
                "season_start": "2025-2026",
                "authority_scope": "LEAGUE_OFFICIAL",
                "target_slot": "official_registration",
            },
            {
                "role": "HEAD_COACH",
                "organization_name": "合成谣言俱乐部",
                "season_start": "2026-2027",
                "authority_scope": "PUBLIC_MEDIA",
                "target_slot": "official_registration",
            },
        ],
        "timeline_events": [
            {
                "event_id": "e1",
                "canonical_source_id": "src-A",
                "source_name": "合成官方通告A",
                "summary": "合成官方通告入选名单",
            },
            {
                "event_id": "e2",
                "canonical_source_id": "src-A",
                "reprint_of": "src-A",
                "source_name": "转载媒体A1",
                "summary": "合成官方通告入选名单",
            },
            {
                "event_id": "e3",
                "canonical_source_id": "src-B",
                "source_name": "合成俱乐部官微B",
                "summary": "俱乐部官微称随队合练未登场",
            },
        ],
    }
    md = render_research_card(
        payload, evidence_verified=True, visibility="public_safe", card_style="wiki_v240"
    )
    assert "合成阿尔法俱乐部" in md
    assert "合成谣言俱乐部" not in md
    assert md.count("合成官方通告入选名单") == 1
    assert "俱乐部官微称随队合练未登场" in md


def test_mq2_2_and_mq2_4_club_card_snapshot_discrepancy_and_seasonless_rows() -> None:
    from cba_kb.club_card_builder import build_club_panorama_card

    res = build_club_panorama_card(
        "合成阿尔法俱乐部",
        seasons=("2025-2026", "2026-2027"),
        organization_profile={
            "organization_id": "ORG-SYNTH-ALPHA",
            "canonical_name": "合成阿尔法俱乐部",
            "short_name": "合成阿尔法",
        },
        master_rows=[
            {
                "club_official": "合成阿尔法俱乐部",
                "player": "合成无赛季国内球员",
                "contract_category": "B类",
            }
        ],
        foreign_rows=[
            {
                "club_official": "合成阿尔法俱乐部",
                "player_name_en": "Synth Seasonless Foreign",
                "player": "Synth Seasonless Foreign",
                "registration_status": "已注册",
            }
        ],
        staff_snapshots=[
            {
                "season": "2026-2027",
                "organization_id": "ORG-SYNTH-ALPHA",
                "person_uid": "PERS-SYNTH-COACH01",
                "person_name": "合成主帅甲",
                "league_listed_role": "ASSISTANT_COACH",
                "club_appointed_role": "HEAD_COACH",
            }
        ],
        visibility="public_safe",
    )
    md = res["markdown"]
    assert res["status"] == "BUILT"
    # M-Q2-2: snapshot-derived coach facets detect institutional discrepancy
    assert "ASSISTANT_COACH" in md and "HEAD_COACH" in md
    assert "Discrepancy" in md or "LEAGUE_VS_CLUB_ROLE_DISCREPANCY" in md
    # m-Q2-4: both seasonless domestic and foreign rows handled consistently
    assert "合成无赛季国内球员" in md
    assert "Synth Seasonless Foreign" in md


def test_mq2_3_editorial_brief_strict_one_to_one_sentence_attribution() -> None:
    from cba_kb.editorial_brief import compose_verified_brief

    facts = [
        {
            "sentence": "合成球员甲2026-2027赛季注册于合成阿尔法。常规赛场均13.5分4.2助攻。",
            "status": "VERIFIED_PRESENT",
            "source_kind": "official_stats",
            "citation_label": "CBA官方统计",
            "source_url": "https://example.invalid/cba/stats",
        }
    ]
    brief = compose_verified_brief(facts, entity_name="合成球员甲", max_chars=120, visibility="public_safe")
    assert len(brief["body"]) <= 120
    assert len(brief["sentence_attributions"]) == 2
    assert any("13.5" in a["sentence"] for a in brief["sentence_attributions"])
    for attr in brief["sentence_attributions"]:
        assert attr["sentence"] in brief["body"]


def test_mq2_1_minor_role_facets_uid_bound_skips_uidless_homonym_snapshot() -> None:
    from cba_kb.role_facets import OBSERVED_EMPTY, build_coach_role_facets

    facets = build_coach_role_facets(
        person_uid="PERS-SYNTH-COACH01",
        person_name="合成同名教练",
        season="2026-2027",
        staff_snapshots=[
            {
                "season": "2026-2027",
                "person_name": "合成同名教练",
                "league_listed_role": "HEAD_COACH",
                "captured_at": "2026-10-10T00:00:00Z",
            }
        ],
    )
    assert facets.league_listed_role == OBSERVED_EMPTY


def test_mq2_2_minor_story_projection_strips_drive_and_non_https_urls() -> None:
    from cba_kb.story_record import make_story_record, project_stories_for_entity

    story = make_story_record(
        title="合成公开故事带私有链接",
        occurred_season="2024-2025",
        published_at="2026-10-01T00:00:00Z",
        canonical_source={
            "source_id": "src-drive-leak",
            "title": "合成专题",
            "url": "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/view",
            "source_kind": "public_media",
            "authority_scope": "PUBLIC_MEDIA",
        },
        participants=[
            {
                "person_uid": "PERS-SYNTH-P100",
                "display_name": "合成球员丙",
                "role": "SUBJECT_INTERVIEWEE",
                "internal_verbatim_or_note": "内部备注",
                "public_perspective_summary": "合成球员丙在2024-2025赛季完成首秀。",
            }
        ],
        organization_ids=["ORG-SYNTH-ALPHA"],
        visibility_policy={"public_export_allowed": True},
    )
    projected = project_stories_for_entity(
        [story], entity_id="PERS-SYNTH-P100", entity_kind="person", visibility="public_safe"
    )
    assert len(projected) == 1
    assert projected[0]["canonical_source"].get("url") == ""
    assert "drive.google.com" not in json.dumps(projected, ensure_ascii=False)


def test_mq2_3_minor_release_prepare_allows_crash_retry_when_already_renamed(
    tmp_path: Path,
) -> None:
    from cba_kb.common import digest
    from cba_kb.release import prepare

    old_name = "研究卡_合成球员甲_合成阿尔法_2025-2026_CURRENT.md"
    new_name = "研究卡_合成球员甲_合成阿尔法_2025-2026_ARCHIVE.md"
    body = b"# Card\nend_marker: true\n"
    sha = digest(body)

    def _make_drive(remote_name: str) -> _FakeRenameDrive:
        return _FakeRenameDrive({
            "f_synth_card01": {
                "id": "f_synth_card01",
                "name": remote_name,
                "mimeType": "text/markdown",
                "parents": ["p_synth_data"],
                "version": "2",
                "modifiedTime": "2026-11-01T00:00:00Z",
                "content": body,
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
                        "artifacts": [{"id": "f_synth_card01", "name": remote_name, "sha256": sha}],
                    },
                    sort_keys=True,
                ).encode("utf-8"),
            },
        })

    cand = tmp_path / "cand.md"
    cand.write_bytes(body)

    # Already renamed to target new_name -> crash-retry idempotent PASS
    plan = prepare(
        _make_drive(new_name),
        tmp_path / "outbox_retry_ok",
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
    assert plan["entries"][0]["name"] == new_name

    # Unrelated remote name -> still fails closed with RENAME_SOURCE_MISMATCH
    with pytest.raises(ValueError, match="RENAME_SOURCE_MISMATCH"):
        prepare(
            _make_drive("unrelated_other_name.md"),
            tmp_path / "outbox_retry_bad",
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


# =============================================================================
# Round 2 Q2 Remediation Regression Tests (M-R2-1, m-R2-1, m-R2-2)
# =============================================================================


def test_mr2_1_distinct_stories_and_events_sharing_canonical_source_id_are_not_merged() -> None:
    from cba_kb.kc_render import (
        dedup_reprints_and_preserve_discrepancies,
        render_research_card,
    )

    # 1. Two distinct stories sharing canonical_source_id both render; reprint still dedups
    stories = [
        {
            "story_id": "story_000000000000000000000001",
            "occurred_season": "2024-2025",
            "title": "合成季前拉练专题",
            "summary": "合成主帅甲带队完成夏训体能储备。",
            "canonical_source": {"source_id": "src-shared-portal", "source_kind": "public_media"},
        },
        {
            "story_id": "story_000000000000000000000002",
            "occurred_season": "2025-2026",
            "title": "合成新援融入专题",
            "summary": "合成新星乙在新赛季担任首发控卫。",
            "canonical_source": {"source_id": "src-shared-portal", "source_kind": "public_media"},
        },
        {
            "story_id": "story_000000000000000000000003",
            "occurred_season": "2024-2025",
            "title": "合成季前拉练专题",
            "summary": "合成主帅甲带队完成夏训体能储备。",
            "reprint_of": "src-shared-portal",
            "canonical_source": {"source_id": "src-shared-portal", "source_kind": "public_media"},
        },
    ]
    # 2. Two distinct timeline events sharing canonical_source_id both render
    events = [
        {
            "event_id": "ev-1",
            "canonical_source_id": "src-official-feed",
            "summary": "2026-10-01 完成季前赛注册",
        },
        {
            "event_id": "ev-2",
            "canonical_source_id": "src-official-feed",
            "summary": "2026-10-15 入选常规赛揭幕战大名单",
        },
        {
            "event_id": "ev-3",
            "canonical_source_id": "src-official-feed",
            "reprint_of": "src-official-feed",
            "summary": "2026-10-01 完成季前赛注册",
        },
    ]
    dedup_ev = dedup_reprints_and_preserve_discrepancies(events)
    assert len(dedup_ev["canonical_items"]) == 2
    ev1 = next(x for x in dedup_ev["canonical_items"] if x["event_id"] == "ev-1")
    assert len(ev1["deduped_reprints"]) == 1

    payload: Dict[str, Any] = {
        "metric": "points_per_game",
        "grains": {"STATS": {"posture": "UNKNOWN", "season": "2026-2027"}},
        "evidence_transparency": {
            "posture": "UNKNOWN",
            "unknown_subkind": "NO_SEASON_STATS_RECORDED_YET",
            "scope_meta": {"player_name": "合成球员戊", "season": "2026-2027"},
        },
        "story_records": stories,
        "timeline_events": events,
    }
    md = render_research_card(
        payload, evidence_verified=True, visibility="public_safe", card_style="wiki_v240"
    )
    assert md.count("合成季前拉练专题") == 1
    assert md.count("合成新援融入专题") == 1
    assert md.count("2026-10-01 完成季前赛注册") == 1
    assert md.count("2026-10-15 入选常规赛揭幕战大名单") == 1


def test_mr2_1_minor_trailing_dot_drive_host_rejected() -> None:
    from cba_kb.story_record import make_story_record, project_stories_for_entity

    story = make_story_record(
        title="合成FQDN尾点链接过滤",
        occurred_season="2025-2026",
        published_at="2026-10-01T00:00:00Z",
        canonical_source={
            "source_id": "src-drive-fqdn",
            "title": "合成来源",
            "url": "https://drive.google.com./file/d/synth_id/view",
            "source_kind": "public_media",
            "authority_scope": "PUBLIC_MEDIA",
        },
        participants=[
            {
                "person_uid": "PERS-SYNTH-P200",
                "display_name": "合成球员丁",
                "role": "SUBJECT_INTERVIEWEE",
                "internal_verbatim_or_note": "内部备注",
                "public_perspective_summary": "合成球员丁完成常规赛注册。",
            }
        ],
        organization_ids=["ORG-SYNTH-ALPHA"],
        visibility_policy={"public_export_allowed": True},
    )
    projected = project_stories_for_entity(
        [story], entity_id="PERS-SYNTH-P200", entity_kind="person", visibility="public_safe"
    )
    assert len(projected) == 1
    assert projected[0]["canonical_source"]["url"] == ""
    assert "drive.google.com" not in json.dumps(projected, ensure_ascii=False)


def test_mr2_2_minor_editorial_brief_no_space_split_and_abbreviations() -> None:
    from cba_kb.editorial_brief import compose_verified_brief

    # No-space English boundary splits into 2 sentences with 2 attributions
    brief_nospace = compose_verified_brief(
        [
            {
                "sentence": "First claim.Second claim.",
                "status": "VERIFIED_PRESENT",
                "source_kind": "official_stats",
                "citation_label": "Official Feed",
                "source_url": "https://example.invalid/cba/feed",
            }
        ],
        entity_name="Synth Player",
        max_chars=120,
        visibility="public_safe",
    )
    assert len(brief_nospace["sentence_attributions"]) == 2
    assert brief_nospace["sentence_attributions"][0]["sentence"] == "First claim."
    assert brief_nospace["sentence_attributions"][1]["sentence"] == "Second claim."

    # Common abbreviation + decimal stays 1 sentence with 1 attribution
    brief_abbrev = compose_verified_brief(
        [
            {
                "sentence": "Uses stats, e.g. 13.5 PPG.",
                "status": "VERIFIED_PRESENT",
                "source_kind": "official_stats",
                "citation_label": "Official Feed",
                "source_url": "https://example.invalid/cba/feed",
            }
        ],
        entity_name="Synth Player",
        max_chars=120,
        visibility="public_safe",
    )
    assert len(brief_abbrev["sentence_attributions"]) == 1
    assert brief_abbrev["sentence_attributions"][0]["sentence"] == "Uses stats, e.g. 13.5 PPG."
