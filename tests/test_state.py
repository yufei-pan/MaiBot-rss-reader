from pathlib import Path

from plugin import RssItem, RssState


def _item(item_id: str, title: str = "t") -> RssItem:
    return RssItem(
        id=item_id,
        title=title,
        link=f"http://example/{item_id}",
        summary="s",
        published="2025-01-01",
        feed_name="测试源",
        feed_url="http://example/feed",
    )


def test_first_poll_establishes_baseline_without_new_items(tmp_path: Path):
    state = RssState(tmp_path / "rss_state.json")
    new_items, should_notify = state.update_feed(
        "http://example/feed", [_item("a"), _item("b")], max_items=30
    )
    assert new_items == []
    assert should_notify is False
    assert state.get_feed_state("http://example/feed").initialized is True


def test_second_poll_detects_new_items(tmp_path: Path):
    state = RssState(tmp_path / "rss_state.json")
    state.update_feed("http://example/feed", [_item("a")], max_items=30)
    new_items, should_notify = state.update_feed(
        "http://example/feed", [_item("a"), _item("b")], max_items=30
    )
    assert len(new_items) == 1
    assert new_items[0].id == "b"
    assert should_notify is True


def test_refresh_cache_updates_items(tmp_path: Path):
    state = RssState(tmp_path / "rss_state.json")
    state.update_feed("http://example/feed", [_item("a")], max_items=30)
    state.refresh_cache("http://example/feed", [_item("a"), _item("b")], max_items=30)
    assert len(state.get_cached_items("http://example/feed")) == 2


def test_state_persists(tmp_path: Path):
    path = tmp_path / "rss_state.json"
    state = RssState(path)
    state.update_feed("http://example/feed", [_item("x")], max_items=30)
    state.save()
    assert len(RssState(path).get_cached_items("http://example/feed")) == 1
