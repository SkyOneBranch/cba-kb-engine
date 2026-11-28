"""Dual-factor ACL gateway, tier visibility helpers, and redacted audit logger for REQ-250.

100% Python stdlib. Enforces fail-closed access control:
- Constant-time secret comparisons via ``hmac.compare_digest``.
- Direct Bearer token authentication (Channel A) ignores any ``X-DingTalk-*`` headers.
- DingTalk dual-factor authentication (Channel B) ONLY inspects ``X-DingTalk-User-Id``
  and ``X-DingTalk-User-Job-Number`` AFTER verifying the DingTalk gateway Bearer token.
- Anonymous access defaults to ``"deny"`` (HTTP 401); explicit ``"public"`` allows
  ``public_safe`` read-only access only.
- Audit logs record masked SHA-256 principal identifiers and NEVER write plaintext
  tokens or job numbers.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, IO, Mapping, Optional, Tuple

VALID_TIERS: Tuple[str, ...] = ("public", "club_media", "club_scout", "owner")
TIER_RANK: Dict[str, int] = {
    "public": 1,
    "club_media": 2,
    "club_scout": 3,
    "owner": 4,
}
ANONYMOUS_POLICIES: Tuple[str, ...] = ("deny", "public")


class MCPAuthError(Exception):
    """Raised when MCP authentication or authorization fails (fail-closed)."""

    def __init__(
        self,
        status_code: int = 401,
        code: str = "AUTHENTICATION_REQUIRED",
        message: Optional[str] = None,
    ) -> None:
        self.status_code = int(status_code)
        self.code = str(code)
        self.message = str(message or code)
        super().__init__(self.message)


@dataclass(frozen=True)
class AuthContext:
    """Resolved authentication & authorization context for a single MCP call."""

    effective_tier: str
    client_kind: str
    principal_masked: str
    authenticated: bool
    dingtalk_verified: bool = False

    def __post_init__(self) -> None:
        if self.effective_tier not in TIER_RANK:
            raise ValueError(f"INVALID_MCP_TIER: {self.effective_tier!r}")


@dataclass(frozen=True)
class DingTalkGatewayConfig:
    """Configuration for DingTalk AI Assistant / Qwen dual-factor gateway auth."""

    tokens: Tuple[str, ...] = ()
    user_id_tiers: Dict[str, str] = field(default_factory=dict)
    job_number_tiers: Dict[str, str] = field(default_factory=dict)
    default_authenticated_tier: str = "public"


@dataclass(frozen=True)
class MCPACLConfig:
    """In-memory ACL configuration loaded from ``mcp_acl.json`` or env JSON."""

    anonymous_tier: str = "deny"
    bearer_tokens: Dict[str, str] = field(default_factory=dict)
    dingtalk_gateway: DingTalkGatewayConfig = field(default_factory=DingTalkGatewayConfig)

    def __post_init__(self) -> None:
        if self.anonymous_tier not in ANONYMOUS_POLICIES:
            raise ValueError(f"INVALID_ANONYMOUS_TIER: {self.anonymous_tier!r}")
        for tier in self.bearer_tokens.values():
            if tier not in TIER_RANK:
                raise ValueError(f"INVALID_BEARER_TIER: {tier!r}")
        if self.dingtalk_gateway.default_authenticated_tier not in TIER_RANK:
            raise ValueError(
                f"INVALID_DINGTALK_DEFAULT_TIER: {self.dingtalk_gateway.default_authenticated_tier!r}"
            )
        for tier in self.dingtalk_gateway.user_id_tiers.values():
            if tier not in TIER_RANK:
                raise ValueError(f"INVALID_DINGTALK_USER_TIER: {tier!r}")
        for tier in self.dingtalk_gateway.job_number_tiers.values():
            if tier not in TIER_RANK:
                raise ValueError(f"INVALID_DINGTALK_JOB_TIER: {tier!r}")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MCPACLConfig":
        if not isinstance(data, Mapping):
            raise ValueError("MCP_ACL_MUST_BE_OBJECT")

        anon = str(data.get("anonymous_tier", "deny")).strip()
        if anon not in ANONYMOUS_POLICIES:
            raise ValueError(f"INVALID_ANONYMOUS_TIER: {anon!r}")

        raw_bearers = data.get("bearer_tokens") or {}
        bearer_map: Dict[str, str] = {}
        if isinstance(raw_bearers, Mapping):
            for tok, tier in raw_bearers.items():
                tok_s = str(tok).strip()
                tier_s = str(tier).strip()
                if not tok_s:
                    continue
                if tier_s not in TIER_RANK:
                    raise ValueError(f"INVALID_BEARER_TIER: {tier_s!r}")
                bearer_map[tok_s] = tier_s
        elif isinstance(raw_bearers, list):
            for item in raw_bearers:
                if not isinstance(item, Mapping):
                    raise ValueError("INVALID_BEARER_TOKEN_ENTRY")
                tok_s = str(item.get("token") or "").strip()
                tier_s = str(item.get("tier") or "").strip()
                if not tok_s:
                    continue
                if tier_s not in TIER_RANK:
                    raise ValueError(f"INVALID_BEARER_TIER: {tier_s!r}")
                bearer_map[tok_s] = tier_s
        else:
            raise ValueError("INVALID_BEARER_TOKENS_FIELD")

        raw_dt = data.get("dingtalk_gateway") or {}
        if not isinstance(raw_dt, Mapping):
            raise ValueError("INVALID_DINGTALK_GATEWAY_FIELD")

        dt_tokens_list = []
        if isinstance(raw_dt.get("token"), str) and raw_dt["token"].strip():
            dt_tokens_list.append(raw_dt["token"].strip())
        if isinstance(raw_dt.get("tokens"), (list, tuple)):
            for t in raw_dt["tokens"]:
                if isinstance(t, str) and t.strip():
                    dt_tokens_list.append(t.strip())

        uid_tiers: Dict[str, str] = {}
        raw_uid_tiers = raw_dt.get("user_id_tiers") or {}
        if isinstance(raw_uid_tiers, Mapping):
            for uid, tier in raw_uid_tiers.items():
                uid_s = str(uid).strip()
                tier_s = str(tier).strip()
                if uid_s:
                    if tier_s not in TIER_RANK:
                        raise ValueError(f"INVALID_DINGTALK_USER_TIER: {tier_s!r}")
                    uid_tiers[uid_s] = tier_s

        job_tiers: Dict[str, str] = {}
        raw_job_tiers = raw_dt.get("job_number_tiers") or {}
        if isinstance(raw_job_tiers, Mapping):
            for job, tier in raw_job_tiers.items():
                job_s = str(job).strip()
                tier_s = str(tier).strip()
                if job_s:
                    if tier_s not in TIER_RANK:
                        raise ValueError(f"INVALID_DINGTALK_JOB_TIER: {tier_s!r}")
                    job_tiers[job_s] = tier_s

        default_dt_tier = str(raw_dt.get("default_authenticated_tier", "public")).strip()
        if default_dt_tier not in TIER_RANK:
            raise ValueError(f"INVALID_DINGTALK_DEFAULT_TIER: {default_dt_tier!r}")

        return cls(
            anonymous_tier=anon,
            bearer_tokens=bearer_map,
            dingtalk_gateway=DingTalkGatewayConfig(
                tokens=tuple(dt_tokens_list),
                user_id_tiers=uid_tiers,
                job_number_tiers=job_tiers,
                default_authenticated_tier=default_dt_tier,
            ),
        )


def load_mcp_acl(
    acl_path: Optional[Path | str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> MCPACLConfig:
    """Load ACL config from file path or ``CBA_KB_MCP_ACL_JSON`` / ``CBA_KB_MCP_ACL_PATH``."""
    if acl_path is not None:
        path = Path(acl_path)
        if not path.is_file():
            raise ValueError(f"MCP_ACL_FILE_NOT_FOUND: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        return MCPACLConfig.from_dict(raw)

    env = environ if environ is not None else os.environ
    raw_json = (env.get("CBA_KB_MCP_ACL_JSON") or "").strip()
    if raw_json:
        return MCPACLConfig.from_dict(json.loads(raw_json))

    env_path = (env.get("CBA_KB_MCP_ACL_PATH") or "").strip()
    if env_path:
        path = Path(env_path)
        if not path.is_file():
            raise ValueError(f"MCP_ACL_FILE_NOT_FOUND: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        return MCPACLConfig.from_dict(raw)

    return MCPACLConfig(anonymous_tier="deny")


def mask_principal(raw: str, prefix: str = "id") -> str:
    """Return a non-reversible SHA-256 prefix mask for audit logs."""
    digest = hashlib.sha256(str(raw).encode("utf-8")).hexdigest()[:12]
    return f"{prefix}:sha256:{digest}"


def tier_visibility(tier: str) -> str:
    """Map an ACL tier to its fail-closed visibility mode."""
    if tier == "public":
        return "public_safe"
    if tier == "club_media":
        return "club_media"
    if tier in ("club_scout", "owner"):
        return "internal_full"
    raise ValueError(f"INVALID_MCP_TIER: {tier!r}")


def tier_allows_internal(tier: str) -> bool:
    """Return True iff the tier is authorized for internal_full clues/notes/full game logs."""
    return TIER_RANK.get(tier, 0) >= TIER_RANK["club_scout"]


def tier_allows_quote_spans(tier: str) -> bool:
    """Return True iff the tier is authorized for controlled quote_span attribution."""
    return TIER_RANK.get(tier, 0) >= TIER_RANK["club_media"]


def _match_bearer_token(candidate: str, token_map: Mapping[str, str]) -> Optional[str]:
    """Constant-time lookup of candidate bearer token in token_map."""
    matched_tier: Optional[str] = None
    for configured_token, tier in token_map.items():
        if hmac.compare_digest(candidate, configured_token):
            matched_tier = tier
    return matched_tier


def _match_gateway_token(candidate: str, gateway_tokens: Tuple[str, ...]) -> bool:
    """Constant-time check of candidate token against DingTalk gateway tokens."""
    matched = False
    for configured_token in gateway_tokens:
        if hmac.compare_digest(candidate, configured_token):
            matched = True
    return matched


def _lookup_constant_time(candidate: str, mapping: Mapping[str, str]) -> Optional[str]:
    if not candidate:
        return None
    matched_val: Optional[str] = None
    for key, val in mapping.items():
        if hmac.compare_digest(candidate, key):
            matched_val = val
    return matched_val


def authenticate_http_headers(
    headers: Mapping[str, str],
    acl: MCPACLConfig,
) -> AuthContext:
    """Authenticate an HTTP MCP request using Channel A (Bearer) or Channel B (DingTalk dual-factor).

    Security invariants:
    - All secret comparisons use ``hmac.compare_digest``.
    - Channel A (direct Bearer) ignores any ``X-DingTalk-*`` headers.
    - Missing/invalid Bearer unconditionally ignores ``X-DingTalk-*`` (zero privilege escalation).
    - Channel B reads ``X-DingTalk-User-Id`` / ``X-DingTalk-User-Job-Number`` ONLY after verifying
      the DingTalk gateway Bearer token.
    """
    lower_headers: Dict[str, str] = {
        str(k).strip().lower(): str(v).strip()
        for k, v in (headers or {}).items()
    }
    auth_raw = lower_headers.get("authorization", "")

    if auth_raw:
        parts = auth_raw.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
            raise MCPAuthError(401, "INVALID_TOKEN", "Malformed Authorization Bearer header")
        token = parts[1].strip()

        # Channel A: Direct Bearer token (ignores any X-DingTalk-* headers)
        direct_tier = _match_bearer_token(token, acl.bearer_tokens)
        if direct_tier is not None:
            return AuthContext(
                effective_tier=direct_tier,
                client_kind="http_bearer",
                principal_masked=mask_principal(token, prefix="tok"),
                authenticated=True,
                dingtalk_verified=False,
            )

        # Channel B: DingTalk Dual-Factor (ONLY after verifying gateway Bearer token)
        if _match_gateway_token(token, acl.dingtalk_gateway.tokens):
            dt_uid = lower_headers.get("x-dingtalk-user-id", "")
            dt_job = lower_headers.get("x-dingtalk-user-job-number", "")

            uid_tier = _lookup_constant_time(dt_uid, acl.dingtalk_gateway.user_id_tiers)
            job_tier = _lookup_constant_time(dt_job, acl.dingtalk_gateway.job_number_tiers)

            candidates = [t for t in (uid_tier, job_tier) if t is not None]
            if candidates:
                resolved_tier = max(candidates, key=lambda t: TIER_RANK[t])
            else:
                resolved_tier = acl.dingtalk_gateway.default_authenticated_tier

            if dt_uid:
                masked = mask_principal(dt_uid, prefix="dt_uid")
            elif dt_job:
                masked = mask_principal(dt_job, prefix="dt_job")
            else:
                masked = mask_principal(token, prefix="dt_gw")

            return AuthContext(
                effective_tier=resolved_tier,
                client_kind="dingtalk",
                principal_masked=masked,
                authenticated=True,
                dingtalk_verified=True,
            )

        # Invalid Bearer token: fail closed regardless of anonymous_tier or X-DingTalk-*
        raise MCPAuthError(401, "INVALID_TOKEN", "Invalid bearer token")

    # No Authorization header: unconditionally ignore X-DingTalk-* headers
    if acl.anonymous_tier == "public":
        return AuthContext(
            effective_tier="public",
            client_kind="anonymous",
            principal_masked="anon:public",
            authenticated=False,
            dingtalk_verified=False,
        )

    raise MCPAuthError(401, "AUTHENTICATION_REQUIRED", "Authentication required")


def authenticate_stdio_env(
    acl: Optional[MCPACLConfig],
    environ: Optional[Mapping[str, str]] = None,
) -> AuthContext:
    """Authenticate local stdio MCP session via ``CBA_KB_MCP_TOKEN`` or ``CBA_KB_MCP_ROLE``."""
    env = environ if environ is not None else os.environ
    token = (env.get("CBA_KB_MCP_TOKEN") or "").strip()
    if token:
        if acl is None:
            raise MCPAuthError(401, "INVALID_TOKEN", "Token provided but no ACL configured")
        matched_tier = _match_bearer_token(token, acl.bearer_tokens)
        if matched_tier is None:
            raise MCPAuthError(401, "INVALID_TOKEN", "Invalid CBA_KB_MCP_TOKEN")
        return AuthContext(
            effective_tier=matched_tier,
            client_kind="stdio",
            principal_masked=mask_principal(token, prefix="stdio_tok"),
            authenticated=True,
            dingtalk_verified=False,
        )

    role = (env.get("CBA_KB_MCP_ROLE") or "").strip()
    if role:
        if role not in TIER_RANK:
            raise MCPAuthError(401, "INVALID_ROLE", f"Invalid CBA_KB_MCP_ROLE: {role!r}")
        return AuthContext(
            effective_tier=role,
            client_kind="stdio",
            principal_masked=f"stdio:{role}",
            authenticated=True,
            dingtalk_verified=False,
        )

    if acl is not None and acl.anonymous_tier == "deny":
        raise MCPAuthError(401, "AUTHENTICATION_REQUIRED", "Stdio credentials required by ACL")

    return AuthContext(
        effective_tier="public",
        client_kind="stdio",
        principal_masked="stdio:public",
        authenticated=False,
        dingtalk_verified=False,
    )


class MCPAuditLogger:
    """Thread-safe JSONL audit logger that records redacted metadata only."""

    def __init__(
        self,
        audit_log_path: Optional[Path | str] = None,
        stream: Optional[IO[str]] = None,
    ) -> None:
        self.audit_log_path = Path(audit_log_path) if audit_log_path is not None else None
        self.stream = stream
        self._lock = threading.Lock()

    def log_tool_call(
        self,
        *,
        auth_context: AuthContext,
        tool_name: str,
        release_id: str,
        latency_ms: float,
        is_error: bool = False,
    ) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "client_kind": auth_context.client_kind,
            "principal_masked": auth_context.principal_masked,
            "effective_tier": auth_context.effective_tier,
            "tool_name": str(tool_name),
            "release_id": str(release_id),
            "latency_ms": round(max(0.0, float(latency_ms)), 3),
            "is_error": bool(is_error),
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            if self.audit_log_path is not None:
                self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.audit_log_path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
            if self.stream is not None:
                self.stream.write(line)
                self.stream.flush()
        return entry
