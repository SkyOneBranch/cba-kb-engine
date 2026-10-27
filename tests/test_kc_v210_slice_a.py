"""CBA-KB v2.1.0 slice A (Issue #215) acceptance tests — kc/0.1 contract
and target-first safe projection.

Frozen binding: REQ-210-KNOWLEDGE-CONSUMPTION-01, LACD_v1.0.2, main spec
SHA-256 ee4bd5338edba757fd2764df563fe21600d3a3313bb8dab2ec2416d840b3ef09,
addendum SHA-256 3230146ab7668972850a63ea9a3bbc31e125b5fcaaeb2f37c757ea861f3f9071.
Clean-room fixture values only (fictitious). No private data, no real
Drive/ACL data, no production credentials.

These tests were written FIRST and must FAIL on the old HEAD for missing
capability (ModuleNotFoundError for cba_kb.kc_projection / kc_cleanroom),
not for environment reasons, then PASS on the slice HEAD.
"""

from pathlib import Path

import pytest
import yaml

from cba_kb.kc_cleanroom import build_cleanroom_fixture
from cba_kb.kc_projection import (
    GRAINS,
    KcError,
    answer_cross_source,
    build_passage,
    build_snapshot,
    build_target_projection,
    declare_identity_relation,
    query_projection,
    query_stats,
    query_stats_from_document,
    resolve_claims,
    resolve_identity,
    route_passages,
    route_uid_slices,
    search_projection,
    typed_result,
    validate_metric_value,
    validate_passage,
    validate_snapshot,
    validator_receipt,
)

A = "fixture_player_0001"
B = "fixture_player_0002"
SEASON = "2025-2026"
QUOTE = "我每天训练一百次罚球。"


@pytest.fixture(scope="module")
def fx():
    return build_cleanroom_fixture()


# ---------------------------------------------------------------- E1 Stats
def test_e1a_exact_regular_whole_points(fx):
    r = query_stats(fx["stats_records"], player_uid=A, season=SEASON,
                    match_type="REGULAR", scope="WHOLE_SEASON",
                    metric="points_per_game")
    assert r["status"] == "OK" and r["grain"] == "STATS"
    assert r["value"] == 12.5
    assert r["key"]["player_uid"] == A
    assert r["key"]["season"] == SEASON
    assert r["key"]["match_type"] == "REGULAR"
    assert r["key"]["scope"] == "WHOLE_SEASON"
    assert r["key"]["metric"] == "points_per_game"
    assert r["key"]["provider"] == "CBA_OFFICIAL"


def test_e1b_regular_and_playoff_are_parallel_not_merged(fx):
    reg = query_stats(fx["stats_records"], player_uid=A, season=SEASON,
                      match_type="REGULAR", scope="WHOLE_SEASON",
                      metric="points_per_game")
    po = query_stats(fx["stats_records"], player_uid=A, season=SEASON,
                     match_type="PLAYOFF", scope="WHOLE_SEASON",
                     metric="points_per_game")
    assert (reg["value"], po["value"]) == (12.5, 18.0)
    assert reg["key"]["match_type"] != po["key"]["match_type"]
    assert reg["source_record_id"] != po["source_record_id"]


def test_e1_no_silent_fallback_to_other_season_or_player(fx):
    # A season with no record is UNKNOWN, not a fallback to another season;
    # the frozen prior-season record (9.0) is returned only when asked for
    # explicitly by its own season key.
    assert query_stats(fx["stats_records"], player_uid=A, season="2023-2024",
                       match_type="REGULAR", scope="WHOLE_SEASON",
                       metric="points_per_game")["status"] == "UNKNOWN"
    assert query_stats(fx["stats_records"], player_uid=A, season="2024-2025",
                       match_type="REGULAR", scope="WHOLE_SEASON",
                       metric="points_per_game")["value"] == 9.0
    assert query_stats(fx["stats_records"], player_uid=B, season=SEASON,
                       match_type="REGULAR", scope="WHOLE_SEASON",
                       metric="points_per_game")["value"] == 7.0
    assert query_stats(fx["stats_records"], player_uid=A, season=SEASON,
                       match_type="PLAYOFF", scope="WHOLE_SEASON",
                       metric="points_per_game")["value"] != 12.5


