"""Associate OneBot recall notices with original events and final sends."""

from collections import OrderedDict
from time import monotonic
from weakref import WeakValueDictionary, ref


class RecallGuard:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.events = WeakValueDictionary()
        self.records = OrderedDict()
        self.recalled = OrderedDict()

    @staticmethod
    def scope(event):
        group = event.get_group_id()
        return (event.get_platform_id(), "group" if group else "private",
                str(group or event.get_sender_id()))

    @staticmethod
    def notice(event):
        raw = event.message_obj.raw_message
        if not raw or raw.get("post_type") != "notice":
            return None
        kind = raw.get("notice_type")
        if kind in {"friend_recall", "group_recall"} and raw.get("message_id") is not None:
            return kind, str(raw["message_id"])
        return None

    @staticmethod
    def source_ids(event):
        return event.get_extra("private_message_wait_source_ids") or [str(event.message_obj.message_id)]

    def prune(self):
        cutoff = monotonic() - 600
        for records in (self.records, self.recalled):
            while records:
                key, record = next(iter(records.items()))
                if len(records) <= 4096 and record["at"] >= cutoff:
                    break
                records.pop(key)

    def is_recalled(self, event):
        scope = self.scope(event)
        return self.enabled and any((scope, str(mid)) in self.recalled for mid in self.source_ids(event))

    def block(self, event):
        if not self.is_recalled(event):
            return False
        event.set_extra("agent_stop_requested", True)
        event.stop_event()
        event.get_result().chain = []
        event.set_extra("meme_manager_pending_images", [])
        return True

    def track(self, event):
        self.prune()
        key = (self.scope(event), str(event.message_obj.message_id))
        self.events[key] = event
        self.records.setdefault(key, {"at": monotonic(), "history_id": event.get_extra("_current_platform_message_history_id")})
        if not event.get_extra("media_reply_filter_recall_send_wrapped"):
            original = event.send

            async def send_if_present(message):
                if not self.block(event):
                    return await original(message)

            event.send = send_if_present
            event.set_extra("media_reply_filter_recall_send_wrapped", True)
        self.block(event)

    def update_history_id(self, event):
        # Native history is inserted after the early input hook, before the
        # proactive decision. Retain its ID even if recall interrupts judging.
        history_id = event.get_extra("_current_platform_message_history_id")
        if history_id is None:
            return
        key = (self.scope(event), str(event.message_obj.message_id))
        for records in (self.records, self.recalled):
            if key in records:
                records[key]["history_id"] = history_id

    def remember_group_record(self, group_context, event):
        key = (self.scope(event), str(event.message_obj.message_id))
        self.records.setdefault(key, {"at": monotonic()}).update(
            history_id=event.get_extra("_current_platform_message_history_id"),
            group_context=ref(group_context), umo=event.unified_msg_origin,
            record_id=event.get_extra("_group_context_record_id"),
        )
        self.update_history_id(event)

    async def remove_group_record(self, key):
        record = self.records.get(key, {})
        context_ref = record.get("group_context")
        context = context_ref() if context_ref else None
        record_id = record.get("record_id")
        if context is None or not record_id:
            return
        umo = record["umo"]
        async with context._get_lock(umo):
            ids = context._record_ids.get(umo)
            messages = context.raw_records.get(umo)
            if ids is None or messages is None:
                return
            kept = [(rid, text) for rid, text in zip(ids, messages) if rid != record_id]
            ids.clear()
            messages.clear()
            ids.extend(rid for rid, _ in kept)
            messages.extend(text for _, text in kept)

    async def recall(self, notice, message_id):
        self.prune()
        scope = self.scope(notice)
        key = (scope, message_id)
        self.recalled[key] = {"at": monotonic(), "history_id": self.records.get(key, {}).get("history_id")}
        self.recalled.move_to_end(key)
        blocked = 0
        for event in list(self.events.values()):
            if self.scope(event) == scope and self.block(event):
                blocked += 1
        await self.remove_group_record(key)
        return blocked

    def history_recalled(self, event, history_id):
        scope = self.scope(event)
        return self.enabled and any(
            key[0] == scope and record.get("history_id") == history_id
            for key, record in self.recalled.items() if record.get("history_id") is not None
        )

    def clear(self):
        self.enabled = False
        self.events.clear()
        self.records.clear()
        self.recalled.clear()
