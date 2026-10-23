"""Dashboard page API for the observatory plugin.

Routes are registered under ``/astrbot_plugin_observatory/...`` and called by
the View through ``window.AstrBotPluginView.apiGet/apiPost``. Every handler
requires the native dashboard login (``request.username``); there is no second
login system and no additional unauthenticated port.

All text is returned as JSON strings. The frontend renders it with textContent,
never by injecting HTML.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import time
from typing import Any

from astrbot.api.web import (
    error_response,
    json_response,
    request,
    stream_response,
)

from . import __version__
from . import compat, queries
from .util import clip, json_loads, recompute_sections

PLUGIN_NAME = "astrbot_plugin_observatory"

BOOL_SETTINGS = {
    "capture_enabled",
    "record_incoming_messages",
    "record_outgoing_messages",
}
INT_SETTINGS = {
    "retention_chat_days": (1, 3650),
    "retention_reasoning_days": (1, 3650),
    "retention_snapshot_days": (1, 3650),
    "retention_usage_days": (1, 3650),
    "refresh_seconds": (0, 300),
    "max_message_chars": (500, 200000),
}
FLOAT_SETTINGS = {"data_budget_gb": (0.1, 200.0)}


def _ok(data: Any):
    return json_response({"status": "ok", "data": data})


def _authed() -> bool:
    try:
        return bool((request.username or "").strip())
    except Exception:
        return False


def _csv_safe(value: Any) -> Any:
    """Neutralize spreadsheet formula injection in exported chat text."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def _int(name: str, default: int, low: int, high: int) -> int:
    value = request.query.get(name, default, type=int)
    if value is None:
        value = default
    return max(low, min(int(value), high))


def _float(name: str, default: float) -> float:
    value = request.query.get(name, default, type=float)
    if value is None:
        value = default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(name: str, default: str = "", limit: int = 200) -> str:
    value = request.query.get(name, default)
    return clip(value if isinstance(value, str) else default, limit)


def _range(default_hours: float = 24.0, max_days: float = 366.0) -> tuple[float, float]:
    now = time.time()
    from_ts = _float("from", now - default_hours * 3600)
    to_ts = _float("to", now)
    if from_ts >= to_ts:
        from_ts, to_ts = to_ts - default_hours * 3600, to_ts
    if to_ts - from_ts > max_days * 86400:
        from_ts = to_ts - max_days * 86400
    return from_ts, to_ts


