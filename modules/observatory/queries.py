"""Read-only aggregate queries used by the dashboard page API.

All values are returned raw (UTC epoch / integers / Decimal-as-string). The
frontend formats them in Asia/Shanghai. Nothing here mutates AstrBot data.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .util import clip, json_loads, normalize_text, parse_ts, recompute_sections

TZ = ZoneInfo("Asia/Shanghai")


def tzinfo(tz_name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or "Asia/Shanghai")
    except Exception:
        return TZ


def local_day_start(ts: float, tz: str = "Asia/Shanghai") -> float:
    dt = datetime.fromtimestamp(float(ts), tzinfo(tz))
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def local_hour_start(ts: float, tz: str = "Asia/Shanghai") -> float:
    dt = datetime.fromtimestamp(float(ts), tzinfo(tz))
    return dt.replace(minute=0, second=0, microsecond=0).timestamp()


def day_bounds(now: float, tz: str = "Asia/Shanghai") -> tuple[float, float]:
    start = local_day_start(now, tz)
    dt = datetime.fromtimestamp(start, tzinfo(tz)) + timedelta(days=1)
    return start, dt.timestamp()


def scope_display(row: dict) -> str:
    alias = (row.get("alias") or "").strip()
    name = (row.get("display_name") or "").strip()
    peer = (row.get("peer_id") or "").strip()
    return alias or name or peer or "未知会话"


def _usage_from_row(row: dict) -> dict:
    return {
        "input_other": row.get("usage_input_other"),
        "input_cached": row.get("usage_input_cached"),
        "output": row.get("usage_output"),
        "source": row.get("usage_source") or "unknown",
    }


async def scopes_for_filter(storage: Any) -> list[dict]:
    return await storage.fetch_all(
        "SELECT * FROM chat_scope ORDER BY COALESCE(last_seen,0) DESC LIMIT 2000"
    )


async def overview(
    storage: Any, from_ts: float, to_ts: float, tz: str = "Asia/Shanghai"
) -> dict:
    traces = await storage.fetch_all(
        "SELECT t.*, s.is_group, s.display_name, s.alias, s.peer_id "
        "FROM reply_trace t LEFT JOIN chat_scope s ON s.scope_id=t.scope_id "
        "WHERE t.started_at>=? AND t.started_at<? ORDER BY t.started_at ASC",
        (from_ts, to_ts),
    )
    requests = await storage.fetch_all(
        "SELECT r.*, s.is_group, s.display_name, s.alias, s.peer_id "
        "FROM llm_request r LEFT JOIN chat_scope s ON s.scope_id=r.scope_id "
        "WHERE r.started_at>=? AND r.started_at<? ORDER BY r.started_at ASC",
        (from_ts, to_ts),
    )
    results = await storage.fetch_all(
        "SELECT res.* FROM llm_result res JOIN llm_request r ON r.request_id=res.request_id "
        "WHERE r.started_at>=? AND r.started_at<?",
        (from_ts, to_ts),
    )
    costs = await storage.fetch_all(
        "SELECT c.* FROM cost_estimate c JOIN llm_request r ON r.request_id=c.request_id "
        "WHERE r.started_at>=? AND r.started_at<?",
        (from_ts, to_ts),
    )
    native = await storage.fetch_all(
        "SELECT * FROM native_reply_summary WHERE start_time>=? AND start_time<?",
        (from_ts, to_ts),
    )

    result_by_request = {row["request_id"]: row for row in results}
    success = sum(1 for row in requests if row["status"] == "completed")
    failed = sum(
        1 for row in requests if row["status"] in ("error", "cancelled", "incomplete")
    )
    running = sum(1 for row in requests if row["status"] == "running")
    usage_rows = [
        result_by_request[row["request_id"]]
        for row in requests
        if row["request_id"] in result_by_request
        and (result_by_request[row["request_id"]].get("usage_source") == "api_reported")
    ]
    tokens = {
        "input_other": sum(int(r.get("usage_input_other") or 0) for r in usage_rows),
        "input_cached": sum(int(r.get("usage_input_cached") or 0) for r in usage_rows),
        "output": sum(int(r.get("usage_output") or 0) for r in usage_rows),
        "reported_requests": len(usage_rows),
        "unknown_requests": len(requests) - len(usage_rows),
    }
    cost_by_currency: dict[str, str] = {}
    cost_status_counts: dict[str, int] = {}
    for row in costs:
        status = row.get("status") or "unknown"
        cost_status_counts[status] = cost_status_counts.get(status, 0) + 1
        if status == "estimated" and row.get("amount") and row.get("currency"):
            currency = row["currency"]
            cost_by_currency[currency] = cost_by_currency.get(currency) or "0"
    # Sum Decimal strings per currency in Python to avoid float money.
    from decimal import Decimal

    sums: dict[str, Decimal] = {}
    for row in costs:
        if row.get("status") == "estimated" and row.get("amount") and row.get("currency"):
            sums[row["currency"]] = sums.get(row["currency"], Decimal(0)) + Decimal(
                str(row["amount"])
            )
    cost_by_currency = {cur: format(value, "f") for cur, value in sums.items()}

    scope_activity: dict[str, dict] = {}
    for row in requests:
        item = scope_activity.setdefault(
            row["scope_id"],
            {
                "scope_id": row["scope_id"],
                "display": scope_display(row),
                "is_group": bool(row.get("is_group")),
                "requests": 0,
                "tokens": 0,
                "started_at": row["started_at"],
            },
        )
        item["requests"] += 1
        result = result_by_request.get(row["request_id"])
        if result and result.get("usage_source") == "api_reported":
            item["tokens"] += int(result.get("usage_input_other") or 0) + int(
                result.get("usage_input_cached") or 0
            ) + int(result.get("usage_output") or 0)

    groups = {sid for sid, item in scope_activity.items() if item["is_group"]}
    privates = {sid for sid, item in scope_activity.items() if not item["is_group"]}
    latencies = sorted(
        int(row["latency_ms"])
        for row in results
        if row.get("latency_ms") is not None
    )
    ttfts = sorted(
        int(row["ttft_ms"]) for row in results if row.get("ttft_ms") is not None
    )
    latency = {
        "avg_ms": int(sum(latencies) / len(latencies)) if latencies else None,
        "p50_ms": latencies[len(latencies) // 2] if latencies else None,
        "p95_ms": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
        if latencies
        else None,
        "ttft_avg_ms": int(sum(ttfts) / len(ttfts)) if ttfts else None,
        "sample": len(latencies),
    }

    # Trend buckets: hourly for windows up to 2 days, daily otherwise.
    span = max(1.0, to_ts - from_ts)
    granularity = "hour" if span <= 2 * 86400 + 3600 else "day"
    bucket_fn = local_hour_start if granularity == "hour" else local_day_start
    buckets: dict[float, dict] = {}
    cursor = bucket_fn(from_ts, tz)
    while cursor < to_ts:
        buckets[cursor] = {
            "bucket_start": cursor,
            "requests": 0,
            "reply_rounds": 0,
            "input_other": 0,
            "input_cached": 0,
            "output": 0,
            "failures": 0,
        }
        step = 3600 if granularity == "hour" else 86400
        cursor += step
    for row in requests:
        key = bucket_fn(row["started_at"], tz)
        bucket = buckets.get(key)
        if bucket is None:
            continue
        bucket["requests"] += 1
        if row["status"] in ("error", "cancelled", "incomplete"):
            bucket["failures"] += 1
        result = result_by_request.get(row["request_id"])
        if result and result.get("usage_source") == "api_reported":
            bucket["input_other"] += int(result.get("usage_input_other") or 0)
            bucket["input_cached"] += int(result.get("usage_input_cached") or 0)
            bucket["output"] += int(result.get("usage_output") or 0)
    for row in traces:
        key = bucket_fn(row["started_at"], tz)
        if key in buckets:
            buckets[key]["reply_rounds"] += 1

    top_sessions = sorted(
        scope_activity.values(), key=lambda item: item["requests"], reverse=True
    )[:8]

    native_tokens = {
        "input_other": sum(int(r.get("input_other") or 0) for r in native),
        "input_cached": sum(int(r.get("input_cached") or 0) for r in native),
        "output": sum(int(r.get("output") or 0) for r in native),
        "reply_rounds": len(native),
    }
    coverage = await _coverage(storage)
    return {
        "range": {"from": from_ts, "to": to_ts, "timezone": tz, "granularity": granularity},
        "scope_definition": "仅主聊天链路：ToolLoopAgentRunner 发起的直接模型请求",
        "cards": {
            "reply_rounds": len(traces),
            "requests": {
                "total": len(requests),
                "success": success,
                "failed": failed,
                "running": running,
            },
            "tokens": tokens,
            "cost": {
                "by_currency": cost_by_currency,
                "status_counts": cost_status_counts,
                "estimated_requests": sum(
                    1 for row in costs if row.get("status") == "estimated"
                ),
            },
            "active_scopes": {
                "groups": len(groups),
                "privates": len(privates),
            },
            "latency": latency,
        },
        "trend": [buckets[key] for key in sorted(buckets)],
        "top_sessions": top_sessions,
        "native_import": {
            "reply_rounds": native_tokens["reply_rounds"],
            "tokens": native_tokens,
            "note": "原生 provider_stats 汇总，单独显示；不与直接请求相加。",
        },
        "coverage": coverage,
    }


async def _coverage(storage: Any) -> dict:
    row = await storage.fetch_one(
        "SELECT coverage_json, updated_at FROM import_checkpoint WHERE name='native_coverage'"
    )
    gaps = await storage.fetch_all(
        "SELECT ts, kind, detail FROM capture_gap ORDER BY id DESC LIMIT 10"
    )
    counts = await storage.fetch_one(
        "SELECT "
        "(SELECT COUNT(*) FROM chat_message) messages, "
        "(SELECT COUNT(*) FROM reply_trace) traces, "
        "(SELECT COUNT(*) FROM llm_request) requests, "
        "(SELECT COUNT(*) FROM llm_request WHERE status='running') running_requests, "
        "(SELECT COUNT(*) FROM native_reply_summary) native_summaries"
    )
    return {
        "live_started_at": parse_ts(storage.get_meta("live_started_at")),
        "installed_at": parse_ts(storage.get_meta("installed_at")),
        "native": json_loads(row["coverage_json"], {}) if row else {},
        "native_updated_at": row["updated_at"] if row else None,
        "counts": counts or {},
        "recent_gaps": gaps,
    }


async def sessions(
    storage: Any,
    *,
    q: str = "",
    scope_type: str = "all",
    sort: str = "recent",
    from_ts: float,
    to_ts: float,
) -> list[dict]:
    scopes = await storage.fetch_all("SELECT * FROM chat_scope")
    stats = await storage.fetch_all(
        "SELECT r.scope_id, COUNT(*) AS requests, MAX(r.started_at) AS last_request, "
        "SUM(CASE WHEN res.usage_source='api_reported' THEN res.usage_input_other ELSE 0 END) AS in_other, "
        "SUM(CASE WHEN res.usage_source='api_reported' THEN res.usage_input_cached ELSE 0 END) AS in_cached, "
        "SUM(CASE WHEN res.usage_source='api_reported' THEN res.usage_output ELSE 0 END) AS out_tokens, "
        "SUM(CASE WHEN r.status IN ('error','cancelled','incomplete') THEN 1 ELSE 0 END) AS failures, "
        "AVG(res.latency_ms) AS latency "
        "FROM llm_request r LEFT JOIN llm_result res ON res.request_id=r.request_id "
        "WHERE r.started_at>=? AND r.started_at<? GROUP BY r.scope_id",
        (from_ts, to_ts),
    )
    msg_stats = await storage.fetch_all(
        "SELECT scope_id, COUNT(*) AS messages, MAX(ts) AS last_message, "
        "MAX(CASE WHEN direction='out' THEN ts END) AS last_out "
        "FROM chat_message WHERE duplicate_of IS NULL GROUP BY scope_id"
    )
    trace_stats = await storage.fetch_all(
        "SELECT scope_id, COUNT(*) AS traces, MAX(started_at) AS last_trace, "
        "MAX(ended_at) AS last_trace_end FROM reply_trace GROUP BY scope_id"
    )
    costs = await storage.fetch_all(
        "SELECT r.scope_id, c.currency, SUM(CAST(c.amount AS REAL)) AS amount "
        "FROM cost_estimate c JOIN llm_request r ON r.request_id=c.request_id "
        "WHERE c.status='estimated' AND r.started_at>=? AND r.started_at<? "
        "GROUP BY r.scope_id, c.currency",
        (from_ts, to_ts),
    )
    stat_by_scope = {row["scope_id"]: row for row in stats}
    msg_by_scope = {row["scope_id"]: row for row in msg_stats}
    trace_by_scope = {row["scope_id"]: row for row in trace_stats}
    cost_by_scope: dict[str, dict[str, str]] = {}
    for row in costs:
        cost_by_scope.setdefault(row["scope_id"], {})[row["currency"]] = format(
            row["amount"] or 0, "f"
        )

    query = (q or "").strip().lower()
    items: list[dict] = []
    for scope in scopes:
        stat = stat_by_scope.get(scope["scope_id"], {})
        msg = msg_by_scope.get(scope["scope_id"], {})
        trace = trace_by_scope.get(scope["scope_id"], {})
        is_group = bool(scope["is_group"])
        if scope_type == "group" and not is_group:
            continue
        if scope_type == "private" and is_group:
            continue
        display = scope_display(scope)
        haystack = " ".join(
            str(scope.get(key) or "")
            for key in ("display_name", "alias", "peer_id", "umo", "platform_id")
        ).lower()
        if query and query not in haystack:
            continue
        last_activity = max(
            float(scope.get("last_seen") or 0),
            float(msg.get("last_message") or 0),
            float(trace.get("last_trace") or 0),
            float(stat.get("last_request") or 0),
        )
        if (
            last_activity <= 0
            and int(stat.get("requests") or 0) == 0
            and int(msg.get("messages") or 0) == 0
        ):
            continue
        items.append(
            {
                "scope_id": scope["scope_id"],
                "platform_id": scope["platform_id"],
                "message_type": scope["message_type"],
                "peer_id": scope["peer_id"],
                "umo": scope["umo"],
                "display": clip(display, 200),
                "is_group": is_group,
                "requests": int(stat.get("requests") or 0),
                "traces": int(trace.get("traces") or 0),
                "messages": int(msg.get("messages") or 0),
                "failures": int(stat.get("failures") or 0),
                "latency_ms": int(stat["latency"]) if stat.get("latency") else None,
                "tokens": {
                    "input_other": int(stat.get("in_other") or 0),
                    "input_cached": int(stat.get("in_cached") or 0),
                    "output": int(stat.get("out_tokens") or 0),
                },
                "cost_by_currency": cost_by_scope.get(scope["scope_id"], {}),
                "last_activity": last_activity,
                "last_message": parse_ts(msg.get("last_message")),
                "last_trace": parse_ts(trace.get("last_trace")),
                "native_only": int(stat.get("requests") or 0) == 0
                and int(msg.get("messages") or 0) > 0,
            }
        )
    if sort == "tokens":
        items.sort(
            key=lambda item: sum(item["tokens"].values()),
            reverse=True,
        )
    elif sort == "requests":
        items.sort(key=lambda item: item["requests"], reverse=True)
    else:
        items.sort(key=lambda item: item["last_activity"], reverse=True)
    return items


async def session_detail(storage: Any, scope_id: str) -> dict | None:
    scope = await storage.fetch_one(
        "SELECT * FROM chat_scope WHERE scope_id=?", (scope_id,)
    )
    if scope is None:
        return None
    return {
        "scope_id": scope["scope_id"],
        "platform_id": scope["platform_id"],
        "message_type": scope["message_type"],
        "peer_id": scope["peer_id"],
        "umo": scope["umo"],
        "display": scope_display(scope),
        "alias": scope.get("alias"),
        "is_group": bool(scope["is_group"]),
        "first_seen": parse_ts(scope.get("first_seen")),
        "last_seen": parse_ts(scope.get("last_seen")),
    }


async def session_messages(
    storage: Any,
    scope_id: str,
    *,
    before_ts: float | None = None,
    before_id: int | None = None,
    limit: int = 50,
) -> dict:
    limit = max(1, min(int(limit or 50), 200))
    where = ["scope_id=?", "duplicate_of IS NULL"]
    params: list[Any] = [scope_id]
    if before_ts is not None:
        where.append("(ts < ? OR (ts = ? AND id < ?))")
        params.extend([before_ts, before_ts, before_id or 0])
    sql = (
        "SELECT id, source, native_id, platform_message_id, direction, sender_id, "
        "sender_name, ts, text, content_json, component_types, reply_to_message_id, "
        "status, status_evidence, trace_id, llm_checkpoint_id "
        f"FROM chat_message WHERE {' AND '.join(where)} "
        "ORDER BY ts DESC, id DESC LIMIT ?"
    )
    params.append(limit + 1)
    rows = await storage.fetch_all(sql, params)
    has_more = len(rows) > limit
    rows = rows[:limit]
    rows.reverse()
    messages = []
    for row in rows:
        messages.append(
            {
                "id": row["id"],
                "kind": "message",
                "source": row["source"],
                "native_id": row["native_id"],
                "platform_message_id": row["platform_message_id"],
                "direction": row["direction"],
                "sender_id": row["sender_id"],
                "sender_name": row["sender_name"],
                "ts": row["ts"],
                "text": row["text"],
                "parts": json_loads(row["content_json"], []),
                "component_types": json_loads(row["component_types"], []),
                "reply_to": row["reply_to_message_id"],
                "status": row["status"],
                "status_evidence": row["status_evidence"],
                "trace_id": row["trace_id"],
                "checkpoint": row["llm_checkpoint_id"],
            }
        )
    oldest = rows[0]["ts"] if rows else before_ts
    newest = rows[-1]["ts"] if rows else before_ts
    replies = []
    if rows:
        traces = await storage.fetch_all(
            "SELECT trace_id, started_at, ended_at, final_request_id, final_preview, "
            "status, usage_input_other, usage_input_cached, usage_output "
            "FROM reply_trace WHERE scope_id=? AND started_at>=? AND started_at<=? "
            "AND final_preview IS NOT NULL ORDER BY started_at ASC",
            (scope_id, (oldest or 0) - 60, (newest or 0) + 60),
        )
        out_texts = [
            normalize_text(item["text"])
            for item in messages
            if item["direction"] == "out"
        ]
        out_rows = await storage.fetch_all(
            "SELECT text, ts FROM chat_message WHERE scope_id=? AND direction='out' "
            "AND ts BETWEEN ? AND ?",
            (scope_id, (oldest or 0) - 120, (newest or 0) + 120),
        )
        out_texts.extend(normalize_text(row["text"]) for row in out_rows)
        for trace in traces:
            preview = normalize_text(trace["final_preview"])
            if not preview:
                continue
            if any(_prefix_match(preview, text) for text in out_texts):
                continue
            replies.append(
                {
                    "id": f"trace:{trace['trace_id']}",
                    "kind": "reply_generated",
                    "direction": "out",
                    "sender_name": "bot",
                    "ts": trace["ended_at"] or trace["started_at"],
                    "text": trace["final_preview"],
                    "trace_id": trace["trace_id"],
                    "status": "generated_not_confirmed",
                    "status_evidence": "llm_final_text",
                    "usage": {
                        "input_other": trace["usage_input_other"],
                        "input_cached": trace["usage_input_cached"],
                        "output": trace["usage_output"],
                    },
                }
            )
    next_cursor = None
    if has_more and rows:
        next_cursor = {"before_ts": rows[0]["ts"], "before_id": rows[0]["id"]}
    return {
        "messages": messages,
        "replies": replies,
        "next_cursor": next_cursor,
        "coverage_note": (
            "显示原生平台记录与安装后的实时补采；原生群记录有数量上限，"
            "历史私聊消息若原生未保存则不可恢复。"
        ),
    }


def _prefix_match(left: str, right: str) -> bool:
    if not left or not right:
        return False
    sample = min(len(left), len(right), 80)
    return left[:sample] == right[:sample]


async def session_context(
    storage: Any,
    scope_id: str,
    *,
    budget_resolver: Any = None,
) -> dict:
    scope = await storage.fetch_one(
        "SELECT * FROM chat_scope WHERE scope_id=?", (scope_id,)
    )
    last_request = await storage.fetch_one(
        "SELECT r.*, res.usage_input_other, res.usage_input_cached, res.usage_output, "
        "res.usage_source, res.reasoning_status FROM llm_request r "
        "LEFT JOIN llm_result res ON res.request_id=r.request_id "
        "WHERE r.scope_id=? ORDER BY r.started_at DESC LIMIT 1",
        (scope_id,),
    )
    last_trace = await storage.fetch_one(
        "SELECT * FROM reply_trace WHERE scope_id=? ORDER BY started_at DESC LIMIT 1",
        (scope_id,),
    )
    compressions = await storage.fetch_all(
        "SELECT id, trace_id, strategy, threshold, trigger_budget, before_tokens, "
        "after_tokens, before_messages, after_messages, summary_blob_id, "
        "summary_chars, token_source, created_at FROM compression_event "
        "WHERE scope_id=? ORDER BY created_at DESC LIMIT 20",
        (scope_id,),
    )
    native_last = await storage.fetch_one(
        "SELECT * FROM native_reply_summary WHERE umo=? ORDER BY start_time DESC LIMIT 1",
        ((scope or {}).get("umo") or "",),
    )
    live_budget = None
    if budget_resolver is not None:
        try:
            live_budget = await budget_resolver(scope or {})
        except Exception:
            live_budget = None

    occupancy = None
    if last_request and last_request.get("usage_source") == "api_reported":
        occupancy = {
            "tokens": int(last_request.get("usage_input_other") or 0)
            + int(last_request.get("usage_input_cached") or 0),
            "source": "api_input",
            "at": last_request.get("started_at"),
            "request_id": last_request.get("request_id"),
            "note": "最近一次直接主模型请求的 usage.input",
        }
    elif native_last:
        occupancy = {
            "tokens": int(native_last.get("input_other") or 0)
            + int(native_last.get("input_cached") or 0),
            "source": "native_summary_input",
            "at": native_last.get("start_time"),
            "note": "原生 provider_stats 汇总输入（可能累计多次模型调用，不是单次请求快照）",
        }
    budget = live_budget or {}
    if last_request and last_request.get("context_budget"):
        budget = {
            **(budget or {}),
            "budget_tokens": last_request.get("context_budget"),
            "trigger_tokens": last_request.get("context_trigger"),
            "threshold": last_request.get("threshold"),
            "strategy": last_request.get("compression_strategy"),
            "keep_recent_ratio": last_request.get("keep_recent_ratio"),
            "source": "captured_runner_config",
        }
    return {
        "scope": scope,
        "occupancy": occupancy,
        "latest_trace": {
            "trace_id": last_trace["trace_id"],
            "status": last_trace["status"],
            "started_at": last_trace["started_at"],
            "ended_at": last_trace["ended_at"],
            "request_count": last_trace["request_count"],
            "tool_call_count": last_trace["tool_call_count"],
            "usage": {
                "input_other": last_trace["usage_input_other"],
                "input_cached": last_trace["usage_input_cached"],
                "output": last_trace["usage_output"],
                "complete_requests": last_trace["usage_complete"],
                "request_count": last_trace["request_count"],
            },
        }
        if last_trace
        else None,
        "budget": budget,
        "compressions": [
            {**row, "summary_available": bool(row.get("summary_blob_id"))}
            for row in compressions
        ],
        "notes": [
            "预算取当前会话/provider 配置，触发点按核心压缩阈值计算，不是写死的 375k。",
            "本次请求输入量优先使用接口 usage.input；原生汇总输入包含输出，仅作历史参考。",
            "压缩前原文只有被采集保存时才能回看；历史重建不会被当成 actual_input。",
        ],
    }


async def traces(
    storage: Any,
    *,
    from_ts: float,
    to_ts: float,
    scope_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    before: float | None = None,
) -> dict:
    limit = max(1, min(int(limit or 50), 200))
    where = ["t.started_at>=?", "t.started_at<?"]
    params: list[Any] = [from_ts, to_ts]
    if scope_id:
        where.append("t.scope_id=?")
        params.append(scope_id)
    if status:
        where.append("t.status=?")
        params.append(status)
    if before:
        where.append("t.started_at<?")
        params.append(before)
    params.append(limit + 1)
    rows = await storage.fetch_all(
        "SELECT t.*, s.display_name, s.alias, s.peer_id, s.is_group, "
        "(SELECT COUNT(*) FROM llm_result res JOIN llm_request r ON r.request_id=res.request_id "
        " WHERE r.trace_id=t.trace_id AND res.reasoning_status='available') AS reasoning_available, "
        "(SELECT SUM(CAST(c.amount AS REAL)) FROM cost_estimate c JOIN llm_request r "
        " ON r.request_id=c.request_id WHERE r.trace_id=t.trace_id AND c.status='estimated') AS cost_amount, "
        "(SELECT currency FROM cost_estimate c JOIN llm_request r ON r.request_id=c.request_id "
        " WHERE r.trace_id=t.trace_id AND c.status='estimated' LIMIT 1) AS cost_currency "
        f"FROM reply_trace t LEFT JOIN chat_scope s ON s.scope_id=t.scope_id "
        f"WHERE {' AND '.join(where)} ORDER BY t.started_at DESC LIMIT ?",
        params,
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for row in rows:
        items.append(
            {
                "trace_id": row["trace_id"],
                "scope_id": row["scope_id"],
                "umo": row["umo"],
                "display": scope_display(row),
                "is_group": bool(row.get("is_group")),
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "status": row["status"],
                "request_count": row["request_count"],
                "tool_call_count": row["tool_call_count"],
                "usage": {
                    "input_other": row["usage_input_other"],
                    "input_cached": row["usage_input_cached"],
                    "output": row["usage_output"],
                    "complete_requests": row["usage_complete"],
                },
                "cost": {
                    "amount": format(row["cost_amount"], "f")
                    if row.get("cost_amount") is not None
                    else None,
                    "currency": row.get("cost_currency"),
                },
                "reasoning_available": int(row.get("reasoning_available") or 0) > 0,
                "final_preview": row["final_preview"],
                "error": row["error"],
            }
        )
    next_cursor = rows[-1]["started_at"] if has_more and rows else None
    return {"traces": items, "next_cursor": next_cursor}


async def trace_detail(storage: Any, trace_id: str) -> dict | None:
    trace = await storage.fetch_one(
        "SELECT t.*, s.display_name, s.alias, s.peer_id, s.is_group, s.platform_id, "
        "s.message_type FROM reply_trace t LEFT JOIN chat_scope s ON s.scope_id=t.scope_id "
        "WHERE t.trace_id=?",
        (trace_id,),
    )
    if trace is None:
        return None
    requests = await storage.fetch_all(
        "SELECT r.*, res.reply_preview, res.reasoning_status, res.reasoning_state, "
        "res.usage_source, res.usage_input_other, res.usage_input_cached, res.usage_output, "
        "res.tool_calls_json, res.latency_ms, res.ttft_ms, "
        "c.amount AS cost_amount, c.currency AS cost_currency, c.status AS cost_status "
        "FROM llm_request r LEFT JOIN llm_result res ON res.request_id=r.request_id "
        "LEFT JOIN cost_estimate c ON c.request_id=r.request_id "
        "WHERE r.trace_id=? ORDER BY r.started_at ASC",
        (trace_id,),
    )
    compressions = await storage.fetch_all(
        "SELECT id, strategy, threshold, trigger_budget, before_tokens, after_tokens, "
        "before_messages, after_messages, summary_blob_id, summary_chars, token_source, "
        "created_at FROM compression_event WHERE trace_id=? ORDER BY created_at ASC",
        (trace_id,),
    )
    return {
        "trace": {
            "trace_id": trace["trace_id"],
            "scope_id": trace["scope_id"],
            "umo": trace["umo"],
            "display": scope_display(trace),
            "is_group": bool(trace.get("is_group")),
            "platform_id": trace.get("platform_id"),
            "message_type": trace.get("message_type"),
            "conversation_id": trace["conversation_id"],
            "trigger_message_id": trace["trigger_message_id"],
            "merged_count": trace["merged_count"],
            "engine": trace["engine"],
            "started_at": trace["started_at"],
            "ended_at": trace["ended_at"],
            "status": trace["status"],
            "request_count": trace["request_count"],
            "usage": {
                "input_other": trace["usage_input_other"],
                "input_cached": trace["usage_input_cached"],
                "output": trace["usage_output"],
                "complete_requests": trace["usage_complete"],
                "request_count": trace["request_count"],
            },
            "tool_call_count": trace["tool_call_count"],
            "final_preview": trace["final_preview"],
            "error": trace["error"],
        },
        "requests": [
            {
                "request_id": row["request_id"],
                "step_no": row["step_no"],
                "attempt_no": row["attempt_no"],
                "provider_id": row["provider_id"],
                "provider_model": row["provider_model"],
                "requested_model": row["requested_model"],
                "is_fallback": bool(row["is_fallback"]),
                "streaming": bool(row["streaming"]),
                "capture_stage": row["capture_stage"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "status": row["status"],
                "error": row["error"],
                "input_est_tokens": row["input_est_tokens"],
                "context_budget": row["context_budget"],
                "context_trigger": row["context_trigger"],
                "input_blob_state": row["input_blob_state"],
                "reply_preview": row.get("reply_preview"),
                "reasoning_status": row.get("reasoning_status"),
                "reasoning_state": row.get("reasoning_state"),
                "usage": _usage_from_row(row),
                "tool_calls": json_loads(row.get("tool_calls_json"), []),
                "latency_ms": row.get("latency_ms"),
                "ttft_ms": row.get("ttft_ms"),
                "cost": {
                    "amount": row.get("cost_amount"),
                    "currency": row.get("cost_currency"),
                    "status": row.get("cost_status"),
                },
            }
            for row in requests
        ],
        "compressions": compressions,
    }


async def requests_list(
    storage: Any,
    *,
    from_ts: float,
    to_ts: float,
    scope_id: str | None = None,
    status: str | None = None,
    provider_id: str | None = None,
    model: str | None = None,
    reasoning: str | None = None,
    cost_status: str | None = None,
    limit: int = 50,
    before: float | None = None,
) -> dict:
    limit = max(1, min(int(limit or 50), 200))
    where = ["r.started_at>=?", "r.started_at<?"]
    params: list[Any] = [from_ts, to_ts]
    if scope_id:
        where.append("r.scope_id=?")
        params.append(scope_id)
    if status:
        where.append("r.status=?")
        params.append(status)
    if provider_id:
        where.append("r.provider_id=?")
        params.append(provider_id)
    if model:
        where.append("r.provider_model LIKE ?")
        params.append(f"%{model}%")
    if reasoning == "available":
        where.append("res.reasoning_status='available'")
    elif reasoning == "not_provided":
        where.append("res.reasoning_status='not_provided'")
    elif reasoning == "problem":
        where.append("res.reasoning_status IN ('expired','partial')")
    if cost_status:
        where.append("c.status=?")
        params.append(cost_status)
    if before:
        where.append("r.started_at<?")
        params.append(before)
    params.append(limit + 1)
    rows = await storage.fetch_all(
        "SELECT r.*, res.reply_preview, res.reasoning_status, res.reasoning_state, "
        "res.usage_source, res.usage_input_other, res.usage_input_cached, "
        "res.usage_output, res.latency_ms, res.ttft_ms, "
        "c.amount AS cost_amount, c.currency AS cost_currency, c.status AS cost_status, "
        "s.display_name, s.alias, s.peer_id, s.is_group "
        "FROM llm_request r LEFT JOIN llm_result res ON res.request_id=r.request_id "
        "LEFT JOIN cost_estimate c ON c.request_id=r.request_id "
        "LEFT JOIN chat_scope s ON s.scope_id=r.scope_id "
        f"WHERE {' AND '.join(where)} ORDER BY r.started_at DESC LIMIT ?",
        params,
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        {
            "request_id": row["request_id"],
            "trace_id": row["trace_id"],
            "scope_id": row["scope_id"],
            "display": scope_display(row),
            "is_group": bool(row.get("is_group")),
            "step_no": row["step_no"],
            "attempt_no": row["attempt_no"],
            "provider_id": row["provider_id"],
            "provider_model": row["provider_model"],
            "requested_model": row["requested_model"],
            "is_fallback": bool(row["is_fallback"]),
            "streaming": bool(row["streaming"]),
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "status": row["status"],
            "error": row["error"],
            "input_est_tokens": row["input_est_tokens"],
            "context_budget": row["context_budget"],
            "context_trigger": row["context_trigger"],
            "reply_preview": row.get("reply_preview"),
            "reasoning_status": row.get("reasoning_status"),
            "reasoning_state": row.get("reasoning_state"),
            "usage": _usage_from_row(row),
            "latency_ms": row.get("latency_ms"),
            "ttft_ms": row.get("ttft_ms"),
            "cost": {
                "amount": row.get("cost_amount"),
                "currency": row.get("cost_currency"),
                "status": row.get("cost_status"),
            },
        }
        for row in rows
    ]
    next_cursor = rows[-1]["started_at"] if has_more and rows else None
    return {"requests": items, "next_cursor": next_cursor}


async def request_detail(storage: Any, request_id: str) -> dict | None:
    row = await storage.fetch_one(
        "SELECT r.*, res.response_id, res.role, res.reply_state, res.reply_preview, "
        "res.reply_chars, res.reasoning_state, res.reasoning_status, "
        "res.reasoning_source, res.reasoning_chars, res.signature_present, "
        "res.usage_source, res.usage_input_other, res.usage_input_cached, "
        "res.usage_output, res.usage_missing_reason, res.latency_ms, res.ttft_ms, "
        "res.tool_calls_json, c.amount AS cost_amount, c.currency AS cost_currency, "
        "c.status AS cost_status, c.reason AS cost_reason, c.price_version_id "
        "FROM llm_request r LEFT JOIN llm_result res ON res.request_id=r.request_id "
        "LEFT JOIN cost_estimate c ON c.request_id=r.request_id "
        "WHERE r.request_id=?",
        (request_id,),
    )
    if row is None:
        return None
    scope = await storage.fetch_one(
        "SELECT * FROM chat_scope WHERE scope_id=?", (row["scope_id"],)
    )
    compressions = await storage.fetch_all(
        "SELECT * FROM compression_event WHERE trace_id=? ORDER BY created_at ASC",
        (row["trace_id"],),
    )
    input_sections = json_loads(row["input_sections_json"], [])
    if row.get("input_blob_id"):
        snapshot_text = await storage.read_blob_text(row["input_blob_id"])
        if snapshot_text:
            snapshot = json_loads(snapshot_text, {})
            if snapshot.get("messages"):
                _, input_sections = recompute_sections(snapshot["messages"])
            if snapshot.get("tool_schema_est_tokens"):
                input_sections = list(input_sections) + [
                    {
                        "key": "tool_schema",
                        "label": "工具 schema",
                        "message_indices": [],
                        "tokens_estimate": snapshot.get("tool_schema_est_tokens"),
                        "tool_count": len(snapshot.get("tool_names") or []),
                    }
                ]
    return {
        "request": {
            "request_id": row["request_id"],
            "trace_id": row["trace_id"],
            "scope_id": row["scope_id"],
            "display": scope_display(scope or {}),
            "umo": scope.get("umo") if scope else None,
            "step_no": row["step_no"],
            "attempt_no": row["attempt_no"],
            "provider_id": row["provider_id"],
            "provider_model": row["provider_model"],
            "requested_model": row["requested_model"],
            "is_fallback": bool(row["is_fallback"]),
            "streaming": bool(row["streaming"]),
            "capture_stage": row["capture_stage"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "status": row["status"],
            "error": row["error"],
            "input_msg_count": row["input_msg_count"],
            "input_est_tokens": row["input_est_tokens"],
            "input_blob_id": row["input_blob_id"],
            "input_blob_state": row["input_blob_state"],
            "input_sections": input_sections,
            "tool_names": json_loads(row["tool_names_json"], []),
            "tool_schema_est_tokens": row["tool_schema_est_tokens"],
            "context": {
                "budget_tokens": row["context_budget"],
                "trigger_tokens": row["context_trigger"],
                "threshold": row["threshold"],
                "strategy": row["compression_strategy"],
                "keep_recent_ratio": row["keep_recent_ratio"],
            },
            "system_prompt_chars": row["system_prompt_chars"],
        },
        "result": {
            "response_id": row.get("response_id"),
            "role": row.get("role"),
            "reply_state": row.get("reply_state"),
            "reply_preview": row.get("reply_preview"),
            "reply_chars": row.get("reply_chars"),
            "reasoning_state": row.get("reasoning_state"),
            "reasoning_status": row.get("reasoning_status"),
            "reasoning_source": row.get("reasoning_source"),
            "reasoning_chars": row.get("reasoning_chars"),
            "signature_present": bool(row.get("signature_present")),
            "usage_source": row.get("usage_source"),
            "usage": {
                "input_other": row.get("usage_input_other"),
                "input_cached": row.get("usage_input_cached"),
                "output": row.get("usage_output"),
            },
            "usage_missing_reason": row.get("usage_missing_reason"),
            "latency_ms": row.get("latency_ms"),
            "ttft_ms": row.get("ttft_ms"),
            "tool_calls": json_loads(row.get("tool_calls_json"), []),
        },
        "cost": {
            "amount": row.get("cost_amount"),
            "currency": row.get("cost_currency"),
            "status": row.get("cost_status"),
            "reason": row.get("cost_reason"),
            "price_version_id": row.get("price_version_id"),
        },
        "compressions": compressions,
        "capture_note": (
            "采集点为 ToolLoopAgentRunner 压缩之后、provider 序列化之前；"
            "provider 适配器内部的再次改写不可见。"
        ),
    }


async def usage(
    storage: Any,
    *,
    from_ts: float,
    to_ts: float,
    group_by: str = "day",
    scope_id: str | None = None,
    provider_id: str | None = None,
    model: str | None = None,
    tz: str = "Asia/Shanghai",
) -> dict:
    where = ["r.started_at>=?", "r.started_at<?"]
    params: list[Any] = [from_ts, to_ts]
    if scope_id:
        where.append("r.scope_id=?")
        params.append(scope_id)
    if provider_id:
        where.append("r.provider_id=?")
        params.append(provider_id)
    if model:
        where.append("r.provider_model LIKE ?")
        params.append(f"%{model}%")
    rows = await storage.fetch_all(
        "SELECT r.started_at, r.provider_id, r.provider_model, r.scope_id, "
        "res.usage_source, res.usage_input_other, res.usage_input_cached, res.usage_output "
        "FROM llm_request r LEFT JOIN llm_result res ON res.request_id=r.request_id "
        f"WHERE {' AND '.join(where)} ORDER BY r.started_at ASC",
        params,
    )
    groups: dict[str, dict] = {}
    for row in rows:
        if group_by == "provider":
            key = row["provider_id"] or "unknown"
        elif group_by == "model":
            key = row["provider_model"] or "unknown"
        elif group_by == "scope":
            key = row["scope_id"]
        else:
            key = str(local_day_start(row["started_at"], tz))
        item = groups.setdefault(
            key,
            {
                "key": key,
                "requests": 0,
                "input_other": 0,
                "input_cached": 0,
                "output": 0,
                "unknown_usage": 0,
            },
        )
        item["requests"] += 1
        if row.get("usage_source") == "api_reported":
            item["input_other"] += int(row.get("usage_input_other") or 0)
            item["input_cached"] += int(row.get("usage_input_cached") or 0)
            item["output"] += int(row.get("usage_output") or 0)
        else:
            item["unknown_usage"] += 1
    scope_map = {}
    if group_by == "scope":
        scope_rows = await storage.fetch_all("SELECT * FROM chat_scope")
        scope_map = {row["scope_id"]: scope_display(row) for row in scope_rows}
        for key, item in groups.items():
            item["label"] = scope_map.get(key, key)
    return {"group_by": group_by, "buckets": list(groups.values())}


async def costs(
    storage: Any,
    *,
    from_ts: float,
    to_ts: float,
    scope_id: str | None = None,
    provider_id: str | None = None,
    model: str | None = None,
    limit: int = 200,
) -> dict:
    where = ["r.started_at>=?", "r.started_at<?"]
    params: list[Any] = [from_ts, to_ts]
    if scope_id:
        where.append("r.scope_id=?")
        params.append(scope_id)
    if provider_id:
        where.append("r.provider_id=?")
        params.append(provider_id)
    if model:
        where.append("r.provider_model LIKE ?")
        params.append(f"%{model}%")
    rows = await storage.fetch_all(
        "SELECT r.request_id, r.started_at, r.scope_id, r.provider_id, r.provider_model, "
        "c.amount, c.currency, c.status, c.reason, c.price_version_id, "
        "c.usage_input_other, c.usage_input_cached, c.usage_output "
        "FROM cost_estimate c JOIN llm_request r ON r.request_id=c.request_id "
        f"WHERE {' AND '.join(where)} ORDER BY r.started_at DESC LIMIT ?",
        params + [max(1, min(int(limit or 200), 1000))],
    )
    summary: dict[str, dict] = {}
    from decimal import Decimal

    for row in rows:
        status = row["status"] or "unknown"
        currency = row.get("currency") or ""
        key = f"{status}|{currency}"
        item = summary.setdefault(
            key,
            {"status": status, "currency": currency, "amount": "0", "count": 0},
        )
        item["count"] += 1
        if status == "estimated" and row.get("amount"):
            item["amount"] = format(
                Decimal(str(item["amount"])) + Decimal(str(row["amount"])), "f"
            )
    return {
        "rows": [
            {
                "request_id": row["request_id"],
                "started_at": row["started_at"],
                "scope_id": row["scope_id"],
                "provider_id": row["provider_id"],
                "provider_model": row["provider_model"],
                "amount": row.get("amount"),
                "currency": row.get("currency"),
                "status": row["status"],
                "reason": row.get("reason"),
                "price_version_id": row.get("price_version_id"),
                "usage": {
                    "input_other": row.get("usage_input_other"),
                    "input_cached": row.get("usage_input_cached"),
                    "output": row.get("usage_output"),
                },
            }
            for row in rows
        ],
        "summary": list(summary.values()),
        "note": "估算金额按请求发生时冻结的单价版本计算，不是供应商账单。",
    }


async def health_counts(storage: Any) -> dict:
    row = await storage.fetch_one(
        "SELECT "
        "(SELECT COUNT(*) FROM chat_message) AS messages, "
        "(SELECT COUNT(*) FROM chat_scope) AS scopes, "
        "(SELECT COUNT(*) FROM reply_trace) AS traces, "
        "(SELECT COUNT(*) FROM reply_trace WHERE status='running') AS open_traces, "
        "(SELECT COUNT(*) FROM llm_request) AS requests, "
        "(SELECT COUNT(*) FROM llm_result WHERE usage_source='unknown') AS unknown_usage, "
        "(SELECT COUNT(*) FROM llm_result WHERE reasoning_status='available') AS reasoning_available, "
        "(SELECT COUNT(*) FROM llm_request WHERE input_blob_state='expired') AS expired_snapshots, "
        "(SELECT COUNT(*) FROM native_reply_summary) AS native_summaries, "
        "(SELECT COUNT(*) FROM cost_estimate WHERE status='estimated') AS estimated_costs, "
        "(SELECT COUNT(*) FROM cost_estimate WHERE status='price_not_configured') AS unconfigured_prices"
    )
    return row or {}


async def export_rows(
    storage: Any,
    kind: str,
    *,
    from_ts: float,
    to_ts: float,
    scope_id: str | None = None,
    limit: int = 5000,
) -> list[dict]:
    limit = max(1, min(int(limit or 5000), 5000))
    if kind == "messages":
        where = ["ts>=?", "ts<?"]
        params: list[Any] = [from_ts, to_ts]
        if scope_id:
            where.append("scope_id=?")
            params.append(scope_id)
        return await storage.fetch_all(
            "SELECT id, scope_id, source, native_id, platform_message_id, direction, "
            "sender_id, sender_name, ts, text, component_types, status, status_evidence "
            f"FROM chat_message WHERE {' AND '.join(where)} "
            "ORDER BY ts DESC LIMIT ?",
            params + [limit],
        )
    if kind == "traces":
        return await storage.fetch_all(
            "SELECT trace_id, scope_id, umo, conversation_id, started_at, ended_at, "
            "status, request_count, tool_call_count, usage_input_other, "
            "usage_input_cached, usage_output, final_preview "
            "FROM reply_trace WHERE started_at>=? AND started_at<? "
            "ORDER BY started_at DESC LIMIT ?",
            (from_ts, to_ts, limit),
        )
    if kind == "costs":
        where = ["r.started_at>=?", "r.started_at<?"]
        params = [from_ts, to_ts]
        if scope_id:
            where.append("r.scope_id=?")
            params.append(scope_id)
        return await storage.fetch_all(
            "SELECT r.request_id, r.started_at, r.scope_id, r.provider_id, "
            "r.provider_model, c.amount, c.currency, c.status, c.reason "
            "FROM cost_estimate c JOIN llm_request r ON r.request_id=c.request_id "
            f"WHERE {' AND '.join(where)} ORDER BY r.started_at DESC LIMIT ?",
            params + [limit],
        )
    where = ["r.started_at>=?", "r.started_at<?"]
    params = [from_ts, to_ts]
    if scope_id:
        where.append("r.scope_id=?")
        params.append(scope_id)
    return await storage.fetch_all(
        "SELECT r.request_id, r.trace_id, r.scope_id, r.step_no, r.attempt_no, "
        "r.provider_id, r.provider_model, r.is_fallback, r.streaming, "
        "r.capture_stage, r.started_at, r.ended_at, r.status, r.error, "
        "r.input_est_tokens, r.context_budget, r.context_trigger, "
        "res.usage_source, res.usage_input_other, res.usage_input_cached, "
        "res.usage_output, res.reasoning_status, res.reasoning_state, "
        "c.amount AS cost_amount, c.currency AS cost_currency, c.status AS cost_status "
        "FROM llm_request r LEFT JOIN llm_result res ON res.request_id=r.request_id "
        "LEFT JOIN cost_estimate c ON c.request_id=r.request_id "
        f"WHERE {' AND '.join(where)} ORDER BY r.started_at DESC LIMIT ?",
        params + [limit],
    )
