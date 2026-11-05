"""REQ-212 preparatory: hardened morphology scanner + fail-closed gates.

All Drive-ID-like values here are SYNTHETIC, built at runtime by
concatenation, so no literal morphology token appears in this file's bytes.
A morphology finding proves shape detection only -- never that a value is a
real private ID -- which is why disposition (AC-B3) stays a separate step.
"""

import hashlib
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

from cba_kb.current_state import security

ROOT = Path(__file__).resolve().parents[1]


def synth_token():
    return "1" + "A" * 32


def synth_url():
    return "https://drive.google.com/file/d/" + synth_token() + "/view"


def synth_field():
    return '{"fileId": "' + synth_token() + '"}'


def test_url_field_and_standalone_forms_detected():
    scanner = security()
    assert scanner.scan_morphology_bytes(synth_url().encode(), "a.txt")
    assert scanner.scan_morphology_bytes(synth_field().encode(), "b.json")
    assert scanner.scan_morphology_bytes(("see " + synth_token() + " here").encode(), "c.md")


def test_findings_carry_fingerprints_never_token_content():
    scanner = security()
    token = synth_token()
    findings = scanner.scan_morphology_bytes(synth_url().encode(), "a.txt")
    assert findings
    assert token not in json.dumps(findings)
    assert findings[0]["rule_id"] == "S_DRIVE_ID_MORPHOLOGY"
    assert findings[0]["fingerprint"] == "sha256:" + hashlib.sha256(token.encode()).hexdigest()[:12]


def test_false_positive_controls():
    scanner = security()
    assert scanner.scan_morphology_bytes(("a" * 40).encode(), "sha1.txt") == []
    assert scanner.scan_morphology_bytes(("b" * 64).encode(), "sha256.txt") == []
    assert scanner.scan_morphology_bytes(("1" * 30).encode(), "digits.txt") == []
    assert scanner.scan_morphology_bytes(b"ordinary prose, no identifiers", "ok.md") == []


def test_secret_scan_default_contract_unchanged():
    scanner = security()
    assert scanner.scan_bytes(b"ordinary prose", "ok.txt") == []
    assert scanner.scan_bytes(b"hello", "ok.txt", morphology=False) == []


def test_morphology_inside_archives_and_json_normalization():
    scanner = security()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("inner.txt", "token " + synth_token())
    findings = scanner.scan_morphology_bytes(buf.getvalue(), "arch.zip")
    assert findings and findings[0]["path"] == "arch.zip!inner.txt"
    assert synth_token() not in json.dumps(findings)


def test_non_bytes_input_fails_closed():
    scanner = security()
    try:
        scanner.scan_morphology_bytes("not bytes", "x.txt")
    except TypeError:
        pass
    else:
        raise AssertionError("non-bytes input must raise TypeError")


def test_missing_artifact_and_digest_cli_fail_closed(tmp_path):
    target = tmp_path / "absent.bin"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_secrets.py"), "--morphology", str(target)],
        text=True, capture_output=True)
    assert result.returncode == 2
    assert "S_MISSING_ARTIFACT" in result.stderr
    good = tmp_path / "good.bin"
    good.write_bytes(b"clean")
    digest = hashlib.sha256(b"clean").hexdigest()
    ok = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_secrets.py"),
         "--digest", str(good), digest], text=True, capture_output=True)
    assert ok.returncode == 0
    bad = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_secrets.py"),
         "--digest", str(good), "0" * 64], text=True, capture_output=True)
    assert bad.returncode == 2 and "DIGEST_MISMATCH" in bad.stderr
    malformed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_secrets.py"),
         "--digest", str(good), "not-a-digest"], text=True, capture_output=True)
    assert malformed.returncode == 2 and "MALFORMED_DIGEST" in malformed.stderr
    missing = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_secrets.py"),
         "--digest", str(target), digest], text=True, capture_output=True)
    assert missing.returncode == 2 and "MISSING_ARTIFACT" in missing.stderr


def _git(tmp_path, *args, check=True):
    return subprocess.run(["git", *args], cwd=tmp_path, check=check,
                          text=True, capture_output=True)


def test_reintroduced_historical_file_fails_closed(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "config", "user.email", "t@t")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "config", "user.name", "t")
    historical = tmp_path / "hist.txt"
    historical.write_text("old notes " + synth_token() + "\n")
    _git(tmp_path, "add", "hist.txt")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "history")
    reintroduced = tmp_path / "copy.txt"
    reintroduced.write_bytes(historical.read_bytes())
    _git(tmp_path, "add", "copy.txt")
    scanner = security()
    findings = scanner.reintroduced_findings(str(tmp_path))
    assert [f for f in findings if f["rule_id"] == "S_REINTRODUCED_HISTORICAL"]
    assert synth_token() not in json.dumps(findings)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_secrets.py"), "--morphology", "--reintroduction"],
        cwd=tmp_path, text=True, capture_output=True)
    assert result.returncode != 0
    assert "S_REINTRODUCED_HISTORICAL" in result.stderr


def test_staged_morphology_gate_and_unreadable_history(tmp_path):
    _git(tmp_path, "init", "-q")
    staged = tmp_path / "new.md"
    staged.write_text("draft " + synth_token() + "\n")
    _git(tmp_path, "add", "new.md")
    scanner = security()
    assert scanner.scan_git(str(tmp_path), morphology=True)
    try:
        scanner.reintroduced_findings(str(tmp_path))
    except RuntimeError as exc:
        assert "HISTORY_UNAVAILABLE" in str(exc)
    else:
        raise AssertionError("history lookup without commits must fail closed")
