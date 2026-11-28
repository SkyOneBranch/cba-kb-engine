"""Safe dataset loader, 6 atomic read-only MCP tools, and 3-tier output isolation (REQ-250).

100% Python stdlib.
- Loads KB snapshot from ``--data-dir`` or ``--bundle-tarball`` (pure in-memory tarfile
  extraction with path traversal, symlink/hardlink, bomb/size, and ACL/secret rejection).
- Exposes 6 atomic read-only tools with high-precision Chinese descriptions and strict
  ``additionalProperties: false`` JSON Schemas:
  1. ``kb_status``
  2. ``resolve_entity``
  3. ``get_person_dossier``
  4. ``get_club_roster``
  5. ``query_stats_and_games``
  6. ``verify_statement``
- Physically strips internal notes, ``NEEDS_SOURCE``, ``INTERNAL_CLUE``, ``quote_span``/
  ``quote_anchors``, and ``.csv``/``.jsonl`` internal filenames for ``public`` tier;
  unlocks controlled ``quote_span`` for ``club_media``; unlocks ``internal_full`` and
  multi-filter ``game_logs`` for ``club_scout`` / ``owner``.
"""
from __future__ import annotations

import copy
import csv
import io
import json
import re
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .mcp_auth import (
    AuthContext,
    tier_allows_internal,
    tier_allows_quote_spans,
    tier_visibility,
)

DEFAULT_MAX_COMPRESSED_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_EXTRACTED_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_MEMBERS = 5000
DEFAULT_PUBLIC_GAME_LOGS_CAP = 5

_FORBIDDEN_BUNDLE_KEYWORDS = ("mcp_acl", "secret", "token")
_INTERNAL_FILE_EXT_RE = re.compile(r"\.(csv|jsonl)\b", re.IGNORECASE)
_INTERNAL_MARKER_SUBSTRINGS = (
    "NEEDS_SOURCE",
    "INTERNAL_CLUE",
    "INTERNAL_ONLY_NOTE",
)
_SEASON_RE = re.compile(r"20\d{2}-20\d{2}")
_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?;\n]+")

_YOUTH_STAGE_LABELS_ZH: Dict[str, str] = {
    "TRAINING_CAMP_CALLUP": "入选集训",
    "WARMUP_APPEARANCE": "热身赛出场",
    "FINAL_TOURNAMENT_ROSTER": "正赛最终名单",
    "OFFICIAL_TOURNAMENT_STATS": "正赛技术统计",
}

_ALL_DOSSIER_SECTIONS: Tuple[str, ...] = (
    "summary",
    "bio",
    "career_stats",
    "recent_games",
    "youth_national",
    "overseas",
    "stories",
    "social",
    "references",
)

TIERED_TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "kb_status",
        "description": (
            "查询当前 CBA 知识库快照版本、观测地平线（observed_until）、实体与故事统计数量、"
            "以及当前调用者的有效权限等级与能力范围（只读）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "resolve_entity",
        "description": (
            "按中文全名、英文名、姓氏、别名或俱乐部简称解析唯一人物或俱乐部实体 ID。"
            "遇到重名或共用姓氏（如“史密斯”）时返回 AMBIGUOUS 与候选列表，绝不擅自猜测。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "待解析的人物姓名、别名、person_uid 或俱乐部名称/简称。",
                },
                "entity_kind": {
                    "type": "string",
                    "enum": ["any", "person", "organization"],
                    "default": "any",
                    "description": "限定实体类型：any（默认）、person 或 organization。",
                },
                "club_hint": {
                    "type": "string",
                    "description": "可选俱乐部提示，用于在重名候选间辅助消歧。",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_person_dossier",
        "description": (
            "按 person_uid 或姓名获取人物结构化档案，并支持按需切片返回指定板块"
            "（summary、bio、career_stats、recent_games、youth_national、overseas、stories、social、references）。"
            "严格执行三级可见性物理隔离。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "person_uid": {
                    "type": "string",
                    "description": "人物唯一标识符（推荐优先使用 resolve_entity 返回的 entity_id）。",
                },
                "name": {
                    "type": "string",
                    "description": "人物姓名或别名（当未提供 person_uid 时用于解析）。",
                },
                "club_hint": {
                    "type": "string",
                    "description": "可选俱乐部提示，配合 name 消歧。",
                },
                "sections": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": list(_ALL_DOSSIER_SECTIONS),
                    },
                    "description": "需要返回的档案板块列表；省略则返回全部板块。",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_club_roster",
        "description": (
            "查询指定 CBA 俱乐部与赛季的五向人员变动台账（留队、新签外援/内援、离队、租借、青训上调）、"
            "现役内外援名单、合同分布以及教练组三面视图（官方注册/俱乐部官宣/临场执教）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "club": {
                    "type": "string",
                    "description": "俱乐部名称、简称或 organization_id（多俱乐部数据集下必填）。",
                },
                "season": {
                    "type": "string",
                    "description": "目标赛季，例如 2025-2026 或 2026-2027。",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "query_stats_and_games",
        "description": (
            "在 Python 引擎内执行确定性的赛季统计聚合（season_stats）或单场技术统计多维筛选（game_logs），"
            "诚实标注生涯覆盖模式（FULL_CBA_CAREER / PARTIAL_SEASONS_OBSERVED）。"
            "public 档位仅返回近期公开摘要，club_scout/owner 档位解锁全量多维条件筛选。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["season_stats", "game_logs"],
                    "description": "查询模式：season_stats（赛季统计）或 game_logs（单场日志）。",
                },
                "person_uid": {
                    "type": "string",
                    "description": "按人物 person_uid 筛选。",
                },
                "name": {
                    "type": "string",
                    "description": "按球员姓名筛选。",
                },
                "season": {
                    "type": "string",
                    "description": "按赛季筛选，例如 2025-2026。",
                },
                "match_type": {
                    "type": "string",
                    "description": "按赛事阶段筛选，例如 常规赛 / 季后赛。",
                },
                "club": {
                    "type": "string",
                    "description": "按所属俱乐部筛选。",
                },
                "opponent": {
                    "type": "string",
                    "description": "按对手俱乐部筛选（game_logs 模式）。",
                },
                "date_from": {
                    "type": "string",
                    "description": "起始比赛日期 YYYY-MM-DD（game_logs 模式）。",
                },
                "date_to": {
                    "type": "string",
                    "description": "截止比赛日期 YYYY-MM-DD（game_logs 模式）。",
                },
                "min_points": {
                    "type": "number",
                    "description": "单场最低得分阈值（game_logs 模式）。",
                },
                "min_rebounds": {
                    "type": "number",
                    "description": "单场最低篮板阈值（game_logs 模式）。",
                },
                "min_assists": {
                    "type": "number",
                    "description": "单场最低助攻阈值（game_logs 模式）。",
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["game_date", "points", "rebounds", "assists", "season"],
                    "description": "排序字段。",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "description": "最大返回条数。",
                },
            },
            "required": ["mode"],
            "additionalProperties": False,
        },
    },
    {
        "name": "verify_statement",
        "description": (
            "对输入陈述按句拆分并与权威注册名单、赛季统计、教练三面视图、公开故事、国字号经历与核验社媒动态逐句核验，"
            "输出核验结论（VERIFIED_MATCH / MULTI_SOURCE_DISCREPANCY / CONTRADICTS_CANONICAL_DATA / "
            "SELF_CLAIM_MATCH / UNVERIFIED_NO_BASIS）及溯源链接。仅 club_media 及以上档位返回受控原文引文锚点。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "statement": {
                    "type": "string",
                    "description": "待核验的中文或双语事实陈述文本。",
                },
                "person_hint": {
                    "type": "string",
                    "description": "可选人物提示（姓名或 person_uid）。",
                },
                "club_hint": {
                    "type": "string",
                    "description": "可选俱乐部提示。",
                },
                "season": {
                    "type": "string",
                    "description": "可选赛季提示，例如 2025-2026。",
                },
            },
            "required": ["statement"],
            "additionalProperties": False,
        },
    },
]


