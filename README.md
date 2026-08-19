<h1 align="center">AstrBot 群典插件</h1>

<p align="center">让群友的抽象发言变成经典罢！😋</p>

<p align="center">
  <a href="./CHANGELOG.md"><img src="https://img.shields.io/badge/version-1.1.0-blue" alt="Version 1.1.0"></a>
  <a href="https://github.com/AstrBotDevs/AstrBot"><img src="https://img.shields.io/badge/AstrBot-%3E%3D4.24.2%20%3C5-blue" alt="AstrBot >=4.24.2 <5"></a>
  <img src="https://img.shields.io/badge/platform-OneBot%2011%20%7C%20QQ-blue" alt="OneBot 11 / QQ">
  <a href="./LICENSE.txt"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue" alt="AGPL-3.0"></a>
</p>

<p align="center">
  <a href="#quick-start">快速开始</a> ·
  <a href="#usage">使用方法</a> ·
  <a href="#configuration">配置</a> ·
  <a href="#data-security">数据安全</a> ·
  <a href="#changelog">更新记录</a>
</p>

群典是一个面向 QQ 群聊的 AstrBot 插件。可收录群友的文字、图片、QQ
表情、商城表情、回复关系和合并转发，并通过随机群典或个人“爆典”合集重新发送。

## 核心亮点

| 能力     | 说明                                                       |
| -------- | ---------------------------------------------------------- |
| 消息收录 | 保存文字、图片、QQ 表情、商城表情、回复快照与合并转发      |
| 随机群典 | 随机发送全群或指定成员的记录，可随机决定单次发送条数       |
| 爆典合集 | 按记录时间查看指定成员参与的全部群典，支持分页和嵌套转发   |
| 卡片渲染 | 单条纯文字或图片记录可使用 CSS 卡片，尺寸和样式均可配置    |
| 后台管理 | 通过官方 Plugin Pages 查看、筛选、删除、备份和迁移群典数据 |

<a id="quick-start"></a>

## 快速开始

### 从插件市场安装

1. 打开 AstrBot Dashboard 的插件市场。
2. 搜索“群典”并安装插件。
3. 重启或重载插件，然后在 QQ 群中引用一条消息发送 `/添加群典`。

运行要求：

- AstrBot `>=4.24.2,<5`
- OneBot 11 / QQ，使用 `aiocqhttp` 适配器
- Python 依赖：`aiohttp>=3.9,<4`、`Pillow>=11.2.1,<13`

<details>
<summary>手动安装</summary>

进入 AstrBot 的插件目录并克隆正式仓库：

```bash
cd AstrBot/data/plugins
git clone https://github.com/conFess233/astrbot_plugin_iconic_quotes.git
cd astrbot_plugin_iconic_quotes
```

使用 AstrBot 所在的 Python 环境安装依赖：

```bash
python -m pip install -r requirements.txt
```

完成后重启 AstrBot。插件目录名应保持为 `astrbot_plugin_iconic_quotes`。

</details>

### 快速上手

1. 引用想要收录的消息，发送：

    ```text
    /添加群典
    ```

2. 随机查看当前群的群典：

    ```text
    /群典
    ```

3. 随机查看指定成员，或获取该成员的完整合集：

    ```text
    /群典 @某人
    /爆典 @某人
    ```

当目标的健康记录数不超过“爆典每页条数”时，`/爆典 @某人` 会直接发送全部记录；超过上限时只提示总页数，再使用 `/爆典 @某人 页码` 获取指定页。

<a id="usage"></a>

## 使用方法

### 指令

| 指令                 | 作用                                     |
| -------------------- | ---------------------------------------- |
| `/添加群典`          | 收录当前引用的普通消息或合并转发         |
| `/群典`              | 随机发送当前群的群典                     |
| `/群典 @某人`        | 随机发送指定成员的群典                   |
| `/群典 info`         | 查看当前群的汇总统计，不展示具体内容     |
| `/爆典 @某人 [页码]` | 按记录时间查看该成员参与的全部群典       |
| `/删除群典 <文字>`   | 发送完整待删除记录，并创建 60 秒确认窗口 |
| `/确认删除`          | 删除当前用户最近一次预览并确认的记录     |

### 默认关键词

| 关键词              | 作用                              |
| ------------------- | --------------------------------- |
| `添加群典`          | 与 `/添加群典` 相同，需要引用消息 |
| `群典`              | 与 `/群典` 相同                   |
| `群典 @某人`        | 随机发送指定成员的群典            |
| `爆典 @某人 [页码]` | 获取指定成员的完整群典合集        |

关键词会在去除首尾空白后按完整语法匹配。关键词开关和列表可以修改，关闭关键词不会禁用对应的斜杠指令；当关键词发生冲突时，爆典语法优先。

