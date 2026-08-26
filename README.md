# AstrBot 洁图

本插件是 AstrBot 的同人图管理插件。第一阶段从 Danbooru 获取明日方舟安洁莉娜的 Safe 图片，并将图片和完整英文 tags 保存到 SQLite。

## 指令

- `洁图`：从数据库中随机选择包含 `angelina_(arknights)` 的本地图片发送。
- `洁图统计`：使用 Pillow 生成现有风格的统计图。八个目标 tag 的数量、百分比、扇区图片和最近一次更新的 `(+n)` 均来自 SQLite。

## 配置

- `auto_update_enabled`：自动更新开关，默认 `true`。
- `update_interval_minutes`：更新间隔分钟数，默认 `60`。
- `update_count`：每次最多新增图片数，默认 `20`，有效范围 `1-200`。
- `max_images`：统一图库最多保存图片数，默认 `10000`。
- `download_concurrency`：图片下载并发数，默认 `2`。

## 运行数据

运行数据位于 AstrBot 的 `data/plugin_data`：

```text
data/plugin_data/astrbot_plugin_gallery/
├── images/
│   ├── 11919554.jpg
│   └── manual.jpg
├── gallery.db
└── update_state.json
```

图片只保存于 `images/` 根目录。直接复制到该目录的图片会在启动或更新前同步入库；文件名为纯数字（例如 `11919554.jpg`）时会记录 post ID，否则 post ID 为空。手动图片默认没有 tag，因此不会出现在“洁图”随机结果中。数据库记录指向的文件被删除后会自动清理，旧的自动图片会按 `downloaded_at` 淘汰，人工图片不会被主动删除。

## Danbooru API

API Adapter 固定请求：

```text
tags=angelina_(arknights)+rating:s
search[order]=created_at
```

只保存可用、未删除、未封禁的 Safe 帖子。字段包括 post ID、原图 URL、file extension、Danbooru md5、created_at 和完整的 `tag_string.split()`。八个目标 tag 仅用于数据库统计，不会分别请求 API。

## 数据模型

```text
images      图片文件、post_id、hash、来源 URL、时间
tags        Danbooru 原始英文 tag
image_tags  图片与 tag 的多对多关系
```

数据库使用 `aiosqlite`，post ID 唯一，hash 作为第二层去重依据。

## 更新状态

`update_state.json` 只保存最近一次更新摘要：

```json
{
  "last_update": "2026-08-22 10:30:00",
  "last_added_total": 20,
  "last_added_by_tag": {
    "angelina_(arknights)": 20
  }
}
```

数据库才是图库事实来源；统计图右上角总计是八个目标 tag 数量之和，不是所有本地文件数量。