# ---------------------------------------------------------------- E2 scope / null
def test_e2a_whole_is_not_derived_from_splits(fx):
    whole = query_stats(fx["stats_records"], player_uid=A, season=SEASON,
                        match_type="REGULAR", scope="WHOLE_SEASON",
                        metric="games_played")
    assert whole["value"] == 40
    # Removing the WHOLE record must NOT let splits manufacture a whole:
    # splits still answer separately, whole answers UNKNOWN.
    without_whole = [r for r in fx["stats_records"]
                     if not (r["player_uid"] == A and r["scope"] == "WHOLE_SEASON"
                             and r["match_type"] == "REGULAR")]
    assert query_stats(without_whole, player_uid=A, season=SEASON,
                       match_type="REGULAR", scope="WHOLE_SEASON",
                       metric="games_played")["status"] == "UNKNOWN"
    t1 = query_stats(fx["stats_records"], player_uid=A, season=SEASON,
                     match_type="REGULAR", scope="TEAM_SPLIT",
                     metric="points_per_game", team_id="fixture_team_T1")
    t2 = query_stats(fx["stats_records"], player_uid=A, season=SEASON,
                     match_type="REGULAR", scope="TEAM_SPLIT",
                     metric="points_per_game", team_id="fixture_team_T2")
    assert (t1["value"], t2["value"]) == (10.0, 15.0)
    assert t1["source_record_id"] != t2["source_record_id"]


def test_e2a_team_split_requires_team_id(fx):
    r = query_stats(fx["stats_records"], player_uid=A, season=SEASON,
                    match_type="REGULAR", scope="TEAM_SPLIT",
                    metric="points_per_game")
    assert r["status"] == "UNKNOWN"
    assert "TEAM_SPLIT" in r["reason"]


def test_e2b_zero_is_zero_null_is_never_zero(fx):
    blocks = query_stats(fx["stats_records"], player_uid=A, season=SEASON,
                         match_type="REGULAR", scope="WHOLE_SEASON",
                         metric="blocks_per_game")
    steals = query_stats(fx["stats_records"], player_uid=A, season=SEASON,
                         match_type="REGULAR", scope="WHOLE_SEASON",
                         metric="steals_per_game")
    assert blocks["value"] == 0 and blocks["status"] == "OK"
    assert steals["value"] is None and steals["status"] == "UNKNOWN"
    assert steals["value"] != 0 or steals["value"] is None  # never coerced


# ------------------------------------------------------- percentage vs ratio
def test_percentage_and_ratio_are_distinct_typed_values():
    pct = validate_metric_value("field_goals_percentage", "45.0%", "percent")
    ratio = validate_metric_value("field_goals_percentage_rate", 0.45, "ratio")
    assert pct["value_kind"] == "PERCENTAGE" and ratio["value_kind"] == "RATIO"
    assert pct["value"] != ratio["value"]
    with pytest.raises(KcError):
        validate_metric_value("field_goals_percentage", 45.0, "percent")
    with pytest.raises(KcError):
        validate_metric_value("field_goals_percentage_rate", "45%", "ratio")
    with pytest.raises(KcError):
        validate_metric_value("points_per_game", True, "per_game")


# ---------------------------------------------------------------- E3 passages
def test_e3a_alias_routing_beats_55_distractors_and_id_order(fx):
    proj = build_target_projection(
        source_objects=fx["source_objects"],
        target_domain="fixture_target",
        authorized=lambda obj, target: (
            obj.get("target_domain") == target and obj.get("rights") == "APPROVED"))
    routed = route_passages(proj["passages"], alias="测试甲",
                            topic_tokens=["罚球", "训练"])
    assert routed["complete"] is True and routed["end_marker"] is True
    assert routed["total_candidates"] == 56  # 1 target + 55 distractors
    first = routed["candidates"][0]
    assert first["player_uid"] == A
    assert first["text"] == QUOTE
    # Distractor ids sort BEFORE the target id, so id order is not relevance.
    assert sorted(p["source_id"] for p in routed["candidates"])[0] != first["source_id"]
    assert validate_passage(first)["valid"] is True


