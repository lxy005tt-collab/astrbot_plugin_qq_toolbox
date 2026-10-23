"""Daily and one-off scheduled posts; durable per-task completion state."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import time

from .interval_publisher import BEIJING
from .qzone_client import QzoneError


def parse_when(value: str):
    value = value.strip()
    match = re.fullmatch(r"(?:每天\s*)?(\d{1,2}):(\d{2})", value)
    if match:
        hour, minute = map(int, match.groups())
        if hour > 23 or minute > 59:
            raise QzoneError("定点时间须在 00:00～23:59 之间。")
        return "daily", (hour, minute)
    try:
        moment = datetime.strptime(value.replace("T", " "), "%Y-%m-%d %H:%M").replace(tzinfo=BEIJING)
        return "once", moment
    except ValueError as exc:
        raise QzoneError("时间格式：每天21:00，或 2026-10-24T21:00（北京时间）。") from exc


def next_due(when: str, now: float) -> float | None:
    kind, value = parse_when(when)
    if kind == "once":
        return value.timestamp() if value.timestamp() > now else None
    current = datetime.fromtimestamp(now, BEIJING)
    moment = current.replace(hour=value[0], minute=value[1], second=0, microsecond=0)
    if moment.timestamp() <= now:
        moment += timedelta(days=1)
    return moment.timestamp()


class FixedPublisher:
    def __init__(self, path: Path, tasks: list[dict], callback, logger, *, clock=time.time):
        self.path, self.callback, self.logger, self.clock = path, callback, logger, clock
        self.tasks = {}
        for task in tasks:
            if not isinstance(task, dict) or not task.get("enabled", True):
                continue
            if not str(task.get("instruction", "")).strip():
                raise QzoneError("定点任务需要填写主题、创作要求或原文。")
            parse_when(str(task.get("when", "")))
            identity = str(task.get("id") or hashlib.sha256(json.dumps(task, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:10])
            if identity in self.tasks:
                raise QzoneError("存在重复的定点任务，请删除重复项。")
            self.tasks[identity] = dict(task)
        try:
            self.state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(self.state, dict):
                raise ValueError("invalid state")
        except (OSError, ValueError) as exc:
            raise QzoneError("定点任务状态文件无法读取，请检查插件数据，避免重复发布。") from exc

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def prepare(self):
        now = self.clock()
        self.state = {key: value for key, value in self.state.items() if key in self.tasks}
        for key, task in self.tasks.items():
            signature = hashlib.sha256(json.dumps(task, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            entry = self.state.get(key, {})
            if entry.get("signature") != signature:
                due = next_due(task["when"], now)
                entry = {"signature": signature, "next_run": due, "status": "waiting" if due else "expired"}
            elif entry.get("next_run") is not None and entry["next_run"] <= now:
                due = next_due(task["when"], now)
                entry.update(next_run=due, status="waiting" if due else "missed")
            self.state[key] = entry
        self._save()

    def snapshot(self):
        result = []
        for key, task in self.tasks.items():
            entry = dict(self.state.get(key, {}))
            entry.update(id=key, name=task.get("name") or task["when"], when=task["when"], instruction=task["instruction"], mode=task.get("mode", "按要求创作"))
            if entry.get("next_run"):
                entry["next_run_beijing"] = datetime.fromtimestamp(entry["next_run"], BEIJING).strftime("%Y-%m-%d %H:%M:%S")
            result.append(entry)
        return result

    async def fire(self, key: str):
        task, entry = self.tasks[key], self.state[key]
        slot = "fixed:" + key + ":" + str(int(entry["next_run"] * 1000))
        entry.update(status="running", last_started=self.clock())
        self._save()
        try:
            result = await self.callback(slot, task)
            entry.update(last_status="success", last_result=result)
            self.logger.info("[QzoneHelper] 定点任务 %s 已完成。", key)
        except asyncio.CancelledError:
            entry.update(last_status="interrupted", last_result="任务被中断，请先核对空间。")
            self._save()
            raise
        except QzoneError as exc:
            entry.update(last_status="failed", last_result=str(exc))
            self.logger.warning("[QzoneHelper] 定点任务 %s 停止：%s", key, exc)
        except Exception as exc:
            entry.update(last_status="error", last_result=f"任务异常：{type(exc).__name__}，请核对空间。")
            self.logger.error("[QzoneHelper] 定点任务 %s 异常：%s", key, type(exc).__name__)
        entry["last_finished"] = self.clock()
        entry["next_run"] = next_due(task["when"], self.clock()) if parse_when(task["when"])[0] == "daily" else None
        entry["status"] = "waiting" if entry["next_run"] else "completed"
        self._save()

    async def run(self):
        while True:
            pending = [(entry["next_run"], key) for key, entry in self.state.items() if entry.get("next_run") is not None]
            if not pending:
                await asyncio.sleep(60)
                continue
            due, key = min(pending)
            remaining = due - self.clock()
            if remaining > 0:
                await asyncio.sleep(min(remaining, 60))
                continue
            await self.fire(key)
