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
    resolve_claims_by_event,
    resolve_identity,
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
from .kc_snapshot import validate_fact_guarantee, validate_snapshot
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


def identity_evidence_digest(*, source_id: str, revision: str,
                             target_domain: str,
                             players: List[Dict[str, Any]],
                             aliases: List[Dict[str, Any]]) -> str:
    """Recomputable binding digest for identity evidence: SHA-256
    over the canonical JSON of the claimed source triple AND the
    actual submitted identity content (players and aliases, sorted
    by their canonical bytes so list order cannot change the
    digest). A digest computed for one identity payload cannot be
    replayed onto different players, aliases, or approval content:
    any content change changes the digest. This binds the digest
    to the submitted page content; it does NOT prove the submitted
    set is the complete formal index — that completeness still
    needs a formal index input this interface does not carry, so
    E4a remains HOLD for complete-entry proof."""
    def _sorted(entries: List[Any]) -> List[Any]:
        return sorted(entries, key=lambda e: canonical_json(_jsonable(e)))

    return sha256_hex(canonical_json({
        "source_id": source_id, "revision": revision,
        "target_domain": target_domain,
        "players": _sorted(players), "aliases": _sorted(aliases)}))


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

    # ---- v2.1.1 T2-1 (Issue #226): controlled identity entry ----
    # Freeze Delta Candidate Δ-03 (B-first): reuse resolve_identity,
    # but only after target-first filtering, with pagination and
    # evidence completeness enforced fail-closed. This is NOT a new
    # public wrapper around the primitives: it is an entry method of
    # the controlled surface, and it adds no new API outside this
    # session. Counts are never included in a consumer result, so an
    # unauthorized-existing candidate and a nonexistent candidate
    # return the identical uniform UNKNOWN (F-02). The session's
    # snapshot binding is reported as a SEPARATE field and is never
    # presented as identity evidence.
    def consume_identity(self, *, players: List[Dict[str, Any]],
                         aliases: List[Dict[str, Any]], query: str,
                         query_kind: str,
                         identity_source: Any = None,
                         all_pages_received: bool = True
                         ) -> Dict[str, Any]:
        def unknown(reason: str) -> Dict[str, Any]:
            self._last_diagnostic = {"cause": reason,
                                     "entry": "consume_identity"}
            return {"status": "UNKNOWN", "grain": "IDENTITY",
                    "candidates": [], "relation": "UNDECIDED",
                    "reason": reason}

        if all_pages_received is not True:
            return unknown("PAGINATION_INCOMPLETE")
        # Post-freeze Q2 B-05 fix: the caller's boolean alone does
        # NOT prove pagination completeness. The supplied players
        # and aliases must also cover the whole authorized player
        # set; a page set that omits authorized players is
        # unproven, even when the caller claims all pages arrived.
        supplied_uids = set()
        for entry in list(players) + list(aliases):
            if isinstance(entry, dict) and entry.get("player_uid"):
                supplied_uids.add(entry.get("player_uid"))
        if not set(self.authorized_players) <= supplied_uids:
            return unknown("IDENTITY_COVERAGE_INCOMPLETE")
        if not isinstance(identity_source, dict):
            return unknown("IDENTITY_EVIDENCE_NOT_BOUND")
        source_id = identity_source.get("source_id")
        revision = identity_source.get("revision")
        digest = identity_source.get("digest")
        hexdigits = set("0123456789abcdef")
        if not (isinstance(source_id, str) and source_id
                and isinstance(revision, str) and revision
                and isinstance(digest, str) and len(digest) == 64
                and all(ch in hexdigits for ch in digest)):
            return unknown("IDENTITY_EVIDENCE_NOT_BOUND")
        # Q2-227-04 repair (Human-authorized, 2026-10-31): a
        # merely well-formed 64-hex string is NOT evidence
        # binding, and neither is a digest recomputed from the
        # claimed triple alone — the old rule let one digest cover
        # any player/alias content under the same source, revision
        # and domain. The digest must now be recomputable from the
        # triple AND the actual submitted players and aliases
        # (identity_evidence_digest, shared with the producer).
        # This proves the digest corresponds to this submitted
        # page content; it does NOT prove the submitted set is
        # the complete formal index — full index verification
        # still needs an index input this interface does not
        # carry, so E4a remains HOLD for complete-entry proof.
        try:
            expected_digest = identity_evidence_digest(
                source_id=source_id, revision=revision,
                target_domain=self.target_domain,
                players=players, aliases=aliases)
        except (TypeError, ValueError):
            return unknown("IDENTITY_EVIDENCE_DIGEST_MISMATCH")
        if digest != expected_digest:
            return unknown("IDENTITY_EVIDENCE_DIGEST_MISMATCH")
        # Target-first: filter BEFORE any resolution, summary, or
        # candidate list is derived. Unauthorized entries never reach
        # the primitive, so they cannot leak through a candidate,
        # a count, or a clarification list.
        filtered_players = [p for p in players
                            if isinstance(p, dict)
                            and p.get("player_uid")
                            in self.authorized_players]
        filtered_aliases = [a for a in aliases
                            if isinstance(a, dict)
                            and a.get("player_uid")
                            in self.authorized_players]
        resolved = resolve_identity(filtered_players, filtered_aliases,
                                    query=query, query_kind=query_kind)
        if resolved["status"] == "UNKNOWN":
            return dict(resolved)
        payload = dict(resolved)
        payload["authorization_scope"] = "AUTHORIZED_PLAYERS_ONLY"
        payload["evidence_binding"] = {
            "source_id": source_id, "revision": revision,
            "digest": digest, "kind": "KC_IDENTITY_EVIDENCE"}
        payload["session_snapshot_id"] = self._bound_snapshot_id
        payload["binding_separation"] = (
            "SESSION_SNAPSHOT_BINDING_IS_NOT_IDENTITY_EVIDENCE")
        self._last_diagnostic = None
        return self._wrap("consume_identity", payload)

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
                       team_id: Optional[str] = None,
                       fact_guarantee: Any = None
                       ) -> Dict[str, Any]:
        """Integrated cross-source answer through the entry. Stats
        come from consume_slice, Document from consume_citation, Fact
        is deep-copied and never mutated — but only if it validates
        against this answer AND against the bound snapshot: same
        player UID (and authorized), same season, anchored by a
        verified slice whose content digest is in the bound
        snapshot's manifest, bound to a committed snapshot, and
        carrying a source that is itself a source_id in the bound
        snapshot's manifest and in the session's authorized sources.
        The STATS grain likewise carries its subject (this query's
        player_uid) inside the grain, hence inside the
        digest-covered payload, so render_verified can revalidate
        the STATS subject against the current authorized set even
        when no other grain identifies the player.
        A merely truthy source proves nothing, and an "OK" slice
        computed from caller-supplied records proves nothing by
        itself. Otherwise the Fact layer is the uniform UNKNOWN,
        never the supplied dict. Claims are resolved in the claim
        layer only: every claim whose fields collide with Fact
        fields is routed through refuse_claim_merge() on this
        integrated path; the refusal is recorded and the claim is
        reported only in the claim layer. Instruction-like passage
        text is carried as data (treat_as_data) and never executed."""
        fact_copy = copy.deepcopy(fact)
        sliced = self.consume_slice(
            records, player_uid=player_uid, season=season,
            match_type=match_type, scope=scope, team_id=team_id,
            provider=provider, competition=competition)
        if "payload" in sliced and sliced["payload"].get("status") == "OK":
            stats_layer = query_slice_metric(sliced["payload"], metric)
        else:
            stats_layer = dict(sliced)
        # Directed repair 3: the STATS grain carries its subject.
        # The grain was computed for exactly this query's player_uid,
        # so that UID is recorded inside the grain itself — inside
        # the payload, hence inside the bytes _wrap's content_digest
        # covers and render_verified recomputes. Rewriting the
        # subject on a genuine artifact (or swapping it between
        # artifacts) therefore breaks the stored-digest check at
        # render time; the identity is deliberately NOT carried in
        # the verification record, where a rewrite would be
        # undetectable. Only an OK grain (one carrying a real metric
        # value) gets a subject: UNKNOWN/UNIFORM grains disclose
        # nothing and need no subject. render_verified revalidates
        # this subject against the current authorized set whenever
        # the STATS grain is OK, whatever the other grains say.
        if isinstance(stats_layer, dict) \
                and stats_layer.get("status") == "OK":
            stats_layer = dict(stats_layer)
            stats_layer["subject_player_uid"] = player_uid
        # Fact authority (directed repair 2): the supplied dict is
        # authoritative only if it binds to this verified answer AND
        # to the bound snapshot. Source membership: the source must
        # be a source_id in the bound snapshot's manifest and in the
        # session's authorized sources — a merely truthy source (a
        # URI, a fabricated string) proves nothing. Slice binding:
        # the justifying slice's content digest must be a digest in
        # the bound snapshot's manifest — an "OK" slice computed
        # from caller-supplied records proves nothing by itself.
        # Anything else yields a uniform UNKNOWN Fact layer
        # (claim-layer refusal reporting below still runs against
        # the supplied copy, in the claim layer only — it never
        # promotes the copy to authoritative).
        manifest = self._snapshot_manifest()
        manifest_digests = {sha for digests in manifest.values()
                            for sha in digests}
        fact_source = fact.get("source") if isinstance(fact, dict) \
            else None
        source_bound = isinstance(fact_source, str) \
            and fact_source in manifest \
            and fact_source in self.authorized_sources
        slice_bound = "payload" in sliced and \
            _content_digest(sliced["payload"]) in manifest_digests
        fact_valid = (
            isinstance(fact, dict)
            and fact.get("player_uid") == player_uid
            and fact.get("player_uid") in self.authorized_players
            and ("season" not in fact or fact.get("season") == season)
            and "payload" in sliced
            and sliced["payload"].get("status") == "OK"
            and self._bound_snapshot_id is not None
            and source_bound
            and slice_bound
        )
        # v2.1.1 T2-1 (Issue #226) Δ-01, strengthened by the
        # post-freeze Q2 (2026-10-31) B-02 fix and closed by the
        # Human B-01 option (a) decision (2026-10-31): the Fact
        # layer ALWAYS requires a guarantee sidecar bound to THIS
        # session's current snapshot AND to the ACTUAL output Fact:
        # same fact_id, same source identifier, a field_scope
        # covering every output Fact field, and — when the Fact's
        # source is the cited passage's source — the same file
        # version as that passage's revision. There is no opt-out:
        # the former require_fact_guarantee parameter was removed
        # because its only remaining use would have been an
        # explicit bypass of this gate, which contract §2.1 leaves
        # unapproved, and no in-repo caller needed it (producer
        # real path and v211 tests all passed True). A sidecar
        # that is merely self-consistent (valid digest,
        # snapshot-bound source) but bound to another source, a
        # partial scope, or a different file version does NOT
        # validate the Fact. A missing, tampered, or mis-bound
        # guarantee turns ONLY the Fact layer UNKNOWN; Stats and
        # Document below are computed independently and are
        # untouched (contract §2.1(5)).
        # Q2-227-01 repair (Human-authorized, 2026-10-31):
        # validate_fact_guarantee() alone is only sidecar
        # self-consistency (digest, snapshot membership, domain);
        # it is NOT authorization, and the gate below adds the
        # authoritative binding the contract §2.1 requires. Three
        # gaps the post-freeze Q2 exposed are closed here, reusing
        # only existing machinery (the bound snapshot's entry
        # digests, the session's registered passages, the cited
        # passage):
        # - source_digest is REQUIRED and must be a digest of the
        #   bound snapshot for the guarantee's source. A digest
        #   of None used to pass the validator; it can no longer
        #   authorize a Fact.
        # - fact_version is REQUIRED on the Fact and must equal
        #   the guarantee's. The Fact version field already
        #   exists in this data model (the producer's real path
        #   requires it); a version-less Fact is legacy and stays
        #   UNKNOWN per §2.1(6) — no schema change is made, and
        #   no version is invented for it.
        # - file_version must be corroborated by an actual
        #   passage object carrying the guarantee's source_digest
        #   (the cited passage, or a passage registered in this
        #   session): the passage's revision must equal the
        #   file_version. The old code checked this only when
        #   the Fact's source happened to equal the cited
        #   passage's source, so a cross-source Fact could carry
        #   any declared file_version. A passage digest binds
        #   {source_id, revision, locator, text} (validate_passage
        #   recomputes it), so a matching digest plus a matching
        #   revision is real version evidence, not a declaration.
        #   If no passage corroborates the file_version, the Fact
        #   is UNKNOWN — the gate never upgrades a declaration.
        if fact_valid:
            guarantee_ok = False
            guarantee_reason = "FACT_GUARANTEE_MISSING_OR_INVALID"
            if isinstance(fact_guarantee, dict) \
                    and isinstance(fact, dict) \
                    and self._bound_snapshot_id is not None:
                current = self._store.current_snapshot()
                if current is not None:
                    checked = validate_fact_guarantee(
                        fact_guarantee, snapshot=current)
                    if checked["valid"] is not True:
                        guarantee_reason = checked["reason"]
                    elif fact_guarantee.get("fact_id") \
                            != fact.get("fact_id"):
                        guarantee_reason = \
                            "FACT_GUARANTEE_FACT_ID_MISMATCH"
                    else:
                        fact_source = fact.get("source_id") \
                            or fact.get("source")
                        scope = fact_guarantee.get("field_scope")
                        if fact_source is None or \
                                fact_guarantee.get("source_id") \
                                != fact_source:
                            guarantee_reason = \
                                "FACT_GUARANTEE_SOURCE_MISMATCH"
                        elif not isinstance(scope, list) \
                                or not set(fact.keys()) \
                                <= set(scope):
                            guarantee_reason = \
                                "FACT_GUARANTEE_SCOPE_INCOMPLETE"
                        elif not isinstance(
                                fact.get("fact_version"), str) \
                                or not fact.get("fact_version") \
                                or fact_guarantee.get("fact_version") \
                                != fact.get("fact_version"):
                            guarantee_reason = \
                                "FACT_GUARANTEE_VERSION_MISMATCH"
                        elif not isinstance(
                                fact_guarantee.get("source_digest"),
                                str) \
                                or not fact_guarantee.get(
                                    "source_digest"):
                            guarantee_reason = \
                                "FACT_GUARANTEE_SOURCE_DIGEST_MISSING"
                        else:
                            digest = fact_guarantee.get(
                                "source_digest")
                            file_version = fact_guarantee.get(
                                "file_version")
                            corroborated = False
                            if isinstance(passage, dict) \
                                    and passage.get("digest") \
                                    == digest \
                                    and passage.get("source_id") \
                                    == fact_source \
                                    and passage.get("revision") \
                                    == file_version:
                                # The cited passage corroborates
                                # only if its own digest
                                # recomputes from {source_id,
                                # revision, locator, text}:
                                # a dict merely CLAIMING the entry
                                # digest with a different revision
                                # or text proves nothing.
                                try:
                                    cited_ok = validate_passage(
                                        passage)["valid"] is True
                                except (KeyError, TypeError,
                                        ValueError):
                                    cited_ok = False
                                corroborated = cited_ok
                            if not corroborated:
                                registered = self._passages.get(
                                    digest)
                                if isinstance(registered, dict) \
                                        and registered.get(
                                            "source_id") \
                                        == fact_source \
                                        and registered.get(
                                            "revision") \
                                        == file_version:
                                    corroborated = True
                            if not corroborated:
                                guarantee_reason = (
                                    "FACT_GUARANTEE_FILE_VERSION_"
                                    "UNCORROBORATED")
                            else:
                                guarantee_ok = True
            if not guarantee_ok:
                fact_valid = False
                self._last_diagnostic = {
                    "cause": guarantee_reason,
                    "entry": "consume_answer"}
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
        # v2.1.1 T2-1 (Issue #226) Δ-02: claims resolve through the
        # event-aware resolver (legacy behaviour preserved for
        # unlabelled claims). Δ-D mis-rejection fix: provenance
        # fields and the kind label are NOT business collisions — a
        # claim differing from the Fact only in source, source_id,
        # digest, locator, revision, claim_id, fact_id, or kind is
        # not a merge attempt and is not refused. Genuine business
        # differences (player_uid, season, team_id, ...) still are.
        # Post-freeze Q2 B-03 fix: the event resolver receives this
        # session's authorization and snapshot context, so a claim
        # association is trusted only when its source is both
        # authorized and a member of the bound snapshot (in
        # addition to the resolver's formal-field checks). A
        # standalone resolver call without context enforces the
        # formal fields only; that limitation is documented on the
        # resolver, and the consumer path always supplies context.
        # Provenance-binding repair (Human-authorized, 2026-10-31):
        # the resolver additionally receives this session's
        # passage anchors — the (source, revision, locator)
        # triples of passages that passed digest validation at
        # registration. An association whose claimed passage was
        # never registered is untrusted even when its digest
        # recomputes: a recomputed digest proves content binding,
        # not formal association identity.
        passage_anchors = {
            (p.get("source_id"), p.get("revision"), p.get("locator"))
            for p in self._passages.values()}
        resolved = resolve_claims_by_event(
            claims, authorized_sources=self.authorized_sources,
            snapshot_sources=set(manifest.keys()),
            snapshot_id=self._bound_snapshot_id,
            passage_anchors=passage_anchors)
        refusals = []
        _PROVENANCE_EXCLUDED = frozenset({
            "source", "source_id", "digest", "locator", "revision",
            "claim_id", "fact_id", "kind"})
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            collision = any(
                key in fact_copy and key not in _PROVENANCE_EXCLUDED
                and claim.get(key) != fact_copy.get(key)
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
        if resolved.get("unresolved"):
            claim_layer["unresolved"] = list(resolved["unresolved"])
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
        never renders as a passage, and so on); CURRENT authorization
        revalidated at render time from the session's sets (a
        verification record proves what was authorized at
        consumption time — it must not outlive a revocation; this
        refuses NEW renderings after authorization changed and does
        not delete already-seen session text). Passage rendering
        additionally re-checks snapshot manifest membership at render
        time. Then the raw renderer is invoked by the entry itself.
        Raw output, unwrapped dicts, forged records, kind mismatches,
        cross-session artifacts, revoked authorizations, and PARTIAL
        catalogs are all refused."""
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
        # Directed repair 2: CURRENT authorization, read from the
        # session's own sets at render time (so a discard() of a
        # source or player after verification is observed here).
        # Only digest-bound identities are revalidated: payload
        # fields are covered by the content-digest check above, so
        # they cannot be rewritten without breaking the digest,
        # while a verification-record-only identity would be
        # forgeable. Directed repair 3 extends this to the answer's
        # STATS grain, which previously had no revalidatable
        # identity: consume_answer now records the grain's subject
        # (the query's player_uid) inside the STATS grain itself,
        # hence inside the digest-covered payload. The STATS check
        # below runs FIRST in the answer branch and applies
        # whenever the STATS grain is OK, whatever the other grains
        # say, under its own code so its firing is independently
        # observable; a grain without a subject (e.g. produced
        # before this repair) fails closed.
        if kind == "passage":
            if payload.get("source_id") not in self.authorized_sources:
                raise KcConsumeError("ARTIFACT_SOURCE_NOT_AUTHORIZED")
        elif kind in ("slice_json", "slice_markdown"):
            if payload.get("player_uid") not in self.authorized_players:
                raise KcConsumeError("ARTIFACT_PLAYER_NOT_AUTHORIZED")
        elif kind == "catalog":
            for sl in payload.get("slices", []):
                if not isinstance(sl, dict) \
                        or sl.get("player_uid") not in \
                        self.authorized_players:
                    raise KcConsumeError(
                        "ARTIFACT_PLAYER_NOT_AUTHORIZED")
        elif kind == "answer":
            grains = payload.get("grains", {})
            stats_layer = grains.get("STATS", {})
            if isinstance(stats_layer, dict) \
                    and stats_layer.get("status") == "OK" \
                    and stats_layer.get("subject_player_uid") not in \
                    self.authorized_players:
                raise KcConsumeError(
                    "ARTIFACT_STATS_SUBJECT_NOT_AUTHORIZED")
            fact_layer = grains.get("CANONICAL_FACT", {})
            if isinstance(fact_layer, dict) \
                    and "player_uid" in fact_layer \
                    and fact_layer["player_uid"] not in \
                    self.authorized_players:
                raise KcConsumeError("ARTIFACT_PLAYER_NOT_AUTHORIZED")
            doc_layer = grains.get("DOCUMENT", {})
            if isinstance(doc_layer, dict) \
                    and doc_layer.get("status") == "OK" \
                    and doc_layer.get("source_id") not in \
                    self.authorized_sources:
                raise KcConsumeError("ARTIFACT_SOURCE_NOT_AUTHORIZED")
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
