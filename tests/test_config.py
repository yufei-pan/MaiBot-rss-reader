"""配置占位空值解析与旧版默认值迁移测试。"""

from typing import Any

from plugin import (
    CURRENT_CONFIG_VERSION,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_PROACTIVE_INTENT_TEMPLATE,
    RssSectionConfig,
    _migrate_legacy_baked_defaults,
    _migrate_nested_stream_feeds,
    create_plugin,
    resolve_effective_rss_config,
)


def _assert_no_none(value: Any, path: str = "$") -> None:
    """归一化结果不得含 None，否则 WebUI/Runner 用 tomlkit 落盘会 ConvertError。"""
    assert value is not None, f"配置含 None：{path}"
    if isinstance(value, dict):
        for key, nested in value.items():
            _assert_no_none(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_no_none(nested, f"{path}[{index}]")


def test_resolve_effective_rss_config_uses_builtin_defaults_when_empty():
    rss = RssSectionConfig()
    effective = resolve_effective_rss_config(rss)

    assert effective.poll_interval_seconds == DEFAULT_POLL_INTERVAL_SECONDS
    assert effective.proactive_intent_template == DEFAULT_PROACTIVE_INTENT_TEMPLATE
    assert effective.allow_private_networks is False
    assert effective.allow_http is False


def test_resolve_effective_rss_config_respects_user_override():
    rss = RssSectionConfig(poll_interval_seconds=600, proactive_intent_template="自定义 {count}")
    effective = resolve_effective_rss_config(rss)

    assert effective.poll_interval_seconds == 600
    assert effective.proactive_intent_template == "自定义 {count}"


def test_migrate_legacy_baked_defaults_strips_shipped_values():
    config = {
        "plugin": {"config_version": "1.0.0"},
        "rss": {
            "poll_interval_seconds": DEFAULT_POLL_INTERVAL_SECONDS,
            "proactive_intent_template": DEFAULT_PROACTIVE_INTENT_TEMPLATE,
        },
    }
    migrated, changed = _migrate_legacy_baked_defaults(config)

    assert changed is True
    assert migrated["rss"]["poll_interval_seconds"] is None
    assert migrated["rss"]["proactive_intent_template"] == ""
    assert migrated["plugin"]["config_version"] == CURRENT_CONFIG_VERSION


def test_migrate_nested_stream_feeds_to_flat_structure():
    config = {
        "plugin": {"config_version": "1.1.0"},
        "rss": {
            "streams": [
                {
                    "stream_id": "abc",
                    "enabled": True,
                    "feeds": [{"url": "https://a.example/rss", "name": "A"}],
                }
            ]
        },
    }
    migrated, changed = _migrate_nested_stream_feeds(config)

    assert changed is True
    assert migrated["rss"]["streams"] == [{"stream_id": "abc", "enabled": True}]
    assert migrated["rss"]["feeds"] == [
        {"stream_id": "abc", "url": "https://a.example/rss", "name": "A"}
    ]


def test_webui_schema_feeds_has_flat_item_fields():
    plugin = create_plugin()
    schema = plugin.get_webui_config_schema(plugin_id="com.0-hz.rss-reader")
    feeds = schema["sections"]["rss"]["fields"]["feeds"]
    streams = schema["sections"]["rss"]["fields"]["streams"]

    assert feeds["item_type"] == "object"
    assert "stream_id" in feeds["item_fields"]
    assert feeds["item_fields"]["url"]["type"] == "string"
    assert streams["item_fields"]["enabled"]["type"] == "boolean"
    assert "feeds" not in streams["item_fields"]


def test_normalize_plugin_config_omits_none_for_toml_persist():
    """WebUI 保存会走 normalize → tomlkit；Optional 默认 None 必须被剔除。"""
    plugin = create_plugin()
    normalized, _ = plugin.normalize_plugin_config({})

    _assert_no_none(normalized)
    assert "poll_interval_seconds" not in normalized["rss"]
    assert "item_separator" not in normalized["rss"]
    assert "allow_http" not in normalized["rss"]

    # 旧默认值迁移会先写成 None，归一化后也应剔除以便落盘
    legacy = {
        "plugin": {"config_version": "1.0.0", "enabled": True},
        "rss": {
            "poll_interval_seconds": DEFAULT_POLL_INTERVAL_SECONDS,
            "proactive_intent_template": DEFAULT_PROACTIVE_INTENT_TEMPLATE,
            "streams": [],
            "feeds": [],
        },
    }
    migrated_normalized, _ = plugin.normalize_plugin_config(legacy)
    _assert_no_none(migrated_normalized)
    assert "poll_interval_seconds" not in migrated_normalized["rss"]
    assert migrated_normalized["rss"]["proactive_intent_template"] == ""
