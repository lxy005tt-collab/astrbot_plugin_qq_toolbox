# 框架接口与观察台兼容说明

本包适配 AstrBot `>=4.29.1,<4.30.0`。消息输入屏蔽使用官方 `event_message_type(ALL)` 事件和 `stop_event()`，不替换 `PipelineScheduler.execute`。该事件在框架唤醒检查之后、插件消息处理阶段执行，不能阻止框架此前的事件解析。

请求中的群聊资料、跟进资料、图片任务状态、语音回执及工具边界均通过 `extra_user_content_parts` 中的 `TextPart.mark_as_temp()` 提供。它们参与本轮生成，框架保存会话时会排除这些临时块；本插件不为此重写当前用户原文或系统提示词。临时块本身仍消耗输入 token，也不能保证服务端缓存必定命中。

观察台仍包含以下非公开接口包装，需 AstrBot 维护者评审；本文不代表已获批准：

| 接口 | 用途 |
| --- | --- |
| `ToolLoopAgentRunner.reset` | 建立一次主回复的关联标识 |
| `ToolLoopAgentRunner._iter_llm_responses` | 记录压缩后的实际请求、流式响应、重试和回退的各次用量 |
| `ContextManager._run_compression` | 记录实际压缩前后大小 |

官方 `on_llm_request` 发生在最终请求构建和压缩之前；`on_llm_response` 不能替代逐次请求的观测。当前可用的官方事件没有提供上述全部数据，故暂保留最小兼容包装，避免把请求前估算误标为实际发送数据。只包装主聊天链路，不额外调用模型，不更改请求、回复或压缩参数。关闭观察台模块后不安装这些包装，卸载时仅在原位仍为本插件包装时还原，避免覆盖其他插件。

实现位置：`modules/observatory/compat.py`。包含接口存在性检查、异常透传与采集异常隔离；框架变更后仍需重新验证兼容性。其他模块的既有兼容桥接不因本轮整改而被声明为官方接口。

官方接口依据：[消息事件与临时上下文](https://docs.astrbot.app/dev/star/guides/listen-message-event.html)。
