from plugin import (
    RssItem,
    TemplateValues,
    format_items,
    render_proactive_intent,
    render_visible_text,
    sort_items_by_published,
)


def _templates() -> TemplateValues:
    return TemplateValues(
        item_template="[{title}]({link})",
        item_separator="\n---\n",
        context_preamble_template="Preamble {count}\n",
        context_visible_text_template="可见 {count} 条",
        proactive_intent_template="新内容 {count} 条，来自 {feed_names}",
    )


def test_sort_items_by_published_desc():
    items = [
        RssItem("1", "旧", "", "", "2024-01-01", "a", "u"),
        RssItem("2", "新", "", "", "2025-06-01", "a", "u"),
    ]
    assert sort_items_by_published(items)[0].title == "新"


def test_format_items():
    items = [RssItem("1", "标题A", "http://a", "摘要", "2025-01-01", "源", "u")]
    assert "[标题A](http://a)" in format_items(items, _templates())


def test_render_templates_replace_placeholders():
    tpl = _templates()
    assert render_visible_text(tpl, count=3, feed_names="科技", stream_id="s1") == "可见 3 条"
    assert "新内容 3 条" in render_proactive_intent(tpl, count=3, feed_names="科技", stream_id="s1")


def test_render_does_not_use_format_braces_in_content():
    tpl = TemplateValues(
        item_template="{title}",
        item_separator="",
        context_preamble_template="",
        context_visible_text_template="{count}",
        proactive_intent_template="{feed_names}",
    )
    items = [RssItem("1", "含{花括号}标题", "", "", "", "", "")]
    assert format_items(items, tpl) == "含{花括号}标题"
