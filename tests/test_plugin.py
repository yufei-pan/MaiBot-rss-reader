"""插件入口与配置模型 smoke test。"""

from plugin import RssReaderPlugin, create_plugin


def test_create_plugin():
    plugin = create_plugin()
    assert isinstance(plugin, RssReaderPlugin)


def test_default_config_schema():
    plugin = create_plugin()
    schema = plugin.get_webui_config_schema(plugin_id="com.0-hz.rss-reader")
    assert isinstance(schema, dict)
    assert schema  # 非空
