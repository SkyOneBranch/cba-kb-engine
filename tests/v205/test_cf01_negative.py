"""CF-01 negative tests: per-principal ACL enforcement (T2-7, Issue #201).

GATE_CF01 evidence. SYNTHETIC grants/principals/documents only — no real
data, no real Drive identifiers.

Attack model: a principal that is NOT in a grant's ``allowed_principals``
(guest / forged / wrong-user) must receive ZERO results and learn nothing
about the protected document (no title, no file_id) through the query path.
``search_index`` takes a ``principal`` parameter; before the fix it was only
checked for non-emptiness (the CF-01 defect), so any non-empty principal saw
everything a document-level approval allowed.
"""

import json

import pytest

from cba_kb import drive_search_index as si
from cba_kb.drive_discovery import _valid_grant_record, decide_drive_rights
from cba_kb.drive_search import _rights_approved_for_search, grant_rights


def _grant(file_id, allowed_principals=("alice",), **over):
    g = {
        "approved": True,
        "approver": "op-1",
        "file_id": file_id,
        "scope": "drive:read",
        "granted_at": "2026-10-18T00:00:00Z",
        "note": "",
    }
    if allowed_principals is not None:
        g["allowed_principals"] = list(allowed_principals)
    g.update(over)
    return g


def _rec(name, file_id, grant):
    return {"name": name, "rights_status": "approved",
            "rights_grant": grant}


def _registry():
    return {
        "f1": _rec("Synthetic quarterly report", "f1", _grant("f1")),
        "f2": _rec("Synthetic roadmap draft", "f2",
                   _grant("f2", allowed_principals=("bob",))),
    }


def _index(reg):
    texts = {"f1": "synthetic revenue numbers",
             "f2": "synthetic roadmap plans"}
    return si.build_index(reg, texts=texts)


def _serialized_contains(payload, *needles):
    blob = json.dumps(payload, ensure_ascii=False)
    return [n for n in needles if n in blob]


# --- (a) unauthorized principal -> zero results, zero leakage --------------


def test_unauthorized_principal_gets_zero_results():
    reg = _registry()
    idx = _index(reg)
    out = si.search_index(idx, "synthetic", "mallory", registry=reg)
    assert out["results"] == []
    leaked = _serialized_contains(out, "f1", "f2", "quarterly", "roadmap")
    assert leaked == [], f"identifier/title leakage: {leaked}"


def test_guest_principal_bypass_closed():
    """The named CF-01 attack: a 'guest' principal must see nothing."""
    reg = _registry()
    idx = _index(reg)
    out = si.search_index(idx, "revenue", "guest", registry=reg)
    assert out["results"] == []


def test_cross_principal_isolation():
    """alice sees only f1; bob sees only f2; neither sees the other's doc."""
    reg = _registry()
    idx = _index(reg)
    alice = si.search_index(idx, "synthetic", "alice", registry=reg)
    bob = si.search_index(idx, "synthetic", "bob", registry=reg)
    assert [r["file_id"] for r in alice["results"]] == ["f1"]
    assert [r["file_id"] for r in bob["results"]] == ["f2"]
    assert _serialized_contains(alice, "f2", "roadmap") == []
    assert _serialized_contains(bob, "f1", "quarterly") == []


def test_authorized_principal_positive_control():
    """Positive control: the fix must not become deny-everyone."""
    reg = _registry()
    idx = _index(reg)
    out = si.search_index(idx, "revenue", "alice", registry=reg)
    assert [r["file_id"] for r in out["results"]] == ["f1"]


# --- strict model: unscoped grants are not valid for a principal query -----


def test_grant_without_allowed_principals_denies_principal_query():
    reg = {"f1": _rec("Synthetic quarterly report", "f1",
                      _grant("f1", allowed_principals=None))}
    idx = si.build_index(reg, texts={"f1": "synthetic revenue numbers"})
    out = si.search_index(idx, "revenue", "alice", registry=reg)
    assert out["results"] == []


