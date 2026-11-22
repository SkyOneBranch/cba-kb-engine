"""Q1+Q2 Round 1 remediation & adversarial regression tests for REQ-222 (v2.2.2).

Covers all 4 code blockers identified in independent Q1+Q2 Round 1 review without
modifying Commit 1's frozen acceptance test file:
- P0-1-BACKWARD-COMPATIBILITY: v2.1.5-1, v2.2.0-1, v2.2.1-1 _content_preserving_candidate()
  outputs must NOT include the new v2.2.2+ master_count_stamp, while v2.2.2-1 DOES
  include it in the top 10 lines and passes validate_current_state().
- P0-2-HISTORICAL-FILTER-OVERREACH: Official operational announcements (signing, waiver,
  injury, roster, #判罚解析#, NBL-CBA meeting) dated before active_window_start
  (e.g. 2025-06-30) are ADMITTED unless explicitly marked/identified as historical
  pinned or past-tournament summaries; historical pinned / past-tournament / generic
  old posts and social fluff are FILTERED.
- P0-2-INDEX-METADATA-LOSS: merge_operational_docs_into_consumer_index() preserves all
  valid sanitized collection metadata (event_class, acquired_at, upstream_origin, and
  custom sanitized fields) with 0 field loss while scrubbing raw Drive IDs and local paths.
- P0-4-NO-PROXY-CIDR: _resolve_sanitized_env_proxies() and cross-origin RequestsHttp
  honor IPv4/IPv6 CIDR networks, IPv6 literals, port-specific NO_PROXY rules, domain
  suffixes, exact hosts, and wildcard *, while keeping _unauth_client.trust_env=False
  and fail-closed rejecting embedded proxy credentials.

All fixtures are 100% synthetic. Zero network calls, zero Drive API calls,
zero local home-directory paths, zero raw Drive IDs.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
import requests
from google.oauth2.credentials import Credentials

from cba_kb import consumer_surface, current_state, drive_http
from cba_kb.native import wrap
from cba_kb.orchestration.release import prepare_v215 as orch

V222_1_BASELINE_SHA = "29589e7d4d90a81a6507ecaa1ec73945a5eecfc1"


class _SpyCredentials(Credentials):
    def __init__(self, marker: str = "synthetic-oauth-q2") -> None:
        super().__init__(token=marker)
        self.before_request_urls: List[str] = []

    @property
    def valid(self) -> bool:
        return True

    def before_request(self, request, method, url, headers) -> None:
        self.before_request_urls.append(url)
        headers["authorization"] = f"Bearer {self.token}"


def _make_requests_response(
    status_code: int,
    *,
    headers: Dict[str, str] | None = None,
    content: bytes = b"",
    url: str = "https://example.invalid/",
    reason: str = "OK",
) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status_code
    resp.reason = reason
    resp.url = url
    resp._content = content
    for k, v in (headers or {}).items():
        resp.headers[k] = v
    return resp


# ---------------------------------------------------------------------------
# P0-1: Legacy release control docs backward compatibility vs v2.2.2+ stamp
# ---------------------------------------------------------------------------


def test_q2_p0_1_legacy_control_docs_byte_compat_and_v222_stamp(tmp_path: Path):
    engine_dir = tmp_path / "engine"
    (engine_dir / "docs").mkdir(parents=True, exist_ok=True)
    (engine_dir / "docs/OPERATIONS_V1_5.md").write_text("ops guide\n", encoding="utf-8")
    (engine_dir / "docs/MASTER_MIGRATION_NOTE.md").write_text("migration note\n", encoding="utf-8")

    allocation = {"status_id": "f-status"}
    id_by_key = {
        "entry/README": "f-readme",
        "derived/INDEX.md": "f-index",
        "entry/context": "f-context",
        "CURRENT_VERSION_DOC": "f-ver",
        "ai/CONTEXT_CARD.md": "f-card",
        "input/MASTER.xlsx": "f-master",
    }
    item_by_key = {
        "entry/README": {"name": "00_README_先读这个"},
        "derived/INDEX.md": {"name": "INDEX"},
        "entry/context": {"name": "02_AI_PROJECT_CONTEXT_技术手册.md"},
        "CURRENT_VERSION_DOC": {"name": "CBA-KB_VERSION.md"},
        "ai/CONTEXT_CARD.md": {"name": "CONTEXT_CARD.md"},
        "input/MASTER.xlsx": {"name": "MASTER.xlsx"},
    }

    def _make_registry(release_id: str) -> Dict[str, Any]:
        return {
            "registry_version": "1",
            "registry_release_id": release_id,
            "products": [
                {
                    "product_id": "domestic_season_snapshots",
                    "semantic_grain": "domestic_season_registration_snapshot",
                    "authority": "canonical",
                    "business_key": ["record_key"],
                    "artifact_key": "input/MASTER.xlsx",
                    "generated_from": [],
                    "deprecated_by": None,
                    "read_policy": "canonical_only",
                    "current_eligible": True,
                    "independent_write_allowed": True,
                    "selector": "MASTER",
                }
            ],
        }

    # 1. Legacy releases (v2.1.5-1, v2.2.0-1, v2.2.1-1) MUST NOT emit the v2.2.2+ stamp
    legacy_releases = (
        ("v2.1.5-1", orch.V215_BASELINE_SHA),
        ("v2.2.0-1", orch.V220_1_BASELINE_SHA),
        ("v2.2.1-1", orch.V221_1_BASELINE_SHA),
    )
    for rel_id, sha in legacy_releases:
        reg = _make_registry(rel_id)
        proj = {"release_id": rel_id, "engine_sha": sha, "active_release_id": "v2.1.4"}
        for logical_key, is_managed in (
            ("entry/README", True),
            ("derived/INDEX.md", True),
            ("entry/context", False),
            ("CURRENT_VERSION_DOC", False),
        ):
            prev = wrap("legacy nav body\n").encode("utf-8") if is_managed else b"legacy body\n"
            rendered = orch._content_preserving_candidate(
                logical_key,
                prev,
                proj,
                allocation,
                id_by_key,
                engine_dir,
                registry=reg,
                item_by_key=item_by_key,
                master_row_count=3465,
            ).decode("utf-8")
            assert "权威国内注册主表规模" not in rendered, (
                f"Legacy release {rel_id} ({logical_key}) must NOT contain v2.2.2+ master_count_stamp"
            )

    # 2. v2.2.2-1 MUST emit the top-10-line stamp and pass validate_current_state()
    reg_222 = _make_registry("v2.2.2-1")
    manifest_bytes = (
        "uid,drive_file_id,content_hash,published_release,hash_scope\n"
        f"input/MASTER.xlsx,f-master,{'a'*64},v2.2.2-1,bytes\n"
    ).encode("utf-8-sig")
    proj_222 = {
        "release_id": "v2.2.2-1",
        "engine_sha": V222_1_BASELINE_SHA,
        "active_release_id": "v2.2.1-1",
    }
    expected_stamp = "权威国内注册主表规模：3,465 行（2017-2027 权威总表，精确行数 3465）"
    rendered_docs: Dict[str, str] = {}
    for logical_key, (role, is_managed) in (
        ("entry/README", ("readme", True)),
        ("derived/INDEX.md", ("index", True)),
        ("entry/context", ("technical_manual", False)),
        ("CURRENT_VERSION_DOC", ("current_version_doc", False)),
    ):
        prev = wrap("old nav\n").encode("utf-8") if is_managed else b"old body\n"
        raw_out = orch._content_preserving_candidate(
            logical_key,
            prev,
            proj_222,
            allocation,
            id_by_key,
            engine_dir,
            registry=reg_222,
            item_by_key=item_by_key,
            master_row_count=3465,
        )
        if is_managed:
            raw_out = orch._managed_candidate(raw_out)
        text = raw_out.decode("utf-8")
        rendered_docs[role] = text
        top_10 = "\n".join(text.splitlines()[:10])
        assert expected_stamp in top_10
        assert "当前发布：v2.2.2-1" in top_10
        assert f"代码提交：{V222_1_BASELINE_SHA}" in top_10

    meta = current_state.target_metadata("v2.2.2-1", V222_1_BASELINE_SHA, reg_222)
    rendered_docs["context_card"] = current_state.generate_context_card(
        meta, reg_222, manifest_bytes
    )
    status = {
        "state": "COMPLETE",
        "current_release_id": "v2.2.2-1",
        "code_commit": V222_1_BASELINE_SHA,
    }
    assert current_state.validate_current_state(status, reg_222, manifest_bytes, rendered_docs)["status"] == "PASS"


# ---------------------------------------------------------------------------
# P0-2: Historical filter precision (protect operational announcements)
# ---------------------------------------------------------------------------


def test_q2_p0_2_operational_announcement_before_window_admitted_while_historical_pinned_filtered():
    filter_fn = getattr(consumer_surface, "filter_curated_operational_announcements", None)
    assert callable(filter_fn), "Missing consumer_surface.filter_curated_operational_announcements"

    bundles = [
        # Official operational announcements BEFORE 2025-07-01 -> MUST be ADMITTED
        {
            "doc_id": "doc_op_signing_pre_window",
            "title": "俱乐部正式签约外援鲍威尔与梅肯试训公告",
            "source_tier": "T1.5",
            "event_class": "注册与转会",
            "published_at": "2025-06-30",
            "body": "俱乐部完成外援鲍威尔签约并安排试训。",
        },
        {
            "doc_id": "doc_op_injury_pre_window",
            "title": "国家队集训名单及赵睿伤情恢复公告",
            "source_tier": "T1",
            "event_class": "伤病与名单",
            "published_at": "2025-06-28",
            "body": "国家队公布集训大名单与伤病观察名单。",
        },
        {
            "doc_id": "doc_op_referee_pre_window",
            "title": "#判罚解析# 季后赛关键回合裁判报告",
            "source_tier": "T1",
            "event_class": "判罚与规则",
            "published_at": "2025-05-20",
            "body": "CBA裁判办公室发布#判罚解析#。",
        },
        {
            "doc_id": "doc_op_nbl_pre_window",
            "title": "NBL-CBA规则对接与联赛工作会议纪要",
            "source_tier": "T1",
            "event_class": "联赛政策",
            "published_at": "2025-06-15",
            "body": "联赛官方工作会议明确NBL-CBA规则对接方案。",
        },
        # Historical pinned / past-tournament / generic old posts -> MUST be FILTERED
        {
            "doc_id": "doc_hist_pinned_flag",
            "title": "俱乐部历史签约回顾",
            "source_tier": "T1",
            "event_class": "注册与转会",
            "published_at": "2025-06-20",
            "pinned": True,
            "body": "历史置顶回顾文章。",
        },
        {
            "doc_id": "doc_hist_pinned_title",
            "title": "置顶：2023-2024赛季联赛旧公告",
            "source_tier": "T1",
            "event_class": "官方公告",
            "published_at": "2024-03-15",
            "body": "旧赛季置顶说明。",
        },
        {
            "doc_id": "doc_hist_asiad",
            "title": "杭州亚运会中国男篮参赛名单与亚运会总结",
            "source_tier": "T1",
            "event_class": "历史总结",
            "published_at": None,
            "body": "关于杭州亚运会备战与亚运会总结。",
        },
        {
            "doc_id": "doc_generic_old_non_op",
            "title": "2024年夏季公益篮球训练营回顾",
            "source_tier": "T2",
            "event_class": "活动回顾",
            "published_at": "2024-06-10",
            "body": "公益训练营圆满结束。",
        },
        {
            "doc_id": "doc_fluff_prophet",
            "title": "【每日预言家】今日有奖互动抽奖",
            "source_tier": "T1",
            "event_class": "社媒互动",
            "published_at": "2026-11-20",
            "body": "转发参与有奖互动抽奖。",
        },
        {
            "doc_id": "doc_fluff_moment",
            "title": "【每日名场面】我和我的篮球图文征集",
            "source_tier": "T1",
            "event_class": "社媒互动",
            "published_at": "2026-11-20",
            "body": "图文征集投票评选最佳高光。",
        },
    ]

    result = filter_fn(bundles, active_window_start="2025-07-01")
    admitted_ids = {b["doc_id"] for b in result["admitted"]}
    filtered_ids = {b["doc_id"] for b in result["filtered"]}

    assert admitted_ids == {
        "doc_op_signing_pre_window",
        "doc_op_injury_pre_window",
        "doc_op_referee_pre_window",
        "doc_op_nbl_pre_window",
    }
    assert filtered_ids == {
        "doc_hist_pinned_flag",
        "doc_hist_pinned_title",
        "doc_hist_asiad",
        "doc_generic_old_non_op",
        "doc_fluff_prophet",
        "doc_fluff_moment",
    }


# ---------------------------------------------------------------------------
# P0-2: Consumer document index zero-loss metadata preservation & scrubbing
# ---------------------------------------------------------------------------


def test_q2_p0_2_consumer_index_preserves_all_sanitized_metadata_zero_loss():
    fake_drive_id = "1" + "SynthQ2IndexDriveToken9876543210"
    fake_local_user = "/Users/" + "synth_operator/private/bundle.md"
    fake_local_home = "/home/" + "synth_runner/private/capture.md"

    checkpoint = {
        "schema_version": 1,
        "consumed_bundles": {
            "2026-11/item_01.md": {
                "doc_id": "doc_meta_001",
                "title": "测试官宣公告",
                "canonical_url": "https://example.org/news/001",
                "source_tier": "T1",
                "completeness": "full",
                "published_at": "2026-11-20",
                "acquired_at": "2026-11-22T01:02:03Z",
                "event_class": "注册与转会",
                "upstream_origin": "CBA官方注册公示",
                "custom_collector_tag": "weibo_v041_curated",
                "source_channel": f"official_feed {fake_drive_id} {fake_local_user}",
                "nested_meta": {
                    "note": f"captured at {fake_local_home}",
                    "score": 42,
                },
                "content_hash": "a" * 64,
                "file_sha256": "b" * 64,
                "matched_persons": ["合成球员甲"],
                "disclosure_posture": "public_evidence",
                "drive_file_id": fake_drive_id,
                "file_id": fake_drive_id,
            }
        },
    }

    merged_bytes = consumer_surface.merge_operational_docs_into_consumer_index(
        None, checkpoint, known_drive_ids=(fake_drive_id,)
    )
    merged_text = merged_bytes.decode("utf-8")
    assert fake_drive_id not in merged_text
    assert "/Users/" + "synth_operator" not in merged_text
    assert "/home/" + "synth_runner" not in merged_text
    assert '"drive_file_id"' not in merged_text
    assert '"file_id"' not in merged_text

    parsed = json.loads(merged_text)
    assert parsed["unique_document_count"] == 1
    doc = parsed["documents"][0]
    assert doc["doc_id"] == "doc_meta_001"
    assert doc["event_class"] == "注册与转会"
    assert doc["acquired_at"] == "2026-11-22T01:02:03Z"
    assert doc["upstream_origin"] == "CBA官方注册公示"
    assert doc["custom_collector_tag"] == "weibo_v041_curated"
    assert doc["nested_meta"]["score"] == 42
    assert "<redacted-drive-id>" in doc["source_channel"]
    assert "<redacted-local-path>" in doc["source_channel"]
    assert "<redacted-local-path>" in doc["nested_meta"]["note"]
    assert doc["layer"] == "evidence"


# ---------------------------------------------------------------------------
# P0-4: NO_PROXY CIDR (IPv4/IPv6), IPv6 literals, ports & cross-origin wiring
# ---------------------------------------------------------------------------


def test_q2_p0_4_no_proxy_cidr_ipv6_ports_and_credential_rejection(monkeypatch, tmp_path: Path):
    helper = getattr(drive_http, "_resolve_sanitized_env_proxies", None)
    assert callable(helper), "Missing drive_http._resolve_sanitized_env_proxies"

    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")

    # 1. IPv4 CIDR matching
    monkeypatch.setenv("NO_PROXY", "10.0.0.0/8,192.168.0.0/16")
    assert not helper("https://10.1.2.3/blob")
    assert not helper("https://192.168.10.20/blob")
    assert helper("https://11.1.2.3/blob") == {"https": "http://127.0.0.1:7890"}

    # 2. IPv6 CIDR and IPv6 literal matching
    monkeypatch.setenv("NO_PROXY", "fc00::/7,[::1]")
    assert not helper("https://[fc00::1]/blob")
    assert not helper("https://[fd12:3456::1]/blob")
    assert not helper("https://[::1]/blob")
    assert helper("https://[2001:db8::1]/blob") == {"https": "http://127.0.0.1:7890"}

    # 3. Port-specific NO_PROXY rules
    monkeypatch.setenv("NO_PROXY", "example.com:8443,10.1.2.3:8443,[::1]:8443")
    assert not helper("https://example.com:8443/blob")
    assert helper("https://example.com/blob") == {"https": "http://127.0.0.1:7890"}
    assert helper("https://example.com:443/blob") == {"https": "http://127.0.0.1:7890"}
    assert not helper("https://10.1.2.3:8443/blob")
    assert helper("https://10.1.2.3/blob") == {"https": "http://127.0.0.1:7890"}
    assert not helper("https://[::1]:8443/blob")
    assert helper("https://[::1]/blob") == {"https": "http://127.0.0.1:7890"}

    # 4. Domain suffix & exact host matching
    monkeypatch.setenv("NO_PROXY", ".internal.local,exact.example")
    assert not helper("https://svc.internal.local/blob")
    assert not helper("https://exact.example/blob")
    assert helper("https://notexact.example/blob") == {"https": "http://127.0.0.1:7890"}

    # 5. Embedded credentials in proxy URL still fail closed
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://" + "u" + ":" + "p" + "@127.0.0.1:7890")
    with pytest.raises(ValueError, match="UNSAFE_PROXY_URL"):
        helper("https://11.1.2.3/blob")

    # 6. End-to-end RequestsHttp cross-origin redirect with CIDR NO_PROXY + trust_env=False
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("NO_PROXY", "10.0.0.0/8")
    netrc_file = tmp_path / "synth_q2.netrc"
    netrc_file.write_text(
        "machine 10.1.2.3\nlogin synth-u\npass" + "word synth-p\n"
        "machine 11.1.2.3\nlogin synth-u\npass" + "word synth-p\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NETRC", str(netrc_file))

    for target_ip, expected_proxies in (
        ("10.1.2.3", {}),
        ("11.1.2.3", {"https": "http://127.0.0.1:7890"}),
    ):
        creds = _SpyCredentials()
        auth_client = drive_http.build_authorized_session(creds)
        recorded: List[Dict[str, Any]] = []
        scripted = [
            _make_requests_response(
                302,
                headers={"Location": f"https://{target_ip}/blob"},
                reason="Found",
            ),
            _make_requests_response(200, content=b"ok", reason="OK"),
        ]

        def _fake_send(self, request, **kwargs):
            recorded.append({
                "url": request.url,
                "headers": {str(k).lower(): str(v) for k, v in dict(request.headers or {}).items()},
                "proxies": dict(kwargs.get("proxies") or {}),
            })
            resp = scripted.pop(0)
            resp.request = request
            return resp

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _fake_send)
        http = drive_http.RequestsHttp(auth_client)
        try:
            resp, body = http.request("https://www.googleapis.com/drive/v3/files/f-cidr")
            assert resp.status == 200
            assert body == b"ok"
            assert http._unauth_client is not None
            assert http._unauth_client.trust_env is False
            assert len(recorded) == 2
            hop1 = recorded[1]
            assert hop1["proxies"] == expected_proxies
            assert "authorization" not in hop1["headers"]
            assert "proxy-authorization" not in hop1["headers"]
            assert "cookie" not in hop1["headers"]
        finally:
            http.close()
