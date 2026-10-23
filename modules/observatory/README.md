# Bot 观察台（astrbot_plugin_observatory）v1.0.0

在 AstrBot 后台里只读观察**主聊天链路**：一轮回复实际发给主模型的内容、模型回复、上游实际返回的公开思考、token 用量、估算费用和上下文压缩。

> 观察台自己不调用任何模型，不发送/撤回 QQ 消息，不修改人格或主聊天上下文，不写入 AstrBot 原生数据库与原生配置。

## 1. 范围

**计入 v1：**

- 主聊天 `ToolLoopAgentRunner` 发起的直接主模型 API 请求（含流式/非流式、工具循环、重试、fallback）。
- 回复轮数、直接请求数、输入非缓存/缓存/输出 token、接口实际返回的公开思考、延迟、估算费用。
- 平台消息记录（原生 `platform_message_history` 历史 + 安装后实时补采）。
- 上下文预算、压缩触发点、压缩事件（策略/前后计数/摘要引用）。

**不计入 v1：**

- 日程判断、语音优化、画图、embedding、记忆提取、图片转述等辅助模型调用。
- 以模型名、provider ID 或“最近活跃会话”猜出来的请求（不采用这种归属方式）。
- 供应商真实账单、汇率换算、订阅额度的单条摊销。
- 完整永久聊天档案：原生群消息有数量上限，历史私聊用户消息原生未保存时不可恢复。

## 2. 安装与打开

### 方式 A：后台上传安装包（推荐）

1. 用本目录的 `dist/astrbot_plugin_observatory-1.0.0.zip`（或按下文 allowlist 自行打包）。
2. AstrBot 后台 → 插件 → 安装插件 → 上传 zip。
3. 安装成功后在插件详情页会出现 **Bot 观察台** 页面入口。

### 方式 B：手动放入插件目录

把插件目录放到 `D:\Tools\AstrBot\data\plugins\astrbot_plugin_observatory`，在后台重载该插件。运行数据会自动创建在
`data/plugin_data/astrbot_plugin_observatory/`（以 `StarTools.get_data_dir()` 实际返回为准）。

### 打开与认证

- 插件页面沿用 AstrBot 后台登录；页面在受限 iframe 内通过 `window.AstrBotPluginView` bridge 调用
  `/{PLUGIN_NAME}/...` 后端接口。
- 后端每个接口都检查 `request.username`；未登录访问返回 401（本机实测）。
- 不需要、也不提供额外端口或第二套账号密码。

## 3. 页面功能

| 入口 | 内容 |
| --- | --- |
| 总览 | 今日/24 小时/7 天卡片、请求与 token 趋势、用量最高会话、原生汇总与覆盖说明、采集健康 |
| 群聊 | 群搜索/排序、聊天记录、引用与附件标记、关联回复、当前上下文进度、压缩事件 |
| 私聊 | 联系人搜索、聊天记录、连发合并条数、发送/生成状态区分 |
| 主模型调用 | 直接请求列表、状态/会话/思考/费用/模型筛选、请求详情标签页 |
| 费用与设置 | 费用汇总、每日用量、价格版本编辑、保留期/预算/刷新设置、健康与导出 |

请求详情五个标签：

1. **实际输入**：压缩后、provider 序列化之前采集的消息快照；按人格/系统规则、历史、当前消息、临时注入、工具结果、
   工具 schema、压缩摘要分组；token 为本地估算并明确标注；支持 `_no_save` 临时内容与结构化 JSON 查看。
2. **模型回复**：最终回复原文；明确“模型生成 ≠ 平台发送成功”。
3. **公开思考**：只读可折叠；状态 `available / not_provided / partial / expired`；不展示加密 signature。
4. **工具与调用过程**：每一步的工具调用与下一次请求里的真实工具结果。
5. **token 与费用**：接口 usage 分项、usage 来源、管理预算、压缩触发点、估算金额与价格版本依据。

交互细节：默认 10 秒自动刷新（可调，0 关闭），页面隐藏时暂停；聊天/请求列表保留阅读位置；深/浅色跟随 AstrBot 主题。

## 4. 统计口径（重要）

