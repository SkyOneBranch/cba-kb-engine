"""On-demand Snowball Core Orchestrator (REQ-230 P0-3).

Coordinates pure-cloud serverless person card serving and on-demand acquisition:
- Fast path (<0.5s):
  1. Fail-closed homonym / multi-candidate disambiguation -> UNDECIDED (0 network)
  2. Fresh cached card in R2 -> HIT_FRESH with direct card_markdown (0 network)
  3. Active cba-kb.negative-receipt.v1 within TTL -> UNKNOWN_NEGATIVE_CACHED (0 network)
  4. Active in-flight concurrency lock -> ACQUIRING_IN_CLOUD reusing job_id (deduplicated=True)
- Slow path (cache miss or force_refresh=True):
  - Delegates to AcquisitionBackend (LocalCliAcquisitionBackend or AsyncDispatchAcquisitionBackend)
  - ACQUIRED_AND_BUILT: verifies SHA-256, persists to R2 cards/, optionally upserts to
    isolated Drive snowball folder, marks job COMPLETED, clears lock, returns full card_markdown
  - UNKNOWN_AFTER_WHITELIST_CHECK: persists cba-kb.negative-receipt.v1 to R2, marks job
    COMPLETED_UNKNOWN, clears lock
  - ACQUIRING_IN_CLOUD: keeps RUNNING job & lock in R2, returns job_id + next_action
"""
from __future__ import annotations

import abc
import hashlib
import inspect
import json
import os
import shlex
import subprocess
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import requests

from .instance import Instance
from .person_card_builder import (
    _find_active_negative_receipt,
    build_person_card,
    resolve_person_query,
)

NEGATIVE_RECEIPT_SCHEMA = "cba-kb.negative-receipt.v1"
DEFAULT_LOCK_TTL_SECONDS = 900.0
_ACTIVE_LOCK_STATUSES = frozenset({"RUNNING", "PENDING", "ACQUIRING_IN_CLOUD"})


def _utc_now(now_fn: Optional[Callable[[], datetime]] = None) -> datetime:
    dt = now_fn() if now_fn is not None else datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class AcquisitionBackend(abc.ABC):
    """Abstract interface for on-demand person acquisition backends."""

    @abc.abstractmethod
    def acquire_person(
        self,
        person: str,
        *,
        season: str,
        job_id: str,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """Acquire or dispatch acquisition for a person and return a structured result dict."""


class LocalCliAcquisitionBackend(AcquisitionBackend):
    """Synchronous acquisition backend using either a local CLI command or build_person_card()."""

    def __init__(
        self,
        *,
        collector_cmd: Optional[Union[str, Sequence[str]]] = None,
        instance: Optional[Any] = None,
        instance_root: Optional[Any] = None,
        local_resolver_inputs: Optional[Mapping[str, Any]] = None,
        whitelist_sources: Sequence[str] = ("local_kb",),
        timeout_seconds: float = 300.0,
    ) -> None:
        if isinstance(collector_cmd, str):
            parts = shlex.split(collector_cmd)
            self.collector_cmd: Optional[Tuple[str, ...]] = tuple(parts) if parts else None
        elif collector_cmd:
            self.collector_cmd = tuple(str(x) for x in collector_cmd)
        else:
            self.collector_cmd = None
        if instance is None and instance_root is not None:
            root = Path(instance_root).expanduser().resolve()
            instance = Instance(
                root=root,
                config_root=(root / "config").resolve(),
                credentials_store=(root / ".credentials").resolve(),
                real_fixture_root=(root / "fixtures" / "real").resolve(),
                data_root=(root / "data").resolve(),
            )
        self.instance = instance
        if instance_root is not None:
            self.instance_root: Optional[Path] = Path(instance_root).expanduser().resolve()
        elif instance is not None and getattr(instance, "root", None) is not None:
            self.instance_root = Path(instance.root).expanduser().resolve()
        else:
            self.instance_root = None
        self.local_resolver_inputs = dict(local_resolver_inputs or {})
        self.whitelist_sources = tuple(whitelist_sources)
        self.timeout_seconds = float(timeout_seconds)

    def acquire_person(
        self,
        person: str,
        *,
        season: str,
        job_id: str,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        collector_sources: Optional[List[str]] = None
        if self.collector_cmd:
            cmd = [
                *self.collector_cmd,
                "--person",
                person,
                "--season",
                season,
                "--job-id",
                job_id,
            ]
            env = os.environ.copy()
            env["CBA_KB_TARGET_PERSON"] = person
            env["CBA_KB_TARGET_SEASON"] = season
            env["CBA_KB_TARGET_JOB_ID"] = job_id
            if self.instance_root is not None:
                cmd.extend(["--instance-root", str(self.instance_root)])
                env["CBA_KB_INSTANCE_ROOT"] = str(self.instance_root)
            if force_refresh:
                cmd.append("--force-refresh")
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
                env=env,
                shell=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"LOCAL_CLI_ACQUISITION_FAILED(rc={proc.returncode}): {proc.stderr.strip()}"
                )
            stdout_text = proc.stdout.strip()
            if stdout_text:
                try:
                    parsed = json.loads(stdout_text)
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict):
                    p_status = str(parsed.get("status") or "").upper()
                    if isinstance(parsed.get("whitelist_sources_checked"), list):
                        collector_sources = [str(s) for s in parsed["whitelist_sources_checked"]]
                    if p_status in ("ACQUIRED_AND_BUILT", "BUILT") and (
                        parsed.get("card_markdown") or parsed.get("markdown")
                    ):
                        return parsed
                    if p_status in ("UNDECIDED", "ACQUIRING_IN_CLOUD"):
                        return parsed
                    if p_status == "UNKNOWN_AFTER_WHITELIST_CHECK" and self.instance is None and not self.local_resolver_inputs:
                        return parsed

        res = build_person_card(
            person,
            season=season,
            instance=self.instance,
            **self.local_resolver_inputs,
        )
        status = str(res.get("status") or "").upper()
        if status == "BUILT":
            md = str(res.get("markdown") or "")
            obs = res.get("observation_horizon") if isinstance(res.get("observation_horizon"), dict) else {}
            return {
                "status": "ACQUIRED_AND_BUILT",
                "person": res.get("person"),
                "filename": res.get("filename"),
                "card_markdown": md,
                "card_sha256": _sha256_text(md),
                "observed_until": obs.get("observed_until"),
                "coverage_status": obs.get("overall_status"),
            }
        if status == "UNDECIDED":
            return {
                "status": "UNDECIDED",
                "candidates": list(res.get("candidates") or []),
            }
        return {
            "status": "UNKNOWN_AFTER_WHITELIST_CHECK",
            "whitelist_sources_checked": list(collector_sources or self.whitelist_sources),
        }


