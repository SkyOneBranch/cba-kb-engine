"""REQ-212 Q2 Round 3 remediation tests (PR #236 follow-up).

Each test FAILS on the Q2-FAIL HEAD and PASSES on the remediated HEAD.
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


def _approval(oracle, **overrides):
    from cba_kb.oracle import ScoringApproval, scoring_code_sha
    fields = dict(
        manifest_digest=oracle.artifact_digest,
        version=oracle.version,
        scoring_code_sha=scoring_code_sha(),
        snapshot_digest="b" * 64,
    )
    fields.update(overrides)
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
    from cba_kb.oracle import ScoringApproval, score_answer_approved, scoring_code_sha
    import cba_kb.oracle as oracle_module
    oracle = _oracle_v2()
    assert scoring_code_sha() == hashlib.sha256(
        Path(oracle_module.__file__).read_bytes()).hexdigest()
    approval = ScoringApproval(
        manifest_digest=oracle.artifact_digest, version=oracle.version,
        scoring_code_sha=scoring_code_sha(), snapshot_digest="b" * 64)
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
    assert two_numbers.verdict == "PASS"


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
