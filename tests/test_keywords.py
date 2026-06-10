from plugin import RssItem, filter_items_by_keywords, parse_keywords


def _item(**overrides: str) -> RssItem:
    defaults = {
        "id": "id-1",
        "title": "Hello World",
        "link": "https://example.com/post",
        "summary": "A short summary",
        "published": "2025-06-01",
        "feed_name": "Example Feed",
        "feed_url": "https://example.com/feed.xml",
    }
    defaults.update(overrides)
    return RssItem(**defaults)


def test_parse_keywords_splits_on_space_and_comma():
    assert parse_keywords("foo, bar  baz") == ["foo", "bar", "baz"]
    assert parse_keywords("中文，英文") == ["中文", "英文"]
    assert parse_keywords("") == []


def test_filter_items_any_keyword_matches_any_field():
    items = [
        _item(title="Alpha"),
        _item(title="Beta", summary="contains gamma"),
        _item(title="Delta", link="https://site.org/omega"),
    ]
    result = filter_items_by_keywords(items, ["gamma", "omega"])
    assert [item.title for item in result] == ["Beta", "Delta"]


def test_filter_items_case_insensitive():
    items = [_item(title="OpenAI Release"), _item(title="other")]
    result = filter_items_by_keywords(items, ["openai"])
    assert len(result) == 1
    assert result[0].title == "OpenAI Release"


def test_filter_items_does_not_match_feed_url():
    items = [_item(feed_url="https://secret-feed.example/rss.xml", title="unrelated")]
    result = filter_items_by_keywords(items, ["secret-feed"])
    assert result == []


def test_filter_items_empty_keywords_returns_all():
    items = [_item(), _item(id="id-2", title="Second")]
    assert filter_items_by_keywords(items, []) == items
