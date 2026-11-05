"""REQ-212 Q2 Round 3 remediation tests (PR #236 follow-up).

Round-1 tests FAIL on the Q2-FAIL HEAD 7eb9ff33 and PASS on the remediated
HEAD 677b5438. Round-2 tests (marked R2) FAIL on HEAD 677b5438 (the Q2
re-review FAIL HEAD) and PASS on the repaired HEAD. Round-3 tests
(marked R3) FAIL on HEAD 8bcd8875 (the Q2 re-review Round 2 FAIL HEAD)
and PASS on the repaired HEAD. One guard test is explicitly labelled as
passing on both HEADs by design, and the Snapshot-pairing test is
labelled as a capability test passing on the R3 baseline HEAD 8bcd8875
and the repaired HEAD by design (it fails on the older 677b5438 only
because the locator-set entrypoints did not exist there yet -- API
absence, not the attack). It closes an evidence gap, it does not falsify.
New entrypoints are imported lazily inside the tests so that each test
fails individually on the old HEAD (a missing entrypoint counts as FAIL
there). All Drive-ID-like values are SYNTHETIC, built at runtime by
concatenation, so no literal morphology token appears in this file's
bytes. Nothing here selects a real person, records a real-client run, or
claims a real result.
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from cba_kb.oracle import (
    ClientReceipt,
    Oracle,
    OracleError,
    OracleQuestion,
    OracleSource,
    ScoreResult,
    adjudicate_repeat,
    build_oracle,
    score_answer,
    validate_receipt,
)
from cba_kb.current_state import security

ROOT = Path(__file__).resolve().parents[1]
SCANNER = security()


def synth_token(letter="A"):
    return "1" + letter * 32


def synth_url(letter="A"):
    return "https://drive.google.com/file/d/" + synth_token(letter) + "/view"


def _oracle_v2():
    return build_oracle(
        version="oracle-v2-synthetic",
        sources=[OracleSource("src-synth", "synthetic primary source", "LAWFUL_PUBLIC_SYNTHETIC")],
        questions=[
            OracleQuestion("q-count", "synthetic count?", "exact", expected=12,
                           mandatory_locator=True),
            OracleQuestion("q-loc", "synthetic locator?", "locator", expected="loc-1",
                           mandatory_locator=True),
            OracleQuestion("q-unk", "synthetic unknown?", "unknown"),
            OracleQuestion("q-rate", "synthetic rate?", "rate", expected=50.0,
                           unit="percent", precision=1, tolerance=0.05),
        ],
    )


def _receipt(oracle, **overrides):
    fields = dict(
        engine_sha="a" * 40,
        snapshot_digest="b" * 64,
        manifest_digest=oracle.artifact_digest,
        question_set_version=oracle.version,
        authorization_domain="synthetic-domain",
        question_id="q-loc",
        verbatim_answer="see loc-1",
        cited_locators=("loc-1",),
    )
    fields.update(overrides)
    return ClientReceipt(**fields)


def _approval(oracle, locators=("loc-1",), **overrides):
    # Version-tolerant construction (R3): pass only the fields the
    # ScoringApproval on the HEAD under test actually supports, so the
    # approval-related tests fail on an old HEAD for their intended
    # reason (the attack succeeding) instead of at fixture construction
    # with ImportError for a helper the old HEAD never had. The old
    # HEAD's score_answer_approved / validate_receipt_approved accept
    # the same kwargs, so the attack logic itself is exercised there.
    import dataclasses
    from cba_kb.oracle import ScoringApproval, scoring_code_sha
    supported = {f.name for f in dataclasses.fields(ScoringApproval)}
    fields = dict(
        manifest_digest=oracle.artifact_digest,
        version=oracle.version,
        scoring_code_sha=scoring_code_sha(),
        snapshot_digest="b" * 64,
    )
    if "authorization_domain" in supported:
        fields["authorization_domain"] = "synthetic-domain"
    if "locator_set_digest" in supported:
        from cba_kb.oracle import locator_set_digest
        fields["locator_set_digest"] = locator_set_digest(locators)
    fields.update({k: v for k, v in overrides.items() if k in supported})
    return ScoringApproval(**fields)


def _run_cli(*argv, cwd=None):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_secrets.py"), *argv],
        text=True, capture_output=True, cwd=cwd)


def _git(tmp_path, *args):
    return subprocess.run(["git", *args], cwd=tmp_path, check=True,
                          text=True, capture_output=True)


def _commit(repo, message):
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", message)


# --- A1: one shared sensitive-path predicate -------------------------------

def test_safe_path_fingerprints_assignment_shaped_path():
    path = "dir/" + "api" + "_key=" + "abcdef"
    safe = SCANNER._safe_path(path)
    assert safe.startswith("[path-sha256:")
    assert "api" + "_key=" not in safe
    # _finding already fingerprinted such paths; the shared predicate must
    # not narrow it.
    assert SCANNER._finding(path, "S_X", b"v")["path"].startswith("[path-sha256:")


def test_safe_path_fingerprints_bearer_shaped_path():
    path = "Bear" + "er " + "tok" + "en12345"
    safe = SCANNER._safe_path(path)
    assert safe.startswith("[path-sha256:")
    assert "tok" + "en12345" not in safe
    assert SCANNER._safe_path("plain/path.txt") == "plain/path.txt"


# --- A2: bound digest verification ----------------------------------------

def test_bound_digest_verifies_trusted_record():
    tmp = Path(tempfile.mkdtemp())
    artifact = tmp / "good.bin"
    artifact.write_bytes(b"original")
    digest = hashlib.sha256(b"original").hexdigest()
    manifest = tmp / "expected.json"
    manifest.write_text(json.dumps({str(artifact): digest}))
    result = _run_cli("--bound-digest", str(artifact), "--manifest", str(manifest))
    assert result.returncode == 0
    assert "Bound digest verified" in result.stdout


def test_bound_digest_rejects_replacement_with_own_digest():
    tmp = Path(tempfile.mkdtemp())
    artifact = tmp / "good.bin"
    artifact.write_bytes(b"original")
    digest = hashlib.sha256(b"original").hexdigest()
    manifest = tmp / "expected.json"
    manifest.write_text(json.dumps({str(artifact): digest}))
    artifact.write_bytes(b"replacement")
    own_digest = hashlib.sha256(b"replacement").hexdigest()
    # Unbound --digest still accepts the replacement with its own digest:
    # that is exactly the hole the bound mode closes.
    pure = _run_cli("--digest", str(artifact), own_digest)
    assert pure.returncode == 0
    bound = _run_cli("--bound-digest", str(artifact), "--manifest", str(manifest))
    assert bound.returncode == 2
    assert "DIGEST_MISMATCH" in bound.stderr


def test_bound_digest_rejects_path_not_in_trusted_record():
    tmp = Path(tempfile.mkdtemp())
    recorded = tmp / "good.bin"
    recorded.write_bytes(b"original")
    digest = hashlib.sha256(b"original").hexdigest()
    manifest = tmp / "expected.json"
    manifest.write_text(json.dumps({str(recorded): digest}))
    other = tmp / "other.bin"
    other.write_bytes(b"original")
    result = _run_cli("--bound-digest", str(other), "--manifest", str(manifest))
    assert result.returncode == 2
    assert "PATH_NOT_IN_TRUSTED_RECORD" in result.stderr


def test_bound_digest_requires_manifest_and_rejects_malformed():
    tmp = Path(tempfile.mkdtemp())
    artifact = tmp / "good.bin"
    artifact.write_bytes(b"original")
    missing_manifest = _run_cli("--bound-digest", str(artifact))
    assert missing_manifest.returncode == 2
    assert "BOUND_MANIFEST_REQUIRED" in missing_manifest.stderr
    bad_manifest = tmp / "bad.json"
    bad_manifest.write_text("{not json")
    malformed = _run_cli("--bound-digest", str(artifact), "--manifest", str(bad_manifest))
    assert malformed.returncode == 2
    assert "MALFORMED_MANIFEST" in malformed.stderr


def test_verify_bound_digest_module_function():
    tmp = Path(tempfile.mkdtemp())
    artifact = tmp / "good.bin"
    artifact.write_bytes(b"original")
    digest = hashlib.sha256(b"original").hexdigest()
    records = SCANNER.load_expected_manifest(
        _write_manifest(tmp, {str(artifact): digest}))
    assert SCANNER.verify_bound_digest(str(artifact), records) == (True, "OK")
    artifact.write_bytes(b"replacement")
    assert SCANNER.verify_bound_digest(str(artifact), records) == (False, "DIGEST_MISMATCH")
    assert SCANNER.verify_bound_digest(
        str(tmp / "absent.bin"), records) == (False, "PATH_NOT_IN_TRUSTED_RECORD")


def _write_manifest(tmp, records):
    manifest = tmp / "expected.json"
    manifest.write_text(json.dumps(records))
    return str(manifest)


# --- A3: cumulative PR-commit scan -----------------------------------------

def _repo_with_deleted_secret(base):
    src = base / "src"
    _git(base, "init", "-q", "src")
    (src / "base.txt").write_text("base\n")
    _git(src, "add", "base.txt")
    _commit(src, "base")
    base_sha = _git(src, "rev-parse", "HEAD").stdout.strip()
    (src / "secret.txt").write_text("old notes " + synth_token() + "\n")
    _git(src, "add", "secret.txt")
    _commit(src, "add secret")
    (src / "secret.txt").unlink()
    _git(src, "add", "-A")
    _commit(src, "remove secret")
    return src, base_sha


def test_cumulative_flags_blob_deleted_before_head():
    base = Path(tempfile.mkdtemp())
    src, base_sha = _repo_with_deleted_secret(base)
    tracked = _run_cli("--tracked", cwd=str(src))
    assert tracked.returncode == 0  # final HEAD is clean: the escape case
    cumulative = _run_cli("--cumulative", "--base", base_sha, cwd=str(src))
    assert cumulative.returncode == 1
    assert "S_DRIVE_ID_MORPHOLOGY" in cumulative.stderr


def test_cumulative_uses_default_base():
    base = Path(tempfile.mkdtemp())
    src = base / "src"
    _git(base, "init", "-q", "src")
    _git(src, "symbolic-ref", "HEAD", "refs/heads/main")
    (src / "base.txt").write_text("base\n")
    _git(src, "add", "base.txt")
    _commit(src, "base")
    _git(src, "checkout", "-qb", "feature")
    (src / "secret.txt").write_text("old notes " + synth_token() + "\n")
    _git(src, "add", "secret.txt")
    _commit(src, "add secret")
    (src / "secret.txt").unlink()
    _git(src, "add", "-A")
    _commit(src, "remove secret")
    cumulative = _run_cli("--cumulative", cwd=str(src))
    assert cumulative.returncode == 1
    assert "S_DRIVE_ID_MORPHOLOGY" in cumulative.stderr


def test_cumulative_fails_closed_on_shallow_and_unresolvable_base():
    base = Path(tempfile.mkdtemp())
    src = base / "src"
    _git(base, "init", "-q", "src")
    (src / "a.txt").write_text("a\n")
    _git(src, "add", "a.txt")
    _commit(src, "one")
    (src / "b.txt").write_text("b\n")
    _git(src, "add", "b.txt")
    _commit(src, "two")
    subprocess.run(["git", "clone", "-q", "--depth", "1", "file://" + str(src),
                    str(base / "shallow")], check=True, capture_output=True)
    shallow = _run_cli("--cumulative", cwd=str(base / "shallow"))
    assert shallow.returncode == 2
    assert "S_HISTORY_UNAVAILABLE" in shallow.stderr
    solo = base / "solo"
    _git(base, "init", "-q", "solo")
    _git(solo, "symbolic-ref", "HEAD", "refs/heads/solo")
    (solo / "only.txt").write_text("only\n")
    _git(solo, "add", "only.txt")
    _commit(solo, "only")
    unresolvable = _run_cli("--cumulative", cwd=str(solo))
    assert unresolvable.returncode == 2
    assert "S_HISTORY_UNAVAILABLE" in unresolvable.stderr


# --- B1: approval-bound formal scoring -------------------------------------

def test_formal_scoring_approves_bound_oracle():
    from cba_kb.oracle import (ScoringApproval, locator_set_digest,
                               score_answer_approved, scoring_code_sha)
    import cba_kb.oracle as oracle_module
    oracle = _oracle_v2()
    assert scoring_code_sha() == hashlib.sha256(
        Path(oracle_module.__file__).read_bytes()).hexdigest()
    approval = ScoringApproval(
        manifest_digest=oracle.artifact_digest, version=oracle.version,
        scoring_code_sha=scoring_code_sha(), snapshot_digest="b" * 64,
        authorization_domain="synthetic-domain",
        locator_set_digest=locator_set_digest(["loc-1"]))
    result = score_answer_approved(
        oracle.question("q-count"), {"text": "12", "value": "12", "cited_locators": ["loc-1"]},
        oracle=oracle, approval=approval, snapshot_digest="b" * 64,
        authorized_locators=["loc-1"])
    assert result.verdict == "PASS"


def test_formal_scoring_rejects_replaced_manifest():
    from cba_kb.oracle import score_answer_approved
    oracle_v1 = _oracle_v2()
    oracle_v2 = build_oracle(
        version="oracle-v2-synthetic",
        sources=[OracleSource("src-synth", "synthetic primary source", "LAWFUL_PUBLIC_SYNTHETIC")],
        questions=[
            OracleQuestion("q-count", "synthetic count?", "exact", expected=13,
                           mandatory_locator=True),
            OracleQuestion("q-loc", "synthetic locator?", "locator", expected="loc-1",
                           mandatory_locator=True),
            OracleQuestion("q-unk", "synthetic unknown?", "unknown"),
            OracleQuestion("q-rate", "synthetic rate?", "rate", expected=50.0,
                           unit="percent", precision=1, tolerance=0.05),
        ],
    )
    approval_v1 = _approval(oracle_v1)
    # Score the replacement question against the replacement Oracle with an
    # approval pinned to the original: must fail closed.
    try:
        score_answer_approved(
            oracle_v2.question("q-count"), {"text": "13", "value": "13",
                                            "cited_locators": ["loc-1"]},
            oracle=oracle_v2, approval=approval_v1, snapshot_digest="b" * 64,
            authorized_locators=["loc-1"])
    except OracleError:
        pass
    else:
        raise AssertionError("replaced manifest must not score under the old approval")
    # Approval built from the replacement manifest must not score the original.
    approval_v2 = _approval(oracle_v2)
    try:
        score_answer_approved(
            oracle_v1.question("q-count"), {"text": "12", "value": "12",
                                            "cited_locators": ["loc-1"]},
            oracle=oracle_v1, approval=approval_v2, snapshot_digest="b" * 64,
            authorized_locators=["loc-1"])
    except OracleError:
        pass
    else:
        raise AssertionError("original Oracle must not score under the replacement approval")


def test_formal_scoring_rejects_wrong_code_sha_and_snapshot():
    from cba_kb.oracle import score_answer_approved
    oracle = _oracle_v2()
    wrong_code = _approval(oracle, scoring_code_sha="0" * 64)
    try:
        score_answer_approved(
            oracle.question("q-count"), {"text": "12", "value": "12", "cited_locators": ["loc-1"]},
            oracle=oracle, approval=wrong_code, snapshot_digest="b" * 64,
            authorized_locators=["loc-1"])
    except OracleError:
        pass
    else:
        raise AssertionError("wrong scoring-code SHA must fail closed")
    wrong_snapshot = _approval(oracle, snapshot_digest="c" * 64)
    try:
        score_answer_approved(
            oracle.question("q-count"), {"text": "12", "value": "12", "cited_locators": ["loc-1"]},
            oracle=oracle, approval=wrong_snapshot, snapshot_digest="b" * 64,
            authorized_locators=["loc-1"])
    except OracleError:
        pass
    else:
        raise AssertionError("approval snapshot must match the applicable Snapshot")
    try:
        score_answer_approved(
            oracle.question("q-count"), {"text": "12", "value": "12", "cited_locators": ["loc-1"]},
            oracle=oracle, approval=None, snapshot_digest="b" * 64,
            authorized_locators=["loc-1"])
    except OracleError:
        pass
    else:
        raise AssertionError("missing approval must fail closed")


def test_formal_scoring_rejects_question_outside_oracle():
    from cba_kb.oracle import score_answer_approved
    oracle = _oracle_v2()
    outsider = OracleQuestion("q-count", "synthetic count?", "exact", expected=99,
                              mandatory_locator=True)
    try:
        score_answer_approved(
            outsider, {"text": "99", "value": "99", "cited_locators": ["loc-1"]},
            oracle=oracle, approval=_approval(oracle), snapshot_digest="b" * 64,
            authorized_locators=["loc-1"])
    except OracleError:
        pass
    else:
        raise AssertionError("a question outside the approved Oracle must not score")


# --- B2: structured value vs verbatim text ----------------------------------

def test_structured_value_contradicting_verbatim_text_fails():
    oracle = _oracle_v2()
    count = oracle.question("q-count")
    contra = score_answer(
        count, {"text": "13", "value": "12", "cited_locators": ["loc-1"]},
        authorized_locators=["loc-1"])
    assert contra.verdict == "FAIL" and contra.dimension == "consistency"
    assert contra.critical is True
    rate = oracle.question("q-rate")
    rate_contra = score_answer(
        rate, {"text": "51.0", "value": 50.0, "unit": "percent"})
    assert rate_contra.verdict == "FAIL" and rate_contra.dimension == "consistency"
    good = score_answer(
        count, {"text": "12", "value": "12", "cited_locators": ["loc-1"]},
        authorized_locators=["loc-1"])
    assert good.verdict == "PASS"
    no_number = score_answer(
        count, {"text": "twelve", "value": "12", "cited_locators": ["loc-1"]},
        authorized_locators=["loc-1"])
    assert no_number.verdict == "PASS"
    two_numbers = score_answer(
        count, {"text": "12 and 13", "value": "12", "cited_locators": ["loc-1"]},
        authorized_locators=["loc-1"])
    # R2 semantics change (Q2 re-review blocker 4): multi-number text
    # whose numbers disagree with the structured value FAILS closed
    # instead of being skipped. The old PASS expectation was the defect
    # the reviewer flagged; it is corrected here, not weakened.
    assert two_numbers.verdict == "FAIL" and two_numbers.dimension == "consistency"
    assert two_numbers.critical is True


def test_locator_fragment_in_text_is_not_an_asserted_count():
    # No-regression guard (not a falsification test): a text that only
    # references a locator ("loc-1", "Q1") asserts no count, so the
    # cross-check must skip it instead of reading the attached digit as
    # the asserted number. Passes on the old HEAD too (no cross-check
    # there); guards the new rule against false positives.
    oracle = _oracle_v2()
    count = oracle.question("q-count")
    for text in ("see loc-1", "Q1", "see loc-1 for the count"):
        result = score_answer(
            count, {"text": text, "value": "12", "cited_locators": ["loc-1"]},
            authorized_locators=["loc-1"])
        assert result.verdict == "PASS", text


# --- B3: formal receipt acceptance ------------------------------------------

def test_formal_receipt_acceptance_requires_approval_binding():
    from cba_kb.oracle import validate_receipt_approved
    oracle = _oracle_v2()
    receipt = _receipt(oracle)
    approval = _approval(oracle)
    assert validate_receipt_approved(
        receipt, oracle=oracle, approval=approval,
        snapshot_digest="b" * 64, authorization_domain="synthetic-domain",
        authorized_locators=["loc-1"]) == []
    changed_snapshot = _receipt(oracle, snapshot_digest="c" * 64)
    assert validate_receipt_approved(
        changed_snapshot, oracle=oracle, approval=approval,
        snapshot_digest="b" * 64, authorization_domain="synthetic-domain",
        authorized_locators=["loc-1"]) != []
    changed_domain = _receipt(oracle, authorization_domain="other-domain")
    assert validate_receipt_approved(
        changed_domain, oracle=oracle, approval=approval,
        snapshot_digest="b" * 64, authorization_domain="synthetic-domain",
        authorized_locators=["loc-1"]) != []
    assert validate_receipt_approved(
        receipt, oracle=oracle, approval=None,
        snapshot_digest="b" * 64, authorization_domain="synthetic-domain",
        authorized_locators=["loc-1"]) != []
    assert validate_receipt_approved(
        receipt, oracle=oracle, approval=_approval(oracle, manifest_digest="d" * 64),
        snapshot_digest="b" * 64, authorization_domain="synthetic-domain",
        authorized_locators=["loc-1"]) != []
    # The scaffolding-only validator with omitted expectations still passes
    # the substituted receipt: that is why formal acceptance must use the
    # new entrypoint.
    assert validate_receipt(changed_snapshot, oracle=oracle,
                            authorized_locators=["loc-1"]) == []


# --- B4: repeat adjudication compares critical dimensions --------------------

def test_repeat_adjudication_compares_critical_dimensions():
    same_verdict_different_dimensions = [
        ScoreResult("q", "PASS", "value", False, "a"),
        ScoreResult("q", "PASS", "locator", False, "b"),
        ScoreResult("q", "PASS", "value", False, "a"),
    ]
    assert adjudicate_repeat(same_verdict_different_dimensions) == ("ADJUDICATION_REQUIRED", None)
    different_criticality = [
        ScoreResult("q", "FAIL", "value", True, "a"),
        ScoreResult("q", "FAIL", "value", False, "b"),
        ScoreResult("q", "FAIL", "value", True, "a"),
    ]
    assert adjudicate_repeat(different_criticality) == ("ADJUDICATION_REQUIRED", None)
    identical = [ScoreResult("q", "PASS", "value", False, "a")] * 3
    assert adjudicate_repeat(identical) == ("AGREED", "PASS")


# --- B5: error strings never echo raw input ----------------------------------

def test_error_strings_never_echo_raw_input():
    from cba_kb.oracle import score_answer_approved, validate_receipt_approved
    oracle = _oracle_v2()
    token = synth_token("C")
    receipt = _receipt(oracle, cited_locators=(token,))
    errors = validate_receipt_approved(
        receipt, oracle=oracle, approval=_approval(oracle),
        snapshot_digest="b" * 64, authorization_domain="synthetic-domain",
        authorized_locators=["loc-1"])
    assert errors
    assert token not in json.dumps(errors)
    assert "sha256:" in json.dumps(errors)
    try:
        score_answer_approved(
            oracle.question("q-count"), {"text": "12", "value": "12", "cited_locators": ["loc-1"]},
            oracle=oracle, approval=_approval(oracle, manifest_digest="d" * 64),
            snapshot_digest="b" * 64, authorized_locators=["loc-1"])
    except OracleError as exc:
        assert "d" * 64 not in str(exc)
    else:
        raise AssertionError("wrong approval must raise")
    tmp = Path(tempfile.mkdtemp())
    artifact = tmp / "good.bin"
    artifact.write_bytes(b"original")
    digest = hashlib.sha256(b"original").hexdigest()
    manifest = tmp / "expected.json"
    manifest.write_text(json.dumps({str(artifact): digest}))
    leaky_path = tmp / ("dir-" + "api" + "_key=" + "abcdef") / "file.bin"
    result = _run_cli("--bound-digest", str(leaky_path), "--manifest", str(manifest))
    assert result.returncode == 2
    assert "PATH_NOT_IN_TRUSTED_RECORD" in result.stderr
    assert "api" + "_key=" not in result.stderr


# --- R2: Q2 re-review Round 1 blockers (FAIL on HEAD 677b5438) -----------

# B1: substituted questions must not reuse an old approval ------------------

def test_r2_formal_scoring_rejects_substituted_question():
    from cba_kb.oracle import score_answer_approved
    oracle = _oracle_v2()
    approval = _approval(oracle)
    tampered = _oracle_v2()
    # Replace only the first question's expected value; the stored
    # artifact_digest is NOT recomputed (frozen field), so the old code
    # compares the approval against the untouched stored digest and PASSes.
    object.__setattr__(tampered, "questions",
                       (OracleQuestion("q-count", "synthetic count?", "exact",
                                       expected=13, mandatory_locator=True),)
                       + tampered.questions[1:])
    assert tampered.artifact_digest == oracle.artifact_digest
    try:
        score_answer_approved(
            tampered.question("q-count"),
            {"text": "13", "value": "13", "cited_locators": ["loc-1"]},
            oracle=tampered, approval=approval, snapshot_digest="b" * 64,
            authorized_locators=["loc-1"])
    except OracleError:
        pass
    else:
        raise AssertionError("a substituted question must not score under the old approval")


def test_r2_formal_scoring_rejects_duck_typed_oracle():
    from cba_kb.oracle import score_answer_approved
    from types import SimpleNamespace
    oracle = _oracle_v2()
    approval = _approval(oracle)
    fake = SimpleNamespace(
        version=oracle.version, sources=oracle.sources,
        questions=oracle.questions,
        scoring_spec_version=oracle.scoring_spec_version,
        artifact_digest=oracle.artifact_digest)
    try:
        score_answer_approved(
            oracle.question("q-count"),
            {"text": "12", "value": "12", "cited_locators": ["loc-1"]},
            oracle=fake, approval=approval, snapshot_digest="b" * 64,
            authorized_locators=["loc-1"])
    except OracleError:
        pass
    else:
        raise AssertionError("a duck-typed stand-in is not a real Oracle")


# B2: lookup identity must equal read identity ------------------------------

def test_r2_bound_digest_alias_and_real_path_agree():
    tmp = Path(tempfile.mkdtemp())
    actual_dir = tmp / "actual"
    actual_dir.mkdir()
    real = actual_dir / "good"
    real.write_bytes(b"original")
    digest = hashlib.sha256(b"original").hexdigest()
    link = tmp / "good"
    os.symlink(real, link)
    manifest = tmp / "expected.json"
    manifest.write_text(json.dumps({str(link): digest}))
    records = SCANNER.load_expected_manifest(str(manifest))
    # The invariant is agreement, not a particular return value: the
    # alias, the real path, and a ".." alias of the same file must all
    # receive the same decision. The old code decides differently for
    # the same inode.
    via_alias = SCANNER.verify_bound_digest(str(link), records)
    via_real = SCANNER.verify_bound_digest(str(real), records)
    via_dotdot = SCANNER.verify_bound_digest(str(tmp / "actual" / ".." / "good"), records)
    assert via_alias == via_real == via_dotdot


def test_r2_manifest_rejects_duplicate_key():
    tmp = Path(tempfile.mkdtemp())
    artifact = tmp / "good.bin"
    artifact.write_bytes(b"original")
    digest = hashlib.sha256(b"original").hexdigest()
    other = hashlib.sha256(b"other").hexdigest()
    manifest = tmp / "expected.json"
    manifest.write_text(
        '{"' + str(artifact) + '": "' + digest + '", '
        '"' + str(artifact) + '": "' + other + '"}')
    try:
        SCANNER.load_expected_manifest(str(manifest))
    except ValueError as exc:
        assert "MALFORMED_MANIFEST" in str(exc)
    else:
        raise AssertionError("a manifest with a duplicate key must fail closed")


def test_r2_manifest_rejects_normalized_duplicate_path():
    tmp = Path(tempfile.mkdtemp())
    artifact = tmp / "good.bin"
    artifact.write_bytes(b"original")
    digest = hashlib.sha256(b"original").hexdigest()
    manifest = tmp / "expected.json"
    # NOTE: the "./" alias must be built as a raw string: pathlib
    # normalizes "." away, so Path(tmp / "." / "good.bin") would collapse
    # to the same key before the manifest is even written.
    manifest.write_text(json.dumps(
        {str(artifact): digest, str(tmp) + "/./good.bin": digest}))
    try:
        SCANNER.load_expected_manifest(str(manifest))
    except ValueError as exc:
        assert "MALFORMED_MANIFEST" in str(exc)
    else:
        raise AssertionError("two keys normalizing to one path must fail closed")


# B3: cumulative scan enumerates per-commit file changes ---------------------

def _repo_with_copied_base_blob(base):
    src = base / "src"
    _git(base, "init", "-q", "src")
    (src / "orig.txt").write_text("old notes " + synth_token() + "\n")
    _git(src, "add", "orig.txt")
    _commit(src, "base")
    base_sha = _git(src, "rev-parse", "HEAD").stdout.strip()
    (src / "new.txt").write_text((src / "orig.txt").read_text())
    _git(src, "add", "new.txt")
    _commit(src, "copy to new path")
    (src / "new.txt").unlink()
    _git(src, "add", "-A")
    _commit(src, "remove copy")
    # R3 fixture fix (Human-approved 2026-11-05): the original file is
    # also removed at the final HEAD, so the final HEAD is genuinely
    # clean under EVERY scan mode -- the previous fixture left orig.txt
    # tracked, so "--tracked --morphology" flagged it and the test's
    # "final HEAD is clean" premise held only for the morphology-off
    # invocation.
    (src / "orig.txt").unlink()
    _git(src, "add", "-A")
    _commit(src, "remove original")
    return src, base_sha


def test_r2_cumulative_flags_copied_base_blob_deleted_before_head():
    base = Path(tempfile.mkdtemp())
    src, base_sha = _repo_with_copied_base_blob(base)
    tracked = _run_cli("--tracked", cwd=str(src))
    assert tracked.returncode == 0  # final HEAD is clean: the escape case
    tracked_morph = _run_cli("--tracked", "--morphology", cwd=str(src))
    assert tracked_morph.returncode == 0  # clean under morphology too
    cumulative = _run_cli("--cumulative", "--base", base_sha, cwd=str(src))
    assert cumulative.returncode == 1
    assert "S_DRIVE_ID_MORPHOLOGY" in cumulative.stderr


def test_r2_cumulative_rejects_base_equal_to_head():
    base = Path(tempfile.mkdtemp())
    src, base_sha = _repo_with_deleted_secret(base)
    result = _run_cli("--cumulative", "--base", "HEAD", cwd=str(src))
    assert result.returncode == 2
    assert "S_HISTORY_UNAVAILABLE" in result.stderr


# B4: decimal / thousands / underscore number parsing ------------------------

def test_r2_decimal_contradiction_fails():
    oracle = _oracle_v2()
    count = oracle.question("q-count")
    contra = score_answer(
        count, {"text": "13.0", "value": "12", "cited_locators": ["loc-1"]},
        authorized_locators=["loc-1"])
    assert contra.verdict == "FAIL" and contra.dimension == "consistency"
    assert contra.critical is True
    agreeing = score_answer(
        count, {"text": "12.0", "value": "12", "cited_locators": ["loc-1"]},
        authorized_locators=["loc-1"])
    assert agreeing.verdict == "PASS"


def test_r2_underscore_and_comma_thousands_agree():
    big = build_oracle(
        version="oracle-v2-synthetic",
        sources=[OracleSource("src-synth", "synthetic primary source",
                              "LAWFUL_PUBLIC_SYNTHETIC")],
        questions=[OracleQuestion("q-big", "synthetic big count?", "exact",
                                  expected=1000, mandatory_locator=True)],
    )
    for text in ("1_000", "1,000"):
        result = score_answer(
            big.question("q-big"),
            {"text": text, "value": "1000", "cited_locators": ["loc-1"]},
            authorized_locators=["loc-1"])
        assert result.verdict == "PASS", text


def test_r2_multi_number_contradiction_fails_closed():
    oracle = _oracle_v2()
    count = oracle.question("q-count")
    result = score_answer(
        count, {"text": "12 and 13", "value": "12", "cited_locators": ["loc-1"]},
        authorized_locators=["loc-1"])
    assert result.verdict == "FAIL" and result.dimension == "consistency"
    assert result.critical is True


# B5: repeat trigger compares critical dimensions ----------------------------

def test_r2_repeat_required_sees_dimension_disagreement():
    from cba_kb.oracle import repeat_required
    assert repeat_required([
        ScoreResult("q", "FAIL", "value", True, "a"),
        ScoreResult("q", "FAIL", "locator", True, "b"),
    ]) is True
    assert repeat_required([
        ScoreResult("q", "PASS", "value", False, "a"),
        ScoreResult("q", "PASS", "value", False, "a"),
    ]) is False


# B6(a): authorization domain is bound by the approval ------------------------

def test_r2_formal_receipt_acceptance_binds_authorization_domain():
    from cba_kb.oracle import validate_receipt_approved
    oracle = _oracle_v2()
    receipt = _receipt(oracle)
    approval = _approval(oracle)
    assert validate_receipt_approved(
        receipt, oracle=oracle, approval=approval,
        snapshot_digest="b" * 64, authorization_domain="synthetic-domain",
        authorized_locators=["loc-1"]) == []
    # Caller changes the receipt domain AND passes the same changed
    # domain: must still fail, because the approval binds synthetic-domain.
    changed = _receipt(oracle, authorization_domain="other-domain")
    assert validate_receipt_approved(
        changed, oracle=oracle, approval=approval,
        snapshot_digest="b" * 64, authorization_domain="other-domain",
        authorized_locators=["loc-1"]) != []
    # An approval built for another domain must not accept the receipt.
    other_approval = _approval(oracle, authorization_domain="other-domain")
    assert validate_receipt_approved(
        receipt, oracle=oracle, approval=other_approval,
        snapshot_digest="b" * 64, authorization_domain="synthetic-domain",
        authorized_locators=["loc-1"]) != []


# B6(b): locator allowlist is bound to the approval ---------------------------

def test_r2_formal_scoring_binds_locator_set_to_approval():
    from cba_kb.oracle import score_answer_approved
    oracle = _oracle_v2()
    approval = _approval(oracle)
    # A foreign locator smuggled in through the caller-supplied allowlist.
    try:
        score_answer_approved(
            oracle.question("q-count"),
            {"text": "12", "value": "12", "cited_locators": ["loc-attacker"]},
            oracle=oracle, approval=approval, snapshot_digest="b" * 64,
            authorized_locators=["loc-attacker"])
    except OracleError:
        pass
    else:
        raise AssertionError("a locator outside the approved set must not score")
    # The approved set still scores.
    result = score_answer_approved(
        oracle.question("q-count"),
        {"text": "12", "value": "12", "cited_locators": ["loc-1"]},
        oracle=oracle, approval=approval, snapshot_digest="b" * 64,
        authorized_locators=["loc-1"])
    assert result.verdict == "PASS"


def test_r2_locator_set_digest_derives_from_real_snapshot():
    from cba_kb.kc_snapshot import build_snapshot, validate_snapshot
    from cba_kb.oracle import (locator_set_digest, score_answer_approved,
                               snapshot_locators)
    oracle = _oracle_v2()
    snapshot = build_snapshot(
        entries=[{"entry_id": "loc-1", "sha256": "a" * 64,
                  "authority": "canonical", "rights": "public",
                  "source_id": "src-synth"}],
        target_domain="synthetic-domain", capabilities=["stats"],
        build_provenance="synthetic-build-1",
        build_time="2026-11-05T00:00:00+00:00",
        data_release="SIMULATED_DATA_RELEASE_1",
        engine_candidate="SIMULATED_ENGINE_CANDIDATE_1",
        build_code="SIMULATED_BUILD_CODE_1")
    assert validate_snapshot(snapshot)["valid"] is True
    locators = snapshot_locators(snapshot)
    assert locators == ("loc-1",)
    assert locator_set_digest(locators) == locator_set_digest(["loc-1"])
    approval = _approval(oracle, locators=locators)
    result = score_answer_approved(
        oracle.question("q-count"),
        {"text": "12", "value": "12", "cited_locators": ["loc-1"]},
        oracle=oracle, approval=approval, snapshot_digest="b" * 64,
        authorized_locators=["loc-1"])
    assert result.verdict == "PASS"
    try:
        score_answer_approved(
            oracle.question("q-count"),
            {"text": "12", "value": "12", "cited_locators": ["loc-attacker"]},
            oracle=oracle, approval=approval, snapshot_digest="b" * 64,
            authorized_locators=["loc-attacker"])
    except OracleError:
        pass
    else:
        raise AssertionError("a locator outside the Snapshot-derived set must not score")


# B6(d): loader error text carries no raw OS text ------------------------------

def test_r2_manifest_loader_error_hides_os_text():
    tmp = Path(tempfile.mkdtemp())
    directory = tmp / "adir"
    directory.mkdir()
    try:
        SCANNER.load_expected_manifest(str(directory))
    except ValueError as exc:
        assert "MANIFEST_UNREADABLE" in str(exc)
        assert str(directory) not in str(exc)
    else:
        raise AssertionError("a directory manifest must fail closed")


# --- R3: Q2 re-review Round 2 blockers (FAIL on HEAD 8bcd8875) ---------------
# Each test below fails on HEAD 8bcd8875 for its intended reason and passes
# on the repaired HEAD, except the Snapshot-pairing test, which is labelled
# in place as a capability test passing on the R3 baseline HEAD and the
# repaired HEAD by design.

# B1: a question that only CLAIMS equality must not score ---------------------

class _LyingEqQuestion:
    """Question-shaped object whose __eq__ always reports True."""

    question_id = "q-count"
    comparison = "exact"
    expected = 13
    mandatory_locator = True

    def __eq__(self, other):
        return True

    def __hash__(self):
        return hash("q-count")


def test_r3_formal_scoring_rejects_lying_eq_question():
    from cba_kb.oracle import score_answer_approved
    oracle = _oracle_v2()
    approval = _approval(oracle)
    fake = _LyingEqQuestion()
    # The fake claims equality with the approved question while carrying a
    # different expected value; the old code judges membership with the
    # caller's __eq__ and then scores the caller's object.
    assert fake in oracle.questions
    try:
        score_answer_approved(
            fake, {"text": "13", "value": "13", "cited_locators": ["loc-1"]},
            oracle=oracle, approval=approval, snapshot_digest="b" * 64,
            authorized_locators=["loc-1"])
    except OracleError:
        pass
    else:
        raise AssertionError("a question that only claims equality must not score")


# B2: a blob restored at its ORIGINAL path must be caught ---------------------

def _repo_with_restored_base_blob(base):
    src = base / "src"
    _git(base, "init", "-q", "src")
    (src / "orig.txt").write_text("old notes " + synth_token() + "\n")
    _git(src, "add", "orig.txt")
    _commit(src, "base")
    base_sha = _git(src, "rev-parse", "HEAD").stdout.strip()
    (src / "orig.txt").unlink()
    _git(src, "add", "-A")
    _commit(src, "delete orig")
    (src / "orig.txt").write_text("old notes " + synth_token() + "\n")
    _git(src, "add", "orig.txt")
    _commit(src, "restore orig")
    (src / "orig.txt").unlink()
    _git(src, "add", "-A")
    _commit(src, "delete orig again")
    return src, base_sha


def test_r3_cumulative_flags_restored_base_blob_at_original_path():
    base = Path(tempfile.mkdtemp())
    src, base_sha = _repo_with_restored_base_blob(base)
    tracked = _run_cli("--tracked", cwd=str(src))
    assert tracked.returncode == 0  # final HEAD is clean
    cumulative = _run_cli("--cumulative", "--base", base_sha, cwd=str(src))
    assert cumulative.returncode == 1
    assert "S_DRIVE_ID_MORPHOLOGY" in cumulative.stderr


# B3: a valid non-UTF-8 Git filename must not break enumeration --------------

def test_r3_cumulative_survives_non_utf8_filename():
    base = Path(tempfile.mkdtemp())
    src = base / "src"
    _git(base, "init", "-q", "src")
    (src / "plain.txt").write_text("hello\n")
    _git(src, "add", "plain.txt")
    _commit(src, "base")
    base_sha = _git(src, "rev-parse", "HEAD").stdout.strip()
    # A tracked filename holding byte 0xff is a valid Git filename; the
    # old code decoded the whole ls-tree output as strict UTF-8 and
    # raised UnicodeDecodeError instead of failing closed.
    bad = b"bad\xffname.txt"
    with open(os.path.join(os.fsencode(str(src)), bad), "wb") as fh:
        fh.write(b"notes\n")
    subprocess.run([b"git", b"add", bad], cwd=str(src), check=True,
                   capture_output=True)
    _commit(src, "add non-utf8 name")
    findings = SCANNER.cumulative_findings(str(src), base=base_sha)
    assert findings == []  # the file holds no morphology tokens


# B4: exact counts keep full precision; exponents and unit suffixes parse ----

def _oracle_big():
    return build_oracle(
        version="oracle-v3-synthetic",
        sources=[OracleSource("src-synth", "synthetic primary source",
                              "LAWFUL_PUBLIC_SYNTHETIC")],
        questions=[OracleQuestion("q-big", "synthetic big count?", "exact",
                                  expected=9007199254740992,
                                  mandatory_locator=True)],
    )


def test_r3_big_int_contradiction_fails():
    oracle = _oracle_big()
    big = oracle.question("q-big")
    contra = score_answer(
        big, {"text": "9007199254740993", "value": "9007199254740992",
              "cited_locators": ["loc-1"]},
        authorized_locators=["loc-1"])
    assert contra.verdict == "FAIL" and contra.dimension == "consistency"
    assert contra.critical is True


def test_r3_big_int_agreement_has_no_consistency_false_positive():
    oracle = _oracle_big()
    big = oracle.question("q-big")
    result = score_answer(
        big, {"text": "9007199254740993", "value": "9007199254740993",
              "cited_locators": ["loc-1"]},
        authorized_locators=["loc-1"])
    # The value itself mismatches the approved expected count, so the
    # answer must fail -- but on the VALUE dimension, never on
    # consistency: the verbatim number and the structured value agree.
    assert result.verdict == "FAIL" and result.dimension == "value"


def test_r3_scientific_notation_contradiction_fails():
    oracle = _oracle_v2()
    count = oracle.question("q-count")
    contra = score_answer(
        count, {"text": "1.3e1", "value": "12", "cited_locators": ["loc-1"]},
        authorized_locators=["loc-1"])
    assert contra.verdict == "FAIL" and contra.dimension == "consistency"
    assert contra.critical is True


def test_r3_unit_suffixed_decimal_contradiction_fails():
    oracle = _oracle_v2()
    count = oracle.question("q-count")
    contra = score_answer(
        count, {"text": "13.0points", "value": "12",
                "cited_locators": ["loc-1"]},
        authorized_locators=["loc-1"])
    assert contra.verdict == "FAIL" and contra.dimension == "consistency"
    assert contra.critical is True


# B5-chain: the loader must not expose the raw path via its traceback --------

def test_r3_manifest_loader_error_suppresses_exception_chain():
    import traceback
    tmp = Path(tempfile.mkdtemp())
    directory = tmp / "adir"
    directory.mkdir()
    try:
        SCANNER.load_expected_manifest(str(directory))
    except ValueError as exc:
        assert "MANIFEST_UNREADABLE" in str(exc)
        assert str(directory) not in str(exc)
        # The chained OSError carries the raw path; `from None` must
        # suppress it so no formatted traceback exposes the path. The
        # correct assertion is __suppress_context__, not
        # __context__ is None (from None sets the flag, the context
        # object itself stays attached but unformatted).
        assert exc.__suppress_context__ is True
        assert str(directory) not in traceback.format_exc()
    else:
        raise AssertionError("a directory manifest must fail closed")


# Pairing gap (capability test, NOT a falsification test) ----------------------
# The round-2 test proved the locator-set digest binding with a "b" * 64
# placeholder for snapshot_digest and never the Snapshot's real
# content_sha256. This test closes that evidence gap with a real Snapshot
# digest. It PASSES ON THE R3 BASELINE HEAD 8bcd8875 AND THE REPAIRED
# HEAD by design: the approval binding already compared snapshot digests
# exactly on the old HEAD, so the old HEAD also accepts the real digest
# and rejects a different one. (It fails on the older 677b5438 only
# because the locator-set entrypoints did not exist there yet -- API
# absence, not the attack.) Its value is the positive evidence that the
# issuer pairing works end to end with a real Snapshot, plus the negative
# case on the new HEAD.

def test_r3_approval_pairs_locator_set_with_real_snapshot_digest():
    from cba_kb.kc_snapshot import build_snapshot, validate_snapshot
    from cba_kb.oracle import (locator_set_digest, score_answer_approved,
                               snapshot_locators)
    oracle = _oracle_v2()

    def _snapshot(domain):
        return build_snapshot(
            entries=[{"entry_id": "loc-1", "sha256": "a" * 64,
                      "authority": "canonical", "rights": "public",
                      "source_id": "src-synth"}],
            target_domain=domain, capabilities=["stats"],
            build_provenance="synthetic-build-1",
            build_time="2026-11-05T00:00:00+00:00",
            data_release="SIMULATED_DATA_RELEASE_1",
            engine_candidate="SIMULATED_ENGINE_CANDIDATE_1",
            build_code="SIMULATED_BUILD_CODE_1")

    snapshot = _snapshot("synthetic-domain")
    assert validate_snapshot(snapshot)["valid"] is True
    other = _snapshot("other-domain")
    assert other.content_sha256 != snapshot.content_sha256
    locators = snapshot_locators(snapshot)
    assert locators == ("loc-1",)
    approval = _approval(oracle, locators=locators,
                         snapshot_digest=snapshot.content_sha256)
    result = score_answer_approved(
        oracle.question("q-count"),
        {"text": "12", "value": "12", "cited_locators": ["loc-1"]},
        oracle=oracle, approval=approval,
        snapshot_digest=snapshot.content_sha256,
        authorized_locators=["loc-1"])
    assert result.verdict == "PASS"
    try:
        score_answer_approved(
            oracle.question("q-count"),
            {"text": "12", "value": "12", "cited_locators": ["loc-1"]},
            oracle=oracle, approval=approval,
            snapshot_digest=other.content_sha256,
            authorized_locators=["loc-1"])
    except OracleError:
        pass
    else:
        raise AssertionError("a different Snapshot digest must not score under this approval")


# --- R4: Q2 re-review Round 3 blockers (FAIL on HEAD 8ad490c0) ---------------
# Each test below fails on HEAD 8ad490c0 for its intended reason and passes
# on the repaired HEAD.

# B1: an OracleQuestion SUBCLASS with a lying __eq__ must not pass the
# formal field-match check ----------------------------------------------------

class _LyingEqSubclass(OracleQuestion):
    """Real OracleQuestion subclass whose __eq__ always reports True."""

    def __eq__(self, other):
        return True

    def __hash__(self):
        return hash("q-count")


def test_r4_formal_scoring_rejects_lying_eq_subclass():
    from cba_kb.oracle import score_answer_approved
    oracle = _oracle_v2()
    approval = _approval(oracle)
    fake = _LyingEqSubclass("q-count", "synthetic count?", "exact",
                            expected=13, mandatory_locator=True)
    # isinstance admits the subclass, and the old field check
    # (approved != question) consults the subclass's lying __eq__ via
    # the reflected comparison, so the check passes and a score is
    # produced instead of OracleError. Scoring still uses the approved
    # object, so the original substituted-expectation exploit stays
    # closed -- but the promised rejection of a question whose fields
    # differ is bypassed.
    assert fake in oracle.questions
    try:
        score_answer_approved(
            fake, {"text": "13", "value": "13", "cited_locators": ["loc-1"]},
            oracle=oracle, approval=approval, snapshot_digest="b" * 64,
            authorized_locators=["loc-1"])
    except OracleError:
        pass
    else:
        raise AssertionError("a subclass with a lying __eq__ must not score")


# B2: a recognized exponent token that Decimal cannot convert must fail
# the consistency check, never be silently dropped ----------------------------

def test_r4_unconvertible_exponent_token_fails_consistency():
    oracle = _oracle_v2()
    count = oracle.question("q-count")
    # "13e99999999999999999999" matches the number regex, but Decimal
    # raises InvalidOperation on it; the old parser dropped it, so the
    # check saw no contradiction and the matching structured value 12
    # passed.
    contra = score_answer(
        count, {"text": "13e99999999999999999999", "value": "12",
                "cited_locators": ["loc-1"]},
        authorized_locators=["loc-1"])
    assert contra.verdict == "FAIL" and contra.dimension == "consistency"
    assert contra.critical is True


# B3: a parsed exponent that overflows the rate subtraction must fail
# closed with a ScoreResult, never raise --------------------------------------

def test_r4_rate_overflow_fails_closed_with_a_verdict():
    oracle = _oracle_v2()
    rate = oracle.question("q-rate")
    # "1e1000000" parses as a Decimal, but the rate consistency
    # subtraction overflows the default decimal context; the old code
    # let decimal.Overflow escape score_answer with no ScoreResult.
    result = score_answer(
        rate, {"text": "1e1000000", "value": 50.0,
               "cited_locators": ["loc-1"]},
        authorized_locators=["loc-1"])
    assert result.verdict == "FAIL" and result.dimension == "consistency"
    assert result.critical is True


# Author fixup (iron-rule self-check finding, NOT one of the three Q2
# blockers): a structured count string beyond Python's int-conversion
# limit (4300 digits) made _parse_count raise ValueError out of
# score_answer with no ScoreResult. The fix returns None for such a
# string so the exact branch fails closed with a verdict. On the old
# HEAD this test fails because the ValueError escapes.
def test_r4_oversized_digit_string_fails_closed():
    oracle = _oracle_v2()
    count = oracle.question("q-count")
    result = score_answer(
        count, {"text": "12", "value": "9" * 5000,
                "cited_locators": ["loc-1"]},
        authorized_locators=["loc-1"])
    assert result.verdict == "FAIL" and result.dimension == "value"
    assert result.critical is False


# R5 (new Q2 task, execution 2 repair): rate consistency must reject a
# non-finite verbatim number independently of caller trap settings -----

def test_r5_rate_nan_token_fails_with_invalid_operation_trap_off():
    import decimal
    from decimal import InvalidOperation
    oracle = _oracle_v2()
    rate = oracle.question("q-rate")
    # With the InvalidOperation trap disabled, Decimal on the huge
    # exponent returns NaN instead of raising; the old code compared
    # abs(NaN - 50.0) > tolerance, which is False, so the contradictory
    # rate answer PASSED. The fix rejects non-finite tokens in
    # _verbatim_numbers, so the check fails closed as undecidable.
    ctx = decimal.getcontext()
    old_trap = ctx.traps[InvalidOperation]
    ctx.traps[InvalidOperation] = False
    try:
        result = score_answer(
            rate, {"text": "13e99999999999999999999", "value": 50.0,
                   "cited_locators": ["loc-1"]},
            authorized_locators=["loc-1"])
    finally:
        ctx.traps[InvalidOperation] = old_trap
    assert result.verdict == "FAIL" and result.dimension == "consistency"
    assert result.critical is True


# R5: the rate consistency branch must not let a Decimal signal escape --

def test_r5_rate_inexact_trap_returns_a_verdict_not_an_exception():
    import decimal
    from decimal import Inexact
    oracle = _oracle_v2()
    rate = oracle.question("q-rate")
    # With the Inexact trap enabled, the rate consistency subtraction
    # (1e-100 minus 50.0) raises decimal.Inexact; the old handler caught
    # only (InvalidOperation, Overflow), so the signal escaped
    # score_answer with no ScoreResult. The fix catches
    # decimal.DecimalException, so the check fails closed with a verdict.
    ctx = decimal.getcontext()
    old_trap = ctx.traps[Inexact]
    ctx.traps[Inexact] = True
    try:
        result = score_answer(
            rate, {"text": "1e-100", "value": 50.0,
                   "cited_locators": ["loc-1"]},
            authorized_locators=["loc-1"])
    finally:
        ctx.traps[Inexact] = old_trap
    assert result.verdict == "FAIL" and result.dimension == "consistency"
    assert result.critical is True


# R6 (new Q2 task, execution 3 repair): rate consistency must be exact,
# independent of the caller's ambient Decimal precision -----------------

def test_r6_rate_precision_2_cannot_round_away_contradiction():
    import decimal
    oracle = _oracle_v2()
    rate = oracle.question("q-rate")
    # Verbatim 50.0501 vs structured 50.0, tolerance 0.05: the true
    # difference is 0.0501 > 0.05, so the check must FAIL. At precision
    # 2 the old Decimal subtraction rounded 0.0501 to 0.050, the >
    # comparison was False, and the matching structured value PASSED.
    # The fix compares exact rationals, so ambient precision cannot
    # round a real contradiction away.
    ctx = decimal.getcontext()
    old_prec = ctx.prec
    ctx.prec = 2
    try:
        result = score_answer(
            rate, {"text": "50.0501", "value": 50.0, "unit": "percent",
                   "cited_locators": ["loc-1"]},
            authorized_locators=["loc-1"])
    finally:
        ctx.prec = old_prec
    assert result.verdict == "FAIL" and result.dimension == "consistency"
    assert result.critical is True


# R6: a fixed-precision band-aid does not close the same hole -----------

def test_r6_rate_long_tail_cannot_round_away_contradiction():
    import decimal
    oracle = _oracle_v2()
    rate = oracle.question("q-rate")
    # A 60-digit tail ("50.05" + 55 zeros + "1") vs structured 50.0,
    # tolerance 0.05: the true difference exceeds the tolerance by one
    # unit in the 60th decimal place, so the check must FAIL. At
    # precision 50 the old subtraction rounded it to exactly 0.05 and
    # the answer PASSED -- any fixed precision is beaten by a longer
    # tail. The fix compares exact rationals, so no tail length matters.
    ctx = decimal.getcontext()
    old_prec = ctx.prec
    ctx.prec = 50
    try:
        result = score_answer(
            rate, {"text": "50.05" + "0" * 55 + "1", "value": 50.0,
                   "unit": "percent", "cited_locators": ["loc-1"]},
            authorized_locators=["loc-1"])
    finally:
        ctx.prec = old_prec
    assert result.verdict == "FAIL" and result.dimension == "consistency"
    assert result.critical is True

# R7: regression GUARDS for the Fraction resource-exhaustion defect ----
# These are GUARDS, not falsification tests: on the pre-regression HEAD
# a3aca6ee the same tokens already FAIL closed promptly (the old Decimal
# subtraction raised Overflow, caught by the handler), so a guard passes
# there too by design. On the regressed HEAD 4ef799eb the calls below
# HANG (no verdict within 20 s under a 256 MiB cap) -- that hang is the
# defect these guards pin down. They must return FAIL promptly on the
# repaired HEAD.

def test_r7_rate_huge_positive_exponent_fails_closed_promptly():
    import time
    oracle = _oracle_v2()
    rate = oracle.question("q-rate")
    # Verbatim 1e999999999999999999 vs structured 50.0, tolerance 0.05:
    # Decimal constructs it finite (adjusted = 999999999999999999), so
    # the finite check passes it through, and the unbounded Fraction
    # conversion then tries to build a 10**18-digit integer. The v7
    # bound rejects the operand before any allocation: FAIL, promptly.
    start = time.monotonic()
    result = score_answer(
        rate, {"text": "1e999999999999999999", "value": 50.0, "unit": "percent",
               "cited_locators": ["loc-1"]},
        authorized_locators=["loc-1"])
    elapsed = time.monotonic() - start
    assert result.verdict == "FAIL" and result.dimension == "consistency"
    assert result.critical is True
    assert elapsed < 10, f"huge-exponent rate check took {elapsed:.1f}s"


def test_r7_rate_huge_negative_exponent_fails_closed_promptly():
    import time
    oracle = _oracle_v2()
    rate = oracle.question("q-rate")
    # Same shape, negative exponent: the denominator would expand to
    # 10**18 digits. The v7 bound rejects it before conversion.
    start = time.monotonic()
    result = score_answer(
        rate, {"text": "1e-999999999999999999", "value": 50.0, "unit": "percent",
               "cited_locators": ["loc-1"]},
        authorized_locators=["loc-1"])
    elapsed = time.monotonic() - start
    assert result.verdict == "FAIL" and result.dimension == "consistency"
    assert result.critical is True
    assert elapsed < 10, f"huge-negative-exponent rate check took {elapsed:.1f}s"
