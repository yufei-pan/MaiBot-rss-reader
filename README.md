# 麦麦 RSS 阅读器

MaiBot 第三方插件：订阅 RSS 源，将新内容注入 Maisaka 上下文并触发主动处理；提供 `query_rss_feeds` 工具与 `/rss` 命令供麦麦与用户查阅订阅。

> *没错是给麦麦**提供** RSS 阅读器不是把麦麦**当作** RSS 阅读器*

通过外部RSS来触发 ***麦麦思考*** 的方式，达成**不尴尬的**，**自然的**，**符合人设的**，**不打断上下文的**话题生成，新闻读取，追踪现实的效果。

同时如果上下文足够长，你可以随时问麦麦rss推送了什么新闻，让麦麦来帮你总结你的feed内容。

> [!WARNING]
> 因为绝大多数的RSS是网页链接而非文本正文，因此麦麦需要某种获取网页内容的方式来查看详情。

> 推荐给麦麦添加网页浏览能力，比如 [maibot-fetch-url-plugin](https://github.com/yufei-pan/maibot-fetch-url-plugin)，mcp-server-fetch，或者（和）playwright。
>
> 麦麦联网插件（search plugin） 可以凑活着用，但是因为要过搜索引擎不一定能直接拉取到目标网页

> [!NOTE]
> 秉承着能力分离的理念，本插件暂时没有添加网页抓取能力的打算。

同时，本插件也有让麦麦**检索 RSS 推送**和让麦麦**自行添加 RSS 推送**的功能。

提示：你也可以不手动配置RSS流，用自然语言询问麦麦有什么想关注的RSS，让麦麦自己决定自己想看什么。（需要联网搜索能力）

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

### `config.toml` 简单示例

`[rss]` 下的数值与模板字段**留空或不写**即使用插件内置默认；仅在你需要覆盖时再填写。插件升级后若内置默认变更，留空字段会自动跟随，无需手动改配置。

订阅采用**扁平结构**：`rss.streams` 声明聊天流开关，`rss.feeds` 通过 `stream_id` 关联 RSS 源（WebUI 可直接编辑）。

```toml
[rss]
# poll_interval_seconds =          # 留空=内置默认 300
# max_seen_ids_per_feed =            # 留空=内置默认 500

[[rss.streams]]
stream_id = "0123456789abcdef0123456789abcdef"
enabled = true

[[rss.feeds]]
stream_id = "0123456789abcdef0123456789abcdef"
url = "https://spectrum.ieee.org/customfeeds/feed/all-topics/rss"
name = "IEEE Spectrum 首页"
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

### 出站安全（SSRF 防护）

插件会定时拉取 RSS 并将内容写入 Maisaka 内部上下文。默认启用出站 URL 安全策略：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `allow_private_networks` | `false` | 是否允许抓取内网 / 环回 / 链路本地 / 云 metadata 等保留地址 |
| `allow_http` | `false` | 是否允许 `http://` 订阅（默认仅 `https://`） |

- 添加订阅（`add_rss_feed`）、定时轮询、`/rss` 刷新均受同一策略约束
- 每次 HTTP 请求（含重定向各跳）都会重复校验目标地址
- 确有需要访问内网 RSS 或 http 源时，请在配置中**显式**开启对应开关

若 `rss_bot_feeds.json` 中已有被拦截的恶意 URL，轮询会失败并在日志中 warning；可手动编辑该文件删除。

## 行为说明

- **首次拉取**：某 feed 第一次被拉取时只建立基线（记录已见条目），不会触发 proactive，避免启动时轰炸用户
- **新内容通知**：仅对真正的新条目注入内部上下文并触发 proactive；默认 intent 明确说明 RSS **不会直接发给用户**，仅供麦麦自己阅读，鼓励按需抓取原文、自主决定是否分享
- **[塑料内存条](https://github.com/yufei-pan/MaiBot-plastic-memory-plugin)**：本插件不依赖便利贴插件；可与塑料内存条配合，麦麦若判断不宜立刻打扰用户，可先记下稍后再聊
- **未配置 stream**：`/rss` 命令静默无响应；`query_rss_feeds` 返回「没有 RSS 订阅」提示
- **双来源订阅**：`config.toml` 中的订阅与麦麦通过 `add_rss_feed` 添加的订阅在运行时合并；后者保存在 `rss_bot_feeds.json`（已 gitignore）
- **`rss_state.json` 体积**：`items` 缓存按 `max_items_per_feed` 有界；`seen_ids` 按 `max_seen_ids_per_feed`（默认 500）裁剪，长期运行体量可预测

### 工具与命令


| 名称                | 说明                                                            |
| ----------------- | ------------------------------------------------------------- |
| `add_rss_feed`    | 校验 URL 后为当前聊天流添加 RSS；重复 URL 返回「已订阅」                           |
| `query_rss_feeds` | 麦麦自行查阅当前流 RSS（内部上下文，不会发给用户）；返回内容含提示（鼓励抓取原文、按己意行动）；可选 `feed_name`、`keywords` |
| `/rss`            | 向用户发送当前流合并后的订阅内容                                              |
| `/rss_list`       | 分开展示配置文件订阅与麦麦自行添加的订阅                                          |
| `/rss_stream_id`  | 返回当前聊天流 `stream_id`                                           |


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
