"""REQ-215+ release orchestration extension keeping scripts/prepare_production.py frozen.

Delegates shared fail-closed primitives and legacy release specs to
``scripts.prepare_production`` while supporting declared consumer-surface
targets, dynamic MASTER row-count binding, and raw Drive ID redaction in
consumer control documents.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
from pathlib import Path
import re
import subprocess
import sys

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[4]
if (_REPO_ROOT / "scripts" / "prepare_production.py").is_file() and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import prepare_production as _base  # noqa: E402

from cba_kb.canonical_registry import load_registry  # noqa: E402
from cba_kb.common import atomic, digest, read, save  # noqa: E402
from cba_kb.consumer_surface import (  # noqa: E402
    build_consumer_surface_targets_spec,
    build_promoted_research_slice_artifacts,
    compute_master_row_count,
    merge_operational_docs_into_consumer_index,
    render_collector_official_digest_md,
    render_consumer_surface_guide_section,
    scrub_raw_drive_ids_for_consumer,
)
from cba_kb.current_state import (  # noqa: E402
    END as CURRENT_STATE_END,
    current_version_document_migration,
    generate_context_card,
    replace_current_block,
    target_metadata,
)
from cba_kb.drive import Drive  # noqa: E402
from cba_kb.instance import load_instance  # noqa: E402
from cba_kb.placement_facade import placement_enforced  # noqa: E402
from cba_kb.release import (  # noqa: E402
    PreMutationAbort,
    _inventory,
    fingerprint,
    prepare as prepare_release,
    snapshot,
    validate_release_topology,
)

# Re-exported frozen constants & shared validators from scripts.prepare_production
FOLDER = _base.FOLDER
NATIVE_DOCUMENT = _base.NATIVE_DOCUMENT
REGISTRY_KEY = _base.REGISTRY_KEY
MANIFEST_KEY = _base.MANIFEST_KEY
CONTEXT_CARD_KEY = _base.CONTEXT_CARD_KEY
FORBIDDEN_CODE_PREFIXES = _base.FORBIDDEN_CODE_PREFIXES
CONTROL_KEYS = _base.CONTROL_KEYS
ProjectionError = _base.ProjectionError
RELEASE_ID = _base.RELEASE_ID
PRODUCT_BASELINE_SHA = _base.PRODUCT_BASELINE_SHA
RELEASE_SPECS = _base.RELEASE_SPECS

_json_hash = _base._json_hash
_private_output = _base._private_output
verify_execution_sha = _base.verify_execution_sha
_status_rows = _base._status_rows
_manifest_rows = _base._manifest_rows
reproject_targets = _base.reproject_targets
validate_manifest_coverage = _base.validate_manifest_coverage
validate_reservation = _base.validate_reservation
validate_policy_reconciliation = _base.validate_policy_reconciliation
validate_release_state = _base.validate_release_state
_reservation_key = _base._reservation_key
_managed_candidate = _base._managed_candidate
_managed_body = _base._managed_body
load_production_dependencies = _base.load_production_dependencies
_load_manifest = _base._load_manifest
_optional_instance_topology = _base._optional_instance_topology
build_release_topology_from_production = _base.build_release_topology_from_production
build_release_infra_compatibility_bundle = _base.build_release_infra_compatibility_bundle
release_infra_compatibility_preflight = _base.release_infra_compatibility_preflight
run_trusted_release_infra_compatibility_preflight = _base.run_trusted_release_infra_compatibility_preflight
build_live_qualification_fingerprints = _base.build_live_qualification_fingerprints

V215_BASELINE_SHA = "3874ebb4b20fdfd5c1d1fd7f9ed9426d9f26036f"
V220_1_BASELINE_SHA = "fa9793c6db76e15880610ced728a7b0d563da604"

V215_RELEASE_SPECS: dict[str, dict] = {
    "v2.1.5-1": {
        "product_baseline_sha": V215_BASELINE_SHA,
        "enable_consumer_surface_v215": True,
    },
    "v2.2.0-1": {
        "product_baseline_sha": V220_1_BASELINE_SHA,
        "enable_consumer_surface_v215": True,
        "enable_operational_kb_v220": True,
    },
}

_CLOSURE_BASE_RELEASES = frozenset({
    "v1.6.1-1", "v1.8.1-1", "v1.8.1-2", "v1.8.1-3",
    "v1.9.0-1", "v2.0.0-1", "v2.0.1-1", "v2.1.1", "v2.1.4",
})
_TECH_MANUAL_BASE_RELEASES = frozenset({
    "v1.8.1-2", "v1.8.1-3", "v1.9.0-1", "v2.0.0-1",
    "v2.0.1-1", "v2.1.1", "v2.1.4",
})
_MODERN_V215_PLUS_RE = re.compile(
    r"^v(?:2\.(?:1\.(?:[5-9]|\d{2,})|[2-9]\d*(?:\.\d+)*)|(?:[3-9]|\d{2,})\.)"
)


def _release_spec(release_id):
    if release_id in V215_RELEASE_SPECS:
        return V215_RELEASE_SPECS[release_id]
    return _base._release_spec(release_id)


def _require_release(release_id):
    return _release_spec(release_id)


def _uses_closure(release_id):
    return (
        release_id in _CLOSURE_BASE_RELEASES
        or bool(isinstance(release_id, str) and _MODERN_V215_PLUS_RE.match(release_id))
    )


def _uses_technical_manual(release_id):
    return (
        release_id in _TECH_MANUAL_BASE_RELEASES
        or bool(isinstance(release_id, str) and _MODERN_V215_PLUS_RE.match(release_id))
    )


def project_targets(
    *,
    release_id,
    engine_sha,
    status_id,
    archive_id,
    previous_status,
    previous_targets,
    manifest_rows,
    tracked,
    tracked_hashes,
    code_parent,
    status_hash=None,
    status_meta=None,
    allocation=None,
    allowed_removed_code_keys=None,
    declared_consumer_targets=None,
):
    """Project exact target semantics without any remote mutation."""
    _require_release(release_id)
    migration = current_version_document_migration(manifest_rows, release_id)
    manifest_rows = migration["manifest"]
    status_by_id = _status_rows(previous_status, previous_targets)
    logical_by_id = _manifest_rows(manifest_rows)
    if set(status_by_id) - set(logical_by_id):
        raise ProjectionError("MANIFEST_MAPPING_MISSING")
    active_ids = set(status_by_id)
    retirement_markers = {}
    for file_id, policy in previous_targets.items():
        if not isinstance(policy, dict):
            raise ProjectionError("PRIVATE_PRODUCTION_TARGET_INVALID")
        if "retire_in_release" not in policy:
            continue
        marker = policy["retire_in_release"]
        if not isinstance(marker, str) or not marker:
            raise ProjectionError("RETIREMENT_POLICY_INVALID")
        retirement_markers[file_id] = marker
    scheduled_retirement_ids = {
        file_id for file_id, marker in retirement_markers.items()
        if marker == release_id
    }
    if scheduled_retirement_ids - active_ids:
        raise ProjectionError("RETIREMENT_TARGET_NOT_IN_PREVIOUS_STATUS")
    historical_retired_ids = {
        file_id for file_id, marker in retirement_markers.items()
        if file_id not in active_ids and marker != release_id
    }
    reserved_ids = set()
    reserved_keys = {}
    if allocation is not None:
        if allocation.get("release_id") != release_id:
            raise ProjectionError("RESERVATION_RELEASE_MISMATCH")
        if allocation.get("status_id") != status_id:
            raise ProjectionError("RESERVATION_STATUS_MISMATCH")
        reserved_keys = dict(allocation.get("reservations") or {})
        reserved_ids = set(reserved_keys.values())
        if len(reserved_ids) != len(reserved_keys):
            raise ProjectionError("RESERVATION_ID_DUPLICATE")
    policy_ids = set(previous_targets)
    extra_policy_ids = policy_ids - active_ids
    reservation_policy_ids = extra_policy_ids - historical_retired_ids
    if not reservation_policy_ids and reserved_ids:
        raise ProjectionError("RESERVATION_POLICY_TARGETS_MISSING")
    if reservation_policy_ids != reserved_ids:
        raise ProjectionError("UNEXPLAINED_RESERVED_POLICY_TARGET")
    reserved_by_id = {file_id: key for key, file_id in reserved_keys.items()}

    existing = {}
    logical_seen = set()
    for file_id, item in status_by_id.items():
        logical_key = logical_by_id[file_id]
        if logical_key in logical_seen:
            raise ProjectionError("LOGICAL_KEY_DUPLICATE")
        logical_seen.add(logical_key)
        policy = previous_targets[file_id]
        existing[logical_key] = {
            "logical_key": logical_key,
            "id": file_id,
            "name": item["name"],
            "previous_sha256": item["sha256"],
            "mime": policy["mime"],
            "mode": policy.get("mode", "binary"),
            "allowed_parents": list(policy.get("allowed_parents") or []),
            "publish_parent": policy.get("publish_parent"),
            "staging_parent": policy.get("staging_parent"),
            **(
                {"retire_in_release": policy["retire_in_release"]}
                if "retire_in_release" in policy else {}
            ),
        }
    if set(reserved_by_id) & set(existing):
        raise ProjectionError("RESERVATION_REUSES_EXISTING_TARGET")
    logical_by_id.update(reserved_by_id)

    tracked = sorted(set(tracked))
    for relative in tracked:
        if relative.startswith(FORBIDDEN_CODE_PREFIXES):
            raise ProjectionError(f"FORBIDDEN_CODE_MIRROR:{relative}")
        if relative not in tracked_hashes:
            raise ProjectionError(f"TRACKED_HASH_MISSING:{relative}")

    current_code = {
        f"code/{relative}": {
            "relative": relative,
            "sha256": tracked_hashes[relative],
        }
        for relative in tracked
    }
    existing_code_keys = {
        key for key in existing if key.startswith("code/")
    }
    non_code_keys = set(existing) - existing_code_keys
    control_keys = non_code_keys & CONTROL_KEYS
    business_keys = non_code_keys - CONTROL_KEYS
    retiring_keys = {
        logical_by_id[file_id] for file_id in scheduled_retirement_ids
    }
    retiring_code_keys = sorted(
        key for key in retiring_keys if key.startswith("code/")
    )
    if retiring_code_keys:
        raise ProjectionError(
            "RETIREMENT_CODE_TARGET_FORBIDDEN:" + ",".join(retiring_code_keys)
        )
    retiring_control_keys = sorted(retiring_keys & CONTROL_KEYS)
    if retiring_control_keys:
        raise ProjectionError(
            "RETIREMENT_CONTROL_TARGET_FORBIDDEN:"
            + ",".join(retiring_control_keys)
        )

    allowed_set = set()
    if allowed_removed_code_keys is not None:
        if not isinstance(allowed_removed_code_keys, (list, tuple, set)):
            raise ProjectionError("ALLOWED_REMOVED_CODE_KEYS_INVALID")
        for k in allowed_removed_code_keys:
            if not isinstance(k, str) or not k.startswith("code/"):
                raise ProjectionError("ALLOWED_REMOVED_CODE_KEYS_INVALID")
        allowed_set = set(allowed_removed_code_keys)
        if allowed_set - existing_code_keys:
            raise ProjectionError("ALLOWED_REMOVED_CODE_KEYS_UNKNOWN")

    removed_code_keys = sorted(
        key for key in existing_code_keys if key not in current_code
    )
    unauthorized_removed = sorted(set(removed_code_keys) - allowed_set)
    if unauthorized_removed:
        raise ProjectionError(
            "REMOVED_TARGET_POLICY_REQUIRED:" + ",".join(unauthorized_removed)
        )
    carried_removed_keys = sorted(set(removed_code_keys) & allowed_set)

    unchanged = []
    modified = []
    reused = []
    for logical_key in sorted(existing_code_keys & set(current_code)):
        item = existing[logical_key]
        reused.append(logical_key)
        if item["previous_sha256"] == current_code[logical_key]["sha256"]:
            unchanged.append(logical_key)
        else:
            modified.append(logical_key)

    for logical_key in carried_removed_keys:
        reused.append(logical_key)
        unchanged.append(logical_key)

    new_keys = sorted(set(current_code) - existing_code_keys)
    if not code_parent:
        raise ProjectionError("CODE_PARENT_REQUIRED")
    new_targets = [
        {
            "logical_key": logical_key,
            "name": current_code[logical_key]["relative"].replace("/", "__"),
            "mime": "text/plain",
            "mode": "binary",
            "publish_parent": code_parent,
            "sha256": current_code[logical_key]["sha256"],
            "relative": current_code[logical_key]["relative"],
            **(
                {"id": reserved_keys[logical_key]}
                if logical_key in reserved_keys else {}
            ),
        }
        for logical_key in new_keys
    ]
    declared_consumer_keys = set()
    if declared_consumer_targets:
        if not isinstance(declared_consumer_targets, dict):
            raise ProjectionError("DECLARED_CONSUMER_TARGETS_INVALID")
        for ckey in sorted(declared_consumer_targets):
            spec = declared_consumer_targets[ckey]
            if (
                not isinstance(ckey, str)
                or not ckey
                or ckey.startswith("code/")
                or ckey in existing
                or ckey in current_code
            ):
                raise ProjectionError(f"DECLARED_TARGET_CONFLICT:{ckey}")
            if not isinstance(spec, dict) or not spec.get("name") or not spec.get("mime"):
                raise ProjectionError(f"DECLARED_CONSUMER_TARGET_INVALID:{ckey}")
            raw_content = spec.get("content")
            if isinstance(raw_content, (bytes, bytearray)):
                content_sha = digest(bytes(raw_content))
            elif isinstance(spec.get("sha256"), str) and spec["sha256"]:
                content_sha = spec["sha256"]
            else:
                raise ProjectionError(f"DECLARED_CONSUMER_TARGET_HASH_REQUIRED:{ckey}")
            target_parent = spec.get("publish_parent") or code_parent
            declared_consumer_keys.add(ckey)
            new_targets.append({
                "logical_key": ckey,
                "name": str(spec["name"]),
                "mime": str(spec["mime"]),
                "mode": str(spec.get("mode", "binary")),
                "publish_parent": target_parent,
                "sha256": content_sha,
                "consumer_target_kind": "consumer_surface",
                **(
                    {"id": reserved_keys[ckey]}
                    if ckey in reserved_keys else {}
                ),
            })
        new_targets.sort(key=lambda item: item["logical_key"])
    if allocation is not None and (set(new_keys) | declared_consumer_keys) != set(reserved_keys):
        raise ProjectionError("RESERVATION_KEY_SET_MISMATCH")

    existing_targets = []
    for logical_key in sorted(existing):
        item = dict(existing[logical_key])
        if logical_key in current_code:
            item["current_sha256"] = current_code[logical_key]["sha256"]
            item["disposition"] = (
                "modified" if logical_key in modified else "unchanged"
            )
        elif logical_key in control_keys:
            item["disposition"] = "metadata_update"
        elif logical_key in retiring_keys:
            item["disposition"] = "RETIRED_FROM_CURRENT"
        elif logical_key in carried_removed_keys:
            item["disposition"] = "carried_forward"
            item["legacy_carried_forward"] = True
        else:
            item["disposition"] = "carried_forward"
        existing_targets.append(item)

    final_keys = sorted(
        (non_code_keys - retiring_keys)
        | set(current_code)
        | set(carried_removed_keys)
        | declared_consumer_keys
    )
    legacy_reservation_keys = sorted(
        non_code_keys
        | set(current_code)
        | set(carried_removed_keys)
        | declared_consumer_keys
    )
    if len(final_keys) != len(set(final_keys)):
        raise ProjectionError("LOGICAL_KEY_COLLISION")
    retiring_targets = [
        {
            "id": existing[logical_key]["id"],
            "logical_key": logical_key,
            "name": existing[logical_key]["name"],
            "sha256": existing[logical_key]["previous_sha256"],
            "disposition": "RETIRED_FROM_CURRENT",
        }
        for logical_key in sorted(retiring_keys)
    ]
    projection = {
        "schema_version": 1,
        "state": "PROJECTED",
        "release_id": release_id,
        "engine_sha": engine_sha,
        "status_id": status_id,
        "archive_id": archive_id,
        "previous_artifact_count": len(previous_status["artifacts"]),
        "reused_target_count": len(reused) + len(non_code_keys - retiring_keys),
        "new_target_count": len(new_targets),
        "removed_target_count": len(removed_code_keys),
        "retired_target_count": len(retiring_targets),
        "carried_forward_count": len((business_keys - retiring_keys) | set(carried_removed_keys)),
        "carried_forward_code_targets": carried_removed_keys,
        "final_artifact_count": len(final_keys),
        "final_current_artifact_count": len(final_keys),
        "final_current_logical_keys": final_keys,
        "existing_targets": existing_targets,
        "new_targets": new_targets,
        "retiring_targets": retiring_targets,
        "removed_targets": removed_code_keys,
        "modified_content_targets": modified,
        "unchanged_targets": unchanged,
        "carried_forward_artifacts": sorted((business_keys - retiring_keys) | set(carried_removed_keys)),
        "active_target_ids": sorted(active_ids),
        "active_production_target_count": len(active_ids),
        "reserved_policy_target_ids": sorted(reserved_ids),
        "reserved_staging_target_count": len(reserved_ids),
        "historical_retired_policy_target_ids": sorted(historical_retired_ids),
        "historical_retired_policy_markers": {
            file_id: retirement_markers[file_id]
            for file_id in sorted(historical_retired_ids)
        },
        "release_status_unchanged": True,
        "control_target_migrations": (
            [] if migration["report"]["status"] == "NOT_APPLICABLE"
            else [migration["report"]]
        ),
        "status_before_hash": status_hash,
        "status_before_meta": status_meta,
        "active_release_id": previous_status.get("current_release_id"),
    }
    projection["semantic_delta_signature"] = _json_hash({
        "keys": final_keys,
        "new": sorted(set(new_keys) | declared_consumer_keys),
        "modified": modified,
        "removed": removed_code_keys,
        **({"retired": sorted(retiring_keys)} if retiring_keys else {}),
        **(
            {
                "consumer_surface_hashes": {
                    item["logical_key"]: item["sha256"]
                    for item in new_targets
                    if item.get("consumer_target_kind") == "consumer_surface"
                }
            }
            if declared_consumer_keys else {}
        ),
        "counts": {
            "previous": projection["previous_artifact_count"],
            "final": projection["final_artifact_count"],
        },
    })
    projection["reservation_compatibility_signature"] = _json_hash({
        "keys": legacy_reservation_keys,
        "new": sorted(set(new_keys) | declared_consumer_keys),
        "modified": modified,
        "removed": removed_code_keys,
        "counts": {
            "previous": projection["previous_artifact_count"],
            "final": len(legacy_reservation_keys),
        },
    })
    return projection


def reserve_staging(
    drive,
    instance,
    *,
    release_id,
    projection,
    output,
    single_writer,
):
    """Create only projected NEW keys below the release staging folder."""
    _require_release(release_id)
    if not single_writer:
        raise ProjectionError("SINGLE_WRITER_REQUIRED")
    if projection.get("release_id") != release_id:
        raise ProjectionError("RESERVATION_RELEASE_MISMATCH")
    output = Path(output)
    allocation_path = output / "allocation.json"
    if allocation_path.is_file():
        existing = read(allocation_path)
        staging_id = existing.get("staging_id")
        reservations = dict(existing.get("reservations") or {})
    else:
        staging_id = None
        reservations = {}
    if not staging_id:
        staging_id = drive.ensure(
            projection["archive_id"],
            f"{release_id}-staging",
            f"{release_id}_staging",
            FOLDER,
        )
    for item in sorted(
        projection["new_targets"], key=lambda value: value["logical_key"],
    ):
        key = item["logical_key"]
        file_id = drive.ensure(
            staging_id,
            _reservation_key(release_id, key),
            item["name"],
            item["mime"],
            b"",
        )
        meta = drive.meta(file_id)
        if meta.get("mimeType") != item["mime"]:
            raise ProjectionError("RESERVED_TARGET_MIME_MISMATCH")
        if staging_id not in (meta.get("parents") or []):
            raise ProjectionError("RESERVED_TARGET_NOT_IN_STAGING")
        reservations[key] = file_id
        output.mkdir(parents=True, exist_ok=True)
        save(output / "allocation-progress.json", {
            "release_id": release_id,
            "staging_id": staging_id,
            "reservations": reservations,
        })
    allocation = {
        "schema_version": 1,
        "state": "RESERVED",
        "release_id": release_id,
        "status_id": projection["status_id"],
        "archive_id": projection["archive_id"],
        "staging_id": staging_id,
        "semantic_delta_signature": projection["semantic_delta_signature"],
        "status_before_hash": projection.get("status_before_hash"),
        "status_before_meta": projection.get("status_before_meta"),
        "active_release_id": projection.get("active_release_id"),
        "active_production_targets": sorted(
            item["id"] for item in projection["existing_targets"]
        ),
        "reserved_staging_targets": sorted(reservations.values()),
        "reservations": reservations,
    }
    validated = validate_reservation(projection, allocation)
    policy = instance.read_json("production.json")
    validate_policy_reconciliation(projection, allocation, policy)
    for item in projection["new_targets"]:
        file_id = allocation["reservations"][item["logical_key"]]
        policy["targets"][file_id] = {
            "mime": item["mime"],
            "mode": item["mode"],
            "allowed_parents": [
                item["publish_parent"], allocation["staging_id"],
            ],
            "staging_parent": allocation["staging_id"],
            "publish_parent": item["publish_parent"],
        }
    save(instance.config_path("production.json"), policy)
    save(allocation_path, allocation)
    return {"allocation": allocation, "validated": validated}


def _candidate_inputs(
    drive,
    engine_root,
    projection,
    allocation,
    output,
    consumer_target_contents=None,
):
    output.mkdir(parents=True, exist_ok=False)
    candidates = output / "candidate-inputs"
    candidates.mkdir()
    entries = []
    retiring_keys = {
        item["logical_key"] for item in projection.get("retiring_targets", [])
    }
    existing_by_key = {
        item["logical_key"]: item for item in projection["existing_targets"]
        if item["logical_key"] not in retiring_keys
    }
    new_by_key = {
        item["logical_key"]: item for item in projection["new_targets"]
    }
    id_by_key = {
        item["logical_key"]: item["id"]
        for item in existing_by_key.values()
    }
    id_by_key.update({
        key: file_id for key, file_id in allocation["reservations"].items()
    })
    code_hashes = {
        item["logical_key"]: item.get("current_sha256") or item.get("previous_sha256") or item.get("sha256")
        for item in projection["existing_targets"]
        if item["logical_key"].startswith("code/")
    }
    code_hashes.update({
        item["logical_key"]: item["sha256"]
        for item in projection["new_targets"]
    })
    ordered = sorted(set(existing_by_key) | set(new_by_key))
    item_by_key = {
        key: existing_by_key.get(key) or new_by_key[key] for key in ordered
    }
    registry = None
    registry_bytes = None
    manifest_previous = None
    if _uses_closure(projection["release_id"]):
        if REGISTRY_KEY not in item_by_key or MANIFEST_KEY not in item_by_key:
            raise ProjectionError("CLOSURE_CONTROL_TARGET_MISSING")
        registry_previous, _ = snapshot(
            drive, item_by_key[REGISTRY_KEY]["id"],
            item_by_key[REGISTRY_KEY].get("mode", "binary"),
        )
        registry = load_registry(registry_previous)
        registry["registry_release_id"] = projection["release_id"]
        registry_bytes = yaml.safe_dump(
            registry, allow_unicode=True, sort_keys=False,
        ).encode()
        manifest_previous, _ = snapshot(
            drive, item_by_key[MANIFEST_KEY]["id"],
            item_by_key[MANIFEST_KEY].get("mode", "binary"),
        )
    data_by_key = {}
    previous_by_key = {}
    carried_code_targets = set(projection.get("carried_forward_code_targets", []))

    # Preload MASTER row count fail-closed before rendering control documents regardless of key sort order.
    csv_count = None
    xlsx_count = None
    if "derived/MASTER.csv" in item_by_key and "derived/MASTER.csv" not in carried_code_targets and "derived/MASTER.csv" not in new_by_key:
        m_item = item_by_key["derived/MASTER.csv"]
        prev_master, _ = snapshot(drive, m_item["id"], m_item.get("mode", "binary"))
        previous_by_key["derived/MASTER.csv"] = prev_master
        data_by_key["derived/MASTER.csv"] = prev_master
        try:
            csv_count = compute_master_row_count(prev_master, is_xlsx=False)
        except Exception as exc:
            raise ProjectionError(f"MASTER_ROW_COUNT_INVALID:{exc}") from exc
    if "input/MASTER.xlsx" in item_by_key and "input/MASTER.xlsx" not in carried_code_targets and "input/MASTER.xlsx" not in new_by_key:
        m_item = item_by_key["input/MASTER.xlsx"]
        prev_master, _ = snapshot(drive, m_item["id"], m_item.get("mode", "binary"))
        previous_by_key["input/MASTER.xlsx"] = prev_master
        data_by_key["input/MASTER.xlsx"] = prev_master
        try:
            xlsx_count = compute_master_row_count(prev_master, is_xlsx=True)
        except Exception as exc:
            raise ProjectionError(f"MASTER_ROW_COUNT_INVALID:{exc}") from exc
    if csv_count is not None and xlsx_count is not None and csv_count != xlsx_count:
        raise ProjectionError(f"MASTER_ROW_COUNT_MISMATCH:{csv_count}!={xlsx_count}")
    master_row_count = csv_count if csv_count is not None else xlsx_count
    requires_master_count = (
        bool(set(ordered) & {"entry/README", "entry/context", "CURRENT_VERSION_DOC", "derived/INDEX.md"})
        or _uses_closure(projection["release_id"])
    )
    if requires_master_count and master_row_count is None:
        raise ProjectionError("MASTER_INPUT_MISSING")

    for logical_key in ordered:
        if logical_key == "input/manifest.csv" or logical_key in data_by_key:
            continue
        item = item_by_key[logical_key]
        if logical_key in carried_code_targets:
            previous, _ = snapshot(
                drive, item["id"], item.get("mode", "binary"),
            )
            previous_by_key[logical_key] = previous
            data = previous
            generated = False
        elif logical_key.startswith("code/"):
            relative = logical_key.removeprefix("code/")
            data = (Path(engine_root) / relative).read_bytes()
            generated = True
        elif item.get("consumer_target_kind") == "consumer_surface":
            if not isinstance(consumer_target_contents, dict) or logical_key not in consumer_target_contents:
                raise ProjectionError(f"CONSUMER_SURFACE_CONTENT_MISSING:{logical_key}")
            data = consumer_target_contents[logical_key]
            if not isinstance(data, bytes):
                raise ProjectionError(f"CONSUMER_SURFACE_BYTES_REQUIRED:{logical_key}")
            if digest(data) != item.get("sha256"):
                raise ProjectionError(f"CONSUMER_SURFACE_HASH_MISMATCH:{logical_key}")
            generated = True
        else:
            previous, _ = snapshot(
                drive, item["id"], item.get("mode", "binary"),
            )
            previous_by_key[logical_key] = previous
            generated_data = _control_candidate(
                logical_key, previous, projection, allocation, id_by_key,
                code_hashes, engine_root, registry, registry_bytes,
                manifest_previous, item_by_key=item_by_key,
                master_row_count=master_row_count,
            )
            generated = generated_data is not None
            data = generated_data if generated else previous
        if generated and item.get("mode") == "managed_doc":
            data = _managed_candidate(data)
        data_by_key[logical_key] = data
    if MANIFEST_KEY in item_by_key:
        if manifest_previous is None:
            manifest_previous, _ = snapshot(
                drive, item_by_key[MANIFEST_KEY]["id"],
                item_by_key[MANIFEST_KEY].get("mode", "binary"),
            )
        previous_by_key[MANIFEST_KEY] = manifest_previous
        data_by_key[MANIFEST_KEY] = _manifest_candidate(
            manifest_previous, projection, allocation, id_by_key,
            data_by_key, item_by_key,
        )
    for index, logical_key in enumerate(ordered):
        item = item_by_key[logical_key]
        candidate = candidates / str(index)
        data = data_by_key[logical_key]
        atomic(candidate, data)
        if logical_key in new_by_key:
            change_class = (
                "NEW_CONSUMER_SURFACE"
                if item.get("consumer_target_kind") == "consumer_surface"
                else "NEW_CODE_MIRROR"
            )
        elif logical_key in CONTROL_KEYS:
            change_class = "CONTROL_METADATA_UPDATE"
        elif logical_key in carried_code_targets:
            change_class = "CARRIED_FORWARD_CODE_MIRROR"
        elif logical_key.startswith("code/"):
            change_class = "CODE_MIRROR_UPDATE"
        else:
            change_class = "BUSINESS_FACT_CARRY_FORWARD"
        entry = {
            "logical_key": logical_key,
            "id": item.get("id") or allocation["reservations"][logical_key],
            "name": item["name"],
            "mime": item["mime"],
            "mode": item.get("mode", "binary"),
            "path": str(candidate),
            "change_class": change_class,
            "before_hash": (
                digest(previous_by_key[logical_key])
                if logical_key in previous_by_key else None
            ),
        }
        if logical_key in new_by_key:
            entry.update({
                "allowed_parents": [
                    new_by_key[logical_key]["publish_parent"],
                    allocation["staging_id"],
                ],
                "staging_parent": allocation["staging_id"],
                "publish_parent": new_by_key[logical_key]["publish_parent"],
            })
        else:
            entry["allowed_parents"] = item.get("allowed_parents") or []
            if item.get("staging_parent"):
                entry["staging_parent"] = item["staging_parent"]
            if item.get("publish_parent"):
                entry["publish_parent"] = item["publish_parent"]
        entries.append(entry)
    return entries


def _manifest_candidate(
    previous, projection, allocation, id_by_key, data_by_key, item_by_key
):
    reader = csv.DictReader(io.StringIO(previous.decode("utf-8-sig")))
    if not reader.fieldnames:
        raise ProjectionError("MANIFEST_SCHEMA_REQUIRED")
    columns = list(reader.fieldnames)
    for name in ("published_release", "hash_scope"):
        if name not in columns:
            columns.append(name)
    rows = list(reader)
    by_id = {}
    by_key = {}
    for row in rows:
        file_id = row.get("drive_file_id")
        logical_key = row.get("uid") or row.get("local_path")
        if not file_id or not logical_key:
            raise ProjectionError("MANIFEST_ROW_INCOMPLETE")
        if file_id in by_id or logical_key in by_key:
            raise ProjectionError("MANIFEST_DUPLICATE")
        by_id[file_id] = row
        by_key[logical_key] = row
    final_keys = sorted(id_by_key)
    if set(final_keys) != set(item_by_key):
        raise ProjectionError("MANIFEST_COVERAGE_MISMATCH")
    output_rows = []
    for logical_key in final_keys:
        file_id = id_by_key[logical_key]
        row = dict(by_key.get(logical_key) or by_id.get(file_id) or {})
        row["drive_file_id"] = file_id
        row["uid"] = logical_key
        row["published_release"] = projection["release_id"]
        if item_by_key[logical_key].get("consumer_target_kind") == "consumer_surface":
            row["asset_type"] = row.get("asset_type") or "release_artifact"
            row["source_type"] = row.get("source_type") or "generated"
            row["local_path"] = row.get("local_path") or logical_key
            row["status"] = "published"
        mode = item_by_key[logical_key].get("mode", "binary")
        row["hash_scope"] = "managed_prefix" if mode == "managed_doc" else "bytes"
        if logical_key == "input/manifest.csv":
            row["content_hash"] = ""
        elif logical_key in data_by_key:
            row["content_hash"] = digest(data_by_key[logical_key])
        output_rows.append({name: row.get(name, "") for name in columns})
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream, fieldnames=columns, lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(output_rows)
    return stream.getvalue().encode("utf-8-sig")


def _control_candidate(
    logical_key,
    previous,
    projection,
    allocation,
    id_by_key,
    code_hashes,
    engine_root,
    registry,
    registry_bytes,
    manifest_bytes,
    *,
    item_by_key=None,
    master_row_count=None,
):
    release_id = projection["release_id"]
    engine_sha = projection["engine_sha"]
    status_id = allocation["status_id"]
    if logical_key == REGISTRY_KEY:
        return registry_bytes if registry_bytes is not None else None
    if logical_key == "control/drive_map.yaml":
        mapping = yaml.safe_load(previous.decode())
        mapping["v1_5_release"] = {
            "release_id": release_id,
            "status_id": status_id,
            "code_commit": engine_sha,
            "targets": id_by_key,
        }
        return yaml.safe_dump(
            mapping, allow_unicode=True, sort_keys=False,
        ).encode()
    if logical_key == "entry/code":
        carried_set = set(projection.get("carried_forward_code_targets", []))
        current_hashes = {k: v for k, v in code_hashes.items() if k not in carried_set}
        carried_hashes = {k: v for k, v in code_hashes.items() if k in carried_set}
        lines = [
            "# 当前代码镜像",
            f"GitHub: https://github.com/SkyOneBranch/cba-kb-engine/tree/{engine_sha}",
            f"commit: {engine_sha}",
            "legacy/ 是历史脚本，禁止用旧 uploader/wechat/sync 向生产写入。",
        ]
        lines.extend(
            f"- {key} | SHA256 {current_hashes[key]}"
            for key in sorted(current_hashes)
        )
        if carried_hashes:
            lines.append("")
            lines.append("# 历史受控结转代码镜像（只读保留，非当前 Git HEAD 文件）")
            lines.extend(
                f"- {key} | SHA256 {carried_hashes[key]}"
                for key in sorted(carried_hashes)
            )
        return ("\n".join(lines) + "\n").encode()
    if logical_key == CONTEXT_CARD_KEY:
        if registry is None or manifest_bytes is None:
            return None
        metadata = target_metadata(release_id, engine_sha, registry)
        return generate_context_card(
            metadata, registry, manifest_bytes,
        ).encode()
    if logical_key in {
        "entry/README", "entry/context", "CURRENT_VERSION_DOC",
        "derived/INDEX.md",
    }:
        return _content_preserving_candidate(
            logical_key, previous, projection, allocation, id_by_key,
            engine_root, registry, item_by_key=item_by_key,
            master_row_count=master_row_count,
        )
    return None


def _readable_target_label(logical_key, item_by_key):
    if isinstance(item_by_key, dict) and isinstance(item_by_key.get(logical_key), dict):
        name = item_by_key[logical_key].get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return logical_key.rsplit("/", 1)[-1]


def _harvest_known_drive_ids(id_by_key, item_by_key, *, release_id, engine_sha):
    safe_names = {release_id, engine_sha}
    if isinstance(item_by_key, dict):
        for spec in item_by_key.values():
            if isinstance(spec, dict) and isinstance(spec.get("name"), str):
                safe_names.add(spec["name"].strip())
    ids = []
    if isinstance(id_by_key, dict):
        for fid in id_by_key.values():
            if (
                isinstance(fid, str)
                and len(fid.strip()) >= 12
                and " " not in fid
                and fid.strip() not in safe_names
            ):
                ids.append(fid.strip())
    return tuple(ids)


def _content_preserving_candidate(
    logical_key,
    previous,
    projection,
    allocation,
    id_by_key,
    engine_root,
    registry=None,
    *,
    item_by_key=None,
    master_row_count=None,
):
    if (
        isinstance(master_row_count, bool)
        or not isinstance(master_row_count, int)
        or master_row_count <= 0
    ):
        raise ProjectionError("MASTER_ROW_COUNT_REQUIRED")
    release_id = projection["release_id"]
    engine_sha = projection["engine_sha"]
    known_drive_ids = _harvest_known_drive_ids(
        id_by_key, item_by_key, release_id=release_id, engine_sha=engine_sha
    )
    metadata = (
        target_metadata(release_id, engine_sha, registry)
        if registry is not None else None
    )
    title = {
        "entry/README": "CBA-KB 发布入口",
        "entry/context": "CBA-KB 当前状态",
        "CURRENT_VERSION_DOC": "CBA-KB 当前版本",
        "derived/INDEX.md": "CBA-KB INDEX",
    }[logical_key]
    links = "\n".join(
        f"- {key} -> {_readable_target_label(key, item_by_key)}"
        for key in sorted(id_by_key)
    )
    consumer_guide = render_consumer_surface_guide_section(
        release_id=release_id,
        master_row_count=master_row_count,
        include_collector_digest=bool(_release_spec(release_id).get("enable_operational_kb_v220")),
    )
    if logical_key == "entry/context":
        operations = (
            Path(engine_root) / "docs/OPERATIONS_V1_5.md"
        ).read_text()
        migration = (
            Path(engine_root) / "docs/MASTER_MIGRATION_NOTE.md"
        ).read_text()
        body_text = (
            f"# {title}\n\n"
            f"当前发布：{release_id}\n\n"
            f"代码提交：{engine_sha}\n\n"
            "读取前请校验 release_status；"
            "PUBLISHING/FAILED/ROLLING_BACK 时使用上一快照。\n\n"
            f"代码镜像：https://github.com/SkyOneBranch/cba-kb-engine/tree/{engine_sha}\n\n"
            f"{consumer_guide}\n\n"
            f"## Current target mapping\n\n{links}\n\n"
            f"## Operations\n\n{operations}\n\n"
            f"## Migration\n\n{migration}\n"
        )
        if metadata is not None:
            from cba_kb.current_state import render_current_state
            body_text = render_current_state(metadata) + body_text
        return body_text.encode()
    preserved = scrub_raw_drive_ids_for_consumer(
        _managed_body(previous), known_drive_ids=known_drive_ids
    )
    if metadata is not None:
        preserved = replace_current_block(preserved, metadata)
    historical_releases = {
        projection.get("active_release_id"),
        "v1.5.4-1",
    }
    for previous_release in sorted(
        release for release in historical_releases if release
    ):
        preserved = preserved.replace(
            f"当前发布：{previous_release}",
            f"历史发布：{previous_release}",
        )
        preserved = preserved.replace(
            f"{previous_release} / COMPLETE",
            f"{previous_release} / 历史发布",
        )
        preserved = preserved.replace(
            f"current release: {previous_release}",
            f"historical release: {previous_release}",
        )
    return (
        f"# {title}\n\n"
        f"当前发布：{release_id}\n\n"
        f"代码提交：{engine_sha}\n\n"
        "读取前请校验 release_status；"
        "PUBLISHING/FAILED/ROLLING_BACK 时使用上一快照。\n\n"
        f"代码镜像：https://github.com/SkyOneBranch/cba-kb-engine/tree/{engine_sha}\n\n"
        f"{consumer_guide}\n\n"
        f"## Current target mapping\n\n{links}\n\n"
        f"## Previous current navigation\n\n{preserved}\n"
    ).encode()


def _evidence_baseline(drive, roots):
    roots = sorted({root for root in roots if root})
    if not roots:
        return []
    items, _ = _inventory(drive, {
        "current": [],
        "history": [],
        "staging": [],
        "evidence": roots,
    })
    protected = []
    for item in sorted(
        (
            item for item in items
            if item.get("mimeType") != FOLDER
        ),
        key=lambda item: item["id"],
    ):
        mode = (
            "managed_doc"
            if item.get("mimeType") == NATIVE_DOCUMENT else "binary"
        )
        data, _ = snapshot(drive, item["id"], mode)
        protected.append({
            "id": item["id"],
            "name": item["name"],
            "sha256": digest(data),
            "mode": mode,
            "kind": "evidence",
        })
    return protected


def _build_closure_contract(
    *, drive, instance, projection, allocation, entries, state
):
    """Build the production closure from actual candidate targets and zones."""
    by_key = {entry["logical_key"]: entry for entry in entries}
    document_keys = {
        "readme": "entry/README",
        "index": "derived/INDEX.md",
        "context_card": CONTEXT_CARD_KEY,
        "current_version_doc": "CURRENT_VERSION_DOC",
    }
    if _uses_technical_manual(projection.get("release_id")):
        document_keys["technical_manual"] = "entry/context"
    required = {REGISTRY_KEY, MANIFEST_KEY, *document_keys.values()}
    if not required <= set(by_key):
        raise ProjectionError("CLOSURE_CONTROL_TARGET_MISSING")
    status = state["status"]
    previous_code_commit = (
        status.get("code_commit") or status.get("published_code_commit")
    )
    if not isinstance(previous_code_commit, str) or not re.fullmatch(
        "[0-9a-f]{40}", previous_code_commit,
    ):
        raise ProjectionError("CLOSURE_PREVIOUS_CODE_REQUIRED")
    protected_specs = (
        ("input/MASTER.xlsx", "master"),
        ("facts/CBA_注册领域_六表.xlsx", "six_table"),
        ("facts/CBA_球员注册_EVENTS.xlsx", "six_table"),
        ("facts/CBA_外籍球员注册_SNAPSHOTS.xlsx", "six_table"),
        ("input/source_registry.csv", "source_registry"),
        ("evidence/bayi_legacy_context.md", "evidence"),
    )
    protected = []
    for logical_key, kind in protected_specs:
        try:
            entry = by_key[logical_key]
        except KeyError:
            raise ProjectionError("CLOSURE_PROTECTED_TARGET_MISSING") from None
        before_hash = entry.get("before_hash")
        if (
            not isinstance(before_hash, str)
            or not re.fullmatch("[0-9a-f]{64}", before_hash)
        ):
            raise ProjectionError("CLOSURE_PROTECTED_HASH_REQUIRED")
        protected.append({
            "id": entry["id"],
            "name": entry["name"],
            "sha256": before_hash,
            "mode": entry.get("mode", "binary"),
            "kind": kind,
        })
    policy = None
    if instance is not None and hasattr(instance, "read_json"):
        try:
            policy = instance.read_json("production.json")
        except Exception:
            policy = None

    has_explicit_topology = False
    if isinstance(policy, dict):
        if ("topology" in policy and isinstance(policy["topology"], list) and policy["topology"]) or (
            "zones" in policy and isinstance(policy["zones"], dict) and policy["zones"]
        ):
            has_explicit_topology = True
    if not has_explicit_topology:
        try:
            has_explicit_topology = _optional_instance_topology(instance) is not None
        except PreMutationAbort as exc:
            raise ProjectionError(f"CLOSURE_TOPOLOGY_INVALID: {exc}") from exc

    if has_explicit_topology:
        planned_target_ids = [
            e["id"] for e in entries
            if isinstance(e, dict) and e.get("id")
        ]
        try:
            nodes = build_release_topology_from_production(
                policy,
                instance=instance,
                release_id=projection.get("release_id"),
                planned_target_ids=planned_target_ids,
                allocation_staging_id=allocation.get("staging_id"),
            )
            validate_release_topology(
                nodes,
                release_id=projection.get("release_id"),
                planned_target_ids=planned_target_ids,
            )
        except Exception as exc:
            raise ProjectionError(f"CLOSURE_TOPOLOGY_INVALID: {exc}") from exc

        current = {node["id"] for node in nodes if node.get("role") == "CURRENT_ZONE"}
        history = {node["id"] for node in nodes if node.get("role") == "HISTORY_ZONE"}
        staging = {node["id"] for node in nodes if node.get("role") == "STAGING_ZONE"}
        evidence = {node["id"] for node in nodes if node.get("role") == "EVIDENCE_ZONE"}

        if not current or not history or not staging or not evidence:
            raise ProjectionError("CLOSURE_ZONE_INCOMPLETE")

        staging_id = allocation.get("staging_id")
        if not staging_id or staging_id not in staging:
            raise ProjectionError("CLOSURE_STAGING_ZONE_MISMATCH")

        groups = [current, history, evidence, staging]
        if sum(len(group) for group in groups) != len(set().union(*groups)):
            raise ProjectionError("CLOSURE_ZONE_OVERLAP")
    else:
        parents = instance.read_json("import_inventory.json")["parents"]
        current = {
            parents["root"], parents["ai"], parents["scripts"],
            parents["config"], parents["data"],
        }
        evidence_keys = {
            logical_key for logical_key, kind in protected_specs
            if kind == "evidence"
        }
        for logical_key in required | (
            {item[0] for item in protected_specs} - evidence_keys
        ):
            parent = by_key[logical_key].get("publish_parent")
            if parent and parent != parents["archive"]:
                current.add(parent)
        history = {parents["archive"]}
        evidence = {
            by_key[logical_key].get("publish_parent")
            for logical_key in evidence_keys
            if by_key[logical_key].get("publish_parent")
        }
        if not evidence:
            raise ProjectionError("CLOSURE_EVIDENCE_ZONE_MISSING")
        staging = {
            entry.get("staging_parent") for entry in entries
            if entry.get("staging_parent")
        }
        staging.add(allocation["staging_id"])
        groups = [current, history, evidence, staging]
        if sum(len(group) for group in groups) != len(set().union(*groups)):
            raise ProjectionError("CLOSURE_ZONE_OVERLAP")
    protected_by_id = {item["id"]: item for item in protected}
    for item in _evidence_baseline(drive, evidence):
        protected_by_id.setdefault(item["id"], item)
    protected = list(protected_by_id.values())
    return {
        "code_commit": projection["engine_sha"],
        "previous_code_commit": previous_code_commit,
        "baseline_release_id": status.get("current_release_id"),
        "registry_key": REGISTRY_KEY,
        "manifest_key": MANIFEST_KEY,
        "documents": document_keys,
        "zones": {
            "current": sorted(current),
            "history": sorted(history),
            "staging": sorted(staging),
            "evidence": sorted(evidence),
        },
        "protected": protected,
    }


def _parse_bundle_frontmatter_and_body(raw_text: str) -> tuple[dict, str]:
    """Split YAML frontmatter and body from an acquisition_bundle_v1 markdown capture."""
    if raw_text.startswith("---\n"):
        parts = raw_text.split("---\n", 2)
        if len(parts) == 3:
            try:
                loaded = yaml.safe_load(parts[1])
                if isinstance(loaded, dict):
                    return loaded, parts[2]
            except yaml.YAMLError:
                pass
    return {}, raw_text


def _load_operational_bundle_records(instance, checkpoint: dict) -> list[dict]:
    """Load consumed bundle metadata + full sanitized bodies from document_archive."""
    raw_consumed = checkpoint.get("consumed_bundles")
    entries: list[tuple[str, dict]] = []
    if isinstance(raw_consumed, dict):
        for key in sorted(raw_consumed):
            val = raw_consumed[key]
            if isinstance(val, dict):
                entries.append((str(key), val))
    elif isinstance(raw_consumed, list):
        for idx, val in enumerate(raw_consumed):
            if isinstance(val, dict):
                entries.append((str(idx), val))

    records: list[dict] = []
    seen_doc_ids: set[str] = set()
    for _key, ckpt_meta in sorted(
        entries, key=lambda item: (str(item[1].get("doc_id") or ""), item[0])
    ):
        doc_id = str(ckpt_meta.get("doc_id") or "").strip()
        if not doc_id or doc_id in seen_doc_ids:
            continue
        seen_doc_ids.add(doc_id)

        doc_dir = instance.document_archive_root / doc_id
        fm: dict = {}
        body = ""
        raw_dir = doc_dir / "raw"
        raw_mds = sorted(raw_dir.glob("*.md")) if raw_dir.is_dir() else []
        if raw_mds:
            fm, body = _parse_bundle_frontmatter_and_body(
                raw_mds[0].read_text(encoding="utf-8")
            )
        else:
            text_path = doc_dir / "text.txt"
            if text_path.is_file():
                body = text_path.read_text(encoding="utf-8")

        record_meta: dict = {}
        record_path = doc_dir / "record.json"
        if record_path.is_file():
            try:
                loaded_rec = json.loads(record_path.read_text(encoding="utf-8"))
                if isinstance(loaded_rec, dict):
                    record_meta = loaded_rec
            except (OSError, ValueError):
                pass

        records.append({
            "doc_id": doc_id,
            "title": fm.get("title") or ckpt_meta.get("title") or record_meta.get("title") or doc_id,
            "source_tier": fm.get("source_tier") or ckpt_meta.get("source_tier") or record_meta.get("source_tier"),
            "event_class": fm.get("event_class") or ckpt_meta.get("event_class") or record_meta.get("event_class"),
            "published_at": (
                fm.get("published_at")
                if "published_at" in fm
                else ckpt_meta.get("published_at", record_meta.get("published_at"))
            ),
            "acquired_at": fm.get("acquired_at") or record_meta.get("captured_at"),
            "canonical_url": fm.get("canonical_url") or ckpt_meta.get("canonical_url") or record_meta.get("canonical_url"),
            "upstream_origin": fm.get("upstream_origin") or record_meta.get("upstream_origin"),
            "raw_sha256": fm.get("raw_sha256") or ckpt_meta.get("file_sha256") or ckpt_meta.get("content_hash"),
            "matched_persons": fm.get("matched_persons") if isinstance(fm.get("matched_persons"), list) else (ckpt_meta.get("matched_persons") or []),
            "body": body,
        })
    return records


def _build_instance_consumer_targets(
    drive, instance, *, release_id, engine_sha, manifest_rows, data_parent
):
    """Build declared consumer-surface targets from existing Drive inputs & instance fixtures."""
    release_spec = _release_spec(release_id)
    by_key = _manifest_rows(manifest_rows)
    id_for = {logical_key: fid for fid, logical_key in by_key.items()}
    master_csv_id = id_for.get("derived/MASTER.csv")
    sr_id = id_for.get("input/source_registry.csv")
    if not master_csv_id or not sr_id:
        raise ProjectionError("CONSUMER_SURFACE_BASE_INPUTS_MISSING")
    master_bytes, _ = snapshot(drive, master_csv_id, "binary")
    sr_bytes, _ = snapshot(drive, sr_id, "binary")
    doc_idx_bytes = None
    if "data/normalized_document_index.json" in id_for:
        doc_idx_bytes, _ = snapshot(drive, id_for["data/normalized_document_index.json"], "binary")

    include_collector_digest = bool(release_spec.get("enable_operational_kb_v220"))
    collector_official_digest_bytes = None
    ops_dir = instance.data_root / "ops_sync_v040_1"
    ops_checkpoint_path = ops_dir / "operational_checkpoint.json"
    if include_collector_digest and ops_checkpoint_path.is_file():
        checkpoint = json.loads(ops_checkpoint_path.read_text(encoding="utf-8"))
        doc_idx_bytes = merge_operational_docs_into_consumer_index(
            doc_idx_bytes, checkpoint
        )
        reg_diff = None
        reg_diff_path = ops_dir / "reg_diff_2526_2627.json"
        if reg_diff_path.is_file():
            reg_diff = json.loads(reg_diff_path.read_text(encoding="utf-8"))
        bundle_records = _load_operational_bundle_records(instance, checkpoint)
        collector_official_digest_bytes = render_collector_official_digest_md(
            bundles=bundle_records,
            reg_diff=reg_diff,
            release_id=release_id,
            engine_sha=engine_sha,
        )

    promoted_research = {}
    slice_spec_path = instance.data_root / "promoted_research_slices.json"
    if slice_spec_path.is_file():
        spec_list = json.loads(slice_spec_path.read_text(encoding="utf-8"))
        if not isinstance(spec_list, list):
            raise ProjectionError("PROMOTED_RESEARCH_SLICES_INVALID")
        for item in spec_list:
            built = build_promoted_research_slice_artifacts(
                item["fixture"],
                player_uid=item["player_uid"],
                player_name=item["player_name"],
                player_id=item["player_id"],
                season=item["season"],
                match_type=item.get("match_type", "REGULAR"),
                scope=item.get("scope", "WHOLE_SEASON"),
                passage_label=item.get("passage_label", "主要场均数据表"),
                target_domain=item.get("target_domain", "production"),
                additional_match_types=item.get("additional_match_types", ()),
            )
            for k, v in built.items():
                v["publish_parent"] = data_parent
                promoted_research[k] = v

    targets = build_consumer_surface_targets_spec(
        release_id=release_id,
        engine_sha=engine_sha,
        master_csv_bytes=master_bytes,
        source_registry_bytes=sr_bytes,
        doc_index_bytes=doc_idx_bytes,
        collector_official_digest_bytes=collector_official_digest_bytes,
        include_collector_digest=include_collector_digest,
        promoted_research_artifacts=promoted_research,
    )
    for spec in targets.values():
        spec["publish_parent"] = data_parent
    return targets


def freeze_plan(
    drive,
    *,
    instance,
    engine_root,
    release_id,
    projection,
    allocation,
    output,
    consumer_target_contents=None,
):
    """Freeze all candidate bytes and a remote-read-only release plan."""
    release_spec = _require_release(release_id)
    verify_execution_sha(
        engine_root,
        projection["engine_sha"],
        release_spec["product_baseline_sha"],
    )
    projection = reproject_targets(projection, allocation)
    state = validate_release_state(drive, instance, projection, allocation)
    output = Path(output)
    if output.exists():
        raise ProjectionError("FREEZE_OUTPUT_MUST_BE_NEW")
    if consumer_target_contents is None and release_spec.get("enable_consumer_surface_v215"):
        runtime = instance.read_json("runtime.json")
        manifest_rows = _load_manifest(drive.get(runtime["inputs"]["manifest.csv"]["id"]))
        data_parent = instance.read_json("import_inventory.json")["parents"]["data"]
        built_targets = _build_instance_consumer_targets(
            drive,
            instance,
            release_id=release_id,
            engine_sha=projection["engine_sha"],
            manifest_rows=manifest_rows,
            data_parent=data_parent,
        )
        consumer_target_contents = {k: v["content"] for k, v in built_targets.items()}
    entries = _candidate_inputs(
        drive, engine_root, projection, allocation, output,
        consumer_target_contents=consumer_target_contents,
    )
    policy = instance.read_json("production.json")
    dependencies = load_production_dependencies(drive, policy)
    outbox = output / "outbox"
    closure = None
    if _uses_closure(release_id):
        closure = _build_closure_contract(
            drive=drive,
            instance=instance,
            projection=projection,
            allocation=allocation,
            entries=entries,
            state=state,
        )
    plan = prepare_release(
        drive,
        outbox,
        release_id,
        entries,
        allocation["archive_id"],
        allocation["status_id"],
        dependencies,
        carry_forward_artifacts=True,
        retired_artifacts=projection.get("retiring_targets", []),
        **({"closure": closure} if closure is not None else {}),
        environment="production",
        code_commit=projection["engine_sha"],
    )
    if {item["id"] for item in plan["dependencies"]} != {
        item["id"] for item in dependencies
    }:
        raise ProjectionError("PRODUCTION_DEPENDENCY_BINDING_MISMATCH")
    save(output / "allocation.json", allocation)
    if closure is not None:
        save(output / "closure.json", closure)
    save(output / "entries.json", plan["entries"])
    classification = {
        entry["logical_key"]: entry["change_class"] for entry in entries
    }
    classification.update({
        item["logical_key"]: "RETIRED_FROM_CURRENT"
        for item in projection.get("retiring_targets", [])
    })
    save(output / "target_classification.json", classification)
    save(output / "dependencies.json", dependencies)
    save(output / "release_state_reconciliation.json", state)
    rollback = {
        "release_id": release_id,
        "actions": [
            {
                "id": item["id"],
                "name": item["name"],
                "before_hash": item["before_hash"],
                "restore": item["before"],
                "validation": "digest_equals_before_hash",
            }
            for item in plan["entries"]
        ],
    }
    verification = {
        "release_id": release_id,
        "checks": [
            {
                "id": item["id"],
                "name": item["name"],
                "mime": item["mime"],
                "after_hash": item["after_hash"],
                "parent": item.get("publish_parent"),
            }
            for item in plan["entries"]
        ],
    }
    save(output / "rollback_manifest.json", rollback)
    save(output / "verification_manifest.json", verification)
    return {
        "state": "FROZEN_PLAN",
        "plan": str(outbox / "plan.json"),
        "journal": str(outbox / "journal.json"),
        "entries": len(plan["entries"]),
        "rollback_actions": len(rollback["actions"]),
        "verification_checks": len(verification["checks"]),
        "rollback_manifest_sha256": digest(
            (output / "rollback_manifest.json").read_bytes()
        ),
        "verification_manifest_sha256": digest(
            (output / "verification_manifest.json").read_bytes()
        ),
    }


def _project_command(args, instance):
    release_spec = _require_release(args.release)
    verify_execution_sha(
        Path.cwd(),
        args.engine_sha,
        release_spec["product_baseline_sha"],
    )
    production = instance.read_json("production.json")
    runtime = instance.read_json("runtime.json")
    drive = Drive(Path.cwd(), instance)
    status_raw = drive.get(production["status_id"])
    status_meta = drive.meta(production["status_id"])
    status = json.loads(status_raw)
    manifest = _load_manifest(
        drive.get(runtime["inputs"]["manifest.csv"]["id"])
    )
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=Path.cwd(),
    ).decode().split("\0")
    tracked = [path for path in tracked if path]
    hashes = {
        path: digest((Path.cwd() / path).read_bytes()) for path in tracked
    }
    allocation_data = read(args.allocation) if args.allocation else None
    targets = production["targets"]
    if allocation_data is None:
        active_ids = {
            item.get("id") for item in (status.get("artifacts") or [])
            if isinstance(item, dict) and item.get("id")
        }
        targets = {
            target_id: spec for target_id, spec in targets.items()
            if target_id in active_ids or (
                isinstance(spec, dict) and "retire_in_release" in spec
            )
        }
    inventory_parents = instance.read_json("import_inventory.json")["parents"]
    declared_consumer_targets = None
    if release_spec.get("enable_consumer_surface_v215"):
        declared_consumer_targets = _build_instance_consumer_targets(
            drive,
            instance,
            release_id=args.release,
            engine_sha=args.engine_sha,
            manifest_rows=manifest,
            data_parent=inventory_parents["data"],
        )
    projection = project_targets(
        release_id=args.release,
        engine_sha=args.engine_sha,
        status_id=production["status_id"],
        archive_id=production["archive_id"],
        previous_status=status,
        previous_targets=targets,
        manifest_rows=manifest,
        tracked=tracked,
        tracked_hashes=hashes,
        code_parent=inventory_parents["scripts"],
        status_hash=digest(status_raw),
        status_meta=fingerprint(status_meta),
        allocation=allocation_data,
        allowed_removed_code_keys=production.get("allowed_removed_code_keys"),
        declared_consumer_targets=declared_consumer_targets,
    )
    save(args.output, projection)
    return projection


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "step", choices=["project", "reserve-staging", "freeze", "compat-preflight"],
    )
    parser.add_argument("--release", default=RELEASE_ID)
    parser.add_argument("--instance-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--projection", type=Path)
    parser.add_argument("--allocation", type=Path)
    parser.add_argument("--single-writer", action="store_true")
    parser.add_argument("--engine-sha")
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--safe-baseline-release-id")
    parser.add_argument("--source-registry-sha", "--source-registry-sha256", dest="source_registry_sha")
    parser.add_argument("--product-version")
    parser.add_argument("--topology", type=Path)
    parser.add_argument("--consumer-manifest", type=Path)
    parser.add_argument("--identity-projection", type=Path)
    parser.add_argument("--platform-results", type=Path)
    parser.add_argument("--require-environmental-stability", action="store_true")
    args = parser.parse_args(argv)
    if args.step == "compat-preflight" and not args.product_version:
        parser.error("--product-version is required for compat-preflight")
    if args.step != "compat-preflight":
        _require_release(args.release)
        if not args.output:
            parser.error("--output is required")
        if args.step == "project" and not args.engine_sha:
            parser.error("--engine-sha is required for project")
    root = Path.cwd()
    instance = load_instance(root, args.instance_root)
    if args.step == "compat-preflight":
        result = run_trusted_release_infra_compatibility_preflight(
            instance=instance,
            engine_root=root,
            evidence_root=args.evidence_root,
            safe_baseline_release_id=args.safe_baseline_release_id,
            source_registry_sha256=args.source_registry_sha,
            expected_product_version=args.product_version,
            topology_path=args.topology,
            consumer_manifest_path=args.consumer_manifest,
            identity_projection_path=args.identity_projection,
            platform_results_path=args.platform_results,
            require_environmental_stability=args.require_environmental_stability,
        )
        if args.output:
            output = _private_output(instance, args.output)
            save(output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    output = _private_output(instance, args.output)
    if args.step == "project":
        result = _project_command(args, instance)
    else:
        if not args.projection:
            parser.error("--projection is required")
        projection = read(args.projection)
        if args.step == "reserve-staging":
            result = reserve_staging(
                placement_enforced(Drive(root, instance)),
                instance,
                release_id=args.release,
                projection=projection,
                output=output,
                single_writer=args.single_writer,
            )
        else:
            if not args.allocation:
                parser.error("--allocation is required")
            allocation = read(args.allocation)
            result = freeze_plan(
                Drive(root, instance),
                instance=instance,
                engine_root=root,
                release_id=args.release,
                projection=projection,
                allocation=allocation,
                output=output,
            )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    main()
