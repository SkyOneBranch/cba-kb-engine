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
  does not invent a binding for it. A guarantee sidecar is written
  separately (guarantee.json) and re-validated by
  verify_local_projection(). When the fixture Fact declares an id
  and a version and its source is a snapshot passage, the sidecar
  is built from that Fact itself and the answer is consumed through
  the guarantee gate, so the sidecar genuinely covers the answer's
  Fact. Otherwise the answer's Fact stays UNKNOWN and
  guarantee.json holds a separately-labelled sample sidecar for
  verifier exercise only: it does NOT cover the answer's Fact, and
  the summary, manifest and index all say so explicitly.
- Phase-2 questions (real connector readability, real P0, cross-
  account ACL) are NOT answered by this module; they remain
  NOT_VERIFIED and need their own authorizations and tests.
- Provenance-binding distinction (Q2-227-05, superseded in form
  by the Human-authorized 2026-10-31 provenance repair): by
  default every passage is a synthetic fixture declaration
  (authority "derived"). A passage becomes authority "canonical"
  ONLY when it is bound to a separately supplied, validated
  canonical registry + manifest pair: the registry must pass
  canonical_registry validation (schema, authority, policy,
  lineage, manifest resolution), the passage's source must match
  a canonical route's artifact_key or file_id, the passage's
  digest must recompute from its content, and it must equal the
  manifest row's content_hash for that route. The old mechanism
  — a caller dict of {revision, digest} taken from the passage
  itself — was mutual consistency, not formal proof, and no
  longer confers anything: a passage plus a self-made binding
  pair is synthetic, never canonical. The manifest, index and
  summary report the formal/synthetic split per source, the
  registry release identity, and whether the answer Fact itself
  is formally sourced. A synthetic-only run never claims formal
  sourcing.
- Residual risk, stated plainly: in this local scope the registry
  pair itself is caller-supplied. The producer validates its
  structure and checks passage digests against its manifest, but
  it does NOT authenticate the registry release — a forged pair
  with a different release_id would still validate. The formal
  claim is therefore checkable out-of-band (compare the reported
  registry_version / registry_release_id and route against the
  published registry), not proven in-band. Authenticating the
  published registry release is a Phase-2 readback item and
  remains NOT_VERIFIED here.

The snapshot is built from slice + passage entries only. Object
visibility uses target-first filtering (target domain + APPROVED
rights) before any materialisation, so BLOCKED and other-target
objects never reach a file.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from .canonical_registry import canonical_routes, manifest_index
from .kc_consume import (
    ConsumeSession,
    _content_digest,
    identity_evidence_digest,
)
from .kc_passages import evidence_chain, validate_passage
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


def _formal_routes(formal_registry: Any) -> Any:
    """Validate a canonical registry + manifest pair and return
    (registry, canonical routes, manifest index). The pair is a
    SEPARATE artifact from the passages: it is validated whole
    (schema, authority, policy, lineage, manifest resolution) and
    read read-only — never derived from, or written from, the
    passages being bound. Any validation failure is a build
    failure, never a silent synthetic fallback."""
    if not isinstance(formal_registry, dict):
        raise KcLocalProducerError(
            "LOCAL_PROJECTION_FORMAL_REGISTRY_MALFORMED")
    registry = formal_registry.get("registry")
    manifest = formal_registry.get("manifest")
    if registry is None or manifest is None:
        raise KcLocalProducerError(
            "LOCAL_PROJECTION_FORMAL_REGISTRY_MALFORMED")
    try:
        routes = canonical_routes(registry, manifest)
        index = manifest_index(manifest)
    except ValueError as exc:
        raise KcLocalProducerError(
            "LOCAL_PROJECTION_FORMAL_REGISTRY_INVALID") from exc
    return registry, routes, index


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


def _identity_digest(target_domain: str, players: List[Dict[str, Any]],
                     aliases: List[Dict[str, Any]]) -> str:
    """Identity-evidence digest for the local sample, computed by
    the SAME rule the consumer entry enforces
    (identity_evidence_digest): the claimed source triple AND the
    actual submitted players and aliases. A digest for one
    identity payload cannot be replayed onto different content.
    It is NOT a Drive readback and proves nothing about a real
    registry or about the completeness of a formal index."""
    return identity_evidence_digest(
        source_id="fixture_canonical_registry", revision="rev-1",
        target_domain=target_domain, players=players, aliases=aliases)


