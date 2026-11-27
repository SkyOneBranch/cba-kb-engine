"""REQ-240 P0-3: PUBLIC_SAFE static HTML preview site builder (stdlib only).

Builds a self-contained static HTML preview site (`index.html`, `persons/<slug>.html`,
`assets/style.css`) strictly from `public_safe`-verified club and person cards.

Fail-closed guarantee:
All input cards and metadata are validated (visibility == "public_safe" and
`verify_public_safe_card_text(...)`) and all HTML/CSS payloads are rendered in
memory BEFORE creating `output_dir` or writing any file. Any violation raises
`ValueError("PUBLIC_SITE_VISIBILITY_VIOLATION: ...")` and leaves 0 partial
output files on disk.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .kc_visibility import validate_card_visibility, verify_public_safe_card_text

_SLUG_SAFE_RE = re.compile(r"[^a-z0-9_-]+")
_URL_RE = re.compile(r"https?://[^\s<>\"')，。；：！？（）【】、]+")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_INLINE_FN_REF_RE = re.compile(r"\[(\d+)\](?!\()")
_PUBLIC_SITE_INTERNAL_TOKEN_RE = re.compile(
    r"(?:\b[\w.-]+\.(?:csv|jsonl)\b|\bteam_staff_snapshots\b|\bclub_announcement:)",
    re.IGNORECASE,
)
_DETAILS_OPEN_RE = re.compile(
    r'^<details(?:\s+class="(audit-drawer|gamelog-drawer)")?\s*>$'
)
_SUMMARY_LINE_RE = re.compile(r"^<summary>(.*)</summary>$")
_INTERNAL_CLUE_MARKER = "[internal_clue_only"
_TILDE_FENCE_MARKER = "~~~"
_COACH_ROLES_SET = frozenset({
    "COACH", "HEAD_COACH", "ASSISTANT_COACH", "ACTING_HEAD_COACH", "STAFF",
    "主教练", "助理教练", "教练", "执行教练",
})
_HIDDEN_INFOBOX_SENTINELS = frozenset({"", "0", "0 cm", "0 kg", "unassigned", "unknown", "-", "none", "n/a"})


def _fail_visibility(reason: str) -> None:
    raise ValueError(f"PUBLIC_SITE_VISIBILITY_VIOLATION: {reason}")


def _verify_public_text_strict(text: str, *, label: str) -> None:
    if not isinstance(text, str):
        _fail_visibility(f"{label}:non_string_text")
    try:
        verify_public_safe_card_text(text)
    except ValueError as exc:
        _fail_visibility(f"{label}:{exc}")
    lowered = text.lower()
    if _INTERNAL_CLUE_MARKER in lowered:
        _fail_visibility(f"{label}:internal_clue_only_marker")
    if _TILDE_FENCE_MARKER in text:
        _fail_visibility(f"{label}:tilde_fenced_code_block")
    if _PUBLIC_SITE_INTERNAL_TOKEN_RE.search(text):
        _fail_visibility(f"{label}:internal_table_or_snapshot_token")


def _validate_card_payload(card: Any, *, label: str) -> Tuple[str, List[Dict[str, str]]]:
    if not isinstance(card, Mapping):
        _fail_visibility(f"{label}:card_must_be_mapping")

    raw_visibility = card.get("visibility")
    try:
        visibility = validate_card_visibility(raw_visibility)  # type: ignore[arg-type]
    except ValueError:
        _fail_visibility(f"{label}:invalid_visibility={raw_visibility!r}")
    if visibility != "public_safe":
        _fail_visibility(f"{label}:visibility={visibility!r} is not 'public_safe'")

    markdown = card.get("markdown")
    if not isinstance(markdown, str):
        _fail_visibility(f"{label}:missing_markdown")
    _verify_public_text_strict(markdown, label=f"{label}.markdown")

    # Validate display fields and citations defensively as well.
    for field_name in ("club_name", "person_name", "organization_id", "person_uid", "title"):
        val = card.get(field_name)
        if isinstance(val, str) and val:
            _verify_public_text_strict(val, label=f"{label}.{field_name}")

    citations = _extract_and_validate_citations(card, markdown=markdown, label=label)
    return markdown, citations


def _extract_and_validate_citations(
    card: Mapping[str, Any], *, markdown: str, label: str
) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    seen_urls: set[str] = set()

    raw_citations = card.get("citations")
    if isinstance(raw_citations, Sequence) and not isinstance(raw_citations, (str, bytes)):
        for idx, item in enumerate(raw_citations):
            c_pub = ""
            c_date = ""
            c_kind = ""
            if isinstance(item, Mapping):
                c_label = str(item.get("label") or item.get("title") or "").strip()
                c_url = str(item.get("url") or item.get("canonical_url") or "").strip()
                c_pub = str(item.get("publisher") or "").strip()
                c_date = str(item.get("published_at") or "").strip()
                c_kind = str(item.get("source_kind") or "").strip()
            elif isinstance(item, str):
                c_label = item.strip()
                c_url = item.strip() if item.strip().startswith(("http://", "https://")) else ""
            else:
                continue

            if c_label:
                _verify_public_text_strict(c_label, label=f"{label}.citations[{idx}].label")
            if c_url:
                _verify_public_text_strict(c_url, label=f"{label}.citations[{idx}].url")
            if c_pub:
                _verify_public_text_strict(c_pub, label=f"{label}.citations[{idx}].publisher")
            if not c_label and not c_url:
                continue
            if c_url:
                seen_urls.add(c_url)
            entry: Dict[str, str] = {"label": c_label or c_url, "url": c_url}
            if c_pub:
                entry["publisher"] = c_pub
            if c_date:
                entry["published_at"] = c_date
            if c_kind:
                entry["source_kind"] = c_kind
            result.append(entry)

    if not result:
        for match in _URL_RE.finditer(markdown):
            url = match.group(0).rstrip(".,;:")
            if url not in seen_urls:
                seen_urls.add(url)
                result.append({"label": url, "url": url})

    return result


def _derive_slug(card: Mapping[str, Any], *, index: int, used: set[str]) -> str:
    raw = card.get("slug") or card.get("person_uid") or card.get("player_uid") or f"person-{index + 1}"
    if not isinstance(raw, str):
        raw = f"person-{index + 1}"
    if "/" in raw or "\\" in raw or ".." in raw:
        _fail_visibility(f"person_cards[{index}]:unsafe_slug={raw!r}")
    candidate = _SLUG_SAFE_RE.sub("-", raw.strip().lower()).strip("-_")
    if not candidate:
        candidate = f"person-{index + 1}"
    slug = candidate
    suffix = 2
    while slug in used:
        slug = f"{candidate}-{suffix}"
        suffix += 1
    used.add(slug)
    return slug


def _safe_json_ld(payload: Mapping[str, Any]) -> str:
    # Escape closing script sequences inside JSON-LD blocks.
    return json.dumps(payload, ensure_ascii=False, indent=2).replace("</", "<\\/")


def _apply_entity_links(
    escaped_html: str, entity_links: Sequence[Tuple[str, str]]
) -> str:
    """Link plain-text entity names in escaped HTML without touching existing tags."""
    if not entity_links:
        return escaped_html
    # Sort longer names first to avoid partial shadowing.
    ordered = sorted(
        [(name, href) for name, href in entity_links if name],
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    if not ordered:
        return escaped_html

    # Split HTML into tags / anchor blocks vs plain text segments.
    parts = re.split(r"(<a\b[^>]*>.*?</a>|<[^>]+>)", escaped_html)
    out_parts: List[str] = []
    for part in parts:
        if not part or part.startswith("<"):
            out_parts.append(part)
            continue
        linked = part
        for name, href in ordered:
            esc_name = html.escape(name)
            esc_href = html.escape(href, quote=True)
            # Replace only outside tags we may have just inserted by splitting again.
            sub_parts = re.split(r"(<a\b[^>]*>.*?</a>)", linked)
            rebuilt: List[str] = []
            for sub in sub_parts:
                if sub.startswith("<a"):
                    rebuilt.append(sub)
                else:
                    rebuilt.append(
                        sub.replace(
                            esc_name,
                            f'<a class="entity-link" href="{esc_href}">{esc_name}</a>',
                        )
                    )
            linked = "".join(rebuilt)
        out_parts.append(linked)
    return "".join(out_parts)


def _replace_inline_fn_refs(
    escaped_html: str,
    *,
    max_citation_index: int,
    seen_fnrefs: set[int],
) -> str:
    if max_citation_index <= 0:
        return escaped_html
    parts = re.split(r"(<a\b[^>]*>.*?</a>|<[^>]+>)", escaped_html)
    out: List[str] = []
    for part in parts:
        if not part or part.startswith("<"):
            out.append(part)
            continue

        def _sub_fn(m: re.Match[str]) -> str:
            idx = int(m.group(1))
            if idx < 1 or idx > max_citation_index:
                return m.group(0)
            if idx not in seen_fnrefs:
                seen_fnrefs.add(idx)
                return f'<sup class="cite-ref"><a class="fn-ref" href="#fn-{idx}" id="fnref-{idx}">[{idx}]</a></sup>'
            return f'<sup class="cite-ref"><a class="fn-ref" href="#fn-{idx}">[{idx}]</a></sup>'

        out.append(_INLINE_FN_REF_RE.sub(_sub_fn, part))
    return "".join(out)


def _format_inline_markdown(
    text: str,
    entity_links: Sequence[Tuple[str, str]],
    *,
    max_citation_index: int = 0,
    seen_fnrefs: Optional[set[int]] = None,
) -> str:
    """Convert inline markdown links, bare URLs, entity links, and `[k]` footnote refs safely."""
    tokens: List[str] = []
    pos = 0
    for match in _MD_LINK_RE.finditer(text):
        if match.start() > pos:
            tokens.append(_format_bare_urls(text[pos : match.start()], entity_links))
        label = html.escape(match.group(1))
        url = html.escape(match.group(2), quote=True)
        tokens.append(
            f'<a href="{url}" target="_blank" rel="noopener noreferrer">{label}</a>'
        )
        pos = match.end()
    if pos < len(text):
        tokens.append(_format_bare_urls(text[pos:], entity_links))
    rendered = "".join(tokens)
    if max_citation_index > 0 and seen_fnrefs is not None:
        rendered = _replace_inline_fn_refs(
            rendered,
            max_citation_index=max_citation_index,
            seen_fnrefs=seen_fnrefs,
        )
    return rendered


def _format_bare_urls(
    text: str, entity_links: Sequence[Tuple[str, str]]
) -> str:
    tokens: List[str] = []
    pos = 0
    for match in _URL_RE.finditer(text):
        if match.start() > pos:
            plain = html.escape(text[pos : match.start()])
            tokens.append(_apply_entity_links(plain, entity_links))
        raw_url = match.group(0)
        trailing = ""
        while raw_url and raw_url[-1] in ".,;:":
            trailing = raw_url[-1] + trailing
            raw_url = raw_url[:-1]
        esc_url = html.escape(raw_url, quote=True)
        tokens.append(
            f'<a href="{esc_url}" target="_blank" rel="noopener noreferrer">{esc_url}</a>'
        )
        if trailing:
            tokens.append(html.escape(trailing))
        pos = match.end()
    if pos < len(text):
        plain = html.escape(text[pos:])
        tokens.append(_apply_entity_links(plain, entity_links))
    return "".join(tokens)


def _markdown_to_html(
    markdown: str,
    *,
    entity_links: Sequence[Tuple[str, str]] = (),
    max_citation_index: int = 0,
    seen_fnrefs: Optional[set[int]] = None,
    strip_references_section: bool = False,
) -> str:
    lines = markdown.splitlines()
    blocks: List[str] = []
    in_list = False
    in_table = False
    skipping_refs = False
    fnref_tracker = seen_fnrefs if seen_fnrefs is not None else set()

    def _close_open() -> None:
        nonlocal in_list, in_table
        if in_list:
            blocks.append("</ul>")
            in_list = False
        if in_table:
            blocks.append("</tbody></table>")
            in_table = False

    def _fmt(s: str) -> str:
        return _format_inline_markdown(
            s,
            entity_links,
            max_citation_index=max_citation_index,
            seen_fnrefs=fnref_tracker,
        )

    for raw_line in lines:
        stripped = raw_line.strip()
        if strip_references_section:
            if stripped == "## 参考资料":
                _close_open()
                skipping_refs = True
                continue
            if skipping_refs:
                if stripped.startswith("## ") or stripped.startswith("<details") or stripped == "end_marker: true":
                    skipping_refs = False
                else:
                    continue

        if not stripped:
            _close_open()
            continue
        if stripped == "end_marker: true":
            _close_open()
            continue

        det_match = _DETAILS_OPEN_RE.fullmatch(stripped)
        if det_match:
            _close_open()
            cls_name = det_match.group(1)
            blocks.append(f'<details class="{cls_name}">' if cls_name else "<details>")
            continue
        if stripped == "</details>":
            _close_open()
            blocks.append("</details>")
            continue
        sum_match = _SUMMARY_LINE_RE.fullmatch(stripped)
        if sum_match:
            _close_open()
            blocks.append(f"<summary>{html.escape(sum_match.group(1))}</summary>")
            continue

        if stripped.startswith("#### "):
            _close_open()
            blocks.append(f"<h4>{_fmt(stripped[5:])}</h4>")
        elif stripped.startswith("### "):
            _close_open()
            blocks.append(f"<h3>{_fmt(stripped[4:])}</h3>")
        elif stripped.startswith("## "):
            _close_open()
            blocks.append(f"<h2>{_fmt(stripped[3:])}</h2>")
        elif stripped.startswith("# "):
            _close_open()
            blocks.append(f"<h1>{_fmt(stripped[2:])}</h1>")
        elif stripped.startswith("- ") or stripped.startswith("* "):
            if in_table:
                blocks.append("</tbody></table>")
                in_table = False
            if not in_list:
                blocks.append("<ul>")
                in_list = True
            blocks.append(f"<li>{_fmt(stripped[2:])}</li>")
        elif stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(set(c) <= {"-", ":", " "} and c for c in cells):
                continue
            if in_list:
                blocks.append("</ul>")
                in_list = False
            if not in_table:
                blocks.append('<table class="wiki-table"><thead><tr>')
                for cell in cells:
                    blocks.append(f"<th>{_fmt(cell)}</th>")
                blocks.append("</tr></thead><tbody>")
                in_table = True
            else:
                blocks.append("<tr>")
                for cell in cells:
                    blocks.append(f"<td>{_fmt(cell)}</td>")
                blocks.append("</tr>")
        else:
            _close_open()
            blocks.append(f"<p>{_fmt(stripped)}</p>")

    _close_open()
    return "\n".join(blocks)


def _render_footnotes_section(
    citations: Sequence[Mapping[str, str]],
    *,
    has_inline_refs: bool = False,
    is_wiki_v241: bool = False,
) -> Tuple[str, str]:
    if not citations:
        return "", ""
    inline_refs: List[str] = []
    items: List[str] = []
    for idx, cite in enumerate(citations, start=1):
        raw_label = str(cite.get("label") or cite.get("url") or f"Source {idx}").strip()
        publisher = str(cite.get("publisher") or "").strip()
        published_at = str(cite.get("published_at") or "").strip()
        parts = [raw_label] if raw_label else []
        if publisher and publisher not in raw_label:
            parts.append(publisher)
        if published_at:
            parts.append(published_at)
        label = html.escape(" · ".join(parts) if parts else f"Source {idx}")
        url = str(cite.get("url") or "").strip()
        inline_refs.append(
            f'<sup><a class="fn-ref" href="#fn-{idx}" id="fnref-{idx}">[{idx}]</a></sup>'
        )
        if url:
            esc_url = html.escape(url, quote=True)
            items.append(
                f'<li id="fn-{idx}">'
                f'<span class="fn-num">[{idx}]</span> '
                f'<a href="{esc_url}" target="_blank" rel="noopener noreferrer">{label}</a> '
                f'<a class="fn-back" href="#fnref-{idx}" aria-label="Back to reference">&#8617;</a>'
                f"</li>"
            )
        else:
            items.append(
                f'<li id="fn-{idx}">'
                f'<span class="fn-num">[{idx}]</span> {label} '
                f'<a class="fn-back" href="#fnref-{idx}" aria-label="Back to reference">&#8617;</a>'
                f"</li>"
            )
    ref_bar = (
        ""
        if (has_inline_refs or is_wiki_v241)
        else '<p class="citation-refs">Sources: ' + " ".join(inline_refs) + "</p>"
    )
    heading = "参考资料" if (is_wiki_v241 or has_inline_refs) else "References"
    section = (
        '<section class="footnotes" id="footnotes">\n'
        f"  <h2>{heading}</h2>\n"
        "  <ol>\n    "
        + "\n    ".join(items)
        + "\n  </ol>\n</section>"
    )
    return ref_bar, section


def _render_style_css() -> str:
    return """/* CBA-KB PUBLIC_SAFE Static Preview Site */
