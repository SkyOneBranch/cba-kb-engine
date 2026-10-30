"""PR-B (Issue #220) versioned catalog, immutable snapshots, manifest
binding — offline/synthetic only.

Hash discipline (stated, never conflated):
- Algorithm: SHA-256.
- Covered bytes: UTF-8 bytes of the canonical JSON (sorted keys,
  compact separators) of EVERY Snapshot field except snapshot_id and
  content_sha256 themselves: entries, target_domain, capabilities,
  build_provenance, build_time, simulated_published_state, data_release,
  engine_candidate, build_code, target_authorization_domain,
  catalog_references, source_digests, simulated_validator_receipt,
  digest_kind, algorithm, covered_bytes.
- Self-hash exclusion rule: snapshot_id and content_sha256 are computed
  from the content and then attached; they are never part of the hashed
  content, so any content change changes the binding.
- Repair round 1 (Q2 FAIL response): build_catalog_snapshot now digests
  full slice payloads (not just slice keys), and validate_snapshot now
  covers every field listed above. A valid digest therefore proves the
  whole manifest except the two self-hash fields themselves.
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
                  "build_provenance", "build_time",
                  "simulated_published_state", "data_release",
                  "engine_candidate", "build_code",
                  "target_authorization_domain", "catalog_references",
                  "source_digests", "simulated_validator_receipt",
                  "digest_kind", "algorithm", "covered_bytes"]


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


def _plain_receipt(receipt: Any) -> Dict[str, Any]:
    fields = ("producer_validated", "connector_returned",
              "client_observed", "client_computed_digest", "simulated")
    if isinstance(receipt, dict):
        return {k: receipt.get(k) for k in fields}
    if receipt is None:
        return {k: None for k in fields}
    return {k: getattr(receipt, k, None) for k in fields}


def _content_dict(entries: List[Dict[str, Any]], target_domain: str,
                  capabilities: List[str], build_provenance: str,
                  build_time: str, simulated_published_state: str,
                  data_release: str, engine_candidate: str,
                  build_code: str, target_authorization_domain: str,
                  catalog_references: List[str], source_digests: List[str],
                  simulated_validator_receipt: Dict[str, Any],
                  digest_kind: str, algorithm: str,
                  covered_bytes: str) -> Dict[str, Any]:
    return {
        "entries": sorted(entries, key=lambda e: e["entry_id"]),
        "target_domain": target_domain,
        "capabilities": sorted(capabilities),
        "build_provenance": build_provenance,
        "build_time": build_time,
        "simulated_published_state": simulated_published_state,
        "data_release": data_release,
        "engine_candidate": engine_candidate,
        "build_code": build_code,
        "target_authorization_domain": target_authorization_domain,
        "catalog_references": sorted(catalog_references),
        "source_digests": sorted(source_digests),
        "simulated_validator_receipt": simulated_validator_receipt,
        "digest_kind": digest_kind,
        "algorithm": algorithm,
        "covered_bytes": covered_bytes,
    }


def _covered_bytes_description() -> str:
    return ("UTF-8 bytes of canonical JSON of "
            "{entries, target_domain, capabilities, build_provenance, "
            "build_time, simulated_published_state, data_release, "
            "engine_candidate, build_code, target_authorization_domain, "
            "catalog_references, source_digests, "
            "simulated_validator_receipt, digest_kind, algorithm, "
            "covered_bytes}")


def build_snapshot(*, entries: List[Dict[str, Any]], target_domain: str,
                   capabilities: List[str], build_provenance: str,
                   build_time: str, data_release: str,
                   engine_candidate: str, build_code: str,
                   simulated_published_state: str = "SIMULATED_PUBLISHED"
                   ) -> Snapshot:
    """INTERNAL primitive: build an immutable snapshot from
    caller-supplied entries. Entry validation is fail-closed: a bad
    SHA-256, authority, or rights value is a build failure. A raw
    build_snapshot() result is not by itself a consumer-trusted
    snapshot: consumer trust requires binding through
    kc_consume.ConsumeSession.bind_current() (digest recomputed,
    target checked) and the entry's citation/answer paths."""
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
    frozen_entries = tuple(
        SnapshotEntry(**item)
        for item in sorted(plain, key=lambda e: e["entry_id"]))
    receipt = SimulatedValidatorReceipt(
        producer_validated=True, connector_returned="NOT_OBSERVED",
        client_observed="NOT_OBSERVED", client_computed_digest=False,
        simulated=True)
    content = _content_dict(
        plain, target_domain, capabilities, build_provenance, build_time,
        simulated_published_state, data_release, engine_candidate,
        build_code, target_domain,
        [e.entry_id for e in frozen_entries],
        [e.sha256 for e in frozen_entries],
        _plain_receipt(receipt), "KC_SNAPSHOT_SHA256", "SHA-256",
        _covered_bytes_description())
    digest = sha256_hex(canonical_json(content))
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
        simulated_validator_receipt=receipt,
    )


