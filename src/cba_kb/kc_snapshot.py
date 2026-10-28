"""PR-B (Issue #220) versioned catalog, immutable snapshots, manifest
binding — offline/synthetic only.

Hash discipline (stated, never conflated):
- Algorithm: SHA-256.
- Covered bytes: UTF-8 bytes of the canonical JSON (sorted keys,
  compact separators) of {entries, target_domain, capabilities,
  build_provenance, build_time, data_release, engine_candidate,
  build_code}.
- Self-hash exclusion rule: snapshot_id and content_sha256 are computed
  from the content and then attached; they are never part of the hashed
  content, so any content change changes the binding.
- A Git blob SHA, a Git commit SHA, a file SHA-256, a specification
  JSON digest, and a Google Docs export digest are different kinds with
  different covered bytes; this module never equates them.
- Data release / Engine candidate / build code are SEPARATE tracks,
  never collapsed into one version number.
- Published state and validator receipt are SIMULATED: this module
  produces no real publication, real authorization, or real Drive
  readback evidence.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from .kc_projection import KcError, canonical_json, sha256_hex

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_AUTHORITIES = frozenset({"canonical", "derived", "control"})
_RIGHTS = frozenset({"public", "copyrighted", "private"})
COVERED_FIELDS = ["entries", "target_domain", "capabilities",
                  "build_provenance", "build_time", "data_release",
                  "engine_candidate", "build_code"]


class KcSnapshotError(KcError):
    """Fail-closed snapshot contract violation."""


@dataclass(frozen=True)
class SnapshotEntry:
    entry_id: str
    sha256: str
    authority: str
    rights: str
    source_id: str


@dataclass(frozen=True)
class SimulatedValidatorReceipt:
    producer_validated: bool
    connector_returned: str
    client_observed: str
    client_computed_digest: bool
    simulated: bool


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    content_sha256: str
    digest_kind: str
    algorithm: str
    covered_bytes: str
    entries: Tuple[SnapshotEntry, ...]
    target_domain: str
    capabilities: Tuple[str, ...]
    build_provenance: str
    build_time: str
    simulated_published_state: str
    data_release: str
    engine_candidate: str
    build_code: str
    target_authorization_domain: str
    catalog_references: Tuple[str, ...]
    source_digests: Tuple[str, ...]
    simulated_validator_receipt: SimulatedValidatorReceipt


def _plain_entry(entry: Any) -> Dict[str, Any]:
    if isinstance(entry, dict):
        return {k: entry[k] for k in
                ("entry_id", "sha256", "authority", "rights", "source_id")}
    data = vars(entry)
    return {k: data[k] for k in
            ("entry_id", "sha256", "authority", "rights", "source_id")}


def _content_dict(entries: List[Dict[str, Any]], target_domain: str,
                  capabilities: List[str], build_provenance: str,
                  build_time: str, data_release: str,
                  engine_candidate: str, build_code: str) -> Dict[str, Any]:
    return {
        "entries": sorted(entries, key=lambda e: e["entry_id"]),
        "target_domain": target_domain,
        "capabilities": sorted(capabilities),
        "build_provenance": build_provenance,
        "build_time": build_time,
        "data_release": data_release,
        "engine_candidate": engine_candidate,
        "build_code": build_code,
    }


def _covered_bytes_description() -> str:
    return ("UTF-8 bytes of canonical JSON of "
            "{entries, target_domain, capabilities, build_provenance, "
            "build_time, data_release, engine_candidate, build_code}")


def build_snapshot(*, entries: List[Dict[str, Any]], target_domain: str,
                   capabilities: List[str], build_provenance: str,
                   build_time: str, data_release: str,
                   engine_candidate: str, build_code: str,
                   simulated_published_state: str = "SIMULATED_PUBLISHED"
                   ) -> Snapshot:
    """Build an immutable snapshot. Entry validation is fail-closed:
    a bad SHA-256, authority, or rights value is a build failure."""
    plain = []
    for entry in entries:
        item = _plain_entry(entry)
        if not _SHA256_RE.match(item.get("sha256", "")):
            raise KcSnapshotError(
                f"INVALID_ENTRY_SHA256:{item.get('entry_id')}")
        if item.get("authority") not in _AUTHORITIES:
            raise KcSnapshotError(
                f"INVALID_ENTRY_AUTHORITY:{item.get('entry_id')}")
        if item.get("rights") not in _RIGHTS:
            raise KcSnapshotError(
                f"INVALID_ENTRY_RIGHTS:{item.get('entry_id')}")
        plain.append(item)
    content = _content_dict(plain, target_domain, capabilities,
                            build_provenance, build_time, data_release,
                            engine_candidate, build_code)
    digest = sha256_hex(canonical_json(content))
    frozen_entries = tuple(
        SnapshotEntry(**item)
        for item in sorted(plain, key=lambda e: e["entry_id"]))
    return Snapshot(
        snapshot_id=f"kc-snapshot:{digest}",
        content_sha256=digest,
        digest_kind="KC_SNAPSHOT_SHA256",
        algorithm="SHA-256",
        covered_bytes=_covered_bytes_description(),
        entries=frozen_entries,
        target_domain=target_domain,
        capabilities=tuple(sorted(capabilities)),
        build_provenance=build_provenance,
        build_time=build_time,
        simulated_published_state=simulated_published_state,
        data_release=data_release,
        engine_candidate=engine_candidate,
        build_code=build_code,
        target_authorization_domain=target_domain,
        catalog_references=tuple(e.entry_id for e in frozen_entries),
        source_digests=tuple(e.sha256 for e in frozen_entries),
        simulated_validator_receipt=SimulatedValidatorReceipt(
            producer_validated=True, connector_returned="NOT_OBSERVED",
            client_observed="NOT_OBSERVED", client_computed_digest=False,
            simulated=True),
    )


def validate_snapshot(snapshot: Any) -> Dict[str, Any]:
    """Recompute the content digest and compare. Accepts a Snapshot or
    a plain dict with the same fields (dict form is used by tamper
    tests). Reading a digest string is NOT computing a digest."""
    if isinstance(snapshot, dict):
        get = snapshot.get
        entries = [_plain_entry(e) for e in snapshot["entries"]]
    else:
        get = lambda key, default=None: getattr(snapshot, key, default)
        entries = [_plain_entry(e) for e in snapshot.entries]
    content = _content_dict(
        entries, get("target_domain"), list(get("capabilities")),
        get("build_provenance"), get("build_time"), get("data_release"),
        get("engine_candidate"), get("build_code"))
    expected = sha256_hex(canonical_json(content))
    valid = (get("content_sha256") == expected
             and get("snapshot_id") == f"kc-snapshot:{expected}"
             and get("digest_kind") == "KC_SNAPSHOT_SHA256")
    return {"valid": valid, "digest_kind": get("digest_kind"),
            "algorithm": get("algorithm")}


def rebuild_snapshot(snapshot: Snapshot, *, entries: List[Dict[str, Any]],
                     target_domain: str, capabilities: List[str],
                     build_provenance: str, build_time: str,
                     data_release: str, engine_candidate: str,
                     build_code: str) -> Dict[str, Any]:
    """Deterministic rebuild: same source + same authorization view
    must rebuild a semantically identical snapshot (same snapshot_id).
    Any difference is reported, never papered over."""
    rebuilt = build_snapshot(entries=entries, target_domain=target_domain,
                             capabilities=capabilities,
                             build_provenance=build_provenance,
                             build_time=build_time,
                             data_release=data_release,
                             engine_candidate=engine_candidate,
                             build_code=build_code)
    return {"identical": rebuilt.snapshot_id == snapshot.snapshot_id,
            "rebuilt_snapshot_id": rebuilt.snapshot_id,
            "original_snapshot_id": snapshot.snapshot_id}


def build_catalog_snapshot(*, slices: List[Dict[str, Any]],
                           expected_slice_keys: List[Tuple[Any, ...]],
                           target_domain: str, capabilities: List[str],
                           build_provenance: str, build_time: str,
                           data_release: str, engine_candidate: str,
                           build_code: str) -> Snapshot:
    """Catalog snapshot over declared slices. A missing or
    incomplete slice is a BUILD FAILURE, not a silent omission."""
    present = {tuple(s["slice_key"]) for s in slices
               if s.get("end_marker")}
    missing = [key for key in expected_slice_keys
               if tuple(key) not in present]
    if missing:
        raise KcSnapshotError(f"CATALOG_SLICES_MISSING:{missing}")
    entries = []
    for key in sorted(expected_slice_keys, key=str):
        entries.append({
            "entry_id": "slice:" + "|".join(str(p) for p in key),
            "sha256": hashlib.sha256(
                canonical_json({"slice_key": [str(p) for p in key]})
            ).hexdigest(),
            "authority": "derived", "rights": "public",
            "source_id": "simulated-slice"})
    return build_snapshot(entries=entries, target_domain=target_domain,
                          capabilities=capabilities,
                          build_provenance=build_provenance,
                          build_time=build_time, data_release=data_release,
                          engine_candidate=engine_candidate,
                          build_code=build_code)