:root {
  --bg: #f8f9fb;
  --surface: #ffffff;
  --text: #1c2430;
  --muted: #5a6577;
  --accent: #1d4ed8;
  --border: #dfe4ec;
  --badge-bg: #e8f5e9;
  --badge-fg: #1b5e20;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Noto Sans SC", sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.65;
}
header.site-header {
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 1rem 1.5rem;
}
.site-header-inner, main.container, footer.site-footer {
  max-width: 960px;
  margin: 0 auto;
}
.site-header-inner {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
}
.site-brand {
  font-weight: 700;
  font-size: 1.1rem;
  color: var(--text);
  text-decoration: none;
}
.badge-public {
  display: inline-block;
  padding: 0.15rem 0.55rem;
  border-radius: 999px;
  font-size: 0.78rem;
  font-weight: 600;
  background: var(--badge-bg);
  color: var(--badge-fg);
}
main.container {
  padding: 1.5rem;
}
article.card-body, section.entity-index, section.footnotes {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 1.5rem;
  margin-bottom: 1.25rem;
}
h1, h2, h3, h4 {
  margin-top: 1.25rem;
  margin-bottom: 0.5rem;
  line-height: 1.3;
}
h1:first-child { margin-top: 0; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
a.entity-link { font-weight: 500; }
.meta-bar {
  font-size: 0.88rem;
  color: var(--muted);
  margin-bottom: 1rem;
}
.breadcrumb {
  font-size: 0.9rem;
  margin-bottom: 1rem;
}
.wiki-table {
  width: 100%;
  border-collapse: collapse;
  margin: 1rem 0;
  font-size: 0.92rem;
}
.wiki-table th, .wiki-table td {
  border: 1px solid var(--border);
  padding: 0.45rem 0.65rem;
  text-align: left;
}
.wiki-table th { background: #f1f4f9; }
.entity-list {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem 1rem;
  list-style: none;
  padding: 0;
  margin: 0.5rem 0 0;
}
.footnotes ol { padding-left: 1.25rem; }
.fn-ref { font-size: 0.8em; }
.fn-back { margin-left: 0.35rem; font-size: 0.85em; }
.wiki-infobox {
  float: right;
  width: 280px;
  margin: 0 0 1rem 1.5rem;
  background: #f9fafc;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.85rem 1rem;
  font-size: 0.9rem;
}
.wiki-infobox h3 {
  margin: 0 0 0.6rem;
  font-size: 1rem;
  text-align: center;
  border-bottom: 1px solid var(--border);
  padding-bottom: 0.4rem;
}
.wiki-infobox table {
  width: 100%;
  border-collapse: collapse;
}
.wiki-infobox th, .wiki-infobox td {
  padding: 0.25rem 0.35rem;
  vertical-align: top;
  border-bottom: 1px solid #edf0f5;
  text-align: left;
}
.wiki-infobox th {
  color: var(--muted);
  font-weight: 600;
  white-space: nowrap;
  width: 40%;
}
.audit-drawer, .gamelog-drawer {
  margin: 1rem 0;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.55rem 0.85rem;
  background: #fafbfd;
}
.audit-drawer summary, .gamelog-drawer summary {
  cursor: pointer;
  font-weight: 600;
  color: var(--muted);
}
.related-persons {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 1.25rem 1.5rem;
  margin-bottom: 1.25rem;
}
.relation-badge {
  display: inline-block;
  margin-left: 0.3rem;
  padding: 0.08rem 0.45rem;
  border-radius: 999px;
  font-size: 0.75rem;
  background: #eef2ff;
  color: #3730a3;
}
.squad-navbox {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.85rem 1.25rem;
  margin-bottom: 1.25rem;
}
.squad-navbox summary {
  cursor: pointer;
  font-weight: 600;
}
@media (max-width: 720px) {
  .wiki-infobox { float: none; width: 100%; margin: 0 0 1rem 0; }
}
footer.site-footer {
  padding: 0 1.5rem 2rem;
  font-size: 0.85rem;
  color: var(--muted);
}
"""


def _is_displayable_infobox_value(val: Any) -> bool:
    if val is None or isinstance(val, bool):
        return False
    if isinstance(val, (int, float)):
        return val > 0
    text = str(val).strip()
    if not text:
        return False
    if text.casefold() in _HIDDEN_INFOBOX_SENTINELS:
        return False
    if "unassigned" in text.casefold():
        return False
    return True


def _verify_mapping_values_public_safe(
    data: Any, *, label: str
) -> None:
    """Recursively verify every string value inside a bio/registration mapping."""
    if isinstance(data, str):
        if data:
            _verify_public_text_strict(data, label=label)
    elif isinstance(data, Mapping):
        for k, v in data.items():
            if k in ("evidence_refs",):
                continue
            _verify_mapping_values_public_safe(v, label=f"{label}.{k}")
    elif isinstance(data, Sequence) and not isinstance(data, (bytes, bytearray)):
        for idx, item in enumerate(data):
            _verify_mapping_values_public_safe(item, label=f"{label}[{idx}]")


def _render_wiki_infobox(person: Mapping[str, Any]) -> str:
    bio = person.get("bio_profile")
    if hasattr(bio, "to_dict") and callable(bio.to_dict):
        bio = bio.to_dict()
    bio_map = bio if isinstance(bio, Mapping) else {}
    reg_map = person.get("registration_profile") if isinstance(person.get("registration_profile"), Mapping) else {}

    rows: List[Tuple[str, str]] = []
    name = str(bio_map.get("canonical_name") or person.get("person_name") or "").strip()

    def _add(label: str, raw_val: Any, suffix: str = "") -> None:
        if not _is_displayable_infobox_value(raw_val):
            return
        disp = f"{raw_val}{suffix}" if suffix else str(raw_val).strip()
        if disp.casefold() in _HIDDEN_INFOBOX_SENTINELS:
            return
        rows.append((label, disp))

    _add("英文名", bio_map.get("english_name") or reg_map.get("player_name_en"))
    _add("出生日期", bio_map.get("birth_date"))
    _add("出生地", bio_map.get("birth_place"))
    _add("国籍", bio_map.get("nationality"))
    h_cm = bio_map.get("height_cm")
    if isinstance(h_cm, int) and not isinstance(h_cm, bool) and h_cm > 0:
        rows.append(("身高", f"{h_cm} cm"))
    w_kg = bio_map.get("weight_kg")
    if isinstance(w_kg, int) and not isinstance(w_kg, bool) and w_kg > 0:
        rows.append(("体重", f"{w_kg} kg"))
    _add("位置", bio_map.get("position") or reg_map.get("position"))
    _add("球衣号码", bio_map.get("jersey_number") or reg_map.get("jersey_number"))
    _add("现效力球队", bio_map.get("current_club") or reg_map.get("current_club"))
    _add("角色", bio_map.get("current_role"))
    _add("青训/选秀来源", bio_map.get("draft_or_youth_origin"))
    honors = bio_map.get("honors")
    if isinstance(honors, Sequence) and not isinstance(honors, (str, bytes)):
        clean_honors = [str(h).strip() for h in honors if _is_displayable_infobox_value(h)]
        if clean_honors:
            rows.append(("主要荣誉", "、".join(clean_honors)))

    if not rows:
        return ""

    tr_html = "\n".join(
        f"      <tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>"
        for k, v in rows
    )
    return (
        '<aside class="wiki-infobox">\n'
        f"  <h3>{html.escape(name or '人物档案')}</h3>\n"
        "  <table>\n"
        "    <tbody>\n"
        f"{tr_html}\n"
        "    </tbody>\n"
        "  </table>\n"
        "</aside>"
    )


def _extract_person_meta(card: Mapping[str, Any]) -> Dict[str, Any]:
    bio = card.get("bio_profile")
    if hasattr(bio, "to_dict") and callable(bio.to_dict):
        bio = bio.to_dict()
    bio_map = bio if isinstance(bio, Mapping) else {}
    reg_map = card.get("registration_profile") if isinstance(card.get("registration_profile"), Mapping) else {}
    person_sub = card.get("person") if isinstance(card.get("person"), Mapping) else {}

    uid = str(
        card.get("person_uid")
        or bio_map.get("person_uid")
        or person_sub.get("person_uid")
        or card.get("player_uid")
        or person_sub.get("player_uid")
        or ""
    ).strip()
    name = str(
        card.get("person_name")
        or card.get("canonical_name")
        or bio_map.get("canonical_name")
        or person_sub.get("player_name")
        or uid
    ).strip()
    slug = str(card.get("slug") or "").strip()
    role = str(
        card.get("entity_role")
        or bio_map.get("current_role")
        or person_sub.get("entity_role")
        or ""
    ).strip()
    club = str(
        bio_map.get("current_club")
        or reg_map.get("current_club")
        or person_sub.get("current_club")
        or ""
    ).strip()
    origin = str(bio_map.get("draft_or_youth_origin") or "").strip()
    alma_mater = str(
        bio_map.get("alma_mater")
        or bio_map.get("school")
        or bio_map.get("university")
        or bio_map.get("college")
        or ""
    ).strip()
    story_ids = {
        str(st.get("story_id")).strip()
        for st in (card.get("projected_stories") or ())
        if isinstance(st, Mapping) and st.get("story_id")
    }
    return {
        "person_uid": uid,
        "person_name": name,
        "slug": slug,
        "is_coach": role.upper() in _COACH_ROLES_SET or role in _COACH_ROLES_SET or "COACH" in role.upper() or "教练" in role,
        "club": club,
        "origin": origin,
        "alma_mater": alma_mater,
        "story_ids": story_ids,
    }


def compute_related_persons(
    target_person: Mapping[str, Any],
    all_persons: Sequence[Mapping[str, Any]],
    *,
    story_records: Sequence[Mapping[str, Any]] = (),
    max_related: int = 8,
) -> List[Dict[str, Any]]:
    """Compute top-N relationship-driven related persons with Chinese relation labels (REQ-241 P0-4)."""
    t_meta = _extract_person_meta(target_person)
    t_story_ids = set(t_meta["story_ids"])
    story_participants_map: Dict[str, set[str]] = {}
    for st in story_records or ():
        if not isinstance(st, Mapping):
            continue
        sid = str(st.get("story_id") or st.get("title") or "").strip()
        if not sid:
            continue
        p_tokens: set[str] = set()
        for p in st.get("participants") or ():
            if not isinstance(p, Mapping):
                continue
            for k in ("person_uid", "display_name"):
                v = str(p.get(k) or "").strip()
                if v:
                    p_tokens.add(v)
        story_participants_map[sid] = p_tokens
        if (t_meta["person_uid"] and t_meta["person_uid"] in p_tokens) or (
            t_meta["person_name"] and t_meta["person_name"] in p_tokens
        ):
            t_story_ids.add(sid)

    scored: List[Dict[str, Any]] = []
    seen_targets: set[str] = set()
    for other in all_persons or ():
        if not isinstance(other, Mapping):
            continue
        o_meta = _extract_person_meta(other)
        if (
            (t_meta["person_uid"] and o_meta["person_uid"] == t_meta["person_uid"])
            or (t_meta["slug"] and o_meta["slug"] == t_meta["slug"])
            or (t_meta["person_name"] and o_meta["person_name"] == t_meta["person_name"])
        ):
            continue
        dedup_k = o_meta["person_uid"] or o_meta["slug"] or o_meta["person_name"]
        if not dedup_k or dedup_k in seen_targets:
            continue
        seen_targets.add(dedup_k)

        o_story_ids = set(o_meta["story_ids"])
        for sid, p_tokens in story_participants_map.items():
            if (o_meta["person_uid"] and o_meta["person_uid"] in p_tokens) or (
                o_meta["person_name"] and o_meta["person_name"] in p_tokens
            ):
                o_story_ids.add(sid)

        shared_stories = t_story_ids & o_story_ids
        score = 0
        relation_label = ""
        if shared_stories:
            score += 50 * len(shared_stories)
            relation_label = "共同经历"
        if t_meta["is_coach"] != o_meta["is_coach"] and (
            t_meta["club"] and o_meta["club"] and t_meta["club"] == o_meta["club"]
        ):
            score += 30
            if not relation_label:
                relation_label = "同期教练与球员"
        if (
            t_meta["alma_mater"]
            and o_meta["alma_mater"]
            and t_meta["alma_mater"] == o_meta["alma_mater"]
        ):
            score += 25
            if not relation_label:
                relation_label = "大学校友"
        if t_meta["origin"] and o_meta["origin"] and t_meta["origin"] == o_meta["origin"]:
            score += 20
            if not relation_label:
                relation_label = "同届选秀" if "选秀" in t_meta["origin"] else "青训同梯队"
        if t_meta["club"] and o_meta["club"] and t_meta["club"] == o_meta["club"]:
            score += 10
            if not relation_label:
                relation_label = "同队队友"
        if score == 0:
            continue

        scored.append(
            {
                "person_uid": o_meta["person_uid"],
                "person_name": o_meta["person_name"],
                "slug": o_meta["slug"],
                "relation_label": relation_label,
                "score": score,
            }
        )

    scored.sort(key=lambda item: (-item["score"], item["person_name"], item["slug"]))
    return scored[: max(0, int(max_related))]


def _render_html_document(
    *,
    title: str,
    site_title: str,
    css_href: str,
    home_href: str,
    json_ld: Mapping[str, Any],
    body_html: str,
    observed_until: str,
    breadcrumb_html: str = "",
    meta_description: str = "",
) -> str:
    esc_title = html.escape(title)
    esc_site = html.escape(site_title)
    esc_css = html.escape(css_href, quote=True)
    esc_home = html.escape(home_href, quote=True)
    esc_obs = html.escape(observed_until)
    meta_obs = f" | Observed until: {esc_obs}" if esc_obs else ""
    desc_tag = (
        f'  <meta name="description" content="{html.escape(meta_description, quote=True)}">\n'
        if meta_description
        else ""
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{desc_tag}"
        f"  <title>{esc_title}</title>\n"
        f'  <link rel="stylesheet" href="{esc_css}">\n'
        '  <script type="application/ld+json">\n'
        f"{_safe_json_ld(json_ld)}\n"
        "  </script>\n"
        "</head>\n"
        "<body>\n"
        '  <header class="site-header">\n'
        '    <div class="site-header-inner">\n'
        f'      <a class="site-brand" href="{esc_home}">{esc_site}</a>\n'
        '      <span class="badge-public">PUBLIC_SAFE</span>\n'
        "    </div>\n"
        "  </header>\n"
        '  <main class="container">\n'
        f"    {breadcrumb_html}\n"
        f'    <div class="meta-bar">Visibility: PUBLIC_SAFE{meta_obs}</div>\n'
        f"    {body_html}\n"
        "  </main>\n"
        '  <footer class="site-footer">\n'
        f"    <p>{esc_site} &middot; PUBLIC_SAFE static preview{meta_obs}</p>\n"
        "  </footer>\n"
        "</body>\n"
        "</html>\n"
    )


def _build_sitemap_xml(
    *,
    base_url: Optional[str],
    person_slugs: Sequence[str],
    observed_until: str,
) -> str:
    prefix = (base_url.rstrip("/") + "/") if base_url else ""
    lastmod_tag = f"<lastmod>{html.escape(observed_until[:10])}</lastmod>" if observed_until else ""
    urls = [f"{prefix}index.html", *(f"{prefix}persons/{slug}.html" for slug in person_slugs)]
    entries = "\n".join(
        f"  <url><loc>{html.escape(u)}</loc>{lastmod_tag}</url>"
        for u in urls
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{entries}\n"
        "</urlset>\n"
    )


def _build_llms_txt(
    *,
    site_title: str,
    club_name: str,
    observed_until: str,
    validated_persons: Sequence[Mapping[str, Any]],
    base_url: Optional[str],
) -> str:
    prefix = (base_url.rstrip("/") + "/") if base_url else ""
    lines: List[str] = [
        f"# {site_title}",
        "",
        f"- Club: {club_name}",
        f"- Visibility: PUBLIC_SAFE",
    ]
    if observed_until:
        lines.append(f"- Observed Until: {observed_until}")
    lines.extend([
        "",
        "## Core Pages",
        f"- [{club_name}]({prefix}index.html)",
        "",
        "## Persons",
    ])
    for p in validated_persons:
        lead = str(p.get("lead_plain_text") or "").strip()
        suffix = f" — {lead}" if lead else ""
        lines.append(f"- [{p['person_name']}]({prefix}persons/{p['slug']}.html){suffix}")
    return "\n".join(lines) + "\n"


def _count_broken_internal_links(
    *,
    index_html: str,
    rendered_persons: Sequence[Tuple[Dict[str, Any], str]],
    valid_rel_paths: set[str],
) -> int:
    broken = 0
    for href in re.findall(r'href="([^"#]+)"', index_html):
        if href.startswith(("http://", "https://", "mailto:", "assets/")):
            continue
        if href not in valid_rel_paths:
            broken += 1
    for _person, p_html in rendered_persons:
        for href in re.findall(r'href="([^"#]+)"', p_html):
            if href.startswith(("http://", "https://", "mailto:", "../assets/")):
                continue
            if href == "../index.html":
                resolved = "index.html"
            elif href.startswith("../"):
                resolved = href[3:]
            else:
                resolved = f"persons/{href}"
            if resolved not in valid_rel_paths:
                broken += 1
    return broken


def build_public_site(
    *,
    club_card: Mapping[str, Any],
    person_cards: Sequence[Mapping[str, Any]],
    output_dir: str | Path,
    site_title: Optional[str] = None,
    observed_until: Optional[str] = None,
    base_url: Optional[str] = None,
    story_records: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build a PUBLIC_SAFE static HTML preview site with fail-closed validation.

    Validates every card (club + all persons), `sitemap.xml`, and `llms.txt`
    BEFORE creating `output_dir` or writing any file. Raises
    `ValueError("PUBLIC_SITE_VISIBILITY_VIOLATION: ...")` on any visibility/
    redaction failure, leaving 0 partial output files.
    """
    if not isinstance(person_cards, Sequence) or isinstance(person_cards, (str, bytes)):
        _fail_visibility("person_cards:must_be_sequence")

    if site_title is not None:
        _verify_public_text_strict(str(site_title), label="site_title")
    if observed_until is not None:
        _verify_public_text_strict(str(observed_until), label="observed_until")
    clean_base_url: Optional[str] = None
    if base_url is not None and str(base_url).strip():
        clean_base_url = str(base_url).strip()
        _verify_public_text_strict(clean_base_url, label="base_url")

    # 1. Fail-closed validation of ALL inputs before touching the filesystem.
    club_md, club_citations = _validate_card_payload(club_card, label="club_card")
    club_is_wiki_v241 = str(club_card.get("card_style") or "").strip() == "wiki_v241"

    validated_persons: List[Dict[str, Any]] = []
    used_slugs: set[str] = set()
    for idx, pcard in enumerate(person_cards):
        p_md, p_citations = _validate_card_payload(pcard, label=f"person_cards[{idx}]")
        slug = _derive_slug(pcard, index=idx, used=used_slugs)
        person_sub = pcard.get("person") if isinstance(pcard.get("person"), Mapping) else {}
        bio_raw = pcard.get("bio_profile")
        if hasattr(bio_raw, "to_dict") and callable(bio_raw.to_dict):
            bio_map = dict(bio_raw.to_dict())
        elif isinstance(bio_raw, Mapping):
            bio_map = dict(bio_raw)
        else:
            bio_map = None
        person_name = str(
            pcard.get("person_name")
            or pcard.get("canonical_name")
            or (bio_map.get("canonical_name") if bio_map else None)
            or person_sub.get("player_name")
            or pcard.get("person_uid")
            or slug
        ).strip()
        person_uid = str(
            pcard.get("person_uid")
            or (bio_map.get("person_uid") if bio_map else None)
            or person_sub.get("person_uid")
            or pcard.get("player_uid")
            or person_sub.get("player_uid")
            or slug
        ).strip()
        lead_plain = str(pcard.get("lead_plain_text") or "").strip()
        if not lead_plain and isinstance(pcard.get("lead_paragraph"), Mapping):
            lead_plain = str(pcard["lead_paragraph"].get("lead_plain_text") or "").strip()
        if lead_plain:
            _verify_public_text_strict(lead_plain, label=f"person_cards[{idx}].lead_plain_text")
        if bio_map is not None:
            _verify_mapping_values_public_safe(
                bio_map, label=f"person_cards[{idx}].bio_profile"
            )
        reg_map = (
            dict(pcard["registration_profile"])
            if isinstance(pcard.get("registration_profile"), Mapping)
            else None
        )
        if reg_map is not None:
            _verify_mapping_values_public_safe(
                reg_map, label=f"person_cards[{idx}].registration_profile"
            )
        validated_persons.append(
            {
                "person_uid": person_uid,
                "person_name": person_name,
                "slug": slug,
                "markdown": p_md,
                "citations": p_citations,
                "observed_until": str(pcard.get("observed_until") or "").strip(),
                "card_style": str(pcard.get("card_style") or "").strip(),
                "bio_profile": bio_map,
                "registration_profile": reg_map,
                "entity_role": str(pcard.get("entity_role") or "").strip(),
                "lead_plain_text": lead_plain,
                "projected_stories": list(pcard.get("projected_stories") or ()),
                "person": dict(person_sub) if person_sub else {},
            }
        )

    club_name = str(
        club_card.get("club_name") or club_card.get("canonical_name") or club_card.get("organization_id") or "Club Panorama"
    ).strip()
    org_id = str(club_card.get("organization_id") or "").strip()
    resolved_site_title = (site_title or f"{club_name} | PUBLIC_SAFE").strip()
    resolved_observed_until = str(
        observed_until or club_card.get("observed_until") or ""
    ).strip()

    # 2. Render all HTML, CSS, sitemap.xml, and llms.txt in memory before any disk I/O.
    club_entity_links: List[Tuple[str, str]] = [
        (p["person_name"], f"persons/{p['slug']}.html") for p in validated_persons
    ]
    club_seen_fnrefs: set[int] = set()
    club_body_html = _markdown_to_html(
        club_md,
        entity_links=club_entity_links,
        max_citation_index=len(club_citations),
        seen_fnrefs=club_seen_fnrefs,
        strip_references_section=bool(club_citations and club_is_wiki_v241),
    )
    club_ref_bar, club_footnotes_html = _render_footnotes_section(
        club_citations,
        has_inline_refs=bool(club_seen_fnrefs),
        is_wiki_v241=club_is_wiki_v241,
    )
    club_article_html = (
        '<article class="card-body">\n'
        f"{club_body_html}\n"
        f"{club_ref_bar}\n"
        "</article>"
    )

    person_directory_items = "\n".join(
        f'    <li><a href="persons/{html.escape(p["slug"], quote=True)}.html">'
        f'{html.escape(p["person_name"])}</a></li>'
        for p in validated_persons
    )
    person_directory_html = (
        '<section class="entity-index">\n'
        "  <h2>成员索引</h2>\n"
        '  <ul class="entity-list">\n'
        f"{person_directory_items}\n"
        "  </ul>\n"
        "</section>"
        if validated_persons
        else ""
    )

    club_json_ld: Dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "SportsTeam",
        "name": club_name,
    }
    if org_id:
        club_json_ld["identifier"] = org_id
    if resolved_observed_until:
        club_json_ld["dateModified"] = resolved_observed_until
        club_json_ld["additionalProperty"] = [
            {
                "@type": "PropertyValue",
                "name": "observed_until",
                "value": resolved_observed_until,
            }
        ]

    index_html = _render_html_document(
        title=f"{club_name} - {resolved_site_title}",
        site_title=resolved_site_title,
        css_href="assets/style.css",
        home_href="index.html",
        json_ld=club_json_ld,
        body_html="\n".join(
            part for part in (club_article_html, person_directory_html, club_footnotes_html) if part
        ),
        observed_until=resolved_observed_until,
        meta_description=f"{club_name} 公开百科与跨赛季阵容档案",
    )

    rendered_persons: List[Tuple[Dict[str, Any], str]] = []
    stories_for_rel = list(story_records or ())
    for person in validated_persons:
        p_obs = resolved_observed_until or person["observed_until"]
        p_is_wiki_v241 = person["card_style"] == "wiki_v241"
        co_entity_links: List[Tuple[str, str]] = [(club_name, "../index.html")]
        co_entity_links.extend(
            (other["person_name"], f"{other['slug']}.html")
            for other in validated_persons
            if other["slug"] != person["slug"]
        )
        p_seen_fnrefs: set[int] = set()
        p_body_html = _markdown_to_html(
            person["markdown"],
            entity_links=co_entity_links,
            max_citation_index=len(person["citations"]),
            seen_fnrefs=p_seen_fnrefs,
            strip_references_section=bool(person["citations"] and p_is_wiki_v241),
        )
        p_ref_bar, p_footnotes_html = _render_footnotes_section(
            person["citations"],
            has_inline_refs=bool(p_seen_fnrefs),
            is_wiki_v241=p_is_wiki_v241,
        )
        infobox_html = _render_wiki_infobox(person)
        p_article_html = (
            '<article class="card-body">\n'
            f"{infobox_html}\n"
            f"{p_body_html}\n"
            f"{p_ref_bar}\n"
            "</article>"
        )

        related_list = compute_related_persons(
            person,
            validated_persons,
            story_records=stories_for_rel,
            max_related=8,
        )
        related_items = "\n".join(
            f'    <li><a href="{html.escape(rel["slug"], quote=True)}.html">'
            f'{html.escape(rel["person_name"])}</a>'
            f'<span class="relation-badge">{html.escape(rel["relation_label"])}</span></li>'
            for rel in related_list
        )
        related_html = (
            '<section class="related-persons">\n'
            "  <h2>关联人物</h2>\n"
            '  <ul class="entity-list">\n'
            f"{related_items}\n"
            "  </ul>\n"
            "</section>"
            if related_list
            else ""
        )

        navbox_items = "\n".join(
            f'      <li><a href="{html.escape(other["slug"], quote=True)}.html">'
            f'{html.escape(other["person_name"])}</a></li>'
            for other in validated_persons
            if other["slug"] != person["slug"]
        )
        navbox_html = (
            '<nav class="squad-navbox">\n'
            "  <details>\n"
            f"    <summary>{html.escape(club_name)} 完整阵容导航（{len(validated_persons)}人）</summary>\n"
            '    <ul class="entity-list">\n'
            f'      <li><a href="../index.html">{html.escape(club_name)}</a></li>\n'
            f"{navbox_items}\n"
            "    </ul>\n"
            "  </details>\n"
            "</nav>"
        )

        breadcrumb_html = (
            '<nav class="breadcrumb">'
            f'<a href="../index.html">&larr; {html.escape(club_name)}</a>'
            "</nav>"
        )

        bio_map = person.get("bio_profile") or {}
        meta_desc = person["lead_plain_text"] or f"{person['person_name']} — {club_name}"
        person_json_ld: Dict[str, Any] = {
            "@context": "https://schema.org",
            "@type": "Person",
            "name": person["person_name"],
            "identifier": person["person_uid"],
            "affiliation": {
                "@type": "SportsTeam",
                "name": club_name,
            },
            "description": meta_desc,
        }
        job_title = str(
            bio_map.get("current_role")
            or bio_map.get("position")
            or person.get("entity_role")
            or ""
        ).strip()
        if _is_displayable_infobox_value(job_title):
            person_json_ld["jobTitle"] = job_title
        birth_date = str(bio_map.get("birth_date") or "").strip()
        if _is_displayable_infobox_value(birth_date):
            person_json_ld["birthDate"] = birth_date
        h_cm = bio_map.get("height_cm")
        if isinstance(h_cm, int) and not isinstance(h_cm, bool) and h_cm > 0:
            person_json_ld["height"] = {
                "@type": "QuantitativeValue",
                "value": h_cm,
                "unitCode": "CMT",
            }
        w_kg = bio_map.get("weight_kg")
        if isinstance(w_kg, int) and not isinstance(w_kg, bool) and w_kg > 0:
            person_json_ld["weight"] = {
                "@type": "QuantitativeValue",
                "value": w_kg,
                "unitCode": "KGM",
            }
        if person["citations"]:
            person_json_ld["citation"] = [
                c.get("url") or c.get("label")
                for c in person["citations"]
                if c.get("url") or c.get("label")
            ]
        if p_obs:
            person_json_ld["dateModified"] = p_obs
            person_json_ld["additionalProperty"] = [
                {
                    "@type": "PropertyValue",
                    "name": "observed_until",
                    "value": p_obs,
                }
            ]
        person_html = _render_html_document(
            title=f"{person['person_name']} - {resolved_site_title}",
            site_title=resolved_site_title,
            css_href="../assets/style.css",
            home_href="../index.html",
            json_ld=person_json_ld,
            body_html="\n".join(
                part
                for part in (p_article_html, related_html, navbox_html, p_footnotes_html)
                if part
            ),
            observed_until=p_obs,
            breadcrumb_html=breadcrumb_html,
            meta_description=meta_desc,
        )
        rendered_persons.append((person, person_html))

    css_text = _render_style_css()
    sitemap_xml = _build_sitemap_xml(
        base_url=clean_base_url,
        person_slugs=[p["slug"] for p in validated_persons],
        observed_until=resolved_observed_until,
    )
    llms_txt = _build_llms_txt(
        site_title=resolved_site_title,
        club_name=club_name,
        observed_until=resolved_observed_until,
        validated_persons=validated_persons,
        base_url=clean_base_url,
    )
    _verify_public_text_strict(index_html, label="index.html")
    for person, person_html in rendered_persons:
        _verify_public_text_strict(person_html, label=f"persons/{person['slug']}.html")
    _verify_public_text_strict(sitemap_xml, label="sitemap.xml")
    _verify_public_text_strict(llms_txt, label="llms.txt")

    valid_rel_paths = {"index.html", *(f"persons/{p['slug']}.html" for p in validated_persons)}
    broken_links_count = _count_broken_internal_links(
        index_html=index_html,
        rendered_persons=rendered_persons,
        valid_rel_paths=valid_rel_paths,
    )

    # 3. Write files only after all validation & rendering succeeded.
    out_root = Path(output_dir)
    assets_dir = out_root / "assets"
    persons_dir = out_root / "persons"
    assets_dir.mkdir(parents=True, exist_ok=True)
    persons_dir.mkdir(parents=True, exist_ok=True)

    index_path = out_root / "index.html"
    css_path = assets_dir / "style.css"
    sitemap_path = out_root / "sitemap.xml"
    llms_txt_path = out_root / "llms.txt"
    index_path.write_text(index_html, encoding="utf-8")
    css_path.write_text(css_text, encoding="utf-8")
    sitemap_path.write_text(sitemap_xml, encoding="utf-8")
    llms_txt_path.write_text(llms_txt, encoding="utf-8")

    person_pages: List[Dict[str, str]] = []
    for person, person_html in rendered_persons:
        p_path = persons_dir / f"{person['slug']}.html"
        p_path.write_text(person_html, encoding="utf-8")
        person_pages.append(
            {
                "person_uid": person["person_uid"],
                "person_name": person["person_name"],
                "slug": person["slug"],
                "path": str(p_path),
                "relative_url": f"persons/{person['slug']}.html",
            }
        )

    return {
        "status": "BUILT",
        "visibility": "public_safe",
        "output_dir": str(out_root),
        "site_title": resolved_site_title,
        "observed_until": resolved_observed_until,
        "index_path": str(index_path),
        "css_path": str(css_path),
        "sitemap_path": str(sitemap_path),
        "llms_txt_path": str(llms_txt_path),
        "broken_links_count": broken_links_count,
        "person_pages": person_pages,
        "page_count": 1 + len(person_pages),
    }
