"""Apply the configured input word list before any QQ model processing."""
from sys import maxsize

from astrbot.api.message_components import Plain, Reply
from astrbot.api.star import Context
from ...runtime import SuiteModule as Star, deferred_filter as filter
from astrbot.core.pipeline.content_safety_check.strategies.keywords import KeywordsStrategy
from astrbot.core.utils.quoted_message.chain_parser import ReplyChainParser


class ModelInputFilter(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self._signature = None
        self._strategy = None

    def blocked(self, event):
        if not self.config.get("enabled", True) or event.get_platform_name() != "aiocqhttp":
            return False
        settings = self.context.get_config(umo=event.unified_msg_origin).get("content_safety", {}).get("internal_keywords", {})
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

    @filter.event_message_type(filter.EventMessageType.ALL, priority=maxsize + 64)
    async def filter_input(self, event):
        if self.blocked(event):
            event.set_extra("model_input_filtered", True)
            event.stop_event()
            self.logger.info("模型输入已拦截：platform=%s message_id=%s", event.get_platform_id(), event.message_obj.message_id)
