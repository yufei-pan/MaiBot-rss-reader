# Changelog

本文件记录 MaiBot-rss-reader 的版本变更。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

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