def test_allowed_principals_string_not_collection_rejected():
    """A string is iterable: without the collection guard, 'ali' would
    substring-match 'alice'. The guard must reject non-collections."""
    reg = {"f1": _rec("Synthetic quarterly report", "f1",
                      _grant("f1", allowed_principals="alice"))}
    idx = si.build_index(reg, texts={"f1": "synthetic revenue numbers"})
    assert si.search_index(idx, "revenue", "alice",
                           registry=reg)["results"] == []
    assert si.search_index(idx, "revenue", "ali",
                           registry=reg)["results"] == []


# --- (b) revoked grant -> denied --------------------------------------------


def test_revoked_grant_denied_for_allowed_principal():
    reg = {"f1": _rec("Synthetic quarterly report", "f1",
                      _grant("f1", revoked=True))}
    idx = si.build_index(reg, texts={"f1": "synthetic revenue numbers"})
    out = si.search_index(idx, "revenue", "alice", registry=reg)
    assert out["results"] == []


# --- (c) direct-call bypass attempts ----------------------------------------


def test_direct_call_bypass_with_forged_principal_intercepted():
    """Bypass the CLI arg parsing and call search_index directly with a
    forged principal: still intercepted at the enforcement layer."""
    reg = _registry()
    idx = _index(reg)
    out = si.search_index(idx, "synthetic", "mallory", registry=reg)
    assert out["results"] == []
    assert _serialized_contains(out, "f1", "quarterly") == []


def test_empty_principal_rejected_not_filtered():
    with pytest.raises(ValueError, match="SEARCH_PRINCIPAL_REQUIRED"):
        si.search_index(_index(_registry()), "synthetic", "",
                        registry=_registry())


def test_non_string_principal_denied():
    reg = _registry()
    idx = _index(reg)
    out = si.search_index(idx, "synthetic", 42, registry=reg)
    assert out["results"] == []


def test_stale_index_visible_flag_cannot_leak_to_unauthorized():
    """Two-phase design: a build-time visible=True must not survive the
    query-time principal gate for an unauthorized principal."""
    reg = _registry()
    idx = _index(reg)
    assert idx["docs"]["f1"]["visible"] is True  # build-time: approved
    out = si.search_index(idx, "revenue", "mallory", registry=reg)
    assert out["results"] == []


# --- validator unit semantics -----------------------------------------------


def test_valid_grant_record_principal_semantics():
    obj = {"file_id": "f1"}
    scoped = _grant("f1", allowed_principals=["alice", "bob"])
    # document-level (no principal): unchanged pre-existing semantic
    assert _valid_grant_record(obj, scoped) is True
    assert _valid_grant_record(obj, scoped, principal=None) is True
    # principal supplied: membership enforced, fail-closed
    assert _valid_grant_record(obj, scoped, principal="alice") is True
    assert _valid_grant_record(obj, scoped, principal="mallory") is False
    assert _valid_grant_record(obj, scoped, principal="") is False
    assert _valid_grant_record(obj, scoped, principal=None) is True
    assert _valid_grant_record(obj, _grant("f1", allowed_principals=None),
                               principal="alice") is False
    assert _valid_grant_record(obj, _grant("f1", allowed_principals="alice"),
                               principal="alice") is False


def test_decide_drive_rights_principal_passthrough():
    obj = {"file_id": "f1"}
    grants = {"f1": _grant("f1", allowed_principals=["alice"])}
    assert decide_drive_rights(obj, grants=grants,
                               principal="alice")[0] == "approved"
    assert decide_drive_rights(obj, grants=grants,
                               principal="mallory")[0] == "review"
    # no principal -> document-level, backward compatible
    assert decide_drive_rights(obj, grants=grants)[0] == "approved"


def test_rights_approved_for_search_principal_passthrough():
    rec = _rec("Synthetic quarterly report", "f1",
               _grant("f1", allowed_principals=["alice"]))
    ok, _ = _rights_approved_for_search("f1", rec, principal="alice")
    assert ok is True
    ok, reason = _rights_approved_for_search("f1", rec, principal="mallory")
    assert ok is False
    assert "RIGHTS_GRANT_INVALID" in reason