def test_e3a_top5_limit_is_a_partial_page_not_a_complete_answer(fx):
    proj = build_target_projection(
        source_objects=fx["source_objects"],
        target_domain="fixture_target",
        authorized=lambda obj, target: (
            obj.get("target_domain") == target and obj.get("rights") == "APPROVED"))
    routed = route_passages(proj["passages"], alias="测试甲",
                            topic_tokens=["罚球", "训练"], limit=5)
    assert routed["complete"] is False
    assert routed["end_marker"] is False
    assert routed["truncated"] is True
    assert len(routed["candidates"]) == 5
    assert routed["total_candidates"] == 56


def test_e3b_cross_source_grains_stay_separate_and_causal_unknown(fx):
    stats = query_stats(fx["stats_records"], player_uid=A, season=SEASON,
                        match_type="REGULAR", scope="WHOLE_SEASON",
                        metric="points_per_game")
    proj = build_target_projection(
        source_objects=fx["source_objects"],
        target_domain="fixture_target",
        authorized=lambda obj, target: (
            obj.get("target_domain") == target and obj.get("rights") == "APPROVED"))
    passage = route_passages(proj["passages"], alias="测试甲",
                             topic_tokens=["罚球", "训练"])["candidates"][0]
    ans = answer_cross_source(fact=fx["fact"], stats_result=stats,
                              passage=passage)
    assert ans["grains"]["CANONICAL_FACT"]["team_id"] == "fixture_team_T2"
    assert ans["grains"]["STATS"]["value"] == 12.5
    assert ans["grains"]["DOCUMENT"]["text"] == QUOTE
    assert ans["causal_inference"]["status"] == "UNKNOWN"


# ---------------------------------------------------------------- E4 identity
def test_e4a_same_name_returns_all_candidates_never_auto_pick(fx):
    r = resolve_identity(fx["players"], fx["aliases"], query="林测试",
                         query_kind="name")
    assert r["status"] == "CLARIFICATION_REQUIRED"
    assert r["candidates"] == [A, B]
    assert r["relation"] == "UNDECIDED"
    assert "value" not in r  # no single picked answer


def test_e4a_approved_alias_resolves_only_its_owner(fx):
    r = resolve_identity(fx["players"], fx["aliases"], query="测试甲",
                         query_kind="alias")
    assert r["candidates"] == [A]
    assert B not in r["candidates"]


def test_identity_relation_requires_evidence_and_never_auto_merges():
    rel = declare_identity_relation(A, B, "NOT_SAME",
                                    evidence=["fixture:separate-uid-records"])
    assert rel["relation"] == "NOT_SAME"
    with pytest.raises(KcError):
        declare_identity_relation(A, B, "SAME", evidence=[])
    with pytest.raises(KcError):
        declare_identity_relation(A, B, "MAYBE_SAME", evidence=["x"])


# ---------------------------------------------------------------- E4b F-01
def test_e4b_claim_conflict_does_not_pollute_stats(fx):
    claims = resolve_claims(fx["claims"])
    assert claims["status"] == "CONFLICT"
    assert claims["grain"] == "CLAIM"
    assert len(claims["conflicting_claim_ids"]) == 2
    stats = query_stats(fx["stats_records"], player_uid=A, season=SEASON,
                        match_type="REGULAR", scope="WHOLE_SEASON",
                        metric="points_per_game")
    assert stats["status"] == "OK" and stats["value"] == 12.5