class AsyncDispatchAcquisitionBackend(AcquisitionBackend):
    """Asynchronous acquisition backend dispatching cloud runner jobs (e.g. GitHub Actions)."""

    def __init__(
        self,
        *,
        dispatcher: Optional[Callable[[Dict[str, Any]], Any]] = None,
        github_repo: Optional[str] = None,
        workflow_id: Optional[str] = None,
        github_token: Optional[str] = None,
        ref: str = "main",
        timeout_seconds: float = 15.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.dispatcher = dispatcher
        self.github_repo = github_repo
        self.workflow_id = workflow_id
        self.github_token = str(github_token) if github_token else None
        self.ref = ref
        self.timeout_seconds = float(timeout_seconds)
        self._http_client = session

    def acquire_person(
        self,
        person: str,
        *,
        season: str,
        job_id: str,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        payload = {
            "job_id": job_id,
            "person": person,
            "season": season,
            "force_refresh": bool(force_refresh),
        }
        if self.dispatcher is not None:
            dispatch_res = self.dispatcher(payload)
            if isinstance(dispatch_res, dict):
                out = dict(dispatch_res)
                out.setdefault("status", "ACQUIRING_IN_CLOUD")
                out.setdefault("job_id", job_id)
                out.setdefault("next_action", "call check_acquisition_job")
                return out
            return {
                "status": "ACQUIRING_IN_CLOUD",
                "job_id": job_id,
                "dispatched": True,
                "next_action": "call check_acquisition_job",
            }

        if not (self.github_repo and self.workflow_id and self.github_token):
            raise RuntimeError("ASYNC_DISPATCH_BACKEND_UNCONFIGURED")

        url = (
            f"https://api.github.com/repos/{self.github_repo}/actions/"
            f"workflows/{self.workflow_id}/dispatches"
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": " ".join(("token", self.github_token)),
            "X-GitHub-Api-Version": "2022-11-28",
        }
        body = {
            "ref": self.ref,
            "inputs": {
                "job_id": job_id,
                "person": person,
                "season": season,
                "force_refresh": "true" if force_refresh else "false",
            },
        }
        client = self._http_client or requests
        resp = client.post(url, headers=headers, json=body, timeout=self.timeout_seconds)
        if resp.status_code not in (200, 201, 202, 204):
            raise RuntimeError(
                f"ASYNC_DISPATCH_FAILED(status={resp.status_code}): {resp.text[:256]}"
            )
        return {
            "status": "ACQUIRING_IN_CLOUD",
            "job_id": job_id,
            "dispatched": True,
            "next_action": "call check_acquisition_job",
        }


def _normalize_master_rows(
    master_rows: Optional[Sequence[Mapping[str, Any]]],
) -> Optional[List[Dict[str, Any]]]:
    if master_rows is None:
        return None
    normalized: List[Dict[str, Any]] = []
    for row in master_rows:
        if not isinstance(row, Mapping):
            continue
        item = dict(row)
        if not item.get("player") and item.get("player_name"):
            item["player"] = item["player_name"]
        if not item.get("club_official") and item.get("club"):
            item["club_official"] = item["club"]
        normalized.append(item)
    return normalized


class SnowballOrchestrator:
    """Core state machine for Pure-Cloud On-Demand Snowball serving and acquisition."""

    def __init__(
        self,
        state_store: Any,
        *,
        acquisition_backend: Optional[Any] = None,
        drive_upserter: Optional[Callable[..., Dict[str, Any]]] = None,
        snowball_folder_id: Optional[str] = None,
        forbidden_folder_ids: Sequence[str] = (),
        production_policy: Optional[Mapping[str, Any]] = None,
        local_resolver_inputs: Optional[Mapping[str, Any]] = None,
        instance: Optional[Any] = None,
        default_negative_ttl_hours: float = 24.0,
        stale_after_hours: float = 48.0,
        lock_ttl_seconds: float = DEFAULT_LOCK_TTL_SECONDS,
        now_fn: Optional[Callable[[], datetime]] = None,
        ensure_local_instance_fn: Optional[Callable[[], Any]] = None,
        on_state_mutated_fn: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> None:
        self.state_store = state_store
        self.acquisition_backend = acquisition_backend
        self.drive_upserter = drive_upserter
        self.snowball_folder_id = snowball_folder_id
        self.forbidden_folder_ids = tuple(forbidden_folder_ids or ())
        self.production_policy = dict(production_policy) if production_policy is not None else None
        self.local_resolver_inputs = (
            dict(local_resolver_inputs) if local_resolver_inputs is not None else None
        )
        self.instance = instance
        self.default_negative_ttl_hours = float(default_negative_ttl_hours)
        self.stale_after_hours = float(stale_after_hours)
        self.lock_ttl_seconds = float(lock_ttl_seconds)
        self.now_fn = now_fn
        self.ensure_local_instance_fn = ensure_local_instance_fn
        self.on_state_mutated_fn = on_state_mutated_fn
        self._inflight_locks_guard = threading.Lock()
        self._inflight_locks: Dict[Tuple[str, str], threading.Lock] = {}

    def _now_utc(self) -> datetime:
        return _utc_now(self.now_fn)

    def _person_mutex(self, season: str, person: str) -> threading.Lock:
        key = (season.strip(), person.strip().casefold())
        with self._inflight_locks_guard:
            lock = self._inflight_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._inflight_locks[key] = lock
            return lock

    @staticmethod
    def _is_lock_expired(lock_doc: Mapping[str, Any], *, now_utc: datetime) -> bool:
        exp = _parse_utc(lock_doc.get("expires_at_utc"))
        if exp is None:
            return False
        return now_utc >= exp

    @staticmethod
    def _can_adopt_existing_lock(
        existing: Mapping[str, Any],
        *,
        explicit_job_id: Optional[str],
        explicit_owner_id: Optional[str],
    ) -> Tuple[bool, Optional[str]]:
        if not explicit_job_id:
            return False, None
        existing_job_id = str(existing.get("job_id") or "").strip()
        if existing_job_id != explicit_job_id:
            return False, None
        existing_owner = str(existing.get("owner_id") or "").strip()
        if explicit_owner_id:
            if existing_owner and existing_owner != explicit_owner_id:
                return False, None
            return True, existing_owner or explicit_owner_id
        if existing_owner:
            return True, existing_owner
        return False, None

    def _try_acquire_lock(
        self,
        *,
        season: str,
        person: str,
        job_id: str,
        explicit_job_id: Optional[str],
        owner_id: str,
        explicit_owner_id: Optional[str],
        now_utc: datetime,
        now_iso: str,
        force_refresh: bool,
    ) -> Tuple[bool, Dict[str, Any]]:
        expires_iso = _iso_utc(now_utc + timedelta(seconds=self.lock_ttl_seconds))
        extra: Dict[str, Any] = {
            "person": person,
            "season": season,
            "force_refresh": bool(force_refresh),
            "created_at_utc": now_iso,
            "updated_at_utc": now_iso,
            "expires_at_utc": expires_iso,
        }
        with self._person_mutex(season, person):
            acquire_fn = getattr(self.state_store, "acquire_active_job_lock", None)
            if callable(acquire_fn):
                call_kwargs: Dict[str, Any] = {
                    "job_id": job_id,
                    "owner_id": owner_id,
                    "extra": extra,
                }
                try:
                    sig = inspect.signature(acquire_fn)
                    if "lock_ttl_seconds" in sig.parameters:
                        call_kwargs["lock_ttl_seconds"] = self.lock_ttl_seconds
                    if "now_utc" in sig.parameters:
                        call_kwargs["now_utc"] = now_utc
                except (TypeError, ValueError):
                    pass
                acquired, lock_doc = acquire_fn(
                    season,
                    person,
                    **call_kwargs,
                )
                if acquired:
                    doc = dict(lock_doc) if isinstance(lock_doc, dict) else {"job_id": job_id, "owner_id": owner_id}
                    doc.setdefault("owner_id", owner_id)
                    return True, doc
                existing = dict(lock_doc) if isinstance(lock_doc, dict) else {}
                adoptable, adopted_owner = self._can_adopt_existing_lock(
                    existing,
                    explicit_job_id=explicit_job_id,
                    explicit_owner_id=explicit_owner_id,
                )
                if adoptable and adopted_owner:
                    existing["owner_id"] = adopted_owner
                    return True, existing
                return False, existing

            # Fallback for custom fake state stores without acquire_active_job_lock
            active_lock = self.state_store.find_active_job_for_person(person, season)
            if isinstance(active_lock, dict):
                lock_status = str(active_lock.get("status") or "").upper()
                lock_job_id = str(active_lock.get("job_id") or "").strip()
                if (
                    lock_status != "RELEASED"
                    and lock_status in _ACTIVE_LOCK_STATUSES
                    and lock_job_id
                    and not self._is_lock_expired(active_lock, now_utc=now_utc)
                ):
                    adoptable, adopted_owner = self._can_adopt_existing_lock(
                        active_lock,
                        explicit_job_id=explicit_job_id,
                        explicit_owner_id=explicit_owner_id,
                    )
                    if adoptable and adopted_owner:
                        adopted = dict(active_lock)
                        adopted["owner_id"] = adopted_owner
                        return True, adopted
                    return False, dict(active_lock)
            lock_doc = {
                "job_id": job_id,
                "owner_id": owner_id,
                "status": "RUNNING",
                **extra,
            }
            self.state_store.set_active_job_lock(person, season, lock_doc)
            return True, lock_doc

    def _release_lock(
        self,
        *,
        season: str,
        person: str,
        owner_id: Optional[str],
        job_id: str,
    ) -> None:
        with self._person_mutex(season, person):
            release_fn = getattr(self.state_store, "release_active_job_lock", None)
            if callable(release_fn):
                release_fn(season, person, owner_id=owner_id, job_id=job_id)
                return
            current = self.state_store.find_active_job_for_person(person, season)
            if isinstance(current, dict):
                cur_job_id = str(current.get("job_id") or "").strip()
                cur_owner = current.get("owner_id")
                if cur_job_id and cur_job_id != job_id:
                    return
                if owner_id is not None and cur_owner is not None and cur_owner != owner_id:
                    return
            self.state_store.clear_active_job_lock(person, season)

    def _resolve_local_candidates(
        self,
        query: str,
        *,
        now_utc: datetime,
    ) -> Optional[Dict[str, Any]]:
        if self.local_resolver_inputs is None and self.instance is None:
            return None
        kwargs = dict(self.local_resolver_inputs or {})
        return resolve_person_query(
            query,
            instance=self.instance,
            master_rows=_normalize_master_rows(kwargs.get("master_rows")),
            foreign_rows=kwargs.get("foreign_rows"),
            identity_registry=kwargs.get("identity_registry"),
            negative_receipts=kwargs.get("negative_receipts"),
            data_cutoff=kwargs.get("data_cutoff"),
            now_utc=now_utc,
        )

    def _lookup_cached_card(
        self,
        season: str,
        query: str,
        *,
        resolved_person: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        keys: List[str] = [query]
        if isinstance(resolved_person, dict):
            for field in ("player_name", "player_id", "player_uid"):
                val = str(resolved_person.get(field) or "").strip()
                if val and val not in keys:
                    keys.append(val)
        for key in keys:
            card = self.state_store.get_card(season, key)
            if isinstance(card, dict) and (card.get("markdown") or card.get("card_markdown")):
                return card
        return None

    @staticmethod
    def _is_card_fresh(
        card: Mapping[str, Any],
        *,
        now_utc: datetime,
        freshness_ttl_hours: float,
    ) -> bool:
        ts_raw = (
            card.get("updated_at_utc")
            or card.get("generated_at_utc")
            or card.get("observed_until")
        )
        ts = _parse_utc(ts_raw)
        if ts is None:
            return False
        age = now_utc - ts
        return age <= timedelta(hours=float(freshness_ttl_hours))

    def _find_negative_receipt_for_season(
        self,
        query: str,
        *,
        season: str,
        now_utc: datetime,
    ) -> Optional[Dict[str, Any]]:
        receipts = self.state_store.get_negative_receipts() or []
        filtered = [
            rec
            for rec in receipts
            if isinstance(rec, dict)
            and (not rec.get("season") or str(rec.get("season")) == season)
        ]
        return _find_active_negative_receipt(query, filtered, now_utc=now_utc)

    def get_league_coverage(self) -> Dict[str, Any]:
        """Evaluate league-wide observation horizon from R2 coverage_ledger.json."""
        from .consumer_surface import evaluate_league_coverage

        ledger = self.state_store.get_coverage_ledger()
        return evaluate_league_coverage(
            ledger,
            now_utc=self._now_utc(),
            stale_after_hours=self.stale_after_hours,
        )

    def check_acquisition_job(self, job_id: str) -> Dict[str, Any]:
        """Check status of an async acquisition job and attach completed card_markdown."""
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("JOB_ID_REQUIRED")
        jid = job_id.strip()
        job = self.state_store.get_job(jid)
        if not isinstance(job, dict):
            return {
                "job_id": jid,
                "status": "NOT_FOUND",
            }

        resp = dict(job)
        status_upper = str(resp.get("status") or "").upper()
        if status_upper in ("COMPLETED", "ACQUIRED_AND_BUILT"):
            if not resp.get("card_markdown"):
                person = str(resp.get("person") or "").strip()
                season = str(resp.get("season") or "").strip()
                if person and season:
                    card = self.state_store.get_card(season, person)
                    if isinstance(card, dict):
                        md = str(card.get("markdown") or card.get("card_markdown") or "")
                        if md:
                            md_sha = _sha256_text(md)
                            expected_sha = resp.get("card_sha256")
                            if expected_sha and expected_sha != md_sha:
                                raise ValueError("JOB_CARD_SHA256_MISMATCH")
                            resp["card_markdown"] = md
                            resp.setdefault("card_sha256", md_sha)
                            resp.setdefault("filename", card.get("filename"))
                            resp.setdefault("observed_until", card.get("observed_until"))
                            resp.setdefault("coverage_status", card.get("coverage_status"))
                            if card.get("drive_sync") is not None:
                                resp.setdefault("drive_sync", card.get("drive_sync"))
        elif status_upper in _ACTIVE_LOCK_STATUSES:
            resp.setdefault("next_action", "call check_acquisition_job")
        return resp

    def get_or_acquire_person(
        self,
        person: str,
        *,
        season: str = "2026-2027",
        force_refresh: bool = False,
        freshness_ttl_hours: float = 24.0,
        job_id: Optional[str] = None,
        owner_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Serve fresh person card from R2 or orchestrate on-demand acquisition."""
        if not isinstance(person, str) or not person.strip():
            raise ValueError("PERSON_QUERY_REQUIRED")

        q_clean = person.strip()
        explicit_job_id = (
            job_id.strip() if isinstance(job_id, str) and job_id.strip() else None
        )
        explicit_owner_id = (
            owner_id.strip() if isinstance(owner_id, str) and owner_id.strip() else None
        )
        now = self._now_utc()

        # 1. Fail-closed homonym / multi-candidate disambiguation (0 network)
        resolved_person: Optional[Dict[str, Any]] = None
        local_res = self._resolve_local_candidates(q_clean, now_utc=now)
        if local_res is not None:
            if local_res.get("status") == "UNDECIDED":
                return {
                    "status": "UNDECIDED",
                    "person": q_clean,
                    "season": season,
                    "candidates": list(local_res.get("candidates") or []),
                    "network_called": False,
                    "next_action": "disambiguate_candidate",
                }
            if local_res.get("status") == "RESOLVED":
                resolved_person = local_res.get("person")

        # 2. Fast path: fresh cached card in R2 (0 network)
        if not force_refresh:
            card = self._lookup_cached_card(season, q_clean, resolved_person=resolved_person)
            if card is not None and self._is_card_fresh(
                card, now_utc=now, freshness_ttl_hours=freshness_ttl_hours
            ):
                md = str(card.get("markdown") or card.get("card_markdown") or "")
                md_sha = _sha256_text(md)
                stored_sha = card.get("card_sha256")
                if stored_sha and stored_sha != md_sha:
                    raise ValueError("CACHED_CARD_SHA256_MISMATCH")
                if explicit_job_id:
                    done_iso = _iso_utc(now)
                    release_owner = explicit_owner_id
                    if release_owner is None:
                        cur_lock = self.state_store.find_active_job_for_person(q_clean, season)
                        if (
                            isinstance(cur_lock, dict)
                            and str(cur_lock.get("job_id") or "").strip() == explicit_job_id
                        ):
                            release_owner = str(cur_lock.get("owner_id") or "").strip() or None
                    self.state_store.put_job(
                        explicit_job_id,
                        {
                            "job_id": explicit_job_id,
                            "status": "COMPLETED",
                            "person": q_clean,
                            "season": season,
                            "filename": card.get("filename"),
                            "card_sha256": md_sha,
                            "updated_at_utc": done_iso,
                        },
                    )
                    self._release_lock(
                        season=season,
                        person=q_clean,
                        owner_id=release_owner,
                        job_id=explicit_job_id,
                    )
                return {
                    "status": "HIT_FRESH",
                    "person": q_clean,
                    "season": season,
                    "filename": card.get("filename"),
                    "card_markdown": md,
                    "card_sha256": md_sha,
                    "observed_until": card.get("observed_until"),
                    "coverage_status": card.get("coverage_status"),
                    "updated_at_utc": card.get("updated_at_utc"),
                    "drive_sync": card.get("drive_sync"),
                    "network_called": False,
                }

            # 3. Active Negative-Receipt within TTL -> UNKNOWN_NEGATIVE_CACHED (0 network)
            active_nr = self._find_negative_receipt_for_season(
                q_clean, season=season, now_utc=now
            )
            if active_nr is not None:
                if explicit_job_id:
                    release_owner = explicit_owner_id
                    if release_owner is None:
                        cur_lock = self.state_store.find_active_job_for_person(q_clean, season)
                        if (
                            isinstance(cur_lock, dict)
                            and str(cur_lock.get("job_id") or "").strip() == explicit_job_id
                        ):
                            release_owner = str(cur_lock.get("owner_id") or "").strip() or None
                    self.state_store.put_job(
                        explicit_job_id,
                        {
                            "job_id": explicit_job_id,
                            "status": "COMPLETED_UNKNOWN",
                            "person": q_clean,
                            "season": season,
                            "negative_receipt": active_nr,
                            "updated_at_utc": _iso_utc(now),
                        },
                    )
                    self._release_lock(
                        season=season,
                        person=q_clean,
                        owner_id=release_owner,
                        job_id=explicit_job_id,
                    )
                return {
                    "status": "UNKNOWN_NEGATIVE_CACHED",
                    "person": q_clean,
                    "season": season,
                    "negative_receipt": active_nr,
                    "whitelist_sources_checked": list(
                        active_nr.get("whitelist_sources_checked") or []
                    ),
                    "network_called": False,
                }

        # 4. Slow path: check backend availability & acquire atomic dedup lock BEFORE job write / backend call
        if self.acquisition_backend is None:
            active_lock = self.state_store.find_active_job_for_person(q_clean, season)
            if isinstance(active_lock, dict):
                lock_status = str(active_lock.get("status") or "").upper()
                lock_job_id = str(active_lock.get("job_id") or "").strip()
                if (
                    lock_status != "RELEASED"
                    and lock_status in _ACTIVE_LOCK_STATUSES
                    and lock_job_id
                    and not self._is_lock_expired(active_lock, now_utc=now)
                ):
                    return {
                        "status": "ACQUIRING_IN_CLOUD",
                        "job_id": lock_job_id,
                        "person": q_clean,
                        "season": season,
                        "deduplicated": True,
                        "network_called": False,
                        "next_action": "call check_acquisition_job",
                    }
            return {
                "status": "UNKNOWN_NO_BACKEND",
                "person": q_clean,
                "season": season,
                "network_called": False,
            }

        resolved_job_id = explicit_job_id or f"job_{uuid.uuid4().hex[:12]}"
        effective_owner_id = explicit_owner_id or uuid.uuid4().hex
        now_iso = _iso_utc(now)

        acquired, lock_doc = self._try_acquire_lock(
            season=season,
            person=q_clean,
            job_id=resolved_job_id,
            explicit_job_id=explicit_job_id,
            owner_id=effective_owner_id,
            explicit_owner_id=explicit_owner_id,
            now_utc=now,
            now_iso=now_iso,
            force_refresh=force_refresh,
        )
        if not acquired:
            existing_job_id = str(lock_doc.get("job_id") or "").strip()
            return {
                "status": "ACQUIRING_IN_CLOUD",
                "job_id": existing_job_id,
                "person": q_clean,
                "season": season,
                "deduplicated": True,
                "network_called": False,
                "next_action": "call check_acquisition_job",
            }

        lock_owner_id: Optional[str] = str(lock_doc.get("owner_id") or "").strip() or effective_owner_id
        job_doc: Dict[str, Any] = {
            "job_id": resolved_job_id,
            "owner_id": lock_owner_id,
            "status": "RUNNING",
            "person": q_clean,
            "season": season,
            "force_refresh": bool(force_refresh),
            "created_at_utc": now_iso,
            "updated_at_utc": now_iso,
        }
        self.state_store.put_job(resolved_job_id, job_doc)

        try:
            if callable(self.ensure_local_instance_fn):
                self.ensure_local_instance_fn()
            backend_result = self.acquisition_backend.acquire_person(
                q_clean,
                season=season,
                job_id=resolved_job_id,
                force_refresh=force_refresh,
            )
        except Exception as exc:
            self.state_store.put_job(
                resolved_job_id,
                {
                    **job_doc,
                    "status": "FAILED",
                    "error": str(exc),
                    "updated_at_utc": _iso_utc(self._now_utc()),
                },
            )
            self._release_lock(
                season=season,
                person=q_clean,
                owner_id=lock_owner_id,
                job_id=resolved_job_id,
            )
            raise

        if not isinstance(backend_result, dict):
            self._release_lock(
                season=season,
                person=q_clean,
                owner_id=lock_owner_id,
                job_id=resolved_job_id,
            )
            raise ValueError("ACQUISITION_BACKEND_INVALID_RESULT")

        backend_status = str(backend_result.get("status") or "").upper()
        done_iso = _iso_utc(self._now_utc())

        if backend_status in ("ACQUIRED_AND_BUILT", "BUILT"):
            md = str(
                backend_result.get("card_markdown")
                or backend_result.get("markdown")
                or ""
            )
            if not md:
                self._release_lock(
                    season=season,
                    person=q_clean,
                    owner_id=lock_owner_id,
                    job_id=resolved_job_id,
                )
                raise ValueError("ACQUIRED_CARD_MARKDOWN_EMPTY")
            md_sha = _sha256_text(md)
            provided_sha = backend_result.get("card_sha256")
            if provided_sha and provided_sha != md_sha:
                self._release_lock(
                    season=season,
                    person=q_clean,
                    owner_id=lock_owner_id,
                    job_id=resolved_job_id,
                )
                raise ValueError("ACQUIRED_CARD_SHA256_MISMATCH")

            person_meta = (
                backend_result.get("person")
                if isinstance(backend_result.get("person"), dict)
                else {}
            )
            filename = str(
                backend_result.get("filename")
                or f"研究卡_{q_clean}_{season}_CURRENT.md"
            )
            observed_until = backend_result.get("observed_until")
            coverage_status = backend_result.get("coverage_status")
            if not observed_until or not coverage_status:
                try:
                    cov = self.get_league_coverage()
                    observed_until = observed_until or cov.get("observed_until")
                    coverage_status = coverage_status or cov.get("overall_status")
                except Exception:
                    pass

            drive_receipt: Optional[Dict[str, Any]] = None
            if self.drive_upserter is not None and self.snowball_folder_id:
                upsert_kwargs: Dict[str, Any] = {
                    "folder_id": self.snowball_folder_id,
                    "filename": filename,
                    "content_bytes": md.encode("utf-8"),
                    "forbidden_folder_ids": list(self.forbidden_folder_ids),
                }
                if self.production_policy is not None:
                    upsert_kwargs["production_policy"] = self.production_policy
                drive_receipt = self.drive_upserter(**upsert_kwargs)

            card_record: Dict[str, Any] = {
                "player_name": person_meta.get("player_name") or q_clean,
                "player_id": person_meta.get("player_id"),
                "player_uid": person_meta.get("player_uid"),
                "season": season,
                "filename": filename,
                "markdown": md,
                "card_sha256": md_sha,
                "updated_at_utc": done_iso,
                "observed_until": observed_until,
                "coverage_status": coverage_status,
                "drive_sync": drive_receipt,
            }
            self.state_store.put_card(season, q_clean, card_record)

            self.state_store.put_job(
                resolved_job_id,
                {
                    **job_doc,
                    "status": "COMPLETED",
                    "updated_at_utc": done_iso,
                    "filename": filename,
                    "card_sha256": md_sha,
                    "drive_sync": drive_receipt,
                },
            )
            result: Dict[str, Any] = {
                "status": "ACQUIRED_AND_BUILT",
                "job_id": resolved_job_id,
                "person": q_clean,
                "season": season,
                "filename": filename,
                "card_markdown": md,
                "card_sha256": md_sha,
                "observed_until": observed_until,
                "coverage_status": coverage_status,
                "drive_sync": drive_receipt,
                "network_called": True,
            }
            try:
                if callable(self.on_state_mutated_fn):
                    self.on_state_mutated_fn(result)
            finally:
                self._release_lock(
                    season=season,
                    person=q_clean,
                    owner_id=lock_owner_id,
                    job_id=resolved_job_id,
                )
            return result

        if backend_status == "UNKNOWN_AFTER_WHITELIST_CHECK":
            ttl_raw = backend_result.get("ttl_hours")
            ttl_hours = (
                float(ttl_raw)
                if isinstance(ttl_raw, (int, float)) and not isinstance(ttl_raw, bool)
                else self.default_negative_ttl_hours
            )
            whitelist_checked = list(backend_result.get("whitelist_sources_checked") or [])
            receipt: Dict[str, Any] = {
                "schema": NEGATIVE_RECEIPT_SCHEMA,
                "query_person": q_clean,
                "season": season,
                "checked_at_utc": done_iso,
                "ttl_hours": ttl_hours,
                "whitelist_sources_checked": whitelist_checked,
                "job_id": resolved_job_id,
            }
            self.state_store.append_negative_receipt(receipt)
            self.state_store.put_job(
                resolved_job_id,
                {
                    **job_doc,
                    "status": "COMPLETED_UNKNOWN",
                    "updated_at_utc": done_iso,
                    "negative_receipt": receipt,
                },
            )
            result = {
                "status": "UNKNOWN_AFTER_WHITELIST_CHECK",
                "job_id": resolved_job_id,
                "person": q_clean,
                "season": season,
                "negative_receipt": receipt,
                "whitelist_sources_checked": whitelist_checked,
                "network_called": True,
            }
            try:
                if callable(self.on_state_mutated_fn):
                    self.on_state_mutated_fn(result)
            finally:
                self._release_lock(
                    season=season,
                    person=q_clean,
                    owner_id=lock_owner_id,
                    job_id=resolved_job_id,
                )
            return result

        if backend_status == "UNDECIDED":
            candidates = list(backend_result.get("candidates") or [])
            self.state_store.put_job(
                resolved_job_id,
                {
                    **job_doc,
                    "status": "UNDECIDED",
                    "updated_at_utc": done_iso,
                    "candidates": candidates,
                },
            )
            self._release_lock(
                season=season,
                person=q_clean,
                owner_id=lock_owner_id,
                job_id=resolved_job_id,
            )
            return {
                "status": "UNDECIDED",
                "job_id": resolved_job_id,
                "person": q_clean,
                "season": season,
                "candidates": candidates,
                "network_called": True,
                "next_action": "disambiguate_candidate",
            }

        if backend_status == "ACQUIRING_IN_CLOUD":
            returned_job_id = str(backend_result.get("job_id") or resolved_job_id).strip() or resolved_job_id
            if returned_job_id != resolved_job_id:
                async_doc = {
                    **job_doc,
                    "job_id": returned_job_id,
                    "updated_at_utc": done_iso,
                }
                self.state_store.put_job(returned_job_id, async_doc)
                self.state_store.set_active_job_lock(q_clean, season, async_doc)
            return {
                "status": "ACQUIRING_IN_CLOUD",
                "job_id": returned_job_id,
                "person": q_clean,
                "season": season,
                "deduplicated": False,
                "network_called": True,
                "next_action": str(
                    backend_result.get("next_action") or "call check_acquisition_job"
                ),
            }

        # Fallback for unexpected backend status
        self.state_store.put_job(
            resolved_job_id,
            {
                **job_doc,
                "status": backend_status or "FAILED",
                "updated_at_utc": done_iso,
            },
        )
        self._release_lock(
            season=season,
            person=q_clean,
            owner_id=lock_owner_id,
            job_id=resolved_job_id,
        )
        return {
            **backend_result,
            "job_id": resolved_job_id,
            "person": q_clean,
            "season": season,
            "network_called": True,
        }


__all__ = [
    "AcquisitionBackend",
    "AsyncDispatchAcquisitionBackend",
    "DEFAULT_LOCK_TTL_SECONDS",
    "LocalCliAcquisitionBackend",
    "NEGATIVE_RECEIPT_SCHEMA",
    "SnowballOrchestrator",
]
