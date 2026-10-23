"""Replace outgoing QQ words locally, independently of the chat model."""

from functools import wraps
from sys import maxsize
from ...runtime import deferred_filter as filter
from astrbot.api.star import Context
from ...runtime import SuiteModule as Star
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .word_filter import WordFilter


BLOCKED_EXTRA = "output_word_filter_reply_blocked"


class OutputWordFilter(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.replacer = WordFilter(config)
        self._original = None
        self._wrapped = None
        self._original_responses = None
        self._wrapped_responses = None

    def _block_event(self, event):
        already_blocked = event.get_extra(BLOCKED_EXTRA, False)
        event.set_extra(BLOCKED_EXTRA, True)
        event.set_extra("agent_stop_requested", True)
        event.set_extra("enable_streaming", False)
        event.set_extra("enable_reasoning", False)
        result = event.get_result()
        if result:
            result.chain = []
        event.stop_event()
        if not already_blocked:
            self.logger.warning("已屏蔽正文命中禁用符号的整条 QQ 回复。")

    def _blocks_response(self, response):
        return bool(response is not None and (
            self.replacer.blocks_text(response.completion_text or "")
            or (response.result_chain and self.replacer.blocks_components(response.result_chain.chain))
        ))

    async def initialize(self):
        self._original = AiocqhttpMessageEvent.__dict__["send_message"]
        original = self._original.__func__

        @wraps(original)
        async def send_filtered(cls, bot, message_chain, event=None, is_group=False, session_id=None):
            # The transport normally receives a raw OneBot Event, whose missing
            # attributes resolve to None rather than AstrMessageEvent methods.
            get_extra = getattr(event, "get_extra", None)
            if callable(get_extra) and get_extra(BLOCKED_EXTRA, False):
                return
            # Inspect the whole chain before QQ splits forwards/files for delivery.
            if self.replacer.blocks_components(message_chain.chain):
                self.logger.warning("已屏蔽命中禁用符号的整条 QQ 输出。")
                return
            filtered = self.replacer.chain(message_chain)
            if self.replacer.blocks_components(filtered.chain):
                self.logger.warning("已屏蔽替换后命中禁用符号的整条 QQ 输出。")
                return
            return await original(
                cls, bot, filtered,
                event=event, is_group=is_group, session_id=session_id,
            )

        self._wrapped = send_filtered
        # Both event.send and Context.send_message reach this QQ transport boundary.
        AiocqhttpMessageEvent.send_message = classmethod(send_filtered)
        # Inspect each model step before tools can execute. Final response hooks
        # alone run too late for a model that immediately sends through a tool.
        self._original_responses = ToolLoopAgentRunner._iter_llm_responses_with_fallback
        original_responses = self._original_responses

        @wraps(original_responses)
        async def guarded_responses(runner):
            event = getattr(getattr(getattr(runner, "run_context", None), "context", None), "event", None)
            guarded = (event is not None and event.get_platform_name() == "aiocqhttp"
                       and self.replacer.has_block_rules)
            if guarded:
                runner.streaming = False
            responses = original_responses(runner)
            try:
                async for response in responses:
                    if guarded and self._blocks_response(response):
                        self._block_event(event)
                        runner.request_stop()
                    # Preserve the original response and usage for accounting; the
                    # runner's stop check prevents text delivery and tool execution.
                    yield response
            finally:
                await responses.aclose()

        self._wrapped_responses = guarded_responses
        ToolLoopAgentRunner._iter_llm_responses_with_fallback = guarded_responses
        self.logger.info(
            "输出禁词替换已加载：词语替换 %d 条，旧版普通词 %d 条，正则 %d 条；本地处理，不调用模型。",
            len(self.config.get("word_replacements", [])),
            len(self.config.get("words", [])), len(self.config.get("patterns", [])),
        )
        self.logger.info("含 * 整条屏蔽：%s（群聊、私聊及工具发送）。", "开启" if self.replacer.block_asterisk else "关闭")
        self.logger.info("正文符号拦截：%s。", " ".join(self.replacer.block_symbols) or "关闭")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=maxsize + 8)
    async def buffer_qq_reply(self, event):
        if event.get_platform_name() == "aiocqhttp" and self.replacer.has_block_rules:
            # A later forbidden symbol must cancel the whole reply, including its prefix.
            event.set_extra("enable_streaming", False)

    @filter.on_llm_request(priority=maxsize + 16)
    async def buffer_model_reply(self, event, request):
        await self.buffer_qq_reply(event)

    @filter.on_llm_response(priority=maxsize + 16)
    async def block_model_reply(self, event, response):
        if event.get_platform_name() == "aiocqhttp" and self._blocks_response(response):
            self._block_event(event)

    @filter.on_decorating_result(priority=maxsize)
    async def block_before_segmentation(self, event):
        if event.get_platform_name() != "aiocqhttp":
            return
        if event.get_extra(BLOCKED_EXTRA, False):
            self._block_event(event)
            return
        result = event.get_result()
        if result and self.replacer.blocks_components(result.chain):
            self._block_event(event)

    @filter.on_using_llm_tool(priority=maxsize)
    async def block_message_tool(self, event, tool, tool_args):
        if (
            event.get_platform_name() != "aiocqhttp"
            or getattr(tool, "name", "") != "send_message_to_user"
            or not isinstance(tool_args, dict)
        ):
            return
        messages = tool_args.get("messages") or []
        if event.get_extra(BLOCKED_EXTRA, False) or any(
            isinstance(message, dict)
            and str(message.get("type", "")).lower() == "plain"
            and self.replacer.blocks_text(str(message.get("text", "")))
            for message in messages
        ):
            # Native validation rejects the empty batch before any segment is sent.
            tool_args["messages"] = []
            self.logger.warning("已屏蔽命中禁用符号的整批 QQ 直接发送工具消息。")

    async def terminate(self):
        current = AiocqhttpMessageEvent.__dict__.get("send_message")
        if self._original is not None and getattr(current, "__func__", None) is self._wrapped:
            AiocqhttpMessageEvent.send_message = self._original
        if self._original_responses is not None and ToolLoopAgentRunner._iter_llm_responses_with_fallback is self._wrapped_responses:
            ToolLoopAgentRunner._iter_llm_responses_with_fallback = self._original_responses
        self._original = None
        self._wrapped = None
        self._original_responses = None
        self._wrapped_responses = None
