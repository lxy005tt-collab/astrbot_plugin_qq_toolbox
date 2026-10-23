"""A deterministic weekly timetable and daily routine, without model calls."""

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
import re


BEIJING_TIME = timezone(timedelta(hours=8), "Asia/Shanghai")
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
CONTEXT_HEADER = "<real_life_schedule>"
LIFE_EXPERIENCE_RULES = (
    "这是角色的虚拟真实生活。在不违反给定人设、课表作息、时间地点、已经明确的经历和约定的前提下，"
    "你可以编造并自然叙述合理的生活细节和经历，例如餐食、课堂小事、日常活动、见闻和感受；"
    "不要求每个细节都先有资料记录。已给出的事实和已经确认的经历优先，不能为新内容随意推翻。"
    "新补充的细节应与前文保持连续，同一顿饭、同一件事被追问时应具体补充，"
    "不要只重复泛泛的活动名称；时间衔接和地点移动应合理。"
    "收拾、出门、步行和到达都需要时间，应承接前文已确认的位置与行动进度，结合已经过去的时间合理衔接。"
    "已经完成的准备不要在下一轮重新开始；课表的开课时间、上课地点和作息时段不能代替移动过程，也不证明已经到达。"
    "临近上课应合理提前准备和出发；路程未知时可补充合理且前后一致的虚拟过渡，不把估计当成已确认事实，无需逐步播报行程。"
    "没课且无明确约定时是个人时间，具体活动依人设和前文合理延续；不默认自习，也不虚构学校统一的自由活动时段。"
    "可以自愿学习或休闲，但别把“无固定安排”照读成正在做的事。"
    "固定课程及明确约定按给定时间执行；普通作息是生活背景，不要求整个时段每一分钟都持续同一动作。"
    "区分自己的经历和别人的经历，不把用户或他人说的事情自动认领为自己做过。"
    "尚未到来的事情作为打算或计划表达。图片生成、消息发送和其他工具执行结果仍以实际回执为准，不能编造成功。\n"
)

DEFAULT_COURSES = """周一 08:00-09:40 自然地理学 | 教室
周一 10:00-11:40 自然地理学 | 教室
周二 10:00-11:40 地图学 | 教室
周二 14:00-15:40 遥感概论 | 教室
周三 08:00-09:40 气象学与气候学 | 教室
周三 14:00-15:40 地理信息系统实验 | GIS实验室
周三 16:00-17:40 地理信息系统实验 | GIS实验室
周四 10:00-11:40 人文地理学 | 教室
周五 08:00-09:40 地理信息系统 | 教室
周五 14:00-15:40 区域地理学 | 教室"""

DEFAULT_ROUTINE = """每天 23:00-07:00 睡觉 | 宿舍
每天 07:00-07:30 起床、洗漱 | 宿舍
每天 07:30-08:00 吃早餐、准备出门 | 食堂或校园
每天 11:40-12:30 吃午饭 | 食堂
每天 12:30-13:30 午休 | 宿舍
每天 13:30-14:00 整理书本、准备下午的安排 | 校园
每天 17:40-18:30 吃晚饭 | 食堂
每天 18:30-19:00 散步、休息 | 校园
每天 22:30-23:00 洗漱、准备睡觉 | 宿舍"""


@dataclass(frozen=True)
class Entry:
    days: frozenset[int]
    start: int
    end: int
    activity: str
    location: str
    kind: str


@dataclass(frozen=True)
class Slot:
    start: int
    end: int
    activity: str
    location: str
    kind: str

    @property
    def interval(self) -> str:
        return f"{clock(self.start)}–{clock(self.end)}"


def clock(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def parse_time(value: str, *, is_end: bool = False) -> int:
    hour, minute = map(int, value.split(":"))
    if hour == 24 and minute == 0 and is_end:
        return 1440
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"无效时间：{value}")
    return hour * 60 + minute


def parse_entries(text: str, kind: str) -> tuple[Entry, ...]:
    entries = []
    selectors = {name: frozenset({i}) for i, name in enumerate(WEEKDAYS)}
    selectors.update({"每天": frozenset(range(7)), "工作日": frozenset(range(5)),
                      "周末": frozenset({5, 6}), "周天": frozenset({6})})
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.translate(str.maketrans({"：": ":", "–": "-", "—": "-", "～": "-", "｜": "|"}))
        match = re.fullmatch(r"(\S+)\s+(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s+(.+)", line)
        if not match:
            raise ValueError(f"第 {number} 行格式错误，应为：周一 08:00-09:40 活动 | 地点")
        day, start_text, end_text, description = match.groups()
        if day not in selectors:
            raise ValueError(f"第 {number} 行星期错误：{day}")
        start = parse_time(start_text)
        end = parse_time(end_text, is_end=True)
        if start == end:
            raise ValueError(f"第 {number} 行开始与结束时间相同")
        activity, _, location = description.partition("|")
        if not activity.strip():
            raise ValueError(f"第 {number} 行缺少活动")
        entries.append(Entry(selectors[day], start, end, activity.strip(), location.strip() or "未指定", kind))
    return tuple(entries)


