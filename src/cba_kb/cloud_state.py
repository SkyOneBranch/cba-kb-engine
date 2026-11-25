"""Private Cloudflare R2 / S3-compatible cloud state store for CBA-KB v2.3.0.

Implements AWS Signature Version 4 (SigV4) signing using Python stdlib only,
fail-closed SHA-256 integrity verification on every read/write, and clean-room
sanitization before uploading local instance snapshots.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import hmac
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import threading
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit
import uuid
import xml.etree.ElementTree as ET

from .common import atomic

_SAFE_ASCII_RE = re.compile(r"^[a-z0-9._-]+$")
_UNICODE_TRIM_RE = re.compile(r"^[\s\u001c-\u001f\u0085\ufeff]+|[\s\u001c-\u001f\u0085\ufeff]+$")
_SAFE_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_ACTIVE_LOCK_STATUSES = frozenset({"RUNNING", "QUEUED", "PENDING", "ACQUIRING_IN_CLOUD"})
DEFAULT_LOCK_TTL_SECONDS = 900.0
DEFAULT_INSTANCE_BUNDLE_KEY = "cloud_instance/cloud_instance_bundle.tar.gz"
DEFAULT_MAX_BUNDLE_COMPRESSED_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_BUNDLE_EXTRACTED_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_BUNDLE_MEMBERS = 5000

_BUNDLE_EXCLUDED_DIR_NAMES = frozenset({
    "evidence",
    "inbox",
    ".credentials",
    ".git",
    "__pycache__",
})

_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: Dict[Tuple[str, str, str, str], threading.Lock] = {}


class CloudStateIntegrityError(ValueError):
    """Raised fail-closed when an R2/S3 object fails SHA-256 verification."""


class R2LockContentionError(RuntimeError):
    """Raised fail-closed when 501 fallback lock verification detects contention or drift."""


__all__ = [
    "CloudStateIntegrityError",
    "DEFAULT_INSTANCE_BUNDLE_KEY",
    "DEFAULT_LOCK_TTL_SECONDS",
    "DEFAULT_MAX_BUNDLE_COMPRESSED_BYTES",
    "DEFAULT_MAX_BUNDLE_EXTRACTED_BYTES",
    "DEFAULT_MAX_BUNDLE_MEMBERS",
    "R2Config",
    "R2CloudStateStore",
    "R2LockContentionError",
    "sign_aws_sigv4_headers",
    "safe_key",
    "_safe_key",
]

R2Transport = Callable[
    [str, str],
    Tuple[int, Dict[str, str], bytes],
]


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _normalize_header_value(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).strip())


def sign_aws_sigv4_headers(
    *,
    method: str,
    url: str,
    access_key_id: str,
    secret_access_key: str,
    region: str = "auto",
    service: str = "s3",
    payload_bytes: bytes = b"",
    headers: Optional[Mapping[str, str]] = None,
    now_utc: Optional[datetime] = None,
) -> Dict[str, str]:
    """Compute AWS Signature Version 4 headers for S3 / Cloudflare R2 requests."""
    if not access_key_id or not secret_access_key:
        raise ValueError("R2_SIGNING_CREDENTIALS_REQUIRED")
    now = now_utc if now_utc is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    parsed = urlsplit(url)
    host = parsed.netloc
    if not host:
        raise ValueError("R2_URL_HOST_REQUIRED")

    raw_path = parsed.path or "/"
    canonical_uri = quote(unquote(raw_path), safe="/-_.~")

    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    encoded_pairs = sorted(
        (quote(k, safe="-_.~"), quote(v, safe="-_.~"))
        for k, v in query_pairs
    )
    canonical_querystring = "&".join(f"{k}={v}" for k, v in encoded_pairs)

    payload_hash = hashlib.sha256(payload_bytes).hexdigest()

    out_headers: Dict[str, str] = {}
    if headers:
        for k, v in headers.items():
            out_headers[str(k)] = str(v)

    lower_map: Dict[str, str] = {k.lower(): _normalize_header_value(v) for k, v in out_headers.items()}
    lower_map["host"] = host
    lower_map["x-amz-date"] = amz_date
    lower_map["x-amz-content-sha256"] = payload_hash

    out_headers["Host"] = host
    out_headers["x-amz-date"] = amz_date
    out_headers["x-amz-content-sha256"] = payload_hash

    sorted_canonical = sorted(lower_map.items())
    canonical_headers = "".join(f"{k}:{v}\n" for k, v in sorted_canonical)
    signed_headers = ";".join(k for k, _ in sorted_canonical)

    canonical_request = "\n".join([
        method.upper(),
        canonical_uri,
        canonical_querystring,
        canonical_headers,
        signed_headers,
        payload_hash,
    ])

    algorithm = "AWS4-HMAC-SHA256"
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        algorithm,
        amz_date,
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    k_date = _hmac_sha256(("AWS4" + secret_access_key).encode("utf-8"), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    k_signing = _hmac_sha256(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    out_headers["Authorization"] = (
        f"{algorithm} Credential={access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return out_headers


@dataclass(frozen=True)
class R2Config:
    endpoint_url: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    region: str = "auto"
    prefix: str = ""

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "R2Config":
        env = os.environ if environ is None else environ
        endpoint = (
            env.get("CBA_KB_R2_ENDPOINT_URL")
            or env.get("CBA_KB_R2_ENDPOINT")
            or env.get("R2_ENDPOINT_URL")
            or env.get("R2_ENDPOINT")
            or ""
        ).strip()
        if not endpoint:
            account_id = (env.get("CBA_KB_R2_ACCOUNT_ID") or env.get("R2_ACCOUNT_ID") or "").strip()
            if account_id:
                endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
        bucket = (env.get("CBA_KB_R2_BUCKET") or env.get("R2_BUCKET") or "").strip()
        access_key = (env.get("CBA_KB_R2_ACCESS_KEY_ID") or env.get("R2_ACCESS_KEY_ID") or "").strip()
        secret_key = (env.get("CBA_KB_R2_SECRET_ACCESS_KEY") or env.get("R2_SECRET_ACCESS_KEY") or "").strip()
        region = (env.get("CBA_KB_R2_REGION") or env.get("R2_REGION") or "auto").strip() or "auto"
        prefix = (env.get("CBA_KB_R2_PREFIX") or env.get("R2_PREFIX") or "").strip()

        missing = [
            name
            for name, val in (
                ("endpoint_url", endpoint),
                ("bucket", bucket),
                ("access_key_id", access_key),
                ("secret_access_key", secret_key),
            )
            if not val
        ]
        if missing:
            raise ValueError(f"R2_CONFIG_MISSING:{','.join(missing)}")
        return cls(
            endpoint_url=endpoint,
            bucket=bucket,
            access_key_id=access_key,
            secret_access_key=secret_key,
            region=region,
            prefix=prefix,
        )


def _safe_key(raw: str) -> str:
    """Deterministic R2 key segment shared with the Cloudflare Worker gateway.

    Contract (exact parity with JS ``replace(/^[\\s\\u001c-\\u001f\\u0085\\ufeff]+|[\\s\\u001c-\\u001f\\u0085\\ufeff]+$/g, "").toLowerCase()``):
    - Strip leading/trailing Unicode whitespace (including ``\\u001c-\\u001f``, ``\\u0085``, and ``\\ufeff``) and lowercase.
    - If matching ``^[a-z0-9._-]+$``, return as-is.
    - Otherwise return ``k_<sha256(utf8)[:24]>``.
    """
    text = _UNICODE_TRIM_RE.sub("", str(raw or "")).lower()
    if not text:
        raise ValueError("R2_KEY_SEGMENT_EMPTY")
    if _SAFE_ASCII_RE.fullmatch(text):
        return text
    digest_hex = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    return f"k_{digest_hex}"


safe_key = _safe_key
_safe_key_segment = _safe_key


def _parse_iso_utc(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_lock_expired(
    lock_doc: Mapping[str, Any],
    now: datetime,
    default_ttl_seconds: float,
) -> bool:
    expires_dt = _parse_iso_utc(lock_doc.get("expires_at_utc"))
    if expires_dt is not None:
        return now >= expires_dt
    created_dt = _parse_iso_utc(lock_doc.get("created_at_utc"))
    if created_dt is not None:
        return now >= (created_dt + timedelta(seconds=float(default_ttl_seconds)))
    return False


def _safe_season_segment(season: str) -> str:
    text = str(season or "").strip()
    if not text or "/" in text or "\\" in text or ".." in text:
        raise ValueError("R2_SEASON_INVALID")
    return _safe_key(text)


def _normalize_prefix(prefix: str) -> str:
    cleaned = str(prefix or "").strip().lstrip("/")
    if cleaned and not cleaned.endswith("/"):
        cleaned += "/"
    return cleaned


def _parse_bool_env(raw: Optional[str]) -> bool:
    return str(raw or "").strip().lower() in ("1", "true", "yes")


def _bundle_allowlisted_exact_names() -> frozenset[str]:
    from .consumer_surface import (
        CONSUMER_MASTER_FILENAME,
        DOMESTIC_REG_DIFF_CSV_FILENAME,
        FOREIGN_REGISTRATION_CSV_FILENAME,
    )

    return frozenset({
        CONSUMER_MASTER_FILENAME,
        "MASTER.csv",
        FOREIGN_REGISTRATION_CSV_FILENAME,
        DOMESTIC_REG_DIFF_CSV_FILENAME,
        "identity_registry.json",
        "operational_checkpoint.json",
        "coverage_ledger.json",
        "negative_receipts.jsonl",
        "snowball_receipts.jsonl",
        "on_demand_cards_manifest.json",
        "curation_profile.yaml",
    })


def _atomic_write_nofollow(base: Path, rel_parts: Sequence[str], content: bytes) -> None:
    """Create parent dirs and atomically write a file using dir_fd + O_NOFOLLOW."""
    rel_display = "/".join(rel_parts)
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)

    try:
        cur_fd = os.open(str(base), dir_flags)
    except OSError as exc:
        raise ValueError(f"R2_BUNDLE_SYMLINK_TARGET:{rel_display}") from exc

    try:
        for seg in rel_parts[:-1]:
            try:
                os.mkdir(seg, 0o755, dir_fd=cur_fd)
            except FileExistsError:
                pass
            try:
                next_fd = os.open(seg, dir_flags, dir_fd=cur_fd)
            except OSError as exc:
                raise ValueError(f"R2_BUNDLE_SYMLINK_TARGET:{rel_display}") from exc
            os.close(cur_fd)
            cur_fd = next_fd

        fname = rel_parts[-1]
        try:
            st = os.lstat(fname, dir_fd=cur_fd)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                raise ValueError(f"R2_BUNDLE_SYMLINK_TARGET:{rel_display}")
        except FileNotFoundError:
            pass

        tmp_name = f".{fname}.{uuid.uuid4().hex[:12]}.tmp"
        file_fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow_flag,
            0o644,
            dir_fd=cur_fd,
        )
        try:
            view = memoryview(content)
            written = 0
            while written < len(view):
                written += os.write(file_fd, view[written:])
            os.fsync(file_fd)
        except Exception:
            os.close(file_fd)
            try:
                os.unlink(tmp_name, dir_fd=cur_fd)
            except OSError:
                pass
            raise
        os.close(file_fd)

        try:
            os.replace(tmp_name, fname, src_dir_fd=cur_fd, dst_dir_fd=cur_fd)
        except Exception:
            try:
                os.unlink(tmp_name, dir_fd=cur_fd)
            except OSError:
                pass
            raise
    finally:
        os.close(cur_fd)


def _is_bundle_member_allowed(rel_posix: str, allowlisted_exact_names: frozenset[str]) -> bool:
    parts = [p for p in rel_posix.split("/") if p]
    if not parts:
        return False
    name = parts[-1]

    # Explicit exclusions (fail-closed)
    if name == "production.json":
        return False
    if any(seg in _BUNDLE_EXCLUDED_DIR_NAMES for seg in parts[:-1]):
        return False
    if "document_lane" in parts and "archive" in parts and "raw" in parts:
        return False

    # Substrate allowlist
    if name in allowlisted_exact_names:
        return True
    if name.startswith("season_stats"):
        return True
    if len(parts) >= 2 and parts[-2] == "game_logs" and (
        name.endswith(".json") or name.endswith(".jsonl")
    ):
        return True
    if len(parts) >= 2 and parts[-2] in ("cards", "on_demand_cards") and name.endswith(".md"):
        return True
    return False


class R2CloudStateStore:
    """S3/R2-backed private cloud state store with SigV4 auth and SHA-256 verification."""

    def __init__(
        self,
        config: R2Config,
        *,
        session=None,
        transport: Optional[Callable[..., Tuple[int, Dict[str, str], bytes]]] = None,
        timeout_seconds: float = 30.0,
        allow_s3_501_lock_fallback: Optional[bool] = None,
    ) -> None:
        self.config = config
        self._http_client = session
        self._transport = transport
        self._timeout_seconds = float(timeout_seconds)
        self._base_prefix = _normalize_prefix(config.prefix)
        if allow_s3_501_lock_fallback is None:
            self.allow_s3_501_lock_fallback = _parse_bool_env(
                os.environ.get("CBA_KB_S3_LOCK_501_FALLBACK")
            )
        else:
            self.allow_s3_501_lock_fallback = bool(allow_s3_501_lock_fallback)

    def _full_key(self, key: str) -> str:
        rel = str(key or "").lstrip("/")
        if not rel or ".." in Path(rel).parts:
            raise ValueError(f"R2_INVALID_KEY:{key}")
        return f"{self._base_prefix}{rel}"

    def _object_url(self, full_key: str) -> str:
        endpoint = self.config.endpoint_url.rstrip("/")
        bucket = self.config.bucket.strip("/")
        encoded_key = quote(full_key, safe="/-_.~")
        return f"{endpoint}/{bucket}/{encoded_key}"

    def _bucket_url(self, query_params: Mapping[str, str]) -> str:
        endpoint = self.config.endpoint_url.rstrip("/")
        bucket = self.config.bucket.strip("/")
        qs = urlencode(sorted(query_params.items()))
        return f"{endpoint}/{bucket}?{qs}" if qs else f"{endpoint}/{bucket}"

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        data: bytes = b"",
    ) -> Tuple[int, Dict[str, str], bytes]:
        signed = sign_aws_sigv4_headers(
            method=method,
            url=url,
            access_key_id=self.config.access_key_id,
            secret_access_key=self.config.secret_access_key,
            region=self.config.region,
            service="s3",
            payload_bytes=data,
            headers=headers,
        )
        if self._transport is not None:
            return self._transport(method, url, headers=signed, data=data)

        if self._http_client is None:
            import requests
            self._http_client = requests.Session()
        resp = self._http_client.request(
            method,
            url,
            headers=signed,
            data=data,
            timeout=self._timeout_seconds,
        )
        return resp.status_code, dict(resp.headers), resp.content

    def put_bytes(
        self,
        key: str,
        data: Union[bytes, bytearray],
        *,
        content_type: str = "application/octet-stream",
    ) -> Dict[str, Any]:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("R2_PAYLOAD_MUST_BE_BYTES")
        payload = bytes(data)
        full_key = self._full_key(key)
        sha256_hex = hashlib.sha256(payload).hexdigest()
        url = self._object_url(full_key)
        status, resp_headers, _ = self._request(
            "PUT",
            url,
            headers={
                "Content-Type": content_type,
                "x-amz-meta-sha256": sha256_hex,
            },
            data=payload,
        )
        if status not in (200, 201, 204):
            raise RuntimeError(f"R2_PUT_FAILED:{status}:{key}")
        lower_resp = {k.lower(): v for k, v in resp_headers.items()}
        return {
            "key": key,
            "sha256": sha256_hex,
            "size_bytes": len(payload),
            "etag": lower_resp.get("etag"),
        }

    def _get_bytes_with_etag(
        self,
        key: str,
        *,
        expected_sha256: Optional[str] = None,
    ) -> Tuple[Optional[bytes], Optional[str]]:
        full_key = self._full_key(key)
        url = self._object_url(full_key)
        status, resp_headers, body = self._request("GET", url)
        if status == 404:
            return None, None
        if status != 200:
            raise RuntimeError(f"R2_GET_FAILED:{status}:{key}")

        actual_sha = hashlib.sha256(body).hexdigest()
        lower_resp = {k.lower(): v for k, v in resp_headers.items()}
        meta_sha = lower_resp.get("x-amz-meta-sha256")
        if not isinstance(meta_sha, str) or not _HEX64_RE.fullmatch(meta_sha.strip().lower()):
            raise CloudStateIntegrityError(
                f"R2_OBJECT_SHA256_MISMATCH:{key}:missing_or_invalid_metadata_sha256"
            )
        if meta_sha.strip().lower() != actual_sha:
            raise CloudStateIntegrityError(f"R2_OBJECT_SHA256_MISMATCH:{key}")
        if expected_sha256 is not None:
            if not isinstance(expected_sha256, str) or not _HEX64_RE.fullmatch(expected_sha256.strip().lower()):
                raise CloudStateIntegrityError(
                    f"R2_OBJECT_SHA256_MISMATCH:{key}:invalid_expected_sha256"
                )
            if expected_sha256.strip().lower() != actual_sha:
                raise CloudStateIntegrityError(f"R2_OBJECT_SHA256_MISMATCH:{key}")
        raw_etag = lower_resp.get("etag")
        etag = raw_etag.strip() if isinstance(raw_etag, str) and raw_etag.strip() else None
        return body, etag

    def _get_json_with_etag(self, key: str) -> Tuple[Optional[Any], Optional[str]]:
        raw, etag = self._get_bytes_with_etag(key)
        if raw is None:
            return None, None
        return json.loads(raw.decode("utf-8")), etag

    def get_bytes(
        self,
        key: str,
        *,
        expected_sha256: Optional[str] = None,
    ) -> Optional[bytes]:
        body, _ = self._get_bytes_with_etag(key, expected_sha256=expected_sha256)
        return body

    def _delete_key_conditional(
        self,
        key: str,
        *,
        if_match: Optional[str] = None,
    ) -> bool:
        full_key = self._full_key(key)
        url = self._object_url(full_key)
        headers = {"If-Match": if_match} if if_match else None
        status, _, _ = self._request("DELETE", url, headers=headers)
        if status in (200, 204):
            return True
        if status in (404, 412):
            return False
        raise RuntimeError(f"R2_DELETE_FAILED:{status}:{key}")

    def delete_key(self, key: str) -> bool:
        return self._delete_key_conditional(key)

    def list_keys(self, prefix: str = "") -> List[str]:
        rel_prefix = str(prefix or "").lstrip("/")
        full_prefix = f"{self._base_prefix}{rel_prefix}"
        keys: List[str] = []
        continuation_token: Optional[str] = None

        while True:
            params: Dict[str, str] = {"list-type": "2", "prefix": full_prefix}
            if continuation_token:
                params["continuation-token"] = continuation_token
            url = self._bucket_url(params)
            status, _, body = self._request("GET", url)
            if status != 200:
                raise RuntimeError(f"R2_LIST_FAILED:{status}:{prefix}")
            root = ET.fromstring(body)
            is_truncated = False
            next_token: Optional[str] = None
            for elem in root.iter():
                tag = elem.tag.rsplit("}", 1)[-1]
                if tag == "Key" and elem.text is not None:
                    k = elem.text
                    if self._base_prefix and k.startswith(self._base_prefix):
                        k = k[len(self._base_prefix):]
                    keys.append(k)
                elif tag == "IsTruncated" and elem.text is not None:
                    is_truncated = elem.text.strip().lower() == "true"
                elif tag == "NextContinuationToken" and elem.text:
                    next_token = elem.text.strip()
            if not is_truncated or not next_token:
                break
            continuation_token = next_token

        return sorted(keys)

    def put_json(self, key: str, obj: Any) -> Dict[str, Any]:
        raw = json.dumps(
            obj,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return self.put_bytes(key, raw, content_type="application/json; charset=utf-8")

    def get_json(
        self,
        key: str,
        *,
        expected_sha256: Optional[str] = None,
    ) -> Optional[Any]:
        raw = self.get_bytes(key, expected_sha256=expected_sha256)
        if raw is None:
            return None
        return json.loads(raw.decode("utf-8"))

    def _card_key(self, season: str, person_key: str) -> str:
        return f"cards/{_safe_season_segment(season)}/{_safe_key(person_key)}.json"

    def put_card(
        self,
        season: str,
        person_key: str,
        card_record: Mapping[str, Any],
    ) -> Dict[str, Any]:
        record = dict(card_record)
        markdown = record.get("markdown")
        if not isinstance(markdown, str) or not markdown:
            raise ValueError("R2_CARD_MARKDOWN_REQUIRED")
        actual_sha = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        declared_sha = record.get("card_sha256")
        if declared_sha is not None and str(declared_sha).strip():
            if str(declared_sha).strip().lower() != actual_sha:
                raise CloudStateIntegrityError("R2_OBJECT_SHA256_MISMATCH:card_sha256")
        record["card_sha256"] = actual_sha
        return self.put_json(self._card_key(season, person_key), record)

    def get_card(self, season: str, person_key: str) -> Optional[Dict[str, Any]]:
        record = self.get_json(self._card_key(season, person_key))
        if record is None:
            return None
        if not isinstance(record, dict):
            raise ValueError("R2_CARD_RECORD_INVALID")
        markdown = record.get("markdown")
        declared_sha = record.get("card_sha256")
        if not isinstance(markdown, str) or not isinstance(declared_sha, str):
            raise CloudStateIntegrityError("R2_OBJECT_SHA256_MISMATCH:card_sha256")
        actual_sha = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        if declared_sha.strip().lower() != actual_sha:
            raise CloudStateIntegrityError("R2_OBJECT_SHA256_MISMATCH:card_sha256")
        return record

    def put_coverage_ledger(self, ledger: Mapping[str, Any]) -> Dict[str, Any]:
        return self.put_json("coverage_ledger.json", dict(ledger))

    def get_coverage_ledger(self) -> Optional[Dict[str, Any]]:
        data = self.get_json("coverage_ledger.json")
        if data is None:
            return None
        if not isinstance(data, dict):
            raise ValueError("R2_COVERAGE_LEDGER_INVALID")
        return data

    def append_negative_receipt(self, receipt: Mapping[str, Any]) -> Dict[str, Any]:
        existing = self.get_bytes("negative_receipts.jsonl") or b""
        line = (
            json.dumps(
                dict(receipt),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if existing and not existing.endswith(b"\n"):
            existing += b"\n"
        return self.put_bytes(
            "negative_receipts.jsonl",
            existing + line,
            content_type="application/x-ndjson; charset=utf-8",
        )

    def get_negative_receipts(self) -> List[Dict[str, Any]]:
        raw = self.get_bytes("negative_receipts.jsonl")
        if raw is None:
            return []
        receipts: List[Dict[str, Any]] = []
        for line in raw.decode("utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            item = json.loads(stripped)
            if isinstance(item, dict):
                receipts.append(item)
        return receipts

    def _job_key(self, job_id: str) -> str:
        jid = str(job_id or "").strip()
        if not jid or not _SAFE_JOB_ID_RE.fullmatch(jid):
            raise ValueError(f"R2_JOB_ID_INVALID:{job_id}")
        return f"jobs/{jid}.json"

    def put_job(self, job_id: str, job_doc: Mapping[str, Any]) -> Dict[str, Any]:
        return self.put_json(self._job_key(job_id), dict(job_doc))

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        data = self.get_json(self._job_key(job_id))
        if data is None:
            return None
        if not isinstance(data, dict):
            raise ValueError("R2_JOB_DOC_INVALID")
        return data

    def _lock_key(self, person_key: str, season: str) -> str:
        return f"locks/{_safe_season_segment(season)}/{_safe_key(person_key)}.json"

    def _local_lock_for(self, lock_key: str) -> threading.Lock:
        scope = (
            self.config.endpoint_url.rstrip("/"),
            self.config.bucket.strip("/"),
            self._base_prefix,
            lock_key,
        )
        with _PROCESS_LOCKS_GUARD:
            lock_obj = _PROCESS_LOCKS.get(scope)
            if lock_obj is None:
                lock_obj = threading.Lock()
                _PROCESS_LOCKS[scope] = lock_obj
            return lock_obj

    def _put_lock_conditional(
        self,
        lock_key: str,
        lock_doc: Mapping[str, Any],
        *,
        if_none_match: Optional[str] = None,
        if_match: Optional[str] = None,
        now_utc: Optional[datetime] = None,
        lock_ttl_seconds: float = DEFAULT_LOCK_TTL_SECONDS,
    ) -> int:
        payload = json.dumps(
            dict(lock_doc),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        sha256_hex = hashlib.sha256(payload).hexdigest()
        full_key = self._full_key(lock_key)
        url = self._object_url(full_key)
        headers: Dict[str, str] = {
            "Content-Type": "application/json; charset=utf-8",
            "x-amz-meta-sha256": sha256_hex,
        }
        if if_none_match is not None:
            headers["If-None-Match"] = if_none_match
        if if_match is not None:
            headers["If-Match"] = if_match
        status, _, _ = self._request("PUT", url, headers=headers, data=payload)
        if (
            status == 501
            and self.allow_s3_501_lock_fallback
            and (if_none_match is not None or if_match is not None)
        ):
            # 1. Pre-check existing remote lock object before unconditional fallback PUT
            cur_existing, _ = self._get_json_with_etag(lock_key)
            if isinstance(cur_existing, dict):
                cur_status = str(cur_existing.get("status") or "").strip().upper()
                check_now = now_utc if now_utc is not None else datetime.now(timezone.utc)
                if check_now.tzinfo is None:
                    check_now = check_now.replace(tzinfo=timezone.utc)
                else:
                    check_now = check_now.astimezone(timezone.utc)
                target_status = str(lock_doc.get("status") or "").strip().upper()
                cur_owner = str(cur_existing.get("owner_id") or "").strip()
                my_owner = str(lock_doc.get("owner_id") or "").strip()
                if target_status == "RELEASED":
                    if cur_owner and cur_owner != my_owner:
                        raise R2LockContentionError(
                            f"R2_LOCK_CONTENTION_501_FALLBACK:{lock_key}:owner_changed"
                        )
                else:
                    if (
                        cur_status in _ACTIVE_LOCK_STATUSES
                        and not _is_lock_expired(cur_existing, check_now, lock_ttl_seconds)
                        and cur_owner != my_owner
                    ):
                        raise R2LockContentionError(
                            f"R2_LOCK_CONTENTION_501_FALLBACK:{lock_key}:active_other_owner"
                        )

            # 2. Unconditional fallback PUT with x-amz-meta-sha256 + fencing_token
            unconditional_headers = {
                "Content-Type": "application/json; charset=utf-8",
                "x-amz-meta-sha256": sha256_hex,
            }
            status, _, _ = self._request(
                "PUT", url, headers=unconditional_headers, data=payload
            )
            if status not in (200, 201, 204):
                return status

            # 3. Mandatory post-PUT read-back verification (SHA-256 + owner_id + fencing_token)
            try:
                rb_raw, _ = self._get_bytes_with_etag(lock_key, expected_sha256=sha256_hex)
            except CloudStateIntegrityError as exc:
                raise R2LockContentionError(
                    f"R2_LOCK_READBACK_MISMATCH:{lock_key}"
                ) from exc
            if rb_raw is None or hashlib.sha256(rb_raw).hexdigest() != sha256_hex:
                raise R2LockContentionError(f"R2_LOCK_READBACK_MISMATCH:{lock_key}")
            try:
                rb_doc = json.loads(rb_raw.decode("utf-8"))
            except Exception as exc:
                raise R2LockContentionError(
                    f"R2_LOCK_READBACK_INVALID_JSON:{lock_key}"
                ) from exc
            if (
                not isinstance(rb_doc, dict)
                or rb_doc.get("owner_id") != lock_doc.get("owner_id")
                or rb_doc.get("fencing_token") != lock_doc.get("fencing_token")
                or rb_doc.get("status") != lock_doc.get("status")
            ):
                raise R2LockContentionError(
                    f"R2_LOCK_READBACK_CONTENTION:{lock_key}"
                )
        return status

    def acquire_active_job_lock(
        self,
        season: str,
        person_key: str,
        *,
        job_id: str,
        owner_id: str,
        extra: Optional[Mapping[str, Any]] = None,
        lock_ttl_seconds: float = DEFAULT_LOCK_TTL_SECONDS,
        now_utc: Optional[datetime] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """Atomically acquire an active job lock with lease TTL and conditional-PUT takeover."""
        ttl = float(lock_ttl_seconds)
        if ttl <= 0:
            raise ValueError("R2_LOCK_TTL_MUST_BE_POSITIVE")
        now = now_utc if now_utc is not None else datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)

        lock_doc: Dict[str, Any] = dict(extra) if isinstance(extra, Mapping) else {}
        lock_doc["job_id"] = str(job_id)
        lock_doc["owner_id"] = str(owner_id)
        lock_doc["fencing_token"] = str(lock_doc.get("fencing_token") or uuid.uuid4().hex)
        lock_doc["person"] = str(person_key)
        lock_doc["season"] = str(season)
        lock_doc["status"] = str(lock_doc.get("status") or "RUNNING")
        created_str = lock_doc.get("created_at_utc") or now.strftime("%Y-%m-%dT%H:%M:%SZ")
        lock_doc["created_at_utc"] = created_str
        created_dt = _parse_iso_utc(created_str) or now
        lock_doc["expires_at_utc"] = (created_dt + timedelta(seconds=ttl)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        lock_key = self._lock_key(person_key, season)
        with self._local_lock_for(lock_key):
            existing, existing_etag = self._get_json_with_etag(lock_key)
            if isinstance(existing, dict):
                existing_status = str(existing.get("status") or "").strip().upper()
                if existing_status in _ACTIVE_LOCK_STATUSES and not _is_lock_expired(
                    existing, now, ttl
                ):
                    return False, existing
                # Released, terminal, or expired lock: take over atomically via PUT If-Match.
                # Fail closed if ETag is missing — NEVER unconditionally delete.
                if not existing_etag:
                    raise RuntimeError(f"R2_LOCK_ETAG_MISSING:{lock_key}")
                status = self._put_lock_conditional(
                    lock_key,
                    lock_doc,
                    if_match=existing_etag,
                    now_utc=now,
                    lock_ttl_seconds=ttl,
                )
                if status in (200, 201, 204):
                    return True, lock_doc
                if status == 412:
                    winner, _ = self._get_json_with_etag(lock_key)
                    return False, winner if isinstance(winner, dict) else {}
                raise RuntimeError(f"R2_LOCK_PUT_FAILED:{status}:{lock_key}")

            status = self._put_lock_conditional(
                lock_key,
                lock_doc,
                if_none_match="*",
                now_utc=now,
                lock_ttl_seconds=ttl,
            )
            if status in (200, 201, 204):
                return True, lock_doc
            if status == 412:
                winner, winner_etag = self._get_json_with_etag(lock_key)
                if isinstance(winner, dict):
                    winner_status = str(winner.get("status") or "").strip().upper()
                    if winner_status in _ACTIVE_LOCK_STATUSES and not _is_lock_expired(
                        winner, now, ttl
                    ):
                        return False, winner
                    # Winner lock is released, terminal, or expired -> atomic takeover via If-Match.
                    if not winner_etag:
                        raise RuntimeError(f"R2_LOCK_ETAG_MISSING:{lock_key}")
                    takeover_status = self._put_lock_conditional(
                        lock_key,
                        lock_doc,
                        if_match=winner_etag,
                        now_utc=now,
                        lock_ttl_seconds=ttl,
                    )
                    if takeover_status in (200, 201, 204):
                        return True, lock_doc
                    if takeover_status == 412:
                        latest, _ = self._get_json_with_etag(lock_key)
                        return False, latest if isinstance(latest, dict) else {}
                    raise RuntimeError(f"R2_LOCK_PUT_FAILED:{takeover_status}:{lock_key}")
                return False, {}
            raise RuntimeError(f"R2_LOCK_PUT_FAILED:{status}:{lock_key}")

    def release_active_job_lock(
        self,
        season: str,
        person_key: str,
        *,
        owner_id: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> bool:
        """Conditionally release an active job lock via R2-native PUT If-Match (status=RELEASED)."""
        if not isinstance(owner_id, str) or not owner_id.strip():
            return False
        lock_key = self._lock_key(person_key, season)
        with self._local_lock_for(lock_key):
            current, etag = self._get_json_with_etag(lock_key)
            if not isinstance(current, dict):
                return False
            current_owner = current.get("owner_id")
            if not isinstance(current_owner, str) or current_owner != owner_id:
                return False
            if job_id is not None:
                current_job = current.get("job_id")
                if not isinstance(current_job, str) or current_job != job_id:
                    return False
            # Fail-closed if ETag is missing: NEVER unconditionally delete or overwrite.
            if not etag:
                return False
            released_doc: Dict[str, Any] = dict(current)
            released_doc["status"] = "RELEASED"
            released_doc["job_id"] = current.get("job_id")
            released_doc["owner_id"] = current.get("owner_id")
            released_doc["released_at_utc"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            status = self._put_lock_conditional(
                lock_key, released_doc, if_match=etag
            )
            if status in (200, 201, 204):
                return True
            if status == 412:
                return False
            raise RuntimeError(f"R2_LOCK_RELEASE_FAILED:{status}:{lock_key}")

    def find_active_job_for_person(
        self,
        person_key: str,
        season: str,
        *,
        now_utc: Optional[datetime] = None,
        default_ttl_seconds: float = DEFAULT_LOCK_TTL_SECONDS,
    ) -> Optional[Dict[str, Any]]:
        lock_doc = self.get_json(self._lock_key(person_key, season))
        if not isinstance(lock_doc, dict):
            return None
        status = str(lock_doc.get("status") or "").strip().upper()
        if status not in _ACTIVE_LOCK_STATUSES:
            return None
        if now_utc is not None:
            now = now_utc.replace(tzinfo=timezone.utc) if now_utc.tzinfo is None else now_utc.astimezone(timezone.utc)
            if _is_lock_expired(lock_doc, now, default_ttl_seconds):
                return None
        elif lock_doc.get("expires_at_utc"):
            if _is_lock_expired(lock_doc, datetime.now(timezone.utc), default_ttl_seconds):
                return None
        return lock_doc

    def set_active_job_lock(
        self,
        person_key: str,
        season: str,
        job_doc: Mapping[str, Any],
    ) -> Dict[str, Any]:
        return self.put_json(self._lock_key(person_key, season), dict(job_doc))

    def clear_active_job_lock(self, person_key: str, season: str) -> bool:
        return self.delete_key(self._lock_key(person_key, season))

    def push_instance_snapshot(
        self,
        local_dir: Union[str, Path],
        *,
        prefix: str = "cloud_instance/",
    ) -> Dict[str, Any]:
        from .current_state import clean

        base = Path(local_dir).resolve()
        if not base.is_dir():
            raise ValueError(f"R2_SNAPSHOT_DIR_MISSING:{local_dir}")
        norm_prefix = _normalize_prefix(prefix)
        uploaded: List[str] = []
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            rel = path.relative_to(base).as_posix()
            data = path.read_bytes()
            clean(data, rel)
            key = f"{norm_prefix}{rel}"
            self.put_bytes(key, data)
            uploaded.append(key)
        return {
            "uploaded_count": len(uploaded),
            "keys": uploaded,
            "prefix": norm_prefix,
        }

    def pull_instance_snapshot(
        self,
        local_dir: Union[str, Path],
        *,
        prefix: str = "cloud_instance/",
    ) -> Dict[str, Any]:
        base = Path(local_dir).resolve()
        base.mkdir(parents=True, exist_ok=True)
        norm_prefix = _normalize_prefix(prefix)
        keys = self.list_keys(prefix=norm_prefix)
        downloaded: List[str] = []
        for key in keys:
            rel = key[len(norm_prefix):] if norm_prefix and key.startswith(norm_prefix) else key
            if not rel or rel.endswith("/"):
                continue
            target = (base / rel).resolve()
            if target == base or not target.is_relative_to(base):
                raise ValueError(f"R2_SNAPSHOT_PATH_ESCAPE:{key}")
            data = self.get_bytes(key)
            if data is None:
                continue
            atomic(target, data)
            downloaded.append(rel)
        return {
            "downloaded_count": len(downloaded),
            "files": downloaded,
            "prefix": norm_prefix,
        }

    def push_instance_bundle(
        self,
        local_dir: Union[str, Path],
        *,
        key: str = DEFAULT_INSTANCE_BUNDLE_KEY,
        max_compressed_bytes: int = DEFAULT_MAX_BUNDLE_COMPRESSED_BYTES,
    ) -> Dict[str, Any]:
        """Pack allowlisted substrate files into a deterministic .tar.gz and upload in 1 PUT."""
        from .current_state import clean

        base = Path(local_dir).resolve()
        if not base.is_dir():
            raise ValueError(f"R2_SNAPSHOT_DIR_MISSING:{local_dir}")
        max_comp = int(max_compressed_bytes)
        if max_comp <= 0:
            raise ValueError("R2_BUNDLE_MAX_COMPRESSED_BYTES_INVALID")

        allowlisted_exact_names = _bundle_allowlisted_exact_names()

        all_files = sorted(
            (p.relative_to(base).as_posix(), p)
            for p in base.rglob("*")
            if p.is_file() and not p.is_symlink()
        )
        included: List[Tuple[str, bytes]] = []
        excluded_count = 0
        for rel, path in all_files:
            if not _is_bundle_member_allowed(rel, allowlisted_exact_names):
                excluded_count += 1
                continue
            data = path.read_bytes()
            clean(data, rel)
            included.append((rel, data))

        buf = io.BytesIO()
        with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tf:
                for rel, data in included:
                    ti = tarfile.TarInfo(name=rel)
                    ti.size = len(data)
                    ti.mtime = 0
                    ti.uid = 0
                    ti.gid = 0
                    ti.uname = ""
                    ti.gname = ""
                    ti.mode = 0o644
                    ti.type = tarfile.REGTYPE
                    tf.addfile(ti, io.BytesIO(data))

        payload = buf.getvalue()
        if len(payload) > max_comp:
            raise ValueError("R2_BUNDLE_COMPRESSED_SIZE_EXCEEDED")

        put_res = self.put_bytes(key, payload, content_type="application/gzip")
        members = [rel for rel, _ in included]
        return {
            "key": key,
            "sha256": put_res["sha256"],
            "size_bytes": len(payload),
            "member_count": len(members),
            "members": members,
            "excluded_count": excluded_count,
        }

    def pull_instance_bundle(
        self,
        local_dir: Union[str, Path],
        *,
        key: str = DEFAULT_INSTANCE_BUNDLE_KEY,
        expected_sha256: Optional[str] = None,
        max_compressed_bytes: int = DEFAULT_MAX_BUNDLE_COMPRESSED_BYTES,
        max_extracted_bytes: int = DEFAULT_MAX_BUNDLE_EXTRACTED_BYTES,
        max_members: int = DEFAULT_MAX_BUNDLE_MEMBERS,
    ) -> Dict[str, Any]:
        """Download and verify a single .tar.gz bundle in 1 GET, failing closed before any write."""
        max_comp = int(max_compressed_bytes)
        max_ext = int(max_extracted_bytes)
        max_mem = int(max_members)

        raw = self.get_bytes(key, expected_sha256=expected_sha256)
        if raw is None:
            raise FileNotFoundError(f"R2_BUNDLE_NOT_FOUND:{key}")
        if len(raw) > max_comp:
            raise ValueError("R2_BUNDLE_COMPRESSED_SIZE_EXCEEDED")

        raw_local_dir = Path(local_dir)
        if raw_local_dir.is_symlink():
            raise ValueError(f"R2_BUNDLE_SYMLINK_TARGET:{local_dir}")
        base = raw_local_dir.resolve()
        allowlisted_exact_names = _bundle_allowlisted_exact_names()
        staged: List[Tuple[str, List[str], bytes]] = []
        total_extracted = 0
        member_count = 0

        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            for member in tf:
                member_count += 1
                if member_count > max_mem:
                    raise ValueError("R2_BUNDLE_MAX_MEMBERS_EXCEEDED")

                raw_name = member.name
                if (
                    not isinstance(raw_name, str)
                    or not raw_name
                    or "\x00" in raw_name
                    or "\\" in raw_name
                    or raw_name.startswith("/")
                    or _WIN_DRIVE_RE.match(raw_name)
                ):
                    raise ValueError(f"R2_BUNDLE_PATH_TRAVERSAL:{raw_name}")
                raw_parts = raw_name.split("/")
                if ".." in raw_parts:
                    raise ValueError(f"R2_BUNDLE_PATH_TRAVERSAL:{raw_name}")

                if member.isdir():
                    continue
                if not member.isreg():
                    raise ValueError(f"R2_BUNDLE_UNSAFE_MEMBER_TYPE:{raw_name}")

                clean_parts = [p for p in raw_parts if p and p != "."]
                if not clean_parts:
                    raise ValueError(f"R2_BUNDLE_PATH_TRAVERSAL:{raw_name}")
                rel_norm = PurePosixPath(*clean_parts).as_posix()
                if not _is_bundle_member_allowed(rel_norm, allowlisted_exact_names):
                    raise ValueError(f"R2_BUNDLE_DISALLOWED_MEMBER:{rel_norm}")

                target = (base / rel_norm).resolve()
                if target == base or not target.is_relative_to(base):
                    raise ValueError(f"R2_BUNDLE_PATH_TRAVERSAL:{raw_name}")

                # Pre-write lstat check: reject any pre-existing symlink along parent dirs or target
                walk_p = base
                for seg in clean_parts:
                    walk_p = walk_p / seg
                    if walk_p.is_symlink():
                        raise ValueError(f"R2_BUNDLE_SYMLINK_TARGET:{rel_norm}")

                if member.size < 0 or (total_extracted + member.size) > max_ext:
                    raise ValueError("R2_BUNDLE_EXTRACTED_SIZE_EXCEEDED")

                remaining = max_ext - total_extracted
                fobj = tf.extractfile(member)
                content = fobj.read(remaining + 1) if fobj is not None else b""
                if len(content) > remaining:
                    raise ValueError("R2_BUNDLE_EXTRACTED_SIZE_EXCEEDED")
                total_extracted += len(content)
                staged.append((rel_norm, clean_parts, content))

        base.mkdir(parents=True, exist_ok=True)
        for _, clean_parts, content in staged:
            _atomic_write_nofollow(base, clean_parts, content)

        return {
            "key": key,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "extracted_count": len(staged),
            "files": [rel for rel, _, _ in staged],
        }
