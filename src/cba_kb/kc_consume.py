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
   nonexistent objects. A catalog with no verifiable slice at all
   returns the same uniform result (never a wrapped catalog with a
   missing list). Passage failures of every cause return one uniform
   UNKNOWN; failure causes live only in the session's internal
   diagnostic record, never in a consumer result. Raw error codes
   remain internal diagnostics of the primitives, never consumer
   surfaces. Dimension/data errors that are not existence signals
   (invalid scope, multiple records for one key) still raise — they
   describe the query, not an object's existence.
B. Immutable commit, tied to observed state. The session owns its
   store. bind_current() establishes a binding ONLY by registering a
   deep copy, staging, validating, committing, and reading back
   through that store; a snapshot that cannot commit never becomes
   trusted current, and no caller object is retained. current_binding()
   recomputes from the store's current observed snapshot on every use
   — never from a cached digest, object, or receipt — and refuses when
   the store's current no longer matches the bound snapshot.
   Replacing the store clears any binding. SimulatedCurrentStore
   (kc_current) additionally binds validation to the active registry
   mapping identity at commit time.
C. Citation gating. consume_citation() requires, in order: a bound
   current snapshot; a passage digest recomputed from
   {source_id, revision, locator, text} that matches the presented
   digest AND the presented text; membership in the session's
   registered authorized-passage set (by digest and full fields);
   membership in the BOUND SNAPSHOT's source manifest (source_id to
   entry digests, derived fresh from the committed snapshot — session
   registration alone never confers membership); source authorization;
   and a chain whose snapshot_id, source_digest, build_version,
   receipt binding, and authorization basis all equal session-held
   values. Any failure returns the same uniform UNKNOWN citation
   result — the caller learns nothing about which check failed. Field
   equality alone, or a caller boolean, never yields a trusted
   citation.
D. Fact/Claim layering. consume_answer() builds the cross-source
   answer through the entry: Stats from consume_slice, Document from
   consume_citation, Fact copied (never mutated) ONLY if it validates
   against this answer — same player UID (and authorized), same
   season, anchored by a verified slice, bound to a committed
   snapshot, and carrying a source; otherwise the Fact layer is the
   uniform UNKNOWN, never the supplied dict. Field-level Fact
   authenticity beyond those bindings (for example team_id against a
   WHOLE_SEASON slice) is NOT proven — see the README residuals.
   Every claim whose fields collide with Fact fields is routed through
   refuse_claim_merge() on this integrated path; the refusal is
   recorded in the claim layer and the claim is reported ONLY there.
   No claim field ever reaches the Fact/Stats layers.
E. Render gating. render_verified() accepts ONLY entry-wrapped
   artifacts: it checks the verification record's session token, then
   recomputes the content digest over the payload and compares it,
   then checks the snapshot binding against the session's current
   snapshot. It additionally requires the verification record's
   producer entry to be the permitted producer for the requested kind
   (passage rendering requires a citation-verified artifact, never a
   passage-only wrapper; slice/catalog/answer kinds likewise bind to
   their producers), and passage rendering re-checks snapshot
   manifest membership at render time. Raw renderer output, unwrapped
   dicts, copied records on different content, kind mismatches, and
   cross-session artifacts are all refused. The raw renderers are
   then invoked by the entry itself, after verification — including
   render_passage_doc, whose evidence_verified flag is set by the
   entry only after its own citation verification, never trusted
   from a caller.

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

