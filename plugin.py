"""麦麦 RSS 阅读器插件。

周期性拉取配置的 RSS 源，将新内容注入 Maisaka 上下文并触发主动处理；
提供 query_rss_feeds 工具与 /rss 命令供麦麦与用户查阅订阅。
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from collections.abc import Mapping
from types import UnionType
from typing import Any, Protocol, Sequence, Union, get_args, get_origin
from urllib.parse import urlparse

import httpx

from maibot_sdk import Command, CONFIG_RELOAD_SCOPE_SELF, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.config import validate_plugin_config
from maibot_sdk.types import ToolParameterInfo, ToolParamType

# --------------------------------------------------------------------------- #
# 默认模板
# --------------------------------------------------------------------------- #

DEFAULT_PROACTIVE_INTENT_TEMPLATE = """有 {count} 条新的 RSS 内容已写入你的内部上下文（来源：{feed_names}）。

这些内容不会直接发给用户，仅供你自己阅读与决策。

这些 RSS 是给你自己看的——不是要你就当用户的过滤器或摘要器。你可以按自己的兴趣决定要不要深入、要不要分享、要不要暂时忽略。

RSS 摘要往往很短；若某条让你好奇，可以用你已有的网页抓取能力（如 fetch、playwright 等）打开原文链接查看详情，再决定接下来想做什么。

你完全可以自主行动：现在聊聊、稍后再说、只记在心里、或通过备忘工具留存；不必为了「服务用户」而强行推送。"""

DEFAULT_CONTEXT_VISIBLE_TEXT_TEMPLATE = "RSS 新内容 {count} 条"
DEFAULT_CONTEXT_PREAMBLE_TEMPLATE = """【RSS 订阅 · 内部上下文】
以下条目仅写入你的对话上下文，不会直接发给用户。供你自己浏览，无需充当用户的信息筛选器。若某条感兴趣，可抓取链接原文深入了解。

