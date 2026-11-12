"""Differential falsification, local-projection wiring, and fail-closed security tests
for REQ-213 `kc_consume.ConsumeSession.consume_answer` `scope` shadowing repair (#239).

Root cause on `85b17891527df7e8487863b380d5107ac8d83768`:
Inside `ConsumeSession.consume_answer`, when `fact_valid` is `True`, the `fact_guarantee`
check bound `scope = fact_guarantee.get("field_scope")` (a `list` of Fact field names),
shadowing the `scope: str` parameter (`"WHOLE_SEASON"` / `"TEAM_SPLIT"`) later passed to
`classify_stats_evidence(..., scope=scope, ...)`. That caused `evidence_transparency`
and `research_card.md` to degrade to `UNKNOWN` whenever `CANONICAL_FACT` passed the
guarantee gate, contradicting `grains.STATS.status == "OK"`.
"""

from __future__ import annotations

import copy
import json

import pytest

from cba_kb.kc_cleanroom import A, SEASON, build_cleanroom_fixture
from cba_kb.kc_consume import ConsumeSession, KcConsumeError, _content_digest
from cba_kb.kc_local_producer import build_local_projection, verify_local_projection
from cba_kb.kc_passages import build_routed_passage, evidence_chain
from cba_kb.kc_projection import sha256_hex
from cba_kb.kc_render import parse_rendered
from cba_kb.kc_snapshot import build_fact_guarantee, build_snapshot
from tests.test_req213_acceptance import _bound_session


def test_guaranteed_fact_whole_season_does_not_shadow_scope_in_consume_answer():
    fx = build_cleanroom_fixture()
    session, _snap, passage, chain, fact, guarantee = _bound_session(fx)

    ans = session.consume_answer(
        records=fx["stats_records"],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        provider="CBA_OFFICIAL",
        competition="CBA",
        metric="points_per_game",
        fact=fact,
        passage=passage,
        source_text=passage["text"],
        chain=chain,
        claims=[],
        fact_guarantee=guarantee,
    )

    assert ans["payload"]["grains"]["CANONICAL_FACT"].get("fact_id") == "v213_fact"
    assert ans["payload"]["grains"]["STATS"]["status"] == "OK"
    expected_val = ans["payload"]["grains"]["STATS"]["value"]
    assert ans["payload"]["evidence_transparency"]["posture"] == "CLEAN_OFFICIAL"
    assert ans["payload"]["evidence_transparency"]["adopted_value"] == expected_val

    card_md = session.render_verified(ans, kind="research_card")
    assert "- Posture: CLEAN_OFFICIAL" in card_md
    assert f"- Metric: points_per_game = {expected_val}" in card_md
    assert "- Scope: WHOLE_SEASON" in card_md
    assert "Posture: UNKNOWN" not in card_md
    assert parse_rendered(card_md, kind="markdown")["end_marker"] is True


