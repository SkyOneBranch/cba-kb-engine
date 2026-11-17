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

_DRIVE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])1[A-Za-z0-9_-]{24,}(?![A-Za-z0-9_-])")
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_DIGITS_RE = re.compile(r"^\d+$")

_DRIVE_HOST = "drive" + ".google." + "com"
_DOCS_HOST = "docs" + ".google." + "com"
_DRIVE_URL_RE = re.compile(
    r"https?://(?:" + re.escape(_DRIVE_HOST) + r"|" + re.escape(_DOCS_HOST) + r")/[^\s)\]<>\"']+"
)

CONSUMER_MASTER_FILENAME = "CBA_国内球员注册权威总表_2017-2027_CURRENT.csv"
CONSUMER_DICTIONARY_FILENAME = "CBA_核心数据表字段字典与权威口径_CURRENT.md"
CONSUMER_SOURCE_REGISTRY_FILENAME = "CBA_来源注册索引_脱敏视图_CURRENT.csv"
CONSUMER_DOC_INDEX_FILENAME = "CBA_文档索引_脱敏视图_CURRENT.json"

_REDACT_JSON_KEYS = frozenset({
    "drive_file_id",
    "current_parent_id",
    "parent_id",
    "web_view_link",
    "webViewLink",
    "drive_url",
})


def _is_raw_drive_token(token: str) -> bool:
    if not token or _HEX40_RE.match(token) or _HEX64_RE.match(token) or _DIGITS_RE.match(token):
        return False
    return token.startswith("1") and len(token) >= 25


def _short_logical_source_id(raw_id: str) -> str:
    return "src:" + hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:12]


def scrub_raw_drive_ids_for_consumer(text: str) -> str:
    """Redact raw Drive ID tokens and direct Drive URLs in consumer-facing prose.

    Preserves 40-hex Git SHAs, 64-hex SHA-256 digests, and ordinary numeric tokens.
    """
    if not isinstance(text, str) or not text:
        return ""
    scrubbed = _DRIVE_URL_RE.sub("<redacted-drive-url>", text)

    def _replace_tok(match: re.Match[str]) -> str:
        tok = match.group(0)
        if _is_raw_drive_token(tok):
            return "<redacted-drive-id>"
        return tok

    return _DRIVE_TOKEN_RE.sub(_replace_tok, scrubbed)


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
            if not header or not any(cell is not None for cell in header):
                raise ValueError("MASTER_ROW_COUNT_EMPTY")
            count = sum(1 for row in rows_iter if any(cell is not None for cell in row))
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


def render_consumer_surface_guide_section(
    *, release_id: str, master_row_count: Optional[int]
) -> str:
    """Render standardized consumer-surface guide block with dynamic MASTER count."""
    count_label = f"{master_row_count:,} 条" if master_row_count is not None else "按构建期动态统计"
    return "\n".join([
        "## 当前生产权威消费面导览 (CURRENT Canonical Surface)",
        "",
        f"- 权威发布标识：`{release_id}`（仅读取处于 `COMPLETE` 状态且位于 `current` 消费区的 `CURRENT` / 权威产物；`90_archive_历史归档`、回收站与关闭沙盒绝非当前真值）。",
        f"- 当前 `MASTER` 国内球员注册总表数据行数：**{count_label}**（由构建期自动统计；历史迁移文档中提及的 `3,451` / `3451` 仅为 `2026-09-10` 早期 v1.1 载体对照基线，已由当前动态行数取代）。",
        f"- **自描述核心消费主表与字典**（面向全盘中文检索优先读取）：",
        f"  - `{CONSUMER_MASTER_FILENAME}`（对应底层 `derived/MASTER.csv` / `input/MASTER.xlsx`，全量国内球员注册权威总表）",
        f"  - `{CONSUMER_DICTIONARY_FILENAME}`（核心数据表字段字典、缩写释义与权威口径说明）",
        f"  - `{CONSUMER_SOURCE_REGISTRY_FILENAME}`（来源文件注册脱敏索引，隐去底层物理 `drive_file_id`，保留逻辑 `source_id` 与可读文件名）",
        "- **人物研究切片（`CURRENT` 正式入列）**：正式上线的球员研究卡采用 `研究卡_<中文姓名>_<球员ID>_<赛季>_CURRENT.md`（及对应 `研究切片_..._CURRENT.md`、`研究摘录_..._CURRENT.md`）命名，优先于历史归档或草稿文件被检出。",
        "- **关键字段语义消歧（严禁跨表混淆）**：",
        "  - `date_year_inferred` 属于事件表 `facts/CBA_球员注册_EVENTS.xlsx`（工作表 `registration_status_events`）的**事件行级布尔字段**：仅在跨季中段“取消注册”等仅写月日、年份由赛季边界推导时为 `True`。",
        "  - `source_registry`（及脱敏索引 `CBA_来源注册索引_脱敏视图_CURRENT.csv`）中的 `season`、`source_title` 与 `discovered_at_utc` 属于**来源文件级元数据**，与事件行级 `date_year_inferred` 相互独立，不得用来源文件字段替代或否定事件行级推导标记。",
    ])