- **平台消息数**：`chat_message` 行数（原生记录 + 实时补采），不等于模型请求数。未触发机器人的群消息也算消息。
- **主回复轮数**：`reply_trace` 行数，一轮机器人回复一行；工具循环可能在同一轮里产生多次直接请求。
- **直接主模型请求数**：`llm_request` 行数；fallback/重试各算一次真实请求。
- **原生汇总**：`native_reply_summary`，来自 `provider_stats`，单独显示，**不与**本插件直接请求相加。
- **本轮累计用量**：同一 trace 的请求 usage 之和；`usage_complete` 记录有几次请求返回了 usage。
- **最新上下文占用**：最近一次直接请求的 `usage.input`（非缓存 + 缓存）；没有时退回原生汇总输入并标注。
- **管理预算**：当前会话/provider 的 `max_context_tokens`。本机主 provider 为 `457317`。
- **压缩触发点**：核心压缩阈值 `0.82`，`int(budget * 0.82) + 1`，本机为约 `375000`；按会话覆盖/实际 provider 计算，不写死。
- **费用**：只按已配置单价和接口 usage 估算；未知单价/未知 usage/订阅额度都不是 0。

## 5. 费用与价格

`cost = input_other / 1e6 * input_price + input_cached / 1e6 * cached_input_price + output / 1e6 * output_price`

- 单价单位固定为“每百万 token”，金额用 `Decimal` 计算，按币种分组，不默认相加不同币种。
- 价格表支持 `per_token / subscription_or_quota / unknown`。
- 支持 `min_input_tokens` 长上下文分档：按本次输入总量选择满足条件的最高档；价格版本在估算时冻结，
  之后编辑单价不会改变旧估算（有测试覆盖）。
- 输入缓存是输入的子集，不会二次相加；思考若已包含在 output，不按思考字符数重复计费。
- 价格表默认为空；没填单价时显示“单价未配置”，不会显示 0 元。

## 6. 数据与保留

- 插件数据：`observatory.db`（SQLite/WAL）+ `blobs/*.json.gz`（请求快照、回复、思考、压缩摘要正文）。
- 默认保留：聊天 30 天、公开思考 7 天、完整请求快照/回复 7 天、用量与费用明细 180 天。
- 正文预算默认 2 GB；超出时优先删除最旧正文，保留统计并标记“正文已过期”。
- 清理只发生在插件数据根目录内，不删除 AstrBot 原生数据。
- 采集队列有界；写入失败/队列满/正文裁剪会在 `health` 与页面健康面板暴露，不静默伪装完整。
- 聊天/工具结果/思考全部按不可信文本渲染，前端只用 `textContent`，不注入 HTML；导出 CSV 对公式注入做转义。

## 7. 接口一览

注册前缀 `/{PLUGIN_NAME}/...`；bridge 调用时省略插件前缀。全部 JSON 返回，错误使用标准 envelope。

| endpoint | 方法 | 用途 |
| --- | --- | --- |
| `overview` | GET | 总览卡片、趋势、top 会话、覆盖 |
| `sessions` | GET | 群/私聊列表、搜索、类型/排序、游标式 offset |
| `sessions/<id>` | GET | 会话元数据 |
| `sessions/<id>/messages` | GET | 平台消息（游标 `before_ts`/`before_id`）+ 未确认发送的模型回复 |
| `sessions/<id>/context` | GET | 最新占用、预算/触发点、压缩事件 |
| `traces` / `traces/<id>` | GET | 回复轮次列表/树 |
| `requests` / `requests/<id>` | GET | 直接请求列表/详情 |
| `requests/<id>/content` | GET | `kind=input/reply/reasoning/json/tools`，游标分页 |
| `usage` / `costs` | GET | 用量/费用汇总与下钻 |
| `prices` / `prices/save` | GET/POST | 价格版本；保存新版本并冻结旧估算 |
| `settings` / `settings/save` | GET/POST | 仅观察台自身设置 |
| `health` | GET | 采集包装、队列、存储、覆盖、缺口 |
| `export` | GET | `kind=requests/traces/messages/costs`，`format=json/csv`，服务端限量 |

