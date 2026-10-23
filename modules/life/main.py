"""Inject the character's current college routine into each chat request."""

from datetime import datetime

from astrbot.api.event import AstrMessageEvent
from ...runtime import deferred_filter as filter
from astrbot.api.star import Context
from ...runtime import SuiteModule as Star
from astrbot.core.agent.message import TextPart
from astrbot.core.provider.entities import ProviderRequest

from .judge import ScheduleJudge
from .schedule import BEIJING_TIME, CONTEXT_HEADER, DEFAULT_COURSES, DEFAULT_ROUTINE, LifeSchedule


class RealLifePlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self._config_key = None
        self._schedule = None
        self._config_error = None
        self._judge = ScheduleJudge(context, config, self.logger)

    async def initialize(self):
        if self._get_schedule() is None:
            raise ValueError(self._config_error)
        self.logger.info("真实生活已加载：按北京时间匹配日程，独立模型按需选择资料。")

    def _get_schedule(self) -> LifeSchedule | None:
        key = (str(self.config.get("weekly_courses", DEFAULT_COURSES)),
               str(self.config.get("daily_routine", DEFAULT_ROUTINE)),
               str(self.config.get("student_identity", "地理学专业大学生")),
               bool(self.config.get("classes_enabled", True)))
        if key != self._config_key:
            self._config_key = key
            try:
                self._schedule = LifeSchedule(*key)
                self._config_error = None
            except ValueError as exc:
                self._schedule = None
                self._config_error = str(exc)
                self.logger.error("真实生活配置有误：%s", exc)
        return self._schedule

    def _is_enabled(self, event: AstrMessageEvent) -> bool:
        scope = "private_enabled" if event.is_private_chat() else "group_enabled"
        return bool(self.config.get("enabled", True) and self.config.get(scope, True))

    @filter.on_llm_request()
    async def inject_life(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self._is_enabled(event) or event.is_stopped():
            return
        schedule = self._get_schedule()
        if schedule is None:
            return
        now = datetime.now(BEIJING_TIME)
        # Event text is the current turn. req.prompt can contain an entire group
        # transcript or other plugins' hidden instructions, so it is not copied.
        selected = await self._judge.select(schedule, now, event.get_message_str(), req.contexts)
        if event.is_stopped():
            return
        # Only replace this plugin's own temporary part if a request is reused.
        req.extra_user_content_parts[:] = [part for part in req.extra_user_content_parts
                                           if not (isinstance(part, TextPart) and part.text.startswith(CONTEXT_HEADER))]
        if selected is None:
            text = schedule.build_context(now)
        elif not selected:
            return
        else:
            text = schedule.build_selected_context(now, selected)
        req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())

    @filter.llm_tool(name="get_real_life_schedule")
    async def get_real_life_schedule(self, event: AstrMessageEvent, day: str = "今天") -> str:
        """回答角色其他时段安排或课表前必须查询，不能凭印象编造；当前活动已随聊天提供。

        Args:
            day (string): 今天、明天、后天、昨天、周一至周日、本周（整周课表）或 YYYY-MM-DD。
        """
        if not self._is_enabled(event):
            return "当前会话未启用真实生活。"
        schedule = self._get_schedule()
        if schedule is None:
            return "日程配置有误，请管理员检查真实生活插件的配置。"
        return schedule.query(day, datetime.now(BEIJING_TIME))