def _is_forbidden_bundle_path(rel_path: str) -> bool:
    norm = rel_path.replace("\\", "/")
    parts = [p.lower() for p in PurePosixPath(norm).parts if p and p != "."]
    for part in parts:
        for kw in _FORBIDDEN_BUNDLE_KEYWORDS:
            if kw in part:
                return True
    return False


def _validate_tar_member_name(name: str) -> str:
    if not name or "\x00" in name:
        raise ValueError(f"INVALID_TAR_MEMBER_NAME: {name!r}")
    norm = name.replace("\\", "/")
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
        raise ValueError(f"ABSOLUTE_PATH_IN_TARBALL: {name!r}")
    parts = PurePosixPath(norm).parts
    if any(p == ".." for p in parts):
        raise ValueError(f"PATH_TRAVERSAL_IN_TARBALL: {name!r}")
    clean = "/".join(p for p in parts if p not in ("", "."))
    if not clean:
        raise ValueError(f"EMPTY_TAR_MEMBER_NAME: {name!r}")
    if _is_forbidden_bundle_path(clean):
        raise ValueError(f"FORBIDDEN_BUNDLE_MEMBER: {clean!r}")
    return clean


def _parse_json_bytes(data: bytes) -> Any:
    return json.loads(data.decode("utf-8"))


def _parse_json_or_jsonl_bytes(data: bytes) -> List[Dict[str, Any]]:
    text = data.decode("utf-8").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        if isinstance(parsed, dict):
            for key in ("records", "items", "rows", "entries", "stories", "persons", "organizations"):
                if isinstance(parsed.get(key), list):
                    return [item for item in parsed[key] if isinstance(item, dict)]
            return [parsed]
        return []
    except json.JSONDecodeError:
        out: List[Dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if isinstance(obj, dict):
                out.append(obj)
        return out


def _parse_csv_bytes(data: bytes) -> List[Dict[str, Any]]:
    text = data.decode("utf-8-sig")
    return [dict(row) for row in csv.DictReader(io.StringIO(text))]


def _normalize_text(val: Any) -> str:
    return str(val or "").strip().casefold()


def _sanitize_value_for_tier(value: Any, tier: str) -> Any:
    """Recursively scrub internal-only fields, filenames, and quote spans per tier."""
    if tier_allows_internal(tier):
        return copy.deepcopy(value)

    allow_quotes = tier_allows_quote_spans(tier)

    if isinstance(value, str):
        if _INTERNAL_FILE_EXT_RE.search(value):
            return None
        for marker in _INTERNAL_MARKER_SUBSTRINGS:
            if marker in value:
                return None
        return value

    if isinstance(value, list):
        cleaned_list = []
        for item in value:
            cleaned = _sanitize_value_for_tier(item, tier)
            if cleaned is not None:
                cleaned_list.append(cleaned)
        return cleaned_list

    if isinstance(value, dict):
        # Drop objects explicitly tagged as NEEDS_SOURCE / INTERNAL_CLUE
        status_val = str(value.get("status") or value.get("verification_status") or "").strip().upper()
        if status_val in ("NEEDS_SOURCE", "INTERNAL_CLUE"):
            return None

        cleaned_dict: Dict[str, Any] = {}
        for k, v in value.items():
            k_low = str(k).lower()
            if k_low in (
                "internal_verbatim_or_note",
                "internal_clue",
                "needs_source_note",
                "attribute_provenance",
                "provenance",
                "source_file",
            ):
                continue
            if "internal" in k_low or "needs_source" in k_low:
                continue
            if not allow_quotes and k_low in ("quote_anchors", "quote_span"):
                continue

            cleaned_v = _sanitize_value_for_tier(v, tier)
            if cleaned_v is None and isinstance(v, (str, dict)):
                # Stripped internal/filename string or NEEDS_SOURCE/INTERNAL_CLUE nested dict
                continue
            cleaned_dict[k] = cleaned_v
        return cleaned_dict

    return value


def _extract_https_urls(obj: Any) -> List[str]:
    urls: List[str] = []
    seen: Set[str] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, str):
            s = node.strip()
            if s.startswith("https://") and s not in seen:
                seen.add(s)
                urls.append(s)
        elif isinstance(node, Mapping):
            for v in node.values():
                _walk(v)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)

    _walk(obj)
    return urls


