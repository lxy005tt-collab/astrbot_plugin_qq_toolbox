"""Merge a private-message burst before the normal reply pipeline continues."""

import asyncio
import math
from dataclasses import dataclass, field

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain


@dataclass
class PendingMessages:
    owner: AstrMessageEvent
    last_received: float
    texts: list[str] = field(default_factory=list)
    chain: list = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    count: int = 0
    source_ids: list[str] = field(default_factory=list)

    def append(self, event: AstrMessageEvent, now: float) -> None:
        self.source_ids.append(str(event.message_obj.message_id))
        self.owner.set_extra("private_message_wait_source_ids", self.source_ids)
        messages = list(event.get_messages())
        if not messages and event.message_str:
            messages = [Plain(event.message_str)]
        if self.chain and messages:
            self.chain.append(Plain("\n"))
        self.chain.extend(messages)
        self.texts.append(event.message_str)
        self.last_received = now
        self.count += 1
        if event is not self.owner:
            # Later events finish immediately; their attachments must remain
            # alive until the owning event finishes its normal pipeline.
            for path in list(event._temporary_local_files):
                self.owner.track_temporary_local_file(path)
                event.untrack_temporary_local_file(path)
        self.changed.set()


class PrivateMessageWait:
    def __init__(self, settings: dict, logger):
        self.logger = logger
        self.wait_seconds = float(settings.get("wait_seconds", 3.0))
        if not math.isfinite(self.wait_seconds) or self.wait_seconds < 0:
            raise ValueError("私聊等待秒数必须是大于等于 0 的有限数字。")
        self._pending: dict[str, PendingMessages] = {}
        self._closing = False

    async def wait_for_messages(self, event: AstrMessageEvent):
        if (
            self._closing
            or self.wait_seconds == 0
            or not event.is_private_chat()
            or event.is_stopped()
        ):
            return
        # WakingCheckStage has already identified registered commands.
        if (
            event.get_extra("handlers_parsed_params")
            or event.message_str.lstrip().startswith("/")
        ):
            return
        if not event.get_messages() and not event.message_str:
            return

        key = event.unified_msg_origin
        now = asyncio.get_running_loop().time()
        batch = self._pending.get(key)
        if batch is not None and batch.owner.is_stopped():
            batch.changed.set()
            batch = None
        if batch is not None:
            batch.append(event, now)
            event.stop_event()
            return

        batch = PendingMessages(event, now)
        batch.append(event, now)
        self._pending[key] = batch
        try:
            while not self._closing and not event.is_stopped():
                batch.changed.clear()
                remaining = self.wait_seconds - (
                    asyncio.get_running_loop().time() - batch.last_received
                )
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(batch.changed.wait(), timeout=remaining)
                except TimeoutError:
                    # A boundary arrival may have reset last_received.
                    continue
            if not self._closing and not event.is_stopped():
                merged_text = "\n".join(text for text in batch.texts if text)
                event.message_str = merged_text
                event.message_obj.message_str = merged_text
                event.message_obj.message = batch.chain
                event.set_extra("private_message_wait_count", batch.count)
                self.logger.debug("私聊已合并 %d 条消息，继续原有回复流程。", batch.count)
        finally:
            if self._pending.get(key) is batch:
                del self._pending[key]

    def close(self):
        self._closing = True
        for batch in self._pending.values():
            batch.owner.stop_event()
            batch.changed.set()
        self._pending.clear()