def build_local_projection(*, fixture: Dict[str, Any],
                           output_dir: str,
                           target_domain: str = "fixture_target",
                           build_time: str = "2026-10-30T00:00:00+00:00",
                           data_release: str = "SIMULATED_DATA_RELEASE_1",
                           engine_candidate: str = "SIMULATED_ENGINE_CANDIDATE_1",
                           build_code: str = "SIMULATED_BUILD_CODE_1",
                           formal_registry: Any = None
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
    # Provenance-binding repair (Human-authorized, 2026-10-31):
    # formal and synthetic inputs are DISTINGUISHED, not merged,
    # and the formal binding is INDEPENDENT of the passage being
    # bound. A passage is formally bound only when a separately
    # supplied, validated canonical registry + manifest pair
    # contains a canonical route for it (artifact_key or file_id
    # equals the passage source) AND the passage's digest —
    # recomputed from its content — equals the manifest row's
    # content_hash for that route. A caller dict of {revision,
    # digest} copied from the passage itself is mutual
    # consistency, not proof, and no longer confers formal
    # status at all. A route present with a mismatching hash, a
    # passage whose digest does not recompute, or an invalid
    # registry pair is a build failure, never a silent
    # downgrade. Formally-bound entries carry
    # authority="canonical" (an existing allowed value); synthetic
    # entries keep authority="derived", and their rights are
    # fixture declarations, NOT authorization grants — the
    # manifest, index and summary all say so explicitly.
    # Residual: the registry pair's release authenticity is NOT
    # proven in-band (see module docstring); the reported
    # release identity is what an out-of-band check compares.
    formal_registry_info = None
    routes: Dict[str, Any] = {}
    row_index: Dict[str, Any] = {}
    if formal_registry is not None:
        registry, routes, row_index = _formal_routes(formal_registry)
        formal_registry_info = {
            "registry_version": registry.get("registry_version"),
            "registry_release_id": registry.get("registry_release_id"),
            "formal_routes": sorted(
                {r["artifact_key"] for r in routes.values()})}
    formal_sources: List[str] = []
    synthetic_sources: List[str] = []
    entries = [{
        "entry_id": "slice:%s|%s|REGULAR|WHOLE_SEASON"
        % (first_player, season),
        "sha256": slice_digest, "authority": "derived",
        "rights": "public", "source_id": "simulated-slice"}]
    for passage in passages:
        route = None
        for candidate in routes.values():
            if passage["source_id"] in (candidate["artifact_key"],
                                        candidate["file_id"]):
                route = candidate
                break
        if route is not None:
            if validate_passage(passage)["valid"] is not True:
                raise KcLocalProducerError(
                    "LOCAL_PROJECTION_FORMAL_PASSAGE_DIGEST_INVALID")
            row = row_index.get(route["artifact_key"])
            if row is None \
                    or row.get("content_hash") != passage["digest"]:
                raise KcLocalProducerError(
                    "LOCAL_PROJECTION_FORMAL_BINDING_MISMATCH")
            authority = "canonical"
            formal_sources.append(passage["source_id"])
        else:
            authority = "derived"
            synthetic_sources.append(passage["source_id"])
        entries.append({
            "entry_id": "passage:" + passage["source_id"],
            "sha256": passage["digest"], "authority": authority,
            "rights": "public", "source_id": passage["source_id"]})
    if formal_sources and synthetic_sources:
        provenance_mode = "mixed"
    elif formal_sources:
        provenance_mode = "formal"
    else:
        provenance_mode = "synthetic-only"
    input_provenance = {
        "mode": provenance_mode,
        "formal_sources": sorted(formal_sources),
        "synthetic_sources": sorted(synthetic_sources),
        "formal_registry": formal_registry_info}
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
                "digest": _identity_digest(target_domain, players,
                                           aliases)},
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
    # Post-freeze Q2 B-06 fix: the guarantee must be wired to the
    # ACTUAL answer Fact, never a fixed disconnected sidecar.
    # Real path: when the fixture Fact declares an id AND a version
    # AND its source is a snapshot passage, the guarantee is built
    # from the Fact's own identifiers and field keys, and the
    # answer is consumed THROUGH the (now unconditional) guarantee
    # gate, so a valid guarantee genuinely means this answer's
    # Fact passed the gate.
    # Fallback path: when the fixture Fact cannot be genuinely
    # guaranteed (no declared version, or an unbound source — as
    # with the clean-room fixture, whose source is an unbound URI),
    # the producer does NOT invent identifiers. The answer is
    # consumed without a guarantee (its Fact stays UNKNOWN, which
    # is honest), and guarantee.json holds a SEPARATE sample
    # sidecar that exists only to exercise the verifier. That
    # sample sidecar is explicitly labelled as NOT covering the
    # answer's Fact, in the summary, the manifest and the index,
    # so guarantee_valid=True can never be misread as proof that
    # the answer's Fact passed the gate.
    fact_source = None
    if isinstance(fact, dict):
        fact_source = fact.get("source_id") or fact.get("source")
    fact_passage = None
    for passage in passages:
        if passage.get("source_id") == fact_source:
            fact_passage = passage
            break
    eligible = (isinstance(fact, dict)
                and isinstance(fact.get("fact_id"), str)
                and bool(fact.get("fact_id"))
                and isinstance(fact.get("fact_version"), str)
                and bool(fact.get("fact_version"))
                and fact_passage is not None)
    if eligible:
        guarantee = build_fact_guarantee(
            fact_id=fact["fact_id"],
            fact_version=fact["fact_version"],
            source_id=fact_source,
            file_version=fact_passage["revision"],
            field_scope=sorted(fact.keys()),
            authorization_domain=target_domain,
            snapshot_id=snap.snapshot_id,
            source_digest=fact_passage["digest"])
        answer = session.consume_answer(
            records=records, player_uid=first_player, season=season,
            match_type="REGULAR", scope="WHOLE_SEASON",
            provider=PROVIDER, competition=COMPETITION,
            metric="points_per_game", fact=fact,
            passage=target_passage,
            source_text=target_passage["text"], chain=chain,
            claims=claims, fact_guarantee=guarantee)
        covers_answer_fact = True
    else:
        answer = session.consume_answer(
            records=records, player_uid=first_player, season=season,
            match_type="REGULAR", scope="WHOLE_SEASON",
            provider=PROVIDER, competition=COMPETITION,
            metric="points_per_game", fact=fact,
            passage=target_passage,
            source_text=target_passage["text"], chain=chain,
            claims=claims)
        guarantee = build_fact_guarantee(
            fact_id="sample_producer_fact", fact_version="v1",
            source_id=target_passage["source_id"],
            file_version=target_passage["revision"],
            field_scope=["player_uid", "season"],
            authorization_domain=target_domain,
            snapshot_id=snap.snapshot_id,
            source_digest=target_passage["digest"])
        covers_answer_fact = False
    guarantee_valid = validate_fact_guarantee(
        guarantee, snapshot=snap)["valid"] is True
    if not guarantee_valid:
        raise KcLocalProducerError("LOCAL_PROJECTION_GUARANTEE_INVALID")
    fact_grain = answer["payload"]["grains"]["CANONICAL_FACT"]
    answer_fact_id = fact.get("fact_id") \
        if isinstance(fact, dict) else None
    answer_fact_status = "UNKNOWN" \
        if isinstance(fact_grain, dict) \
        and fact_grain.get("status") == "UNKNOWN" else "OK"
    answer_fact_guaranteed = bool(
        covers_answer_fact and guarantee_valid
        and isinstance(fact_grain, dict)
        and fact_grain.get("fact_id") == answer_fact_id
        and answer_fact_status == "OK")
    # Q2-227-05: sidecar coverage (the guarantee genuinely covers
    # this answer's Fact) is NOT formal-source coverage. The
    # answer Fact is formally sourced only when its source
    # passage was formally bound above; a synthetic-only run
    # never reports formal sourcing, however valid its sidecar.
    answer_fact_formally_sourced = bool(
        covers_answer_fact and answer_fact_status == "OK"
        and fact_source in formal_sources)
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
        "- answer CANONICAL_FACT status: %s" % answer_fact_status,
        "- answer Fact guaranteed by its own guarantee: %s"
        % answer_fact_guaranteed,
        "- sample guarantee covers the answer Fact: %s"
        % covers_answer_fact,
        "- answer Fact formally sourced (formal source binding): %s"
        % answer_fact_formally_sourced,
        "- input provenance mode: %s (formal sources: %s; "
        "synthetic-fixture sources: %s)"
        % (provenance_mode, ",".join(sorted(formal_sources)) or "-",
           ",".join(sorted(synthetic_sources)) or "-"),
        "- formal registry release (out-of-band check identity; "
        "the producer does NOT authenticate the release itself): %s"
        % (formal_registry_info or "none"),
        "- synthetic entries' authority/rights are fixture "
        "declarations, NOT authorization grants.",
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
        "answer_fact": {"fact_id": answer_fact_id,
                        "status": answer_fact_status,
                        "guaranteed": answer_fact_guaranteed,
                        "formally_sourced":
                            answer_fact_formally_sourced},
        "guarantee": {"fact_id": guarantee["fact_id"],
                      "valid": guarantee_valid,
                      "covers_answer_fact": covers_answer_fact},
        "input_provenance": input_provenance,
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
        "answer_fact_id": answer_fact_id,
        "answer_fact_status": answer_fact_status,
        "answer_fact_guaranteed": answer_fact_guaranteed,
        "answer_fact_formally_sourced": answer_fact_formally_sourced,
        "input_provenance": input_provenance,
    }


