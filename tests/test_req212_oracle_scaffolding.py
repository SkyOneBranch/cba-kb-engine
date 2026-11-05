"""REQ-212: independent Oracle scaffolding (P0-A / P1-D).

All persons, values, and answers here are SYNTHETIC fixtures for the
scaffolding. Nothing here selects a real person, records a real-client run,
or claims any real-client result; an unrunnable client is
REAL_CLIENT_NOT_EXECUTED, never a receipt.
"""

from cba_kb.oracle import (
    ClientReceipt,
    OracleError,
    OracleQuestion,
    OracleSource,
    ScoreResult,
    adjudicate_repeat,
    build_oracle,
    classify_answer,
    repeat_required,
    score_answer,
    validate_receipt,
    verify_manifest,
)


def _oracle():
    return build_oracle(
        version="oracle-v1-synthetic",
        sources=[OracleSource("src-synth", "synthetic primary source", "LAWFUL_PUBLIC_SYNTHETIC")],
        questions=[
            OracleQuestion("q-count", "synthetic count?", "exact", expected=12),
            OracleQuestion("q-uid", "synthetic uid?", "uid", expected="UID-CANON",
                           alias_map=(("UID-ALIAS", "UID-CANON"),)),
            OracleQuestion("q-rate", "synthetic rate?", "rate", expected=50.0,
                           unit="percent", precision=1, tolerance=0.05),
            OracleQuestion("q-loc", "synthetic locator?", "locator", expected="loc-1",
                           mandatory_locator=True),
            OracleQuestion("q-unk", "synthetic unknown?", "unknown"),
        ],
    )


def test_build_versions_and_digests_deterministically():
    first = _oracle()
    second = _oracle()
    assert first.artifact_digest == second.artifact_digest
    assert len(first.artifact_digest) == 64
    manifest = first.to_manifest()
    assert verify_manifest(manifest) == (True, "OK")


def test_build_rejects_questions_without_expected_or_unknown():
    try:
        build_oracle(
            version="v",
            sources=[OracleSource("s", "p", "r")],
            questions=[OracleQuestion("q", "?", "exact", expected=None)],
        )
    except OracleError:
        pass
    else:
        raise AssertionError("a question with neither expected nor UNKNOWN must be rejected")


def test_build_rejects_empty_sources_and_duplicate_ids():
    for kwargs in (
        dict(version="v", sources=[], questions=[OracleQuestion("q", "?", "unknown")]),
        dict(version="v", sources=[OracleSource("s", "p", "r")],
             questions=[OracleQuestion("q", "?", "unknown"), OracleQuestion("q", "?", "unknown")]),
    ):
        try:
            build_oracle(**kwargs)
        except OracleError:
            pass
        else:
            raise AssertionError("invalid Oracle build must be rejected")


def test_manifest_tamper_and_substitution_fail_closed():
    oracle = _oracle()
    manifest = oracle.to_manifest()
    tampered = dict(manifest)
    tampered["questions"] = [dict(q, expected=999) if q["question_id"] == "q-count"
                             else q for q in manifest["questions"]]
    assert verify_manifest(tampered) == (False, "DIGEST_MISMATCH")
    assert verify_manifest({"version": "v"}) == (False, "MALFORMED_MANIFEST")
    assert verify_manifest(dict(manifest, artifact_digest="0" * 64)) == (False, "DIGEST_MISMATCH")


def test_scoring_exact_uid_rate_and_aliases():
    oracle = _oracle()
    assert score_answer(oracle.question("q-count"), "12").verdict == "PASS"
    assert score_answer(oracle.question("q-count"), "1,2").verdict == "PASS"
    assert score_answer(oracle.question("q-count"), "13").verdict == "FAIL"
    assert score_answer(oracle.question("q-count"), "about twelve").verdict == "FAIL"
    assert score_answer(oracle.question("q-uid"), "UID-ALIAS").verdict == "PASS"
    assert score_answer(oracle.question("q-uid"), "UID-CANON").verdict == "PASS"
    assert score_answer(oracle.question("q-uid"), "UID-OTHER").verdict == "FAIL"
    assert score_answer(oracle.question("q-uid"), "UID-OTHER").critical is True
    rate = oracle.question("q-rate")
    assert score_answer(rate, {"text": "50.0", "value": 50.0, "unit": "percent"}).verdict == "PASS"
    assert score_answer(rate, {"text": "50.0", "value": 50.0, "unit": "points"}).verdict == "FAIL"
    assert score_answer(rate, {"text": "51.0", "value": 51.0, "unit": "percent"}).verdict == "FAIL"