服务端对筛选范围、limit、币种/价格、路径和 ID 做校验；不返回 API Key、Cookie、provider 完整配置或 base64 正文。

## 8. 已知限制

1. 采集点是 runner 的 provider 调用边界，**不是** wire 报文；provider 适配器内部再次改写、SDK 内部重试不可见，
   页面统一标 `capture_stage=runner_provider_payload`。
2. 原生 `provider_stats` 是每轮汇总，可能累计多次模型调用；只作为“原生汇总”导入，不拆分步骤、不补公开思考。
3. 原生群消息每个会话有 100 条上限（本机配置）；私聊原生只保存过 bot 行。安装前的用户私聊历史无法恢复。
   原生汇总没有逐请求公开思考，状态为 `historical_not_recorded`；不会补写或伪造。
4. `scope_id` 由 `platform_id + message_type + peer_id` 生成；`bot_self_id` 存为元数据但不参与键，
   因为原生历史行没有 bot 账号。同一平台实例同时跑多个 bot 的极端场景可能合并到同一会话（v1 未处理）。
5. 主动消息团队若直接调用 `context.send_message()` 而不经过事件发送钩子，聊天记录里可能只有模型生成侧，
   没有平台发送侧；页面会分开标注，不会假定“生成=发送成功”。
6. 附件只展示原生占位符/可用元数据，不批量下载历史附件，不开放任意本地文件读取；失效链接显示不可用。
7. 公开思考只在流式实际持续返回时逐步出现；后端拿不到就不承诺实时逐字。
8. 价格不会联网猜测；真实供应商账单不在 v1 接入范围。

## 9. 用户验收清单

1. 后台插件详情能看到 **Bot 观察台** 页面入口，打开后是五个导航入口。
2. 进入一个群和一个私聊，能看到消息、人物、时间和来源标记；未触发机器人的群消息也在。
3. 发一条会唤醒机器人的消息后，在“主模型调用”看到新的直接请求；从聊天里的“请求详情”能打开同一行。
4. 详情里检查：实际输入分组、公开思考状态、token 分项、费用状态；无公开思考时显示“未提供”而不是空白。
5. 普通回复与带工具的回复对比：确认“回复轮数”和“直接请求数”是分开的。
6. 打开某个群/私聊的上下文条：管理预算约 457,317、压缩触发点约 375,000（以实际配置为准）。
7. 费用页不填单价时显示“单价未配置”；填入演示价格后新请求显示估算，旧记录金额不变。

## 10. 回退/卸载

- 后台禁用或卸载插件即可停止采集；页面与接口随插件移除。
- 插件不会修改 AstrBot 原生配置/数据库；卸载后如需清理观察数据，只删除 `data/plugin_data/astrbot_plugin_observatory/`。
- 若需彻底恢复本机方法包装：正常卸载会走 `terminate()` 恢复；异常残留时重启 AstrBot 即可（包装只在进程内）。

## 11. 源码结构

```text
astrbot_plugin_observatory/
  main.py            # Star 生命周期、事件补采、预算解析、价格入口
  compat.py          # 可恢复的 runner/ContextManager 包装
  collector.py       # 主聊天捕获、队列、快照、思考/usage
  storage.py         # SQLite + gzip 正文 + 保留期/预算清理
  native_reader.py   # 原生历史只读导入
  queries.py         # 只读聚合
  pricing.py         # Decimal、版本化价格、长上下文分档
  page_api.py        # 原生 Web API 端点
  integration-evidence.md
  tests/             # 无网络替身验证，不进入安装包
  views/dashboard/   # 原生 View（index.html/app.js/style.css/modules）
  metadata.yaml
  _conf_schema.json
  .astrbot-plugin/i18n/zh-CN.json
```

安装包 allowlist：`metadata.yaml`、`_conf_schema.json`、`__init__.py`、`main.py`、`compat.py`、`collector.py`、
`storage.py`、`native_reader.py`、`pricing.py`、`page_api.py`、`queries.py`、`util.py`、`README.md`、
`.astrbot-plugin/i18n/zh-CN.json`、`views/**`。不包含 `tests/`、`__pycache__/`、原生数据库、聊天数据、密钥、模型快照
或验证 artifacts。
