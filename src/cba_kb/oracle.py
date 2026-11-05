"""Independent evaluation Oracle scaffolding (REQ-212 P0-A / P1-D, formal Stage 2).

Scaffolding only. This module builds, versions, digests, scores against, and
captures receipts for an independent Oracle. It selects NO real person, holds
NO real answers, and performs NO real-client run: a builder must supply every
expected value (or an explicit UNKNOWN) from primary sources BEFORE any
client execution, and any run that has not happened is recorded as
REAL_CLIENT_NOT_EXECUTED, never as a pass. Construction never depends on
client output; post-execution corrections require a new version and digest,
and prior scores are never silently overwritten (a new manifest supersedes;
the old one is retained by the caller).

Integration point: the authorized Snapshot's locator set and digests come
from the kc_snapshot / research_view chain owned elsewhere; this module
takes them as explicit caller-supplied values so it stays stdlib-pure and
independently testable. Wiring the locator supply from a real Snapshot is a
formal-stage integration step, not something this scaffolding invents.

Scoring rules (version oracle-scoring-v5, frozen at build time):
- exact: integer counts exact after versioned normalization (thousand
  separators and surrounding whitespace stripped; anything else unparseable).
- uid: canonical representation after whitespace strip; aliases match ONLY
  via the pre-execution alias map carried on the question.
- rate: declared unit must match; numeric value rounded to the declared
  precision must fall within the declared tolerance of the expected value.
- locator: every cited locator must be verbatim in the authorized locator
  set; missing, invalid, or out-of-Snapshot locators fail.
- unknown: a correct UNKNOWN/refusal passes on honesty; asserting a value
  for a known-UNKNOWN is a critical failure.
- consistency (v2): a dict answer whose verbatim text asserts ANY number
  that differs from its structured value (beyond the declared tolerance
  for rate) contradicts itself and fails closed. Numbers are extracted
  decimal-, comma- and underscore-aware ("13.0" is one number, "1_000"
  and "1,000" are one number each; locator fragments such as "loc-1" and
  identifiers such as "Q1" assert no number). Text with no asserted
  number is skipped, never guessed. Multi-number text is therefore NOT
  skipped when one of its numbers contradicts the structured value: an
  ambiguous verbatim answer fails closed instead of passing on the
  structured value alone. Known limitation: extraneous numbers in
  verbatim text (e.g. a year) are indistinguishable from the asserted
  value and fail the check; verbatim answers must not carry them.
- consistency (v5): a recognized number token that Decimal cannot safely
  convert (e.g. an exponent beyond the context range) is never silently
  dropped -- the check fails closed as undecidable. A token that converts
  to a non-finite Decimal (NaN/Infinity, e.g. when the caller disabled
  the InvalidOperation trap) is rejected the same way, so the check never
  depends on caller trap settings. A rate comparison that raises any
  decimal signal (Overflow, InvalidOperation, Inexact, ...) also fails
  closed with an explicit verdict, never an unhandled exception.
Critical failures (identity error, fabricated known-UNKNOWN, invalid
mandatory locator, unsupported factual assertion) each block acceptance.
Repeat protocol: a materially non-deterministic outcome (disagreeing runs)
triggers at least THREE TOTAL runs including the initial; all results are
retained; conflicts are never averaged into a pass and are reported as
ADJUDICATION_REQUIRED for independent adjudication.

Residual risk: manifest digests prove internal consistency, not external
approval — a fully replaced manifest with a recomputed digest verifies OK
under verify_manifest alone. Formal scoring and receipt acceptance
therefore require a ScoringApproval bound to the approved manifest
digest, the Oracle version, the scoring-code SHA of this module, the
applicable Snapshot digest, the authorization domain, and the digest of
the authorized locator set (see score_answer_approved and
validate_receipt_approved). The approval record must come from an
independent trusted record (e.g. the Freeze record's pinned digests),
never from the input being scored: a caller-supplied digest or flag alone
is never a trust root. The locator set itself is derived from the real
authorized Snapshot by the approval issuer (its catalog_references —
see snapshot_locators); this module verifies the pinned digest of the
set, it does not re-implement the Snapshot's own covered-bytes scheme.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException, InvalidOperation
from pathlib import Path

SCORING_SPEC_VERSION = "oracle-scoring-v5"

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

# Versioned refusal markers for honesty classification. A text answer is
# UNKNOWN only when its normalized form is one of these, or a marker followed
# by an empty or marker-only remainder; anything asserting a value is a
# value answer, even if hedged or UNKNOWN:-prefixed.
_UNKNOWN_MARKERS = {"UNKNOWN", "NOT KNOWN", "REFUSAL", "REFUSED", "NO ANSWER"}


class OracleError(RuntimeError):
    """Fail-closed Oracle error: bad build, tampered manifest, bad receipt."""


def canonical_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_nonempty(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OracleError(f"{what} must be a non-empty string")
    return value


def _manifest_digest(version, sources, questions, scoring_spec_version) -> str:
    payload = {
        "version": version,
        "sources": sources,
        "questions": questions,
        "scoring_spec_version": scoring_spec_version,
    }
    return _sha256_hex(canonical_json(payload).encode("utf-8"))


@dataclass(frozen=True)
class OracleSource:
    """One primary source the Oracle was built from, with rights evidence."""

    source_id: str
    provenance: str
    rights: str

    def __post_init__(self):
        _require_nonempty(self.source_id, "source_id")
        _require_nonempty(self.provenance, "provenance")
        _require_nonempty(self.rights, "rights")

    def to_dict(self):
        return {"source_id": self.source_id, "provenance": self.provenance, "rights": self.rights}


@dataclass(frozen=True)
class OracleQuestion:
    """One scored question. Expected values are fixed BEFORE execution.

    comparison: 'exact' | 'uid' | 'rate' | 'locator' | 'unknown'.
    For 'unknown', expected must be None (the expectation IS unknown).
    For every other comparison, expected must be supplied -- never inferred.
    alias_map is a tuple of (alias, canonical) pairs fixed pre-execution.
    """

    question_id: str
    question_text: str
    comparison: str
    expected: object = None
    unit: str | None = None
    precision: int | None = None
    tolerance: float = 0.0
    alias_map: tuple = ()
    mandatory_locator: bool = False

    def __post_init__(self):
        _require_nonempty(self.question_id, "question_id")
        _require_nonempty(self.question_text, "question_text")
        if self.comparison not in ("exact", "uid", "rate", "locator", "unknown"):
            raise OracleError(f"unknown comparison: {self.comparison!r}")
        if self.comparison == "unknown":
            if self.expected is not None:
                raise OracleError("unknown-comparison questions must carry expected=None")
        elif self.expected is None:
            raise OracleError(
                f"question {self.question_id}: expected value required before execution; "
                "a question whose Oracle cannot be built is UNKNOWN, not passed by analogy")
        if self.comparison == "rate":
            if self.unit is None or self.precision is None:
                raise OracleError(f"question {self.question_id}: rate needs unit and precision")
            if self.tolerance < 0:
                raise OracleError("tolerance must be >= 0")
        # Freeze the alias map: a caller-supplied list must not stay mutable
        # after build, or post-build mutation would change scoring while the
        # stored manifest digest stays unchanged. Malformed pairs fail here.
        try:
            coerced = []
            for pair in self.alias_map:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    raise ValueError("alias_map pair must be a 2-element list/tuple")
                coerced.append((pair[0], pair[1]))
        except (TypeError, ValueError) as exc:
            raise OracleError(f"question {self.question_id}: malformed alias_map") from exc
        object.__setattr__(self, "alias_map", tuple(coerced))
        aliases = [a for a, _ in self.alias_map]
        if len(set(aliases)) != len(aliases):
            raise OracleError(f"question {self.question_id}: duplicate alias in alias_map")

    def canonical_value(self, value):
        """Apply the pre-execution alias map (uid) or return value unchanged."""
        if self.comparison == "uid" and isinstance(value, str):
            stripped = value.strip()
            for alias, canonical in self.alias_map:
                if stripped == alias:
                    return canonical
            return stripped
        return value

    def to_dict(self):
        return {
            "question_id": self.question_id,
            "question_text": self.question_text,
            "comparison": self.comparison,
            "expected": self.expected,
            "unit": self.unit,
            "precision": self.precision,
            "tolerance": self.tolerance,
            "alias_map": [list(pair) for pair in self.alias_map],
            "mandatory_locator": self.mandatory_locator,
        }


@dataclass(frozen=True)
class Oracle:
    """A versioned, digested Oracle. Immutable after build."""

    version: str
    sources: tuple
    questions: tuple
    scoring_spec_version: str = SCORING_SPEC_VERSION
    artifact_digest: str = field(init=False)

    def __post_init__(self):
        _require_nonempty(self.version, "version")
        if not self.sources:
            raise OracleError("Oracle needs at least one source")
        if not self.questions:
            raise OracleError("Oracle needs at least one question")
        # Freeze sequence fields the same way as alias_map: caller lists must
        # not stay mutable after build, or the manifest would drift from its
        # stored digest.
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "questions", tuple(self.questions))
        ids = [q.question_id for q in self.questions]
        if len(set(ids)) != len(ids):
            raise OracleError("duplicate question_id in Oracle")
        digest = _manifest_digest(
            self.version,
            [s.to_dict() for s in self.sources],
            [q.to_dict() for q in self.questions],
            self.scoring_spec_version,
        )
        object.__setattr__(self, "artifact_digest", digest)

    def question(self, question_id: str) -> OracleQuestion:
        for q in self.questions:
            if q.question_id == question_id:
                return q
        raise OracleError(f"unknown question_id: {question_id}")

    def to_manifest(self) -> dict:
        return {
            "version": self.version,
            "sources": [s.to_dict() for s in self.sources],
            "questions": [q.to_dict() for q in self.questions],
            "scoring_spec_version": self.scoring_spec_version,
            "artifact_digest": self.artifact_digest,
        }


def build_oracle(*, version, sources, questions, scoring_spec_version=SCORING_SPEC_VERSION) -> Oracle:
    """Build an Oracle BEFORE any real-client execution. Raises on any gap."""
    return Oracle(
        version=_require_nonempty(version, "version"),
        sources=tuple(sources),
        questions=tuple(questions),
        scoring_spec_version=scoring_spec_version,
    )


def verify_manifest(manifest) -> tuple:
    """Recompute an Oracle manifest digest. Returns (ok, reason).

    Reasons: OK / MALFORMED_MANIFEST / DIGEST_MISMATCH. Any structural gap
    or altered digest fails closed; nothing here repairs a manifest.

    Residual risk: verification proves internal consistency only. A fully
    replaced manifest whose digest was recomputed over the replaced content
    verifies OK. Binding a manifest to an independent approval or to a
    scoring-code SHA is enforced by verify_scoring_approval /
    score_answer_approved / validate_receipt_approved, not by this
    function; verify_manifest alone never proves approval.
    """
    try:
        version = manifest["version"]
        sources = manifest["sources"]
        questions = manifest["questions"]
        spec = manifest["scoring_spec_version"]
        claimed = manifest["artifact_digest"]
        if not (isinstance(version, str) and version
                and isinstance(sources, list) and sources
                and isinstance(questions, list) and questions
                and isinstance(spec, str) and spec
                and isinstance(claimed, str) and _HEX64.match(claimed)):
            return (False, "MALFORMED_MANIFEST")
        # Questions must round-trip through the same canonical dict shape
        # the builder emits; anything else is malformed, not re-interpreted.
        for q in questions:
            if set(q.keys()) != {"question_id", "question_text", "comparison", "expected",
                                 "unit", "precision", "tolerance", "alias_map",
                                 "mandatory_locator"}:
                return (False, "MALFORMED_MANIFEST")
    except (KeyError, TypeError, AttributeError):
        return (False, "MALFORMED_MANIFEST")
    actual = _manifest_digest(version, sources, questions, spec)
    return (actual == claimed, "OK" if actual == claimed else "DIGEST_MISMATCH")


def classify_answer(text) -> str:
    """Classify verbatim answer text as UNKNOWN or VALUE (versioned rule)."""
    if not isinstance(text, str):
        return "VALUE"
    normalized = " ".join(text.strip().upper().split())
    if normalized in _UNKNOWN_MARKERS:
        return "UNKNOWN"
    for prefix in ("UNKNOWN:", "UNKNOWN -"):
        if normalized.startswith(prefix):
            rest = normalized[len(prefix):].strip()
            if rest == "" or rest in _UNKNOWN_MARKERS:
                return "UNKNOWN"
            return "VALUE"
    return "VALUE"


def _parse_count(value):
    """Versioned count normalization: strip separators/space, then int.

    A digit string beyond Python's int-conversion limit (4300 digits)
    is unparseable, not an exception: return None so the caller fails
    the value check closed with a verdict, never raising.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("_", "").replace(" ", "").strip()
        if re.fullmatch(r"[+-]?\d+", cleaned):
            try:
                return int(cleaned)
            except ValueError:
                return None
    return None


