import hashlib
from pathlib import Path
import subprocess

import pytest

from automation.hardening import (
    RELEASE_IDENTITY_NOT_READY,
    HardeningFailure,
    validate_release_identity,
)
from cba_kb.orchestration.release import prepare_v215
from scripts import prepare_production as base_orchestration
from test_release_orchestration import (
    ClosureDrive,
    ClosureInstance,
    _closure_entries,
    projection_inputs,
)


V215_BASELINE_SHA = "3874ebb4b20fdfd5c1d1fd7f9ed9426d9f26036f"
FROZEN_PREPARE_PRODUCTION_SHA256 = (
    "0ccb79429fb4ec5e270a3154af987475ae75bd4ef81de48c1e665a3b93cf4573"
)


def test_v215_1_release_spec_and_frozen_base_script():
    expected = {
        "product_baseline_sha": V215_BASELINE_SHA,
        "enable_consumer_surface_v215": True,
    }
    assert prepare_v215.V215_RELEASE_SPECS["v2.1.5-1"] == expected
    assert prepare_v215._require_release("v2.1.5-1") == expected
    assert prepare_v215._release_spec("v2.1.5-1") == expected

    for forbidden in ["v2.1.5", "v2.1.5-2", "v9.9.9"]:
        with pytest.raises(
            prepare_v215.ProjectionError,
            match=f"RELEASE_ID_FORBIDDEN:{forbidden}",
        ):
            prepare_v215._require_release(forbidden)

    for base_forbidden in ["v2.1.5", "v2.1.5-1"]:
        with pytest.raises(
            base_orchestration.ProjectionError,
            match=f"RELEASE_ID_FORBIDDEN:{base_forbidden}",
        ):
            base_orchestration._require_release(base_forbidden)

    repo = Path(__file__).resolve().parents[1]
    actual_sha = hashlib.sha256(
        (repo / "scripts" / "prepare_production.py").read_bytes()
    ).hexdigest()
    assert actual_sha == FROZEN_PREPARE_PRODUCTION_SHA256


def test_v215_1_release_identity_validation():
    result = validate_release_identity({
        "target_product_version": "v2.1.5",
        "proposed_release_attempt": "v2.1.5-1",
        "product_candidate_sha": V215_BASELINE_SHA,
        "release_execution_role": "RELEASE_EXECUTOR",
    })
    assert result["status"] == "PASS"
    assert result["facts"]["target_product_version"] == "v2.1.5"
    assert result["facts"]["release_attempt"] == "v2.1.5-1"
    assert result["facts"]["canonical_product_baseline_sha"] == V215_BASELINE_SHA

    with pytest.raises(HardeningFailure) as exc_info:
        validate_release_identity({
            "target_product_version": "v2.1.5",
            "proposed_release_attempt": "v2.1.5-2",
            "product_candidate_sha": V215_BASELINE_SHA,
            "release_execution_role": "RELEASE_EXECUTOR",
        })
    assert exc_info.value.classification == RELEASE_IDENTITY_NOT_READY


def test_v215_1_projection_and_baseline_ancestry():
    inputs = projection_inputs()
    inputs["release_id"] = "v2.1.5-1"
    inputs["engine_sha"] = V215_BASELINE_SHA
    projected = prepare_v215.project_targets(**inputs)
    assert projected["release_id"] == "v2.1.5-1"
    assert projected["engine_sha"] == V215_BASELINE_SHA
    assert projected["state"] == "PROJECTED"

    repo = Path(__file__).resolve().parents[1]
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True,
    ).strip()
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", V215_BASELINE_SHA, head],
        cwd=repo,
        check=False,
    ).returncode == 0


def test_v215_1_closure_contract_binds_technical_manual():
    spec = prepare_v215._require_release("v2.1.5-1")
    assert spec["enable_consumer_surface_v215"] is True
    assert prepare_v215._uses_closure("v2.1.5-1") is True
    assert prepare_v215._uses_technical_manual("v2.1.5-1") is True

    base_entries = _closure_entries()
    state = {
        "status": {
            "state": "COMPLETE",
            "current_release_id": "v2.1.4",
            "code_commit": "b" * 40,
        }
    }
    with pytest.raises(
        prepare_v215.ProjectionError,
        match="CLOSURE_CONTROL_TARGET_MISSING",
    ):
        prepare_v215._build_closure_contract(
            drive=ClosureDrive(),
            instance=ClosureInstance(),
            projection={"release_id": "v2.1.5-1", "engine_sha": V215_BASELINE_SHA},
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
    closure = prepare_v215._build_closure_contract(
        drive=ClosureDrive(),
        instance=ClosureInstance(),
        projection={"release_id": "v2.1.5-1", "engine_sha": V215_BASELINE_SHA},
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
    assert closure["baseline_release_id"] == "v2.1.4"