class KnowledgeDataset:
    """In-memory read-only CBA-KB snapshot backing the 6 atomic MCP tools."""

    def __init__(
        self,
        *,
        persons: List[Dict[str, Any]],
        bio_profiles: Dict[str, Dict[str, Any]],
        organizations: List[Dict[str, Any]],
        master_rows: List[Dict[str, Any]],
        season_stats: List[Dict[str, Any]],
        game_logs: List[Dict[str, Any]],
        stories: List[Dict[str, Any]],
        roster_ledger: List[Dict[str, Any]],
        registration_time_slices: List[Dict[str, Any]],
        coach_role_facets: Dict[str, Any],
        overseas_tenures: List[Dict[str, Any]],
        youth_tenures: List[Dict[str, Any]],
        coverage_ledger: Dict[str, Any],
        operational_checkpoint: Dict[str, Any],
        shenzhen_pilot_context: Dict[str, Any],
        club_contexts: Optional[Dict[str, Dict[str, Any]]] = None,
        social_profiles: Optional[List[Dict[str, Any]]] = None,
        social_posts: Optional[List[Dict[str, Any]]] = None,
        demand_ledger_path: Optional[Path | str] = None,
    ) -> None:
        # Deduplicate persons by person_uid so cross-club shards never split lifetime PERS-*
        dedup_persons: List[Dict[str, Any]] = []
        person_by_uid: Dict[str, Dict[str, Any]] = {}
        for p in persons:
            if not isinstance(p, Mapping):
                continue
            uid = str(p.get("person_uid") or "").strip()
            if not uid:
                dedup_persons.append(dict(p))
                continue
            if uid not in person_by_uid:
                p_copy = copy.deepcopy(dict(p))
                person_by_uid[uid] = p_copy
                dedup_persons.append(p_copy)
            else:
                existing = person_by_uid[uid]
                ex_tenures = existing.setdefault("role_tenures", [])
                for t in p.get("role_tenures") or ():
                    if t not in ex_tenures:
                        ex_tenures.append(copy.deepcopy(t))
                ex_aliases = existing.setdefault("aliases", [])
                for a in p.get("aliases") or ():
                    if a not in ex_aliases:
                        ex_aliases.append(a)

        self.persons = dedup_persons
        self.bio_profiles = bio_profiles
        self.organizations = organizations
        self.master_rows = master_rows
        self.season_stats = season_stats
        self.game_logs = game_logs
        self.stories = stories
        self.roster_ledger = roster_ledger
        self.registration_time_slices = registration_time_slices
        self.coach_role_facets = coach_role_facets
        self.overseas_tenures = overseas_tenures
        self.youth_tenures = youth_tenures
        self.coverage_ledger = coverage_ledger
        self.operational_checkpoint = operational_checkpoint
        self.shenzhen_pilot_context = shenzhen_pilot_context
        self.club_contexts = club_contexts if club_contexts is not None else (
            {"ORG-CBA-SHENZHEN": dict(shenzhen_pilot_context)} if shenzhen_pilot_context else {}
        )
        self.social_profiles = list(social_profiles or [])
        self.social_posts = list(social_posts or [])
        self.demand_ledger_path = Path(demand_ledger_path) if demand_ledger_path is not None else None

        self._person_by_uid = person_by_uid

    @property
    def release_id(self) -> str:
        return str(self.operational_checkpoint.get("release_id") or "unknown")

    @property
    def code_commit(self) -> str:
        return str(self.operational_checkpoint.get("code_commit") or "unknown")

    @property
    def observed_until(self) -> str:
        return str(
            self.operational_checkpoint.get("observed_until")
            or self.coverage_ledger.get("observed_until")
            or "unknown"
        )

    @property
    def entity_count(self) -> int:
        return len(self.persons) + len(self.organizations)

    @classmethod
    def _from_file_map(
        cls,
        files: Mapping[str, bytes],
        *,
        demand_ledger_path: Optional[Path | str] = None,
    ) -> "KnowledgeDataset":
        persons: List[Dict[str, Any]] = []
        bio_profiles: Dict[str, Dict[str, Any]] = {}
        organizations: List[Dict[str, Any]] = []
        master_rows: List[Dict[str, Any]] = []
        season_stats: List[Dict[str, Any]] = []
        game_logs: List[Dict[str, Any]] = []
        stories: List[Dict[str, Any]] = []
        roster_ledger: List[Dict[str, Any]] = []
        registration_time_slices: List[Dict[str, Any]] = []
        coach_role_facets: Dict[str, Any] = {}
        overseas_tenures: List[Dict[str, Any]] = []
        youth_tenures: List[Dict[str, Any]] = []
        coverage_ledger: Dict[str, Any] = {}
        operational_checkpoint: Dict[str, Any] = {}
        shenzhen_pilot_context: Dict[str, Any] = {}
        club_contexts: Dict[str, Dict[str, Any]] = {}
        social_profiles: List[Dict[str, Any]] = []
        social_posts: List[Dict[str, Any]] = []

        for rel_path in sorted(files.keys()):
            data = files[rel_path]
            posix = PurePosixPath(rel_path.replace("\\", "/"))
            fname = posix.name
            fname_low = fname.lower()
            parent_names = {p.lower() for p in posix.parts[:-1]}

            if fname_low == "person_registry.json":
                parsed = _parse_json_bytes(data)
                if isinstance(parsed, dict) and isinstance(parsed.get("persons"), list):
                    persons.extend(p for p in parsed["persons"] if isinstance(p, dict))
                elif isinstance(parsed, list):
                    persons.extend(p for p in parsed if isinstance(p, dict))
            elif fname_low == "person_bio_profiles.json":
                parsed = _parse_json_bytes(data)
                raw_profiles = parsed.get("profiles") if isinstance(parsed, dict) and "profiles" in parsed else parsed
                if isinstance(raw_profiles, dict):
                    for uid, prof in raw_profiles.items():
                        if isinstance(prof, dict):
                            bio_profiles[str(uid)] = prof
                elif isinstance(raw_profiles, list):
                    for prof in raw_profiles:
                        if isinstance(prof, dict) and prof.get("person_uid"):
                            bio_profiles[str(prof["person_uid"])] = prof
            elif fname_low == "organization_profiles.json":
                parsed = _parse_json_bytes(data)
                if isinstance(parsed, dict) and isinstance(parsed.get("organizations"), list):
                    organizations.extend(o for o in parsed["organizations"] if isinstance(o, dict))
                elif isinstance(parsed, list):
                    organizations.extend(o for o in parsed if isinstance(o, dict))
            elif fname_low == "master.csv" or (fname_low.endswith(".csv") and ("domestic" in fname_low or "foreign" in fname_low)):
                master_rows.extend(_parse_csv_bytes(data))
            elif "game_logs" in parent_names and (fname_low.endswith(".json") or fname_low.endswith(".jsonl")):
                game_logs.extend(_parse_json_or_jsonl_bytes(data))
            elif fname_low.startswith("season_stats") and (fname_low.endswith(".json") or fname_low.endswith(".jsonl")):
                season_stats.extend(_parse_json_or_jsonl_bytes(data))
            elif fname_low in ("story_records.jsonl", "story_records.json"):
                stories.extend(_parse_json_or_jsonl_bytes(data))
            elif fname_low in ("club_season_roster_ledger.jsonl", "club_season_roster_ledger.json"):
                roster_ledger.extend(_parse_json_or_jsonl_bytes(data))
            elif fname_low in ("registration_time_slices.jsonl", "registration_time_slices.json"):
                registration_time_slices.extend(_parse_json_or_jsonl_bytes(data))
            elif fname_low == "coach_role_facets.json":
                parsed = _parse_json_bytes(data)
                if isinstance(parsed, dict):
                    coach_role_facets = parsed
            elif fname_low in ("person_overseas_tenures.jsonl", "person_overseas_tenures.json"):
                overseas_tenures.extend(_parse_json_or_jsonl_bytes(data))
            elif fname_low in ("youth_national_tenures.jsonl", "youth_national_tenures.json"):
                youth_tenures.extend(_parse_json_or_jsonl_bytes(data))
            elif fname_low == "coverage_ledger.json":
                parsed = _parse_json_bytes(data)
                if isinstance(parsed, dict):
                    coverage_ledger = parsed
            elif fname_low == "operational_checkpoint.json":
                parsed = _parse_json_bytes(data)
                if isinstance(parsed, dict):
                    operational_checkpoint = parsed
            elif fname_low == "shenzhen_pilot_context.json":
                parsed = _parse_json_bytes(data)
                if isinstance(parsed, dict):
                    shenzhen_pilot_context = parsed
                    oid = str(parsed.get("organization_id") or "ORG-CBA-SHENZHEN")
                    club_contexts.setdefault(oid, {}).update(parsed)
            elif fname_low in ("club_context.json", "club_pack.json"):
                parsed = _parse_json_bytes(data)
                if isinstance(parsed, dict):
                    oid = str(parsed.get("organization_id") or "").strip()
                    if not oid and len(posix.parts) >= 2:
                        oid = posix.parts[-2]
                    if oid:
                        club_contexts.setdefault(oid, {}).update(parsed)
            elif fname_low == "social_profiles.json":
                parsed = _parse_json_bytes(data)
                if isinstance(parsed, dict) and isinstance(parsed.get("profiles"), list):
                    social_profiles.extend(p for p in parsed["profiles"] if isinstance(p, dict))
                elif isinstance(parsed, list):
                    social_profiles.extend(p for p in parsed if isinstance(p, dict))
            elif fname_low in ("social_posts.jsonl", "social_posts.json"):
                social_posts.extend(_parse_json_or_jsonl_bytes(data))

        return cls(
            persons=persons,
            bio_profiles=bio_profiles,
            organizations=organizations,
            master_rows=master_rows,
            season_stats=season_stats,
            game_logs=game_logs,
            stories=stories,
            roster_ledger=roster_ledger,
            registration_time_slices=registration_time_slices,
            coach_role_facets=coach_role_facets,
            overseas_tenures=overseas_tenures,
            youth_tenures=youth_tenures,
            coverage_ledger=coverage_ledger,
            operational_checkpoint=operational_checkpoint,
            shenzhen_pilot_context=shenzhen_pilot_context,
            club_contexts=club_contexts,
            social_profiles=social_profiles,
            social_posts=social_posts,
            demand_ledger_path=demand_ledger_path,
        )

    @classmethod
    def from_data_dir(
        cls,
        data_dir: Path | str,
        *,
        demand_ledger_path: Optional[Path | str] = None,
    ) -> "KnowledgeDataset":
        root = Path(data_dir)
        if not root.is_dir():
            raise ValueError(f"DATA_DIR_NOT_FOUND: {root}")
        files: Dict[str, bytes] = {}
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(root).as_posix()
            if _is_forbidden_bundle_path(rel):
                continue
            files[rel] = path.read_bytes()
        return cls._from_file_map(files, demand_ledger_path=demand_ledger_path)

    @classmethod
    def from_sharded_dir(
        cls,
        shard_dir: Path | str,
        *,
        clubs: Optional[Sequence[str]] = None,
        demand_ledger_path: Optional[Path | str] = None,
    ) -> "KnowledgeDataset":
        from .club_sharding import load_sharded_file_map

        files = load_sharded_file_map(shard_dir, clubs=clubs)
        return cls._from_file_map(files, demand_ledger_path=demand_ledger_path)

    @classmethod
    def from_bundle_tarball(
        cls,
        tarball_path: Path | str,
        *,
        max_compressed_bytes: int = DEFAULT_MAX_COMPRESSED_BYTES,
        max_extracted_bytes: int = DEFAULT_MAX_EXTRACTED_BYTES,
        max_members: int = DEFAULT_MAX_MEMBERS,
    ) -> "KnowledgeDataset":
        path = Path(tarball_path)
        if not path.is_file():
            raise ValueError(f"BUNDLE_TARBALL_NOT_FOUND: {path}")
        compressed_size = path.stat().st_size
        if compressed_size > max_compressed_bytes:
            raise ValueError(
                f"TARBALL_COMPRESSED_SIZE_EXCEEDED: {compressed_size} > {max_compressed_bytes}"
            )

        files: Dict[str, bytes] = {}
        total_extracted = 0
        member_count = 0

        with tarfile.open(path, "r:*") as tf:
            for member in tf:
                member_count += 1
                if member_count > max_members:
                    raise ValueError(f"TARBALL_TOO_MANY_MEMBERS: > {max_members}")

                if (
                    member.issym()
                    or member.islnk()
                    or member.ischr()
                    or member.isblk()
                    or member.isfifo()
                ):
                    raise ValueError(f"UNSAFE_TAR_MEMBER_TYPE: {member.name!r}")

                clean_name = _validate_tar_member_name(member.name)

                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError(f"UNSUPPORTED_TAR_MEMBER: {member.name!r}")
                if member.size < 0 or member.size > max_extracted_bytes:
                    raise ValueError(f"TARBALL_MEMBER_TOO_LARGE: {member.name!r}")

                extracted_stream = tf.extractfile(member)
                if extracted_stream is None:
                    continue

                buf = io.BytesIO()
                while True:
                    chunk = extracted_stream.read(65536)
                    if not chunk:
                        break
                    total_extracted += len(chunk)
                    if total_extracted > max_extracted_bytes:
                        raise ValueError(
                            f"TARBALL_EXTRACTED_SIZE_EXCEEDED: > {max_extracted_bytes}"
                        )
                    buf.write(chunk)
                files[clean_name] = buf.getvalue()

        return cls._from_file_map(files)

    # ------------------------------------------------------------------
    # Entity & club lookup helpers
    # ------------------------------------------------------------------
    def _club_aliases_for(self, club_query: str) -> Set[str]:
        q_cf = _normalize_text(club_query)
        aliases: Set[str] = {q_cf} if q_cf else set()
        for org in self.organizations:
            names = [
                org.get("organization_id"),
                org.get("canonical_name"),
                org.get("short_name"),
                *(org.get("aliases") or ()),
            ]
            norm_names = {_normalize_text(n) for n in names if n}
            if q_cf in norm_names or any(q_cf and q_cf in nn for nn in norm_names):
                aliases.update(norm_names)
        return aliases

    def _club_matches(self, candidate_club: Any, club_query: str) -> bool:
        if not club_query:
            return True
        c_cf = _normalize_text(candidate_club)
        if not c_cf:
            return False
        aliases = self._club_aliases_for(club_query)
        if c_cf in aliases:
            return True
        q_cf = _normalize_text(club_query)
        return q_cf in c_cf or c_cf in q_cf

    def _resolve_club_info(self, club_query: str) -> Tuple[str, Optional[str], Set[str]]:
        q_cf = _normalize_text(club_query)
        for org in self.organizations:
            names = [
                org.get("organization_id"),
                org.get("canonical_name"),
                org.get("short_name"),
                *(org.get("aliases") or ()),
            ]
            norm_names = {_normalize_text(n) for n in names if n}
            if q_cf in norm_names or any(q_cf and q_cf in nn for nn in norm_names):
                display = str(org.get("short_name") or org.get("canonical_name") or club_query)
                org_id = str(org.get("organization_id") or "") or None
                return display, org_id, norm_names
        return club_query.strip(), None, {q_cf}

    def _person_match_dict(self, p: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "entity_id": str(p.get("person_uid") or ""),
            "entity_kind": "person",
            "canonical_name": str(p.get("canonical_name") or ""),
            "club": str(p.get("current_club") or p.get("primary_club") or ""),
            "role": str(p.get("primary_role") or ""),
            "aliases": list(p.get("aliases") or []),
        }

    def _org_match_dict(self, o: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "entity_id": str(o.get("organization_id") or ""),
            "entity_kind": "organization",
            "canonical_name": str(o.get("canonical_name") or ""),
            "club": str(o.get("short_name") or o.get("canonical_name") or ""),
            "role": "CLUB",
            "aliases": list(o.get("aliases") or []),
        }

    # ------------------------------------------------------------------
    # Tool 1: kb_status
    # ------------------------------------------------------------------
    def tool_kb_status(self, arguments: Mapping[str, Any], auth_context: AuthContext) -> Dict[str, Any]:
        tier = auth_context.effective_tier
        if tier_allows_internal(tier):
            story_count = len(self.stories)
        else:
            story_count = sum(
                1
                for s in self.stories
                if isinstance(s.get("visibility_policy"), Mapping)
                and s["visibility_policy"].get("public_export_allowed") is True
            )

        return {
            "release_id": self.release_id,
            "code_commit": self.code_commit,
            "observed_until": self.observed_until,
            "entity_counts": {
                "persons": len(self.persons),
                "organizations": len(self.organizations),
            },
            "story_count": story_count,
            "overseas_tenure_count": len(self.overseas_tenures),
            "youth_tenure_count": len(self.youth_tenures),
            "effective_tier": tier,
            "capabilities": {
                "visibility": tier_visibility(tier),
                "quote_spans_allowed": tier_allows_quote_spans(tier),
                "internal_clues_allowed": tier_allows_internal(tier),
                "full_game_logs_filter": tier != "public",
            },
        }

    # ------------------------------------------------------------------
    # Tool 2: resolve_entity
    # ------------------------------------------------------------------
    def tool_resolve_entity(self, arguments: Mapping[str, Any], auth_context: AuthContext) -> Dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ValueError("Missing required argument: query")
        entity_kind = str(arguments.get("entity_kind") or "any").strip()
        club_hint = str(arguments.get("club_hint") or "").strip()
        q_cf = _normalize_text(query)

        exact_id_matches: List[Dict[str, Any]] = []
        exact_name_matches: List[Dict[str, Any]] = []
        alias_or_partial_matches: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()

        if entity_kind in ("any", "person"):
            for p in self.persons:
                uid = str(p.get("person_uid") or "")
                cname = _normalize_text(p.get("canonical_name"))
                ename = _normalize_text(p.get("english_name"))
                aliases = [_normalize_text(a) for a in (p.get("aliases") or ()) if a]

                if uid and _normalize_text(uid) == q_cf:
                    if uid not in seen_ids:
                        seen_ids.add(uid)
                        exact_id_matches.append(self._person_match_dict(p))
                    continue

                if q_cf in (cname, ename):
                    if uid not in seen_ids:
                        seen_ids.add(uid)
                        exact_name_matches.append(self._person_match_dict(p))
                    continue

                if (
                    q_cf in aliases
                    or (q_cf and q_cf in cname)
                    or (q_cf and q_cf in ename)
                    or any(q_cf and q_cf in a for a in aliases)
                ):
                    if uid not in seen_ids:
                        seen_ids.add(uid)
                        alias_or_partial_matches.append(self._person_match_dict(p))

        if entity_kind in ("any", "organization"):
            for o in self.organizations:
                oid = str(o.get("organization_id") or "")
                cname = _normalize_text(o.get("canonical_name"))
                sname = _normalize_text(o.get("short_name"))
                aliases = [_normalize_text(a) for a in (o.get("aliases") or ()) if a]

                if oid and _normalize_text(oid) == q_cf:
                    if oid not in seen_ids:
                        seen_ids.add(oid)
                        exact_id_matches.append(self._org_match_dict(o))
                    continue

                if q_cf in (cname, sname):
                    if oid not in seen_ids:
                        seen_ids.add(oid)
                        exact_name_matches.append(self._org_match_dict(o))
                    continue

                if (
                    q_cf in aliases
                    or (q_cf and q_cf in cname)
                    or (q_cf and q_cf in sname)
                    or any(q_cf and q_cf in a for a in aliases)
                ):
                    if oid not in seen_ids:
                        seen_ids.add(oid)
                        alias_or_partial_matches.append(self._org_match_dict(o))

        if exact_id_matches:
            matches = exact_id_matches
        elif exact_name_matches:
            matches = exact_name_matches
        else:
            matches = alias_or_partial_matches

        if club_hint and len(matches) > 1:
            narrowed = [m for m in matches if self._club_matches(m.get("club"), club_hint)]
            if narrowed:
                matches = narrowed

        if not matches:
            if self.demand_ledger_path is not None:
                from .demand_ledger import record_demand_miss

                record_demand_miss(
                    self.demand_ledger_path,
                    query_key=query,
                    miss_reason="ENTITY_NOT_FOUND",
                    club_hint=club_hint or None,
                )
            return {
                "status": "NOT_FOUND",
                "matches": [],
                "clarification_needed": False,
            }
        if len(matches) == 1:
            return {
                "status": "RESOLVED",
                "matches": matches,
                "clarification_needed": False,
            }
        return {
            "status": "AMBIGUOUS",
            "matches": matches,
            "clarification_needed": True,
        }

    # ------------------------------------------------------------------
    # Tool 3: get_person_dossier
    # ------------------------------------------------------------------
    def _resolve_single_person(
        self,
        *,
        person_uid: Optional[str],
        name: Optional[str],
        club_hint: Optional[str] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        if person_uid:
            uid_s = str(person_uid).strip()
            p = self._person_by_uid.get(uid_s)
            if p is not None:
                return p, None
            return None, {"status": "NOT_FOUND", "person_uid": uid_s}

        if name:
            res = self.tool_resolve_entity(
                {"query": name, "entity_kind": "person", "club_hint": club_hint or ""},
                AuthContext("public", "internal", "internal:resolve", True),
            )
            if res["status"] == "RESOLVED":
                uid = res["matches"][0]["entity_id"]
                return self._person_by_uid.get(uid), None
            return None, res

        raise ValueError("Either person_uid or name is required")

    def tool_get_person_dossier(
        self,
        arguments: Mapping[str, Any],
        auth_context: AuthContext,
    ) -> Dict[str, Any]:
        tier = auth_context.effective_tier
        person, err = self._resolve_single_person(
            person_uid=arguments.get("person_uid"),
            name=arguments.get("name"),
            club_hint=arguments.get("club_hint"),
        )
        if person is None:
            return {
                "status": (err or {}).get("status", "NOT_FOUND"),
                "effective_tier": tier,
                "resolution": err,
            }

        uid = str(person["person_uid"])
        req_sections = arguments.get("sections")
        if req_sections is None:
            sections = list(_ALL_DOSSIER_SECTIONS)
        else:
            sections = [str(s) for s in req_sections if str(s) in _ALL_DOSSIER_SECTIONS]

        result: Dict[str, Any] = {
            "status": "FOUND",
            "person_uid": uid,
            "canonical_name": person.get("canonical_name"),
            "effective_tier": tier,
        }

        bio = self.bio_profiles.get(uid, {})
        stats_rows = [s for s in self.season_stats if str(s.get("person_uid") or "") == uid]
        game_rows = sorted(
            [g for g in self.game_logs if str(g.get("person_uid") or "") == uid],
            key=lambda g: str(g.get("game_date") or ""),
            reverse=True,
        )
        overseas_rows = [o for o in self.overseas_tenures if str(o.get("person_uid") or "") == uid]
        youth_rows = [y for y in self.youth_tenures if str(y.get("person_uid") or "") == uid]

        person_stories: List[Dict[str, Any]] = []
        for s in self.stories:
            parts = s.get("participants") or []
            if not any(isinstance(pt, Mapping) and str(pt.get("person_uid") or "") == uid for pt in parts):
                continue
            pub_allowed = (
                isinstance(s.get("visibility_policy"), Mapping)
                and s["visibility_policy"].get("public_export_allowed") is True
            )
            if not tier_allows_internal(tier) and not pub_allowed:
                continue
            story_copy = copy.deepcopy(s)
            if not tier_allows_internal(tier):
                # Strip internal_verbatim_or_note from participants
                for pt in story_copy.get("participants") or []:
                    if isinstance(pt, dict):
                        pt.pop("internal_verbatim_or_note", None)
                if not tier_allows_quote_spans(tier):
                    story_copy.pop("quote_anchors", None)
            person_stories.append(story_copy)

        if "summary" in sections:
            result["summary"] = {
                "person_uid": uid,
                "canonical_name": person.get("canonical_name"),
                "english_name": person.get("english_name"),
                "aliases": list(person.get("aliases") or []),
                "primary_club": person.get("primary_club"),
                "current_club": person.get("current_club"),
                "primary_role": person.get("primary_role"),
                "roles": list(person.get("roles") or []),
                "role_tenures": copy.deepcopy(person.get("role_tenures") or []),
            }

        if "bio" in sections:
            result["bio"] = copy.deepcopy(bio)

        if "career_stats" in sections:
            modes = {str(s.get("career_coverage_mode") or "") for s in stats_rows if s.get("career_coverage_mode")}
            coverage_mode = (
                "FULL_CBA_CAREER"
                if modes == {"FULL_CBA_CAREER"}
                else ("PARTIAL_SEASONS_OBSERVED" if stats_rows else "NO_STATS_OBSERVED")
            )
            result["career_stats"] = {
                "career_coverage_mode": coverage_mode,
                "seasons": copy.deepcopy(stats_rows),
            }

        if "recent_games" in sections:
            cap = DEFAULT_PUBLIC_GAME_LOGS_CAP if not tier_allows_internal(tier) else len(game_rows)
            result["recent_games"] = copy.deepcopy(game_rows[:cap])

        if "youth_national" in sections:
            enriched_youth = []
            for y in youth_rows:
                yc = copy.deepcopy(y)
                stage = str(yc.get("participation_stage") or "")
                if stage in _YOUTH_STAGE_LABELS_ZH:
                    yc["participation_stage_zh"] = _YOUTH_STAGE_LABELS_ZH[stage]
                enriched_youth.append(yc)
            result["youth_national"] = enriched_youth

        if "overseas" in sections:
            result["overseas"] = copy.deepcopy(overseas_rows)

        if "stories" in sections:
            result["stories"] = person_stories

        if "social" in sections:
            from .social_workbench import project_social_for_person

            social_vis = "public_safe" if tier == "public" else "internal_full"
            result["social"] = project_social_for_person(
                uid,
                profiles=self.social_profiles,
                posts=self.social_posts,
                visibility=social_vis,
            )

        if "references" in sections:
            ref_sources = [person, bio, stats_rows, overseas_rows, youth_rows, person_stories]
            result["references"] = _extract_https_urls(ref_sources)

        return _sanitize_value_for_tier(result, tier)

    # ------------------------------------------------------------------
    # Tool 4: get_club_roster
    # ------------------------------------------------------------------
    def tool_get_club_roster(
        self,
        arguments: Mapping[str, Any],
        auth_context: AuthContext,
    ) -> Dict[str, Any]:
        tier = auth_context.effective_tier
        club_query = str(arguments.get("club") or "").strip()
        season = str(arguments.get("season") or "").strip()

        if not club_query:
            if len(self.organizations) == 1:
                only_org = self.organizations[0]
                club_query = str(
                    only_org.get("short_name")
                    or only_org.get("canonical_name")
                    or only_org.get("organization_id")
                    or ""
                ).strip()
            else:
                return {
                    "status": "CLUB_PARAMETER_REQUIRED",
                    "message": "多俱乐部数据集下请指定 club 参数（如 深圳、广东 或 organization_id）。",
                    "available_clubs": [
                        {
                            "organization_id": o.get("organization_id"),
                            "canonical_name": o.get("canonical_name"),
                            "short_name": o.get("short_name"),
                        }
                        for o in self.organizations
                    ],
                    "effective_tier": tier,
                }

        club_display, org_id, _ = self._resolve_club_info(club_query)

        ledger_entries: List[Dict[str, Any]] = []
        for entry in self.roster_ledger:
            if not self._club_matches(entry.get("club"), club_query):
                continue
            if season and str(entry.get("season") or "").strip() != season:
                continue
            ledger_entries.append(copy.deepcopy(entry))

        movement_ledger: Dict[str, List[Dict[str, Any]]] = {}
        contract_dist: Dict[str, int] = {}
        active_domestic: List[Dict[str, Any]] = []
        active_foreign: List[Dict[str, Any]] = []

        for entry in ledger_entries:
            cat = str(entry.get("movement_category") or "UNKNOWN")
            movement_ledger.setdefault(cat, []).append(entry)
            ctype = str(entry.get("contract_type") or "UNKNOWN")
            contract_dist[ctype] = contract_dist.get(ctype, 0) + 1
            if str(entry.get("status") or "").upper() == "ACTIVE":
                if ctype == "外援" or "FOREIGN" in cat.upper():
                    active_foreign.append(entry)
                else:
                    active_domestic.append(entry)

        # Fallback / supplement from MASTER.csv if ledger had no active domestic
        if not active_domestic:
            for row in self.master_rows:
                if not self._club_matches(row.get("club"), club_query):
                    continue
                if season and str(row.get("season") or "").strip() != season:
                    continue
                active_domestic.append(copy.deepcopy(row))

        # 3-facet coaching staff view
        coaching_staff: Optional[Dict[str, Any]] = None
        clubs_facets = self.coach_role_facets.get("clubs") if isinstance(self.coach_role_facets, Mapping) else None
        if isinstance(clubs_facets, Mapping):
            for c_name, c_data in clubs_facets.items():
                if self._club_matches(c_name, club_query):
                    if isinstance(c_data, Mapping):
                        if not season or str(c_data.get("season") or "") in ("", season):
                            coaching_staff = copy.deepcopy(dict(c_data))
                            break

        result = {
            "club": club_display,
            "organization_id": org_id,
            "season": season or "ALL",
            "movement_ledger": movement_ledger,
            "ledger_entries": ledger_entries,
            "active_roster": {
                "domestic": active_domestic,
                "foreign": active_foreign,
            },
            "contract_distribution": contract_dist,
            "coaching_staff": coaching_staff,
            "effective_tier": tier,
        }
        return _sanitize_value_for_tier(result, tier)

    # ------------------------------------------------------------------
    # Tool 5: query_stats_and_games
    # ------------------------------------------------------------------
    def tool_query_stats_and_games(
        self,
        arguments: Mapping[str, Any],
        auth_context: AuthContext,
    ) -> Dict[str, Any]:
        tier = auth_context.effective_tier
        mode = str(arguments.get("mode") or "").strip()
        if mode not in ("season_stats", "game_logs"):
            raise ValueError(f"Invalid mode: {mode!r}")

        person_uid = str(arguments.get("person_uid") or "").strip()
        name = str(arguments.get("name") or "").strip()
        if not person_uid and name:
            person, _ = self._resolve_single_person(person_uid=None, name=name)
            if person is not None:
                person_uid = str(person["person_uid"])

        season = str(arguments.get("season") or "").strip()
        match_type = str(arguments.get("match_type") or "").strip()
        club = str(arguments.get("club") or "").strip()
        limit = arguments.get("limit")

        if mode == "season_stats":
            rows: List[Dict[str, Any]] = []
            for s in self.season_stats:
                if person_uid and str(s.get("person_uid") or "") != person_uid:
                    continue
                if not person_uid and name and _normalize_text(name) not in _normalize_text(s.get("player_name")):
                    continue
                if season and str(s.get("season") or "").strip() != season:
                    continue
                if match_type and str(s.get("match_type") or "").strip() != match_type:
                    continue
                if club and not self._club_matches(s.get("club"), club):
                    continue
                rows.append(copy.deepcopy(s))

            sort_by = str(arguments.get("sort_by") or "season")
            if sort_by == "season":
                rows.sort(key=lambda r: str(r.get("season") or ""), reverse=True)
            if isinstance(limit, int) and limit > 0:
                rows = rows[:limit]

            modes = {str(r.get("career_coverage_mode") or "") for r in rows if r.get("career_coverage_mode")}
            if not rows:
                coverage_mode = "NO_STATS_OBSERVED"
            elif modes == {"FULL_CBA_CAREER"}:
                coverage_mode = "FULL_CBA_CAREER"
            else:
                coverage_mode = "PARTIAL_SEASONS_OBSERVED"

            total_games = sum(int(r.get("games_played") or 0) for r in rows)
            if total_games > 0:
                weighted_ppg = round(
                    sum(float(r.get("points_per_game") or 0.0) * int(r.get("games_played") or 0) for r in rows)
                    / total_games,
                    2,
                )
                weighted_rpg = round(
                    sum(float(r.get("rebounds_per_game") or 0.0) * int(r.get("games_played") or 0) for r in rows)
                    / total_games,
                    2,
                )
                weighted_apg = round(
                    sum(float(r.get("assists_per_game") or 0.0) * int(r.get("games_played") or 0) for r in rows)
                    / total_games,
                    2,
                )
            else:
                weighted_ppg = weighted_rpg = weighted_apg = None

            result = {
                "mode": "season_stats",
                "career_coverage_mode": coverage_mode,
                "count": len(rows),
                "rows": rows,
                "aggregate": {
                    "total_games_played": total_games,
                    "weighted_points_per_game": weighted_ppg,
                    "weighted_rebounds_per_game": weighted_rpg,
                    "weighted_assists_per_game": weighted_apg,
                },
                "effective_tier": tier,
            }
            return _sanitize_value_for_tier(result, tier)

        # mode == "game_logs"
        is_public_tier = not tier_allows_internal(tier) and tier == "public"
        opponent = str(arguments.get("opponent") or "").strip()
        date_from = str(arguments.get("date_from") or "").strip()
        date_to = str(arguments.get("date_to") or "").strip()
        min_points = arguments.get("min_points")
        min_rebounds = arguments.get("min_rebounds")
        min_assists = arguments.get("min_assists")
        sort_by = str(arguments.get("sort_by") or "game_date")

        games: List[Dict[str, Any]] = []
        for g in self.game_logs:
            if person_uid and str(g.get("person_uid") or "") != person_uid:
                continue
            if not person_uid and name and _normalize_text(name) not in _normalize_text(g.get("player_name")):
                continue
            if season and str(g.get("season") or "").strip() != season:
                continue
            if match_type and str(g.get("match_type") or "").strip() != match_type:
                continue
            if club and not self._club_matches(g.get("club"), club):
                continue

            if not is_public_tier:
                if opponent and _normalize_text(opponent) not in _normalize_text(g.get("opponent")):
                    continue
                gdate = str(g.get("game_date") or "")
                if date_from and gdate < date_from:
                    continue
                if date_to and gdate > date_to:
                    continue
                if min_points is not None and float(g.get("points") or 0) < float(min_points):
                    continue
                if min_rebounds is not None and float(g.get("rebounds") or 0) < float(min_rebounds):
                    continue
                if min_assists is not None and float(g.get("assists") or 0) < float(min_assists):
                    continue

            games.append(copy.deepcopy(g))

        if sort_by in ("points", "rebounds", "assists"):
            games.sort(key=lambda r: float(r.get(sort_by) or 0.0), reverse=True)
        else:
            games.sort(key=lambda r: str(r.get("game_date") or ""), reverse=True)

        if is_public_tier:
            eff_limit = min(int(limit) if isinstance(limit, int) and limit > 0 else DEFAULT_PUBLIC_GAME_LOGS_CAP, DEFAULT_PUBLIC_GAME_LOGS_CAP)
            games = games[:eff_limit]
        elif isinstance(limit, int) and limit > 0:
            games = games[:limit]

        result = {
            "mode": "game_logs",
            "count": len(games),
            "games": games,
            "public_capped": is_public_tier,
            "effective_tier": tier,
        }
        return _sanitize_value_for_tier(result, tier)

    # ------------------------------------------------------------------
    # Tool 6: verify_statement
    # ------------------------------------------------------------------
    def _known_club_names(self) -> List[Tuple[str, str]]:
        """Return list of (alias_or_name, canonical_display) sorted longest-first."""
        pairs: List[Tuple[str, str]] = []
        seen: Set[str] = set()
        for org in self.organizations:
            display = str(org.get("short_name") or org.get("canonical_name") or "")
            for n in [org.get("canonical_name"), org.get("short_name"), *(org.get("aliases") or ())]:
                if isinstance(n, str) and n.strip() and n.strip() not in seen:
                    seen.add(n.strip())
                    pairs.append((n.strip(), display))
        for p in self.persons:
            for key in ("primary_club", "current_club"):
                val = str(p.get(key) or "").strip()
                if val and val not in seen:
                    seen.add(val)
                    pairs.append((val, val))
        for r in self.master_rows:
            val = str(r.get("club") or "").strip()
            if val and val not in seen:
                seen.add(val)
                pairs.append((val, val))
        pairs.sort(key=lambda item: len(item[0]), reverse=True)
        return pairs

    def _find_person_in_sentence(
        self,
        sentence: str,
        person_hint: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        if person_hint:
            p, _ = self._resolve_single_person(
                person_uid=person_hint if person_hint in self._person_by_uid else None,
                name=None if person_hint in self._person_by_uid else person_hint,
            )
            if p is not None:
                return p
        for p in self.persons:
            cname = str(p.get("canonical_name") or "").strip()
            if cname and cname in sentence:
                return p
            for alias in p.get("aliases") or ():
                a = str(alias or "").strip()
                if a and len(a) >= 2 and a in sentence:
                    return p
        return None

    def _verify_single_sentence(
        self,
        sentence: str,
        *,
        person_hint: Optional[str],
        club_hint: Optional[str],
        season_hint: Optional[str],
        tier: str,
    ) -> Dict[str, Any]:
        allow_quotes = tier_allows_quote_spans(tier)
        season_match = _SEASON_RE.search(sentence)
        season = season_match.group(0) if season_match else (season_hint or "")

        # 1) Check 3-facet coaching staff (coach_role_facets.json)
        if any(kw in sentence for kw in ("主教练", "主帅", "执教", "教练")):
            clubs_facets = (
                self.coach_role_facets.get("clubs")
                if isinstance(self.coach_role_facets, Mapping)
                else None
            )
            if isinstance(clubs_facets, Mapping):
                for c_name, c_data in clubs_facets.items():
                    if not isinstance(c_data, Mapping):
                        continue
                    if (club_hint and self._club_matches(c_name, club_hint)) or (c_name in sentence) or any(
                        al in sentence for al in self._club_aliases_for(c_name) if len(al) >= 2
                    ):
                        reg = c_data.get("official_registered_head_coach") or {}
                        ann = c_data.get("club_announced_head_coach") or {}
                        act = c_data.get("acting_or_on_court_head_coach") or {}
                        reg_name = str(reg.get("name") or "").strip()
                        act_name = str(act.get("name") or "").strip()
                        cites = _extract_https_urls(c_data)

                        if act_name and act_name in sentence and reg_name and act_name != reg_name:
                            return {
                                "sentence": sentence,
                                "verdict": "MULTI_SOURCE_DISCREPANCY",
                                "reason": str(
                                    c_data.get("discrepancy_note")
                                    or f"官方注册主教练为{reg_name}，而{act_name}为临场执行主教练。"
                                ),
                                "citations": cites,
                            }
                        if reg_name and reg_name in sentence:
                            return {
                                "sentence": sentence,
                                "verdict": "VERIFIED_MATCH",
                                "reason": f"官方注册主教练确为{reg_name}。",
                                "citations": cites,
                            }

        # 2) Resolve person for registration / stories / youth / overseas checks
        person = self._find_person_in_sentence(sentence, person_hint)
        if person is not None:
            uid = str(person["person_uid"])

            # 2a) Club affiliation check (MASTER.csv, roster_ledger, role_tenures)
            mentioned_clubs: List[str] = []
            for alias, canonical_disp in self._known_club_names():
                if alias in sentence and canonical_disp not in mentioned_clubs:
                    mentioned_clubs.append(canonical_disp)

            if mentioned_clubs and any(
                kw in sentence for kw in ("效力", "注册", "加盟", "所属", "赛季", "队", "俱乐部")
            ):
                canonical_clubs: Set[str] = set()
                cites: List[str] = []
                for row in self.master_rows:
                    if str(row.get("person_uid") or "") == uid:
                        if not season or str(row.get("season") or "") == season:
                            canonical_clubs.add(str(row.get("club") or ""))
                for entry in self.roster_ledger:
                    if str(entry.get("person_uid") or "") == uid:
                        if not season or str(entry.get("season") or "") == season:
                            canonical_clubs.add(str(entry.get("club") or ""))
                for tenure in person.get("role_tenures") or ():
                    if isinstance(tenure, Mapping):
                        canonical_clubs.add(str(tenure.get("organization_name") or ""))
                        cites.extend(_extract_https_urls(tenure))
                if not canonical_clubs and person.get("current_club"):
                    canonical_clubs.add(str(person["current_club"]))

                canonical_clubs.discard("")
                if canonical_clubs:
                    matched_any = any(
                        any(self._club_matches(cc, mc) for cc in canonical_clubs)
                        for mc in mentioned_clubs
                    )
                    if matched_any:
                        return {
                            "sentence": sentence,
                            "verdict": "VERIFIED_MATCH",
                            "reason": f"与权威注册/档案记录一致（{', '.join(sorted(canonical_clubs))}）。",
                            "citations": cites,
                        }
                    return {
                        "sentence": sentence,
                        "verdict": "CONTRADICTS_CANONICAL_DATA",
                        "reason": (
                            f"陈述提及 {', '.join(mentioned_clubs)}，但权威记录显示为 "
                            f"{', '.join(sorted(canonical_clubs))}。"
                        ),
                        "citations": cites,
                    }

            # 2b) Youth national tenure check
            youth_rows = [y for y in self.youth_tenures if str(y.get("person_uid") or "") == uid]
            for y in youth_rows:
                comp = str(y.get("competition_or_program") or "")
                stage = str(y.get("participation_stage") or "")
                stage_zh = _YOUTH_STAGE_LABELS_ZH.get(stage, "")
                if comp and (comp in sentence or (stage_zh and stage_zh in sentence)):
                    return {
                        "sentence": sentence,
                        "verdict": "VERIFIED_MATCH",
                        "reason": f"与国字号/青年赛事记录一致（{comp}：{stage_zh or stage}）。",
                        "citations": _extract_https_urls(y),
                    }

            # 2c) Overseas tenure check
            overseas_rows = [o for o in self.overseas_tenures if str(o.get("person_uid") or "") == uid]
            for o in overseas_rows:
                org_name = str(o.get("organization_name") or "")
                league = str(o.get("league_name") or "")
                if (org_name and org_name in sentence) or (league and league in sentence):
                    return {
                        "sentence": sentence,
                        "verdict": "VERIFIED_MATCH",
                        "reason": f"与 D-01 海外经历事实索引一致（{league} / {org_name}）。",
                        "citations": _extract_https_urls(o),
                    }

            # 2d) StoryRecord check (with tier-controlled quote_anchors)
            for s in self.stories:
                parts = s.get("participants") or []
                if not any(isinstance(pt, Mapping) and str(pt.get("person_uid") or "") == uid for pt in parts):
                    continue
                pub_allowed = (
                    isinstance(s.get("visibility_policy"), Mapping)
                    and s["visibility_policy"].get("public_export_allowed") is True
                )
                if not tier_allows_internal(tier) and not pub_allowed:
                    continue

                corpus_parts = [
                    str(s.get("title") or ""),
                    str(s.get("organization_summary") or ""),
                ]
                for pt in parts:
                    if isinstance(pt, Mapping):
                        corpus_parts.append(str(pt.get("public_perspective_summary") or ""))
                corpus = " ".join(corpus_parts)

                # Compute CJK 2-gram overlap between sentence and story summary
                sent_clean = re.sub(r"\s+", "", sentence)
                corp_clean = re.sub(r"\s+", "", corpus)
                if len(sent_clean) >= 4:
                    sent_grams = {sent_clean[i : i + 2] for i in range(len(sent_clean) - 1)}
                    corp_grams = {corp_clean[i : i + 2] for i in range(len(corp_clean) - 1)}
                    overlap = len(sent_grams & corp_grams) / max(1, len(sent_grams))
                    if overlap >= 0.45:
                        claim_out: Dict[str, Any] = {
                            "sentence": sentence,
                            "verdict": "VERIFIED_MATCH",
                            "reason": f"与归档报道《{s.get('title') or ''}》一致。",
                            "citations": _extract_https_urls(s.get("canonical_source")),
                        }
                        if allow_quotes and s.get("quote_anchors"):
                            claim_out["quote_anchors"] = copy.deepcopy(s["quote_anchors"])
                        return claim_out

            # 2e) Social post self-claim check (SELF_CLAIM_MATCH)
            for sp in self.social_posts:
                if str(sp.get("person_uid") or "") != uid:
                    continue
                if str(sp.get("status") or "ACTIVE").upper() == "WITHDRAWN":
                    continue
                ep_kind = str(sp.get("epistemic_source_kind") or "").upper()
                if ep_kind == "DERIVED_TRANSCRIPT" and not sp.get("transcript_human_confirmed"):
                    if tier == "public":
                        continue
                sp_text = str(sp.get("summary_or_quote") or "")
                sent_clean = re.sub(r"\s+", "", sentence)
                sp_clean = re.sub(r"\s+", "", sp_text)
                if len(sent_clean) >= 4 and len(sp_clean) >= 4:
                    sent_grams = {sent_clean[i : i + 2] for i in range(len(sent_clean) - 1)}
                    sp_grams = {sp_clean[i : i + 2] for i in range(len(sp_clean) - 1)}
                    overlap = len(sent_grams & sp_grams) / max(1, len(sent_grams))
                    if overlap >= 0.35:
                        cat = str(sp.get("topic_category") or "").upper()
                        if self.demand_ledger_path is not None and cat in (
                            "INJURY_RECOVERY",
                            "SIGNING_DEPARTURE_NOTE",
                        ):
                            from .demand_ledger import record_demand_miss

                            record_demand_miss(
                                self.demand_ledger_path,
                                query_key=f"{person.get('canonical_name', '')}:{cat}",
                                miss_reason="SOCIAL_EARLY_SIGNAL_UNCONFIRMED_OFFICIALLY",
                                signal_kind=cat,
                            )
                        return {
                            "sentence": sentence,
                            "verdict": "SELF_CLAIM_MATCH",
                            "reason": "该说法与当事人在核验社媒发布的个人动态一致（SELF_STATEMENT），但尚无 CBA 官方或俱乐部公告确认，引用时须注明系本人社媒表态。",
                            "citations": [str(sp.get("clean_post_url") or "")],
                        }

        # 3) Fallback: no canonical basis
        return {
            "sentence": sentence,
            "verdict": "UNVERIFIED_NO_BASIS",
            "reason": "知识库当前快照中无支持或反驳该陈述的权威证据。",
            "citations": [],
        }

    def tool_verify_statement(
        self,
        arguments: Mapping[str, Any],
        auth_context: AuthContext,
    ) -> Dict[str, Any]:
        tier = auth_context.effective_tier
        statement = str(arguments.get("statement") or "").strip()
        if not statement:
            raise ValueError("Missing required argument: statement")

        person_hint = str(arguments.get("person_hint") or "").strip() or None
        club_hint = str(arguments.get("club_hint") or "").strip() or None
        season_hint = str(arguments.get("season") or "").strip() or None

        raw_sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(statement) if s.strip()]
        if not raw_sentences:
            raw_sentences = [statement]

        claims = [
            self._verify_single_sentence(
                sent,
                person_hint=person_hint,
                club_hint=club_hint,
                season_hint=season_hint,
                tier=tier,
            )
            for sent in raw_sentences
        ]

        verdicts = [c["verdict"] for c in claims]
        if "CONTRADICTS_CANONICAL_DATA" in verdicts:
            overall = "CONTRADICTS_CANONICAL_DATA"
        elif "MULTI_SOURCE_DISCREPANCY" in verdicts:
            overall = "MULTI_SOURCE_DISCREPANCY"
        elif all(v == "VERIFIED_MATCH" for v in verdicts):
            overall = "VERIFIED_MATCH"
        elif "SELF_CLAIM_MATCH" in verdicts:
            overall = "SELF_CLAIM_MATCH"
        else:
            overall = "UNVERIFIED_NO_BASIS"

        result = {
            "statement": statement,
            "verdict": overall,
            "claims": claims,
            "effective_tier": tier,
        }
        return _sanitize_value_for_tier(result, tier)

    # ------------------------------------------------------------------
    # Unified Dispatcher
    # ------------------------------------------------------------------
    def call_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        auth_context: AuthContext,
    ) -> Dict[str, Any]:
        dispatch = {
            "kb_status": self.tool_kb_status,
            "resolve_entity": self.tool_resolve_entity,
            "get_person_dossier": self.tool_get_person_dossier,
            "get_club_roster": self.tool_get_club_roster,
            "query_stats_and_games": self.tool_query_stats_and_games,
            "verify_statement": self.tool_verify_statement,
        }
        handler = dispatch.get(tool_name)
        if handler is None:
            raise KeyError(f"Unknown tool: {tool_name}")

        # Enforce additionalProperties: false from TIERED_TOOL_DEFINITIONS
        tool_def = next(t for t in TIERED_TOOL_DEFINITIONS if t["name"] == tool_name)
        allowed_keys = set(tool_def["inputSchema"].get("properties", {}).keys())
        extra_keys = set(arguments.keys()) - allowed_keys
        if extra_keys:
            raise ValueError(f"Unexpected arguments for {tool_name}: {sorted(extra_keys)}")

        return handler(arguments, auth_context)
