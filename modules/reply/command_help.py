"""Render live command metadata as inert, human-readable instructions."""

import inspect
from collections import defaultdict

from astrbot.api.event import MessageChain
from astrbot.core.star.command_management import list_commands
from astrbot.core.star.filter.event_message_type import (
    EventMessageType,
    EventMessageTypeFilter,
)
from astrbot.core.star.star_handler import star_handlers_registry


# Stable handler names keep explanations correct when a command is renamed.
DETAILS = {
    "astrbot_plugin_media_reply_filter": {
        "command_menu": ("[全部]", "查看常用指令；“全部”仅管理员可用"),
        "recall_by_quote": ("", "先引用目标消息再执行；撤回两分钟内的消息"),
    },
    "astrbot_plugin_openai_image": {
        "draw_image": ("<画面描述>", "生成图片；例如画一只猫"),
        "image_to_image": ("<修改要求>", "先发图片再执行，按提示确认后生成"),
        "edit_image": ("<修改要求>", "先发原图再执行，按提示确认后修图"),
        "draw_milk_frog": ("[画面描述]", "画奶蛙；附图时替换图片主角"),
        "draw_fat_fish": ("[画面描述]", "画大肥鱼；附图时替换图片主角"),
        "drawing_quota": ("", "查询自己今天剩余的群聊绘图次数"),
    },
    "builtin_commands": {
        "help": ("", "查看框架自带帮助"),
        "reset": ("", "清空当前会话的聊天上下文"),
        "new_conv": ("", "新建一个会话"),
        "stop": ("", "停止当前正在执行的回复任务"),
        "stats": ("", "查看当前会话的用量统计"),
        "sid": ("", "查看当前会话的标识"),
        "name": ("<名称>", "设置当前会话的显示名称"),
        "provider": ("[编号] [子编号]", "不填参数查看模型服务，按返回的编号切换"),
        "update_dashboard": ("", "更新 AstrBot 管理页面"),
        "set_variable": ("<变量名> <值>", "设置当前会话的变量"),
        "unset_variable": ("<变量名>", "删除当前会话的变量"),
    },
    "astrbot_plugin_self_learning": {
        "start_learning_command": ("", "手动启动学习"),
        "stop_learning_command": ("", "停止学习"),
        "force_learning_command": ("", "立即执行一次学习"),
        "learning_status_command": ("", "查看学习状态"),
        "remember_command": ("", "先引用消息再执行，记住该对话及其上下文"),
        "affection_status_command": ("", "查看好感度状态"),
        "set_mood_command": ("<情绪名>", "设置 bot 情绪，例如 happy、calm、sad"),
    },
    "astrbot_plugin_livingmemory": {
        "lmem": ("", "查看长期记忆管理的子指令"),
        "status": ("", "查看长期记忆系统状态"),
        "search": ("<关键词> [条数]", "搜索长期记忆，条数默认 5"),
        "forget": ("<记忆编号>", "删除指定的长期记忆"),
        "rebuild_index": ("", "重建长期记忆检索索引"),
        "rebuild_graph": ("", "重建图记忆索引"),
        "webui": ("", "查看记忆管理页面的访问说明"),
        "summarize": ("[消息条数]", "立即总结当前会话的记忆"),
        "reset": ("", "重置当前会话的长期记忆上下文"),
        "cleanup": ("[preview或exec]", "清理历史记忆注入；默认 preview 只预览"),
        "help": ("", "查看长期记忆功能的详细帮助"),
    },
    "astrbot_plugin_codex_dispatch": {
        "codex_dispatch": ("<任务内容>", "仅私聊派发任务；status 查进度，cancel 取消；沿用白名单"),
    },
    "astrbot_plugin_pokepro": {
        "on_poke_cmd": ("<目标> [次数]", "目标可用@某人或“我”；戳全体仅管理员可用"),
    },
}
COMMON_DETAILS = {
    ("astrbot_plugin_openai_image", "draw_image"): ("<内容>", "画图"),
    ("astrbot_plugin_openai_image", "image_to_image"): ("<要求>", "先发图，再按要求修改"),
    ("astrbot_plugin_openai_image", "edit_image"): ("<要求>", "先发原图，再修图"),
    ("astrbot_plugin_openai_image", "draw_milk_frog"): ("[内容]", "画奶蛙"),
    ("astrbot_plugin_openai_image", "draw_fat_fish"): ("[内容]", "画大肥鱼"),
    ("astrbot_plugin_openai_image", "drawing_quota"): ("", "查绘图次数"),
    ("astrbot_plugin_media_reply_filter", "recall_by_quote"): ("", "引用消息后撤回，限两分钟内"),
    ("astrbot_plugin_pokepro", "on_poke_cmd"): ("<目标>", "戳一下"),
    ("builtin_commands", "reset"): ("", "清空聊天上下文"),
    ("builtin_commands", "stop"): ("", "停止回复"),
    ("astrbot_plugin_codex_dispatch", "codex_dispatch"): ("<任务>", "私聊派发任务"),
    ("astrbot_plugin_media_reply_filter", "command_menu"): ("", "查看常用指令"),
}
PARAM_NAMES = {
    "prompt": "内容", "query": "关键词", "k": "条数", "doc_id": "记忆编号",
    "alias": "名称", "key": "变量名", "value": "值", "category": "分类名",
    "category_name": "分类名", "description": "说明", "category_description": "分类说明",
    "emotion": "分类名", "emotion_name": "分类名", "name": "名称", "mode": "模式",
    "message_count": "消息条数", "idx": "编号", "idx2": "子编号",
}
PLUGIN_NAMES = {
    "builtin_commands": "基础指令",
    "astrbot_plugin_livingmemory": "长期记忆",
    "astrbot_plugin_self_learning": "自学习",
}
INTRO = (
    "使用时：在【】内的指令名前加半角斜杠，参数与指令之间留空格。\n"
    "<内容>需要填写，[内容]可选；【管理员】仅 AstrBot 管理员可用。\n"
    "以下均为说明文字，未直接列出可执行的斜杠指令。"
)