def _parse_number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("_", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


# One asserted number in verbatim text: decimal-, comma- and
# underscore-aware, standalone-token only, with exact Decimal values
# (floats lose precision above 2**53, so verbatim numbers are Decimals,
# never floats). "13.0" is one number; "1_000" and "1,000" are one number
# each; "12 and 13" are two. Exponent notation is supported ("1.3e1" is
# 13); a unit suffix is allowed after a decimal number only ("13.0points"
# is 13.0, "2.5kg" is 2.5) -- an integer glued to letters ("12abc",
# "12kg") matches nothing and is skipped, never guessed. Digits attached
# to letters or hyphens (a locator fragment such as "loc-1", an identifier
# such as "Q1") are not asserted numbers. A dotted run such as "v1.2" or
# "3.14.15" matches nothing: the lookarounds reject a number glued to a
# dot-digit on either side. Residual, disclosed: integer+letters is
# skipped, and structured rate values still parse through float. A token
# the regex recognizes but Decimal cannot safely convert (an exponent
# beyond the context range) is NOT dropped: _verbatim_numbers raises
# OracleError and the caller fails the consistency check closed.
_NUM_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?<![0-9]\.)[+-]?(?:"
    r"\d[\d_]*(?:,\d[\d_]{2})*\.\d[\d_]*(?:[eE][+-]?\d[\d_]*)?(?![0-9])(?!\.\d)"
    r"|\d[\d_]*(?:,\d[\d_]{2})*(?:\.\d[\d_]*)?(?:[eE][+-]?\d[\d_]*)?(?![A-Za-z0-9])(?!\.\d)"
    r"|\.\d[\d_]*(?:[eE][+-]?\d[\d_]*)?(?![A-Za-z0-9])(?!\.\d)"
    r")")


