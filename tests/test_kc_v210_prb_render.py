"""PR-B (Issue #220) rendering tests — written FIRST, must FAIL on old HEAD.

New module cba_kb.kc_render does not exist on base 15867026.
CANDIDATE renderings only. No format is described as readable by the
Drive connector: readability is NOT_VERIFIED until separately
authorized Phase-2 measurement. Clean-room values only.
"""

import json

import pytest

from cba_kb.kc_cleanroom import A, QUOTE, SEASON, build_cleanroom_fixture
from cba_kb.kc_render import (
    MEASUREMENT_PROTOCOL,
    READABILITY_STATUS,
    KcRenderError,
    check_completeness,
    parse_rendered,
    render_catalog,
    render_json_slice,
    render_markdown_table,
    render_passage_doc,
)


@pytest.fixture(scope="module")
def fx():
    return build_cleanroom_fixture()


def _slice(fx):
    return {
        "player_uid": A, "provider": "CBA_OFFICIAL", "competition": "CBA",
        "season": SEASON, "match_type": "REGULAR", "scope": "WHOLE_SEASON",
        "team_id": None,
        "metrics": {"points_per_game": {"value": 12.5, "unit": "per_game"},
                    "games_played": {"value": 40, "unit": "games"}},
        "supported_metrics": ["games_played", "points_per_game"],
        "row_count": 1, "complete": True, "end_marker": True,
    }


def test_json_slice_round_trips_with_end_marker(fx):
    text = render_json_slice(_slice(fx))
    parsed = parse_rendered(text, kind="json")
    assert parsed["end_marker"] is True
    assert parsed["metrics"]["points_per_game"]["value"] == 12.5
    assert check_completeness(text, kind="json") is True


def test_markdown_table_carries_values_and_end_marker(fx):
    text = render_markdown_table(_slice(fx))
    assert "points_per_game" in text and "12.5" in text
    assert check_completeness(text, kind="markdown") is True
    assert parse_rendered(text, kind="markdown")["end_marker"] is True


def test_passage_doc_carries_excerpt_revision_locator_digest(fx):
    passage = {"source_id": "fixture_doc_D1", "revision": "rev-1",
               "locator": "P3", "text": QUOTE, "digest": "f" * 64,
               "title": "授权中文采访"}
    text = render_passage_doc(passage, evidence_verified=True)
    assert QUOTE in text and "rev-1" in text and "P3" in text
    assert "f" * 64 in text
    assert check_completeness(text, kind="text") is True


def test_rendering_without_end_marker_is_an_explicit_error(fx):
    text = render_json_slice(_slice(fx))
    stripped = text.replace('"end_marker": true', '"end_marker": false')
    with pytest.raises(KcRenderError):
        parse_rendered(stripped, kind="json")
    assert check_completeness(stripped, kind="json") is False
    md = render_markdown_table(_slice(fx))
    md_stripped = md.replace("end_marker: true", "end_marker: false")
    with pytest.raises(KcRenderError):
        parse_rendered(md_stripped, kind="markdown")


def test_measurement_protocol_covers_required_checks_and_is_not_verified():
    assert READABILITY_STATUS == "NOT_VERIFIED_PENDING_PHASE_2"
    checks = {c["check"] for c in MEASUREMENT_PROTOCOL}
    assert {"pagination", "folder_children_limits", "sheets_ranges",
            "json_mime_searchability",
            "docs_revision_export_stability"} <= checks
    for c in MEASUREMENT_PROTOCOL:
        assert c["phase"] == "PHASE_2_SEPARATELY_AUTHORIZED"
        assert c["status"] == "NOT_VERIFIED"


def test_catalog_rendering_carries_end_marker(fx):
    catalog = {"catalog_version": 1, "snapshot_id": "kc-snapshot:abc",
               "slices": [_slice(fx)], "complete": True, "end_marker": True}
    text = render_catalog(catalog)
    parsed = json.loads(text)
    assert parsed["end_marker"] is True
    assert parsed["catalog_version"] == 1


# ---- Repair round 1: terminal-marker enforcement (R4) ----
# These fail behaviorally on pre-repair code: the old parser accepts an
# embedded marker, the old catalog renderer stamps completion, and the
# old passage renderer appends a marker without any verification.

def test_embedded_marker_in_body_is_not_terminal_proof(fx):
    text = ("# Title\n\nBody mentions end_marker: true in passing.\n\n"
            "Content ends here with NO terminal marker line.\n")
    with pytest.raises(KcRenderError):
        parse_rendered(text, kind="markdown")
    assert check_completeness(text, kind="markdown") is False
    good = render_markdown_table(_slice(fx)) + "\n\n"
    assert check_completeness(good, kind="markdown") is True


def test_incomplete_catalog_rendering_is_refused(fx):
    with pytest.raises(KcRenderError):
        render_catalog({"catalog_version": 1, "complete": False,
                        "end_marker": False})
    with pytest.raises(KcRenderError):
        render_catalog({"catalog_version": 1})


def test_passage_doc_requires_explicit_evidence_verification(fx):
    passage = {"source_id": "fixture_doc_D1", "revision": "rev-1",
               "locator": "P3", "text": QUOTE, "digest": "f" * 64,
               "title": "授权中文采访"}
    with pytest.raises(KcRenderError):
        render_passage_doc(passage)
    text = render_passage_doc(passage, evidence_verified=True)
    assert QUOTE in text and "rev-1" in text and "P3" in text
