"""Per-group text echo decisions; no model calls or message sending."""

import random
from collections import deque
from dataclasses import dataclass, field


@dataclass
class GroupState:
    text: str = ""
    count: int = 0
    senders: set[str] = field(default_factory=set)
    last_seen: float = 0.0
    joined: bool = False
    follow_checked: bool = False
    last_repeat: float | None = None
    recent_sent: deque = field(default_factory=lambda: deque(maxlen=20))
    message_ids: deque = field(default_factory=lambda: deque(maxlen=64))


class GroupRepeater:
    SAME_TEXT_COOLDOWN = 300

    def __init__(self, config: dict):
        self.config = config
        self.probability = float(config.get("probability", 2))
        self.follow_probability = float(config.get("follow_probability", 100))
        self.follow_count = int(config.get("follow_count", 3))
        self.cooldown = float(config.get("cooldown_seconds", 60))
        self.window = float(config.get("chain_window_seconds", 60))
        self.max_length = int(config.get("max_text_length", 120))
        if not (0 <= self.probability <= 100 and 0 <= self.follow_probability <= 100):
            raise ValueError("复读概率请填写 0–100 的百分数。")
        if not (2 <= self.follow_count <= 10 and self.cooldown >= 0 and self.window > 0 and self.max_length > 0):
            raise ValueError("连续复读条数应在 2–10 之间，冷却非负，时间窗口和字数上限应大于 0。")
        self.groups = {}

    def break_chain(self, key):
        state = self.groups.get(key)
        if state:
            state.text = ""
            state.count = 0
            state.senders.clear()
            state.joined = state.follow_checked = False

    def remember_sent(self, key, text: str, now: float):
        if text.strip():
            state = self.groups.setdefault(key, GroupState())
            state.recent_sent.append((text.strip(), now))

    @staticmethod
    def _hit(percent):
        return percent >= 100 or (percent > 0 and random.random() * 100 < percent)

    def consider(self, key, sender: str, text: str, message_id: str, now: float, *, signature: str | None = None, restore_only: bool = False):
        """Return 'random' or 'follow', reserving a repeat before any await."""
        state = self.groups.setdefault(key, GroupState())
        if message_id:
            if message_id in state.message_ids:
                return None
            state.message_ids.append(message_id)
        normalized = text.strip()
        identity = signature if signature is not None else normalized
        if identity != state.text or now - state.last_seen > self.window:
            self.break_chain(key)
            state.text = identity
        state.count += 1
        state.senders.add(sender)
        state.last_seen = now
        while state.recent_sent and now - state.recent_sent[0][1] >= self.SAME_TEXT_COOLDOWN:
            state.recent_sent.popleft()
        if restore_only:
            return None
        if state.joined or any(sent == normalized for sent, _ in state.recent_sent):
            return None
        if state.last_repeat is not None and now - state.last_repeat < self.cooldown:
            return None

        reason = None
        if (
            self.config.get("follow_enabled", True)
            and not state.follow_checked
            and state.count >= self.follow_count
            and len(state.senders) >= 2
        ):
            state.follow_checked = True
            if self._hit(self.follow_probability):
                reason = "follow"
        if reason is None and self._hit(self.probability):
            reason = "random"
        if reason:
            state.joined = True
            state.last_repeat = now
            self.remember_sent(key, normalized, now)
        return reason

    def clear(self):
        self.groups.clear()