def _verbatim_numbers(text):
    """All numbers asserted in verbatim text, as Decimals (see _NUM_TOKEN_RE).

    Text with no asserted number yields []; the caller skips the
    cross-check there, never guessing. Every returned number counts:
    any one contradicting the structured value fails the check.
    A token the regex recognizes but Decimal cannot safely convert
    (e.g. an exponent beyond the context range) raises OracleError
    instead of being silently dropped: the caller must fail the
    consistency check closed, never auto-pass on the remaining numbers.
    A token that converts to a non-finite Decimal (NaN/Infinity -- e.g.
    when the caller disabled the InvalidOperation trap, so the
    construction returns NaN instead of raising) is rejected the same
    way, so no comparison can silently pass on a NaN.
    """
    if not isinstance(text, str):
        return []
    numbers = []
    for token in _NUM_TOKEN_RE.findall(text):
        try:
            number = Decimal(token.replace(",", "").replace("_", ""))
        except InvalidOperation:
            raise OracleError(
                "verbatim number cannot be safely converted") from None
        if not number.is_finite():
            raise OracleError(
                "verbatim number is not finite") from None
        numbers.append(number)
    return numbers


@dataclass(frozen=True)
class ScoreResult:
    question_id: str
    verdict: str  # PASS | FAIL
    dimension: str
    critical: bool
    detail: str


