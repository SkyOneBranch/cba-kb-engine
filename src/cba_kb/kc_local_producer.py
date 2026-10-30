"""Minimal local producer for CBA-KB v2.1.1 T2-1 (Issue #226).

LOCAL ONLY, by construction:
- Inputs are caller-supplied synthetic/authorized fixture dicts. This
  module reads no Drive, no network, no credentials, and no private
  production data, and it writes only to the caller-chosen local
  output directory. A local output directory is NOT a Drive
  publication, NOT a current-pointer change, and NOT an ACL change.
- The producer reuses the controlled entry (ConsumeSession) for every
  consumer surface: slice, catalog, identity, citation, answer. It
  never renders raw primitives. A Fact whose source is not bound to
  the snapshot stays UNKNOWN in the sample answer — the producer
  does not invent a binding for it. A valid guarantee sidecar for a
  snapshot-bound producer fact is written separately (guarantee.json)
  and re-validated by verify_local_projection().
- Phase-2 questions (real connector readability, real P0, cross-
  account ACL) are NOT answered by this module; they remain
  NOT_VERIFIED and need their own authorizations and tests.

The snapshot is built from slice + passage entries only. Object
visibility uses target-first filtering (target domain + APPROVED
rights) before any materialisation, so BLOCKED and other-target
objects never reach a file.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from .kc_consume import ConsumeSession, _content_digest
from .kc_passages import evidence_chain
from .kc_projection import canonical_json, sha256_hex
from .kc_snapshot import (
    build_fact_guarantee,
    build_snapshot,
    validate_fact_guarantee,
    validate_snapshot,
)

PROVIDER = "CBA_OFFICIAL"
COMPETITION = "CBA"


class KcLocalProducerError(RuntimeError):
    """Fail-closed local producer contract violation."""


def _snapshot_dict(snap: Any) -> Dict[str, Any]:
    receipt = snap.simulated_validator_receipt
    receipt_d = dict(receipt) if isinstance(receipt, dict) \
        else dict(vars(receipt))
    return {
        "entries": [{"entry_id": e.entry_id, "sha256": e.sha256,
                     "authority": e.authority, "rights": e.rights,
                     "source_id": e.source_id} for e in snap.entries],
        "target_domain": snap.target_domain,
        "capabilities": list(snap.capabilities),
        "build_provenance": snap.build_provenance,
        "build_time": snap.build_time,
        "simulated_published_state": snap.simulated_published_state,
        "data_release": snap.data_release,
        "engine_candidate": snap.engine_candidate,
        "build_code": snap.build_code,
        "target_authorization_domain": snap.target_authorization_domain,
        "catalog_references": list(snap.catalog_references),
        "source_digests": list(snap.source_digests),
        "simulated_validator_receipt": receipt_d,
        "content_sha256": snap.content_sha256,
        "snapshot_id": snap.snapshot_id,
        "digest_kind": snap.digest_kind,
        "algorithm": snap.algorithm,
        "covered_bytes": snap.covered_bytes,
    }


def _identity_digest(target_domain: str) -> str:
    """Synthetic identity-evidence digest for the local sample: the
    digest of the canonical JSON describing the identity source
    (source_id, revision, target domain). It binds the identity
    evidence to a version string; it is NOT a Drive readback and
    proves nothing about a real registry."""
    return sha256_hex(canonical_json({
        "source_id": "fixture_canonical_registry",
        "revision": "rev-1", "target_domain": target_domain}))


def build_local_projection(*, fixture: Dict[str, Any],
                           output_dir: str,
                           target_domain: str = "fixture_target",
                           build_time: str = "2026-10-30T00:00:00+00:00",
                           data_release: str = "SIMULATED_DATA_RELEASE_1",
                           engine_candidate: str = "SIMULATED_ENGINE_CANDIDATE_1",
                           build_code: str = "SIMULATED_BUILD_CODE_1"
                           ) -> Dict[str, Any]:
    """Build one local materialisation from a fixture dict and write
    it to output_dir. Returns a summary including the snapshot id,
    the file list, the identity result, and the guarantee validity.
    Raises KcLocalProducerError on any unbound or unverified step —
    a partial sample is never written as if complete."""
    players = fixture["players"]
    aliases = fixture.get("aliases") or []
    records = fixture["stats_records"]
    fact = fixture["fact"]
    claims = fixture.get("claims") or []
    passages = fixture["passages"]
    source_objects = fixture.get("source_objects") or []
    if not players or not records or not passages:
        raise KcLocalProducerError("LOCAL_PROJECTION_INPUT_INCOMPLETE")
    first_player = players[0]["player_uid"]
    season = records[0]["season"]
    authorized_players = {p["player_uid"] for p in players}
    authorized_sources = {p["source_id"] for p in passages}
    authorized_objects = {o["object_id"] for o in source_objects
                          if o.get("target_domain") == target_domain
                          and o.get("rights") == "APPROVED"}
    session_kw = dict(target_domain=target_domain,
                      authorized_players=authorized_players,
                      authorized_sources=authorized_sources,
                      authorized_objects=authorized_objects)
    probe = ConsumeSession(**session_kw)
    probed = probe.consume_slice(
        records, player_uid=first_player, season=season,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION)
    if "payload" not in probed or probed["payload"].get("status") != "OK":
        raise KcLocalProducerError("LOCAL_PROJECTION_SLICE_NOT_OK")
    slice_digest = _content_digest(probed["payload"])
    entries = [{
        "entry_id": "slice:%s|%s|REGULAR|WHOLE_SEASON"
        % (first_player, season),
        "sha256": slice_digest, "authority": "derived",
        "rights": "public", "source_id": "simulated-slice"}]
    for passage in passages:
        entries.append({
            "entry_id": "passage:" + passage["source_id"],
            "sha256": passage["digest"], "authority": "derived",
            "rights": "public", "source_id": passage["source_id"]})
    snap = build_snapshot(
        entries=entries, target_domain=target_domain,
        capabilities=["stats", "identity", "document", "answer"],
        build_provenance="kc_local_producer",
        build_time=build_time, data_release=data_release,
        engine_candidate=engine_candidate, build_code=build_code)
    session = ConsumeSession(**session_kw)
    session.bind_current(snap)
    session.register_passages(passages)
    sliced = session.consume_slice(
        records, player_uid=first_player, season=season,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION)
    catalog = session.consume_catalog(
        records, player_uid=first_player,
        expected_keys=[(season, "REGULAR", "WHOLE_SEASON", None)],
        provider=PROVIDER, competition=COMPETITION)
    if aliases:
        identity = session.consume_identity(
            players=players, aliases=aliases,
            query=aliases[0]["alias"], query_kind="alias",
            identity_source={
                "source_id": "fixture_canonical_registry",
                "revision": "rev-1",
                "digest": _identity_digest(target_domain)},
            all_pages_received=True)
    else:
        identity = {"status": "UNKNOWN", "grain": "IDENTITY",
                    "candidates": [], "relation": "UNDECIDED",
                    "reason": "NO_ALIAS_INPUT"}
    target_passage = None
    for passage in passages:
        if passage.get("player_uid") == first_player:
            target_passage = passage
            break
    if target_passage is None:
        target_passage = passages[0]
    binding = session.current_binding()
    chain = evidence_chain(
        receipt_id=binding["receipt_binding"],
        snapshot_id=snap.snapshot_id,
        build_version=snap.build_code,
        source_digest=target_passage["digest"],
        authorization_basis=binding["authorization_basis"])
    cited = session.consume_citation(
        passage=target_passage, source_text=target_passage["text"],
        chain=chain)
    answer = session.consume_answer(
        records=records, player_uid=first_player, season=season,
        match_type="REGULAR", scope="WHOLE_SEASON",
        provider=PROVIDER, competition=COMPETITION,
        metric="points_per_game", fact=fact, passage=target_passage,
        source_text=target_passage["text"], chain=chain, claims=claims)
    guarantee = build_fact_guarantee(
        fact_id="producer_fact", fact_version="v1",
        source_id=target_passage["source_id"],
        file_version=target_passage["revision"],
        field_scope=["player_uid", "season"],
        authorization_domain=target_domain,
        snapshot_id=snap.snapshot_id,
        source_digest=target_passage["digest"])
    guarantee_valid = validate_fact_guarantee(
        guarantee, snapshot=snap)["valid"] is True
    if not guarantee_valid:
        raise KcLocalProducerError("LOCAL_PROJECTION_GUARANTEE_INVALID")
    slice_json = session.render_verified(sliced, kind="slice_json")
    slice_md = session.render_verified(sliced, kind="slice_markdown")
    catalog_md = session.render_verified(catalog, kind="catalog")
    passage_md = session.render_verified(cited, kind="passage")
    answer_json = session.render_verified(answer, kind="answer")
    identity_payload = identity.get("payload", identity)
    identity_status = identity_payload.get("status")
    identity_candidates = identity_payload.get("candidates", [])
    files: Dict[str, str] = {
        "slices/%s.json" % first_player: slice_json,
        "slices/%s.md" % first_player: slice_md,
        "catalog.md": catalog_md,
        "passages/%s.md" % target_passage["source_id"]: passage_md,
        "answer.json": answer_json,
        "snapshot.json": json.dumps(_snapshot_dict(snap),
                                    ensure_ascii=False, sort_keys=True,
                                    indent=2),
        "guarantee.json": json.dumps(guarantee, ensure_ascii=False,
                                     sort_keys=True, indent=2),
    }
    index_lines = [
        "# Local projection (LOCAL ONLY, synthetic)",
        "",
        "This directory is a LOCAL materialisation. It is NOT a Drive",
        "publication, NOT a current-pointer change, and NOT an ACL change.",
        "",
        "- snapshot_id: %s" % snap.snapshot_id,
        "- content_sha256: %s" % snap.content_sha256,
        "- target_domain: %s" % target_domain,
        "- build_time: %s" % build_time,
        "- identity: %s %s" % (identity_status, identity_candidates),
        "- answer CANONICAL_FACT status: %s"
        % answer["payload"]["grains"]["CANONICAL_FACT"].get("status"),
        "",
        "## Files",
        "",
    ]
    for path in sorted(files):
        index_lines.append("- %s" % path)
    index_lines.append("- manifest.json")
    index_lines.append("")
    index_lines.append("Verify with verify_local_projection(output_dir).")
    files["index.md"] = "\n".join(index_lines) + "\n"
    os.makedirs(output_dir, exist_ok=True)
    manifest_files = []
    for rel_path in sorted(files):
        full = os.path.join(output_dir, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        data = files[rel_path].encode("utf-8")
        with open(full, "wb") as fh:
            fh.write(data)
        manifest_files.append({
            "path": rel_path, "sha256": sha256_hex(data),
            "bytes": len(data)})
    manifest: Dict[str, Any] = {
        "snapshot_id": snap.snapshot_id,
        "content_sha256": snap.content_sha256,
        "target_domain": target_domain,
        "build_time": build_time,
        "local_only": True,
        "drive_publication": False,
        "identity": {"status": identity_status,
                     "candidates": identity_candidates},
        "guarantee": {"fact_id": guarantee["fact_id"],
                      "valid": guarantee_valid},
        "files": manifest_files,
    }
    manifest["manifest_sha256"] = sha256_hex(
        canonical_json(manifest))
    with open(os.path.join(output_dir, "manifest.json"), "w",
              encoding="utf-8") as fh:
        fh.write(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                            indent=2))
    return {
        "output_dir": output_dir,
        "snapshot_id": snap.snapshot_id,
        "content_sha256": snap.content_sha256,
        "files": sorted(files) + ["manifest.json"],
        "identity": {"status": identity_status,
                     "candidates": identity_candidates},
        "guarantee_valid": guarantee_valid,
        "answer_fact_status":
            answer["payload"]["grains"]["CANONICAL_FACT"].get("status"),
    }


def verify_local_projection(output_dir: str) -> Dict[str, Any]:
    """Re-verify a local materialisation from its own files only:
    manifest digest, every file digest and size, snapshot digest and
    binding, guarantee validity, answer end marker, and index
    snapshot reference. Returns {"valid", "checks", "failures"}."""
    checks: Dict[str, bool] = {}
    failures: List[str] = []

    def fail(name: str, reason: str) -> None:
        checks[name] = False
        failures.append(reason)

    manifest_path = os.path.join(output_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        return {"valid": False, "checks": {"manifest_exists": False},
                "failures": ["MANIFEST_MISSING"]}
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return {"valid": False, "checks": {"manifest_parse": False},
                "failures": ["MANIFEST_UNPARSEABLE"]}
    checks["manifest_exists"] = True
    if manifest.get("local_only") is not True:
        fail("local_only", "MANIFEST_NOT_LOCAL_ONLY")
    else:
        checks["local_only"] = True
    if manifest.get("drive_publication") is not False:
        fail("drive_publication", "MANIFEST_CLAIMS_DRIVE_PUBLICATION")
    else:
        checks["drive_publication"] = True
    claimed = manifest.get("manifest_sha256")
    without = {k: v for k, v in manifest.items()
               if k != "manifest_sha256"}
    if claimed != sha256_hex(canonical_json(without)):
        fail("manifest_digest", "MANIFEST_DIGEST_MISMATCH")
    else:
        checks["manifest_digest"] = True
    for entry in manifest.get("files") or []:
        rel = entry.get("path", "")
        full = os.path.join(output_dir, rel)
        if not os.path.exists(full):
            fail("file:" + rel, "FILE_MISSING:" + rel)
            continue
        with open(full, "rb") as fh:
            data = fh.read()
        if sha256_hex(data) != entry.get("sha256") \
                or len(data) != entry.get("bytes"):
            fail("file:" + rel, "FILE_DIGEST_MISMATCH:" + rel)
        else:
            checks["file:" + rel] = True
    snap_dict = None
    snap_path = os.path.join(output_dir, "snapshot.json")
    if os.path.exists(snap_path):
        try:
            with open(snap_path, encoding="utf-8") as fh:
                snap_dict = json.load(fh)
        except (OSError, ValueError):
            fail("snapshot_parse", "SNAPSHOT_UNPARSEABLE")
    if snap_dict is not None:
        if validate_snapshot(snap_dict)["valid"] is not True:
            fail("snapshot_digest", "SNAPSHOT_DIGEST_INVALID")
        else:
            checks["snapshot_digest"] = True
        if snap_dict.get("snapshot_id") != manifest.get("snapshot_id") \
                or snap_dict.get("content_sha256") \
                != manifest.get("content_sha256"):
            fail("snapshot_binding", "SNAPSHOT_BINDING_MISMATCH")
        else:
            checks["snapshot_binding"] = True
        guarantee_path = os.path.join(output_dir, "guarantee.json")
        if os.path.exists(guarantee_path):
            try:
                with open(guarantee_path, encoding="utf-8") as fh:
                    guarantee = json.load(fh)
            except (OSError, ValueError):
                fail("guarantee_parse", "GUARANTEE_UNPARSEABLE")
                guarantee = None
            if guarantee is not None and validate_fact_guarantee(
                    guarantee, snapshot=snap_dict)["valid"] is not True:
                fail("guarantee", "GUARANTEE_INVALID")
            elif guarantee is not None:
                checks["guarantee"] = True
    answer_path = os.path.join(output_dir, "answer.json")
    if os.path.exists(answer_path):
        try:
            with open(answer_path, encoding="utf-8") as fh:
                answer = json.load(fh)
        except (OSError, ValueError):
            fail("answer_parse", "ANSWER_UNPARSEABLE")
            answer = None
        if answer is not None:
            if answer.get("end_marker") is not True \
                    or answer.get("rendering_kind") != "json_answer":
                fail("answer_marker", "ANSWER_END_MARKER_MISSING")
            else:
                checks["answer_marker"] = True
    index_path = os.path.join(output_dir, "index.md")
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as fh:
            index_text = fh.read()
        if manifest.get("snapshot_id") not in index_text:
            fail("index_snapshot", "INDEX_SNAPSHOT_MISSING")
        else:
            checks["index_snapshot"] = True
    return {"valid": not failures, "checks": checks, "failures": failures}
