"""Let proactive comments address the shared topic instead of its last speaker."""

import json

from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.platform import MessageType
from astrbot.builtin_stars.astrbot.group_chat_context import GROUP_HISTORY_HEADER
from ...temporary_context import set_temporary_text

TOPIC_EXTRA = "media_reply_filter_topic_context"
TOPIC_HEADER = "[本次群聊话题资料]\n"
DEFAULT_TOPIC_STYLE = """按照当前人格，像群里的普通参与者一样，就大家共同讨论的话题或群体现象自然插一句。可以表达感受、补充观点、接梗或适度吐槽，由语境和人格决定，不要求每次调侃。
不要把触发本轮的最后一条消息当成专门向你提出的问题，也不必寻找一个具体回复对象。不要自动点名、艾特或引用最后一个发言的人，不强行追问别人。
关注聊天中真实出现的内容；不要编造群体共识，不把少数人的私事扩大成全群现象，不假装了解未提供的图片内容。自然简洁，不写聊天总结、分析报告或判断过程，不套固定开场、口头禅或示例台词。"""
TOPIC_JUDGE_RULES = """本次额外区分两种接话方式：
TOPIC：对公开讨论的话题、群里共同经历或聊天现象发表评论，不特定回复某一个人。多人接梗、多人连续复读、一起吐槽或明显的话题转变，都可以成为相关评论的素材；能自然表达新感受、观点或人格化反应时可以 TOPIC。评价复读现象与机械跟着复读不同，不因出现重复消息就一律排除；没有可评论的内容或只会重复原话时仍 IGNORE。
REPLY：明确在延续与机器人的对话，适合回应当前发言者。
IGNORE：没有自然且相关的参与点。别人之间的私事、只适合特定对象回答的问题仍应忽略，不能借 TOPIC 绕过谈话关系判断。
不需要有人提问、艾特机器人或邀请发言才可选择 TOPIC。不要为了插话而强行评论；以上分类说明用于细化原规则中的重复刷屏与回应对象判断。"""


def is_topic(event):
    return bool(event.get_extra(TOPIC_EXTRA))


def prepare_topic_request(event, request, style):
    payload = event.get_extra(TOPIC_EXTRA)
    if not payload:
        return
    # The same selected context used by the judge is enough for generation.
    # Replace only the framework's group-history block; keep life, memory,
    # attachments and other plugins' request parts intact.
    request.extra_user_content_parts[:] = [
        part for part in request.extra_user_content_parts
        if not getattr(part, "text", "").startswith(GROUP_HISTORY_HEADER)
    ]
    set_temporary_text(request, TOPIC_HEADER, json.dumps(payload, ensure_ascii=False))
    set_temporary_text(request, "[群聊话题插话方式]\n",
                       "本轮是你自主参与群聊的话题发言，当前触发消息只是群聊素材，并非指定向你提问。"
                       "结合本次群聊话题资料，按照当前人格自然表达，无需回复指定的人。\n"
                       + str(style or DEFAULT_TOPIC_STYLE))



def topic_components(components):
    had_mention = any(isinstance(component, At) for component in components)
    result = []
    for component in components:
        if isinstance(component, (At, Reply)):
            continue
        if had_mention and not result and isinstance(component, Plain) and component.text.startswith("\n"):
            component = Plain(component.text[1:])
        result.append(component)
    return result


def wrap_topic_delivery(event):
    if (not is_topic(event) or event.get_platform_name() != "aiocqhttp"
            or event.get_message_type() != MessageType.GROUP_MESSAGE
            or event.get_extra("media_reply_filter_topic_send_wrapped")):
        return
    original_send = event.send

    async def send_topic(message):
        return await original_send(message.derive(topic_components(message.chain)))

    # Core may add its automatic quote after result-decorating hooks.
    event.send = send_topic
    event.set_extra("media_reply_filter_topic_send_wrapped", True)


def prepare_topic_tool(event, tool, arguments):
    if (not is_topic(event) or getattr(tool, "name", "") != "send_message_to_user"
            or not isinstance(arguments, dict)
            or str(arguments.get("session") or event.unified_msg_origin) != event.unified_msg_origin):
        return
    messages = arguments.get("messages")
    if isinstance(messages, list):
        arguments["messages"] = [message for message in messages if not isinstance(message, dict)
                                 or str(message.get("type", "")).lower() not in {"at", "atall", "at_all", "reply"}]