# --- grant_rights records allowed_principals ---------------------------------


def _write_registry(tmp_path, file_id="f1"):
    from cba_kb.drive_search import load_registry  # noqa
    import json as _json
    reg_path = tmp_path / "reg.json"
    reg_path.write_text(_json.dumps({
        file_id: {"file_id": file_id, "name": "Synthetic doc",
                  "rights_status": "review"}}), encoding="utf-8")
    return reg_path


def test_grant_rights_records_allowed_principals(tmp_path):
    reg_path = _write_registry(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    grant_rights(reg_path, ledger, "f1", approver="op-1",
                 allowed_principals=["bob", "alice", "alice"])
    import json as _json
    stored = _json.loads(reg_path.read_text(encoding="utf-8"))["f1"]
    assert stored["rights_grant"]["allowed_principals"] == ["alice", "bob"]
    events = [ _json.loads(line) for line in
               ledger.read_text(encoding="utf-8").splitlines() ]
    assert events[-1]["grant"]["allowed_principals"] == ["alice", "bob"]


def test_grant_rights_rejects_bad_principals(tmp_path):
    reg_path = _write_registry(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    with pytest.raises(ValueError, match="PRINCIPALS"):
        grant_rights(reg_path, ledger, "f1", approver="op-1",
                     allowed_principals="alice")
    with pytest.raises(ValueError, match="PRINCIPALS"):
        grant_rights(reg_path, ledger, "f1", approver="op-1",
                     allowed_principals=["alice", ""])
    with pytest.raises(ValueError, match="PRINCIPALS"):
        grant_rights(reg_path, ledger, "f1", approver="op-1",
                     allowed_principals=[])


# === (d) search_drive_registry: principal-required + fully-excluding ACL ===
# R2 remediation for Q2 R1 blockers B1 (CRITICAL) + B2 (HIGH).
# Contract: principal is a required query parameter; every file is
# authorized for the querying principal BEFORE matching/classification.
# Files the principal cannot read are entirely invisible: no results,
# no excluded.details, no review_listing, no counts, no total_considered.

from cba_kb.drive_discovery import ACCESS_VERIFIED  # noqa: E402
from cba_kb.drive_search import (  # noqa: E402
    grant_rights as _grant_rights,
    load_registry as _load_registry,
    search_drive_registry,
)


def _rs_rec(name, file_id, grant, verify=ACCESS_VERIFIED, reason="OK"):
    rec = {
        "name": name,
        "verify_status": verify,
        "verify_reason": reason,
        "rights_status": "approved",
        "rights_grant": grant,
    }
    return rec


def _restricted_registry():
    """Reviewer's B1/B2 reproducer registry: file visible only to alice."""
    return {
        "restricted-file-001": _rs_rec(
            "Confidential acquisition plan", "restricted-file-001",
            {"approved": True, "approver": "operator",
             "file_id": "restricted-file-001", "scope": "drive:read",
             "allowed_principals": ["alice"]}),
    }


def test_regsearch_b1_reviewer_reproducer_zero_leak():
    """Reviewer's exact B1 construction: guest must learn NOTHING.
    The serialized response for a guest must equal the response over an
    empty registry for the same query (no file_id, no name, no counts)."""
    reg = _restricted_registry()
    out_guest = search_drive_registry(reg, "Confidential", principal="guest")
    out_empty = search_drive_registry({}, "Confidential", principal="guest")
    assert out_guest["results"] == []
    assert out_guest == out_empty, "unauthorized file shifted the response"
    leaked = _serialized_contains(out_guest, "restricted-file-001",
                                  "Confidential acquisition plan")
    assert leaked == [], f"B1 leakage: {leaked}"


def test_regsearch_principal_required():
    reg = _restricted_registry()
    with pytest.raises(ValueError, match="SEARCH_PRINCIPAL_REQUIRED"):
        search_drive_registry(reg, "Confidential")
    with pytest.raises(ValueError, match="SEARCH_PRINCIPAL_REQUIRED"):
        search_drive_registry(reg, "Confidential", principal=None)
    with pytest.raises(ValueError, match="SEARCH_PRINCIPAL_REQUIRED"):
        search_drive_registry(reg, "Confidential", principal="")
    with pytest.raises(ValueError, match="SEARCH_PRINCIPAL_REQUIRED"):
        search_drive_registry(reg, "Confidential", principal="   ")
    with pytest.raises(ValueError, match="SEARCH_PRINCIPAL_REQUIRED"):
        search_drive_registry(reg, "Confidential", principal=42)


def test_regsearch_unauthorized_does_not_shift_counts():
    """Unauthorized files must not affect any counter (total or category)."""
    reg = {
        "f1": _rs_rec("Falcons report alice", "f1",
                      _grant("f1", allowed_principals=("alice",))),
        "f2": _rs_rec("Falcons report bob", "f2",
                      _grant("f2", allowed_principals=("bob",))),
    }
    out = search_drive_registry(reg, "Falcons", principal="alice")
    assert [r["file_id"] for r in out["results"]] == ["f1"]
    assert out["total_considered"] == 1
    assert out["excluded"]["no_match"] == 0
    out_m = search_drive_registry(reg, "Falcons", principal="mallory")
    assert out_m == search_drive_registry({}, "Falcons", principal="mallory")
    out_b = search_drive_registry(reg, "Falcons", principal="bob")
    assert [r["file_id"] for r in out_b["results"]] == ["f2"]


def test_regsearch_verify_failed_no_leak_for_unauthorized():
    """VERIFY_FAILED diagnostics are visible ONLY to authorized principals."""
    reg = {
        "f1": _rs_rec("Falcons ok", "f1",
                      _grant("f1", allowed_principals=("alice",))),
        "f2": _rs_rec("Falcons broken", "f2",
                      _grant("f2", allowed_principals=("bob",)),
                      verify="VERIFY_FAILED", reason="CHECKSUM_MISMATCH"),
    }
    # alice: sees her file; f2's failure diagnostic must not leak to her.
    out = search_drive_registry(reg, "Falcons", principal="alice",
                                include_review=True)
    assert [r["file_id"] for r in out["results"]] == ["f1"]
    leaked = _serialized_contains(out, "f2", "broken", "CHECKSUM_MISMATCH")
    assert leaked == [], f"B2 leakage to unauthorized: {leaked}"
    # bob (authorized): diagnostic visible.
    out_b = search_drive_registry(reg, "Falcons", principal="bob",
                                  include_review=True)
    assert out_b["results"] == []
    reasons = {d["file_id"]: d["reason"]
               for d in out_b["excluded"]["details"]}
    assert reasons.get("f2") == "VERIFY_FAILED:CHECKSUM_MISMATCH"
    assert [r["file_id"] for r in out_b["review_listing"]] == ["f2"]


def test_regsearch_review_required_gated_by_acl():
    reg = {
        "f1": _rs_rec("Falcons pending", "f1",
                      _grant("f1", allowed_principals=("alice",)),
                      verify="REVIEW_REQUIRED", reason="ACCESS_DENIED"),
    }
    out = search_drive_registry(reg, "Falcons", principal="alice",
                                include_review=True)
    assert out["results"] == []
    assert [r["file_id"] for r in out["review_listing"]] == ["f1"]
    out_m = search_drive_registry(reg, "Falcons", principal="mallory",
                                  include_review=True)
    assert out_m == search_drive_registry({}, "Falcons",
                                           principal="mallory",
                                           include_review=True)


def test_regsearch_revoked_and_missing_grant_invisible():
    reg = {
        "f1": _rs_rec("Falcons revoked", "f1",
                      _grant("f1", allowed_principals=("alice",),
                             revoked=True)),
        "f2": {"name": "Falcons no grant", "verify_status": ACCESS_VERIFIED,
               "verify_reason": "OK", "rights_status": "approved"},
    }
    for p in ("alice", "mallory"):
        out = search_drive_registry(reg, "Falcons", principal=p,
                                    include_review=True)
        assert out == search_drive_registry({}, "Falcons", principal=p,
                                            include_review=True)


def test_include_review_cannot_bypass_acl():
    """include_review=True must not disclose review items to unauthorized."""
    reg = {
        "f1": _rs_rec("Falcons alice pending", "f1",
                      _grant("f1", allowed_principals=("alice",)),
                      verify="REVIEW_REQUIRED", reason="ACCESS_DENIED"),
        "f2": _rs_rec("Falcons bob pending", "f2",
                      _grant("f2", allowed_principals=("bob",)),
                      verify="REVIEW_REQUIRED", reason="ACCESS_DENIED"),
    }
    out = search_drive_registry(reg, "Falcons", principal="alice",
                                include_review=True)
    assert [r["file_id"] for r in out["review_listing"]] == ["f1"]
    assert _serialized_contains(out, "f2", "bob pending") == []


def test_regsearch_authorized_positive_control():
    """Positive control: authorized reads + diagnostics keep working."""
    reg = _restricted_registry()
    out = search_drive_registry(reg, "Confidential", principal="alice")
    assert [r["file_id"] for r in out["results"]] == ["restricted-file-001"]
    assert out["results"][0]["name"] == "Confidential acquisition plan"
    assert out["results"][0]["rights_status"] == "approved"
    assert out["total_considered"] == 1


# --- CLI paths: drive-search gains --principal; both paths enforce ACL ------


def _write_reg_file(tmp_path, records):
    import json as _json
    reg_path = tmp_path / "registry.json"
    reg_path.write_text(_json.dumps(records, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return reg_path


def _cli_main(argv):
    import sys
    from cba_kb import cli as _cli
    old = sys.argv
    sys.argv = argv
    try:
        _cli.main()
    finally:
        sys.argv = old


def test_cli_drive_search_principal_arg_required(tmp_path, capsys):
    reg_path = _write_reg_file(tmp_path, _restricted_registry())
    with pytest.raises(SystemExit) as exc:
        _cli_main(["cba-kb", "drive-search", "--registry", str(reg_path),
                   "--query", "Confidential"])
    assert exc.value.code == 2  # argparse: missing required --principal


def test_cli_drive_search_enforces_acl(tmp_path, capsys):
    """Two CLI search paths: guest learns nothing, alice sees her file."""
    import json as _json
    reg_path = _write_reg_file(tmp_path, _restricted_registry())
    idx_path = tmp_path / "index.json"
    si.save_index(idx_path,
                  si.build_index(_restricted_registry(),
                                 texts={"restricted-file-001":
                                        "synthetic confidential"}))
    # drive-search as guest
    _cli_main(["cba-kb", "drive-search", "--registry", str(reg_path),
               "--query", "Confidential", "--principal", "guest"])
    out = _json.loads(capsys.readouterr().out)
    leaked = [n for n in ("restricted-file-001",
                          "Confidential acquisition plan")
              if n in _json.dumps(out, ensure_ascii=False)]
    assert leaked == [] and out["results"] == []
    # drive-search as alice
    _cli_main(["cba-kb", "drive-search", "--registry", str(reg_path),
               "--query", "Confidential", "--principal", "alice"])
    out_a = _json.loads(capsys.readouterr().out)
    assert [r["file_id"] for r in out_a["results"]] == ["restricted-file-001"]
    # drive-search-index as guest
    _cli_main(["cba-kb", "drive-search-index", "--index", str(idx_path),
               "--registry", str(reg_path), "--query", "confidential",
               "--principal", "guest"])
    out_i = _json.loads(capsys.readouterr().out)
    assert out_i["results"] == []
    # drive-search-index as alice
    _cli_main(["cba-kb", "drive-search-index", "--index", str(idx_path),
               "--registry", str(reg_path), "--query", "confidential",
               "--principal", "alice"])
    out_ia = _json.loads(capsys.readouterr().out)
    assert [r["file_id"] for r in out_ia["results"]] == ["restricted-file-001"]
