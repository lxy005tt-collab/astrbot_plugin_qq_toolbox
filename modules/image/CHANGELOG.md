# 修复记录

## 2026-10-22 本地修复（版本号仍为 v1.4.5，未发版）

环境：AstrBot 4.29.1，本机 `data/config/astrbot_plugin_openai_image_config.json`。
只改 `main.py`，未新增配置项、未改命令名、未改工具名。

### 1. 待确认任务不再吞掉其它指令和正常聊天

`handle_pending_edit_confirmation`（原 priority=1）在 `/图生图`、`/修图` 预览后的 10 分钟窗口内，会把同会话里该用户的**所有**文本消息都当成顺序调整：解析不出就回“没看懂”并 `stop_event()`，导致其它插件指令（`/表情管理`、`/画mimi`、`/help` 等）与正常聊天被吞。

- 入口遇到 `handlers_parsed_params` 直接返回，已注册指令交回各自的 handler。
- 未识别的 `/` 开头文本放行。
- 顺序调整解析失败时不再发提示、不再 `stop_event()`，消息回到正常流程；pending 保留，确认/取消/调整顺序照旧可用。
- 单图 pending 下不再把任意聊天当成重排（`_resolve_adjusted_image_paths` 在 `image_count <= 1` 时返回 None）。

### 2. 确认词收紧

`CONFIRM_KEYWORDS` 原来含 `好/行/可以/ok/yes/开始`，配合 10 分钟窗口容易误触、直接扣当日额度。

- 现在只保留：`确认`、`确定`、`确认生成`、`确认开始`、`开始处理`、`开始生成`。
- 匹配前剥掉首尾标点，`确认。`、`确认！` 仍算确认。

### 3. 不再对每条消息下载媒体

`handle_pending_edit_confirmation` 原来无条件 `_extract_input_images()`：`Image` 走 `convert_to_file_path()`（URL 图会下载），`File` 先 `get_file()` 整文件下载再判断扩展名，且下载后不清理。

- 改用不落盘的 `_has_media()` 判断，只有“无文字 + 带媒体”需要暂存时才真正取图。
- `File` 组件先看文件名/URL 扩展名（`_is_supported_file_component`），是图片才下载；压缩包等直接跳过。

### 4. 顺序调整解析

`_parse_order_adjustment_simple` 里“第一张”被当成“置顶”关键词，且排在“最后”之前：

- `把第一张放到最后` → 原 `[1,2,3]`（无变化，且跳过了 LLM 兜底），现 `[2,3,1]`。
- “第一张”从置顶关键词中移除；`最后/末尾/队尾/排尾` 判定提前。

### 5. 私聊“先发图再发指令”不再必然失败

私聊合并等待 5 秒期间，图片消息要在合并结束后才暂存，而 `/图生图` 指令不等待、立刻读暂存 → 典型操作序列必失败。

- `handle_pending_edit_confirmation` 优先级 `1 → maxsize + 5`，排在合并等待（`maxsize + 4`）之前：图片立即暂存，合并行为不变。

### 验证

- `python -m py_compile` 通过。
- 纯函数实测：顺序解析 9 组用例、确认/取消 13 组用例。
- 用真实插件类 + 假事件跑了 6 个场景：已注册指令不劫持、单图 pending 聊天放行、双图 pending 聊天放行、顺序调整正常重排并截断、纯图消息正常暂存、纯文本不触发取图。
- 热重载：2026-10-22 00:51 经面板接口重载成功，旧实例 handlers 已移除，新实例加载完成，`generate_image` / `edit_image` 重新注册，日志无异常。

### 已知未修（本次范围外）

- `绘图图片` 目录无保留期，生成图/输入归档/联网参考图会持续累积。
- `/图生图`、`/修图` 在 `web_research_enabled` 开启时也会先联网检索，`img2img` 会把检索图作为额外参考图追加到 edits 请求。
- `_send_error_message` 在 `except` 分支内可能二次抛 `ValueError`（平台未连接时只留下后台任务错误日志）。
- `group_image_only` 下 `suppress_group_drawing_ack` 会清空整条结果链。
- `_get_platform_context` 每次 LLM 响应都会构建并在 `detailed_logging` 开启时打印平台上下文。