# Artifacts a complete local materialisation must contain, and
# file categories it must contain at least one of. The verifier
# enforces this set independently of the manifest's own file list:
# a manifest that omits a required artifact must fail even when
# its own digest is self-consistent (Q2 round 1, blocker D-01).
REQUIRED_LOCAL_ARTIFACTS = ("snapshot.json", "guarantee.json",
                             "answer.json", "index.md", "catalog.md")
REQUIRED_LOCAL_CATEGORIES = ("slices/", "passages/")

# Sentinel for "JSON parsing did not produce a value". It must be
# distinct from None: a parsed JSON null IS a value (Python None)
# and must fail explicitly as a non-object, never be mistaken
# for "nothing was read" (Q2 round 2, D-02 remainder).
_NOT_PARSED = object()


def verify_local_projection(output_dir: str) -> Dict[str, Any]:
    """Re-verify a local materialisation from its own files only:
    manifest digest, every manifest-listed file digest and size,
    the mandatory artifact set and categories above, snapshot
    digest and binding, guarantee validity, answer end marker,
    and index snapshot reference. Returns {"valid", "checks",
    "failures"} and NEVER raises: any malformed structure,
    unexpected type, or unreadable file is a failure, not an
    exception (Q2 round 1, blocker D-02). A parsed JSON null
    is a value, not an absence: any parsed artifact that is
    not an object fails explicitly, and None is never used
    as both "unparsed" and "parsed null" (Q2 round 2, D-02
    remainder).

    Residual risk, stated plainly: this is a structural
    completeness and digest-integrity check against the
    directory's own manifest. It is NOT authentication against
    an actor who can rewrite every file and recompute the
    manifest digest — no signing key exists in this local
    scope — and it does NOT independently read back any
    recorded file revision from an external registry. Real
    external provenance remains NOT_VERIFIED."""
    checks: Dict[str, bool] = {}
    failures: List[str] = []

    def fail(name: str, reason: str) -> None:
        checks[name] = False
        failures.append(reason)

    manifest_path = os.path.join(output_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        return {"valid": False, "checks": {"manifest_exists": False},
                "failures": ["MANIFEST_MISSING"]}
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return {"valid": False, "checks": {"manifest_parse": False},
                "failures": ["MANIFEST_UNPARSEABLE"]}
    checks["manifest_exists"] = True
    if not isinstance(manifest, dict):
        return {"valid": False,
                "checks": {"manifest_exists": True,
                           "manifest_type": False},
                "failures": ["MANIFEST_NOT_OBJECT"]}
    try:
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
        files_field = manifest.get("files")
        listed: List[str] = []
        if not isinstance(files_field, list):
            fail("manifest_files", "MANIFEST_FILES_INVALID")
        else:
            for entry in files_field:
                if not isinstance(entry, dict):
                    fail("file_entry", "FILE_ENTRY_INVALID")
                    continue
                rel = entry.get("path")
                if not isinstance(rel, str) or not rel \
                        or os.path.isabs(rel) \
                        or ".." in rel.split("/"):
                    fail("file_entry", "FILE_PATH_UNSAFE")
                    continue
                if not isinstance(entry.get("sha256"), str) \
                        or type(entry.get("bytes")) is not int:
                    fail("file:" + rel, "FILE_ENTRY_INVALID:" + rel)
                    continue
                listed.append(rel)
                full = os.path.join(output_dir, rel)
                if not os.path.isfile(full):
                    fail("file:" + rel, "FILE_MISSING:" + rel)
                    continue
                try:
                    with open(full, "rb") as fh:
                        data = fh.read()
                except OSError:
                    fail("file:" + rel, "FILE_UNREADABLE:" + rel)
                    continue
                if sha256_hex(data) != entry.get("sha256") \
                        or len(data) != entry.get("bytes"):
                    fail("file:" + rel, "FILE_DIGEST_MISMATCH:" + rel)
                else:
                    checks["file:" + rel] = True
        for rel in REQUIRED_LOCAL_ARTIFACTS:
            if rel not in listed:
                fail("mandatory:" + rel,
                     "REQUIRED_FILE_MISSING_FROM_MANIFEST:" + rel)
        for prefix in REQUIRED_LOCAL_CATEGORIES:
            if not any(p.startswith(prefix) for p in listed):
                fail("mandatory:" + prefix,
                     "REQUIRED_CATEGORY_MISSING_FROM_MANIFEST:" + prefix)
        snap_dict = None
        snap_path = os.path.join(output_dir, "snapshot.json")
        if not os.path.isfile(snap_path):
            fail("snapshot_exists", "SNAPSHOT_MISSING")
        else:
            snap_value: Any = _NOT_PARSED
            try:
                with open(snap_path, encoding="utf-8") as fh:
                    snap_value = json.load(fh)
            except (OSError, ValueError):
                fail("snapshot_parse", "SNAPSHOT_UNPARSEABLE")
            if snap_value is _NOT_PARSED:
                pass  # parse failure already recorded above
            elif not isinstance(snap_value, dict):
                # A parsed JSON null is a value, not an absence:
                # it must fail explicitly, never be mistaken for
                # "no snapshot was read" (Q2 round 2, D-02).
                fail("snapshot_type", "SNAPSHOT_NOT_OBJECT")
            else:
                snap_dict = snap_value
        if snap_dict is not None:
            try:
                snap_ok = validate_snapshot(snap_dict)["valid"] is True
            except Exception:  # noqa: BLE001 - verifier never raises
                snap_ok = False
            if not snap_ok:
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
        if not os.path.isfile(guarantee_path):
            fail("guarantee_exists", "GUARANTEE_MISSING")
        else:
            guarantee_value: Any = _NOT_PARSED
            try:
                with open(guarantee_path, encoding="utf-8") as fh:
                    guarantee_value = json.load(fh)
            except (OSError, ValueError):
                fail("guarantee_parse", "GUARANTEE_UNPARSEABLE")
            if guarantee_value is _NOT_PARSED:
                pass  # parse failure already recorded above
            elif not isinstance(guarantee_value, dict):
                # Parsed JSON null (or any non-object) is a value,
                # not an absence: fail explicitly (Q2 round 2, D-02).
                fail("guarantee_type", "GUARANTEE_NOT_OBJECT")
            elif snap_dict is None:
                fail("guarantee",
                     "GUARANTEE_UNVERIFIABLE_WITHOUT_SNAPSHOT")
            else:
                try:
                    g_ok = validate_fact_guarantee(
                        guarantee_value,
                        snapshot=snap_dict)["valid"] is True
                except Exception:  # noqa: BLE001 - never raises
                    g_ok = False
                if not g_ok:
                    fail("guarantee", "GUARANTEE_INVALID")
                else:
                    checks["guarantee"] = True
        answer_path = os.path.join(output_dir, "answer.json")
        if not os.path.isfile(answer_path):
            fail("answer_exists", "ANSWER_MISSING")
        else:
            answer_value: Any = _NOT_PARSED
            try:
                with open(answer_path, encoding="utf-8") as fh:
                    answer_value = json.load(fh)
            except (OSError, ValueError):
                fail("answer_parse", "ANSWER_UNPARSEABLE")
            if answer_value is _NOT_PARSED:
                pass  # parse failure already recorded above
            elif not isinstance(answer_value, dict):
                # Parsed JSON null (or any non-object) is a value,
                # not an absence: fail explicitly (Q2 round 2, D-02).
                fail("answer_type", "ANSWER_NOT_OBJECT")
            elif answer_value.get("end_marker") is not True \
                    or answer_value.get("rendering_kind") != "json_answer":
                fail("answer_marker", "ANSWER_END_MARKER_MISSING")
            else:
                checks["answer_marker"] = True
        index_path = os.path.join(output_dir, "index.md")
        if not os.path.isfile(index_path):
            fail("index_exists", "INDEX_MISSING")
        else:
            try:
                with open(index_path, encoding="utf-8") as fh:
                    index_text = fh.read()
            except OSError:
                fail("index_read", "INDEX_UNREADABLE")
                index_text = None
            if index_text is not None:
                sid = manifest.get("snapshot_id")
                if not isinstance(sid, str) or sid not in index_text:
                    fail("index_snapshot", "INDEX_SNAPSHOT_MISSING")
                else:
                    checks["index_snapshot"] = True
    except Exception:  # noqa: BLE001 - verifier never raises
        fail("verifier", "VERIFIER_INTERNAL_ERROR")
    return {"valid": not failures, "checks": checks, "failures": failures}