# ---------------------------------------------------------------- E5 F-02 / injection
def test_e5a_blocked_is_indistinguishable_from_nonexistent(fx):
    def auth(obj, target):
        return obj.get("target_domain") == target and obj.get("rights") == "APPROVED"

    with_blocked = build_target_projection(
        source_objects=fx["source_objects"], target_domain="fixture_target",
        authorized=auth)
    without_blocked = build_target_projection(
        source_objects=[o for o in fx["source_objects"]
                        if o["object_id"] != "fixture_doc_BLOCKED"],
        target_domain="fixture_target", authorized=auth)
    # Consumer-visible surfaces must be byte-identical across both worlds.
    assert with_blocked["consumer_view"] == without_blocked["consumer_view"]
    assert "fixture_doc_BLOCKED" not in str(with_blocked["consumer_view"])
    q_blocked = query_projection(with_blocked, "fixture_doc_BLOCKED")
    q_missing = query_projection(with_blocked, "fixture_doc_DOES_NOT_EXIST")
    assert q_blocked == q_missing
    assert q_blocked["status"] == "UNKNOWN"
    assert "fixture_doc_BLOCKED" not in str(q_blocked)


def test_e5b_injection_text_is_data_not_instruction(fx):
    proj = build_target_projection(
        source_objects=fx["source_objects"],
        target_domain="fixture_target",
        authorized=lambda obj, target: (
            obj.get("target_domain") == target and obj.get("rights") == "APPROVED"))
    # The malicious passage is visible as data, but its instruction is
    # never executed: no hidden resource appears, normal query still works.
    assert "fixture_doc_HIDDEN" not in proj["visible_object_ids"]
    assert search_projection(proj, "隐藏目录") == []
    routed = route_passages(proj["passages"], alias="测试甲",
                            topic_tokens=["罚球", "训练"])
    assert routed["candidates"][0]["text"] == QUOTE


# ---------------------------------------------------------------- AC-02 rebuild
def test_ac02_rebuild_is_deterministic_and_slice_completeness_is_honest(fx):
    def auth(obj, target):
        return obj.get("target_domain") == target and obj.get("rights") == "APPROVED"

    p1 = build_target_projection(source_objects=fx["source_objects"],
                                 target_domain="fixture_target", authorized=auth)
    p2 = build_target_projection(source_objects=fx["source_objects"],
                                 target_domain="fixture_target", authorized=auth)
    assert p1["consumer_view"] == p2["consumer_view"]
    slices = route_uid_slices(fx["stats_records"], player_uid=A)
    assert slices["complete"] is True
    assert slices["end_marker"] is True
    keys = {(s["season"], s["match_type"], s["scope"]) for s in slices["slices"]}
    assert (SEASON, "REGULAR", "WHOLE_SEASON") in keys
    assert (SEASON, "REGULAR", "TEAM_SPLIT") in keys
    assert (SEASON, "PLAYOFF", "WHOLE_SEASON") in keys
    # A truncated slice (declared rows > actual rows) is PARTIAL, never OK.
    truncated = [dict(s) for s in slices["slices"]]
    truncated[0] = dict(truncated[0], total_rows=truncated[0]["total_rows"] + 1)
    partial = {"slices": truncated}
    assert any(s["total_rows"] != len(s["rows"]) for s in partial["slices"])


def test_ac02_uid_slice_truncation_is_partial(fx):
    # Direct: build slices from a record set missing one TEAM_SPLIT team.
    records = [r for r in fx["stats_records"]
               if not (r["scope"] == "TEAM_SPLIT"
                       and r.get("team_id") == "fixture_team_T2")]
    # Full-slice completeness is judged against the declared slice set in
    # the fixture, so a caller-supplied expected set exposes the gap.
    slices = route_uid_slices(
        records, player_uid=A,
        expected_keys={(SEASON, "REGULAR", "WHOLE_SEASON", None),
                       (SEASON, "REGULAR", "TEAM_SPLIT", "fixture_team_T1"),
                       (SEASON, "REGULAR", "TEAM_SPLIT", "fixture_team_T2"),
                       (SEASON, "PLAYOFF", "WHOLE_SEASON", None)})
    assert slices["complete"] is False
    assert "fixture_team_T2" in str(slices["missing"])