def test_guaranteed_fact_team_split_preserves_scope_parameter():
    fx = build_cleanroom_fixture()
    split_rec = copy.deepcopy(fx["stats_records"][1])
    team_id = split_rec["team_id"]

    passage = build_routed_passage(
        source_id="fixture_doc_D1",
        revision="rev-1",
        locator="P3",
        text=fx["passages"][0]["text"],
        player_uid=A,
        aliases=["测试甲"],
        title="fixture_doc_D1",
        authorized_url="fixture://authorized-copy/fixture_doc_D1",
    )
    probe = ConsumeSession(
        target_domain="fixture_target",
        authorized_players={A},
        authorized_sources={"fixture_doc_D1"},
        authorized_objects={"fixture_doc_D1"},
    )
    probed = probe.consume_slice(
        [split_rec],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="TEAM_SPLIT",
        team_id=team_id,
        provider="CBA_OFFICIAL",
        competition="CBA",
    )
    slice_digest = _content_digest(probed["payload"])
    snap = build_snapshot(
        entries=[
            {
                "entry_id": "entry-stats-split",
                "sha256": slice_digest,
                "authority": "canonical",
                "rights": "public",
                "source_id": "fixture_stats",
            },
            {
                "entry_id": "entry-doc-D1",
                "sha256": passage["digest"],
                "authority": "canonical",
                "rights": "public",
                "source_id": "fixture_doc_D1",
            },
        ],
        target_domain="fixture_target",
        capabilities=["stats"],
        build_provenance="simulated-build-split",
        build_time="2026-11-12T00:00:00+00:00",
        data_release="SIMULATED_DATA_RELEASE_1",
        engine_candidate="SIMULATED_ENGINE_CANDIDATE_1",
        build_code="SIMULATED_BUILD_CODE_1",
    )
    session = ConsumeSession(
        target_domain="fixture_target",
        authorized_players={A},
        authorized_sources={"fixture_doc_D1"},
        authorized_objects={"fixture_doc_D1"},
    )
    session.bind_current(snap)
    session.register_passages([passage])
    binding = session.current_binding()
    chain = evidence_chain(
        receipt_id=binding["receipt_binding"],
        snapshot_id=snap.snapshot_id,
        build_version=snap.build_code,
        source_digest=passage["digest"],
        authorization_basis=binding["authorization_basis"],
    )
    fact = {
        "fact_id": "v213_split_fact",
        "fact_version": "v1",
        "player_uid": A,
        "season": SEASON,
        "team_id": team_id,
        "kind": "REGISTRATION",
        "source": "fixture_doc_D1",
    }
    guarantee = build_fact_guarantee(
        fact_id=fact["fact_id"],
        fact_version="v1",
        source_id=fact["source"],
        file_version=passage["revision"],
        field_scope=sorted(fact.keys()),
        authorization_domain="fixture_target",
        snapshot_id=snap.snapshot_id,
        source_digest=passage["digest"],
    )

    ans = session.consume_answer(
        records=[split_rec],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="TEAM_SPLIT",
        team_id=team_id,
        provider="CBA_OFFICIAL",
        competition="CBA",
        metric="points_per_game",
        fact=fact,
        passage=passage,
        source_text=passage["text"],
        chain=chain,
        claims=[],
        fact_guarantee=guarantee,
    )

    assert ans["payload"]["grains"]["CANONICAL_FACT"].get("fact_id") == "v213_split_fact"
    assert ans["payload"]["grains"]["STATS"]["status"] == "OK"
    assert ans["payload"]["evidence_transparency"]["posture"] == "CLEAN_OFFICIAL"
    card_md = session.render_verified(ans, kind="research_card")
    assert "- Posture: CLEAN_OFFICIAL" in card_md
    assert "- Scope: TEAM_SPLIT" in card_md


def test_guaranteed_fact_local_projection_e2e_emits_clean_official_and_verifies_nine_artifacts(
    tmp_path,
):
    fx = build_cleanroom_fixture()
    passage_source = fx["passages"][0]["source_id"]
    fx["fact"] = {
        "fact_id": "guaranteed_proj_fact_213",
        "fact_version": "v1",
        "player_uid": fx["players"][0]["player_uid"],
        "season": fx["stats_records"][0]["season"],
        "team_id": fx["stats_records"][0]["team_id"],
        "kind": "REGISTRATION",
        "source": passage_source,
    }

    out_dir = tmp_path / "proj_p03"
    summary = build_local_projection(
        fixture=fx,
        output_dir=str(out_dir),
        engine_candidate="req213-scope-shadowing-fix",
    )
    assert summary["answer_fact_status"] == "OK"
    assert summary["answer_fact_guaranteed"] is True

    verified = verify_local_projection(str(out_dir))
    assert verified["valid"] is True
    assert verified["failures"] == []

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["files"]) == 9
    for entry in manifest["files"]:
        raw = (out_dir / entry["path"]).read_bytes()
        assert len(raw) == entry["bytes"]
        assert sha256_hex(raw) == entry["sha256"]

    card_md = (out_dir / "research_card.md").read_text(encoding="utf-8")
    assert "- Posture: CLEAN_OFFICIAL" in card_md
    assert "Posture: UNKNOWN" not in card_md

    answer_doc = json.loads((out_dir / "answer.json").read_text(encoding="utf-8"))
    assert answer_doc["grains"]["STATS"]["status"] == "OK"
    assert answer_doc["evidence_transparency"]["posture"] == "CLEAN_OFFICIAL"