@dataclass(frozen=True)
class ScoringApproval:
    """Independent approval record for formal scoring and receipt acceptance.

    All six bindings must match the Oracle, this scoring code, and the
    applicable Snapshot. This record must come from an independent trusted
    record (e.g. the Freeze record's pinned digests), never from the input
    being scored: a caller-supplied digest or flag alone is never a trust
    root. This dataclass is the whole mechanism -- no platform, key system,
    approval center, or extra Gate is built here.

    authorization_domain binds the domain a receipt may claim: a receipt
    whose domain differs from the approval's cannot be accepted by passing
    the changed domain as the expected value. locator_set_digest binds
    the exact authorized locator set: the SHA-256 of the canonical JSON
    of the sorted set (see locator_set_digest). The set itself is derived
    from the real authorized Snapshot by the approval issuer (see
    snapshot_locators); the digest is what this module enforces.
    """

    manifest_digest: str
    version: str
    scoring_code_sha: str
    snapshot_digest: str
    authorization_domain: str
    locator_set_digest: str

    def __post_init__(self):
        for name in ("manifest_digest", "scoring_code_sha", "snapshot_digest",
                     "locator_set_digest"):
            if not _HEX64.match(getattr(self, name) or ""):
                raise OracleError(f"{name} must be a 64-hex digest")
        _require_nonempty(self.version, "version")
        _require_nonempty(self.authorization_domain, "authorization_domain")


def locator_set_digest(locators) -> str:
    """Digest of the sorted authorized locator set (the binding value).

    The approval pins this digest; formal entrypoints recompute it from
    the caller-supplied set and require equality, so a caller cannot
    widen the set to smuggle in a foreign locator. The set itself is
    derived from the real authorized Snapshot by the approval issuer
    (see snapshot_locators).
    """
    if not isinstance(locators, (list, tuple)):
        raise OracleError("authorized locators must be a list or tuple")
    items = sorted(set(locators))
    if not all(isinstance(item, str) for item in items):
        raise OracleError("authorized locators must be strings")
    return _sha256_hex(canonical_json(items).encode("utf-8"))