"""
DEFAULT_QUERY_RSS_HINT = """
---
【提示】以下为内部上下文，不会发给用户。这些 RSS 是给你自己查阅的，不是让你帮用户过滤或代读。摘要可能不完整；若某条感兴趣，请用网页抓取工具打开链接查看全文，再按你想做的来（分享、备忘、忽略或仅自己消化）。"""
DEFAULT_ITEM_TEMPLATE = """### {title}
- 来源：{feed_name}
- 时间：{published}
- 链接：{link}
- 摘要：{summary}
"""
DEFAULT_ITEM_SEPARATOR = "\n"

DEFAULT_POLL_INTERVAL_SECONDS = 300
DEFAULT_REQUEST_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_CONCURRENT_FETCHES = 5
DEFAULT_MAX_ITEMS_PER_FEED = 30
DEFAULT_MAX_SEEN_IDS_PER_FEED = 500
CURRENT_CONFIG_VERSION = "1.3.0"

_TAG_RE = re.compile(r"<[^>]+>")


# --------------------------------------------------------------------------- #
# 出站 URL 安全（SSRF 防护）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FeedFetchPolicy:
    allow_private_networks: bool = False
    allow_http: bool = False


def _is_private_address(address: str) -> bool:
    """判断 IP 字符串是否属于内网 / 环回 / 链路本地 / 保留地址。"""
    try:
        ip_obj = ipaddress.ip_address(address)
    except ValueError:
        return False
    return bool(
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_reserved
        or ip_obj.is_multicast
        or ip_obj.is_unspecified
    )


def _assert_scheme_allowed(scheme: str, *, allow_http: bool) -> None:
    normalized = scheme.lower()
    if normalized == "https":
        return
    if normalized == "http" and allow_http:
        return
    if normalized == "http":
        raise ValueError("仅允许 https 订阅地址（可在配置 rss.allow_http 中放行 http）")
    raise ValueError("URL 必须是有效的 http/https 地址")


async def _assert_host_allowed(host: str, *, allow_private_networks: bool) -> None:
    """校验目标主机不属于内网 / 保留地址（除非配置放行）。"""
    if allow_private_networks:
        return
    if not host:
        raise ValueError("无法解析目标主机名")
    stripped_host = host.strip("[]")
    try:
        ipaddress.ip_address(stripped_host)
        addresses = [stripped_host]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(stripped_host, None)
        except OSError as exc:
            raise ValueError(f"域名解析失败：{stripped_host}（{exc}）") from exc
        addresses = [str(info[4][0]) for info in infos]
    for address in addresses:
        if _is_private_address(address):
            raise ValueError(
                f"目标地址 {stripped_host} 解析到内网/保留地址，已被安全策略拦截"
                "（可在配置 rss.allow_private_networks 中放行）"
            )


async def validate_outbound_feed_url(url: str, *, policy: FeedFetchPolicy) -> str:
    """请求前预检 RSS 订阅 URL（scheme、认证信息、主机解析）。"""
    normalized = url.strip()
    if not normalized:
        raise ValueError("URL 不能为空")
    parsed = urlparse(normalized)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("URL 必须是有效的 http/https 地址")

    _assert_scheme_allowed(parsed.scheme, allow_http=policy.allow_http)

    if parsed.username or parsed.password:
        raise ValueError("URL 不允许内嵌认证信息")

    hostname = (parsed.hostname or "").strip()
    if not hostname:
        raise ValueError("URL 缺少有效的主机名")

    if not policy.allow_private_networks:
        if hostname.lower() in {"localhost", "localhost.localdomain"}:
            raise ValueError("不允许访问本地主机")
        await _assert_host_allowed(hostname, allow_private_networks=False)

    return normalized


def build_feed_http_client(*, policy: FeedFetchPolicy, timeout: float) -> httpx.AsyncClient:
    """构建带 SSRF 防护钩子的 HTTP 客户端（含重定向各跳校验）。"""

    async def ssrf_request_hook(request: httpx.Request) -> None:
        _assert_scheme_allowed(str(request.url.scheme or ""), allow_http=policy.allow_http)
        await _assert_host_allowed(
            str(request.url.host or ""),
            allow_private_networks=policy.allow_private_networks,
        )

    return httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        event_hooks={"request": [ssrf_request_hook]},
    )


# --------------------------------------------------------------------------- #
# RSS 拉取与解析
# --------------------------------------------------------------------------- #


@dataclass
class RssItem:
    id: str
    title: str
    link: str
    summary: str
    published: str
    feed_name: str
    feed_url: str


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return unescape(_TAG_RE.sub("", text)).strip()


def _entry_id(entry: Any, feed_url: str) -> str:
    for attr in ("id", "guid"):
        value = getattr(entry, attr, None)
        if value:
            return str(value).strip()
    link = str(getattr(entry, "link", "") or "").strip()
    if link:
        return link
    title = str(getattr(entry, "title", "") or "").strip()
    published = str(getattr(entry, "published", "") or getattr(entry, "updated", "") or "").strip()
    digest = hashlib.sha256(f"{feed_url}|{title}|{published}".encode("utf-8")).hexdigest()
    return f"hash:{digest}"


def _entry_published(entry: Any) -> str:
    if getattr(entry, "published", None):
        return str(entry.published).strip()
    if getattr(entry, "updated", None):
        return str(entry.updated).strip()
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if parsed:
        try:
            return f"{parsed.tm_year:04d}-{parsed.tm_mon:02d}-{parsed.tm_mday:02d}"
        except Exception:
            pass
    return ""


def _entry_summary(entry: Any) -> str:
    for attr in ("summary", "description", "content"):
        value = getattr(entry, attr, None)
        if not value:
            continue
        if isinstance(value, list) and value:
            value = value[0].get("value", "") if isinstance(value[0], dict) else value[0]
        text = _strip_html(str(value))
        if text:
            return text
    return ""


def _get_feedparser() -> Any:
    """延迟导入 feedparser，避免插件模块加载阶段因依赖未安装而无法解析配置 Schema。"""
    try:
        import feedparser
    except ImportError as exc:
        raise ImportError(
            "缺少依赖 feedparser。请在 _manifest.json 声明后重载/重启 MaiBot 以自动安装，"
            "或手动执行: pip install 'feedparser>=6.0.0'"
        ) from exc
    return feedparser


def _items_from_parsed(parsed: Any, feed_url: str, feed_name: str = "") -> tuple[str, list[RssItem]]:
    resolved_name = (feed_name or getattr(parsed.feed, "title", "") or feed_url).strip()
    items: list[RssItem] = []
    for entry in parsed.entries:
        items.append(
            RssItem(
                id=_entry_id(entry, feed_url),
                title=str(getattr(entry, "title", "") or "（无标题）").strip(),
                link=str(getattr(entry, "link", "") or "").strip(),
                summary=_entry_summary(entry),
                published=_entry_published(entry),
                feed_name=resolved_name,
                feed_url=feed_url,
            )
        )
    return resolved_name, items


def parse_feed_content(content: bytes, feed_url: str, feed_name: str = "") -> list[RssItem]:
    parsed = _get_feedparser().parse(content)
    _, items = _items_from_parsed(parsed, feed_url, feed_name)
    return items


def validate_feed_bytes(content: bytes, feed_url: str, feed_name: str = "") -> tuple[str, list[RssItem]]:
    """校验响应体为可解析的 RSS/Atom，并返回 feed 标题与条目。"""
    parsed = _get_feedparser().parse(content)
    has_feed = bool(getattr(parsed, "feed", None) and getattr(parsed.feed, "title", None))
    has_entries = bool(parsed.entries)
    if getattr(parsed, "bozo", False) and not has_entries and not has_feed:
        exc = getattr(parsed, "bozo_exception", None)
        raise ValueError(f"RSS 解析失败: {exc or '格式无效'}")
    if not has_feed and not has_entries:
        raise ValueError("响应内容不是有效的 RSS/Atom feed")
    return _items_from_parsed(parsed, feed_url, feed_name)


async def validate_feed_url(
    url: str,
    *,
    timeout: float = 20.0,
    feed_name: str = "",
    client: httpx.AsyncClient | None = None,
    policy: FeedFetchPolicy | None = None,
) -> tuple[str, list[RssItem]]:
    """拉取并校验 RSS URL，返回 (feed 标题, 条目列表)。"""
    fetch_policy = policy or FeedFetchPolicy()
    normalized = await validate_outbound_feed_url(url, policy=fetch_policy)

    owns_client = client is None
    if client is None:
        client = build_feed_http_client(policy=fetch_policy, timeout=timeout)
    try:
        response = await client.get(normalized, timeout=timeout)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if content_type and "html" in content_type and "xml" not in content_type and "rss" not in content_type:
            raise ValueError(f"URL 返回 HTML 而非 RSS/XML（Content-Type: {content_type}）")
        return await asyncio.to_thread(validate_feed_bytes, response.content, normalized, feed_name)
    finally:
        if owns_client:
            await client.aclose()


async def fetch_feed(
    url: str,
    *,
    timeout: float = 20.0,
    feed_name: str = "",
    client: httpx.AsyncClient | None = None,
    policy: FeedFetchPolicy | None = None,
) -> list[RssItem]:
    normalized_url = url.strip()
    if not normalized_url:
        return []

    fetch_policy = policy or FeedFetchPolicy()
    normalized_url = await validate_outbound_feed_url(normalized_url, policy=fetch_policy)

    owns_client = client is None
    if client is None:
        client = build_feed_http_client(policy=fetch_policy, timeout=timeout)

    try:
        response = await client.get(normalized_url, timeout=timeout)
        response.raise_for_status()
        return await asyncio.to_thread(parse_feed_content, response.content, normalized_url, feed_name)
    finally:
        if owns_client:
            await client.aclose()


# --------------------------------------------------------------------------- #
# 格式化与模板渲染
# --------------------------------------------------------------------------- #


class _TemplateConfig(Protocol):
    item_template: str
    item_separator: str
    context_preamble_template: str
    context_visible_text_template: str
    proactive_intent_template: str


def _render(template: str, **values: Any) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", str(value))
    return rendered


def _parse_published(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_keywords(raw: str) -> list[str]:
    """将逗号或空格分隔的关键词字符串解析为列表。"""
    if not raw or not raw.strip():
        return []
    parts = re.split(r"[,，\s]+", raw.strip())
    return [part for part in parts if part]


def _item_searchable_text(item: RssItem) -> str:
    return "\n".join(
        [
            item.id,
            item.title,
            item.link,
            item.summary,
            item.published,
            item.feed_name,
        ]
    )


def filter_items_by_keywords(items: Sequence[RssItem], keywords: Sequence[str]) -> list[RssItem]:
    """任意关键词匹配任意可搜索字段（不含 feed_url）则保留。"""
    normalized = [kw.strip().lower() for kw in keywords if kw and kw.strip()]
    if not normalized:
        return list(items)
    result: list[RssItem] = []
    for item in items:
        haystack = _item_searchable_text(item).lower()
        if any(keyword in haystack for keyword in normalized):
            result.append(item)
    return result


def sort_items_by_published(items: Sequence[RssItem]) -> list[RssItem]:
    def sort_key(item: RssItem) -> tuple[int, str]:
        parsed = _parse_published(item.published)
        if parsed is None:
            return (0, item.published)
        return (1, parsed.isoformat())

    return sorted(items, key=sort_key, reverse=True)


def format_item(item: RssItem, templates: _TemplateConfig) -> str:
    return _render(
        templates.item_template,
        feed_name=item.feed_name or "",
        title=item.title or "（无标题）",
        link=item.link or "",
        summary=item.summary or "",
        published=item.published or "（未知）",
    )


def format_items(
    items: Sequence[RssItem],
    templates: _TemplateConfig,
    *,
    max_items: int | None = None,
) -> str:
    sorted_items = sort_items_by_published(list(items))
    if max_items is not None and max_items > 0:
        sorted_items = sorted_items[:max_items]
    return templates.item_separator.join(format_item(item, templates) for item in sorted_items)


def render_preamble(
    templates: _TemplateConfig,
    *,
    count: int = 0,
    feed_names: str = "",
    stream_id: str = "",
) -> str:
    return _render(
        templates.context_preamble_template,
        count=count,
        feed_names=feed_names,
        stream_id=stream_id,
    )


def render_visible_text(
    templates: _TemplateConfig,
    *,
    count: int,
    feed_names: str = "",
    stream_id: str = "",
) -> str:
    return _render(
        templates.context_visible_text_template,
        count=count,
        feed_names=feed_names,
        stream_id=stream_id,
    )


def render_proactive_intent(
    templates: _TemplateConfig,
    *,
    count: int,
    feed_names: str,
    stream_id: str,
) -> str:
    return _render(
        templates.proactive_intent_template,
        count=count,
        feed_names=feed_names,
        stream_id=stream_id,
    )


@dataclass(frozen=True)
class TemplateValues:
    """用于单元测试的模板值容器。"""

    item_template: str
    item_separator: str
    context_preamble_template: str
    context_visible_text_template: str
    proactive_intent_template: str


# --------------------------------------------------------------------------- #
# 状态持久化
# --------------------------------------------------------------------------- #


@dataclass
class FeedState:
    seen_ids: list[str] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    last_fetch: str = ""
    initialized: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "seen_ids": list(self.seen_ids),
            "items": list(self.items),
            "last_fetch": self.last_fetch,
            "initialized": self.initialized,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FeedState:
        return cls(
            seen_ids=[str(x) for x in data.get("seen_ids", [])],
            items=list(data.get("items", [])),
            last_fetch=str(data.get("last_fetch", "") or ""),
            initialized=bool(data.get("initialized", False)),
        )


def _item_to_dict(item: RssItem) -> dict[str, Any]:
    return asdict(item)


def _item_from_dict(data: dict[str, Any]) -> RssItem:
    return RssItem(
        id=str(data.get("id", "")),
        title=str(data.get("title", "")),
        link=str(data.get("link", "")),
        summary=str(data.get("summary", "")),
        published=str(data.get("published", "")),
        feed_name=str(data.get("feed_name", "")),
        feed_url=str(data.get("feed_url", "")),
    )


def _prune_seen_ids(seen_ids: list[str], item_ids: set[str], max_seen: int) -> list[str]:
    """裁剪 seen_ids，优先保留当前缓存条目 ID。"""
    if max_seen <= 0:
        return list(dict.fromkeys(seen_ids))
    ordered = list(dict.fromkeys(seen_ids))
    if len(ordered) <= max_seen:
        return ordered
    priority = [item_id for item_id in ordered if item_id in item_ids]
    others = [item_id for item_id in ordered if item_id not in item_ids]
    slots_for_others = max(0, max_seen - len(priority))
    return priority + others[-slots_for_others:]


@dataclass
class BotFeedEntry:
    url: str
    name: str
    added_at: str

    def to_dict(self) -> dict[str, str]:
        return {"url": self.url, "name": self.name, "added_at": self.added_at}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BotFeedEntry:
        return cls(
            url=str(data.get("url", "") or "").strip(),
            name=str(data.get("name", "") or "").strip(),
            added_at=str(data.get("added_at", "") or "").strip(),
        )


class BotFeedsStore:
    """麦麦通过工具自行添加的 RSS 订阅（独立于 config.toml）。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._streams: dict[str, list[BotFeedEntry]] = {}
        self.load()

    def load(self) -> None:
        if not self._path.exists():
            self._streams = {}
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._streams = {}
            return
        streams_raw = raw.get("streams", {})
        if not isinstance(streams_raw, dict):
            self._streams = {}
            return
        self._streams = {}
        for stream_id, payload in streams_raw.items():
            feeds_raw = payload.get("feeds", []) if isinstance(payload, dict) else []
            feeds = [
                BotFeedEntry.from_dict(entry)
                for entry in feeds_raw
                if isinstance(entry, dict) and str(entry.get("url", "")).strip()
            ]
            if feeds:
                self._streams[str(stream_id)] = feeds

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "streams": {
                stream_id: {"feeds": [entry.to_dict() for entry in feeds]}
                for stream_id, feeds in self._streams.items()
            },
        }
        temp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, self._path)

    def all_stream_ids(self) -> set[str]:
        return set(self._streams.keys())

    def get_feeds(self, stream_id: str) -> list[tuple[str, str]]:
        normalized = (stream_id or "").strip()
        return [(entry.url, entry.name) for entry in self._streams.get(normalized, [])]

    def has_url(self, stream_id: str, url: str) -> bool:
        normalized_url = url.strip()
        return any(feed_url == normalized_url for feed_url, _ in self.get_feeds(stream_id))

    def add_feed(self, stream_id: str, url: str, name: str) -> bool:
        """追加订阅；若 URL 已存在则返回 False。"""
        normalized_stream = (stream_id or "").strip()
        normalized_url = url.strip()
        if not normalized_stream or not normalized_url:
            return False
        if self.has_url(normalized_stream, normalized_url):
            return False
        entry = BotFeedEntry(
            url=normalized_url,
            name=name.strip(),
            added_at=datetime.now(timezone.utc).isoformat(),
        )
        self._streams.setdefault(normalized_stream, []).append(entry)
        return True