def render_consumer_field_dictionary_md(
    *, release_id: str, engine_sha: str, master_row_count: int
) -> bytes:
    """Generate the self-describing CURRENT consumer field dictionary Markdown document."""
    lines = [
        f"# CBA-KB 核心数据表字段字典与权威口径（{release_id} / CURRENT）",
        "",
        f"- **发布版本**: `{release_id}`",
        f"- **代码基线 Commit**: `{engine_sha}`",
        f"- **国内球员注册权威总表 (`MASTER`) 动态构建行数**: **{master_row_count:,} 条**（历史文档中的 `3,451` 仅指 `2026-09-10` v1.1 迁移对照基线，非当前实时行数）。",
        "- **核心原则**: `Statement != Fact. Evidence != Fact. Unknown != 0.`",
        "",
        "## 1. 权威产物路由与命名规范（CURRENT Canonical Surface）",
        "",
        "| 消费文件名 / 路径 | 权威层级 | 覆盖范围与用途 |",
        "| --- | --- | --- |",
        f"| `{CONSUMER_MASTER_FILENAME}` (`derived/MASTER.csv`, `input/MASTER.xlsx`) | `canonical` | 国内球员赛季注册权威总表（{master_row_count:,} 行，覆盖 2017–2027） |",
        "| `facts/CBA_注册领域_六表.xlsx` | `canonical` | 注册域结构化六表（国内注册、交易窗口、外援优先权与注册快照、状态事件） |",
        "| `facts/CBA_球员注册_EVENTS.xlsx` | `canonical` | 全量注册状态变更与跨季事件表（`registration_status_events`） |",
        "| `facts/CBA_外籍球员注册_SNAPSHOTS.xlsx` | `canonical` | 外籍球员官方注册快照 |",
        f"| `{CONSUMER_SOURCE_REGISTRY_FILENAME}` | `control / metadata` | 来源文件注册脱敏索引（隐藏原始物理 Drive ID，保留逻辑 `source_id` 与可读标题） |",
        "| `研究卡_<中文姓名>_<球员ID>_<赛季>_CURRENT.md` | `research` | 已获授权并正式入列 `current` 的人物技术统计与多源证据研究卡 |",
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
        "",
        "end_marker: true",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def export_consumer_master_csv(raw_csv_bytes: bytes) -> bytes:
    """Create self-describing CURRENT MASTER CSV copy with source_file_id tokens redacted."""
    text = raw_csv_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("MASTER_CSV_HEADER_REQUIRED")
    fieldnames = list(reader.fieldnames)
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in reader:
        cleaned_row: Dict[str, Any] = {}
        for col in fieldnames:
            val = row.get(col, "")
            if col == "source_file_id" and isinstance(val, str) and _is_raw_drive_token(val.strip()):
                cleaned_row[col] = _short_logical_source_id(val.strip())
            elif isinstance(val, str):
                cleaned_row[col] = scrub_raw_drive_ids_for_consumer(val)
            else:
                cleaned_row[col] = val
        writer.writerow(cleaned_row)
    return out.getvalue().encode("utf-8-sig")


def export_consumer_source_registry_csv(raw_csv_bytes: bytes) -> bytes:
    """Export AI consumer view of source_registry.csv without raw drive_file_id tokens."""
    text = raw_csv_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("SOURCE_REGISTRY_HEADER_REQUIRED")
    drop_cols = {"drive_file_id", "current_parent_id", "parent_id", "web_view_link", "webViewLink"}
    kept_cols = [col for col in reader.fieldnames if col not in drop_cols]
    if not kept_cols:
        raise ValueError("SOURCE_REGISTRY_NO_KEPT_COLUMNS")
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=kept_cols, lineterminator="\n")
    writer.writeheader()
    for row in reader:
        out_row: Dict[str, str] = {}
        for col in kept_cols:
            val = row.get(col) or ""
            if col == "source_id":
                stripped = val.strip()
                if stripped.startswith("drive:"):
                    raw_id = stripped[len("drive:"):].strip()
                    val = _short_logical_source_id(raw_id) if raw_id else stripped
                else:
                    val = scrub_raw_drive_ids_for_consumer(stripped)
            else:
                val = scrub_raw_drive_ids_for_consumer(val)
            out_row[col] = val
        writer.writerow(out_row)
    return out.getvalue().encode("utf-8")