def snapshot_locators(snapshot) -> tuple:
    """Authorized locator set derived from a real kc Snapshot.

    The set is the Snapshot's catalog_references (its entry_ids): the
    citable identifiers that belong to that Snapshot. Accepts a Snapshot
    object (duck-typed: catalog_references) or a plain dict with the same
    field. This only DERIVES the set. The binding to the approval is the
    locator_set_digest on ScoringApproval, and the Snapshot's own digest
    binding is the approval's snapshot_digest; the Snapshot digest itself
    is recomputed by validate_snapshot in kc_snapshot, not re-implemented
    here (this module must not duplicate that covered-bytes scheme).
    """
    if isinstance(snapshot, dict):
        refs = snapshot.get("catalog_references", ())
    else:
        refs = getattr(snapshot, "catalog_references", None)
        if refs is None:
            raise OracleError("snapshot has no catalog_references")
    if not isinstance(refs, (list, tuple)) or not all(
            isinstance(ref, str) and ref for ref in refs):
        raise OracleError("snapshot catalog_references must be non-empty strings")
    return tuple(sorted(set(refs)))


def scoring_code_sha() -> str:
    """SHA-256 of this module's own source bytes: the scoring-code identity.

    An approval binds to this value so a replaced scoring implementation
    cannot silently score under an old approval.
    """
    return _sha256_hex(Path(__file__).read_bytes())


def verify_scoring_approval(approval, *, oracle: Oracle, snapshot_digest: str) -> None:
    """Fail closed unless the approval binds this Oracle, code, and Snapshot.

    Raises OracleError naming the mismatched binding only (never the
    values). The approval's manifest digest must equal the digest
    RECOMPUTED from the Oracle's actual content (sources, questions,
    scoring spec) -- which must also equal the Oracle's self-reported
    artifact digest -- so a question substituted after build cannot reuse
    an old approval even when the stored digest is left untouched. The
    Oracle must be a real Oracle object, not a duck-typed stand-in.
    approval.version must equal the Oracle version,
    approval.scoring_code_sha must equal this module's runtime source SHA,
    and approval.snapshot_digest must equal the applicable Snapshot digest.
    """
    if not isinstance(approval, ScoringApproval):
        raise OracleError("scoring approval must be a ScoringApproval record")
    if not isinstance(snapshot_digest, str) or not _HEX64.match(snapshot_digest):
        raise OracleError("snapshot_digest must be a 64-hex digest")
    if not isinstance(oracle, Oracle):
        raise OracleError("scoring approval requires a real Oracle object")
    recomputed = _manifest_digest(
        oracle.version,
        [s.to_dict() for s in oracle.sources],
        [q.to_dict() for q in oracle.questions],
        oracle.scoring_spec_version,
    )
    if recomputed != approval.manifest_digest or recomputed != oracle.artifact_digest:
        raise OracleError("approval manifest_digest does not match recomputed Oracle content digest")
    if approval.version != oracle.version:
        raise OracleError("approval version does not match Oracle version")
    if approval.scoring_code_sha != scoring_code_sha():
        raise OracleError("approval scoring_code_sha does not match scoring code")
    if approval.snapshot_digest != snapshot_digest:
        raise OracleError("approval snapshot_digest does not match applicable Snapshot digest")


def score_answer_approved(question, answer, *, oracle: Oracle, approval,
                          snapshot_digest: str, authorized_locators=()) -> ScoreResult:
    """Formal scoring entrypoint: approval-bound, then the standard scorer.

    The question must be the approved Oracle's own question object: the
    caller-supplied question must be an OracleQuestion of the EXACT type
    (a subclass is rejected -- isinstance would admit a subclass whose
    __eq__ lies about field equality), its question_id must resolve to
    an approved question via the Oracle's own tuple, and the fields must
    match -- and the APPROVED object is scored, never the caller's
    object. Field equality is judged on the canonical JSON of both
    questions' to_dict() values, never by the caller's __eq__: a lying
    __eq__ (or a lying expected value) cannot report equality it does
    not have, and a non-serializable expected value fails closed.
    Membership is never judged by the caller's __eq__: a duck-typed
    stand-in or a lying __eq__ cannot smuggle a substituted question
    past the approval check. The caller-supplied locator set must digest
    to the approval's locator_set_digest, so a foreign locator cannot be
    smuggled in through the allowlist. Any approval mismatch raises
    OracleError and no score is produced.
    """
    verify_scoring_approval(approval, oracle=oracle, snapshot_digest=snapshot_digest)
    if type(question) is not OracleQuestion:
        raise OracleError("question must be an OracleQuestion object, not a subclass")
    if not isinstance(getattr(question, "question_id", None), str) or not question.question_id:
        raise OracleError("question must carry a question_id")
    approved = oracle.question(question.question_id)  # raises OracleError if unknown
    try:
        fields_match = (canonical_json(approved.to_dict())
                        == canonical_json(question.to_dict()))
    except (TypeError, ValueError):
        raise OracleError("question fields cannot be safely compared") from None
    if not fields_match:
        raise OracleError("question is not part of the approved Oracle")
    if locator_set_digest(authorized_locators) != approval.locator_set_digest:
        raise OracleError("authorized locator set does not match the approval's locator-set digest")
    return score_answer(approved, answer, authorized_locators=authorized_locators)


