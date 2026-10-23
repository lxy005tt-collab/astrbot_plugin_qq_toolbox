"""Periodically requeue unanswered private messages through the native pipeline."""

import asyncio
import copy
import json
import time
from collections import defaultdict
from pathlib import Path
from weakref import WeakValueDictionary

from aiocqhttp import Event
from astrbot.api.platform import MessageType
from astrbot.core.star.session_llm_manager import SessionServiceManager
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


RECOVERY_EXTRA = "private_reply_watchdog_recovery"


class PrivateReplyWatchdog:
    def __init__(self, context, settings, logger, state_path=None):
        self.context = context
        self.logger = logger
        self.enabled = settings.get("enabled", True)
        self.interval = float(settings.get("interval_minutes", 20)) * 60
        if self.interval <= 0:
            raise ValueError("私聊漏回复检查间隔必须大于 0 分钟。")
        self.path = Path(state_path) if state_path else Path(get_astrbot_data_path()) / "plugin_data/astrbot_plugin_media_reply_filter/private_reply_watchdog.json"
        self.pending = json.loads(self.path.read_text(encoding="utf-8-sig")).get("pending", {}) if self.path.exists() else {}
        self.running = defaultdict(set)
        self.queued = WeakValueDictionary()
        self.lock = asyncio.Lock()
        self.task = None

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 1, "pending": self.pending}, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def start(self):
        if self.enabled and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self.loop())

    async def loop(self):
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.check_once()
            except Exception as error:
                self.logger.warning("私聊漏回复巡检失败（%s），下个周期再检查。", type(error).__name__)

    def acknowledge(self, event):
        umo = event.unified_msg_origin
        record = self.pending.get(umo)
        source_ids = event.get_extra("private_message_wait_source_ids") or [str(event.message_obj.message_id)]
        if record and record["message_id"] in source_ids:
            del self.pending[umo]
            self.save()

    async def observe(self, event):
        if not self.enabled or event.get_platform_name() != "aiocqhttp" or event.get_message_type() != MessageType.FRIEND_MESSAGE:
            return
        raw = event.message_obj.raw_message
        if not raw:
            return
        umo = event.unified_msg_origin
        if raw.get("post_type") == "notice":
            if raw.get("notice_type") == "friend_recall":
                record = self.pending.get(umo)
                if record and record["message_id"] == str(raw.get("message_id")):
                    del self.pending[umo]
                    self.save()
            return
        if (raw.get("post_type") != "message" or not event.get_messages()
                or str(event.get_sender_id()) == str(event.get_self_id())
                or event.get_extra("handlers_parsed_params", {})
                or not event.is_at_or_wake_command):
            return
        config = self.context.get_config(umo=umo)
        if not config.get("provider_settings", {}).get("enable", True) or not await SessionServiceManager.should_process_llm_request(event):
            return

        message_id = str(event.message_obj.message_id)
        if event.get_extra(RECOVERY_EXTRA):
            current = self.pending.get(umo)
            if current is None or current["message_id"] != message_id:
                event.stop_event()
                return
        else:
            self.pending[umo] = {
                "message_id": message_id,
                "platform_id": event.get_platform_id(),
                "peer_id": str(event.get_sender_id()),
                "received_at": time.time(),
                "last_attempt_at": 0,
                "raw": copy.deepcopy(dict(raw)),
            }
            self.save()

        current_task = asyncio.current_task()
        self.running[umo].add(current_task)

        def finished(task):
            self.running[umo].discard(task)
            if not self.running[umo]:
                self.running.pop(umo, None)
            if self.queued.get(umo) is event:
                self.queued.pop(umo, None)

        current_task.add_done_callback(finished)
        original_send = event.send

        async def send_and_record(message):
            result = await original_send(message)
            if message.chain and not event.get_extra("agent_stop_requested"):
                self.acknowledge(event)
            return result

        event.send = send_and_record

    async def check_once(self, now=None):
        if not self.enabled or self.lock.locked():
            return 0
        now = time.time() if now is None else now
        recovered = 0
        async with self.lock:
            for umo, record in list(self.pending.items()):
                if any(not task.done() for task in self.running.get(umo, ())) or umo in self.queued:
                    continue
                if now - record.get("last_attempt_at", 0) < self.interval:
                    continue
                platform = self.context.get_platform_inst(record["platform_id"])
                if platform is None:
                    continue
                config = self.context.get_config(umo=umo)
                if not config.get("provider_settings", {}).get("enable", True) or not await SessionServiceManager.is_llm_enabled_for_session(umo):
                    continue
                record["last_attempt_at"] = now
                self.save()
                try:
                    async with asyncio.timeout(30):
                        # Refresh attachments through OneBot instead of replaying
                        # temporary local files that the old pipeline cleaned up.
                        fresh = await platform.get_client().call_action("get_msg", message_id=int(record["message_id"]))
                        payload = copy.deepcopy(record["raw"])
                        for field in ("message", "sender", "time", "raw_message"):
                            if field in fresh:
                                payload[field] = fresh[field]
                        if not payload.get("message") or str(payload["sender"]["user_id"]) != record["peer_id"]:
                            continue
                        payload.update(post_type="message", message_type="private", user_id=record["peer_id"], message_id=int(record["message_id"]))
                        message = await platform.convert_message(Event.from_payload(payload))
                        if message is None:
                            continue
                        event = platform.create_event(message)
                        if event.unified_msg_origin != umo or self.pending.get(umo) is not record:
                            continue
                        event.set_extra(RECOVERY_EXTRA, True)
                        self.queued[umo] = event
                        platform.commit_event(event)
                        recovered += 1
                        self.logger.info("私聊漏回复巡检：已为会话 %s 的原消息 ID=%s 触发一次正常回复流程。", umo, record["message_id"])
                except Exception as error:
                    self.logger.warning("私聊漏回复补回失败（%s），本周期不再重复触发。", type(error).__name__)
        return recovered

    async def close(self):
        self.enabled = False
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