class RssState:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._feeds: dict[str, FeedState] = {}
        self.load()

    def load(self) -> None:
        if not self._path.exists():
            self._feeds = {}
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._feeds = {}
            return
        feeds_raw = raw.get("feeds", {})
        if not isinstance(feeds_raw, dict):
            self._feeds = {}
            return
        self._feeds = {
            str(url): FeedState.from_dict(state if isinstance(state, dict) else {})
            for url, state in feeds_raw.items()
        }

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"feeds": {url: state.to_dict() for url, state in self._feeds.items()}}
        temp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, self._path)

    def get_feed_state(self, feed_url: str) -> FeedState:
        if feed_url not in self._feeds:
            self._feeds[feed_url] = FeedState()
        return self._feeds[feed_url]

    def get_cached_items(self, feed_url: str) -> list[RssItem]:
        return [_item_from_dict(item) for item in self.get_feed_state(feed_url).items]

    def update_feed(
        self,
        feed_url: str,
        fetched_items: list[RssItem],
        *,
        max_items: int,
        max_seen_ids: int = 500,
    ) -> tuple[list[RssItem], bool]:
        state = self.get_feed_state(feed_url)
        seen = set(state.seen_ids)
        trimmed = fetched_items[:max_items] if max_items > 0 else fetched_items
        state.items = [_item_to_dict(item) for item in trimmed]
        state.last_fetch = datetime.now(timezone.utc).isoformat()
        item_ids = {item.id for item in trimmed}

        if not state.initialized:
            for item in trimmed:
                seen.add(item.id)
            state.seen_ids = _prune_seen_ids(list(seen), item_ids, max_seen_ids)
            state.initialized = True
            self._feeds[feed_url] = state
            return [], False

        new_items = [item for item in trimmed if item.id not in seen]
        for item in trimmed:
            seen.add(item.id)
        state.seen_ids = _prune_seen_ids(list(seen), item_ids, max_seen_ids)
        self._feeds[feed_url] = state
        return new_items, bool(new_items)

    def refresh_cache(
        self,
        feed_url: str,
        fetched_items: list[RssItem],
        *,
        max_items: int,
        max_seen_ids: int = 500,
    ) -> None:
        state = self.get_feed_state(feed_url)
        trimmed = fetched_items[:max_items] if max_items > 0 else fetched_items
        state.items = [_item_to_dict(item) for item in trimmed]
        state.last_fetch = datetime.now(timezone.utc).isoformat()
        seen = set(state.seen_ids)
        for item in trimmed:
            seen.add(item.id)
        item_ids = {item.id for item in trimmed}
        state.seen_ids = _prune_seen_ids(list(seen), item_ids, max_seen_ids)
        if trimmed and not state.initialized:
            state.initialized = True
        self._feeds[feed_url] = state

    def is_stale(self, feed_url: str, poll_interval_seconds: int) -> bool:
        state = self.get_feed_state(feed_url)
        if not state.last_fetch:
            return True
        try:
            last = datetime.fromisoformat(state.last_fetch.replace("Z", "+00:00"))
        except ValueError:
            return True
        age = (datetime.now(timezone.utc) - last.astimezone(timezone.utc)).total_seconds()
        return age > max(1, poll_interval_seconds)