def validate_snapshot(snapshot: Any) -> Dict[str, Any]:
    """Recompute the content digest and compare. Accepts a Snapshot or
    a plain dict with the same fields (dict form is used by tamper
    tests). Reading a digest string is NOT computing a digest. Every
    field except snapshot_id and content_sha256 is covered: mutating
    any of them invalidates the digest."""
    if isinstance(snapshot, dict):
        get = snapshot.get
        entries = [_plain_entry(e) for e in snapshot["entries"]]
        receipt = _plain_receipt(snapshot.get("simulated_validator_receipt"))
        refs = list(snapshot.get("catalog_references") or [])
        digests = list(snapshot.get("source_digests") or [])
    else:
        get = lambda key, default=None: getattr(snapshot, key, default)
        entries = [_plain_entry(e) for e in snapshot.entries]
        receipt = _plain_receipt(snapshot.simulated_validator_receipt)
        refs = list(snapshot.catalog_references)
        digests = list(snapshot.source_digests)
    content = _content_dict(
        entries, get("target_domain"), list(get("capabilities") or []),
        get("build_provenance"), get("build_time"),
        get("simulated_published_state"), get("data_release"),
        get("engine_candidate"), get("build_code"),
        get("target_authorization_domain"), refs, digests, receipt,
        get("digest_kind"), get("algorithm"), get("covered_bytes"))
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


# ------------------------------------------------- v2.1.1 T2-1 (Issue #226):
# Fact guarantee sidecar (Freeze Delta Candidate Δ-01, C class).
#
# The sidecar is a SEPARATE dict bound to a snapshot by digest — it is
# the single T2-1 implementation choice (sidecar, not manifest
# extension), and it NEVER changes the five-field SnapshotEntry
# (entry_id, sha256, authority, rights, source_id). A guarantee proves:
# a fact_id/version maps to an authorized source_id, an authorized
# file version, a field scope, an authorization domain, and one
# immutable snapshot. The digest covers every field except the digest
# itself, so any mutation invalidates it. A missing or invalid
# guarantee never blocks Stats or Document: the consumer entry turns
# only the affected Fact layer UNKNOWN (see kc_consume).
FACT_GUARANTEE_DIGEST_KIND = "KC_FACT_GUARANTEE_SHA256"
_FACT_GUARANTEE_FIELDS = ("fact_id", "fact_version", "source_id",
                          "file_version", "field_scope",
                          "authorization_domain", "snapshot_id",
                          "source_digest")


def _guarantee_content(guarantee: Dict[str, Any]) -> Dict[str, Any]:
    return {k: guarantee.get(k) for k in _FACT_GUARANTEE_FIELDS}


def build_fact_guarantee(*, fact_id: str, fact_version: str,
                         source_id: str, file_version: str,
                         field_scope: List[str],
                         authorization_domain: str, snapshot_id: str,
                         source_digest: Any = None) -> Dict[str, Any]:
    """Build a Fact guarantee sidecar. Fail-closed at construction:
    empty identifiers, an empty field scope, or a malformed optional
    source digest are build failures, never silent defaults."""
    for name, value in (("fact_id", fact_id), ("fact_version", fact_version),
                        ("source_id", source_id),
                        ("file_version", file_version),
                        ("authorization_domain", authorization_domain),
                        ("snapshot_id", snapshot_id)):
        if not isinstance(value, str) or not value:
            raise KcSnapshotError(f"INVALID_FACT_GUARANTEE_{name.upper()}")
    if not isinstance(field_scope, list) or not field_scope \
            or any(not isinstance(f, str) or not f for f in field_scope):
        raise KcSnapshotError("INVALID_FACT_GUARANTEE_FIELD_SCOPE")
    if source_digest is not None and not _SHA256_RE.match(source_digest):
        raise KcSnapshotError("INVALID_FACT_GUARANTEE_SOURCE_DIGEST")
    content = {
        "fact_id": fact_id, "fact_version": fact_version,
        "source_id": source_id, "file_version": file_version,
        "field_scope": list(field_scope),
        "authorization_domain": authorization_domain,
        "snapshot_id": snapshot_id, "source_digest": source_digest,
    }
    digest = sha256_hex(canonical_json(content))
    return dict(content, digest=digest,
                digest_kind=FACT_GUARANTEE_DIGEST_KIND,
                algorithm="SHA-256",
                covered_bytes=(
                    "UTF-8 bytes of canonical JSON of "
                    "{fact_id, fact_version, source_id, file_version, "
                    "field_scope, authorization_domain, snapshot_id, "
                    "source_digest}"))


