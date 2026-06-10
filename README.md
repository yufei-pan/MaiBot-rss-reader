# 麦麦 RSS 阅读器

MaiBot 第三方插件：订阅 RSS 源，将新内容注入 Maisaka 上下文并触发主动处理；提供 `query_rss_feeds` 工具与 `/rss` 命令供麦麦与用户查阅订阅。

> *没错是给麦麦提供RSS阅读器不是把麦麦当作RSS阅读器*

通过外部RSS来触发 ***麦麦思考*** 的方式，达成**不尴尬的**，**自然的**，**符合人设的**，**不打断上下文的**话题生成，新闻读取，追踪现实的效果。

同时如果上下文足够长，你可以随时问麦麦rss推送了什么新闻，让麦麦来帮你总结你的feed内容。

> [!WARNING]
> 因为绝大多数的RSS网页链接而非文本正文，因此麦麦需要某种获取网页内容的方式来查看详情。


> 推荐给麦麦添加网页浏览能力，比如mcp-server-fetch，或者（和）playwright。
>
> 麦麦联网插件（search plugin） 可以凑活着用，但是因为要过搜索引擎不一定能直接拉取到目标网页

> [!NOTE]
> 秉承着能力分离的理念，本插件暂时没有添加网页抓取能力的打算。

同时，本插件也有让麦麦**检索RSS推送**和让麦麦**自行添加RSS推送**的功能。

