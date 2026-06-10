from plugin import parse_feed_content

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>测试 Feed</title>
    <item>
      <title>第一条</title>
      <link>https://example.com/1</link>
      <guid>guid-1</guid>
      <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
      <description><![CDATA[<p>摘要一</p>]]></description>
    </item>
  </channel>
</rss>
""".encode("utf-8")


def test_parse_feed_content():
    items = parse_feed_content(SAMPLE_RSS, "https://example.com/feed.xml", "自定义名")
    assert len(items) == 1
    assert items[0].title == "第一条"
    assert items[0].id == "guid-1"
    assert items[0].link == "https://example.com/1"
    assert "摘要一" in items[0].summary
    assert items[0].feed_name == "自定义名"
