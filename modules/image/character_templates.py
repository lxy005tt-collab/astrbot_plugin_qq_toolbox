"""Bundled character identities, with optional user-supplied references."""
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


@dataclass(frozen=True)
class CharacterTemplate:
    name: str
    reference_setting: str
    identity: str
    reference_asset: str


CHARACTERS = {
    "奶蛙": CharacterTemplate(
        "奶蛙", "character_milk_frog_reference",
        "黄色圆润胖乎乎的拟人小角色，绿色大眼睛、黑色圆瞳，细小微笑嘴，"
        "浅奶油色大肚皮，短粗四肢，手脚末端偏深橄榄色。形象以所附黄色设定图为准。",
        "milk_frog_turnaround.jpg",
    ),
    "大肥鱼": CharacterTemplate(
        "大肥鱼", "character_fat_fish_reference",
        "蓝色长发、发梢渐变浅蓝、蓝色大眼睛的可爱女仆角色；"
        "白色褶边头饰、蓝色蝴蝶结和侧边白色小翼装饰，深蓝女仆裙、白围裙，"
        "有蓝色鲸鱼图案及鱼尾造型细节。大肥鱼是这位角色的名字，"
        "不要把她画成真实的鱼。身份以所附蓝色人物设定图为准。",
        "fat_fish_turnaround.jpg",
    ),
}


def bundled_character_reference(name: str) -> Path:
    template = CHARACTERS.get(name)
    if template is None:
        raise ValueError("未找到对应的内置角色。")
    return Path(__file__).resolve().parents[2] / "views" / "dashboard" / "assets" / "characters" / template.reference_asset


def validate_character_reference(path: str, name: str) -> str:
    path = str(path or "").strip()
    file = Path(path).expanduser() if path else bundled_character_reference(name)
    if not file.is_absolute() or not file.is_file():
        if path:
            raise ValueError(f"{name}的自定义三视图路径无效；可清空该设置，改用插件内置三视图。")
        raise ValueError(f"{name}的内置三视图丢失，请重新安装完整插件包。")
    try:
        with Image.open(file) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError(f"{name}的设定图仅支持 PNG、JPEG 或 WebP。")
            image.verify()
    except (OSError, SyntaxError) as exc:
        raise ValueError(f"{name}的固定设定图无法读取，请管理员重新设置。") from exc
    return str(file.resolve())


def compose_character_prompt(template: CharacterTemplate, prompt: str, replace_subject: bool) -> str:
    if replace_subject:
        task = (
            f"这是主体替换编辑任务。第 1 张图片是要修改的原图，第 2 张图片是{template.name}的身份设定图。\n"
            "先识别原图最主要的人物、拟人角色或动物主体，再将这个主角的完整身份和身体形象"
            f"替换为{template.name}，不是只贴脸或叠加一个新角色。"
            "有多个角色时，优先按用户指定的对象替换；未指定时只替换主要主角，保留其他角色。\n"
            "尽量保留原图主角的姿态、动作、朝向、表情意图、画面占比和位置，"
            "保留背景、构图、光照及其他非目标内容。除非用户要求改变，沿用原图画风。\n"
        )
    else:
        task = (
            f"这是固定人物生成任务。所附第 1 张图片是{template.name}的身份设定图。\n"
            f"根据用户的场景、动作和风格要求创作{template.name}的新图片，保持该角色身份。"
            "场景、姿势、服装和画风按用户要求创作，不照抄设定图的白底布局。\n"
        )
    view_rule = (
        "第 2 张设定图中的多视角都是同一角色的身份参考，不复制它的角色数量或白底布局。"
        "保留第 1 张原图中非目标角色及原有排版。\n"
        if replace_subject else
        "设定图中的正面、侧面和背面是同一角色的不同视角，"
        "不要把多视图或设定图拼贴进最终画面。未指定角色数量时只画一个主角。\n"
    )
    return (
        task + "固定形象：" + template.identity + "\n"
        + view_rule + "只输出最终成图，不输出主体识别说明或分析。\n\n用户补充要求：\n"
        + (prompt.strip() or f"将原图的主要主体替换为{template.name}，其余尽量保留。")
    )
