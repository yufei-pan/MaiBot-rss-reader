"""麦麦 RSS 阅读器插件。

周期性拉取配置的 RSS 源，将新内容注入 Maisaka 上下文并触发主动处理；
提供 query_rss_feeds 工具与 /rss 命令供麦麦与用户查阅订阅。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any, Protocol, Sequence

import httpx

from maibot_sdk import Command, CONFIG_RELOAD_SCOPE_SELF, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

# --------------------------------------------------------------------------- #
# 默认模板
# --------------------------------------------------------------------------- #

DEFAULT_PROACTIVE_INTENT_TEMPLATE = """有 {count} 条新的 RSS 订阅内容已写入你的上下文（来源：{feed_names}）。
你可以自行决定是否查阅详情。若你认为值得告知用户，可自行组织话术并附上摘要与原文链接；
若与当前对话无关或时机不合适，也可以忽略；若需要日后参考，也可通过你已有的备忘工具（如便利贴）自行记录。"""

DEFAULT_CONTEXT_VISIBLE_TEXT_TEMPLATE = "RSS 新内容 {count} 条"
DEFAULT_CONTEXT_PREAMBLE_TEMPLATE = "【RSS 订阅】\n"
DEFAULT_ITEM_TEMPLATE = """### {title}
- 来源：{feed_name}
- 时间：{published}
- 链接：{link}
- 摘要：{summary}
"""
DEFAULT_ITEM_SEPARATOR = "\n"

_TAG_RE = re.compile(r"<[^>]+>")


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


def parse_feed_content(content: bytes, feed_url: str, feed_name: str = "") -> list[RssItem]:
    parsed = _get_feedparser().parse(content)
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
    return items


async def fetch_feed(
    url: str,
    *,
    timeout: float = 20.0,
    feed_name: str = "",
    client: httpx.AsyncClient | None = None,
) -> list[RssItem]:
    normalized_url = url.strip()
    if not normalized_url:
        return []

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(follow_redirects=True)

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
    ) -> tuple[list[RssItem], bool]:
        state = self.get_feed_state(feed_url)
        seen = set(state.seen_ids)
        trimmed = fetched_items[:max_items] if max_items > 0 else fetched_items
        state.items = [_item_to_dict(item) for item in trimmed]
        state.last_fetch = datetime.now(timezone.utc).isoformat()

        if not state.initialized:
            for item in trimmed:
                seen.add(item.id)
            state.seen_ids = list(seen)
            state.initialized = True
            self._feeds[feed_url] = state
            return [], False

        new_items = [item for item in trimmed if item.id not in seen]
        for item in new_items:
            seen.add(item.id)
        state.seen_ids = list(seen)
        self._feeds[feed_url] = state
        return new_items, bool(new_items)

    def refresh_cache(self, feed_url: str, fetched_items: list[RssItem], *, max_items: int) -> None:
        state = self.get_feed_state(feed_url)
        trimmed = fetched_items[:max_items] if max_items > 0 else fetched_items
        state.items = [_item_to_dict(item) for item in trimmed]
        state.last_fetch = datetime.now(timezone.utc).isoformat()
        seen = set(state.seen_ids)
        for item in trimmed:
            seen.add(item.id)
        state.seen_ids = list(seen)
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
# 配置模型
# --------------------------------------------------------------------------- #


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class RssFeedConfig(PluginConfigBase):
    url: str = Field(default="", description="RSS 订阅地址")
    name: str = Field(default="", description="订阅源显示名称（可选）")


class StreamRssConfig(PluginConfigBase):
    stream_id: str = Field(
        default="",
        description="聊天流 ID（32 位小写十六进制 MD5，与 session_id 相同；可在目标聊天中发送 /rss_stream_id 获取）",
    )
    enabled: bool = Field(default=True, description="是否为此聊天流启用 RSS")
    feeds: list[RssFeedConfig] = Field(default_factory=list, description="RSS 源列表")


class RssSectionConfig(PluginConfigBase):
    __ui_label__ = "RSS 设置"
    __ui_icon__ = "rss"
    __ui_order__ = 1

    poll_interval_seconds: int = Field(default=300, ge=30, description="RSS 全局拉取间隔（秒）")
    request_timeout_seconds: float = Field(default=20.0, ge=1.0, description="HTTP 请求超时（秒）")
    max_concurrent_fetches: int = Field(default=5, ge=1, description="并行拉取上限")
    max_items_per_feed: int = Field(default=30, ge=1, description="每个 feed 缓存/返回的最大条目数")
    proactive_intent_template: str = Field(
        default=DEFAULT_PROACTIVE_INTENT_TEMPLATE,
        description="proactive.trigger 的 intent 模板",
    )
    context_visible_text_template: str = Field(
        default=DEFAULT_CONTEXT_VISIBLE_TEXT_TEMPLATE,
        description="context.append 的 visible_text 模板",
    )
    context_preamble_template: str = Field(
        default=DEFAULT_CONTEXT_PREAMBLE_TEMPLATE,
        description="注入上下文的引导语（条目列表前）",
    )
    item_template: str = Field(
        default=DEFAULT_ITEM_TEMPLATE,
        description="单条 RSS 条目的 Markdown 格式",
    )
    item_separator: str = Field(default=DEFAULT_ITEM_SEPARATOR, description="多条目之间的分隔符")
    streams: list[StreamRssConfig] = Field(default_factory=list, description="按聊天流配置的 RSS 订阅")


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
        self._poll_task: asyncio.Task[None] | None = None
        self._poll_stop = asyncio.Event()
        self._fetch_semaphore: asyncio.Semaphore | None = None

    async def on_load(self) -> None:
        self._state = RssState(self._plugin_dir / "rss_state.json")
        self._fetch_semaphore = asyncio.Semaphore(max(1, self.config.rss.max_concurrent_fetches))
        self._restart_poll_loop()
        self.ctx.logger.info("麦麦 RSS 阅读器插件已加载")

    async def on_unload(self) -> None:
        self._stop_poll_loop()
        self.ctx.logger.info("麦麦 RSS 阅读器插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del config_data, version
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self._fetch_semaphore = asyncio.Semaphore(max(1, self.config.rss.max_concurrent_fetches))
            self._restart_poll_loop()
            self.ctx.logger.info("麦麦 RSS 阅读器配置已更新")

    def _is_enabled(self) -> bool:
        return bool(self.config.plugin.enabled)

    def _get_stream_config(self, stream_id: str) -> StreamRssConfig | None:
        normalized = (stream_id or "").strip()
        if not normalized:
            return None
        for stream in self.config.rss.streams:
            if stream.stream_id.strip() == normalized and stream.enabled:
                return stream
        return None

    def _collect_feed_index(self) -> dict[str, list[tuple[str, str]]]:
        index: dict[str, list[tuple[str, str]]] = {}
        for stream in self.config.rss.streams:
            if not stream.enabled:
                continue
            stream_id = stream.stream_id.strip()
            if not stream_id:
                continue
            for feed in stream.feeds:
                url = feed.url.strip()
                if not url:
                    continue
                index.setdefault(url, []).append((stream_id, feed.name.strip()))
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
            interval = max(30, int(self.config.rss.poll_interval_seconds))
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

        timeout = float(self.config.rss.request_timeout_seconds)
        max_items = int(self.config.rss.max_items_per_feed)
        new_by_stream: dict[str, list[RssItem]] = {}

        async with httpx.AsyncClient(follow_redirects=True) as client:
            tasks = [
                self._fetch_and_diff(url, bindings, client, timeout=timeout, max_items=max_items)
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
    ) -> dict[str, list[RssItem]]:
        assert self._state is not None
        feed_name = next((name for _, name in bindings if name), "")
        semaphore = self._fetch_semaphore or asyncio.Semaphore(1)

        async with semaphore:
            try:
                items = await fetch_feed(url, timeout=timeout, feed_name=feed_name, client=client)
            except Exception as exc:
                self.ctx.logger.warning("拉取 RSS 失败 url=%s: %s", url, exc)
                return {}

        new_items, _ = self._state.update_feed(url, items, max_items=max_items)
        if not new_items:
            return {}
        return {stream_id: list(new_items) for stream_id, _ in bindings}

    async def _notify_stream(self, stream_id: str, new_items: list[RssItem]) -> None:
        if not new_items:
            return
        rss_cfg = self.config.rss
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

    async def _refresh_stream_feeds(self, stream_cfg: StreamRssConfig) -> None:
        if self._state is None:
            return
        timeout = float(self.config.rss.request_timeout_seconds)
        max_items = int(self.config.rss.max_items_per_feed)
        poll_interval = int(self.config.rss.poll_interval_seconds)

        async with httpx.AsyncClient(follow_redirects=True) as client:
            for feed in stream_cfg.feeds:
                url = feed.url.strip()
                if not url or not self._state.is_stale(url, poll_interval):
                    continue
                try:
                    items = await fetch_feed(
                        url, timeout=timeout, feed_name=feed.name.strip(), client=client
                    )
                    self._state.refresh_cache(url, items, max_items=max_items)
                except Exception as exc:
                    self.ctx.logger.warning("刷新 RSS 失败 url=%s: %s", url, exc)
        self._state.save()

    def _collect_stream_items(
        self, stream_cfg: StreamRssConfig, *, feed_name_filter: str = ""
    ) -> list[RssItem]:
        assert self._state is not None
        items: list[RssItem] = []
        name_filter = feed_name_filter.strip().lower()
        for feed in stream_cfg.feeds:
            url = feed.url.strip()
            if not url:
                continue
            configured_name = feed.name.strip()
            if name_filter:
                candidates = {configured_name.lower(), url.lower()}
                if name_filter not in candidates and not any(
                    name_filter in c for c in candidates if c
                ):
                    continue
            items.extend(self._state.get_cached_items(url))
        return sort_items_by_published(items)

    async def _build_stream_feed_content(
        self, stream_id: str, *, feed_name_filter: str = ""
    ) -> str | None:
        stream_cfg = self._get_stream_config(stream_id)
        if stream_cfg is None or not stream_cfg.feeds:
            return None
        await self._refresh_stream_feeds(stream_cfg)
        items = self._collect_stream_items(stream_cfg, feed_name_filter=feed_name_filter)
        if not items:
            return "当前没有可显示的 RSS 条目（可能尚未完成首次拉取）。"
        max_items = int(self.config.rss.max_items_per_feed)
        truncated = len(items) > max_items
        body = format_items(items, self.config.rss, max_items=max_items)
        preamble = render_preamble(
            self.config.rss,
            count=min(len(items), max_items),
            feed_names=feed_name_filter or "全部订阅",
            stream_id=stream_id,
        )
        suffix = f"\n\n（仅显示最近 {max_items} 条）" if truncated else ""
        return preamble + body + suffix

    @Tool(
        "query_rss_feeds",
        description="查询当前聊天流已配置的 RSS 订阅的完整内容（按时间排序）",
        parameters=[
            ToolParameterInfo(
                name="feed_name",
                param_type=ToolParamType.STRING,
                description="可选，按订阅源名称或 URL 过滤",
                required=False,
            ),
        ],
    )
    async def handle_query_rss_feeds(self, feed_name: str = "", **kwargs: Any) -> dict[str, Any]:
        stream_id = str(kwargs.get("stream_id") or "").strip()
        content = await self._build_stream_feed_content(stream_id, feed_name_filter=feed_name)
        if content is None:
            return {"content": "当前聊天流未配置 RSS 订阅。"}
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
            "请将其填入 config.toml 的 [[rss.streams]] 对应项，并将 enabled 设为 true。"
        )
        await self.ctx.send.text(message, stream_id)
        return True, "已发送 stream_id", 2

    @Command("rss", description="查看本聊天流 RSS 订阅", pattern=r"^/rss(?:\s+.*)?$")
    async def handle_rss(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = str(kwargs.get("stream_id") or "").strip()
        stream_cfg = self._get_stream_config(stream_id)
        if stream_cfg is None or not stream_cfg.feeds:
            return False, "", 0
        content = await self._build_stream_feed_content(stream_id)
        if not content:
            return False, "", 0
        await self.ctx.send.text(content, stream_id)
        return True, "已发送 RSS 订阅内容", 2


def create_plugin() -> RssReaderPlugin:
    return RssReaderPlugin()
