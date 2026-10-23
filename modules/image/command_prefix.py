"""Recognize an explicit drawing command after a literal @mimi prefix."""
from __future__ import annotations

import re

from astrbot.api import logger
from astrbot.api.message_components import At
from astrbot.core.star.filter.custom_filter import CustomFilter


class DrawingMentionPrefix(CustomFilter):
    """Normalize before AstrBot's native command filter, preserving its gates."""
    _prefix = re.compile(r"^[@＠]mimi(?:\s+|(?=/))(?P<command>/\S[\s\S]*)$", re.I)
    _commands = {"画图", "draw", "image", "绘图", "图生图", "img2img", "image2image",
                 "修图", "改图", "edit", "inpaint", "奶蛙", "大肥鱼", "绘图额度", "画图额度", "生图额度"}

    def filter(self, event, cfg):
        if event.get_platform_name() != "aiocqhttp" or not event.get_group_id():
            return True
        match = self._prefix.fullmatch(event.get_message_str().strip())
        if not match:
            return True
        command = match["command"]
        name = command.split(maxsplit=1)[0][1:]
        if name not in self._commands:
            return True
        # Do not turn a message addressed to another account into our command.
        if any(isinstance(c, At) and str(c.qq) != str(event.get_self_id()) for c in event.get_messages()):
            return True
        event.message_str = command[1:].strip()
        event.is_at_or_wake_command = True
        event.is_wake = True
        event.set_extra("openai_image_text_mention", True)
        logger.info("OpenAIImage recognized literal @mimi drawing command: sender=%s command=%s", event.get_sender_id(), name)
        return True
