"""Authenticated AstrBot plugin-page endpoints for today's drawing usage."""
from __future__ import annotations

import json
import re

from astrbot.api import logger
from astrbot.api.web import request


class QuotaAdmin:
    def __init__(self, plugin):
        self.plugin = plugin

    def register(self):
        prefix = "/astrbot_plugin_openai_image/page/quota"
        self.plugin.context.register_web_api(prefix, self.get_status, ["GET"], "今日绘图额度")
        self.plugin.context.register_web_api(prefix + "/reset", self.reset, ["POST"], "重置今日已完成绘图次数")

    def snapshot(self):
        plugin = self.plugin
        result = plugin._group_quota.today(plugin._quota_limit())
        for row in result["entries"]:
            try:
                platform, bot_id, sender_id = json.loads(row["actor"])
            except (ValueError, TypeError):
                platform, bot_id, sender_id = "unknown", "", "未知用户"
            row.update(platform=str(platform), bot_id=str(bot_id), sender_id=str(sender_id))
        result.update(limit=plugin._quota_limit(), enabled=bool(plugin.config.get("group_daily_quota_enabled", True)),
                      admin_exempt=bool(plugin.config.get("group_daily_quota_admin_exempt", True)))
        return result

    async def get_status(self):
        if not request.username:
            return {"status": "error", "message": "请先登录 AstrBot 管理面板。"}
        result = self.snapshot()
        for row in result["entries"]:
            row.pop("actor")
        return {"status": "ok", "data": result}

    async def reset(self):
        if not request.username:
            return {"status": "error", "message": "请先登录 AstrBot 管理面板。"}
        body = await request.json(default=None)
        if not isinstance(body, dict) or body.get("scope") not in {"all", "user"}:
            return {"status": "error", "message": "请选择重置全部群友或指定 QQ。"}
        snapshot = self.snapshot()
        actors = None
        if body["scope"] == "user":
            sender = str(body.get("sender_id") or "").strip()
            if not re.fullmatch(r"[0-9]{5,15}", sender):
                return {"status": "error", "message": "请输入正确的 QQ 号。"}
            actors = [r["actor"] for r in snapshot["entries"] if r["platform"] == "aiocqhttp" and r["sender_id"] == sender]
        try:
            result = self.plugin._group_quota.reset_today(str(body.get("day") or ""), actors)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        logger.info("OpenAIImage quota reset: operator=%s scope=%s users=%s completed_count=%s", request.username, body["scope"], result["reset_users"], result["reset_count"])
        return {"status": "ok", "data": result}
