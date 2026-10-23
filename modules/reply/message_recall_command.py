"""Administrator-only recall of the exact message quoted by /ch."""

import math
from time import time

from astrbot.api.message_components import Reply


async def recall_quoted_message(event, logger):
    event.stop_event()
    if not event.is_admin():
        return "只有 AstrBot 管理员可以使用 /ch。"
    if event.get_platform_name() != "aiocqhttp":
        return "此撤回指令仅支持 QQ。"

    quoted = next(
        (component for component in event.get_messages() if isinstance(component, Reply)),
        None,
    )
    if quoted is None:
        return "请先引用要撤回的消息，再发送 /ch。"
    try:
        message_id = int(quoted.id)
    except (TypeError, ValueError):
        return "无法读取引用消息的 ID，请重新引用后发送 /ch。"

    raw = event.message_obj.raw_message or {}
    routing = {"self_id": raw["self_id"]} if raw.get("self_id") else {}
    try:
        original = await event.bot.call_action(
            "get_msg", message_id=message_id, **routing
        )
        sent_at = float(original["time"])
        if not math.isfinite(sent_at) or sent_at <= 0:
            raise ValueError("Missing original send time")
    except Exception as error:
        logger.warning("/ch 查询引用消息失败（%s）。", type(error).__name__)
        return "无法获取引用消息的发送时间，暂时无法撤回；消息可能已被撤回。"

    if time() - sent_at > 120:
        return "超过两分钟无法撤回。"

    try:
        await event.bot.call_action("delete_msg", message_id=message_id, **routing)
    except Exception as error:
        logger.warning("/ch 撤回引用消息失败（%s）。", type(error).__name__)
        return "撤回失败：机器人可能没有撤回权限，或该消息已被撤回。"

    logger.info("管理员 /ch 已撤回引用消息，消息 ID=%s。", message_id)
    return None
