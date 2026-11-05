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

Scoring rules (version oracle-scoring-v1, frozen at build time):
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
Critical failures (identity error, fabricated known-UNKNOWN, invalid
mandatory locator, unsupported factual assertion) each block acceptance.
Repeat protocol: a materially non-deterministic outcome (disagreeing runs)
triggers at least THREE TOTAL runs including the initial; all results are
retained; conflicts are never averaged into a pass and are reported as
ADJUDICATION_REQUIRED for independent adjudication.

Residual risk: manifest digests prove internal consistency, not external
approval — a fully replaced manifest with a recomputed digest verifies OK,
so approval binding lives in the execution record, not in this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

SCORING_SPEC_VERSION = "oracle-scoring-v1"

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
    scoring-code SHA is a process-level control outside this module;
    verify_manifest alone never proves approval.
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
    """Versioned count normalization: strip separators/space, then int."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace(" ", "").strip()
        if re.fullmatch(r"[+-]?\d+", cleaned):
            return int(cleaned)
    return None


def _parse_number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class ScoreResult:
    question_id: str
    verdict: str  # PASS | FAIL
    dimension: str
    critical: bool
    detail: str


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


def repeat_required(results) -> bool:
    """Trigger: any disagreement across runs is materially non-deterministic."""
    verdicts = {r.verdict for r in results}
    return len(verdicts) > 1


def adjudicate_repeat(results, *, minimum_runs: int = 3) -> tuple:
    """Adjudicate repeated runs. Returns (status, verdict-or-None).

    Statuses: AGREED / INSUFFICIENT_RUNS / ADJUDICATION_REQUIRED. Conflicts
    are never averaged into a pass and no reviewer override is encoded here.
    The floor is three total runs: a caller-supplied minimum below three is
    raised to three, never honored downward.
    """
    effective_minimum = max(minimum_runs, 3)
    if len(results) < effective_minimum:
        return ("INSUFFICIENT_RUNS", None)
    verdicts = {r.verdict for r in results}
    if len(verdicts) == 1:
        return ("AGREED", results[0].verdict)
    return ("ADJUDICATION_REQUIRED", None)
