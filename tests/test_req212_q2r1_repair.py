"""REQ-212 Q2 Round 1 repair tests (PR #236 follow-up).

Each test FAILS on the Q2-FAIL HEAD and PASSES on the repaired HEAD.
All Drive-ID-like values are SYNTHETIC, built at runtime by concatenation,
so no literal morphology token appears in this file's bytes. Nothing here
selects a real person, records a real-client run, or claims a real result.
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
    OracleQuestion,
    OracleSource,
    adjudicate_repeat,
    build_oracle,
    classify_answer,
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


def test_alias_map_list_mutation_cannot_change_scoring():
    alias_map = [["UID-ALIAS", "UID-CANON"]]
    oracle = build_oracle(
        version="v",
        sources=[OracleSource("s", "p", "r")],
        questions=[OracleQuestion("q-uid", "uid?", "uid", expected="UID-CANON",
                                  alias_map=alias_map)],
    )
    digest = oracle.artifact_digest
    assert score_answer(oracle.question("q-uid"), "UID-ALIAS").verdict == "PASS"
    alias_map.append(["UID-OTHER", "UID-CANON"])
    alias_map[0][1] = "UID-CHANGED"
    assert score_answer(oracle.question("q-uid"), "UID-ALIAS").verdict == "PASS"
    assert score_answer(oracle.question("q-uid"), "UID-OTHER").verdict == "FAIL"
    assert oracle.artifact_digest == digest
    assert oracle.to_manifest()["questions"][0]["alias_map"] == [["UID-ALIAS", "UID-CANON"]]


def test_unknown_prefix_factual_assertion_is_value():
    assert classify_answer("UNKNOWN: the answer is 42") == "VALUE"
    unk = OracleQuestion("q-unk", "unk?", "unknown")
    result = score_answer(unk, "UNKNOWN: the answer is 42")
    assert result.verdict == "FAIL" and result.critical is True
    assert classify_answer("UNKNOWN") == "UNKNOWN"
    assert classify_answer("UNKNOWN: NOT KNOWN") == "UNKNOWN"
    assert classify_answer("UNKNOWN:") == "UNKNOWN"
    assert score_answer(unk, "UNKNOWN").verdict == "PASS"


def test_mandatory_locator_enforced_for_non_locator_questions():
    oracle = _oracle_v2()
    count = oracle.question("q-count")
    bad = score_answer(count, {"text": "12", "value": "12", "cited_locators": ["loc-9"]},
                       authorized_locators=["loc-1"])
    assert bad.verdict == "FAIL" and bad.dimension == "locator" and bad.critical is True
    missing = score_answer(count, {"text": "12", "value": "12", "cited_locators": []},
                           authorized_locators=["loc-1"])
    assert missing.verdict == "FAIL" and missing.dimension == "locator"
    good = score_answer(count, {"text": "12", "value": "12", "cited_locators": ["loc-1"]},
                        authorized_locators=["loc-1"])
    assert good.verdict == "PASS"


def test_receipt_snapshot_and_domain_binding():
    oracle = _oracle_v2()
    receipt = _receipt(oracle)
    assert validate_receipt(receipt, oracle=oracle, authorized_locators=["loc-1"],
                            expected_snapshot_digest="b" * 64,
                            expected_authorization_domain="synthetic-domain") == []
    changed_snapshot = _receipt(oracle, snapshot_digest="c" * 64)
    assert validate_receipt(changed_snapshot, oracle=oracle, authorized_locators=["loc-1"],
                            expected_snapshot_digest="b" * 64) != []
    changed_domain = _receipt(oracle, authorization_domain="other-domain")
    assert validate_receipt(changed_domain, oracle=oracle, authorized_locators=["loc-1"],
                            expected_authorization_domain="synthetic-domain") != []
    # Without expected values the old contract still holds (format only).
    assert validate_receipt(receipt, oracle=oracle, authorized_locators=["loc-1"]) == []


def test_rejected_locator_reported_by_fingerprint_only():
    oracle = _oracle_v2()
    receipt = _receipt(oracle, cited_locators=("loc-9",))
    errors = validate_receipt(receipt, oracle=oracle, authorized_locators=["loc-1"])
    assert errors
    assert "loc-9" not in json.dumps(errors)
    assert hashlib.sha256(b"loc-9").hexdigest()[:12] in json.dumps(errors)


def test_repeat_floor_cannot_be_lowered():
    oracle = _oracle_v2()
    single = [score_answer(oracle.question("q-count"),
                           {"text": "12", "value": "12", "cited_locators": ["loc-1"]},
                           authorized_locators=["loc-1"])]
    assert adjudicate_repeat(single, minimum_runs=1) == ("INSUFFICIENT_RUNS", None)
    agreed = single * 3
    assert adjudicate_repeat(agreed) == ("AGREED", "PASS")


def test_morphology_finding_paths_never_echo_identifiers():
    token = synth_token("B")
    findings = SCANNER.scan_morphology_bytes(("see " + synth_token() + " here").encode(), token)
    assert findings
    assert token not in json.dumps(findings)
    assert findings[0]["path"].startswith("[path-sha256:")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(token + ".txt", "see " + synth_token() + " here")
    zip_findings = SCANNER.scan_morphology_bytes(buf.getvalue(), "arch.zip")
    assert zip_findings
    assert token not in json.dumps(zip_findings)


def test_unreadable_archive_fails_morphology_guard():
    try:
        SCANNER.require_morphology_clean(b"PK" + b"\x00" * 30, "arch.zip")
    except RuntimeError as exc:
        assert "MORPHOLOGY_GUARD" in str(exc)
    else:
        raise AssertionError("unreadable archive must fail the morphology guard")


def test_digest_mode_rejects_combined_scan_flags():
    good = Path(tempfile.mkdtemp()) / "good.bin"
    good.write_bytes(b"clean")
    digest = hashlib.sha256(b"clean").hexdigest()
    combined = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_secrets.py"), "--digest", str(good), digest,
         "--morphology", str(good.parent / "absent.bin")],
        text=True, capture_output=True)
    assert combined.returncode == 2
    assert "DIGEST_MODE_EXCLUSIVE" in combined.stderr
    pure = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_secrets.py"), "--digest", str(good), digest],
        text=True, capture_output=True)
    assert pure.returncode == 0


def _git(tmp_path, *args):
    return subprocess.run(["git", *args], cwd=tmp_path, check=True,
                          text=True, capture_output=True)


def test_shallow_history_fails_closed():
    base = Path(tempfile.mkdtemp())
    src = base / "src"
    _git(base, "init", "-q", "src")
    _git(src, "-c", "user.email=t@t", "-c", "user.name=t", "config", "user.email", "t@t")
    _git(src, "-c", "user.email=t@t", "-c", "user.name=t", "config", "user.name", "t")
    (src / "hist.txt").write_text("old notes " + synth_token() + "\n")
    _git(src, "add", "hist.txt")
    _git(src, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "history")
    (src / "hist.txt").unlink()
    (src / "other.txt").write_text("other\n")
    _git(src, "add", "-A")
    _git(src, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "remove")
    subprocess.run(["git", "clone", "-q", "--depth", "1", "file://" + str(src),
                    str(base / "shallow")], check=True, capture_output=True)
    shallow = base / "shallow"
    assert subprocess.run(["git", "-C", str(shallow), "rev-parse",
                           "--is-shallow-repository"], text=True,
                          capture_output=True).stdout.strip() == "true"
    (shallow / "copy.txt").write_text("old notes " + synth_token() + "\n")
    _git(shallow, "add", "copy.txt")
    try:
        SCANNER.reintroduced_findings(str(shallow))
    except RuntimeError as exc:
        assert "HISTORY_UNAVAILABLE" in str(exc)
    else:
        raise AssertionError("shallow history must fail closed, not report no reintroduction")


def test_missing_object_in_batch_check_fails_closed():
    module = SCANNER  # the loaded scripts/check_secrets.py module object

    class FakeProc:
        def __init__(self, stdout):
            self.stdout = stdout
            self.returncode = 0
            self.stderr = ""

    class FakeSubprocess:
        # historical_blob_ids references subprocess.CalledProcessError in
        # except clauses and subprocess.STDOUT as a kwarg, so the fake must
        # expose both.
        CalledProcessError = subprocess.CalledProcessError
        STDOUT = subprocess.STDOUT

        def __init__(self, batch_stdout):
            self.batch_stdout = batch_stdout

        def check_output(self, cmd, **kwargs):
            if cmd[1] == "rev-parse":
                return b"false\n"
            if cmd[1] == "rev-list":
                return b"deadbeef\n"
            raise AssertionError("unexpected command: %r" % (cmd,))

        def run(self, cmd, **kwargs):
            return FakeProc(self.batch_stdout)

    real_subprocess = module.subprocess
    try:
        for batch_stdout in ("deadbeef missing\n", "garbage\n"):
            module.subprocess = FakeSubprocess(batch_stdout)
            try:
                module.historical_blob_ids(".")
            except RuntimeError as exc:
                assert "HISTORY_UNAVAILABLE" in str(exc)
            else:
                raise AssertionError("unexpected batch-check line must fail closed: %r"
                                     % batch_stdout)
    finally:
        module.subprocess = real_subprocess


def test_cited_locators_frozen_on_receipt():
    oracle = _oracle_v2()
    cited = ["loc-1"]
    receipt = _receipt(oracle, cited_locators=cited)
    assert validate_receipt(receipt, oracle=oracle, authorized_locators=["loc-1"]) == []
    cited.append("loc-9")
    assert validate_receipt(receipt, oracle=oracle, authorized_locators=["loc-1"]) == []
    assert receipt.cited_locators == ("loc-1",)


def test_oracle_sequences_frozen_at_build():
    # Direct construction (not build_oracle, which already tuples): caller
    # lists must not stay mutable, or the manifest would drift from its digest.
    sources = [OracleSource("s", "p", "r")]
    questions = [OracleQuestion("q", "?", "unknown")]
    oracle = Oracle(version="v", sources=sources, questions=questions)
    manifest_before = oracle.to_manifest()
    sources.append(OracleSource("s2", "p2", "r2"))
    questions.append(OracleQuestion("q2", "?", "unknown"))
    assert oracle.to_manifest() == manifest_before
    assert oracle.artifact_digest == manifest_before["artifact_digest"]


def test_bom_prefixed_utf8_bytes_do_not_escape_morphology():
    url = synth_url()
    mangled = b"\xff\xfe" + url.encode("utf-8")
    assert SCANNER.scan_morphology_bytes(mangled, "u.txt")
    proper = b"\xff\xfe" + url.encode("utf-16-le")
    assert SCANNER.scan_morphology_bytes(proper, "u.txt")
