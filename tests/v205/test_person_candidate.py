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

import itertools
import json
from pathlib import Path

import pytest

from cba_kb import person_candidate as pc
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


def test_same_source_same_name_is_ambiguous():
    """B2 (Q2 R1): same source + same name does NOT authorize a merge.

    Shared document provenance never proves shared personal identity:
    one document can mention two different people with the same name.
    The pair stays a candidate under REVIEW_REQUIRED.
    """
    out = assess_pair(
        _sighting("虚构球员甲", "synthetic://a"),
        _sighting("虚构球员甲", "synthetic://a"),
    )
    assert out["verdict"] == "AMBIGUOUS"
    assert out["status"] == "REVIEW_REQUIRED"
    assert out["automatic_merge_allowed"] is False
    assert out["merged_view"] is None
    assert "SAME_NAME_SAME_SOURCE" in out["ambiguity_signals"]


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


def test_unverified_different_external_ids_are_ambiguous():
    """B4 (Q2 R1): two DIFFERENT unverified external IDs prove nothing.

    Only IDs verified as authoritative by the trusted upstream may
    separate two people; a bare observed ID difference is candidate
    evidence only, so the pair stays under REVIEW_REQUIRED.
    """
    out = assess_pair(
        _sighting("虚构球员甲", "synthetic://a", external_id="EXT-9001"),
        _sighting("虚构球员甲", "synthetic://b", external_id="EXT-9002"),
    )
    assert out["verdict"] == "AMBIGUOUS"
    assert out["status"] == "REVIEW_REQUIRED"
    assert out["automatic_merge_allowed"] is False
    assert "UNVERIFIED_EXTERNAL_IDS_DIFFER" in out["ambiguity_signals"]