# --------------------------------------------------------------------------- #
# 配置解析（空值 = 使用代码内置默认，便于版本升级后自动跟随新默认）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EffectiveRssConfig:
    """运行时生效的 RSS 配置（已解析占位空值）。"""

    poll_interval_seconds: int
    request_timeout_seconds: float
    max_concurrent_fetches: int
    max_items_per_feed: int
    max_seen_ids_per_feed: int
    proactive_intent_template: str
    context_visible_text_template: str
    context_preamble_template: str
    item_template: str
    item_separator: str
    allow_private_networks: bool
    allow_http: bool


def _effective_int(value: int | None, default: int, *, minimum: int = 1) -> int:
    if value is None:
        return default
    return max(minimum, int(value))


def _effective_float(value: float | None, default: float, *, minimum: float = 1.0) -> float:
    if value is None:
        return default
    return max(minimum, float(value))


def _effective_template(value: str | None, default: str) -> str:
    if value is None or not str(value).strip():
        return default
    return str(value)


def _effective_separator(value: str | None, default: str) -> str:
    if value is None:
        return default
    return str(value)


def _effective_bool(value: bool | None, default: bool) -> bool:
    if value is None:
        return default
    return bool(value)


def resolve_effective_rss_config(rss: RssSectionConfig) -> EffectiveRssConfig:
    return EffectiveRssConfig(
        poll_interval_seconds=_effective_int(
            rss.poll_interval_seconds, DEFAULT_POLL_INTERVAL_SECONDS, minimum=30
        ),
        request_timeout_seconds=_effective_float(
            rss.request_timeout_seconds, DEFAULT_REQUEST_TIMEOUT_SECONDS, minimum=1.0
        ),
        max_concurrent_fetches=_effective_int(
            rss.max_concurrent_fetches, DEFAULT_MAX_CONCURRENT_FETCHES, minimum=1
        ),
        max_items_per_feed=_effective_int(rss.max_items_per_feed, DEFAULT_MAX_ITEMS_PER_FEED, minimum=1),
        max_seen_ids_per_feed=_effective_int(
            rss.max_seen_ids_per_feed, DEFAULT_MAX_SEEN_IDS_PER_FEED, minimum=50
        ),
        proactive_intent_template=_effective_template(
            rss.proactive_intent_template, DEFAULT_PROACTIVE_INTENT_TEMPLATE
        ),
        context_visible_text_template=_effective_template(
            rss.context_visible_text_template, DEFAULT_CONTEXT_VISIBLE_TEXT_TEMPLATE
        ),
        context_preamble_template=_effective_template(
            rss.context_preamble_template, DEFAULT_CONTEXT_PREAMBLE_TEMPLATE
        ),
        item_template=_effective_template(rss.item_template, DEFAULT_ITEM_TEMPLATE),
        item_separator=_effective_separator(rss.item_separator, DEFAULT_ITEM_SEPARATOR),
        allow_private_networks=_effective_bool(rss.allow_private_networks, False),
        allow_http=_effective_bool(rss.allow_http, False),
    )


_LEGACY_BAKED_RSS_DEFAULTS: dict[str, int | float | str] = {
    "poll_interval_seconds": DEFAULT_POLL_INTERVAL_SECONDS,
    "request_timeout_seconds": DEFAULT_REQUEST_TIMEOUT_SECONDS,
    "max_concurrent_fetches": DEFAULT_MAX_CONCURRENT_FETCHES,
    "max_items_per_feed": DEFAULT_MAX_ITEMS_PER_FEED,
    "max_seen_ids_per_feed": DEFAULT_MAX_SEEN_IDS_PER_FEED,
    "proactive_intent_template": DEFAULT_PROACTIVE_INTENT_TEMPLATE,
    "context_visible_text_template": DEFAULT_CONTEXT_VISIBLE_TEXT_TEMPLATE,
    "context_preamble_template": DEFAULT_CONTEXT_PREAMBLE_TEMPLATE,
    "item_template": DEFAULT_ITEM_TEMPLATE,
    "item_separator": DEFAULT_ITEM_SEPARATOR,
}