def safe_text(value):
    return " ".join(str(value or "").split()).replace("/", "／")


def parameter_usage(handler):
    if handler is None:
        return ""
    result = []
    params = list(inspect.signature(handler.handler).parameters.values())
    if params and params[0].name in ("self", "cls"):
        params = params[1:]
    for param in params[1:]:
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        name = PARAM_NAMES.get(param.name, param.name)
        result.append(f"<{name}>" if param.default is param.empty else f"[{name}]")
    return " ".join(result)


def visible_commands(items, selected, inherited_permission="everyone"):
    for item in items:
        if not item.get("enabled", True) or not item.get("plugin_activated", False):
            continue
        if selected not in (None, ["*"]) and not item.get("reserved"):
            if item.get("plugin") not in selected:
                continue
        current = dict(item)
        if inherited_permission == "admin":
            current["permission"] = "admin"
        yield current
        yield from visible_commands(
            item.get("sub_commands", []), selected, current.get("permission", "everyone")
        )


def command_origin(item):
    plugin, name = item.get("plugin", ""), item.get("handler_name", "")
    if plugin == "astrbot_plugin_qq_toolbox" and "__" in name:
        from ...runtime import LEGACY_BY_KEY
        key, name = name.split("__", 1)
        plugin = LEGACY_BY_KEY.get(key, plugin)
    return plugin, name


def command_line(item, compact=False):
    plugin, name = command_origin(item)
    handler = star_handlers_registry.get_handler_by_full_name(item["handler_full_name"])
    detail = COMMON_DETAILS.get((plugin, name)) if compact else None
    detail = detail or DETAILS.get(plugin, {}).get(name)
    if detail is None:
        description = next(
            (line.strip() for line in str(item.get("description") or "").splitlines() if line.strip()),
            "执行此功能",
        )
        detail = (parameter_usage(handler), description)
    usage, description = detail
    labels = []
    permission = item.get("permission")
    # These handlers also enforce admin permission inside their implementation.
    manual_admin = (plugin, name) in {
        ("astrbot_plugin_codex_dispatch", "codex_dispatch"),
        ("astrbot_plugin_media_reply_filter", "recall_by_quote"),
    }
    if permission == "admin" or manual_admin:
        labels.append("【管理员】")
    elif permission == "shared_group_admin":
        labels.append("【共享群限管理员】" if compact else "【共享群会话限管理员】")
    elif permission == "group_admin":
        labels.append("【群聊限管理员】")
    if handler is not None:
        for rule in handler.event_filters:
            if isinstance(rule, EventMessageTypeFilter):
                if rule.event_message_type == EventMessageType.PRIVATE_MESSAGE:
                    labels.append("【仅私聊】")
                elif rule.event_message_type == EventMessageType.GROUP_MESSAGE:
                    labels.append("【仅群聊】")
    command = safe_text(item.get("effective_command") or item.get("original_command"))
    invocation = " ".join(part for part in (command, safe_text(usage)) if part)
    aliases = item.get("aliases", [])
    alias_note = f"（别名：{'、'.join(safe_text(alias) for alias in aliases)}）" if aliases and not compact else ""
    return f"• 【{invocation}】{''.join(labels)} — {safe_text(description)[:120]}{alias_note}"


def render_command_pages(items, selected=None):
    groups = defaultdict(list)
    for item in visible_commands(items, selected):
        title = PLUGIN_NAMES.get(item.get("plugin")) or item.get("plugin_display_name") or item.get("plugin")
        groups[safe_text(title)].append(command_line(item))
    lines = []
    for title, commands in groups.items():
        lines.append(f"\n〔{title}〕")
        lines.extend(commands)
    if not lines:
        lines = ["当前会话没有已启用的指令。"]
    chunks, current = [], []
    for line in lines:
        if current and len("\n".join(current + [line])) > 1500:
            chunks.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        chunks.append("\n".join(current))
    return [
        f"Bot 指令说明（第 {index} 页，共 {len(chunks)} 页）\n"
        + (INTRO + "\n" if index == 1 else "")
        + chunk
        for index, chunk in enumerate(chunks, 1)
    ]


def render_common_commands(items, selected=None):
    available = {
        command_origin(item): item
        for item in visible_commands(items, selected)
    }
    lines = ["常用指令（使用时在名称前加半角斜杠）"]
    lines.extend(
        command_line(available[key], compact=True)
        for key in COMMON_DETAILS if key in available
    )
    lines.append("完整清单【管理员】：使用“指令 全部”，前面加半角斜杠。")
    return ["\n".join(lines)]


async def send_command_help(event, mode=""):
    event.stop_event()
    if mode.strip() == "全部" and not event.is_admin():
        await event.send(MessageChain().message("完整指令清单仅管理员可查看。"))
        return
    items = await list_commands()
    pages = (
        render_command_pages(items, event.plugins_name)
        if mode.strip() == "全部"
        else render_common_commands(items, event.plugins_name)
    )
    for page in pages:
        # Direct plain-text sending keeps the list out of LLM/tool execution,
        # text segmentation, TTS and automatic meme decoration.
        await event.send(MessageChain().message(page))
