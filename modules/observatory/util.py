"""Small shared helpers for the observatory plugin.

No AstrBot imports here so the module can be used by tests and writer threads.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone
from typing import Any

EMOJI_PATTERN = re.compile("[\u200d\u2600-\u27bf\ufe00-\ufe0f\U0001f000-\U0001faff]")

# Must stay aligned with astrbot.core.agent.context.token_counter.EstimateTokenCounter
# (4.29.x). This is a local *estimate* only; API-reported usage always wins in UI.
CHINESE_TOKEN_RATE = 0.6
EMOJI_TOKEN_RATE = 2.0
OTHER_TOKEN_RATE = 0.3


def utc_now() -> float:
    return time.time()


def ts_to_local_text(ts: float | None, tz_name: str = "Asia/Shanghai") -> str:
    if ts is None:
        return ""
    try:
        from zoneinfo import ZoneInfo

        return datetime.fromtimestamp(float(ts), ZoneInfo(tz_name)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except Exception:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )


def parse_ts(value: Any) -> float | None:
    """Parse native DB timestamps (naive UTC datetime / ISO text / epoch)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    chinese = 0
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            chinese += 1
    emoji = len(EMOJI_PATTERN.findall(text))
    other = max(0, len(text) - chinese - emoji)
    return int(
        chinese * CHINESE_TOKEN_RATE
        + emoji * EMOJI_TOKEN_RATE
        + other * OTHER_TOKEN_RATE
    )


def estimate_json_tokens(payload: Any) -> int:
    try:
        return estimate_tokens(json.dumps(payload, ensure_ascii=False))
    except Exception:
        return 0


def sha1_short(text: str, length: int = 20) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:length]


def text_hash(text: str) -> str:
    normalized = " ".join((text or "").split())
    return hashlib.sha1(normalized.encode("utf-8", "replace")).hexdigest()


def clip(text: Any, limit: int) -> str:
    if text is None:
        return ""
    value = str(text)
    if limit <= 0 or len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def json_loads(text: Any, default: Any = None) -> Any:
    if text is None:
        return default
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def parse_umo(umo: str | None) -> tuple[str, str, str]:
    """Return (platform_id, message_type, peer_id) from a UMO string.

    UMO format is ``platform_id:MessageType:peer_id``. Unknown input returns
    empty components; callers must not treat this as a conversation identity.
    """
    text = (umo or "").strip()
    if not text:
        return "", "", ""
    parts = text.split(":", 2)
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return text, "", ""


def scope_id_for(platform_id: str, message_type: str, peer_id: str) -> str:
    """Stable scope key.

    ``bot_self_id`` is stored as scope metadata but is not part of the key:
    AstrBot's native message table does not expose the bot account for
    historical rows, and mixing two different keys for the same chat would
    split one conversation in two. See README "已知限制".
    """
    raw = "\x1f".join([platform_id, message_type, peer_id])
    return sha1_short(raw, 24)


def is_data_uri(url: str | None) -> bool:
    return isinstance(url, str) and url.strip().lower().startswith("data:")


def safe_url(url: Any, limit: int = 800) -> str | None:
    """Keep http(s) references, never base64 payloads or local absolute paths."""
    if not isinstance(url, str) or not url.strip():
        return None
    value = url.strip()
    if is_data_uri(value):
        return None
    if value.startswith(("http://", "https://")):
        return clip(value, limit)
    # Local path: keep only the file name to avoid exposing host layout.
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    return clip(f"[local] {name}", limit)


def normalize_text(text: Any) -> str:
    return " ".join(str(text or "").split())


def bucket_start(ts: float, step_seconds: int) -> float:
    return math.floor(float(ts) / step_seconds) * step_seconds


SECTION_LABELS = {
    "persona_system": "人格 / 系统规则",
    "history": "历史对话",
    "current_message": "当前消息",
    "temp_injection": "临时注入",
    "tool_result": "工具结果",
    "compression_summary": "压缩摘要",
    "tool_call_history": "历史模型工具调用",
    "checkpoint": "内部检查点",
    "uncategorized": "未分类内容",
}
SECTION_ORDER = list(SECTION_LABELS)


def is_reminder_text(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return False
    return (
        value.startswith("<system_reminder")
        or value.startswith("[SYSTEM NOTICE]")
        or value.startswith("Current datetime:")
    )


def classify_message(
    role: str,
    meaningful_text: str,
    has_tool_calls: bool,
    index: int,
    last_user_index: int,
    *,
    no_save: bool = False,
    has_reminder: bool = False,
    only_reminder: bool = False,
) -> str:
    """Group one input message for display.

    Order matters: the current user turn stays "current_message" even when a
    system reminder is appended to the same message.
    """
    if role == "_checkpoint":
        return "checkpoint"
    if role == "system":
        return "persona_system"
    if role == "tool":
        return "tool_result"
    if role == "assistant" and has_tool_calls:
        return "tool_call_history"
    if role == "user":
        text = meaningful_text or ""
        if "previous history conversation summary" in text.lower() or "对话历史总结" in text:
            return "compression_summary"
        if only_reminder or (no_save and not text.strip()):
            return "temp_injection"
        if index == last_user_index:
            return "current_message"
        return "history"
    return "history" if role == "assistant" else "uncategorized"


def recompute_sections(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Recompute display groups/sections from a stored input snapshot.

    The snapshot keeps the real parts; grouping is presentation, so it can be
    recomputed when the classifier improves. Token numbers are the stored local
    estimates; they are never a substitute for API usage.
    """
    if not messages:
        return [], []
    prepared: list[tuple[dict, list[str], str, list[str], bool]] = []
    last_user_index = -1
    raw_last_user_index = -1
    for message in messages:
        parts = message.get("parts") or []
        texts = [str(part.get("text") or "") for part in parts if part.get("type") == "text"]
        meaningful = [text for text in texts if not is_reminder_text(text)]
        has_reminder = any(is_reminder_text(text) for text in texts)
        prepared.append((message, texts, "\n".join(meaningful), meaningful, has_reminder))
        if message.get("role") == "user":
            raw_last_user_index = int(message.get("index", 0))
            if "\n".join(meaningful).strip():
                last_user_index = int(message.get("index", 0))
    if last_user_index < 0:
        # Media-only current turn (no text part at all): still the current turn.
        last_user_index = raw_last_user_index
    aggregated: dict[str, dict] = {}
    result: list[dict] = []
    for message, texts, meaningful_text, meaningful, has_reminder in prepared:
        role = message.get("role") or "unknown"
        group = classify_message(
            role,
            meaningful_text,
            bool(message.get("tool_calls")),
            int(message.get("index", 0)),
            last_user_index,
            no_save=bool(message.get("no_save")),
            has_reminder=has_reminder,
            only_reminder=has_reminder and not meaningful_text.strip(),
        )
        updated = {**message, "group": group}
        result.append(updated)
        section = aggregated.setdefault(
            group,
            {
                "key": group,
                "label": SECTION_LABELS.get(group, group),
                "message_indices": [],
                "tokens_estimate": 0,
            },
        )
        section["message_indices"].append(int(message.get("index", 0)))
        section["tokens_estimate"] += int(message.get("est_tokens") or 0)
    sections = [aggregated[key] for key in SECTION_ORDER if key in aggregated]
    return result, sections
