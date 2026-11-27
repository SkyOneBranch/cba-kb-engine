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
_URL_RE = re.compile(r"https?://[^\s<>\"')]+")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_INTERNAL_CLUE_MARKER = "[internal_clue_only"
_TILDE_FENCE_MARKER = "~~~"


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
            if isinstance(item, Mapping):
                c_label = str(item.get("label") or item.get("title") or "").strip()
                c_url = str(item.get("url") or item.get("canonical_url") or "").strip()
            elif isinstance(item, str):
                c_label = item.strip()
                c_url = item.strip() if item.strip().startswith(("http://", "https://")) else ""
            else:
                continue

            if c_label:
                _verify_public_text_strict(c_label, label=f"{label}.citations[{idx}].label")
            if c_url:
                _verify_public_text_strict(c_url, label=f"{label}.citations[{idx}].url")
            if not c_label and not c_url:
                continue
            if c_url:
                seen_urls.add(c_url)
            result.append({"label": c_label or c_url, "url": c_url})

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


def _format_inline_markdown(
    text: str, entity_links: Sequence[Tuple[str, str]]
) -> str:
    """Convert inline markdown links and bare URLs safely, then auto-link entities."""
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
    return "".join(tokens)


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
    markdown: str, *, entity_links: Sequence[Tuple[str, str]] = ()
) -> str:
    lines = markdown.splitlines()
    blocks: List[str] = []
    in_list = False
    in_table = False

    def _close_open() -> None:
        nonlocal in_list, in_table
        if in_list:
            blocks.append("</ul>")
            in_list = False
        if in_table:
            blocks.append("</tbody></table>")
            in_table = False

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            _close_open()
            continue
        if stripped == "end_marker: true":
            _close_open()
            continue

        if stripped.startswith("#### "):
            _close_open()
            blocks.append(f"<h4>{_format_inline_markdown(stripped[5:], entity_links)}</h4>")
        elif stripped.startswith("### "):
            _close_open()
            blocks.append(f"<h3>{_format_inline_markdown(stripped[4:], entity_links)}</h3>")
        elif stripped.startswith("## "):
            _close_open()
            blocks.append(f"<h2>{_format_inline_markdown(stripped[3:], entity_links)}</h2>")
        elif stripped.startswith("# "):
            _close_open()
            blocks.append(f"<h1>{_format_inline_markdown(stripped[2:], entity_links)}</h1>")
        elif stripped.startswith("- ") or stripped.startswith("* "):
            if in_table:
                blocks.append("</tbody></table>")
                in_table = False
            if not in_list:
                blocks.append("<ul>")
                in_list = True
            blocks.append(f"<li>{_format_inline_markdown(stripped[2:], entity_links)}</li>")
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
                    blocks.append(f"<th>{_format_inline_markdown(cell, entity_links)}</th>")
                blocks.append("</tr></thead><tbody>")
                in_table = True
            else:
                blocks.append("<tr>")
                for cell in cells:
                    blocks.append(f"<td>{_format_inline_markdown(cell, entity_links)}</td>")
                blocks.append("</tr>")
        else:
            _close_open()
            blocks.append(f"<p>{_format_inline_markdown(stripped, entity_links)}</p>")

    _close_open()
    return "\n".join(blocks)


