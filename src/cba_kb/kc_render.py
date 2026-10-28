"""PR-B (Issue #220) CANDIDATE renderings — offline/synthetic only.

No format in this module is described as readable by the Drive
connector. Readability is NOT_VERIFIED until separately authorized
Phase-2 measurement (see MEASUREMENT_PROTOCOL). A rendering without
its end marker is an explicit error, never a silent truncation: the
parser raises instead of returning partial content as complete.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from .kc_projection import KcError

READABILITY_STATUS = "NOT_VERIFIED_PENDING_PHASE_2"

MEASUREMENT_PROTOCOL = (
    {"check": "pagination",
     "what_to_measure": "Whether paged reads return every page and "
                         "the consumer can detect a missing page.",
     "pass_criterion": "All pages retrieved; a missing page is detected, "
                        "never silently treated as complete.",
     "phase": "PHASE_2_SEPARATELY_AUTHORIZED", "status": "NOT_VERIFIED"},
    {"check": "folder_children_limits",
     "what_to_measure": "Maximum directly readable children per folder "
                         "and the behaviour past that limit.",
     "pass_criterion": "The limit is measured; exceeding it is detected, "
                        "never silently truncated.",
     "phase": "PHASE_2_SEPARATELY_AUTHORIZED", "status": "NOT_VERIFIED"},
    {"check": "sheets_ranges",
     "what_to_measure": "Whether native range reads return exact rows "
                         "for a routed slice key.",
     "pass_criterion": "Range reads match the routed key exactly.",
     "phase": "PHASE_2_SEPARATELY_AUTHORIZED", "status": "NOT_VERIFIED"},
    {"check": "sheets_cell_limits",
     "what_to_measure": "Maximum cells per scan and the behaviour past "
                         "that limit.",
     "pass_criterion": "The limit is measured; exceeding it is detected, "
                        "never silently truncated.",
     "phase": "PHASE_2_SEPARATELY_AUTHORIZED", "status": "NOT_VERIFIED"},
    {"check": "json_mime_searchability",
     "what_to_measure": "Whether JSON MIME content is searchable and "
                         "fully readable through the connector.",
     "pass_criterion": "JSON content is fully readable with types "
                        "preserved, or the format is rejected.",
     "phase": "PHASE_2_SEPARATELY_AUTHORIZED", "status": "NOT_VERIFIED"},
    {"check": "docs_revision_export_stability",
     "what_to_measure": "Whether a Docs revision and locator survive "
                         "rendering/export unchanged.",
     "pass_criterion": "Revision and locator bind to the same text on "
                        "re-read, or the binding is reported broken.",
     "phase": "PHASE_2_SEPARATELY_AUTHORIZED", "status": "NOT_VERIFIED"},
)


class KcRenderError(KcError):
    """Fail-closed rendering contract violation."""


def _marker_line(end_marker: bool) -> str:
    return "end_marker: true" if end_marker else "end_marker: false"


def render_json_slice(slice_dict: Dict[str, Any]) -> str:
    """JSON candidate rendering. The slice's own end_marker is carried
    verbatim; this function never upgrades an incomplete slice."""
    rendered = dict(slice_dict)
    rendered["rendering_kind"] = "json_slice"
    rendered["readability"] = READABILITY_STATUS
    return json.dumps(rendered, ensure_ascii=False, sort_keys=True,
                      indent=2)


def render_markdown_table(slice_dict: Dict[str, Any]) -> str:
    """Markdown field-table candidate rendering, with an explicit end
    marker line the consumer can verify."""
    lines = [
        "# Slice: %s / %s / %s / %s"
        % (slice_dict["player_uid"], slice_dict["season"],
           slice_dict["match_type"], slice_dict["scope"]),
        "",
        "| metric | value | unit |",
        "| --- | --- | --- |",
    ]
    for metric in slice_dict.get("supported_metrics", []):
        entry = slice_dict["metrics"][metric]
        lines.append("| %s | %s | %s |"
                     % (metric, entry["value"], entry["unit"]))
    lines.append("")
    lines.append(_marker_line(bool(slice_dict.get("end_marker"))))
    return "\n".join(lines) + "\n"


def render_passage_doc(passage: Dict[str, Any]) -> str:
    """Passage text-doc candidate rendering: title, excerpt, revision,
    locator, digest, and an explicit end marker line."""
    lines = [
        "# %s" % passage.get("title", passage["source_id"]),
        "",
        passage["text"],
        "",
        "source_id: %s" % passage["source_id"],
        "revision: %s" % passage["revision"],
        "locator: %s" % passage["locator"],
        "digest: %s" % passage["digest"],
        "",
        "end_marker: true",
    ]
    return "\n".join(lines) + "\n"


def render_catalog(catalog: Dict[str, Any]) -> str:
    """Catalog candidate rendering. The rendering itself is complete
    (it renders the whole catalog dict given); slice-level completeness
    remains the slices' own end markers."""
    rendered = dict(catalog)
    rendered["rendering_kind"] = "json_catalog"
    rendered["readability"] = READABILITY_STATUS
    rendered["end_marker"] = True
    return json.dumps(rendered, ensure_ascii=False, sort_keys=True,
                      indent=2)


def parse_rendered(text: str, *, kind: str) -> Dict[str, Any]:
    """Parse a rendering and enforce its end marker. A missing or
    false end marker raises KcRenderError — partial content is never
    returned as complete."""
    if kind == "json":
        parsed = json.loads(text)
        if parsed.get("end_marker") is not True:
            raise KcRenderError("RENDERING_END_MARKER_MISSING")
        return parsed
    if kind in ("markdown", "text"):
        if "end_marker: true" not in text:
            raise KcRenderError("RENDERING_END_MARKER_MISSING")
        return {"end_marker": True}
    raise KcRenderError(f"UNKNOWN_RENDERING_KIND:{kind}")


def check_completeness(text: str, *, kind: str) -> bool:
    """True only if the rendering parses with a true end marker."""
    try:
        parse_rendered(text, kind=kind)
    except (KcRenderError, ValueError):
        return False
    return True


def measurement_protocol() -> List[Dict[str, Any]]:
    """A copy of the Phase-2 measurement protocol as data. Every check
    is NOT_VERIFIED until separately authorized measurement runs."""
    return [dict(check) for check in MEASUREMENT_PROTOCOL]