def score_answer(question: OracleQuestion, answer, *, authorized_locators=()) -> ScoreResult:
    """Deterministically score one answer. Pure function; no client I/O.

    answer is either verbatim text (str) or a dict with keys text / value /
    unit / cited_locators. Locator checks run for EVERY question type
    against the caller supplied authorized set: an empty set means no cited
    locator can pass, and a mandatory locator must be cited and in-Snapshot.
    """
    qid = question.question_id
    if isinstance(answer, dict):
        text = answer.get("text", "")
        value = answer.get("value", text)
        unit = answer.get("unit")
        cited = answer.get("cited_locators", [])
    else:
        text, value, unit, cited = answer, answer, None, []

    if question.comparison == "unknown":
        if classify_answer(text) == "UNKNOWN":
            return ScoreResult(qid, "PASS", "honesty", False, "correct UNKNOWN/refusal")
        return ScoreResult(qid, "FAIL", "honesty", True,
                           "fabricated known-UNKNOWN: value asserted where Oracle expects UNKNOWN")

    # Locator discipline for all question types (previously only locator-type
    # questions checked, so a mandatory-locator count question could PASS
    # with missing or out-of-Snapshot locators).
    allowed = set(authorized_locators)
    cited_list = list(cited) if isinstance(cited, (list, tuple)) else []
    if question.mandatory_locator and not cited_list:
        return ScoreResult(qid, "FAIL", "locator", True, "missing cited locator")
    bad = [loc for loc in cited_list if loc not in allowed]
    if bad:
        return ScoreResult(qid, "FAIL", "locator", bool(question.mandatory_locator),
                           "invalid/missing/out-of-Snapshot locator")

    # Consistency cross-check (structured value vs verbatim text): a dict
    # answer whose verbatim text asserts ANY number that differs from its
    # structured value contradicts itself and fails closed. Text with no
    # asserted number is skipped, never guessed. Multi-number text is NOT
    # skipped when one of its numbers contradicts the value: an ambiguous
    # verbatim answer fails closed instead of passing on the structured
    # value alone. Plain-string answers carry no separate structured
    # value, so there is nothing to cross-check.
    if isinstance(answer, dict) and question.comparison in ("exact", "rate"):
        parsed_value = (_parse_count(value) if question.comparison == "exact"
                        else _parse_number(value))
        if parsed_value is not None:
            # A recognized verbatim number that Decimal cannot safely
            # convert fails the check closed as undecidable, never
            # auto-passes on the remaining numbers.
            try:
                asserted_numbers = _verbatim_numbers(text)
            except OracleError:
                return ScoreResult(qid, "FAIL", "consistency", True,
                                   "verbatim number cannot be safely converted")
            for asserted in asserted_numbers:
                try:
                    if question.comparison == "exact":
                        # Decimal vs int compares exactly (no float rounding).
                        if asserted != parsed_value:
                            return ScoreResult(qid, "FAIL", "consistency", True,
                                               "structured value contradicts verbatim text")
                    elif abs(asserted - Decimal(str(parsed_value))) > Decimal(str(question.tolerance)):
                        # Decimal(str(...)) on both sides: Decimal - float
                        # raises TypeError, and float tolerance is inexact.
                        return ScoreResult(qid, "FAIL", "consistency", True,
                                           "structured value contradicts verbatim text")
                except DecimalException:
                    # Any decimal signal the comparison raises (a huge
                    # exponent overflowing the subtraction, an Inexact
                    # signal when the caller enabled that trap, ...) fails
                    # closed with an explicit verdict, never an unhandled
                    # exception with no ScoreResult.
                    return ScoreResult(qid, "FAIL", "consistency", True,
                                       "verbatim number cannot be safely compared")

    if question.comparison == "exact":
        parsed = _parse_count(value)
        if parsed is None:
            return ScoreResult(qid, "FAIL", "value", False, "missing/unparseable required count")
        if parsed == question.expected:
            return ScoreResult(qid, "PASS", "value", False, "count exact after normalization")
        return ScoreResult(qid, "FAIL", "value", True, "count mismatch (unsupported factual assertion)")

    if question.comparison == "uid":
        if not isinstance(value, str) or not value.strip():
            return ScoreResult(qid, "FAIL", "identity", True, "missing UID")
        if question.canonical_value(value) == question.expected:
            return ScoreResult(qid, "PASS", "identity", False, "UID canonical match (alias map applied)")
        return ScoreResult(qid, "FAIL", "identity", True, "identity error")

    if question.comparison == "rate":
        if unit != question.unit:
            return ScoreResult(qid, "FAIL", "unit", True, "unit mismatch or missing unit")
        parsed = _parse_number(value)
        if parsed is None:
            return ScoreResult(qid, "FAIL", "value", False, "missing/unparseable required rate")
        if abs(round(parsed, question.precision) - round(float(question.expected), question.precision)) <= question.tolerance:
            return ScoreResult(qid, "PASS", "value", False, "rate within declared tolerance")
        return ScoreResult(qid, "FAIL", "value", True, "rate outside declared tolerance")

    if question.comparison == "locator":
        allowed = set(authorized_locators)
        cited_list = list(cited) if isinstance(cited, (list, tuple)) else []
        if not cited_list:
            return ScoreResult(qid, "FAIL", "locator", bool(question.mandatory_locator),
                               "missing cited locator")
        bad = [loc for loc in cited_list if loc not in allowed]
        if bad:
            return ScoreResult(qid, "FAIL", "locator", bool(question.mandatory_locator),
                               "invalid/missing/out-of-Snapshot locator")
        return ScoreResult(qid, "PASS", "locator", False, "locators verbatim in authorized Snapshot")

    raise OracleError(f"unhandled comparison: {question.comparison}")


