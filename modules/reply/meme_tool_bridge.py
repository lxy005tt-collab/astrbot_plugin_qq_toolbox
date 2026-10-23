"""Translate same-session message-tool meme markers through Meme Manager."""

import os
import shutil
import tempfile
from pathlib import Path

from astrbot.api.message_components import Plain
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path


PRIVATE_MEME_REPLY_BOUNDARY = """[私聊表情使用节奏]
当前是一对一私聊。你喜欢主动用表情包表达情绪和接话，不需要等用户要求。根据当下语境和表达意图，自主选择纯文字、图文结合或纯表情。
不要每次回复都带图，也不要把附带一张表情包变成每条文字回复的固定流程。有贴切的表情时可以自然使用，也可以连续使用；只回文字也是正常选择。自主把握节奏，不按固定比例、不凑次数，也不刻意轮流切换回复形式。用户有具体的配图或输出格式要求时遵循。不要向用户解释本段规则、选图频率或内部标记。
"""


class MemeToolBridge:
    def __init__(self, context, logger):
        self.context = context
        self.logger = logger
        self.files = {}

    @staticmethod
    def _remove_files(paths):
        for path in paths:
            Path(path).unlink(missing_ok=True)

    async def prepare(self, event, tool, tool_args):
        if getattr(tool, "name", "") != "send_message_to_user" or not isinstance(tool_args, dict):
            return
        if event.get_platform_name() != "aiocqhttp":
            return
        session = tool_args.get("session") or event.unified_msg_origin
        if str(session) != event.unified_msg_origin:
            return
        messages = tool_args.get("messages")
        if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
            return
        if not any(str(m.get("type", "")).lower() == "plain" and "&&" in str(m.get("text", "")) for m in messages):
            return
        metadata = self.context.get_registered_star("meme_manager")
        manager = metadata.star_cls if metadata else None
        if manager is None or not callable(getattr(manager, "compat_prepare_message", None)):
            return

        rewritten = []
        copied_files = []
        native_temp_files = []
        changed = False
        try:
            for message in messages:
                text = str(message.get("text", ""))
                if str(message.get("type", "")).lower() != "plain" or "&&" not in text:
                    rewritten.append(message)
                    continue
                prepared = await manager.compat_prepare_message(event, [Plain(text)])
                native_temp_files.extend(prepared.get("temp_files") or [])
                if not prepared.get("emotions"):
                    rewritten.append(message)
                    continue
                changed = True
                for component in prepared["cleaned_chain"].chain:
                    if isinstance(component, Plain) and component.text.strip():
                        rewritten.append({**message, "text": component.text})
                for image in prepared.get("images") or []:
                    source = Path(await image.convert_to_file_path())
                    # Native message-tool file permissions still run afterwards.
                    # Only the selected meme is copied into its allowed temp root.
                    temp_root = Path(get_astrbot_temp_path())
                    temp_root.mkdir(parents=True, exist_ok=True)
                    descriptor, destination = tempfile.mkstemp(prefix="vivi-meme-", suffix=source.suffix, dir=temp_root)
                    os.close(descriptor)
                    copied_files.append(destination)
                    shutil.copy2(source, destination)
                    rewritten.append({"type": "image", "path": destination})
            if changed:
                tool_args["messages"] = rewritten
                self.files[id(tool_args)] = copied_files
                self.logger.info("已将直接发送工具中的表情标记转换为 %d 张图片。", len(copied_files))
        except Exception as error:
            # Do not let a failed conversion fall through and publish raw tags.
            # An empty list makes the original tool return an error to the model.
            tool_args["messages"] = []
            self._remove_files(copied_files)
            self.logger.warning("工具发送表情转换失败（%s），已阻止标记原样发出。", type(error).__name__)
        finally:
            self._remove_files(native_temp_files)

    def finished(self, tool_args):
        self._remove_files(self.files.pop(id(tool_args), []))

    def close(self):
        for paths in self.files.values():
            self._remove_files(paths)
        self.files.clear()
