# Changelog

本文件记录 MaiBot-rss-reader 的版本变更。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 变更

- manifest 补声明运行期依赖 `httpx>=0.28.0`：`plugin.py` 直接 `import httpx` 抓取订阅源，此前只是碰巧由宿主环境提供
- manifest 兼容上限改为实际验证过的范围：宿主 `max_version` 1.99.99 → **1.3.99**，SDK `max_version` 2.99.99 → **2.14.99**（宿主 1.3.8 + SDK 2.12.0 上实际运行，SDK 2.14.0 上测试通过）。插件仓库审核不再接受「永远兼容」式的上限
- SDK 兼容上限 `max_version` 2.14.99 → **2.16.99**：宿主 1.3.14 + SDK 2.16.0 上测试通过（SDK 2.15 / 2.16 只新增能力，无破坏性变更）

## [0.4.3] - 2026-08-01

### 变更

- 将带注释的配置模板改为 `config.default.toml`，运行期 `config.toml` 不再入库
- 在 `create_plugin` / `on_load` 中从模板补齐或恢复 Runner 生成的空壳配置

## [0.4.2] - 2026-07-11

### 修复

- WebUI 清空可选字段时按默认值处理，避免空字符串触发校验错误

## [0.4.1] - 2026-07-11

### 修复

- 持久化 TOML 前去除 `None`，修复 WebUI 保存因 Optional 默认序列化为 `None` 而被 tomlkit 拒绝的问题

## [0.4.0] - 2026-06-13

### 新增

- 出站 RSS 拉取默认启用 SSRF 防护：拦截私网 / loopback / link-local，校验重定向跳数，默认要求 HTTPS（可显式关闭）

## [0.3.0] - 2026-06-13

### 变更

- 空配置字段升级时跟随代码默认值
- `rss.streams` 与 `rss.feeds` 扁平拆分以便 WebUI 编辑，并自动从嵌套配置迁移

## [0.2.2] - 2026-06-11

### 变更

- 明确 RSS 内容为内部上下文，不会直接发给用户
- 更新 README 中的网页浏览插件建议

## [0.2.1] - 2026-06-10

### 文档

- 补充插件用途、MCP fetch 说明与 README 排版
- 调整默认 RSS prompt（bot-first 阅读）与 feed 工具描述

## [0.2.0] - 2026-06-10

### 新增

- 首次发布：按聊天流轮询 RSS，将新条目注入 Maisaka 上下文，提供 `query_rss_feeds` 与 `/rss` 命令
- 关键词 / bot-feed 工具、测试，并将本地状态文件排除出版本控制
