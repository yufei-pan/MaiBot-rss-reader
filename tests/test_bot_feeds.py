from pathlib import Path

from plugin import BotFeedsStore


def test_bot_feeds_store_roundtrip(tmp_path: Path):
    path = tmp_path / "rss_bot_feeds.json"
    store = BotFeedsStore(path)
    assert store.add_feed("stream-a", "https://a.example/feed", "源 A")
    store.save()

    reloaded = BotFeedsStore(path)
    assert reloaded.get_feeds("stream-a") == [("https://a.example/feed", "源 A")]


def test_bot_feeds_dedupes_url_per_stream(tmp_path: Path):
    path = tmp_path / "rss_bot_feeds.json"
    store = BotFeedsStore(path)
    assert store.add_feed("stream-a", "https://a.example/feed", "源 A")
    assert not store.add_feed("stream-a", "https://a.example/feed", "重复")
    assert len(store.get_feeds("stream-a")) == 1


def test_bot_feeds_separate_streams(tmp_path: Path):
    path = tmp_path / "rss_bot_feeds.json"
    store = BotFeedsStore(path)
    store.add_feed("stream-a", "https://a.example/feed", "A")
    store.add_feed("stream-b", "https://b.example/feed", "B")
    store.save()

    reloaded = BotFeedsStore(path)
    assert reloaded.get_feeds("stream-a") == [("https://a.example/feed", "A")]
    assert reloaded.get_feeds("stream-b") == [("https://b.example/feed", "B")]
    assert reloaded.all_stream_ids() == {"stream-a", "stream-b"}
