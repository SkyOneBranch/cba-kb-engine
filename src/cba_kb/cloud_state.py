"""Private Cloudflare R2 / S3-compatible cloud state store for CBA-KB v2.3.0.

Implements AWS Signature Version 4 (SigV4) signing using Python stdlib only,
fail-closed SHA-256 integrity verification on every read/write, and clean-room
sanitization before uploading local instance snapshots.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Union
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit
import xml.etree.ElementTree as ET

from .common import atomic

_SAFE_ASCII_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_ACTIVE_LOCK_STATUSES = frozenset({"RUNNING", "QUEUED", "PENDING", "ACQUIRING_IN_CLOUD"})

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
        endpoint = (env.get("CBA_KB_R2_ENDPOINT_URL") or env.get("R2_ENDPOINT_URL") or "").strip()
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


def _safe_key_segment(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("R2_KEY_SEGMENT_EMPTY")
    if _SAFE_ASCII_RE.fullmatch(text):
        return text.lower()
    digest_hex = hashlib.sha256(text.lower().encode("utf-8")).hexdigest()[:24]
    return f"k_{digest_hex}"


def _safe_season_segment(season: str) -> str:
    text = str(season or "").strip()
    if not text or "/" in text or "\\" in text or ".." in text:
        raise ValueError("R2_SEASON_INVALID")
    return _safe_key_segment(text)


def _normalize_prefix(prefix: str) -> str:
    cleaned = str(prefix or "").strip().lstrip("/")
    if cleaned and not cleaned.endswith("/"):
        cleaned += "/"
    return cleaned


class R2CloudStateStore:
    """S3/R2-backed private cloud state store with SigV4 auth and SHA-256 verification."""

    def __init__(
        self,
        config: R2Config,
        *,
        session=None,
        transport: Optional[Callable[..., Tuple[int, Dict[str, str], bytes]]] = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.config = config
        self._http_client = session
        self._transport = transport
        self._timeout_seconds = float(timeout_seconds)
        self._base_prefix = _normalize_prefix(config.prefix)

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

    def get_bytes(
        self,
        key: str,
        *,
        expected_sha256: Optional[str] = None,
    ) -> Optional[bytes]:
        full_key = self._full_key(key)
        url = self._object_url(full_key)
        status, resp_headers, body = self._request("GET", url)
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError(f"R2_GET_FAILED:{status}:{key}")

        actual_sha = hashlib.sha256(body).hexdigest()
        lower_resp = {k.lower(): v for k, v in resp_headers.items()}
        meta_sha = lower_resp.get("x-amz-meta-sha256")
        if meta_sha is not None and meta_sha.strip().lower() != actual_sha:
            raise ValueError(f"R2_OBJECT_SHA256_MISMATCH:{key}")
        if expected_sha256 is not None and expected_sha256.strip().lower() != actual_sha:
            raise ValueError(f"R2_OBJECT_SHA256_MISMATCH:{key}")
        return body

    def delete_key(self, key: str) -> bool:
        full_key = self._full_key(key)
        url = self._object_url(full_key)
        status, _, _ = self._request("DELETE", url)
        if status in (200, 204):
            return True
        if status == 404:
            return False
        raise RuntimeError(f"R2_DELETE_FAILED:{status}:{key}")

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
        return f"cards/{_safe_season_segment(season)}/{_safe_key_segment(person_key)}.json"

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
                raise ValueError("R2_OBJECT_SHA256_MISMATCH:card_sha256")
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
            raise ValueError("R2_OBJECT_SHA256_MISMATCH:card_sha256")
        actual_sha = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        if declared_sha.strip().lower() != actual_sha:
            raise ValueError("R2_OBJECT_SHA256_MISMATCH:card_sha256")
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
        return f"locks/{_safe_season_segment(season)}/{_safe_key_segment(person_key)}.json"

    def find_active_job_for_person(
        self,
        person_key: str,
        season: str,
    ) -> Optional[Dict[str, Any]]:
        lock_doc = self.get_json(self._lock_key(person_key, season))
        if not isinstance(lock_doc, dict):
            return None
        status = str(lock_doc.get("status") or "").strip().upper()
        if status in _ACTIVE_LOCK_STATUSES:
            return lock_doc
        return None

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
