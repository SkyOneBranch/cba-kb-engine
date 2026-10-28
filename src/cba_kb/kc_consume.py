"""Route A unified controlled consumption entry — offline/synthetic only.

This module is the ONLY consumer-trusted surface of PR-B (Issue #220).
Every consumer-facing Slice, Catalog, Snapshot, Current, Passage,
Citation, Render, and Fact/Claim answer output must be produced through
a ConsumeSession. The raw builders in kc_slices, kc_passages,
kc_snapshot, kc_current, kc_render, and kc_visibility are INTERNAL
primitives: they remain directly callable (Python has no module
privacy), but their outputs carry no entry verification record, and
the consumer render path refuses anything without one. Trust is never
established by renaming, by README wording, or by a caller-supplied
boolean: the entry VERIFIES — it recomputes digests, checks registry
membership, compares snapshot/receipt/build bindings against
session-held state, and translates raw diagnostics into uniform
consumer results.

The five non-bypassable rules, and where each is enforced here:

A. Authorization first. All visible output is generated from the
   session's authorized sets. consume_slice / consume_catalog /
   consume_object translate the raw primitives' distinguishing
   outcomes (PLAYER_NOT_AUTHORIZED raise vs EMPTY return, blocked vs
   missing view entries) into ONE uniform leak-free result per
   surface: same status, same reason, same detail, no error code,
   count, path, or diagnostic that differs between unauthorized and
   nonexistent objects. Raw error codes remain internal diagnostics
   of the primitives, never consumer surfaces.
B. Immutable commit. SimulatedCurrentStore (kc_current) binds the
   validated digest and a deep copy at validate() time; commit()
   recomputes and refuses on any mismatch, wrong target, or registry
   replacement. ConsumeSession.commit_current() drives that store
   path; it adds no second, weaker commit.
C. Citation gating. consume_citation() requires, in order: a bound
   current snapshot; a passage digest recomputed from
   {source_id, revision, locator, text} that matches the presented
   digest AND the presented text; membership in the session's
   registered authorized-passage set (by digest and full fields);
   source authorization; and a chain whose snapshot_id,
   source_digest, build_version, receipt binding, and authorization
   basis all equal session-held values. Any failure returns the same
   uniform UNKNOWN citation result — the caller learns nothing about
   which check failed. Field equality alone, or a caller boolean,
   never yields a trusted citation.
D. Fact/Claim layering. consume_answer() builds the cross-source
   answer through the entry: Stats from consume_slice, Document from
   consume_citation, Fact copied (never mutated). Every claim whose
   fields collide with Fact fields is routed through
   refuse_claim_merge() on this integrated path; the refusal is
   recorded in the claim layer and the claim is reported ONLY there.
   No claim field ever reaches the Fact/Stats layers.
E. Render gating. render_verified() accepts ONLY entry-wrapped
   artifacts: it checks the verification record's session token, then
   recomputes the content digest over the payload and compares it,
   then checks the snapshot binding against the session's current
   snapshot. Raw renderer output, unwrapped dicts, copied records on
   different content, and cross-session artifacts are all refused.
   The raw renderers are then invoked by the entry itself, after
   verification — including render_passage_doc, whose
   evidence_verified flag is set by the entry only after its own
   citation verification, never trusted from a caller.

Verification records are session capability tokens, not cryptographic
authentication: a record is bound to one session id and one payload
digest. Copying a record to other content fails the digest check;
presenting it to another session fails the session check; presenting
the same record with the same content is harmless (same content).
Within-process token security is the honest boundary of this offline
module; it proves nothing about real Drive authorization, producer
authenticity, ACL eligibility, or upstream pagination completeness —
all of which remain NOT_VERIFIED (see the README).
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from typing import Any, Dict, List, Optional

from .kc_current import KcCurrentError, SimulatedCurrentStore
from .kc_passages import verify_passage_text
from .kc_projection import (
    KcError,
    canonical_json,
    resolve_claims,
    sha256_hex,
    validate_passage,
)
from .kc_render import (
    READABILITY_STATUS,
    render_catalog,
    render_json_slice,
    render_markdown_table,
    render_passage_doc,
)
from .kc_slices import (
    KcSliceError,
    build_slice,
    build_slice_catalog,
    query_slice_metric,
)
from .kc_snapshot import validate_snapshot
from .kc_visibility import (
    NOT_FOUND_DETAIL,
    query_visible,
    refuse_claim_merge,
    target_first_view,
    treat_as_data,
)

DEFAULT_AUTHORIZATION_BASIS = "SIMULATED_TARGET_SCOPE"

UNIFORM_NOT_FOUND: Dict[str, Any] = {
    "status": "UNKNOWN",
    "grain": "UNKNOWN",
    "value": None,
    "reason": "NO_VERIFIED_RESULT_IN_CURRENT_AUTHORIZED_SCOPE",
    "detail": NOT_FOUND_DETAIL,
}

UNIFORM_CITATION_UNKNOWN: Dict[str, Any] = {
    "status": "UNKNOWN",
    "grain": "DOCUMENT",
    "reason": "CITATION_NOT_VERIFIED",
    "verbatim": None,
}


class KcConsumeError(KcError, ValueError):
    """Fail-closed consumption-entry contract violation."""


def _jsonable(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def _content_digest(payload: Dict[str, Any]) -> str:
    return sha256_hex(canonical_json(_jsonable(payload)))


class ConsumeSession:
    """One controlled consumption session: one target domain, three
    authorized sets (players, sources, objects), one bound current
    snapshot, one registered authorized-passage set, one session
    token. All consumer outputs are produced through its methods."""

    def __init__(self, *, target_domain: str,
                 authorized_players: Any = (),
                 authorized_sources: Any = (),
                 authorized_objects: Any = (),
                 authorization_basis: str = DEFAULT_AUTHORIZATION_BASIS):
        self.target_domain = target_domain
        self.authorized_players = set(authorized_players)
        self.authorized_sources = set(authorized_sources)
        self.authorized_objects = set(authorized_objects)
        self.authorization_basis = authorization_basis
        self.session_id = uuid.uuid4().hex
        self._current_snapshot: Any = None
        self._current_digest: Optional[str] = None
        self._current_build_code: Optional[str] = None
        self._receipt_binding: Optional[str] = None
        self._passages: Dict[str, Dict[str, Any]] = {}
        self._store: Optional[SimulatedCurrentStore] = None

    # ---- current snapshot binding (rule C prerequisite) ----

    def bind_current(self, snapshot: Any) -> Dict[str, Any]:
        """Bind a validated snapshot as this session's current. The
        snapshot digest is recomputed (never trusted from a string),
        and the target domain must match this session's domain."""
        if validate_snapshot(snapshot)["valid"] is not True:
            raise KcConsumeError("CURRENT_SNAPSHOT_DIGEST_INVALID")
        if isinstance(snapshot, dict):
            domain = snapshot.get("target_domain")
            digest = snapshot.get("content_sha256")
            sid = snapshot.get("snapshot_id")
            build_code = snapshot.get("build_code")
        else:
            domain = getattr(snapshot, "target_domain", None)
            digest = getattr(snapshot, "content_sha256", None)
            sid = getattr(snapshot, "snapshot_id", None)
            build_code = getattr(snapshot, "build_code", None)
        if domain != self.target_domain:
            raise KcConsumeError("CURRENT_SNAPSHOT_TARGET_MISMATCH")
        self._current_snapshot = snapshot
        self._current_digest = digest
        self._current_build_code = build_code
        self._receipt_binding = sha256_hex(canonical_json({
            "snapshot_id": sid, "content_sha256": digest,
            "build_code": build_code}))
        return self.current_binding()

    def current_binding(self) -> Dict[str, Any]:
        """The binding a caller uses to build an evidence chain for
        this session's current snapshot. A chain carrying any other
        receipt binding, build version, or authorization basis is
        refused by consume_citation()."""
        if self._current_snapshot is None:
            raise KcConsumeError("NO_CURRENT_SNAPSHOT_BOUND")
        if isinstance(self._current_snapshot, dict):
            sid = self._current_snapshot.get("snapshot_id")
        else:
            sid = getattr(self._current_snapshot, "snapshot_id", None)
        return {"snapshot_id": sid,
                "content_sha256": self._current_digest,
                "build_code": self._current_build_code,
                "receipt_binding": self._receipt_binding,
                "authorization_basis": self.authorization_basis}

    def register_passages(self, passages: List[Dict[str, Any]]) -> int:
        """Register authorized passages for membership checks. A
        passage whose source is not authorized never enters the
        registry; a passage whose digest does not validate is a
        registration failure, never a silent skip."""
        count = 0
        for passage in passages:
            if passage.get("source_id") not in self.authorized_sources:
                continue
            if validate_passage(passage)["valid"] is not True:
                raise KcConsumeError(
                    "PASSAGE_REGISTRATION_DIGEST_INVALID")
            self._passages[passage["digest"]] = copy.deepcopy(passage)
            count += 1
        return count

    # ---- verification wrapping (rule E) ----

    def _wrap(self, entry: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(self._current_snapshot, dict):
            sid = self._current_snapshot.get("snapshot_id")
        elif self._current_snapshot is not None:
            sid = getattr(self._current_snapshot, "snapshot_id", None)
        else:
            sid = None
        return {
            "payload": payload,
            "verification": {
                "verified_by": "kc_consume",
                "entry": entry,
                "session_id": self.session_id,
                "content_digest": _content_digest(payload),
                "snapshot_id": sid,
                "simulated": True,
            },
        }

    # ---- rule A: slices, catalogs, objects ----

    def consume_slice(self, records: List[Dict[str, Any]], *,
                      player_uid: str, season: str, match_type: str,
                      scope: str, provider: str, competition: str,
                      team_id: Optional[str] = None) -> Dict[str, Any]:
        """Consumer slice: unauthorized and nonexistent players return
        the identical uniform result. Dimension/data errors that are
        not existence signals (invalid scope, multiple records for
        one key) still raise — they describe the query, not an
        object's existence."""
        try:
            out = build_slice(
                records, player_uid=player_uid, season=season,
                match_type=match_type, scope=scope, team_id=team_id,
                provider=provider, competition=competition,
                authorized_ids=self.authorized_players)
        except KcSliceError as exc:
            if str(exc).startswith("PLAYER_NOT_AUTHORIZED"):
                return dict(UNIFORM_NOT_FOUND)
            raise
        if out.get("status") == "EMPTY":
            return dict(UNIFORM_NOT_FOUND)
        return self._wrap("consume_slice", out)

    def consume_catalog(self, records: List[Dict[str, Any]], *,
                        player_uid: str, expected_keys: List[Any],
                        provider: str,
                        competition: str) -> Dict[str, Any]:
        """Consumer catalog: unauthorized and nonexistent players
        return the identical uniform result. An authorized player's
        catalog may be PARTIAL (missing slices listed); a PARTIAL
        catalog is wrapped but never renders as trusted-complete."""
        if player_uid not in self.authorized_players:
            return dict(UNIFORM_NOT_FOUND)
        try:
            out = build_slice_catalog(
                records, player_uid=player_uid,
                expected_keys=expected_keys, provider=provider,
                competition=competition,
                authorized_ids=self.authorized_players)
        except KcSliceError as exc:
            if str(exc).startswith("PLAYER_NOT_AUTHORIZED"):
                return dict(UNIFORM_NOT_FOUND)
            raise
        return self._wrap("consume_catalog", out)

    def consume_object(self, objects: List[Dict[str, Any]],
                       object_id: str) -> Dict[str, Any]:
        """Consumer object lookup: the target-first view already
        returns the identical uniform result for unauthorized-existing
        and nonexistent objects; the entry preserves it unchanged."""
        view = target_first_view(objects,
                                 authorized_ids=self.authorized_objects)
        return query_visible(view, object_id)

    # ---- rule C: citation gating ----

    def consume_citation(self, *, passage: Dict[str, Any],
                         source_text: str,
                         chain: Dict[str, Any]) -> Dict[str, Any]:
        """Consumer citation. Every check must pass; any failure —
        unbound session, digest mismatch, text mismatch, unregistered
        passage, unauthorized source, or chain binding mismatch —
        returns the same uniform UNKNOWN result, revealing nothing
        about which check failed."""
        try:
            if self._current_snapshot is None:
                return dict(UNIFORM_CITATION_UNKNOWN)
            if not isinstance(passage, dict) \
                    or not isinstance(chain, dict):
                return dict(UNIFORM_CITATION_UNKNOWN)
            # Digest recomputed from fields + text equality (not a
            # trusted digest string, not a caller boolean).
            if verify_passage_text(passage, source_text) is not True:
                return dict(UNIFORM_CITATION_UNKNOWN)
            # Membership: the passage must be a registered member,
            # field-for-field, not merely digest-shaped.
            registered = self._passages.get(passage.get("digest"))
            if registered is None:
                return dict(UNIFORM_CITATION_UNKNOWN)
            for field in ("source_id", "revision", "locator", "text"):
                if registered.get(field) != passage.get(field):
                    return dict(UNIFORM_CITATION_UNKNOWN)
            # Authorization of the source itself.
            if passage.get("source_id") not in self.authorized_sources:
                return dict(UNIFORM_CITATION_UNKNOWN)
            # Chain binding against session-held values.
            binding = self.current_binding()
            if chain.get("snapshot_id") != binding["snapshot_id"]:
                return dict(UNIFORM_CITATION_UNKNOWN)
            if chain.get("source_digest") != passage.get("digest"):
                return dict(UNIFORM_CITATION_UNKNOWN)
            if chain.get("build_version") != binding["build_code"]:
                return dict(UNIFORM_CITATION_UNKNOWN)
            if chain.get("receipt_id") != binding["receipt_binding"]:
                return dict(UNIFORM_CITATION_UNKNOWN)
            if chain.get("authorization_basis") \
                    != binding["authorization_basis"]:
                return dict(UNIFORM_CITATION_UNKNOWN)
        except (KcError, KeyError, TypeError, AttributeError):
            return dict(UNIFORM_CITATION_UNKNOWN)
        payload = {
            "status": "OK", "grain": "DOCUMENT",
            "verbatim": passage["text"],
            "source_id": passage["source_id"],
            "revision": passage["revision"],
            "locator": passage["locator"],
            "digest": passage["digest"],
            "snapshot_id": binding["snapshot_id"],
            "receipt_binding": binding["receipt_binding"],
        }
        return self._wrap("consume_citation", payload)

    def consume_passage(self, passage: Dict[str, Any]) -> Dict[str, Any]:
        """Consumer passage: membership + authorization + digest
        verification, returning the passage wrapped for rendering.
        Anything unverified raises — it is never wrapped as trusted."""
        if not isinstance(passage, dict):
            raise KcConsumeError("PASSAGE_NOT_VERIFIED")
        if validate_passage(passage)["valid"] is not True:
            raise KcConsumeError("PASSAGE_NOT_VERIFIED")
        registered = self._passages.get(passage.get("digest"))
        if registered is None:
            raise KcConsumeError("PASSAGE_NOT_REGISTERED_MEMBER")
        for field in ("source_id", "revision", "locator", "text"):
            if registered.get(field) != passage.get(field):
                raise KcConsumeError("PASSAGE_NOT_REGISTERED_MEMBER")
        if passage.get("source_id") not in self.authorized_sources:
            raise KcConsumeError("PASSAGE_SOURCE_NOT_AUTHORIZED")
        return self._wrap("consume_passage", dict(passage))

    # ---- rule D: integrated Fact/Claim answer ----

    def consume_answer(self, *, records: List[Dict[str, Any]],
                       player_uid: str, season: str, match_type: str,
                       scope: str, provider: str, competition: str,
                       metric: str, fact: Dict[str, Any],
                       passage: Dict[str, Any], source_text: str,
                       chain: Dict[str, Any],
                       claims: List[Dict[str, Any]],
                       team_id: Optional[str] = None) -> Dict[str, Any]:
        """Integrated cross-source answer through the entry. Stats
        come from consume_slice, Document from consume_citation, Fact
        is deep-copied and never mutated. Claims are resolved in the
        claim layer only: every claim whose fields collide with Fact
        fields is routed through refuse_claim_merge() on this path;
        the refusal is recorded and the claim is reported only in the
        claim layer. Instruction-like passage text is carried as data
        (treat_as_data) and never executed."""
        fact_copy = copy.deepcopy(fact)
        sliced = self.consume_slice(
            records, player_uid=player_uid, season=season,
            match_type=match_type, scope=scope, team_id=team_id,
            provider=provider, competition=competition)
        if "payload" in sliced and sliced["payload"].get("status") == "OK":
            stats_layer = query_slice_metric(sliced["payload"], metric)
        else:
            stats_layer = dict(sliced)
        cited = self.consume_citation(passage=passage,
                                      source_text=source_text,
                                      chain=chain)
        if "payload" in cited:
            doc_layer = {
                "status": "OK", "grain": "DOCUMENT",
                "verbatim": cited["payload"]["verbatim"],
                "source_id": cited["payload"]["source_id"],
                "revision": cited["payload"]["revision"],
                "locator": cited["payload"]["locator"],
                "digest": cited["payload"]["digest"],
            }
        else:
            doc_layer = dict(cited)
        resolved = resolve_claims(claims)
        refusals = []
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            collision = any(
                key in fact_copy and claim.get(key) != fact_copy.get(key)
                for key in claim)
            if collision:
                try:
                    refuse_claim_merge(fact_copy, claim)
                except ValueError:
                    refusals.append({
                        "claim_id": claim.get("claim_id"),
                        "refused": True,
                        "reason": "CLAIM_MERGE_INTO_FACT_REFUSED",
                        "claim": copy.deepcopy(claim),
                    })
        claim_layer = {
            "status": "CONFLICT" if (resolved["status"] == "CONFLICT"
                                      or refusals) else resolved["status"],
            "grain": "CLAIM",
            "conflicting_claim_ids": resolved["conflicting_claim_ids"],
            "refusals": refusals,
            "claims": copy.deepcopy(claims),
        }
        handling = treat_as_data(
            passage.get("text", "") if isinstance(passage, dict) else "")
        payload = {
            "grains": {
                "CANONICAL_FACT": fact_copy,
                "STATS": stats_layer,
                "DOCUMENT": doc_layer,
            },
            "claim_layer": claim_layer,
            "causal_inference": {
                "status": "UNKNOWN",
                "reason": "CAUSAL_INFERENCE_NOT_DERIVED_FROM_CORRELATED_SOURCES",
            },
            "instruction_handling": handling,
        }
        return self._wrap("consume_answer", payload)

    # ---- rule B at the entry: store commit ----

    def attach_store(self, store: SimulatedCurrentStore) -> None:
        """Attach a SimulatedCurrentStore. commit_current() drives
        that store's own commit, which revalidates the bound snapshot
        (digest, target, content) at commit time."""
        self._store = store

    def commit_current(self) -> Dict[str, Any]:
        if self._store is None:
            raise KcConsumeError("NO_STORE_ATTACHED")
        return self._store.commit()

    # ---- rule E: consumer rendering ----

    def render_verified(self, artifact: Any, *, kind: str) -> str:
        """Render ONLY entry-verified artifacts. Checks, in order:
        artifact shape; verification record present and entry-marked;
        session token match; content digest recomputed over the
        payload; snapshot binding match when the record carries one.
        Then the raw renderer is invoked by the entry itself. Raw
        output, unwrapped dicts, forged records, cross-session
        artifacts, and PARTIAL catalogs are all refused."""
        if not isinstance(artifact, dict):
            raise KcConsumeError("ARTIFACT_NOT_ENTRY_VERIFIED")
        payload = artifact.get("payload")
        verification = artifact.get("verification")
        if not isinstance(payload, dict) \
                or not isinstance(verification, dict):
            raise KcConsumeError("ARTIFACT_NOT_ENTRY_VERIFIED")
        if verification.get("verified_by") != "kc_consume":
            raise KcConsumeError("ARTIFACT_NOT_ENTRY_VERIFIED")
        if verification.get("session_id") != self.session_id:
            raise KcConsumeError("ARTIFACT_SESSION_MISMATCH")
        if verification.get("content_digest") \
                != _content_digest(payload):
            raise KcConsumeError("ARTIFACT_DIGEST_MISMATCH")
        record_snapshot = verification.get("snapshot_id")
        if record_snapshot is not None:
            try:
                current_id = self.current_binding()["snapshot_id"]
            except KcConsumeError:
                raise KcConsumeError("ARTIFACT_SNAPSHOT_MISMATCH")
            if record_snapshot != current_id:
                raise KcConsumeError("ARTIFACT_SNAPSHOT_MISMATCH")
        if kind == "slice_json":
            return render_json_slice(payload)
        if kind == "slice_markdown":
            return render_markdown_table(payload)
        if kind == "catalog":
            return render_catalog(payload)
        if kind == "passage":
            # The flag is set by the entry here, AFTER the verification
            # above — never trusted from a caller.
            return render_passage_doc(payload, evidence_verified=True)
        if kind == "answer":
            rendered = dict(payload)
            rendered["rendering_kind"] = "json_answer"
            rendered["readability"] = READABILITY_STATUS
            rendered["end_marker"] = True
            return json.dumps(rendered, ensure_ascii=False, sort_keys=True,
                              indent=2)
        raise KcConsumeError(f"UNKNOWN_RENDERING_KIND:{kind}")
