"""Human-curated social media workbench & safe projection (REQ-251 P0-E2).

100% Python stdlib, 0 crawlers, 0 audio/video files, 0 tracking query params.
- `export_social_workbench_csv`: idempotent CSV export from `person_registry`,
  excluding `CHRONICLER` and unverified minors, preserving human-filled rows.
- `clean_social_url_and_share_text`: strips Douyin/Xiaohongshu share-text prose
  and tracking parameters (`xsec_token`, `share_id`, `utm_*`, `sec_uid`, etc.).
- `bind_social_account` & `apply_social_workbench_csv`: enforces
  `FAN_REPOST_WARNING`, `IMPERSONATOR` hard block, and `WITHDRAWN` takedown.
- `validate_social_post` & `project_social_for_person`: enforces 5 career topic
  categories, `SELF_STATEMENT` vs `CLUB_OFFICIAL_ANNOUNCEMENT` epistemic boundary,
  and `DERIVED_TRANSCRIPT` quarantine when `transcript_human_confirmed` is False.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .common import atomic
from .person_registry import classify_person_category

WORKBENCH_SCHEMA = "cba-kb.social-workbench.v1"
SOCIAL_PROFILES_SCHEMA = "cba-kb.social-profiles.v1"
SOCIAL_POST_SCHEMA = "cba-kb.social-post.v1"

WORKBENCH_CSV_COLUMNS: tuple[str, ...] = (
    "person_uid",
    "canonical_name",
    "club",
    "entity_role",
    "platform",
    "account_handle_or_id",
    "clean_profile_url",
    "raw_input_or_share_text",
    "verification_badge",
    "account_status",
    "operator_type",
    "content_focus_summary",
    "notes",
)

VALID_PLATFORMS = frozenset({"DOUYIN", "XIAOHONGSHU"})
VALID_ACCOUNT_STATUSES = frozenset({
    "HUMAN_VERIFIED",
    "CANDIDATE_UNVERIFIED",
    "IMPERSONATOR",
    "WITHDRAWN",
    "NONE",
})
VALID_OPERATOR_TYPES = frozenset({
    "CLUB_OFFICIAL_BLUE_V",
    "ATHLETE_SELF",
    "COACH_SELF",
    "FAN_REPOST_WARNING",
    "MEDIA_OUTLET",
})
VALID_TOPIC_CATEGORIES = frozenset({
    "INJURY_RECOVERY",
    "TRAINING_MATCH",
    "SIGNING_DEPARTURE_NOTE",
    "TEAMMATE_COACH_INTERACTION",
    "EARLY_CAREER_MEMORY",
})
TOPIC_CATEGORY_LABELS_ZH: Dict[str, str] = {
    "INJURY_RECOVERY": "伤病康复",
    "TRAINING_MATCH": "特训比赛",
    "SIGNING_DEPARTURE_NOTE": "签约离队感言",
    "TEAMMATE_COACH_INTERACTION": "队友教练互动",
    "EARLY_CAREER_MEMORY": "早年履历",
}
VALID_EPISTEMIC_SOURCE_KINDS = frozenset({
    "CLUB_OFFICIAL_ANNOUNCEMENT",
    "FIRST_PARTY_POST_SPAN",
    "DERIVED_TRANSCRIPT",
})
FORBIDDEN_MEDIA_EXTENSIONS = frozenset({
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".mp3", ".wav", ".m4a", ".aac",
})
_TRACKING_PARAM_EXACT = frozenset({
    "xsec_token",
    "xsec_source",
    "share_id",
    "sec_uid",
    "u_code",
    "did",
    "iid",
    "app",
    "source",
    "share_from",
    "timestamp",
    "xhsshare",
    "apptime",
})
_FAN_REPOST_KEYWORDS = ("粉丝", "后援会", "搬运", "剪辑", "球迷")
_PRIVATE_LIFE_KEYWORDS = ("家庭住址", "私宅", "身份证", "手机号", "父母住址", "配偶隐私")
_URL_EXTRACT_RE = re.compile(r"https?://[^\s<>\"'，。；：！？（）【】]+")


class SocialWorkbenchError(ValueError):
    """Raised when a social account or post violates safety, media, or schema rules."""


def reject_forbidden_media_path(path_or_url: Any) -> None:
    """Hard-block any audio/video file extension from entering KB or cloud bundles."""
    if not isinstance(path_or_url, str):
        return
    clean = urlsplit(path_or_url.strip()).path.lower() if "://" in path_or_url else path_or_url.strip().lower()
    for ext in FORBIDDEN_MEDIA_EXTENSIONS:
        if clean.endswith(ext):
            raise SocialWorkbenchError(f"FORBIDDEN_AUDIO_VIDEO_ASSET:{ext}")


def clean_social_url_and_share_text(raw_input: str) -> str:
    """Extract URL from Douyin/Xiaohongshu share text and strip all tracking params."""
    if not isinstance(raw_input, str) or not raw_input.strip():
        return ""
    text = raw_input.strip()
    m = _URL_EXTRACT_RE.search(text)
    url = m.group(0) if m else text
    reject_forbidden_media_path(url)
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return ""
    kept_params = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAM_EXACT
    ]
    clean_query = urlencode(kept_params)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, clean_query, ""))


def detect_fan_repost_warning(handle_or_id: str, summary: str = "") -> bool:
    """Return True if account name or summary suggests a fan/repost account."""
    combined = f"{handle_or_id or ''} {summary or ''}"
    return any(kw in combined for kw in _FAN_REPOST_KEYWORDS)


def export_social_workbench_csv(
    person_registry: Mapping[str, Any],
    output_path: Path | str,
    *,
    reference_year: int = 2026,
) -> Dict[str, Any]:
    """Idempotently export pre-filled social workbench CSV excluding CHRONICLER & minors."""
    out = Path(output_path)
    existing_by_key: Dict[tuple[str, str], Dict[str, str]] = {}
    if out.is_file():
        with out.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                uid = str(row.get("person_uid") or "").strip()
                plat = str(row.get("platform") or "").strip().upper()
                if uid:
                    existing_by_key[(uid, plat)] = {col: str(row.get(col) or "") for col in WORKBENCH_CSV_COLUMNS}

    rows: List[Dict[str, str]] = []
    exported_uids: List[str] = []
    for p in person_registry.get("persons") or ():
        if not isinstance(p, Mapping):
            continue
        if classify_person_category(p) == "CHRONICLER":
            continue
        birth_year = p.get("birth_year")
        if isinstance(birth_year, int) and not isinstance(birth_year, bool):
            if (reference_year - birth_year) < 18:
                continue
        uid = str(p.get("person_uid") or "").strip()
        if not uid:
            continue
        cname = str(p.get("canonical_name") or "").strip()
        club = str(p.get("current_club") or p.get("primary_club") or "").strip()
        role = str(p.get("primary_role") or "PLAYER").strip()

        # Preserve any existing human-filled rows for this person
        matched_existing = [v for (k_uid, _), v in existing_by_key.items() if k_uid == uid]
        if matched_existing:
            rows.extend(matched_existing)
        else:
            rows.append({
                "person_uid": uid,
                "canonical_name": cname,
                "club": club,
                "entity_role": role,
                "platform": "",
                "account_handle_or_id": "",
                "clean_profile_url": "",
                "raw_input_or_share_text": "",
                "verification_badge": "",
                "account_status": "NONE",
                "operator_type": "",
                "content_focus_summary": "",
                "notes": "",
            })
        exported_uids.append(uid)

    out.parent.mkdir(parents=True, exist_ok=True)
    import io

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(WORKBENCH_CSV_COLUMNS), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    atomic(out, buf.getvalue().encode("utf-8"))
    return {
        "schema": WORKBENCH_SCHEMA,
        "output_path": str(out),
        "exported_count": len(exported_uids),
        "row_count": len(rows),
    }


def bind_social_account(
    *,
    person_uid: str,
    canonical_name: str,
    platform: str,
    account_handle_or_id: str,
    raw_input_or_share_text: str,
    operator_type: str = "ATHLETE_SELF",
    account_status: str = "HUMAN_VERIFIED",
    verification_badge: str = "",
    content_focus_summary: str = "",
    club: str = "",
    notes: str = "",
) -> Dict[str, Any]:
    """Clean, validate, and bind a single social account with anti-impersonator & fan guards."""
    plat = str(platform or "").strip().upper()
    if plat not in VALID_PLATFORMS:
        raise SocialWorkbenchError(f"INVALID_SOCIAL_PLATFORM:{platform}")
    status = str(account_status or "").strip().upper()
    if status not in VALID_ACCOUNT_STATUSES:
        raise SocialWorkbenchError(f"INVALID_ACCOUNT_STATUS:{account_status}")
    if status == "IMPERSONATOR":
        raise SocialWorkbenchError("IMPERSONATOR_ACCOUNT_BLOCKED")
    op_type = str(operator_type or "").strip().upper()
    if op_type and op_type not in VALID_OPERATOR_TYPES:
        raise SocialWorkbenchError(f"INVALID_OPERATOR_TYPE:{operator_type}")

    clean_url = clean_social_url_and_share_text(raw_input_or_share_text)
    warning: Optional[str] = None
    if detect_fan_repost_warning(account_handle_or_id, content_focus_summary):
        warning = "FAN_REPOST_WARNING"
        if status == "HUMAN_VERIFIED":
            status = "CANDIDATE_UNVERIFIED"
        if op_type != "FAN_REPOST_WARNING":
            op_type = "FAN_REPOST_WARNING"

    return {
        "person_uid": str(person_uid).strip(),
        "canonical_name": str(canonical_name).strip(),
        "club": str(club).strip(),
        "platform": plat,
        "account_handle_or_id": str(account_handle_or_id).strip(),
        "clean_profile_url": clean_url,
        "verification_badge": str(verification_badge).strip(),
        "account_status": status,
        "operator_type": op_type or "ATHLETE_SELF",
        "content_focus_summary": str(content_focus_summary).strip(),
        "notes": str(notes).strip(),
        "warning": warning,
    }


def apply_social_workbench_csv(
    csv_path: Path | str,
    *,
    output_path: Optional[Path | str] = None,
) -> Dict[str, Any]:
    """Apply human-curated workbench CSV into sanitized `social_profiles.json`."""
    p = Path(csv_path)
    if not p.is_file():
        raise SocialWorkbenchError(f"WORKBENCH_CSV_NOT_FOUND:{p.name}")

    profiles: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    skipped_withdrawn = 0

    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            status = str(row.get("account_status") or "NONE").strip().upper()
            if status in ("NONE", ""):
                continue
            if status == "WITHDRAWN":
                skipped_withdrawn += 1
                continue
            if status == "IMPERSONATOR":
                raise SocialWorkbenchError(
                    f"IMPERSONATOR_ACCOUNT_BLOCKED:{row.get('person_uid')}"
                )
            plat = str(row.get("platform") or "").strip().upper()
            if not plat:
                continue
            raw_in = str(row.get("raw_input_or_share_text") or row.get("clean_profile_url") or "").strip()
            bound = bind_social_account(
                person_uid=str(row.get("person_uid") or "").strip(),
                canonical_name=str(row.get("canonical_name") or "").strip(),
                platform=plat,
                account_handle_or_id=str(row.get("account_handle_or_id") or "").strip(),
                raw_input_or_share_text=raw_in,
                operator_type=str(row.get("operator_type") or "ATHLETE_SELF").strip(),
                account_status=status,
                verification_badge=str(row.get("verification_badge") or "").strip(),
                content_focus_summary=str(row.get("content_focus_summary") or "").strip(),
                club=str(row.get("club") or "").strip(),
                notes=str(row.get("notes") or "").strip(),
            )
            if bound.get("warning"):
                warnings.append({
                    "person_uid": bound["person_uid"],
                    "warning": bound["warning"],
                    "account_handle_or_id": bound["account_handle_or_id"],
                })
            profiles.append(bound)

    payload = {
        "schema": SOCIAL_PROFILES_SCHEMA,
        "profiles": profiles,
        "warnings": warnings,
        "withdrawn_count": skipped_withdrawn,
    }
    if output_path is not None:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        atomic(out, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return payload


def validate_social_post(post: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate a `cba-kb.social-post.v1` record enforcing media, privacy, and epistemic rules."""
    if not isinstance(post, Mapping):
        raise SocialWorkbenchError("SOCIAL_POST_OBJECT_REQUIRED")
    schema = str(post.get("schema") or "").strip()
    if schema != SOCIAL_POST_SCHEMA:
        raise SocialWorkbenchError(f"INVALID_SOCIAL_POST_SCHEMA:{schema}")

    post_id = str(post.get("post_id") or "").strip()
    if not post_id:
        raise SocialWorkbenchError("POST_ID_REQUIRED")

    plat = str(post.get("platform") or "").strip().upper()
    if plat not in VALID_PLATFORMS:
        raise SocialWorkbenchError(f"INVALID_SOCIAL_PLATFORM:{plat}")

    raw_url = str(post.get("clean_post_url") or "").strip()
    reject_forbidden_media_path(raw_url)
    clean_url = clean_social_url_and_share_text(raw_url)
    if not clean_url:
        raise SocialWorkbenchError("VALID_CLEAN_POST_URL_REQUIRED")

    cat = str(post.get("topic_category") or "").strip().upper()
    if cat not in VALID_TOPIC_CATEGORIES:
        raise SocialWorkbenchError(f"INVALID_TOPIC_CATEGORY:{cat}")

    ep_kind = str(post.get("epistemic_source_kind") or "").strip().upper()
    if ep_kind not in VALID_EPISTEMIC_SOURCE_KINDS:
        raise SocialWorkbenchError(f"INVALID_EPISTEMIC_SOURCE_KIND:{ep_kind}")

    summary = str(post.get("summary_or_quote") or "").strip()
    if not summary:
        raise SocialWorkbenchError("SUMMARY_OR_QUOTE_REQUIRED")
    if any(kw in summary for kw in _PRIVATE_LIFE_KEYWORDS):
        raise SocialWorkbenchError("PRIVATE_LIFE_OR_ADDRESS_REJECTED")

    status = str(post.get("status") or "ACTIVE").strip().upper()
    if status not in ("ACTIVE", "WITHDRAWN"):
        raise SocialWorkbenchError(f"INVALID_POST_STATUS:{status}")

    confirmed = bool(post.get("transcript_human_confirmed", False))
    return {
        "schema": SOCIAL_POST_SCHEMA,
        "post_id": post_id,
        "person_uid": str(post.get("person_uid") or "").strip() or None,
        "organization_id": str(post.get("organization_id") or "").strip() or None,
        "platform": plat,
        "clean_post_url": clean_url,
        "published_at": str(post.get("published_at") or "").strip(),
        "topic_category": cat,
        "topic_category_zh": TOPIC_CATEGORY_LABELS_ZH[cat],
        "epistemic_source_kind": ep_kind,
        "summary_or_quote": summary,
        "transcript_human_confirmed": confirmed,
        "status": status,
        "raw_sha256": str(post.get("raw_sha256") or "").strip().lower(),
    }


