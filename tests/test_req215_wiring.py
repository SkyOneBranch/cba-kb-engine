"""End-to-end call-chain and fail-closed adversarial wiring tests for REQ-215 (v2.1.5)."""
from __future__ import annotations

import copy
import csv
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
from openpyxl import Workbook
import pytest

from automation.hardening import validate_release_identity
from cba_kb import aliases, consumer_surface, kc_cleanroom, kc_render, oracle
from cba_kb.kc_consume import ConsumeSession
from cba_kb.kc_passages import evidence_chain
from cba_kb.kc_snapshot import build_snapshot
from cba_kb.native import wrap
from cba_kb.orchestration.release import prepare_v215 as orch
from tests.test_release_orchestration import canonical_registry, projection_inputs

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. Club team_id dictionary & Research Card wiring (positive + negative)
# ---------------------------------------------------------------------------


def test_wiring_all_20_clubs_resolve_and_unknown_team_id_falls_back_honestly():
    clubs = aliases.Clubs(ROOT).clubs
    assert len(aliases.CBA_TEAM_ID_TO_ZH) == 20
    assert len(aliases.CBA_TEAM_ID_TO_CANONICAL_ID) == 20
    for tid, zh_name in aliases.CBA_TEAM_ID_TO_ZH.items():
        assert aliases.resolve_team_id_zh(tid) == zh_name
        assert aliases.resolve_team_id_zh(int(tid)) == zh_name
        assert aliases.CBA_TEAM_ID_TO_CANONICAL_ID[tid] in clubs
        disp = aliases.format_club_team_display(tid)
        assert zh_name in disp and tid in disp

    for bad_tid in ("999999", "", "   ", None, True, False):
        assert aliases.resolve_team_id_zh(bad_tid) is None

    assert aliases.format_club_team_display("999999") == "999999 (unresolved_team_id)"
    assert aliases.format_club_team_display(None, None) is None


def test_wiring_research_card_unknown_club_fallback_and_rule_a_zero_leak():
    fx = copy.deepcopy(kc_cleanroom.build_cleanroom_fixture())
    fx["stats_records"][0] = kc_cleanroom._stats_record(
        player_uid=kc_cleanroom.A,
        provider_player_id="synth_01",
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        team_id="999999",
        team_name=None,
        metrics=fx["stats_records"][0]["metrics"],
    )

    promoted = consumer_surface.build_promoted_research_slice_artifacts(
        fx,
        player_uid=kc_cleanroom.A,
        player_name="林测试",
        player_id="synth_01",
        season="2025-2026",
    )
    card_md = promoted["research/card_synth_01_2025_2026"]["content"].decode("utf-8")
    assert "999999 (unresolved_team_id)" in card_md
    assert card_md.splitlines()[0].startswith("# 人物研究卡｜林测试（synth_01）｜2025-2026｜CURRENT")
    assert card_md.strip().endswith("end_marker: true")

    # Rule A non-leak: unauthorized vs nonexistent players remain identical UNKNOWN
    snap = build_snapshot(
        entries=[{
            "entry_id": "passage:fixture_doc_D1",
            "sha256": fx["passages"][0]["digest"],
            "authority": "canonical",
            "rights": "public",
            "source_id": "fixture_doc_D1",
        }],
        target_domain="fixture_target",
        capabilities=["stats", "identity", "document", "answer"],
        build_provenance="test",
        build_time="2026-11-17T00:00:00+00:00",
        data_release="SYNTH",
        engine_candidate="v2.1.5",
        build_code="v2.1.5",
    )
    sess = ConsumeSession(
        target_domain="fixture_target",
        authorized_players={kc_cleanroom.A},
        authorized_sources={"fixture_doc_D1"},
        authorized_objects={"fixture_doc_D1"},
    )
    sess.bind_current(snap)
    sess.register_passages(fx["passages"][:1])
    binding = sess.current_binding()
    chain = evidence_chain(
        receipt_id=binding["receipt_binding"],
        snapshot_id=snap.snapshot_id,
        build_version=snap.build_code,
        source_digest=fx["passages"][0]["digest"],
        authorization_basis=binding["authorization_basis"],
    )

    out_unauth = sess.consume_answer(
        records=fx["stats_records"],
        player_uid=kc_cleanroom.B,
        season=kc_cleanroom.SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        provider="CBA_OFFICIAL",
        competition="CBA",
        metric="points_per_game",
        fact=fx["fact"],
        passage=fx["passages"][0],
        source_text=fx["passages"][0]["text"],
        chain=chain,
        claims=[],
    )
    out_nonex = sess.consume_answer(
        records=fx["stats_records"],
        player_uid="nonexistent_player_999",
        season=kc_cleanroom.SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        provider="CBA_OFFICIAL",
        competition="CBA",
        metric="points_per_game",
        fact=fx["fact"],
        passage=fx["passages"][0],
        source_text=fx["passages"][0]["text"],
        chain=chain,
        claims=[],
    )
    assert out_unauth["payload"]["evidence_transparency"] == out_nonex["payload"]["evidence_transparency"]
    unauth_card = sess.render_verified(out_unauth, kind="research_card")
    assert unauth_card.splitlines()[0] == "# Research Card (Serving Projection)"
    assert "林测试" not in unauth_card and "深圳马可波罗" not in unauth_card

    for unsafe in ("", "   ", "../escape", "a/b", "a\\b", 123):
        with pytest.raises(ValueError, match="UNSAFE_FILENAME_PART"):
            kc_render.research_card_consumer_filename(unsafe, "907766", "2025-2026")


