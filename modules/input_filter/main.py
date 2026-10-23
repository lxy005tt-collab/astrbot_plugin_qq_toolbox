"""Apply the configured input word list before any QQ model processing."""
from functools import wraps

from astrbot.api.message_components import Plain, Reply
from astrbot.api.star import Context
from ...runtime import SuiteModule as Star
from astrbot.core.pipeline.scheduler import PipelineScheduler
from astrbot.core.pipeline.content_safety_check.strategies.keywords import KeywordsStrategy
from astrbot.core.utils.quoted_message.chain_parser import ReplyChainParser


class ModelInputFilter(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self._original = None
        self._wrapped = None
        self._signature = None
        self._strategy = None

    def blocked(self, scheduler, event):
        if not self.config.get("enabled", True) or event.get_platform_name() != "aiocqhttp":
            return False
        settings = scheduler.ctx.astrbot_config.get("content_safety", {}).get("internal_keywords", {})
        if not settings.get("enable", False):
            return False
        signature = tuple(settings.get("extra_keywords") or [])
        if signature != self._signature:
            self._strategy = KeywordsStrategy(list(signature))
            self._signature = signature
        if self._strategy is None:
            return False
        text = [event.get_message_str()]
        components = event.get_messages()
        text.append("".join(c.text for c in components if isinstance(c, Plain)))
        parser = ReplyChainParser()
        for component in components:
            if isinstance(component, Reply):
                quoted = parser.extract_text_from_reply_component(component)
                if quoted:
                    text.append(quoted)
        allowed, _ = self._strategy.check("\n".join(text))
        return not allowed

    async def initialize(self):
        self._original = PipelineScheduler.execute
        original = self._original

        @wraps(original)
        async def execute_filtered(scheduler, event):
            if self.blocked(scheduler, event):
                event.set_extra("model_input_filtered", True)
                event.stop_event()
                event.cleanup_temporary_local_files()
                self.logger.info("模型输入已拦截：platform=%s message_id=%s", event.get_platform_id(), event.message_obj.message_id)
                return
            return await original(scheduler, event)

        self._wrapped = execute_filtered
        PipelineScheduler.execute = execute_filtered
        self.logger.info("模型输入前置屏蔽已启用：复用内容安全输入词表，命中后整条忽略，不进入模型、绘图或学习流程。")

    async def terminate(self):
        if self._original is not None and PipelineScheduler.execute is self._wrapped:
            PipelineScheduler.execute = self._original
        self._original = self._wrapped = None
