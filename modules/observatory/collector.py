"""Main-chat capture pipeline.

The hot path only copies the minimum data and enqueues it; a background writer
task turns events into SQLite rows and gzip bodies. Nothing here may raise into
the chat flow, and nothing here sends messages, wakes the bot, or mutates the
provider request.
"""

from __future__ import annotations

import asyncio
import math
import time
import uuid
import weakref
from dataclasses import dataclass, field
from typing import Any

from .util import (
    SECTION_LABELS,
    classify_message,
    clip,
    estimate_json_tokens,
    estimate_tokens,
    is_reminder_text,
    json_dumps,
    parse_umo,
    safe_url,
    scope_id_for,
    text_hash,
)

CAPTURE_STAGE = "runner_provider_payload"
QUEUE_MAX = 2000
PART_TOKEN_IMAGE = 765
PART_TOKEN_AUDIO = 500


def _now() -> float:
    return time.time()


@dataclass
class TraceState:
    trace_id: str
    scope: dict
    started_at: float
    conversation_id: str | None = None
    session_id: str | None = None
    trigger_message_id: str | None = None
    merged_count: int = 1
    step_no: int = 1
    attempt_no: int = 0
    advance_next: bool = False
    closed: bool = False
    final_text: str = ""


@dataclass
class RequestCapture:
    request_id: str
    trace: TraceState
    provider_id: str = ""
    provider_model: str = ""
    requested_model: str | None = None
    is_fallback: bool = False
    streaming: bool = False
    started_at: float = field(default_factory=_now)
    first_token_at: float = 0.0
    chunks: int = 0
    chunk_text: list[str] = field(default_factory=list)
    chunk_reasoning: list[str] = field(default_factory=list)
    final: dict | None = None
    finished: bool = False
    outcome: str = "running"
    runner_ref: Any = None