# ---------------------------------------------------------------------------
# 2. MASTER row count CSV/XLSX + prepare_v215 control & consumer wiring
# ---------------------------------------------------------------------------


def _make_sample_master_xlsx(row_count: int) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "MASTER"
    ws.append(["record_key", "season", "club_id", "player"])
    for i in range(row_count):
        ws.append([f"2025-2026|club|{i}", "2025-2026", "club", f"player_{i}"])
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


def test_wiring_compute_master_row_count_csv_xlsx_and_failures():
    xlsx_bytes = _make_sample_master_xlsx(37)
    assert consumer_surface.compute_master_row_count(xlsx_bytes, is_xlsx=True) == 37

    csv_bytes = ("col1,col2\n" + "\n".join(f"a{i},b{i}" for i in range(42)) + "\n\n").encode("utf-8-sig")
    assert consumer_surface.compute_master_row_count(csv_bytes, is_xlsx=False) == 42

    with pytest.raises(ValueError, match="MASTER_ROW_COUNT_EMPTY"):
        consumer_surface.compute_master_row_count(b"col1,col2\n\n")

    wb = Workbook()
    wb.active.title = "NOT_MASTER"
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    with pytest.raises(ValueError, match="MASTER_SHEET_MISSING"):
        consumer_surface.compute_master_row_count(buf.getvalue(), is_xlsx=True)