> [!NOTE]
> 和[塑料内存条](https://github.com/yufei-pan/MaiBot-plastic-memory-plugin)一起使用可以做到如果麦麦自行判断这个新闻不应该现在马上打扰你时，把新闻记下来以后再聊的效果。

## 功能

- 按可配置间隔全局拉取 RSS（客户端 pull）
- 按 **聊天流（stream_id）** 配置多个 RSS 源
- 新内容：聚合、排序、格式化后注入 Maisaka 上下文，并触发一次 proactive（每 stream 每轮最多一次）
- 麦麦可通过 `query_rss_feeds` 工具主动查询完整订阅（支持关键词过滤）
- 麦麦可通过 `add_rss_feed` 工具为当前聊天流添加订阅（写入本地 `rss_bot_feeds.json`，不修改 `config.toml`）
- 用户可在已配置的聊天流中使用 `/rss` 命令查看订阅
- 用户可在任意聊天流发送 `/rss_list` 查看配置文件与麦麦自行添加的订阅列表
- 用户可在任意聊天流发送 `/rss_stream_id` 获取该流的 ID（便于填写配置）

## 安装

1. 将本目录放入 MaiBot 的 `plugins/`（或通过 WebUI 安装插件）
2. 确保 Host 已安装 `maibot-plugin-sdk >= 2.5.1`
3. **重启 MaiBot 一次**——`_manifest.json` 中声明的 `feedparser` 会由 Host 自动安装到运行环境
4. 在 WebUI 或 `config.toml` 中启用插件并配置 RSS 源

若日志仍报 `No module named 'feedparser'`，可在 MaiBot 同一 Python 环境中手动安装：

```bash
pip install 'feedparser>=6.0.0'
```

## 配置说明

### 获取 `stream_id`

`stream_id` 必须是 Host 中**已存在**的聊天流 ID，且与 `session_id` 相同。


| 属性  | 说明                                  |
| --- | ----------------------------------- |
| 类型  | 字符串                                 |
| 格式  | **32 位小写十六进制 MD5**（字符集 `0-9`、`a-f`） |
| 示例  | `a1b2c3d4e5f6789012345678abcdef01`  |


获取方式（任选其一）：

1. **推荐**：在目标私聊或群聊中发送 `/rss_stream_id`，插件会回复当前聊天的 stream_id
2. WebUI「推理过程 → 回复器」调试信息中查看
3. MaiBot 日志 / `ctx.chat.get_all_streams()`（开发调试）

#### `/rss_stream_id` 的风险说明

该命令会把当前聊天的内部 stream_id 发给**触发命令的会话**（群聊里通常全群可见）。风险总体较低：

- stream_id 是内部哈希标识，**不直接包含** QQ 号等平台原始 ID

### `config.toml` 示例

```toml
[rss]
poll_interval_seconds = 300
max_seen_ids_per_feed = 500   # 每个 feed 已见 ID 上限，防止 rss_state.json 无限增长

# 第一个聊天流：订阅两个 RSS 源
[[rss.streams]]
stream_id = "0123456789abcdef0123456789abcdef"  # 32 位 hex，用 /rss_stream_id 获取真实值
enabled = true

  [[rss.streams.feeds]]
  url = "https://spectrum.ieee.org/customfeeds/feed/all-topics/rss"
  name = "IEEE Spectrum 首页"

  [[rss.streams.feeds]]
  url = "https://openai.com/news/rss.xml"
  name = "OpenAI 新闻"

# 第二个聊天流：另一个群/私聊
[[rss.streams]]
stream_id = "fedcba9876543210fedcba9876543210"
enabled = true

  [[rss.streams.feeds]]
  url = "http://news.mit.edu/rss/topic/artificial-intelligence2"
  name = "MIT 新闻 - AI"
```

### 可配置模板（`[rss]` 下）


| 字段                              | 占位符                                                            |
| ------------------------------- | -------------------------------------------------------------- |
| `proactive_intent_template`     | `{count}`, `{feed_names}`, `{stream_id}`                       |
| `context_visible_text_template` | `{count}`, `{feed_names}`, `{stream_id}`                       |
| `context_preamble_template`     | `{count}`, `{feed_names}`, `{stream_id}`                       |
| `item_template`                 | `{feed_name}`, `{title}`, `{link}`, `{summary}`, `{published}` |
| `item_separator`                | 无                                                              |


模板使用 `str.replace` 渲染，占位符格式为 `{key}`。

## 行为说明

- **首次拉取**：某 feed 第一次被拉取时只建立基线（记录已见条目），不会触发 proactive，避免启动时轰炸用户
- **新内容通知**：仅对真正的新条目注入上下文并触发 proactive；intent 为建议式语气，由麦麦自行决定是否告知用户
- **plastic-memory**：本插件不依赖便利贴插件；默认可在 `proactive_intent_template` 中提示麦麦可自行使用备忘工具
- **未配置 stream**：`/rss` 命令静默无响应；`query_rss_feeds` 返回「没有 RSS 订阅」提示
- **双来源订阅**：`config.toml` 中的订阅与麦麦通过 `add_rss_feed` 添加的订阅在运行时合并；后者保存在 `rss_bot_feeds.json`（已 gitignore）
- `**rss_state.json` 体积**：`items` 缓存按 `max_items_per_feed` 有界；`seen_ids` 按 `max_seen_ids_per_feed`（默认 500）裁剪，长期运行体量可预测

### 工具与命令


| 名称                | 说明                                                         |
| ----------------- | ---------------------------------------------------------- |
| `add_rss_feed`    | 校验 URL 后为当前聊天流添加 RSS；重复 URL 返回「已订阅」                        |
| `query_rss_feeds` | 查询当前流订阅；可选 `feed_name`、`keywords`（空格/逗号分隔，任意关键词匹配标题/摘要等字段） |
| `/rss`            | 向用户发送当前流合并后的订阅内容                                           |
| `/rss_list`       | 分开展示配置文件订阅与麦麦自行添加的订阅                                       |
| `/rss_stream_id`  | 返回当前聊天流 `stream_id`                                        |


## 目录结构

单文件插件，所有逻辑在 `plugin.py` 中。运行时数据：


| 文件                   | 用途                            |
| -------------------- | ----------------------------- |
| `rss_state.json`     | 各 feed 的缓存条目与已见 ID（gitignore） |
| `rss_bot_feeds.json` | 麦麦通过工具添加的订阅（gitignore）        |


## 开发与测试

```bash
cd Maibot-rss-reader
python3 -m venv .venv && source .venv/bin/activate
pip install feedparser httpx pytest
# 若在 mai-bot-plugins monorepo 内，conftest 会自动找到 ../maibot-plugin-sdk；
# 否则请：pip install maibot-plugin-sdk
pytest -q
```

## 许可证

MIT