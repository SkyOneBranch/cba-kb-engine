from pathlib import Path
import subprocess

import pytest

from scripts import prepare_production as orchestration
from test_release_orchestration import (
    ClosureDrive,
    ClosureInstance,
    _closure_entries,
    projection_inputs,
)


V214_BASELINE_SHA = "ac6ffc9bb31dd5bb69c785c7a73549b7f85b639e"


def test_v214_release_spec_and_require_release():
    expected = {"product_baseline_sha": V214_BASELINE_SHA}
    assert orchestration.RELEASE_SPECS["v2.1.4"] == expected
    assert orchestration._require_release("v2.1.4") == expected
    assert orchestration._release_spec("v2.1.4") == expected

    for forbidden in ["v2.1.4-1", "v2.1.5", "v9.9.9"]:
        with pytest.raises(
            orchestration.ProjectionError,
            match=f"RELEASE_ID_FORBIDDEN:{forbidden}",
        ):
            orchestration._require_release(forbidden)


def test_v214_projection_and_baseline_ancestry():
    inputs = projection_inputs()
    inputs["release_id"] = "v2.1.4"
    inputs["engine_sha"] = V214_BASELINE_SHA
    projected = orchestration.project_targets(**inputs)
    assert projected["release_id"] == "v2.1.4"
    assert projected["engine_sha"] == V214_BASELINE_SHA
    assert projected["state"] == "PROJECTED"

    repo = Path(__file__).resolve().parents[1]
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True,
    ).strip()
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", V214_BASELINE_SHA, head],
        cwd=repo,
        check=False,
    ).returncode == 0


def test_v214_closure_contract_binds_technical_manual():
    base_entries = _closure_entries()
    state = {
        "status": {
            "state": "COMPLETE",
            "current_release_id": "v2.1.1",
            "code_commit": "b" * 40,
        }
    }
    with pytest.raises(
        orchestration.ProjectionError,
        match="CLOSURE_CONTROL_TARGET_MISSING",
    ):
        orchestration._build_closure_contract(
            drive=ClosureDrive(),
            instance=ClosureInstance(),
            projection={"release_id": "v2.1.4", "engine_sha": "a" * 40},
            allocation={"staging_id": "staging"},
            entries=base_entries,
            state=state,
        )

    entries_with_manual = [
        *base_entries,
        {
            "logical_key": "entry/context",
            "id": "id-manual",
            "name": "context.md",
            "mime": "text/plain",
            "mode": "binary",
            "before_hash": "f" * 64,
            "publish_parent": "root",
            "staging_parent": "staging",
        },
    ]
    closure = orchestration._build_closure_contract(
        drive=ClosureDrive(),
        instance=ClosureInstance(),
        projection={"release_id": "v2.1.4", "engine_sha": "a" * 40},
        allocation={"staging_id": "staging"},
        entries=entries_with_manual,
        state=state,
    )
    assert closure["documents"] == {
        "readme": "entry/README",
        "index": "derived/INDEX.md",
        "context_card": "ai/CONTEXT_CARD.md",
        "current_version_doc": "CURRENT_VERSION_DOC",
        "technical_manual": "entry/context",
    }
    assert closure["baseline_release_id"] == "v2.1.1"