### 收录内容

- 普通消息保存发送者 QQ ID、昵称快照、正文、来源时间和收录信息。
- 合并转发整体计为一条记录，保留节点顺序、节点作者和节点内容。
- 被收录消息自身带有回复关系时，会将被回复消息保存为本地快照；添加命令用于选择来源的引用不会被误存为回复。
- QQ 内置表情保存表情 ID 并原生重放；商城表情保存本地图片和必要元数据，原生发送失败时降级为图片。
- Unicode Emoji 作为普通文字保存；JPEG、PNG、WebP 和 GIF 按内容哈希存储。
- 头像不会持久化。卡片发送时通过 API 实时获取，失败或不存在时使用空白头像区域。
- 语音、视频和文件等不支持的消息段不会保存；没有任何可保存内容时会明确提示添加失败。

### 查询与发送

`/群典` 对普通记录和合并转发记录等概率抽取，单次抽取不会重复。`/群典 @某人` 使用 QQ ID 精确匹配普通记录；合并转发只有在能够安全归属于该成员时才会进入其随机池。

`/爆典 @某人` 则会收集该成员作为普通消息作者或任一转发节点作者参与的记录，按收录时间从旧到新排列。多作者合并转发整体计为一条，不拆分、不重复计数。

发送方式包括：

- 单条文字：`“金句内容”——发送者`
- CSS 图片卡片：头像、金句正文、群昵称和记录时间
- 多条聚合：以合并转发发送，每个节点使用金句发送者的 QQ ID 和昵称
- 原始合并转发：优先嵌入爆典合集；不兼容时降级为扁平合集，并在必要时将商城表情降级为本地图片

含 QQ 表情、商城表情或回复快照的记录会绕过 CSS 卡片，使用原生消息链发送，避免内容丢失。

<a id="configuration"></a>

## 配置

所有配置都可以在 AstrBot 插件配置或“群典管理”Plugin Page 中修改。以下是最常用的配置。

### 触发与发送

| 配置键                  | 默认值     | 作用                                      |
| ----------------------- | ---------- | ----------------------------------------- |
| `add_keyword_enabled`   | `true`     | 启用添加关键词                            |
| `add_keywords`          | `添加群典` | 添加关键词列表                            |
| `query_keyword_enabled` | `true`     | 启用随机查询关键词                        |
| `query_keywords`        | `群典`     | 随机查询关键词列表                        |
| `send_count`            | `1`        | 每次最多发送的记录数，范围 1～10          |
| `random_send_count`     | `false`    | 在 1 到 `send_count` 之间随机决定发送数量 |
| `send_mode`             | `text`     | 单条发送方式：`text` 或 `card`            |
| `aggregate_multiple`    | `true`     | 将多条普通记录聚合为合并转发              |

### 爆典

| 配置键                  | 默认值   | 作用                        |
| ----------------------- | -------- | --------------------------- |
| `burst_keyword_enabled` | `true`   | 启用爆典关键词              |
| `burst_keywords`        | `爆典`   | 爆典关键词列表              |
| `burst_page_size`       | `50`     | 爆典每页记录数，范围 1～100 |
| `burst_roles`           | `member` | 可使用爆典的角色            |

### 权限与名单

每项操作可选择 `bot_admin`、`owner`、`admin`、`member` 或 `everyone`。

| 配置键                                | 默认值      | 作用                         |
| ------------------------------------- | ----------- | ---------------------------- |
| `add_roles`                           | `everyone`  | 添加权限                     |
| `query_roles`                         | `everyone`  | 随机查询权限                 |
| `burst_roles`                         | `member`    | 爆典权限                     |
| `info_roles`                          | `everyone`  | 查看统计权限                 |
| `delete_roles`                        | `bot_admin` | 删除权限                     |
| `group_blacklist` / `group_whitelist` | 空          | 限制可使用插件的群           |
| `user_blacklist` / `user_whitelist`   | 空          | 限制可调用插件的用户         |
| `excluded_author_ids`                 | 空          | 禁止指定 QQ 用户的消息被收录 |

黑名单优先于白名单。禁止收录名单同样作用于回复快照和合并转发节点；无法可靠确认身份时会拒绝收录。

### 容量与内容限制

