"""REQ-214-1 acceptance suite: LACD governance baseline 1.0.2 -> 1.0.4.

Frozen in Commit 1; must FAIL on baseline 7f99c6b (1.0.4 assertions violated)
and PASS after Commit 2. Commit 2 must not modify this file by one byte.
Deterministic file / JSON / hash assertions only; no external network.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

WORKFLOW_SHA = "7eea80607c0a91eff1416253127db0801e1cbf5e944f6090f00e44896cab3c2b"
MANAGED_SECTION_SHA = (
    "f02da4680881afc425e5c67f33ae7bf085e38a3b66b174f3f3bb09b6cb3ea0e0"
)
LACD_COMMIT = "5aaf063c451b17ca01657d06c8c2eddf8cf68ff7"
LACD_YAML_SHA = "830587bef042655c847c6ec4e740cb04f4dfffd2097ea26e111d3a96e21a68f2"
PROJECT_RULES_SHA = (
    "3546ae7753cf0ecbc28f793e0532f9ba0eee673b7d63927d5186142419b57f1e"
)
PREPARE_PRODUCTION_SHA = (
    "0ccb79429fb4ec5e270a3154af987475ae75bd4ef81de48c1e665a3b93cf4573"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _managed_inner(text: str) -> str:
    """Bytes exactly between the BEGIN and END markers.

    LACD-faithful extraction (migrate.py _extract_section): the section is
    text[begin.end():end.start()] with no added or stripped newline. The
    1.0.4 upgrade writes the BEGIN marker directly followed by the template
    bytes, so the section hash is the template-file hash itself.
    """
    begin = re.search(
        r'<!--\s*LACD:MANAGED:BEGIN\b[^>]*?section="lacd-agent-protocol"'
        r'[^>]*?version="1.0.4"[^>]*?-->',
        text,
    )
    assert begin is not None, "AGENTS.md managed BEGIN marker v1.0.4 not found"
    end = re.search(r"<!--\s*LACD:MANAGED:END\s*-->", text[begin.end():])
    assert end is not None, "AGENTS.md managed END marker not found"
    return text[begin.end():begin.end() + end.start()]


def test_ac1_workflow_is_lacd_104_template():
    p = ROOT / ".github/workflows/lacd-verify.yml"
    assert p.is_file()
    assert _sha256(p) == WORKFLOW_SHA
    text = p.read_text(encoding="utf-8")
    assert "git+https://github.com/SkyOneBranch/LACD.git@v1.0.4" in text
    assert "SkyCjq/LACD.git@v1.0.2" not in text
    assert "SkyCjq/LACD" not in text


def test_ac2_agents_managed_block_is_lacd_104():
    p = ROOT / "AGENTS.md"
    text = p.read_text(encoding="utf-8")
    assert 'version="1.0.4"' in text
    assert 'version="1.0.2"' not in text
    inner = _managed_inner(text)
    assert hashlib.sha256(inner.encode("utf-8")).hexdigest() == (
        MANAGED_SECTION_SHA
    )


def test_ac2_agents_project_lines_synced_to_104():
    lines = (ROOT / "AGENTS.md").read_text(encoding="utf-8").splitlines()
    assert "LACD v1.0.4" in lines[0]
    assert "LACD v1.0.4" in lines[2]
    assert "LACD v1.0.4" in lines[25]
    assert "LACD v1.0.4" in lines[46]
    assert "LACD v1.0.2" not in (ROOT / "AGENTS.md").read_text(
        encoding="utf-8"
    )


def test_ac3_lock_json_versions_and_hashes():
    lock = json.loads((ROOT / ".lacd/lock.json").read_text(encoding="utf-8"))
    assert lock["lacd_cli_version"] == "1.0.4"
    assert lock["template_version"] == "1.0.4"
    assert lock["generated_by"] == "lacd 1.0.4"
    managed = {e["path"]: e for e in lock["managed_files"]}
    entry = managed[".github/workflows/lacd-verify.yml"]
    assert entry["template_version"] == "1.0.4"
    assert entry["sha256"] == WORKFLOW_SHA
    shared = {e["path"]: e for e in lock["shared_files"]}
    shared_entry = shared["AGENTS.md"]
    assert shared_entry["template_version"] == "1.0.4"
    assert shared_entry["sha256"] == MANAGED_SECTION_SHA


def test_ac3_lock_source_is_strong_git_tag_binding():
    lock = json.loads((ROOT / ".lacd/lock.json").read_text(encoding="utf-8"))
    assert lock["source"] == {
        "type": "git-tag",
        "repo": "SkyOneBranch/LACD",
        "ref": "v1.0.4",
        "commit": LACD_COMMIT,
    }


def test_ac4_no_backup_and_configs_unchanged():
    assert not (ROOT / ".lacd/backup").exists()
    assert _sha256(ROOT / "lacd.yaml") == LACD_YAML_SHA
    assert _sha256(ROOT / ".lacd/project-rules.yaml") == PROJECT_RULES_SHA
    assert _sha256(ROOT / "scripts/prepare_production.py") == (
        PREPARE_PRODUCTION_SHA
    )
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == "2.0.2"