def test_verified_different_external_ids_are_distinct():
    """Verified authoritative IDs that differ DO prove DISTINCT (B4)."""
    out = assess_pair(
        _sighting("虚构球员甲", "synthetic://a", external_id="EXT-9001"),
        _sighting("虚构球员甲", "synthetic://b", external_id="EXT-9002"),
        verified_external_ids={"EXT-9001", "EXT-9002"},
    )
    assert out["verdict"] == "DISTINCT"
    assert out["verdict_reason"] == "DIFFERENT_EXTERNAL_IDS"
    assert out["automatic_merge_allowed"] is False


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
    """Rule 3 + B4: a VERIFIED authoritative external ID permits merge.

    The ID must arrive via ``verified_external_ids`` (the trusted
    upstream's registry cross-check); the same ID carried only inside
    the sightings is observation evidence and never merges (see the
    B4 regression tests below).
    """
    left = _sighting("虚构球员乙", "synthetic://s1",
                     external_id="EXT-9001", team="虚构球队甲")
    right = _sighting("虚构球员乙", "synthetic://s2",
                      external_id="EXT-9001", team="虚构球队甲")
    out = assess_pair(left, right, verified_external_ids={"EXT-9001"})
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
    """Rule 3 + B5: a trusted association delivered via the authorized
    channel permits merge. The channel type (not a self-asserted dict)
    is what the module honors."""
    out = assess_pair(
        _sighting("虚构球员甲", "synthetic://a"),
        _sighting("虚构球员甲", "synthetic://b"),
        trusted_link=pc.AuthorizedTrustedLink(
            "SOURCE_DECLARED",
            evidence_refs=["synthetic://test/operator-note-1"],
            asserted_by="synthetic-operator-1",
        ),
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
    """Rule 3 + B5: an authorized-channel Human decision is the final
    arbiter, even over ambiguity. Plain dicts never are (see B5 tests)."""
    out = assess_pair(
        _sighting("虚构球员甲", "synthetic://a"),
        _sighting("甲甲", "synthetic://b"),
        human_decision=pc.AuthorizedHumanDecision(
            "same", decided_by="synthetic-reviewer-1"),
    )
    assert out["verdict"] == "MERGE_ALLOWED"
    assert out["verdict_reason"] == "HUMAN_DECISION_SAME"


def test_human_decision_not_same():
    out = assess_pair(
        _sighting("虚构球员甲", "synthetic://a"),
        _sighting("虚构球员甲", "synthetic://b"),
        human_decision=pc.AuthorizedHumanDecision(
            "not_same", decided_by="synthetic-reviewer-1"),
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
        human_decisions={(a["source_ref"], b["source_ref"]):
                         pc.AuthorizedHumanDecision(
                             "same", decided_by="synthetic-reviewer-1")},
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


# ============================================================ Q2 R1 regression
# Every construction below is the independent reviewer's R1 counterexample,
# replayed verbatim (synthetic values). Each one bypassed the R1 code and
# produced MERGE_ALLOWED / an identity leak; each must now fail closed.

def test_b1_batch_order_reversal_keeps_review_required():
    """B1: pair verdicts are stored by original index but were retrieved
    in source_ref order; a reversed pair missed the lookup and the group
    silently defaulted to MERGE_ALLOWED."""
    a = {"source_ref": "z#1", "source_id": "z", "display_name": "Synthetic Alex"}
    b = {"source_ref": "a#1", "source_id": "a", "display_name": "Synthetic Alex"}
    pair = assess_pair(a, b)
    assert pair["verdict"] == "AMBIGUOUS"
    assert pair["status"] == "REVIEW_REQUIRED"
    for ordered in ([a, b], [b, a]):
        out = assess_batch(ordered)
        assert len(out["groups"]) == 1
        group = out["groups"][0]
        assert group["verdict"] == "REVIEW_REQUIRED"
        assert group["automatic_merge_allowed"] is False
        assert group["merged_view"] is None


def test_b1_batch_conflict_survives_ref_sorting():
    """B1: a CONFLICT edge must be found regardless of ref sort order."""
    left = {"source_ref": "z#9", "source_id": "s1", "display_name": "虚构球员乙",
            "external_id": "EXT-9001", "team": "虚构球队甲"}
    right = {"source_ref": "a#1", "source_id": "s2", "display_name": "虚构球员乙",
             "external_id": "EXT-9001", "team": "虚构球队丙"}
    out = assess_batch([left, right], verified_external_ids={"EXT-9001"})
    assert len(out["groups"]) == 1
    group = out["groups"][0]
    assert group["verdict"] == "CONFLICT"
    assert group["merged_view"] is None
    assert len(group["sightings"]) == 2  # both records retained


def test_batch_permutation_invariance():
    """B1: batch output must not depend on input ordering, at all."""
    sightings = [
        {"source_ref": "m#2", "source_id": "src-a", "display_name": "虚构球员甲"},
        {"source_ref": "a#9", "source_id": "src-b", "display_name": "虚构球员甲"},
        {"source_ref": "z#1", "source_id": "src-c", "display_name": "虚构球员乙",
         "external_id": "EXT-9001", "team": "虚构球队甲"},
        {"source_ref": "b#3", "source_id": "src-d", "display_name": "虚构球员乙",
         "external_id": "EXT-9001", "team": "虚构球队丙"},
        {"source_ref": "c#5", "source_id": "src-e", "display_name": "虚构球员丙",
         "birth_month": "1998-11"},
    ]
    baseline = assess_batch(sightings)
    verdicts = {g["verdict"] for g in baseline["groups"]}
    assert verdicts == {"REVIEW_REQUIRED", "CONFLICT", "SINGLETON"}
    assert baseline["status"] == "CONFLICT"
    for perm in itertools.permutations(sightings):
        assert assess_batch(list(perm)) == baseline


def test_b2_two_people_same_document_not_merged():
    """B2 reviewer construction: one document, two rows, same name."""
    a = {"source_ref": "article#row1", "source_id": "article",
         "display_name": "Alex"}
    b = {"source_ref": "article#row2", "source_id": "article",
         "display_name": "Alex"}
    out = assess_pair(a, b)
    assert out["verdict"] == "AMBIGUOUS"
    assert out["status"] == "REVIEW_REQUIRED"
    assert out["automatic_merge_allowed"] is False


def test_b3_transitive_distinct_blocks_group_merge():
    """B3: A~B and B~C are ambiguous, A-C is DISTINCT (verified IDs).
    The group must never be MERGE_ALLOWED; the cannot-link violation
    surfaces as CONFLICT with all records retained."""
    a = {"source_ref": "t#a", "source_id": "s", "display_name": "虚构球员甲",
         "external_id": "EXT-9001"}
    b = {"source_ref": "t#b", "source_id": "s", "display_name": "虚构球员甲"}
    c = {"source_ref": "t#c", "source_id": "s", "display_name": "虚构球员甲",
         "external_id": "EXT-9002"}
    out = assess_batch([a, b, c],
                       verified_external_ids={"EXT-9001", "EXT-9002"})
    assert len(out["groups"]) == 1
    group = out["groups"][0]
    assert group["verdict"] == "CONFLICT"
    assert group["automatic_merge_allowed"] is False
    assert group["merged_view"] is None
    assert len(group["sightings"]) == 3


def test_b3_reviewer_construction_unverified_ids_not_merged():
    """B3 reviewer construction with bare (unverified) EXT-1/EXT-2:
    under the B4 contract these IDs prove nothing either way, so the
    transitive group degrades to REVIEW_REQUIRED, never a merge."""
    a = {"source_ref": "a", "source_id": "s", "display_name": "Alex",
         "external_id": "EXT-1"}
    b = {"source_ref": "b", "source_id": "s", "display_name": "Alex"}
    c = {"source_ref": "c", "source_id": "s", "display_name": "Alex",
         "external_id": "EXT-2"}
    out = assess_batch([a, b, c])
    assert len(out["groups"]) == 1
    group = out["groups"][0]
    assert group["verdict"] == "REVIEW_REQUIRED"
    assert group["automatic_merge_allowed"] is False
    assert group["merged_view"] is None


def test_b3_human_not_same_is_cannot_link():
    """B3: an authorized Human not_same decision is a hard cannot-link
    constraint; transitive grouping cannot launder it into a merge."""
    x = {"source_ref": "n#1", "source_id": "s1", "display_name": "虚构球员甲"}
    y = {"source_ref": "n#2", "source_id": "s2", "display_name": "虚构球员甲"}
    z = {"source_ref": "n#3", "source_id": "s3", "display_name": "虚构球员甲"}
    out = assess_batch(
        [x, y, z],
        human_decisions={("n#1", "n#3"): pc.AuthorizedHumanDecision(
            "not_same", decided_by="synthetic-reviewer-1")},
    )
    assert len(out["groups"]) == 1
    group = out["groups"][0]
    assert group["verdict"] == "CONFLICT"
    assert group["automatic_merge_allowed"] is False
    assert group["merged_view"] is None
    assert len(group["sightings"]) == 3


def test_b4_unverified_same_external_id_is_review_required():
    """B4 reviewer construction: identical bare IDs from two article
    sources are observation evidence only — REVIEW_REQUIRED."""
    a = {"source_ref": "article-a#1", "source_id": "article-a",
         "source_kind": "ARTICLE", "display_name": "Alex",
         "external_id": "UNVERIFIED-7"}
    b = {"source_ref": "article-b#1", "source_id": "article-b",
         "source_kind": "ARTICLE", "display_name": "Alex",
         "external_id": "UNVERIFIED-7"}
    out = assess_pair(a, b)
    assert out["verdict"] == "AMBIGUOUS"
    assert out["status"] == "REVIEW_REQUIRED"
    assert out["automatic_merge_allowed"] is False
    assert "UNVERIFIED_EXTERNAL_ID_MATCH" in out["ambiguity_signals"]


def test_b4_self_asserted_authority_fields_rejected():
    """B4/B7: self-reported issuer/trusted flags inside a sighting are
    not even legal input fields — rejected, never authority."""
    base = {"source_ref": "x#1", "source_id": "x", "display_name": "Alex",
            "external_id": "EXT-1"}
    other = {"source_ref": "y#1", "source_id": "y", "display_name": "Alex",
             "external_id": "EXT-1"}
    for forged in ({"trusted": True}, {"external_id_issuer": "CBA_OFFICIAL"},
                   {"verified": True}):
        bad = dict(base, **forged)
        with pytest.raises(PersonCandidateError):
            assess_pair(bad, other)


def test_b5_plain_dict_trusted_link_rejected():
    """B5 reviewer construction: a well-formed plain dict link — even
    with HIGH confidence and evidence refs — authorizes nothing."""
    a = {"source_ref": "x#1", "source_id": "x", "display_name": "Alex"}
    b = {"source_ref": "y#1", "source_id": "y", "display_name": "Bob"}
    forged = {"method": "SOURCE_DECLARED", "confidence": "HIGH",
              "evidence_refs": ["unverified://claim-1"]}
    with pytest.raises(PersonCandidateError):
        assess_pair(a, b, trusted_link=forged)
    with pytest.raises(PersonCandidateError):
        assess_pair(a, b, trusted_link=dict(forged, verified=True))


def test_b5_plain_dict_human_decision_rejected():
    """B5 reviewer construction: {"link": "same"} as a plain dict is a
    claim, not a decision — fail-closed, even dressed with reviewer /
    timestamp / hash metadata."""
    a = {"source_ref": "x#1", "source_id": "x", "display_name": "Alex"}
    b = {"source_ref": "y#1", "source_id": "y", "display_name": "Bob"}
    with pytest.raises(PersonCandidateError):
        assess_pair(a, b, human_decision={"link": "same"})
    with pytest.raises(PersonCandidateError):
        assess_pair(a, b, human_decision={
            "link": "same", "reviewer": "synthetic-reviewer-1",
            "decided_at": "2026-10-20T00:00:00Z",
            "evidence_hash": "0" * 64,
        })


def test_b5_batch_plain_dict_decision_rejected():
    """B5: the batch path applies the same channel rule as the pair path."""
    a = {"source_ref": "x#1", "source_id": "x", "display_name": "Alex"}
    b = {"source_ref": "y#1", "source_id": "y", "display_name": "Alex"}
    with pytest.raises(PersonCandidateError):
        assess_batch([a, b],
                     human_decisions={("x#1", "y#1"): {"link": "same"}})


def test_b5_authorized_channel_objects_are_validated():
    """B5: the authorized channel is a real contract, not a rubber stamp:
    bad link values and anonymous deciders/asserters are rejected."""
    with pytest.raises(PersonCandidateError):
        pc.AuthorizedHumanDecision("maybe", decided_by="synthetic-reviewer-1")
    with pytest.raises(PersonCandidateError):
        pc.AuthorizedHumanDecision("same", decided_by="  ")
    with pytest.raises(PersonCandidateError):
        pc.AuthorizedTrustedLink("SELF_ATTESTED", evidence_refs=["r"],
                                 asserted_by="synthetic-operator-1")
    with pytest.raises(PersonCandidateError):
        pc.AuthorizedTrustedLink("SOURCE_DECLARED", evidence_refs=[],
                                 asserted_by="synthetic-operator-1")


def test_b6_same_id_different_birth_month_is_conflict():
    """B6 reviewer construction: same claimed ID, contradictory birth
    months -> CONFLICT (both records kept), never a silent merge."""
    a = {"source_ref": "stats-a#1", "source_id": "stats-a",
         "display_name": "Alex", "external_id": "EXT-1",
         "birth_month": "1995-03"}
    b = {"source_ref": "stats-b#1", "source_id": "stats-b",
         "display_name": "Alex", "external_id": "EXT-1",
         "birth_month": "1996-07"}
    out = assess_pair(a, b)
    assert out["verdict"] == "CONFLICT"
    assert out["conflict_fields"] == ["birth_month"]
    assert out["merged_view"] is None
    assert out["automatic_merge_allowed"] is False
    assert out["sightings"] == [a, b]  # both records preserved


def test_b6_verified_id_multiple_contradictions_all_reported():
    """B6: every substantive contradiction is reported, not just one."""
    left = _sighting("虚构球员乙", "synthetic://s1", external_id="EXT-9001",
                     team="虚构球队甲", birth_month="1995-03")
    right = _sighting("虚构球员乙", "synthetic://s2", external_id="EXT-9001",
                      team="虚构球队丙", birth_month="1996-07")
    out = assess_pair(left, right, verified_external_ids={"EXT-9001"})
    assert out["verdict"] == "CONFLICT"
    assert out["conflict_fields"] == ["birth_month", "team"]


def test_b7_player_uid_input_rejected():
    """B7 reviewer construction: an identity-bearing field in the input
    must never ride through into the output sightings."""
    a = {"source_ref": "a#1", "source_id": "a", "display_name": "Alex",
         "player_uid": "PERSON-123"}
    b = {"source_ref": "b#1", "source_id": "b", "display_name": "Alex"}
    with pytest.raises(PersonCandidateError, match="player_uid"):
        assess_pair(a, b)


def test_b7_identity_fields_rejected_in_batch():
    """B7: the whitelist applies on the batch path too, and to any
    non-business field, not just player_uid."""
    a = {"source_ref": "a#1", "source_id": "a", "display_name": "Alex",
         "person_id": "P-1"}
    b = {"source_ref": "b#1", "source_id": "b", "display_name": "Alex"}
    with pytest.raises(PersonCandidateError, match="person_id"):
        assess_batch([a, b])
    c = {"source_ref": "c#1", "source_id": "c", "display_name": "Alex",
         "scout_notes": "looks promising"}
    with pytest.raises(PersonCandidateError, match="scout_notes"):
        assess_pair(c, b)


def test_r3_reversed_key_not_same_enforced():
    """R2-F1: 反向 key 的 not_same 决策必须生效，不能静默丢弃。"""
    import cba_kb.person_candidate as pc
    a = {"source_ref": "z#1", "display_name": "Alex", "external_id": "EXT-1"}
    b = {"source_ref": "a#1", "display_name": "Alex", "external_id": "EXT-1"}
    decision = pc.AuthorizedHumanDecision("not_same", decided_by="rev-1")
    out = pc.assess_batch(
        [a, b],
        human_decisions={("z#1", "a#1"): decision},  # 反向 key
        verified_external_ids={"EXT-1"})
    g = out["groups"][0]
    assert g["verdict"] != "MERGE_ALLOWED"
    assert g.get("automatic_merge_allowed") is not True


def test_r3_forged_dict_reversed_key_rejected():
    """R2-F1/B5: 反向 key 的伪造 dict 也必须被验证并拒绝。"""
    import cba_kb.person_candidate as pc
    a = {"source_ref": "z#1", "display_name": "Alex", "external_id": "EXT-1"}
    b = {"source_ref": "a#1", "display_name": "Alex", "external_id": "EXT-1"}
    with pytest.raises(pc.PersonCandidateError):
        pc.assess_batch(
            [a, b],
            human_decisions={("z#1", "a#1"): {"link": "same"}},
            verified_external_ids={"EXT-1"})


def test_r3_trusted_link_does_not_override_birth_month_conflict():
    """R2-F2: trusted link 不能盖掉同 ID 出生月矛盾。"""
    import cba_kb.person_candidate as pc
    a = {"source_ref": "stats-a#1", "display_name": "Alex",
         "external_id": "EXT-1", "birth_month": "1995-03"}
    b = {"source_ref": "stats-b#1", "display_name": "Alex",
         "external_id": "EXT-1", "birth_month": "1996-07"}
    link = pc.AuthorizedTrustedLink(
        "SOURCE_DECLARED", evidence_refs=["synthetic://audit-1"],
        asserted_by="op-1")
    out = pc.assess_pair(a, b, trusted_link=link,
                         verified_external_ids={"EXT-1"})
    assert out["verdict"] == "CONFLICT"
    assert "birth_month" in out["conflict_fields"]


def test_r4_forged_dict_link_on_conflict_path_raises():
    """R3-F1: 冲突路径上的伪造 dict link 也必须抛错，不能静默返回 CONFLICT。"""
    import cba_kb.person_candidate as pc
    a = {"source_ref": "stats-a#1", "display_name": "Alex",
         "external_id": "EXT-1", "birth_month": "1995-03"}
    b = {"source_ref": "stats-b#1", "display_name": "Alex",
         "external_id": "EXT-1", "birth_month": "1996-07"}
    with pytest.raises(pc.PersonCandidateError):
        pc.assess_pair(a, b, trusted_link={"method": "SOURCE_DECLARED"},
                       verified_external_ids={"EXT-1"})
