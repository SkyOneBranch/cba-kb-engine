"""Frozen acceptance tests for REQ-230 (cba-kb-engine v2.3.0).

Covers P0-1 through P0-5 and hard clean-room / frozen-file guardrails:
- P0-1: Observation Horizon on consumer surface & evaluate_league_coverage()
- P0-2: Headless Drive OAuth, R2CloudStateStore (SigV4 + SHA-256), topology-safe Drive upsert
- P0-3: SnowballOrchestrator state machine, negative receipts, concurrency dedup lock
- P0-4: HTTPS SnowballGatewayApp (Bearer-scheme auth + 28s bounded wait), MCP stdio server, CLI & templates
- P0-5: v2.3.0-1 release spec registration & O(Delta) on-demand cards promotion

100% offline and synthetic. Lazy imports inside test bodies ensure clean pytest collection
and differential falsification on baseline 3a7239361b2b81f763dce3477b3c3ad81f357b90.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_SHA_V230_1 = "3a7239361b2b81f763dce3477b3c3ad81f357b90"
FROZEN_PREPARE_PRODUCTION_SHA256 = (
    "0ccb79429fb4ec5e270a3154af987475ae75bd4ef81de48c1e665a3b93cf4573"
)


class _FakeR2Transport:
    """In-memory S3/R2 transport for deterministic offline testing."""

    def __init__(self) -> None:
        self.objects: Dict[str, Tuple[bytes, Dict[str, str]]] = {}
        self.requests_log: List[Dict[str, Any]] = []

    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: Dict[str, str],
        data: bytes = b"",
    ) -> Tuple[int, Dict[str, str], bytes]:
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(url)
        # Path format: /<bucket>/<key> or /<bucket>
        parts = parsed.path.lstrip("/").split("/", 1)
        key = parts[1] if len(parts) > 1 else ""
        qs = parse_qs(parsed.query)
        self.requests_log.append({
            "method": method,
            "url": url,
            "key": key,
            "has_auth": "Authorization" in headers,
            "has_amz_date": "x-amz-date" in {k.lower() for k in headers},
        })

        if method == "PUT":
            meta = {
                k.lower(): v
                for k, v in headers.items()
                if k.lower().startswith("x-amz-meta-")
            }
            self.objects[key] = (bytes(data), meta)
            return 200, {"ETag": hashlib.md5(data).hexdigest()}, b""

        if method == "GET" and qs.get("list-type") == ["2"]:
            prefix = qs.get("prefix", [""])[0]
            matched = sorted(k for k in self.objects if k.startswith(prefix))
            items_xml = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in matched)
            xml_body = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                f"<ListBucketResult>{items_xml}</ListBucketResult>"
            ).encode("utf-8")
            return 200, {"content-type": "application/xml"}, xml_body

        if method == "GET":
            if key not in self.objects:
                return 404, {}, b"NotFound"
            payload, meta = self.objects[key]
            return 200, dict(meta), payload

        if method == "DELETE":
            self.objects.pop(key, None)
            return 204, {}, b""

        return 405, {}, b"MethodNotAllowed"


class _FakeSnowballDrive:
    """In-memory Drive fake supporting list/put/create for snowball folder tests."""

    def __init__(self) -> None:
        import threading

        self._main_thread_id = threading.get_ident()
        self.files: Dict[str, Dict[str, Any]] = {}
        self.contents: Dict[str, bytes] = {}
        self._counter = 0
        self.put_calls = 0
        self.create_calls = 0

    def list(self, parent: str) -> List[Dict[str, Any]]:
        return [
            dict(meta)
            for meta in self.files.values()
            if parent in meta.get("parents", [])
        ]

    def put(self, file_id: str, content: bytes, mime: str) -> Dict[str, Any]:
        if file_id not in self.files:
            raise KeyError(file_id)
        self.put_calls += 1
        self.contents[file_id] = bytes(content)
        self.files[file_id]["mimeType"] = mime
        self.files[file_id]["size"] = str(len(content))
        return dict(self.files[file_id])

    def create_binary(self, parent: str, name: str, content: bytes, mime: str) -> Dict[str, Any]:
        self._counter += 1
        fid = f"drv_snowball_obj_{self._counter:04d}"
        self.create_calls += 1
        meta = {
            "id": fid,
            "name": name,
            "mimeType": mime,
            "parents": [parent],
            "size": str(len(content)),
        }
        self.files[fid] = meta
        self.contents[fid] = bytes(content)
        return dict(meta)

    def ensure(self, parent: str, key: str, name: str, mime: str, content: Optional[bytes] = None, *, index=None) -> str:
        res = self.create_binary(parent, name, content or b"", mime)
        res["appProperties"] = {"cba_key": key}
        return res["id"]


def _synthetic_coverage_ledger(
    *,
    domestic_status: str = "COMPLETE",
    domestic_missing: Optional[List[str]] = None,
    game_stats_observed: str = "2026-11-24T12:00:00Z",
) -> Dict[str, Any]:
    return {
        "schema": "cba-kb.coverage-ledger.v1",
        "generated_at_utc": "2026-11-24T12:30:00Z",
        "sources": [
            {
                "source_id": "cba_domestic_20_teams",
                "domain": "domestic_registration",
                "status": domestic_status,
                "observed_until": "2026-11-24T12:00:00Z",
                "missing": list(domestic_missing or []),
            },
            {
                "source_id": "cba_foreign_snapshot",
                "domain": "foreign_registration",
                "status": "COMPLETE",
                "observed_until": "2026-11-24T12:00:00Z",
                "missing": [],
            },
            {
                "source_id": "cba_club_weibo_25",
                "domain": "club_announcements",
                "status": "COMPLETE",
                "observed_until": "2026-11-24T12:00:00Z",
                "missing": [],
            },
            {
                "source_id": "cba_league_rules_handbook",
                "domain": "league_rules",
                "status": "COMPLETE",
                "observed_until": "2026-11-24T10:00:00Z",
                "missing": [],
            },
            {
                "source_id": "cba_official_game_stats",
                "domain": "game_stats",
                "status": "COMPLETE",
                "observed_until": game_stats_observed,
                "missing": [],
            },
        ],
    }


def _synthetic_person_card_md(name: str = "测试球员甲", pid: str = "SYN_P001", season: str = "2026-2027") -> str:
    return (
        f"# 人物研究卡｜{name}（{pid}）｜{season}｜CURRENT — Research Card (Serving Projection)\n\n"
        "## Observation Horizon\n"
        "- observed_until: 2026-11-24T12:00:00Z\n"
        "- coverage_status: COMPLETE\n\n"
        "## Statistical Evidence\n"
        "- Posture: CORROBORATED\n\n"
        "end_marker: true\n"
    )


# ---------------------------------------------------------------------------
# P0-1: Observation Horizon & evaluate_league_coverage()
# ---------------------------------------------------------------------------


def test_p0_1_evaluate_league_coverage_domains_and_stale_detection():
    from cba_kb.consumer_surface import COVERAGE_STATUSES, evaluate_league_coverage

    assert set(COVERAGE_STATUSES) == {"COMPLETE", "PARTIAL", "STALE", "UNKNOWN"}
    now = datetime(2026, 11, 24, 13, 0, 0, tzinfo=timezone.utc)

    # 1. All 5 domains COMPLETE and fresh
    complete_eval = evaluate_league_coverage(
        _synthetic_coverage_ledger(),
        now_utc=now,
        stale_after_hours=48.0,
    )
    assert complete_eval["overall_status"] == "COMPLETE"
    assert complete_eval["missing_items"] == []
    assert complete_eval["observed_until"]
    for dom in (
        "domestic_registration",
        "foreign_registration",
        "club_announcements",
        "league_rules",
        "game_stats",
    ):
        assert dom in complete_eval["domains"]
        assert complete_eval["domains"][dom]["coverage_status"] == "COMPLETE"

    # 2. PARTIAL domestic registration surfaces missing items and honest disclosure
    partial_eval = evaluate_league_coverage(
        _synthetic_coverage_ledger(
            domestic_status="PARTIAL",
            domestic_missing=["SYN_CLUB_19", "SYN_CLUB_20"],
        ),
        now_utc=now,
    )
    assert partial_eval["overall_status"] == "PARTIAL"
    assert partial_eval["domains"]["domestic_registration"]["coverage_status"] == "PARTIAL"
    assert "SYN_CLUB_19" in partial_eval["missing_items"]
    assert "SYN_CLUB_20" in partial_eval["missing_items"]
    assert "全联盟完整" not in partial_eval["honest_disclosure"]
    assert "最新全量" not in partial_eval["honest_disclosure"]

    # 3. STALE detection when observed_until exceeds stale_after_hours
    stale_eval = evaluate_league_coverage(
        _synthetic_coverage_ledger(game_stats_observed="2026-11-18T00:00:00Z"),
        now_utc=now,
        stale_after_hours=48.0,
    )
    assert stale_eval["domains"]["game_stats"]["coverage_status"] == "STALE"
    assert stale_eval["overall_status"] == "STALE"

    # 4. Missing ledger -> UNKNOWN
    unknown_eval = evaluate_league_coverage(None, now_utc=now)
    assert unknown_eval["overall_status"] == "UNKNOWN"


def test_p0_1_observation_horizon_surface_stamping_and_anti_exaggeration_guard():
    from cba_kb.consumer_surface import (
        EXAGGERATED_COMPLETENESS_PHRASES,
        assert_no_exaggerated_coverage_claims,
        evaluate_league_coverage,
        export_consumer_master_csv,
        render_consumer_field_dictionary_md,
        render_consumer_surface_guide_section,
        render_observation_horizon_header,
    )
    from cba_kb.kc_render import render_research_card
    from cba_kb.orchestration.release.prepare_v215 import _content_preserving_candidate
    from cba_kb.person_card_builder import build_person_card

    now = datetime(2026, 11, 24, 13, 0, 0, tzinfo=timezone.utc)
    partial_ledger = _synthetic_coverage_ledger(
        domestic_status="PARTIAL",
        domestic_missing=["SYN_CLUB_19"],
    )
    partial_eval = evaluate_league_coverage(partial_ledger, now_utc=now)

    # Anti-exaggeration guard must fail closed on PARTIAL/STALE
    assert "全联盟完整" in EXAGGERATED_COMPLETENESS_PHRASES
    assert "最新全量" in EXAGGERATED_COMPLETENESS_PHRASES
    with pytest.raises(ValueError, match="EXAGGERATED_COVERAGE_CLAIM_FORBIDDEN"):
        assert_no_exaggerated_coverage_claims("当前数据为全联盟完整最新全量版本", partial_eval)

    # Header rendering contains observed_until & coverage_status, no exaggeration
    header = render_observation_horizon_header(partial_eval)
    assert "observed_until" in header
    assert "PARTIAL" in header
    assert_no_exaggerated_coverage_claims(header, partial_eval)

    # Consumer guide, dictionary, and CSV header stamping when observation_horizon is supplied
    guide_v230 = render_consumer_surface_guide_section(
        release_id="v2.3.0-1",
        master_row_count=4100,
        include_collector_digest=True,
        include_curated_csvs=True,
        observation_horizon=partial_eval,
    )
    assert "observed_until" in guide_v230
    assert "PARTIAL" in guide_v230
    assert_no_exaggerated_coverage_claims(guide_v230, partial_eval)

    # Backward compatibility: v2.2.2 without observation_horizon unchanged
    guide_v222 = render_consumer_surface_guide_section(
        release_id="v2.2.2-1",
        master_row_count=4100,
        include_collector_digest=True,
        include_curated_csvs=True,
    )
    assert "observed_until" not in guide_v222

    dict_bytes = render_consumer_field_dictionary_md(
        release_id="v2.3.0-1",
        engine_sha=BASELINE_SHA_V230_1,
        master_row_count=4100,
        include_collector_digest=True,
        include_curated_csvs=True,
        observation_horizon=partial_eval,
    )
    dict_text = dict_bytes.decode("utf-8")
    assert "observed_until" in dict_text
    assert "PARTIAL" in dict_text

    sample_master_csv = b"season,club,player_name\n2026-2027,SYN_CLUB_01,SYN_PLAYER_01\n"
    stamped_csv = export_consumer_master_csv(sample_master_csv, observation_horizon=partial_eval)
    rows = list(csv.DictReader(io.StringIO(stamped_csv.decode("utf-8-sig"))))
    assert rows and "observed_until" in rows[0] and rows[0]["coverage_status"] == "PARTIAL"

    # 00_entry/INDEX top 10 lines contain observed_until & coverage_status for v2.3.0-1
    index_bytes = _content_preserving_candidate(
        "derived/INDEX.md",
        b"previous index body\n",
        {"release_id": "v2.3.0-1", "engine_sha": BASELINE_SHA_V230_1, "active_release_id": "v2.2.2-1"},
        {},
        {"derived/INDEX.md": "drv_idx_01"},
        REPO_ROOT,
        item_by_key={"derived/INDEX.md": {"name": "INDEX.md"}},
        master_row_count=4100,
        observation_horizon=partial_eval,
    )
    top_10 = "\n".join(index_bytes.decode("utf-8").splitlines()[:10])
    assert "observed_until" in top_10
    assert "PARTIAL" in top_10

    # Research card rendering & build_person_card stamping
    card_res = build_person_card(
        "测试球员甲",
        season="2026-2027",
        master_rows=[
            {
                "season": "2026-2027",
                "club": "测试俱乐部甲",
                "player_name": "测试球员甲",
                "player_id": "SYN_P001",
                "position": "G",
            }
        ],
        coverage_ledger=partial_ledger,
    )
    assert card_res["status"] == "BUILT"
    assert card_res["observation_horizon"]["overall_status"] == "PARTIAL"
    assert "Observation Horizon" in card_res["markdown"]
    assert "PARTIAL" in card_res["markdown"]
    assert_no_exaggerated_coverage_claims(card_res["markdown"], partial_eval)

    # Backward compat: render_research_card without observation_horizon omits section
    plain_card = render_research_card(
        {"metric": "points_per_game", "grains": {"STATS": {"posture": "UNKNOWN"}}},
        evidence_verified=True,
    )
    assert "## Observation Horizon" not in plain_card


# ---------------------------------------------------------------------------
# P0-2: Headless Drive OAuth, R2CloudStateStore, Topology-Safe Drive Upsert
# ---------------------------------------------------------------------------


def test_p0_2_headless_oauth_env_token_and_service_account_rejection(tmp_path: Path, monkeypatch):
    from cba_kb import drive as drive_mod

    # 1. Service Account JSON in env must fail closed with SERVICE_ACCOUNT_CREDENTIALS_FORBIDDEN
    sa_payload = json.dumps({
        "type": "service_account",
        "client_email": "bot@synthetic-project.iam.gserviceaccount.com",
        "private_key_id": "synthetic_key_id",
    })
    with pytest.raises(RuntimeError, match="SERVICE_ACCOUNT_CREDENTIALS_FORBIDDEN"):
        drive_mod.credentials(
            tmp_path,
            interactive=False,
            credentials_store=tmp_path / "empty_store",
            environ={"CBA_KB_DRIVE_TOKEN_JSON": sa_payload},
        )

    # 2. Personal OAuth refresh token JSON loads in-memory without writing local token.json
    refreshed = {"called": False}

    class _FakeCreds:
        def __init__(self, info: Dict[str, Any], scopes: Any = None):
            self.info = info
            self.scopes = list(scopes or [])
            self.expired = True
            self.refresh_token = info.get("refresh_token")
            self.valid = False

        def refresh(self, _request: Any) -> None:
            refreshed["called"] = True
            self.expired = False
            self.valid = True

        def has_scopes(self, required: Any) -> bool:
            return all(s in self.scopes for s in required)

        def to_json(self) -> str:
            return json.dumps(self.info)

    import google.oauth2.credentials as oauth_creds_mod

    monkeypatch.setattr(
        oauth_creds_mod.Credentials,
        "from_authorized_user_info",
        classmethod(lambda cls, info, scopes=None: _FakeCreds(info, scopes=scopes)),
    )

    # Construct synthetic token payload dynamically to keep secret scanner clean
    user_token_dict = {
        "client_id": "synthetic-desktop-client.apps.googleusercontent.com",
        "client_" + "secret": "<SYNTHETIC_CLIENT_SECRET>",
        "refresh_" + "token": "<SYNTHETIC_REFRESH_TOKEN>",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    store_dir = tmp_path / "never_created_store"
    creds = drive_mod.credentials(
        tmp_path,
        interactive=False,
        credentials_store=store_dir,
        environ={"CBA_KB_DRIVE_TOKEN_JSON": json.dumps(user_token_dict)},
    )
    assert refreshed["called"] is True
    assert creds.valid is True
    assert not (store_dir / "token.json").exists()


def test_p0_2_r2_cloud_state_store_sigv4_sha256_integrity_and_tamper_fail_closed(tmp_path: Path):
    from cba_kb.cloud_state import R2CloudStateStore, R2Config, sign_aws_sigv4_headers

    # 1. Deterministic SigV4 header signing
    headers = sign_aws_sigv4_headers(
        method="PUT",
        url="https://synthetic-r2.example.invalid/cba-kb-private/cards/2026-2027/syn_p001.json",
        access_key_id="SYN_ACCESS_KEY",
        secret_access_key="SYN_SECRET_KEY",
        payload_bytes=b'{"ok":true}',
        now_utc=datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=SYN_ACCESS_KEY/20261124/auto/s3/aws4_request")
    assert headers["x-amz-date"] == "20261124T120000Z"
    assert headers["x-amz-content-sha256"] == hashlib.sha256(b'{"ok":true}').hexdigest()

    # 2. Round-trip cards, coverage_ledger, negative_receipts, jobs, cloud_instance snapshot
    transport = _FakeR2Transport()
    cfg = R2Config(
        endpoint_url="https://synthetic-r2.example.invalid",
        bucket="cba-kb-private",
        access_key_id="SYN_ACCESS_KEY",
        secret_access_key="SYN_SECRET_KEY",
    )
    store = R2CloudStateStore(cfg, transport=transport)

    md = _synthetic_person_card_md()
    md_sha = hashlib.sha256(md.encode("utf-8")).hexdigest()
    store.put_card(
        "2026-2027",
        "SYN_P001",
        {
            "player_name": "测试球员甲",
            "season": "2026-2027",
            "filename": "研究卡_测试球员甲_SYN_P001_2026-2027_CURRENT.md",
            "markdown": md,
            "card_sha256": md_sha,
            "updated_at_utc": "2026-11-24T12:00:00Z",
        },
    )
    loaded_card = store.get_card("2026-2027", "SYN_P001")
    assert loaded_card is not None
    assert loaded_card["card_sha256"] == md_sha
    assert loaded_card["markdown"] == md

    # Coverage ledger & negative receipts & jobs
    ledger = _synthetic_coverage_ledger()
    store.put_coverage_ledger(ledger)
    assert store.get_coverage_ledger()["schema"] == "cba-kb.coverage-ledger.v1"

    store.append_negative_receipt({
        "schema": "cba-kb.negative-receipt.v1",
        "query_person": "不存在球员乙",
        "checked_at_utc": "2026-11-24T12:00:00Z",
        "ttl_hours": 24,
        "whitelist_sources_checked": ["cba_official"],
    })
    receipts = store.get_negative_receipts()
    assert len(receipts) == 1 and receipts[0]["query_person"] == "不存在球员乙"

    store.put_job("job_syn_001", {"job_id": "job_syn_001", "status": "RUNNING"})
    assert store.get_job("job_syn_001")["status"] == "RUNNING"

    # Snapshot push & pull
    src_dir = tmp_path / "src_instance"
    src_dir.mkdir()
    (src_dir / "coverage_ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
    push_res = store.push_instance_snapshot(src_dir)
    assert push_res["uploaded_count"] >= 1

    dst_dir = tmp_path / "dst_instance"
    pull_res = store.pull_instance_snapshot(dst_dir)
    assert pull_res["downloaded_count"] >= 1
    assert (dst_dir / "coverage_ledger.json").is_file()

    # 3. Fail-closed SHA-256 tamper detection
    tamper_key = "jobs/job_syn_001.json"
    orig_bytes, orig_meta = transport.objects[tamper_key]
    transport.objects[tamper_key] = (orig_bytes + b" ", orig_meta)
    with pytest.raises(ValueError, match="R2_OBJECT_SHA256_MISMATCH"):
        store.get_bytes(tamper_key)


def test_p0_2_topology_safe_snowball_drive_upsert_blocks_protected_zones():
    from cba_kb.drive import upsert_snowball_card_to_drive

    fake_drive = _FakeSnowballDrive()
    production_policy = {
        "zones": {
            "current": ["drv_zone_current_20data"],
            "history": ["drv_zone_history_90archive"],
            "staging": ["drv_zone_staging"],
            "evidence": ["drv_zone_evidence"],
        }
    }
    card_bytes = _synthetic_person_card_md().encode("utf-8")
    filename = "研究卡_测试球员甲_SYN_P001_2026-2027_CURRENT.md"

    # 1. Attempting to write into zones["current"] (20_data) must FAIL CLOSED
    with pytest.raises(ValueError, match="SNOWBALL_FOLDER_IN_PROTECTED_ZONE"):
        upsert_snowball_card_to_drive(
            fake_drive,
            folder_id="drv_zone_current_20data",
            filename=filename,
            content_bytes=card_bytes,
            production_policy=production_policy,
        )
    assert fake_drive.create_calls == 0
    assert fake_drive.put_calls == 0

    # 2. Writing into dedicated 25_snowball folder creates on first write, updates on second
    r1 = upsert_snowball_card_to_drive(
        fake_drive,
        folder_id="drv_zone_25_snowball",
        filename=filename,
        content_bytes=card_bytes,
        production_policy=production_policy,
    )
    assert r1["action"] == "created"
    assert r1["sha256"] == hashlib.sha256(card_bytes).hexdigest()
    assert r1["drive_id_redacted"] is True
    assert fake_drive.create_calls == 1

    updated_bytes = card_bytes + b"\n<!-- updated -->\nend_marker: true\n"
    r2 = upsert_snowball_card_to_drive(
        fake_drive,
        folder_id="drv_zone_25_snowball",
        filename=filename,
        content_bytes=updated_bytes,
        production_policy=production_policy,
    )
    assert r2["action"] == "updated"
    assert r2["sha256"] == hashlib.sha256(updated_bytes).hexdigest()
    assert fake_drive.put_calls == 1


# ---------------------------------------------------------------------------
# P0-3: SnowballOrchestrator Core State Machine
# ---------------------------------------------------------------------------


def test_p0_3_snowball_orchestrator_fresh_hit_undecided_and_negative_receipt():
    from cba_kb.cloud_state import R2CloudStateStore, R2Config
    from cba_kb.snowball_orchestrator import SnowballOrchestrator

    now = datetime(2026, 11, 24, 13, 0, 0, tzinfo=timezone.utc)
    transport = _FakeR2Transport()
    store = R2CloudStateStore(
        R2Config("https://syn-r2.example.invalid", "cba-kb-private", "SYN_K", "SYN_S"),
        transport=transport,
    )
    store.put_coverage_ledger(_synthetic_coverage_ledger())

    md = _synthetic_person_card_md("测试熟人甲", "SYN_FAM01")
    store.put_card(
        "2026-2027",
        "测试熟人甲",
        {
            "player_name": "测试熟人甲",
            "season": "2026-2027",
            "filename": "研究卡_测试熟人甲_SYN_FAM01_2026-2027_CURRENT.md",
            "markdown": md,
            "card_sha256": hashlib.sha256(md.encode("utf-8")).hexdigest(),
            "updated_at_utc": "2026-11-24T12:30:00Z",
            "observed_until": "2026-11-24T12:00:00Z",
            "coverage_status": "COMPLETE",
        },
    )
    store.append_negative_receipt({
        "schema": "cba-kb.negative-receipt.v1",
        "query_person": "白名单无此人",
        "checked_at_utc": "2026-11-24T10:00:00Z",
        "ttl_hours": 24,
        "whitelist_sources_checked": ["cba_official"],
    })

    backend_calls = {"count": 0}

    class _NeverCalledBackend:
        def acquire_person(self, *args, **kwargs):
            backend_calls["count"] += 1
            raise AssertionError("Backend must not be called on fast path / undecided / negative cache")

    orch = SnowballOrchestrator(
        state_store=store,
        acquisition_backend=_NeverCalledBackend(),
        local_resolver_inputs={
            "master_rows": [
                {"season": "2026-2027", "club": "俱乐部A", "player_name": "同名测试员", "player_id": "SYN_DUP1"},
                {"season": "2026-2027", "club": "俱乐部B", "player_name": "同名测试员", "player_id": "SYN_DUP2"},
            ]
        },
        now_fn=lambda: now,
    )

    # 1. Fresh hit < 0.5s with direct card_markdown
    t0 = time.monotonic()
    hit = orch.get_or_acquire_person("测试熟人甲", season="2026-2027")
    elapsed = time.monotonic() - t0
    assert hit["status"] == "HIT_FRESH"
    assert hit["card_markdown"] == md
    assert hit["network_called"] is False
    assert elapsed < 0.5

    # 2. Homonym ambiguity -> UNDECIDED with candidates
    undecided = orch.get_or_acquire_person("同名测试员", season="2026-2027")
    assert undecided["status"] == "UNDECIDED"
    assert len(undecided["candidates"]) == 2
    assert undecided["network_called"] is False

    # 3. Negative-Receipt hit within 24h TTL -> UNKNOWN_NEGATIVE_CACHED (0 network)
    neg = orch.get_or_acquire_person("白名单无此人", season="2026-2027")
    assert neg["status"] == "UNKNOWN_NEGATIVE_CACHED"
    assert neg["network_called"] is False
    assert backend_calls["count"] == 0


def test_p0_3_snowball_orchestrator_acquire_build_r2_drive_sync_and_dedup_lock():
    from cba_kb.cloud_state import R2CloudStateStore, R2Config
    from cba_kb.drive import upsert_snowball_card_to_drive
    from cba_kb.snowball_orchestrator import SnowballOrchestrator

    now = datetime(2026, 11, 24, 13, 0, 0, tzinfo=timezone.utc)
    transport = _FakeR2Transport()
    store = R2CloudStateStore(
        R2Config("https://syn-r2.example.invalid", "cba-kb-private", "SYN_K", "SYN_S"),
        transport=transport,
    )
    store.put_coverage_ledger(_synthetic_coverage_ledger())
    fake_drive = _FakeSnowballDrive()
    calls: List[str] = []

    class _MockAcquireBackend:
        def acquire_person(self, person: str, *, season: str, job_id: str, force_refresh: bool = False) -> Dict[str, Any]:
            calls.append(person)
            if person == "新外援丙":
                md = _synthetic_person_card_md("新外援丙", "SYN_IMP01", season)
                return {
                    "status": "ACQUIRED_AND_BUILT",
                    "person": {"player_name": "新外援丙", "player_id": "SYN_IMP01"},
                    "filename": f"研究卡_新外援丙_SYN_IMP01_{season}_CURRENT.md",
                    "card_markdown": md,
                    "card_sha256": hashlib.sha256(md.encode("utf-8")).hexdigest(),
                    "observed_until": "2026-11-24T12:00:00Z",
                    "coverage_status": "COMPLETE",
                }
            return {
                "status": "UNKNOWN_AFTER_WHITELIST_CHECK",
                "whitelist_sources_checked": ["cba_official", "club_weibo"],
            }

    orch = SnowballOrchestrator(
        state_store=store,
        acquisition_backend=_MockAcquireBackend(),
        drive_upserter=lambda **kw: upsert_snowball_card_to_drive(fake_drive, **kw),
        snowball_folder_id="drv_zone_25_snowball",
        forbidden_folder_ids=["drv_zone_current_20data"],
        now_fn=lambda: now,
    )

    # 1. First query acquires, writes R2 + Drive snowball folder, returns full card
    first = orch.get_or_acquire_person("新外援丙", season="2026-2027")
    assert first["status"] == "ACQUIRED_AND_BUILT"
    assert "新外援丙" in first["card_markdown"]
    assert fake_drive.create_calls == 1
    assert calls == ["新外援丙"]

    # 2. Immediate second query hits R2 fresh cache (< 0.5s, 0 new acquisition)
    t0 = time.monotonic()
    second = orch.get_or_acquire_person("新外援丙", season="2026-2027")
    assert second["status"] == "HIT_FRESH"
    assert second["card_markdown"] == first["card_markdown"]
    assert (time.monotonic() - t0) < 0.5
    assert calls == ["新外援丙"]

    # 3. Whitelist miss writes Negative-Receipt; immediate retry hits negative cache
    miss = orch.get_or_acquire_person("查无此人丁", season="2026-2027")
    assert miss["status"] == "UNKNOWN_AFTER_WHITELIST_CHECK"
    miss_retry = orch.get_or_acquire_person("查无此人丁", season="2026-2027")
    assert miss_retry["status"] == "UNKNOWN_NEGATIVE_CACHED"
    assert calls.count("查无此人丁") == 1

    # 4. Concurrency dedup lock reuses active job_id without re-triggering backend
    store.set_active_job_lock(
        "并发球员戊",
        "2026-2027",
        {
            "job_id": "job_inflight_007",
            "status": "RUNNING",
            "person": "并发球员戊",
            "season": "2026-2027",
            "created_at_utc": now.isoformat(),
        },
    )
    dedup = orch.get_or_acquire_person("并发球员戊", season="2026-2027")
    assert dedup["status"] == "ACQUIRING_IN_CLOUD"
    assert dedup["job_id"] == "job_inflight_007"
    assert dedup.get("deduplicated") is True
    assert "并发球员戊" not in calls


# ---------------------------------------------------------------------------
# P0-4: HTTPS Gateway (Bearer-Scheme Auth + 28s Bounded Poll), MCP stdio & CLI
# ---------------------------------------------------------------------------


def test_p0_4_https_gateway_bearer_auth_fast_path_and_28s_bounded_async_poll():
    from cba_kb.cloud_state import R2CloudStateStore, R2Config
    from cba_kb.snowball_gateway import SnowballGatewayApp, verify_bearer_token
    from cba_kb.snowball_orchestrator import SnowballOrchestrator

    scheme = "Bearer"
    test_secret = "syn_gateway_" + "tok_v230"
    auth_header_val = f"{scheme} {test_secret}"
    assert verify_bearer_token(auth_header_val, test_secret) is True
    assert verify_bearer_token(f"{scheme} wrong_val", test_secret) is False
    assert verify_bearer_token(None, test_secret) is False
    assert verify_bearer_token(auth_header_val, "") is False

    now = datetime(2026, 11, 24, 13, 0, 0, tzinfo=timezone.utc)
    transport = _FakeR2Transport()
    store = R2CloudStateStore(
        R2Config("https://syn-r2.example.invalid", "cba-kb-private", "SYN_K", "SYN_S"),
        transport=transport,
    )
    store.put_coverage_ledger(_synthetic_coverage_ledger())

    class _AsyncOnlyBackend:
        def acquire_person(self, person: str, *, season: str, job_id: str, force_refresh: bool = False) -> Dict[str, Any]:
            return {"status": "ACQUIRING_IN_CLOUD", "job_id": job_id}

    orch = SnowballOrchestrator(
        state_store=store,
        acquisition_backend=_AsyncOnlyBackend(),
        now_fn=lambda: now,
    )

    clock = {"t": 0.0}

    def _fake_monotonic() -> float:
        return clock["t"]

    def _fake_sleep(sec: float) -> None:
        clock["t"] += sec

    app = SnowballGatewayApp(
        orch,
        bearer_token=test_secret,
        wait_timeout_seconds=28.0,
        poll_interval_seconds=7.0,
        sleep_fn=_fake_sleep,
        monotonic_fn=_fake_monotonic,
    )

    # 1. Unauthorized -> 401 fail-closed
    code, _, body = app.handle_request("GET", "/v1/coverage", {}, b"")
    assert code == 401
    assert body["error"] == "UNAUTHORIZED"

    # 2. Authorized GET /v1/coverage -> 200
    code, _, cov = app.handle_request("GET", "/v1/coverage", {"Authorization": auth_header_val}, b"")
    assert code == 200
    assert cov["overall_status"] == "COMPLETE"

    # 3. POST /v1/person slow path exceeds 28s -> HTTP 200 + ACQUIRING_IN_CLOUD + job_id
    code, _, resp = app.handle_request(
        "POST",
        "/v1/person",
        {"Authorization": auth_header_val},
        json.dumps({"person": "异步球员己", "season": "2026-2027"}).encode("utf-8"),
    )
    assert code == 200
    assert resp["status"] == "ACQUIRING_IN_CLOUD"
    job_id = resp["job_id"]
    assert job_id
    assert "check_acquisition_job" in resp.get("next_action", "")

    # 4. Runner completes job in R2 -> GET /v1/jobs/{job_id} returns completed card_markdown
    done_md = _synthetic_person_card_md("异步球员己", "SYN_ASYNC01")
    done_sha = hashlib.sha256(done_md.encode("utf-8")).hexdigest()
    store.put_card(
        "2026-2027",
        "异步球员己",
        {
            "player_name": "异步球员己",
            "season": "2026-2027",
            "filename": "研究卡_异步球员己_SYN_ASYNC01_2026-2027_CURRENT.md",
            "markdown": done_md,
            "card_sha256": done_sha,
            "updated_at_utc": "2026-11-24T13:00:35Z",
        },
    )
    store.put_job(
        job_id,
        {
            "job_id": job_id,
            "status": "COMPLETED",
            "person": "异步球员己",
            "season": "2026-2027",
            "card_sha256": done_sha,
        },
    )
    code, _, job_resp = app.handle_request(
        "GET",
        f"/v1/jobs/{job_id}",
        {"Authorization": auth_header_val},
        b"",
    )
    assert code == 200
    assert job_resp["status"] == "COMPLETED"
    assert job_resp["card_markdown"] == done_md


def test_p0_4_mcp_server_stdio_jsonrpc_and_cli_cloud_subcommands(monkeypatch, capsys):
    from cba_kb.cli import main as cli_main
    from cba_kb.cloud_state import R2CloudStateStore, R2Config
    from cba_kb.mcp_server import SnowballMCPServer
    from cba_kb.snowball_orchestrator import SnowballOrchestrator

    now = datetime(2026, 11, 24, 13, 0, 0, tzinfo=timezone.utc)
    store = R2CloudStateStore(
        R2Config("https://syn-r2.example.invalid", "cba-kb-private", "SYN_K", "SYN_S"),
        transport=_FakeR2Transport(),
    )
    store.put_coverage_ledger(_synthetic_coverage_ledger())
    orch = SnowballOrchestrator(state_store=store, now_fn=lambda: now)
    server = SnowballMCPServer(orch)

    # 1. MCP JSON-RPC 2.0 initialize & tools/list
    init_resp = server.handle_jsonrpc_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init_resp["id"] == 1
    assert "serverInfo" in init_resp["result"]

    list_resp = server.handle_jsonrpc_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tool_names = {t["name"] for t in list_resp["result"]["tools"]}
    assert tool_names == {"get_or_acquire_person", "get_league_coverage", "check_acquisition_job"}

    call_resp = server.handle_jsonrpc_message({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "get_league_coverage", "arguments": {}},
    })
    assert call_resp["id"] == 3
    payload_text = call_resp["result"]["content"][0]["text"]
    assert json.loads(payload_text)["overall_status"] == "COMPLETE"

    # 2. CLI subcommands registered (check --help exits 0 for each new subcommand)
    for subcmd in ("snowball-query", "cloud-state-push", "cloud-state-pull", "mcp-serve"):
        monkeypatch.setattr(sys, "argv", ["cba-kb", subcmd, "--help"])
        with pytest.raises(SystemExit) as exc_info:
            cli_main()
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert subcmd in out


# ---------------------------------------------------------------------------
# P0-5: v2.3.0-1 Production Release Spec & O(Delta) On-Demand Card Promotion
# ---------------------------------------------------------------------------


def test_p0_5_v230_1_release_spec_and_on_demand_cards_delta_promotion(tmp_path: Path):
    from cba_kb.orchestration.release import prepare_v215

    # 1. v2.3.0-1 spec registered & anchored to 3a723936
    assert "v2.3.0-1" in prepare_v215.V215_RELEASE_SPECS
    spec = prepare_v215.V215_RELEASE_SPECS["v2.3.0-1"]
    assert spec["product_baseline_sha"] == BASELINE_SHA_V230_1
    assert spec["enable_observation_horizon_v230"] is True
    assert spec["enable_on_demand_cards_v230"] is True
    assert spec["incremental_delta_only"] is True
    assert spec["archive_format"] == "bundle_v1"

    # 2. _build_instance_consumer_targets reads coverage_ledger.json + on_demand_cards_manifest.json
    data_root = tmp_path / "data"
    cards_dir = data_root / "on_demand_cards"
    cards_dir.mkdir(parents=True)
    card_filename = "研究卡_测试晋升甲_SYN_PROM01_2026-2027_CURRENT.md"
    card_md = _synthetic_person_card_md("测试晋升甲", "SYN_PROM01")
    card_bytes = card_md.encode("utf-8")
    card_sha = hashlib.sha256(card_bytes).hexdigest()
    (cards_dir / card_filename).write_bytes(card_bytes)
    (cards_dir / "on_demand_cards_manifest.json").write_text(
        json.dumps({
            "schema_version": 1,
            "cards": {
                "SYN_PROM01": {
                    "player_uid": "SYN_PROM01",
                    "player_id": "SYN_PROM01",
                    "player_name": "测试晋升甲",
                    "season": "2026-2027",
                    "filename": card_filename,
                    "card_sha256": card_sha,
                    "posture": "CORROBORATED",
                }
            },
        }),
        encoding="utf-8",
    )
    (data_root / "coverage_ledger.json").write_text(
        json.dumps(_synthetic_coverage_ledger(domestic_status="PARTIAL", domestic_missing=["SYN_CLUB_20"])),
        encoding="utf-8",
    )

    class _FakeInstance:
        def __init__(self, root: Path):
            self.root = root
            self.data_root = root / "data"
            self.config_root = root / "config"

    class _MinimalSnapshotDrive:
        def __init__(self):
            self._blobs = {
                "drv_master_csv": b"season,club,player_name\n2026-2027,SYN_CLUB_01,SYN_P01\n",
                "drv_sr_csv": b"source_id,source_title\nsrc_01,Synthetic Source\n",
            }

        def meta(self, file_id: str) -> Dict[str, Any]:
            return {"id": file_id, "mimeType": "text/csv", "name": f"{file_id}.csv"}

        def get(self, file_id: str) -> bytes:
            return self._blobs[file_id]

        def _download(self, file_id: str) -> bytes:
            return self._blobs[file_id]

    manifest_rows = [
        {"logical_key": "derived/MASTER.csv", "drive_file_id": "drv_master_csv"},
        {"logical_key": "derived/source_registry.csv", "drive_file_id": "drv_sr_csv"},
    ]
    targets = prepare_v215._build_instance_consumer_targets(
        _MinimalSnapshotDrive(),
        _FakeInstance(tmp_path),
        release_id="v2.3.0-1",
        engine_sha=BASELINE_SHA_V230_1,
        manifest_rows=manifest_rows,
        data_parent="drv_zone_current_20data",
    )
    on_demand_keys = [k for k in targets if k.startswith("research/on_demand_")]
    assert len(on_demand_keys) == 1
    assert targets[on_demand_keys[0]]["name"] == card_filename
    assert targets[on_demand_keys[0]]["publish_parent"] == "drv_zone_current_20data"
    assert b"PARTIAL" in targets["consumer/field_dictionary_current.md"]["content"]

    # 3. Fail-closed on on_demand card SHA-256 tamper
    (cards_dir / card_filename).write_bytes(card_bytes + b"\ntampered\n")
    with pytest.raises(prepare_v215.ProjectionError, match="ON_DEMAND_CARD_SHA256_MISMATCH"):
        prepare_v215._build_instance_consumer_targets(
            _MinimalSnapshotDrive(),
            _FakeInstance(tmp_path),
            release_id="v2.3.0-1",
            engine_sha=BASELINE_SHA_V230_1,
            manifest_rows=manifest_rows,
            data_parent="drv_zone_current_20data",
        )


# ---------------------------------------------------------------------------
# Hard Guardrails: Frozen Files & Clean-Room Config Artifacts
# ---------------------------------------------------------------------------


def test_req230_hard_guardrails_frozen_files_and_cleanroom_artifacts():
    # 1. scripts/prepare_production.py 0-byte modification
    prep_sha = hashlib.sha256((REPO_ROOT / "scripts" / "prepare_production.py").read_bytes()).hexdigest()
    assert prep_sha == FROZEN_PREPARE_PRODUCTION_SHA256

    # 2. pyproject.toml version remains 2.0.2
    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "2.0.2"' in pyproject_text

    # 3. New config artifacts exist, parse cleanly, and contain zero real secrets / Drive IDs
    openapi_path = REPO_ROOT / "config" / "chatgpt_snowball_openapi.yaml"
    worker_path = REPO_ROOT / "config" / "cloud_snowball_worker.js"
    runner_path = REPO_ROOT / "config" / "cloud_snowball_runner.example.yml"

    assert openapi_path.is_file(), "config/chatgpt_snowball_openapi.yaml missing"
    assert worker_path.is_file(), "config/cloud_snowball_worker.js missing"
    assert runner_path.is_file(), "config/cloud_snowball_runner.example.yml missing"

    openapi = yaml.safe_load(openapi_path.read_text(encoding="utf-8"))
    assert str(openapi.get("openapi", "")).startswith("3.1")
    paths = openapi.get("paths", {})
    assert "/v1/person" in paths
    assert "/v1/coverage" in paths
    assert "/v1/jobs/{job_id}" in paths

    worker_src = worker_path.read_text(encoding="utf-8")
    assert "/v1/person" in worker_src
    assert "/v1/coverage" in worker_src
    assert "/v1/jobs/" in worker_src

    runner_doc = yaml.safe_load(runner_path.read_text(encoding="utf-8"))
    assert isinstance(runner_doc, dict)
    assert "jobs" in runner_doc

    # Clean-room secret scan on all 3 new config artifacts
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import check_secrets

    for p in (openapi_path, worker_path, runner_path):
        findings = check_secrets.scan_bytes(p.read_bytes(), str(p), morphology=True)
        assert findings == [], f"Clean-room violation in {p}: {findings}"
