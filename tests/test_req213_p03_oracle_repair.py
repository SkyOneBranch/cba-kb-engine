"""Targeted falsification and boundary regression tests for REQ-213 P0-3 H1 repair.

Covers:
1. Identifier/filename prefixes (`03_DATA_...`, `04_PROVENANCE_...`, `02_RESULT_...`,
   `03-DATA-...`, `12_abc`, `13.0_kg`) are NOT misparsed as asserted numbers by
   `_NUM_TOKEN_RE` / `_verbatim_numbers`.
2. Formal `validate_receipt_approved` + `score_answer_approved` call chain passes
   when a verbatim answer mentions an authorized `03_DATA_...` filename alongside
   a genuine native `cited_locators` entry.
3. Mentioning a filename in prose NEVER substitutes for missing native
   `cited_locators` (`FAIL / locator / critical=True` remains strictly enforced).
4. Valid numeric forms (`1_000`, `1,000`, decimals, negatives, scientific notation,
   unit-suffixed decimals, standalone IDs) and genuine numeric contradictions
   remain strictly enforced.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from cba_kb.oracle import (
    ClientReceipt,
    OracleError,
    OracleQuestion,
    OracleSource,
    ScoringApproval,
    _verbatim_numbers,
    build_oracle,
    locator_set_digest,
    score_answer,
    score_answer_approved,
    scoring_code_sha,
    validate_receipt_approved,
)


SYNTH_LOCATOR_DATA = "03_DATA_SYNTH_PLAYER_SEASON_STATS_2025-2026.json"
SYNTH_LOCATOR_PROV = "04_PROVENANCE_SYNTH_2025-2026.json"
SYNTH_LOCATOR_RES = "02_RESULT_SYNTH_2025-2026.md"
SYNTH_LOCATOR_MAN = "01_MANIFEST_SYNTH_2025-2026.json"
SYNTH_SNAPSHOT_DIGEST = "a" * 64
SYNTH_ENGINE_SHA = "b" * 40


def _build_synth_approved_oracle():
    locators = (
        SYNTH_LOCATOR_MAN,
        SYNTH_LOCATOR_RES,
        SYNTH_LOCATOR_DATA,
        SYNTH_LOCATOR_PROV,
    )
    oracle = build_oracle(
        version="req213-p03-synth-repair-v1",
        sources=[
            OracleSource(
                "src-synth-official",
                "Synthetic official stats fixture",
                "LAWFUL_PUBLIC_SYNTHETIC",
            )
        ],
        questions=[
            OracleQuestion(
                "Q213-SYNTH-CLEAN",
                "Synthetic regular season PPG?",
                "rate",
                expected=18.4,
                unit="ppg",
                precision=1,
                tolerance=0.05,
                mandatory_locator=True,
                required_scope_tokens=("2025-2026", "常规赛", "42场"),
                expected_posture="QUIET_CLEAN_OFFICIAL",
                designated_locators=(SYNTH_LOCATOR_DATA,),
            )
        ],
    )
    approval = ScoringApproval(
        manifest_digest=oracle.artifact_digest,
        version=oracle.version,
        scoring_code_sha=scoring_code_sha(),
        snapshot_digest=SYNTH_SNAPSHOT_DIGEST,
        authorization_domain="synth-player-2025-2026-v1",
        locator_set_digest=locator_set_digest(locators),
    )
    return oracle, approval, locators


def test_h1_filename_and_identifier_prefixes_not_extracted_as_numbers():
    for token in (
        SYNTH_LOCATOR_DATA,
        SYNTH_LOCATOR_PROV,
        SYNTH_LOCATOR_RES,
        SYNTH_LOCATOR_MAN,
        "03-DATA-FILE.json",
        "research_card.md",
        "03_ and _03 and 12_abc and 13.0_kg and 13.0e1_a",
        "loc-1 and Q1 and DATA_03 and v2.1.3 and 3.14.15",
    ):
        assert _verbatim_numbers(token) == [], f"unexpected number extracted from {token!r}"


def test_h1_formal_approved_chain_passes_with_prose_filename_and_native_citation():
    oracle, approval, locators = _build_synth_approved_oracle()
    question = oracle.question("Q213-SYNTH-CLEAN")
    verbatim = (
        f"2025-2026 赛季，常规赛（42场）：18.4 分/场"
        f"（来源文件：{SYNTH_LOCATOR_DATA}）。"
    )
    receipt = ClientReceipt(
        engine_sha=SYNTH_ENGINE_SHA,
        snapshot_digest=SYNTH_SNAPSHOT_DIGEST,
        manifest_digest=oracle.artifact_digest,
        question_set_version=oracle.version,
        authorization_domain="synth-player-2025-2026-v1",
        question_id=question.question_id,
        verbatim_answer=verbatim,
        cited_locators=(SYNTH_LOCATOR_DATA,),
        receipt_mode="VERBATIM",
    )
    errors = validate_receipt_approved(
        receipt,
        oracle=oracle,
        approval=approval,
        snapshot_digest=SYNTH_SNAPSHOT_DIGEST,
        authorization_domain="synth-player-2025-2026-v1",
        authorized_locators=locators,
    )
    assert errors == []

    verdict = score_answer_approved(
        question,
        {
            "text": receipt.verbatim_answer,
            "value": 18.4,
            "unit": "ppg",
            "cited_locators": list(receipt.cited_locators),
        },
        oracle=oracle,
        approval=approval,
        snapshot_digest=SYNTH_SNAPSHOT_DIGEST,
        authorized_locators=locators,
    )
    assert verdict.verdict == "PASS"


def test_h1_prose_filename_never_substitutes_for_missing_native_citation():
    oracle, approval, locators = _build_synth_approved_oracle()
    question = oracle.question("Q213-SYNTH-CLEAN")
    verbatim = (
        f"2025-2026 赛季，常规赛（42场）：18.4 分/场"
        f"（来源文件：{SYNTH_LOCATOR_DATA}）。"
    )
    receipt_no_cite = ClientReceipt(
        engine_sha=SYNTH_ENGINE_SHA,
        snapshot_digest=SYNTH_SNAPSHOT_DIGEST,
        manifest_digest=oracle.artifact_digest,
        question_set_version=oracle.version,
        authorization_domain="synth-player-2025-2026-v1",
        question_id=question.question_id,
        verbatim_answer=verbatim,
        cited_locators=(),
        receipt_mode="VERBATIM",
    )
    errors = validate_receipt_approved(
        receipt_no_cite,
        oracle=oracle,
        approval=approval,
        snapshot_digest=SYNTH_SNAPSHOT_DIGEST,
        authorization_domain="synth-player-2025-2026-v1",
        authorized_locators=locators,
    )
    assert errors != []

    verdict = score_answer_approved(
        question,
        {
            "text": verbatim,
            "value": 18.4,
            "unit": "ppg",
            "cited_locators": [],
        },
        oracle=oracle,
        approval=approval,
        snapshot_digest=SYNTH_SNAPSHOT_DIGEST,
        authorized_locators=locators,
    )
    assert verdict.verdict == "FAIL"
    assert verdict.dimension == "locator"
    assert verdict.critical is True


def test_h1_numeric_boundaries_and_real_contradictions_preserved():
    assert _verbatim_numbers("1_000 and 1_000_000 and 1,000 and 1,234,567.89") == [
        Decimal("1000"),
        Decimal("1000000"),
        Decimal("1000"),
        Decimal("1234567.89"),
    ]
    assert _verbatim_numbers("-12.5 and +3.14 and .5 and -.5") == [
        Decimal("-12.5"),
        Decimal("3.14"),
        Decimal("0.5"),
        Decimal("-0.5"),
    ]
    assert _verbatim_numbers("1.3e1 and 1.3e-2 and 1.3E+2 and 1e1_0") == [
        Decimal("13"),
        Decimal("0.013"),
        Decimal("1.3E+2"),
        Decimal("1E+10"),
    ]
    assert _verbatim_numbers("13.0points and 2.5kg and 12abc and 12kg") == [
        Decimal("13.0"),
        Decimal("2.5"),
    ]
    assert _verbatim_numbers("Provider ID 800101 Team ID 30020") == [
        Decimal("800101"),
        Decimal("30020"),
    ]
    with pytest.raises(OracleError):
        _verbatim_numbers("13e99999999999999999999")

    oracle, approval, locators = _build_synth_approved_oracle()
    question = oracle.question("Q213-SYNTH-CLEAN")
    contradictory = score_answer_approved(
        question,
        {
            "text": f"2025-2026 赛季，常规赛（42场）：18.4 分/场（另见 19.2 分/场，{SYNTH_LOCATOR_DATA}）。",
            "value": 18.4,
            "unit": "ppg",
            "cited_locators": [SYNTH_LOCATOR_DATA],
        },
        oracle=oracle,
        approval=approval,
        snapshot_digest=SYNTH_SNAPSHOT_DIGEST,
        authorized_locators=locators,
    )
    assert contradictory.verdict == "FAIL"
    assert contradictory.dimension == "consistency"
    assert contradictory.critical is True
