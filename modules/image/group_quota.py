"""Persistent successful drawing counts, with in-process queue reservations."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


BEIJING = timezone(timedelta(hours=8))


class QuotaExceeded(ValueError):
    pass


@dataclass(frozen=True)
class Reservation:
    token: str
    day: str
    actor: str


@dataclass(frozen=True)
class QuotaStatus:
    day: str
    used: int
    pending: int
    remaining: int


class GroupDrawingQuota:
    def __init__(self, path: str | Path, clock: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(BEIJING))
        self._pending: dict[str, Reservation] = {}
        self._lock = threading.Lock()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS usage ("
                "day TEXT NOT NULL, actor TEXT NOT NULL, used INTEGER NOT NULL, "
                "PRIMARY KEY (day, actor))"
            )

    def _status(self, actor: str, limit: int) -> QuotaStatus:
        day = self._clock().astimezone(BEIJING).date().isoformat()
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT used FROM usage WHERE day = ? AND actor = ?", (day, actor)
            ).fetchone()
        used = int(row[0]) if row else 0
        pending = sum(r.day == day and r.actor == actor for r in self._pending.values())
        return QuotaStatus(day, used, pending, max(0, limit - used - pending))

    def status(self, actor: str, limit: int) -> QuotaStatus:
        with self._lock:
            return self._status(actor, limit)

    def today(self, limit: int) -> dict:
        """Read completed and in-flight usage together without changing it."""
        with self._lock:
            day = self._clock().astimezone(BEIJING).date().isoformat()
            with closing(sqlite3.connect(self.path)) as db:
                used = dict(db.execute("SELECT actor, used FROM usage WHERE day = ?", (day,)))
            pending: dict[str, int] = {}
            for ticket in self._pending.values():
                if ticket.day == day:
                    pending[ticket.actor] = pending.get(ticket.actor, 0) + 1
            return {"day": day, "entries": [
                {"actor": actor, "used": used.get(actor, 0), "pending": pending.get(actor, 0),
                 "remaining": max(0, limit - used.get(actor, 0) - pending.get(actor, 0))}
                for actor in sorted(used.keys() | pending.keys())
            ]}

    def reset_today(self, expected_day: str, actors: list[str] | None = None) -> dict:
        """Reset completed usage only; live reservations must remain valid."""
        with self._lock:
            day = self._clock().astimezone(BEIJING).date().isoformat()
            if expected_day != day:
                raise ValueError("日期已变化，请刷新额度页面后再重置。")
            with closing(sqlite3.connect(self.path)) as db, db:
                rows = db.execute("SELECT actor, used FROM usage WHERE day = ?", (day,)).fetchall()
                selected = [(actor, used) for actor, used in rows if actors is None or actor in actors]
                db.executemany("DELETE FROM usage WHERE day = ? AND actor = ?", [(day, actor) for actor, _ in selected])
            return {"day": day, "reset_users": len(selected), "reset_count": sum(used for _, used in selected)}

    def reserve(self, actor: str, limit: int) -> Reservation:
        with self._lock:
            status = self._status(actor, limit)
            if status.remaining == 0:
                if status.used >= limit:
                    raise QuotaExceeded(
                        f"你今天的免费绘图次数已用完（{status.used}/{limit}），"
                        "北京时间明天 00:00 恢复。"
                    )
                raise QuotaExceeded(
                    f"今日绘图额度已占满：已完成 {status.used}/{limit} 次，"
                    f"排队或生成中 {status.pending} 次。请等任务结束，失败会归还额度。"
                )
            reservation = Reservation(uuid.uuid4().hex, status.day, actor)
            self._pending[reservation.token] = reservation
            return reservation

    def complete(self, reservation: Reservation | None) -> None:
        if reservation is None:
            return
        with self._lock:
            if reservation.token not in self._pending:
                return
            # Charge the day the task was accepted, even if delivery crossed midnight.
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute(
                    "INSERT INTO usage (day, actor, used) VALUES (?, ?, 1) "
                    "ON CONFLICT(day, actor) DO UPDATE SET used = used + 1",
                    (reservation.day, reservation.actor),
                )
            self._pending.pop(reservation.token)

    def release(self, reservation: Reservation | None) -> None:
        if reservation is not None:
            with self._lock:
                self._pending.pop(reservation.token, None)