def _scrub_json_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: _scrub_json_obj(v)
            for k, v in obj.items()
            if k not in _REDACT_JSON_KEYS
        }
    if isinstance(obj, list):
        return [_scrub_json_obj(item) for item in obj]
    if isinstance(obj, str):
        return scrub_raw_drive_ids_for_consumer(obj)
    return obj


def export_consumer_document_index(raw_index_bytes: bytes) -> bytes:
    """Export normalized document index JSON without raw drive_file_id or Drive URLs."""
    parsed = json.loads(raw_index_bytes.decode("utf-8"))
    scrubbed = _scrub_json_obj(parsed)
    return (json.dumps(scrubbed, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


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

    probe = ConsumeSession(
        target_domain=target_domain,
        authorized_players=authorized_players,
        authorized_sources=authorized_sources,
        authorized_objects=authorized_objects,
    )
    probed = probe.consume_slice(
        records,
        player_uid=player_uid,
        season=season,
        match_type=match_type,
        scope=scope,
        provider=provider,
        competition=competition,
    )
    if "payload" not in probed or probed["payload"].get("status") != "OK":
        raise ValueError("PROMOTED_RESEARCH_SLICE_STATS_NOT_OK")
    slice_digest = _content_digest(probed["payload"])
    entries = [
        {
            "entry_id": f"slice:{player_uid}|{season}|{match_type}|{scope}",
            "sha256": slice_digest,
            "authority": "derived",
            "rights": "public",
            "source_id": "promoted-research-slice",
        }
    ]
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
    sliced = session.consume_slice(
        records,
        player_uid=player_uid,
        season=season,
        match_type=match_type,
        scope=scope,
        provider=provider,
        competition=competition,
    )
    if "payload" not in sliced or sliced["payload"].get("status") != "OK":
        raise ValueError("PROMOTED_RESEARCH_SLICE_STATS_NOT_OK")

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
    slice_md = session.render_verified(sliced, kind="slice_markdown").encode("utf-8")
    passage_md = session.render_verified(cited, kind="passage").encode("utf-8")

    pid_slug = _safe_ascii_key(player_id)
    season_slug = _safe_ascii_key(season)
    mt_slug = _safe_ascii_key(match_type)

    card_name = research_card_consumer_filename(player_name, player_id, season)
    slice_name = slice_consumer_filename(player_name, player_id, season, match_type)
    passage_name = f"研究摘录_{'_'.join(player_name.split())}_{'_'.join(player_id.split())}_{'_'.join(season.split())}_{'_'.join(passage_label.split())}_CURRENT.md"

    return {
        f"research/card_{pid_slug}_{season_slug}": {
            "name": card_name,
            "mime": "text/markdown",
            "mode": "binary",
            "parent_role": "data",
            "content": card_md,
        },
        f"research/slice_{pid_slug}_{season_slug}_{mt_slug}": {
            "name": slice_name,
            "mime": "text/markdown",
            "mode": "binary",
            "parent_role": "data",
            "content": slice_md,
        },
        f"research/passage_{pid_slug}_{season_slug}": {
            "name": passage_name,
            "mime": "text/markdown",
            "mode": "binary",
            "parent_role": "data",
            "content": passage_md,
        },
    }


def build_consumer_surface_targets_spec(
    *,
    release_id: str,
    engine_sha: str,
    master_csv_bytes: bytes,
    source_registry_bytes: bytes,
    doc_index_bytes: Optional[bytes] = None,
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
    if promoted_research_artifacts:
        for key, spec in sorted(promoted_research_artifacts.items()):
            targets[key] = dict(spec)
    return targets