def validate_fact_guarantee(guarantee: Any, *,
                            snapshot: Any) -> Dict[str, Any]:
    """Validate a guarantee sidecar against a snapshot object or dict.
    Returns {"valid": bool, "reason": str|None}; it never raises on a
    malformed or tampered sidecar — a bad guarantee is simply invalid,
    and the consumer entry turns the affected Fact UNKNOWN."""
    def invalid(reason: str) -> Dict[str, Any]:
        return {"valid": False, "reason": reason}

    if not isinstance(guarantee, dict):
        return invalid("GUARANTEE_MALFORMED")
    for name in ("fact_id", "fact_version", "source_id", "file_version",
                 "authorization_domain", "snapshot_id"):
        value = guarantee.get(name)
        if not isinstance(value, str) or not value:
            return invalid("GUARANTEE_MALFORMED")
    scope = guarantee.get("field_scope")
    if not isinstance(scope, list) or not scope \
            or any(not isinstance(f, str) or not f for f in scope):
        return invalid("GUARANTEE_FIELD_SCOPE_EMPTY")
    if guarantee.get("digest_kind") != FACT_GUARANTEE_DIGEST_KIND \
            or guarantee.get("algorithm") != "SHA-256":
        return invalid("GUARANTEE_DIGEST_MISMATCH")
    try:
        expected = sha256_hex(canonical_json(_guarantee_content(guarantee)))
    except (TypeError, ValueError):
        return invalid("GUARANTEE_MALFORMED")
    if guarantee.get("digest") != expected:
        return invalid("GUARANTEE_DIGEST_MISMATCH")
    if validate_snapshot(snapshot)["valid"] is not True:
        return invalid("GUARANTEE_SNAPSHOT_INVALID")
    if isinstance(snapshot, dict):
        snap_id = snapshot.get("snapshot_id")
        snap_domain = snapshot.get("target_domain")
        entries = [_plain_entry(e) for e in snapshot["entries"]]
    else:
        snap_id = getattr(snapshot, "snapshot_id", None)
        snap_domain = getattr(snapshot, "target_domain", None)
        entries = [_plain_entry(e) for e in snapshot.entries]
    if guarantee.get("snapshot_id") != snap_id:
        return invalid("GUARANTEE_SNAPSHOT_MISMATCH")
    if guarantee.get("authorization_domain") != snap_domain:
        return invalid("GUARANTEE_AUTHORIZATION_MISMATCH")
    sources = {}
    for entry in entries:
        sources.setdefault(entry.get("source_id"), set()).add(
            entry.get("sha256"))
    if guarantee.get("source_id") not in sources:
        return invalid("GUARANTEE_SOURCE_NOT_IN_SNAPSHOT")
    source_digest = guarantee.get("source_digest")
    if source_digest is not None \
            and source_digest not in sources.get(
                guarantee.get("source_id"), set()):
        return invalid("GUARANTEE_SOURCE_DIGEST_MISMATCH")
    return {"valid": True, "reason": None}


def _jsonable(value: Any) -> Any:
    """Recursively normalize tuples to lists so canonical JSON is
    deterministic for every slice shape. Anything else must already be
    JSON-serializable; otherwise json.dumps raises and the build fails
    (fail-closed), never silently coerced."""
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def _slice_digest(slice_dict: Dict[str, Any]) -> str:
    """Content binding for one slice: the FULL slice dict (key,
    metrics, row counts, markers, player), not the key alone. Two
    slices with the same key but different payloads get different
    digests, so a changed Stats answer cannot hide behind an unchanged
    catalog snapshot ID."""
    return hashlib.sha256(
        canonical_json(_jsonable(slice_dict))).hexdigest()


def build_catalog_snapshot(*, slices: List[Dict[str, Any]],
                           expected_slice_keys: List[Tuple[Any, ...]],
                           target_domain: str, capabilities: List[str],
                           build_provenance: str, build_time: str,
                           data_release: str, engine_candidate: str,
                           build_code: str,
                           authorized_ids: Any) -> Snapshot:
    """Catalog snapshot over declared slices. Slices are filtered by
    authorized_ids FIRST (a slice whose player_uid is not authorized —
    including a slice with no player_uid at all — never contributes).
    A missing or incomplete slice is a BUILD FAILURE, not a silent
    omission; the failure message carries only the missing COUNT, never
    the key list, so it cannot leak identifying metadata."""
    allowed = set(authorized_ids)
    visible = [s for s in slices if s.get("player_uid") in allowed]
    by_key = {}
    for s in visible:
        key = tuple(s["slice_key"])
        if s.get("end_marker") and key not in by_key:
            by_key[key] = s
    missing = [key for key in expected_slice_keys
               if tuple(key) not in by_key]
    if missing:
        raise KcSnapshotError(
            f"CATALOG_SLICES_MISSING:{len(missing)}")
    entries = []
    for key in sorted(expected_slice_keys, key=str):
        sl = by_key[tuple(key)]
        entries.append({
            "entry_id": "slice:" + "|".join(str(p) for p in key),
            "sha256": _slice_digest(sl),
            "authority": "derived", "rights": "public",
            "source_id": "simulated-slice"})
    return build_snapshot(entries=entries, target_domain=target_domain,
                          capabilities=capabilities,
                          build_provenance=build_provenance,
                          build_time=build_time, data_release=data_release,
                          engine_candidate=engine_candidate,
                          build_code=build_code)
