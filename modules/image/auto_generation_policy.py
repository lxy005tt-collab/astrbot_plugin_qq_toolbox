"""Conservative intent checks for image tools in groups with auto generation off."""

from __future__ import annotations

import re


_TOOL_COMMANDS = {
    "generate_image": {"画图", "绘图", "draw", "image"},
    "edit_image": {"图生图", "img2img", "image2image", "修图", "改图", "edit", "inpaint"},
    "generate_mimi_image": {"画mimi", "mimi自拍"},
}
_COMMAND_PREFIX = re.compile(
    r"^\s*(?:[@＠][^\s,，]+\s*)*(?:[/／!！])?(?P<command>[^\s/／!！,，]+)",
    re.IGNORECASE,
)

_GENERATE_PATTERNS = (
    re.compile(r"(?:帮我|给我|替我|请你?|麻烦你?|我想(?:让你|要你)?|想让你)(?:帮我)?(?:画|绘制)\s*\S+", re.IGNORECASE),
    re.compile(r"(?:^|[\s，,。！？])(?:画|绘制)\s*(?:一张|张|一幅|幅|一个|个|一只|只|一下|出来)", re.IGNORECASE),
    re.compile(r"(?:生成|制作|做|来|发|出)\s*(?:一张|张|一幅|幅|一个|个)?\s*(?:图像|图片|插画|海报|壁纸|头像|照片|配图|图)", re.IGNORECASE),
    re.compile(r"(?:draw|generate|create|make)\s+(?:me\s+)?(?:an?\s+)?(?:image|picture|illustration|photo|wallpaper|avatar)\b", re.IGNORECASE),
    re.compile(r"\b(?:draw me|make me a picture|generate an image|create an image)\b", re.IGNORECASE),
)
_EDIT_PATTERNS = (
    re.compile(r"(?:帮我|请|麻烦|给我)?\s*(?:把|将)\s*(?:这张|这个|原图|附图|图片|照片|图)?[^。！？!?]{0,24}(?:改|修|编辑|重绘|重画|变成|改成|换成|加上|去掉|删除|移除|替换|调整|保留|裁剪)", re.IGNORECASE),
    re.compile(r"(?:帮我|请|麻烦|给我)\s*(?:改|修|编辑|重绘|重画|变成|改成|换成|加上|去掉|删除|移除|替换|调整|保留|裁剪)[^。！？!?]{0,32}", re.IGNORECASE),
    re.compile(r"(?:edit|retouch|change|replace|remove|add|crop)\s+(?:this|the|attached)\s+(?:image|picture|photo)\b", re.IGNORECASE),
)
_EDIT_IMAGELESS_DIRECTIVE = re.compile(
    r"^\s*(?:请|麻烦|帮我|给我)?\s*(?:变成|改成|换成|重绘|重画|修一下|编辑|裁剪|去掉|删除|移除|加上|替换|调整为|保留)",
    re.IGNORECASE,
)
_MIMI_REFERENT = re.compile(
    r"mimi|咪咪|画你自己|画你|给你画|你的自拍|你的头像|你的立绘|自画像|自拍照",
    re.IGNORECASE,
)
_MIMI_REQUEST = re.compile(
    r"(?:mimi|咪咪).{0,20}(?:画|绘制|生成|制作|做|自拍|头像|立绘)"
    r"|(?:画|绘制|生成|制作|做).{0,20}(?:mimi|咪咪|你自己|你的(?:自拍|头像|立绘|自画像))",
    re.IGNORECASE,
)
_NON_REQUEST_CONTEXT = re.compile(
    r"教程|怎么做|如何做|怎么画|如何画|怎么生成|如何生成|操作方法|步骤|代码示例|为什么",
    re.IGNORECASE,
)


def is_explicit_image_tool_request(
    message: str,
    tool_name: str,
    *,
    has_input_image: bool = False,
) -> bool:
    """Return whether the current user message explicitly requests this image action."""
    text = str(message or "").strip()
    tool_name = str(tool_name or "").strip()
    if not text or tool_name not in _TOOL_COMMANDS:
        return False

    command_match = _COMMAND_PREFIX.match(text)
    if command_match and command_match.group("command").casefold() in _TOOL_COMMANDS[tool_name]:
        return True

    if tool_name == "generate_image":
        if _NON_REQUEST_CONTEXT.search(text):
            return False
        return any(pattern.search(text) for pattern in _GENERATE_PATTERNS)

    if tool_name == "edit_image":
        if any(pattern.search(text) for pattern in _EDIT_PATTERNS):
            return True
        return bool(has_input_image and _EDIT_IMAGELESS_DIRECTIVE.search(text))

    if tool_name == "generate_mimi_image":
        if _NON_REQUEST_CONTEXT.search(text) or not _MIMI_REFERENT.search(text):
            return False
        return any(pattern.search(text) for pattern in _GENERATE_PATTERNS) or bool(
            _MIMI_REQUEST.search(text)
        )

    return False
