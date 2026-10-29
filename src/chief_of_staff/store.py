"""SQLite persistence and user-scoped task operations."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def _init_db(self) -> None:
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
              id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','done')),
              created_at TEXT NOT NULL, due_at TEXT, project TEXT, priority INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_user_status_due ON tasks(user_id,status,due_at);
            CREATE TABLE IF NOT EXISTS notes (
              id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, body TEXT NOT NULL,
              tags TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reminders (
              id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, task_id INTEGER NOT NULL,
              due_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
                CHECK(state IN ('pending','sending','sent','cancelled')),
              attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
              UNIQUE(task_id), FOREIGN KEY(task_id) REFERENCES tasks(id)
            );
            CREATE TABLE IF NOT EXISTS reminder_delivery_log (
              id INTEGER PRIMARY KEY, reminder_id INTEGER NOT NULL, attempted_at TEXT NOT NULL,
              outcome TEXT NOT NULL, detail TEXT
            );
            CREATE TABLE IF NOT EXISTS user_settings (
              user_id INTEGER PRIMARY KEY, timezone TEXT NOT NULL DEFAULT 'UTC',
              quiet_start INTEGER NOT NULL DEFAULT 23, quiet_end INTEGER NOT NULL DEFAULT 8
            );
            CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(1,CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS daily_briefing_delivery(user_id INTEGER NOT NULL,local_date TEXT NOT NULL,state TEXT NOT NULL,sent_at TEXT,PRIMARY KEY(user_id,local_date));
            """)

    @staticmethod
    def now() -> str:
        return datetime.now(UTC).isoformat()

    def set_timezone(self, user_id: int, timezone_name: str) -> None:
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Unknown timezone; use an IANA name such as Europe/Moscow") from exc
        with self._connect() as db:
            db.execute("INSERT INTO user_settings(user_id,timezone) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET timezone=excluded.timezone", (user_id, timezone_name))

    def get_timezone(self, user_id: int) -> str:
        with self._connect() as db:
            row = db.execute("SELECT timezone FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
        return str(row[0]) if row else "UTC"

    def ensure_user_settings(self, user_id: int, timezone_name: str = "UTC", quiet_start: int = 23, quiet_end: int = 8) -> None:
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Unknown timezone; use an IANA name such as Europe/Moscow") from exc
        if not 0 <= quiet_start <= 23 or not 0 <= quiet_end <= 23:
            raise ValueError("Quiet hour values must be between 0 and 23")
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO user_settings(user_id,timezone,quiet_start,quiet_end) VALUES(?,?,?,?)", (user_id, timezone_name,quiet_start,quiet_end))

    def set_quiet_hours(self, user_id: int, start: int, end: int) -> None:
        if not 0 <= start <= 23 or not 0 <= end <= 23:
            raise ValueError("Quiet hour values must be between 0 and 23")
        tz = self.get_timezone(user_id)
        with self._connect() as db:
            db.execute("INSERT INTO user_settings(user_id,timezone,quiet_start,quiet_end) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET quiet_start=excluded.quiet_start,quiet_end=excluded.quiet_end", (user_id,tz,start,end))

    def claim_daily_briefing(self, user_id: int, local_date: str) -> bool:
        with self._connect() as db:
            cur = db.execute("INSERT OR IGNORE INTO daily_briefing_delivery(user_id,local_date,state) VALUES(?,?,'sending')", (user_id,local_date))
            return bool(cur.rowcount)

    def finish_daily_briefing(self, user_id: int, local_date: str, sent: bool) -> None:
        with self._connect() as db:
            db.execute("UPDATE daily_briefing_delivery SET state=?,sent_at=? WHERE user_id=? AND local_date=? AND state='sending'", ("sent" if sent else "failed",self.now() if sent else None,user_id,local_date))

    def add_task(self, user_id: int, title: str, due_at: datetime | None = None,
                 project: str | None = None, priority: int = 0) -> int:
        title = title.strip()
        if not title:
            raise ValueError("Task title cannot be empty")
        if not 0 <= priority <= 3:
            raise ValueError("Priority must be between 0 and 3")
        if due_at and due_at.tzinfo is None:
            raise ValueError("Due date must include a timezone")
        utc_due = due_at.astimezone(UTC).isoformat() if due_at else None
        with self._connect() as db:
            cur = db.execute("INSERT INTO tasks(user_id,title,created_at,due_at,project,priority) VALUES(?,?,?,?,?,?)", (user_id,title,self.now(),utc_due,project,priority))
            task_id = int(cur.lastrowid)
            if utc_due:
                db.execute("INSERT INTO reminders(user_id,task_id,due_at) VALUES(?,?,?)", (user_id,task_id,utc_due))
        return task_id

    def add_note(self, user_id: int, body: str, tags: list[str] | None = None) -> int:
        body = body.strip()
        if not body:
            raise ValueError("Note cannot be empty")
        clean_tags = sorted({t.strip().lstrip("#").lower() for t in (tags or []) if t.strip()})
        with self._connect() as db:
            cur = db.execute("INSERT INTO notes(user_id,body,tags,created_at) VALUES(?,?,?,?)", (user_id,body,",".join(clean_tags),self.now()))
            return int(cur.lastrowid)

    def complete(self, user_id: int, task_id: int) -> bool:
        with self._connect() as db:
            cur = db.execute("UPDATE tasks SET status='done' WHERE id=? AND user_id=? AND status='open'", (task_id,user_id))
            if cur.rowcount:
                db.execute("UPDATE reminders SET state='cancelled' WHERE task_id=? AND user_id=? AND state='pending'", (task_id,user_id))
            return bool(cur.rowcount)

    def tasks(self, user_id: int, state: str = "open") -> list[sqlite3.Row]:
        with self._connect() as db:
            return list(db.execute("SELECT * FROM tasks WHERE user_id=? AND status=? ORDER BY COALESCE(due_at,'9999'),id", (user_id,state)))

    def search(self, user_id: int, query: str) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        needle = f"%{query.strip()}%"
        with self._connect() as db:
            tasks = list(db.execute("SELECT * FROM tasks WHERE user_id=? AND title LIKE ? ORDER BY id DESC", (user_id,needle)))
            notes = list(db.execute("SELECT * FROM notes WHERE user_id=? AND (body LIKE ? OR tags LIKE ?) ORDER BY id DESC", (user_id,needle,needle)))
        return tasks, notes

    def due_reminders(self, now: datetime) -> list[sqlite3.Row]:
        if now.tzinfo is None:
            raise ValueError("Clock must be timezone-aware")
        with self._connect() as db:
            return list(db.execute("SELECT r.*,t.title,s.timezone,s.quiet_start,s.quiet_end FROM reminders r JOIN tasks t ON t.id=r.task_id LEFT JOIN user_settings s ON s.user_id=r.user_id WHERE r.state='pending' AND r.due_at<=? AND t.status='open' ORDER BY r.due_at", (now.astimezone(UTC).isoformat(),)))

    def claim_reminder(self, reminder_id: int, user_id: int) -> bool:
        """Atomically claim one reminder; a claimed item is never auto-retried (at-most-once)."""
        with self._connect() as db:
            cur = db.execute("UPDATE reminders SET state='sending',attempts=attempts+1 WHERE id=? AND user_id=? AND state='pending'", (reminder_id,user_id))
            return bool(cur.rowcount)

    def finish_reminder(self, reminder_id: int, sent: bool, detail: str = "") -> None:
        outcome = "sent" if sent else "failed"
        with self._connect() as db:
            db.execute("UPDATE reminders SET state=?,last_error=? WHERE id=? AND state='sending'", ("sent" if sent else "cancelled",None if sent else detail[:500],reminder_id))
            db.execute("INSERT INTO reminder_delivery_log(reminder_id,attempted_at,outcome,detail) VALUES(?,?,?,?)", (reminder_id,self.now(),outcome,detail[:500]))
