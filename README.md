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
- `proxy`：Danbooru API 与图片下载使用的代理，例如 `http://127.0.0.1:7890`。留空时回退到 `HTTPS_PROXY` / `ALL_PROXY` / `HTTP_PROXY` 环境变量；两者都没有则直连（国内网络通常不可用，更新会失败）。支持带认证的地址：`http://user:pass@host:port`。
- `cookie`：Cloudflare 验证 Cookie，默认空。仅当出口 IP 被 Cloudflare 拦截时填写 `cf_clearance=...`（可连同 `__cf_bm`）。Cookie 与出口 IP、User-Agent 绑定，换代理后需重新获取。
- `login`：Danbooru 用户名，默认空。与 `api_key` 同时填写后启用 HTTP Basic 认证。
- `api_key`：Danbooru API Key，默认空。在 Danbooru 设置页获取，用于提高接口速率上限，**不影响 Cloudflare 校验**。

### 容器部署的代理地址

插件跑在 Docker 内时，`127.0.0.1` 指容器自身，必须填写容器能访问到的宿主机地址：

- 容器网关：容器内执行 `ip route | awk '/default/{print $3}'`，常见为 `172.17.0.1` 或 `172.19.0.1`；
- 或 `host.docker.internal`（compose 需添加 `extra_hosts: ["host.docker.internal:host-gateway"]`）。

代理需监听 `0.0.0.0` 或容器可达的网卡地址。只绑定某个 docker 网桥 IP（例如 `172.18.0.1`）时，位于其他 docker 网络的容器通常连不上，会报 `DanbooruConnectionError: ConnectError`。

## 运行数据

运行数据位于 AstrBot 的 `data/plugin_data`：

```text
data/plugin_data/astrbot_plugin_gallery/
├── images/
│   ├── 11919554.jpg
│   └── manual.jpg
├── gallery.db
├── danbooru_cursor.json
└── update_state.json
```

图片只保存于 `images/` 根目录。直接复制到该目录的图片会在启动或更新前同步入库；文件名为纯数字（例如 `11919554.jpg`）时会记录 post ID，否则 post ID 为空。手动图片默认没有 tag，因此不会出现在“洁图”随机结果中。数据库记录指向的文件被删除后会自动清理，旧的自动图片会按 `downloaded_at` 淘汰，人工图片不会被主动删除。

## Danbooru API

API Adapter 固定请求：

```text
tags=angelina_(arknights)+rating:g,s
search[order]=id_desc
```

Danbooru 已把旧的 `safe` 拆成 `general` 与 `sensitive`，只查 `rating:s` 会漏掉全部 general 图（约占核心 tag 的 53%），因此必须同时请求 `rating:g,s`。

只保存可用、未删除、未封禁的安全级别帖子。字段包括 post ID、原图 URL、file extension、Danbooru md5、created_at 和完整的 `tag_string.split()`。八个目标 tag 仅用于数据库统计，不会分别请求 API。

游标文件记录所属查询条件；查询条件变化时旧游标自动作废，历史会重新遍历一次，便于换用新查询后回填。

请求由 `curl_cffi` 以 Chrome 指纹发出，不覆盖 `user-agent`；相邻请求间隔至少 1.2 秒，失败按 1.5s / 3s 退避重试 3 次。配置了 `login` + `api_key` 时使用 HTTP Basic 头认证，密钥不会出现在 URL 里。

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
  },
  "last_error": null,
  "consecutive_failures": 0
}
```

数据库才是图库事实来源；统计图右上角总计是去重后的图片张数，八个目标 tag 的分项存在重复计数（一张图可同时属于多个 tag），因此分项之和会大于总计。`last_update` 只在请求成功时刷新，`last_error` 与 `consecutive_failures` 记录连续失败，便于区分“没有新图”和“更新失败”。

## 故障排查

- `Danbooru returned a Cloudflare challenge`：先确认没有自定义 `user-agent`。curl_cffi 的 impersonation 会带上与 TLS 指纹匹配的 UA，一旦被覆盖成别的 Chrome 版本，Cloudflare 会 100% 拦截（实测同一代理：带 UA 0/4 通过，去掉 UA 4/4 通过）。
- 换代理后仍被拦：优先更换干净的出口 IP；仍不行就用同一个代理在浏览器打开 danbooru.donmai.us 通过人机验证，把 `cf_clearance` Cookie 填进 `cookie` 配置。Cookie 与出口 IP 绑定，换代理后需重新获取。
- `DanbooruConnectionError` / `ConnectError`：代理不可达。Docker 部署时 `127.0.0.1` 指向容器自身，应改用宿主机网关 IP 或 `host.docker.internal`，并让代理监听 `0.0.0.0`。带认证的代理建议用 curl_cffi，httpx 在部分代理上直接 CONNECT 失败。
- `login` / `api_key` 只提升 Danbooru 应用层的速率上限，不参与 Cloudflare 校验，不能用来绕过 challenge。
- `update_state.json` 的 `last_update` 长期不刷新：请求一直在失败，直接看同一文件的 `last_error`。