def test_wiring_prepare_v215_dynamic_master_count_redaction_and_consumer_targets(
    monkeypatch, tmp_path
):
    reg = canonical_registry()
    reg_bytes = __import__("yaml").safe_dump(reg, allow_unicode=True, sort_keys=False).encode("utf-8")

    # Setup docs needed by entry/context
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/OPERATIONS_V1_5.md").write_text("ops\n", encoding="utf-8")
    (tmp_path / "docs/MASTER_MIGRATION_NOTE.md").write_text(
        (ROOT / "docs/MASTER_MIGRATION_NOTE.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    fake_drive_token_1 = "1" + "FakeDriveIdTokenAlpha1234567890"
    fake_drive_token_2 = "1" + "FakeDriveIdTokenBeta12345678901"
    raw_sr_csv = (
        "source_id,drive_file_id,current_parent_id,source_title,source_type,season,authority_tier,evidence_status,usage_policy,discovered_at_utc,sha256\n"
        f"drive:{fake_drive_token_1},{fake_drive_token_1},{fake_drive_token_2},2025-2026官方公示.pdf,league_notice,2025-2026,T1,EXTRACTED,canonical_allowed,2026-10-01T00:00:00Z,{'c'*64}\n"
    ).encode("utf-8-sig")

    master_csv = (
        "record_key,season,club_id,club_official,sequence,player,registration_stage,registration_method,contract_category,contract_term_official,former_club,notice_deadline,registration_status,remarks,source_file_id,source_url,source_page,source_type,extraction_method,verification_level\n"
        + "".join(
            f"2025-2026|shenzhen|p{i},2025-2026,shenzhen,深圳新世纪,{i},示例{i},国内,延续,B类,2年,,,,{fake_drive_token_1},https://example.org/{i},1,notice,manual,auto_validated\n"
            for i in range(3465)
        )
    ).encode("utf-8-sig")

    fx = copy.deepcopy(kc_cleanroom.build_cleanroom_fixture())
    fx["stats_records"][0] = kc_cleanroom._stats_record(
        player_uid=kc_cleanroom.A,
        provider_player_id="907766",
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        team_id="29131",
        team_name=None,
        metrics=fx["stats_records"][0]["metrics"],
    )
    promoted_research = consumer_surface.build_promoted_research_slice_artifacts(
        fx,
        player_uid=kc_cleanroom.A,
        player_name="林测试",
        player_id="907766",
        season="2025-2026",
    )
    consumer_targets = consumer_surface.build_consumer_surface_targets_spec(
        release_id="v2.1.5-1",
        engine_sha="e" * 40,
        master_csv_bytes=master_csv,
        source_registry_bytes=raw_sr_csv,
        promoted_research_artifacts=promoted_research,
    )
    for spec in consumer_targets.values():
        spec["publish_parent"] = "data"

    monkeypatch.setitem(
        orch.V215_RELEASE_SPECS,
        "v2.1.5-1",
        {"product_baseline_sha": "b" * 40, "enable_consumer_surface_v215": True},
    )
    rel_check = validate_release_identity({
        "target_product_version": "v2.1.5",
        "proposed_release_attempt": "v2.1.5-1",
        "product_candidate_sha": "b" * 40,
        "release_execution_role": "RELEASE_EXECUTOR",
    })
    assert rel_check["status"] == "PASS"

    # Build projection inputs using Base v2.1.4 fixtures plus derived/MASTER.csv and control docs
    inputs = projection_inputs(tracked=[], hashes={})
    inputs["manifest_rows"] = [
        {"drive_file_id": fake_drive_token_1, "uid": "config/canonical_products.yaml"},
        {"drive_file_id": "manifest-id", "uid": "input/manifest.csv"},
        {"drive_file_id": fake_drive_token_2, "uid": "entry/README"},
        {"drive_file_id": "index-id", "uid": "derived/INDEX.md"},
        {"drive_file_id": "context-id", "uid": "entry/context"},
        {"drive_file_id": "ctx-card-id", "uid": "ai/CONTEXT_CARD.md"},
        {"drive_file_id": "ver-doc-id", "uid": "CURRENT_VERSION_DOC"},
        {"drive_file_id": "master-csv-id", "uid": "derived/MASTER.csv"},
        {"drive_file_id": "event-file", "uid": "facts/events.jsonl"},
        {"drive_file_id": "compat-file", "uid": "facts/old_events.jsonl"},
    ]
    artifacts_meta = [
        ("config/canonical_products.yaml", fake_drive_token_1, "canonical_products.yaml", "text/yaml", "binary", "config", reg_bytes),
        ("input/manifest.csv", "manifest-id", "manifest.csv", "text/csv", "binary", "config", (
            "uid,asset_type,source_type,source_url,local_path,content_hash,drive_file_id,ima_item_id,status,synced_at,published_release,hash_scope\n"
            + "facts/events.jsonl,release_artifact,generated,,facts/events.jsonl," + ("b" * 64) + ",event-file,,published,,v2.1.4,bytes\n"
            + "facts/old_events.jsonl,release_artifact,generated,,facts/old_events.jsonl," + ("c" * 64) + ",compat-file,,published,,v2.1.4,bytes\n"
            + "\n".join(
                f"{r['uid']},release_artifact,generated,,{r['uid']},{'0'*64},{r['drive_file_id']},,published,,v2.1.4,bytes"
                for r in inputs["manifest_rows"]
                if r["uid"] not in ("facts/events.jsonl", "facts/old_events.jsonl")
            ) + "\n"
        ).encode("utf-8-sig")),
        ("entry/README", fake_drive_token_2, "00_README_先读这个", orch.NATIVE_DOCUMENT, "managed_doc", "root", wrap(f"- master: {fake_drive_token_1}\n").encode()),
        ("derived/INDEX.md", "index-id", "INDEX", orch.NATIVE_DOCUMENT, "managed_doc", "data", wrap("index old\n").encode()),
        ("entry/context", "context-id", "02_AI_PROJECT_CONTEXT_技术手册.md", "text/markdown", "binary", "root", b"ctx old\n"),
        ("ai/CONTEXT_CARD.md", "ctx-card-id", "CONTEXT_CARD.md", "text/markdown", "binary", "ai", b"card old\n"),
        ("CURRENT_VERSION_DOC", "ver-doc-id", "CBA-KB_v2.1.4.md", "text/markdown", "binary", "root", b"ver old\n"),
        ("derived/MASTER.csv", "master-csv-id", "MASTER.csv", "text/csv", "binary", "data", master_csv),
        ("facts/events.jsonl", "event-file", "events.jsonl", "application/x-ndjson", "binary", "data", b"{}\n"),
        ("facts/old_events.jsonl", "compat-file", "old_events.jsonl", "application/x-ndjson", "binary", "data", b"{}\n"),
    ]

    previous_status = {
        "state": "COMPLETE",
        "current_release_id": "v2.1.4",
        "code_commit": "b" * 40,
        "artifacts": [
            {"id": fid, "name": name, "sha256": orch.digest(content)}
            for _lk, fid, name, _mime, _mode, _parent, content in artifacts_meta
        ],
    }
    previous_targets = {
        fid: {
            "mime": mime,
            "mode": mode,
            "allowed_parents": [parent, "staging"],
            "publish_parent": parent,
        }
        for _lk, fid, _name, mime, mode, parent, _content in artifacts_meta
    }
    proj = orch.project_targets(
        release_id="v2.1.5-1",
        engine_sha="e" * 40,
        status_id="status",
        archive_id="archive",
        previous_status=previous_status,
        previous_targets=previous_targets,
        manifest_rows=inputs["manifest_rows"],
        tracked=[],
        tracked_hashes={},
        code_parent="scripts",
        declared_consumer_targets=consumer_targets,
    )
    assert proj["new_target_count"] == 6
    assert "consumer/master_domestic_2017_2027_current.csv" in proj["final_current_logical_keys"]
    assert "research/card_907766_2025_2026" in proj["final_current_logical_keys"]

    # Conflict check: declaring code/ or existing key fails closed
    with pytest.raises(orch.ProjectionError, match="DECLARED_TARGET_CONFLICT"):
        orch.project_targets(
            release_id="v2.1.5-1",
            engine_sha="e" * 40,
            status_id="status",
            archive_id="archive",
            previous_status=previous_status,
            previous_targets=previous_targets,
            manifest_rows=inputs["manifest_rows"],
            tracked=[],
            tracked_hashes={},
            code_parent="scripts",
            declared_consumer_targets={"derived/MASTER.csv": {"name": "x", "mime": "text/csv", "content": b"x"}},
        )

    reservations = {item["logical_key"]: f"res-{idx}" for idx, item in enumerate(proj["new_targets"])}
    allocation = {
        "release_id": "v2.1.5-1",
        "status_id": "status",
        "staging_id": "staging",
        "reservations": reservations,
        "semantic_delta_signature": proj["semantic_delta_signature"],
    }
    bytes_by_id = {fid: content for _lk, fid, _name, _mime, _mode, _parent, content in artifacts_meta}

    def fake_snapshot(_drive, file_id, _mode="binary"):
        return bytes_by_id[file_id], {"id": file_id}

    monkeypatch.setattr(orch, "snapshot", fake_snapshot)
    out_dir = tmp_path / "cand_out"
    consumer_contents = {k: v["content"] for k, v in consumer_targets.items()}
    entries = orch._candidate_inputs(
        object(), tmp_path, proj, allocation, out_dir,
        consumer_target_contents=consumer_contents,
    )
    by_key = {e["logical_key"]: e for e in entries}

    # Verify control docs ALL got dynamic 3,465 count (even CURRENT_VERSION_DOC sorting before derived/MASTER.csv!)
    for ctrl_key in ("CURRENT_VERSION_DOC", "entry/README", "entry/context", "derived/INDEX.md"):
        txt = Path(by_key[ctrl_key]["path"]).read_text(encoding="utf-8")
        assert "3,465 条" in txt, f"{ctrl_key} missing dynamic 3,465 count"
        assert "date_year_inferred" in txt
        assert fake_drive_token_1 not in txt and fake_drive_token_2 not in txt

    # Verify promoted consumer targets marked NEW_CONSUMER_SURFACE & published in manifest.csv
    assert by_key["consumer/master_domestic_2017_2027_current.csv"]["change_class"] == "NEW_CONSUMER_SURFACE"
    assert by_key["research/card_907766_2025_2026"]["change_class"] == "NEW_CONSUMER_SURFACE"

    manifest_out = Path(by_key["input/manifest.csv"]["path"]).read_text(encoding="utf-8-sig")
    m_rows = {r["uid"]: r for r in csv.DictReader(io.StringIO(manifest_out))}
    assert m_rows["research/card_907766_2025_2026"]["status"] == "published"
    assert m_rows["consumer/source_registry_index_current.csv"]["published_release"] == "v2.1.5-1"

    # Tampered consumer content fails closed on hash mismatch
    tampered = dict(consumer_contents)
    tampered["consumer/field_dictionary_current.md"] = b"tampered"
    with pytest.raises(orch.ProjectionError, match="CONSUMER_SURFACE_HASH_MISMATCH"):
        orch._candidate_inputs(
            object(), tmp_path, proj, allocation, tmp_path / "cand_tampered",
            consumer_target_contents=tampered,
        )


# ---------------------------------------------------------------------------
# 3. Oracle v8 end-to-end formal approved chain + negative spoofing tests
# ---------------------------------------------------------------------------


def test_wiring_oracle_v8_approved_chain_inline_citations_and_spoofing_rejection():
    locators = (
        "研究卡_林测试_907766_2025-2026_CURRENT.md",
        "00_README_先读这个",
        "03_DATA_SYNTH_2025-2026.json",
    )
    o = oracle.build_oracle(
        version="v2.1.5-b2-synth",
        sources=[oracle.OracleSource("src-1", "Synth official slice", "LAWFUL_PUBLIC_SYNTHETIC")],
        questions=[
            oracle.OracleQuestion(
                question_id="B2-Q10",
                question_text="Regular season PPG and team?",
                comparison="rate",
                expected=20.1,
                unit="per_game",
                precision=1,
                tolerance=0.05,
                mandatory_locator=True,
                required_scope_tokens=("2025-2026", "41场", "深圳马可波罗"),
                expected_posture="QUIET_CLEAN_OFFICIAL",
                designated_locators=("研究卡_林测试_907766_2025-2026_CURRENT.md",),
            )
        ],
    )
    assert o.to_manifest()["scoring_spec_version"] == "oracle-scoring-v8"
    snap_digest = "a" * 64
    approval = oracle.ScoringApproval(
        manifest_digest=o.artifact_digest,
        version=o.version,
        scoring_code_sha=oracle.scoring_code_sha(),
        snapshot_digest=snap_digest,
        authorization_domain="synth-v215-b2",
        locator_set_digest=oracle.locator_set_digest(locators),
    )

    drive_prefix = "https://" + "drive.google.com/file/d/"
    verbatim_pass = (
        f"2025-2026 赛季常规赛（41场）效力深圳马可波罗期间场均得分 20.1 分"
        f"（参见 [研究卡_林测试_907766_2025-2026_CURRENT.md]({drive_prefix}synth-card-1/view?page=12) "
        f"及 03-原始数据说明，来源 00_README_先读这个）。"
    )
    receipt = oracle.build_v0123_client_receipt(
        engine_sha="f" * 40,
        snapshot_digest=snap_digest,
        manifest_digest=o.artifact_digest,
        question_set_version=o.version,
        authorization_domain="synth-v215-b2",
        question_id="B2-Q10",
        verbatim_answer=verbatim_pass,
        authorized_locators=locators,
    )
    assert receipt.citation_protocol == "INLINE_LINK_CHIP_V0123"
    assert oracle.validate_receipt_approved(
        receipt,
        oracle=o,
        approval=approval,
        snapshot_digest=snap_digest,
        authorization_domain="synth-v215-b2",
        authorized_locators=locators,
    ) == []

    verdict = oracle.score_answer_approved(
        o.question("B2-Q10"),
        {
            "text": receipt.verbatim_answer,
            "value": 20.1,
            "unit": "per_game",
            "cited_locators": list(receipt.cited_locators),
        },
        oracle=o,
        approval=approval,
        snapshot_digest=snap_digest,
        authorized_locators=locators,
    )
    assert verdict.verdict == "PASS", verdict

    # Negative 1: Plain prose without Drive/Docs inline link must extract () and fail approved validation/scoring
    prose_only = "2025-2026 赛季常规赛（41场）效力深圳马可波罗，场均得分 20.1（来源文件：研究卡_林测试_907766_2025-2026_CURRENT.md）。"
    prose_receipt = oracle.build_v0123_client_receipt(
        engine_sha="f" * 40,
        snapshot_digest=snap_digest,
        manifest_digest=o.artifact_digest,
        question_set_version=o.version,
        authorization_domain="synth-v215-b2",
        question_id="B2-Q10",
        verbatim_answer=prose_only,
        authorized_locators=locators,
    )
    assert prose_receipt.cited_locators == ()
    assert oracle.validate_receipt_approved(
        prose_receipt,
        oracle=o,
        approval=approval,
        snapshot_digest=snap_digest,
        authorization_domain="synth-v215-b2",
        authorized_locators=locators,
    ) != []
    prose_verdict = oracle.score_answer_approved(
        o.question("B2-Q10"),
        {"text": prose_only, "value": 20.1, "unit": "per_game", "cited_locators": list(prose_receipt.cited_locators)},
        oracle=o,
        approval=approval,
        snapshot_digest=snap_digest,
        authorized_locators=locators,
    )
    assert prose_verdict.verdict == "FAIL" and prose_verdict.dimension == "locator"

    # Negative 2: Manually spoofing cited_locators under INLINE_LINK_CHIP_V0123 without an inline link fails validation
    spoofed = oracle.ClientReceipt(
        engine_sha="f" * 40,
        snapshot_digest=snap_digest,
        manifest_digest=o.artifact_digest,
        question_set_version=o.version,
        authorization_domain="synth-v215-b2",
        question_id="B2-Q10",
        verbatim_answer=prose_only,
        cited_locators=("研究卡_林测试_907766_2025-2026_CURRENT.md",),
        receipt_mode="VERBATIM",
        citation_protocol="INLINE_LINK_CHIP_V0123",
    )
    errs = oracle.validate_receipt_approved(
        spoofed,
        oracle=o,
        approval=approval,
        snapshot_digest=snap_digest,
        authorization_domain="synth-v215-b2",
        authorized_locators=locators,
    )
    assert errs and "inline link/chip" in errs[0]

    # Negative 3: Unsupported citation protocol raises OracleError
    with pytest.raises(oracle.OracleError, match="unsupported citation_protocol"):
        oracle.ClientReceipt(
            engine_sha="f" * 40,
            snapshot_digest=snap_digest,
            manifest_digest=o.artifact_digest,
            question_set_version=o.version,
            authorization_domain="synth-v215-b2",
            question_id="B2-Q10",
            verbatim_answer=verbatim_pass,
            cited_locators=("研究卡_林测试_907766_2025-2026_CURRENT.md",),
            citation_protocol="UNKNOWN_PROTO",
        )


# ---------------------------------------------------------------------------
# 4. Governance T2 tiering & gate selftest wiring
# ---------------------------------------------------------------------------


def test_wiring_prepare_v215_path_is_t2_and_gate_selftest_passes():
    spec = importlib.util.spec_from_file_location(
        "tiering", ROOT / ".github/scripts/tiering.py"
    )
    assert spec is not None and spec.loader is not None
    tiering = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tiering)

    tiers = tiering.get_tiers()
    assert tiering.file_tier("src/cba_kb/orchestration/release/prepare_v215.py", tiers) == "T2"
    assert tiering.compute_tier(
        [
            "src/cba_kb/consumer_surface.py",
            "src/cba_kb/orchestration/release/prepare_v215.py",
            "README.md",
        ],
        tiers,
    ) == "T2"

    result = subprocess.run(
        [sys.executable, ".github/scripts/gate_selftest.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "ALL NEGATIVE TESTS PASS" in result.stdout
