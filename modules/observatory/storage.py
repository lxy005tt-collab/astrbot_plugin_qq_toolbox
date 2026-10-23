"""SQLite + gzip body storage for the observatory plugin.

Only this module touches the plugin's own database and blob files. Native
AstrBot tables are read elsewhere (native_reader.py) with a read-only connection.
"""

from __future__ import annotations

import asyncio
import gzip
import os
import shutil
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable

from .util import (
    clip,
    json_dumps,
    json_loads,
    normalize_text,
    parse_umo,
    scope_id_for,
    utc_now,
)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS setting (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_scope (
    scope_id TEXT PRIMARY KEY,
    platform_id TEXT NOT NULL,
    bot_self_id TEXT NOT NULL,
    message_type TEXT NOT NULL,
    peer_id TEXT NOT NULL,
    umo TEXT NOT NULL,
    display_name TEXT,
    alias TEXT,
    is_group INTEGER NOT NULL,
    first_seen REAL,
    last_seen REAL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_id TEXT NOT NULL,
    source TEXT NOT NULL,
    native_id TEXT NOT NULL,
    platform_message_id TEXT,
    direction TEXT NOT NULL,
    sender_id TEXT,
    sender_name TEXT,
    ts REAL NOT NULL,
    text TEXT,
    content_json TEXT,
    component_types TEXT,
    reply_to_message_id TEXT,
    status TEXT,
    status_evidence TEXT,
    trace_id TEXT,
    llm_checkpoint_id TEXT,
    imported_batch TEXT,
    duplicate_of TEXT,
    created_at REAL NOT NULL,
    UNIQUE(source, native_id)
);
CREATE INDEX IF NOT EXISTS ix_chat_message_scope_ts
    ON chat_message(scope_id, ts DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_chat_message_ts ON chat_message(ts);
CREATE TABLE IF NOT EXISTS reply_trace (
    trace_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    umo TEXT NOT NULL,
    conversation_id TEXT,
    session_id TEXT,
    persona_id TEXT,
    trigger_message_id TEXT,
    merged_count INTEGER,
    engine TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    status TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    tool_call_count INTEGER NOT NULL DEFAULT 0,
    usage_input_other INTEGER,
    usage_input_cached INTEGER,
    usage_output INTEGER,
    usage_complete INTEGER NOT NULL DEFAULT 0,
    final_request_id TEXT,
    final_preview TEXT,
    final_text_hash TEXT,
    error TEXT,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reply_trace_started ON reply_trace(started_at DESC);
CREATE INDEX IF NOT EXISTS ix_reply_trace_scope ON reply_trace(scope_id, started_at DESC);
CREATE TABLE IF NOT EXISTS llm_request (
    request_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    step_no INTEGER NOT NULL DEFAULT 1,
    attempt_no INTEGER NOT NULL DEFAULT 1,
    provider_id TEXT,
    provider_model TEXT,
    requested_model TEXT,
    is_fallback INTEGER NOT NULL DEFAULT 0,
    streaming INTEGER NOT NULL DEFAULT 0,
    capture_stage TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    status TEXT NOT NULL,
    error TEXT,
    input_msg_count INTEGER,
    input_est_tokens INTEGER,
    input_blob_id TEXT,
    input_blob_state TEXT NOT NULL DEFAULT 'pending',
    input_sections_json TEXT,
    tool_names_json TEXT,
    tool_count INTEGER,
    tool_schema_est_tokens INTEGER,
    context_budget INTEGER,
    context_trigger INTEGER,
    compression_strategy TEXT,
    keep_recent_ratio REAL,
    threshold REAL,
    system_prompt_chars INTEGER,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_llm_request_started ON llm_request(started_at DESC);
CREATE INDEX IF NOT EXISTS ix_llm_request_trace ON llm_request(trace_id, step_no, attempt_no);
CREATE INDEX IF NOT EXISTS ix_llm_request_scope ON llm_request(scope_id, started_at DESC);
CREATE TABLE IF NOT EXISTS llm_result (
    result_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    response_id TEXT,
    role TEXT,
    reply_blob_id TEXT,
    reply_state TEXT NOT NULL DEFAULT 'missing',
    reply_chars INTEGER,
    reply_preview TEXT,
    reasoning_blob_id TEXT,
    reasoning_state TEXT NOT NULL DEFAULT 'missing',
    reasoning_chars INTEGER,
    reasoning_status TEXT NOT NULL DEFAULT 'not_provided',
    reasoning_source TEXT,
    signature_present INTEGER NOT NULL DEFAULT 0,
    tool_calls_json TEXT,
    usage_source TEXT NOT NULL DEFAULT 'unknown',
    usage_input_other INTEGER,
    usage_input_cached INTEGER,
    usage_output INTEGER,
    usage_missing_reason TEXT,
    latency_ms INTEGER,
    ttft_ms INTEGER,
    finish_reason TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_llm_result_usage ON llm_result(usage_source);
CREATE TABLE IF NOT EXISTS compression_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT,
    scope_id TEXT,
    umo TEXT,
    conversation_id TEXT,
    strategy TEXT,
    threshold REAL,
    trigger_budget INTEGER,
    before_tokens INTEGER,
    after_tokens INTEGER,
    before_messages INTEGER,
    after_messages INTEGER,
    summary_blob_id TEXT,
    summary_chars INTEGER,
    token_source TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_compression_trace ON compression_event(trace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_compression_scope ON compression_event(scope_id, created_at DESC);
CREATE TABLE IF NOT EXISTS native_reply_summary (
    native_id INTEGER PRIMARY KEY,
    umo TEXT NOT NULL,
    conversation_id TEXT,
    provider_id TEXT,
    provider_model TEXT,
    status TEXT,
    input_other INTEGER,
    input_cached INTEGER,
    output INTEGER,
    start_time REAL,
    end_time REAL,
    ttft REAL,
    agent_type TEXT,
    imported_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_native_summary_time ON native_reply_summary(start_time DESC);
CREATE TABLE IF NOT EXISTS price_version (
    price_version_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    model TEXT NOT NULL,
    tier_label TEXT,
    min_input_tokens INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'per_million_tokens',
    billing_mode TEXT NOT NULL DEFAULT 'per_token',
    input_price TEXT,
    cached_input_price TEXT,
    output_price TEXT,
    effective_at REAL NOT NULL,
    created_at REAL NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    note TEXT
);
CREATE INDEX IF NOT EXISTS ix_price_active
    ON price_version(provider_id, model, is_active, min_input_tokens);
CREATE TABLE IF NOT EXISTS cost_estimate (
    request_id TEXT PRIMARY KEY,
    result_id TEXT,
    usage_input_other INTEGER,
    usage_input_cached INTEGER,
    usage_output INTEGER,
    price_version_id TEXT,
    amount TEXT,
    currency TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    computed_at REAL NOT NULL,
    price_snapshot_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_cost_status ON cost_estimate(status, computed_at DESC);
CREATE TABLE IF NOT EXISTS blob (
    blob_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    raw_bytes INTEGER NOT NULL,
    compression TEXT NOT NULL DEFAULT 'gzip',
    created_at REAL NOT NULL,
    expires_at REAL,
    state TEXT NOT NULL DEFAULT 'available',
    note TEXT
);
CREATE INDEX IF NOT EXISTS ix_blob_expiry ON blob(state, expires_at);
CREATE INDEX IF NOT EXISTS ix_blob_created ON blob(created_at);
CREATE TABLE IF NOT EXISTS import_checkpoint (
    name TEXT PRIMARY KEY,
    cursor TEXT,
    batch TEXT,
    coverage_json TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS collector_metric (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS capture_gap (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_capture_gap_ts ON capture_gap(ts DESC);
"""


class Storage:
    def __init__(
        self,
        data_dir: Path,
        defaults: dict[str, Any],
        logger: Any = None,
        cost_resolver: Callable[..., Any] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "observatory.db"
        self.blobs_dir = self.data_dir / "blobs"
        self.defaults = dict(defaults or {})
        self.logger = logger
        self.cost_resolver = cost_resolver
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._closed = False
        self._last_write_at = 0.0
        self._last_error: str | None = None

    # ------------------------------------------------------------------ setup
    def open(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=10.0,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=8000")
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.executescript(SCHEMA)
        self._conn = conn
        version = self.get_meta("schema_version")
        if version is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        elif str(version) != str(SCHEMA_VERSION):
            # v1 has no migrations; keep the data and record the mismatch.
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        if self.get_meta("installed_at") is None:
            self.set_meta("installed_at", str(utc_now()))
            self.set_meta("live_started_at", str(utc_now()))

    def close(self) -> None:
        self._closed = True
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    pass
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("observatory storage is not open")
        return self._conn

    # --------------------------------------------------------------- meta/set
    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM setting WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return self.defaults.get(key, default)
        return json_loads(row["value"], default)

    def get_settings(self) -> dict[str, Any]:
        merged = dict(self.defaults)
        with self._lock:
            rows = self.conn.execute("SELECT key, value FROM setting").fetchall()
        for row in rows:
            merged[row["key"]] = json_loads(row["value"], merged.get(row["key"]))
        return merged

    def save_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self._lock:
            for key, value in values.items():
                self.conn.execute(
                    "INSERT INTO setting(key, value, updated_at) VALUES(?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    (key, json_dumps(value), now),
                )
        return self.get_settings()

    def inc_metric(self, key: str, amount: float = 1.0) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO collector_metric(key, value, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=collector_metric.value + "
                "excluded.value, updated_at=excluded.updated_at",
                (key, float(amount), utc_now()),
            )

    def get_metrics(self) -> dict[str, float]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT key, value FROM collector_metric"
            ).fetchall()
        return {row["key"]: float(row["value"] or 0) for row in rows}

    # ------------------------------------------------------------- async read
    async def fetch_all(self, sql: str, params: tuple | list = ()) -> list[dict]:
        def run() -> list[dict]:
            with self._lock:
                rows = self.conn.execute(sql, tuple(params)).fetchall()
            return [dict(row) for row in rows]

        return await asyncio.to_thread(run)

    async def fetch_one(self, sql: str, params: tuple | list = ()) -> dict | None:
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None

    async def read_blob_text(self, blob_id: str | None) -> str | None:
        if not blob_id:
            return None

        def run() -> str | None:
            with self._lock:
                row = self.conn.execute(
                    "SELECT path, state, expires_at FROM blob WHERE blob_id=?",
                    (blob_id,),
                ).fetchone()
            if row is None or row["state"] != "available":
                return None
            expires_at = row["expires_at"]
            if expires_at and float(expires_at) <= utc_now():
                return None
            path = Path(row["path"])
            try:
                path.resolve().relative_to(self.blobs_dir.resolve())
            except (ValueError, OSError):
                return None
            if not path.is_file():
                return None
            try:
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    return fh.read()
            except (OSError, EOFError):
                return None

        return await asyncio.to_thread(run)

    # --------------------------------------------------------------- writing
    async def write_events(self, events: list[dict]) -> None:
        if not events:
            return
        await asyncio.to_thread(self._apply_batch, events)

    def _apply_batch(self, events: list[dict]) -> None:
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                for event in events:
                    try:
                        self._apply_event(event)
                    except sqlite3.Error as exc:
                        self._last_error = f"{type(exc).__name__}: {exc}"
                        if self.logger:
                            self.logger.warning(
                                "observatory write failed (%s): %s",
                                event.get("type"),
                                exc,
                            )
                self.conn.execute("COMMIT")
                self._last_write_at = utc_now()
            except sqlite3.Error as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                try:
                    self.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                if self.logger:
                    self.logger.warning("observatory batch write failed: %s", exc)

    def _apply_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "scope_upsert":
            self._upsert_scope(event)
        elif kind == "scope_alias":
            self._update_scope_alias(event)
        elif kind == "message":
            self._insert_message(event)
        elif kind == "message_trace":
            self.conn.execute(
                "UPDATE chat_message SET trace_id=? WHERE scope_id=? "
                "AND platform_message_id=? AND trace_id IS NULL",
                (
                    event.get("trace_id"),
                    event.get("scope_id"),
                    event.get("platform_message_id"),
                ),
            )
        elif kind == "trace_start":
            self._insert_trace(event)
        elif kind == "trace_close":
            self._close_trace(event)
        elif kind == "request_start":
            self._insert_request(event)
        elif kind == "request_finish":
            self._finish_request(event)
        elif kind == "compression":
            self._insert_compression(event)
        elif kind == "native_summary":
            self._insert_native_summary(event)
        elif kind == "checkpoint":
            self._upsert_checkpoint(event)
        elif kind == "metric_inc":
            self.inc_metric_in_tx(event.get("key", "unknown"), event.get("amount", 1))
        elif kind == "gap":
            self.conn.execute(
                "INSERT INTO capture_gap(ts, kind, detail) VALUES(?, ?, ?)",
                (utc_now(), event.get("kind", "unknown"), clip(event.get("detail"), 2000)),
            )

    def inc_metric_in_tx(self, key: str, amount: float = 1.0) -> None:
        self.conn.execute(
            "INSERT INTO collector_metric(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=collector_metric.value + "
            "excluded.value, updated_at=excluded.updated_at",
            (key, float(amount), utc_now()),
        )

    def _upsert_scope(self, event: dict) -> None:
        scope = event["scope"]
        now = utc_now()
        self.conn.execute(
            "INSERT INTO chat_scope(scope_id, platform_id, bot_self_id, message_type, "
            "peer_id, umo, display_name, alias, is_group, first_seen, last_seen, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope_id) DO UPDATE SET "
            "display_name=CASE WHEN COALESCE(excluded.display_name,'')<>'' "
            "THEN excluded.display_name ELSE chat_scope.display_name END, "
            "alias=CASE WHEN COALESCE(excluded.alias,'')<>'' "
            "THEN excluded.alias ELSE chat_scope.alias END, "
            "is_group=excluded.is_group, "
            "last_seen=MAX(COALESCE(chat_scope.last_seen,0), COALESCE(excluded.last_seen,0)), "
            "updated_at=excluded.updated_at",
            (
                scope["scope_id"],
                scope.get("platform_id", ""),
                scope.get("bot_self_id", ""),
                scope.get("message_type", ""),
                scope.get("peer_id", ""),
                scope.get("umo", ""),
                scope.get("display_name"),
                scope.get("alias"),
                1 if scope.get("is_group") else 0,
                scope.get("first_seen") or now,
                scope.get("last_seen") or now,
                now,
            ),
        )

    def _insert_message(self, event: dict) -> None:
        row = event["row"]
        if row.get("dedupe_out") and row.get("direction") == "out":
            ts = float(row.get("ts") or utc_now())
            candidates = self.conn.execute(
                "SELECT text FROM chat_message WHERE scope_id=? AND direction='out' "
                "AND ts BETWEEN ? AND ?",
                (row["scope_id"], ts - 30, ts + 30),
            ).fetchall()
            incoming = normalize_text(row.get("text") or "")
            for candidate in candidates:
                existing = normalize_text(candidate["text"] or "")
                sample = min(len(incoming), len(existing), 80)
                if sample and incoming[:sample] == existing[:sample]:
                    return
        self.conn.execute(
            "INSERT OR IGNORE INTO chat_message(scope_id, source, native_id, "
            "platform_message_id, direction, sender_id, sender_name, ts, text, "
            "content_json, component_types, reply_to_message_id, status, "
            "status_evidence, trace_id, llm_checkpoint_id, imported_batch, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["scope_id"],
                row["source"],
                row["native_id"],
                row.get("platform_message_id"),
                row.get("direction", "in"),
                row.get("sender_id"),
                row.get("sender_name"),
                float(row.get("ts") or utc_now()),
                row.get("text"),
                row.get("content_json"),
                row.get("component_types"),
                row.get("reply_to_message_id"),
                row.get("status"),
                row.get("status_evidence"),
                row.get("trace_id"),
                row.get("llm_checkpoint_id"),
                row.get("imported_batch"),
                utc_now(),
            ),
        )

    def _update_scope_alias(self, event: dict) -> None:
        umo = str(event.get("umo") or "").strip()
        alias = str(event.get("alias") or "").strip()
        if not umo or not alias:
            return
        now = utc_now()
        cur = self.conn.execute(
            "UPDATE chat_scope SET alias=?, "
            "display_name=CASE WHEN COALESCE(display_name,'')='' THEN ? "
            "ELSE display_name END, updated_at=? WHERE umo=?",
            (clip(alias, 200), clip(alias, 200), now, umo),
        )
        if cur.rowcount:
            return
        platform_id, message_type, peer_id = parse_umo(umo)
        if not platform_id or not peer_id:
            return
        self.conn.execute(
            "INSERT OR IGNORE INTO chat_scope(scope_id, platform_id, bot_self_id, "
            "message_type, peer_id, umo, display_name, alias, is_group, first_seen, "
            "last_seen, updated_at) VALUES(?, ?, '', ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
            (
                scope_id_for(platform_id, message_type, peer_id),
                platform_id,
                message_type,
                peer_id,
                umo,
                clip(alias, 200),
                clip(alias, 200),
                1 if "GroupMessage" in message_type else 0,
                now,
            ),
        )

    def _insert_trace(self, event: dict) -> None:
        row = event["row"]
        self.conn.execute(
            "INSERT OR REPLACE INTO reply_trace(trace_id, scope_id, umo, "
            "conversation_id, session_id, persona_id, trigger_message_id, "
            "merged_count, engine, started_at, ended_at, status, request_count, "
            "tool_call_count, usage_input_other, usage_input_cached, usage_output, "
            "usage_complete, final_request_id, final_preview, final_text_hash, "
            "error, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, "
            "NULL, NULL, NULL, 0, NULL, NULL, NULL, NULL, ?)",
            (
                row["trace_id"],
                row["scope_id"],
                row["umo"],
                row.get("conversation_id"),
                row.get("session_id"),
                row.get("persona_id"),
                row.get("trigger_message_id"),
                row.get("merged_count"),
                row.get("engine"),
                float(row["started_at"]),
                row.get("ended_at"),
                row.get("status", "running"),
                utc_now(),
            ),
        )

    def _close_trace(self, event: dict) -> None:
        self.conn.execute(
            "UPDATE reply_trace SET status=?, ended_at=?, final_request_id=?, "
            "final_preview=?, final_text_hash=?, error=?, updated_at=? "
            "WHERE trace_id=?",
            (
                event.get("status", "completed"),
                event.get("ended_at") or utc_now(),
                event.get("final_request_id"),
                clip(event.get("final_text"), 600),
                event.get("final_text_hash"),
                clip(event.get("error"), 2000),
                utc_now(),
                event["trace_id"],
            ),
        )

    def _insert_request(self, event: dict) -> None:
        row = event["row"]
        sections = json_dumps(row.get("sections") or [])
        tool_names = json_dumps(row.get("tool_names") or [])
        input_blob_id = row.get("input_blob_id")
        input_state = "missing"
        if input_blob_id:
            input_state = self._write_blob(
                input_blob_id,
                "request_input",
                event.get("input_text") or "",
                event.get("input_expires_at"),
                note=f"trace={row.get('trace_id')}",
            )
        self.conn.execute(
            "INSERT OR REPLACE INTO llm_request(request_id, trace_id, scope_id, "
            "step_no, attempt_no, provider_id, provider_model, requested_model, "
            "is_fallback, streaming, capture_stage, started_at, status, "
            "input_msg_count, input_est_tokens, input_blob_id, input_blob_state, "
            "input_sections_json, tool_names_json, tool_count, "
            "tool_schema_est_tokens, context_budget, context_trigger, "
            "compression_strategy, keep_recent_ratio, threshold, "
            "system_prompt_chars, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?)",
            (
                row["request_id"],
                row["trace_id"],
                row["scope_id"],
                int(row.get("step_no") or 1),
                int(row.get("attempt_no") or 1),
                row.get("provider_id"),
                row.get("provider_model"),
                row.get("requested_model"),
                1 if row.get("is_fallback") else 0,
                1 if row.get("streaming") else 0,
                row.get("capture_stage") or "unknown",
                float(row["started_at"]),
                row.get("status") or "running",
                row.get("input_msg_count"),
                row.get("input_est_tokens"),
                input_blob_id,
                input_state,
                sections,
                tool_names,
                len(row.get("tool_names") or []),
                row.get("tool_schema_est_tokens"),
                row.get("context_budget"),
                row.get("context_trigger"),
                row.get("compression_strategy"),
                row.get("keep_recent_ratio"),
                row.get("threshold"),
                row.get("system_prompt_chars"),
                utc_now(),
            ),
        )

    def _finish_request(self, event: dict) -> None:
        request_id = event["request_id"]
        trace_id = event["trace_id"]
        result = event["result"]
        usage = result.get("usage") or {}
        now = utc_now()
        self.conn.execute(
            "UPDATE llm_request SET ended_at=?, status=?, error=?, "
            "input_blob_state=CASE WHEN input_blob_state='pending' THEN 'missing' "
            "ELSE input_blob_state END WHERE request_id=?",
            (now, result.get("status", "completed"), clip(result.get("error"), 2000), request_id),
        )
        reply_blob_id = result.get("reply_blob_id")
        reply_state = "missing"
        if reply_blob_id:
            reply_state = self._write_blob(
                reply_blob_id,
                "reply",
                result.get("reply_text") or "",
                event.get("reply_expires_at"),
                note=f"request={request_id}",
            )
        reasoning_blob_id = result.get("reasoning_blob_id")
        reasoning_state = "missing"
        if reasoning_blob_id:
            reasoning_state = self._write_blob(
                reasoning_blob_id,
                "reasoning",
                result.get("reasoning_text") or "",
                event.get("reasoning_expires_at"),
                note=f"request={request_id}",
            )
            if reasoning_state != "available":
                result["reasoning_status"] = "expired"
        result_id = result["result_id"]
        self.conn.execute(
            "INSERT OR REPLACE INTO llm_result(result_id, request_id, response_id, "
            "role, reply_blob_id, reply_state, reply_chars, reply_preview, "
            "reasoning_blob_id, reasoning_state, reasoning_chars, reasoning_status, "
            "reasoning_source, signature_present, tool_calls_json, usage_source, "
            "usage_input_other, usage_input_cached, usage_output, "
            "usage_missing_reason, latency_ms, ttft_ms, finish_reason, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result_id,
                request_id,
                result.get("response_id"),
                result.get("role"),
                reply_blob_id,
                reply_state,
                len(result.get("reply_text") or ""),
                clip(result.get("reply_text"), 600),
                reasoning_blob_id,
                reasoning_state,
                len(result.get("reasoning_text") or ""),
                result.get("reasoning_status") or "not_provided",
                result.get("reasoning_source"),
                1 if result.get("signature_present") else 0,
                json_dumps(result.get("tool_calls") or []),
                result.get("usage_source") or "unknown",
                usage.get("input_other"),
                usage.get("input_cached"),
                usage.get("output"),
                result.get("usage_missing_reason"),
                result.get("latency_ms"),
                result.get("ttft_ms"),
                result.get("finish_reason"),
                now,
            ),
        )
        # Aggregate into the trace. Usage is stored only when reported by the API.
        self.conn.execute(
            "UPDATE reply_trace SET request_count=request_count+1, "
            "tool_call_count=tool_call_count+?, "
            "usage_input_other=COALESCE(usage_input_other,0)+?, "
            "usage_input_cached=COALESCE(usage_input_cached,0)+?, "
            "usage_output=COALESCE(usage_output,0)+?, "
            "usage_complete=usage_complete+?, updated_at=? WHERE trace_id=?",
            (
                len(result.get("tool_calls") or []),
                usage.get("input_other") or 0,
                usage.get("input_cached") or 0,
                usage.get("output") or 0,
                1 if result.get("usage_source") == "api_reported" else 0,
                now,
                trace_id,
            ),
        )
        if self.cost_resolver is not None:
            try:
                cost = self.cost_resolver(
                    conn=self.conn,
                    request_id=request_id,
                    result_id=result_id,
                    provider_id=event.get("provider_id") or "",
                    provider_model=event.get("provider_model") or "",
                    usage_source=result.get("usage_source"),
                    input_other=usage.get("input_other"),
                    input_cached=usage.get("input_cached"),
                    output=usage.get("output"),
                    ts=now,
                )
            except Exception as exc:  # never let estimation break capture
                if self.logger:
                    self.logger.debug("cost estimation failed: %s", exc)
                cost = None
            if cost:
                self.conn.execute(
                    "INSERT OR REPLACE INTO cost_estimate(request_id, result_id, "
                    "usage_input_other, usage_input_cached, usage_output, "
                    "price_version_id, amount, currency, status, reason, "
                    "computed_at, price_snapshot_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        request_id,
                        result_id,
                        usage.get("input_other"),
                        usage.get("input_cached"),
                        usage.get("output"),
                        cost.get("price_version_id"),
                        cost.get("amount"),
                        cost.get("currency"),
                        cost.get("status"),
                        clip(cost.get("reason"), 1000),
                        now,
                        json_dumps(cost.get("snapshot") or {}),
                    ),
                )

    def _insert_compression(self, event: dict) -> None:
        row = event["row"]
        summary_blob_id = row.get("summary_blob_id")
        if summary_blob_id:
            self._write_blob(
                summary_blob_id,
                "compression_summary",
                row.get("summary_text") or "",
                event.get("summary_expires_at"),
                note=f"trace={row.get('trace_id')}",
            )
        self.conn.execute(
            "INSERT INTO compression_event(trace_id, scope_id, umo, conversation_id, "
            "strategy, threshold, trigger_budget, before_tokens, after_tokens, "
            "before_messages, after_messages, summary_blob_id, summary_chars, "
            "token_source, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.get("trace_id"),
                row.get("scope_id"),
                row.get("umo"),
                row.get("conversation_id"),
                row.get("strategy"),
                row.get("threshold"),
                row.get("trigger_budget"),
                row.get("before_tokens"),
                row.get("after_tokens"),
                row.get("before_messages"),
                row.get("after_messages"),
                summary_blob_id,
                len(row.get("summary_text") or ""),
                row.get("token_source"),
                row.get("created_at") or utc_now(),
            ),
        )

    def _insert_native_summary(self, event: dict) -> None:
        row = event["row"]
        self.conn.execute(
            "INSERT OR IGNORE INTO native_reply_summary(native_id, umo, "
            "conversation_id, provider_id, provider_model, status, input_other, "
            "input_cached, output, start_time, end_time, ttft, agent_type, imported_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["native_id"],
                row["umo"],
                row.get("conversation_id"),
                row.get("provider_id"),
                row.get("provider_model"),
                row.get("status"),
                row.get("input_other"),
                row.get("input_cached"),
                row.get("output"),
                row.get("start_time"),
                row.get("end_time"),
                row.get("ttft"),
                row.get("agent_type"),
                utc_now(),
            ),
        )

    def _upsert_checkpoint(self, event: dict) -> None:
        self.conn.execute(
            "INSERT INTO import_checkpoint(name, cursor, batch, coverage_json, updated_at) "
            "VALUES(?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
            "cursor=excluded.cursor, batch=excluded.batch, "
            "coverage_json=excluded.coverage_json, updated_at=excluded.updated_at",
            (
                event["name"],
                str(event.get("cursor") or ""),
                event.get("batch"),
                json_dumps(event.get("coverage") or {}),
                utc_now(),
            ),
        )

    # ------------------------------------------------------------------ blobs
    def _write_blob(
        self,
        blob_id: str,
        kind: str,
        text: str,
        expires_at: float | None,
        note: str = "",
    ) -> str:
        try:
            raw = text.encode("utf-8", "replace")
            compressed = gzip.compress(raw, compresslevel=6)
            subdir = self.blobs_dir / kind
            subdir.mkdir(parents=True, exist_ok=True)
            path = subdir / f"{blob_id}.json.gz"
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(compressed)
            os.replace(tmp, path)
            self.conn.execute(
                "INSERT OR REPLACE INTO blob(blob_id, kind, path, size_bytes, "
                "raw_bytes, compression, created_at, expires_at, state, note) "
                "VALUES(?, ?, ?, ?, ?, 'gzip', ?, ?, 'available', ?)",
                (
                    blob_id,
                    kind,
                    str(path),
                    len(compressed),
                    len(raw),
                    utc_now(),
                    expires_at,
                    clip(note, 200),
                ),
            )
            return "available"
        except OSError as exc:
            self._last_error = f"blob write failed: {exc}"
            if self.logger:
                self.logger.warning("observatory blob write failed: %s", exc)
            return "missing"

    # ----------------------------------------------------------- maintenance
    def maintenance(self, settings: dict[str, Any], now: float | None = None) -> dict:
        """Expire bodies, enforce retention, and trim to the data budget.

        Never deletes anything outside the plugin data directory. Returns
        counters for the health endpoint.
        """
        now = now or utc_now()
        stats = {
            "expired_bodies": 0,
            "trimmed_for_budget": 0,
            "deleted_messages": 0,
            "deleted_requests": 0,
            "freed_bytes": 0,
        }
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                stats.update(self._expire_due_blobs(now))
                stats["trimmed_for_budget"] = self._enforce_budget(settings, now)
                stats["deleted_messages"] = self._delete_old_messages(settings, now)
                stats["deleted_requests"] = self._delete_old_requests(settings, now)
                self.conn.execute("COMMIT")
                self.conn.execute("PRAGMA incremental_vacuum(200)")
            except sqlite3.Error as exc:
                self._last_error = f"maintenance failed: {exc}"
                try:
                    self.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                if self.logger:
                    self.logger.warning("observatory maintenance failed: %s", exc)
        return stats

    def _expire_due_blobs(self, now: float) -> dict:
        rows = self.conn.execute(
            "SELECT blob_id, kind, path, size_bytes FROM blob "
            "WHERE state='available' AND expires_at IS NOT NULL AND expires_at<=?",
            (now,),
        ).fetchall()
        freed = 0
        for row in rows:
            freed += self._expire_blob_row(row, "expired")
        return {"expired_bodies": len(rows), "freed_bytes": freed}

    def _expire_blob_row(self, row: sqlite3.Row, reason: str) -> int:
        path = Path(row["path"])
        try:
            path.resolve().relative_to(self.blobs_dir.resolve())
            if path.is_file():
                size = path.stat().st_size
                path.unlink()
            else:
                size = 0
        except (OSError, ValueError):
            size = 0
        self.conn.execute(
            "UPDATE blob SET state=?, note=COALESCE(note,'') || ? WHERE blob_id=?",
            ("expired", f" [{reason}]", row["blob_id"]),
        )
        kind = row["kind"]
        if kind == "request_input":
            self.conn.execute(
                "UPDATE llm_request SET input_blob_state='expired' WHERE input_blob_id=?",
                (row["blob_id"],),
            )
        elif kind == "reasoning":
            self.conn.execute(
                "UPDATE llm_result SET reasoning_state='expired', "
                "reasoning_status=CASE WHEN reasoning_status='available' "
                "THEN 'expired' ELSE reasoning_status END WHERE reasoning_blob_id=?",
                (row["blob_id"],),
            )
        elif kind == "reply":
            self.conn.execute(
                "UPDATE llm_result SET reply_state='expired' WHERE reply_blob_id=?",
                (row["blob_id"],),
            )
        return size

    def _enforce_budget(self, settings: dict[str, Any], now: float) -> int:
        budget_gb = float(settings.get("data_budget_gb") or 2.0)
        budget_bytes = max(64 * 1024 * 1024, int(budget_gb * 1024**3))
        row = self.conn.execute(
            "SELECT COALESCE(SUM(size_bytes),0) AS total FROM blob WHERE state='available'"
        ).fetchone()
        total = int(row["total"] or 0)
        if total <= budget_bytes:
            return 0
        target = int(budget_bytes * 0.9)
        trimmed = 0
        rows = self.conn.execute(
            "SELECT blob_id, kind, path, size_bytes FROM blob "
            "WHERE state='available' ORDER BY created_at ASC LIMIT 5000"
        ).fetchall()
        for blob in rows:
            if total <= target:
                break
            total -= self._expire_blob_row(blob, "data_budget")
            trimmed += 1
        return trimmed

    def _delete_old_messages(self, settings: dict[str, Any], now: float) -> int:
        days = float(settings.get("retention_chat_days") or 30)
        cutoff = now - days * 86400
        cur = self.conn.execute("DELETE FROM chat_message WHERE ts < ?", (cutoff,))
        return max(0, cur.rowcount or 0)

    def _delete_old_requests(self, settings: dict[str, Any], now: float) -> int:
        days = float(settings.get("retention_usage_days") or 180)
        cutoff = now - days * 86400
        old_ids = [
            row["request_id"]
            for row in self.conn.execute(
                "SELECT request_id FROM llm_request WHERE started_at < ? LIMIT 5000",
                (cutoff,),
            ).fetchall()
        ]
        if not old_ids:
            return 0
        placeholders = ",".join("?" for _ in old_ids)
        # Bodies first so the blob rows and files are removed together.
        blob_ids = [
            row["blob_id"]
            for row in self.conn.execute(
                f"SELECT input_blob_id AS blob_id FROM llm_request WHERE request_id IN ({placeholders}) "
                f"UNION SELECT reply_blob_id FROM llm_result WHERE request_id IN ({placeholders}) "
                f"UNION SELECT reasoning_blob_id FROM llm_result WHERE request_id IN ({placeholders})",
                tuple(old_ids) * 3,
            ).fetchall()
        ]
        for blob_id in blob_ids:
            if not blob_id:
                continue
            row = self.conn.execute(
                "SELECT blob_id, kind, path, size_bytes FROM blob WHERE blob_id=?",
                (blob_id,),
            ).fetchone()
            if row:
                self._expire_blob_row(row, "retention")
                self.conn.execute("DELETE FROM blob WHERE blob_id=?", (blob_id,))
        self.conn.execute(
            f"DELETE FROM llm_result WHERE request_id IN ({placeholders})",
            tuple(old_ids),
        )
        self.conn.execute(
            f"DELETE FROM cost_estimate WHERE request_id IN ({placeholders})",
            tuple(old_ids),
        )
        self.conn.execute(
            f"DELETE FROM llm_request WHERE request_id IN ({placeholders})",
            tuple(old_ids),
        )
        # Traces with no requests left are removed as well.
        self.conn.execute(
            "DELETE FROM reply_trace WHERE started_at < ? AND trace_id NOT IN "
            "(SELECT DISTINCT trace_id FROM llm_request)",
            (cutoff,),
        )
        self.conn.execute(
            "DELETE FROM compression_event WHERE created_at < ?", (cutoff,)
        )
        self.conn.execute(
            "DELETE FROM native_reply_summary WHERE start_time < ?", (cutoff,)
        )
        self.conn.execute("DELETE FROM capture_gap WHERE ts < ?", (cutoff,))
        return len(old_ids)

    # ------------------------------------------------------------------ stats
    def disk_stats(self) -> dict:
        with self._lock:
            blob_row = self.conn.execute(
                "SELECT COALESCE(SUM(size_bytes),0) AS total, COUNT(*) AS count "
                "FROM blob WHERE state='available'"
            ).fetchone()
            db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
            expired = self.conn.execute(
                "SELECT COUNT(*) AS c FROM blob WHERE state='expired'"
            ).fetchone()
        return {
            "db_bytes": db_size,
            "body_bytes": int(blob_row["total"] or 0),
            "body_count": int(blob_row["count"] or 0),
            "expired_body_count": int(expired["c"] or 0),
            "last_write_at": self._last_write_at,
            "last_error": self._last_error,
        }

    def backup_copy(self) -> str | None:
        """Optional maintenance helper; not used by v1 endpoints."""
        if not self.db_path.exists():
            return None
        target = self.data_dir / "observatory.db.bak"
        shutil.copy2(self.db_path, target)
        return str(target)

    def backfill_trace_links(self) -> int:
        """Link existing chat rows to reply traces when evidence allows.

        Trigger messages are linked by their platform message id. Sent messages
        are linked only when the trace ended within two minutes and the stored
        preview matches the sent text prefix; this is a display convenience, not
        a claim about model attribution.
        """
        updated = 0
        with self._lock:
            try:
                traces = self.conn.execute(
                    "SELECT trace_id, scope_id, trigger_message_id, ended_at, "
                    "final_preview FROM reply_trace WHERE final_preview IS NOT NULL "
                    "OR trigger_message_id IS NOT NULL"
                ).fetchall()
                for trace in traces:
                    if trace["trigger_message_id"]:
                        cur = self.conn.execute(
                            "UPDATE chat_message SET trace_id=? WHERE scope_id=? "
                            "AND platform_message_id=? AND trace_id IS NULL",
                            (
                                trace["trace_id"],
                                trace["scope_id"],
                                trace["trigger_message_id"],
                            ),
                        )
                        updated += max(0, cur.rowcount or 0)
                    preview = normalize_text(trace["final_preview"] or "")
                    end = float(trace["ended_at"] or 0)
                    if not preview or not end:
                        continue
                    candidates = self.conn.execute(
                        "SELECT id, text FROM chat_message WHERE scope_id=? "
                        "AND direction='out' AND trace_id IS NULL "
                        "AND ts BETWEEN ? AND ?",
                        (trace["scope_id"], end - 120, end + 120),
                    ).fetchall()
                    for candidate in candidates:
                        sent = normalize_text(candidate["text"] or "")
                        sample = min(len(sent), len(preview), 80)
                        if sample and sent[:sample] == preview[:sample]:
                            self.conn.execute(
                                "UPDATE chat_message SET trace_id=? WHERE id=?",
                                (trace["trace_id"], candidate["id"]),
                            )
                            updated += 1
                            break
            except sqlite3.Error as exc:
                self._last_error = f"backfill failed: {exc}"
        return updated