def _render_footnotes_section(citations: Sequence[Mapping[str, str]]) -> Tuple[str, str]:
    if not citations:
        return "", ""
    inline_refs: List[str] = []
    items: List[str] = []
    for idx, cite in enumerate(citations, start=1):
        label = html.escape(cite.get("label") or cite.get("url") or f"Source {idx}")
        url = cite.get("url") or ""
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
        '<p class="citation-refs">Sources: ' + " ".join(inline_refs) + "</p>"
    )
    section = (
        '<section class="footnotes" id="footnotes">\n'
        "  <h2>References</h2>\n"
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
footer.site-footer {
  padding: 0 1.5rem 2rem;
  font-size: 0.85rem;
  color: var(--muted);
}
"""


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
) -> str:
    esc_title = html.escape(title)
    esc_site = html.escape(site_title)
    esc_css = html.escape(css_href, quote=True)
    esc_home = html.escape(home_href, quote=True)
    esc_obs = html.escape(observed_until)
    meta_obs = f" | Observed until: {esc_obs}" if esc_obs else ""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
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


def build_public_site(
    *,
    club_card: Mapping[str, Any],
    person_cards: Sequence[Mapping[str, Any]],
    output_dir: str | Path,
    site_title: Optional[str] = None,
    observed_until: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a PUBLIC_SAFE static HTML preview site with fail-closed validation.

    Validates every card (club + all persons) BEFORE creating `output_dir` or
    writing any file. Raises `ValueError("PUBLIC_SITE_VISIBILITY_VIOLATION: ...")`
    on any visibility/redaction failure, leaving 0 partial output files.
    """
    if not isinstance(person_cards, Sequence) or isinstance(person_cards, (str, bytes)):
        _fail_visibility("person_cards:must_be_sequence")

    if site_title is not None:
        _verify_public_text_strict(str(site_title), label="site_title")
    if observed_until is not None:
        _verify_public_text_strict(str(observed_until), label="observed_until")

    # 1. Fail-closed validation of ALL inputs before touching the filesystem.
    club_md, club_citations = _validate_card_payload(club_card, label="club_card")

    validated_persons: List[Dict[str, Any]] = []
    used_slugs: set[str] = set()
    for idx, pcard in enumerate(person_cards):
        p_md, p_citations = _validate_card_payload(pcard, label=f"person_cards[{idx}]")
        slug = _derive_slug(pcard, index=idx, used=used_slugs)
        person_name = str(
            pcard.get("person_name") or pcard.get("canonical_name") or pcard.get("person_uid") or slug
        ).strip()
        person_uid = str(pcard.get("person_uid") or pcard.get("player_uid") or slug).strip()
        validated_persons.append(
            {
                "person_uid": person_uid,
                "person_name": person_name,
                "slug": slug,
                "markdown": p_md,
                "citations": p_citations,
                "observed_until": str(pcard.get("observed_until") or "").strip(),
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

    # 2. Render all HTML and CSS in memory before any disk I/O.
    club_entity_links: List[Tuple[str, str]] = [
        (p["person_name"], f"persons/{p['slug']}.html") for p in validated_persons
    ]
    club_ref_bar, club_footnotes_html = _render_footnotes_section(club_citations)
    club_article_html = (
        '<article class="card-body">\n'
        f"{_markdown_to_html(club_md, entity_links=club_entity_links)}\n"
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
        "  <h2>Persons</h2>\n"
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
    )

    rendered_persons: List[Tuple[Dict[str, Any], str]] = []
    for person in validated_persons:
        p_obs = resolved_observed_until or person["observed_until"]
        co_entity_links: List[Tuple[str, str]] = [(club_name, "../index.html")]
        co_entity_links.extend(
            (other["person_name"], f"{other['slug']}.html")
            for other in validated_persons
            if other["slug"] != person["slug"]
        )
        p_ref_bar, p_footnotes_html = _render_footnotes_section(person["citations"])
        p_article_html = (
            '<article class="card-body">\n'
            f"{_markdown_to_html(person['markdown'], entity_links=co_entity_links)}\n"
            f"{p_ref_bar}\n"
            "</article>"
        )
        other_items = "\n".join(
            f'    <li><a href="{html.escape(other["slug"], quote=True)}.html">'
            f'{html.escape(other["person_name"])}</a></li>'
            for other in validated_persons
            if other["slug"] != person["slug"]
        )
        related_html = (
            '<section class="entity-index">\n'
            "  <h2>Related Entities</h2>\n"
            '  <ul class="entity-list">\n'
            f'    <li><a href="../index.html">{html.escape(club_name)}</a></li>\n'
            f"{other_items}\n"
            "  </ul>\n"
            "</section>"
        )
        breadcrumb_html = (
            '<nav class="breadcrumb">'
            f'<a href="../index.html">&larr; {html.escape(club_name)}</a>'
            "</nav>"
        )
        person_json_ld: Dict[str, Any] = {
            "@context": "https://schema.org",
            "@type": "Person",
            "name": person["person_name"],
            "identifier": person["person_uid"],
            "affiliation": {
                "@type": "SportsTeam",
                "name": club_name,
            },
        }
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
                part for part in (p_article_html, related_html, p_footnotes_html) if part
            ),
            observed_until=p_obs,
            breadcrumb_html=breadcrumb_html,
        )
        rendered_persons.append((person, person_html))

    css_text = _render_style_css()

    # 3. Write files only after all validation & rendering succeeded.
    out_root = Path(output_dir)
    assets_dir = out_root / "assets"
    persons_dir = out_root / "persons"
    assets_dir.mkdir(parents=True, exist_ok=True)
    persons_dir.mkdir(parents=True, exist_ok=True)

    index_path = out_root / "index.html"
    css_path = assets_dir / "style.css"
    index_path.write_text(index_html, encoding="utf-8")
    css_path.write_text(css_text, encoding="utf-8")

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
        "person_pages": person_pages,
        "page_count": 1 + len(person_pages),
    }