| 配置键                   | 默认值  | 作用                           |
| ------------------------ | ------- | ------------------------------ |
| `max_records_per_group`  | `5000`  | 每个群的最大记录数             |
| `max_media_mb`           | `2048`  | 插件全部本地媒体的容量上限     |
| `max_image_mb`           | `10`    | 单张图片或贴纸的大小上限       |
| `max_images_per_record`  | `9`     | 单条记录的图片与贴纸数量上限   |
| `max_forward_nodes`      | `100`   | 单个合并转发的节点上限         |
| `max_text_chars`         | `5000`  | 单条消息或单个节点的字符上限   |
| `max_forward_text_chars` | `50000` | 合并转发及回复链的累计字符上限 |
| `max_reply_depth`        | `3`     | 回复快照深度，范围 1～10       |

达到记录、媒体或单条内容上限时，整次添加会失败并清理无引用资源，不会截断内容或静默淘汰旧记录。

### 冷却、重试与卡片

| 配置键                                | 默认值              | 作用                               |
| ------------------------------------- | ------------------- | ---------------------------------- |
| `global_cooldown_ms`                  | `1000`              | 插件操作后的冷却时间               |
| `cooldown_message`                    | `群典功能冷却中...` | 冷却期间唯一执行的回复内容         |
| `send_retry_count`                    | `2`                 | 发送失败后的额外重试次数           |
| `send_retry_delay_ms`                 | `500`               | 每次重试之间的固定延迟             |
| `retry_on_ambiguous_failure`          | `false`             | 是否重试超时等结果不明确的失败     |
| `card_width`                          | `1200`              | CSS 卡片画布宽度                   |
| `card_min_height` / `card_max_height` | `480` / `2000`      | 卡片高度边界                       |
| `card_auto_height`                    | `true`              | 按内容收缩并裁切右侧、底部画布留白 |
| `card_custom_css`                     | 空                  | 自定义卡片 CSS，不允许外部资源     |

<details>
<summary>高级配置与群级覆盖</summary>

- `storage_subdir`：插件专属数据根目录下的相对目录，默认 `iconic_quotes`。
- `allow_bot_authors`：是否允许人工引用或合并转发收录机器人作者，默认关闭。
- `delete_preview_limit`：命令删除一次最多预览的记录数，默认 20。
- `audit_limit`：删除审计最多保留的条数，默认 10000。
- `group_overrides`：按群号覆盖关键词、发送、爆典、权限和部分容量配置，建议通过管理页编辑。

存储路径迁移必须通过管理页执行。卡片 CSS 会经过安全过滤，不允许 `@import`、`url()` 或其他外部资源。

</details>

## WebUI

插件详情页中的“群典管理”复用 AstrBot Dashboard 的端口和认证，无需单独部署 Web 服务。Bot 管理员可以：

- 按群分页、搜索和筛选记录
- 查看文字、图片、表情、回复快照、转发节点和记录元数据
- 单条或批量删除，并查看删除审计
- 编辑全局配置和群级覆盖，预览 CSS 卡片
- 导出 ZIP 备份
- 预检并确认导入备份，查看新增、重复、冲突和缺失资源统计
- 显式迁移存储目录，并保留迁移前备份

<a id="data-security"></a>

## 数据与安全

所有插件数据均位于 AstrBot 的插件专属数据目录：

```text
AstrBot/data/plugin_data/astrbot_plugin_iconic_quotes/iconic_quotes/
├── groups/<群号>.json
├── images/<群号>/<内容哈希>.<扩展名>
├── backups/
└── audit.json
```

- 数据按群隔离，JSON 当前使用 Schema v2。
- v1 数据和官方备份仍可读取，并在下一次修改时创建 `.bak` 后惰性升级。
- JSON 使用临时文件原子替换；主文件损坏时可尝试读取 `.bak`。
- 图片和商城表情按二进制 SHA-256 去重，单独存放在 `images/`。
- 头像通过 API 临时获取，不保存到 JSON 或磁盘。
- 导入备份会校验路径、清单、文件哈希、图片格式和容量，再由管理员确认写入。
- 数据为明文，请保护 AstrBot 数据目录和导出的 ZIP 文件。

从旧目录升级时，如果新目录为空，插件会把旧数据迁移到专属目录并保留带时间戳的备份；如果新旧目录同时有数据，则继续使用新目录，不自动覆盖任一侧。

## 已知限制

- 仅支持 OneBot 11 / QQ 群聊。
- 仅支持收录一层合并转发；发送爆典时会优先尝试嵌套，并提供兼容降级。
- 纯图片记录不能通过字符串删除命令命中，请在管理页删除。
- OneBot 无法返回作者 QQ ID 时，记录会标记为身份不完整，只能进入群级随机池。
- 图片或 JSON 被外部修改后，异常记录不会参与随机发送或爆典健康记录统计。

<a id="changelog"></a>

## 更新记录

[CHANGELOG.md](./CHANGELOG.md)。

## License

本项目以 [AGPL-3.0](./LICENSE.txt) 发布。