class PageApi:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    @property
    def storage(self):
        return self.plugin.storage

    @property
    def collector(self):
        return self.plugin.collector

    @property
    def tz(self) -> str:
        return self.plugin.get_timezone()

    # ------------------------------------------------------------- register
    def register(self) -> None:
        register = self.plugin.context.register_web_api
        routes = [
            ("overview", self.overview, ["GET"]),
            ("sessions", self.sessions, ["GET"]),
            ("sessions/<scope_id>", self.session_detail, ["GET"]),
            ("sessions/<scope_id>/messages", self.session_messages, ["GET"]),
            ("sessions/<scope_id>/context", self.session_context, ["GET"]),
            ("traces", self.traces, ["GET"]),
            ("traces/<trace_id>", self.trace_detail, ["GET"]),
            ("requests", self.requests, ["GET"]),
            ("requests/<request_id>", self.request_detail, ["GET"]),
            ("requests/<request_id>/content", self.request_content, ["GET"]),
            ("usage", self.usage, ["GET"]),
            ("costs", self.costs, ["GET"]),
            ("prices", self.prices, ["GET"]),
            ("prices/save", self.prices_save, ["POST"]),
            ("settings", self.settings, ["GET"]),
            ("settings/save", self.settings_save, ["POST"]),
            ("health", self.health, ["GET"]),
            ("export", self.export, ["GET"]),
        ]
        for path, handler, methods in routes:
            register(
                f"/{PLUGIN_NAME}/{path}",
                handler,
                methods,
                f"Bot 观察台 {path}",
            )

    # -------------------------------------------------------------- handlers
    async def overview(self):
        if not _authed():
            return error_response("需要后台登录", status_code=401)
        from_ts, to_ts = _range()
        data = await queries.overview(self.storage, from_ts, to_ts, self.tz)
        data["health"] = await self._health_brief()
        return _ok(data)

    async def sessions(self):
        if not _authed():
            return error_response("需要后台登录", status_code=401)
        from_ts, to_ts = _range(default_hours=7 * 24)
        q = _text("q", "", 200)
        scope_type = _text("type", "all", 20)
        if scope_type not in ("all", "group", "private"):
            scope_type = "all"
        sort = _text("sort", "recent", 20)
        if sort not in ("recent", "tokens", "requests"):
            sort = "recent"
        items = await queries.sessions(
            self.storage,
            q=q,
            scope_type=scope_type,
            sort=sort,
            from_ts=from_ts,
            to_ts=to_ts,
        )
        cursor = _int("cursor", 0, 0, 1_000_000)
        limit = _int("limit", 100, 1, 200)
        page = items[cursor : cursor + limit]
        next_cursor = cursor + limit if cursor + limit < len(items) else None
        return _ok(
            {
                "sessions": page,
                "total": len(items),
                "next_cursor": next_cursor,
                "range": {"from": from_ts, "to": to_ts},
            }
        )

    async def session_detail(self, scope_id: str):
        if not _authed():
            return error_response("需要后台登录", status_code=401)
        data = await queries.session_detail(self.storage, scope_id)
        if data is None:
            return error_response("会话不存在", status_code=404)
        return _ok(data)

    async def session_messages(self, scope_id: str):
        if not _authed():
            return error_response("需要后台登录", status_code=401)
        before_ts = request.query.get("before_ts", None, type=float)
        before_id = request.query.get("before_id", None, type=int)
        limit = _int("limit", 50, 1, 200)
        data = await queries.session_messages(
            self.storage,
            scope_id,
            before_ts=before_ts,
            before_id=before_id,
            limit=limit,
        )
        return _ok(data)

    async def session_context(self, scope_id: str):
        if not _authed():
            return error_response("需要后台登录", status_code=401)
        data = await queries.session_context(
            self.storage,
            scope_id,
            budget_resolver=self.plugin.resolve_budget,
        )
        return _ok(data)

    async def traces(self):
        if not _authed():
            return error_response("需要后台登录", status_code=401)
        from_ts, to_ts = _range(default_hours=7 * 24)
        data = await queries.traces(
            self.storage,
            from_ts=from_ts,
            to_ts=to_ts,
            scope_id=_text("scope_id", "", 80) or None,
            status=_text("status", "", 30) or None,
            limit=_int("limit", 50, 1, 200),
            before=request.query.get("before", None, type=float),
        )
        return _ok(data)

    async def trace_detail(self, trace_id: str):
        if not _authed():
            return error_response("需要后台登录", status_code=401)
        data = await queries.trace_detail(self.storage, trace_id)
        if data is None:
            return error_response("回复轮次不存在", status_code=404)
        return _ok(data)

    async def requests(self):
        if not _authed():
            return error_response("需要后台登录", status_code=401)
        from_ts, to_ts = _range(default_hours=7 * 24)
        data = await queries.requests_list(
            self.storage,
            from_ts=from_ts,
            to_ts=to_ts,
            scope_id=_text("scope_id", "", 80) or None,
            status=_text("status", "", 30) or None,
            provider_id=_text("provider_id", "", 120) or None,
            model=_text("model", "", 200) or None,
            reasoning=_text("reasoning", "", 30) or None,
            cost_status=_text("cost_status", "", 40) or None,
            limit=_int("limit", 50, 1, 200),
            before=request.query.get("before", None, type=float),
        )
        return _ok(data)

    async def request_detail(self, request_id: str):
        if not _authed():
            return error_response("需要后台登录", status_code=401)
        data = await queries.request_detail(self.storage, request_id)
        if data is None:
            return error_response("请求不存在", status_code=404)
        data["result"] = data.get("result") or {}
        return _ok(data)

    async def request_content(self, request_id: str):
        if not _authed():
            return error_response("需要后台登录", status_code=401)
        kind = _text("kind", "input", 20)
        if kind not in ("input", "reply", "reasoning", "json", "tools"):
            return error_response("不支持的正文类型", status_code=400)
        row = await self.storage.fetch_one(
            "SELECT r.*, res.reply_blob_id, res.reply_state, res.reasoning_blob_id, "
            "res.reasoning_state, res.reasoning_status, res.reply_chars, "
            "res.reasoning_chars, res.tool_calls_json, res.role "
            "FROM llm_request r LEFT JOIN llm_result res ON res.request_id=r.request_id "
            "WHERE r.request_id=?",
            (request_id,),
        )
        if row is None:
            return error_response("请求不存在", status_code=404)
        if kind == "tools":
            return _ok(await self._tool_trace(row))
        if kind == "json":
            text = await self.storage.read_blob_text(row.get("input_blob_id"))
            if text is None:
                return _ok(
                    {
                        "kind": kind,
                        "state": row.get("input_blob_state") or "missing",
                        "snapshot": None,
                        "note": "请求快照正文已过期或未保存；统计与元数据仍保留。",
                    }
                )
            snapshot = json_loads(text, {})
            return _ok(
                {
                    "kind": kind,
                    "state": "available",
                    "snapshot": snapshot,
                    "request_id": request_id,
                }
            )
        if kind == "input":
            text = await self.storage.read_blob_text(row.get("input_blob_id"))
            if text is None:
                return _ok(
                    {
                        "kind": kind,
                        "state": row.get("input_blob_state") or "missing",
                        "messages": [],
                        "sections": json_loads(row.get("input_sections_json"), []),
                        "next_cursor": None,
                        "note": "请求快照正文已过期或未保存；统计与元数据仍保留。",
                    }
                )
            snapshot = json_loads(text, {})
            messages = snapshot.get("messages") or []
            messages, sections = recompute_sections(messages)
            if snapshot.get("tool_schema_est_tokens"):
                sections = list(sections) + [
                    {
                        "key": "tool_schema",
                        "label": "工具 schema",
                        "message_indices": [],
                        "tokens_estimate": snapshot.get("tool_schema_est_tokens"),
                        "tool_count": len(snapshot.get("tool_names") or []),
                    }
                ]
            cursor = _int("cursor", 0, 0, 1_000_000)
            limit = _int("limit", 20, 1, 100)
            page = messages[cursor : cursor + limit]
            next_cursor = cursor + limit if cursor + limit < len(messages) else None
            return _ok(
                {
                    "kind": kind,
                    "state": "available",
                    "messages": page,
                    "sections": sections or snapshot.get("sections") or [],
                    "tool_schema": snapshot.get("tool_schema") or [],
                    "capture_stage": snapshot.get("capture_stage"),
                    "capture_stage_note": snapshot.get("capture_stage_note"),
                    "total_messages": len(messages),
                    "next_cursor": next_cursor,
                    "input_est_tokens": snapshot.get("total_est_tokens"),
                    "dropped_modality_parts": snapshot.get("dropped_modality_parts"),
                }
            )
        if kind == "reply":
            blob_id = row.get("reply_blob_id")
            state = row.get("reply_state") or "missing"
        else:
            blob_id = row.get("reasoning_blob_id")
            state = row.get("reasoning_state") or "missing"
        text = await self.storage.read_blob_text(blob_id)
        if text is None:
            note = (
                "公开思考正文已过期"
                if kind == "reasoning"
                else "回复正文已过期或未保存"
            )
            return _ok(
                {
                    "kind": kind,
                    "state": "expired" if blob_id else state,
                    "status": row.get("reasoning_status") if kind == "reasoning" else None,
                    "text": "",
                    "note": note,
                }
            )
        cursor = _int("cursor", 0, 0, 10_000_000)
        limit = _int("limit", 20000, 1000, 60000)
        chunk = text[cursor : cursor + limit]
        return _ok(
            {
                "kind": kind,
                "state": "available",
                "status": row.get("reasoning_status") if kind == "reasoning" else None,
                "text": chunk,
                "cursor": cursor,
                "next_cursor": cursor + limit if cursor + limit < len(text) else None,
                "total_chars": len(text),
                "note": (
                    "上游实际返回的可读思考文本；不包含加密 signature。"
                    if kind == "reasoning"
                    else "模型最终回复文本；平台是否成功发送请以发送记录为准。"
                ),
            }
        )

    async def _tool_trace(self, row: dict) -> dict:
        trace_id = row.get("trace_id")
        requests = await self.storage.fetch_all(
            "SELECT r.request_id, r.step_no, r.attempt_no, r.provider_model, "
            "r.started_at, r.status, res.tool_calls_json, res.reply_preview "
            "FROM llm_request r LEFT JOIN llm_result res ON res.request_id=r.request_id "
            "WHERE r.trace_id=? ORDER BY r.started_at ASC",
            (trace_id,),
        )
        steps = []
        for index, item in enumerate(requests):
            calls = json_loads(item.get("tool_calls_json"), [])
            results = []
            if index + 1 < len(requests):
                next_row = await self.storage.fetch_one(
                    "SELECT input_blob_id FROM llm_request WHERE request_id=?",
                    (requests[index + 1]["request_id"],),
                )
                snapshot_text = (
                    await self.storage.read_blob_text(next_row.get("input_blob_id"))
                    if next_row
                    else None
                )
                if snapshot_text:
                    snapshot = json_loads(snapshot_text, {})
                    for message in snapshot.get("messages") or []:
                        if message.get("group") != "tool_result":
                            continue
                        text_parts = [
                            part.get("text", "")
                            for part in message.get("parts") or []
                            if part.get("type") == "text"
                        ]
                        results.append(
                            {
                                "tool_call_id": message.get("tool_call_id"),
                                "text": clip("\n".join(text_parts), 8000),
                            }
                        )
            steps.append(
                {
                    "request_id": item["request_id"],
                    "step_no": item["step_no"],
                    "attempt_no": item["attempt_no"],
                    "provider_model": item["provider_model"],
                    "started_at": item["started_at"],
                    "status": item["status"],
                    "tool_calls": calls,
                    "tool_results": results,
                    "reply_preview": item.get("reply_preview"),
                }
            )
        return {
            "kind": "tools",
            "trace_id": trace_id,
            "steps": steps,
            "note": "工具结果来自下一次请求的实际输入快照；未发生下一次请求时无法确认结果。",
        }

    async def usage(self):
        if not _authed():
            return error_response("需要后台登录", status_code=401)
        from_ts, to_ts = _range(default_hours=7 * 24)
        group_by = _text("group_by", "day", 20)
        if group_by not in ("day", "provider", "model", "scope"):
            group_by = "day"
        data = await queries.usage(
            self.storage,
            from_ts=from_ts,
            to_ts=to_ts,
            group_by=group_by,
            scope_id=_text("scope_id", "", 80) or None,
            provider_id=_text("provider_id", "", 120) or None,
            model=_text("model", "", 200) or None,
            tz=self.tz,
        )
        return _ok(data)

    async def costs(self):
        if not _authed():
            return error_response("需要后台登录", status_code=401)
        from_ts, to_ts = _range(default_hours=7 * 24)
        data = await queries.costs(
            self.storage,
            from_ts=from_ts,
            to_ts=to_ts,
            scope_id=_text("scope_id", "", 80) or None,
            provider_id=_text("provider_id", "", 120) or None,
            model=_text("model", "", 200) or None,
            limit=_int("limit", 200, 1, 1000),
        )
        return _ok(data)

    async def prices(self):
        if not _authed():
            return error_response("需要后台登录", status_code=401)
        rows = await asyncio.to_thread(self.plugin.list_prices)
        return _ok(
            {
                "prices": rows,
                "currencies": sorted(
                    {row["currency"] for row in rows if row.get("currency")}
                ),
                "note": (
                    "单价只影响新请求的估算；历史估算冻结当时的价格版本。"
                    "未配置单价不是免费。金额为按 token 估算，不是供应商账单。"
                ),
            }
        )

    async def prices_save(self):
        if not _authed():
            return error_response("需要后台登录", status_code=401)
        payload = await request.json(default={})
        try:
            row = await asyncio.to_thread(self.plugin.save_price, payload)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        rows = await asyncio.to_thread(self.plugin.list_prices)
        return _ok({"saved": row, "prices": rows})

    async def settings(self):
        if not _authed():
            return error_response("需要后台登录", status_code=401)
        return _ok(
            {
                "settings": self.storage.get_settings(),
                "defaults": self.storage.defaults,
                "editable": sorted(
                    list(BOOL_SETTINGS) + list(INT_SETTINGS) + list(FLOAT_SETTINGS)
                ),
                "note": "只保存观察台自身设置，不修改主模型、压缩门槛或其他插件配置。",
            }
        )

    async def settings_save(self):
        if not _authed():
            return error_response("需要后台登录", status_code=401)
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是对象", status_code=400)
        cleaned: dict[str, Any] = {}
        errors: list[str] = []
        for key, value in payload.items():
            if key in BOOL_SETTINGS:
                if not isinstance(value, bool):
                    errors.append(f"{key} 必须是布尔值")
                else:
                    cleaned[key] = value
            elif key in INT_SETTINGS:
                low, high = INT_SETTINGS[key]
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    errors.append(f"{key} 必须是整数")
                    continue
                if number < low or number > high:
                    errors.append(f"{key} 必须在 {low}..{high} 之间")
                    continue
                cleaned[key] = number
            elif key in FLOAT_SETTINGS:
                low, high = FLOAT_SETTINGS[key]
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    errors.append(f"{key} 必须是数字")
                    continue
                if number < low or number > high:
                    errors.append(f"{key} 必须在 {low}..{high} 之间")
                    continue
                cleaned[key] = round(number, 2)
            else:
                errors.append(f"未知设置项：{key}")
        if errors:
            return error_response("；".join(errors), status_code=400)
        saved = self.storage.save_settings(cleaned)
        self.plugin.plugin_config.update(cleaned)
        self.plugin.plugin_config.save_config()
        self.collector.update_settings(saved)
        return _ok({"settings": saved})

    async def health(self):
        if not _authed():
            return error_response("需要后台登录", status_code=401)
        return _ok(await self._health_full())

    async def export(self):
        if not _authed():
            return error_response("需要后台登录", status_code=401)
        kind = _text("kind", "requests", 20)
        if kind not in ("requests", "traces", "messages", "costs"):
            return error_response("不支持的导出类型", status_code=400)
        fmt = _text("format", "json", 10)
        if fmt not in ("json", "csv"):
            return error_response("format 只能是 json 或 csv", status_code=400)
        from_ts, to_ts = _range(default_hours=7 * 24)
        rows = await queries.export_rows(
            self.storage,
            kind,
            from_ts=from_ts,
            to_ts=to_ts,
            scope_id=_text("scope_id", "", 80) or None,
            limit=_int("limit", 5000, 1, 5000),
        )
        stamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"observatory_{kind}_{stamp}.{fmt}"
        if fmt == "json":
            return json_response(
                {"status": "ok", "data": rows, "scope": kind},
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        buffer = io.StringIO()
        buffer.write("\ufeff")
        if rows:
            fieldnames = list(rows[0].keys())
            writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        key: _csv_safe(
                            json.dumps(value, ensure_ascii=False)
                            if isinstance(value, (dict, list))
                            else value
                        )
                        for key, value in row.items()
                    }
                )
        return stream_response(
            iter([buffer.getvalue()]),
            content_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ----------------------------------------------------------------- health
    async def _health_brief(self) -> dict:
        stats = await asyncio.to_thread(self.storage.disk_stats)
        metrics = self.storage.get_metrics()
        return {
            "capture": compat.active_state(),
            "queue": self.collector._trace_stats(),
            "body_bytes": stats.get("body_bytes"),
            "last_write_at": stats.get("last_write_at"),
            "dropped_total": int(metrics.get("queue_dropped") or 0),
        }

    async def _health_full(self) -> dict:
        stats = await asyncio.to_thread(self.storage.disk_stats)
        counts = await queries.health_counts(self.storage)
        coverage = await queries._coverage(self.storage)
        gaps = await self.storage.fetch_all(
            "SELECT ts, kind, detail FROM capture_gap ORDER BY id DESC LIMIT 20"
        )
        settings = self.storage.get_settings()
        return {
            "plugin_version": __version__,
            "timezone": self.tz,
            "data_dir": str(self.plugin.data_dir),
            "capture": compat.active_state(),
            "collector": self.collector._trace_stats(),
            "storage": stats,
            "counts": counts,
            "coverage": coverage,
            "recent_gaps": gaps,
            "settings": settings,
            "metrics": self.storage.get_metrics(),
            "notes": [
                "采集点：压缩后、provider 序列化前；provider 适配器内部重写不可见。",
                "只统计主聊天 ToolLoopAgentRunner 的直接请求；辅助模型调用不计入。",
                "正文过期后仍保留统计与“已过期”状态。",
                "观察台不删除任何原生 AstrBot 数据。",
            ],
        }