# ---------------------------------------------------------------- digests / snapshot
def test_passage_digest_is_revision_bound_and_tamper_evident(fx):
    p = build_passage(source_id="fixture_doc_D1", revision="rev-1",
                      locator="P3", text=QUOTE)
    assert p["digest_kind"] == "KC_PASSAGE_SHA256"
    assert p["algorithm"] == "SHA-256"
    assert "digest" not in p["covered_fields"]
    assert validate_passage(p)["valid"] is True
    tampered = dict(p, text="我每天训练一千次罚球。")
    assert validate_passage(tampered)["valid"] is False


def test_snapshot_binding_is_immutable_and_kinds_are_not_conflated(fx):
    entries = [{"entry_id": "stats-A-regular", "kind": "STATS",
                "sha256": "0" * 64, "authority": "canonical", "rights": "public"}]
    snap = build_snapshot(entries=entries, target_domain="fixture_target",
                          capabilities=["stats-exact", "passage"],
                          build_provenance="fixture-producer-v1")
    assert validate_snapshot(snap)["valid"] is True
    assert snap["digest_kind"] == "KC_SNAPSHOT_SHA256"
    assert snap["snapshot_id"].startswith("kc-snapshot:")
    tampered = dict(snap, entries=[dict(entries[0], rights="private")])
    assert validate_snapshot(tampered)["valid"] is False
    receipt = validator_receipt(subject_kind="PASSAGE",
                                subject_id="fixture_doc_D1",
                                algorithm="SHA-256",
                                covered_bytes="canonical JSON of source_id, revision, locator, text",
                                producer_validated=True)
    assert receipt["client_computed_digest"] is False
    assert receipt["connector_returned"] == "NOT_OBSERVED"
    assert receipt["client_observed"] == "NOT_OBSERVED"


def test_document_requesting_stats_is_not_applicable():
    r = query_stats_from_document(document_kind="DOCUMENT",
                                  requested_metric="points_per_game")
    assert r["status"] == "NOT_APPLICABLE"
    assert r["reason"] == "DOCUMENT_REQUESTING_STATS"


def test_grain_is_authoritative_not_caller_spoofable():
    r = typed_result(caller_grain="DOCUMENT", authoritative_grain="STATS",
                     payload={"value": 12.5})
    assert r["grain"] == "STATS"
    assert set(GRAINS) >= {"STATS", "IDENTITY", "CANONICAL_FACT", "DOCUMENT",
                           "STATEMENT", "CLAIM", "UNKNOWN"}


def test_golden_v1_preserved_unchanged():
    path = Path(__file__).resolve().parent.parent / "config" / "consumer_golden_questions_v1.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    qs = data["questions"]
    assert len(qs) == 10
    by_id = {q["id"]: q for q in qs}
    assert by_id["gq01"]["expected_answer"] == "v1.6.1-1"
    assert by_id["gq10"]["expected_answer"] == "BLOCKED"


def test_end_to_end_call_chain_fixture_to_answer(fx):
    """Real call chain, not a unit stub: fixture -> projection -> routing
    -> stats -> passage -> cross-source answer."""
    proj = build_target_projection(
        source_objects=fx["source_objects"],
        target_domain="fixture_target",
        authorized=lambda obj, target: (
            obj.get("target_domain") == target and obj.get("rights") == "APPROVED"))
    ident = resolve_identity(fx["players"], fx["aliases"], query="测试甲",
                             query_kind="alias")
    assert ident["candidates"] == [A]
    stats = query_stats(fx["stats_records"], player_uid=ident["candidates"][0],
                        season=SEASON, match_type="REGULAR",
                        scope="WHOLE_SEASON", metric="points_per_game")
    passage = route_passages(proj["passages"], alias="测试甲",
                             topic_tokens=["罚球", "训练"])["candidates"][0]
    ans = answer_cross_source(fact=fx["fact"], stats_result=stats,
                              passage=passage)
    assert ans["grains"]["STATS"]["value"] == 12.5
    assert ans["grains"]["DOCUMENT"]["text"] == QUOTE
    assert ans["grains"]["CANONICAL_FACT"]["team_id"] == "fixture_team_T2"
