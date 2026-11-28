"""Unit tests for v2 config normalization, v1<->v2 migration, and fan-out routing."""

from __future__ import annotations

import pytest

from kubedoor_tools.notify import (
    ChannelConfig,
    match_channels,
    migrate_v1_to_v2,
    normalize_v2_config,
    project_v2_to_v1,
)

WECOM_KEY = "a1b2c3d4-0000-4bc4-97a0-0ec2c3f5a1b2"
DING_TOKEN = "0123456789abcdef" * 4
FEISHU_HOOK = "3f8c1a2b-1111-2222-3333-444455556666"


def test_v1_to_v2_migration_and_projection_round_trip():
    v1 = {
        "default": {"type": "wecom", "token": WECOM_KEY, "at": "zhangsan"},
        "clusters": {
            "prod-k8s": {"type": "dingding", "token": DING_TOKEN, "at": "13800000000"},
            "dev-k8s": {"type": "feishu", "token": FEISHU_HOOK, "at": ""},
        },
    }
    v2 = migrate_v1_to_v2(v1)
    assert v2["version"] == 2
    assert v2["default_channel_ids"] == ["ch_default"]
    assert set(v2["channels"]) == {"ch_default", "ch_cluster_dev-k8s", "ch_cluster_prod-k8s"}
    assert len(v2["routes"]) == 2

    # Idempotent migration on v2 input
    assert migrate_v1_to_v2(v2) == v2

    # Projection back to v1 preserves default & clusters
    projected = project_v2_to_v1(v2)
    assert projected == v1

    # Route matching preserves v1 semantics (cluster wins, unknown falls back to default)
    prod_matches = match_channels(v2, env="prod-k8s", category="metric_alert", severity="critical")
    assert [c.id for c in prod_matches] == ["ch_cluster_prod-k8s"]
    assert prod_matches[0].provider == "dingding"

    other_matches = match_channels(v2, env="staging", category="ops", severity="info")
    assert [c.id for c in other_matches] == ["ch_default"]


def test_normalize_v2_config_secret_reuse_encryption_and_validation():
    raw = {
        "channels": [
            {
                "id": "ch_wecom",
                "name": "值班企微",
                "provider": "wecom",
                "enabled": True,
                "settings": {"domain_preset": "public", "at": "alice"},
                "secrets": {"token": f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM_KEY}"},
            },
            {
                "id": "ch_hook",
                "name": "SOAR Webhook",
                "provider": "webhook",
                "enabled": True,
                "settings": {"url": "https://soar.example.com/ingest", "method": "POST"},
                "secrets": {"hmac_secret": "hmac-123456"},
            },
        ],
        "routes": [
            {
                "id": "rt_crit",
                "name": "Critical Fanout",
                "enabled": True,
                "match": {"envs": ["prod-k8s"], "categories": ["metric_alert"], "severities": ["critical"]},
                "channel_ids": ["ch_wecom", "ch_hook"],
                "continue": False,
            }
        ],
        "default_channel_ids": ["ch_wecom"],
    }

    normalized = normalize_v2_config(
        raw,
        secret_encryptor=lambda s: f"enc:{s}",
        secret_decryptor=lambda s: s.removeprefix("enc:"),
    )
    assert normalized["channels"]["ch_wecom"]["secrets"]["token"] == f"enc:{WECOM_KEY}"
    assert normalized["channels"]["ch_hook"]["secrets"]["hmac_secret"] == "enc:hmac-123456"

    # Update with blank secrets reuses existing encrypted values
    updated_raw = {
        "channels": {
            "ch_wecom": {
                "id": "ch_wecom",
                "name": "值班企微改名",
                "provider": "wecom",
                "enabled": True,
                "settings": {"domain_preset": "public", "at": "bob"},
                "secrets": {"token": ""},
            },
            "ch_hook": {
                "id": "ch_hook",
                "name": "SOAR Webhook",
                "provider": "webhook",
                "enabled": True,
                "settings": {"url": "https://soar.example.com/ingest", "method": "POST"},
                "secrets": {},
            },
        },
        "routes": normalized["routes"],
        "default_channel_ids": ["ch_wecom"],
    }
    reused = normalize_v2_config(
        updated_raw,
        existing=normalized,
        secret_encryptor=lambda s: f"enc2:{s}",
        secret_decryptor=lambda s: s.removeprefix("enc:"),
    )
    assert reused["channels"]["ch_wecom"]["secrets"]["token"] == f"enc:{WECOM_KEY}"
    assert reused["channels"]["ch_wecom"]["settings"]["at"] == "bob"

    # Switching provider with blank secret must fail
    switched = {
        "channels": {
            "ch_wecom": {
                "id": "ch_wecom",
                "name": "换类型",
                "provider": "feishu",
                "enabled": True,
                "settings": {"domain_preset": "feishu"},
                "secrets": {"token": ""},
            }
        },
        "routes": [],
        "default_channel_ids": [],
    }
    with pytest.raises(ValueError, match="请填写飞书机器人地址"):
        normalize_v2_config(switched, existing=normalized)

    # Dangling route channel_id rejected
    bad_route = {
        "channels": raw["channels"],
        "routes": [{"id": "r1", "match": {}, "channel_ids": ["missing_ch"]}],
        "default_channel_ids": [],
    }
    with pytest.raises(ValueError, match="不存在的通道"):
        normalize_v2_config(bad_route, existing=normalized)