class Collector:
    def __init__(
        self,
        storage: Any,
        settings: dict[str, Any],
        logger: Any = None,
        compat_state: Any = None,
    ) -> None:
        self.storage = storage
        self.settings = dict(settings or {})
        self.logger = logger
        self.compat_state = compat_state
        self.queue: asyncio.Queue[dict] | None = None
        self._writer_task: asyncio.Task | None = None
        self._maintenance_task: asyncio.Task | None = None
        self._closing = False
        self._dropped = 0
        self._last_drop_log = 0.0
        self._started = False

    # -------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._started:
            return
        self.queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self._closing = False
        self._started = True
        now = _now()
        # A previous process may have been killed mid-reply.
        await self.storage.fetch_all(
            "SELECT 1"
        )  # ensure the DB is reachable before background loops
        await self._close_stale_traces(now)
        try:
            await asyncio.to_thread(self.storage.backfill_trace_links)
        except Exception:
            pass
        self._writer_task = asyncio.create_task(self._writer_loop())
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())

    async def stop(self, flush_timeout: float = 5.0) -> None:
        self._closing = True
        if self.queue is not None and not self.queue.empty():
            try:
                await asyncio.wait_for(self.queue.join(), timeout=flush_timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        for task in (self._writer_task, self._maintenance_task):
            if task is not None and not task.done():
                task.cancel()
        self._writer_task = None
        self._maintenance_task = None
        self._started = False

    async def _close_stale_traces(self, now: float) -> None:
        running = await self.storage.fetch_one(
            "SELECT COUNT(*) AS c FROM reply_trace WHERE status='running'"
        )
        if running and int(running.get("c") or 0) > 0:
            await self._emit_control(
                [
                    {
                        "type": "gap",
                        "kind": "stale_trace",
                        "detail": (
                            f"closing {running.get('c')} running trace(s) left by a "
                            "previous process"
                        ),
                    }
                ]
            )
        await self._emit_control(
            [
                {
                    "type": "metric_inc",
                    "key": "collector_started",
                    "amount": 1,
                }
            ]
        )
        await asyncio.to_thread(self._close_stale_sync, now)

    def _close_stale_sync(self, now: float) -> None:
        with self.storage._lock:
            self.storage.conn.execute(
                "UPDATE reply_trace SET status='unknown', ended_at=?, "
                "error=COALESCE(error,'观察台在回复完成前停止或重载') WHERE status='running'",
                (now,),
            )

    def _close_old_open_traces(self) -> None:
        """Safety net for a reply that never produced a final response."""
        cutoff = _now() - 30 * 60
        try:
            with self.storage._lock:
                self.storage.conn.execute(
                    "UPDATE reply_trace SET status='unknown', ended_at=?, "
                    "error=COALESCE(error,'超过 30 分钟没有最终响应，状态未知') "
                    "WHERE status='running' AND started_at < ?",
                    (_now(), cutoff),
                )
        except Exception:
            pass

    def update_settings(self, settings: dict[str, Any]) -> None:
        self.settings = dict(settings or {})

    def enabled(self, key: str = "capture_enabled") -> bool:
        value = self.settings.get(key, True)
        return bool(value)

    # ----------------------------------------------------------------- queue
    async def _emit_control(self, events: list[dict]) -> None:
        try:
            await self.storage.write_events(events)
        except Exception:
            pass

    def _emit(self, event: dict) -> bool:
        queue = self.queue
        if queue is None or self._closing:
            self._dropped += 1
            return False
        try:
            queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            self._dropped += 1
            now = _now()
            if now - self._last_drop_log > 30:
                self._last_drop_log = now
                if self.logger:
                    self.logger.warning(
                        "观察台写入队列已满，事件被丢弃（累计 %d）", self._dropped
                    )
            if self._dropped % 20 == 1:
                self._emit_safe(
                    {
                        "type": "gap",
                        "kind": "queue_overflow",
                        "detail": f"queue full, dropped={self._dropped}",
                    }
                )
            return False

    def _emit_safe(self, event: dict) -> None:
        queue = self.queue
        if queue is not None:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def _writer_loop(self) -> None:
        assert self.queue is not None
        while not self._closing:
            try:
                first = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            batch = [first]
            deadline = time.monotonic() + 0.25
            while len(batch) < 500 and time.monotonic() < deadline:
                try:
                    batch.append(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await self.storage.write_events(batch)
            except Exception as exc:  # noqa: BLE001
                if self.logger:
                    self.logger.warning("observatory batch write failed: %s", exc)
            finally:
                for _ in batch:
                    self.queue.task_done()
        # Drain what was already queued on shutdown.
        while not self.queue.empty():
            batch = []
            try:
                while len(batch) < 500:
                    batch.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                pass
            if batch:
                try:
                    await self.storage.write_events(batch)
                except Exception:
                    pass
                for _ in batch:
                    self.queue.task_done()

    async def _maintenance_loop(self) -> None:
        while not self._closing:
            try:
                stats = await asyncio.to_thread(
                    self.storage.maintenance, self.settings
                )
                self._close_old_open_traces()
                if stats.get("trimmed_for_budget") or stats.get("expired_bodies"):
                    await self._emit_control(
                        [
                            {
                                "type": "metric_inc",
                                "key": "maintenance_trimmed",
                                "amount": float(
                                    stats.get("trimmed_for_budget", 0)
                                    + stats.get("expired_bodies", 0)
                                ),
                            }
                        ]
                    )
                if self._dropped:
                    await self._emit_control(
                        [
                            {
                                "type": "metric_inc",
                                "key": "queue_dropped",
                                "amount": float(self._dropped),
                            }
                        ]
                    )
                    self._dropped = 0
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                if self.logger:
                    self.logger.debug("observatory maintenance error: %s", exc)
            try:
                await asyncio.sleep(300)
            except asyncio.CancelledError:
                break

    # ------------------------------------------------------------- main reset
    def on_runner_reset(self, runner: Any) -> None:
        if not self.enabled():
            return
        if getattr(runner, "_observatory_trace", None) is not None:
            return
        trace = self._make_trace(runner)
        if trace is None:
            return
        self._emit(
            {
                "type": "metric_inc",
                "key": "trace_started",
                "amount": 1,
            }
        )

    def _make_trace(self, runner: Any) -> TraceState | None:
        run_context = getattr(runner, "run_context", None)
        context = getattr(run_context, "context", None)
        event = getattr(context, "event", None)
        if event is None:
            self._emit(
                {
                    "type": "gap",
                    "kind": "missing_event",
                    "detail": "main runner has no event; capture skipped",
                }
            )
            return None
        req = getattr(runner, "req", None)
        scope = self._scope_from_event(event)
        if scope is None:
            return None
        conversation_id = None
        if req is not None and getattr(req, "conversation", None) is not None:
            conversation_id = req.conversation.cid
        message_obj = getattr(event, "message_obj", None)
        trigger_message_id = str(getattr(message_obj, "message_id", "") or "") or None
        merged_count = 1
        try:
            merged_count = int(event.get_extra("private_message_wait_count") or 1) or 1
        except Exception:
            merged_count = 1
        trace = TraceState(
            trace_id=uuid.uuid4().hex,
            scope=scope,
            started_at=_now(),
            conversation_id=conversation_id,
            session_id=getattr(req, "session_id", None) if req is not None else None,
            trigger_message_id=trigger_message_id,
            merged_count=merged_count,
        )
        setattr(runner, "_observatory_trace", trace)
        context_manager = getattr(runner, "request_context_manager", None)
        if context_manager is not None:
            try:
                setattr(context_manager, "_observatory_trace", trace)
            except Exception:
                pass
        self._emit({"type": "scope_upsert", "scope": scope})
        try:
            event.set_extra("_observatory_trace_id", trace.trace_id)
        except Exception:
            pass
        self._emit(
            {
                "type": "trace_start",
                "row": {
                    "trace_id": trace.trace_id,
                    "scope_id": scope["scope_id"],
                    "umo": scope["umo"],
                    "conversation_id": conversation_id,
                    "session_id": trace.session_id,
                    "persona_id": None,
                    "trigger_message_id": trigger_message_id,
                    "merged_count": merged_count,
                    "engine": "local_tool_loop_agent",
                    "started_at": trace.started_at,
                    "status": "running",
                },
            }
        )
        # The triggering platform message belongs to the live capture too; using
        # the same native_id makes the insert idempotent.
        if self.enabled("record_incoming_messages"):
            self._emit_live_message(event, scope, trace_id=trace.trace_id)
            if trigger_message_id:
                self._emit(
                    {
                        "type": "message_trace",
                        "scope_id": scope["scope_id"],
                        "platform_message_id": trigger_message_id,
                        "trace_id": trace.trace_id,
                    }
                )
        return trace

    def _scope_from_event(self, event: Any) -> dict | None:
        try:
            umo = str(event.unified_msg_origin or "")
            platform_id = str(event.get_platform_id() or "")
            message_obj = getattr(event, "message_obj", None)
            message_type = "GroupMessage"
            try:
                mt = event.get_message_type()
                message_type = getattr(mt, "value", str(mt))
            except Exception:
                message_type = str(getattr(getattr(message_obj, "type", ""), "value", ""))
            group = getattr(message_obj, "group", None)
            group_id = str(getattr(group, "group_id", "") or getattr(message_obj, "group_id", "") or "")
            is_group = "GroupMessage" in message_type or bool(group_id)
            peer_id = group_id if is_group else str(event.get_sender_id() or "")
            if not peer_id:
                _, _, peer_id = parse_umo(umo)
            if not platform_id or not peer_id:
                return None
            display = None
            if is_group:
                display = str(getattr(group, "group_name", "") or "") or None
            else:
                sender = getattr(message_obj, "sender", None)
                display = str(getattr(sender, "nickname", "") or "") or None
            bot_self_id = str(event.get_self_id() or "")
            scope_id = scope_id_for(platform_id, message_type or "", peer_id)
            return {
                "scope_id": scope_id,
                "platform_id": platform_id,
                "bot_self_id": bot_self_id,
                "message_type": message_type or "",
                "peer_id": peer_id,
                "umo": umo or f"{platform_id}:{message_type}:{peer_id}",
                "display_name": clip(display, 200) if display else None,
                "alias": None,
                "is_group": is_group,
                "first_seen": _now(),
                "last_seen": _now(),
            }
        except Exception as exc:  # noqa: BLE001
            self._emit_safe(
                {
                    "type": "gap",
                    "kind": "scope_error",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            return None

    # --------------------------------------------------------------- requests
    def begin_request(self, runner: Any, include_model: bool = True) -> RequestCapture | None:
        if not self.enabled():
            return None
        trace = getattr(runner, "_observatory_trace", None)
        if trace is None:
            trace = self._make_trace(runner)
        if trace is None:
            return None
        if trace.closed:
            self._emit(
                {
                    "type": "gap",
                    "kind": "request_after_trace_close",
                    "detail": f"trace {trace.trace_id} got another request; new trace created",
                }
            )
            setattr(runner, "_observatory_trace", None)
            trace = self._make_trace(runner)
            if trace is None:
                return None
        if trace.advance_next:
            trace.step_no += 1
            trace.attempt_no = 1
            trace.advance_next = False
        else:
            trace.attempt_no += 1

        provider = getattr(runner, "provider", None)
        provider_id = ""
        provider_model = ""
        if provider is not None:
            provider_config = getattr(provider, "provider_config", {}) or {}
            provider_id = str(provider_config.get("id") or "")
            try:
                provider_model = str(provider.get_model() or "")
            except Exception:
                provider_model = str(provider_config.get("model") or "")
            if not provider_id:
                try:
                    provider_id = str(provider.meta().id or "")
                except Exception:
                    provider_id = ""
        req = getattr(runner, "req", None)
        requested_model = getattr(req, "model", None) if include_model else None
        try:
            is_fallback = not include_model
        except Exception:
            is_fallback = False
        snapshot = self._build_snapshot(runner, provider)
        if snapshot is None:
            snapshot = {
                "capture_stage": CAPTURE_STAGE,
                "created_at": _now(),
                "message_count": None,
                "messages": [],
                "tool_names": [],
                "tool_schema": [],
                "tool_schema_est_tokens": None,
                "total_est_tokens": None,
                "system_prompt_chars": None,
                "context": {},
                "sections": [],
                "snapshot_error": "snapshot build failed",
            }
        cap = RequestCapture(
            request_id=uuid.uuid4().hex,
            trace=trace,
            provider_id=provider_id,
            provider_model=provider_model,
            requested_model=requested_model,
            is_fallback=is_fallback,
            streaming=bool(getattr(runner, "streaming", False)),
            started_at=_now(),
        )
        try:
            setattr(runner, "_observatory_cap", cap)
        except Exception:
            pass
        trace.final_text = trace.final_text or ""
        tool_names = snapshot.get("tool_names") or []
        try:
            cap.runner_ref = weakref.ref(runner)
        except TypeError:
            cap.runner_ref = None
        self._emit(
            {
                "type": "request_start",
                "row": {
                    "request_id": cap.request_id,
                    "trace_id": trace.trace_id,
                    "scope_id": trace.scope["scope_id"],
                    "step_no": trace.step_no,
                    "attempt_no": trace.attempt_no,
                    "provider_id": provider_id,
                    "provider_model": provider_model,
                    "requested_model": requested_model,
                    "is_fallback": is_fallback,
                    "streaming": cap.streaming,
                    "capture_stage": CAPTURE_STAGE,
                    "started_at": cap.started_at,
                    "status": "running",
                    "input_msg_count": snapshot.get("message_count"),
                    "input_est_tokens": snapshot.get("total_est_tokens"),
                    "input_blob_id": uuid.uuid4().hex,
                    "sections": snapshot.get("sections"),
                    "tool_names": tool_names,
                    "tool_schema_est_tokens": snapshot.get("tool_schema_est_tokens"),
                    "context_budget": snapshot.get("context", {}).get("budget_tokens"),
                    "context_trigger": snapshot.get("context", {}).get("trigger_tokens"),
                    "compression_strategy": snapshot.get("context", {}).get("strategy"),
                    "keep_recent_ratio": snapshot.get("context", {}).get(
                        "keep_recent_ratio"
                    ),
                    "threshold": snapshot.get("context", {}).get("threshold"),
                    "system_prompt_chars": snapshot.get("system_prompt_chars"),
                },
                "input_text": json_dumps(snapshot),
                "input_expires_at": self._expires_at("retention_snapshot_days"),
            }
        )
        self._emit({"type": "metric_inc", "key": "request_started", "amount": 1})
        return cap

    def _build_snapshot(self, runner: Any, provider: Any) -> dict | None:
        try:
            return self._build_snapshot_inner(runner, provider)
        except Exception as exc:  # noqa: BLE001 - never break the chat
            self._emit_safe(
                {
                    "type": "gap",
                    "kind": "snapshot_error",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            return None

    def _build_snapshot_inner(self, runner: Any, provider: Any) -> dict:
        context_manager = getattr(runner, "request_context_manager", None)
        config = getattr(context_manager, "config", None)
        compressor = getattr(context_manager, "compressor", None)
        budget = 0
        try:
            budget = int(getattr(config, "max_context_tokens", 0) or 0)
        except Exception:
            budget = 0
        threshold = 0.82
        try:
            threshold = float(getattr(compressor, "compression_threshold", 0.82))
        except Exception:
            threshold = 0.82
        keep_recent = None
        try:
            keep_recent = float(getattr(compressor, "keep_recent_ratio"))
        except Exception:
            keep_recent = None
        strategy = type(compressor).__name__ if compressor is not None else None
        trigger = int(math.ceil(budget * threshold)) if budget > 0 else None

        modalities = None
        if provider is not None:
            try:
                modalities = provider.provider_config.get("modalities")
            except Exception:
                modalities = None

        tool_set = None
        try:
            tool_set = runner._func_tool_for_provider()
        except Exception:
            tool_set = getattr(getattr(runner, "req", None), "func_tool", None)
        if isinstance(modalities, list) and modalities and "tool_use" not in modalities:
            tool_set = None

        tool_names: list[str] = []
        tool_schema: list[dict] = []
        schema_est = 0
        if tool_set is not None:
            try:
                tool_names = list(tool_set.names())
            except Exception:
                tool_names = []
            try:
                raw_schema = tool_set.openai_schema()
                schema_est = estimate_json_tokens(raw_schema)
                for item in raw_schema[:200]:
                    fn = item.get("function") or {}
                    tool_schema.append(
                        {
                            "name": clip(fn.get("name"), 120),
                            "description": clip(fn.get("description"), 500),
                            "parameters": list(
                                ((fn.get("parameters") or {}).get("properties") or {}).keys()
                            )[:40],
                        }
                    )
            except Exception:
                tool_schema = [{"name": name} for name in tool_names[:200]]
                schema_est = estimate_tokens(json_dumps(tool_schema))

        messages = list(getattr(getattr(runner, "run_context", None), "messages", []) or [])
        last_user_index = -1
        raw_last_user_index = -1
        for index, message in enumerate(messages):
            if getattr(message, "role", None) != "user":
                continue
            raw_last_user_index = index
            if self._meaningful_message_text(message).strip():
                last_user_index = index
        if last_user_index < 0:
            last_user_index = raw_last_user_index

        serialized: list[dict] = []
        sections: dict[str, dict] = {}
        total_est = 0
        system_prompt_chars = 0
        dropped_parts = 0
        for index, message in enumerate(messages):
            item = self._serialize_message(
                message,
                index=index,
                last_user_index=last_user_index,
                modalities=modalities,
            )
            dropped_parts += item.pop("dropped_parts", 0)
            if item["role"] == "system" and system_prompt_chars == 0:
                system_prompt_chars = len(item.get("plain_text") or "")
            est = item.get("est_tokens") or 0
            total_est += est
            group = item["group"]
            section = sections.setdefault(
                group,
                {
                    "key": group,
                    "label": SECTION_LABELS.get(group, group),
                    "message_indices": [],
                    "tokens_estimate": 0,
                },
            )
            section["message_indices"].append(index)
            section["tokens_estimate"] += est
            item.pop("plain_text", None)
            serialized.append(item)

        ordered_sections = [
            sections[key] for key in SECTION_LABELS if key in sections
        ]
        if schema_est:
            ordered_sections.append(
                {
                    "key": "tool_schema",
                    "label": "工具 schema",
                    "message_indices": [],
                    "tokens_estimate": schema_est,
                    "tool_count": len(tool_names),
                }
            )
        return {
            "capture_stage": CAPTURE_STAGE,
            "capture_stage_note": (
                "压缩处理之后、provider 适配器序列化之前，ToolLoopAgentRunner "
                "实际传给 provider 的 messages/tools（按 provider modalities 镜像过滤）。"
            ),
            "created_at": _now(),
            "message_count": len(serialized),
            "messages": serialized,
            "tool_names": tool_names,
            "tool_schema": tool_schema,
            "tool_schema_est_tokens": schema_est,
            "total_est_tokens": total_est + schema_est,
            "messages_est_tokens": total_est,
            "system_prompt_chars": system_prompt_chars,
            "dropped_modality_parts": dropped_parts,
            "context": {
                "budget_tokens": budget or None,
                "trigger_tokens": trigger,
                "threshold": threshold,
                "strategy": strategy,
                "keep_recent_ratio": keep_recent,
                "source": "runner_context_config",
            },
            "sections": ordered_sections,
        }

    def _serialize_message(
        self,
        message: Any,
        index: int,
        last_user_index: int,
        modalities: Any,
    ) -> dict:
        role = str(getattr(message, "role", "") or "unknown")
        raw_content = getattr(message, "content", None)
        parts: list[dict] = []
        plain_text_parts: list[str] = []
        dropped = 0
        no_save = bool(getattr(message, "_no_save", False))
        image_allowed = not (isinstance(modalities, list) and modalities and "image" not in modalities)
        audio_allowed = not (isinstance(modalities, list) and modalities and "audio" not in modalities)
        if isinstance(raw_content, str):
            parts.append({"type": "text", "text": raw_content})
            plain_text_parts.append(raw_content)
        elif isinstance(raw_content, list):
            for part in raw_content:
                ptype = str(getattr(part, "type", "") or "unknown")
                if getattr(part, "_no_save", False):
                    no_save = True
                if ptype == "text":
                    text = str(getattr(part, "text", "") or "")
                    parts.append({"type": "text", "text": text})
                    plain_text_parts.append(text)
                elif ptype == "think":
                    parts.append(
                        {
                            "type": "think",
                            "think": str(getattr(part, "think", "") or ""),
                            "encrypted_present": bool(getattr(part, "encrypted", None)),
                        }
                    )
                elif ptype == "image_url":
                    if not image_allowed:
                        dropped += 1
                        continue
                    url_obj = getattr(part, "image_url", None)
                    url = getattr(url_obj, "url", None)
                    parts.append(
                        {
                            "type": "image_url",
                            "url": safe_url(url),
                            "data_uri": bool(isinstance(url, str) and url.startswith("data:")),
                            "id": clip(getattr(url_obj, "id", None), 200),
                        }
                    )
                elif ptype == "audio_url":
                    if not audio_allowed:
                        dropped += 1
                        continue
                    url_obj = getattr(part, "audio_url", None)
                    url = getattr(url_obj, "url", None)
                    parts.append(
                        {
                            "type": "audio_url",
                            "url": safe_url(url),
                            "data_uri": bool(isinstance(url, str) and url.startswith("data:")),
                            "id": clip(getattr(url_obj, "id", None), 200),
                        }
                    )
                else:
                    parts.append({"type": ptype})
        elif raw_content is not None:
            maybe_id = getattr(raw_content, "id", None)
            part = {"type": "checkpoint"} if maybe_id else {"type": "unknown"}
            if maybe_id:
                part["id"] = clip(maybe_id, 120)
            parts.append(part)

        tool_calls: list[dict] = []
        for call in list(getattr(message, "tool_calls", None) or [])[:40]:
            if isinstance(call, dict):
                fn = call.get("function") or {}
                tool_calls.append(
                    {
                        "id": clip(call.get("id"), 120),
                        "name": clip(fn.get("name"), 120),
                        "arguments": clip(fn.get("arguments"), 4000),
                    }
                )
            else:
                fn = getattr(call, "function", None)
                tool_calls.append(
                    {
                        "id": clip(getattr(call, "id", None), 120),
                        "name": clip(getattr(fn, "name", None), 120),
                        "arguments": clip(getattr(fn, "arguments", None), 4000),
                    }
                )

        plain_text = "\n".join(text for text in plain_text_parts if text)
        meaningful_parts = [
            text for text in plain_text_parts if not is_reminder_text(text)
        ]
        meaningful_text = "\n".join(text for text in meaningful_parts if text)
        has_reminder = any(is_reminder_text(text) for text in plain_text_parts)
        group = self._classify(
            role,
            meaningful_text,
            bool(tool_calls),
            index,
            last_user_index,
            no_save=no_save,
            has_reminder=has_reminder,
            only_reminder=has_reminder and not meaningful_text.strip(),
        )
        tokens = 0
        for part in parts:
            if part["type"] == "text":
                tokens += estimate_tokens(part.get("text"))
            elif part["type"] == "think":
                tokens += estimate_tokens(part.get("think"))
            elif part["type"] == "image_url":
                tokens += PART_TOKEN_IMAGE
            elif part["type"] == "audio_url":
                tokens += PART_TOKEN_AUDIO
        for call in tool_calls:
            tokens += estimate_tokens(json_dumps(call))

        return {
            "index": index,
            "role": role,
            "group": group,
            "parts": parts,
            "tool_calls": tool_calls,
            "tool_call_id": clip(getattr(message, "tool_call_id", None), 120),
            "no_save": no_save,
            "est_tokens": tokens,
            "plain_text": plain_text,
            "dropped_parts": dropped,
        }

    @staticmethod
    def _meaningful_message_text(message: Any) -> str:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return "" if is_reminder_text(content) else content
        if isinstance(content, list):
            texts = []
            for part in content:
                if getattr(part, "type", "") == "text":
                    text = str(getattr(part, "text", "") or "")
                    if not is_reminder_text(text):
                        texts.append(text)
            return "\n".join(texts)
        return ""

    @staticmethod
    def _classify(
        role: str,
        plain_text: str,
        has_tool_calls: bool,
        index: int,
        last_user_index: int,
        *,
        no_save: bool = False,
        has_reminder: bool = False,
        only_reminder: bool = False,
    ) -> str:
        return classify_message(
            role,
            plain_text,
            has_tool_calls,
            index,
            last_user_index,
            no_save=no_save,
            has_reminder=has_reminder,
            only_reminder=only_reminder,
        )

    # --------------------------------------------------------------- response
    def observe_response(self, cap: RequestCapture, response: Any) -> None:
        if cap.finished:
            return
        if getattr(response, "is_chunk", False):
            cap.chunks += 1
            if not cap.first_token_at:
                cap.first_token_at = _now()
            try:
                text = response.completion_text or ""
            except Exception:
                text = ""
            reasoning = getattr(response, "reasoning_content", None)
            if text:
                cap.chunk_text.append(text)
            if reasoning:
                cap.chunk_reasoning.append(reasoning)
            return
        cap.first_token_at = cap.first_token_at or _now()
        usage = getattr(response, "usage", None)
        cap.final = {
            "role": str(getattr(response, "role", "") or ""),
            "text": str(getattr(response, "completion_text", "") or ""),
            "reasoning": getattr(response, "reasoning_content", None),
            "signature": bool(getattr(response, "reasoning_signature", None)),
            "response_id": clip(getattr(response, "id", None), 200),
            "tool_calls": [
                {
                    "id": clip(call_id, 120),
                    "name": clip(name, 120),
                    "arguments": clip(json_dumps(args), 4000),
                }
                for name, args, call_id in zip(
                    list(getattr(response, "tools_call_name", []) or []),
                    list(getattr(response, "tools_call_args", []) or []),
                    list(getattr(response, "tools_call_ids", []) or []),
                )
            ][:40],
            "usage": (
                {
                    "input_other": getattr(usage, "input_other", None),
                    "input_cached": getattr(usage, "input_cached", None),
                    "output": getattr(usage, "output", None),
                }
                if usage is not None
                else None
            ),
        }

    def finish_request(
        self, cap: RequestCapture, outcome: str = "completed", error: str | None = None
    ) -> None:
        if cap.finished:
            return
        cap.finished = True
        cap.outcome = outcome
        trace = cap.trace
        final = cap.final
        now = _now()
        chunk_text = "".join(cap.chunk_text)
        chunk_reasoning = "".join(cap.chunk_reasoning)

        # The fallback wrapper closes this generator immediately after the
        # runner consumes a final response, which surfaces as GeneratorExit.
        # The runner state tells us whether that close means "done", "terminal
        # error", or "fallback may still continue".
        runner_obj = None
        if cap.runner_ref is not None:
            try:
                runner_obj = cap.runner_ref()
            except Exception:
                runner_obj = None
        runner_state = ""
        try:
            if runner_obj is not None:
                runner_state = getattr(getattr(runner_obj, "state", None), "name", "")
        except Exception:
            runner_state = ""
        aborted = False
        try:
            if runner_obj is not None and hasattr(runner_obj, "was_aborted"):
                aborted = bool(runner_obj.was_aborted())
        except Exception:
            aborted = False

        finished_status = "completed"
        result_role = final.get("role") if final else None
        reply_text = ""
        reasoning_text = ""
        reasoning_status = "not_provided"
        reasoning_source = None
        signature_present = False
        tool_calls: list[dict] = []
        usage: dict | None = None
        usage_missing_reason = "模型响应未包含 usage"
        response_id = None
        if final is not None:
            response_id = final.get("response_id")
            reply_text = final.get("text") or ""
            final_reasoning = final.get("reasoning") or ""
            if final_reasoning:
                reasoning_text = str(final_reasoning)
                reasoning_source = "api_reported"
                reasoning_status = "available"
            elif chunk_reasoning:
                reasoning_text = chunk_reasoning
                reasoning_source = "stream_deltas"
                reasoning_status = "available" if outcome == "completed" else "partial"
            signature_present = bool(final.get("signature"))
            tool_calls = final.get("tool_calls") or []
            usage = final.get("usage")
            if result_role == "err":
                finished_status = "error"
                error = error or clip(reply_text, 2000) or "model returned an error response"
            elif outcome == "cancelled":
                # A received final response means the provider call completed,
                # even if the consumer closes the generator right after it.
                finished_status = "completed"
            elif outcome == "incomplete":
                finished_status = "incomplete"
        else:
            if chunk_text:
                reply_text = chunk_text
            if chunk_reasoning:
                reasoning_text = chunk_reasoning
                reasoning_source = "stream_deltas"
                reasoning_status = "partial"
            if outcome == "cancelled":
                finished_status = "cancelled"
            elif outcome == "completed":
                finished_status = "incomplete"
                error = error or "stream ended without a final response"
            else:
                finished_status = "error" if outcome == "error" else outcome

        if finished_status == "error" and outcome == "error" and not error:
            error = "provider request failed"
        if reasoning_status == "not_provided" and signature_present:
            reasoning_status = "not_provided"

        latency_ms = int((now - cap.started_at) * 1000)
        ttft_ms = (
            int((cap.first_token_at - cap.started_at) * 1000)
            if cap.first_token_at
            else None
        )
        usage_source = "api_reported"
        usage_values = None
        if usage is not None:
            usage_values = {
                "input_other": usage.get("input_other"),
                "input_cached": usage.get("input_cached"),
                "output": usage.get("output"),
            }
            if None in usage_values.values():
                usage_source = "unknown"
                usage_missing_reason = "usage 字段不完整"
        else:
            usage_source = "unknown"

        reply_blob_id = uuid.uuid4().hex if reply_text else None
        reasoning_blob_id = uuid.uuid4().hex if reasoning_text else None
        result = {
            "result_id": uuid.uuid4().hex,
            "response_id": response_id,
            "role": result_role,
            "status": finished_status,
            "error": clip(error, 2000) if error else None,
            "reply_text": reply_text,
            "reply_blob_id": reply_blob_id,
            "reply_state": "available" if reply_text else "empty",
            "reasoning_text": reasoning_text,
            "reasoning_blob_id": reasoning_blob_id,
            "reasoning_status": reasoning_status,
            "reasoning_source": reasoning_source,
            "signature_present": signature_present,
            "tool_calls": tool_calls,
            "usage": usage_values or {},
            "usage_source": usage_source,
            "usage_missing_reason": (
                None if usage_source == "api_reported" else usage_missing_reason
            ),
            "latency_ms": latency_ms,
            "ttft_ms": ttft_ms,
            "finish_reason": None,
        }
        self._emit(
            {
                "type": "request_finish",
                "request_id": cap.request_id,
                "trace_id": trace.trace_id,
                "provider_id": cap.provider_id,
                "provider_model": cap.provider_model,
                "result": result,
                "reasoning_expires_at": self._expires_at("retention_reasoning_days"),
                "reply_expires_at": self._expires_at("retention_snapshot_days"),
            }
        )
        if finished_status != "completed":
            self._emit(
                {
                    "type": "metric_inc",
                    "key": "request_not_completed",
                    "amount": 1,
                }
            )
        try:
            setattr(cap.trace, "final_text", reply_text or trace.final_text)
        except Exception:
            pass

        # Decide whether this reply trace is complete. Closing cannot be decided
        # at provider-final time alone: a failed candidate may still fall back.
        close_status = None
        close_error = None
        if final is not None and result_role == "err":
            # An err response is handed to the fallback loop first and only
            # reaches the runner when every candidate failed; the runner state
            # distinguishes terminal failure from a pending fallback attempt.
            if runner_state == "ERROR":
                close_status = "error"
                close_error = error
        elif tool_calls:
            # The runner closes this generator right after receiving a final
            # response, then executes tools and starts the next step.
            trace.advance_next = True
        elif final is not None:
            close_status = "completed"
        elif outcome == "cancelled" or aborted:
            close_status = "aborted"
            close_error = error or "用户中止或连接取消"
        if close_status is not None:
            trace.closed = True
            self._emit(
                {
                    "type": "trace_close",
                    "trace_id": trace.trace_id,
                    "status": close_status,
                    "ended_at": now,
                    "final_request_id": cap.request_id,
                    "final_text": reply_text or trace.final_text,
                    "final_text_hash": text_hash(reply_text or trace.final_text),
                    "error": close_error,
                }
            )
        # Detach this attempt from the runner so a later attempt cannot read it.
        if cap.runner_ref is not None:
            try:
                runner_obj = cap.runner_ref()
                if runner_obj is not None and getattr(
                    runner_obj, "_observatory_cap", None
                ) is cap:
                    setattr(runner_obj, "_observatory_cap", None)
            except Exception:
                pass

    # ------------------------------------------------------------ compression
    def on_compression(
        self,
        context_manager: Any,
        messages_before: Any,
        before_tokens: Any,
        messages_after: Any,
    ) -> None:
        trace = getattr(context_manager, "_observatory_trace", None)
        if trace is None or trace.closed:
            self._emit(
                {
                    "type": "metric_inc",
                    "key": "compression_unscoped",
                    "amount": 1,
                }
            )
            return
        try:
            counter = getattr(context_manager, "token_counter", None)
            if counter is not None:
                after_tokens = int(counter.count_tokens(list(messages_after)))
                token_source = "core_estimator_after_compression"
            else:
                after_tokens = sum(
                    estimate_tokens(self._message_text(m)) for m in messages_after
                )
                token_source = "local_estimator"
        except Exception:
            after_tokens = None
            token_source = "unknown"
        compressor = getattr(context_manager, "compressor", None)
        threshold = float(getattr(compressor, "compression_threshold", 0.82) or 0.82)
        strategy = type(compressor).__name__ if compressor is not None else None
        config = getattr(context_manager, "config", None)
        budget = int(getattr(config, "max_context_tokens", 0) or 0)
        summary_text = ""
        for message in list(messages_after or [])[:6]:
            text = self._message_text(message)
            lowered = text.lower()
            if "previous history conversation summary" in lowered or "对话历史总结" in text:
                summary_text = text
                break
        summary_blob_id = uuid.uuid4().hex if summary_text else None
        self._emit(
            {
                "type": "compression",
                "row": {
                    "trace_id": trace.trace_id,
                    "scope_id": trace.scope["scope_id"],
                    "umo": trace.scope["umo"],
                    "conversation_id": trace.conversation_id,
                    "strategy": strategy,
                    "threshold": threshold,
                    "trigger_budget": int(math.ceil(budget * threshold)) if budget else None,
                    "before_tokens": int(before_tokens) if before_tokens is not None else None,
                    "after_tokens": after_tokens,
                    "before_messages": len(list(messages_before or [])),
                    "after_messages": len(list(messages_after or [])),
                    "summary_blob_id": summary_blob_id,
                    "summary_text": summary_text,
                    "token_source": token_source,
                    "created_at": _now(),
                },
                "summary_expires_at": self._expires_at("retention_snapshot_days"),
            }
        )
        self._emit({"type": "metric_inc", "key": "compression_events", "amount": 1})

    @staticmethod
    def _message_text(message: Any) -> str:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks = []
            for part in content:
                if getattr(part, "type", "") == "text":
                    chunks.append(str(getattr(part, "text", "") or ""))
                elif getattr(part, "type", "") == "think":
                    chunks.append(str(getattr(part, "think", "") or ""))
            return "\n".join(chunks)
        return ""

    # ------------------------------------------------------------------ live
    def record_incoming(self, event: Any) -> None:
        if not self.enabled() or not self.enabled("record_incoming_messages"):
            return
        scope = self._scope_from_event(event)
        if scope is None:
            return
        self._emit({"type": "scope_upsert", "scope": scope})
        self._emit_live_message(event, scope)

    def _emit_live_message(self, event: Any, scope: dict, trace_id: str | None = None) -> None:
        message_obj = getattr(event, "message_obj", None)
        if message_obj is None:
            return
        platform_id = scope["platform_id"]
        message_id = str(getattr(message_obj, "message_id", "") or "")
        native_id = (
            f"live:{platform_id}:{message_id}" if message_id else f"live:{uuid.uuid4().hex}"
        )
        components = list(getattr(message_obj, "message", None) or [])
        parts: list[dict] = []
        text_chunks: list[str] = []
        component_types: set[str] = set()
        reply_to = None
        max_chars = int(self.settings.get("max_message_chars") or 8000)
        for comp in components[:120]:
            ctype = str(getattr(comp, "type", "") or "unknown")
            ctype = getattr(ctype, "value", ctype)
            component_types.add(ctype)
            if ctype == "Plain":
                text = str(getattr(comp, "text", "") or "")
                parts.append({"type": "plain", "text": clip(text, max_chars)})
                text_chunks.append(text)
            elif ctype in ("Image", "Record", "Video", "File"):
                parts.append(
                    {
                        "type": ctype.lower(),
                        "url": safe_url(
                            getattr(comp, "url", None) or getattr(comp, "file", None)
                        ),
                        "file_present": bool(
                            getattr(comp, "file", None) or getattr(comp, "path", None)
                        ),
                        "name": clip(getattr(comp, "name", None), 200),
                    }
                )
            elif ctype == "At":
                parts.append(
                    {
                        "type": "at",
                        "user_id": clip(getattr(comp, "qq", None), 120),
                        "name": clip(getattr(comp, "name", None), 120),
                    }
                )
            elif ctype == "Reply":
                reply_to = clip(getattr(comp, "id", None), 120)
                parts.append(
                    {
                        "type": "reply",
                        "message_id": reply_to,
                        "sender_name": clip(
                            getattr(comp, "sender_nickname", None), 120
                        ),
                        "text": clip(getattr(comp, "message_str", None), 500),
                    }
                )
            elif ctype == "Json":
                parts.append(
                    {
                        "type": "json",
                        "preview": clip(json_dumps(getattr(comp, "data", None)), 1000),
                    }
                )
            elif ctype == "Face":
                parts.append({"type": "face", "id": clip(getattr(comp, "id", None), 80)})
            else:
                parts.append({"type": ctype})
        message_text = str(getattr(message_obj, "message_str", "") or "")
        if not message_text:
            message_text = "".join(text_chunks)
        ts = getattr(message_obj, "timestamp", None)
        try:
            ts = float(ts) if ts else _now()
        except (TypeError, ValueError):
            ts = _now()
        sender = getattr(message_obj, "sender", None)
        row = {
            "scope_id": scope["scope_id"],
            "source": "live",
            "native_id": native_id,
            "platform_message_id": message_id or None,
            "direction": "in",
            "sender_id": clip(getattr(sender, "user_id", None), 120),
            "sender_name": clip(getattr(sender, "nickname", None), 200),
            "ts": ts,
            "text": clip(message_text, max_chars),
            "content_json": json_dumps(parts),
            "component_types": json_dumps(sorted(component_types)),
            "reply_to_message_id": reply_to,
            "status": "received",
            "status_evidence": "live_event",
            "trace_id": trace_id,
            "llm_checkpoint_id": event.get_extra("llm_checkpoint_id")
            if hasattr(event, "get_extra")
            else None,
        }
        self._emit({"type": "message", "row": row})
        self._emit({"type": "metric_inc", "key": "live_messages", "amount": 1})

    def record_outgoing(self, event: Any) -> None:
        if not self.enabled() or not self.enabled("record_outgoing_messages"):
            return
        try:
            result = event.get_result()
        except Exception:
            result = None
        if result is None:
            return
        chain = list(getattr(result, "chain", None) or [])
        if not chain:
            return
        scope = self._scope_from_event(event)
        if scope is None:
            return
        texts: list[str] = []
        parts: list[dict] = []
        component_types: set[str] = set()
        for comp in chain[:120]:
            ctype = str(getattr(comp, "type", "") or "unknown")
            ctype = getattr(ctype, "value", ctype)
            component_types.add(ctype)
            if ctype == "Plain":
                text = str(getattr(comp, "text", "") or "")
                texts.append(text)
                parts.append({"type": "plain", "text": clip(text, 8000)})
            elif ctype in ("Image", "Record", "Video", "File"):
                parts.append(
                    {
                        "type": ctype.lower(),
                        "url": safe_url(
                            getattr(comp, "url", None) or getattr(comp, "file", None)
                        ),
                        "file_present": bool(getattr(comp, "file", None)),
                    }
                )
            else:
                parts.append({"type": ctype})
        text = "".join(texts).strip()
        if not text and not parts:
            return
        trace_id = None
        try:
            trace_id = event.get_extra("_observatory_trace_id")
        except Exception:
            trace_id = None
        self._emit({"type": "scope_upsert", "scope": scope})
        self._emit(
            {
                "type": "message",
                "row": {
                    "scope_id": scope["scope_id"],
                    "source": "live_sent",
                    "native_id": f"sent:{uuid.uuid4().hex}",
                    "platform_message_id": None,
                    "direction": "out",
                    "sender_id": "bot",
                    "sender_name": "bot",
                    "ts": _now(),
                    "text": clip(text, 8000),
                    "content_json": json_dumps(parts),
                    "component_types": json_dumps(sorted(component_types)),
                    "status": "sent",
                    "status_evidence": "after_message_sent_hook",
                    "trace_id": trace_id,
                },
            }
        )
        self._emit({"type": "metric_inc", "key": "live_outgoing", "amount": 1})

    # ------------------------------------------------------------------ misc
    def _expires_at(self, retention_key: str) -> float:
        days = float(self.settings.get(retention_key) or 7)
        return _now() + max(1.0, days) * 86400

    def _trace_stats(self) -> dict:
        return {
            "queue_depth": self.queue.qsize() if self.queue is not None else 0,
            "queue_max": QUEUE_MAX,
            "dropped_since_flush": self._dropped,
            "closing": self._closing,
            "started": self._started,
        }
