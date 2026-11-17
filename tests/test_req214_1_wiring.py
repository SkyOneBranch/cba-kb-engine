"""REQ-214-1 wiring test: real call chains for the LACD 1.0.4 upgrade.

- tiering.py classifies every changed path of this upgrade as T2 and the
  whole change set as T2 (FINAL_TIER = T2).
- gate_selftest.py runs end-to-end and passes (exit 0).
- When a real `lacd` CLI is on PATH, `lacd verify --local --strict` must
  exit 0 and `lacd status --json` must report strong/match/VALIDATED/clean.
  When no CLI is present (offline CI), the lock/template contract is
  verified structurally instead, so the suite stays green without network.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHANGED_PATHS = [
    ".github/workflows/lacd-verify.yml",
    "AGENTS.md",
    ".lacd/lock.json",
]


def _load_tiering():
    spec = importlib.util.spec_from_file_location(
        "tiering", ROOT / ".github/scripts/tiering.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_wiring_changed_paths_are_t2():
    tiering = _load_tiering()
    tiers = tiering.get_tiers()
    for path in CHANGED_PATHS:
        assert tiering.file_tier(path, tiers) == "T2", path
    assert tiering.compute_tier(CHANGED_PATHS, tiers) == "T2"


def test_wiring_gate_selftest_passes():
    result = subprocess.run(
        [sys.executable, ".github/scripts/gate_selftest.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "ALL NEGATIVE TESTS PASS" in result.stdout


def test_wiring_lacd_cli_or_lock_contract():
    lacd = shutil.which("lacd")
    if lacd is not None:
        verify = subprocess.run(
            [lacd, "verify", "--local", "--strict"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert verify.returncode == 0, (
            verify.stdout[-2000:] + verify.stderr[-2000:]
        )
        status = subprocess.run(
            [lacd, "status", "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert status.returncode == 0
        data = json.loads(status.stdout)
        assert data["source_pin"]["strength"] == "strong"
        assert data["version_consistency"]["status"] == "consistent"
        assert data["state"] == "VALIDATED"
        assert data["dimensions"]["drift"]["status"] == "clean"
        return
    # No lacd CLI in this environment (offline CI): verify the contract
    # structurally from the files the CLI itself would read.
    lock = json.loads((ROOT / ".lacd/lock.json").read_text(encoding="utf-8"))
    assert lock["lacd_cli_version"] == "1.0.4"
    assert lock["template_version"] == "1.0.4"
    assert lock["source"] == {
        "type": "git-tag",
        "repo": "SkyOneBranch/LACD",
        "ref": "v1.0.4",
        "commit": "5aaf063c451b17ca01657d06c8c2eddf8cf68ff7",
    }
    assert not (ROOT / ".lacd/backup").exists()