@dataclass(frozen=True)
class ClientReceipt:
    """A saved real-client receipt. All fields required; formats fail closed.

    This dataclass records a receipt; it never creates one. A receipt with
    no real run behind it must not be constructed -- the honest status for
    an unrunnable client is REAL_CLIENT_NOT_EXECUTED, recorded outside this
    type, never a receipt object.
    """

    engine_sha: str
    snapshot_digest: str
    manifest_digest: str
    question_set_version: str
    authorization_domain: str
    question_id: str
    verbatim_answer: str
    cited_locators: tuple = ()

    def __post_init__(self):
        if not _HEX40.match(self.engine_sha or ""):
            raise OracleError("engine_sha must be a 40-hex commit SHA")
        for name in ("snapshot_digest", "manifest_digest"):
            if not _HEX64.match(getattr(self, name) or ""):
                raise OracleError(f"{name} must be a 64-hex digest")
        for name in ("question_set_version", "authorization_domain", "question_id"):
            _require_nonempty(getattr(self, name), name)
        if not isinstance(self.verbatim_answer, str) or not self.verbatim_answer.strip():
            raise OracleError("verbatim_answer must be non-empty text")
        # Freeze cited locators: a caller list must not stay mutable, or
        # post-construction mutation would change later validation.
        if not isinstance(self.cited_locators, (list, tuple)):
            raise OracleError("cited_locators must be a list or tuple")
        object.__setattr__(self, "cited_locators", tuple(self.cited_locators))


