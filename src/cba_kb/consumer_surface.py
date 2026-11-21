"""Self-describing AI consumer surface exporters, redaction, and slice promotion (REQ-215)."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from .kc_consume import ConsumeSession, _content_digest
from .kc_passages import evidence_chain
from .kc_render import (
    research_card_consumer_filename,
    slice_consumer_filename,
)
from .kc_snapshot import build_fact_guarantee, build_snapshot

_DRIVE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_.-])[A-Za-z0-9_-]{19,64}(?![A-Za-z0-9_.-])")
_DRIVE_PREFIXED_ID_RE = re.compile(r"\bdrive:[A-Za-z0-9_-]{4,}")
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_DIGITS_RE = re.compile(r"^\d+$")
_SHORT_SOURCE_ID_RE = re.compile(r"^src:[0-9a-f]{12}$")

_DRIVE_HOST = "drive" + ".google." + "com"
_DOCS_HOST = "docs" + ".google." + "com"
_DRIVE_URL_RE = re.compile(
    r"https?://(?:" + re.escape(_DRIVE_HOST) + r"|" + re.escape(_DOCS_HOST) + r")/[^\s)\]<>\"']+"
)
_LOCAL_PATH_RE = re.compile(
    r"(?:file://)?(?<![A-Za-z0-9_.:/])/(?:Users|home)/[^\s)\]<>\"',;]+"
)

CONSUMER_MASTER_FILENAME = "CBA_国内球员注册权威总表_2017-2027_CURRENT.csv"
CONSUMER_DICTIONARY_FILENAME = "CBA_核心数据表字段字典与权威口径_CURRENT.md"
CONSUMER_SOURCE_REGISTRY_FILENAME = "CBA_来源注册索引_脱敏视图_CURRENT.csv"
CONSUMER_DOC_INDEX_FILENAME = "CBA_文档索引_脱敏视图_CURRENT.json"
COLLECTOR_DIGEST_FILENAME = "CBA_官方采集动态与注册增量摘要_CURRENT.md"

DEFAULT_MAX_EXTERNAL_CITATIONS_PER_CARD = 10

_SOURCE_TIER_RANK = {
    "T1": 0,
    "T1.5": 1,
    "T2": 2,
    "MANUAL": 3,
}
_COMPLETENESS_RANK = {
    "full": 0,
    "partial": 1,
    "metadata_only": 2,
}


class _DescStr:
    """Wrapper for descending lexicographic string ordering in sort keys."""

    __slots__ = ("val",)

    def __init__(self, val: str) -> None:
        self.val = val

    def __lt__(self, other: "_DescStr") -> bool:
        return self.val > other.val

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _DescStr) and self.val == other.val


def _published_at_sort_key(value: Any) -> tuple[int, _DescStr]:
    if value is None:
        return (1, _DescStr(""))
    if hasattr(value, "isoformat") and callable(value.isoformat):
        text = str(value.isoformat()).strip()
    elif isinstance(value, str):
        text = value.strip()
    else:
        text = ""
    if not text:
        return (1, _DescStr(""))
    return (0, _DescStr(text))


def select_external_citations(
    citations: Sequence[Dict[str, Any]],
    *,
    max_external_citations_per_card: int = DEFAULT_MAX_EXTERNAL_CITATIONS_PER_CARD,
) -> Dict[str, Any]:
    """Deterministically rank and cap external citations for a player research card.

    Rank order:
      1. source_tier: T1 > T1.5 > T2 > MANUAL > other
      2. completeness: full > partial > metadata_only > other
      3. published_at descending (None / empty sorts last)
      4. doc_id ascending
    """
    if (
        isinstance(max_external_citations_per_card, bool)
        or not isinstance(max_external_citations_per_card, int)
        or max_external_citations_per_card < 1
    ):
        raise ValueError("MAX_EXTERNAL_CITATIONS_PER_CARD_INVALID")

    items = [dict(c) for c in (citations or ()) if isinstance(c, dict)]

    def _sort_key(c: Dict[str, Any]) -> tuple[int, int, tuple[int, _DescStr], str]:
        tier = str(c.get("source_tier") or "")
        comp = str(c.get("completeness") or "")
        doc_id = str(c.get("doc_id") or "")
        return (
            _SOURCE_TIER_RANK.get(tier, 4),
            _COMPLETENESS_RANK.get(comp, 3),
            _published_at_sort_key(c.get("published_at")),
            doc_id,
        )

    ranked = sorted(items, key=_sort_key)
    selected = ranked[:max_external_citations_per_card]
    total_linked = len(ranked)
    displayed = len(selected)
    return {
        "selected": selected,
        "total_linked_count": total_linked,
        "displayed_count": displayed,
        "truncated": total_linked > displayed,
        "max_external_citations_per_card": max_external_citations_per_card,
    }

_REDACT_JSON_KEYS = frozenset({
    "drive_file_id",
    "file_id",
    "current_parent_id",
    "parent_id",
    "folder_id",
    "root_folder_id",
    "source_file_id",
    "web_view_link",
    "webViewLink",
    "drive_url",
})


def _is_raw_drive_token(token: str) -> bool:
    if not token or _HEX40_RE.match(token) or _HEX64_RE.match(token) or _DIGITS_RE.match(token):
        return False
    if token.startswith("1") and len(token) >= 25:
        return True
    if token.startswith("0") and len(token) >= 19 and any(c.isupper() for c in token) and any(c.islower() for c in token):
        return True
    if (
        len(token) >= 25
        and token.count("_") <= 2
        and any(c.isupper() for c in token)
        and any(c.islower() for c in token)
        and any(c.isdigit() for c in token)
    ):
        return True
    return False


def _short_logical_source_id(raw_id: str) -> str:
    return "src:" + hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:12]


def _normalize_provenance_ids(known_drive_ids: Sequence[str]) -> list[str]:
    cleaned: set[str] = set()
    for raw in known_drive_ids or ():
        if not isinstance(raw, str):
            continue
        tok = raw.strip()
        if not tok:
            continue
        if tok.startswith("drive:"):
            tok = tok[len("drive:"):].strip()
        if not tok or len(tok) < 6:
            continue
        if _HEX40_RE.match(tok) or _HEX64_RE.match(tok) or _DIGITS_RE.match(tok):
            continue
        cleaned.add(tok)
    return sorted(cleaned, key=len, reverse=True)


def _compile_provenance_pattern(
    known_drive_ids: Sequence[str],
) -> Optional[re.Pattern[str]]:
    provenance_tokens = _normalize_provenance_ids(known_drive_ids)
    if not provenance_tokens:
        return None
    return re.compile(
        r"(?<![A-Za-z0-9_-])(?:"
        + "|".join(re.escape(tok) for tok in provenance_tokens)
        + r")(?![A-Za-z0-9_-])"
    )


def _scrub_with_pattern(
    text: str, prov_re: Optional[re.Pattern[str]]
) -> str:
    if not isinstance(text, str) or not text:
        return ""
    scrubbed = _DRIVE_URL_RE.sub("<redacted-drive-url>", text)
    scrubbed = _DRIVE_PREFIXED_ID_RE.sub("<redacted-drive-id>", scrubbed)
    scrubbed = _LOCAL_PATH_RE.sub("<redacted-local-path>", scrubbed)
    if prov_re is not None:
        scrubbed = prov_re.sub("<redacted-drive-id>", scrubbed)

    def _replace_tok(match: re.Match[str]) -> str:
        tok = match.group(0)
        if _is_raw_drive_token(tok):
            return "<redacted-drive-id>"
        return tok

    return _DRIVE_TOKEN_RE.sub(_replace_tok, scrubbed)


def scrub_raw_drive_ids_for_consumer(
    text: str,
    *,
    known_drive_ids: Sequence[str] = (),
) -> str:
    """Redact raw Drive IDs (provenance-known + alternate shapes) and Drive URLs.

    Preserves 40-hex Git SHAs, 64-hex SHA-256 digests, filenames, and ordinary numbers.
    """
    return _scrub_with_pattern(text, _compile_provenance_pattern(known_drive_ids))


def _require_positive_master_row_count(master_row_count: object) -> int:
    """Require an exact positive integer row count; fail closed on missing/bool/zero."""
    if (
        isinstance(master_row_count, bool)
        or not isinstance(master_row_count, int)
        or master_row_count <= 0
    ):
        raise ValueError("MASTER_ROW_COUNT_REQUIRED")
    return master_row_count


def compute_master_row_count(data: bytes, *, is_xlsx: bool = False) -> int:
    """Compute exact data-row count from MASTER CSV or XLSX bytes at build time."""
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("MASTER_BYTES_REQUIRED")
    raw = bytes(data)
    if is_xlsx:
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        try:
            if "MASTER" not in wb.sheetnames:
                raise ValueError("MASTER_SHEET_MISSING")
            ws = wb["MASTER"]
            rows_iter = ws.values
            header = next(rows_iter, None)
            if not header or not any(
                cell is not None and (not isinstance(cell, str) or cell.strip())
                for cell in header
            ):
                raise ValueError("MASTER_ROW_COUNT_EMPTY")
            count = sum(
                1
                for row in rows_iter
                if any(
                    cell is not None and (not isinstance(cell, str) or cell.strip())
                    for cell in row
                )
            )
        finally:
            wb.close()
    else:
        text = raw.decode("utf-8-sig")
        reader = csv.reader(io.StringIO(text))
        header = next(reader, None)
        if not header or not any(cell.strip() for cell in header):
            raise ValueError("MASTER_ROW_COUNT_EMPTY")
        count = sum(1 for row in reader if any(cell.strip() for cell in row))
    if count <= 0:
        raise ValueError("MASTER_ROW_COUNT_EMPTY")
    return count


def _should_include_collector_digest(
    release_id: str, include_collector_digest: Optional[bool]
) -> bool:
    if include_collector_digest is not None:
        return bool(include_collector_digest)
    return isinstance(release_id, str) and release_id.startswith("v2.2")


def render_consumer_surface_guide_section(
    *,
    release_id: str,
    master_row_count: int,
    include_collector_digest: Optional[bool] = None,
) -> str:
    """Render standardized consumer-surface guide block with exact dynamic MASTER count."""
    exact_count = _require_positive_master_row_count(master_row_count)
    count_label = f"{exact_count:,} 条"
    lines = [
        "## 当前生产权威消费面导览 (CURRENT Canonical Surface)",
        "",
        f"- 权威发布标识：`{release_id}`（仅读取处于 `COMPLETE` 状态且位于 `current` 消费区的 `CURRENT` / 权威产物；`90_archive_历史归档`、回收站与关闭沙盒绝非当前真值）。",
        f"- 当前 `MASTER` 国内球员注册总表数据行数：**{count_label}**（由构建期自动统计；历史迁移文档中提及的 `3,451` / `3451` 仅为 `2026-09-10` 早期 v1.1 载体对照基线，已由当前动态行数取代）。",
        "- **自描述核心消费主表与字典**（面向全盘中文检索优先读取）：",
        f"  - `{CONSUMER_MASTER_FILENAME}`（对应底层 `derived/MASTER.csv` / `input/MASTER.xlsx`，全量国内球员注册权威总表）",
        f"  - `{CONSUMER_DICTIONARY_FILENAME}`（核心数据表字段字典、缩写释义与权威口径说明）",
        f"  - `{CONSUMER_SOURCE_REGISTRY_FILENAME}`（来源文件注册脱敏索引，隐去底层物理 `drive_file_id`，保留逻辑 `source_id` 与可读文件名）",
    ]
    if _should_include_collector_digest(release_id, include_collector_digest):
        lines.extend([
            f"  - `{CONSUMER_DOC_INDEX_FILENAME}`（已合并最新受控采集文档索引的脱敏视图，隐去物理 Drive ID、`file_id` 与本地路径）",
            f"  - `{COLLECTOR_DIGEST_FILENAME}`（`evidence / candidate_observation` 层级官方采集动态与注册增量摘要：含 2026-2027 外籍球员注册快照、近三赛季国内注册与跨季增量比对及官方公告摘要；**遵守 `Statement != Fact. Evidence != Fact. Unknown != 0.`，不得直接视作已晋升的 `canonical` 事实**）",
        ])
    lines.extend([
        "- **人物研究切片（`CURRENT` 正式入列）**：正式上线的已授权球员研究卡采用 `研究卡_<中文姓名>_<球员ID>_<赛季>_CURRENT.md`（及对应 `研究切片_..._CURRENT.md`、`研究摘录_..._CURRENT.md`）命名并置于 `current`；全联盟通用赛季技术统计总表与球员人民币精确薪资未在公开事实层发布，未入列对象一律按 `UNKNOWN` 诚实拒答（`Unknown != 0`）。",
        "- **关键字段语义消歧（严禁跨表混淆）**：",
        "  - `date_year_inferred` 属于事件表 `facts/CBA_球员注册_EVENTS.xlsx`（工作表 `registration_status_events`）的**事件行级布尔字段**：仅在跨季中段“取消注册”等仅写月日、年份由赛季边界推导时为 `True`。",
        "  - `source_registry`（及脱敏索引 `CBA_来源注册索引_脱敏视图_CURRENT.csv`）中的 `season`、`source_title` 与 `discovered_at_utc` 属于**来源文件级元数据**，与事件行级 `date_year_inferred` 相互独立，不得用来源文件字段替代或否定事件行级推导标记。",
    ])
    return "\n".join(lines)


def render_consumer_field_dictionary_md(
    *,
    release_id: str,
    engine_sha: str,
    master_row_count: int,
    include_collector_digest: Optional[bool] = None,
) -> bytes:
    """Generate the self-describing CURRENT consumer field dictionary Markdown document."""
    exact_count = _require_positive_master_row_count(master_row_count)
    include_digest = _should_include_collector_digest(release_id, include_collector_digest)
    lines = [
        f"# CBA-KB 核心数据表字段字典与权威口径（{release_id} / CURRENT）",
        "",
        f"- **发布版本**: `{release_id}`",
        f"- **代码基线 Commit**: `{engine_sha}`",
        f"- **国内球员注册权威总表 (`MASTER`) 动态构建行数**: **{exact_count:,} 条**（历史文档中的 `3,451` 仅指 `2026-09-10` v1.1 迁移对照基线，非当前实时行数）。",
        "- **核心原则**: `Statement != Fact. Evidence != Fact. Unknown != 0.`",
        "",
        "## 1. 权威产物路由与命名规范（CURRENT Canonical Surface）",
        "",
        "| 消费文件名 / 路径 | 权威层级 | 覆盖范围与用途 |",
        "| --- | --- | --- |",
        f"| `{CONSUMER_MASTER_FILENAME}` (`derived/MASTER.csv`, `input/MASTER.xlsx`) | `canonical` | 国内球员赛季注册权威总表（{exact_count:,} 行，覆盖 2017–2027） |",
        "| `facts/CBA_注册领域_六表.xlsx` | `canonical` | 注册域结构化六表（国内注册、交易窗口、外援优先权与注册快照、状态事件） |",
        "| `facts/CBA_球员注册_EVENTS.xlsx` | `canonical` | 全量注册状态变更与跨季事件表（`registration_status_events`） |",
        "| `facts/CBA_外籍球员注册_SNAPSHOTS.xlsx` | `canonical` | 外籍球员官方注册快照 |",
        f"| `{CONSUMER_SOURCE_REGISTRY_FILENAME}` | `control / metadata` | 来源文件注册脱敏索引（隐藏原始物理 Drive ID，保留逻辑 `source_id` 与可读标题） |",
    ]
    if include_digest:
        lines.extend([
            f"| `{CONSUMER_DOC_INDEX_FILENAME}` | `control / metadata` | 官方文档脱敏索引（含 `consumed_bundles` 合并视图，隐去原始 Drive ID、`file_id` 与本地路径） |",
            f"| `{COLLECTOR_DIGEST_FILENAME}` | `evidence / candidate_observation` | 受控官方采集动态与注册增量摘要（2026-2027 外籍注册快照、三赛季国内注册与跨季 `25-26 -> 26-27` 增量观察、官方公告摘录；`Statement != Fact. Evidence != Fact. Unknown != 0.`） |",
        ])
    lines.extend([
        "| `研究卡_<中文姓名>_<球员ID>_<赛季>_CURRENT.md` | `research` | 已获授权并正式入列 `current` 的人物技术统计与多源证据研究卡（仅限已入列切片，未入列球员统计与精确薪资均属 `UNKNOWN`） |",
        "",
        "## 2. 易混淆字段明确消歧",
        "",
        "| 字段名 | 所属产物与粒度 | 真实语义 | 常见误读警示 |",
        "| --- | --- | --- | --- |",
        "| `date_year_inferred` | `facts/CBA_球员注册_EVENTS.xlsx#registration_status_events`（**事件行级**） | `True` 表示该行事件日期（如外援取消注册仅记月日）的年份系依据赛季起止边界推导得出；`False` 表示原始来源明确写出年份。 | **禁止**在 `source_registry` 文件清单中寻找该字段，或以文件年份推断覆盖事件行级 `date_year_inferred`。 |",
        "| `season` / `discovered_at_utc` | `CBA_来源注册索引_脱敏视图_CURRENT.csv`（**来源文件级**） | 描述原始公告/PDF/报道文件的归属赛季与采集入库时间。 | 仅描述源文件元数据，不代表具体事件行的年份推导属性。 |",
        "| `verification_level` | `MASTER`（**注册记录级**） | 注册条目机器核验等级（如 `auto_validated` 仅表示通过确定性规则核验，不等同于人工复核）。 | 不得将空值或 `UNKNOWN` 解释为 `0`。 |",
        "",
        "## 3. 全盘检索优先规则",
        "",
        "1. 在全盘搜索模式下，仅采信文件名带 `CURRENT` 或位于正式 `current` 消费区的权威主表与研究卡。",
        "2. 历史快照仅保留于受控备份归档，不得作为当前事实出处引用。",
    ])
    if include_digest:
        lines.append(
            f"3. `{COLLECTOR_DIGEST_FILENAME}` 属于 `evidence / candidate_observation` 证据观察层，引用时须显式区分官方公告/候选项与 `canonical` 主表事实（`Statement != Fact. Evidence != Fact. Unknown != 0.`）。"
        )
    lines.extend([
        "",
        "end_marker: true",
    ])
    return ("\n".join(lines) + "\n").encode("utf-8")


def export_consumer_master_csv(
    raw_csv_bytes: bytes, *, known_drive_ids: Sequence[str] = ()
) -> bytes:
    """Create self-describing CURRENT MASTER CSV copy with provenance-based ID redaction."""
    text = raw_csv_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("MASTER_CSV_HEADER_REQUIRED")
    fieldnames = list(reader.fieldnames)
    rows = list(reader)
    provenance_ids: set[str] = set(known_drive_ids or ())
    for row in rows:
        sfid = (row.get("source_file_id") or "").strip()
        if sfid:
            if sfid.startswith("drive:"):
                sfid = sfid[len("drive:"):].strip()
            if sfid and not _SHORT_SOURCE_ID_RE.match(sfid):
                provenance_ids.add(sfid)
    prov_re = _compile_provenance_pattern(sorted(provenance_ids))

    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        cleaned_row: Dict[str, Any] = {}
        for col in fieldnames:
            val = row.get(col, "")
            if col == "source_file_id" and isinstance(val, str) and val.strip():
                raw_sfid = val.strip()
                if _SHORT_SOURCE_ID_RE.match(raw_sfid):
                    cleaned_row[col] = raw_sfid
                else:
                    if raw_sfid.startswith("drive:"):
                        raw_sfid = raw_sfid[len("drive:"):].strip()
                    cleaned_row[col] = (
                        _short_logical_source_id(raw_sfid)
                        if raw_sfid
                        else _scrub_with_pattern(val, prov_re)
                    )
            elif isinstance(val, str):
                cleaned_row[col] = _scrub_with_pattern(val, prov_re)
            else:
                cleaned_row[col] = val
        writer.writerow(cleaned_row)
    return out.getvalue().encode("utf-8-sig")


def export_consumer_source_registry_csv(
    raw_csv_bytes: bytes, *, known_drive_ids: Sequence[str] = ()
) -> bytes:
    """Export AI consumer view of source_registry.csv with provenance-based ID removal."""
    text = raw_csv_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("SOURCE_REGISTRY_HEADER_REQUIRED")
    drop_cols = {
        "drive_file_id",
        "current_parent_id",
        "parent_id",
        "folder_id",
        "root_folder_id",
        "web_view_link",
        "webViewLink",
    }
    kept_cols = [col for col in reader.fieldnames if col not in drop_cols]
    if not kept_cols:
        raise ValueError("SOURCE_REGISTRY_NO_KEPT_COLUMNS")
    rows = list(reader)
    provenance_ids: set[str] = set(known_drive_ids or ())
    row_raw_drive_ids: list[Optional[str]] = []
    for row in rows:
        raw_fid = (row.get("drive_file_id") or "").strip() or None
        row_raw_drive_ids.append(raw_fid)
        for dcol in drop_cols:
            dval = (row.get(dcol) or "").strip()
            if dval:
                provenance_ids.add(dval)
        sid_val = (row.get("source_id") or "").strip()
        if sid_val.startswith("drive:"):
            raw_sid = sid_val[len("drive:"):].strip()
            if raw_sid:
                provenance_ids.add(raw_sid)
    prov_re = _compile_provenance_pattern(sorted(provenance_ids))

    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=kept_cols, lineterminator="\n")
    writer.writeheader()
    for row, raw_fid in zip(rows, row_raw_drive_ids):
        out_row: Dict[str, str] = {}
        for col in kept_cols:
            val = row.get(col) or ""
            if col == "source_id":
                stripped = val.strip()
                if stripped.startswith("drive:"):
                    raw_id = stripped[len("drive:"):].strip()
                    val = _short_logical_source_id(raw_id) if raw_id else stripped
                elif raw_fid and stripped == raw_fid and not _SHORT_SOURCE_ID_RE.match(stripped):
                    val = _short_logical_source_id(raw_fid)
                else:
                    val = _scrub_with_pattern(stripped, prov_re)
            else:
                val = _scrub_with_pattern(val, prov_re)
            out_row[col] = val
        writer.writerow(out_row)
    return out.getvalue().encode("utf-8")


def _collect_json_provenance_ids(obj: Any, out_ids: list[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _REDACT_JSON_KEYS and isinstance(v, str) and v.strip():
                out_ids.append(v.strip())
            else:
                _collect_json_provenance_ids(v, out_ids)
    elif isinstance(obj, list):
        for item in obj:
            _collect_json_provenance_ids(item, out_ids)


def _scrub_json_obj(obj: Any, *, prov_re: Optional[re.Pattern[str]] = None) -> Any:
    if isinstance(obj, dict):
        return {
            k: _scrub_json_obj(v, prov_re=prov_re)
            for k, v in obj.items()
            if k not in _REDACT_JSON_KEYS
        }
    if isinstance(obj, list):
        return [_scrub_json_obj(item, prov_re=prov_re) for item in obj]
    if isinstance(obj, str):
        return _scrub_with_pattern(obj, prov_re)
    return obj


def export_consumer_document_index(
    raw_index_bytes: bytes, *, known_drive_ids: Sequence[str] = ()
) -> bytes:
    """Export normalized document index JSON with provenance-based ID & URL redaction."""
    parsed = json.loads(raw_index_bytes.decode("utf-8"))
    provenance_ids: list[str] = list(known_drive_ids or ())
    _collect_json_provenance_ids(parsed, provenance_ids)
    prov_re = _compile_provenance_pattern(provenance_ids)
    scrubbed = _scrub_json_obj(parsed, prov_re=prov_re)
    return (json.dumps(scrubbed, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _extract_consumed_bundle_items(operational_checkpoint: Dict[str, Any]) -> list[Dict[str, Any]]:
    raw_bundles = (operational_checkpoint or {}).get("consumed_bundles")
    items: list[Dict[str, Any]] = []
    if isinstance(raw_bundles, dict):
        for rel_key in sorted(raw_bundles):
            entry = raw_bundles[rel_key]
            if isinstance(entry, dict):
                copied = dict(entry)
                copied.setdefault("_bundle_key", str(rel_key))
                items.append(copied)
    elif isinstance(raw_bundles, list):
        for entry in raw_bundles:
            if isinstance(entry, dict):
                items.append(dict(entry))
    items.sort(key=lambda d: (str(d.get("doc_id") or ""), str(d.get("_bundle_key") or "")))
    return items


def merge_operational_docs_into_consumer_index(
    existing_index_bytes: Optional[bytes],
    operational_checkpoint: Dict[str, Any],
    *,
    known_drive_ids: Sequence[str] = (),
) -> bytes:
    """Idempotently merge operational checkpoint consumed_bundles into the consumer document index."""
    if existing_index_bytes and existing_index_bytes.strip():
        parsed = json.loads(existing_index_bytes.decode("utf-8"))
        if not isinstance(parsed, dict):
            parsed = {"schema_version": 1, "documents": []}
    else:
        parsed = {"schema_version": 1, "documents": []}

    provenance_ids: list[str] = list(known_drive_ids or ())
    _collect_json_provenance_ids(parsed, provenance_ids)
    _collect_json_provenance_ids(operational_checkpoint, provenance_ids)

    raw_docs = parsed.get("documents")
    existing_docs = list(raw_docs) if isinstance(raw_docs, list) else []

    merged_docs: list[Dict[str, Any]] = []
    index_by_doc_id: Dict[str, int] = {}
    for doc in existing_docs:
        if not isinstance(doc, dict):
            continue
        cleaned = dict(doc)
        did = str(cleaned.get("doc_id") or "").strip()
        if did:
            if did in index_by_doc_id:
                continue
            index_by_doc_id[did] = len(merged_docs)
        merged_docs.append(cleaned)

    for bundle in _extract_consumed_bundle_items(operational_checkpoint):
        doc_id = str(bundle.get("doc_id") or "").strip()
        if not doc_id:
            continue
        raw_sha = bundle.get("raw_sha256") or bundle.get("file_sha256")
        persons = bundle.get("matched_persons")
        evidence_fields: Dict[str, Any] = {
            "doc_id": doc_id,
            "title": bundle.get("title"),
            "canonical_url": bundle.get("canonical_url"),
            "source_tier": bundle.get("source_tier"),
            "completeness": bundle.get("completeness"),
            "published_at": bundle.get("published_at"),
            "content_hash": bundle.get("content_hash"),
            "raw_sha256": raw_sha,
            "file_sha256": bundle.get("file_sha256"),
            "matched_persons": list(persons) if isinstance(persons, list) else [],
            "disclosure_posture": bundle.get("disclosure_posture"),
            "layer": "evidence",
        }
        if doc_id in index_by_doc_id:
            existing_entry = dict(merged_docs[index_by_doc_id[doc_id]])
            existing_entry.update(evidence_fields)
            merged_docs[index_by_doc_id[doc_id]] = existing_entry
        else:
            index_by_doc_id[doc_id] = len(merged_docs)
            merged_docs.append(evidence_fields)

    parsed["schema_version"] = parsed.get("schema_version") or 1
    parsed["documents"] = merged_docs
    parsed["unique_document_count"] = len(merged_docs)
    payload = json.dumps(parsed, ensure_ascii=False).encode("utf-8")
    return export_consumer_document_index(payload, known_drive_ids=provenance_ids)


def _md_cell(value: Any) -> str:
    if value is None:
        return "—"
    text = str(value).strip()
    if not text:
        return "—"
    return text.replace("\r", " ").replace("\n", " ").replace("|", "\\|")


def _classify_digest_bundle(bundle: Dict[str, Any]) -> str:
    title = str(bundle.get("title") or "")
    body = str(bundle.get("body") or "")
    if "外籍球员注册" in title or ("外籍球员注册信息" in body and "| 俱乐部 |" in body):
        return "foreign_registration"
    if "国内球员注册信息" in title:
        return "domestic_registration"
    return "announcement"


def _bundle_sort_key(bundle: Dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(bundle.get("doc_id") or ""),
        str(bundle.get("published_at") or ""),
        str(bundle.get("canonical_url") or ""),
    )


def _render_digest_reg_diff_section(reg_diff: Dict[str, Any]) -> list[str]:
    summary = reg_diff.get("summary") or {}
    old_b = reg_diff.get("old_bundle") or {}
    new_b = reg_diff.get("new_bundle") or {}
    notice = str(
        reg_diff.get("statement_notice")
        or "Candidate diff derived from official registration pages. Statement != Fact. Evidence != Fact. Unknown != 0."
    ).strip()

    lines = [
        "### 2.2 跨季国内球员注册增量观察（2025-2026 → 2026-2027）",
        "",
        f"> **声明边界**: {notice}",
        "",
        f"- **旧赛季基线**: `{old_b.get('title') or '2025-2026'}`（俱乐部 {old_b.get('clubs_count', '—')} 家，球员 {old_b.get('players_count', '—')} 人）",
        f"- **新赛季快照**: `{new_b.get('title') or '2026-2027'}`（俱乐部 {new_b.get('clubs_count', '—')} 家，球员 {new_b.get('players_count', '—')} 人）",
        (
            f"- **增量统计**: 新增 `{summary.get('added_count', 0)}` 人、"
            f"移除 `{summary.get('removed_count', 0)}` 人、"
            f"转会/俱乐部变更 `{summary.get('transferred_count', 0)}` 人、"
            f"合同字段变更 `{summary.get('contract_changed_count', 0)}` 人"
        ),
        "",
    ]

    transferred = reg_diff.get("transferred_players") or []
    lines.append(f"#### 转会 / 俱乐部变更观察（共 {len(transferred)} 条）")
    lines.append("")
    if transferred:
        lines.extend([
            "| 球员 | 原俱乐部 | 新俱乐部 | 原合同类别 | 新合同类别 |",
            "| --- | --- | --- | --- | --- |",
        ])
        for row in transferred:
            lines.append(
                f"| {_md_cell(row.get('player'))} | {_md_cell(row.get('from_club'))} | "
                f"{_md_cell(row.get('to_club'))} | {_md_cell(row.get('old_contract_type'))} | "
                f"{_md_cell(row.get('new_contract_type'))} |"
            )
    else:
        lines.append("- （无转会记录）")
    lines.append("")

    contract_changed = reg_diff.get("contract_changed_players") or []
    lines.append(f"#### 合同与注册属性变更观察（共 {len(contract_changed)} 条）")
    lines.append("")
    if contract_changed:
        lines.extend([
            "| 球员 | 俱乐部 | 变更字段明细 |",
            "| --- | --- | --- |",
        ])
        for row in contract_changed:
            changed_fields = row.get("changed_fields") or {}
            detail_parts = []
            for fname in sorted(changed_fields):
                delta = changed_fields[fname] or {}
                detail_parts.append(f"{fname}: {delta.get('old', '—')} → {delta.get('new', '—')}")
            lines.append(
                f"| {_md_cell(row.get('player'))} | {_md_cell(row.get('club'))} | "
                f"{_md_cell('；'.join(detail_parts))} |"
            )
    else:
        lines.append("- （无合同字段变更记录）")
    lines.append("")

    added = reg_diff.get("added_players") or []
    lines.append(f"#### 新增注册观察（共 {len(added)} 条）")
    lines.append("")
    if added:
        lines.extend([
            "| 球员 | 俱乐部 | 关键字段摘要 |",
            "| --- | --- | --- |",
        ])
        for row in added:
            fields = row.get("fields") or {}
            summary_bits = [
                f"{k}={v}" for k, v in fields.items()
                if k in ("注册类型", "注册子类型", "合同类别", "合同到期日") and v
            ]
            lines.append(
                f"| {_md_cell(row.get('player'))} | {_md_cell(row.get('club'))} | "
                f"{_md_cell('；'.join(summary_bits))} |"
            )
    else:
        lines.append("- （无新增注册记录）")
    lines.append("")

    removed = reg_diff.get("removed_players") or []
    lines.append(f"#### 移出名单观察（共 {len(removed)} 条）")
    lines.append("")
    if removed:
        lines.extend([
            "| 球员 | 原俱乐部 | 关键字段摘要 |",
            "| --- | --- | --- |",
        ])
        for row in removed:
            fields = row.get("fields") or {}
            summary_bits = [
                f"{k}={v}" for k, v in fields.items()
                if k in ("注册类型", "注册子类型", "合同类别", "合同到期日") and v
            ]
            lines.append(
                f"| {_md_cell(row.get('player'))} | {_md_cell(row.get('club'))} | "
                f"{_md_cell('；'.join(summary_bits))} |"
            )
    else:
        lines.append("- （无移出名单记录）")
    lines.append("")
    return lines


def render_collector_official_digest_md(
    *,
    bundles: Sequence[Dict[str, Any]],
    reg_diff: Optional[Dict[str, Any]] = None,
    release_id: str = "v2.2.0-1",
    engine_sha: str = "",
    known_drive_ids: Sequence[str] = (),
) -> bytes:
    """Deterministically render governed collector official digest Markdown in evidence layer."""
    sorted_bundles = sorted((dict(b) for b in (bundles or ()) if isinstance(b, dict)), key=_bundle_sort_key)

    foreign_bundles: list[Dict[str, Any]] = []
    domestic_bundles: list[Dict[str, Any]] = []
    announcement_bundles: list[Dict[str, Any]] = []
    for b in sorted_bundles:
        kind = _classify_digest_bundle(b)
        if kind == "foreign_registration":
            foreign_bundles.append(b)
        elif kind == "domestic_registration":
            domestic_bundles.append(b)
        else:
            announcement_bundles.append(b)

    lines: list[str] = [
        f"# CBA 官方采集动态与注册增量摘要（{release_id} / CURRENT）",
        "",
        f"- **发布版本**: `{release_id}`",
        f"- **代码基线 Commit**: `{engine_sha or 'unknown'}`",
        "- **数据层级声明**: `evidence / candidate_observation`（来源自受控官方采集束 `acquisition_bundle_v1` 与跨季增量比对，**非**已晋升的 `canonical` 事实表）",
        "- **核心原则**: `Statement != Fact. Evidence != Fact. Unknown != 0.`",
        f"- **纳入采集文档总数**: **{len(sorted_bundles)}** 份（外籍注册快照 {len(foreign_bundles)} 份、国内注册快照 {len(domestic_bundles)} 份、官方公告与赛事动态 {len(announcement_bundles)} 份）",
        "",
        "## 1. 2026-2027 外籍球员注册官方快照",
        "",
    ]

    if foreign_bundles:
        for b in foreign_bundles:
            lines.extend([
                f"### {_md_cell(b.get('title') or b.get('doc_id'))}",
                "",
                f"- `doc_id`: `{b.get('doc_id')}` | `source_tier`: `{b.get('source_tier')}` | `published_at`: `{b.get('published_at')}` | `canonical_url`: {b.get('canonical_url') or '—'} | `raw_sha256`: `{b.get('raw_sha256') or '—'}`",
                "",
                str(b.get("body") or "").strip(),
                "",
            ])
    else:
        lines.extend(["- （本次采集未包含外籍球员注册快照）", ""])

    lines.extend([
        "## 2. 近三赛季国内球员注册快照与跨季增量观察",
        "",
        "### 2.1 国内球员注册官方快照索引",
        "",
    ])
    if domestic_bundles:
        lines.extend([
            "| doc_id | 标题 | 来源等级 | 发布日期 | canonical_url | raw_sha256 |",
            "| --- | --- | --- | --- | --- | --- |",
        ])
        for b in domestic_bundles:
            lines.append(
                f"| `{_md_cell(b.get('doc_id'))}` | {_md_cell(b.get('title'))} | "
                f"`{_md_cell(b.get('source_tier'))}` | {_md_cell(b.get('published_at'))} | "
                f"{_md_cell(b.get('canonical_url'))} | `{_md_cell(b.get('raw_sha256'))}` |"
            )
        lines.append("")
        for b in domestic_bundles:
            body = str(b.get("body") or "").strip()
            if body:
                excerpt = body if len(body) <= 1200 else body[:1200].rstrip() + "\n...（完整原文存于受控 document_archive）"
                lines.extend([
                    f"#### {_md_cell(b.get('title') or b.get('doc_id'))}",
                    "",
                    excerpt,
                    "",
                ])
    else:
        lines.extend(["- （本次采集未包含国内球员注册快照）", ""])

    if isinstance(reg_diff, dict):
        lines.extend(_render_digest_reg_diff_section(reg_diff))

    lines.extend([
        "## 3. 官方公告与赛事动态全量索引及摘录",
        "",
        "| doc_id | 标题 | 来源等级 | 事件分类 | 发布日期 | 采集时间 | canonical_url | raw_sha256 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ])
    for b in sorted_bundles:
        lines.append(
            f"| `{_md_cell(b.get('doc_id'))}` | {_md_cell(b.get('title'))} | "
            f"`{_md_cell(b.get('source_tier'))}` | {_md_cell(b.get('event_class'))} | "
            f"{_md_cell(b.get('published_at'))} | {_md_cell(b.get('acquired_at'))} | "
            f"{_md_cell(b.get('canonical_url'))} | `{_md_cell(b.get('raw_sha256'))}` |"
        )
    lines.append("")

    for b in announcement_bundles:
        persons = b.get("matched_persons") or []
        persons_str = ", ".join(str(p) for p in persons) if persons else "—"
        lines.extend([
            f"### {_md_cell(b.get('title') or b.get('doc_id'))}",
            "",
            (
                f"- `doc_id`: `{b.get('doc_id')}` | `source_tier`: `{b.get('source_tier')}` | "
                f"`event_class`: `{b.get('event_class')}` | `upstream_origin`: `{b.get('upstream_origin') or '—'}` | "
                f"`published_at`: `{b.get('published_at')}` | `acquired_at`: `{b.get('acquired_at')}`"
            ),
            f"- `canonical_url`: {b.get('canonical_url') or '—'} | `raw_sha256`: `{b.get('raw_sha256') or '—'}` | `matched_persons`: {persons_str}",
            "",
            str(b.get("body") or "").strip(),
            "",
        ])

    rendered = scrub_raw_drive_ids_for_consumer(
        "\n".join(lines).rstrip() + "\n",
        known_drive_ids=known_drive_ids,
    )
    if not rendered.endswith("\n"):
        rendered += "\n"
    rendered += "\nend_marker: true\n"
    return rendered.encode("utf-8")


def _safe_ascii_key(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_").lower() or "item"


def build_promoted_research_slice_artifacts(
    fixture: Dict[str, Any],
    *,
    player_uid: str,
    player_name: str,
    player_id: str,
    season: str,
    match_type: str = "REGULAR",
    scope: str = "WHOLE_SEASON",
    passage_label: str = "主要场均数据表",
    target_domain: str = "fixture_target",
    provider: str = "CBA_OFFICIAL",
    competition: str = "CBA",
    metric: str = "points_per_game",
    additional_match_types: Sequence[str] = (),
) -> Dict[str, Dict[str, Any]]:
    """Build self-describing CURRENT research card, slice, and excerpt artifacts via ConsumeSession."""
    players = fixture["players"]
    records = fixture["stats_records"]
    passages = fixture["passages"]
    fact = fixture.get("fact")
    claims = fixture.get("claims") or []
    source_objects = fixture.get("source_objects") or []
    if not players or not records or not passages:
        raise ValueError("PROMOTED_RESEARCH_SLICE_INPUT_INCOMPLETE")

    authorized_players = {player_uid}
    authorized_sources = {p["source_id"] for p in passages}
    authorized_objects = {
        o["object_id"]
        for o in source_objects
        if o.get("target_domain") == target_domain and o.get("rights") == "APPROVED"
    }
    if not authorized_objects:
        authorized_objects = set(authorized_sources)

    ordered_match_types: list[str] = [match_type]
    for extra_mt in additional_match_types or ():
        if isinstance(extra_mt, str) and extra_mt and extra_mt not in ordered_match_types:
            ordered_match_types.append(extra_mt)

    probe = ConsumeSession(
        target_domain=target_domain,
        authorized_players=authorized_players,
        authorized_sources=authorized_sources,
        authorized_objects=authorized_objects,
    )
    entries = []
    for mt in ordered_match_types:
        probed = probe.consume_slice(
            records,
            player_uid=player_uid,
            season=season,
            match_type=mt,
            scope=scope,
            provider=provider,
            competition=competition,
        )
        if "payload" not in probed or probed["payload"].get("status") != "OK":
            raise ValueError(f"PROMOTED_RESEARCH_SLICE_STATS_NOT_OK:{mt}")
        entries.append({
            "entry_id": f"slice:{player_uid}|{season}|{mt}|{scope}",
            "sha256": _content_digest(probed["payload"]),
            "authority": "derived",
            "rights": "public",
            "source_id": "promoted-research-slice",
        })
    for p in passages:
        entries.append({
            "entry_id": "passage:" + p["source_id"],
            "sha256": p["digest"],
            "authority": "canonical",
            "rights": "public",
            "source_id": p["source_id"],
        })

    snap = build_snapshot(
        entries=entries,
        target_domain=target_domain,
        capabilities=["stats", "identity", "document", "answer"],
        build_provenance="consumer_surface",
        build_time="2026-11-17T00:00:00+00:00",
        data_release="PROMOTED_CURRENT_SLICE",
        engine_candidate="v2.1.5",
        build_code="v2.1.5",
    )
    session = ConsumeSession(
        target_domain=target_domain,
        authorized_players=authorized_players,
        authorized_sources=authorized_sources,
        authorized_objects=authorized_objects,
    )
    session.bind_current(snap)
    session.register_passages(passages)
    sliced_by_mt: Dict[str, Dict[str, Any]] = {}
    for mt in ordered_match_types:
        sliced = session.consume_slice(
            records,
            player_uid=player_uid,
            season=season,
            match_type=mt,
            scope=scope,
            provider=provider,
            competition=competition,
        )
        if "payload" not in sliced or sliced["payload"].get("status") != "OK":
            raise ValueError(f"PROMOTED_RESEARCH_SLICE_STATS_NOT_OK:{mt}")
        sliced_by_mt[mt] = sliced

    target_passage = next(
        (p for p in passages if p.get("player_uid") == player_uid),
        passages[0],
    )
    binding = session.current_binding()
    chain = evidence_chain(
        receipt_id=binding["receipt_binding"],
        snapshot_id=snap.snapshot_id,
        build_version=snap.build_code,
        source_digest=target_passage["digest"],
        authorization_basis=binding["authorization_basis"],
    )
    cited = session.consume_citation(
        passage=target_passage,
        source_text=target_passage["text"],
        chain=chain,
    )

    guarantee = None
    fact_source = (fact.get("source_id") or fact.get("source")) if isinstance(fact, dict) else None
    fact_passage = next((p for p in passages if p.get("source_id") == fact_source), None)
    if (
        isinstance(fact, dict)
        and isinstance(fact.get("fact_id"), str)
        and fact.get("fact_id")
        and isinstance(fact.get("fact_version"), str)
        and fact.get("fact_version")
        and fact_passage is not None
    ):
        guarantee = build_fact_guarantee(
            fact_id=fact["fact_id"],
            fact_version=fact["fact_version"],
            source_id=fact_source,
            file_version=fact_passage["revision"],
            field_scope=sorted(fact.keys()),
            authorization_domain=target_domain,
            snapshot_id=snap.snapshot_id,
            source_digest=fact_passage["digest"],
        )

    ans = session.consume_answer(
        records=records,
        player_uid=player_uid,
        season=season,
        match_type=match_type,
        scope=scope,
        provider=provider,
        competition=competition,
        metric=metric,
        fact=fact,
        passage=target_passage,
        source_text=target_passage["text"],
        chain=chain,
        claims=claims,
        fact_guarantee=guarantee,
    )

    card_md = session.render_verified(ans, kind="research_card").encode("utf-8")
    passage_md = session.render_verified(cited, kind="passage").encode("utf-8")

    pid_slug = _safe_ascii_key(player_id)
    season_slug = _safe_ascii_key(season)

    card_name = research_card_consumer_filename(player_name, player_id, season)
    passage_name = f"研究摘录_{'_'.join(player_name.split())}_{'_'.join(player_id.split())}_{'_'.join(season.split())}_{'_'.join(passage_label.split())}_CURRENT.md"

    result: Dict[str, Dict[str, Any]] = {
        f"research/card_{pid_slug}_{season_slug}": {
            "name": card_name,
            "mime": "text/markdown",
            "mode": "binary",
            "parent_role": "data",
            "content": card_md,
        },
        f"research/passage_{pid_slug}_{season_slug}": {
            "name": passage_name,
            "mime": "text/markdown",
            "mode": "binary",
            "parent_role": "data",
            "content": passage_md,
        },
    }
    for mt in ordered_match_types:
        mt_slug = _safe_ascii_key(mt)
        slice_md = session.render_verified(sliced_by_mt[mt], kind="slice_markdown").encode("utf-8")
        result[f"research/slice_{pid_slug}_{season_slug}_{mt_slug}"] = {
            "name": slice_consumer_filename(player_name, player_id, season, mt),
            "mime": "text/markdown",
            "mode": "binary",
            "parent_role": "data",
            "content": slice_md,
        }
    return result


def build_consumer_surface_targets_spec(
    *,
    release_id: str,
    engine_sha: str,
    master_csv_bytes: bytes,
    source_registry_bytes: bytes,
    doc_index_bytes: Optional[bytes] = None,
    collector_official_digest_bytes: Optional[bytes] = None,
    include_collector_digest: Optional[bool] = None,
    promoted_research_artifacts: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Build declared consumer-surface target contents keyed by logical_key for Stage 4."""
    row_count = compute_master_row_count(master_csv_bytes)
    targets: Dict[str, Dict[str, Any]] = {
        "consumer/master_domestic_2017_2027_current.csv": {
            "name": CONSUMER_MASTER_FILENAME,
            "mime": "text/csv",
            "mode": "binary",
            "parent_role": "data",
            "content": export_consumer_master_csv(master_csv_bytes),
        },
        "consumer/field_dictionary_current.md": {
            "name": CONSUMER_DICTIONARY_FILENAME,
            "mime": "text/markdown",
            "mode": "binary",
            "parent_role": "data",
            "content": render_consumer_field_dictionary_md(
                release_id=release_id,
                engine_sha=engine_sha,
                master_row_count=row_count,
                include_collector_digest=include_collector_digest,
            ),
        },
        "consumer/source_registry_index_current.csv": {
            "name": CONSUMER_SOURCE_REGISTRY_FILENAME,
            "mime": "text/csv",
            "mode": "binary",
            "parent_role": "data",
            "content": export_consumer_source_registry_csv(source_registry_bytes),
        },
    }
    if doc_index_bytes is not None:
        targets["consumer/document_index_current.json"] = {
            "name": CONSUMER_DOC_INDEX_FILENAME,
            "mime": "application/json",
            "mode": "binary",
            "parent_role": "data",
            "content": export_consumer_document_index(doc_index_bytes),
        }
    if collector_official_digest_bytes is not None:
        targets["consumer/collector_official_digest_current.md"] = {
            "name": COLLECTOR_DIGEST_FILENAME,
            "mime": "text/markdown",
            "mode": "binary",
            "parent_role": "data",
            "content": collector_official_digest_bytes,
        }
    if promoted_research_artifacts:
        for key, spec in sorted(promoted_research_artifacts.items()):
            targets[key] = dict(spec)
    return targets
