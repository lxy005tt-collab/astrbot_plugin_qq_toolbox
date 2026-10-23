"""Read-only import of AstrBot's native history.

Two native sources are used:

- ``provider_stats``: native per-reply summaries. Imported as a separate
  ``native_reply_summary`` population so it is never added to the per-request
  numbers collected by this plugin.
- ``platform_message_history``: native platform message log (group cap applies).
  Rows older than this plugin's install time are imported as history. Newer
  user rows are covered by live capture; newer bot rows are imported when no
  matching live send record exists.

No native table is ever written or altered.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

from .util import clip, json_dumps, json_loads, parse_ts, parse_umo, scope_id_for, utc_now

PROVIDER_STATS_BATCH = 1000
PLATFORM_HISTORY_BATCH = 1000


def _open_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA query_only=ON")
    return conn


def _content_parts(raw: Any) -> list[dict]:
    payload = json_loads(raw, {})
    if isinstance(payload, dict):
        parts = payload.get("message")
        if isinstance(parts, list):
            return [p for p in parts if isinstance(p, dict)]
    if isinstance(raw, list):
        return [p for p in raw if isinstance(p, dict)]
    return []


def _content_role(raw: Any) -> str:
    payload = json_loads(raw, {})
    if isinstance(payload, dict):
        role = str(payload.get("type") or "user").lower()
        if role in ("bot", "assistant", "self"):
            return "out"
        return "in"
    return "in"


def _parts_text(parts: list[dict]) -> str:
    chunks = []
    for part in parts:
        ptype = str(part.get("type") or "")
        if ptype == "plain":
            chunks.append(str(part.get("text") or ""))
        elif ptype == "reply":
            quoted = str(part.get("text") or "")
            if quoted:
                chunks.append(f"[引用] {quoted}")
        elif ptype == "at":
            name = str(part.get("name") or part.get("user_id") or "")
            chunks.append(f"@{name}")
        elif ptype in ("image", "record", "video", "file", "face", "json", "forward"):
            chunks.append(f"[{ptype}]")
    return "".join(chunks).strip()


def _sanitize_parts(parts: list[dict], limit: int = 8000) -> list[dict]:
    out: list[dict] = []
    for part in parts[:120]:
        ptype = clip(part.get("type") or "unknown", 40)
        item: dict[str, Any] = {"type": ptype}
        for key in ("text", "message_id", "sender_name", "user_id", "name", "id"):
            if key in part:
                item[key] = clip(part.get(key), limit)
        if ptype == "plain" and "text" in item:
            item["text"] = clip(item["text"], limit)
        out.append(item)
    return out


class NativeReader:
    def __init__(
        self,
        native_db_path: Path,
        storage: Any,
        logger: Any = None,
        max_message_chars: int = 8000,
    ) -> None:
        self.native_db_path = Path(native_db_path)
        self.storage = storage
        self.logger = logger
        self.max_message_chars = max_message_chars

    async def import_all(self) -> dict:
        if not self.native_db_path.is_file():
            return {"ok": False, "reason": "native database not found"}

        live_started_at = float(self.storage.get_meta("live_started_at") or utc_now())
        checkpoints = {
            row["name"]: row for row in await self.storage.fetch_all(
                "SELECT name, cursor FROM import_checkpoint"
            )
        }

        result = await asyncio.to_thread(
            self._read_native,
            checkpoints,
            live_started_at,
        )
        events = result.pop("events", [])
        alias_map = result.pop("aliases", {})
        if events:
            await self.storage.write_events(events)
        # Refresh aliases even when no new rows arrived.
        alias_events = [
            {"type": "scope_alias", "umo": umo, "alias": alias}
            for umo, alias in alias_map.items()
        ]
        if alias_events:
            await self.storage.write_events(alias_events)
        result["ok"] = True
        result["native_db"] = str(self.native_db_path)
        return result

    # ------------------------------------------------------------------ thread
    def _read_native(self, checkpoints: dict, live_started_at: float) -> dict:
        events: list[dict] = []
        coverage: dict[str, Any] = {"live_started_at": live_started_at}
        try:
            conn = _open_ro(self.native_db_path)
        except sqlite3.Error as exc:
            return {"ok": False, "reason": f"cannot open native db: {exc}"}

        try:
            alias_map: dict[str, str] = {}
            try:
                for row in conn.execute(
                    "SELECT umo, auto_name, user_alias FROM umo_aliases"
                ):
                    name = (row["user_alias"] or row["auto_name"] or "").strip()
                    if row["umo"] and name and name != "None":
                        alias_map[str(row["umo"])] = clip(name, 200)
            except sqlite3.Error:
                alias_map = {}

            stats_cursor = int((checkpoints.get("native_provider_stats") or {}).get("cursor") or 0)
            stats_batch = f"native_stats_{int(utc_now())}"
            rows = conn.execute(
                "SELECT * FROM provider_stats WHERE id > ? ORDER BY id LIMIT ?",
                (stats_cursor, PROVIDER_STATS_BATCH),
            ).fetchall()
            imported_stats = 0
            skipped_live = 0
            max_stat_id = stats_cursor
            stat_times: list[float] = []
            for row in rows:
                max_stat_id = max(max_stat_id, int(row["id"]))
                start = parse_ts(row["start_time"]) or parse_ts(row["created_at"])
                if start is not None:
                    stat_times.append(start)
                if start is not None and start >= live_started_at:
                    skipped_live += 1
                    continue
                if str(row["agent_type"] or "") != "internal":
                    continue
                events.append(
                    {
                        "type": "native_summary",
                        "row": {
                            "native_id": int(row["id"]),
                            "umo": row["umo"],
                            "conversation_id": row["conversation_id"],
                            "provider_id": row["provider_id"],
                            "provider_model": row["provider_model"],
                            "status": row["status"],
                            "input_other": row["token_input_other"],
                            "input_cached": row["token_input_cached"],
                            "output": row["token_output"],
                            "start_time": start,
                            "end_time": parse_ts(row["end_time"]),
                            "ttft": float(row["time_to_first_token"] or 0),
                            "agent_type": row["agent_type"],
                        },
                    }
                )
                imported_stats += 1
            if max_stat_id > stats_cursor:
                events.append(
                    {
                        "type": "checkpoint",
                        "name": "native_provider_stats",
                        "cursor": max_stat_id,
                        "batch": stats_batch,
                        "coverage": {
                            "imported": imported_stats,
                            "skipped_after_live": skipped_live,
                            "last_native_id": max_stat_id,
                        },
                    }
                )
            coverage["provider_stats"] = {
                "cursor": max_stat_id,
                "imported": imported_stats,
                "skipped_after_live": skipped_live,
                "min_start": min(stat_times) if stat_times else None,
                "max_start": max(stat_times) if stat_times else None,
                "batch_limit": PROVIDER_STATS_BATCH,
                "reached_limit": len(rows) >= PROVIDER_STATS_BATCH,
            }

            msg_cursor = int((checkpoints.get("native_platform_history") or {}).get("cursor") or 0)
            msg_batch = f"native_pmh_{int(utc_now())}"
            msg_rows = conn.execute(
                "SELECT * FROM platform_message_history WHERE id > ? ORDER BY id LIMIT ?",
                (msg_cursor, PLATFORM_HISTORY_BATCH),
            ).fetchall()
            imported_msgs = 0
            skipped_live_msgs = 0
            max_msg_id = msg_cursor
            msg_times: list[float] = []
            for row in msg_rows:
                max_msg_id = max(max_msg_id, int(row["id"]))
                ts = parse_ts(row["created_at"]) or utc_now()
                msg_times.append(ts)
                direction = _content_role(row["content"])
                if direction == "in" and ts >= live_started_at:
                    skipped_live_msgs += 1
                    continue
                scope = self._scope_from_row(row)
                if scope is None:
                    continue
                parts = _content_parts(row["content"])
                text = _parts_text(parts)
                if not text and not parts:
                    continue
                reply_to = None
                for part in parts:
                    if str(part.get("type") or "") == "reply":
                        reply_to = clip(part.get("message_id"), 120)
                        break
                events.append({"type": "scope_upsert", "scope": scope})
                events.append(
                    {
                        "type": "message",
                        "row": {
                            "scope_id": scope["scope_id"],
                            "source": "native_pmh",
                            "native_id": f"pmh:{int(row['id'])}",
                            "platform_message_id": None,
                            "direction": direction,
                            "sender_id": row["sender_id"] or ("bot" if direction == "out" else None),
                            "sender_name": clip(row["sender_name"], 200) or ("bot" if direction == "out" else None),
                            "ts": ts,
                            "text": clip(text, self.max_message_chars),
                            "content_json": json_dumps(_sanitize_parts(parts, self.max_message_chars)),
                            "component_types": json_dumps(
                                sorted({str(p.get("type") or "unknown") for p in parts})
                            ),
                            "reply_to_message_id": reply_to,
                            "status": "native_recorded",
                            "status_evidence": "native_platform_message_history",
                            "llm_checkpoint_id": row["llm_checkpoint_id"],
                            "imported_batch": msg_batch,
                            "dedupe_out": direction == "out",
                        },
                    }
                )
                imported_msgs += 1
            if max_msg_id > msg_cursor:
                events.append(
                    {
                        "type": "checkpoint",
                        "name": "native_platform_history",
                        "cursor": max_msg_id,
                        "batch": msg_batch,
                        "coverage": {
                            "imported": imported_msgs,
                            "skipped_after_live": skipped_live_msgs,
                            "last_native_id": max_msg_id,
                        },
                    }
                )
            coverage["platform_messages"] = {
                "cursor": max_msg_id,
                "imported": imported_msgs,
                "skipped_after_live": skipped_live_msgs,
                "min_created": min(msg_times) if msg_times else None,
                "max_created": max(msg_times) if msg_times else None,
                "batch_limit": PLATFORM_HISTORY_BATCH,
                "reached_limit": len(msg_rows) >= PLATFORM_HISTORY_BATCH,
                "note": "原生群消息每个会话有数量上限；私聊仅原生已保存的记录可用。",
            }
        except sqlite3.Error as exc:
            return {
                "ok": False,
                "reason": f"native read failed: {type(exc).__name__}: {exc}",
                "events": events,
                "coverage": coverage,
            }
        finally:
            conn.close()

        coverage["notes"] = [
            "原生 provider_stats 是每轮回复的汇总，不等同于单次 API 请求。",
            "安装后由本插件直接采集的请求与原生汇总分开显示，不重复相加。",
            "原生平台消息覆盖率受 AstrBot 自身的数量上限影响，不能当作完整永久档案。",
        ]
        events.append(
            {
                "type": "checkpoint",
                "name": "native_coverage",
                "cursor": "",
                "batch": f"native_coverage_{int(utc_now())}",
                "coverage": coverage,
            }
        )
        return {"events": events, "coverage": coverage, "aliases": alias_map}

    def _scope_from_row(self, row: sqlite3.Row) -> dict | None:
        platform_id = str(row["platform_id"] or "").strip()
        user_id = str(row["user_id"] or "").strip()
        if not platform_id:
            return None
        message_type = "FriendMessage"
        peer_id = user_id
        umo = ""
        if ":" in user_id:
            p, mtype, peer = parse_umo(user_id)
            platform_id = p or platform_id
            message_type = mtype or "GroupMessage"
            peer_id = peer
            umo = user_id
        else:
            umo = f"{platform_id}:FriendMessage:{user_id}"
        if not peer_id:
            return None
        is_group = "GroupMessage" in message_type
        display = None
        if is_group:
            display = f"群 {peer_id}"
        elif row["sender_name"] and str(row["sender_name"]) not in ("bot", "None"):
            display = clip(row["sender_name"], 200)
        scope_id = scope_id_for(platform_id, message_type, peer_id)
        return {
            "scope_id": scope_id,
            "platform_id": platform_id,
            "bot_self_id": "",
            "message_type": message_type,
            "peer_id": peer_id,
            "umo": umo,
            "display_name": display,
            "alias": None,
            "is_group": is_group,
            "first_seen": parse_ts(row["created_at"]),
            "last_seen": parse_ts(row["created_at"]),
        }