def test_fail_closed_incomplete_or_malformed_field_scope_still_rejects_fact_only():
    fx = build_cleanroom_fixture()
    session, snap, passage, chain, fact, guarantee = _bound_session(fx)

    # Attack 1: valid sidecar digest, but field_scope omits a Fact field ("team_id")
    incomplete_fields = [k for k in sorted(fact.keys()) if k != "team_id"]
    incomplete_guarantee = build_fact_guarantee(
        fact_id=fact["fact_id"],
        fact_version="v1",
        source_id=fact["source"],
        file_version=passage["revision"],
        field_scope=incomplete_fields,
        authorization_domain="fixture_target",
        snapshot_id=snap.snapshot_id,
        source_digest=passage["digest"],
    )
    ans_inc = session.consume_answer(
        records=fx["stats_records"],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        provider="CBA_OFFICIAL",
        competition="CBA",
        metric="points_per_game",
        fact=fact,
        passage=passage,
        source_text=passage["text"],
        chain=chain,
        claims=[],
        fact_guarantee=incomplete_guarantee,
    )
    assert ans_inc["payload"]["grains"]["CANONICAL_FACT"]["status"] == "UNKNOWN"
    assert session._last_diagnostic["cause"] == "FACT_GUARANTEE_SCOPE_INCOMPLETE"
    assert ans_inc["payload"]["grains"]["STATS"]["status"] == "OK"
    assert ans_inc["payload"]["evidence_transparency"]["posture"] == "CLEAN_OFFICIAL"

    # Attack 2: tampered field_scope string / version mismatch fails closed
    tampered_version = copy.deepcopy(guarantee)
    fact_bad_ver = copy.deepcopy(fact)
    fact_bad_ver["fact_version"] = "v2"
    ans_ver = session.consume_answer(
        records=fx["stats_records"],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        provider="CBA_OFFICIAL",
        competition="CBA",
        metric="points_per_game",
        fact=fact_bad_ver,
        passage=passage,
        source_text=passage["text"],
        chain=chain,
        claims=[],
        fact_guarantee=tampered_version,
    )
    assert ans_ver["payload"]["grains"]["CANONICAL_FACT"]["status"] == "UNKNOWN"


def test_post_consumption_revocation_still_blocks_research_card_render():
    fx = build_cleanroom_fixture()
    session, _snap, passage, chain, fact, guarantee = _bound_session(fx)

    ans = session.consume_answer(
        records=fx["stats_records"],
        player_uid=A,
        season=SEASON,
        match_type="REGULAR",
        scope="WHOLE_SEASON",
        provider="CBA_OFFICIAL",
        competition="CBA",
        metric="points_per_game",
        fact=fact,
        passage=passage,
        source_text=passage["text"],
        chain=chain,
        claims=[],
        fact_guarantee=guarantee,
    )
    assert "- Posture: CLEAN_OFFICIAL" in session.render_verified(ans, kind="research_card")

    session.authorized_sources.discard("fixture_doc_D1")
    with pytest.raises(KcConsumeError, match="ARTIFACT_SOURCE_NOT_AUTHORIZED"):
        session.render_verified(ans, kind="research_card")

    session.authorized_sources.add("fixture_doc_D1")
    session.authorized_players.discard(A)
    with pytest.raises(KcConsumeError, match="ARTIFACT_STATS_SUBJECT_NOT_AUTHORIZED"):
        session.render_verified(ans, kind="research_card")
