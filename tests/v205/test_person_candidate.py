"""T2-4: Person Candidate layer tests (SYNTHETIC fixtures only).

AC coverage: AC-04~07 (candidate-only output never leaks or asserts a
Person identity), AC-23 (titles/aliases are searchable candidates only;
Person ID is never guessed), AC-24 (dual-source conflicts keep BOTH
records plus conflict status; last-write-wins is forbidden), AC-05
(malformed input -> explicit REJECTED, ambiguity -> REVIEW_REQUIRED,
never silent).

All players, IDs, teams and values are invented. Real CBA data
MUST NEVER enter this repo (AC-29).

Falsification: these tests were run against the unmodified base first
and failed with ModuleNotFoundError (no person_candidate module) before
implementation.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cba_kb.person_candidate import (
    PersonCandidateError,
    assess_batch,
    assess_pair,
)

FIXTURE = Path(__file__).parent / "fixtures" / "person_candidate_synth.json"


def load_sightings():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["sightings"]


def _sighting(name, source_id, **over):
    s = {
        "source_ref": source_id + "#loc",
        "source_id": source_id,
        "source_kind": "ARTICLE",
        "display_name": name,
    }
    s.update(over)
    return s


# ---------------------------------------------------------------- fixture

def test_fixture_is_synthetic():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert payload["schema"] == "person_candidate_synth_v1"
    assert payload["source_provider"] == "SYNTHETIC_TEST"
    for s in payload["sightings"]:
        assert "虚构" in s["display_name"] or s["display_name"] == "甲甲"
        ext = s.get("external_id")
        if ext is not None:
            assert ext.startswith("EXT-900"), "must use invented external IDs"


# ------------------------------------------------- ambiguity (fail-closed)

def test_same_name_different_source_is_ambiguous():
    """同名不同源 -> REVIEW_REQUIRED, never auto-merge (rule 1)."""
    out = assess_pair(
        _sighting("虚构球员甲", "synthetic://a"),
        _sighting("虚构球员甲", "synthetic://b"),
    )
    assert out["verdict"] == "AMBIGUOUS"
    assert out["status"] == "REVIEW_REQUIRED"
    assert out["automatic_merge_allowed"] is False
    assert out["identity_assertion"] == "NONE"
    assert "SAME_NAME_DIFFERENT_SOURCE" in out["ambiguity_signals"]


def test_nickname_overlap_is_ambiguous():
    """AC-23: an alias hit is a candidate signal only, never a Person ID."""
    out = assess_pair(
        _sighting("虚构球员甲", "synthetic://a", nicknames=["甲甲"]),
        _sighting("甲甲", "synthetic://b"),
    )
    assert out["verdict"] == "AMBIGUOUS"
    assert out["status"] == "REVIEW_REQUIRED"
    assert out["automatic_merge_allowed"] is False
    assert "NICKNAME_OVERLAP" in out["ambiguity_signals"]


def test_same_birth_month_does_not_resolve():
    """同月 is NOT disambiguating evidence: ambiguity survives (rule 1)."""
    out = assess_pair(
        _sighting("虚构球员甲", "synthetic://a", birth_month="1995-03"),
        _sighting("虚构球员甲", "synthetic://b", birth_month="1995-03"),
    )
    assert out["verdict"] == "AMBIGUOUS"
    assert out["automatic_merge_allowed"] is False


def test_same_team_does_not_resolve():
    """同球队 is NOT disambiguating evidence: ambiguity survives (rule 1)."""
    out = assess_pair(
        _sighting("虚构球员甲", "synthetic://a", team="虚构球队甲"),
        _sighting("虚构球员甲", "synthetic://b", team="虚构球队甲"),
    )
    assert out["verdict"] == "AMBIGUOUS"
    assert out["automatic_merge_allowed"] is False


def test_same_source_same_name_groups():
    """Same source + same name is intra-source grouping, allowed."""
    out = assess_pair(
        _sighting("虚构球员甲", "synthetic://a"),
        _sighting("虚构球员甲", "synthetic://a"),
    )
    assert out["verdict"] == "MERGE_ALLOWED"
    assert out["verdict_reason"] == "SAME_NAME_SAME_SOURCE"
    assert out["merged_view"]["source_refs"]  # provenance preserved


def test_missing_source_id_stays_ambiguous():
    """A name match without a source identity cannot group silently."""
    left = _sighting("虚构球员甲", "synthetic://a")
    right = _sighting("虚构球员甲", "synthetic://a")
    del right["source_id"]
    out = assess_pair(left, right)
    assert out["verdict"] == "AMBIGUOUS"
    assert out["automatic_merge_allowed"] is False


# ---------------------------------------------------------------- distinct

def test_different_birth_month_is_distinct():
    """One person has exactly one birth month: conflict -> split, kept."""
    out = assess_pair(
        _sighting("虚构球员甲", "synthetic://a", birth_month="1995-03"),
        _sighting("虚构球员甲", "synthetic://b", birth_month="1996-07"),
    )
    assert out["verdict"] == "DISTINCT"
    assert out["verdict_reason"] == "DIFFERENT_BIRTH_MONTHS"
    assert out["automatic_merge_allowed"] is False


def test_different_external_id_is_distinct():
    out = assess_pair(
        _sighting("虚构球员甲", "synthetic://a", external_id="EXT-9001"),
        _sighting("虚构球员甲", "synthetic://b", external_id="EXT-9002"),
    )
    assert out["verdict"] == "DISTINCT"
    assert out["verdict_reason"] == "DIFFERENT_EXTERNAL_IDS"


def test_unrelated_names_are_distinct():
    out = assess_pair(
        _sighting("虚构球员甲", "synthetic://a", team="虚构球队甲"),
        _sighting("虚构球员丙", "synthetic://b", team="虚构球队甲"),
    )
    assert out["verdict"] == "DISTINCT"
    assert out["automatic_merge_allowed"] is False


# ------------------------------------------------- conflict (AC-24, keep both)

def test_conflict_same_id_different_team_keeps_both():
    """AC-24: same authoritative ID, contradictory team -> CONFLICT.

    BOTH sightings are preserved with conflict status; no merged view,
    no winner.
    """
    left = _sighting("虚构球员乙", "synthetic://s1",
                     external_id="EXT-9001", team="虚构球队甲")
    right = _sighting("虚构球员乙", "synthetic://s2",
                      external_id="EXT-9001", team="虚构球队丙")
    out = assess_pair(left, right)
    assert out["verdict"] == "CONFLICT"
    assert out["status"] == "CONFLICT"
    assert out["conflict_fields"] == ["team"]
    assert out["sightings"] == [left, right]  # both kept, input order
    assert out["merged_view"] is None
    assert out["automatic_merge_allowed"] is False


def test_conflict_no_last_write_wins():
    """Swapping input order must not change the conflict outcome."""
    left = _sighting("虚构球员乙", "synthetic://s1",
                     external_id="EXT-9001", team="虚构球队甲")
    right = _sighting("虚构球员乙", "synthetic://s2",
                      external_id="EXT-9001", team="虚构球队丙")
    fwd = assess_pair(left, right)
    rev = assess_pair(right, left)
    assert fwd["verdict"] == rev["verdict"] == "CONFLICT"
    assert fwd["conflict_fields"] == rev["conflict_fields"]
    # The same two records survive in both directions: nobody "wins".
    fwd_refs = {s["source_ref"] for s in fwd["sightings"]}
    rev_refs = {s["source_ref"] for s in rev["sightings"]}
    assert fwd_refs == rev_refs == {left["source_ref"], right["source_ref"]}
    assert fwd["sightings"] == [left, right]  # input order preserved
    assert rev["sightings"] == [right, left]


def test_conflict_same_id_different_name():
    left = _sighting("虚构球员乙", "synthetic://s1", external_id="EXT-9001")
    right = _sighting("虚构球员丁", "synthetic://s2", external_id="EXT-9001")
    out = assess_pair(left, right)
    assert out["verdict"] == "CONFLICT"
    assert "display_name" in out["conflict_fields"]


# ------------------------------------------------------- merge-allowed paths

def test_authoritative_external_id_allows_merge():
    """Rule 3: an authoritative external ID association permits merge."""
    left = _sighting("虚构球员乙", "synthetic://s1",
                     external_id="EXT-9001", team="虚构球队甲")
    right = _sighting("虚构球员乙", "synthetic://s2",
                      external_id="EXT-9001", team="虚构球队甲")
    out = assess_pair(left, right)
    assert out["verdict"] == "MERGE_ALLOWED"
    assert out["verdict_reason"] == "AUTHORITATIVE_EXTERNAL_ID_MATCH"
    assert out["automatic_merge_allowed"] is True
    view = out["merged_view"]
    assert view["external_id"] == "EXT-9001"
    assert set(view["source_refs"]) == {left["source_ref"], right["source_ref"]}
    # Merge never promotes to a Person identity.
    assert out["identity_assertion"] == "NONE"
    assert "player_uid" not in view


def test_trusted_link_allows_merge():
    """Rule 3: a validated trusted association permits merge."""
    out = assess_pair(
        _sighting("虚构球员甲", "synthetic://a"),
        _sighting("虚构球员甲", "synthetic://b"),
        trusted_link={
            "method": "SOURCE_DECLARED",
            "confidence": "HIGH",
            "evidence_refs": ["synthetic://test/operator-note-1"],
        },
    )
    assert out["verdict"] == "MERGE_ALLOWED"
    assert out["verdict_reason"] == "TRUSTED_ASSOCIATION"


def test_malformed_trusted_link_rejected():
    """A trusted link without evidence is not trusted: fail-closed."""
    with pytest.raises(PersonCandidateError):
        assess_pair(
            _sighting("虚构球员甲", "synthetic://a"),
            _sighting("虚构球员甲", "synthetic://b"),
            trusted_link={"method": "SOURCE_DECLARED", "confidence": "HIGH"},
        )


def test_low_confidence_trusted_link_rejected():
    with pytest.raises(PersonCandidateError):
        assess_pair(
            _sighting("虚构球员甲", "synthetic://a"),
            _sighting("虚构球员甲", "synthetic://b"),
            trusted_link={
                "method": "SOURCE_DECLARED",
                "confidence": "LOW",
                "evidence_refs": ["synthetic://test/note"],
            },
        )


def test_human_decision_same_is_final_arbiter():
    """Rule 3: Human review is the final arbiter, even over ambiguity."""
    out = assess_pair(
        _sighting("虚构球员甲", "synthetic://a"),
        _sighting("甲甲", "synthetic://b"),
        human_decision={"link": "same"},
    )
    assert out["verdict"] == "MERGE_ALLOWED"
    assert out["verdict_reason"] == "HUMAN_DECISION_SAME"


def test_human_decision_not_same():
    out = assess_pair(
        _sighting("虚构球员甲", "synthetic://a"),
        _sighting("虚构球员甲", "synthetic://b"),
        human_decision={"link": "not_same"},
    )
    assert out["verdict"] == "DISTINCT"
    assert out["verdict_reason"] == "HUMAN_DECISION_NOT_SAME"


def test_invalid_human_decision_rejected():
    with pytest.raises(PersonCandidateError):
        assess_pair(
            _sighting("虚构球员甲", "synthetic://a"),
            _sighting("虚构球员甲", "synthetic://b"),
            human_decision={"link": "maybe"},
        )


# ---------------------------------------------------------------- validation

def test_invalid_sighting_rejected():
    with pytest.raises(PersonCandidateError):
        assess_pair({"source_ref": "x"}, _sighting("虚构球员甲", "synthetic://b"))
    with pytest.raises(PersonCandidateError):
        assess_pair("not-a-dict", _sighting("虚构球员甲", "synthetic://b"))


def test_invalid_birth_month_rejected():
    with pytest.raises(PersonCandidateError):
        assess_pair(
            _sighting("虚构球员甲", "synthetic://a", birth_month="March 1995"),
            _sighting("虚构球员甲", "synthetic://b"),
        )


# -------------------------------------------------------------------- batch

def test_batch_status_conflict_and_review():
    """Fixture batch: 乙 conflicts, 甲 is ambiguous -> batch CONFLICT."""
    out = assess_batch(load_sightings())
    assert out["status"] == "CONFLICT"
    assert out["identity_assertion"] == "NONE"
    verdicts = {g["verdict"] for g in out["groups"]}
    assert "CONFLICT" in verdicts
    assert "REVIEW_REQUIRED" in verdicts
    # Every sighting appears in exactly one group; none dropped silently.
    grouped = [s["source_ref"] for g in out["groups"] for s in g["sightings"]]
    assert sorted(grouped) == sorted(s["source_ref"] for s in load_sightings())


def test_batch_conflict_group_keeps_both_records():
    out = assess_batch(load_sightings())
    conflict = next(g for g in out["groups"] if g["verdict"] == "CONFLICT")
    assert len(conflict["sightings"]) == 2
    teams = {s["team"] for s in conflict["sightings"]}
    assert teams == {"虚构球队甲", "虚构球队丙"}  # both kept, no winner
    assert conflict["merged_view"] is None


def test_batch_review_group_has_no_merged_view():
    out = assess_batch(load_sightings())
    review = next(g for g in out["groups"] if g["verdict"] == "REVIEW_REQUIRED")
    assert review["merged_view"] is None
    assert review["automatic_merge_allowed"] is False


def test_batch_human_decision_resolves_ambiguity():
    sightings = load_sightings()
    a = next(s for s in sightings if s["source_ref"].endswith("article-001#line-12"))
    b = next(s for s in sightings if s["source_ref"].endswith("article-002#line-05"))
    out = assess_batch(
        [a, b],
        human_decisions={(a["source_ref"], b["source_ref"]): {"link": "same"}},
    )
    assert out["status"] == "RESOLVED"
    assert out["groups"][0]["verdict"] == "MERGE_ALLOWED"


def test_batch_is_deterministic():
    first = assess_batch(load_sightings())
    second = assess_batch(load_sightings())
    assert first == second


def test_never_asserts_identity_anywhere():
    """The candidate layer NEVER emits a Person identity (AC-23)."""
    pair_outs = [
        assess_pair(_sighting("虚构球员甲", "synthetic://a"),
                    _sighting("虚构球员甲", "synthetic://b")),
        assess_pair(_sighting("虚构球员乙", "synthetic://s1", external_id="EXT-9001"),
                    _sighting("虚构球员乙", "synthetic://s2", external_id="EXT-9001")),
        assess_batch(load_sightings()),
    ]
    blob = json.dumps(pair_outs, ensure_ascii=False)
    assert "player_uid" not in blob
    for out in pair_outs:
        assert out["identity_assertion"] == "NONE"