def expand_day(entries: tuple[Entry, ...], weekday: int) -> list[Slot]:
    slots = []
    for entry in entries:
        overnight = entry.end < entry.start
        if weekday in entry.days:
            slots.append(Slot(entry.start, 1440 if overnight else entry.end,
                              entry.activity, entry.location, entry.kind))
        if overnight and (weekday - 1) % 7 in entry.days and entry.end:
            slots.append(Slot(0, entry.end, entry.activity, entry.location, entry.kind))
    return sorted(slots, key=lambda item: item.start)


def validate_overlaps(entries: tuple[Entry, ...], label: str) -> None:
    for weekday in range(7):
        slots = expand_day(entries, weekday)
        for previous, current in zip(slots, slots[1:]):
            if current.start < previous.end:
                raise ValueError(f"{label}{WEEKDAYS[weekday]}有重叠：{previous.interval}、{current.interval}")


class LifeSchedule:
    def __init__(self, courses: str = DEFAULT_COURSES, routine: str = DEFAULT_ROUTINE,
                 identity: str = "地理学专业大学生", classes_enabled: bool = True):
        self.courses = parse_entries(courses, "class") if classes_enabled else ()
        self.routine = parse_entries(routine, "routine")
        self.identity = identity.strip() or "地理学专业大学生"
        validate_overlaps(self.courses, "课表")
        validate_overlaps(self.routine, "作息")

    def courses_on(self, day: date) -> list[Slot]:
        return expand_day(self.courses, day.weekday())

    def timeline(self, day: date) -> list[Slot]:
        classes = self.courses_on(day)
        routines = expand_day(self.routine, day.weekday())
        breaks = [Slot(left.end, right.start, "课间休息、整理笔记或前往下一间教室", "教学区", "break")
                  for left, right in zip(classes, classes[1:]) if 0 < right.start - left.end <= 30]
        boundaries = sorted({0, 1440} | {minute for item in classes + breaks + routines
                                       for minute in (item.start, item.end)})
        result = []
        for start, end in zip(boundaries, boundaries[1:]):
            selected = next((item for item in classes + breaks + routines if item.start <= start < item.end), None)
            slot = replace(selected, start=start, end=end) if selected else Slot(start, end, "无固定安排", "未指定", "routine")
            if result and (result[-1].activity, result[-1].location, result[-1].kind) == (slot.activity, slot.location, slot.kind):
                result[-1] = replace(result[-1], end=end)
            else:
                result.append(slot)
        return result

    def describe_current(self, now: datetime) -> str:
        now = now.replace(tzinfo=BEIJING_TIME) if now.tzinfo is None else now.astimezone(BEIJING_TIME)
        day = now.date()
        minute = now.hour * 60 + now.minute
        timeline = self.timeline(day)
        index = next(i for i, item in enumerate(timeline) if item.start <= minute < item.end)
        current = timeline[index]
        current_activity = f"上《{current.activity}》" if current.kind == "class" else current.activity
        lines = [f"北京时间：{now:%Y-%m-%d %H:%M} {WEEKDAYS[day.weekday()]}",
                 f"身份：{self.identity}",
                 f"当前时段安排：{current.interval} {current_activity}", f"安排地点：{current.location}"]
        # Skip an identical overnight continuation when finding the next activity.
        upcoming = [(day, item) for item in timeline[index + 1:]]
        tomorrow = day + timedelta(days=1)
        upcoming.extend((tomorrow, item) for item in self.timeline(tomorrow))
        next_item = next(((date_, item) for date_, item in upcoming
                          if (item.activity, item.location, item.kind) != (current.activity, current.location, current.kind)), None)
        if next_item:
            date_, item = next_item
            prefix = "明天 " if date_ != day else ""
            lines.append(f"接下来：{prefix}{item.interval} {item.activity}")
        return "\n".join(lines)

    def describe_day(self, day: date) -> str:
        lines = [f"{day.isoformat()} {WEEKDAYS[day.weekday()]} 的生活安排："]
        lines.extend(f"{item.interval} {'上课：' if item.kind == 'class' else ''}{item.activity} | {item.location}"
                     for item in self.timeline(day))
        return "\n".join(lines)

    def describe_week(self, day: date) -> str:
        lines = ["一周固定课表（未列出的时间均无课）："]
        for weekday, name in enumerate(WEEKDAYS):
            reference = day + timedelta(days=weekday - day.weekday())
            lessons = self.courses_on(reference)
            text = "；".join(f"{item.interval} {item.activity}" for item in lessons) or "无课"
            lines.append(f"{name}：{text}")
        return "\n".join(lines)

    def judge_reference(self) -> dict[str, str]:
        def format_entries(entries):
            lines = []
            for item in entries:
                days = "每天" if len(item.days) == 7 else "/".join(WEEKDAYS[i] for i in sorted(item.days))
                lines.append(f"{days} {clock(item.start)}-{clock(item.end)} {item.activity} | {item.location}")
            return "\n".join(lines)
        return {"courses": format_entries(self.courses) or "无固定课程",
                "routine": format_entries(self.routine) or "无固定日常安排",
                "rule": "课程覆盖同时段普通作息；课表按周重复，未列出的时段无课。"}

    def resolve_day(self, requested: str, now: datetime) -> date:
        offsets = {"昨天": -1, "今天": 0, "明天": 1, "后天": 2}
        if requested in offsets:
            return now.date() + timedelta(days=offsets[requested])
        if requested in WEEKDAYS:
            return now.date() + timedelta(days=WEEKDAYS.index(requested) - now.weekday())
        return date.fromisoformat(requested)

    def build_selected_context(self, now: datetime, selected) -> str:
        now = now.replace(tzinfo=BEIJING_TIME) if now.tzinfo is None else now.astimezone(BEIJING_TIME)
        facts = []
        for selection in selected:
            if selection.scope == "current":
                facts.append(self.describe_current(now))
            elif selection.scope == "week":
                facts.append(self.describe_week(now.date()))
            else:
                target = self.resolve_day(selection.day, now)
                if selection.scope == "classes":
                    classes = self.courses_on(target)
                    lessons = "；".join(f"{item.interval} {item.activity} | {item.location}" for item in classes) or "无课"
                    facts.append(f"{target.isoformat()} {WEEKDAYS[target.weekday()]} 课程：{lessons}")
                else:
                    facts.append(self.describe_day(target))
        return (
            f"{CONTEXT_HEADER}\n"
            f"北京时间：{now:%Y-%m-%d %H:%M} {WEEKDAYS[now.weekday()]}\n"
            f"以下是你自己作为{self.identity}的角色日程，不是用户行踪。按这些安排自然回复，无关时不主动提起。\n"
            + LIFE_EXPERIENCE_RULES
            + "其他时段的既定安排需要时可查询 get_real_life_schedule。\n"
            + "\n".join(facts)
            + "\n</real_life_schedule>"
        )

    def describe(self, now: datetime) -> str:
        now = now.replace(tzinfo=BEIJING_TIME) if now.tzinfo is None else now.astimezone(BEIJING_TIME)
        return f"{self.describe_current(now)}\n\n{self.describe_day(now.date())}"

    def query(self, requested: str, now: datetime) -> str:
        now = now.replace(tzinfo=BEIJING_TIME) if now.tzinfo is None else now.astimezone(BEIJING_TIME)
        requested = requested.strip() or "今天"
        if requested in ("本周", "一周", "课表"):
            return self.describe_week(now.date())
        offsets = {"昨天": -1, "今天": 0, "明天": 1, "后天": 2}
        if requested in offsets:
            target = now.date() + timedelta(days=offsets[requested])
        else:
            requested = requested.replace("星期", "周").replace("周天", "周日")
            if requested in WEEKDAYS:
                target = now.date() + timedelta(days=WEEKDAYS.index(requested) - now.weekday())
            else:
                try:
                    target = date.fromisoformat(requested)
                except ValueError:
                    return "日期请使用今天、明天、后天、昨天、周一至周日、本周或 YYYY-MM-DD。"
        return self.describe_day(target)

    def build_context(self, now: datetime) -> str:
        return (
            f"{CONTEXT_HEADER}\n"
            "这是你自己的角色日程，不是用户行踪。无关时不主动提起，照常回复。\n"
            + LIFE_EXPERIENCE_RULES
            + "回答其他时段的既定课表或安排前，先调用 get_real_life_schedule，以查询结果为准；"
            "查不到的固定安排不要擅自改写，仍可在既有设定内补充合理的虚拟日常。\n"
            + f"{self.describe_current(now)}\n"
            + "</real_life_schedule>"
        )