# Render kind to permitted verification producers. A verification
# record is bound to the entry that produced it; rendering a record
# under any other kind is a kind mismatch, never a rendering.
_KIND_ENTRIES: Dict[str, set] = {
    "slice_json": {"consume_slice"},
    "slice_markdown": {"consume_slice"},
    "catalog": {"consume_catalog"},
    "passage": {"consume_citation"},
    "answer": {"consume_answer"},
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
    """One controlled consumption session — one target domain, three
    authorized sets (players, sources, objects), one store-owned
    current binding, one registered authorized-passage set, one
    session token. All consumer outputs are produced through its
    methods. The session owns its store: the current binding exists
    only as a validated commit plus observed readback through that
    store, never as a retained caller object."""

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
        self._store: SimulatedCurrentStore = SimulatedCurrentStore(
            target_domain=target_domain)
        self._bound_snapshot_id: Optional[str] = None
        self._passages: Dict[str, Dict[str, Any]] = {}
        # Internal diagnostics only: failure causes behind uniform
        # consumer results. Never copied into a consumer result.
        self._last_diagnostic: Optional[Dict[str, Any]] = None

    # ---- current snapshot binding (rule C prerequisite) ----

    def bind_current(self, snapshot: Any) -> Dict[str, Any]:
        """Bind a validated, COMMITTED, READ-BACK snapshot as this
        session's current. The snapshot is deep-copied, registered in
        the session store, staged, validated, committed, and read
        back; the binding derives from that observed state. A snapshot
        that cannot commit never becomes trusted current, and no
        caller object is retained, so later caller mutation cannot
        move the binding."""
        if validate_snapshot(snapshot)["valid"] is not True:
            raise KcConsumeError("CURRENT_SNAPSHOT_DIGEST_INVALID")
        if isinstance(snapshot, dict):
            domain = snapshot.get("target_domain")
            sid = snapshot.get("snapshot_id")
        else:
            domain = getattr(snapshot, "target_domain", None)
            sid = getattr(snapshot, "snapshot_id", None)
        if domain != self.target_domain:
            raise KcConsumeError("CURRENT_SNAPSHOT_TARGET_MISMATCH")
        if not isinstance(sid, str) or not sid:
            raise KcConsumeError("CURRENT_SNAPSHOT_HAS_NO_ID")
        self._store.register(copy.deepcopy(snapshot))
        self._store.stage(sid)
        self._store.validate()
        receipt = self._store.commit()
        if receipt.get("snapshot_id") != sid:
            raise KcConsumeError("CURRENT_COMMIT_READBACK_MISMATCH")
        self._bound_snapshot_id = sid
        return self.current_binding()

    def current_binding(self) -> Dict[str, Any]:
        """The binding a caller uses to build an evidence chain for
        this session's current snapshot. Recomputed on EVERY use from
        the store's current observed snapshot: the committed snapshot
        is revalidated and the digest and receipt binding are
        recomputed fresh. No cached digest, object, or receipt is ever
        reused, so a stale store, a swapped store, or a mutated caller
        object cannot yield a binding."""
        if self._bound_snapshot_id is None:
            raise KcConsumeError("NO_CURRENT_SNAPSHOT_BOUND")
        cur = self._store.current_snapshot()
        if cur is None:
            raise KcConsumeError("CURRENT_NOT_COMMITTED")
        if isinstance(cur, dict):
            sid = cur.get("snapshot_id")
            digest = cur.get("content_sha256")
            build_code = cur.get("build_code")
        else:
            sid = getattr(cur, "snapshot_id", None)
            digest = getattr(cur, "content_sha256", None)
            build_code = getattr(cur, "build_code", None)
        if sid != self._bound_snapshot_id:
            raise KcConsumeError("CURRENT_BINDING_STALE")
        if validate_snapshot(cur)["valid"] is not True:
            raise KcConsumeError("CURRENT_SNAPSHOT_DIGEST_INVALID")
        receipt_binding = sha256_hex(canonical_json({
            "snapshot_id": sid, "content_sha256": digest,
            "build_code": build_code}))
        return {"snapshot_id": sid,
                "content_sha256": digest,
                "build_code": build_code,
                "receipt_binding": receipt_binding,
                "authorization_basis": self.authorization_basis}

    def _snapshot_manifest(self) -> Dict[str, set]:
        """Source manifest of the store's current snapshot: source_id
        to entry digests, derived fresh from the committed snapshot on
        every use. An uncommitted or absent current yields an empty
        manifest (fail-closed). Revision and locator linkage ride on
        the digest: a passage digest is computed from {source_id,
        revision, locator, text}, so a manifest digest match binds all
        four, while snapshot entries carry only source_id and digest
        columns. Callers must establish binding freshness first
        (citation via current_binding, passage via an explicit store
        check, rendering via the generic snapshot check)."""
        cur = self._store.current_snapshot()
        if cur is None:
            return {}
        entries = cur["entries"] if isinstance(cur, dict) else cur.entries
        manifest: Dict[str, set] = {}
        for entry in entries:
            if isinstance(entry, dict):
                source_id = entry.get("source_id")
                sha = entry.get("sha256")
            else:
                source_id = getattr(entry, "source_id", None)
                sha = getattr(entry, "sha256", None)
            if source_id is not None and sha is not None:
                manifest.setdefault(source_id, set()).add(sha)
        return manifest

    def register_passages(self, passages: List[Dict[str, Any]]) -> int:
        """Register authorized passages for membership checks. A
        passage whose source is not authorized never enters the
        registry; a passage whose digest does not validate is a
        registration failure, never a silent skip. Registration alone
        never confers snapshot membership: citation and passage
        rendering additionally check the snapshot-bound manifest."""
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
        return {
            "payload": payload,
            "verification": {
                "verified_by": "kc_consume",
                "entry": entry,
                "session_id": str(self.session_id),
                "content_digest": _content_digest(payload),
                "snapshot_id": self._bound_snapshot_id,
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
        catalog with at least one verifiable slice may be PARTIAL
        (missing slices listed); a PARTIAL catalog is wrapped but
        never renders as trusted-complete. A catalog with NO
        verifiable slice at all returns the uniform result, identical
        to the unauthorized result — a wrapped catalog with a missing
        list would disclose that this UID is authorized but empty."""
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
        if not any(s.get("status") == "OK"
                   for s in out.get("slices", [])):
            self._last_diagnostic = {
                "cause": "CATALOG_NO_VERIFIABLE_RECORDS",
                "entry": "consume_catalog"}
            return dict(UNIFORM_NOT_FOUND)
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
        passage, snapshot-membership miss, unauthorized source, or
        chain binding mismatch — returns the same uniform UNKNOWN
        result, revealing nothing about which check failed."""
        try:
            if self._bound_snapshot_id is None:
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
            # Snapshot membership: the passage's source and digest
            # must appear in the bound snapshot's entries. Session
            # registration alone never confers membership.
            manifest = self._snapshot_manifest()
            if passage.get("source_id") not in manifest \
                    or passage.get("digest") not in manifest.get(
                        passage.get("source_id"), set()):
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
        """Consumer passage: digest + registry membership + snapshot
        manifest membership + source authorization, against a fresh
        binding. EVERY failure returns the SAME uniform UNKNOWN result
        as every unverified citation; the failure cause is recorded
        only in the session's internal _last_diagnostic, never in the
        result. A successful result is wrapped for inspection, but it
        is NOT by itself renderable as a passage: passage rendering
        requires citation verification (see render_verified)."""
        def fail(cause: str) -> Dict[str, Any]:
            self._last_diagnostic = {"cause": cause,
                                     "entry": "consume_passage"}
            return dict(UNIFORM_CITATION_UNKNOWN)

        if not isinstance(passage, dict):
            return fail("PASSAGE_NOT_A_DICT")
        if validate_passage(passage)["valid"] is not True:
            return fail("PASSAGE_NOT_VERIFIED")
        registered = self._passages.get(passage.get("digest"))
        if registered is None:
            return fail("PASSAGE_NOT_REGISTERED_MEMBER")
        for field in ("source_id", "revision", "locator", "text"):
            if registered.get(field) != passage.get(field):
                return fail("PASSAGE_NOT_REGISTERED_MEMBER")
        if self._bound_snapshot_id is None \
                or self._store.read_current() != self._bound_snapshot_id:
            return fail("CURRENT_BINDING_STALE")
        manifest = self._snapshot_manifest()
        if passage.get("source_id") not in manifest \
                or passage.get("digest") not in manifest.get(
                    passage.get("source_id"), set()):
            return fail("PASSAGE_SNAPSHOT_MEMBERSHIP_MISS")
        if passage.get("source_id") not in self.authorized_sources:
            return fail("PASSAGE_SOURCE_NOT_AUTHORIZED")
        self._last_diagnostic = None
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
        is deep-copied and never mutated — but only if it validates
        against this answer: same player UID (and authorized), same
        season, anchored by a verified slice, bound to a committed
        snapshot, and carrying a source. Otherwise the Fact layer is
        the uniform UNKNOWN, never the supplied dict. Claims are
        resolved in the claim layer only: every claim whose fields
        collide with Fact fields is routed through
        refuse_claim_merge() on this integrated path; the refusal is
        recorded and the claim is reported only in the claim layer.
        Instruction-like passage text is carried as data
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
        # Fact authority: the supplied dict is authoritative only if
        # it binds to this verified answer. Anything else yields a
        # uniform UNKNOWN Fact layer (claim-layer refusal reporting
        # below still runs against the supplied copy, in the claim
        # layer only — it never promotes the copy to authoritative).
        fact_valid = (
            isinstance(fact, dict)
            and fact.get("player_uid") == player_uid
            and fact.get("player_uid") in self.authorized_players
            and ("season" not in fact or fact.get("season") == season)
            and "payload" in sliced
            and sliced["payload"].get("status") == "OK"
            and self._bound_snapshot_id is not None
            and bool(fact.get("source"))
        )
        fact_layer = fact_copy if fact_valid else dict(UNIFORM_NOT_FOUND)
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
                "CANONICAL_FACT": fact_layer,
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
        """Attach a SimulatedCurrentStore. Replacing the store clears
        any session binding: a binding from a previous store's
        committed state must never survive a store swap. The store's
        target domain must match this session's domain."""
        if store is None:
            raise KcConsumeError("NO_STORE_ATTACHED")
        if store.target_domain != self.target_domain:
            raise KcConsumeError("STORE_TARGET_MISMATCH")
        self._store = store
        self._bound_snapshot_id = None
        self._last_diagnostic = None

    def commit_current(self) -> Dict[str, Any]:
        """Drive the session store's commit (which revalidates the
        bound snapshot at commit time). The session binding itself is
        established by bind_current()'s commit plus readback, not by
        this method alone."""
        return self._store.commit()

    # ---- rule E: consumer rendering ----

    def render_verified(self, artifact: Any, *, kind: str) -> str:
        """Render ONLY entry-verified artifacts. Checks, in order:
        artifact shape; verification record present and entry-marked;
        session token match; content digest recomputed over the
        payload; snapshot binding match when the record carries one;
        producer entry permitted for the requested kind (a
        passage-only wrapper never renders as a passage, a slice
        never renders as a passage, and so on). Passage rendering
        additionally re-checks snapshot manifest membership at render
        time. Then the raw renderer is invoked by the entry itself.
        Raw output, unwrapped dicts, forged records, kind mismatches,
        cross-session artifacts, and PARTIAL catalogs are all
        refused."""
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
        allowed = _KIND_ENTRIES.get(kind)
        if allowed is None:
            raise KcConsumeError(f"UNKNOWN_RENDERING_KIND:{kind}")
        if verification.get("entry") not in allowed:
            raise KcConsumeError("ARTIFACT_KIND_MISMATCH")
        if kind == "slice_json":
            return render_json_slice(payload)
        if kind == "slice_markdown":
            return render_markdown_table(payload)
        if kind == "catalog":
            return render_catalog(payload)
        if kind == "passage":
            # Membership re-checked at render time: the cited source
            # and digest must be in the current snapshot's manifest.
            manifest = self._snapshot_manifest()
            if payload.get("source_id") not in manifest \
                    or payload.get("digest") not in manifest.get(
                        payload.get("source_id"), set()):
                raise KcConsumeError(
                    "ARTIFACT_SNAPSHOT_MEMBERSHIP_MISS")
            passage_shape = {
                "text": payload["verbatim"],
                "title": payload["source_id"],
                "source_id": payload["source_id"],
                "revision": payload["revision"],
                "locator": payload["locator"],
                "digest": payload["digest"],
            }
            # The flag is set by the entry here, AFTER the verification
            # above — never trusted from a caller.
            return render_passage_doc(passage_shape,
                                      evidence_verified=True)
        if kind == "answer":
            rendered = dict(payload)
            rendered["rendering_kind"] = "json_answer"
            rendered["readability"] = READABILITY_STATUS
            rendered["end_marker"] = True
            return json.dumps(rendered, ensure_ascii=False, sort_keys=True,
                              indent=2)
        raise KcConsumeError(f"UNKNOWN_RENDERING_KIND:{kind}")
