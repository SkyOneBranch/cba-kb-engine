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