def test_locator_rules_and_out_of_snapshot_failure():
    oracle = _oracle()
    loc = oracle.question("q-loc")
    good = {"text": "see loc-1", "value": "loc-1", "cited_locators": ["loc-1"]}
    assert score_answer(loc, good, authorized_locators=["loc-1"]).verdict == "PASS"
    bad = {"text": "see loc-9", "value": "loc-9", "cited_locators": ["loc-9"]}
    result = score_answer(loc, bad, authorized_locators=["loc-1"])
    assert result.verdict == "FAIL" and result.critical is True
    assert score_answer(loc, {"text": "x", "cited_locators": []},
                        authorized_locators=["loc-1"]).verdict == "FAIL"


def test_unknown_honesty_and_fabrication_is_critical():
    oracle = _oracle()
    unk = oracle.question("q-unk")
    assert classify_answer("UNKNOWN") == "UNKNOWN"
    assert score_answer(unk, "UNKNOWN").verdict == "PASS"
    # Versioned marker rule: hedged prose is not a machine-verifiable
    # refusal marker, so it classifies as a value answer and fails honesty.
    assert classify_answer("I refuse to guess") == "VALUE"
    assert score_answer(unk, "I refuse to guess").verdict == "FAIL"
    fabricated = score_answer(unk, "42")
    assert fabricated.verdict == "FAIL" and fabricated.critical is True


def test_receipt_validation_and_format_fail_closed():
    oracle = _oracle()
    receipt = ClientReceipt(
        engine_sha="a" * 40,
        snapshot_digest="b" * 64,
        manifest_digest=oracle.artifact_digest,
        question_set_version=oracle.version,
        authorization_domain="synthetic-domain",
        question_id="q-loc",
        verbatim_answer="see loc-1",
        cited_locators=("loc-1",),
    )
    assert validate_receipt(receipt, oracle=oracle, authorized_locators=["loc-1"]) == []
    assert validate_receipt(receipt, oracle=oracle, authorized_locators=[]) != []
    wrong_manifest = ClientReceipt(
        engine_sha="a" * 40, snapshot_digest="b" * 64, manifest_digest="0" * 64,
        question_set_version=oracle.version, authorization_domain="synthetic-domain",
        question_id="q-loc", verbatim_answer="see loc-1", cited_locators=("loc-1",))
    assert validate_receipt(wrong_manifest, oracle=oracle, authorized_locators=["loc-1"]) != []
    try:
        ClientReceipt(engine_sha="short", snapshot_digest="b" * 64,
                      manifest_digest=oracle.artifact_digest, question_set_version=oracle.version,
                      authorization_domain="d", question_id="q-loc", verbatim_answer="x")
    except OracleError:
        pass
    else:
        raise AssertionError("malformed receipt formats must be rejected at construction")


def test_repeat_protocol_never_averages_into_pass():
    oracle = _oracle()
    q = oracle.question("q-count")
    agreed = [score_answer(q, "12") for _ in range(3)]
    assert repeat_required(agreed) is False
    assert adjudicate_repeat(agreed) == ("AGREED", "PASS")
    mixed = [score_answer(q, "12"), score_answer(q, "12"), score_answer(q, "13")]
    assert repeat_required(mixed) is True
    assert adjudicate_repeat(mixed) == ("ADJUDICATION_REQUIRED", None)
    assert adjudicate_repeat(mixed[:2]) == ("INSUFFICIENT_RUNS", None)


def test_no_receipt_without_a_run_is_constructible_by_default():
    # Every ClientReceipt field is required: there is no default that invents
    # a run, a digest, or an answer. Construction with a missing field is a
    # TypeError, which is the fail-closed shape this scaffolding requires.
    try:
        ClientReceipt(engine_sha="a" * 40)
    except TypeError:
        pass
    else:
        raise AssertionError("partial receipt construction must fail")