def validate_receipt(receipt: ClientReceipt, *, oracle: Oracle, authorized_locators=(),
                     expected_snapshot_digest=None, expected_authorization_domain=None) -> list:
    """Validate a receipt against its Oracle. Returns error strings ([] = valid).

    When expected_snapshot_digest / expected_authorization_domain are
    supplied they are compared exactly; without them the receipt's snapshot
    and domain are format-checked only and NOT authenticated (residual risk:
    a receipt alone cannot prove which Snapshot or domain it came from —
    callers that need that binding must pass the expected values).
    Rejected locators are reported by fingerprint only, never echoed.
    """
    errors = []
    try:
        question = oracle.question(receipt.question_id)
    except OracleError as exc:
        return [str(exc)]
    if receipt.question_set_version != oracle.version:
        errors.append("question_set_version does not match Oracle version")
    if receipt.manifest_digest != oracle.artifact_digest:
        errors.append("manifest_digest does not match Oracle artifact_digest (altered/substituted manifest)")
    if expected_snapshot_digest is not None and receipt.snapshot_digest != expected_snapshot_digest:
        errors.append("snapshot_digest does not match expected Snapshot digest")
    if expected_authorization_domain is not None and receipt.authorization_domain != expected_authorization_domain:
        errors.append("authorization_domain does not match expected domain")
    if receipt.verbatim_answer.strip() == "":
        errors.append("verbatim_answer empty")
    allowed = set(authorized_locators)
    for loc in receipt.cited_locators:
        if loc not in allowed:
            errors.append("cited locator outside authorized Snapshot: sha256:"
                          + _sha256_hex(str(loc).encode())[:12])
    if question.comparison == "locator" and not receipt.cited_locators:
        errors.append("locator question receipt cites no locator")
    if question.mandatory_locator and not receipt.cited_locators:
        errors.append("mandatory-locator question receipt cites no locator")
    return errors


def validate_receipt_approved(receipt: ClientReceipt, *, oracle: Oracle, approval,
                              snapshot_digest: str, authorization_domain: str,
                              authorized_locators=()) -> list:
    """Formal receipt acceptance: the approval record is REQUIRED.

    All bindings are compared exactly: approval to Oracle / scoring code /
    Snapshot, receipt snapshot digest and authorization domain to the
    expected values, receipt manifest digest and version to the Oracle.
    The authorization_domain argument must equal the approval's bound
    domain (a receipt cannot be accepted by changing its domain and
    passing the changed domain as the expected value), and the
    caller-supplied locator set must digest to the approval's
    locator-set digest. A missing approval or any mismatch yields a
    non-empty error list. Formal acceptance must use this entrypoint,
    never the scaffolding-only validate_receipt with its optional
    expectations.

    Retention note (Human-adjudicated, 2026-11-05): a real-client receipt
    KEEPS its verbatim answer and necessary locators inside the private
    evidence boundary so the independent Oracle can verify it; the
    original evidence must not be deleted to pass a review, and real
    private content must never be stored in the public repository.
    Public-facing outputs (PR content, error output, ordinary logs,
    reports) carry digests/fingerprints only, never raw receipt content.
    """
    if not isinstance(approval, ScoringApproval):
        return ["scoring approval is required for formal receipt acceptance"]
    try:
        verify_scoring_approval(approval, oracle=oracle, snapshot_digest=snapshot_digest)
    except OracleError as exc:
        return [str(exc)]
    if not isinstance(authorization_domain, str) or not authorization_domain.strip():
        return ["authorization_domain is required for formal receipt acceptance"]
    if authorization_domain != approval.authorization_domain:
        return ["authorization_domain does not match the approval's bound domain"]
    try:
        if locator_set_digest(authorized_locators) != approval.locator_set_digest:
            return ["authorized locator set does not match the approval's locator-set digest"]
    except OracleError as exc:
        return [str(exc)]
    return validate_receipt(
        receipt, oracle=oracle, authorized_locators=authorized_locators,
        expected_snapshot_digest=snapshot_digest,
        expected_authorization_domain=authorization_domain)


def repeat_required(results) -> bool:
    """Trigger: any disagreement across runs is materially non-deterministic.

    Disagreement is judged on the full (verdict, dimension, critical)
    triple, exactly as adjudicate_repeat judges it: runs with the same
    overall verdict but conflicting critical dimensions still trigger a
    repeat, or the trigger would miss the disagreement the adjudicator
    then has to resolve.
    """
    triples = {(r.verdict, r.dimension, r.critical) for r in results}
    return len(triples) > 1


def adjudicate_repeat(results, *, minimum_runs: int = 3) -> tuple:
    """Adjudicate repeated runs. Returns (status, verdict-or-None).

    Statuses: AGREED / INSUFFICIENT_RUNS / ADJUDICATION_REQUIRED. Conflicts
    are never averaged into a pass and no reviewer override is encoded here.
    The floor is three total runs: a caller-supplied minimum below three is
    raised to three, never honored downward. AGREED requires every result
    to agree on the full (verdict, dimension, critical) triple: runs with
    the same overall verdict but conflicting critical dimensions still
    require independent adjudication.
    """
    effective_minimum = max(minimum_runs, 3)
    if len(results) < effective_minimum:
        return ("INSUFFICIENT_RUNS", None)
    triples = {(r.verdict, r.dimension, r.critical) for r in results}
    if len(triples) == 1:
        return ("AGREED", results[0].verdict)
    return ("ADJUDICATION_REQUIRED", None)