def project_social_for_person(
    person_uid: str,
    *,
    profiles: Sequence[Mapping[str, Any]] = (),
    posts: Sequence[Mapping[str, Any]] = (),
    visibility: str = "public_safe",
) -> Dict[str, Any]:
    """Project verified social accounts and career posts for a person with epistemic & tier rules."""
    is_public = visibility == "public_safe"
    uid = str(person_uid or "").strip()

    accounts: List[Dict[str, Any]] = []
    for raw_acc in profiles or ():
        if not isinstance(raw_acc, Mapping):
            continue
        if str(raw_acc.get("person_uid") or "").strip() != uid:
            continue
        status = str(raw_acc.get("account_status") or "").strip().upper()
        if status in ("WITHDRAWN", "IMPERSONATOR", "NONE"):
            continue
        if is_public and status != "HUMAN_VERIFIED":
            continue
        accounts.append({
            "platform": str(raw_acc.get("platform") or "").upper(),
            "account_handle_or_id": str(raw_acc.get("account_handle_or_id") or "").strip(),
            "clean_profile_url": clean_social_url_and_share_text(
                str(raw_acc.get("clean_profile_url") or "")
            ),
            "verification_badge": str(raw_acc.get("verification_badge") or "").strip(),
            "account_status": status,
            "operator_type": str(raw_acc.get("operator_type") or "").strip(),
            "content_focus_summary": str(raw_acc.get("content_focus_summary") or "").strip(),
        })

    projected_posts: List[Dict[str, Any]] = []
    for raw_post in posts or ():
        if not isinstance(raw_post, Mapping):
            continue
        try:
            validated = validate_social_post(raw_post)
        except SocialWorkbenchError:
            continue
        if validated["person_uid"] != uid:
            continue
        if validated["status"] == "WITHDRAWN":
            continue
        if (
            is_public
            and validated["epistemic_source_kind"] == "DERIVED_TRANSCRIPT"
            and not validated["transcript_human_confirmed"]
        ):
            continue

        is_club_official = validated["epistemic_source_kind"] == "CLUB_OFFICIAL_ANNOUNCEMENT"
        ep_tag = (
            "[俱乐部官方蓝V · CLUB_OFFICIAL]"
            if is_club_official
            else "[本人社媒动态 · SELF_STATEMENT]"
        )
        projected_posts.append({
            **validated,
            "epistemic_tag": ep_tag,
            "is_self_statement": not is_club_official,
        })

    projected_posts.sort(key=lambda x: (x.get("published_at") or "", x.get("post_id") or ""), reverse=True)
    return {
        "accounts": accounts,
        "posts": projected_posts,
    }


def extract_early_social_demand_signals(
    posts: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Return early injury/signing signals from social posts that warrant official-source follow-up."""
    signals: List[Dict[str, Any]] = []
    for raw in posts or ():
        if not isinstance(raw, Mapping):
            continue
        cat = str(raw.get("topic_category") or "").strip().upper()
        status = str(raw.get("status") or "ACTIVE").strip().upper()
        if status == "ACTIVE" and cat in ("INJURY_RECOVERY", "SIGNING_DEPARTURE_NOTE"):
            signals.append({
                "person_uid": raw.get("person_uid"),
                "topic_category": cat,
                "published_at": raw.get("published_at"),
                "post_id": raw.get("post_id"),
            })
    return signals