def _migrate_legacy_baked_defaults(config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """将旧版 config.toml 中写死的默认值还原为占位空值，以便跟随代码内置默认。"""
    rss = config.get("rss")
    if not isinstance(rss, dict):
        return config, False

    changed = False
    for key, legacy_value in _LEGACY_BAKED_RSS_DEFAULTS.items():
        if key not in rss:
            continue
        current = rss[key]
        if isinstance(legacy_value, str):
            if str(current) != legacy_value:
                continue
            rss[key] = "" if key != "item_separator" else None
        elif current == legacy_value:
            rss[key] = None
        else:
            continue
        changed = True

    plugin_section = config.get("plugin")
    if isinstance(plugin_section, dict):
        plugin_section["config_version"] = CURRENT_CONFIG_VERSION

    return config, changed


def _migrate_nested_stream_feeds(config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """将旧版 [[rss.streams]] + [[rss.streams.feeds]] 嵌套结构迁移为扁平 streams + feeds。"""
    rss = config.get("rss")
    if not isinstance(rss, dict):
        return config, False

    streams = rss.get("streams")
    if not isinstance(streams, list) or not streams:
        return config, False
    if not any(isinstance(stream, dict) and isinstance(stream.get("feeds"), list) for stream in streams):
        return config, False

    flat_streams: list[dict[str, Any]] = []
    flat_feeds: list[dict[str, str]] = []
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        stream_id = str(stream.get("stream_id", "") or "").strip()
        if stream_id:
            flat_streams.append(
                {
                    "stream_id": stream_id,
                    "enabled": bool(stream.get("enabled", True)),
                }
            )
        nested_feeds = stream.get("feeds")
        if not isinstance(nested_feeds, list):
            continue
        for feed in nested_feeds:
            if not isinstance(feed, dict):
                continue
            url = str(feed.get("url", "") or "").strip()
            if not stream_id or not url:
                continue
            flat_feeds.append(
                {
                    "stream_id": stream_id,
                    "url": url,
                    "name": str(feed.get("name", "") or "").strip(),
                }
            )

    rss["streams"] = flat_streams
    rss["feeds"] = flat_feeds
    return config, True



def _annotation_allows_none(annotation: Any) -> bool:
    """判断类型注解是否允许 ``None``（如 ``int | None``）。"""
    origin = get_origin(annotation)
    if origin is Union or origin is UnionType:
        return type(None) in get_args(annotation)
    return annotation is type(None)


def _unwrap_optional_annotation(annotation: Any) -> Any:
    """剥掉 ``X | None``，返回内层类型。"""
    origin = get_origin(annotation)
    if origin is Union or origin is UnionType:
        args = [item for item in get_args(annotation) if item is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _coerce_webui_blank_optionals(data: Any, model: type[Any]) -> Any:
    """WebUI 清空 Optional 字段会提交空字符串；转为 None 以便跟随内置默认。"""
    if not isinstance(data, Mapping):
        return data
    model_fields = getattr(model, "model_fields", None)
    if not isinstance(model_fields, dict):
        return dict(data)
    cleaned = dict(data)
    for name, field_info in model_fields.items():
        if name not in cleaned:
            continue
        value = cleaned[name]
        annotation = field_info.annotation
        inner = _unwrap_optional_annotation(annotation)
        if hasattr(inner, "model_fields") and isinstance(value, Mapping):
            cleaned[name] = _coerce_webui_blank_optionals(value, inner)
            continue
        origin = get_origin(inner)
        if origin is list and isinstance(value, list):
            args = get_args(inner)
            item_type = args[0] if args else None
            if item_type is not None and hasattr(item_type, "model_fields"):
                cleaned[name] = [
                    _coerce_webui_blank_optionals(item, item_type) if isinstance(item, Mapping) else item
                    for item in value
                ]
            continue
        if isinstance(value, str) and not value.strip() and _annotation_allows_none(annotation):
            cleaned[name] = None
    return cleaned


def _strip_none_deep(value: Any) -> Any:
    """递归移除 ``None``，避免 Runner/WebUI 用 tomlkit 落盘时 ConvertError。"""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, nested in value.items():
            if nested is None:
                continue
            stripped = _strip_none_deep(nested)
            if stripped is None:
                continue
            cleaned[key] = stripped
        return cleaned
    if isinstance(value, list):
        return [_strip_none_deep(item) for item in value if item is not None]
    return value


def _dump_config_for_persist(config: Mapping[str, Any]) -> dict[str, Any]:
    """生成可写回 config.toml 的配置（tomlkit 不支持 ``None``）。"""
    validated = validate_plugin_config(RssReaderPluginConfig, config)
    dumped = validated.model_dump(mode="python", exclude_none=True)
    return _strip_none_deep(dumped)


# --------------------------------------------------------------------------- #
# 配置模型
# --------------------------------------------------------------------------- #


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default=CURRENT_CONFIG_VERSION, description="配置版本")


class RssFeedConfig(PluginConfigBase):
    stream_id: str = Field(
        default="",
        description="所属聊天流 ID（与 rss.streams 中 stream_id 对应；可用 /rss_stream_id 获取）",
        json_schema_extra={
            "label": "聊天流 ID",
            "placeholder": "0123456789abcdef0123456789abcdef",
        },
    )
    url: str = Field(
        default="",
        description="RSS 订阅地址",
        json_schema_extra={
            "label": "RSS URL",
            "placeholder": "https://example.com/feed.xml",
        },
    )
    name: str = Field(
        default="",
        description="订阅源显示名称（可选）",
        json_schema_extra={
            "label": "显示名称",
            "placeholder": "Example News",
        },
    )


class StreamRssConfig(PluginConfigBase):
    stream_id: str = Field(
        default="",
        description="聊天流 ID（32 位小写十六进制 MD5，与 session_id 相同；可在目标聊天中发送 /rss_stream_id 获取）",
        json_schema_extra={
            "label": "聊天流 ID",
            "placeholder": "0123456789abcdef0123456789abcdef",
        },
    )
    enabled: bool = Field(
        default=True,
        description="是否为此聊天流启用 RSS",
        json_schema_extra={"label": "启用"},
    )


class RssSectionConfig(PluginConfigBase):
    __ui_label__ = "RSS 设置"
    __ui_icon__ = "rss"
    __ui_order__ = 1

    poll_interval_seconds: int | None = Field(
        default=None,
        ge=30,
        description="RSS 全局拉取间隔（秒）；留空使用插件内置默认",
        json_schema_extra={"placeholder": str(DEFAULT_POLL_INTERVAL_SECONDS)},
    )
    request_timeout_seconds: float | None = Field(
        default=None,
        ge=1.0,
        description="HTTP 请求超时（秒）；留空使用插件内置默认",
        json_schema_extra={"placeholder": str(int(DEFAULT_REQUEST_TIMEOUT_SECONDS))},
    )
    max_concurrent_fetches: int | None = Field(
        default=None,
        ge=1,
        description="并行拉取上限；留空使用插件内置默认",
        json_schema_extra={"placeholder": str(DEFAULT_MAX_CONCURRENT_FETCHES)},
    )
    max_items_per_feed: int | None = Field(
        default=None,
        ge=1,
        description="每个 feed 缓存/返回的最大条目数；留空使用插件内置默认",
        json_schema_extra={"placeholder": str(DEFAULT_MAX_ITEMS_PER_FEED)},
    )
    max_seen_ids_per_feed: int | None = Field(
        default=None,
        ge=50,
        description="每个 feed 保留的已见条目 ID 上限；留空使用插件内置默认",
        json_schema_extra={"placeholder": str(DEFAULT_MAX_SEEN_IDS_PER_FEED)},
    )
    proactive_intent_template: str = Field(
        default="",
        description="proactive.trigger 的 intent 模板；留空使用插件内置默认",
        json_schema_extra={"placeholder": DEFAULT_PROACTIVE_INTENT_TEMPLATE},
    )
    context_visible_text_template: str = Field(
        default="",
        description="context.append 的 visible_text 模板；留空使用插件内置默认",
        json_schema_extra={"placeholder": DEFAULT_CONTEXT_VISIBLE_TEXT_TEMPLATE},
    )
    context_preamble_template: str = Field(
        default="",
        description="注入上下文的引导语（条目列表前）；留空使用插件内置默认",
        json_schema_extra={"placeholder": DEFAULT_CONTEXT_PREAMBLE_TEMPLATE},
    )
    item_template: str = Field(
        default="",
        description="单条 RSS 条目的 Markdown 格式；留空使用插件内置默认",
        json_schema_extra={"placeholder": DEFAULT_ITEM_TEMPLATE},
    )
    item_separator: str | None = Field(
        default=None,
        description="多条目之间的分隔符；留空使用插件内置默认（换行）",
        json_schema_extra={"placeholder": DEFAULT_ITEM_SEPARATOR},
    )
    allow_private_networks: bool | None = Field(
        default=None,
        description="是否允许抓取内网 / 环回 / 保留地址。默认关闭（SSRF 防护），仅在确有需要时开启",
    )
    allow_http: bool | None = Field(
        default=None,
        description="是否允许 http:// 订阅地址。默认关闭，仅允许 https",
    )
    streams: list[StreamRssConfig] = Field(
        default_factory=list,
        description="按聊天流启用 RSS（stream_id + enabled）",
        json_schema_extra={"label": "聊天流"},
    )
    feeds: list[RssFeedConfig] = Field(
        default_factory=list,
        description="RSS 源列表（通过 stream_id 关联到上方聊天流）",
        json_schema_extra={"label": "RSS 源"},
    )


class RssReaderPluginConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    rss: RssSectionConfig = Field(default_factory=RssSectionConfig)


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #


class RssReaderPlugin(MaiBotPlugin):
    config_model = RssReaderPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._plugin_dir = Path(__file__).resolve().parent
        self._state: RssState | None = None
        self._bot_feeds: BotFeedsStore | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._poll_stop = asyncio.Event()
        self._fetch_semaphore: asyncio.Semaphore | None = None

    def _rss(self) -> EffectiveRssConfig:
        return resolve_effective_rss_config(self.config.rss)

    def _feed_fetch_policy(self) -> FeedFetchPolicy:
        cfg = self._rss()
        return FeedFetchPolicy(
            allow_private_networks=cfg.allow_private_networks,
            allow_http=cfg.allow_http,
        )

    def normalize_plugin_config(
        self, config_data: Mapping[str, Any] | None
    ) -> tuple[dict[str, Any], bool]:
        sanitized = _coerce_webui_blank_optionals(dict(config_data or {}), RssReaderPluginConfig)
        normalized, changed = super().normalize_plugin_config(sanitized)
        migrated, migrated_changed = _migrate_legacy_baked_defaults(normalized)
        flattened, flattened_changed = _migrate_nested_stream_feeds(migrated)
        persistable = _dump_config_for_persist(flattened)
        return persistable, changed or migrated_changed or flattened_changed or persistable != flattened

    async def on_load(self) -> None:
        self._state = RssState(self._plugin_dir / "rss_state.json")
        self._bot_feeds = BotFeedsStore(self._plugin_dir / "rss_bot_feeds.json")
        self._fetch_semaphore = asyncio.Semaphore(max(1, self._rss().max_concurrent_fetches))
        self._restart_poll_loop()
        self.ctx.logger.info("麦麦 RSS 阅读器插件已加载")

    async def on_unload(self) -> None:
        self._stop_poll_loop()
        self.ctx.logger.info("麦麦 RSS 阅读器插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del config_data, version
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self._fetch_semaphore = asyncio.Semaphore(max(1, self._rss().max_concurrent_fetches))
            self._restart_poll_loop()
            self.ctx.logger.info("麦麦 RSS 阅读器配置已更新")

    def _is_enabled(self) -> bool:
        return bool(self.config.plugin.enabled)

    def _max_seen_ids(self) -> int:
        return max(50, self._rss().max_seen_ids_per_feed)

    def _feeds_from_config(self, stream_id: str) -> list[tuple[str, str]]:
        normalized = (stream_id or "").strip()
        if not normalized:
            return []
        if not any(stream.stream_id.strip() == normalized for stream in self.config.rss.streams):
            return []
        return [
            (feed.url.strip(), feed.name.strip())
            for feed in self.config.rss.feeds
            if feed.stream_id.strip() == normalized and feed.url.strip()
        ]

    def _feeds_from_bot(self, stream_id: str) -> list[tuple[str, str]]:
        if self._bot_feeds is None:
            return []
        return self._bot_feeds.get_feeds(stream_id)

    def _effective_feeds_for_stream(self, stream_id: str) -> list[tuple[str, str]]:
        """合并 config（需 enabled）与 bot 自添加订阅，URL 去重，config 名称优先。"""
        normalized = (stream_id or "").strip()
        if not normalized:
            return []
        merged: dict[str, str] = {}
        stream_enabled = any(
            stream.stream_id.strip() == normalized and stream.enabled
            for stream in self.config.rss.streams
        )
        if stream_enabled:
            for feed in self.config.rss.feeds:
                if feed.stream_id.strip() != normalized:
                    continue
                url = feed.url.strip()
                if url:
                    merged[url] = feed.name.strip()
        for url, name in self._feeds_from_bot(normalized):
            if url not in merged:
                merged[url] = name
        return list(merged.items())

    def _all_poll_stream_ids(self) -> set[str]:
        stream_ids: set[str] = set()
        for stream in self.config.rss.streams:
            if stream.enabled:
                stream_id = stream.stream_id.strip()
                if stream_id:
                    stream_ids.add(stream_id)
        if self._bot_feeds is not None:
            stream_ids.update(self._bot_feeds.all_stream_ids())
        return stream_ids

    def _has_effective_feed_url(self, stream_id: str, url: str) -> bool:
        normalized_url = url.strip()
        return any(feed_url == normalized_url for feed_url, _ in self._effective_feeds_for_stream(stream_id))

    def _collect_feed_index(self) -> dict[str, list[tuple[str, str]]]:
        index: dict[str, list[tuple[str, str]]] = {}
        for stream_id in self._all_poll_stream_ids():
            for url, name in self._effective_feeds_for_stream(stream_id):
                index.setdefault(url, []).append((stream_id, name))
        return index

    def _restart_poll_loop(self) -> None:
        self._stop_poll_loop()
        if not self._is_enabled():
            return
        self._poll_stop = asyncio.Event()
        self._poll_task = asyncio.create_task(self._poll_loop())

    def _stop_poll_loop(self) -> None:
        if self._poll_task is not None:
            self._poll_stop.set()
            self._poll_task.cancel()
            self._poll_task = None

    async def _poll_loop(self) -> None:
        while not self._poll_stop.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ctx.logger.error("RSS 轮询异常: %s", exc, exc_info=True)
            interval = max(30, self._rss().poll_interval_seconds)
            try:
                await asyncio.wait_for(self._poll_stop.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                continue

    async def _poll_once(self) -> None:
        if not self._is_enabled() or self._state is None:
            return
        feed_index = self._collect_feed_index()
        if not feed_index:
            return

        rss_cfg = self._rss()
        timeout = rss_cfg.request_timeout_seconds
        max_items = rss_cfg.max_items_per_feed
        new_by_stream: dict[str, list[RssItem]] = {}
        policy = self._feed_fetch_policy()

        async with build_feed_http_client(policy=policy, timeout=timeout) as client:
            tasks = [
                self._fetch_and_diff(
                    url, bindings, client, timeout=timeout, max_items=max_items, policy=policy
                )
                for url, bindings in feed_index.items()
            ]
            for result in await asyncio.gather(*tasks, return_exceptions=True):
                if isinstance(result, Exception):
                    self.ctx.logger.warning("RSS feed 拉取失败: %s", result)
                    continue
                for stream_id, items in result.items():
                    new_by_stream.setdefault(stream_id, []).extend(items)

        self._state.save()

        notify_tasks = [
            self._notify_stream(stream_id, sort_items_by_published(items))
            for stream_id, items in new_by_stream.items()
            if items
        ]
        if notify_tasks:
            await asyncio.gather(*notify_tasks, return_exceptions=True)

    async def _fetch_and_diff(
        self,
        url: str,
        bindings: list[tuple[str, str]],
        client: httpx.AsyncClient,
        *,
        timeout: float,
        max_items: int,
        policy: FeedFetchPolicy,
    ) -> dict[str, list[RssItem]]:
        assert self._state is not None
        feed_name = next((name for _, name in bindings if name), "")
        semaphore = self._fetch_semaphore or asyncio.Semaphore(1)

        async with semaphore:
            try:
                items = await fetch_feed(
                    url, timeout=timeout, feed_name=feed_name, client=client, policy=policy
                )
            except Exception as exc:
                self.ctx.logger.warning("拉取 RSS 失败 url=%s: %s", url, exc)
                return {}

        new_items, _ = self._state.update_feed(
            url, items, max_items=max_items, max_seen_ids=self._max_seen_ids()
        )
        if not new_items:
            return {}
        return {stream_id: list(new_items) for stream_id, _ in bindings}

    async def _notify_stream(self, stream_id: str, new_items: list[RssItem]) -> None:
        if not new_items:
            return
        rss_cfg = self._rss()
        feed_names = ", ".join(sorted({item.feed_name or item.feed_url for item in new_items}))
        count = len(new_items)

        content = render_preamble(rss_cfg, count=count, feed_names=feed_names, stream_id=stream_id)
        content += format_items(new_items, rss_cfg)
        visible = render_visible_text(rss_cfg, count=count, feed_names=feed_names, stream_id=stream_id)

        await self.ctx.maisaka.context.append(
            stream_id=stream_id,
            segments=[{"type": "text", "content": content}],
            visible_text=visible,
            source_kind="plugin:rss-reader",
        )
        await self.ctx.maisaka.proactive.trigger(
            stream_id=stream_id,
            intent=render_proactive_intent(
                rss_cfg, count=count, feed_names=feed_names, stream_id=stream_id
            ),
            reason="rss_new_items",
            metadata={
                "count": count,
                "feeds": sorted({item.feed_name or item.feed_url for item in new_items}),
                "plugin": "rss-reader",
            },
        )

    async def _refresh_stream_feeds(self, stream_id: str) -> None:
        if self._state is None:
            return
        rss_cfg = self._rss()
        timeout = rss_cfg.request_timeout_seconds
        max_items = rss_cfg.max_items_per_feed
        poll_interval = rss_cfg.poll_interval_seconds
        policy = self._feed_fetch_policy()

        async with build_feed_http_client(policy=policy, timeout=timeout) as client:
            for url, name in self._effective_feeds_for_stream(stream_id):
                if not self._state.is_stale(url, poll_interval):
                    continue
                try:
                    items = await fetch_feed(
                        url, timeout=timeout, feed_name=name, client=client, policy=policy
                    )
                    self._state.refresh_cache(
                        url,
                        items,
                        max_items=max_items,
                        max_seen_ids=self._max_seen_ids(),
                    )
                except Exception as exc:
                    self.ctx.logger.warning("刷新 RSS 失败 url=%s: %s", url, exc)
        self._state.save()

    def _collect_stream_items(
        self, stream_id: str, *, feed_name_filter: str = ""
    ) -> list[RssItem]:
        assert self._state is not None
        items: list[RssItem] = []
        name_filter = feed_name_filter.strip().lower()
        for url, configured_name in self._effective_feeds_for_stream(stream_id):
            if name_filter:
                candidates = {configured_name.lower(), url.lower()}
                if name_filter not in candidates and not any(
                    name_filter in candidate for candidate in candidates if candidate
                ):
                    continue
            items.extend(self._state.get_cached_items(url))
        return sort_items_by_published(items)

    async def _build_stream_feed_content(
        self,
        stream_id: str,
        *,
        feed_name_filter: str = "",
        keywords: str = "",
        for_bot: bool = False,
    ) -> str | None:
        if not self._effective_feeds_for_stream(stream_id):
            return None
        await self._refresh_stream_feeds(stream_id)
        items = self._collect_stream_items(stream_id, feed_name_filter=feed_name_filter)
        keyword_list = parse_keywords(keywords)
        if keyword_list:
            items = filter_items_by_keywords(items, keyword_list)
        if not items:
            if keyword_list:
                message = f"没有匹配关键词「{'、'.join(keyword_list)}」的 RSS 条目。"
            else:
                message = "当前没有可显示的 RSS 条目（可能尚未完成首次拉取）。"
            return message + (DEFAULT_QUERY_RSS_HINT if for_bot else "")
        rss_cfg = self._rss()
        max_items = rss_cfg.max_items_per_feed
        sorted_items = sort_items_by_published(items)
        truncated = len(sorted_items) > max_items
        body = format_items(sorted_items, rss_cfg, max_items=max_items)
        preamble = render_preamble(
            rss_cfg,
            count=min(len(sorted_items), max_items),
            feed_names=feed_name_filter or "全部订阅",
            stream_id=stream_id,
        )
        suffix = f"\n\n（仅显示最近 {max_items} 条）" if truncated else ""
        bot_hint = DEFAULT_QUERY_RSS_HINT if for_bot else ""
        return preamble + body + suffix + bot_hint

    def _format_feed_list_lines(self, feeds: list[tuple[str, str]]) -> str:
        if not feeds:
            return "无"
        lines: list[str] = []
        for url, name in feeds:
            label = name or url
            lines.append(f"- {label} / {url}")
        return "\n".join(lines)

    @Tool(
        "add_rss_feed",
        description="为当前聊天流添加 RSS/Atom 订阅",
        parameters=[
            ToolParameterInfo(
                name="url",
                param_type=ToolParamType.STRING,
                description="RSS/Atom 订阅地址（https）",
                required=True,
            ),
            ToolParameterInfo(
                name="name",
                param_type=ToolParamType.STRING,
                description="可选，显示名称；缺省使用 feed 标题",
                required=False,
            ),
        ],
    )
    async def handle_add_rss_feed(self, url: str, name: str = "", **kwargs: Any) -> dict[str, Any]:
        stream_id = str(kwargs.get("stream_id") or "").strip()
        if not stream_id:
            return {"content": "无法获取当前聊天流 ID，添加 RSS 失败。"}
        if self._state is None or self._bot_feeds is None:
            return {"content": "插件尚未完成加载，请稍后重试。"}

        normalized_url = url.strip()
        if not normalized_url:
            return {"content": "请提供有效的 RSS 订阅 URL。"}

        if self._has_effective_feed_url(stream_id, normalized_url):
            return {"content": f"当前聊天流已订阅该 RSS：{normalized_url}"}

        rss_cfg = self._rss()
        timeout = rss_cfg.request_timeout_seconds
        policy = self._feed_fetch_policy()
        try:
            resolved_name, items = await validate_feed_url(
                normalized_url, timeout=timeout, feed_name=name.strip(), policy=policy
            )
        except httpx.HTTPStatusError as exc:
            return {"content": f"无法拉取 RSS（HTTP {exc.response.status_code}）：{normalized_url}"}
        except httpx.RequestError as exc:
            return {"content": f"无法访问 RSS 地址：{exc}"}
        except ValueError as exc:
            return {"content": str(exc)}
        except Exception as exc:
            self.ctx.logger.warning("校验 RSS URL 失败 url=%s: %s", normalized_url, exc)
            return {"content": f"校验 RSS 失败：{exc}"}

        display_name = name.strip() or resolved_name or normalized_url
        if not self._bot_feeds.add_feed(stream_id, normalized_url, display_name):
            return {"content": f"当前聊天流已订阅该 RSS：{normalized_url}"}
        self._bot_feeds.save()

        max_items = rss_cfg.max_items_per_feed
        self._state.update_feed(
            normalized_url,
            items,
            max_items=max_items,
            max_seen_ids=self._max_seen_ids(),
        )
        self._state.save()

        return {"content": f"已为当前聊天流添加 RSS：{display_name}（{normalized_url}）"}

    @Tool(
        "query_rss_feeds",
        description=(
            "查阅当前聊天流的 RSS 订阅（内部上下文，不会发给用户；给你自己看，可按兴趣拉取原文深入了解）。"
            "按时间排序，支持按源名与关键词过滤。"
        ),
        parameters=[
            ToolParameterInfo(
                name="feed_name",
                param_type=ToolParamType.STRING,
                description="可选，按订阅源名称或 URL 过滤",
                required=False,
            ),
            ToolParameterInfo(
                name="keywords",
                param_type=ToolParamType.STRING,
                description="可选，空格或逗号分隔的关键词（任意匹配即保留）",
                required=False,
            ),
        ],
    )
    async def handle_query_rss_feeds(
        self, feed_name: str = "", keywords: str = "", **kwargs: Any
    ) -> dict[str, Any]:
        stream_id = str(kwargs.get("stream_id") or "").strip()
        content = await self._build_stream_feed_content(
            stream_id, feed_name_filter=feed_name, keywords=keywords, for_bot=True
        )
        if content is None:
            return {"content": "当前聊天流没有 RSS 订阅。"}
        return {"content": content}

    @Command(
        "rss_stream_id",
        description="查看当前聊天流的 stream_id（用于配置 RSS 订阅）",
        pattern=r"^/rss_stream_id$",
    )
    async def handle_rss_stream_id(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = str(kwargs.get("stream_id") or "").strip()
        if not stream_id:
            return False, "无法获取当前聊天流 ID", 2
        message = (
            f"当前聊天流 stream_id：\n{stream_id}\n\n"
            "说明：这是 32 位小写十六进制字符串（MD5），与 session_id 相同。"
            "请将其填入 config.toml 的 rss.streams / rss.feeds，并在 streams 中将 enabled 设为 true。"
        )
        await self.ctx.send.text(message, stream_id)
        return True, "已发送 stream_id", 2

    @Command("rss", description="查看本聊天流 RSS 订阅", pattern=r"^/rss(?:\s+.*)?$")
    async def handle_rss(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = str(kwargs.get("stream_id") or "").strip()
        if not self._effective_feeds_for_stream(stream_id):
            return False, "", 0
        content = await self._build_stream_feed_content(stream_id)
        if not content:
            return False, "", 0
        await self.ctx.send.text(content, stream_id)
        return True, "已发送 RSS 订阅内容", 2

    @Command("rss_list", description="列出本聊天流的 RSS 订阅来源", pattern=r"^/rss_list$")
    async def handle_rss_list(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = str(kwargs.get("stream_id") or "").strip()
        bot_name = str(await self.ctx.config.get("bot.nickname", "麦麦"))
        config_section = self._format_feed_list_lines(self._feeds_from_config(stream_id))
        bot_section = self._format_feed_list_lines(self._feeds_from_bot(stream_id))
        message = (
            f"【配置文件中的 RSS 订阅】\n{config_section}\n\n"
            f"【{bot_name} 自行添加的 RSS 订阅】\n{bot_section}"
        )
        await self.ctx.send.text(message, stream_id)
        return True, "已发送 RSS 订阅列表", 2


def create_plugin() -> RssReaderPlugin:
    return RssReaderPlugin()