def test_match_channels_ordered_routes_continue_fanout_and_disabled():
    v2 = {
        "version": 2,
        "channels": {
            "ch_a": {"id": "ch_a", "name": "A", "provider": "wecom", "enabled": True, "settings": {}, "secrets": {"token": WECOM_KEY}},
            "ch_b": {"id": "ch_b", "name": "B", "provider": "feishu", "enabled": True, "settings": {}, "secrets": {"token": FEISHU_HOOK}},
            "ch_c": {"id": "ch_c", "name": "C", "provider": "slack", "enabled": False, "settings": {}, "secrets": {"token": "T/B/X"}},
            "ch_def": {"id": "ch_def", "name": "Def", "provider": "dingding", "enabled": True, "settings": {}, "secrets": {"token": DING_TOKEN}},
        },
        "routes": [
            {
                "id": "r_disabled",
                "enabled": False,
                "match": {"envs": ["prod"]},
                "channel_ids": ["ch_def"],
                "continue": False,
            },
            {
                "id": "r_fanout_1",
                "enabled": True,
                "match": {"envs": ["prod"], "severities": ["critical"]},
                "channel_ids": ["ch_a", "ch_c"],  # ch_c disabled -> skipped
                "continue": True,
            },
            {
                "id": "r_fanout_2",
                "enabled": True,
                "match": {"categories": ["metric_alert"]},
                "channel_ids": ["ch_a", "ch_b"],  # ch_a deduplicated
                "continue": False,
            },
            {
                "id": "r_never_reached",
                "enabled": True,
                "match": {},
                "channel_ids": ["ch_def"],
                "continue": False,
            },
        ],
        "default_channel_ids": ["ch_def"],
    }

    # Matches r_fanout_1 (continue=True) + r_fanout_2 (continue=False)
    fanout = match_channels(v2, env="prod", category="metric_alert", severity="critical")
    assert [c.id for c in fanout] == ["ch_a", "ch_b"]
    assert all(isinstance(c, ChannelConfig) for c in fanout)

    # Matches only r_fanout_2 (continue=False)
    metric_info = match_channels(v2, env="prod", category="metric_alert", severity="info")
    assert [c.id for c in metric_info] == ["ch_a", "ch_b"]

    # Matches no route -> default_channel_ids
    fallback = match_channels(v2, env="dev", category="ops", severity="info")
    assert [c.id for c in fallback] == ["ch_def"]

    # project_v2_to_v1 skips non-legacy channels cleanly
    v2_webhook_only = {
        "version": 2,
        "channels": {
            "ch_wh": {"id": "ch_wh", "name": "WH", "provider": "webhook", "enabled": True, "settings": {"url": "https://a.example"}, "secrets": {}}
        },
        "routes": [],
        "default_channel_ids": ["ch_wh"],
    }
    assert project_v2_to_v1(v2_webhook_only) == {"clusters": {}}
